# Sonos Analyzer

Analyze Sonos speakers on your local network — firmware, connectivity, stereo-pair topology, Wi-Fi vs Ethernet issues, and practical recommendations.

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

## What it checks

- Speaker inventory (room name, IP, model, firmware)
- Firmware consistency across the household
- Stereo pair topology (left coordinator vs right satellite)
- Mixed Wi-Fi + Ethernet pairs (common cause of dropouts)
- Wi-Fi channel and PHY error hints
- Bridge RX drop ratios (informational)
- Trueplay / room calibration state hints
- Actionable recommendations (wire both, swap speakers, Wi-Fi-only fallback, DHCP reservations)

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

## License

MIT
