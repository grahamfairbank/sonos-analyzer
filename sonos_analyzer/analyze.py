"""Analyze parsed Sonos diagnostics and produce findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics


@dataclass
class Finding:
    severity: str  # high, medium, low, ok
    title: str
    detail: str
    affected_ips: list[str] = field(default_factory=list)


@dataclass
class AnalysisReport:
    generated_at: str
    speaker_count: int
    firmware_versions: dict[str, list[str]]
    speakers: list[dict]
    findings: list[Finding]
    recommendations: list[str]
    snapshot: HouseholdSnapshot


def analyze(snapshot: HouseholdSnapshot) -> AnalysisReport:
    speakers = list(snapshot.speakers.values())
    findings: list[Finding] = []
    recommendations: list[str] = []

    firmware_versions: dict[str, list[str]] = {}
    for sp in speakers:
        firmware_versions.setdefault(sp.software_version or "unknown", []).append(sp.ip_address)

    if len(firmware_versions) > 1:
        versions = ", ".join(sorted(firmware_versions))
        findings.append(
            Finding(
                severity="medium",
                title="Firmware versions differ across speakers",
                detail=f"Found multiple versions: {versions}. All speakers should run the same version.",
                affected_ips=[ip for ips in firmware_versions.values() for ip in ips],
            )
        )
    elif speakers:
        version = next(iter(firmware_versions))
        findings.append(
            Finding(
                severity="ok",
                title="Firmware matched across household",
                detail=f"All speakers report {version}.",
                affected_ips=[sp.ip_address for sp in speakers],
            )
        )

    stereo_groups = _stereo_pairs(speakers)
    for left, right in stereo_groups:
        left_wifi = _is_wifi(left)
        right_wifi = _is_wifi(right)
        left_eth = _is_ethernet(left)
        right_eth = _is_ethernet(right)

        if left_wifi and right_eth:
            findings.append(
                Finding(
                    severity="high",
                    title=f"Stereo pair '{left.zone_name}' is mixed Wi-Fi + Ethernet",
                    detail=(
                        f"{left.zone_name} ({left.ip_address}) is the stereo coordinator on Wi-Fi, "
                        f"while {right.zone_name} ({right.ip_address}) is wired with Wi-Fi disabled. "
                        "Sonos stereo pairs work best when both speakers share the same network path. "
                        "If you cannot wire the left speaker, unplug the right speaker and use Wi-Fi only, "
                        "or physically swap the speakers so the wired unit sits on the left and re-pair."
                    ),
                    affected_ips=[left.ip_address, right.ip_address],
                )
            )
            recommendations.append(
                "Family Room / stereo pair: if only the right-side Ethernet jack is usable, "
                "either swap speakers and re-pair with the wired unit as Left, or unplug Ethernet "
                "and run both speakers on Wi-Fi."
            )
        elif left_eth and right_wifi:
            findings.append(
                Finding(
                    severity="high",
                    title=f"Stereo pair '{left.zone_name}' has wired coordinator but wireless right",
                    detail=(
                        "The left coordinator is wired, but the right speaker is on Wi-Fi. "
                        "Prefer both wired, or both wireless."
                    ),
                    affected_ips=[left.ip_address, right.ip_address],
                )
            )
        elif left_eth and right_eth:
            findings.append(
                Finding(
                    severity="ok",
                    title=f"Stereo pair '{left.zone_name}' is fully wired",
                    detail="Both speakers use Ethernet — ideal topology for a stereo pair.",
                    affected_ips=[left.ip_address, right.ip_address],
                )
            )

    wifi_speakers = [sp for sp in speakers if _is_wifi(sp) and not _is_ethernet(sp)]
    if wifi_speakers:
        channels = {sp.wifi_channel_mhz for sp in wifi_speakers if sp.wifi_channel_mhz}
        if channels:
            ch_list = ", ".join(sorted(channels))
            findings.append(
                Finding(
                    severity="medium",
                    title="Wireless speakers use 2.4 GHz Wi-Fi",
                    detail=(
                        f"Operating channel(s): {ch_list} MHz. "
                        "Many Sonos models (including SYMFONISK Bookshelf S21) are 2.4 GHz only. "
                        "Ethernet is the best way to avoid congestion if a cable is available."
                    ),
                    affected_ips=[sp.ip_address for sp in wifi_speakers],
                )
            )

    for sp in speakers:
        drop_ratio = _rx_drop_ratio(sp)
        if drop_ratio is not None and drop_ratio >= 20:
            findings.append(
                Finding(
                    severity="medium",
                    title=f"High bridge RX drop ratio on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} ({sp.ip_address}) shows {drop_ratio:.1f}% br0 RX drops "
                        f"({sp.br0.rx_dropped:,} / {sp.br0.rx_packets:,}). "
                        "Sonos counts filtered multicast here, but unusually high ratios can indicate stress."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        if sp.phy_errors is not None and sp.phy_errors > 5000:
            findings.append(
                Finding(
                    severity="medium",
                    title=f"Elevated Wi-Fi PHY errors on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} reports {sp.phy_errors:,} PHY errors since last reset. "
                        "Check signal strength, channel congestion, and router 2.4 GHz width (use 20 MHz)."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        if sp.room_calibration_state == "1":
            findings.append(
                Finding(
                    severity="low",
                    title=f"Trueplay may be incomplete on {sp.zone_name}",
                    detail=(
                        f"{sp.zone_name} reports room calibration state 1. "
                        "Re-run Trueplay from the Sonos app after changing speaker placement or wiring."
                    ),
                    affected_ips=[sp.ip_address],
                )
            )

        if sp.br0.rx_errors or sp.br0.tx_errors or sp.eth0.rx_errors or sp.eth0.tx_errors:
            findings.append(
                Finding(
                    severity="high",
                    title=f"Interface errors on {sp.zone_name}",
                    detail=f"{sp.zone_name} reports non-zero interface errors in ifconfig output.",
                    affected_ips=[sp.ip_address],
                )
            )

    if not recommendations:
        recommendations.append(
            "No critical topology issues detected. Consider fixed DHCP reservations for speaker IPs."
        )

    recommendations.extend(
        [
            "Reserve DHCP addresses for each speaker so IPs stay stable.",
            "Keep router 2.4 GHz on 20 MHz channel width for Sonos compatibility.",
            "Re-run Trueplay after any wiring or placement changes.",
        ]
    )
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_recs: list[str] = []
    for rec in recommendations:
        if rec not in seen:
            seen.add(rec)
            unique_recs.append(rec)

    speaker_rows = [_speaker_to_dict(sp) for sp in sorted(speakers, key=lambda s: s.zone_name)]

    return AnalysisReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        speaker_count=len(speakers),
        firmware_versions=firmware_versions,
        speakers=speaker_rows,
        findings=sorted(findings, key=_severity_rank),
        recommendations=unique_recs,
        snapshot=snapshot,
    )


def _stereo_pairs(speakers: list[SpeakerDiagnostics]) -> list[tuple[SpeakerDiagnostics, SpeakerDiagnostics]]:
    by_uid = {sp.local_uid: sp for sp in speakers if sp.local_uid}
    pairs: list[tuple[SpeakerDiagnostics, SpeakerDiagnostics]] = []
    seen: set[tuple[str, str]] = set()

    for left in [sp for sp in speakers if sp.is_stereo_left and sp.channel_map_set]:
        right_uid = None
        for part in left.channel_map_set.split(";"):
            if ":" not in part:
                continue
            uid, rest = part.split(":", 1)
            role = rest.split(",")[-1].upper()
            if role in {"RF", "R", "RIGHT"}:
                right_uid = uid
                break
        if not right_uid:
            continue

        right = by_uid.get(right_uid)
        if right is None:
            continue

        key = tuple(sorted([left.ip_address, right.ip_address]))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((left, right))

    return pairs


def _is_wifi(sp: SpeakerDiagnostics) -> bool:
    conn = (sp.connection_type or "").lower()
    return "wifi" in conn or sp.connection_type_code == "5"


def _is_ethernet(sp: SpeakerDiagnostics) -> bool:
    conn = (sp.connection_type or "").lower()
    return "ethernet" in conn or sp.ethernet_link or sp.connection_type_code == "4"


def _rx_drop_ratio(sp: SpeakerDiagnostics) -> float | None:
    if sp.br0.rx_packets <= 0:
        return None
    return 100.0 * sp.br0.rx_dropped / sp.br0.rx_packets


def _severity_rank(finding: Finding) -> int:
    return {"high": 0, "medium": 1, "low": 2, "ok": 3}.get(finding.severity, 4)


def _speaker_to_dict(sp: SpeakerDiagnostics) -> dict:
    drop_ratio = _rx_drop_ratio(sp)
    return {
        "zone_name": sp.zone_name,
        "ip_address": sp.ip_address,
        "model_name": sp.model_name,
        "model_number": sp.model_number,
        "software_version": sp.software_version,
        "display_version": sp.display_version,
        "connection_type": sp.connection_type,
        "ethernet_link": sp.ethernet_link,
        "ethernet_speed": sp.ethernet_speed,
        "wifi_channel_mhz": sp.wifi_channel_mhz,
        "wifi_mode": sp.wifi_mode,
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
    }
