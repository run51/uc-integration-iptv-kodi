# ruff: noqa: E501  (the embedded HTML page keeps long lines)
"""Settings web page of the integration: choose the categories and channels shown on the remote.

Served on the local network (port 9097 by default): GET / the page, GET /api/catalog the full catalog with
the hidden lists, POST /api/hidden saves them (hidden.json in the config directory) and reloads the driver.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOG = logging.getLogger("titan.settings")

PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Titan Player — chaînes</title>
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
</style></head><body>
<header><h1>Titan Player — chaînes affichées sur la remote</h1>
<div class="row"><input type="search" id="q" placeholder="Rechercher une chaîne ou une catégorie…">
<span class="stat" id="stat"></span><span class="saved" id="saved"></span></div></header>
<main id="list"></main>
<script>
let cat=[],hidden={categories:[],channels:{}},open=new Set(),timer=null;
const $=s=>document.querySelector(s);
const fold=t=>(t||"").normalize("NFD").replace(/[̀-ͯ]/g,"").toLowerCase();
function isCatHidden(id){return hidden.categories.includes(id)}
function isChHidden(c,id){return (hidden.channels[c]||[]).includes(id)}
function save(){$("#saved").textContent="…";clearTimeout(timer);timer=setTimeout(async()=>{
 const r=await fetch("api/hidden",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(hidden)});
 $("#saved").textContent=r.ok?"Enregistré ✓":"Erreur";stat()},400)}
function stat(){let shown=0,tot=0;for(const c of cat){tot+=c.channels.length;if(!isCatHidden(c.id))shown+=c.channels.filter(h=>!isChHidden(c.id,h.id)).length}
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
   for(const h of chs){const off=isChHidden(c.id,h.id);const e=document.createElement("label");e.className="ch"+(off?" off":"");
    e.innerHTML=`<input type="checkbox" ${off?"":"checked"}><img loading="lazy" alt=""><span></span>`;
    e.querySelector("span").textContent=h.name;e.title=h.name;if(h.logo)e.querySelector("img").src=h.logo;else e.querySelector("img").style.visibility="hidden";
    e.querySelector("input").onchange=ev=>{setCh(c.id,h.id,ev.target.checked);e.classList.toggle("off",!ev.target.checked);save();
     d.querySelector(".count").textContent=c.channels.filter(x=>!isChHidden(c.id,x.id)).length+"/"+c.channels.length};
    g.appendChild(e)}d.appendChild(g)}
  L.appendChild(d)}stat()}
$("#q").oninput=render;
fetch("api/catalog").then(r=>r.json()).then(j=>{cat=j.catalog.categories;hidden=Object.assign({categories:[],channels:{}},j.hidden);render()});
</script></body></html>"""


def start(port: int, get_catalog, get_hidden, save_hidden) -> ThreadingHTTPServer:
    """Serve the settings page in a background thread."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/catalog":
                body = json.dumps({"catalog": get_catalog(), "hidden": get_hidden()}, ensure_ascii=False)
                self._send(200, body.encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path != "/api/hidden":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(length, 2_000_000)) or b"{}")
                hidden = {
                    "categories": [str(c) for c in data.get("categories", [])],
                    "channels": {str(k): [int(x) for x in v] for k, v in (data.get("channels") or {}).items()},
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
