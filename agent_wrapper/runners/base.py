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
  受け取り、各承認要求ごとの専用完了通知をloop.call_soon_threadsafeで橋渡しする
- 終了理由(stop_reason): 正常完了/エラー/最大会話ターン到達を区別して
  SharedState.set_stopped()に記録する。却下してもセッションは中断しない
  (interrupt=False相当)ため「中断」とは断定せず、却下件数のみ詳細に添える
- 会話確認の上限(_MAX_CONVERSATIONAL_TURNS)に達しても勝手に打ち切らず、
  _confirm_continue_conversation()で人間にエスカレーションする(承認=継続して
  カウンタをリセット / 却下=終了)。打ち切りをラッパーが独断しないため
  (2026-07-11の実運用で、正当な多段確認の途中で勝手に打ち切る問題が顕在化)

承認待ちはSharedState上でFIFOキュー化されており、同時に複数件発生しても
先頭1件ずつ独立して解除される。

参照: docs/agent_wrapper_sdk_integration_plan.md セクション4
"""

import asyncio
import threading
from typing import Any

from .. import ollama_client, rules
from ..wrapper import (
    HumanAction,
    HumanResponse,
    Notifier,
    SharedState,
    format_audit_log,
)


class ApprovalRunnerBase:
    """承認ゲート付きrunnerの共通基盤。AgentWrapper(wrapper.py)と同じ
    公開インターフェース(start/approve/deny/stop)を提供する。

    サブクラスは _run_async() を実装する。
    """

    _MAX_CONVERSATIONAL_TURNS = 5
    PLAN_PROMPT_PREFIX = (
        "作業を始める前に、最初の応答で実装計画だけを提示してください。"
        "含める項目: 目的 / 方針 / 変更範囲(ファイル・モジュール) / 追加依存 / "
        "影響・リスク / 戻し方 / 検証方法。"
        "計画の承認が返ってくるまで作業を開始しないこと。\n\n"
    )

    def __init__(
        self,
        prompt: str,
        cwd: str,
        state: SharedState,
        ollama_model: str = "gemma4:e4b",
        notifier: Notifier | None = None,
        permission: dict[str, Any] | None = None,
    ) -> None:
        self.prompt = prompt
        self.cwd = cwd
        self.state = state
        self.ollama_model = ollama_model
        self.notifier: Notifier = notifier or (lambda level, title, body: None)
        # 役割(server.py ROLE_PERMISSIONS)ごとの強制制限。ClaudeRunnerは
        # PreToolUseで明示拒否だけを適用し、許可操作も共通の3層ゲートへ送る。
        self.permission: dict[str, Any] = permission or {}
        self._deny_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = threading.Event()

    @property
    def initial_prompt(self) -> str:
        return self.PLAN_PROMPT_PREFIX + self.prompt

    def start(self) -> threading.Thread:
        self.state.set_running()
        thread = threading.Thread(target=self._run_in_thread, daemon=True)
        thread.start()
        return thread

    def _run_in_thread(self) -> None:
        asyncio.run(self._run_main())

    async def _run_main(self) -> None:
        # stop()から別スレッド越しにこのタスクをキャンセルできるよう、
        # 実行中のループ/タスクを保持しておく(2026-09-01追加。従来は
        # state.set_stopped()するだけでタスク自体は動き続け、停止後も
        # 新しいターンが進んで承認要求が生成され続ける不具合があった)。
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            if self._stop_requested.is_set():
                return
            await self._run_async()
            if self.state.snapshot()["status"] != "stopped":
                # 却下してもセッションは継続しうる(interrupt=False)ので「中断」と
                # 断定せず、却下があった事実だけを正常完了の詳細に添える
                complete_detail = "セッションが正常終了しました"
                if self._deny_count:
                    complete_detail += f"(人間の却下 {self._deny_count}件を含む)"
                self.state.set_stopped("normal_complete", complete_detail)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.state.append_log(f"(runner error: {e})")
            self.state.set_stopped("error", str(e))
        finally:
            snap = self.state.snapshot()
            detail = snap.get("stop_reason_detail") or snap.get("stop_reason_label")
            self.notifier("high", "エージェント終了", str(detail))

    async def _run_async(self) -> None:
        raise NotImplementedError

    async def _gate(self, text: str, l1_facts: str | None = None) -> HumanResponse:
        """承認判定。人間(またはollama自動承認)のHumanResponseをそのまま返す。

        呼び出し側は response.action == "approve" で許可判定し、却下時の
        メッセージも同じresponseから取ること(共有変数の後読みは要求間で
        文面を取り違えるレースになるため禁止)。
        """
        match = rules.check_destructive(text)
        if match.matched:
            classification = ollama_client.classify_destructive_match(text)
            explanation = ollama_client.explain_operation(text, model=self.ollama_model)
            reason = f"{classification['headline']} / {match.reason}\n{explanation}"
            self.notifier("high", "削除系の操作を検知", reason)
            self.state.append_log(
                format_audit_log("rules", "エスカレーション", text, match.reason)
            )
            return await self._wait_for_approval("destructive", reason, detail=text)

        judged = ollama_client.judge_permission(
            text,
            model=self.ollama_model,
            l1_facts=l1_facts,
            approved_plan=self.state.snapshot()["approved_plan"],
        )
        if judged["decision"] == "allow":
            self.state.append_log(
                format_audit_log("ollama", "自動承認", text, str(judged.get("raw", "")))
            )
            return HumanResponse("approve")

        self.state.append_log(
            format_audit_log(
                "ollama", "エスカレーション", text, str(judged.get("raw", ""))
            )
        )
        explanation = ollama_client.explain_operation(text, model=self.ollama_model)
        self.notifier("medium", "権限確認(要判断)", explanation)
        return await self._wait_for_approval(
            "permission_escalated", explanation, detail=text
        )

    async def _conversational_reply(self, last_text: str) -> str | None:
        judged = ollama_client.judge_conversational_question(
            last_text, model=self.ollama_model
        )
        if judged["decision"] == "not_question":
            return None
        if judged["decision"] == "allow":
            self.state.append_log(
                format_audit_log(
                    "ollama", "自動承認", last_text, str(judged.get("raw", ""))
                )
            )
            return "y"

        self.state.append_log(
            format_audit_log(
                "ollama", "エスカレーション", last_text, str(judged.get("raw", ""))
            )
        )
        explanation = ollama_client.explain_operation(
            last_text, model=self.ollama_model
        )
        self.notifier("medium", "エージェントからの確認(要判断)", explanation)
        response = await self._wait_for_approval(
            "conversational_escalated", explanation, detail=last_text
        )
        return response.to_agent_text()

    async def _confirm_continue_conversation(self) -> bool:
        """会話確認が上限に達したときの継続判断を人間に委ねる。Trueなら継続。"""
        reason = (
            f"エージェントとの会話確認が{self._MAX_CONVERSATIONAL_TURNS}回続いています。"
            "作業を継続させますか?(承認=継続 / 却下=セッション終了)"
        )
        self.notifier("medium", "会話確認が上限に到達", reason)
        response = await self._wait_for_approval("conversation_limit", reason)
        return response.action == "approve"

    async def _request_plan_approval(self, plan_text: str) -> str:
        """応答全文を計画として人間へ提示し、承認時だけ共有状態へ保存する。"""
        # 他の承認待ちと同様にプッシュ通知も送る(外出先で計画到着に気づけるように)
        self.notifier("high", "実装計画の承認待ち", plan_text[:400])
        response = await self._wait_for_approval(
            "plan_approval", "実装計画を承認しますか?", detail=plan_text
        )
        if response.action == "approve":
            self.state.set_approved_plan(plan_text)
            suffix = f"。補足: {response.message}" if response.message else ""
            return f"y(計画を承認します){suffix}"
        return response.to_agent_text()

    async def _wait_for_approval(
        self, kind: str, reason: str, detail: str = ""
    ) -> HumanResponse:
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[HumanResponse] = loop.create_future()

        def on_response(response: HumanResponse) -> None:
            def resolve() -> None:
                if not response_future.done():
                    response_future.set_result(response)

            loop.call_soon_threadsafe(resolve)

        self.state.set_waiting(kind, reason, on_response, detail=detail)
        return await response_future

    def respond(
        self,
        action: HumanAction,
        message: str = "",
        request_id: str | int | None = None,
    ) -> None:
        response = HumanResponse(action=action, message=message.strip())
        resolved = self.state.respond(response, request_id=request_id)
        if resolved is None:
            return
        if action == "deny":
            self._deny_count += 1
        suffix = f": {response.message}" if response.message else ""
        self.state.append_log(f"(人間が{response.log_label()}しました{suffix})")
        target = resolved.detail or resolved.reason
        self.state.append_log(
            format_audit_log(
                "人間",
                response.log_label(),
                target,
                response.message or resolved.reason,
            )
        )

    def approve(self) -> None:
        self.respond("approve")

    def deny(self) -> None:
        self.respond("deny")

    def stop(self) -> None:
        self._stop_requested.set()
        self.state.set_stopped("terminated", "stop() が呼ばれました")
        if (
            self._loop is not None
            and not self._loop.is_closed()
            and self._task is not None
            and not self._task.done()
        ):
            self._loop.call_soon_threadsafe(self._task.cancel)
