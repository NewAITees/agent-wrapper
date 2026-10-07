import asyncio
import unittest
from unittest import mock

from agent_wrapper.runners.claude_runner import ClaudeRunner
from agent_wrapper.server import SessionManager, WrappedSession


class ClaudeInputQueueTests(unittest.IsolatedAsyncioTestCase):
    def session(self, name="worker"):
        manager = SessionManager()
        session = WrappedSession(
            name, "claude", "", "", ".", manager.shared_state(name, "claude")
        )
        session.state.set_running()
        session.state.set_approved_plan("approved")
        runner = ClaudeRunner(
            "task",
            ".",
            session.state,
            input_queue=session.input_queue,
            idle_wait_seconds=0.03 if name == "orchestrator" else 0,
        )
        session._runner = runner
        return session, runner

    async def test_fifo_instructions_precede_automatic_reply(self):
        session, runner = self.session()
        session.submit("first\r")
        session.submit("second\r")
        client = mock.Mock(query=mock.AsyncMock())
        with (
            mock.patch.object(runner, "_collect_turn", mock.AsyncMock(return_value=[])),
            mock.patch.object(runner, "_read_turn", return_value="done"),
            mock.patch.object(
                runner, "_conversational_reply", mock.AsyncMock(return_value=None)
            ) as reply,
        ):
            await runner._converse(client)
        self.assertEqual(
            client.query.call_args_list,
            [mock.call("[配信者の指示] first"), mock.call("[配信者の指示] second")],
        )
        reply.assert_awaited_once()
        self.assertIn("[人間指示] first", session.state.full_log_text())

    async def test_approval_wait_holds_instruction_until_resolved(self):
        session, runner = self.session()
        client = mock.Mock(query=mock.AsyncMock())

        async def collect(_client):
            await runner._wait_for_approval("permission_escalated", "approve?")
            return []

        with (
            mock.patch.object(runner, "_collect_turn", side_effect=collect),
            mock.patch.object(runner, "_read_turn", return_value="done"),
            mock.patch.object(
                runner, "_conversational_reply", mock.AsyncMock(return_value=None)
            ),
        ):
            task = asyncio.create_task(runner._converse(client))
            try:
                await asyncio.sleep(0)
                session.submit("extra\r")
                await asyncio.sleep(0)
                client.query.assert_not_called()
                runner.respond("approve")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(
                    client.query.call_args, mock.call("[配信者の指示] extra")
                )
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_only_orchestrator_waits_and_resumes(self):
        for name in ("worker", "orchestrator"):
            session, runner = self.session(name)
            client = mock.Mock(query=mock.AsyncMock())
            with (
                mock.patch.object(
                    runner, "_collect_turn", mock.AsyncMock(return_value=[])
                ),
                mock.patch.object(runner, "_read_turn", return_value="done"),
                mock.patch.object(
                    runner, "_conversational_reply", mock.AsyncMock(return_value=None)
                ),
            ):
                task = asyncio.create_task(runner._converse(client))
                await asyncio.sleep(0.005)
                if name == "worker":
                    self.assertTrue(task.done())
                else:
                    self.assertFalse(task.done())
                    session.submit("resume\r")
                    await asyncio.wait_for(task, 0.5)
                    client.query.assert_awaited_once_with("[配信者の指示] resume")

    async def test_stop_cancels_idle_immediately(self):
        session, runner = self.session("orchestrator")
        runner.idle_wait_seconds = 1800

        async def run():
            await runner._converse(mock.Mock(query=mock.AsyncMock()))

        with (
            mock.patch.object(runner, "_run_async", side_effect=run),
            mock.patch.object(runner, "_collect_turn", mock.AsyncMock(return_value=[])),
            mock.patch.object(runner, "_read_turn", return_value="done"),
            mock.patch.object(
                runner, "_conversational_reply", mock.AsyncMock(return_value=None)
            ),
        ):
            task = asyncio.create_task(runner._run_main())
            await asyncio.sleep(0.005)
            self.assertFalse(task.done())
            runner.stop()
            await asyncio.wait_for(task, 0.2)
        self.assertEqual(session.state.snapshot()["stop_reason"], "terminated")

    async def test_mock_sdk_receives_initial_task_then_instruction_and_closes_queue(
        self,
    ):
        session, runner = self.session("orchestrator")
        session.submit("next\r")
        client = mock.MagicMock()
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        client.query = mock.AsyncMock()

        async def response():
            from claude_agent_sdk.types import AssistantMessage, TextBlock

            yield AssistantMessage(content=[TextBlock(text="done")], model="mock")

        client.receive_response.side_effect = response
        with (
            mock.patch(
                "agent_wrapper.runners.claude_runner.ClaudeSDKClient",
                return_value=client,
            ),
            mock.patch.object(
                runner, "_conversational_reply", mock.AsyncMock(return_value=None)
            ),
        ):
            await runner._run_main()
        self.assertEqual(
            client.query.call_args_list,
            [mock.call(runner.initial_prompt), mock.call("[配信者の指示] next")],
        )
        self.assertEqual(session.state.snapshot()["stop_reason"], "normal_complete")
        with self.assertRaisesRegex(ValueError, "stopped"):
            session.input_queue.put("after completion")

    async def test_plan_approval_holds_new_instruction(self):
        session, runner = self.session()
        session.state.set_approved_plan(None)
        client = mock.Mock(query=mock.AsyncMock())
        with (
            mock.patch.object(runner, "_collect_turn", mock.AsyncMock(return_value=[])),
            mock.patch.object(runner, "_read_turn", return_value="plan"),
            mock.patch.object(
                runner, "_conversational_reply", mock.AsyncMock(return_value=None)
            ),
        ):
            task = asyncio.create_task(runner._converse(client))
            await asyncio.sleep(0)
            session.submit("revise\r")
            client.query.assert_not_called()
            runner.respond("approve")
            await asyncio.wait_for(task, 0.5)
        # 計画承認の返答を失わず、指示と1つのメッセージにまとめて渡す。
        client.query.assert_awaited_once_with(
            "y(計画を承認します)\n\n[配信者の指示] revise"
        )


class ApprovalHoldMutationTests(unittest.TestCase):
    def test_bypassing_approval_wait_is_detected(self):
        from agent_wrapper.wrapper import HumanResponse

        case = ClaudeInputQueueTests(
            "test_approval_wait_holds_instruction_until_resolved"
        )
        result = unittest.TestResult()
        with mock.patch.object(
            ClaudeRunner,
            "_wait_for_approval",
            mock.AsyncMock(return_value=HumanResponse("approve")),
        ):
            case.run(result)
        self.assertTrue(result.failures)
        self.assertFalse(result.errors)
