"""Build analysis inputs from the test household without any network access."""

from __future__ import annotations

from sonos_analyzer.analyze import analyze
from sonos_analyzer.parse import (
    HouseholdSnapshot,
    merge_speakers,
    parse_device_description,
    parse_review_xml,
    parse_zone_group_state,
)
from tests import household as hh


def snapshot(speakers=None, groups=None) -> HouseholdSnapshot:
    if speakers is None:
        speakers, groups = hh.household()
    merged = merge_speakers(parse_review_xml(hh.review_xml(speakers)), parse_zone_group_state(hh.zone_group_state(groups)))
    fake = hh.by_ip(speakers)
    for ip, sp in merged.items():
        meta = parse_device_description(hh.device_description(fake[ip]))
        sp.model_name, sp.model_number, sp.display_version = meta["model_name"], meta["model_number"], meta["display_version"]
    return HouseholdSnapshot(speakers=merged, source_ips=[speakers[0].ip])


def report(speakers=None, groups=None):
    return analyze(snapshot(speakers, groups))


def finding(report, text):
    """The finding whose title contains text, or None."""
    return next((f for f in report.findings if text in f.title), None)
