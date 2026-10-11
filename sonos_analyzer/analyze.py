"""Analyze parsed Sonos diagnostics and produce findings."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics


# PHY error rate: flag radios well above the household's typical level.
PHY_RATE_OUTLIER_FACTOR = 5
PHY_RATE_MIN_PER_SEC = 500
NOISE_FLOOR_ELEVATED_DBM = -85
RX_DROP_RATIO_PCT = 20
INTERFACE_ERROR_RATIO_PCT = 0.01

ROLE_ORDER = ["Front", "Left", "Right", "Left surround", "Right surround", "Sub"]
# 5 GHz 80 MHz blocks, by first channel
BLOCKS_5GHZ = [(36, 48), (52, 64), (100, 112), (116, 128), (132, 144), (149, 161)]


@dataclass
class Finding:
    severity: str  # high, medium, low, info, ok
    title: str
    detail: str
    affected_ips: list[str] = field(default_factory=list)
    category: str = ""  # rooms, firmware, wireless, wired, calibration
    # One entry per affected speaker: {"name", "ip", "note"}
    affected: list[dict] = field(default_factory=list)
    recommendation: str = ""

    def __post_init__(self) -> None:
        if not self.affected_ips:
            self.affected_ips = [a["ip"] for a in self.affected]


@dataclass
class AnalysisReport:
    generated_at: str
    speaker_count: int
    firmware_versions: dict[str, list[str]]
    speakers: list[dict]
    findings: list[Finding]
    recommendations: list[str]
    snapshot: HouseholdSnapshot
    room_count: int = 0
    firmware: list[dict] = field(default_factory=list)
    wireless: list[dict] = field(default_factory=list)
    connections: dict = field(default_factory=dict)


def analyze(snapshot: HouseholdSnapshot) -> AnalysisReport:
    speakers = sorted(snapshot.speakers.values(), key=_sort_key)
    rooms = _rooms(speakers)

    firmware_versions: dict[str, list[str]] = {}
    for sp in speakers:
        firmware_versions.setdefault(sp.software_version or "unknown", []).append(sp.ip_address)

    findings: list[Finding] = []
    findings += _firmware_findings(speakers, rooms)
    findings += _room_findings(speakers, rooms)
    findings += _wireless_findings(speakers)
    findings += _wired_findings(speakers)
    findings += _calibration_findings(rooms)

    recommendations = [f.recommendation for f in sorted(findings, key=_severity_rank) if f.recommendation]
    if not any(f.severity in {"high", "medium"} for f in findings):
        recommendations.append("No significant issues detected.")
    recommendations.append("Reserve DHCP addresses for each speaker so IPs stay stable.")
    if any(_radio_use(sp) == "wifi" and _band(_active_freq(sp)) == "2.4 GHz" for sp in speakers):
        recommendations.append("Keep router 2.4 GHz on 20 MHz channel width for Sonos compatibility.")

    return AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        speaker_count=len(speakers),
        firmware_versions=firmware_versions,
        speakers=[_speaker_to_dict(sp) for sp in speakers],
        findings=sorted(findings, key=_severity_rank),
        recommendations=list(dict.fromkeys(recommendations)),
        snapshot=snapshot,
        room_count=len(rooms),
        firmware=_firmware_summary(speakers),
        wireless=_wireless_map(speakers),
        connections=_connections(speakers),
    )


# --- Rooms and bonded sets -------------------------------------------------


def _room_key(sp: SpeakerDiagnostics) -> str:
    return sp.room_uid or sp.local_uid or sp.ip_address


def _room_name(sp: SpeakerDiagnostics) -> str:
    return sp.room_name or sp.zone_name or sp.ip_address


def _label(sp: SpeakerDiagnostics) -> str:
    """Room name plus role, e.g. 'Living Room (Left surround)'."""
    return f"{_room_name(sp)} ({sp.role})" if sp.role else _room_name(sp)


def _sort_key(sp: SpeakerDiagnostics) -> tuple:
    role = ROLE_ORDER.index(sp.role) if sp.role in ROLE_ORDER else len(ROLE_ORDER)
    return (_room_name(sp).lower(), _room_key(sp), role, sp.ip_address)


def _rooms(speakers: list[SpeakerDiagnostics]) -> dict[str, list[SpeakerDiagnostics]]:
    rooms: dict[str, list[SpeakerDiagnostics]] = {}
    for sp in speakers:
        rooms.setdefault(_room_key(sp), []).append(sp)
    return rooms


def _room_findings(
    speakers: list[SpeakerDiagnostics], rooms: dict[str, list[SpeakerDiagnostics]]
) -> list[Finding]:
    findings: list[Finding] = []
    wifi_channels = {_channel(_active_freq(sp)) for sp in speakers if _path(sp) == "wifi"}

    for members in rooms.values():
        if len(members) < 2:
            continue
        name = _room_name(members[0])
        if members[0].bond == "home_theater":
            findings += _home_theater_findings(name, members, wifi_channels)
        else:
            findings += _stereo_pair_findings(name, members)
    return findings


def _stereo_pair_findings(name: str, members: list[SpeakerDiagnostics]) -> list[Finding]:
    paths = {_path(sp) for sp in members}
    wired = [sp for sp in members if _path(sp) == "wired"]
    wireless = [sp for sp in members if _path(sp) != "wired"]
    affected = _affected(members, lambda sp: _connection_summary(sp))

    if wired and wireless:
        return [
            Finding(
                severity="high",
                category="rooms",
                title=f"Stereo pair '{name}' is mixed wired + wireless",
                detail=(
                    f"{_names(wired)} is on Ethernet while {_names(wireless)} is wireless. "
                    "Stereo pairs stay in sync best when both speakers share the same network path."
                ),
                affected=affected,
                recommendation=(
                    f"{name}: wire both speakers of the pair, or unplug Ethernet and run both on Wi-Fi."
                ),
            )
        ]

    channels = {_channel(_active_freq(sp)) for sp in members if _active_freq(sp)}
    if paths == {"wifi"} and len(channels) > 1:
        return [
            Finding(
                severity="medium",
                category="rooms",
                title=f"Stereo pair '{name}' is split across Wi-Fi channels",
                detail=(
                    "The two speakers are connected to different access points or bands. "
                    "Audio between them then crosses your router instead of staying on one radio."
                ),
                affected=affected,
                recommendation=(
                    f"{name}: get both speakers onto the same access point and band "
                    "(move them closer to one AP, or power-cycle the one on the other channel)."
                ),
            )
        ]

    path = "wired" if paths == {"wired"} else "on the same Wi-Fi channel" if paths == {"wifi"} else "on the same path"
    return [
        Finding(
            severity="ok",
            category="rooms",
            title=f"Stereo pair '{name}' is consistent",
            detail=f"Both speakers are {path}.",
            affected=affected,
        )
    ]


def _home_theater_findings(
    name: str, members: list[SpeakerDiagnostics], wifi_channels: set[int | None]
) -> list[Finding]:
    findings: list[Finding] = []
    primary = members[0]
    satellites = members[1:]
    linked = [sp for sp in satellites if _path(sp) == "satellite"]
    via_wifi = [sp for sp in satellites if _path(sp) == "wifi"]
    link_freq = primary.ht_channel_mhz or next((_active_freq(sp) for sp in linked), "")
    link_channel = _channel(link_freq)

    if via_wifi:
        findings.append(
            Finding(
                severity="medium",
                category="rooms",
                title=f"Home theater '{name}' has satellites on home Wi-Fi",
                detail=(
                    "Surrounds and subs normally connect straight to the soundbar over its own 5 GHz link. "
                    "These are going through your router instead, which adds delay and dropouts."
                ),
                affected=_affected(via_wifi, _connection_summary),
                recommendation=f"{name}: power-cycle the listed satellites so they rejoin the soundbar directly.",
            )
        )

    shared = _block_5ghz(link_channel)
    overlapping = sorted(ch for ch in wifi_channels if ch and shared and _block_5ghz(ch) == shared)
    if linked and overlapping:
        findings.append(
            Finding(
                severity="low",
                category="rooms",
                title=f"Home theater '{name}' surround link overlaps your Wi-Fi",
                detail=(
                    f"The soundbar's surround link is on channel {link_channel}, in the same 80 MHz block as "
                    f"Wi-Fi channel {', '.join(map(str, overlapping))} used by other speakers."
                ),
                affected=_affected([primary], lambda sp: f"link on channel {link_channel}"),
                recommendation=f"{name}: move the router's 5 GHz channel away from {link_channel}.",
            )
        )

    if _path(primary) == "wifi" and satellites:
        findings.append(
            Finding(
                severity="low",
                category="rooms",
                title=f"Home theater '{name}' soundbar is on Wi-Fi",
                detail=(
                    "The soundbar uses its radio both for your Wi-Fi and for its surrounds. "
                    "Wiring it leaves the radio free for the surround link."
                ),
                affected=_affected([primary], _connection_summary),
                recommendation=f"{name}: connect the soundbar by Ethernet if a cable is available.",
            )
        )

    if not findings:
        link = f" on a dedicated link (channel {link_channel})" if linked and link_channel else ""
        findings.append(
            Finding(
                severity="ok",
                category="rooms",
                title=f"Home theater '{name}' is set up cleanly",
                detail=(
                    f"{primary.model_name or 'Primary'} is {_connection_summary(primary).lower()}, "
                    f"with {len(satellites)} satellite(s){link}."
                ),
                affected=_affected(members, lambda sp: sp.model_name),
            )
        )
    return findings


# --- Firmware ---------------------------------------------------------------


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def _model(sp: SpeakerDiagnostics) -> str:
    return sp.model_name or sp.model_number or sp.hardware_version or "Unknown model"


def _firmware_summary(speakers: list[SpeakerDiagnostics]) -> list[dict]:
    newest_by_model: dict[str, tuple[int, ...]] = {}
    for sp in speakers:
        version = _version_tuple(sp.software_version)
        newest_by_model[_model(sp)] = max(newest_by_model.get(_model(sp), ()), version)

    groups: dict[str, list[SpeakerDiagnostics]] = {}
    for sp in speakers:
        groups.setdefault(sp.software_version or "unknown", []).append(sp)

    newest = max((_version_tuple(v) for v in groups), default=())
    summary = []
    for version, members in sorted(groups.items(), key=lambda kv: _version_tuple(kv[0]), reverse=True):
        parsed = _version_tuple(version)
        behind = [sp for sp in members if parsed < newest_by_model[_model(sp)]]
        if parsed == newest:
            status = "newest"
        elif behind:
            status = "behind"
        else:
            # Every speaker of these models runs this version: an older release line.
            status = "older_line"
        summary.append(
            {
                "version": version,
                "display_version": next((sp.display_version for sp in members if sp.display_version), ""),
                "status": status,
                "count": len(members),
                "models": sorted({_model(sp) for sp in members}),
                "speakers": [{"name": _label(sp), "ip": sp.ip_address, "model": _model(sp)} for sp in members],
                "behind_ips": [sp.ip_address for sp in behind],
            }
        )
    return summary


def _firmware_findings(
    speakers: list[SpeakerDiagnostics], rooms: dict[str, list[SpeakerDiagnostics]]
) -> list[Finding]:
    findings: list[Finding] = []
    summary = _firmware_summary(speakers)
    by_ip = {sp.ip_address: sp for sp in speakers}

    for members in rooms.values():
        versions = {sp.software_version for sp in members}
        if len(versions) > 1:
            name = _room_name(members[0])
            findings.append(
                Finding(
                    severity="high",
                    category="firmware",
                    title=f"'{name}' has speakers on different firmware",
                    detail="Bonded speakers (pairs, surrounds, subs) must run the same version to play together.",
                    affected=_affected(members, lambda sp: sp.software_version),
                    recommendation=f"{name}: run Settings > System > System Updates in the Sonos app.",
                )
            )

    behind = [by_ip[ip] for group in summary for ip in group["behind_ips"]]
    if behind:
        findings.append(
            Finding(
                severity="medium",
                category="firmware",
                title=f"{len(behind)} speaker(s) behind on firmware",
                detail="Other speakers of the same model in this household already run a newer version.",
                affected=_affected(behind, lambda sp: f"{_model(sp)} on {sp.software_version}"),
                recommendation="Run Settings > System > System Updates in the Sonos app for the speakers that are behind.",
            )
        )

    older = [g for g in summary if g["status"] == "older_line"]
    if older:
        newest = summary[0]
        lines = "; ".join(
            f"{g['version']} on {', '.join(g['models'])} ({g['count']} speaker(s))" for g in older
        )
        findings.append(
            Finding(
                severity="info",
                category="firmware",
                title="Older models are on an older firmware line",
                detail=(
                    f"The newest version here is {newest['version']}. Also in use: {lines}. Every speaker of those models runs the "
                    "same version, so this looks like the latest release those models receive, not a missed update."
                ),
                affected=_affected(
                    [by_ip[s["ip"]] for g in older for s in g["speakers"]],
                    lambda sp: f"{_model(sp)} on {sp.software_version}",
                ),
            )
        )

    if len(summary) == 1 and speakers:
        findings.append(
            Finding(
                severity="ok",
                category="firmware",
                title="Firmware matched across household",
                detail=f"All speakers report {summary[0]['version']}.",
                affected_ips=[sp.ip_address for sp in speakers],
            )
        )
    return findings


# --- Wireless ---------------------------------------------------------------


def _path(sp: SpeakerDiagnostics) -> str:
    """How the speaker carries audio: wired, wifi, satellite or sonosnet."""
    code = sp.connection_type_code
    conn = (sp.connection_type or "").lower()
    if code == "8" or sp.radio_mode == "satellite":
        return "satellite"
    if code in {"3", "4"} or sp.ethernet_link or "ethernet" in conn:
        return "wired"
    if sp.sonosnet_disabled is False and "sonosnet" in sp.wifi_mode.lower():
        return "sonosnet"
    if code == "5" or "wifi" in conn:
        return "wifi"
    return "unknown"


def _radio_on(sp: SpeakerDiagnostics) -> bool:
    if _path(sp) != "wired":
        return True
    return sp.connection_type_code != "4" and (sp.wifi_enabled is True or sp.phy_errors is not None)


def _active_freq(sp: SpeakerDiagnostics) -> str:
    """Frequency (MHz) the radio is actually using, or '' when the radio is off."""
    if not _radio_on(sp):
        return ""
    if _path(sp) == "wired" and sp.ht_channel_mhz:
        return sp.ht_channel_mhz
    return sp.wifi_channel_mhz if sp.wifi_channel_mhz not in {"", "0"} else ""


def _band(freq: str) -> str:
    mhz = int(freq) if freq.isdigit() else 0
    if 2400 <= mhz < 2500:
        return "2.4 GHz"
    if 5000 <= mhz < 5925:
        return "5 GHz"
    if 5925 <= mhz < 7200:
        return "6 GHz"
    return ""


def _channel(freq: str) -> int | None:
    mhz = int(freq) if freq.isdigit() else 0
    band = _band(freq)
    if band == "2.4 GHz":
        return 14 if mhz == 2484 else (mhz - 2407) // 5
    if band == "5 GHz":
        return (mhz - 5000) // 5
    if band == "6 GHz":
        return (mhz - 5950) // 5
    return None


def _block_5ghz(channel: int | None) -> tuple[int, int] | None:
    return next((b for b in BLOCKS_5GHZ if channel and b[0] <= channel <= b[1]), None)


def _noise_floor(sp: SpeakerDiagnostics) -> int | None:
    """Worst noise floor across the radio's real receive chains (0 means no reading)."""
    chains = sp.noise_floor_dbm[: sp.rx_chains] if sp.rx_chains else sp.noise_floor_dbm
    readings = [n for n in chains if n < 0]
    return max(readings) if readings else None


def _radio_use(sp: SpeakerDiagnostics) -> str:
    path = _path(sp)
    if path == "wired":
        if not _radio_on(sp):
            return "off"
        return "ht_link" if sp.ht_channel_mhz else "idle"
    return {"satellite": "ht_link", "wifi": "wifi", "sonosnet": "sonosnet"}.get(path, "wifi")


RADIO_USE_LABEL = {
    "wifi": "Home Wi-Fi",
    "ht_link": "Home theater link",
    "sonosnet": "SonosNet",
    "idle": "Wired, radio on but unused",
    "off": "Wired, radio off",
}


def _wireless_map(speakers: list[SpeakerDiagnostics]) -> list[dict]:
    groups: dict[tuple, list[SpeakerDiagnostics]] = {}
    for sp in speakers:
        use = _radio_use(sp)
        freq = _active_freq(sp) if use != "off" else ""
        groups.setdefault((use, freq), []).append(sp)

    order = list(RADIO_USE_LABEL)
    rows = []
    for (use, freq), members in sorted(groups.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
        rows.append(
            {
                "use": use,
                "label": RADIO_USE_LABEL[use],
                "band": _band(freq),
                "channel": _channel(freq),
                "freq_mhz": int(freq) if freq else None,
                "speakers": [
                    {
                        "name": _label(sp),
                        "ip": sp.ip_address,
                        "phy_errors_per_sec": _round_rate(sp.phy_error_rate),
                        "noise_floor_dbm": _noise_floor(sp),
                    }
                    for sp in members
                ],
            }
        )
    return rows


def _wireless_findings(speakers: list[SpeakerDiagnostics]) -> list[Finding]:
    findings: list[Finding] = []
    # Radios that carry audio; an unused radio on a wired speaker does not matter.
    active = [sp for sp in speakers if _radio_use(sp) in {"wifi", "ht_link", "sonosnet"}]
    wifi = [sp for sp in speakers if _radio_use(sp) == "wifi"]

    on_24 = [sp for sp in wifi if _band(_active_freq(sp)) == "2.4 GHz"]
    odd_24 = [sp for sp in on_24 if _channel(_active_freq(sp)) not in {1, 6, 11}]
    if odd_24:
        findings.append(
            Finding(
                severity="medium",
                category="wireless",
                title="2.4 GHz Wi-Fi is on an overlapping channel",
                detail="Channels other than 1, 6 and 11 overlap their neighbours and pick up interference from both.",
                affected=_affected(odd_24, lambda sp: f"channel {_channel(_active_freq(sp))}"),
                recommendation="Set the router's 2.4 GHz radio to channel 1, 6 or 11.",
            )
        )
    if on_24:
        findings.append(
            Finding(
                severity="low",
                category="wireless",
                title=f"{len(on_24)} speaker(s) on 2.4 GHz Wi-Fi",
                detail=(
                    "2.4 GHz is the most congested band. Some models only support 2.4 GHz; "
                    "for those, Ethernet is the way to avoid it."
                ),
                affected=_affected(on_24, lambda sp: f"channel {_channel(_active_freq(sp))}"),
            )
        )

    dfs = [sp for sp in wifi if 52 <= (_channel(_active_freq(sp)) or 0) <= 144 and _band(_active_freq(sp)) == "5 GHz"]
    if dfs:
        findings.append(
            Finding(
                severity="low",
                category="wireless",
                title=f"{len(dfs)} speaker(s) on a 5 GHz DFS channel",
                detail=(
                    "Routers must leave DFS channels (52-144) when they detect radar, "
                    "which briefly disconnects everything on them."
                ),
                affected=_affected(dfs, lambda sp: f"channel {_channel(_active_freq(sp))}"),
                recommendation="If you see periodic whole-house dropouts, move the router's 5 GHz radio off DFS channels.",
            )
        )

    # Compare like with like: speakers on home Wi-Fi against each other.
    measured = [sp for sp in wifi if sp.phy_error_rate is not None]
    if len(measured) < 3:
        measured = [sp for sp in active if sp.phy_error_rate is not None]
    if len(measured) >= 3:
        median = statistics.median(sp.phy_error_rate for sp in measured)
        threshold = max(PHY_RATE_MIN_PER_SEC, PHY_RATE_OUTLIER_FACTOR * median)
        noisy = sorted(
            (sp for sp in active if (sp.phy_error_rate or 0) >= threshold),
            key=lambda sp: -(sp.phy_error_rate or 0),
        )
        if noisy:

            def note(sp: SpeakerDiagnostics) -> str:
                times = f", {sp.phy_error_rate / median:.0f}x typical" if median >= 1 else ""
                return f"{sp.phy_error_rate:,.0f}/s{times}"

            findings.append(
                Finding(
                    severity="medium",
                    category="wireless",
                    title=f"Unusually noisy radio environment at {_names(noisy)}",
                    detail=(
                        f"PHY errors are signals the radio could not decode. Most speakers here see about "
                        f"{median:,.0f} per second; these see far more, which points to interference or a weak "
                        "signal at that spot rather than a household-wide problem."
                    ),
                    affected=_affected(noisy, note),
                    recommendation=(
                        f"{_names(noisy)}: move the speaker away from other electronics, metal and thick walls, "
                        "or closer to an access point; wire it if a cable is available."
                    ),
                )
            )

    loud = [sp for sp in active if (_noise_floor(sp) or -200) > NOISE_FLOOR_ELEVATED_DBM]
    if loud:
        findings.append(
            Finding(
                severity="medium",
                category="wireless",
                title=f"Elevated noise floor on {len(loud)} speaker(s)",
                detail=f"A noise floor above {NOISE_FLOOR_ELEVATED_DBM} dBm leaves little headroom for the Wi-Fi signal.",
                affected=_affected(loud, lambda sp: f"{_noise_floor(sp)} dBm"),
                recommendation="Look for interference sources near the listed speakers, or change Wi-Fi channel.",
            )
        )

    extended = [sp for sp in wifi if sp.behind_wifi_extender]
    if extended:
        findings.append(
            Finding(
                severity="low",
                category="wireless",
                title=f"{len(extended)} speaker(s) behind a Wi-Fi extender",
                detail="Extenders halve throughput and are a common cause of grouping dropouts.",
                affected=_affected(extended, _connection_summary),
            )
        )

    channels = sorted({(_band(_active_freq(sp)), _channel(_active_freq(sp))) for sp in wifi if _active_freq(sp)})
    if len(channels) > 1:
        findings.append(
            Finding(
                severity="info",
                category="wireless",
                title=f"Wi-Fi speakers are spread across {len(channels)} channels",
                detail=(
                    "Normal with several access points or a mesh. It only matters when speakers that play "
                    "together sit on different channels. Channels: "
                    + ", ".join(f"{band} ch {ch}" for band, ch in channels)
                    + "."
                ),
                affected=_affected(
                    wifi, lambda sp: f"{_band(_active_freq(sp))} ch {_channel(_active_freq(sp))}"
                ),
            )
        )
    return findings


# --- Speaker-to-speaker signal ----------------------------------------------

# Same bands Sonos uses to colour its own network matrix.
SIGNAL_BANDS = [(45, "strong"), (30, "fair"), (18, "weak"), (1, "poor")]


def _signal_band(strength: float) -> str:
    return next((band for floor, band in SIGNAL_BANDS if strength >= floor), "none")


def _mac_int(mac: str) -> int | None:
    digits = re.sub(r"[^0-9A-Fa-f]", "", mac)
    return int(digits, 16) if len(digits) == 12 else None


def _connections(speakers: list[SpeakerDiagnostics]) -> dict:
    """Signal strength between speaker radios, from each speaker's node table."""
    bases = {sp.ip_address: _mac_int(sp.mac_address) for sp in speakers}

    def owner(peer_mac: str) -> str | None:
        # A speaker's radios use the addresses just above its Ethernet MAC.
        peer = _mac_int(peer_mac)
        matches = [
            (peer - base, ip) for ip, base in bases.items() if peer is not None and base is not None and 0 <= peer - base <= 3
        ]
        return min(matches)[1] if matches else None

    # heard[a][b] = (signal a receives from b, signal b receives from a) as reported by a
    heard: dict[str, dict[str, tuple[int, int]]] = {}
    for sp in speakers:
        for mac, signal_from, signal_to, _stp in sp.radio_peers:
            peer_ip = owner(mac)
            if peer_ip and peer_ip != sp.ip_address:
                best = heard.setdefault(sp.ip_address, {}).get(peer_ip, (0, 0))
                heard[sp.ip_address][peer_ip] = max(best, (signal_from, signal_to))

    links = []
    ips = [sp.ip_address for sp in speakers]
    for i, a in enumerate(ips):
        for b in ips[i + 1 :]:
            a_report = heard.get(a, {}).get(b, (0, 0))
            b_report = heard.get(b, {}).get(a, (0, 0))
            # Each direction, preferring the receiver's own measurement.
            a_hears_b = a_report[0] or b_report[1]
            b_hears_a = b_report[0] or a_report[1]
            if not (a_hears_b and b_hears_a):
                continue
            # Whole numbers (half up), so the band always matches the number shown.
            strength = (a_hears_b + b_hears_a + 1) // 2
            links.append(
                {
                    "a": a,
                    "b": b,
                    "a_hears_b": a_hears_b,
                    "b_hears_a": b_hears_a,
                    "strength": strength,
                    "band": _signal_band(strength),
                }
            )

    linked = {ip for link in links for ip in (link["a"], link["b"])}
    nodes, excluded = [], []
    for sp in speakers:
        freq = _active_freq(sp)
        if sp.ip_address in linked or sp.radio_peers:
            nodes.append(
                {
                    "ip": sp.ip_address,
                    "name": _label(sp),
                    "channel": f"{_band(freq)} ch {_channel(freq)}" if freq else "",
                    "link_count": sum(1 for l in links if sp.ip_address in (l["a"], l["b"])),
                }
            )
        else:
            use = _radio_use(sp)
            reason = {
                "off": "wired, radio off",
                "idle": "wired, radio not used",
                "ht_link": "home theater link, no signal data reported",
            }.get(use, "no signal data reported")
            excluded.append({"ip": sp.ip_address, "name": _label(sp), "reason": reason})

    return {
        "nodes": nodes,
        "links": links,
        "excluded": excluded,
        # With SonosNet off the speakers reach each other through the access point,
        # so these numbers describe radio proximity rather than the audio path.
        "mesh_active": any(sp.stp_enabled for sp in speakers),
    }


# --- Wired / bridge ---------------------------------------------------------


def _rx_drop_ratio(sp: SpeakerDiagnostics) -> float | None:
    if sp.br0.rx_packets <= 0:
        return None
    return 100.0 * sp.br0.rx_dropped / sp.br0.rx_packets


def _error_ratio(sp: SpeakerDiagnostics) -> float:
    errors = sp.eth0.rx_errors + sp.eth0.tx_errors + sp.br0.rx_errors + sp.br0.tx_errors
    packets = sp.eth0.rx_packets + sp.eth0.tx_packets + sp.br0.rx_packets + sp.br0.tx_packets
    return 100.0 * errors / packets if packets else 0.0


def _wired_findings(speakers: list[SpeakerDiagnostics]) -> list[Finding]:
    findings: list[Finding] = []
    wired = [sp for sp in speakers if _path(sp) == "wired"]
    sonosnet_in_use = any(_path(sp) == "sonosnet" for sp in speakers)

    idle_radio = [sp for sp in wired if _radio_use(sp) == "idle"]
    if idle_radio and not sonosnet_in_use:
        findings.append(
            Finding(
                severity="low",
                category="wired",
                title=f"Wi-Fi radio still on for {len(idle_radio)} wired speaker(s)",
                detail=(
                    "These speakers are on Ethernet and nothing connects through their radio, "
                    "so it only adds to local Wi-Fi congestion."
                ),
                affected=_affected(idle_radio, _connection_summary),
                recommendation=(
                    f"{_names(idle_radio)}: turn the radio off in the Sonos app "
                    "(Settings > System > [room] > [product] > Disable Wi-Fi)."
                ),
            )
        )

    slow = [sp for sp in wired if sp.ethernet_speed and not re.fullmatch(r"1\d{2,}(FD)?", sp.ethernet_speed)]
    if slow:
        findings.append(
            Finding(
                severity="medium",
                category="wired",
                title=f"Ethernet link negotiated poorly on {len(slow)} speaker(s)",
                detail="A 10 Mbps or half-duplex link usually means a damaged cable or a bad switch port.",
                affected=_affected(slow, lambda sp: sp.ethernet_speed),
                recommendation=f"{_names(slow)}: replace the Ethernet cable or try another switch port.",
            )
        )

    errors = [sp for sp in speakers if _error_ratio(sp) >= INTERFACE_ERROR_RATIO_PCT]
    if errors:
        findings.append(
            Finding(
                severity="medium",
                category="wired",
                title=f"Interface errors on {len(errors)} speaker(s)",
                detail=f"More than {INTERFACE_ERROR_RATIO_PCT}% of packets on the network interface had errors.",
                affected=_affected(errors, lambda sp: f"{_error_ratio(sp):.3f}% errors"),
                recommendation=f"{_names(errors)}: check the Ethernet cable and switch port.",
            )
        )

    # Home theater satellites drop most bridge traffic by design.
    dropping = [
        sp
        for sp in speakers
        if _path(sp) != "satellite" and (_rx_drop_ratio(sp) or 0) >= RX_DROP_RATIO_PCT
    ]
    if dropping:
        findings.append(
            Finding(
                severity="low",
                category="wired",
                title=f"High bridge RX drop ratio on {len(dropping)} speaker(s)",
                detail=(
                    "Sonos counts filtered multicast as drops, so some is normal, "
                    "but unusually high ratios can indicate a busy or looping network."
                ),
                affected=_affected(dropping, lambda sp: f"{_rx_drop_ratio(sp):.1f}%"),
            )
        )

    meshed = [sp for sp in speakers if sp.stp_enabled]
    if meshed:
        roots = [sp for sp in meshed if sp.stp_is_root]
        blocking = [sp for sp in meshed if any(state == "blocking" for _port, state in sp.stp_ports)]
        findings.append(
            Finding(
                severity="info",
                category="wired",
                title=f"SonosNet mesh active on {len(meshed)} speaker(s)",
                detail=(
                    (f"Root bridge: {_names(roots)}. " if roots else "")
                    + (f"{len(blocking)} speaker(s) have a blocked port, which is how the mesh avoids loops." if blocking else "")
                ).strip()
                or "Speakers are bridging through SonosNet.",
                affected=_affected(meshed, _connection_summary),
            )
        )
        if any(_path(sp) == "wifi" for sp in speakers):
            findings.append(
                Finding(
                    severity="low",
                    category="wired",
                    title="Household mixes SonosNet and home Wi-Fi",
                    detail="Some speakers use SonosNet while others use your Wi-Fi; grouping across the two is less reliable.",
                    affected=_affected([sp for sp in speakers if _path(sp) == "wifi"], _connection_summary),
                )
            )
    return findings


# --- Trueplay ---------------------------------------------------------------

# RoomCalibrationState as reported by zone topology: 1 = Trueplay tuning applied,
# 4 = no tuning, 5 = not applicable (satellite / bonded secondary).
TRUEPLAY = {"1": "tuned", "4": "not tuned"}


def _calibration_findings(rooms: dict[str, list[SpeakerDiagnostics]]) -> list[Finding]:
    primaries = [members[0] for members in rooms.values()]
    untuned = [sp for sp in primaries if TRUEPLAY.get(sp.room_calibration_state) == "not tuned"]
    if not untuned:
        return []
    return [
        Finding(
            severity="info",
            category="calibration",
            title=f"Trueplay tuning not applied in {len(untuned)} room(s)",
            detail=(
                "Optional. Trueplay adjusts a speaker to the room's acoustics; "
                "not every model or placement supports it."
            ),
            affected=_affected(untuned, lambda sp: sp.model_name),
        )
    ]


# --- Shared helpers ---------------------------------------------------------


def _connection_summary(sp: SpeakerDiagnostics) -> str:
    path = _path(sp)
    freq = _active_freq(sp)
    channel = f" ch {_channel(freq)}" if freq else ""
    if path == "wired":
        return "Wired" + (", radio on" if _radio_on(sp) else "")
    if path == "satellite":
        return f"Home theater link{channel}"
    if path == "sonosnet":
        return f"SonosNet{channel}"
    if path == "wifi":
        return f"Wi-Fi {_band(freq)}{channel}".replace("  ", " ").strip()
    return sp.connection_type or "Unknown"


def _names(speakers: list[SpeakerDiagnostics]) -> str:
    return ", ".join(dict.fromkeys(_label(sp) for sp in speakers))


def _affected(speakers: list[SpeakerDiagnostics], note) -> list[dict]:
    return [{"name": _label(sp), "ip": sp.ip_address, "note": note(sp) or ""} for sp in speakers]


def _round_rate(rate: float | None) -> float | None:
    return round(rate, 1) if rate is not None else None


def _severity_rank(finding: Finding) -> int:
    return {"high": 0, "medium": 1, "low": 2, "info": 3, "ok": 4}.get(finding.severity, 5)


def _speaker_to_dict(sp: SpeakerDiagnostics) -> dict:
    drop_ratio = _rx_drop_ratio(sp)
    freq = _active_freq(sp)
    return {
        "zone_name": sp.zone_name,
        "room_name": _room_name(sp),
        "room_id": _room_key(sp),
        "role": sp.role,
        "bond": sp.bond,
        "ip_address": sp.ip_address,
        "model_name": sp.model_name,
        "model_number": sp.model_number,
        "software_version": sp.software_version,
        "display_version": sp.display_version,
        "connection_type": sp.connection_type,
        "connection_summary": _connection_summary(sp),
        "network_path": _path(sp),
        "radio_on": _radio_on(sp),
        "ethernet_link": sp.ethernet_link,
        "ethernet_speed": sp.ethernet_speed,
        "wifi_band": _band(freq),
        "wifi_channel": _channel(freq),
        "wifi_channel_mhz": freq,
        "wifi_mode": sp.wifi_mode,
        "noise_floor_dbm": _noise_floor(sp),
        "trueplay": TRUEPLAY.get(sp.room_calibration_state),
        "room_calibration_state": sp.room_calibration_state,
        "boot_seq": sp.boot_seq,
        "airplay_enabled": sp.airplay_enabled,
        "is_stereo_left": sp.is_stereo_left,
        "is_stereo_right": sp.is_stereo_right,
        "is_stereo_coordinator": sp.is_stereo_coordinator,
        "br0_rx_drop_ratio_pct": round(drop_ratio, 1) if drop_ratio is not None else None,
        "br0_rx_packets": sp.br0.rx_packets,
        "br0_rx_dropped": sp.br0.rx_dropped,
        "phy_errors": sp.phy_errors,
        "phy_errors_per_sec": _round_rate(sp.phy_error_rate),
    }
