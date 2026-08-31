"""
複数のharness(claude/codex/opencode)をConPTY越しにWeb UIへ並列表示するサーバー。

関連: docs/agent_server_architecture_options.md(Option B, 自前ConPTY+WebSocket方式の
採用経緯), tasks/alignment.md(harness/wrapped/native-bg/raw-pty等の用語定義)。
`agent_wrapper.wrapper`/`agent_wrapper.runners`(SDK/MCPフックでollama一次判定+強制人間承認
が効く"wrapped"方式)とは別系統で、こちらは各harnessを素の対話モードのままWebに中継する
"raw-pty"方式。承認はCLI自身の対話プロンプトに人間が直接答える。

画面フロー:
1. 起動直後はセットアップ画面のみ(まだ何も起動しない)
2. 作業フォルダをネイティブのWindowsフォルダ選択ダイアログで選ぶ(テキスト入力不可)。
   過去に使ったフォルダは`~/.agent-wrapper/recent_folders.json`に記録され、
   `/recent-folders`から一覧を取得できる(2026-09-01追加)。
3. ターミナル行(名前・harness・source・model)を可変個数で設定する
   (モデルはその場でCLIから取得した候補から選択する。自由入力はしない)
4. 「起動」でまとめてセッション生成

セッション(ptyプロセス)は/startで指定された分だけ生成し、複数の閲覧者(ブラウザの
WebSocket接続・/sendへのHTTP POST)が同じセッションに相乗りする(2026-08-31、
接続ごとに新規pty生成していた初期実装で複数ブラウザから見え方が食い違うバグが
発覚し、この共有方式に修正した)。

起動:
    uv run python -m agent_wrapper.server
    または(uv tool install -e . 後):
    agent-wrapper-server
"""

import asyncio
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import tkinter
import webbrowser
from pathlib import Path
from tkinter import filedialog
from typing import Any

import requests
import websockets
import winpty
from websockets.asyncio.server import serve

from .approval import ApprovalBroker, ApprovalDecision
from .wrapper import SharedState

HTTP_PORT = 8765
WS_PORT = 8766
OLLAMA_BASE_URL = "http://localhost:11434"

_CONFIG_DIR = Path.home() / ".agent-wrapper"
_RECENT_FOLDERS_PATH = _CONFIG_DIR / "recent_folders.json"
_MAX_RECENT_FOLDERS = 10

# claude/codexはCLIからモデル一覧を取得する手段が無いため、代表的な候補を決め打ちする。
# opencodeは`opencode models`の実出力からsource(プロバイダ)ごとに動的に組み立てる。
_STATIC_MODELS: dict[str, dict[str, list[str]]] = {
    "claude": {"anthropic": ["sonnet", "opus", "haiku", "fable"]},
    "codex": {"openai": ["gpt-5.2", "gpt-5.2-codex"]},
}

_STATIC_PAGE_PATH = Path(__file__).parent / "server_static" / "index.html"

# ターミナルの名前(role)に応じて、既定のシステムプロンプトを完全に置き換える形で
# 差し込む(2026-09-01、ユーザー指示: 「名称に沿ったシステムプロンプトを挿入し、
# 既存のシステムプロンプトは挿入しない」)。claude/opencodeは置き換え手段が実在するが、
# codexはCLIに置き換え手段が見当たらないため対象外(tasks/todo.md参照)。
ROLE_SYSTEM_PROMPTS: dict[str, str] = {
    "orchestrator": (
        "あなたはこのマルチセッション作業におけるorchestratorです。"
        "各セッション(planner/worker/reviewer等)からの許可依頼・質問を一次的に受け止め、"
        "内容を人間が判断しやすい形に整理してください。"
        "あなた自身が最終的な実行許可を与えることはありません。"
        "また、各セッションへの作業の割り振り・進行管理を担当してください。"
    ),
    "planner": (
        "あなたはこのマルチセッション作業におけるplannerです。"
        "要求を分析し、実装に入る前に具体的な計画(目的・方針・変更範囲・影響・検証方法)を"
        "まとめることに専念してください。自分でコードを実装することはせず、"
        "計画をworker等の他セッションに引き継ぐ前提で作業してください。"
    ),
    "worker": (
        "あなたはこのマルチセッション作業におけるworkerです。"
        "与えられた計画・指示に従って実装作業を行うことに専念してください。"
        "計画そのものの妥当性に大きな疑問がある場合は、実装を進める前にその旨を明確に述べてください。"
    ),
    "reviewer": (
        "あなたはこのマルチセッション作業におけるreviewerです。"
        "他セッションが行った実装の正しさ・簡潔さ・安全性をレビューすることに専念してください。"
        "自分で新規に大きな実装を行うのではなく、指摘事項を具体的に述べてください。"
    ),
    "tester": (
        "あなたはこのマルチセッション作業におけるtesterです。"
        "実装に対するテストの作成・実行・結果の報告に専念してください。"
    ),
    "utility": (
        "あなたはこのマルチセッション作業におけるutilityです。"
        "他セッションからの軽量な下請け作業(要約、内容の一次判定、簡単な確認作業など)を"
        "担当してください。大きな設計判断や最終的な許可判断は行わず、"
        "材料を整理して返すことに専念してください。"
    ),
}


def system_prompt_for(name: str) -> str:
    """ターミナルの名前(role)に対応するシステムプロンプトを返す。

    既知の役割名(ROLE_SYSTEM_PROMPTS)以外の任意の名前にも、その名前を役割として
    扱う汎用プロンプトを生成する(常に何らかのカスタムプロンプトを持たせる設計)。
    """
    preset = ROLE_SYSTEM_PROMPTS.get(name.strip().lower())
    if preset:
        return preset
    return (
        f"あなたはこのマルチセッション作業における役割「{name}」を担当します。"
        "他のセッションと連携しながら、この役割に期待される作業に集中してください。"
    )


_OPENCODE_AGENT_DIR = Path.home() / ".config" / "opencode" / "agents"

# ターミナル名(role)はUI入力値であり、opencode用agentファイル名(f"{name}.md")に
# そのまま使われる。検証しないと"../../evil"のようなパストラバーサル名で
# ~/.config/opencode/agents 外への書き込み・既存ファイルの上書きが可能になってしまう
# (2026-09-01、codexレビューで指摘)。
_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_role_name(name: str) -> None:
    if not _ROLE_NAME_RE.match(name):
        raise ValueError(
            f"invalid terminal name: {name!r} "
            "(英数字・ハイフン・アンダースコアのみ使用できます)"
        )


def opencode_agent_markdown(name: str, prompt: str) -> str:
    """指定した役割名・プロンプトから、opencodeのMarkdown agent定義を組み立てる。"""
    return f"---\ndescription: {name} role (agent-wrapper)\nmode: primary\n---\n\n{prompt}\n"


def write_opencode_agent_file(
    name: str, prompt: str, agent_dir: Path | None = None
) -> Path:
    """opencodeの`--agent <name>`で選択できるよう、役割専用のagent定義を書き出す。

    opencodeにはシステムプロンプトを生CLIフラグで置き換える手段が無く、
    agent定義(prompt本文がそのままシステムプロンプトになる)を経由する必要がある。
    """
    validate_role_name(name)
    agent_dir = agent_dir or _OPENCODE_AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / f"{name}.md"
    path.write_text(opencode_agent_markdown(name, prompt), encoding="utf-8")
    return path


def add_recent_folder(
    folders: list[str], path: str, limit: int = _MAX_RECENT_FOLDERS
) -> list[str]:
    """`path`を先頭に移動(無ければ追加)し、最大`limit`件に切り詰める。"""
    return [path, *(f for f in folders if f != path)][:limit]


def load_recent_folders(path: Path = _RECENT_FOLDERS_PATH) -> list[str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def remember_folder(cwd: str, path: Path = _RECENT_FOLDERS_PATH) -> list[str]:
    """作業フォルダとして使われたパスを履歴に記録する(2026-08-31、ユーザー指示で追加)。"""
    folders = add_recent_folder(load_recent_folders(path), cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(folders, ensure_ascii=False, indent=2), encoding="utf-8")
    return folders


def resolve_opencode_exe() -> str:
    """opencode CLIの実exeを解決する。

    npmのWindows向けグローバルシム(opencode.cmd/opencode.ps1)は`/bin/sh`経由の
    起動を前提にしており、このマシンでは壊れて動かないことを2026-08-31に確認済み
    (詳細: docs/agent_server_architecture_options.md)。そのため`npm root -g`配下の
    実体exeを直接解決する。環境変数AGENT_WRAPPER_OPENCODE_EXEで上書き可能。
    """
    override = os.environ.get("AGENT_WRAPPER_OPENCODE_EXE")
    if override:
        return override
    npm_path = shutil.which("npm.cmd") or shutil.which("npm")
    if npm_path is None:
        raise RuntimeError(
            "npmコマンドが見つかりません。"
            "環境変数AGENT_WRAPPER_OPENCODE_EXEで直接パスを指定してください。"
        )
    try:
        # npmはWindowsでは"npm.cmd"(バッチファイル)であり、CreateProcessはバッチ
        # ファイルを直接起動できないため、cmd.exe /c 経由で呼ぶ必要がある
        # (shutil.whichでフルパスまで解決済みなので引数展開の余地は無い)。
        npm_root = subprocess.run(
            ["cmd", "/c", npm_path, "root", "-g"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(
            "opencode-aiの実exeを自動解決できませんでした(`npm root -g`失敗)。"
            "環境変数AGENT_WRAPPER_OPENCODE_EXEで直接パスを指定してください。"
        ) from e
    candidate = (
        Path(npm_root)
        / "opencode-ai"
        / "node_modules"
        / "opencode-windows-x64"
        / "bin"
        / "opencode.exe"
    )
    if not candidate.exists():
        raise RuntimeError(
            f"opencode-aiの実exeが見つかりません: {candidate}"
            "。環境変数AGENT_WRAPPER_OPENCODE_EXEで直接パスを指定してください。"
        )
    return str(candidate)


def parse_opencode_models(stdout: str) -> dict[str, list[str]]:
    """`opencode models`の出力(1行1つの`source/model`)をsourceごとにまとめる。"""
    sources: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "/" not in line:
            continue
        source, model = line.split("/", 1)
        sources.setdefault(source, []).append(model)
    return sources


def _opencode_models(opencode_exe: str) -> dict[str, list[str]]:
    try:
        result = subprocess.run(
            [opencode_exe, "models"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_opencode_models(result.stdout)


def parse_ollama_tags(payload: dict[str, Any]) -> list[str]:
    """`GET /api/tags`のレスポンス(`{"models": [{"name": "...", ...}, ...]}`)から名前一覧を作る。"""
    return [str(m["name"]) for m in payload.get("models", [])]


def ollama_models(base_url: str = OLLAMA_BASE_URL) -> list[str]:
    """ollamaにpull済みのモデル一覧をAPIから取得する(未起動なら空リスト)。"""
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=5)
        response.raise_for_status()
    except requests.RequestException:
        return []
    return parse_ollama_tags(response.json())


def models_for(harness: str, opencode_exe: str | None = None) -> dict[str, list[str]]:
    if harness == "opencode":
        sources = _opencode_models(opencode_exe or resolve_opencode_exe())
        # opencode.jsonc側の手動登録に関わらず、ollamaに実際にpull済みのモデルを
        # 常にsourceとして提示する(2026-08-31、ユーザーから「ollamaのモデルをAPIから
        # 持ってきてリストにして」との指示で追加)。
        live_ollama = ollama_models()
        if live_ollama:
            sources["ollama"] = live_ollama
        return sources
    return _STATIC_MODELS.get(harness, {})


def build_argv(
    harness: str, source: str, model: str, name: str, opencode_exe: str | None = None
) -> list[str]:
    """harness起動コマンドを組み立てる。

    `name`(ターミナルの役割名)に応じたシステムプロンプトを、既定のシステムプロンプトを
    置き換える形で挿入する(claude/opencodeのみ。codexはCLIに置き換え手段が無いため
    素のまま起動する。詳細はROLE_SYSTEM_PROMPTS/system_prompt_forを参照)。
    """
    validate_role_name(name)
    if harness == "claude":
        argv = ["claude", "--system-prompt", system_prompt_for(name)]
        if model:
            argv += ["--model", model]
        return argv
    if harness == "codex":
        return ["codex", "-m", model] if model else ["codex"]
    if harness == "opencode":
        exe = opencode_exe or resolve_opencode_exe()
        write_opencode_agent_file(name, system_prompt_for(name))
        argv = [exe, "--agent", name]
        if model:
            argv += ["-m", f"{source}/{model}"]
        return argv
    raise ValueError(f"unknown harness: {harness}")


class Session:
    """1つのターミナルに対応する、/startで生成される常駐pty。"""

    _SCROLLBACK_LIMIT = 200_000

    def __init__(self, name: str, argv: list[str], cwd: str) -> None:
        self.name = name
        self.proc = winpty.PtyProcess.spawn(argv, cwd=cwd)
        self.subscribers: set[object] = set()
        self.scrollback = bytearray()

    def write(self, text: str) -> None:
        self.proc.write(text)

    def terminate(self) -> None:
        if self.proc.isalive():
            self.proc.terminate(force=True)

    async def broadcast_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self.proc.isalive():
            data = await loop.run_in_executor(None, self.proc.read, 4096)
            if not data:
                break
            raw = (
                data.encode("utf-8", errors="ignore") if isinstance(data, str) else data
            )
            self.scrollback.extend(raw)
            del self.scrollback[: -self._SCROLLBACK_LIMIT]
            dead: list[object] = []
            for ws in self.subscribers:
                try:
                    await ws.send(data)  # type: ignore[attr-defined]
                except websockets.exceptions.ConnectionClosed:
                    dead.append(ws)
            for ws in dead:
                self.subscribers.discard(ws)


class SessionManager:
    """1つの作業フォルダに対する全ターミナルのライフサイクルを管理する。"""

    def __init__(self, broker: ApprovalBroker | None = None) -> None:
        self.sessions: dict[str, Session] = {}
        self._broadcast_tasks: list[asyncio.Task[None]] = []
        self.approval_broker = broker or ApprovalBroker()

    def shared_state(self, session_id: str, harness: str) -> SharedState:
        """wrapped runnerを中央Brokerへ接続するセッション別状態を作る。"""
        return SharedState(
            broker=self.approval_broker,
            session_id=session_id,
            harness=harness,
        )

    def respond_approval(
        self,
        session_id: str,
        request_id: str,
        action: str,
        message: str = "",
    ) -> bool:
        try:
            decision = ApprovalDecision(action, message)  # type: ignore[arg-type]
        except ValueError:
            return False
        return (
            self.approval_broker.respond(session_id, request_id, decision) is not None
        )

    async def stop_all(self) -> None:
        for task in self._broadcast_tasks:
            task.cancel()
        self._broadcast_tasks.clear()
        for session in self.sessions.values():
            session.terminate()
        self.sessions.clear()

    async def start(self, cwd: str, terminals: list[dict[str, Any]]) -> list[str]:
        await self.stop_all()
        names: list[str] = []
        for term in terminals:
            name = str(term["name"])
            argv = build_argv(
                str(term["harness"]),
                str(term.get("source", "")),
                str(term.get("model", "")),
                name,
            )
            session = Session(name, argv, cwd)
            self.sessions[name] = session
            self._broadcast_tasks.append(asyncio.create_task(session.broadcast_loop()))
            names.append(name)
        remember_folder(cwd)
        return names


def _pick_folder() -> str | None:
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title="作業フォルダを選択") or None
    finally:
        root.destroy()


def _make_http_handler(
    manager: SessionManager, loop_holder: list[asyncio.AbstractEventLoop]
) -> type[http.server.SimpleHTTPRequestHandler]:
    handler_dir = str(_STATIC_PAGE_PATH.parent)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=handler_dir, **kwargs)  # type: ignore[arg-type]

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/pick-folder":
                self._json({"path": _pick_folder()})
                return
            if self.path.startswith("/models"):
                harness = (
                    self.path.split("?harness=", 1)[-1]
                    if "?harness=" in self.path
                    else ""
                )
                try:
                    self._json(models_for(harness))
                except RuntimeError as e:
                    self._json({"error": str(e)}, status=500)
                return
            if self.path == "/sessions":
                self._json({"names": list(manager.sessions.keys())})
                return
            if self.path == "/recent-folders":
                self._json({"folders": load_recent_folders()})
                return
            if self.path == "/approvals":
                self._json(
                    {
                        "requests": [
                            item.to_dict() for item in manager.approval_broker.pending()
                        ]
                    }
                )
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/start":
                body = self._read_json()
                future = asyncio.run_coroutine_threadsafe(
                    manager.start(str(body["cwd"]), list(body["terminals"])),
                    loop_holder[0],
                )
                try:
                    names = future.result(timeout=30)
                except ValueError as e:
                    self._json({"error": str(e)}, status=400)
                    return
                self._json({"names": names})
                return
            if self.path.startswith("/send/"):
                name = self.path.removeprefix("/send/")
                body = self._read_json()
                session = manager.sessions.get(name)
                if session is None or not session.proc.isalive():
                    self._json({"error": f"pane not active: {name}"}, status=404)
                    return
                text = str(body.get("text", ""))
                if body.get("enter", True):
                    text += "\r"
                session.write(text)
                self._json({"ok": True})
                return
            if self.path.startswith("/approvals/") and self.path.endswith("/respond"):
                request_id = self.path.removeprefix("/approvals/").removesuffix(
                    "/respond"
                )
                body = self._read_json()
                session_id = str(body.get("session_id", ""))
                action = str(body.get("action", ""))
                message = str(body.get("message", ""))
                if not session_id or not request_id:
                    self._json(
                        {
                            "ok": False,
                            "error": "session_id and request_id are required",
                        },
                        status=400,
                    )
                    return
                if not manager.respond_approval(
                    session_id, request_id, action, message
                ):
                    self._json(
                        {
                            "ok": False,
                            "error": "request is missing, stale, expired, or invalid",
                        },
                        status=409,
                    )
                    return
                self._json({"ok": True})
                return
            self.send_error(404)

    return Handler


def _serve_http(
    manager: SessionManager, loop_holder: list[asyncio.AbstractEventLoop]
) -> None:
    handler = _make_http_handler(manager, loop_holder)
    with http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler) as httpd:
        httpd.serve_forever()


async def _handle_ws(ws: object, session: Session) -> None:
    if session.scrollback:
        await ws.send(bytes(session.scrollback).decode("utf-8", errors="ignore"))  # type: ignore[attr-defined]
    session.subscribers.add(ws)
    try:
        async for message in ws:  # type: ignore[attr-defined]
            if isinstance(message, bytes):
                message = message.decode("utf-8", errors="ignore")
            session.write(message)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        session.subscribers.discard(ws)


async def _main_async() -> None:
    manager = SessionManager()
    loop_holder = [asyncio.get_running_loop()]

    async def router(ws: object) -> None:
        path = ws.request.path  # type: ignore[attr-defined]
        name = path.strip("/")
        session = manager.sessions.get(name)
        if session is None:
            await ws.close(code=4004, reason=f"unknown pane: {name}")  # type: ignore[attr-defined]
            return
        await _handle_ws(ws, session)

    threading.Thread(
        target=_serve_http, args=(manager, loop_holder), daemon=True
    ).start()
    webbrowser.open(f"http://127.0.0.1:{HTTP_PORT}/index.html")
    async with serve(router, "127.0.0.1", WS_PORT):
        print(f"WebSocket: ws://127.0.0.1:{WS_PORT}/<pane-name>")
        print(f"HTTP:      http://127.0.0.1:{HTTP_PORT}/index.html")
        await asyncio.Future()


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
