"""Parse Sonos diagnostic XML into structured data."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


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
    is_stereo_left: bool = False
    is_stereo_right: bool = False
    is_stereo_coordinator: bool = False


@dataclass
class HouseholdSnapshot:
    speakers: dict[str, SpeakerDiagnostics] = field(default_factory=dict)
    source_ips: list[str] = field(default_factory=list)
    fetch_errors: list[str] = field(default_factory=list)


def parse_review_xml(xml_text: str) -> dict[str, SpeakerDiagnostics]:
    root = ET.fromstring(xml_text)
    speakers: dict[str, SpeakerDiagnostics] = {}

    for block in root.findall("ZPSupportInfo"):
        speaker = _parse_support_block(block)
        if speaker and speaker.ip_address:
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
        el = device.find(f"d:{tag}", ns) or device.find(tag)
        return (el.text or "").strip() if el is not None else ""

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

    unescaped = html.unescape(xml_text.strip())
    root = ET.fromstring(unescaped)
    speakers: dict[str, SpeakerDiagnostics] = {}

    for group in root.findall(".//ZoneGroup"):
        coordinator = group.attrib.get("Coordinator", "")
        group_id = group.attrib.get("ID", "")

        for member in group.findall("ZoneGroupMember"):
            location = member.attrib.get("Location", "")
            ip = _ip_from_location(location)
            if not ip:
                continue

            channel_map = member.attrib.get("ChannelMapSet", "")
            speaker = SpeakerDiagnostics(
                zone_name=member.attrib.get("ZoneName", ip),
                ip_address=ip,
                local_uid=member.attrib.get("UUID", ""),
                software_version=member.attrib.get("SoftwareVersion", ""),
                connection_type_code=member.attrib.get("ConnectionType", ""),
                wifi_channel_mhz=member.attrib.get("ChannelFreq", ""),
                behind_wifi_extender=member.attrib.get("BehindWifiExtender", "0") == "1",
                room_calibration_state=member.attrib.get("RoomCalibrationState", ""),
                boot_seq=member.attrib.get("BootSeq", ""),
                airplay_enabled=member.attrib.get("AirPlayEnabled", "0") == "1",
                is_invisible=member.attrib.get("Invisible", "0") == "1",
                channel_map_set=channel_map,
                coordinator_uid=coordinator,
                group_id=group_id,
                ethernet_link=member.attrib.get("EthLink", "0") == "1",
            )
            speaker.connection_type = _connection_type_label(speaker.connection_type_code)
            _apply_channel_map_roles(speaker, channel_map, coordinator, member.attrib.get("UUID", ""))
            speakers[ip] = speaker

    return speakers


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
                if current in ("", 0, False, None, []) and value not in ("", 0, False, None, []):
                    setattr(existing, key, value)
    return merged


def _parse_support_block(block: ET.Element) -> SpeakerDiagnostics | None:
    zp = block.find("ZPInfo")
    if zp is None:
        return None

    def zp_text(tag: str) -> str:
        el = zp.find(tag)
        return (el.text or "").strip() if el is not None else ""

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

    enet = block.find("EnetPorts/Port")
    if enet is not None:
        speaker.ethernet_link = _child_text(enet, "Link") == "1"
        speaker.ethernet_speed = _child_text(enet, "Speed")

    for cmd in block.findall("Command"):
        if cmd.attrib.get("cmdline") == "/sbin/ifconfig" and cmd.text:
            _parse_ifconfig(cmd.text, speaker)

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


def _parse_ath_status(text: str, speaker: SpeakerDiagnostics) -> None:
    channel_match = re.search(r"Operating on channel (\d+)", text)
    if channel_match:
        speaker.wifi_channel_mhz = channel_match.group(1)
    home_match = re.search(r"Home channel is (\d+)", text)
    if home_match:
        speaker.wifi_home_channel_mhz = home_match.group(1)

    phy_match = re.search(r"PHY errors since last reading/reset:\s*(\d+)", text)
    if phy_match:
        speaker.phy_errors = int(phy_match.group(1))

    hal_match = re.search(r"HAL Reset Failures: cnt:\s*(\d+)", text)
    if hal_match:
        speaker.hal_reset_failures = int(hal_match.group(1))

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
        "4": "Ethernet (WiFi disabled)",
        "5": "WiFi",
        "3": "SonosNet",
    }.get(code, code or "Unknown")


def _child_text(parent: ET.Element, tag: str) -> str:
    el = parent.find(tag)
    return (el.text or "").strip() if el is not None else ""


def _ip_from_location(location: str) -> str | None:
    match = re.match(r"https?://([^:/]+)", location)
    return match.group(1) if match else None
