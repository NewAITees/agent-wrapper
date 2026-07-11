"""
LAN内のブラウザから状態確認・ログ閲覧・承認操作ができる簡易ダッシュボード。
Mac / iPad / Android のブラウザから http://<WSL2のIP>:8765/ でアクセスする想定。
"""

from typing import Protocol

from flask import Flask, Response, jsonify, request, render_template_string

from .wrapper import HumanAction, SharedState


class Approvable(Protocol):
    """AgentWrapper/runnerが共通で持つ、ダッシュボードから呼ばれる操作。"""

    def approve(self) -> None: ...
    def deny(self) -> None: ...
    def respond(
        self,
        action: HumanAction,
        message: str = "",
        request_id: int | None = None,
    ) -> None: ...


PAGE = r"""
<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Wrapper Dashboard</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0f172a;
    --panel: #111827;
    --panel-soft: #1f2937;
    --line: #334155;
    --text: #e5eefb;
    --muted: #94a3b8;
    --good: #34d399;
    --warn: #f59e0b;
    --bad: #fb7185;
    --accent: #38bdf8;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: radial-gradient(circle at top, #1e293b 0%, var(--bg) 55%);
    color: var(--text);
    padding: 1rem;
  }
  .wrap { max-width: 1100px; margin: 0 auto; display: grid; gap: 1rem; }
  .panel {
    background: rgba(17, 24, 39, 0.92);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 16px;
    padding: 1rem;
    box-shadow: 0 20px 60px rgba(15, 23, 42, 0.35);
  }
  .top { display: grid; grid-template-columns: 1.6fr 1fr; gap: 1rem; }
  .status { font-size: 1.25rem; font-weight: 700; }
  .status.waiting { color: var(--warn); }
  .status.running { color: var(--good); }
  .status.stopped { color: var(--muted); }
  .meta, .reason, .stop { margin-top: 0.6rem; color: var(--muted); white-space: pre-wrap; }
  .stop strong, .reason strong { color: var(--text); }
  .qr-card { display: grid; place-items: center; gap: 0.75rem; }
  .qr-card img { width: min(100%, 260px); background: white; padding: 0.75rem; border-radius: 12px; }
  .url { font-size: 0.9rem; color: var(--accent); word-break: break-all; }
  .controls { display: grid; gap: 0.75rem; }
  textarea {
    width: 100%; min-height: 110px; resize: vertical; border-radius: 12px;
    border: 1px solid var(--line); background: #020617; color: var(--text);
    padding: 0.8rem; font: inherit;
  }
  .buttons { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  button {
    border: none; border-radius: 999px; padding: 0.7rem 1rem; font: inherit; font-weight: 700;
    cursor: pointer; color: #020617;
  }
  button[disabled] { opacity: 0.4; cursor: not-allowed; }
  .approve { background: var(--good); }
  .explain { background: var(--accent); }
  .deny { background: var(--bad); }
  .copy { background: #cbd5e1; }
  .log-head { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
  pre {
    margin: 0; padding: 1rem; background: #020617; border: 1px solid #0f172a; border-radius: 12px;
    min-height: 50vh; max-height: 60vh; overflow: auto; white-space: pre-wrap; user-select: text;
    font: 0.88rem/1.45 "Cascadia Code", Consolas, monospace;
  }
  @media (max-width: 840px) {
    .top { grid-template-columns: 1fr; }
    .buttons button { flex: 1 1 100%; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <section class="panel">
        <div class="status" id="status">読み込み中...</div>
        <div class="meta" id="summary"></div>
        <div class="reason" id="reason"></div>
        <div class="stop" id="stop"></div>
      </section>
      <section class="panel qr-card">
        <img id="qr" alt="ダッシュボードQRコード">
        <div class="url" id="dashboard-url"></div>
      </section>
    </div>

    <section class="panel controls">
      <label for="message">エージェントへのメッセージ</label>
      <textarea id="message" placeholder="必要な補足や質問を書いて送れます"></textarea>
      <div class="buttons">
        <button class="approve" id="approve-btn" onclick="act('approve')">承認</button>
        <button class="approve" id="approve-msg-btn" onclick="act('approve', true)">承認+メッセージ</button>
        <button class="explain" id="explain-btn" onclick="act('explain', true)">わからないから説明して</button>
        <button class="deny" id="deny-btn" onclick="act('deny', true)">却下</button>
      </div>
    </section>

    <section class="panel">
      <div class="log-head">
        <h3>ログ</h3>
        <button class="copy" onclick="copyLog()">全文コピー</button>
      </div>
      <pre id="log"></pre>
    </section>
  </div>

<script>
let autoScroll = true;
let currentRequestId = null;
const logEl = document.getElementById('log');
logEl.addEventListener('scroll', () => {
  autoScroll = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
});

function setButtonsEnabled(enabled) {
  for (const id of ['approve-btn', 'approve-msg-btn', 'explain-btn', 'deny-btn']) {
    document.getElementById(id).disabled = !enabled;
  }
}

async function refresh() {
  const s = await fetch('/status').then(r => r.json());
  currentRequestId = s.pending_request_id ?? null;
  const statusEl = document.getElementById('status');
  statusEl.textContent = '状態: ' + s.status;
  statusEl.className = 'status ' + (s.status === 'waiting_human' ? 'waiting' : (s.status === 'running' ? 'running' : 'stopped'));

  document.getElementById('summary').textContent = s.last_summary ? ('直近要約 (' + (s.last_summary_time || '-') + '): ' + s.last_summary) : '';

  let reasonHtml = '';
  if (s.pending_reason) {
    reasonHtml = '<strong>待機理由:</strong> ' + (s.pending_kind || '-') + ' / ' + s.pending_reason.replace(/\n/g, '<br>');
    if ((s.pending_count || 0) > 1) {
      reasonHtml += '<br><strong>キュー:</strong> 他 ' + (s.pending_count - 1) + ' 件の承認待ちがあります';
    }
  }
  document.getElementById('reason').innerHTML = reasonHtml;
  document.getElementById('stop').innerHTML = s.status === 'stopped'
    ? ('<strong>終了理由:</strong> ' + (s.stop_reason_label || s.stop_reason || '-') + (s.stop_reason_detail ? ' / ' + s.stop_reason_detail : ''))
    : '';
  document.getElementById('qr').src = s.qr_code_data_url || '';
  document.getElementById('dashboard-url').textContent = s.dashboard_url || '';
  setButtonsEnabled(s.status === 'waiting_human');

  const log = await fetch('/log?n=400').then(r => r.json());
  logEl.textContent = log.lines.join('\n');
  if (autoScroll) {
    logEl.scrollTop = logEl.scrollHeight;
  }
}

async function act(action, withMessage = false) {
  const message = withMessage ? document.getElementById('message').value : '';
  await fetch('/respond', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, message, request_id: currentRequestId })
  });
  if (action !== 'explain') {
    document.getElementById('message').value = '';
  }
  await refresh();
}

async function copyLog() {
  const text = await fetch('/log_text').then(r => r.text());
  await navigator.clipboard.writeText(text);
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

    @app.route("/log_text")
    def log_text() -> Response:
        return Response(state.full_log_text(), mimetype="text/plain; charset=utf-8")

    @app.route("/respond", methods=["POST"])
    def respond() -> Response:
        payload = request.get_json(silent=True) or {}
        raw_action = str(payload.get("action", "approve"))
        if raw_action == "explain":
            action: HumanAction = "explain"
        elif raw_action == "deny":
            action = "deny"
        else:
            action = "approve"
        message = str(payload.get("message", ""))
        raw_request_id = payload.get("request_id")
        request_id = raw_request_id if isinstance(raw_request_id, int) else None
        agent_wrapper.respond(action, message, request_id=request_id)
        return jsonify({"ok": True})

    @app.route("/approve", methods=["POST"])
    def approve() -> Response:
        agent_wrapper.approve()
        return jsonify({"ok": True})

    @app.route("/deny", methods=["POST"])
    def deny() -> Response:
        agent_wrapper.deny()
        return jsonify({"ok": True})

    return app
