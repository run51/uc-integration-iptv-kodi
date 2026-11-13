"""TV guide read from Kodi: PVR IPTV Simple loads the public XMLTV guide, the Remote asks Kodi for what it shows.

Same interface as epg.Guide (programmes, now, upcoming, past, search) for the catalog, which stays without I/O:
the driver fills the caches before browsing (the programmes on air of all the channels in one request, the
programmes of one channel when its page is opened).
"""

import time

from epg import fold

NOWS_TTL_S = 60
CHANNEL_TTL_S = 300


def programme(b: dict, to_ts) -> dict | None:
    """Kodi broadcast (UTC times) -> {start, stop, title, sub, cat, icon}."""
    try:
        start, stop = to_ts(b["starttime"]), to_ts(b["endtime"])
    except (KeyError, TypeError, ValueError):
        return None
    genre = b.get("genre") or []
    return {
        "start": int(start), "stop": int(stop),
        "title": (b.get("title") or b.get("label") or "").strip(),
        "sub": (b.get("episodename") or "").strip(),
        "cat": (", ".join(genre) if isinstance(genre, list) else str(genre)).strip(),
        "icon": b.get("thumbnail") if str(b.get("thumbnail") or "").startswith("http") else "",
    }


class KodiGuide:
    def __init__(self) -> None:
        self.nows: dict[int, dict] = {}  # stream id -> programme on air
        self.progs: dict[int, list[dict]] = {}  # stream id -> programmes, for the channels whose page was opened
        self.nows_at = 0.0
        self.progs_at: dict[int, float] = {}

    def nows_stale(self) -> bool:
        return time.monotonic() - self.nows_at > NOWS_TTL_S

    def channel_stale(self, stream_id: int) -> bool:
        return time.monotonic() - self.progs_at.get(stream_id, 0.0) > CHANNEL_TTL_S

    def set_nows(self, nows: dict[int, dict]) -> None:
        self.nows, self.nows_at = nows, time.monotonic()

    def set_channel(self, stream_id: int, progs: list[dict]) -> None:
        self.progs[stream_id] = sorted(progs, key=lambda p: p["start"])
        self.progs_at[stream_id] = time.monotonic()

    def programmes(self, stream_id: int) -> list[dict]:
        if self.progs.get(stream_id):
            return self.progs[stream_id]
        return [self.nows[stream_id]] if stream_id in self.nows else []

    def now(self, stream_id: int, at: float | None = None) -> dict | None:
        at = at or time.time()
        for p in self.programmes(stream_id):
            if p["start"] <= at < p["stop"]:
                return p
        p = self.nows.get(stream_id)
        return p if p and p["start"] <= at < p["stop"] else None

    def upcoming(self, stream_id: int, count: int = 30, at: float | None = None) -> list[dict]:
        at = at or time.time()
        return [p for p in self.programmes(stream_id) if p["stop"] > at][:count]

    def past(self, stream_id: int, count: int = 60, at: float | None = None) -> list[dict]:
        """Programmes already over (replay), the most recent first."""
        at = at or time.time()
        return [p for p in reversed(self.progs.get(stream_id, [])) if p["stop"] <= at][:count]

    def search(self, query: str, at: float | None = None, horizon_s: int = 24 * 3600) -> list[tuple[int, dict]]:
        """(stream id, programme) on air (or in the pages opened) whose title contains the query, soonest first."""
        at = at or time.time()
        needle = fold(query)
        if len(needle) < 3:
            return []
        found = {}
        for sid in set(self.nows) | set(self.progs):
            for p in self.programmes(sid):
                if p["stop"] > at and p["start"] < at + horizon_s and needle in fold(p["title"] + " " + p["sub"]):
                    found[sid] = p
                    break
        return sorted(found.items(), key=lambda t: t[1]["start"])
