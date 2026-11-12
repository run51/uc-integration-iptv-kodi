"""TV guide (EPG) from a public XMLTV file (xmltvfr.fr for the French channels).

Only the programmes of the channels of the catalog are kept, from a few hours ago to ~36 h ahead. A channel
is matched by its guide id (tvg-id of the M3U, e.g. "France2.fr"), else by its name without the quality
words ("TF1 4K HDR UHD (Résolution Exclus)" -> "tf1" -> the guide channel named "TF1").
"""

import gzip
import io
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

SOURCES = ["https://xmltvfr.fr/xmltv/xmltv_fr.xml.gz"]
KEEP_BEFORE_S, KEEP_AFTER_S = 6 * 3600, 36 * 3600
QUALITY = re.compile(
    r"\(.*?\)|\b(fhd\+?|uhd|hdr|hd|sd|4k|8k|low|hevc|h265|dolby digital|double audio|exclus\w*|"
    r"resolution|multi|backup|fr|vf|vost\w*)\b",
    re.I,
)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


def base_name(name: str) -> str:
    """Channel name without quality words, folded: "CANAL+ SPORT FHD+ (Resolution Exclusive)" -> "canalsport"."""
    return fold(QUALITY.sub(" ", (name or "").replace("+", " plus ")))


def parse_time(value: str) -> int:
    """XMLTV "20261112140000 +0100" -> unix time."""
    value = value.strip()
    dt = datetime.strptime(value[:14], "%Y%m%d%H%M%S")
    if len(value) > 15:
        off = value[15:].strip()
        sign = 1 if off[0] == "+" else -1
        dt -= sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def parse(raw: bytes, wanted: set[str], now: float | None = None) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """(guide channel id -> folded display names joined, guide id -> programmes) for the wanted ids.

    wanted may be empty: then all channel names are returned and no programme (first pass for name matching).
    """
    now = now or time.time()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    names: dict[str, str] = {}
    progs: dict[str, list[dict]] = {}
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "channel":
            names[el.get("id", "")] = "|".join(base_name(d.text or "") for d in el.findall("display-name"))
            el.clear()
        elif el.tag == "programme":
            cid = el.get("channel", "")
            if cid in wanted:
                try:
                    start, stop = parse_time(el.get("start", "")), parse_time(el.get("stop", ""))
                except (ValueError, IndexError):
                    el.clear()
                    continue
                if stop > now - KEEP_BEFORE_S and start < now + KEEP_AFTER_S:
                    icon = el.find("icon")
                    progs.setdefault(cid, []).append({
                        "start": start, "stop": stop,
                        "title": (el.findtext("title") or "").strip(),
                        "sub": (el.findtext("sub-title") or "").strip(),
                        "cat": (el.findtext("category") or "").strip(),
                        "desc": (el.findtext("desc") or "").strip()[:400],
                        "icon": icon.get("src", "") if icon is not None else "",
                    })
            el.clear()
    for lst in progs.values():
        lst.sort(key=lambda p: p["start"])
    return names, progs


def match_ids(channels: list[dict], names: dict[str, str]) -> dict[int, str]:
    """stream id -> guide id, from the tvg-id when the guide knows it, else from the channel name."""
    by_name: dict[str, str] = {}
    for cid, joined in names.items():
        for n in joined.split("|"):
            if n and n not in by_name:
                by_name[n] = cid
    out = {}
    for ch in channels:
        if ch.get("epg") in names:
            out[ch["id"]] = ch["epg"]
            continue
        key = base_name(ch["name"])
        if key in by_name:
            out[ch["id"]] = by_name[key]
    return out


class Guide:
    def __init__(self, progs: dict[str, list[dict]] | None = None, ids: dict[int, str] | None = None):
        self.progs = progs or {}
        self.ids = ids or {}

    def programmes(self, stream_id: int) -> list[dict]:
        return self.progs.get(self.ids.get(stream_id, ""), [])

    def now(self, stream_id: int, at: float | None = None) -> dict | None:
        at = at or time.time()
        for p in self.programmes(stream_id):
            if p["start"] <= at < p["stop"]:
                return p
        return None

    def upcoming(self, stream_id: int, count: int = 30, at: float | None = None) -> list[dict]:
        at = at or time.time()
        return [p for p in self.programmes(stream_id) if p["stop"] > at][:count]

    def search(self, query: str, at: float | None = None, horizon_s: int = 24 * 3600) -> list[tuple[int, dict]]:
        """(stream id, programme) whose title contains the query, airing now or within the horizon, soonest first."""
        at = at or time.time()
        needle = fold(query)
        if len(needle) < 3:
            return []
        found = []
        for sid, gid in self.ids.items():
            for p in self.progs.get(gid, []):
                if p["stop"] > at and p["start"] < at + horizon_s and needle in fold(p["title"] + " " + p["sub"]):
                    found.append((sid, p))
                    break
        found.sort(key=lambda t: t[1]["start"])
        return found
