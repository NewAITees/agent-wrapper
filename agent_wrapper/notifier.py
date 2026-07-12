"""
通知の送信口をひとつにまとめておくモジュール。

標準出力とログファイルへの記録に加えて、ntfy_topic が指定されていれば
ntfy.sh(https://ntfy.sh)経由でMac/iPad/Androidへプッシュ通知する。
"""

import datetime
from collections.abc import Callable

import requests

LEVEL_LABEL = {"high": "[高]", "medium": "[中]", "low": "[低]"}

NTFY_URL = "https://ntfy.sh"
# ntfyのJSON publish APIではpriorityは整数(1〜5)でなければならない。
# 文字列("urgent"等)はHTTPヘッダ方式でのみ有効なので、ここでは整数に変換する。
NTFY_PRIORITY = {"high": 5, "medium": 3, "low": 1}


def send_ntfy(topic: str, level: str, title: str, body: str, timeout: int = 10) -> None:
    try:
        requests.post(
            NTFY_URL,
            json={
                "topic": topic,
                "title": title,
                "message": body,
                "priority": NTFY_PRIORITY.get(level, 3),
            },
            timeout=timeout,
        )
    except Exception:
        pass


def make_notifier(
    log_path: str = "notifications.log",
    ntfy_topic: str | None = None,
) -> Callable[[str, str, str], None]:
    def send(level: str, title: str, body: str) -> None:
        line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {LEVEL_LABEL.get(level, '')} {title}: {body}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        if ntfy_topic:
            send_ntfy(ntfy_topic, level, title, body)

    return send
