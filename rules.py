"""
削除・破壊的操作を検知するルールエンジン。
ここに引っかかったものは、エージェントが「権限だけ欲しい」と自己申告していても
問答無用でSTOP(人間確認)に格上げする。
"""
import re
from dataclasses import dataclass


@dataclass
class RuleMatch:
    matched: bool
    pattern: str = ""
    reason: str = ""


DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+-rf\b", "再帰的な強制削除 (rm -rf)"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard による変更の破棄"),
    (r"\bgit\s+push\s+.*--force\b", "force push"),
    (r"\bdrop\s+table\b", "DROP TABLE"),
    (r"\bdrop\s+database\b", "DROP DATABASE"),
    (r"\bdel\s+/f\s+/s\s+/q\b", "Windows del /f /s /q による強制削除"),
    (r"\bformat\s+[a-zA-Z]:\b", "ドライブのフォーマット"),
    (r"\btruncate\s+table\b", "TRUNCATE TABLE"),
    (r">\s*/dev/sd[a-z]\b", "デバイスへの直接書き込み"),
    # パッケージインストール・外部コード取得系。
    # ollamaの一次判定に任せず、削除系と同じく問答無用で人間の確認を必須にする。
    (r"\bnpm\s+i(nstall)?\b", "パッケージインストール (npm install)"),
    (r"\byarn\s+add\b", "パッケージインストール (yarn add)"),
    (r"\bpnpm\s+add\b", "パッケージインストール (pnpm add)"),
    (r"\bpip3?\s+install\b", "パッケージインストール (pip install)"),
    (r"\buv\s+add\b", "パッケージインストール (uv add)"),
    (r"\buv\s+pip\s+install\b", "パッケージインストール (uv pip install)"),
    (r"\bcargo\s+(install|add)\b", "パッケージインストール (cargo install/add)"),
    (r"\bgo\s+(install|get)\b", "パッケージインストール (go install/get)"),
    (r"\bgem\s+install\b", "パッケージインストール (gem install)"),
    (r"\bbrew\s+install\b", "パッケージインストール (brew install)"),
    (r"\bapt(-get)?\s+install\b", "パッケージインストール (apt install)"),
    (r"\bchoco\s+install\b", "パッケージインストール (choco install)"),
    (r"\bwinget\s+install\b", "パッケージインストール (winget install)"),
    (r"\bgit\s+clone\b", "未知のリポジトリの取得 (git clone)"),
    (r"\bdocker\s+pull\b", "未知のDockerイメージの取得 (docker pull)"),
    (r"\bdocker\s+run\b", "未知のDockerイメージの実行 (docker run)"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in DESTRUCTIVE_PATTERNS]


def check_destructive(line: str) -> RuleMatch:
    for pattern, reason in _COMPILED:
        if pattern.search(line):
            return RuleMatch(matched=True, pattern=pattern.pattern, reason=reason)
    return RuleMatch(matched=False)


# エージェントが発する意思表示マーカー。
# CLAUDE.md / AGENTS.md 側でこの形式で出力するようルール化しておく想定。
REQUEST_STOP_MARKER = re.compile(r"::REQUEST_STOP::\s*(.*)")
REQUEST_PERMISSION_MARKER = re.compile(r"::REQUEST_PERMISSION::\s*(.*)")


def parse_agent_signal(line: str) -> tuple[str | None, str | None]:
    """
    エージェントの出力行を見て、意思表示マーカーがあれば種別と内容を返す。
    戻り値: ("stop", 内容) | ("permission", 内容) | (None, None)
    """
    m = REQUEST_STOP_MARKER.search(line)
    if m:
        return "stop", m.group(1).strip()
    m = REQUEST_PERMISSION_MARKER.search(line)
    if m:
        return "permission", m.group(1).strip()
    return None, None
