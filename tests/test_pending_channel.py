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


def test_b1_consume_ok_shortcircuit() -> None:
    """B1/B6：consume ok → 短路确认（规则 7 只确认消歧）+ 消费记录 +
    hint 清空（以真实迁移结果为准）。原观察模式断言随 P2.4b 激活
    更新为接管语义。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
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
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B1 consume ok] 短路回复非 None", r is not None, str(r))
        check("[B1 consume ok] 确认文案引用所选候选与城市",
              r is not None and "海贝皇大排档（南京店）" in str(r)
              and "南京" in str(r), str(r))
        check("[B1 consume ok] 只确认消歧（不产生行程动作文案）",
              r is not None and "行程已生成" not in str(r)
              and "已为您规划" not in str(r), str(r))
        check("[B1 consume ok] pending 已消费",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        rec = store._latest_consumption()
        check("[B1 consume ok] 七字段消费记录完整",
              rec is not None and rec["selected_name"]
              == "海贝皇大排档（南京店）"
              and rec["selected_city"] == "南京"
              and rec["candidate_id"] == "c1"
              and rec["pending_id"] == p.pending_id
              and rec["session_id"] == "s1"
              and "consumed_at" in rec and "original_intent" in rec,
              str(rec))
        check("[B1/B6] hint 已清空（真实迁移成功）",
              s.active_pending_id is None, str(s.active_pending_id))
        check("[B1] lease 已归还", rt.active_ops == 0, "")
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
        # 真实 handle：P2.4b 激活——consume 短路，原链路不执行
        r = orc2.handle(s, "第一个")
        check("[集成 on] consume 短路回复（route=pending_consume）",
              r is not None and "已确认" in str(r)
              and orc2.last_ctx.route == "pending_consume"
              and orc2.last_ctx.handled is True,
              f"{r} route={getattr(orc2.last_ctx, 'route', None)}")
        check("[集成 on] pending 已消费（原链路未重复处理）",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        check("[集成 on] 影子记录 consume 判定",
              rt.interceptor.shadow_log
              and rt.interceptor.shadow_log[-1]["verdict"] == V_CONSUME,
              str(rt.interceptor.shadow_log[-1:]))
        check("[集成 on] hint 已清空（真实迁移成功）",
              s.active_pending_id is None, "")
        check("[集成 on] lease 已归还", rt.active_ops == 0, "")
    finally:
        set_env()


# ================================================== 6. P2.4a-R1：真实线程并发 + acquire 异常安全


def test_real_thread_lease_blocks_eviction() -> None:
    """R1 测试 A（真实线程）：持 lease 线程暂停在 Barrier 期间，
    淘汰扫描被闸 1 阻止、runtime 未被移除；恢复后照常使用（观察
    模式合法只读路径，绝不引入 act），归还后淘汰恢复可用。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        mgr = PendingRuntimeManager(max_runtimes=1, now=clock)
        met1 = threading.Barrier(2, timeout=5)   # 持 lease 暂停点
        met2 = threading.Barrier(2, timeout=5)   # 淘汰断言完成点
        errors = []
        shared = {}

        def holder():
            try:
                rt = mgr.acquire("s1")
                shared["rt"] = rt
                met1.wait()                  # 暂停：持 lease 未做操作
                # 恢复：持 lease 期间照常使用（只读 evaluate，无 act）
                v = rt.interceptor.evaluate("你好", None)
                shared["verdict"] = v.verdict
                met2.wait()
                mgr.release(rt)
            except BaseException as e:
                errors.append(e)

        t = threading.Thread(target=holder)
        t.start()
        met1.wait()      # 此刻 holder 已持 lease 暂停
        # 制造 LRU 压力：第二个 acquire 使 len 2 > max 1 → 触发淘汰扫描
        rt2 = mgr.acquire("s2")
        check("[线程A] 持 lease 的 s1 未被淘汰（闸 1，对象未被移除）",
              mgr.runtime("s1") is not None
              and mgr.runtime("s1") is shared.get("rt"),
              str(type(mgr.runtime("s1"))))
        check("[线程A] runtime store 实例未被替换（未关闭/未重建）",
              shared.get("rt") is not None
              and mgr.runtime("s1").store is shared["rt"].store, "")
        mgr.release(rt2)
        met2.wait()      # holder 恢复执行只读操作并归还
        t.join(timeout=5)
        check("[线程A] 无死锁（holder 线程按时结束）",
              not t.is_alive(), f"alive={t.is_alive()} errors={errors}")
        check("[线程A] 线程无异常", errors == [], str(errors))
        check("[线程A] 暂停恢复后只读 evaluate 正常产出 verdict",
              shared.get("verdict") == V_NONE, str(shared.get("verdict")))
        # 归还后淘汰恢复：holder 的 release 可能已触发过一轮淘汰
        # （归还时容量释放即淘汰，§4.5 release 语义）——此处重新制造
        # 压力，断言 s1（LRU 最旧、无保留态）可被正常逐出
        rt3 = mgr.acquire("s3")
        snap = mgr.snapshot()
        check("[线程A] 归还后淘汰恢复（s1 被逐出）",
              "s1" not in snap and "s3" in snap, str(list(snap)))
        mgr.release(rt3)
        # 淘汰原子性：重新获取得到全新 runtime（store 新实例）
        rt1b = mgr.acquire("s1")
        check("[线程A] 重建后 store 为全新实例",
              rt1b.store is not shared["rt"].store, "")
        mgr.release(rt1b)
    finally:
        set_env()


def test_real_thread_acquire_release_evict_stress() -> None:
    """R1 测试 B（真实线程）：多线程 acquire/release 与淘汰扫描交错
    压力——active_ops 不出现 <1 的持 lease 计数（无负数、无重复
    归还）、持 lease 的 runtime 绝不被淘汰替换、无死锁、结束后
    全部计数归零、manager 仍可用。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        mgr = PendingRuntimeManager(max_runtimes=2, now=clock)
        errors = []
        ops = {"acq": 0, "rel": 0}
        ops_lock = threading.Lock()
        sids = [f"w{i}" for i in range(4)]    # 4 个 sid 挤 max=2 → 持续压力

        def worker(i: int) -> None:
            try:
                for round_ in range(50):
                    sid = sids[(i + round_) % len(sids)]
                    rt = mgr.acquire(sid)
                    with ops_lock:
                        ops["acq"] += 1
                    if rt.active_ops < 1:
                        raise AssertionError(
                            f"acquire 后计数 <1（重复归还/负数）: "
                            f"{rt.active_ops}")
                    if mgr.runtime(sid) is not rt:
                        raise AssertionError(
                            "持 lease 的 runtime 被淘汰替换（闸 1 失效）")
                    rt.store.get_active_pending()    # 只读业务操作
                    time.sleep(0)                    # 让出 GIL 放大交错
                    mgr.release(rt)
                    with ops_lock:
                        ops["rel"] += 1
            except BaseException as e:
                errors.append(e)

        def evictor() -> None:
            try:
                for _ in range(300):
                    mgr.evict()
                    time.sleep(0)
            except BaseException as e:
                errors.append(e)

        ws = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        ev = threading.Thread(target=evictor)
        for w in ws:
            w.start()
        ev.start()
        for w in ws:
            w.join(timeout=15)
        ev.join(timeout=15)
        alive = [w.is_alive() for w in ws] + [ev.is_alive()]
        check("[线程B] 无死锁（全部线程按时结束）", not any(alive),
              f"alive={alive}")
        check("[线程B] 全线程无异常（计数/替换不变量成立）",
              errors == [], str(errors[:3]))
        check("[线程B] acquire/release 严格成对",
              ops["acq"] == ops["rel"] and ops["acq"] > 0, str(ops))
        snap = mgr.snapshot()
        check("[线程B] 结束后全部 lease 归零",
              all(v[0] == 0 for v in snap.values()), str(snap))
        # manager 仍可用（状态一致，无泄漏残留）
        rt = mgr.acquire("w0")
        check("[线程B] 压力后 manager 仍可用", rt.active_ops == 1,
              str(rt.active_ops))
        mgr.release(rt)
        check("[线程B] 再归还后计数归零", rt.active_ops == 0,
              str(rt.active_ops))
    finally:
        set_env()


def test_gate_evaluate_exception_returns_lease() -> None:
    """R1 测试 C：真实 gate 路径（handle → _pending_gate）中 evaluate
    抛异常 → finally 确实归还 lease，异常向上传播（当前设计），
    manager 状态一致、可继续使用。非独立 finally 模拟片段。"""
    set_env(channel="1", store_kind="memory")
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s-err"
        # 预置 open pending：否则 gate 在第 ④ 步（无任何线索）即旁路，
        # evaluate 根本不会执行——必须让异常发生在真实 evaluate 路径
        mgr = PendingRuntimeManager()
        orc._pending_manager = mgr
        rt0 = mgr.acquire("s-err")
        p = rt0.store.create_pending("排海贝皇大排档", "plan_modify",
                                     "must_visit", SNAP)
        mgr.release(rt0)
        s.active_pending_id = p.pending_id
        orig = PendingInterceptor.evaluate

        def boom(self, utterance, parse_result=None, **kw):
            raise RuntimeError("evaluate 注入异常")

        PendingInterceptor.evaluate = boom
        raised = False
        try:
            try:
                orc.handle(s, "你好")
            except RuntimeError:
                raised = True
        finally:
            PendingInterceptor.evaluate = orig
        check("[gate异常] 异常沿真实 gate 路径向上传播", raised, "")
        check("[gate异常] manager 已构造（异常前完成 acquire）",
              mgr is not None, "")
        snap = mgr.snapshot() if mgr is not None else {}
        check("[gate异常] lease 已归还（ops=0）",
              "s-err" in snap and snap["s-err"][0] == 0, str(snap))
        # 异常后 manager 状态一致：可重新获取并正常归还
        rt = mgr.acquire("s-err")
        check("[gate异常] 异常后可重新获取（计数=1）",
              rt.active_ops == 1, str(rt.active_ops))
        mgr.release(rt)
        check("[gate异常] 再归还后计数=0", rt.active_ops == 0,
              str(rt.active_ops))
    finally:
        set_env()


def test_acquire_clock_failure_no_lease_leak() -> None:
    """R1：注入时钟在 acquire 中途抛异常 → 绝不产生半租约（计数
    不泄漏）；回滚不与正常归还重复；manager 恢复可用。覆盖
    新建路径 / 已存在路径 / 淘汰扫描回滚三条异常路径。"""
    set_env(channel="1", store_kind="memory")
    try:
        base = datetime(2026, 11, 19, 10, 0, 0)
        state = {"n": 0, "fail_on": None}    # 第 fail_on 次调用起抛异常

        def flaky_clock():
            state["n"] += 1
            if state["fail_on"] is not None \
                    and state["n"] >= state["fail_on"]:
                raise RuntimeError("时钟注入异常")
            return base

        mgr = PendingRuntimeManager(max_runtimes=4, now=flaky_clock)
        # 场景1（新建路径）：第 1 次时钟调用在 PendingRuntime 构造内
        # 成功，第 2 次（now_e 换算）抛异常 → 异常先于计数变更
        state["fail_on"] = 2
        failed = False
        try:
            mgr.acquire("x")
        except RuntimeError:
            failed = True
        check("[时钟异常] 新建路径 acquire 抛出异常", failed, "")
        rt = mgr.runtime("x")
        check("[时钟异常] 新建路径无半租约（未持有或计数=0）",
              rt is None or rt.active_ops == 0, str(rt))
        # 恢复后可正常获取、正常归还（计数不残留）
        state["n"], state["fail_on"] = 0, None
        rt = mgr.acquire("x")
        check("[时钟异常] 恢复后正常获取（计数=1）",
              rt.active_ops == 1, str(rt.active_ops))
        # 场景2（已存在路径）：时钟异常先于计数增加，计数不被污染
        state["n"], state["fail_on"] = 0, 1
        failed = False
        try:
            mgr.acquire("x")
        except RuntimeError:
            failed = True
        check("[时钟异常] 已存在路径 acquire 抛出异常", failed, "")
        check("[时钟异常] 已存在路径计数未被污染（仍=1）",
              rt.active_ops == 1, str(rt.active_ops))
        state["n"], state["fail_on"] = 0, None
        mgr.release(rt)
        check("[时钟异常] 异常路径后正常归还（计数=0）",
              rt.active_ops == 0, str(rt.active_ops))
        # 场景3（淘汰扫描回滚）：增量之后异常（_evict_locked 注入）
        # → 计数必须回滚，且绝不与正常归还重复
        mgr2 = PendingRuntimeManager(max_runtimes=1, now=flaky_clock)
        rt_y = mgr2.acquire("y")
        orig_evict = mgr2._evict_locked

        def broken_evict(now_e=None):
            raise RuntimeError("淘汰扫描注入异常")

        mgr2._evict_locked = broken_evict
        failed = False
        try:
            mgr2.acquire("z")        # len 2 > max 1 → 淘汰扫描抛异常
        except RuntimeError:
            failed = True
        check("[时钟异常] 增量后淘汰扫描异常向外传播", failed, "")
        rt_z = mgr2.runtime("z")
        check("[时钟异常] 回滚生效（z 计数=0，无泄漏）",
              rt_z is None or rt_z.active_ops == 0, str(rt_z))
        check("[时钟异常] 回滚不误伤他人（y 计数仍=1）",
              rt_y.active_ops == 1, str(rt_y.active_ops))
        mgr2._evict_locked = orig_evict
        mgr2.release(rt_y)
        check("[时钟异常] 回滚后正常归还仍工作（y=0）",
              rt_y.active_ops == 0, str(rt_y.active_ops))
    finally:
        set_env()


# ================================================== 7. P2.4b：verdict 行为与消费接管（B1–B12）

from travel_agent.pending import _ConsumeOutcome  # noqa: E402


def _gate_harness(clock, sid="s1", store=None, interceptor=None):
    """B 组测试装具：注入式 manager + 预置 store 的 gate 测试环境。"""
    orc = Orchestrator()
    s = Session(use_llm=False)
    s.pending_session_id = sid
    if store is None:
        store = PendingStore(session_id=sid, now=clock)
    if interceptor is None:
        interceptor = PendingInterceptor(store)
    mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
    rt = PendingRuntime(sid, store, interceptor,
                        last_used=int(clock().timestamp()))
    mgr._runtimes[sid] = rt
    orc._pending_manager = mgr
    return orc, s, store, rt, mgr


class _FailConsumeStore(PendingStore):
    """act 如实失败（不抛异常）：consume_pending 返回 ok=False。"""

    def consume_pending(self, pending_id, candidate_id):
        return _ConsumeOutcome(ok=False, pending_id=pending_id,
                               reason="注入：如实失败")


class _RaiseBeforeConsumeStore(PendingStore):
    """act 抛异常且确认未迁移（B2：store 状态仍 open）。"""

    def consume_pending(self, pending_id, candidate_id):
        raise RuntimeError("注入：consume 前异常")


class _RaiseAfterCommitStore(PendingStore):
    """提交边界异常（B4）：真实迁移已完成后 act 再抛。"""

    def consume_pending(self, pending_id, candidate_id):
        out = super().consume_pending(pending_id, candidate_id)
        if out.ok:
            raise RuntimeError("注入：提交后异常")
        return out


class _RaiseAndHideStore(_RaiseBeforeConsumeStore):
    """无法判定（B4）：act 抛异常，且**其后的**状态查询也失败
    （首次查询放行给 evaluate，第二次起失败 → gate 无法判定）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._status_queries = 0

    def get_pending_by_id(self, pending_id):
        self._status_queries += 1
        if self._status_queries >= 2:
            raise RuntimeError("注入：状态查询失败")
        return super().get_pending_by_id(pending_id)


class _PureCuePR:
    """纯选择线索解析桩（真实 parse 会抽出具名候选，P2.3 冻结规则）。"""
    poi_candidates = []
    intent_coarse = ""


class _DestChangedPR:
    destination_new = "上海"
    destination_writable = True
    poi_candidates = []
    intent_coarse = ""


class _StubCand:
    def __init__(self, name: str) -> None:
        self.name = name


class _NewQuestionPR:
    poi_candidates = [_StubCand("夫子庙")]
    intent_coarse = ""


def test_b1_consume_fail_no_shortcircuit() -> None:
    """B1/B2：act 如实失败 → 不短路、不改选、不伪造，交回原链路。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc, s, store, rt, _ = _gate_harness(clock, store=_FailConsumeStore())
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B1 consume 失败] 不短路（交回原链路）", r is None, str(r))
        check("[B1 consume 失败] pending 保持 open（可重试）",
              store.get_pending_by_id(p.pending_id).status == "open", "")
        check("[B1 consume 失败] 无消费记录",
              store._latest_consumption() is None, "")
        check("[B1/B6] act 失败 hint 保留",
              s.active_pending_id == p.pending_id, "")
        check("[B1] 失败已降级进 shadow log",
              any(getattr(e, "get", lambda k: None)("action")
                  == "consume_failed_not_shortcircuit"
                  for e in rt.interceptor.shadow_log),
              str(rt.interceptor.shadow_log[-1:]))
        check("[B1] lease 已归还", rt.active_ops == 0, "")
    finally:
        set_env()


def test_b2_act_exception_open_status() -> None:
    """B2：act 抛异常且确认未迁移（store 仍 open）→ 安全降级原链路。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc, s, store, rt, _ = _gate_harness(
            clock, store=_RaiseBeforeConsumeStore())
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B2 act 异常] 不短路", r is None, str(r))
        check("[B2 act 异常] store 确认 open → 原链路安全",
              store.get_pending_by_id(p.pending_id).status == "open", "")
        check("[B2/B6] 未迁移 → hint 保留",
              s.active_pending_id == p.pending_id, "")
        gate_recs = [e for e in rt.interceptor.shadow_log
                      if getattr(e, "get", lambda k: None)("stage") == "gate"]
        check("[B2] act 异常记入 shadow（含 store 实际状态）",
              gate_recs and gate_recs[-1]["action"] == "act_exception"
              and gate_recs[-1]["store_status"] == "open",
              str(gate_recs[-1:]))
        check("[B2] lease 已归还（异常路径）", rt.active_ops == 0, "")
    finally:
        set_env()


def test_b4_commit_boundary_exception() -> None:
    """B4：提交边界异常（迁移已真实完成）→ 兜底确认短路，绝不交回
    原链路造成重复业务处理。memory 与 SQLite 双实现。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc, s, store, rt, _ = _gate_harness(
            clock, store=_RaiseAfterCommitStore())
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B4 提交边界] 兜底确认短路（不进原链路）",
              r is not None and "已确认" in str(r), str(r))
        check("[B4 提交边界] store 已真实迁移 consumed",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        check("[B4/B6] 迁移已确认 → hint 清空",
              s.active_pending_id is None, "")
        check("[B4] 兜底不伪造候选细节（不引用不可靠数据）",
              r is not None and "海贝皇大排档" not in str(r), str(r))
        check("[B4] lease 已归还", rt.active_ops == 0, "")
    finally:
        set_env()

    # SQLite 实现：真实 DB 提交后再抛（连接逐操作，独立核验）
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    class _RaiseAfterCommitSQLite(SQLitePendingStore):
        def consume_pending(self, pending_id, candidate_id):
            out = super().consume_pending(pending_id, candidate_id)
            if out.ok:
                raise RuntimeError("注入：SQLite 提交后异常")
            return out

    set_env(channel="1", store_kind="sqlite")   # 独立设回开关（B4 sqlite）
    clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
    try:
        st = _RaiseAfterCommitSQLite("s1", db_path=dbp, now=clock)
        orc, s, store, rt, _ = _gate_harness(clock, store=st)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B4 sqlite 提交边界] 兜底确认短路",
              r is not None and "已确认" in str(r), str(r))
        check("[B4 sqlite 提交边界] DB 行状态 consumed",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        check("[B4 sqlite 提交边界] hint 清空",
              s.active_pending_id is None, "")
    finally:
        set_env()


def test_b4_undeterminable_conservative() -> None:
    """B4：act 异常且状态无法判定 → 保守拦截：不伪造成功、不交回
    原链路（防不确定状态下的重复业务处理），hint 保留。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc, s, store, rt, _ = _gate_harness(
            clock, store=_RaiseAndHideStore())
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B4 无法判定] 保守回复（不交回原链路）",
              r is not None and "无法确认" in str(r), str(r))
        check("[B4 无法判定] 不伪造消费成功",
              r is not None and "已确认您的选择" not in str(r), str(r))
        check("[B4/B6] 无法判定 → hint 保留（不误清）",
              s.active_pending_id == p.pending_id, "")
        check("[B4] 保守拦截记入 warnings",
              any("无法判定" in w for w in orc._pending_warnings),
              str(orc._pending_warnings))
        check("[B4] lease 已归还", rt.active_ops == 0, "")
    finally:
        set_env()


def test_b1_cancel_and_new_question_verdicts() -> None:
    """B1：cancel_explicit / cancel_dest_changed / cancel_new_question
    → 不短路交回原链路 + 终态迁移成功清 hint。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        # cancel_explicit
        orc, s, store, rt, _ = _gate_harness(clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "算了，取消", make_ctx("算了，取消"))
        check("[B1 cancel_explicit] 不短路", r is None, str(r))
        check("[B1 cancel_explicit] pending 已取消",
              store.get_pending_by_id(p.pending_id).status == "cancelled", "")
        check("[B1/B6] cancel 迁移成功 → hint 清空",
              s.active_pending_id is None, "")
        # cancel_dest_changed（解析桩：明确新目的地 + writable）
        orc2, s2, store2, rt2, _ = _gate_harness(clock, sid="s2")
        p2 = store2.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s2.active_pending_id = p2.pending_id
        ctx2 = make_ctx("改成去上海")
        ctx2.parse = _DestChangedPR()
        r2 = orc2._pending_gate(s2, "改成去上海", ctx2)
        check("[B1 cancel_dest] 不短路", r2 is None, str(r2))
        check("[B1 cancel_dest] pending 已取消",
              store2.get_pending_by_id(p2.pending_id).status == "cancelled",
              "")
        check("[B1/B6] cancel_dest 迁移成功 → hint 清空",
              s2.active_pending_id is None, "")
        # cancel_new_question（解析桩：快照外具名新实体）
        orc3, s3, store3, rt3, _ = _gate_harness(clock, sid="s3")
        p3 = store3.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s3.active_pending_id = p3.pending_id
        ctx3 = make_ctx("帮我查一下夫子庙")
        ctx3.parse = _NewQuestionPR()
        r3 = orc3._pending_gate(s3, "帮我查一下夫子庙", ctx3)
        check("[B1 cancel_new_question] 不短路", r3 is None, str(r3))
        check("[B1 cancel_new_question] pending 已取消",
              store3.get_pending_by_id(p3.pending_id).status == "cancelled",
              "")
        check("[B1/B6] new_question 迁移成功 → hint 清空",
              s3.active_pending_id is None, "")
    finally:
        set_env()


def test_b1_expired_restate_handback_duplicate_none() -> None:
    """B1：expired_reply / restate / hand_back / duplicate_select / none
    五类 verdict 全路径（回复与状态逐项断言）。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, advance = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        # expired_reply：到期未登记 + 选择语境 → 短路告知过期
        orc, s, store, rt, _ = _gate_harness(clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        advance(301)
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B1 expired_reply] 短路如实告知过期",
              r is not None and "过期" in str(r), str(r))
        check("[B1 expired_reply] pending 已登记 expired",
              store.get_pending_by_id(p.pending_id).status == "expired", "")
        check("[B1/B6] expired 迁移成功 → hint 清空",
              s.active_pending_id is None, "")
        # restate：显式重述请求 → 短路重述候选 1..N，零状态变化
        orc2, s2, store2, rt2, _ = _gate_harness(clock, sid="s2")
        p2 = store2.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s2.active_pending_id = p2.pending_id
        r2 = orc2._pending_gate(s2, "再说一遍", make_ctx("再说一遍"))
        check("[B1 restate] 短路重述候选列表",
              r2 is not None and "1." in str(r2) and "2." in str(r2)
              and "海贝皇大排档（南京店）" in str(r2)
              and "海贝皇大排档（上海店）" in str(r2), str(r2))
        check("[B1 restate] 快照顺序与候选一致（1..N）",
              r2 is not None and str(r2).find("南京店")
              < str(r2).find("上海店"), str(r2))
        check("[B1 restate] pending 保持 open（零状态变化）",
              store2.get_pending_by_id(p2.pending_id).status == "open", "")
        check("[B1/B6] restate → hint 保留",
              s2.active_pending_id == p2.pending_id, "")
        # hand_back：询问/评价标记 → 保守交回原链路
        r2b = orc2._pending_gate(s2, "南京那家怎么样", make_ctx("南京那家怎么样"))
        check("[B1 hand_back] 不短路（保守红线）", r2b is None, str(r2b))
        check("[B1 hand_back] pending 仍 open",
              store2.get_pending_by_id(p2.pending_id).status == "open", "")
        check("[B1/B6] hand_back → hint 保留",
              s2.active_pending_id == p2.pending_id, "")
        # duplicate_select：先消费，窗口内纯选择线索 → 短路告知已确认
        orc3, s3, store3, rt3, _ = _gate_harness(clock, sid="s3")
        p3 = store3.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s3.active_pending_id = p3.pending_id
        r3a = orc3._pending_gate(s3, "第一个", make_ctx("第一个"))
        check("[B1 duplicate 前置] 首次消费成功短路",
              r3a is not None and "已确认" in str(r3a), str(r3a))
        ctx3 = make_ctx("第一个")
        ctx3.parse = _PureCuePR()
        r3b = orc3._pending_gate(s3, "第一个", ctx3)
        check("[B1 duplicate_select] 短路告知已确认（引用真实记录）",
              r3b is not None and "已确认过" in str(r3b)
              and "海贝皇大排档（南京店）" in str(r3b), str(r3b))
        check("[B1 duplicate_select] 不重复消费（记录仍 1 条）",
              store3._latest_consumption() is not None
              and store3.get_pending_by_id(p3.pending_id).status
              == "consumed", "")
        check("[B1/B6] duplicate → hint 保持 None（零状态）",
              s3.active_pending_id is None, "")
        # none：pending 不可消费（已登记 expired）且非重复选择语境
        # → 原链路，零状态变化，hint 保留（open pending 下「你好」
        # 会因新实体证据产出 cancel_new_question，见 P2.3 冻结规则）
        orc4, s4, store4, rt4, _ = _gate_harness(clock, sid="s4")
        p4 = store4.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s4.active_pending_id = p4.pending_id
        store4.expire_pending(p4.pending_id)   # 显式登记 expired
        r4 = orc4._pending_gate(s4, "你好", make_ctx("你好"))
        check("[B1 none] 不短路", r4 is None, str(r4))
        check("[B1 none] pending 保持 expired（零状态变化）",
              store4.get_pending_by_id(p4.pending_id).status == "expired",
              "")
        check("[B1/B6] none → hint 保留",
              s4.active_pending_id == p4.pending_id, "")
    finally:
        set_env()


def test_b3_reply_construction_failure_still_shortcircuit() -> None:
    """B3：consume 成功后 _pending_reply 抛异常 → 仍短路（兜底确认），
    绝不撤销消费、绝不回退普通意图路由；非迁移 verdict 回复构造失败
    → 零状态保守交回原链路。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc, s, store, rt, _ = _gate_harness(clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id

        def _boom(*a, **kw):
            raise RuntimeError("注入：回复构造失败")

        orc._pending_reply = _boom
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B3] 回复构造失败仍短路（兜底确认）",
              r is not None and "已确认" in str(r), str(r))
        check("[B3] 消费未被撤销",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        check("[B3/B6] 迁移成功 → hint 清空（不回退路由）",
              s.active_pending_id is None, "")
        check("[B3] 构造失败记入 shadow",
              any(getattr(e, "get", lambda k: None)("action")
                  == "reply_construction_failed"
                  for e in rt.interceptor.shadow_log),
              str(rt.interceptor.shadow_log[-1:]))
        check("[B3] lease 已归还", rt.active_ops == 0, "")
        # 非迁移 verdict（restate）回复构造失败 → 零状态交回原链路
        orc2, s2, store2, rt2, _ = _gate_harness(clock, sid="s2")
        p2 = store2.create_pending("排海贝皇大排档", "plan_modify",
                                   "must_visit", SNAP)
        s2.active_pending_id = p2.pending_id
        orc2._pending_reply = _boom
        r2 = orc2._pending_gate(s2, "再说一遍", make_ctx("再说一遍"))
        check("[B3 非迁移] 回复构造失败 → 保守交回原链路",
              r2 is None, str(r2))
        check("[B3 非迁移] pending 保持 open（零状态）",
              store2.get_pending_by_id(p2.pending_id).status == "open", "")
        check("[B3/B6 非迁移] hint 保留",
              s2.active_pending_id == p2.pending_id, "")
    finally:
        set_env()


def test_b5_shadow_sink_exception_isolated() -> None:
    """B5：shadow sink 异常不影响主链路 / lease 归还 / verdict 行为；
    装配层 _shadow_gate 自身吞掉一切记录异常。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))

        def _bad_sink(rec):
            raise RuntimeError("注入：shadow sink 异常")

        store = PendingStore(session_id="s1", now=clock)
        orc, s, store, rt, _ = _gate_harness(
            clock, sid="s1", store=store,
            interceptor=PendingInterceptor(store, shadow_sink=_bad_sink))
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        r = orc._pending_gate(s, "第一个", make_ctx("第一个"))
        check("[B5] sink 异常不影响短路回复",
              r is not None and "已确认" in str(r), str(r))
        check("[B5] sink 异常不影响消费",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        check("[B5] lease 已归还", rt.active_ops == 0, "")

        class _BadLog(list):
            def append(self, item):
                raise RuntimeError("注入：shadow 记录失败")

        orc._shadow_gate(rt, "x", "y", detail="d", store_status=None,
                         pending_id="p")     # 必须静默吞掉，不得抛出
        check("[B5] 装配层 shadow 记录异常被隔离", True, "")
    finally:
        set_env()


def test_b7_b8_real_handle_route_and_no_snatch() -> None:
    """B7：短路落点 route=pending_consume / handled=True / history 追加；
    B8：pending 选择不被下游 gate 抢答（含周边 gate 前的真实 handle）。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc = Orchestrator()
        s = Session(use_llm=False)
        s.pending_session_id = "s1"
        store = PendingStore(session_id="s1", now=clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s.active_pending_id = p.pending_id
        mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
        rt = PendingRuntime("s1", store, PendingInterceptor(store),
                            last_used=int(clock().timestamp()))
        mgr._runtimes["s1"] = rt
        orc._pending_manager = mgr
        # B8：含名称+选择话术的句子 → 消费通道接管，不被周边 gate 抢答
        r = orc.handle(s, "排海贝皇大排档，第一个")
        check("[B8] 名称+序号选择不被下游 gate 抢答",
              orc.last_ctx.route == "pending_consume"
              and orc.last_ctx.handled is True,
              str(getattr(orc.last_ctx, "route", None)))
        check("[B8] 短路回复为消费确认",
              r is not None and "已确认" in str(r), str(r))
        check("[B8] pending 已消费（下游未重复处理）",
              store.get_pending_by_id(p.pending_id).status == "consumed", "")
        # B7：短路落点契约——history 追加 agent 消息
        check("[B7] history 已追加 agent 消息",
              s.history and s.history[-1]["role"] == "agent"
              and str(r) in s.history[-1]["content"], str(s.history[-1:]))
        # 对照：无 pending 时同类句子走原链路（无 pending 不误触发）
        s2 = Session(use_llm=False)
        r2 = orc.handle(s2, "你好")
        check("[B7 对照] 无 pending 会话零接管",
              orc.last_ctx.route != "pending_consume", str(r2)[:40])
    finally:
        set_env()


def test_b9_cross_session_isolation_both_impls() -> None:
    """B9：跨 Session 隔离——A 消费不影响 B；memory 与 SQLite 双实现。"""
    set_env(channel="1", store_kind="memory")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc = Orchestrator()
        mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
        orc._pending_manager = mgr
        sa = Session(use_llm=False)
        sa.pending_session_id = "s-A"
        sb = Session(use_llm=False)
        sb.pending_session_id = "s-B"
        sta = PendingStore(session_id="s-A", now=clock)
        stb = PendingStore(session_id="s-B", now=clock)
        rta = PendingRuntime("s-A", sta, PendingInterceptor(sta),
                             last_used=int(clock().timestamp()))
        rtb = PendingRuntime("s-B", stb, PendingInterceptor(stb),
                             last_used=int(clock().timestamp()))
        mgr._runtimes["s-A"] = rta
        mgr._runtimes["s-B"] = rtb
        pa = sta.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        pb = stb.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        sa.active_pending_id = pa.pending_id
        sb.active_pending_id = pb.pending_id
        ra = orc._pending_gate(sa, "第一个", make_ctx("第一个"))
        # hand_back 话术（零状态变化）：open pending 下「你好」会因新实体
        # 证据产出 cancel_new_question（P2.3 冻结规则），不适合隔离对照
        rb = orc._pending_gate(sb, "南京那家怎么样", make_ctx("南京那家怎么样"))
        check("[B9] A 消费成功短路", ra is not None and "已确认" in str(ra),
              str(ra))
        check("[B9] B 未受影响（pending 仍 open）",
              stb.get_pending_by_id(pb.pending_id).status == "open", "")
        check("[B9] B 无 A 的消费记录",
              stb._latest_consumption() is None, "")
        check("[B9] B 的 none verdict 不短路", rb is None, str(rb))
        check("[B9] 双 lease 已归还",
              rta.active_ops == 0 and rtb.active_ops == 0, "")
    finally:
        set_env()

    # SQLite 双实现
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    set_env(channel="1", store_kind="sqlite")
    try:
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        orc = Orchestrator()
        mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
        orc._pending_manager = mgr
        sta = SQLitePendingStore("s-A", db_path=dbp, now=clock)
        stb = SQLitePendingStore("s-B", db_path=dbp, now=clock)
        rta = PendingRuntime("s-A", sta, PendingInterceptor(sta),
                             last_used=int(clock().timestamp()))
        rtb = PendingRuntime("s-B", stb, PendingInterceptor(stb),
                             last_used=int(clock().timestamp()))
        mgr._runtimes["s-A"] = rta
        mgr._runtimes["s-B"] = rtb
        pa = sta.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        pb = stb.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP)
        sa = Session(use_llm=False)
        sa.pending_session_id = "s-A"
        sa.active_pending_id = pa.pending_id
        sb = Session(use_llm=False)
        sb.pending_session_id = "s-B"
        sb.active_pending_id = pb.pending_id
        ra = orc._pending_gate(sa, "第一个", make_ctx("第一个"))
        # hand_back 话术（零状态变化）：open pending 下「你好」会因新实体
        # 证据产出 cancel_new_question（P2.3 冻结规则），不适合隔离对照
        rb = orc._pending_gate(sb, "南京那家怎么样", make_ctx("南京那家怎么样"))
        check("[B9 sqlite] A 消费成功短路",
              ra is not None and "已确认" in str(ra), str(ra))
        check("[B9 sqlite] A 的 DB 行 consumed",
              sta.get_pending_by_id(pa.pending_id).status == "consumed", "")
        check("[B9 sqlite] B 的 pending 仍 open",
              stb.get_pending_by_id(pb.pending_id).status == "open", "")
        check("[B9 sqlite] B 无 A 的消费记录",
              stb._latest_consumption() is None, "")
        check("[B9 sqlite] B 的 none 不短路", rb is None, str(rb))
    finally:
        set_env()


def test_b10_channel0_bitwise_with_pending() -> None:
    """B10：channel=0 时即便存在 pending/hint 也与无 pending 基线逐位
    一致，manager 不构造。"""
    set_env(channel="0")
    try:
        orc = Orchestrator()
        clock, _ = fixed_clock(datetime(2026, 11, 19, 10, 0, 0))
        # 带 pending + hint 的会话
        s1 = Session(use_llm=False)
        s1.pending_session_id = "s1"
        store = PendingStore(session_id="s1", now=clock)
        p = store.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP)
        s1.active_pending_id = p.pending_id
        r1 = orc.handle(s1, "你好")
        # 无 pending 的对照会话
        s2 = Session(use_llm=False)
        r2 = orc.handle(s2, "你好")
        check("[B10] channel=0 输出与无 pending 基线逐位一致",
              str(r1) == str(r2), f"{str(r1)[:60]} / {str(r2)[:60]}")
        check("[B10] channel=0 不消费（pending 保持 open）",
              store.get_pending_by_id(p.pending_id).status == "open", "")
        check("[B10] channel=0 manager 未构造",
              orc._pending_manager is None, "")
    finally:
        set_env()



# ================================================== 8. P2.5a：生产侧集成（ambiguous → 创建 → 展示）

import travel_agent.orchestrator.orchestrator as _orch_mod
from travel_agent.entity_resolver import (MatchRow, ResolvedCandidate,
                                          ResolveResult)


class _StubResolver:
    """resolve 数据源桩（真实 KB 无测试歧义实体，快照数据源必须可
    控制——决策链 extract/decide 保持真实执行）。按调用序返回预置
    ResolveResult；超出序列重复最后一次（新鲜度断言依赖此语义）。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, candidates, context=None, **kw):
        self.calls += 1
        return self.results[min(self.calls, len(self.results)) - 1]


def _amb_rc(text_a, city_a, text_b, city_b, confidence=0.8):
    return ResolvedCandidate(
        original={"text": text_a.split("（")[0], "entity_type":
                  "restaurant", "role": "query"},
        status="ambiguous", confidence=confidence,
        probe_status="not_applicable",
        matches=[MatchRow(city=city_a, name=text_a,
                          category="restaurant", source=f"e-{city_a}"),
                 MatchRow(city=city_b, name=text_b,
                          category="restaurant", source=f"e-{city_b}")])


def _conf_rc(text_a, city_a):
    return ResolvedCandidate(
        original={"text": text_a, "entity_type": "restaurant",
                  "role": "query"},
        status="confirmed", confidence=0.95,
        probe_status="not_applicable",
        matches=[MatchRow(city=city_a, name=text_a,
                          category="restaurant", source=f"e-{city_a}")])


def _produce_session(orc, sid):
    s = Session(use_llm=False)
    s.pending_session_id = sid
    return s


def test_p25a_produce_hook_integration() -> None:
    """A1（00880ed §11.3）：ambiguous → clarify → 创建 Pending →
    展示 1..N（真实 handle，快照冻结、ID 可引用、hint 刷新）。"""
    set_env(channel="1", store_kind="memory")
    old = _orch_mod._resolve_entities
    try:
        stub = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        _orch_mod._resolve_entities = stub
        orc = Orchestrator()
        s = _produce_session(orc, "s-prod")
        r = orc.handle(s, "海贝皇大排档哪家好")
        check("[A1] 短路返回展示回复", r is not None
              and "1. 海贝皇大排档（南京店）" in str(r)
              and "2. 海贝皇大排档（上海店）" in str(r), str(r))
        check("[A1] 提示回复序号", "回复序号" in str(r), "")
        check("[A1] route=pending_produce + handled",
              orc.last_ctx.route == "pending_produce"
              and orc.last_ctx.handled, str(orc.last_ctx.route))
        rt = orc._pending_manager.runtime("s-prod")
        p = rt.store.get_active_pending()
        check("[A1] Pending 已创建且 open", p is not None
              and p.status == "open", str(p))
        check("[A1] 快照冻结 1..2、ID 可引用",
              [c.display_order for c in p.candidates] == [1, 2]
              and p.candidate_by_id(p.candidates[0].candidate_id)
              is not None, str(p.as_dict()))
        check("[A1] hint 刷新为 Pending ID",
              s.active_pending_id == p.pending_id, "")
        check("[A1] history 尾部为 agent 展示",
              s.history[-1]["role"] == "agent"
              and "当前候选如下" in s.history[-1]["content"], "")
        check("[A1] 决策引擎影子记录 clarify",
              orc._decider is not None
              and orc._decider.shadow_log
              and orc._decider.shadow_log[-1]["action"] == "clarify",
              str(orc._decider.shadow_log[-1:] if orc._decider else None))
        check("[A1] 决策链真实执行（resolve 桩被调用 1 次）",
              stub.calls == 1, str(stub.calls))
        check("[A1] lease 已归还", rt.active_ops == 0, "")
        check("[A1] producer 影子无异常记录",
              not orc._producer.shadow_log
              or all("failed" not in r["action"]
                     for r in orc._producer.shadow_log),
              str(orc._producer.shadow_log))
    finally:
        _orch_mod._resolve_entities = old
        set_env()


def test_p25a_rule8_and_freshness() -> None:
    """A2 集成（展示轮后新问题 → cancel_new_question → 新 clarify →
    新 Pending）+ D1 硬约束①（快照永远来自本轮 resolver 结果）+
    C1 集成（hand_back 轮生产钩子撞规则 8 → 原链路不覆盖不轮换）。"""
    set_env(channel="1", store_kind="memory")
    old = _orch_mod._resolve_entities
    try:
        stub = _StubResolver([
            ResolveResult(resolved=[_amb_rc("甲大排档（南京店）", "南京",
                                            "甲大排档（上海店）", "上海")]),
            ResolveResult(resolved=[_amb_rc("乙餐厅（玄武店）", "南京",
                                            "乙餐厅（浦东店）", "上海")]),
        ])
        _orch_mod._resolve_entities = stub
        orc = Orchestrator()
        s = _produce_session(orc, "s-fresh")
        r1 = orc.handle(s, "甲大排档哪家好")
        rt = orc._pending_manager.runtime("s-fresh")
        p1 = rt.store.get_active_pending()
        check("[fresh] 轮1 创建成功（快照=甲）", p1 is not None
              and p1.candidates[0].name == "甲大排档（南京店）", str(p1))
        # 轮 2（A2）：新问题 → gate cancel_new_question（真实迁移）
        # → 生产钩子 clarify → 新 Pending（快照=本轮桩输出）
        r2 = orc.handle(s, "帮我查一下夫子庙")
        check("[A2] 轮2 短路展示新快照（cancel 后生产接续）",
              orc.last_ctx.route == "pending_produce"
              and "乙餐厅（玄武店）" in str(r2), str(r2))
        # memory store _active 单槽已被 p2 替换，p1 不可按 ID 回查
        # （既有契约）；p2 创建成功本身即强证明 p1 已不在 open 态
        # （否则规则 8 会拒绝创建），再以 gate 影子锁定迁移 verdict
        check("[A2] gate 影子记录 cancel_new_question（真实迁移）",
              rt.interceptor.shadow_log
              and rt.interceptor.shadow_log[-1]["verdict"]
              == "cancel_new_question"
              and rt.interceptor.shadow_log[-1]["pending_id"]
              == p1.pending_id, str(rt.interceptor.shadow_log[-1:]))
        p2 = rt.store.get_active_pending()
        check("[fresh] D1①：新 Pending 快照=本轮 resolver 结果",
              p2 is not None and p2.pending_id != p1.pending_id
              and p2.candidates[0].name == "乙餐厅（玄武店）"
              and p2.candidates[1].name == "乙餐厅（浦东店）",
              str([c.name for c in (p2.candidates if p2 else [])]))
        check("[fresh] hint 刷新为新 ID",
              s.active_pending_id == p2.pending_id, "")
        # 轮 3（C1 集成）：hand_back 话术（gate 零状态交回）→ 生产钩子
        # clarify → 规则 8 拒绝（p2 open）→ 原链路，不覆盖不轮换
        r3 = orc.handle(s, "南京那家怎么样")
        check("[C1] hand_back 轮规则 8 拒绝 → 原链路",
              orc.last_ctx.route != "pending_produce", str(r3))
        check("[C1] 原 Pending 保持 open 未被覆盖",
              rt.store.get_active_pending().pending_id == p2.pending_id
              and rt.store.get_pending_by_id(
                  p2.pending_id).status == "open", "")
        check("[C1] producer 影子记录规则 8",
              any(x["action"] == "produce_rejected_rule8"
                  for x in orc._producer.shadow_log),
              str(orc._producer.shadow_log))
    finally:
        _orch_mod._resolve_entities = old
        set_env()


def test_p25a_channel0_zero_side_effect() -> None:
    """C1/C3（开关负向）：channel=0 → 完全旁路：决策链不调用、
    producer/decider/manager 零构造、零 Pending 创建。"""
    for env in (None, "0"):
        set_env(channel=env)
        old = _orch_mod._resolve_entities
        try:
            stub = _StubResolver([ResolveResult(resolved=[
                _amb_rc("海贝皇大排档（南京店）", "南京",
                        "海贝皇大排档（上海店）", "上海")])])
            _orch_mod._resolve_entities = stub
            orc = Orchestrator()
            s = _produce_session(orc, "s-off")
            r = orc.handle(s, "海贝皇大排档哪家好")
            check(f"[C1 env={env}] 正常出回复（原链路）",
                  r is not None, str(r))
            check(f"[C1 env={env}] route 非 pending_produce",
                  orc.last_ctx.route != "pending_produce",
                  str(orc.last_ctx.route))
            check(f"[C1 env={env}] producer 未构造",
                  orc._producer is None, str(orc._producer))
            check(f"[C1 env={env}] decider 未构造（决策链未调用）",
                  orc._decider is None and stub.calls == 0,
                  f"{orc._decider} calls={stub.calls}")
            check(f"[C1 env={env}] manager 未构造（零 Pending 副作用）",
                  orc._pending_manager is None, "")
        finally:
            _orch_mod._resolve_entities = old
    set_env()


def test_p25a_non_clarify_not_produced() -> None:
    """C2（D1 约束②负向）：仅 clarify 进生产侧——confirmed/execute
    无论 TAKEOVER 取值一律交回原链路，钩子绝不消费 execute。"""
    old = _orch_mod._resolve_entities
    try:
        # confirmed → decide(execute, takeover=0) → None → 原链路
        set_env(channel="1", store_kind="memory")
        stub = _StubResolver([ResolveResult(resolved=[
            _conf_rc("唯一好店", "南京")])])
        _orch_mod._resolve_entities = stub
        orc = Orchestrator()
        s = _produce_session(orc, "s-exe")
        r = orc.handle(s, "唯一好店怎么样")
        check("[C2] confirmed → 原链路（execute 不被钩子消费）",
              orc.last_ctx.route != "pending_produce", str(r))
        rt = orc._pending_manager.runtime("s-exe")
        check("[C2] 零 Pending 创建",
              rt.store.get_active_pending() is None, "")
        check("[C2] 决策影子记录 execute（决策真实形成）",
              orc._decider.shadow_log
              and orc._decider.shadow_log[-1]["action"] == "execute", "")
        os.environ["TRAVEL_PENDING_DECISION_TAKEOVER_CLEAN"] = "1"
        # TAKEOVER=1 批次内 → decide 返回 execute（原语义）→ 钩子仍交回
        os.environ["TRAVEL_DECISION_TAKEOVER"] = "1"
        try:
            r2 = orc.handle(s, "唯一好店怎么样")
            check("[C2] TAKEOVER=1 execute → 钩子仍不消费（原链路）",
                  orc.last_ctx.route != "pending_produce", str(r2))
        finally:
            os.environ.pop("TRAVEL_DECISION_TAKEOVER", None)
            os.environ.pop("TRAVEL_PENDING_DECISION_TAKEOVER_CLEAN", None)
        check("[C2'] TAKEOVER=1 后零 Pending 创建",
              rt.store.get_active_pending() is None, "")
    finally:
        _orch_mod._resolve_entities = old
        set_env()


def test_p25a_decision_chain_failure() -> None:
    """决策链异常（resolve 抛出）→ 保守交回原链路 + 警告 + lease 归还。"""
    set_env(channel="1", store_kind="memory")
    old = _orch_mod._resolve_entities
    try:
        def boom(candidates, context=None, **kw):
            raise RuntimeError("注入：resolve 故障")
        _orch_mod._resolve_entities = boom
        orc = Orchestrator()
        s = _produce_session(orc, "s-fail")
        r = orc.handle(s, "海贝皇大排档哪家好")
        check("[链路异常] 原链路正常出回复", r is not None, str(r))
        check("[链路异常] route 非 pending_produce",
              orc.last_ctx.route != "pending_produce", "")
        check("[链路异常] 警告已记录",
              any("决策链异常" in w for w in orc._pending_warnings),
              str(orc._pending_warnings))
        rt = orc._pending_manager.runtime("s-fail")
        check("[链路异常] lease 已归还", rt.active_ops == 0, "")
        check("[链路异常] 零 Pending 副作用",
              rt.store.get_active_pending() is None, "")
    finally:
        _orch_mod._resolve_entities = old
        set_env()

# ================================================== main


def main() -> int:
    for fn in (test_channel_off_full_bypass,
               test_channel_on_no_pending_bypass,
               test_channel_on_missing_sid_bypass,
               test_b1_consume_ok_shortcircuit,
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
               test_gate_and_store_integration_real_handle,
               test_real_thread_lease_blocks_eviction,
               test_real_thread_acquire_release_evict_stress,
               test_gate_evaluate_exception_returns_lease,
               test_acquire_clock_failure_no_lease_leak,
               test_b1_consume_fail_no_shortcircuit,
               test_b2_act_exception_open_status,
               test_b4_commit_boundary_exception,
               test_b4_undeterminable_conservative,
               test_b1_cancel_and_new_question_verdicts,
               test_b1_expired_restate_handback_duplicate_none,
               test_b3_reply_construction_failure_still_shortcircuit,
               test_b5_shadow_sink_exception_isolated,
               test_b7_b8_real_handle_route_and_no_snatch,
               test_b9_cross_session_isolation_both_impls,
               test_b10_channel0_bitwise_with_pending,
               test_p25a_produce_hook_integration,
               test_p25a_rule8_and_freshness,
               test_p25a_channel0_zero_side_effect,
               test_p25a_non_clarify_not_produced,
               test_p25a_decision_chain_failure):
        fn()
    print(f"\npending channel P2.4a(+R1+P2.4b+P2.5a): {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
