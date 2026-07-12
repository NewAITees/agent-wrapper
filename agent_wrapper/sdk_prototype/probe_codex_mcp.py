"""
codex mcp-server に MCPクライアントとして接続し、以下を検証する(Phase 3)。

検証の合格基準(ユーザーと合意済み):
1. MCP経由でも codex がファイルを書き、シェルコマンドを実行できること
   (=ターミナルを持ったフルエージェントであること)← 最重要
2. その過程で承認要求(elicitation)がクライアント側に構造化データとして届くこと
   (=そこに rules/ollama/人間承認 を差し込めること)

実行方法:
  uv run python agent_wrapper/sdk_prototype/probe_codex_mcp.py tools   # ツール一覧のみ(モデル呼び出しなし)
  uv run python agent_wrapper/sdk_prototype/probe_codex_mcp.py run     # 実タスク実行(ChatGPTサブスク利用)
"""

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import mcp.types as t
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ELICITATIONS: list[str] = []


def _codex_cmd() -> str:
    path = shutil.which("codex")
    if not path:
        raise RuntimeError("codex CLI が見つかりません")
    return path


async def elicitation_callback(
    context: Any,
    params: t.ElicitRequestParams,
) -> t.ElicitResult:
    entry = params.model_dump_json()
    ELICITATIONS.append(entry)
    print(f"[ELICITATION] {entry[:300]}")
    message = getattr(params, "message", "") or ""
    # codexのmcp-serverはMCP標準のaction/contentではなく、トップレベルの
    # {"decision": "approved"|"denied"} を読む(codex-rs/mcp-server/src/exec_approval.rs
    # のExecApprovalResponse参照。パース失敗時は保守的にdenied扱い)。
    # ElicitResultはextra="allow"なので、decisionを追加フィールドとして同梱する。
    if "PROBE_DENY_ME" in message:
        print("[ELICITATION] -> denied")
        return t.ElicitResult.model_validate(
            {"action": "decline", "decision": "denied"}
        )
    print("[ELICITATION] -> approved")
    return t.ElicitResult.model_validate({"action": "accept", "decision": "approved"})


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    mode = sys.argv[1] if len(sys.argv) > 1 else "tools"

    server = StdioServerParameters(
        command=_codex_cmd(),
        args=["mcp-server", "-c", 'approval_policy="untrusted"'],
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(
            read, write, elicitation_callback=elicitation_callback
        ) as session:
            init = await session.initialize()
            print(f"[INIT] server={init.serverInfo.name} v{init.serverInfo.version}")

            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"[TOOL] {tool.name}")
                print(f"  desc: {(tool.description or '')[:120]}")
                print(
                    f"  schema: {json.dumps(tool.inputSchema, ensure_ascii=False)[:600]}"
                )

            if mode != "run":
                return

            with tempfile.TemporaryDirectory(
                prefix="codex_mcp_probe_", dir="C:\\tmp"
            ) as tmpdir:
                print(f"[INFO] tmpdir={tmpdir}")
                prompt = (
                    "y(承認済み・確認不要) 次を順に実行してください。"
                    "1) probe_hello.txt というファイルに hello_from_codex と書き込む "
                    "2) シェルコマンドでそのファイルの内容を表示する "
                    "3) mkdir probe_dir を実行する "
                    "最後に結果を一行で報告してください。"
                )
                result = await session.call_tool(
                    "codex",
                    arguments={
                        "prompt": prompt,
                        "cwd": tmpdir,
                        "approval-policy": "untrusted",
                        "sandbox": "workspace-write",
                    },
                )
                print("[RESULT]")
                for block in result.content:
                    if isinstance(block, t.TextContent):
                        print(block.text[:2000])
                    else:
                        print(type(block).__name__, str(block)[:500])

                created = Path(tmpdir) / "probe_hello.txt"
                print(f"[CHECK] probe_hello.txt exists: {created.exists()}")
                if created.exists():
                    print(
                        f"[CHECK] content: {created.read_text(encoding='utf-8').strip()}"
                    )
                print(
                    f"[CHECK] probe_dir exists: {(Path(tmpdir) / 'probe_dir').exists()}"
                )

    print(f"[SUMMARY] elicitation回数: {len(ELICITATIONS)}")


if __name__ == "__main__":
    asyncio.run(main())
