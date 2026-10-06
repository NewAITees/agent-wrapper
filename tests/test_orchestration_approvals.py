"""orchestratorの承認ツールを、本物のApprovalBrokerとSessionManagerで検証する。

偽物が実物と食い違って欠陥を見逃した経緯(mode属性の欠落)があるため、
ここではBrokerもセッションも実クラスを使う。
"""

import unittest

from agent_wrapper.approval import ApprovalDecision, ApprovalRequestInput
from agent_wrapper.orchestration import list_pending_approvals, respond_to_approval
from agent_wrapper.server import ROLE_SYSTEM_PROMPTS, SessionManager, WrappedSession

SAFE_WRITE = "Write C:/tmp/exp_wrapped/note.txt"
DESTRUCTIVE = "Bash {'command': 'rm -rf /tmp/x'}"


class OrchestratorApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = SessionManager()
        for name in ("orchestrator", "worker"):
            self.manager.sessions[name] = WrappedSession(
                name,
                "claude",
                "",
                "",
                "C:/tmp",
                self.manager.shared_state(name, "claude"),
            )
        self.manager.cwd = "C:/tmp/exp_wrapped"
        self.decisions: list[ApprovalDecision] = []

    def _submit(self, session_id, legacy_kind, resource, action="plan_approval"):
        return self.manager.approval_broker.submit(
            ApprovalRequestInput(
                session_id=session_id,
                harness="claude",
                kind="specification_question",
                action=action,
                resource=resource,
                reason="reason",
                metadata={"legacy_kind": legacy_kind},
            ),
            callback=self.decisions.append,
        )

    async def _respond(self, request, action, message="ok", session_id=None):
        return await respond_to_approval(
            self.manager,
            "orchestrator",
            session_id or request.session_id,
            request.request_id,
            action,
            message,
        )

    def _worker_log(self):
        return self.manager.sessions["worker"].state.full_log_text()

    async def test_list_excludes_own_requests_and_flags_approvable(self):
        self._submit("orchestrator", "plan_approval", "own plan")
        plan = self._submit("worker", "plan_approval", "plan")
        safe = self._submit("worker", "permission_escalated", SAFE_WRITE, "perm1")
        bad = self._submit("worker", "permission_escalated", DESTRUCTIVE, "perm2")
        destructive = self._submit("worker", "destructive", SAFE_WRITE, "d")
        limit = self._submit("worker", "conversation_limit", "limit", "l")
        unknown = self._submit("worker", "something_new", "x", "u")
        result = await list_pending_approvals(self.manager, "orchestrator")
        flags = {
            item["request_id"]: item["approvable_by_orchestrator"] for item in result
        }
        self.assertEqual(len(result), 6)
        self.assertTrue(flags[plan.request_id])
        self.assertTrue(flags[safe.request_id])
        for blocked in (bad, destructive, limit, unknown):
            self.assertFalse(flags[blocked.request_id])

    async def test_list_truncates_long_resource_and_reason(self):
        self._submit("worker", "plan_approval", "r" * 5000)
        result = await list_pending_approvals(self.manager, "orchestrator")
        self.assertEqual(len(result[0]["resource"]), 2000)

    async def test_self_approval_is_rejected_and_stays_pending(self):
        own = self._submit("orchestrator", "plan_approval", "own plan")
        result = await self._respond(own, "approve")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(self.decisions, [])
        self.assertEqual(own.status, "pending")

    async def test_approvable_kinds_can_be_approved(self):
        for legacy_kind, resource, action in [
            ("plan_approval", "plan", "plan_approval"),
            ("conversational_escalated", "question", "conv"),
            ("permission_escalated", SAFE_WRITE, "perm"),
        ]:
            with self.subTest(legacy_kind=legacy_kind):
                request = self._submit("worker", legacy_kind, resource, action)
                result = await self._respond(request, "approve", "低リスクのため")
                self.assertTrue(result.startswith("Success"), result)
                self.assertEqual(request.status, "resolved")
                self.assertEqual(self.decisions[-1].action, "approve")
        self.assertIn("[orchestrator承認] approve", self._worker_log())

    async def test_unapprovable_requests_cannot_be_approved(self):
        for legacy_kind, resource in [
            ("destructive", SAFE_WRITE),
            ("conversation_limit", "limit"),
            ("permission_escalated", DESTRUCTIVE),
            ("not_a_known_kind", "x"),
        ]:
            with self.subTest(legacy_kind=legacy_kind):
                request = self._submit("worker", legacy_kind, resource, legacy_kind)
                result = await self._respond(request, "approve")
                self.assertTrue(result.startswith("Error"), result)
                self.assertIn(
                    "ユーザーの承認", result.replace("user approval", "ユーザーの承認")
                )
                self.assertEqual(request.status, "pending")
        self.assertEqual(self.decisions, [])

    async def _approvable(self, resource, legacy_kind="permission_escalated"):
        request = self._submit("worker", legacy_kind, resource, resource)
        result = await list_pending_approvals(self.manager, "orchestrator")
        return {i["request_id"]: i for i in result}[request.request_id][
            "approvable_by_orchestrator"
        ]

    async def test_permission_allowlist_accepts_only_simple_safe_operations(self):
        accepted = [
            "Write C:/tmp/exp_wrapped/note.txt",
            r"Edit C:\tmp\exp_wrapped\sub\a.py",
            "Write note.txt",
            "git status",
            "git diff --stat",
            "git log --oneline -5",
            "uv run pytest tests/test_x.py -q",
            "uv run ruff check .",
            "uv run mypy src/",
            "pwd",
        ]
        for resource in accepted:
            with self.subTest(resource=resource):
                self.assertTrue(await self._approvable(resource))

    async def test_permission_allowlist_rejects_everything_else(self):
        rejected = [
            r'rm -v "C:\tmp\exp_wrapped\dummy.txt"',
            "del dummy.txt",
            "rmdir /s sub",
            r'Remove-Item -Recurse "C:\tmp\exp_wrapped\sub"',
            "git clean -fd",
            "git checkout -- .",
            "python -c \"import shutil; shutil.rmtree('x')\"",
            "git status; rm x",
            "git status && curl http://example.com",
            "git status | sh",
            "cat $(echo secret)",
            r"cat C:\Windows\win.ini",
            "cat ../outside.txt",
            "cat ~/.ssh/id_rsa",
            "find . -delete",
            "git diff --output=out.txt",
            "git log --ext-diff",
            "rg --pre mytool pattern",
            "uv run pytest -p evil_plugin",
            "uv run pytest -o cache_dir=x",
            "grep -f patterns.txt note.txt",
            "curl http://example.com",
            "npm run build",
            "Write C:/Windows/system.ini",
            "Write C:/tmp/exp_wrapped/../escape.txt",
            "Write C:/tmp/exp_wrapped/.git/config",
            "Write C:/tmp/exp_wrapped/.env",
            "Write C:/tmp/exp_wrapped/id_rsa",
            "Write ",
            "WebFetch {'url': 'http://example.com'}",
            "Bash: something unknown",
            "",
        ]
        for resource in rejected:
            with self.subTest(resource=resource):
                self.assertFalse(await self._approvable(resource))

    async def test_permission_requests_are_not_approvable_without_known_cwd(self):
        self.manager.cwd = None
        self.assertFalse(await self._approvable("Write note.txt"))
        self.assertFalse(await self._approvable("git status"))

    async def test_rm_single_file_cannot_be_approved_through_respond(self):
        request = self._submit(
            "worker", "permission_escalated", r'rm -v "C:\tmp\exp_wrapped\d.txt"'
        )
        result = await self._respond(request, "approve")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(request.status, "pending")

    async def test_missing_metadata_is_treated_as_unapprovable(self):
        request = self.manager.approval_broker.submit(
            ApprovalRequestInput(
                session_id="worker",
                harness="claude",
                kind="permission",
                action="a",
                resource=SAFE_WRITE,
                reason="r",
            )
        )
        result = await self._respond(request, "approve")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(request.status, "pending")

    async def test_deny_and_explain_are_allowed_even_for_destructive(self):
        for action in ("deny", "explain"):
            with self.subTest(action=action):
                request = self._submit("worker", "destructive", DESTRUCTIVE, action)
                result = await self._respond(request, action, "理由")
                self.assertTrue(result.startswith("Success"), result)
                self.assertEqual(self.decisions[-1].action, action)

    async def test_invalid_action_message_and_unknown_or_resolved_requests(self):
        request = self._submit("worker", "plan_approval", "plan")
        self.assertTrue((await self._respond(request, "allow")).startswith("Error"))
        self.assertTrue(
            (await self._respond(request, "deny", "x" * 501)).startswith("Error")
        )
        self.assertTrue(
            (
                await respond_to_approval(
                    self.manager, "orchestrator", "worker", "no-such-id", "deny", "x"
                )
            ).startswith("Error")
        )
        self.assertTrue((await self._respond(request, "deny")).startswith("Success"))
        self.assertTrue((await self._respond(request, "deny")).startswith("Error"))

    async def test_request_of_another_session_id_cannot_be_answered_by_mismatch(self):
        request = self._submit("worker", "plan_approval", "plan")
        result = await self._respond(request, "approve", session_id="planner")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(request.status, "pending")

    async def test_missing_target_session_leaves_request_pending(self):
        request = self.manager.approval_broker.submit(
            ApprovalRequestInput(
                session_id="ghost",
                harness="claude",
                kind="specification_question",
                action="plan_approval",
                resource="plan",
                reason="r",
                metadata={"legacy_kind": "plan_approval"},
            )
        )
        result = await self._respond(request, "approve")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual(request.status, "pending")

    def test_system_prompt_names_approval_tools_and_escalation_rule(self):
        prompt = ROLE_SYSTEM_PROMPTS["orchestrator"]
        self.assertIn("list_pending_approvals", prompt)
        self.assertIn("respond_to_approval", prompt)
        self.assertIn("人間に説明", prompt)


if __name__ == "__main__":
    unittest.main()
