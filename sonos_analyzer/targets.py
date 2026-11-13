"""Which addresses the analyzer is allowed to contact."""

from __future__ import annotations

import ipaddress
import re

# RFC 1918 private ranges, plus carrier-grade NAT space used by some home and mesh
# networks (and Tailscale). Listed explicitly: Python's is_private also covers
# documentation and reserved ranges that no speaker uses.
_ALLOWED_NETWORKS = [
    ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
]
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def is_allowed_target(host: str | None) -> bool:
    """Only private IPv4 literals are probed, whether typed, discovered or reported by a speaker."""
    try:
        address = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    return address.version == 4 and any(address in net for net in _ALLOWED_NETWORKS)


def clean_text(value: str) -> str:
    """Drop control characters from speaker-supplied text (stops terminal escape injection)."""
    return _CONTROL_CHARS.sub("", value)
