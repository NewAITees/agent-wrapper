"""workerなどに承認待ちが届いたら、orchestratorへ通知する(簡単な承認を任せるため)。

本物の ApprovalBroker / SessionManager / WrappedSession を使う。
"""

import unittest
from unittest import mock

from agent_wrapper.approval import ApprovalBroker, ApprovalRequestInput
from agent_wrapper.server import SessionManager, WrappedSession


def make_session(manager: SessionManager, name: str) -> WrappedSession:
    session = WrappedSession(
        name, "claude", "", "", "C:/tmp", manager.shared_state(name, "claude")
    )
    manager.sessions[name] = session
    return session


def running(session: WrappedSession) -> WrappedSession:
    session._runner = object()  # type: ignore[assignment]
    return session


def request(session_id: str, resource: str = "Write a.txt") -> ApprovalRequestInput:
    return ApprovalRequestInput(
        session_id=session_id,
        harness="claude",
        kind="permission",
        action="permission_escalated",
        resource=resource,
        reason="r",
    )


class BrokerListenerTests(unittest.TestCase):
    def test_listener_is_called_once_per_new_request_but_not_for_merged_duplicates(
        self,
    ):
        broker = ApprovalBroker()
        seen = []
        broker.add_listener(seen.append)

        first = broker.submit(request("worker"))
        broker.submit(request("worker"))  # 同一内容は統合される
        broker.submit(request("worker", "Write b.txt"))

        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], first)

    def test_a_failing_listener_does_not_break_submit_or_other_listeners(self):
        broker = ApprovalBroker()
        seen = []

        def broken(_request):
            raise RuntimeError("boom")

        broker.add_listener(broken)
        broker.add_listener(seen.append)

        created = broker.submit(request("worker"))

        self.assertEqual(created.session_id, "worker")
        self.assertEqual(len(seen), 1)


class OrchestratorNoticeTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.manager = SessionManager(clock=lambda: self.now[0])
        self.orchestrator = running(make_session(self.manager, "orchestrator"))
        make_session(self.manager, "worker")

    def notices(self):
        queue = self.orchestrator.input_queue
        items = []
        while (item := queue.take_nowait()) is not None:  # type: ignore[union-attr]
            items.append(item)
        return items

    def test_a_worker_request_notifies_the_running_orchestrator(self):
        self.manager.approval_broker.submit(request("worker"))

        items = self.notices()

        self.assertEqual(len(items), 1)
        self.assertEqual(getattr(items[0], "label", None), "システム通知")
        self.assertIn("worker", items[0])
        self.assertIn("list_pending_approvals", items[0])
        self.assertIn("respond_to_approval", items[0])

    def test_notices_for_the_same_session_are_coalesced_for_a_while(self):
        self.manager.approval_broker.submit(request("worker", "Write a"))
        self.manager.approval_broker.submit(request("worker", "Write b"))
        self.assertEqual(len(self.notices()), 1)

        self.now[0] += 5.0
        self.manager.approval_broker.submit(request("worker", "Write c"))
        self.assertEqual(self.notices(), [])

        self.now[0] += 60.0
        self.manager.approval_broker.submit(request("worker", "Write d"))
        self.assertEqual(len(self.notices()), 1)

    def test_orchestrators_own_requests_and_unknown_sessions_do_not_notify(self):
        self.manager.approval_broker.submit(request("orchestrator"))
        self.manager.approval_broker.submit(request("opencode:external-1"))
        self.assertEqual(self.notices(), [])

    def test_no_notice_when_the_orchestrator_is_not_running(self):
        manager = SessionManager()
        orchestrator = make_session(manager, "orchestrator")
        make_session(manager, "worker")

        manager.approval_broker.submit(request("worker"))

        self.assertIsNone(orchestrator.input_queue.take_nowait())  # type: ignore[union-attr]

    def test_a_failed_notice_does_not_start_the_coalescing_window(self):
        manager = SessionManager(clock=lambda: self.now[0])
        orchestrator = make_session(manager, "orchestrator")  # まだ起動していない
        make_session(manager, "worker")
        manager.approval_broker.submit(request("worker", "Write a"))

        running(orchestrator)
        self.now[0] += 1.0
        manager.approval_broker.submit(request("worker", "Write b"))

        self.assertIsNotNone(orchestrator.input_queue.take_nowait())  # type: ignore[union-attr]

    def test_an_external_broker_also_gets_the_listener(self):
        broker = ApprovalBroker()
        manager = SessionManager(broker)
        orchestrator = running(make_session(manager, "orchestrator"))
        make_session(manager, "worker")

        broker.submit(request("worker"))

        self.assertIsNotNone(orchestrator.input_queue.take_nowait())  # type: ignore[union-attr]

    def test_prompt_tells_the_orchestrator_how_to_react_to_the_notice(self):
        from agent_wrapper.server import ROLE_SYSTEM_PROMPTS

        prompt = ROLE_SYSTEM_PROMPTS["orchestrator"]
        self.assertIn("承認待ちが届きました", prompt)


if __name__ == "__main__":
    unittest.main()
    mock  # noqa: B018  (未使用importの警告を避ける)
