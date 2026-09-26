# -*- coding: utf-8 -*-
"""Memory Hub 压测（degraded 模式 + 假向量模式对照）。

覆盖：写入吞吐（顺序/并发）、召回延迟随库规模曲线（关键词路 vs 假向量路）、
consolidate 存量整理、TTL 清扫、事件回放查询。
"""
import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_hub import MemoryHub, deps

random.seed(42)
_TMP = tempfile.mkdtemp()


def fake_vec(text):
    # 确定性伪向量：与内容 bigram 哈希相关（同内容同向量，无语义，只测性能路径）
    v = [0.0] * 64
    for i in range(len(text) - 1):
        v[hash(text[i:i + 2]) % 64] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def inject_fake_embed():
    deps.configure(embed=lambda ts: [fake_vec(t) for t in ts],
                   embed_one=fake_vec,
                   embed_available=lambda: True)


def clear_embed():
    deps.configure(embed=None, embed_one=None)  # None 不覆盖，需直接改
    deps._impl["embed"] = None
    deps._impl["embed_one"] = None
    deps._impl["embed_available"] = None


def ms(t0):
    return (time.time() - t0) * 1000


def gen_contents(n, prefix="压测条目"):
    out = []
    words = ["对账流程", "部署规范", "汇报格式", "薪资档位", "库存盘点", "周报模板",
             "供应商管理", "订单审核", "权限配置", "数据备份"]
    for i in range(n):
        w = words[i % len(words)]
        out.append("%s%d：%s第%04d号的执行细节说明与补充条件" % (prefix, i, w, i))
    return out


def bench_write(N):
    print("\n== 1) 顺序写入 %d 条（单 subject，degraded 模式）==" % N)
    hub = MemoryHub(os.path.join(_TMP, "w1.db"))
    contents = gen_contents(N)
    t0 = time.time()
    for i, ctext in enumerate(contents):
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="bench", subject="u-stress")
    dt = ms(t0)
    print("  总耗时 %.1f ms ｜ %.0f 条/秒 ｜ 平均 %.2f ms/条" % (
        dt, N / (dt / 1000), dt / N))
    # 分段速率（检测 O(n²) 恶化）
    hub2 = MemoryHub(os.path.join(_TMP, "w2.db"))
    marks = []
    for phase, (a, b) in enumerate([(0, 200), (800, 1000), (1800, 2000)]):
        cs = gen_contents(b - a, "分段%d" % phase)
        base = hub2.list_memories(subject="u2", limit=1)
        t0 = time.time()
        for ctext in cs:
            hub2.write([{"kind": "fact", "content": ctext}],
                       actor="bench", subject="u2")
        marks.append((len(hub2.list_memories(subject="u2", limit=2000)),
                      ms(t0) / len(cs)))
    # 前面分段从空开始，需预填——上面写法每段只写 200 条，直接看各段均速
    print("  分段均速（各 200 条，库依次增长）：" +
          " / ".join("%.2f ms/条" % m for _, m in marks))
    return hub


def bench_recall_scaling(N):
    print("\n== 2) 召回延迟随库规模（同一 subject 装载 %d 条）==" % N)
    hub = MemoryHub(os.path.join(_TMP, "r1.db"))
    contents = gen_contents(N)
    for ctext in contents:
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="bench", subject="u-recall")
    sizes = [100, 500, 1000, 2000, N] if N >= 2000 else [100, 500, N]
    # 用多 subject 模拟不同库规模：直接删库重建太慢，改为控制可见条数——
    # 这里简单做法：不同规模的独立库（小库用同数据前缀）
    for sz in sizes:
        h = MemoryHub(os.path.join(_TMP, "r%d.db" % sz))
        for ctext in contents[:sz]:
            h.write([{"kind": "fact", "content": ctext}],
                    actor="bench", subject="u-recall")
        qs = ["对账流程怎么执行", "周报模板细节", "完全不相关的量子问题"]
        t0 = time.time()
        n_q = 20
        for _ in range(n_q):
            for q in qs:
                h.recall(q, "u-recall")
        dt = ms(t0) / (n_q * len(qs))
        print("  库规模 %5d 条：平均 %.2f ms/次召回（3 类查询混跑）" % (sz, dt))


def bench_fake_vec(N):
    print("\n== 3) 假向量路径（64 维，写时嵌入+召回余弦，%d 条）==" % N)
    inject_fake_embed()
    hub = MemoryHub(os.path.join(_TMP, "v1.db"))
    contents = gen_contents(N)
    t0 = time.time()
    for ctext in contents:
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="bench", subject="u-vec")
    w_dt = ms(t0) / N
    t0 = time.time()
    for _ in range(30):
        hub.recall("对账流程执行细节", "u-vec")
    r_dt = ms(t0) / 30
    print("  写入 %.2f ms/条（含嵌入+判重扫描）｜ 召回 %.2f ms/次（含全量余弦）"
          % (w_dt, r_dt))
    clear_embed()


def bench_concurrent(threads_n, per_thread):
    print("\n== 4) 并发写（%d 线程 × %d 条，不同 subject）==" % (
        threads_n, per_thread))
    hub = MemoryHub(os.path.join(_TMP, "c1.db"))
    errs = []

    def worker(tid):
        try:
            for i in range(per_thread):
                hub.write([{"kind": "fact",
                            "content": "并发线程%d条目%03d的内容说明" % (tid, i)}],
                          actor="bench", subject="u-c%d" % tid)
        except Exception as e:
            errs.append("%s: %r" % (tid, e))

    t0 = time.time()
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    dt = ms(t0)
    total = threads_n * per_thread
    ok = sum(len(hub.list_memories(subject="u-c%d" % i, limit=10 ** 6))
             for i in range(threads_n))
    print("  总耗时 %.1f ms ｜ %.0f 条/秒 ｜ 落库 %d/%d ｜ 异常 %d" % (
        dt, total / (dt / 1000), ok, total, len(errs)))
    for e in errs[:3]:
        print("    异常样例：%s" % e)


def bench_consolidate(N):
    print("\n== 5) consolidate 存量整理（%d 条单组，degraded 无向量）==" % N)
    hub = MemoryHub(os.path.join(_TMP, "g1.db"))
    contents = gen_contents(N, prefix="整理")
    for ctext in contents:
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="bench", subject="u-cons")
    t0 = time.time()
    rep = hub.consolidate(namespace="dialogue", subject="u-cons",
                          actor="stress", dry_run=True)
    print("  dry-run：%d 组 %d 条扫描 耗时 %.1f ms（组内上限 200 条防 O(n²)）"
          % (rep["groups"], rep["scanned"], ms(t0)))


def bench_ttl_maintain(N):
    print("\n== 6) TTL 清扫（%d 条已过期）==" % N)
    hub = MemoryHub(os.path.join(_TMP, "t1.db"))
    contents = gen_contents(N, prefix="过期")
    for ctext in contents:
        hub.write([{"kind": "fact", "content": ctext, "expires_at":
                    "2000-01-01 00:00:00"}], actor="bench", subject="u-ttl")
    t0 = time.time()
    out = hub.maintain(actor="stress")
    print("  清扫 %d 条 耗时 %.1f ms" % (out["expired"], ms(t0)))


def bench_events(N):
    print("\n== 7) 事件回放查询（约 %d 条事件）==" % N)
    hub = MemoryHub(os.path.join(_TMP, "e1.db"))
    contents = gen_contents(N, prefix="事件")
    for ctext in contents:
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="bench", subject="u-ev")
    t0 = time.time()
    evs = hub.events(subject="u-ev", limit=100)
    print("  events(subject) 返回 %d 条 耗时 %.2f ms" % (len(evs), ms(t0)))


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    print("Memory Hub 压测 ｜ 库目录 %s ｜ 规模 N=%d" % (_TMP, N))
    bench_write(N)
    bench_recall_scaling(min(N, 2000))
    bench_fake_vec(min(N, 2000))
    bench_concurrent(4, 50)
    bench_consolidate(N)
    bench_ttl_maintain(min(N, 500))
    bench_events(N)
    print("\n压测完成。")
