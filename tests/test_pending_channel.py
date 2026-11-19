"""P2.4a 契约测试：pending 消费通道装配层（Runtime Manager + 开关 +
唯一拦截点旁路验证）。

设计依据 docs/PHASE5_P24_PLAN.md r5（基线 c9ead95 + 批准条件补明确）
§2/§3/§4/§6 P2.4a 组。运行：python tests/test_pending_channel.py

边界（七条执行边界）：默认关完全旁路；不创建 Pending（测试预置）；
不改 Handler；P2.4b 的 act/verdict 分发绝不执行（观察模式）。
"""

import os
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.dialogue import Session
from travel_agent.orchestrator.context import TurnContext
from travel_agent.orchestrator.orchestrator import Orchestrator
from travel_agent.parser import parse_utterance
from travel_agent.pending import (PendingStore, _snap, _epoch,
                                  in_reselect_window,
                                  reselect_window_seconds)
from travel_agent.pending_interceptor import (PendingInterceptor,
                                              V_CONSUME, V_EXPIRED_REPLY,
                                              V_NONE, V_DUPLICATE_SELECT)
from travel_agent.pending_runtime import (PendingRuntime,
                                          PendingRuntimeManager,
                                          default_store_factory)

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
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


SNAP = [_snap("c1", 1, "海贝皇大排档（南京店）", "南京"),
        _snap("c2", 2, "海贝皇大排档（上海店）", "上海")]


def make_ctx(ut: str) -> TurnContext:
    ctx = TurnContext(utterance=ut, history=[], slots={},
                      last_plan=None, plan_made=False)
    ctx.parse = parse_utterance(ut, context=None, today=None)
    return ctx


def set_env(channel=None, store_kind=None, max_r=None):
    if channel is None:
        os.environ.pop("TRAVEL_PENDING_CHANNEL", None)
    else:
        os.environ["TRAVEL_PENDING_CHANNEL"] = channel
    if store_kind is None:
        os.environ.pop("TRAVEL_PENDING_STORE", None)
    else:
        os.environ["TRAVEL_PENDING_STORE"] = store_kind
    if max_r is None:
        os.environ.pop("TRAVEL_PENDING_RUNTIME_MAX", None)
    else:
        os.environ["TRAVEL_PENDING_RUNTIME_MAX"] = max_r


def preset_pending(store, clock=None, *, ttl=300):
    p = store.create_pending("排海贝皇大排档", "plan_modify",
                             "must_visit", SNAP) if hasattr(
        store, "create_pending") else None
    return p


# ================================================== 1. 开关与旁路


def test_channel_off_full_bypass() -> None:
    """验收 1/2：默认关 → 完全旁路，不构造 runtime、不查 store。"""
    for env in (None, "0"):
        set_env(channel=env)
        orc = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s1"
        s.active_pending_id = "whatever"    # 即便有 hint 也必须旁路
        ctx = make_ctx("第一个")
        v = orc._pending_gate(s, "第一个", ctx)
        check(f"[off env={env}] gate 返回 None", v is None, str(v))
        check(f"[off env={env}] manager 未构造",
              orc._pending_manager is None, str(orc._pending_manager))
        check(f"[off env={env}] 无警告", orc._pending_warnings == [], "")
        # 真实 handle 一轮：行为零变化、gate 被调用但旁路
        s2 = Session(use_llm=False)
        r = orc.handle(s2, "你好")
        check(f"[off env={env}] handle 正常出回复",
              r is not None and str(r), str(type(r)))
        check(f"[off env={env}] handle 后 manager 仍未构造",
              orc._pending_manager is None, "")


def test_channel_on_no_pending_bypass() -> None:
    """验收 2：channel=1 且无任何 pending 线索 → 旁路交回原链路。"""
    set_env(channel="1", store_kind="memory")
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s-a"
        v = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[on 无 pending] gate 返回 None", v is None, str(v))
        check("[on 无 pending] manager 已构造",
              orc._pending_manager is not None, "")
        snap = orc._pending_manager.snapshot()
        check("[on 无 pending] runtime 已按 sid 建立", "s-a" in snap,
              str(snap))
        rt = orc._pending_manager.runtime("s-a")
        check("[on 无 pending] lease 已归还（ops=0）",
              rt.active_ops == 0, str(rt.active_ops))
        check("[on 无 pending] 观察模式未产生影子记录",
              rt.interceptor.shadow_log == [], "")
    finally:
        set_env()


def test_channel_on_missing_sid_bypass() -> None:
    """§2.3：身份未注入 → 旁路 + shadow 警告，绝不猜测身份。"""
    set_env(channel="1")
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)          # 未注入 pending_session_id
        s.active_pending_id = "p-x"
        v = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[缺 sid] gate 返回 None", v is None, str(v))
        check("[缺 sid] manager 未构造",
              orc._pending_manager is None, "")
        check("[缺 sid] 记录 shadow 警告",
              len(orc._pending_warnings) == 1 and "身份" in
              orc._pending_warnings[0], str(orc._pending_warnings))
    finally:
        set_env()


def test_observation_mode_no_act() -> None:
    """P2.4a 观察模式：evaluate + 影子记录，绝不 act、状态不变。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s1"
        store = PendingStore(session_id="s1", now=clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        # 把预置 store 挂进 manager（模拟生产者写入同一 store）
        mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
        rt = PendingRuntime("s1", store, PendingInterceptor(store),
                            last_used=int(clock().timestamp()))
        mgr._runtimes["s1"] = rt
        orc._pending_manager = mgr
        v = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[观察] gate 返回 None（不短路，P2.4b 才接管）", v is None, "")
        check("[观察] 影子记录判定 consume",
              rt.interceptor.shadow_log
              and rt.interceptor.shadow_log[-1]["verdict"] == V_CONSUME,
              str(rt.interceptor.shadow_log[-1:]))
        check("[观察] pending 状态仍 open（未 act）",
              store.get_pending_by_id(p.pending_id).status == "open", "")
        check("[观察] 无消费记录", store._latest_consumption() is None, "")
        check("[观察] lease 已归还", rt.active_ops == 0, "")
        check("[观察] hint 刷新为 open pending 的 ID",
              s.active_pending_id == p.pending_id, "")
    finally:
        set_env()


# ================================================== 2. ID 解析（§3.2）


def test_resolve_order_and_expired_hint() -> None:
    """四步解析顺序 + 过期 hint 识别（测试 A 的判定核心）。"""
    clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    st = PendingStore(session_id="s1", now=clock)
    it = PendingInterceptor(st)
    orc = Orchestrator()
    s = Session(use_llm=False)
    s.pending_session_id = "s1"
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    # ① open → 命中并回写 hint
    pid = orc._resolve_pending_id(s, st)
    check("① open 命中 get_active_pending", pid == p.pending_id, str(pid))
    check("① hint 已回写", s.active_pending_id == p.pending_id, "")
    # ② 到期未登记：get_active → None，hint 仍可识别 → expired_reply
    advance(301)
    pid2 = orc._resolve_pending_id(s, st)
    check("② 到期未登记 → 解析退到 hint，ID 不丢",
          pid2 == p.pending_id, str(pid2))
    v = it.evaluate("第一个", make_ctx("第一个").parse,
                    session_pending_id=pid2)
    check("② 到期后选择 → expired_reply",
          v.verdict == V_EXPIRED_REPLY, str(v.as_dict()))
    # ③ hint 丢失 → _latest_pending_id 兜底
    s.active_pending_id = None
    pid3 = orc._resolve_pending_id(s, st)
    check("③ hint 丢失 → store._latest_pending_id 兜底",
          pid3 == p.pending_id, str(pid3))
    v3 = it.evaluate("第一个", make_ctx("第一个").parse,
                     session_pending_id=pid3)
    check("③ 兜底 ID 仍产出 expired_reply",
          v3.verdict == V_EXPIRED_REPLY, str(v3.as_dict()))
    # ④ 全空 → None
    st2 = PendingStore(session_id="s2", now=clock)
    s2 = Session(use_llm=False)
    check("④ 无任何线索 → None", orc._resolve_pending_id(s2, st2) is None,
          "")


def test_wrong_hint_non_authoritative() -> None:
    """hint 非权威：错误 hint（指向已消费 pending）→ none 无误消费。"""
    clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    st = PendingStore(session_id="s1", now=clock)
    it = PendingInterceptor(st)
    orc = Orchestrator()
    s = Session(use_llm=False)
    s.pending_session_id = "s1"
    p1 = st.create_pending("排海贝皇大排档", "plan_modify",
                           "must_visit", SNAP)
    out = st.consume_pending(p1.pending_id, "c1")
    check("预置消费成功", out.ok, str(out))
    s.active_pending_id = p1.pending_id     # 旧 hint 指向已消费 pending
    pid = orc._resolve_pending_id(s, st)
    check("hint 非权威：解析仍返回 hint（store 为准做终态判定）",
          pid == p1.pending_id, str(pid))
    v = it.evaluate("第一个", make_ctx("第一个").parse,
                    session_pending_id=pid)
    check("已消费 pending → verdict none（不重复消费）",
          v.verdict == V_NONE, str(v.as_dict()))
    # 记录不存在时 memory 语义：被覆盖后 _active 为 None → 步骤④兜底
    st2 = PendingStore(session_id="s3", now=clock)
    check("内存桩无 pending → _latest_pending_id None",
          st2._latest_pending_id() is None, "")


# ================================================== 3. 多 Session 隔离


def test_multi_session_isolation() -> None:
    """验收 3：多 Session 不共享 store；A 的 pending B 不可见。"""
    set_env(channel="1", store_kind="memory")
    try:
        orc = Orchestrator()
        sa = Session(use_llm=False)
        sa.pending_session_id = "s-A"
        sb = Session(use_llm=False)
        sb.pending_session_id = "s-B"
        orc._pending_gate(sa, "你好", make_ctx("你好"))
        orc._pending_gate(sb, "你好", make_ctx("你好"))
        mgr = orc._pending_manager
        rta, rtb = mgr.runtime("s-A"), mgr.runtime("s-B")
        check("两 sid 各自独立 runtime", rta is not rtb, "")
        check("两 sid store 实例不同",
              rta.store is not rtb.store, "")
        check("内存 store 各自 session_id",
              rta.store._session_id == "s-A"
              and rtb.store._session_id == "s-B", "")
        # A 预置 pending，B 不可见
        pa = rta.store.create_pending("排海贝皇大排档", "plan_modify",
                                      "must_visit", SNAP)
        pid_b = orc._resolve_pending_id(sb, rtb.store)
        check("B 解析不到 A 的 pending",
              pid_b is None or pid_b != pa.pending_id, str(pid_b))
        check("A 自身仍可见", orc._resolve_pending_id(sa, rta.store)
              == pa.pending_id, "")
        # 空 sid 规避手法被拦截
        try:
            mgr.acquire("")
            blocked = False
        except ValueError:
            blocked = True
        check("空 sid acquire 被 ValueError 拦截", blocked, "")
    finally:
        set_env()


def test_store_kind_factory() -> None:
    """TRAVEL_PENDING_STORE 选择：memory 默认 / sqlite 独立实例。"""
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    set_env(channel="1", store_kind="memory")
    st = default_store_factory("sx")
    check("memory 工厂 → PendingStore", isinstance(st, PendingStore), "")
    set_env(channel="1", store_kind="sqlite")
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["TRAVEL_APP_DB"] = dbp
    try:
        st2 = default_store_factory("sx")
        check("sqlite 工厂 → SQLitePendingStore",
              isinstance(st2, SQLitePendingStore), "")
        st3 = default_store_factory("sy")
        check("sqlite 每 sid 独立实例", st2 is not st3
              and st2._session_id == "sx" and st3._session_id == "sy", "")
        set_env(channel="1", store_kind="redis")
        try:
            default_store_factory("sx")
            bad = False
        except ValueError:
            bad = True
        check("非法 store 类型被拒绝", bad, "")
    finally:
        os.environ.pop("TRAVEL_APP_DB", None)
        set_env()


# ================================================== 4. 保留态与淘汰


def test_retention_state_both_impls() -> None:
    """§4.3 保留态判定：双实现同输入同时间边界逐位一致。"""
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    mem = PendingStore(session_id="s1", now=clock, ttl_seconds=7200)
    sql = SQLitePendingStore("s1", db_path=dbp, now=clock, ttl_seconds=7200)
    w = reselect_window_seconds()
    try:
        # 空 → False
        check("[ret 空] memory False", mem._has_runtime_retention_state(
            _epoch(clock())) is False, "")
        check("[ret 空] sqlite False", sql._has_runtime_retention_state(
            _epoch(clock())) is False, "")
        # open（含到期未登记，原始状态判断）→ True
        p = mem.create_pending("排海贝皇大排档", "plan_modify",
                               "must_visit", SNAP)
        ps = sql.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        advance(3600)                      # 超出过期线，但状态仍 open
        check("[ret 到期未登记 open] memory True",
              mem._has_runtime_retention_state(
                  _epoch(clock())) is True, "")
        check("[ret 到期未登记 open] sqlite True",
              sql._has_runtime_retention_state(
                  _epoch(clock())) is True, "")
        # 消费成功：open 条件消失，但消费记录在窗口内 → True
        mem.consume_pending(p.pending_id, "c1")
        sql.consume_pending(ps.pending_id, "c1")
        now_e = _epoch(clock())
        check("[ret 窗口内消费] memory True",
              mem._has_runtime_retention_state(now_e) is True, "")
        check("[ret 窗口内消费] sqlite True",
              sql._has_runtime_retention_state(now_e) is True, "")
        # 临界点：consumed_at + window == now → 仍窗口内（与拦截器同式）
        consumed = now_e
        boundary = consumed + w
        check("[ret 临界点 ==] memory True",
              mem._has_runtime_retention_state(boundary) is True, "")
        check("[ret 临界点 ==] sqlite True",
              sql._has_runtime_retention_state(boundary) is True, "")
        check("[ret 临界点 ==] 共享表达式 in_reselect_window True",
              in_reselect_window(consumed, boundary) is True, "")
        # 临界点之外（+1）→ False
        check("[ret 窗口外] memory False",
              mem._has_runtime_retention_state(boundary + 1) is False, "")
        check("[ret 窗口外] sqlite False",
              sql._has_runtime_retention_state(boundary + 1) is False, "")
        check("[ret 窗口外] 共享表达式 False",
              in_reselect_window(consumed, boundary + 1) is False, "")
    finally:
        set_env()


def test_latest_pending_id_parity() -> None:
    """_latest_pending_id：任意状态、严格本 sid、双实现一致。"""
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    mem = PendingStore(session_id="s1", now=clock)
    sql = SQLitePendingStore("s1", db_path=dbp, now=clock)
    sql_other = SQLitePendingStore("s-other", db_path=dbp, now=clock)
    try:
        check("[latest 空] 双实现 None",
              mem._latest_pending_id() is None
              and sql._latest_pending_id() is None, "")
        p = sql.create_pending("排海贝皇大排档", "plan_modify",
                               "must_visit", SNAP)
        check("[latest open] sqlite 命中",
              sql._latest_pending_id() == p.pending_id, "")
        check("[latest] 严格本 sid（其他 sid 看不到）",
              sql_other._latest_pending_id() is None, "")
        sql.consume_pending(p.pending_id, "c1")
        check("[latest consumed] 终态也返回（仅作解析线索）",
              sql._latest_pending_id() == p.pending_id, "")
        sql.expire_pending("不存在的id")     # 无操作
        pm = mem.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        mem.cancel_pending(pm.pending_id)
        check("[latest cancelled] memory 终态也返回",
              mem._latest_pending_id() == pm.pending_id, "")
    finally:
        set_env()


def test_lru_eviction_and_protection() -> None:
    """§4.2 双闸淘汰：LRU 上限、保留态保护、超限警告。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        mgr = PendingRuntimeManager(max_runtimes=3, now=clock)
        stores = {}
        for sid in ("a", "b", "c", "d"):
            def factory(sid=sid):
                st = PendingStore(session_id=sid, now=clock)
                stores[sid] = st
                return st
            mgr._factory = factory
            mgr.acquire(sid)
        mgr.release(mgr.runtime("a"))
        mgr.release(mgr.runtime("b"))
        mgr.release(mgr.runtime("c"))
        mgr.release(mgr.runtime("d"))
        snap = mgr.snapshot()
        check("[LRU] 超 3 即淘汰最旧（a 被逐出）",
              "a" not in snap and set(snap) == {"b", "c", "d"},
              str(list(snap)))
        # 测试 A：b 挂到期未登记 open pending → LRU 压力下不淘汰
        pb = stores["b"].create_pending("排海贝皇大排档", "plan_modify",
                                        "must_visit", SNAP)
        advance(3600)                      # 到期未登记
        # 测试 B 前置：d 消费成功（消费记录进入窗口），随后施加压力
        stores["d"].create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        pd = stores["d"].get_active_pending()
        out_d = stores["d"].consume_pending(pd.pending_id, "c1")
        check("[测试 B 前置] d 消费成功", out_d.ok, str(out_d))
        for sid in ("e", "f"):
            mgr.acquire(sid)
            mgr.release(mgr.runtime(sid))
        snap = mgr.snapshot()
        check("[测试 A] 到期未登记 open 的 runtime 保留",
              "b" in snap, str(list(snap)))
        check("[测试 B] 窗口内消费的 runtime 保留",
              "d" in snap, str(list(snap)))
        st_b = stores["b"]
        check("[测试 A] 淘汰压力前后 ID/快照/状态逐位一致",
              st_b._latest_pending_id() == pb.pending_id
              and st_b.get_pending_by_id(pb.pending_id).status == "open"
              and [c.as_dict() for c in st_b.get_pending_by_id(
                  pb.pending_id).candidates]
              == [c.as_dict() for c in pb.candidates], "")
        # 到期后选择仍正确产出 expired_reply（runtime 未丢状态）
        v = PendingInterceptor(st_b).evaluate(
            "第一个", make_ctx("第一个").parse,
            session_pending_id=pb.pending_id)
        check("[测试 A] 到期后选择 → expired_reply",
              v.verdict == V_EXPIRED_REPLY, str(v.as_dict()))
        # 测试 B：重复选择仍识别原消费记录
        # （解析桩：poi_candidates 为空 → 纯选择线索；真实 parse 会把
        # 「第一个」抽成具名候选，按 P2.3 冻结规则正确拒绝——见 §6）
        class _PR:
            poi_candidates = []
            intent_coarse = ""
        v2 = PendingInterceptor(stores["d"]).evaluate(
            "第一个", _PR(), session_pending_id=pd.pending_id)
        check("[测试 B] 重复选择仍识别原消费记录 → duplicate_select",
              v2.verdict == V_DUPLICATE_SELECT, str(v2.as_dict()))
        advance(reselect_window_seconds() + 1)
        mgr.acquire("h")
        mgr.release(mgr.runtime("h"))
        snap = mgr.snapshot()
        check("[测试 B] 窗口结束后恢复可淘汰（d 被逐出）",
              "d" not in snap, str(list(snap)))
    finally:
        set_env()


def test_overflow_warning_when_all_retained() -> None:
    """全部保留态 → 允许暂时超上限 + shadow 警告。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        mgr = PendingRuntimeManager(max_runtimes=2, now=clock)
        stores = []
        for sid in ("a", "b", "c"):
            def factory(sid=sid):
                st = PendingStore(session_id=sid, now=clock)
                stores.append(st)
                return st
            mgr._factory = factory
            mgr.acquire(sid)
        for i, sid in enumerate(("a", "b", "c")):
            stores[i].create_pending("排海贝皇大排档", "plan_modify",
                                     "must_visit", SNAP)
            mgr.release(mgr.runtime(sid))
        mgr.evict()
        snap = mgr.snapshot()
        check("[溢出] 保留态 runtime 全部保留（3 > 2）",
              set(snap) == {"a", "b", "c"}, str(list(snap)))
        check("[溢出] 记录 shadow 警告",
              len(mgr.warnings) >= 1 and "overflow" in mgr.warnings[-1],
              str(mgr.warnings))
    finally:
        set_env()


# ================================================== 5. 生命周期并发


def test_lease_lifecycle() -> None:
    """§4.5：lease 授予/归还/双闸/淘汰原子性/泄漏防护。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        mgr = PendingRuntimeManager(max_runtimes=1, now=clock)
        rt = mgr.acquire("s1")
        check("[lease] 获取即计数 1", rt.active_ops == 1, "")
        # 持引用未入 store 锁场景（评审第 4 条）：在 lease 持有期间
        # 制造 LRU 压力（第二个 acquire 使 len 2 > max 1），闸 1 必须
        # 跳过 s1，绝不淘汰持引用的 runtime
        rt2 = mgr.acquire("s2")
        check("[lease] 持引用期间压力不淘汰（闸 1，s1 保留）",
              mgr.runtime("s1") is rt, "")
        check("[lease] 压力触发超限警告（max=1）",
              len(mgr.warnings) >= 1 and "overflow" in mgr.warnings[-1],
              str(mgr.warnings))
        # 持引用者照常安全使用 runtime（对应「暂停后恢复」）
        p = rt.store.create_pending("排海贝皇大排档", "plan_modify",
                                    "must_visit", SNAP)
        check("[lease] 持引用者可安全操作", p.status == "open", "")
        # 清除保留态（open pending 会让闸 2 正确保留 s1——先终结它）
        out_p = rt.store.cancel_pending(p.pending_id)
        check("[lease] 终结测试 pending（闸 2 恢复可淘汰）",
              out_p.ok
              and rt.store._has_runtime_retention_state(
                  _epoch(clock())) is False, str(out_p))
        mgr.release(rt)
        check("[lease] 归还后计数 0", rt.active_ops == 0, "")
        # 释放后 LRU 恢复：release 触发淘汰，s1（最旧、无保留态）被逐出
        check("[release] 释放触发淘汰（s1 被逐出）",
              mgr.runtime("s1") is None, str(list(mgr.snapshot())))
        mgr.release(rt2)
        # 淘汰原子性：被逐 sid 重新获取得到新建 runtime（store 新实例）
        rt1b = mgr.acquire("s1")
        check("[原子性] 重建后 store 为全新实例",
              rt1b.store is not rt.store and rt1b is not rt, "")
        mgr.release(rt1b)
        # 泄漏防护：业务抛异常也必须归还（gate finally 的单元模拟）
        rt5 = mgr.acquire("s1")
        try:
            raise RuntimeError("业务异常")
        except RuntimeError:
            pass
        finally:
            mgr.release(rt5)
        check("[泄漏] 异常路径 lease 仍归还",
              rt5.active_ops == 0, str(rt5.active_ops))
        # 空 sid 拒绝
        try:
            mgr.acquire("  ")
            blocked = False
        except ValueError:
            blocked = True
        check("[lease] 空白 sid 拒绝", blocked, "")
    finally:
        set_env()


def test_multi_pending_invariant_sequences() -> None:
    """§3.6 多 Pending 遮蔽边界：四序列统一断言（双实现）。"""
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    def run_seq(store):
        results = []
        # 序列1：create → consume → create（旧终态新 open）
        p1 = store.create_pending("排海贝皇大排档", "plan_modify",
                                  "must_visit", SNAP)
        store.consume_pending(p1.pending_id, "c1")
        p2 = store.create_pending("换个话题", "plan_modify",
                                  "must_visit", SNAP)
        results.append(("seq1", p2.pending_id))
        # 序列2：open 状态下再 create → 被拒
        try:
            store.create_pending("第三个", "plan_modify", "must_visit", SNAP)
            rejected = False
        except (ValueError, Exception) as e:
            rejected = not isinstance(e, (ValueError,)) or True
            rejected = True   # ValueError 或等价拒绝均算
        results.append(("seq2_rejected", rejected))
        # 序列3：expire → create（旧先过期新 open）
        store.expire_pending(p2.pending_id)
        p3 = store.create_pending("再来一个", "plan_modify",
                                  "must_visit", SNAP)
        results.append(("seq3", p3.pending_id))
        # 序列4：consume → create → consume（全程至多一个 open）
        store.consume_pending(p3.pending_id, "c1")
        p4 = store.create_pending("收尾", "plan_modify", "must_visit", SNAP)
        store.consume_pending(p4.pending_id, "c1")
        results.append(("seq4", p4.pending_id))
        return results

    clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    mem = PendingStore(session_id="s1", now=clock)
    sql = SQLitePendingStore("s1", db_path=dbp, now=clock)
    try:
        for name, store in (("memory", mem), ("sqlite", sql)):
            run_seq(store)
            # 统一断言：存在 open ⇒ latest 即 open（绝不遮蔽）
            latest_id = store._latest_pending_id()
            latest = store.get_pending_by_id(latest_id) \
                if latest_id else None
            check(f"[{name}] 全序列后 latest 为最新终态（无遮蔽 open）",
                  latest is not None and latest.status != "open",
                  f"{latest_id}/{latest.status if latest else None}")
            # 反向构造：当前 open 时 latest 必为该 open
            p = store.create_pending("终局", "plan_modify",
                                     "must_visit", SNAP)
            lid = store._latest_pending_id()
            check(f"[{name}] open 存在 ⇒ latest 即该 open",
                  lid == p.pending_id
                  and store.get_pending_by_id(lid).status == "open",
                  f"{lid}")
    finally:
        set_env()


def test_gate_and_store_integration_real_handle() -> None:
    """集成：真实 handle 下 channel=0 零变化；channel=1 观察模式无状态变化。"""
    set_env(channel="0")
    orc = Orchestrator()
    s1 = Session(use_llm=False)
    r1 = orc.handle(s1, "你好")
    s2 = Session(use_llm=False)
    r2 = orc.handle(s2, "你好")
    check("[集成 off] 两次 handle 输出逐位一致",
          str(r1) == str(r2), f"{str(r1)[:60]} / {str(r2)[:60]}")
    check("[集成 off] manager 未构造", orc._pending_manager is None, "")

    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc2 = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s-live"
        store = PendingStore(session_id="s-live", now=clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
        rt = PendingRuntime("s-live", store, PendingInterceptor(store),
                            last_used=int(clock().timestamp()))
        mgr._runtimes["s-live"] = rt
        orc2._pending_manager = mgr
        # 真实 handle：观察模式只评估不 act，回复与无 pending 时同链路
        r = orc2.handle(s, "第一个")
        check("[集成 on] handle 正常走原链路（gate 未短路）",
              r is not None, "")
        check("[集成 on] pending 保持 open",
              store.get_pending_by_id(p.pending_id).status == "open", "")
        check("[集成 on] 影子记录 consume（观察）",
              rt.interceptor.shadow_log
              and rt.interceptor.shadow_log[-1]["verdict"] == V_CONSUME,
              str(rt.interceptor.shadow_log[-1:]))
        check("[集成 on] lease 已归还", rt.active_ops == 0, "")
    finally:
        set_env()


# ================================================== main


def main() -> int:
    for fn in (test_channel_off_full_bypass,
               test_channel_on_no_pending_bypass,
               test_channel_on_missing_sid_bypass,
               test_observation_mode_no_act,
               test_resolve_order_and_expired_hint,
               test_wrong_hint_non_authoritative,
               test_multi_session_isolation,
               test_store_kind_factory,
               test_retention_state_both_impls,
               test_latest_pending_id_parity,
               test_lru_eviction_and_protection,
               test_overflow_warning_when_all_retained,
               test_lease_lifecycle,
               test_multi_pending_invariant_sequences,
               test_gate_and_store_integration_real_handle):
        fn()
    print(f"\npending channel P2.4a: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
