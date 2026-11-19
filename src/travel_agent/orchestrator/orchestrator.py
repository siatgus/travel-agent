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
from ..pending import (PendingStore, PENDING_CANCELLED, PENDING_CONSUMED,
                       PENDING_EXPIRED, PENDING_OPEN)
from ..pending_interceptor import (PendingInterceptor, V_CANCEL_DEST_CHANGED,
                                   V_CANCEL_EXPLICIT, V_CANCEL_NEW_QUESTION,
                                   V_CONSUME, V_DUPLICATE_SELECT,
                                   V_EXPIRED_REPLY, V_RESTATE)
from ..pending_runtime import PendingRuntimeManager
from .context import TurnContext
from .guard import GuardPipeline
from .handlers import QA_HANDLERS
from .handlers.plan_handler import PlanHandler
from .intent_router import IntentRouter, QA_KINDS
from .result import answer_reply

# ---- P2.4b 冻结常量（评审 2026-11-19 CONDITIONAL PASS） ----

# 可产生终态迁移的 verdict：hint 清理只对这些 verdict 且**真实迁移
# 成功**（§3.3）时执行，绝不仅凭 verdict 类型清理
_MIGRATING_VERDICTS = frozenset((V_CONSUME, V_CANCEL_NEW_QUESTION,
                                 V_CANCEL_EXPLICIT, V_CANCEL_DEST_CHANGED,
                                 V_EXPIRED_REPLY))
# 过期告知文案（规则 5：如实告知过期，提示重新提问）
_EXPIRED_NOTICE = "您之前的选择已经过期失效了，请重新提问，我再为您推荐。"
# act 异常且无法判定 store 状态时的保守回复（B4：既不伪造消费成功，
# 也不交回原链路造成不确定状态下的重复业务处理）
_UNDETERMINED_NOTICE = "系统暂时无法确认您的选择状态，请稍后重试。"


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
        """唯一拦截点（步骤 1.75）：pending 消费通道（P2.4b 激活）。

        边界（评审冻结）：开关关闭完全旁路；只接消费侧、不创建
        Pending（生产者仍冻结）；evaluate/act 业务逻辑、两个 store、
        Runtime Manager（含 R1 异常安全）零修改——本方法只做装配层
        分发：evaluate → act → _sync_active_hint → _pending_reply。

        异常边界（评审冻结四项）：
        ① 消费成功是短路的决定性依据——act 成功后 _pending_reply
          构造失败必须兜底确认短路，绝不撤销消费、绝不回退普通意图
          路由（防同一条输入被原链路重复处理）；
        ② act 异常 ≠ 一定未迁移——按 store 实际状态安全判定；无法
          判定时保守拦截（不伪造成功、不交回原链路）；
        ③ shadow 永不影响主链路（拦截器已隔离 sink 异常，本方法
          _shadow_gate 亦吞掉一切记录异常）；
        ④ hint 同步以真实迁移结果为准（_sync_active_hint）。
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
            # evaluate 纯判定零副作用（P2.3 冻结契约）；hint 非权威：
            # 错误 hint 由 store 状态机产出 none，无误消费
            v = rt.interceptor.evaluate(utterance,
                                        getattr(ctx, "parse", None),
                                        session_pending_id=pid)
            name = v.verdict
            out = None
            try:
                out = rt.interceptor.act(v)    # act 如实返回，失败不伪装
            except Exception as exc:           # 评审冻结约束 ②（B2/B4）
                status = self._store_status_safe(rt, pid)
                self._shadow_gate(rt, name, "act_exception",
                                  detail=f"{type(exc).__name__}: {exc}",
                                  store_status=status, pending_id=pid)
                if status == PENDING_CONSUMED:
                    # 提交边界异常但迁移已确认成功 → 兜底确认短路，
                    # 绝不交回原链路（防重复业务处理）
                    self._sync_active_hint(session, name, True)
                    return self._fallback_confirm_reply(session)
                if status == PENDING_EXPIRED:
                    self._sync_active_hint(session, name, True)
                    if name == V_EXPIRED_REPLY:
                        return answer_reply(session, _EXPIRED_NOTICE)
                    return None                # 其余 → 原链路（§2.5）
                if status == PENDING_CANCELLED:
                    self._sync_active_hint(session, name, True)
                    return None                # cancel → 原链路（§2.5）
                if status == PENDING_OPEN:
                    self._sync_active_hint(session, name, False)
                    return None                # 确认未迁移 → 安全失败
                # 无法判定（pending 不存在或状态查询失败）→ 保守拦截
                self._sync_active_hint(session, name, False)
                self._pending_warnings.append(
                    "pending gate: act 异常且无法判定 store 状态"
                    "（保守拦截，hint 保留）")
                return answer_reply(session, _UNDETERMINED_NOTICE)
            migrated = bool(out is not None and getattr(out, "ok", False)) \
                and name in _MIGRATING_VERDICTS
            self._sync_active_hint(session, name, migrated)
            try:
                # 消费成功是短路的决定性依据（评审冻结规则 ①，B3）
                return self._pending_reply(out, v, session, rt)
            except Exception as exc:
                self._shadow_gate(rt, name, "reply_construction_failed",
                                  detail=f"{type(exc).__name__}: {exc}",
                                  pending_id=pid)
                if migrated:
                    return self._fallback_confirm_reply(session)
                return None        # 未迁移零状态 → 保守交回原链路
        finally:
            # lease 归还：此时全部 store 锁已释放（批准条件冻结时序）
            self._pending_manager.release(rt)

    # ---------------------------------------------- P2.4b 装配私有方法

    @staticmethod
    def _sync_active_hint(session, verdict_name: str, migrated: bool) -> None:
        """hint 同步（§3.3 冻结）：以**真实终态迁移结果**为准——迁移
        成功且属迁移类 verdict 才清空；失败/零状态 verdict 一律保留。"""
        if migrated and verdict_name in _MIGRATING_VERDICTS:
            session.active_pending_id = None

    @staticmethod
    def _store_status_safe(rt, pending_id: str) -> str | None:
        """act 异常后的安全判定（评审冻结约束 ②）：读取 store 实际
        状态；pending 不存在或查询失败 → None（无法判定，保守处理）。"""
        try:
            p = rt.store.get_pending_by_id(pending_id)
            return p.status if p is not None else None
        except Exception:
            return None

    @staticmethod
    def _shadow_gate(rt, verdict_name: str, action: str,
                     detail: str = "", store_status: str | None = None,
                     pending_id: str | None = None) -> None:
        """装配层影子记录（评审冻结约束 ③，B5）：shadow 是观察记录，
        绝不影响主链路——记录本身的一切异常在此吞掉。"""
        try:
            rt.interceptor.shadow_log.append({
                "stage": "gate", "verdict": verdict_name,
                "action": action, "detail": detail,
                "store_status": store_status, "pending_id": pending_id})
        except Exception:
            pass

    @staticmethod
    def _fallback_confirm_reply(session, selected_name: str = "",
                                selected_city: str | None = None) -> Reply:
        """B3/B4 兜底消费确认（评审冻结规则 ①）：不依赖复杂业务数据。
        消费成功后回复构造失败/提交边界异常时**仍然短路**，绝不交回
        原链路、绝不伪造超出「已确认选择」的内容。"""
        if selected_name:
            where = f"（{selected_city}）" if selected_city else ""
            text = f"已确认您的选择：{selected_name}{where}。"
        else:
            text = "已确认您的选择，消歧完成。"
        return answer_reply(session, text)

    def _pending_reply(self, out, v, session, rt) -> Reply | None:
        """§2.5 冻结接线表：verdict → 回复行为（九类逐一，不增不减）。

        需要短路但无法按冻结格式构造内容（如 consume 成功但候选信息
        缺失、duplicate 记录缺失）时抛异常，由 gate 按 B3 规则兜底——
        绝不拼造数据、绝不伪造消费结果。"""
        name = v.verdict
        pid = v.pending_id
        if name == V_CONSUME:
            if out is not None and out.ok:
                cand = getattr(out, "candidate", None)
                cname = getattr(cand, "name", "") \
                    if cand is not None else ""
                ccity = getattr(cand, "city", None) \
                    if cand is not None else None
                if not cname:
                    raise ValueError(
                        "consume 成功但缺少候选信息，无法按冻结格式构造确认")
                # 规则 7：只确认消歧，不产生行程动作
                return answer_reply(
                    session,
                    f"已确认：{cname}" + (f"（{ccity}）" if ccity else "")
                    + "。消歧完成，如需调整行程请直接告诉我。")
            # 失败 → 不短路：降级进 shadow，交回原链路（不改选不伪造）
            self._shadow_gate(rt, name, "consume_failed_not_shortcircuit",
                              detail=str(getattr(out, "reason", "")),
                              pending_id=pid)
            return None
        if name in (V_CANCEL_NEW_QUESTION, V_CANCEL_EXPLICIT,
                    V_CANCEL_DEST_CHANGED):
            # §2.5：cancel ok → 不短路（本轮按原链路处理新问题/取消/
            # 改目的地）；失败同样不短路（保守交回原链路），仅记影子
            if out is None or not getattr(out, "ok", False):
                self._shadow_gate(rt, name, "cancel_not_ok",
                                  detail=str(getattr(out, "reason", "")),
                                  pending_id=pid)
            return None
        if name == V_EXPIRED_REPLY:
            if out is not None and out.ok:
                return answer_reply(session, _EXPIRED_NOTICE)
            self._shadow_gate(rt, name, "expired_register_failed",
                              detail=str(getattr(out, "reason", "")),
                              pending_id=pid)
            return None
        if name == V_RESTATE:
            p = rt.store.get_pending_by_id(pid or "")
            if p is None or p.status != PENDING_OPEN:
                self._shadow_gate(rt, name, "restate_snapshot_unavailable",
                                  pending_id=pid)
                return None            # 快照不可用 → 保守交回原链路
            cands = sorted(p.candidates,
                           key=lambda c: (c.display_order is None,
                                          c.display_order))
            lines = [f"{i}. {c.name}" + (f"（{c.city}）" if c.city else "")
                     for i, c in enumerate(cands, 1)]
            return answer_reply(session, "当前候选如下：\n"
                                + "\n".join(lines) + "\n回复序号即可确认。")
        if name == V_DUPLICATE_SELECT:
            rec = rt.store._latest_consumption()   # 冻结七字段真实记录
            if not rec or not rec.get("selected_name"):
                self._shadow_gate(rt, name, "duplicate_record_missing",
                                  pending_id=pid)
                return None            # 无法引用真实记录 → 不拼造，交回原链路
            city = rec.get("selected_city")
            return answer_reply(session, "您刚刚已确认过「"
                                + str(rec["selected_name"])
                                + (f"（{city}）" if city else "")
                                + "」，无需重复选择。")
        return None                    # hand_back / none → 原链路（保守红线）

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
