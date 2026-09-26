# -*- coding: utf-8 -*-
"""演示数据种子：给测试库灌一批覆盖全部功能的记忆数据。

用法：python -m memory_hub.seed_demo [--db PATH]
（写入时会尽量绕开慢速 LLM 裁决：相近内容用程序化失效+挂链代替）
"""
import argparse
import os
import sqlite3
import sys
import uuid

from . import deps
from .core import MemoryHub


def seed(db_path: str) -> dict:
    hub = MemoryHub(db_path)
    stats = {"subjects": {}, "episodes": 0}

    def _set_supersede(old_mid, new_mid):
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute("UPDATE memories SET superseded_by=? WHERE memory_id=?",
                         (new_mid, old_mid))
        conn.close()

    # ---------- subject = me（网页默认用户）----------
    S = "me"
    r = hub.write([
        {"kind": "preference", "content": "汇报和周报都用简洁列表，不要长段落"},
        {"kind": "preference", "content": "会议纪要当天发群里，隔天不发"},
        {"kind": "preference", "content": "重要决定先口头沟通再补文字记录"},
        {"kind": "fact", "content": "办公室在酒仙桥超图大厦三层"},
        {"kind": "fact", "content": "公司成立于2022年7月，全员十二人"},
        {"kind": "fact", "content": "供应商账期统一三十天"},
        {"kind": "person", "content": "报价审批由老王最终拍板", "entities": ["老王"]},
        {"kind": "person", "content": "人事和报销事务找李姐办理", "entities": ["李姐"]},
        {"kind": "decision", "content": "今年不再新增远程岗位，实习仅线下"},
        {"kind": "project", "content": "FactoryBrain 一期九月三十日前完成联调",
         "entities": ["FactoryBrain"], "expires_at": "2026-10-15 00:00:00"},
        {"kind": "procedure", "content": "周报生成流程 SOP（附模板文件）",
         "entities": ["周报"],
         "files": [{"name": "weekly-sop.md",
                    "content": "# 周报 SOP\n1. 拉取本周工单数据\n2. 按项目分组汇总\n"
                               "3. 标红风险项\n4. 周五 17:00 前发群"}]},
    ], actor="seed", subject=S, source_session="seed-batch")
    stats["subjects"][S] = len(r["added"])

    # 改口链：工位三层 → 五层（程序化失效 + 挂 supersede 链，替代慢速 LLM 裁决）
    r_old = hub.write([{"kind": "fact", "content": "我的工位在三层靠窗位置"}],
                      actor="seed", subject=S, embed_now=False)
    r_new = hub.write([{"kind": "fact", "content": "我的工位搬到五层新办公区了"}],
                      actor="seed", subject=S, embed_now=False)
    old_mid = r_old["added"][0]["memory_id"]
    new_mid = r_new["added"][0]["memory_id"]
    hub.invalidate(old_mid, actor="seed", reason="搬迁改口")
    _set_supersede(old_mid, new_mid)

    # 已过期条目（演示 TTL）
    hub.write([{"kind": "fact", "content": "临时通知：本周五下午断电维护两小时",
                "expires_at": "2000-01-01 00:00:00"}],
              actor="seed", subject=S, embed_now=False)
    hub.recall("临时通知", subject=S)      # 触发 TTL 自动失效

    # 会话摘要
    for i, s in enumerate([
        "用户说明了公司人员构成和成立时间",
        "用户调整了工位信息（三层→五层）",
        "用户确认了周报 SOP 并上传模板",
    ]):
        hub.add_episode(S, s, session_id="seed-session", turn_seq=i)
    stats["episodes"] = 3

    # ---------- subject = colleague（演示多用户隔离）----------
    C = "colleague"
    rc = hub.write([
        {"kind": "preference", "content": "colleague 喜欢上午开会，下午写代码"},
        {"kind": "fact", "content": "colleague 负责供应链模块开发"},
    ], actor="seed", subject=C)
    stats["subjects"][C] = len(rc["added"])

    return stats


def main():
    ap = argparse.ArgumentParser(description="灌入演示数据")
    ap.add_argument("--db", default=os.path.join(
        deps.runtime_dir(), "memory_web.db"))
    args = ap.parse_args()
    # 尝试接 ollama（有向量写入质量更好；接不上也能跑）
    try:
        from . import backends_ollama
        backends_ollama.configure()
    except Exception:
        pass
    st = seed(args.db)
    print("演示数据已入库：%s" % args.db)
    for s, n in st["subjects"].items():
        print("  subject=%s 新增 %d 条" % (s, n))
    print("  会话摘要 %d 条（含一条改口链、一条已过期、一条挂 SOP 文件）"
          % st["episodes"])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
