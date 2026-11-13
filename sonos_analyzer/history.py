"""Scan history: keep a small record of each scan and work out what changed.

Each scan is saved as one JSON file. Speakers are keyed by their permanent ID (LocalUID),
so an IP change shows up as a change rather than as a new speaker. Files that can't be
read or don't look like a scan record are skipped, never fatal.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics

FORMAT = 1
KEEP_DAYS = 30
# Trends (restarts, IP and coordinator changes) are counted over this window.
TREND_DAYS = 7
# Records dated further ahead than this (a clock corrected backwards) are ignored.
FUTURE_SLACK = timedelta(hours=1)
_lock = threading.Lock()


def default_dir(app_mode: bool = False) -> Path | None:
    """Where history lives: $SONOS_ANALYZER_DATA, else the user's app-data folder for the desktop app."""
    configured = os.environ.get("SONOS_ANALYZER_DATA")
    if configured:
        return Path(configured)
    if not app_mode:
        return None
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Sonos Analyzer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Sonos Analyzer"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "sonos-analyzer"


def number(value) -> int | None:
    """A plain non-negative integer from speaker text, or None (rejects '²', huge values, junk)."""
    text = str(value).strip()
    return int(text) if re.fullmatch(r"[0-9]{1,10}", text) else None


def record_from_snapshot(snapshot: HouseholdSnapshot, scanned_at: datetime) -> dict:
    """The parts of a scan that later scans are compared against."""
    groups: dict[str, list[str]] = {}
    coordinators: dict[str, str] = {}
    for sp in snapshot.speakers.values():
        if sp.group_id and not sp.is_invisible:
            groups.setdefault(sp.group_id, []).append(sp.local_uid)
            coordinators.setdefault(sp.group_id, sp.coordinator_uid)
    speakers = {}
    for sp in snapshot.speakers.values():
        if not sp.local_uid:
            continue
        speakers[sp.local_uid] = {
            "name": _label(sp),
            "ip": sp.ip_address,
            "model": sp.model_name,
            "version": sp.software_version,
            "boot_seq": number(sp.boot_seq),
            "coordinator": sp.coordinator_uid,
            "eth0": _counters(sp.eth0),
            "br0": _counters(sp.br0),
            "wired": sp.ethernet_link,
        }
    root_ids = [sp.stp_root_id for sp in snapshot.speakers.values() if sp.stp_root_id]
    return {
        "v": FORMAT,
        "scanned_at": scanned_at.isoformat(timespec="seconds"),
        "speakers": speakers,
        "stp_root": max(set(root_ids), key=root_ids.count) if root_ids else "",
        "bridge_ids": {sp.stp_bridge_id: sp.local_uid for sp in snapshot.speakers.values() if sp.stp_bridge_id},
        # Visible groups with more than one member: {coordinator: sorted members}
        "groups": {
            coordinators[gid]: sorted(members)
            for gid, members in groups.items()
            if len(members) > 1 and coordinators.get(gid)
        },
    }


def _label(sp: SpeakerDiagnostics) -> str:
    name = sp.room_name or sp.zone_name or sp.ip_address
    return f"{name} ({sp.role})" if sp.role else name


def _counters(stats) -> dict:
    return {
        "rx_packets": stats.rx_packets,
        "tx_packets": stats.tx_packets,
        "rx_errors": stats.rx_errors,
        "tx_errors": stats.tx_errors,
        "rx_dropped": stats.rx_dropped,
    }


def _when(record) -> datetime | None:
    """The record's scan time if the record is usable, else None."""
    if not isinstance(record, dict) or not isinstance(record.get("speakers"), dict):
        return None
    try:
        when = datetime.fromisoformat(str(record.get("scanned_at")))
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _speaker(record: dict, uid: str) -> dict | None:
    sp = record["speakers"].get(uid)
    return sp if isinstance(sp, dict) else None


class History:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []

        def key(path: Path) -> tuple:
            try:
                return (path.name[:23], path.stat().st_mtime_ns)
            except OSError:  # removed while listing
                return (path.name[:23], 0)

        # Oldest first; files written in the same second keep the order they were written in.
        return sorted(self.dir.glob("scan-*.json"), key=key)

    def load(self, since: datetime | None = None, now: datetime | None = None) -> list[dict]:
        """Usable records, oldest first. Damaged, foreign or future-dated files are skipped."""
        latest = (now or datetime.now(timezone.utc)) + FUTURE_SLACK
        records = []
        for path in self._files():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            when = _when(record)
            if when is None or when > latest or (since is not None and when < since):
                continue
            records.append(record)
        return records

    def save(self, record: dict) -> None:
        with _lock:
            # Speaker names and addresses: readable by this account only (where the OS supports it).
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            stamp = record["scanned_at"].replace(":", "").replace("+0000", "Z")
            path = self.dir / f"scan-{stamp}.json"
            n = 1
            while path.exists():  # two scans in the same second
                path = self.dir / f"scan-{stamp}-{n}.json"
                n += 1
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":")))
            tmp.replace(path)
            self.prune()

    def prune(self, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)
        for path in self._files():
            try:
                when = _when(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, UnicodeDecodeError):
                when = None
            if when is None or when < cutoff:
                path.unlink(missing_ok=True)

    def clear(self) -> int:
        with _lock:
            files = self._files()
            for path in files:
                path.unlink(missing_ok=True)
            return len(files)


def compare(records: list[dict], current: dict) -> dict:
    """What changed: since the previous scan, and trends over the last TREND_DAYS.

    Each speaker is compared with the last earlier scan that contained it, so a speaker that
    was missing from a scan (restarting, or switched off for a while) is still tracked.
    """
    now = _when(current) or datetime.now(timezone.utc)
    usable = [r for r in records if _when(r) is not None]
    previous = usable[-1] if usable else None
    window = [r for r in usable if _when(r) >= now - timedelta(days=TREND_DAYS)]
    chain = [*window, current]

    restarts: dict[str, int] = {}
    ip_changes: dict[str, list[str]] = {}
    updates: dict[str, list[str]] = {}
    last_seen: dict[str, tuple[dict, dict]] = {}  # uid -> (record, speaker entry)
    for record in chain:
        for uid, sp in record["speakers"].items():
            if not isinstance(sp, dict):
                continue
            if uid in last_seen:
                old = last_seen[uid][1]
                boot, old_boot = number(sp.get("boot_seq")), number(old.get("boot_seq"))
                if boot is not None and old_boot is not None and boot > old_boot:
                    # A firmware update restarts the speaker on purpose; don't count that one.
                    if sp.get("version") == old.get("version"):
                        restarts[uid] = restarts.get(uid, 0) + 1
                    else:
                        updates.setdefault(uid, []).append(str(sp.get("version", "")))
                if sp.get("ip") and old.get("ip") and sp.get("ip") != old.get("ip"):
                    ip_changes.setdefault(uid, []).append(str(sp["ip"]))
            last_seen[uid] = (record, sp)

    coordinator_changes = []
    for before, after in zip(chain, chain[1:]):
        old_groups, new_groups = before.get("groups"), after.get("groups")
        if not isinstance(old_groups, dict) or not isinstance(new_groups, dict):
            continue
        old_by_members = {tuple(m): c for c, m in old_groups.items() if isinstance(m, list)}
        for coord, members in new_groups.items():
            old_coord = old_by_members.get(tuple(members)) if isinstance(members, list) else None
            if old_coord and old_coord != coord:
                coordinator_changes.append({"at": after["scanned_at"], "members": members, "from": old_coord, "to": coord})

    stp_root_changes = [
        {"at": after["scanned_at"], "from": before.get("stp_root", ""), "to": after.get("stp_root", "")}
        for before, after in zip(chain, chain[1:])
        if before.get("stp_root") and after.get("stp_root") and before["stp_root"] != after["stp_root"]
    ]
    bridge_ids: dict[str, str] = {}
    for r in chain:
        if isinstance(r.get("bridge_ids"), dict):
            bridge_ids.update(r["bridge_ids"])

    # Each speaker's last appearance before this scan.
    before: dict[str, tuple[dict, dict]] = {}
    for record in window:
        for uid, sp in record["speakers"].items():
            if isinstance(sp, dict):
                before[uid] = (record, sp)
    # Missing: seen earlier in the window but not now, with when it was last seen.
    missing = {uid: rec["scanned_at"] for uid, (rec, _sp) in before.items() if uid not in current["speakers"]}
    new = [uid for uid in current["speakers"] if uid not in before] if window else []

    names, ips, models = {}, {}, {}
    for r in chain:
        for uid, sp in r["speakers"].items():
            if isinstance(sp, dict):
                names[uid], ips[uid], models[uid] = sp.get("name", uid), sp.get("ip", ""), sp.get("model", "")

    return {
        "trend_days": TREND_DAYS,
        "previous_at": previous["scanned_at"] if previous else None,
        "scans_in_window": len(window),
        "first_scan_at": usable[0]["scanned_at"] if usable else None,
        "restarts": restarts,
        "ip_changes": ip_changes,
        "updates": updates,
        "coordinator_changes": coordinator_changes,
        "stp_root_changes": stp_root_changes,
        "bridge_ids": bridge_ids,
        "missing": missing,
        "new": new,
        "names": names,
        "ips": ips,
        "models": models,
        "wired_since": _wired_since(before, current),
    }


def _wired_since(before: dict, current: dict) -> dict:
    """Interface error and drop rates since each speaker's previous scan, if it didn't restart."""
    out = {}
    now = _when(current)
    for uid, sp in current["speakers"].items():
        if uid not in before or not isinstance(sp, dict):
            continue
        record, old = before[uid]
        if number(sp.get("boot_seq")) != number(old.get("boot_seq")):
            continue
        port = "eth0" if sp.get("wired") else "br0"
        a, b = old.get(port), sp.get(port)
        if not isinstance(a, dict) or not isinstance(b, dict):
            continue
        try:
            packets = (b["rx_packets"] - a["rx_packets"]) + (b["tx_packets"] - a["tx_packets"])
            errors = (b["rx_errors"] - a["rx_errors"]) + (b["tx_errors"] - a["tx_errors"])
            dropped = b["rx_dropped"] - a["rx_dropped"]
        except (KeyError, TypeError):
            continue
        if packets < 0 or errors < 0 or dropped < 0:
            continue  # counters went backwards: a restart we couldn't see
        seconds = (now - _when(record)).total_seconds() if now else 0
        out[uid] = {"port": port, "seconds": seconds, "packets": packets, "errors": errors, "dropped": dropped}
    return out
