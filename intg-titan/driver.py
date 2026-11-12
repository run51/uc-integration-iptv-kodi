"""Titan Player integration for the Unfolded Circle Remote: browse the IPTV channels by category, with
their logos, and play one on the Android TV box (NVIDIA Shield) running Titan Player.

Chain: Remote → this driver → the Remote's own Core REST API → the Android TV integration's media player
       → select_source "iptvtitan://channel/<account>/<stream>" → Titan Player opens the live channel.

The channel catalog (categories, names, stream ids, logos; no login) is built from the provider's M3U
playlist by tools/build_catalog.py and read from catalog.json (config directory, else next to the driver).
"""

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import epg
import settings_page
import ucapi
from catalog import Catalog, hhmm
from ucapi import media_player as mp

_LOG = logging.getLogger("titan")
ENTITY_ID = "titan_player"
DEFAULT_SHIELD = "uc_androidtv_driver.main.48B02D36F2DF"
DEFAULT_LIMIT, MAX_LIMIT = 50, 100
POLL_S = 5
EPG_REFRESH_S = 6 * 3600
EPG_RETRY_S = 15 * 60
TITAN_APP = "titan"  # the Android TV player names the open app "Titan Player - IPTV"

FEATURES = [
    mp.Features.ON_OFF,
    mp.Features.TOGGLE,
    mp.Features.PLAY_PAUSE,
    mp.Features.STOP,
    mp.Features.NEXT,
    mp.Features.PREVIOUS,
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
FORWARD = {  # commands passed on to the Shield's media player as they are
    mp.Commands.ON: "media_player.on",
    mp.Commands.OFF: "media_player.off",
    mp.Commands.TOGGLE: "media_player.toggle",
    mp.Commands.PLAY_PAUSE: "media_player.play_pause",
    mp.Commands.STOP: "media_player.stop",
}
STEP = {mp.Commands.NEXT: 1, mp.Commands.CHANNEL_UP: 1, mp.Commands.PREVIOUS: -1, mp.Commands.CHANNEL_DOWN: -1}


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


def page_bounds(paging) -> tuple[int, int]:
    page = max(1, int(getattr(paging, "page", 1) or 1))
    limit = min(MAX_LIMIT, max(1, int(getattr(paging, "limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)))
    return page, limit


class TitanPlayer(mp.MediaPlayer):
    def __init__(self, driver: "Driver"):
        super().__init__(
            ENTITY_ID,
            {"en": "Titan Player", "fr": "Titan Player"},
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
        self.account = 1
        self.catalog = Catalog({})
        self.current: tuple[str, int] | None = None  # (category id, stream id) last played
        self.last: dict = {}
        self.poll_task: asyncio.Task | None = None
        self.raw_catalog: dict = {}
        self.hidden: dict = {}
        self.settings_server = None
        self.guide: epg.Guide | None = None
        self.epg_task: asyncio.Task | None = None
        self.programme_key = None  # (stream, start) of the programme whose position was last sent

    def rebuild(self) -> None:
        self.catalog = Catalog(self.raw_catalog, self.hidden, self.guide)

    async def load_guide(self, download: bool = True) -> None:
        """Download the public guide (or read the cached copy) and keep the programmes of the catalog."""
        raw = None
        if download:
            for url in epg.SOURCES:
                try:
                    raw = await asyncio.to_thread(
                        lambda u=url: urllib.request.urlopen(urllib.request.Request(u, headers={
                            "User-Agent": "uc-intg-titan-player"}), timeout=120).read())
                    with open(self.path("epg.xml.gz"), "wb") as file:
                        file.write(raw)
                    break
                except (OSError, ValueError, urllib.error.URLError) as ex:
                    _LOG.warning("Guide download %s failed: %s", url, ex)
        if raw is None:
            try:
                with open(self.path("epg.xml.gz"), "rb") as file:
                    raw = file.read()
            except OSError:
                return
        channels = Catalog(self.raw_catalog).channels()

        def work():
            names, _ = epg.parse(raw, set())
            ids = epg.match_ids(channels, names)
            _, progs = epg.parse(raw, set(ids.values()))
            return epg.Guide(progs, ids)

        self.guide = await asyncio.to_thread(work)
        self.rebuild()
        count = sum(len(v) for v in self.guide.progs.values())
        _LOG.info("Guide: %d channels, %d programmes", len(self.guide.ids), count)

    async def guide_loop(self) -> None:
        await self.load_guide(download=False)  # cached copy first: browsing has the guide at once
        while True:
            try:
                await self.load_guide()
                await asyncio.sleep(EPG_REFRESH_S)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001 - keep the loop alive
                _LOG.warning("Guide refresh failed: %s", ex)
                await asyncio.sleep(EPG_RETRY_S)

    def save_hidden(self, hidden: dict) -> None:
        """Called from the settings page thread: store the hidden lists and rebuild the browsed catalog."""
        with open(self.path("hidden.json"), "w", encoding="utf-8") as file:
            json.dump(hidden, file)
        self.hidden = hidden
        self.rebuild()
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
        self.account = int(cfg.get("account") or 1)
        self.raw_catalog = self.load_json("catalog.json") or {}
        self.hidden = self.load_json("hidden.json") or {}
        self.rebuild()
        if self.epg_task is None or self.epg_task.done():
            self.epg_task = asyncio.get_event_loop().create_task(self.guide_loop())
        if self.settings_server is None:
            port = int(os.getenv("TITAN_SETTINGS_PORT", "9097"))
            try:
                self.settings_server = settings_page.start(
                    port, lambda: self.raw_catalog, lambda: self.hidden, self.save_hidden)
            except OSError as ex:
                _LOG.warning("Settings page not started on port %d: %s", port, ex)
        state = self.load_json("state.json") or {}
        if state.get("category") and state.get("stream"):
            self.current = (str(state["category"]), int(state["stream"]))
        if not self.api.available_entities.contains(ENTITY_ID):
            self.api.available_entities.add(TitanPlayer(self))
        _LOG.info("Remote %s, Shield %s, %d categories", self.remote.base, self.shield, len(self.catalog.categories))

    async def setup_handler(self, msg: ucapi.SetupDriver) -> ucapi.SetupAction:
        if isinstance(msg, ucapi.DriverSetupRequest):
            data = msg.setup_data
            cfg = {
                "remote_url": (data.get("remote_url") or "http://127.0.0.1").strip(),
                "api_key": (data.get("api_key") or "").strip(),
                "shield": (data.get("shield") or DEFAULT_SHIELD).strip(),
                "account": int(float(data.get("account") or 1)),
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
        try:
            container, items = self.catalog.browse(options.media_id)
        except KeyError:
            return ucapi.StatusCodes.NOT_FOUND
        page, limit = page_bounds(options.paging)
        container.items = items[(page - 1) * limit : page * limit]
        return mp.BrowseResults(media=container, pagination=ucapi.Pagination(page=page, limit=limit, count=len(items)))

    async def search(self, options: mp.SearchOptions):
        items = self.catalog.search(options.query)
        page, limit = page_bounds(options.paging)
        return mp.SearchResults(media=items[(page - 1) * limit : page * limit],
                                pagination=ucapi.Pagination(page=page, limit=limit, count=len(items)))

    # -------------------------------------------------------------- commands
    async def play_channel(self, cat_id: str, stream_id: int) -> ucapi.StatusCodes:
        link = f"iptvtitan://channel/{self.account}/{stream_id}"
        _LOG.info("Play %s (%s)", link, cat_id)
        await self.remote.command(self.shield, "media_player.select_source", {"source": link})
        self.current = (cat_id, stream_id)
        try:
            with open(self.path("state.json"), "w", encoding="utf-8") as file:
                json.dump({"category": cat_id, "stream": stream_id}, file)
        except OSError:
            pass
        await self.refresh()
        return ucapi.StatusCodes.OK

    async def on_command(self, entity, cmd_id: str, params: dict | None, websocket=None):
        params = params or {}
        if self.remote is None:
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        try:
            if cmd_id == mp.Commands.PLAY_MEDIA:
                parsed = Catalog.parse_channel_id(str(params.get("media_id") or ""))
                if not parsed or not self.catalog.channel(*parsed):
                    return ucapi.StatusCodes.NOT_FOUND
                return await self.play_channel(*parsed)
            if cmd_id in STEP:
                if not self.current:
                    return ucapi.StatusCodes.SERVICE_UNAVAILABLE
                ch = self.catalog.neighbour(*self.current, STEP[cmd_id])
                return await self.play_channel(self.current[0], ch["id"]) if ch else ucapi.StatusCodes.NOT_FOUND
            if cmd_id in FORWARD:
                await self.remote.command(self.shield, FORWARD[cmd_id])
                await self.refresh()
                return ucapi.StatusCodes.OK
        except (OSError, ValueError, urllib.error.URLError) as ex:
            _LOG.warning("Command %s failed: %s", cmd_id, ex)
            return ucapi.StatusCodes.SERVICE_UNAVAILABLE
        return ucapi.StatusCodes.NOT_IMPLEMENTED

    # ----------------------------------------------------------------- state
    def attributes(self, shield: dict) -> dict:
        a = shield.get("attributes") or {}
        state = a.get("state") or mp.States.UNKNOWN
        app = a.get("media_title") or ""
        attrs = {mp.Attributes.STATE: state, mp.Attributes.MEDIA_TYPE: "channel",
                 mp.Attributes.MEDIA_TITLE: app, mp.Attributes.MEDIA_ARTIST: "",
                 mp.Attributes.MEDIA_IMAGE_URL: a.get("media_image_url") or ""}
        in_titan = TITAN_APP in app.lower() or TITAN_APP in str(a.get("source") or "").lower()
        if in_titan and self.current and state not in ("OFF", "STANDBY", "UNAVAILABLE"):
            cat = self.catalog.by_id.get(self.current[0])
            ch = self.catalog.channel(*self.current)
            if cat and ch:
                attrs[mp.Attributes.MEDIA_TITLE] = ch["name"]
                attrs[mp.Attributes.MEDIA_ARTIST] = cat["name"]
                attrs[mp.Attributes.MEDIA_IMAGE_URL] = ch.get("logo") or attrs[mp.Attributes.MEDIA_IMAGE_URL]
                attrs[mp.Attributes.STATE] = mp.States.PLAYING if state in ("ON", "PLAYING") else state
                p = self.guide.now(ch["id"]) if self.guide else None
                if p:  # "Plus belle la vie" / "TF1 FHD · 14:00–14:25" with the programme image and its progress
                    attrs[mp.Attributes.MEDIA_TITLE] = p["title"]
                    attrs[mp.Attributes.MEDIA_ARTIST] = f"{hhmm(p['start'])}–{hhmm(p['stop'])} · {ch['name']}"
                    attrs[mp.Attributes.MEDIA_IMAGE_URL] = p.get("icon") or attrs[mp.Attributes.MEDIA_IMAGE_URL]
                    attrs[mp.Attributes.MEDIA_DURATION] = p["stop"] - p["start"]
                    key = (ch["id"], p["start"])
                    if key != self.programme_key or mp.Attributes.MEDIA_POSITION not in self.last:
                        attrs[mp.Attributes.MEDIA_POSITION] = int(time.time() - p["start"])
                        attrs[mp.Attributes.MEDIA_POSITION_UPDATED_AT] = datetime.now(timezone.utc).isoformat()
                        self.programme_key = key
                else:
                    attrs[mp.Attributes.MEDIA_DURATION] = 0
                    attrs[mp.Attributes.MEDIA_POSITION] = 0
                    self.programme_key = None
        return attrs

    async def refresh(self) -> None:
        if self.remote is None:
            return
        try:
            attrs = self.attributes(await self.remote.entity(self.shield))
        except (OSError, ValueError, urllib.error.URLError) as ex:
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
