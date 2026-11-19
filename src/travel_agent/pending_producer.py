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
  取消也失败 → **二线 expire 终结补偿**（§11.R3.1 / R4 定性：
  尽力而为，成功 → 孤儿终态、隔离可验证 → 交回原链路；失败 →
  无后续隔离保证 → 保守拦截回复，**绝不交回原链路**，残余窗口
  由生产开关冻结规避——§11.R3.2）。

禁止清单：不修改 store 五方法契约；不触碰 evaluate/act；不读
shadow 决策记录；无 Handler / Resolver 依赖。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .decision import ACTION_CLARIFY, DecisionResult  # noqa: F401  (契约引用)
from .dialogue import Reply   # P2.5a-R2：保守回复第二层自建用（只读不修改）
from .entity_resolver import STATUS_AMBIGUOUS

# HC-4 角色红线（§3.1）：与 decision._HC4_ROLES 同值。decision 的
# 私有常量不便 import，双处同值由 P1 HC-4 负向测试 + 本模块 P1 测试
# 共同锁定；决策层已先行拦截（deferred），此处为纵深防御。
_HC4_ROLES = ("negated", "remove")

# 孤儿取消也失败时的保守拦截文案（§10.2：绝不交回原链路）
_ORPHAN_NOTICE = "系统暂时无法处理您的选择请求，请稍后重试。"


class PendingProduceError(RuntimeError):
    """保守拦截回复构造失败（理论不可达）时的明确失败信号。

    P2.5a-R3 契约核验：保守拦截路径的返回类型恒为 Reply——绝不
    返回 None 或字符串（server.py / Session.turn 对 handle 返回值
    直接属性访问 reply.render/mode/plan/slots/...，非 Reply 即
    AttributeError → 500）。若连自建 Reply 也失败（全默认字段
    dataclass + 常量参数，确定性构造，理论不可达），以本异常明确
    失败并携带孤儿 Pending ID，绝不静默交回原链路。"""


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
        # P2.5a-R2（验收裁定 2026-11-19）：reply_builder **返回 None**
        # 与抛异常同视为绑定失败——此时 Pending 已 open，直接把 None
        # 交回原链路会遗留 open 孤儿（§10.2 缺口 1）。两种形态都必须
        # 先走补偿矩阵，只有孤儿被安全取消后才允许交回原链路。
        try:
            reply = reply_builder(text)
        except Exception as exc:
            self._shadow("produce_bind_failed",
                         detail=f"{type(exc).__name__}: {exc}",
                         pending_id=p.pending_id)
            return self._compensate_orphan(store, p.pending_id,
                                           reply_builder)
        if reply is None:
            self._shadow("produce_bind_failed",
                         detail="reply_builder returned None",
                         pending_id=p.pending_id)
            return self._compensate_orphan(store, p.pending_id,
                                           reply_builder)

        return ProduceOutcome(reply, p.pending_id)

    def _conservative_reply(self, reply_builder):
        """保守拦截回复（P2.5a-R2：三层构造，绝不返回 None、绝不抛异常）。

        §10.2 缺口 2：保守回复若再次依赖刚刚失败过的同一 reply_builder，
        二次失败会向上传播异常，导致既无保守拦截回复、孤儿身份也丢失。
        构造顺序（P2.5a-R3 契约核验后定版：返回类型恒为 Reply）：
        1) reply_builder(_ORPHAN_NOTICE)——与展示同源（刚失败过，
           但兜底文案构造通常更简单，可能已恢复）；
        2) producer 自建最小 Reply（终层）：mode="answer" 为服务层
           既有合法形态（answer_reply 同款），Reply 为全默认字段
           dataclass、无 __post_init__ 校验，常量参数为确定性构造。
        原第三层纯字符串兜底已移除——server.py 对 handle 返回值直接
        属性访问（reply.render/mode/plan/slots/...），字符串会
        AttributeError → 500，违反既有返回类型契约。若第 2 层也失败
        （理论不可达），异常向上传播并由 _compensate_orphan 转为
        携带孤儿身份的 PendingProduceError：明确失败，绝不静默交回
        None/字符串。"""
        try:
            reply = reply_builder(_ORPHAN_NOTICE)
            if reply is not None:
                return reply
        except Exception:
            pass                      # 首层失败 → 降级自建，绝不传播
        return Reply(mode="answer", render=_ORPHAN_NOTICE)

    def _conservative_intercept(self, pending_id: str,
                                reply_builder) -> ProduceOutcome:
        """保守拦截终点（P2.5a-R3）：恒产出 Reply 形态的 ProduceOutcome。

        _conservative_reply 两层均失败（理论不可达）时转 PendingProduceError
        ——异常消息携带孤儿 Pending ID（身份保留），本轮明确失败，
        绝不把 None/字符串当成普通结果交回原链路。"""
        try:
            reply = self._conservative_reply(reply_builder)
        except Exception as exc:
            raise PendingProduceError(
                f"保守拦截回复构造失败（孤儿 Pending={pending_id} 保留 "
                "open，本轮明确失败，绝不静默交回原链路）") from exc
        return ProduceOutcome(reply, pending_id)

    def _compensate_orphan(self, store, pending_id: str,
                           reply_builder) -> ProduceOutcome:
        """孤儿 Pending 补偿（§10.2 失败矩阵第三行 + §11.R3.1 L1）。

        立即尝试取消孤儿；取消成功 → shadow 记录 + 交回原链路；
        取消失败（异常或 ok=False）→ **L1 二线终结补偿**
        `_second_line_terminal`（既有 expire_pending 接口，独立事务，
        覆盖瞬时单笔故障）。保守回复经 _conservative_reply 两层构造，
        二次失败不再向上传播（P2.5a-R2：缺口 2）；终层构造失败
        （理论不可达）以 PendingProduceError 明确失败（P2.5a-R3）。

        R4 冻结定性（§11.R4）：二线终结属尽力而为补偿——双终结均
        失败时**不存在即时消费隔离保证**，残余窗口（TTL 内孤儿仍可
        被权威定位）由 §11.R3.2 生产开关冻结规避（channel=0 恒不
        进入本模块）。"""
        try:
            out = store.cancel_pending(pending_id)
        except Exception as exc:
            self._shadow("produce_orphan_cancel_failed",
                         detail=f"cancel 抛异常 {type(exc).__name__}: {exc}",
                         pending_id=pending_id)
            return self._second_line_terminal(
                store, pending_id,
                f"cancel 抛异常 {type(exc).__name__}: {exc}",
                reply_builder)
        if getattr(out, "ok", False):
            self._shadow("produce_orphan_cancelled",
                         pending_id=pending_id)
            return ProduceOutcome(None, None)      # 孤儿已安全取消
        # cancel 返回 ok=False：状态异常（rowcount=0 同源语义）→
        # 走 L1 二线终结补偿
        detail = str(getattr(out, "reason", "cancel ok=False"))
        self._shadow("produce_orphan_cancel_failed",
                     detail=detail, pending_id=pending_id)
        return self._second_line_terminal(store, pending_id, detail,
                                          reply_builder)

    def _second_line_terminal(self, store, pending_id: str,
                              cancel_detail: str,
                              reply_builder) -> ProduceOutcome:
        """L1 二线终结补偿（§11.R3.1 新增批准增量；R4 修正定性）。

        cancel 失败后**立即调用既有 expire_pending（契约④接口）**：
        两笔终结为独立事务（sqlite 侧各自 connect + BEGIN IMMEDIATE），
        瞬时单笔故障下第二次调用有效。分支：
        - expire 成功 → 孤儿终态（expired）→ get_active_pending()
          返回 None → 权威定位①不可达，隔离效果由消费定位门直接
          判 None **可验证** → 交回原链路（shadow 记
          produce_orphan_terminal_retry）；
        - expire 失败（异常或 ok=False）→ 按 R4 冻结定性**无后续
          隔离保证**（L2 TTL 为最终清理边界、到期前仍可被定位；
          L3 仅持续故障生效；L4 单槽只阻创建不阻消费）→ 保守拦截，
          绝不交回原链路，shadow 记 produce_orphan_terminal_retry_
          failed 双通道保留孤儿身份。
        本方法不新增 store 状态、不新增接口（批准边界）。"""
        try:
            out2 = store.expire_pending(pending_id)
        except Exception as exc:
            self._shadow("produce_orphan_terminal_retry_failed",
                         detail=f"cancel 失败（{cancel_detail}）"
                                f"+ expire 抛异常 "
                                f"{type(exc).__name__}: {exc}",
                         pending_id=pending_id)
            return self._conservative_intercept(pending_id, reply_builder)
        if getattr(out2, "ok", False):
            self._shadow("produce_orphan_terminal_retry",
                         detail=f"cancel 失败（{cancel_detail}），"
                                "二线 expire 成功 → 孤儿终态 "
                                "（隔离可验证：权威定位判 None）",
                         pending_id=pending_id)
            return ProduceOutcome(None, None)      # 孤儿已安全终态
        detail2 = str(getattr(out2, "reason", "expire ok=False"))
        self._shadow("produce_orphan_terminal_retry_failed",
                     detail=f"cancel 失败（{cancel_detail}）"
                            f"+ expire ok=False（{detail2}）"
                            "——残余窗口：TTL 内孤儿仍可被权威定位",
                     pending_id=pending_id)
        return self._conservative_intercept(pending_id, reply_builder)
