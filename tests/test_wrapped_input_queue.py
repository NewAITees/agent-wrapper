"""本物のwrappedセッションで追加入力の境界と寿命を検証する。"""

import json
import unittest
import test_api_token
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from agent_wrapper.server import Session, SessionManager, WrappedSession


class WrappedInputQueueTests(unittest.TestCase):
    def session(self, harness="claude"):
        manager = SessionManager()
        session = WrappedSession(
            "worker", harness, "", "", ".", manager.shared_state("worker", harness)
        )
        manager.sessions["worker"] = session
        runner_path = f"agent_wrapper.server.{'Claude' if harness == 'claude' else 'Codex'}Runner.start"
        with mock.patch(runner_path):
            self.assertEqual(
                session.submit("first\r"), {"ok": True, "status": "started"}
            )
        return session

    def test_running_inputs_queue_in_fifo_order_with_positions(self):
        session = self.session()
        for i in range(5):
            self.assertEqual(
                session.submit(f"next{i}\r"),
                {"ok": True, "status": "queued", "position": i + 1},
            )
        with self.assertRaisesRegex(ValueError, "full"):
            session.submit("overflow\r")
        self.assertEqual(
            [session.input_queue.take_nowait() for _ in range(5)],
            [f"next{i}" for i in range(5)],
        )

    def test_invalid_inputs_do_not_enter_queue(self):
        session = self.session()
        for text in ("\r", "   \r", "x" * 4001 + "\r"):
            with self.assertRaises(ValueError):
                session.submit(text)
        self.assertEqual(session.submit("x" * 4000 + "\r")["position"], 1)

    def test_codex_running_input_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(ValueError, "codex"):
            self.session("codex").submit("next\r")

    def test_stop_rejects_inputs_and_clears_pending_queue(self):
        session = self.session()
        session.submit("next\r")
        with mock.patch("agent_wrapper.server.aituber_pusher.send"):
            session.terminate()
        self.assertIsNone(session.input_queue.take_nowait())
        with self.assertRaisesRegex(ValueError, "stopped"):
            session.submit("later\r")

    def test_completed_session_rejects_inputs(self):
        session = self.session()
        session.state.set_stopped("normal_complete", "done")
        with self.assertRaisesRegex(ValueError, "stopped"):
            session.submit("later\r")

    def test_concurrent_producers_cannot_exceed_capacity(self):
        session = self.session()

        def submit(i):
            try:
                return session.submit(f"item{i}\r")["position"]
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            positions = list(executor.map(submit, range(20)))
        self.assertEqual(sorted(p for p in positions if p is not None), [1, 2, 3, 4, 5])


class QueueMutationTests(unittest.TestCase):
    """実際の回帰テストを変異した実装に適用し、検出できることを確認する。"""

    def test_removing_capacity_is_detected(self):
        with mock.patch("agent_wrapper.input_queue.MAX_INPUTS", 100):
            with self.assertRaises(AssertionError):
                case = WrappedInputQueueTests()
                case.test_running_inputs_queue_in_fifo_order_with_positions()

    def test_removing_length_limit_is_detected(self):
        with mock.patch("agent_wrapper.input_queue.MAX_INPUT_LENGTH", 10000):
            with self.assertRaises(AssertionError):
                WrappedInputQueueTests().test_invalid_inputs_do_not_enter_queue()


class WrappedInputHttpTests(unittest.TestCase):
    request = test_api_token.ApiTokenTests.request

    def setUp(self):
        environment = mock.patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.manager = SessionManager()

    def create_session(self, harness="claude"):
        session = WrappedSession(
            "orchestrator",
            harness,
            "",
            "",
            ".",
            self.manager.shared_state("orchestrator", harness),
        )
        self.manager.sessions["orchestrator"] = session
        return session

    def send(self, text):
        status, response, _ = self.request("/send/orchestrator", text=text)
        return status, json.loads(response.split("\r\n\r\n", 1)[1])

    def start(self, harness="claude"):
        session = self.create_session(harness)
        path = f"agent_wrapper.server.{'Claude' if harness == 'claude' else 'Codex'}Runner.start"
        with mock.patch(path):
            self.assertEqual(
                self.send("first"), (200, {"ok": True, "status": "started"})
            )
        return session

    def test_http_fifo_positions_and_full_conflict(self):
        session = self.start()
        for i in range(5):
            self.assertEqual(
                self.send(f"item{i}"),
                (200, {"ok": True, "status": "queued", "position": i + 1}),
            )
        status, body = self.send("overflow")
        self.assertEqual(status, 409)
        self.assertIn("full", body["error"])
        self.assertEqual(
            [session.input_queue.take_nowait() for _ in range(5)],
            [f"item{i}" for i in range(5)],
        )

    def test_http_empty_and_long_inputs_are_conflicts(self):
        session = self.start()
        for text in ("", " ", "x" * 4001):
            with self.subTest(text_length=len(text)):
                status, body = self.send(text)
                self.assertEqual(status, 409)
                self.assertIn("error", body)
        self.assertIsNone(session.input_queue.take_nowait())

    def test_http_codex_running_input_is_conflict(self):
        self.start("codex")
        status, body = self.send("next")
        self.assertEqual(status, 409)
        self.assertIn("codex", body["error"])

    def test_http_raw_pty_preserves_response_and_input(self):
        with mock.patch("agent_wrapper.server.winpty.PtyProcess.spawn") as spawn:
            session = Session("orchestrator", ["mock"], ".", "claude")
        self.manager.sessions["orchestrator"] = session
        self.assertEqual(self.send("next"), (200, {"ok": True}))
        spawn.return_value.write.assert_called_once_with("next\r")
