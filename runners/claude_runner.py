"""
claude-agent-sdk経由で実際のClaude Codeを動かすrunner。

役割:
- PreToolUseフック(Bash/Write/Edit)は常に"ask"を返し、判定をcan_use_toolに委ねる
  (can_use_tool単体では「Claudeが安全と自己判断した呼び出し」を拾えない。
  GitHub Issue #912で仕様と確認済み)
- can_use_toolが共通ゲート(base.pyのApprovalRunnerBase._gate())で判定する:
  rules.check_destructive()に一致すれば(ollamaのALLOW/ESCALATE判定を経由せず)
  ollama_client.explain_operation()で内容を説明した上で必ず人間の承認を待つ。
  一致しなければollama_client.judge_permission()の一次判定を経て、ALLOWならそのまま
  続行、そうでなければ同様に説明文を添えて人間の承認を待つ。
  却下時はPermissionResultDeny(message=人間のメッセージ, interrupt=False)を返す
- ClaudeSDKClient(query()ではなく)を使い、Claudeがツール呼び出しではなく会話文で
  「y/nで確認してください」等の返答を求めてきた場合は、
  ollama_client.judge_conversational_question()でnot_question/allow/escalateを
  一次判定する(共通実装: ApprovalRunnerBase._conversational_reply())。
  この会話応答はあくまで「進めてよいか」という相槌への一次対応であり、実際の
  ツール実行の安全性はPreToolUse/can_use_tool側のrules.check_destructive()判定で
  別途独立して担保される。
- setting_sources=["project"] を指定し、この操作者個人のグローバル設定
  (~/.claude/CLAUDE.md等)がラップ対象の(無関係な)セッションに紛れ込まないようにする。
  ただしこれだけでは混入を完全には防げないことが実機で判明している(原因未確定)。
- AgentWrapper(wrapper.py, mock/subprocess方式)と同じ公開インターフェース
  (start/respond/approve/deny/stop)を持ち、dashboard.py/main.pyから透過的に扱える。

承認待ちはSharedState上でFIFOキュー化されており、複数のツール呼び出しが同時にaskへ
倒れても、先頭1件ずつ独立して解除される。

参照: docs/agent_wrapper_sdk_integration_plan.md
"""

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import (
    AssistantMessage,
    HookContext,
    HookJSONOutput,
    PermissionResultAllow,
    PermissionResultDeny,
    PreToolUseHookInput,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from .. import rules
from .base import ApprovalRunnerBase
from .tool_describe import GATED_TOOLS, describe_tool_call

_GATED_MATCHER = "|".join(sorted(GATED_TOOLS))


class ClaudeRunner(ApprovalRunnerBase):
    """claude-agent-sdk経由でClaude Codeを動かすrunner(詳細はモジュールdocstring)。

    関連: ApprovalRunnerBase(base.py, 共通ゲート実装), SharedState(wrapper.py),
    rules.py(検知ルール), ollama_client.py(一次判定/説明),
    tool_describe.py(ツール呼び出しの文字列化)。
    """

    async def _run_async(self) -> None:
        options = ClaudeAgentOptions(
            permission_mode="default",
            cwd=self.cwd,
            can_use_tool=self._can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=_GATED_MATCHER, hooks=[self._pre_tool_use_hook])  # type: ignore[list-item]
                ]
            },
            setting_sources=["project"],
        )
        async with ClaudeSDKClient(options=options) as client:
            await client.query(self.prompt)
            await self._converse(client)

    async def _converse(self, client: ClaudeSDKClient) -> None:
        turns = 0
        while True:
            last_text = self._read_turn(await self._collect_turn(client))
            if not last_text:
                return
            reply = await self._conversational_reply(last_text)
            if reply is None:
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
            await client.query(reply)
            turns += 1

    async def _collect_turn(self, client: ClaudeSDKClient) -> list[object]:
        messages: list[object] = []
        async for message in client.receive_response():
            self._handle_message(message)
            messages.append(message)
        return messages

    def _read_turn(self, messages: list[object]) -> str:
        last_text = ""
        for message in messages:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        last_text = block.text
        return last_text

    def _handle_message(self, message: object) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    self.state.append_log(block.text)
                elif isinstance(block, ToolUseBlock):
                    self.state.append_log(f"[ツール実行] {block.name} {block.input}")
                else:
                    self._log_unknown_block(block)

    def _log_unknown_block(self, block: object) -> None:
        block_type = type(block).__name__
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            self.state.append_log(f"[{block_type}] {text[:200]}")
            return
        summary = getattr(block, "summary", None)
        if isinstance(summary, str) and summary:
            self.state.append_log(f"[{block_type}] {summary[:200]}")
            return
        content = getattr(block, "content", None)
        if isinstance(content, str) and content:
            self.state.append_log(f"[{block_type}] {content[:200]}")
            return
        self.state.append_log(f"[{block_type}] {block}")

    async def _pre_tool_use_hook(
        self,
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
            }
        }

    async def _can_use_tool(
        self,
        tool_name: str,
        input_data: dict,
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        text = describe_tool_call(tool_name, input_data)
        paths = (
            [str(input_data.get("file_path", ""))]
            if tool_name in ("Write", "Edit")
            else None
        )
        l1_facts = rules.describe_l1_facts(paths, self.cwd)
        response = await self._gate(text, l1_facts)
        if response.action == "approve":
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(
            message=response.to_agent_text(),
            interrupt=False,
        )
