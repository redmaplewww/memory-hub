# -*- coding: utf-8 -*-
"""MemoryHub 基准评测（二期收尾：LOCOMO 式子集对照）。

同一份自含数据集（多会话记忆写入 → 问答召回）跑两种模式，量化后端注入的收益：
- 降级模式（零依赖）：关键词单路召回，literal 类应全过、语义/冲突类为 0——基线；
- 完整模式（注入 LLM+embedding）：同义改写向量召回、改口冲突自动 invalidate 生效。

用法：
  python -m memory_hub.bench                 # 临时库跑一轮
  python -m memory_hub.bench --db PATH --topk 5
  python -m memory_hub.bench --json          # 机器可读输出

设计说明：
- 数据集内嵌（不依赖 LOCOMO 原文件），问题按能力分类，逐类统计命中率——
  对照口径与 DESIGN-MEMORY §三的阈值校准用例同源（表格→列表改口、对账走老王等）。
- 写入后统一错开 updated_at（秒级精度无法区分同秒写入），保证降级模式
  「无命中→最近 3 条」回退的确定性，不污染语义类测量。
"""
import argparse
import json
import os
import sys
import tempfile
import uuid


def _dataset() -> dict:
    return {
        # 写入顺序即时间顺序；两个 preference 在前，后面 ≥3 条 filler
        # 使降级模式的最近 3 条回退不含语义类目标（否则会假阳性）
        "memories": [
            {"kind": "preference", "content": "汇报统一用表格格式"},
            {"kind": "preference", "content": "以后汇报改用列表格式"},
            {"kind": "fact", "content": "办公室在酒仙桥超图大厦"},
            {"kind": "person", "content": "供应商对账统一走老王处理"},
            {"kind": "fact", "content": "技术岗薪资三档 15-20K"},
            {"kind": "fact", "content": "公司成立于2022年7月"},
        ],
        "questions": [
            {"q": "办公室在哪里", "expect": "酒仙桥", "category": "literal"},
            {"q": "供应商对账找谁", "expect": "老王", "category": "literal"},
            {"q": "技术岗薪资怎么定", "expect": "三档", "category": "literal"},
            {"q": "公司哪年成立的", "expect": "2022", "category": "literal"},
            {"q": "你希望我怎么做周报", "expect": "列表", "category": "paraphrase",
             "note": "同义改写：与目标条目无共同字面词，需向量召回"},
            {"q": "现在汇报要用什么格式", "expect": "列表", "avoid": "统一用表格",
             "category": "conflict",
             "note": "改口后旧偏好应已被 invalidate——需 LLM 冲突消解，"
                     "默认召回不得再出现旧值（avoid 取旧值特征子串，"
                     "不能只用「表格」：会误杀「列表格式」本身）"},
        ],
    }


def run(db_path: str = None, topk: int = 5) -> dict:
    """跑一轮基准，返回报告 dict（含逐题明细，可 json.dumps）。"""
    path = db_path or os.path.join(
        tempfile.mkdtemp(), "bench-%s.db" % uuid.uuid4().hex[:6])
    from .core import MemoryHub
    from . import deps
    hub = MemoryHub(path)
    subject = "bench-user"
    data = _dataset()
    hub.write(data["memories"], actor="bench", subject=subject)

    # 错开时间戳：同秒写入会让「最近 N 条」回退不确定
    import sqlite3
    conn = sqlite3.connect(path)
    with conn:
        rows = conn.execute(
            "SELECT memory_id FROM memories WHERE subject=? ORDER BY rowid",
            (subject,)).fetchall()
        for i, (mid,) in enumerate(rows):
            conn.execute(
                "UPDATE memories SET created_at=?, updated_at=? WHERE memory_id=?",
                ("2026-01-01 00:00:%02d" % i, "2026-01-01 00:00:%02d" % i, mid))
    conn.close()

    if deps.llm_available() and deps.embed_available():
        mode = "full"
    elif deps.embed_available():
        mode = "vec-only"
    else:
        mode = "degraded"

    results = []
    for item in data["questions"]:
        hits = hub.recall(item["q"], subject, k=topk)
        contents = [h["content"] for h in hits]
        hit = any(item["expect"] in c for c in contents)
        clean = all(item.get("avoid", "\0") not in c for c in contents)
        results.append({"q": item["q"], "category": item["category"],
                        "ok": bool(hit and clean),
                        "hit": bool(hit), "clean": bool(clean),
                        "note": item.get("note", ""), "hits": contents})

    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], {"total": 0, "ok": 0})
        c["total"] += 1
        c["ok"] += int(r["ok"])
    return {"mode": mode, "topk": topk,
            "total": len(results), "ok": sum(r["ok"] for r in results),
            "categories": cats, "results": results, "db": path}


def _print(report: dict):
    print("模式：%s（top-%d）  命中 %d/%d" % (
        report["mode"], report["topk"], report["ok"], report["total"]))
    for cat, c in report["categories"].items():
        print("  %-10s %d/%d" % (cat, c["ok"], c["total"]))
    for r in report["results"]:
        mark = "✓" if r["ok"] else "✗"
        print("  %s [%s] %s" % (mark, r["category"], r["q"]))
        if not r["ok"]:
            note = r["note"] or ("命中：" + " | ".join(r["hits"][:3]))
            print("      %s" % note)


def main():
    ap = argparse.ArgumentParser(description="MemoryHub 基准评测")
    ap.add_argument("--db", default=None, help="留空则用临时库")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = ap.parse_args()

    report = run(db_path=args.db, topk=args.topk)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
