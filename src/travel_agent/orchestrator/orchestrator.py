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
from ..candidate_extractor import extract as _extract_candidates
from ..decision import (ACTION_CLARIFY, ACTION_EXECUTE, DEFAULT_BATCHES,
                        DecisionEngine)
from ..dialogue import Reply
from ..entity_resolver import resolve as _resolve_entities
from ..parser import parse_utterance
from ..pending import (PendingStore, PENDING_CANCELLED, PENDING_CONSUMED,
                       PENDING_EXPIRED, PENDING_OPEN)
from ..pending_interceptor import (PendingInterceptor, V_CANCEL_DEST_CHANGED,
                                   V_CANCEL_EXPLICIT, V_CANCEL_NEW_QUESTION,
                                   V_CONSUME, V_DUPLICATE_SELECT,
                                   V_EXPIRED_REPLY, V_RESTATE)
from ..pending_producer import PendingProducer
from ..pending_revalidator import PendingRevalidator
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

# ---- P2.5b 冻结常量（评审 2026-11-19 开工批准条件） ----

# D2 方案甲：可信内部流来源标记（绝不由用户输入/LLM 输出伪造——
# 只由触点 1 的重验证 confirmed 路径写入 ctx）
_PENDING_DECISION_SOURCE = "pending_consume"
# 查询类 intent → 既有 QA 分支 kind 映射（IntentInQueryWhitelist =
# decision.DEFAULT_BATCHES，规划类永不放行；kind 须经既有分支入口
# session._answer 进入，Handler 零改动）
_QUERY_INTENT_KIND = {b: "poi" for b in DEFAULT_BATCHES}


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
        # P2.5a：生产侧装配（延迟构造——channel=0 永不构造，零开销旁路）
        self._producer: PendingProducer | None = None
        self._decider: DecisionEngine | None = None
        # P2.5b：重验证装配（同上，channel=0 永不构造，零开销旁路）
        self._revalidator: PendingRevalidator | None = None

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
                return self._pending_reply(out, v, session, rt, ctx)
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

    def _pending_reply(self, out, v, session, rt, ctx) -> Reply | None:
        """§2.5 冻结接线表：verdict → 回复行为（九类逐一，不增不减）。

        需要短路但无法按冻结格式构造内容（如 consume 成功但候选信息
        缺失、duplicate 记录缺失）时抛异常，由 gate 按 B3 规则兜底——
        绝不拼造数据、绝不伪造消费结果。

        P2.5b：V_CONSUME ok 分支在确认回复构造**之前**插入 fresh
        重验证（触点 1，§4.1 铁律——只决定「之后交给谁」，绝不反向
        改变已完成的消费）；其余八类 verdict 分支逐位不动。"""
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
                # ---- P2.5b 触点 1：消费成功 → fresh 重验证 ----
                re_out = self._revalidate_after_consume(
                    session, rt, v, cname, ccity, ctx)
                if re_out is not None:
                    return re_out     # 四分支成品回复 / 规划类确认话术
                # 查询类 confirmed：DecisionResult 已挂 ctx（来源
                # pending_consume），本轮 gate 不短路——由触点 2
                # （AllowHandler 五条件消费点）恒产出回复
                return None
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

    # -------------------------------------------------- P2.5b 重验证接入

    def _ensure_revalidator(self) -> PendingRevalidator:
        """延迟构造 revalidator（channel=0 永不构造，零开销旁路）。"""
        if self._revalidator is None:
            self._revalidator = PendingRevalidator()
        return self._revalidator

    @staticmethod
    def _consume_confirm_reply(session, selected_name: str,
                               selected_city: str | None) -> Reply:
        """P2.4b 冻结消费确认文案（规则 7：只确认消歧，不产生行程
        动作）。触点 1（规划类 confirmed）与触点 2（五条件不满足回退）
        共用同一来源，绝不双处维护分叉。"""
        return answer_reply(
            session,
            f"已确认：{selected_name}"
            + (f"（{selected_city}）" if selected_city else "")
            + "。消歧完成，如需调整行程请直接告诉我。")

    def _revalidate_after_consume(self, session, rt, v, cname: str,
                                  ccity, ctx) -> Reply | None:
        """P2.5b 触点 1：消费成功后的 fresh 重验证接入（§4.1：确认
        回复构造之前；§4.1 铁律——只决定「之后交给谁」，绝不反向
        改变已完成的消费）。

        返回 Reply → 四分支成品回复 / 规划类确认话术 / B3 兜底短路；
        返回 None → 查询类 confirmed，DecisionResult 已挂 ctx（来源
        pending_consume），交触点 2 恒产出回复。"""
        parse = getattr(ctx, "parse", None)
        try:
            re_out = self._ensure_revalidator().revalidate(
                rt.store,
                session_id=str(getattr(session, "pending_session_id",
                                       "") or ""),
                pending_id=str(getattr(v, "pending_id", "") or ""),
                candidate_id=str(getattr(v, "candidate_id", "") or ""),
                candidate_name=cname,
                candidate_city=(ccity or ""),
                # HC-1（§10.3）：用户明确 query_city ＞ LLM hints
                # （仅辅助）＞ destination（既有回退链）
                query_city=(getattr(parse, "query_city", "") or ""),
                destination=((getattr(parse, "destination", "") or "")
                             or ((getattr(session, "slots", {}) or {})
                                 .get("destination") or "")),
                reply_builder=lambda t: answer_reply(session, t))
        except Exception as exc:               # 防御（revalidate 不抛出）
            self._pending_warnings.append(
                f"pending revalidate 异常 {type(exc).__name__}: {exc}")
            return self._fallback_confirm_reply(session, cname, ccity)
        if re_out.status == "":
            # B3 兜底（§4.1）：重验证未产出业务结论（归属校验失败 /
            # resolve 异常 / 话术构造异常）→ 冻结确认短路，消费事实
            # 不撤销，shadow 归因已由 revalidator 记录
            self._pending_warnings.append(
                "pending revalidate: " + (re_out.detail or "未产出结论"))
            return self._fallback_confirm_reply(session, cname, ccity)
        if re_out.backfill_ok is False:
            # D3 契约 3：rowcount=0 不当作成功（shadow 已记录）+ 警告
            # （任何分支的回填失败都告警，业务流不变——F4）
            self._pending_warnings.append(
                "pending revalidate 回填失败: " + (re_out.detail or ""))
        if re_out.status == "confirmed":
            if re_out.decision is not None:
                # 查询类 confirmed：DecisionResult（fresh entity +
                # 全链归因 reason）挂 ctx 交触点 2（AllowHandler 五
                # 条件消费点）；来源标记只由本路径写入（可信内部流）
                ctx.pending_decision = re_out.decision
                ctx.pending_decision_source = _PENDING_DECISION_SOURCE
                ctx.pending_decision_utterance = re_out.original_utterance
                return None
            # 规划类 / HC-4 防御 confirmed → 只回冻结确认话术，
            # 绝不写行程（§1.1：消歧成功 ≠ 授权修改行程，A4）
            return self._consume_confirm_reply(session, cname, ccity)
        # ambiguous / unverified / rejected → 成品回复短路
        if re_out.status == "ambiguous" and re_out.new_pending_id:
            # hint 加速（非权威，§3.3 同源语义）：指向新 Pending
            session.active_pending_id = re_out.new_pending_id
        return re_out.reply if re_out.reply is not None else \
            self._fallback_confirm_reply(session, cname, ccity)

    # ---------------------------------------- P2.5b AllowHandler 消费点

    def _allow_handler_consume(self, session, utterance: str, ctx) \
            -> Reply | None:
        """P2.5b 触点 2：AllowHandler 五条件受控消费点（§10.1 D2
        方案甲首次落地——§9.4「分支入口先看接入点 A 是否给出
        DecisionResult」结构的建立；Handler 零改动）。

        ctx.pending_decision 为 None（非 pending 消费轮）→ 返回
        None，主流程逐位不变。五条件 AND（缺一不可）：
        ChannelEnabled ∧ SourcePendingConsume（可信内部流，绝不由
        用户输入/LLM 输出伪造）∧ ActionExecute ∧
        IntentInQueryWhitelist（decision.DEFAULT_BATCHES，规划类
        永不）∧ RevalidationConfirmed。

        全过 → 经**既有业务分支入口** session._answer（与 handle
        步骤 5 同一入口、同一 Handler 链）执行 Handler——
        GuardPipeline / 权限 / Handler 前置条件零绕过；任一条件不
        满足或注入失败 → 冻结确认话术交回，绝不裸调 Handler、绝不
        退回普通路由（防选择话术被普通意图解析重复执行）。"""
        decision = getattr(ctx, "pending_decision", None)
        if decision is None:
            return None                        # 非消费通道轮 → 逐位不变
        entity = getattr(decision, "entity", None) or {}
        ename = str(entity.get("name", "") or "")
        ecity = entity.get("city")
        confirm = lambda: self._consume_confirm_reply(session, ename, ecity)

        # ---- 五条件 AND（§10.1 定版）----
        allowed = (
            self._channel_enabled()                       # ① 开关
            and getattr(ctx, "pending_decision_source", None)
            == _PENDING_DECISION_SOURCE                   # ② 可信内部流
            and getattr(decision, "action", "") == ACTION_EXECUTE  # ③
            and (getattr(decision, "intent", "") or "")
            in DEFAULT_BATCHES                            # ④ 规划类永不
            and "pending_revalidate:"                     # ⑤ confirmed 归因
            in (getattr(decision, "reason", "") or "")
            and bool(entity.get("name")))                 # 注入实体非缺失
        if not allowed:
            return confirm()                  # 确认话术交回（不进 Handler）
        kind = _QUERY_INTENT_KIND.get(
            getattr(decision, "intent", "") or "")
        orig_ut = str(getattr(ctx, "pending_decision_utterance", "")
                      or "").strip()
        if kind is None or not orig_ut:
            # 注入前置缺失 → 回退确认话术，绝不裸调 Handler
            self._pending_warnings.append(
                "pending consume execute: 注入前置缺失（kind/utterance）")
            return confirm()
        try:
            # 既有业务分支入口（与 handle 步骤 5 同一入口同一链）；
            # DecisionResult 经 ctx 载体随轮可溯（§9.4 结构）
            text = session._answer(kind, orig_ut)
        except Exception as exc:
            self._pending_warnings.append(
                f"pending consume execute 注入失败 "
                f"{type(exc).__name__}: {exc}")
            return confirm()                  # 注入失败 → 回退确认话术
        if not text:
            # Handler 前置条件未接住（返回 None）→ 服从原规则（F3：
            # 不因消歧绕过权限/前置），回退确认话术
            return confirm()
        return answer_reply(session, text)

    # -------------------------------------------------- P2.5a 生产钩子

    def _ensure_producer(self) -> PendingProducer:
        """延迟构造 producer（channel=0 永不构造，零开销旁路）。"""
        if self._producer is None:
            self._producer = PendingProducer()
        return self._producer

    def _ensure_decider(self) -> DecisionEngine:
        """延迟构造决策引擎（同上；channel=0 永不构造）。"""
        if self._decider is None:
            self._decider = DecisionEngine()
        return self._decider

    def _pending_produce(self, session, utterance: str, ctx) -> Reply | None:
        """P2.5a 生产钩子（步骤 1.8）：ambiguous → 创建 Pending → 展示。

        channel=0 → 完全旁路：不调用决策链、不构造 producer/engine、
        零开销（§10.5 逐位回归）；身份缺失 → 旁路 + 警告（§2.3，
        绝不猜测身份）。

        决策链（§9.4 固定顺序，本钩子内首次实际接入）：extract →
        resolve → decide。decide 在 TAKEOVER=0 下经 D1 方案甲仅放行
        clarify；其他 action（含 TAKEOVER=1 批次内的 execute 等）一律
        交回原链路——本钩子绝不消费 execute（D2 AllowHandler 受控
        流转属 P2.5b）。决策链任何异常 → 保守交回原链路 + 警告。

        生产装配（§10.2 三步顺序 + 补偿矩阵）全部委托 producer；
        创建成功即设 hint（非权威，store 为准）。lease 时序与 gate
        同冻结口径：业务全部完成后 finally 归还。
        """
        if not self._channel_enabled():
            return None                        # 完全旁路（零决策链开销）
        sid = getattr(session, "pending_session_id", None)
        if not sid or not str(sid).strip():
            self._pending_warnings.append(
                "pending produce bypass: session 身份未注入"
                "（pending_session_id 缺失）")
            return None
        rt = self._pending_runtime(str(sid))
        try:
            try:
                # 决策链输入 context（HC-1 城市三级：resolve 期望城市
                # 走 slots.destination / trip_dest / last_city 既有规则）
                extract_ctx = {"plan_made": bool(
                                   getattr(session, "plan_made", False)),
                               "slots": getattr(session, "slots", {}) or {}}
                cands = _extract_candidates(utterance, extract_ctx)
                rctx = {"slots": getattr(session, "slots", {}) or {},
                        "trip_dest": (getattr(ctx.parse, "destination",
                                              "") or ""),
                        "last_city": (getattr(session, "query_city",
                                              "") or "")}
                rr = _resolve_entities(cands.poi_candidates, rctx)
                decision = self._ensure_decider().decide(
                    ctx.parse, rr.resolved, rctx,
                    intent=getattr(ctx.parse, "intent", "") or None)
            except Exception as exc:
                self._pending_warnings.append(
                    f"pending produce: 决策链异常 "
                    f"{type(exc).__name__}: {exc}")
                return None                    # 保守交回原链路
            if decision is None or decision.action != ACTION_CLARIFY:
                return None                    # 非 clarify → 原链路
            top = self._ensure_producer().pick_ambiguous(rr.resolved)
            if top is None:
                self._pending_warnings.append(
                    "pending produce: clarify 但 HC-4 排除后无可入快照候选")
                return None
            out = self._ensure_producer().produce(
                rt.store,
                utterance=utterance,
                intent=decision.intent,
                role=(top.original or {}).get("role") or "",
                matches=top.matches,
                reply_builder=lambda t: answer_reply(session, t))
            if out.reply is not None:
                # hint 加速（非权威）：错值最坏结果为 evaluate 产出
                # none 交回原链路，无误消费（§3.3 同源语义）
                session.active_pending_id = out.pending_id
                return out.reply
            return None
        finally:
            # lease 归还：全部 store 调用返回后（批准条件冻结时序）
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

        # ---- 1.77 P2.5b 触点 2：AllowHandler 受控消费点（D2 方案甲，
        # §9.4「分支入口先看 DecisionResult」结构首次落地）——仅消费
        # 通道重验证 confirmed 轮非旁路（ctx.pending_decision 非
        # None，触点 1 写入）；普通轮恒返回 None 逐位不变。全过 →
        # 经既有分支入口执行 Handler；否则冻结确认话术交回。
        controlled = self._allow_handler_consume(session, utterance, ctx)
        if controlled is not None:
            session.history.append({"role": "agent",
                                    "content": str(controlled)})
            ctx.route, ctx.handled = "pending_consume_execute", True
            return controlled

        # ---- 1.8 P2.5a 生产钩子：ambiguous → 创建 Pending → 展示快照
        # （接入点 A 决策链首次落地 + clarify 分支；channel=0 完全旁路
        # 零开销；短路文本与 Pending ID 已由 producer 按冻结 §10.2
        # 三步顺序与补偿矩阵完成装配）
        produced = self._pending_produce(session, utterance, ctx)
        if produced is not None:
            session.history.append({"role": "agent",
                                    "content": str(produced)})
            ctx.route, ctx.handled = "pending_produce", True
            return produced

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
