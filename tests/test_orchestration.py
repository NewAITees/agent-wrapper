import unittest
from unittest import mock

from agent_wrapper.orchestration import (
    create_orchestration_server,
    list_sessions,
    read_session_output,
    send_to_session,
)
from agent_wrapper.runners.claude_runner import ClaudeRunner, _GATED_MATCHER
from agent_wrapper.runners.tool_describe import describe_tool_call
from agent_wrapper.server import ROLE_SYSTEM_PROMPTS, SessionManager, WrappedSession
from agent_wrapper.wrapper import SharedState
from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext


class FakeSession:
    def __init__(self, name, harness="claude", mode="wrapped", alive=True, runner=None):
        self.name = name
        self.harness = harness
        self.mode = mode
        self.alive = alive
        self._runner = runner
        self.writes = []
        self.output = "session output"

    def is_alive(self):
        return self.alive

    def write(self, text):
        self.writes.append(text)

    def output_text(self, tail=None):
        return self.output[-tail:] if tail else self.output


class FakeManager:
    def __init__(self, sessions):
        self.sessions = {session.name: session for session in sessions}


class OrchestrationToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = FakeSession("worker")
        self.raw = FakeSession("raw", harness="codex", mode="raw-pty")
        self.manager = FakeManager([self.worker, self.raw])

    async def test_list_sessions_excludes_orchestrator_and_reports_input_state(self):
        self.manager.sessions["orchestrator"] = FakeSession("orchestrator")
        result = await list_sessions(self.manager, "orchestrator")
        self.assertEqual([item["name"] for item in result], ["worker", "raw"])
        self.assertTrue(result[0]["accepts_input"])
        self.assertFalse(result[1]["accepts_input"])

    async def test_send_to_wrapped_session_before_runner_start(self):
        result = await send_to_session(
            self.manager, "orchestrator", "worker", "do task"
        )
        self.assertIn("success", result.lower())
        self.assertEqual(self.worker.writes, ["do task\r"])

    async def test_send_to_raw_pty_is_rejected_because_it_bypasses_the_broker(self):
        result = await send_to_session(self.manager, "orchestrator", "raw", "y")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(self.raw.writes, [])

    async def test_send_to_session_with_unknown_mode_is_rejected(self):
        class NoMode:
            harness = "claude"

            def __init__(self):
                self.writes = []

            def is_alive(self):
                return True

            def write(self, text):
                self.writes.append(text)

        unknown = NoMode()
        manager = FakeManager([])
        manager.sessions["mystery"] = unknown
        result = await send_to_session(manager, "orchestrator", "mystery", "task")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(unknown.writes, [])
        listed = await list_sessions(manager, "orchestrator")
        self.assertFalse(listed[0]["accepts_input"])

    async def test_send_accepts_4000_character_instruction(self):
        text = "x" * 4000
        result = await send_to_session(self.manager, "orchestrator", "worker", text)
        self.assertIn("success", result.lower())
        self.assertEqual(self.worker.writes, [text + "\r"])

    async def test_send_rejects_missing_self_empty_and_overlong_text(self):
        for target, text in [
            ("missing", "task"),
            ("orchestrator", "task"),
            ("worker", ""),
        ]:
            with self.subTest(target=target, text=text):
                result = await send_to_session(
                    self.manager, "orchestrator", target, text
                )
                self.assertIn("error", result.lower())
        result = await send_to_session(
            self.manager, "orchestrator", "worker", "x" * 5000
        )
        self.assertIn("error", result.lower())
        self.assertEqual(self.worker.writes, [])

    async def test_send_rejects_dead_and_already_started_wrapped_session(self):
        self.worker._runner = object()
        self.assertFalse(
            (await list_sessions(self.manager, "orchestrator"))[0]["accepts_input"]
        )
        result = await send_to_session(self.manager, "orchestrator", "worker", "more")
        self.assertIn("すでに作業中", result)
        self.assertEqual(self.worker.writes, [])
        self.worker._runner = None
        self.worker.alive = False
        result = await send_to_session(self.manager, "orchestrator", "worker", "more")
        self.assertIn("alive", result.lower())

    async def test_read_returns_bounded_tail_and_rejects_self(self):
        self.worker.output = "0123456789"
        self.assertEqual(
            await read_session_output(self.manager, "orchestrator", "worker", 4), "6789"
        )
        self.assertIn(
            "error",
            (
                await read_session_output(
                    self.manager, "orchestrator", "orchestrator", 4
                )
            ).lower(),
        )
        self.worker.output = "x" * 5000
        self.assertEqual(
            len(
                await read_session_output(self.manager, "orchestrator", "worker", 5000)
            ),
            4000,
        )

    async def test_sdk_server_exposes_three_tools(self):
        with mock.patch(
            "agent_wrapper.orchestration.create_sdk_mcp_server",
            side_effect=lambda name, tools: {"name": name, "tools": tools},
        ):
            config = create_orchestration_server(self.manager, "orchestrator")
        self.assertEqual(config["name"], "orchestration")
        self.assertEqual(
            {item.name for item in config["tools"]},
            {"list_sessions", "send_to_session", "read_session_output"},
        )


class OrchestrationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_orchestrator_claude_wrapped_gets_mcp_server(self):
        manager = SessionManager()
        with (
            mock.patch("agent_wrapper.server.remember_folder"),
            mock.patch(
                "agent_wrapper.server.create_orchestration_server", create=True
            ) as create_server,
            mock.patch("agent_wrapper.server.Session"),
            mock.patch(
                "agent_wrapper.server.asyncio.create_task",
                side_effect=lambda coro: (coro.close(), mock.Mock())[1],
            ),
        ):
            await manager.start(
                ".",
                [
                    {"name": "orchestrator", "harness": "claude", "mode": "wrapped"},
                    {"name": "planner", "harness": "claude", "mode": "wrapped"},
                    {"name": "other", "harness": "codex", "mode": "wrapped"},
                    {"name": "terminal", "harness": "claude", "mode": "raw-pty"},
                ],
            )
        self.assertEqual(create_server.call_count, 1)
        self.assertIsNotNone(manager.sessions["orchestrator"].mcp_servers)
        self.assertIsNone(manager.sessions["planner"].mcp_servers)
        self.assertIsNone(manager.sessions["other"].mcp_servers)

    async def test_claude_runner_passes_optional_mcp_servers(self):
        mcp_servers = {"orchestration": object()}
        runner = ClaudeRunner(
            prompt="test", cwd=".", state=SharedState(), mcp_servers=mcp_servers
        )
        client = mock.AsyncMock()
        client.__aenter__.return_value = client
        with (
            mock.patch(
                "agent_wrapper.runners.claude_runner.ClaudeAgentOptions"
            ) as options,
            mock.patch(
                "agent_wrapper.runners.claude_runner.ClaudeSDKClient",
                return_value=client,
            ),
            mock.patch.object(runner, "_converse", new=mock.AsyncMock()),
        ):
            await runner._run_async()
        self.assertIs(options.call_args.kwargs["mcp_servers"], mcp_servers)

    def test_system_prompt_names_tools_and_initial_input_limit(self):
        prompt = ROLE_SYSTEM_PROMPTS["orchestrator"]
        for tool_name in ("list_sessions", "send_to_session", "read_session_output"):
            self.assertIn(tool_name, prompt)
        self.assertIn("未起動", prompt)

    async def test_mcp_send_uses_three_layer_gate_and_describes_recipient_and_text(
        self,
    ):
        tool_name = "mcp__orchestration__send_to_session"
        call_input = {"name": "worker", "text": "update the parser"}
        self.assertIn(tool_name, _GATED_MATCHER)
        description = describe_tool_call(tool_name, call_input)
        self.assertIn("worker", description)
        self.assertIn("update the parser", description)

        runner = ClaudeRunner(prompt="test", cwd=".", state=SharedState())
        with mock.patch.object(
            runner,
            "_gate",
            new=mock.AsyncMock(return_value=mock.Mock(action="approve")),
        ) as gate:
            result = await runner._can_use_tool(
                tool_name, call_input, ToolPermissionContext()
            )
        self.assertIsInstance(result, PermissionResultAllow)
        gate.assert_awaited_once()
        self.assertEqual(gate.await_args.args[0], description)


if __name__ == "__main__":
    unittest.main()


class RealWrappedSessionTests(unittest.IsolatedAsyncioTestCase):
    """FakeSessionではなく本物のWrappedSessionで、起動済み判定が効くことを確認する。"""

    def _manager_with_running_worker(self):
        manager = SessionManager()
        worker = WrappedSession(
            "worker",
            "claude",
            "",
            "",
            "C:/tmp",
            manager.shared_state("worker", "claude"),
        )
        worker._runner = object()
        manager.sessions["worker"] = worker
        return manager, worker

    async def test_list_sessions_reports_running_wrapped_as_not_accepting_input(self):
        manager, _ = self._manager_with_running_worker()
        result = await list_sessions(manager, "orchestrator")
        self.assertEqual(result[0]["mode"], "wrapped")
        self.assertFalse(result[0]["accepts_input"])

    async def test_send_to_running_real_wrapped_session_is_rejected(self):
        manager, _ = self._manager_with_running_worker()
        result = await send_to_session(manager, "orchestrator", "worker", "追加指示")
        self.assertTrue(result.startswith("Error"))
