"""
runner共通の基底クラス。

ClaudeRunner(claude-agent-sdk)とCodexRunner(codex mcp-server)で、
以下の挙動を完全に一致させるための共通実装を持つ:
- 3層ゲート(_gate): rules.check_destructive一致→(ollamaの判定を経由せず)
  explain_operationで説明の上で必ず人間の承認待ち / 不一致→judge_permissionの
  一次判定→ALLOWなら続行、ESCALATEなら説明つきで人間の承認待ち
- 会話一次受付(_conversational_reply): judge_conversational_questionで
  not_question/allow/escalateを判定し、escalateは人間の承認待ち
- ダッシュボード連携(approve/deny/_wait_for_approval): asyncio.Eventを
  loop.call_soon_threadsafeで橋渡しする

既知の制限: 承認待ちは1件のみを想定(tasks/lessons.md参照)。

参照: docs/agent_wrapper_sdk_integration_plan.md セクション4
"""

import asyncio
import threading

from .. import ollama_client, rules
from ..wrapper import Notifier, SharedState


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
        self._approved: bool = False

    def start(self) -> threading.Thread:
        self.state.status = "running"
        thread = threading.Thread(target=self._run_in_thread, daemon=True)
        thread.start()
        return thread

    def _run_in_thread(self) -> None:
        try:
            asyncio.run(self._run_async())
        finally:
            self.state.status = "stopped"
            self.notifier("high", "エージェント終了", "セッションが終了しました")

    async def _run_async(self) -> None:
        raise NotImplementedError

    async def _gate(self, text: str) -> bool:
        """ツール実行の承認判定。Trueなら承認(実行してよい)。

        wrapper.pyの_handle_lineと1対1対応: 破壊的一致は必ず人間確認、
        それ以外はollama一次判定を経てALLOW/人間エスカレーション。
        """
        match = rules.check_destructive(text)
        if match.matched:
            explanation = ollama_client.explain_operation(text, model=self.ollama_model)
            reason = f"{match.reason}\n{explanation}"
            self.state.set_waiting("destructive", reason)
            self.notifier("high", "削除系の操作を検知", reason)
            return await self._wait_for_approval()

        judged = ollama_client.judge_permission(text, model=self.ollama_model)
        if judged["decision"] == "allow":
            self.state.append_log(f"(ollama自動承認: {text})")
            return True

        explanation = ollama_client.explain_operation(text, model=self.ollama_model)
        self.state.set_waiting("permission_escalated", explanation)
        self.notifier("medium", "権限確認(要判断)", explanation)
        return await self._wait_for_approval()

    async def _conversational_reply(self, last_text: str) -> str | None:
        """エージェントの会話文への一次受付。返答文を返す。not_questionならNone。"""
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
        approved = await self._wait_for_approval()
        return "y" if approved else "n。却下されました。別の方法を検討してください。"

    async def _wait_for_approval(self) -> bool:
        self._loop = asyncio.get_running_loop()
        self._approve_event = asyncio.Event()
        self._approved = False
        await self._approve_event.wait()
        self.state.set_running()
        return self._approved

    def approve(self) -> None:
        """ダッシュボードからの承認操作。待機中でなければ何もしない。"""
        if self.state.snapshot()["status"] != "waiting_human":
            return
        self.state.append_log("(人間が承認しました)")
        self._approved = True
        self._notify_event()

    def deny(self) -> None:
        """ダッシュボードからの却下操作。待機中でなければ何もしない。"""
        if self.state.snapshot()["status"] != "waiting_human":
            return
        self.state.append_log("(人間が却下しました)")
        self._approved = False
        self._notify_event()

    def _notify_event(self) -> None:
        if self._loop is not None and self._approve_event is not None:
            self._loop.call_soon_threadsafe(self._approve_event.set)

    def stop(self) -> None:
        self.state.status = "stopped"
