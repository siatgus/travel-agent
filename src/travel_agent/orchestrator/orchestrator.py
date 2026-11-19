"""Orchestrator：唯一调度入口（文档 4.1 目标架构第一阶段）。

一轮消息的处理顺序（= 原 _turn_impl 的真实顺序，逐段搬移、行为零变化）：

    1. 创建 TurnContext（单轮上下文）
    2. 明确咨询预判：周边餐饮/酒店/两点路线（_nearby_gate，最高优先级）
    3. 行程节点问答 gate（_plan_spot_gate）
    4. 意图路由（IntentRouter：规则 → LLM 兜底 → spot_qa 规则兜底）
    5. 非规划问答分发（Session 既有 _answer / QA 处理方法，本阶段复用）
    6. 方案修改守卫（GuardPipeline，保持原顺序与业务语义）
    7. 规划管道 → PlanHandler（handlers/plan_handler.py，已迁入）

业务原则不变：真实班次/价格/距离由规则与工具负责；模型只在受控范围内
兜底分类和润色表达；规划请求宁可进规则管道，也不让 LLM 自由生成行程。
"""

from __future__ import annotations

import os

from .. import llm
from ..dialogue import Reply
from ..parser import parse_utterance
from ..pending import PendingStore
from ..pending_runtime import PendingRuntimeManager
from .context import TurnContext
from .guard import GuardPipeline
from .handlers import QA_HANDLERS
from .handlers.plan_handler import PlanHandler
from .intent_router import IntentRouter, QA_KINDS
from .result import answer_reply


class Orchestrator:
    """单轮流程调度。Handler 复用 Session 既有业务方法（第一阶段）。"""

    def __init__(self):
        self.router = IntentRouter()
        self.guards = GuardPipeline()
        self.plan_handler = PlanHandler()
        self.last_ctx: TurnContext | None = None   # 最近一轮的观测点（排查/测试用）
        # P2.4a：pending 消费通道 runtime 管理器（延迟构造——
        # TRAVEL_PENDING_CHANNEL 关闭时永不创建，完全旁路零开销）
        self._pending_manager: PendingRuntimeManager | None = None
        self._pending_warnings: list[str] = []     # shadow 警告（旁路等）

    # -------------------------------------------------- P2.4a pending gate

    @staticmethod
    def _channel_enabled() -> bool:
        """TRAVEL_PENDING_CHANNEL 开关（默认 0 = 完全旁路）。"""
        return os.environ.get("TRAVEL_PENDING_CHANNEL", "0").strip() == "1"

    def _pending_runtime(self, sid: str):
        """延迟装配 Runtime Manager（首个 channel=1 请求时构造）。"""
        if self._pending_manager is None:
            self._pending_manager = PendingRuntimeManager()
        return self._pending_manager.acquire(sid)

    def _resolve_pending_id(self, session, store) -> str | None:
        """Pending ID 解析（§3.2 冻结四步顺序；hint 非权威，store 为准）。"""
        p = store.get_active_pending()
        if p is not None:                      # ① open 且未过期
            session.active_pending_id = p.pending_id   # hint 刷新
            return p.pending_id
        hint = getattr(session, "active_pending_id", None)
        if hint:                               # ② hint（过期/终态识别）
            return hint
        return store._latest_pending_id()      # ③ 重启恢复线索

    def _pending_gate(self, session, utterance: str, ctx) -> Reply | None:
        """唯一拦截点（步骤 1.75）：pending 消费通道。

        P2.4a 边界：开关关闭完全旁路（不构造 runtime、不查 store、
        不 evaluate/act）；开关打开时**只接消费侧、不创建 Pending**
        （生产者仍冻结），本阶段为**观察模式**——evaluate 纯判定 +
        影子记录后一律返回 None 交回原链路；verdict 分发/回复行为
        （含 consume 成功短路）属 P2.4b，本阶段绝不执行 act。
        """
        if not self._channel_enabled():
            return None                        # 完全旁路（§2.2）
        sid = getattr(session, "pending_session_id", None)
        if not sid or not str(sid).strip():
            # 会话身份缺失 → 旁路 + shadow 警告（绝不猜测身份，§2.3）
            self._pending_warnings.append(
                "pending gate bypass: session 身份未注入（pending_session_id 缺失）")
            return None
        rt = self._pending_runtime(str(sid))
        try:
            pid = self._resolve_pending_id(session, rt.store)
            if pid is None:                    # ④ 无任何线索 → 旁路
                return None
            # evaluate 纯判定零副作用（shadow_sink 异常隔离在拦截器内）；
            # hint 非权威：错误 hint 由 store 状态机产出 none，无误消费
            rt.interceptor.evaluate(utterance, getattr(ctx, "parse", None),
                                    session_pending_id=pid)
            return None                        # P2.4b 才接管 verdict 分发
        finally:
            # lease 归还：此时全部 store 锁已释放（批准条件冻结时序）
            self._pending_manager.release(rt)

    # -------------------------------------------------- 调度入口
    def handle(self, session, utterance: str,
               history_text: str | None = None) -> Reply:
        # ---- 1. 创建单轮上下文；跨轮状态仍归 Session ----
        ctx = TurnContext(utterance=utterance, history=session.history,
                          slots=session.slots, last_plan=session.last_plan,
                          plan_made=session.plan_made)
        self.last_ctx = ctx
        session.turn_no += 1
        session.history.append({"role": "user",
                                "content": history_text or utterance})
        # 说话层/闲聊层带最近几轮（分类、抽取、校验层不读它，见 llm.attach_history）
        llm.attach_history(session.client, session._llm_history())

        # ---- 1.5 统一解析（Phase 2）：一句话只完整解析一次，结果挂在
        # ctx.parse 供路由/守卫/规划管道消费——任何模块不得再对原始句子
        # 独立执行互相冲突的实体抽取。查询城市落在 session.query_city，
        # 与 slots.destination 独立存储（查询类意图绝不改写目的地）。
        ctx.parse = parse_utterance(utterance, context=session,
                                    today=session.today)
        session.query_city = ctx.parse.query_city

        # ---- 1.75 pending 消费通道（P2.4a 单点接入，唯一拦截点）----
        # 落点冻结：统一解析之后、周边咨询 gate 之前——保证「第一个」
        # 等选择话术不被下游 gate 抢答；返回 None 时原链路一行不改。
        gated = self._pending_gate(session, utterance, ctx)
        if gated is not None:
            session.history.append({"role": "agent", "content": str(gated)})
            ctx.route, ctx.handled = "pending_consume", True
            return gated

        # ---- 2. 明确咨询预判：周边问句分流（找吃的/找住的/两点路线）----
        # 「宽窄巷子附近有什么好吃的」一旦被当成「问宽窄巷子这个景点」，
        # 周边查询就提前结束；「从X怎么去Y」的「怎么去」还会撞上行程点名拦截。
        nearby = session._nearby_gate(utterance)
        if nearby is not None:
            session.history.append({"role": "agent", "content": nearby})
            ctx.route, ctx.handled = "qa_nearby", True
            return answer_reply(session, nearby)

        # ---- 3. 行程节点问答 gate：点名行程里的店/景点问细节 ----
        gated = session._plan_spot_gate(utterance)
        if gated is not None:
            session.history.append({"role": "agent", "content": gated})
            ctx.route, ctx.handled = "qa_plan_spot", True
            return answer_reply(session, gated)

        # ---- 4. 意图路由：问天气/景点/班次/闲聊时直接答，不进排行程流水线 ----
        # 之前所有输入都被当成「补槽位」，于是问天气会被回「排行程还差 2 件事」，
        # 问到第三轮还会硬凑出一份别的城市的行程。
        ir = self.router.route(session, utterance, parse=getattr(ctx, "parse", None))
        ctx.intent, ctx.intent_source = ir.name, ir.source
        ctx.city = ir.entities.get("city")

        # ---- 5. 非规划问答分发（QA Handler 层，本阶段复用 Session 方法）----
        if ir.name in QA_KINDS:
            text = session._answer(ir.name, utterance)
            if text is not None:
                session.history.append({"role": "agent", "content": text})
                ctx.route, ctx.handled = "qa", True
                return answer_reply(session, text)

        # ---- 6. 方案修改守卫（GuardPipeline，保持原有顺序与语义）----
        reply = self.guards.run(session, utterance, ir.name, ctx)
        if reply is not None:
            ctx.route, ctx.handled = "guard", True
            return reply

        # ---- 7. 规划管道：槽位抽取合并 → 追问/拦截 → plan_trip ----
        ctx.route = "plan"
        return self.plan_handler.run_plan_pipeline(session, utterance, ctx)

    # -------------------------------------------------- 规划管道（委托）
    def run_plan_pipeline(self, session, utterance: str, ctx) -> Reply:
        """委托给 PlanHandler（第三阶段迁入 handlers/plan_handler.py）。

        保留入口：既有测试与外部调用方按此签名调用，业务实现全在
        PlanHandler，本方法不做任何处理。
        """
        return self.plan_handler.run_plan_pipeline(session, utterance, ctx)
