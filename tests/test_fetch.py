"""Speaker requests: no redirects, size cap, private addresses only."""

import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from sonos_analyzer import fetch


class _Server:
    """A tiny local HTTP server; records each path it was asked for."""

    def __init__(self, respond):
        self.paths = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                respond(self)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchTests(unittest.TestCase):
    def setUp(self):
        # Loopback is normally refused; allow it so tests can use local servers.
        patcher = mock.patch.object(fetch, "is_allowed_target", lambda host: host == "127.0.0.1")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_redirect_not_followed(self):
        target = _Server(lambda h: (h.send_response(200), h.end_headers(), h.wfile.write(b"secret")))
        self.addCleanup(target.close)

        def redirect(h):
            h.send_response(302)
            h.send_header("Location", f"http://127.0.0.1:{target.port}/admin")
            h.end_headers()

        speaker = _Server(redirect)
        self.addCleanup(speaker.close)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            fetch._get(f"http://127.0.0.1:{speaker.port}/support/review")
        self.assertEqual(ctx.exception.code, 302)
        self.assertEqual(target.paths, [])

    def test_size_cap(self):
        def big(h):
            h.send_response(200)
            h.end_headers()
            h.wfile.write(b"x" * 2048)

        server = _Server(big)
        self.addCleanup(server.close)
        with mock.patch.object(fetch, "MAX_RESPONSE_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "larger than"):
                fetch._get(f"http://127.0.0.1:{server.port}/")
            # At the limit is fine.
            with mock.patch.object(fetch, "MAX_RESPONSE_BYTES", 2048):
                self.assertEqual(len(fetch._get(f"http://127.0.0.1:{server.port}/")), 2048)

    def test_user_agent(self):
        seen = {}

        def record(h):
            seen["ua"] = h.headers.get("User-Agent")
            h.send_response(200)
            h.end_headers()

        server = _Server(record)
        self.addCleanup(server.close)
        fetch._get(f"http://127.0.0.1:{server.port}/")
        self.assertTrue(seen["ua"].startswith("sonos-analyzer/"))


class TargetRefusalTests(unittest.TestCase):
    def test_public_address_never_contacted(self):
        with mock.patch.object(fetch._OPENER, "open") as opener:
            with self.assertRaisesRegex(ValueError, "not a private IPv4"):
                fetch.fetch_review("8.8.8.8")
            opener.assert_not_called()

    def test_unreachable_check_refuses_public(self):
        with mock.patch("socket.create_connection") as connect:
            self.assertFalse(fetch.is_reachable("8.8.8.8"))
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
