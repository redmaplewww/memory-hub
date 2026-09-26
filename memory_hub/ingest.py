# -*- coding: utf-8 -*-
"""本地知识库入库 + 问答 CLI（产品入口）。

把本地文件夹（md/txt/markdown）导入 Memory Hub 成为可召回的本地知识库：
  python -m memory_hub.ingest FOLDER --db kb.db --subject me
  python -m memory_hub.ingest ask "问题" --db kb.db --subject me
  python -m memory_hub.ingest stats --db kb.db

入库策略（确定性，零 LLM 依赖）：
- 按「标题/空行」切块（chunk ≤400 字，块前缀带上文件名与标题便于溯源）；
- 每块写一条 kind=fact 条目（source_session=相对路径）；
- 每个文件另挂一条 kind=procedure 溯源条目，附原件全文（≤64KB，超限不附）；
- 重复导入幂等（相同内容块走 substring/余弦去重）。
"""
import argparse
import os
import sys

CHUNK_SIZE = 400
TEXT_EXTS = (".md", ".markdown", ".txt")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", "runtime"}


def _chunks(text: str, chunk_size: int = CHUNK_SIZE) -> list:
    """按空行分段，超长段再切；保留段落完整性优先。"""
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 > chunk_size and buf:
            out.append(buf)
            buf = p[:chunk_size]
            p = p[chunk_size:]
        buf = (buf + "\n" + p).strip() if buf else p
        while len(buf) > chunk_size:
            out.append(buf[:chunk_size])
            buf = buf[chunk_size:]
    if buf:
        out.append(buf)
    return out


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if os.path.splitext(fn)[1].lower() in TEXT_EXTS:
                yield os.path.join(dirpath, fn)


def ingest_folder(hub, folder: str, subject: str, namespace: str = "dialogue",
                  actor: str = "ingest") -> dict:
    """整个文件夹入库。返回 {files, chunks, deduped, files_attached}。"""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        return {"files": 0, "chunks": 0, "deduped": 0, "files_attached": 0}
    stats = {"files": 0, "chunks": 0, "deduped": 0, "files_attached": 0}
    for path in _iter_files(folder):
        rel = os.path.relpath(path, folder).replace("\\", "/")
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        if not text.strip():
            continue
        stats["files"] += 1
        chunks = _chunks(text)
        proc_content = "来源档案：%s（%d 字，%d 块）" % (rel, len(text), len(chunks))
        # 文档更新消解（按块内容全等比对，精确无误伤）：
        # 旧块不在新块集合 → 下架（"文档更新"留痕）；完全相同 → 核心 exact 去重。
        # 不用子串启发式——真实文档同文件内短块常是另一长块的子串（重复表格行等）。
        existing = [m for m in hub.list_memories(
            subject=subject, namespace=namespace, limit=10 ** 6)
            if m.get("source_session") == rel and m.get("status") == "active"]
        # 集合比对口径须与核心 write 一致（strip + 500 字截断），否则截断块
        # 会被误判为"已更新"而下架重加
        new_set = {("【%s】%s" % (rel, c)).strip()[:500] for c in chunks}
        for om in existing:
            if om["kind"] == "fact" and om["content"] not in new_set:
                hub.invalidate(om["memory_id"], actor=actor,
                               reason="文档更新：块被新版取代")
            elif om["kind"] == "procedure" and om["content"] != proc_content:
                hub.invalidate(om["memory_id"], actor=actor,
                               reason="文档更新：旧档案索引被取代")
        items = [{"kind": "fact", "content": "【%s】%s" % (rel, c),
                  "source_session": rel} for c in chunks]
        # 溯源条目：文件级 procedure + 原件挂载（超 64KB 跳过附件）
        if len(text.encode("utf-8")) <= 64 * 1024:
            items.append({"kind": "procedure",
                          "content": "来源档案：%s（%d 字，%d 块）" % (
                              rel, len(text), len(chunks)),
                          "source_session": rel,
                          "files": [{"name": os.path.basename(rel),
                                     "content": text}]})
        r = hub.write(items, actor=actor, namespace=namespace, subject=subject)
        stats["chunks"] += len(r["added"])
        stats["deduped"] += len(r["deduped"])
        stats["files_attached"] += sum(1 for a in r["added"] if a["file_ids"])
    return stats


def _hub(db):
    from .core import MemoryHub
    return MemoryHub(db)


def main():
    ap = argparse.ArgumentParser(description="本地知识库：文件入库 / 问答 / 统计")
    ap.add_argument("cmd", choices=["ingest", "ask", "stats"],
                    help="ingest=文件夹入库  ask=问答  stats=统计")
    ap.add_argument("target", nargs="?", help="ingest: 文件夹路径；ask: 问题")
    ap.add_argument("--db", default=os.path.join("runtime", "kb.db"))
    ap.add_argument("--subject", default="local-kb")
    ap.add_argument("--namespace", default="dialogue")
    ap.add_argument("--topk", type=int, default=5)
    args = ap.parse_args()

    if args.cmd == "ingest":
        if not args.target:
            print("用法：ingest 需要文件夹路径")
            return 2
        hub = _hub(args.db)
        st = ingest_folder(hub, args.target, subject=args.subject,
                           namespace=args.namespace)
        print("入库完成：%d 个文件 / 新增 %d 块 / 去重 %d 块 / 挂载原件 %d 个"
              % (st["files"], st["chunks"], st["deduped"], st["files_attached"]))
        print("库：%s（subject=%s）" % (args.db, args.subject))
    elif args.cmd == "ask":
        if not args.target:
            print("用法：ask 需要问题")
            return 2
        hub = _hub(args.db)
        hits = hub.recall(args.target, subject=args.subject,
                          namespace=args.namespace, k=args.topk)
        if not hits:
            print("（无相关记忆）")
            return 0
        for i, h in enumerate(hits, 1):
            print("%d. [%s] %s" % (i, h["kind"], h["content"][:120]))
            if h.get("file_ids"):
                f = hub.read_memory_file(h["file_ids"][0])
                if f:
                    print("   附件：%s（%d 字）" % (f["name"], len(f["content"])))
    else:
        hub = _hub(args.db)
        rows = hub.list_memories(subject=args.subject,
                                 namespace=args.namespace, limit=10 ** 6)
        active = sum(1 for m in rows if m["status"] == "active")
        eps = hub.episodes(args.subject, namespace=args.namespace, limit=10 ** 6)
        print("条目 %d（active %d）/ episodes %d / 库 %s"
              % (len(rows), active, len(eps), args.db))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
