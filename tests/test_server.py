import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_wrapper.server import (
    ROLE_SYSTEM_PROMPTS,
    SessionManager,
    add_recent_folder,
    build_argv,
    load_recent_folders,
    opencode_agent_markdown,
    parse_ollama_tags,
    parse_opencode_models,
    remember_folder,
    system_prompt_for,
    write_opencode_agent_file,
)
from agent_wrapper.approval import ApprovalRequestInput
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
    def test_claude_without_model_still_replaces_system_prompt(self):
        result = build_argv("claude", "", "", "worker")

        self.assertEqual(
            result, ["claude", "--system-prompt", system_prompt_for("worker")]
        )

    def test_claude_with_model(self):
        result = build_argv("claude", "anthropic", "sonnet", "planner")

        self.assertEqual(
            result,
            [
                "claude",
                "--system-prompt",
                system_prompt_for("planner"),
                "--model",
                "sonnet",
            ],
        )

    def test_claude_never_uses_append_system_prompt(self):
        result = build_argv("claude", "", "", "worker")

        self.assertNotIn("--append-system-prompt", result)

    def test_codex_with_model_has_no_system_prompt_injection(self):
        result = build_argv("codex", "openai", "gpt-5.2", "worker")

        self.assertEqual(result, ["codex", "-m", "gpt-5.2"])

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


if __name__ == "__main__":
    unittest.main()
