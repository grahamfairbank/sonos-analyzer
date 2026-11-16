"""Pre-release firmware, other Sonos systems, household counts, playing groups and the network map."""

import unittest

from sonos_analyzer import collect
from sonos_analyzer.analyze import _build, analyze, is_prerelease
from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics
from tests.helpers import finding, snapshot

SNAPSHOT_BUILD = "99.0-83023-main_snapshot_05_release"


class PrereleaseTests(unittest.TestCase):
    def test_build_number_of_snapshot_version(self):
        self.assertEqual(_build(SNAPSHOT_BUILD), "83023")
        self.assertEqual(_build("98.0-82170"), "82170")

    def test_detection(self):
        self.assertTrue(is_prerelease(SpeakerDiagnostics("A", "192.168.50.1", build_type="beta")))
        self.assertFalse(is_prerelease(SpeakerDiagnostics("A", "192.168.50.1", build_type="release",
                                                          software_version=SNAPSHOT_BUILD)))
        # No BuildType: fall back to the version string.
        self.assertTrue(is_prerelease(SpeakerDiagnostics("A", "192.168.50.1", software_version=SNAPSHOT_BUILD)))
        self.assertFalse(is_prerelease(SpeakerDiagnostics("A", "192.168.50.1", software_version="98.0-82170")))

    def test_one_beta_speaker_does_not_put_the_rest_behind(self):
        before = finding(analyze(snapshot()), "missed a firmware update").affected_ips
        snap = snapshot()
        beta = next(iter(snap.speakers.values()))
        beta.software_version, beta.build_type = SNAPSHOT_BUILD, "alpha"
        report = analyze(snap)
        f = finding(report, "pre-release firmware")
        self.assertEqual((f.severity, f.affected_ips), ("info", [beta.ip_address]))
        self.assertIn("'alpha'", f.detail)
        # The test household already has one speaker behind; the beta adds no others.
        self.assertEqual(finding(report, "missed a firmware update").affected_ips, before)
        self.assertIn("prerelease", {g["status"] for g in report.firmware})

    def test_whole_household_on_beta(self):
        snap = snapshot()
        for sp in snap.speakers.values():
            sp.build_type = "beta"
        report = analyze(snap)
        self.assertIn("Every speaker runs it", finding(report, "pre-release firmware").detail)
        self.assertNotIn("prerelease", {g["status"] for g in report.firmware})


class OtherSystemTests(unittest.TestCase):
    def test_smaller_system_set_aside(self):
        def sp(ip, hh, gen, name):
            return SpeakerDiagnostics(name, ip, household_id=hh, sw_gen=gen)
        main = {f"192.168.50.{n}": sp(f"192.168.50.{n}", "Sonos_A", "2", f"Room {n}") for n in range(10, 14)}
        old = {"192.168.50.40": sp("192.168.50.40", "Sonos_B", "1", "Garage")}
        snap = HouseholdSnapshot()
        maps, reachable = collect._keep_main_system(snap, [old, main], ["192.168.50.40", "192.168.50.10"])
        self.assertEqual(reachable, ["192.168.50.10"])
        self.assertEqual(set(maps[0]), set(main))
        self.assertEqual(snap.other_systems, [{"sw_gen": "1", "speaker_count": 1, "rooms": ["Garage"]}])
        f = finding(analyze(HouseholdSnapshot(speakers=main, other_systems=snap.other_systems)), "Another Sonos system (S1)")
        self.assertEqual(f.severity, "info")
        self.assertNotIn("Sonos_B", f.detail)

    def test_one_system_untouched(self):
        maps = [{"192.168.50.10": SpeakerDiagnostics("A", "192.168.50.10", household_id="Sonos_A")}]
        snap = HouseholdSnapshot()
        self.assertEqual(collect._keep_main_system(snap, maps, ["192.168.50.10"]), (maps, ["192.168.50.10"]))
        self.assertEqual(snap.other_systems, [])


class OverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = snapshot()
        for n, sp in enumerate(cls.snap.speakers.values()):
            sp.response_ms = 4.0 + n
        cls.report = analyze(cls.snap)

    def test_summary_counts(self):
        s = self.report.summary
        self.assertEqual(s["speakers"], 11)
        self.assertEqual(s["reachable"], 11)
        self.assertEqual(s["stereo_pairs"], 1)
        self.assertEqual(s["home_theaters"], 1)
        self.assertGreaterEqual(s["satellites"], 1)

    def test_groups_cover_every_room(self):
        self.assertEqual(sum(g["room_count"] for g in self.report.groups), self.report.room_count)
        for g in self.report.groups:
            self.assertEqual(g["rooms"][0], g["coordinator"])

    def test_map_covers_every_room_once(self):
        names = [room["name"] for column in self.report.network_map for room in column["rooms"]]
        self.assertEqual(len(names), self.report.room_count)
        titles = [c["title"] for c in self.report.network_map]
        self.assertEqual(titles[0], "Wired")
        self.assertTrue(any(t.startswith("Wi-Fi") for t in titles))

    def test_response_time_and_link_type(self):
        sp = next(iter(self.snap.speakers.values()))
        sp.response_ms, sp.wireless_mode = 250.0, "3"
        row = next(r for r in analyze(self.snap).speakers if r["ip_address"] == sp.ip_address)
        self.assertTrue(row["response_slow"])
        self.assertEqual(row["link_type"], "Satellite link (Wi-Fi only)")
        sp.response_ms, sp.wireless_mode = 4.0, ""


if __name__ == "__main__":
    unittest.main()
