# Phase 5 P2.4 开工申请：PendingInterceptor 单点接入（消费通道）

> 状态：**申请待批**。本文档为开工申请，未写任何业务代码。
> 前序：P2.3 最终验收通过（最终提交链 797bf76 → a913289 → 0773ad9 → 79b1c04）。

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
    store, interceptor = self._pending_runtime(session)
    if store.get_active_pending() is None and session_pending_id is None:
        return None                    # 无 pending → 交回原链路
    v = interceptor.evaluate(utterance, ctx.parse,
                             session_pending_id=..., now=...)
    out = interceptor.act(v)           # act 如实返回，失败不伪装
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

### 2.3 会话身份（需确认的决策 ①）

`Session` 无 id。方案：**server/调用方注入**——`Session` 增加一个
`pending_session_id: str | None = None` 属性（不改 `__init__` 签名、
不影响存档 schema），由 server 在建会话时以 sid 赋值；未注入且 channel=1
时按「无 pending」旁路并记一条 shadow 警告。拦截器自身不猜测身份。
（备选：给 Orchestrator.handle 加带默认值的新参数——改动更侵入，不推荐。）

### 2.4 runtime 装配

- Orchestrator 延迟装配：第一次 channel=1 的请求时构造
  `store + PendingInterceptor`（store 类型按 §2.2 开关），缓存于
  Orchestrator 实例；channel=0 时永不构造。
- store 生命周期跨会话（SQLite）；内存 store 仅测试/单进程默认。

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

## 3. 风险清单与对策

| # | 风险 | 对策 |
|---|---|---|
| 1 | 拦截点误放在 gate 后，选择话术被周边 gate 吞掉 | 落点固定 1.5↔2 之间；负向测试：「排海贝皇大排档…第一个」类句子在 pending 存在时不被其他 gate 抢答（channel=1 + 预置 pending） |
| 2 | consume 成功后本轮原链路仍执行造成双重处理 | 消费成功必须短路返回；测试断言原链路未执行（route 标记为 pending_consume） |
| 3 | act 失败被伪装成功 | 沿用 P2.3 act 诚实性契约 + 新增集成级失败注入（store 抛异常 → 交回原链路 + shadow 记录） |
| 4 | session 身份缺失导致跨会话串扰 | 未注入 id → 旁路 + 警告；跨 Session 隔离测试复用 P2.2/P2.3 契约 |
| 5 | 开关误开影响现有用户 | 默认 0；全量回归在 channel=0 下跑，逐位对比兜底 |
| 6 | 内存/SQLite 行为分叉 | P2.3 双实现对拍脚本保留并纳入本阶段回归 |

## 4. 测试与验收门槛（申请冻结）

`test_pending_channel.py` 目标 ≥40 条：
- 开关矩阵：0 旁路逐位对比（≥5）、1+无 pending 旁路（≥3）、1+有 pending 全 verdict 路径；
- 落点正确性（gate 抢答负向）、consume 成功短路 + 失败不短路、
  act 失败注入、session 身份缺失旁路、跨 Session 隔离、双实现各跑一遍；
- 回归门槛：现有全套件（pending 60 / sqlite 66 / interceptor 150 /
  decision 51 / adapter 32 / resolver 79 / corpus v4 50 / parser 62 /
  extractor 36）全绿 + dialog/server 相关既有套件不回归。

## 5. 明确不做（继续冻结）

- 不创建 pending（DecisionEngine 接管/clarify 生产者不授权）；
- 不修改任何 Handler；`TRAVEL_PENDING_CHANNEL` 默认保持 0；
- 不扩大 DecisionEngine 接管范围（TAKEOVER=0）；
- 不进入 P2.5（运行时验证）/ P3。

## 6. 申请结论

以上方案（含两个待确认决策：① 会话身份经 `Session.pending_session_id`
注入；② 拦截点固定在步骤 1.5 与 2 之间、consume 成功短路/失败不短路）
确认后申请开工。实现拟拆两步独立提交：**P2.4a** 装配层 + 开关 + 旁路
逐位对比；**P2.4b** verdict→回复行为 + 集成测试。每步独立回归、独立验收。
