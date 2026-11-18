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


def test_dataclass_contract() -> None:
    """交付 1：数据结构字段、枚举、候选快照契约。"""
    p = PendingDisambiguation(
        pending_id="p1", original_utterance="排海贝皇大排档",
        original_intent="plan_modify", original_role="must_visit",
        candidates=tuple(SNAP), created_at="2026-11-18T15:00:00",
        expires_at="2026-11-18T15:05:00")
    check("八字段契约齐全",
          all(hasattr(p, f) for f in
              ("pending_id", "original_utterance", "original_intent",
               "original_role", "candidates", "created_at", "expires_at",
               "status")), "")
    check("快照逐条六冻结字段齐全",
          all(all(k in c for k in
                  ("candidate_id", "display_order", "entity_id", "name",
                   "city", "address")) for c in p.candidates), "")
    check("frozen：pending_id 与快照不可变",
          p.__dataclass_params__.frozen, "")
    check("display_order 冻结为 1..N", [c["display_order"] for c in
                                        p.candidates] == [1, 2], "")
    check("as_dict 候选为列表拷贝",
          p.as_dict()["candidates"] == SNAP and
          p.as_dict()["candidates"] is not p.candidates, "")

    for bad, why in (
            (dict(status="unknown", candidates=tuple(SNAP)), "非法 status"),
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
        check("缺冻结字段的快照被拦截", False, "未抛异常")
    except ValueError as e:
        check("缺冻结字段的快照被拦截", "candidate_id" in str(e), str(e))


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
    check("③ consume 成功并回传所选候选（无行程动作）",
          out.ok and out.candidate == SNAP[0], str(out))
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


def main() -> int:
    for fn in (test_dataclass_contract, test_five_methods_lifecycle,
               test_atomic_consume, test_single_active_and_rules):
        fn()
    print(f"\npending contract: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
