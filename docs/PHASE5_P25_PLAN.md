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

### 11.R3 开工申请修订（复审 2026-11-19 CONDITIONAL PASS 两阻断项落实，形成 r3）

> 复审裁定：范围划分/D1 方案甲/HC-4/展示先行/创建失败处理/模块
> 职责边界/分阶段验收**认可**；两个阻断项（cancel 失败孤儿隔离、
> P2.5a 运行边界）+ 三个细节须补入申请形成 r3，按修订条款做开工
> 前复审。本节纯设计修订，不改业务代码。
>
> **与现状的差异如实申报**：当前归档（83de409）按 §11 原文实施，
> **不含** 11.R3.1 的 L1 二线终结与负向隔离测试——两项为 r3 新增
> 批准条件，批准后以独立整改申请实施（producer 模块内，冻结清单
> 零触碰）。

#### 11.R3.1 阻断项 1：cancel 失败后孤儿 Pending 的可验证隔离

**问题重述（裁定场景）**：create 成功 → 绑定失败 → cancel 失败 →
服务端保留 open 孤儿 → 下一轮有效选择表达可能消费用户从未见过的
快照——重新引入 §10.2 要消除的状态不一致。保守拦截只解决当前轮。

**代码事实基础（r3 取证）**：

1. 消费定位门 `_resolve_pending_id` 第①步 `store.get_active_pending()`
   为**权威定位**（orchestrator.py:103-110，「hint 非权威，store
   为准」）——孤儿 open Pending **无需 hint 即可直达消费路径**，
   裁定关注成立；
2. 既有时间终态：`DEFAULT_TTL_SECONDS = 300`（pending.py:57），
   `get_active_pending` 显式过滤过期 open（规则 5 注释）——TTL
   到期后权威定位①永久不可达；
3. sqlite cancel/expire 同走 `_terminal_transition`
   （pending_store_sqlite.py:537-542），但为**两笔独立事务**
   （各自 connect + BEGIN IMMEDIATE）——瞬时单笔故障下第二次
   调用有效；
4. 现行补偿（83de409）：bind/return 失败 → `cancel_pending` →
   失败（异常或 ok=False）→ 保守拦截 + PendingProduceError +
   shadow orphan——**无二线终结**，裁定指出的缺口属实。

**r3 隔离设计（不加新状态；全部利用既有接口与既有状态语义）**：

- **L1 二线终结（新增补偿步骤，producer 模块内）**：cancel 失败
  分支增加「立即调用 `expire_pending(pending_id)`（契约④既有
  接口）」。两笔独立事务覆盖瞬时故障；expire 成功 → pending
  终态 → `get_active_pending` 返回 None → 权威定位①不可达
  （**可验证**：隔离效果由消费定位门直接判 None 证明，非日志
  推断）；shadow 记 `orphan_terminal_retry`；
- **L2 时间终态（既有，声明为最终防线）**：TTL 300s 到期后孤儿
  永久退出可消费集——无论补偿结果如何，不一致状态有界；
- **L3 消费侧 fail-closed（既有 P2.4b 冻结边界）**：持续性 store
  故障场景下，下一轮 evaluate/act 落入同一故障域 → P2.4b 异常
  边界②「无法判定时保守拦截（不伪造成功、不交回原链路）」→
  消费不可达；
- **L4 单槽阻塞效应（既有，如实申报）**：孤儿占用 `_active`
  槽位期间，规则 8 拒绝一切新 Pending 创建（不覆盖不轮换）——
  孤儿不会被静默轮换出权威定位，爆炸半径有界（≤TTL）。

**残余窗口如实声明（裁定第 1-3 点要求的明示）**：L1+L2+L3 组合
后仍剩窄切片——「cancel 与 expire 两笔事务均失败，而 5 分钟内
后续消费读写正常」。对该切片的**完全封闭**需要新增 store 状态或
新存储接口（超出 P2.5a 冻结范围，须单独评审授权）。r3 立场：在
该切片被单独评审关闭之前，组合防线声明为「隔离尽力而为 + 明示
残余窗口」；且 11.R3.2 的开关冻结使该窗口在生产不可达——**两个
阻断项互为成立条件**。若评审不接受该组合，则明确结论为：当前
补偿方案不足以保证安全，不得启用生产开关（与 11.R3.2 一致）。

**负向测试（新增，r3 交付判定）**：

- 变体一（双终结故障桩）：cancel 抛异常 + expire 抛异常 → 本轮
  保守拦截；下一轮注入旧候选有效选择表达（「第1个」）→ 断言
  Handler 不执行、旧 Pending status 仍 open 未被消费、回复为
  保守拦截形态；
- 变体二（单终结故障桩）：cancel 失败 + expire 成功 → 下一轮
  同样选择表达 → 断言权威定位①判 None（get_active_pending 为
  空）、旧 Pending 不被消费、不走 pending 消费通道；
- 变体三（持久故障对照）：store 持续故障 → 下一轮 evaluate/
  act 触发 P2.4b fail-closed 保守拦截（证明 L3 生效）。

#### 11.R3.2 阻断项 2：P2.5a 与 P2.5b 的运行安全边界（冻结条款）

- P2.5a 可独立开发、测试、提交和验收（范围划分不变，裁定已认可）；
- **P2.5a 验收通过 ≠ 允许生产启用**；
- **P2.5b 最终验收之前，所有生产环境 `TRAVEL_PENDING_CHANNEL`
  必须保持 0**（orchestrator.py:91 默认值 "0" 为冻结项，交付
  diff 须证明无任何默认值/读取逻辑改动）；
- **禁止以测试配置、临时环境变量、灰度配置绕过**本约束将未完成
  重验证闭环的消费链路暴露给真实用户；
- 与 11.R3.1 的关系：开关冻结同时使孤儿隔离残余窗口与「未完成
  重验证的消费链路」在生产均不可达。

#### 11.R3.3 细节 1：cancel 接口精确签名与失败语义

- 签名：`cancel_pending(pending_id: str) -> _ConsumeOutcome`
  （pending.py:436 契约②；pending_store_sqlite.py:537 同签名，
  双实现逐位语义）；expire 同构（契约④，pending.py:452 /
  sqlite:542）；
- 调用条件：仅当 `create_pending` 成功返回 PendingDisambiguation
  且步骤 3（绑定/返回）失败时；入参 = 刚创建实例的 pending_id
  （同会话、status==open）；
- 失败语义：可预期失败以 `ok=False + reason` 返回（pending 不
  存在 / pending_id 不匹配 / status 非 open）；存储层异常以异常
  形态传播（sqlite `_terminal_transition` 连接层无吞异常设计）
  ——producer 侧对 cancel 调用整体 `try/except`（83de409 已实现
  ：异常与 ok=False 同进入保守拦截分支），r3 冻结该承接语义不变。

#### 11.R3.4 细节 2：「绑定/返回环节失败」的精确集合

**触发补偿**（已实现 + r3 冻结）：

- (a) `reply_builder` 抛出任何异常（shadow `produce_bind_failed`
  携带 pending_id）；
- (b) `reply_builder` 返回 None（P2.5a-R2 裁定：与抛异常同视为
  绑定失败，防止 None 交回遗留 open 孤儿）。

**不触发补偿（超出可观测边界，如实声明不谎称可补偿）**：

- `ProduceOutcome` 正常返回之后的**呈现层失败**（响应发送失败、
  网络断连、客户端中断等）——producer 无感知通道，不纳入补偿
  矩阵。该情形下 Pending 保持 open 但用户未见——不一致窗口由
  L2 TTL（300s 后权威定位永久不可达）兜底，如实列为残余风险，
  不宣称闭环。

#### 11.R3.5 细节 3：channel=0 回归逐位对比项（C1/C2 补强）

channel=0 完全旁路回归在既有「回复内容 + 管理/存储零构造」之上，
r3 冻结以下逐位对比项（memory 与 sqlite 双 store 分别执行）：

1. decision 返回值逐位：类型、action、intent、entity、reason
   全文逐字（channel=0 时 decide 必须全影子口径）；
2. producer 调用次数 = 0：`build_candidates`/`produce` 调用计数
   桩证明零调用；
3. store 构造次数 = 0：Runtime Manager 零装配
   （`_pending_manager is None`）；
4. 回复内容逐位相等（与基线输出口径）；
5. shadow：pending 相关记录零新增（`produce_*`/`pending_*` 前缀
   均不出现）。

#### 11.R3.6 r3 交付判定增量汇总

| 增量 | 内容 | 落点 |
|---|---|---|
| 补偿矩阵第三行增强 | L1 二线 expire 终结 + shadow `orphan_terminal_retry` | producer（批准后独立整改申请） |
| 负向隔离测试 | 11.R3.1 三变体 | test_pending_producer / test_pending_channel |
| 运行边界冻结条款 | 11.R3.2 四条 | 本节（验收判据引用） |
| channel=0 逐位对比 | 11.R3.5 五项 | test_pending_channel C1/C2 |

**r3 申请结论**：按本节修订条款请求开工前复审；复审通过后按
增量清单实施（producer 模块内变更 + 测试，冻结清单零触碰），
实施仍走独立 commit + 独立验收。

#### 11.R4 双终结失败残余窗口的验收边界（r3 复审 CONDITIONAL PASS 整改，形成 r4）

> 复审裁定（2026-11-19）：设计方向认可、暂不批准开工；§11.R3.1
> 存在逻辑矛盾——双终结失败 + 下一轮存储恢复后，当前机制无任何
> 依据阻止消费定位门定位并消费孤儿；不得声称 L1–L4 已完全隔离。
> 本节按裁定修正安全性质定性与测试预期，业务代码零改动。

**1. 安全性质重新定性（取代 11.R3.1 的防线表述）**

| 防线 | 修正后定性 | 边界 |
|---|---|---|
| L1 二线 expire | **二次终结补偿**（尽力而为） | 成功 → 隔离可验证（定位判 None）；失败 → **无后续隔离保证** |
| L2 TTL | **最终清理边界** | 到期后过期 Pending 不再经权威定位进入消费链路；**到期前仍可被定位**——不是即时隔离机制 |
| L3 fail-closed | **仅持续性故障生效** | 下一轮 store 读取仍失败时保守拦截；存储恢复后**不再提供隔离** |
| L4 单槽阻塞 | **生命周期约束及影响范围控制** | 只阻止新 Pending 覆盖/轮换，**不阻止旧 Pending 被消费**——不是消费隔离防线 |

**结论行（冻结表述）**：L1–L4 组合属于**尽力而为的补偿及故障
约束，不构成双终结失败后的即时消费隔离保证**。

**2. 负向测试变体一修正（拆分为两个独立场景）**

r3 变体一的验收逻辑不成立——双终结失败后下一轮若存储恢复，
`get_active_pending()` 正常返回、定位门可定位孤儿，已描述机制
无隔离依据。修正为：

- **场景一（持续性故障 → 验证 L3）**：cancel/expire 失败，且
  下一轮 store 读取**仍然失败** → 断言 P2.4b fail-closed 保守
  拦截、旧 Pending 未被消费。验证的是持续故障下的 fail-closed，
  **不是**双终结失败后的孤儿隔离；
- **场景二（故障恢复 → 残余窗口的存在性证明）**：cancel/expire
  失败，下一轮 store 读取**恢复正常** → 验收预期 = **承认残余
  窗口存在**：断言仅限「本轮保守拦截已发生 + shadow 双通道记录
  孤儿身份（pending_id 可溯）+ 孤儿在 TTL 内仍可被权威定位
  （以 `get_active_pending()` 返回非空为证）」——该场景作为
  **已知风险的回归证据**（证明风险真实存在），**绝不写成必然
  不消费**；不伪造隔离成功断言；
- 场景二预期更新条款：未来若实现真正的隔离机制，本场景预期
  随之更新为「定位被拒 / 消费被拒」，并以独立设计评审为前提。

**3. 生产约束继续冻结 + 验收检查范围扩大（11.R3.2 增补）**

- 双终结失败残余窗口未通过独立设计彻底封闭前，生产环境保持
  `TRAVEL_PENDING_CHANNEL=0`（R3.2 四条条款全部继续有效）；
- **验收不只核代码默认值**：须检查**所有生产配置入口、部署配置
  与环境变量读取路径**，确认无其他途径把开关设为 1——验收
  证据包括：`TRAVEL_PENDING_CHANNEL` 读取点全库清点（r4 预核对：
  实际为**两处** `os.environ.get`——orchestrator.py:91 与
  decision.py:167，默认值均 "0"、`.strip()=="1"` 同一判定式；
  其余出现均为注释/文档字符串）+ 部署/配置文件无 `TRAVEL_*`
  写入的清单核对（r4 预核对：仓库内无 yaml/toml/ini/env/服务
  配置含 `TRAVEL_*`）。

**4. channel=0 逐位对比基线补充（11.R3.5 增补）**

decision 返回值逐位对比必须覆盖 **channel=0 时本轮真实 clarify
场景**（decide 未接管分支内的 clarify 路径），不能只拿普通非
clarify 场景作基线——否则无法证明 D1 放行改动在 channel=0 下
没有破坏原有影子行为。

**5. 后续独立评审声明**

若未来要求 channel=1 下也保证双终结失败后绝不消费孤儿，必须
**另行设计真正的隔离机制**（如 store 级隔离语义或消费定位
准入控制），不得以 TTL、单槽阻塞或「故障持续」假设替代；届时
另案单独申请评审。

**r4 申请结论**：按本节请求简短复审；通过后批准 P2.5a 按
11.R3.6 增量清单（含本节修正后的测试预期）独立整改开工。当前
维持业务代码零改动，不得提前实施。

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

---

## 13. P2.5a 验收关闭记录（2026-11-19 评审裁定）

### 13.1 裁定结果

**P2.5a：PASS，验收关闭。** 最终修正提交 `4a639a8`（R3 契约修正）。
本裁定不构成 P2.5b 或 P3 的开发授权——下一阶段仍需单独提交申请
与实施方案。

| 项 | 状态 |
|---|---|
| P2.5a 生产侧（32b2e63 + 74754f1 + 4a639a8） | 验收通过，关闭 |
| P2.5b 重验证侧 | 尚未授权启动 |
| P3 | 未启动 |
| TRAVEL_PENDING_CHANNEL | 0（不变） |
| TRAVEL_PENDING_TAKEOVER / TRAVEL_DECISION_TAKEOVER | 0 / 0（不变） |

### 13.2 验收过程（三轮）

1. **初验（32b2e63）**：commit 身份/范围/冻结零触碰/测试证据摘要符合；
   五 hunk 偏差待裁定 → 补交 1111 行完整 diff 与原始测试日志。
2. **CONDITIONAL PASS #1（R2 = 74754f1）**：五个 orchestrator hunk
   裁定接受——§10.5「三处以内」为 P2.5 全阶段逻辑触点约束（生产钩子
   + 重验证接入 + AllowHandler 消费点），P2.5a 仅占用其一，P2.5b 配额
   完整保留。孤儿补偿两缺口需修正：①reply_builder 返回 None 同视为
   绑定失败进补偿矩阵；②保守回复不再依赖刚失败过的同一 builder。
3. **CONDITIONAL PASS #2（R3 = 4a639a8）**：契约核验裁定——服务层
   （Session.turn / server.py /api/chat）对 handle 返回值直接属性访问
   reply.render/mode/plan/slots/turn_no/ask_rounds/assumptions/trace/
   fetched 九类字段，纯字符串兜底违反返回类型契约；修正为
   _conservative_reply 两层定版（builder 同源层 → 自建 Reply 终层，
   mode="answer" 与 answer_reply 同款）+ 终层理论不可达失败以
   PendingProduceError（携带孤儿 Pending ID）明确失败，绝不静默交回。

### 13.3 最终验收清单（评审逐项裁定）

None 返回触发孤儿补偿 ✓ / builder 异常触发孤儿补偿 ✓ / 取消失败后
保守拦截 ✓ / 保守回复恒符合 Reply 类型契约 ✓ / 终层构造失败明确抛出
PendingProduceError ✓ / 孤儿 Pending ID 可追踪（异常消息 + shadow_log
双通道）✓ / 冻结文件零触碰（orchestrator 五 hunk 接受、decision 1 hunk、
interceptor/runtime/pending/双 store/resolver/dialogue 零 diff）✓ /
producer 专项 58/0 ×3、channel 专项 254/0 ×3 ✓ / 14 套件全量回归全绿 ✓

**验收证据边界（评审声明）**：本次裁定基于提交方提供的原始代码、完整
diff 与测试实跑报告完成静态复核；评审方未在仓库中独立执行 Git 命令
或复现测试。

### 13.4 修正提交链

| commit | 内容 |
|---|---|
| `32b2e63` | P2.5a 生产侧实施（按 00880ed 批准范围，§12） |
| `74754f1` | R2：孤儿补偿返回值缺口修正（None 视为绑定失败 + 保守回复独立构造路径） |
| `4a639a8` | R3：Reply 类型契约定版（移除字符串兜底 + PendingProduceError 明确失败） |

---

## 14. P2.5b 独立开工申请（重验证侧，2026-11-19）

> 设计基线：`4171095`（P2.5 设计 r2 PASS，§10 定版条款优先）。
> 代码基线：`83de409`（P2.5a 验收关闭归档；生产侧终态 32b2e63 +
> R2 `74754f1` + R3 `4a639a8`）。**本申请仅覆盖 §10.6 实施顺序
> 第 4–6 步（P2.5b 重验证侧）；P2.6 影子观察与 P3 不在本申请范围。**

### 14.1 范围（一件事）

消费成功（P2.4b 短路决定性依据不变）→ gate consume ok 分支内、
确认回复构造**之前**插入 fresh 重验证（§4.1 铁律：重验证只决定
「之后交给谁」，绝不反向改变 Pending 状态，原 Pending 保持
consumed）→ 四分支流转（§4.3）+ confirmed 查询类经 AllowHandler
五条件受控流转（§10.1 D2 方案甲）+ 回填三列（§10.1 D3 修订 SQL +
六契约）。

### 14.2 文件修改清单（精确到落点）

| 文件 | 修改 | 对应冻结条款 |
|---|---|---|
| `pending_revalidator.py`（新增） | PendingRevalidator：①输入边界——selected candidate_id ∈ 原快照**再次校验**（不重复信任消费事务）、candidate city 仅作身份属性、query context city 按 HC-1 既有规则构建（明确 query_city ＞ LLM hints 仅辅助 ＞ destination，绝不覆盖/替代，冲突走 Resolver 既有规则重判，§10.3 双概念）；②fresh resolve——全新调用既有 `resolve()`，不复用旧 matches/旧 DecisionResult，probe_status 如实标注；③四分支产出（§4.3 全表：confirmed 查询类构造 DecisionResult(execute) 含全链归因 reason / 规划类只确认话术；ambiguous→新候选快照交 producer 创建新 Pending；unverified→保守回复绝不 rejected；rejected→规定话术保留原始表达）；④B3 兜底延续——重验证任何异常 → 既有 `_fallback_confirm_reply` 确认短路，消费事实不撤销；⑤回填调用——按六契约调用 record_revalidation，rowcount=0/异常仅 shadow 归因不改业务流 | §4.1–4.3 / §10.1 D3 / §10.3 |
| `pending.py`（Memory store） | 新增 `record_revalidation(...)` 方法（条件更新语义：按 pending_id+session_id+candidate_id 定位**既有**消费记录，命中恰一条才写三列并返回 True；未命中/异常返回 False，绝不插入新记录）；消费记录七字段→十字段（+revalidate_status/revalidate_probe_status/revalidated_at，默认 None；七字段名与语义逐位不变；`_latest_consumption` 返回扩容） | §10.1 D3 六契约 / §4.5 |
| `pending_store_sqlite.py` | 新增同名 `record_revalidation`：单条条件 UPDATE（§10.1 修订版，WHERE 含 candidate_id 候选归属数据库层验证）SET 三列；rowcount=1 → True / 0 → False；无 DDL（三列自 P2.2 冻结建表预留）；与 Memory 逐位一致（字段名/合法值/epoch 语义） | §10.1 D3 |
| `orchestrator.py`（两逻辑触点，配额内） | ①**重验证接入**：`_dispatch` V_CONSUME ok 分支内、确认回复构造之前——原静态确认回复改为「重验证 → 四分支产出」；其余八类 verdict 分支**逐位不动**；②**AllowHandler 受控消费点**（D2 方案甲首次落地）：五条件 AND（ChannelEnabled ∧ SourcePendingConsume ∧ ActionExecute ∧ IntentInQueryWhitelist[规划类永不] ∧ RevalidationConfirmed），全过 → 注入既有业务分支入口执行 Handler（Handler 零改动、GuardPipeline/权限/前置条件不绕过）；任一条件不满足 → 确认话术交回；注入失败/实体缺失 → 回退确认话术，绝不裸调 Handler | §10.1 D2 / §10.5 |
| `tests/test_pending_revalidator.py`（新增） | 重验证专项（见 14.3 第一组） | — |
| `tests/test_pending_sqlite.py`（扩展） | record_revalidation 双实现对拍（见 14.3 第三组） | §10.1 D3 |
| `tests/test_pending_channel.py`（扩展） | 集成与负向（见 14.3 第二组） | §10.4 |
| `docs/PHASE5_P25_PLAN.md` | §15 实施记录 | — |

**禁止触碰**：pending_interceptor.py（evaluate/act 九类 verdict 判定
与执行原样）、pending_runtime.py（含 R1）、decision.py（**本阶段零
改动**——D1 已在 P2.5a 完成）、Handler 全部、entity_resolver.py
（只调用既有 resolve()，HC-1 上下文经既有 context 参数传递，无适配
点则零改动；若实测需最小适配点，先停手单独申报评审）、
create_pending/consume_pending 等既有五方法契约、SQLite 建表语句、
P2.4b 四项异常边界与其余八类 verdict 接线、两 TAKEOVER 开关语义。

### 14.3 测试清单（P2.5b 交付判定）

**第一组：重验证专项（test_pending_revalidator.py）**
- 输入边界：candidate_id 不在原快照 → 拒绝重验证（B3 兜底）；
  快照 city 仅作身份属性、query context city 按 HC-1 三级构建的
  单元级断言；
- G1–G5 城市矩阵（§10.3 五条，resolve 数据源桩 + 真实判定链）；
- B1 fresh：mock KB 变化 → 重验证结果跟随变化（成文断言不复用旧
  matches）；B2 探测缓存命中 / quota_exceeded / unavailable 路径
  probe_status 如实标注；
- 四分支产出单元：confirmed(execute DecisionResult 归因齐全) /
  ambiguous(新快照+新旧 pending_id 关联影子) / unverified(保守话术)
  / rejected(规定话术)；
- 回填六契约逐条：条件更新不插入 / 命中 1 行成功 / 0 行 False+shadow /
  异常不改消费 / 双实现逐位 / 合法值四枚举、异常与未探测保持 NULL。

**第二组：集成与负向（test_pending_channel.py 扩展）**
- A3（强化断言）：选择 → 消费 → 重验证 confirmed → 查询类
  DecisionResult 进既有分支 Handler——Handler 收到**重验证后构造**
  的 DecisionResult、entity 为 fresh confirmed 实体、reason 含
  pending_id/candidate_id/重验证归因、同一轮无重复意图解析重复执行
  （route/影子锁定）；
- A4 规划类 confirmed → 只回确认话术，行程状态深拷贝对比零写入；
- A5（强化）：ambiguous → 新 Pending（新 ID ≠ 旧 ID、新 TTL、新
  快照来自 fresh matches；旧 Pending 保持 consumed 不复活、不重复
  消费）；
- A6 unverified → 保守回复、绝不 rejected 话术、Handler 调用 0 次；
- A7 rejected → 「规定数据源与检索范围内没有找到」、保留原始表达；
- D1 重验证抛异常 → B3 兜底确认短路（消费不撤销）；D2 回填失败 →
  回复与状态不变、shadow 归因；D3 重验证期间 store 异常 → 保守路径；
- E1 四分支全部回填三列（memory 桩十字段实读断言）；E2 失败分支
  影子含原 pending_id/candidate_id/重验证结果；
- F1 非 confirmed → 原 Pending 保持 consumed、Handler 调用 0 次；
  F2 ambiguous 重建失败（规则 8 撞车）→ 不执行 Handler、不复活旧
  Pending、不伪造成功；F3 confirmed 但 Guard/权限拒绝 → 服从原规则
  不绕过；F4 回填失败与业务流分离（不改变四分支结果、无重复消费/
  重复 Handler）；
- channel=0 逐位：重验证侧零触发（回填零调用、消费回复与 P2.4b
  基线逐位一致）。

**第三组：双实现对拍（test_pending_sqlite.py 扩展）**
- record_revalidation 双实现：命中 1 行（三列落库、revalidated_at
  为 INTEGER UTC epoch）/ 0 行（跨 session、跨 pending、candidate_id
  不符三形态全 False）/ store 异常 → False 不抛出；
- 十字段对拍：_latest_consumption 双实现字段名与值逐位一致、七字段
  语义不变（duplicate_select 话术回归不受影响）。

**回归门槛**：全套件绿 + channel=0 全量逐位（§10.5 全项，含新增
影子字段清单）+ git diff 断言：decision.py 零 diff；orchestrator
限两逻辑触点（重验证接入 + AllowHandler 消费点）；store 限
record_revalidation + 十字段。

### 14.4 新增影子字段预申报（§10.5 要求的清单）

| 载体 | 新增字段 | 对既有字段影响 |
|---|---|---|
| revalidator 影子记录 | stage="revalidator" / action / detail / pending_id / candidate_id / revalidate_status（可选） | 纯新增独立 log，不影响 gate/producer 影子 |
| 内存桩消费记录 | revalidate_status / revalidate_probe_status（TEXT，默认 None）/ revalidated_at（INTEGER，默认 None） | 七字段名与语义逐位不变（D3 已批契约升级） |
| SQLite | 无 DDL、无新列 | UPDATE 仅写三列预留列 |

### 14.5 预期偏差预申报（如发生，记入 §15 并如实报告）

1. **AllowHandler 消费点为该结构的首次落地**：§9.4 所述「既有分支
   入口的 DecisionResult 消费点」在当前代码中尚不存在（同 P2.5a
   偏差 1 的成因——decide() 在 P2.5a 前无调用方）。P2.5b 将按设计
   在分支入口建立该消费点并注入重验证 DecisionResult，属于设计
   授权范围内的建立而非改动既有结构；orchestrator 预计 hunks 数
   以实交为准、如实申报（逻辑触点两处，配额内）。
2. **重验证接入点在 P2.4b 冻结接线区内的一处授权修改**：consume ok
   分支为 §10.5 允许清单明示的「重验证接入」落点；修改后其余八类
   verdict 分支逐位不变由既有 150 项 interceptor/channel 断言 +
   新增对比锁定。
3. **测试数据源桩沿用 P2.5a 风格**：真实 KB 无测试歧义实体，
   resolve 数据源用桩、extract/decide/Resolver 判定链保持真实执行
   （A3/BD/E/F/G 各组同风格）。

### 14.6 基线与回滚方案

- **基线**：设计 `4171095` / 代码 `83de409`；P2.5b 交付单 commit
  （revalidator + 双 store + orchestrator 两触点 + 测试 + §15 记录）。
- **回滚**：①运行时——`TRAVEL_PENDING_CHANNEL=0` 即重验证与受控
  流转完全旁路（consume ok 分支行为需回退确认路径由设计保证逐位
  等效 P2.4b 基线）；②代码——单 commit revert 恢复 `83de409`
  （pending_revalidator.py 独立新文件；store 新方法独立，revert 无
  残留耦合）。
- **风险预案**：重验证 resolve 延迟（在线模式）→ 既有探测缓存与
  限额约束内，超时降级 unverified 保守路径（§9 既有风险条款）；
  链式 ambiguous 多轮 → 规则 8 保证单 active，链式状态泄漏由 A5
  强化断言锁定。

**申请结论**：按本清单申请 P2.5b 开工；批准后开始编码，交付后
独立提交 + 独立回归 + 独立代码验收；P2.6 影子观察与 P3 不因本
阶段通过而自动启动。

---

## 15. P2.5b 实施记录（按 §14 批准范围交付，2026-11-19）

> 开工授权：评审批准 §14 申请范围（设计基线 `4171095`，代码基线
> `83de409`）。单 commit 交付；P2.6 影子观察与 P3 未授权未实施。

### 15.1 实施范围（§14.2 六项清单对照）

| 清单项 | 交付 |
|---|---|
| `pending_revalidator.py`（新增，+335 行） | PendingRevalidator：①归属再校验（重读 Pending、candidate_id ∈ 快照再验证、非 consumed 保守 B3）；②HC-1 双概念（§10.3：query context city 按 明确 query_city ＞ hints 仅辅助不入期望城市 ＞ destination 重建；候选 city 仅身份属性入影子归因）；③fresh resolve（全新调用既有 resolve()，probe/review/kb 可注入，绝不复用旧 matches）；④四分支产出（confirmed 查询类构造 DecisionResult(execute) 全链归因 reason 含 pending_id/candidate_id/probe/规划类与 HC-4 只确认不构造 execute；ambiguous 委托 producer 重建新 Pending+失败保守重试话术；unverified 保守回复绝不伪装 rejected；rejected 规定话术保留原始表达；skipped/未知状态防御不回填）；⑤D3 六契约回填（四枚举校验、异常吞掉+shadow 归因）；⑥永不抛异常（异常 → status="" → 调用方 B3） |
| `pending.py` | `record_revalidation`（条件更新语义：pending_id+candidate_id+session 三键定位既有记录，命中恰一条写三列；未命中/异常/非法值 False，绝不插入）；消费记录七→十字段（+revalidate_status/revalidate_probe_status/revalidated_at 默认 None；REVALIDATE_STATUSES 四枚举常量；CONSUMPTION_FIELDS 契约清单） |
| `pending_store_sqlite.py` | 同名 `record_revalidation`：单条条件 UPDATE（§10.1 修订版 WHERE 含 candidate_id），rowcount==1 → True / 0 → False；连接与执行异常全部吞掉 → False（契约 4）；无 DDL（三列自 P2.2 预留）；`_latest_consumption` 扩容十字段（revalidated_at NULL 保持 None，落库转 int） |
| `orchestrator.py`（两逻辑触点，配额内） | ①**重验证接入**：`_pending_reply` V_CONSUME ok 分支内、确认回复构造前调 `_revalidate_after_consume`（四分支成品回复短路；查询类 confirmed 将 DecisionResult 挂 ctx + 来源标记 `pending_consume` 返回 None 交触点 2；规划类/HC-4 confirmed → 冻结确认话术；status="" → `_fallback_confirm_reply` B3 短路消费不撤销）；②**AllowHandler 消费点**（D2 方案甲首次落地，§9.4「分支入口先看 DecisionResult」结构建立）：handle 步骤 1.77 调 `_allow_handler_consume`——五条件 AND（ChannelEnabled ∧ SourcePendingConsume ∧ ActionExecute ∧ IntentInQueryWhitelist=DEFAULT_BATCHES 规划类永不 ∧ RevalidationConfirmed=归因证据+实体非缺失），全过经既有入口 `session._answer(kind, 原 utterance)` 执行 Handler（与步骤 5 同一入口，Guard/权限零绕过），任一不满足/注入失败/Handler 未接住 → 冻结确认话术交回，绝不裸调 Handler、绝不退回普通路由；其余八类 verdict 分支逐位不动 |
| `tests/test_pending_revalidator.py`（新增） | 输入边界四态 + G1–G5 城市矩阵 + B1 fresh（KB 变化跟随 + 链式重选）+ B2 探测四路径如实标注 + 四分支产出 + 六契约 + F4 分离，**71 断言** |
| `tests/test_pending_sqlite.py`（扩展） | `test_revalidation_backfill_parity`：双实现命中 1 行/0 行三形态/非法值/异常吞掉/十字段逐位对拍；既有七字段断言按 D3 已批升级为十字段（**字段集断言更新，前七字段名与语义逐位不变**） |
| `tests/test_pending_channel.py`（扩展） | 第 9 节 7 个集成测试（A3 强化断言/A4 行程深拷贝零写入/A5 新旧 Pending 生命周期/A6+A7/F1/D1-D3+F4/E2/F2/F3/channel=0 零触发），254 → **308** |
| `docs/PHASE5_P25_PLAN.md` | 本记录 |

### 15.2 实施差异如实申报（与 §14 文字的偏差）

1. **TurnContext 新增三个载体字段（context.py +7 行）**：§14.2 文件
   清单未列 context.py。触点 1 与触点 2 分处 gate 与 handle 主流程，
   DecisionResult/来源标记/原始问句需要本轮生命周期载体
   （`pending_decision` / `pending_decision_source` /
   `pending_decision_utterance`，默认 None——普通轮逐位不变）。
   属触点 2 的必要配套（§9.4 结构的 ctx 载体形式），非第三逻辑触点、
   不触碰任何冻结模块；如评审裁定不可接受，可改为动态属性挂载
   （零文件改动），等价语义。
2. **orchestrator.py 源码 diff 为 10 hunks（-U0 口径）**：import 块 +
   冻结常量 + `__init__` 延迟字段 + gate 调用点 ctx 传参（既有
   `_pending_reply` 内部签名扩展）+ V_CONSUME ok 分支改写（触点 1）
   + 新方法区（`_ensure_revalidator`/`_consume_confirm_reply`/
   `_revalidate_after_consume`/`_allow_handler_consume`）+ handle
   1.77 调用块（触点 2）。**逻辑触点恰两处**（重验证接入 +
   AllowHandler 消费点），§10.5 配额内；hunk 数以实交为准如实申报。
3. **冻结确认话术提取为 `_consume_confirm_reply` 共用助手**：触点 1
   （规划类 confirmed）与触点 2（五条件不满足回退）共用同一文案来源，
   文本与 P2.4b 冻结口径逐字一致，避免双处维护分叉。
4. **B 段既有测试补重验证桩（兼容垫片）**：六处 P2.4b 时代消费断言
   （集成 on / B1 duplicate 前置 / B5 / B8 / B9 双实现）在 P2.5b 后
   消费 ok 必经重验证——真实 KB 无测试实体 → unverified 保守回复，
   与其「冻结确认话术」断言冲突。加 `_stub_rv_confirmed` 垫片
   （confirmed 桩 → plan 类 pending 走冻结确认话术），断言语义
   保持原样；该行为变化本身即 P2.5b 设计目标（channel=1 消费轮
   按 fresh 四分支流转，channel=0 逐位不变——B10 断言锁定）。
5. **选择话术统一为「第1个」**：裸数字 "1" 非选择语境（拦截器
   `_ORDER_RE` 冻结：序号须带锚定词，第 9 节新测试据此用合法锚定
   形式；拦截器本身零改动）。
6. **rejected 分支的真实链路可达性**：复核源类型适用范围为
   attraction（Resolver 冻结行为），unknown 类型单源空证据 →
   unverified 保守路径——rejected 产出以注入 resolver 分支级验证
   （71 断言中锁定规定话术/保留原始表达/回填溯源），真实链路行为
   属 Resolver 冻结范围不属本阶段。

### 15.3 新增影子字段清单（§10.5 要求逐条声明不影响既有字段）

| 载体 | 新增 | 影响 |
|---|---|---|
| revalidator 影子记录 | stage="revalidator" / action / detail / pending_id / candidate_id / revalidate_status（§14.4 预申报原样落地） | 纯新增独立 log；gate/producer 影子零改动 |
| 内存桩消费记录 | revalidate_status / revalidate_probe_status（默认 None）/ revalidated_at（默认 None） | 七字段名与语义逐位不变（duplicate_select 话术回归不受影响——B 段断言全绿） |
| SQLite | 无 DDL、无新列 | UPDATE 仅写三列预留列 |
| TurnContext | pending_decision / pending_decision_source / pending_decision_utterance（默认 None） | 普通轮全默认值，逐位不变（B10/集成 off 断言锁定） |

### 15.4 测试结果（实际运行）

- test_pending_revalidator：**71 pass / 0 fail**，连续 3 轮
- test_pending_channel：254 → **308 pass / 0 fail**，连续 3 轮
- test_pending_sqlite：66 → **85 pass / 0 fail**，连续 3 轮
- 全量回归 14 套件全绿（当日实跑、退出码全 0）：pending 60 /
  sqlite 85 / interceptor 150 / decision 51（decision.py 零 diff
  下全绿）/ orchestrator 33 / routing 49 / reply 28 / resolver 79 /
  corpus v4 50 / parser 62 / extractor 36 / dialog 244 / server 52 /
  conversations 21
- channel=0 逐位：B10 既有对比 + 新增 `test_p25b_channel0_zero_
  revalidation`（双 env：零构造、零 resolve 调用、零回填、原路由）
  + 全量回归默认 env 未设 channel 下执行

### 15.5 开关与边界保持

`TRAVEL_PENDING_CHANNEL` 生产默认 0（重验证/受控流转完全旁路）；
`TRAVEL_PENDING_TAKEOVER`、`TRAVEL_DECISION_TAKEOVER` 默认 0 语义
未动；未新增开关；decision.py 零 diff（D1 已在 P2.5a 完成）；
interceptor/runtime/pending 五方法契约/resolver/Handler/dialogue/
producer 零触碰（git diff 复核为空）；P2.6 与 P3 未授权未实施。

### 15.6 回滚

单 commit revert 恢复 `83de409`：pending_revalidator.py 独立新文件；
store 新方法独立；orchestrator 触点集中在 V_CONSUME ok 分支与 1.77
调用块；context.py 三字段默认 None 无行为耦合；测试扩展独立段落。
运行时回退：channel=0 即整体旁路。

## 16. P2.5b-R1 修复记录（验收 FAIL 2026-11-19 的整改，待复审）

> 评审裁定（2026-11-19）：P2.5b 当前代码验收 **FAIL**，不归档 PASS；
> 设计批准保持有效；P2.6/P3 未授权；三开关保持关闭。修复严格限定
> 在已批准范围内，冻结模块（Resolver/Handler/interceptor/runtime/
> decision.py）零修改（git diff 复核为空）。

### 16.1 P0 缺陷修复（回填双实现）

**RV-01 内存 Store（pending.py record_revalidation）**
- **唯一命中校验**：原实现 `for ... reversed` 命中即写（多条命中时
  只改最后一条并返回 True）→ 改为统计命中数，`len(hits) != 1`
  （0 条或多条）一律 False 零写入，与 SQLite rowcount==1 唯一性
  语义逐位对齐；
- **半成功防线**：测试暴露 `_epoch(self._now())` 在写入
  revalidate_status/probe_status 之后执行，时钟异常时前两列已被
  改写（违反契约 4「异常零写入」）→ 唯一可失败步骤提前到任何写入
  之前（先算后写，与 consume_pending 前置段同范式）；
- **异常返回 False**：复核确认当前提交（1fce3f3）已有整体
  `except Exception → False`（pending.py 原 407–422 行），裁定中
  「异常直接抛出」基于的早期 diff 版本与最终提交不一致，本项在
  修复前已满足，专项测试（时钟 RuntimeError 注入）在 1fce3f3 与
  本修复提交下均通过。

**RV-02 SQLite Store 提交顺序（pending_store_sqlite.py）**
- 原实现 `con.commit()` 后才 `return cur.rowcount == 1`：多行命中
  时两行已修改且已提交、最后才返回 False——数据被错误落库；
- 修正为**先校验后提交**：`BEGIN IMMEDIATE`（与
  `_terminal_transition` 同范式；连接 isolation_level=None 显式
  事务模式，裸 UPDATE 会即时自动提交，rollback 无法撤销——故必须
  显式 BEGIN 包住 UPDATE）→ UPDATE → `rowcount != 1`（0 行或多行）
  → `ROLLBACK` + False；恰一行 → `COMMIT` + True；
- 新增专项测试：无 PK 测试 schema 造双行同键记录 → False 且两行
  三列保持 NULL（全部回滚）；恢复唯一后 True 正常落库。生产 DDL
  以 pending_id 为 PRIMARY KEY，正常链路不可能多行命中——本测试
  证明防线在唯一性被破坏的历史库上依然生效。

**RV-03 SQLite 异常边界**
- `except sqlite3.Error` → `except Exception`（含 _now_epoch 时钟
  故障、连接层任意 RuntimeError 等，契约 4 不向业务层传播）；
- 事务中途异常（UPDATE 已执行、事务已开启）→ `self._rollback(con)`
  撤销已修改行（复用既有 `_rollback` 静态助手）再返回 False；
- 新增专项测试三类：时钟 RuntimeError → False 三列 NULL；连接层
  RuntimeError → False；UPDATE 后注入异常（事务已开启）→ 已修改
  行被 ROLLBACK（三列 NULL，绝不半成功提交）。

### 16.2 P1 修复与证据（RV-04/05/06/07/08）

**RV-04/RV-08 rejected 保留原始表达（pending_revalidator.py）**
- 原实现 `_REJECTED_TMPL.format(name=candidate_name)`，
  original_utterance 未传入 rejected 分支——与 §4.3/A7 冻结口径
  「按『规定数据源与检索范围内没有找到』话术回复，保留原始表达」
  不符；
- 修正：`_branch_rejected` 增加 `original_utterance` 参数（取自
  原 Pending 快照，消费成功前必非空；空值降级为仅候选名形式），
  话术改为「在规定数据源与检索范围内没有找到与您的问题
  「{utterance}」对应的「{name}」。」——原始问句为句子主体，
  候选名附带说明；
- 测试修正（A7/RV-08）：原始问句「哪家海贝皇大排档好」与候选名
  「海贝皇大排档（南京店）」天然不同，断言二者**分别出现**且原始
  问句先于候选名（不再是只证明候选名保留的弱断言）。

**RV-05 LLM city hints 辅助语义（对照 HC-1 原文）**
- 批准原文（§1.3）：「用户明确 query_city（可参与判定）＞ LLM
  city_hints（仅辅助线索，不得升级为用户明确指定）＞ destination
  （规划目的地，LLM 提示绝不覆盖）」；§10.3 表格同口径。
- 当前实现（pending_revalidator.py ② 段）：hints 仅清洗后记录进
  shadow（`hints=...（仅辅助不入期望城市）`），**不参与
  effective_city 构建**（query_city ＞ destination ＞ ""），不传入
  resolve ctx。定位：**只记录、不参与任何判定输入**。
- 依据：EntityResolver 冻结（§10.5：「若 HC-1 上下文传递需最小
  适配点，先在设计中列明再单独评审，不得擅改」）——resolve() 无
  hints 入参，未经评审授权不得新增传参通道；G3 测试已锁定「hint
  仅辅助不得升级」。此为保守实现，若评审裁定 hints 须以某种非权威
  形式参与判定，需先批 Resolver 适配点，不在本轮修复范围。

**RV-06 entity_type="unknown" 检索范围（entity_resolver.py 证据，
冻结模块只读核验）**
- `resolve()` 入口（entity_resolver.py:363）：
  `etype = c.get("entity_type") or "unknown"`——unknown 是**缺省
  兜底值**，所有未标注类型的候选（含生产链路 parse 产出）同此
  处理，非重验证专属特殊类型；
- 检索范围不受 unknown 影响：KB 匹配（:377–381）按 name 精确/
  双向包含检索，`idx.get(name)` / `_partial_hits(name, idx)` 与
  entity_type 无关；在线探测（:218–219）
  `kind = {"restaurant": ..., "hotel": ...}.get(entity_type,
  "attraction")`——unknown 落 **attraction（find_spot 泛检索）**，
  是最宽的默认分支，只选择探测 API 变体、不缩小范围；
- skip 条件（:366）仅对 `etype in ("category", "city")` 生效，
  unknown 走完整验证管线。结论：unknown 不会意外缩小或改变检索
  范围。

**RV-07 `_answer()` 到 Handler 的完整调用链（P1-7 证据）**
```
orchestrator 步骤 1.77（orchestrator.py:587）
  → _allow_handler_consume（:401–462）：五条件 AND 全过
    → session._answer(kind="poi", orig_ut)（:452）
      → dialogue.py:419–430 Session._answer：委托
        QA_HANDLERS[kind].handle(session, utterance)
        （handlers/__init__.py:35–42：QA_HANDLERS["poi"]=POIHandler）
      → poi_handler.py:76–88 POIHandler.handle：
        city = resolve_city(session, utterance)
              （_base.py:6–14：本句城市 > slots.destination
                > last_city > trip_dest）
        → intent.refine_intent(utterance, city) 细分路由
        → 各细分 Handler.handle(session, city, utterance)
          → Session._*_answer（复用既有业务方法，KB/真实工具）
```
- **如实语义声明**：fresh DecisionResult 在本链路中的作用是
  （a）五条件放行判定（:428–437）与（b）kind 选择
  （`_QUERY_INTENT_KIND[decision.intent]`，:440–441）；其 entity
  字段**未注入** Handler 执行——业务执行按批准设计（§4.4 方案甲
  「Handler 零改动」「不新增任何 Handler 接口」；§9.4 冻结结构
  「每个分支入口先看接入点 A 是否给出 DecisionResult：给了且
  action=execute → 走该 Handler 受控链路」）经既有入口重走
  Handler 链。即 DecisionResult 是**授权与选链依据**，不是向
  Handler 传数据的载体——这与 §9.4 原文「走该 Handler」字面一致；
  裁定所指「Handler 真正使用 fresh entity / 没有重新解析」在当前
  批准设计下**不可成立**（Handler 冻结、接口不新增），残留风险
  如实申报：业务执行基于原 utterance 重解析，其结果与 fresh
  confirmed 实体可能不一致（同一 utterance + 既有链路，理论一致
  但无强约束）。若评审裁定须把 fresh entity 注入执行依据，属
  Handler 接口变更，需单独评审授权，不在本轮修复范围。

### 16.3 P2 复核项证据

- **Context 生命周期**：ctx 每轮新建（_turn_impl 内构造），
  pending_decision/source/utterance 三载体仅在触点 1（步骤 1.75
  内 consume-ok 分支）写入、仅触点 2（步骤 1.77）读取；gate 短路
  轮（返回 Reply）直接 return，ctx 不跨轮存活，无泄漏路径；
- **DEFAULT_BATCHES 成员与映射**：decision.py:66
  `DEFAULT_BATCHES = ("poi_query", "poi_qa", "poi_location_query")`
  （冻结值）；orchestrator.py:65
  `_QUERY_INTENT_KIND = {b: "poi" for b in DEFAULT_BATCHES}` →
  QA_HANDLERS["poi"] = POIHandler（handlers/__init__.py:37）→
  POIHandler.handle 链（见 16.2 RV-07 调用链），规划类
  （plan_*）不在 DEFAULT_BATCHES，永不放行（④ 条件）；
- **Orchestrator 逻辑触点计数**：本轮修复 orchestrator.py 零 diff
  ——仍为批准的两处：触点 1 = consume-ok 分支调
  `_revalidate_after_consume`（:263），触点 2 = 步骤 1.77 调
  `_allow_handler_consume`（:587）。§10.5 配额不变。

### 16.4 验证结果

- test_pending_revalidator.py：71 → **78** 断言（+RV-04 双出现/
  顺序断言 2、+RV-01 内存唯一命中/恢复/时钟故障 6）；
- test_pending_sqlite.py：85 → **94** 断言（+RV-02 多行命中回滚/
  恢复唯一/恢复落库 3、+RV-03 时钟/连接层/事务中途异常 6）；
- test_pending_channel.py：308 不变（A7 集成断言同步强化为 RV-08
  口径——原始问句「哪家海贝皇大排档好」与候选名分别断言；仅加强
  条件，未放宽任何既有断言）；
- x3 连续全绿 + 全量回归 17 套件全绿（pending 60 / producer 58 /
  revalidator 78 / sqlite 94 / channel 308 / interceptor 150 /
  decision / orchestrator 33 / routing 49 / reply 28 / resolver /
  corpus v4 / parser 62 / extractor 36 / dialog 244 / server 52 /
  conversations 21）；
- 冻结零触碰：decision.py / interceptor / runtime / resolver /
  Handler / dialogue / producer / **orchestrator** diff 为空；
- 开关保持 0/0/0；P2.6 / P3 未授权未实施。

## 17. P2.5b-R1 验收补证包（复审 2026-11-19 六项 R1 清单，不改生产代码）

> 复审裁定：R1 实质性修复获认可（RV-01/02/04/08 通过），暂缓签
> PASS，要求先提交补证包（R1-01～R1-06）。本节全部为**只读证据
> 与仅测试文件的断言补强**，生产代码零 diff（git 复核见 17.6）。

### 17.1 R1-01 `_rollback()` 完整实现（pending_store_sqlite.py:254-259）

```python
@staticmethod
def _rollback(con: sqlite3.Connection) -> None:
    try:
        con.execute("ROLLBACK")
    except sqlite3.Error:
        pass
```

- 实现为 **`con.execute("ROLLBACK")` 语句路径**（不是
  `con.rollback()` 方法）——因此复审指出的测试代理对象
  （BoomMidTxnCon / RollbackBoomCon，未实现 `rollback()` 属性，
  仅透传 `execute(sql)`）**可以正常透传 ROLLBACK**，事务中途异常
  测试（RV-03③）确实执行了数据库级回滚，不存在 AttributeError
  假回滚；
- 该实现自 P2.2 起冻结使用（cancel/expire/consume 半成功防线同一
  助手），本阶段零 diff；
- 异常边界：`except sqlite3.Error` 覆盖真实 sqlite3 连接
  `con.execute()` 可能抛出的**完整异常族**（sqlite3 所有异常均
  派生自 sqlite3.Error）——对真实连接，ROLLBACK 失败不可能以非
  sqlite3.Error 形态抛出，契约 4 边界对真实连接是闭合的；
  唯一能出现非 sqlite3 异常的场景是连接对象本身非 sqlite3 实现
  （仅测试代理可构造），如实申报不掩饰；
- **新增故障测试**（test_pending_sqlite.py
  test_revalidation_sqlite_multirow_and_non_sqlite_fault 第 ④ 段）：
  UPDATE 0 行命中 → 进入 ROLLBACK 阶段 → 注入
  `sqlite3.OperationalError` → `_rollback` 内部吞掉 → 方法仍返回
  False、异常不传播。按复审要求，该测试**不断言数据状态**（回滚
  失败时数据是否残留属不确定状态，不伪装成断言），只锁定「返回
  False + 异常不传播」契约边界。

### 17.2 R1-02 回填三列完整断言（仅测试文件，已完成）

- test_pending_revalidator.py「[RV-01] 时钟异常 → 三列零写入」
  补齐 `revalidate_probe_status is None`（原先只断言两列，检查名
  与断言数不符的问题如实更正）；
- test_pending_sqlite.py「[RV-03] 时钟异常 → 三列零写入」同步补齐
  probe 列（同一问题在 sqlite 侧的镜像）；
- 修正后：revalidator 78/0、sqlite **95**/0（+1 回滚故障测试）。

### 17.3 R1-03 RV-05 hints 语义（对照原文 + 代码库先例，交评审裁定）

**批准文本三处原文（逐字）：**

1. PHASE5_DESIGN.md:378 HC-1 硬约束本体：城市归属按**意图选城市
   字段**（查询类 query_city、规划类 destination），`query_city ≠
   destination` 绝不判冲突——**HC-1 本体根本没有 hints 的位置**；
2. PHASE5_DESIGN.md:691（P2 前置风险项②原文）：「city_hints 注入
   边界——经 HC-1 意图城市字段路径传入验证上下文，**不写
   slots**；三级区分：用户明确 query_city（可参与判定）＞LLM
   city_hints（仅辅助线索，不得升级为用户明确指定）＞destination
   （规划目的地，LLM 提示绝不覆盖）」；
3. PHASE5_P25_PLAN.md §1.3/§10.3 与上引同文。

**两种读法的实质差异（如实呈现，不替评审关闭）：**

- 读法甲（只记录）：「仅辅助线索」修饰的是 hints 的**权限上限**
  （不得升级为用户明确指定、不覆盖 destination），未赋予其任何
  判定参与权 → 当前实现合规；
- 读法乙（低优先级参与检索）：三级是优先级链，hints 在无明确
  query_city 时可作为辅助期望城市参与 Resolver 判定（但仍不得
  升级、不覆盖 destination）→ 当前实现未完整落实。

**决定性代码事实（本轮只读核查新增）：**

- `grep -rn "\.city_hints"`（排除定义模块）：**全代码库零下游
  消费点**。LLM fallback 产出 hints（llm_entity_fallback.py:53，
  白名单仅收 query_city、destination 出现即越权丢弃
  :36/:187-190），但 parse 产出后**没有任何生产模块读取它**——
  orchestrator / producer / decision / resolver 均不消费。即
  「hints 辅助参与检索」在既有生产链路**从未存在过**；
- P2.5a 生产钩子的 resolve 上下文（orchestrator
  _pending_produce → resolve_ctx）同样不含 hints；
- Resolver 冻结（§10.5）：`resolve()` 无 hints 入参；为读法乙
  补通道须**先批最小适配点**。

**实现方立场（供裁定，不视为关闭）：** 读法甲与全代码库现状一致
（hints 自 P1.5 引入以来从未有消费通道），且 G3 测试已锁定「不得
升级」；读法乙是**新增行为**而非恢复既有语义。若评审裁定读法乙，
需要的最小变更是：resolve ctx 增加一个独立辅助键（如
`aux_city_hints`）+ Resolver 内部在无期望城市时仅用于**辅助归因
记录**（不参与命中判定）——属冻结区单点授权变更，按 §10.5 程序
单独申请，不在本轮修复范围。

### 17.4 R1-04 RV-06 检索范围只读调用链证据

**两探测分支的实际查询范围（poi_sources.py 只读核验）：**

- attraction 分支 `find_spot`（:363-402）与 restaurant/hotel 分支
  `search_amap_keyword`（:325-360）调用**同一个高德接口**
  `place/text`，参数形态一致：`keywords={name}`、`city={city}`、
  `citylimit=true`——**均无 types 过滤，查询范围 = 全城全品类
  关键词检索**。两个分支的 API 层检索范围完全相同；
- 差异仅在**结果后处理**，如实申报两点：
  1. `find_spot` 返回**单个最优**（完全相等 3.0 分 > 双向包含
     2.0 分，同分取离城心最近）；`search_amap_keyword` 返回最多
     5 条（resolver 传 limit=5）。因此 unknown 候选在 KB 未命中+
     在线探测路径下，同名多分店场景的 matches 数量为 1 而非 5——
     对单候选重验证，理论上存在「typed=ambiguous / unknown=取最优
     单条」的分流差异。方向性影响：unknown 路径**不会误判
     rejected**（空结果判定与分支无关），但 matches 多样性信息
     少于 typed 路径，如实申报不作掩饰；
  2. category 标注差异（`_amap_row(p, kind, ...)`）仅影响
     MatchRow.category 字段值，不影响命中/未命中判定；
- **unknown 是否可能遗漏餐饮/酒店实体**：不会——place/text 关键
  词检索对全品类 POI 生效，餐饮酒店实体同名即在结果内（复审问询
  的「attraction=最宽」表述修正为：attraction 与 restaurant/hotel
  分支**查询范围等宽**，差异在结果条数与标注）；
- **KB 与在线探测的合并逻辑**（entity_resolver.py:384-453）：
  **不合并**。KB 命中即短路（probe_status=not_applicable，
  matches=kb_hits）；KB 未命中才在线探测，`rc.matches =
  online_hits`（替换而非叠加）；主源命中即分流，复核源只对
  `etype ∈ {"attraction"}` 启用（:66 `_REVIEW_APPLICABLE_TYPES`，
  unknown → skipped/type_not_covered）。即 unknown 候选**永远走
  不到双源 rejected**（单源空证据 → unverified，保守方向）——
  这正是重验证测试 ②' 用例验证过的 Resolver 冻结行为。

### 17.5 R1-05 RV-07 fresh entity 一致性（设计契约材料，交评审裁定）

**批准文本原文（逐字）：**

1. PHASE5_DESIGN.md:638-642（§9.4 决策结果消费）：「**每个分支
   入口先看接入点 A 是否给出 DecisionResult**：给了且
   action=execute → **走该 Handler 受控链路**；clarify → 进消歧
   通道；reject_reply → 按措辞约束回复不执行；conservative →
   保守话术不执行；category_path / deferred → 交回」；
2. PHASE5_P25_PLAN.md §4.4 开头引述冻结要求：「走 §9.4 接入点 A
   既有消费路径，**Handler 拿到的仍是 DecisionResult，不新增任何
   Handler 接口**」；方案甲定版：「gate 产出 DecisionResult 后，
   在既有 if/elif 分支入口的 DecisionResult 消费点注入（该消费点
   结构 §9.4 已冻结），但放行条件为 TRAVEL_PENDING_CHANNEL=1 且
   来源标记=pending_consume，不读 TAKEOVER……**实现落点在
   orchestrator 分支入口消费点一处（只增判断条件），Handler 零
   改动**」。

**两种读法与内部一致性分析（不替评审裁定）：**

- 读法甲（授权+选链）：DecisionResult 在**分支入口**被消费——
  决定「是否放行、走哪条受控链路」；「走该 Handler」指进入该
  Handler 的既有业务链。entity 不需要进入 Handler 执行数据流。
  支持证据：§9.4 消费表全部动作（clarify/reject_reply/
  conservative/category_path/deferred）都是**路由决策**，无一涉及
  向 Handler 传实体数据；方案甲明文「Handler 零改动」；
- 读法乙（entity 为执行依据）：Handler 执行须以 decision.entity
  为数据源。与「不新增任何 Handler 接口」**直接冲突**——Handler
  现接口为 `(session, utterance)`（QA Handler 层自晴航 2.0 冻结），
  传实体必新增参数或改业务方法签名，两个冻结条款在读法乙下无法
  同时成立。

**实现方立场：** 读法甲是唯一使 §4.4 与 §9.4 两处冻结文本自洽的
解释；P3（DecisionEngine 接管）才是实体驱动执行的既定路线（
TAKEOVER=1 时 decide 产出直接消费），P2.5 的 confirmed 受控流转
是「授权+选链」性质的预演。**若评审裁定读法乙**，在不破坏冻结的
前提下存在一个可评审的方案选项：利用已批准的 ctx 载体
（pending_decision 本轮已在 ctx 上），由 **orchestrator 在
_allow_handler_consume 内**（触点 2 既有代码块内，不进 Handler）
对业务执行结果与 fresh entity 做一致性校验（如 resolve_city 结果
≠ decision.city → 丢弃执行结果回退确认话术）——落点仍在触点 2
一处、Handler 零改动；该方案改变触点 2 行为语义，须评审批准后才
实施，本轮不动。

### 17.6 R1-06 Git / 开关 / 回归原始证据

见随附原始输出存档 `P25bR1_evidence_logs.txt`（本轮生成），包含：

- 提交链：`git log --oneline -3`（3c1af27 = R1 修复，1fce3f3 =
  P2.5b 交付，83de409 = P2.5a 归档基线）；
- 冻结模块 diff 空证明：`git diff 1fce3f3..3c1af27 --name-only`
  全量改动清单（7 文件，冻结模块不在其中）+ 冻结路径逐项
  `git diff` 空输出；
- 三开关：`TRAVEL_PENDING_CHANNEL`（orchestrator.py:91，默认
  "0"）、`TRAVEL_DECISION_TAKEOVER`（decision.py:145，默认空串≠
  "1"）代码读取点证据；`TRAVEL_PENDING_TAKEOVER` **全代码库零
  读取点**（纯预留开关，语义未接线未改）；运行时
  `env | grep TRAVEL` 输出（三项均未设置 → 全部走默认关闭）；
- 全量回归 17 套件原始运行输出（非摘要）。

### 17.7 第二轮补证增补（2026-11-19，裁定 RV-05/RV-07=读法乙后的原始材料交付）

> 第二轮裁定要点已记录：RV-05 定为低优先级辅助上下文（适配点须按
> §10.5 单独申请）；RV-07 定为读法乙（触点 2 一致性校验方案须证明
> 能实际约束最终执行实体，不仅执行前比较一次）。本轮不改生产代码。

**R1-02 三列断言全量清点（本轮自查发现并补齐三处遗漏）：**

复查全部「三列」命名检查点后，除 17.2 已补的两处时钟异常测试外，
另发现三处检查名声称三列、实际断言不全（如实申报）：

- revalidator「[边界①] 不回填」：原仅断言 status+epoch → 补齐
  probe 列；
- revalidator「[边界④] 异常不回填」：原仅断言 status → 补齐
  probe+epoch 两列；
- revalidator「[skipped 防御] 不回填」：原仅断言 status → 补齐
  probe+epoch 两列。

补齐后，两测试文件中所有声称「三列/三字段」的检查点（revalidator
6 处：边界①/边界④/skipped/契约6/RV-01时钟/confirmed落位；sqlite
7 处：契约校验:468/三列落库:578/RV-02回滚:681/RV-02恢复:698/
RV-03时钟:725/RV-03中途:786/parity:629）均为三字段独立有效断言。
验证：revalidator 78/0、sqlite 95/0、channel 308/0（补强仅收紧，
断言数不变，无放宽）。

## 18. RV-07 独立设计方案：fresh DecisionResult 对最终执行实体的强约束（设计申请，零生产代码）

> 裁定（2026-11-19）：P2.5b-R1 补证工作有条件接受，P2.5b 不签
> PASS；先交 RV-07 独立设计方案，核心命题——**Handler 接口完全
> 不变的条件下，让 Handler 最终使用的实体受 fresh DecisionResult
> 强约束，而非仅调用前比较一次**。设计级证明之前不批触点 2 生产
> 代码修改。本节纯设计文档，无任何生产代码 diff。
>
> RV-05 裁定（读法乙：低优先级辅助上下文）已记录；其 Resolver
> 适配点按 §10.5 程序另案单独申请，不在本节范围。

### 18.1 命题与验收口径

A3（§10.3/§10.4 冻结）：执行路径必须能够保证 Handler 使用本轮
fresh DecisionResult/entity，并保留 pending_id、candidate_id 归因。

按裁定口径，「强约束」必须满足三个判据：

- **J1 实际使用约束**：被链路实际用于检索/作答的实体 = fresh
  entity，且该等式由机制构造保证（不是由执行前的一次预测比较）；
- **J2 分歧不可达**：即使链内发生任何形式的实体偏移，偏移结果
  既不能呈现给用户、也不能写入持久状态（KB）；
- **J3 归因保留**：pending_id / candidate_id / decision.reason 全程
  可溯。

### 18.2 执行链代码事实（本轮只读取证，全部给出处）

**实体解析点分布**（poi 白名单三类意图 → QA_HANDLERS["poi"] →
POIHandler → 细分链，最终全部委托 Session 业务方法）：

| 路径 | 实体如何产生 | 出处 |
|---|---|---|
| 点名景点/POI | `intent.spot_mention(utterance, city)` 从问句重解析 | dialogue.py:495 |
| 周边/路线 | `intent.nearby_route_ends` + `_resolve_spot(city, name)` 按名查库 | dialogue.py:585-594 |
| 餐厅 | `intent.food_keyword` 品类/片区词检索（非单一实体） | dialogue.py:1244 |
| 酒店 | 城市级在售酒店清单（无单一实体） | dialogue.py:1291 |
| 泛推荐兜底 | `intent.poi_answer(city)` 城市级列表 | poi_handler.py:88 |

**副作用清单**（链内真实发生的持久/状态写入）：

1. `session.last_city = city`（poi_handler.py:80）；
2. KB 写入三处：`autofetch.ensure_spot`（dialogue.py:967，点名
   POI 查不到时联网补录**入库**）、`get_data_service().ensure`
   （dialogue.py:787 / :1258 / :1291，餐厅/酒店定向补库）、
   `autofetch.ensure_city_data`（intent.py poi_answer 内，泛推荐
   兜底也会补库）；
3. LLM 调用（措辞组织 `_speak`、`unknown_spot_reply`）与
   websearch（`_general_answer`）。

**决定性事实——数据面不止一个咽喉点**：`agent.dispatch
("search_poi")`（mock_tools.py:301 / real_tools.py:1181 兼容入口）、
`DataService` 进程单例（services/__init__.py:32，
get_pois/get_restaurants/get_hotels/ensure）、`tools.search_poi`
（intent.py poi_answer 内独立引用）、`autofetch.*`、websearch/LLM
**五个互不隶属的访问面**。任何「运行时拦截面」方案都必须同拦全部
五面才完备。

**实体参数化入口已存在**：`_spot_qa(city, spot)`（dialogue.py:956）
与 `intent.spot_answer(city, spot, pois)`（intent.py:1042）中实体均
为**函数实参**；spot_answer 渲染品类自适应（restaurant 走人均口径，
intent.py:1076-1078），对 attraction/restaurant/hotel/unknown 四类
候选统一适用。

### 18.3 候选机制对比（含被否决项的否决理由）

| 机制 | 思路 | 判定 |
|---|---|---|
| A 事后验收丢弃 | 先走链，执行后核对回复含 fresh entity，不符即丢弃 | **否决**：KB 入库等副作用在核对前已发生（18.2 副作用清单），事后不可撤销——J2 不满足 |
| B 执行前预演等值门 | 用冻结解析器预演 spot_mention 等与 fresh 比对 | **否决**：即裁定明示不接受的「仅调用前比较一次」；且兜底链+副作用+LLM 使预演不可证完备 |
| C 数据面作用域代理 | 运行时替换五访问面为 fresh-entity 过滤代理 | 可行但**面大脆弱**：须同拦五面（18.2 决定性事实）、模块级全局替换、恢复窗口管理；列备选 |
| D Handler 接口扩展 | Handler 增实体参数 | **否决**：违反 §4.4/§9.4「Handler 零改动、不新增任何 Handler 接口」冻结 |
| **E 实体参数化直调**（推荐） | 控制路径不经 POIHandler 模糊链，**把 fresh entity 作为函数实参直调业务方法** | 见 18.4；实体按构造钉死，J1/J2 由构造保证 |

### 18.4 机制 E 详细设计（本次裁定申请的核心）

**落点**：触点 2 `_allow_handler_consume` 五条件全过分支的**内部
行为变更**——`session._answer(kind, orig_ut)` 改为实体参数化直调。
不新增逻辑触点（§10.5 配额不变），变更范围为触点 2 既有代码块内；
须评审批准触点 2 语义变更（即本申请）。

**执行策略**：

```
fresh entity = decision.entity（name/city，fresh resolve 产出）
utterance    = ctx.pending_decision_utterance（原问句，仅用于
               「周边」语义检测，不参与实体解析）

统一入口：session._spot_qa(fresh_city, fresh_name)
  ├─ 问句含「周边/附近」→ session._nearby_qa(fresh_city, fresh_name)
  │   （同为实体实参化入口，钉死性质不变）
  └─ 其余 → _spot_qa：KB 按 keyword=fresh_name 检索 →
      spot_answer(fresh_city, fresh_name, items) 渲染
      （品类自适应：restaurant 人均 / attraction 门票口径）
```

**三条设计不变量（设计级证明核心）**：

- **I1 实体钉死**：执行主题 = 实参 fresh_name。直调路径不存在
  `spot_mention`/`refine_intent` 等任何从问句重解析实体的步骤——
  实体进入执行的唯一通道是函数实参，而实参只来自 fresh
  DecisionResult（触点 2 五条件已锁定 decision 来源为可信内部流）；
- **I2 分歧不可达（按构造，非按检测）**：既然不存在重解析步骤，
  「内部实体 ≠ fresh entity」这一状态**在构造上不可发生**——不是
  被检测出来后拦截，而是根本无法产生。这直接回应裁定「不能仅在
  执行前比较一次、之后仍让 Handler 从原始问句重新解析」的否决点；
- **I3 失败保守 + 闭环验收**：`_spot_qa` 查不到 fresh entity →
  保守话术（dialogue.py:984-999，不编造）；触点 2 保留**回复一致
  性验收位**：fresh_name 未出现于回复、或出现 other_city 型歧义
  信号（dialogue.py:973-983）→ 丢弃执行结果、回退冻结确认话术——
  该验收是**兜底**而非唯一约束（与机制 A 的本质区别：A 里它是
  唯一约束且副作用已发生；E 里实体已被钉死，验收位只处理
  数据缺失类失败）。

**副作用处置**：

- KB autofetch：直调路径唯一可能的写入是针对 **fresh entity
  本身**的补录（查不到时）——语义对齐：fresh resolve 刚确认其
  存在，入库使 KB 与 fresh 结论一致，不存在「错误实体入库」路径
  （错误实体连检索词都进不去）；
- `session.last_city`：由 orchestrator 按 fresh_city 显式设置
  （语义与 POIHandler.handle:80 一致，落点在触点 2 内）；
- `_rest_fetched`/`_hotel_fetched` 会话缓存：直调不经过餐厅/酒店
  清单方法，不触发。

**归因保留（J3）**：DecisionResult.reason 已携带
`pending_revalidate:` 前缀归因（pending_id/candidate_id，五条件⑤
消费）；触点 2 执行前后 shadow 各记一条（execute_begin/execute_
done，含 pending_id/candidate_id/entity/reply 摘要），与既有
shadow 通道同构。

**与冻结条款的相容性（如实申报偏差点）**：

- Handler 类与接口：**零改动**（控制路径不调用 Handler，Handler
  于普通路由的冻结行为原样保留）；
- §9.4 消费表动作语义：execute→执行 / clarify/reject/conservative
  →不执行——不变；
- **偏差点**：§9.4「走该 Handler 受控链路」与 §10.1 D2 方案甲
  「经既有入口 session._answer」的字面（经 QA_HANDLERS 链）改为
  「直调该链最终委托的同一业务方法」。论证：A3 的实质是 fresh
  entity 成为**执行依据**——机制 E 以实参绑定在构造上满足；链式
  路径（当前实现）恰恰是裁定认定不满足 A3 的形态。该偏差即本次
  申请请求裁定的事项。

**残余风险如实申报**：

1. 细分措辞损失：原链的 refine_intent 细分（品类问句、关键词
   推荐）在 confirmed 消费轮不可达——但消费轮的用户问句本就是
   关于候选实体的单一问句（pending 由它产生），点名 QA 是主语义；
   「周边」变体已由 _nearby_qa 分支覆盖（见执行策略）；
2. `spot_answer` 的 `_speak` LLM 措辞非确定性——只影响措辞，
   不影响实体选择（实体已钉死），I1 不受影响；
3. `_spot_qa` 的 other_city 分支可能告知「实体在别的城市」——
   这是 fresh resolve 有效城市与 KB 归属的真实分歧，属 I3 验收位
   处理的保守失败，回退确认话术，绝不呈现错误归属。

### 18.5 裁定请求

1. 机制 E（实体参数化直调）是否批准为触点 2 语义变更的设计基础？
2. 若裁定必须字面保留「经 QA_HANDLERS 链」，则机制 C 为备选——
   但须接受五访问面同拦的复杂度与模块级全局替换的脆弱性（18.3）。
3. 批准后实施仍须开工申请（含测试设计：分歧不可达性用「构造性
   证明 + 注入式反证」双路测试），生产代码在本申请获批前零改动。

### 18.6 本节边界

纯设计文档；开关 0/0/0；P2.5b PASS 归档维持冻结；P2.6/P3 未授权。

## 19. P2.5a 独立整改实施记录（开工批准 2026-11-19，待代码验收）

> 开工批准（2026-11-19）：批准范围为 P2.5a 独立整改，按 §11.R3.6
> 增量清单实施——开发授权，非生产启用授权。交付须含独立 commit、
> 完整 diff、测试原始输出、channel=0 逐位对比证据。

### 19.1 增量 1+2：producer 补偿矩阵 L1 二线终结 + shadow 归因

- 落点：pending_producer.py 新增 `_second_line_terminal` 方法
  （producer 模块内，批准边界）；`_compensate_orphan` cancel 失败
  两个分支（异常 / ok=False）统一改走二线终结；
- 机制：cancel 失败 → **立即调用既有 `expire_pending`（契约④
  接口，独立事务）**：
  - expire 成功 → shadow 记 `produce_orphan_terminal_retry`
    （含孤儿 ID + cancel 失败原因）→ **交回原链路**（ProduceOutcome
    (None, None)）——孤儿已终态（expired），隔离效果由权威定位门
    `get_active_pending() 判 None` 直接可验证；
  - expire 失败（异常或 ok=False）→ shadow 记
    `produce_orphan_terminal_retry_failed`（含孤儿 ID + 双终结失败
    原因 + 残余窗口声明）→ 保守拦截，绝不交回原链路；
- **R4 冻结定性落实**：方法 docstring 与模块头同步改写——二线终结
  为尽力而为补偿；双终结失败时无即时消费隔离保证（L2 TTL 到期前
  仍可定位 / L3 仅持续故障生效 / L4 单槽只阻创建不阻消费），残余
  窗口由 §11.R3.2 生产开关冻结规避（channel=0 恒不进入本模块）；
- 不新增 store 状态、不新增接口、不触碰契约五方法（批准边界）。

### 19.2 增量 3：两种独立负向测试（R4 修正预期）

**producer 单元（test_pending_producer.py，58→72 断言）**：

- 测试桩升级：CountingStore 增 expire 计数/故障注入；
  `cancel_ok_override`/`expire_ok_override` 修正为**状态一致的
  纯失败模拟**（不实际取消/过期——原实现先真取消再覆盖 ok，状态
  与模拟结果不一致，如实申报此桩缺陷并已修正）；
- 新增 `test_p3r4_second_line_terminal`：①cancel 异常+expire 成功
  → 交回原链路 + expired + 定位判 None + cancel/expire 各恰一次
  + shadow 记 retry 含孤儿 ID；②cancel ok=False+expire 成功同①；
  ③双终结异常 → 保守拦截 + 孤儿仍 open + shadow 记 retry_failed
  （**未伪造隔离成功**）；③'双终结 ok=False 同③；
  **R4 场景二**：故障恢复后权威定位仍可达（残余窗口证据）+ shadow
  双通道孤儿身份 + TTL 到期后定位判 None（L2 最终清理边界可验证）
  ——**停在定位证据层，不进入消费流程**（裁定边界 1）；
- **既有测试预期同步修正**（新契约：保守拦截仅由双终结失败触达）：
  P3②/P3③'/R2②③④⑤/R3①②③/影子隔离 共 9 处单终结形态升级为
  双终结形态——原「cancel 失败 → 保守拦截」预期改为「二线终结
  成功 → 交回」或「双终结失败 → 保守拦截」，无断言放宽。

**集成（test_pending_channel.py，308→330 断言）**：

- 既有形态 C 升级双终结注入（原 cancel 单终结在 L1 下会交回）；
- 新增 `test_p25a_orphan_terminal_integration`：形态 D（单终结+
  expire 真实成功 → 交回 + 定位判 None + shadow retry）、形态 E
  （双终结 → pending_produce 短路 + Reply 契约 + 孤儿 open + 
  shadow retry_failed + lease 归还）、**R4 场景二**（定位证据层
  收口：权威定位可达 + shadow 双通道同 ID）、**R4 场景一**（下一
  轮 get_active_pending 持续故障 → 异常按 P2.4b 冻结边界向上传播，
  孤儿仍 open、零消费记录、lease finally 归还——fail-closed）；
- 既有 R2 集成形态 A/B（补偿成功交回）不变、仍通过。

### 19.3 增量 4：channel=0 真实 clarify 场景逐位对比

新增 `test_p25a_channel0_clarify_decision_bitwise`：

- **decide 层逐位**（真实 parse + 真实 ambiguous resolve 输入）：
  基线（CHANNEL 未设）/ channel=0 / channel=1+TAKEOVER=0 三台
  独立 DecisionEngine 同输入决策——channel=0 返回与基线一致
  （None），**影子记录三台逐位相等**（dict 全字段比较：intent/
  takeover/batch_enabled/action/reason/entity_text/statuses）——
  证明 D1 放行 hunk 零影子行为差；D1 单点放行（channel=1）返回
  clarify DecisionResult 且 pending=None（消歧载荷由 producer 从
  本轮 fresh matches 构建，绝不从历史/旧 Pending 恢复）、重放逐位
  一致（确定性构造）；
- **全链路逐位**：channel=0 下真实 ambiguous 问句 handle 输出与
  无 pending 基础设施基线会话**逐位一致**（str(reply) 相等）+
  producer/decider/manager 零构造。

### 19.4 增量 5：生产开关冻结条款保留确认

- §11.R3.2 四条全部有效：本轮 diff 不含 orchestrator.py 与
  decision.py（git 复核冻结清单全零 diff，两处开关读取点
  orchestrator.py:91 / decision.py:167 未触碰）；开关运行时
  0/0/0（测试全程 set_env 控制并 finally 恢复）；
- **裁定验收细节 2 落实**：仓库扫描不能替代生产部署核查——生产
  启用前的独立验收门槛（部署配置/启动脚本/容器环境变量/CI 注入
  全查）已在 §11.R4.3 记录，不阻碍本次开发验收。

### 19.5 验证结果

- 专项：producer **72**/0（58→72）、channel **330**/0（308→330）、
  pending 契约 60/0、interceptor 150/0（零 diff 无扰动）；
- 全量回归 **17 套件全绿**（exit=0 原始输出见
  P25aR4_evidence_logs.txt）；x3 连续复跑通过；
- 冻结模块零 diff：decision / interceptor / runtime / resolver /
  pending / pending_store_sqlite / revalidator / dialogue /
  orchestrator 全部为空；改动仅 3 文件（producer + 两测试）；
- 开关 0/0/0；P2.5b 相关代码零触碰；P2.6/P3 未授权。

---

## 20. P2.5a 独立整改最终验收关闭记录（2026-11-20 评审裁定）

### 20.1 裁定结果（评审原文归档）

**正式裁定：P2.5a 独立整改验收通过。验收对象：947883d。本次验收
仅覆盖 P2.5a，不向后延伸。**

评审归档用语（照录）：

> P2.5a 独立整改（commit 947883d）经设计边界、代码变更范围、补偿
> 逻辑、R4 风险场景、channel=0 真实 clarify 逐位对比及回归证据
> 审查，最终验收通过。P2.5a 阶段关闭。该结论不构成 P2.5b 验收
> 通过或生产启用授权。生产环境 TRAVEL_PENDING_CHANNEL 继续保持
> 关闭，后续阶段须另行申请。

### 20.2 最终验收清单（评审逐项裁定，全部 PASS）

L1 二线终结补偿 PASS / 双终结失败保守拦截 PASS / R4 故障恢复与
持续故障场景 PASS / 孤儿 Pending 残余风险表述 PASS / channel=0
真实 clarify 逐位对比 PASS / D1 channel=1 放行条件 PASS /
TAKEOVER=0 测试隔离 PASS / 冻结模块改动范围 PASS（依据已提交
diff 与证据记录）/ 17 套件单轮回归 PASS（依据已提交原始输出）/
独立测试恢复逻辑 PASS。

### 20.3 补证提交链（最终验收依据）

| commit | 内容 |
|---|---|
| `947883d` | P2.5a 独立整改实施（§19，四增量） |
| `ba211db` | 验收补证 r5：bitwise 测试显式固定 TRAVEL_DECISION_TAKEOVER（保存→五段固定 "0"+前置 assert→finally 恢复；单测 8/0 exit=0，双开关恢复 None） |
| `26a8cfe` | 验收补证 r6：两项健壮性建议（第二阶段环境断言移入 try；d1 空值防护）+ 两处既有测试墙钟时间炸弹修复（§4.2 LRU 两处 evaluate 传 now=clock；gate B1 s3 装具改 _ClockBoundInterceptor，因 _pending_gate 冻结不传 now=）；channel 330/0 exit=0、全量 17 套件 exit=0 ×17 |

时间炸弹根因备案：interceptor `_now_epoch(None)` 回退真实墙钟，
与 fixed_clock store 混源，600 秒重选窗口判定跨过 fixed 时间戳后
确定性失败——2026-11-19 的 330/0 系真实时间早于 fixed 时间戳、
负 diff 意外通过。残余：其余约 18 处 evaluate 未传 now 调用点
今日验证全绿，全量排查待独立授权。

### 20.4 阶段状态与冻结边界（评审五项强调，照录归档）

1. TRAVEL_PENDING_CHANNEL 在所有生产环境继续保持 0；
2. 不得通过临时环境变量、灰度配置或部署脚本绕过冻结；
3. P2.5a 通过**不代表**双终结失败后的孤儿 Pending 已获得即时
   隔离保证（§11.R4 定性持续有效：L1–L4 为尽力补偿与故障约束）；
4. 生产启用前，仍须独立核查部署配置、启动脚本、容器环境变量及
   CI 注入（§11.R4.3 启用前独立验收门槛）；
5. 不自动开始 P2.5b。

| 项 | 状态 |
|---|---|
| P2.5a 独立整改（947883d + ba211db + 26a8cfe） | PASS，关闭 |
| P2.5b 重验证闭环 | 未验收（维持 FAIL，待 RV-07 机制裁定） |
| P2.6 / P3 | 未授权 |
| TRAVEL_PENDING_CHANNEL | 0（不变） |
| TRAVEL_PENDING_TAKEOVER / TRAVEL_DECISION_TAKEOVER | 0 / 0（不变） |

### 20.5 待办移交

- P2.5b：维持 FAIL，待 §18 机制 E（RV-07 fresh entity 强约束）
  裁定后另行申请；不因本关闭自动启动；
- RV-05 city_hints Resolver 适配点：另案申请，未实施；
- 测试密闭性全量排查（约 18 处 evaluate 无 now= 调用点）：
  test-only，待独立授权。

---

## 21. P2.5b 整改独立开工申请（RV-07 机制 E 实施，2026-11-20）

> P2.5a 已于 §20 关闭（评审 2026-11-20 正式裁定 PASS）。按 §20.4
> 第 5 条「不自动开始 P2.5b」，本节为**独立开工申请**——纯设计
> 文档，生产代码在本申请获批前零改动。开关 0/0/0。

### 21.1 申请范围与背景

P2.5b（1fce3f3 → 3c1af27 → 9b25f24 → 64ef36f）维持 FAIL。R1/R2
两轮后 RV-01/02/03/04/06/08 已闭合，剩余**唯一实质阻断 = RV-07**：
裁定读法乙——执行路径必须保证 Handler 使用 fresh
DecisionResult/entity 实际约束最终执行实体，当前 `_answer` 链式
实现（实体由问句重解析）不满足 A3/J1。

§18 已提交机制 E 设计（实体参数化直调，I1/I2/I3 三不变量），
**尚未获裁定**。本申请按 §18.5.3 预告的程序提交实施申请，请求
评审**一轮复审完成两项裁定**：

1. **机制 E 批准为触点 2 语义变更的设计基础**（§18.5.1，含
   §18.4 申报偏差点：§9.4「走该 Handler 受控链路」与 §10.1 D2
   方案甲「经既有入口 session._answer」的字面改为「直调该链最终
   委托的同一业务方法」）；
2. **本节增量清单开工批准**。

若裁定否决机制 E、必须字面保留 QA_HANDLERS 链，则按 §18.5.2 转
机制 C 备选（五访问面同拦）另行申请——本申请自动作废，不实施。

### 21.2 实施增量清单（授权边界）

**增量 1（唯一生产代码改动）：orchestrator.py 触点 2 内部行为
变更**——`_allow_handler_consume` 五条件全过分支，现有：

```python
text = session._answer(kind, orig_ut)
```

改为机制 E 直调（伪码，按 §18.4 执行策略）：

```python
ename/ecity 来自 decision.entity（五条件⑤+非缺失已锁定来源）
session.last_city = ecity（显式设置，语义同 POIHandler.handle:80）
shadow: execute_begin（pending_id/candidate_id/entity）
if re.search(r"周边|附近|周围", orig_ut):
    text = session._nearby_qa(ecity, ename)      # 实体实参化入口
else:
    text = session._spot_qa(ecity, ename)        # 实体实参化入口
shadow: execute_done（reply 摘要）
# I3 验收位（兜底）：fresh_name 未出现于 text 或 other_city 歧义
# 信号 → 丢弃执行结果、回退 confirm()；text 为空 → 回退 confirm()
# （语义同现有 F3：不因消歧绕过前置）
return answer_reply(session, text)
```

- 五条件 AND、confirm() 回退、异常边界（except → confirm）、
  `pending_decision is None → None` 等既有结构**逐位保留**；
- 周边检测沿用 dialogue.py:493 既有正则语义（`orig_ut` 仅作此
  语义检测，绝不参与实体解析——I1）；
- §10.5 配额不变：仍是触点 2 同一代码块内部，无新逻辑触点；
- 除本块外 orchestrator 其余部分、全部 Handler 类与接口、
  dialogue.py、decision/interceptor/runtime/resolver/pending/
  双 store/revalidator/producer **零改动**（冻结清单照旧）。

**增量 2（测试）**：构造性证明 + 注入式反证双路（§18.5.3 要求）：

- **构造性证明（J1/J3）**：
  - 捕获桩：monkeypatch `agent.dispatch`/`session._spot_qa`/
    `session._nearby_qa`，断言检索 keyword/city 实参 ==
    decision.entity 的 fresh name/city（实体唯一通道 = 实参）；
  - `session.last_city == fresh_city`；
  - DecisionResult.reason 归因前缀保留（pending_id/candidate_id）；
  - shadow execute_begin/done 双条记录含 pending_id/candidate_id；
  - confirmed 消费轮回复含 fresh entity 内容（A3 主断言延续）。
- **注入式反证（I2 构造不可达）**：
  - 向消费轮注入「会误导问句重解析」的输入变形（如问句含其他
    景点名/别名，使原链 spot_mention 必然产出不同实体），断言
    直调路径产出的检索实参仍为 fresh_name——证明不存在「从问句
    重解析实体」步骤，偏移状态构造上不可发生；
  - 反证对照：普通路由（非消费轮）同输入走原链重解析——证明
    差异仅由机制 E 引入且被限定在触点 2 内。
- **失败保守（I3）**：KB 查不到 fresh entity → 保守话术或验收位
  回退确认话术；other_city 分支信号 → 回退确认，绝不呈现错误
  归属；`_spot_qa` 抛异常 → 既有 except → confirm 路径不变。
- **开关冻结**：channel=0 全链路逐位回归不破坏（零构造零旁路
  语义不变）；五条件任一不满足 → confirm 路径逐位不变。

**增量 3（受影响既有测试升级，零断言放宽）**：现有以
`session._answer` 为桩接缝的测试改为对接缝 `_spot_qa`/
`_nearby_qa`/`agent.dispatch` 断言（直调后 `_answer` 不再是消费
轮执行入口）。实施时逐一清点并在实施记录中列全表，初步涉及：

| 测试函数 | 影响 |
|---|---|
| test_p25b_a3_confirmed_controlled_flow | 主断言升级为直调契约（fresh 实参捕获 + 归因保留） |
| test_p25b_a4_plan_confirmed_no_plan_write | 规划类 confirm 路径零触碰，预期仅接缝名称调整 |
| test_p25b_a6_a7_unverified_rejected | unverified/rejected 不进执行，预期仅接缝名称调整 |
| test_p25b_d1_d2_d3_robustness | 注入失败/前置缺失回退 confirm 路径保留，接缝调整 |
| test_p25b_f2_f3_negative | F3「前置未接住」（s2._answer→None 桩）改桩 `_spot_qa` 空/异常 |
| test_p25a_channel0_clarify_decision_bitwise | 仅 channel=0 路径，预期零改动（直调仅 channel=1 消费轮可达） |

原则：断言只收紧不放宽；桩缺陷如实申报（沿用 §19 惯例）。

**增量 4（文档）**：§22 实施记录（diff 清单、证据日志、冻结
零触碰 diff 证明）。

### 21.3 验收标准（对应 §18.1 判据）

| 判据 | 验收测试 |
|---|---|
| J1 实际使用约束（构造保证） | 捕获桩断言检索/渲染实参恒等 fresh entity（增量 2 构造性证明组） |
| J2 分歧不可达（按构造） | 注入式反证组：误导性问句输入下直调实参仍为 fresh；对照普通路由证明差异被触点 2 限定 |
| J3 归因保留 | reason 前缀 + shadow begin/done 双通道断言 |
| 既有契约不回退 | 五条件/confirm 回退/异常边界/空回复 F3 全部保留断言；channel=0 逐位；全量 17 套件 exit=0 |

验收证据按惯例：完整 diff 原文 + 测试实跑原始输出 + 退出码；
生产配置启用前核查门槛（§11.R4.3）继续有效，不在本阶段。

### 21.4 冻结与边界声明

- 开关 0/0/0 贯穿全程；生产启用禁止；
- Handler 类与接口零改动（机制 D 否决理由持续有效）；
- P2.6/P3 不在申请范围；RV-05 Resolver 适配点另案（不在本申请）；
- 测试密闭性全量排查（约 18 处 evaluate 无 now=）不在本申请；
- 本申请获批即锁定上述增量清单为唯一授权范围，任何超范围改动
  须重新申请。

### 21.5 裁定请求

1. §18.4 机制 E（含申报偏差点）是否批准为设计基础？
2. 本节增量清单是否批准开工？
3. 若机制 E 否决：是否转机制 C 另案，或维持 P2.5b FAIL 状态
   等待其他机制提案？（三项择一裁定即可，本申请不预设立场。）

---

## 22. P2.5b 整改实施记录（机制 E，按 §21 批准范围交付，2026-11-20）

> 评审裁定（2026-11-20）：机制 E 批准为设计基础（含 §18.5.1 语义
> 调整）；§21.2 增量 1～4 批准独立开工，附五项实施约束；机制 C
> 不启用。本节按批准范围交付，**完成后暂停待独立代码验收**。
> 核心原则照录：**批准绕过 _answer 的问句重新解析，不等于批准
> 绕过原有业务安全链路。**

### 22.1 授权范围落实

- 改动仅 2 文件：`orchestrator.py`（触点 2 内部，+91/-33 行中的
  生产部分）+ `tests/test_pending_channel.py`（接缝升级 + 新增
  双路测试）；
- 冻结清单零触碰（git diff 复核）：pending_interceptor /
  pending_runtime / entity_resolver / pending.py / 双 store /
  pending_revalidator / pending_producer / dialogue.py / 全部
  Handler 类与接口 / decision.py **零 diff**；
- 开关 0/0/0 贯穿；P2.6/P3 未授权；RV-05 适配点与密闭性排查
  不在本次范围（未实施）。

### 22.2 增量 1：触点 2 内部行为变更（唯一生产改动）

`_allow_handler_consume` 五条件全过分支：

```python
# 旧（RV-07 缺陷实证：实参是原始问句，实体由链内重解析）
text = session._answer(kind, orig_ut)

# 新（机制 E：fresh entity 经函数实参成为唯一执行实体）
session.last_city = ecity            # 语义同 POIHandler.handle:80
shadow execute_begin（entity + reason 归因）
if orig_ut 含 {周边,附近,周围,附件}:     # dialogue.py:497 同一
    text = session._nearby_qa(ecity, ename)   # 关键词集等价判定
else:
    text = session._spot_qa(ecity, ename)
shadow execute_done（entity + reason + reply 摘要）
# I3 验收位：ename not in text 或 "不在{ecity}" in text → confirm
```

逐位保留：五条件 AND 原文 / confirm() 回退 / `decision is None →
None` 透传 / except → confirm（警告文案不变）/ F3 空文本回退 /
`kind is None or not orig_ut` 前置（仅扩展 `or not ecity`，警告
文案同步为 kind/utterance/city——见等价性表）。周边检测用
`any(k in orig_ut for k in (...))` 与 dialogue.py:497 正则
`周边|附近|周围|附件` 字面等价（纯字面量 alternation，零新增
import，diff 严格限于触点 2 块）。

### 22.3 业务方法等价性证明表（评审约束 1——本次批准核心条件）

| 检查对象 | 证明 |
|---|---|
| `_spot_qa` 是原链同一业务方法 | 原链：POIHandler → AttractionHandler（`_method="_attraction_answer"`，poi_handler.py）→ `_attraction_answer` → dialogue.py:499 `return self._spot_qa(city, spot)`——直调即该最终委托点，实参形态 (city, spot) 相同，fresh 值取代 resolve_city/spot_mention 产出 |
| `_nearby_qa` 是原链同一业务方法 | 原链：dialogue.py:497-498 周边分支 `return self._nearby_qa(city, spot)`——直调即该最终委托点，周边判定复用同一关键词集 |
| 权限检查 | QA 执行链**不存在独立权限层**（全仓核查：moderation/auth 不在 QA 路径任何环节）；消费轮权限闸 = 五条件②可信内部流（`pending_decision_source == "pending_consume"`，绝不由用户输入/LLM 伪造）——直调前后同一 |
| GuardPipeline | 位于 handle **步骤 6**（方案修改守卫，guard.py），仅规划类触达；QA 类正常路径在步骤 5 已返回、从不执行 Guard——直调未绕过任何实际会执行的检查；规划类经五条件④（DEFAULT_BATCHES 白名单）永久排除在消费执行之外（A4 测试锁定） |
| 参数校验 | ename 非空 = 五条件⑤后置 `bool(entity.get("name"))`（既有）；**新增 ecity 缺失前置 → confirm** = 原链 POIHandler.handle「resolve_city 为空 → 泛推荐兜底」的等价映射，消费轮语义下更保守（绝不泛答） |
| Handler 前置条件 | F3 语义保留：业务方法返回 None/空 → confirm（测试 I3-e）；异常 → confirm（测试 I3-c/d）；原链细分兜底链（refine_intent 顺序）按获批偏差绕过（§18.4 申报：消费轮问句本为关于候选实体的单一问句，点名 QA 为主语义，周边变体保留） |

**last_city 语义验证（约束 3）**：设置时机与 POIHandler.handle:80
一致（链执行前设置，异常路径不回滚——原链同语义）；本轮查询
使用 fresh city（检索实参断言锁定）；值 = fresh confirmed 实体
归属城市——对比原链 resolve_city 在消费轮的实际输出（问句无城市
时落 slots.destination/旧 last_city，本轮 A3/D2 旧实现实证为
空城泛推荐），机制 E 以 fresh 为准正是 RV-07 修复本意，不存在
「错误继承」新增路径；附近与普通 POI 共用同一 ecity 实参。

**I3 约束 4 落实**：fresh_name-in-reply 仅为辅助验收位；other_city
信号 = dialogue.py `_spot_qa` 冻结话术模板的确定性结构——精确子串
匹配「不在{ecity}」，非宽泛正则、非回复文本猜测；误报方向 = 保守
confirm（安全侧）。

**保证范围限定（评审 2026-11-20 整改项 3 后修订）**：本检测**已
覆盖当前冻结模板中明确包含「不在」的冲突话术**（S1 note 形态 /
S2 跨城同名 message / S4 fallback，穷举表见 §22.7.2）；**其他模板
形式不在本次检测保证范围内**——其中 S3（autofetch.py:937 重定向
失败 message，不含「不在{city}」）为已申报已知缺口：该分支不呈现
偏移归属、不编造位置（J2 安全方向仍成立），但 I3 保守回退不触发，
修复需生产改动、按评审边界本轮不实施。S1 由 I3-b 注入测试锁定，
S2/S3/S4 由 `test_p25b_other_city_signal_coverage` 锁定（S3 断言
显式记录「当前不回退」的边界事实，不构成覆盖保证）。

**如实申报**：shadow 复用既有 `_shadow_gate` 通道（stage="gate"，
verdict="allow_execute"，action=execute_begin/done），pending_id
字段为 None、归因经 detail=reason（含 pending_id/candidate_id
全文）承载——与既有通道同构，未新增 shadow 基础设施。

### 22.4 增量 2/3：测试设计与既有测试升级（零断言放宽）

**新增双路测试**（§21.2，channel 330→352）：

1. `test_p25b_mechanism_e_fresh_entity_enforcement`：
   - 构造性证明：捕获 `agent.dispatch` 最终检索实参——
     Entity_Decision == Entity_BusinessMethod（_spot_qa 实参）==
     Entity_FinalLookup（dispatch keyword/city）三段恒等 fresh
     entity；last_city==fresh_city；回复含 fresh 实体且非确认
     话术；shadow begin/done 恰各一条且 begin 携带归因 reason；
     reason 含 pending_id/candidate_id/pending_revalidate 前缀；
     **轮 1（创建 pending）完成后清空捕获缓冲**，使恒等断言只
     可能来自消费轮（评审整改项 1）；
   - 注入式反证：误导性原始问句「上海迪士尼门票多少钱」注入
     Pending——消费路径最终检索实参仍为 fresh entity；断言
     **误导实体未进入消费路径的 POI 检索 keyword**（范围限定为
     本次捕获的 `search_poi` 调用，不宣称覆盖全部检索通道——
     评审整改项 2）；偏移内容零呈现（I2/J2）；**反证对照**：同一
     问句经普通路由真实重解析出「迪士尼」（spot_mention 实证），
     分歧机制真实存在、反证有证明力，差异被限定于触点 2。
2. `test_p25b_mechanism_e_failure_conservative`（I3 组）：
   a) KB 无结果 → 保守话术（含 fresh 实体、不编造、非确认话术）；
   b) other_city 注入 → 验收位回退确认话术 + 「位于上海」「不在
   南京」零呈现（J2）+ 警告记录；c) `_spot_qa` 异常 → 异常边界
   回退 + 警告；d) `_nearby_qa` 异常（原问句含周边语义）→ 同一
   边界；e) 空文本 → F3 回退；f) 五条件③不满足（action=clarify
   直调注入）→ confirm 语义不变。
3. `test_p25b_other_city_signal_coverage`（整改项 3 新增）：
   other_city 四形态穷举与保证边界锁定，S3 已知缺口显式断言，
   见 §22.7.2。

**既有测试接缝升级明细**（增量 3）：

| 测试 | 变更 | 断言强度 |
|---|---|---|
| A3 | `_count_answers` 计数 → 受控直调桩 + 实参捕获；「恰一次入口调用」→「恰一次直调且实参恒等 fresh entity」 | **收紧**（新增实参恒等断言） |
| D2 | 同 A3（受控桩 + 恒等断言） | **收紧** |
| A4/A6/A7/D1/D3/F2 | `_count_answers` 装具重定位（包装 `_spot_qa`，零调用断言不变） | 等价 |
| F3 | 桩接缝 `s2._answer→None` → `s2._spot_qa→None` | 等价 |
| `_preset_orc` | 增 `ut` 可选参数（默认原文不变，反证装具用） | 向后兼容 |

**如实申报（接缝事实变化）**：旧实现在消费轮的实际执行内容为
POIHandler「resolve_city 空城 → `intent.poi_answer("")` 泛推荐」
——即 J1 缺陷的行为实证；机制 E 后为实体化执行，故 A3/D2 以
受控桩替换真实 KB 链（避免 mock KB 无南京数据触发 autofetch 的
网络不确定性），最终检索实参的真实性由专项测试 1 的 dispatch
捕获链证明。

### 22.5 验证结果

- 专项：channel **356**/0（实施时 330→352；整改项 1/2 为断言
  收紧、整改项 3 新增 4 项断言 → 356），exit=0；完整原始输出与
  逐套件命令见 P25bR2_raw_evidence.txt / P25bR2_logs/；
- 全量回归 **17 套件全绿 exit=0**（原始 stdout 逐套件留档，见
  P25bR2_raw_evidence.txt「套件原始输出」段）；
- 冻结模块零 diff（逐文件 `git diff --stat` 原始输出见
  P25bR2_raw_evidence.txt「冻结清单逐项核验」段）；开关 0/0/0。

### 22.6 状态

实施 commit：**9eba91f**（基线 1e9e77e）；整改 commit 紧随其后
（评审 2026-11-20 CONDITIONAL PASS 五项整改，见 §22.7）。**整改
交付后暂停，等待独立代码验收**；P2.6/P3 未授权；生产启用禁止
（启用前部署配置审计为独立门槛）。

### 22.7 验收整改记录（评审 2026-11-20 CONDITIONAL PASS）

裁定：机制 E 核心实现方向 / 五条件及回退语义 / fresh entity 实参
约束 = 初步通过；构造性证明需补强、误导实体反证需收窄、other_city
全路径覆盖待核实、回归与冻结证据待原始材料、交付文档一致性需修正。

#### 22.7.1 整改项 1：消费轮检索捕获隔离

轮 1（创建 pending）完成后、消费轮 `handle(s, "第1个")` 之前调用
`dispatch_calls.clear()`。理由：轮 1 的建链过程可能已产生符合
fresh entity 的检索记录，若不清空，`any(...)` 恒等断言可能由轮 1
残留记录满足而使消费轮缺陷漏检。整改后该断言只可能由消费轮捕获
满足。反证段与对照段原本已在各自轮前清空，保持不变。

#### 22.7.2 整改项 2：反证断言收窄

原断言「误导实体从未进入任何检索」改为「误导实体未进入消费路径的
POI 检索 keyword（范围限定：本次捕获的 `search_poi` 调用）」。原因：
捕获桩只拦截 `agent.dispatch("search_poi", ...)`，无法证明全部检索
通道；收窄后断言与捕获范围严格对应，不宣称超出证据范围的结论。

#### 22.7.3 整改项 3：other_city 全部返回形态核对（穷举表）

机制 E 消费路径只经 `_spot_qa`：`_nearby_qa` 在 KB 无该点坐标时
委托 `_spot_qa`（dialogue.py:1352-1354）；`_nearby_gate` 的 other_city
分支（dialogue.py:718-721）不在直调路径内。

| 形态 | 来源 | 返回文本 | 含「不在{city}」 | 验收位 |
|---|---|---|---|---|
| S1 | dialogue.py:976-981（`res.poi` 存在） | `「{name}」位于{true_city}，不在{city}。` + spot_answer | 是 | 回退（I3-b 覆盖） |
| S2 | autofetch.py:858 跨城同名 → `res.message` 透传（dialogue.py:982-983） | `「{name}」资料库里记在{other}，不在{city}；…` | 是 | 回退（新增覆盖） |
| S3 | autofetch.py:937 重定向失败 → `res.message` 透传 | `「{name}」的位置离{city}很远，我没能核实到它在{city}` | **否** | **不回退（已知缺口）** |
| S4 | dialogue.py:982-983 fallback（message 缺失） | `「{spot}」不在{city}一带，我没法按{city}来介绍。` | 是 | 回退（新增覆盖） |

注：autofetch.py:933（重定向成功）虽为 other_city，但其返回含 `poi`，
在 `_spot_qa` 内被 S1 分支吸收，message 不参与输出，故不构成独立
形态。

**S3 已知缺口定性（如实申报）**：该分支不呈现偏移城市名、不呈现
偏移实体、不编造位置——J2「偏移归属零呈现」仍成立；但 I3 保守
回退不触发，用户会看到「离{city}很远、没能核实」的原样答复。
修复需在触点 2 内增加信号来源（生产改动），按评审边界
「不批准扩大生产改动范围」本轮不实施，待独立授权。

**测试覆盖**：`test_p25b_other_city_signal_coverage`（4 断言）锁定
S2/S3/S4，S1 由既有 I3-b 锁定；S3 断言显式记录「当前不回退」，
作用为防止日后被误读为全覆盖，不构成覆盖保证。

#### 22.7.4 整改项 4：交付文档一致性

§22.6 原文「commit：待提交（基线 1e9e77e）」→ 修正为实施 commit
**9eba91f**；§22.5 证据文件名由误写的 P25bR1_evidence_logs.txt 修正
为 P25bME_evidence_logs.txt（该名曾用于上一轮补证），并更新为整改后
的 P25bR2 系列工件。

#### 22.7.5 整改项 5：原始证据补齐

新增 P25bR2_raw_evidence.txt：逐套件实际执行命令、完整原始 stdout
与退出码；`git show --format=fuller --stat` 全文（实施 commit 与整改
commit 各一份）；冻结清单 **10 项（9 个文件 + `handlers/` 目录级
覆盖）**的逐项 `git diff --stat` 原始输出；另含触点 2 范围证明
（全部 hunk 的 old 行号落在 402–461，处于 `_allow_handler_consume`
区间 [401, 527) 内，且 diff 中无任何 `class`/`def` 定义行增删——
9 个 hunk 系 docstring 重写与函数体分段所致，不代表触及范围扩大）。
整改范围严格限于测试文件 + 本文档：`git diff --stat 9eba91f 7e168f5
-- src/` 为空。

**一致性自查（如实申报）**：整改 commit 信息中「11-file frozen-list」
与本节实际执行的 10 项（9 文件 + handlers 目录）不符，系表述笔误；
以本节与 P25bR2_raw_evidence.txt 的逐项输出为准（未增删任何被核验
对象，仅计数口径差异：目录级覆盖 handlers 下 9 个 Handler 文件）。
