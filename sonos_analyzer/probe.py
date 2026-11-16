"""Background response-time probe (server mode).

Once a minute (by default) each speaker gets one connection on port 1400, staggered so
they don't all go at once. Each try's time, or failure, goes into an hourly summary per
speaker; runs of failed tries are kept as outages. That's enough to tell a speaker that's
always a little slow from one that drops out every evening, which a scan every 15 minutes
only catches by luck.

Summaries are kept per speaker (by its Sonos ID, so an IP change doesn't split the history)
in one small JSON file next to the scan history, for as long as the scan history.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonos_analyzer.fetch import response_times_ms
from sonos_analyzer.targets import clean_text, is_allowed_target

FORMAT = 1
FILE_NAME = "probe.json"
DEFAULT_INTERVAL_SECONDS = 60
# This many failed tries in a row is an outage (3 minutes at the default interval).
OUTAGE_MIN_FAILURES = 3
SAVE_EVERY_SECONDS = 300
MAX_SPEAKERS = 64


def _hour(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00Z")


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ" if len(stamp) > 17 else "%Y-%m-%dT%H:00Z").replace(
            tzinfo=timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _stamp(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bucket_stats(times: list[float], failures: int) -> dict:
    """Summary of one hour of tries: count, failures, median, 95th percentile, worst, jitter."""
    stats: dict = {"n": len(times) + failures, "fail": failures}
    if times:
        ordered = sorted(times)
        stats["median"] = round(statistics.median(ordered), 1)
        stats["p95"] = round(ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))], 1)
        stats["max"] = round(ordered[-1], 1)
        # Jitter: average change between one try and the next.
        steps = [abs(b - a) for a, b in zip(times, times[1:])]
        stats["jitter"] = round(statistics.fmean(steps), 1) if steps else 0.0
    return stats


class Prober:
    def __init__(self, folder: Path | str, interval_seconds: float = DEFAULT_INTERVAL_SECONDS, keep_days: int = 30):
        self.path = Path(folder) / FILE_NAME
        self.interval = max(float(interval_seconds), 10.0)
        self.keep_days = keep_days
        self.lock = threading.Lock()
        self.targets: dict[str, dict] = {}  # ip -> {"uid", "name"}
        self.speakers: dict[str, dict] = {}  # uid -> {"name", "ip", "hours": {hour: stats}}
        self.outages: list[dict] = []
        self.started = _stamp(datetime.now(timezone.utc))
        # Current hour, not yet summarized: uid -> {"hour", "times", "fail"}
        self._open: dict[str, dict] = {}
        # Failures in a row: uid -> {"count", "start"}
        self._streak: dict[str, dict] = {}
        self._saved = time.monotonic()
        self._load()

    # --- storage -----------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("v") != FORMAT:
            return
        for uid, entry in (data.get("speakers") or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("hours"), dict):
                self.speakers[str(uid)[:80]] = {
                    "name": clean_text(str(entry.get("name", "")))[:80],
                    "ip": str(entry.get("ip", ""))[:15],
                    "hours": {h: s for h, s in entry["hours"].items() if _parse(h) and isinstance(s, dict)},
                }
        self.outages = [o for o in data.get("outages") or [] if isinstance(o, dict) and _parse(o.get("start", ""))]
        self.started = data.get("started") if _parse(data.get("started", "")) else self.started
        self._prune(datetime.now(timezone.utc))

    def save(self) -> None:
        with self.lock:
            data = {"v": FORMAT, "started": self.started, "speakers": self.speakers, "outages": self.outages}
            text = json.dumps(data, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, self.path)
        self._saved = time.monotonic()

    def clear(self) -> None:
        with self.lock:
            self.speakers, self.outages, self._open, self._streak = {}, [], {}, {}
            self.started = _stamp(datetime.now(timezone.utc))
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _prune(self, now: datetime) -> None:
        cutoff = _hour(now - timedelta(days=self.keep_days))
        for entry in self.speakers.values():
            entry["hours"] = {h: s for h, s in entry["hours"].items() if h >= cutoff}
        self.outages = [o for o in self.outages if o["start"] >= cutoff][-500:]

    # --- probing -----------------------------------------------------------

    def set_targets(self, targets: dict[str, dict]) -> None:
        """Speakers to probe: ip -> {"uid", "name"}; private addresses only."""
        with self.lock:
            self.targets = {
                ip: {"uid": str(t["uid"])[:80], "name": clean_text(str(t.get("name", "")))[:80]}
                for ip, t in list(targets.items())[:MAX_SPEAKERS]
                if is_allowed_target(ip) and t.get("uid")
            }

    def record(self, uid: str, name: str, ip: str, ms: float | None, now: datetime) -> None:
        with self.lock:
            entry = self.speakers.setdefault(uid, {"name": name, "ip": ip, "hours": {}})
            entry["name"], entry["ip"] = name or entry["name"], ip
            hour = _hour(now)
            current = self._open.get(uid)
            if current and current["hour"] != hour:
                entry["hours"][current["hour"]] = bucket_stats(current["times"], current["fail"])
                current = None
            if current is None:
                current = self._open[uid] = {"hour": hour, "times": [], "fail": 0}
            streak = self._streak.setdefault(uid, {"count": 0, "start": ""})
            if ms is None:
                current["fail"] += 1
                if streak["count"] == 0:
                    streak["start"] = _stamp(now)
                streak["count"] += 1
            else:
                current["times"].append(ms)
                if streak["count"] >= OUTAGE_MIN_FAILURES:
                    self.outages.append(
                        {"uid": uid, "name": entry["name"], "start": streak["start"], "end": _stamp(now),
                         "failures": streak["count"]}
                    )
                streak["count"], streak["start"] = 0, ""

    def probe_once(self, ip: str) -> float | None:
        times = response_times_ms(ip, tries=1)
        return round(times[0], 1) if times else None

    def run(self) -> None:
        while True:
            with self.lock:
                targets = list(self.targets.items())
            if not targets:
                time.sleep(self.interval)
                continue
            gap = self.interval / len(targets)
            for ip, target in targets:
                started = time.monotonic()
                try:
                    ms = self.probe_once(ip)
                    self.record(target["uid"], target["name"], ip, ms, datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001 - one bad probe never stops the loop
                    pass
                time.sleep(max(0.0, gap - (time.monotonic() - started)))
            if time.monotonic() - self._saved >= SAVE_EVERY_SECONDS:
                try:
                    with self.lock:
                        self._prune(datetime.now(timezone.utc))
                    self.save()
                except OSError:
                    pass

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="response-probe").start()

    # --- reading -----------------------------------------------------------

    def snapshot(self, days: int = 7, now: datetime | None = None) -> dict:
        """Recent data for analysis and the page, including the hour in progress and open outages."""
        now = now or datetime.now(timezone.utc)
        cutoff = _hour(now - timedelta(days=days))
        with self.lock:
            speakers = {}
            for uid, entry in self.speakers.items():
                hours = {h: dict(s) for h, s in entry["hours"].items() if h >= cutoff}
                current = self._open.get(uid)
                if current:
                    hours[current["hour"]] = {**bucket_stats(current["times"], current["fail"]), "partial": True}
                speakers[uid] = {"name": entry["name"], "ip": entry["ip"], "hours": dict(sorted(hours.items()))}
            outages = [dict(o) for o in self.outages if o["start"] >= cutoff]
            for uid, streak in self._streak.items():
                if streak["count"] >= OUTAGE_MIN_FAILURES and uid in self.speakers:
                    outages.append({"uid": uid, "name": self.speakers[uid]["name"], "start": streak["start"],
                                    "end": "", "failures": streak["count"], "ongoing": True})
            return {"interval_seconds": self.interval, "since": self.started, "days": days,
                    "speakers": speakers, "outages": outages}
