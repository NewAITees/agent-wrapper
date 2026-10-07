"""API authentication through the real HTTP handler, without socket access."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_wrapper.approval import ApprovalRequestInput
from agent_wrapper.server import SessionManager, WrappedSession, _make_http_handler


class ApiTokenTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.manager = SessionManager()
        self.session = WrappedSession(
            "orchestrator",
            "claude",
            "",
            "",
            ".",
            self.manager.shared_state("orchestrator", "claude"),
        )
        self.manager.sessions["orchestrator"] = self.session
        self.session._runner = mock.Mock()

    def request(
        self,
        path,
        authorization=None,
        *,
        method="POST",
        extra_headers=(),
        content_type="application/json",
        text="instruction",
    ):
        request = self.manager.approval_broker.submit(
            ApprovalRequestInput(
                "orchestrator", "claude", "permission", "test", "x", "test"
            )
        )
        path = path.replace("REQUEST", request.request_id)
        body = json.dumps(
            {"text": text, "session_id": "orchestrator", "action": "approve"}
        ).encode()
        headers = [
            "Host: 127.0.0.1:28765",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
        ]
        if authorization is not None:
            headers.append(f"Authorization: {authorization}")
        headers.extend(extra_headers)
        raw = (
            f"{method} {path} HTTP/1.0\r\n" + "\r\n".join(headers) + "\r\n\r\n"
        ).encode() + body
        output = io.BytesIO()
        connection = mock.Mock()
        connection.makefile.side_effect = [io.BytesIO(raw), output]
        connection.sendall.side_effect = output.write
        _make_http_handler(self.manager, [])(
            connection,
            ("127.0.0.1", 1),
            SimpleNamespace(server_address=("127.0.0.1", 28765)),
        )
        response = output.getvalue().decode()
        status = int(response.split(" ", 2)[1])
        return status, response, request

    def test_missing_wrong_empty_and_duplicate_credentials_are_rejected(self):
        with mock.patch.dict(
            "os.environ", {"AGENT_WRAPPER_API_TOKEN": "private-test-token"}
        ):
            for path in ("/send/orchestrator", "/approvals/REQUEST/respond"):
                for authorization in (
                    None,
                    "Bearer wrong",
                    "Bearer ",
                    "",
                    "Basic private-test-token",
                ):
                    with self.subTest(path=path, authorization=authorization):
                        with self.assertLogs(
                            "agent_wrapper.server", level="WARNING"
                        ) as logs:
                            status, response, request = self.request(
                                path, authorization
                            )
                        self.assertEqual(status, 401)
                        self.assertEqual(request.status, "pending")
                        self.assertNotIn(
                            "private-test-token", response + str(logs.output)
                        )
                status, _, _ = self.request(
                    path,
                    "Bearer private-test-token",
                    extra_headers=("Authorization: Bearer private-test-token",),
                )
                self.assertEqual(status, 401)

    def test_correct_token_and_unset_or_empty_token_allow_both_routes(self):
        for token in ("private-test-token", "", None):
            environment = {} if token is None else {"AGENT_WRAPPER_API_TOKEN": token}
            with mock.patch.dict("os.environ", environment, clear=True):
                for path in ("/send/orchestrator", "/approvals/REQUEST/respond"):
                    with self.subTest(token=token, path=path):
                        status, _, _ = self.request(
                            path, "Bearer private-test-token" if token else None
                        )
                        self.assertEqual(status, 200)

    def test_get_does_not_require_token(self):
        with mock.patch.dict(
            "os.environ", {"AGENT_WRAPPER_API_TOKEN": "private-test-token"}
        ):
            for path in ("/sessions", "/approvals"):
                status, _, _ = self.request(path, method="GET")
                self.assertEqual(status, 200)

    def test_authenticated_requests_still_obey_browser_boundaries(self):
        with mock.patch.dict(
            "os.environ", {"AGENT_WRAPPER_API_TOKEN": "private-test-token"}
        ):
            for header, status in (
                ("Origin: https://evil.example", 403),
                ("Sec-Fetch-Site: cross-site", 403),
                ("Content-Type: text/plain", 415),
            ):
                with self.subTest(header=header):
                    actual, _, _ = self.request(
                        "/send/orchestrator",
                        "Bearer private-test-token",
                        content_type="text/plain"
                        if status == 415
                        else "application/json",
                        extra_headers=() if status == 415 else (header,),
                    )
                    self.assertEqual(actual, status)

    def test_mutation_auth_comparison_is_detected(self):
        with mock.patch("agent_wrapper.server.hmac.compare_digest", return_value=True):
            result = unittest.TestResult()
            ApiTokenTests(
                "test_missing_wrong_empty_and_duplicate_credentials_are_rejected"
            ).run(result)
        self.assertTrue(result.failures)
        self.assertFalse(result.errors)

    def test_mutation_boundary_removal_is_detected(self):
        with mock.patch(
            "agent_wrapper.server._request_boundary_error", return_value=None
        ):
            result = unittest.TestResult()
            ApiTokenTests(
                "test_authenticated_requests_still_obey_browser_boundaries"
            ).run(result)
        self.assertTrue(result.failures)
        self.assertFalse(result.errors)
