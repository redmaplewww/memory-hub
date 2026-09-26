# -*- coding: utf-8 -*-
"""把 ZCode session 历史喂进记忆库（真实语料采集）。

用法：
    python -m memory_hub.feed_sessions [--db PATH] [--dir 工作区路径关键字]
                                       [--max-chunks 80] [--max-batches 8]

两路写入：
1) 对话正文切块 → kind=fact，subject=session-log（可检索"当时怎么做的"）；
   批量补嵌向量（不走 LLM 裁决，避免长耗时）。
2) 用户消息批量交 LLM 提炼持久记忆（偏好/决定/事实）→ subject=me（默认视图可见）。

只读打开 ZCode 会话库，不干扰正在运行的 ZCode。
"""
import argparse
import json
import os
import sqlite3
import sys

from . import deps
from .core import MemoryHub

ZDB = os.path.join(os.path.expanduser("~"), ".zcode", "cli", "db", "db.sqlite")

INSIGHT_SYSTEM = """你是长期记忆提炼器。下面是同一项目的一位用户多轮对话中的发言。
提炼 0~4 条值得长期记住的、关于该用户或其项目的信息。

类别：preference(偏好/习惯/工作方式要求) / fact(稳定事实) / decision(拍板决定) /
project(项目关键信息) / person(人物关系) / procedure(可复用做法)
纪律：只提炼明文出现的信息；不要提炼一次性的指令、寒暄、代码细节；
每条 ≤60 字；给 entities（人名/项目名等实体，≤3 个）。
输出 JSON：{"memories": [{"kind": "preference", "content": "...", "entities": []}]}
"""


def _text_parts(conn, session_id):
    """按顺序取 (role, text) —— 只取 text 片段，跳过模型切换/工具等。"""
    rows = conn.execute("""
        SELECT m.data AS mdata, p.data AS pdata
        FROM message m JOIN part p ON p.message_id = m.id
        WHERE m.session_id = ? ORDER BY m.sequence, p.sequence""",
        (session_id,)).fetchall()
    out = []
    for r in rows:
        try:
            md = json.loads(r["mdata"])
            pd = json.loads(r["pdata"])
        except Exception:
            continue
        if pd.get("type") != "text" or not (pd.get("text") or "").strip():
            continue
        role = md.get("role") or "user"
        if role not in ("user", "assistant"):
            continue
        out.append((role, pd["text"]))
    return out


def _chunks(text, size=400):
    out, buf = [], ""
    for para in [p.strip() for p in text.split("\n") if p.strip()]:
        if len(buf) + len(para) + 1 > size and buf:
            out.append(buf)
            buf = ""
        buf = (buf + "\n" + para).strip() if buf else para
        while len(buf) > size:
            out.append(buf[:size])
            buf = buf[size:]
    if buf:
        out.append(buf)
    return out


def _extract_insights(text_batch):
    content = deps.llm_chat(
        [{"role": "system", "content": INSIGHT_SYSTEM},
         {"role": "user", "content": "用户发言摘录：\n" + text_batch}],
        temperature=0.0)
    data = deps.parse_json(content) if content else None
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        return []
    out = []
    for m in data["memories"]:
        if not isinstance(m, dict):
            continue
        c = str(m.get("content", "")).strip()[:120]
        if 2 <= len(c):
            item = {"kind": m.get("kind") if m.get("kind") in
                    ("preference", "fact", "decision", "project", "person",
                     "procedure") else "fact", "content": c}
            ents = [str(x).strip()[:30] for x in (m.get("entities") or [])
                    if str(x).strip()][:3]
            if ents:
                item["entities"] = ents
            out.append(item)
    return out


def feed(hub: MemoryHub, zdb: str, dir_filter: str, db_path: str,
         max_chunks: int = 80, max_batches: int = 8,
         subject_log: str = "session-log", subject_insight: str = "me") -> dict:
    conn = sqlite3.connect("file:%s?mode=ro" % zdb.replace("\\", "/"),
                           uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        "SELECT id, title, time_created FROM session WHERE directory LIKE ? "
        "ORDER BY time_created", ("%" + dir_filter + "%",)).fetchall()

    stat = {"sessions": 0, "chunks": 0, "insights": 0, "batches": 0,
            "details": []}
    for s in sessions:
        parts = _text_parts(conn, s["id"])
        if not parts:
            continue
        stat["sessions"] += 1
        title = (s["title"] or s["id"][:16])[:40]
        tag = "【session:%s】" % title

        # ---- 1) 对话正文切块（embed_now=False，稍后批量补嵌）----
        transcript = "\n".join("%s：%s" % ("用户" if r == "user" else "AI", t)
                               for r, t in parts)
        n_here = 0
        for c in _chunks(transcript):
            if stat["chunks"] >= max_chunks:
                break
            hub.write([{"kind": "fact", "content": (tag + c)[:500]}],
                      actor="session-feed", subject=subject_log,
                      source_session="zcode:" + title, embed_now=False)
            stat["chunks"] += 1
            n_here += 1

        # ---- 2) 用户消息批量提炼持久记忆（LLM）----
        users = [t for r, t in parts if r == "user" and len(t.strip()) > 4]
        batch, batches = "", 0
        for u in users + [None]:
            if u is not None:
                batch = (batch + "\n- " + u.strip()[:300]) if batch else "- " + u.strip()[:300]
            if (u is None and batch) or len(batch) > 900:
                if stat["batches"] >= max_batches:
                    batch = ""
                    continue
                items = _extract_insights(batch)
                stat["batches"] += 1
                if items:
                    hub.write(items, actor="session-feed",
                              subject=subject_insight,
                              source_session="zcode:" + title, embed_now=False)
                    stat["insights"] += len(items)
                batch, batches = "", batches + 1
        hub.add_episode(subject_insight,
                        "session《%s》：%d 段对话，正文入库 %d 块"
                        % (title, len(parts), n_here),
                        session_id="zcode:" + s["id"][:20])
        stat["details"].append({"session": s["id"][:20], "title": title,
                                "messages": len(parts), "chunks": n_here})

    conn.close()

    # ---- 批量补嵌向量（一次 embed 批调用，不触发 LLM 裁决）----
    c2 = sqlite3.connect(db_path)
    with c2:
        c2.execute("UPDATE memories SET vec_stale=1 WHERE subject IN (?,?)",
                   (subject_log, subject_insight))
    c2.close()
    st = hub.maintain()
    stat["reembedded"] = st["reembedded"]
    return stat


def main():
    ap = argparse.ArgumentParser(description="ZCode session 历史 → 记忆库")
    ap.add_argument("--db", default=os.path.join(
        deps.runtime_dir(), "memory_web.db"))
    ap.add_argument("--zdb", default=ZDB)
    ap.add_argument("--dir", default="记忆模块", help="工作区路径关键字")
    ap.add_argument("--max-chunks", type=int, default=80)
    ap.add_argument("--max-batches", type=int, default=8)
    args = ap.parse_args()

    try:
        from . import backends_ollama
        backends_ollama.configure()
    except Exception:
        pass
    hub = MemoryHub(args.db)
    st = feed(hub, args.zdb, args.dir, args.db, args.max_chunks, args.max_batches)
    print("采集完成：%d 个 session" % st["sessions"])
    for d in st["details"]:
        print("  《%s》%d 段对话 → %d 块" % (d["title"], d["messages"], d["chunks"]))
    print("对话正文 %d 块 / 提炼记忆 %d 条（%d 批 LLM）/ 补嵌向量 %d 条"
          % (st["chunks"], st["insights"], st["batches"], st["reembedded"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
