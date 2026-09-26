# -*- coding: utf-8 -*-
"""MemoryHub 表结构与幂等迁移（dialogue.db 内）。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(
  namespace TEXT, subject TEXT, persona_json TEXT,
  version INTEGER DEFAULT 1, updated_at TEXT,
  PRIMARY KEY(namespace, subject));
CREATE TABLE IF NOT EXISTS episodes(
  episode_id TEXT PRIMARY KEY, namespace TEXT, subject TEXT,
  session_id TEXT DEFAULT '', turn_seq INTEGER DEFAULT 0,
  summary TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS idx_ep_sub ON episodes(namespace, subject, ts);
CREATE TABLE IF NOT EXISTS memory_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT,
  op TEXT, from_status TEXT, to_status TEXT,
  actor TEXT, note TEXT DEFAULT '', ts TEXT);
CREATE INDEX IF NOT EXISTS idx_mev_mem ON memory_events(memory_id, event_id);
CREATE TABLE IF NOT EXISTS memory_files(
  file_id TEXT PRIMARY KEY, name TEXT, sha TEXT, size INTEGER,
  content TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_mfile_sha ON memory_files(sha);
CREATE TABLE IF NOT EXISTS namespaces(
  namespace TEXT PRIMARY KEY, owner TEXT, policy TEXT DEFAULT 'open',
  created_at TEXT);
"""

# 现有 memories 表（dialogue/memory.py 建）的演进列：列已存在时报错忽略
MEMORIES_MIGRATE = [
    "ALTER TABLE memories ADD COLUMN vec BLOB",
    "ALTER TABLE memories ADD COLUMN vec_stale INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN access TEXT DEFAULT 'internal'",
    "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'",
    "ALTER TABLE memories ADD COLUMN valid_at TEXT",
    "ALTER TABLE memories ADD COLUMN invalid_at TEXT",
    "ALTER TABLE memories ADD COLUMN expires_at TEXT",
    "ALTER TABLE memories ADD COLUMN version INTEGER DEFAULT 1",
    "ALTER TABLE memories ADD COLUMN namespace TEXT DEFAULT 'dialogue'",
    "ALTER TABLE memories ADD COLUMN confidence REAL",
    "ALTER TABLE memories ADD COLUMN subject TEXT DEFAULT ''",
    "ALTER TABLE memories ADD COLUMN file_ids TEXT DEFAULT ''",
    # M9：supersede 溯源链——被取代条目指向后继 id（invalidate 时写入，
    # reactivate 清空；related() 沿链遍历，图遍历的轻量落地）
    "ALTER TABLE memories ADD COLUMN superseded_by TEXT",
    # M10：实体信号（写入存实体、召回按实体重叠加权的第三路）+ 召回反馈计数
    "ALTER TABLE memories ADD COLUMN entities TEXT DEFAULT ''",
    "ALTER TABLE memories ADD COLUMN hit_count INTEGER DEFAULT 0",
]

# memories 表完整建表（旧库由 dialogue/memory.py 先建基表，此处幂等兜底；
# idx_mem_ns 引用迁移列，须在 ALTER 之后创建——见 core._init_schema）
MEMORIES_BASE = """
CREATE TABLE IF NOT EXISTS memories(
  memory_id TEXT PRIMARY KEY, tenant TEXT, user_id TEXT, role TEXT,
  kind TEXT, content TEXT, source_session TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, active INTEGER DEFAULT 1,
  vec BLOB, vec_stale INTEGER DEFAULT 0, access TEXT DEFAULT 'internal',
  status TEXT DEFAULT 'active', valid_at TEXT, invalid_at TEXT,
  expires_at TEXT, version INTEGER DEFAULT 1,
  namespace TEXT DEFAULT 'dialogue', confidence REAL, subject TEXT DEFAULT '',
  file_ids TEXT DEFAULT '', superseded_by TEXT,
  entities TEXT DEFAULT '', hit_count INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_mem_user ON memories(user_id, active);
"""

# 迁移完成后创建（引用 namespace/subject 列）
MEMORIES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_mem_ns ON memories(namespace, subject, status);
"""
