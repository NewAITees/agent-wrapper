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
from urllib.parse import parse_qs, urlsplit

import requests
import websockets
import winpty
from websockets.asyncio.server import serve

from . import ollama_client, rules
from .approval import ApprovalBroker, ApprovalDecision
from .approval_adapters import OpenCodePermissionAdapter
from .runners.claude_runner import ClaudeRunner
from .runners.codex_runner import CodexRunner
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
        "各セッション(planner/worker/tester/reviewer等)からの許可依頼・質問を一次的に受け止め、"
        "内容を人間が判断しやすい形に整理してください。あなた自身が最終的な実行許可を"
        "与えることはありません。\n\n"
        "打ち返しループ(手戻り)を担当します:\n"
        "1. worker/testerが完了マーカーをinboxに置くのを確認する\n"
        "2. reviewerのレビュー結果(.agent-server/inbox/review-result.md等)とテスト結果を読む\n"
        "3. 不合格なら、該当セッション(worker/tester)へ`/send`相当の手段で修正指示を送り、1に戻す\n"
        "4. 合格なら、人間に完了報告とコミット可否の確認を求める(あなたはcommit/pushしない)\n"
        "(delegate skillの方針: 完了報告を鵜呑みにせず、実際の差分・テスト結果を見てから次を判断する)"
    ),
    "planner": (
        "あなたはこのマルチセッション作業におけるplannerです。"
        "要求を分析し、実装に入る前に具体的な計画(目的・方針・変更範囲・影響・検証方法)を"
        "まとめることに専念してください。自分でコードを実装することはせず、"
        "計画は`.md`ファイルとしてinboxに書き出し、workerとtesterへ並列で引き継ぐ前提で"
        "作業してください(workerが実装・testerがテスト作成を同時に進められるよう、"
        "両方が着手できる粒度で書くこと)。"
    ),
    "worker": (
        "あなたはこのマルチセッション作業におけるworkerです。"
        "与えられた計画・指示に従って実装作業を行うことに専念してください。"
        "計画そのものの妥当性に大きな疑問がある場合は、実装を進める前にその旨を明確に述べてください。"
        "要求された以上の抽象化・機能・柔軟性を追加しないこと(simplify skillの方針: "
        "シンプルさより複雑な設計を選ぶ理由がない限り、シンプルな方を選ぶ)。"
        "git commit/pushは行わない(人間の役割)。"
    ),
    "tester": (
        "あなたはこのマルチセッション作業におけるtesterです。"
        "plannerの計画を受けて、workerの実装と並行してテストコードの作成に専念してください。"
        "テストの実行結果自体は`/run-tests`のような機械的なAPIからも"
        "取得できますが、あなたの役割は「何をテストすべきか」を計画からテストケースに"
        "落とし込み、実際にテストコードを書くことです(run skillの方針: 実際に実行して"
        "結果を確認してから完了と報告する。テストを書いただけで完了とみなさない)。"
        "git commit/pushは行わない(人間の役割)。"
    ),
    "reviewer": (
        "あなたはこのマルチセッション作業におけるreviewerです。"
        "他セッションが行った実装・テストの正しさ・簡潔さ・安全性をレビューすることに"
        "専念してください。自分でソースコードを編集することはありません。"
        "正しさのバグ(failure_scenario付き)と、簡略化できる点は分けて指摘し、"
        "確信度の低い指摘を確信度の高いものと同列に誇張しないこと(code-review skillの方針)。"
        "レビュー結果は`.md`ファイルとして`.agent-server/inbox/review-result.md`等に"
        "書き出してください(これだけは例外的に書き込みが許可されています)。"
        "git commit/pushは行わない(人間の役割)。"
    ),
    "utility": (
        "あなたはこのマルチセッション作業におけるutilityです。"
        "他セッションからの軽量な下請け作業(要約、内容の一次判定、簡単な確認作業など)を"
        "担当してください。大きな設計判断や最終的な許可判断は行わず、"
        "材料を整理して返すことに専念してください。"
    ),
}

# 役割ごとに、harness自身が強制するツール権限を割り当てる(2026-09-01、ユーザー指示:
# システムプロンプトは「お願い」に過ぎず逸脱しうるため、harnessが物理的に強制できる
# 権限機構(claude wrappedのPreToolUse、raw CLIのallowedTools/disallowedTools、
# codexのsandbox、opencodeのagent permission)を役割ごとに設定する)。
# git commit/pushはどの役割にも渡さず、常に人間だけが行える前提にしている。
# claudeの"effort"はClaudeAgentOptions.effortへそのまま渡す(2026-09-02、コスト
# 急増インシデントを受けて追加)。SDK既定は"high"(最大の思考コスト)で、この
# リポジトリはこれまで一度も明示的に下げていなかった。git status/diff程度しか
# 行わないworker/testerは"low"で十分なため、役割ごとに絞る。未指定時のfallbackも
# role_permission_forの呼び出し側でなくここで"low"にしておく(明示し忘れを防ぐ)。
ROLE_PERMISSIONS: dict[str, dict[str, Any]] = {
    "orchestrator": {
        "claude": {"disallowedTools": ["Edit", "Write", "Bash"], "effort": "medium"},
        "codex": {"sandbox": "read-only"},
        "opencode": {"read": "allow", "edit": "deny", "bash": "deny"},
    },
    "planner": {
        "claude": {
            "disallowedTools": ["Edit", "Write", "Bash"],
            "allowedTools": ["Edit(**/*.md)", "Write(**/*.md)"],
            "effort": "medium",
        },
        "codex": {"sandbox": "read-only"},
        "opencode": {"read": "allow", "edit": "ask", "bash": "deny"},
    },
    "worker": {
        "claude": {
            "allowedTools": [
                "Bash(git status)",
                "Bash(git diff *)",
                "Bash(git log *)",
                "Bash(uv run pytest *)",
                "Bash(uv run ruff *)",
                "Bash(uv run mypy *)",
            ],
            "disallowedTools": [
                "Bash(git commit *)",
                "Bash(git push *)",
                "Bash(git merge *)",
            ],
            "effort": "low",
        },
        "codex": {"sandbox": "workspace-write"},
        "opencode": {
            "read": "allow",
            "edit": "allow",
            "bash": {
                "git status": "allow",
                "git diff *": "allow",
                "git log *": "allow",
                "uv run pytest*": "allow",
                "uv run ruff*": "allow",
                "uv run mypy*": "allow",
                "git commit *": "deny",
                "git push *": "deny",
                "*": "ask",
            },
        },
    },
    "tester": {
        "claude": {
            "allowedTools": [
                "Bash(git status)",
                "Bash(git diff *)",
                "Bash(uv run pytest *)",
            ],
            "disallowedTools": [
                "Bash(git commit *)",
                "Bash(git push *)",
                "Bash(git merge *)",
            ],
            "effort": "low",
        },
        "codex": {"sandbox": "workspace-write"},
        "opencode": {
            "read": "allow",
            "edit": "allow",
            "bash": {
                "git status": "allow",
                "git diff *": "allow",
                "uv run pytest*": "allow",
                "git commit *": "deny",
                "git push *": "deny",
                "*": "ask",
            },
        },
    },
    "reviewer": {
        "claude": {
            "disallowedTools": [
                "Edit",
                "Write",
                "Bash(git commit *)",
                "Bash(git push *)",
                "Bash(git merge *)",
            ],
            "allowedTools": [
                "Edit(**/*.md)",
                "Write(**/*.md)",
                "Bash(uv run pytest *)",
                "Bash(uv run ruff *)",
                "Bash(uv run mypy *)",
                "Bash(git diff *)",
                "Bash(git log *)",
            ],
            "effort": "medium",
        },
        "codex": {"sandbox": "read-only"},
        "opencode": {
            "read": "allow",
            "edit": "ask",
            "bash": {
                "uv run pytest*": "allow",
                "uv run ruff*": "allow",
                "uv run mypy*": "allow",
                "git diff *": "allow",
                "git log *": "allow",
                "*": "deny",
            },
        },
    },
    "utility": {
        "claude": {"disallowedTools": ["Edit", "Write", "Bash"], "effort": "low"},
        "codex": {"sandbox": "read-only"},
        "opencode": {"read": "allow", "edit": "deny", "bash": "deny"},
    },
}


def role_permission_for(name: str, harness: str) -> dict[str, Any]:
    """役割名(未知の名前は制限なし)とharnessから、強制すべき権限設定を返す。"""
    tier = ROLE_PERMISSIONS.get(name.strip().lower())
    return dict(tier.get(harness, {})) if tier else {}


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


def _permission_yaml_lines(value: dict[str, Any], indent: int = 1) -> list[str]:
    """`permission`辞書(文字列値 or ネストしたコマンドパターン辞書)を最小限のYAMLへ変換する。

    opencodeのagent frontmatterはYAMLなので、汎用YAMLライブラリを追加せず、
    この用途に必要な「文字列値」と「1階層のネスト」だけを扱える簡易実装にしている。
    """
    pad = "  " * indent
    lines: list[str] = []
    for key, val in value.items():
        safe_key = key if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*$", key) else f'"{key}"'
        if isinstance(val, dict):
            lines.append(f"{pad}{safe_key}:")
            lines.extend(_permission_yaml_lines(val, indent + 1))
        else:
            lines.append(f"{pad}{safe_key}: {val}")
    return lines


def opencode_agent_markdown(
    name: str, prompt: str, permission: dict[str, Any] | None = None
) -> str:
    """指定した役割名・プロンプト・権限設定から、opencodeのMarkdown agent定義を組み立てる。"""
    lines = [f"description: {name} role (agent-wrapper)", "mode: primary"]
    if permission:
        lines.append("permission:")
        lines.extend(_permission_yaml_lines(permission))
    frontmatter = "\n".join(lines)
    return f"---\n{frontmatter}\n---\n\n{prompt}\n"


def write_opencode_agent_file(
    name: str,
    prompt: str,
    agent_dir: Path | None = None,
    permission: dict[str, Any] | None = None,
) -> Path:
    """opencodeの`--agent <name>`で選択できるよう、役割専用のagent定義を書き出す。

    opencodeにはシステムプロンプトを生CLIフラグで置き換える手段が無く、
    agent定義(prompt本文がそのままシステムプロンプトになる)を経由する必要がある。
    `permission`はagent定義のpermissionフィールド(read/edit/bashの許可・拒否)。
    """
    validate_role_name(name)
    agent_dir = agent_dir or _OPENCODE_AGENT_DIR
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / f"{name}.md"
    path.write_text(opencode_agent_markdown(name, prompt, permission), encoding="utf-8")
    return path


_OPENCODE_BROKER_PLUGIN_MARKER = "// generated by agent-wrapper approval broker"


def write_opencode_broker_plugin(
    name: str,
    cwd: str,
    broker_url: str = f"http://127.0.0.1:{HTTP_PORT}",
) -> Path:
    """OpenCode permission eventをBrokerへ中継するpluginを対象projectへ配置する。"""
    validate_role_name(name)
    path = Path(cwd) / ".opencode" / "plugins" / f"agent-wrapper-{name}.js"
    if path.exists() and _OPENCODE_BROKER_PLUGIN_MARKER not in path.read_text(
        encoding="utf-8"
    ):
        raise ValueError(f"既存のOpenCode pluginを上書きできません: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    source = f"""{_OPENCODE_BROKER_PLUGIN_MARKER}
export const AgentWrapperApprovalPlugin = async ({{ client }}) => {{
  console.error("[agent-wrapper] permission event plugin loaded")
  return {{
    event: async ({{ event }}) => {{
      if (event.type !== "permission.asked") return
      const request = event.properties
      try {{
        const response = await fetch({json.dumps(broker_url + "/approval-events")}, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{
            session_id: request.sessionID,
            owner_session_id: {json.dumps(name)},
            harness: "opencode",
            action: request.permission,
            resources: Array.from(request.patterns || []),
            message: "OpenCode permission request",
            metadata: request.metadata || {{}},
          }}),
        }})
        const decision = await response.json()
        await client.permission.reply({{
          requestID: request.id,
          reply: response.ok && decision.effect === "allow" ? "once" : "reject",
        }})
      }} catch (error) {{
        console.error(`[agent-wrapper] approval bridge error: ${{String(error)}}`)
        await client.permission.reply({{ requestID: request.id, reply: "reject" }})
      }}
    }},
  }}
}}
"""
    path.write_text(source, encoding="utf-8")
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


RUN_TESTS_COMMAND: list[str] = ["uv", "run", "pytest", "-q"]
RUN_TESTS_TIMEOUT_SEC = 300


def parse_pytest_failures(stdout: str) -> list[str]:
    """pytestの`-q`出力から`FAILED <test id>`行だけを抜き出す。"""
    return [
        line.removeprefix("FAILED ").split(" - ", 1)[0].strip()
        for line in stdout.splitlines()
        if line.startswith("FAILED ")
    ]


def run_tests(cwd: str, command: list[str] | None = None) -> dict[str, Any]:
    """テストのライフサイクルのうち「実行」だけを担う機械的な処理(AIを介さない)。

    2026-09-01、ユーザー指示: テストは「設計(人間/planner)→実装(tester)→実行(機械的)」の
    3段階に分けるべきで、実行段階にAIは不要。コマンドは決め打ち(RUN_TESTS_COMMAND)のみ
    許可し、任意のシェルコマンドを実行可能にしない。
    """
    argv = command or RUN_TESTS_COMMAND
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=RUN_TESTS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "command": argv,
            "returncode": None,
            "passed": False,
            "timed_out": True,
            "stdout": (e.stdout or ""),
            "stderr": (e.stderr or ""),
            "failed_tests": [],
        }
    return {
        "command": argv,
        "returncode": result.returncode,
        "passed": result.returncode == 0,
        "timed_out": False,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "failed_tests": parse_pytest_failures(result.stdout),
    }


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
    harness: str,
    source: str,
    model: str,
    name: str = "worker",
    opencode_exe: str | None = None,
) -> list[str]:
    """harness起動コマンドを組み立てる。

    `name`(ターミナルの役割名)に応じたシステムプロンプトを、既定のシステムプロンプトを
    置き換える形で挿入する(claude/opencodeのみ。codexはCLIに置き換え手段が無いため
    素のまま起動する。詳細はROLE_SYSTEM_PROMPTS/system_prompt_forを参照)。
    さらに、その役割がharness自身の権限機構で実際に強制できることの範囲を狭める
    (ROLE_PERMISSIONS/role_permission_for参照。システムプロンプトは「お願い」に過ぎず
    逸脱しうるため、harnessが物理的に強制できる権限を役割ごとに割り当てている)。
    """
    validate_role_name(name)
    permission = role_permission_for(name, harness)
    if harness == "claude":
        argv = ["claude", "--system-prompt", system_prompt_for(name)]
        if model:
            argv += ["--model", model]
        if permission.get("allowedTools"):
            argv += ["--allowedTools", " ".join(permission["allowedTools"])]
        if permission.get("disallowedTools"):
            argv += ["--disallowedTools", " ".join(permission["disallowedTools"])]
        return argv
    if harness == "codex":
        argv = ["codex", "-m", model] if model else ["codex"]
        if permission.get("sandbox"):
            argv += ["-s", permission["sandbox"]]
        return argv
    if harness == "opencode":
        exe = opencode_exe or resolve_opencode_exe()
        write_opencode_agent_file(
            name, system_prompt_for(name), permission=permission or None
        )
        argv = [exe, "--agent", name]
        if model:
            argv += ["-m", f"{source}/{model}"]
        return argv
    raise ValueError(f"unknown harness: {harness}")


class Session:
    """1つのターミナルに対応する、/startで生成される常駐pty。"""

    _SCROLLBACK_LIMIT = 200_000

    def __init__(
        self,
        name: str,
        argv: list[str],
        cwd: str,
        harness: str = "",
        source: str = "",
        model: str = "",
    ) -> None:
        self.name = name
        self.harness = harness
        self.source = source
        self.model = model
        self.proc = winpty.PtyProcess.spawn(argv, cwd=cwd)
        self.subscribers: set[object] = set()
        self.scrollback = bytearray()

    def write(self, text: str) -> None:
        self.proc.write(text)

    def output_text(self, tail: int | None = None) -> str:
        """外部のターミナル/AIがWebSocketを張らずに出力を読むためのスナップショット。"""
        raw = bytes(self.scrollback)
        if tail is not None and tail > 0:
            raw = raw[-tail:]
        return raw.decode("utf-8", errors="ignore")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "harness": self.harness,
            "source": self.source,
            "model": self.model,
            "mode": "raw-pty",
            "alive": self.proc.isalive(),
        }

    def is_alive(self) -> bool:
        return bool(self.proc.isalive())

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


class WrappedSession:
    """SDK/MCP runnerをWeb terminalへ接続する承認Broker対応セッション。"""

    def __init__(
        self,
        name: str,
        harness: str,
        source: str,
        model: str,
        cwd: str,
        state: SharedState,
    ) -> None:
        self.name = name
        self.harness = harness
        self.source = source
        self.model = model
        self.cwd = cwd
        self.state = state
        self.subscribers: set[object] = set()
        self.scrollback = bytearray()
        self._input = ""
        self._runner: ClaudeRunner | CodexRunner | None = None
        self._terminated = False
        self.state.append_log(
            "[wrapped] タスクを入力してEnterを押すと、承認Broker経由で開始します。"
        )

    def write(self, text: str) -> None:
        # Enter(送信)は"\r"のみで判定する。"\n"は複数行タスク文の一部として
        # そのままバッファへ積む(2026-09-01修正: 従来は"\n"も送信扱いだったため、
        # /sendへ複数行本文を渡すと最初の改行までしか実際のタスクに渡らず、
        # 以降の要件が丸ごと欠落してworkerが「タスク内容が不明」と聞き返す
        # 不具合があった)。
        if self._runner is not None:
            return
        for char in text:
            if char == "\r":
                prompt = self._input.strip()
                self._input = ""
                if prompt:
                    self._start(prompt)
            elif char in {"\x08", "\x7f"}:
                self._input = self._input[:-1]
            elif char == "\n" or char >= " ":
                self._input += char

    def _start(self, prompt: str) -> None:
        self.state.append_log(f"[prompt] {prompt}")
        full_prompt = f"{system_prompt_for(self.name)}\n\n{prompt}"
        runner_type = ClaudeRunner if self.harness == "claude" else CodexRunner
        permission = role_permission_for(self.name, self.harness)
        self._runner = runner_type(
            prompt=full_prompt, cwd=self.cwd, state=self.state, permission=permission
        )
        self._runner.start()

    def output_text(self, tail: int | None = None) -> str:
        raw = bytes(self.scrollback)
        if tail is not None and tail > 0:
            raw = raw[-tail:]
        return raw.decode("utf-8", errors="ignore")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "harness": self.harness,
            "source": self.source,
            "model": self.model,
            "mode": "wrapped",
            "alive": self.is_alive(),
        }

    def is_alive(self) -> bool:
        if self._terminated:
            return False
        return self._runner is None or self.state.snapshot()["status"] != "stopped"

    def terminate(self) -> None:
        self._terminated = True
        if self._runner is not None:
            self._runner.stop()
        else:
            self.state.set_stopped("terminated", "session stopped before prompt")

    async def broadcast_loop(self) -> None:
        sent = ""
        while self.is_alive():
            current = self.state.full_log_text()
            if current != sent:
                delta = current[len(sent) :] if current.startswith(sent) else current
                raw = delta.encode("utf-8", errors="ignore")
                self.scrollback.extend(raw)
                dead: list[object] = []
                for ws in self.subscribers:
                    try:
                        await ws.send(delta)  # type: ignore[attr-defined]
                    except websockets.exceptions.ConnectionClosed:
                        dead.append(ws)
                for ws in dead:
                    self.subscribers.discard(ws)
                sent = current
            await asyncio.sleep(0.1)


class SessionManager:
    """1つの作業フォルダに対する全ターミナルのライフサイクルを管理する。"""

    def __init__(self, broker: ApprovalBroker | None = None) -> None:
        self.sessions: dict[str, Session | WrappedSession] = {}
        self._broadcast_tasks: dict[str, asyncio.Task[None]] = {}
        self.approval_broker = broker or ApprovalBroker()
        self.cwd: str | None = None
        self._external_session_ids: dict[str, set[str]] = {}
        self._external_session_ids_lock = threading.Lock()

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

    def evaluate_permission(
        self,
        session_id: str,
        harness: str,
        event: dict[str, object],
        timeout: float = 300.0,
        owner_session_id: str | None = None,
    ) -> ApprovalDecision:
        """構造化permission eventを一次判定し、必要な場合だけ人間を待つ。"""
        if owner_session_id:
            if owner_session_id not in self.sessions:
                return ApprovalDecision("deny", "owner session is not active")
            with self._external_session_ids_lock:
                self._external_session_ids.setdefault(owner_session_id, set()).add(
                    session_id
                )
        request_input = OpenCodePermissionAdapter(session_id, harness).adapt(event)
        text = f"{request_input.action}: {request_input.resource}"
        match = rules.check_destructive(text)
        if not match.matched:
            judged = ollama_client.judge_permission(text)
            if judged["decision"] == "allow":
                return ApprovalDecision("approve", "utility AI approved")

        completed = threading.Event()
        decisions: list[ApprovalDecision] = []

        def resolve(decision: ApprovalDecision) -> None:
            decisions.append(decision)
            completed.set()

        request = self.approval_broker.submit(request_input, callback=resolve)
        if completed.wait(timeout):
            return decisions[0]
        timeout_decision = ApprovalDecision("deny", "Approval Broker timed out")
        self.approval_broker.respond(session_id, request.request_id, timeout_decision)
        return decisions[0] if decisions else timeout_decision

    async def stop_all(self) -> None:
        for name in list(self.sessions):
            await self.stop_one(name)
        self.approval_broker.cancel_all()

    async def stop_one(self, name: str) -> bool:
        """1セッションだけを停止・破棄する(全体を作り直さずに済ませるため)。"""
        session = self.sessions.pop(name, None)
        if session is None:
            return False
        task = self._broadcast_tasks.pop(name, None)
        if task is not None:
            task.cancel()
        session.terminate()
        self.approval_broker.cancel_session(name)
        with self._external_session_ids_lock:
            external_ids = self._external_session_ids.pop(name, set())
        for external_id in external_ids:
            self.approval_broker.cancel_session(external_id)
        return True

    async def start(self, cwd: str, terminals: list[dict[str, Any]]) -> list[str]:
        await self.stop_all()
        self.cwd = cwd
        names: list[str] = []
        for term in terminals:
            name = str(term["name"])
            harness = str(term["harness"])
            source = str(term.get("source", ""))
            model = str(term.get("model", ""))
            default_mode = "wrapped" if harness in {"claude", "codex"} else "raw-pty"
            mode = str(term.get("mode", default_mode))
            session: Session | WrappedSession
            if mode == "wrapped" and harness in {"claude", "codex"}:
                session = WrappedSession(
                    name,
                    harness,
                    source,
                    model,
                    cwd,
                    self.shared_state(name, harness),
                )
            elif mode == "raw-pty":
                if harness == "opencode":
                    write_opencode_broker_plugin(name, cwd)
                argv = build_argv(harness, source, model, name)
                session = Session(
                    name, argv, cwd, harness=harness, source=source, model=model
                )
            else:
                raise ValueError(f"unsupported session mode {mode!r} for {harness}")
            self.sessions[name] = session
            self._broadcast_tasks[name] = asyncio.create_task(session.broadcast_loop())
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

        def _split_query(self, path: str) -> tuple[str, dict[str, list[str]]]:
            split = urlsplit(path)
            return split.path, parse_qs(split.query)

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
                self._json(
                    {
                        "names": list(manager.sessions.keys()),
                        "sessions": [s.to_dict() for s in manager.sessions.values()],
                    }
                )
                return
            if self.path.startswith("/sessions/"):
                clean_path, query = self._split_query(self.path)
                if clean_path.endswith("/output"):
                    name = clean_path.removeprefix("/sessions/").removesuffix("/output")
                    session = manager.sessions.get(name)
                    if session is None:
                        self._json({"error": f"unknown session: {name}"}, status=404)
                        return
                    tail = int(query.get("tail", [0])[0] or 0) or None
                    self._json({"name": name, "text": session.output_text(tail)})
                    return
            if self.path == "/recent-folders":
                self._json({"folders": load_recent_folders()})
                return
            if self.path.startswith("/approvals"):
                _, query = self._split_query(self.path)
                session_id = query.get("session_id", [None])[0]
                self._json(
                    {
                        "requests": [
                            item.to_dict()
                            for item in manager.approval_broker.pending(session_id)
                        ]
                    }
                )
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/run-tests":
                if manager.cwd is None:
                    self._json(
                        {"error": "no active workspace (call /start first)"}, status=400
                    )
                    return
                self._json(run_tests(manager.cwd))
                return
            if self.path == "/approval-events":
                body = self._read_json()
                session_id = str(body.get("session_id", ""))
                harness = str(body.get("harness", ""))
                if not session_id or harness != "opencode":
                    self._json(
                        {"error": "valid session_id and opencode harness are required"},
                        status=400,
                    )
                    return
                try:
                    decision = manager.evaluate_permission(
                        session_id,
                        harness,
                        {
                            "action": body.get("action", ""),
                            "resources": body.get("resources", []),
                            "message": body.get("message", ""),
                            "metadata": body.get("metadata", {}),
                        },
                        owner_session_id=str(body.get("owner_session_id", "")) or None,
                    )
                except ValueError as error:
                    self._json({"error": str(error)}, status=400)
                    return
                self._json(
                    {
                        "effect": "allow" if decision.action == "approve" else "deny",
                        "message": decision.message,
                    }
                )
                return
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
                if session is None or not session.is_alive():
                    self._json({"error": f"pane not active: {name}"}, status=404)
                    return
                text = str(body.get("text", ""))
                if body.get("enter", True):
                    text += "\r"
                session.write(text)
                self._json({"ok": True})
                return
            if self.path.startswith("/sessions/") and self.path.endswith("/stop"):
                name = self.path.removeprefix("/sessions/").removesuffix("/stop")
                stop_future = asyncio.run_coroutine_threadsafe(
                    manager.stop_one(name), loop_holder[0]
                )
                stopped = stop_future.result(timeout=10)
                if not stopped:
                    self._json({"error": f"unknown session: {name}"}, status=404)
                    return
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
            if self.path == "/utility/judge":
                body = self._read_json()
                mode = str(body.get("mode", ""))
                text = str(body.get("text", ""))
                try:
                    if mode == "permission":
                        result: object = ollama_client.judge_permission(text)
                    elif mode == "summarize":
                        result = ollama_client.summarize_chunk(text)
                    elif mode == "conversational":
                        result = ollama_client.judge_conversational_question(text)
                    else:
                        self._json({"error": f"unknown mode: {mode}"}, status=400)
                        return
                except Exception as e:
                    self._json({"error": str(e)}, status=502)
                    return
                self._json(result)
                return
            self.send_error(404)

    return Handler


def _serve_http(
    manager: SessionManager, loop_holder: list[asyncio.AbstractEventLoop]
) -> None:
    handler = _make_http_handler(manager, loop_holder)
    with http.server.ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), handler) as httpd:
        httpd.serve_forever()


async def _handle_ws(ws: object, session: Session | WrappedSession) -> None:
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
