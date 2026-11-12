"""Image cache of the integration: channel logos and programme pictures kept on the Remote.

The Remote's interface keeps pictures in memory for a while only and downloads them again over Wi-Fi when
it needs them later (or after a restart). The integration downloads each picture once, makes it small (the
lists show 60 px pictures, the player card a few hundred) and serves it from the Remote itself.
"""

import hashlib
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOG = logging.getLogger("titan.images")

MAX_DOWNLOAD = 3 * 1024 * 1024
MAX_SIDE = 320  # the player card is ~400 px wide; list pictures are 60 px
USER_AGENT = "uc-intg-iptv-kodi"


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
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = BytesIO()
        if img.mode in ("RGBA", "LA", "P"):
            img.convert("RGBA").save(out, "PNG", optimize=True)
            return out.getvalue(), "image/png"
        img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - keep the original picture when it cannot be read
        kind = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return data, kind


class ImageCache:
    def __init__(self, folder: str, max_bytes: int = 60 * 1024 * 1024) -> None:
        self.folder = folder
        self.max_bytes = max_bytes
        os.makedirs(folder, exist_ok=True)
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()
        self.base = ""  # "http://<remote>:9300" set by the driver
        self.refused_at = 0.0  # last "429 Too Many Requests" (imgur, which hosts most logos, limits the rate)

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
        """(picture, content type), downloaded and made smaller the first time."""
        if not url.startswith(("http://", "https://")):
            return None
        key = self.key(url)
        path = self.path(key)
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            if os.path.exists(path):
                os.utime(path)  # most recently used: kept when the cache is trimmed
                return self.read(path)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20, context=TLS) as resp:
                    data = resp.read(MAX_DOWNLOAD + 1)
            except urllib.error.HTTPError as ex:
                if ex.code == 429:
                    self.refused_at = time.monotonic()
                _LOG.debug("Picture %s not downloaded: %s", url, ex)
                return None
            except (OSError, ValueError) as ex:
                _LOG.debug("Picture %s not downloaded: %s", url, ex)
                return None
            if not data or len(data) > MAX_DOWNLOAD:
                return None
            small, kind = shrink(data)
            tmp = path + ".tmp"
            with open(tmp, "wb") as file:
                file.write(b"P" if kind == "image/png" else b"J")
                file.write(small)
            os.replace(tmp, path)
        return small, kind

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

    def prefetch(self, urls: list[str], pause: float = 1.5, backoff: float = 120.0) -> int:
        """Download the pictures not cached yet, gently (called at night for the channel logos); after a
        "too many requests" answer, wait a while before going on."""
        done = 0
        for url in urls:
            if url and not self.cached(url):
                refused = self.refused_at
                if self.get(url):
                    done += 1
                elif self.refused_at != refused:
                    time.sleep(backoff)
                time.sleep(pause)
        self.trim()
        return done
