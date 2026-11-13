"""Parse Sonos diagnostic XML into structured data."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from sonos_analyzer.targets import clean_text, is_allowed_target


@dataclass
class IfconfigStats:
    rx_packets: int = 0
    tx_packets: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    rx_errors: int = 0
    tx_errors: int = 0


@dataclass
class SpeakerDiagnostics:
    zone_name: str
    ip_address: str
    mac_address: str = ""
    serial_number: str = ""
    local_uid: str = ""
    model_name: str = ""
    model_number: str = ""
    software_version: str = ""
    display_version: str = ""
    hardware_version: str = ""
    connection_type: str = ""
    connection_type_code: str = ""
    wifi_mode: str = ""
    ethernet_link: bool = False
    ethernet_speed: str = ""
    wifi_channel_mhz: str = ""
    wifi_home_channel_mhz: str = ""
    behind_wifi_extender: bool = False
    room_calibration_state: str = ""
    boot_seq: str = ""
    airplay_enabled: bool = False
    is_invisible: bool = False
    channel_map_set: str = ""
    coordinator_uid: str = ""
    group_id: str = ""
    br0: IfconfigStats = field(default_factory=IfconfigStats)
    eth0: IfconfigStats = field(default_factory=IfconfigStats)
    phy_errors: int | None = None
    noise_floor_dbm: list[int] = field(default_factory=list)
    hal_reset_failures: int | None = None
    # Older (Atheros) radios: how hard the radio is tuning out interference.
    # ANI level 0-9 (higher = more interference) or a weak-signal level; Sonos maps both to
    # one score (6 best .. 1.5) and combines it with the noise floor in its network matrix.
    ani_level: int | None = None
    weak_signal_score: float | None = None
    is_stereo_left: bool = False
    is_stereo_right: bool = False
    is_stereo_coordinator: bool = False
    # Room / bonded-set membership (from zone group topology)
    room_name: str = ""
    room_uid: str = ""
    role: str = ""
    bond: str = ""  # "stereo_pair", "home_theater" or ""
    ht_sat_chan_map_set: str = ""
    wifi_enabled: bool | None = None
    # Radio / bridge detail (from /support/review)
    radio_mode: str = ""
    ht_channel_mhz: str = ""
    rx_chains: int = 0
    sonosnet_disabled: bool | None = None
    stp_enabled: bool | None = None
    stp_is_root: bool = False
    stp_bridge_id: str = ""  # this speaker's bridge ID, e.g. 8000.020000000a00 (priority.MAC)
    stp_root_id: str = ""  # the bridge ID of the spanning-tree root it sees
    stp_ports: list[tuple[str, str]] = field(default_factory=list)
    # Other radios this speaker hears: (mac, signal from, signal to, stp state)
    radio_peers: list[tuple[str, int, int, str]] = field(default_factory=list)
    # PHY errors per second, sampled over a short window by the collector
    phy_error_rate: float | None = None
    phy_window_errors: int | None = None  # errors counted in that window
    phy_window_seconds: float | None = None  # window length, from reset read to second read
    phy_not_measured: str = ""  # why there is no rate, when the speaker has a radio counter


@dataclass
class PhySample:
    """How the PHY error rates were measured, so readers can judge them."""

    requested_seconds: float
    started_at: str  # UTC, ISO 8601: when the counters were cleared
    ended_at: str  # UTC: when they were read again


@dataclass
class HouseholdSnapshot:
    speakers: dict[str, SpeakerDiagnostics] = field(default_factory=dict)
    source_ips: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)
    phy_sample: PhySample | None = None


def parse_review_xml(xml_text: str) -> dict[str, SpeakerDiagnostics]:
    root = ET.fromstring(xml_text)
    speakers: dict[str, SpeakerDiagnostics] = {}

    for block in root.findall("ZPSupportInfo"):
        speaker = _parse_support_block(block)
        if speaker and is_allowed_target(speaker.ip_address):
            speakers[speaker.ip_address] = speaker

    return speakers


def parse_device_description(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    device = root.find("d:device", ns)
    if device is None:
        device = root.find("device")
    if device is None:
        return {}

    def text(tag: str) -> str:
        # Elements without children are falsy, so `or` cannot be used here.
        el = device.find(f"d:{tag}", ns)
        if el is None:
            el = device.find(tag)
        return clean_text((el.text or "").strip()) if el is not None else ""

    return {
        "model_name": text("modelName"),
        "model_number": text("modelNumber"),
        "display_version": text("displayVersion"),
        "room_name": text("roomName"),
    }


def parse_zone_group_state(xml_text: str) -> dict[str, SpeakerDiagnostics]:
    """Parse escaped ZoneGroupState XML from SOAP response."""
    if not xml_text:
        return {}

    # fetch_zone_group_state returns the inner XML already unescaped by ElementTree;
    # unescaping again breaks room names that contain "&", "<" or quotes.
    text = xml_text.strip()
    if text.startswith("&lt;"):
        text = html.unescape(text)
    root = ET.fromstring(text)
    speakers: dict[str, SpeakerDiagnostics] = {}

    for group in root.findall(".//ZoneGroup"):
        coordinator = group.attrib.get("Coordinator", "")
        group_id = group.attrib.get("ID", "")

        for member in group.findall("ZoneGroupMember"):
            # Home theater surrounds and subs are nested under their primary.
            for element in [member, *member.findall("Satellite")]:
                speaker = _parse_zone_member(element, coordinator, group_id)
                if speaker:
                    speakers[speaker.ip_address] = speaker

    names_by_uid = {sp.local_uid: sp.zone_name for sp in speakers.values()}
    for speaker in speakers.values():
        speaker.room_name = names_by_uid.get(speaker.room_uid, speaker.zone_name)

    return speakers


def _parse_zone_member(member: ET.Element, coordinator: str, group_id: str) -> SpeakerDiagnostics | None:
    attrib = member.attrib
    ip = _ip_from_location(attrib.get("Location", ""))
    if not ip:
        return None

    # Every attribute is speaker-supplied: strip control characters (as for review text).
    def attr(name: str, default: str = "") -> str:
        return clean_text(attrib.get(name, default))

    uuid = attr("UUID")
    channel_map = attr("ChannelMapSet")
    ht_map = attr("HTSatChanMapSet")
    freq = attr("ChannelFreq")
    speaker = SpeakerDiagnostics(
        zone_name=attr("ZoneName", ip),
        ip_address=ip,
        local_uid=uuid,
        software_version=attr("SoftwareVersion"),
        connection_type_code=attr("ConnectionType"),
        wifi_channel_mhz=freq if re.fullmatch(r"[0-9]{1,5}", freq) else "",
        behind_wifi_extender=attrib.get("BehindWifiExtender", "0") == "1",
        room_calibration_state=attr("RoomCalibrationState"),
        boot_seq=attr("BootSeq"),
        airplay_enabled=attrib.get("AirPlayEnabled", "0") == "1",
        is_invisible=attrib.get("Invisible", "0") == "1",
        channel_map_set=channel_map,
        ht_sat_chan_map_set=ht_map,
        coordinator_uid=clean_text(coordinator),
        group_id=clean_text(group_id),
        ethernet_link=attrib.get("EthLink", "0") == "1",
    )
    if "WifiEnabled" in attrib:
        speaker.wifi_enabled = attrib["WifiEnabled"] == "1"
    speaker.connection_type = _connection_type_label(speaker.connection_type_code)
    _apply_channel_map_roles(speaker, channel_map, coordinator, uuid)

    # The first entry of either map is the room's primary speaker.
    bond_map = ht_map or channel_map
    channels = _parse_channel_map(bond_map)
    speaker.room_uid = next(iter(channels), uuid)
    if uuid in channels:
        speaker.bond = "home_theater" if ht_map else "stereo_pair"
        speaker.role = _role_label(channels[uuid])
    return speaker


def _parse_channel_map(channel_map: str) -> dict[str, str]:
    """'UID:LF,LF;UID:RF,RF' -> {uid: channels}, in order."""
    channels: dict[str, str] = {}
    for part in channel_map.split(";"):
        if ":" in part:
            uid, chans = part.split(":", 1)
            channels[uid] = chans.upper()
    return channels


def _role_label(channels: str) -> str:
    first = channels.split(",")[0]
    if channels in {"LF,LF", "LF"}:
        return "Left"
    if channels in {"RF,RF", "RF"}:
        return "Right"
    if channels == "LF,RF":
        return "Front"
    if first == "SW":
        return "Sub"
    if first == "LR":
        return "Left surround"
    if first == "RR":
        return "Right surround"
    return channels


def merge_speakers(*maps: dict[str, SpeakerDiagnostics]) -> dict[str, SpeakerDiagnostics]:
    merged: dict[str, SpeakerDiagnostics] = {}
    for source in maps:
        for ip, speaker in source.items():
            if ip not in merged:
                merged[ip] = speaker
                continue
            existing = merged[ip]
            for key, value in speaker.__dict__.items():
                current = getattr(existing, key)
                if isinstance(current, IfconfigStats):
                    continue
                if current is None and value is not None:
                    # e.g. WifiEnabled="0" from topology must fill an unknown, not be dropped.
                    setattr(existing, key, value)
                elif current in ("", 0, False, []) and value not in ("", 0, False, None, []):
                    setattr(existing, key, value)
    return merged


def _parse_support_block(block: ET.Element) -> SpeakerDiagnostics | None:
    zp = block.find("ZPInfo")
    if zp is None:
        return None

    def zp_text(tag: str) -> str:
        el = zp.find(tag)
        return clean_text((el.text or "").strip()) if el is not None else ""

    speaker = SpeakerDiagnostics(
        zone_name=zp_text("ZoneName"),
        ip_address=zp_text("IPAddress"),
        mac_address=zp_text("MACAddress"),
        serial_number=zp_text("SerialNumber"),
        local_uid=zp_text("LocalUID"),
        software_version=zp_text("SoftwareVersion"),
        hardware_version=zp_text("HardwareVersion"),
    )

    wireless = block.find("Wireless/WirelessInfo")
    if wireless is not None:
        speaker.wifi_mode = _child_text(wireless, "WifiModeString")
        conn = _child_text(wireless, "ConnectionTypeString")
        if conn:
            speaker.connection_type = conn
        speaker.connection_type_code = _child_text(wireless, "ConnectionType")
        sonosnet_disabled = _child_text(wireless, "SonosNetDisabled")
        if sonosnet_disabled:
            speaker.sonosnet_disabled = sonosnet_disabled == "1"

    # Multi-port models list every port; report the one with a link.
    for enet in block.findall("EnetPorts/Port"):
        if _child_text(enet, "Link") == "1":
            speaker.ethernet_link = True
            speaker.ethernet_speed = _child_text(enet, "Speed")
            break

    for cmd in block.findall("Command"):
        if cmd.attrib.get("cmdline") == "/sbin/ifconfig" and cmd.text:
            _parse_ifconfig(cmd.text, speaker)
        elif "brctl showstp" in cmd.attrib.get("cmdline", "") and cmd.text:
            _parse_showstp(cmd.text, speaker)

    for file_el in block.findall("File"):
        if file_el.attrib.get("name") == "/proc/ath_rincon/status" and file_el.text:
            _parse_ath_status(file_el.text, speaker)

    return speaker


def _parse_ifconfig(text: str, speaker: SpeakerDiagnostics) -> None:
    current = None
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("br0 "):
            current = speaker.br0
        elif line.startswith("eth0 "):
            current = speaker.eth0
        elif current is None:
            continue
        elif "RX packets:" in line:
            m = re.search(
                r"RX packets:(\d+) errors:(\d+) dropped:(\d+)",
                line,
            )
            if m:
                current.rx_packets = int(m.group(1))
                current.rx_errors = int(m.group(2))
                current.rx_dropped = int(m.group(3))
        elif "TX packets:" in line:
            m = re.search(
                r"TX packets:(\d+) errors:(\d+) dropped:(\d+)",
                line,
            )
            if m:
                current.tx_packets = int(m.group(1))
                current.tx_errors = int(m.group(2))
                current.tx_dropped = int(m.group(3))


def _parse_showstp(text: str, speaker: SpeakerDiagnostics) -> None:
    if "STP is disabled" in text:
        speaker.stp_enabled = False
        return

    bridge_id = re.search(r"bridge id\s+(\S+)", text)
    if not bridge_id:
        return
    speaker.stp_enabled = True
    speaker.stp_bridge_id = bridge_id.group(1).lower()
    root = re.search(r"designated root\s+(\S+)", text)
    speaker.stp_root_id = root.group(1).lower() if root else ""
    speaker.stp_is_root = bool(root) and root.group(1) == bridge_id.group(1)

    port = None
    for line in text.splitlines():
        header = re.match(r"^(\S+) \(\d+\)", line)
        if header:
            port = header.group(1)
            continue
        state = re.search(r"\bstate\s+(\w+)", line)
        if port and state:
            speaker.stp_ports.append((port, state.group(1)))
            port = None


def _parse_ath_status(text: str, speaker: SpeakerDiagnostics) -> None:
    mode_match = re.search(r"Mode: \w+ \((\w+)\)", text)
    if mode_match:
        speaker.radio_mode = mode_match.group(1)
    ht_match = re.search(r"HT Channel is (\d+)", text)
    if ht_match and ht_match.group(1) != "0":
        speaker.ht_channel_mhz = ht_match.group(1)
    chains_match = re.search(r"RF Chains: RX:(\d+)", text)
    if chains_match:
        speaker.rx_chains = int(chains_match.group(1))
    channel_match = re.search(r"Operating on channel (\d+)", text)
    if channel_match:
        speaker.wifi_channel_mhz = channel_match.group(1)
    home_match = re.search(r"Home channel is (\d+)", text)
    if home_match:
        speaker.wifi_home_channel_mhz = home_match.group(1)

    phy_match = re.search(r"PHY errors since last reading/reset:\s*(\d+)", text)
    if phy_match:
        speaker.phy_errors = int(phy_match.group(1))

    weak = re.search(r"OFDM Weak signal level: (\d+)", text)
    ani = re.search(r"OFDM ANI level: (\d+)", text)
    if weak:
        speaker.weak_signal_score = float(weak.group(1))
    elif ani:
        speaker.ani_level = int(ani.group(1))
        speaker.weak_signal_score = (12 - speaker.ani_level) / 2  # as Sonos's review.js

    hal_match = re.search(r"HAL Reset Failures: cnt:\s*(\d+)", text)
    if hal_match:
        speaker.hal_reset_failures = int(hal_match.group(1))

    # Node xx:xx:xx:xx:xx:xx - FROM 63 {61} : TO 63 {61} : STP 03
    for match in re.finditer(
        r"^\s*Node ([0-9A-Fa-f:]{17}) - FROM (\d+)(?: \d+)? : TO (\d+)(?: \d+)? : STP (\w+)",
        text,
        re.MULTILINE,
    ):
        speaker.radio_peers.append(
            (match.group(1).upper(), int(match.group(2)), int(match.group(3)), match.group(4))
        )

    for match in re.finditer(r"Noise Floor:\s*(-?\d+)\s*dBm", text):
        speaker.noise_floor_dbm.append(int(match.group(1)))


def _apply_channel_map_roles(
    speaker: SpeakerDiagnostics,
    channel_map: str,
    coordinator: str,
    uuid: str,
) -> None:
    speaker.is_stereo_coordinator = uuid == coordinator and bool(channel_map)
    if not channel_map:
        return

    for part in channel_map.split(";"):
        if ":" not in part:
            continue
        uid, rest = part.split(":", 1)
        role = rest.split(",")[-1] if "," in rest else rest
        if uid != uuid:
            continue
        role_upper = role.upper()
        speaker.is_stereo_left = role_upper in {"LF", "L", "LEFT"}
        speaker.is_stereo_right = role_upper in {"RF", "R", "RIGHT"}


def _connection_type_label(code: str) -> str:
    return {
        "3": "Ethernet",
        "4": "Ethernet (WiFi disabled)",
        "5": "WiFi",
        "8": "Home theater link",
    }.get(code, code or "Unknown")


def _child_text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    return clean_text((el.text or "").strip()) if el is not None else ""


def _ip_from_location(location: str) -> str | None:
    match = re.match(r"https?://([^:/]+)", location)
    return match.group(1) if match and is_allowed_target(match.group(1)) else None
