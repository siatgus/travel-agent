# 第五阶段 P2.5 开工申请与设计方案：消歧运行时验证（八步完整链路 + 重验证四分支）

> 申请日期：2026-11-19。设计基线：`f914072`（P2.4b 关闭归档）。
> **版本：r2（2026-11-19 评审 CONDITIONAL PASS 修订定版，§10 为
> 裁决定版条款，与 §0–§9 冲突时以 §10 为准）。**
> 冻结基线链：P2.1 `589ebe4` / P2.2 `67843fc` / P2.3 `79b1c04` /
> P2.4a `3fb910b`+R1 `5966d02` / P2.4b `1d051fd`。
> **本文档为开工申请，未经批准不写任何业务代码。**
> 设计依据：PHASE5_P2_PLAN.md §5（重验证失败契约）、§7 实施顺序表
> （P2.5 = 消歧运行时验证：八步完整链路、重验证失败四分支）；
> PHASE5_DESIGN.md v3.2.1 §7（九条消费规则）、§10 P2 前置风险项③
> （八步核心验收场景）、§9.4 接入点 A 消费路径。

---

## 0. 申请范围（一件事）

**把 P2 已建成的「消费半环」补成「完整闭环」**：用户从 ambiguous
被阻断开始，到创建 Pending、展示快照、选择、原子消费、**重验证**，
最终按重验证结果安全流转——全程可观测、可回退、旧链路零破坏。

P2.4b 已就位（消费侧）：选择识别 → evaluate/act → 原子消费 →
九类 verdict 接线。P2.5 只补两段缺口：

1. **生产侧**（八步链路的第 1~2 步）：ambiguous 执行阻断 →
   创建 Pending → 展示快照；
2. **重验证侧**（八步链路的第 7~8 步）：消费成功 ≠ 重验证成功
   ≠ Handler 已执行——消费成功后全新走 `entity_resolver.resolve()`，
   按四分支契约流转。

**不做**：P2.6 影子观察（单独申请）、P3 查询类接管批次扩容、
LLM 兜底（P4）、规划类消费（P5）、DEP-001、灰度自动开启。
P2.5 结束后决策层执行类接管仍默认纯影子（TAKEOVER=0）。

## 1. 冻结依据（原文重申，逐条落点进测试）

### 1.1 八步核心验收场景（PHASE5_DESIGN §10 风险项③）

> ambiguous→创建 Pending→展示快照→用户选择→快照 ID 校验→
> 原子消费→重验证 confirmed→交原 Handler；二次选择/过期选择/
> 快照外选择一律拒绝；**消歧成功 ≠ 授权修改行程**（身份确定，
> 行程动作仍由原有权限与角色规则决定）。

### 1.2 重验证失败四分支契约（P2 方案 §5，评审阻断项②闭合条款）

> **原子消费成功只代表用户选择已被接受，不代表实体重新验证成功，
> 更不代表 Handler 已经执行。**
> 重验证 = **全新走 `entity_resolver.resolve()` 决策链**（KB 索引
> 重建 + 探测按当时开关执行），**不复用**旧 Pending 的 matches 或
> 旧 DecisionResult。允许命中 Resolver 既有进程级探测缓存。

| 重验证结果 | 后续处理 |
|---|---|
| confirmed | 允许按 original_intent 进入对应流程（查询类直进 Handler；规划类只回确认话术不写行程） |
| ambiguous | **不执行 Handler**；由 fresh matches 生成**新候选快照**并创建**新 Pending**（新 pending_id、新 TTL）重新请求选择 |
| unverified | 不执行 Handler；保守回复（「暂时查不到可靠信息，可告知城市或地址」），**绝不判定为 rejected** |
| rejected | 不执行 Handler；按「规定数据源与检索范围内没有找到」话术回复，保留原始表达 |

> 重验证失败分支契约（原 Pending 保持 consumed，**绝不静默
> 恢复 open**；任何后续 Pending 用**新 pending_id + 新快照**）。
> 归因：所有失败分支保留原 pending_id、candidate_id 与重验证结果
> （pending_consumption 的 revalidate_status / revalidate_probe_status
> 列回填；shadow_log 同步记录；reason 携带全链归因）。

### 1.3 city_hints 注入边界（PHASE5_DESIGN §10 风险项②，P2.5 必须继承）

重验证上下文的城市来源执行 HC-1 三级区分：用户明确 query_city
（可参与判定）＞ LLM city_hints（仅辅助线索，不得升级为用户明确
指定）＞ destination（规划目的地，LLM 提示绝不覆盖）。

## 2. 现状盘点（P2.4b 后的真实缺口）

| 八步链路 | 现状 | 缺口 |
|---|---|---|
| ① ambiguous 执行阻断 | DecisionEngine 已产出 clarify（影子） | 影子结果未进入业务流（TAKEOVER=0 时 decide 返回 None） |
| ② 创建 Pending | store.create_pending 双实现已验收 | **无任何调用方**（生产者冻结至今） |
| ③ 展示快照 | restate 接线已有 1..N 渲染逻辑（装配层） | 生产侧首轮展示话术缺位 |
| ④ 用户选择 | P2.3 evaluate 选择线索判定已验收 | — |
| ⑤ 快照 ID 校验 | create 时校验 + 消费事务内候选校验（P2.2） | — |
| ⑥ 原子消费 | P2.2/P2.4b 条件更新 + 单事务已验收 | — |
| ⑦ 重验证 | **完全缺位**（revalidate 三列建好未回填） | 本阶段核心 |
| ⑧ 交原 Handler | **缺位**（P2.4b consume ok 一律短路确认文案） | 本阶段核心 |

另外：内存桩消费记录为七字段（P2.3 冻结），SQLite 表已含
revalidate_status / revalidate_probe_status / revalidated_at 三列
（P2.2 冻结建表「P2.5 重验证回填」注释）——双实现需同步扩展。

## 3. §A 生产侧设计（ambiguous → 创建 Pending → 展示）

### 3.1 触发与数据流

```
handle 步骤 1.5~2 之间（接入点 A 影子决策已完成，原链路分支之前）:
  decision = decision_engine.decide(...)   # 既有调用，影子/返回行为见 3.2
  if 生产开关允许 and decision 是 clarify:
      candidates = pending_producer.build_candidates(
                       decision 关联的 ambiguous fresh matches)
      p = store.create_pending(utterance, intent, role, candidates)
      reply = pending_producer.clarify_reply(p)   # 展示快照 1..N
      return reply（本轮短路；不进原链路——执行已阻断，原分支
                   只会产出泛化追问，二者不叠加）
```

- **快照数据源**：clarify 对应 ResolvedCandidate 的 **fresh
  matches**（本轮 resolve 刚产出，天然满足「不复用旧 matches」）；
  producer 把 matches 转为六字段 dict（candidate_id 生成、
  display_order 1..N 按 Resolver 既有排序），交
  `store.create_pending`（其内部校验六字段/序号/唯一性——P2.1
  冻结契约，零改动）。
- **HC-4 永不进快照**：negated / remove 角色候选绝不进入候选快照
  （原 Pending 出口不带 must_visit 的既有红线同源延伸）。
- **规则 8 兜底**：已有 active pending 时 create 拒绝（现有异常）；
  producer 捕获后**不覆盖不轮换**，直接交回原链路（保守）。
- **生产者装配为新独立模块** `pending_producer.py`（新文件，不在
  任何冻结禁止清单内）：candidates 构建 + 展示话术，可独立测试。

### 3.2 clarify 结果如何进入业务流（开放决策点 D1，需评审裁决）

**现状**：TAKEOVER=0 时 `decide()` 返回 None 只记影子；DecisionEngine
的 clarify 分支当前 `pending=None`（快照数据源在 ResolvedCandidate）。
生产侧激活必须让 orchestrator 感知「本轮是 clarify」。三个选项：

- **方案甲（推荐）**：`decide()` 增加**仅 clarify 放行**的影子外
  出口——TAKEOVER 仍为 0 时，若本轮结论为 clarify 且生产开关开启，
  返回该 DecisionResult（action=clarify）。理由：①clarify 是
  「请求用户消歧」，不是业务执行，不属于 P3 接管边界（执行类）；
  ②TAKEOVER 的冻结语义（**执行类** DecisionResult 消费门控）保持
  不变——execute/reject_reply/conservative 在 TAKEOVER=0 下仍然
  只记影子；③改动最小（decision.py 一处放行条件 + 注释）。
- **方案乙**：独立开关 `TRAVEL_PENDING_PRODUCE`（默认 0），由
  orchestrator 从 shadow_decision 影子记录读取最近 clarify 构建。
  缺点：影子记录是纯观测数据，用它驱动业务违背「影子不进业务链路」
  的既有原则（P0/P1 多次冻结），且影子记录格式变更会波及业务。
- **方案丙**：不改 decision.py，producer 自建一套 ambiguous 判定
  复刻。缺点：判定逻辑双份、必然漂移，违背单一决策层原则。**不
  推荐，列出仅为完整性。**

若评审选甲，`decision.py` 的修改点进入本阶段允许清单（其余
DecisionEngine 逻辑零触碰）；若选乙，decision.py 保持零修改。

### 3.3 展示话术

- 快照 1..N 列表（与 restate 接线同构：名称 + 城市，序号冻结）；
  首轮展示**不**重复角色/意图解释（沿用既有 clarify 措辞约束）；
- 构造失败 → 不短路、交回原链路（pending 已创建不影响——它等待
  下一轮选择，属规则 8 正常态），shadow 记录。

## 4. §B 重验证设计（消费成功 → fresh resolve → 四分支）

### 4.1 触发点与短路铁律的延续

P2.4b 冻结规则①（消费成功是短路决定性依据）**原样延续**：重验证
插入在 gate 的 consume ok 分支内、确认回复构造**之前**——即：

```
consume ok（原子迁移已完成，不可逆）:
  → 重验证（fresh resolve 单候选）
  → 四分支产出回复/流转（见 4.3）
  → 任何一步异常 → _fallback_confirm_reply 兜底确认短路（B3 契约
     不变：绝不撤销消费、绝不回退原链路）
```

重验证阶段无论发生什么，**消费状态与 hint 同步已按 P2.4b 语义
完成**（consumed + hint 清理）——重验证只决定「之后交给谁」，
绝不反向改变 Pending 状态（**原 Pending 保持 consumed，绝不静默
恢复 open**）。

### 4.2 fresh 重验证的输入边界

- **验证对象**：仅被选中的那一个候选（selected candidate_id 必须
  ∈ 原快照——P2.2 消费事务已校验，重验证不重复信任，再次校验）；
- **上下文城市（HC-1 三级）**：取**快照候选自身的 city** 作为验证
  期望城市锚点（快照冻结数据）。理由：本轮 utterance 若改了目的地，
  P2.3 的 cancel_destination_changed 已先行取消 Pending，根本走
  不到消费；能走到重验证即城市语境一致，用快照城市最保守、不受
  本轮解析副产物污染。LLM city_hints 仅作辅助线索传入（若本轮
  parse 产出且非用户明确 query_city），绝不覆盖快照城市；
- **fresh 判定**：全新调用 `entity_resolver.resolve()`——KB 索引
  重建、探测按当时开关执行（`TRAVEL_RESOLVER_ONLINE` 语义不变），
  允许命中既有进程级探测缓存（P0 既有策略）；**不复用**旧 Pending
  的 matches、不读旧 DecisionResult；
- **probe_status 如实标注**：KB-only 影子模式（在线关）下重验证
  的 probe_status 按实际执行情况记录，绝不把「没探测」伪装成
  「探测过没找到」。

### 4.3 四分支流转（§1.2 冻结表逐行落实）

| fresh 结果 | 回复/流转 | 归因要求 |
|---|---|---|
| **confirmed** | 查询类 original_intent：构造 DecisionResult（action=execute，entity=fresh confirmed，reason 含 pending_id/candidate_id/revalidate 链路归因）走 §4.4 受控消费路径；规划类（plan_*）：只回确认话术，**不写行程**（§1.1 红线：消歧成功 ≠ 授权修改行程） | reason 全链归因 |
| **ambiguous** | 由 fresh matches 生成**新候选快照** → producer 创建**新 Pending**（新 pending_id、新 TTL）→ 展示新快照 1..N 请求重新选择；**不执行 Handler** | 影子记录新旧 pending_id 关联 |
| **unverified** | 不执行 Handler；保守回复「暂时查不到可靠信息，可告知城市或地址」；**绝不判定为 rejected** | revalidate_probe_status 如实回填 |
| **rejected** | 不执行 Handler；按「规定数据源与检索范围内没有找到」话术回复，保留原始表达 | revalidate_probe_status=cross_source_empty 溯源 |

- **新 Pending 创建失败**（ambiguous 分支撞规则 8 等）：不覆盖、
  不轮换、不静默——shadow 警告 + 保守回复（提示稍后重试），原
  Pending 保持 consumed；绝不把失败伪装成 confirmed。
- **重验证自身异常**（resolve 抛出等）：B3 兜底确认短路（消费
  已成功的事实不被撤销），shadow 记录异常 + 归因。

### 4.4 confirmed → 交原 Handler 的受控路径（开放决策点 D2，需评审裁决）

冻结要求「走 §9.4 接入点 A 既有消费路径，Handler 拿到的仍是
DecisionResult，不新增任何 Handler 接口」。但 §9.4 的 DecisionResult
消费门槛是「TAKEOVER=1 + 批次白名单」——而 P2.5 边界要求 TAKEOVER
保持 0。两个选项：

- **方案甲（推荐）**：pending 消费通道的 confirmed execute 走
  **与 TRAVEL_DECISION_TAKEOVER 无关的独立受控路径**——gate 产出
  DecisionResult 后，在**既有 if/elif 分支入口的 DecisionResult
  消费点**注入（该消费点结构 §9.4 已冻结：「每个分支入口先看接入
  点 A 是否给出 DecisionResult」），但放行条件为
  `TRAVEL_PENDING_CHANNEL=1 且来源标记=pending_consume`，不读
  TAKEOVER。理由：①TAKEOVER 的冻结语义是「DecisionEngine 执行类
  接管总闸」，pending 通道已有自己的开关与完整的独立验收链
  （P2.2~P2.4b），二者合并会让两个独立安全机制互相耦合；②回退
  粒度更好——关 channel 即整体回退消歧闭环，TAKEOVER 独立控制
  P3 接管；③符合「消歧成功≠授权修改行程」：规划类根本不进此路径。
  实现落点在 orchestrator 分支入口消费点一处（只增判断条件），
  Handler 零改动。
- **方案乙**：confirmed execute 仍受 TAKEOVER 门控（TAKEOVER=0 时
  只回确认话术不进 Handler）。优点：零新增消费点。缺点：P2.5 闭环
  在默认配置下第 ⑧ 步永远走不通，八步链路验收只能靠开关组合拼出，
  「运行时验证」名不副实。

若评审选甲，需同步冻结：该消费点仅放行 `action=execute 且
intent ∈ 查询类批次`（与 P3 白名单批次语义对齐但独立判定），
规划类 intent 一律确认话术。

### 4.5 重验证结果回填（存储接口扩展，开放决策点 D3）

- **新增 store 方法** `record_revalidation(session_id, pending_id,
  candidate_id, revalidate_status, revalidate_probe_status) -> bool`：
  双实现（memory + SQLite）；SQLite 实现为单条条件 UPDATE
  `pending_consumption SET revalidate_status=?, revalidate_probe_
  status=?, revalidated_at=? WHERE pending_id=? AND session_id=?`
  （消费记录 pending_id 为主键，恰好一行）；回填失败不抛出到
  业务链路（shadow 记录，归因不丢——影子日志兜底）。
- 内存桩消费记录从七字段扩展为**十字段**（+revalidate_status/
  revalidate_probe_status/revalidated_at）——这是对 P2.3 冻结
  「七字段契约」的**追加**（SQLite 建表自 P2.2 起即含三列，冻结
  建表语句本就预留），`_latest_consumption()` 返回字段增加但
  七字段名与语义不变；**需评审确认此契约升级**（类比 P2.2 时间
  契约 INTEGER 升级的先例）。
- duplicate_select 话术仍引用消费记录既有字段，不受影响。

## 5. §C 开关矩阵（P2.5 定版建议）

| 开关 | 默认 | P2.5 语义 |
|---|---|---|
| `TRAVEL_PENDING_CHANNEL` | **0** | 1 = 消歧闭环全链启用（生产 + 消费 + 重验证 + confirmed 受控流转）；0 = 完全旁路（现状逐位保持） |
| `TRAVEL_PENDING_TAKEOVER` | **0** | 语义不变（DecisionEngine 生产者接管预留，本阶段不启用不接线） |
| `TRAVEL_DECISION_TAKEOVER` | **0** | 语义不变（P3 执行类接管总闸，与 pending 通道无关） |
| `TRAVEL_RESOLVER_ONLINE` | 0 | 重验证探测按既有语义执行与标注，不改 |
| 新增开关 | 无（除 D1 选乙时的 TRAVEL_PENDING_PRODUCE） | 不引入第三个接管开关 |

## 6. §D 测试清单（评审定版后逐条进 test_pending_channel / 新文件）

**八步链路正向（channel=1，双 store 实现）**：
- A1 ambiguous → clarify → 创建 Pending → 展示 1..N（快照冻结、
  candidate_id 可引用、HC-4 角色不在快照）；
- A2 展示轮后再发新问题 → cancel_new_question → 原链路（P2.3 既有）；
- A3 用户选择 → 快照 ID 校验 → 原子消费 → **重验证 confirmed** →
  查询类 DecisionResult(execute) 进对应 Handler（route=既有分支，
  reason 含 pending_id/candidate_id）；
- A4 规划类 confirmed → 只回确认话术，**行程零写入**（plan 状态
  深拷贝对比）；
- A5 ambiguous → 新 Pending（新 ID ≠ 旧 ID、新快照来自 fresh
  matches、旧 Pending 保持 consumed 不复活）→ 展示新快照；
- A6 unverified → 保守回复、绝不 rejected 话术、不执行 Handler；
- A7 rejected → 「规定数据源与检索范围内没有找到」、保留原始表达；
  （A5~A7 的 rejected 需双源充分证据注入，非法组合负向拦截沿用
  §8 合法组合表）

**fresh 与缓存边界**：
- B1 mock KB 变化 → 重验证结果跟随变化（不复用旧 matches 的成文
  断言，P2 方案 §7 fresh 定义）；
- B2 探测缓存命中路径与 quota_exceeded/unavailable 路径如实标注。

**生产侧**：
- C1 已有 active pending 时新 clarify → create 拒绝 → 交回原链路
  （不覆盖不轮换）；
- C2 展示话术构造失败 → 不短路、pending 保留、原链路正常；
- C3 channel=0 时 clarify → 纯影子，零 Pending 创建（逐位对比）。

**重验证健壮性（延续 P2.4b 异常边界风格）**：
- D1 重验证抛异常 → 兜底确认短路（消费成功事实不被撤销）；
- D2 record_revalidation 失败 → 不影响回复与状态，shadow 归因；
- D3 重验证期间 store 抛异常 → 保守路径（同 P2.4b 矩阵风格）。

**归因与影子**：
- E1 四分支全部回填 revalidate 三列（双实现对拍：SQLite 真库 +
  内存桩十字段）；
- E2 失败分支影子记录含原 pending_id/candidate_id/重验证结果。

**回归门槛**：现有全套件全绿（pending 60 / sqlite 66 /
interceptor 150 / channel 202 / decision 51 / adapter 32 /
resolver 79 / corpus v4 50 / parser 62 / extractor 36 / dialog 244
/ server 52 / conversations 21）+ channel=0 全量逐位对比 + git diff
单点断言（orchestrator 生产钩子与分支消费点两处以外零改动）。

## 7. §E 执行边界（允许 / 禁止清单，待评审定版）

**允许修改**：
- `pending_producer.py`（新文件：候选构建 + 展示话术）；
- `pending_revalidator.py`（新文件：fresh 重验证装配 + 四分支），
  或并入 producer——二选一请评审定；
- Orchestrator：生产钩子一处 + 分支入口消费点放行条件一处；
- 两个 store：`record_revalidation` 新方法 + 内存桩十字段；
- decision.py：仅当 D1 选甲（clarify 影子外出口一处）；
- 上述行为的专项测试 + 本计划实施记录。

**禁止修改**：
- `PendingInterceptor.evaluate/act` 业务逻辑（九类 verdict 判定
  与执行原样）；
- PendingRuntimeManager（含 R1 异常安全）、lease/淘汰/锁顺序；
- Handler 全部文件；`create_pending/consume_pending` 等既有五方法
  契约；SQLite 建表语句（三列已预留，无 DDL 变更）；
- DecisionEngine 的 execute/reject_reply/conservative/deferred
  分支与 TAKEOVER 门控语义（D1 选甲也仅动 clarify 放行一处）；
- P2.4b 已冻结的四项异常边界与九类 verdict 接线表（重验证插入
  consume ok 分支内部，其余 verdict 行为逐位不变）；
- TRAVEL_PENDING_TAKEOVER / TRAVEL_DECISION_TAKEOVER 语义。

## 8. §F 开放决策点汇总（请评审逐项裁决）

| # | 决策点 | 选项 | 建议方案 |
|---|---|---|---|
| D1 | clarify 结果如何进入业务流 | 甲：decide() 仅 clarify 影子外出口（改 decision.py 一处）；乙：独立开关 + 读影子记录；丙：producer 复刻判定 | **甲**（clarify 非执行类，不属于 TAKEOVER 门控范围） |
| D2 | confirmed execute 放行条件 | 甲：channel=1 独立受控路径（不读 TAKEOVER）；乙：仍受 TAKEOVER 门控 | **甲**（两安全机制解耦、回退粒度好；需同步冻结仅查询类批次放行） |
| D3 | 重验证回填接口 | 新增 record_revalidation（双实现）+ 内存桩消费记录七→十字段 | **批准此契约升级**（SQLite 三列自 P2.2 冻结建表即预留） |
| D4 | revalidator 独立文件 vs 并入 producer | 独立（职责单一、可测）vs 并入（文件少） | **独立** |
| D5 | 重验证上下文城市 | 快照候选 city 为锚（推荐）vs 本轮 query_city | **快照城市**（本轮目的地变化已在 P2.3 被 cancel，走不到重验证） |

## 9. §G 已知风险

- 生产侧首轮展示与原链路泛化追问的叠加风险：clarify 短路后本轮
  不再进原分支，需负向测试锁定「本轮只有一个追问出口」；
- ambiguous→新 Pending 的链式场景：连续多轮 ambiguous 可能产生
  长链，规则 8 保证同时仅一个 active，但需测试链式重选不产生
  状态泄漏（旧 pending 全部保持 consumed）；
- 重验证引入 resolve 调用的延迟：KB-only 默认模式下为本地索引
  （毫秒级）；在线模式下受既有探测缓存与限额约束，超时降级
  unverified 保守路径，不阻塞用户（与 Resolver 既有契约一致）；
- DecisionResult.execute 注入分支入口的消费点：必须保证注入失败
  或实体缺失时回退确认话术，绝不裸调 Handler（Handler 零改动
  红线下的防御性设计）。

**申请结论**：以上方案与五个开放决策点经评审定版后申请开工，
建议子阶段划分 P2.5a（生产侧：producer + clarify 出口 + 展示）/
P2.5b（重验证：四分支 + 回填 + confirmed 受控流转），各自独立
commit、独立回归、独立验收；生产开关保持默认 0，不提前进入
P2.6 / P3。

---

## 10. r2 设计修订（评审 CONDITIONAL PASS 定版，2026-11-19）

> 评审裁定：方向认可；D1/D2 选方案甲；D3 修订后冻结；两处设计
> 缺口（§10.2 展示失败补偿、§10.3 HC-1 双概念城市）必须先闭合；
> F1–F4 负向测试补齐；允许/禁止清单按评审模块权限表定版。
> **本节条款与 §0–§9 冲突时，以本节为准。**未授权前不写业务代码。

### 10.1 D1 / D2 / D3 评审裁决（定版）

**D1 = 方案甲（仅 clarify 放行）**，附两条硬约束：
- `decide()` 放行 clarify 时返回的必须是**本轮真实 DecisionResult**，
  其关联 ResolvedCandidate 来源于**本轮 resolver 结果**——绝不从
  历史 shadow 记录、旧 Pending 或上轮 DecisionResult 恢复；
- channel=0 时原有影子行为**逐位保持**；channel=1 时**仅 clarify**
  进入生产侧；execute / reject_reply / conservative / deferred /
  category_path 仍遵守原 TAKEOVER 门控语义，一字不改。

**D2 = 方案甲（独立受控消费路径）**，放行条件正式冻结为五条件
**同时成立**（缺一不可）：

```
AllowHandler = ChannelEnabled
             ∧ SourcePendingConsume      # 可信内部控制流产生，
                                         # 绝不由用户输入/LLM 输出伪造
             ∧ ActionExecute
             ∧ IntentInQueryWhitelist    # 规划类 intent 永不进入
             ∧ RevalidationConfirmed
```

补充冻结：①不得因重验证 confirmed 就直接调用 Handler——必须
继续经过**原有业务分支入口**；②原有角色权限、GuardPipeline、
参数校验与 Handler 前置条件**不得绕过**；③
`TRAVEL_DECISION_TAKEOVER` 默认值与语义不改；④消歧只确认实体
身份，不授予行程修改权限。

**D3 = 接受十字段扩展，回填 SQL 修订后冻结**。原方案 §4.5 的
WHERE 子句缺 candidate_id（方法参数含 candidate_id 但 SQL 未
使用，无法在数据库层验证归属），修订为：

```sql
UPDATE pending_consumption
   SET revalidate_status = ?,
       revalidate_probe_status = ?,
       revalidated_at = ?
 WHERE pending_id = ?
   AND session_id = ?
   AND candidate_id = ?          -- 候选归属数据库层验证
```

`pending_consumption.candidate_id TEXT NOT NULL` 自 P2 方案 §2
冻结建表即存在，双实现消费记录均含该字段，无需 DDL 变更。

**回填六条契约（定版，实现阶段不得再议）**：
1. 必须是**条件更新**，绝不插入新的消费记录；
2. 更新行数 = 1 才算回填成功；
3. 更新行数 = 0 **不得当作成功**（shadow 记录 + 警告）；
4. 回填异常**不得改变已完成的消费结果**（异常吞掉 + shadow 归因
   兜底，业务流不感知）；
5. Memory 与 SQLite 对字段名、合法状态值、时间语义（revalidated_at
   为 INTEGER UTC epoch）**逐位一致**；
6. `revalidate_status` 合法值**固定**为 confirmed / ambiguous /
   unverified / rejected——**仅在重验证真实完成且 resolve 产出
   业务结论时回填**；异常或未执行探测时三列保持 NULL（绝不把
   异常/未探测伪装成四种业务结果之一），异常详情进 shadow。

### 10.2 首轮展示失败的 Pending 生命周期补偿（修订 §3.3，闭合缺口 4.1）

**评审指出的风险**：create_pending 成功但 clarify_reply 构造失败
→ 用户未看到快照，服务端却持有 open Pending 并继续接受选择判断
——用户可见状态与服务端状态不一致。

**冻结原则：不能让用户看不到快照，却让该快照对应的 Pending 继续
正常接受选择。**

**定版顺序（展示内容优先于创建）**：
1. **先构造**候选数据与完整展示内容（话术文本在创建前即为可验证
   的成品字符串）；
2. **再创建** Pending（`create_pending`）；
3. **绑定返回**：Pending ID 与展示内容绑定后短路返回。

**失败补偿矩阵**：

| 失败点 | 处置 |
|---|---|
| 展示内容构造失败（步骤 1） | 未创建，零副作用，交回原链路 |
| create_pending 失败 / 规则 8 拒绝 | 交回原链路（现有契约） |
| 步骤 2 成功后绑定/返回环节失败 | **立即尝试 cancel 该 Pending**（安全取消孤儿）；取消成功 → shadow 记录 + 交回原链路；**取消也失败 → 保守拦截回复**（告知系统暂时无法处理选择请求），**绝不交回原链路**（防 open 孤儿被下轮误消费）+ shadow 记录 orphan Pending + 警告 |

### 10.3 重验证城市上下文的 HC-1 双概念拆分（修订 §4.2，闭合缺口 4.2）

**评审指出**：快照候选 city 是被选中实体的冻结属性，**不应自动
等同**于用户明确指定的 query_city——原方案用候选 city 直接充当
验证期望城市锚点，未完整落实 HC-1 三级区分。

**定版：重验证输入拆为两个正交概念，缺一不可**：

| 概念 | 来源 | 用途 |
|---|---|---|
| **selected candidate city** | 快照候选的冻结 city 字段 | 候选身份与地理属性约束（候选是谁、在哪） |
| **query context city** | 按 HC-1 既有规则构建：用户明确 query_city ＞ LLM city_hints（仅辅助，绝不升级）＞ destination（绝不覆盖用户明确指定） | 参与 Resolver 的城市判定 |

- 绝不用候选 city 直接替代 query context city；绝不让 LLM hint
  覆盖用户明确指定的城市；
- 若本轮存在用户明确 query_city，按既有 Resolver 规则参与判定；
  无明确城市时 query context city 按既有回退链构建（与首轮一致）；
- **若明确 query_city 与候选快照 city 冲突：按既有 Resolver 规则
  重新判断，绝不因 Pending 已消费就强行 confirmed**（冲突导致的
  非 confirmed 结果按四分支如实流转）。

**补充城市矩阵测试五条**（G1–G5）：
- G1 用户明确城市 = 候选城市 → 按既有规则判定；
- G2 用户明确城市 ≠ 候选城市 → Resolver 既有规则重判，不强行
  confirmed；
- G3 无明确城市、仅有 LLM city_hints → hint 仅辅助，不得升级；
- G4 无明确城市无 hints、仅 destination → 按既有回退链，不覆盖；
- G5 三者同时存在 → 优先级不变（明确 ＞ hints ＞ destination）。

### 10.4 测试清单补齐（修订 §6）

**新增负向验收 F1–F4**：

| 编号 | 必须证明的行为 |
|---|---|
| F1 | 消费后重验证结果非 confirmed → 原 Pending 保持 consumed，**Handler 调用次数为 0** |
| F2 | ambiguous 重建 Pending 失败（规则 8 撞车等）→ 不执行 Handler、不复活旧 Pending、不伪造新 Pending 成功 |
| F3 | confirmed 但 Handler 前置权限拒绝 → 继续服从原权限与 Guard 规则，**不因消歧绕过** |
| F4 | 回填失败与业务流分离 → 回填失败不改变四分支结果、不造成重复消费或重复 Handler |

**A3 断言强化**（不止「Handler 被调用」）：
- Handler 接收的是**重验证后构造的** DecisionResult；
- entity 为 fresh confirmed 实体（非快照旧数据）；
- reason 含原 pending_id、candidate_id 与重验证归因；
- 原始 utterance / intent / role 传递符合既有规则；
- **同一轮不再走一次普通意图解析导致重复 Handler 执行**。

**A5 断言强化**：新旧 Pending 生命周期关系——旧 Pending 保持
consumed；新 Pending 独立 ID、独立 TTL、独立候选快照。

### 10.5 允许 / 禁止清单定版（修订 §7，按评审模块权限表）

| 模块 | 权限 |
|---|---|
| pending_producer.py | 新增，允许 |
| pending_revalidator.py | 新增，允许（独立文件，D4 定版） |
| orchestrator.py | 允许：生产侧接入、重验证接入、受控 Handler 流转（AllowHandler 五条件消费点） |
| decision.py | 仅按 D1 方案甲修改 clarify 放行条件（一处） |
| Pending Store（双实现） | 仅增加重验证回填接口（§10.1 修订 SQL + 六条契约）及内存桩十字段 |
| pending_interceptor.py | **原则上冻结**，不修改 P2.3 evaluate/act |
| pending_runtime.py | **冻结**（含 R1） |
| Handler | **冻结**接口与业务实现 |
| EntityResolver | **复用既有接口，不修改判定规则**——P2.5 只调用既有 resolve()，不顺便重构；若 HC-1 上下文传递需最小适配点，**先在设计中列明再单独评审**，不得擅改 |
| 开关 | TRAVEL_PENDING_TAKEOVER / TRAVEL_DECISION_TAKEOVER 语义均不改 |

**channel=0 逐位回归标准（定版）**：
- 全套件在 channel=0 下运行全绿（含 dialog/server/conversations）；
- B10 式逐位对比**扩展**：含 pending 存在、hint 存在、clarify 场景
  的会话与无 pending 基线逐位一致；
- 影子行为与 P2.4b 基线逐位一致——本阶段新增影子字段（revalidate
  三列、orphan 记录等）须在验收报告中**列出新增字段清单**，逐条
  声明不影响既有字段；
- git diff 单点断言：orchestrator 仅生产钩子 + 重验证接入 +
  AllowHandler 消费点三处以内，decision.py 仅 clarify 放行一处。

### 10.6 实施顺序与阶段退出条件（新增，评审点名补齐）

**实施顺序（每子阶段独立 commit / 回归 / 验收）**：

```
P2.5a 生产侧
  1. pending_producer：候选构建（HC-4 排除、六字段、1..N）
     + 展示内容先行构造 + §10.2 补偿矩阵
  2. decision.py clarify 放行（D1 方案甲 + 两条硬约束）
  3. orchestrator 生产钩子 + 测试（A1/A2、C1–C3、G 矩阵生产侧）
  → 独立验收后再开 P2.5b
P2.5b 重验证侧
  4. pending_revalidator：fresh resolve 双概念城市（§10.3）
     + 四分支流转 + B3 兜底延续
  5. record_revalidation 双实现（§10.1 修订 SQL + 六条契约）
     + 内存桩七→十字段
  6. orchestrator AllowHandler 五条件消费点 + 测试
     （A3–A7、B1–B2、D1–D3、E1–E2、F1–F4）
  → 独立验收；P2.6 影子观察另行申请
```

**阶段退出条件（全部满足才申请最终验收）**：
1. 八步链路在 memory 与 SQLite 双实现下全通（A1–A7 含强化断言）；
2. F1–F4 负向全绿；G1–G5 城市矩阵全绿；
3. channel=0 逐位回归标准（§10.5）全部满足；
4. 全量回归绿（既有 13 组套件 + 新增测试）；
5. 重验证三列回填双实现对拍（含 rowcount=0 / 异常路径）；
6. git diff 单点断言通过；实施记录写入本文档 §11。

### 10.7 r2 修订记录摘要

| 评审要求 | 落点 |
|---|---|
| D1 方案甲 + 两条硬约束 | §10.1 |
| D2 方案甲 + AllowHandler 五条件 | §10.1 |
| D3 SQL 补 candidate_id + 六条契约 | §10.1 |
| 首轮展示失败生命周期补偿 | §10.2 |
| HC-1 双概念城市 + G1–G5 | §10.3 |
| F1–F4 + A3/A5 强化 | §10.4 |
| 模块权限表 + channel=0 逐位标准 | §10.5 |
| 实施顺序 + 退出条件 | §10.6 |


---

## 11. P2.5a 独立开工申请（生产侧，2026-11-19）

> 设计基线：`4171095`（P2.5 设计 r2 PASS）。代码基线：`f914072`
> （P2.4b 关闭）。**本申请仅覆盖 P2.5a 生产侧；P2.5b（重验证侧）
> 待 P2.5a 验收通过后另行申请。**

### 11.1 范围（一件事）

ambiguous 执行阻断 → 创建 Pending → 首轮展示快照，含展示失败的
孤儿 Pending 生命周期补偿（§10.2 全矩阵）。**不含**重验证、回填、
AllowHandler 受控流转（全部属 P2.5b）。

### 11.2 文件修改清单（精确到落点）

| 文件 | 修改 | 对应冻结条款 |
|---|---|---|
| `pending_producer.py`（新增） | ①候选构建：clarify fresh matches → 六字段 dict（candidate_id 生成、display_order 1..N 按 Resolver 既有排序、HC-4 角色排除）；②展示内容先行构造（§10.2 步骤 1，成品字符串先于创建）；③create + 绑定返回 + 补偿矩阵（§10.2） | §3.1 / §10.2 |
| `decision.py`（一处） | clarify 影子外出口：仅当 channel=1 且本轮结论为 clarify 时返回本轮真实 DecisionResult（关联候选来自本轮 resolver 结果）；channel=0 影子行为逐位不变；其余 action 门控一字不改 | §10.1 D1 两硬约束 |
| `orchestrator.py`（一处生产钩子） | 接入点 A 决策调用后的 clarify 分支：producer 装配 + 短路返回；规则 8 拒绝时捕获并交回原链路（不覆盖不轮换） | §3.1 |
| `tests/test_pending_producer.py`（新增） | 生产侧专项测试（见 11.3） | — |
| `tests/test_pending_channel.py`（扩展） | 生产钩子集成用例 + channel=0 逐位对比扩展 | §10.5 |
| `docs/PHASE5_P25_PLAN.md` | §12 实施记录 | — |

**禁止触碰（P2.5a 零修改）**：pending_interceptor.py、
pending_runtime.py、pending.py、pending_store_sqlite.py（生产侧
不需要新 store 方法——create/查询既有接口已够用）、Handler 全部、
Resolver、decision.py 其余分支、TAKEOVER 两开关语义、
P2.4b 九类 verdict 接线与四项异常边界。

### 11.3 测试清单（P2.5a 交付判定）

**生产专项（test_pending_producer.py）**：
- P1 候选构建：fresh matches → 六字段、1..N 序号冻结、candidate_id
  唯一可引用；HC-4（negated/remove）角色排除负向；
- P2 展示内容先于创建：构造失败时 create **不被调用**（零副作用）；
- P3 补偿矩阵三行：①绑定/返回失败 → 立即 cancel 孤儿 → shadow
  记录 + 交回原链路；②cancel 也失败 → 保守拦截回复、**绝不交回
  原链路** + orphan 警告；③正常路径 Pending ID 与展示内容绑定；
- P4 规则 8 拒绝：已有 active pending 时新 clarify → create 拒绝
  → 交回原链路，原 Pending 状态不变。

**集成（test_pending_channel.py 扩展）**：
- A1 八步前两步：ambiguous → clarify → 创建 → 展示 1..N（快照
  冻结、ID 可引用）；
- A2 展示轮后新问题 → cancel_new_question → 原链路（验证 P2.3
  拦截器在生产侧 Pending 上照常工作）；
- C1 channel=0 时 clarify → 纯影子、零 Pending 创建（管理器/存储
  零构造）；
- C2 channel=1 仅 clarify 进生产侧：execute/conservative 等其余
  action 仍走原门控（D1 约束②负向）；
- D1 约束①：clarify 结果为本轮真实判定——mock 本轮 KB 变化，
  快照随本轮 matches 变化，与上轮/影子记录无关。

**回归门槛**：全套件绿（13 组既有 + 新增）+ **channel=0 逐位
回归标准（§10.5）全项**：B10 式对比扩展到「pending 存在/hint
存在/clarify 场景」；git diff 断言——orchestrator 仅生产钩子
一处、decision.py 仅 clarify 放行一处。

### 11.4 基线与回滚方案

- **基线**：设计 `4171095` / 代码 `f914072`；P2.5a 交付为单commit
  （或 producer+decision+orchestrator 一体单提交，含测试与记录）。
- **回滚**：①运行时回退——`TRAVEL_PENDING_CHANNEL=0` 即生产侧
  完全旁路（decide 恢复全影子、orchestrator 钩子首行返回），
  无需回滚代码；②代码回退——单 commit revert 即恢复 `f914072`
  行为（新文件 pending_producer.py 独立，revert 无残留耦合）。
- **风险预案**：clarify 出口若与本轮 resolve 时序有实测偏差
  （P2 方案 §8 风险一），接入位置允许在钩子内部实测调整，仍只增
  不改，偏差记入 §12 实施记录。

**申请结论**：按本清单申请 P2.5a 开工；批准后开始编码，交付后
独立提交 + 独立回归 + 独立代码验收；P2.5b 申请待 P2.5a 验收
通过后提交。

---

## 12. P2.5a 实施记录（按 00880ed 批准范围交付，2026-11-19）

> 开工授权：评审批准 P2.5a 按 00880ed 申请范围开工（设计基线
> `4171095`，代码基线 `f914072`）。单 commit 交付；P2.5b 未授权。

### 12.1 实施范围（六项清单对照）

| 申请清单 | 交付 |
|---|---|
| `pending_producer.py`（新增） | PendingProducer：pick_ambiguous（HC-4 纵深排除 + 与 DecisionEngine 同档排序口径）/ build_candidates（六字段、1..N 按 matches 既有序、空 name 拒绝降质）/ clarify_reply_text（成品字符串，与 restate 接线同构）/ produce（§10.2 三步编排 + 补偿矩阵）/ _compensate_orphan（取消失败→保守拦截绝不交回）/ 影子记录（sink 异常隔离，B5 精神） |
| `decision.py`（一处） | decide() 未接管分支内 D1 方案甲放行：`not takeover_on ∧ action==clarify ∧ channel==1` → 返回本轮真实 DecisionResult；channel=0/其他 action/TAKEOVER=1 逐位不变（1 hunk） |
| `orchestrator.py`（生产钩子） | handle 步骤 1.8 调用点一块 + `_pending_produce` 私有方法（决策链首次接入 extract→resolve→decide + clarify 分支 + producer 装配 + lease finally）+ `_ensure_producer`/`_ensure_decider` 延迟构造 + import/字段（5 hunks，逻辑落点一处，见 12.4） |
| `tests/test_pending_producer.py`（新增） | P1–P4 + 影子隔离共 38 断言 |
| `tests/test_pending_channel.py`（扩展） | 第 8 节 5 个集成测试（A1 / A2+新鲜度+C1 / C1·C3 开关 / C2 双 TAKEOVER / 决策链异常），202 断言 → 241 |
| 文档 §12 | 本记录 |

**禁止清单零触碰**（git diff 核查）：pending_interceptor.py、
pending_runtime.py、pending.py、pending_store_sqlite.py、Handler 全部、
entity_resolver.py、decision.py 其余分支（1 hunk 之外无改动）、
P2.4b 九类 verdict 接线与四项异常边界、两开关语义——全部未动；
生产侧未新增任何 store 方法。

### 12.2 强制验收条件逐项证据

| 条件 | 证据（测试断言） |
|---|---|
| 展示先于创建，构造失败零副作用 | test_p2：monkeypatch 话术构造抛异常 + 空 name 候选拒绝 → create_calls==0、produce 返回 None |
| 绑定失败 → 立即取消孤儿 | test_p3①：cancel 恰好一次、pending→cancelled、shadow produce_orphan_cancelled |
| 取消失败 → 保守拦截绝不交回 | test_p3②③'：cancel 抛异常 / cancel ok=False（rowcount=0 同源）→ 返回 _ORPHAN_NOTICE、pending_id 保留、shadow orphan 记录 |
| D1① 本轮真实 DecisionResult | 结构保证：producer/钩子无任何读取 shadow_log/旧 Pending/上轮结果的路径；test_p25a_rule8_and_freshness：轮 2 cancel_new_question 后新 Pending 快照=本轮桩输出（乙），绝不复用轮 1（甲） |
| D1② channel=0 逐位 | test_p25a_channel0_zero_side_effect ×2（env 未设/0）：producer/decider/manager 全 None、stub.calls==0（决策链未调用）、route 非 pending_produce；全量回归 channel=0 全绿 |
| C1/C2 开关负向 | test_p25a_non_clarify_not_produced：confirmed→execute 在 TAKEOVER=0 与 =1 下钩子均交回原链路、零 Pending 创建（本钩子绝不消费 execute，D2 属 P2.5b） |
| P1–P4 | test_pending_producer 38/0 |
| A1/A2 | test_p25a_produce_hook_integration（route=pending_produce、快照 1..2、ID 可引用、hint、decider 影子 clarify、lease 归还）/ test_p25a_rule8_and_freshness（A2 轮） |
| 规则 8 不覆盖不轮换 | test_p4（store 层）+ test_p25a_rule8_and_freshness 轮 3（hand_back 轮钩子撞规则 8 → 原链路、原 Pending open 不变、影子记录） |
| 全量回归 | 见 12.3 |
| git diff 单点断言 | decision.py 1 hunk；orchestrator.py 5 hunks（12.4）；冻结文件零触碰（12.1） |
| 决策链异常健壮性 | test_p25a_decision_chain_failure：resolve 抛异常 → 警告 + 原链路 + lease 归还 + 零 Pending 副作用 |

### 12.3 测试结果（实际运行）

- test_pending_producer：**38 pass / 0 fail**
- test_pending_channel：202 → **241 pass / 0 fail，连续三轮**
- 全量回归：pending 60 / sqlite 66 / interceptor 150 / decision 全过
  （51 断言）/ adapter 32 / resolver 全过（79 断言）/ corpus v4 全过
  （50 断言）/ parser 62 / extractor 36 / dialog 244 / server 52 /
  conversations 21——全部 0 失败
- channel=0 逐位回归：全量回归即默认 env（未设 channel）下执行，
  加上 C1 双 env 断言与 B10 既有对比，§10.5 标准满足

### 12.4 实施差异如实记录（与申请文字的偏差，均不改变冻结语义）

1. **接入点 A 决策调用此前在代码中不存在**：申请 §11.2 写「接入点
   A 决策调用后的 clarify 分支」，经核查 `decide()` 全仓无调用方
   （P1 只交付 decision.py 与影子测试，TAKEOVER=0 下从未接入
   orchestrator）。P2.5a 生产钩子因此包含接入点 A 决策链的**首次
   实际落地**（extract → resolve → decide），全部收在
   `_pending_produce` 单方法内；决策链任何异常 → 保守交回原链路
   （专项测试锁定）。handle() 主流程仅一个调用块。
2. **orchestrator.py diff 为 5 hunks**：import 块 2 + `__init__`
   延迟构造字段 + `_pending_produce` 方法区 + handle 调用点——逻辑
   落点为生产钩子一处，与「仅一处生产钩子」的申请语义一致；
   `git diff` 断言数值如实申报为 5。
3. **A2 集成场景的真实行为优于设计预期**：open pending 下真实
   parse「帮我查一下夫子庙」产出 cancel_new_question（P2.3 冻结
   规则），gate 先真实取消旧 Pending，随后生产钩子以本轮 fresh
   结果创建新 Pending——A2（cancel 接续）与 D1①（新鲜度）在同一
   轮集成验证；规则 8 的集成验证改由 hand_back 轮（gate 零状态
   交回 → 钩子 clarify → create 拒绝 → 原链路）覆盖。
4. **LLM fallback 未接入决策链**：§9.4 步骤 3 的 fallback 在现有
   代码中为 LLMCandidateAdapter（默认关闭、P4 启用）；P2.5a 边界
   未要求接入，决策链为 extract → resolve → decide 三步，与申请
   测试清单一致，偏差仅为此如实记录。
5. **memory store 单槽回查限制**：`_active` 被新 Pending 替换后旧
   Pending 不可按 ID 回查（既有契约，SQLite 有历史）；A2 断言改用
   间接强证明（新 Pending 创建成功 ⇔ 旧 Pending 已不在 open 态，
   否则规则 8 拒绝）+ gate 影子 verdict 锁定。

### 12.5 开关与边界保持

`TRAVEL_PENDING_CHANNEL` 生产默认 0（producer/decider/manager 零
构造、决策链不调用）；`TRAVEL_PENDING_TAKEOVER`、
`TRAVEL_DECISION_TAKEOVER` 默认 0 且语义未动；未新增开关；TAKEOVER
执行类接管语义逐位保持（TAKEOVER=1 时 decide 分支整体不进入）。
P2.5b（重验证 + 回填 + AllowHandler 受控流转）未授权未实施。
