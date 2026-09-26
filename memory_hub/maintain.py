# -*- coding: utf-8 -*-
"""MemoryHub 维护 CLI（offline 通道）。

用法：
  python -m memory_hub.maintain                        # TTL 清扫 + 补嵌
  python -m memory_hub.maintain --db PATH              # 指定库
  python -m memory_hub.maintain --consolidate          # 追加 sleep-time 存量整理
  python -m memory_hub.maintain --consolidate --dry-run --subject u1   # 只报告不动库
"""
import argparse
import os
import sys

from . import deps


def main():
    ap = argparse.ArgumentParser(description="MemoryHub 维护")
    ap.add_argument("--db", default=os.path.join(deps.runtime_dir(), "dialogue.db"))
    ap.add_argument("--consolidate", action="store_true",
                    help="sleep-time 存量整理（判重合并 + 冲突消解补扫）")
    ap.add_argument("--dry-run", action="store_true",
                    help="仅配合 --consolidate：只报告，不改库")
    ap.add_argument("--subject", default=None,
                    help="仅整理指定 subject（默认全部）")
    ap.add_argument("--namespace", default="dialogue")
    args = ap.parse_args()

    from .core import MemoryHub
    hub = MemoryHub(args.db)
    out = hub.maintain(actor="maintain-cli")
    print("维护完成：TTL 过期失效 %d 条 / 向量补嵌 %d 条" % (
        out["expired"], out["reembedded"]))
    if args.consolidate:
        rep = hub.consolidate(namespace=args.namespace, subject=args.subject,
                              actor="maintain-cli", dry_run=args.dry_run)
        print("sleep-time 整理%s：%d 组 %d 条扫描 / 判重合并 %d / 冲突消解 %d / "
              "LLM 裁决 %d" % ("（dry-run 仅报告）" if args.dry_run else "",
                               rep["groups"], rep["scanned"], rep["merged"],
                               rep["conflicts"], rep["llm_judged"]))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
