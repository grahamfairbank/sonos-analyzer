# Independent review: Fable, 2026-11-13

| | |
|---|---|
| **Reviewer** | Claude Fable, run in Claude Code as an independent, read-only reviewer |
| **Scope** | Whole repository, with attention to the changes since v0.4.0-beta.2: scan history and background scans, the desktop app key, the new findings, Docker and CI changes, and whether the docs match the code |
| **Reviewed version** | 0.4.0 release candidate, commit `bd9c3cf` |
| **Fixes released in** | 0.4.0 |

The reviewer read every tracked file, ran the test suite, ran the web server and the desktop app on 127.0.0.1 (high ports, background scans off, a temporary history folder), and exercised history, analysis and reports with the test household and mocks. It did not contact the speakers or anything else on the lab network, change any file in the repository, or touch GitHub. It disclosed read-only internet lookups to verify pins (the GitHub action tags, the python.org installer, the Docker Hub manifest and PyPI metadata). The original report is reproduced in full below, after a summary of what was done about each finding.

The previous review is [2026-11-12](2026-11-12-fable-review.md).

## Resolution

| ID | Severity | Finding | Status in 0.4.0 |
|---|---|---|---|
| R1 | Medium | One unexpected history file made every scan fail | **Fixed.** Records carry a format version; files that aren't a usable record (wrong shape, no time zone, future-dated) are skipped; the comparison tolerates missing fields; and any history problem is reported as a note instead of failing the scan. **Clear history** is shown whenever history is on, even before a scan. |
| R2 | Medium | Docker Desktop kept probing remembered addresses on other networks | **Fixed.** Laptop setups (`docker-compose.desktop.yml` and the Docker Desktop `docker run` commands) turn background scans off, and the Docker Desktop guide explains history. On servers, background scans stop reusing remembered speakers after three failed rounds in a row, until someone scans. |
| R3 | Low | A lone speaker on an older major version was called "up to date" | **Fixed.** A second firmware line counts as Sonos's legacy-product line only when it shares the main line's release (build number or display version). Otherwise its speakers are shown as "older line, not confirmed current", with advice to check System Updates. |
| R4 | Low | Restarts and IP changes were missed across a gap; "missing" lasted one scan | **Fixed.** Each speaker is compared with the last scan that contained it; missing speakers stay listed with "not seen since" for the 7-day window. Portables (Move, Roam) that drop out are informational. |
| R5 | Low | Wi-Fi errors blamed a cable; tiny samples rated "Very high" | **Fixed.** The interface errors finding covers Ethernet on wired speakers only, and counts under 10,000 packets show "Too few packets" instead of a rating. The explainer covers wireless links. |
| R6 | Low | Odd numbers from a device could fail a scan | **Fixed.** Boot counters and frequencies go through strict number parsing; anything else is ignored. |
| R7 | Low | Desktop builds bundled Python 3.12.10; PyInstaller's dependencies unpinned | **Fixed.** Desktop builds use Python 3.14.10 (universal2 installer pinned by SHA-256 on the Mac), and PyInstaller with all its dependencies installs from a hash-pinned requirements file that Dependabot maintains. |
| R8 | Low | A manual run could attest a different commit than the one built | **Fixed.** The tag input is gone: manual runs must start from the release tag, and the workflow refuses to run otherwise. |
| R9 | Low | "Remove completely" left the history volume behind | **Fixed.** The removal commands delete the volume, and the README notes that a mounted folder must be writable by `nobody`. The changed `docker run` lines are in the changelog. |
| R10 | Low | Small gaps around the desktop key | **Fixed:** (a) only requests carrying the key keep the app running; (c) the history folder and files are private to the account (`0700`/`0600`); (d) responses send `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`. **Documented:** (b) on Linux run-from-source, other accounts can see the link with the key; the guide says to use Docker on a shared Linux machine. |
| R11 | Info | Topology attributes skipped control-character cleanup | **Fixed.** Every attribute read from the zone topology is cleaned. |
| R12 | Info | History edge cases | **Fixed:** (a) the Clear history confirmation says a running scan still saves its result; (b) after waiting for another scan the page says whose results it shows; (c) future-dated records are ignored. **Accepted:** (d) reading the history twice per scan is fast enough at 30 days of 15-minute scans. |
| R13 | Info | Monitoring turns on by default for Docker servers | **Addressed.** Called out in the changelog and release notes with the `SONOS_ANALYZER_SCAN_INTERVAL=0` switch; the README says the SonosNet checks are tested only with made-up data. |
| R14 | Info | Release hygiene | **Fixed or done at release:** version, classifier, download links and changelog heading are set when 0.4.0 is tagged; the README feature list, SECURITY.md address ranges, the previous review's F16 wording and the test file layout are corrected. |

## Original report

### Overall assessment

The new code is in good shape from a security standpoint: I found no High finding and no regression of the earlier fixes. The Host and Origin checks, the JSON-only POST rule and the per-launch key all behaved correctly under probing. History file names are built only from the server's own timestamp, so there is no path traversal. The HTML report escapes every new cell, and the page still builds all DOM through `textContent` with no `innerHTML` and no external resources. All ten action SHA pins, the base-image digest and the python.org installer SHA-256 match upstream.

The weaknesses are in robustness and in what the new monitoring does unattended. I recommend fixing three things before tagging 0.4.0:

- **R1:** one unexpected file in the history folder makes every scan fail with a 500, and the page offers no way out when there is no report to show.
- **R2:** on a laptop with Docker Desktop, background scans keep probing the last-known speaker addresses every 15 minutes on whatever network the laptop is on. The Docker Desktop guide does not mention background scans at all.
- **R3:** any lone speaker on an older major version is reported as "up to date on Sonos's legacy-product line" with no evidence. This reopens F13 in a new form.

R4 to R7 are cheap correctness fixes worth taking in the same pass. R14 is the release checklist (the version is still `0.4.0b2`, and the download links point to beta.2). The rest can follow in 0.4.1.

**Disclosed side effects:** none on the lab network. No discovery multicast was sent and nothing on the speakers' network was contacted; every scan path ran with `collect_snapshot` and `discover_speakers` mocked, and requests to the real local servers were limited to ones refused before a scan starts. Unlike last time I did use outbound internet, read-only and with no project data, to verify pins:

- `git ls-remote` against github.com for the ten action repositories
- one download of `python-3.12.10-macos11.pkg` from python.org, hashed and deleted
- a manifest lookup on Docker Hub
- the PyInstaller 6.22.3 metadata on pypi.org

No file in the repository was changed; all work was in a scratch copy.

### Findings

| ID | Severity | Area | Location (at `bd9c3cf`) | Problem | Verified | Recommended fix |
|---|---|---|---|---|---|---|
| R1 | Medium | Robustness (history) | `sonos_analyzer/history.py` `load`, `prune`, `compare`, `_wired_since`; `web.py` `run_scan` | `load` and `prune` only tolerate `OSError`, `ValueError` and `KeyError`, and `run_scan` only catches `OSError`. A `scan-*.json` that is valid JSON but not the expected shape raises `TypeError` or `KeyError`, so every scan, user or background, returns 500 "Scan failed". This continues until the file ages out (up to 30 days; a bad file inside the 7-day window also breaks `compare`). `HISTORY.clear()` still works, but the **Clear history** button is only drawn after a report renders, so after a restart there is nothing to click. The realistic trigger is a later version changing the record format, or a hand-edited or partly restored volume. | CONFIRMED: with one such file in the folder, `run_scan` raised for a JSON list, `null`, a record without `speakers`, a timestamp without a time zone, a numeric `scanned_at`, and a speaker entry without `eth0`. `clear()` removed the file each time. | Wrap the whole history step in `run_scan` in `except Exception`. Validate each record in `load` and skip the rest. Use `.get` with defaults in `compare` and `_wired_since`. Add a `"v": 1` field to records. Show **Clear history** whenever history is on. |
| R2 | Medium | Privacy / reach (background scans) | `web.py` `scan_in_background`, `_last_known_ips`; `docker-compose.desktop.yml`; `docs/docker-desktop.md`; README quick-try `docker run` | The image defaults to a 15-minute background scan. Under Docker Desktop discovery finds nothing, so each round falls back to the addresses in the newest history record. A failed round saves no record, so the same addresses are retried every 15 minutes until that record is 7 days old. On a laptop taken to an office, customer or public network, the container keeps opening TCP 1400 to up to 64 remembered private addresses and requests `/support/review` from whatever answers. The requests are read-only and private-range only, but they go to other people's hosts without the user doing anything. `docs/docker-desktop.md` never mentions background scans or history. | CONFIRMED with mocks (no network): with a 6-day-old record and discovery returning nothing, `scan_in_background` called `collect_snapshot` with the 11 remembered addresses on each of three consecutive rounds. | Set `SONOS_ANALYZER_SCAN_INTERVAL=0` for laptop setups and describe monitoring as a server feature, or stop reusing remembered addresses after two or three failed rounds until a person scans again. Add a "Background scans and history" paragraph to the Docker Desktop guide. |
| R3 | Low | Correctness (firmware) | `sonos_analyzer/analyze.py` `_firmware_lines`, `_firmware_summary` and finding | Whatever version is newest on any other major line becomes the "legacy-product line", even when the builds differ and when the model is a current one. A lone speaker that missed a major update is told it is up to date, with a Sonos support article cited. | CONFIRMED: household on `86.2-70000` with the single Era 100 on `85.0-64110` gave status `maintenance` and "1 older speaker(s) are up to date on Sonos's legacy-product line". | Say "up to date" only when the release matches; otherwise report "on an older firmware line; could not confirm it is current" or "behind". |
| R4 | Low | Correctness (history compare) | `history.py` `compare` | (a) Restarts and IP changes are only compared between consecutive scans that both contain the speaker; a speaker absent from one scan that returns with a higher BootSeq or a new address is counted as neither. (b) "Missing" is computed against the previous scan only, so the finding disappears after one interval while the speaker is still gone. Portables that sleep on battery raise a Medium "missing" finding each time. | CONFIRMED with three-scan sequences. | Compare each speaker with the last record in the window that contains it. Report "not seen since <time>". Word portables differently. |
| R5 | Low | Correctness (network errors) | `analyze.py` `_interface_errors`, `_wired_findings`; page explainer | The check runs on `br0` for every speaker that is not wired, but the finding recommends checking the Ethernet cable. There is no minimum sample size: one error in a few dozen packets is rated "Very high" and raises a Medium finding. | CONFIRMED. | Raise the finding only for `eth0` on wired speakers; require a minimum packet count (for example 10,000). |
| R6 | Low | Robustness (speaker-supplied values) | `history.py` `_int`; `analyze.py` `_band`/`_channel`, `_wireless_map`; `parse.py` `_parse_zone_member` | `str.isdigit()` accepts characters such as `²` that `int()` rejects, and `int(freq)` in `_wireless_map` is not guarded. One odd `BootSeq` or `ChannelFreq` from any device that answers as a speaker makes the whole scan fail. | CONFIRMED. | Parse numbers with one strict helper and normalize `ChannelFreq` in `parse.py`. |
| R7 | Low | Supply chain (desktop builds) | `.github/workflows/desktop-build.yml` | Both desktop builds bundle Python 3.12.10 (April 2025), the last 3.12 release with binary installers, so the apps ship an interpreter and Expat that get no further fixes. PyInstaller is pinned by version, but its dependencies are not, and neither is covered by Dependabot. | CONFIRMED for the pins; PLAUSIBLE that no newer 3.12 binary exists. | Move desktop builds to a Python series that still gets binary installers; install PyInstaller from a hash-pinned requirements file maintained by Dependabot. |
| R8 | Low | Supply chain (attestation accuracy) | `desktop-build.yml`; `docs/desktop-app.md` | With `workflow_dispatch`, the workflow runs from the branch it was started on but builds `inputs.tag`, so the attestation can name a commit that is not the source built. | PLAUSIBLE. | Drop the `tag` input and require manual runs to start from the tag ref, or check `git rev-parse HEAD` equals `$GITHUB_SHA`. |
| R9 | Low | Docs accuracy (data left behind) | README and Docker Desktop guide; `Dockerfile` `VOLUME /data` | "Remove completely" now leaves the `sonos-analyzer-data` volume with 30 days of speaker names and addresses. Users who keep an older `docker run` line without `-v` get a new anonymous volume on every re-create. A bind mount owned by another user makes saving fail quietly. | CONFIRMED by reading. | Add `-v` / `docker volume rm` to removal steps, note the changed `docker run` line, and say a mounted folder must be writable by `nobody` (65534). |
| R10 | Low | Security (desktop key, residual gaps) | `web.py` `PUBLIC_PATHS`, `do_GET`, `_send`; `app.py`; `history.py` `save` | (a) Keyless `/healthz`, `/api/app` and `/` still refresh `last_activity`, so another local account can keep the app running. (b) On Linux run-from-source the link with the key is visible in `/proc/<pid>/cmdline` of the browser launcher. (c) History files use the default umask, readable by other accounts on a shared Linux machine. (d) No `X-Frame-Options` or `Referrer-Policy`. | CONFIRMED for all four. | (a) Refresh only for keyed requests; (b) document; (c) `0700`/`0600`; (d) add the headers. |
| R11 | Info | Security (residual of F14) | `parse.py` `_parse_zone_member` | Zone-topology attributes skip `clean_text`; C1 controls and newlines pass through to the terminal report. | CONFIRMED. | Apply `clean_text` to every attribute. |
| R12 | Info | Behaviour (history edge cases) | `web.py`, page, `history.py` | (a) Clear history during a scan leaves that scan's record. (b) After a 409 the page may show the background scan's results, not the user's. (c) A future-dated record is never pruned and stays "the previous scan". (d) Each scan reads every history file twice (0.8 s with 2,880 files). | CONFIRMED (a, c, d); (b) by reading. | (a) Say so in the confirmation; (b) tell the user; (c) ignore future records; (d) fine for now. |
| R13 | Info | Operational (monitoring default) | `web.py` `main`; README | Upgrading a Docker install turns on a full scan every 15 minutes with no opt-in, including clearing every speaker's PHY counter. Disclosed, but long-term effects on real speakers are untested, and the new SonosNet findings are untested on hardware. | PLAUSIBLE. | Call out the default in the release notes with the `=0` switch; mark SonosNet findings as untested on real hardware. |
| R14 | Info | Release hygiene | `__init__.py`, `pyproject.toml`, desktop guide, CHANGELOG, README, SECURITY.md, `tests/test_history.py`, previous review | (a) Version still `0.4.0b2`, Beta classifier. (b) Desktop guide links beta.2 files. (c) CHANGELOG "Unreleased". (d) README feature list omits the new checks. (e) SECURITY.md writes "100.64.x" for 100.64.0.0/10. (f) The previous review's F16 row says only the page is open; `/healthz` and `/api/app` are too. (g) `unittest.main()` guard in the middle of `tests/test_history.py`. | CONFIRMED by inspection. | Fix as listed. |

### Verified fixed from the previous review

- **F1 (Host / Origin): holds.** Foreign `Host` values (including `127.0.0.1.evil.com`, `2130706433`, `0x7f.1`) return 403; a foreign `Origin` on `/api/analyze`, `/api/quit` and `/api/history/clear` returns 403; a non-JSON content type returns 415. `.local` names are accepted by design; in the desktop app that path is now covered by the key.
- **F2 (redirects, size cap): holds.**
- **F3 (targets): holds and is tighter.** The explicit allowlist refuses documentation, benchmarking, reserved, link-local and loopback ranges, IPv4-mapped IPv6, leading zeros and CIDR strings, and is applied again before every request, including addresses reused from history.
- **F4, F6, F9, F10, F11, F12: hold.**
- **F5, F8: hold.** Workflow permissions are read-only except the attach job; all action SHAs, the base-image digest and the python.org hash match upstream. Remaining gaps were R7 and R8.
- **F7 (Windows port reuse): code holds;** not run on Windows by the reviewer.
- **F13 (firmware heuristic): partly reopened;** see R3.
- **F14 (control characters): holds** for review and device-description text; residual in topology attributes, R11.
- **F15: holds.**
- **F16 (per-launch key): fixed and verified.** Without the key, `/api/report`, `/report.html`, `/api/analyze`, `/api/history/clear` and `/api/quit` return 401; a wrong key or a key in the query string also returns 401. The comparison is constant-time, the key is 192 bits from `secrets`, the page removes it from the address bar and sends it only as a same-origin header. Residual gaps were R10.

### Not covered

- Real speakers and real SonosNet: the ANI/weak-signal scoring, STP parsing, BootSeq behavior, the model lists and the statements attributed to Sonos support articles were not checked against hardware or the articles.
- Windows and macOS at runtime: browser launch, the key hand-off, history folder permissions, the F7 bind behavior.
- A browser run of the page (reviewed by reading only).
- An actual run of the workflows or `gh attestation verify`.
