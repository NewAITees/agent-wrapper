"""WebSocket(ブラウザのターミナル)からの入力は、従来どおり稼働中は無視する。

HTTP /send だけが追加指示のキューに積む。キー入力が1文字ずつ指示になったり、
空のEnterで例外が出てWebSocket処理が落ちたりしないことを、本物のWrappedSessionで確認する。
"""

import unittest

from agent_wrapper.server import SessionManager, WrappedSession


def running_session() -> WrappedSession:
    manager = SessionManager()
    session = WrappedSession(
        "orchestrator",
        "claude",
        "",
        "",
        "C:/tmp",
        manager.shared_state("orchestrator", "claude"),
    )
    session._runner = object()  # type: ignore[assignment]
    return session


class WebSocketInputTests(unittest.TestCase):
    def test_keystrokes_while_running_are_ignored_not_queued(self):
        session = running_session()
        for key in ["a", "b", "\r", "\x7f", "\n"]:
            session.write(key)
        assert session.input_queue is not None
        self.assertIsNone(session.input_queue.take_nowait())

    def test_empty_enter_while_running_does_not_raise(self):
        session = running_session()
        session.write("\r")

    def test_submit_queues_while_running_and_rejects_empty(self):
        session = running_session()
        result = session.submit("追加の指示")
        self.assertEqual(result["status"], "queued")
        with self.assertRaises(ValueError):
            session.submit("   ")

    def test_keystrokes_before_start_still_buffer_into_the_first_task(self):
        manager = SessionManager()
        session = WrappedSession(
            "worker",
            "claude",
            "",
            "",
            "C:/tmp",
            manager.shared_state("worker", "claude"),
        )
        session.write("hel")
        session.write("lo")
        self.assertEqual(session._input, "hello")


class IdleWaitWiringTests(unittest.TestCase):
    """idle待機(追加指示を待ち続ける)はorchestratorだけ。他のセッションは従来どおり終了する。"""

    def _runner_kwargs(self, name: str) -> dict[str, object]:
        from unittest import mock

        manager = SessionManager()
        session = WrappedSession(
            name, "claude", "", "", "C:/tmp", manager.shared_state(name, "claude")
        )
        with mock.patch("agent_wrapper.server.ClaudeRunner") as runner_type:
            session.submit("task\r")
        return dict(runner_type.call_args.kwargs)

    def test_only_role_sessions_wait_for_more_instructions(self):
        from agent_wrapper.input_queue import IDLE_WAIT_SECONDS

        for name in ("orchestrator", "worker", "planner", "tester", "reviewer"):
            self.assertEqual(
                self._runner_kwargs(name)["idle_wait_seconds"], IDLE_WAIT_SECONDS
            )
        # 名前が未知の汎用セッションは、作業が終われば従来どおり終了する。
        self.assertEqual(self._runner_kwargs("custom-helper")["idle_wait_seconds"], 0)


class MultipleEnterAndWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def test_multiple_enters_in_the_first_input_do_not_start_two_runners(self):
        from unittest import mock

        manager = SessionManager()
        session = WrappedSession(
            "worker",
            "claude",
            "",
            "",
            "C:/tmp",
            manager.shared_state("worker", "claude"),
        )
        with mock.patch("agent_wrapper.server.ClaudeRunner") as runner_type:
            with self.assertRaises(ValueError):
                session.submit("first" + chr(13) + "second" + chr(13))
        runner_type.assert_not_called()

    async def test_handle_ws_ignores_input_while_running_and_survives_empty_enter(self):
        from agent_wrapper.server import _handle_ws

        session = running_session()

        class FakeWebSocket:
            def __init__(self, messages):
                self._messages = messages

            def __aiter__(self):
                return self._iterate()

            async def _iterate(self):
                for message in self._messages:
                    yield message

        await _handle_ws(FakeWebSocket(["a", "b", chr(13), b"c"]), session)
        assert session.input_queue is not None
        self.assertIsNone(session.input_queue.take_nowait())

    def test_orchestrator_prompt_says_prefix_inside_tool_results_is_not_authoritative(
        self,
    ):
        from agent_wrapper.server import ROLE_SYSTEM_PROMPTS

        prompt = ROLE_SYSTEM_PROMPTS["orchestrator"]
        self.assertIn("ツール結果", prompt)
        self.assertIn("配信者の指示ではない", prompt)


if __name__ == "__main__":
    unittest.main()
