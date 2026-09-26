# -*- coding: utf-8 -*-
"""M8a MemoryHub 内核测试：三层记忆 + 事件审计 + 向量召回 + invalidate。

双模式运行：
- twin-agent 仓内（conftest LLM 在线门禁）：冲突裁决/画像合并走真实 DeepSeek，
  向量召回走真实本地 bge（fastembed）。
- 独立打包（memory_hub_package/tests/）：无 LLM/embed 后端时，带
  @backend 标记的用例自动跳过（deps 未注入），其余（CRUD/TTL/审计/隔离）照跑。

沙箱临时库，不触碰真实记忆。
"""
import os
import sys
import time
import uuid

import pytest

from memory_hub import MemoryHub
from memory_hub import deps as _deps

_HAS_LLM = _deps.llm_available()
_HAS_EMBED = _deps.embed_available()
backend = pytest.mark.skipif(
    not (_HAS_LLM and _HAS_EMBED),
    reason="独立部署：未注入 LLM/embedding 后端（deps.configure）")


@pytest.fixture
def hub(tmp_path):
    return MemoryHub(str(tmp_path / ("mem-%s.db" % uuid.uuid4().hex[:6])))


def _uid():
    return "u-" + uuid.uuid4().hex[:6]


# ---------------- 迁移兼容 ----------------

def test_schema_migration_from_legacy(tmp_path):
    """旧版 dialogue/memory.py 建的 memories 基表可无损演进到 Hub schema。"""
    import sqlite3
    p = str(tmp_path / "legacy.db")
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE memories(
      memory_id TEXT PRIMARY KEY, tenant TEXT, user_id TEXT, role TEXT,
      kind TEXT, content TEXT, source_session TEXT DEFAULT '',
      created_at TEXT, updated_at TEXT, active INTEGER DEFAULT 1);
    """)
    c.execute("INSERT INTO memories(memory_id,tenant,user_id,role,kind,content,"
              "created_at,updated_at,active) VALUES('m-legacy','yunpai','u1','owner',"
              "'fact','旧记忆内容','2026-09-14 10:00:00','2026-09-14 10:00:00',1)")
    c.commit()
    c.close()

    h = MemoryHub(p)
    m = h.get_memory("m-legacy")
    assert m is not None and m["content"] == "旧记忆内容"
    assert m["status"] == "active" and m["namespace"] == "dialogue"
    assert m["vec"] is None            # 旧条目无向量（vec_stale=0，可由 maintain 补）


# ---------------- 写入与去重 ----------------

def test_write_and_dedup_substring(hub):
    uid = _uid()
    r = hub.write([{"kind": "preference", "content": "汇报用表格格式"}],
                  actor="owner", subject=uid)
    assert len(r["added"]) == 1
    # 子串重复 → deduped
    r2 = hub.write([{"kind": "preference", "content": "汇报用表格格式"}],
                   actor="owner", subject=uid)
    assert r2["deduped"] and not r2["added"]


@backend
def test_write_dedup_semantic(hub):
    """语义同义（余弦≥0.85）判重。"""
    uid = _uid()
    hub.write([{"kind": "fact", "content": "供应商对账统一走老王那边"}],
              actor="owner", subject=uid)
    r = hub.write([{"kind": "fact", "content": "供应商对账找老王处理"}],
                  actor="owner", subject=uid)
    assert r["deduped"] or not r["added"], r


# ---------------- 冲突消解（invalidate） ----------------

@backend
def test_contradict_invalidates_old(hub):
    """改口：新偏好推翻旧偏好——旧条目 invalidated（不删），新条目 active。
    冲突裁决走真实 LLM；语义相近前提（改口句通常高相似）由本用例同时验证。"""
    uid = _uid()
    hub.write([{"kind": "preference", "content": "汇报统一用表格格式"}],
              actor="owner", subject=uid)
    r = hub.write([{"kind": "preference", "content": "以后汇报改用列表格式"}],
                  actor="owner", subject=uid)
    rows = hub.list_memories(subject=uid, include_invalidated=True)
    if r["invalidated"]:
        # LLM 裁决为 contradict：旧失效新生效，默认召回只剩新偏好
        statuses = {m["content"]: m["status"] for m in rows}
        assert statuses["汇报统一用表格格式"] == "invalidated"
        assert statuses["以后汇报改用列表格式"] == "active"
        hits = hub.recall("汇报格式偏好", uid)
        assert all(h["content"] != "汇报统一用表格格式" for h in hits)
        ev = hub.events(memory_id=[m["memory_id"] for m in rows
                                   if m["status"] == "invalidated"][0])
        assert any(e["op"] == "invalidated" for e in ev)
    else:
        # LLM 判 coexist（保守）：两条并存——记录在案，不算失败
        assert len(rows) == 2


def test_invalidate_and_reactivate(hub):
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "办公室在酒仙桥超图大厦"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    assert hub.invalidate(mid, actor="owner", reason="测试")
    assert hub.get_memory(mid)["status"] == "invalidated"
    assert hub.recall("办公室在哪", uid) == [] or \
        all(h["memory_id"] != mid for h in hub.recall("办公室在哪", uid))
    assert hub.reactivate(mid)
    assert hub.get_memory(mid)["status"] == "active"
    assert hub.recall("办公室在哪", uid) and \
        hub.recall("办公室在哪", uid)[0]["memory_id"] == mid


# ---------------- 生命周期 ----------------

def test_forget_soft_delete(hub):
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "测试条目 %s" % uuid.uuid4().hex[:6]}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    assert hub.forget(mid, actor="owner", reason="不要了")
    m = hub.get_memory(mid)
    assert m["status"] == "archived" and m["active"] == 0
    assert all(h["memory_id"] != mid for h in hub.list_memories(subject=uid))
    assert any(e["op"] == "forgot" for e in hub.events(memory_id=mid))


def test_ttl_expiry(hub):
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "临时信息 %s" % uuid.uuid4().hex[:6],
                    "expires_at": "2000-01-01 00:00:00"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    # 召回时发现 TTL 已过 → 自动失效并跳过
    hits = hub.recall("临时信息", uid)
    assert all(h["memory_id"] != mid for h in hits)
    assert hub.get_memory(mid)["status"] == "invalidated"
    ev = hub.events(memory_id=mid)
    assert any(e["op"] == "invalidated" and "TTL" in e["note"] for e in ev)


@backend
def test_maintain_reembeds_stale(hub):
    """vec_stale=1 的条目由 maintain 补嵌（真实 bge）。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "需要补嵌的条目 %s" % uuid.uuid4().hex[:6]}],
                  actor="owner", subject=uid, embed_now=False)
    mid = r["added"][0]["memory_id"]
    assert hub.get_memory(mid)["vec"] is None

    import sqlite3
    conn = sqlite3.connect(hub.path)
    with conn:
        conn.execute("UPDATE memories SET vec_stale=1 WHERE memory_id=?", (mid,))
    conn.close()

    out = hub.maintain()
    assert out["reembedded"] >= 1
    assert hub.get_memory(mid)["vec"] is not None


# ---------------- 召回 ----------------

@backend
def test_recall_semantic_paraphrase(hub):
    """同义改写召回（此前旧系统的实测短板）：无共同字面词也能命中。"""
    uid = _uid()
    hub.write([{"kind": "preference", "content": "我偏好用表格来做汇报"}],
              actor="owner", subject=uid)
    hits = hub.recall("你希望我怎么做周报", uid)
    assert hits and any("表格" in h["content"] for h in hits)


def test_recall_kw_and_rrf(hub):
    uid = _uid()
    hub.write([
        {"kind": "fact", "content": "办公室在酒仙桥超图大厦"},
        {"kind": "fact", "content": "技术岗薪资三档 15-20K"},
        {"kind": "decision", "content": "报价统一走老王"},
    ], actor="owner", subject=uid)
    hits = hub.recall("办公室在哪", uid)
    assert hits and hits[0]["content"].startswith("办公室")


# ---------------- procedure 文件引用（M8b-1 MemFS-lite） ----------------

def test_procedure_write_with_files(hub):
    """kind=procedure 可挂文件：落库→召回带 file_ids→内容可原样读回。"""
    uid = _uid()
    r = hub.write([{
        "kind": "procedure", "content": "周报生成流程 SOP",
        "files": [{"name": "weekly-sop.md", "content": "# 周报 SOP\n1. 拉取数据\n2. 生成表格"}],
    }], actor="owner", subject=uid)
    assert len(r["added"]) == 1 and r["added"][0]["kind"] == "procedure"
    fids = r["added"][0]["file_ids"]
    assert len(fids) == 1
    f = hub.read_memory_file(fids[0])
    assert f["name"] == "weekly-sop.md" and "拉取数据" in f["content"]
    hits = hub.recall("周报流程 SOP", uid)
    assert hits and hits[0]["file_ids"] == fids


def test_file_sha_dedup_reuse(hub):
    """同内容文件跨记忆复用同一 file_id（sha 去重，不重复存储）。
    真向量模式下第二条内容本身即语义判重（added 为空）——同属正确行为。"""
    uid = _uid()
    doc = {"name": "spec.txt", "content": "同一份规格说明内容"}
    r1 = hub.write([{"kind": "procedure", "content": "规格说明 A", "files": [doc]}],
                   actor="owner", subject=uid)
    r2 = hub.write([{"kind": "procedure", "content": "规格说明 B 场景", "files": [doc]}],
                   actor="owner", subject=uid)
    if r2["added"]:
        assert r1["added"][0]["file_ids"] == r2["added"][0]["file_ids"]
    else:
        assert r2["deduped"]            # 语义判重：第二条整体去重


def test_attach_files_and_event(hub):
    """attach_files 追加文件：版本+1，事件 files-attached 留痕。"""
    uid = _uid()
    r = hub.write([{"kind": "procedure", "content": "部署流程手册"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    v0 = hub.get_memory(mid)["version"]
    new_ids = hub.attach_files(mid, [{"name": "deploy.sh", "content": "echo deploy"}],
                               actor="owner")
    assert len(new_ids) == 1
    m = hub.get_memory(mid)
    assert m["version"] == v0 + 1 and new_ids[0] in m["file_ids"]
    assert any(e["op"] == "files-attached" for e in hub.events(memory_id=mid))
    meta = hub.memory_files(mid)
    assert len(meta) == 1 and meta[0]["name"] == "deploy.sh" \
        and "content" not in meta[0]


def test_file_oversize_rejected(hub):
    """超限（>64KB）或非文本文件静默跳过，记忆本体照常落库。"""
    uid = _uid()
    r = hub.write([{
        "kind": "procedure", "content": "带超限附件的流程",
        "files": [{"name": "big.txt", "content": "x" * (64 * 1024 + 1)},
                  "不是个字典"],
    }], actor="owner", subject=uid)
    assert len(r["added"]) == 1 and r["added"][0]["file_ids"] == []


def test_recall_namespace_isolation(hub):
    uid = _uid()
    hub.write([{"kind": "fact", "content": "对话域条目内容"}],
              actor="owner", namespace="dialogue", subject=uid)
    hub.write([{"kind": "fact", "content": "Agent域条目内容"}],
              actor="agent", namespace="agent", subject="agent-1")
    assert hub.recall("条目内容", uid, namespace="dialogue")
    assert all("Agent域" not in h["content"]
               for h in hub.recall("条目内容", uid, namespace="dialogue"))
    assert any("Agent域" in h["content"]
               for h in hub.recall("条目内容", "agent-1", namespace="agent"))


def test_recall_visible_filter(hub):
    uid = _uid()
    hub.write([{"kind": "fact", "content": "公开的记忆条目", "access": "public"},
               {"kind": "fact", "content": "受限的记忆条目", "access": "restricted-hr"}],
              actor="owner", subject=uid)
    hits = hub.recall("记忆条目", uid, visible=["public"])
    assert hits and all(h["content"] == "公开的记忆条目" for h in hits)


def test_recall_empty_fallback(hub):
    """无任何命中 → 最近 3 条背景（保持在场感）。"""
    uid = _uid()
    hub.write([{"kind": "fact", "content": "完全不相关的内容甲"}],
              actor="owner", subject=uid)
    hits = hub.recall("量子力学问题", uid)
    assert 1 <= len(hits) <= 3


# ---------------- L1 profile ----------------

@backend
def test_profile_projection_and_merge(hub):
    """kind=preference 写入自动投影到 L1 profile（真实 LLM 合并）。"""
    uid = _uid()
    r = hub.write([
        {"kind": "preference", "content": "汇报偏好简洁列表"},
        {"kind": "fact", "content": "办公室在酒仙桥"},
    ], actor="owner", subject=uid)
    assert r["profile_updated"] is True
    p = hub.profile(uid)
    assert p["version"] >= 1
    joined = "".join(str(x) for x in (p["persona"].get("preferences") or []))
    assert "列表" in joined
    # 事实条目不进画像
    assert "酒仙桥" not in joined


# ---------------- L3 episodes ----------------

def test_episodes(hub):
    uid = _uid()
    hub.add_episode(uid, "用户询问了公司地址，AI 给出酒仙桥答案", session_id="s-x")
    hub.add_episode(uid, "用户口述了新字段提案", session_id="s-x", turn_seq=1)
    eps = hub.episodes(uid)
    assert len(eps) == 2 and eps[0]["summary"]        # 最新在前
    assert all(e["session_id"] == "s-x" for e in eps)


# ---------------- 事件审计 ----------------

def test_events_audit_trail(hub):
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "审计测试条目 %s" % uuid.uuid4().hex[:6]}],
                  actor="owner", subject=uid, source_session="s-audit")
    mid = r["added"][0]["memory_id"]
    hub.edit(mid, "审计测试条目-编辑后", actor="owner")
    hub.forget(mid, actor="owner", reason="清理")
    evs = hub.events(memory_id=mid)
    ops = [e["op"] for e in evs]
    assert ops == ["extracted", "edited", "forgot"]
    assert "旧值" in evs[1]["note"]
    # 按 subject 聚合回放
    all_ev = hub.events(subject=uid)
    assert len(all_ev) >= 3


@backend
def test_edit_rebuilds_vector_and_version(hub):
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "编辑前内容：公司有五名全职员工"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    v0 = hub.get_memory(mid)["version"]
    assert hub.edit(mid, "编辑后内容：公司有九名全职员工", actor="owner")
    m = hub.get_memory(mid)
    assert m["version"] == v0 + 1 and m["vec"] is not None
    hits = hub.recall("多少名员工", uid)
    assert any("九名" in h["content"] for h in hits)


# ---------------- sleep-time 整理与 supersede 溯源（M9） ----------------

import contextlib


@contextlib.contextmanager
def _fake_deps(llm=None, emb=None):
    """临时注入假 LLM/假向量（deps.configure），退出恢复原实现。

    只测 Hub 管线（裁决分支/阈值分档/链写入），不测模型质量——
    真后端质量由 @backend 用例在 twin-agent 仓内覆盖。
    """
    import json as _json
    saved = dict(_deps._impl)
    try:
        if llm is not None:
            _deps.configure(
                llm_chat=lambda msgs, temperature=0.1, json_mode=True: llm(msgs),
                llm_available=lambda: True,
                parse_json=lambda t: _json.loads(t) if t else None)
        if emb is not None:
            _deps.configure(embed_one=emb,
                            embed=lambda ts: [emb(t) for t in ts],
                            embed_available=lambda: True)
        yield
    finally:
        _deps._impl.clear()
        _deps._impl.update(saved)


def _set_vecs(hub, mapping):
    """直接写 vec BLOB，精确控制条目间余弦：同向=1.0、(1,0)/(0.7,0.714…)=0.70。"""
    import sqlite3
    import struct
    conn = sqlite3.connect(hub.path)
    with conn:
        for mid, v in mapping.items():
            conn.execute("UPDATE memories SET vec=? WHERE memory_id=?",
                         (struct.pack("%df" % len(v), *v), mid))
    conn.close()


def _set_created(hub, mid, ts):
    """固定 created_at（秒级精度无法区分同秒写入，排序新旧需要显式时间）。"""
    import sqlite3
    conn = sqlite3.connect(hub.path)
    with conn:
        conn.execute("UPDATE memories SET created_at=? WHERE memory_id=?",
                     (ts, mid))
    conn.close()


def test_consolidate_merges_near_dup(hub):
    """存量高相似对（余弦≥0.85）确定性判重合并：留长条目，短条目挂 superseded_by。"""
    uid = _uid()
    r = hub.write([
        {"kind": "fact", "content": "供应商对账统一走老王那边处理"},
        {"kind": "fact", "content": "供应商对账这事找老王就行"},
    ], actor="owner", subject=uid, embed_now=False)
    mids = [a["memory_id"] for a in r["added"]]
    assert len(mids) == 2
    _set_vecs(hub, {m: (1.0, 0.0) for m in mids})

    rep = hub.consolidate(subject=uid, actor="maintain-test")
    assert rep["merged"] == 1 and rep["conflicts"] == 0 and rep["llm_judged"] == 0

    rows = {m["memory_id"]: m for m in
            hub.list_memories(subject=uid, include_invalidated=True)}
    dropped = [m for m in mids if rows[m]["status"] == "invalidated"]
    kept = [m for m in mids if rows[m]["status"] == "active"]
    assert len(dropped) == 1 and len(kept) == 1
    assert len(rows[kept[0]]["content"]) >= len(rows[dropped[0]]["content"])
    assert rows[dropped[0]]["superseded_by"] == kept[0]
    ev = hub.events(memory_id=dropped[0])
    assert any(e["op"] == "invalidated" and "sleep-time" in e["note"] for e in ev)


def test_consolidate_conflict_llm_and_related(hub):
    """存量矛盾对（0.60≤余弦<0.85）LLM 裁决 contradict：新胜旧，related() 双向可溯。"""
    uid = _uid()
    old_c, new_c = "技术岗薪资三档十五到二十K", "技术岗薪资从下月起统一上调"
    r = hub.write([{"kind": "fact", "content": old_c}],
                  actor="owner", subject=uid, embed_now=False)
    r2 = hub.write([{"kind": "fact", "content": new_c}],
                   actor="owner", subject=uid, embed_now=False)
    old_mid, new_mid = r["added"][0]["memory_id"], r2["added"][0]["memory_id"]
    _set_created(hub, old_mid, "2026-01-01 00:00:00")
    _set_created(hub, new_mid, "2026-01-02 00:00:00")
    _set_vecs(hub, {old_mid: (1.0, 0.0), new_mid: (0.7, 0.714142842)})

    with _fake_deps(llm=lambda msgs: '{"relation": "contradict"}'):
        rep = hub.consolidate(subject=uid, actor="maintain-test")
    assert rep["conflicts"] == 1 and rep["llm_judged"] == 1 and rep["merged"] == 0

    m = hub.get_memory(old_mid)
    assert m["status"] == "invalidated" and m["superseded_by"] == new_mid
    rel = hub.related(old_mid)
    assert rel["supersede_chain"] and \
        rel["supersede_chain"][0]["memory_id"] == new_mid
    rel2 = hub.related(new_mid)
    assert rel2["superseded_by"] is None and \
        [x["memory_id"] for x in rel2["supersedes"]] == [old_mid]
    # reactivate 清链：恢复旧条目时取代关系不再成立
    assert hub.reactivate(old_mid)
    assert hub.get_memory(old_mid)["superseded_by"] is None


def test_consolidate_dry_run(hub):
    """dry_run 只报告不动库。"""
    uid = _uid()
    r = hub.write([
        {"kind": "fact", "content": "周会固定在每周一上午开"},
        {"kind": "fact", "content": "周会时间定在礼拜一早上"},
    ], actor="owner", subject=uid, embed_now=False)
    mids = [a["memory_id"] for a in r["added"]]
    _set_vecs(hub, {m: (1.0, 0.0) for m in mids})

    rep = hub.consolidate(subject=uid, dry_run=True)
    assert rep["merged"] == 1
    assert all(hub.get_memory(m)["status"] == "active" for m in mids)
    assert all(e["op"] == "extracted" for e in hub.events(subject=uid))


def test_write_conflict_sets_supersede_chain(hub):
    """在线改口路径（假 LLM+假向量注入）：旧条目 invalidated 且 superseded_by 指向新条目。"""
    uid = _uid()

    def _emb(text):
        return [0.7, 0.714142842] if "列表" in text else [1.0, 0.0]

    with _fake_deps(llm=lambda msgs: '{"relation": "contradict"}', emb=_emb):
        r1 = hub.write([{"kind": "preference", "content": "汇报统一用表格格式"}],
                       actor="owner", subject=uid)
        r2 = hub.write([{"kind": "preference", "content": "以后汇报改用列表格式"}],
                       actor="owner", subject=uid)
    assert r2["invalidated"] and len(r2["added"]) == 1
    old_mid, new_mid = r1["added"][0]["memory_id"], r2["added"][0]["memory_id"]
    m = hub.get_memory(old_mid)
    assert m["status"] == "invalidated" and m["superseded_by"] == new_mid
    assert hub.recall("汇报格式", uid) and \
        all(h["memory_id"] != old_mid for h in hub.recall("汇报格式", uid))


def test_supersede_chain_multi_hop(hub):
    """两级取代链 A→B→C：前向追到最新版本，后向收回全部历史版本。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "薪资方案甲版本内容一"},
                   {"kind": "fact", "content": "薪资方案乙版本内容二"}],
                  actor="owner", subject=uid, embed_now=False)
    a, b = [x["memory_id"] for x in r["added"]]
    _set_created(hub, a, "2026-01-01 00:00:00")
    _set_created(hub, b, "2026-01-02 00:00:00")
    _set_vecs(hub, {a: (1.0, 0.0), b: (0.7, 0.714142842)})
    with _fake_deps(llm=lambda msgs: '{"relation": "contradict"}'):
        assert hub.consolidate(subject=uid)["conflicts"] == 1

    r3 = hub.write([{"kind": "fact", "content": "薪资方案丙版本内容三"}],
                   actor="owner", subject=uid, embed_now=False)
    c = r3["added"][0]["memory_id"]
    _set_created(hub, c, "2026-01-03 00:00:00")
    _set_vecs(hub, {c: (1.0, 0.0)})          # 与 b 的余弦仍为 0.70
    with _fake_deps(llm=lambda msgs: '{"relation": "contradict"}'):
        assert hub.consolidate(subject=uid)["conflicts"] == 1

    rel = hub.related(a)
    assert [x["memory_id"] for x in rel["supersede_chain"]] == [b, c]
    assert hub.get_memory(c)["status"] == "active"
    assert {x["memory_id"] for x in hub.related(c)["supersedes"]} == {a, b}


def test_consolidate_llm_duplicate_merges(hub):
    """LLM 裁决 duplicate（0.60≤余弦<0.85）：按合并处理而非丢弃。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "公司账期约定是三十天整"},
                   {"kind": "fact", "content": "和供应商约定三十天账期"}],
                  actor="owner", subject=uid, embed_now=False)
    mids = [a["memory_id"] for a in r["added"]]
    _set_created(hub, mids[0], "2026-01-01 00:00:00")
    _set_created(hub, mids[1], "2026-01-02 00:00:00")
    _set_vecs(hub, {mids[0]: (1.0, 0.0), mids[1]: (0.7, 0.714142842)})

    with _fake_deps(llm=lambda msgs: '{"relation": "duplicate"}'):
        rep = hub.consolidate(subject=uid)
    assert rep["llm_judged"] == 1 and rep["merged"] == 1 and rep["conflicts"] == 0
    assert len(hub.list_memories(subject=uid)) == 1


def test_maintain_consolidate_flag(hub):
    """maintain(consolidate=True)：确定性任务 + sleep-time 整理一次跑完。"""
    uid = _uid()
    hub.write([
        {"kind": "fact", "content": "对账联系人就是老王本人"},
        {"kind": "fact", "content": "对账联系老王他本人"},
    ], actor="owner", subject=uid, embed_now=False)
    mids = [m["memory_id"] for m in hub.list_memories(subject=uid)]
    _set_vecs(hub, {m: (1.0, 0.0) for m in mids})
    out = hub.maintain(actor="maintain-test", consolidate=True)
    assert "consolidated" in out and out["consolidated"]["merged"] == 1
    assert len(hub.list_memories(subject=uid)) == 1


# ---------------- M10：批量裁决 / 实体信号 / 时间意图 / 过滤 / 精排 / 反馈 ----------------

def test_batch_judge_two_candidates(hub):
    """P1 批量裁决：两个相近候选一次 LLM 调用分别判定（coexist + contradict）。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "技术岗薪资三档十五到二十K"},
                   {"kind": "fact", "content": "技术岗薪资半年评审一次"}],
                  actor="owner", subject=uid, embed_now=False)
    old_mids = [a["memory_id"] for a in r["added"]]
    _set_created(hub, old_mids[0], "2026-01-01 00:00:00")
    _set_created(hub, old_mids[1], "2026-01-01 00:00:01")
    _set_vecs(hub, {m: (0.7, 0.714142842) for m in old_mids})

    def _emb(text):
        return [1.0, 0.0] if "上调" in text else [0.0, 1.0]

    batch_json = ('{"relations": [{"id": 1, "relation": "coexist"}, '
                  '{"id": 2, "relation": "contradict"}]}')
    with _fake_deps(llm=lambda msgs: batch_json, emb=_emb):
        r2 = hub.write([{"kind": "fact", "content": "技术岗薪资从下月起统一上调"}],
                       actor="owner", subject=uid)
    assert len(r2["invalidated"]) == 1 and len(r2["added"]) == 1
    assert hub.get_memory(old_mids[0])["status"] == "active"
    assert hub.get_memory(old_mids[1])["status"] == "invalidated"
    assert hub.get_memory(old_mids[1])["superseded_by"] == r2["added"][0]["memory_id"]


def test_write_entities_stored(hub):
    """P2 实体落库：write 传 entities 存列、去重去空，召回命中带出。"""
    uid = _uid()
    hub.write([{"kind": "person", "content": "对账联系人对口人",
                "entities": ["老王", "老王", ""]}],
              actor="owner", subject=uid)
    m = hub.list_memories(subject=uid)[0]
    assert m["entities"] == '["老王"]'
    hits = hub.recall("老王负责什么", uid)
    assert hits and hits[0]["entities"] == ["老王"]


def test_recall_entity_signal(hub):
    """P2 实体路召回：条目内容与查询无字面重叠，仅凭实体命中也能入榜。"""
    uid = _uid()
    hub.write([{"kind": "person", "content": "对账事宜联系人对口人",
                "entities": ["老王"]},
               {"kind": "person", "content": "老王负责报价审批"}],
              actor="owner", subject=uid)
    hits = hub.recall("老王管什么", uid)
    assert len(hits) == 2                    # 实体路 + 关键词路都入榜


def test_recall_temporal_past_intent(hub):
    """P2 时间意图：「之前」类问句自动连同失效历史一起召回。"""
    uid = _uid()
    r = hub.write([{"kind": "preference", "content": "汇报格式用表格"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    hub.invalidate(mid, actor="owner", reason="改口")
    # 默认召回不出现旧值
    assert all(h["memory_id"] != mid for h in hub.recall("汇报格式", uid))
    # 历史问句连旧版本一起给
    hits = hub.recall("之前汇报格式是什么样的", uid)
    assert any(h["memory_id"] == mid and h["status"] == "invalidated"
               for h in hits)


def test_recall_filters(hub):
    """P3 结构化过滤：kind 精确 + created_at 区间（字符串比较，含端点）。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "事实条目甲"},
                   {"kind": "decision", "content": "决策条目乙"}],
                  actor="owner", subject=uid)
    mids = {a["content"]: a["memory_id"] for a in r["added"]}
    _set_created(hub, mids["事实条目甲"], "2026-01-05 00:00:00")
    _set_created(hub, mids["决策条目乙"], "2026-02-05 00:00:00")
    assert [h["content"] for h in
            hub.recall("条目", uid, filters={"kind": "fact"})] == ["事实条目甲"]
    assert [h["content"] for h in hub.recall(
        "条目", uid, filters={"since": "2026-01-15 00:00:00"})] == ["决策条目乙"]
    assert [h["content"] for h in hub.recall(
        "条目", uid, filters={"since": "2026-01-01 00:00:00",
                             "until": "2026-01-31 00:00:00"})] == ["事实条目甲"]


def test_recall_rerank(hub):
    """P3 LLM 二段精排：fake order 翻转 RRF 原序；返回垃圾时保持原序。"""
    uid = _uid()
    hub.write([{"kind": "fact", "content": "公司注册地址在酒仙桥"},
               {"kind": "fact", "content": "办公室在望京大厦"}],
              actor="owner", subject=uid)
    base = hub.recall("公司办公室在哪", uid)
    assert base[0]["content"] == "办公室在望京大厦"      # RRF 原序
    with _fake_deps(llm=lambda msgs: '{"order": [2, 1]}'):
        flipped = hub.recall("公司办公室在哪", uid, rerank=True)
    assert [h["content"] for h in flipped] == ["公司注册地址在酒仙桥",
                                               "办公室在望京大厦"]
    with _fake_deps(llm=lambda msgs: "not json"):
        keep = hub.recall("公司办公室在哪", uid, rerank=True)
    assert keep[0]["content"] == "办公室在望京大厦"


def test_hit_count_feedback(hub):
    """P3 召回反馈：命中条目 hit_count 递增。"""
    uid = _uid()
    r = hub.write([{"kind": "fact", "content": "反馈计数测试条目"}],
                  actor="owner", subject=uid)
    mid = r["added"][0]["memory_id"]
    assert hub.get_memory(mid)["hit_count"] == 0
    hub.recall("反馈计数测试条目", uid)
    hub.recall("反馈计数测试条目", uid)
    assert hub.get_memory(mid)["hit_count"] == 2


def test_extract_custom_instructions_and_entities():
    """P1 定制抽取：custom_instructions 注入系统提示词；entities 随条目解析。"""
    from memory_hub import extract
    captured = {}

    def _llm(msgs, **kw):
        captured["system"] = msgs[0]["content"]
        return ('{"memories": [{"kind": "fact", "content": "订单 9527 未送达", '
                '"entities": ["订单 9527"]}]}')

    with _fake_deps(llm=_llm):
        out = extract.extract_items("我的订单 9527 还没到", "已为您查询",
                                    custom_instructions="只提取订单号和物流信息")
    assert "只提取订单号和物流信息" in captured["system"]
    assert "调用方定制要求" in captured["system"]
    assert out and out[0]["entities"] == ["订单 9527"]


def test_extract_custom_instructions_absent():
    """不传 custom_instructions 时系统提示词保持原样。"""
    from memory_hub import extract
    captured = {}

    def _llm(msgs, **kw):
        captured["system"] = msgs[0]["content"]
        return '{"memories": []}'

    with _fake_deps(llm=_llm):
        extract.extract_items("你好", "你好")
    assert "调用方定制要求" not in captured["system"]


def test_norm_relation_aliases():
    """relation 值归一：中文/大小写/同义词兼容；「不冲突」须先于「冲突」命中。"""
    from memory_hub.core import _norm_relation
    assert _norm_relation("contradict") == "contradict"
    assert _norm_relation("DUPLICATE") == "duplicate"
    assert _norm_relation("冲突") == "contradict"
    assert _norm_relation("矛盾") == "contradict"
    assert _norm_relation("不冲突") == "coexist"      # 否定优先
    assert _norm_relation("共存") == "coexist"
    assert _norm_relation("重复") == "duplicate"
    assert _norm_relation("乱码unknown") == "coexist"  # 未知保守
    assert _norm_relation(None) == "coexist"
    assert _norm_relation(123) == "coexist"


def test_consolidate_rebuilds_profile(hub):
    """画像回缩：整理使偏好失效后，L1 画像以有效偏好重建（剔除失效项）。

    真实场景：LLM 判定改口（表格→列表）后旧偏好失效，但画像在写入时
    只做投影累加——不重建会新旧并存。"""
    uid = _uid()
    r1 = hub.write([{"kind": "preference", "content": "汇报统一用表格格式"}],
                   actor="owner", subject=uid, embed_now=False)
    r2 = hub.write([{"kind": "preference", "content": "以后汇报改用列表格式"}],
                   actor="owner", subject=uid, embed_now=False)
    a, b = r1["added"][0]["memory_id"], r2["added"][0]["memory_id"]
    _set_created(hub, a, "2026-01-01 00:00:00")
    _set_created(hub, b, "2026-01-02 00:00:00")
    _set_vecs(hub, {a: (1.0, 0.0), b: (0.7, 0.714142842)})

    with _fake_deps(llm=lambda msgs: '{"relation": "contradict"}'):
        # 画像里两条并存（写入投影；假后端 merge 为简单追加）
        before = hub.profile(uid)
        joined = "".join(before["persona"].get("preferences") or [])
        assert "表格" in joined and "列表" in joined
        rep = hub.consolidate(subject=uid)
    assert rep["conflicts"] == 1 and rep["profiles_rebuilt"] == 1
    after = hub.profile(uid)
    joined = "".join(after["persona"].get("preferences") or [])
    assert "列表格式" in joined and "统一用表格" not in joined   # 失效偏好被剔除
    assert after["version"] > before["version"]
    evs = [e["op"] for e in hub.events(subject=uid, limit=10**6)]
    assert evs.count("profile-updated") >= 3             # 投影2次 + 重建1次


# ---------------- 基准评测（bench，零依赖基线） ----------------

bench0dep = pytest.mark.skipif(
    _HAS_LLM or _HAS_EMBED, reason="仅零依赖基线（有后端时跑完整模式口径不同）")


@bench0dep
def test_bench_zero_dep_baseline():
    """基准零依赖基线：literal 4/4 全过、语义/冲突类 0/2——后端注入收益的对照起点。"""
    from memory_hub import bench
    report = bench.run()
    assert report["mode"] == "degraded"
    c = report["categories"]
    assert c["literal"]["ok"] == c["literal"]["total"] == 4
    assert c["paraphrase"]["ok"] == 0
    assert c["conflict"]["ok"] == 0
    assert report["total"] == 6 and report["ok"] == 4


# ---------------- 并发纪律 ----------------

try:
    import twin_agent  # noqa: F401
    _IN_HOST = True
except ImportError:
    _IN_HOST = False
hostonly = pytest.mark.skipif(not _IN_HOST,
                              reason="独立部署：宿主 SessionManager 不在包内")


@hostonly
def test_shared_lock_with_session_manager(hub, tmp_path):
    """Hub 与 SessionManager 同库共享 dbutil.LOCK——交错写不死锁（宿主专属）。"""
    from twin_agent.dialogue import SessionManager
    sm = SessionManager(hub.path)
    uid = _uid()
    sid = sm.create_session("owner", uid)
    contents = ["办公室在酒仙桥超图大厦", "技术岗薪资三档 15-20K",
                "公司成立于2022年7月", "报价统一走老王", "实习生仅线下不招远程"]
    for i, ctext in enumerate(contents):
        hub.write([{"kind": "fact", "content": ctext}],
                  actor="owner", subject=uid)
        sm.append(sid, "user", "消息 %d" % i)
    assert len(hub.list_memories(subject=uid)) == 5
    assert len(sm.history(sid)) == 5


# ---------------- namespace 治理（M8b-2 多 Agent 共享/隔离） ----------------

def test_namespace_restricted_write_denied(hub):
    """restricted 命名空间：owner 可写，其他 Agent 静默拒绝（denied 回执）。"""
    hub.register_namespace("shared-kb", owner="agent-A", policy="restricted")
    r_owner = hub.write([{"kind": "fact", "content": "共享事实条目甲"}],
                        actor="agent-A", namespace="shared-kb", subject="proj-1")
    assert len(r_owner["added"]) == 1 and not r_owner["denied"]
    r_guest = hub.write([{"kind": "fact", "content": "访客想写的条目"}],
                        actor="agent-B", namespace="shared-kb", subject="proj-1")
    assert r_guest["denied"] == ["shared-kb"] and not r_guest["added"]
    # 访客只读：召回可见
    hits = hub.recall("共享事实", subject="proj-1", namespace="shared-kb")
    assert any("共享事实条目甲" in h["content"] for h in hits)


def test_namespace_open_and_backward_compat(hub):
    """未注册命名空间默认开放（向后兼容）；open 策略任何人可写。"""
    # 未注册 → 开放
    r = hub.write([{"kind": "fact", "content": "未注册域的条目"}],
                  actor="anyone", namespace="wild-ns", subject="s")
    assert len(r["added"]) == 1 and not r["denied"]
    # open → 任何人可写
    hub.register_namespace("free-ns", owner="agent-A", policy="open")
    r2 = hub.write([{"kind": "fact", "content": "开放域的条目"}],
                   actor="agent-B", namespace="free-ns", subject="s")
    assert len(r2["added"]) == 1 and not r2["denied"]
    # 非法策略名注册失败
    assert hub.register_namespace("bad-ns", owner="a", policy="weird") is False


def test_namespace_registry_listing(hub):
    """list_namespaces 列出注册表；重复注册覆盖策略。"""
    hub.register_namespace("ns-x", owner="a", policy="open")
    hub.register_namespace("ns-y", owner="b", policy="restricted")
    hub.register_namespace("ns-x", owner="a2", policy="restricted")  # 覆盖
    nss = {n["namespace"]: n for n in hub.list_namespaces()}
    assert set(nss) == {"ns-x", "ns-y"}
    assert nss["ns-x"]["policy"] == "restricted" and nss["ns-x"]["owner"] == "a2"
    assert nss["ns-y"]["policy"] == "restricted"
    # 覆盖后原 open 写权限立即收紧
    r = hub.write([{"kind": "fact", "content": "覆盖后写入测试条目"}],
                  actor="guest", namespace="ns-x", subject="s")
    assert r["denied"] == ["ns-x"]


def test_recall_multi_namespace_fusion(hub):
    """Agent 融合召回：自己的域 + 共享域一次查询，命中带 namespace 来源。"""
    uid = _uid()
    hub.write([{"kind": "fact", "content": "私有域：本机部署偏好条目"}],
              actor="agent-A", namespace="agent", subject=uid)
    hub.write([{"kind": "fact", "content": "共享域：集群部署规范条目"}],
              actor="agent-A", namespace="shared-kb", subject=uid)
    hits = hub.recall("部署规范偏好", subject=uid,
                      namespace=["agent", "shared-kb"])
    src = {h["namespace"] for h in hits}
    assert src == {"agent", "shared-kb"}
    # 单域查询行为不变（默认 dialogue 域隔离依旧）
    assert all(h["namespace"] == "agent"
               for h in hub.recall("部署", subject=uid, namespace="agent"))


# ---------------- 本地知识库入库（ingest 产品入口） ----------------

def test_ingest_folder_and_recall(hub, tmp_path):
    """文件夹入库：切块条目可召回、文件级溯源条目挂原件、重复导入幂等。"""
    from memory_hub.ingest import ingest_folder
    d = tmp_path / "docs"
    d.mkdir()
    (d / "notes.md").write_text(
        "# 对账规范\n\n对账流程：先导出银行流水，再与系统订单逐笔核对。\n\n"
        "# 报销制度\n\n报销需在费用发生后 30 天内提交发票。", encoding="utf-8")
    (d / "memo.txt").write_text("办公室网络密码每月轮换一次。", encoding="utf-8")

    st = ingest_folder(hub, str(d), subject="kb")
    assert st["files"] == 2 and st["chunks"] >= 2 and st["files_attached"] >= 1
    hits = hub.recall("对账怎么核对", subject="kb")
    assert any("银行流水" in h["content"] for h in hits)
    hits2 = hub.recall("报销时限", subject="kb")
    assert any("30 天" in h["content"] for h in hits2)
    # 附件可读回
    proc = [m for m in hub.list_memories(subject="kb")
            if m["kind"] == "procedure"
            and "notes.md" in m["source_session"]][0]
    import json as _json
    fid = _json.loads(proc["file_ids"])[0]
    f = hub.read_memory_file(fid)
    assert f["name"] == "notes.md" and "银行流水" in f["content"]
    # 重复导入：内容相同 → 全部去重，不产生重复条目
    st2 = ingest_folder(hub, str(d), subject="kb")
    assert st2["deduped"] >= st2["chunks"] or st2["chunks"] == 0


def test_ingest_chunking():
    from memory_hub.ingest import _chunks
    paras = ["段%d" % i * 60 for i in range(5)]  # 每段 120 字
    text = "\n\n".join(paras)
    cs = _chunks(text, 400)
    assert all(len(c) <= 400 for c in cs)
    assert "".join(c.replace("\n", "") for c in cs).count("段") == 5 * 60
    assert _chunks("短文本") == ["短文本"]


def test_ingest_idempotent_no_churn(hub, tmp_path):
    """同内容重复导入零动作：不新增、不下架（曾因 write 截断口径不一致
    导致 6 块被误下架重加）。"""
    from memory_hub.ingest import ingest_folder
    d = tmp_path / "docs2"
    d.mkdir()
    body = "\n\n".join("# 小节 %d\n\n内容第%d段：填一些真实长度的说明文字。" % (i, i)
                       for i in range(30))
    (d / "doc.md").write_text(body, encoding="utf-8")
    ingest_folder(hub, str(d), subject="kb2")
    before = {m["memory_id"] for m in hub.list_memories(subject="kb2", limit=10**6)}
    st = ingest_folder(hub, str(d), subject="kb2")
    after = {m["memory_id"] for m in hub.list_memories(subject="kb2", limit=10**6)}
    assert st["chunks"] == 0 and after == before


def test_ingest_update_supersedes_old(hub, tmp_path):
    """文档修订重导：旧口径块自动下架（事件留痕）、新口径可召回；
    文档级溯源索引被取代而非堆积。"""
    from memory_hub.ingest import ingest_folder
    d = tmp_path / "docs3"
    d.mkdir()
    f = d / "policy.md"
    f.write_text("# 运维约定\n\n发布窗口为每周六 22:00-24:00，须双人复核。",
                 encoding="utf-8")
    ingest_folder(hub, str(d), subject="kb3")
    # 修订口径
    f.write_text("# 运维约定\n\n发布窗口为每周日 23:00 起，须双人复核。",
                 encoding="utf-8")
    ingest_folder(hub, str(d), subject="kb3")
    hits = hub.recall("发布窗口是几点", subject="kb3")
    assert hits and any("23:00" in h["content"] for h in hits)
    assert all("22:00" not in h["content"] for h in hits)
    evs = [e for e in hub.events(subject="kb3", limit=10**6)
           if e["op"] == "invalidated" and "文档更新" in (e["note"] or "")]
    assert evs                                   # 更新消解留痕
    procs = [m for m in hub.list_memories(subject="kb3")
             if m["kind"] == "procedure"]
    assert len(procs) == 1                       # 溯源索引不堆积
