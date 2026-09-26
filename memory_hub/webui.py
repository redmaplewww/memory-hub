# -*- coding: utf-8 -*-
"""Memory Hub 本地知识库 Web UI（零依赖，Python 标准库）。

用法：
  python -m memory_hub.webui --db kb.db --subject zzg --namespace zcode-history
  # 可选 --no-backend：降级模式（不加载 bge/DeepSeek）
浏览器打开 http://127.0.0.1:8765
"""
import argparse
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>本地知识库 · Memory Hub</title>
<style>
:root{--bg:#0f1117;--card:#171a23;--line:#262b38;--fg:#e8eaf0;--dim:#8b93a7;
--acc:#6c8cff;--ok:#4cc38a;--warn:#e5a13d;--bad:#e05555}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:14px/1.7 "Segoe UI",system-ui,sans-serif;padding:28px}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:20px;font-weight:600}
h1 small{color:var(--dim);font-weight:400;margin-left:10px;font-size:13px}
.search{display:flex;gap:10px;margin:18px 0 6px}
input[type=text]{flex:1;background:var(--card);border:1px solid var(--line);color:var(--fg);
padding:11px 14px;border-radius:9px;font-size:15px;outline:none}
input[type=text]:focus{border-color:var(--acc)}
button{background:var(--acc);border:0;color:#fff;padding:11px 22px;border-radius:9px;
cursor:pointer;font-size:14px}
button.ghost{background:var(--card);border:1px solid var(--line);color:var(--dim)}
.meta{color:var(--dim);font-size:12px;margin-bottom:16px}
.kinds{display:flex;gap:8px;margin:14px 0;flex-wrap:wrap}
.kinds button{padding:5px 14px;font-size:13px}
.kind-tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;margin-right:8px}
.k-fact{background:#1d3a5f;color:#7fb3ff}.k-decision{background:#3a2f1d;color:#e5c07d}
.k-procedure{background:#1d3f33;color:#6fd3a0}.k-project{background:#3a1d2f;color:#e07fb0}
.k-preference{background:#2a1d3f;color:#b39fff}.k-person{background:#1d2a3f;color:#7fd4ff}
.ent{display:inline-block;background:var(--card);border:1px solid var(--line);color:var(--dim);
font-size:11px;padding:0 7px;border-radius:9px;margin:0 4px 4px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;
padding:14px 16px;margin-bottom:10px}
.card .body{margin-top:6px;font-size:14px}
.card .foot{margin-top:8px;color:var(--dim);font-size:12px}
.st-active{color:var(--ok)}.st-invalidated{color:var(--bad);text-decoration:line-through}
.st-archived{color:var(--warn)}
a{color:var(--acc);text-decoration:none;cursor:pointer}
pre.digest{background:#10131b;border:1px solid var(--line);border-radius:9px;
padding:12px;white-space:pre-wrap;font-size:12.5px;max-height:420px;overflow:auto;margin-top:8px}
.qa{border-left:3px solid var(--acc);padding-left:12px;color:var(--dim);margin-bottom:14px;font-size:13px}
.empty{color:var(--dim);text-align:center;padding:50px 0}
</style></head><body><div class="wrap">
<h1>📚 本地知识库 <small id="stats"></small></h1>
<div class="search">
  <input id="q" type="text" placeholder="语义搜索：例如「怎么部署到 Cloudflare」" autofocus>
  <button onclick="doSearch()">搜索</button>
  <button class="ghost" onclick="loadList()">全部知识</button>
</div>
<div class="kinds" id="kinds"></div>
<div class="meta" id="meta">回车搜索 ｜ 关键词+向量+实体三路融合召回</div>
<div id="list"><div class="empty">加载中…</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const KCLS={fact:'k-fact',decision:'k-decision',procedure:'k-procedure',
project:'k-project',preference:'k-preference',person:'k-person'};
async function get(url){const r=await fetch(url);return r.json()}
function entHtml(m){try{return JSON.parse(m.entities||'[]')
 .map(e=>'<span class="ent">'+e+'</span>').join('')}catch(e){return ''}}
function card(m){
 let f='';
 if(m.file_ids){try{f=' ｜ <a onclick="showFile(\\''+JSON.parse(m.file_ids)[0]+'\\')">📎 溯源摘要</a>'}catch(e){}}
 return '<div class="card"><div><span class="kind-tag '+(KCLS[m.kind]||'k-fact')+'">'+m.kind+'</span>'
 +'<span class="st-'+m.status+'">'+m.content+'</span></div>'
 +(m.entities?'<div style="margin-top:6px">'+entHtml(m)+'</div>':'')
 +'<div class="foot">'+(m.source_session||'')+' · '+(m.updated_at||'')+f+'</div></div>'}
async function loadStats(){const s=await get('/api/stats');
 $('stats').textContent=s.active+' 条知识 · '+s.kinds.join(' · ')+(s.backend?' · 完整模式':' · 降级模式')}
async function loadList(kind){
 const url='/api/list'+(kind?'?kind='+kind:'');
 const d=await get(url);$('list').innerHTML=d.items.map(card).join('')||
 '<div class="empty">（空）</div>';
 $('meta').textContent='共 '+d.items.length+' 条'}
async function doSearch(){
 const q=$('q').value.trim();if(!q)return loadList();
 const d=await get('/api/search?q='+encodeURIComponent(q));
 $('list').innerHTML='<div class="qa">🔍 '+q+' → '+d.hits.length+' 条命中</div>'
  +d.hits.map(card).join('')||'';}
async function showFile(fid){
 const d=await get('/api/file?id='+fid);
 $('list').innerHTML='<div class="qa">📎 '+d.name+'</div><pre class="digest">'
  +d.content.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</pre>'}
$('q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});
(async()=>{await loadStats();
 const d=await get('/api/list');const ks={};d.items.forEach(m=>ks[m.kind]=(ks[m.kind]||0)+1);
 $('kinds').innerHTML='<button class="ghost" onclick="loadList()">全部</button>'
  +Object.keys(ks).map(k=>'<button class="ghost" onclick="loadList(\\''+k+'\\')">'
   +k+' '+ks[k]+'</button>').join('');
 loadList()})()
</script></body></html>"""


def make_handler(hub, subject, namespace, has_backend):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            try:
                if u.path == "/":
                    b = _PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                elif u.path == "/api/search":
                    q = (qs.get("q") or [""])[0]
                    self._json({"hits": hub.recall(q, subject=subject,
                                                    namespace=namespace, k=10)})
                elif u.path == "/api/list":
                    rows = hub.list_memories(subject=subject,
                                             namespace=namespace,
                                             include_invalidated=False, limit=500)
                    kind = (qs.get("kind") or [""])[0]
                    if kind:
                        rows = [m for m in rows if m["kind"] == kind]
                    for m in rows:
                        m.pop("vec", None)
                    self._json({"items": rows})
                elif u.path == "/api/file":
                    f = hub.read_memory_file((qs.get("id") or [""])[0])
                    self._json(f or {"name": "未找到", "content": ""})
                elif u.path == "/api/stats":
                    rows = hub.list_memories(subject=subject,
                                             namespace=namespace, limit=10 ** 6)
                    active = [m for m in rows if m["status"] == "active"]
                    from collections import Counter
                    self._json({
                        "active": len(active),
                        "kinds": ["%s %d" % kv for kv in
                                  Counter(m["kind"] for m in active).items()],
                        "backend": has_backend})
                else:
                    self.send_error(404)
            except Exception as e:
                self._json({"error": repr(e)}, 500)

    return H


def main():
    ap = argparse.ArgumentParser(description="本地知识库 Web UI")
    ap.add_argument("--db", default="kb.db")
    ap.add_argument("--subject", default="zzg")
    ap.add_argument("--namespace", default="zcode-history")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-backend", action="store_true",
                    help="降级模式：不加载 bge/DeepSeek")
    args = ap.parse_args()

    has_backend = False
    if not args.no_backend:
        try:
            from . import backend_local
            backend_local.install()
            from . import deps
            has_backend = deps.llm_available() or deps.embed_available()
        except Exception:
            pass
    from .core import MemoryHub
    hub = MemoryHub(args.db)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port),
                              make_handler(hub, args.subject, args.namespace,
                                           has_backend))
    print("本地知识库 Web UI → http://127.0.0.1:%d（Ctrl+C 停止）"
          % args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
