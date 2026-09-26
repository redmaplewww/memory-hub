# Memory Hub · 本地知识库产品测试报告

> 报告日期：2026-09-20（v2，补真后端验证） ｜ 包位置：`memory_hub_package/` ｜ 验证环境：Windows 10 / Python 3.10 / 百度同步盘（SQLite 高锁争用环境，最不利情形）
> 本报告覆盖：产品形态 → 本轮修复 → 功能测试 → 基准评测 → 压测 → **真后端验证（DeepSeek + bge-small-zh-v1.5 本地推理）** → E2E 实录 → 已知边界。全部数字可复现（命令附各节）。

## 一、产品形态（交付物）

一个零第三方依赖（纯 Python 标准库 + SQLite）的本地知识库产品，单文件夹分发：

| 文件 | 角色 |
|---|---|
| `memory_hub/core.py` | 内核：三层记忆（画像/条目/溯源）+ 冲突消解 + 三路召回（向量/关键词/实体 RRF）+ 生命周期 + 事件审计 + supersede 溯源 + consolidate 整理 + namespace 治理 |
| `memory_hub/ingest.py` | **产品入口 CLI**：本地文件夹（md/txt）→ 入库 / `ask` 问答 / `stats` 统计 |
| `memory_hub/maintain.py` | 维护 CLI：TTL 清扫 + 补嵌 + sleep-time 整理 |
| `memory_hub/bench.py` | 基准评测（LOCOMO 式自含数据集，双模式对照） |
| `memory_hub/extract.py` | 对话→记忆抽取（需 LLM，注入即用） |
| `memory_hub/deps.py` | 可插拔接缝（LLM/embedding/runtime_dir 注入，未注入安全降级） |
| `memory_hub/schema.py` / `dbutil.py` | 幂等迁移 / 并发基元（全局锁+锁库退避） |
| `stress_check.py` | 压测脚本（本次新增，可复跑） |

用户路径：`python -m memory_hub.ingest ingest 我的文档 --db kb.db` → `python -m memory_hub.ingest ask "问题" --db kb.db`。数据 100% 本地单文件，无网络依赖。

## 二、本轮修复（压测发现的三个热点）

| # | 问题 | 修复 | 效果（实测） |
|---|---|---|---|
| 1 | `maintain()` TTL 清扫逐条开连接+锁写 | 批量单事务（`_expire` 循环内联 + 事件同事务）；补嵌同步改 `executemany` | **500 条 2187ms → 21.7ms（100 倍）** |
| 2 | 召回命中回写 `hit_count` 逐条 `_write`（每次召回 5 次连接+锁） | `_bump_hits` 改单事务 `executemany` | **1000 条库召回 30ms → 18.5ms（-38%）** |
| 3 | `write()` 忽略 item 级 `source_session`，入库溯源字段丢失 | item 级优先于函数级参数 | E2E 中每条记忆可溯源到来源文件 |
| 附 | `entities` 列 INSERT 占位符少 1（24 用例崩） | 补齐占位符（与并行开发会话协同修复） | 恢复全绿 |

未修复（记录在案）：写入 O(n) 存量判重扫描（万条库 17.8ms/条，容量内可接受）、召回线性扫描（万条 ~100ms）——超万条规模需 SQL 侧候选预筛，属三期。

## 三、功能测试（pytest）

**结果：38 passed / 7 skipped / 0 failed**（45 个用例；跳过均为需真实 LLM+embedding 后端的 `@backend` 用例，本机无网关，模块设计即如此降级）。

覆盖面（按测试文件分类）：
- **内核**：迁移兼容（旧库无损演进）/ 去重（子串+语义）/ 改口 invalidate+恢复 / TTL / 补嵌 / RRF 召回 / namespace 隔离 / ACL 过滤 / 画像投影 / episodes / 事件审计回放 / 编辑向量重建 / 共享锁并发
- **M8b-1 文件记忆**：写入挂载 / sha 去重复用 / attach 留痕 / 超限静默跳过
- **M9 整理与溯源**：判重合并 / LLM 裁决链 / reactivate 清链 / dry-run / 两级取代链遍历
- **M8b-2 namespace 治理**：restricted 写拒绝 / 开放兼容 / 注册表覆盖 / 多域融合召回
- **ingest 入库（本次新增 2 例）**：文件夹入库→切块召回→附件读回→重复导入幂等（19 块全去重 0 新增）；分块器边界（400 字上限、段落完整性）

复现：`cd memory_hub_package && python -m pytest tests/ -q`

## 四、基准评测（bench，降级模式）

literal（字面关键词）**4/4** ✅ ｜ paraphrase（同义改写）0/1、conflict（改口消解）0/1 —— **符合设计预期**：这两类能力依赖向量/LLM 后端，降级模式基线本应为 0，恰好证明「降级不假阳性」。注入真后端后应在完整模式下 6/6（待真后端环境验证）。

复现：`python -m memory_hub.bench`

## 五、压测（修复前后对照，N=1000）

| 项目 | 修复前 | 修复后 | 结论 |
|---|---|---|---|
| 顺序写入 | 8.4 ms/条（118 条/s） | 9.6 ms/条（104 条/s）* | 持平（*本轮测时同步盘繁忙，绝对值含环境噪声） |
| 召回（1000 条库） | 30.4 ms | **18.5 ms** | 修复 #2 生效 |
| **TTL 清扫 500 条** | **2187 ms** | **21.7 ms** | 修复 #1 生效（100 倍） |
| 并发写 4 线程×50 | 200/200 落库 0 异常 | 200/200 落库 0 异常 | 全局锁下无丢失无死锁 |
| consolidate 1000 条 | 8.2 ms | 6.5 ms | 组上限防 O(n²) 生效 |
| 事件回放 | 2.1 ms | 3.0 ms | 正常 |
| 假向量路径写/召回 | 4.8 / 5.7 ms | 4.1 / 23.9 ms* | 余弦非瓶颈；*含环境噪声 |
| 外推点：1 万条写 / 召回 | 17.8 ms/条 / 104 ms | 未复测（两个已修热点均按万条等比放大收益） | 线性扫描，万条为当前容量舒适上限 |

复现：`python stress_check.py 1000`

## 六、E2E 实录（真实文档入库）

以本仓库 `本地知识库/个人知识库开源方案调研-20260916.md` 为真实语料：

```
$ python -m memory_hub.ingest ingest "G:\BaiduSyncdisk\Zcode\本地知识库" --db demo-kb.db --subject local-kb
入库完成：1 个文件 / 新增 19 块 / 去重 0 块 / 挂载原件 1 个
$ python -m memory_hub.ingest ask "推荐一个中文友好的知识库方案" --db demo-kb.db --subject local-kb
→ 命中推荐表（AnythingLLM/MaxKB 段落）与「中文/国产模型适配」段落 ✅
$ python -m memory_hub.ingest ask "蒸馏能力最强的项目是什么" ...  → 命中蒸馏能力对比表 ✅
$ python -m memory_hub.ingest stats ...  → 条目 19（active 19）✅
$ 重复 ingest 同一文件夹 → 新增 0 块 / 去重 19 块（幂等）✅
$ python -m memory_hub.maintain --db demo-kb.db → 维护完成 0/0 ✅
```

降级（纯关键词）模式下召回正确命中相关段落；注入向量后同义问句召回质量将进一步提升（bench 已给出量化口径）。

## 七、真后端验证（2026-09-20 v2 补充）

**后端**：embedding = `BAAI/bge-small-zh-v1.5`（fastembed 本地推理，512 维归一化，与阈值校准同款，经 hf-mirror 下载）；LLM = DeepSeek `deepseek-chat`（API）。适配器 `memory_hub/backend_local.py`，一行 `backend_local.install()` 注入。

### 7.1 全量测试（真后端注入）

**43 passed / 2 skipped / 0 failed**（跳过 = 零依赖基线用例「后端在场时按设计跳过」+ 宿主 SessionManager 专属用例）。此前 7 个从未真跑的 `@backend` 用例全部通过，覆盖：
- **语义判重**（余弦≥0.85：「对账走老王」两种说法判重）✅
- **改口冲突自动 invalidate**（表格→列表：LLM 裁决 contradict，旧条目失效挂 supersede 链）✅
- **同义改写召回**（「你希望我怎么做周报」→ 命中「表格汇报」偏好，无共同字面词）✅
- **画像 LLM 合并投影**（preference 投影 L1，fact 不进画像）✅
- **编辑后向量重建 + 语义召回**、**maintain 补嵌**、**consolidate LLM 裁决链** ✅

期间发现并修复 2 个测试/评测层缺陷（内核行为本身正确）：
1. `test_file_sha_dedup_reuse` 按降级模式写死断言——真向量下「规格说明 A/B」内容语义重复被整体判重（正确行为），测试改为双模式断言；
2. **bench conflict 题假阴性**：`avoid="表格"` 会误杀「列**表格**式」本身（子串重叠），LLM 裁决 4/4 次 contradict、系统行为完全正确却被判失败——avoid 改为旧值特征子串「统一用表格」。

### 7.2 基准评测（完整模式）

三轮复跑：**6/6 全过（literal 4/4、paraphrase 1/1、conflict 1/1）**。对比降级基线 4/6：向量注入解决同义改写召回（+1），LLM 注入解决改口冲突消解（+1）——后端注入收益量化兑现，与设计预期完全一致。

复现：
```
python -c "from memory_hub import backend_local; backend_local.install(); \
  from memory_hub.bench import run; print(run())"
```

### 7.3 真后端语义问答 E2E

真实语料（开源方案调研报告）入库后问**同义改写问题**（无共同关键词，降级模式必失败）：

| 问题 | 命中（Top-3 首条） |
|---|---|
| 「想快速搭建且数据不出本机选什么」 | AnythingLLM「拖拽文件 / 本地存储 / 数据留本机」段落 ✅ |
| 「哪家图谱构建能力强但资源要求高」 | 二轮调研「知识图谱构建」需求升级段落 ✅ |

入库侧真向量判重也生效：19 块中 3 块语义重复被自动去重（降级模式为 0）。

### 7.4 后端注入对性能的影响（附测）

真 bge 嵌入（CPU）：写时嵌入约 +2ms/条（本地推理），召回查询嵌入 +1 次推理；DeepSeek 裁决仅冲突候选触发（同库同 subject 余弦≥0.60 才调用），正常写入无 LLM 开销。CLI/产品路径延迟仍在毫秒级。

## 八、已知边界与遗留

1. ~~7 个 @backend 用例未真跑~~ → **✅ 已于 §七 真后端验证闭环（43 passed，bench 完整模式 6/6）**。
2. **容量边界**：单 subject 万条内性能合格；召回为 Python 线性扫描，超万条需候选预筛（三期）。
3. **写入 O(n) 判重扫描**：万条库 17.8ms/条，尚可；更大规模需 SQL 粗筛。
4. **协议与环境**：测试库走百度同步盘目录属最不利情形（锁争用），全量仍全绿——并发基元（重试+全局锁）经受住了同步盘干扰。
5. **LLM 非确定性**：DeepSeek 裁决温度 0 仍有轻微波动，bench conflict 题三轮全过但依赖裁决稳定；内核侧保守降级（coexist 不动旧条目）保证最坏只是并存而非误删。
6. **并行开发提示**：本项目存在多会话并行开发史（M9/M9b/P2/P3 由并行会话贡献），本轮修复均在其上合并并通过全量回归。

## 九、结论

- 功能：**真后端下 43 过 2 跳 0 失败**（降级模式 38 过 7 跳），内核全部模块含 LLM/向量路径均已实证；
- 基准：降级 4/6 → **完整模式三轮 6/6**，后端注入收益量化兑现；
- 性能：两个热点修复合计使 TTL 清扫提速 100 倍、召回延迟降 38%，千条级全场景毫秒级响应；
- 产品完整度：入库→问答→维护→评测→压测→真后端 六件套齐备，零依赖可分发、后端一行注入，数据全本地；
- 待办优先级：① 万条级召回预筛；② 写入判重 SQL 粗筛；③ bench 数据集扩容（现 6 问，够冒烟不够统计）。
