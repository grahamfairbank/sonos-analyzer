"""Web front end for sonos-analyzer (stdlib only)."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from sonos_analyzer import __version__
from sonos_analyzer.analyze import AnalysisReport, analyze
from sonos_analyzer.collect import collect_snapshot
from sonos_analyzer.discovery import discover_speakers, off_network_hint
from sonos_analyzer.report import render_html, report_payload
from sonos_analyzer.targets import is_allowed_target


MAX_BODY = 16 * 1024
MAX_IPS = 64
MAX_DISCOVER_TIMEOUT = 15.0

DISCOVERY_INTERFACE = os.environ.get("SONOS_ANALYZER_INTERFACE") or None
# Host names the page may be opened by, besides IP addresses, localhost, *.local and this
# machine's own name. Anything else is refused, which stops DNS-rebinding attacks.
EXTRA_ALLOWED_HOSTS = {
    name.strip().lower().rstrip(".")
    for name in os.environ.get("SONOS_ANALYZER_ALLOWED_HOSTS", "").split(",")
    if name.strip()
}

_scan_lock = threading.Lock()
_last_report: AnalysisReport | None = None

# Set by the desktop app (sonos_analyzer.app): adds a Quit button to the page.
APP_MODE = False
on_quit = None
last_activity = time.monotonic()


class ScanError(Exception):
    def __init__(self, status: HTTPStatus, message: str, fetch_errors: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fetch_errors = fetch_errors or []


def parse_ips(raw: object) -> list[str]:
    """Validate user-supplied speaker addresses; only private LAN IPs are probed."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        raise ScanError(HTTPStatus.BAD_REQUEST, "ips must be a list or comma-separated string")
    if len(raw) > MAX_IPS:
        raise ScanError(HTTPStatus.BAD_REQUEST, f"Too many IPs (max {MAX_IPS})")

    ips: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not is_allowed_target(text):
            raise ScanError(HTTPStatus.BAD_REQUEST, f"Not a private IPv4 address: {item}")
        if text not in ips:
            ips.append(text)
    return ips


def _own_names() -> set[str]:
    names = {"localhost"}
    for name in (socket.gethostname(), socket.getfqdn()):
        if name:
            names.add(name.lower().rstrip("."))
    return names


_OWN_NAMES = _own_names()


def host_allowed(host_header: str | None) -> bool:
    """Accept IP literals, localhost, *.local, this machine's names and configured names."""
    if not host_header:
        return True  # HTTP/1.0 clients; browsers always send Host
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else host
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return (
        host in _OWN_NAMES
        or host in EXTRA_ALLOWED_HOSTS
        or host.endswith((".localhost", ".local"))
    )


def run_scan(ips: list[str], discover_timeout: float) -> AnalysisReport:
    global _last_report

    if not _scan_lock.acquire(blocking=False):
        raise ScanError(HTTPStatus.CONFLICT, "A scan is already running. Try again in a moment.")
    try:
        if not ips:
            try:
                found = discover_speakers(timeout=discover_timeout, interface=DISCOVERY_INTERFACE)
            except (OSError, ImportError) as exc:
                raise ScanError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Discovery failed on interface {DISCOVERY_INTERFACE or 'all'}: {exc}",
                ) from None
            ips = [d.ip for d in found]
            if not ips:
                raise ScanError(
                    HTTPStatus.NOT_FOUND,
                    "No Sonos speakers discovered. Enter one speaker's IP address above and scan "
                    "again; one speaker is enough to find the rest. Discovery does not work under "
                    "Docker Desktop, or when this computer is on a different network from the speakers.",
                )

        snapshot = collect_snapshot(ips)
        if not snapshot.speakers:
            hint = off_network_hint(ips)
            raise ScanError(
                HTTPStatus.BAD_GATEWAY,
                "Could not read diagnostics from any speaker." + (f" {hint}" if hint else ""),
                snapshot.fetch_errors,
            )

        _last_report = analyze(snapshot)
        return _last_report
    finally:
        _scan_lock.release()


class Handler(BaseHTTPRequestHandler):
    server_version = f"sonos-analyzer/{__version__}"
    # Drop connections that stall, so slow or half-open clients cannot hold threads.
    timeout = 30

    def _refuse_foreign_host(self) -> bool:
        """Refuse names that could come from DNS rebinding; True when the request was refused."""
        host = self.headers.get("Host")
        if host_allowed(host):
            return False
        self._send_json(
            HTTPStatus.FORBIDDEN,
            {
                "error": f"This page cannot be opened as {host}. Open it by IP address, or add the name to "
                "SONOS_ANALYZER_ALLOWED_HOSTS."
            },
        )
        return True

    def _refuse_cross_origin(self) -> bool:
        """For state-changing requests, any Origin must be this page's own; True when refused.

        Both http:// and https:// count as the page's own origin for the same Host, so the UI
        also works behind a reverse proxy that terminates TLS (e.g. https://sonos.home.lan).
        """
        origin = self.headers.get("Origin")
        host = (self.headers.get("Host") or "").lower()
        if origin is None or origin.lower() in (f"http://{host}", f"https://{host}"):
            return False
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin requests are not allowed"})
        return True

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        global last_activity
        if self._refuse_foreign_host():
            return
        last_activity = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            page = resources.files("sonos_analyzer").joinpath("static/index.html").read_bytes()
            self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
        elif path == "/api/app":
            self._send_json(HTTPStatus.OK, {"app": APP_MODE, "version": __version__})
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok", "version": __version__})
        elif path == "/api/report":
            if _last_report is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No scan has been run yet."})
            else:
                self._send_json(HTTPStatus.OK, report_payload(_last_report))
        elif path == "/report.html":
            if _last_report is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No scan has been run yet."})
            else:
                self._send(
                    HTTPStatus.OK,
                    render_html(_last_report).encode("utf-8"),
                    "text/html; charset=utf-8",
                    {"Content-Disposition": 'attachment; filename="sonos-report.html"'},
                )
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        global last_activity
        if self._refuse_foreign_host() or self._refuse_cross_origin():
            return
        last_activity = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path == "/api/quit" and APP_MODE and on_quit:
            # JSON only, like /api/analyze, so other sites cannot stop the app.
            if self.headers.get_content_type() != "application/json":
                self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
                return
            self._send_json(HTTPStatus.OK, {"stopping": True})
            threading.Thread(target=on_quit, daemon=True).start()
            return
        if path != "/api/analyze":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        # Requiring JSON forces a CORS preflight, so other sites cannot trigger scans.
        if self.headers.get_content_type() != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
            return

        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ScanError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError:
                raise ScanError(HTTPStatus.BAD_REQUEST, "Content-Length must be a number") from None
            if length < 0:
                raise ScanError(HTTPStatus.BAD_REQUEST, "Content-Length must not be negative")
            if length > MAX_BODY:
                raise ScanError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ScanError(HTTPStatus.BAD_REQUEST, "Request body is not valid JSON") from None
            if not isinstance(body, dict):
                raise ScanError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")

            ips = parse_ips(body.get("ips"))
            try:
                timeout = float(body.get("discover_timeout", 3.0))
            except (TypeError, ValueError):
                raise ScanError(HTTPStatus.BAD_REQUEST, "discover_timeout must be a number") from None
            timeout = min(max(timeout, 1.0), MAX_DISCOVER_TIMEOUT)

            report = run_scan(ips, timeout)
        except ScanError as exc:
            self._send_json(exc.status, {"error": exc.message, "fetch_errors": exc.fetch_errors})
            return
        except Exception as exc:  # noqa: BLE001 - report instead of dropping the connection
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Scan failed: {exc}"})
            return

        self._send_json(HTTPStatus.OK, report_payload(report))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - http.server signature
        # The desktop app has no console on macOS, and a request log is noise for its users.
        if not APP_MODE:
            super().log_message(format, *args)

    def _send_json(self, status: HTTPStatus, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the sonos-analyzer web UI.")
    parser.add_argument(
        "--host",
        default=os.environ.get("SONOS_ANALYZER_HOST", "127.0.0.1"),
        help="Address to bind (default: 127.0.0.1, or $SONOS_ANALYZER_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SONOS_ANALYZER_PORT", "8400")),
        help="Port to listen on (default: 8400, or $SONOS_ANALYZER_PORT)",
    )
    args = parser.parse_args(argv)

    # As PID 1 in a container Python ignores SIGTERM unless a handler is installed.
    signal.signal(signal.SIGTERM, _raise_interrupt)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"sonos-analyzer web UI listening on http://{args.host}:{args.port} "
        f"(discovery interface: {DISCOVERY_INTERFACE or 'all'})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
