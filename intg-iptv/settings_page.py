# ruff: noqa: E501  (the embedded HTML page keeps long lines)
"""Settings web page of the integration: import the IPTV playlist, choose the categories and channels shown.

Served on the local network: GET / the page, GET /api/catalog the full catalog with the hidden lists and the
playlist status, POST /api/hidden saves them (hidden.json in the config directory) and reloads the driver,
POST /api/m3u imports the provider's M3U playlist (body = the file). GET /kodi.m3u and /epg.xml.gz: the
playlist and guide for Kodi, only to the allowed addresses (the stream addresses carry the provider login).
"""

import email.utils
import json
import logging
import os
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from catalog import is_4k
from epg import base_name

_LOG = logging.getLogger("titan.settings")

PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IPTV — réglages</title>
<style>
:root{--bg:#0f1115;--panel:#181b22;--line:#2a2f3a;--fg:#e8eaf0;--mute:#9aa3b2;--accent:#4da6ff;--off:#5b6270}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:14px max(16px,env(safe-area-inset-left))}
h1{margin:0 0 8px;font-size:20px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=search]{flex:1;min-width:200px;padding:9px 12px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--fg)}
.stat{color:var(--mute);font-size:13px}.saved{color:var(--accent);font-size:13px;min-width:90px}
main{padding:12px max(16px,env(safe-area-inset-left)) 40px;max-width:1100px;margin:auto}
.cat{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden}
.cat.hidden{opacity:.55}.head{display:flex;align-items:center;gap:10px;padding:10px 12px;cursor:pointer}
.head .name{flex:1;font-weight:600}.head .count{color:var(--mute);font-size:13px}
.tools{display:flex;gap:8px;padding:0 12px 8px}.tools button{background:none;border:1px solid var(--line);color:var(--mute);border-radius:6px;padding:4px 10px;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:6px;padding:0 12px 12px}
.ch{display:flex;align-items:center;gap:10px;padding:8px 10px;min-height:52px;border-radius:8px;border:1px solid var(--line);cursor:pointer;min-width:0}
.ch.off{opacity:.4}.ch img{width:56px;height:38px;object-fit:contain;background:#000;border-radius:4px;flex:none}
.ch span{overflow-wrap:anywhere;font-size:14px;line-height:1.3}
input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent);flex:none}
.chev{color:var(--mute);width:16px}
.opt{display:flex;gap:8px;align-items:center;margin-top:8px;color:var(--mute);font-size:14px;cursor:pointer}
.ch.auto{opacity:.4}.ch .why{display:block;color:var(--mute);font-size:12px}
.imp{margin-top:8px;color:var(--mute);font-size:14px}.btn{border:1px solid var(--line);border-radius:6px;padding:5px 12px;cursor:pointer;color:var(--fg)}
</style></head><body>
<header><h1>IPTV — chaînes affichées sur la remote et dans Kodi</h1>
<div class="row"><input type="search" id="q" placeholder="Rechercher une chaîne ou une catégorie…">
<span class="stat" id="stat"></span><span class="saved" id="saved"></span></div>
<div class="row imp"><span id="pl">Playlist : …</span><label class="btn">Importer la playlist (.m3u)<input type="file" id="m3u" accept=".m3u,.m3u8" hidden></label></div>
<label class="opt"><input type="checkbox" id="p4k"> Garder seulement la version 4K d'une chaîne quand elle existe (les autres versions servent encore au replay)</label></header>
<main id="list"></main>
<script>
let cat=[],hidden={categories:[],channels:{},prefer_4k:true},open=new Set(),timer=null;
const $=s=>document.querySelector(s);
const fold=t=>(t||"").normalize("NFD").replace(/[̀-ͯ]/g,"").toLowerCase();
function isCatHidden(id){return hidden.categories.includes(id)}
function isChHidden(c,id){return (hidden.channels[c]||[]).includes(id)}
function autoHidden(c,h){if(!hidden.prefer_4k||h.uhd)return false;  // another version of the channel is shown in 4K
 return c.channels.some(x=>x.uhd&&x.key===h.key&&!isChHidden(c.id,x.id))}
function save(){$("#saved").textContent="…";clearTimeout(timer);timer=setTimeout(async()=>{
 const r=await fetch("api/hidden",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(hidden)});
 $("#saved").textContent=r.ok?"Enregistré ✓":"Erreur";stat()},400)}
function stat(){let shown=0,tot=0;for(const c of cat){tot+=c.channels.length;if(!isCatHidden(c.id))shown+=c.channels.filter(h=>!isChHidden(c.id,h.id)&&!autoHidden(c,h)).length}
 $("#stat").textContent=`${cat.length-hidden.categories.length}/${cat.length} catégories · ${shown}/${tot} chaînes affichées`}
function setCh(c,id,show){const l=new Set(hidden.channels[c]||[]);show?l.delete(id):l.add(id);hidden.channels[c]=[...l];if(!hidden.channels[c].length)delete hidden.channels[c]}
function render(){const q=fold($("#q").value);const L=$("#list");L.innerHTML="";
 for(const c of cat){const catMatch=fold(c.name).includes(q);const chs=q&&!catMatch?c.channels.filter(h=>fold(h.name).includes(q)):c.channels;
  if(q&&!catMatch&&!chs.length)continue;
  const d=document.createElement("section");d.className="cat"+(isCatHidden(c.id)?" hidden":"");
  const vis=c.channels.filter(h=>!isChHidden(c.id,h.id)).length;const isOpen=open.has(c.id)||(q&&!catMatch);
  d.innerHTML=`<div class="head"><input type="checkbox" ${isCatHidden(c.id)?"":"checked"} title="Afficher la catégorie"><span class="chev">${isOpen?"▾":"▸"}</span><span class="name"></span><span class="count">${vis}/${c.channels.length}</span></div>`;
  d.querySelector(".name").textContent=c.name;
  const cb=d.querySelector("input");cb.onclick=e=>{e.stopPropagation();const s=new Set(hidden.categories);cb.checked?s.delete(c.id):s.add(c.id);hidden.categories=[...s];save();render()};
  d.querySelector(".head").onclick=()=>{open.has(c.id)?open.delete(c.id):open.add(c.id);render()};
  if(isOpen){const t=document.createElement("div");t.className="tools";
   t.innerHTML='<button>Tout cocher</button><button>Tout décocher</button>';
   const [a,b]=t.querySelectorAll("button");a.onclick=()=>{for(const h of chs)setCh(c.id,h.id,true);save();render()};b.onclick=()=>{for(const h of chs)setCh(c.id,h.id,false);save();render()};
   d.appendChild(t);const g=document.createElement("div");g.className="grid";
   for(const h of chs){const off=isChHidden(c.id,h.id);const auto=!off&&autoHidden(c,h);const e=document.createElement("label");e.className="ch"+(off?" off":"")+(auto?" auto":"");
    e.innerHTML=`<input type="checkbox" ${off?"":"checked"}><img loading="lazy" alt=""><span></span>`;
    e.querySelector("span").textContent=h.name;if(auto){const w=document.createElement("small");w.className="why";w.textContent="Masquée : version 4K affichée";e.querySelector("span").appendChild(w)}e.title=h.name;if(h.logo)e.querySelector("img").src=h.logo;else e.querySelector("img").style.visibility="hidden";
    e.querySelector("input").onchange=ev=>{setCh(c.id,h.id,ev.target.checked);e.classList.toggle("off",!ev.target.checked);save();
     d.querySelector(".count").textContent=c.channels.filter(x=>!isChHidden(c.id,x.id)).length+"/"+c.channels.length};
    g.appendChild(e)}d.appendChild(g)}
  L.appendChild(d)}stat()}
$("#q").oninput=render;
$("#p4k").onchange=e=>{hidden.prefer_4k=e.target.checked;save();render()};
function showPlaylist(p){$("#pl").textContent=p&&p.channels?`Playlist : ${p.channels} chaînes en direct, importée le ${p.imported||"?"}`:"Playlist : aucune, importe le fichier M3U de ton fournisseur"}
$("#m3u").onchange=async e=>{const f=e.target.files[0];if(!f)return;$("#pl").textContent="Import en cours…";
 const r=await fetch("api/m3u",{method:"POST",body:f});const j=await r.json().catch(()=>({}));
 if(!r.ok){$("#pl").textContent="Import refusé : "+(j.error||r.status);return}load()};
function load(){fetch("api/catalog").then(r=>r.json()).then(j=>{cat=j.catalog.categories;hidden=Object.assign({categories:[],channels:{},prefer_4k:true},j.hidden);$("#p4k").checked=hidden.prefer_4k!==false;showPlaylist(j.playlist);render()})}
load();
</script></body></html>"""


MAX_PLAYLIST = 300 * 1024 * 1024


def start(port: int, get_catalog, get_hidden, save_hidden, import_m3u=None, feed=None, allowed=None,
          playlist=None, image=None) -> ThreadingHTTPServer:
    """Serve the settings page in a background thread.

    import_m3u(path) -> {"channels": n} | raises ValueError; feed(path) -> (bytes, content type[, mtime]) | None;
    allowed(ip) -> bool for the Kodi files; playlist() -> {"channels", "imported"}."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            try:
                self._write(code, body, ctype, headers)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the interface gave up a picture (list scrolled away)

        def _write(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path.startswith("/img/") and image:  # picture through the image cache (?u=original address)
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                original = query.get("u", [""])[0]
                found = image(original)
                if found is None and original.startswith(("http://", "https://")):
                    # not cached yet (queued): the interface gets it from its original address, at once
                    self._send(302, b"", "text/plain", {"Location": original, "Cache-Control": "no-store"})
                elif found is None:
                    self._send(404, b"not found", "text/plain")
                else:
                    self._send(200, found[0], found[1], {"Cache-Control": "max-age=31536000, immutable"})
                return
            if path in ("/kodi.m3u", "/epg.xml.gz") and feed:
                if allowed and not allowed(self.client_address[0]):
                    _LOG.warning("Refused %s %s", self.client_address[0], path)
                    self._send(403, b"forbidden", "text/plain")
                    return
                found = feed(path)
                if found is None:
                    self._send(503, b"not ready", "text/plain")
                    return
                body, ctype, *mtime = found
                if mtime:  # the guide (8 MB) is sent again only when it changed
                    since = self.headers.get("If-Modified-Since")
                    try:
                        unchanged = since and email.utils.parsedate_to_datetime(since).timestamp() >= int(mtime[0])
                    except (TypeError, ValueError):
                        unchanged = False
                    if unchanged:
                        self.send_response(304)
                        self.end_headers()
                        return
                    self._send(200, body, ctype, {"Last-Modified": email.utils.formatdate(mtime[0], usegmt=True)})
                    return
                self._send(200, body, ctype)
                return
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/catalog":
                catalog = {"categories": [
                    {**cat, "channels": [{**ch, "key": base_name(ch["name"]), "uhd": is_4k(ch["name"])}
                                         for ch in cat["channels"]]}
                    for cat in (get_catalog() or {}).get("categories", [])]}
                body = json.dumps({"catalog": catalog, "hidden": get_hidden(),
                                   "playlist": playlist() if playlist else None}, ensure_ascii=False)
                self._send(200, body.encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def import_playlist(self) -> None:
            """The M3U file is written to a temporary file as it comes (it can be ~40 MB), then read."""
            length = int(self.headers.get("Content-Length") or 0)
            if not import_m3u or not 0 < length <= MAX_PLAYLIST:
                self._send(400, json.dumps({"error": "fichier absent ou trop gros"}).encode(), "application/json")
                return
            fd, tmp = tempfile.mkstemp(suffix=".m3u")
            try:
                with os.fdopen(fd, "wb") as out:
                    left = length
                    while left > 0:
                        chunk = self.rfile.read(min(left, 65536))
                        if not chunk:
                            break
                        out.write(chunk)
                        left -= len(chunk)
                result = import_m3u(tmp)
                self._send(200, json.dumps(result).encode(), "application/json")
            except ValueError as ex:
                self._send(400, json.dumps({"error": str(ex)}, ensure_ascii=False).encode(), "application/json")
            finally:
                os.unlink(tmp)

        def do_POST(self):  # noqa: N802
            if self.path == "/api/m3u":
                self.import_playlist()
                return
            if self.path != "/api/hidden":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(length, 2_000_000)) or b"{}")
                hidden = {
                    "categories": [str(c) for c in data.get("categories", [])],
                    "channels": {str(k): [int(x) for x in v] for k, v in (data.get("channels") or {}).items()},
                    "prefer_4k": bool(data.get("prefer_4k", True)),
                }
            except (ValueError, TypeError, AttributeError):
                self._send(400, b"bad request", "text/plain")
                return
            save_hidden(hidden)
            self._send(200, b"{}", "application/json")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="settings-page").start()
    _LOG.info("Settings page on port %d", port)
    return server
