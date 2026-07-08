"""
エントリーポイント。

使い方:
    agent-wrapper --agent claude   # uv tool install -e . 済みならどのディレクトリからでも
    uv run python -m agent_wrapper.main --agent mock   # リポジトリルートから直接動かす場合

ダッシュボードは http://0.0.0.0:28765/ で起動する。起動時にLAN側のURLとQRコードを
ターミナルに表示するので、スマホ/MacはそれをアクセスするかQRコードを読み取ればよい。
LAN外からのアクセスにはWindows Defenderファイアウォールでポートを開ける必要がある
(詳細はREADME.md参照)。
"""

import argparse
import os
import sys

from . import notifier, qr_display
from .dashboard import make_app
from .runners.claude_runner import ClaudeRunner
from .wrapper import AgentWrapper, SharedState

AGENT_COMMANDS = {
    # 実際の環境に合わせて調整する。ヘッドレスモードで動かす想定。
    # "claude" は ClaudeRunner(claude-agent-sdk経由)を使うため、ここには含めない。
    "codex": ["codex", "exec"],
    "mock": [sys.executable, "-m", "agent_wrapper.mock_agent"],
}

AGENT_CHOICES = ["claude", *AGENT_COMMANDS.keys()]

DEFAULT_PORT = 28765


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Windowsのcp932コンソールでも日本語/QRのブロック文字を出せるようにする

    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=AGENT_CHOICES, default="mock")
    parser.add_argument("--ollama-model", default="gemma4:e4b")
    parser.add_argument(
        "--checkin-interval", type=int, default=600, help="定期チェックインの間隔(秒)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--ntfy-topic",
        default=os.environ.get("AGENT_WRAPPER_NTFY_TOPIC"),
        help="ntfy.sh のトピック名(省略時は環境変数 AGENT_WRAPPER_NTFY_TOPIC を使う。両方未指定ならプッシュ通知は行わない)",
    )
    parser.add_argument(
        "--prompt", help="--agent claude 使用時に必須。エージェントへの初期指示。"
    )
    parser.add_argument(
        "--prompt-file",
        help="--agent claude 使用時、--prompt の代わりにファイルから読む。",
    )
    args = parser.parse_args()

    if args.agent == "claude" and not (args.prompt or args.prompt_file):
        parser.error("--agent claude を使う場合は --prompt か --prompt-file が必須です")

    state = SharedState()
    notify = notifier.make_notifier(ntfy_topic=args.ntfy_topic)

    agent: AgentWrapper | ClaudeRunner
    if args.agent == "claude":
        prompt = args.prompt
        if args.prompt_file:
            with open(args.prompt_file, encoding="utf-8") as f:
                prompt = f.read()
        agent = ClaudeRunner(
            prompt=prompt,
            cwd=os.getcwd(),
            state=state,
            ollama_model=args.ollama_model,
            notifier=notify,
        )
    else:
        agent = AgentWrapper(
            cmd=AGENT_COMMANDS[args.agent],
            state=state,
            ollama_model=args.ollama_model,
            checkin_interval_sec=args.checkin_interval,
            notifier=notify,
        )
    agent.start()

    app = make_app(agent, state)

    lan_url = f"http://{qr_display.get_lan_ip()}:{args.port}/"
    print(f"ダッシュボード(このPC上): http://localhost:{args.port}/")
    qr_display.print_dashboard_qr(lan_url)
    if not args.ntfy_topic:
        print("(--ntfy-topic 未指定のため、プッシュ通知は行われません)", flush=True)

    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
