"""HTTPスレッドとClaudeのイベントループをつなぐ有界入力キュー。"""

import asyncio
import threading
import time
from collections import deque

MAX_INPUTS = 5
MAX_INPUT_LENGTH = 4000
IDLE_WAIT_SECONDS = 1800.0
HUMAN_LABEL = "配信者の指示"
NOTICE_LABEL = "システム通知"
ORCHESTRATOR_LABEL = "orchestratorからの指示"


class QueuedInput(str):
    """見出し(誰からの入力か)つきの入力。文字列として比較でき、見出しは label で読む。"""

    label: str

    def __new__(cls, text: str, label: str = HUMAN_LABEL) -> "QueuedInput":
        item = super().__new__(cls, text)
        item.label = label
        return item


class WrappedInputQueue:
    """追加指示を安全なターン境界まで保持する。関連: WrappedSession/ClaudeRunner。"""

    def __init__(self) -> None:
        self._items: deque[QueuedInput] = deque()
        self._lock = threading.Lock()
        self._closed = False

    def put(self, text: str, label: str = HUMAN_LABEL) -> int:
        if not text.strip():
            raise ValueError("input must not be empty")
        if len(text) > MAX_INPUT_LENGTH:
            raise ValueError("input exceeds 4000 characters")
        with self._lock:
            if self._closed:
                raise ValueError("session stopped")
            if len(self._items) >= MAX_INPUTS:
                raise ValueError("input queue is full")
            self._items.append(QueuedInput(text, label))
            return len(self._items)

    def take_nowait(self) -> QueuedInput | None:
        with self._lock:
            return self._items.popleft() if self._items else None

    async def wait(self, timeout: float) -> QueuedInput | None:
        deadline = time.monotonic() + timeout
        while True:
            item = self.take_nowait()
            if item is not None:
                return item
            with self._lock:
                if self._closed:
                    return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.02, remaining))

    def close(self) -> int:
        with self._lock:
            self._closed = True
            remaining = len(self._items)
            self._items.clear()
            return remaining
