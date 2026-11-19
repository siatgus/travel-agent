"""P2.2 契约测试：SQLitePendingStore 事务/并发/隔离/时间契约。

验收依据 P2.1 验收后的 P2.2 开工要求（八条）+ 半成功防线。
运行：python tests/test_pending_sqlite.py
内存桩与 SQLite 双实现生命周期对拍锁定「一致的状态语义」。
"""

import os
import re
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.pending import (PendingStore, _snap,
                                  PENDING_OPEN, PENDING_CONSUMED,
                                  PENDING_CANCELLED, PENDING_EXPIRED)
from travel_agent.pending_store_sqlite import (SQLitePendingStore,
                                               _epoch, _iso_utc)

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
    """可推进的注入时钟（naive → SQLite 实现按 UTC 解释）。"""
    state = {"now": start}

    def now():
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, advance


def temp_db() -> str:
    d = tempfile.mkdtemp(prefix="pending_sqlite_")
    return os.path.join(d, "pending_test.db")


def raw_row(db: str, pending_id: str):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM pending_disambiguation WHERE pending_id = ?",
            (pending_id,)).fetchone()
    finally:
        con.close()


def raw_consumptions(db: str, pending_id: str):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM pending_consumption WHERE pending_id = ?",
            (pending_id,)).fetchall()
    finally:
        con.close()


SNAP = [_snap("c1", 1, "海贝皇大排档（南京店）", "南京", "nj-001", "中山路1号"),
        _snap("c2", 2, "海贝皇大排档（上海店）", "上海", "sh-001", "南京西路2号")]


# ------------------------------------------------ 验收要求 1+3：时间契约与索引

def test_time_contract_and_schema() -> None:
    """时间列均为 UTC epoch 整数；ISO 字符串仅派生展示；索引存在。"""
    db = temp_db()
    clock, _ = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    st = SQLitePendingStore("s1", db_path=db, ttl_seconds=60, now=clock)
    p = st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP)

    con = sqlite3.connect(db)
    try:
        objs = {r[0]: r[1] for r in con.execute(
            "SELECT name, type FROM sqlite_master"
            " WHERE type IN ('table', 'index')")}
        idx_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'ux_pending_open'"
        ).fetchone()[0]
        types = con.execute(
            "SELECT typeof(created_at), typeof(expires_at)"
            " FROM pending_disambiguation").fetchone()
    finally:
        con.close()

    check("pending_disambiguation 表存在",
          objs.get("pending_disambiguation") == "table", str(objs))
    check("pending_consumption 表存在",
          objs.get("pending_consumption") == "table", str(objs))
    check("ux_pending_open 部分唯一索引存在（单 Session 单 active）",
          objs.get("ux_pending_open") == "index"
          and "status" in idx_sql and "'open'" in idx_sql, idx_sql)
    check("created_at/expires_at 库内为 INTEGER epoch",
          tuple(types) == ("integer", "integer"), str(tuple(types)))

    row = raw_row(db, p.pending_id)
    exp_created = int(_epoch(datetime(2026, 11, 18, 15, 0, 0)
                             .replace(tzinfo=timezone.utc)))
    check("epoch 值与注入时钟一致（naive 按 UTC 解释）",
          int(row["created_at"]) == exp_created
          and int(row["expires_at"]) == exp_created + 60,
          f"{row['created_at']}/{row['expires_at']} vs {exp_created}")
    # ISO 字符串由 epoch UTC 派生（展示用途），可无损还原回 epoch
    back = _epoch(datetime.strptime(
        p.created_at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc))
    check("ISO 展示字符串与 epoch 无损互转",
          back == int(row["created_at"]), f"{p.created_at} -> {back}")
    check("数据类契约不变（ISO 字符串字段）",
          re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", p.created_at)
          is not None, p.created_at)


# ------------------------------------------------ 生命周期 + 验收要求 8：双实现对拍

def _lifecycle(make_store) -> list:
    """统一生命周期脚本：两实现逐位对拍。返回各步结果序列。"""
    out = []
    st = make_store()
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    out.append(p.status)
    out.append(st.get_active_pending() is not None)
    r1 = st.consume_pending(p.pending_id, "c1")
    out.append((r1.ok, r1.candidate.name))
    out.append(st.consume_pending(p.pending_id, "c2").ok)   # 重复消费
    p2 = st.create_pending("换个问题", "poi_qa", "query", SNAP)
    out.append(p2.status)
    out.append(st.cancel_pending(p2.pending_id).ok)
    out.append(st.cancel_pending(p2.pending_id).ok)         # 重复取消
    out.append(st.get_active_pending() is None)
    p3 = st.create_pending("再换", "poi_qa", "query", SNAP)
    out.append(st.expire_pending(p3.pending_id).ok)
    out.append(st.get_pending_by_id(p3.pending_id).status)
    out.append(st.consume_pending(p3.pending_id, "c1").ok)  # 终态不可消费
    out.append(st.get_active_pending() is None)
    out.append(st.consume_pending("不存在", "c1").ok)
    return out


def test_parity_memory_vs_sqlite() -> None:
    """验收要求 8：内存桩与 SQLite 实现状态语义一致。"""
    mem = _lifecycle(lambda: PendingStore())
    sql = _lifecycle(lambda: SQLitePendingStore("s1", db_path=temp_db()))
    check("双实现生命周期逐位一致",
          mem == sql, f"mem={mem}\nsql={sql}")

    # 快照重建：经 SQLite 存取后候选仍是冻结结构且值不变
    db = temp_db()
    st = SQLitePendingStore("s1", db_path=db)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    got = st.get_active_pending()
    check("存取往返后 as_dict 与入参一致（冻结候选还原）",
          got.as_dict()["candidates"] == SNAP, str(got.as_dict()))
    check("存取往返后仍为 PendingCandidate 冻结结构",
          all(c.__dataclass_params__.frozen for c in got.candidates), "")
    check("candidate_by_id / snapshot_candidate_ids 契约不变",
          got.candidate_by_id("c2").city == "上海"
          and got.snapshot_candidate_ids() == ("c1", "c2"), "")
    check("消费回传冻结候选",
          st.consume_pending(p.pending_id, "c2").candidate.name ==
          "海贝皇大排档（上海店）", "")


# ------------------------------------------------ 验收要求 4+5：原子性/并发消费

def test_consume_atomicity_and_record() -> None:
    """验收要求 4：候选校验+条件更新+消费记录同事务；记录内容正确。"""
    db = temp_db()
    st = SQLitePendingStore("s1", db_path=db)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    check("快照外候选拒绝且不产生消费记录",
          not st.consume_pending(p.pending_id, "c999").ok
          and raw_consumptions(db, p.pending_id) == [], "")
    check("拒绝后 pending 仍 open 可消费",
          raw_row(db, p.pending_id)["status"] == PENDING_OPEN, "")

    out = st.consume_pending(p.pending_id, "c2")
    check("消费成功", out.ok, str(out))
    rows = raw_consumptions(db, p.pending_id)
    check("消费记录恰好一条", len(rows) == 1, str(rows))
    r = rows[0]
    check("消费记录字段正确（session/candidate/名称/城市）",
          r["session_id"] == "s1" and r["candidate_id"] == "c2"
          and r["selected_name"] == "海贝皇大排档（上海店）"
          and r["selected_city"] == "上海"
          and r["original_intent"] == "plan_modify", str(dict(r)))
    con = sqlite3.connect(db)
    try:
        t = con.execute("SELECT typeof(consumed_at) FROM"
                        " pending_consumption").fetchone()[0]
    finally:
        con.close()
    check("consumed_at 为 INTEGER epoch", t == "integer", str(t))
    check("消费后 status=consumed",
          raw_row(db, p.pending_id)["status"] == PENDING_CONSUMED, "")
    check("重复消费被拒（规则 6）",
          not st.consume_pending(p.pending_id, "c1").ok
          and "已消费" in st.consume_pending(p.pending_id, "c1").reason, "")


def test_half_success_rollback() -> None:
    """半成功防线：消费记录写入失败 → 整体回滚，pending 保持 open。"""
    db = temp_db()
    st = SQLitePendingStore("s1", db_path=db)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    # 预埋同 pending_id 的消费记录，制造 INSERT 唯一冲突
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO pending_consumption (pending_id, session_id,"
            " candidate_id, consumed_at, original_intent, selected_name)"
            " VALUES (?, 's1', 'cX', 0, 'plan_modify', '预埋记录')",
            (p.pending_id,))
        con.commit()
    finally:
        con.close()

    out = st.consume_pending(p.pending_id, "c1")
    check("消费记录写入失败 → 消费失败且 reason 可归因",
          not out.ok and "回滚" in out.reason, str(out))
    check("回滚后 pending 保持 open（绝无半成功状态）",
          raw_row(db, p.pending_id)["status"] == PENDING_OPEN, "")
    check("回滚后不产生第二条消费记录",
          len(raw_consumptions(db, p.pending_id)) == 1, "")
    check("回滚后 pending 仍可正常消费", st.get_active_pending() is not
          None, "")


def test_concurrent_consume() -> None:
    """验收要求 5：两线程并发消费同一 Pending，恰好一个成功。"""
    db = temp_db()
    sa = SQLitePendingStore("s1", db_path=db)
    sb = SQLitePendingStore("s1", db_path=db)   # 独立连接
    p = sa.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    results = []
    barrier = threading.Barrier(2)

    def worker(store, cid):
        barrier.wait()
        results.append(store.consume_pending(p.pending_id, cid))

    t1 = threading.Thread(target=worker, args=(sa, "c1"))
    t2 = threading.Thread(target=worker, args=(sb, "c2"))
    t1.start(); t2.start(); t1.join(); t2.join()

    check("并发消费恰好一次成功",
          sum(1 for r in results if r.ok) == 1, str(results))
    winner_reason_ok = all(
        ("已消费" in r.reason) for r in results if not r.ok)
    check("失败方归因为已消费（BEGIN IMMEDIATE 串行化）",
          winner_reason_ok, str([r.reason for r in results if not r.ok]))
    check("并发后 status=consumed 且消费记录恰一条",
          raw_row(db, p.pending_id)["status"] == PENDING_CONSUMED
          and len(raw_consumptions(db, p.pending_id)) == 1, "")


# ------------------------------------------------ 验收要求 2+6：创建事务与自动过期

def test_auto_expire_create() -> None:
    """验收要求 6：A 到期无显式 expire → create B 成功（双实现）。"""
    clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))

    # 内存桩
    mem = PendingStore(ttl_seconds=60, now=clock)
    a = mem.create_pending("排海贝皇大排档", "plan_modify",
                           "must_visit", SNAP)
    advance(61)
    try:
        b = mem.create_pending("再来一次", "plan_modify",
                               "must_visit", SNAP)
        check("内存桩：A 到期未显式 expire，create B 成功",
              b.status == PENDING_OPEN
              and mem.get_active_pending().pending_id == b.pending_id, "")
    except ValueError as e:
        check("内存桩：A 到期未显式 expire，create B 成功", False, str(e))

    # SQLite
    db = temp_db()
    sql = SQLitePendingStore("s1", db_path=db, ttl_seconds=60, now=clock)
    a2 = sql.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    advance(61)
    b2 = sql.create_pending("再来一次", "plan_modify", "must_visit", SNAP)
    check("SQLite：A 到期未显式 expire，create B 成功",
          b2.status == PENDING_OPEN
          and sql.get_active_pending().pending_id == b2.pending_id, "")
    check("旧 pending A 在创建事务内被自动过期登记",
          raw_row(db, a2.pending_id)["status"] == PENDING_EXPIRED, "")
    check("自动过期后 A 不可消费",
          not sql.consume_pending(a2.pending_id, "c1").ok, "")

    # 规则 8：未到期的 open A 仍阻塞 create（单 active）
    clock2, _ = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    st8 = SQLitePendingStore("s1", db_path=temp_db(), ttl_seconds=600,
                             now=clock2)
    st8.create_pending("占住", "plan_modify", "must_visit", SNAP)
    try:
        st8.create_pending("撞索引", "poi_qa", "query", SNAP)
        check("未到期 open A 阻塞 create（规则 8，索引兜底）",
              False, "未抛异常")
    except ValueError as e:
        check("未到期 open A 阻塞 create（规则 8，索引兜底）",
              "active pending" in str(e), str(e))


def test_concurrent_create() -> None:
    """并发创建：部分唯一索引兜底，恰好一个成功。"""
    db = temp_db()
    clock, _ = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    sa = SQLitePendingStore("s1", db_path=db, ttl_seconds=600, now=clock)
    sb = SQLitePendingStore("s1", db_path=db, ttl_seconds=600, now=clock)
    outcomes = {"ok": 0, "reject": 0}
    barrier = threading.Barrier(2)

    def worker(store):
        barrier.wait()
        try:
            store.create_pending("并发创建", "plan_modify",
                                 "must_visit", SNAP)
            outcomes["ok"] += 1
        except ValueError:
            outcomes["reject"] += 1

    t1 = threading.Thread(target=worker, args=(sa,))
    t2 = threading.Thread(target=worker, args=(sb,))
    t1.start(); t2.start(); t1.join(); t2.join()
    check("并发创建恰好一个成功（索引兜底）",
          outcomes == {"ok": 1, "reject": 1}, str(outcomes))
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM pending_disambiguation"
                        " WHERE session_id = 's1' AND status = 'open'"
                        ).fetchone()[0]
    finally:
        con.close()
    check("并发后该 Session 恰一个 open",
          n == 1, str(n))


# ------------------------------------------------ 验收要求 7：Session 隔离

def test_session_isolation() -> None:
    """验收要求 7：B 不能消费 A 的 pending_id，A 状态不变。"""
    db = temp_db()
    sa = SQLitePendingStore("session-A", db_path=db)
    sb = SQLitePendingStore("session-B", db_path=db)
    pa = sa.create_pending("排海贝皇大排档", "plan_modify",
                           "must_visit", SNAP)

    out = sb.consume_pending(pa.pending_id, "c1")
    check("B 消费 A 的 pending_id 被拒绝",
          not out.ok and "不匹配" in out.reason, str(out))
    check("拒绝后 A 的 pending 状态不变（仍 open 可见可消费）",
          raw_row(db, pa.pending_id)["status"] == PENDING_OPEN
          and sa.get_active_pending() is not None, "")
    check("B 侧 get_active_pending 看不到 A 的 pending",
          sb.get_active_pending() is None, "")
    check("B 侧 get_pending_by_id 对 A 的 pending 返回 None",
          sb.get_pending_by_id(pa.pending_id) is None, "")
    check("B 侧 cancel/expire 同样隔离",
          not sb.cancel_pending(pa.pending_id).ok
          and not sb.expire_pending(pa.pending_id).ok, "")
    check("隔离拒绝后 A 仍可正常消费",
          sa.consume_pending(pa.pending_id, "c1").ok, "")

    # 跨 Session：A 消费成功后，consumption 记录归属 A，B 不可见
    rows = raw_consumptions(db, pa.pending_id)
    check("消费记录归属正确 session",
          len(rows) == 1 and rows[0]["session_id"] == "session-A",
          str(rows))
    pb = sb.create_pending("B 自己的", "poi_qa", "query", SNAP)
    check("B 可创建自己的 pending（互不干扰）",
          sb.get_active_pending().pending_id == pb.pending_id, "")


# ------------------------------------------------ 规则 5：过期判定（epoch 比较）

def test_expiry_boundary_epoch_compare() -> None:
    """过期判定用 epoch 严格比较：expires_at > now 才可消费。"""
    clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    db = temp_db()
    st = SQLitePendingStore("s1", db_path=db, ttl_seconds=60, now=clock)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", SNAP)
    advance(59)
    check("到期前 1 秒仍可消费",
          st.consume_pending(p.pending_id, "c1").ok, "")

    clock2, advance2 = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    db2 = temp_db()
    st2 = SQLitePendingStore("s1", db_path=db2, ttl_seconds=60,
                             now=clock2)
    p2 = st2.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    advance2(60)   # 跨秒边界：expires_at == now → 严格比较判过期
    check("到期边界（expires_at == now）get_active 返回 None",
          st2.get_active_pending() is None, "")
    out = st2.consume_pending(p2.pending_id, "c1")
    check("到期边界消费被拒且登记 expired（规则 5）",
          not out.ok and "过期" in out.reason
          and raw_row(db2, p2.pending_id)["status"] == PENDING_EXPIRED,
          str(out))


def test_latest_consumption_parity() -> None:
    """P2.3 冻结项：_latest_consumption 双实现同语义对拍。

    - 消费成功才记录七字段；未成功消费不产生记录；
    - 内存桩与 SQLite 字段级一致（重复选择识别的内部查询路径，
      不进公共契约）。
    """
    recs = {}
    for impl in ("memory", "sqlite"):
        if impl == "memory":
            st = PendingStore(session_id="s1")
        else:
            st = SQLitePendingStore("s1", db_path=temp_db())
        check(f"[{impl}] 初始无消费记录",
              st._latest_consumption() is None, "")
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP)
        check(f"[{impl}] 消费失败（快照外）不产生记录",
              not st.consume_pending(p.pending_id, "c999").ok
              and st._latest_consumption() is None, "")
        out = st.consume_pending(p.pending_id, "c1")
        check(f"[{impl}] 消费成功产生记录", out.ok
              and st._latest_consumption() is not None, "")
        recs[impl] = st._latest_consumption()
        r = recs[impl]
        # P2.5b D3 已批契约升级：七字段 → 十字段（前七字段名与语义
        # 逐位不变；重验证三列消费时默认 None，待 record_revalidation
        # 条件回填）
        check(f"[{impl}] 记录十字段齐全且值正确（D3 升级）",
              set(r) == {"session_id", "pending_id", "candidate_id",
                         "consumed_at", "original_intent",
                         "selected_name", "selected_city",
                         "revalidate_status", "revalidate_probe_status",
                         "revalidated_at"}
              and r["session_id"] == "s1"
              and r["pending_id"] == p.pending_id
              and r["candidate_id"] == "c1"
              and isinstance(r["consumed_at"], int)
              and r["original_intent"] == "plan_modify"
              and r["selected_name"] == "海贝皇大排档（南京店）"
              and r["selected_city"] == "南京"
              and r["revalidate_status"] is None
              and r["revalidate_probe_status"] is None
              and r["revalidated_at"] is None, str(r))
        check(f"[{impl}] 重复消费失败不追加记录（同一 pending 一次）",
              not st.consume_pending(p.pending_id, "c2").ok
              and st._latest_consumption()["candidate_id"] == "c1", "")
    m, s = recs["memory"], recs["sqlite"]
    m_cmp = dict(m, consumed_at=s["consumed_at"],
                 pending_id=s["pending_id"])   # 时间戳/ID 天然不同
    check("双实现记录字段级一致（时间戳/ID 除外）", m_cmp == s,
          f"mem={m}\nsql={s}")


def test_expiry_boundary_and_fail_closed() -> None:
    """P2.3 验收补丁：过期边界（等于即过期）+ 异常时间失败关闭。"""
    from dataclasses import replace as dc_replace
    # 边界：now == expires_at → 已过期（内存桩与 SQLite 同语义）
    clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    mem = PendingStore(ttl_seconds=60, now=clock)
    pm = mem.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    advance(60)
    check("内存桩：expires_at == now → get_active 返回 None",
          mem.get_active_pending() is None, "")
    out = mem.consume_pending(pm.pending_id, "c1")
    check("内存桩：expires_at == now → 消费被拒",
          not out.ok and "过期" in out.reason, str(out))

    db = temp_db()
    sql = SQLitePendingStore("s1", db_path=db, ttl_seconds=60,
                             now=clock)
    ps = sql.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP)
    advance(60)
    check("SQLite：expires_at == now → get_active 返回 None",
          sql.get_active_pending() is None, "")
    check("SQLite：expires_at == now → 消费被拒且登记 expired",
          not sql.consume_pending(ps.pending_id, "c1").ok
          and raw_row(db, ps.pending_id)["status"] == PENDING_EXPIRED,
          "")

    # 失败关闭：无法解析的 expires_at 绝不允许消费（内存桩）
    clock2, _ = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    mem2 = PendingStore(ttl_seconds=600, now=clock2)
    p2 = mem2.create_pending("排海贝皇大排档", "plan_modify",
                             "must_visit", SNAP)
    mem2._active = dc_replace(p2, expires_at="不是时间")
    out2 = mem2.consume_pending(p2.pending_id, "c1")
    check("内存桩：异常 expires_at → 失败关闭拒绝消费",
          not out2.ok, str(out2))
    check("内存桩：异常 expires_at → get_active 不返回",
          mem2.get_active_pending() is None, "")

    # 失败关闭：SQLite 异常时间值 → 消费拒绝 + 自动过期兜底
    clock3, _ = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
    db3 = temp_db()
    sql3 = SQLitePendingStore("s1", db_path=db3, ttl_seconds=600,
                              now=clock3)
    p3 = sql3.create_pending("排海贝皇大排档", "plan_modify",
                             "must_visit", SNAP)
    con = sqlite3.connect(db3)
    try:
        con.execute("UPDATE pending_disambiguation SET"
                    " expires_at = '不是时间' WHERE pending_id = ?",
                    (p3.pending_id,))
        con.commit()
    finally:
        con.close()
    check("SQLite：异常 expires_at → get_active 返回 None（失败关闭）",
          sql3.get_active_pending() is None, "")
    out3 = sql3.consume_pending(p3.pending_id, "c1")
    check("SQLite：异常 expires_at → 消费被拒（失败关闭）",
          not out3.ok, str(out3))
    check("SQLite：异常 expires_at → 登记 expired（不被消费）",
          raw_row(db3, p3.pending_id)["status"] == PENDING_EXPIRED, "")
    # 异常时间不阻塞新 Pending（自动过期兜底覆盖 SQL 不可比行）
    p3b = sql3.create_pending("再来一次", "plan_modify",
                              "must_visit", SNAP)
    check("SQLite：异常时间旧行不阻塞 create（兜底自动过期）",
          p3b.status == PENDING_OPEN
          and sql3.get_active_pending().pending_id == p3b.pending_id,
          "")


def test_revalidation_backfill_parity() -> None:
    """P2.5b D3 六契约：record_revalidation 双实现对拍。

    - 命中 1 行（三列落库、revalidated_at 为 INTEGER UTC epoch）；
    - 0 行三形态（跨 session / 跨 pending / candidate_id 不符）全
      False，绝不插入新记录；
    - store 异常 → False 不抛出（契约 4）；
    - 十字段对拍：_latest_consumption 双实现字段名与值逐位一致
      （七字段语义不变 + 重验证三列）。"""
    clock, _ = fixed_clock(datetime(2026, 11, 18, 16, 0, 0))
    recs = {}
    pids = {}
    for impl, mk in (("memory",
                      lambda: PendingStore(session_id="s-rb", now=clock)),
                     ("sqlite",
                      lambda: SQLitePendingStore("s-rb", db_path=temp_db(),
                                                 now=clock))):
        st = mk()
        p = st.create_pending("排海贝皇大排档", "poi_query", "query", SNAP)
        out = st.consume_pending(p.pending_id, "c1")
        check(f"[{impl}] 消费成功（回填前置）", out.ok, str(out))
        # 命中 1 行 → True + 三列落库
        ok = st.record_revalidation("s-rb", p.pending_id, "c1",
                                    "confirmed", "not_applicable")
        rec = st._latest_consumption()
        check(f"[{impl}] 回填命中 1 行 → True", ok is True, str(ok))
        check(f"[{impl}] 三列落库（status/probe/epoch int）",
              rec["revalidate_status"] == "confirmed"
              and rec["revalidate_probe_status"] == "not_applicable"
              and isinstance(rec["revalidated_at"], int), str(rec))
        # 0 行三形态全 False（契约 3）
        for sid, pid_, cid_, tag in (
                ("s-other", p.pending_id, "c1", "跨session"),
                ("s-rb", "nope", "c1", "跨pending"),
                ("s-rb", p.pending_id, "c999", "候选不符")):
            ok0 = st.record_revalidation(sid, pid_, cid_, "confirmed", None)
            check(f"[{impl}] 0行·{tag} → False", ok0 is False, str(ok0))
        # 非法值 → False（契约 6），不覆盖已回填值
        okb = st.record_revalidation("s-rb", p.pending_id, "c1",
                                     "skipped", None)
        check(f"[{impl}] 非法状态值 → False（契约 6）", okb is False, "")
        check(f"[{impl}] 非法值不写（原回填值保持）",
              st._latest_consumption()["revalidate_status"] == "confirmed",
              "")
        recs[impl] = rec
        pids[impl] = p.pending_id
    # store 异常 → False 不抛出（契约 4）
    st_sql = SQLitePendingStore("s-rb2", db_path=temp_db(), now=clock)
    orig_connect = st_sql._connect

    def boom_connect():
        raise sqlite3.OperationalError("注入：db 故障")
    st_sql._connect = boom_connect
    try:
        oke = st_sql.record_revalidation("s-rb2", "p-x", "c1",
                                         "confirmed", None)
    finally:
        st_sql._connect = orig_connect
    check("[sqlite] store 异常 → False 不抛出（契约 4）", oke is False, "")
    stm = PendingStore(session_id="s-rb2", now=clock)
    p_m = stm.create_pending("排海贝皇大排档", "poi_query", "query", SNAP)
    stm.consume_pending(p_m.pending_id, "c1")

    def boom_now():
        raise RuntimeError("注入：时钟故障")
    stm._now = boom_now          # 实现体内部 _epoch(self._now()) 抛出
    okm = None
    try:
        okm = stm.record_revalidation("s-rb2", p_m.pending_id, "c1",
                                      "confirmed", None)
    except Exception as e:
        check("[memory] 异常被吞（契约 4：不抛出到业务）", False, str(e))
    check("[memory] store 异常 → False 不抛出（契约 4）", okm is False, "")
    # 十字段双实现逐位对拍（ID/时间戳天然不同，其余逐位一致）
    m, s = recs["memory"], recs["sqlite"]
    m_cmp = dict(m, consumed_at=s["consumed_at"],
                 pending_id=s["pending_id"],
                 revalidated_at=s["revalidated_at"])
    check("十字段双实现字段级一致（ID/时间戳除外）", m_cmp == s,
          f"mem={m}\nsql={s}")


def main() -> int:
    for fn in (test_time_contract_and_schema,
               test_parity_memory_vs_sqlite,
               test_consume_atomicity_and_record,
               test_half_success_rollback,
               test_concurrent_consume,
               test_auto_expire_create,
               test_concurrent_create,
               test_session_isolation,
               test_expiry_boundary_epoch_compare,
               test_latest_consumption_parity,
               test_expiry_boundary_and_fail_closed,
               test_revalidation_backfill_parity):
        fn()
    print(f"\npending sqlite store: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
