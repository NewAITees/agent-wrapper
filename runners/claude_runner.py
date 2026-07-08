"""
claude-agent-sdk経由で実際のClaude Codeを動かすrunner。

役割:
- PreToolUseフック(Bash/Write/Edit)は常に"ask"を返し、判定をcan_use_toolに委ねる
- can_use_toolがwrapper.pyの_handle_lineと1対1で対応する判定を行う:
  rules.check_destructive()に一致すれば(ollamaのALLOW/ESCALATE判定を経由せず)
  ollama_client.explain_operation()で内容を説明した上で必ず人間の承認を待つ。
  一致しなければollama_client.judge_permission()の一次判定を経て、ALLOWならそのまま
  続行、そうでなければ同様に説明文を添えて人間の承認を待つ。
- ClaudeSDKClient(query()ではなく)を使い、Claudeがツール呼び出しではなく会話文で
  「y/nで確認してください」等の返答を求めてきた場合は、
  ollama_client.judge_conversational_question()でnot_question/allow/escalateを
  一次判定する(judge_permission()と同じ考え方)。allowなら"y"を自動返信、
  escalateなら人間の承認待ちにしてから返信する(最大_MAX_CONVERSATIONAL_TURNS回)。
  この会話応答はあくまで「進めてよいか」という相槌への一次対応であり、実際の
  ツール実行の安全性はPreToolUse/can_use_tool側のrules.check_destructive()判定で
  別途独立して担保される。
- setting_sources=["project"] を指定し、この操作者個人のグローバル設定
  (~/.claude/CLAUDE.md等)がラップ対象の(無関係な)セッションに紛れ込まないようにする。
  ターゲットプロジェクト自身の.claude/settings.json・CLAUDE.mdは尊重する。
- AgentWrapper(wrapper.py, mock/subprocess方式)と同じ公開インターフェース
  (start/approve/deny/stop)を持ち、dashboard.py/main.pyから透過的に扱える。

既知の制限: 承認待ちは1件のみを想定している(mock方式のthreading.Event共有と同種の
制限。tasks/lessons.md参照)。複数のツール呼び出しが同時にaskへ倒れた場合、片方への
承認がもう片方の待機も解除してしまう可能性がある。

参照: docs/agent_wrapper_sdk_integration_plan.md
"""

import asyncio
import threading

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

from .. import ollama_client, rules
from ..wrapper import Notifier, SharedState
from .tool_describe import GATED_TOOLS, describe_tool_call

_GATED_MATCHER = "|".join(sorted(GATED_TOOLS))


class ClaudeRunner:
    """claude-agent-sdk経由でClaude Codeを動かすrunner。AgentWrapperと同じ
    公開インターフェース(start/approve/deny/stop)を持つ。

    関連: SharedState(wrapper.py), rules.py(検知ルール),
    ollama_client.py(一次判定/説明), tool_describe.py(ツール呼び出しの文字列化)。
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
        """応答を読み、Claudeが会話文で確認を求めてきた場合の一次受付を行う。

        judge_conversational_question()でnot_question/allow/escalateを判定し、
        can_use_toolの分岐(rules.check_destructive/judge_permission)と同じ
        考え方で: allowなら自動で続行、escalateなら人間の承認待ちにする。
        """
        for _ in range(self._MAX_CONVERSATIONAL_TURNS):
            last_text = self._read_turn(await self._collect_turn(client))
            if not last_text:
                return

            judged = ollama_client.judge_conversational_question(
                last_text, model=self.ollama_model
            )
            if judged["decision"] == "not_question":
                return

            if judged["decision"] == "allow":
                self.state.append_log("(ollamaが会話上の確認に自動応答: y)")
                await client.query("y")
                continue

            explanation = ollama_client.explain_operation(
                last_text, model=self.ollama_model
            )
            self.state.set_waiting("conversational_escalated", explanation)
            self.notifier("medium", "エージェントからの確認(要判断)", explanation)
            approved = await self._wait_for_approval()
            reply = (
                "y" if approved else "n。却下されました。別の方法を検討してください。"
            )
            await client.query(reply)

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

        match = rules.check_destructive(text)
        if match.matched:
            explanation = ollama_client.explain_operation(text, model=self.ollama_model)
            reason = f"{match.reason}\n{explanation}"
            self.state.set_waiting("destructive", reason)
            self.notifier("high", "削除系の操作を検知", reason)
            approved = await self._wait_for_approval()
            return self._result(approved, input_data)

        judged = ollama_client.judge_permission(text, model=self.ollama_model)
        if judged["decision"] == "allow":
            self.state.append_log(f"(ollama自動承認: {text})")
            return PermissionResultAllow(updated_input=input_data)

        explanation = ollama_client.explain_operation(text, model=self.ollama_model)
        self.state.set_waiting("permission_escalated", explanation)
        self.notifier("medium", "権限確認(要判断)", explanation)
        approved = await self._wait_for_approval()
        return self._result(approved, input_data)

    def _result(
        self, approved: bool, input_data: dict
    ) -> PermissionResultAllow | PermissionResultDeny:
        if approved:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="人間が却下しました", interrupt=False)

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
