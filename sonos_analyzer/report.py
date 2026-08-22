"""Render analysis reports to terminal, JSON, and HTML."""

from __future__ import annotations

import json
from dataclasses import asdict
from html import escape
from pathlib import Path

from sonos_analyzer.analyze import AnalysisReport, Finding


SEVERITY_LABEL = {
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "ok": "OK",
}


def print_terminal(report: AnalysisReport) -> None:
    print(f"Sonos household analysis ({report.speaker_count} speaker(s))")
    print(f"Generated: {report.generated_at}")
    print()

    print("Speakers")
    print("-" * 72)
    for sp in report.speakers:
        role = []
        if sp["is_stereo_left"]:
            role.append("stereo L/coordinator")
        if sp["is_stereo_right"]:
            role.append("stereo R")
        role_text = f" [{', '.join(role)}]" if role else ""
        drop = sp["br0_rx_drop_ratio_pct"]
        drop_text = f", RX drop {drop}%" if drop is not None else ""
        print(
            f"  {sp['zone_name']:<22} {sp['ip_address']:<16} "
            f"{sp['connection_type'] or 'unknown'}{role_text}{drop_text}"
        )
    print()

    print("Findings")
    print("-" * 72)
    for finding in report.findings:
        ips = ", ".join(finding.affected_ips)
        suffix = f" ({ips})" if ips else ""
        print(f"[{SEVERITY_LABEL.get(finding.severity, finding.severity.upper())}] {finding.title}{suffix}")
        print(f"  {finding.detail}")
        print()

    print("Recommendations")
    print("-" * 72)
    for idx, rec in enumerate(report.recommendations, start=1):
        print(f"  {idx}. {rec}")
    print()


def write_json(report: AnalysisReport, path: Path) -> None:
    payload = {
        "generated_at": report.generated_at,
        "speaker_count": report.speaker_count,
        "firmware_versions": report.firmware_versions,
        "speakers": report.speakers,
        "findings": [asdict(f) for f in report.findings],
        "recommendations": report.recommendations,
        "source_ips": report.snapshot.source_ips,
        "fetch_errors": report.snapshot.fetch_errors,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_html(report: AnalysisReport, path: Path) -> None:
    speaker_rows = "".join(_html_speaker_row(sp) for sp in report.speakers)
    finding_blocks = "".join(_html_finding(f) for f in report.findings)
    rec_items = "".join(f"<li>{escape(rec)}</li>" for rec in report.recommendations)

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sonos household analysis</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; background: #fafafa; }}
    h1, h2 {{ margin-bottom: 0.4rem; }}
    .meta {{ color: #555; margin-bottom: 1.5rem; }}
    table {{ border-collapse: collapse; width: 100%; background: #fff; margin: 1rem 0; }}
    th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; vertical-align: top; }}
    th {{ background: #f0f0f0; }}
    .finding {{ background: #fff; border: 1px solid #ddd; border-left-width: 4px; padding: 1rem; margin: 0.75rem 0; }}
    .high {{ border-left-color: #c0392b; }}
    .medium {{ border-left-color: #d68910; }}
    .low {{ border-left-color: #2980b9; }}
    .ok {{ border-left-color: #27ae60; }}
    ul {{ line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>Sonos household analysis</h1>
  <p class="meta">{report.speaker_count} speaker(s) · Generated {escape(report.generated_at)}</p>

  <h2>Speakers</h2>
  <table>
    <thead>
      <tr>
        <th>Room</th><th>IP</th><th>Connection</th><th>Firmware</th><th>Role</th><th>RX drop %</th>
      </tr>
    </thead>
    <tbody>{speaker_rows}</tbody>
  </table>

  <h2>Findings</h2>
  {finding_blocks or '<p>No findings.</p>'}

  <h2>Recommendations</h2>
  <ul>{rec_items}</ul>
</body>
</html>
"""
    path.write_text(html_doc, encoding="utf-8")


def _html_speaker_row(sp: dict) -> str:
    role = []
    if sp["is_stereo_left"]:
        role.append("L / coordinator")
    if sp["is_stereo_right"]:
        role.append("R")
    return (
        "<tr>"
        f"<td>{escape(sp['zone_name'] or '')}</td>"
        f"<td>{escape(sp['ip_address'] or '')}</td>"
        f"<td>{escape(sp['connection_type'] or '')}</td>"
        f"<td>{escape(sp['software_version'] or '')}</td>"
        f"<td>{escape(', '.join(role))}</td>"
        f"<td>{sp['br0_rx_drop_ratio_pct'] if sp['br0_rx_drop_ratio_pct'] is not None else ''}</td>"
        "</tr>"
    )


def _html_finding(finding: Finding) -> str:
    ips = ", ".join(finding.affected_ips)
    meta = f"<p><strong>Affected:</strong> {escape(ips)}</p>" if ips else ""
    return (
        f'<div class="finding {escape(finding.severity)}">'
        f"<h3>{escape(finding.title)}</h3>"
        f"<p>{escape(finding.detail)}</p>{meta}</div>"
    )
