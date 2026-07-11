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
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from . import ollama_client, qr_display, rules

Notifier = Callable[[str, str, str], None]
HumanAction = Literal["approve", "explain", "deny"]
StopReason = Literal[
    "running",
    "normal_complete",
    "error",
    "max_conversation_turns",
    "terminated",
]
ApprovalCallback = Callable[["HumanResponse"], None]

_LOG_TEXT_LIMIT = 400
_LOG_TEXT_HEAD = 200


def summarize_log_text(text: str) -> str:
    """ログ表示だけを短縮する。承認判定やApprovalRequest.detailには使わない。"""
    one_line = text.replace("\r", "").replace("\n", "\\n")
    if len(one_line) <= _LOG_TEXT_LIMIT:
        return one_line
    target_match = re.search(
        r"\bSet-Content\s+(?:(?:-LiteralPath|-Path)\s+)?[\"']?([^\"'|;\r\n]+)",
        text,
        re.IGNORECASE,
    )
    target = target_match.group(1).strip() if target_match else "抽出不可"
    return f"{one_line[:_LOG_TEXT_HEAD]}…(全{len(text)}文字、対象: {target})"


def format_audit_log(decider: str, result: str, target: str, reason: str = "") -> str:
    """承認判定をgrep可能な統一形式の1行にする。"""
    line = f"[監査] 判定者={decider} 結果={result} 対象={summarize_log_text(target)}"
    if reason:
        line += f" 理由={summarize_log_text(reason)}"
    return line


_STOP_REASON_LABELS: dict[StopReason, str] = {
    "running": "実行中",
    "normal_complete": "正常完了",
    "error": "エラー",
    "max_conversation_turns": "最大会話ターン到達",
    "terminated": "手動停止",
}


@dataclass(slots=True)
class HumanResponse:
    action: HumanAction
    message: str = ""

    def to_agent_text(self) -> str:
        message = self.message.strip()
        if self.action == "approve":
            return "y" if not message else f"y。補足: {message}"
        if self.action == "explain":
            return (
                "説明してください。" if not message else f"説明してください。{message}"
            )
        return "n" if not message else f"n。理由: {message}"

    def log_label(self) -> str:
        if self.action == "approve":
            return "承認"
        if self.action == "explain":
            return "説明要求"
        return "却下"


@dataclass(slots=True)
class ApprovalRequest:
    id: int
    kind: str
    reason: str
    created_at: str
    detail: str = ""
    callback: ApprovalCallback = field(repr=False, default=lambda response: None)
    response: HumanResponse | None = None

    def resolve(self, response: HumanResponse) -> None:
        self.response = response
        self.callback(response)


class SharedState:
    """dashboard.py と wrapper.py の間で共有する状態。スレッドセーフにするためlockを持つ。"""

    def __init__(self, log_maxlen: int = 2000) -> None:
        self.lock = threading.Lock()
        self.log: collections.deque[str] = collections.deque(maxlen=log_maxlen)
        self.status = "starting"  # starting | running | waiting_human | stopped
        self.last_summary: str | None = None
        self.last_summary_time: str | None = None
        self.stop_reason: StopReason = "running"
        self.stop_reason_detail: str | None = None
        self.dashboard_url: str | None = None
        self.qr_code_data_url: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self._pending_requests: collections.deque[ApprovalRequest] = collections.deque()
        self._next_request_id = 1

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {line}")

    def tail(self, n: int = 100) -> list[str]:
        with self.lock:
            return list(self.log)[-n:]

    def full_log_text(self) -> str:
        with self.lock:
            return "\n".join(self.log)

    def set_waiting(
        self,
        kind: str,
        reason: str,
        callback: ApprovalCallback | None = None,
        detail: str = "",
    ) -> ApprovalRequest:
        created_at = datetime.datetime.now().strftime("%H:%M:%S")
        with self.lock:
            request = ApprovalRequest(
                id=self._next_request_id,
                kind=kind,
                reason=reason,
                created_at=created_at,
                detail=detail,
                callback=callback or (lambda response: None),
            )
            self._next_request_id += 1
            self._pending_requests.append(request)
            self.status = "waiting_human"
        return request

    def set_running(self) -> None:
        with self.lock:
            if not self._pending_requests:
                self.status = "running"
            self.stop_reason = "running"
            self.stop_reason_detail = None

    def set_summary(self, text: str) -> None:
        with self.lock:
            self.last_summary = text
            self.last_summary_time = datetime.datetime.now().strftime("%H:%M:%S")

    def set_qr_code(self, url: str) -> None:
        with self.lock:
            self.dashboard_url = url
            self.qr_code_data_url = qr_display.make_qr_data_url(url)

    def respond(
        self,
        response: HumanResponse,
        request_id: int | None = None,
    ) -> ApprovalRequest | None:
        with self.lock:
            if not self._pending_requests:
                return None
            current = self._pending_requests[0]
            if request_id is not None and current.id != request_id:
                return None
            request = self._pending_requests.popleft()
            request.response = response
            if self.status != "stopped":
                self.status = "waiting_human" if self._pending_requests else "running"
        request.resolve(response)
        return request

    def set_stopped(self, reason: StopReason, detail: str | None = None) -> None:
        with self.lock:
            self.status = "stopped"
            self._pending_requests.clear()
            self.stop_reason = reason
            self.stop_reason_detail = detail

    def stop_reason_label(self) -> str:
        with self.lock:
            return _STOP_REASON_LABELS[self.stop_reason]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = self._pending_requests[0] if self._pending_requests else None
            return {
                "status": self.status,
                "pending_kind": current.kind if current else None,
                "pending_reason": current.reason if current else None,
                "pending_detail": current.detail if current else None,
                "pending_count": len(self._pending_requests),
                "pending_request_id": current.id if current else None,
                "last_summary": self.last_summary,
                "last_summary_time": self.last_summary_time,
                "stop_reason": self.stop_reason,
                "stop_reason_label": _STOP_REASON_LABELS[self.stop_reason],
                "stop_reason_detail": self.stop_reason_detail,
                "dashboard_url": self.dashboard_url,
                "qr_code_data_url": self.qr_code_data_url,
            }


class AgentWrapper:
    """subprocess+正規表現方式でエージェントを監視する実装(--agent mock用)。

    関連: SharedState(同モジュール), rules.py(検知ルール), ollama_client.py(一次判定),
    dashboard.py/notifier.py(このクラスの状態を参照する側)。

    この方式は mock_agent.py には機能するが、実際の claude/codex CLI には
    ヘッドレスモードの制約上そのままでは機能しない(設計ドキュメント参照:
    docs/agent_wrapper_sdk_integration_plan.md)。claude/codexへの本統合は
    SDK/MCPベースの別実装(agent_wrapper/runners/)が担う。
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
        self._deny_count = 0

    def start(self) -> threading.Thread:
        self.state.set_running()
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
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                line = line.rstrip("\n")
                self.state.append_log(line)
                self._handle_line(line)
            proc.wait()
            if proc.returncode == 0:
                # 却下してもプロセス自体は継続しうるので「中断」とは断定せず、
                # 却下があった事実だけを正常完了の詳細に添える
                complete_detail = "プロセスが正常終了しました"
                if self._deny_count:
                    complete_detail += f"(人間の却下 {self._deny_count}件を含む)"
                self.state.set_stopped("normal_complete", complete_detail)
            else:
                self.state.set_stopped(
                    "error", f"プロセスが終了コード {proc.returncode} で終了しました"
                )
        except Exception as e:
            self.state.append_log(f"(read loop error: {e})")
            self.state.set_stopped("error", str(e))
        finally:
            self._stop_flag.set()  # 定期チェックインスレッドも停止(ゾンビ承認防止)
            label = self.state.stop_reason_label()
            detail = self.state.snapshot().get("stop_reason_detail")
            self.notifier(
                "high", "エージェント終了", f"{label}: {detail or ''}".strip()
            )

    def _handle_line(self, line: str) -> None:
        match = rules.check_destructive(line)
        if match.matched:
            classification = ollama_client.classify_destructive_match(line)
            reason = f"{classification['headline']} / {match.reason}"
            self.notifier("high", "削除系の操作を検知", reason)
            self.state.append_log(
                format_audit_log("rules", "エスカレーション", line, match.reason)
            )
            self._wait_for_approval("destructive", reason, detail=line)
            return

        kind, content = rules.parse_agent_signal(line)
        if kind == "stop":
            reason = content or "エージェントが停止を要求"
            self.notifier("high", "エージェントが停止を要求", reason)
            self.state.append_log(
                format_audit_log("rules", "エスカレーション", line, reason)
            )
            self._wait_for_approval("stop_request", reason, detail=line)
        elif kind == "permission":
            judged = ollama_client.judge_permission(
                content or "", model=self.ollama_model
            )
            if judged["decision"] == "allow":
                self._send_to_stdin("y\n")
                self.state.append_log(
                    format_audit_log(
                        "ollama",
                        "自動承認",
                        content or "",
                        str(judged.get("raw", "")),
                    )
                )
            else:
                self.state.append_log(
                    format_audit_log(
                        "ollama",
                        "エスカレーション",
                        content or "",
                        str(judged.get("raw", "")),
                    )
                )
                reason = ollama_client.explain_operation(
                    content or "権限確認", model=self.ollama_model
                )
                self.notifier("medium", "権限確認(要判断)", reason)
                self._wait_for_approval("permission_escalated", reason, detail=line)

    def _checkin_loop(self) -> None:
        while not self._stop_flag.is_set():
            # エージェント終了時(_read_loopのfinallyで_stop_flagが立つ)に即座に
            # 抜けられるよう、sleepではなくイベント待ちにする。終了後もチェックインが
            # 承認待ちを生み続け、死んだプロセスへの承認(stdin書き込み失敗)を誘発する
            # ゾンビ化バグが2026-07-11の実機で発生した
            self._stop_flag.wait(timeout=self.checkin_interval_sec)
            if self._stop_flag.is_set() or self.state.snapshot()["status"] == "stopped":
                return
            recent = "\n".join(self.state.tail(50))
            if not recent.strip():
                continue
            summary = ollama_client.summarize_chunk(recent, model=self.ollama_model)
            self.state.set_summary(summary)
            self.notifier("low", "定期チェックイン", summary)

            if ollama_client.judge_is_major_decision(recent, model=self.ollama_model):
                self.notifier("medium", "方針決定っぽい相談を検知", summary)
                self.state.append_log(
                    format_audit_log("ollama", "エスカレーション", recent, summary)
                )
                self._wait_for_approval("major_decision", summary, detail=recent)

    def _wait_for_approval(
        self, kind: str, reason: str, detail: str = ""
    ) -> HumanResponse:
        event = threading.Event()
        response_holder: list[HumanResponse] = []

        def on_response(response: HumanResponse) -> None:
            response_holder.append(response)
            event.set()

        self.state.set_waiting(kind, reason, on_response, detail=detail)
        event.wait()
        return response_holder[0]

    def _send_to_stdin(self, text: str) -> None:
        try:
            assert (
                self.state.process is not None and self.state.process.stdin is not None
            )
            self.state.process.stdin.write(text)
            self.state.process.stdin.flush()
        except Exception as e:
            self.state.append_log(f"(stdin書き込み失敗: {e})")

    def respond(
        self,
        action: HumanAction,
        message: str = "",
        request_id: int | None = None,
    ) -> None:
        response = HumanResponse(action=action, message=message.strip())
        resolved = self.state.respond(response, request_id=request_id)
        if resolved is None:
            return
        suffix = f": {response.message}" if response.message else ""
        self.state.append_log(f"(人間が{response.log_label()}しました{suffix})")
        target = resolved.detail or resolved.reason
        self.state.append_log(
            format_audit_log(
                "人間",
                response.log_label(),
                target,
                response.message or resolved.reason,
            )
        )
        self._send_to_stdin(response.to_agent_text() + "\n")
        if action == "deny":
            self._deny_count += 1

    def approve(self) -> None:
        self.respond("approve")

    def deny(self) -> None:
        self.respond("deny")

    def stop(self) -> None:
        self._stop_flag.set()
        if self.state.process:
            self.state.process.terminate()
        self.state.set_stopped("terminated", "stop() が呼ばれました")
