"""Discover Sonos speakers on the local network via SSDP."""

from __future__ import annotations

import re
import socket
import struct
from dataclasses import dataclass


SSDP_MCAST = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:schemas-upnp-org:device:ZonePlayer:1"
SEARCH_MESSAGE = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_MCAST[0]}:{SSDP_MCAST[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    f"ST: {SEARCH_TARGET}\r\n"
    "\r\n"
).encode("ascii")

LOCATION_RE = re.compile(r"^LOCATION:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class DiscoveredSpeaker:
    ip: str
    location_url: str


def discover_speakers(timeout: float = 3.0) -> list[DiscoveredSpeaker]:
    """Send SSDP M-SEARCH and collect Sonos ZonePlayer endpoints."""
    seen: set[str] = set()
    speakers: list[DiscoveredSpeaker] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        sock.bind(("", 0))

        # Join multicast on a best-effort interface.
        try:
            mreq = struct.pack("=4s4s", socket.inet_aton(SSDP_MCAST[0]), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            pass

        deadline = socket.getdefaulttimeout()
        import time

        end = time.monotonic() + timeout
        sock.sendto(SEARCH_MESSAGE, SSDP_MCAST)

        while time.monotonic() < end:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(0.5, remaining))
            try:
                data, _addr = sock.recvfrom(65535)
            except TimeoutError:
                continue

            text = data.decode("utf-8", errors="replace")
            match = LOCATION_RE.search(text)
            if not match:
                continue

            location = match.group(1).strip()
            ip = _ip_from_location(location)
            if not ip or ip in seen:
                continue
            seen.add(ip)
            speakers.append(DiscoveredSpeaker(ip=ip, location_url=location))
    finally:
        sock.close()

    return sorted(speakers, key=lambda s: s.ip)


def _ip_from_location(location: str) -> str | None:
    # http://192.168.1.165:1400/xml/device_description.xml
    match = re.match(r"https?://([^:/]+)", location)
    return match.group(1) if match else None
