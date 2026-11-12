"""TV guide (EPG) from a public XMLTV file (xmltvfr.fr for the French channels).

Only the programmes of the channels of the catalog are kept, from yesterday morning (replay) to ~36 h ahead. A channel
is matched by its guide id (tvg-id of the M3U, e.g. "France2.fr"), else by its name without the quality
words ("TF1 4K HDR UHD (Résolution Exclus)" -> "tf1" -> the guide channel named "TF1").
"""

import gzip
import io
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

SOURCES = ["https://xmltvfr.fr/xmltv/xmltv_fr.xml.gz"]
KEEP_BEFORE_S, KEEP_AFTER_S = 40 * 3600, 36 * 3600
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


def open_source(source):
    """A readable binary stream of the XMLTV data: file path (gzipped or not) or bytes."""
    if isinstance(source, (bytes, bytearray)):
        data = io.BytesIO(source)
    else:
        data = open(source, "rb")  # noqa: SIM115 - closed by the caller
    head = data.read(2)
    data.seek(0)
    return gzip.GzipFile(fileobj=data) if head == b"\x1f\x8b" else data


def elements(source):
    """(tag, element) of each <channel> and <programme>, read as a stream (the French guide is ~100 MB of XML:
    the parsed elements are dropped from the tree as soon as they are handed over)."""
    stream = open_source(source)
    try:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        for event, el in context:
            if event == "end" and el.tag in ("channel", "programme"):
                yield el.tag, el
                root.clear()
    finally:
        stream.close()


def parse(source, wanted: set[str], now: float | None = None, reduced: str | None = None
          ) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """(guide channel id -> folded display names joined, guide id -> programmes) for the wanted ids.

    source: path of the XMLTV file (gzipped or not) or its bytes. wanted may be empty: then all channel names
    are returned and no programme (first pass for name matching). reduced: path of a gzipped XMLTV file to
    write with only the wanted channels and their programmes (the guide served to Kodi).
    """
    now = now or time.time()
    names: dict[str, str] = {}
    progs: dict[str, list[dict]] = {}
    out = gzip.open(reduced + ".tmp", "wb") if reduced else None
    if out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="uc-intg-iptv-kodi">\n')
    try:
        for tag, el in elements(source):
            if tag == "channel":
                cid = el.get("id", "")
                names[cid] = "|".join(base_name(d.text or "") for d in el.findall("display-name"))
                if out and cid in wanted:
                    out.write(ET.tostring(el, encoding="utf-8"))
                continue
            cid = el.get("channel", "")
            if cid not in wanted:
                continue
            if out:
                out.write(ET.tostring(el, encoding="utf-8"))
            try:
                start, stop = parse_time(el.get("start", "")), parse_time(el.get("stop", ""))
            except (ValueError, IndexError):
                continue
            if stop > now - KEEP_BEFORE_S and start < now + KEEP_AFTER_S:
                icon = el.find("icon")
                progs.setdefault(cid, []).append({
                    "start": start, "stop": stop,
                    "title": (el.findtext("title") or "").strip(),
                    "sub": (el.findtext("sub-title") or "").strip(),
                    "cat": (el.findtext("category") or "").strip(),
                    "icon": icon.get("src", "") if icon is not None else "",
                })
        if out:
            out.write(b"</tv>\n")
            out.close()
            os.replace(reduced + ".tmp", reduced)
            out = None
    finally:
        if out:
            out.close()
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

    def past(self, stream_id: int, count: int = 60, at: float | None = None) -> list[dict]:
        """Programmes already over (replay), the most recent first."""
        at = at or time.time()
        return [p for p in reversed(self.programmes(stream_id)) if p["stop"] <= at][:count]

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
