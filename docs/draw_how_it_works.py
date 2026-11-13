"""Draw docs/how-it-works.svg. Run: python3 docs/draw_how_it_works.py docs/how-it-works.svg

Keep it shareable: no hostnames, IPs or other private details.
"""
import sys
from xml.sax.saxutils import escape

W, H = 1200, 800
out = []
add = out.append

C = {
    "in": ("#e8f1fb", "#3b7dd8"),
    "app": ("#e9f6ee", "#2e8b57"),
    "ext": ("#f3eefb", "#7a4fc4"),
    "store": ("#fdf3e3", "#c98a12"),
    "out": ("#fdecec", "#c0392b"),
    "guard": ("#f4f4f2", "#8a877f"),
}


def box(x, y, w, h, kind, title, lines, size=13):
    fill, stroke = C[kind]
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    cx = x + w / 2
    ty = y + 26
    add(f'<text x="{cx}" y="{ty}" text-anchor="middle" font-size="15" font-weight="bold" fill="#222">{escape(title)}</text>')
    for i, line in enumerate(lines):
        add(f'<text x="{cx}" y="{ty + 21 + i * 18}" text-anchor="middle" font-size="{size}" fill="#333">{escape(line)}</text>')


def arrow(points, label=None, at=None, both=False):
    d = "M" + " L".join(f"{x},{y}" for x, y in points)
    start = ' marker-start="url(#ah)"' if both else ""
    add(f'<path d="{d}" fill="none" stroke="#555" stroke-width="2" marker-end="url(#ah)"{start}/>')
    if label:
        lx, ly = at
        add(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-size="12" fill="#555">{escape(label)}</text>')


add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    'font-family="Segoe UI, Helvetica, Arial, sans-serif">')
add("<title>How Sonos Analyzer works</title>")
add('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
    'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#555"/></marker></defs>')
add(f'<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>')
add('<text x="40" y="52" font-size="28" font-weight="bold" fill="#222">How Sonos Analyzer works</text>')
add('<text x="40" y="80" font-size="15" fill="#555">Reads the diagnostics built into the Sonos speakers on your network, '
    'compares them with earlier scans, and explains what to fix in plain words.</text>')

# Ways in
add('<text x="40" y="122" font-size="13" font-weight="bold" fill="#3b7dd8">WAYS IN</text>')
entries = [
    ("Desktop app", ["Windows or Mac, nothing to install;", "opens a browser tab with a key", "made at each launch"]),
    ("Docker on a server", ["always on, on the speakers' network;", "scans on its own every 15 min"]),
    ("Docker Desktop", ["a laptop with Docker; page on", "this computer only; type one IP"]),
    ("Command line", ["sonos-analyzer --ips ...", "prints, or saves JSON / HTML"]),
]
ey = 135
for title, lines in entries:
    box(40, ey, 240, 100, "in", title, lines)
    add(f'<path d="M280,{ey + 50} L305,{ey + 50}" fill="none" stroke="#555" stroke-width="2"/>')
    ey += 115
add('<path d="M305,185 L305,530" fill="none" stroke="#555" stroke-width="2"/>')
arrow([(305, 230), (330, 230)])

# Pipeline
steps = [
    ("1. Find speakers", ["SSDP search on every", "network interface, a typed", "IP, or the last known", "speakers"]),
    ("2. Read diagnostics", ["up to 12 speakers at once:", "support review, groups,", "model, radio status;", "PHY errors over 5 s"]),
    ("3. Compare", ["with earlier scans: missing", "speakers, restarts, IP,", "firmware, coordinator and", "spanning-tree changes"]),
    ("4. Analyze", ["firmware lines, rooms and", "groups, Wi-Fi, noise and", "signal, wired errors,", "SonosNet bridge"]),
]
sx = 330
for title, lines in steps:
    box(sx, 160, 190, 140, "app", title, lines)
    sx += 210
for x in (520, 730, 940):
    arrow([(x, 230), (x + 20, 230)])

# Speakers
box(330, 390, 400, 110, "ext", "Sonos speakers on your network",
    ["port 1400: /support/review, zone topology,", "device description, radio status",
     "read-only, no Sonos account or cloud"])
arrow([(425, 300), (425, 390)], "search", (452, 350), both=True)
arrow([(635, 300), (635, 390)], "HTTP / SOAP", (680, 350), both=True)

# History
box(750, 390, 190, 110, "store", "Scan history", ["one small file per scan,", "kept 30 days; Docker", "volume or app-data folder"])
arrow([(845, 300), (845, 390)], None, None, both=True)

# Results
box(960, 390, 190, 190, "out", "5. Results page",
    ["findings to act on, plus", "a collapsed informational", "section; plain-language", "readings with bars and", "colors; speaker signal", "map; JSON and HTML", "downloads"])
arrow([(1055, 300), (1055, 390)], "report", (1085, 350))
add('<text x="1055" y="598" text-anchor="middle" font-size="12" fill="#555">in the browser, or from the command line</text>')

# Guards
box(330, 615, 830, 115, "guard", "Safeguards",
    ["Contacts only private IPv4 addresses on port 1400, never follows redirects, and caps response sizes.",
     "The page refuses unknown host names (DNS rebinding) and cross-site requests; there is no login, so keep servers on a trusted network.",
     "The desktop app requires the key from the link it opened. Nothing on the speakers is changed (reading the PHY counter clears it)."],
    size=13)

# Legend
lx, ly = 40, 620
add(f'<text x="{lx}" y="{ly}" font-size="13" font-weight="bold" fill="#555">KEY</text>')
for i, (kind, label) in enumerate([("in", "Ways in"), ("app", "Sonos Analyzer"), ("ext", "Outside it"),
                                   ("store", "Data it keeps"), ("out", "What you see")]):
    fill, stroke = C[kind]
    y = ly + 14 + i * 20
    add(f'<rect x="{lx}" y="{y}" width="14" height="14" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    add(f'<text x="{lx + 22}" y="{y + 12}" font-size="13" fill="#333">{label}</text>')

add('<text x="40" y="780" font-size="12" fill="#888">sonos-analyzer · docs/how-it-works.svg · 2026-11-13 · '
    'github.com/grahamfairbank/sonos-analyzer</text>')
add("</svg>")
open(sys.argv[1], "w").write("\n".join(out))
