"""
codex mcp-server にMCPクライアントとして接続し、実際のCodexを動かすrunner。

背景(検証済み・2026-07-09):
- `codex exec`(非対話)は承認ポリシーを強制的にneverに上書きするため、
  PermissionRequestフックも承認要求も一切発生せずゲートできない。
- `codex mcp-server` はcodexツールの引数として approval-policy を受け付け、
  現行0.149.1ではon-requestにすると承認が必要な操作のたびにMCP elicitationが
  クライアント(このrunner)へ届く。エージェント本体(シェル実行・
  apply_patchによるファイル編集)はローカルでフルに動作する。
- codexのelicitation応答はMCP標準(action/content)ではなく、トップレベルの
  {"decision": "approved"|"denied"} を読む独自形式
  (codex-rs/mcp-server/src/exec_approval.rs のExecApprovalResponse。
  codex自身のソースにTODOとして明記されており、パース失敗時は保守的に
  denied扱いになる)。ElicitResultはextra="allow"なのでdecisionを同梱する。
  contentに人間のメッセージを渡す場合はdict形式にする(list不可、pydanticの型制約)。

役割:
- elicitation(exec-approval/patch-approval)が届くたびに、
  ApprovalRunnerBase._gate()(rules.check_destructive→人間確認 /
  judge_permission→ALLOW or 人間確認)で判定し、decision(+人間のメッセージ)を返す
- セッション終了時の最終メッセージが会話上の確認質問であれば、
  _conversational_reply()で一次受付し、codex-replyツールで返答する
- codex-replyに必要なthreadIdは、codexツール応答のstructuredContent
  (実機確認済み: {"threadId": ..., "content": ...})から取得する。
  elicitation経由の取得はフォールバック。かつてはelicitation経由のみだったため、
  elicitationが一度も発生しないまま会話質問が来ると返答できず即終了する
  バグがあった(2026-07-09の実運用で顕在化、修正済み)
- AgentWrapper/ClaudeRunnerと同じ公開インターフェース(start/respond/approve/deny/stop)

既知の制限:
- 承認待ちはSharedState上でFIFOキュー化されており、複数件同時に発生しても先頭1件ずつ独立して解除される
- codexが送る独自通知(codex/event)はmcp SDKの型検証を通らず警告ログが出るが、
  動作には影響しない(elicitation/ツール応答は正常に処理される)。
  ログ肥大の抑制はtasks/todo.md「ログ設計の改善」を参照

参照: docs/agent_wrapper_sdk_integration_plan.md セクション3.2
"""

import logging
import shutil
from typing import Any

import mcp.types as t
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .. import rules
from ..wrapper import summarize_log_text
from .base import ApprovalRunnerBase


class _CodexEventValidationFilter(logging.Filter):
    """codex/event独自通知に限り、MCP SDKの既知の検証警告を落とす。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            "Failed to validate notification" in message and "codex/event" in message
        )


def _install_codex_event_filter() -> None:
    root_logger = logging.getLogger()
    if not any(
        isinstance(item, _CodexEventValidationFilter) for item in root_logger.filters
    ):
        root_logger.addFilter(_CodexEventValidationFilter())


def _codex_cmd() -> str:
    path = shutil.which("codex")
    if not path:
        raise RuntimeError("codex CLI が見つかりません(PATHを確認してください)")
    return path


class CodexRunner(ApprovalRunnerBase):
    """codex mcp-server経由でCodexを動かすrunner(詳細はモジュールdocstring)。

    関連: ApprovalRunnerBase(base.py, 共通ゲート実装), SharedState(wrapper.py),
    rules.py(検知ルール), ollama_client.py(一次判定/説明)。
    """

    # OSサンドボックスの既定はdanger-full-access(サンドボックスなし)。
    # 承認ゲート(approval-policy=on-requestによるelicitation承認)は
    # この設定と無関係に常に有効で、これが一次防壁。サンドボックスは補助壁だが、
    # Windowsでは読み取りすら誤ブロックする不安定さに加え、サンドボックス起因の
    # 失敗を昇格で再実行する承認要求がmcp経由でクライアントに届かない上流バグ
    # (openai/codex#21982系統)があり、2026-07-11の実運用で作業を複数回停止させた
    # ため、人間の判断で既定オフとした(経緯: docs/agent_wrapper_permission_policy.md)。
    # 信頼できないコードを扱う場合は--codex-sandbox workspace-writeを指定する。
    DEFAULT_SANDBOX = "danger-full-access"
    APPROVAL_POLICY = "on-request"

    def __init__(
        self, *args: Any, sandbox: str = DEFAULT_SANDBOX, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._thread_id: str | None = None
        self.sandbox = sandbox

    async def _run_async(self) -> None:
        _install_codex_event_filter()
        self.state.append_log("[codex] codex/eventの既知のMCP検証警告だけを抑制します")
        server = StdioServerParameters(command=_codex_cmd(), args=["mcp-server"])
        async with stdio_client(server) as (read, write):
            async with ClientSession(
                read, write, elicitation_callback=self._on_elicitation
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    "codex",
                    arguments={
                        "prompt": self.initial_prompt,
                        "cwd": self.cwd,
                        "approval-policy": self.APPROVAL_POLICY,
                        "sandbox": self.sandbox,
                    },
                )
                self._capture_thread_id(result)
                last_text = self._log_result(result)
                await self._converse(session, last_text)

    async def _converse(self, session: ClientSession, last_text: str) -> None:
        turns = 0
        plan_approved = self.state.snapshot()["approved_plan"] is not None
        while True:
            if not last_text:
                return
            reply: str | None
            if not plan_approved:
                reply = await self._request_plan_approval(last_text)
                plan_approved = self.state.snapshot()["approved_plan"] is not None
            else:
                reply = await self._conversational_reply(last_text)
            if reply is None:
                return
            if self._thread_id is None:
                self.state.append_log(
                    "(threadId不明のため会話を継続できません。セッションを終了します)"
                )
                return
            if turns >= self._MAX_CONVERSATIONAL_TURNS:
                # 勝手に打ち切らず人間に継続可否を確認する(base.py参照)
                if not await self._confirm_continue_conversation():
                    self.state.append_log("(人間の判断により会話確認を終了します)")
                    self.state.set_stopped(
                        "max_conversation_turns",
                        "会話確認の上限で人間が終了を選択しました",
                    )
                    return
                turns = 0
            self.state.append_log(f"[人間応答送信] {reply}")
            result = await session.call_tool(
                "codex-reply",
                arguments={"threadId": self._thread_id, "prompt": reply},
            )
            self._capture_thread_id(result)
            last_text = self._log_result(result)
            turns += 1

    def _capture_thread_id(self, result: t.CallToolResult) -> None:
        structured = result.structuredContent or {}
        thread_id = structured.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            self._thread_id = thread_id

    def _log_result(self, result: t.CallToolResult) -> str:
        last_text = ""
        for block in result.content:
            if isinstance(block, t.TextContent):
                last_text = block.text
                self.state.append_log(summarize_log_text(block.text))
            else:
                self.state.append_log(
                    f"[codex:{type(block).__name__}] {summarize_log_text(str(block))}"
                )
        return last_text

    def _describe_elicitation(self, params: t.ElicitRequestParams) -> str:
        extra = params.model_extra or {}
        kind = str(extra.get("codex_elicitation", ""))
        if kind == "exec-approval":
            command = extra.get("codex_command") or []
            return " ".join(str(part) for part in command)
        if kind == "patch-approval":
            changes = extra.get("codex_changes") or {}
            paths = ", ".join(str(p) for p in changes)
            return f"apply_patch {paths}"
        return str(getattr(params, "message", "") or extra)

    async def _on_elicitation(
        self,
        context: Any,
        params: t.ElicitRequestParams,
    ) -> t.ElicitResult:
        extra = params.model_extra or {}
        thread_id = extra.get("threadId")
        if isinstance(thread_id, str):
            self._thread_id = thread_id

        text = self._describe_elicitation(params)

        changes = extra.get("codex_changes") or {}

        paths = (
            list(changes)
            if extra.get("codex_elicitation") == "patch-approval"
            else None
        )

        l1_facts = rules.describe_l1_facts(
            [str(path) for path in paths] if paths else None, self.cwd
        )

        self.state.append_log(f"[承認要求] {summarize_log_text(text)}")

        response = await self._gate(text, l1_facts)
        return self._elicit_result(
            response.action == "approve", response.to_agent_text()
        )

    @staticmethod
    def _elicit_result(approved: bool, message: str = "") -> t.ElicitResult:
        content = {"reply": message} if message else {}
        if approved:
            payload: dict[str, object] = {"action": "accept", "decision": "approved"}
            if content:
                payload["content"] = content
            return t.ElicitResult.model_validate(payload)
        payload = {"action": "decline", "decision": "denied"}
        if content:
            payload["content"] = content
        return t.ElicitResult.model_validate(payload)
