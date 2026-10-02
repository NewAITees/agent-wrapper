"""AITuberへの補助push通知。

この通知は補助情報であり、承認ゲートやセッション管理の代替・フォールバックではない。
"""

import asyncio
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)


def summarize_approval(action: str, resource: str) -> str:
    """承認要求を配信読み上げ向けに安全な短い要約へ変換する。"""
    words = resource.split()
    if not words:
        return action

    first_word = words[0]
    if (
        "=" in first_word
        or first_word.lower().startswith(("http://", "https://"))
        or first_word.startswith("-")
        or first_word.lower().startswith("authorization:")
    ):
        return action

    is_path = "/" in first_word or "\\" in first_word
    filename = first_word.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    if is_path:
        is_safe_name = bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", filename))
    else:
        is_safe_name = bool(re.fullmatch(r"[a-z][a-z0-9_.+-]{0,23}", filename))
    if not is_safe_name or re.search(r"[A-Za-z0-9]{16,}", filename):
        return action
    return f"{action}: {filename}"


_EVENT_TYPES = {"approval_pending", "session_stopped", "result_arrived"}


def _validate_push_url(url: str) -> None:
    """AITuberのtokenを送るため、明示されたloopbackの/eventだけ許可する。"""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AGENT_WRAPPER_AITUBER_URL is invalid") from exc
    if (
        url != url.strip()
        or parsed.scheme.lower() != "http"
        or hostname is None
        or hostname.lower() not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/event"
        or "?" in url
        or "#" in url
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError(
            "AGENT_WRAPPER_AITUBER_URL must be http to a loopback host at /event"
        )


class AituberPusher:
    """設定された場合だけ通知をdaemon threadから送信する。"""

    def __init__(self, url: str | None = None, token: str | None = None) -> None:
        self.url = url if url is not None else os.getenv("AGENT_WRAPPER_AITUBER_URL")
        if self.url is not None:
            _validate_push_url(self.url)
        self.token = (
            token if token is not None else os.getenv("AGENT_WRAPPER_AITUBER_TOKEN")
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def send(self, event_type: str, session: str, message: str) -> None:
        if event_type not in _EVENT_TYPES:
            raise ValueError(f"unsupported AITuber event type: {event_type}")
        if not self.enabled:
            return
        thread = threading.Thread(
            target=self._send_sync,
            args=(event_type, session, message[:300]),
            daemon=True,
        )
        thread.start()

    def _send_sync(self, event_type: str, session: str, message: str) -> None:
        if not self.url:
            return
        headers = {"X-Aituber-Token": self.token} if self.token else {}
        try:
            response = requests.post(
                self.url,
                json={
                    "type": event_type,
                    "session": session,
                    "message": message[:300],
                },
                headers=headers,
                timeout=3,
                allow_redirects=False,
            )
            if response.status_code != 202:
                logger.warning(
                    "AITuber push failed: HTTP status %s", response.status_code
                )
        except requests.RequestException as exc:
            logger.warning("AITuber push failed: %s", type(exc).__name__)
        except Exception as exc:
            logger.warning("AITuber push failed: %s", type(exc).__name__)


aituber_pusher = AituberPusher()


class InboxTracker:
    """inboxファイルのmtimeを記録し、新規・更新ファイルだけ返す。"""

    def __init__(self, inbox: Path) -> None:
        self.inbox = inbox
        self._mtimes = self._snapshot()

    def _snapshot(self) -> dict[str, int]:
        if not self.inbox.is_dir():
            return {}
        return {
            path.name: path.stat().st_mtime_ns
            for path in self.inbox.iterdir()
            if path.is_file()
        }

    def poll(self) -> list[str]:
        current = self._snapshot()
        changed = [
            name
            for name, mtime in current.items()
            if name not in self._mtimes or self._mtimes[name] != mtime
        ]
        self._mtimes = current
        return changed


async def poll_inbox(inbox: Path, pusher: AituberPusher, session: str) -> None:
    """既存状態を基準にし、以降のinbox新規・更新を通知する。"""
    tracker = InboxTracker(inbox)
    while True:
        await asyncio.sleep(2)
        for filename in tracker.poll():
            pusher.send("result_arrived", session, filename)
