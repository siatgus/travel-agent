"""PendingInterceptor 多轮消歧拦截器（Phase 5 P2.3）。

设计依据 docs/PHASE5_P23_PLAN.md（P2.3 开工申请，评审冻结）与
docs/PHASE5_P2_PLAN.md §3/§3.1/§4/§4.1。**独立模块，不接线**：
Orchestrator 接入属 P2.4（未授权），TRAVEL_PENDING_CHANNEL 保持
默认 0，Handler 零改动。

P2.3 验收要求逐条落点：
1. evaluate/act 严格分离——evaluate() 纯判定：不调用 store 状态
   迁移方法、不修改 Pending/context/slots/ParseResult/调用方候选
   结构（深拷贝对比测试锁定）；shadow_sink 异常不影响判定结果。
   act() 只执行 evaluate 已产生的 verdict，store 失败如实返回，
   不自动改选其他候选，不伪造成功结果。
2. 判定优先级冻结（评审定版）：
   ① Session/Pending 有效性 → ② 明确目的地变化 → ③ 用户明确
   取消 → ④ 过期检查 → ⑤ 可靠候选选择 → ⑥ 明确新问题 →
   ⑦ 重复选择识别（仅无可消费 pending 时）→ ⑧ hand_back →
   ⑨ none。低优先级的名称/城市/序号匹配**绝不覆盖**目的地变化。
3. hand_back 与 restate 边界（P2.3 验收补丁冻结）——
   restate **只留给显式重述请求**（「再说一遍有哪些店」「重新列
   一下」）；其余与 pending 相关但不足以确定是选择、重述还是
   新问题的表达（「南京那家怎么样」「南京有什么好吃的」「那家
   店值得去吗」、选择歧义/越界、取消与选择冲突）一律 hand_back：
   保守交回原链路，pending 保持 open，**绝不消费、绝不取消**。
4. 重复选择识别（§4.1）须同时满足：纯选择线索（序号指代或
   「就这家/还是它」）+ 不含新实体候选/新意图信号 + 当前 Session
   最近一次消费在 TRAVEL_PENDING_RESELECT_WINDOW（默认 600 秒）
   内；任一不满足交回原链路，绝不猜测历史候选、不改任何状态。

时间判定沿用 P2.2 契约：到期判定委托 store._is_expired（时钟在
store 内自洽）；重复选择窗口用 epoch 整数比较（调用方注入时钟
需与 store 一致）。规则依据：PHASE5_DESIGN v3.2.1 §7 九条消费规则。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime

from travel_agent.pending import PENDING_OPEN, _ConsumeOutcome, _epoch
# R-01（I-2）：影子容器有界化共享机制（纯标准库模块，无反向依赖）
from travel_agent.retention import (LIM_ID, LIM_REASON, LIM_UTTERANCE,
                                    SHADOW_CAPACITY, RetentionInvariantError,
                                    append_bounded, clip_evidence,
                                    coerce_text, new_stats)

# ---- 九类互斥 verdict（P2.3 冻结契约） ----
V_CONSUME = "consume"                       # 可靠选择定位唯一候选
V_CANCEL_NEW_QUESTION = "cancel_new_question"  # 明确新问题 → 取消
V_HAND_BACK = "hand_back"                   # 模糊表达：不取消不消费
V_RESTATE = "restate"                       # 选择语境但无法唯一定位
V_EXPIRED_REPLY = "expired_reply"           # 选择时已过期 → 显式登记
V_CANCEL_EXPLICIT = "cancel_explicit"       # 用户明确取消
V_CANCEL_DEST_CHANGED = "cancel_destination_changed"  # 目的地变化
V_DUPLICATE_SELECT = "duplicate_select"     # 重复选择：零状态变化
V_NONE = "none"                             # 不适用：正常走原链路

VERDICTS = (V_CONSUME, V_CANCEL_NEW_QUESTION, V_HAND_BACK, V_RESTATE,
            V_EXPIRED_REPLY, V_CANCEL_EXPLICIT, V_CANCEL_DEST_CHANGED,
            V_DUPLICATE_SELECT, V_NONE)

# act() 无 store 动作的 verdict（零状态变化）
ZERO_STATE_VERDICTS = (V_HAND_BACK, V_RESTATE, V_DUPLICATE_SELECT,
                       V_NONE)

DEFAULT_RESELECT_WINDOW = 600   # 秒（TRAVEL_PENDING_RESELECT_WINDOW 覆盖）


@dataclass(frozen=True)
class InterceptVerdict:
    """evaluate() 的判定结果（frozen）。

    **判定 ≠ 已执行**：除零状态变化类外，须由 act() 落到 store，
    act 返回值才是实际执行结果。
    """

    verdict: str
    pending_id: str | None = None
    candidate_id: str | None = None
    reason: str = ""
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(f"非法 verdict：{self.verdict!r}，"
                             f"允许值 {VERDICTS}")

    def as_dict(self) -> dict:
        return {"verdict": self.verdict,
                "pending_id": self.pending_id,
                "candidate_id": self.candidate_id,
                "reason": self.reason,
                "evidence": dict(self.evidence)}


# ------------------------------------------------ 选择线索解析（只在快照内匹配，快照外绝不猜）

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 序号指代：必须带锚定词（第/选/就要/号），日期（第2天）、人数
# （3个人）、天数（两天）不误触发——负向测试锁定。
_ORDER_RE = re.compile(
    r"第\s*([0-9一二两三四五六七八九十]+)\s*[个家]"
    r"|选\s*([0-9一二两三四五六七八九十]+)\s*[个号家]?"
    r"|就要\s*([0-9一二两三四五六七八九十]+)\s*号"
    r"|(?<![0-9])([0-9]+)\s*号")

_CANCEL_KW = ("取消", "算了", "不选了", "先不选", "不订了")

# 纯选择语气（无定位词的确认语气）
_PURE_TONES = ("就这家", "还是它", "就它", "就这个", "就要这个",
               "就要这家", "就那家")

# 指示词（选择语境信号）
_DEMONS = ("那家", "这家", "那间", "那个", "这家店", "那家店")

# 询问/评价标记：出现即不确认为选择（「南京那家怎么样」「那家店
# 值得去吗」→ hand_back 保守交回原链路，绝不消费、绝不取消）
_Q_MARKERS = ("怎么样", "怎样", "如何", "值得", "推荐", "介绍",
              "好吃", "好玩", "评价", "靠谱", "怎么样?", "好吗")

# 显式重述请求：restate 的唯一触发来源（P2.3 验收补丁冻结边界——
# restate 只留给「用户明确要求重新展示/重复说明候选」）
_REDISPLAY_PATTERNS = ("再说一遍", "再说一次", "重新列", "再列一次",
                       "重新说", "重复一下", "重述", "列一下选项",
                       "有哪些店", "有哪些选择", "重新展示")

# 明确新意图的粗分类（结构化解析依据；poi 粗类须有具名实体候选
# 才算依据，纯城市词/泛化品类词不算——评审红线）
_NEW_COARSE = ("weather", "transport")


def _parse_order(ut: str) -> int | None:
    m = _ORDER_RE.search(ut)
    if m is None:
        return None
    raw = next((g for g in m.groups() if g), None)
    if raw is None:
        return None
    if raw.isdigit():
        return int(raw)
    return _CN_NUM.get(raw)          # 复合中文数字不支持 → None 保守


def _strip_branch(name: str) -> str:
    """「海贝皇大排档（南京店）」→「海贝皇大排档」（含匹配放宽用）。"""
    return re.sub(r"[（(].*?[）)]", "", name or "").strip()


@dataclass
class _Sel:
    """选择线索解析结果（内部）。"""
    context: bool = False            # 是否具备选择语境
    kind: str = ""                   # order/name/city_demon/pure_tone/bare_demon
    order: int | None = None
    candidate_ids: tuple = ()        # name 匹配命中的快照候选
    question: bool = False           # 含询问/评价标记 → 不确认为选择


def _find_selection(ut: str, p, parse_result=None) -> _Sel:
    """解析选择线索。p 为 None 时只识别 order/pure_tone（供重复选择）。"""
    q = any(m in ut for m in _Q_MARKERS)
    order = _parse_order(ut)
    if order is not None:
        return _Sel(True, "order", order=order, question=q)
    if any(t in ut for t in _PURE_TONES):
        return _Sel(True, "pure_tone", question=q)
    if p is None:
        return _Sel(False, question=q)
    # 名称匹配（精确/包含，只在快照内）：全名命中优先于去分店核心
    full = [c.candidate_id for c in p.candidates
            if c.name and c.name in ut]
    if not full:
        full = [c.candidate_id for c in p.candidates
                if len(ut) >= 3 and ut in c.name]
    if not full:
        full = [c.candidate_id for c in p.candidates
                if len(_strip_branch(c.name)) >= 2
                and _strip_branch(c.name) in ut]
    if full:
        return _Sel(True, "name", candidate_ids=tuple(full), question=q)
    # 城市限定指代：快照内城市 + 指示词（「南京那家」）
    if any(d in ut for d in _DEMONS):
        cities = [c for c in p.candidates
                  if c.city and c.city in ut]
        if cities:
            return _Sel(True, "city_demon", question=q)
        # 指示词 + 解析出的快照外城市（如「苏州那家」）→ 不是对
        # 当前快照的选择，快照外绝不猜 → 无选择语境（hand_back）
        for attr in ("query_city", "destination", "destination_new"):
            foreign = getattr(parse_result, attr, None) if \
                parse_result is not None else None
            if foreign and foreign not in \
                    {c.city for c in p.candidates}:
                return _Sel(False, question=q)
        # 裸指示词（无城市/名称/序号）：选择语境但无定位
        return _Sel(True, "bare_demon", question=q)
    return _Sel(False, question=q)


def _resolve_selection(sel: _Sel, p, ut: str) -> tuple[str, list[str]]:
    """把选择语境解析为候选：unique / ambiguous / invalid。"""
    cands = list(p.candidates)
    if sel.kind == "order":
        if sel.order is not None and 1 <= sel.order <= len(cands):
            return "unique", [cands[sel.order - 1].candidate_id]
        return "invalid", []
    if sel.kind == "name":
        if len(sel.candidate_ids) == 1:
            return "unique", list(sel.candidate_ids)
        return "ambiguous", list(sel.candidate_ids)
    if sel.kind == "city_demon":
        hit = [c for c in cands if c.city and c.city in ut]
        if len(hit) == 1:
            return "unique", [hit[0].candidate_id]
        return "ambiguous", [c.candidate_id for c in hit]
    # pure_tone / bare_demon：恰一条候选才可定位
    if len(cands) == 1:
        return "unique", [cands[0].candidate_id]
    return "ambiguous", []


class PendingInterceptor:
    """多轮消歧拦截器（P2.3）：evaluate 纯判定 + act 受控执行。"""

    def __init__(self, store, *, shadow_sink=None,
                 reselect_window: int | None = None) -> None:
        self._store = store
        self.shadow_sink = shadow_sink       # 可选回调：record(dict)
        self.shadow_log: list[dict] = []     # 影子判定记录（纯记录）
        # ---- R-01（I-2）：S3 容量治理状态（_v 与 gate 共用同一池）----
        self.shadow_dead = False             # 容器不可判定后不再写入
        self.shadow_stats = None             # 首次成功追加后才就位
        if reselect_window is None:
            reselect_window = int(
                os.environ.get("TRAVEL_PENDING_RESELECT_WINDOW",
                               str(DEFAULT_RESELECT_WINDOW)))
        self._reselect_window = int(reselect_window)

    # -------------------------------------------------- 主入口

    def evaluate(self, utterance: str, parse_result=None, *,
                 session_pending_id: str | None = None,
                 now=None, context: dict | None = None) -> InterceptVerdict:
        """纯判定：零副作用（不改 store 状态 / parse_result / context）。

        context 仅由调用方传入并原样保留（P2.4 接线用），evaluate
        **绝不读取修改**——深拷贝对比测试锁定。"""
        ut = (utterance or "").strip()
        now_e = self._now_epoch(now)
        p = self._locate_pending(session_pending_id)
        consumable = p is not None and p.status == PENDING_OPEN
        sel = _find_selection(ut, p if consumable else None,
                              parse_result)
        dest = self._dest_changed(parse_result, p)
        cancel_hit = any(k in ut for k in _CANCEL_KW)
        newq = self._new_question_evidence(parse_result, p, sel)

        # ① 无可消费 pending → 重复选择识别 → none
        if not consumable:
            dup = self._duplicate_verdict(ut, sel, parse_result, now_e)
            if dup is not None:
                return dup
            return self._v(V_NONE, ut, now_e,
                           reason="无可消费 pending 且非重复选择语境，"
                                  "交回原链路")

        pid = p.pending_id
        redisplay = any(t in ut for t in _REDISPLAY_PATTERNS)
        # ② 明确目的地变化（最高优先级，规则 4；含新目的地恰在快照
        #    城市集合内的保守取舍——旧快照一律作废重走 Resolver）
        if dest is not None:
            return self._v(
                V_CANCEL_DEST_CHANGED, ut, now_e, pending_id=pid,
                reason="本句明确新目的地（规划语义），候选城市归属已"
                       "变化，旧快照作废重新走 EntityResolver（规则 4）",
                evidence=dest)
        # ③ 用户明确取消（与选择语境冲突时保守 → hand_back，
        #    P2.3 验收补丁冻结边界：restate 只留给显式重述请求）
        if cancel_hit:
            if sel.context:
                return self._v(
                    V_HAND_BACK, ut, now_e, pending_id=pid,
                    reason="取消与选择线索同时命中，不足以确定是选择、"
                           "重述还是新问题，保守交回原链路（pending "
                           "保持 open）",
                    evidence={"conflict": "cancel+selection",
                              "selection_kind": sel.kind})
            return self._v(V_CANCEL_EXPLICIT, ut, now_e, pending_id=pid,
                           reason="用户明确取消（规则 2）",
                           evidence={"keyword": next(
                               k for k in _CANCEL_KW if k in ut)})
        # ④ 过期检查（选择语境或显式重述请求一律如实告知过期，规则 5）
        expired = self._store._is_expired(p)
        if expired and (sel.context or redisplay):
            return self._v(
                V_EXPIRED_REPLY, ut, now_e, pending_id=pid,
                reason="pending 已过期，不得继续执行（规则 5）",
                evidence={"selection_kind": sel.kind or None,
                          "redisplay": redisplay})
        # ⑤ 可靠候选选择（规则 1/9；询问/评价标记已排除确认选择）
        if sel.context and not sel.question:
            state, ids = _resolve_selection(sel, p, ut)
            if state == "unique":
                return self._v(
                    V_CONSUME, ut, now_e, pending_id=pid,
                    candidate_id=ids[0],
                    reason="可靠选择线索定位唯一候选（规则 1/9）",
                    evidence={"selection_kind": sel.kind,
                              "order": sel.order})
            # P2.3 验收补丁冻结边界：选择尝试失败（越界/歧义）不足
            # 以确定是选择还是新问题 → hand_back 交回原链路
            if state in ("ambiguous", "invalid"):
                return self._v(
                    V_HAND_BACK, ut, now_e, pending_id=pid,
                    reason="选择语境但无法唯一定位候选，不足以确定是"
                           "选择还是新问题，保守交回原链路（pending "
                           "保持 open，不消费不取消）",
                    evidence={"selection_kind": sel.kind,
                              "resolve": state,
                              "order": sel.order})
        # ⑥ 明确新问题（有足够结构化依据才取消，规则 3）
        if newq is not None:
            return self._v(
                V_CANCEL_NEW_QUESTION, ut, now_e, pending_id=pid,
                reason="明确新问题证据充分，先取消旧 pending 再走原"
                       "链路（规则 3，不误消费）",
                evidence=newq)
        # ⑦ 显式重述请求（「再说一遍有哪些店」「重新列一下」）
        #    → restate：引用 pending 重述选项，保持 open
        if redisplay:
            return self._v(
                V_RESTATE, ut, now_e, pending_id=pid,
                reason="用户明确要求重新展示候选，引用 pending 重述"
                       "选项（保持 open，不消费不取消）",
                evidence={"redisplay": True})
        # ⑧ 与 pending 相关但不足以判定 → hand_back（验收补丁冻结：
        #    「南京那家怎么样」「那家店值得去吗」等一律交回原链路）
        if sel.context:
            return self._v(
                V_HAND_BACK, ut, now_e, pending_id=pid,
                reason="与 pending 相关但不足以确定是选择、重述还是"
                       "新问题，保守交回原链路（pending 保持 open，"
                       "不消费不取消）",
                evidence={"selection_kind": sel.kind,
                          "question_marker": sel.question})
        return self._v(
            V_HAND_BACK, ut, now_e, pending_id=pid,
            reason="表达模糊且无取消/选择/新问题依据，交回原链路，"
                   "pending 保持 open（不得当作取消）",
            evidence={})

    def act(self, verdict: InterceptVerdict) -> _ConsumeOutcome | None:
        """只执行 evaluate 已产生的 verdict；失败如实返回。"""
        if not isinstance(verdict, InterceptVerdict):
            raise TypeError(
                f"act 只接受 InterceptVerdict，收到 "
                f"{type(verdict).__name__}")
        v = verdict.verdict
        if v == V_CONSUME:
            return self._store.consume_pending(
                verdict.pending_id, verdict.candidate_id)
        if v in (V_CANCEL_NEW_QUESTION, V_CANCEL_EXPLICIT,
                 V_CANCEL_DEST_CHANGED):
            return self._store.cancel_pending(verdict.pending_id)
        if v == V_EXPIRED_REPLY:
            return self._store.expire_pending(verdict.pending_id)
        if v in ZERO_STATE_VERDICTS:
            return None
        raise ValueError(f"未知 verdict：{v!r}")

    # -------------------------------------------------- 判定依据

    def _locate_pending(self, session_pending_id: str | None):
        if session_pending_id:
            return self._store.get_pending_by_id(session_pending_id)
        return self._store.get_active_pending()

    def _dest_changed(self, parse_result, p) -> dict | None:
        """本句明确新目的地（规划语义）→ 一律按目的地变化（冻结）。"""
        if p is None or parse_result is None:
            return None
        dest_new = getattr(parse_result, "destination_new", None)
        writable = bool(getattr(parse_result,
                                "destination_writable", False))
        if not (dest_new and writable):
            return None
        snap_cities = sorted({c.city for c in p.candidates if c.city})
        return {"destination_new": dest_new,
                "in_snapshot_cities": dest_new in snap_cities,
                "snapshot_cities": snap_cities}

    def _new_question_evidence(self, parse_result, p, sel) -> dict | None:
        """新问题依据：具名实体候选 / 明确新意图；模糊信号不算。"""
        if parse_result is None:
            return None
        if sel is not None and sel.context and not sel.question:
            return None        # 选择语境优先，不叠加新问题判定
        coarse = getattr(parse_result, "intent_coarse", "") or ""
        cands = getattr(parse_result, "poi_candidates", None) or []
        snap_names = {c.name for c in p.candidates} if p is not None \
            else set()
        new_entities = [getattr(c, "name", "") for c in cands
                        if getattr(c, "name", "")
                        and getattr(c, "name") not in snap_names]
        if new_entities:
            return {"evidence": "new_entity_candidates",
                    "names": new_entities}
        if coarse in _NEW_COARSE:
            return {"evidence": "explicit_intent",
                    "intent_coarse": coarse}
        return None

    def _duplicate_verdict(self, ut, sel, parse_result,
                           now_e) -> InterceptVerdict | None:
        """§4.1 重复选择识别（仅无可消费 pending 时调用）。

        三条件缺一不可：纯选择线索（order/pure_tone）+ 无新实体
        候选/新意图信号/取消词 + 窗口内；绝不猜测历史候选。
        """
        rec = self._store._latest_consumption()
        if not rec:
            return None
        if sel is None or not sel.context or sel.question:
            return None
        if sel.kind not in ("order", "pure_tone"):
            return None
        if any(k in ut for k in _CANCEL_KW):
            return None
        pr_cands = getattr(parse_result, "poi_candidates", None) or []
        if any(getattr(c, "name", "") for c in pr_cands):
            return None            # 含新实体候选 → 非纯选择线索
        coarse = getattr(parse_result, "intent_coarse", "") or ""
        if coarse in _NEW_COARSE:
            return None            # 新意图信号 → 非纯选择线索
        try:
            consumed_at = int(rec["consumed_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if now_e - consumed_at > self._reselect_window:
            return None            # 超窗口 → 交回原链路
        if sel.kind == "order":
            old = self._store.get_pending_by_id(rec.get("pending_id")
                                                or "")
            if old is None:
                return None        # 无法映射快照 → 绝不猜
            ids = old.snapshot_candidate_ids()
            if sel.order is None or not (1 <= sel.order <= len(ids)):
                return None
            cid = ids[sel.order - 1]
        else:
            cid = rec.get("candidate_id") or ""
            if not cid:
                return None
        return self._v(
            V_DUPLICATE_SELECT, ut, now_e,
            pending_id=rec.get("pending_id"), candidate_id=cid,
            reason=f"刚刚已确认过「{rec.get('selected_name')}」，"
                   f"不执行任何业务、不改任何状态（规则 6）",
            evidence={"consumed_at": consumed_at,
                      "window": self._reselect_window,
                      "selected_name": rec.get("selected_name"),
                      "selected_city": rec.get("selected_city")})

    # -------------------------------------------------- 影子与时间

    def _now_epoch(self, now) -> int:
        if now is None:
            return int(time.time())
        if callable(now):
            now = now()
        return _epoch(now)

    def _v(self, verdict, ut, now_e, **kw) -> InterceptVerdict:
        """构造 verdict 并写一条影子判定记录（R-01：有界 + 失效标记）。

        ``v`` 的构造、返回值和业务分支完全在观察支路之外——记录侧任何
        失败都不改变判定结果。写入顺序（评审 2026-11-23 修订 1/2）：
        ``append_bounded`` 成功 → 提交 stats → 调用 sink；sink 是否调用
        由独立 ``append_succeeded`` 决定，**不再**依据 ``rec is not None``
        （rec 在追加前已赋值，追加失败时仍非 None）。
        """
        v = InterceptVerdict(verdict=verdict, **kw)   # 业务：保护范围外
        rec = None
        append_succeeded = False
        if not self.shadow_dead:
            try:
                rec = {"utterance": coerce_text(ut, LIM_UTTERANCE),
                       "verdict": coerce_text(v.verdict, LIM_ID),
                       "pending_id": coerce_text(v.pending_id, LIM_ID),
                       "candidate_id": coerce_text(v.candidate_id, LIM_ID),
                       "reason": coerce_text(v.reason, LIM_REASON),
                       "evidence": clip_evidence(v.evidence),
                       "ts": now_e}
                stats = self.shadow_stats
                if stats is None:
                    stats = new_stats(self.shadow_log, SHADOW_CAPACITY)
                append_bounded(self.shadow_log, rec, stats)
                if self.shadow_stats is None:
                    self.shadow_stats = stats         # 先于 sink 提交
                append_succeeded = True
            except RetentionInvariantError:
                # 顺序不可交换：先置失效，再吞掉（不重试、不追 warning）
                self.shadow_dead = True
            except Exception:
                pass
        if append_succeeded and self.shadow_sink is not None:
            try:
                self.shadow_sink(rec)
            except Exception:
                pass               # 影子通道绝不影响判定结果
        return v
