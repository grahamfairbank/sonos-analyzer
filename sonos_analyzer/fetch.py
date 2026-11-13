"""HTTP/SOAP fetch helpers for Sonos diagnostics."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from sonos_analyzer import __version__
from sonos_analyzer.targets import is_allowed_target


DEFAULT_PORT = 1400
TIMEOUT = 8
USER_AGENT = f"sonos-analyzer/{__version__}"
# A household review is a few hundred KB even for large systems.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Speakers never redirect; following one would let a device send us anywhere."""

    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(req: urllib.request.Request) -> str:
    host = urllib.request.urlparse(req.full_url).hostname
    if not is_allowed_target(host):
        raise ValueError(f"refusing to contact {host}: not a private IPv4 address")
    with _OPENER.open(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError(f"response from {host} is larger than {MAX_RESPONSE_BYTES} bytes")
    return data.decode("utf-8", errors="replace")


@dataclass
class FetchError:
    ip: str
    message: str


def _get(url: str) -> str:
    return _open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}))


def _soap(ip: str, service_path: str, action: str, body: str) -> str:
    return soap_request(f"http://{ip}:{DEFAULT_PORT}{service_path}", action, body)


def soap_request(url: str, action: str, body: str) -> str:
    """POST a UPnP SOAP action to a private address."""
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": action,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    return _open(req)


def is_reachable(ip: str, timeout: float = 2.0) -> bool:
    """Quick check that the speaker's port accepts connections."""
    if not is_allowed_target(ip):
        return False
    try:
        with socket.create_connection((ip, DEFAULT_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_review(ip: str) -> str:
    return _get(f"http://{ip}:{DEFAULT_PORT}/support/review")


def fetch_device_description(ip: str) -> str:
    return _get(f"http://{ip}:{DEFAULT_PORT}/xml/device_description.xml")


def fetch_ath_status(ip: str) -> str:
    """Radio status. Reading it resets the speaker's PHY error counter."""
    return _get(f"http://{ip}:{DEFAULT_PORT}/status/proc/ath_rincon/status")


def fetch_zone_group_state(ip: str) -> str:
    body = (
        '<u:GetZoneGroupState xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        "</u:GetZoneGroupState>"
    )
    soap = _soap(
        ip,
        "/ZoneGroupTopology/Control",
        '"urn:schemas-upnp-org:service:ZoneGroupTopology:1#GetZoneGroupState"',
        body,
    )
    root = ET.fromstring(soap)
    for elem in root.iter():
        if elem.tag.endswith("ZoneGroupState") and elem.text:
            return elem.text
    return ""


def probe_speaker(ip: str) -> tuple[str | None, str | None, FetchError | None]:
    """Return (review_xml, device_description_xml) or an error."""
    try:
        review = fetch_review(ip)
        desc = fetch_device_description(ip)
        return review, desc, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, None, FetchError(ip=ip, message=str(exc))
