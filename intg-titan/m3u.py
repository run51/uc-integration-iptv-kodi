"""Read the provider's M3U playlist (Xtream codes) into the channel catalog of the integration.

Keeps, for the live channels only, the provider's categories (group-title, in playlist order) and for each
channel its display name, its stream id (last part of the stream URL), its logo URL and its guide id
(tvg-id). The stream URLs carry the account login: only their common start ("http://host:port/user/pass")
is returned apart, to be kept in a private file; it is never written to the catalog nor logged.

The playlist is read line by line (a full provider playlist with films and series is ~40 MB).
"""

import re
from collections.abc import Iterable

SEPARATOR = re.compile(r"[▀▄■━═]")  # "▀▄ FR ▀▄▀▄ REGIONALES ▄▀▄▀" pseudo channels used as headings
PREFIX = re.compile(r"^\s*\|[A-Z0-9-]{2,6}\|\s*")  # "|FR| TF1 FHD" -> "TF1 FHD"
LIVE_URL = re.compile(r"^(https?://[^/]+/(?:live/)?[^/]+/[^/]+)/(\d+)(?:\.\w+)?$")
ATTR = re.compile(r'([a-z-]+)="([^"]*)"')


def parse_lines(lines: Iterable[str]) -> tuple[dict, str]:
    """(catalog, stream base) from the playlist lines; stream base = "" when no live channel was found."""
    categories: dict[str, list[dict]] = {}
    bases: dict[str, int] = {}
    info = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF"):
            info = line
            continue
        if not line or line.startswith("#") or info is None:
            continue
        match = LIVE_URL.match(line)
        if match and "/movie/" not in line and "/series/" not in line:
            name = info.rsplit(",", 1)[-1].strip()
            if name and not SEPARATOR.search(name):
                attrs = dict(ATTR.findall(info))
                logo = attrs.get("tvg-logo", "").strip()
                categories.setdefault(attrs.get("group-title", "").strip() or "Autres", []).append({
                    "id": int(match.group(2)),
                    "name": PREFIX.sub("", name).strip() or name,
                    "logo": logo if logo.startswith("http") else "",
                    "epg": attrs.get("tvg-id", "").strip(),
                })
                base = match.group(1).replace("/live/", "/", 1)
                bases[base] = bases.get(base, 0) + 1
        info = None
    catalog = {"categories": [{"id": str(n), "name": name, "channels": channels}
                              for n, (name, channels) in enumerate(categories.items(), 1)]}
    base = max(bases, key=bases.get) if bases else ""
    return catalog, base


def parse_file(path: str) -> tuple[dict, str]:
    with open(path, encoding="utf-8", errors="replace") as file:
        return parse_lines(file)


def channel_count(catalog: dict) -> int:
    return sum(len(c["channels"]) for c in catalog.get("categories", []))
