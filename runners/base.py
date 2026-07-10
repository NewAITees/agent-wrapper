"""
runner共通の基底クラス。

ClaudeRunner(claude-agent-sdk)とCodexRunner(codex mcp-server)で、
以下の挙動を完全に一致させるための共通実装を持つ:
- 3層ゲート(_gate): rules.check_destructive一致→(ollamaの判定を経由せず)
  explain_operationで説明の上で必ず人間の承認待ち / 不一致→judge_permissionの
  一次判定→ALLOWなら続行、ESCALATEなら説明つきで人間の承認待ち
- 会話一次受付(_conversational_reply): judge_conversational_questionで
  not_question/allow/escalateを判定し、escalateは人間の承認待ち
- ダッシュボード連携(respond/approve/deny/_wait_for_approval):
  人間の応答はHumanResponse(action: approve/explain/deny + 任意メッセージ)として
  受け取り、asyncio.Eventをloop.call_soon_threadsafeで橋渡しする
- 終了理由(stop_reason): 正常完了/エラー/最大会話ターン到達を区別して
  SharedState.set_stopped()に記録する。却下してもセッションは中断しない
  (interrupt=False相当)ため「中断」とは断定せず、却下件数のみ詳細に添える

既知の制限: 承認待ちは1件のみを想定(tasks/lessons.md参照)。

参照: docs/agent_wrapper_sdk_integration_plan.md セクション4
"""

import asyncio
import threading

from .. import ollama_client, rules
from ..wrapper import HumanAction, HumanResponse, Notifier, SharedState


class ApprovalRunnerBase:
    """承認ゲート付きrunnerの共通基盤。AgentWrapper(wrapper.py)と同じ
    公開インターフェース(start/approve/deny/stop)を提供する。

    サブクラスは _run_async() を実装する。
    """

    _MAX_CONVERSATIONAL_TURNS = 5

    def __init__(
        self,
        prompt: str,
        cwd: str,
        state: SharedState,
        ollama_model: str = "gemma4:e4b",
        notifier: Notifier | None = None,
    ) -> None:
        self.prompt = prompt
        self.cwd = cwd
        self.state = state
        self.ollama_model = ollama_model
        self.notifier: Notifier = notifier or (lambda level, title, body: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._approve_event: asyncio.Event | None = None
        self._response = HumanResponse("approve")
        self._deny_count = 0

    def start(self) -> threading.Thread:
        self.state.set_running()
        thread = threading.Thread(target=self._run_in_thread, daemon=True)
        thread.start()
        return thread

    def _run_in_thread(self) -> None:
        try:
            asyncio.run(self._run_async())
            if self.state.snapshot()["status"] != "stopped":
                # 却下してもセッションは継続しうる(interrupt=False)ので「中断」と
                # 断定せず、却下があった事実だけを正常完了の詳細に添える
                complete_detail = "セッションが正常終了しました"
                if self._deny_count:
                    complete_detail += f"(人間の却下 {self._deny_count}件を含む)"
                self.state.set_stopped("normal_complete", complete_detail)
        except Exception as e:
            self.state.append_log(f"(runner error: {e})")
            self.state.set_stopped("error", str(e))
        finally:
            snap = self.state.snapshot()
            detail = snap.get("stop_reason_detail") or snap.get("stop_reason_label")
            self.notifier("high", "エージェント終了", str(detail))

    async def _run_async(self) -> None:
        raise NotImplementedError

    async def _gate(self, text: str) -> bool:
        match = rules.check_destructive(text)
        if match.matched:
            explanation = ollama_client.explain_operation(text, model=self.ollama_model)
            reason = f"{match.reason}\n{explanation}"
            self.state.set_waiting("destructive", reason)
            self.notifier("high", "削除系の操作を検知", reason)
            response = await self._wait_for_approval()
            return response.action == "approve"

        judged = ollama_client.judge_permission(text, model=self.ollama_model)
        if judged["decision"] == "allow":
            self.state.append_log(f"(ollama自動承認: {text})")
            return True

        explanation = ollama_client.explain_operation(text, model=self.ollama_model)
        self.state.set_waiting("permission_escalated", explanation)
        self.notifier("medium", "権限確認(要判断)", explanation)
        response = await self._wait_for_approval()
        return response.action == "approve"

    async def _conversational_reply(self, last_text: str) -> str | None:
        judged = ollama_client.judge_conversational_question(
            last_text, model=self.ollama_model
        )
        if judged["decision"] == "not_question":
            return None
        if judged["decision"] == "allow":
            self.state.append_log("(ollamaが会話上の確認に自動応答: y)")
            return "y"

        explanation = ollama_client.explain_operation(
            last_text, model=self.ollama_model
        )
        self.state.set_waiting("conversational_escalated", explanation)
        self.notifier("medium", "エージェントからの確認(要判断)", explanation)
        response = await self._wait_for_approval()
        return response.to_agent_text()

    async def _wait_for_approval(self) -> HumanResponse:
        self._loop = asyncio.get_running_loop()
        self._approve_event = asyncio.Event()
        self._response = HumanResponse("approve")
        await self._approve_event.wait()
        response = self._response
        self.state.set_running()
        return response

    def respond(self, action: HumanAction, message: str = "") -> None:
        if self.state.snapshot()["status"] != "waiting_human":
            return
        response = HumanResponse(action=action, message=message.strip())
        self._response = response
        if action == "deny":
            self._deny_count += 1
        suffix = f": {response.message}" if response.message else ""
        self.state.append_log(f"(人間が{response.log_label()}しました{suffix})")
        self._notify_event()

    def approve(self) -> None:
        self.respond("approve")

    def deny(self) -> None:
        self.respond("deny")

    def _notify_event(self) -> None:
        if self._loop is not None and self._approve_event is not None:
            self._loop.call_soon_threadsafe(self._approve_event.set)

    def stop(self) -> None:
        self.state.set_stopped("terminated", "stop() が呼ばれました")
