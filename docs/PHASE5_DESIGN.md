# 第五阶段设计：置信度三档分流接管与 LLM 语义兜底（v3.2.1 开发基线）

> 版本历史：
> v1 `e0438ab`（2026-11-18 初稿）→ v2（2026-11-18 评审修订）→ v3
> （2026-11-18 最后一轮修订）→ v3.1（开发指令补录 + 六项设计落地）
> → v3.2（契约冻结：五档/六档分离等 8 项收敛）→ **v3.2.1 本文**
> （2026-11-18 开发前契约修订 5 处：deferred 定义、LLM 职责收缩、
> 接入点 A 执行顺序、probe 组合校验、语料字段冻结）。
> 第四阶段基线：commit `2f66f4e`（验收已通过）。
> **本版为实施基线，核心契约不再改动。**
> 本阶段定位：**建立可靠的决策边界**，让三、四阶段的结果逐步参与业务
> 决策——不是继续堆叠识别规则。

## 0. 修订总览

### 0.1 v1 → v2（上一轮评审，保留存档）

| # | 修订点 | 要点 |
|---|---|---|
| 1 | rejected 证据标准 | 单源空结果 → unverified；双源复核均空才可 rejected |
| 2 | negated/remove 分离 | negated→现有排除逻辑、remove→现有删除链路；DEP-001 不做前置 |
| 3 | LLM 候选契约 | source 字段 + 独立适配器；Span 不可伪造 |
| 4 | 消歧状态契约 | PendingDisambiguation 八字段 + 七条消费规则 |
| 5 | 枚举正交 | status / probe_status / probe_reason 三字段分离 + 合法组合表 |
| 6 | 城市硬约束 | 按意图选城市字段（query_city ≠ destination 不判冲突） |
| 7 | 实体归并 | 先归并再判唯一性，无证据保留歧义 |
| 8 | 接管门槛 | corpus 预期 + 硬约束负向断言；差异三分类；高风险零容忍 |

### 0.2 v2 → v3（本轮评审）

| # | 修订点 | v2 | v3 | 原因 |
|---|---|---|---|---|
| 1 | LLM 数据流 | 适配器画在 EntityResolver 之后，又说产物要回流 EntityResolver，**数据流矛盾** | LLMCandidateAdapter 与 CandidateExtractor **并列汇入统一候选集合**，之后同走 EntityResolver→DecisionEngine→Handler；LLM 仅在规则主链无可用结构且符合兜底条件时触发；不允许旁路 | 消除数据流矛盾 |
| 2 | 来源字段拆分 | 单一 source 字段同时表示候选来源与验证来源 | **candidate_source**（user/llm）与 **evidence_sources**（kb/online）正交拆分；LLM 候选仅原文精确匹配才有真实 Span | 一个字段承担两种语义无法归因 |
| 3 | P3 接管范围 | 按意图名列表接管 | **category_query 泛化品类不进实体验证**；poi_query 必须检查存在具体实体候选才接管（不得仅凭意图名）；新增完整路由边界表；nearby_food_query 只验证用户指定锚点 | 泛化搜索被误拉进实体验证 |
| 4 | 消歧快照 | 候选快照无 ID/顺序字段 | 每个候选快照增加 candidate_id / display_order / entity_id / name / city / address；单 Session 单 active pending；候选 ID 与展示顺序冻结；状态消费**原子化** | 防重复提交、防顺序漂移、防跨 pending 误引用 |
| 5 | rejected 语义 | 「双源复核均空」 | rejected =「**在规定的数据源与检索范围内没有找到**」，绝不代表现实中绝对不存在；双源复核必须检查**数据源独立性**与**实体类型适用范围**，不适用源不强制执行；逐源 **probe_results** 保留成功/无结果/失败/原因；证据不足保持 unverified | 双源都未收录 ≠ 现实不存在；适用性不明的复核源会制造假证据 |
| 6 | 测试清单 | T-A~T-H | 新增 **T-I**（9 条：LLM Span 真实性、来源正交、双源一成一败、复核源不适用、快照顺序稳定、一次性消费、category_query 不进验证、nearby 只验锚点、query_city 不冲突） | 覆盖本轮修订 |

---

## 1. 术语与语义基线

### 1.1 五状态 + skipped 旁路

confirmed / ambiguous / rejected / unverified / skipped 五状态；
`skipped` 是「不适用实体验证」的旁路（品类词、城市、空候选）。

### 1.2 rejected 语义（v3 收紧）

**rejected 表示「在规定的数据源与检索范围内没有找到」，
绝不代表现实中绝对不存在。**

探测结果五分法：

| 情形 | probe_status | probe_reason | status |
|---|---|---|---|
| KB 已收录 | not_applicable | — | confirmed/ambiguous |
| 未执行在线探测（KB-only 影子 / 策略关闭） | not_requested | disabled_by_policy | unverified |
| 探测失败 | unavailable | network_timeout / no_key / circuit_open / no_city_center / quota_exceeded | unverified |
| 探测成功、规定范围内无匹配（单源） | not_found | data_source_empty | **unverified（保守处理）** |
| ≥2 个适用且独立的源均成功且无匹配 | not_found | cross_source_empty | **rejected** |

**双源复核的两个前置检查（v3 新增）：**

1. **数据源独立性**：复核源与主源的数据来源必须独立（高德与
   Wikivoyage 满足；同一上游的两个镜像不满足，只算一源）。
2. **实体类型适用范围**：复核源对目标城市/实体类型必须有覆盖能力
   （如 Wikivoyage 城市页缺失、或对某类餐厅覆盖明显不足 → 该源
   **不适用，不强制执行**，也不计入复核）。不适用时跳过该源
   （probe_results 记 skipped + 原因）。

- 任一适用源 unavailable → 整体 unverified。
- **逐源 probe_results**（v3 新增）：每条候选保留各数据源的独立结果：
  ```json
  "probe_results": [
    {"source": "amap",       "outcome": "success_empty", "reason": ""},
    {"source": "wikivoyage", "outcome": "skipped",
     "reason": "type_not_covered"}
  ]
  ```
  outcome ∈ success_hit / success_empty / failed / skipped；
  聚合出的 probe_status 是结论，probe_results 是证据明细。
- 措辞约束：即使 rejected，用户可见回复是「**在当前数据源与检索范围
  内没有找到**」，绝不表述为「这家店不存在」（双源都未收录 ≠ 现实
  不存在）。
- 决策行为区分：rejected 可如实告知规定范围内查无此地并引导换说法；
  unverified 只能说明暂无法核实，不得下结论。
- 多候选（success_hit 且归并后仍 >1 实体）→ ambiguous，独立情形。

### 1.3 低置信度 ≠ rejected；ambiguous ≠ 怀疑存在

分数低但无存在性证据 → 保守处理，probe 字段保持真实值；ambiguous
只表示「需要用户消歧」（同名餐厅南京、上海各一家，两家都真实）。

### 1.4 影子默认 KB-only 保持不变（已裁定）

`TRAVEL_RESOLVER_ONLINE=1` 才允许在线探测；在线探测有缓存与调用限额
（超限 unavailable + reason=quota_exceeded）。

---

## 2. 目标架构（v3 修正数据流）

```
用户输入
   ├─→ CandidateExtractor（规则主链；candidate_source=user）
   │            │
   │            │   兜底触发条件（同时满足才调 LLM）：
   └─→ LLMCandidateAdapter        ① 规则主链无可用结构
       （candidate_source=llm）    ② 兜底开关已开启
            │                     ③ LLM 输出可解析为结构化候选
            │                        ── 不满足则跳过，走保守处理
            ↓
   ┌─ 统一候选集合（每条带 candidate_source 与真实/null Span）─┐
   ↓
   EntityResolver（存在性验证、城市归属、类型、实体归并 → 唯一性、
   │               双源复核探测；evidence_sources=kb/online）
   ↓
   DecisionEngine（硬约束 → 归并唯一性 → 置信度 → 分流）
   ↓
   Handler（既有受控链路：查询应答 / PlanHandler / 消歧通道）
```

- LLMCandidateAdapter 与 CandidateExtractor **并列**产出候选，汇入
  同一集合；**所有 LLM 候选必须经过 EntityResolver 和
  DecisionEngine**，与用户候选走完全相同的验证与分流，**不允许任何
  旁路直接进入业务**。
- v2 版「适配器挂在 EntityResolver 之后」的画法已废弃（与要求产物
  回流 EntityResolver 相互矛盾）。
- 兜底是旁路**触发机制**，不是与四档并列的第四层：主链无可用结构时
  才触发，触发与否、触发结果全部记录进影子数据供归因。

### 2.1 五档运行时路由（v3.1 补录）

DecisionEngine 按 status 分流，五档各自是**运行时状态**而非仅影子
记录：

| status | 路由 |
|---|---|
| confirmed | 直接进入原业务（如「介绍一下总统府」→ _spot_qa，**绝不掉到泛推荐**） |
| ambiguous | **执行阻断态**：澄清不执行（「我查到几家海贝皇大排档，你想去哪一家？如果是南京这家，我可以直接排进南京行程」） |
| rejected | 查询/规划分别处理：查询如实告知规定范围内查无此地并引导换说法；规划不排入、保留原始表达 |
| unverified | 保守处理（「我暂时查不到这家餐厅的可靠信息，你可以告诉我所在城市或地址，我再帮你确认」） |
| skipped | 品类词，走品类查询/泛化 Handler |

### 2.2 模块职责表（v3.1 补录，命名对齐开发指令）

| 模块 | 负责什么 |
|---|---|
| parser.py | 用户说了什么（统一 Schema、三城市字段） |
| candidate_extractor.py | 里面有哪些实体候选（五层瀑布） |
| entity_resolver.py | 候选到底是谁（存在性验证与消歧） |
| **decision.py**（新增） | 系统现在应该做什么（消费 ParseResult + resolved 实体 + context，产出 DecisionResult） |
| **llm_entity_fallback.py**（新增） | 规则实在看不懂时辅助理解（LLMCandidateAdapter 落点，产出候选对象而非决策） |
| orchestrator.py | 调度整个流程（消费 DecisionResult；**不再在 _turn_impl 里堆 if/elif 做置信判断**） |

### 2.3 DecisionResult 契约（v3.2 冻结）

```python
@dataclass
class DecisionResult:
    action: str          # execute / clarify / reject_reply / conservative /
                         # category_path / deferred
    intent: str          # 细分意图（poi_qa / poi_query / plan_modify / ...）
    entity: object | None   # 归并后的已验证实体（可含 entity_id）
    city: str | None     # 本次决策实际采用的城市值
    city_source: str | None  # v3.2 冻结：query_city / destination /
                             # departure_city —— 决策归因依据
    confidence: float    # 同档内排序用，永不跨档
    reason: str          # 可归因说明（命中哪条硬约束/哪条分流规则）
    pending: PendingDisambiguation | None   # action=clarify 时携带
```

**冻结原则（v3.2，最高优先级）：**

1. **五档 status = DecisionEngine 的实体决策状态**（实体验证后处于
   什么状态：confirmed/ambiguous/unverified/rejected/skipped）；
   **六档 action = Orchestrator 的运行时路由动作**（下一步允许做什么）。
   两者严格分离——DecisionResult 里**没有 status 字段**，status 只在
   DecisionEngine 内部参与定档，对外只暴露 action。
2. **没有 route 字段**：action=execute 后走哪个 Handler 由既有 intent
   分支决定，DecisionEngine 只决策「能不能执行」，不指定具体 Handler。
3. **没有 candidates 字段**：ambiguous 的候选信息只放
   `pending: PendingDisambiguation`（快照），不再额外给
   `candidates=[]`。
4. **city 归因**：`city` 是值、`city_source` 是来源
   （DecisionCity(value, source) 的扁平化形式）。「从上海去南京，推荐
   几个当地餐厅」这类场景采用 departure_city 还是 destination 是决策
   归因问题，必须显式记录，不能让后续 Handler 猜。
5. 字段约束：`action` 六枚举；`entity` 为 None 时 action 不得为
   execute；`pending` 仅 action=clarify 时非空；`reason` 只作影子
   归因记录，**不决定回复文案**。
6. orchestrator 及所有 Handler 只消费 `DecisionResult`，不直接读
   resolver 原始记录；后续意图接管都在 decision.py 内扩展。
7. `confidence` 只在同档内使用（如消歧候选展示排序），任何硬约束失败
   直接定档，分数不参与跨档救回（§3）。

### 2.3.1 五档 status → 六档 action 固定映射（v3.2 冻结）

| 五档 status | action | 含义 |
|---|---|---|
| confirmed | execute | 已确认，进入原业务 |
| ambiguous | clarify | 执行阻断，只做消歧 |
| rejected | reject_reply | 查询/规划拒绝路径 |
| unverified | conservative | 保守处理，不执行实体业务 |
| skipped | category_path | 品类/泛化 Handler |
| 任意需要延后 | deferred | 已决策但明确不接管，交回原流程 |

映射固定、逐条进测试；DecisionEngine 内部五档定档逻辑变更时映射表
不变。

**deferred 双语义与返回值判定表（v3.2.1 冻结）：**

| 情况 | decide() 返回值 |
|---|---|
| TAKEOVER=0（默认） | None（影子记录后交回原流程） |
| 不在批次白名单 | None |
| 本轮不满足实体验证入口条件 | None |
| 已形成决策，但当前意图没有接管处理器 | DecisionResult(action="deferred") |
| 已形成有效实体决策 | 按上表五档映射 action |

**deferred 不是第六种实体状态，也不是五档状态的映射结果**——它是
执行策略层的「交回」动作，绝不进五档状态机（负向测试：任何
entity status 不得迁移为 deferred；deferred 不得作为 DecisionEngine
内部定档输入）。None = DecisionEngine 未形成结论；deferred = 形成了
结论但本步不消费，两者不混。

字段约束：`action` 六枚举；`city` 必须标注来源（query_city /
destination / departure_city，供归因）；`entity` 为 None 时 action
不得为 execute；`pending` 仅 action=clarify 时非空。

### 2.4 LLM fallback schema（v3.1 补录）

llm_entity_fallback.py 的 LLM 结构化输出契约（schema_version=1）：

```json
{
  "schema_version": 1,
  "intent": "poi_qa | poi_query | nearby_food_query | unknown",
  "entity_candidates": [
    {"text": "国民政府中央广播电台旧址",
     "entity_type": "attraction | restaurant | hotel | unknown"}
  ],
  "city_hints": {"query_city": "南京"},
  "notes": "规则只识别出『广播台』，按上下文补全全名"
}
```

硬性规则：
0. **LLM 职责只有两项（v3.2.1 冻结）**：① 实体文本补全（如把
   「民国时候的广播台旧址」补全为具体候选名称）；② 实体类型辅助
   识别（景点/餐厅/酒店）。**角色判定不在其职责内**——LLM 输出中
   不存在也不允许出现角色字段，query/must_visit 等角色归属由
   Parser / CandidateExtractor / 既有角色规则决定。
1. **白名单字段**：只有上列五个键；出现 `destination`、`plan`、
   `actions`、`poi_id`、`must_visit` 等任何越权字段 → 该字段丢弃并记
   notes（LLM 无权表达这些意图，见 §6.4 红线）。
2. **只产候选不产决策**：输出仅含「可能的实体文本 + 类型猜测 + 城市
   提示」，是否存在、归谁、做什么由 EntityResolver 与 DecisionEngine
   决定。
3. **适配器校验**：JSON 不可解析 / entity_candidates 为空 / text 超长
   或含标点注入特征 → 整体丢弃，走保守处理，不报错。
4. **来源标记**：经 LLMCandidateAdapter 产出后 candidate_source=llm；
   text 不在原始用户输入中 → span=null（§6.2）。
5. **城市提示不是写入**：city_hints.query_city 只作为 EntityResolver
   的验证上下文，不写 slots（HC-5）。
6. **LLM 不参与五档状态判断**（v3.2 冻结）：唯一合法链路是
   `LLM → 候选补全 → EntityResolver → DecisionEngine → 五档 status →
   DecisionResult.action`。LLM 输出**不含** action / status /
   confidence / decision / plan / destination / must_visit / poi_id /
   actions——schema 出现这些键一律越权丢弃。`plan` / `must_visit` /
   `destination` 三个键为**必测越权字段**（T-C 负向断言）。
7. **触发判据不含意图兜底项**（v3.2 冻结）：不使用「OTHER / 低置信度
   / rule conflict」类判据，唯一公式：
   `should_fallback = rule_structure_unavailable AND llm_fallback_enabled AND cost_gate_pass`
   （三个判据的展开见 §2.6）。

### 2.5 五档状态转换表（v3.1 补录）

**轮内转换**（单轮解析→验证→决策，由 §2.1 路由表决定，不可逆）：

```
skipped ← 品类词/城市/空候选（本轮终点，不参与后续）
confirmed ← KB/双源证据 + 硬约束全过
ambiguous ← 多实体/城市/类型/分店硬约束命中
unverified ← 未探测/探测失败/单源空（保守）
rejected ← 双源适用且独立均空（HC-6 证据闸）
```

**跨轮转换**（实体在同一会话的后续轮次中允许的状态迁移）：

| 当前 | 下一 | 触发条件 |
|---|---|---|
| ambiguous | confirmed | 用户从 pending 快照消歧选择（原子消费，规则 §7） |
| unverified | confirmed | 用户补充城市/地址/分店后，重验证获得 KB 或双源证据 |
| unverified | rejected | 仅当重试探测**实际成功**且双源均空；探测仍失败则维持 unverified |
| rejected | （不复活） | 同一文本保持 rejected；用户换说法产生**新候选**才重新走全链 |
| 任意 | ambiguous | 上下文变化（如改目的地）触发重新验证（§7 规则 4） |
| — | skipped | 只能来自品类词/城市/空候选，任何实体状态不得迁入 |

**禁止转换**（负向测试）：unverified → rejected（无充分证据）；
ambiguous → rejected（静默降档）；confirmed → unverified（证据不
灭）；任何状态 → must_visit（HC-4）。

**skipped 入口严格收紧（v3.2 冻结）**：「没有具体实体」≠ skipped。
「推荐一家酒店」这类无候选表达应走 unverified/conservative 或澄清
链；只有 §2.1 判定为**品类词 / 城市 / 空候选对应的泛化请求**才
skipped → category_path。断言形式：`entity status ≠ skipped` 与
`任何状态 ≠ must_visit` 同级列为必测负向。

**跨轮状态机验收用例（v3.2 冻结，语料必含）：**

| Case | 第一轮 | 第二轮 | 预期 |
|---|---|---|---|
| A | 介绍一下海贝皇大排档 → ambiguous | 南京那家 → 消费 pending 快照 | confirmed → execute |
| B | 介绍一下 XX 餐厅 → unverified | 就在南京夫子庙附近 → 补充城市重验证 | confirmed → execute |
| C | 查一下不存在的 XX → rejected | 还是这家 | **不复活**，仍 rejected → reject_reply |

### 2.6 LLM 兜底触发条件（v3.1 成文）

三个条件**同时满足**才调用（§2 ①②③ 的可执行判据）：

1. **规则主链无可用结构**，具体判据（满足其一）：
   a. 本轮实体候选为空，但意图需要实体（poi_qa / poi_query 类）；
   b. **实体边界不确定**（规则识别出的文本不完整，需要实体文本
      补全，如「广播台旧址」→「国民政府中央广播电台旧址」）；
   c. 实体类型不确定（无法判定候选更接近景点、餐厅还是酒店）。
   **注意（v3.2.1）：候选角色不确定（query/must_visit 等角色归属）
   不是触发条件**——角色永远由 Parser、CandidateExtractor 和既有
   角色规则负责，LLM 不参与角色判断（§2.4 规则 6）。
2. **兜底开关已开启**：`TRAVEL_LLM_FALLBACK=1`（**默认关闭**，影子
   阶段只统计触发判据命中数，不实际调用）。
3. **成本闸**：每轮最多一次调用、超时上限（默认 8s）、无重试风暴；
   触发与否、结果、耗时全部记入影子数据。

不满足任一条件 → 静默跳过，走 §2.1 的 unverified/澄清保守路径。

### 2.7 收敛总图（v3.2 冻结：五档在 Engine 内部，action 对外路由）

```
用户输入
   │
   ▼
parse_utterance（parser.py）
   │
   ▼
CandidateExtractor ─┐  统一候选集合（candidate_source=user/llm）
LLMCandidateAdapter ┘  （LLM 仅在 §2.6 三条件满足时补候选）
   │
   ▼
EntityResolver（存在性/城市/类型/归并；evidence_sources=kb/online）
   │
   ▼
DecisionEngine（decision.py）—— 五档 status 定档只发生在内部
   │      ┌────────────┼────────────┐
   │   五档状态    LLM fallback 候选   HC-1~HC-7 硬约束
   │      └────────────┼────────────┘
   ▼
DecisionResult（action / intent / entity / city+city_source /
   confidence / reason / pending）—— 无 status、无 route、无 candidates
   │
   ├─ execute      → 既有 intent 路由 → 原业务 Handler
   ├─ clarify      → 消歧通道（pending 快照）→ 消费后原业务 Handler
   ├─ reject_reply → 拒绝回复（规定范围内没找到 + 引导换说法）
   ├─ conservative → 保守回复（暂无法核实，请求补充信息）
   ├─ category_path→ 泛化链（品类 Handler / DataService）
   └─ deferred     → 原流程（decide 返回 None 为未接管的另一形态）
```

TAKEOVER=0（默认）：decide() 只影子记录，返回 None，**行为零改变**；
TAKEOVER=1 + 批次白名单：才按上表消费 action。

## 3. 硬约束清单（先检查能否确认，再算分）

| 编号 | 硬约束 | 要点 |
|---|---|---|
| HC-1 | 城市归属 | **按意图选城市字段**：查询类比 `query_city`、规划类比 `destination`、`departure_city` 仅路线框架。`query_city ≠ destination` 绝不判冲突。命中城市与所选字段不符 → ambiguous，0.92 分也不改判 |
| HC-2 | 类型冲突 | restaurant vs attraction → ambiguous |
| HC-3 | 分店/同名歧义 | 归并后仍多实体 → ambiguous，列全量候选 |
| HC-4 | 角色闸 | negated/remove 验证照常，但永不进入新增行程路径（§5） |
| HC-5 | 写入权限闸 | destination 只许规划类意图写；LLM 无权写 |
| HC-6 | 探测证据闸 | rejected 必须满足 §1.2 充分证据（含双源前置检查），否则最多保守处理 |
| HC-7 | 归并闸 | 无充分证据不得凭名称相似合并记录（§4） |

分数只做同档内排序（如消歧候选展示顺序），永不跨档救回被硬约束拦下
的记录。

**confidence 计算边界（v3.2.1 冻结）：**
- 参与评分的证据：匹配方式基线分（KB 全名 0.92 / 部分匹配 0.82 /
  在线 0.80）+ 上下文调整（无城市 −0.12 / 类型不符 −0.25），沿用
  第四阶段策略，**第五阶段不重设计评分模型**；
- 同档排序依据：分数 → 匹配方式 → 名称长度稳定性；
- 无足够评分依据时取档内保守值（默认 0.0，不臆造中间分）；
- 分数仅用于档内排序与展示，绝不参与定档与跨档。

## 4. 实体归并先于唯一性

同一 POI 的名称、别名、分店记录、多数据源记录不能直接算成多个实体。

归并证据优先级（高→低）：
1. 稳定 POI ID / 实体 ID
2. 地址证据（同城市 + 同地址/门牌）
3. 坐标邻近（同城市且距离 ≤ 阈值）
4. 品牌名 + 分店后缀解析

证据不足时**保留歧义**（ambiguous），绝不仅凭名称相似度合并不同
分店；归并产生的合并/保留决策全部写入 notes 供影子对比归因。

## 5. negated 与 remove 分离

| 角色 | 行为归属 | 决策层行为 |
|---|---|---|
| negated | 交回**现有排除逻辑**（slots.avoid / PlanHandler 排除链路） | 不允许进入新增行程路径 |
| remove | 保留**现有 PlanHandler 删除链路** | 不经 DecisionEngine 做任何删除动作 |
| 两者 | — | 都不能被 DecisionEngine 当作 must_visit（HC-4） |

DEP-001 只负责未来复杂行程依赖联动（如删景点联动删除其分店餐位），
**不得成为现有删除功能的前置条件**。

独立行为断言（语料 v5 必含）：
- 「去掉苍山」：走删除链路，方案中苍山被移除，**不**触发新增/消歧；
- 「不想去总统府」：走排除链路，新方案不再排入总统府，**不**触发
  删除报错，也**不**被当作 must_visit。

## 6. 候选来源与证据来源契约（v3 拆分）

### 6.1 两个正交字段

| 字段 | 枚举 | 含义 |
|---|---|---|
| **candidate_source** | `user` / `llm` | 候选**从哪来**：用户原始文本抽取，或 LLM 结构化输出生成 |
| **evidence_sources** | `kb` / `online`（可多值） | 实体验证**用什么证据**：KB 命中，或在线探测命中 |

两个字段独立取值：LLM 候选（candidate_source=llm）验证后
evidence_sources 可以是 kb 或 online；用户候选同样。**绝不复用一个
source 字段同时表达两种语义。**

### 6.2 Span 规则（v3 收紧）

- span_start / span_end：候选在**原始用户输入**中的真实位置；
  不存在时为 **null**。
- LLM 候选**只有与原文精确匹配时**才能获得真实 Span；模型改写、
  补全、推断的内容（不在原文中）一律 **null，绝不伪造**。
- CandidateExtractor 只能为原始用户文本提供 Span，不为模型生成文本
  提供 Span。

### 6.3 LLMCandidateAdapter（独立适配器）

- 职责单一：LLM JSON 输出 → 候选对象（text / entity_type /
  candidate_source=llm / span=null 或真实值），不与
  CandidateExtractor 耦合。
- 触发条件见 §2（三条同时满足；不满足静默跳过走保守处理）。
- LLM 建议的候选**不能直接成为 must_visit**：必须通过 EntityResolver
  与 DecisionEngine 全链。

### 6.4 LLM 六条红线（保留）

1. LLM 可以辅助理解用户表达，不能直接确认 POI 存在。
2. LLM 输出的候选一律经 EntityResolver 验证，绝不直通业务。
3. LLM 不能直接修改 slots.destination。
4. LLM 不能直接执行 plan_trip。
5. LLM 不能直接删除、移动或新增真实行程节点。
6. 用户消歧前，绝不静默挑选分店。

降级：无 Key / 超时 / 输出不可解析 → 静默跳过走保守处理，绝不报错
打断主链，绝不编造。

## 7. 多轮消歧状态契约 PendingDisambiguation（v3 扩展）

```python
@dataclass
class PendingDisambiguation:
    pending_id: str            # 唯一 ID
    original_utterance: str    # 触发消歧的原始句子
    original_intent: str       # 当时的细分意图
    original_role: str         # 候选当时的角色
    candidates: list[dict]     # 候选快照（字段见下，冻结不再变）
    created_at: str            # ISO 时间
    expires_at: str            # 过期时间
    status: str                # open / consumed / cancelled / expired
```

**候选快照逐条字段（v3 新增）：**

| 字段 | 说明 |
|---|---|
| candidate_id | 快照内唯一 ID（用户选择引用的凭据） |
| display_order | 展示序号（1,2,3…），**冻结** |
| entity_id | 归并后的实体 ID（可空：未入库实体为 null） |
| name | 实体名 |
| city | 归属城市 |
| address | 地址（可空） |

消费规则（九条，逐条进测试）：
1. 用户按序号选择时，只能引用当前 pending 的**候选快照**；候选 ID 与
   展示顺序**冻结**，会话期间 KB 变化不影响快照。
2. 用户取消 → status=cancelled，pending 清除。
3. 用户提出新问题（与 pending 无关的意图）→ 不得误消费旧 pending。
4. 用户修改目的地 → 候选的城市归属已变化，重新走 EntityResolver，
   旧快照作废。
5. pending 过期 → status=expired，不得继续执行，如实告知需重新提问。
6. **同一个 pending 只能成功消费一次**；状态迁移**原子化**
   （open→consumed 单次迁移、加锁/幂等），重复提交不产生第二次执行。
7. 用户选择分店只完成消歧（ambiguous→confirmed），不等于授权执行
   行程修改——行程动作仍走对应 Handler 的既有受控链路。
8. **同一 Session 同时最多一个可消费的 active pending**；若未来支持
   多个，必须通过 pending_id 显式引用，绝不默认消费「最近一个」。
9. 选择引用的 candidate_id 必须属于原快照；快照外的 ID 一律拒绝。

**存储接口（v3.2.1 补充，P2 实现）：**`create_pending` /
`get_active_pending` / `consume_pending` / `cancel_pending` /
`expire_pending`。其中 `consume_pending` 的 open→consumed 迁移
**必须原子**：不得使用「先读 status 判断再写」两步式（并发请求会
同时通过判断），须用数据库条件更新（`UPDATE ... WHERE
status='open'` 检查受影响行数）或等效事务/锁机制，保证只能成功
一次。

## 8. 枚举正交与组合合法性

```text
status（业务分流，五值）:
  confirmed / ambiguous / rejected / unverified / skipped

probe_status（聚合探测结论，五值）:
  not_applicable / not_requested / success_hit / not_found / unavailable

probe_reason（六值起步）:
  network_timeout / no_key / circuit_open / no_city_center /
  quota_exceeded / disabled_by_policy
  （+ data_source_empty / cross_source_empty，仅配 not_found）

probe_results（v3 新增，逐源证据明细）:
  [{source, outcome: success_hit|success_empty|failed|skipped, reason}]
```

合法组合表（组合测试判定依据）：

| status | 合法 probe_status | 约束 |
|---|---|---|
| confirmed | not_applicable / success_hit | 唯一实体 + 硬约束全过 |
| ambiguous | not_applicable / success_hit | 多实体或硬约束命中 |
| rejected | not_found | **必须 reason=cross_source_empty，且 probe_results 显示 ≥2 个适用且独立的源均 success_empty**（双源前置检查通过） |
| unverified | not_requested / unavailable / not_found | not_found 单源（data_source_empty）或 unavailable 必带 reason |
| skipped | not_applicable | 品类/城市/空候选 |

非法组合示例（测试必须拦截）：(rejected, unavailable)、(rejected,
not_found + data_source_empty)、(rejected, 双源任一 failed/skipped)、
(skipped, success_hit)、(unavailable 无 probe_reason)。

**probe_reason 空值规则（v3.2.1 冻结，组合校验判定依据）：**

| probe_status | probe_reason 允许值 |
|---|---|
| not_applicable | None（必须为空） |
| not_requested | disabled_by_policy（固定） |
| success_hit | None（必须为空） |
| not_found（单源） | data_source_empty（固定） |
| not_found（双源） | cross_source_empty（固定） |
| unavailable | 必须为具体失败原因（network_timeout / no_key / circuit_open / no_city_center / quota_exceeded 之一），不得为空 |

**probe_results 内部一致性不变量（v3.2.1 新增，Resolver 级测试）：**

1. 聚合 probe_status 必须能从 probe_results 逐源明细推导重现；
2. 任一源 outcome=failed → 聚合不得为 not_found（**主源
   success_empty + 复核源 failed → unverified**，绝不能因两个源都
   没返回实体就当成双源空结果——此场景同时做成 Resolver 内部
   不变量测试，不只停留在语料层）；
3. skipped 的源不计入双源复核计数；
4. outcome 与 reason 一一对应（failed/skipped 必带 reason，
   success_hit/success_empty 的 reason 为空）。

## 9. 分批接管与 P3 边界（v3 修订）

### 9.1 P3 接管边界（v3：按「是否存在具体实体候选」判定，不按意图名）

**进入 DecisionEngine 的唯一条件：本轮解析产出了具体实体候选
（点名景点、点名餐厅、点名分店）。**

| 意图/表达 | 处理 |
|---|---|
| 点名景点、点名餐厅、点名分店 | 进入 DecisionEngine |
| poi_query | **必须检查存在具体实体候选才接管**；仅凭意图名不接管（无具体候选 → 走泛化路径） |
| category_query（泛化品类搜索） | **不自动进入具体 POI 实体验证**；继续走既有 Handler/DataService |
| 泛化景点搜索 | 继续走 Handler/DataService |
| 特色美食问答 | 继续走 **FoodHandler** |
| 餐厅品类搜索 | 继续走 **RestaurantHandler** |
| 泛化酒店推荐 | 继续走 **HotelHandler/DataService**（实体存在性不能代替酒店房态或价格查询） |
| nearby_food_query | **只验证用户指定的周边锚点**；搜索返回的餐厅不是用户原始指定实体，不作为候选进入实体验证（如需引用，candidate_source 不得标 user） |

### 9.2 批次

| 批次 | 范围 | 说明 |
|---|---|---|
| 5.0 影子决策 | DecisionEngine 全量影子运行（shadow_decision 纯记录） | 纯 KB-only |
| 5.1 查询第一批 | 具体实体候选的 poi_query / poi_location_query（只读） | 按 §9.1 边界判定 |
| 5.2 查询第二批 | nearby_food_query / restaurant_search / hotel_search 的**点名实体部分** | 泛化部分仍走 Handler/DataService |
| 5.3 规划类 | plan_modify 的点名/排除槽位 | 消歧通道上线后 |

### 9.3 准入门槛

- 以 **corpus 预期结果和硬约束负向断言**为准，不是新旧一致率。
- 新旧差异逐条三分类：新系统修复旧链路缺陷 / 合理的决策差异 /
  新系统回归（阻断接管）。
- **高风险错误必须为零**：错误城市确认、否定对象新增、静默选择分店、
  rejected/unverified 混淆。
- 回退：`TRAVEL_DECISION_TAKEOVER=0` 立即回旧链路。

### 9.4 Orchestrator 接入点（v3.1 补录）

orchestrator._turn_impl 现有流程不动，只在**三个固定点位**插入
DecisionEngine 调用，全部由开关控制。

**接入点 A 的固定执行顺序（v3.2.1 冻结，决策链严格在
EntityResolver 之后）：**

```
_turn_impl:
  # 1. 解析用户表达（既有调用链，复用不重写）
  ctx = parse_utterance(...)
  # 2. 提取候选（既有 CandidateExtractor，影子链路）
  candidates = candidate_extractor.extract(ctx.parse)
  # 3. 必要时补充 LLM 候选——默认关闭，P4 才真正启用
  candidates = fallback_if_needed(ctx.parse, candidates, context)
  # 4. 实体验证（复用既有 Resolver 调用链，不重复解析或验证）
  resolved = entity_resolver.resolve(candidates, context)
  # 5. 决策（接入点 A）
  decision = decision_engine.decide(ctx.parse, resolved, context)
       TAKEOVER=0（默认）→ 仅记录 shadow_decision，返回 None
       TAKEOVER=1 且本意图批次已启用 → 返回 DecisionResult
  # 6. 交给既有业务路由（原 if/elif 一字不改）
```

注意：上表是数据流示意；实际开发**必须复用现有 Resolver 调用链**
（parser 影子通道中的 shadow_resolved），不得为接入 DecisionEngine
而重复解析或验证。

**决策结果消费（原 if/elif 分支入口处）：**
- 每个分支入口先看接入点 A 是否给出 DecisionResult：
  给了且 action=execute → 走该 Handler 受控链路；clarify → 进消歧
  通道（P2 后启用）；reject_reply → 按措辞约束回复不执行；
  conservative → 保守话术不执行；category_path / deferred → 交回
  原分支照旧；没给（None）→ 原分支逻辑一字不改。

**接入点 B（PlanHandler 写入前）**：HC-5 复核（destination 写权限）
——即使后续链路出现 destination 也不能绕过。

**接入点 C（回复组装后）**：只记录 DecisionResult.reason 作影子
归因；**reason 不决定回复文案**。

原则：**接入点只增不改**——旧分支在 DecisionResult 为 None 时行为
与现状完全一致；任何批次接管都通过「开关 + 批次白名单」双条件放行，
回退开关一关即全部恢复旧路径。

### 9.4.1 语料与开关矩阵

| 开关组合 | 行为 |
|---|---|
| `TRAVEL_RESOLVER_ONLINE=0`（默认）+ `TRAVEL_LLM_FALLBACK=0`（默认）+ `TRAVEL_DECISION_TAKEOVER=0`（默认） | 纯影子：候选识别、实体验证、决策全部只记录 |
| RESOLVER_ONLINE=1 | 影子追加在线探测（有缓存与限额） |
| LLM_FALLBACK=1 | 兜底可实际调用（P4 后） |
| DECISION_TAKEOVER=1 + 批次白名单 | 对应意图消费 DecisionResult |

## 10. 实施顺序

```
P0   探测元数据与限额   正交枚举落地（含逐源 probe_results）、调用限额、
│                       单源空结果降级 unverified
P1   DecisionEngine     decision.py：硬约束→归并唯一性→置信度→分流；
│                       影子决策接入 shadow_decision
P1.5 契约补全           PendingDisambiguation 契约 + LLMCandidateAdapter
│                       契约：仅契约定义与测试桩，不上线不接业务
P2   消歧通道           PendingDisambiguation 实现 + 多轮消费规则（含原子化）
P3   查询类接管         按 §9.1 边界：仅具体实体候选；泛化问答/品类/酒店不动
P4   LLM 兜底           **P3 稳定之后**（不并行上线）；适配器接真 LLM，
│                       产物回流统一候选集合，六红线测试
P5   规划类消费         5.3 批次
```

每步独立 commit；回归门槛：v1–v4 语料 + 全套件绿 + 当步新语料绿。

### 10.1 长期路线（v3.1 补录，开发指令定版）

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 回归语料库 | ✅ `842170b` |
| Phase 2 | 统一 Parser + 城市字段拆分 | ✅ `b3a2547` |
| Phase 3 | 五层候选识别 | ✅ `80337bd` |
| Phase 4 | EntityResolver + 校验 | ✅ `2f66f4e`（验收通过） |
| Phase 5 | Decision + LLM 兜底 | ← 本文档 |
| Phase 6 | 行程依赖一致性引擎（DEP-001：删除豫园联动南翔馒头店；强依赖删除/弱依赖重选/独立保留） | 待设计 |
| Phase 7 | POI/酒店/餐厅问答能力完善 | 待设计 |
| Phase 8 | 性能、缓存、工具调用优化 | 待设计 |

DEP-001 在第五阶段**完全不碰**：它属于「实体解析 → 行程实体依赖
关系 → 删除传播 → 重新校验」的独立系统，留待 Phase 6 单独设计；
现有删除/排除功能不以其为前置（§5）。

## 11. 测试用例清单

### T-A rejected/unverified 不混淆
单源空结果→unverified(data_source_empty)；双源复核均空→rejected
(cross_source_empty)；主源空+复核源 unavailable→unverified；rejected
用户措辞不含「不存在」；unavailable 必带 reason。

### T-B negated/remove 分离
「去掉苍山」走删除链路（方案移除、无新增无消歧无 must_visit）；
「不想去总统府」走排除链路（不再排入、无删除报错、role≠must_visit）；
「不要安排夫子庙」DecisionEngine 输出不含 must_visit；角色×状态矩阵
全格不进行程。

### T-C LLM 候选契约
LLM 生成新候选 candidate_source=llm、span=null；LLM 输出经
EntityResolver 验证后才出现在分流结果；直接 must_visit 建议被拒；
适配器脏输入降级不报错；六红线 mock（destination 不可写 / 不执行
plan_trip / 不动行程节点 / 不静默选分店）。

### T-D PendingDisambiguation
快照引用（KB 变更后按序号选择仍命中快照）；取消清除；新问题不误
消费；改目的地重验证；过期不可执行；一次性消费（原子化）；选分店
≠授权修改；单 Session 单 active pending；快照外 candidate_id 拒绝。

### T-E 枚举组合
§8 合法组合表逐格断言；非法组合拦截（含 rejected 双源任一失败）。

### T-F 城市字段按意图
查询类 query_city≠destination 不判冲突；规划类 destination 跨城命中
→ambiguous；departure_city 仅路线框架。

### T-G 实体归并
同 POI 多记录（ID/地址/坐标证据）归并后唯一→confirmed；名称相似无
证据→保留 ambiguous；品牌+分店后缀归并为品牌与分店关系。

### T-H 接管门槛
新旧差异三分类逐条可归因；高风险四项零容忍一票否断言。

### T-I v3 新增（9 条）

| 用例 | 断言 |
|---|---|
| LLM Span 真实性 | 原文精确匹配→真实 Span；改写/补全/推断→null，绝不伪造 |
| 候选来源与证据来源正交 | candidate_source(user/llm) × evidence_sources(kb/online) 独立取值、独立断言 |
| 双源一成功一失败 | 一源 success_empty + 一源 failed → unverified，绝不 rejected |
| 复核源不适用 | 不适用源记 skipped 不强制执行、不计入复核；证据不足保持 unverified |
| pending 快照顺序稳定 | KB/候选集变更后 display_order 与 candidate_id 不变 |
| pending 一次性消费 | 原子化：重复提交第二次无效，不重复执行 |
| category_query 不进实体验证 | 泛化品类查询不产生实体验证记录，走既有 Handler |
| nearby_food_query 只验证锚点 | 仅锚点进入验证；搜索返回餐厅不作 user 候选 |
| query_city 与 destination 不冲突 | 查询类两城不同不判 HC-1 冲突 |

### T-J 重点病例 15 条（v3.1 补录，开发指令点名，语料 v5 必含）

| # | 病例 | 预期要点 |
|---|---|---|
| ① | 排上海贝皇大排档 | 城市前缀并入实体；EntityResolver 按上海验证 |
| ② | 排海贝皇大排档 | 多分店 → ambiguous 执行阻断，澄清不排入 |
| ③ | 介绍国民政府中央广播电台旧址 | 长名 L1/L2 命中 → confirmed → POI QA，不掉泛推荐 |
| ④ | 宽窄巷子是什么地方 | 点名景点 confirmed → 查询应答 |
| ⑤ | 成都有哪些好吃的火锅店 | **category_query 不进实体验证**，走品类路径（skipped/品类 Handler） |
| ⑥ | 上海有哪些好吃的 | 泛化美食走 FoodHandler；**query_city=上海 且 destination=成都 两字段互不污染**（防线断言） |
| ⑦ | 南京有哪些酒店 | 泛化酒店走 HotelHandler/DataService，不进实体验证 |
| ⑧ | 南京总统府附近有什么好吃的 | 锚点=总统府验证；返回餐厅不作 user 候选 |
| ⑨ | 这个地方介绍一下 | 无具体实体候选 → 不接管，走澄清/上下文 |
| ⑩ | 那家店怎么样 | 同上，规则无候选且 LLM 不触发时保守处理 |
| ⑪ | 海贝皇大排档是哪家 | 指代消歧 → 引用 active pending 快照或澄清 |
| ⑫ | 南京的海贝皇大排档 | 城市限定 → 归并后唯一则 confirmed |
| ⑬ | 上海的海贝皇大排档 | 同上按上海验证；跨城同名不静默合并 |
| ⑭ | 查不到的餐厅 | 双源复核均空 → rejected（措辞「规定范围内没有找到」） |
| ⑮ | 网络不可用时查询餐厅 | probe unavailable → unverified + reason=network_timeout，**绝不 rejected** |

其中 ⑥ 是本阶段最重要防线之一：三城市字段拆分后任何路由都不得把
query_city 写进 destination。⑭⑮ 是 T-A 的真实语料落地形式。

### T-K 验收语料 v5 全表（v3.1 补录；v3.2 升级为状态机断言；v3.2.1 冻结字段）

语料文件 tests/corpus_v5.tsv，**TSV 列格式 v3.2.1 冻结为 12 列**：

```text
id │ utterance │ context │ kb │ probe_spec │ expect_resolved │
expect_status │ expect_action │ expect_turn │ expect_permissions │
expect_diff_class │ note
```

- `expect_status` 是**测试预期字段**，仅用于断言校验——不意味着把
  status 塞回 DecisionResult（DecisionResult 契约不变）；
- `expect_permissions`：越权/错误执行检查（如 destination 未被改写、
  未静默选分店）；
- `expect_diff_class`：新旧链路差异三分类（fix_old / by_design /
  regression）。

KB 与探测全部注入、纯离线运行。**断言要求**：

- **必测正向**：confirmed → execute、ambiguous → clarify、
  rejected → reject_reply、unverified → conservative、
  skipped → category_path（映射表 §2.3.1 逐格）；
- **必测负向**：unverified ≠ rejected、ambiguous ≠ rejected、
  confirmed ≠ unverified、`entity status ≠ skipped`、
  `任何状态 ≠ must_visit`；
- 每条断言四元组：`expect_action / expect_status / expect_turn(单轮或
  跨轮) / 权限检查（是否越权、是否错误执行）`。

以下为编号 16–30（补全 30 条）：

| # | 病例 | 预期要点 | 对应测试组 |
|---|---|---|---|
| 16 | 不想去总统府 | negated 走排除链路；role≠must_visit；验证照常 | T-B |
| 17 | 去掉苍山 | remove 走删除链路；无新增无消歧 | T-B |
| 18 | 从大理到丽江玩5天 | 城市走路线框架；无垃圾候选（对比第三阶段修复） | T-F |
| 19 | 记在南京不在上海（destination=上海 上下文） | 跨城归属修正，notes 如实标注 | T-F |
| 20 | 帮我查一下豫园商城 | 部分匹配→归并证据后 confirmed 或如实 ambiguous | T-G |
| 21 | 介绍一下南京的总统府 | query_city=南京 且实体同城 → confirmed | T-F |
| 22 | 上海的总统府（KB 仅南京总统府） | HC-1 城市≠所选字段 → ambiguous，0.92 分不救回 | T-F/T-H |
| 23 | 我想去南京那家海贝皇 | 城市+品牌引用消费 active pending（快照内 ID） | T-D |
| 24 | 算了不去海贝皇了 | pending → cancelled，清除不执行 | T-D |
| 25 | （过期后）选第2家 | pending expired → 拒绝执行，如实告知 | T-D |
| 26 | 南京天气怎么样 | 无 POI 候选且意图不需要实体 → **decide 返回 None（未接管），不是 skipped** | §2.3.1 deferred 双语义 |
| 27 | 帮我排两天的行程 | 无实体候选，规划框架意图 → deferred 或 None；**不判 skipped** | §2.3.1 / §2.5 |
| 28 | 附近有什么好吃的 | 品类词泛化请求 → **skipped → category_path**（skipped 入口的合法形态） | §2.5 |
| 29 | （LLM mock）南京那个民国时候的广播台旧址介绍一下 | 规则候选不全 → LLM 兜底产 candidate_source=llm、span=null → Resolver 验证 → confirmed → POI QA | T-C/T-I |
| 30 | （LLM mock 输出含 destination 字段） | 越权字段丢弃 + notes 记录；destination 不被改写 | T-C 六红线 |

断言规则：30 条逐条含 **新旧链路差异三分类标注**（修复旧缺陷 /
合理决策差异 / 新系统回归），后者阻断接管；高风险四项零容忍断言
（§9.3）嵌入语料检查。

### T-L 跨轮状态机（v3.2 新增）

§2.5 跨轮验收用例 Case A/B/C 逐轮断言：第一轮 expect_action /
expect_status，第二轮 expect_action（含 pending 消费原子性、补充信息
重验证、rejected 不复活）；加一轮负向：Case A 第二轮说「上海那家」
（与快照城市不符）→ 不消费，重新 ambiguous 并提示。

## 12. 明确不做（阶段边界）

- 不修 DEP-001（且不是现有删除/排除功能的前置条件）。
- 不改 Composer、路线优化、预算计算。
- 不做在线写库（probe-then-store 仍是旧链路 ensure_spot 职责，不扩大）。
- LLM 不做存在性判断、不做行程写操作、不允许旁路（§6.4 红线即边界）。
- 不一次性接管全部意图（严格按 §9 批次与 §9.1 边界）；泛化美食/品类/
  酒店问答不动。

## 13. 设计变更记录

| 日期 | 版本 | 变更 | 原因 |
|---|---|---|---|
| 2026-11-18 | v1（e0438ab） | 初稿 | 第五阶段设计评审 |
| 2026-11-18 | v2 | rejected 双源复核、negated/remove 分离、source 契约、PendingDisambiguation、枚举正交、城市字段按意图、实体归并、接管门槛（详见 §0.1） | 第一轮评审 5 问题 + 3 要求 |
| 2026-11-18 | v3（本文） | §2 数据流修正：LLMCandidateAdapter 与 CandidateExtractor 并列汇入统一候选集合，废弃「挂 EntityResolver 之后」的矛盾画法；LLM 触发三条件成文 | LLM 候选必须回流验证与架构图位置矛盾（评审 1） |
| 2026-11-18 | v3 | §6 拆分 candidate_source（user/llm）与 evidence_sources（kb/online）为正交字段；Span 仅原文精确匹配才真实 | 单一 source 字段承担两种语义，LLM 生成文本无真实 Span（评审 2） |
| 2026-11-18 | v3 | §9.1 P3 边界改为「按具体实体候选判定」+ 完整路由表：category_query 不进验证、poi_query 检查候选存在、nearby_food_query 只验锚点、美食/品类/酒店问答归各自 Handler/DataService | 泛化搜索被误拉进实体验证（评审 3） |
| 2026-11-18 | v3 | §7 快照逐条字段（candidate_id/display_order/entity_id/name/city/address）；单 active pending；ID 与顺序冻结；状态消费原子化 | 防重复提交、防顺序漂移、防跨 pending 误引用（评审 4） |
| 2026-11-18 | v3 | §1.2 rejected 语义收紧为「规定数据源与检索范围内没有找到」；双源复核增加独立性+类型适用性前置检查；逐源 probe_results 明细；不适用源不强制执行 | 双源都未收录 ≠ 现实不存在（评审 5） |
| 2026-11-18 | v3 | §11 新增 T-I 测试组（9 条） | 覆盖本轮全部修订（评审 6） |
| 2026-11-18 | v3.1（本文） | §2.1 五档运行时路由表（confirmed 直接执行不掉泛推荐、ambiguous 执行阻断、rejected/unverified 分行为、skipped 品类路径） | 开发指令要求五档成为真正的运行时状态 |
| 2026-11-18 | v3.1 | §2.2 模块职责表（decision.py / llm_entity_fallback.py 命名对齐）、§2.3 DecisionResult 契约（action/intent/entity/city/confidence/reason/pending），orchestrator 只消费 DecisionResult | 开发指令：不再在 _turn_impl 里堆 if/elif |
| 2026-11-18 | v3.1 | §11 新增 T-J 重点病例 15 条（含 ⑥ query_city/destination 防线、⑭⑮ rejected/unverified 真实语料） | 开发指令点名的重点测试病例 |
| 2026-11-18 | v3.2（冻结版） | **五档/六档分离**：§2.3 冻结原则明确 status=Engine 内部实体决策状态、action=Orchestrator 运行时路由动作；DecisionResult 无 status/route/candidates；新增 city_source 字段（归因） | 评审：confirmed 与 execute 混用会导致状态机与路由混乱 |
| 2026-11-18 | v3.2 | §2.3.1 五档→六档固定映射表 + deferred 双语义（None=未接管 / deferred=已决策不接管，不混） | 评审：灰度迁移需要区分两种「不接管」 |
| 2026-11-18 | v3.2 | §2.4 LLM 不参与五档判断写死；越权必测字段（plan/must_visit/destination）；触发判据删除 OTHER/低置信度，唯一 should_fallback 公式 | 评审：LLM 权限收紧到只补候选 |
| 2026-11-18 | v3.2 | §2.5 skipped 入口收紧（没有实体≠skipped）+ 跨轮验收用例 Case A/B/C；§11 T-K 升级为状态机断言（四元组+必测正负向）；§11 新增 T-L 跨轮状态机组；§2.7 收敛总图 | 评审：验收从分类题升级为状态机题 |
| 2026-11-18 | v3.2.1（开发基线） | §2.3.1 deferred 返回值判定表（deferred 不是第六种实体状态，不进五档状态机）；§2.6/§2.4 LLM 职责收缩为实体文本补全+类型辅助，角色不确定不再是触发条件；§9.4 接入点 A 固定执行顺序（决策链在 Resolver 之后，复用既有调用链）；§8 probe_reason 空值规则 + probe_results 内部一致性不变量（主源空+复核失败→unverified 作 Resolver 级不变量测试）；§11 T-K TSV 冻结 12 列（expect_status 是测试预期，不回塞 DecisionResult）；§7 消歧存储接口五方法+原子消费实现要求；§3 confidence 计算边界冻结 | 开发前最终评审 5 个必改问题 + 2 个落实事项，防止开发时自行猜测 |
| 2026-11-18 | v3.1 | §10.1 长期路线 Phase 6–8 定版；DEP-001 留待 Phase 6，第五阶段完全不碰 | 开发指令的路线定版要求 |
| 2026-11-18 | v3.1 | §2.4 LLM fallback schema（白名单五键、越权字段丢弃、适配器校验、city_hints 不写入）；§2.5 五档状态转换表（轮内转换 + 跨轮迁移 + 禁止转换）；§2.6 触发条件可执行判据（三条件 + 成本闸，默认关）；§9.4 Orchestrator 三接入点只增不改 + 开关矩阵；§11 T-K 语料 v5 全表 30 条（16–30 覆盖 T-A~T-I，含三分类标注） | 「六项怎么设计」评审问答落地：一次定死 DecisionResult / schema / 触发条件 / 状态转换 / 接入点 / 验收语料 |
