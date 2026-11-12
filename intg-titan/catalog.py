"""The channel catalog as media browser items for the remote. No I/O.

catalog.json (built by tools/build_catalog.py from the provider's M3U playlist):
  {"categories": [{"id": "3", "name": "FR TV FULL HD|4K (France)", "channels": [{"id": 1149, "name": "TF1 FHD",
  "logo": "https://..."}]}]}

Media ids:
  root               the categories, in the provider's order (as in Titan Player)
  cat:<cat>          a category's channels
  ch:<cat>:<stream>  a channel (played with the iptvtitan://channel/<account>/<stream> link)
"""

import unicodedata

from ucapi.media_player import BrowseMediaItem, MediaClass, MediaContentType

ROOT = "root"
CATEGORY_ICON = "icon://uc:list"
CHANNEL_ICON = "icon://uc:tv"


def fold(text: str) -> str:
    """Lower case without accents nor spaces, for search: "France 2" ~ "france2"."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in text.lower() if ch.isalnum())


def apply_filter(data: dict, hidden: dict | None) -> dict:
    """Catalog without the categories and channels hidden on the settings page.

    hidden = {"categories": ["7", ...], "channels": {"3": [1151, ...]}}
    """
    hidden = hidden or {}
    cats = set(hidden.get("categories") or [])
    chans = {k: set(v) for k, v in (hidden.get("channels") or {}).items()}
    out = []
    for cat in data.get("categories", []):
        if cat["id"] in cats:
            continue
        drop = chans.get(cat["id"], set())
        out.append({**cat, "channels": [ch for ch in cat["channels"] if ch["id"] not in drop]})
    return {"categories": out}


class Catalog:
    def __init__(self, data: dict, hidden: dict | None = None):
        self.categories = [c for c in apply_filter(data, hidden)["categories"] if c.get("channels")]
        self.by_id = {c["id"]: c for c in self.categories}

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
    def parse_channel_id(media_id: str) -> tuple[str, int] | None:
        parts = (media_id or "").split(":")
        if len(parts) == 3 and parts[0] == "ch" and parts[2].isdigit():
            return parts[1], int(parts[2])
        return None

    def channel_item(self, cat: dict, ch: dict, with_category: bool = False) -> BrowseMediaItem:
        return BrowseMediaItem(
            media_id=f"ch:{cat['id']}:{ch['id']}",
            title=ch["name"],
            subtitle=cat["name"] if with_category else None,
            media_class=MediaClass.CHANNEL,
            media_type=MediaContentType.CHANNEL,
            can_play=True,
            thumbnail=ch.get("logo") or CHANNEL_ICON,
        )

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
                media_id=ROOT, title="Titan Player", media_class=MediaClass.DIRECTORY,
                media_type=MediaContentType.CHANNELS, can_browse=True, can_search=True,
            )
            return container, [self.category_item(c) for c in self.categories]
        if media_id.startswith("cat:"):
            cat = self.by_id[media_id[4:]]
            container = self.category_item(cat)
            return container, [self.channel_item(cat, ch) for ch in cat["channels"]]
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
        return [self.channel_item(cat, ch, with_category=True) for _, cat, ch in found[:limit]]
