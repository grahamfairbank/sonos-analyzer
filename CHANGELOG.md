# Changelog

All notable changes to this fork of [soinkleined/sonos-analyzer](https://github.com/soinkleined/sonos-analyzer). The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

Includes the fixes from an [independent Fable review](docs/reviews/2026-11-13-fable-review.md) of the release candidate (R1 to R14).

**Upgrading a Docker server:** background scans are on by default (every 15 minutes). Set `SONOS_ANALYZER_SCAN_INTERVAL=0` to turn them off. The `docker run` commands now include `-v sonos-analyzer-data:/data` to keep the scan history across updates, and the laptop (Docker Desktop) commands also turn background scans off.

### Added

- Automated tests for parsing, analysis, speaker requests and the web server's protections, using a made-up household in the speakers' real response formats; they run on every push and pull request on Linux, Windows and macOS ([#2](https://github.com/grahamfairbank/sonos-analyzer/issues/2)).

- Plain-language radio readings: noise floor, speaker-to-speaker signal and PHY errors show phone-style bars or a dot with a plain word and color, with the number underneath. Noise floor and signal use the bands and colors of the network matrix built into Sonos speakers; a collapsible "What these readings mean" section explains each reading ([#15](https://github.com/grahamfairbank/sonos-analyzer/issues/15)).
- Scan history and monitoring: the server scans every 15 minutes (`SONOS_ANALYZER_SCAN_INTERVAL`) and keeps 30 days of history in `/data`; each scan is compared with earlier ones to report speakers missing since the last scan, unexpected restarts (the speaker's boot counter, the signal Home Assistant uses), firmware updates, IP address changes and group coordinator changes. The desktop app keeps history in the user's app-data folder and scans only when asked. **Clear history** on the page deletes it ([#5](https://github.com/grahamfairbank/sonos-analyzer/issues/5)).
- Network errors column: errors on each speaker's connection since the previous scan, or since its last restart when there's no earlier scan, with the time span shown ([#5](https://github.com/grahamfairbank/sonos-analyzer/issues/5)).
- Informational section: findings that need no action are collected in a collapsed section and never counted in the severity tiles; the DFS channel note is now informational ([#17](https://github.com/grahamfairbank/sonos-analyzer/issues/17)).
- Group coordinator check: a group led by a speaker on a noisy or older radio while a wired or better-placed member could lead it, with how to regroup ([#18](https://github.com/grahamfairbank/sonos-analyzer/issues/18)).
- Older Wi-Fi radios: informational note listing models Sonos documents as 802.11a/b/g without WPA3, so a router change keeps WPA2 available for them ([#19](https://github.com/grahamfairbank/sonos-analyzer/issues/19)).
- SonosNet bridge health: warns when spanning tree blocks a wired speaker's Ethernet port, with Sonos's managed-switch requirements (STP and BPDU flooding); names the root bridge (a speaker, or a device outside Sonos such as a switch) and tracks root changes between scans, which make the network re-form ([#20](https://github.com/grahamfairbank/sonos-analyzer/issues/20)).
- Noise floor coloring also uses an older radio's interference (ANI) level, as Sonos's own network matrix does; failed radio resets are reported when non-zero ([#21](https://github.com/grahamfairbank/sonos-analyzer/issues/21)).
- Auto / Light / Dark theme switch; Auto follows the system setting ([#16](https://github.com/grahamfairbank/sonos-analyzer/issues/16)).

### Changed

- Firmware analysis recognizes the household's main line and the separate legacy-product line Sonos has kept some early S2 models on since August 2025 (citing Sonos's Legacy Product Update article). Speakers on either are reported as current; anything else is flagged as having missed an update, with the version it should be on ([#13](https://github.com/grahamfairbank/sonos-analyzer/issues/13)).
- The "turn off Wi-Fi" advice for wired speakers is no longer given for models that can't turn Wi-Fi off: Era 100, Era 300, Move, Move 2, the Roam family and Sub Mini ([#11](https://github.com/grahamfairbank/sonos-analyzer/issues/11)).
- PHY error rates show how they were measured: the counter is cleared on every speaker at the same moment and read again after the sample window. The page and HTML report give the window and UTC time, each speaker's exact count and window on hover, the reason when a speaker couldn't be measured, and the unit (per second) on every value. In JSON, the support review's raw counter, which covers an unknown period, is now `phy_errors_unknown_window` ([#14](https://github.com/grahamfairbank/sonos-analyzer/issues/14)).
- Trueplay findings leave out portables (Move, Roam), which tune themselves, and Port, Boost and Amp; the speakers table shows "Not applicable" for them ([#12](https://github.com/grahamfairbank/sonos-analyzer/issues/12)).

### Security

- Review fixes: a damaged history file can no longer make scans fail (R1); laptops don't scan in the background, and servers stop reusing remembered speakers after three failed rounds (R2); history files are private to the account (R10); responses forbid framing and send no referrer (R10); topology text is cleaned of control characters (R11); desktop builds use Python 3.14.10 and hash-pinned build tools (R7); manual desktop builds must run from the release tag so build provenance names the source built (R8).
- Desktop app: a random key is made at each launch and passed in the link the app opens; requests without it are refused, so other accounts on a shared computer can't read results, scan or quit the app (review F16, [#3](https://github.com/grahamfairbank/sonos-analyzer/issues/3)).
- Verifiable builds: the Docker base image is pinned by digest, and the image and every desktop download get signed build provenance attestations (`gh attestation verify`) (review F8, [#4](https://github.com/grahamfairbank/sonos-analyzer/issues/4)).
- SECURITY.md, and private vulnerability reporting turned on for the repository ([#6](https://github.com/grahamfairbank/sonos-analyzer/issues/6)).
- Only RFC 1918 private ranges and 100.64.0.0/10 are contacted. Python's private-address test also accepted documentation and reserved ranges.

### Fixed

- Firmware: a second version line is called Sonos's legacy-product line only when it shares the main release; otherwise it's "older line, not confirmed current" (review R3).
- Scan history follows each speaker across scans it missed, keeps missing speakers listed with when they were last seen, and treats sleeping portables as informational (R4).
- The interface errors finding covers Ethernet on wired speakers only, and needs at least 10,000 packets to rate (R5).
- Odd numbers reported by a device can no longer fail a scan (R6).
- Scans were refused with "Cross-origin requests are not allowed" when the web UI is served over HTTPS by a reverse proxy (TLS offload). An `https://` origin for the same host name is now accepted as well as `http://`. Thanks to @wimmme ([#1](https://github.com/grahamfairbank/sonos-analyzer/pull/1)).

### Documentation

- Redrawn "How it works" diagram for the current design, shown at the top of the README ([#7](https://github.com/grahamfairbank/sonos-analyzer/issues/7)).
- README: how to use the analyzer behind a reverse proxy.

## [0.4.0-beta.2] - 2026-11-12

Fixes from an [independent Fable review](docs/reviews/2026-11-12-fable-review.md) of 0.4.0-beta.1.

### Security

- The web server refuses requests addressed to unknown host names, which blocks DNS-rebinding attacks from websites; new `SONOS_ANALYZER_ALLOWED_HOSTS` setting for names you use (F1). Cross-origin POSTs are refused.
- Speaker requests never follow redirects, responses are capped at 8 MiB, and only private IPv4 addresses are contacted, including addresses learned from discovery or from the speakers themselves (F2, F3).
- Speaker-supplied text has control characters removed before it is shown or logged (F14).
- Desktop build jobs no longer have write access to releases; PyInstaller and every GitHub Action are pinned; releases include `SHA256SUMS.txt`; Dependabot keeps the pins current (F5, F8).

### Fixed

- Room names containing `&`, `"` or `<` broke all room, stereo-pair and home theater analysis (F4).
- Invalid or negative `Content-Length` could hang a request; idle connections are now dropped after 30 seconds (F6).
- On Windows the desktop app could start on a port another program was already using (F7).
- A wired speaker with Wi-Fi turned off could be reported as having its radio on (F10).
- Discovery stops after 64 speakers, so a flood of fake replies cannot stall a scan (F9).
- Firmware comparison groups by model number, and no longer guesses when a speaker's model can't be read (F13).
- `--interface` on Windows gave an unhelpful error; the docs now say discovery on every interface is Linux-only (F12).
- Clearer off-network hint, scan-time wording, macOS bundle version format; 100.64.0.0/10 addresses are allowed (F11, F15).

### Changed

- The Mac download is now one universal app for Apple silicon and Intel (`…-macos-universal.zip`), replacing the separate Apple-silicon and Intel builds. GitHub's Apple-silicon build machines repeatedly failed to start the build. The beta.2 downloads were rebuilt after release for this change.

## [0.4.0-beta.1] - 2026-11-12

Pilot pre-release of the desktop app.

### Added

- Desktop app for Windows and macOS (Apple silicon and Intel), built by GitHub Actions and attached to each release. Opens the analyzer in the browser, with discovery working; unsigned for the pilot. The guide also shows how to run the identical app from inspectable source with Python. See [Desktop app for Windows and Mac](docs/desktop-app.md).
- `sonos-analyzer-app` / `python -m sonos_analyzer.app`: serves the UI on this computer, opens the browser, and stops on **Quit** or after 15 minutes with no page open.

### Changed

- Pre-release tags no longer update the `latest` Docker image.

## [0.3.0] - 2026-11-12

### Added

- Ready-built Docker image for Intel/AMD and ARM at `ghcr.io/grahamfairbank/sonos-analyzer`, built by GitHub Actions from each version tag.
- [Run on a laptop with Docker Desktop](docs/docker-desktop.md): a guide for Windows and Mac laptops, with an easy route (ready-built image, one command) and an inspect-the-code route (build from source).
- `docker-compose.desktop.yml`: bridge networking with the UI on `127.0.0.1:8400`, for Docker Desktop.
- README "Get started" table pointing laptop, server and Python users to the right instructions.
- This changelog.

### Changed

- Speaker discovery is sent on every network interface by default; `--interface` / `SONOS_ANALYZER_INTERFACE` limits it to one. Docker's own bridge interfaces are skipped.
- Unreachable speakers are ruled out together before fetching, so a failed scan takes seconds instead of about 8 seconds per speaker.
- Clearer messages when discovery finds nothing, or when the host has no address on the speakers' network.

### Fixed

- Replies from non-Sonos devices to the discovery search were treated as speakers.

## [0.2.0] - 2026-10-11

### Added

- Web UI (`sonos-analyzer-web`), Dockerfile and `docker-compose.yml` using host networking.
- Rooms: stereo pairs and home theater sets grouped with each speaker's role, and checked as a unit.
- Wi-Fi PHY error rate measured over a sample window, with household outliers flagged; noise floor per speaker.
- Radio channel map, speaker-to-speaker signal matrix and diagram, wired-link checks, and per-model firmware comparison.
- Findings grouped per issue, listing every affected speaker; an `info` severity.

### Changed

- The household review is fetched once instead of once per speaker, making scans about five times faster on large systems.

### Fixed

- Model name and version were never read from the device description.
- The Trueplay calibration state was interpreted the wrong way round.
- Home theater satellites were flagged for bridge packet drops they make by design.

## [0.1.0] - 2026-08-22

Original command-line analyzer by David Klein ([soinkleined/sonos-analyzer](https://github.com/soinkleined/sonos-analyzer)).

[0.4.0-beta.2]: https://github.com/grahamfairbank/sonos-analyzer/compare/v0.4.0-beta.1...v0.4.0-beta.2
[0.4.0-beta.1]: https://github.com/grahamfairbank/sonos-analyzer/compare/v0.3.0...v0.4.0-beta.1
[0.3.0]: https://github.com/grahamfairbank/sonos-analyzer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/grahamfairbank/sonos-analyzer/compare/56892df...4530d00
[0.1.0]: https://github.com/soinkleined/sonos-analyzer/commit/56892df
