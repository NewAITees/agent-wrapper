import asyncio
import logging
import unittest
from unittest import mock

import mcp.types as t

from agent_wrapper.runners.codex_runner import (
    CodexRunner,
    _CodexEventValidationFilter,
)
from agent_wrapper.wrapper import SharedState


def _exec_params(command: list[str], thread_id: str = "th-1") -> t.ElicitRequestParams:
    return t.ElicitRequestFormParams.model_validate(
        {
            "message": f"Allow Codex to run `{' '.join(command)}`?",
            "requestedSchema": {"type": "object", "properties": {}},
            "threadId": thread_id,
            "codex_elicitation": "exec-approval",
            "codex_command": command,
            "codex_cwd": "C:\\tmp\\x",
        }
    )


def _patch_params(paths: list[str]) -> t.ElicitRequestParams:
    return t.ElicitRequestFormParams.model_validate(
        {
            "message": "Allow Codex to apply proposed code changes?",
            "requestedSchema": {"type": "object", "properties": {}},
            "threadId": "th-1",
            "codex_elicitation": "patch-approval",
            "codex_changes": {p: {"type": "add", "content": "x"} for p in paths},
        }
    )


def _tool_result(text: str, thread_id: str | None = None) -> t.CallToolResult:
    structured = {"threadId": thread_id, "content": text} if thread_id else None
    return t.CallToolResult.model_validate(
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
            "isError": False,
        }
    )


class CodexEventValidationFilterTests(unittest.TestCase):
    def test_suppresses_only_codex_event_validation_warning(self):
        filter_ = _CodexEventValidationFilter()
        blocked = logging.LogRecord(
            "root",
            logging.WARNING,
            "",
            0,
            "Failed to validate notification: bad. Message was: codex/event",
            (),
            None,
        )
        other_validation = logging.LogRecord(
            "root",
            logging.WARNING,
            "",
            0,
            "Failed to validate notification: another method",
            (),
            None,
        )
        other_codex = logging.LogRecord(
            "root",
            logging.WARNING,
            "",
            0,
            "codex/event disconnected",
            (),
            None,
        )

        self.assertFalse(filter_.filter(blocked))
        self.assertTrue(filter_.filter(other_validation))
        self.assertTrue(filter_.filter(other_codex))


class _FakeSession:
    """call_toolの呼び出しを記録し、用意した結果を順に返すClientSession代替。"""

    def __init__(self, results: list[t.CallToolResult]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._results = list(results)

    async def call_tool(self, name: str, arguments: dict) -> t.CallToolResult:
        self.calls.append((name, arguments))
        return self._results.pop(0)


class CaptureThreadIdTests(unittest.TestCase):
    def setUp(self):
        self.runner = CodexRunner(prompt="t", cwd=".", state=SharedState())

    def test_captures_from_structured_content(self):
        self.runner._capture_thread_id(_tool_result("ok", thread_id="th-abc"))
        self.assertEqual(self.runner._thread_id, "th-abc")

    def test_missing_structured_content_keeps_previous_value(self):
        self.runner._thread_id = "th-old"
        self.runner._capture_thread_id(_tool_result("ok"))
        self.assertEqual(self.runner._thread_id, "th-old")

    def test_non_string_thread_id_is_ignored(self):
        result = t.CallToolResult.model_validate(
            {
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"threadId": 123},
                "isError": False,
            }
        )
        self.runner._capture_thread_id(result)
        self.assertIsNone(self.runner._thread_id)


class ConverseTests(unittest.TestCase):
    def test_initial_prompt_requires_plan(self):
        runner = CodexRunner(prompt="実装して", cwd=".", state=SharedState())
        self.assertTrue(runner.initial_prompt.startswith(runner.PLAN_PROMPT_PREFIX))

    """回帰テスト: elicitationゼロで会話質問が先行しても返答できること。

    2026-07-09の実運用で、codexが最初のツール実行前に計画確認の会話質問を
    返した際、threadIdがelicitation経由でしか取得されず返答不能のまま
    セッションが終了するバグが顕在化した(structuredContentからの取得で修正)。
    """

    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_replies_using_thread_id_from_initial_result(self, mock_judge):
        mock_judge.side_effect = [
            {"decision": "allow", "raw": "ALLOW"},
            {"decision": "not_question", "raw": "NOT_A_QUESTION"},
        ]
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = CodexRunner(prompt="t", cwd=".", state=state)
        session = _FakeSession(
            [_tool_result("了解、完了しました", thread_id="th-init")]
        )

        async def scenario():
            # elicitationは一度も発生していない前提で、初回応答から取得済みとする
            runner._capture_thread_id(
                _tool_result(
                    "計画で進めてよければyを返してください", thread_id="th-init"
                )
            )
            await runner._converse(session, "計画で進めてよければyを返してください")

        asyncio.run(scenario())

        self.assertEqual(len(session.calls), 1)
        name, arguments = session.calls[0]
        self.assertEqual(name, "codex-reply")
        self.assertEqual(arguments["threadId"], "th-init")
        self.assertEqual(arguments["prompt"], "y")

    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_logs_and_stops_when_thread_id_unavailable(self, mock_judge):
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = CodexRunner(prompt="t", cwd=".", state=state)
        session = _FakeSession([])

        asyncio.run(runner._converse(session, "進めてよいですか？"))

        self.assertEqual(session.calls, [])
        self.assertTrue(any("threadId不明" in line for line in state.tail(10)))


class DescribeElicitationTests(unittest.TestCase):
    def setUp(self):
        self.runner = CodexRunner(prompt="t", cwd=".", state=SharedState())

    def test_exec_approval_joins_command(self):
        text = self.runner._describe_elicitation(
            _exec_params(["pwsh", "-Command", "npm install lodash"])
        )
        self.assertEqual(text, "pwsh -Command npm install lodash")

    def test_patch_approval_lists_paths(self):
        text = self.runner._describe_elicitation(_patch_params(["C:\\tmp\\a.txt"]))
        self.assertEqual(text, "apply_patch C:\\tmp\\a.txt")

    def test_unknown_kind_falls_back_to_message(self):
        params = t.ElicitRequestFormParams.model_validate(
            {
                "message": "何か確認",
                "requestedSchema": {"type": "object", "properties": {}},
            }
        )
        self.assertIn("何か確認", self.runner._describe_elicitation(params))


class ElicitResultTests(unittest.TestCase):
    def test_approved_carries_codex_decision_field(self):
        result = CodexRunner._elicit_result(True)
        dumped = result.model_dump()
        self.assertEqual(dumped["action"], "accept")
        # codex独自形式: トップレベルのdecisionが必須(exec_approval.rs)
        self.assertEqual(dumped["decision"], "approved")

    def test_denied_carries_codex_decision_field(self):
        result = CodexRunner._elicit_result(False)
        dumped = result.model_dump()
        self.assertEqual(dumped["action"], "decline")
        self.assertEqual(dumped["decision"], "denied")

    def test_message_is_stored_as_dict_content(self):
        result = CodexRunner._elicit_result(True, "y。補足: 条件付き")
        dumped = result.model_dump()
        self.assertEqual(dumped["content"], {"reply": "y。補足: 条件付き"})


class OnElicitationTests(unittest.TestCase):
    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_ollama_allow_approves_without_human(self, mock_judge, mock_explain):
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        state = SharedState()
        runner = CodexRunner(prompt="t", cwd=".", state=state)

        result = asyncio.run(
            runner._on_elicitation(None, _exec_params(["pwsh", "-Command", "ls"]))
        )

        self.assertEqual(result.model_dump()["decision"], "approved")
        mock_explain.assert_not_called()
        self.assertNotEqual(state.snapshot()["status"], "waiting_human")
        self.assertEqual(runner._thread_id, "th-1")

    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_patch_passes_path_facts(self, mock_judge):
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        runner = CodexRunner(prompt="t", cwd="C:\\tmp", state=SharedState())

        asyncio.run(runner._on_elicitation(None, _patch_params(["C:\\tmp\\a.txt"])))

        facts = mock_judge.call_args.kwargs["l1_facts"]
        self.assertIn("cwd配下=はい", facts)
        self.assertIn("シークレットらしい名前=いいえ", facts)

    @mock.patch("agent_wrapper.ollama_client.classify_destructive_match")
    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_destructive_command_waits_for_human_and_deny_maps_to_denied(
        self, mock_judge, mock_explain, mock_classify
    ):
        mock_explain.return_value = "再帰削除の説明"
        mock_classify.return_value = {
            "classification": "embedded_text",
            "headline": "誤検知の可能性あり(コマンド本文中の文字列に一致)",
        }
        state = SharedState()
        runner = CodexRunner(prompt="t", cwd=".", state=state)

        async def scenario():
            task = asyncio.create_task(
                runner._on_elicitation(
                    None, _exec_params(["bash", "-c", "rm -rf /tmp/x"])
                )
            )
            await asyncio.sleep(0)
            status_while_waiting = state.snapshot()
            runner.deny()
            result = await task
            return result, status_while_waiting

        result, status_while_waiting = asyncio.run(scenario())

        mock_judge.assert_not_called()
        mock_classify.assert_called_once()
        self.assertEqual(status_while_waiting["status"], "waiting_human")
        self.assertEqual(status_while_waiting["pending_kind"], "destructive")
        self.assertEqual(
            status_while_waiting["pending_detail"], "bash -c rm -rf /tmp/x"
        )
        self.assertIn("誤検知の可能性あり", status_while_waiting["pending_reason"])
        self.assertEqual(result.model_dump()["decision"], "denied")

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_escalate_waits_for_human_and_approve_maps_to_approved(
        self, mock_judge, mock_explain
    ):
        mock_judge.return_value = {"decision": "escalate", "raw": "ESCALATE"}
        mock_explain.return_value = "説明文"
        state = SharedState()
        runner = CodexRunner(prompt="t", cwd=".", state=state)

        async def scenario():
            task = asyncio.create_task(
                runner._on_elicitation(
                    None, _exec_params(["pwsh", "-Command", "some-ambiguous"])
                )
            )
            await asyncio.sleep(0)
            status_while_waiting = state.snapshot()
            runner.approve()
            result = await task
            return result, status_while_waiting

        result, status_while_waiting = asyncio.run(scenario())

        self.assertEqual(status_while_waiting["pending_kind"], "permission_escalated")
        self.assertEqual(result.model_dump()["decision"], "approved")

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_deny_message_is_attached_to_own_request(self, mock_judge, mock_explain):
        """却下メッセージが(共有変数の後読みではなく)自分の要求の応答から取られること。"""
        mock_judge.return_value = {"decision": "escalate", "raw": "ESCALATE"}
        mock_explain.return_value = "説明文"
        state = SharedState()
        runner = CodexRunner(prompt="t", cwd=".", state=state)

        async def scenario():
            task = asyncio.create_task(
                runner._on_elicitation(None, _exec_params(["pwsh", "-Command", "x"]))
            )
            await asyncio.sleep(0)
            runner.respond("deny", "危険です")
            return await task

        result = asyncio.run(scenario())
        dumped = result.model_dump()
        self.assertEqual(dumped["decision"], "denied")
        self.assertEqual(dumped["content"], {"reply": "n。理由: 危険です"})


class SandboxOptionTests(unittest.TestCase):
    def test_current_mcp_approval_policy_is_on_request(self):
        self.assertEqual(CodexRunner.APPROVAL_POLICY, "on-request")

    def test_default_sandbox_is_full_access(self):
        """既定はサンドボックスなし。根拠はCodexRunnerのクラスコメントと
        docs/agent_wrapper_permission_policy.md(承認ゲートは常に有効)。"""
        runner = CodexRunner(prompt="t", cwd=".", state=SharedState())
        self.assertEqual(runner.sandbox, "danger-full-access")

    def test_sandbox_can_be_overridden(self):
        runner = CodexRunner(
            prompt="t", cwd=".", state=SharedState(), sandbox="workspace-write"
        )
        self.assertEqual(runner.sandbox, "workspace-write")


if __name__ == "__main__":
    unittest.main()
