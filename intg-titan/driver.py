"""IPTV integration for the Unfolded Circle Remote: browse the IPTV channels by category, with their logos and
the TV guide, and watch them, live or in replay, in Kodi on the Android TV box (NVIDIA Shield).

Chain: Remote → this driver → Kodi JSON-RPC (PVR IPTV Simple Client fed by tools/kodi_feed.py with the same
       catalog) → Player.Open channel / broadcast.
Kodi is brought to the front through the Remote's own Core REST API and the Android TV integration (the Shield
refuses the usual "market://launch" app link: its Play Store page is opened and "Ouvrir" pressed).

The channel catalog (categories, names, stream ids, logos; no login) is built from the provider's M3U
playlist by tools/build_catalog.py and read from catalog.json (config directory, else next to the driver).
"""

import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import epg
import feed
import images
import m3u
import settings_page
import ucapi
from catalog import LOCAL_TZ, Catalog, day_label, hhmm, is_4k
from kodi_client import DEFAULT_URL as KODI_URL
from kodi_client import Kodi, KodiError, kodi_name, utc_ts
from kodi_guide import KodiGuide
from kodi_guide import programme as kodi_programme
from ucapi import media_player as mp

_LOG = logging.getLogger("titan")
ENTITY_ID = "titan_player"  # kept from the Titan Player versions: activities and pages refer to it
DEFAULT_SHIELD = "uc_androidtv_driver.main.48B02D36F2DF"
DEFAULT_LIMIT, MAX_LIMIT = 50, 100
POLL_S = 5
GUIDE_CHECK_S = 30 * 60
GUIDE_NIGHT = (3, 6)  # the public guide is updated once a day: downloaded at night (local hours)
GUIDE_MAX_AGE_S = 36 * 3600  # older than that (the Remote slept all night): downloaded at once
KODI_STORE = "https://play.google.com/store/apps/details?id=org.xbmc.kodi"
KODI_APP, STORE_APP = "org.xbmc.kodi", "com.android.vending"
ANDROID_LOG = "intg-androidtv"  # log service of the Remote's Android TV integration
STREAM_START_S = 10  # a 4K channel silent for this long is replaced by its FHD version
REPLAY_CHECK_S = 12  # after starting a replay, time to see whether the provider sent the programme or live TV
OFF_STATES = ("OFF", "STANDBY", "UNAVAILABLE")
SETTINGS_PORT = 9300  # the Remote refuses 8000-9200 to custom integrations

FEATURES = [
    mp.Features.ON_OFF,
    mp.Features.TOGGLE,
    mp.Features.PLAY_PAUSE,
    mp.Features.STOP,
    mp.Features.NEXT,
    mp.Features.PREVIOUS,
    mp.Features.FAST_FORWARD,
    mp.Features.REWIND,
    mp.Features.CHANNEL_SWITCHER,
    mp.Features.MEDIA_TITLE,
    mp.Features.MEDIA_ARTIST,
    mp.Features.MEDIA_IMAGE_URL,
    mp.Features.MEDIA_TYPE,
    mp.Features.MEDIA_DURATION,
    mp.Features.MEDIA_POSITION,
    mp.Features.PLAY_MEDIA,
    mp.Features.BROWSE_MEDIA,
    mp.Features.SEARCH_MEDIA,
]
STEP = {mp.Commands.NEXT: 1, mp.Commands.CHANNEL_UP: 1, mp.Commands.PREVIOUS: -1, mp.Commands.CHANNEL_DOWN: -1}
SEEK = {mp.Commands.FAST_FORWARD: "smallforward", mp.Commands.REWIND: "smallbackward"}
ERRORS = (OSError, ValueError, urllib.error.URLError, KodiError)


def lan_address() -> str:
    """Address of the Remote on the local network (the pictures are also shown by the web configurator)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent: only the route is looked up
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def item_ts(text: str) -> float | None:
    """Times of the item playing ("2026-11-12 14:45:00", UTC like the broadcasts) -> unix time."""
    try:
        return utc_ts(text)
    except (TypeError, ValueError):
        return None


class RemoteApi:
    """Minimal client of the Remote's Core REST API (API key)."""

    def __init__(self, base_url: str, api_key: str):
        url = urllib.parse.urlsplit(base_url.rstrip("/"))
        self.auth = "Bearer " + api_key
        if url.username:  # "http://user:password@host" (e.g. the core simulator): basic authentication
            cred = f"{urllib.parse.unquote(url.username)}:{urllib.parse.unquote(url.password or '')}"
            self.auth = "Basic " + base64.b64encode(cred.encode()).decode()
            url = url._replace(netloc=url.hostname + (f":{url.port}" if url.port else ""))
        self.base = urllib.parse.urlunsplit(url)

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": self.auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"null")

    async def entity(self, entity_id: str) -> dict:
        return await asyncio.to_thread(self._call, "GET", f"/api/entities/{entity_id}")

    async def command(self, entity_id: str, cmd_id: str, params: dict | None = None) -> None:
        body = {"cmd_id": cmd_id, **({"params": params} if params else {})}
        await asyncio.to_thread(self._call, "PUT", f"/api/entities/{entity_id}/command", body)

    async def current_app(self, service: str = ANDROID_LOG) -> tuple[str, str] | None:
        """(time, package) of the app on screen, as the Android TV integration last logged it
        ("current_app: org.xbmc.kodi"); its media player entity does not tell (its source stays the Play
        Store after Kodi was opened from there). None when the log is not readable."""
        try:
            lines = await asyncio.to_thread(
                self._call, "GET", f"/api/system/logs?s={service}&p=7&limit=5&q=current_app")
        except (OSError, ValueError, urllib.error.URLError):
            return None
        for line in lines or []:
            msg = str(line.get("msg") or "")
            if "current_app:" in msg:
                return str(line.get("ts") or ""), msg.split("current_app:")[-1].strip()
        return None


def page_bounds(paging) -> tuple[int, int]:
    page = max(1, int(getattr(paging, "page", 1) or 1))
    limit = min(MAX_LIMIT, max(1, int(getattr(paging, "limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)))
    return page, limit


class TitanPlayer(mp.MediaPlayer):
    def __init__(self, driver: "Driver"):
        super().__init__(
            ENTITY_ID,
            {"en": "IPTV", "fr": "IPTV"},
            FEATURES,
            {mp.Attributes.STATE: mp.States.UNKNOWN},
            device_class=mp.DeviceClasses.TV,
            cmd_handler=driver.on_command,
        )
        self._driver = driver

    async def browse(self, options):
        return await self._driver.browse(options)

    async def search(self, options):
        return await self._driver.search(options)


class Driver:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.api = ucapi.IntegrationAPI(loop)
        self.remote: RemoteApi | None = None
        self.shield = DEFAULT_SHIELD
        self.kodi = Kodi()
        self.no_replay: set[int] = set()  # channels whose provider sent live TV instead of a past programme
        self.by_kodi_name: dict[str, tuple[str, int]] = {}
        self.catalog = Catalog({})
        self.current: tuple[str, int] | None = None  # (category id, stream id) last played
        self.replay: dict | None = None  # {"stream" (source), "shown", "category", "start"} of the last replay
        self.check_task: asyncio.Task | None = None
        self.launch_lock = asyncio.Lock()
        self.background: asyncio.Task | None = None
        self.stream_base = ""  # "http://host:port/user/pass" of the provider (private, never logged)
        self.feed_cache: bytes | None = None
        # Kodi put on screen by this driver and not left since (the Android TV integration cannot tell: its
        # "source" stays "Google Play Store" or "Kodi" even after a sleep, when the home screen is back)
        self.kodi_front = False
        self.last: dict = {}
        self.poll_task: asyncio.Task | None = None
        self.raw_catalog: dict = {}
        self.hidden: dict = {}
        self.settings_server = None
        self.guide = KodiGuide()  # programmes asked to Kodi (it reads the public guide itself)
        self.images: images.ImageCache | None = None
        self.guide_ids: dict[int, str] = {}  # stream id -> guide channel id, for the tvg-id of Kodi's playlist
        self.sid_by_kodi_name: dict[str, int] = {}
        self.epg_task: asyncio.Task | None = None
        self.programme_key = None  # (stream, start) of the programme whose position was last sent

    def rebuild(self) -> None:
        self.catalog = Catalog(self.raw_catalog, self.hidden, self.guide, self.no_replay,
                               self.images.url_for if self.images else None)
        self.feed_cache = None
        self.sid_by_kodi_name = {}
        for _, ch in self.catalog.lineup:
            self.sid_by_kodi_name.setdefault(kodi_name(ch["name"]), ch["id"])
        self.by_kodi_name = {}
        for cat in self.catalog.categories:
            for ch in cat["channels"]:
                self.by_kodi_name.setdefault(kodi_name(ch["name"]), (cat["id"], ch["id"]))
                for twin in self.catalog.twins.get(ch["id"], []):  # its replay is shown as the channel's
                    self.by_kodi_name.setdefault(kodi_name(twin["name"]), (cat["id"], ch["id"]))

    def save_state(self) -> None:
        state = {"no_replay": sorted(self.no_replay)}
        if self.current:
            state.update(category=self.current[0], stream=self.current[1])
        try:
            with open(self.path("state.json"), "w", encoding="utf-8") as file:
                json.dump(state, file)
        except OSError:
            pass

    @staticmethod
    def tls_context() -> ssl.SSLContext:
        """Certificates of certifi: the Python packed for the Remote has no system certificate store."""
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            return ssl.create_default_context()

    def download_guide(self) -> None:
        """Public guide written to the config directory as it comes (~9 MB gzipped)."""
        for url in epg.SOURCES:
            tmp = self.path("epg.xml.gz.part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "uc-intg-iptv-kodi"})
                with urllib.request.urlopen(req, timeout=120, context=self.tls_context()) as resp, \
                        open(tmp, "wb") as out:
                    shutil.copyfileobj(resp, out, 65536)
                os.replace(tmp, self.path("epg.xml.gz"))
                return
            except (OSError, ValueError) as ex:
                _LOG.warning("Guide download %s failed: %s", url, ex)

    async def load_guide(self, download: bool = True) -> None:
        """Download the public guide (or use the copy kept) and match the playlist channels to its channel ids,
        from its channel list only: Kodi reads the programmes (the full file is served to it), the Remote asks
        Kodi for what it shows."""
        if download:
            await asyncio.to_thread(self.download_guide)
        source = self.path("epg.xml.gz")
        if not os.path.exists(source):
            return
        # all the channels of the playlist, hidden ones too: the replay of a 4K channel uses its FHD version
        channels = [ch for cat in self.raw_catalog.get("categories", []) for ch in cat["channels"]]

        def work():
            names, _ = epg.parse(source, set(), channels_only=True)
            return epg.match_ids(channels, names)

        self.guide_ids = await asyncio.to_thread(work)
        self.feed_cache = None
        _LOG.info("Guide: %d of %d playlist channels matched", len(self.guide_ids), len(channels))

    # --------------------------------------------------------- programmes from Kodi
    async def refresh_nows(self, force: bool = False) -> None:
        """Programme on air of every channel, in one request to Kodi (kept a minute)."""
        if not (force or self.guide.nows_stale()) or not await self.kodi.alive():
            return
        res = await self.kodi.call("PVR.GetChannels", {"channelgroupid": "alltv", "properties": ["broadcastnow"]})
        nows = {}
        for c in (res or {}).get("channels") or []:
            sid = self.sid_by_kodi_name.get(c.get("label") or "")
            p = kodi_programme(c["broadcastnow"], utc_ts) if sid and c.get("broadcastnow") else None
            if p:
                nows[sid] = p
        self.guide.set_nows(nows)

    async def fetch_channel(self, stream_id: int) -> None:
        """Programmes of a channel (yesterday to the coming days), asked when its page is opened."""
        if not self.guide.channel_stale(stream_id) or not await self.kodi.alive():
            return
        cid = await self.kodi_channel(stream_id)
        if cid is None:
            return
        res = await self.kodi.call("PVR.GetBroadcasts", {
            "channelid": cid, "properties": ["title", "starttime", "endtime", "genre", "episodename", "thumbnail"]})
        progs = [p for p in (kodi_programme(b, utc_ts) for b in (res or {}).get("broadcasts") or []) if p]
        self.guide.set_channel(stream_id, progs)

    async def prepare(self, media_id: str | None) -> None:
        """Ask Kodi for the programmes the page about to be shown needs (nothing when Kodi is not running)."""
        try:
            await self.refresh_nows()
            target = None
            if not media_id:
                playing = self.last.get(mp.Attributes.STATE) in (mp.States.PLAYING, mp.States.PAUSED)
                target = self.current[1] if playing and self.current else None
            else:
                parsed = Catalog.parse_channel_id(media_id) or Catalog.parse_channel_id(media_id, "past")
                target = parsed[1] if parsed else None
            if target:
                await self.fetch_channel(target)
        except KodiError as ex:
            _LOG.debug("Programmes not available from Kodi: %s", ex)

    def guide_due(self) -> bool:
        """Download the guide once a night (8 MB); at once when missing or older than a day and a half."""
        path = self.path("epg.xml.gz")
        if not os.path.exists(path):
            return True
        age = time.time() - os.path.getmtime(path)
        hour = datetime.now(LOCAL_TZ).hour
        return age > GUIDE_MAX_AGE_S or (age > 12 * 3600 and GUIDE_NIGHT[0] <= hour < GUIDE_NIGHT[1])

    async def prefetch_logos(self) -> None:
        """Logos of the channels shown (and kept for the replay) downloaded into the image cache, gently."""
        if self.images:
            urls = [ch.get("logo") for _, ch in self.catalog.lineup if ch.get("logo")]
            queued = self.images.prefetch(urls)  # downloaded one by one by the cache's background worker
            if queued:
                _LOG.info("Image cache: %d channel logos queued", queued)

    async def guide_loop(self) -> None:
        await self.load_guide(download=False)  # copy kept first: the channel ids at once
        await self.prefetch_logos()  # only the logos missing (first start, new channels)
        while True:
            try:
                if self.guide_due():
                    await self.load_guide()
                    await self.prefetch_logos()
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - keep the loop alive
                _LOG.warning("Guide refresh failed: %s", ex)
            await asyncio.sleep(GUIDE_CHECK_S)

    # ------------------------------------------------------- playlist and Kodi feed
    def import_m3u(self, path: str) -> dict:
        """Called from the settings page thread with the uploaded playlist: catalog + private stream base."""
        catalog, base = m3u.parse_file(path)
        count = m3u.channel_count(catalog)
        if not count or not base:
            raise ValueError("aucune chaîne en direct au format Xtream dans ce fichier")
        with open(self.path("catalog.json"), "w", encoding="utf-8") as file:
            json.dump(catalog, file, ensure_ascii=False, separators=(",", ":"))
        tmp = self.path("streams.json.tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as file:
            json.dump({"stream_base": base}, file)
        os.replace(tmp, self.path("streams.json"))
        _LOG.info("Playlist imported: %d categories, %d live channels", len(catalog["categories"]), count)
        self.loop.call_soon_threadsafe(self.use_catalog, catalog, base)
        return {"categories": len(catalog["categories"]), "channels": count}

    def use_catalog(self, catalog: dict, base: str) -> None:
        self.raw_catalog, self.stream_base, self.feed_cache = catalog, base, None
        self.rebuild()
        asyncio.ensure_future(self.load_guide(download=not os.path.exists(self.path("epg.xml.gz"))))
        asyncio.ensure_future(self.prefetch_logos())

    def playlist_status(self) -> dict:
        path = next((p for p in (self.path("streams.json"), self.path("catalog.json")) if os.path.exists(p)), None)
        if not path or not self.raw_catalog:
            return {"channels": 0}
        when = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d/%m/%Y %H:%M")
        return {"channels": m3u.channel_count(self.raw_catalog), "imported": when}

    def feed_allowed(self, ip: str) -> bool:
        kodi_host = urllib.parse.urlsplit(self.kodi.url).hostname
        return ip in (kodi_host, "127.0.0.1")

    def feed_file(self, path: str) -> tuple[bytes, str] | None:
        """Kodi's playlist (built from the channels shown + their versions kept for the replay) or guide."""
        if path == "/epg.xml.gz":  # the whole public guide: Kodi reads the programmes itself
            try:
                with open(self.path("epg.xml.gz"), "rb") as file:
                    return file.read(), "application/gzip", os.path.getmtime(self.path("epg.xml.gz"))
            except OSError:
                return None
        if not self.stream_base or not self.catalog.lineup:
            return None
        if self.feed_cache is None:
            self.feed_cache = feed.build_m3u(self.catalog.lineup, self.guide_ids, self.stream_base).encode()
        return self.feed_cache, "audio/x-mpegurl; charset=utf-8"

    def save_hidden(self, hidden: dict) -> None:
        """Called from the settings page thread: store the hidden lists and rebuild the browsed catalog."""
        with open(self.path("hidden.json"), "w", encoding="utf-8") as file:
            json.dump(hidden, file)
        self.hidden = hidden
        # rebuilt in the event loop like after a playlist import: one rebuild after the other, the last one
        # with the latest choice (a rebuild here could finish before one started earlier without it)
        self.loop.call_soon_threadsafe(self.rebuild)
        _LOG.info("Hidden: %d categories, %d channel lists", len(hidden["categories"]), len(hidden["channels"]))

    # ------------------------------------------------------------- configuration
    def path(self, name: str) -> str:
        return os.path.join(self.api.config_dir_path, name)

    def load_json(self, name: str) -> dict | None:
        for path in (self.path(name), os.path.join(os.path.dirname(os.path.abspath(__file__)), name)):
            try:
                with open(path, encoding="utf-8") as file:
                    return json.load(file)
            except (OSError, ValueError):
                continue
        return None

    def configure(self, cfg: dict) -> None:
        self.remote = RemoteApi(cfg["remote_url"], cfg["api_key"])
        self.shield = cfg.get("shield") or DEFAULT_SHIELD
        self.kodi = Kodi(cfg.get("kodi_url") or KODI_URL, cfg.get("kodi_auth") or "")
        self.stream_base = (self.load_json("streams.json") or {}).get("stream_base", "")
        self.raw_catalog = self.load_json("catalog.json") or {}
        self.hidden = self.load_json("hidden.json") or {}
        state = self.load_json("state.json") or {}
        self.no_replay = {int(x) for x in state.get("no_replay") or []}
        port = int(os.getenv("TITAN_SETTINGS_PORT", str(SETTINGS_PORT)))
        if self.images is None:
            self.images = images.ImageCache(self.path("images"))
            self.images.base = f"http://{lan_address()}:{port}"
        self.rebuild()
        if self.epg_task is None or self.epg_task.done():
            self.epg_task = asyncio.get_event_loop().create_task(self.guide_loop())
        if self.settings_server is None:
            try:
                self.settings_server = settings_page.start(
                    port, lambda: self.raw_catalog, lambda: self.hidden, self.save_hidden,
                    import_m3u=self.import_m3u, feed=self.feed_file, allowed=self.feed_allowed,
                    playlist=self.playlist_status, image=self.images.get)
            except OSError as ex:
                _LOG.warning("Settings page not started on port %d: %s", port, ex)
        if state.get("category") and state.get("stream"):
            self.current = (str(state["category"]), int(state["stream"]))
        if not self.api.available_entities.contains(ENTITY_ID):
            self.api.available_entities.add(TitanPlayer(self))
        _LOG.info("Remote %s, Shield %s, Kodi %s, %d categories", self.remote.base, self.shield, self.kodi.url,
                  len(self.catalog.categories))

    async def setup_handler(self, msg: ucapi.SetupDriver) -> ucapi.SetupAction:
        if isinstance(msg, ucapi.DriverSetupRequest):
            data = msg.setup_data
            cfg = {
                "remote_url": (data.get("remote_url") or "http://127.0.0.1").strip(),
                "api_key": (data.get("api_key") or "").strip(),
                "shield": (data.get("shield") or DEFAULT_SHIELD).strip(),
                "kodi_url": (data.get("kodi_url") or KODI_URL).strip(),
            }
            try:
                await RemoteApi(cfg["remote_url"], cfg["api_key"]).entity(cfg["shield"])
            except (OSError, ValueError, urllib.error.URLError) as ex:
                _LOG.warning("Setup: the Remote API or the Shield entity does not answer: %s", ex)
                return ucapi.SetupError(ucapi.IntegrationSetupError.CONNECTION_REFUSED)
            os.makedirs(self.api.config_dir_path, exist_ok=True)
            with open(self.path("config.json"), "w", encoding="utf-8") as file:
                json.dump(cfg, file)
            self.configure(cfg)
            return ucapi.SetupComplete()
        return ucapi.SetupError()

    # -------------------------------------------------------------- browsing
    async def browse(self, options: mp.BrowseOptions):
        await self.prepare(options.media_id)
        playing = self.last.get(mp.Attributes.STATE) in (mp.States.PLAYING, mp.States.PAUSED)
        try:
            container, items = self.catalog.browse(options.media_id, self.current if playing else None)
        except KeyError:
            return ucapi.StatusCodes.NOT_FOUND
        page, limit = page_bounds(options.paging)
        container.items = items[(page - 1) * limit : page * limit]
        return mp.BrowseResults(media=container, pagination=ucapi.Pagination(page=page, limit=limit, count=len(items)))

    async def search(self, options: mp.SearchOptions):
        await self.prepare("root")
        items = self.catalog.search(options.query)
        page, limit = page_bounds(options.paging)
        return mp.SearchResults(media=items[(page - 1) * limit : page * limit],
                                pagination=ucapi.Pagination(page=page, limit=limit, count=len(items)))

    # -------------------------------------------------------------- commands
    async def shield_state(self) -> dict:
        return (await self.remote.entity(self.shield)).get("attributes") or {}

    async def app_after(self, since: str, package: str, seconds: float) -> bool:
        """Wait until the Android TV integration logs this app on screen after the time given."""
        for _ in range(int(seconds * 2)):
            app = await self.remote.current_app()
            if app and app[0] > since and app[1] == package:
                return True
            await asyncio.sleep(0.5)
        return False

    async def bring_kodi(self) -> None:
        """Play Store page of Kodi, then "Ouvrir" (focused) once the page is on screen (it may take 8 s after a
        wake-up), again while Kodi is not on screen. Without the integration's log: fixed waits."""
        _LOG.info("Opening Kodi (Play Store page, then Ouvrir)")
        before = await self.remote.current_app()
        since = before[0] if before else ""
        await self.remote.command(self.shield, "media_player.select_source", {"source": KODI_STORE})
        if before:
            if not await self.app_after(since, STORE_APP, 15):
                _LOG.warning("The Play Store page did not show")
        else:
            await asyncio.sleep(7)
        for _ in range(3):
            app = await self.remote.current_app()
            pressed_at = app[0] if app else ""
            await self.remote.command(self.shield, "media_player.cursor_enter")
            if before:
                if await self.app_after(pressed_at, KODI_APP, 8):
                    break
            else:
                for _ in range(10):
                    await asyncio.sleep(1)
                    if await self.kodi.alive():
                        break
                break
        else:
            _LOG.warning("Kodi did not open from its Play Store page")
            return
        self.kodi_front = True

    async def kodi_on_screen(self) -> bool:
        app = await self.remote.current_app()
        return app[1] == KODI_APP if app else self.kodi_front

    def watch_shield(self, a: dict) -> None:
        """Forget that Kodi is on screen once the Shield sleeps, dreams or shows another app.
        Kodi playing behind another app (or behind the home screen after a wake-up) is closed by Android."""
        source = str(a.get("source") or "")
        if a.get("state") in OFF_STATES or "backdrop" in source.lower() or source not in ("Kodi", "Google Play Store"):
            self.kodi_front = False

    async def ensure_kodi(self, to_front: bool = False) -> bool:
        """Kodi running with its TV ready: wake the Shield, open Kodi when it does not run (or is behind)."""
        async with self.launch_lock:
            a = await self.shield_state()
            self.watch_shield(a)
            woke = a.get("state") in OFF_STATES
            if woke:  # it wakes up on the home screen
                await self.remote.command(self.shield, "media_player.on")
                await asyncio.sleep(8)
            if to_front or woke or not await self.kodi_on_screen() or not await self.kodi.alive():
                await self.bring_kodi()
            for _ in range(30):
                if await self.kodi.alive() and await self.kodi.pvr_ready():
                    return True
                await asyncio.sleep(2)
            _LOG.warning("Kodi did not answer")
            return False

    async def wait_player(self, seconds: float) -> bool:
        for _ in range(int(seconds * 2)):
            if await self.kodi.player():
                return True
            await asyncio.sleep(0.5)
        return False

    async def open_item(self, item: dict) -> None:
        """Player.Open; when nothing starts because Kodi is not on screen (paused behind another app),
        bring Kodi to the front and open again. Kodi on screen: the stream itself does not start."""
        await self.kodi.call("Player.Open", {"item": item})
        if not await self.wait_player(4) and not await self.kodi_on_screen():
            await self.ensure_kodi(to_front=True)
            if not await self.wait_player(6):
                await self.kodi.call("Player.Open", {"item": item})

    async def wait_started(self, seconds: float, stream_id: int) -> bool:
        """The picture of this channel runs: a player on it with a time (a stream that does not answer
        leaves it at 0; the channel opened just before may still be playing for a moment)."""
        wanted = kodi_name(self.catalog.names.get(stream_id, ""))
        for _ in range(int(seconds)):
            pl = await self.kodi.player()
            if pl and pl["time"] > 0 and pl["item"].get("channel") == wanted:
                return True
            await asyncio.sleep(1)
        return False

    async def soon(self, action, *args) -> ucapi.StatusCodes:
        """Answer the Remote at once and play in the background: waking the Shield, opening Kodi and checking
        that the picture starts take longer than the Remote waits for a command. A new command replaces the
        one still running (several CH+ in a row: only the last channel is opened)."""
        if self.background and not self.background.done():
            self.background.cancel()

        async def later():
            try:
                await action(*args)
            except asyncio.CancelledError:
                raise
            except ERRORS as ex:
                _LOG.warning("%s failed: %s", action.__name__, ex)
            except Exception:  # noqa: BLE001 - never lose the error of a background task
                _LOG.exception("%s failed", action.__name__)

        self.background = asyncio.create_task(later())
        return ucapi.StatusCodes.OK

    def img(self, url: str | None) -> str:
        return self.images.url_for(url or "") if self.images else (url or "")

    async def kodi_channel(self, stream_id: int) -> int | None:
        name = self.catalog.names.get(stream_id)
        return await self.kodi.channel_id(name, self.catalog.number(stream_id)) if name else None

    async def open_kodi(self) -> ucapi.StatusCodes:
        return ucapi.StatusCodes.OK if await self.ensure_kodi() else ucapi.StatusCodes.SERVICE_UNAVAILABLE

    def remember(self, cat_id: str, stream_id: int) -> None:
        self.current = (cat_id, stream_id)
        self.save_state()

    async def play_channel(self, cat_id: str, stream_id: int) -> ucapi.StatusCodes:
        if not await self.ensure_kodi():
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        self.replay = None
        self.remember(cat_id, stream_id)
        # a 4K channel that sends nothing (some only broadcast for events): its other versions, FHD first
        found = self.catalog.find(stream_id)
        fallback = [t["id"] for t in self.catalog.twins.get(stream_id, [])] if found and is_4k(found[1]["name"]) else []
        for n, source in enumerate([stream_id, *fallback]):
            cid = await self.kodi_channel(source)
            if cid is None:
                _LOG.warning("Channel %s not found in Kodi", source)
                continue
            _LOG.info("Play channel %s (Kodi %s)%s", source, cid, f" instead of {stream_id}" if n else "")
            await self.open_item({"channelid": cid})
            if not fallback or await self.wait_started(STREAM_START_S, source):
                break
            _LOG.warning("Channel %s does not start", source)
        await self.refresh()
        return ucapi.StatusCodes.OK

    async def play_replay(self, cat_id: str, stream_id: int, start: int) -> ucapi.StatusCodes:
        """Replay of a programme, from the channel itself or one of its other versions (the FHD of a 4K
        channel), whichever is not known to lack the replay."""
        sources = self.catalog.replay_sources(stream_id)
        if not sources:
            return ucapi.StatusCodes.NOT_FOUND
        source = sources[0]
        if not await self.ensure_kodi():
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        cid = await self.kodi_channel(source)
        bid = await self.kodi.broadcast_id(cid, start) if cid is not None else None
        if bid is None:
            _LOG.warning("Programme %s of channel %s not found in Kodi", start, source)
            return ucapi.StatusCodes.NOT_FOUND
        _LOG.info("Play replay of %s at %s from channel %s (Kodi broadcast %s)", stream_id, start, source, bid)
        self.replay = {"stream": source, "shown": stream_id, "category": cat_id, "start": start}
        self.remember(cat_id, stream_id)
        await self.open_item({"broadcastid": bid})
        if self.check_task and not self.check_task.done() and self.check_task is not asyncio.current_task():
            self.check_task.cancel()  # (a retry runs inside the check task itself: do not cancel it)
        self.check_task = asyncio.create_task(self.check_replay(dict(self.replay)))
        await self.refresh()
        return ucapi.StatusCodes.OK

    async def check_replay(self, asked: dict) -> None:
        """Providers send live TV for a channel without catch-up: remember it, try the next version of the
        channel, and hide the "Déjà diffusé" row once none is left."""
        await asyncio.sleep(REPLAY_CHECK_S)
        try:
            pl = await self.kodi.player()
        except KodiError:
            return
        if not pl or self.replay != asked:
            return
        began = item_ts(pl["item"].get("starttime"))
        if began is None or abs(began - asked["start"]) <= 300:
            return
        _LOG.info("Channel %s has no replay (Kodi plays the programme of %s)", asked["stream"],
                  pl["item"].get("starttime"))
        self.no_replay.add(asked["stream"])
        self.replay = None
        self.save_state()
        self.rebuild()
        if self.catalog.replay_sources(asked["shown"]):
            await self.play_replay(asked["category"], asked["shown"], asked["start"])
        else:
            await self.refresh()

    async def on_command(self, entity, cmd_id: str, params: dict | None, websocket=None):
        params = params or {}
        if self.remote is None:
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        try:
            if cmd_id == mp.Commands.PLAY_MEDIA:
                media_id = str(params.get("media_id") or "")
                replay = Catalog.parse_replay_id(media_id)
                if replay and self.catalog.channel(replay[0], replay[1]):
                    return await self.soon(self.play_replay, *replay)
                parsed = Catalog.parse_channel_id(media_id)
                if not parsed or not self.catalog.channel(*parsed):
                    return ucapi.StatusCodes.NOT_FOUND
                return await self.soon(self.play_channel, *parsed)
            if cmd_id in STEP:
                if not self.current:
                    return ucapi.StatusCodes.SERVICE_UNAVAILABLE
                ch = self.catalog.neighbour(*self.current, STEP[cmd_id])
                if not ch:
                    return ucapi.StatusCodes.NOT_FOUND
                self.current = (self.current[0], ch["id"])  # the next CH+ counts from here at once
                return await self.soon(self.play_channel, *self.current)
            if cmd_id == mp.Commands.TOGGLE:
                cmd_id = mp.Commands.OFF if self.last.get(mp.Attributes.STATE) in (
                    mp.States.PLAYING, mp.States.PAUSED, mp.States.ON) else mp.Commands.ON
            if cmd_id == mp.Commands.ON:  # Kodi on the last channel watched
                if self.current and self.catalog.channel(*self.current):
                    return await self.soon(self.play_channel, *self.current)
                return await self.soon(self.open_kodi)
            if cmd_id == mp.Commands.OFF:
                try:
                    pl = await self.kodi.player()
                    if pl:
                        await self.kodi.call("Player.Stop", {"playerid": pl["playerid"]})
                except KodiError:
                    pass
                await self.remote.command(self.shield, "media_player.off")
                await self.refresh()
                return ucapi.StatusCodes.OK
            if cmd_id in (mp.Commands.PLAY_PAUSE, mp.Commands.STOP) or cmd_id in SEEK:
                pl = await self.kodi.player()
                if not pl:
                    return ucapi.StatusCodes.OK
                if cmd_id == mp.Commands.PLAY_PAUSE:
                    await self.kodi.call("Player.PlayPause", {"playerid": pl["playerid"]})
                elif cmd_id == mp.Commands.STOP:
                    await self.kodi.call("Player.Stop", {"playerid": pl["playerid"]})
                else:
                    await self.kodi.call("Player.Seek", {"playerid": pl["playerid"], "value": {"step": SEEK[cmd_id]}})
                await self.refresh()
                return ucapi.StatusCodes.OK
        except ERRORS as ex:
            _LOG.warning("Command %s failed: %s", cmd_id, ex)
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        return ucapi.StatusCodes.NOT_IMPLEMENTED

    # ----------------------------------------------------------------- state
    def programme_attrs(self, attrs: dict, ch: dict, p: dict, position: int, duration: int, artist: str,
                        resync: bool = False) -> None:
        """Programme shown with its progress; the position is sent again only for a new programme (the remote
        moves the bar itself while playing), or every 30 s in replay where Kodi's position is the reference."""
        attrs[mp.Attributes.MEDIA_TITLE] = p["title"]
        attrs[mp.Attributes.MEDIA_ARTIST] = artist
        attrs[mp.Attributes.MEDIA_IMAGE_URL] = self.img(p.get("icon") or ch.get("logo"))
        attrs[mp.Attributes.MEDIA_DURATION] = duration
        key = (ch["id"], p["start"], position // 30 if resync else 0, attrs[mp.Attributes.STATE])
        if key != self.programme_key or mp.Attributes.MEDIA_POSITION not in self.last:
            attrs[mp.Attributes.MEDIA_POSITION] = position
            attrs[mp.Attributes.MEDIA_POSITION_UPDATED_AT] = datetime.now(timezone.utc).isoformat()
            self.programme_key = key

    def kodi_attrs(self, pl: dict | None) -> dict:
        attrs = {mp.Attributes.STATE: mp.States.ON, mp.Attributes.MEDIA_TYPE: "channel",
                 mp.Attributes.MEDIA_TITLE: "Kodi", mp.Attributes.MEDIA_ARTIST: "", mp.Attributes.MEDIA_IMAGE_URL: ""}
        no_progress = {mp.Attributes.MEDIA_DURATION: 0, mp.Attributes.MEDIA_POSITION: 0}
        if not pl:
            self.programme_key = None
            return {**attrs, **no_progress}
        item = pl["item"]
        attrs[mp.Attributes.STATE] = mp.States.PAUSED if pl["speed"] == 0 else mp.States.PLAYING
        attrs[mp.Attributes.MEDIA_TITLE] = item.get("title") or item.get("label") or ""
        found = self.by_kodi_name.get(item.get("channel") or "")
        if not found:
            self.programme_key = None
            return {**attrs, **no_progress}
        cat, ch = self.catalog.by_id[found[0]], self.catalog.channel(*found)
        if self.current != found and (not self.current or self.current[1] != found[1]):
            self.remember(*found)  # zapped in Kodi itself: CH+/- and the next start follow
        name = item.get("channel") or ch["name"]  # the version really playing ("M6 FHD" for M6 4K)
        attrs[mp.Attributes.MEDIA_TITLE] = name
        attrs[mp.Attributes.MEDIA_ARTIST] = cat["name"]
        attrs[mp.Attributes.MEDIA_IMAGE_URL] = self.img(ch.get("logo"))
        began = item_ts(item.get("starttime"))
        now = time.time()
        if item.get("type") == "channel" and began is not None:  # live: the programme on air, as Kodi sees it
            live = {"title": item.get("title") or "", "start": int(began),
                    "stop": int(item_ts(item.get("endtime")) or began), "icon": item.get("thumbnail") or ""}
            if not str(live["icon"]).startswith("http"):
                live["icon"] = ""
        else:
            live = self.guide.now(ch["id"])
        # a programme opened from the guide (not the channel) that began before the one on air: replay
        if item.get("type") != "channel" and began is not None and (not live or began < live["start"] - 60):
            progs = self.guide.programmes(ch["id"])
            p = next((x for x in progs if abs(x["start"] - began) < 120), None)
            p = p or {"title": item.get("title") or "", "start": began, "stop": item_ts(item.get("endtime")) or began}
            when = f"{day_label(p['start'], now)}{hhmm(p['start'])}–{hhmm(p['stop'])}"
            self.programme_attrs(attrs, ch, p, pl["time"], pl["total"], f"Replay · {when} · {name}", True)
        elif live:  # "Plus belle la vie" / "14:00–14:25 · TF1 FHD" with the programme image and its progress
            self.programme_attrs(attrs, ch, live, int(now - live["start"]), live["stop"] - live["start"],
                                 f"{hhmm(live['start'])}–{hhmm(live['stop'])} · {name}")
        else:
            self.programme_key = None
            attrs.update(no_progress)
        return attrs

    async def current_attrs(self) -> dict:
        a = await self.shield_state()
        self.watch_shield(a)
        state = a.get("state") or mp.States.UNKNOWN
        if state in OFF_STATES:
            return {mp.Attributes.STATE: state, mp.Attributes.MEDIA_TITLE: "", mp.Attributes.MEDIA_ARTIST: "",
                    mp.Attributes.MEDIA_IMAGE_URL: "", mp.Attributes.MEDIA_DURATION: 0, mp.Attributes.MEDIA_POSITION: 0}
        try:
            pl = await self.kodi.player() if await self.kodi.alive() else False
        except KodiError as ex:
            _LOG.debug("Kodi state unavailable: %s", ex)
            pl = False
        if pl and pl["item"].get("type") != "channel":  # replay or not: compare with the programme on air
            try:
                await self.refresh_nows()
            except KodiError:
                pass
        if pl or (pl is None and str(a.get("source") or "") in ("Kodi", "Google Play Store")):
            return self.kodi_attrs(pl)
        return {mp.Attributes.STATE: state, mp.Attributes.MEDIA_TYPE: "channel",  # another app is on screen
                mp.Attributes.MEDIA_TITLE: a.get("media_title") or a.get("source") or "",
                mp.Attributes.MEDIA_ARTIST: "", mp.Attributes.MEDIA_IMAGE_URL: a.get("media_image_url") or "",
                mp.Attributes.MEDIA_DURATION: 0, mp.Attributes.MEDIA_POSITION: 0}

    async def refresh(self) -> None:
        if self.remote is None:
            return
        try:
            attrs = await self.current_attrs()
        except ERRORS as ex:
            _LOG.debug("Shield state unavailable: %s", ex)
            attrs = {mp.Attributes.STATE: mp.States.UNAVAILABLE}
        changed = {k: v for k, v in attrs.items() if self.last.get(k) != v}
        if changed:
            self.last.update(changed)
            self.api.configured_entities.update_attributes(ENTITY_ID, changed)

    async def poll(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(POLL_S)

    def register_events(self) -> None:
        @self.api.listens_to(ucapi.Events.CONNECT)
        async def on_connect(*_):
            await self.api.set_device_state(ucapi.DeviceStates.CONNECTED)

        @self.api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
        async def on_subscribe(entity_ids, *_):
            self.last = {}
            if ENTITY_ID in entity_ids and (self.poll_task is None or self.poll_task.done()):
                self.poll_task = asyncio.create_task(self.poll())
            else:
                await self.refresh()

        @self.api.listens_to(ucapi.Events.ENTER_STANDBY)
        async def on_standby(*_):
            if self.poll_task:
                self.poll_task.cancel()
                self.poll_task = None

        @self.api.listens_to(ucapi.Events.EXIT_STANDBY)
        async def on_wake(*_):
            if self.poll_task is None or self.poll_task.done():
                self.poll_task = asyncio.create_task(self.poll())


async def main() -> None:
    logging.basicConfig(level=os.getenv("UC_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    driver = Driver(asyncio.get_running_loop())
    driver.register_events()
    cfg = driver.load_json("config.json")
    await driver.api.init("driver.json", driver.setup_handler)
    if cfg and cfg.get("api_key"):
        driver.configure(cfg)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())
    loop.run_forever()
