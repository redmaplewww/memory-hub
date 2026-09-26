# -*- coding: utf-8 -*-
"""观点创作知识库重建：按篇切分 + 清洗 + 细粒度块 + DeepSeek 逐篇观点蒸馏。

处理「我的笔记语料.md」（85 篇小红书笔记拼接）：
- 按篇（--- 分隔）解析：标题 / 互动数据 / 正文；
- 清洗：去话题标签行、元数据块、多余空白；
- 层1 细块：正文按 240 字切（kind=fact，带篇名前缀）；
- 层2 蒸馏：每篇过 DeepSeek 提取观点/判断/方法论（kind=decision/preference/procedure）；
- 层3 数据：每篇互动数据一条（kind=fact，看哪类内容表现好）。

用法：python rebuild_creation.py [--db creation.db] [--no-llm]
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_hub import MemoryHub, deps, backend_local

backend_local.install()

SRC = r"G:/BaiduSyncdisk/Zcode/自媒体运营团队/xiaohongshu-ops-team/content/corpus/我的笔记语料.md"

DISTILL_SYSTEM = """你是观点蒸馏器。从一篇小红书笔记正文中提取作者（红枫）的**观点、判断和可复用方法论**。

只提取：
- preference：作者明确表达的喜好/立场/态度（对工具、产品、现象的看法）
- decision：作者的判断性结论（怎么看好某事、得出的教训）
- procedure：可复用的做法/技巧/流程

纪律：
1. 只提取正文明文表达的内容，禁止推断。
2. 每条 ≤80 字、独立可读、保留作者口吻中的信息量。
3. 正文是纯记录/无观点时输出空数组。最多 4 条。
4. entities：涉及的工具/产品/主题（1-3 个短词）。
输出 JSON：{"memories": [{"kind": "preference", "content": "...", "entities": []}]}"""

HASHTAG = re.compile(r"#[^\s#]\S*(?:\[话题\]#)?")


def parse_posts(text):
    posts = []
    for part in re.split(r"\n---+\n", text):
        m = re.search(r"^## (.+)$", part, re.M)
        if not m:
            continue
        title = m.group(1).strip()
        meta, body = "", part[m.end():]
        mb = re.match(r"\s*((?:- .+\n)+)", body)
        if mb:
            meta = mb.group(1)
            body = body[mb.end():]
        body = body.replace("**正文**", "")
        posts.append((title, meta, body.strip()))
    return posts


def clean_body(body):
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if s.startswith("#") and ("[话题]" in s or re.fullmatch(
                r"(#[^\s#]+\s*)+", s)):
            continue                      # 话题标签行
        lines.append(s)
    out = "\n".join(lines)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def fine_chunks(body, size=240):
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    out, buf = [], ""
    for p in paras:
        if buf and len(buf) + len(p) + 1 > size:
            out.append(buf)
            buf = ""
        if len(p) > size:                 # 超长段再切
            while len(p) > size:
                cut = p.rfind("。", 0, size)
                cut = cut + 1 if cut > size // 2 else size
                out.append(p[:cut].strip())
                p = p[cut:].strip()
        buf = (buf + "\n" + p).strip() if buf else p
    if buf:
        out.append(buf)
    return out


def distill(title, body):
    if len(body) < 30:
        return []
    for _ in range(2):
        content = deps.llm_chat(
            [{"role": "system", "content": DISTILL_SYSTEM},
             {"role": "user", "content": "标题：%s\n\n正文：\n%s" % (
                 title, body[:6000])}],
            temperature=0.0, max_tokens=1500)
        if content:
            data = deps.parse_json(content)
            if data and isinstance(data.get("memories"), list):
                return data["memories"]
        time.sleep(1)
    return []


def parse_meta(meta):
    d = {}
    for ln in meta.splitlines():
        m = re.match(r"- (发布|互动|标签)：(.+)", ln.strip())
        if m:
            d[m.group(1)] = m.group(2).strip()
    return d


CTX_SYSTEM = """你是上下文标注器（Anthropic Contextual Retrieval 法）。
给定一篇笔记的概要和它的若干正文块，为每块写一句「上下文前缀」，说明该块出自哪里、
在讲什么，使块脱离原文后仍可独立理解。前缀 ≤40 字，不用重复块内已有信息。
输出 JSON：{"contexts": ["前缀1", "前缀2", ...]}（数量与块一一对应）"""


def context_prefixes(title, meta, body, chunks):
    """块上下文前缀：LLM 可用时逐块生成（Anthropic 法）；
    不可用时降级为确定性前缀（标签+日期，来自篇元数据）。"""
    md = parse_meta(meta)
    tags = md.get("标签", "")
    tags = re.sub(r"[#\s]", "", tags)[:30]
    date = (md.get("发布", "") or "")[:10]
    fallback = "·".join(x for x in (date, tags) if x)
    if len(chunks) <= 1 or not deps.llm_available():
        return [fallback] * len(chunks)
    gist = body[:300]
    listing = "\n".join("[%d] %s" % (i, c[:120]) for i, c in enumerate(chunks))
    content = deps.llm_chat(
        [{"role": "system", "content": CTX_SYSTEM},
         {"role": "user", "content": "标题：%s\n%s\n概要：%s\n\n块：\n%s" % (
             title, meta, gist, listing)}],
        temperature=0.0, max_tokens=1200)
    data = deps.parse_json(content) if content else None
    ctxs = (data or {}).get("contexts") if isinstance(data, dict) else None
    if isinstance(ctxs, list) and len(ctxs) == len(chunks):
        return [(str(c)[:40] or fallback) for c in ctxs]
    return [fallback] * len(chunks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="creation2.db")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    text = open(SRC, encoding="utf-8").read()
    posts = parse_posts(text)
    print("解析 %d 篇笔记" % len(posts))
    hub = MemoryHub(args.db)
    if os.path.exists(args.db):
        pass                                # 全新库直接写
    n_body = n_distill = n_meta = 0
    for i, (title, meta, body) in enumerate(posts, 1):
        body = clean_body(body)
        md = parse_meta(meta)
        items = []
        fcs = fine_chunks(body)
        for c, ctx in zip(fcs, context_prefixes(title, meta, body, fcs)):
            prefix = "《%s》%s" % (title, "〔%s〕" % ctx if ctx else "")
            items.append({"kind": "fact",
                          "content": prefix + c,
                          "source_session": title})
        if md.get("互动"):
            items.append({"kind": "fact",
                          "content": "笔记《%s》数据：%s ｜ %s ｜ 标签：%s" % (
                              title, md.get("发布", "?"), md["互动"],
                              md.get("标签", "?")),
                          "source_session": title})
        if not args.no_llm:
            for m in distill(title, body):
                if not isinstance(m, dict):
                    continue
                kind = m.get("kind") if m.get("kind") in (
                    "preference", "decision", "procedure") else "preference"
                ctext = str(m.get("content", "")).strip()[:160]
                if len(ctext) < 4:
                    continue
                items.append({"kind": kind, "content": ctext,
                              "entities": [str(e)[:20] for e in
                                           (m.get("entities") or [])][:3],
                              "source_session": title})
        r = hub.write(items, actor="zzg", namespace="creation",
                      subject="views", source_session=title)
        n_body += sum(1 for a in r["added"] if a["kind"] == "fact")
        n_distill += sum(1 for a in r["added"]
                         if a["kind"] in ("preference", "decision",
                                          "procedure"))
        n_meta += 0                    # 互动数据已并入 fact 计数
        if i % 10 == 0:
            print("  %d/%d 篇…（累计细块 %d / 观点 %d）" % (
                i, len(posts), n_body, n_distill))
    print("完成：细块 %d / 互动数据 %d 合计 fact / 蒸馏观点 %d → %s" % (
        n_body, n_meta, n_distill, args.db))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
