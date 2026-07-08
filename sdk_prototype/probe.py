"""
claude-agent-sdk の PreToolUse フック + can_use_tool コールバックの組み合わせが
実際にどう動くかを確認する隔離プロトタイプ(Phase 1)。

参照: docs/agent_wrapper_sdk_integration_plan.md セクション5 Phase 1

これまでの検証で分かったこと:
- can_use_tool は「Claude自身が確認が必要と判断した呼び出し」にしか発火しない
  (GitHub Issue #912)。単純なechoコマンドのような「Claudeが安全と自己判断した
  操作」はcan_use_toolを経由せずスルーされる。rules.pyのように「Claudeより
  厳しく、正規表現で強制的に判定したい」という目的には合わない。
- PreToolUse フックはマッチした全てのツール呼び出しで無条件に発火する。戻り値の
  permissionDecisionで "allow"/"deny"/"ask" を直接指定でき、"ask" を返すと
  can_use_tool に処理が渡る。

本検証の設計(rules.py + ollama_client.py + ダッシュボード承認の3層構造に対応):
1. PreToolUse フックが rules.check_destructive() 相当の判定を行う
   - 一致(PROBE_DENY_ME) -> 即 "deny"(can_use_toolを経由しない)
   - 曖昧(PROBE_ASK_ME) -> "ask"(can_use_toolにフォールバック)
   - それ以外 -> "allow"
2. can_use_tool が ollama_client.judge_permission() + ダッシュボード承認 相当を担う
   (ここでは単純にAllowを返す)

実行方法: uv run python agent_wrapper/sdk_prototype/probe.py
"""

import asyncio
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
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

HOOK_LOG: list[str] = []
CALLBACK_LOG: list[str] = []


async def pre_tool_use_hook(
    input_data: PreToolUseHookInput, tool_use_id: str | None, context: HookContext
) -> HookJSONOutput:
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}

    tool_name = input_data.get("tool_name", "")
    command = str(input_data.get("tool_input", {}).get("command", ""))
    HOOK_LOG.append(f"PreToolUse呼び出し: tool={tool_name} command={command!r}")
    print(f"[HOOK] tool={tool_name} command={command!r}")

    if tool_name != "Bash":
        return {}

    if "PROBE_DENY_ME" in command:
        print("[HOOK] -> deny (rules.check_destructive相当)")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "probe: rules.check_destructive相当で拒否",
            }
        }

    if "PROBE_ASK_ME" in command:
        print("[HOOK] -> ask (can_use_toolへフォールバック)")
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "probe: 曖昧なため人間/ollama判定へ",
            }
        }

    print("[HOOK] -> allow")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "probe: 安全と判定",
        }
    }


async def can_use_tool(
    tool_name: str,
    input_data: dict,
    context: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    CALLBACK_LOG.append(f"can_use_tool呼び出し: tool={tool_name} input={input_data}")
    print(f"[CALLBACK] tool={tool_name} input={input_data}")
    print("[CALLBACK] -> Allow (ollama_client.judge_permission相当)")
    return PermissionResultAllow(updated_input=input_data)


async def prompt_stream(text: str) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": "probe",
    }


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    with tempfile.TemporaryDirectory(prefix="agent_wrapper_probe_") as tmpdir:
        tmp_path = Path(tmpdir)

        options = ClaudeAgentOptions(
            permission_mode="default",
            cwd=str(tmp_path),
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Bash", hooks=[pre_tool_use_hook])  # type: ignore[list-item]
                ]
            },
            setting_sources=[],  # SDK隔離モード。グローバルCLAUDE.mdの5原則指示を引き継がせない。
        )

        prompt = (
            "次の3つのBashコマンドを順番に実行してください。"
            "1つ目: echo PROBE_ALLOW_OK "
            "2つ目: echo PROBE_ASK_ME "
            "3つ目: echo PROBE_DENY_ME "
            "拒否された場合はその旨を報告し、次に進んでください。"
            "最後に3つとも簡潔に結果を報告してください。"
        )

        print(f"[INFO] tmpdir={tmp_path}")
        print(f"[INFO] prompt={prompt}")
        print("[INFO] --- query開始 ---")

        async for message in query(prompt=prompt_stream(prompt), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(f"[ASSISTANT TEXT] {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"[ASSISTANT TOOL_USE] {block.name} {block.input}")
            else:
                print(f"[MESSAGE] {type(message).__name__}: {message}")

        print("[INFO] --- query終了 ---")
        print(f"[INFO] PreToolUseフックが呼ばれた回数: {len(HOOK_LOG)}")
        for entry in HOOK_LOG:
            print(f"[INFO] {entry}")
        print(f"[INFO] can_use_toolが呼ばれた回数: {len(CALLBACK_LOG)}")
        for entry in CALLBACK_LOG:
            print(f"[INFO] {entry}")


if __name__ == "__main__":
    asyncio.run(main())
