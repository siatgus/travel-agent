# 第五阶段设计：置信度三档分流接管与 LLM 语义兜底（v2 评审修订版）

> 版本历史：
> v1 `e0438ab`（2026-11-18 初稿）→ **v2 本文**（2026-11-18 评审修订，仅文档，
> 未动业务代码）。第四阶段基线：commit `2f66f4e`。
> 本阶段定位：**建立可靠的决策边界**，让三、四阶段的结果逐步参与业务决策
> ——不是继续堆叠识别规则。

## 0. 评审修订总览（v1 → v2 关键差异）

| # | 修订点 | v1 初稿 | v2 修订后 | 修改原因 |
|---|---|---|---|---|
| 1 | rejected 证据标准 | probe 成功且零结果 → rejected | 单源空结果 → unverified（保守处理）；**双源复核均成功且均无结果**才可 rejected；回复措辞一律保守 | 关键词搜索空数组 ≠ 现实中不存在；单一数据源无结果不得向用户绝对断言 |
| 2 | negated/remove | 统一「验证结果供 DEP-001 使用」 | negated 交回**现有排除逻辑**、remove 走**现有 PlanHandler 删除链路**；两者都不能被当作 must_visit；DEP-001 只是未来联动，不是现有删除功能的前置条件 | 现有删除/排除功能不能被未实现的 DEP-001 阻塞 |
| 3 | LLM 候选来源 | 只提「输出回流验证」 | 新增 **source 契约**（user/llm/kb/online）+ **LLMCandidateAdapter 独立适配器**；source=llm 的候选 Span 不存在时保持 null，绝不伪造；不得直接成为 must_visit | LLM 生成文本不在原始输入中，CandidateExtractor 无法提供真实 Span |
| 4 | 消歧状态 | 「Session 多轮确认」一句话 | 完整 **PendingDisambiguation 状态契约**（8 字段）+ 7 条消费规则（快照引用/一次性消费/过期/取消/新问题不误消费/改目的地重验证/选分店≠授权修改） | 多轮消歧是无状态会话里最容易出错的环节，必须先定契约 |
| 5 | 枚举设计 | status 与 probe_status 都出现 skipped | **正交枚举**：status 五值；probe_status 五值（not_applicable/not_requested/success_hit/not_found/unavailable）；probe_reason 六值；合法组合表 + 组合测试 | 两字段共用 skipped 含义不清，无法归因 |
| 6 | 城市硬约束 | 统一与 destination 比对 | **按意图选城市字段**：查询类比 query_city、规划类比 destination、departure_city 仅路线；query_city ≠ destination 绝不判冲突 | 查询类会被误判为城市冲突（「问上海的成都小吃」） |
| 7 | 唯一性判定 | 同名多记录直接 ambiguous | **先实体归并，再判唯一性**：POI ID > 实体 ID > 地址/坐标证据；无证据保留歧义，不凭名称相似合并分店 | 名称、别名、分店、多源记录会被误算成多个实体 |
| 8 | 接管门槛 | 「新旧一致率达标」 | **corpus 预期结果 + 硬约束负向断言为准**；新旧差异三分类（修复旧缺陷/合理决策差异/新系统回归）；高风险错误零容忍 | 一致率会把旧链路缺陷当标准 |
| 9 | 实施顺序 | P0→P1→P2→P3/P4 并行→P5 | **P0→P1→P1.5→P2→P3→P4→P5**；P4 在 P3 稳定之后，不并行；P3 只接管具体实体候选 | LLM 兜底与查询接管并行上线风险不可控；泛化问答仍归 Handler/DataService |

---

## 1. 术语与语义基线（第四阶段验收结论，v2 修订）

### 1.1 五状态 + skipped 旁路

实现为五种状态：confirmed / ambiguous / rejected / unverified / skipped。
`skipped` 是「不适用实体验证」的旁路（品类词、城市、空候选），不归入
四档分流。

### 1.2 rejected 的充分证据条件（v2 修订，核心修正）

**探测结果五分法**（统计口径，绝不统一记为「未匹配」）：

| 情形 | probe_status | probe_reason | status |
|---|---|---|---|
| KB 已收录 | not_applicable | — | confirmed/ambiguous |
| 未执行在线探测（KB-only 影子 / 策略关闭） | not_requested | disabled_by_policy | unverified |
| 探测失败（网络/Key/熔断/无坐标/超限额） | unavailable | network_timeout / no_key / circuit_open / no_city_center / quota_exceeded | unverified |
| 探测成功但当前数据源无匹配 | not_found | data_source_empty（单源）| **unverified（保守处理）** |
| **双源复核均成功且均无结果** | not_found | cross_source_empty | **rejected** |

- **rejected 充分证据**：主源（高德）与复核源（Wikivoyage）**均成功执行**
  且均返回无匹配；任一源 unavailable → 整体 unverified。
- **单源空结果绝不 rejected**：降级 unverified，决策层行为是保守处理。
- 措辞约束：即使 rejected，用户可见回复是「**按当前数据源没有查到**」，
  绝不绝对断言「这家店不存在」（数据源有覆盖盲区）。
- rejected 与 unverified 的决策行为不同：rejected 可如实告知查无此地并
  引导换说法；unverified 只能说明暂无法核实，不得下结论。
- probe 多候选（success_hit 且 matches>1）→ ambiguous，独立于以上情形。

### 1.3 低置信度 ≠ rejected；ambiguous ≠ 怀疑存在

- 分数低但无存在性证据 → 保守处理，probe 字段保持真实值，绝不倒填。
- ambiguous 只表示「需要用户消歧」：同名餐厅南京、上海各一家，两家都
  真实存在，只是用户没指定。

### 1.4 影子默认 KB-only 保持不变（已裁定）

`TRAVEL_RESOLVER_ONLINE=1` 才允许在线探测；在线探测有缓存与调用限额
（超限 unavailable + reason=quota_exceeded）。

---

## 2. 目标架构

```
CandidateExtractor（Phase 3：候选抽取、Span、类型、角色）
   ↓
EntityResolver（Phase 4 + v2 修订：存在性验证、城市归属、类型、
│               实体归并 → 唯一性、分店消歧）
│   新增：②实体归并（POI ID/实体 ID/地址证据），归并后再判唯一性
│   新增：双源复核探测（主源高德 + 复核源 Wikivoyage）
   ↓
LLMCandidateAdapter（Phase 5 新增，独立适配器）
│   LLM 结构化输出 → 候选对象；source=llm、Span 不可伪造（null）
│   产物回流 EntityResolver，走同一套验证
   ↓
DecisionEngine（src/travel_agent/decision.py）
│  ① 硬约束检查（HC-1~HC-7，§3）
│  ② 存在性与唯一性（基于归并后实体）
│  ③ 置信度计算（仅硬约束全过后）
│  ④ 最终分流
   ↓
高置信自动接管 │ 中置信用户消歧（PendingDisambiguation）│
低置信保守处理 │ LLM 兜底（主链无可用结构时旁路触发，产物回流）
```

## 3. 硬约束清单（v2 修订：先检查能否确认，再算分）

| 编号 | 硬约束 | v2 修订要点 |
|---|---|---|
| HC-1 | 城市归属 | **按意图选城市字段**：查询类比 `query_city`、规划类比 `destination`、`departure_city` 仅用于路线框架。查询句 `query_city ≠ destination` **绝不判城市冲突**（「问上海的成都小吃」不是冲突）。命中城市与所选字段不符 → ambiguous，0.92 分也不改判 |
| HC-2 | 类型冲突 | restaurant vs attraction → ambiguous |
| HC-3 | 分店/同名歧义 | 归并后仍多实体 → ambiguous，列全量候选 |
| HC-4 | 角色闸 | negated/remove 验证照常，但**永不进入新增行程路径**（§5） |
| HC-5 | 写入权限闸 | destination 只许规划类意图写；LLM 无权写 |
| HC-6 | 探测证据闸 | rejected 必须满足 §1.2 充分证据，否则最多保守处理 |
| HC-7 | 归并闸 | 无充分证据不得凭名称相似合并记录（§4） |

分数只做同档内排序（如消歧候选展示顺序），永不跨档救回被硬约束拦下
的记录。

## 4. 实体归并先于唯一性（v2 新增）

同一 POI 的名称、别名、分店记录、多数据源记录**不能直接算成多个实体**。

归并证据优先级（高→低）：
1. 稳定 POI ID / 实体 ID（KB 主键、高德 POI ID）
2. 地址证据（同城市 + 同地址/门牌）
3. 坐标邻近（同城市且距离 ≤ 阈值，如 100 米）
4. 品牌名 + 分店后缀解析（「海贝皇海鲜大排档」与「海贝皇海鲜大排档
   郑和南路店」是品牌与分店，不是两处）

规则：证据不足时**保留歧义**（ambiguous），绝不仅凭名称相似度合并
不同分店；归并产生的合并/保留决策全部写入 notes 供影子对比归因。

## 5. negated 与 remove 分离（v2 新增）

实体验证与行程动作是两条链路，**互为前置**：

| 角色 | 行为归属 | 决策层行为 |
|---|---|---|
| negated | 交回**现有排除逻辑**（slots.avoid / PlanHandler 排除链路） | 不允许进入新增行程路径；验证结果只决定排除提示的措辞 |
| remove | 保留**现有 PlanHandler 删除链路** | 不经 DecisionEngine 做任何删除动作 |
| 两者 | — | **都不能被 DecisionEngine 当作 must_visit**（HC-4） |

- DEP-001 只负责未来复杂行程依赖联动（如删景点联动删除其分店餐位），
  **不得成为现有删除功能的前置条件**。
- 独立行为断言（语料 v5 必含）：
  - 「去掉苍山」：走删除链路，方案中苍山被移除，**不**触发新增/消歧；
  - 「不想去总统府」：走排除链路，新方案不再排入总统府，**不**触发
    删除报错，也**不**被当作 must_visit。

## 6. LLM 候选来源契约（v2 修订）

### 6.1 source 字段（候选对象新增，四枚举）

| source | 含义 | Span |
|---|---|---|
| user | 来自用户原始文本（CandidateExtractor / 旧剥词产物） | 真实 Span |
| llm | LLM 结构化输出生成 | 文本在原始输入中→真实 Span；**不在→null，绝不伪造** |
| kb | KB 归并/别名解析产物 | 视来源 |
| online | 在线探测返回 | null |

Span 契约扩展：CandidateExtractor 只能为原始用户文本提供 Span；
**不假设它能给模型生成文本提供 Span**。

### 6.2 LLMCandidateAdapter（独立适配器）

- 职责单一：LLM JSON 输出 → 候选对象（text / entity_type / source=llm /
  span=null 或真实值），不与 CandidateExtractor 耦合。
- 产物强制回流：EntityResolver → DecisionEngine 全链验证，与用户候选
  同一套硬约束与分流。
- **LLM 建议的候选不能直接成为 must_visit**：必须通过来源校验
  （source=llm 标记不可丢）+ EntityResolver + DecisionEngine。

### 6.3 LLM 六条红线（v1 保留）

1. LLM 可以辅助理解用户表达，不能直接确认 POI 存在。
2. LLM 输出的候选一律回流 EntityResolver 验证，绝不直通业务。
3. LLM 不能直接修改 slots.destination（HC-5 同样生效）。
4. LLM 不能直接执行 plan_trip。
5. LLM 不能直接删除、移动或新增真实行程节点。
6. 用户消歧前，绝不静默挑选分店。

降级：无 Key / 超时 / 输出不可解析 → 静默跳过走保守处理，绝不报错
打断主链，绝不编造。

## 7. 多轮消歧状态契约 PendingDisambiguation（v2 新增）

```python
@dataclass
class PendingDisambiguation:
    pending_id: str            # 唯一 ID（uuid）
    original_utterance: str    # 触发消歧的原始句子
    original_intent: str       # 当时的细分意图（poi_query / plan_modify…）
    original_role: str         # 候选当时的角色（must_visit / query / nearby_anchor）
    candidates: list[dict]     # 候选快照（含 source/confidence/city，冻结不再变）
    created_at: str            # ISO 时间
    expires_at: str            # 过期时间（如 30 分钟）
    status: str                # open / consumed / cancelled / expired
```

消费规则（七条，逐条进测试）：
1. 用户按序号选择时，**只能引用当前 pending 的候选快照**——会话期间
   KB 变化不影响已冻结快照。
2. 用户取消 → status=cancelled，pending 清除。
3. 用户提出新问题（与 pending 无关的意图）→ **不得误消费旧 pending**，
   旧 pending 按过期策略处理。
4. 用户修改目的地 → 候选的城市归属已变化，**重新走 EntityResolver
   验证**，旧快照作废。
5. pending 过期（expires_at 已过）→ status=expired，**不得继续执行**，
   如实告知需重新提问。
6. **同一个 pending 只能成功消费一次**（consumed 后再选序号无效）。
7. **用户选择分店只完成消歧**（ambiguous → confirmed），**不等于授权
   执行行程修改**——行程动作仍走对应 Handler 的既有受控链路。

## 8. 枚举正交与组合合法性（v2 修订）

```text
status（业务分流，五值）:
  confirmed / ambiguous / rejected / unverified / skipped

probe_status（探测进度，五值，不再复用 skipped）:
  not_applicable   KB 已命中，无需探测
  not_requested    策略上未探测（KB-only 影子 / 关闭）
  success_hit      探测成功且有结果
  not_found        探测成功且无结果
  unavailable      探测没做成（必须带 probe_reason）

probe_reason（探测原因/失败原因，六值起步）:
  network_timeout / no_key / circuit_open / no_city_center /
  quota_exceeded / disabled_by_policy
  （+ data_source_empty / cross_source_empty，仅配 not_found）
```

合法组合表（组合测试的判定依据）：

| status | 合法 probe_status | 约束 |
|---|---|---|
| confirmed | not_applicable / success_hit | 唯一实体 + 硬约束全过 |
| ambiguous | not_applicable / success_hit | 多实体或硬约束命中 |
| rejected | not_found | **必须 reason=cross_source_empty**（双源复核） |
| unverified | not_requested / unavailable / not_found | not_found 时 reason=data_source_empty（单源保守）或 unavailable 带 reason |
| skipped | not_applicable | 品类/城市/空候选 |

非法组合示例（测试必须拦截）：(rejected, unavailable)、(rejected,
not_found + data_source_empty)、(skipped, success_hit)、
(unavailable 无 probe_reason)。

## 9. 分批接管与验收门槛（v2 修订）

### 9.1 接管范围边界（P3）

- P3 只处理**具体实体候选**的意图（点名景点、点名餐厅位置、点名分店）。
- **特色美食问答、泛化餐厅品类搜索、泛化酒店推荐仍由对应 Handler 和
  DataService 处理**——实体存在性不能代替酒店房态、价格查询。
- LLM 兜底默认关闭，随 5.1 起旁路挂载但需设置项开启。

### 9.2 批次

| 批次 | 范围 | 说明 |
|---|---|---|
| 5.0 影子决策 | DecisionEngine 全量影子运行，`shadow_decision` 记录 | 纯记录，纯 KB-only |
| 5.1 查询第一批 | poi_query / poi_location_query / category_query（只读） | 具体实体候选 |
| 5.2 查询第二批 | nearby_food_query / restaurant_search / hotel_search 的**点名实体部分** | 泛化部分仍走 Handler/DataService |
| 5.3 规划类 | plan_modify 的点名/排除槽位 | 消歧通道上线后 |

### 9.3 准入门槛（v2 修订：一致率不是唯一标准）

- **以 corpus 预期结果和硬约束负向断言为准**，不是新旧一致率。
- 新旧差异逐条三分类：
  1. 新系统**修复旧链路缺陷**（如旧链路把城市当品类）
  2. **合理的决策差异**（行为不同但均正确，需人工确认记录）
  3. **新系统回归**（旧行为正确、新行为变差）→ 阻断接管
- **高风险错误必须为零**：
  - 错误城市确认（跨城实体被静默确认）
  - 否定对象新增（negated/remove 实体被排进行程）
  - 静默选择分店（ambiguous 未消歧直接执行）
  - rejected/unverified 混淆（无充分证据下断言不存在）
- 回退：`TRAVEL_DECISION_TAKEOVER=0` 立即回旧链路。

## 10. 实施顺序（v2 修订）

```
P0  探测元数据与限额   probe_status/probe_reason 正交枚举落地（§8）、
│                      调用限额；单源空结果降级 unverified（§1.2）
P1  DecisionEngine     decision.py：硬约束→归并→唯一性→置信度→分流；
│                      影子决策接入 shadow_decision
P1.5 契约补全（新增）  PendingDisambiguation 状态契约 + LLMCandidateAdapter
│                      契约：仅契约定义与测试桩（fake LLM/fake pending），
│                      不上线、不接业务
P2  消歧通道           PendingDisambiguation 实现 + Session 多轮消费规则
P3  查询类接管         按 5.1→5.2 批次，仅具体实体候选；泛化问答不动
P4  LLM 兜底           **P3 稳定之后**（不并行上线）；适配器接真 LLM，
│                      产物回流验证，六红线测试
P5  规划类消费         5.3 批次
```

每步独立 commit；回归门槛：v1–v4 语料 + 全套件绿 + 当步新语料绿。

## 11. 需要新增的测试用例清单（v2）

| 组 | 用例 | 断言 |
|---|---|---|
| T-A rejected/unverified 不混淆 | 单源空结果 | status=unverified（reason=data_source_empty），绝不 rejected |
| | 双源复核均空 | status=rejected（reason=cross_source_empty） |
| | 主源空 + 复核源 unavailable | status=unverified，整批不得 rejected |
| | rejected 用户措辞 | 回复不含「不存在」，含「没有查到」类表述 |
| | unavailable 必带 reason | 非法组合拦截 |
| T-B negated/remove 分离 | 「去掉苍山」 | 走删除链路：方案移除苍山；无新增、无消歧、无 must_visit |
| | 「不想去总统府」 | 走排除链路：新方案不排入；无删除报错；role≠must_visit |
| | 「不要安排夫子庙」 | DecisionEngine 输出不得含 must_visit |
| | 角色×状态矩阵 | negated/remove × confirmed/ambiguous/unverified 全格不进行程 |
| T-C LLM 来源契约 | LLM 生成新候选 | source=llm、span=null；不得伪造 Span |
| | LLM 建议回流 | 输出经 EntityResolver 验证后才出现在分流结果 |
| | LLM 建议 must_visit | 直接建议被拒：必须经全链 |
| | 适配器脏输入 | 非法 JSON/缺字段 → 降级不报错 |
| | 六红线 mock | destination 不可写 / 不执行 plan_trip / 不动行程节点 / 不静默选分店 |
| T-D PendingDisambiguation | 快照引用 | KB 变更后按序号选择仍命中快照 |
| | 取消清除 | cancelled 后选择无效 |
| | 新问题不误消费 | 无关意图不消费 open pending |
| | 改目的地重验证 | destination 变更 → 旧快照作废、重新验证 |
| | 过期不可执行 | expired → 拒绝执行并告知 |
| | 一次性消费 | consumed 后二次选择无效 |
| | 选分店≠授权 | 消歧完成只置 confirmed，不触发行程修改 |
| T-E 枚举组合 | 合法组合表 | §8 全表逐格断言 |
| | 非法组合 | (rejected, unavailable) 等拦截 |
| T-F 城市字段按意图 | 查询类 query_city≠destination | 不判城市冲突（「问上海的成都小吃」） |
| | 规划类 destination 冲突 | 跨城命中 → ambiguous |
| | departure_city | 仅路线框架使用 |
| T-G 实体归并 | 同 POI 多记录（ID/地址/坐标证据） | 归并后唯一 → confirmed |
| | 名称相似但无证据 | 保留 ambiguous，不合并 |
| | 品牌+分店后缀 | 归并为品牌与分店关系，不误合并两处 |
| T-H 接管门槛 | 新旧差异三分类 | 修复缺陷/合理差异/回归 逐条可归因 |
| | 高风险零容忍 | 错误城市确认、否定对象新增、静默选分店、混淆 rejected/unverified → 一票否断言 |

## 12. 明确不做（阶段边界，v2 重申）

- 不修 DEP-001（且 DEP-001 不是现有删除/排除功能的前置条件）。
- 不改 Composer、路线优化、预算计算。
- 不做在线写库（probe-then-store 仍是旧链路 ensure_spot 职责，不扩大）。
- LLM 不做存在性判断、不做行程写操作（§6.3 红线即边界）。
- 不一次性接管全部意图（严格按 §9 批次）；泛化美食/酒店问答不动。

## 13. 设计变更记录

| 日期 | 版本 | 变更 | 原因 |
|---|---|---|---|
| 2026-11-18 | v1（e0438ab） | 初稿 | 第五阶段设计评审 |
| 2026-11-18 | v2（本文） | §1.2 rejected 证据标准改为双源复核充分条件，单源空结果降级 unverified；新增探测结果五分法 | 关键词搜索空数组 ≠ 现实不存在，单源无结果不得绝对断言（评审问题 1） |
| 2026-11-18 | v2 | §5 negated/remove 分离：negated→现有排除逻辑、remove→现有删除链路，DEP-001 不做前置；新增两条独立行为断言 | 现有删除功能不能被未实现的 DEP-001 阻塞（评审问题 2） |
| 2026-11-18 | v2 | §6 新增 source 四枚举、Span 不可伪造（llm→null）、LLMCandidateAdapter 独立适配器、LLM 候选不得直接 must_visit | LLM 生成文本无真实 Span，CandidateExtractor 不承担模型文本的 Span（评审问题 3） |
| 2026-11-18 | v2 | §7 新增 PendingDisambiguation 八字段契约 + 七条消费规则 | 多轮消歧是无状态会话高风险点，先定契约（评审问题 4） |
| 2026-11-18 | v2 | §8 枚举正交：probe_status 弃用 skipped、not_found 拆单源/双源，probe_reason 六值 + 合法组合表 | 两字段共用 skipped 无法归因（评审问题 5） |
| 2026-11-18 | v2 | HC-1 城市硬约束按意图选字段（query_city/destination/departure_city） | 查询类会被误判城市冲突（评审要求 6） |
| 2026-11-18 | v2 | §4 实体归并先于唯一性，证据优先级 + 无证据保留歧义 | 名称/别名/分店/多源记录会误算多实体（评审要求 7） |
| 2026-11-18 | v2 | §9.3 接管门槛改 corpus 预期 + 硬约束负向断言，差异三分类，高风险零容忍 | 一致率会把旧链路缺陷当标准（评审要求 8） |
| 2026-11-18 | v2 | §10 实施顺序 P0→P1→P1.5→P2→P3→P4→P5，P4 不与 P3 并行；P3 只接管具体实体候选，泛化问答归 Handler/DataService | LLM 与查询接管并行上线风险不可控（评审要求 三） |
