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
-- ③ 消费结果记录 INSERT INTO pending_consumption(
--      pending_id, candidate_id, consumed_at, selected_name, selected_city)
COMMIT;
```

要点：BEGIN IMMEDIATE 下「候选校验 + 条件更新 + 结果记录」原子；
**并发语义与 P1.5 Barrier 测试完全一致**——迁移后重跑同一并发
测试（两线程 + Barrier 打同一 SQLite 文件，恰好一次成功）作为
迁移验收断言。`create` 撞部分唯一索引 → 捕获 IntegrityError →
按规则 8 拒绝。时钟注入改为 SQL 参数比较（`expires_at > ?`）。

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
2. **新问题**：parse 产出与 pending 无关的具体意图（新实体候选、
   天气、路线框架、泛化品类）→ 先 `cancel_pending`（规则 3，
   不误消费）再走原链路。
3. **无法判定**（「那家店怎么样」「还是它」但快照有多个候选）：
   不消费，引用 pending 重述选项（「您是指①南京店还是②上海店？」）。

冲突时保守：同时命中选择线索与新问题信号 → 按 3 处理。
**判定为影子可观测**：每轮把「三路判定结果 + 命中依据」写
shadow_log，供接管前归因。

## 4. 过期 / 取消 / 目的地变化处理

| 情形 | 处理 | 规则 |
|---|---|---|
| 到期后选择 | 拦截点发现 `expires_at <= now` → 显式 `expire_pending` 登记为 expired，如实回复「该选择已过期，请重新提问」，不消费 | 规则 5 |
| 用户取消 | 「取消/算了/不选了」→ `cancel_pending` → cancelled，pending 清除 | 规则 2 |
| 目的地变化 | parse 检出本轮 destination ≠ pending 所属上下文目的地 → 旧 pending 取消（reason=destination_changed），候选城市归属已变须重走 EntityResolver，本轮按新问题处理 | 规则 4 |
| 重复选择 | 第一次已 consumed；再次选择 → 拒绝并如实告知已确认过 | 规则 6 |
| 新消歧需求 | 旧 pending 非 open 时才允许 create（部分唯一索引兜底） | 规则 8 |

## 5. 消歧成功后如何安全交给原 Handler

**铁律：消歧成功 ≠ 授权修改行程**（身份确定，行程动作仍由原有
权限与角色规则决定）。分两步走：

1. **身份确定**：原子消费成功 → 取回所选 PendingCandidate →
   以其 name/city 重走 EntityResolver（新鲜验证，不用陈旧 matches）
   → 得到 fresh confirmed 实体。
2. **按 original_intent 分流**：
   - **查询类**（poi_qa/poi_query）：直接以 confirmed 实体进入
     既有 Handler（与用户当场点名该实体等价），正常应答；
   - **规划类**（plan_modify 等）：**只回复确认话术**（「已确认是
     南京这家（海贝皇大排档·夫子庙店）」），不写行程；用户随后
     明确说「排这家」才按既有 PlanHandler 链路处理（HC-5 写权限
     闸、destination 复核全部照旧）——身份确定与行程写入是两次
     用户动作；
   - negated/remove 候选：永不进入消费路径（HC-4，消费只对
     query/must_visit 语境的 ambiguous 产生，且出口不带 must_visit）。

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
| pending.py 改造 | PendingCandidate 冻结结构 + 接口兼容 + 不可变负向测试 |
| pending_store_sqlite.py（或并入 pending.py） | 五方法 SQLite 实现，条件更新 + 单事务 + 部分唯一索引 |
| pending_interceptor.py | 三路判定 + 过期/取消/目的地变化处理 + 影子记录 |
| 接入点 | orchestrator 拦截点一处（开关控制），Handler 零改动 |
| 测试 | test_pending.py 扩展（不可变 + SQLite 并发迁移断言）、test_pending_interceptor.py 新增（三路判定 8+ 场景、八步核心场景端到端、三个拒绝场景、旧链路零改变） |

实施顺序：①PendingCandidate 改造 → ②SQLite store（含并发迁移
验收）→ ③拦截器 + 判定规则 → ④接入点（开关关）→ ⑤影子验证积累
→ ⑥（经批准后）开关开启灰度。每步独立 commit。

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
