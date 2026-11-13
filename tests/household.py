"""A made-up Sonos household for tests, in the formats real speakers return.

Addresses, MACs and IDs are fake. The XML shapes follow real /support/review,
ZoneGroupTopology and device description responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

NET = "192.168.50."


@dataclass
class Speaker:
    n: int  # last octet; also used to derive the MAC and UID
    zone: str
    model: str
    model_number: str
    version: str = "85.0-64110"
    connection_type: str = "5"  # 3 Ethernet, 4 Ethernet with Wi-Fi off, 5 Wi-Fi, 8 home theater link
    connection_label: str = "WiFi"
    eth_link: bool = False
    eth_speed: str = "100FD"
    wifi_enabled: str | None = None  # topology attribute; None leaves it out
    channel_freq: str = "5180"
    home_channel: str = "5180"
    ht_channel: str = "0"
    mode: str = "INFRA (sonosnet)"
    sonosnet_disabled: str = "1"
    phy_errors: int = 1200
    noise_floor: list[int] = field(default_factory=lambda: [-95, -96])
    rx_chains: int = 2
    peers: list[tuple[str, int, int, str]] = field(default_factory=list)
    channel_map: str = ""
    ht_map: str = ""
    satellites: list["Speaker"] = field(default_factory=list)
    calibration: str = "1"
    stp: str = "STP is disabled"
    br0_rx: tuple[int, int, int] = (100000, 0, 10)  # packets, errors, dropped

    @property
    def ip(self) -> str:
        return f"{NET}{self.n}"

    @property
    def mac(self) -> str:
        return f"02:00:00:00:{self.n:02X}:00"

    def radio_mac(self, offset: int = 1) -> str:
        return f"02:00:00:00:{self.n:02X}:{offset:02X}"

    @property
    def uid(self) -> str:
        return f"RINCON_0200000000{self.n:02X}01400"


def ath_status(sp: Speaker) -> str:
    lines = [
        f"Debug info for {sp.mode.split()[0]} mode at 148761",
        "",
        f"Mode: {sp.mode}",
        f"Operating on channel {sp.channel_freq}",
        f"Home channel is {sp.home_channel}",
        f"HT Channel is {sp.ht_channel}",
        f"RF Chains: RX:{sp.rx_chains} TX:{sp.rx_chains}",
    ]
    lines += [f"Noise Floor:  {nf} dBm (chain {i} ctl)" for i, nf in enumerate(sp.noise_floor)]
    lines.append(f"PHY errors since last reading/reset: {sp.phy_errors}        ")
    lines.append("HAL Reset Failures: cnt: 0")
    lines += [f"  Node {mac} - FROM {fr} : TO {to} : STP {stp}" for mac, fr, to, stp in sp.peers]
    return "\n".join(lines) + "\n"


def ifconfig(sp: Speaker) -> str:
    packets, errors, dropped = sp.br0_rx
    return (
        f"br0       Link encap:Ethernet  HWaddr {sp.mac}\n"
        f"          inet addr:{sp.ip}  Bcast:192.168.50.255  Mask:255.255.255.0\n"
        "          UP BROADCAST RUNNING MULTICAST  MTU:1500  Metric:1\n"
        f"          RX packets:{packets} errors:{errors} dropped:{dropped} overruns:0 frame:0\n"
        "          TX packets:90000 errors:0 dropped:0 overruns:0 carrier:0\n"
        "\n"
        f"eth0      Link encap:Ethernet  HWaddr {sp.mac}\n"
        "          UP BROADCAST RUNNING MULTICAST  MTU:1500  Metric:1\n"
        "          RX packets:5000 errors:0 dropped:0 overruns:0 frame:0\n"
        "          TX packets:4000 errors:0 dropped:0 overruns:0 carrier:0\n"
    )


def support_block(sp: Speaker) -> str:
    return (
        "<ZPSupportInfo>"
        "<ZPInfo>"
        f"<ZoneName>{escape(sp.zone)}</ZoneName>"
        f"<LocalUID>{sp.uid}</LocalUID>"
        f"<SerialNumber>02-00-00-00-{sp.n:02X}-00:A</SerialNumber>"
        f"<SoftwareVersion>{sp.version}</SoftwareVersion>"
        "<HardwareVersion>1.20.1.6-1.2</HardwareVersion>"
        f"<IPAddress>{sp.ip}</IPAddress>"
        f"<MACAddress>{sp.mac}</MACAddress>"
        "</ZPInfo>"
        "<Wireless><WirelessInfo Name=\"Wireless Info\">"
        "<WifiModeString>SONOSNET_MODE</WifiModeString>"
        f"<SonosNetDisabled>{sp.sonosnet_disabled}</SonosNetDisabled>"
        f"<ConnectionType>{sp.connection_type}</ConnectionType>"
        f"<ConnectionTypeString>{sp.connection_label}</ConnectionTypeString>"
        "</WirelessInfo></Wireless>"
        "<EnetPorts>"
        f"<Port port=\"0\"><Link>{1 if sp.eth_link else 0}</Link><Speed>{sp.eth_speed if sp.eth_link else ''}</Speed></Port>"
        "</EnetPorts>"
        f"<File name=\"/proc/ath_rincon/status\">{escape(ath_status(sp))}</File>"
        f"<Command cmdline=\"/sbin/ifconfig\">{escape(ifconfig(sp))}</Command>"
        f"<Command cmdline=\"/usr/sbin/brctl showstp br0\">{escape(sp.stp)}</Command>"
        "</ZPSupportInfo>"
    )


def review_xml(speakers: list[Speaker]) -> str:
    blocks = "".join(support_block(sp) for sp in _all(speakers))
    return f'<?xml version="1.0" ?><ZPNetworkInfo>{blocks}</ZPNetworkInfo>'


def _member_attrs(sp: Speaker) -> str:
    attrs = {
        "UUID": sp.uid,
        "Location": f"http://{sp.ip}:1400/xml/device_description.xml",
        "ZoneName": sp.zone,
        "SoftwareVersion": sp.version,
        "ConnectionType": sp.connection_type,
        "ChannelFreq": sp.channel_freq,
        "EthLink": "1" if sp.eth_link else "0",
        "RoomCalibrationState": sp.calibration,
        "BootSeq": "42",
    }
    if sp.wifi_enabled is not None:
        attrs["WifiEnabled"] = sp.wifi_enabled
    if sp.channel_map:
        attrs["ChannelMapSet"] = sp.channel_map
    if sp.ht_map:
        attrs["HTSatChanMapSet"] = sp.ht_map
    return " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items())


def zone_group_state(groups: list[list[Speaker]]) -> str:
    """Inner ZoneGroupState XML, as fetch_zone_group_state returns it (already unescaped once)."""
    out = []
    for members in groups:
        coordinator = members[0].uid
        body = ""
        for sp in members:
            sats = "".join(f"<Satellite {_member_attrs(s)}/>" for s in sp.satellites)
            body += f"<ZoneGroupMember {_member_attrs(sp)}>{sats}</ZoneGroupMember>"
        out.append(f'<ZoneGroup Coordinator="{coordinator}" ID="{coordinator}:1">{body}</ZoneGroup>')
    return f"<ZoneGroupState><ZoneGroups>{''.join(out)}</ZoneGroups></ZoneGroupState>"


def device_description(sp: Speaker) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
        f"<roomName>{escape(sp.zone)}</roomName>"
        f"<modelName>{escape(sp.model)}</modelName>"
        f"<modelNumber>{sp.model_number}</modelNumber>"
        f"<displayVersion>{sp.version.split('-')[0]}</displayVersion>"
        "</device></root>"
    )


def _all(speakers: list[Speaker]) -> list[Speaker]:
    out = []
    for sp in speakers:
        out.append(sp)
        out.extend(sp.satellites)
    return out


def household() -> tuple[list[Speaker], list[list[Speaker]]]:
    """Speakers (satellites nested) and their zone groups."""
    den = Speaker(10, "Den", "Sonos Beam", "S14", connection_type="3", connection_label="Ethernet", eth_link=True,
                  calibration="4")
    studio = Speaker(11, "Studio", "Sonos Era 100", "S39", connection_type="3", connection_label="Ethernet", eth_link=True)
    left = Speaker(12, "Kitchen & Dining", "Sonos One", "S13", channel_freq="2412", home_channel="2412")
    right = Speaker(13, "Kitchen & Dining", "Sonos One", "S13", channel_freq="2462", home_channel="2462")
    pair = f"{left.uid}:LF,LF;{right.uid}:RF,RF"
    left.channel_map = right.channel_map = pair
    arc = Speaker(
        14, "Living Room", "Sonos Arc", "S19", connection_type="3", connection_label="Ethernet",
        eth_link=True, ht_channel="5745", mode="HTAP (sonosnet)",
    )
    sub = Speaker(15, "Living Room", "Sonos Sub", "S26", connection_type="8", connection_label="Home Theater 2.0",
                  channel_freq="5745", mode="SATELLITE (satellite)")
    arc.ht_map = sub.ht_map = f"{arc.uid}:LF,RF;{sub.uid}:SW"
    arc.satellites = [sub]
    patio = Speaker(16, "Patio", "Sonos Amp", "S16", connection_type="3", connection_label="Ethernet",
                    eth_link=True, wifi_enabled="0")
    # Garage is on the maintenance line of the same release; Bedroom missed an update on the main line.
    garage = Speaker(17, "Garage", "Sonos Play:1", "S12", version="84.1-64110", channel_freq="2412", home_channel="2412")
    bedroom = Speaker(18, "Bedroom", "Sonos One", "S13", version="85.0-60000", channel_freq="2412", home_channel="2412")

    porch = Speaker(19, "Porch", "Sonos Roam 2", "S54", calibration="4", channel_freq="2412", home_channel="2412")
    deck = Speaker(20, "Deck", "Sonos Move 2", "S36", calibration="4")

    # Kitchen left and right hear each other well; garage hears the left speaker weakly.
    left.peers = [(right.radio_mac(1), 52, 50, "03"), (garage.radio_mac(2), 20, 18, "03")]
    right.peers = [(left.radio_mac(1), 50, 52, "03")]

    speakers = [den, studio, left, right, arc, patio, garage, bedroom, porch, deck]
    groups = [[den], [studio], [left, right], [arc], [patio], [garage], [bedroom], [porch], [deck]]
    return speakers, groups


def by_ip(speakers: list[Speaker]) -> dict[str, Speaker]:
    return {sp.ip: sp for sp in _all(speakers)}
