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
    return f"{tool_name}: {tool_input}"
