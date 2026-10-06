"""Session dispatch tools exposed only to the orchestrator Claude session."""

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig


def _session_input_ready(session: Any) -> bool:
    # raw-ptyは承認Brokerを通らず、確認プロンプトへ「y」を送れてしまうため対象外。
    # modeが不明なセッションも拒否する(fail-closed)。
    if not session.is_alive():
        return False
    return getattr(session, "mode", None) == "wrapped" and session._runner is None


async def list_sessions(
    manager: Any, orchestrator_name: str
) -> list[dict[str, object]]:
    """Return peer sessions and whether each can currently receive input."""
    result = []
    for name, session in manager.sessions.items():
        if name == orchestrator_name:
            continue
        result.append(
            {
                "name": name,
                "harness": session.harness,
                "mode": getattr(session, "mode", "unknown"),
                "alive": session.is_alive(),
                "accepts_input": _session_input_ready(session),
            }
        )
    return result


async def send_to_session(
    manager: Any, orchestrator_name: str, name: str, text: str
) -> str:
    """Send one task through the same write path used by HTTP /send."""
    if name == orchestrator_name:
        return "Error: orchestrator cannot send a task to itself."
    session = manager.sessions.get(name)
    if session is None:
        return f"Error: session does not exist: {name}."
    if not session.is_alive():
        return f"Error: session is not alive: {name}."
    if not isinstance(text, str) or not text.strip():
        return "Error: text must not be empty."
    if len(text) > 4000:
        return "Error: text must be at most 4000 characters."
    if getattr(session, "mode", None) != "wrapped":
        return (
            f"Error: {name} はwrappedセッションではないため、指示を送れません"
            "(承認Brokerを通らない入力は許可しない)。"
        )
    if session._runner is not None:
        return f"Error: {name} はすでに作業中。入力は受け付けられない状態です。"
    session.write(text + "\r")
    return f"Success: instruction sent to {name}."


async def read_session_output(
    manager: Any, orchestrator_name: str, name: str, tail: int
) -> str:
    """Read bounded tail output from a peer session."""
    if name == orchestrator_name:
        return "Error: orchestrator cannot read its own output through this tool."
    session = manager.sessions.get(name)
    if session is None:
        return f"Error: session does not exist: {name}."
    bounded_tail = max(1, min(4000, int(tail)))
    return session.output_text(bounded_tail)


def create_orchestration_server(
    manager: Any, orchestrator_name: str
) -> McpSdkServerConfig:
    """Build the in-process MCP server bound to one orchestrator session."""

    @tool("list_sessions", "List peer sessions and their current input readiness.", {})
    async def list_sessions_tool(_args: dict[str, Any]) -> dict[str, Any]:
        sessions = await list_sessions(manager, orchestrator_name)
        return {
            "content": [
                {"type": "text", "text": json.dumps(sessions, ensure_ascii=False)}
            ]
        }

    @tool(
        "send_to_session",
        "Send an instruction to an available peer session. Requires human approval.",
        {
            "type": "object",
            "properties": {"name": {"type": "string"}, "text": {"type": "string"}},
            "required": ["name", "text"],
        },
    )
    async def send_to_session_tool(args: dict[str, Any]) -> dict[str, Any]:
        result = await send_to_session(
            manager, orchestrator_name, str(args.get("name", "")), args.get("text", "")
        )
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        "read_session_output",
        "Read the recent output of a peer session.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tail": {"type": "integer", "minimum": 1, "maximum": 4000},
            },
            "required": ["name", "tail"],
        },
    )
    async def read_session_output_tool(args: dict[str, Any]) -> dict[str, Any]:
        result = await read_session_output(
            manager,
            orchestrator_name,
            str(args.get("name", "")),
            int(args.get("tail", 4000)),
        )
        return {"content": [{"type": "text", "text": result}]}

    return create_sdk_mcp_server(
        "orchestration",
        tools=[list_sessions_tool, send_to_session_tool, read_session_output_tool],
    )
