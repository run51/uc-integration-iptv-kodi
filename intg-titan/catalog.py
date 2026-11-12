"""The channel catalog as media browser items for the remote. No I/O.

catalog.json (built by tools/build_catalog.py from the provider's M3U playlist):
  {"categories": [{"id": "3", "name": "FR TV FULL HD|4K (France)", "channels": [{"id": 1149, "name": "TF1 FHD",
  "logo": "https://..."}]}]}

Media ids:
  root               the categories, in the provider's order (as in Titan Player)
  cat:<cat>          a category: first the "Programme TV" row, then its channels (tap = play at once)
  guide:<cat>        the same channels, tap = the channel's programme page (the remote has no long press)
  ch:<cat>:<stream>  a channel: played in Kodi; browsed = its programme page
  past:<cat>:<stream>          the programmes already aired (replay), the most recent first
  rp:<cat>:<stream>:<start>    one of them: played in Kodi from the provider's catch-up
"""

import re
import time
import unicodedata

from epg import base_name

from ucapi.media_player import BrowseMediaItem, MediaClass, MediaContentType

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Paris")
except Exception:  # noqa: BLE001 - no tz database: fall back to the system local time
    LOCAL_TZ = None

ROOT = "root"
CATEGORY_ICON = "icon://uc:list"
GUIDE_ICON = "icon://uc:calendar-days"
CHANNEL_ICON = "icon://uc:tv"
REPLAY_ICON = "icon://uc:arrow-rotate-left"


def hhmm(ts: float) -> str:
    from datetime import datetime

    if LOCAL_TZ:
        return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%H:%M")
    return time.strftime("%H:%M", time.localtime(ts))


def day_label(ts: float, now: float | None = None) -> str:
    """"" today, "demain " tomorrow, "hier " yesterday (programme lists span yesterday to ~36 h ahead)."""
    from datetime import datetime

    now = now or time.time()
    d = datetime.fromtimestamp(ts, LOCAL_TZ).date()
    today = datetime.fromtimestamp(now, LOCAL_TZ).date()
    if d == today:
        return ""
    return "demain " if d > today else ("hier " if (today - d).days == 1 else d.strftime("%d/%m "))


def fold(text: str) -> str:
    """Lower case without accents nor spaces, for search: "France 2" ~ "france2"."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


UHD = re.compile(r"\b(4k|uhd)\b", re.I)
MAX_TWINS = 2


def is_4k(name: str) -> bool:
    return bool(UHD.search(name or ""))


def twin_rank(name: str) -> int:
    """Which other version of a channel to try first for its replay: FHD, then HD, then the rest."""
    up = (name or "").upper()
    return 0 if "FHD" in up else 1 if re.search(r"\bHD\b", up) else 2


def apply_filter(data: dict, hidden: dict | None) -> dict:
    """Catalog without the categories and channels hidden on the settings page.

    hidden = {"categories": ["7", ...], "channels": {"3": [1151, ...]}, "prefer_4k": true}
    prefer_4k (default on): in a category, a channel shown in 4K hides its other versions (same name
    without the quality words: "TF1 4K HDR UHD" hides "TF1 FHD").
    """
    hidden = hidden or {}
    cats = set(hidden.get("categories") or [])
    chans = {k: set(v) for k, v in (hidden.get("channels") or {}).items()}
    prefer_4k = hidden.get("prefer_4k", True)
    out = []
    for cat in data.get("categories", []):
        if cat["id"] in cats:
            continue
        drop = chans.get(cat["id"], set())
        kept = [ch for ch in cat["channels"] if ch["id"] not in drop]
        if prefer_4k:
            uhd = {base_name(ch["name"]) for ch in kept if is_4k(ch["name"])}
            kept = [ch for ch in kept if is_4k(ch["name"]) or base_name(ch["name"]) not in uhd]
        out.append({**cat, "channels": kept})
    return {"categories": out}


def replay_twins(data: dict, categories: list[dict]) -> dict[int, list[dict]]:
    """Hidden versions of each channel shown (same category, same name without the quality words), the
    FHD first: the provider has the replay of "TF1 FHD" but not of "TF1 4K"."""
    shown = {ch["id"] for cat in categories for ch in cat["channels"]}
    raw = {cat["id"]: cat["channels"] for cat in data.get("categories", [])}
    twins: dict[int, list[dict]] = {}
    for cat in categories:
        by_key: dict[str, list[dict]] = {}
        for ch in raw.get(cat["id"], []):
            if ch["id"] not in shown:
                by_key.setdefault(base_name(ch["name"]), []).append(ch)
        for ch in cat["channels"]:
            others = sorted(by_key.get(base_name(ch["name"]), []), key=lambda c: twin_rank(c["name"]))
            if others:
                twins[ch["id"]] = others[:MAX_TWINS]
    return twins


def kodi_lineup(data: dict, hidden: dict | None) -> list[tuple[str, dict]]:
    """(group, channel) in the order of the Kodi playlist: the channels shown, then their hidden versions
    kept for the replay. Kodi numbers them from 1 in this order."""
    cats = [c for c in apply_filter(data, hidden)["categories"] if c.get("channels")]
    out = [(cat["name"], ch) for cat in cats for ch in cat["channels"]]
    seen = {ch["id"] for _, ch in out}
    for twins in replay_twins(data, cats).values():
        for ch in twins:
            if ch["id"] not in seen:
                seen.add(ch["id"])
                out.append(("Replay (autres versions)", ch))
    return out


class Catalog:
    def __init__(self, data: dict, hidden: dict | None = None, guide=None, no_replay: set | None = None):
        self.categories = [c for c in apply_filter(data, hidden)["categories"] if c.get("channels")]
        self.by_id = {c["id"]: c for c in self.categories}
        self.guide = guide  # epg.Guide or None
        self.no_replay = no_replay if no_replay is not None else set()  # channels seen without catch-up
        self.twins = replay_twins(data, self.categories)
        self.lineup = kodi_lineup(data, hidden)
        self.numbers = {ch["id"]: n for n, (_, ch) in enumerate(self.lineup, 1)}
        self.names = {ch["id"]: ch["name"] for _, ch in self.lineup}

    def number(self, stream_id: int) -> int | None:
        """Channel number in Kodi (order of the playlist made by tools/kodi_feed.py)."""
        return self.numbers.get(stream_id)

    def replay_sources(self, stream_id: int) -> list[int]:
        """Channels to try for the replay of a channel, those known without replay left out: a 4K channel
        through its other versions first (providers seldom record 4K), else the channel itself first."""
        twins = [t["id"] for t in self.twins.get(stream_id, [])]
        own = self.names.get(stream_id, "")
        order = twins + [stream_id] if is_4k(own) else [stream_id] + twins
        return [s for s in order if s not in self.no_replay]

    def find(self, stream_id: int) -> tuple[dict, dict] | None:
        """(category, channel) of a stream id (the first category holding it)."""
        for cat in self.categories:
            for ch in cat["channels"]:
                if ch["id"] == stream_id:
                    return cat, ch
        return None

    def channels(self) -> list[dict]:
        return [ch for c in self.categories for ch in c["channels"]]

    def now_line(self, stream_id: int) -> str | None:
        """"14:00 Plus belle la vie" for the programme on air, None without guide."""
        p = self.guide.now(stream_id) if self.guide else None
        return f"{hhmm(p['start'])} {p['title']}" if p else None

    def channel(self, cat_id: str, stream_id: int) -> dict | None:
        for ch in self.by_id.get(cat_id, {}).get("channels", []):
            if ch["id"] == stream_id:
                return ch
        return None

    def neighbour(self, cat_id: str, stream_id: int, step: int) -> dict | None:
        """Next (+1) or previous (-1) channel of a category, wrapping around."""
        channels = self.by_id.get(cat_id, {}).get("channels", [])
        for n, ch in enumerate(channels):
            if ch["id"] == stream_id:
                return channels[(n + step) % len(channels)]
        return None

    @staticmethod
    def parse_channel_id(media_id: str, prefix: str = "ch") -> tuple[str, int] | None:
        parts = (media_id or "").split(":")
        if len(parts) == 3 and parts[0] == prefix and parts[2].isdigit():
            return parts[1], int(parts[2])
        return None

    @staticmethod
    def parse_replay_id(media_id: str) -> tuple[str, int, int] | None:
        parts = (media_id or "").split(":")
        if len(parts) == 4 and parts[0] == "rp" and parts[2].isdigit() and parts[3].isdigit():
            return parts[1], int(parts[2]), int(parts[3])
        return None

    def has_replay(self, stream_id: int) -> bool:
        return bool(self.guide and self.replay_sources(stream_id) and self.guide.past(stream_id, 1))

    def channel_item(
        self, cat: dict, ch: dict, with_category: bool = False, subtitle: str | None = None, guide_mode: bool = False
    ) -> BrowseMediaItem:
        """A channel row: tap = watch it; in guide mode, tap = its programme page (the play icon still watches it)."""
        line = subtitle or self.now_line(ch["id"]) or (cat["name"] if with_category else None)
        has_guide = guide_mode and bool(self.guide and self.guide.programmes(ch["id"]))
        return BrowseMediaItem(
            media_id=f"ch:{cat['id']}:{ch['id']}",
            title=ch["name"],
            subtitle=line,
            artist=line,
            media_class=MediaClass.CHANNEL,
            media_type=MediaContentType.CHANNEL,
            can_play=True,
            can_browse=has_guide,
            thumbnail=ch.get("logo") or CHANNEL_ICON,
        )

    def programme_items(self, cat: dict, ch: dict) -> list[BrowseMediaItem]:
        items = []
        now = time.time()
        if self.has_replay(ch["id"]):
            items.append(BrowseMediaItem(
                media_id=f"past:{cat['id']}:{ch['id']}",
                title="Déjà diffusé",
                subtitle="Revoir un programme passé (replay)",
                media_class=MediaClass.DIRECTORY,
                media_type=MediaContentType.VIDEO,
                can_browse=True,
                thumbnail=REPLAY_ICON,
            ))
        for p in self.guide.upcoming(ch["id"]) if self.guide else []:
            when = f"{day_label(p['start'], now)}{hhmm(p['start'])}–{hhmm(p['stop'])}"
            on_air = p["start"] <= now < p["stop"]
            items.append(BrowseMediaItem(
                media_id=f"ch:{cat['id']}:{ch['id']}",  # playing a programme row plays the channel
                title=p["title"] + (f" – {p['sub']}" if p.get("sub") else ""),
                subtitle=("En cours · " if on_air else "") + when + (f" · {p['cat']}" if p.get("cat") else ""),
                media_class=MediaClass.VIDEO,
                media_type=MediaContentType.VIDEO,
                can_play=on_air,
                thumbnail=p.get("icon") or ch.get("logo") or CHANNEL_ICON,
            ))
        return items

    def past_items(self, cat: dict, ch: dict) -> list[BrowseMediaItem]:
        now = time.time()
        return [BrowseMediaItem(
            media_id=f"rp:{cat['id']}:{ch['id']}:{p['start']}",
            title=p["title"] + (f" – {p['sub']}" if p.get("sub") else ""),
            subtitle=f"{day_label(p['start'], now)}{hhmm(p['start'])}–{hhmm(p['stop'])}"
            + (f" · {p['cat']}" if p.get("cat") else ""),
            media_class=MediaClass.VIDEO,
            media_type=MediaContentType.VIDEO,
            can_play=True,
            thumbnail=p.get("icon") or ch.get("logo") or CHANNEL_ICON,
        ) for p in (self.guide.past(ch["id"]) if self.guide else [])]

    def category_item(self, cat: dict) -> BrowseMediaItem:
        return BrowseMediaItem(
            media_id=f"cat:{cat['id']}",
            title=cat["name"],
            subtitle=f"{len(cat['channels'])} chaînes",
            media_class=MediaClass.DIRECTORY,
            media_type=MediaContentType.CHANNELS,
            can_browse=True,
            can_search=True,
            thumbnail=CATEGORY_ICON,
        )

    def browse(self, media_id: str | None) -> tuple[BrowseMediaItem, list[BrowseMediaItem]]:
        """Container and its children; KeyError for an unknown id."""
        media_id = media_id or ROOT
        if media_id == ROOT:
            container = BrowseMediaItem(
                media_id=ROOT, title="IPTV", media_class=MediaClass.DIRECTORY,
                media_type=MediaContentType.CHANNELS, can_browse=True, can_search=True,
            )
            return container, [self.category_item(c) for c in self.categories]
        if media_id.startswith("cat:"):
            cat = self.by_id[media_id[4:]]
            container = self.category_item(cat)
            items = [self.channel_item(cat, ch) for ch in cat["channels"]]
            if self.guide and any(self.guide.programmes(ch["id"]) for ch in cat["channels"]):
                items.insert(0, BrowseMediaItem(
                    media_id=f"guide:{cat['id']}", title="Programme TV",
                    subtitle="Voir le programme de chaque chaîne", media_class=MediaClass.DIRECTORY,
                    media_type=MediaContentType.CHANNELS, can_browse=True, thumbnail=GUIDE_ICON,
                ))
            return container, items
        if media_id.startswith("guide:"):
            cat = self.by_id[media_id[6:]]
            container = BrowseMediaItem(
                media_id=media_id, title=f"Programme TV · {cat['name']}", media_class=MediaClass.DIRECTORY,
                media_type=MediaContentType.CHANNELS, can_browse=True, thumbnail=GUIDE_ICON,
            )
            return container, [self.channel_item(cat, ch, guide_mode=True) for ch in cat["channels"]]
        parsed = self.parse_channel_id(media_id, "past")
        if parsed and self.channel(*parsed):
            cat, ch = self.by_id[parsed[0]], self.channel(*parsed)
            container = BrowseMediaItem(
                media_id=media_id, title=f"Déjà diffusé · {ch['name']}", media_class=MediaClass.DIRECTORY,
                media_type=MediaContentType.VIDEO, can_browse=True, thumbnail=ch.get("logo") or REPLAY_ICON,
            )
            return container, self.past_items(cat, ch)
        parsed = self.parse_channel_id(media_id)
        if parsed and self.channel(*parsed):
            cat, ch = self.by_id[parsed[0]], self.channel(*parsed)
            p = self.guide.now(ch["id"]) if self.guide else None
            container = BrowseMediaItem(  # programme page: cover = logo, info line = programme on air, Play = watch
                media_id=media_id,
                title=ch["name"],
                artist=f"En cours : {p['title']}" if p else cat["name"],
                media_class=MediaClass.PLAYLIST,  # the remote shows a playlist as a page: cover, info line, Play
                media_type=MediaContentType.CHANNEL,
                can_browse=True,
                can_play=True,
                thumbnail=ch.get("logo") or CHANNEL_ICON,
            )
            return container, self.programme_items(cat, ch)
        raise KeyError(media_id)

    def search(self, query: str, limit: int = 200) -> list[BrowseMediaItem]:
        """Channels whose name contains the query, the closest names first."""
        needle = fold(query)
        if not needle:
            return []
        found = []
        for cat in self.categories:
            for ch in cat["channels"]:
                name = fold(ch["name"])
                if needle in name:
                    found.append((len(name), cat, ch))
        found.sort(key=lambda t: t[0])
        items = [self.channel_item(cat, ch, with_category=True) for _, cat, ch in found[:limit]]
        if self.guide:  # then the channels airing a matching programme now or within 24 h
            seen = {i.media_id for i in items}
            where = {ch["id"]: cat for cat in self.categories for ch in cat["channels"]}
            now = time.time()
            for sid, p in self.guide.search(query):
                cat = where.get(sid)
                ch = self.channel(cat["id"], sid) if cat else None
                if not ch or f"ch:{cat['id']}:{sid}" in seen:
                    continue
                when = "En cours" if p["start"] <= now else f"{day_label(p['start'], now)}{hhmm(p['start'])}"
                items.append(self.channel_item(cat, ch, subtitle=f"{when} · {p['title']}"))
                if len(items) >= limit:
                    break
        return items
