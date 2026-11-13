import html
import unittest

from sonos_analyzer.parse import (
    SpeakerDiagnostics,
    merge_speakers,
    parse_device_description,
    parse_review_xml,
    parse_zone_group_state,
)
from tests import household as hh


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.speakers, _ = hh.household()
        self.parsed = parse_review_xml(hh.review_xml(self.speakers))
        self.fake = hh.by_ip(self.speakers)

    def test_every_block_parsed(self):
        self.assertEqual(set(self.parsed), set(self.fake))

    def test_identity_and_wireless(self):
        den = self.parsed["192.168.50.10"]
        self.assertEqual(den.zone_name, "Den")
        self.assertEqual(den.mac_address, "02:00:00:00:0A:00")
        self.assertEqual(den.connection_type_code, "3")
        self.assertEqual(den.connection_type, "Ethernet")
        self.assertTrue(den.sonosnet_disabled)
        self.assertTrue(den.ethernet_link)
        self.assertEqual(den.ethernet_speed, "100FD")

    def test_ampersand_in_zone_name(self):
        self.assertEqual(self.parsed["192.168.50.12"].zone_name, "Kitchen & Dining")

    def test_ifconfig_counters(self):
        den = self.parsed["192.168.50.10"]
        self.assertEqual((den.br0.rx_packets, den.br0.rx_errors, den.br0.rx_dropped), (100000, 0, 10))
        self.assertEqual(den.eth0.rx_packets, 5000)

    def test_ath_status(self):
        arc = self.parsed["192.168.50.14"]
        self.assertEqual(arc.radio_mode, "sonosnet")
        self.assertEqual(arc.ht_channel_mhz, "5745")
        self.assertEqual(arc.rx_chains, 2)
        self.assertEqual(arc.phy_errors, 1200)
        self.assertEqual(arc.hal_reset_failures, 0)
        self.assertEqual(arc.noise_floor_dbm, [-95, -96])
        # HT Channel 0 means no home theater link.
        self.assertEqual(self.parsed["192.168.50.10"].ht_channel_mhz, "")

    def test_radio_peers(self):
        left = self.parsed["192.168.50.12"]
        self.assertEqual(left.radio_peers[0], ("02:00:00:00:0D:01", 52, 50, "03"))
        self.assertEqual(len(left.radio_peers), 2)

    def test_stp_disabled(self):
        self.assertIs(self.parsed["192.168.50.10"].stp_enabled, False)

    def test_stp_enabled_root(self):
        sp = SpeakerDiagnostics(zone_name="x", ip_address="192.168.50.1")
        from sonos_analyzer.parse import _parse_showstp

        _parse_showstp(
            "br0\n bridge id\t\t8000.020000000a00\n designated root\t8000.020000000a00\n\n"
            "eth0 (1)\n port id\t\t8001\t\t\tstate\t\t\tforwarding\n"
            "ath0 (2)\n port id\t\t8002\t\t\tstate\t\t\tblocking\n",
            sp,
        )
        self.assertTrue(sp.stp_enabled)
        self.assertTrue(sp.stp_is_root)
        self.assertEqual(sp.stp_ports, [("eth0", "forwarding"), ("ath0", "blocking")])

    def test_public_address_block_dropped(self):
        xml = hh.review_xml(self.speakers).replace("<IPAddress>192.168.50.10<", "<IPAddress>8.8.8.8<")
        self.assertNotIn("8.8.8.8", parse_review_xml(xml))

    def test_control_characters_removed(self):
        # C1 CSI (0x9b) is valid in XML and starts a terminal escape sequence.
        xml = hh.review_xml(self.speakers).replace("<ZoneName>Den<", "<ZoneName>Den&#x9b;31m<")
        self.assertEqual(parse_review_xml(xml)["192.168.50.10"].zone_name, "Den31m")


class ZoneGroupTests(unittest.TestCase):
    def setUp(self):
        speakers, groups = hh.household()
        self.xml = hh.zone_group_state(groups)
        self.parsed = parse_zone_group_state(self.xml)

    def test_satellites_included(self):
        self.assertIn("192.168.50.15", self.parsed)
        sub = self.parsed["192.168.50.15"]
        self.assertEqual(sub.bond, "home_theater")
        self.assertEqual(sub.role, "Sub")
        self.assertEqual(sub.room_name, "Living Room")

    def test_stereo_pair(self):
        left, right = self.parsed["192.168.50.12"], self.parsed["192.168.50.13"]
        self.assertEqual((left.bond, left.role), ("stereo_pair", "Left"))
        self.assertEqual(right.role, "Right")
        self.assertEqual(left.room_uid, right.room_uid)
        self.assertTrue(left.is_stereo_coordinator)
        self.assertEqual(left.room_name, "Kitchen & Dining")

    def test_escaped_form(self):
        # Some callers pass the SOAP text still escaped.
        parsed = parse_zone_group_state(html.escape(self.xml, quote=False))
        self.assertEqual(parsed["192.168.50.12"].zone_name, "Kitchen & Dining")

    def test_name_with_markup_characters(self):
        xml = self.xml.replace('ZoneName="Den"', 'ZoneName="Den &lt;&quot;A&amp;B&quot;&gt;"')
        self.assertEqual(parse_zone_group_state(xml)["192.168.50.10"].zone_name, 'Den <"A&B">')

    def test_wifi_enabled_attribute(self):
        self.assertIs(self.parsed["192.168.50.16"].wifi_enabled, False)
        self.assertIsNone(self.parsed["192.168.50.10"].wifi_enabled)

    def test_public_location_skipped(self):
        xml = self.xml.replace("http://192.168.50.10:1400", "http://8.8.8.8:1400")
        self.assertNotIn("8.8.8.8", parse_zone_group_state(xml))

    def test_empty(self):
        self.assertEqual(parse_zone_group_state(""), {})


class DeviceDescriptionTests(unittest.TestCase):
    def test_namespaced(self):
        sp = hh.household()[0][0]
        meta = parse_device_description(hh.device_description(sp))
        self.assertEqual(meta["model_name"], "Sonos Beam")
        self.assertEqual(meta["model_number"], "S14")
        self.assertEqual(meta["room_name"], "Den")

    def test_without_namespace(self):
        meta = parse_device_description("<root><device><modelName>Sonos One</modelName></device></root>")
        self.assertEqual(meta["model_name"], "Sonos One")
        self.assertEqual(meta["model_number"], "")

    def test_no_device(self):
        self.assertEqual(parse_device_description("<root/>"), {})


class MergeTests(unittest.TestCase):
    def test_topology_fills_unknowns(self):
        review = {"192.168.50.16": SpeakerDiagnostics(zone_name="Patio", ip_address="192.168.50.16", mac_address="aa")}
        topo = {"192.168.50.16": SpeakerDiagnostics(zone_name="Patio", ip_address="192.168.50.16", wifi_enabled=False, room_name="Patio")}
        merged = merge_speakers(review, topo)["192.168.50.16"]
        self.assertIs(merged.wifi_enabled, False)
        self.assertEqual(merged.room_name, "Patio")
        self.assertEqual(merged.mac_address, "aa")

    def test_first_value_wins(self):
        a = {"192.168.50.1": SpeakerDiagnostics(zone_name="A", ip_address="192.168.50.1")}
        b = {"192.168.50.1": SpeakerDiagnostics(zone_name="B", ip_address="192.168.50.1")}
        self.assertEqual(merge_speakers(a, b)["192.168.50.1"].zone_name, "A")


if __name__ == "__main__":
    unittest.main()
