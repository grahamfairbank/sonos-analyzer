import unittest

from sonos_analyzer.report import render_html, report_payload
from tests import household as hh
from tests.helpers import finding, report


class AnalyzeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = report()
        cls.speakers = {sp["ip_address"]: sp for sp in cls.report.speakers}

    def test_counts(self):
        self.assertEqual(self.report.speaker_count, 11)
        # Kitchen pair and Living Room home theater each count as one room.
        self.assertEqual(self.report.room_count, 9)

    def test_wifi_off_advice_skips_models_without_ethernet_port(self):
        f = finding(self.report, "Wi-Fi radio still on")
        self.assertIsNotNone(f)
        # The Beam has an Ethernet port; the Era 100 (USB-C adapter) can't turn Wi-Fi off.
        self.assertEqual(f.affected_ips, ["192.168.50.10"])

    def test_wifi_off_advice_skips_every_listed_model(self):
        for model in ["Sonos Era 100", "Sonos Era 100 SL", "Sonos Era 300", "Sonos Move", "Sonos Move 2",
                      "Sonos Roam", "Sonos Roam 2", "Sonos Roam SL", "Sonos Sub Mini"]:
            with self.subTest(model=model):
                sp = hh.Speaker(30, "Test", model, "S00", connection_type="3", connection_label="Ethernet", eth_link=True)
                self.assertIsNone(finding(report([sp], [[sp]]), "Wi-Fi radio still on"))

    def test_wifi_off_advice_kept_for_unknown_models(self):
        sp = hh.Speaker(30, "Test", "", "", connection_type="3", connection_label="Ethernet", eth_link=True)
        self.assertIsNotNone(finding(report([sp], [[sp]]), "Wi-Fi radio still on"))

    def test_wifi_disabled_in_topology_means_radio_off(self):
        self.assertFalse(self.speakers["192.168.50.16"]["radio_on"])
        self.assertTrue(self.speakers["192.168.50.10"]["radio_on"])

    def test_trueplay_ignores_portables(self):
        f = finding(self.report, "Trueplay")
        self.assertEqual(f.affected_ips, ["192.168.50.10"])
        self.assertEqual(self.speakers["192.168.50.19"]["trueplay"], "not applicable")
        self.assertEqual(self.speakers["192.168.50.20"]["trueplay"], "not applicable")

    def test_trueplay_many_portables_one_beam(self):
        roams = [hh.Speaker(30 + i, f"Roam {i}", "Sonos Roam", "S27", calibration="4") for i in range(9)]
        beam = hh.Speaker(40, "TV", "Sonos Beam", "S14", calibration="4")
        r = report([*roams, beam], [[sp] for sp in [*roams, beam]])
        f = finding(r, "Trueplay")
        self.assertIn("1 room", f.title)
        self.assertEqual(f.affected_ips, [beam.ip])

    def test_trueplay_skips_port_boost_amp(self):
        sps = [hh.Speaker(30 + i, m, f"Sonos {m}", "S00", calibration="4") for i, m in enumerate(["Port", "Boost", "Amp"])]
        r = report(sps, [[sp] for sp in sps])
        self.assertIsNone(finding(r, "Trueplay"))
        self.assertEqual({sp["trueplay"] for sp in r.speakers}, {"not applicable"})

    def test_trueplay_only_portables_untuned(self):
        roam = hh.Speaker(30, "Porch", "Sonos Roam", "S27", calibration="4")
        self.assertIsNone(finding(report([roam], [[roam]]), "Trueplay"))

    def test_stereo_pair_split_channels(self):
        f = finding(self.report, "split across Wi-Fi channels")
        self.assertEqual(sorted(f.affected_ips), ["192.168.50.12", "192.168.50.13"])
        self.assertIn("Kitchen & Dining", f.title)

    def test_firmware_lines(self):
        statuses = {g["version"]: g["status"] for g in self.report.firmware}
        self.assertEqual(statuses, {"85.0-64110": "newest", "84.1-64110": "maintenance", "85.0-60000": "behind"})

    def test_firmware_outlier_finding(self):
        f = finding(self.report, "missed a firmware update")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.affected_ips, ["192.168.50.18"])
        self.assertIn("expected 85.0-64110", f.affected[0]["note"])

    def test_maintenance_line_same_release(self):
        f = finding(self.report, "current on Sonos's maintenance line")
        self.assertEqual(f.severity, "ok")
        self.assertEqual(f.affected_ips, ["192.168.50.17"])
        self.assertIn("same Sonos release", f.detail)
        self.assertIn("build 64110", f.detail)

    def test_maintenance_line_different_build(self):
        main = hh.Speaker(30, "A", "Sonos Era 100", "S39", version="98.0-82170")
        old = hh.Speaker(31, "B", "Sonos Play:1", "S12", version="86.11-80000")
        r = report([main, old], [[main], [old]])
        f = finding(r, "maintenance line")
        self.assertEqual(f.severity, "info")
        self.assertNotIn("same Sonos release", f.detail)
        self.assertIsNone(finding(r, "missed a firmware update"))

    def test_third_line_and_old_build_are_outliers(self):
        sps = [
            hh.Speaker(30, "A", "Sonos Era 100", "S39", version="98.0-82170"),
            hh.Speaker(31, "B", "Sonos Play:1", "S12", version="86.11-82170"),
            hh.Speaker(32, "C", "Sonos Play:1", "S12", version="86.11-82170"),
            hh.Speaker(33, "D", "SYMFONISK Bookshelf", "S21", version="86.10-80000"),
            hh.Speaker(34, "E", "Sonos Connect", "S2", version="80.2-70000"),
        ]
        r = report(sps, [[sp] for sp in sps])
        statuses = {g["version"]: g["status"] for g in r.firmware}
        self.assertEqual(statuses["98.0-82170"], "newest")
        self.assertEqual(statuses["86.11-82170"], "maintenance")
        self.assertEqual(statuses["86.10-80000"], "behind")
        self.assertEqual(statuses["80.2-70000"], "behind")
        f = finding(r, "missed a firmware update")
        self.assertEqual(sorted(f.affected_ips), ["192.168.50.33", "192.168.50.34"])
        self.assertIn("expected 86.11-82170", f.affected[0]["note"] + f.affected[1]["note"])

    def test_single_version(self):
        a = hh.Speaker(30, "A", "Sonos One", "S13")
        b = hh.Speaker(31, "B", "Sonos Five", "S23")
        r = report([a, b], [[a], [b]])
        self.assertIsNotNone(finding(r, "Firmware matched"))
        self.assertIsNone(finding(r, "maintenance line"))

    def test_bonded_room_mixed_firmware(self):
        speakers, groups = hh.household()
        speakers[3].version = "85.0-60000"  # Kitchen right
        f = finding(report(speakers, groups), "has speakers on different firmware")
        self.assertEqual(f.severity, "high")

    def test_home_theater_satellite(self):
        sub = self.speakers["192.168.50.15"]
        self.assertEqual(sub["network_path"], "satellite")
        self.assertEqual(sub["room_name"], "Living Room")

    def test_signal_links(self):
        links = {frozenset((l["a"], l["b"])): l for l in self.report.connections["links"]}
        kitchen = links[frozenset(("192.168.50.12", "192.168.50.13"))]
        self.assertEqual(kitchen["strength"], 51)
        self.assertEqual(kitchen["band"], "strong")
        # Only one side reported the garage link; both directions come from that report.
        self.assertEqual(links[frozenset(("192.168.50.12", "192.168.50.17"))]["band"], "weak")


class ReportTests(unittest.TestCase):
    def test_payload_is_json_ready(self):
        import json

        payload = report_payload(report())
        json.dumps(payload)
        self.assertEqual(payload["speaker_count"], 11)

    def test_html_escapes_speaker_text(self):
        sp = hh.Speaker(30, "<script>alert(1)</script>", "Sonos One", "S13")
        html = render_html(report([sp], [[sp]]))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
