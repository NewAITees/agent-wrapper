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
    harness: str, source: str, model: str, opencode_exe: str | None = None
) -> list[str]:
    if harness == "claude":
        return ["claude", "--model", model] if model else ["claude"]
    if harness == "codex":
        return ["codex", "-m", model] if model else ["codex"]
    if harness == "opencode":
        exe = opencode_exe or resolve_opencode_exe()
        return [exe, "-m", f"{source}/{model}"] if model else [exe]
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

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._broadcast_tasks: list[asyncio.Task[None]] = []

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
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/start":
                body = self._read_json()
                future = asyncio.run_coroutine_threadsafe(
                    manager.start(str(body["cwd"]), list(body["terminals"])),
                    loop_holder[0],
                )
                names = future.result(timeout=30)
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
