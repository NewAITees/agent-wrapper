"""
Claude Code / Codex をサブプロセスとして起動し、出力を監視するラッパー本体。

役割:
- エージェントの標準出力を読み取りログとして蓄積する
- ルールエンジンで破壊的操作を検知する
- エージェントの意思表示マーカー(::REQUEST_STOP:: / ::REQUEST_PERMISSION::)を検知する
- 権限確認はollamaに一次判定させ、危険なら人間にエスカレーションする
- 一定間隔でollamaに要約させ、状態として保持する
- ダッシュボード(dashboard.py)から参照される共有状態(SharedState)を更新する
"""

import collections
import datetime
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from . import ollama_client, rules

Notifier = Callable[[str, str, str], None]


class SharedState:
    """dashboard.py と wrapper.py の間で共有する状態。スレッドセーフにするためlockを持つ。"""

    def __init__(self, log_maxlen: int = 2000) -> None:
        self.lock = threading.Lock()
        self.log: collections.deque[str] = collections.deque(maxlen=log_maxlen)
        self.status = "starting"  # starting | running | waiting_human | stopped
        self.pending_reason: str | None = None  # 人間の判断待ちの理由
        self.pending_kind: str | None = (
            None  # "destructive" | "stop_request" | "major_decision"
        )
        self.last_summary: str | None = None
        self.last_summary_time: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.approve_event = threading.Event()

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {line}")

    def tail(self, n: int = 100) -> list[str]:
        with self.lock:
            return list(self.log)[-n:]

    def set_waiting(self, kind: str, reason: str) -> None:
        with self.lock:
            self.status = "waiting_human"
            self.pending_kind = kind
            self.pending_reason = reason
        self.approve_event.clear()

    def set_running(self) -> None:
        with self.lock:
            self.status = "running"
            self.pending_kind = None
            self.pending_reason = None

    def set_summary(self, text: str) -> None:
        with self.lock:
            self.last_summary = text
            self.last_summary_time = datetime.datetime.now().strftime("%H:%M:%S")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "pending_kind": self.pending_kind,
                "pending_reason": self.pending_reason,
                "last_summary": self.last_summary,
                "last_summary_time": self.last_summary_time,
            }


class AgentWrapper:
    """subprocess+正規表現方式でエージェントを監視する現行実装。

    関連: SharedState(同モジュール), rules.py(検知ルール), ollama_client.py(一次判定),
    dashboard.py/notifier.py(このクラスの状態を参照する側)。

    この方式は mock_agent.py には機能するが、実際の claude/codex CLI には
    ヘッドレスモードの制約上そのままでは機能しない(設計ドキュメント参照:
    docs/agent_wrapper_sdk_integration_plan.md)。claude/codexへの本統合は
    SDKベースの別実装(agent_wrapper/runners/、未着手)に置き換える計画。
    """

    def __init__(
        self,
        cmd: list[str],
        state: SharedState,
        ollama_model: str = "gemma4:e4b",
        checkin_interval_sec: int = 600,
        notifier: Notifier | None = None,
    ) -> None:
        self.cmd = cmd
        self.state = state
        self.ollama_model = ollama_model
        self.checkin_interval_sec = checkin_interval_sec
        self.notifier: Notifier = notifier or (lambda level, title, body: None)
        self._stop_flag = threading.Event()

    def start(self) -> threading.Thread:
        self.state.status = "running"
        self.state.process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        reader_thread.start()

        checkin_thread = threading.Thread(target=self._checkin_loop, daemon=True)
        checkin_thread.start()

        return reader_thread

    def _read_loop(self) -> None:
        proc = self.state.process
        assert proc is not None and proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            line = line.rstrip("\n")
            self.state.append_log(line)
            self._handle_line(line)
        self.state.status = "stopped"
        self.notifier("high", "エージェント終了", "プロセスが終了しました")

    def _handle_line(self, line: str) -> None:
        # 1. 破壊的操作の検知(自己申告より優先し、強制エスカレーション)
        match = rules.check_destructive(line)
        if match.matched:
            self.state.set_waiting("destructive", match.reason)
            self.notifier("high", "削除系の操作を検知", match.reason)
            self._wait_for_approval()
            return

        # 2. エージェント自身の意思表示
        kind, content = rules.parse_agent_signal(line)
        if kind == "stop":
            self.state.set_waiting(
                "stop_request", content or "エージェントが停止を要求"
            )
            self.notifier("high", "エージェントが停止を要求", content or "")
            self._wait_for_approval()
        elif kind == "permission":
            judged = ollama_client.judge_permission(
                content or "", model=self.ollama_model
            )
            if judged["decision"] == "allow":
                self._send_to_stdin("y\n")
                self.state.append_log(f"(ollama自動承認: {content})")
            else:
                self.state.set_waiting("permission_escalated", content or "権限確認")
                self.notifier("medium", "権限確認(要判断)", content or "")
                self._wait_for_approval()

    def _checkin_loop(self) -> None:
        while not self._stop_flag.is_set():
            time.sleep(self.checkin_interval_sec)
            recent = "\n".join(self.state.tail(50))
            if not recent.strip():
                continue
            summary = ollama_client.summarize_chunk(recent, model=self.ollama_model)
            self.state.set_summary(summary)
            self.notifier("low", "定期チェックイン", summary)

            if ollama_client.judge_is_major_decision(recent, model=self.ollama_model):
                self.state.set_waiting("major_decision", summary)
                self.notifier("medium", "方針決定っぽい相談を検知", summary)
                self._wait_for_approval()

    def _wait_for_approval(self) -> None:
        self.state.approve_event.wait()
        self.state.set_running()

    def _send_to_stdin(self, text: str) -> None:
        try:
            assert (
                self.state.process is not None and self.state.process.stdin is not None
            )
            self.state.process.stdin.write(text)
            self.state.process.stdin.flush()
        except Exception as e:
            self.state.append_log(f"(stdin書き込み失敗: {e})")

    def approve(self) -> None:
        """ダッシュボードからの承認操作。待機中でなければ何もしない(誤操作でのstdin書き込みを防ぐ)。"""
        if self.state.snapshot()["status"] != "waiting_human":
            return
        self.state.append_log("(人間が承認しました)")
        self._send_to_stdin("y\n")
        self.state.approve_event.set()

    def deny(self) -> None:
        """ダッシュボードからの却下操作。待機中でなければ何もしない。"""
        if self.state.snapshot()["status"] != "waiting_human":
            return
        self.state.append_log("(人間が却下しました)")
        self._send_to_stdin("n\n")
        self.state.approve_event.set()

    def stop(self) -> None:
        self._stop_flag.set()
        if self.state.process:
            self.state.process.terminate()
        self.state.status = "stopped"
