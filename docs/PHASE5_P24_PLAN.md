# Phase 5 P2.4 开工申请（修订版 r4）：PendingInterceptor 单点接入（消费通道）

> 状态：**修订版申请待批**（r4）。本文档为开工申请，未写任何业务代码。
> 前序：P2.3 最终验收通过（最终提交链 797bf76 → a913289 → 0773ad9 → 79b1c04）。
> 修订记录：r2 补入 R1 追踪机制（§3）与 sid 隔离（§4）；r3 闭合
> `_latest_pending_id()` 查询语义与 memory Runtime 业务状态保留淘汰。
> r4 按评审意见补齐两项边界约束：① 同一 sid 单有效 Pending 不变量
> 与遮蔽边界测试（§3.6）；② 保留检查与淘汰的并发同步约束、窗口
> 比较符与 P2.3 严格一致（§4.4）；测试 A/B 边界断言补强（§6）。

## 0. 一句话范围

把 P2.3 的 `PendingInterceptor`（evaluate/act + 九类 verdict）以**唯一一个拦截点**接入
`Orchestrator.handle`，由 `TRAVEL_PENDING_CHANNEL` 控制（**默认 0 = 完全旁路**）；
只接**消费侧**，不创建任何 pending（生产者属 DecisionEngine 接管，仍冻结）。

## 1. 链路现状（勘察结论，设计的落点依据）

- `Orchestrator.handle(session, utterance)` 是唯一调度入口，顺序：
  1 TurnContext → 1.5 统一解析 `parse_utterance` → 2 周边咨询 gate →
  3 行程节点问答 gate → 4 意图路由 → 5 QA 分发 → 6 GuardPipeline →
  7 规划管道。
- `DecisionEngine`（P1）与 `PendingInterceptor`（P2.3）目前**均未接线**：
  全仓库无任何 `DecisionEngine(`/`PendingInterceptor(`/`PendingStore(` 生产调用，
  只有测试引用。`TRAVEL_PENDING_CHANNEL` 全仓库无读取点。
- `Session`（dialogue.py:304）**没有 session_id 字段**；会话身份只存在于
  server 层的 sid（conversations.py 按 sid 存档）。PendingStore 按
  session_id 绑定，因此接入前必须先解决「拦截点如何拿到会话身份」。
- 现有 gate（周边/行程点名）在 parse 之后立即短路返回——拦截点若放在
  gate 之后，用户「第一个」这类选择可能被周边 gate 吞掉；若放在 parse
  之前，则拿不到 `ctx.parse`。**落点只能是 1.5 与 2 之间。**

## 2. 接入设计（单点，冻结提案）

### 2.1 唯一拦截点

```
Orchestrator.handle:
  1.   TurnContext
  1.5  ctx.parse = parse_utterance(...)          # 现状不动
  1.75 ⬅ 新增 pending_gate（本阶段唯一新增点）
  2~7  现状不动
```

`pending_gate` 伪码（实现为 Orchestrator 私有方法，不新建文件亦可）：

```
def _pending_gate(self, session, utterance, ctx) -> Reply | None:
    if not channel_enabled():          # TRAVEL_PENDING_CHANNEL，默认 0
        return None                    # 完全旁路：不构造、不查询、零开销
    rt = self._pending_runtime(session)      # 按 sid 隔离（§4）
    pid = self._resolve_pending_id(session, rt.store)   # 解析顺序见 §3.2
    if pid is None:
        return None                    # 无任何 pending 线索 → 交回原链路
    v = rt.interceptor.evaluate(utterance, ctx.parse,
                                 session_pending_id=pid, now=...)
    out = rt.interceptor.act(v)        # act 如实返回，失败不伪装
    self._sync_active_hint(session, v, out)   # 终态迁移后清理 hint（§3.3）
    return self._pending_reply(out, v, session)   # 按 verdict 生成回复
```

- 拦截点返回 `None` → 原链路原样执行（步骤 2~7 一行不改）。
- 拦截点返回 `Reply` → 短路（同现有 gate 的返回约定）。
- **单点原则**：Orchestrator 内只出现这一个调用处；Handler 一律不改。

### 2.2 开关语义（TRAVEL_PENDING_CHANNEL）

| 值 | 行为 |
|---|---|
| 未设置 / 0（默认） | **完全旁路**：不 import 时构造 runtime、不查 store、不 evaluate；Orchestrator 行为与 79b1c04 逐位一致（深拷贝对比测试锁定） |
| 1 | 启用消费通道；且**仍不创建 pending**（生产者冻结），故无 pending 会话零变化 |

补充开关（本阶段新增，均默认安全值）：
- `TRAVEL_PENDING_STORE` = `memory`（默认，进程内）| `sqlite`（挂 `paths.app_db()`）；
- `TRAVEL_PENDING_RESELECT_WINDOW` 沿用 P2.3（默认 600s）。

### 2.3 会话身份（决策 ①，已获原则批准）

`Session` 无 id。方案：**server/调用方注入**——`Session` 增加两个
独立属性（不改 `__init__` 签名、不影响存档 schema），由 server 在建
会话时以 sid 赋值；未注入且 channel=1 时按「无 pending」旁路并记一条
shadow 警告。拦截器自身不猜测身份。（备选：给 Orchestrator.handle 加
带默认值的新参数——改动更侵入，不推荐。）

**两个字段严格区分、不得混用（评审冻结）：**

| 字段 | 含义 | 写入方 |
|---|---|---|
| `pending_session_id` | 当前 Session 的**身份**（= sid），PendingStore 绑定与隔离的依据 | server 建会话时一次性注入 |
| `active_pending_id` | 当前 Session 正在等待用户选择的 **Pending ID**（提示值，非权威） | 生产者创建成功后写入（P2.4 测试预置）；gate 按 §3.3 规则清理 |

`pending_session_id` 绝不承担 Pending ID 职责；`active_pending_id`
绝不参与 Store 的 session 隔离判定（隔离仍由 store 内部
`session_id` 条件保证，P2.2 已验收机制）。

### 2.4 runtime 装配 → 按 sid 隔离（详见 §4）

- Orchestrator 延迟装配：第一次 channel=1 的请求时按 sid 构造
  `PendingRuntime(store + PendingInterceptor)`，**以 sid 为 key 缓存
  于 dict，绝不共享单个 store 实例**（§4）；channel=0 时永不构造。
- store 生命周期跨会话（SQLite，每 sid 独立实例）；内存 store 仅
  测试/单进程默认，同样每 sid 独立实例。
- **禁止规避**：不得以空字符串/默认 session_id、共享单例内存 store
  等方式绕过隔离（评审冻结，负向测试锁定）。

### 2.5 verdict → 回复行为（九类逐一冻结）

| verdict | act 结果 | pending_gate 行为 |
|---|---|---|
| consume | ok | 短路：消费确认文案（引用 `selected_name`/城市，规则 7：只确认消歧，不产生行程动作），历史追加 agent 消息 |
| consume | **失败** | **不短路**：如实把失败原因降级进 shadow log，交回原链路（绝不改选其他候选、绝不伪造成功） |
| cancel_new_question / cancel_explicit / cancel_destination_changed | ok | 不短路：pending 已取消，本轮按原链路处理（其本就是新问题/取消/改目的地语义） |
| expired_reply | ok | 短路：如实告知已过期（规则 5），提示重新提问 |
| restate | 零状态变化 | 短路：重述候选列表（快照冻结顺序 1..N） |
| hand_back | 零状态变化 | **不短路**：交回原链路（保守红线） |
| duplicate_select | 零状态变化 | 短路：告知已确认过（引用 `_latest_consumption` 七字段） |
| none | — | 不短路 |

### 2.6 零变化保证（TAKEOVER 仍 0）

1. channel=0：Orchestrator 输出与 79b1c04 逐位一致（既有全量回归 +
   新增「开关关闭逐位对比」测试）。
2. channel=1 且无 pending：同样零变化（旁路返回 None）。
3. 不创建 pending → 消费通道在真实链路上**暂无可消费对象**；它的价值
   在于：通道语义、回复行为、故障路径全部就绪并受测，生产者
   （DecisionEngine 接管 / clarify 创建）属后续阶段单独申请。
4. evaluate/act/shadow 全部沿用 P2.3 契约，本阶段零修改拦截器与两个
   store 的业务逻辑（只允许装配层适配）。

## 3. R1：active_pending_id 追踪 / 清理 / 恢复机制（评审开工条件 ①）

### 3.1 问题

`get_active_pending()` 对已过期未登记的 Pending 返回 None；若调用方
不持有该 Pending 的 ID，`evaluate()` 无法收到 `session_pending_id`，
用户对过期 Pending 的选择会直接落入原意图链路，`expired_reply`
（规则 5）永远无法触发。因此必须有独立于 store 查询结果的 **ID 追踪**。

### 3.2 ID 解析顺序（pending_gate 内，冻结）

```
1. store.get_active_pending()       # 命中 → open 且未过期，取其 pending_id，
                                    #   并回写 session.active_pending_id（hint 刷新）
2. session.active_pending_id        # hint 存在 → 直接作为 session_pending_id
                                    #   传入 evaluate（过期/终态识别路径）
3. store._latest_pending_id()       # 两者皆无（如进程重启后 hint 丢失）→
                                    #   store 内部方法返回本 sid 最近一条
                                    #   pending 的 ID（含已过期/已取消）；
                                    #   evaluate 对终态 Pending 自然产出 none
4. 全部为空 → 旁路交回原链路
```

- **hint 非权威**：绝不仅凭 `session.active_pending_id` 认定 Pending
  有效——有效性（open/过期/归属）最终全部由 Store 判定（evaluate/act
  内部经 session_id 条件与状态机校验，P2.2/P2.3 已验收机制）。
- **store 新增内部方法** `_latest_pending_id()`（下划线，不进公共契约，
  沿用 `_latest_consumption` 先例）。**查询语义（r3 冻结）：**
  - **严格按当前 store 绑定的 session_id 查询**，绝不接受外部传入
    sid（防跨会话读取）；
  - **记录范围**：本 sid `created_at` 最新一条 pending 记录，**包含
    全部状态**（open / expired / consumed / cancelled）——终态记录
    也返回，因为它仍是「该会话最近一次 pending 交互」的线索；
  - **返回值仅作 evaluate 的解析线索**，绝不被视为有效待处理对象：
    终态记录传入 evaluate 后由 P2.3 冻结状态机产出 `none`（consumed /
    cancelled → none，不重复消费、不触发 duplicate_select——重复选择
    识别只走 `_latest_consumption()`，两者数据源分离）；expired 记录
    → `expired_reply`；open → 正常判定；
  - 内存桩与 SQLite 同语义，纳入双实现对拍；重启恢复依赖此方法
    （SQLite 持久化跨进程；内存 store 进程内失效，重启后返回 None，
    属预期）。
- 解析顺序第 3 步仅在 SQLite store 下生效语义完整；memory store 重启
  即清空，第 3 步天然返回 None，行为一致。

### 3.3 清理 / 更新规则（gate 在 act 之后执行，冻结）

| act 结果 | `session.active_pending_id` |
|---|---|
| consume / cancel_* / expired_reply **成功**（终态迁移） | **清空为 None** |
| 上述 verdict 失败（act 如实失败，状态未迁移） | **保留**（pending 仍 open，可重试） |
| restate / hand_back / duplicate_select / none | 保留（状态未迁移） |

- 过期 Pending 的 ID **不因 `get_active_pending()` 返回 None 而丢弃**：
  hint 只在终态迁移**成功**后清理。
- hint 与 store 状态不一致时以 store 为准（hint 只是加速解析的缓存，
  错值的最坏结果是 evaluate 产出 none 交回原链路，不产生误消费）。

### 3.4 生产者接口预留（本阶段不实现）

生产者（DecisionEngine 接管 / clarify 创建，后续阶段授权）契约：
`create_pending` 成功后**必须** `session.active_pending_id = p.pending_id`
（同一事务语义：创建失败不得写 hint）；`pending_session_id` 缺失时
拒绝创建。P2.4 用测试预置 Pending + 手工写 hint 验证全链路。

### 3.5 存档/恢复策略

`active_pending_id` **暂不入** `Session.snapshot()` schema（P2.4 无
生产者，无持久化 pending 可恢复）；待生产者落地时必须随存档持久化
并在 `Session.restore` 回填，恢复后仍走 §3.2 解析顺序（store 为准）。
该要求已记入生产者接口契约（§3.4），防遗漏。

### 3.6 单有效 Pending 不变量（r4 新增，评审边界约束 ①）

**约束：同一 sid 任意时刻最多存在一个业务有效（status=open）的
Pending。** 该不变量由既有已验收机制保证，r4 明确为设计约束并加
边界测试：

- P1.5 规则 8：存在 active open 时 `create_pending` 一律拒绝；
- P2.2：部分唯一索引 `ux_pending_open(session_id) WHERE status='open'`
  在 DB 层兜底；`create_pending` 事务内先自动过期本 sid 旧 open
  （阻断项①）——因此「新 Pending 创建时旧 Pending」只会发生
  open→expired 一种状态转换，遵守既定规则，绝无静默并存。

**遮蔽不变量**：由上述机制可推导——若本 sid 存在 open Pending，
则它必是 `created_at` 最新的记录（更晚的创建要么被拒、要么先把旧
open 转为 expired），故 `_latest_pending_id()` 返回终态记录时，
**不可能存在被遮蔽的 open Pending**；反之为真。评审场景
（P1 open 10:00 / P2 consumed 10:05）在该不变量下不可构造：P2 能
创建即证明 P1 已非 open。边界测试锁定此推导（见 §6 多 Pending 组）：
穷举 create→consume→create、open 阻断、过期+create 等序列，断言
任一序列后「open 存在 ⇒ open 是最新记录」成立。

## 4. R2：Runtime 按 sid 隔离与缓存清理（评审开工条件 ②）

### 4.1 隔离结构

```
self._pending_runtimes: dict[str, PendingRuntime]   # key = sid
```

每个 `PendingRuntime` 独立持有：
- **memory store**：按 sid 保留**独立实例**（进程内互不可见，天然隔离）；
- **sqlite store**：每个 sid 对应**独立 `SQLitePendingStore` 实例**
  （各自绑定自己的 session_id；同一 DB 文件，隔离由 P2.2 的
  session_id SQL 条件保证）；
- 独立 `PendingInterceptor`（无共享可变状态，独立装配防串扰）。

### 4.2 缓存清理策略（r3 重写：按「业务状态保留」判定，不看 open）

评审阻断项闭合——`get_active_pending() is None` **不能**作为可淘汰
依据：到期未登记的 Pending 底层仍是 open、刚消费的记录仍在重复选择
窗口内，两者被淘汰都会造成状态丢失（expired_reply / duplicate_select
失效）。冻结如下：

**SQLite runtime**：任意时刻均可安全淘汰（Pending 与消费记录均持久化
在 DB，重建后经 `_latest_pending_id()` / `_latest_consumption()` 完整
恢复，淘汰不删除任何数据库记录）。

**memory runtime**（数据随淘汰丢失，必须更严格）——仅当满足
`_has_runtime_retention_state(now) is False` 才允许淘汰：

| runtime 状态 | memory 是否允许淘汰 |
|---|---|
| 无 Pending，也无重复选择窗口内的消费记录 | **允许** |
| 存在未终结 Pending（status=open，**含已到期但尚未处理**的） | **不允许** |
| 存在仍处于重复选择识别窗口内（默认 10 分钟）的消费记录 | **不允许** |

### 4.3 `_has_runtime_retention_state(now)`（内部只读判定，r3 新增）

- 两个 store 均提供同语义实现（下划线内部方法，不进公共 Store API，
  沿用 `_latest_consumption` 先例），纳入双实现对拍；
- 语义：`True` 表示该 runtime 仍承载必须由它承担的业务状态——
  ① 本 sid 存在 status='open' 的 Pending（**按库内原始状态判断，
  不做时间过滤**，因此到期未登记的 open 记录同样命中）；
  ② 最近一条消费记录的 `consumed_at` 距今 < `TRAVEL_PENDING_RESELECT_WINDOW`
  （epoch 比较，与 P2.3 重复选择同一窗口口径）；
- ①② 任一成立 → `True`（保留）；全部失效 → `False`（允许淘汰）；
- 该方法**只读**，绝不产生状态迁移或时间登记副作用；
- 淘汰算法：LRU（OrderedDict，上限 `TRAVEL_PENDING_RUNTIME_MAX`
  默认 256）顺位扫描候选，memory runtime 处于保留态则跳过淘汰下一个；
  若全部 memory runtime 均处于保留态导致超限，**允许暂时超上限不
  淘汰**并记 shadow 警告（宁可靠内多缓存，不丢会话状态）；memory
  Runtime 淘汰 = 状态丢弃，与「进程内存退出即失效」同语义。
- 未来若需在容量压力下淘汰 memory runtime，须另行设计持久化/迁移
  机制，**不在 P2.4 顺手扩大范围**（评审冻结）。
- channel=0 时 dict 永不创建、永不增长。

### 4.4 保留检查与淘汰的并发同步约束（r4 新增，评审边界约束 ②）

**竞态场景（评审指出）**：LRU 扫描判定某 runtime 无保留状态 → 并发
请求恰好向该 runtime 创建 Pending → LRU 按旧判断淘汰 → 新 Pending
的运行时上下文丢失。冻结约束：

- **检查与淘汰必须在同一临界区内原子完成**，且该临界区与该 runtime
  store 的状态写入（create / consume / cancel / expire / 消费记录写入）
  共用**同一把锁**——memory 实现即 `PendingStore._lock`；SQLite 实现
  用既有 `BEGIN IMMEDIATE` 事务边界 + store 级锁，不引入新并发架构；
- 具体协议：淘汰线程先取得候选 runtime store 的锁 → 锁内执行
  `_has_runtime_retention_state(now)` → 保留态则放行下一个候选；
  可淘汰则在**同一临界区内**完成「从 `_pending_runtimes` 移除 +
  关闭连接（SQLite）」再释放锁——锁外绝不依据过期的保留判断执行
  淘汰动作；
- 这样竞态只有两种可序列化结果：① 创建先于锁获取 → 保留检查看到
  新 Pending（True，不淘汰）；② 淘汰先完成 → 并发创建进入按 §4.1
  新建的 runtime（该 Pending 尚不存在，新建无损）；不存在「创建成功
  但 runtime 被淘汰」的第三种交错；
- `PendingRuntime` 装配（§4.1）对同一 sid 的并发首次访问用 dict 操作
  原子性 + 双检，保证同 sid 只建一个实例；
- **双实现对拍覆盖相同输入状态与相同时间边界**（评审冻结）：保留态
  判定、淘汰决策、窗口临界点行为逐位一致。

**窗口比较符一致性（评审特别强调）**：`_has_runtime_retention_state`
的「窗口内」判定与 P2.3 `PendingInterceptor` 重复选择识别的窗口判定
**必须使用完全相同的比较表达式**（同一常量、同一比较符，实现上共用
同一函数/常量定义），绝不允许一处 `<`、一处 `<=`；测试在
`consumed_at + window == now` 临界点断言两处判定结果一致。

## 5. 风险清单与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | 拦截点误放在 gate 后，选择话术被周边 gate 吞掉 | 落点固定 1.5↔2 之间；负向测试：「排海贝皇大排档…第一个」类句子在 pending 存在时不被其他 gate 抢答（channel=1 + 预置 pending） |
| 2 | consume 成功后本轮原链路仍执行造成双重处理 | 消费成功必须短路返回；测试断言原链路未执行（route 标记为 pending_consume） |
| 3 | act 失败被伪装成功 | 沿用 P2.3 act 诚实性契约 + 新增集成级失败注入（store 抛异常 → 交回原链路 + shadow 记录） |
| 4 | session 身份缺失导致跨会话串扰 | 未注入 id → 旁路 + 警告；跨 Session 隔离测试复用 P2.2/P2.3 契约 |
| 5 | 开关误开影响现有用户 | 默认 0；全量回归在 channel=0 下跑，逐位对比兜底 |
| 6 | 内存/SQLite 行为分叉 | P2.3 双实现对拍脚本保留并纳入本阶段回归 |

## 6. 测试与验收门槛（申请冻结，含 R1/R2 专项）

`test_pending_channel.py` 目标 ≥50 条，分两组验收：

**P2.4a 组（装配与旁路）**：
- channel=0 旁路逐位对比（≥5）；channel=1 无 pending 线索旁路（≥3）；
- **R1**：hint 解析四步顺序各一例；过期未登记 Pending + hint →
  evaluate 收到正确 `session_pending_id`、`expired_reply` 可触发；
  hint 非权威（错误 hint → none 交回原链路，无误消费）；
  `_latest_pending_id()` 重启恢复（SQLite 重连后按 sid 取回）；
  终态迁移成功清 hint / 失败保留 hint；
- **R2**：多 Session（A/B 交错请求）各自独立 runtime、store 隔离
  （B 查不到 A 的 pending）；共享单例 store 规避手法被测试拦截；
  LRU 上限与保留态保护；channel=0 时 dict 为空；
- **测试 A（r3 评审冻结，r4 补断言）**：Pending 到期未登记 → 推入
  LRU 淘汰压力 → runtime 不得因 `get_active_pending() is None` 被淘汰
  （memory）；随后用户选择 → 仍正确产出 `expired_reply`；
  **r4 边界断言**：淘汰压力前后 Pending ID、候选快照六字段与原始
  状态（open）逐位一致（store 查询对比，非对象引用）；
- **测试 B（r3 评审冻结，r4 补时间边界）**：消费成功后处于重复选择
  窗口内 → 推入淘汰压力 → runtime 不得被淘汰（memory）；随后
  「第一个」→ 仍识别原消费记录产出 `duplicate_select`；
  **r4 边界断言**：`consumed_at + window == now` 临界点**以内必须
  保留、以外允许淘汰**，且该比较符与 P2.3 重复选择识别严格一致
  （同一常量/表达式，两处判定在临界点结果相同）；
- **多 Pending 遮蔽边界组（r4 新增）**：穷举
  create→consume→create、open 阻断 create、过期+create 自动登记
  等序列，断言「本 sid 存在 open ⇒ open 必为 `_latest_pending_id()`
  所指」的不变量成立（终态记录绝不遮蔽仍有效的旧记录，§3.6）；
- **保留-淘汰并发竞态组（r4 新增）**：多线程交错「LRU 扫描 vs
  create_pending」注入，断言只有两种可序列化结果（创建先至则不淘汰
  / 淘汰先至则新建无损），不存在创建成功但 runtime 被淘汰的交错
  （§4.4）；双实现同输入同时间边界对拍保留态判定与淘汰决策。

**P2.4b 组（verdict 行为与集成）**：
- 九类 verdict 全路径（§2.5 表逐行）：consume 成功短路 + 只确认消歧
  不动行程；consume 失败不短路不伪造不改选；act 抛异常不影响原链路；
  hand_back 不改状态；restate 候选顺序与快照一致（1..N）；duplicate_select
  不重复消费不执行 Handler；expired_reply 后不允许继续消费；
  cancel_* 成功后进入原有链路；
- 集成级故障注入（store 抛异常）、跨 Session 隔离、双实现各跑一遍、
  gate 抢答负向。

**回归门槛**：现有全套件（pending 60 / sqlite 66 / interceptor 150 /
decision 51 / adapter 32 / resolver 79 / corpus v4 50 / parser 62 /
extractor 36）全绿 + dialog/server 相关既有套件不回归。

## 7. 明确不做（继续冻结，评审逐条重申）

- 不创建 Pending；不接入 DecisionEngine 生产者；
- 不修改任何 Handler；不修改 P2.3 的 evaluate/act 业务逻辑；
- `TRAVEL_PENDING_CHANNEL` 生产默认值保持 0（不开启）；
- 不扩大 DecisionEngine 接管范围（TAKEOVER=0）；
- 不进入 P2.5（运行时验证）/ P3。

## 8. P2.4a / P2.4b 验收边界（评审意见冻结）

**P2.4a：装配层与安全旁路**（先交付，独立验收）
- 范围：Session 身份注入（两字段）、R1 Pending ID 追踪接口与解析
  顺序、R2 runtime 按 sid 隔离与清理、开关矩阵、唯一拦截点接入
  （先验证旁路行为）、§6 P2.4a 组测试。
- 验收七条：默认关闭完全旁路；不构造 runtime/不查 store/不 evaluate/
  act；多 Session 不共享 store；过期 Pending ID 不丢失；原有行为
  不变；不改 Handler；不创建 Pending。
- **P2.4a 通过后才能申请 P2.4b。**

**P2.4b：verdict 回复与消费行为**（P2.4a 验收通过后单独申请）
- 范围与重点验收：§6 P2.4b 组表格逐行。

## 9. 申请结论（修订版）

评审两项开工条件已补齐：R1 见 §3（解析顺序/hint 清理/重启恢复/
生产者接口/存档策略），R2 见 §4（按 sid 隔离/LRU 保护/禁止规避）。
已批准决策（拦截点 1.5↔2、server 注入身份、consume 成功短路失败
回退、两阶段实施、默认关）全部保留。申请批准 **P2.4a** 开工；
P2.4b 待 P2.4a 独立验收通过后另行申请。
