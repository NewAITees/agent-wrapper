"""
CodexRunner(agent_wrapper/runners/codex_runner.py)を実機のcodexバックエンドに対して
起動し、start()/SharedState/approve()の配線が実際に機能することを確認するスモークテスト。

smoke_claude_runner.pyのcodex版。waiting_humanになった場合は人間の代わりに
自動でapprove()する(無人実行のため)。

実行方法: uv run python agent_wrapper/sdk_prototype/smoke_codex_runner.py
"""

import sys
import tempfile
import time

from agent_wrapper.runners.codex_runner import CodexRunner
from agent_wrapper.wrapper import SharedState


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    with tempfile.TemporaryDirectory(
        prefix="agent_wrapper_codex_smoke_", dir="C:\\tmp"
    ) as tmpdir:
        state = SharedState()
        runner = CodexRunner(
            prompt=(
                "y(承認済み・確認不要) smoke_ok.txt というファイルに codex_smoke_test_ok "
                "と書き込み、mkdir smoke_dir を実行して、結果を一行で報告してください。"
            ),
            cwd=tmpdir,
            state=state,
        )

        print(f"[INFO] tmpdir={tmpdir}")
        print("[INFO] runner.start()")
        runner.start()

        deadline = time.monotonic() + 180
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
            print("[WARN] タイムアウト(180秒)しました")

        print("[INFO] --- ログ全体 ---")
        for line in state.tail(200):
            print(line)

        from pathlib import Path

        created = Path(tmpdir) / "smoke_ok.txt"
        print(f"[CHECK] smoke_ok.txt exists: {created.exists()}")
        if created.exists():
            print(f"[CHECK] content: {created.read_text(encoding='utf-8').strip()}")
        print(f"[CHECK] smoke_dir exists: {(Path(tmpdir) / 'smoke_dir').exists()}")


if __name__ == "__main__":
    main()
