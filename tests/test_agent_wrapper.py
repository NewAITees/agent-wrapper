import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_wrapper import dashboard, notifier, ollama_client, qr_display, rules
from agent_wrapper.dashboard import make_app
from agent_wrapper.wrapper import (
    AgentWrapper,
    HumanResponse,
    SharedState,
    format_audit_log,
    summarize_log_text,
)


class LogFormattingTests(unittest.TestCase):
    def test_summarizes_long_embedded_set_content_payload(self):
        text = "A" * 450 + " | Set-Content -LiteralPath 'C:\\tmp\\report.txt'"

        result = summarize_log_text(text)

        self.assertTrue(result.startswith("A" * 200))
        self.assertIn(f"全{len(text)}文字", result)
        self.assertIn("C:\\tmp\\report.txt", result)
        self.assertLess(len(result), len(text))

    def test_keeps_short_log_text_unchanged(self):
        self.assertEqual(summarize_log_text("短いコマンド"), "短いコマンド")

    def test_audit_log_is_single_line_and_grep_friendly(self):
        result = format_audit_log("ollama", "自動承認", "safe command", "安全")

        self.assertEqual(
            result,
            "[監査] 判定者=ollama 結果=自動承認 対象=safe command 理由=安全",
        )
        self.assertNotIn("\n", result)


class CheckDestructiveTests(unittest.TestCase):
    def test_detects_rm_rf(self):
        match = rules.check_destructive("rm -rf build/ を実行しようとしています")
        self.assertTrue(match.matched)
        self.assertIn("削除", match.reason)

    def test_detects_git_reset_hard(self):
        match = rules.check_destructive("git reset --hard origin/main")
        self.assertTrue(match.matched)

    def test_detects_force_push(self):
        match = rules.check_destructive("git push origin main --force")
        self.assertTrue(match.matched)

    def test_safe_line_not_matched(self):
        match = rules.check_destructive("src/main.py を編集しました")
        self.assertFalse(match.matched)

    def test_detects_npm_install(self):
        match = rules.check_destructive(
            "::REQUEST_PERMISSION:: npm install lodash を実行してよいか"
        )
        self.assertTrue(match.matched)
        self.assertIn("インストール", match.reason)

    def test_detects_pip_install(self):
        match = rules.check_destructive("pip install requests を実行します")
        self.assertTrue(match.matched)

    def test_detects_uv_add(self):
        match = rules.check_destructive("uv add flask==3.1.3")
        self.assertTrue(match.matched)

    def test_detects_git_clone(self):
        match = rules.check_destructive(
            "git clone https://example.com/unknown/repo.git"
        )
        self.assertTrue(match.matched)

    def test_detects_docker_pull(self):
        match = rules.check_destructive("docker pull someuser/unknown-image:latest")
        self.assertTrue(match.matched)

    def test_detects_docker_run(self):
        match = rules.check_destructive("docker run someuser/unknown-image:latest")
        self.assertTrue(match.matched)

    def test_past_tense_mention_of_install_is_not_matched(self):
        match = rules.check_destructive("パッケージをインストールしました")
        self.assertFalse(match.matched)


class ParseAgentSignalTests(unittest.TestCase):
    def test_parses_stop_marker(self):
        kind, content = rules.parse_agent_signal(
            "::REQUEST_STOP:: このAPI設計を変えるべきか"
        )
        self.assertEqual(kind, "stop")
        self.assertEqual(content, "このAPI設計を変えるべきか")

    def test_parses_permission_marker(self):
        kind, content = rules.parse_agent_signal(
            "::REQUEST_PERMISSION:: npm install lodash"
        )
        self.assertEqual(kind, "permission")
        self.assertEqual(content, "npm install lodash")

    def test_returns_none_for_plain_line(self):
        kind, content = rules.parse_agent_signal("依存関係を追加しました")
        self.assertIsNone(kind)
        self.assertIsNone(content)


class SharedStateTests(unittest.TestCase):
    def test_approved_plan_is_exposed_in_snapshot(self):
        state = SharedState()
        state.set_approved_plan("目的: 安全に変更する")

        self.assertEqual(state.snapshot()["approved_plan"], "目的: 安全に変更する")

    def test_set_waiting_exposes_pending_detail(self):
        state = SharedState()
        state.set_waiting("destructive", "理由", detail="rm -rf build")

        snap = state.snapshot()

        self.assertEqual(snap["pending_detail"], "rm -rf build")

    def test_set_qr_code_populates_dashboard_fields(self):
        state = SharedState()
        with mock.patch(
            "agent_wrapper.wrapper.qr_display.make_qr_data_url",
            return_value="data:image/png;base64,abc",
        ):
            state.set_qr_code("http://192.168.1.23:28765/")
        snap = state.snapshot()
        self.assertEqual(snap["dashboard_url"], "http://192.168.1.23:28765/")
        self.assertEqual(snap["qr_code_data_url"], "data:image/png;base64,abc")

    def test_human_response_formats_messages(self):
        self.assertEqual(HumanResponse("approve").to_agent_text(), "y")
        self.assertIn("補足", HumanResponse("approve", "追加条件あり").to_agent_text())
        self.assertIn(
            "説明してください", HumanResponse("explain", "意図を教えて").to_agent_text()
        )
        self.assertIn("理由", HumanResponse("deny", "危険です").to_agent_text())


class OllamaClientTests(unittest.TestCase):
    def _mock_response(self, text):
        resp = mock.Mock()
        resp.json.return_value = {"response": text}
        resp.raise_for_status.return_value = None
        return resp

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_permission_allow(self, mock_post):
        mock_post.return_value = self._mock_response("ALLOW")
        result = ollama_client.judge_permission("npm install lodash")
        self.assertEqual(result["decision"], "allow")

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_permission_escalate(self, mock_post):
        mock_post.return_value = self._mock_response("ESCALATE")
        result = ollama_client.judge_permission("rm -rf 何か")
        self.assertEqual(result["decision"], "escalate")

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_permission_escalates_on_connection_error(self, mock_post):
        mock_post.side_effect = ConnectionError("ollama not running")
        result = ollama_client.judge_permission("npm install lodash")
        self.assertEqual(result["decision"], "escalate")

    def test_classify_destructive_match_detects_embedded_text(self):
        result = ollama_client.classify_destructive_match(
            "echo 'rm -rf /tmp/x' | Set-Content tests.txt"
        )
        self.assertEqual(result["classification"], "embedded_text")
        self.assertIn("誤検知", result["headline"])

    def test_classify_destructive_match_defaults_to_command_self(self):
        result = ollama_client.classify_destructive_match("rm -rf build")
        self.assertEqual(result["classification"], "command_self")

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_is_major_decision_defaults_true_on_error(self, mock_post):
        mock_post.side_effect = ConnectionError("ollama not running")
        self.assertTrue(ollama_client.judge_is_major_decision("何か相談"))

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_is_major_decision_true_when_model_says_yes(self, mock_post):
        mock_post.return_value = self._mock_response("YES")
        self.assertTrue(ollama_client.judge_is_major_decision("設計を変えるべきか"))

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_is_major_decision_false_only_on_clear_no(self, mock_post):
        mock_post.return_value = self._mock_response("NO")
        self.assertFalse(ollama_client.judge_is_major_decision("軽微な確認です"))

    @mock.patch("agent_wrapper.ollama_client.requests.post")
    def test_judge_is_major_decision_defaults_true_on_non_english_reply(
        self, mock_post
    ):
        mock_post.return_value = self._mock_response("はい、これは重要な設計判断です")
        self.assertTrue(ollama_client.judge_is_major_decision("設計を変えるべきか"))


class QrDisplayTests(unittest.TestCase):
    @mock.patch("agent_wrapper.qr_display.socket.socket")
    def test_get_lan_ip_falls_back_to_localhost_on_error(self, mock_socket_cls):
        mock_socket = mock.Mock()
        mock_socket.connect.side_effect = OSError("no network")
        mock_socket_cls.return_value = mock_socket
        self.assertEqual(qr_display.get_lan_ip(), "127.0.0.1")

    @mock.patch("agent_wrapper.qr_display.socket.socket")
    def test_get_lan_ip_returns_detected_address(self, mock_socket_cls):
        mock_socket = mock.Mock()
        mock_socket.getsockname.return_value = ("192.168.1.23", 12345)
        mock_socket_cls.return_value = mock_socket
        self.assertEqual(qr_display.get_lan_ip(), "192.168.1.23")

    @mock.patch("agent_wrapper.qr_display.qrcode.QRCode")
    def test_print_dashboard_qr_encodes_the_given_url(self, mock_qrcode_cls):
        mock_qr = mock.Mock()
        mock_qrcode_cls.return_value = mock_qr
        qr_display.print_dashboard_qr("http://192.168.1.23:28765/")
        mock_qr.add_data.assert_called_once_with("http://192.168.1.23:28765/")
        mock_qr.make.assert_called_once()
        mock_qr.print_ascii.assert_called_once()

    @mock.patch("agent_wrapper.qr_display.qrcode.QRCode")
    def test_make_qr_data_url_returns_png_data_uri(self, mock_qrcode_cls):
        mock_image = mock.Mock()
        mock_qr = mock.Mock()
        mock_qr.make_image.return_value = mock_image
        mock_qrcode_cls.return_value = mock_qr

        data_url = qr_display.make_qr_data_url("http://192.168.1.23:28765/")

        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        mock_image.save.assert_called_once()


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.state = SharedState()
        self.agent = mock.Mock()
        self.app = make_app(self.agent, self.state).test_client()

    def test_status_includes_qr_and_stop_reason(self):
        self.state.set_qr_code("http://192.168.1.23:28765/")
        self.state.set_stopped("normal_complete", "正常終了しました")

        response = self.app.get("/status")

        payload = response.get_json()
        self.assertEqual(payload["dashboard_url"], "http://192.168.1.23:28765/")
        self.assertIn("stop_reason", payload)
        self.assertEqual(payload["stop_reason_label"], "正常完了")

    def test_status_includes_pending_detail(self):
        self.state.set_waiting("destructive", "理由", detail="rm -rf build")

        response = self.app.get("/status")

        payload = response.get_json()
        self.assertEqual(payload["pending_detail"], "rm -rf build")

    def test_log_text_returns_full_joined_log(self):
        self.state.append_log("one")
        self.state.append_log("two")

        response = self.app.get("/log_text")

        lines = response.get_data(as_text=True).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("one"))
        self.assertTrue(lines[1].endswith("two"))

    def test_respond_posts_action_and_message(self):
        response = self.app.post(
            "/respond",
            json={"action": "explain", "message": "何をするのですか"},
        )

        self.assertEqual(response.get_json(), {"ok": True})
        self.agent.respond.assert_called_once_with(
            "explain", "何をするのですか", request_id=None
        )


class NotifierNtfyTests(unittest.TestCase):
    LOG_PATH = "notifications_test_tmp.log"

    def tearDown(self):
        if os.path.exists(self.LOG_PATH):
            os.remove(self.LOG_PATH)

    @mock.patch("agent_wrapper.notifier.requests.post")
    def test_send_ntfy_maps_level_to_integer_priority(self, mock_post):
        notifier.send_ntfy("some-topic", "high", "危険な操作", "詳細メッセージ")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["topic"], "some-topic")
        self.assertEqual(kwargs["json"]["title"], "危険な操作")
        self.assertEqual(kwargs["json"]["message"], "詳細メッセージ")
        self.assertEqual(kwargs["json"]["priority"], 5)

    @mock.patch("agent_wrapper.notifier.requests.post")
    def test_send_ntfy_swallows_connection_errors(self, mock_post):
        mock_post.side_effect = ConnectionError("ntfy unreachable")
        notifier.send_ntfy("some-topic", "low", "件名", "本文")

    @mock.patch("agent_wrapper.notifier.send_ntfy")
    def test_make_notifier_calls_send_ntfy_when_topic_given(self, mock_send_ntfy):
        send = notifier.make_notifier(log_path=self.LOG_PATH, ntfy_topic="some-topic")
        send("medium", "件名", "本文")
        mock_send_ntfy.assert_called_once_with("some-topic", "medium", "件名", "本文")

    @mock.patch("agent_wrapper.notifier.send_ntfy")
    def test_make_notifier_skips_ntfy_without_topic(self, mock_send_ntfy):
        send = notifier.make_notifier(log_path=self.LOG_PATH, ntfy_topic=None)
        send("medium", "件名", "本文")
        mock_send_ntfy.assert_not_called()


def _wait_until(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class AgentWrapperIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.state = SharedState()
        self.wrapper = AgentWrapper(
            cmd=[sys.executable, "-m", "agent_wrapper.mock_agent"],
            state=self.state,
            checkin_interval_sec=9999,
        )

    def tearDown(self):
        self.wrapper.stop()

    @mock.patch("agent_wrapper.ollama_client.explain_operation")
    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_full_flow_permission_install_destructive_stop_and_completion(
        self, mock_judge_permission, mock_explain
    ):
        mock_judge_permission.return_value = {"decision": "escalate", "raw": "ESCALATE"}
        mock_explain.return_value = "権限確認の説明(テスト用)"
        self.wrapper.start()

        waiting = _wait_until(
            lambda: self.state.snapshot()["pending_kind"] == "permission_escalated"
        )
        self.assertTrue(waiting, "permission_escalated 待ちにならなかった")
        self.assertEqual(self.state.snapshot()["status"], "waiting_human")
        mock_judge_permission.assert_called_once()
        self.wrapper.approve()

        waiting = _wait_until(
            lambda: "npm install" in (self.state.snapshot()["pending_reason"] or "")
        )
        self.assertTrue(waiting, "npm install の停止待ちにならなかった")
        self.assertEqual(self.state.snapshot()["pending_kind"], "destructive")
        mock_judge_permission.assert_called_once()
        self.wrapper.approve()

        waiting = _wait_until(
            lambda: "rm -rf" in (self.state.snapshot()["pending_reason"] or "")
        )
        self.assertTrue(waiting, "rm -rf の停止待ちにならなかった")
        self.wrapper.approve()

        waiting = _wait_until(
            lambda: self.state.snapshot()["pending_kind"] == "stop_request"
        )
        self.assertTrue(waiting, "stop_request 待ちにならなかった")
        self.wrapper.approve()

        finished = _wait_until(
            lambda: self.state.snapshot()["status"] == "stopped", timeout=10.0
        )
        self.assertTrue(finished, "プロセスが完了状態にならなかった")
        self.assertEqual(self.state.snapshot()["stop_reason"], "normal_complete")
        self.assertIn("全ての作業が完了しました", "\n".join(self.state.tail(50)))

    @mock.patch("agent_wrapper.ollama_client.judge_permission")
    def test_ollama_auto_allow_does_not_wait_for_human(self, mock_judge_permission):
        mock_judge_permission.return_value = {"decision": "allow", "raw": "ALLOW"}
        self.wrapper.start()

        waiting = _wait_until(
            lambda: "判定者=ollama 結果=自動承認" in "\n".join(self.state.tail(50))
        )
        self.assertTrue(waiting, "ollama自動承認の監査ログが出なかった")
        mock_judge_permission.assert_called_once()
        self.assertNotEqual(
            self.state.snapshot()["pending_kind"], "permission_escalated"
        )

    def test_approve_is_a_no_op_when_nothing_is_pending(self):
        self.state.status = "running"
        self.wrapper.approve()
        self.assertEqual(self.state.tail(50), [])

    def test_deny_is_a_no_op_when_nothing_is_pending(self):
        self.state.status = "running"
        self.wrapper.deny()
        self.assertEqual(self.state.tail(50), [])

    def test_respond_with_message_writes_message_to_stdin(self):
        self.state.set_waiting("destructive", "テスト用")
        stdin = mock.Mock()
        process = mock.Mock(stdin=stdin)
        self.state.process = process

        self.wrapper.respond("approve", "この条件で進めてください")

        stdin.write.assert_called_once_with("y。補足: この条件で進めてください\n")
        self.assertIn("この条件で進めてください", "\n".join(self.state.tail(10)))

    def test_deny_sets_stop_reason_candidate(self):
        self.state.set_waiting("destructive", "テスト用")
        stdin = mock.Mock()
        process = mock.Mock(stdin=stdin)
        self.state.process = process

        self.wrapper.respond("deny", "危険です")

        self.assertEqual(self.wrapper._deny_count, 1)
        stdin.write.assert_called_once_with("n。理由: 危険です\n")


class ApprovalQueueThreadingTests(unittest.TestCase):
    def setUp(self):
        self.state = SharedState()
        self.wrapper = AgentWrapper(
            cmd=[sys.executable, "-m", "agent_wrapper.mock_agent"],
            state=self.state,
            checkin_interval_sec=9999,
        )

    def tearDown(self):
        self.wrapper.stop()

    def test_first_response_only_releases_head_request(self):
        results: list[tuple[str, HumanResponse]] = []

        def wait(name: str, kind: str, reason: str) -> None:
            response = self.wrapper._wait_for_approval(kind, reason)
            results.append((name, response))

        first = threading.Thread(target=wait, args=("first", "stop_request", "先頭"))
        second = threading.Thread(
            target=wait, args=("second", "major_decision", "後続")
        )
        first.start()
        second.start()

        self.assertTrue(
            _wait_until(lambda: self.state.snapshot()["pending_count"] == 2)
        )
        snap = self.state.snapshot()
        self.assertEqual(snap["pending_kind"], "stop_request")
        self.assertEqual(snap["pending_reason"], "先頭")

        first_request_id = snap["pending_request_id"]
        self.wrapper.respond("approve", "1件目だけ", request_id=first_request_id)

        self.assertTrue(_wait_until(lambda: len(results) == 1))
        self.assertEqual(results[0][0], "first")
        self.assertEqual(results[0][1].message, "1件目だけ")
        remaining = self.state.snapshot()
        self.assertEqual(remaining["status"], "waiting_human")
        self.assertEqual(remaining["pending_kind"], "major_decision")
        self.assertEqual(remaining["pending_reason"], "後続")
        self.assertEqual(remaining["pending_count"], 1)

        self.wrapper.respond(
            "deny", "2件目", request_id=remaining["pending_request_id"]
        )
        self.assertTrue(_wait_until(lambda: len(results) == 2))
        self.assertEqual(results[1][0], "second")
        self.assertEqual(results[1][1].action, "deny")
        self.assertEqual(results[1][1].message, "2件目")

        first.join(timeout=1)
        second.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_request_id_mismatch_does_not_release_head_request(self):
        done = threading.Event()

        def wait() -> None:
            self.wrapper._wait_for_approval("stop_request", "先頭")
            done.set()

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(
            _wait_until(lambda: self.state.snapshot()["pending_count"] == 1)
        )

        self.wrapper.respond("approve", request_id=999999)
        time.sleep(0.1)
        self.assertFalse(done.is_set())
        self.assertEqual(self.state.snapshot()["pending_kind"], "stop_request")

        self.wrapper.respond(
            "approve", request_id=self.state.snapshot()["pending_request_id"]
        )
        self.assertTrue(done.wait(1.0))
        thread.join(timeout=1)


class CheckinLifecycleTests(unittest.TestCase):
    """回帰テスト: エージェント終了後に定期チェックインが承認待ちを生み続けない。

    2026-07-11の実機で、mock終了後も約10分間隔でmajor_decisionの承認待ちが
    生成され続け、承認すると死んだプロセスへのstdin書き込み(Errno 22)で
    失敗し続けるゾンビ化が発生した。
    """

    def test_read_loop_end_sets_stop_flag(self):
        state = SharedState()
        wrapper = AgentWrapper(cmd=["x"], state=state, checkin_interval_sec=1)
        proc = mock.Mock()
        proc.stdout = io.StringIO("")
        proc.wait.return_value = 0
        proc.returncode = 0
        state.process = proc

        wrapper._read_loop()

        self.assertTrue(wrapper._stop_flag.is_set())
        self.assertEqual(state.snapshot()["status"], "stopped")

    def test_checkin_loop_exits_promptly_when_stop_flag_set(self):
        state = SharedState()
        state.append_log("直近ログに方針決定っぽい文言が残っている状態")
        wrapper = AgentWrapper(cmd=["x"], state=state, checkin_interval_sec=60)
        thread = threading.Thread(target=wrapper._checkin_loop, daemon=True)
        thread.start()

        wrapper._stop_flag.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertNotEqual(state.snapshot()["status"], "waiting_human")

    def test_checkin_loop_exits_when_state_is_stopped(self):
        state = SharedState()
        state.append_log("何かのログ")
        state.set_stopped("normal_complete", "終了済み")
        wrapper = AgentWrapper(cmd=["x"], state=state, checkin_interval_sec=0)
        thread = threading.Thread(target=wrapper._checkin_loop, daemon=True)
        thread.start()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(state.snapshot()["status"], "stopped")


class DashboardPageScriptTests(unittest.TestCase):
    """PAGE内のJavaScriptの構文を検証する。

    背景: PAGEはPythonのraw文字列のため、JSの文字列/正規表現リテラル内に
    実改行が紛れ込んでもpytestでは検知できず、ブラウザで白画面/機能停止になる
    事故が2026-07-10と07-11に計3箇所で発生した。nodeがある環境では構文を直接検証する。
    """

    def test_page_script_is_valid_javascript(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node が無い環境ではスキップ")
        match = re.search(r"<script>(.*?)</script>", dashboard.PAGE, re.DOTALL)
        assert match is not None
        with tempfile.TemporaryDirectory() as tmp:
            js_path = os.path.join(tmp, "page.js")
            with open(js_path, "w", encoding="utf-8") as f:
                f.write(match.group(1))
            result = subprocess.run(
                [node, "--check", js_path], capture_output=True, text=True
            )
        self.assertEqual(
            result.returncode, 0, "PAGEのJS構文エラー: " + result.stderr[:2000]
        )


class PermissionPolicyTests(unittest.TestCase):
    def test_l3_commands_require_human(self):
        commands = [
            "git push origin main",
            "git merge release/1.0",
            "gh pr merge 42",
            "deploy production",
            "npm publish",
            "docker push example/app:1",
            "twine upload dist/*",
            "gh release create v1",
            "cargo publish",
            "gem push pkg.gem",
            "gh secret set TOKEN",
            "aws secretsmanager put-secret-value",
            "vault write secret/app token=x",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(rules.check_destructive(command).matched)

    def test_l3_mentions_do_not_match(self):
        for text in ["デプロイについて調べました", "npm publishの手順を確認しました"]:
            with self.subTest(text=text):
                self.assertFalse(rules.check_destructive(text).matched)

    def test_describe_l1_facts_for_inside_outside_and_secret(self):
        cwd = os.path.abspath("workspace")
        facts = rules.describe_l1_facts(
            ["src/app.py", os.path.join(cwd, "..", "outside.txt"), ".env"], cwd
        )
        self.assertIn("src/app.py: cwd配下=はい", facts)
        self.assertIn("outside.txt: cwd配下=いいえ", facts)
        self.assertIn(".env: cwd配下=はい, シークレットらしい名前=はい", facts)

    def test_describe_l1_facts_unknown(self):
        self.assertEqual(rules.describe_l1_facts(None, "."), "対象パス: 不明")

    @mock.patch("agent_wrapper.ollama_client._generate", return_value="ALLOW")
    def test_judge_permission_includes_l1_facts_and_policy(self, mock_generate):
        ollama_client.judge_permission("Write src/app.py", l1_facts="cwd配下=はい")
        prompt = mock_generate.call_args.args[1]
        self.assertIn("ファイルを書き込むという理由だけでESCALATE", prompt)
        self.assertIn("cwd配下=はい", prompt)

    @mock.patch("agent_wrapper.ollama_client._generate", return_value="ALLOW")
    def test_judge_permission_includes_approved_plan(self, mock_generate):
        ollama_client.judge_permission(
            "Write src/app.py", approved_plan="目的: src/app.pyを修正する"
        )

        prompt = mock_generate.call_args.args[1]
        self.assertIn("承認済みの実装計画", prompt)
        self.assertIn("目的: src/app.pyを修正する", prompt)
        self.assertIn("計画から明らかに外れる場合はESCALATE", prompt)
        self.assertIn("スケジューラ登録", prompt)

    @mock.patch("agent_wrapper.ollama_client._generate", return_value="ESCALATE")
    def test_conversational_prompt_forbids_blanket_auto_yes(self, mock_generate):
        ollama_client.judge_conversational_question("全部実行してよいですか")
        self.assertIn(
            "包括的な確認には自動でyと答えず", mock_generate.call_args.args[1]
        )

    @mock.patch("agent_wrapper.ollama_client._generate", return_value="説明")
    def test_explain_operation_requests_comprehensive_explanation(self, mock_generate):
        ollama_client.explain_operation("apply patch")
        prompt = mock_generate.call_args.args[1]
        self.assertIn("何のために", prompt)
        self.assertIn("影響・リスク・注意点", prompt)
        self.assertIn("2〜4文", prompt)


if __name__ == "__main__":
    unittest.main()
