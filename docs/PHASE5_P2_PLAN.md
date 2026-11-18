# 第五阶段 P2 开工申请与实施方案：运行时消歧通道

> 申请日期：2026-11-18。设计基线：`a5ddf42`（P1.5 验收记录补录）。
> 冻结基线链：P0 `7f01ea6` / P1 `4b43c03` / P1.5 `169bb6e`。
> **本文档为开工申请，未经批准不写任何业务代码。**
> 设计依据：docs/PHASE5_DESIGN.md v3.2.1 §7（九条消费规则）、
> §10 P2 前置风险项（快照不可变 / SQLite 并发语义 / city_hints
> 边界 / 八步核心验收场景）。

---

## 0. 申请范围（一件事）

**让用户能够通过多轮对话选择正确实体，并安全地消费 Pending。**

不做：P3 查询类接管批次扩容、LLM 接入、Composer/路线/预算修改、
DEP-001。P2 结束后决策层仍默认纯影子，唯一新增的运行时通道是
「消歧选择消费」，且由独立开关控制（默认关）。

## 1. 不可变候选结构（兼容现有 Pending 接口）

**现状缺陷**：`PendingDisambiguation(frozen=True)` 只防字段重赋值，
`candidates: tuple[dict, ...]` 内部 dict 可被外部改动
（`p.candidates[0]["name"] = …` 生效）。

**改造方案**（pending.py 内部改造，**五方法签名不变**）：

```python
@dataclass(frozen=True)
class PendingCandidate:
    candidate_id: str
    display_order: int          # 冻结序号（规则 1）
    entity_id: str | None
    name: str
    city: str
    address: str | None
```

- `PendingDisambiguation.candidates` 类型改为 `tuple[PendingCandidate, …]`。
- **接口兼容**：
  - `create_pending(candidates: list[dict])` 签名不变，store 内部把
    dict 逐个转成 PendingCandidate（校验六个冻结字段、1..N 序号、
    ID 唯一）；调用方（P2 的决策层）继续传 dict 快照；
  - `snapshot_candidate_ids()` 返回值不变（tuple[str, …]）；
  - `candidate_by_id()` 改为返回 `PendingCandidate`（frozen，天然
    不可变），另保留 `candidate_as_dict(id)` 供序列化；
  - `as_dict()` 输出形状不变（list[dict]），DecisionResult.to_dict
    与影子记录零感知。
- **负向测试**：断言 `p.candidates[0].name = "x"` 抛
  FrozenInstanceError；`create_pending` 收到的 dict 列表被调用方
  事后修改，不影响已创建快照（深拷贝边界）。

## 2. SQLite 表结构、事务边界与并发消费

**存储选型**：沿用 `TRAVEL_APP_DB`（账号库已有连接管理），新增
独立表，不与现有表交叉。内存 `PendingStore` 保留为测试桩与无 DB
降级路径。

```sql
CREATE TABLE IF NOT EXISTS pending_disambiguation (
    pending_id         TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    original_utterance TEXT NOT NULL,
    original_intent    TEXT NOT NULL,
    original_role      TEXT NOT NULL,
    candidates_json    TEXT NOT NULL,     -- 序列化的冻结候选 tuple
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    status             TEXT NOT NULL      -- open/consumed/cancelled/expired
);
-- 规则 8：单 Session 单 active——数据库级强制，不靠应用层判断
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_open
    ON pending_disambiguation(session_id) WHERE status = 'open';
-- 消费结果记录（阻断项②③的归因与重复选择识别数据源）
CREATE TABLE IF NOT EXISTS pending_consumption (
    pending_id              TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    candidate_id            TEXT NOT NULL,
    consumed_at             TEXT NOT NULL,
    original_intent         TEXT NOT NULL,
    selected_name           TEXT NOT NULL,
    selected_city           TEXT,
    revalidate_status       TEXT,          -- 重验证结果（confirmed/…）
    revalidate_probe_status TEXT,
    revalidated_at          TEXT
);
```

**consume_pending 事务边界**（P1.5 验收点，单事务完成三件事）：

```sql
BEGIN IMMEDIATE;                       -- 写锁串行化并发消费
-- ① 候选校验：读 candidates_json，candidate_id 不在快照内 → ROLLBACK 拒绝
-- ② 条件更新（受影响行数=1 才算成功）：
UPDATE pending_disambiguation
   SET status = 'consumed'
 WHERE pending_id = ? AND session_id = ?
   AND status = 'open' AND expires_at > ?;
-- rowcount != 1 → ROLLBACK，返回失败原因（已消费/已过期/不存在）
-- ③ 消费结果记录 INSERT INTO pending_consumption(...)
COMMIT;
```

要点：BEGIN IMMEDIATE 下「候选校验 + 条件更新 + 结果记录」原子；
**并发语义与 P1.5 Barrier 测试完全一致**——迁移后重跑同一并发
测试（两线程 + Barrier 打同一 SQLite 文件，恰好一次成功）作为
迁移验收断言。`create` 撞部分唯一索引 → 捕获 IntegrityError →
按规则 8 拒绝。时钟注入改为 SQL 参数比较（`expires_at > ?`）。

**create_pending 事务（阻断项 ①：到期永不阻塞新 Pending）**：

```sql
BEGIN IMMEDIATE;
-- ① 自动过期：该 Session 已过期的 open 记录 → expired
UPDATE pending_disambiguation
   SET status = 'expired'
 WHERE session_id = ? AND status = 'open' AND expires_at <= ?;
-- ② 插入新记录（部分唯一索引兜底并发创建）
INSERT INTO pending_disambiguation(...) VALUES (...);
COMMIT;
```

内存桩同语义：锁内先自动过期再查重。

## 3. 多轮对话：选择候选 vs 新问题（Pending 拦截判定）

**接入方式**：新增受控拦截点，位于 `parse_utterance` 之后、既有
intent 分支之前（§9.4 接入点 A 的前置检查，只增不改）；开关
`TRAVEL_PENDING_CHANNEL=1`（默认 0=纯影子，仅记录判定结果）。

**三路判定**（顺序固定，逐条进测试）：

1. **选择线索**（utterance 与 active pending 快照对得上）：
   - 序号指代：「第一个」「第2个」「选2」「就要2号」→ display_order；
   - 快照内名称匹配：utterance 文本与快照某候选 name 精确/包含匹配
     （**只在快照内匹配，快照外绝不猜**，规则 9）；
   - 城市限定指代：「南京那家」「上海这家」→ 快照内按 city 过滤后
     唯一命中才可消费，多命中追问不消费。
2. **新问题**：parse 产出与 pending 无关的**明确**具体意图（新实体
   候选命中、明确意图分类如天气/路线框架/泛化品类）→ 先
   `cancel_pending`（规则 3，不误消费）再走原链路。
   **判定必须有足够依据**：解析层产生的**模糊候选**（纯城市候选、
   泛化品类词、角色未定）**不得**单独触发旧 Pending 取消——
   「南京那家怎么样」优先按选择线索判定；负向测试三条：明确新
   查询（介绍一下总统府）→取消、明确新意图（明天天气）→取消、
   模糊候选（仅城市词）→**不取消**；
3. **无法判定**（「那家店怎么样」「还是它」但快照有多个候选）：
   不消费，引用 pending 重述选项（「您是指①南京店还是②上海店？」）。

冲突时保守：同时命中选择线索与新问题信号 → 按 3 处理。
**判定为影子可观测**：每轮把「三路判定结果 + 命中依据」写
shadow_log，供接管前归因。

## 4. 过期 / 取消 / 目的地变化处理

| 情形 | 处理 | 规则 |
|---|---|---|
| 到期后选择 | 拦截点发现 `expires_at <= now` → 显式 `expire_pending` 登记为 expired，如实回复「该选择已过期，请重新提问」，不消费 | 规则 5 |
| **到期但无请求触发显式 expire（阻断项 ①）** | `create_pending` **同一事务内**先把该 Session 已过期的 open 记录条件更新为 expired（`UPDATE … WHERE session_id=? AND status='open' AND expires_at <= ?`），再 INSERT 新记录；内存桩在锁内先自动过期再查重。并发创建仍由部分唯一索引兜底。**到期永不阻塞新 Pending** | 规则 5+8 |
| 用户取消 | 「取消/算了/不选了」→ `cancel_pending` → cancelled，pending 清除 | 规则 2 |
| 目的地变化 | parse 检出本轮 destination ≠ pending 所属上下文目的地 → 旧 pending 取消（reason=destination_changed），候选城市归属已变须重走 EntityResolver，本轮按新问题处理 | 规则 4 |
| 重复选择 | 见 §3.1 重复选择识别机制；识别为重复 → 如实告知已确认过，不执行任何业务 | 规则 6 |
| 新消歧需求 | 旧 pending 非 open 时才允许 create（部分唯一索引兜底） | 规则 8 |

**新增测试**：A 到期但无显式 expire → 创建 B 成功（内存桩与
SQLite 双实现都要过）；并发 create 恰好一个成功（索引兜底）。

## 4.1 重复选择识别机制（阻断项 ③）

**问题**：消费后 `get_active_pending()` 返回 None，第二次「选第一个」
凭什么识别为重复选择而不是新问题？

**机制**：`pending_consumption` 表（消费结果记录）天然保存了最近
一次消费的归因信息（session_id / pending_id / candidate_id /
consumed_at / original_intent / selected_name / selected_city /
revalidate_status）。拦截点在**无 active pending** 时：

1. 查该 Session 最近一条 consumption 记录（拦截器内部查询路径，
   不新增公共存储接口）；
2. **同时满足**以下条件才识别为重复选择：
   - utterance 是**明确的重复选择语境**：纯选择线索（序号指代、
     「就这家」「还是它」），**不含**新实体候选、不含新意图信号；
   - 在合理时间窗口内（默认 10 分钟，`TRAVEL_PENDING_RESELECT_
     WINDOW` 可配）；
3. 命中 → 如实告知「刚刚已确认过『XX（南京店）』」，不执行任何
   业务，不改任何状态；
4. **无法可靠识别**（语境模糊 / 超窗口 / utterance 含新候选）→
   **不得猜测用户指哪个历史候选，直接交回原有业务链路**。

**新增测试**：窗口内纯选择线索 → 告知已确认不执行；超窗口 → 按
新问题走原链路；「选第一个 + 新实体」→ 新问题；跨 Session 的
consumption 记录互不可见。

## 5. 消歧成功后如何安全交给原 Handler

**铁律：消歧成功 ≠ 授权修改行程**（身份确定，行程动作仍由原有
权限与角色规则决定）。

**消费与重验证是两个独立阶段，失败契约显式闭合（阻断项 ②）**：

> **原子消费成功只代表用户选择已被接受，不代表实体重新验证成功，
> 更不代表 Handler 已经执行。**

**重验证 = 全新走 `entity_resolver.resolve()` 决策链**（KB 索引
重建 + 探测按当时开关执行），**不复用**旧 Pending 的 matches 或
旧 DecisionResult。允许命中 Resolver 既有进程级探测缓存（缓存
证据有效期沿用 P0 既有策略）；KB-only 影子模式下重验证受当时
开关约束，probe_status 如实标注。fresh 定义进测试（mock KB 变化
→ 重验证结果跟着变）。

**重验证失败分支契约**（原 Pending 保持 consumed，**绝不静默
恢复 open**；任何后续 Pending 用**新 pending_id + 新快照**）：

| 重验证结果 | 后续处理 |
|---|---|
| confirmed | 允许按 original_intent 进入对应流程（查询类直进 Handler；规划类只回确认话术不写行程） |
| ambiguous | **不执行 Handler**；由 fresh matches 生成**新候选快照**并创建**新 Pending**（新 pending_id、新 TTL）重新请求选择 |
| unverified | 不执行 Handler；保守回复（「暂时查不到可靠信息，可告知城市或地址」），**绝不判定为 rejected** |
| rejected | 不执行 Handler；按「规定数据源与检索范围内没有找到」话术回复，保留原始表达 |

**归因要求**：所有失败分支保留原 pending_id、candidate_id 与
重验证结果（pending_consumption 增加 revalidate_status/
revalidate_probe_status 列；shadow_log 同步记录；DecisionResult.
reason 携带全链归因）。

| 消费成功后的流转 | 说明 |
|---|---|
| confirmed → Handler | 身份确定；行程动作仍走原有权限与角色规则 |
| negated/remove 候选 | 永不进入消费路径（HC-4），出口不带 must_visit |

消费后的 DecisionResult（action=execute，entity=fresh confirmed，
reason 含 pending_id 与 candidate_id 全链归因）走 §9.4 接入点 A
既有消费路径，Handler 拿到的仍是 DecisionResult，**不新增任何
Handler 接口**。

## 6. 旧业务链路不受影响的验证

1. **开关默认关**：`TRAVEL_PENDING_CHANNEL=0` 时拦截点即时返回
   None（仅 shadow 记录），全量行为与 P1.5 基线逐位一致；
2. **深拷贝对比测试**（沿用 P1 验收点 9 方法）：拦截前后
   context/slots/ParseResult/resolved 深拷贝零改变；
3. **负向语料**：开关开启但 utterance 与 pending 无关 → 判定为
   新问题，旧链路输出不变；快照外 ID/过期/重复选择 → 拒绝路径
   输出可控；
4. **全套件回归**：v1–v4 语料 + decision/pending/adapter/resolver
   /parser/extractor/plan_quality 等全绿；
5. **静态边界检查**：orchestrator/handlers 的 diff 仅限拦截点一处
   插入，无其他改动（git diff 断言写入验收报告）。

## 7. 交付物与测试清单

| 交付 | 内容 |
|---|---|
| pending.py 改造 | PendingCandidate 冻结结构 + 接口兼容 + 不可变负向测试 + create 自动过期（内存桩） |
| pending_store_sqlite.py（或并入 pending.py） | 五方法 SQLite 实现，条件更新 + 单事务 + 部分唯一索引 + create 自动过期同事务 |
| pending_interceptor.py | 三路判定 + 过期/取消/目的地变化 + **重复选择识别（§4.1）** + 影子记录 |
| 接入点 | orchestrator 拦截点一处（开关控制），Handler 零改动 |
| 测试 | test_pending.py 扩展、test_pending_interceptor.py 新增（见下） |

**测试清单（评审修订版，含三个建议边界）**：
- 不可变负向：frozen 赋值抛异常；调用方改入参 dict 不影响快照
- 到期不阻塞：A 到期无显式 expire → create B 成功（内存桩 + SQLite 双实现）；并发 create 恰好一个成功
- 重验证失败分支：confirmed → 进流程 / ambiguous → 新 Pending（新 ID 新快照，原 Pending 保持 consumed）/ unverified → 保守不 rejected / rejected → 拒绝话术；失败归因含原 pending_id/candidate_id/重验证结果
- fresh 定义：mock KB 变化 → 重验证结果跟随变化（不复用旧 matches）
- 重复选择：窗口内纯选择线索 → 告知已确认不执行；超窗口 → 新问题；含新候选 → 新问题；跨 Session consumption 互不可见
- Session 隔离：**Session B 用相同 pending_id 请求消费 → 拒绝且 A 的 Pending 状态不变**（不能只靠 pending_id 全局唯一）
- 新问题判定依据：明确查询 → 取消；明确新意图 → 取消；**模糊候选（仅城市词）→ 不取消**
- 旧链路零改变：开关关深拷贝对比；负向语料；git diff 单点断言

实施顺序（评审定版 P2.1–P2.6，每步独立 commit）：

| 步骤 | 工作 | 验收重点 |
|---|---|---|
| P2.1 | PendingCandidate 不可变改造 | 深拷贝边界、不可变负向测试 |
| P2.2 | SQLite Store | 事务、条件更新、部分唯一索引、并发迁移、create 自动过期、Session 隔离 |
| P2.3 | PendingInterceptor | 三路判定、取消、过期、**重复选择识别** |
| P2.4 | Orchestrator 单点接入 | 开关关闭时旧链路零变化（**即使完成也不默认打开 TRAVEL_PENDING_CHANNEL**） |
| P2.5 | 消歧运行时验证 | 八步完整链路、**重验证失败四分支** |
| P2.6 | 影子观察 | 记录决策依据，不自动灰度开启 |

每步独立 commit；回归门槛：v1–v4 语料 + 全套件绿 + 当步新语料绿。

## 8. 已知风险

- 拦截点在 `parse_utterance` 之后：解析层仍会把「南京那家」解析出
  城市候选等副产物，判定须以 pending 快照优先，避免旧链路先产出
  泛推荐——接入位置与执行顺序在实施时以实测确认，必要时拦截点
  提前到 intent 路由入口最前（仍只增不改）；
- SQLite 时钟注入与 P1.5 内存桩的语义差异（字符串时间比较 vs
  datetime 对象），迁移测试需覆盖跨秒边界用例；
- `TRAVEL_APP_DB` 为多用户库，pending 必须带 session_id 隔离，
  验收含跨 Session 隔离负向测试。

**申请结论**：以上方案确认后申请开工，从 ①PendingCandidate 冻结
结构改造开始，每步独立提交、独立回归、独立验收，不提前进入 P3。

## 9. P2.2 实施记录（SQLite PendingStore）

> 实施基线：P2.1 `589ebe4` 验收通过。交付 `pending_store_sqlite.py`
> + `tests/test_pending_sqlite.py`；内存桩保留为测试桩与无 DB 降级
> 路径。P2.2 八条验收要求逐条落点见模块 docstring。

**相对本方案 §2 的契约偏差（P2.2 验收要求 1 定版）**：时间列由
TEXT ISO 字符串升级为 **INTEGER UTC Unix epoch**——created_at /
expires_at / consumed_at / revalidated_at 库内一律 epoch 整数，
所有过期判定与比较只用 `expires_at > ?` / `expires_at <= ?` 整数
比较，绝不做字符串时间比较；`PendingDisambiguation` 数据类的
created_at/expires_at 仍是 ISO 字符串（v3.2.1 冻结契约），由
epoch 以 UTC 派生，仅展示用途。注入时钟返回 naive datetime 按
UTC 解释（测试确定性）。`pending_consumption.pending_id` 设为
PRIMARY KEY（同一 pending 只能成功消费一次），同时充当半成功
回滚的失败诱因测试锚点。

**内存桩同语义补丁**：`PendingStore.create_pending` 锁内先自动
过期（open 且已到期 → expired）再查重——到期永不阻塞新 Pending
（阻断项①），与 SQLite 实现双实现对拍锁定（`_lifecycle` 逐位
对比脚本）。

### 9.1 P2.3 验收补丁记录（消费记录字段契约与时间边界）

**消费记录字段契约（定版：七字段）**：`_latest_consumption()`
双实现统一返回 `session_id / pending_id / candidate_id /
consumed_at / original_intent / selected_name / selected_city`。
`selected_city` 并非未批准扩展——`pending_consumption.selected_city
TEXT` 列自本方案 §2 的冻结建表语句起就存在；其**用途**：
① `duplicate_select` 话术与影子归因（如实告知「刚刚已确认过
『XX（南京店）』」，§4.1 要求话术带城市店名）；② 影子证据字段，
供接管前归因观测。**不参与**重复选择三条件判定（判定只用纯选择
线索 + 新实体/新意图信号 + 时间窗口），因此不存在城市误关联的
误判路径。内存桩与 SQLite 均返回相同七字段（字段级对拍锁定）；
未成功消费不产生记录；按当前 Session 查询、不跨 Session 读取。

**过期边界语义（定版）**：`expires_at` 为有效期**截止时刻**，
`now == expires_at` 即过期——内存桩与 SQLite 的 `_is_expired`
统一用 `>=`，SQL 条件更新用 `expires_at <= ?`（自动过期）与
`expires_at > ?`（可消费），三处口径一致。

**异常时间值失败关闭（定版）**：无法解析的 `expires_at` 一律
**保守视为已过期**——内存桩与 SQLite 的 `_is_expired` 返回 True；
SQLite 消费路径对不可比较值同样拒绝；`get_active_pending` 与
`create_pending` 自动过期均带 Python 侧兜底，异常时间行不被消费
也不阻塞新 Pending。绝不让异常时间值导致 Pending 被继续消费。
