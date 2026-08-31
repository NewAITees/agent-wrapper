"""外部harnessの構造化承認イベントをApprovalRequestInputへ正規化する境界。"""

from typing import Protocol

from .approval import ApprovalRequestInput


class ApprovalAdapter(Protocol):
    def adapt(self, event: dict[str, object]) -> ApprovalRequestInput: ...


class OpenCodePermissionAdapter:
    """OpenCode V2 permission evaluation/askedイベント用の純粋変換。

    beta APIとの通信・購読はこのクラスの責務に含めない。バージョン固定後のtransportが
    受け取ったeventをここへ渡すことで、Broker本体をAPI変更から隔離する。
    """

    def __init__(self, session_id: str, harness: str = "opencode") -> None:
        self.session_id = session_id
        self.harness = harness

    def adapt(self, event: dict[str, object]) -> ApprovalRequestInput:
        action = str(event.get("action", ""))
        raw_resources = event.get("resources", [])
        resources = (
            [str(item) for item in raw_resources]
            if isinstance(raw_resources, list)
            else [str(raw_resources)]
        )
        if not action or not resources:
            raise ValueError("OpenCode permission event requires action and resources")
        return ApprovalRequestInput(
            session_id=self.session_id,
            harness=self.harness,
            kind="permission",
            action=action,
            resource="\n".join(resources),
            reason=str(event.get("message", "OpenCode permission request")),
            metadata={"source_event": dict(event)},
        )
