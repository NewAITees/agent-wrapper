"""
codex mcp-server にMCPクライアントとして接続し、実際のCodexを動かすrunner。

背景(検証済み・2026-07-09):
- `codex exec`(非対話)は承認ポリシーを強制的にneverに上書きするため、
  PermissionRequestフックも承認要求も一切発生せずゲートできない。
- `codex mcp-server` はcodexツールの引数として approval-policy を受け付け、
  untrustedにすると承認が必要な操作のたびにMCP elicitationが
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
- 承認待ちは1件のみを想定(tasks/lessons.md参照)
- codexが送る独自通知(codex/event)はmcp SDKの型検証を通らず警告ログが出るが、
  動作には影響しない(elicitation/ツール応答は正常に処理される)。
  ログ肥大の抑制はtasks/todo.md「ログ設計の改善」を参照

参照: docs/agent_wrapper_sdk_integration_plan.md セクション3.2
"""

import shutil
from typing import Any

import mcp.types as t
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .base import ApprovalRunnerBase


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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._thread_id: str | None = None

    async def _run_async(self) -> None:
        server = StdioServerParameters(command=_codex_cmd(), args=["mcp-server"])
        async with stdio_client(server) as (read, write):
            async with ClientSession(
                read, write, elicitation_callback=self._on_elicitation
            ) as session:
                await session.initialize()
                result = await session.call_tool(
                    "codex",
                    arguments={
                        "prompt": self.prompt,
                        "cwd": self.cwd,
                        "approval-policy": "untrusted",
                        "sandbox": "workspace-write",
                    },
                )
                self._capture_thread_id(result)
                last_text = self._log_result(result)
                await self._converse(session, last_text)

    async def _converse(self, session: ClientSession, last_text: str) -> None:
        for _ in range(self._MAX_CONVERSATIONAL_TURNS):
            if not last_text:
                return
            reply = await self._conversational_reply(last_text)
            if reply is None:
                return
            if self._thread_id is None:
                self.state.append_log(
                    "(threadId不明のため会話を継続できません。セッションを終了します)"
                )
                return
            self.state.append_log(f"[人間応答送信] {reply}")
            result = await session.call_tool(
                "codex-reply",
                arguments={"threadId": self._thread_id, "prompt": reply},
            )
            self._capture_thread_id(result)
            last_text = self._log_result(result)
        self.state.append_log("(会話確認が最大ターン数に達したため終了します)")
        self.state.set_stopped(
            "max_conversation_turns", "会話確認が最大5ターンに達しました"
        )

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
                self.state.append_log(block.text)
            else:
                self.state.append_log(f"[codex:{type(block).__name__}] {block}")
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
        self.state.append_log(f"[承認要求] {text}")
        approved = await self._gate(text)
        return self._elicit_result(approved, self._response.to_agent_text())

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
