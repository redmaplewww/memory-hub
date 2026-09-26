# -*- coding: utf-8 -*-
"""从 ZCode 历史 session 构建个人知识网络。

流程：rollout/*.jsonl → 抽取对话精华（用户消息 + 助手结论）→
DeepSeek 蒸馏成结构化知识点（kind/entities）→ 真向量写入本地知识库
（namespace=zcode-history）→ 每会话附原始摘要文件（溯源）。

用法：python build_knowledge.py [--db kb.db] [--max-chars 8000] [--dry-run]
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_hub import MemoryHub, deps, backend_local

backend_local.install()

ROLLOUT = os.path.expanduser(r"~\.zcode\cli\rollout")

DISTILL_SYSTEM = """你是个人知识蒸馏器。从一段 ZCode 编程会话记录中提取值得长期记住的知识点。

只提取以下类别：
- fact：项目/环境/账号等稳定事实（项目位置、技术栈、架构、部署形态）
- decision：会话中做出的决策与原因（选型、阈值、设计取舍）
- procedure：可复用的做法（流程、命令、踩坑解法）
- project：项目关键节点（完成了什么、达到什么状态）

纪律：
1. 只提取记录中明文出现的信息，禁止推断。
2. 每条 ≤120 字，独立可读（「XX 项目的 YY」而非「这个项目」）。
3. entities：该条涉及的项目名/工具/模块（2-4 个短词，供知识网络关联）。
4. 去掉一次性调试细节。最多 12 条，挑最有长期价值的。
输出 JSON：{"memories": [{"kind": "fact", "content": "...", "entities": ["..."]}]}"""


def load_sessions():
    out = []
    for fn in sorted(glob.glob(os.path.join(ROLLOUT, "model-io-sess_*.jsonl"))):
        sid = os.path.basename(fn).replace("model-io-", "").replace(".jsonl", "")
        turns = []
        with open(fn, encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                req = obj.get("request", {})
                ts = (obj.get("completedAt") or "")[:10]
                for m in req.get("messages", []):
                    if m.get("role") != "user":
                        continue
                    c = m.get("content")
                    txt = " ".join(x.get("text", "") for x in c
                                   if isinstance(x, dict) and x.get("type") == "text") \
                        if isinstance(c, list) else str(c)
                    txt = txt.strip()
                    # 过滤工具结果/系统注入/命令包装
                    if not txt or txt.startswith("<") or txt.startswith("[{"):
                        continue
                    if "system-reminder" in txt[:80] or txt.startswith("<command"):
                        continue
                    turns.append((ts, txt[:600]))
                # 助手最终结论（每请求的 text 尾部）
                resp = obj.get("response") or {}
                at = (resp.get("text") or "").strip()
                if at and len(at) > 80:
                    turns.append((ts, "结论：" + at[-700:]))
        if turns:
            out.append({"sid": sid, "turns": turns})
    return out


def digest(session, max_chars):
    """会话转录压缩：去重相邻、截断到上限。"""
    seen, parts = set(), []
    for ts, t in session["turns"]:
        key = t[:60]
        if key in seen:
            continue
        seen.add(key)
        parts.append("- %s" % t)
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（截断）"
    return text


def distill(session_text):
    for attempt in range(2):            # 偶发超时/截断重试一次
        content = deps.llm_chat(
            [{"role": "system", "content": DISTILL_SYSTEM},
             {"role": "user", "content": session_text[:12000]}],
            temperature=0.0, max_tokens=3000)
        if not content:
            continue
        data = deps.parse_json(content)
        if data and isinstance(data.get("memories"), list):
            break
        time.sleep(2)
    else:
        return []
    out = []
    for m in (data or {}).get("memories", []):
        if not isinstance(m, dict):
            continue
        kind = m.get("kind") if m.get("kind") in (
            "fact", "decision", "procedure", "project") else "fact"
        ctext = str(m.get("content", "")).strip()[:200]
        if len(ctext) < 4:
            continue
        ents = [str(e).strip()[:30] for e in (m.get("entities") or [])
                if str(e).strip()][:4]
        out.append({"kind": kind, "content": ctext, "entities": ents})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="kb.db")
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    hub = MemoryHub(args.db)
    sessions = load_sessions()
    print("发现 %d 个历史 session：%s" % (
        len(sessions), ", ".join(s["sid"][:16] for s in sessions)))
    total = 0
    for s in sessions:
        dg = digest(s, args.max_chars)
        print("\n=== %s（%d 轮，摘要 %d 字）===" % (s["sid"][:16],
                                                    len(s["turns"]), len(dg)))
        items = distill(dg)
        for it in items:
            print("  [%s] %s%s" % (it["kind"], it["content"][:70],
                                   "  {%s}" % ",".join(it["entities"])
                                   if it["entities"] else ""))
        if not args.dry_run and items:
            # 溯源条目：带上会话开头摘录，避免模板句式被判重丢附件
            head = dg.split("\n")[0][:60]
            r = hub.write(
                items + [{"kind": "procedure",
                          "content": "ZCode 会话溯源 %s：%d 轮对话，开头：「%s」" % (
                              s["sid"][:16], len(s["turns"]), head),
                          "files": [{"name": "session-%s.md" % s["sid"][:8],
                                     "content": dg}]}],
                actor="zcode-history", namespace="zcode-history",
                subject="zzg", source_session=s["sid"][:8])
            total += len(r["added"])
            print("  → 入库 %d 条（去重 %d）/ 摘要挂载" % (
                len(r["added"]), len(r["deduped"])))
        elif not args.dry_run:
            print("  → 蒸馏为空，跳过")
    if not args.dry_run:
        print("\n知识网络构建完成：本批入库 %d 条 → %s（namespace=zcode-history）"
              % (total, args.db))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
