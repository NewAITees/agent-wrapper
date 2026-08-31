import datetime
import unittest

from agent_wrapper.approval import (
    ApprovalBroker,
    ApprovalDecision,
    ApprovalRequestInput,
)
from agent_wrapper.approval_adapters import OpenCodePermissionAdapter


class ApprovalBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ApprovalBroker()

    def test_keeps_permission_and_specification_requests_distinct(self):
        permission = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="claude",
                kind="permission",
                action="shell",
                resource="uv run pytest",
                reason="テスト実行",
            )
        )
        specification = self.broker.submit(
            ApprovalRequestInput(
                session_id="planner-1",
                harness="codex",
                kind="specification_question",
                action="choose",
                resource="認証方式A/B",
                reason="仕様判断",
            )
        )

        self.assertEqual(permission.kind, "permission")
        self.assertEqual(specification.kind, "specification_question")
        self.assertNotEqual(permission.request_id, specification.request_id)

    def test_response_is_scoped_to_session_and_request(self):
        request = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="claude",
                kind="permission",
                action="edit",
                resource="src/app.py",
                reason="計画内編集",
            )
        )

        self.assertIsNone(
            self.broker.respond(
                "worker-2", request.request_id, ApprovalDecision("approve")
            )
        )
        self.assertEqual(len(self.broker.pending("worker-1")), 1)

        resolved = self.broker.respond(
            "worker-1", request.request_id, ApprovalDecision("approve")
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(self.broker.pending("worker-1"), [])

    def test_duplicate_response_is_rejected(self):
        request = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="claude",
                kind="permission",
                action="edit",
                resource="src/app.py",
                reason="計画内編集",
            )
        )
        decision = ApprovalDecision("approve")

        self.assertIsNotNone(
            self.broker.respond("worker-1", request.request_id, decision)
        )
        self.assertIsNone(self.broker.respond("worker-1", request.request_id, decision))

    def test_always_is_not_a_valid_broker_decision(self):
        with self.assertRaises(ValueError):
            ApprovalDecision("always")  # type: ignore[arg-type]

    def test_expired_request_cannot_be_approved(self):
        request = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="opencode",
                kind="permission",
                action="shell",
                resource="git status",
                reason="状態確認",
                expires_at=datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(seconds=1),
            )
        )

        self.assertIsNone(
            self.broker.respond(
                "worker-1", request.request_id, ApprovalDecision("approve")
            )
        )
        self.assertEqual(request.status, "expired")

    def test_pending_lists_all_sessions_in_creation_order(self):
        first = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="claude",
                kind="permission",
                action="edit",
                resource="a.py",
                reason="A",
            )
        )
        second = self.broker.submit(
            ApprovalRequestInput(
                session_id="worker-2",
                harness="codex",
                kind="permission",
                action="shell",
                resource="pytest",
                reason="B",
            )
        )

        self.assertEqual(
            [item.request_id for item in self.broker.pending()],
            [first.request_id, second.request_id],
        )


class OpenCodePermissionAdapterTests(unittest.TestCase):
    def test_converts_structured_event_without_transport_dependency(self):
        adapted = OpenCodePermissionAdapter("worker-1").adapt(
            {
                "action": "shell",
                "resources": ["git status", "C:/work"],
                "message": "Run command?",
            }
        )

        self.assertEqual(adapted.session_id, "worker-1")
        self.assertEqual(adapted.kind, "permission")
        self.assertEqual(adapted.action, "shell")
        self.assertEqual(adapted.resource, "git status\nC:/work")

    def test_rejects_unstructured_event(self):
        with self.assertRaises(ValueError):
            OpenCodePermissionAdapter("worker-1").adapt({"message": "missing"})


if __name__ == "__main__":
    unittest.main()
