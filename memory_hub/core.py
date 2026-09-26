# -*- coding: utf-8 -*-
"""MemoryHub 内核（M8a）：三层记忆 + 事件审计 + 向量/关键词 RRF 召回 + invalidate。

设计要点（DESIGN-MEMORY.md）：
- 冲突消解 = 双时间戳 invalidate（Graphiti 范式）：矛盾旧条目 status=invalidated
  不删除，事件留痕；默认召回只取有效条目。
- 向量召回 = 复用 retrieval/embed（bge 512 维 BLOB）；写时同步嵌入，失败标
  vec_stale 由 maintain 补嵌。
- 生命周期 = expires_at TTL + recency 衰减（排序降权非过滤）+ forget 软删。
- 作用域 = (namespace, subject)；ACL 召回前过滤（照 hybrid 契约）。
- 纪律：任何失败静默降级（向量不可用→关键词单路），绝不抛给对话主链路；
  SQLite 写走 dbutil.LOCK + retry_db（与 SessionManager 同库同锁）。
"""
import hashlib
import json
import math
import os
import sqlite3
import struct
import uuid
from contextlib import contextmanager

from .dbutil import LOCK, now, retry_db
from . import deps, schema

KINDS = ("preference", "fact", "decision", "person", "project", "procedure")

# procedure 记忆挂文件引用（MemFS-lite）：内容存 memory_files 表（文本、sha 去重）
MAX_FILE_BYTES = 64 * 1024

# 召回/去重阈值（M8a-3 用真实 bge-small-zh 实证校准，见 DESIGN-MEMORY.md §阈值）
# 实测分布：无关对≈0.34-0.58、同义对≈0.67-0.85、矛盾对≈0.78——绝对值整体偏低，
# 阈值按相对分布取：召回 0.40（边缘无关对靠 RRF 排序压后）/ 冲突候选 0.60 / 判重 0.85
VEC_THRESHOLD = 0.40      # 召回下限（进入向量排名表）
NEAR_DUP_SIM = 0.85       # ≥ 视为重复，丢弃新条目
CONFLICT_CANDIDATE_SIM = 0.60   # ≥ 才触发 LLM 冲突比对

_RECENCY_HALF_LIFE_DAYS = 30.0   # recency 衰减半衰期

CONFLICT_SYSTEM = """你是记忆冲突裁决器。已有记忆与新记忆语义高度相近，判断两者关系。

只输出 JSON：
- {"relation": "duplicate"}：意思完全相同
- {"relation": "contradict"}：新记忆推翻/取代旧记忆（如偏好改口、值更新）
- {"relation": "coexist"}：各有信息，不冲突

relation 的值必须严格使用英文小写词 duplicate / contradict / coexist，禁止中文或其它写法。"""

CONFLICT_SYSTEM_BATCH = """你是记忆冲突裁决器。一条新记忆与多条已有记忆语义相近，逐一判断关系。

只输出 JSON：
{"relations": [
  {"id": 1, "relation": "duplicate"},
  {"id": 2, "relation": "contradict"},
  {"id": 3, "relation": "coexist"}
]}
relation 含义：duplicate=意思完全相同；contradict=新记忆推翻/取代该旧记忆
（偏好改口、值更新）；coexist=各有信息不冲突。
每条已有记忆都必须给出判定；无法确定时用 coexist（保守不动旧记忆）。
relation 的值必须严格使用英文小写词 duplicate / contradict / coexist，禁止中文或其它写法。"""


def _norm_relation(v) -> str:
    """relation 值归一：模型可能回中文（实测 qwen 系回「冲突／重复／共存」）。

    先判否定（「不冲突」含「冲突」，须先于冲突词命中），未知值 → coexist（保守）。"""
    if not isinstance(v, str):
        return "coexist"
    k = v.strip().lower().replace(" ", "").replace("_", "")
    if k in ("duplicate", "contradict", "coexist"):
        return k
    if any(w in k for w in ("不冲突", "无冲突", "共存", "并存", "coexist", "both")):
        return "coexist"
    if any(w in k for w in ("矛盾", "冲突", "取代", "推翻", "contradict",
                            "conflict", "replace")):
        return "contradict"
    if any(w in k for w in ("重复", "相同", "一致", "duplicate", "dupe", "same")):
        return "duplicate"
    return "coexist"

# 时间意图提示词（P2）：命中则召回连同失效历史一起给（Mem0 temporal 思路 +
# 本模块 invalidate 双时间戳的差异化落地）
_PAST_HINTS = ("之前", "以前", "原来", "早先", "曾经", "上次", "历史")


def _entities_of(m) -> list:
    """条目 entities 列（JSON 数组）安全解析。"""
    raw = m.get("entities") if isinstance(m, dict) else None
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _cos(a: bytes, b) -> float:
    """BLOB vs BLOB/list 余弦（bge 归一化向量即点积，此处仍按通用余弦实现）。"""
    if not a or not b:
        return 0.0
    try:
        va = struct.unpack("%df" % (len(a) // 4), a)
        if isinstance(b, (bytes, bytearray)):
            vb = struct.unpack("%df" % (len(b) // 4), b)
        else:
            vb = tuple(b)
        n = min(len(va), len(vb))
        dot = sum(va[i] * vb[i] for i in range(n))
        na = math.sqrt(sum(x * x for x in va[:n])) or 1e-9
        nb = math.sqrt(sum(x * x for x in vb[:n])) or 1e-9
        return dot / (na * nb)
    except Exception:
        return 0.0


def _bigrams(t: str) -> set:
    t = t or ""
    return {t[i:i + 2] for i in range(len(t) - 1) if not t[i].isspace()}


def _kw_score(query_grams: set, text: str) -> int:
    return sum(1 for g in query_grams if g in (text or ""))


class WriteReceipt(dict):
    """write() 返回回执：{added, deduped, invalidated, episodes, profile_updated}。"""


class MemoryHub:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(deps.runtime_dir(), "dialogue.db")
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        retry_db(self._init_schema)

    # ---------------- 基础设施 ----------------
    def _init_schema(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    pass
                conn.executescript(schema.MEMORIES_BASE)
                conn.executescript(schema.SCHEMA)
                for ddl in schema.MEMORIES_MIGRATE:
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass          # 列已存在
                # 旧库基表存在时 CREATE TABLE IF NOT EXISTS 不补新列，
                # 上面的 ALTER 补完后才能建引用新列的索引
                conn.executescript(schema.MEMORIES_INDEX)
        finally:
            conn.close()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _write(self, fn):
        """写操作：全局锁 + 事务 + 锁库退避。"""
        def _op():
            with LOCK, self._conn() as c:
                return fn(c)
        return retry_db(_op)

    def _read(self, fn):
        def _op():
            with self._conn() as c:
                return fn(c)
        return retry_db(_op)

    # ---------------- 事件流 ----------------
    @staticmethod
    def _event(c, memory_id, op, from_status="", to_status="", actor="system",
               note=""):
        c.execute("INSERT INTO memory_events(memory_id,op,from_status,to_status,"
                  "actor,note,ts) VALUES(?,?,?,?,?,?,?)",
                  (memory_id, op, from_status, to_status, actor, note, now()))

    # ---------------- namespace 治理（多 Agent 共享/隔离） ----------------
    NS_POLICIES = ("open", "restricted")

    def register_namespace(self, namespace, owner, policy="open") -> bool:
        """注册/更新命名空间。policy：open=任何人可写；restricted=仅 owner 可写
        （其余 Agent 只读——共享记忆防误写）。重复注册覆盖策略。"""
        if policy not in self.NS_POLICIES:
            return False

        def _op(c):
            c.execute(
                "INSERT INTO namespaces(namespace,owner,policy,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(namespace) DO UPDATE SET "
                "owner=excluded.owner, policy=excluded.policy",
                (namespace, owner, policy, now()))
            self._event(c, "namespace:%s" % namespace, "namespace-registered",
                        "", "", owner, policy)
        self._write(_op)
        return True

    def list_namespaces(self) -> list:
        rows = self._read(lambda c: c.execute(
            "SELECT namespace, owner, policy, created_at FROM namespaces "
            "ORDER BY namespace").fetchall())
        return [dict(r) for r in rows]

    def _can_write(self, namespace, actor_name) -> bool:
        """未注册命名空间默认开放（向后兼容）；restricted 仅 owner 可写。"""
        row = self._read(lambda c: c.execute(
            "SELECT owner, policy FROM namespaces WHERE namespace=?",
            (namespace,)).fetchone())
        if row is None:
            return True
        return row["policy"] == "open" or row["owner"] == actor_name

    # ---------------- L2 写入（含去重与冲突消解） ----------------
    def write(self, items, actor="system", namespace="dialogue", subject="",
              source_session="", tenant="yunpai",
              embed_now=True) -> WriteReceipt:
        """items: [{kind, content, confidence?, expires_at?, access?}]。
        幂等安全；去重（包含+余弦）→ 冲突消解（LLM 比对相近条目）→ 落库+事件。"""
        receipt = WriteReceipt(added=[], deduped=[], invalidated=[],
                               profile_updated=False, denied=[])
        if not items:
            return receipt
        role = actor if isinstance(actor, str) else (actor or {}).get("role", "system")
        actor_name = role if isinstance(actor, str) else \
            (actor or {}).get("user_id", "system")
        # namespace 写权限：restricted 且非 owner → 静默拒绝（不抛异常）
        if not self._can_write(namespace, actor_name):
            receipt["denied"].append(namespace)
            return receipt

        for it in items:
            content = str(it.get("content", "")).strip()[:500]
            if not (2 <= len(content) <= 500):
                continue
            kind = it.get("kind") if it.get("kind") in KINDS else "fact"
            vec = deps.embed_one(content) if embed_now else None
            file_ids = self._store_files(it.get("files") or [])

            existing = self._read(lambda c: c.execute(
                "SELECT memory_id, content, kind, status, vec FROM memories "
                "WHERE namespace=? AND subject=? AND status='active'",
                (namespace, subject)).fetchall())

            # 1) 去重：双向包含 或 余弦≥0.92
            dup = None
            for e in existing:
                if content in e["content"] or e["content"] in content:
                    dup = e["memory_id"]
                    break
                if vec is not None and e["vec"] is not None \
                        and _cos(e["vec"], vec) >= NEAR_DUP_SIM:
                    dup = e["memory_id"]
                    break
            if dup:
                receipt["deduped"].append(dup)
                continue

            # 2) 冲突消解：相近候选（余弦≥0.60 且同 kind）单次 LLM 批量裁决
            #    （P1：N 次调用并 1 次；id 先取：被推翻条目的 superseded_by 指向本条）
            mid = "m-" + uuid.uuid4().hex[:10]
            ts = now()
            invalidated = []
            if vec is not None:
                candidates = [e for e in existing if e["vec"] is not None
                              and e["kind"] == kind
                              and _cos(e["vec"], vec) >= CONFLICT_CANDIDATE_SIM]
                if candidates:
                    relations = self._judge_relations(
                        content, [c["content"] for c in candidates])
                    for i, cand in enumerate(candidates, 1):
                        relation = relations.get(i, "coexist")
                        if relation == "contradict":
                            invalidated.append(cand["memory_id"])
                        elif relation == "duplicate":
                            receipt["deduped"].append(cand["memory_id"])
                            invalidated = []   # 撤销已标记，直接按去重处理
                            break
            if invalidated:
                self._write(lambda c: [
                    self._invalidate_row(c, _mid, actor_name, "被新记忆取代",
                                         superseded_by=mid)
                    for _mid in invalidated])
                receipt["invalidated"].extend(invalidated)

            # 3) 落库（entities：调用方/抽取器可传，召回实体路用）
            entities = list(dict.fromkeys(
                str(x).strip()[:30] for x in (it.get("entities") or [])
                if str(x).strip()))[:5]
            vec_blob = struct.pack("%df" % len(vec), *vec) if vec else None

            def _insert(c):
                c.execute(
                    "INSERT INTO memories(memory_id,tenant,user_id,role,kind,content,"
                    "source_session,created_at,updated_at,active,vec,vec_stale,access,"
                    "status,valid_at,expires_at,version,namespace,confidence,subject,"
                    "file_ids,entities) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1,?,0,?,?,?,?,1,?,?,?,?,?)",
                    (mid, tenant, subject, role, kind, content,
                     it.get("source_session") or source_session,
                     ts, ts, vec_blob, it.get("access", "internal"), "active",
                     ts, it.get("expires_at"), namespace,
                     it.get("confidence"), subject,
                     json.dumps(file_ids) if file_ids else "",
                     json.dumps(entities, ensure_ascii=False) if entities else ""))
                self._event(c, mid, "extracted", "", "active", actor_name,
                            source_session and ("session " + source_session) or "")
            self._write(_insert)
            receipt["added"].append({"memory_id": mid, "kind": kind,
                                     "content": content, "file_ids": file_ids,
                                     "entities": entities})

        # 4) 偏好投影到 L1 profile（kind=preference 的条目）
        prefs = [a["content"] for a in receipt["added"]
                 if a["kind"] == "preference"]
        if prefs:
            receipt["profile_updated"] = self.update_profile(
                namespace, subject, prefs, actor=actor_name)
        return receipt

    # ---------------- procedure 文件引用（MemFS-lite） ----------------
    def _store_files(self, files) -> list:
        """files: [{name, content}]（文本，≤64KB）。sha 去重复用；任何失败跳过该项。"""
        out = []
        for f in files:
            try:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("name", "file")).strip()[:200] or "file"
                content = f.get("content", "")
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                content = str(content or "")
                if not content or len(content.encode("utf-8")) > MAX_FILE_BYTES:
                    continue
                sha = hashlib.sha256(
                    ("%s\0%s" % (name, content)).encode("utf-8")).hexdigest()
                row = self._read(lambda c: c.execute(
                    "SELECT file_id FROM memory_files WHERE sha=?",
                    (sha,)).fetchone())
                if row:
                    out.append(row["file_id"])
                    continue
                fid = "f-" + uuid.uuid4().hex[:10]
                self._write(lambda c: c.execute(
                    "INSERT INTO memory_files(file_id,name,sha,size,content,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (fid, name, sha, len(content.encode("utf-8")), content,
                     now())))
                out.append(fid)
            except Exception:
                continue
        return out

    @staticmethod
    def _file_ids(m) -> list:
        if m is None:
            return []
        if not isinstance(m, dict):
            try:
                m = dict(m)
            except Exception:
                return []
        raw = m.get("file_ids")
        if not raw:
            return []
        try:
            ids = json.loads(raw)
            return ids if isinstance(ids, list) else []
        except Exception:
            return []

    def attach_files(self, memory_id, files, actor="system") -> list:
        """给已有记忆追加文件引用（版本+1，事件留痕）。"""
        row = self._read(lambda c: c.execute(
            "SELECT file_ids FROM memories WHERE memory_id=? AND active=1",
            (memory_id,)).fetchone())
        if row is None:
            return []
        new_ids = self._store_files(files or [])
        if not new_ids:
            return []
        merged = [x for x in self._file_ids(row) if x not in new_ids] + new_ids

        def _op(c):
            c.execute("UPDATE memories SET file_ids=?, version=version+1, "
                      "updated_at=? WHERE memory_id=?",
                      (json.dumps(merged), now(), memory_id))
            self._event(c, memory_id, "files-attached", "active", "active",
                        actor, "+%d 文件" % len(new_ids))
        self._write(_op)
        return new_ids

    def read_memory_file(self, file_id) -> dict:
        row = self._read(lambda c: c.execute(
            "SELECT file_id, name, sha, size, content, created_at "
            "FROM memory_files WHERE file_id=?", (file_id,)).fetchone())
        return dict(row) if row else None

    def memory_files(self, memory_id) -> list:
        """某条记忆挂载的全部文件元信息（不含 content）。"""
        row = self._read(lambda c: c.execute(
            "SELECT file_ids FROM memories WHERE memory_id=?",
            (memory_id,)).fetchone())
        if row is None:
            return []
        out = []
        for fid in self._file_ids(row):
            f = self.read_memory_file(fid)
            if f:
                out.append({k: f[k] for k in ("file_id", "name", "sha",
                                              "size", "created_at")})
        return out

    @staticmethod
    def _judge_relation(old_content: str, new_content: str) -> str:
        """LLM 决断两条高度相近记忆的关系；失败返回 coexist（保守不动旧记忆）。"""
        content = deps.llm_chat(
            [{"role": "system", "content": CONFLICT_SYSTEM},
             {"role": "user", "content": "旧记忆：%s\n新记忆：%s" % (
                 old_content[:200], new_content[:200])}],
            temperature=0.0)
        if not content:
            return "coexist"
        data = deps.parse_json(content)
        if isinstance(data, dict) and data.get("relation") is not None:
            return _norm_relation(data["relation"])
        return "coexist"

    @staticmethod
    def _judge_relations(new_content: str, candidates: list) -> dict:
        """单次 LLM 批量裁决 {序号: relation}（P1：N 次调用并 1 次降本提速）。

        兼容旧单条格式 {"relation": ...}（应用到全部候选——单候选常见路径）；
        任何失败全部返回 coexist（保守不动旧记忆）。"""
        lines = "\n".join("%d. %s" % (i, c[:200])
                          for i, c in enumerate(candidates, 1))
        content = deps.llm_chat(
            [{"role": "system", "content": CONFLICT_SYSTEM_BATCH},
             {"role": "user", "content": "已有记忆：\n%s\n\n新记忆：%s" % (
                 lines, new_content[:200])}],
            temperature=0.0)
        out = {i: "coexist" for i in range(1, len(candidates) + 1)}
        if not content:
            return out
        data = deps.parse_json(content)
        if isinstance(data, dict) and isinstance(data.get("relations"), list):
            for item in data["relations"]:
                if isinstance(item, dict) and item.get("id") in out \
                        and item.get("relation") is not None:
                    out[item["id"]] = _norm_relation(item["relation"])
        elif isinstance(data, dict) and data.get("relation") is not None:
            rel = _norm_relation(data["relation"])
            out = {i: rel for i in out}
        return out

    # ---------------- 生命周期 ----------------
    def _invalidate_row(self, c, memory_id, actor, reason, superseded_by=None):
        c.execute("UPDATE memories SET status='invalidated', invalid_at=?, "
                  "superseded_by=COALESCE(?, superseded_by), updated_at=? "
                  "WHERE memory_id=?",
                  (now(), superseded_by, now(), memory_id))
        self._event(c, memory_id, "invalidated", "active", "invalidated",
                    actor, reason)

    def invalidate(self, memory_id, actor="system", reason="") -> bool:
        n = self._write(lambda c: c.execute(
            "UPDATE memories SET status='invalidated', invalid_at=?, updated_at=? "
            "WHERE memory_id=? AND status='active'",
            (now(), now(), memory_id)).rowcount)
        if n:
            self._write(lambda c: self._event(
                c, memory_id, "invalidated", "active", "invalidated", actor, reason))
        return bool(n)

    def forget(self, memory_id, actor="system", reason="") -> bool:
        """人工遗忘：软删（active=0, status=archived），事件留痕。"""
        n = self._write(lambda c: c.execute(
            "UPDATE memories SET active=0, status='archived', updated_at=? "
            "WHERE memory_id=? AND status!='archived'",
            (now(), memory_id)).rowcount)
        if n:
            self._write(lambda c: self._event(
                c, memory_id, "forgot", "active", "archived", actor, reason))
        return bool(n)

    def reactivate(self, memory_id, actor="system") -> bool:
        n = self._write(lambda c: c.execute(
            "UPDATE memories SET status='active', active=1, invalid_at=NULL, "
            "superseded_by=NULL, updated_at=? WHERE memory_id=?",
            (now(), memory_id)).rowcount)
        if n:
            self._write(lambda c: self._event(
                c, memory_id, "reactivated", "invalidated", "active", actor))
        return bool(n)

    def edit(self, memory_id, content, actor="system") -> bool:
        """人工编辑：内容+向量重建+版本+1（旧值进事件 note，可审计）。"""
        content = str(content or "").strip()[:500]
        if not content:
            return False
        old = self._read(lambda c: c.execute(
            "SELECT content, version FROM memories WHERE memory_id=?",
            (memory_id,)).fetchone())
        if old is None:
            return False
        vec = deps.embed_one(content)
        vec_blob = struct.pack("%df" % len(vec), *vec) if vec else None
        self._write(lambda c: c.execute(
            "UPDATE memories SET content=?, vec=?, vec_stale=?, version=version+1, "
            "updated_at=? WHERE memory_id=?",
            (content, vec_blob, 0 if vec_blob else 1, now(), memory_id)))
        self._write(lambda c: self._event(
            c, memory_id, "edited", "active", "active", actor,
            "旧值：%s" % old["content"][:120]))
        return True

    # ---------------- 召回（L2 向量+关键词+实体 RRF；含 TTL/recency/时间意图） ----------------
    def recall(self, query, subject, namespace="dialogue", visible=None,
               k=5, include_invalidated=False, filters=None,
               rerank=False) -> list:
        """namespace 可传 list（多 Agent 融合召回：自己的 + 共享的），命中带来源域。

        - 时间意图（P2）：查询含「之前/原来/历史」等 → 自动连同失效历史召回；
        - filters（P3）：{"kind": "fact", "since": "2026-01-01", "until": ...}
          （created_at 字符串区间，含端点）；
        - rerank（P3）：LLM 二段精排，需注入 LLM，失败保持 RRF 原序；
        - 命中条目 hit_count+1（P3 反馈信号，失败静默）。"""
        q = (query or "").strip()
        if not q:
            return []
        if any(h in q for h in _PAST_HINTS):
            include_invalidated = True      # 时间意图：历史问句连旧版本一起给
        ns_list = list(namespace) if isinstance(namespace, (list, tuple)) \
            else [namespace]
        rows = []
        for ns in ns_list:
            rows.extend(self._read(lambda c: c.execute(
                "SELECT memory_id, namespace, kind, content, source_session, "
                "access, status, updated_at, created_at, expires_at, vec, "
                "file_ids, entities, hit_count "
                "FROM memories WHERE namespace=? AND subject=? AND active=1",
                (ns, subject)).fetchall()))
        # 状态过滤：默认只取有效；TTL 到期视同 invalidated
        ts_now = now()
        out = []
        for r in rows:
            if include_invalidated:
                if r["status"] == "archived":
                    continue
            elif r["status"] != "active":
                continue
            if r["expires_at"] and r["expires_at"] <= ts_now and r["status"] == "active":
                self._write(lambda c, _mid=r["memory_id"]: self._invalidate_row(
                    c, _mid, "system", "TTL 到期"))
                continue
            if visible is not None and r["access"] not in visible:
                continue
            out.append(dict(r))
        if filters:                          # 结构化过滤（P3）
            fk, since, until = filters.get("kind"), filters.get("since"), \
                filters.get("until")
            out = [m for m in out
                   if (not fk or m["kind"] == fk)
                   and (not since or (m.get("created_at") or "") >= since)
                   and (not until or (m.get("created_at") or "") <= until)]
        if not out:
            return []

        q_vec = deps.embed_one(q)

        grams = _bigrams(q)
        scored = []
        ent_rank = []
        for m in out:
            kw = _kw_score(grams, m["content"])
            ents = _entities_of(m)
            if ents and sum(1 for e in ents if e in q) > 0:
                ent_rank.append(m["memory_id"])   # 实体重叠（P2，Mem0 第三路）
            sim = _cos(m["vec"], q_vec) if (q_vec and m["vec"]) else 0.0
            scored.append((m, kw, sim))
        # 各路先按本路分数降序再进 RRF（名次=相关度而非库返回序）
        kw_rank = [mid for _, mid in sorted(
            ((kw, m["memory_id"]) for m, kw, _ in scored if kw > 0),
            key=lambda x: -x[0])]
        vec_rank = [mid for _, mid in sorted(
            ((sim, m["memory_id"]) for m, _, sim in scored if sim >= VEC_THRESHOLD),
            key=lambda x: -x[0])]
        # 无任何命中 → 最近 3 条背景（保持在场感，沿用旧 recall 语义）
        if not vec_rank and not kw_rank and not ent_rank:
            scored.sort(key=lambda x: x[0]["updated_at"] or "", reverse=True)
            hits = [self._to_hit(m) for m, _, _ in scored[:min(k, 3)]]
            self._bump_hits(h["memory_id"] for h in hits)
            return hits

        # RRF(k=60) + recency 衰减（排序降权非过滤）
        rrf = {}
        for rank_list in (vec_rank, kw_rank, ent_rank):
            for r, mid in enumerate(rank_list):
                rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (60 + r + 1)

        def _recency_factor(updated_at: str) -> float:
            import datetime
            try:
                d = datetime.datetime.strptime(updated_at or "", "%Y-%m-%d %H:%M:%S")
                days = max(0.0, (datetime.datetime.now() - d).total_seconds() / 86400)
            except ValueError:
                days = 0.0
            return 0.7 + 0.3 * math.pow(0.5, days / _RECENCY_HALF_LIFE_DAYS)

        for m, _, sim in scored:
            if m["memory_id"] in rrf:
                rrf[m["memory_id"]] += 0.15 * sim            # 向量分数微加成
                rrf[m["memory_id"]] *= _recency_factor(m["updated_at"])
        ranked = [m for m, _, _ in scored if m["memory_id"] in rrf]
        ranked.sort(key=lambda m: -rrf[m["memory_id"]])
        if rerank and deps.llm_available() and len(ranked) > 1:
            ranked = self._rerank(q, ranked)
        hits = [self._to_hit(m) for m in ranked[:k]]
        self._bump_hits(h["memory_id"] for h in hits)
        return hits

    @staticmethod
    def _rerank(query, memories):
        """LLM 二段精排（P3）：按相关度重排，失败/解析失败保持原序。"""
        try:
            listing = "\n".join("%d. %s" % (i, m["content"][:120])
                                for i, m in enumerate(memories, 1))
            content = deps.llm_chat(
                [{"role": "system", "content":
                  "你是记忆检索精排器。按与查询的相关度从高到低重排候选编号。"
                  "只输出 JSON：{\"order\": [3,1,2]}，不要解释。"},
                 {"role": "user", "content": "查询：%s\n候选：\n%s" % (
                     query, listing)}],
                temperature=0.0)
            data = deps.parse_json(content) if content else None
            order = data.get("order") if isinstance(data, dict) else None
            if isinstance(order, list) and order:
                pairs = list(enumerate(memories, 1))
                idx = {n: r for r, n in enumerate(
                    x for x in order
                    if isinstance(x, int) and 1 <= x <= len(memories))}
                if idx:
                    pairs.sort(key=lambda p: idx.get(p[0], len(memories)))
                    return [m for _, m in pairs]
        except Exception:
            pass
        return memories

    def _bump_hits(self, mids):
        """召回反馈（P3）：命中条目 hit_count+1，失败静默（读路径不冒错）。
        批量单事务（压测修复：逐条 _write 各开连接+锁，recall 延迟 3 倍）。"""
        ids = [m for m in mids if m]
        if not ids:
            return
        try:

            def _op(c):
                c.executemany(
                    "UPDATE memories SET hit_count=hit_count+1 WHERE memory_id=?",
                    [(m,) for m in ids])
            self._write(_op)
        except Exception:
            pass

    @staticmethod
    def _to_hit(m: dict) -> dict:
        return {"memory_id": m["memory_id"], "kind": m["kind"],
                "content": m["content"],
                "source_session": m.get("source_session") or "",
                "status": m["status"],
                "file_ids": MemoryHub._file_ids(m),
                "entities": _entities_of(m),
                "hit_count": m.get("hit_count") or 0,
                "namespace": m.get("namespace") or "dialogue"}

    # ---------------- L1 profile ----------------
    def profile(self, subject, namespace="dialogue") -> dict:
        row = self._read(lambda c: c.execute(
            "SELECT persona_json, version, updated_at FROM profiles "
            "WHERE namespace=? AND subject=?",
            (namespace, subject)).fetchone())
        if row is None:
            return {"namespace": namespace, "subject": subject,
                    "persona": {}, "version": 0}
        return {"namespace": namespace, "subject": subject,
                "persona": json.loads(row["persona_json"] or "{}"),
                "version": row["version"],
                "updated_at": row["updated_at"]}

    def update_profile(self, namespace, subject, new_facts, actor="system") -> bool:
        """偏好/画像合并进 L1 单文档（LLM 合并失败则简单追加，永不抛异常）。"""
        cur = self.profile(subject, namespace)
        persona = dict(cur.get("persona") or {})
        merged = self._merge_persona(persona, new_facts)
        version = int(cur.get("version") or 0) + 1

        def _op(c):
            c.execute(
                "INSERT INTO profiles(namespace,subject,persona_json,version,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(namespace,subject) DO UPDATE SET "
                "persona_json=excluded.persona_json, version=excluded.version, "
                "updated_at=excluded.updated_at",
                (namespace, subject, json.dumps(merged, ensure_ascii=False),
                 version, now()))
            self._event(c, "profile:%s/%s" % (namespace, subject),
                        "profile-updated", "", "", actor,
                        "v%d：+%d 条" % (version, len(new_facts)))
        self._write(_op)
        return True

    @staticmethod
    def _merge_persona(persona: dict, new_facts: list) -> dict:
        if not deps.llm_available():
            prefs = list(persona.get("preferences") or [])
            prefs.extend(f for f in new_facts if f not in prefs)
            return {"preferences": prefs[:30]}
        content = deps.llm_chat(
            [{"role": "system", "content": "把新偏好合并进用户画像 JSON。"
              "只输出合并后的 JSON（键自定，preferences 数组必含），不要解释。"},
             {"role": "user", "content": "当前画像：%s\n\n新偏好：%s" % (
                 json.dumps(persona, ensure_ascii=False)[:800],
                 json.dumps(new_facts, ensure_ascii=False)[:800])}],
            temperature=0.0)
        data = deps.parse_json(content) if content else None
        if isinstance(data, dict) and "preferences" in data:
            return data
        prefs = list(persona.get("preferences") or [])
        prefs.extend(f for f in new_facts if f not in prefs)
        return {"preferences": prefs[:30]}

    # ---------------- L3 episodes ----------------
    def add_episode(self, subject, summary, session_id="", turn_seq=0,
                    namespace="dialogue", actor="system") -> str:
        eid = "ep-" + uuid.uuid4().hex[:10]

        def _op(c):
            c.execute("INSERT INTO episodes(episode_id,namespace,subject,session_id,"
                      "turn_seq,summary,ts) VALUES(?,?,?,?,?,?,?)",
                      (eid, namespace, subject, session_id, turn_seq,
                       (summary or "")[:1000], now()))
        self._write(_op)
        return eid

    def episodes(self, subject, namespace="dialogue", limit=20) -> list:
        rows = self._read(lambda c: c.execute(
            "SELECT * FROM episodes WHERE namespace=? AND subject=? "
            "ORDER BY ts DESC LIMIT ?", (namespace, subject, limit)).fetchall())
        return [dict(r) for r in rows]

    # ---------------- 查询 / 审计 ----------------
    def list_memories(self, subject=None, namespace="dialogue",
                      include_invalidated=False, include_archived=False,
                      limit=200) -> list:
        q = "SELECT * FROM memories WHERE namespace=?"
        args = [namespace]
        if subject is not None:
            q += " AND subject=?"
            args.append(subject)
        if include_archived:
            pass                          # 全部状态
        elif include_invalidated:
            q += " AND active=1"          # active + invalidated
        else:
            q += " AND active=1 AND status='active'"
        q += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        rows = self._read(lambda c: c.execute(q, args).fetchall())
        return [dict(r) for r in rows]

    def get_memory(self, memory_id):
        row = self._read(lambda c: c.execute(
            "SELECT * FROM memories WHERE memory_id=?", (memory_id,)).fetchone())
        return dict(row) if row else None

    def events(self, memory_id=None, subject=None, namespace="dialogue",
               limit=100) -> list:
        """审计回放：按记忆条目或按 subject（含 profile 事件）。"""
        if memory_id:
            rows = self._read(lambda c: c.execute(
                "SELECT * FROM memory_events WHERE memory_id=? ORDER BY event_id "
                "LIMIT ?", (memory_id, limit)).fetchall())
        else:
            prefix = "profile:%s/" % namespace
            rows = self._read(lambda c: c.execute(
                "SELECT e.* FROM memory_events e JOIN memories m "
                "ON m.memory_id=e.memory_id WHERE m.namespace=? AND m.subject=? "
                "UNION ALL SELECT * FROM memory_events WHERE memory_id LIKE ? "
                "ORDER BY event_id LIMIT ?",
                (namespace, subject or "", prefix + "%", limit)).fetchall())
        return [dict(r) for r in rows]

    def find_by_content(self, fragment, subject=None, namespace="dialogue") -> list:
        frag = (fragment or "").strip()
        rows = self.list_memories(subject=subject, namespace=namespace,
                                  include_invalidated=True)
        if not frag:
            return []
        return [m for m in rows
                if frag in m["content"] or m["content"] in frag]

    def related(self, memory_id) -> dict:
        """supersede 溯源（图遍历轻量版，Graphiti invalidate 链的落地）：
        前向链 supersede_chain = 本条被谁取代（逐级追到最新有效版本）；
        后向 supersedes = 本条取代过哪些条目（≤3 层）。空 dict = 条目不存在。"""
        row = self.get_memory(memory_id)
        if row is None:
            return {}
        chain, seen = [], {memory_id}
        cur = row.get("superseded_by")
        while cur and cur not in seen and len(chain) < 10:
            nxt = self.get_memory(cur)
            if nxt is None:
                break
            chain.append({"memory_id": cur, "content": nxt["content"],
                          "status": nxt["status"]})
            seen.add(cur)
            cur = nxt.get("superseded_by")
        back, frontier = [], [memory_id]
        for _ in range(3):
            nxt_ids = []
            for pid in frontier:
                rows = self._read(lambda c, _p=pid: c.execute(
                    "SELECT memory_id, content, status FROM memories "
                    "WHERE superseded_by=?", (_p,)).fetchall())
                for r in rows:
                    back.append(dict(r))
                    nxt_ids.append(r["memory_id"])
            if not nxt_ids:
                break
            frontier = nxt_ids
        return {"memory_id": memory_id, "status": row["status"],
                "superseded_by": row.get("superseded_by"),
                "supersede_chain": chain, "supersedes": back}

    # ---------------- 维护（maintain CLI 调用） ----------------
    def consolidate(self, namespace="dialogue", subject=None, actor="system",
                    dry_run=False, max_group=200) -> dict:
        """sleep-time 批量整理（Letta dreaming 精简版，M9）。

        在线写入只与写入时刻的存量比对，跨时段渗漏的重复/矛盾靠本任务补扫：
        按 (subject, kind) 分组全量比对——余弦≥NEAR_DUP_SIM 确定性判重合并
        （保留信息量大的长条目，另一条 invalidated 并挂 superseded_by）；
        [CONFLICT_CANDIDATE_SIM, NEAR_DUP_SIM) 且 LLM 可用时交裁决
        （duplicate 合并 / contradict 新胜旧）。无向量条目退化为双向包含判重。
        全部事件留痕；dry_run 只报告不动库。返回
        {groups, scanned, merged, conflicts, llm_judged}。
        """
        rep = {"groups": 0, "scanned": 0, "merged": 0, "conflicts": 0,
               "llm_judged": 0, "profiles_rebuilt": 0}
        dirty_profiles = set()      # 发生偏好失效的 subject → 整理后重建画像
        conds, args = "namespace=? AND status='active' AND active=1", [namespace]
        if subject is not None:
            conds += " AND subject=?"
            args.append(subject)
        rows = self._read(lambda c: c.execute(
            "SELECT memory_id, subject, kind, content, vec, created_at "
            "FROM memories WHERE " + conds, args).fetchall())
        rep["scanned"] = len(rows)
        groups = {}
        for r in rows:
            groups.setdefault((r["subject"], r["kind"]), []).append(dict(r))

        def _merge(keep, drop):
            rep["merged"] += 1
            if drop.get("kind") == "preference":
                dirty_profiles.add(drop["subject"])
            if not dry_run:
                self._write(lambda c: self._invalidate_row(
                    c, drop["memory_id"], actor, "sleep-time 判重合并",
                    superseded_by=keep["memory_id"]))

        def _supersede(old, new):
            rep["conflicts"] += 1
            if old.get("kind") == "preference":
                dirty_profiles.add(old["subject"])
            if not dry_run:
                self._write(lambda c: self._invalidate_row(
                    c, old["memory_id"], actor, "sleep-time 冲突消解",
                    superseded_by=new["memory_id"]))

        for (_subj, _kind), items in groups.items():
            rep["groups"] += 1
            if len(items) > max_group:   # 防御：超大组只整理最近 max_group 条
                items = sorted(items, key=lambda x: x["created_at"] or "",
                               reverse=True)[:max_group]
            acted = set()
            # 无向量条目：双向包含确定性判重
            nov = [m for m in items if not m["vec"]]
            for i in range(len(nov)):
                for j in range(i + 1, len(nov)):
                    a, b = nov[i], nov[j]
                    if a["memory_id"] in acted or b["memory_id"] in acted:
                        continue
                    if a["content"] in b["content"] or b["content"] in a["content"]:
                        keep, drop = (a, b) if len(a["content"]) >= len(b["content"]) \
                            else (b, a)
                        acted.add(drop["memory_id"])
                        _merge(keep, drop)
            # 有向量条目：余弦配对，相似度降序贪心处理
            wv = [m for m in items if m["vec"] and m["memory_id"] not in acted]
            pairs = []
            for i in range(len(wv)):
                for j in range(i + 1, len(wv)):
                    sim = _cos(wv[i]["vec"], wv[j]["vec"])
                    if sim >= CONFLICT_CANDIDATE_SIM:
                        pairs.append((sim, wv[i], wv[j]))
            pairs.sort(key=lambda p: -p[0])
            for sim, a, b in pairs:
                if a["memory_id"] in acted or b["memory_id"] in acted:
                    continue
                if sim < NEAR_DUP_SIM:
                    if not deps.llm_available():
                        continue
                    rep["llm_judged"] += 1
                    older, newer = sorted((a, b),
                                          key=lambda x: x["created_at"] or "")
                    relation = self._judge_relation(older["content"],
                                                    newer["content"])
                    if relation == "contradict":
                        acted.add(older["memory_id"])
                        _supersede(older, newer)
                        continue
                    if relation != "duplicate":
                        continue          # coexist：各有信息，不动
                    a, b = older, newer
                keep, drop = (a, b) if len(a["content"]) >= len(b["content"]) \
                    else (b, a)
                acted.add(drop["memory_id"])
                _merge(keep, drop)
        # 画像回缩：偏好条目失效后 L1 不会自动剔除（写入只投影累加），
        # 以有效偏好为事实源重建，消除「改口后画像新旧并存」。
        if dirty_profiles and not dry_run:
            for subj in dirty_profiles:
                try:
                    self.rebuild_profile(namespace, subj, actor=actor)
                    rep["profiles_rebuilt"] += 1
                except Exception:
                    pass
        return rep

    def rebuild_profile(self, namespace, subject, actor="system") -> bool:
        """以当前有效 preference 条目重建 L1 画像（剔除已失效偏好）。

        persona = 从空画像合并全部有效偏好（LLM 归并同类，不可用退化为
        去重列表）；版本 = 旧版本 + 1，事件留痕。永不抛异常。"""
        rows = self._read(lambda c: c.execute(
            "SELECT content FROM memories WHERE namespace=? AND subject=? "
            "AND kind='preference' AND status='active' AND active=1 "
            "ORDER BY created_at", (namespace, subject)).fetchall())
        prefs = [r["content"] for r in rows]
        persona = self._merge_persona({"preferences": []}, prefs) if prefs \
            else {"preferences": []}
        cur = self.profile(subject, namespace)
        version = int(cur.get("version") or 0) + 1

        def _op(c):
            c.execute(
                "INSERT INTO profiles(namespace,subject,persona_json,version,"
                "updated_at) VALUES(?,?,?,?,?) ON CONFLICT(namespace,subject) "
                "DO UPDATE SET persona_json=excluded.persona_json, "
                "version=excluded.version, updated_at=excluded.updated_at",
                (namespace, subject, json.dumps(persona, ensure_ascii=False),
                 version, now()))
            self._event(c, "profile:%s/%s" % (namespace, subject),
                        "profile-updated", "", "", actor,
                        "v%d：重建（有效偏好 %d 条）" % (version, len(prefs)))
        self._write(_op)
        return True

    def maintain(self, actor="system", consolidate=False) -> dict:
        """确定性维护：TTL 清扫 + vec_stale 补嵌；
        consolidate=True 追加 sleep-time 存量整理（结果并入 out["consolidated"]）。"""
        out = {"expired": 0, "reembedded": 0}
        ts_now = now()
        expired = self._read(lambda c: c.execute(
            "SELECT memory_id FROM memories WHERE status='active' AND active=1 "
            "AND expires_at IS NOT NULL AND expires_at<=?",
            (ts_now,)).fetchall())
        if expired:
            # 批量单事务（压测修复：逐条 _write 各开连接+锁，500 条 2.2s → 单事务 <0.1s）
            mids = [r["memory_id"] for r in expired]

            def _expire(c):
                for _mid in mids:
                    self._invalidate_row(c, _mid, actor, "TTL 到期（maintain）")
            self._write(_expire)
            out["expired"] = len(mids)
        stale = self._read(lambda c: c.execute(
            "SELECT memory_id, content FROM memories WHERE vec_stale=1 AND active=1"
        ).fetchall())
        if stale:
            try:
                texts = [r["content"] for r in stale]
                vecs = deps.embed(texts) or []
                updates = [(struct.pack("%df" % len(v), *v), r["memory_id"])
                           for r, v in zip(stale, vecs) if v]
                if updates:

                    def _reembed(c):
                        c.executemany(
                            "UPDATE memories SET vec=?, vec_stale=0 WHERE memory_id=?",
                            updates)
                    self._write(_reembed)
                    out["reembedded"] = len(updates)
            except Exception:
                pass
        if consolidate:
            out["consolidated"] = self.consolidate(actor=actor)
        return out
