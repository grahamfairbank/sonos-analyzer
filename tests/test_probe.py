"""Background response-time probe: hourly summaries, outages, storage and findings."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonos_analyzer import probe
from sonos_analyzer.analyze import analyze
from tests.helpers import finding, snapshot

T0 = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


def feed(prober, uid, ip, values, start=T0, step=60):
    """values: ms per try, None for a failed try."""
    for i, ms in enumerate(values):
        prober.record(uid, uid, ip, ms, start + timedelta(seconds=step * i))


class BucketTests(unittest.TestCase):
    def test_stats(self):
        s = probe.bucket_stats([2.0, 4.0, 3.0, 100.0], 1)
        self.assertEqual((s["n"], s["fail"], s["median"], s["max"]), (5, 1, 3.5, 100.0))
        self.assertEqual(s["p95"], 100.0)
        self.assertAlmostEqual(s["jitter"], (2 + 1 + 97) / 3, places=1)

    def test_all_failed(self):
        self.assertEqual(probe.bucket_stats([], 3), {"n": 3, "fail": 3})


class ProberTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.p = probe.Prober(self.dir.name, 60)

    def test_hour_rollover_and_partial(self):
        feed(self.p, "A", "192.168.50.10", [2.0] * 70)  # 70 minutes: one full hour + 10 min
        snap = self.p.snapshot(now=T0 + timedelta(hours=1, minutes=10))
        hours = list(snap["speakers"]["A"]["hours"].values())
        self.assertEqual(hours[0]["n"], 60)
        self.assertTrue(hours[1]["partial"])
        self.assertEqual(hours[1]["n"], 10)

    def test_outage_recorded_after_three_failures(self):
        feed(self.p, "A", "192.168.50.10", [2.0, None, None, None, None, 2.0, None, None, 2.0])
        outages = self.p.snapshot(now=T0 + timedelta(minutes=10))["outages"]
        self.assertEqual(len(outages), 1)
        self.assertEqual(outages[0]["failures"], 4)

    def test_ongoing_outage(self):
        feed(self.p, "A", "192.168.50.10", [2.0, None, None, None])
        o = self.p.snapshot(now=T0 + timedelta(minutes=5))["outages"]
        self.assertTrue(o[0]["ongoing"])

    def test_save_load_clear(self):
        feed(self.p, "A", "192.168.50.10", [2.0] * 65)
        self.p.save()
        path = Path(self.dir.name) / probe.FILE_NAME
        if sys.platform != "win32":  # Windows has no POSIX permissions
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        again = probe.Prober(self.dir.name, 60)
        self.assertIn("A", again.speakers)
        again.clear()
        self.assertFalse(path.exists())

    def test_damaged_file_ignored(self):
        (Path(self.dir.name) / probe.FILE_NAME).write_text("{not json")
        self.assertEqual(probe.Prober(self.dir.name, 60).speakers, {})
        (Path(self.dir.name) / probe.FILE_NAME).write_text(json.dumps({"v": 99}))
        self.assertEqual(probe.Prober(self.dir.name, 60).speakers, {})

    def test_targets_private_only(self):
        self.p.set_targets({"8.8.8.8": {"uid": "X"}, "192.168.50.10": {"uid": "A", "name": "Den"}, "192.168.50.11": {}})
        self.assertEqual(list(self.p.targets), ["192.168.50.10"])


class ProbeFindingTests(unittest.TestCase):
    def setUp(self):
        self.snap = snapshot()
        self.sps = [sp for sp in self.snap.speakers.values() if sp.local_uid]
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.p = probe.Prober(self.dir.name, 60)

    def steady(self, skip=(), ms=3.0, hours=4):
        for sp in self.sps:
            if sp.local_uid not in skip:
                feed(self.p, sp.local_uid, sp.ip_address, [ms] * (60 * hours))

    def run_analysis(self):
        return analyze(self.snap, probe=self.p.snapshot(now=T0 + timedelta(hours=5)))

    def test_quiet_household_no_findings(self):
        self.steady()
        self.assertFalse([f for f in self.run_analysis().findings if f.category == "monitoring"])

    def test_single_speaker_outage_is_low(self):
        bad = self.sps[0]
        self.steady(skip={bad.local_uid})
        feed(self.p, bad.local_uid, bad.ip_address, [3.0] * 30 + [None] * 5 + [3.0] * 30)
        f = finding(self.run_analysis(), "stopped answering for 3 minutes")
        self.assertEqual(f.severity, "low")
        self.assertEqual(f.affected_ips, [bad.ip_address])
        self.assertIn("1 time(s), longest 5 min", f.affected[0]["note"])

    def test_everyone_down_together_is_info(self):
        for sp in self.sps:
            feed(self.p, sp.local_uid, sp.ip_address, [3.0] * 30 + [None] * 4 + [3.0] * 30)
        report = self.run_analysis()
        self.assertEqual(finding(report, "All speakers stopped answering").severity, "info")
        self.assertIsNone(finding(report, "stopped answering for 3 minutes"))

    def test_consistently_slow_speaker(self):
        slow = self.sps[0]
        self.steady(skip={slow.local_uid})
        feed(self.p, slow.local_uid, slow.ip_address, [150.0] * 240)
        f = finding(self.run_analysis(), "answer slowly most of the time")
        self.assertEqual(f.affected_ips, [slow.ip_address])

    def test_time_pattern(self):
        from sonos_analyzer.analyze import _time_pattern
        base = datetime(2026, 11, 10, 19, 30).astimezone()
        moments = [base + timedelta(days=d) for d in range(3)] + [base + timedelta(days=1, hours=9)]
        self.assertIn("3 of 4 happened between", _time_pattern(moments))
        self.assertEqual(_time_pattern(moments[:2]), "")


class WebProbeTests(unittest.TestCase):
    def test_no_probe_without_background_scans(self):
        from sonos_analyzer import web
        saved = (web.HISTORY, web.SCAN_INTERVAL_MINUTES, web.PROBE)
        try:
            with tempfile.TemporaryDirectory() as folder:
                web.configure(folder, 0)  # desktop app and laptop setups: no background scans
                self.assertIsNone(web.PROBE)
                self.assertEqual(web.monitoring_status()["probe_seconds"], 0)
                self.assertIsNone(web.full_payload(analyze(snapshot()))["probe"])
        finally:
            web.HISTORY, web.SCAN_INTERVAL_MINUTES, web.PROBE = saved

    def test_payload_maps_probe_to_current_ips(self):
        from sonos_analyzer import web
        with tempfile.TemporaryDirectory() as folder:
            prober = probe.Prober(folder, 60)
            snap = snapshot()
            sp = next(s for s in snap.speakers.values() if s.local_uid)
            feed(prober, sp.local_uid, sp.ip_address, [2.0] * 70)
            saved = web.PROBE
            web.PROBE = prober
            try:
                data = web.probe_payload(analyze(snap))
            finally:
                web.PROBE = saved
        entry = data["speakers"][sp.ip_address]
        self.assertEqual(entry["week"]["n"], 70)
        self.assertEqual(entry["week"]["outages"], 0)


if __name__ == "__main__":
    unittest.main()
