"""Serve the web UI with a made-up demo household, for README screenshots.

    python3 docs/demo_household.py [port]

Nothing on the network is contacted: every speaker request is answered from invented
data in the speakers' real response formats (tests/household.py). Names, addresses, MACs
and IDs are fake. The household has a few deliberate problems so the page has something
to show: a noisy 2.4 GHz speaker that drops out in the evenings, a stereo pair that
missed an update, a speaker that keeps restarting, and a wired speaker with its radio on.
"""

from __future__ import annotations

import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sonos_analyzer import collect, history, probe, web  # noqa: E402
from sonos_analyzer.parse import RouterCheck  # noqa: E402
from tests import household as hh  # noqa: E402

MAIN, LEGACY, OLDER = "98.0-82170", "86.11-82170", "97.2-81220"
DISPLAY = {MAIN: "27.3", LEGACY: "27.3", OLDER: "27.1"}
CH36, CH149, CH6, HT = "5180", "5745", "2437", "5825"


def wifi(n, zone, model, number, freq=CH36, **kw):
    kw.setdefault("noise_floor", [-101, -100])
    kw.setdefault("phy_errors", random.randint(400, 900))
    return hh.Speaker(n, zone, model, number, version=kw.pop("version", MAIN), channel_freq=freq, home_channel=freq,
                      mode="INFRA (station)", **kw)


def wired(n, zone, model, number, **kw):
    return hh.Speaker(n, zone, model, number, version=kw.pop("version", MAIN), connection_type=kw.pop("ct", "4"),
                      connection_label=kw.pop("label", "Ethernet (WiFi Disabled)"), eth_link=True,
                      br0_rx=(4000, 0, 2), **kw)


def household():
    random.seed(7)
    arc = wired(20, "Living Room", "Sonos Arc Ultra", "S45", ct="3", label="Ethernet", ht_channel=HT,
                mode="HTAP (sonosnet)", calibration="4", phy_errors=310)
    sats = [
        hh.Speaker(21, "Living Room", "Sonos Era 300", "S41", version=MAIN, connection_type="8",
                   connection_label="Home Theater 2.0", channel_freq=HT, mode="SATELLITE (satellite)", phy_errors=4),
        hh.Speaker(22, "Living Room", "Sonos Era 300", "S41", version=MAIN, connection_type="8",
                   connection_label="Home Theater 2.0", channel_freq=HT, mode="SATELLITE (satellite)", phy_errors=2),
        hh.Speaker(23, "Living Room", "Sonos Sub 4", "S50", version=MAIN, connection_type="8",
                   connection_label="Home Theater 2.0", channel_freq=HT, mode="SATELLITE (satellite)", phy_errors=0,
                   noise_floor=[]),
    ]
    arc.ht_map = "".join([f"{arc.uid}:LF,RF;", f"{sats[0].uid}:LR;", f"{sats[1].uid}:RR;", f"{sats[2].uid}:SW"])
    for s in sats:
        s.ht_map = arc.ht_map
    arc.satellites = sats

    kl = wifi(30, "Kitchen", "Sonos Era 100", "S39", version=OLDER)
    kr = wifi(31, "Kitchen", "Sonos Era 100", "S39", version=OLDER)
    kl.channel_map = kr.channel_map = f"{kl.uid}:LF,LF;{kr.uid}:RF,RF"
    dining = wifi(32, "Dining Room", "Sonos One", "S13", calibration="4")
    office = wifi(33, "Office", "Sonos Era 100", "S39", calibration="4")
    bedroom = wifi(34, "Primary Bedroom", "Sonos Five", "S23", freq=CH149, calibration="4")
    bath = wifi(35, "Bathroom", "Sonos One SL", "S38", freq=CH149)
    kids = wifi(36, "Kids' Room", "Sonos Play:1", "S12", freq=CH6, version=LEGACY, noise_floor=[-84, -86],
                phy_errors=9500)
    garage = wifi(37, "Garage", "Sonos Play:1", "S12", version=LEGACY)
    porch = wifi(38, "Porch", "Sonos Roam 2", "S54", calibration="4")
    den = wired(40, "Den", "Sonos Beam", "S31", ct="3", label="Ethernet", calibration="4", phy_errors=2400)
    patio = wired(41, "Patio", "Sonos Amp", "S16", wifi_enabled="0")
    vinyl = wired(42, "Record Player", "Sonos Port", "S35", wifi_enabled="0")

    radios = [kl, kr, dining, office, garage, porch]
    for a in radios:
        a.peers = [(b.radio_mac(1), random.randint(24, 52), random.randint(24, 52), "03") for b in radios if b is not a]
    for a, b in ((bedroom, bath),):
        a.peers = [(b.radio_mac(1), 44, 41, "03")]
        b.peers = [(a.radio_mac(1), 41, 44, "03")]

    speakers = [arc, kl, kr, dining, office, bedroom, bath, kids, garage, porch, den, patio, vinyl]
    groups = [[arc], [kl, kr, dining], [office], [bedroom, bath], [kids], [garage], [porch], [den], [patio], [vinyl]]
    return speakers, groups


def description(sp):
    return hh.device_description(sp).replace(
        f"<displayVersion>{sp.version.split('-')[0]}</displayVersion>", f"<displayVersion>{DISPLAY[sp.version]}</displayVersion>"
    )


def fake_probe(prober, speakers, now):
    """24 hours of background checks: quiet, except Kids' Room in the evenings."""
    rng = random.Random(3)
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
    for sp in hh._all(speakers):
        wired_ = sp.eth_link
        for minute in range(24 * 60 + now.minute):
            at = start + timedelta(minutes=minute)
            local_hour = at.astimezone().hour
            ms = rng.uniform(0.3, 0.9) if wired_ else rng.uniform(1.5, 6) + (rng.random() < 0.03) * rng.uniform(20, 60)
            if sp.zone == "Kids' Room" and 19 <= local_hour <= 21:
                if rng.random() < 0.25:
                    ms = None
                else:
                    ms = rng.uniform(40, 260)
            prober.record(sp.uid, sp.zone, sp.ip, None if ms is None else round(ms, 1), at)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18405
    speakers, groups = household()
    fake = hh.by_ip(speakers)
    patches = {
        "is_reachable": lambda ip: True,
        "fetch_review": lambda ip: hh.review_xml(speakers),
        "fetch_zone_group_state": lambda ip: hh.zone_group_state(groups),
        "fetch_device_description": lambda ip: description(fake[ip]),
        "fetch_ath_status": lambda ip: hh.ath_status(fake[ip]),
        "response_times_ms": lambda ip: [0.4, 0.5, 0.6] if fake[ip].eth_link else [1.8, 2.4, 3.9],
    }
    for name, fn in patches.items():
        mock.patch.object(collect, name, side_effect=fn).start()
    mock.patch.object(collect.network, "check_router",
                      return_value=RouterCheck(searched=True, gateways=[])).start()
    mock.patch.object(collect.network, "in_container", return_value=True).start()

    folder = Path(tempfile.mkdtemp(prefix="sonos-demo-"))
    web.HISTORY = history.History(folder)
    web.SCAN_INTERVAL_MINUTES = 15
    web.PROBE = probe.Prober(folder, 60)
    now = datetime.now(timezone.utc)
    fake_probe(web.PROBE, speakers, now)
    web.PROBE.set_targets({sp.ip: {"uid": sp.uid, "name": sp.zone} for sp in hh._all(speakers)})

    # An earlier scan in which the Record Player had booted fewer times: it restarted since.
    first = collect.collect_snapshot([speakers[0].ip], phy_sample_seconds=0)
    record = history.record_from_snapshot(first, now - timedelta(hours=6))
    for uid, entry in record["speakers"].items():
        if entry["name"] == "Record Player":
            entry["boot_seq"] = 39
    web.HISTORY.save(record)

    web.run_scan([speakers[0].ip], 1)
    server = ThreadingHTTPServer(("127.0.0.1", port), web.Handler)
    print(f"Demo household on http://127.0.0.1:{port}/ (history in {folder})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
