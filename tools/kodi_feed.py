"""Playlist and TV guide of the chosen IPTV channels for Kodi (PVR IPTV Simple Client).

Serves two files over HTTP, rebuilt when the channel choice changes and every 6 hours:
  /kodi.m3u     the channels left visible on the settings page, in the catalog order, numbered, with the guide id
                (tvg-id) of the public guide and Xtream catch-up enabled (catchup="xc"), then the hidden
                versions of these channels (FHD of a 4K channel) kept for the replay, in their own group;
  /epg.xml.gz   the public XMLTV guide (xmltvfr.fr) reduced to these channels, past programmes included.

The stream addresses carry the provider credentials: they are read from a private file (never logged) and the
files are only served to the allowed addresses (the Shield).

Usage:
  python kodi_feed.py --data /data --catalog /data/catalog.json --hidden /config/hidden.json \
      --port 9100 --allow 192.168.2.25
/data/kodi_feed.json = {"stream_base": "http://host:port/user/pass"}   (chmod 600)
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import epg
from catalog import kodi_lineup
from feed import build_m3u

_LOG = logging.getLogger("kodi_feed")
REFRESH_S = 6 * 3600


def reduce_guide(raw: bytes, wanted: set[str]) -> bytes:
    """The XMLTV file with only the wanted channels and their programmes, gzipped."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    out = io.BytesIO()
    out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="titan-player kodi_feed">\n')
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if el.tag == "channel":
            if el.get("id") in wanted:
                out.write(ET.tostring(el, encoding="utf-8"))
            el.clear()
        elif el.tag == "programme":
            if el.get("channel") in wanted:
                out.write(ET.tostring(el, encoding="utf-8"))
            el.clear()
    out.write(b"</tv>\n")
    return gzip.compress(out.getvalue())


class Feed:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.lock = threading.Lock()
        self.m3u = b""
        self.guide = b""
        self.stamp: tuple = ()
        self.built_at = 0.0
        with open(os.path.join(args.data, "kodi_feed.json"), encoding="utf-8") as file:
            self.stream_base = json.load(file)["stream_base"]

    def _inputs_stamp(self) -> tuple:
        return tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (self.args.catalog, self.args.hidden))

    def _guide_raw(self) -> bytes:
        cache = os.path.join(self.args.data, "xmltv_source.xml.gz")
        fresh = os.path.exists(cache) and time.time() - os.path.getmtime(cache) < REFRESH_S
        if not fresh:
            for url in epg.SOURCES:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "uc-intg-titan-player kodi-feed"})
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        data = resp.read()
                    with open(cache, "wb") as file:
                        file.write(data)
                    break
                except OSError as ex:
                    _LOG.warning("Guide download %s failed: %s", url, ex)
        try:
            with open(cache, "rb") as file:
                return file.read()
        except OSError:
            return b""

    def build(self) -> None:
        with open(self.args.catalog, encoding="utf-8") as file:
            data = json.load(file)
        hidden = {}
        if os.path.exists(self.args.hidden):
            with open(self.args.hidden, encoding="utf-8") as file:
                hidden = json.load(file)
        lineup = kodi_lineup(data, hidden)
        channels = [ch for _, ch in lineup]
        raw = self._guide_raw()
        ids = epg.match_ids(channels, epg.parse(raw, set())[0]) if raw else {}
        m3u = build_m3u(lineup, ids, self.stream_base, self.args.epg_url).encode()
        guide = reduce_guide(raw, set(ids.values())) if raw else self.guide
        with self.lock:
            self.m3u, self.guide = m3u, guide
            self.stamp, self.built_at = self._inputs_stamp(), time.time()
        _LOG.info("Built: %d channels (%d with a guide), guide %d kB", len(channels), len(ids), len(guide) // 1024)

    def ensure(self) -> None:
        if self._inputs_stamp() != self.stamp or time.time() - self.built_at > REFRESH_S:
            try:
                self.build()
            except Exception:  # noqa: BLE001 - keep serving the previous files
                _LOG.exception("Build failed")


def serve(feed: Feed) -> None:
    allow = set(feed.args.allow)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            client = self.client_address[0]
            if allow and client not in allow:
                _LOG.warning("Refused %s %s", client, self.path)
                self.send_error(403)
                return
            feed.ensure()
            path = self.path.split("?")[0]
            if path == "/kodi.m3u":
                body, ctype = feed.m3u, "audio/x-mpegurl; charset=utf-8"
            elif path == "/epg.xml.gz":
                body, ctype = feed.guide, "application/gzip"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            _LOG.info("Served %s to %s", path, client)

        def log_message(self, *args) -> None:  # the default log would print nothing secret, but keep it quiet
            pass

    ThreadingHTTPServer(("0.0.0.0", feed.args.port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="/data")
    parser.add_argument("--catalog", default="/data/catalog.json")
    parser.add_argument("--hidden", default="/config/hidden.json")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--allow", nargs="*", default=[])
    parser.add_argument("--epg-url", default="")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    feed = Feed(args)
    feed.build()

    def refresher() -> None:
        while True:
            time.sleep(600)
            feed.ensure()

    threading.Thread(target=refresher, daemon=True).start()
    serve(feed)


if __name__ == "__main__":
    main()
