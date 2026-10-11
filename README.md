# Sonos Analyzer

Analyze Sonos speakers on your local network — firmware, connectivity, stereo-pair topology, Wi-Fi vs Ethernet issues, and practical recommendations.

This is a fork of [soinkleined/sonos-analyzer](https://github.com/soinkleined/sonos-analyzer) by David Klein, which provides the original command-line analyzer. The fork adds a web UI, a Docker image and deeper analysis; see [Changes from the original](#changes-from-the-original).

Uses the same diagnostic endpoints Sonos exposes locally (`/support/review`, UPnP zone topology). **No cloud account required.** Your speakers must be reachable on the same network as the machine running this tool.

## Requirements

- Python 3.10+
- No third-party dependencies (stdlib only)

## Install

```bash
cd sonos-analyzer
pip install -e .
```

Or run without installing:

```bash
python -m sonos_analyzer
```

## Usage

Discover speakers automatically (SSDP):

```bash
sonos-analyzer
```

Target specific IPs (useful if discovery is blocked):

```bash
sonos-analyzer --ips 192.168.1.165,192.168.1.89,192.168.1.193
```

Write reports:

```bash
sonos-analyzer --json report.json --html report.html
```

Quiet mode (files only):

```bash
sonos-analyzer --ips 192.168.1.165 --json report.json --html report.html --quiet
```

## Web UI

Serve a browser front end instead of using the CLI:

```bash
sonos-analyzer-web --host 0.0.0.0 --port 8400
```

Then open `http://<host>:8400`. Leave the IP field empty to auto-discover, or enter speaker IPs. The page shows the same speakers, findings and recommendations as the CLI, and lets you download the JSON/HTML reports.

The UI has no authentication — run it only on a trusted LAN. It only probes private IPv4 addresses.

### Docker

```bash
docker compose up -d --build
```

The container uses **host networking** (Linux), which SSDP discovery needs because multicast does not cross Docker's bridge network. Change the port with `SONOS_ANALYZER_PORT` in `docker-compose.yml`. Discovery is sent on every network interface, so multi-homed hosts work without configuration; set `SONOS_ANALYZER_INTERFACE` (interface name or IPv4 address, also `--interface` on the CLI) to limit it to one. On Docker Desktop (macOS/Windows) host networking is limited, so enter speaker IPs manually.

## What it checks

- Speaker inventory grouped by room: stereo pairs and home theater sets (soundbar, surrounds, subs) are shown as one room with each speaker's role
- Bonded rooms: mixed wired + wireless stereo pairs, pairs split across Wi-Fi channels, satellites not on the soundbar's own link, surround link overlapping your Wi-Fi
- Firmware: per-version breakdown by model, speakers that are behind others of the same model, and bonded speakers on mismatched versions
- Radio channel map: which speakers use which band and channel, and for what (home Wi-Fi, home theater link, SonosNet, unused)
- Wi-Fi quality: PHY error rate measured over a short window (`--phy-sample`), outliers against the rest of the household, noise floor, 2.4 GHz overlapping channels, DFS channels, extenders
- Wired checks: radio left on for wired speakers, slow or half-duplex Ethernet links, interface error ratios, bridge RX drops, SonosNet / spanning-tree state
- Speaker-to-speaker signal: how strongly each speaker's radio hears every other one (the data behind Sonos's own network matrix), shown in the web UI as a link diagram and a matrix
- Trueplay tuning state per room
- Related issues are reported as one finding listing every affected speaker, with actionable recommendations

## Limitations

- Must run on the **same LAN** as your Sonos system
- Some networks block SSDP multicast — use `--ips` if discovery finds nothing
- Diagnostics are read-only; the tool does not change Sonos settings
- Findings are heuristic guidance, not official Sonos support output

## Example

```bash
sonos-analyzer --ips 192.168.1.165,192.168.1.89,192.168.1.193 \
  --json sonos-report.json \
  --html sonos-report.html
```

## Changes from the original

Based on [soinkleined/sonos-analyzer](https://github.com/soinkleined/sonos-analyzer) 0.1.0. This fork (0.2.0) adds:

- A web UI (`sonos-analyzer-web`) and a Docker image / compose file
- Rooms: stereo pairs and home theater sets grouped with roles, and checked as a unit
- Measured Wi-Fi PHY error rates, a radio channel map and a speaker-to-speaker signal matrix
- Wired checks, per-model firmware comparison, and findings grouped per issue
- Discovery on every network interface, and scans roughly five times faster on large households
- Fixes: model name and version were never read from the device description; the Trueplay state was interpreted the wrong way round

Tested against one household (Sonos S2, Wi-Fi and Ethernet, SonosNet off). SonosNet / Boost setups, S1 systems and portable speakers are untested.

## License

MIT. Copyright (c) 2026 David Klein (original project) and Graham Fairbank (this fork). See [LICENSE](LICENSE).
