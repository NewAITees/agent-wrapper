import asyncio
from concurrent.futures import ThreadPoolExecutor
import datetime
import http.client
import http.server
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_wrapper.server import (
    ROLE_PERMISSIONS,
    ROLE_SYSTEM_PROMPTS,
    Session,
    SessionManager,
    WrappedSession,
    _make_http_handler,
    add_recent_folder,
    build_argv,
    load_recent_folders,
    opencode_agent_markdown,
    parse_ollama_tags,
    parse_opencode_models,
    parse_pytest_failures,
    remember_folder,
    role_permission_for,
    run_tests,
    system_prompt_for,
    write_opencode_agent_file,
    write_opencode_broker_plugin,
)
from agent_wrapper.approval import ApprovalDecision, ApprovalRequestInput
from agent_wrapper.wrapper import HumanResponse


class ParseOpencodeModelsTests(unittest.TestCase):
    def test_groups_models_by_source(self):
        stdout = "opencode/big-pickle\nollama/gemma4:e4b\nopenai/gpt-5.6-terra\nopenai/gpt-5.6-terra-fast\n"

        result = parse_opencode_models(stdout)

        self.assertEqual(
            result,
            {
                "opencode": ["big-pickle"],
                "ollama": ["gemma4:e4b"],
                "openai": ["gpt-5.6-terra", "gpt-5.6-terra-fast"],
            },
        )

    def test_ignores_lines_without_slash(self):
        stdout = "some warning line\nopenai/gpt-5.6-terra\n"

        result = parse_opencode_models(stdout)

        self.assertEqual(result, {"openai": ["gpt-5.6-terra"]})

    def test_empty_output_yields_empty_dict(self):
        self.assertEqual(parse_opencode_models(""), {})


class ParsePytestFailuresTests(unittest.TestCase):
    def test_extracts_failed_test_ids(self):
        stdout = (
            "..F..\n"
            "FAILED tests/test_x.py::test_a - AssertionError: boom\n"
            "FAILED tests/test_y.py::test_b\n"
            "1 passed, 2 failed"
        )

        self.assertEqual(
            parse_pytest_failures(stdout),
            ["tests/test_x.py::test_a", "tests/test_y.py::test_b"],
        )

    def test_no_failures_returns_empty_list(self):
        self.assertEqual(parse_pytest_failures("5 passed in 0.1s"), [])


class RunTestsTests(unittest.TestCase):
    """テストの「実行」段階が機械的(AI不要)であることを検証する。"""

    def test_successful_command_reports_passed(self):
        result = run_tests(".", command=[sys.executable, "-c", "print('ok')"])

        self.assertTrue(result["passed"])
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["failed_tests"], [])

    def test_failing_command_reports_failed_tests(self):
        script = (
            "print('FAILED tests/test_x.py::test_a - boom'); import sys; sys.exit(1)"
        )
        result = run_tests(".", command=[sys.executable, "-c", script])

        self.assertFalse(result["passed"])
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["failed_tests"], ["tests/test_x.py::test_a"])

    @mock.patch("agent_wrapper.server.subprocess.run")
    def test_timeout_is_reported_without_raising(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["x"], timeout=1, output="partial", stderr="err"
        )

        result = run_tests(".", command=["x"])

        self.assertTrue(result["timed_out"])
        self.assertFalse(result["passed"])
        self.assertIsNone(result["returncode"])

    def test_default_command_is_uv_run_pytest(self):
        result = run_tests(".", command=[sys.executable, "-c", "pass"])

        # commandを明示指定した場合は既定(RUN_TESTS_COMMAND)を上書きできることの確認。
        self.assertEqual(result["command"], [sys.executable, "-c", "pass"])


class ParseOllamaTagsTests(unittest.TestCase):
    def test_extracts_names(self):
        payload = {"models": [{"name": "qwen3.8:27b"}, {"name": "gemma4:e4b"}]}

        self.assertEqual(parse_ollama_tags(payload), ["qwen3.8:27b", "gemma4:e4b"])

    def test_missing_models_key_yields_empty_list(self):
        self.assertEqual(parse_ollama_tags({}), [])


class SystemPromptForTests(unittest.TestCase):
    def test_known_role_returns_preset(self):
        self.assertEqual(system_prompt_for("worker"), ROLE_SYSTEM_PROMPTS["worker"])

    def test_known_role_is_case_insensitive(self):
        self.assertEqual(system_prompt_for("Worker"), ROLE_SYSTEM_PROMPTS["worker"])

    def test_unknown_name_gets_generic_prompt_mentioning_name(self):
        prompt = system_prompt_for("custom-role")

        self.assertIn("custom-role", prompt)


class RolePermissionForTests(unittest.TestCase):
    def test_all_six_default_roles_have_all_three_harnesses_defined(self):
        for role in (
            "orchestrator",
            "planner",
            "worker",
            "tester",
            "reviewer",
            "utility",
        ):
            for harness in ("claude", "codex", "opencode"):
                self.assertIn(harness, ROLE_PERMISSIONS[role], f"{role}/{harness}")

    def test_unknown_role_gets_no_restriction(self):
        self.assertEqual(role_permission_for("custom-role", "claude"), {})

    def test_case_insensitive_lookup(self):
        self.assertEqual(
            role_permission_for("Worker", "codex"),
            role_permission_for("worker", "codex"),
        )

    def test_no_role_is_allowed_to_commit_or_push(self):
        for role, per_harness in ROLE_PERMISSIONS.items():
            claude_allowed = " ".join(
                per_harness.get("claude", {}).get("allowedTools", [])
            )
            self.assertNotIn("git commit", claude_allowed, role)
            self.assertNotIn("git push", claude_allowed, role)
            opencode_bash = per_harness.get("opencode", {}).get("bash", {})
            if isinstance(opencode_bash, dict):
                for pattern, effect in opencode_bash.items():
                    if "commit" in pattern or "push" in pattern:
                        self.assertEqual(effect, "deny", f"{role}/{pattern}")

    def test_reviewer_can_write_markdown_but_not_source(self):
        allowed = role_permission_for("reviewer", "claude")["allowedTools"]
        disallowed = role_permission_for("reviewer", "claude")["disallowedTools"]

        self.assertIn("Edit(**/*.md)", allowed)
        self.assertIn("Edit", disallowed)
        self.assertIn("Bash(git commit *)", disallowed)


class PermissionYamlTests(unittest.TestCase):
    def test_flat_permission_renders_as_key_value_lines(self):
        markdown = opencode_agent_markdown(
            "worker", "do the work", {"read": "allow", "edit": "deny"}
        )

        self.assertIn("permission:\n  read: allow\n  edit: deny", markdown)

    def test_nested_bash_permission_renders_indented(self):
        markdown = opencode_agent_markdown(
            "worker", "do the work", {"bash": {"git status": "allow"}}
        )

        self.assertIn('bash:\n    "git status": allow', markdown)

    def test_no_permission_omits_permission_block(self):
        markdown = opencode_agent_markdown("worker", "do the work")

        self.assertNotIn("permission:", markdown)


class OpencodeAgentFileTests(unittest.TestCase):
    def test_markdown_includes_name_and_prompt(self):
        markdown = opencode_agent_markdown("worker", "do the work")

        self.assertIn("description: worker role", markdown)
        self.assertIn("mode: primary", markdown)
        self.assertIn("do the work", markdown)

    def test_write_creates_file_with_content(self):
        agent_dir = Path(tempfile.mkdtemp())

        path = write_opencode_agent_file("worker", "do the work", agent_dir)

        self.assertEqual(path, agent_dir / "worker.md")
        self.assertIn("do the work", path.read_text(encoding="utf-8"))

    def test_rejects_path_traversal_name(self):
        agent_dir = Path(tempfile.mkdtemp())

        with self.assertRaises(ValueError):
            write_opencode_agent_file("../../evil", "do the work", agent_dir)

        self.assertEqual(list(agent_dir.iterdir()), [])

    def test_rejects_name_with_path_separator(self):
        agent_dir = Path(tempfile.mkdtemp())

        with self.assertRaises(ValueError):
            write_opencode_agent_file("sub/dir", "do the work", agent_dir)


class BuildArgvTests(unittest.TestCase):
    def test_legacy_three_argument_call_uses_worker_role(self):
        self.assertEqual(
            build_argv("codex", "openai", "gpt-5.2"),
            ["codex", "-m", "gpt-5.2", "-s", "workspace-write"],
        )

    def test_claude_without_model_still_replaces_system_prompt(self):
        result = build_argv("claude", "", "", "worker")

        self.assertEqual(
            result[:3], ["claude", "--system-prompt", system_prompt_for("worker")]
        )
        self.assertIn("--allowedTools", result)
        self.assertIn("--disallowedTools", result)

    def test_claude_with_model(self):
        result = build_argv("claude", "anthropic", "sonnet", "planner")

        self.assertEqual(
            result[:5],
            [
                "claude",
                "--system-prompt",
                system_prompt_for("planner"),
                "--model",
                "sonnet",
            ],
        )
        self.assertIn("--disallowedTools", result)

    def test_claude_never_uses_append_system_prompt(self):
        result = build_argv("claude", "", "", "worker")

        self.assertNotIn("--append-system-prompt", result)

    def test_codex_with_model_applies_role_sandbox(self):
        result = build_argv("codex", "openai", "gpt-5.2", "worker")

        self.assertEqual(result, ["codex", "-m", "gpt-5.2", "-s", "workspace-write"])

    def test_codex_orchestrator_gets_read_only_sandbox(self):
        result = build_argv("codex", "openai", "gpt-5.2", "orchestrator")

        self.assertEqual(result, ["codex", "-m", "gpt-5.2", "-s", "read-only"])

    def test_opencode_with_source_and_model_selects_role_agent(self):
        result = build_argv(
            "opencode", "ollama", "gemma4:e4b", "utility", opencode_exe="opencode.exe"
        )

        self.assertEqual(
            result, ["opencode.exe", "--agent", "utility", "-m", "ollama/gemma4:e4b"]
        )

    def test_opencode_without_model_still_selects_role_agent(self):
        result = build_argv("opencode", "", "", "utility", opencode_exe="opencode.exe")

        self.assertEqual(result, ["opencode.exe", "--agent", "utility"])

    def test_opencode_writes_agent_file_for_role(self):
        agent_dir = Path(tempfile.mkdtemp())
        with mock.patch("agent_wrapper.server._OPENCODE_AGENT_DIR", agent_dir):
            build_argv("opencode", "", "", "reviewer", opencode_exe="opencode.exe")

        self.assertTrue((agent_dir / "reviewer.md").exists())

    def test_unknown_harness_raises(self):
        with self.assertRaises(ValueError):
            build_argv("unknown", "", "", "worker")

    def test_path_traversal_name_rejected_for_every_harness(self):
        for harness in ("claude", "codex", "opencode"):
            with self.assertRaises(ValueError):
                build_argv(harness, "", "", "../evil", opencode_exe="opencode.exe")

    def test_legacy_call_without_name_defaults_to_worker_role(self):
        result = build_argv("claude", "", "")

        self.assertEqual(
            result[:3], ["claude", "--system-prompt", system_prompt_for("worker")]
        )
        self.assertIn("--allowedTools", result)

    def test_opencode_agent_file_embeds_role_permission(self):
        agent_dir = Path(tempfile.mkdtemp())
        with mock.patch("agent_wrapper.server._OPENCODE_AGENT_DIR", agent_dir):
            build_argv("opencode", "", "", "worker", opencode_exe="opencode.exe")

        content = (agent_dir / "worker.md").read_text(encoding="utf-8")
        self.assertIn("permission:", content)
        self.assertIn("edit: allow", content)

    def test_opencode_orchestrator_agent_file_denies_edit_and_bash(self):
        agent_dir = Path(tempfile.mkdtemp())
        with mock.patch("agent_wrapper.server._OPENCODE_AGENT_DIR", agent_dir):
            build_argv("opencode", "", "", "orchestrator", opencode_exe="opencode.exe")

        content = (agent_dir / "orchestrator.md").read_text(encoding="utf-8")
        self.assertIn("edit: deny", content)
        self.assertIn("bash: deny", content)


class _FakePtyProcess:
    """外部の実プロセスを起動せずSessionをテストするためのwinpty.PtyProcess代替。"""

    def __init__(self) -> None:
        self._alive = True
        self.written: list[str] = []
        self.terminated_force: bool | None = None

    def isalive(self) -> bool:
        return self._alive

    def write(self, text: str) -> None:
        self.written.append(text)

    def terminate(self, force: bool = False) -> None:
        self.terminated_force = force
        self._alive = False

    def read(self, size: int) -> str:
        return ""


def _make_session(name: str = "worker", **kwargs) -> Session:
    with mock.patch(
        "agent_wrapper.server.winpty.PtyProcess.spawn", return_value=_FakePtyProcess()
    ):
        return Session(name, ["claude"], "C:/tmp", **kwargs)


class SessionTests(unittest.TestCase):
    def test_to_dict_reports_metadata_and_liveness(self):
        session = _make_session(harness="claude", source="anthropic", model="sonnet")

        self.assertEqual(
            session.to_dict(),
            {
                "name": "worker",
                "harness": "claude",
                "source": "anthropic",
                "model": "sonnet",
                "mode": "raw-pty",
                "alive": True,
            },
        )

    def test_to_dict_reflects_termination(self):
        session = _make_session()
        session.terminate()

        self.assertFalse(session.to_dict()["alive"])

    def test_output_text_returns_full_scrollback(self):
        session = _make_session()
        session.scrollback.extend(b"hello world")

        self.assertEqual(session.output_text(), "hello world")

    def test_output_text_tail_limits_to_last_n_bytes(self):
        session = _make_session()
        session.scrollback.extend(b"0123456789")

        self.assertEqual(session.output_text(tail=4), "6789")

    def test_output_text_empty_scrollback_is_empty_string(self):
        session = _make_session()

        self.assertEqual(session.output_text(), "")

    def test_write_forwards_to_underlying_process(self):
        session = _make_session()
        session.write("hello\r")

        self.assertEqual(session.proc.written, ["hello\r"])

    def test_concurrent_stop_notifications_are_sent_once(self):
        session = _make_session()
        original_value = session._stop_notified

        def get_notified(_self):
            value = original_value_holder[0]
            time.sleep(0.01)
            return value

        original_value_holder = [original_value]
        with mock.patch.object(
            type(session),
            "_stop_notified",
            new=property(
                get_notified,
                lambda _self, value: original_value_holder.__setitem__(0, value),
            ),
            create=True,
        ):
            self._assert_concurrent_stop_notification_once(session)

    def _assert_concurrent_stop_notification_once(self, session):
        with mock.patch("agent_wrapper.server.aituber_pusher.send") as send:
            with ThreadPoolExecutor(max_workers=32) as executor:
                futures = [executor.submit(session._notify_stopped) for _ in range(32)]
                for future in futures:
                    future.result()

        send.assert_called_once_with("session_stopped", "worker", "session stopped")


class SessionManagerStopOneTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_one_terminates_and_removes_session(self):
        manager = SessionManager()
        session = _make_session()
        manager.sessions["worker"] = session

        result = await manager.stop_one("worker")

        self.assertTrue(result)
        self.assertNotIn("worker", manager.sessions)
        self.assertFalse(session.proc.isalive())

    async def test_stop_one_unknown_name_returns_false(self):
        manager = SessionManager()

        self.assertFalse(await manager.stop_one("nope"))

    async def test_stop_one_cancels_that_sessions_pending_approvals(self):
        manager = SessionManager()
        manager.sessions["worker"] = _make_session()
        state = manager.shared_state("worker", "claude")
        request = state.set_waiting("permission_escalated", "edit")

        await manager.stop_one("worker")

        self.assertNotEqual(request.status, "pending")
        self.assertEqual(manager.approval_broker.pending("worker"), [])

    async def test_stop_all_stops_every_session(self):
        manager = SessionManager()
        manager.sessions["worker"] = _make_session("worker")
        manager.sessions["reviewer"] = _make_session("reviewer")

        await manager.stop_all()

        self.assertEqual(manager.sessions, {})

    async def test_stop_all_waits_for_inbox_task_cancellation(self):
        manager = SessionManager()
        started = asyncio.Event()
        finished = asyncio.Event()

        async def wait_for_cancel(*_args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                finished.set()

        with (
            mock.patch("agent_wrapper.server.aituber_pusher", mock.Mock(enabled=True)),
            mock.patch("agent_wrapper.server.poll_inbox", wait_for_cancel),
            mock.patch("agent_wrapper.server.remember_folder"),
        ):
            await manager.start(".", [])
            await started.wait()
            old_task = manager._inbox_task

            await manager.stop_all()

        self.assertIsNotNone(old_task)
        self.assertTrue(old_task.done())
        self.assertTrue(finished.is_set())

    async def test_stop_all_propagates_its_own_cancellation(self):
        manager = SessionManager()
        started = asyncio.Event()
        cancellation_started = asyncio.Event()
        finish_cleanup = asyncio.Event()

        async def wait_for_cancel(*_args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancellation_started.set()
                await finish_cleanup.wait()

        with (
            mock.patch("agent_wrapper.server.aituber_pusher", mock.Mock(enabled=True)),
            mock.patch("agent_wrapper.server.poll_inbox", wait_for_cancel),
            mock.patch("agent_wrapper.server.remember_folder"),
        ):
            await manager.start(".", [])
            await started.wait()
            stop_task = asyncio.create_task(manager.stop_all())
            await cancellation_started.wait()
            stop_task.cancel()
            finish_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await stop_task

    async def test_restart_waits_for_previous_inbox_task(self):
        manager = SessionManager()
        started_count = 0
        first_finished = asyncio.Event()
        first_started = asyncio.Event()
        all_started = asyncio.Event()

        async def wait_for_cancel(*_args):
            nonlocal started_count
            started_count += 1
            if started_count == 1:
                first_started.set()
            elif started_count == 2:
                all_started.set()
            try:
                await asyncio.Future()
            finally:
                if started_count == 1:
                    first_finished.set()

        with (
            mock.patch("agent_wrapper.server.aituber_pusher", mock.Mock(enabled=True)),
            mock.patch("agent_wrapper.server.poll_inbox", wait_for_cancel),
            mock.patch("agent_wrapper.server.remember_folder"),
        ):
            await manager.start(".", [])
            await first_started.wait()
            old_task = manager._inbox_task
            await manager.start(".", [])
            await all_started.wait()

        self.assertIsNotNone(old_task)
        self.assertTrue(old_task.done())
        self.assertTrue(first_finished.is_set())
        await manager.stop_all()

    async def test_stop_one_cancels_opencode_internal_session_approval(self):
        manager = SessionManager()
        manager.sessions["worker"] = _make_session()
        decisions: list[ApprovalDecision] = []

        def evaluate() -> None:
            decisions.append(
                manager.evaluate_permission(
                    "ses_internal",
                    "opencode",
                    {"action": "bash", "resources": ["Remove-Item important.txt"]},
                    timeout=2,
                    owner_session_id="worker",
                )
            )

        with mock.patch("agent_wrapper.server.rules.check_destructive") as destructive:
            destructive.return_value.matched = True
            thread = threading.Thread(target=evaluate)
            thread.start()
            for _ in range(100):
                if manager.approval_broker.pending("ses_internal"):
                    break
                threading.Event().wait(0.01)

            await manager.stop_one("worker")
            thread.join(timeout=2)

        self.assertEqual(decisions, [ApprovalDecision("deny", "session cancelled")])
        self.assertEqual(manager.approval_broker.pending("ses_internal"), [])

    async def test_late_opencode_event_after_stop_is_denied_without_pending(self):
        manager = SessionManager()

        decision = manager.evaluate_permission(
            "ses_late",
            "opencode",
            {"action": "bash", "resources": ["Remove-Item important.txt"]},
            owner_session_id="worker",
        )

        self.assertEqual(
            decision, ApprovalDecision("deny", "owner session is not active")
        )
        self.assertEqual(manager.approval_broker.pending("ses_late"), [])


class WrappedSessionTests(unittest.TestCase):
    def test_concurrent_stop_notifications_are_sent_once(self):
        session = WrappedSession(
            "worker",
            "claude",
            "anthropic",
            "sonnet",
            ".",
            SessionManager().shared_state("worker", "claude"),
        )
        original_value = session._stop_notified
        original_value_holder = [original_value]

        def get_notified(_self):
            value = original_value_holder[0]
            time.sleep(0.01)
            return value

        with mock.patch.object(
            type(session),
            "_stop_notified",
            new=property(
                get_notified,
                lambda _self, value: original_value_holder.__setitem__(0, value),
            ),
            create=True,
        ):
            with mock.patch("agent_wrapper.server.aituber_pusher.send") as send:
                with ThreadPoolExecutor(max_workers=32) as executor:
                    futures = [
                        executor.submit(session._notify_stopped) for _ in range(32)
                    ]
                    for future in futures:
                        future.result()

            send.assert_called_once_with("session_stopped", "worker", "session stopped")

    def test_first_submitted_line_starts_claude_runner_with_broker_state(self):
        state = SessionManager().shared_state("worker", "claude")
        runner = mock.Mock()
        runner_type = mock.Mock(return_value=runner)
        with mock.patch("agent_wrapper.server.ClaudeRunner", runner_type):
            session = WrappedSession(
                "worker", "claude", "anthropic", "sonnet", ".", state
            )
            session.write("implement this\r")

        runner.start.assert_called_once_with()
        self.assertIs(runner_type.call_args.kwargs["state"], state)
        self.assertIn("implement this", runner_type.call_args.kwargs["prompt"])

    def test_role_permission_is_forwarded_to_runner(self):
        state = SessionManager().shared_state("reviewer", "claude")
        runner_type = mock.Mock(return_value=mock.Mock())
        with mock.patch("agent_wrapper.server.ClaudeRunner", runner_type):
            session = WrappedSession(
                "reviewer", "claude", "anthropic", "sonnet", ".", state
            )
            session.write("review this\r")

        permission = runner_type.call_args.kwargs["permission"]
        self.assertEqual(permission, role_permission_for("reviewer", "claude"))
        self.assertIn("Edit", permission["disallowedTools"])

    def test_manager_defaults_claude_to_wrapped_mode(self):
        manager = SessionManager()
        with mock.patch("agent_wrapper.server.remember_folder"):
            names = asyncio.run(
                manager.start(
                    ".",
                    [
                        {
                            "name": "worker",
                            "harness": "claude",
                            "source": "",
                            "model": "",
                        }
                    ],
                )
            )
        self.assertEqual(names, ["worker"])
        self.assertIsInstance(manager.sessions["worker"], WrappedSession)


class OpenCodeBrokerPluginTests(unittest.TestCase):
    def test_plugin_uses_permission_event_and_reply_api(self):
        cwd = tempfile.mkdtemp()

        path = write_opencode_broker_plugin("worker", cwd)
        source = path.read_text(encoding="utf-8")

        self.assertIn('event.type !== "permission.asked"', source)
        self.assertIn("client.permission.reply", source)
        self.assertIn("/approval-events", source)
        self.assertIn('owner_session_id: "worker"', source)

    def test_plugin_can_target_an_isolated_broker_url(self):
        cwd = tempfile.mkdtemp()

        path = write_opencode_broker_plugin(
            "worker", cwd, broker_url="http://127.0.0.1:43210"
        )

        self.assertIn(
            'fetch("http://127.0.0.1:43210/approval-events"',
            path.read_text(encoding="utf-8"),
        )

    def test_plugin_does_not_overwrite_unowned_file(self):
        cwd = Path(tempfile.mkdtemp())
        path = cwd / ".opencode" / "plugins" / "agent-wrapper-worker.js"
        path.parent.mkdir(parents=True)
        path.write_text("user plugin", encoding="utf-8")

        with self.assertRaises(ValueError):
            write_opencode_broker_plugin("worker", str(cwd))


class ApprovalHttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = SessionManager()
        handler = _make_http_handler(self.manager, [asyncio.new_event_loop()])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict | None = None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_lists_and_resolves_request_once(self):
        state = self.manager.shared_state("worker", "claude")
        request = state.set_waiting("permission_escalated", "edit")

        status, listing = self.request("GET", "/approvals?session_id=worker")
        first_status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"session_id": "worker", "action": "approve"},
        )
        second_status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"session_id": "worker", "action": "approve"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(listing["requests"][0]["request_id"], request.request_id)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 409)

    def test_rejects_cross_session_response(self):
        request = self.manager.shared_state("worker", "claude").set_waiting(
            "permission_escalated", "edit"
        )

        status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"session_id": "reviewer", "action": "approve"},
        )

        self.assertEqual(status, 409)

    def test_rejects_missing_session_id_and_invalid_action(self):
        request = self.manager.shared_state("worker", "claude").set_waiting(
            "permission_escalated", "edit"
        )

        missing_status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"action": "approve"},
        )
        invalid_status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"session_id": "worker", "action": "always"},
        )

        self.assertEqual(missing_status, 400)
        self.assertEqual(invalid_status, 409)

    def test_expired_request_returns_conflict(self):
        request = self.manager.approval_broker.submit(
            ApprovalRequestInput(
                session_id="worker",
                harness="claude",
                kind="permission",
                action="edit",
                resource="src/app.py",
                reason="expired",
                expires_at=datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(seconds=1),
            )
        )

        status, _ = self.request(
            "POST",
            f"/approvals/{request.request_id}/respond",
            {"session_id": "worker", "action": "approve"},
        )

        self.assertEqual(status, 409)

    def test_structured_opencode_event_returns_broker_effect(self):
        with mock.patch.object(
            self.manager,
            "evaluate_permission",
            return_value=ApprovalDecision("approve", "safe"),
        ):
            status, data = self.request(
                "POST",
                "/approval-events",
                {
                    "session_id": "worker",
                    "harness": "opencode",
                    "action": "bash",
                    "resources": ["uv run pytest"],
                },
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, {"effect": "allow", "message": "safe"})


class RunTestsHttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = SessionManager()
        handler = _make_http_handler(self.manager, [asyncio.new_event_loop()])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, path: str):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(
            "POST", path, body="{}", headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_returns_400_without_active_workspace(self):
        status, data = self.post("/run-tests")

        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_runs_configured_workspace_and_returns_structured_result(self):
        self.manager.cwd = "."
        with mock.patch(
            "agent_wrapper.server.run_tests",
            return_value={
                "command": ["uv", "run", "pytest", "-q"],
                "returncode": 0,
                "passed": True,
                "timed_out": False,
                "stdout": "1 passed",
                "stderr": "",
                "failed_tests": [],
            },
        ) as mock_run_tests:
            status, data = self.post("/run-tests")

        mock_run_tests.assert_called_once_with(".")
        self.assertEqual(status, 200)
        self.assertTrue(data["passed"])


class UtilityJudgeHttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = SessionManager()
        handler = _make_http_handler(self.manager, [asyncio.new_event_loop()])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def post(self, path: str, payload: dict):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        body = json.dumps(payload).encode()
        connection.request(
            "POST", path, body=body, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def test_permission_mode_returns_judge_permission_result(self):
        with mock.patch(
            "agent_wrapper.server.ollama_client.judge_permission",
            return_value={"decision": "allow", "raw": "ALLOW"},
        ) as mock_judge:
            status, data = self.post(
                "/utility/judge", {"mode": "permission", "text": "rm foo.txt"}
            )

        mock_judge.assert_called_once_with("rm foo.txt")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"decision": "allow", "raw": "ALLOW"})

    def test_summarize_mode_returns_summarize_chunk_result(self):
        with mock.patch(
            "agent_wrapper.server.ollama_client.summarize_chunk",
            return_value="テストを実行した",
        ) as mock_summarize:
            status, data = self.post(
                "/utility/judge", {"mode": "summarize", "text": "log output"}
            )

        mock_summarize.assert_called_once_with("log output")
        self.assertEqual(status, 200)
        self.assertEqual(data, "テストを実行した")

    def test_conversational_mode_returns_judge_conversational_question_result(self):
        with mock.patch(
            "agent_wrapper.server.ollama_client.judge_conversational_question",
            return_value={"decision": "escalate", "raw": "ESCALATE"},
        ) as mock_judge:
            status, data = self.post(
                "/utility/judge",
                {"mode": "conversational", "text": "続けてよいですか?"},
            )

        mock_judge.assert_called_once_with("続けてよいですか?")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"decision": "escalate", "raw": "ESCALATE"})

    def test_unknown_mode_returns_400(self):
        status, data = self.post(
            "/utility/judge", {"mode": "bogus", "text": "anything"}
        )

        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_ollama_exception_returns_502(self):
        with mock.patch(
            "agent_wrapper.server.ollama_client.judge_permission",
            side_effect=RuntimeError("ollama not running"),
        ):
            status, data = self.post(
                "/utility/judge", {"mode": "permission", "text": "rm foo.txt"}
            )

        self.assertEqual(status, 502)
        self.assertIn("error", data)


class AddRecentFolderTests(unittest.TestCase):
    def test_new_folder_is_prepended(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\c")

        self.assertEqual(result, ["C:\\c", "C:\\a", "C:\\b"])

    def test_existing_folder_moves_to_front(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\b")

        self.assertEqual(result, ["C:\\b", "C:\\a"])

    def test_truncates_to_limit(self):
        result = add_recent_folder(["C:\\a", "C:\\b"], "C:\\c", limit=2)

        self.assertEqual(result, ["C:\\c", "C:\\a"])


class RecentFoldersPersistenceTests(unittest.TestCase):
    def test_load_missing_file_returns_empty_list(self):
        path = Path(tempfile.mkdtemp()) / "does-not-exist" / "recent_folders.json"

        self.assertEqual(load_recent_folders(path), [])

    def test_remember_folder_persists_and_dedupes(self):
        path = Path(tempfile.mkdtemp()) / "recent_folders.json"

        remember_folder("C:\\project-a", path)
        result = remember_folder("C:\\project-b", path)
        result = remember_folder("C:\\project-a", path)

        self.assertEqual(result, ["C:\\project-a", "C:\\project-b"])
        self.assertEqual(load_recent_folders(path), result)


class ApprovalBrokerIntegrationTests(unittest.TestCase):
    def test_opencode_permission_is_auto_approved_only_after_utility_judgment(self):
        manager = SessionManager()
        with (
            mock.patch("agent_wrapper.server.rules.check_destructive") as destructive,
            mock.patch(
                "agent_wrapper.server.ollama_client.judge_permission",
                return_value={"decision": "allow"},
            ) as judge,
        ):
            destructive.return_value.matched = False
            decision = manager.evaluate_permission(
                "worker",
                "opencode",
                {"action": "bash", "resources": ["uv run pytest"]},
            )

        self.assertEqual(decision.action, "approve")
        judge.assert_called_once()

    def test_opencode_escalation_waits_for_request_scoped_human_response(self):
        manager = SessionManager()
        decisions: list[ApprovalDecision] = []

        def evaluate():
            decisions.append(
                manager.evaluate_permission(
                    "worker",
                    "opencode",
                    {"action": "bash", "resources": ["Remove-Item important.txt"]},
                    timeout=2,
                )
            )

        with mock.patch("agent_wrapper.server.rules.check_destructive") as destructive:
            destructive.return_value.matched = True
            thread = threading.Thread(target=evaluate)
            thread.start()
            for _ in range(100):
                pending = manager.approval_broker.pending("worker")
                if pending:
                    break
                threading.Event().wait(0.01)
            self.assertTrue(pending)
            manager.respond_approval("worker", pending[0].request_id, "deny", "keep it")
            thread.join(timeout=2)

        self.assertEqual(decisions, [ApprovalDecision("deny", "keep it")])

    def test_shared_states_use_one_broker_without_cross_session_release(self):
        manager = SessionManager()
        worker = manager.shared_state("worker-1", "claude")
        reviewer = manager.shared_state("reviewer-1", "codex")
        worker_request = worker.set_waiting("permission_escalated", "worker")
        reviewer_request = reviewer.set_waiting("plan_approval", "reviewer")

        self.assertEqual(len(manager.approval_broker.pending()), 2)
        self.assertFalse(
            manager.respond_approval("reviewer-1", worker_request.request_id, "approve")
        )
        self.assertTrue(
            manager.respond_approval("worker-1", worker_request.request_id, "approve")
        )
        self.assertEqual(
            [item.request_id for item in manager.approval_broker.pending()],
            [reviewer_request.request_id],
        )

    def test_server_boundary_rejects_always(self):
        manager = SessionManager()
        state = manager.shared_state("worker-1", "opencode")
        request = state.set_waiting("permission_escalated", "install")

        self.assertFalse(
            manager.respond_approval("worker-1", request.request_id, "always")
        )
        self.assertEqual(len(manager.approval_broker.pending("worker-1")), 1)

    def test_broker_response_calls_existing_shared_state_callback(self):
        manager = SessionManager()
        state = manager.shared_state("worker-1", "claude")
        received: list[HumanResponse] = []
        request = state.set_waiting(
            "permission_escalated", "edit", callback=received.append
        )

        self.assertTrue(
            manager.respond_approval(
                "worker-1", request.request_id, "deny", "範囲外です"
            )
        )
        self.assertEqual(received, [HumanResponse("deny", "範囲外です")])

    def test_expired_request_does_not_leave_shared_state_waiting(self):
        manager = SessionManager()
        state = manager.shared_state("worker-1", "opencode")
        manager.approval_broker.submit(
            ApprovalRequestInput(
                session_id="worker-1",
                harness="opencode",
                kind="permission",
                action="shell",
                resource="git status",
                reason="期限切れ",
                expires_at=datetime.datetime.now(datetime.UTC)
                - datetime.timedelta(seconds=1),
            )
        )
        state.status = "waiting_human"

        snapshot = state.snapshot()

        self.assertEqual(snapshot["status"], "running")
        self.assertIsNone(snapshot["pending_request_id"])

    def test_stop_all_cancels_pending_requests(self):
        manager = SessionManager()
        state = manager.shared_state("worker-1", "claude")
        received: list[HumanResponse] = []
        request = state.set_waiting(
            "permission_escalated", "edit", callback=received.append
        )

        asyncio.run(manager.stop_all())

        self.assertEqual(request.status, "cancelled")
        self.assertEqual(received, [HumanResponse("deny", "session cancelled")])


class ServerPageApprovalLayoutTests(unittest.TestCase):
    def setUp(self):
        self.page = Path("agent_wrapper/server_static/index.html").read_text(
            encoding="utf-8"
        )

    def test_runtime_has_approval_rail_before_terminal_workspace(self):
        approval_position = self.page.index('id="approval-rail"')
        terminal_position = self.page.index('id="terminal-workspace"')

        self.assertLess(approval_position, terminal_position)
        self.assertIn('class="runtime-shell"', self.page)

    def test_page_uses_broker_list_and_request_scoped_response_api(self):
        self.assertIn('fetch("/approvals")', self.page)
        self.assertIn('"/approvals/" + encodeURIComponent', self.page)
        self.assertIn("body: JSON.stringify({ session_id:", self.page)

    def test_page_exposes_human_decision_controls(self):
        self.assertIn('data-approval-action="approve"', self.page)
        self.assertIn('data-approval-action="explain"', self.page)
        self.assertIn('data-approval-action="deny"', self.page)
        self.assertIn('id="approval-flow"', self.page)

    def test_terminal_workspace_has_orchestrator_stage_and_two_by_two_role_grid(self):
        orchestrator_position = self.page.index('id="orchestrator-stage"')
        role_grid_position = self.page.index('id="role-terminal-grid"')

        self.assertLess(orchestrator_position, role_grid_position)
        self.assertIn('class="orchestrator-stage"', self.page)
        self.assertIn('class="role-terminal-grid"', self.page)
        self.assertIn('name.toLowerCase() === "orchestrator"', self.page)

    def test_codex_is_not_presented_as_fully_broker_gated(self):
        self.assertIn('config.harness === "codex"', self.page)
        self.assertIn('"計画承認のみ"', self.page)

    def test_opencode_is_presented_as_unverified(self):
        self.assertIn("Broker未検証", self.page)
        self.assertNotIn('"Broker hook"', self.page)


if __name__ == "__main__":
    unittest.main()
