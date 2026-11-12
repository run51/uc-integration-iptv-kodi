"""Build the channel catalog of the integration from the provider's M3U playlist.

Usage: python tools/build_catalog.py <playlist.m3u> <catalog.json>

Keeps, for the live channels only, the provider's categories (group-title, in playlist order) and for
each channel its display name, its stream id (the last part of the stream URL) and its logo URL.
The stream URLs themselves, which contain the account login, are never written to the catalog.
"""

import json
import re
import sys

SEPARATOR = re.compile(r"[▀▄■━═]")  # "▀▄ FR ▀▄▀▄ REGIONALES ▄▀▄▀" pseudo channels used as headings
PREFIX = re.compile(r"^\s*\|[A-Z0-9-]{2,6}\|\s*")  # "|FR| TF1 FHD" -> "TF1 FHD"
LIVE_URL = re.compile(r"^https?://[^/]+/(?:live/)?[^/]+/[^/]+/(\d+)(?:\.\w+)?$")


def parse(text: str) -> dict:
    categories: dict[str, list[dict]] = {}
    info = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            info = line
            continue
        if not line or line.startswith("#") or info is None:
            continue
        match = LIVE_URL.match(line)
        if match and "/movie/" not in line and "/series/" not in line:
            name = info.rsplit(",", 1)[-1].strip()
            if name and not SEPARATOR.search(name):
                group = re.search(r'group-title="([^"]*)"', info)
                logo = re.search(r'tvg-logo="([^"]*)"', info)
                categories.setdefault((group.group(1).strip() if group else "") or "Autres", []).append(
                    {
                        "id": int(match.group(1)),
                        "name": PREFIX.sub("", name).strip() or name,
                        "logo": logo.group(1).strip() if logo and logo.group(1).startswith("http") else "",
                    }
                )
        info = None
    return {
        "categories": [
            {"id": str(n), "name": name, "channels": channels}
            for n, (name, channels) in enumerate(categories.items(), 1)
        ]
    }


def main() -> None:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as file:
        catalog = parse(file.read())
    with open(sys.argv[2], "w", encoding="utf-8") as file:
        json.dump(catalog, file, ensure_ascii=False, separators=(",", ":"))
    count = sum(len(c["channels"]) for c in catalog["categories"])
    print(f"{len(catalog['categories'])} categories, {count} channels")


if __name__ == "__main__":
    main()
