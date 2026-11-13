"""Scan history: saving, pruning and what changed between scans."""

import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonos_analyzer import history
from sonos_analyzer.analyze import analyze
from tests.helpers import finding, snapshot

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def record(at=T0, snap=None):
    return history.record_from_snapshot(snap or snapshot(), at)


def later(rec, minutes):
    out = copy.deepcopy(rec)
    out["scanned_at"] = (datetime.fromisoformat(rec["scanned_at"]) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return out


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = history.History(Path(self.tmp.name) / "hist")

    def test_save_load_clear(self):
        self.assertEqual(self.store.load(), [])
        self.store.save(record(datetime.now(timezone.utc)))
        self.assertEqual(len(self.store.load()), 1)
        self.assertEqual(self.store.clear(), 1)
        self.assertEqual(self.store.load(), [])

    def test_prunes_after_30_days(self):
        old = record(datetime.now(timezone.utc) - timedelta(days=31))
        self.store.save(old)
        self.store.save(record(datetime.now(timezone.utc)))
        self.assertEqual(len(self.store.load()), 1)

    def test_damaged_file_ignored(self):
        self.store.save(record(datetime.now(timezone.utc)))
        (self.store.dir / "scan-bad.json").write_text("{not json")
        self.assertEqual(len(self.store.load()), 1)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.first = record()
        self.uids = {sp["ip"]: uid for uid, sp in self.first["speakers"].items()}

    def test_no_history(self):
        changes = history.compare([], self.first)
        self.assertIsNone(changes["previous_at"])
        self.assertEqual(changes["restarts"], {})

    def test_restart_counted_but_not_update(self):
        den, garage = self.uids["192.168.50.10"], self.uids["192.168.50.17"]
        second = later(self.first, 15)
        second["speakers"][den]["boot_seq"] += 1
        second["speakers"][garage]["boot_seq"] += 1
        second["speakers"][garage]["version"] = "84.2-64110"
        changes = history.compare([self.first], second)
        self.assertEqual(changes["restarts"], {den: 1})
        self.assertEqual(changes["updates"], {garage: ["84.2-64110"]})

    def test_repeated_restarts_are_medium(self):
        den = self.uids["192.168.50.10"]
        chain = [self.first]
        for i in range(3):
            nxt = later(chain[-1], 15)
            nxt["speakers"][den]["boot_seq"] += 1
            chain.append(nxt)
        changes = history.compare(chain[:-1], chain[-1])
        self.assertEqual(changes["restarts"][den], 3)
        f = finding(analyze(snapshot(), changes), "Unexpected restarts")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.affected[0]["note"], "restarted at least 3 times")

    def test_ip_change_is_informational(self):
        den = self.uids["192.168.50.10"]
        second = later(self.first, 15)
        second["speakers"][den]["ip"] = "192.168.50.99"
        changes = history.compare([self.first], second)
        self.assertEqual(changes["ip_changes"], {den: ["192.168.50.99"]})
        f = finding(analyze(snapshot(), changes), "IP address changed")
        self.assertEqual(f.severity, "info")
        self.assertEqual(f.recommendation, "")

    def test_missing_and_new(self):
        den = self.uids["192.168.50.10"]
        second = later(self.first, 15)
        gone = second["speakers"].pop(den)
        second["speakers"]["RINCON_NEW"] = {**gone, "ip": "192.168.50.77", "name": "Attic"}
        changes = history.compare([self.first], second)
        self.assertEqual(changes["missing"], [den])
        self.assertEqual(changes["new"], ["RINCON_NEW"])
        self.assertEqual(finding(analyze(snapshot(), changes), "missing since the last scan").severity, "medium")

    def test_coordinator_change(self):
        kitchen = next(c for c, m in self.first["groups"].items())
        members = self.first["groups"][kitchen]
        other = next(m for m in members if m != kitchen)
        second = later(self.first, 15)
        second["groups"] = {other: members}
        changes = history.compare([self.first], second)
        self.assertEqual(changes["coordinator_changes"][0]["from"], kitchen)
        self.assertEqual(finding(analyze(snapshot(), changes), "Group coordinator changed").severity, "info")

    def test_errors_since_last_scan(self):
        den = self.uids["192.168.50.10"]
        second = later(self.first, 15)
        eth = second["speakers"][den]["eth0"]
        eth["rx_packets"] += 10000
        eth["rx_errors"] += 5
        changes = history.compare([self.first], second)
        self.assertEqual(changes["wired_since"][den]["errors"], 5)
        self.assertEqual(changes["wired_since"][den]["seconds"], 900)
        sp = next(s for s in analyze(snapshot(), changes).speakers if s["ip_address"] == "192.168.50.10")
        self.assertEqual(sp["interface"]["span"], "since last scan")
        self.assertEqual(sp["interface"]["level"]["label"], "High")  # 5 in 10,000 = 0.05%

    def test_restart_resets_wired_baseline(self):
        den = self.uids["192.168.50.10"]
        second = later(self.first, 15)
        second["speakers"][den]["boot_seq"] += 1
        self.assertNotIn(den, history.compare([self.first], second)["wired_since"])

    def test_old_scans_outside_trend_window(self):
        den = self.uids["192.168.50.10"]
        old = later(self.first, -60 * 24 * 8)
        old["speakers"][den]["boot_seq"] -= 1
        changes = history.compare([old], self.first)
        self.assertEqual(changes["restarts"], {})


class DefaultDirTests(unittest.TestCase):
    def test_server_needs_setting(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(history.default_dir())
            self.assertIsNotNone(history.default_dir(app_mode=True))
        with mock.patch.dict(os.environ, {"SONOS_ANALYZER_DATA": "/data"}):
            self.assertEqual(history.default_dir(), Path("/data"))


if __name__ == "__main__":
    unittest.main()


class StpRootTests(unittest.TestCase):
    STP = ("br0\n bridge id\t\t{me}\n designated root\t{root}\n\n"
           "eth0 (1)\n port id\t\t8001\t\t\tstate\t\t\tforwarding\n")

    def household_with_root(self, root):
        from tests import household as hh

        speakers, groups = hh.household()
        for sp in speakers[:2]:
            sp.stp = self.STP.format(me=f"8000.{sp.mac.replace(':', '').lower()}", root=root)
        return snapshot(speakers, groups)

    def test_outside_root_named(self):
        snap = self.household_with_root("1000.aabbccddeeff")
        f = finding(analyze(snap), "SonosNet mesh active")
        self.assertIn("outside your Sonos system (bridge ID 1000.aabbccddeeff)", f.detail)

    def test_root_changes_tracked(self):
        first = record(T0, self.household_with_root("1000.aabbccddeeff"))
        den_bridge = "8000.020000000a00"
        second = record(T0 + timedelta(minutes=15), self.household_with_root(den_bridge))
        changes = history.compare([first], second)
        self.assertEqual(len(changes["stp_root_changes"]), 1)
        f = finding(analyze(self.household_with_root(den_bridge), changes), "spanning-tree root changed")
        self.assertEqual(f.severity, "info")
        self.assertEqual(f.affected[0]["name"], "Den")
        self.assertIn("a device outside Sonos (1000.aabbccddeeff)", f.affected[0]["note"])

    def test_repeated_root_changes_are_low(self):
        roots = ["1000.aabbccddeeff", "8000.020000000a00"] * 2
        chain = [record(T0 + timedelta(minutes=15 * i), self.household_with_root(r)) for i, r in enumerate(roots)]
        changes = history.compare(chain[:-1], chain[-1])
        self.assertEqual(len(changes["stp_root_changes"]), 3)
        f = finding(analyze(self.household_with_root(roots[-1]), changes), "spanning-tree root changed")
        self.assertEqual(f.severity, "low")
        self.assertIn("BPDU", f.recommendation)
