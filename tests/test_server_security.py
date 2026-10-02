"""HTTP/WS boundary security regression tests."""

import http.client
import http.server
import json
import threading
import unittest
from unittest import mock

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from agent_wrapper.server import SessionManager, _make_http_handler, _ws_process_request


class HttpRequestBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.manager = SessionManager()
        handler = _make_http_handler(self.manager, [])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method,
        path,
        *,
        host=None,
        origin=None,
        content_type=None,
        fetch_site=None,
        fetch_site_values=None,
    ):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = {}
        if host is not None:
            headers["Host"] = host
        if origin is not None:
            headers["Origin"] = origin
        if content_type is not None:
            headers["Content-Type"] = content_type
        if fetch_site is not None:
            headers["Sec-Fetch-Site"] = fetch_site
        body = b"{}" if method == "POST" else None
        if fetch_site_values is not None:
            connection.putrequest(
                method, path, skip_host=True, skip_accept_encoding=True
            )
            connection.putheader("Host", f"127.0.0.1:{self.server.server_address[1]}")
            if origin is not None:
                connection.putheader("Origin", origin)
            if content_type is not None:
                connection.putheader("Content-Type", content_type)
            for value in fetch_site_values:
                connection.putheader("Sec-Fetch-Site", value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
        else:
            connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, response.getheader("Content-Type"), payload)
        connection.close()
        return result

    def test_rejects_host_for_another_origin(self):
        port = self.server.server_address[1]
        status, content_type, payload = self.request(
            "GET", "/sessions", host=f"evil.example:{port}"
        )
        self.assertEqual(status, 403)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        self.assertEqual(json.loads(payload), {"error": "request host is not allowed"})

    def test_rejects_cross_origin_post(self):
        status, _, _ = self.request(
            "POST",
            "/run-tests",
            origin="http://evil.example",
            content_type="application/json",
        )
        self.assertEqual(status, 403)

    def test_rejects_cross_site_and_same_site_post(self):
        port = self.server.server_address[1]
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                status, _, _ = self.request(
                    "POST",
                    "/run-tests",
                    origin=f"http://127.0.0.1:{port}",
                    content_type="application/json",
                    fetch_site=site,
                )
                self.assertEqual(status, 403)

    def test_rejects_text_plain_post(self):
        status, _, _ = self.request("POST", "/run-tests", content_type="text/plain")
        self.assertEqual(status, 415)

    def test_allows_json_post_without_origin(self):
        status, content_type, payload = self.request(
            "POST", "/run-tests", content_type="Application/JSON; charset=utf-8"
        )
        self.assertEqual(status, 400)
        self.assertIn("application/json", content_type)
        self.assertEqual(
            json.loads(payload), {"error": "no active workspace (call /start first)"}
        )

    def test_allows_same_origin_and_localhost_hosts(self):
        port = self.server.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            with self.subTest(host=host):
                status, _, _ = self.request(
                    "GET", "/sessions", host=host, origin=f"http://localhost:{port}"
                )
                self.assertEqual(status, 200)

    def test_allows_same_origin_json_post(self):
        port = self.server.server_address[1]
        status, _, _ = self.request(
            "POST",
            "/run-tests",
            host=f"127.0.0.1:{port}",
            origin=f"http://127.0.0.1:{port}",
            content_type="application/json; charset=UTF-8",
            fetch_site="same-origin",
        )
        self.assertEqual(status, 400)

    def test_sec_fetch_site_requires_one_allowlisted_value(self):
        port = self.server.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        rejected = (
            ("same-origin, cross-site",),
            ("cross-site, same-origin",),
            ("same-origin", "cross-site"),
            ("unknown-value",),
            ("",),
            ("cross-site",),
            ("same-site",),
        )
        with (
            mock.patch(
                "agent_wrapper.server._pick_folder", return_value="C:/work"
            ) as pick_folder,
            mock.patch(
                "agent_wrapper.server.models_for", return_value={}
            ) as models_for,
            mock.patch(
                "agent_wrapper.server._opencode_models", return_value={}
            ) as opencode_models,
        ):
            for method in ("GET", "POST", "PUT", "DELETE"):
                for values in rejected:
                    with self.subTest(method=method, values=values):
                        if len(values) == 1:
                            status, _, _ = self.request(
                                method,
                                "/pick-folder"
                                if method == "POST"
                                else "/models?harness=opencode",
                                origin=origin,
                                content_type="application/json"
                                if method == "POST"
                                else None,
                                fetch_site=values[0],
                            )
                        else:
                            status, _, _ = self.request(
                                method,
                                "/pick-folder"
                                if method == "POST"
                                else "/models?harness=opencode",
                                origin=origin,
                                content_type="application/json"
                                if method == "POST"
                                else None,
                                fetch_site_values=values,
                            )
                        self.assertEqual(status, 403)
            pick_folder.assert_not_called()
            models_for.assert_not_called()
            opencode_models.assert_not_called()

    def test_sec_fetch_site_allows_only_single_same_origin_or_none_value(self):
        for site in ("same-origin", "Same-Origin", "none", "NONE"):
            with self.subTest(site=site):
                status, _, _ = self.request("GET", "/sessions", fetch_site=site)
                self.assertEqual(status, 200)

        status, _, _ = self.request("GET", "/sessions")
        self.assertEqual(status, 200)

    def test_pick_folder_is_post_only_and_post_opens_dialog_once(self):
        with mock.patch(
            "agent_wrapper.server._pick_folder", return_value="C:/work"
        ) as pick:
            status, _, _ = self.request("GET", "/pick-folder")
            self.assertEqual(status, 405)
            pick.assert_not_called()

            status, _, payload = self.request(
                "POST", "/pick-folder", content_type="application/json"
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload), {"path": "C:/work"})
            pick.assert_called_once_with()

    def test_pick_folder_post_rejects_untrusted_browser_headers(self):
        port = self.server.server_address[1]
        with mock.patch("agent_wrapper.server._pick_folder") as pick:
            for headers in (
                {"origin": "http://evil.example"},
                {"origin": f"http://127.0.0.1:{port}", "fetch_site": "cross-site"},
                {"origin": f"http://127.0.0.1:{port}", "fetch_site": "same-site"},
            ):
                with self.subTest(headers=headers):
                    status, _, _ = self.request(
                        "POST",
                        "/pick-folder",
                        content_type="application/json",
                        **headers,
                    )
                    self.assertEqual(status, 403)
            pick.assert_not_called()

    def test_rejects_cross_site_gets_before_endpoint_side_effects(self):
        paths = (
            "/sessions",
            "/recent-folders",
            "/models?harness=opencode",
            "/sessions/worker/output",
            "/index.html",
        )
        with (
            mock.patch("agent_wrapper.server.models_for") as models_for,
            mock.patch("agent_wrapper.server._opencode_models") as opencode_models,
            mock.patch("agent_wrapper.server._pick_folder") as pick_folder,
        ):
            for path in paths:
                for fetch_site in ("cross-site", "same-site"):
                    with self.subTest(path=path, fetch_site=fetch_site):
                        status, _, _ = self.request("GET", path, fetch_site=fetch_site)
                        self.assertEqual(status, 403)
                with self.subTest(path=path, origin="evil"):
                    status, _, _ = self.request(
                        "GET", path, origin="http://evil.example"
                    )
                    self.assertEqual(status, 403)
            models_for.assert_not_called()
            opencode_models.assert_not_called()
            pick_folder.assert_not_called()

    def test_allows_originless_and_same_origin_gets(self):
        port = self.server.server_address[1]
        for headers in (
            {},
            {"host": f"127.0.0.1:{port}"},
            {"host": f"localhost:{port}"},
            {"fetch_site": "same-origin"},
            {"fetch_site": "none"},
            {"origin": f"http://127.0.0.1:{port}"},
            {"origin": f"http://localhost:{port}"},
        ):
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/sessions", **headers)
                self.assertEqual(status, 200)

    def test_direct_static_page_navigation_with_fetch_site_none_is_allowed(self):
        status, content_type, payload = self.request(
            "GET", "/index.html", fetch_site="none"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn(b"<!doctype html", payload.lower())


class WebSocketRequestBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_cross_origin_websocket_handshake(self):
        async def handler(websocket):
            await websocket.close()

        async with serve(
            handler, "127.0.0.1", 0, process_request=_ws_process_request
        ) as server:
            port = server.sockets[0].getsockname()[1]
            with self.assertRaises(InvalidStatus):
                async with connect(
                    f"ws://127.0.0.1:{port}/worker", origin="http://evil.example"
                ):
                    self.fail("cross-origin WebSocket was accepted")

    async def test_allows_localhost_origin_and_originless_client(self):
        async def handler(websocket):
            await websocket.close()

        with mock.patch("agent_wrapper.server.HTTP_PORT", 18765):
            async with serve(
                handler, "127.0.0.1", 0, process_request=_ws_process_request
            ) as server:
                port = server.sockets[0].getsockname()[1]
                for hostname in ("127.0.0.1", "localhost"):
                    for origin in (
                        "http://localhost:18765",
                        "http://127.0.0.1:18765",
                        None,
                    ):
                        with self.subTest(hostname=hostname, origin=origin):
                            async with connect(
                                f"ws://{hostname}:{port}/worker",
                                host="127.0.0.1",
                                origin=origin,
                            ):
                                pass

    async def test_rejects_non_same_origin_fetch_site(self):
        async def handler(websocket):
            await websocket.close()

        with mock.patch("agent_wrapper.server.HTTP_PORT", 18765):
            async with serve(
                handler, "127.0.0.1", 0, process_request=_ws_process_request
            ) as server:
                port = server.sockets[0].getsockname()[1]
                for site in ("cross-site", "same-site"):
                    with self.subTest(site=site), self.assertRaises(InvalidStatus):
                        async with connect(
                            f"ws://localhost:{port}/worker",
                            host="127.0.0.1",
                            origin="http://localhost:18765",
                            additional_headers={"Sec-Fetch-Site": site},
                        ):
                            self.fail("non same-origin WebSocket was accepted")

    async def test_websocket_fetch_site_requires_one_allowlisted_value(self):
        async def handler(websocket):
            await websocket.close()

        rejected = (
            (("Sec-Fetch-Site", "same-origin, cross-site"),),
            (("Sec-Fetch-Site", "cross-site, same-origin"),),
            (("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")),
            (("Sec-Fetch-Site", "unknown-value"),),
            (("Sec-Fetch-Site", ""),),
        )
        with mock.patch("agent_wrapper.server.HTTP_PORT", 18765):
            async with serve(
                handler, "127.0.0.1", 0, process_request=_ws_process_request
            ) as server:
                port = server.sockets[0].getsockname()[1]
                for headers in rejected:
                    with (
                        self.subTest(headers=headers),
                        self.assertRaises(InvalidStatus),
                    ):
                        async with connect(
                            f"ws://localhost:{port}/worker",
                            host="127.0.0.1",
                            origin="http://localhost:18765",
                            additional_headers=headers,
                        ):
                            self.fail("invalid Sec-Fetch-Site WebSocket was accepted")
                for headers in (
                    None,
                    (("Sec-Fetch-Site", "same-origin"),),
                    (("Sec-Fetch-Site", "Same-Origin"),),
                    (("Sec-Fetch-Site", "none"),),
                ):
                    with self.subTest(allowed_headers=headers):
                        async with connect(
                            f"ws://localhost:{port}/worker",
                            host="127.0.0.1",
                            origin="http://localhost:18765",
                            additional_headers=headers,
                        ):
                            pass

    async def test_rejects_websocket_with_an_untrusted_host(self):
        async def handler(websocket):
            await websocket.close()

        async with serve(
            handler, "127.0.0.1", 0, process_request=_ws_process_request
        ) as server:
            port = server.sockets[0].getsockname()[1]
            with self.assertRaises(InvalidStatus):
                async with connect(
                    f"ws://evil.example:{port}/worker",
                    host="127.0.0.1",
                    proxy=None,
                ):
                    self.fail("untrusted Host was accepted")
