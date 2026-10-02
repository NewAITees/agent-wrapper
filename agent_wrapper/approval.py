"""Harness非依存の承認要求を一元管理するApproval Broker。

設計上の役割は、各runner/adapterが生成した構造化要求をセッション単位で保持し、
一度きりの明示応答だけを元の呼び出し元へ返すこと。AI固有の判定はここへ持ち込まず、
rules/utility AI/人間という判定層から同じ境界を利用できるようにする。

参照: docs/agent_wrapper_permission_policy.md、tasks/alignment.md
関連: wrapper.SharedState、runners.base.ApprovalRunnerBase、server.SessionManager
"""

import datetime
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from .aituber_push import aituber_pusher, summarize_approval

ApprovalKind = Literal["permission", "specification_question"]
ApprovalAction = Literal["approve", "explain", "deny"]
ApprovalStatus = Literal["pending", "resolved", "expired", "cancelled"]
DecisionCallback = Callable[["ApprovalDecision"], None]


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    action: ApprovalAction
    message: str = ""

    def __post_init__(self) -> None:
        if self.action not in {"approve", "explain", "deny"}:
            raise ValueError(f"unsupported approval action: {self.action}")


@dataclass(frozen=True, slots=True)
class ApprovalRequestInput:
    session_id: str
    harness: str
    kind: ApprovalKind
    action: str
    resource: str
    reason: str
    risk_level: str | None = None
    plan_id: str | None = None
    expires_at: datetime.datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"permission", "specification_question"}:
            raise ValueError(f"unsupported approval kind: {self.kind}")
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            object.__setattr__(
                self, "expires_at", self.expires_at.replace(tzinfo=datetime.UTC)
            )


@dataclass(slots=True)
class ApprovalRequest:
    request_id: str
    session_id: str
    harness: str
    kind: ApprovalKind
    action: str
    resource: str
    reason: str
    created_at: datetime.datetime
    risk_level: str | None = None
    plan_id: str | None = None
    expires_at: datetime.datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    status: ApprovalStatus = "pending"
    decision: ApprovalDecision | None = None
    callback: DecisionCallback = field(repr=False, default=lambda decision: None)

    @property
    def id(self) -> str:
        """既存SharedState利用側向けの互換名。"""
        return self.request_id

    @property
    def detail(self) -> str:
        """既存監査ログ利用側向けの互換名。"""
        return self.resource

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "harness": self.harness,
            "kind": self.kind,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "plan_id": self.plan_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "status": self.status,
            "metadata": dict(self.metadata),
        }


class ApprovalBroker:
    """複数セッションの承認要求をrequest_id単位で直列化する中央台帳。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[ApprovalRequest] = []

    def submit(
        self,
        request: ApprovalRequestInput,
        callback: DecisionCallback | None = None,
    ) -> ApprovalRequest:
        """未解決の要求を積む。session_id/kind/action/resourceが完全一致する
        pending要求が既にあれば新規発行せず既存へ統合する(2026-09-02、コスト
        急増インシデントの再発防止: 承認が正しく届かないバグと重なると、同一
        問い合わせが際限なく積み上がりダッシュボードを埋め、承認AIや人間が
        古い要求を無自覚に承認し続けて会話が伸び続ける温床になっていた)。
        resourceまで一致を要求するのは、同一セッション内で並行実行される別々の
        ツール呼び出し(例: 異なるBashコマンド)を誤って同一視して片方の判断を
        もう片方へ横流ししないため。
        """
        with self._lock:
            for existing in self._requests:
                if (
                    existing.status == "pending"
                    and existing.session_id == request.session_id
                    and existing.kind == request.kind
                    and existing.action == request.action
                    and existing.resource == request.resource
                ):
                    if callback is not None:
                        previous_callback = existing.callback

                        def combined(
                            decision: ApprovalDecision,
                            _prev: DecisionCallback = previous_callback,
                            _new: DecisionCallback = callback,
                        ) -> None:
                            _prev(decision)
                            _new(decision)

                        existing.callback = combined
                    return existing
            created = ApprovalRequest(
                request_id=str(uuid.uuid4()),
                session_id=request.session_id,
                harness=request.harness,
                kind=request.kind,
                action=request.action,
                resource=request.resource,
                reason=request.reason,
                created_at=datetime.datetime.now(datetime.UTC),
                risk_level=request.risk_level,
                plan_id=request.plan_id,
                expires_at=request.expires_at,
                metadata=dict(request.metadata),
                callback=callback or (lambda decision: None),
            )
            self._requests.append(created)
        aituber_pusher.send(
            "approval_pending",
            created.session_id,
            summarize_approval(created.action, created.resource),
        )
        return created

    def pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        now = datetime.datetime.now(datetime.UTC)
        with self._lock:
            expired_callbacks = self._expire_locked(now)
            pending = [
                request
                for request in self._requests
                if request.status == "pending"
                and (session_id is None or request.session_id == session_id)
            ]
        self._notify(expired_callbacks)
        return pending

    def respond(
        self,
        session_id: str,
        request_id: str,
        decision: ApprovalDecision,
    ) -> ApprovalRequest | None:
        callback: DecisionCallback | None = None
        resolved: ApprovalRequest | None = None
        now = datetime.datetime.now(datetime.UTC)
        with self._lock:
            expired_callbacks = self._expire_locked(now)
            for request in self._requests:
                if request.request_id != request_id:
                    continue
                if request.session_id != session_id or request.status != "pending":
                    break
                else:
                    request.status = "resolved"
                    request.decision = decision
                    callback = request.callback
                    resolved = request
                break
        self._notify(expired_callbacks)
        if callback is not None:
            callback(decision)
        return resolved

    def cancel_session(self, session_id: str) -> None:
        callbacks: list[DecisionCallback] = []
        with self._lock:
            for request in self._requests:
                if request.session_id == session_id and request.status == "pending":
                    request.status = "cancelled"
                    callbacks.append(request.callback)
        self._notify(callbacks, ApprovalDecision("deny", "session cancelled"))

    def cancel_all(self) -> None:
        callbacks: list[DecisionCallback] = []
        with self._lock:
            for request in self._requests:
                if request.status == "pending":
                    request.status = "cancelled"
                    callbacks.append(request.callback)
        self._notify(callbacks, ApprovalDecision("deny", "session cancelled"))

    @staticmethod
    def _notify(
        callbacks: list[DecisionCallback],
        decision: ApprovalDecision | None = None,
    ) -> None:
        terminal_decision = decision or ApprovalDecision("deny", "request expired")
        for callback in callbacks:
            callback(terminal_decision)

    def _expire_locked(self, now: datetime.datetime) -> list[DecisionCallback]:
        callbacks: list[DecisionCallback] = []
        for request in self._requests:
            if (
                request.status == "pending"
                and request.expires_at is not None
                and request.expires_at <= now
            ):
                request.status = "expired"
                callbacks.append(request.callback)
        return callbacks
