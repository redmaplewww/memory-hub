# DESIGN-MEMORY：通用记忆模块（Memory Hub）设计

> M8 · 2026-09-17（M8b-1/2 · M9/M9b · 2026-09-18~19，M10 · 2026-09-20）。目标：一个可承接后续所有涉及记忆的系统开发的通用模块。
> 本文档含三部分：①记忆系统设计空间分析（各家优劣与用途映射）；②本模块架构与
> 关键决策；③实施记录与阈值校准。选型依据 = Mem0 / Letta(MemGPT) / Zep(Graphiti) /
> LangMem / ChatGPT / Anthropic memory tool 官方文档调研（出处见附录）。

## 一、记忆系统设计空间（不同用途该怎么选）

### 1.1 五家范式一句话

| 项目 | 一句话范式 | 最强处 | 最弱处 |
|---|---|---|---|
| **Mem0** (v3) | 只增不删 + 多信号排序（语义+BM25+实体） | 成本最低（单次 LLM、p50<1s）、基准最好看 | 无一致性问题解决——矛盾记忆并存靠排序压旧 |
| **Graphiti/Zep** | 时间知识图谱：三元组 + 双时间戳 invalidate（"invalidated — not deleted"） | 知识更新/时间推理最强、全程可审计 | 需要 Neo4j 等图库，构建成本高 |
| **Letta (MemGPT)** | memory blocks 自编辑 + sleep-time 后台整理 + MemFS(git) | Agent 自治、共享 block、记忆可 diff/回滚 | 复杂度高、token 占用管理难 |
| **LangMem** | 语义/情节/程序三类记忆 + Active/Background 双抽取时机 | 分类学最完整、episodic→procedural 闭环 | 依赖 LangGraph 生态 |
| **ChatGPT** | saved memories + 检索引用历史 | 体验最顺滑（无显式审批） | 零治理能力，API 侧无内置记忆 |

### 1.2 用途 → 设计映射（本模块的能力地图）

| 用途 | 核心需求 | 该用的设计 | Memory Hub 落点 |
|---|---|---|---|
| 个人聊天助手 | 低延迟低成本，一致性要求低 | ADD-only+时序排序+decay | 关掉 LLM 裁决即降级为此形态 |
| **企业知识工作流** | 一致性、审计、权限、可解释 | invalidate 时间戳+事件溯源+ACL | **主模式（本项目）** |
| 长期项目协作 | 程序性知识、版本化 | 文件/git 记忆（MemFS 思路） | 二期：kind=procedure 挂文件引用 |
| 多 Agent 共享记忆 | 隔离与共享平衡 | namespace 作用域+只读保护 | 接口已留（namespace="agent"），二期启用 |
| 自改进 Agent | 行为学习 | episodic→procedural 闭环 | 二期 |

**选型结论**：不引外部库（零依赖纪律），自建「分层混合」架构——
这也是上述各家 2025-2026 的共同收敛方向。

## 二、架构：三层记忆 + 治理面

```
MemoryHub（唯一门面 twin_agent/memory_hub/）
  write() / recall() / profile() / forget() / edit() / invalidate() / events() / maintain()
──────────────────────────────────────────────
L1 profiles  当前态层 —— 用户画像/偏好聚合，LLM 整体重写、版本化（Letta block / LangMem profile）
L2 memories  语义层   —— 条目记忆，向量(bge)+关键词 RRF 召回，双时间戳 invalidate（Mem0 collection）
L3 episodes  溯源层   —— 会话轮次摘要，只增不删（Graphiti episodes：every fact traces back here）
──────────────────────────────────────────────
治理面：memory_events 审计流（照 work_events 模式）+ access 列 ACL 前过滤 + 生命周期三件套
```

### 2.1 四个核心决策

1. **冲突消解 = 双时间戳 invalidate**（Graphiti 范式）。新记忆写入时，与同
   namespace/subject/kind 中余弦 ≥0.60 的候选交 LLM 裁决（duplicate/contradict/coexist）；
   矛盾 → 旧条目 `status=invalidated, invalid_at=now`（不删），新条目 `valid_at=now`。
   默认召回只取 active；面板可查全历史并可 reactivate。→ 解决实测暴露的
   "用户改口后新旧偏好并存、互相矛盾"问题。
2. **向量召回 = 复用 retrieval/embed.py**（bge-small-zh 512 维，BLOB 存储）。
   写时同步嵌入（失败标 `vec_stale=1`，maintain 补嵌）；召回 = 向量余弦(≥0.40) +
   关键词二元组 → RRF(k=60) 融合 → recency 半衰期 30 天降权（排序非过滤，
   Mem0 decay 思路）→ `access in visible` 前过滤（照 hybrid 契约）。
   → 解决实测暴露的"同义改写丢召回"。
3. **生命周期三件套**：`expires_at` TTL（召回/维护时自动 invalidate）+
   recency 衰减 + `forget()` 软删（archived，事件留痕）。全程事件化。
4. **作用域 = (namespace, subject)**：现用 `("dialogue", user_id)`；
   预留 `("agent", agent_id)`、`("project", pid)`。

### 2.2 表结构（dialogue.db 内，幂等迁移）

- `memories` 演进列：`vec BLOB / vec_stale / access / status(active|invalidated|archived) /
  valid_at / invalid_at / expires_at / version / namespace / confidence / subject /
  file_ids（M8b-1）/ superseded_by（M9）`
- 新表：`profiles(namespace,subject,persona_json,version)`、
  `episodes(episode_id,namespace,subject,session_id,turn_seq,summary,ts)`、
  `memory_events(event_id,memory_id,op,from_status,to_status,actor,note,ts)`
  （op ∈ extracted/invalidated/reactivated/archived/edited/forgot/profile-updated/files-attached）、
  `memory_files(file_id,name,sha,size,content,created_at)`（M8b-1）

### 2.3 写入管线（双模式，LangMem Active/Background 精简版）

- **Online（轮末，chat_turn._maintain）**：LLM 抽取 0-3 条（提示词注入已有记忆防重）
  → embed → 去重（双向包含 ∨ 余弦≥0.85）→ 冲突裁决 → 落库+事件 →
  preference 自动投影 L1 profile（LLM 合并画像，版本+1）。
- **Offline（maintain CLI）**：`python -m twin_agent.memory_hub.maintain`
  —— 确定性任务：TTL 清扫、vec_stale 补嵌；`--consolidate` 追加 sleep-time
  存量整理（M9，判重合并 + 冲突消解补扫，dry-run 可预览）。
- **纪律**：任何失败静默跳过（LLM 不可用→不抽取不裁决，向量不可用→关键词单路），
  绝不阻塞对话主链路。

### 2.4 集成点（谁在用 Hub）

| 调用方 | 用法 |
|---|---|
| `dialogue/chat.py` | 轮前 `hub.recall`+`hub.profile` → state `_memories`/`_memory_profile`；轮末 `add_episode`+`extract_memories` |
| `agents/query.py` | `_generate` 注入【用户画像】段 |
| `agents/memory.py` | 对话命令「记住/忘掉/你记住了什么」走 Hub |
| `dialogue/memory.py` | 兼容门面（MemoryStore/extract_memories 旧签名不变，内部全转发 Hub） |
| `webapp/server.py` | /api/memories CRUD+reactivate+timeline；记忆面板（画像卡/状态徽章/时间线/含失效开关） |
| 后续新系统 | `from twin_agent.memory_hub import MemoryHub` 即是承接入口 |

## 三、阈值校准（M8a-3 实证，真实 bge-small-zh-v1.5）

实测相似度分布（余弦）：

| 对比类型 | 实测值 | 结论 |
|---|---|---|
| 无关对（表格格式 vs 公司地址） | 0.344 | 必须高于召回线 |
| 同义召回对（我偏好表格汇报 vs 你希望我怎么做周报） | 0.596 | 须召回 |
| 同域不同事（实习生薪资 vs 不招远程） | 0.577 | 不应冲突 |
| 矛盾对（表格格式 vs 改成列表格式） | 0.784 | 须触发冲突裁决 |
| 强同义对（对账走老王 两种说法） | 0.849 | 须判重 |

**bge 绝对值整体偏低**（理想 0.92 判重在 bge 上是 0.85），故取：
`VEC_THRESHOLD=0.40`（边缘无关对靠 RRF 排序压后）/ `CONFLICT_CANDIDATE_SIM=0.60` /
`NEAR_DUP_SIM=0.85`。换嵌入模型须重新校准。

## 四、红线与二期边界

- 记忆永不直写知识层（与 twin 六道门禁分离）；治理动作全部事件留痕；
  对话主链路永不因记忆失败阻塞；同库同锁（dbutil.LOCK，与 SessionManager 共享）。
- 二期（接口已留）：~~kind=procedure 文件引用~~（✅ M8b-1 完成）、
  ~~图遍历（轻量 supersede 链）~~（✅ M9 完成）、
  ~~LLM 后台批量整理（sleep-time）~~（✅ M9 完成）、
  ~~基准评测~~（✅ M9b：自含 LOCOMO 式子集对照，原 LOCOMO 数据集导入可后续再加）、
  ~~多 Agent namespace 启用~~（✅ M8b-2：注册表 + 写权限策略 + 融合召回完成）。
  二期规划项全部落地。

### 4.1 M8b-1：procedure 记忆 + 文件引用（MemFS-lite，2026-09-18）

- `KINDS` 增 `procedure`；`write()` items 支持 `files: [{name, content}]`
  （纯文本，≤64KB，超限/非文本静默跳过不阻塞写入）。
- 新表 `memory_files(file_id, name, sha, size, content, created_at)`：
  内容按 `sha(name+content)` 去重，同内容文件跨记忆复用同一 file_id。
- `memories` 增 `file_ids` 列（JSON 数组，幂等迁移兼容旧库）；
  `recall()` 命中项带 `file_ids`。
- 门面方法：`attach_files()`（追加挂载，version+1，事件 `files-attached`）、
  `read_memory_file()`（读回全文）、`memory_files()`（挂载元信息，不含 content）。
- 设计取舍：文件内容存 SQLite 而非磁盘——保持单文件库 + 同锁纪律，
  程序性知识体量小（SOP/脚本/模板），64KB 上限即可覆盖。

### 4.2 M9：sleep-time 存量整理 + supersede 溯源链（2026-09-18）

- **动机**：在线写入只与写入时刻的存量比对——跨时段渗漏的重复/矛盾
  （写入时向量缺失、阈值边缘、抽取重复表述）无人补扫；invalidate 只记事件，
  「哪条取代了哪条」无结构化链可查。
- **supersede 链**：`memories` 增 `superseded_by` 列（幂等迁移）。在线冲突
  invalidate 与整理失效均写入后继 id；`reactivate` 清链（恢复旧条目则取代
  关系不再成立）；`related(mid)` 前向沿链追到最新版本（≤10 跳）、后向收回
  被本条取代的全部历史（≤3 层）——图遍历的轻量落地，替代独立图库。
- **`consolidate(namespace, subject?, dry_run?)`**（Letta dreaming 精简版）：
  按 (subject, kind) 分组对 active 条目全量补扫——余弦≥0.85 确定性判重合并
  （保留信息量大的长条目，被并条目 invalidated + superseded_by 指向保留者）；
  0.60~0.85 且 LLM 可用时交裁决（duplicate 合并 / contradict 新胜旧，与在线
  语义一致）；无向量条目退化为双向包含判重。相似度降序贪心处理，单组上限
  200 条防 O(n²) 失控。`dry_run` 只报告不动库；maintain CLI `--consolidate`。
- **修复**：`_cos()` 此前只支持 BLOB×list（write/recall 的调用形态），BLOB×BLOB
  会把字节当整数算出垃圾相似度——consolidate 首次用到双 BLOB 形态，已修。
- 事件 op 复用 `invalidated`（note 标 sleep-time 来源），审计工具零适配。

### 4.3 M9b：基准评测 bench（2026-09-19）

- `memory_hub/bench.py`：自含 LOCOMO 式子集（6 条记忆 + 6 问），问题按能力分类：
  literal（字面关键词）/ paraphrase（同义改写，需向量）/ conflict（改口后旧值
  不得再召回，需冲突消解）。同一数据集双模式对照，量化后端注入收益。
- 降级基线实测：literal 4/4、paraphrase 0/1、conflict 0/1（命中 4/6）——
  与设计预期一致；注入真后端后应在完整模式下全过（twin-agent 仓内同源验证）。
- 防假阳性：偏好类目标前置 + ≥3 条 filler + 错开 updated_at，使降级模式
  「无命中→最近 3 条」回退不含语义类目标。
- CLI：`python -m memory_hub.bench [--db PATH --topk N --json]`。

### 4.4 M8b-2：多 Agent namespace 治理（2026-09-19）

- 新表 `namespaces(namespace PK, owner, policy, created_at)`，policy ∈
  `open`（默认）/ `restricted`（仅 owner 可写，其余 Agent 只读）。
  **未注册命名空间默认开放**——单 Agent 旧用法零改动（向后兼容）。
- `write()` 增策略门：restricted 且非 owner → 静默拒绝，回执 `denied=[ns]`
  （不抛异常，与主链路降级纪律一致）。
- `recall()` 的 `namespace` 参数支持传 list：多域融合召回（自己的域 +
  共享域一次查询，RRF 统一排序），命中项新增 `namespace` 字段标注来源域。
- 门面：`register_namespace()`（重复注册覆盖策略，事件留痕）、
  `list_namespaces()`。
- 用法：`register_namespace("shared-kb", owner="agent-A", policy="restricted")`
  ——A 维护共享记忆，B/C 只读消费；B 融合召回
  `recall(q, subject, namespace=["agent", "shared-kb"])`。

### 4.5 M10：对照 Mem0 现行方案的六项优化（2026-09-20）

> 依据：Mem0 官方文档（how-it-works / memory-operations / memory-types /
> search / reranker-search / custom-instructions / async / Dream API）调研对照。
> 明确不跟的：图数据库、多 reranker provider 生态、AsyncMemory 全套、
> 存储级 decay——均与单文件 SQLite / 零依赖纪律冲突（见调研记录）。

- **P1 批量冲突裁决**：write 的相近候选从逐条 LLM 调用改为**单次批量调用**
  （`_judge_relations`，输出 per-candidate relations JSON；兼容旧单条格式
  `{"relation": ...}` 应用到全部候选；失败全 coexist）。N 次调用并 1 次。
- **P1 抽取定制**：`extract_items(..., custom_instructions=)`——领域口径
  追加进系统提示词（Mem0 custom_instructions 思路），只收窄不放宽纪律。
- **P2 实体信号（第三路召回）**：`memories` 增 `entities` 列（JSON，≤5 个、
  每个 ≤30 字）；条目实体与查询做**子串重叠**即入实体路 RRF——中文专名
  （老王/项目名）无需 NER 也能命中，零依赖实现 Mem0 entity boost。
  entities 由抽取器顺带产出或调用方直传。
- **P2 时间意图**：查询含「之前/原来/历史」等提示词 → 自动
  `include_invalidated`——历史问句连旧版本一起给（双时间戳设计的差异化收益，
  Mem0 temporal 信号的对位实现）。
- **P3 LLM 二段精排**：`recall(..., rerank=True)` RRF 榜单交 LLM 按相关度
  重排（`{"order": [3,1,2]}`），失败/解析失败保持原序。
- **P3 过滤与反馈**：`recall(filters={"kind","since","until"})`（created_at
  字符串区间）；命中条目 `hit_count+1`（读路径 best-effort 写，失败静默）。
- **顺手修复**：RRF 各路名次此前按库返回序而非本路分数赋名——现关键词按
  命中数、向量按相似度降序后再进 RRF（相关性排序质量修复）。

### 4.6 M10b：真实语料入库测试 + ingest 更新链路修复（2026-09-20）

> 用真实工程文档（云湃 FactoryBrain 基线 3 份 + PMC 排程三件套 + 差异说明，
> 共 7 份 / 约 9 万字 / 202 块）做完整出入库测试，暴露并修复两个 ingest 缺陷。

- **缺陷 1（更新丢失）**：文档追加/修订后重导，新块被核心 write 的双向包含
  去重误杀（旧末块 ⊂ 新扩展块 → 判重丢弃），新内容静默丢失。
- **缺陷 2（溯源堆积）**：文档每次变更都新增一条「来源档案」索引条目，
  新旧并存无取代关系。
- **修复**：入库前按块内容全等比对（集合 diff）——旧块不在新集合 →
  invalidate（note「文档更新」留痕）；完全相同 → 核心 exact 去重自然幂等；
  同文件旧溯源索引一并被取代。**比对口径与核心 write 一致**（strip+500 截断）
  ——曾因口径不一致导致同内容重复导入误下架 6 块（不可见截断差异）。
- 回归测试 ×2：同内容重导零动作（不增不下架）、修订重导旧块下架新口径
  可召回且溯源索引不堆积。
- 真实语料验收：8/8 跨文档知识问答（总体原则/7×Domain Service/PostgreSQL/
  FR 编号体系/命名规范/快照契约/验收矩阵）、幂等 0 新增、追加更新入库可查、
  口径修订自动换血、原件全文读回。

### 4.7 M10c：本地 Ollama 接入修复 + 画像回缩（2026-09-24）

- **根因一（思考型模型）**：qwen35-fix:9b 把输出全部写进 `message.thinking`、
  `content` 为空（实测 47~96 秒拿到空串）——抽取/裁决/画像合并/精排全链路
  静默拿 None。修复：`backends_ollama` 默认 `think=False`（2 秒返回有效
  JSON）；不支持 think 参数的模型自动回退并从 thinking 文本摘取 JSON
  （`_last_json` 取含业务键的最后一个对象）；失败重试；`keep_alive=30m`
  模型常驻（本机换载大模型曾压崩服务）；`num_predict=1024` 防跑飞；
  新增 `probe()` 体检（服务/对话/向量可用性与延迟）。
- **根因二（中文枚举）**：模型回 `{"关系": "冲突"}` 而非英文枚举。core 增
  `_norm_relation` 别名归一（冲突/矛盾/取代→contradict，不冲突/共存→coexist，
  重复/相同→duplicate；否定词先于冲突词命中，未知保守 coexist），两个裁决
  提示词追加「必须英文小写」约束。
- **画像回缩（rebuild_profile）**：写入投影只累加，条目失效后 L1 新旧偏好
  并存（实测库 v8 同时含「表格」「列表」）。consolidate 检测到偏好条目失效
  → 以**当前有效 preference** 为唯一事实源重建画像（回执 `profiles_rebuilt`）。
- 实测：裁决 2 秒/次（原 96 秒拿空）、批量裁决 3 秒、抽取 8~13 秒、
  整理 dry-run 2 秒（原 374 秒）、bench 完整模式 **2 秒 6/6**（原 92 秒）；
  真实库画像 v8→v9 剔除失效偏好。网页控制台改系统级分离启动
  （PowerShell Start-Process，不随开发 shell 退出）。

## 五、验收记录

- M10c（2026-09-24）：本地后端修复 + 画像回缩。新增 2 例（中文别名归一
  8 断言、画像回缩：整理失效后画像剔除旧偏好+版本递增+事件留痕）。
  全套 42 passed / 7 skipped；真库画像重建验证；bench 完整模式 2 秒 6/6。

- M10b（2026-09-20）：真实语料入库全链路 5/5（问答 8/8、幂等、更新、修订、
  读回）；新增回归 2 例。全套 40 passed / 7 skipped。

- M10（2026-09-20）：六项优化 9 例全过——批量裁决双候选分别判定、实体落库
  去重、实体路无字面重叠入榜、时间意图含失效历史、kind/区间过滤、rerank
  翻转+垃圾回退、hit_count 递增、定制抽取注入提示词/不传不变。全套
  36 passed / 7 skipped；bench 降级基线 4/6 不变（排序修复无回归）。

- M8b-2（2026-09-19）：namespace 治理 4 例全过——restricted 写拒绝
  （owner 可写/访客 denied+只读可见）、未注册默认开放+非法策略拒绝、
  注册表列举+覆盖生效、多域融合召回带来源标注。全套 27 passed / 7 skipped。

- M9b（2026-09-19）：bench 降级基线用例过（literal 4/4、语义/冲突 0/2、
  模式与计数断言）；CLI 冒烟（表格输出 + JSON 输出）。全套 23 passed / 7 skipped。

- M9（2026-09-18）：整理与溯源 7 例全过——确定性判重合并（留长条目+挂链）、
  LLM 裁决 contradict 新胜旧+related 双向溯源、reactivate 清链、dry-run 不动库、
  在线改口写 supersede 链、两级取代链 A→B→C 前向/后向遍历、LLM duplicate 合并、
  maintain(consolidate=True) 合跑；假后端经 deps.configure 注入（只测管线不测模型
  质量，真后端仍由 @backend 用例在 twin-agent 仓内覆盖）。全套 22 passed / 7 skipped。
  CLI 冒烟：`--consolidate`（TTL 1 + 合并 1）、`--dry-run` 仅报告。

- M8b-1（2026-09-18）：procedure+文件引用 4 例全过——写入挂载/召回带 file_ids、
  sha 去重复用、attach_files 事件留痕、超限静默跳过。

- 内核 18/18（test_memory_hub：迁移兼容/去重/改口 invalidate/失效恢复/TTL/补嵌/
  同义改写召回/RRF/namespace 隔离/ACL 过滤/画像投影/episode/审计回放/编辑向量重建/共享锁并发）
- 门面兼容 35/35（test_memory + test_dialogue + test_memory_hub，旧断言语义仅
  recall 排序语义一条更新——RRF 融合使语义相关条目可入榜）
- 在线 E2E（真实 DeepSeek+bge）：改口「表格→列表」旧偏好自动 invalidated、
  同义问句「你希望我怎么做周报」精确召回新偏好；画像 v2；事件流 5 条可回放
- webapp 记忆面板实测：画像卡/状态徽章（已失效删除线）/时间线弹层/含失效开关

## 附录：调研出处

- Mem0: github.com/mem0ai/mem0 ｜ docs.mem0.ai（graph-memory / memory-decay /
  custom-instructions / reranker-search / memory-operations）
- Letta: docs.letta.com（memory-blocks / dreaming）｜ sleep-time 论文 arXiv:2504.13171
- Graphiti/Zep: github.com/getzep/graphiti ｜ 论文 arXiv:2501.13956
- LangMem: github.com/langchain-ai/langmem（conceptual guide）
- Anthropic: claude.com/blog/context-management（memory tool +39%、context editing -84% tokens）
- 注：各项目基准数字口径不一（Mem0 Platform vs OSS、双方互测争议），
  选型以自有数据实测为准——本模块阈值即按自有实证校准。
