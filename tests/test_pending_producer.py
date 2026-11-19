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
from travel_agent.dialogue import Reply
import travel_agent.pending_producer as _pp_mod
from travel_agent.pending import PendingStore
from travel_agent.pending_producer import (_ORPHAN_NOTICE, PendingProducer,
                                           PendingProduceError,
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
    """create/cancel/expire 调用计数桩（契约不变，仅观测）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.create_calls = 0
        self.cancel_calls = 0
        self.expire_calls = 0
        self.last_created_id = None
        self.cancel_raise = None
        self.cancel_ok_override = None
        self.expire_raise = None
        self.expire_ok_override = None

    def create_pending(self, *a, **kw):
        self.create_calls += 1
        p = super().create_pending(*a, **kw)
        self.last_created_id = p.pending_id
        return p

    def cancel_pending(self, pending_id):
        self.cancel_calls += 1
        if self.cancel_raise is not None:
            raise self.cancel_raise
        if self.cancel_ok_override is not None:
            # 状态一致的失败模拟：不实际取消（真实 cancel ok=False
            # 语义 = rowcount 0，Pending 保持 open），仅返回失败结果
            return SimpleNamespace(ok=False, reason="注入：cancel ok=False")
        return super().cancel_pending(pending_id)

    def expire_pending(self, pending_id):
        self.expire_calls += 1
        if self.expire_raise is not None:
            raise self.expire_raise
        if self.expire_ok_override is not None:
            return SimpleNamespace(ok=False, reason="注入：expire ok=False")
        return super().expire_pending(pending_id)


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
    store2.expire_raise = RuntimeError("注入：expire 失败")  # 双终结
    out2 = _produce(pr2, store2, [m("南京", "A店"), m("上海", "B店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[P3②] 双终结失败 → 保守拦截（绝不交回原链路）",
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
    store3.expire_ok_override = False                     # 双终结 ok=False
    out3 = _produce(pr3, store3, [m("南京", "A店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[P3③'] 双终结 ok=False → 保守拦截",
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


# ================================================== P3-R2 补偿返回值边界
# （P2.5a-R2 验收裁定 2026-11-19：空返回值与二次构造失败两缺口）


def none_builder():
    """永远返回 None 的 builder（不抛异常——缺口 1 形态）。"""
    return lambda text: None


def always_failing_builder():
    """永远抛异常的 builder（保守层首层必失败——缺口 2 形态）。"""
    def builder(text):
        raise RuntimeError("注入：构造必然失败")
    return builder


def raise_then_none_builder():
    """首次抛异常、其后返回 None（保守层首层二次失败形态）。"""
    state = {"n": 0}

    def builder(text):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("注入：首次构造失败")
        return None
    return builder


def test_p3r2_none_and_double_failure_boundary() -> None:
    """P3-R2：reply_builder 返回 None 视为绑定失败；保守回复三层
    构造永不交 None、永不传播异常；孤儿身份始终保留。"""
    # ① builder 返回 None + cancel 成功 → 取消孤儿后交回原链路
    pr1 = PendingProducer(id_gen=fixed_ids())
    store1 = CountingStore(session_id="s-r2a", now=fixed_clock())
    out1 = _produce(pr1, store1, [m("南京", "A店"), m("上海", "B店")],
                    reply_builder=none_builder())
    check("[R2①] None 返回 → 触发补偿（cancel 恰好一次）",
          store1.cancel_calls == 1, str(store1.cancel_calls))
    check("[R2①] 孤儿已被取消",
          store1.get_pending_by_id(store1.last_created_id).status
          == "cancelled", "")
    check("[R2①] 补偿成功 → 交回原链路（reply/pending_id 全 None）",
          out1.reply is None and out1.pending_id is None, str(out1))
    check("[R2①] shadow 记录 bind_failed(None)",
          any(r["action"] == "produce_bind_failed"
              and "None" in r["detail"] for r in pr1.shadow_log),
          str(pr1.shadow_log))

    # ② builder 必然抛异常 + cancel/expire 双终结故障 → 第二层自建
    #    Reply，绝不传播异常、绝不交回原链路、孤儿身份保留
    pr2 = PendingProducer(id_gen=fixed_ids())
    store2 = CountingStore(session_id="s-r2b", now=fixed_clock())
    store2.cancel_raise = RuntimeError("注入：cancel 失败")
    store2.expire_raise = RuntimeError("注入：expire 失败")
    out2 = _produce(pr2, store2, [m("南京", "A店")],
                    reply_builder=always_failing_builder())
    check("[R2②] 双重失败仍返回保守回复（异常未传播）",
          out2.reply is not None, str(out2))
    check("[R2②] 保守回复 = 自建 Reply（mode=answer/render=通告）",
          isinstance(out2.reply, Reply)
          and out2.reply.mode == "answer"
          and out2.reply.render == _ORPHAN_NOTICE,
          str(out2.reply))
    check("[R2②] 孤儿 pending_id 保留", out2.pending_id is not None, "")
    check("[R2②] shadow 记录 orphan",
          any(r["action"] == "produce_orphan_cancel_failed"
              and r["pending_id"] == out2.pending_id for r in pr2.shadow_log),
          str(pr2.shadow_log))

    # ③ builder 必然返回 None + 双终结 ok=False → 同保守拦截（第二层）
    pr3 = PendingProducer(id_gen=fixed_ids())
    store3 = CountingStore(session_id="s-r2c", now=fixed_clock())
    store3.cancel_ok_override = False
    store3.expire_ok_override = False
    out3 = _produce(pr3, store3, [m("南京", "A店")],
                    reply_builder=none_builder())
    check("[R2③] None+ok=False → 保守拦截（非 None 非 str-空）",
          out3.reply is not None
          and _ORPHAN_NOTICE in str(out3.reply), str(out3))
    check("[R2③] 孤儿 pending_id 保留", out3.pending_id is not None, "")

    # ④ 首次抛异常、保守层首层返回 None → 第二层兜底
    pr4 = PendingProducer(id_gen=fixed_ids())
    store4 = CountingStore(session_id="s-r2d", now=fixed_clock())
    store4.cancel_raise = RuntimeError("注入：cancel 失败")
    store4.expire_raise = RuntimeError("注入：expire 失败")
    out4 = _produce(pr4, store4, [m("南京", "A店")],
                    reply_builder=raise_then_none_builder())
    check("[R2④] 首层二次失败 → 第二层兜底（异常未传播）",
          out4.reply is not None
          and getattr(out4.reply, "render", "") == _ORPHAN_NOTICE, str(out4))

    # ⑤ builder 必然抛异常 + cancel ok=False → 同保守拦截（第二层）
    pr5 = PendingProducer(id_gen=fixed_ids())
    store5 = CountingStore(session_id="s-r2e", now=fixed_clock())
    store5.cancel_ok_override = False
    store5.expire_ok_override = False
    out5 = _produce(pr5, store5, [m("南京", "A店")],
                    reply_builder=always_failing_builder())
    check("[R2⑤] 异常+ok=False → 保守拦截（第二层）",
          out5.reply is not None
          and getattr(out5.reply, "render", "") == _ORPHAN_NOTICE, str(out5))

    # ⑥ 正常路径不受影响：builder 正常返回时第二/三层永不介入
    pr6 = PendingProducer(id_gen=fixed_ids())
    store6 = CountingStore(session_id="s-r2f", now=fixed_clock())
    out6 = _produce(pr6, store6, [m("南京", "A店")])
    check("[R2⑥] 正常路径逐位不变",
          out6.reply is not None and out6.pending_id is not None
          and store6.cancel_calls == 0, str(out6))


def test_p3r3_reply_contract_terminal_failure() -> None:
    """P3-R3（契约核验 2026-11-19）：保守拦截路径返回类型恒为 Reply
    （绝不字符串/None）；终层 Reply 构造失败（理论不可达）→
    PendingProduceError 携带孤儿身份，明确失败而非静默交回。"""
    # ① 终层形态：builder 必然失败 + cancel 失败 → 返回真 Reply 实例，
    #    且其字段满足服务层消费契约（mode/turn_no/ask_rounds/slots/
    #    render/plan/assumptions/trace/fetched 全部可属性访问）
    pr1 = PendingProducer(id_gen=fixed_ids())
    store1 = CountingStore(session_id="s-r3a", now=fixed_clock())
    store1.cancel_raise = RuntimeError("注入：cancel 失败")
    store1.expire_raise = RuntimeError("注入：expire 失败")   # 双终结→保守拦截
    out1 = _produce(pr1, store1, [m("南京", "A店")],
                    reply_builder=always_failing_builder())
    r = out1.reply
    check("[R3①] 保守回复是真 Reply 实例（非 str/None）",
          isinstance(r, Reply) and not isinstance(r, str), str(type(r)))
    check("[R3①] 服务层消费字段全部可属性访问",
          r.mode == "answer" and r.render == _ORPHAN_NOTICE
          and isinstance(r.slots, dict) and r.plan is None
          and isinstance(r.trace, list) and r.turn_no == 0
          and r.ask_rounds == 0 and isinstance(r.fetched, dict)
          and isinstance(r.assumptions, list), str(r.as_dict()))
    check("[R3①] as_dict 可序列化（服务层 dict 消费形态）",
          isinstance(r.as_dict(), dict)
          and r.as_dict()["mode"] == "answer", str(r.as_dict()))

    # ② 终层构造失败（monkeypatch Reply 抛异常，理论不可达路径）→
    #    PendingProduceError 明确失败，异常消息携带孤儿 Pending ID，
    #    绝不返回 None/字符串静默交回
    pr2 = PendingProducer(id_gen=fixed_ids())
    store2 = CountingStore(session_id="s-r3b", now=fixed_clock())
    store2.cancel_raise = RuntimeError("注入：cancel 失败")
    store2.expire_raise = RuntimeError("注入：expire 失败")   # 双终结→保守拦截
    orig_reply_cls = _pp_mod.Reply
    def boom_reply(**kw):
        raise RuntimeError("注入：Reply 构造失败（模拟不可达）")
    _pp_mod.Reply = boom_reply
    try:
        try:
            _produce(pr2, store2, [m("南京", "A店")],
                     reply_builder=always_failing_builder())
            check("[R3②] 终层失败 → PendingProduceError", False,
                  "未抛异常（静默交回？）")
        except PendingProduceError as exc:
            check("[R3②] 终层失败 → PendingProduceError（明确失败）",
                  True, "")
            check("[R3②] 异常消息携带孤儿 Pending ID",
                  store2.last_created_id is not None
                  and store2.last_created_id in str(exc), str(exc))
            check("[R3②] shadow 已记录 orphan（身份双通道保留）",
                  any(x["action"] == "produce_orphan_cancel_failed"
                      and x["pending_id"] == store2.last_created_id
                      for x in pr2.shadow_log), str(pr2.shadow_log))
        except Exception as exc:
            check("[R3②] 终层失败 → PendingProduceError", False,
                  f"异常类型错误: {type(exc).__name__}")
    finally:
        _pp_mod.Reply = orig_reply_cls

    # ③ monkeypatch 恢复后回归正常（隔离无残留）
    store3 = CountingStore(session_id="s-r3c", now=fixed_clock())
    store3.cancel_raise = RuntimeError("注入：cancel 失败")
    store3.expire_raise = RuntimeError("注入：expire 失败")   # 双终结→保守拦截
    out3 = _produce(pr2, store3, [m("南京", "A店")],
                    reply_builder=always_failing_builder())
    check("[R3③] 恢复后保守路径回归 Reply 终层",
          isinstance(out3.reply, Reply), str(type(out3.reply)))


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
    store2.expire_raise = RuntimeError("注入：expire 失败")  # 双终结
    out2 = _produce(pr2, store2, [m("南京", "A")],
                    reply_builder=failing_builder(fail_count=1))
    check("[影子] sink 异常不影响补偿矩阵分支",
          out2.reply is not None
          and _ORPHAN_NOTICE in str(out2.reply), str(out2))


# ================================================== P3-R4 L1 二线终结补偿
# （§11.R3.1 批准增量 2026-11-19：cancel 失败 → 二线 expire 终结；
#   R4 修正定性：尽力而为，双终结失败无即时隔离保证）


def test_p3r4_second_line_terminal() -> None:
    """P3-R4：L1 二线终结补偿四变体 + R4 场景二定位证据 + L2 边界。"""
    # ① 单终结故障：cancel 抛异常 + expire 成功 → 孤儿终态 → 交回
    #    原链路；隔离效果由权威定位判 None 可验证（§11.R3.1 判据）
    pr1 = PendingProducer(id_gen=fixed_ids())
    store1 = CountingStore(session_id="s-r4a", now=fixed_clock())
    store1.cancel_raise = RuntimeError("注入：cancel 失败")
    out1 = _produce(pr1, store1, [m("南京", "A店"), m("上海", "B店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[R4①] 二线 expire 成功 → 交回原链路",
          out1.reply is None and out1.pending_id is None, str(out1))
    check("[R4①] 孤儿已终态（expired）",
          store1.get_pending_by_id(store1.last_created_id).status
          == "expired", "")
    check("[R4①] 隔离可验证：权威定位判 None",
          store1.get_active_pending() is None, "")
    check("[R4①] cancel/expire 各恰一次",
          store1.cancel_calls == 1 and store1.expire_calls == 1,
          f"cancel={store1.cancel_calls} expire={store1.expire_calls}")
    check("[R4①] shadow 记 orphan_terminal_retry 含孤儿 ID",
          any(r["action"] == "produce_orphan_terminal_retry"
              and r["pending_id"] == store1.last_created_id
              for r in pr1.shadow_log), str(pr1.shadow_log))

    # ② cancel ok=False 形态 + expire 成功 → 同①
    pr2 = PendingProducer(id_gen=fixed_ids())
    store2 = CountingStore(session_id="s-r4b", now=fixed_clock())
    store2.cancel_ok_override = False
    out2 = _produce(pr2, store2, [m("南京", "A店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[R4②] cancel ok=False → 二线 expire 成功 → 交回",
          out2.reply is None and out2.pending_id is None, str(out2))
    check("[R4②] 孤儿已终态 + 定位 None",
          store2.get_pending_by_id(store2.last_created_id).status
          == "expired" and store2.get_active_pending() is None, "")

    # ③ 双终结故障（cancel+expire 均抛异常）→ 保守拦截，绝不交回；
    #    按 R4 冻结定性：无即时消费隔离保证，残余窗口如实暴露
    pr3 = PendingProducer(id_gen=fixed_ids())
    clock3 = {"t": datetime(2026, 11, 19, 12, 0, 0)}
    store3 = CountingStore(session_id="s-r4c", now=lambda: clock3["t"])
    store3.cancel_raise = RuntimeError("注入：cancel 失败")
    store3.expire_raise = RuntimeError("注入：expire 失败")
    out3 = _produce(pr3, store3, [m("南京", "A店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[R4③] 双终结失败 → 保守拦截（绝不交回原链路）",
          out3.reply is not None
          and _ORPHAN_NOTICE in str(out3.reply), str(out3))
    check("[R4③] 孤儿仍 open（未伪造隔离成功）",
          store3.get_pending_by_id(store3.last_created_id).status
          == "open", "")
    check("[R4③] shadow 记 terminal_retry_failed 含孤儿 ID",
          any(r["action"] == "produce_orphan_terminal_retry_failed"
              and r["pending_id"] == store3.last_created_id
              for r in pr3.shadow_log), str(pr3.shadow_log))

    # ③' 双终结 ok=False 形态 → 同③
    pr4 = PendingProducer(id_gen=fixed_ids())
    store4 = CountingStore(session_id="s-r4d", now=fixed_clock())
    store4.cancel_ok_override = False
    store4.expire_ok_override = False
    out4 = _produce(pr4, store4, [m("南京", "A店")],
                    reply_builder=failing_builder(fail_count=1))
    check("[R4③'] 双终结 ok=False → 保守拦截",
          out4.reply is not None
          and _ORPHAN_NOTICE in str(out4.reply), str(out4))

    # R4 场景二（故障恢复）：双终结失败后，下一轮存储读取正常——
    # 按裁定明确边界：**停在定位证据层**，验证残余窗口存在（孤儿
    # TTL 内仍可被权威定位），绝不进入消费流程、绝不写成必然不消费
    clock3["t"] = datetime(2026, 11, 19, 12, 0, 30)   # 存储恢复（TTL 内）
    check("[R4场景二] 故障恢复后孤儿仍可被权威定位（残余窗口证据）",
          store3.get_active_pending() is not None
          and store3.get_active_pending().pending_id
          == store3.last_created_id, "")
    check("[R4场景二] shadow 双通道保留孤儿身份",
          [r["pending_id"] for r in pr3.shadow_log
           if r["action"].startswith("produce_orphan")]
          == [store3.last_created_id, store3.last_created_id],
          str(pr3.shadow_log))
    # L2 最终清理边界（R4 定性：到期后不可经权威定位进入消费链路）
    clock3["t"] = datetime(2026, 11, 19, 12, 6, 0)    # TTL 300s 已过
    check("[R4场景二] TTL 到期后权威定位判 None（最终清理边界）",
          store3.get_active_pending() is None, "")


# ================================================== main


def main() -> int:
    test_p1_build_candidates_shape()
    test_p2_display_before_create()
    test_p3_orphan_compensation_matrix()
    test_p3r2_none_and_double_failure_boundary()
    test_p3r3_reply_contract_terminal_failure()
    test_p3r4_second_line_terminal()
    test_p4_rule8_rejection()
    test_shadow_isolation()
    print(f"\npending producer P2.5a: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
