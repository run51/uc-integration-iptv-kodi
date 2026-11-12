"""Image cache of the integration: channel logos and programme pictures kept on the Remote.

The Remote's interface keeps pictures in memory for a while only and downloads them again over Wi-Fi when
it needs them later (or after a restart). The integration keeps a small copy of each picture and serves it
from the Remote itself.

The Remote's processor is small and shared with its interface: pictures are downloaded and made smaller by
ONE background worker, one after the other, gently. A picture not cached yet is not waited for: the
interface is sent to its original address (as without the cache) and the picture is queued for later.
"""

import hashlib
import logging
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOG = logging.getLogger("titan.images")

MAX_DOWNLOAD = 3 * 1024 * 1024
MAX_SIDE = 240  # list pictures are 60 px, the player card a few hundred
USER_AGENT = "uc-intg-iptv-kodi"
PAUSE_S = 1.0  # between two downloads
BACKOFF_S = 120.0  # after "429 Too Many Requests" (imgur, which hosts most logos, limits the rate)


def tls_context() -> ssl.SSLContext:
    """Certificates of certifi: the Python packed for the Remote has no system certificate store (the logos
    are mostly on https://i.imgur.com)."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


TLS = tls_context()


def shrink(data: bytes) -> tuple[bytes, str]:
    """Picture made smaller (Pillow, when available); PNG keeps the transparency of the logos."""
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(data))
        img.draft("RGB", (MAX_SIDE, MAX_SIDE))  # JPEG: decoded directly at a reduced size
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = BytesIO()
        if img.mode in ("RGBA", "LA", "P"):
            img.convert("RGBA").save(out, "PNG", compress_level=6)
            return out.getvalue(), "image/png"
        img.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - keep the original picture when it cannot be read
        kind = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return data, kind


class ImageCache:
    def __init__(self, folder: str, max_bytes: int = 60 * 1024 * 1024) -> None:
        self.folder = folder
        self.max_bytes = max_bytes
        os.makedirs(folder, exist_ok=True)
        self.base = ""  # "http://<remote>:9300" set by the driver
        self.queue: queue.Queue[str] = queue.Queue()
        self.pending: set[str] = set()
        self.guard = threading.Lock()
        self.done = 0
        threading.Thread(target=self.worker, daemon=True, name="image-cache").start()

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha1(url.encode()).hexdigest()[:20]

    def url_for(self, url: str) -> str:
        """Address of a picture through the cache ("" and icon:// addresses unchanged)."""
        if not self.base or not url or not url.startswith(("http://", "https://")):
            return url
        return f"{self.base}/img/{self.key(url)}?u={urllib.parse.quote(url, safe='')}"

    def path(self, key: str) -> str:
        return os.path.join(self.folder, key)

    def cached(self, url: str) -> bool:
        return os.path.exists(self.path(self.key(url)))

    def get(self, url: str) -> tuple[bytes, str] | None:
        """(picture, content type) when cached; else None and the picture is queued (never waits)."""
        if not url.startswith(("http://", "https://")):
            return None
        path = self.path(self.key(url))
        if os.path.exists(path):
            try:
                os.utime(path)  # most recently used: kept when the cache is trimmed
            except OSError:
                pass
            return self.read(path)
        self.enqueue(url)
        return None

    def enqueue(self, url: str) -> None:
        with self.guard:
            if url in self.pending:
                return
            self.pending.add(url)
        self.queue.put(url)

    def prefetch(self, urls: list[str]) -> int:
        """Queue the pictures not cached yet (the channel logos, at start and at night)."""
        missing = [u for u in dict.fromkeys(urls) if u and not self.cached(u)]
        for url in missing:
            self.enqueue(url)
        return len(missing)

    def worker(self) -> None:
        while True:
            url = self.queue.get()
            try:
                if not self.cached(url):
                    refused = self.fetch(url)
                    time.sleep(BACKOFF_S if refused else PAUSE_S)
                if self.queue.empty():
                    self.trim()
                    if self.done:
                        _LOG.info("Image cache: %d pictures downloaded", self.done)
                        self.done = 0
            except Exception:  # noqa: BLE001 - the worker must keep running
                _LOG.exception("Image cache worker")
            finally:
                with self.guard:
                    self.pending.discard(url)

    def fetch(self, url: str) -> bool:
        """Download, make smaller and keep a picture; True when the site answered "too many requests"."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20, context=TLS) as resp:
                data = resp.read(MAX_DOWNLOAD + 1)
        except urllib.error.HTTPError as ex:
            _LOG.debug("Picture %s not downloaded: %s", url, ex)
            return ex.code == 429
        except (OSError, ValueError) as ex:
            _LOG.debug("Picture %s not downloaded: %s", url, ex)
            return False
        if not data or len(data) > MAX_DOWNLOAD:
            return False
        small, kind = shrink(data)
        path = self.path(self.key(url))
        tmp = path + ".tmp"
        with open(tmp, "wb") as file:
            file.write(b"P" if kind == "image/png" else b"J")
            file.write(small)
        os.replace(tmp, path)
        self.done += 1
        return False

    @staticmethod
    def read(path: str) -> tuple[bytes, str] | None:
        try:
            with open(path, "rb") as file:
                data = file.read()
        except OSError:
            return None
        return data[1:], "image/png" if data[:1] == b"P" else "image/jpeg"

    def trim(self) -> None:
        """Drop the pictures used least recently above the size limit."""
        files = []
        for name in os.listdir(self.folder):
            path = self.path(name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        total = sum(f[1] for f in files)
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
