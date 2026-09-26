# -*- coding: utf-8 -*-
"""Memory Hub 测试用 Web 服务（纯标准库，零新依赖）。

用法：python -m memory_hub.webapp [--db PATH] [--port 8765] [--no-ollama]
启动时自动尝试接入本机 Ollama（backends_ollama），接不上则降级模式运行。
"""
import argparse
import json
import os
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import deps
from .core import MemoryHub

HUB = None
_JOBS = {}
_JOB_SEQ = [0]
_JOB_LOCK = threading.Lock()
_NO_OLLAMA = False


def _try_ollama():
    try:
        from . import backends_ollama
        backends_ollama.configure()
        return backends_ollama.llm_available(), backends_ollama.embed_available()
    except Exception:
        return False, False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _safe(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            traceback.print_exc()
            return {"error": traceback.format_exc(limit=2)}, 500

    # ---------- routing ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            self._static("webapp.html", "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._safe(self._status)
        elif u.path == "/api/memories":
            self._safe(self._list, q)
        elif u.path.startswith("/api/memory/"):
            self._safe(self._detail, u.path.split("/")[-1])
        elif u.path == "/api/profile":
            self._safe(self._profile, q)
        elif u.path == "/api/episodes":
            self._safe(self._episodes, q)
        elif u.path == "/api/bench":
            self._safe(self._bench, q)
        elif u.path == "/api/job":
            jid = int(q.get("id") or 0)
            self._json(_JOBS.get(jid) or {"state": "unknown"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        if u.path == "/api/write":
            self._safe(self._write, b)
        elif u.path == "/api/recall":
            self._safe(self._recall, b)
        elif u.path == "/api/extract":
            self._safe(self._extract, b)
        elif u.path == "/api/ingest":
            self._safe(self._ingest, b)
        elif u.path == "/api/consolidate":
            self._safe(self._consolidate, b)
        elif u.path.startswith("/api/memory/"):
            self._safe(self._op, u.path.split("/")[-1], b)
        else:
            self._json({"error": "not found"}, 404)

    # ---------- static ----------
    def _static(self, name, ctype):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            self._json({"error": "missing " + name}, 500)

    def _clean(self, obj):
        """剥掉 BLOB 等不可序列化字段（vec 等），前端用不到。"""
        if isinstance(obj, dict):
            return {k: ("<%dB blob>" % len(v)) if isinstance(v, (bytes, bytearray))
                    else self._clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._clean(x) for x in obj]
        return obj

    # ---------- api ----------
    def _status(self):
        llm, emb = (deps.llm_available(), deps.embed_available())
        self._json({"llm": llm, "embed": emb,
                    "mode": "full" if (llm and emb) else
                    ("vec-only" if emb else "degraded"),
                    "db": HUB.path})

    def _write(self, b):
        items = b.get("items") or []
        if b.get("content"):
            items = [{"content": b["content"], "kind": b.get("kind", "fact"),
                      "entities": b.get("entities") or []}]
        r = HUB.write(items, actor=b.get("actor") or "web",
                      subject=b.get("subject") or "me")
        self._json(dict(r))

    def _recall(self, b):
        flt = b.get("filters") or {}
        filters = {k: flt[k] for k in ("kind", "since", "until")
                   if flt.get(k)} or None
        hits = HUB.recall(b.get("query") or "", subject=b.get("subject") or "me",
                          k=int(b.get("k") or 5),
                          include_invalidated=bool(b.get("include_invalidated")),
                          rerank=bool(b.get("rerank")), filters=filters)
        self._json({"hits": hits})

    def _list(self, q):
        rows = HUB.list_memories(
            subject=q.get("subject") or None,
            include_invalidated=q.get("all") == "1", limit=500)
        self._json({"memories": self._clean(rows)})

    def _detail(self, mid):
        m = HUB.get_memory(mid)
        if m is None:
            return self._json({"error": "not found"}, 404)
        self._json({"memory": self._clean(m), "related": HUB.related(mid),
                    "events": HUB.events(memory_id=mid, limit=50)})

    def _op(self, op, b):
        mid = b.get("memory_id") or ""
        fn = {"edit": lambda: HUB.edit(mid, b.get("content") or "", actor="web"),
              "invalidate": lambda: HUB.invalidate(mid, actor="web",
                                                   reason=b.get("reason") or ""),
              "reactivate": lambda: HUB.reactivate(mid, actor="web"),
              "forget": lambda: HUB.forget(mid, actor="web",
                                           reason=b.get("reason") or ""),
              "attach": lambda: HUB.attach_files(
                  mid, [{"name": b.get("name") or "file.txt",
                         "content": b.get("file_content") or ""}], actor="web")}
        if op not in fn:
            return self._json({"error": "bad op"}, 400)
        self._json({"ok": fn[op]()})

    def _profile(self, q):
        self._json(HUB.profile(q.get("subject") or "me"))

    def _episodes(self, q):
        self._json({"episodes": HUB.episodes(q.get("subject") or "me", limit=50)})

    def _extract(self, b):
        from .extract import extract_items
        out = extract_items(b.get("user_msg") or "", b.get("ai_msg") or "",
                            custom_instructions=b.get("custom_instructions") or "")
        self._json({"items": out})

    def _ingest(self, b):
        from .ingest import ingest_folder
        st = ingest_folder(HUB, b.get("folder") or "", actor="web",
                           subject=b.get("subject") or "me")
        self._json(st)

    def _consolidate(self, b):
        rep = HUB.consolidate(subject=b.get("subject") or None, actor="web",
                              dry_run=bool(b.get("dry_run")))
        self._json(rep)

    def _bench(self, q):
        from . import bench
        with _JOB_LOCK:
            _JOB_SEQ[0] += 1
            jid = _JOB_SEQ[0]
        _JOBS[jid] = {"state": "running"}

        def _run():
            try:
                _JOBS[jid] = {"state": "done", "report": bench.run()}
            except Exception:
                _JOBS[jid] = {"state": "error", "error": traceback.format_exc(2)}
        threading.Thread(target=_run, daemon=True).start()
        self._json({"job": jid})


def main():
    global HUB
    ap = argparse.ArgumentParser(description="Memory Hub 测试 Web 服务")
    ap.add_argument("--db", default=os.path.join(
        deps.runtime_dir(), "memory_web.db"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-ollama", action="store_true")
    args = ap.parse_args()

    if not args.no_ollama:
        llm, emb = _try_ollama()
    else:
        llm, emb = False, False
    HUB = MemoryHub(args.db)
    mode = "full" if (llm and emb) else ("vec-only" if emb else "degraded")
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("Memory Hub Web：%s  模式：%s  库：%s" %
          ("http://127.0.0.1:%d" % args.port, mode, args.db))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
