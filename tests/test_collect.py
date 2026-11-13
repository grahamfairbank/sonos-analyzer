"""collect_snapshot with the network replaced by the test household."""

import unittest
from unittest import mock

from sonos_analyzer import collect
from tests import household as hh


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.speakers, self.groups = hh.household()
        self.fake = hh.by_ip(self.speakers)
        self.calls = {"review": [], "zgs": []}
        self.down: set[str] = set()

        def review(ip):
            self.calls["review"].append(ip)
            return hh.review_xml(self.speakers)

        def zgs(ip):
            self.calls["zgs"].append(ip)
            return hh.zone_group_state(self.groups)

        patches = {
            "is_reachable": lambda ip: ip not in self.down,
            "fetch_review": review,
            "fetch_zone_group_state": zgs,
            "fetch_device_description": lambda ip: hh.device_description(self.fake[ip]),
            "fetch_ath_status": lambda ip: hh.ath_status(self.fake[ip]),
        }
        for name, fn in patches.items():
            p = mock.patch.object(collect, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_one_review_covers_household(self):
        snap = collect.collect_snapshot(["192.168.50.10", "192.168.50.12"], phy_sample_seconds=0)
        self.assertEqual(self.calls["review"], ["192.168.50.10"])
        self.assertEqual(len(snap.speakers), 11)
        self.assertEqual(snap.speakers["192.168.50.10"].model_name, "Sonos Beam")
        self.assertEqual(snap.speakers["192.168.50.15"].role, "Sub")
        self.assertEqual(snap.fetch_errors, [])

    def test_unreachable_speaker_reported(self):
        self.down = {"192.168.50.10"}
        snap = collect.collect_snapshot(["192.168.50.10", "192.168.50.12"], phy_sample_seconds=0)
        self.assertIn("192.168.50.10: no response on port 1400", snap.fetch_errors)
        self.assertEqual(self.calls["review"], ["192.168.50.12"])
        self.assertEqual(len(snap.speakers), 11)

    def test_nothing_reachable(self):
        self.down = {"192.168.50.10"}
        snap = collect.collect_snapshot(["192.168.50.10"], phy_sample_seconds=0)
        self.assertEqual(snap.speakers, {})

    def test_phy_rate_sampled(self):
        snap = collect.collect_snapshot(["192.168.50.10"], phy_sample_seconds=0.05)
        den = snap.speakers["192.168.50.10"]
        # The rate is the second reading over the measured window, not the review's counter.
        self.assertEqual(den.phy_window_errors, 1200)
        self.assertGreaterEqual(den.phy_window_seconds, 0.05)
        self.assertAlmostEqual(den.phy_error_rate, 1200 / den.phy_window_seconds)
        self.assertEqual(snap.phy_sample.requested_seconds, 0.05)
        self.assertTrue(snap.phy_sample.started_at.endswith("+00:00"))

    def test_phy_skipped(self):
        snap = collect.collect_snapshot(["192.168.50.10"], phy_sample_seconds=0)
        self.assertIsNone(snap.phy_sample)
        self.assertIsNone(snap.speakers["192.168.50.10"].phy_error_rate)

    def test_phy_second_read_fails(self):
        reads = {"n": 0}

        def flaky(ip):
            reads["n"] += 1
            if ip == "192.168.50.12" and reads["n"] > len(self.fake):
                raise OSError("timed out")
            return hh.ath_status(self.fake[ip])

        collect.fetch_ath_status.side_effect = flaky
        snap = collect.collect_snapshot(["192.168.50.10"], phy_sample_seconds=0.05)
        left = snap.speakers["192.168.50.12"]
        self.assertIsNone(left.phy_error_rate)
        self.assertEqual(left.phy_not_measured, "second reading: read failed: timed out")
        self.assertIsNotNone(snap.speakers["192.168.50.10"].phy_error_rate)

    def test_review_error_is_not_fatal(self):
        collect.fetch_review.side_effect = [OSError("timed out"), hh.review_xml(self.speakers)]
        snap = collect.collect_snapshot(["192.168.50.10", "192.168.50.12"], phy_sample_seconds=0)
        self.assertIn("192.168.50.10: timed out", snap.fetch_errors)
        self.assertEqual(len(snap.speakers), 11)


if __name__ == "__main__":
    unittest.main()
