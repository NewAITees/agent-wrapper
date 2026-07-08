"""
LAN内のブラウザから状態確認・ログ閲覧・承認操作ができる簡易ダッシュボード。
Mac / iPad / Android のブラウザから http://<WSL2のIP>:8765/ でアクセスする想定。
"""

from typing import Protocol

from flask import Flask, Response, jsonify, request, render_template_string

from .wrapper import SharedState


class Approvable(Protocol):
    """AgentWrapper/ClaudeRunnerが共通で持つ、ダッシュボードから呼ばれる操作。"""

    def approve(self) -> None: ...
    def deny(self) -> None: ...


PAGE = """
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Wrapper Dashboard</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #1e1e2e; color: #cdd6f4; padding: 1rem; }
  .status { font-size: 1.4rem; margin-bottom: 0.5rem; }
  .waiting { color: #f38ba8; }
  .running { color: #a6e3a1; }
  .stopped { color: #6c7086; }
  #log { background: #11111b; padding: 0.8rem; border-radius: 8px; height: 50vh; overflow-y: auto; white-space: pre-wrap; font-size: 0.85rem; }
  button { padding: 0.6rem 1.2rem; margin-right: 0.5rem; border-radius: 6px; border: none; font-size: 1rem; }
  .approve { background: #a6e3a1; }
  .deny { background: #f38ba8; }
  .reason { margin: 0.5rem 0; }
</style>
</head>
<body>
  <div class="status" id="status">読み込み中...</div>
  <div class="reason" id="reason"></div>
  <div>
    <button class="approve" onclick="act('approve')">承認</button>
    <button class="deny" onclick="act('deny')">却下</button>
  </div>
  <h3>直近ログ</h3>
  <div id="log"></div>

<script>
async function refresh() {
  const s = await fetch('/status').then(r => r.json());
  const el = document.getElementById('status');
  el.textContent = '状態: ' + s.status + (s.last_summary ? ' / 直近要約: ' + s.last_summary : '');
  el.className = 'status ' + (s.status === 'waiting_human' ? 'waiting' : (s.status === 'running' ? 'running' : 'stopped'));
  document.getElementById('reason').textContent = s.pending_reason ? ('待機理由: ' + s.pending_kind + ' - ' + s.pending_reason) : '';

  const log = await fetch('/log?n=200').then(r => r.json());
  const logEl = document.getElementById('log');
  logEl.textContent = log.lines.join('\\n');
  logEl.scrollTop = logEl.scrollHeight;
}
async function act(kind) {
  await fetch('/' + kind, { method: 'POST' });
  refresh();
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


def make_app(agent_wrapper: Approvable, state: SharedState) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return render_template_string(PAGE)

    @app.route("/status")
    def status() -> Response:
        return jsonify(state.snapshot())

    @app.route("/log")
    def log() -> Response:
        n = int(request.args.get("n", 100))
        return jsonify({"lines": state.tail(n)})

    @app.route("/approve", methods=["POST"])
    def approve() -> Response:
        agent_wrapper.approve()
        return jsonify({"ok": True})

    @app.route("/deny", methods=["POST"])
    def deny() -> Response:
        agent_wrapper.deny()
        return jsonify({"ok": True})

    return app
