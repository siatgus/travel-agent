# 第五阶段设计：置信度三档分流接管与 LLM 语义兜底

> 2026-11-18。第四阶段基线：commit `2f66f4e`（EntityResolver 实体验证，已冻结）。
> 本阶段定位：**建立可靠的决策边界**，让三、四阶段的结果逐步参与业务决策——
> 不是继续堆叠识别规则。

## 0. 第四阶段验收意见吸收（设计前提）

### 0.1 术语事实修正：五状态，skipped 是旁路

实现为五种状态，其中 `skipped` 是「不适用实体验证」的旁路（品类词、
城市、空候选），**不强行归入四档分流**。后续文档与统计口径统一按
「四档分流结果 + skipped 旁路」表述。

### 0.2 rejected ≠ 低置信度（语义修正，必须落进代码与语料）

| 状态 | 语义 | 证据标准 |
|---|---|---|
| confirmed | 我知道它是什么，且唯一、约束全过 | KB/在线唯一命中 + 硬约束全过 |
| ambiguous | 它可能是什么有多个答案，或约束冲突 | 多候选/跨城/类型冲突/分店歧义 |
| rejected | **已有足够证据确认不存在** | probe_status=not_found（探测成功执行且无结果） |
| unverified | 没有足够证据判断 | probe unavailable / skipped / KB-only 未探测 |

- 「带我去南京的星际海底餐厅」：在线探测成功且明确无匹配 → rejected；
  高德超时 → unverified。**两者绝不允许混淆**（第四阶段已有红线测试，
  第五阶段延续并升级为决策层断言）。
- 低置信度 ≠ rejected：分数低但无存在性证据 → 保守处理（不进行程），
  probe_status 保持真实值，绝不倒填 not_found。
- ambiguous 不代表候选不真实：南京、上海各有一家同名餐厅，两家都真实，
  只是用户没指定——分流语义是「请求消歧」，不是「怀疑存在」。

### 0.3 影子默认 KB-only 保持不变（已裁定，不改动）

四个理由（控制变量 / 测试稳定 / 保护成本 / 便于归因）不变。第五阶段
沿用：影子默认 KB-only，`TRAVEL_RESOLVER_ONLINE=1` 才允许在线探测。

### 0.4 第四阶段遗留的两个小改进（纳入 P0，验收意见明确要求）

1. **探测元数据分离记录**。shadow_resolved / ResolveResult 每条增加：
   ```json
   {"probe_status": "unavailable", "probe_source": "amap",
    "probe_reason": "network_timeout", "status": "unverified"}
   ```
   统计口径必须能区分五种情形，绝不统一记为「未匹配」：
   - KB 没有收录（kb_miss → 进入探测或 skipped）
   - 未执行在线探测（skipped：KB-only 影子）
   - 在线探测失败（unavailable + reason：network_timeout / no_key /
     circuit_open / no_city_center / quota_exceeded）
   - 探测成功但无结果（not_found → rejected）
   - 探测返回多个结果（online_hit + matches>1 → ambiguous）
2. **在线探测缓存与调用限额**。复用 poi_sources 的结果缓存与熔断；
   新增进程级调用限额（如每城市每分钟 ≤N 次、单轮 utterance ≤M 次
   探测），超限落 unavailable + reason=quota_exceeded，绝不外推压力。

## 1. 目标架构

```
CandidateExtractor（Phase 3：候选抽取、Span、类型、角色）
   ↓
EntityResolver（Phase 4：存在性验证、城市归属、类型、分店）
   ↓
DecisionEngine（Phase 5 新增：src/travel_agent/decision.py）
   │  ① 硬约束检查        城市冲突 / 类型冲突 / 分店歧义 / 角色闸 / 写入权限闸
   │  ② 存在性与唯一性    confirmed 唯一？ambiguous 列候选？rejected 有证据？
   │  ③ 置信度计算        硬约束全过后才有资格算分
   │  ④ 最终分流          四个出口
   ↓
┌────────────────┬────────────────┬────────────────┬──────────────────┐
│ 高置信 自动接管 │ 中置信 用户消歧 │ 低置信 保守处理 │ LLM 兜底          │
│ 符合规则，允许  │ 展示候选列表，  │ 不进入行程，    │ 结构化语义补充：  │
│ 进入对应业务    │ 绝不静默选择    │ 保留原始表达    │ 规则无法理解时    │
│ 处理            │                 │                 │ 辅助抽取，结果    │
│                 │                 │                 │ 仍须 EntityResolver│
└────────────────┴────────────────┴────────────────┴──────────────────┘
```

LLM 兜底是**旁路**不是第四档并列层：规则理解不了用户表达时，LLM 补充
结构化候选/槽位建议，其产物**回流** CandidateExtractor→EntityResolver→
DecisionEngine 主链，走同一套验证与分流。

## 2. 硬约束优先原则（验收意见四.1 的落实）

第四阶段的实现里，城市/类型冲突表现为扣分（−0.12 / −0.25），只是结果
恰好落不到阈值——**分数不是约束**。第五阶段改为显式两段式：

```
DecisionEngine.decide(resolved, context, intent)
   │
   ① 硬约束检查（任何一条失败 → 直接定档，不看分数）
   │   HC-1 城市归属冲突      matches 与 query_city/destination 不符 → ambiguous
   │                          （0.92 分也不能把南京餐厅确认成上海餐厅）
   │   HC-2 类型冲突          restaurant vs attraction → ambiguous
   │   HC-3 分店/同名歧义     多记录 → ambiguous，列全量候选
   │   HC-4 角色闸            negated/remove 实体验证照常，但绝不进入
   │                          行程决策（见 §3）
   │   HC-5 写入权限闸        destination 只许规划类意图写；LLM 无权写
   │   HC-6 探测证据闸        rejected 必须有 not_found 证据，否则最多保守处理
   │
   ② 存在性与唯一性检查
   │   唯一命中 → 进入 ③；多命中 → ambiguous；无命中且无证据 → 保守/unverified
   │
   ③ 置信度计算（沿用第四阶段分值表，仅作排序与边界参考）
   │
   ④ 分流：
       高置信自动接管（硬约束全过 + 唯一 + confidence ≥ 0.75）
       中置信用户消歧（硬约束命中或多候选）
       低置信保守处理（无存在性证据：不进行程、保留原始表达）
       LLM 兜底（主链无法产出任何可用结构时才触发，见 §4）
```

要点：**先检查能否确认，再计算置信度**。分数只影响同档位内的排序
（比如消歧候选列表的展示顺序），永不跨档位救回一条被硬约束拦下的记录。

## 3. entity_status × candidate_role：两个独立维度

实体验证回答「它是什么」，角色回答「用户想对它做什么」。第五阶段明确
二者正交，任何决策不得用一个覆盖另一个：

| candidate_role | entity_status=confirmed | ambiguous | rejected | unverified |
|---|---|---|---|---|
| must_visit | 可进入行程决策 | 请求消歧 | 不进入，如实告知 | 不进入，说明未核实 |
| query | 可直接回答 | 列候选回答 | 如实回答查无此地 | 如实说明暂无法核实 |
| nearby_anchor | 可作检索锚点 | 消歧后再用 | 不作锚点，诚实回答 | 不作锚点 |
| negated / remove | **绝不加入行程**；验证结果仅供后续一致性引擎（DEP-001） | 同左 | 同左 | 同左 |

「不想去总统府」「去掉苍山」「不要安排夫子庙」：实体都真实存在
（confirmed），但角色决定它们与行程决策无关。

## 4. LLM 兜底边界（六条红线）

LLM（DeepSeek，llm.py 既有通道）只做**结构化语义补充**，默认关闭、
可降级：

1. LLM 可以辅助理解用户表达（指代、口语化说法、隐含槽位），
   **不能直接确认 POI 存在**——存在性结论只来自 EntityResolver。
2. LLM 输出的城市、景点、餐厅候选一律视为「新候选文本」，
   **必须回流 EntityResolver 验证**后才能进入分流，绝不直通业务。
3. LLM **不能直接修改 slots.destination**（写入权限闸 HC-5 对 LLM
   同样生效；它只能建议，由规划意图链路按既有闸门落值）。
4. LLM **不能直接执行 plan_trip**。
5. LLM **不能直接删除、移动或新增真实行程节点**（行程修改仍是
   PlanHandler 的既有受控链路）。
6. **用户消歧前，绝不静默挑选分店**——ambiguous 的唯一出口是向用户
   展示候选列表。

降级路径：无 Key / 超时 / 输出不可解析 → LLM 兜底静默跳过，走保守
处理（保留原始表达、如实说明），**绝不报错打断主链，绝不编造**。

## 5. 分批接管路线（影子决策 → 查询类 → 规划类）

| 批次 | 范围 | 准入条件 | 回退 |
|---|---|---|---|
| 5.0 影子决策 | DecisionEngine 全量影子运行，`ParseResult.shadow_decision` 记录「若接管会怎样」与旧链路实际行为对比 | 设计评审通过 | 无需回退（纯记录） |
| 5.1 查询第一批 | poi_query / poi_location_query / category_query（只读、无副作用） | 5.0 影子决策与旧链路一致率达标 + corpus v5 全绿 | 开关 `TRAVEL_DECISION_TAKEOVER=0` 立即回旧链路 |
| 5.2 查询第二批 | nearby_food_query / restaurant_search / food_query / hotel_search | 5.1 稳定（无新增语料回归） | 同上 |
| 5.3 规划类 | plan_modify 的点名/排除槽位消费 DecisionEngine 结果 | 5.2 稳定 + 用户消歧通道上线 | 同上 |
| LLM 兜底 | 随 5.1 起旁路挂载，默认关闭（设置项开启） | mock LLM 专项测试全绿 | 默认关闭即回退 |

每批接管只开一个消费方；任何一批出现语料回归，立即回退并冻结该批。

## 6. 实施计划（任务分解）

| 任务 | 内容 | 产出 | 前置 |
|---|---|---|---|
| P0 探测元数据 | probe_source/probe_reason 分离记录；缓存与调用限额 | entity_resolver.py 小改 + 语料补断言 | 无 |
| P1 DecisionEngine | decision.py：硬约束→唯一性→置信度→分流；影子决策接入 ParseResult.shadow_decision | 新模块 + 影子接线 + 单测 | P0 |
| P2 用户消歧通道 | Session 多轮确认（展示候选列表、记住 pending 消歧上下文），ambiguous 的唯一出口 | dialogue.py 消歧轮次 + 单测 | P1 |
| P3 查询类接管 | 按 5.1→5.2 批次把查询意图的实体消费切到 DecisionEngine，带接管开关 | handlers 改造 + 语料断言 | P1/P2 |
| P4 LLM 兜底 | 结构化语义补充（候选建议/槽位建议），输出强制回流 EntityResolver；六红线逐条测试 | llm 侧新函数 + 回流测试 | P1 |
| P5 规划类消费 | 5.3 批次 | PlanHandler 改造 | P3 |
| 语料 v5 | 决策层断言：硬约束优先、rejected/unverified 不混淆、角色×状态正交、LLM 六红线（mock） | corpus_v5.tsv + test_corpus_v5.py | P1（随做随补） |

顺序：P0 → P1 → P2 → P3/P4（并行）→ P5；语料 v5 随任务同步扩充。
每步提交独立 commit，回归门槛：既有 v1–v4 + 全套件绿 + 新语料绿。

## 7. 验收标准（第五阶段）

1. 硬约束专项：城市冲突 0.92 分也进 ambiguous（不改判城市）；类型冲突同。
2. rejected/unverified 专项：探测成功无结果 → rejected；超时/无 Key/
   限额 → unverified，且决策层行为不同（rejected 可如实回答查无此地，
   unverified 只能说明暂无法核实）。
3. 角色×状态矩阵逐格断言（negated+confirmed 绝不进行程）。
4. LLM 六红线逐条专项（mock LLM，离线可复现）：输出回流验证、destination
   不可写、不可执行 plan_trip、不可动行程节点、不静默选分店、降级不报错。
5. 消歧通道：ambiguous 只产候选列表，不产生任何静默选择。
6. 影子决策对比报告：与新旧行为逐条分类（一致 / 决策差异 / 旧链路缺陷）。
7. 全量回归：v1–v4 语料 + 全套件绿；影子 KB-only 默认不产生网络请求。

## 8. 明确不做（阶段边界）

- 不修 DEP-001（行程依赖一致性引擎，仍等实体 ID/节点 ID 稳定）。
- 不改 Composer、路线优化、预算计算。
- 不做在线写库（probe-then-store 仍是旧链路 ensure_spot 的职责，
  且本阶段不扩大它）。
- LLM 不做存在性判断、不做行程写操作（§4 红线即边界）。
- 不一次性接管全部意图（严格按 §5 批次）。
