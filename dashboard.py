"""
LAN内のブラウザから状態確認・ログ閲覧・承認操作ができる簡易ダッシュボード。
Mac / iPad / Android のブラウザから http://<WSL2のIP>:8765/ でアクセスする想定。
"""

from typing import Protocol

from flask import Flask, Response, jsonify, render_template_string, request

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
    --bg: #08111f;
    --panel: rgba(15, 23, 42, 0.92);
    --panel-soft: #172033;
    --line: rgba(148, 163, 184, 0.22);
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
    background:
      radial-gradient(circle at top, rgba(56, 189, 248, 0.18), transparent 28%),
      linear-gradient(180deg, #0f172a 0%, var(--bg) 100%);
    color: var(--text);
    padding: 0.8rem;
  }
  .wrap {
    max-width: 960px;
    margin: 0 auto;
    display: grid;
    gap: 0.85rem;
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 18px;
    padding: 1rem;
    box-shadow: 0 20px 60px rgba(2, 6, 23, 0.28);
  }
  .status {
    font-size: 1.2rem;
    font-weight: 700;
  }
  .status.waiting { color: var(--warn); }
  .status.running { color: var(--good); }
  .status.stopped { color: var(--muted); }
  .meta, .reason, .stop, .detail-meta {
    margin-top: 0.65rem;
    color: var(--muted);
    white-space: pre-wrap;
  }
  .reason strong, .stop strong, .detail-meta strong { color: var(--text); }
  .controls {
    display: grid;
    gap: 0.75rem;
  }
  textarea {
    width: 100%;
    min-height: 110px;
    resize: vertical;
    border-radius: 12px;
    border: 1px solid var(--line);
    background: #020617;
    color: var(--text);
    padding: 0.8rem;
    font: inherit;
  }
  .buttons {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.55rem;
  }
  button {
    border: none;
    border-radius: 999px;
    padding: 0.78rem 1rem;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
    color: #020617;
  }
  button[disabled] { opacity: 0.42; cursor: not-allowed; }
  .approve { background: var(--good); }
  .explain { background: var(--accent); }
  .deny { background: var(--bad); }
  .copy, .toggle {
    background: #cbd5e1;
  }
  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.6rem;
  }
  .section-head h3 {
    margin: 0;
    font-size: 1rem;
  }
  .subactions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .pending-box {
    border: 1px solid var(--line);
    border-radius: 14px;
    background: rgba(2, 6, 23, 0.72);
    overflow: hidden;
  }
  details summary {
    cursor: pointer;
    list-style: none;
    padding: 0.85rem 1rem;
    font-weight: 700;
  }
  details summary::-webkit-details-marker { display: none; }
  .detail-pre, .log-pre {
    margin: 0;
    padding: 1rem;
    background: #020617;
    color: var(--text);
    white-space: pre-wrap;
    user-select: text;
    font: 0.88rem/1.45 "Cascadia Code", Consolas, monospace;
  }
  .detail-pre {
    max-height: 32vh;
    overflow: auto;
    border-top: 1px solid #0f172a;
  }
  .log-pre {
    min-height: 44vh;
    max-height: 62vh;
    overflow: auto;
    border-radius: 12px;
    border: 1px solid #0f172a;
  }
  .toggles {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    flex-wrap: wrap;
    color: var(--muted);
  }
  .toggle-row {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }
  .qr-details {
    background: rgba(2, 6, 23, 0.35);
    border-radius: 14px;
  }
  .qr-body {
    padding: 0 1rem 1rem;
    display: grid;
    gap: 0.8rem;
    justify-items: center;
  }
  .qr-body img {
    width: min(100%, 240px);
    background: white;
    padding: 0.75rem;
    border-radius: 12px;
  }
  .url {
    color: var(--accent);
    word-break: break-all;
    font-size: 0.92rem;
    text-align: center;
  }
  @media (min-width: 760px) {
    body { padding: 1rem; }
    .buttons { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
</style>
</head>
<body>
  <div class="wrap">
    <section class="panel">
      <div class="status" id="status">読み込み中...</div>
      <div class="meta" id="summary"></div>
      <div class="reason" id="reason"></div>
      <div class="stop" id="stop"></div>
      <details class="qr-details">
        <summary>QRコードを表示</summary>
        <div class="qr-body">
          <img id="qr" alt="ダッシュボードQRコード">
          <div class="url" id="dashboard-url"></div>
        </div>
      </details>
    </section>

    <section class="panel controls">
      <div class="section-head">
        <h3>承認操作</h3>
        <div class="toggles">
          <label class="toggle-row" for="sound-toggle">
            <input type="checkbox" id="sound-toggle">
            通知音
          </label>
        </div>
      </div>
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
      <div class="section-head">
        <h3>承認待ちの判断材料</h3>
      </div>
      <div class="detail-meta" id="detail-meta">承認待ちはありません</div>
      <div class="pending-box">
        <details id="detail-box">
          <summary id="detail-summary">対象全文</summary>
          <pre class="detail-pre" id="detail"></pre>
        </details>
      </div>
    </section>

    <section class="panel">
      <div class="section-head">
        <h3>ログ</h3>
        <div class="subactions">
          <button class="copy" onclick="copyLog()">全文コピー</button>
        </div>
      </div>
      <pre class="log-pre" id="log"></pre>
    </section>
  </div>

<script>
let autoScroll = true;
let currentRequestId = null;
let previousStatus = null;
let previousRequestId = null;
let audioContext = null;
const logEl = document.getElementById('log');
const detailBox = document.getElementById('detail-box');
const soundToggle = document.getElementById('sound-toggle');
const SOUND_KEY = 'agent_wrapper_sound_enabled';

function loadSoundPreference() {
  const saved = localStorage.getItem(SOUND_KEY);
  soundToggle.checked = saved !== '0';
}

function saveSoundPreference() {
  localStorage.setItem(SOUND_KEY, soundToggle.checked ? '1' : '0');
}

function ensureAudioContext() {
  if (!audioContext) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (AudioCtx) {
      audioContext = new AudioCtx();
    }
  }
  if (audioContext && audioContext.state === 'suspended') {
    audioContext.resume().catch(() => {});
  }
  return audioContext;
}

function primeAudio() {
  ensureAudioContext();
}

function playNotificationSound() {
  if (!soundToggle.checked) {
    return;
  }
  const ctx = ensureAudioContext();
  if (!ctx) {
    return;
  }
  const now = ctx.currentTime;
  const oscillator = ctx.createOscillator();
  const gain = ctx.createGain();
  oscillator.type = 'sine';
  oscillator.frequency.setValueAtTime(880, now);
  oscillator.frequency.exponentialRampToValueAtTime(660, now + 0.18);
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.05, now + 0.01);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.24);
  oscillator.connect(gain);
  gain.connect(ctx.destination);
  oscillator.start(now);
  oscillator.stop(now + 0.25);
}

logEl.addEventListener('scroll', () => {
  autoScroll = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 20;
});
soundToggle.addEventListener('change', saveSoundPreference);
document.addEventListener('pointerdown', primeAudio, { once: true });
document.addEventListener('keydown', primeAudio, { once: true });

function setButtonsEnabled(enabled) {
  for (const id of ['approve-btn', 'approve-msg-btn', 'explain-btn', 'deny-btn']) {
    document.getElementById(id).disabled = !enabled;
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
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
    reasonHtml = '<strong>待機理由:</strong> ' + escapeHtml(s.pending_kind || '-') + ' / ' + escapeHtml(s.pending_reason).replace(/\n/g, '<br>');
    if ((s.pending_count || 0) > 1) {
      reasonHtml += '<br><strong>キュー:</strong> 他 ' + (s.pending_count - 1) + ' 件の承認待ちがあります';
    }
  }
  document.getElementById('reason').innerHTML = reasonHtml;
  document.getElementById('stop').innerHTML = s.status === 'stopped'
    ? ('<strong>終了理由:</strong> ' + escapeHtml(s.stop_reason_label || s.stop_reason || '-') + (s.stop_reason_detail ? ' / ' + escapeHtml(s.stop_reason_detail) : ''))
    : '';
  document.getElementById('qr').src = s.qr_code_data_url || '';
  document.getElementById('dashboard-url').textContent = s.dashboard_url || '';
  setButtonsEnabled(s.status === 'waiting_human');

  const waiting = s.status === 'waiting_human' && !!s.pending_reason;
  document.getElementById('detail-meta').innerHTML = waiting
    ? ('<strong>現在の承認待ち:</strong> ' + escapeHtml(s.pending_kind || '-') + ' / request #' + (s.pending_request_id ?? '-'))
    : '承認待ちはありません';
  document.getElementById('detail').textContent = s.pending_detail || '';
  detailBox.open = waiting;
  document.getElementById('detail-summary').textContent = waiting ? '対象全文を表示' : '対象全文';

  if (
    waiting &&
    soundToggle.checked &&
    (previousStatus !== 'waiting_human' || previousRequestId !== currentRequestId)
  ) {
    playNotificationSound();
  }
  previousStatus = s.status;
  previousRequestId = currentRequestId;

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

loadSoundPreference();
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
