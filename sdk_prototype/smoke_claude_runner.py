"""
ClaudeRunner(agent_wrapper/runners/claude_runner.py)を実機のclaude backendに対して
起動し、start()/SharedState/approve()の配線が実際に機能することを確認するスモークテスト。

Phase 1のprobe.pyと違い、claude_agent_sdkを直接叩くのではなく、production相当の
ClaudeRunnerクラスをそのまま使う。人間の承認待ち(waiting_human)になった場合は、
このスクリプトが人間の代わりに自動でapprove()する(無人実行のため)。

実行方法: uv run python agent_wrapper/sdk_prototype/smoke_claude_runner.py
"""

import sys
import tempfile
import time

from agent_wrapper.runners.claude_runner import ClaudeRunner
from agent_wrapper.wrapper import SharedState


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    with tempfile.TemporaryDirectory(prefix="agent_wrapper_smoke_") as tmpdir:
        state = SharedState()
        runner = ClaudeRunner(
            prompt=(
                "echo agent_wrapper_smoke_test_ok というBashコマンドを実行して、"
                "その出力をそのまま報告してください。それ以外は何もしないでください。"
            ),
            cwd=tmpdir,
            state=state,
        )

        print(f"[INFO] tmpdir={tmpdir}")
        print("[INFO] runner.start()")
        runner.start()

        deadline = time.monotonic() + 90
        last_status = None
        while time.monotonic() < deadline:
            snapshot = state.snapshot()
            if snapshot["status"] != last_status:
                print(f"[STATUS] {snapshot}")
                last_status = snapshot["status"]
            if snapshot["status"] == "waiting_human":
                print("[INFO] waiting_human検知 -> 人間の代わりに自動approve()します")
                runner.approve()
            if snapshot["status"] == "stopped":
                break
            time.sleep(0.5)
        else:
            print("[WARN] タイムアウト(90秒)しました")

        print("[INFO] --- ログ全体 ---")
        for line in state.tail(200):
            print(line)


if __name__ == "__main__":
    main()
