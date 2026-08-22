"""Command-line interface for sonos-analyzer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sonos_analyzer.analyze import analyze
from sonos_analyzer.discovery import discover_speakers
from sonos_analyzer.fetch import fetch_zone_group_state, probe_speaker
from sonos_analyzer.parse import (
    HouseholdSnapshot,
    merge_speakers,
    parse_device_description,
    parse_review_xml,
    parse_zone_group_state,
)
from sonos_analyzer.report import print_terminal, write_html, write_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze Sonos speakers on your local network.",
        epilog="""Examples:
  sonos-analyzer
  sonos-analyzer --ips 192.168.1.165,192.168.1.89
  sonos-analyzer --json report.json --html report.html
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--ips",
        help="Comma-separated speaker IPs (skip SSDP discovery)",
    )
    parser.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        help="SSDP discovery timeout in seconds (default: 3)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Write JSON report to PATH",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="Write HTML report to PATH",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress terminal report (useful with --json/--html only)",
    )

    args = parser.parse_args(argv)

    if args.ips:
        ips = [ip.strip() for ip in args.ips.split(",") if ip.strip()]
    else:
        discovered = discover_speakers(timeout=args.discover_timeout)
        ips = [d.ip for d in discovered]
        if not ips:
            print(
                "No Sonos speakers discovered via SSDP. "
                "Try --ips 192.168.1.x or open a speaker's IP in a browser to confirm it is reachable.",
                file=sys.stderr,
            )
            return 1

    snapshot = _collect_snapshot(ips)
    if not snapshot.speakers:
        print("Could not read diagnostics from any speaker.", file=sys.stderr)
        for err in snapshot.fetch_errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    report = analyze(snapshot)

    if not args.quiet:
        print_terminal(report)

    if args.json:
        write_json(report, Path(args.json))
        if not args.quiet:
            print(f"Wrote JSON report to {args.json}")

    if args.html:
        write_html(report, Path(args.html))
        if not args.quiet:
            print(f"Wrote HTML report to {args.html}")

    return 0


def _collect_snapshot(ips: list[str]) -> HouseholdSnapshot:
    snapshot = HouseholdSnapshot(source_ips=ips)
    review_maps = []
    group_maps = []
    descriptions: dict[str, dict[str, str]] = {}

    for ip in ips:
        review_xml, desc_xml, err = probe_speaker(ip)
        if err:
            snapshot.fetch_errors.append(f"{ip}: {err.message}")
            continue

        if review_xml:
            review_maps.append(parse_review_xml(review_xml))
        if desc_xml:
            descriptions[ip] = parse_device_description(desc_xml)

        try:
            group_xml = fetch_zone_group_state(ip)
            group_maps.append(parse_zone_group_state(group_xml))
        except Exception as exc:  # noqa: BLE001 - surface as non-fatal
            snapshot.fetch_errors.append(f"{ip} zone group: {exc}")

    snapshot.speakers = merge_speakers(*review_maps, *group_maps)

    for ip, meta in descriptions.items():
        if ip not in snapshot.speakers:
            continue
        speaker = snapshot.speakers[ip]
        speaker.model_name = meta.get("model_name", speaker.model_name)
        speaker.model_number = meta.get("model_number", speaker.model_number)
        speaker.display_version = meta.get("display_version", speaker.display_version)

    return snapshot


if __name__ == "__main__":
    raise SystemExit(main())
