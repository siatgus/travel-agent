"""P2.5a 生产侧专项测试：PendingProducer（候选构建 + 展示先行 +
创建编排 + §10.2 孤儿补偿矩阵 + 规则 8 兜底）。

设计依据 docs/PHASE5_P25_PLAN.md（设计基线 4171095）§3.1/§3.3/§10.2；
开工申请 00880ed §11.3 测试清单 P1–P4。运行：
python tests/test_pending_producer.py

边界：只测装配层——store 用内存桩/真实 PendingStore（五方法契约
零修改），不触碰 evaluate/act、Handler、Resolver 判定规则。
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.entity_resolver import MatchRow, ResolvedCandidate
from travel_agent.pending import PendingStore
from travel_agent.pending_producer import (_ORPHAN_NOTICE, PendingProducer,
                                           ProduceOutcome)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL {name}: {detail}")


def fixed_clock():
    return lambda: datetime(2026, 11, 19, 12, 0, 0)


def m(city, name, source="", area=""):
    """MatchRow 快捷构造（entity_id ← source，address ← area）。"""
    return MatchRow(city=city, name=name, category="restaurant",
                    area=area, source=source, origin="kb")


def rc(text, status="ambiguous", role="query", matches=None,
       confidence=0.9):
    return ResolvedCandidate(
        original={"text": text, "entity_type": "restaurant", "role": role},
        status=status, confidence=confidence, probe_status="not_applicable",
        matches=matches or [])


def fixed_ids():
    state = {"n": 0}

    def gen():
        state["n"] += 1
        return f"cid{state['n']:03d}"

    return gen


class CountingStore(PendingStore):
    """create/cancel 调用计数桩（契约不变，仅观测）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.create_calls = 0
        self.cancel_calls = 0
        self.last_created_id = None
        self.cancel_raise = None
        self.cancel_ok_override = None

    def create_pending(self, *a, **kw):
        self.create_calls += 1
        p = super().create_pending(*a, **kw)
        self.last_created_id = p.pending_id
        return p

    def cancel_pending(self, pending_id):
        self.cancel_calls += 1
        if self.cancel_raise is not None:
            raise self.cancel_raise
        out = super().cancel_pending(pending_id)
        if self.cancel_ok_override is not None:
            out.ok = self.cancel_ok_override
        return out


def _produce(producer, store, matches, reply_builder=None):
    rb = reply_builder or (lambda t: f"REPLY::{t}")
    return producer.produce(
        store, utterance="海贝皇大排档哪家好", intent="poi_query",
        role="query", matches=matches, reply_builder=rb)


def failing_builder(fail_count=1):
    """前 fail_count 次构造失败、其后成功（模拟复杂构造失败 /
    简单兜底文本成功——生产中两者共用 answer_reply 同源语义）。"""
    state = {"n": 0}

    def builder(text):
        state["n"] += 1
        if state["n"] <= fail_count:
            raise RuntimeError("注入：Reply 构造失败")
        return f"REPLY::{text}"

    return builder


# ================================================== P1 候选构建


def test_p1_build_candidates_shape() -> None:
    """P1：fresh matches → 六字段、1..N 冻结、id 唯一可引用。"""
    pr = PendingProducer(id_gen=fixed_ids())
    cands = pr.build_candidates(
        [m("南京", "海贝皇大排档（南京店）", source="e-nj", area="玄武区"),
         m("上海", "海贝皇大排档（上海店）", source="e-sh", area="浦东新区")])
    check("[P1] 六字段齐全",
          all(set(c) == {"candidate_id", "display_order", "entity_id",
                         "name", "city", "address"} for c in cands),
          str(cands))
    check("[P1] display_order 1..N 冻结",
          [c["display_order"] for c in cands] == [1, 2],
          str([c["display_order"] for c in cands]))
    check("[P1] candidate_id 唯一",
          len({c["candidate_id"] for c in cands}) == 2, "")
    check("[P1] entity_id ← source",
          [c["entity_id"] for c in cands] == ["e-nj", "e-sh"], str(cands))
    check("[P1] address ← area",
          cands[0]["address"] == "玄武区", str(cands[0]))
    check("[P1] city/name 如实",
          cands[1]["city"] == "上海"
          and cands[1]["name"] == "海贝皇大排档（上海店）", str(cands[1]))
    # 空匹配 → 空列表（交回原链路）；非法 name → 拒绝降质
    check("[P1] 空匹配 → 空列表",
          pr.build_candidates([]) == [], "")
    try:
        pr.build_candidates([m("南京", "")])
        check("[P1] 空 name 拒绝", False, "未抛异常")
    except ValueError:
        check("[P1] 空 name 拒绝", True, "")
    # pick_ambiguous：HC-4 纵深排除 + 档内排序（绝不改变档位）
    amb = rc("海贝皇", matches=[m("南京", "南京店"), m("上海", "上海店")],
             confidence=0.7)
    hc4 = rc("排除我", role="negated", matches=[m("南京", "x")],
             confidence=0.99)
    top = pr.pick_ambiguous([hc4, amb])
    check("[P1] HC-4 角色绝不入快照（纵深排除）",
          top is amb, str(getattr(top, "text", None)))
    check("[P1] 无 ambiguous → None",
          pr.pick_ambiguous([rc("好店", status="confirmed")]) is None, "")
    conf = rc("某店", status="confirmed")
    check("[P1] 非 ambiguous 不被选中",
          pr.pick_ambiguous([conf]) is None, "")


# ================================================== P2 展示先于创建


def test_p2_display_before_create() -> None:
    """P2：§10.2 步骤 1 失败 → 零副作用，create 绝不被调用。"""
    pr = PendingProducer(id_gen=fixed_ids())
    store = CountingStore(session_id="s-p2", now=fixed_clock())
    # 注入：展示话术构造失败（步骤 1）
    orig = PendingProducer.__dict__["clarify_reply_text"]  # 原 staticmethod
    def boom(cands):
        raise RuntimeError("注入：展示构造失败")
    PendingProducer.clarify_reply_text = staticmethod(boom)
    try:
        out = _produce(pr, store, [m("南京", "A店"), m("上海", "B店")])
    finally:
        PendingProducer.clarify_reply_text = orig
    check("[P2] 步骤1失败 → 交回原链路（reply=None）",
          out.reply is None and out.pending_id is None, str(out))
    check("[P2] 零副作用：create 未被调用", store.create_calls == 0,
          str(store.create_calls))
    check("[P2] shadow 记录 build_failed",
          any(r["action"] == "produce_display_build_failed"
              for r in pr.shadow_log), str(pr.shadow_log))
    # 注入：候选构建失败（空 name → ValueError）→ 同样零副作用
    out2 = _produce(pr, store, [m("南京", "")])
    check("[P2] 候选构建失败同样零副作用",
          out2.reply is None and store.create_calls == 0, str(out2))
    # 空匹配 → skip 影子 + 原链路
    out3 = _produce(pr, store, [])
    check("[P2] 空匹配 → 原链路", out3.reply is None, str(out3))
    check("[P2] skip 影子",
          any(r["action"] == "produce_skip" for r in pr.shadow_log), "")


# ================================================== P3 孤儿补偿矩阵


def test_p3_orphan_compensation_matrix() -> None:
    """P3：§10.2 失败矩阵——绑定失败 → 取消孤儿；取消失败 → 保守拦截。"""
    # ③ 正常路径：Pending ID 与展示内容绑定返回
    pr = PendingProducer(id_gen=fixed_ids())
    store = CountingStore(session_id="s-p3", now=fixed_clock())
    out = _produce(pr, store, [m("南京", "A店"), m("上海", "B店")])
    check("[P3正常] 返回短路回复", out.reply is not None, str(out))
    check("[P3正常] pending_id 绑定返回",
          out.pending_id is not None
          and out.pending_id == store._active.pending_id, str(out))
    check("[P3正常] pending 为 open",
          store.get_pending_by_id(out.pending_id).status == "open", "")
    check("[P3正常] 展示 1..N 含名称城市",
          "1. A店（南京）" in str(out.reply)
          and "2. B店（上海）" in str(out.reply), str(out.reply))
    check("[P3正常] 提示回复序号", "回复序号" in str(out.reply), "")

    # ① 绑定/返回失败 → 立即取消孤儿 → 交回原链路
    pr1 = PendingProducer(id_gen=fixed_ids())
    store1 = CountingStore(session_id="s-p3a", now=fixed_clock())
    out1 = _produce(pr1, store1, [m("南京", "A店"), m("上海", "B店")],
                    reply_builder=failing_builder(fail_count=99))
    check("[P3①] 绑定失败 → 交回原链路",
          out1.reply is None and out1.pending_id is None, str(out1))
    check("[P3①] 孤儿已被取消（cancel 恰好一次）",
          store1.cancel_calls == 1
          and store1.get_pending_by_id(store1.last_created_id).status
          == "cancelled", f"cancel={store1.cancel_calls}")
    check("[P3①] shadow 记录取消成功",
          any(r["action"] == "produce_orphan_cancelled"
              for r in pr1.shadow_log), str(pr1.shadow_log))
    check("[P3①] shadow 记录绑定失败含 pending_id",
          any(r["action"] == "produce_bind_failed" and r["pending_id"]
              for r in pr1.shadow_log), str(pr1.shadow_log))

    # ② 取消也失败（cancel 抛异常）→ 保守拦截，绝不交回原链路
    pr2 = PendingProducer(id_gen=fixed_ids())
    store2 = CountingStore(session_id="s-p3b", now=fixed_clock())
    store2.cancel_raise = RuntimeError("注入：cancel 失败")
    out2 = _produce(pr2, store2, [m("南京", "A店"), m("上海", "B店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[P3②] 取消失败 → 保守拦截（绝不交回原链路）",
          out2.reply is not None
          and _ORPHAN_NOTICE in str(out2.reply), str(out2))
    check("[P3②] pending_id 保留（孤儿可见可排查）",
          out2.pending_id is not None, str(out2))
    check("[P3②] shadow 记录 orphan",
          any(r["action"] == "produce_orphan_cancel_failed"
              and r["pending_id"] for r in pr2.shadow_log),
          str(pr2.shadow_log))

    # ③' cancel 返回 ok=False（rowcount=0 同源语义）→ 同保守拦截
    pr3 = PendingProducer(id_gen=fixed_ids())
    store3 = CountingStore(session_id="s-p3c", now=fixed_clock())
    store3.cancel_ok_override = False
    out3 = _produce(pr3, store3, [m("南京", "A店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[P3③'] cancel ok=False → 保守拦截",
          out3.reply is not None
          and _ORPHAN_NOTICE in str(out3.reply), str(out3))


# ================================================== P4 规则 8 兜底


def test_p4_rule8_rejection() -> None:
    """P4：已有 active pending → create 拒绝 → 交回原链路，不覆盖不轮换。"""
    pr = PendingProducer(id_gen=fixed_ids())
    store = CountingStore(session_id="s-p4", now=fixed_clock())
    first = store.create_pending(
        "旧句子", "poi_query", "query",
        [{"candidate_id": "old1", "display_order": 1,
          "entity_id": "e1", "name": "旧候选", "city": "南京",
          "address": None}])
    out = _produce(pr, store, [m("南京", "新A"), m("上海", "新B")])
    check("[P4] create 拒绝 → 交回原链路",
          out.reply is None and out.pending_id is None, str(out))
    check("[P4] 原 Pending 状态不变（open）",
          store.get_pending_by_id(first.pending_id).status == "open", "")
    check("[P4] 原 Pending 候选未被覆盖",
          [c.name for c in store.get_pending_by_id(
              first.pending_id).candidates] == ["旧候选"], "")
    check("[P4] shadow 记录规则 8",
          any(r["action"] == "produce_rejected_rule8"
              for r in pr.shadow_log), str(pr.shadow_log))
    # store 其他异常（非 ValueError）→ 同保守交回
    class BoomStore(CountingStore):
        def create_pending(self, *a, **kw):
            raise IOError("注入：store 故障")
    bs = BoomStore(session_id="s-p4b", now=fixed_clock())
    out2 = _produce(pr, bs, [m("南京", "A")])
    check("[P4'] store 故障 → 原链路",
          out2.reply is None and out2.pending_id is None, str(out2))
    check("[P4'] shadow 记录 create_failed",
          any(r["action"] == "produce_create_failed"
              for r in pr.shadow_log), str(pr.shadow_log[-2:]))


# ================================================== 影子隔离


def test_shadow_isolation() -> None:
    """B5 精神：shadow sink 异常被隔离，不影响编排与补偿分支。"""
    def bad_sink(rec):
        raise RuntimeError("注入：shadow sink 异常")
    pr = PendingProducer(id_gen=fixed_ids(), shadow_sink=bad_sink)
    store = CountingStore(session_id="s-sh", now=fixed_clock())
    out = _produce(pr, store, [m("南京", "A")])
    check("[影子] sink 异常不影响正常生产",
          out.reply is not None and out.pending_id is not None, str(out))
    pr2 = PendingProducer(id_gen=fixed_ids(), shadow_sink=bad_sink)
    store2 = CountingStore(session_id="s-sh2", now=fixed_clock())
    store2.cancel_raise = RuntimeError("注入：cancel 失败")
    out2 = _produce(pr2, store2, [m("南京", "A")],
                    reply_builder=failing_builder(fail_count=1))
    check("[影子] sink 异常不影响补偿矩阵分支",
          out2.reply is not None
          and _ORPHAN_NOTICE in str(out2.reply), str(out2))


# ================================================== main


def main() -> int:
    test_p1_build_candidates_shape()
    test_p2_display_before_create()
    test_p3_orphan_compensation_matrix()
    test_p4_rule8_rejection()
    test_shadow_isolation()
    print(f"\npending producer P2.5a: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
