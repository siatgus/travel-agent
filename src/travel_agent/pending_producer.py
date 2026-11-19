"""P2.5a 生产侧装配：ambiguous 执行阻断 → 创建 Pending → 首轮展示快照。

设计依据 docs/PHASE5_P25_PLAN.md（设计基线 4171095，评审 PASS；
开工范围 00880ed §11，评审批准 2026-11-19）：

- §3.1：候选快照数据源 = clarify 关联的**本轮 fresh matches**（本轮
  resolver 刚产出，天然满足 D1 硬约束①「绝不从历史 shadow 记录、
  旧 Pending 或上轮 DecisionResult 恢复」——本模块结构上没有任何
  读取 shadow 决策记录或旧 Pending 的路径）；
- HC-4 永不进快照（§3.1 冻结红线）：negated / remove 角色候选绝不
  进入候选快照（决策层 deferred 之外的第二道纵深防御）；
- 规则 8 兜底：已有 active pending 时 create 拒绝 → 不覆盖不轮换，
  交回原链路（保守）；
- §10.2：展示内容**先于创建**构造（步骤 1 产出成品字符串，构造
  失败零副作用），创建后绑定/返回失败 → 立即取消孤儿 Pending；
  取消也失败 → 保守拦截回复，**绝不交回原链路**（防 open 孤儿被
  下轮误消费）。

禁止清单：不修改 store 五方法契约；不触碰 evaluate/act；不读
shadow 决策记录；无 Handler / Resolver 依赖。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .decision import ACTION_CLARIFY, DecisionResult  # noqa: F401  (契约引用)
from .entity_resolver import STATUS_AMBIGUOUS

# HC-4 角色红线（§3.1）：与 decision._HC4_ROLES 同值。decision 的
# 私有常量不便 import，双处同值由 P1 HC-4 负向测试 + 本模块 P1 测试
# 共同锁定；决策层已先行拦截（deferred），此处为纵深防御。
_HC4_ROLES = ("negated", "remove")

# 孤儿取消也失败时的保守拦截文案（§10.2：绝不交回原链路）
_ORPHAN_NOTICE = "系统暂时无法处理您的选择请求，请稍后重试。"


@dataclass
class ProduceOutcome:
    """produce() 的返回值。

    reply 为 None ⇔ 交回原链路（构建失败 / 规则 8 拒绝 / 孤儿已安全
    取消）；reply 非 None ⇔ 短路返回（正常展示或保守拦截），此时
    pending_id 为创建成功且未被补偿取消的 Pending ID。"""

    reply: object | None = None
    pending_id: str | None = None


class PendingProducer:
    """生产侧装配器：候选构建 + 展示话术 + 创建编排 + 孤儿补偿。

    纯装配层：候选数据全部来自调用方传入的本轮 fresh matches，
    不做任何 ambiguous 判定（判定在 DecisionEngine——方案丙的
    「判定复刻」被 D1 冻结禁止；本模块的 pick_ambiguous 只做
    「决策结果对应候选的数据定位」，排序口径与 DecisionEngine
    同档内排序一致，绝不改变档位）。"""

    def __init__(self, *, id_gen=None, shadow_sink=None) -> None:
        self._id_gen = id_gen or (lambda: uuid.uuid4().hex[:12])
        self._shadow_sink = shadow_sink          # 可选回调：record(dict)
        self.shadow_log: list[dict] = []         # 生产侧影子记录（纯记录）

    # -------------------------------------------------- 影子记录

    def _shadow(self, action: str, detail: str = "",
                pending_id: str | None = None) -> None:
        """生产侧影子记录（B5 精神：记录自身一切异常被吞掉，绝不
        影响主链路、绝不改变补偿矩阵分支）。"""
        try:
            rec = {"stage": "producer", "action": action,
                   "detail": detail, "pending_id": pending_id}
            self.shadow_log.append(rec)
            if self._shadow_sink is not None:
                self._shadow_sink(rec)
        except Exception:
            pass

    # -------------------------------------------------- 候选构建

    def pick_ambiguous(self, resolved):
        """从本轮 resolver 结果定位决策对应的 top ambiguous 候选。

        仅数据定位（非判定）：HC-4 角色排除（红线）→ 与
        DecisionEngine._sort_by_confidence 同口径排序（-confidence,
        -len(text)；只在档内排序，绝不改变档位）。无可入快照候选
        返回 None（交回原链路）。"""
        ambs = [r for r in (resolved or [])
                if getattr(r, "status", "") == STATUS_AMBIGUOUS
                and ((getattr(r, "original", None) or {}).get("role")
                     not in _HC4_ROLES)]
        if not ambs:
            return None
        return sorted(ambs,
                      key=lambda r: (-getattr(r, "confidence", 0.0),
                                     -len(getattr(r, "text", "") or "")))[0]

    def build_candidates(self, matches, *, id_gen=None) -> list[dict]:
        """本轮 fresh matches → 六字段候选快照 dict 列表（§3.1）。

        - display_order 1..N 按 matches 既有顺序（Resolver 产出序，
          绝不重排——重排即伪造 Resolver 判定）；
        - candidate_id 由生成器产出（快照内唯一性由
          PendingDisambiguation.__post_init__ 校验兜底）；
        - entity_id ← MatchRow.source；address ← MatchRow.area；
        - name 为空的命中行视为非法数据 → 抛异常（由 produce 归入
          步骤 1 构造失败：零副作用交回原链路，绝不降质入快照）；
        - 空匹配 → 空列表（调用方交回原链路）。
        """
        gen = id_gen or self._id_gen
        out: list[dict] = []
        for i, m in enumerate(matches or [], 1):
            name = str(getattr(m, "name", "") or "").strip()
            if not name:
                raise ValueError(f"第 {i} 条 match 缺少合法 name，"
                                 "拒绝构造降质快照")
            out.append({
                "candidate_id": gen(),
                "display_order": i,
                "entity_id": (getattr(m, "source", "") or "") or None,
                "name": name,
                "city": str(getattr(m, "city", "") or ""),
                "address": (getattr(m, "area", "") or "") or None,
            })
        return out

    # -------------------------------------------------- 展示话术

    @staticmethod
    def clarify_reply_text(candidates) -> str:
        """首轮展示话术（§3.3：快照 1..N 列表，与 restate 接线同构：
        名称 + 城市，序号冻结；不重复角色/意图解释）。

        必须在 create_pending **之前**构造为成品字符串（§10.2 步骤 1）；
        构造失败由 produce 归入步骤 1（零副作用交回原链路）。"""
        lines = []
        for c in candidates:
            line = f"{c['display_order']}. {c['name']}"
            if c.get("city"):
                line += f"（{c['city']}）"
            lines.append(line)
        return ("当前候选如下：\n" + "\n".join(lines)
                + "\n回复序号即可确认。")

    # -------------------------------------------------- 编排（§10.2 三步）

    def produce(self, store, *, utterance: str, intent: str, role: str,
                matches, reply_builder, id_gen=None) -> ProduceOutcome:
        """生产编排（§10.2 冻结顺序 + 补偿矩阵全矩阵）。

        store：PendingStore 双实现任一（只调用既有 create_pending /
        cancel_pending 契约，零修改）；reply_builder(text) -> Reply
        由调用方提供（orchestrator 传 answer_reply 偏函数）。

        返回 ProduceOutcome：reply=None ⇔ 交回原链路。
        """
        # ---- 步骤 1：候选构建 + 展示内容（先于创建；失败零副作用）----
        try:
            cands = self.build_candidates(matches, id_gen=id_gen)
            if not cands:
                self._shadow("produce_skip", detail="无有效候选")
                return ProduceOutcome(None, None)
            text = self.clarify_reply_text(cands)
        except Exception as exc:
            self._shadow("produce_display_build_failed",
                         detail=f"{type(exc).__name__}: {exc}")
            return ProduceOutcome(None, None)      # 未创建，零副作用

        # ---- 步骤 2：创建（规则 8 拒绝 → 不覆盖不轮换，交回原链路）----
        try:
            p = store.create_pending(utterance, intent, role, cands)
        except ValueError as exc:      # 规则 8：已存在 active pending
            self._shadow("produce_rejected_rule8", detail=str(exc))
            return ProduceOutcome(None, None)
        except Exception as exc:       # 其他 store 异常 → 同保守交回
            self._shadow("produce_create_failed",
                         detail=f"{type(exc).__name__}: {exc}")
            return ProduceOutcome(None, None)

        # ---- 步骤 3：绑定返回（失败 → 孤儿补偿矩阵）----
        try:
            reply = reply_builder(text)
        except Exception as exc:
            self._shadow("produce_bind_failed",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=p.pending_id)
            return self._compensate_orphan(store, p.pending_id,
                                           reply_builder)

        return ProduceOutcome(reply, p.pending_id)

    def _compensate_orphan(self, store, pending_id: str,
                           reply_builder) -> ProduceOutcome:
        """孤儿 Pending 补偿（§10.2 失败矩阵第三行）。

        立即尝试取消孤儿；取消成功 → shadow 记录 + 交回原链路；
        取消也失败 → 保守拦截回复，**绝不交回原链路**（防 open 孤儿
        被下轮误消费）+ shadow 记录 orphan Pending。"""
        try:
            out = store.cancel_pending(pending_id)
        except Exception as exc:
            self._shadow("produce_orphan_cancel_failed",
                         detail=f"cancel 抛异常 {type(exc).__name__}: {exc}",
                         pending_id=pending_id)
            return ProduceOutcome(reply_builder(_ORPHAN_NOTICE), pending_id)
        if getattr(out, "ok", False):
            self._shadow("produce_orphan_cancelled",
                         pending_id=pending_id)
            return ProduceOutcome(None, None)      # 孤儿已安全取消
        # cancel 返回 ok=False：状态异常（rowcount=0 同源语义）→
        # 无法确认孤儿已被移出可消费态 → 保守拦截，绝不交回原链路
        self._shadow("produce_orphan_cancel_failed",
                     detail=str(getattr(out, "reason", "cancel ok=False")),
                     pending_id=pending_id)
        return ProduceOutcome(reply_builder(_ORPHAN_NOTICE), pending_id)
