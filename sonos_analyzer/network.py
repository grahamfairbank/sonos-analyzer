"""Checks of the home network around the speakers: which address the analyzer uses,
and whether the router reports a private internet-side (WAN) address (double NAT).

Nothing here contacts a public address. The router is found with a UPnP search on the
local network and asked only for its WAN address; routers with UPnP turned off don't
answer, and then nothing is reported.
"""

from __future__ import annotations

import ipaddress
import os
import re
import select
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET

from sonos_analyzer.discovery import SSDP_MCAST, local_addresses
from sonos_analyzer.fetch import _get, soap_request
from sonos_analyzer.parse import Gateway, RouterCheck
from sonos_analyzer.targets import clean_text, is_allowed_target

GATEWAY_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)
WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
MAX_GATEWAYS = 4
LOCATION_RE = re.compile(r"^location:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def in_container() -> bool:
    """Inside Docker the analyzer's own address says nothing about the user's phone."""
    return os.path.exists("/.dockerenv") or os.environ.get("SONOS_ANALYZER_DATA") == "/data"


def source_address(ip: str) -> str:
    """The local address this computer uses to reach ip (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((ip, 1400))
            return sock.getsockname()[0]
    except OSError:
        return ""


def classify_wan(address: str) -> str:
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return "unknown"
    if ip.version != 4:
        return "unknown"
    if ip.is_unspecified:
        return "none"
    if ip in _CGNAT:
        return "cgnat"
    if is_allowed_target(address.strip()) or ip.is_private:
        return "private"
    return "public"


def search_gateways(timeout: float = 2.0) -> list[str]:
    """UPnP search for internet gateways; returns their description URLs."""
    local_ips = local_addresses() or [""]
    socks: list[socket.socket] = []
    locations: list[str] = []
    try:
        for local_ip in local_ips:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            try:
                sock.bind((local_ip, 0))
                if local_ip:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                for target in GATEWAY_TARGETS:
                    sock.sendto(_search_message(target), SSDP_MCAST)
            except OSError:
                sock.close()
                continue
            socks.append(sock)
        end = time.monotonic() + timeout
        while socks and (remaining := end - time.monotonic()) > 0:
            readable, _, _ = select.select(socks, [], [], min(0.5, remaining))
            for sock in readable:
                try:
                    data, _addr = sock.recvfrom(65535)
                except OSError:
                    continue
                text = data.decode("utf-8", errors="replace")
                if "InternetGatewayDevice" not in text:
                    continue
                match = LOCATION_RE.search(text)
                if match and match.group(1) not in locations:
                    locations.append(match.group(1))
    finally:
        for sock in socks:
            sock.close()
    return locations


def _search_message(target: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {target}\r\n\r\n"
    ).encode()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def describe_gateway(location: str) -> Gateway:
    """Read the gateway's description and ask its WAN service for the external address."""
    host = urllib.parse.urlparse(location).hostname or ""
    gateway = Gateway(ip=host)
    if not is_allowed_target(host):
        gateway.error = "not a private address"
        return gateway
    try:
        root = ET.fromstring(_get(location))
        names = {_local(e.tag): (e.text or "").strip() for e in root.iter() if _local(e.tag) in {"friendlyName", "modelName", "manufacturer"}}
        gateway.model = clean_text(names.get("modelName") or names.get("friendlyName") or "")[:80]
        control = None
        for service in root.iter():
            if _local(service.tag) != "service":
                continue
            fields = {_local(c.tag): (c.text or "").strip() for c in service}
            if fields.get("serviceType") in WAN_SERVICES and fields.get("controlURL"):
                control = (fields["serviceType"], fields["controlURL"])
                break
        if not control:
            gateway.error = "no WAN connection service"
            return gateway
        service_type, control_url = control
        url = urllib.parse.urljoin(location, control_url)
        # The control address must be the same router that answered.
        if urllib.parse.urlparse(url).hostname != host:
            gateway.error = "control address points elsewhere"
            return gateway
        reply = soap_request(
            url,
            f'"{service_type}#GetExternalIPAddress"',
            f'<u:GetExternalIPAddress xmlns:u="{service_type}"></u:GetExternalIPAddress>',
        )
        external = ""
        for elem in ET.fromstring(reply).iter():
            if _local(elem.tag) == "NewExternalIPAddress":
                external = (elem.text or "").strip()
        gateway.wan = classify_wan(external)
        if gateway.wan in {"private", "cgnat"}:
            gateway.wan_ip = external
    except Exception as exc:  # noqa: BLE001 - a router that answers badly is only "unknown"
        gateway.error = clean_text(str(exc))[:120]
    return gateway


def check_router(timeout: float = 2.0) -> RouterCheck:
    check = RouterCheck(searched=True)
    seen: set[str] = set()
    for location in search_gateways(timeout):
        host = urllib.parse.urlparse(location).hostname or ""
        if host in seen or not is_allowed_target(host):
            continue
        seen.add(host)
        check.gateways.append(describe_gateway(location))
        if len(check.gateways) >= MAX_GATEWAYS:
            break
    return check
