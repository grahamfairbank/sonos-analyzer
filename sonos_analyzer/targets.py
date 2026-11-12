"""Which addresses the analyzer is allowed to contact."""

from __future__ import annotations

import ipaddress
import re

# Carrier-grade NAT space, used by some home and mesh networks (and Tailscale).
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def is_allowed_target(host: str | None) -> bool:
    """Only private IPv4 literals are probed, whether typed, discovered or reported by a speaker."""
    try:
        address = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    return address.version == 4 and (address.is_private or address in _SHARED_ADDRESS_SPACE) and not (
        address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local
    )


def clean_text(value: str) -> str:
    """Drop control characters from speaker-supplied text (stops terminal escape injection)."""
    return _CONTROL_CHARS.sub("", value)
