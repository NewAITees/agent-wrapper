"""
claude-agent-sdkのツール呼び出し(tool_name/tool_input)を、rules.check_destructive()や
ollama_client側の判定関数に渡すための文字列表現に変換する。
"""

GATED_TOOLS: frozenset[str] = frozenset({"Bash", "Write", "Edit"})


def describe_tool_call(tool_name: str, tool_input: dict) -> str:
    """rules.check_destructive/ollama_clientに渡すための文字列表現を組み立てる。"""
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))
    if tool_name in ("Write", "Edit"):
        return f"{tool_name} {tool_input.get('file_path', '')}"
    if tool_name == "mcp__orchestration__send_to_session":
        return (
            "セッションへの指示送信: "
            f"宛先={tool_input.get('name', '')}\n内容:\n{tool_input.get('text', '')}"
        )
    if tool_name == "mcp__orchestration__read_session_output":
        return (
            "セッション出力の読み取り: "
            f"対象={tool_input.get('name', '')}, 末尾={tool_input.get('tail', '')}文字"
        )
    if tool_name == "mcp__orchestration__list_sessions":
        return "セッション一覧の読み取り"
    if tool_name == "mcp__orchestration__list_pending_approvals":
        return "他セッションの承認待ち一覧の読み取り"
    if tool_name == "mcp__orchestration__respond_to_approval":
        return (
            "他セッションの承認要求への応答: "
            f"対象セッション={tool_input.get('session_id', '')}, "
            f"request_id={tool_input.get('request_id', '')}, "
            f"操作={tool_input.get('action', '')}, "
            f"理由・説明={tool_input.get('message', '')}"
        )
    return f"{tool_name}: {tool_input}"
