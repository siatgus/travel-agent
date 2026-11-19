"""P1.5 契约测试：PendingDisambiguation 数据结构 + 五方法存储。

设计依据 docs/PHASE5_DESIGN.md v3.2.1 §7 九条消费规则，逐条锁定；
不接业务、不改 Orchestrator。运行：python tests/test_pending.py
"""

import os
import sys
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.pending import (PendingDisambiguation, PendingStore,
                                  _snap, PENDING_OPEN, PENDING_CONSUMED,
                                  PENDING_CANCELLED, PENDING_EXPIRED)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL {name}: {detail}")


def fixed_clock(start: datetime):
    """可推进的注入时钟。"""
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


SNAP = [_snap("c1", 1, "海贝皇大排档（南京店）", "南京", "nj-001", "中山路1号"),
        _snap("c2", 2, "海贝皇大排档（上海店）", "上海", "sh-001", "南京西路2号")]

from travel_agent.pending import PendingCandidate, _candidate_from_dict

FROZEN = tuple(_candidate_from_dict(d) for d in SNAP)


def test_dataclass_contract() -> None:
    """交付 1：数据结构字段、枚举、候选快照契约。"""
    p = PendingDisambiguation(
        pending_id="p1", original_utterance="排海贝皇大排档",
        original_intent="plan_modify", original_role="must_visit",
        candidates=FROZEN, created_at="2026-11-18T15:00:00",
        expires_at="2026-11-18T15:05:00")
    check("八字段契约齐全",
          all(hasattr(p, f) for f in
              ("pending_id", "original_utterance", "original_intent",
               "original_role", "candidates", "created_at", "expires_at",
               "status")), "")
    check("快照逐条六冻结字段齐全",
          all(all(hasattr(c, k) for k in
                  ("candidate_id", "display_order", "entity_id", "name",
                   "city", "address")) for c in p.candidates), "")
    check("frozen：pending_id 与快照不可变",
          p.__dataclass_params__.frozen
          and p.candidates[0].__dataclass_params__.frozen, "")
    check("display_order 冻结为 1..N", [c.display_order for c in
                                        p.candidates] == [1, 2], "")
    check("as_dict 候选输出形状与 P1.5 完全兼容",
          p.as_dict()["candidates"] == SNAP, "")

    for bad, why in (
            (dict(status="unknown", candidates=FROZEN), "非法 status"),
            (dict(candidates=()), "open 无候选"),
    ):
        try:
            PendingDisambiguation(
                pending_id="px", original_utterance="排海贝皇",
                original_intent="plan_modify", original_role="must_visit",
                created_at="2026-11-18T15:00:00",
                expires_at="2026-11-18T15:05:00", **bad)
            check(f"非法构造被拦截：{why}", False, "未抛异常")
        except ValueError:
            check(f"非法构造被拦截：{why}", True, "")

    try:
        PendingDisambiguation(
            pending_id="py", original_utterance="排海贝皇",
            original_intent="plan_modify", original_role="must_visit",
            candidates=({"name": "缺字段"},),
            created_at="2026-11-18T15:00:00",
            expires_at="2026-11-18T15:05:00")
        check("dict 快照直构被拦截（须经 store 转换）", False, "未抛异常")
    except TypeError as e:
        check("dict 快照直构被拦截（须经 store 转换）",
              "PendingCandidate" in str(e), str(e))


def test_five_methods_lifecycle() -> None:
    """交付 2：五方法接口 create/get/consume/cancel/expire。"""
    st = PendingStore()
    p = st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)
    check("① create 返回 open pending",
          p.status == PENDING_OPEN and p.pending_id, str(p.status))
    got = st.get_active_pending()
    check("② get_active_pending 返回同一 pending",
          got is not None and got.pending_id == p.pending_id, str(got))
    out = st.consume_pending(p.pending_id, "c1")
    check("③ consume 成功并回传所选候选（frozen，无行程动作）",
          out.ok and isinstance(out.candidate, PendingCandidate)
          and out.candidate.candidate_id == "c1", str(out))
    check("③ consume 后 status=consumed",
          st.get_pending_by_id(p.pending_id).status == PENDING_CONSUMED, "")
    check("③ 消费后无 active pending（单 active 契约）",
          st.get_active_pending() is None, "")

    st2 = PendingStore()
    p2 = st2.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    c = st2.cancel_pending(p2.pending_id)
    check("④ cancel 成功 → cancelled",
          c.ok and st2.get_pending_by_id(p2.pending_id).status ==
          PENDING_CANCELLED, str(c))
    check("④ 取消后 get_active_pending 返回 None（规则 2 清除）",
          st2.get_active_pending() is None, "")

    st3 = PendingStore()
    p3 = st3.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    e = st3.expire_pending(p3.pending_id)
    check("⑤ expire 成功 → expired",
          e.ok and st3.get_pending_by_id(p3.pending_id).status ==
          PENDING_EXPIRED, str(e))
    check("⑤ 过期后 get_active_pending 返回 None（规则 5）",
          st3.get_active_pending() is None, "")


def test_atomic_consume() -> None:
    """交付 3：open → consumed 只能成功一次；快照外 ID 拒绝。"""
    st = PendingStore()
    p = st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)
    out1 = st.consume_pending(p.pending_id, "c2")
    out2 = st.consume_pending(p.pending_id, "c1")   # 重复提交
    check("首次消费成功", out1.ok, str(out1))
    check("重复提交失败（规则 6：只能成功消费一次）",
          not out2.ok and "已消费" in out2.reason, str(out2))
    check("重复消费不改变所选候选",
          out2.candidate is None, str(out2.candidate))

    st2 = PendingStore()
    p2 = st2.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)
    out3 = st2.consume_pending(p2.pending_id, "c999")
    check("快照外 candidate_id 拒绝（规则 9）",
          not out3.ok and "快照" in out3.reason, str(out3))
    check("拒绝后 pending 仍可正常消费",
          st2.consume_pending(p2.pending_id, "c1").ok, "")

    out4 = st2.consume_pending("不存在", "c1")
    check("pending_id 不匹配拒绝",
          not out4.ok and "不匹配" in out4.reason, str(out4))

    # 并发原子性：两线程同时消费，恰好一个成功（等效条件更新语义）
    st3 = PendingStore()
    p3 = st3.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    results = []
    barrier = threading.Barrier(2)

    def worker(cid: str) -> None:
        barrier.wait()
        results.append(st3.consume_pending(p3.pending_id, cid).ok)

    t1 = threading.Thread(target=worker, args=("c1",))
    t2 = threading.Thread(target=worker, args=("c2",))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发消费恰好一次成功（原子迁移）",
          sum(results) == 1, str(results))
    check("并发消费后 status=consumed",
          st3.get_pending_by_id(p3.pending_id).status == PENDING_CONSUMED, "")


def test_single_active_and_rules() -> None:
    """规则 8（单 active）/ 3（新问题不误消费）/ 4（改目的地作废）
    / 5（过期不可执行）。"""
    st = PendingStore()
    p1 = st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)
    try:
        st.create_pending("换个问题", "poi_query", "query", SNAP)
        check("存在 active 时再 create 被拒绝（规则 8）", False, "未抛异常")
    except ValueError as e:
        check("存在 active 时再 create 被拒绝（规则 8）",
              "active pending" in str(e), str(e))

    # 规则 3：新问题 → 显式取消旧 pending，绝不静默消费
    st.cancel_pending(p1.pending_id)
    check("新问题前旧 pending 已取消，get_active 返回 None（规则 3）",
          st.get_active_pending() is None, "")
    p2 = st.create_pending("介绍一下总统府", "poi_qa", "query", SNAP)
    # 规则 4：修改目的地 → 调用方重新验证，旧快照作废
    st.cancel_pending(p2.pending_id)
    p3 = st.create_pending("改去南京了，排海贝皇大排档", "plan_modify",
                           "must_visit", SNAP)
    check("改目的地后旧快照作废、新建 pending 重新验证（规则 4）",
          p3.original_utterance != p2.original_utterance
          and p3.status == PENDING_OPEN, str(p3.status))

    # 规则 5：过期 pending 不可消费、get_active 不可见
    clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    st4 = PendingStore(ttl_seconds=60, now=clock)
    p4 = st4.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)
    check("有效期内可消费可见", st4.get_active_pending() is not None, "")
    advance(61)
    check("过期后 get_active_pending 返回 None（规则 5）",
          st4.get_active_pending() is None, "")
    out = st4.consume_pending(p4.pending_id, "c1")
    check("过期后消费被拒绝且 reason 可归因",
          not out.ok and "过期" in out.reason, str(out))


def test_immutable_snapshot_p21() -> None:
    """P2.1 专项：快照真正不可变 + 接口兼容（验收冻结项）。"""
    st = PendingStore()
    src = [_snap("c1", 1, "海贝皇大排档（南京店）", "南京"),
           _snap("c2", 2, "海贝皇大排档（上海店）", "上海")]
    p = st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", src)

    # 验收点：输入 dict 后续修改不能污染 Pending 快照
    src[0]["name"] = "被污染的名字"
    src[0]["city"] = "被污染的城市"
    got = st.get_active_pending()
    check("入参 dict 事后修改不污染快照",
          got.candidates[0].name == "海贝皇大排档（南京店）"
          and got.candidates[0].city == "南京",
          f"{got.candidates[0].name}/{got.candidates[0].city}")

    # 验收点：PendingCandidate 必须是真正的 frozen dataclass
    try:
        p.candidates[0].name = "另一个地点"
        check("候选字段赋值被拦截（真正不可变）", False, "未抛异常")
    except Exception as e:
        from dataclasses import FrozenInstanceError
        check("候选字段赋值被拦截（真正不可变）",
              isinstance(e, FrozenInstanceError), str(e))

    # 验收点：candidate_by_id 返回 frozen 结构，无 dict 可改路径
    c = got.candidate_by_id("c1")
    check("candidate_by_id 返回 frozen PendingCandidate",
          isinstance(c, PendingCandidate)
          and c.__dataclass_params__.frozen, str(type(c)))
    check("candidate_as_dict 兼容旧调用方（拷贝 dict）",
          got.candidate_as_dict("c1")["name"] == "海贝皇大排档（南京店）"
          and got.candidate_as_dict("999") is None, "")
    check("snapshot_candidate_ids 行为与返回结构不变",
          got.snapshot_candidate_ids() == ("c1", "c2"), "")

    # 验收点：as_dict 外部结构保持兼容（list[dict]，六字段齐全）
    d = got.as_dict()
    check("as_dict 外部结构兼容",
          isinstance(d["candidates"], list)
          and all(isinstance(x, dict) and set(x) == {
              "candidate_id", "display_order", "entity_id", "name",
              "city", "address"} for x in d["candidates"]), str(d))

    # 消费语义回归：frozen 候选贯穿 consume 结果与状态迁移
    out = st.consume_pending(p.pending_id, "c2")
    check("frozen 候选贯穿消费（快照外仍拒绝）",
          out.ok and out.candidate.name == "海贝皇大排档（上海店）", str(out))
    check("消费后再次消费仍被拒绝（原子语义不变）",
          not st.consume_pending(p.pending_id, "c1").ok, "")

    # 缺字段 / 坏序号的 dict 在 store 转换处被拦截
    for bad, why in (
            ([{"candidate_id": "c9"}], "缺冻结字段"),
            ([_snap("c1", 2, "店", "城"), _snap("c2", 2, "店", "城")],
             "序号非 1..N"),
    ):
        try:
            st.create_pending("x", "poi_qa", "query", bad)
            check(f"store 转换拦截：{why}", False, "未抛异常")
        except ValueError:
            check(f"store 转换拦截：{why}", True, "")


def test_consume_atomicity_fault_injection() -> None:
    """P2.3-R1/R2：消费原子性故障注入 + candidate 冻结契约。

    R1：前置步骤（时钟/epoch/记录构造）任一失败 → pending 保持
    open、无消费记录（无「已 consumed 但记录缺失」半成功状态）；
    恢复后仍可成功消费。
    R2：_ConsumeOutcome.candidate 为冻结 PendingCandidate（P2.1），
    candidate_as_dict() 兼容垫片返回六字段 dict。
    """
    from travel_agent.pending import SNAPSHOT_FIELDS

    # ---- R1：时钟抛异常 ----
    clock, advance = fixed_clock(datetime(2026, 11, 18, 16, 0, 0))
    st = PendingStore(ttl_seconds=300, now=clock)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    boom = {"on": True}

    def broken_now():
        if boom["on"]:
            raise RuntimeError("时钟故障注入")
        return clock()

    st._now = broken_now
    try:
        st.consume_pending(p.pending_id, "c1")
        raised = False
    except RuntimeError:
        raised = True
    check("R1 时钟故障 → 异常如实抛出（不吞异常不伪装失败）", raised, "")
    check("R1 故障后 pending 仍 open",
          st.get_pending_by_id(p.pending_id).status == PENDING_OPEN,
          st.get_pending_by_id(p.pending_id).status)
    check("R1 故障后 _latest_consumption() 为 None（无半成功记录）",
          st._latest_consumption() is None, str(st._latest_consumption()))
    # 恢复时钟后仍可成功消费（同一 pending、同一候选）
    boom["on"] = False
    out = st.consume_pending(p.pending_id, "c1")
    check("R1 恢复时钟后消费成功", out.ok, str(out))
    rec = st._latest_consumption()
    check("R1 恢复后记录恰好一条且七字段正确",
          rec is not None and rec["pending_id"] == p.pending_id
          and rec["candidate_id"] == "c1"
          and rec["original_intent"] == "plan_modify"
          and rec["selected_name"] == "海贝皇大排档（南京店）"
          and rec["selected_city"] == "南京"
          and isinstance(rec["consumed_at"], int), str(rec))
    out2 = st.consume_pending(p.pending_id, "c2")
    check("R1 成功后同一 pending 不可再消费（恰一次）",
          not out2.ok and "已消费" in out2.reason, str(out2))

    # ---- R1 变体：时钟返回非法类型（epoch 转换失败）----
    clock3, _ = fixed_clock(datetime(2026, 11, 18, 16, 0, 0))
    st3 = PendingStore(ttl_seconds=300, now=clock3)
    p3 = st3.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    st3._now = lambda: "不是时间"   # _epoch 将抛 TypeError
    try:
        st3.consume_pending(p3.pending_id, "c1")
        raised3 = False
    except (TypeError, RuntimeError):
        raised3 = True
    check("R1 非法时钟类型 → 异常抛出", raised3, "")
    check("R1 非法时钟后 pending 仍 open",
          st3.get_pending_by_id(p3.pending_id).status == PENDING_OPEN, "")
    check("R1 非法时钟后无消费记录",
          st3._latest_consumption() is None, "")

    # ---- R1 变体：状态对象构造（replace → __post_init__）失败 ----
    # 验证 replace() 已提前到前置段：构造失败时记录也绝不先写入
    import travel_agent.pending as _pmod
    orig_post = _pmod.PendingDisambiguation.__post_init__

    def _boom_post(self):
        raise RuntimeError("状态对象构造故障注入")

    clock5, _ = fixed_clock(datetime(2026, 11, 18, 16, 0, 0))
    st5 = PendingStore(ttl_seconds=300, now=clock5)
    p5 = st5.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    _pmod.PendingDisambiguation.__post_init__ = _boom_post
    try:
        st5.consume_pending(p5.pending_id, "c1")
        raised5 = False
    except RuntimeError:
        raised5 = True
    finally:
        _pmod.PendingDisambiguation.__post_init__ = orig_post
    check("R1 状态对象构造失败 → 异常抛出", raised5, "")
    check("R1 构造失败后 pending 仍 open",
          st5.get_pending_by_id(p5.pending_id).status == PENDING_OPEN, "")
    check("R1 构造失败后无消费记录（记录绝不先于状态写入）",
          st5._latest_consumption() is None, "")
    out5b = st5.consume_pending(p5.pending_id, "c1")
    check("R1 构造恢复后消费成功", out5b.ok, str(out5b))

    # ---- R2：candidate 冻结契约 + dict 兼容垫片 ----
    clock4, _ = fixed_clock(datetime(2026, 11, 18, 16, 0, 0))
    st4 = PendingStore(ttl_seconds=300, now=clock4)
    p4 = st4.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    out4 = st4.consume_pending(p4.pending_id, "c1")
    cand = out4.candidate
    check("R2 candidate 是 PendingCandidate 冻结结构",
          isinstance(cand, PendingCandidate)
          and cand.__dataclass_params__.frozen, repr(type(cand)))
    check("R2 冻结候选不可属性赋值",
          _raises_frozen(cand), "")
    d = out4.candidate_as_dict()
    check("R2 candidate_as_dict 垫片返回六字段 dict",
          isinstance(d, dict) and set(d) == set(SNAPSHOT_FIELDS)
          and d["candidate_id"] == "c1"
          and d["name"] == "海贝皇大排档（南京店）", str(d))
    check("R2 垫片 dict 是拷贝（改它不动冻结快照）",
          (d.__setitem__("name", "篡改"), True)[1]
          and cand.name == "海贝皇大排档（南京店）", "")
    # SQLite 实现同契约抽查（返回对象同为冻结结构 + 垫片可用）
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    st5 = SQLitePendingStore("s-r2", db_path=temp_db(),
                             ttl_seconds=300, now=clock4)
    p5 = st5.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    out5 = st5.consume_pending(p5.pending_id, "c1")
    check("R2 SQLite candidate 同为冻结 PendingCandidate",
          out5.ok and isinstance(out5.candidate, PendingCandidate)
          and out5.candidate.__dataclass_params__.frozen, str(out5))
    check("R2 SQLite 垫片同样返回六字段 dict",
          isinstance(out5.candidate_as_dict(), dict)
          and set(out5.candidate_as_dict()) == set(SNAPSHOT_FIELDS), "")


def _raises_frozen(obj) -> bool:
    try:
        obj.name = "篡改"
        return False
    except Exception:
        return True


def temp_db() -> str:
    """R2 SQLite 抽查用的临时库路径。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def main() -> int:
    for fn in (test_dataclass_contract, test_five_methods_lifecycle,
               test_atomic_consume, test_single_active_and_rules,
               test_immutable_snapshot_p21,
               test_consume_atomicity_fault_injection):
        fn()
    print(f"\npending contract: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
