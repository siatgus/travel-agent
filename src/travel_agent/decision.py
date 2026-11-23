"""DecisionEngine：五档定档 → 六档 action → DecisionResult（Phase 5 P1）。

第三、四阶段回答了「用户说了什么 / 候选是谁」；本模块回答
「**系统现在应该做什么**」。设计依据 docs/PHASE5_DESIGN.md v3.2.1：

- **五档 status = DecisionEngine 内部的实体决策状态**（实体验证后处于
  什么状态），**六档 action = Orchestrator 的运行时路由动作**（下一步
  允许做什么）。两者严格分离：DecisionResult 没有 status 字段，
  status 只在本模块内部参与定档，对外只暴露 action。
- 流水线顺序（§3）：硬约束检查 → 存在性与唯一性 → 置信度 → 分流。
  **confidence 只做同档内排序，永不跨档救回被硬约束拦下的记录。**

红线（P1 验收标准，tests/test_decision.py 逐条锁定）：
1. 错误城市不能被高置信度覆盖（HC-1：按意图选城市字段，query_city
   ≠ destination 绝不判冲突；冲突 → clarify，0.92 分也不改判）。
2. restaurant/attraction 类型冲突不能直接 execute（HC-2）。
3. 同名分店未消歧不能静默选中一个（HC-3）。
4. 单源空结果不能映射为 rejected → 只能 conservative（HC-6）。
5. entity=None 时不能返回 execute（构造校验）。
6. pending 只能出现在 clarify（构造校验）。
7. skipped 只能来自品类词/城市（§2.5 入口收紧），普通无候选结果
   → decide 返回 None（未形成结论），绝不自动产生 skipped。
8. deferred 不是实体状态，不进五档状态机——它是执行策略层「已决策
   但明确交回」的动作（negated/remove 角色走 deferred，HC-4）。
9. TAKEOVER=0（默认）：只记录影子决策并返回 None，原有业务链路
   行为不变；本模块为纯函数式，不修改 context/slots/行程。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .entity_resolver import (
    STATUS_CONFIRMED,
    STATUS_AMBIGUOUS,
    STATUS_REJECTED,
    STATUS_UNVERIFIED,
    STATUS_SKIPPED,
    REASON_CROSS_SOURCE_EMPTY,
    AMBIG_REASON_TYPE,
)
# R-01（I-2 + I-3）：影子容器有界化共享机制（纯标准库模块，无反向依赖）
from .retention import (LIM_ENTITY, LIM_ENTITY_CITY, LIM_REASON,
                        SHADOW_CAPACITY, STATUSES_MAX_ITEMS, STATUS_ITEM_MAX,
                        RetentionInvariantError, append_bounded,
                        clip_str_list, coerce_text, new_stats)

# ---- 六档 action（v3.2.1 §2.3 冻结枚举） ----
ACTION_EXECUTE = "execute"              # 已确认，进入原业务
ACTION_CLARIFY = "clarify"              # 执行阻断，只做消歧
ACTION_REJECT_REPLY = "reject_reply"    # 规定范围内没找到，拒绝路径
ACTION_CONSERVATIVE = "conservative"    # 暂无法核实，保守处理
ACTION_CATEGORY_PATH = "category_path"  # 品类/泛化 Handler
ACTION_DEFERRED = "deferred"            # 已决策但明确交回原流程

VALID_ACTIONS = (ACTION_EXECUTE, ACTION_CLARIFY, ACTION_REJECT_REPLY,
                 ACTION_CONSERVATIVE, ACTION_CATEGORY_PATH, ACTION_DEFERRED)

# ---- 五档 → 六档固定映射（v3.2.1 §2.3.1；deferred 不在此表：
#      它是执行策略层动作，不是任何实体状态的映射结果） ----
STATUS_TO_ACTION = {
    STATUS_CONFIRMED: ACTION_EXECUTE,
    STATUS_AMBIGUOUS: ACTION_CLARIFY,
    STATUS_REJECTED: ACTION_REJECT_REPLY,
    STATUS_UNVERIFIED: ACTION_CONSERVATIVE,
    STATUS_SKIPPED: ACTION_CATEGORY_PATH,
}

# 默认接管批次白名单（TAKEOVER=1 时生效；5.1 只读查询第一批）
DEFAULT_BATCHES = ("poi_query", "poi_qa", "poi_location_query")

# HC-4：这两个角色永不进入新增行程路径（验证照常，处置交回既有链路）
_HC4_ROLES = ("negated", "remove")

_PLAN_PREFIX = "plan"

# ---- P2.6 增量 3（评审 2026-11-20 批准）：影子记录字段契约 ----

# 新增的四个**观察字段**。纯记录用途，供测试与文档核对字段口径；
# **不是决策输入**——不得被 _build() / decide() / DecisionResult 读取，
# 也不得据此改变任何判定分支（tests/test_pending_channel.py 有断言锁定）。
SHADOW_OBSERVATION_KEYS = ("city_source", "entity_city",
                           "confidence", "candidates_count")

# 影子记录的**完整键集合**（11 键 = 既有 7 键 + 上述 4 观察字段）。
# Orchestrator 侧按本元组做**白名单投影**复制记录副本（不是整条
# dict(...) 复制）——生产侧写入的键即此集合，不多不少。
SHADOW_RECORD_KEYS = ("intent", "takeover", "batch_enabled", "action",
                      "reason", "entity_text", "statuses",
                      ) + SHADOW_OBSERVATION_KEYS


@dataclass
class DecisionResult:
    """Orchestrator 的唯一业务出口（v3.2.1 §2.3 冻结契约）。

    无 status、无 route、无 candidates 字段。"""
    action: str                    # 六枚举之一
    intent: str                    # 细分意图
    entity: dict | None = None     # 归并后的已验证实体（可含 entity_id）
    city: str | None = None        # 本次决策实际采用的城市值
    city_source: str | None = None  # query_city / destination / departure_city
    confidence: float = 0.0        # 同档内排序用，永不跨档
    reason: str = ""               # 可归因说明（影子归因，不决定回复文案）
    pending: object | None = None  # PendingDisambiguation；仅 clarify 携带

    def __post_init__(self) -> None:
        if self.action not in VALID_ACTIONS:
            raise ValueError(f"非法 action：{self.action!r}，"
                             f"允许值 {VALID_ACTIONS}")
        if self.entity is None and self.action == ACTION_EXECUTE:
            raise ValueError("entity=None 时不得返回 execute（P1 验收点5）")
        if self.pending is not None and self.action != ACTION_CLARIFY:
            raise ValueError("pending 只能出现在 clarify（P1 验收点6）")
        if self.reason and "must_visit" in self.reason:
            # HC-4 防呆：决策出口不携带任何 must_visit 语义
            raise ValueError("DecisionResult 不得携带 must_visit 语义")

    def to_dict(self) -> dict:
        pending = None
        if self.pending is not None:
            pending = (self.pending.as_dict()
                       if hasattr(self.pending, "as_dict") else dict(self.pending))
        return {
            "action": self.action,
            "intent": self.intent,
            "entity": dict(self.entity) if self.entity else None,
            "city": self.city,
            "city_source": self.city_source,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "pending": pending,
        }


class DecisionEngine:
    """消费 ParseResult + resolved 实体 + context，产出 DecisionResult。

    纯决策层：不指定具体 Handler（action=execute 后由既有 intent 分支
    决定路由）、不修改行程、不写 slots；TAKEOVER=0 时只记影子。
    """

    def __init__(self, *, batches: tuple[str, ...] | None = None,
                 shadow_sink=None) -> None:
        self.batches = tuple(batches) if batches else DEFAULT_BATCHES
        self.shadow_sink = shadow_sink        # 可选回调：record(dict)
        self.shadow_log: list[dict] = []      # 影子决策记录（纯记录）
        # ---- R-01（I-2）：S2 容量治理状态（惰性，不在此构造 stats）----
        # shadow_dead：跨轮失效标记——append_bounded 抛
        # RetentionInvariantError 后置 True，此后不再追加（容器不可判定时
        # 禁止继续写入）；shadow_stats：首次**成功**追加后才就位
        # （I-3 检测点读它的 total_appended 判断本轮是否产生了新记录）。
        self.shadow_dead = False
        self.shadow_stats = None

    # -------------------------------------------------- 主入口

    def decide(self, parse_result, resolved, context: dict | None = None,
               *, takeover: bool | None = None,
               intent: str | None = None) -> DecisionResult | None:
        """决策主入口（设计 §9.4 接入点 A）。

        返回值三态（v3.2.1 §2.3.1 判定表）：
        - 未形成结论（无实体候选等）→ None；
        - TAKEOVER=0 或意图不在批次白名单 → None（影子已记录）；
        - 已形成有效实体决策且接管 → DecisionResult。
        """
        ctx = context or {}
        it = intent or getattr(parse_result, "intent", "") or ""
        coarse = getattr(parse_result, "intent_coarse", "") or ""
        takeover_on = (os.environ.get("TRAVEL_DECISION_TAKEOVER", "")
                       .strip() == "1") if takeover is None else bool(takeover)

        result = self._build(parse_result, resolved, ctx, it, coarse)
        self._shadow(result, it, resolved, takeover_on,
                     it in self.batches)
        if result is None:
            return None
        if not takeover_on or it not in self.batches:
            # 未接管：deferred 双语义中的 None 形态——决策已形成但本步
            # 不消费，交回原流程（旧链路行为零改变）
            # P2.5a（D1 方案甲，评审 2026-11-19 冻结，仅此一处放行）：
            # TAKEOVER=0 时仅 clarify 允许离开影子进入生产侧——
            # clarify 是「请求用户消歧」，不是业务执行，不属于
            # TAKEOVER 门控的执行类接管边界。放行返回的即本轮真实
            # DecisionResult（关联候选来自本轮 resolver 结果，绝不从
            # 历史 shadow 记录、旧 Pending 或上轮 DecisionResult 恢复）。
            # channel=0 或其他 action（execute / reject_reply /
            # conservative / deferred / category_path）：原行为逐位保持；
            # TAKEOVER=1 时本分支整体不进入，原门控一字不改。
            if (not takeover_on
                    and result.action == ACTION_CLARIFY
                    and os.environ.get("TRAVEL_PENDING_CHANNEL",
                                       "0").strip() == "1"):
                return result
            return None
        return result

    # -------------------------------------------------- 影子记录

    def _shadow(self, result, intent, resolved, takeover_on,
                batch_on) -> None:
        """写一条影子决策记录（R-01：有界 + 失效标记）。

        写入顺序（评审 2026-11-23 修订 2）：``append_bounded`` 成功 →
        提交 stats → 调用 sink。sink 失败不回滚记录、不丢计数。
        只捕获 ``RetentionInvariantError``（置 dead 后返回）；``_build``
        之后的任何其他异常仍按原行为外抛，业务边界不变。
        """
        if self.shadow_dead:
            return                                # 跨轮失效：不再写该容器
        entity = (result.entity if result else None) or {}
        rec = {
            "intent": intent,
            "takeover": takeover_on,
            "batch_enabled": batch_on,
            "action": result.action if result else None,
            "reason": coerce_text(result.reason if result else "未形成结论",
                                  LIM_REASON),
            "entity_text": coerce_text(
                (result.entity or {}).get("name") if result else None,
                LIM_ENTITY),
            "statuses": clip_str_list(
                [getattr(r, "status", "") for r in (resolved or [])],
                STATUSES_MAX_ITEMS, STATUS_ITEM_MAX),
            # ---- P2.6 增量 3（纯记录，不参与决策）----
            # 取值对象是 _build() 的返回值 result（见 PHASE5_P25_PLAN
            # §25.4.2 调用链），与本方法所属 decide() 的对外返回值无关：
            # TAKEOVER=0 时 decide() 返回 None，此处仍有本轮真实决策值。
            "city_source": result.city_source if result else None,
            "entity_city": coerce_text(entity.get("city"), LIM_ENTITY_CITY),
            "confidence": result.confidence if result else None,
            "candidates_count": len(resolved or []),
        }
        stats = self.shadow_stats
        if stats is None:
            # 惰性构造：current_size 取容器真实 len（不假设 0）
            stats = new_stats(self.shadow_log, SHADOW_CAPACITY)
        try:
            append_bounded(self.shadow_log, rec, stats)
        except RetentionInvariantError:
            # 容器不可判定：先置失效，本轮不写、不重试、不修补
            self.shadow_dead = True
            return
        if self.shadow_stats is None:
            self.shadow_stats = stats              # 成功后才提交（先于 sink）
        if self.shadow_sink is not None:
            try:
                self.shadow_sink(rec)
            except Exception:
                pass  # 影子通道绝不影响业务

    # -------------------------------------------------- 定档流水线

    def _build(self, parse_result, resolved, ctx, intent,
               coarse) -> DecisionResult | None:
        """硬约束 → 存在性与唯一性 → 置信度 → 分流（§3 顺序）。"""
        city, city_source = self._intent_city(parse_result, coarse, ctx)

        pois = [r for r in (resolved or [])
                if getattr(r, "status", "") != STATUS_SKIPPED]
        skipped_only = bool(resolved) and not pois

        # 验收点7：普通无候选结果 → None（未形成结论），绝不自动 skipped
        if not resolved:
            return None
        if skipped_only:
            # 品类词/城市（§2.5 收紧入口：只有这类才 skipped）
            return DecisionResult(
                action=ACTION_CATEGORY_PATH, intent=intent,
                city=city or None, city_source=city_source,
                reason="全部候选为品类词/城市（§2.5 合法 skipped 入口）"
                       "→ 走泛化链")

        # ---- HC-4 角色闸（negated/remove 永不进入新增路径） ----
        primary = self._pick_primary(pois)
        role = (primary.original or {}).get("role") or ""
        if role in _HC4_ROLES:
            # 已形成结论（该实体验证照常），但处置交回既有链路：
            # negated → 现有排除逻辑；remove → 现有删除链路。
            return DecisionResult(
                action=ACTION_DEFERRED, intent=intent,
                entity=self._entity_of(primary),
                city=city or None, city_source=city_source,
                confidence=primary.confidence,
                reason=f"HC-4：角色 {role} 交回既有"
                       + ("排除" if role == "negated" else "删除")
                       + "链路，不进新增路径")

        # ---- 有效状态重估（HC-1：按意图选城市字段） ----
        effective = [(r, self._effective_status(r, city)) for r in pois]

        # ---- 存在性与唯一性（amb > rejected > unverified > confirmed，
        #      最约束优先；同档内才看置信度排序） ----
        ambs = [(r, st) for r, st in effective if st == STATUS_AMBIGUOUS]
        if ambs:
            r = self._top(ambs)
            return DecisionResult(
                action=ACTION_CLARIFY, intent=intent, entity=None,
                city=city or None, city_source=city_source,
                confidence=r.confidence,
                reason=f"{self._ambig_reason_text(r)}（HC-{self._ambig_hc(r)}）"
                       "→ 执行阻断，请求用户消歧")

        rejs = [r for r, st in effective if st == STATUS_REJECTED]
        if rejs:
            r = self._top([(x, "") for x in rejs])
            # HC-6 证据闸复检：rejected 必须双源充分证据，否则降保守
            if getattr(r, "probe_reason", "") != REASON_CROSS_SOURCE_EMPTY:
                return DecisionResult(
                    action=ACTION_CONSERVATIVE, intent=intent, entity=None,
                    city=city or None, city_source=city_source,
                    confidence=r.confidence,
                    reason="HC-6：rejected 证据不充分，降级保守处理")
            return DecisionResult(
                action=ACTION_REJECT_REPLY, intent=intent, entity=None,
                city=city or None, city_source=city_source,
                confidence=r.confidence,
                reason="双源复核均无匹配（规定数据源与检索范围内没有找到）"
                       "→ 拒绝路径，保留原始表达")

        unvs = [r for r, st in effective if st == STATUS_UNVERIFIED]
        if unvs:
            r = self._top([(x, "") for x in unvs])
            detail = getattr(r, "probe_status", "")
            return DecisionResult(
                action=ACTION_CONSERVATIVE, intent=intent, entity=None,
                city=city or None, city_source=city_source,
                confidence=r.confidence,
                reason=f"证据不足保持未验证（probe={detail}，"
                       "含单源空结果）→ 保守处理，不执行实体业务")

        confs = [(r, st) for r, st in effective if st == STATUS_CONFIRMED]
        if confs:
            r = self._top(confs)
            # 确认后仍复检 HC-2 类型（resolver 已保证，此处防演化回归）
            if getattr(r, "type_check", "") == "mismatch":
                return DecisionResult(
                    action=ACTION_CLARIFY, intent=intent, entity=None,
                    city=city or None, city_source=city_source,
                    confidence=r.confidence,
                    reason="HC-2：类型冲突不能直接 execute → 消歧")
            return DecisionResult(
                action=ACTION_EXECUTE, intent=intent,
                entity=self._entity_of(r),
                city=city or None, city_source=city_source,
                confidence=r.confidence,
                reason="唯一匹配 + 城市/类型一致（硬约束全过）→ 进入原业务")

        return None  # 未形成结论（全部候选状态未知等极端情形）

    # -------------------------------------------------- HC 子步

    def _intent_city(self, parse_result, coarse, ctx):
        """HC-1：按意图选城市字段（v3.2.1 §3）。

        规划类 → destination；查询类 → query_city（缺省回退
        destination 继承值）；departure_city 仅路线框架不作归属判定。
        query_city ≠ destination 绝不判冲突。"""
        qc = getattr(parse_result, "query_city", None) or ""
        dc = getattr(parse_result, "destination", None) or ""
        if not dc:
            dc = ((ctx.get("slots") or {}).get("destination") or "").strip()
        is_plan = (coarse or "").startswith(_PLAN_PREFIX) or \
                  (getattr(parse_result, "intent", "") or "").startswith(
                      _PLAN_PREFIX)
        if is_plan:
            return dc.strip(), "destination"
        if qc.strip():
            return qc.strip(), "query_city"
        return dc.strip(), "destination"   # 查询类回退继承目的地

    def _effective_status(self, rc, intent_city: str) -> str:
        """HC-1 重估：查询类用意图城市重判跨城/多城歧义。

        - cross_city_mismatch 且意图城市命中 → confirmed（旧链路拿
          destination 比对造成的误判，由决策层修正——合理决策差异）；
        - multi_city 且意图城市恰好唯一命中 → confirmed；
        - branch_same_name / type_mismatch 保留 ambiguous（分店与类型
          消歧必须由用户完成，绝不静默选择）。
        """
        st = getattr(rc, "status", "")
        if st != STATUS_AMBIGUOUS:
            return st
        reason = getattr(rc, "ambiguity_reason", "")
        matches = getattr(rc, "matches", None) or []
        hit = [m for m in matches if getattr(m, "city", "") == intent_city]
        if intent_city and hit:
            if reason == "cross_city_mismatch":
                return STATUS_CONFIRMED      # HC-1 修正
            if reason == "multi_city" and len({m.city for m in hit}) == 1:
                return STATUS_CONFIRMED
        return st

    def _pick_primary(self, pois):
        """主候选：优先 role=query（查询语境），否则第一条。

        只选展示与归因主对象，绝不静默替用户挑分店（HC-3 的实体
        选择永远留给 clarify 通道）。"""
        for r in pois:
            if (r.original or {}).get("role") == "query":
                return r
        return pois[0]

    def _sort_by_confidence(self, pairs):
        """同档内排序：confidence → 匹配稳定性（名称长度）。

        只在档内排序，永不改变档位（硬约束已先判定）。"""
        return sorted(
            pairs,
            key=lambda p: (-getattr(p[0], "confidence", 0.0),
                           -len(getattr(p[0], "text", "") or "")))

    def _top(self, pairs):
        """同档内最优元素（排序不跨档）。"""
        return self._sort_by_confidence(pairs)[0][0]

    def _ambig_reason_text(self, rc) -> str:
        m = {"multi_city": "跨城同名", "branch_same_name": "同名分店",
             "cross_city_mismatch": "城市归属冲突", "type_mismatch": "类型冲突",
             "multi_online": "在线多候选"}
        return m.get(getattr(rc, "ambiguity_reason", ""), "多候选歧义")

    def _ambig_hc(self, rc) -> str:
        m = {"multi_city": "1", "branch_same_name": "3",
             "cross_city_mismatch": "1", "type_mismatch": "2",
             "multi_online": "3"}
        return m.get(getattr(rc, "ambiguity_reason", ""), "3")

    def _entity_of(self, rc) -> dict:
        """已验证实体 dict（含 entity_id 供后续追踪；绝不携带 role
        语义——HC-4 的 must_visit 永远不出现在决策出口）。"""
        matches = getattr(rc, "matches", None) or []
        first = matches[0] if matches else None
        return {
            "name": getattr(rc, "text", ""),
            "entity_id": (getattr(first, "source", "") or "") or None,
            "city": getattr(first, "city", "") if first else None,
            "category": getattr(first, "category", "") if first
            else (rc.original or {}).get("entity_type"),
            "status": getattr(rc, "status", ""),
        }
