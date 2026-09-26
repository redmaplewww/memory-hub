# -*- coding: utf-8 -*-
"""自动采集入库演示：终端聊天，每轮对话自动「召回→回答→抽取入库」。

这是设计文档 §2.3 Online 写入管线的完整落地示例——宿主系统照抄
_turn_end() 三行即可获得自动记忆。用法：

    python -m memory_hub.chatdemo [--db PATH] [--subject me]

流程（每轮）：
  轮前  recall + profile → 注入【相关记忆】【用户画像】→ 回答更懂你
  轮末  extract_items → 值得记的自动 write 入库 + add_episode 留存档
网页控制台（webapp）开着的话，边聊边刷新「记忆库」页就能看到自动采集效果。
"""
import argparse
import json
import os
import sys
import urllib.request

from . import deps
from .core import MemoryHub
from .extract import extract_items

SYSTEM_BASE = ("你是一个有长期记忆的助手。回答要简洁。"
               "如提供了【相关记忆】和【用户画像】，优先依据它们作答。")


def _ollama_chat_plain(messages):
    """直接对话（非 JSON 模式）——chatdemo 自己的回答不走 deps（那是要 JSON 的）。"""
    from . import backends_ollama as B
    payload = {"model": B.CHAT_MODEL, "messages": messages, "stream": False,
               "options": {"temperature": 0.5}}
    req = urllib.request.Request(B.HOST + "/api/chat",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))["message"]["content"]


def main():
    ap = argparse.ArgumentParser(description="自动记忆采集聊天演示")
    ap.add_argument("--db", default=os.path.join(
        deps.runtime_dir(), "memory_web.db"))
    ap.add_argument("--subject", default="me")
    ap.add_argument("--custom", default="",
                    help="抽取定制规则（可选），如：只记工作相关")
    args = ap.parse_args()

    try:
        from . import backends_ollama
        backends_ollama.configure()
        if not deps.llm_available():
            print("⚠ Ollama 不可用，抽取功能将失效（对话仍可继续）")
    except Exception:
        print("⚠ Ollama 接入失败，降级模式")

    hub = MemoryHub(args.db)
    print("=" * 58)
    print("自动记忆采集演示（subject=%s）" % args.subject)
    print("正常聊天即可。每轮结束后自动抽取该记的内容入库；")
    print("退出输入 q。另开网页控制台可见记忆实时增长。")
    print("=" * 58)

    while True:
        try:
            user = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user or user.lower() in ("q", "quit", "exit"):
            break

        # ---- 轮前：召回 + 画像注入 ----
        hits = hub.recall(user, subject=args.subject, k=4)
        prof = hub.profile(args.subject)
        mem_block = ""
        if hits:
            mem_block = "【相关记忆】\n" + "\n".join(
                "- " + h["content"] for h in hits) + "\n"
        prof_block = ""
        prefs = (prof.get("persona") or {}).get("preferences") or []
        if prefs:
            prof_block = "【用户画像】偏好：" + "；".join(prefs[:10]) + "\n"

        # ---- 回答（chatdemo 自己的对话，不走 deps 接缝）----
        print("…思考中", flush=True)
        try:
            answer = _ollama_chat_plain([
                {"role": "system", "content": SYSTEM_BASE + "\n" + mem_block + prof_block},
                {"role": "user", "content": user}])
        except Exception as e:
            print("（对话模型出错：%s）" % e)
            continue
        print("AI：" + answer)

        # ---- 轮末：自动采集（宿主系统只需抄这三行）----
        existing = [m["content"] for m in
                    hub.list_memories(subject=args.subject, limit=50)]
        items = extract_items(user, answer, existing=existing,
                              custom_instructions=args.custom)
        if items:
            r = hub.write(items, actor="auto-chat", subject=args.subject)
            hub.add_episode(args.subject, "用户：%s…｜AI：%s…" % (
                user[:40], answer[:40]))
            for a in r["added"]:
                print("  🧠 已记住 [%s] %s" % (a["kind"], a["content"]))
            for mid in r["invalidated"]:
                print("  🔄 检测到改口，旧记忆已失效（%s）" % mid)
        else:
            hub.add_episode(args.subject, "用户：%s…（本轮无可记内容）" % user[:40])

    print("\n再见。累计记忆 %d 条（网页控制台可查看）" %
          len(hub.list_memories(subject=args.subject, limit=10 ** 6)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
