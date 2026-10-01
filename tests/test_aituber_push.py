import logging
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_wrapper.aituber_push import AituberPusher, InboxTracker
from agent_wrapper.approval import ApprovalBroker, ApprovalRequestInput
from agent_wrapper.server import SessionManager, WrappedSession


class ApprovalSummaryTests(unittest.TestCase):
    def test_summarizes_only_action_and_safe_first_word(self):
        from agent_wrapper.aituber_push import summarize_approval

        cases = [
            ("bash", "npm install foo", "bash: npm"),
            (
                "bash",
                'curl -H "Authorization: Bearer xxxx" https://example.test/a',
                "bash: curl",
            ),
            ("bash", "API_KEY=abc123 python x.py", "bash"),
            ("edit", r"C:\work\secret\.env", "edit: .env"),
        ]
        for action, resource, expected in cases:
            with self.subTest(resource=resource):
                self.assertEqual(summarize_approval(action, resource), expected)

    def test_never_includes_sensitive_arguments_or_unsafe_first_word(self):
        from agent_wrapper.aituber_push import summarize_approval

        cases = [
            ("bash", "python --password=supersecret script.py", "supersecret"),
            (
                "bash",
                "curl -H Authorization:Bearer-secret https://secret.test/token",
                "Bearer-secret",
            ),
            ("bash", "curl https://secret.test/path?token=secret", "secret.test"),
            ("bash", "TOKEN=hidden command", "hidden"),
            ("bash", "Authorization: Bearer top-secret", "top-secret"),
            ("bash", "--header=Authorization:top-secret", "top-secret"),
            ("bash", "", None),
            ("bash", "x" * 41 + " arg", "x" * 41),
            ("edit", r"C:\Users\alice\secrets\private-key.pem", "alice"),
            ("bash", "https://secret.test/path arg", "secret.test"),
        ]
        for action, resource, secret in cases:
            with self.subTest(resource=resource):
                summary = summarize_approval(action, resource)
                if secret is not None:
                    self.assertNotIn(secret, summary)
                self.assertIn(action, summary)

    def test_excludes_token_like_first_words_and_long_alphanumeric_runs(self):
        from agent_wrapper.aituber_push import summarize_approval

        sensitive_resources = [
            "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 run",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789 status",
            "AKIAABCDEFGHIJKLMNOP aws",
            "Bearer abc",
            "Python run",
            "a" * 25 + " run",
            "a1b2c3d4e5f6a7b8c9 run",
        ]
        for resource in sensitive_resources:
            with self.subTest(resource=resource):
                summary = summarize_approval("bash", resource)
                self.assertEqual(summary, "bash")
                self.assertNotIn(resource.split()[0], summary)

    def test_allows_common_command_names(self):
        from agent_wrapper.aituber_push import summarize_approval

        for command in ("npm", "git", "python3", "curl", "python3.exe"):
            with self.subTest(command=command):
                self.assertEqual(
                    summarize_approval("bash", f"{command} run"),
                    f"bash: {command}",
                )

    def test_path_uses_safe_basename_and_excludes_token_like_filename(self):
        from agent_wrapper.aituber_push import summarize_approval

        self.assertEqual(summarize_approval("edit", r"C:\work\.env"), "edit: .env")
        token_filename = "ghp_abcdefghijklmnopqrstuvwxyz0123456789.txt"
        summary = summarize_approval("edit", rf"C:\work\{token_filename}")
        self.assertEqual(summary, "edit")
        self.assertNotIn(token_filename, summary)


class AituberPusherTests(unittest.TestCase):
    def test_url_unset_disables_sending(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            pusher = AituberPusher()
            with mock.patch("agent_wrapper.aituber_push.requests.post") as post:
                pusher.send("approval_pending", "worker", "review")
                post.assert_not_called()

    def test_rejects_unknown_event_type(self):
        pusher = AituberPusher(url="http://127.0.0.1/event")
        with self.assertRaises(ValueError):
            pusher.send("unknown", "worker", "message")

    def test_truncates_message_and_adds_token_header(self):
        pusher = AituberPusher(url="http://127.0.0.1/event", token="secret-token")
        response = mock.Mock(status_code=202)
        with mock.patch(
            "agent_wrapper.aituber_push.requests.post", return_value=response
        ) as post:
            pusher._send_sync("approval_pending", "worker", "x" * 400)
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://127.0.0.1/event")
        self.assertEqual(kwargs["json"]["message"], "x" * 300)
        self.assertEqual(kwargs["headers"], {"X-Aituber-Token": "secret-token"})
        self.assertEqual(kwargs["timeout"], 3)

    def test_token_is_not_logged_and_failure_warns_without_raising(self):
        pusher = AituberPusher(url="http://127.0.0.1/event", token="secret-token")
        with (
            mock.patch(
                "agent_wrapper.aituber_push.requests.post",
                side_effect=RuntimeError("401 unauthorized"),
            ),
            self.assertLogs("agent_wrapper.aituber_push", logging.WARNING) as logs,
        ):
            pusher._send_sync("approval_pending", "worker", "safe")
        self.assertNotIn("secret-token", "".join(logs.output))
        self.assertIn("RuntimeError", "".join(logs.output))

    def test_send_does_not_wait_for_http_request(self):
        pusher = AituberPusher(url="http://127.0.0.1/event")
        entered = threading.Event()
        release = threading.Event()

        def blocked_post(*args, **kwargs):
            entered.set()
            release.wait(2)
            return mock.Mock(status_code=202)

        with mock.patch("agent_wrapper.aituber_push.requests.post", blocked_post):
            pusher.send("approval_pending", "worker", "safe")
            self.assertTrue(entered.wait(1))
            release.set()

    def test_inbox_tracker_reports_only_new_and_updated_files(self):
        inbox = Path("inbox")
        existing = Path("old.md")
        added = Path("new.md")
        mtimes = {existing: 1}

        def snapshot_files(path):
            return list(mtimes)

        with (
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "iterdir", snapshot_files),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                Path,
                "stat",
                autospec=True,
                side_effect=lambda path: SimpleNamespace(st_mtime_ns=mtimes[path]),
            ),
        ):
            tracker = InboxTracker(inbox)
            self.assertEqual(tracker.poll(), [])

            mtimes[added] = 2
            self.assertEqual(tracker.poll(), ["new.md"])
            self.assertEqual(tracker.poll(), [])

            mtimes[existing] = 3
            self.assertEqual(tracker.poll(), ["old.md"])

    def test_inbox_polling_sends_only_changes_after_startup_snapshot(self):
        import asyncio

        inbox = Path("inbox")
        existing = Path("old.md")
        added = Path("new.md")
        mtimes = {existing: 1}
        poll_count = 0

        def snapshot_files(path):
            return list(mtimes)

        async def sleep_then_change(_seconds):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                mtimes[added] = 2
            elif poll_count == 2:
                mtimes[existing] = 3
            else:
                raise asyncio.CancelledError

        pusher = mock.Mock()
        with (
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(Path, "iterdir", snapshot_files),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(
                Path,
                "stat",
                autospec=True,
                side_effect=lambda path: SimpleNamespace(st_mtime_ns=mtimes[path]),
            ),
            mock.patch("agent_wrapper.aituber_push.asyncio.sleep", sleep_then_change),
        ):
            from agent_wrapper.aituber_push import poll_inbox

            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(poll_inbox(inbox, pusher, "inbox"))

        self.assertEqual(
            pusher.send.call_args_list,
            [
                mock.call("result_arrived", "inbox", "new.md"),
                mock.call("result_arrived", "inbox", "old.md"),
            ],
        )

    def test_new_broker_request_sends_one_approval_notification(self):
        request = ApprovalRequestInput(
            session_id="worker",
            harness="claude",
            kind="permission",
            action="Write",
            resource="result.md --password=secret-token https://private.example",
            reason="write result",
        )
        with mock.patch("agent_wrapper.approval.aituber_pusher.send") as send:
            ApprovalBroker().submit(request)
        send.assert_called_once_with("approval_pending", "worker", "Write: result.md")
        self.assertNotIn("secret-token", send.call_args.args[2])
        self.assertNotIn("private.example", send.call_args.args[2])

    def test_stopping_session_sends_once(self):
        import asyncio

        async def run():
            manager = SessionManager()
            session = WrappedSession(
                "worker",
                "claude",
                "anthropic",
                "sonnet",
                ".",
                manager.shared_state("worker", "claude"),
            )
            manager.sessions["worker"] = session
            with mock.patch("agent_wrapper.server.aituber_pusher.send") as send:
                await manager.stop_one("worker")
                session._notify_stopped()
            send.assert_called_once_with("session_stopped", "worker", "session stopped")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
