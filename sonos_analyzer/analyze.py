"""Analyze parsed Sonos diagnostics and produce findings."""

from __future__ import annotations

import ipaddress
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics
from sonos_analyzer.targets import is_allowed_target


# PHY error rate: flag radios well above the household's typical level.
PHY_RATE_OUTLIER_FACTOR = 5
PHY_RATE_MIN_PER_SEC = 500
NOISE_FLOOR_ELEVATED_DBM = -85
RX_DROP_RATIO_PCT = 20
INTERFACE_ERROR_RATIO_PCT = 0.01
# Below this many packets an error rate means little (one error in 40 packets is 2.5%).
MIN_INTERFACE_PACKETS = 10_000

# Findings that ask for action; "info" and "ok" are informational.
ACTION_SEVERITIES = ("high", "medium", "low")
ROLE_ORDER = ["Front", "Left", "Right", "Left surround", "Right surround", "Sub"]
# Models that can't turn their Wi-Fi off: no built-in Ethernet port (USB-C adapter or none).
NO_WIFI_OFF_MODELS = re.compile(r"\b(era 100|era 300|move|roam|sub mini)\b", re.IGNORECASE)
# Portables tune themselves as they move, so manual Trueplay doesn't apply.
PORTABLE_MODELS = re.compile(r"\b(move|roam)\b", re.IGNORECASE)
# No Trueplay to check: Port and Boost have no speakers, and an Amp supports it only with
# Sonos Architectural speakers, which the speaker data doesn't show.
NO_TRUEPLAY_MODELS = re.compile(r"\b(port|boost|amp)\b", re.IGNORECASE)
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
    history: dict | None = None


def analyze(snapshot: HouseholdSnapshot, changes: dict | None = None) -> AnalysisReport:
    """changes: comparison with earlier scans (history.compare), when history is kept."""
    speakers = sorted(snapshot.speakers.values(), key=_sort_key)
    interface = {sp.ip_address: _interface_errors(sp, changes) for sp in speakers}
    rooms = _rooms(speakers)

    outlier_ips = {sp.ip_address for sp in _phy_outliers(speakers)[0]}
    firmware_versions: dict[str, list[str]] = {}
    for sp in speakers:
        firmware_versions.setdefault(sp.software_version or "unknown", []).append(sp.ip_address)

    findings: list[Finding] = []
    findings += _firmware_findings(speakers, rooms)
    findings += _room_findings(speakers, rooms)
    findings += _wireless_findings(speakers)
    findings += _wired_findings(speakers, interface)
    findings += _calibration_findings(rooms)
    findings += _coordinator_findings(speakers, outlier_ips)
    findings += _radio_generation_findings(speakers)
    findings += _network_findings(snapshot, speakers)
    if changes:
        findings += _history_findings(changes)

    # Informational findings need no action, so they add no recommendations.
    recommendations = [
        f.recommendation for f in sorted(findings, key=_severity_rank) if f.recommendation and f.severity in ACTION_SEVERITIES
    ]
    if not any(f.severity in {"high", "medium"} for f in findings):
        recommendations.append("No significant issues detected.")
    recommendations.append("Reserve DHCP addresses for each speaker so IPs stay stable.")
    if any(_radio_use(sp) == "wifi" and _band(_active_freq(sp)) == "2.4 GHz" for sp in speakers):
        recommendations.append("Keep router 2.4 GHz on 20 MHz channel width for Sonos compatibility.")

    return AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        speaker_count=len(speakers),
        firmware_versions=firmware_versions,
        speakers=[{**_speaker_to_dict(sp, outlier_ips), "interface": interface[sp.ip_address]} for sp in speakers],
        findings=sorted(findings, key=_severity_rank),
        recommendations=list(dict.fromkeys(recommendations)),
        snapshot=snapshot,
        room_count=len(rooms),
        firmware=_firmware_summary(speakers),
        wireless=_wireless_map(speakers, outlier_ips),
        connections=_connections(speakers),
        history=_history_summary(changes),
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


def _model_key(sp: SpeakerDiagnostics) -> str | None:
    """Model identity for firmware comparison; None when the device description could not be read."""
    return sp.model_number or sp.model_name or None


def _line(version: str) -> str:
    """Release line: the major version, e.g. '86' for '86.11-82170'."""
    return version.split(".", 1)[0].split("-", 1)[0] if version else ""


def _build(version: str) -> str:
    """Build number after the dash, e.g. '82170'; shared by every line of one Sonos release."""
    return version.rsplit("-", 1)[1] if "-" in version else ""


def _firmware_lines(speakers: list[SpeakerDiagnostics]) -> dict:
    """Work out the household's main and maintenance firmware versions.

    A healthy household runs one or two versions: the main line for current models and,
    often, a maintenance line that Sonos keeps older models on. Anything else is an outlier.
    """
    versions = {sp.software_version for sp in speakers if sp.software_version}
    if not versions:
        return {"main": "", "maintenance": "", "same_release": False}
    main = max(versions, key=_version_tuple)
    newest_by_line: dict[str, str] = {}
    for v in versions:
        line = _line(v)
        if line not in newest_by_line or _version_tuple(v) > _version_tuple(newest_by_line[line]):
            newest_by_line[line] = v
    others = [v for line, v in newest_by_line.items() if line != _line(main)]

    def display(v: str) -> str:
        return next((sp.display_version for sp in speakers if sp.software_version == v and sp.display_version), "")

    def same_release(v: str) -> bool:
        return bool(_build(v)) and _build(v) == _build(main) or bool(display(v)) and display(v) == display(main)

    def weight(v: str) -> tuple:
        # Prefer a line that shares the main release, then the one most speakers use.
        return (same_release(v), sum(1 for sp in speakers if sp.software_version == v), _version_tuple(v))

    best = max(others, key=weight) if others else ""
    # Only a line that shares the main release (build or display version) counts as current;
    # otherwise its speakers are on an older line that can't be confirmed as up to date.
    if best and same_release(best):
        return {"main": main, "maintenance": best, "unconfirmed": "", "same_release": True}
    return {"main": main, "maintenance": "", "unconfirmed": best, "same_release": False}


def _and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _firmware_summary(speakers: list[SpeakerDiagnostics]) -> list[dict]:
    lines = _firmware_lines(speakers)
    newest_by_model: dict[str, tuple[int, ...]] = {}
    for sp in speakers:
        if key := _model_key(sp):
            newest_by_model[key] = max(newest_by_model.get(key, ()), _version_tuple(sp.software_version))

    groups: dict[str, list[SpeakerDiagnostics]] = {}
    for sp in speakers:
        groups.setdefault(sp.software_version or "unknown", []).append(sp)

    summary = []
    for version, members in sorted(groups.items(), key=lambda kv: _version_tuple(kv[0]), reverse=True):
        # The same model on a newer version elsewhere means this one missed an update.
        model_behind = [
            sp for sp in members if _model_key(sp) and _version_tuple(version) < newest_by_model[_model_key(sp)]
        ]
        if version == lines["main"] and not model_behind:
            status = "newest"
        elif version == lines["maintenance"] and not model_behind:
            status = "maintenance"
        elif version == lines["unconfirmed"] and not model_behind:
            status = "unconfirmed"
        else:
            status = "behind"
        summary.append(
            {
                "version": version,
                "display_version": next((sp.display_version for sp in members if sp.display_version), ""),
                "status": status,
                "count": len(members),
                "models": sorted({_model(sp) for sp in members}),
                "speakers": [{"name": _label(sp), "ip": sp.ip_address, "model": _model(sp)} for sp in members],
                "behind_ips": [sp.ip_address for sp in members] if status == "behind" else [],
            }
        )
    return summary


def _firmware_findings(
    speakers: list[SpeakerDiagnostics], rooms: dict[str, list[SpeakerDiagnostics]]
) -> list[Finding]:
    findings: list[Finding] = []
    summary = _firmware_summary(speakers)
    lines = _firmware_lines(speakers)
    by_ip = {sp.ip_address: sp for sp in speakers}

    for members in rooms.values():
        versions = {sp.software_version for sp in members}
        # Sonos's legacy-product line carries the same release under another version number
        # (86.11-82170 alongside 98.0-82170), so bonded speakers match when the builds do.
        builds = {_build(v) for v in versions}
        same_release = len(builds) == 1 and "" not in builds
        if len(versions) > 1 and not same_release:
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

    def expected(sp: SpeakerDiagnostics) -> str:
        for other in (lines["maintenance"], lines["unconfirmed"]):
            if other and _line(sp.software_version) == _line(other):
                return other
        return lines["main"]

    outliers = [by_ip[ip] for group in summary for ip in group["behind_ips"]]
    if outliers:
        current = f"{lines['main']} (main line)"
        if lines["maintenance"]:
            current += f" or {lines['maintenance']} (legacy-product line)"
        findings.append(
            Finding(
                severity="medium",
                category="firmware",
                title=f"{len(outliers)} speaker(s) missed a firmware update",
                detail=(
                    f"The rest of the household is on {current}. These speakers are on an earlier version, "
                    "so they most likely missed an update."
                ),
                affected=_affected(
                    outliers, lambda sp: f"{_model(sp)} on {sp.software_version}, expected {expected(sp)}"
                ),
                recommendation=(
                    "Run Settings > System > System Updates in the Sonos app. If a speaker stays behind, "
                    "unplug it for 10 seconds and check again."
                ),
            )
        )

    maintenance = next((g for g in summary if g["status"] == "maintenance"), None)
    if maintenance:
        main = next(g for g in summary if g["version"] == lines["main"])
        detail = (
            f"{_and_list(maintenance['models'])} speakers run {maintenance['version']}, while newer models run "
            f"{main['version']}. Since August 2025, Sonos keeps some of its earliest S2 products on their own "
            "firmware line, which receives essential updates with a focus on stability. Sonos says the different "
            "version number is normal and does not affect how these speakers work with the rest of the system."
        )
        parts = [p for p in (main["display_version"], f"build {_build(main['version'])}" if _build(main["version"]) else "") if p]
        detail += (
            " Both are the same Sonos release" + (f" ({', '.join(parts)})" if parts else "")
            + ", so these speakers are fully up to date. Source: support.sonos.com/en/article/legacy-product-update"
        )
        findings.append(
            Finding(
                severity="ok",
                category="firmware",
                title=f"{maintenance['count']} older speaker(s) are up to date on Sonos's legacy-product line",
                detail=detail,
                affected=_affected(
                    [by_ip[s["ip"]] for s in maintenance["speakers"]],
                    lambda sp: f"{_model(sp)} on {sp.software_version}",
                ),
            )
        )

    unconfirmed = next((g for g in summary if g["status"] == "unconfirmed"), None)
    if unconfirmed:
        findings.append(
            Finding(
                severity="info",
                category="firmware",
                title=f"{unconfirmed['count']} speaker(s) on an older firmware line that couldn't be confirmed as current",
                detail=(
                    f"{_and_list(unconfirmed['models'])} speakers run {unconfirmed['version']}, a different version "
                    f"line from the {lines['main']} your newer speakers run, and the two don't share a release build. "
                    "Sonos keeps some older products on their own line, but this scan can't tell whether these are on "
                    "its latest version. Check Settings > System > System Updates in the Sonos app."
                ),
                affected=_affected(
                    [by_ip[s["ip"]] for s in unconfirmed["speakers"]],
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
    if sp.connection_type_code == "4":
        return False
    if sp.wifi_enabled is not None:
        return sp.wifi_enabled
    return sp.phy_errors is not None


def _can_disable_wifi(sp: SpeakerDiagnostics) -> bool:
    """False for models whose radio stays on by design; unknown models are assumed able."""
    return not NO_WIFI_OFF_MODELS.search(sp.model_name or "")


def _active_freq(sp: SpeakerDiagnostics) -> str:
    """Frequency (MHz) the radio is actually using, or '' when the radio is off."""
    if not _radio_on(sp):
        return ""
    if _path(sp) == "wired" and sp.ht_channel_mhz:
        return sp.ht_channel_mhz
    return sp.wifi_channel_mhz if sp.wifi_channel_mhz not in {"", "0"} else ""


def _mhz(freq: str) -> int:
    """Frequency in MHz from speaker text; 0 for anything that isn't a plain number."""
    return int(freq) if re.fullmatch(r"[0-9]{1,5}", freq or "") else 0


def _band(freq: str) -> str:
    mhz = _mhz(freq)
    if 2400 <= mhz < 2500:
        return "2.4 GHz"
    if 5000 <= mhz < 5925:
        return "5 GHz"
    if 5925 <= mhz < 7200:
        return "6 GHz"
    return ""


def _channel(freq: str) -> int | None:
    mhz = _mhz(freq)
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


def _phy_outliers(speakers: list[SpeakerDiagnostics]) -> tuple[list[SpeakerDiagnostics], float | None]:
    """Radios carrying audio whose PHY error rate is far above the household's typical level."""
    active = [sp for sp in speakers if _radio_use(sp) in {"wifi", "ht_link", "sonosnet"}]
    wifi = [sp for sp in speakers if _radio_use(sp) == "wifi"]
    # Compare like with like: speakers on home Wi-Fi against each other.
    measured = [sp for sp in wifi if sp.phy_error_rate is not None]
    if len(measured) < 3:
        measured = [sp for sp in active if sp.phy_error_rate is not None]
    if len(measured) < 3:
        return [], None
    median = statistics.median(sp.phy_error_rate for sp in measured)
    threshold = max(PHY_RATE_MIN_PER_SEC, PHY_RATE_OUTLIER_FACTOR * median)
    noisy = sorted(
        (sp for sp in active if (sp.phy_error_rate or 0) >= threshold),
        key=lambda sp: -(sp.phy_error_rate or 0),
    )
    return noisy, median


# Plain-language levels for readers without a radio background. bars: 1-4, more is better.
# tone: good (green), fair (yellow), warn (orange), bad (red), muted.
# Noise floor and speaker-to-speaker signal use the bands and colors of the network matrix
# built into Sonos speakers (/support/review). Sonos doesn't grade PHY errors; those
# bands are this tool's guide.
def _level(bars: int, label: str, tone: str) -> dict:
    return {"bars": bars, "label": label, "tone": tone}


NOISE_LEVELS = [
    _level(1, "Very noisy", "bad"),
    _level(2, "Noisy", "warn"),
    _level(3, "Some noise", "fair"),
    _level(4, "Quiet", "good"),
]


def noise_level(dbm: int | None, weak_signal_score: float | None = None) -> dict | None:
    """Background radio noise where the speaker sits; lower dBm is quieter.

    Like Sonos's network matrix, an older radio's interference score (when reported) must
    also clear each band: a quiet reading with the radio fighting interference isn't quiet.
    """
    if dbm is None:
        return None
    by_noise = 4 if dbm <= -95 else 3 if dbm <= -90 else 2 if dbm <= NOISE_FLOOR_ELEVATED_DBM else 1
    if weak_signal_score is None:
        return NOISE_LEVELS[by_noise - 1]
    by_score = 4 if weak_signal_score > 4 else 3 if weak_signal_score > 3 else 2 if weak_signal_score > 2 else 1
    return NOISE_LEVELS[min(by_noise, by_score) - 1]


def phy_level(sp: SpeakerDiagnostics, outlier_ips: set[str]) -> dict | None:
    """How much garbled radio traffic the speaker hears, in plain words."""
    if sp.phy_error_rate is None:
        return None
    if _radio_use(sp) == "idle":
        return _level(0, "Not in use", "muted")
    if sp.ip_address in outlier_ips:
        return _level(1, "Very high", "bad")
    if sp.phy_error_rate >= PHY_RATE_MIN_PER_SEC:
        return _level(2, "High", "warn")
    return _level(4, "Normal", "good")


SIGNAL_LEVELS = {
    "strong": _level(4, "Strong", "good"),
    "fair": _level(3, "Fair", "fair"),
    "weak": _level(2, "Weak", "warn"),
    "poor": _level(1, "Poor", "bad"),
}


def _wireless_map(speakers: list[SpeakerDiagnostics], outlier_ips: set[str] = frozenset()) -> list[dict]:
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
                "freq_mhz": _mhz(freq) or None,
                "speakers": [
                    {
                        "name": _label(sp),
                        "ip": sp.ip_address,
                        "phy_errors_per_sec": _round_rate(sp.phy_error_rate),
                        "phy_level": phy_level(sp, outlier_ips),
                        "noise_floor_dbm": _noise_floor(sp),
                        "noise_level": noise_level(_noise_floor(sp), sp.weak_signal_score),
        "ani_level": sp.ani_level,
        "hal_reset_failures": sp.hal_reset_failures,
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
                severity="info",
                category="wireless",
                title=f"{len(dfs)} speaker(s) on a 5 GHz DFS channel",
                detail=(
                    "Routers must leave DFS channels (52-144) when they detect radar, "
                    "which briefly disconnects everything on them. Usually harmless; if you notice "
                    "occasional whole-house dropouts, moving the router's 5 GHz radio off DFS channels is worth a try."
                ),
                affected=_affected(dfs, lambda sp: f"channel {_channel(_active_freq(sp))}"),
            )
        )

    noisy, median = _phy_outliers(speakers)
    if median is not None:
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
                    "level": SIGNAL_LEVELS.get(_signal_band(strength)),
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


def _interface_errors(sp: SpeakerDiagnostics, changes: dict | None) -> dict | None:
    """Errors and drops on the speaker's main interface, over a stated span.

    Since the previous scan when the speaker hasn't restarted; otherwise since it last
    restarted (the counters' own start, an unknown span).
    """
    since = (changes or {}).get("wired_since", {}).get(sp.local_uid)
    if since:
        port, packets, errors, dropped = since["port"], since["packets"], since["errors"], since["dropped"]
        span, seconds = "since last scan", since["seconds"]
    else:
        port = "eth0" if sp.ethernet_link else "br0"
        stats = getattr(sp, port)
        packets = stats.rx_packets + stats.tx_packets
        errors, dropped = stats.rx_errors + stats.tx_errors, stats.rx_dropped
        span, seconds = "since restart", None
    if packets <= 0 and since is None:
        return None  # no counters at all
    pct = 100.0 * errors / packets if packets > 0 else 0.0
    graded = packets >= MIN_INTERFACE_PACKETS
    return {
        "port": port,
        "span": span,
        "seconds": round(seconds) if seconds is not None else None,
        "packets": packets,
        "errors": errors,
        "dropped": dropped,
        "error_pct": round(pct, 4),
        "graded": graded,
        "level": _level(0, "Too few packets", "muted") if not graded
        else _level(4, "None", "good") if errors == 0
        else _level(4, "Normal", "good") if pct < INTERFACE_ERROR_RATIO_PCT
        else _level(2, "High", "warn") if pct < 10 * INTERFACE_ERROR_RATIO_PCT
        else _level(1, "Very high", "bad"),
    }


def _rx_drop_ratio(sp: SpeakerDiagnostics) -> float | None:
    if sp.br0.rx_packets <= 0:
        return None
    return 100.0 * sp.br0.rx_dropped / sp.br0.rx_packets


def _error_ratio(sp: SpeakerDiagnostics) -> float:
    errors = sp.eth0.rx_errors + sp.eth0.tx_errors + sp.br0.rx_errors + sp.br0.tx_errors
    packets = sp.eth0.rx_packets + sp.eth0.tx_packets + sp.br0.rx_packets + sp.br0.tx_packets
    return 100.0 * errors / packets if packets else 0.0


def stp_root(speakers: list[SpeakerDiagnostics]) -> str:
    """The spanning-tree root most speakers agree on, or ''."""
    ids = [sp.stp_root_id for sp in speakers if sp.stp_root_id]
    return max(set(ids), key=ids.count) if ids else ""


def _wired_findings(speakers: list[SpeakerDiagnostics], interface: dict | None = None) -> list[Finding]:
    findings: list[Finding] = []
    wired = [sp for sp in speakers if _path(sp) == "wired"]
    sonosnet_in_use = any(_path(sp) == "sonosnet" for sp in speakers)

    idle_radio = [sp for sp in wired if _radio_use(sp) == "idle" and _can_disable_wifi(sp)]
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

    interface = interface or {sp.ip_address: _interface_errors(sp, None) for sp in speakers}
    # Cable problems: Ethernet on wired speakers only, with enough packets to judge.
    errors = [
        sp for sp in speakers
        if (i := interface.get(sp.ip_address)) and i["port"] == "eth0" and i["graded"]
        and i["error_pct"] >= INTERFACE_ERROR_RATIO_PCT
    ]
    if errors:
        findings.append(
            Finding(
                severity="medium",
                category="wired",
                title=f"Interface errors on {len(errors)} speaker(s)",
                detail=f"More than {INTERFACE_ERROR_RATIO_PCT}% of packets on the network interface had errors.",
                affected=_affected(
                    errors, lambda sp: f"{interface[sp.ip_address]['error_pct']:.3f}% errors {interface[sp.ip_address]['span']}"
                ),
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
        root_id = stp_root(speakers)
        outside_root = bool(root_id) and not roots
        # With every radio off, spanning tree runs only across the Ethernet ports: no wireless mesh.
        wired_only = all(_path(sp) == "wired" and not _radio_on(sp) for sp in meshed)
        title = (
            f"Spanning tree active on {len(meshed)} wired speaker(s), not a wireless mesh"
            if wired_only
            else f"SonosNet mesh active on {len(meshed)} speaker(s)"
        )
        wired_note = (
            "Every one of these speakers has its Wi-Fi radio off, so this is spanning tree across their Ethernet "
            "ports, which Sonos keeps running to prevent loops. No wireless SonosNet mesh is in use. "
            if wired_only
            else ""
        )
        findings.append(
            Finding(
                severity="info",
                category="wired",
                title=title,
                detail=(
                    wired_note
                    + (f"Root bridge: {_names(roots)}. " if roots else "")
                    + (
                        f"Root bridge: a device outside your Sonos system (bridge ID {root_id}), usually a network switch. "
                        if outside_root else ""
                    )
                    + (f"{len(blocking)} speaker(s) have a blocked port, which is how the mesh avoids loops." if blocking else "")
                ).strip()
                or "Speakers are bridging through SonosNet.",
                affected=_affected(meshed, _connection_summary),
            )
        )
        # A blocked Ethernet port means spanning tree has turned off that speaker's cable.
        cable_blocked = [
            sp for sp in meshed
            if sp.ethernet_link and any(port.startswith("eth") and state == "blocking" for port, state in sp.stp_ports)
        ]
        if cable_blocked:
            findings.append(
                Finding(
                    severity="low",
                    category="wired",
                    title=f"Ethernet blocked by spanning tree on {len(cable_blocked)} wired speaker(s)",
                    detail=(
                        "These speakers are plugged in, but spanning tree has blocked their Ethernet port to prevent a "
                        "network loop, so they aren't using the cable. Sonos requires managed switches to have Spanning "
                        "Tree Protocol (STP) and BPDU flooding enabled; without them, several wired Sonos products can "
                        "cause loops and broadcast storms."
                    ),
                    affected=_affected(
                        cable_blocked,
                        lambda sp: ", ".join(f"{port} {state}" for port, state in sp.stp_ports if port.startswith("eth")),
                    ),
                    recommendation=(
                        "If you use managed switches, enable STP and BPDU flooding on them (Sonos: Using Sonos with a "
                        "managed switch). In Sonos's own example setup, the main switch is made the root bridge."
                    ),
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


def _is_portable(sp: SpeakerDiagnostics) -> bool:
    return bool(PORTABLE_MODELS.search(sp.model_name or ""))


def _trueplay(sp: SpeakerDiagnostics) -> str | None:
    if _is_portable(sp) or NO_TRUEPLAY_MODELS.search(sp.model_name or ""):
        return "not applicable"
    return TRUEPLAY.get(sp.room_calibration_state)


def _calibration_findings(rooms: dict[str, list[SpeakerDiagnostics]]) -> list[Finding]:
    primaries = [members[0] for members in rooms.values()]
    untuned = [sp for sp in primaries if _trueplay(sp) == "not tuned"]
    if not untuned:
        return []
    return [
        Finding(
            severity="info",
            category="calibration",
            title=f"Trueplay tuning not applied in {len(untuned)} room(s)",
            detail=(
                "Optional. Trueplay adjusts a speaker to the room's acoustics; "
                "not every model or placement supports it. Portable speakers (Move, Roam) "
                "tune themselves, and Port, Boost and Amp are not counted."
            ),
            affected=_affected(untuned, lambda sp: sp.model_name),
        )
    ]


# --- Groups and radio hardware ----------------------------------------------

# Sonos's "Supported WiFi modes and security standards" article: One (Gen 1), Play:1, Play:3,
# Play:5 (Gen 2), Sub (Gen 1 and 2), SYMFONISK Bookshelf (Gen 1) and Table lamp (Gen 1) support
# only 802.11a/b/g and no WPA3. Names that differ only by generation are matched by model
# number; Sub (Gen 1/2) and Table lamp (Gen 1) are left out until their numbers are confirmed.
OLDER_RADIO_MODEL_NUMBERS = {"S1", "S12", "S3", "S6", "S13", "S21"}
OLDER_RADIO_NAMES = re.compile(r"^(sonos )?(play:1|play:3)$", re.IGNORECASE)


def _older_radio(sp: SpeakerDiagnostics) -> bool:
    return sp.model_number in OLDER_RADIO_MODEL_NUMBERS or bool(OLDER_RADIO_NAMES.match(sp.model_name or ""))


def _weak_lead(sp: SpeakerDiagnostics, outlier_ips: set[str]) -> str:
    """Why this speaker is a poor choice to lead a group, or ''."""
    if _path(sp) == "wired":
        return ""
    if sp.ip_address in outlier_ips:
        return "its radio sees far more interference than the rest of your speakers"
    level = noise_level(_noise_floor(sp), sp.weak_signal_score)
    if level and level["bars"] <= 2:
        return "it sits in a noisy radio spot"
    if _older_radio(sp):
        return "it has an older Wi-Fi radio"
    return ""


def _coordinator_findings(speakers: list[SpeakerDiagnostics], outlier_ips: set[str]) -> list[Finding]:
    """A group's coordinator receives the audio and sends it to the other members."""
    groups: dict[str, list[SpeakerDiagnostics]] = {}
    for sp in speakers:
        if sp.group_id and not sp.is_invisible:
            groups.setdefault(sp.group_id, []).append(sp)
    by_uid = {sp.local_uid: sp for sp in speakers}
    flagged = []
    for members in groups.values():
        rooms = {_room_key(sp) for sp in members}
        coordinator = by_uid.get(members[0].coordinator_uid)
        if len(rooms) < 2 or coordinator is None:
            continue  # a single room (even a pair or home theater) isn't a group of rooms
        reason = _weak_lead(coordinator, outlier_ips)
        if not reason:
            continue
        better = [sp for sp in members if _room_key(sp) != _room_key(coordinator) and _path(sp) == "wired"]
        better = better or [
            sp for sp in members if _room_key(sp) != _room_key(coordinator) and not _weak_lead(sp, outlier_ips)
        ]
        if better:
            flagged.append((coordinator, reason, better[0]))
    if not flagged:
        return []
    return [
        Finding(
            severity="low",
            category="rooms",
            title=f"{len(flagged)} group(s) led by a speaker that's a weak choice",
            detail=(
                "In a group, one speaker (the group coordinator) receives the music and sends it to the others, "
                "so it works hardest on your network. It's the room the group was started from, and Sonos "
                "recommends starting groups from a wired speaker or the one closest to your router."
            ),
            affected=[
                {"name": _label(c), "ip": c.ip_address, "note": f"leads the group, but {reason}; {_label(b)} would be better"}
                for c, reason, b in flagged
            ],
            recommendation=(
                "Ungroup, then group again starting from the better speaker: play on that room first, then add the "
                "others. "
                + "; ".join(f"For example, start from {_label(b)} instead of {_label(c)}" for c, _r, b in flagged[:2])
                + "."
            ),
        )
    ]


def _radio_generation_findings(speakers: list[SpeakerDiagnostics]) -> list[Finding]:
    findings = []
    older = [sp for sp in speakers if _older_radio(sp)]
    if older:
        findings.append(
            Finding(
                severity="info",
                category="wireless",
                title=f"{len(older)} speaker(s) use an older Wi-Fi standard",
                detail=(
                    "Sonos lists these models as using older Wi-Fi standards (802.11a/b/g) without WPA3 security. "
                    "They work fine today. When you replace your router or change its security settings, make sure "
                    "WPA2 stays available on the network these speakers use; a WPA3-only network will leave them "
                    "unable to connect. Source: support.sonos.com/en-us/article/supported-wifi-modes-and-security-standards-for-sonos-products"
                ),
                affected=_affected(older, lambda sp: sp.model_name or sp.model_number),
            )
        )
    resets = [sp for sp in speakers if (sp.hal_reset_failures or 0) > 0]
    if resets:
        findings.append(
            Finding(
                severity="info",
                category="wireless",
                title=f"Radio reset failures reported on {len(resets)} speaker(s)",
                detail=(
                    "Older radios report a count of failed radio resets. Its meaning isn't documented; a count that "
                    "keeps rising suggests the radio isn't restarting cleanly, which is worth a speaker restart and, "
                    "if it continues, a diagnostic report to Sonos."
                ),
                affected=_affected(resets, lambda sp: f"{sp.hal_reset_failures} failure(s)"),
            )
        )
    return findings


# --- Network layout: subnets and double NAT --------------------------------

SAME_SUBNET_SOURCE = "Sonos system requirements: support.sonos.com/en-us/article/sonos-system-requirements"


def _subnet(ip: str, mask: str) -> ipaddress.IPv4Network | None:
    try:
        return ipaddress.ip_network(f"{ip}/{mask}", strict=False)
    except ValueError:
        return None


def _network_findings(snapshot: HouseholdSnapshot, speakers: list[SpeakerDiagnostics]) -> list[Finding]:
    findings: list[Finding] = []
    # The household's network is the one most visible speakers report. Compare addresses, not masks:
    # home theater surrounds on a direct link to their soundbar report a wider mask (255.0.0.0).
    counts: dict[ipaddress.IPv4Network, int] = {}
    for sp in speakers:
        net = _subnet(sp.ip_address, sp.netmask) if sp.netmask and not sp.is_invisible else None
        if net is not None:
            counts[net] = counts.get(net, 0) + 1
    if not counts:
        return findings
    main = max(counts, key=lambda net: (counts[net], net.prefixlen))

    def outside_main(ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip) not in main
        except ValueError:
            return False

    others = [sp for sp in speakers if outside_main(sp.ip_address)]
    if others:
        findings.append(
            Finding(
                severity="medium",
                category="network",
                title=f"{len(others)} speaker(s) are on a different network from the rest",
                detail=(
                    f"Most speakers are on {main}, but these are not. "
                    "Sonos requires the Sonos app and all Sonos products to be on the same subnet. A second router "
                    "is the usual cause: an internet provider's router plus your own, or a mesh system set to router "
                    f"mode instead of bridge (access point) mode. {SAME_SUBNET_SOURCE}"
                ),
                affected=_affected(others, lambda sp: sp.ip_address),
                recommendation=(
                    "Put every speaker on one network: set the second router or mesh system to bridge "
                    "(access point) mode, then restart the speakers that were on the other network."
                ),
            )
        )

    local_ips = sorted(
        {
            local
            for sp in speakers
            if (local := snapshot.analyzer_addresses.get(sp.ip_address, "")) and is_allowed_target(local)
        }
    )
    if local_ips and not any(not outside_main(local) for local in local_ips):
        findings.append(
            Finding(
                severity="low",
                category="network",
                title="This computer is on a different network from the speakers",
                detail=(
                    f"This computer reached the speakers from {', '.join(local_ips)}, which is outside the speakers' "
                    f"network ({main}). The scan worked because a router passed the traffic, but the Sonos "
                    "app on a phone or computer on this network won't find the speakers. If your phone is on the "
                    f"speakers' network, this doesn't matter. {SAME_SUBNET_SOURCE}"
                ),
                recommendation=f"Run the Sonos app from a phone or computer on the speakers' network ({main}).",
            )
        )

    router = snapshot.router
    if router and router.searched and router.gateways:
        answered = [g for g in router.gateways if g.wan != "unknown"]
        double = [g for g in answered if g.wan == "private"]
        shared = [g for g in answered if g.wan == "cgnat"]
        public = [g for g in answered if g.wan == "public"]
        def where(g):
            return f"{g.model} ({g.ip})" if g.model else g.ip
        if double:
            findings.append(
                Finding(
                    severity="info",
                    category="network",
                    title="Double NAT: your router sits behind another router",
                    detail=(
                        "Your router reports an internet-side address on a private network ("
                        + "; ".join(f"{where(g)}: {g.wan_ip}" for g in double)
                        + "), so another router is in front of it. Sonos works through double NAT as long as the "
                        "speakers and the phone or computer running the Sonos app are on the same network; problems "
                        "start when some devices connect to the front router instead. Read with UPnP from the router."
                    ),
                    affected=[{"name": g.model or "Router", "ip": g.ip, "note": f"internet side {g.wan_ip}"} for g in double],
                )
            )
        if shared:
            findings.append(
                Finding(
                    severity="info",
                    category="network",
                    title="Your internet provider shares its public address (carrier-grade NAT)",
                    detail=(
                        "Your router's internet-side address is in 100.64.0.0/10, the range providers use to share "
                        "one public address between customers. This doesn't affect Sonos inside your home."
                    ),
                    affected=[{"name": g.model or "Router", "ip": g.ip, "note": f"internet side {g.wan_ip}"} for g in shared],
                )
            )
        if len(answered) > 1:
            findings.append(
                Finding(
                    severity="info",
                    category="network",
                    title=f"{len(answered)} routers answered on your network",
                    detail=(
                        "More than one device offered to route internet traffic. If one of them is a mesh system or a "
                        "second router, check it is in bridge (access point) mode so all devices share one network."
                    ),
                    affected=[{"name": g.model or "Router", "ip": g.ip, "note": ""} for g in answered],
                )
            )
        if public and not double and not shared and len(answered) == 1:
            findings.append(
                Finding(
                    severity="ok",
                    category="network",
                    title="No double NAT",
                    detail="Your router reports a public internet address, so no second router sits in front of it.",
                    affected=[{"name": g.model or "Router", "ip": g.ip, "note": ""} for g in public],
                )
            )
    return findings


# --- Changes between scans ------------------------------------------------


def _history_summary(changes: dict | None) -> dict | None:
    if not changes:
        return None
    return {
        "previous_at": changes["previous_at"],
        "first_scan_at": changes["first_scan_at"],
        "scans_in_window": changes["scans_in_window"],
        "trend_days": changes.get("trend_days", 7),
    }


def _history_findings(changes: dict) -> list[Finding]:
    findings: list[Finding] = []
    names, ips = changes["names"], changes["ips"]
    days = changes.get("trend_days", 7)

    def item(uid: str, note: str) -> dict:
        return {"name": names.get(uid, uid), "ip": ips.get(uid, ""), "note": note}

    missing = changes["missing"]
    models = changes.get("models", {})
    # Portables go to sleep on battery, so their absence is normal.
    asleep = {uid: t for uid, t in missing.items() if PORTABLE_MODELS.search(models.get(uid, "") or "")}
    gone = {uid: t for uid, t in missing.items() if uid not in asleep}

    def seen(at: str) -> str:
        return f"not seen since {at[:16].replace('T', ' ')} UTC"

    if gone:
        findings.append(
            Finding(
                severity="medium",
                category="history",
                title=f"{len(gone)} speaker(s) missing",
                detail=(
                    "These speakers answered an earlier scan in the last "
                    f"{days} days but not this one: they may be unplugged, off the network or restarting."
                ),
                affected=[item(uid, seen(at)) for uid, at in sorted(gone.items(), key=lambda kv: kv[1])],
                recommendation="Check that the missing speakers have power and appear in the Sonos app; scan again in a few minutes.",
            )
        )
    if asleep:
        findings.append(
            Finding(
                severity="info",
                category="history",
                title=f"{len(asleep)} portable speaker(s) not seen in this scan",
                detail="Portable speakers (Move, Roam) go to sleep or leave the network on battery, so this is usually normal.",
                affected=[item(uid, seen(at)) for uid, at in sorted(asleep.items(), key=lambda kv: kv[1])],
            )
        )

    restarts = changes["restarts"]
    if restarts:
        repeated = {uid: n for uid, n in restarts.items() if n >= 3}
        findings.append(
            Finding(
                severity="medium" if repeated else "low",
                category="history",
                title=f"Unexpected restarts in the last {days} days on {len(restarts)} speaker(s)",
                detail=(
                    "These speakers restarted without a firmware update: their boot counter went up between scans "
                    "(the same signal Home Assistant uses to detect Sonos restarts). An occasional restart can follow "
                    "a power cut; repeated ones usually point to power or a fault."
                ),
                affected=[
                    item(uid, f"restarted at least {n} time{'s' if n != 1 else ''}")
                    for uid, n in sorted(restarts.items(), key=lambda kv: -kv[1])
                ],
                recommendation=(
                    "For speakers that restart repeatedly: plug them straight into a wall outlet (not a power strip or "
                    "switched socket), check the cable, and contact Sonos support if it keeps happening."
                ),
            )
        )

    if changes["updates"]:
        findings.append(
            Finding(
                severity="info",
                category="history",
                title=f"Firmware updated on {len(changes['updates'])} speaker(s) in the last {days} days",
                detail="Restarts that came with these updates are expected and aren't counted as unexpected.",
                affected=[item(uid, f"now {versions[-1]}") for uid, versions in changes["updates"].items()],
            )
        )

    if changes["ip_changes"]:
        findings.append(
            Finding(
                severity="info",
                category="history",
                title=f"IP address changed on {len(changes['ip_changes'])} speaker(s) in the last {days} days",
                detail="Sonos finds speakers again after an address change, so this is usually harmless.",
                affected=[item(uid, " → ".join(addrs)) for uid, addrs in changes["ip_changes"].items()],
            )
        )

    if changes["coordinator_changes"]:
        findings.append(
            Finding(
                severity="info",
                category="history",
                title=f"Group coordinator changed {len(changes['coordinator_changes'])} time(s) in the last {days} days",
                detail=(
                    "The same group of speakers was led by a different speaker. This happens when the group is "
                    "started again from another room, or when the leading speaker leaves the group (Sonos then hands "
                    "the job to another member automatically)."
                ),
                affected=[
                    item(c["to"], f"took over from {names.get(c['from'], c['from'])} at {c['at'][11:16]} UTC")
                    for c in changes["coordinator_changes"]
                ],
            )
        )

    root_changes = changes.get("stp_root_changes", [])
    if root_changes:
        def root_name(root_id: str) -> str:
            return names.get(changes.get("bridge_ids", {}).get(root_id, ""), "") or f"a device outside Sonos ({root_id})"

        findings.append(
            Finding(
                severity="low" if len(root_changes) >= 3 else "info",
                category="wired",
                title=f"SonosNet spanning-tree root changed {len(root_changes)} time(s) in the last {days} days",
                detail=(
                    "Each change makes the network re-form, which can cause brief dropouts. An occasional change "
                    "follows a speaker or switch restarting; frequent changes suggest a device that keeps restarting "
                    "or a cable that keeps disconnecting."
                ),
                affected=[
                    {"name": root_name(c["to"]), "ip": "", "note": f"took over from {root_name(c['from'])} at {c['at'][11:16]} UTC"}
                    for c in root_changes
                ],
                recommendation=(
                    "Check that wired speakers and switches have stable power and cables, and that managed switches "
                    "have STP and BPDU flooding enabled."
                    if len(root_changes) >= 3 else ""
                ),
            )
        )

    if changes["new"]:
        findings.append(
            Finding(
                severity="info",
                category="history",
                title=f"{len(changes['new'])} new speaker(s) since the last scan",
                detail="Speakers that weren't in the previous scan.",
                affected=[item(uid, "") for uid in changes["new"]],
            )
        )
    return findings


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


def _speaker_to_dict(sp: SpeakerDiagnostics, outlier_ips: set[str] = frozenset()) -> dict:
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
        "noise_level": noise_level(_noise_floor(sp), sp.weak_signal_score),
                        "ani_level": sp.ani_level,
        "trueplay": _trueplay(sp),
        "room_calibration_state": sp.room_calibration_state,
        "boot_seq": sp.boot_seq,
        "airplay_enabled": sp.airplay_enabled,
        "is_stereo_left": sp.is_stereo_left,
        "is_stereo_right": sp.is_stereo_right,
        "is_stereo_coordinator": sp.is_stereo_coordinator,
        "br0_rx_drop_ratio_pct": round(drop_ratio, 1) if drop_ratio is not None else None,
        "br0_rx_packets": sp.br0.rx_packets,
        "br0_rx_dropped": sp.br0.rx_dropped,
        # From the support review; covers an unknown period since anything last read the counter.
        "phy_errors_unknown_window": sp.phy_errors,
        "phy_errors_per_sec": _round_rate(sp.phy_error_rate),
        "phy_level": phy_level(sp, outlier_ips),
        "phy_window_errors": sp.phy_window_errors,
        "phy_window_seconds": round(sp.phy_window_seconds, 2) if sp.phy_window_seconds is not None else None,
        "phy_not_measured": sp.phy_not_measured,
    }
