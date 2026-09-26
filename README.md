# Memory Hub · 通用记忆模块（独立分发版）

> 三层记忆 + 事件审计 + 冲突消解的通用记忆系统。零第三方依赖（纯标准库），
> LLM 与向量为可选注入——不注入也能跑（降级模式），注入后获得完整能力。
> 设计分析与各家范式对照见 `DESIGN-MEMORY.md`。

## 结构

```
memory_hub/
  __init__.py    # 出口：MemoryHub / WriteReceipt
  core.py        # 内核：三层记忆 + write/recall/生命周期/事件流
  schema.py      # 表结构与幂等迁移（memories 演进列 / profiles / episodes / memory_events）
  deps.py        # 依赖接缝：LLM 与 embedding 注入式，未注入自动安全降级
  extract.py     # 对话→记忆条目抽取器（Online 模式）
  maintain.py    # 维护 CLI：python -m memory_hub.maintain（TTL 清扫 + 补嵌 + --consolidate 存量整理）
  bench.py       # 基准评测：python -m memory_hub.bench（LOCOMO 式子集，降级/完整模式对照）
  ingest.py      # 本地知识库 CLI：文件夹入库 / ask 问答 / stats 统计
  backend_local.py # 真后端适配器：bge-small-zh-v1.5(fastembed) + DeepSeek，一行注入
  dbutil.py      # SQLite 并发基元（全局写锁 + 锁库退避重试）
tests/
  test_memory_hub.py   # 49 用例（无后端自动跳过 7 个 backend 用例）
DESIGN-MEMORY.md       # 设计文档（六家范式调研 / 用途→设计映射 / 阈值校准）
TEST-REPORT.md         # 测试报告（功能/基准/压测/E2E 全量验证记录）
```

## 快速开始（零依赖模式）

```python
import sys; sys.path.insert(0, ".")
from memory_hub import MemoryHub

hub = MemoryHub("my_app.db")     # 不传路径则 TWIN_RUNTIME 环境变量或 ./runtime/dialogue.db

# 写入（去重自动生效；无 LLM 时跳过冲突裁决）
hub.write([{"kind": "preference", "content": "汇报用表格格式"}],
          actor="app", subject="user-1")

# 召回（无向量时关键词单路）
hits = hub.recall("怎么做汇报", subject="user-1")

# 生命周期 + 审计
hub.forget(hits[0]["memory_id"], actor="app", reason="用户要求")
hub.events(subject="user-1")          # 全程事件回放
```

### 本地知识库 CLI（把文件夹变成可问答的知识库）

```
python -m memory_hub.ingest ingest 我的文档 --db kb.db --subject local-kb
python -m memory_hub.ingest ask "对账流程是什么" --db kb.db --subject local-kb
python -m memory_hub.ingest stats --db kb.db --subject local-kb
```

md/txt 按段落切块（≤400 字/块，条目带来源文件前缀可溯源），每文件另挂一条
procedure 溯源条目附原件全文（≤64KB）；重复导入幂等（内容块自动去重）。

## 开箱真后端（本地向量 + DeepSeek）

```python
from memory_hub import backend_local
backend_local.install()   # bge-small-zh-v1.5 本地推理 + DeepSeek API（DEEPSEEK_API_KEY）
```

此后同义改写召回、改口冲突自动 invalidate、画像 LLM 合并、语义判重全部生效
（模型与阈值校准同款，无需重标）。模型首次下载若超时：
`HF_ENDPOINT=https://hf-mirror.com`。

## 接入完整能力（注入你的 LLM 与 embedding）

```python
from memory_hub import deps

deps.configure(
    llm_chat=my_llm_chat,          # (messages, temperature=0.1, json_mode=True) -> str | None
    llm_available=my_llm_available,# () -> bool
    parse_json=my_parse_json,      # text -> dict | None（剥围栏/截取，尽力解析即可）
    embed=my_embed,                # (texts: list) -> [[float,...],...] | None（建议归一化）
    embed_one=my_embed_one,        # (text) -> [float,...] | None
    embed_available=my_embed_available,
    runtime_dir=lambda: "/var/myapp/runtime",
)
# 接入后自动获得：同义改写向量召回（RRF 融合）、改口冲突自动 invalidate、
# LLM 画像合并、对话抽取器（memory_hub.extract.extract_items）
```

## 核心语义

- **三层**：L1 `profiles` 用户画像（版本化当前态）/ L2 `memories` 条目记忆 /
  L3 `episodes` 会话摘要溯源（只增）。全部在同一个 SQLite 库。
- **冲突消解**：新记忆与同作用域相近记忆（余弦≥0.60）交 LLM 裁决，矛盾则旧条目
  `status=invalidated`（**不删**、事件留痕、可 `reactivate`）——"invalidated, not deleted"。
- **supersede 溯源链（M9）**：被取代条目记 `superseded_by` 指向后继；
  `hub.related(mid)` 前向追到最新版本、后向收回全部历史版本（图遍历轻量版），
  `reactivate` 自动清链。
- **批量冲突裁决（M10）**：写入时相近候选**单次 LLM 调用**逐条裁决
  （N 次调用并 1 次降本提速；兼容旧单条格式）。
- **三路召回（M10）**：向量 + 关键词 + **实体重叠**（条目带 `entities`，
  查询含该实体即入榜）RRF 融合，各路先按本路分数排序；
  `rerank=True` 可选 LLM 二段精排（失败保持原序）。
- **时间意图（M10）**：查询含「之前/原来/历史」等 → 自动连同失效历史一起召回
  （invalidate 双时间戳的差异化收益）。
- **结构化过滤与反馈（M10）**：`recall(filters={"kind","since","until"})`；
  命中条目 `hit_count` 自动递增（常用记忆浮上来）。
- **抽取定制（M10）**：`extract_items(..., custom_instructions=...)` 注入领域
  抽取口径（Mem0 custom_instructions 思路）；抽取器顺带产出 `entities`。
- **sleep-time 整理（M9）**：`hub.consolidate()` 对存量 active 条目按 (subject,kind)
  补扫——余弦≥0.85 确定性判重合并（留长条目）、0.60~0.85 且 LLM 可用时交裁决
  （duplicate 合并 / contradict 新胜旧），无向量退化为包含判重；全程事件留痕，
  `dry_run=True` 只报告不动库。
- **生命周期**：`expires_at` TTL 自动失效 / recency 半衰期 30 天排序降权 / `forget` 软删归档。
- **作用域**：`(namespace, subject)`——单用户 `("dialogue", user_id)`，
  多 Agent 共享 `("agent", agent_id)`（namespace 隔离，互不可见）。
- **多 Agent namespace 治理（M8b-2）**：`hub.register_namespace("shared-kb",
  owner="agent-A", policy="restricted")`——A 独占写、其余 Agent 只读
  （访客写入回执 `denied`，不抛异常）；`open` 或未注册 = 任何人可写（向后兼容）。
  融合召回：`hub.recall(q, subject, namespace=["agent", "shared-kb"])`
  一次查自己的域 + 共享域，命中带 `namespace` 来源标注。
- **程序性记忆（M8b-1）**：`kind="procedure"` 可挂文件引用
  `files: [{"name": "sop.md", "content": "..."}]`（文本 ≤64KB，sha 去重复用）；
  召回命中带 `file_ids`，`hub.read_memory_file(fid)` 读回全文，
  `hub.attach_files(mid, files)` 追加挂载（事件留痕）。
- **纪律**：所有失败静默降级，绝不抛给宿主主链路；每个变更一条 `memory_events` 事件。

## 阈值（换 embedding 模型须重新校准，方法见 DESIGN-MEMORY §三）

`VEC_THRESHOLD=0.40`（召回）/ `CONFLICT_CANDIDATE_SIM=0.60`（冲突候选）/
`NEAR_DUP_SIM=0.85`（判重）——按 bge-small-zh-v1.5 实证分布标定。

## 测试

```
cd tests && python -m pytest test_memory_hub.py -v
# 有 LLM/embed 环境（twin-agent 仓内同源文件）：49 用例全跑
# 无后端环境：42 个用例跑（含假后端注入的裁决/实体/精排/画像回缩用例），7 个 backend 用例自动跳过
```

## 本地 Ollama 接入（可选）

```python
from memory_hub import backends_ollama
backends_ollama.configure()               # 默认 qwen35-fix:9b + qwen3-embedding:0.6b
print(backends_ollama.probe())            # 体检：服务/对话/向量可用性与延迟
```

思考型模型自动关思考（think=False，实测 96 秒空转 → 2 秒出结果）；失败重试、
模型常驻（keep_alive）、thinking 兜底摘 JSON、中文枚举归一。

## 基准评测

```
python -m memory_hub.bench            # 降级基线：literal 4/4，语义/冲突类 0/2
python -m memory_hub.bench --json     # 机器可读报告
# 注入 LLM+embedding 后同一数据集即完整模式：同义改写召回 + 改口冲突消解应全过
```

## 维护 CLI

```
python -m memory_hub.maintain --db my_app.db
# TTL 清扫（到期条目自动 invalidate）+ vec_stale 补嵌（嵌入曾失败的条目）
python -m memory_hub.maintain --db my_app.db --consolidate
# 追加 sleep-time 存量整理；--dry-run 只报告不动库；--subject u1 限定作用域
```
