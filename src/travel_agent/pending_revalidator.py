"""P2.5b 重验证侧装配：消费成功 → fresh resolve → 四分支流转 + 回填。

设计依据 docs/PHASE5_P25_PLAN.md（设计基线 4171095，§10 定版条款
优先；开工范围 §14，评审批准 2026-11-19）：

- §4.1 铁律：重验证只发生在消费成功之后、确认回复构造之前；只决定
  「之后交给谁」，绝不反向改变 Pending 状态（原 Pending 保持
  consumed，绝不静默恢复 open）；任何一步异常 → 调用方 B3 兜底
  （_fallback_confirm_reply 确认短路，消费事实不撤销）；
- §4.2 fresh 输入边界：仅重验证被选中的那一个候选；candidate_id
  ∈ 原快照**再次校验**（不重复信任消费事务）；全新调用既有
  resolve()——绝不复用旧 matches / 旧 DecisionResult；probe_status
  如实标注（绝不把「没探测」伪装成「探测过没找到」）；
- §10.3 HC-1 双概念拆分：selected candidate city（快照冻结的候选
  身份属性）与 query context city（按 HC-1 规则重建：用户明确
  query_city ＞ LLM city_hints（仅辅助，绝不升级为期望城市）＞
  destination）互不替代；明确 query_city 与候选 city 冲突时按既有
  Resolver 规则重新判断，绝不因 Pending 已消费就强行 confirmed；
- §4.3 四分支：confirmed（查询类构造 DecisionResult(execute) 全链
  归因 / 规划类只确认不写行程）、ambiguous（fresh matches 生成新
  快照 → producer 创建新 Pending，旧 Pending 保持 consumed）、
  unverified（保守回复，绝不伪装 rejected）、rejected（规定数据源
  与检索范围内没有找到，保留原始表达）；
- §10.1 D3 六契约：仅在重验证真实完成且 resolve 产出四枚举业务
  结论时调用 record_revalidation；异常 / 未探测 / skipped 一律不
  回填（三列保持 NULL）；rowcount=0 / 回填异常仅 shadow 归因，
  绝不改业务流。

禁止清单：不修改 store 既有五方法契约（只新增调用
record_revalidation）；不触碰 evaluate/act；不读 shadow 决策记录；
Handler / Resolver 判定规则零依赖（只调用既有 resolve() 入口）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decision import ACTION_EXECUTE, DecisionResult
from .entity_resolver import (STATUS_AMBIGUOUS, STATUS_CONFIRMED,
                              STATUS_REJECTED, STATUS_SKIPPED,
                              STATUS_UNVERIFIED)
from .entity_resolver import resolve as _resolve_entities
from .pending import REVALIDATE_STATUSES
from .pending_producer import PendingProducer

# HC-4 角色红线（§3.1 同源）：negated / remove 永不进入 execute
# DecisionResult 构造（producer 已在快照层排除，此处纵深防御）。
_HC4_ROLES = ("negated", "remove")

# 规划类意图前缀（decision._PLAN_PREFIX 同值同源；规划类 confirmed
# 只回确认话术，绝不写行程——§1.1 红线）
_PLAN_PREFIX = "plan"

# ---- 话术（§4.3 冻结口径） ----
# unverified 保守回复（绝不判定为 rejected）
_CONSERVATIVE_TMPL = ("暂时查不到「{name}」的可靠信息，"
                      "您可以告诉我它所在的城市或更具体的位置，"
                      "我再帮您确认。")
# rejected 规定话术（§4.3/A7 冻结口径：保留用户原始表达——原始
# 问句为句子主体，候选名附带说明，二者不等价，测试须分别断言）
_REJECTED_TMPL = ("在规定数据源与检索范围内没有找到与您的问题"
                  "「{utterance}」对应的「{name}」。")
# 原始问句不可用时（理论不可达：original_utterance 来自原 Pending
# 快照，消费成功前必非空）的降级形式——仍不引入未证实表述
_REJECTED_NO_UTT_TMPL = "在规定数据源与检索范围内没有找到「{name}」。"
# ambiguous 重建失败（规则 8 撞车 / store 异常）的保守重试话术
# （§4.3：不覆盖、不轮换、不静默、绝不伪装成 confirmed）
_RETRY_NOTICE = ("系统暂时无法完成重新确认，请稍后重试；"
                 "您也可以换一种说法告诉我您的需求。")


@dataclass
class RevalidationOutcome:
    """revalidate() 的返回值（调用方 = orchestrator 触点 1）。

    status 为空串 ⇔ 重验证未能产出业务结论（归属校验失败 / resolve
    异常 / 话术构造异常等）→ 调用方按 B3 兜底确认短路（消费事实
    不撤销，shadow 归因已由本模块记录）。

    status = confirmed 且 decision 非 None ⇔ 查询类 confirmed：
    reply 为 None，由调用方将 decision 交给 AllowHandler 受控消费点
    （五条件 AND 全过才进 Handler，否则确认话术交回）。
    status = confirmed 且 decision 为 None ⇔ 规划类（或 HC-4 防御）
    confirmed：调用方回冻结确认话术，绝不写行程。
    其余分支 reply 恒为成品 Reply（短路返回）。"""

    status: str = ""                 # "" / confirmed / ambiguous / unverified / rejected
    probe_status: str | None = None  # fresh resolve 的如实探测标注
    reply: object | None = None      # 短路回复（confirmed 查询类为 None）
    decision: DecisionResult | None = None  # 仅查询类 confirmed 非 None
    new_pending_id: str | None = None       # 仅 ambiguous 重建成功非 None
    backfill_ok: bool | None = None  # 回填结果（True/False；未回填 None）
    effective_city: str = ""         # HC-1 重建的 query context city
    city_source: str = ""            # query_city / destination / ""
    original_utterance: str = ""     # 原 Pending 原始问句（触点 2 注入用）
    detail: str = ""                 # 归因（异常类型、skip 原因等）


class PendingRevalidator:
    """重验证装配器：归属再校验 + fresh resolve + 四分支 + 回填。

    纯装配层：判定全部委托既有 resolve()（单一决策层原则——本模块
    绝不复刻任何 Resolver/DecisionEngine 判定逻辑）；ambiguous 重建
    委托既有 PendingProducer（§10.2 三步顺序与补偿矩阵原样复用）。
    """

    def __init__(self, *, shadow_sink=None,
                 producer: PendingProducer | None = None) -> None:
        self._shadow_sink = shadow_sink          # 可选回调：record(dict)
        self._producer = producer                # None 时延迟构造
        self.shadow_log: list[dict] = []         # 重验证影子（纯记录）

    # -------------------------------------------------- 影子记录

    def _shadow(self, action: str, detail: str = "",
                pending_id: str | None = None,
                candidate_id: str | None = None,
                revalidate_status: str | None = None) -> None:
        """重验证影子记录（§14.4 预申报字段清单；B5 精神：记录自身
        一切异常被吞掉，绝不影响主链路与四分支流转）。"""
        try:
            rec = {"stage": "revalidator", "action": action,
                   "detail": detail, "pending_id": pending_id,
                   "candidate_id": candidate_id,
                   "revalidate_status": revalidate_status}
            self.shadow_log.append(rec)
            if self._shadow_sink is not None:
                self._shadow_sink(rec)
        except Exception:
            pass

    def _producer_of(self) -> PendingProducer:
        if self._producer is None:
            self._producer = PendingProducer()
        return self._producer

    # -------------------------------------------------- 主入口

    def revalidate(self, store, *, session_id: str, pending_id: str,
                   candidate_id: str, candidate_name: str,
                   candidate_city: str = "", original_role: str = "",
                   query_city: str = "", llm_city_hints=(),
                   destination: str = "",
                   reply_builder=None, resolver=None,
                   probe_fn=None, review_fn=None, kb_provider=None,
                   allow_online=None, budget=None) -> RevalidationOutcome:
        """消费成功后的 fresh 重验证（四分支 + 回填，绝不抛异常）。

        输入由调用方（orchestrator 触点 1）从消费事务结果与本轮
        parse 取得；本模块自行从 store 重读 Pending 完成 candidate_id
        归属再校验并取得 original_intent / original_utterance
        （不重复信任消费事务，§4.2）。candidate_city 为快照冻结的
        候选身份属性（§10.3 双概念之一，仅记录与身份约束，绝不替代
        query context city）。

        任何异常 → 返回 status=""（调用方 B3 兜底），异常详情进
        shadow，消费结果不被撤销。
        """
        try:
            return self._revalidate_impl(
                store, session_id=session_id, pending_id=pending_id,
                candidate_id=candidate_id, candidate_name=candidate_name,
                candidate_city=candidate_city, original_role=original_role,
                query_city=query_city, llm_city_hints=llm_city_hints,
                destination=destination, reply_builder=reply_builder,
                resolver=resolver, probe_fn=probe_fn, review_fn=review_fn,
                kb_provider=kb_provider, allow_online=allow_online,
                budget=budget)
        except Exception as exc:
            self._shadow("revalidate_exception",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=pending_id, candidate_id=candidate_id)
            return RevalidationOutcome(
                detail=f"revalidate 异常 {type(exc).__name__}: {exc}")

    # -------------------------------------------------- 实现体

    def _revalidate_impl(self, store, *, session_id, pending_id,
                         candidate_id, candidate_name, candidate_city,
                         original_role, query_city, llm_city_hints,
                         destination, reply_builder, resolver, probe_fn,
                         review_fn, kb_provider, allow_online,
                         budget) -> RevalidationOutcome:
        # ---- ① 归属再校验（§4.2：不重复信任消费事务，再次校验）----
        try:
            p = store.get_pending_by_id(pending_id)
        except Exception as exc:
            self._shadow("revalidate_locate_failed",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=pending_id, candidate_id=candidate_id)
            return RevalidationOutcome(
                detail=f"pending 定位失败 {type(exc).__name__}: {exc}")
        if p is None or p.status != "consumed":
            # memory 单槽被替换等场景：pending 不可回查 → 无法校验
            # 归属 → 保守交 B3（消费事实不撤销）
            self._shadow("revalidate_snapshot_unavailable",
                         detail=f"pending 状态 {getattr(p, 'status', None)}",
                         pending_id=pending_id, candidate_id=candidate_id)
            return RevalidationOutcome(detail="pending 不可回查或非 consumed")
        if candidate_id not in p.snapshot_candidate_ids():
            self._shadow("revalidate_candidate_not_in_snapshot",
                         detail="candidate_id 不在原快照内",
                         pending_id=pending_id, candidate_id=candidate_id)
            return RevalidationOutcome(detail="candidate_id 不在原快照内")
        original_intent = p.original_intent
        original_utterance = p.original_utterance
        if not original_role:
            original_role = p.original_role

        # ---- ② HC-1 双概念拆分（§10.3）：query context city 重建 ----
        # 用户明确 query_city ＞ LLM city_hints（仅辅助，绝不升级为
        # 期望城市）＞ destination（绝不覆盖用户明确指定）。
        # selected candidate city（candidate_city 参数 / 快照冻结值）
        # 仅作候选身份属性，绝不替代 query context city。
        qc = (query_city or "").strip()
        dc = (destination or "").strip()
        if qc:
            effective_city, city_source = qc, "query_city"
        elif dc:
            effective_city, city_source = dc, "destination"
        else:
            effective_city, city_source = "", ""
        hints = [str(h).strip() for h in (llm_city_hints or ())
                 if str(h or "").strip()]
        rctx: dict = {}
        if effective_city:
            rctx = {"slots": {"destination": effective_city}}
        self._shadow("revalidate_context_built",
                     detail=(f"query_city={qc!r} destination={dc!r} "
                             f"hints={hints!r}（仅辅助不入期望城市） "
                             f"candidate_city={candidate_city!r}"
                             f"（仅身份属性）"
                             f" → effective={effective_city!r}"),
                     pending_id=pending_id, candidate_id=candidate_id)

        # ---- ③ fresh resolve（§4.2：全新调用既有 resolve()，单候选；
        #       绝不复用旧 matches / 旧 DecisionResult）----
        resolve_fn = resolver or _resolve_entities
        cand = {"text": candidate_name, "entity_type": "unknown",
                "role": original_role or "query"}
        kw: dict = {}
        if probe_fn is not None:
            kw["probe_fn"] = probe_fn
        if review_fn is not None:
            kw["review_fn"] = review_fn
        if kb_provider is not None:
            kw["kb_provider"] = kb_provider
        if allow_online is not None:
            kw["allow_online"] = allow_online
        if budget is not None:
            kw["budget"] = budget
        rr = resolve_fn([cand], rctx, **kw)
        rc = (rr.resolved or [None])[0]
        if rc is None:
            self._shadow("revalidate_empty_result",
                         detail="resolve 未产出结果",
                         pending_id=pending_id, candidate_id=candidate_id)
            return RevalidationOutcome(detail="resolve 未产出结果")
        status = getattr(rc, "status", "")
        probe_status = getattr(rc, "probe_status", None) or None

        # ---- ④ skipped 防御（快照名来自 KB 真实 POI，理论不可达）：
        #       不回填（skipped 非四枚举合法值）、保守回复，绝不伪装
        if status == STATUS_SKIPPED:
            self._shadow("revalidate_skipped_unexpected",
                         detail="单候选被 resolve 判为 skipped（防御路径）",
                         pending_id=pending_id, candidate_id=candidate_id)
            return self._conservative_outcome(
                store, session_id, pending_id, candidate_id,
                candidate_name, probe_status, reply_builder,
                detail="unexpected skipped（不回填，绝不伪装业务结果）")

        # ---- ⑤ 四分支（§4.3 全表）----
        if status not in REVALIDATE_STATUSES:
            # 未知状态 → 保守（同 unverified 处置但不回填）
            self._shadow("revalidate_unknown_status",
                         detail=f"resolve 状态 {status!r} 非四枚举",
                         pending_id=pending_id, candidate_id=candidate_id)
            return self._conservative_outcome(
                store, session_id, pending_id, candidate_id,
                candidate_name, probe_status, reply_builder,
                detail=f"未知状态 {status!r}（不回填）")

        backfill_ok = self._backfill(
            store, session_id, pending_id, candidate_id, status,
            probe_status)

        if status == STATUS_CONFIRMED:
            return self._branch_confirmed(
                rc, original_intent=original_intent,
                original_role=original_role,
                original_utterance=original_utterance,
                pending_id=pending_id,
                candidate_id=candidate_id, effective_city=effective_city,
                city_source=city_source, probe_status=probe_status,
                backfill_ok=backfill_ok)
        if status == STATUS_AMBIGUOUS:
            return self._branch_ambiguous(
                rc, store=store, session_id=session_id,
                original_utterance=original_utterance,
                original_intent=original_intent,
                original_role=original_role, pending_id=pending_id,
                candidate_id=candidate_id, probe_status=probe_status,
                reply_builder=reply_builder, backfill_ok=backfill_ok)
        if status == STATUS_UNVERIFIED:
            return self._conservative_outcome(
                store, session_id, pending_id, candidate_id,
                candidate_name, probe_status, reply_builder,
                backfill_ok=backfill_ok)
        # STATUS_REJECTED
        return self._branch_rejected(
            candidate_name, original_utterance=original_utterance,
            probe_status=probe_status, reply_builder=reply_builder,
            backfill_ok=backfill_ok, pending_id=pending_id,
            candidate_id=candidate_id)

    # -------------------------------------------------- 分支实现

    def _branch_confirmed(self, rc, *, original_intent, original_role,
                          original_utterance,
                          pending_id, candidate_id, effective_city,
                          city_source, probe_status,
                          backfill_ok) -> RevalidationOutcome:
        """confirmed：查询类构造 DecisionResult(execute)（全链归因）；
        规划类 / HC-4 防御 → decision=None（调用方回冻结确认话术，
        绝不写行程）。绝不因 Pending 已消费就跳过 Resolver 重判——
        本分支只在 fresh resolve 真实产出 confirmed 时进入。"""
        intent = (original_intent or "").strip()
        plan_like = intent.startswith(_PLAN_PREFIX) \
            or (original_role or "") in _HC4_ROLES
        if plan_like:
            self._shadow("revalidate_confirmed_no_execute",
                         detail=(f"intent={intent!r} role={original_role!r}"
                                 " → 只确认，不构造 execute（规划类/HC-4）"),
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=STATUS_CONFIRMED)
            return RevalidationOutcome(
                status=STATUS_CONFIRMED, probe_status=probe_status,
                decision=None, backfill_ok=backfill_ok,
                effective_city=effective_city, city_source=city_source,
                detail="confirmed（规划类/HC-4 → 只确认话术）")
        matches = getattr(rc, "matches", None) or []
        first = matches[0] if matches else None
        entity = {
            "name": getattr(rc, "text", ""),
            "entity_id": (getattr(first, "source", "") or "") or None,
            "city": getattr(first, "city", "") if first else None,
            "category": getattr(first, "category", "") if first
            else (rc.original or {}).get("entity_type"),
            "status": getattr(rc, "status", ""),
        }
        reason = (f"pending_revalidate: pending_id={pending_id} "
                  f"candidate_id={candidate_id} "
                  f"revalidate=confirmed probe={probe_status} "
                  f"fresh_resolve_status={getattr(rc, 'status', '')}")
        decision = DecisionResult(
            action=ACTION_EXECUTE, intent=intent, entity=entity,
            city=effective_city or None,
            city_source=city_source or None,
            confidence=getattr(rc, "confidence", 0.0),
            reason=reason)
        self._shadow("revalidate_confirmed_execute_built",
                     detail=reason, pending_id=pending_id,
                     candidate_id=candidate_id,
                     revalidate_status=STATUS_CONFIRMED)
        return RevalidationOutcome(
            status=STATUS_CONFIRMED, probe_status=probe_status,
            reply=None, decision=decision, backfill_ok=backfill_ok,
            effective_city=effective_city, city_source=city_source,
            original_utterance=original_utterance,
            detail=reason)

    def _branch_ambiguous(self, rc, *, store, session_id,
                          original_utterance, original_intent,
                          original_role, pending_id, candidate_id,
                          probe_status, reply_builder,
                          backfill_ok) -> RevalidationOutcome:
        """ambiguous：fresh matches → 新候选快照 → producer 创建新
        Pending（新 ID、新 TTL）→ 展示新快照；旧 Pending 保持
        consumed 不复活；不执行 Handler。重建失败（规则 8 / store
        异常）→ 保守重试话术，绝不伪造成功。"""
        matches = getattr(rc, "matches", None) or []
        try:
            out = self._producer_of().produce(
                store, utterance=original_utterance,
                intent=original_intent, role=original_role or "query",
                matches=matches, reply_builder=reply_builder)
        except Exception as exc:
            self._shadow("revalidate_rebuild_exception",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=STATUS_AMBIGUOUS)
            return self._retry_outcome(reply_builder, pending_id,
                                       candidate_id, probe_status,
                                       backfill_ok,
                                       detail=f"重建异常 {type(exc).__name__}")
        if out.reply is not None and out.pending_id:
            self._shadow("revalidate_new_pending_created",
                         detail=(f"新 Pending={out.pending_id}"
                                 f"（旧 {pending_id} 保持 consumed）"),
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=STATUS_AMBIGUOUS)
            return RevalidationOutcome(
                status=STATUS_AMBIGUOUS, probe_status=probe_status,
                reply=out.reply, new_pending_id=out.pending_id,
                backfill_ok=backfill_ok,
                detail=f"新 Pending {out.pending_id}（fresh 快照）")
        # 重建失败（规则 8 拒绝 / store 异常 / 展示构造失败）：
        # §4.3 —— shadow 警告 + 保守重试话术，原 Pending 保持 consumed
        self._shadow("revalidate_rebuild_failed",
                     detail="新 Pending 创建未成功（不覆盖不轮换）",
                     pending_id=pending_id, candidate_id=candidate_id,
                     revalidate_status=STATUS_AMBIGUOUS)
        return self._retry_outcome(reply_builder, pending_id,
                                   candidate_id, probe_status,
                                   backfill_ok, detail="新 Pending 创建未成功")

    def _branch_rejected(self, candidate_name, *, original_utterance,
                         probe_status, reply_builder, backfill_ok,
                         pending_id, candidate_id) -> RevalidationOutcome:
        """rejected：规定话术（保留用户原始表达——§4.3/A7 冻结口径，
        原始问句与候选名均出现在回复中），不执行 Handler。"""
        ut = str(original_utterance or "").strip()
        if ut:
            text = _REJECTED_TMPL.format(name=candidate_name,
                                         utterance=ut)
        else:
            text = _REJECTED_NO_UTT_TMPL.format(name=candidate_name)
        reply = reply_builder(text) if reply_builder is not None else text
        self._shadow("revalidate_rejected_reply",
                     detail=f"probe={probe_status}（cross_source_empty 溯源）",
                     pending_id=pending_id, candidate_id=candidate_id,
                     revalidate_status=STATUS_REJECTED)
        return RevalidationOutcome(
            status=STATUS_REJECTED, probe_status=probe_status,
            reply=reply, backfill_ok=backfill_ok,
            detail="rejected（规定数据源与检索范围内没有找到）")

    def _conservative_outcome(self, store, session_id, pending_id,
                              candidate_id, candidate_name, probe_status,
                              reply_builder, backfill_ok=None,
                              detail="") -> RevalidationOutcome:
        """unverified：保守回复（绝不判定为 rejected），不执行
        Handler；不回填（skip/未知状态防御路径）或按传入结果回填。"""
        text = _CONSERVATIVE_TMPL.format(name=candidate_name)
        reply = reply_builder(text) if reply_builder is not None else text
        self._shadow("revalidate_conservative",
                     detail=detail or f"probe={probe_status}",
                     pending_id=pending_id, candidate_id=candidate_id,
                     revalidate_status=(STATUS_UNVERIFIED
                                        if backfill_ok is not None else None))
        return RevalidationOutcome(
            status=STATUS_UNVERIFIED, probe_status=probe_status,
            reply=reply, backfill_ok=backfill_ok,
            detail=detail or "unverified（保守回复，绝不伪装 rejected）")

    def _retry_outcome(self, reply_builder, pending_id, candidate_id,
                       probe_status, backfill_ok, detail) \
            -> RevalidationOutcome:
        """ambiguous 重建失败的保守重试话术（绝不伪造成功）。"""
        reply = (reply_builder(_RETRY_NOTICE)
                 if reply_builder is not None else _RETRY_NOTICE)
        return RevalidationOutcome(
            status=STATUS_AMBIGUOUS, probe_status=probe_status,
            reply=reply, backfill_ok=backfill_ok,
            detail=f"ambiguous 重建失败保守重试：{detail}")

    # -------------------------------------------------- 回填（D3 六契约）

    def _backfill(self, store, session_id, pending_id, candidate_id,
                  status, probe_status) -> bool:
        """按六契约调用 store.record_revalidation。

        - 仅四枚举真实业务结论才走到这里（异常/未探测/skipped 在
          上游已返回，三列保持 NULL——契约 6）；
        - rowcount=0 / store 异常由 store 返回 False（契约 2/3/4），
          本模块仅 shadow 归因，绝不改四分支流转（契约 4 / F4）。
        """
        try:
            ok = store.record_revalidation(
                session_id, pending_id, candidate_id, status,
                probe_status)
        except Exception as exc:
            self._shadow("revalidate_backfill_exception",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=status)
            return False
        if not ok:
            self._shadow("revalidate_backfill_failed",
                         detail="record_revalidation 返回 False"
                                "（rowcount=0 / 未命中 / 校验拒绝）",
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=status)
        else:
            self._shadow("revalidate_backfilled",
                         detail=f"status={status} probe={probe_status}",
                         pending_id=pending_id, candidate_id=candidate_id,
                         revalidate_status=status)
        return ok
