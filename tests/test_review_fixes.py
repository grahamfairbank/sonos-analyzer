"""Regression tests for the findings of the 2026-11-13 review of the 0.4.0 release candidate."""

import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sonos_analyzer import history, web
from sonos_analyzer.analyze import analyze
from sonos_analyzer.parse import parse_zone_group_state
from tests import household as hh
from tests.helpers import finding, report, snapshot

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def at(minutes, snap=None):
    return history.record_from_snapshot(snap or snapshot(), T0 + timedelta(minutes=minutes))


class R1DamagedHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = history.History(Path(self.tmp.name))
        self.store.save(history.record_from_snapshot(snapshot(), datetime.now(timezone.utc)))

    def test_odd_files_are_skipped(self):
        for i, content in enumerate(['[]', 'null', '{"scanned_at": "2026-11-13T12:00:00"}',
                                     '{"scanned_at": 5, "speakers": {}}', '{"speakers": {}}', '\udcff']):
            (self.store.dir / f"scan-2026-11-13T00000{i}Z.json").write_text(content, errors="surrogateescape")
        records = self.store.load()
        self.assertEqual(len(records), 1)
        history.compare(records, at(0))

    def test_entry_without_counters(self):
        rec = at(-15)
        for sp in rec["speakers"].values():
            del sp["eth0"]
            sp["boot_seq"] = "x"
        changes = history.compare([rec, {"scanned_at": "junk"}], at(0))
        self.assertEqual(changes["restarts"], {})

    def test_history_failure_never_fails_a_scan(self):
        broken = mock.Mock()
        broken.load.side_effect = TypeError("bad record")
        with mock.patch.object(web, "HISTORY", broken), \
                mock.patch.object(web, "collect_snapshot", side_effect=lambda ips: snapshot()):
            r = web.run_scan(["192.168.50.10"], 3.0)
        self.assertTrue(any("Scan history could not be read" in e for e in r.snapshot.fetch_errors))

    def test_future_record_ignored(self):
        future = history.record_from_snapshot(snapshot(), datetime.now(timezone.utc) + timedelta(days=2))
        self.store.save(future)
        self.assertEqual(len(self.store.load()), 1)

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_files_private(self):
        path = next(self.store.dir.glob("scan-*.json"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class R2BackgroundFallback(unittest.TestCase):
    def test_stops_reusing_remembered_speakers(self):
        calls = []

        def run_scan(ips, _timeout):
            calls.append(list(ips))
            raise web.ScanError(web.HTTPStatus.NOT_FOUND if not ips else web.HTTPStatus.BAD_GATEWAY, "x")

        rounds = {"n": 0}

        def sleep(_seconds):
            rounds["n"] += 1
            if rounds["n"] > 6:
                raise KeyboardInterrupt

        with mock.patch.object(web, "run_scan", run_scan), mock.patch.object(web, "_fallback_failures", 0), \
                mock.patch.object(web, "_last_known_ips", return_value=["192.168.50.10"]), \
                mock.patch.object(web.time, "sleep", sleep), mock.patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                web.scan_in_background(15)
        remembered = [c for c in calls if c]
        self.assertEqual(len(remembered), web.MAX_FALLBACK_FAILURES)


class R4GapsInHistory(unittest.TestCase):
    def test_restart_and_ip_across_a_missing_scan(self):
        a = at(0)
        den = next(uid for uid, sp in a["speakers"].items() if sp["ip"] == "192.168.50.10")
        b = at(15)
        del b["speakers"][den]
        c = at(30)
        c["speakers"][den]["boot_seq"] += 1
        c["speakers"][den]["ip"] = "192.168.50.99"
        changes = history.compare([a, b], c)
        self.assertEqual(changes["restarts"], {den: 1})
        self.assertEqual(changes["ip_changes"], {den: ["192.168.50.99"]})
        self.assertEqual(changes["new"], [])

    def test_missing_persists(self):
        a = at(0)
        den = next(uid for uid, sp in a["speakers"].items() if sp["ip"] == "192.168.50.10")
        b, c = at(15), at(30)
        del b["speakers"][den], c["speakers"][den]
        self.assertEqual(history.compare([a, b], c)["missing"], {den: a["scanned_at"]})

    def test_sleeping_portable_is_informational(self):
        a = at(0)
        porch = next(uid for uid, sp in a["speakers"].items() if sp["ip"] == "192.168.50.19")
        b = at(15)
        del b["speakers"][porch]
        changes = history.compare([a], b)
        r = analyze(snapshot(), changes)
        self.assertEqual(finding(r, "portable speaker(s) not seen").severity, "info")
        self.assertIsNone(finding(r, "speaker(s) missing"))


class R5InterfaceErrors(unittest.TestCase):
    def test_wireless_errors_dont_blame_a_cable(self):
        speakers, groups = hh.household()
        speakers[8].br0_rx = (100000, 50, 0)  # Porch Roam on Wi-Fi
        self.assertIsNone(finding(report(speakers, groups), "Interface errors"))

    def test_too_few_packets_not_graded(self):
        speakers, groups = hh.household()
        # Den is wired; its Ethernet port saw 9,000 packets, below the 10,000 needed to judge.
        r = report(speakers, groups)
        den = next(sp for sp in r.speakers if sp["ip_address"] == "192.168.50.10")
        self.assertEqual(den["interface"]["level"]["label"], "Too few packets")


    def test_no_change_since_last_scan(self):
        a, b = at(0), at(15)  # identical counters
        r = analyze(snapshot(), history.compare([a], b))
        den = next(sp for sp in r.speakers if sp["ip_address"] == "192.168.50.10")
        self.assertEqual(den["interface"]["span"], "since last scan")
        self.assertEqual(den["interface"]["level"]["label"], "Too few packets")


class R6OddNumbers(unittest.TestCase):
    def test_odd_bootseq_and_freq(self):
        speakers, groups = hh.household()
        xml = hh.zone_group_state(groups).replace('BootSeq="42"', 'BootSeq="²"', 1).replace(
            'ChannelFreq="5180"', 'ChannelFreq="²"', 1)
        parsed = parse_zone_group_state(xml)
        snap = snapshot()
        for ip, sp in parsed.items():
            snap.speakers[ip].boot_seq = sp.boot_seq
            snap.speakers[ip].wifi_channel_mhz = sp.wifi_channel_mhz or "abc"
        rec = history.record_from_snapshot(snap, T0)
        analyze(snap, history.compare([], rec))
        self.assertIsNone(history.number("²"))
        self.assertIsNone(history.number("9" * 50))


class R10DesktopKey(unittest.TestCase):
    def test_keyless_requests_dont_keep_app_alive(self):
        from tests.test_web import ServerTests

        case = ServerTests("test_page_and_health")
        case.setUp()
        try:
            with mock.patch.object(web, "APP_KEY", "k"):
                web.last_activity = 0.0
                case.request("GET", "/healthz")
                self.assertEqual(web.last_activity, 0.0)
                case.request("GET", "/healthz", headers={"X-Analyzer-Key": "k"})
                self.assertGreater(web.last_activity, 0.0)
                resp, _ = case.request("GET", "/")
                self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
                self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        finally:
            case.doCleanups()


class R11TopologyText(unittest.TestCase):
    def test_control_characters_removed(self):
        _speakers, groups = hh.household()
        xml = hh.zone_group_state(groups).replace('SoftwareVersion="85.0-64110"', 'SoftwareVersion="85.0&#155;31m-1&#10;X"', 1)
        versions = {sp.software_version for sp in parse_zone_group_state(xml).values()}
        self.assertIn("85.031m-1X", versions)


if __name__ == "__main__":
    unittest.main()
