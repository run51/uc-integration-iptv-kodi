"""Kodi JSON-RPC client (Kodi on the Shield plays the IPTV channels through PVR IPTV Simple Client).

Kodi's channels come from the playlist made by tools/kodi_feed.py out of the same catalog, so a channel of the
catalog is found in Kodi by its name (the feed writes the catalog names as they are, " replaced by ').
"""

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = "http://192.168.2.25:8080/jsonrpc"
CHANNELS_TTL_S = 600


def kodi_name(name: str) -> str:
    """The channel name as the feed writes it in the playlist (and Kodi shows it)."""
    return (name or "").replace('"', "'").replace("\n", " ").strip()


def utc_text(ts: float) -> str:
    """Unix time -> "2026-11-12 09:40:00", the UTC format of PVR.GetBroadcasts."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_ts(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def seconds(t: dict | None) -> int:
    t = t or {}
    return int(t.get("hours", 0)) * 3600 + int(t.get("minutes", 0)) * 60 + int(t.get("seconds", 0))


class KodiError(Exception):
    pass


class Kodi:
    def __init__(self, url: str = DEFAULT_URL, auth: str = ""):
        self.url = url or DEFAULT_URL
        self.auth = "Basic " + base64.b64encode(auth.encode()).decode() if auth else ""
        self.channels: list[dict] = []  # [{channelid, label, channelnumber}]
        self.channels_at = 0.0

    def _call(self, method: str, params: dict | None, timeout: float) -> object:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        if self.auth:
            req.add_header("Authorization", self.auth)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if "error" in data:
            raise KodiError(f"{method}: {data['error'].get('message')}")
        return data.get("result")

    async def call(self, method: str, params: dict | None = None, timeout: float = 8) -> object:
        try:
            return await asyncio.to_thread(self._call, method, params, timeout)
        except (OSError, ValueError, urllib.error.URLError) as ex:
            raise KodiError(f"{method}: {ex}") from ex

    async def alive(self) -> bool:
        try:
            await self.call("JSONRPC.Ping", timeout=2)
            return True
        except KodiError:
            return False

    async def pvr_ready(self) -> bool:
        try:
            return bool((await self.call("PVR.GetProperties", {"properties": ["available"]}))["available"])
        except (KodiError, TypeError, KeyError):
            return False

    async def load_channels(self, force: bool = False) -> list[dict]:
        if force or not self.channels or time.monotonic() - self.channels_at > CHANNELS_TTL_S:
            res = await self.call("PVR.GetChannels", {"channelgroupid": "alltv", "properties": ["channelnumber"]})
            self.channels = (res or {}).get("channels") or []
            self.channels_at = time.monotonic()
        return self.channels

    async def channel_id(self, name: str, number: int | None = None) -> int | None:
        """Kodi channel id of a catalog channel (same name; the number tells apart channels of the same name)."""
        for force in (False, True):
            same = [c for c in await self.load_channels(force) if c.get("label") == kodi_name(name)]
            if same:
                exact = [c for c in same if number and c.get("channelnumber") == number]
                return (exact or same)[0]["channelid"]
        return None

    async def broadcast_id(self, channel_id: int, start: float) -> int | None:
        """Kodi broadcast of the channel starting at this time (the guide of Kodi and ours are the same file)."""
        res = await self.call("PVR.GetBroadcasts", {"channelid": channel_id, "properties": ["starttime"]})
        best, gap = None, 121.0
        for b in (res or {}).get("broadcasts") or []:
            try:
                d = abs(utc_ts(b["starttime"]) - start)
            except (KeyError, ValueError):
                continue
            if d < gap:
                best, gap = b["broadcastid"], d
        return best

    async def player(self) -> dict | None:
        """{item, speed, time, total} of the video playing, None when nothing plays."""
        players = await self.call("Player.GetActivePlayers") or []
        video = next((p for p in players if p.get("type") == "video"), None)
        if not video:
            return None
        pid = video["playerid"]
        fields = ["title", "channel", "channelnumber", "starttime", "endtime", "thumbnail"]
        item = (await self.call("Player.GetItem", {"playerid": pid, "properties": fields}) or {}).get("item") or {}
        if not item.get("channel"):  # a replay (broadcast) item has no channel field: ask the player
            labels = await self.call("XBMC.GetInfoLabels", {"labels": ["VideoPlayer.ChannelName"]}) or {}
            item["channel"] = labels.get("VideoPlayer.ChannelName") or ""
        props = await self.call("Player.GetProperties", {
            "playerid": pid, "properties": ["speed", "time", "totaltime"]}) or {}
        return {"playerid": pid, "item": item, "speed": props.get("speed", 1),
                "time": seconds(props.get("time")), "total": seconds(props.get("totaltime"))}
