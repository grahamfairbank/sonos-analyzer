"""The web server's request checks, with scans replaced by the test household."""

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from sonos_analyzer import web
from tests.helpers import snapshot


class HostAllowedTests(unittest.TestCase):
    def test_allowed(self):
        with mock.patch.object(web, "EXTRA_ALLOWED_HOSTS", {"sonos.home.lan"}):
            for host in [None, "", "192.168.1.5", "192.168.1.5:8400", "[::1]:8400", "localhost", "localhost:8400",
                         "app.localhost", "nas.local", "nas.local.", "sonos.home.lan", "SONOS.home.lan:443"]:
                with self.subTest(host=host):
                    self.assertTrue(web.host_allowed(host))

    def test_refused(self):
        with mock.patch.object(web, "EXTRA_ALLOWED_HOSTS", {"sonos.home.lan"}):
            for host in ["evil.com", "evil.com:8400", "sonos.home.lan.evil.com", "localhost.evil.com", "local"]:
                with self.subTest(host=host):
                    self.assertFalse(web.host_allowed(host))


class ParseIpsTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(web.parse_ips("192.168.1.2, 192.168.1.3 192.168.1.2"), ["192.168.1.2", "192.168.1.3"])
        self.assertEqual(web.parse_ips(["10.0.0.1"]), ["10.0.0.1"])
        self.assertEqual(web.parse_ips(None), [])

    def test_refused(self):
        for raw in ["8.8.8.8", "127.0.0.1", ["a"] * (web.MAX_IPS + 1), 42, {"ip": "10.0.0.1"}]:
            with self.subTest(raw=raw), self.assertRaises(web.ScanError):
                web.parse_ips(raw)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)
        for name, value in {"_last_report": None, "APP_MODE": False, "on_quit": None,
                            "EXTRA_ALLOWED_HOSTS": {"sonos.home.lan"}}.items():
            p = mock.patch.object(web, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(web.Handler, "log_message", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(web, "collect_snapshot", side_effect=lambda ips, **kw: snapshot())
        self.collect = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(web, "discover_speakers", return_value=[])
        self.discover = p.start()
        self.addCleanup(p.stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, body=None, headers=None, raw_length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        headers = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if body is not None and raw_length is None:
            headers.setdefault("Content-Length", str(len(body)))
        if raw_length is not None:
            headers["Content-Length"] = raw_length
        for k, v in headers.items():
            if v is not None:
                conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def scan(self, body=b'{"ips": ["192.168.50.10"]}', **headers):
        return self.request("POST", "/api/analyze", body, {"Content-Type": "application/json", **headers})

    def test_page_and_health(self):
        resp, data = self.request("GET", "/")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"Sonos Analyzer", data)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(json.loads(self.request("GET", "/healthz")[1])["status"], "ok")

    def test_foreign_host_refused(self):
        resp, data = self.request("GET", "/", headers={"Host": "evil.com"})
        self.assertEqual(resp.status, 403)
        self.assertIn(b"cannot be opened as evil.com", data)
        self.assertEqual(self.scan(Host="evil.com")[0].status, 403)

    def test_scan_and_report(self):
        self.assertEqual(self.request("GET", "/api/report")[0].status, 404)
        resp, data = self.scan()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(data)["speaker_count"], 11)
        self.assertEqual(self.request("GET", "/api/report")[0].status, 200)
        resp, data = self.request("GET", "/report.html")
        self.assertEqual(resp.status, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))

    def test_origin_check(self):
        cases = {
            None: 200,
            f"http://127.0.0.1:{self.port}": 200,
            f"https://127.0.0.1:{self.port}": 200,
            "https://evil.com": 403,
            "http://127.0.0.1": 403,
            "null": 403,
        }
        for origin, status in cases.items():
            with self.subTest(origin=origin):
                self.assertEqual(self.scan(Origin=origin)[0].status, status)

    def test_origin_behind_tls_proxy(self):
        resp, _ = self.scan(Host="sonos.home.lan", Origin="https://sonos.home.lan")
        self.assertEqual(resp.status, 200)
        resp, _ = self.scan(Host="sonos.home.lan", Origin="https://sonos.home.lan:8443")
        self.assertEqual(resp.status, 403)

    def test_content_type_required(self):
        resp, _ = self.request("POST", "/api/analyze", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)

    def test_content_length(self):
        for raw, status in [(None, 411), ("-1", 400), ("abc", 400), (str(web.MAX_BODY + 1), 413)]:
            with self.subTest(length=raw):
                resp, _ = self.request("POST", "/api/analyze", None, {"Content-Type": "application/json"},
                                       raw_length=raw if raw is not None else None)
                self.assertEqual(resp.status, status)

    def test_bad_bodies(self):
        for body, status in [(b"not json", 400), (b"[]", 400), (b'{"ips": "8.8.8.8"}', 400),
                             (b'{"discover_timeout": "x"}', 400)]:
            with self.subTest(body=body):
                self.assertEqual(self.scan(body)[0].status, status)
        self.collect.assert_not_called()

    def test_no_speakers_discovered(self):
        resp, data = self.scan(b"{}")
        self.assertEqual(resp.status, 404)
        self.assertIn(b"No Sonos speakers discovered", data)

    def test_scan_already_running(self):
        web._scan_lock.acquire()
        try:
            self.assertEqual(self.scan()[0].status, 409)
        finally:
            web._scan_lock.release()

    def test_history_clear(self):
        import tempfile
        from sonos_analyzer import history

        resp, _ = self.request("POST", "/api/history/clear", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(resp.status, 404)  # history off
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(web, "HISTORY", history.History(tmp)):
            self.assertEqual(self.scan()[0].status, 200)
            self.assertEqual(self.scan()[0].status, 200)
            resp, data = self.request("GET", "/api/report")
            self.assertTrue(json.loads(data)["monitoring"]["history"])
            self.assertIsNotNone(json.loads(data)["history"]["previous_at"])
            resp, _ = self.request("POST", "/api/history/clear", b"{}", {"Content-Type": "application/json", "Origin": "https://evil.com"})
            self.assertEqual(resp.status, 403)
            resp, data = self.request("POST", "/api/history/clear", b"{}", {"Content-Type": "application/json"})
            self.assertEqual(json.loads(data)["cleared"], 2)

    def test_app_key_required(self):
        with mock.patch.object(web, "APP_KEY", "launch-key"):
            self.assertEqual(self.request("GET", "/")[0].status, 200)
            self.assertEqual(self.request("GET", "/healthz")[0].status, 200)
            resp, data = self.request("GET", "/api/app")
            self.assertTrue(json.loads(data)["key_required"])
            resp, data = self.request("GET", "/api/report")
            self.assertEqual(resp.status, 401)
            self.assertTrue(json.loads(data)["key_required"])
            self.assertEqual(self.request("GET", "/api/report", headers={"X-Analyzer-Key": "wrong"})[0].status, 401)
            self.assertEqual(self.request("GET", "/report.html")[0].status, 401)
            self.assertEqual(self.scan()[0].status, 401)
            self.collect.assert_not_called()
            self.assertEqual(self.scan(**{"X-Analyzer-Key": "launch-key"})[0].status, 200)
            self.assertEqual(self.request("GET", "/api/report", headers={"X-Analyzer-Key": "launch-key"})[0].status, 200)

    def test_no_key_in_server_mode(self):
        self.assertEqual(self.request("GET", "/api/report")[0].status, 404)
        self.assertFalse(json.loads(self.request("GET", "/api/app")[1])["key_required"])

    def test_quit_only_in_app_mode(self):
        self.assertEqual(self.request("POST", "/api/quit", b"{}", {"Content-Type": "application/json"})[0].status, 404)
        quit_called = threading.Event()
        with mock.patch.object(web, "APP_MODE", True), mock.patch.object(web, "on_quit", quit_called.set):
            resp, _ = self.request("POST", "/api/quit", b"{}", {"Content-Type": "text/plain"})
            self.assertEqual(resp.status, 415)
            resp, _ = self.request("POST", "/api/quit", b"{}", {"Content-Type": "application/json", "Origin": "https://evil.com"})
            self.assertEqual(resp.status, 403)
            self.assertFalse(quit_called.is_set())
            resp, _ = self.request("POST", "/api/quit", b"{}", {"Content-Type": "application/json"})
            self.assertEqual(resp.status, 200)
            self.assertTrue(quit_called.wait(2))


if __name__ == "__main__":
    unittest.main()
