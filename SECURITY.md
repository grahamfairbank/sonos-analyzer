# Security policy

## Reporting a problem

Please report security problems privately, not in a public issue:

1. Go to the [Security tab](https://github.com/grahamfairbank/sonos-analyzer/security) of this repository.
2. Select **Report a vulnerability** and describe the problem, how to reproduce it, and which version you used.

You'll get a reply within 7 days. Once the problem is confirmed, a fix is released as soon as it's ready and the report is credited in the changelog unless you'd rather stay anonymous.

## Supported versions

Fixes go into the latest release. Pre-releases (betas) are replaced by the next release and aren't patched separately.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases and betas | No: please update |

## What's in scope

- The web UI and its API (`sonos-analyzer-web`, the Docker image and the desktop app).
- The command-line tool.
- The release builds: the Docker image on GitHub Container Registry and the desktop downloads on the releases page.

Problems in Sonos speakers or the Sonos app should go to Sonos. Problems in the [original project](https://github.com/soinkleined/sonos-analyzer) that also affect this fork are welcome here.

## How the analyzer limits its own reach

- It only reads from speakers, and only contacts private IPv4 addresses (10.x, 172.16-31.x, 192.168.x and 100.64.x) on port 1400. It never follows redirects and caps the size of each response.
- The web UI has no login. It refuses host names it doesn't know (to block DNS rebinding) and cross-site requests. On a server, keep it on a trusted network; the desktop app and the Docker Desktop setup listen only on the computer itself.
- Independent reviews and what was done about each finding are in [docs/reviews](docs/reviews).
