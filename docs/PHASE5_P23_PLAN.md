# 第五阶段 P2.3 开工申请：多轮消歧拦截器（PendingInterceptor）

> 申请日期：2026-11-18。P2.2 验收通过（`67843fc`）。
> 设计依据：docs/PHASE5_P2_PLAN.md §3/§3.1/§4/§4.1、
> docs/PHASE5_DESIGN.md v3.2.1 §7 九条消费规则。
> **本文档为开工申请，未经批准不写任何业务代码。**

---

## 0. 申请范围（一件事）

**在 parse 之后、intent 分支之前插入的判定点，独立成模块交付：
判断用户下一轮的话是「选择候选 / 明确新问题 / 无法判定」，并对
选择、取消、过期、目的地变化、重复选择给出确定性的处置结论。**

**明确不做（本轮禁止清单）**：
- Orchestrator 接入与拦截点接线（P2.4）；
- Handler 任何改动；
- 打开 `TRAVEL_PENDING_CHANNEL`（保持默认 0）；
- 扩大 DecisionEngine 接管范围；
- 进入 P2.5 / P3。

模块交付形态：`src/travel_agent/pending_interceptor.py` +
`tests/test_pending_interceptor.py`，纯判定+受控执行，可被
P2.4 以一处调用接入。

## 1. 接口设计（两步显式：先判定后执行）

```python
class PendingInterceptor:
    def __init__(self, store, *, shadow_sink=None,
                 reselect_window: int = 600) -> None: ...
    def evaluate(self, utterance: str, parse_result=None, *,
                 session_pending_id: str | None = None,
                 now=None) -> InterceptVerdict:   # 纯判定，零副作用
    def act(self, verdict: InterceptVerdict) -> _ConsumeOutcome | None:
        # 按 verdict 执行 store 迁移（consume/cancel/expire）
```

- `store`：内存桩 `PendingStore` 或 `SQLitePendingStore`
  （P2.2 已保证双实现状态语义一致，拦截器对两者透明）；
- `session_pending_id`：由调用方从会话状态传入（创建 pending 时
  已记录），拦截器用 `get_pending_by_id` 取全量状态（含 open 但
  已到期）；未传则退回 `get_active_pending()`；
- **`evaluate()` 纯判定**：不改 store、不改 parse_result/context
  （负向测试做深拷贝对比）；只写影子记录；
- **`act()` 执行**：仅按 verdict 类别调用 store 五方法，失败如实
  返回 outcome 不抛（store 已保证原子性）；
- 影子记录沿用 DecisionEngine 模式：`interceptor.shadow_log`
  追加 dict（utterance / verdict / rule_hit / evidence /
  pending_id / candidate_id），`shadow_sink` 回调异常隔离。

### InterceptVerdict 冻结契约（九类，互斥）

| verdict | 含义 | store 动作（act） |
|---|---|---|
| `consume` | 可靠选择定位到唯一候选 | `consume_pending` |
| `cancel_new_question` | 明确新问题（有足够依据） | `cancel_pending` → 交回原链路 |
| `hand_back` | 模糊表达，依据不足：**不取消不消费**，pending 保持 open，交回原链路 | 无 |
| `restate` | 选择语境但无法唯一定位候选 | 无（引用 pending 重述选项） |
| `expired_reply` | 选择线索但 pending 已到期 | `expire_pending` 显式登记 → 如实回复「该选择已过期，请重新提问」 |
| `cancel_explicit` | 用户取消（取消/算了/不选了） | `cancel_pending` |
| `cancel_destination_changed` | 本句明确新目的地（规划语义）≠ 快照城市集合 | `cancel_pending`（影子 reason=destination_changed） |
| `duplicate_select` | 无 active + 窗口内纯选择线索（§4.1） | **无任何 store 动作**，零状态变化 |
| `none` | 无 active pending 且非重复选择 | 无，正常走原链路 |

每条 verdict 附 `evidence` dict（命中规则名、命中文本、
display_order/city/candidate_id），供影子归因与测试断言。

## 2. 三路判定（顺序固定，保守优先）

判定顺序如下；**冲突时一律落保守侧**（同时命中选择线索与新问题
信号 → `restate`）：

```
0. 无 active/会话 pending → 重复选择识别（§4.1）→ 否则 none
1. 目的地变化检查（最高优先级，见 2.4）
2. 用户取消话术 → cancel_explicit
3. 选择线索解析（2.1）→ 命中唯一 → consume / 到期则 expired_reply
4. 新问题依据检查（2.3）→ 依据充分 → cancel_new_question
5. 其余（含纯选择语气但无法定位）→ restate 或 hand_back
```

### 2.1 选择线索（只在快照内匹配，快照外绝不猜，规则 9）

| 线索 | 规则 | 唯一性 |
|---|---|---|
| 序号指代 | 「第一个/第1个/第2家/选2/就要2号/2号」→ display_order；锚定词（第/选/号/家）+数字，**日期/人数数字不误触发**（负向：「两天」「3个人」） | 序号在 1..N 内才有效 |
| 纯选择语气 | 「就这家/还是它/就它」等无定位词的确认语气 | 快照恰 1 条候选 → 该候选；多条 → restate |
| 名称匹配 | utterance 含快照候选 name，或候选 name 含 utterance 主体（精确/包含双向） | 多候选同时命中 → restate |
| 城市限定指代 | 「南京那家/上海这家」：城市词 + 指示词（那家/这家/那个） | 按城市过滤快照，唯一命中才 consume，多命中 restate |

`consume` 的 candidate_id 必须 ∈ `snapshot_candidate_ids()`（规则 9）。

### 2.2 用户提醒的保守红线（本轮验收重点）

- 「南京那家怎么样」：城市词 + 指示词 → **选择线索**，不是新问题；
- 「南京有什么好吃的」：仅城市词 + 泛化品类（好吃的），**无指示词、
  无快照名称、无新实体候选 → `hand_back`，绝不取消旧 pending**；
- 重复选择识别（§4.1）**必须同时满足**「纯选择线索 + 窗口内 +
  不含新实体候选/新意图信号」，只凭时间窗口命中不成立。

### 2.3 新问题判定依据（模糊候选不得单独触发取消）

判 `cancel_new_question` 须有**结构化解析依据**，二者之一：
- parse_result.intent 为明确新意图（weather / route / plan_create
  / plan_modify 等，非泛化承接）；
- parse_result.poi_candidates 含**具名实体候选**（非纯城市词、
  非泛化品类词、非角色未定的模糊信号）。

负向三例（逐条进测试）：明确新查询「介绍一下总统府」→ 取消；
明确新意图「明天天气」→ 取消；**模糊候选（仅城市词）→ 不取消**
（hand_back）。

### 2.4 目的地变化（优先级最高，规则 4）

- 判定条件：`parse_result.destination_new` 非空（本句**新说**的
  目的地，且具规划语义 destination_writable）**且** 不在快照候选
  城市集合内 → `cancel_destination_changed`；
- **设计决策（需评审确认）**：若新目的地恰在快照城市集合内
  （如快照 {南京,上海}，用户说「改去南京了，排海贝皇大排档」），
  保守起见**仍按目的地变化处理**——候选城市归属已变化，须重走
  EntityResolver（fresh matches），不做名称匹配消费；负向测试
  锁定该取舍；
- 目的地变化判定**优先于**名称/序号匹配，避免「改去 X + 旧名称」
  被误消费。

## 3. 重复选择识别（§4.1，无 active pending 时）

数据源：消费结果记录。**内部查询路径不进公共存储契约**：两个
store 均补一个下划线内部方法 `_latest_consumption()`（返回最近
一条 dict 或 None）——SQLite 读 `pending_consumption`（按
consumed_at 取最新）；内存桩在 `consume_pending` 成功时同步记录
（与 SQLite 同语义，对拍脚本扩展覆盖）。

识别为 `duplicate_select` 须**同时满足**：
1. utterance 是纯选择线索（序号指代或「就这家/还是它」），不含
   新实体候选、不含新意图信号；
2. 消费发生在 `TRAVEL_PENDING_RESELECT_WINDOW`（默认 600 秒，
   epoch 整数比较）内；
3. 所指序号能映射到该已消费 pending 快照内的候选。

命中 → 如实告知「刚刚已确认过『XX（城市店）』」，**不执行业务、
不改任何状态**；任一条件不满足 → `none` 交回原链路，**绝不猜测
用户指哪个历史候选**。

## 4. 与 P2.2 的时间契约衔接

- 全部时间判定用 UTC epoch 整数比较（注入时钟语义与
  SQLitePendingStore 一致：naive datetime 按 UTC 解释）；
- 到期判定：pending open 但 `expires_at <= now` → 选择线索走
  `expired_reply`（act 里显式 expire 登记）；取消话术仍可
  cancel（open → cancelled，store 已支持）。

## 5. 测试清单（test_pending_interceptor.py，目标 ≥45 条）

| 组 | 用例 |
|---|---|
| 选择线索 | 序号指代 4 变体各 consume 正确候选；序号越界 → restate；名称精确/包含匹配；名称歧义 → restate；城市限定唯一命中 consume；城市多命中 restate；纯选择语气单候选 consume、多候选 restate；快照外名称绝不猜（「总统府那家」→ hand_back） |
| 保守红线 | 「南京那家怎么样」→ 选择线索；「南京有什么好吃的」→ hand_back 不取消；模糊候选不触发 cancel（负向三例） |
| 新问题 | 明确新查询 → cancel_new_question；明确新意图 → cancel_new_question；cancel 后 get_active 为 None 且可新建 |
| 目的地变化 | 新目的地不在快照城市 → cancel_destination_changed；**新目的地在快照城市集合内 → 仍按目的地变化（保守取舍负向锁定）**；目的地变化优先于名称匹配 |
| 过期/取消 | 到期后选择 → expired_reply + store 登记 expired + 不消费；到期后取消 → cancelled；取消话术 → cancel_explicit |
| 重复选择 | 窗口内纯选择线索 → duplicate_select（零状态变化）；超窗口 → none；「选第一个+新实体」→ none 交回原链路；序号映射到已消费快照候选 → 告知含名称与城市；跨 Session consumption 互不可见（SQLite 双 store） |
| 纯度与契约 | evaluate 深拷贝对比（parse_result/context 零改变）；影子记录字段齐全且 sink 异常隔离；act 失败如实返回不抛；内存桩与 SQLite 双实现对拍（选择/取消/过期三链路）；无 pending 时全程 none 零干扰 |

## 6. 交付物与验收门槛

| 交付 | 内容 |
|---|---|
| pending_interceptor.py | InterceptVerdict 冻结契约 + 九类判定 + act 执行 + 影子记录 |
| pending.py / pending_store_sqlite.py | 各补 `_latest_consumption()` 内部查询（不进公共契约）+ 内存桩消费记录 |
| test_pending_interceptor.py | §5 清单 |
| 回归 | pending 41 / pending sqlite 45 / decision 51 / adapter 32 / resolver 79 / corpus v4 50 / parser 62 / extractor 36 全绿 + 当步新语料绿 |

每步独立 commit；**不接线、不开关、不动 Orchestrator/Handler**；
P2.4 未授权不启动。

## 7. 已知风险与边界

- 拦截器不读环境变量开关（`TRAVEL_PENDING_CHANNEL` 的启用判断属
  P2.4 接线），模块本身可被单测直驱；
- 序号指代正则只覆盖列举的锚定模式（第/选/号/家），歧义输入一律
  落 restate/hand_back，宁漏勿错；
- 名称包含匹配可能过宽（如候选名含通用词），以「唯一命中才消费」
  与影子可观测兜底，接管前靠影子日志校准；
- 拦截器不做 LLM 判定（P3 边界），纯规则 + 解析结构依据。

**申请结论**：以上方案确认后开工，独立提交、独立回归、独立验收。

## 8. P2.3 验收补丁记录（评审意见闭合）

1. **hand_back / restate 边界修正（冻结语义对齐）**：restate
   只留给**显式重述请求**（「再说一遍有哪些店」「重新列一下」
   等模式）；「南京那家怎么样」「南京有什么好吃的」「那家店值得
   去吗」及选择歧义/越界、取消与选择冲突一律 **hand_back**——
   保守交回原链路，pending 保持 open，不消费不取消。九类 verdict
   契约不变，仅收紧 restate 触发条件。
2. **消费记录字段契约定版为七字段**（含 selected_city）：依据与
   用途见 PHASE5_P2_PLAN.md §9.1——selected_city 是 §2 冻结建表
   语句中的既有列，用于 duplicate_select 话术与影子归因，不参与
   判定条件，双实现字段级对拍锁定。
3. **过期边界**：`expires_at == now` 即过期（>=），内存桩/SQLite/
   SQL 三处口径一致；**异常时间值失败关闭**（保守视为已过期，
   绝不让 Pending 被继续消费），见 §9.1。

## 9. P2.3 复验补丁记录（R1/R2）

1. **R1 消费原子性（内存桩）**：`consume_pending` 的可失败前置
   步骤（候选读取、时钟获取/epoch 转换、消费记录构造）全部提前
   到状态迁移之前；记录写入与 open→consumed 迁移背靠背完成，中间
   无任何可失败步骤——任何前置失败，pending 保持 open、无消费
   记录（等效 P2.2 SQLite 单事务 ROLLBACK 的半成功防线）。故障
   注入测试：时钟抛异常 / 时钟返回非法类型两种场景，均验证
   open 保持 + `_latest_consumption()` 为 None + 恢复后可成功消费。
2. **R2 candidate 契约恢复（P2.1）**：`_ConsumeOutcome.candidate`
   类型标注由误写的 `dict | None` 恢复为 `PendingCandidate | None`
   （两实现运行时本就返回冻结对象，属标注回归）；补
   `candidate_as_dict()` 兼容垫片（六字段 dict 拷贝，失败为 None）；
   双实现测试锁定冻结结构与垫片行为。
