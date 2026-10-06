"""Session dispatch tools exposed only to the orchestrator Claude session."""

import json
import re
import shlex
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from . import rules


_ORCHESTRATOR_APPROVABLE_KINDS = frozenset(
    {"plan_approval", "conversational_escalated", "permission_escalated"}
)


def _legacy_kind(request: Any) -> str:
    metadata = getattr(request, "metadata", None)
    if not isinstance(metadata, dict):
        return "unknown"
    kind = metadata.get("legacy_kind")
    return kind if isinstance(kind, str) else "unknown"


# permission_escalatedをorchestratorが承認できる「簡単で安全」な操作の許可リスト。
# 自由文のブロックリスト(rules.check_destructive)だけでは、rm単体やpython -cなどの
# 未知の削除・外部通信を素通しする(2026-10-06の実機とCodex監査で確認)ため、
# 許可リストに載るものだけを承認可とし、それ以外は人間へ回す。
_READ_ONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("pwd",),
    ("ls",),
    ("dir",),
    ("cat",),
    ("head",),
    ("tail",),
    ("wc",),
    ("grep",),
    ("rg",),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("uv", "run", "pytest"),
    ("uv", "run", "ruff", "check"),
    ("uv", "run", "mypy"),
    ("python", "-m", "pytest"),
)
_SHELL_META = re.compile(r"[;&|<>`$(){}\r\n]")
# 出力ファイル書き込み・外部コマンド実行・プラグイン読み込みにつながる引数。
_FORBIDDEN_FLAG_PARTS = ("output", "ext-diff", "textconv", "exec", "pre")
_FORBIDDEN_SHORT_FLAGS = frozenset({"-o", "-p", "-c", "-f"})
_PROTECTED_DIR_NAMES = frozenset({".git", ".claude", ".agent-server", ".github"})


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_in_cwd(raw: str, cwd: Path) -> Path | None:
    raw = raw.strip().strip("\"'")
    if not raw or raw.startswith("~"):
        return None
    candidate = Path(raw)
    resolved = (cwd / candidate if not candidate.is_absolute() else candidate).resolve(
        strict=False
    )
    return resolved if _inside(resolved, cwd) else None


def _safe_file_edit(resource: str, cwd: Path) -> bool:
    _, _, raw = resource.partition(" ")
    if "\n" in raw or "\r" in raw:
        return False
    resolved = _resolve_in_cwd(raw, cwd)
    if resolved is None:
        return False
    relative_parts = resolved.relative_to(cwd).parts
    if any(part.lower() in _PROTECTED_DIR_NAMES for part in relative_parts):
        return False
    return not rules._SECRET_NAME_RE.search(resolved.name)


def _safe_read_only_command(command: str, cwd: Path) -> bool:
    if not command.strip() or _SHELL_META.search(command):
        return False
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return False
    lowered = [token.strip("\"'").lower() for token in tokens]
    if not any(
        tuple(lowered[: len(prefix)]) == prefix for prefix in _READ_ONLY_COMMANDS
    ):
        return False
    for token in tokens[1:]:
        bare = token.strip("\"'")
        if bare.startswith("-"):
            name = bare.lstrip("-").lower()
            if bare.lower() in _FORBIDDEN_SHORT_FLAGS or any(
                part in name for part in _FORBIDDEN_FLAG_PARTS
            ):
                return False
            continue
        path_part = bare.split("::", 1)[0]
        if path_part.startswith("~"):
            return False
        if any(mark in path_part for mark in "/\\:") or path_part in {".", ".."}:
            if _resolve_in_cwd(path_part, cwd) is None:
                return False
    return True


def _safe_permission(resource: str, cwd: object) -> bool:
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    if rules.check_destructive(resource).matched:
        return False
    cwd_path = Path(cwd).resolve(strict=False)
    if resource.startswith(("Write ", "Edit ")):
        return _safe_file_edit(resource, cwd_path)
    return _safe_read_only_command(resource, cwd_path)


def _approvable_by_orchestrator(request: Any, cwd: object = None) -> bool:
    kind = _legacy_kind(request)
    if kind not in _ORCHESTRATOR_APPROVABLE_KINDS:
        return False
    if kind == "permission_escalated":
        resource = getattr(request, "resource", None)
        if not isinstance(resource, str):
            return False
        return _safe_permission(resource, cwd)
    return True


async def list_pending_approvals(
    manager: Any, orchestrator_name: str
) -> list[dict[str, object]]:
    """Return bounded peer approval details and the code-enforced approve flag."""
    requests = manager.approval_broker.pending()
    result: list[dict[str, object]] = []
    for request in requests:
        session_id = getattr(request, "session_id", None)
        if not isinstance(session_id, str) or session_id == orchestrator_name:
            continue
        resource = getattr(request, "resource", "")
        reason = getattr(request, "reason", "")
        result.append(
            {
                "request_id": getattr(request, "request_id", ""),
                "session_id": session_id,
                "legacy_kind": _legacy_kind(request),
                "action": getattr(request, "action", ""),
                "resource": resource[:2000] if isinstance(resource, str) else "",
                "reason": reason[:800] if isinstance(reason, str) else "",
                "approvable_by_orchestrator": _approvable_by_orchestrator(
                    request, getattr(manager, "cwd", None)
                ),
            }
        )
    return result


async def respond_to_approval(
    manager: Any,
    orchestrator_name: str,
    session_id: str,
    request_id: str,
    action: str,
    message: str,
) -> str:
    """Apply the orchestrator approval allowlist, then respond through SessionManager."""
    if session_id == orchestrator_name:
        return "Error: orchestrator cannot respond to its own approval request."
    if action not in {"approve", "deny", "explain"}:
        return "Error: action must be approve, deny, or explain."
    if not isinstance(message, str) or len(message) > 500:
        return "Error: message must be at most 500 characters."
    request = next(
        (
            item
            for item in manager.approval_broker.pending(session_id)
            if getattr(item, "request_id", None) == request_id
        ),
        None,
    )
    if request is None:
        return "Error: approval request does not exist or is no longer pending."
    if action == "approve" and not _approvable_by_orchestrator(
        request, getattr(manager, "cwd", None)
    ):
        return "Error: this request requires user approval; it remains pending."
    session = manager.sessions.get(session_id)
    state = getattr(session, "state", None)
    append_log = getattr(state, "append_log", None)
    if not callable(append_log):
        return (
            "Error: target session audit log is unavailable; request remains pending."
        )
    if not manager.respond_approval(session_id, request_id, action, message):
        return "Error: approval request does not exist or is no longer pending."
    append_log(f"[orchestrator承認] {action} request_id={request_id} 理由: {message}")
    return f"Success: {action} response recorded for {request_id}."


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

    @tool(
        "list_pending_approvals",
        "List peer approval requests and whether this orchestrator may approve them.",
        {},
    )
    async def list_pending_approvals_tool(_args: dict[str, Any]) -> dict[str, Any]:
        requests = await list_pending_approvals(manager, orchestrator_name)
        return {
            "content": [
                {"type": "text", "text": json.dumps(requests, ensure_ascii=False)}
            ]
        }

    @tool(
        "respond_to_approval",
        "Respond to a peer approval request. Approvals are mechanically restricted.",
        {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "request_id": {"type": "string"},
                "action": {"type": "string", "enum": ["approve", "deny", "explain"]},
                "message": {"type": "string", "maxLength": 500},
            },
            "required": ["session_id", "request_id", "action", "message"],
        },
    )
    async def respond_to_approval_tool(args: dict[str, Any]) -> dict[str, Any]:
        result = await respond_to_approval(
            manager,
            orchestrator_name,
            args.get("session_id", ""),
            args.get("request_id", ""),
            args.get("action", ""),
            args.get("message", ""),
        )
        return {"content": [{"type": "text", "text": result}]}

    return create_sdk_mcp_server(
        "orchestration",
        tools=[
            list_sessions_tool,
            send_to_session_tool,
            read_session_output_tool,
            list_pending_approvals_tool,
            respond_to_approval_tool,
        ],
    )
