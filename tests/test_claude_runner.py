import asyncio
import unittest
from unittest import mock

from claude_agent_sdk.types import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    ToolPermissionContext,
)

from agent_wrapper.runners.claude_runner import ClaudeRunner
from agent_wrapper.runners.tool_describe import GATED_TOOLS, describe_tool_call
from agent_wrapper.wrapper import SharedState


class DescribeToolCallTests(unittest.TestCase):
    def test_bash_returns_command(self):
        text = describe_tool_call("Bash", {"command": "npm install lodash"})
        self.assertEqual(text, "npm install lodash")

    def test_write_returns_tool_and_path(self):
        text = describe_tool_call("Write", {"file_path": "/tmp/a.txt", "content": "x"})
        self.assertEqual(text, "Write /tmp/a.txt")

    def test_edit_returns_tool_and_path(self):
        text = describe_tool_call("Edit", {"file_path": "/tmp/a.txt"})
        self.assertEqual(text, "Edit /tmp/a.txt")

    def test_other_tool_falls_back_to_repr(self):
        text = describe_tool_call("WebFetch", {"url": "https://example.com"})
        self.assertIn("WebFetch", text)
        self.assertIn("https://example.com", text)

    def test_gated_tools_are_bash_write_edit(self):
        self.assertEqual(GATED_TOOLS, frozenset({"Bash", "Write", "Edit"}))


def _run_scenario(coro_factory, *, resolve: str, message: str = ""):
    state = SharedState()
    runner = ClaudeRunner(prompt="test", cwd=".", state=state)

    async def scenario():
        task = asyncio.create_task(coro_factory(runner))
        await asyncio.sleep(0)
        self_status = state.snapshot()
        if resolve == "approve":
            runner.respond("approve", message)
        elif resolve == "deny":
            runner.respond("deny", message)
        elif resolve == "explain":
            runner.respond("explain", message)
        result = await task
        return result, self_status

    result, status_while_waiting = asyncio.run(scenario())
    return runner, state, result, status_while_waiting


class CanUseToolTests(unittest.TestCase):
    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_allow_via_ollama_never_waits_for_human(self, mock_judge, mock_explain):
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)

        result = asyncio.run(
            runner._can_use_tool("Bash", {"command": "ls"}, ToolPermissionContext())
        )

        self.assertIsInstance(result, PermissionResultAllow)
        mock_explain.assert_not_called()
        self.assertNotEqual(state.snapshot()["status"], "waiting_human")

    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_write_passes_structural_l1_facts(self, mock_judge):
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        runner = ClaudeRunner(prompt="test", cwd="C:\\work", state=SharedState())

        asyncio.run(
            runner._can_use_tool(
                "Write", {"file_path": "src/app.py"}, ToolPermissionContext()
            )
        )

        facts = mock_judge.call_args.kwargs["l1_facts"]
        self.assertIn("cwd配下=はい", facts)
        self.assertIn("シークレットらしい名前=いいえ", facts)

    @mock.patch("agent_wrapper.ollama_client.classify_destructive_match")
    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_destructive_match_bypasses_ollama_decision_and_waits_for_human(
        self, mock_judge, mock_explain, mock_classify
    ):
        mock_explain.return_value = "このコマンドはディレクトリを削除します。"
        mock_classify.return_value = {
            "classification": "embedded_text",
            "headline": "誤検知の可能性あり(コマンド本文中の文字列に一致)",
        }

        async def call(runner):
            return await runner._can_use_tool(
                "Bash", {"command": "rm -rf /tmp/x"}, ToolPermissionContext()
            )

        runner, state, result, status_while_waiting = _run_scenario(
            call, resolve="approve"
        )

        mock_judge.assert_not_called()
        mock_explain.assert_called_once()
        mock_classify.assert_called_once()
        self.assertEqual(status_while_waiting["status"], "waiting_human")
        self.assertEqual(status_while_waiting["pending_kind"], "destructive")
        self.assertEqual(status_while_waiting["pending_detail"], "rm -rf /tmp/x")
        self.assertIn(
            "誤検知の可能性あり",
            status_while_waiting["pending_reason"],
        )
        self.assertIn(
            "このコマンドはディレクトリを削除します。",
            status_while_waiting["pending_reason"],
        )
        self.assertIsInstance(result, PermissionResultAllow)

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_ollama_escalate_waits_for_human_and_can_be_denied(
        self, mock_judge, mock_explain
    ):
        mock_judge.return_value = {"decision": "escalate", "raw": "ESCALATE"}
        mock_explain.return_value = "説明文"

        async def call(runner):
            return await runner._can_use_tool(
                "Bash", {"command": "some-ambiguous-command"}, ToolPermissionContext()
            )

        runner, state, result, status_while_waiting = _run_scenario(
            call, resolve="deny", message="危険なのでやめて"
        )

        mock_judge.assert_called_once()
        mock_explain.assert_called_once()
        self.assertEqual(status_while_waiting["status"], "waiting_human")
        self.assertEqual(status_while_waiting["pending_kind"], "permission_escalated")
        self.assertIsInstance(result, PermissionResultDeny)
        self.assertIn("危険", result.message)

    def test_approve_and_deny_are_no_ops_when_nothing_pending(self):
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        runner.approve()
        runner.deny()
        self.assertEqual(state.snapshot()["status"], "starting")


class PreToolUseHookTests(unittest.TestCase):
    def test_returns_ask_for_pretooluse_event(self):
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        output = asyncio.run(
            runner._pre_tool_use_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {},
                },
                "tool-use-1",
                {"signal": None},
            )
        )
        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "ask",
        )

    def test_returns_empty_for_other_events(self):
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        output = asyncio.run(
            runner._pre_tool_use_hook(
                {"hook_event_name": "PostToolUse"},
                "tool-use-1",
                {"signal": None},
            )
        )
        self.assertEqual(output, {})


class _FakeClient:
    def __init__(self, turns):
        self._turns = turns
        self._call_count = 0
        self.sent: list[str] = []

    async def query(self, text: str) -> None:
        self.sent.append(text)

    async def receive_response(self):
        messages = self._turns[self._call_count]
        self._call_count += 1
        for m in messages:
            yield m


class _FakeThinkingBlock:
    def __init__(self, summary: str):
        self.summary = summary


class _FakeToolResultBlock:
    def __init__(self, text: str):
        self.text = text


def _text_turn(text: str) -> list[AssistantMessage]:
    return [AssistantMessage(content=[TextBlock(text=text)], model="claude-sonnet-5")]


class ConverseTests(unittest.TestCase):
    def test_initial_prompt_requires_plan(self):
        runner = ClaudeRunner(prompt="実装して", cwd=".", state=SharedState())
        self.assertTrue(runner.initial_prompt.startswith(runner.PLAN_PROMPT_PREFIX))
        self.assertTrue(runner.initial_prompt.endswith("実装して"))

    def test_plan_approval_saves_plan_and_returns_explicit_approval(self):
        async def scenario():
            state = SharedState()
            runner = ClaudeRunner(prompt="t", cwd=".", state=state)
            task = asyncio.create_task(runner._request_plan_approval("計画全文"))
            await asyncio.sleep(0)
            self.assertEqual(state.snapshot()["pending_kind"], "plan_approval")
            self.assertEqual(state.snapshot()["pending_detail"], "計画全文")
            runner.respond("approve", "補足事項")
            reply = await task
            self.assertEqual(reply, "y(計画を承認します)。補足: 補足事項")
            self.assertEqual(state.snapshot()["approved_plan"], "計画全文")

        asyncio.run(scenario())

    def test_plan_rejection_does_not_save_plan(self):
        async def scenario():
            state = SharedState()
            runner = ClaudeRunner(prompt="t", cwd=".", state=state)
            task = asyncio.create_task(runner._request_plan_approval("旧計画"))
            await asyncio.sleep(0)
            runner.respond("deny", "範囲を狭めてください")
            reply = await task
            self.assertEqual(reply, "n。理由: 範囲を狭めてください")
            self.assertIsNone(state.snapshot()["approved_plan"])

        asyncio.run(scenario())

    def test_revised_plan_is_approved_after_rejection(self):
        async def scenario():
            state = SharedState()
            runner = ClaudeRunner(prompt="t", cwd=".", state=state)

            first = asyncio.create_task(runner._request_plan_approval("旧計画"))
            await asyncio.sleep(0)
            runner.respond("explain", "検証方法を追加してください")
            self.assertEqual(
                await first, "説明してください。検証方法を追加してください"
            )

            revised = asyncio.create_task(runner._request_plan_approval("改訂計画"))
            await asyncio.sleep(0)
            self.assertEqual(state.snapshot()["pending_detail"], "改訂計画")
            runner.approve()
            self.assertEqual(await revised, "y(計画を承認します)")
            self.assertEqual(state.snapshot()["approved_plan"], "改訂計画")

        asyncio.run(scenario())

    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_allow_auto_replies_then_stops_on_non_question(self, mock_judge):
        mock_judge.side_effect = [
            {"decision": "allow", "raw": "ALLOW"},
            {"decision": "not_question", "raw": "NOT_A_QUESTION"},
        ]
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        client = _FakeClient(
            [_text_turn("続けてよいですか？(y/n)"), _text_turn("完了しました。")]
        )

        asyncio.run(runner._converse(client))

        self.assertEqual(mock_judge.call_count, 2)
        self.assertEqual(client.sent, ["y"])
        self.assertTrue(
            any("判定者=ollama 結果=自動承認" in line for line in state.tail(50))
        )

    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_escalates_at_max_turns_and_stops_when_human_denies(self, mock_judge):
        """上限到達時に勝手に打ち切らず人間に継続可否を確認する(却下→終了)。"""
        mock_judge.return_value = {"decision": "allow", "raw": "ALLOW"}
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        max_turns = ClaudeRunner._MAX_CONVERSATIONAL_TURNS
        client = _FakeClient([_text_turn("続けますか？(y/n)")] * (max_turns + 1))

        async def scenario():
            task = asyncio.create_task(runner._converse(client))
            for _ in range(500):
                await asyncio.sleep(0)
                if state.snapshot()["pending_kind"] == "conversation_limit":
                    break
            self.assertEqual(state.snapshot()["pending_kind"], "conversation_limit")
            runner.deny()
            await task

        asyncio.run(scenario())

        self.assertEqual(len(client.sent), max_turns)
        self.assertEqual(state.snapshot()["stop_reason"], "max_conversation_turns")

    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_continues_past_max_turns_when_human_approves(self, mock_judge):
        """上限到達時に人間が承認すれば会話を継続する(カウンタリセット)。"""
        max_turns = ClaudeRunner._MAX_CONVERSATIONAL_TURNS
        mock_judge.side_effect = [{"decision": "allow", "raw": "ALLOW"}] * (
            max_turns + 1
        ) + [{"decision": "not_question", "raw": "NOT_A_QUESTION"}]
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        client = _FakeClient([_text_turn("続けますか？(y/n)")] * (max_turns + 2))

        async def scenario():
            task = asyncio.create_task(runner._converse(client))
            for _ in range(500):
                await asyncio.sleep(0)
                if state.snapshot()["pending_kind"] == "conversation_limit":
                    break
            self.assertEqual(state.snapshot()["pending_kind"], "conversation_limit")
            runner.approve()
            await task

        asyncio.run(scenario())

        # 承認により6件目の返信が送信され、その後not_questionで正常に抜ける
        self.assertEqual(len(client.sent), max_turns + 1)
        self.assertNotEqual(state.snapshot()["stop_reason"], "max_conversation_turns")

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_escalate_waits_for_human_before_replying(self, mock_judge, mock_explain):
        mock_judge.side_effect = [
            {"decision": "escalate", "raw": "ESCALATE"},
            {"decision": "not_question", "raw": "NOT_A_QUESTION"},
        ]
        mock_explain.return_value = "このコマンドは複数のファイルを削除します。"
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        client = _FakeClient(
            [_text_turn("削除してもいいですか？"), _text_turn("完了しました。")]
        )

        async def scenario():
            task = asyncio.create_task(runner._converse(client))
            await asyncio.sleep(0)
            status_while_waiting = state.snapshot()
            runner.respond("approve", "READMEだけ確認してください")
            await task
            return status_while_waiting

        status_while_waiting = asyncio.run(scenario())

        self.assertEqual(status_while_waiting["status"], "waiting_human")
        self.assertEqual(
            status_while_waiting["pending_kind"], "conversational_escalated"
        )
        mock_explain.assert_called_once()
        self.assertEqual(client.sent, ["y。補足: READMEだけ確認してください"])

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_conversational_question")
    def test_escalate_denied_sends_rejection_reply(self, mock_judge, mock_explain):
        mock_judge.side_effect = [
            {"decision": "escalate", "raw": "ESCALATE"},
            {"decision": "not_question", "raw": "NOT_A_QUESTION"},
        ]
        mock_explain.return_value = "このコマンドは複数のファイルを削除します。"
        state = SharedState()
        state.set_approved_plan("既存テスト用計画")
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        client = _FakeClient(
            [_text_turn("削除してもいいですか？"), _text_turn("完了しました。")]
        )

        async def scenario():
            task = asyncio.create_task(runner._converse(client))
            await asyncio.sleep(0)
            runner.respond("deny", "その理由を先に説明してください")
            await task

        asyncio.run(scenario())

        self.assertEqual(client.sent, ["n。理由: その理由を先に説明してください"])

    def test_handle_message_logs_unknown_blocks(self):
        state = SharedState()
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)
        message = AssistantMessage(
            content=[_FakeThinkingBlock("要点"), _FakeToolResultBlock("result")],
            model="claude-sonnet-5",
        )

        runner._handle_message(message)

        log = "\n".join(state.tail(10))
        self.assertIn("FakeThinkingBlock", log)
        self.assertIn("FakeToolResultBlock", log)


class ApprovalQueueAsyncTests(unittest.TestCase):
    def test_first_response_only_releases_head_request(self):
        state = SharedState()
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)

        async def scenario():
            first = asyncio.create_task(
                runner._wait_for_approval("stop_request", "先頭")
            )
            second = asyncio.create_task(
                runner._wait_for_approval("major_decision", "後続")
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            snap = state.snapshot()
            self.assertEqual(snap["pending_kind"], "stop_request")
            self.assertEqual(snap["pending_reason"], "先頭")
            self.assertEqual(snap["pending_count"], 2)

            runner.respond(
                "approve", "1件目だけ", request_id=snap["pending_request_id"]
            )
            first_response = await first
            self.assertFalse(second.done())

            remaining = state.snapshot()
            self.assertEqual(remaining["status"], "waiting_human")
            self.assertEqual(remaining["pending_kind"], "major_decision")
            self.assertEqual(remaining["pending_reason"], "後続")
            self.assertEqual(remaining["pending_count"], 1)

            runner.respond("deny", "2件目", request_id=remaining["pending_request_id"])
            second_response = await second
            return first_response, second_response

        first_response, second_response = asyncio.run(scenario())
        self.assertEqual(first_response.action, "approve")
        self.assertEqual(first_response.message, "1件目だけ")
        self.assertEqual(second_response.action, "deny")
        self.assertEqual(second_response.message, "2件目")

    def test_request_id_mismatch_does_not_release_head_request(self):
        state = SharedState()
        runner = ClaudeRunner(prompt="test", cwd=".", state=state)

        async def scenario():
            first = asyncio.create_task(
                runner._wait_for_approval("stop_request", "先頭")
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            runner.respond("approve", request_id=999999)
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            snap = state.snapshot()
            self.assertEqual(snap["pending_kind"], "stop_request")
            runner.respond("approve", request_id=snap["pending_request_id"])
            return await first

        response = asyncio.run(scenario())
        self.assertEqual(response.action, "approve")


if __name__ == "__main__":
    unittest.main()
