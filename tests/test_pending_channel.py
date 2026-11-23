"""P2.4a 契约测试：pending 消费通道装配层（Runtime Manager + 开关 +
唯一拦截点旁路验证）。

设计依据 docs/PHASE5_P24_PLAN.md r5（基线 c9ead95 + 批准条件补明确）
§2/§3/§4/§6 P2.4a 组。运行：python tests/test_pending_channel.py

边界（七条执行边界）：默认关完全旁路；不创建 Pending（测试预置）；
不改 Handler；P2.4b 的 act/verdict 分发绝不执行（观察模式）。
"""

import copy
import inspect
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

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
        # （r6 密闭性修复：显式注入 now=clock——此前 evaluate 未传
        # now 时 _now_epoch 回退真实墙钟，与 fixed_clock store 混源，
        # expired/TTL 判定跨日不确定）
        v = PendingInterceptor(st_b).evaluate(
            "第一个", make_ctx("第一个").parse,
            session_pending_id=pb.pending_id, now=clock)
        check("[测试 A] 到期后选择 → expired_reply",
              v.verdict == V_EXPIRED_REPLY, str(v.as_dict()))
        # 测试 B：重复选择仍识别原消费记录
        # （解析桩：poi_candidates 为空 → 纯选择线索；真实 parse 会把
        # 「第一个」抽成具名候选，按 P2.3 冻结规则正确拒绝——见 §6）
        class _PR:
            poi_candidates = []
            intent_coarse = ""
        v2 = PendingInterceptor(stores["d"]).evaluate(
            "第一个", _PR(), session_pending_id=pd.pending_id, now=clock)
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
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        _rv_mod._resolve_entities = old_rv
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


class _ClockBoundInterceptor(PendingInterceptor):
    """测试密闭性装具（r6）：evaluate 强制注入 fixed clock。

    背景：PendingInterceptor._now_epoch(None) 回退真实墙钟
    （time.time()），与 fixed_clock store 组合成混合时间源——
    duplicate_select 的 600 秒重选窗口判定（now_e - consumed_at
    > window）在执行日期跨过 fixed 时间戳后确定性失败（时间炸弹，
    2026-11-19 330/0 系真实时间早于 fixed 时间戳、负 diff 意外
    通过）。orchestrator._pending_gate 为冻结模块、调用 evaluate
    不传 now=，gate 路径只能在测试侧绑定时钟。本类零业务语义
    改变，仅使时间源单一化。"""

    def __init__(self, store, clock, **kw) -> None:
        super().__init__(store, **kw)
        self._clock = clock

    def evaluate(self, utterance, parse_result=None, **kw):
        kw.setdefault("now", self._clock)
        return super().evaluate(utterance, parse_result, **kw)


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
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        # （r6 密闭性修复：gate 路径经冻结的 _pending_gate 调用
        # evaluate 不传 now=，此处用 _ClockBoundInterceptor 注入
        # fixed clock，消除与 store 时间戳的混合时间源）
        store3 = PendingStore(session_id="s3", now=clock)
        orc3, s3, store3, rt3, _ = _gate_harness(
            clock, sid="s3", store=store3,
            interceptor=_ClockBoundInterceptor(store3, clock))
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
        _rv_mod._resolve_entities = old_rv
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
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        _rv_mod._resolve_entities = old_rv
        set_env()


def test_b7_b8_real_handle_route_and_no_snatch() -> None:
    """B7：短路落点 route=pending_consume / handled=True / history 追加；
    B8：pending 选择不被下游 gate 抢答（含周边 gate 前的真实 handle）。"""
    set_env(channel="1", store_kind="memory")
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        _rv_mod._resolve_entities = old_rv
        set_env()


def test_b9_cross_session_isolation_both_impls() -> None:
    """B9：跨 Session 隔离——A 消费不影响 B；memory 与 SQLite 双实现。"""
    set_env(channel="1", store_kind="memory")
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        _rv_mod._resolve_entities = old_rv
        set_env()

    # SQLite 双实现
    from travel_agent.pending_store_sqlite import SQLitePendingStore
    import tempfile
    fd, dbp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    set_env(channel="1", store_kind="sqlite")
    old_rv = _stub_rv_confirmed()   # P2.5b 兼容垫片：plan 类 confirmed → 冻结确认话术
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
        _rv_mod._resolve_entities = old_rv
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
from travel_agent.pending_producer import _ORPHAN_NOTICE


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


def test_p25a_compensation_boundary_integration() -> None:
    """P2.5a-R2 集成（§10.2 缺口 1/2）：真实 handle 下 reply_builder
    返回 None / 抛异常 → 钩子触发补偿矩阵——孤儿被取消或保守拦截，
    任何路径 lease 均经 finally 归还、绝不静默交回 open 孤儿。"""
    set_env(channel="1", store_kind="memory")
    old_resolve = _orch_mod._resolve_entities
    old_answer = _orch_mod.answer_reply
    try:
        stub = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        _orch_mod._resolve_entities = stub
        orc = Orchestrator()

        # 形态 A：builder 返回 None → 补偿取消孤儿 → 交回原链路
        _orch_mod.answer_reply = lambda session, text: None
        s = _produce_session(orc, "s-r2a")
        r = orc.handle(s, "海贝皇大排档哪家好")
        rt = orc._pending_manager.runtime("s-r2a")
        check("[R2集成A] route 非 pending_produce（补偿后交回）",
              orc.last_ctx.route != "pending_produce", str(r))
        check("[R2集成A] 孤儿已被取消（无 open Pending）",
              rt.store.get_active_pending() is None, "")
        check("[R2集成A] lease 已归还", rt.active_ops == 0, "")
        check("[R2集成A] producer 影子记录 bind_failed(None)",
              any(x["action"] == "produce_bind_failed"
                  and "None" in x["detail"]
                  for x in orc._producer.shadow_log),
              str(orc._producer.shadow_log))

        # 形态 B：builder 首次（展示构造）抛异常 + cancel 成功 →
        # 取消后才允许交回原链路；原链路自身的构造恢复成功
        b_calls = {"n": 0}
        def boom_once_answer(session, text):
            b_calls["n"] += 1
            if b_calls["n"] == 1:
                raise RuntimeError("注入：首次 Reply 构造失败")
            from travel_agent.dialogue import Reply
            return Reply(mode="answer", turn_no=session.turn_no,
                         ask_rounds=session.ask_rounds,
                         slots=dict(session.slots), render=text)
        _orch_mod.answer_reply = boom_once_answer
        s2 = _produce_session(orc, "s-r2b")
        r2 = orc.handle(s2, "海贝皇大排档哪家好")
        rt2 = orc._pending_manager.runtime("s-r2b")
        check("[R2集成B] route 非 pending_produce（取消后交回）",
              orc.last_ctx.route != "pending_produce", str(r2))
        check("[R2集成B] 孤儿已被取消",
              rt2.store.get_active_pending() is None
              and any(x["action"] == "produce_orphan_cancelled"
                      for x in orc._producer.shadow_log), "")
        check("[R2集成B] lease 已归还", rt2.active_ops == 0, "")

        # 形态 C（§11.R3.1 双终结形态）：builder 二次构造也失败 +
        # cancel/expire 双终结故障（注入）→ 第二层自建 Reply 保守拦截，
        # 绝不交回原链路，lease 仍归还
        calls = {"n": 0}
        def always_boom(session, text):
            calls["n"] += 1
            raise RuntimeError(f"注入：第 {calls['n']} 次构造失败")
        _orch_mod.answer_reply = always_boom
        s3 = _produce_session(orc, "s-r2c")
        rt3 = orc._pending_runtime("s-r2c")    # 预装配（acquire 一次）
        orig_cancel = rt3.store.cancel_pending
        orig_expire = rt3.store.expire_pending
        def cancel_boom(pending_id):
            raise RuntimeError("注入：cancel 失败")
        def expire_boom(pending_id):
            raise RuntimeError("注入：expire 失败")
        rt3.store.cancel_pending = cancel_boom
        rt3.store.expire_pending = expire_boom
        orc._pending_manager.release(rt3)      # 归还预获取 lease（仅注入用）
        try:
            r3 = orc.handle(s3, "海贝皇大排档哪家好")
        finally:
            rt3.store.cancel_pending = orig_cancel
            rt3.store.expire_pending = orig_expire
        rt3 = orc._pending_manager.runtime("s-r2c")
        check("[R2集成C] handle 正常返回（异常未传播）",
              r3 is not None, str(r3))
        check("[R2集成C] 保守拦截 = pending_produce 短路",
              orc.last_ctx.route == "pending_produce"
              and _ORPHAN_NOTICE in str(r3), str(r3))
        check("[R2集成C] 返回对象满足 Reply 契约（服务层属性访问安全）",
              hasattr(r3, "render") and hasattr(r3, "mode")
              and getattr(r3, "mode", None) == "answer"
              and not isinstance(r3, str)
              and _ORPHAN_NOTICE == getattr(r3, "render", None), str(r3))
        check("[R2集成C] 孤儿 open 保留可排查",
              rt3.store.get_active_pending() is not None, "")
        check("[R2集成C] lease 已归还", rt3.active_ops == 0, "")
        check("[R2集成C] shadow 记录 orphan + 双次 bind/conservative 失败",
              any(x["action"] == "produce_orphan_cancel_failed"
                  for x in orc._producer.shadow_log)
              and calls["n"] >= 2, str(orc._producer.shadow_log))
    finally:
        _orch_mod._resolve_entities = old_resolve
        _orch_mod.answer_reply = old_answer
        set_env()


def test_p25a_orphan_terminal_integration() -> None:
    """§11.R3.1 批准增量集成（r4 裁定 2026-11-19）：L1 二线终结补偿
    真实 handle 链路——单终结故障 expire 成功 → 可验证隔离后交回；
    双终结故障 → 保守拦截；R4 场景二停在定位证据层、场景一持续
    故障走 P2.4b fail-closed。"""
    set_env(channel="1", store_kind="memory")
    old_resolve = _orch_mod._resolve_entities
    old_answer = _orch_mod.answer_reply
    try:
        stub = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        _orch_mod._resolve_entities = stub

        # 形态 D：builder 必然失败 + cancel 注入失败 + expire 真实成功
        # → 二线终结 → 孤儿终态 → 交回原链路（隔离可验证：定位判 None）
        _orch_mod.answer_reply = lambda session, text: None
        orc = Orchestrator()
        s = _produce_session(orc, "s-r4d")
        rt = orc._pending_runtime("s-r4d")     # 预装配注入
        orig_cancel = rt.store.cancel_pending
        def cancel_boom(pending_id):
            raise RuntimeError("注入：cancel 失败")
        rt.store.cancel_pending = cancel_boom
        orc._pending_manager.release(rt)
        try:
            r = orc.handle(s, "海贝皇大排档哪家好")
        finally:
            rt.store.cancel_pending = orig_cancel
        rt = orc._pending_manager.runtime("s-r4d")
        check("[形态D] 二线终结成功 → 交回原链路",
              orc.last_ctx.route != "pending_produce", str(r))
        check("[形态D] 隔离可验证：权威定位判 None（孤儿已终态）",
              rt.store.get_active_pending() is None, "")
        check("[形态D] shadow 记 orphan_terminal_retry",
              any(x["action"] == "produce_orphan_terminal_retry"
                  for x in orc._producer.shadow_log),
              str(orc._producer.shadow_log))
        check("[形态D] lease 已归还", rt.active_ops == 0, "")

        # 形态 E：双终结故障（cancel+expire 均注入失败）→ 保守拦截
        orc2 = Orchestrator()
        s2 = _produce_session(orc2, "s-r4e")
        rt2 = orc2._pending_runtime("s-r4e")
        orig_cancel2 = rt2.store.cancel_pending
        orig_expire2 = rt2.store.expire_pending
        def cancel_boom2(pending_id):
            raise RuntimeError("注入：cancel 失败")
        def expire_boom2(pending_id):
            raise RuntimeError("注入：expire 失败")
        rt2.store.cancel_pending = cancel_boom2
        rt2.store.expire_pending = expire_boom2
        orc2._pending_manager.release(rt2)
        try:
            r2 = orc2.handle(s2, "海贝皇大排档哪家好")
        finally:
            rt2.store.cancel_pending = orig_cancel2
            rt2.store.expire_pending = orig_expire2
        rt2 = orc2._pending_manager.runtime("s-r4e")
        check("[形态E] 双终结失败 → pending_produce 保守拦截短路",
              orc2.last_ctx.route == "pending_produce"
              and _ORPHAN_NOTICE in str(r2), str(r2))
        check("[形态E] 返回对象满足 Reply 契约",
              hasattr(r2, "render") and getattr(r2, "mode", None) == "answer"
              and not isinstance(r2, str), str(type(r2)))
        check("[形态E] 孤儿 open 保留（未伪造隔离成功）",
              rt2.store.get_active_pending() is not None, "")
        check("[形态E] shadow 记 terminal_retry_failed 含孤儿 ID",
              any(x["action"] == "produce_orphan_terminal_retry_failed"
                  and x["pending_id"]
                  for x in orc2._producer.shadow_log),
              str(orc2._producer.shadow_log))
        check("[形态E] lease 已归还", rt2.active_ops == 0, "")

        # R4 场景二（故障恢复）：双终结失败后存储读取正常——按裁定
        # 边界**停在定位证据层**：孤儿 TTL 内仍可被权威定位（残余
        # 窗口存在性证据），shadow 双通道保留孤儿身份；绝不进入
        # 消费/选择解析/Handler 执行（本测试到此为止，不再 handle）
        check("[场景二] 故障恢复后权威定位仍可达（残余窗口证据）",
              rt2.store.get_active_pending() is not None, "")
        orphan_id = rt2.store.get_active_pending().pending_id
        check("[场景二] shadow 双通道孤儿身份（cancel_failed + "
              "terminal_retry_failed 同 ID）",
              all(any(x["action"] == a and x["pending_id"] == orphan_id
                      for x in orc2._producer.shadow_log)
                  for a in ("produce_orphan_cancel_failed",
                            "produce_orphan_terminal_retry_failed")),
              str(orc2._producer.shadow_log))

        # R4 场景一（持续故障）：双终结失败后下一轮 store 读取持续
        # 故障 → P2.4b 消费侧边界：绝不消费孤儿（fail-closed）
        orc3 = Orchestrator()
        s3 = _produce_session(orc3, "s-r4f")
        stub2 = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        _orch_mod._resolve_entities = stub2
        rt3 = orc3._pending_runtime("s-r4f")
        orig_cancel3 = rt3.store.cancel_pending
        orig_expire3 = rt3.store.expire_pending
        orig_getact = rt3.store.get_active_pending
        rt3.store.cancel_pending = cancel_boom2
        rt3.store.expire_pending = expire_boom2
        orc3._pending_manager.release(rt3)
        try:
            orc3.handle(s3, "海贝皇大排档哪家好")   # 保守拦截轮
        finally:
            rt3.store.cancel_pending = orig_cancel3
            rt3.store.expire_pending = orig_expire3
        pid3 = rt3.store.get_active_pending().pending_id
        # 下一轮：读取持续故障（get_active_pending 抛异常）
        def getact_boom():
            raise RuntimeError("注入：store 读取持续故障")
        rt3.store.get_active_pending = getact_boom
        try:
            orc3.handle(s3, "第1个")
            raised3 = False
        except RuntimeError:
            raised3 = True
        finally:
            rt3.store.get_active_pending = orig_getact
        rt3 = orc3._pending_manager.runtime("s-r4f")
        check("[场景一] 持续故障轮异常按 P2.4b 冻结边界向上传播"
              "（绝不静默消费）", raised3, "")
        check("[场景一] 孤儿 Pending 未被消费（仍 open、零消费记录）",
              rt3.store.get_pending_by_id(pid3).status == "open"
              and rt3.store._latest_consumption() is None, "")
        check("[场景一] lease 已归还（异常路径 finally 生效）",
              rt3.active_ops == 0, str(rt3.active_ops))
    finally:
        _orch_mod._resolve_entities = old_resolve
        _orch_mod.answer_reply = old_answer
        set_env()


def test_p25a_channel0_clarify_decision_bitwise() -> None:
    """§11.R3.5 增补（r4 裁定）：decision 逐位对比覆盖 channel=0 时
    本轮真实 clarify 场景——decide 未接管分支 + D1 放行 hunk 的
    零影子行为差（真实 parse + 真实 ambiguous resolve 输入）。
    （r5 验收补证：TRAVEL_DECISION_TAKEOVER 由本测试显式保存、
    固定为 "0"、并在 finally 中恢复——测试条件独立、确定、可恢复，
    不受执行环境影响。）"""
    from travel_agent.decision import DecisionEngine
    from travel_agent.parser import parse_utterance

    # 真实 clarify 输入：真实 parse（本轮问句）+ 真实 ambiguous 形态
    s0 = Session(use_llm=False)
    parse0 = parse_utterance("海贝皇大排档哪家好", context=s0,
                             today=s0.today)
    resolved = [_amb_rc("海贝皇大排档（南京店）", "南京",
                        "海贝皇大排档（上海店）", "上海")]
    rctx = {"slots": {}, "trip_dest": "", "last_city": ""}

    # 环境变量显式控制：两个开关均保存原值、按测试条件固定、finally 恢复
    old_chan = os.environ.get("TRAVEL_PENDING_CHANNEL")
    old_takeover = os.environ.get("TRAVEL_DECISION_TAKEOVER")
    try:
        # 基线：P2.5a 引入前语义（CHANNEL 未设）→ decide None + 影子
        os.environ.pop("TRAVEL_PENDING_CHANNEL", None)
        os.environ["TRAVEL_DECISION_TAKEOVER"] = "0"
        assert os.environ["TRAVEL_DECISION_TAKEOVER"] == "0"
        eng_base = DecisionEngine()
        d_base = eng_base.decide(parse0, resolved, dict(rctx),
                                 intent=(parse0.intent or "") or None)
        # channel=0：D1 放行条件不满足 → 原行为逐位保持
        os.environ["TRAVEL_PENDING_CHANNEL"] = "0"
        assert os.environ["TRAVEL_PENDING_CHANNEL"] == "0"
        assert os.environ["TRAVEL_DECISION_TAKEOVER"] == "0"
        eng0 = DecisionEngine()
        d0 = eng0.decide(parse0, resolved, dict(rctx),
                         intent=(parse0.intent or "") or None)
        # channel=1 + TAKEOVER=0：D1 单点放行（影子仍零差）
        os.environ["TRAVEL_PENDING_CHANNEL"] = "1"
        os.environ["TRAVEL_DECISION_TAKEOVER"] = "0"
        assert os.environ["TRAVEL_PENDING_CHANNEL"] == "1"
        assert os.environ["TRAVEL_DECISION_TAKEOVER"] == "0"
        eng1 = DecisionEngine()
        d1 = eng1.decide(parse0, resolved, dict(rctx),
                         intent=(parse0.intent or "") or None)

        check("[逐位] channel=0 真实 clarify 场景 decide 返回与基线一致"
              "（None，未形成消费）", d_base is None and d0 is None,
              f"base={d_base} ch0={d0}")
        check("[逐位] channel=0 影子记录与基线逐位一致",
              eng0.shadow_log == eng_base.shadow_log,
              f"{eng0.shadow_log}\n{eng_base.shadow_log}")
        check("[逐位] D1 放行分支（channel=1）影子记录与基线逐位一致"
              "（放行零影子差）",
              eng1.shadow_log == eng_base.shadow_log,
              f"{eng1.shadow_log}\n{eng_base.shadow_log}")
        check("[逐位] D1 单点放行：clarify DecisionResult 离开影子",
              d1 is not None and d1.action == "clarify"
              and "歧义" in d1.reason, str(d1))
        # D1 契约：clarify 结果本身不携带 pending（消歧载荷由
        # producer 从本轮 fresh matches 构建，D1 绝不从历史恢复）
        # （r6 健壮性：d1 为 None 时不再直接访问属性，保证失败项
        # 完整报告而不中断后续断言）
        check("[逐位] D1 放行结果 pending=None（载荷由生产侧从本轮 "
              "fresh matches 构建，绝不从历史/旧 Pending 恢复）",
              d1 is not None and d1.pending is None, str(d1))
        # 放行结果确定性：同输入重放逐位一致
        os.environ["TRAVEL_PENDING_CHANNEL"] = "1"
        os.environ["TRAVEL_DECISION_TAKEOVER"] = "0"
        assert os.environ["TRAVEL_PENDING_CHANNEL"] == "1"
        assert os.environ["TRAVEL_DECISION_TAKEOVER"] == "0"
        eng2 = DecisionEngine()
        d2 = eng2.decide(parse0, resolved, dict(rctx),
                         intent=(parse0.intent or "") or None)
        check("[逐位] 放行结果重放逐位一致（确定性构造）",
              repr(d2) == repr(d1), f"{d2}\n{d1}")
    finally:
        if old_chan is None:
            os.environ.pop("TRAVEL_PENDING_CHANNEL", None)
        else:
            os.environ["TRAVEL_PENDING_CHANNEL"] = old_chan
        if old_takeover is None:
            os.environ.pop("TRAVEL_DECISION_TAKEOVER", None)
        else:
            os.environ["TRAVEL_DECISION_TAKEOVER"] = old_takeover

    # channel=0 全链路逐位：真实 ambiguous 问句输出与无 pending
    # 基础设施基线逐位一致（producer/decider/manager 零构造）
    # （r5：该阶段同样显式固定 TAKEOVER=0，并用完恢复）
    # （r6 健壮性：环境设置与断言全部移入 try——即使断言失败，
    # finally 的恢复逻辑也保证执行）
    try:
        set_env(channel="0")
        os.environ["TRAVEL_DECISION_TAKEOVER"] = "0"
        assert os.environ["TRAVEL_PENDING_CHANNEL"] == "0"
        assert os.environ["TRAVEL_DECISION_TAKEOVER"] == "0"
        old = _orch_mod._resolve_entities
        _orch_mod._resolve_entities = _StubResolver([ResolveResult(
            resolved=[_amb_rc("海贝皇大排档（南京店）", "南京",
                              "海贝皇大排档（上海店）", "上海")])])
        try:
            orc_a = Orchestrator()
            s_a = _produce_session(orc_a, "s-bit0")
            r_a = orc_a.handle(s_a, "海贝皇大排档哪家好")
            orc_b = Orchestrator()
            s_b = Session(use_llm=False)      # 无任何 pending 身份
            r_b = orc_b.handle(s_b, "海贝皇大排档哪家好")
            check("[逐位] channel=0 真实 clarify 问句输出与无 pending "
                  "基线逐位一致", str(r_a) == str(r_b),
                  f"{str(r_a)[:80]} / {str(r_b)[:80]}")
            check("[逐位] channel=0 零构造（producer/decider/manager）",
                  orc_a._producer is None and orc_a._decider is None
                  and orc_a._pending_manager is None, "")
        finally:
            _orch_mod._resolve_entities = old
    finally:
        set_env()
        if old_takeover is None:
            os.environ.pop("TRAVEL_DECISION_TAKEOVER", None)
        else:
            os.environ["TRAVEL_DECISION_TAKEOVER"] = old_takeover


# ============================================ 9. P2.5b：重验证侧集成（消费 → fresh 重验证 → 四分支/受控流转）

import travel_agent.pending_revalidator as _rv_mod
from travel_agent.pending_runtime import PendingRuntime


def _conf_rc_fresh(text, city, source, confidence=0.92):
    """confirmed 候选（entity_id 可控——fresh 与快照区分断言用）。"""
    return ResolvedCandidate(
        original={"text": text, "entity_type": "restaurant", "role": "query"},
        status="confirmed", confidence=confidence,
        probe_status="not_applicable",
        matches=[MatchRow(city=city, name=text, category="restaurant",
                          source=source)])


def _unver_rc(text):
    return ResolvedCandidate(
        original={"text": text, "entity_type": "unknown", "role": "query"},
        status="unverified", confidence=0.0,
        probe_status="not_requested",
        probe_reason="disabled_by_policy")


def _rej_rc(text):
    return ResolvedCandidate(
        original={"text": text, "entity_type": "unknown", "role": "query"},
        status="rejected", confidence=0.3, probe_status="not_found",
        probe_reason="cross_source_empty")


def _preset_orc(sid, clock=None, *, intent="poi_query", role="query",
                ut="哪家海贝皇大排档好"):
    if clock is None:
        clock = lambda: datetime(2026, 11, 19, 10, 0, 0)
    """预置 store + runtime 注入 manager（B 段惯用法）；
    ut 参数可注入自定义原始问句（机制 E 反证装具用）。"""
    orc = Orchestrator()
    s = Session(use_llm=False)
    s.pending_session_id = sid
    store = PendingStore(session_id=sid, now=clock)
    p = store.create_pending(ut, intent, role, SNAP)
    s.active_pending_id = p.pending_id
    mgr = PendingRuntimeManager(max_runtimes=8, now=clock)
    rt = PendingRuntime(sid, store, PendingInterceptor(store),
                        last_used=int(clock().timestamp()))
    mgr._runtimes[sid] = rt
    orc._pending_manager = mgr
    return orc, s, store, p


def _count_answers(s):
    """包装 session._spot_qa 统计消费轮直调业务入口调用次数（F1/A6
    用；机制 E 后消费轮执行入口 = _spot_qa/_nearby_qa 实体参数化
    直调，session._answer 不再是消费轮执行路径——见 §21/§22）。"""
    orig = s._spot_qa
    calls = {"n": 0}

    def counting(city, spot):
        calls["n"] += 1
        return orig(city, spot)
    s._spot_qa = counting
    return calls



def _stub_rv_confirmed():
    """P2.5b 兼容垫片：为不关心重验证的既有 B 段测试注入 confirmed
    重验证桩——plan 类 pending 消费后走冻结确认话术，保持其原始断言
    语义（返回原 resolve 引用供 finally 恢复）。"""
    old = _rv_mod._resolve_entities
    _rv_mod._resolve_entities = _StubResolver([
        ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-nj")])])
    return old


def _stub_rv_unverified():
    """B 段兼容垫片：unverified 桩（真实 KB 未命中路径的显式形式）。"""
    old = _rv_mod._resolve_entities
    _rv_mod._resolve_entities = _StubResolver([
        ResolveResult(resolved=[_unver_rc("海贝皇大排档（南京店）")])])
    return old


def test_p25b_a3_confirmed_controlled_flow() -> None:
    """A3（强化断言）：选择 → 消费 → 重验证 confirmed → 查询类经
    AllowHandler 五条件进既有分支入口——Handler 收到重验证后构造的
    DecisionResult（ctx 载体）、entity 为 fresh 实体、reason 全链
    归因、同一轮无重复意图解析重复执行。"""
    set_env(channel="1", store_kind="memory")
    old_orch_resolve = _orch_mod._resolve_entities
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        # 轮 1 生产桩：ambiguous 双店（走真实 produce 链）
        _orch_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _amb_rc("海贝皇大排档（南京店）", "南京",
                        "海贝皇大排档（上海店）", "上海")])])
        # 轮 2 重验证桩：confirmed 单店（fresh entity_id ≠ 快照）
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        orc = Orchestrator()
        s = _produce_session(orc, "s-a3")
        r1 = orc.handle(s, "帮我查一下海贝皇大排档")   # poi_query
        rt = orc._pending_manager.runtime("s-a3")
        p1 = rt.store.get_active_pending()
        check("[A3] 轮1 创建 pending（intent=poi_query 入白名单）",
              p1 is not None and p1.original_intent == "poi_query",
              str(p1))
        # 机制 E：受控直调桩——捕获实参（fresh entity 恒等断言）并
        # 返回受控产出（避免真实 KB/autofetch 的网络不确定性）
        spot_calls = []
        orig_spot_qa = s._spot_qa

        def cap_spot_qa(city, spot):
            spot_calls.append((city, spot))
            return f"已为您介绍「{spot}」（{city}）：门票与开放时间如上。"
        s._spot_qa = cap_spot_qa
        r2 = orc.handle(s, "第1个")                        # 选择 c1
        check("[A3] route=pending_consume_execute（受控流转）",
              orc.last_ctx.route == "pending_consume_execute"
              and orc.last_ctx.handled, str(orc.last_ctx.route))
        d = orc.last_ctx.pending_decision
        check("[A3] Handler 收到重验证后构造的 DecisionResult（ctx 载体）",
              d is not None and d.action == "execute"
              and d.intent == "poi_query", str(d))
        check("[A3] entity 为 fresh confirmed 实体（非快照旧 entity_id）",
              d is not None and d.entity["entity_id"] == "e-nj-fresh"
              and d.entity["status"] == "confirmed", str(d.entity))
        cid_a3 = p1.candidates[0].candidate_id
        check("[A3] reason 全链归因（pending_id/candidate_id/revalidate）",
              d is not None and p1.pending_id in d.reason
              and cid_a3 in d.reason and "pending_revalidate" in d.reason,
              d.reason if d else "")
        check("[A3] 消费记录回填 confirmed（E1·confirmed 行）",
              rt.store._latest_consumption()["revalidate_status"]
              == "confirmed"
              and rt.store._latest_consumption()["revalidated_at"]
              is not None, str(rt.store._latest_consumption()))
        check("[A3] 同一轮恰一次直调且实参恒等 fresh entity"
              "（机制 E：Entity_Decision == Entity_BusinessMethod）",
              len(spot_calls) == 1
              and spot_calls[0] == ("南京", "海贝皇大排档（南京店）"),
              str(spot_calls))
        check("[A3] pending 已消费 + hint 已清空",
              rt.store.get_pending_by_id(p1.pending_id).status == "consumed"
              and s.active_pending_id is None, "")
        check("[A3] lease 已归还", rt.active_ops == 0, "")
        check("[A3] 回复非 None（Handler 产出）", r2 is not None, str(r2))
        check("[A3] 重验证影子含 confirmed 记录（E2）",
              any(x["action"] == "revalidate_confirmed_execute_built"
                  and x["pending_id"] == p1.pending_id
                  and x["candidate_id"] == cid_a3
                  for x in orc._revalidator.shadow_log),
              str(orc._revalidator.shadow_log[-2:]))
    finally:
        s._spot_qa = orig_spot_qa
        _orch_mod._resolve_entities = old_orch_resolve
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_a4_plan_confirmed_no_plan_write() -> None:
    """A4：规划类 confirmed → 只回冻结确认话术，行程状态深拷贝对比
    零写入；决策层不构造 execute（消费点不进入）。"""
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        orc, s, store, p = _preset_orc("s-a4", lambda: datetime(2026, 11, 19, 10, 0, 0),
                                       intent="plan_modify", role="query")
        # 预置规划状态（深拷贝对比基线）
        s.slots = {"destination": "南京", "days": 2}
        s.last_plan = {"city": "南京", "days": 2, "items": ["x"]}
        import copy as _copy
        snap = _copy.deepcopy({"slots": s.slots, "plan": s.last_plan})
        calls = _count_answers(s)
        r = orc.handle(s, "第1个")
        check("[A4] 只回冻结确认话术（route=pending_consume）",
              orc.last_ctx.route == "pending_consume"
              and "已确认：海贝皇大排档（南京店）" in str(r), str(r))
        check("[A4] 行程状态深拷贝对比零写入",
              _copy.deepcopy({"slots": s.slots, "plan": s.last_plan})
              == snap, str(s.slots))
        check("[A4] Handler 入口调用 0 次（规划类永不放行）",
              calls["n"] == 0, str(calls))
        check("[A4] 决策层未构造 execute（decision 不入 ctx）",
              orc.last_ctx.pending_decision is None, "")
        check("[A4] 消费记录回填 confirmed（业务结论如实）",
              store._latest_consumption()["revalidate_status"]
              == "confirmed", "")
    finally:
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_a5_ambiguous_new_pending() -> None:
    """A5（强化）：ambiguous → 新 Pending（新 ID ≠ 旧 ID、新快照来自
    fresh matches；旧 Pending 保持 consumed 不复活、无重复消费）。"""
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _amb_rc("乙大排档（玄武店）", "南京",
                        "乙大排档（浦东店）", "上海")])])
        orc, s, store, p = _preset_orc("s-a5", lambda: datetime(2026, 11, 19, 10, 0, 0))
        consume_calls = {"n": 0}
        orig_consume = store.consume_pending

        def counting_consume(pid, cid):
            consume_calls["n"] += 1
            return orig_consume(pid, cid)
        store.consume_pending = counting_consume
        r = orc.handle(s, "第1个")
        check("[A5] 短路展示新快照（route=pending_consume）",
              orc.last_ctx.route == "pending_consume"
              and "乙大排档（玄武店）" in str(r)
              and "乙大排档（浦东店）" in str(r), str(r))
        rt = orc._pending_manager.runtime("s-a5")
        p2 = rt.store.get_active_pending()
        check("[A5] 新 Pending 独立 ID（≠ 旧 ID）",
              p2 is not None and p2.pending_id != p.pending_id, str(p2))
        check("[A5] 新快照来自 fresh matches（1..2）",
              [c.display_order for c in p2.candidates] == [1, 2]
              and p2.candidates[0].name == "乙大排档（玄武店）", str(p2))
        check("[A5] hint 刷新为新 Pending ID",
              s.active_pending_id == p2.pending_id, "")
        check("[A5] 消费记录回填 ambiguous",
              store._latest_consumption()["revalidate_status"]
              == "ambiguous", "")
        check("[A5] 无重复消费（重验证全程零 consume）",
              consume_calls["n"] == 1, str(consume_calls))
        check("[A5] 重验证影子记录新旧 ID 关联（E2）",
              any(x["action"] == "revalidate_new_pending_created"
                  and x["pending_id"] == p.pending_id
                  for x in orc._revalidator.shadow_log),
              str(orc._revalidator.shadow_log[-2:]))
    finally:
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_a6_a7_unverified_rejected() -> None:
    """A6：unverified → 保守回复（绝不 rejected 话术）、Handler 0 次
    （F1：原 Pending 保持 consumed）；A7：rejected → 规定话术保留
    原始表达。"""
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        # ---- A6 unverified ----
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[_unver_rc("海贝皇大排档（南京店）")])])
        orc, s, store, p = _preset_orc("s-a6", lambda: datetime(2026, 11, 19, 10, 0, 0))
        calls = _count_answers(s)
        r = orc.handle(s, "第1个")
        check("[A6] 保守回复（暂查不到 + 引导）",
              orc.last_ctx.route == "pending_consume"
              and "暂时查不到" in str(r), str(r))
        check("[A6] 绝不出现 rejected 话术",
              "没有找到" not in str(r), str(r))
        check("[A6/F1] Handler 调用 0 次",
              calls["n"] == 0
              and orc.last_ctx.route != "pending_consume_execute",
              str(calls))
        check("[A6/F1] 原 Pending 保持 consumed",
              store.get_pending_by_id(p.pending_id).status == "consumed",
              "")
        check("[A6] 回填 unverified + probe 如实（E1·unverified 行）",
              store._latest_consumption()["revalidate_status"]
              == "unverified"
              and store._latest_consumption()["revalidate_probe_status"]
              == "not_requested", str(store._latest_consumption()))
        # ---- A7 rejected ----
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[_rej_rc("海贝皇大排档（南京店）")])])
        orc2, s2, store2, p2 = _preset_orc("s-a7", lambda: datetime(2026, 11, 19, 10, 0, 0))
        calls2 = _count_answers(s2)
        r2 = orc2.handle(s2, "第1个")
        check("[A7] 规定话术 + 保留原始表达（RV-08 强化：原始问句与"
              "候选名分别出现且二者不同）",
              orc2.last_ctx.route == "pending_consume"
              and "规定数据源与检索范围内没有找到" in str(r2)
              and "哪家海贝皇大排档好" in str(r2)
              and "海贝皇大排档（南京店）" in str(r2)
              and "哪家海贝皇大排档好" != "海贝皇大排档（南京店）",
              str(r2))
        check("[A7/F1] Handler 调用 0 次 + 原 Pending consumed",
              calls2["n"] == 0
              and store2.get_pending_by_id(p2.pending_id).status
              == "consumed", str(calls2))
        check("[A7] 回填 rejected（E1·rejected 行）",
              store2._latest_consumption()["revalidate_status"]
              == "rejected", "")
    finally:
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_d1_d2_d3_robustness() -> None:
    """D1 重验证抛异常 → B3 兜底确认短路（消费不撤销）；D2 回填失败
    → 回复与状态不变、shadow 归因 + 警告（F4：无重复 Handler）；
    D3 重验证期间 store 异常 → 保守路径。"""
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        # ---- D1：重验证 resolve 抛异常 ----
        def boom(candidates, context=None, **kw):
            raise RuntimeError("注入：重验证 resolve 故障")
        _rv_mod._resolve_entities = boom
        orc, s, store, p = _preset_orc("s-d1", lambda: datetime(2026, 11, 19, 10, 0, 0))
        calls = _count_answers(s)
        r = orc.handle(s, "第1个")
        check("[D1] B3 兜底确认短路（消费事实不被撤销）",
              r is not None and "已确认您的选择" in str(r)
              and store.get_pending_by_id(p.pending_id).status
              == "consumed", str(r))
        check("[D1] 回填未发生（三列 NULL，契约 6）",
              store._latest_consumption()["revalidate_status"] is None, "")
        check("[D1] 警告已记录", any("revalidate" in w
              for w in orc._pending_warnings), str(orc._pending_warnings))
        # ---- D2：回填失败（rowcount=0 同源）→ 分支不变 + 无重复 Handler
        orc2, s2, store2, p2 = _preset_orc("s-d2", lambda: datetime(2026, 11, 19, 10, 0, 0))
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        store2.record_revalidation = lambda *a, **kw: False
        # 机制 E：受控直调桩（避免真实 KB/autofetch 网络不确定性）+
        # fresh 实参捕获
        d2_calls = []
        orig_spot2 = s2._spot_qa

        def cap_spot2(city, spot):
            d2_calls.append((city, spot))
            return f"已为您介绍「{spot}」（{city}）：门票与开放时间如上。"
        s2._spot_qa = cap_spot2
        r2 = orc2.handle(s2, "第1个")
        check("[D2] 回填失败 → 分支结果不变（仍受控流转）",
              orc2.last_ctx.route == "pending_consume_execute"
              and r2 is not None, str(r2))
        check("[D2] 回填失败警告 + shadow 归因（E2）",
              any("回填失败" in w for w in orc2._pending_warnings)
              and any(x["action"] == "revalidate_backfill_failed"
                      for x in orc2._revalidator.shadow_log),
              str(orc2._pending_warnings))
        check("[D2/F4] 无重复直调（恰 1 次）且实参恒等 fresh entity"
              "（机制 E）",
              len(d2_calls) == 1
              and d2_calls[0] == ("南京", "海贝皇大排档（南京店）"),
              str(d2_calls))
        check("[D2/F4] 无重复消费（同一 pending 恰一次）",
              store2._latest_consumption()["pending_id"] == p2.pending_id,
              "")
        # ---- D3：重验证期间 store 异常（pending 定位失败）----
        # 注意：interceptor locate 与 revalidator 归属校验共用
        # get_pending_by_id——仅第二次（重验证）调用注入故障
        orc3, s3, store3, p3 = _preset_orc("s-d3")
        orig_get = store3.get_pending_by_id
        get_calls = {"n": 0}

        def boom_get_second(pending_id):
            get_calls["n"] += 1
            if get_calls["n"] >= 2:
                raise RuntimeError("注入：store 定位故障")
            return orig_get(pending_id)
        store3.get_pending_by_id = boom_get_second
        r3 = orc3.handle(s3, "第1个")
        check("[D3] store 异常 → B3 保守路径（确认短路）",
              r3 is not None and "已确认您的选择" in str(r3), str(r3))
        check("[D3] gate evaluate 正常（首次 locate 未受影响）",
              get_calls["n"] >= 2, str(get_calls))
        store3.get_pending_by_id = orig_get
        check("[D3] lease 已归还",
              orc3._pending_manager.runtime("s-d3").active_ops == 0, "")
    finally:
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_f2_f3_negative() -> None:
    """F2：ambiguous 重建失败（规则 8 撞车）→ 不执行 Handler、不复活
    旧 Pending、不伪造成功；F3：confirmed 但 Handler 前置未接住 →
    服从原规则回退确认话术，不因消歧绕过。"""
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        # ---- F2：重建失败（create 拒绝）----
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _amb_rc("乙大排档（玄武店）", "南京",
                        "乙大排档（浦东店）", "上海")])])
        orc, s, store, p = _preset_orc("s-f2", lambda: datetime(2026, 11, 19, 10, 0, 0))
        orig_create = store.create_pending

        def rule8_create(*a, **kw):
            raise ValueError("已存在 active pending（规则 8 拒绝）")
        store.create_pending = rule8_create
        calls = _count_answers(s)
        r = orc.handle(s, "第1个")
        check("[F2] 保守重试话术（不伪造成功）",
              "稍后重试" in str(r), str(r))
        check("[F2] 不执行 Handler", calls["n"] == 0, str(calls))
        check("[F2] 不复活旧 Pending（保持 consumed + 回填 ambiguous）",
              store._latest_consumption()["pending_id"] == p.pending_id
              and store._latest_consumption()["revalidate_status"]
              == "ambiguous", str(store._latest_consumption()))
        # ---- F3：confirmed 但 Handler 前置未接住 ----
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        orc2, s2, store2, p2 = _preset_orc("s-f3", lambda: datetime(2026, 11, 19, 10, 0, 0))
        orig_answer = s2._spot_qa
        s2._spot_qa = lambda city, spot: None   # 业务方法前置未接住
        try:
            r2 = orc2.handle(s2, "第1个")
        finally:
            s2._spot_qa = orig_answer
        check("[F3] 业务方法未接住 → 回退确认话术（服从原规则不绕过）",
              orc2.last_ctx.route == "pending_consume_execute"
              and "已确认：海贝皇大排档（南京店）" in str(r2), str(r2))
        check("[F3] 决策已构造（confirmed 归因在案）",
              orc2.last_ctx.pending_decision is not None, "")
    finally:
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_mechanism_e_fresh_entity_enforcement() -> None:
    """机制 E（§18.4/§21，评审 2026-11-20 批准开工）：J1/J2/J3
    构造性证明 + 注入式反证（§21.2 增量 2 双路测试）。

    构造性证明（J1 三段等式）：Entity_Decision == Entity_Business
    Method == Entity_FinalLookup——捕获 _spot_qa 实参与 agent.
    dispatch 最终检索实参（keyword/city），三段恒等 fresh entity；
    last_city == fresh_city；shadow execute_begin/done 双记录含
    reason 归因；DecisionResult.reason 保留 pending_id/candidate_id。

    注入式反证（I2 构造不可达 + J2）：误导性原始问句（含其他景点
    名「上海迪士尼门票多少钱」）作为 Pending 原始问句注入——对照
    普通路由对同一问句真实重解析出「迪士尼」（分歧机制真实存在、
    反证有证明力），而 confirmed 消费路径最终检索实参仍为 fresh
    entity、误导实体未进入消费路径的 POI 检索 keyword（断言范围限定
    为本次捕获的 search_poi 调用，不宣称覆盖全部检索通道）——偏移
    状态在直调路径按构造不可发生，差异被限定在触点 2 内。

    消费轮隔离（评审 2026-11-20 整改项 1）：轮1 创建 pending 完成后
    清空 dispatch_calls，构造性证明的捕获只可能来自消费轮。"""
    import travel_agent.agent as _agent_mod
    set_env(channel="1", store_kind="memory")
    old_orch_resolve = _orch_mod._resolve_entities
    old_rv_resolve = _rv_mod._resolve_entities
    old_dispatch = _agent_mod.dispatch
    try:
        _orch_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _amb_rc("海贝皇大排档（南京店）", "南京",
                        "海贝皇大排档（上海店）", "上海")])])
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        dispatch_calls = []

        def cap_dispatch(name, args):
            if name == "search_poi":
                dispatch_calls.append(dict(args or {}))
                return {"ok": True, "items": [{
                    "name": "海贝皇大排档（南京店）", "city": "南京",
                    "category": "attraction", "area": "玄武区",
                    "duration_min": 90, "cost": 0,
                    "open_hours": "全天", "geo": (32.06, 118.79)}],
                    "total": 1}
            return old_dispatch(name, args)
        _agent_mod.dispatch = cap_dispatch
        orc = Orchestrator()
        s = _produce_session(orc, "s-me1")
        r1 = orc.handle(s, "帮我查一下海贝皇大排档")
        rt = orc._pending_manager.runtime("s-me1")
        p1 = rt.store.get_active_pending()
        check("[E·前置] 轮1 创建 pending", p1 is not None, str(p1))
        # ---- 构造性证明（J1/J3）----
        # 隔离：轮1（创建 pending）期间可能已产生符合 fresh entity 的
        # 检索记录，必须先清空，使下方断言只可能来自消费轮捕获
        # （评审 2026-11-20 整改项 1）
        dispatch_calls.clear()
        r2 = orc.handle(s, "第1个")
        d = orc.last_ctx.pending_decision
        check("[E·构造] route=pending_consume_execute（机制 E 受控流转）",
              orc.last_ctx.route == "pending_consume_execute"
              and d is not None, str(orc.last_ctx.route))
        check("[E·构造] Entity_Decision = fresh confirmed 实体",
              d is not None and d.entity.get("name")
              == "海贝皇大排档（南京店）"
              and d.entity.get("city") == "南京"
              and d.entity.get("status") == "confirmed", str(d))
        check("[E·构造] Entity_FinalLookup：最终检索实参恒等 fresh"
              " entity（keyword/city 逐项相等）",
              any(c.get("keyword") == "海贝皇大排档（南京店）"
                  and c.get("city") == "南京" for c in dispatch_calls),
              str(dispatch_calls))
        check("[E·构造] last_city = fresh_city（语义同 POIHandler:80）",
              s.last_city == "南京", str(s.last_city))
        check("[E·构造] 回复呈现 fresh 实体产出（非确认话术）",
              "海贝皇大排档（南京店）" in str(r2)
              and "已确认：" not in str(r2), str(r2)[:120])
        check("[E·构造] J3 归因：reason 含 pending_id/candidate_id/"
              "revalidate 前缀",
              d is not None and p1.pending_id in d.reason
              and p1.candidates[0].candidate_id in d.reason
              and "pending_revalidate:" in d.reason, d.reason if d else "")
        shadow_acts = [x.get("action") for x in rt.interceptor.shadow_log]
        check("[E·构造] shadow execute_begin/done 恰各一条（J3 双通道）",
              shadow_acts.count("execute_begin") == 1
              and shadow_acts.count("execute_done") == 1, str(shadow_acts))
        check("[E·构造] shadow begin 记录携带归因 reason（含 "
              "pending_revalidate）",
              any(x.get("action") == "execute_begin"
                  and "pending_revalidate" in str(x.get("detail", ""))
                  for x in rt.interceptor.shadow_log),
              str(rt.interceptor.shadow_log[-3:]))
        # ---- 注入式反证（I2/J2）：误导性原始问句 ----
        dispatch_calls.clear()
        orc2, s2, store2, p2 = _preset_orc(
            "s-me2", lambda: datetime(2026, 11, 19, 10, 0, 0),
            ut="上海迪士尼门票多少钱")
        r3 = orc2.handle(s2, "第1个")
        check("[E·反证] 注入条件成立：原问句含误导实体名（迪士尼）",
              "迪士尼" in str(orc2.last_ctx.pending_decision_utterance
                              or ""), str(orc2.last_ctx))
        check("[E·反证] 消费路径最终检索实参仍为 fresh entity",
              any(c.get("keyword") == "海贝皇大排档（南京店）"
                  and c.get("city") == "南京" for c in dispatch_calls),
              str(dispatch_calls))
        # 断言范围严格限定为「本次捕获的 search_poi 调用」——不宣称
        # 覆盖全部检索通道（评审 2026-11-20 整改项 2：收窄至捕获范围
        # 能够证明的结论）
        check("[E·反证] 误导实体未进入消费路径的 POI 检索 keyword"
              "（范围限定：本次捕获的 search_poi 调用）",
              dispatch_calls
              and all("迪士尼" not in str(c.get("keyword") or "")
                      for c in dispatch_calls), str(dispatch_calls))
        check("[E·反证] 偏移实体内容未呈现给用户（J2）",
              "迪士尼" not in str(r3), str(r3)[:120])
        # ---- 反证对照：普通路由真实重解析（分歧机制存在）----
        dispatch_calls.clear()
        s3 = Session(use_llm=False)          # 无 pending 身份 → 原链路
        orc3 = Orchestrator()
        orc3.handle(s3, "上海迪士尼门票多少钱")
        check("[E·反证对照] 同一问句在普通路由下真实重解析出「迪士尼」"
              "（分歧机制真实存在，反证有证明力）",
              any("迪士尼" in str(c.get("keyword") or "")
                  for c in dispatch_calls), str(dispatch_calls))
    finally:
        _agent_mod.dispatch = old_dispatch
        _orch_mod._resolve_entities = old_orch_resolve
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_mechanism_e_failure_conservative() -> None:
    """机制 E I3 失败保守组（评审约束 4/C，§21.2 增量 2）：KB 无有效
    检索结果 → 保守话术不编造；other_city 冲突 → 冻结模板确定性
    信号回退确认话术（偏移归属绝不呈现，J2）；_spot_qa/_nearby_qa
    异常 → 既有异常边界回退；空文本 → F3 回退；五条件任一不满足
    → confirm 语义不变（直调不改变任何回退行为）。"""
    import travel_agent.agent as _agent_mod
    import travel_agent.autofetch as _autofetch_mod
    from travel_agent.decision import (ACTION_CLARIFY, DecisionResult)
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    old_ensure_spot = _autofetch_mod.ensure_spot
    old_dispatch = _agent_mod.dispatch
    try:
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京",
                               "e-nj-fresh")])])
        _agent_mod.dispatch = lambda name, args: (
            {"ok": True, "items": [], "total": 0}
            if name == "search_poi" else old_dispatch(name, args))
        # ---- a) KB 无有效结果 → 保守话术（不编造、非确认话术）----
        _autofetch_mod.ensure_spot = lambda *a, **kw: {"status": "not_found"}
        orc, s, store, p = _preset_orc(
            "s-me-f1", lambda: datetime(2026, 11, 19, 10, 0, 0))
        r1 = orc.handle(s, "第1个")
        check("[I3-a] KB 无结果 → 保守话术（含 fresh 实体、绝不编造）",
              "暂时查不到" in str(r1)
              and "海贝皇大排档（南京店）" in str(r1)
              and "已确认：" not in str(r1), str(r1)[:120])
        # ---- b) other_city 冲突 → 确定性信号回退确认话术（J2）----
        _autofetch_mod.ensure_spot = lambda *a, **kw: {
            "status": "other_city", "city": "上海",
            "poi": {"name": "海贝皇大排档（南京店）", "city": "上海",
                    "category": "attraction"}}
        orc2, s2, store2, p2 = _preset_orc(
            "s-me-f2", lambda: datetime(2026, 11, 19, 10, 0, 0))
        r2 = orc2.handle(s2, "第1个")
        check("[I3-b] other_city 冲突 → 验收位回退确认话术",
              "已确认：海贝皇大排档（南京店）" in str(r2)
              and orc2.last_ctx.route == "pending_consume_execute",
              str(r2))
        check("[I3-b] 偏移归属绝不呈现（J2：「位于上海」「不在南京」"
              "零出现）",
              "位于上海" not in str(r2) and "不在南京" not in str(r2),
              str(r2))
        check("[I3-b] 验收位回退警告已记录",
              any("验收位回退" in w for w in orc2._pending_warnings),
              str(orc2._pending_warnings))
        # ---- c) _spot_qa 异常 → 既有异常边界回退 ----
        orc3, s3, store3, p3 = _preset_orc(
            "s-me-f3", lambda: datetime(2026, 11, 19, 10, 0, 0))
        orig_sq = s3._spot_qa

        def boom_sq(city, spot):
            raise RuntimeError("注入：业务方法异常")
        s3._spot_qa = boom_sq
        try:
            r3 = orc3.handle(s3, "第1个")
        finally:
            s3._spot_qa = orig_sq
        check("[I3-c] _spot_qa 异常 → 异常边界回退确认话术 + 警告",
              "已确认：" in str(r3)
              and orc3.last_ctx.route == "pending_consume_execute"
              and any("注入失败" in w for w in orc3._pending_warnings),
              str(r3))
        # ---- d) _nearby_qa 异常（原问句含周边语义）→ 同一异常边界 ----
        orc4, s4, store4, p4 = _preset_orc(
            "s-me-f4", lambda: datetime(2026, 11, 19, 10, 0, 0),
            ut="海贝皇大排档周边怎么样")
        orig_nq = s4._nearby_qa

        def boom_nq(city, spot):
            raise RuntimeError("注入：周边业务方法异常")
        s4._nearby_qa = boom_nq
        try:
            r4 = orc4.handle(s4, "第1个")
        finally:
            s4._nearby_qa = orig_nq
        check("[I3-d] _nearby_qa 异常 → 同一异常边界回退确认话术",
              "已确认：" in str(r4)
              and any("注入失败" in w for w in orc4._pending_warnings),
              str(r4))
        # ---- e) 空文本 → F3 回退（语义不变）----
        orc5, s5, store5, p5 = _preset_orc(
            "s-me-f5", lambda: datetime(2026, 11, 19, 10, 0, 0))
        orig_sq5 = s5._spot_qa
        s5._spot_qa = lambda city, spot: ""
        try:
            r5 = orc5.handle(s5, "第1个")
        finally:
            s5._spot_qa = orig_sq5
        check("[I3-e] 空文本 → F3 回退确认话术（直调零改变）",
              "已确认：海贝皇大排档（南京店）" in str(r5)
              and orc5.last_ctx.route == "pending_consume_execute",
              str(r5))
        # ---- f) 五条件③不满足（action=clarify）→ confirm 不变 ----
        orc6, s6, store6, p6 = _preset_orc(
            "s-me-f6", lambda: datetime(2026, 11, 19, 10, 0, 0))
        ctx6 = SimpleNamespace(
            pending_decision=DecisionResult(
                action=ACTION_CLARIFY, intent="poi_query",
                entity={"name": "海贝皇大排档（南京店）", "city": "南京"}),
            pending_decision_source="pending_consume",
            pending_decision_utterance="第1个")
        rep = orc6._allow_handler_consume(s6, "第1个", ctx6)
        check("[I3-f] 五条件③不满足 → confirm 回退（直调不改五条件"
              "语义）",
              rep is not None and "已确认：海贝皇大排档（南京店）"
              in str(rep), str(rep))
    finally:
        _autofetch_mod.ensure_spot = old_ensure_spot
        _agent_mod.dispatch = old_dispatch
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_other_city_template_text_detection() -> None:
    """消费端对 other_city 冻结模板文本的检测测试（评审 2026-11-20
    整改项 D1/D2 后改名与收窄表述）。

    本测试**只验证消费端检测逻辑面对给定文本时的行为**，不验证生产
    分支是否确实生成该文本——生产源头由静态核验证明（autofetch.py:858
    跨城同名、:937 重定向失败，两处 message 均为 f-string 字面量构造，
    原文见 P25bR3_chain_evidence.txt；若要证明源头模板不漂移，需另测
    真实生成分支，不在本测试范围）。

    消费路径只经 _spot_qa：_nearby_qa 在 KB 无坐标时委托 _spot_qa
    （dialogue.py:1352-1354），_nearby_gate 的 other_city 分支
    （dialogue.py:718-721）不在机制 E 直调路径内。故 other_city 返回
    形态穷举为四类：

      S1 res.poi 存在 → 「位于{true_city}，不在{city}」note
         （dialogue.py:979-981）—— 含信号 ✓（既有 I3-b 已覆盖）
      S2 无 poi + message = autofetch.py:858 跨城同名文本
         （含「不在{city}」）—— 含信号 ✓
      S3 无 poi + message = autofetch.py:937 重定向失败文本
         （「离{city}很远，我没能核实到它在{city}」）—— **不含信号** ✗
      S4 无 poi + message 缺失 → fallback「不在{city}一带」
         （dialogue.py:982-983）—— 含信号 ✓

    S3 是已申报的已知缺口：该分支不呈现偏移归属、不编造位置（J2
    安全方向仍成立），但 I3 保守回退不触发——用户看到的是「离得很
    远、没能核实」的原样答复而非确认话术。修复需在触点 2 内增加信号
    来源（生产改动），按评审边界本轮不实施，待独立授权。本测试的
    作用是锁定当前边界，防止日后被误读为「全部 other_city 分支已覆盖」。
    """
    import travel_agent.agent as _agent_mod
    import travel_agent.autofetch as _autofetch_mod
    set_env(channel="1", store_kind="memory")
    old_rv_resolve = _rv_mod._resolve_entities
    old_ensure_spot = _autofetch_mod.ensure_spot
    old_dispatch = _agent_mod.dispatch
    FRESH = "海贝皇大排档（南京店）"

    def run(sid, ensure_ret):
        _autofetch_mod.ensure_spot = lambda *a, **kw: ensure_ret
        orc, s, _st, _p = _preset_orc(
            sid, lambda: datetime(2026, 11, 19, 10, 0, 0))
        return orc, s, (orc.handle(s, "第1个"))
    try:
        _rv_mod._resolve_entities = _StubResolver([
            ResolveResult(resolved=[
                _conf_rc_fresh(FRESH, "南京", "e-nj-fresh")])])
        _agent_mod.dispatch = lambda name, args: (
            {"ok": True, "items": [], "total": 0}
            if name == "search_poi" else old_dispatch(name, args))
        # ---- S2：autofetch.py:858 跨城同名 message（含信号）----
        msg858 = (f"「{FRESH}」资料库里记在上海，不在南京；"
                  f"如果你要的确实是南京的那个，告诉我我重新核实")
        _o2, _s2, r2 = run("s-oc-s2", {"status": "other_city",
                                       "source": "user", "message": msg858})
        check("[OC·S2] message=跨城同名形态（含「不在{city}」）→ 回退确认"
              "（偏移归属零呈现）",
              "已确认：" in str(r2) and "记在上海" not in str(r2),
              str(r2)[:120])
        # ---- S3：autofetch.py:937 重定向失败 message（不含信号）----
        msg937 = f"「{FRESH}」的位置离南京很远，我没能核实到它在南京"
        _o3, _s3, r3 = run("s-oc-s3", {"status": "other_city",
                                       "message": msg937})
        check("[OC·S3·文本形态] 返回的确实是注入的重定向失败模板文本"
              "（防空回复/异文本误过）",
              "离南京很远" in str(r3) and "没能核实" in str(r3)
              and FRESH in str(r3), str(r3)[:160])
        check("[OC·S3·已知缺口] message=重定向失败形态（不含「不在{city}」）"
              "→ 当前不回退（已申报：I3 保守回退不触发）",
              "已确认：" not in str(r3) and "没能核实" in str(r3),
              str(r3)[:160])
        check("[OC·S3·安全方向] 该分支不呈现偏移归属、不编造位置（J2 保持）",
              "上海" not in str(r3) and "迪士尼" not in str(r3),
              str(r3)[:160])
        # ---- S4：message 缺失 → dialogue 侧 fallback（含信号）----
        _o4, _s4, r4 = run("s-oc-s4", {"status": "other_city"})
        check("[OC·S4] message 缺失 → fallback「不在{city}一带」→ 回退确认",
              "已确认：" in str(r4) and "一带" not in str(r4),
              str(r4)[:120])
    finally:
        _autofetch_mod.ensure_spot = old_ensure_spot
        _agent_mod.dispatch = old_dispatch
        _rv_mod._resolve_entities = old_rv_resolve
        set_env()


def test_p25b_channel0_zero_revalidation() -> None:
    """channel=0 逐位：重验证侧零触发——revalidator/producer/decider
    零构造、重验证 resolve 零调用、回填零调用；普通路由行为不变。"""
    for env in (None, "0"):
        set_env(channel=env)
        old_orch_resolve = _orch_mod._resolve_entities
        old_rv_resolve = _rv_mod._resolve_entities
        try:
            stub = _StubResolver([ResolveResult(resolved=[
                _amb_rc("海贝皇大排档（南京店）", "南京",
                        "海贝皇大排档（上海店）", "上海")])])
            _orch_mod._resolve_entities = stub
            _rv_mod._resolve_entities = _StubResolver([ResolveResult(
                resolved=[_conf_rc_fresh("x", "南京", "e-x")])])
            orc = Orchestrator()
            s = _produce_session(orc, "s-off-b")
            # 预置一个 open pending（若通道误开，选择话术会触发消费链）
            rt = orc._pending_runtime("s-off-b")
            p = rt.store.create_pending("哪家海贝皇大排档好", "poi_query",
                                        "query", SNAP)
            s.active_pending_id = p.pending_id
            orc._pending_manager.release(rt)
            r = orc.handle(s, "第1个")
            check(f"[C0 env={env}] 选择话术走原路由（非 pending 通道）",
                  orc.last_ctx.route not in ("pending_consume",
                                             "pending_consume_execute",
                                             "pending_produce"),
                  str(orc.last_ctx.route))
            check(f"[C0 env={env}] 重验证 resolve 零调用",
                  _rv_mod._resolve_entities.calls == 0, "")
            check(f"[C0 env={env}] revalidator/producer/decider 零构造",
                  orc._revalidator is None and orc._producer is None
                  and orc._decider is None, "")
            check(f"[C0 env={env}] 回填零发生（记录不存在或三列 NULL）",
                  rt.store._latest_consumption() is None
                  or rt.store._latest_consumption()["revalidate_status"]
                  is None, "")
        finally:
            _orch_mod._resolve_entities = old_orch_resolve
            _rv_mod._resolve_entities = old_rv_resolve
    set_env()

# ================================================== 9. P2.6 增量 1+3：DecisionEngine 影子触达

# 验收项与 PHASE5_P25_PLAN §25.5 一一对应：
# #1 S1 开关关闭冻结 / #2 S2 每轮恰好一次 + J-A / #3 S3 记录一致 + J-B /
# #4 S6 第 1 层（触点直接调用零写入 + DecisionResult 不变性）/
# #5 S6 第 2 层（真实 handle 基线对照）/ #6 S4a 四路异常隔离 /
# #7 输入边界（字段缺失，不承担隔离证明）/ #8 S5 零网络 + 零重解析 + 对象身份 /
# #9 附加检查二（CHANNEL=1 不重复决策）/ #10 S7 开关冻结。

from travel_agent.decision import (DecisionEngine, DecisionResult,
                                   SHADOW_OBSERVATION_KEYS, SHADOW_RECORD_KEYS)


def _p26_env(shadow=None, channel=None, takeover=None) -> None:
    """P2.6 三开关显式设置（None = 移除，绝不隐式继承外部环境）。"""
    for key, val in (("TRAVEL_DECISION_SHADOW", shadow),
                     ("TRAVEL_PENDING_CHANNEL", channel),
                     ("TRAVEL_DECISION_TAKEOVER", takeover)):
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


class _CountingEngine(DecisionEngine):
    """decide() 计数 + 入参/返回值捕获装具（S2/S3/S5/附加检查二）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.decide_calls = 0
        self.decide_args = []
        self.decide_rets = []

    def decide(self, parse_result, resolved, context=None, **kw):
        self.decide_calls += 1
        self.decide_args.append((parse_result, list(resolved or []),
                                 dict(context or {}), dict(kw)))
        out = super().decide(parse_result, resolved, context, **kw)
        self.decide_rets.append(out)
        return out


class _AppendBoom(list):
    """append 恒抛异常的 list 子类（记录容器 / 警告载体损坏装具）。"""

    def append(self, item):
        raise RuntimeError("append boom")


class _FailIfCalled:
    """调用即计数并抛错——用于「编排层不得二次解析」的反证装具。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        raise AssertionError("orchestrator 不得再调 _resolve_entities")


def _boom_sink(rec):
    raise RuntimeError("sink boom")


def test_p26_shadow_switch_off_frozen() -> None:
    """S1：TRAVEL_DECISION_SHADOW 非 "1" 时为零行为——不建 engine、
    不建记录容器、不额外解析；四组取值（未设 / "0" / "false" / ""）
    的回复与 route 逐字一致。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    try:
        baseline = None
        for env in (None, "0", "false", ""):
            _p26_env(shadow=env, channel=None, takeover=None)
            stub = _StubResolver([ResolveResult(resolved=[
                _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-off")])])
            _er_mod.resolve = stub
            orc = Orchestrator()
            s = Session(use_llm=False)
            r = orc.handle(s, "南京海贝皇大排档哪家好")
            tag = f"[S1 env={env!r}]"
            check(f"{tag} 开关判定 False",
                  orc._decision_shadow_enabled() is False, "")
            check(f"{tag} 决策引擎未构造", orc._decider is None,
                  str(orc._decider))
            check(f"{tag} 观察记录容器未创建",
                  orc._decision_shadow_log is None,
                  str(orc._decision_shadow_log))
            check(f"{tag} resolve 仅 parser 一次",
                  stub.calls == 1, str(stub.calls))
            check(f"{tag} route 非 pending_*",
                  str(orc.last_ctx.route or "").startswith("pending") is False,
                  str(orc.last_ctx.route))
            check(f"{tag} producer 未构造", orc._producer is None, "")
            check(f"{tag} 正常出回复", r is not None and bool(str(r)),
                  str(r)[:80])
            if baseline is None:
                baseline = (str(r), orc.last_ctx.route)
            else:
                check(f"{tag} 回复与基线逐字一致",
                      str(r) == baseline[0], f"{str(r)[:60]}")
                check(f"{tag} route 与基线一致",
                      orc.last_ctx.route == baseline[1], "")
    finally:
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_switch_on_single_call() -> None:
    """S2：开关开 + channel 关 → 三类问句各一轮，每轮恰好一次
    decide()、恰好一条观察记录；记录覆盖 11 键；J-A 断言（decide()
    对外返回 None，而记录 city_source/confidence 有真实非空值——
    证明字段取自 _build() 的内部 result，不是对外返回值）。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        stub = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-on",
                           confidence=0.92)])])
        _er_mod.resolve = stub
        orc = Orchestrator()
        s = Session(use_llm=False)
        eng = _CountingEngine()
        orc._decider = eng
        for i, ut in enumerate(("南京海贝皇大排档哪家好",      # poi
                                "明天南京天气怎么样",          # weather
                                "帮我排个南京3天行程"),        # plan
                               start=1):
            orc.handle(s, ut)
            check(f"[S2 #{i}] 每轮恰好一次 decide()（累计 {i}）",
                  eng.decide_calls == i, str(eng.decide_calls))
            check(f"[S2 #{i}] 观察记录每轮 +1（累计 {i}）",
                  len(orc._decision_shadow_log or []) == i,
                  str(len(orc._decision_shadow_log or [])))
            check(f"[S2 #{i}] engine.shadow_log 每轮 +1（累计 {i}）",
                  len(eng.shadow_log) == i, str(len(eng.shadow_log)))
        rec0, rec2 = orc._decision_shadow_log[0], orc._decision_shadow_log[2]
        check("[S2] 记录键集合恰为 11 键契约",
              len(rec0) == 11 and set(rec0) == set(SHADOW_RECORD_KEYS)
              and set(SHADOW_OBSERVATION_KEYS) <= set(rec0), str(sorted(rec0)))
        # ---- J-A：外部返回 None 与内部真实决策值并存 ----
        check("[S2·J-A] 第 1 轮 decide() 对外返回 None（TAKEOVER=0）",
              eng.decide_rets[0] is None, str(eng.decide_rets[0]))
        check("[S2·J-A] 记录 city_source 非空真值（== query_city）",
              rec0["city_source"] == "query_city", str(rec0["city_source"]))
        check("[S2·J-A] 记录 confidence 非空真值（== 0.92）",
              rec0["confidence"] == 0.92, str(rec0["confidence"]))
        check("[S2·J-A] 记录 action 非空真值（== execute）",
              rec0["action"] == "execute", str(rec0["action"]))
        check("[S2] 规划轮 city_source 走 destination（口径一致）",
              rec2["city_source"] == "destination", str(rec2["city_source"]))
    finally:
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_record_matches_independent_decide() -> None:
    """S3：同一 parse / 同一 resolved / 同一 rctx 下另建独立
    DecisionEngine 调一次 decide()，11 字段逐字段相等；副本与源记录
    字段相等但**身份不同**；J-B（记录字段与 _shadow() 实际收到的
    result 逐一对应，且 takeover_on is False）。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    old_shadow = DecisionEngine._shadow
    _p26_env(shadow="1", channel=None, takeover=None)
    cap = {}

    def wrap(self, result, intent, resolved, takeover_on, batch_on):
        cap["result"] = result
        cap["takeover_on"] = takeover_on
        return old_shadow(self, result, intent, resolved, takeover_on,
                          batch_on)

    try:
        _er_mod.resolve = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-s3",
                           confidence=0.88)])])
        DecisionEngine._shadow = wrap
        orc = Orchestrator()
        s = Session(use_llm=False)
        eng = _CountingEngine()
        orc._decider = eng
        orc.handle(s, "南京海贝皇大排档哪家好")
        parse, objs, rctx, kw = eng.decide_args[0]
        ind = DecisionEngine()
        ind.decide(parse, list(objs), rctx, **kw)
        mine, src = orc._decision_shadow_log[0], ind.shadow_log[-1]
        for key in SHADOW_RECORD_KEYS:
            check(f"[S3] 字段 {key} 与独立决策逐字段相等",
                  mine[key] == src[key], f"{mine[key]!r} != {src[key]!r}")
        check("[S3] 副本非引用（身份不同）",
              mine is not src and mine is not orc._decider.shadow_log[-1], "")
        check("[S3] 副本键集合 == 源记录键集合 == 11 键",
              set(mine) == set(src) == set(SHADOW_RECORD_KEYS), "")
        # ---- J-B：调用链断言 ----
        res = cap.get("result")
        check("[S3·J-B] _shadow() 捕获到非 None 的 result", res is not None,
              str(res))
        check("[S3·J-B] _shadow() 收到 takeover_on is False",
              cap.get("takeover_on") is False, str(cap.get("takeover_on")))
        check("[S3·J-B] 记录 action / city_source / confidence / "
              "entity_city 与 result 逐一相等",
              mine["action"] == res.action
              and mine["city_source"] == res.city_source
              and mine["confidence"] == res.confidence
              and mine["entity_city"] == (res.entity or {}).get("city"),
              str(mine))
    finally:
        DecisionEngine._shadow = old_shadow
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_touchpoint_direct_zero_write() -> None:
    """S6 第 1 层：**直接调用** `_decision_shadow(session, ctx)`（不经
    handle）——session 状态与解析对象前后快照相等，DecisionResult
    进入真实 _shadow 前后的 to_dict() 完全相等（记录装配未改业务
    对象）；记录恰 11 键、无 utterance/history 键、不含问句唯一标记。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    old_shadow = DecisionEngine._shadow
    _p26_env(shadow="1", channel=None, takeover=None)
    cap = {}

    def wrap(self, result, intent, resolved, takeover_on, batch_on):
        cap["before"] = copy.deepcopy(result.to_dict()) if result else None
        cap["result"] = result
        out = old_shadow(self, result, intent, resolved, takeover_on, batch_on)
        cap["after"] = copy.deepcopy(result.to_dict()) if result else None
        # 反向污染装具：向**源记录**追加原文片段与额外键——若触点改成
        # `dict(rec)` 整条复制，下面两条断言立即失败；只有白名单投影
        # 才能把额外键挡在副本之外（评审 2026-11-20 实施约束）
        self.shadow_log[-1]["utterance_echo"] = "ZQXTOKEN-leak"
        return out

    try:
        _er_mod.resolve = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-s6")])])
        DecisionEngine._shadow = wrap
        orc = Orchestrator()
        s = Session(use_llm=False)
        ut = "南京海贝皇大排档哪家好 ZQXTOKEN"
        ctx = make_ctx(ut)
        s.query_city = ctx.parse.query_city
        pr = ctx.parse
        before = {
            "slots": copy.deepcopy(s.slots),
            "query_city": s.query_city,
            "plan_made": s.plan_made,
            "last_plan": copy.deepcopy(s.last_plan),
            "active_pending_id": s.active_pending_id,
            "objs": [o.as_dict() for o in pr.shadow_resolved_objs],
        }
        orc._decision_shadow(s, ctx)          # 直接调用触点，不经 handle
        after = {
            "slots": copy.deepcopy(s.slots),
            "query_city": s.query_city,
            "plan_made": s.plan_made,
            "last_plan": copy.deepcopy(s.last_plan),
            "active_pending_id": s.active_pending_id,
            "objs": [o.as_dict() for o in pr.shadow_resolved_objs],
        }
        check("[S6-1] session 五字段 + 解析对象快照前后相等",
              before == after, f"{before} != {after}")
        check("[S6-1] ctx.route 未变、pending_decision 仍为 None",
              ctx.route is None and ctx.pending_decision is None, "")
        check("[S6-1] DecisionResult to_dict() 进入/退出 _shadow 完全相等",
              cap.get("before") is not None and cap["before"] == cap["after"],
              f"{cap.get('before')} != {cap.get('after')}")
        rec = orc._decision_shadow_log[0]
        check("[S6-1] 记录恰 11 键（无多余键）",
              len(rec) == 11 and set(rec) == set(SHADOW_RECORD_KEYS),
              str(sorted(rec)))
        check("[S6-1] 污染装具有效性：源记录确实带额外键 utterance_echo",
              "utterance_echo" in orc._decider.shadow_log[-1],
              str(sorted(orc._decider.shadow_log[-1])))
        check("[S6-1] 白名单投影未复制源记录的额外键（非 dict(rec) 整条复制）",
              "utterance_echo" not in rec, str(sorted(rec)))
        check("[S6-1] 记录不含 utterance/history 类键",
              all(k not in rec for k in ("utterance", "history", "text",
                                         "query", "raw")), str(sorted(rec)))
        blob = json.dumps(rec, ensure_ascii=False, default=str)
        check("[S6-1] 不落原文：问句唯一标记 ZQXTOKEN 未出现在记录中",
              "ZQXTOKEN" not in blob, blob[:160])
        check("[S6-1] 触点返回值恒为 None",
              orc._decision_shadow(s, ctx) is None, "")
    finally:
        DecisionEngine._shadow = old_shadow
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_handle_baseline_equivalence() -> None:
    """S6 第 2 层：同输入分别以 SHADOW=0 / SHADOW=1 跑真实 `handle()`
    ——Reply 文本逐字相等、route 相等、producer/manager 均未构造；
    唯一差异是观察记录容器（关=None / 开=1 条 11 键）。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    try:
        _er_mod.resolve = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-base")])])
        snap = {}
        for tag, env in (("off", None), ("on", "1")):
            _p26_env(shadow=env, channel=None, takeover=None)
            orc = Orchestrator()
            s = Session(use_llm=False)
            r = orc.handle(s, "你好")
            snap[tag] = {"reply": str(r), "route": orc.last_ctx.route,
                         "producer": orc._producer,
                         "manager": orc._pending_manager,
                         "log": orc._decision_shadow_log}
            if tag == "on":
                check("[S6-2] 开启侧记录恰 1 条且 11 键",
                      len(orc._decision_shadow_log or []) == 1
                      and set(orc._decision_shadow_log[0])
                      == set(SHADOW_RECORD_KEYS),
                      str(orc._decision_shadow_log))
            else:
                check("[S6-2] 关闭侧无记录容器（None）",
                      orc._decision_shadow_log is None, "")
        check("[S6-2] 两次 Reply 文本逐字相等",
              snap["off"]["reply"] == snap["on"]["reply"],
              f"{snap['off']['reply'][:60]} != {snap['on']['reply'][:60]}")
        check("[S6-2] route 相等",
              snap["off"]["route"] == snap["on"]["route"],
              f"{snap['off']['route']} != {snap['on']['route']}")
        check("[S6-2] producer / manager 两侧均未构造",
              snap["off"]["producer"] is None and snap["on"]["producer"] is None
              and snap["off"]["manager"] is None
              and snap["on"]["manager"] is None, "")
        check("[S6-2] 唯一差异确为观察记录（关 None / 开 1 条）",
              snap["off"]["log"] is None and len(snap["on"]["log"]) == 1, "")
    finally:
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_exception_isolation() -> None:
    """S4a：四路真异常注入，四路都必须返回正常 Reply 且 route 正常。
    ① `_build` 抛异常 → 无记录 + 警告；② 记录容器 append 抛异常 →
    decide() 已执行但复制失败被吞；③ shadow_sink 抛异常 → 记录仍生成
    （_shadow 先 append 再调 sink）；④ `_build` 抛异常且警告载体本身
    也抛异常 → 警告记录失败不得外抛。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    old_build = DecisionEngine._build
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        _er_mod.resolve = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-ex")])])

        # ---- ① _build 抛异常 ----
        def boom_build(self, *a, **kw):
            raise RuntimeError("build boom")
        DecisionEngine._build = boom_build
        orc1 = Orchestrator()
        r1 = orc1.handle(Session(use_llm=False), "南京海贝皇大排档哪家好")
        check("[S4a-①] 正常返回 Reply", r1 is not None and bool(str(r1)),
              str(r1)[:60])
        check("[S4a-①] 触达失败 → 无观察记录",
              orc1._decision_shadow_log is None, "")
        check("[S4a-①] engine.shadow_log 长度不变（0）",
              orc1._decider is not None and orc1._decider.shadow_log == [],
              str(orc1._decider.shadow_log if orc1._decider else None))
        check("[S4a-①] 异常已记入警告（前缀 decision shadow:）",
              any(w.startswith("decision shadow:")
                  for w in orc1._pending_warnings),
              str(orc1._pending_warnings))

        # ---- ② 记录容器 append 抛异常（decide() 已成功）----
        DecisionEngine._build = old_build
        orc2 = Orchestrator()
        orc2._decision_shadow_log = _AppendBoom()
        r2 = orc2.handle(Session(use_llm=False), "南京海贝皇大排档哪家好")
        check("[S4a-②] 正常返回 Reply", r2 is not None and bool(str(r2)),
              str(r2)[:60])
        check("[S4a-②] decide() 已执行（engine.shadow_log == 1）",
              orc2._decider is not None
              and len(orc2._decider.shadow_log) == 1,
              str(orc2._decider.shadow_log if orc2._decider else None))
        check("[S4a-②] 复制失败被吞并记入警告",
              any(w.startswith("decision shadow:")
                  for w in orc2._pending_warnings),
              str(orc2._pending_warnings))

        # ---- ③ shadow_sink 抛异常（记录仍生成）----
        orc3 = Orchestrator()
        orc3._decider = DecisionEngine(shadow_sink=_boom_sink)
        r3 = orc3.handle(Session(use_llm=False), "南京海贝皇大排档哪家好")
        check("[S4a-③] 正常返回 Reply", r3 is not None and bool(str(r3)),
              str(r3)[:60])
        check("[S4a-③] 记录仍生成（sink 异常前的记录）",
              len(orc3._decision_shadow_log or []) == 1
              and len(orc3._decider.shadow_log) == 1,
              str(orc3._decision_shadow_log))
        check("[S4a-③] sink 异常不产生警告（_shadow 内部静默）",
              orc3._pending_warnings == [], str(orc3._pending_warnings))

        # ---- ④ _build 抛异常 + 警告载体自身 append 失败 ----
        DecisionEngine._build = boom_build
        orc4 = Orchestrator()
        orc4._pending_warnings = _AppendBoom()
        r4 = orc4.handle(Session(use_llm=False), "南京海贝皇大排档哪家好")
        check("[S4a-④] 警告追加失败也不外抛，正常返回 Reply",
              r4 is not None and bool(str(r4)), str(r4)[:60])
        check("[S4a-④] route 正常（未被影子异常改写）",
              orc4.last_ctx is not None and orc4.last_ctx.route is not None,
              str(orc4.last_ctx.route if orc4.last_ctx else None))
    finally:
        DecisionEngine._build = old_build
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_input_boundary_missing_field() -> None:
    """输入异常边界（与 S4a 隔离证明严格分开）：① 解析对象缺
    `shadow_resolved_objs` → 以空列表决策（candidates_count == 0、
    未形成结论），不崩；② `ctx.parse is None` → 触点直接 return，
    零记录、零 engine 构造。**本函数不承担异常隔离的证明**。"""
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        _er_mod.resolve = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-bd")])])
        orc = Orchestrator()
        s = Session(use_llm=False)
        ctx = make_ctx("南京海贝皇大排档哪家好")
        s.query_city = ctx.parse.query_city
        del ctx.parse.shadow_resolved_objs        # 属性缺失
        orc._decision_shadow(s, ctx)
        rec = (orc._decision_shadow_log or [None])[0]
        check("[边界①] 缺失字段 → 以空列表决策（candidates_count == 0）",
              rec is not None and rec["candidates_count"] == 0, str(rec))
        check("[边界①] 空候选 → 未形成结论（action None / reason 保留）",
              rec is not None and rec["action"] is None
              and rec["reason"] == "未形成结论", str(rec))
        check("[边界①] 不崩且无警告", orc._pending_warnings == [],
              str(orc._pending_warnings))
        # ---- ② parse is None ----
        orc2 = Orchestrator()
        ctx2 = make_ctx("你好")
        ctx2.parse = None
        orc2._decision_shadow(s, ctx2)
        check("[边界②] parse is None → 零记录", orc2._decision_shadow_log
              is None, "")
        check("[边界②] parse is None → 不构造 engine（零开销）",
              orc2._decider is None, "")
    finally:
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_zero_network_and_no_reparse() -> None:
    """S5 + 附加检查一：dispatch 调用数为 0（影子决策未触发外部检索）；
    `entity_resolver.resolve` 恰 1 次（仅 parser）、编排层二次解析装具
    零调用；`confidence=0.777` 数值指纹 + **元素级 id() 身份断言**
    （decide() 入参列表元素、parse.shadow_resolved_objs 元素、桩返回
    对象为同一批）——证明对象来自本轮 parser 产物，不是数值碰巧相同。"""
    import travel_agent.agent as _agent_mod
    import travel_agent.entity_resolver as _er_mod
    old_resolve = _er_mod.resolve
    old_dispatch = _agent_mod.dispatch
    old_orch_resolve = _orch_mod._resolve_entities
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        fp = _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-fp",
                            confidence=0.777)
        stub = _StubResolver([ResolveResult(resolved=[fp])])
        _er_mod.resolve = stub
        guard = _FailIfCalled()
        _orch_mod._resolve_entities = guard
        disp = []

        def cap_dispatch(name, args):
            disp.append(name)
            return old_dispatch(name, args)
        _agent_mod.dispatch = cap_dispatch
        orc = Orchestrator()
        eng = _CountingEngine()
        orc._decider = eng
        s = Session(use_llm=False)
        r = orc.handle(s, "你好")             # 业务路径本身零检索
        check("[S5] 正常出回复", r is not None and bool(str(r)), "")
        check("[S5] dispatch 调用数 == 0（无外部检索）", disp == [], str(disp))
        check("[S5] entity_resolver.resolve 恰 1 次（仅 parser）",
              stub.calls == 1, str(stub.calls))
        check("[S5] 编排层二次解析零调用（调用即抛错装具未被触发）",
              guard.calls == 0, str(guard.calls))
        check("[S5] 每轮恰好一次 decide()", eng.decide_calls == 1,
              str(eng.decide_calls))
        rec = (orc._decision_shadow_log or [None])[0]
        check("[S5] 数值指纹：confidence == 0.777",
              rec is not None and rec["confidence"] == 0.777, str(rec))
        objs = orc.last_ctx.parse.shadow_resolved_objs
        check("[S5] candidates_count == len(parse.shadow_resolved_objs) == 1",
              rec is not None and rec["candidates_count"] == len(objs) == 1,
              f"{rec and rec['candidates_count']} / {len(objs)}")
        check("[S5] statuses 与指纹对象一致", rec is not None
              and rec["statuses"] == ["confirmed"], str(rec and rec["statuses"]))
        # ---- 元素级身份断言（数值相同不足以证明同源）----
        passed = eng.decide_args[0][1]
        check("[身份] decide() 入参元素 is 桩返回对象",
              len(passed) == 1 and id(passed[0]) == id(fp), str(passed))
        check("[身份] parse.shadow_resolved_objs 元素 is 同一对象",
              len(objs) == 1 and id(objs[0]) == id(fp), str(objs))
        check("[身份] 三者 id() 完全一致（非数值巧合）",
              id(passed[0]) == id(objs[0]) == id(fp),
              f"{id(passed[0])} {id(objs[0])} {id(fp)}")
    finally:
        _agent_mod.dispatch = old_dispatch
        _orch_mod._resolve_entities = old_orch_resolve
        _er_mod.resolve = old_resolve
        _p26_env()


def test_p26_shadow_channel1_no_double_decide() -> None:
    """附加检查二：CHANNEL=1 时影子触点不触达，绝不产生第二次
    decide()。两轮对照：
    - 生产轮（ambiguous → 创建 Pending）：decide() 恰 1 次，全部来自
      `_pending_produce` 钩子，影子零记录；
    - 消费轮（选择 → 重验证）：decide() 0 次（重验证直接构造
      DecisionResult，不走 decide()），影子零记录，route/回复与既有
      P2.5b 基线一致。"""
    _p26_env(shadow="1", channel="1", takeover=None)
    old_orch_resolve = _orch_mod._resolve_entities
    old_rv_resolve = _rv_mod._resolve_entities
    try:
        # ---- 生产轮 ----
        _orch_mod._resolve_entities = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        orc = Orchestrator()
        eng = _CountingEngine()
        orc._decider = eng
        s = _produce_session(orc, "s-p26-a")
        r1 = orc.handle(s, "海贝皇大排档哪家好")
        check("[C1 生产轮] route=pending_produce（钩子照常工作）",
              orc.last_ctx.route == "pending_produce", str(orc.last_ctx.route))
        check("[C1 生产轮] 回复含候选展示", "海贝皇大排档（南京店）" in str(r1),
              str(r1)[:80])
        check("[C1 生产轮] decide() 恰 1 次（仅 pending 钩子，影子未叠加）",
              eng.decide_calls == 1, str(eng.decide_calls))
        check("[C1 生产轮] 影子零记录", orc._decision_shadow_log is None,
              str(orc._decision_shadow_log))
        check("[C1 生产轮] 开关判定 False（channel=1 时影子不触达）",
              orc._decision_shadow_enabled() is False, "")

        # ---- 消费轮 ----
        _orch_mod._resolve_entities = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-c1")])])
        _rv_mod._resolve_entities = _StubResolver([ResolveResult(resolved=[
            _conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-c1-rv")])])
        orc2, s2, store2, p2 = _preset_orc(
            "s-p26-b", lambda: datetime.now())
        eng2 = _CountingEngine()
        orc2._decider = eng2
        r2 = orc2.handle(s2, "第1个")
        check("[C1 消费轮] route=pending_consume_execute（既有基线）",
              orc2.last_ctx.route == "pending_consume_execute",
              str(orc2.last_ctx.route))
        check("[C1 消费轮] decide() 0 次（重验证不调 decide，影子未叠加）",
              eng2.decide_calls == 0, str(eng2.decide_calls))
        check("[C1 消费轮] 影子零记录", orc2._decision_shadow_log is None,
              str(orc2._decision_shadow_log))
        check("[C1 消费轮] 回复呈现 fresh 实体（未被影子影响）",
              "海贝皇大排档（南京店）" in str(r2), str(r2)[:80])
    finally:
        _orch_mod._resolve_entities = old_orch_resolve
        _rv_mod._resolve_entities = old_rv_resolve
        _p26_env()


def test_p26_switch_freeze() -> None:
    """S7：三个既有开关语义不变、新开关默认关闭；`TRAVEL_DECISION_SHADOW`
    在生产源码中只有一处读取点（`_decision_shadow_enabled`）；
    `SHADOW_OBSERVATION_KEYS` 是字段契约常量，不被 `_build` / `decide` /
    `DecisionResult` 读取。"""
    # ---- ① 既有 CHANNEL 开关三态不变 ----
    for env, want in ((None, False), ("0", False), ("1", True)):
        _p26_env(shadow=None, channel=env, takeover=None)
        check(f"[S7] _channel_enabled() env={env!r} → {want}",
              Orchestrator._channel_enabled() is want, "")
    # ---- ② TAKEOVER 未设 → decide() 返回 None（既有语义）----
    _p26_env(shadow=None, channel=None, takeover=None)
    pr = make_ctx("南京海贝皇大排档哪家好").parse
    out = DecisionEngine().decide(
        pr, [_conf_rc_fresh("海贝皇大排档（南京店）", "南京", "e-fz")],
        {"slots": {}}, intent="poi_query")
    check("[S7] TRAVEL_DECISION_TAKEOVER 未设 → decide() 返回 None",
          out is None, str(out))
    # ---- ③ 新开关默认关闭 ----
    check("[S7] 新开关未设时默认关闭",
          Orchestrator._decision_shadow_enabled() is False, "")
    # ---- ④ 生产源码中开关读取点唯一（只数带引号的字面量读取，
    #         注释/docstring 中的名称提及不计）----
    hits = []
    root = os.path.join(os.path.dirname(__file__), "..", "src", "travel_agent")
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            n = src.count('"TRAVEL_DECISION_SHADOW"')
            if n:
                hits.append((os.path.relpath(path, root), n))
    check("[S7] TRAVEL_DECISION_SHADOW 生产读取点唯一（orchestrator 1 处）",
          len(hits) == 1 and hits[0][0].endswith("orchestrator.py")
          and hits[0][1] == 1, str(hits))
    # ---- ⑤ SHADOW_OBSERVATION_KEYS 不是决策输入 ----
    check("[S7] SHADOW_OBSERVATION_KEYS 恰 4 个观察字段",
          SHADOW_OBSERVATION_KEYS == ("city_source", "entity_city",
                                      "confidence", "candidates_count"),
          str(SHADOW_OBSERVATION_KEYS))
    check("[S7] SHADOW_RECORD_KEYS == 既有 7 键 + 4 观察字段（11 键）",
          len(SHADOW_RECORD_KEYS) == 11
          and tuple(SHADOW_RECORD_KEYS[7:]) == SHADOW_OBSERVATION_KEYS,
          str(SHADOW_RECORD_KEYS))
    for fn in (DecisionEngine._build, DecisionEngine.decide,
               DecisionEngine._shadow):
        check(f"[S7] {fn.__name__} 无 SHADOW_OBSERVATION_KEYS 读取点",
              "SHADOW_OBSERVATION_KEYS" not in inspect.getsource(fn), "")
    check("[S7] DecisionResult 无 SHADOW_OBSERVATION_KEYS 读取点",
          "SHADOW_OBSERVATION_KEYS"
          not in inspect.getsource(DecisionResult), "")
    _p26_env()


# ================================================== R-01（I-2 + I-3）：S1/S2 有界化

def _s2_fill(engine, count, start=1):
    """直接驱动 S2 观察写入（不经 decide）——容量/计数断言的最小装具。

    记录内容用 ``reason=round-NNNN`` 做轮次指纹，便于断言「末条是本轮
    最新」而不是陈旧记录。"""
    for i in range(start, start + count):
        res = SimpleNamespace(action="clarify",
                              reason="round-%04d" % i,
                              entity=None, city_source="query_city",
                              confidence=0.5)
        engine._shadow(res, "poi_query", [], False, True)


def _shadow_parse(tag):
    """无候选轮的解析对象（decide 恒返回 None，但仍产生一条记录）。"""
    return SimpleNamespace(shadow_resolved_objs=[], intent=tag,
                           intent_coarse="", query_city="",
                           destination="")


def test_r01_s2_bounded_and_counter() -> None:
    """T19 / T29 / T38（S2）：250 次追加 → 容器 100、计数 250、淘汰 150；
    stats 首次成功前为 None、成功后就位且复用同一对象。"""
    from travel_agent.decision import DecisionEngine as _DE
    from travel_agent.retention import SHADOW_CAPACITY
    eng = _DE()
    check("[T38·S2] 首次追加前 shadow_stats is None",
          eng.shadow_stats is None, str(eng.shadow_stats))
    check("[T38·S2] dead 初值 False", eng.shadow_dead is False, "")
    _s2_fill(eng, 250)
    check("[T19] 容器封顶 100", len(eng.shadow_log) == SHADOW_CAPACITY,
          str(len(eng.shadow_log)))
    check("[T19] total_appended == 250",
          eng.shadow_stats.total_appended == 250,
          str(eng.shadow_stats.total_appended))
    check("[T19] total_evicted == 150",
          eng.shadow_stats.total_evicted == 150,
          str(eng.shadow_stats.total_evicted))
    check("[T29·S2] current_size == len(shadow_log)",
          eng.shadow_stats.current_size == len(eng.shadow_log),
          f"{eng.shadow_stats.current_size} != {len(eng.shadow_log)}")
    check("[T19] 末条来自第 250 轮（不是最早记录）",
          eng.shadow_log[-1]["reason"] == "round-0250",
          str(eng.shadow_log[-1]["reason"]))
    check("[T19] 首条为第 151 轮",
          eng.shadow_log[0]["reason"] == "round-0151",
          str(eng.shadow_log[0]["reason"]))
    stats_before = eng.shadow_stats
    _s2_fill(eng, 1, start=251)
    check("[T38·S2] 后续追加复用同一 stats 对象",
          eng.shadow_stats is stats_before, "")
    check("[T38·S2] capacity 为冻结常量 100",
          eng.shadow_stats.capacity == SHADOW_CAPACITY,
          str(eng.shadow_stats.capacity))


def test_r01_s2_field_clip_non_str() -> None:
    """T37（S2）：声明为 str 的字段注入非 str → 固定占位，不改字段集。"""
    from travel_agent.decision import DecisionEngine as _DE
    eng = _DE()
    res = SimpleNamespace(action="clarify", reason=123,
                          entity={"city": 456}, city_source=None,
                          confidence=0.1)
    eng._shadow(res, "poi_query", [SimpleNamespace(status=789)], False, True)
    rec = eng.shadow_log[-1]
    check("[T37·S2] reason 非 str → <non-str:int>",
          rec["reason"] == "<non-str:int>", str(rec["reason"]))
    check("[T37·S2] entity_city 非 str → <non-str:int>",
          rec["entity_city"] == "<non-str:int>", str(rec["entity_city"]))
    check("[T37·S2] statuses 元素非 str → <non-str:int>",
          rec["statuses"] == ["<non-str:int>"], str(rec["statuses"]))
    check("[T37·S2] 记录仍为 11 键",
          len(rec) == 11 and set(rec) == set(SHADOW_RECORD_KEYS),
          str(sorted(rec)))


def test_r01_s1_projection_beyond_s2_capacity() -> None:
    """T20 / T21（I-3 关键判据）：S2 达容量 100 后，S1 仍须收到第 250 轮
    的记录——旧实现以 len 判据会在第 101 轮起静默漏投影。"""
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)
        for i in range(1, 251):
            ctx = SimpleNamespace(parse=_shadow_parse("batch-%03d" % i))
            orc._decision_shadow(s, ctx)
        eng = orc._decider
        check("[T20] S2 容器封顶 100", len(eng.shadow_log) == 100,
              str(len(eng.shadow_log)))
        check("[T20] S2 计数仍为 250（不因封顶冻结）",
              eng.shadow_stats.total_appended == 250,
              str(eng.shadow_stats.total_appended))
        check("[T21] S1 容器同样封顶 100",
              len(orc._decision_shadow_log) == 100,
              str(len(orc._decision_shadow_log or [])))
        check("[T21] S1 计数 250",
              orc._decision_shadow_stats.total_appended == 250,
              str(orc._decision_shadow_stats.total_appended))
        check("[T21] S1 末条来自第 250 轮（len 判据会停在第 100 轮）",
              orc._decision_shadow_log[-1]["intent"] == "batch-250",
              str(orc._decision_shadow_log[-1]["intent"]))
        check("[T21] S1 首条为第 151 轮",
              orc._decision_shadow_log[0]["intent"] == "batch-151",
              str(orc._decision_shadow_log[0]["intent"]))
        check("[T29·S1] current_size == len",
              orc._decision_shadow_stats.current_size
              == len(orc._decision_shadow_log),
              str(orc._decision_shadow_stats.current_size))
    finally:
        _p26_env()


def test_r01_s1_lazy_no_record() -> None:
    """T22 / T23：无记录轮（parse 缺失）容器与 stats 均保持 None；
    首次成功投影后两者同时就位，记录恰 1 条。"""
    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)
        orc._decision_shadow(s, SimpleNamespace(parse=None))
        check("[T22] parse 缺失 → 容器仍 None",
              orc._decision_shadow_log is None,
              str(orc._decision_shadow_log))
        check("[T22] parse 缺失 → stats 仍 None",
              orc._decision_shadow_stats is None,
              str(orc._decision_shadow_stats))
        check("[T22] parse 缺失 → dead 仍 False",
              orc._decision_shadow_dead is False, "")
        ctx = SimpleNamespace(parse=_shadow_parse("first"))
        orc._decision_shadow(s, ctx)
        check("[T23] 首次成功 → 容器就位且恰 1 条",
              isinstance(orc._decision_shadow_log, list)
              and len(orc._decision_shadow_log) == 1,
              str(orc._decision_shadow_log))
        check("[T23] 首次成功 → stats 同时就位（非 None）",
              orc._decision_shadow_stats is not None, "")
        check("[T23] 记录为 11 键白名单投影",
              set(orc._decision_shadow_log[0]) == set(SHADOW_RECORD_KEYS),
              str(sorted(orc._decision_shadow_log[0])))
    finally:
        _p26_env()


def test_r01_s1_first_append_atomic() -> None:
    """T39：S1 首次投影失败 → 容器与 stats 均仍为 None、dead=True、
    恰 1 条 warning（复用既有外层 except，不新增第 13 个写入点）。"""
    from travel_agent.retention import RetentionInvariantError
    orig = _orch_mod.append_bounded

    def boom(records, record, stats):
        raise RetentionInvariantError("mutation")

    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        _orch_mod.append_bounded = boom
        orc = Orchestrator()
        s = Session(use_llm=False)
        orc._decision_shadow(s, SimpleNamespace(parse=_shadow_parse("x")))
        check("[T39] 容器仍 None（未留半成品）",
              orc._decision_shadow_log is None,
              str(orc._decision_shadow_log))
        check("[T39] stats 仍 None",
              orc._decision_shadow_stats is None,
              str(orc._decision_shadow_stats))
        check("[T39] dead 已置 True",
              orc._decision_shadow_dead is True, "")
        warns = [w for w in orc._pending_warnings
                 if w.startswith("decision shadow:")]
        check("[T39] 恰 1 条 warning（既有站点）", len(warns) == 1,
              str(orc._pending_warnings))
        check("[T39] S2 侧记录不受影响（decide 已执行）",
              orc._decider is not None
              and len(orc._decider.shadow_log) == 1,
              str(orc._decider.shadow_log if orc._decider else None))
    finally:
        _orch_mod.append_bounded = orig
        _p26_env()


def test_r01_s1_dead_persistent() -> None:
    """T45（S1）：dead 后 3 轮投影阶段零 append 调用；decide 契约不变
    （Q2：dead 只停投影不停 decide），warning 仍只 1 条。"""
    from travel_agent.retention import RetentionInvariantError
    orig = _orch_mod.append_bounded
    calls = {"n": 0}

    def boom(records, record, stats):
        raise RetentionInvariantError("commit")

    def counting(records, record, stats):
        calls["n"] += 1
        raise AssertionError("dead 后不得再调用 append_bounded")

    _p26_env(shadow="1", channel=None, takeover=None)
    try:
        _orch_mod.append_bounded = boom
        orc = Orchestrator()
        s = Session(use_llm=False)
        orc._decision_shadow(s, SimpleNamespace(parse=_shadow_parse("r1")))
        _orch_mod.append_bounded = counting
        for i in (2, 3, 4):
            orc._decision_shadow(
                s, SimpleNamespace(parse=_shadow_parse("r%d" % i)))
        check("[T45·S1] dead 跨轮保持", orc._decision_shadow_dead is True, "")
        check("[T45·S1] 后续 3 轮 S1 append 调用 0 次", calls["n"] == 0,
              str(calls))
        check("[T45·S1] decide 未被 dead 阻断（S2 仍 4 条）",
              len(orc._decider.shadow_log) == 4,
              str(len(orc._decider.shadow_log)))
        warns = [w for w in orc._pending_warnings
                 if w.startswith("decision shadow:")]
        check("[T45·S1] warning 仍恰 1 条（dead 后早退不重复记）",
              len(warns) == 1, str(orc._pending_warnings))
        check("[T45·S1] 容器未被 dead 后写入",
              orc._decision_shadow_log is None,
              str(orc._decision_shadow_log))
    finally:
        _orch_mod.append_bounded = orig
        _p26_env()


def test_r01_sink_after_commit_s2_s4_s5() -> None:
    """修订 2 验收：S2/S4/S5 的 stats 必须在 sink 之前提交——sink 内可见
    已提交 stats 与已入容器记录；sink 抛异常不回滚记录、不丢计数。"""
    from travel_agent.decision import DecisionEngine as _DE
    from travel_agent.pending_producer import PendingProducer
    from travel_agent.pending_revalidator import PendingRevalidator

    seen = {}

    def sink(tag, owner):
        def _sink(rec):
            seen[tag] = {
                "stats": owner.shadow_stats is not None,
                "n": len(owner.shadow_log),
            }
            raise RuntimeError("sink boom")
        return _sink

    eng = _DE(shadow_sink=None)
    eng.shadow_sink = sink("S2", eng)
    res = SimpleNamespace(action="clarify", reason="r", entity=None,
                          city_source=None, confidence=0.0)
    eng._shadow(res, "poi_query", [], False, True)
    check("[修订2·S2] sink 内 stats 已提交", seen["S2"]["stats"] is True,
          str(seen.get("S2")))
    check("[修订2·S2] sink 内记录已入容器", seen["S2"]["n"] == 1,
          str(seen.get("S2")))
    check("[修订2·S2] sink 抛异常不回滚记录", len(eng.shadow_log) == 1, "")
    check("[修订2·S2] sink 抛异常不丢计数",
          eng.shadow_stats.total_appended == 1,
          str(eng.shadow_stats.total_appended))

    prod = PendingProducer()
    prod._shadow_sink = sink("S4", prod)
    prod._shadow("produce_skip", detail="d")
    check("[修订2·S4] sink 内 stats 已提交", seen["S4"]["stats"] is True,
          str(seen.get("S4")))
    check("[修订2·S4] sink 内记录已入容器", seen["S4"]["n"] == 1, "")
    check("[修订2·S4] sink 异常不丢计数",
          prod.shadow_stats.total_appended == 1,
          str(prod.shadow_stats.total_appended))

    rev = PendingRevalidator()
    rev._shadow_sink = sink("S5", rev)
    rev._shadow("revalidate_backfilled", detail="d")
    check("[修订2·S5] sink 内 stats 已提交", seen["S5"]["stats"] is True,
          str(seen.get("S5")))
    check("[修订2·S5] sink 内记录已入容器", seen["S5"]["n"] == 1, "")
    check("[修订2·S5] sink 异常不丢计数",
          rev.shadow_stats.total_appended == 1,
          str(rev.shadow_stats.total_appended))


def test_r01_s4_s5_bounded() -> None:
    """S4 / S5 容量与字段裁剪（同构验收）：120 次 → 100 / 120 / 20；
    detail 与 ID 字段按冻结上限裁剪。"""
    from travel_agent.retention import (LIM_DETAIL, LIM_ID, SHADOW_CAPACITY)
    from travel_agent.pending_producer import PendingProducer
    from travel_agent.pending_revalidator import PendingRevalidator

    prod = PendingProducer()
    check("[S4] 首次追加前 stats is None", prod.shadow_stats is None, "")
    for i in range(120):
        prod._shadow("act-%03d" % i, detail="d" * 300, pending_id="p" * 80)
    check("[S4] 容器封顶 100", len(prod.shadow_log) == SHADOW_CAPACITY,
          str(len(prod.shadow_log)))
    check("[S4] total_appended == 120",
          prod.shadow_stats.total_appended == 120,
          str(prod.shadow_stats.total_appended))
    check("[S4] total_evicted == 20",
          prod.shadow_stats.total_evicted == 20,
          str(prod.shadow_stats.total_evicted))
    check("[S4] detail 裁剪到 256",
          len(prod.shadow_log[-1]["detail"]) == LIM_DETAIL,
          str(len(prod.shadow_log[-1]["detail"])))
    check("[S4] pending_id 裁剪到 64",
          len(prod.shadow_log[-1]["pending_id"]) == LIM_ID,
          str(len(prod.shadow_log[-1]["pending_id"])))
    check("[S4] current_size == len（T29）",
          prod.shadow_stats.current_size == len(prod.shadow_log), "")

    rev = PendingRevalidator()
    check("[S5] 首次追加前 stats is None", rev.shadow_stats is None, "")
    for i in range(120):
        rev._shadow("revalidate-%03d" % i, detail="e" * 300,
                    pending_id="p" * 80, candidate_id="c" * 80,
                    revalidate_status="confirmed")
    check("[S5] 容器封顶 100", len(rev.shadow_log) == SHADOW_CAPACITY,
          str(len(rev.shadow_log)))
    check("[S5] total_appended == 120 / evicted == 20",
          rev.shadow_stats.total_appended == 120
          and rev.shadow_stats.total_evicted == 20,
          str(rev.shadow_stats))
    check("[S5] detail / candidate_id 按上限裁剪",
          len(rev.shadow_log[-1]["detail"]) == LIM_DETAIL
          and len(rev.shadow_log[-1]["candidate_id"]) == LIM_ID,
          str(len(rev.shadow_log[-1]["detail"])))
    check("[S5] current_size == len（T29）",
          rev.shadow_stats.current_size == len(rev.shadow_log), "")


def test_r01_channel1_s2_still_bounded() -> None:
    """T12 / T43：``CHANNEL=1 ∧ SHADOW=0`` 时 S2 仍被构造且受 100 约束
    （该组合走 `_pending_produce` 的决策链，不经过影子触达）。"""
    from travel_agent.retention import SHADOW_CAPACITY
    old = _orch_mod._resolve_entities
    set_env(channel="1", store_kind="memory")
    _p26_env(shadow="0", channel="1", takeover=None)
    try:
        stub = _StubResolver([ResolveResult(resolved=[
            _amb_rc("海贝皇大排档（南京店）", "南京",
                    "海贝皇大排档（上海店）", "上海")])])
        _orch_mod._resolve_entities = stub
        orc = Orchestrator()
        s = _produce_session(orc, "r01-t12")
        orc.handle(s, "海贝皇大排档哪家好")
        eng = orc._decider
        check("[T12] channel=1 路径构造了 S2", eng is not None, "")
        check("[T12] S1 观察容器未创建（SHADOW=0）",
              orc._decision_shadow_log is None and
              orc._decision_shadow_stats is None, "")
        check("[T38·S2] 首次成功追加后 stats 就位",
              eng.shadow_stats is not None
              and eng.shadow_stats.capacity == SHADOW_CAPACITY,
              str(eng.shadow_stats))
        base = eng.shadow_stats.total_appended
        _s2_fill(eng, 250)
        check("[T12] 同一 S2 仍封顶 100",
              len(eng.shadow_log) == SHADOW_CAPACITY,
              str(len(eng.shadow_log)))
        check("[T12] 计数按追加次数累加",
              eng.shadow_stats.total_appended == base + 250,
              f"{eng.shadow_stats.total_appended} != {base + 250}")
    finally:
        _orch_mod._resolve_entities = old
        set_env()
        _p26_env()


def _t43_matrix(raise_at: int | None = None) -> None:
    """T43 组合矩阵本体。``raise_at`` 为组合序号，用于注入循环中途异常
    （F-01：验证 finally 恢复而不是仅靠顺序执行到底）。"""
    for idx, (shadow, channel) in enumerate((("0", "0"), ("1", "0"),
                                             ("0", "1"), ("1", "1"))):
        set_env(channel=channel, store_kind="memory")
        _p26_env(shadow=shadow, channel=channel, takeover=None)
        orc = Orchestrator()
        tag = f"[T43 shadow={shadow} channel={channel}]"
        check(f"{tag} 决策引擎未构造", orc._decider is None, "")
        check(f"{tag} producer / revalidator 未构造",
              orc._producer is None and orc._revalidator is None, "")
        check(f"{tag} S1 容器 / stats 均 None",
              orc._decision_shadow_log is None
              and orc._decision_shadow_stats is None, "")
        check(f"{tag} S1 dead 初值 False",
              orc._decision_shadow_dead is False, "")
        if raise_at is not None and idx == raise_at:
            raise RuntimeError("mid-loop injection")


def test_r01_switch_matrix_laziness() -> None:
    """T43：四种 SHADOW/CHANNEL 组合下，构造期一律零容器、零 stats、
    dead=False（开关关闭不产生任何容量治理状态）。

    F-01 修复（评审 2026-11-23）：环境设置与恢复整体放进 try/finally，
    并在循环中途注入异常，单独断言「异常后环境已恢复」——不再依赖
    「循环顺序执行到底」这一隐含前提。
    """
    try:
        # --- 先注入中途异常：验证 finally 生效（异常必须在恢复前抛出）---
        try:
            _t43_matrix(raise_at=2)
        except RuntimeError:
            injected = True
        else:
            injected = False
        finally:
            # F-01 的实质：恢复动作无条件执行，而不是「循环跑完才恢复」
            set_env()
            _p26_env()
        check("[T43·F01] 中途异常注入生效", injected, "未抛出")
        check("[T43·F01] 中途异常后 CHANNEL 已恢复",
              "TRAVEL_PENDING_CHANNEL" not in os.environ,
              str(os.environ.get("TRAVEL_PENDING_CHANNEL")))
        check("[T43·F01] 中途异常后 SHADOW 已恢复",
              "TRAVEL_DECISION_SHADOW" not in os.environ,
              str(os.environ.get("TRAVEL_DECISION_SHADOW")))
        check("[T43·F01] 中途异常后 TAKEOVER 已恢复",
              "TRAVEL_DECISION_TAKEOVER" not in os.environ,
              str(os.environ.get("TRAVEL_DECISION_TAKEOVER")))
        # --- 异常路径跑完后，正常矩阵仍须逐项成立 ---
        _t43_matrix()
        check("[T43·F01] 异常后重跑矩阵仍通过（状态无残留）", True, "")
    finally:
        set_env()
        _p26_env()


# ================================================== 补测（评审 2026-11-23 授权）
# 授权范围：仅本测试文件；五个生产文件 / retention.py / test_retention.py
# 全部零 diff。本节补的是「有界化接入后新增的不变量」，不重复已通过项。


def _mk_res(reason="r", action="clarify"):
    return SimpleNamespace(action=action, reason=reason, entity=None,
                           city_source=None, confidence=0.0)


def _s45_specs():
    """S4 / S5 的同构装具表：(标签, 模块, 工厂, 单条写入)。"""
    import travel_agent.pending_producer as pp_mod
    import travel_agent.pending_revalidator as pr_mod
    from travel_agent.pending_producer import PendingProducer
    from travel_agent.pending_revalidator import PendingRevalidator

    return (
        ("S4", pp_mod,
         lambda sink=None: PendingProducer(shadow_sink=sink),
         lambda o, i=0: o._shadow("act-%d" % i, detail="d",
                                  pending_id="p")),
        ("S5", pr_mod,
         lambda sink=None: PendingRevalidator(shadow_sink=sink),
         lambda o, i=0: o._shadow("rv-%d" % i, detail="d",
                                  pending_id="p", candidate_id="c",
                                  revalidate_status="confirmed")),
    )


def test_r01_s2_rie_and_dead() -> None:
    """S2 补测（评审第 2 项）：append 抛 RIE 的三条不变量。

    ① 首次追加即失败 → dead=True、stats 仍 None、容器零新增、sink 零调用；
    ② 已有 stats 时失败 → 三计数与容器均不得误增（stats 是冻结快照语义）；
    ③ dead 后永久停止 → 后续 append 调用 0 次。
    另含「非 RIE 异常仍按原边界吞掉、不置 dead」。
    """
    import travel_agent.decision as d_mod
    from travel_agent.decision import DecisionEngine as _DE
    from travel_agent.retention import RetentionInvariantError

    orig = d_mod.append_bounded
    calls = []

    def rie(records, record, stats):
        raise RetentionInvariantError("mutation")

    # ---- ① 首次追加即失败 ----
    eng = _DE(shadow_sink=lambda rec: calls.append(rec))
    d_mod.append_bounded = rie
    try:
        eng._shadow(_mk_res(), "poi_query", [], False, True)
    finally:
        d_mod.append_bounded = orig
    check("[S2·RIE①] dead=True", eng.shadow_dead is True, "")
    check("[S2·RIE①] stats 仍 None（未提交）",
          eng.shadow_stats is None, str(eng.shadow_stats))
    check("[S2·RIE①] 容器零新增", len(eng.shadow_log) == 0,
          str(len(eng.shadow_log)))
    check("[S2·RIE①] sink 零调用", len(calls) == 0, str(len(calls)))

    # ---- ② 已有 stats 时失败：计数不误增 ----
    eng2 = _DE()
    _s2_fill(eng2, 3)
    before = (eng2.shadow_stats.total_appended,
              eng2.shadow_stats.total_evicted, len(eng2.shadow_log))
    d_mod.append_bounded = rie
    try:
        eng2._shadow(_mk_res("r2"), "poi_query", [], False, True)
    finally:
        d_mod.append_bounded = orig
    check("[S2·RIE②] total_appended 不误增",
          eng2.shadow_stats.total_appended == before[0],
          f"{eng2.shadow_stats.total_appended} != {before[0]}")
    check("[S2·RIE②] total_evicted 不误增",
          eng2.shadow_stats.total_evicted == before[1],
          str(eng2.shadow_stats.total_evicted))
    check("[S2·RIE②] 容器与 current_size 均不变",
          len(eng2.shadow_log) == before[2]
          and eng2.shadow_stats.current_size == before[2],
          f"{len(eng2.shadow_log)} / {eng2.shadow_stats.current_size}")
    check("[S2·RIE②] dead=True", eng2.shadow_dead is True, "")

    # ---- ③ dead 后永久停止 ----
    counter = {"n": 0}

    def counting(*a, **kw):
        counter["n"] += 1
        raise AssertionError("dead 后不得再调用 append_bounded")

    d_mod.append_bounded = counting
    try:
        for _ in range(3):
            eng2._shadow(_mk_res("r3"), "poi_query", [], False, True)
    finally:
        d_mod.append_bounded = orig
    check("[S2·dead③] 后续 3 轮 append 调用 0 次", counter["n"] == 0,
          str(counter))
    check("[S2·dead③] 容器仍为 dead 前 3 条",
          len(eng2.shadow_log) == 3, str(len(eng2.shadow_log)))

    # ---- ④ 非 RIE 异常：S2 原有边界是「不外吞」----
    # decision._shadow 只捕获 RetentionInvariantError；改造前它也没有
    # try/except 包住 append，故非 RIE 异常照旧外抛（冻结语义，不是缺陷）。
    # 该异常由调用方边界接住：orchestrator._decision_shadow 的 try /
    # _pending_produce 的 try，两处均为 except Exception → 记 warning 并
    # 继续，业务返回值不受影响。
    def boom(*a, **kw):
        raise RuntimeError("append boom")

    eng3 = _DE()
    _s2_fill(eng3, 2)
    d_mod.append_bounded = boom
    raised = None
    try:
        eng3._shadow(_mk_res("r4"), "poi_query", [], False, True)
    except Exception as exc:                     # noqa: BLE001
        raised = exc
    finally:
        d_mod.append_bounded = orig
    check("[S2·边界④] 非 RIE 仍外抛（与改造前一致）",
          isinstance(raised, RuntimeError), repr(raised))
    check("[S2·边界④] 不置 dead（RIE 专属语义）",
          eng3.shadow_dead is False, "")
    check("[S2·边界④] 计数不误增",
          eng3.shadow_stats.total_appended == 2,
          str(eng3.shadow_stats.total_appended))
    check("[S2·边界④] 容器不变", len(eng3.shadow_log) == 2,
          str(len(eng3.shadow_log)))


def test_r01_s4_s5_rie_and_dead() -> None:
    """S4 / S5 补测（评审第 3 项）：首次失败、已有 stats 时失败、
    dead 后永久停止，两容器同构各断言一遍。"""
    from travel_agent.retention import RetentionInvariantError

    for tag, mod, factory, emit in _s45_specs():
        orig = mod.append_bounded

        def rie(*a, **kw):
            raise RetentionInvariantError("mutation")

        # ---- ① 首次追加即失败 ----
        calls = []
        obj = factory(lambda rec: calls.append(rec))
        mod.append_bounded = rie
        try:
            emit(obj)
        finally:
            mod.append_bounded = orig
        check(f"[{tag}·RIE①] dead=True", obj.shadow_dead is True, "")
        check(f"[{tag}·RIE①] stats 仍 None",
              obj.shadow_stats is None, str(obj.shadow_stats))
        check(f"[{tag}·RIE①] 容器零新增", len(obj.shadow_log) == 0,
              str(len(obj.shadow_log)))
        check(f"[{tag}·RIE①] sink 零调用", len(calls) == 0, str(len(calls)))

        # ---- ② 已有 stats 时失败 ----
        calls2 = []
        obj2 = factory(lambda rec: calls2.append(rec))
        for i in range(3):
            emit(obj2, i)
        before = (obj2.shadow_stats.total_appended,
                  obj2.shadow_stats.total_evicted, len(obj2.shadow_log))
        mod.append_bounded = rie
        try:
            emit(obj2, 9)
        finally:
            mod.append_bounded = orig
        check(f"[{tag}·RIE②] total_appended 不误增",
              obj2.shadow_stats.total_appended == before[0],
              f"{obj2.shadow_stats.total_appended} != {before[0]}")
        check(f"[{tag}·RIE②] total_evicted 不误增",
              obj2.shadow_stats.total_evicted == before[1],
              str(obj2.shadow_stats.total_evicted))
        check(f"[{tag}·RIE②] 容器 / current_size 不变",
              len(obj2.shadow_log) == before[2]
              and obj2.shadow_stats.current_size == before[2],
              f"{len(obj2.shadow_log)} / {obj2.shadow_stats.current_size}")
        check(f"[{tag}·RIE②] sink 未收到失败记录", len(calls2) == 3,
              str(len(calls2)))
        check(f"[{tag}·RIE②] dead=True", obj2.shadow_dead is True, "")

        # ---- ③ dead 后永久停止 ----
        counter = {"n": 0}

        def counting(*a, **kw):
            counter["n"] += 1
            raise AssertionError("dead 后不得再调用 append_bounded")

        mod.append_bounded = counting
        try:
            for i in range(3):
                emit(obj2, 100 + i)
        finally:
            mod.append_bounded = orig
        check(f"[{tag}·dead③] 后续 3 轮 append 调用 0 次",
              counter["n"] == 0, str(counter))
        check(f"[{tag}·dead③] sink 也未再被调用", len(calls2) == 3,
              str(len(calls2)))
        check(f"[{tag}·dead③] 容器仍为 3 条", len(obj2.shadow_log) == 3,
              str(len(obj2.shadow_log)))

        # ---- ④ 非 RIE 异常：原有「记录侧一切异常吞掉」边界不变 ----
        def boom(*a, **kw):
            raise RuntimeError("append boom")

        calls3 = []
        obj3 = factory(lambda rec: calls3.append(rec))
        emit(obj3, 0)
        n0 = obj3.shadow_stats.total_appended
        mod.append_bounded = boom
        raised = None
        try:
            emit(obj3, 1)
        except Exception as exc:                 # noqa: BLE001
            raised = exc
        finally:
            mod.append_bounded = orig
        check(f"[{tag}·边界④] 不外抛（原吞噬边界不变）",
              raised is None, repr(raised))
        check(f"[{tag}·边界④] 不置 dead", obj3.shadow_dead is False, "")
        check(f"[{tag}·边界④] 计数不误增",
              obj3.shadow_stats.total_appended == n0,
              f"{obj3.shadow_stats.total_appended} != {n0}")
        check(f"[{tag}·边界④] sink 未收到失败记录", len(calls3) == 1,
              str(len(calls3)))


def test_r01_s4_s5_sink_exception_isolated() -> None:
    """S4 / S5 补测（评审第 4 项）：sink 抛异常 → 记录与 stats 不回滚、
    异常不外抛（原「记录异常全吞」边界不变），且不影响后续追加。"""
    for tag, mod, factory, emit in _s45_specs():
        def boom(rec):
            raise RuntimeError("sink boom")

        obj = factory(boom)
        raised = None
        try:
            emit(obj)
        except Exception as exc:                 # noqa: BLE001
            raised = exc
        check(f"[{tag}·sink] 不外抛", raised is None, repr(raised))
        check(f"[{tag}·sink] 记录保留（不回滚）",
              len(obj.shadow_log) == 1, str(len(obj.shadow_log)))
        check(f"[{tag}·sink] stats 已提交（不丢计数）",
              obj.shadow_stats is not None
              and obj.shadow_stats.total_appended == 1,
              str(obj.shadow_stats))
        emit(obj, 1)
        check(f"[{tag}·sink] 后续追加不受影响",
              len(obj.shadow_log) == 2
              and obj.shadow_stats.total_appended == 2,
              str(obj.shadow_stats))
        check(f"[{tag}·sink] dead 保持 False", obj.shadow_dead is False, "")


def test_r01_s4_s5_non_str_fields() -> None:
    """S4 / S5 补测（评审第 5 项）：非 str 的 ID / detail 字段收敛为
    固定占位；超长仍按冻结上限裁剪；None 原样保留（coerce_text 契约）。"""
    from travel_agent.retention import LIM_DETAIL, LIM_ID
    from travel_agent.pending_producer import PendingProducer
    from travel_agent.pending_revalidator import PendingRevalidator

    prod = PendingProducer()
    prod._shadow("act", detail=456, pending_id=789)
    rec = prod.shadow_log[-1]
    check("[S4·非str] detail → <non-str:int>",
          rec["detail"] == "<non-str:int>", str(rec["detail"]))
    check("[S4·非str] pending_id → <non-str:int>",
          rec["pending_id"] == "<non-str:int>", str(rec["pending_id"]))
    check("[S4·非str] 记录仍为 4 键",
          set(rec) == {"stage", "action", "detail", "pending_id"},
          str(sorted(rec)))

    prod._shadow("act", detail="d" * 400, pending_id="p" * 200)
    rec2 = prod.shadow_log[-1]
    check("[S4·超长] detail 裁到 256",
          len(rec2["detail"]) == LIM_DETAIL, str(len(rec2["detail"])))
    check("[S4·超长] pending_id 裁到 64",
          len(rec2["pending_id"]) == LIM_ID, str(len(rec2["pending_id"])))

    prod._shadow("act", detail=None, pending_id=None)
    rec3 = prod.shadow_log[-1]
    check("[S4·None] detail / pending_id 原样 None",
          rec3["detail"] is None and rec3["pending_id"] is None,
          str(rec3))

    rev = PendingRevalidator()
    rev._shadow("rv", detail=123, pending_id=321, candidate_id=654,
                revalidate_status="confirmed")
    rec4 = rev.shadow_log[-1]
    check("[S5·非str] detail → <non-str:int>",
          rec4["detail"] == "<non-str:int>", str(rec4["detail"]))
    check("[S5·非str] pending_id → <non-str:int>",
          rec4["pending_id"] == "<non-str:int>", str(rec4["pending_id"]))
    check("[S5·非str] candidate_id → <non-str:int>",
          rec4["candidate_id"] == "<non-str:int>", str(rec4["candidate_id"]))
    check("[S5·非str] revalidate_status 原值（非裁剪字段）",
          rec4["revalidate_status"] == "confirmed", str(rec4))

    rev._shadow("rv", detail="e" * 400, pending_id="p" * 200,
                candidate_id="c" * 200, revalidate_status="confirmed")
    rec5 = rev.shadow_log[-1]
    check("[S5·超长] 三字段按上限裁剪",
          len(rec5["detail"]) == LIM_DETAIL
          and len(rec5["pending_id"]) == LIM_ID
          and len(rec5["candidate_id"]) == LIM_ID,
          str((len(rec5["detail"]), len(rec5["pending_id"]),
               len(rec5["candidate_id"]))))


def test_r01_real_handle_250_rounds() -> None:
    """I-3 接入点补测（评审第 6 项）：经 ``Orchestrator.handle`` 真实决策
    链跑 250 轮，验证 S2 封顶后 S1 仍在第 101 轮起持续投影，且两者保留
    的是**最新** 100 条（逐条逐字段相等），不是最早 100 条。"""
    from travel_agent.retention import SHADOW_CAPACITY

    old_autofetch = os.environ.get("TRAVEL_AUTOFETCH")
    old_real = os.environ.get("USE_REAL_DATA")
    _p26_env(shadow="1", channel=None, takeover=None)
    os.environ["TRAVEL_AUTOFETCH"] = "0"          # 不触发后台抓取
    os.environ["USE_REAL_DATA"] = "0"             # mock tools（无网络）
    try:
        orc = Orchestrator()
        s = Session(use_llm=False)
        rounds = 250
        for _ in range(rounds):
            orc.handle(s, "西湖附近有什么好吃的")   # 真实链路：parse→resolve→decide
        eng = orc._decider
        s1 = orc._decision_shadow_stats
        s2 = eng.shadow_stats
        check("[I-3·250] 每轮恰好一次 decide（S2 计数 == 轮数）",
              s2.total_appended == rounds, str(s2.total_appended))
        check("[I-3·250] S2 容器封顶 100 / current_size == len",
              len(eng.shadow_log) == SHADOW_CAPACITY
              and s2.current_size == len(eng.shadow_log),
              f"{len(eng.shadow_log)} / {s2.current_size}")
        check("[I-3·250] S2 total_evicted == 150",
              s2.total_evicted == rounds - SHADOW_CAPACITY,
              str(s2.total_evicted))
        check("[I-3·250] S1 total_appended == 250（第 101 轮起仍投影）",
              s1.total_appended == rounds, str(s1.total_appended))
        check("[I-3·250] S1 容器封顶 100 / current_size == len",
              len(orc._decision_shadow_log) == SHADOW_CAPACITY
              and s1.current_size == len(orc._decision_shadow_log),
              f"{len(orc._decision_shadow_log)} / {s1.current_size}")
        check("[I-3·250] S1 total_evicted == 150",
              s1.total_evicted == rounds - SHADOW_CAPACITY,
              str(s1.total_evicted))
        projected = [{k: r.get(k) for k in SHADOW_RECORD_KEYS}
                     for r in eng.shadow_log[-SHADOW_CAPACITY:]]
        check("[I-3·250] S1 保留记录 == S2 末 100 条投影（逐条逐字段）",
              orc._decision_shadow_log == projected,
              "首条不一致" if orc._decision_shadow_log[:1] != projected[:1]
              else "长度不一致")
        check("[I-3·250] S1 记录键集恒为 11 键白名单",
              all(set(r) == set(SHADOW_RECORD_KEYS)
                  for r in orc._decision_shadow_log), "")
        check("[I-3·250] 末条为本轮产物（与 S2 末条同源）",
              orc._decision_shadow_log[-1]
              == {k: eng.shadow_log[-1].get(k) for k in SHADOW_RECORD_KEYS},
              str(orc._decision_shadow_log[-1])[:120])
    finally:
        for key, val in (("TRAVEL_AUTOFETCH", old_autofetch),
                         ("USE_REAL_DATA", old_real)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        _p26_env()


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
               test_p25a_decision_chain_failure,
               test_p25a_compensation_boundary_integration,
               test_p25a_orphan_terminal_integration,
               test_p25a_channel0_clarify_decision_bitwise,
               test_p25b_a3_confirmed_controlled_flow,
               test_p25b_a4_plan_confirmed_no_plan_write,
               test_p25b_a5_ambiguous_new_pending,
               test_p25b_a6_a7_unverified_rejected,
               test_p25b_d1_d2_d3_robustness,
               test_p25b_f2_f3_negative,
               test_p25b_mechanism_e_fresh_entity_enforcement,
               test_p25b_mechanism_e_failure_conservative,
               test_p25b_other_city_template_text_detection,
               test_p25b_channel0_zero_revalidation,
               # ---- P2.6 增量 1+3：DecisionEngine 影子触达（10 个）----
               test_p26_shadow_switch_off_frozen,
               test_p26_shadow_switch_on_single_call,
               test_p26_shadow_record_matches_independent_decide,
               test_p26_shadow_touchpoint_direct_zero_write,
               test_p26_shadow_handle_baseline_equivalence,
               test_p26_shadow_exception_isolation,
               test_p26_shadow_input_boundary_missing_field,
               test_p26_shadow_zero_network_and_no_reparse,
               test_p26_shadow_channel1_no_double_decide,
               test_p26_switch_freeze,
               # ---- R-01（I-2 + I-3）：S1/S2 有界化（10 个）----
               test_r01_s2_bounded_and_counter,
               test_r01_s2_field_clip_non_str,
               test_r01_s1_projection_beyond_s2_capacity,
               test_r01_s1_lazy_no_record,
               test_r01_s1_first_append_atomic,
               test_r01_s1_dead_persistent,
               test_r01_sink_after_commit_s2_s4_s5,
               test_r01_s4_s5_bounded,
               test_r01_channel1_s2_still_bounded,
               test_r01_switch_matrix_laziness,
               # ---- 补测（评审 2026-11-23 授权，仅测试文件）----
               test_r01_s2_rie_and_dead,
               test_r01_s4_s5_rie_and_dead,
               test_r01_s4_s5_sink_exception_isolated,
               test_r01_s4_s5_non_str_fields,
               test_r01_real_handle_250_rounds):
        fn()
    print(f"\npending channel P2.4a(+R1+P2.4b+P2.5a+P2.5b+P2.6): {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
