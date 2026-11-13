"""Scan history: keep a small record of each scan and work out what changed.

Each scan is saved as one JSON file. Speakers are keyed by their permanent ID (LocalUID),
so an IP change shows up as a change rather than as a new speaker.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sonos_analyzer.parse import HouseholdSnapshot, SpeakerDiagnostics

KEEP_DAYS = 30
# Trends (restarts, IP and coordinator changes) are counted over this window.
TREND_DAYS = 7
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


def record_from_snapshot(snapshot: HouseholdSnapshot, scanned_at: datetime) -> dict:
    """The parts of a scan that later scans are compared against."""
    groups: dict[str, list[str]] = {}
    for sp in snapshot.speakers.values():
        if sp.group_id and not sp.is_invisible:
            groups.setdefault(sp.group_id, []).append(sp.local_uid)
    speakers = {}
    for sp in snapshot.speakers.values():
        if not sp.local_uid:
            continue
        speakers[sp.local_uid] = {
            "name": _label(sp),
            "ip": sp.ip_address,
            "model": sp.model_name,
            "version": sp.software_version,
            "boot_seq": _int(sp.boot_seq),
            "coordinator": sp.coordinator_uid,
            "eth0": _counters(sp.eth0),
            "br0": _counters(sp.br0),
            "wired": sp.ethernet_link,
        }
    root_ids = [sp.stp_root_id for sp in snapshot.speakers.values() if sp.stp_root_id]
    return {
        "scanned_at": scanned_at.isoformat(timespec="seconds"),
        "speakers": speakers,
        "stp_root": max(set(root_ids), key=root_ids.count) if root_ids else "",
        "bridge_ids": {sp.stp_bridge_id: sp.local_uid for sp in snapshot.speakers.values() if sp.stp_bridge_id},
        # Visible groups with more than one member: {coordinator: sorted members}
        "groups": {
            coord: sorted(members)
            for gid, members in groups.items()
            if len(members) > 1 and (coord := next((s.coordinator_uid for s in snapshot.speakers.values() if s.group_id == gid), ""))
        },
    }


def _label(sp: SpeakerDiagnostics) -> str:
    name = sp.room_name or sp.zone_name or sp.ip_address
    return f"{name} ({sp.role})" if sp.role else name


def _int(value: str) -> int | None:
    return int(value) if str(value).isdigit() else None


def _counters(stats) -> dict:
    return {
        "rx_packets": stats.rx_packets,
        "tx_packets": stats.tx_packets,
        "rx_errors": stats.rx_errors,
        "tx_errors": stats.tx_errors,
        "rx_dropped": stats.rx_dropped,
    }


class History:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        # Oldest first; files written in the same second keep the order they were written in.
        def key(path: Path) -> tuple:
            try:
                return (path.name[:23], path.stat().st_mtime_ns)
            except OSError:  # removed while listing
                return (path.name[:23], 0)

        return sorted(self.dir.glob("scan-*.json"), key=key)

    def load(self, since: datetime | None = None) -> list[dict]:
        records = []
        for path in self._files():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                when = datetime.fromisoformat(record["scanned_at"])
            except (OSError, ValueError, KeyError):
                continue  # a damaged file shouldn't stop monitoring
            if since is None or when >= since:
                records.append(record)
        return records

    def save(self, record: dict) -> None:
        with _lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = record["scanned_at"].replace(":", "").replace("+0000", "Z")
            path = self.dir / f"scan-{stamp}.json"
            n = 1
            while path.exists():  # two scans in the same second
                path = self.dir / f"scan-{stamp}-{n}.json"
                n += 1
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
            self.prune()

    def prune(self, now: datetime | None = None) -> None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)
        for path in self._files():
            try:
                when = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["scanned_at"])
            except (OSError, ValueError, KeyError):
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
    """What changed: since the previous scan, and trends over the last TREND_DAYS."""
    previous = records[-1] if records else None
    now = datetime.fromisoformat(current["scanned_at"])
    window = [r for r in records if datetime.fromisoformat(r["scanned_at"]) >= now - timedelta(days=TREND_DAYS)]
    chain = [*window, current]

    restarts: dict[str, int] = {}
    ip_changes: dict[str, list[str]] = {}
    updates: dict[str, list[str]] = {}
    for before, after in zip(chain, chain[1:]):
        for uid, sp in after["speakers"].items():
            old = before["speakers"].get(uid)
            if not old:
                continue
            if sp["boot_seq"] is not None and old["boot_seq"] is not None and sp["boot_seq"] > old["boot_seq"]:
                # A firmware update restarts the speaker on purpose; don't count that one.
                if sp["version"] == old["version"]:
                    restarts[uid] = restarts.get(uid, 0) + 1
                else:
                    updates.setdefault(uid, []).append(sp["version"])
            if sp["ip"] != old["ip"]:
                ip_changes.setdefault(uid, []).append(sp["ip"])

    coordinator_changes = []
    for before, after in zip(chain, chain[1:]):
        old_by_members = {tuple(m): c for c, m in before.get("groups", {}).items()}
        for coord, members in after.get("groups", {}).items():
            old_coord = old_by_members.get(tuple(members))
            if old_coord and old_coord != coord:
                coordinator_changes.append(
                    {"at": after["scanned_at"], "members": members, "from": old_coord, "to": coord}
                )

    stp_root_changes = [
        {"at": after["scanned_at"], "from": before.get("stp_root", ""), "to": after.get("stp_root", "")}
        for before, after in zip(chain, chain[1:])
        if before.get("stp_root") and after.get("stp_root") and before["stp_root"] != after["stp_root"]
    ]
    bridge_ids = {}
    for r in chain:
        bridge_ids.update(r.get("bridge_ids", {}))

    missing, new = [], []
    if previous:
        missing = [uid for uid in previous["speakers"] if uid not in current["speakers"]]
        new = [uid for uid in current["speakers"] if uid not in previous["speakers"]]

    return {
        "trend_days": TREND_DAYS,
        "previous_at": previous["scanned_at"] if previous else None,
        "scans_in_window": len(window),
        "first_scan_at": records[0]["scanned_at"] if records else None,
        "restarts": restarts,
        "ip_changes": ip_changes,
        "updates": updates,
        "coordinator_changes": coordinator_changes,
        "stp_root_changes": stp_root_changes,
        "bridge_ids": bridge_ids,
        "missing": missing,
        "new": new,
        "names": {uid: sp["name"] for r in chain for uid, sp in r["speakers"].items()},
        "ips": {uid: sp["ip"] for r in chain for uid, sp in r["speakers"].items()},
        "wired_since": _wired_since(previous, current),
    }


def _wired_since(previous: dict | None, current: dict) -> dict:
    """Interface error and drop rates since the previous scan, for speakers that didn't restart."""
    out = {}
    if not previous:
        return out
    span = (datetime.fromisoformat(current["scanned_at"]) - datetime.fromisoformat(previous["scanned_at"])).total_seconds()
    for uid, sp in current["speakers"].items():
        old = previous["speakers"].get(uid)
        if not old or sp["boot_seq"] != old["boot_seq"]:
            continue
        port = "eth0" if sp["wired"] else "br0"
        a, b = old[port], sp[port]
        packets = (b["rx_packets"] - a["rx_packets"]) + (b["tx_packets"] - a["tx_packets"])
        errors = (b["rx_errors"] - a["rx_errors"]) + (b["tx_errors"] - a["tx_errors"])
        dropped = b["rx_dropped"] - a["rx_dropped"]
        if packets < 0 or errors < 0 or dropped < 0:
            continue  # counters went backwards: treat as a restart we couldn't see
        out[uid] = {"port": port, "seconds": span, "packets": packets, "errors": errors, "dropped": dropped}
    return out
