"""P2.3 契约测试：PendingInterceptor 九类 verdict / 纯度 / 保守红线。

验收依据 P2.3 开工申请（评审冻结）：evaluate/act 严格分离、判定
优先级、hand_back 不当作取消、重复选择三条件。运行：
python tests/test_pending_interceptor.py
"""

import copy
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.pending import PendingStore, PENDING_OPEN
from travel_agent.pending_interceptor import (
    PendingInterceptor, InterceptVerdict, VERDICTS, ZERO_STATE_VERDICTS,
    V_CONSUME, V_CANCEL_NEW_QUESTION, V_HAND_BACK, V_RESTATE,
    V_EXPIRED_REPLY, V_CANCEL_EXPLICIT, V_CANCEL_DEST_CHANGED,
    V_DUPLICATE_SELECT, V_NONE)
from travel_agent.pending_store_sqlite import SQLitePendingStore
from travel_agent.parser import EntityCandidate, ParseResult

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


def temp_db() -> str:
    d = tempfile.mkdtemp(prefix="pending_itc_")
    return os.path.join(d, "pending_test.db")


SNAP2 = [
    {"candidate_id": "c1", "display_order": 1, "entity_id": "nj-001",
     "name": "海贝皇大排档（南京店）", "city": "南京",
     "address": "中山路1号"},
    {"candidate_id": "c2", "display_order": 2, "entity_id": "sh-001",
     "name": "海贝皇大排档（上海店）", "city": "上海",
     "address": "南京西路2号"}]
SNAP1 = [SNAP2[0]]


def make(impl: str, session: str = "s1", clock=None, window=600):
    if impl == "memory":
        st = PendingStore(ttl_seconds=600, now=clock, session_id=session)
    else:
        st = SQLitePendingStore(session, db_path=temp_db(),
                                ttl_seconds=600, now=clock)
    it = PendingInterceptor(st, reselect_window=window)
    return st, it


def pr_entity(name="总统府", city="南京"):
    return ParseResult(text=f"介绍一下{name}", intent="poi_query",
                       intent_coarse="poi",
                       poi_candidates=[EntityCandidate(name=name,
                                                       city=city)])


def pr_weather():
    return ParseResult(text="明天天气怎么样", intent="weather_query",
                       intent_coarse="weather")


def pr_fuzzy():
    return ParseResult(text="南京有什么好吃的",
                       intent="restaurant_search", intent_coarse="poi",
                       query_city="南京", poi_category="restaurant")


def pr_city(city: str):
    return ParseResult(text=f"{city}那家", intent="poi_query",
                       intent_coarse="poi", query_city=city)


def pr_dest(dest: str, writable: bool = True):
    return ParseResult(text="改目的地", intent="plan_modify",
                       intent_coarse="plan",
                       destination_new=dest,
                       destination_writable=writable)


def one(impl, ut, snap=SNAP2, parse=None, clock=None, **kw):
    """新 store + 新 pending + 单次 evaluate 的便捷入口。"""
    st, it = make(impl, clock=clock, **kw)
    p = st.create_pending("排海贝皇大排档", "plan_modify",
                          "must_visit", snap)
    return st, it, p, it.evaluate(ut, parse,
                                  now=clock() if clock else None)


# ------------------------------------------------ Group A：序号指代

def test_order_selection() -> None:
    for impl in ("memory", "sqlite"):
        for ut, cid in (("第一个", "c1"), ("第2个", "c2"), ("选2", "c2"),
                        ("就要2号", "c2"), ("2号", "c2"), ("选第二个", "c2")):
            st, it, p, v = one(impl, ut)
            check(f"[{impl}] 序号指代「{ut}」→ consume {cid}",
                  v.verdict == V_CONSUME and v.candidate_id == cid
                  and v.pending_id == p.pending_id, str(v.as_dict()))
        for ut in ("第3个", "第0个"):
            st, it, p, v = one(impl, ut)
            check(f"[{impl}] 序号越界「{ut}」→ hand_back 保持 open",
                  v.verdict == V_HAND_BACK
                  and st.get_pending_by_id(p.pending_id).status ==
                  PENDING_OPEN, str(v.as_dict()))
        # 数字不误触发：日期/人数/天数
        for ut in ("两天行程", "3个人怎么分", "第二天行程"):
            st, it, p, v = one(impl, ut)
            check(f"[{impl}] 非序号表达「{ut}」不触发选择/取消",
                  v.verdict == V_HAND_BACK
                  and st.get_pending_by_id(p.pending_id).status ==
                  PENDING_OPEN, str(v.as_dict()))


# ------------------------------------------------ Group B：名称/城市/语气

def test_name_city_selection() -> None:
    for impl in ("memory", "sqlite"):
        st, it, p, v = one(impl, "海贝皇大排档（南京店）")
        check(f"[{impl}] 全名匹配 → consume c1",
              v.verdict == V_CONSUME and v.candidate_id == "c1",
              str(v.as_dict()))
        st, it, p, v = one(impl, "海贝皇")
        check(f"[{impl}] 名称歧义（两候选都含核心）→ hand_back",
              v.verdict == V_HAND_BACK
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, str(v.as_dict()))
        _, it, _, v = one(impl, "南京那家")
        check(f"[{impl}] 城市限定指代唯一命中 → consume c1",
              v.verdict == V_CONSUME and v.candidate_id == "c1",
              str(v.as_dict()))
        _, it, _, v = one(impl, "上海这家")
        check(f"[{impl}] 城市限定指代 → c2",
              v.verdict == V_CONSUME and v.candidate_id == "c2",
              str(v.as_dict()))
        _, it, _, v = one(impl, "苏州那家", parse=pr_city("苏州"))
        check(f"[{impl}] 快照外城市绝不猜 → hand_back",
              v.verdict == V_HAND_BACK, str(v.as_dict()))
        st, it, p, v = one(impl, "就这家", snap=SNAP2)
        check(f"[{impl}] 纯选择语气多候选 → hand_back",
              v.verdict == V_HAND_BACK, str(v.as_dict()))
        _, it, _, v = one(impl, "就这家", snap=SNAP1)
        check(f"[{impl}] 纯选择语气单候选 → consume",
              v.verdict == V_CONSUME and v.candidate_id == "c1",
              str(v.as_dict()))
        _, it, _, v = one(impl, "还是它", snap=SNAP1)
        check(f"[{impl}] 「还是它」单候选 → consume",
              v.verdict == V_CONSUME, str(v.as_dict()))


# ------------------------------------------------ Group C：保守红线（评审重点）

def test_conservative_red_lines() -> None:
    for impl in ("memory", "sqlite"):
        # 「南京那家怎么样？」——询问/评价标记 → 不确认为选择，
        # 绝不取消、绝不消费，pending 保持 open
        st, it, p, v = one(impl, "南京那家怎么样？")
        check(f"[{impl}] 「南京那家怎么样？」→ hand_back 不消费不取消",
              v.verdict == V_HAND_BACK and v.verdict not in
              (V_CANCEL_NEW_QUESTION, V_CANCEL_EXPLICIT,
               V_CANCEL_DEST_CHANGED, V_CONSUME, V_RESTATE),
              str(v.as_dict()))
        check(f"[{impl}] 「南京那家怎么样？」后 pending 仍 open",
              st.get_active_pending() is not None
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, "")
        # 「南京有什么好吃的」——仅城市词+泛化品类 → hand_back
        st, it, p, v = one(impl, "南京有什么好吃的", parse=pr_fuzzy())
        check(f"[{impl}] 模糊候选（仅城市词+品类）→ hand_back 不取消",
              v.verdict == V_HAND_BACK, str(v.as_dict()))
        check(f"[{impl}] hand_back 后 pending 保持 open",
              st.get_active_pending() is not None, "")
        # 「那家店值得去吗」——指代+询问 → hand_back 保持 open
        st, it, p, v = one(impl, "那家店值得去吗")
        check(f"[{impl}] 「那家店值得去吗」→ hand_back 保持 open",
              v.verdict == V_HAND_BACK
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, str(v.as_dict()))
        # 名称匹配 + 询问标记 → 同样不消费
        st, it, p, v = one(impl, "海贝皇大排档（南京店）怎么样")
        check(f"[{impl}] 全名+询问标记 → hand_back 不确认消费",
              v.verdict == V_HAND_BACK
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, str(v.as_dict()))
        # plan 粗类但无目的地/无实体 → 依据不足 hand_back（保守）
        _, it, p, v = one(impl, "帮我排个行程", parse=ParseResult(
            text="帮我排个行程", intent="plan_create",
            intent_coarse="plan"))
        check(f"[{impl}] plan 无结构化依据 → hand_back 不取消",
              v.verdict == V_HAND_BACK
              and st.get_active_pending() is not None, str(v.as_dict()))


# ------------------------------------------------ Group D：明确新问题

def test_clear_new_question() -> None:
    for impl in ("memory", "sqlite"):
        st, it, p, v = one(impl, "介绍一下总统府", parse=pr_entity())
        check(f"[{impl}] 明确新查询 → cancel_new_question",
              v.verdict == V_CANCEL_NEW_QUESTION, str(v.as_dict()))
        check(f"[{impl}] act 执行取消成功",
              it.act(v).ok and st.get_active_pending() is None, "")
        p2 = st.create_pending("再问", "poi_qa", "query", SNAP2)
        check(f"[{impl}] 取消后可新建 pending（规则 3 链路）",
              st.get_active_pending().pending_id == p2.pending_id, "")
        _, it, _, v = one(impl, "明天天气怎么样", parse=pr_weather())
        check(f"[{impl}] 明确新意图（天气）→ cancel_new_question",
              v.verdict == V_CANCEL_NEW_QUESTION
              and v.evidence.get("intent_coarse") == "weather",
              str(v.as_dict()))
        # 泛化承接类 coarse 不单独构成依据（保守）
        _, it, _, v = one(impl, "嗯嗯", parse=ParseResult(
            text="嗯嗯", intent="smalltalk", intent_coarse="smalltalk"))
        check(f"[{impl}] 闲聊无依据 → hand_back 不取消",
              v.verdict == V_HAND_BACK, str(v.as_dict()))


# ------------------------------------------------ Group D2：显式重述请求（restate 唯一来源）

def test_restate_explicit_requests() -> None:
    """P2.3 验收补丁：restate 只留给显式重述请求。"""
    for impl in ("memory", "sqlite"):
        for ut in ("再说一遍有哪些店", "重新列一下", "再列一次选项",
                   "有哪些店"):
            st, it, p, v = one(impl, ut)
            check(f"[{impl}] 显式重述「{ut}」→ restate 保持 open",
                  v.verdict == V_RESTATE
                  and st.get_pending_by_id(p.pending_id).status ==
                  PENDING_OPEN, str(v.as_dict()))
        # 重述请求与消费/取消互斥：act 零状态变化
        st, it, p, v = one(impl, "重新列一下")
        check(f"[{impl}] restate 的 act 零状态变化",
              it.act(v) is None
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, "")
        # 到期后重述请求 → 如实告知过期（规则 5）
        clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st, it = make(impl, clock=clock)
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP2)
        advance(601)
        v = it.evaluate("再说一遍有哪些店", now=clock(),
                        session_pending_id=p.pending_id)
        check(f"[{impl}] 到期 + 重述请求 → expired_reply",
              v.verdict == V_EXPIRED_REPLY, str(v.as_dict()))


# ------------------------------------------------ Group E：目的地变化（最高优先级）

def test_destination_change() -> None:
    for impl in ("memory", "sqlite"):
        # 新目的地不在快照城市集合
        st, it, p, v = one(impl, "改去成都了，排海贝皇大排档",
                           parse=pr_dest("成都"))
        check(f"[{impl}] 目的地变化 → cancel_destination_changed",
              v.verdict == V_CANCEL_DEST_CHANGED
              and v.evidence.get("in_snapshot_cities") is False,
              str(v.as_dict()))
        check(f"[{impl}] act 执行后 pending=cancelled",
              it.act(v).ok and st.get_pending_by_id(
                  p.pending_id).status == "cancelled", "")
        # 冻结取舍：新目的地恰在快照城市集合内 → 仍按目的地变化
        _, it, p, v = one(impl, "改去南京了，排海贝皇大排档",
                          parse=pr_dest("南京"))
        check(f"[{impl}] 新目的地在快照城市集合内 → 仍按目的地变化"
              "（保守冻结）",
              v.verdict == V_CANCEL_DEST_CHANGED
              and v.evidence.get("in_snapshot_cities") is True,
              str(v.as_dict()))
        # 优先级：目的地变化覆盖名称匹配
        _, it, _, v = one(impl, "改去成都了，排海贝皇大排档",
                          parse=pr_dest("成都"))
        check(f"[{impl}] 目的地变化优先于名称匹配",
              v.verdict == V_CANCEL_DEST_CHANGED, str(v.as_dict()))
        # destination_new 但无规划写权限 → 不构成目的地变化
        _, it, _, v = one(impl, "南京那家", parse=pr_dest("南京",
                                                          writable=False))
        check(f"[{impl}] 无写权限的 destination_new 不触发目的地变化",
              v.verdict == V_CONSUME, str(v.as_dict()))


# ------------------------------------------------ Group F：过期处理

def test_expiry_handling() -> None:
    for impl in ("memory", "sqlite"):
        clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st, it = make(impl, clock=clock)
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP2)
        advance(601)
        v = it.evaluate("第一个", now=clock(),
                        session_pending_id=p.pending_id)
        check(f"[{impl}] 到期后选择 → expired_reply",
              v.verdict == V_EXPIRED_REPLY, str(v.as_dict()))
        check(f"[{impl}] act 显式登记 expired",
              it.act(v).ok and st.get_pending_by_id(
                  p.pending_id).status == "expired", "")
        # 到期 + 询问语境 → 同样如实告知过期
        clock2, advance2 = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st2, it2 = make(impl, clock=clock2)
        p2b = st2.create_pending("排海贝皇大排档", "plan_modify",
                                 "must_visit", SNAP2)
        advance2(601)
        v2 = it2.evaluate("那家店值得去吗", now=clock2(),
                          session_pending_id=p2b.pending_id)
        check(f"[{impl}] 到期 + 询问语境 → expired_reply",
              v2.verdict == V_EXPIRED_REPLY, str(v2.as_dict()))
        # 到期 + 取消话术 → cancel 优先于过期（open→cancelled）
        clock3, advance3 = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st3, it3 = make(impl, clock=clock3)
        p3 = st3.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP2)
        advance3(601)
        v3 = it3.evaluate("算了", now=clock3(),
                          session_pending_id=p3.pending_id)
        check(f"[{impl}] 到期后取消话术 → cancel_explicit",
              v3.verdict == V_CANCEL_EXPLICIT and it3.act(v3).ok
              and st3.get_pending_by_id(p3.pending_id).status ==
              "cancelled", "")
        # 到期 + 明确新问题 → 取消
        clock4, advance4 = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st4, it4 = make(impl, clock=clock4)
        p4 = st4.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP2)
        advance4(601)
        v4 = it4.evaluate("介绍一下总统府", pr_entity(), now=clock4(),
                          session_pending_id=p4.pending_id)
        check(f"[{impl}] 到期 + 明确新问题 → cancel_new_question",
              v4.verdict == V_CANCEL_NEW_QUESTION, str(v4.as_dict()))


# ------------------------------------------------ Group G：取消

def test_explicit_cancel() -> None:
    for impl in ("memory", "sqlite"):
        for ut in ("算了", "不选了", "取消"):
            st, it, p, v = one(impl, ut)
            check(f"[{impl}] 取消话术「{ut}」→ cancel_explicit",
                  v.verdict == V_CANCEL_EXPLICIT and it.act(v).ok
                  and st.get_pending_by_id(p.pending_id).status ==
                  "cancelled", str(v.as_dict()))
        # 取消与选择冲突 → 保守 hand_back，pending 保持 open
        st, it, p, v = one(impl, "算了，还是第一个吧")
        check(f"[{impl}] 取消+选择冲突 → 保守 hand_back 不消费",
              v.verdict == V_HAND_BACK
              and st.get_pending_by_id(p.pending_id).status ==
              PENDING_OPEN, str(v.as_dict()))


# ------------------------------------------------ Group H：重复选择识别

def test_duplicate_selection() -> None:
    for impl in ("memory", "sqlite"):
        st, it = make(impl, session="s1")
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP2)
        out = st.consume_pending(p.pending_id, "c1")
        check(f"[{impl}] 前置：消费成功", out.ok, str(out))
        v = it.evaluate("第一个")
        check(f"[{impl}] 窗口内纯序号 → duplicate_select",
              v.verdict == V_DUPLICATE_SELECT
              and v.candidate_id == "c1", str(v.as_dict()))
        check(f"[{impl}] duplicate 证据含已选名称/城市",
              v.evidence.get("selected_name") == "海贝皇大排档（南京店）"
              and v.evidence.get("selected_city") == "南京",
              str(v.evidence))
        check(f"[{impl}] duplicate 零状态变化（消费记录仍 1 条）",
              st._latest_consumption()["candidate_id"] == "c1"
              and st.get_pending_by_id(p.pending_id).status ==
              "consumed", "")
        v2 = it.evaluate("就这家")
        check(f"[{impl}] 窗口内「就这家」→ duplicate_select",
              v2.verdict == V_DUPLICATE_SELECT, str(v2.as_dict()))
        # 超窗口 → none 交回原链路
        clock, advance = fixed_clock(datetime(2026, 11, 18, 15, 0, 0))
        st2, it2 = make(impl, session="s1", clock=clock)
        pp = st2.create_pending("排海贝皇大排档", "plan_modify",
                                "must_visit", SNAP2)
        st2.consume_pending(pp.pending_id, "c1")
        advance(601)
        v3 = it2.evaluate("第一个", now=clock())
        check(f"[{impl}] 超窗口 → none 交回原链路",
              v3.verdict == V_NONE, str(v3.as_dict()))
        # 含新实体候选 → 非纯选择线索 → none
        v4 = it.evaluate("第一个吧，另外介绍一下总统府",
                         pr_entity())
        check(f"[{impl}] 纯线索+新实体 → none 交回原链路",
              v4.verdict == V_NONE, str(v4.as_dict()))
        # 非纯选择线索（城市限定指代）→ none
        v5 = it.evaluate("南京那家")
        check(f"[{impl}] 消费后城市指代 → none 不猜历史候选",
              v5.verdict == V_NONE, str(v5.as_dict()))
        # 序号映射越界 → none 绝不猜
        v6 = it.evaluate("第三个")
        check(f"[{impl}] 序号超出已消费快照 → none",
              v6.verdict == V_NONE, str(v6.as_dict()))
        # 无任何消费记录 → none
        st3, it3 = make(impl, session="s9")
        v7 = it3.evaluate("第一个")
        check(f"[{impl}] 无消费记录 → none",
              v7.verdict == V_NONE, str(v7.as_dict()))


def test_duplicate_cross_session_sqlite() -> None:
    """跨 Session consumption 互不可见（SQLite 双 store）。"""
    st_a, it_a = make("sqlite", session="session-A")
    st_b, it_b = make("sqlite", session="session-B")
    p = st_a.create_pending("排海贝皇大排档", "plan_modify",
                            "must_visit", SNAP2)
    st_a.consume_pending(p.pending_id, "c1")
    v = it_b.evaluate("第一个")
    check("Session B 看不到 A 的消费记录 → none",
          v.verdict == V_NONE, str(v.as_dict()))
    check("A 自己仍可识别重复选择",
          it_a.evaluate("第一个").verdict == V_DUPLICATE_SELECT, "")


# ------------------------------------------------ Group I：evaluate 纯度

def test_evaluate_purity() -> None:
    for impl in ("memory", "sqlite"):
        st, it = make(impl)
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP2)
        pr = ParseResult(text="改去南京了，排海贝皇大排档",
                         intent="plan_modify", intent_coarse="plan",
                         destination_new="南京",
                         destination_writable=True,
                         poi_candidates=[EntityCandidate(name="总统府",
                                                         city="南京")])
        pr_copy = copy.deepcopy(pr)
        ctx = {"destination": "上海", "slots": {"days": 2}}
        ctx_copy = copy.deepcopy(ctx)
        v = it.evaluate("改去南京了，排海贝皇大排档", pr, context=ctx)
        check(f"[{impl}] evaluate 不修改 ParseResult（深拷贝对比）",
              pr == pr_copy, "")
        check(f"[{impl}] evaluate 不修改 context（深拷贝对比）",
              ctx == ctx_copy, "")
        # evaluate 不执行 store 迁移：verdict 已产生但状态未变
        check(f"[{impl}] evaluate 后 pending 仍 open（未消费）",
              st.get_pending_by_id(p.pending_id).status == PENDING_OPEN,
              str(v.as_dict()))
        check(f"[{impl}] evaluate 不产生消费记录",
              st._latest_consumption() is None, "")
        # shadow_sink 异常不影响判定结果
        def bad_sink(rec):
            raise RuntimeError("sink 故障")
        it2 = PendingInterceptor(st, shadow_sink=bad_sink)
        v2 = it2.evaluate("第一个", now=None)
        check(f"[{impl}] sink 异常被隔离，判定不受影响",
              v2.verdict == V_CONSUME and len(it2.shadow_log) == 1,
              str(v2.as_dict()))


# ------------------------------------------------ Group J：act 诚实性

def test_act_honesty() -> None:
    for impl in ("memory", "sqlite"):
        st, it = make(impl)
        p = st.create_pending("排海贝皇大排档", "plan_modify",
                              "must_visit", SNAP2)
        v = it.evaluate("第一个")
        check(f"[{impl}] act 消费成功", it.act(v).ok, "")
        out2 = it.act(v)      # 同一 verdict 再执行 → 如实失败
        check(f"[{impl}] store 失败如实返回，不伪造成功",
              out2 is not None and not out2.ok, str(out2))
        check(f"[{impl}] 失败后不自动改选其他候选（消费记录仍 1 条）",
              st._latest_consumption()["candidate_id"] == "c1", "")
        # act 只接受 InterceptVerdict
        try:
            it.act({"verdict": V_CONSUME})
            check(f"[{impl}] act 拒绝非 verdict 输入", False, "未抛异常")
        except TypeError:
            check(f"[{impl}] act 拒绝非 verdict 输入", True, "")
        # 零状态 verdict → act 返回 None 且 store 不变
        st2, it2 = make(impl)
        p2 = st2.create_pending("x", "poi_qa", "query", SNAP2)
        v2 = it2.evaluate("南京有什么好吃的", pr_fuzzy())
        check(f"[{impl}] 零状态 verdict act 返回 None",
              it2.act(v2) is None
              and st2.get_active_pending() is not None, "")


# ------------------------------------------------ Group K：verdict 契约与影子

def test_verdict_contract_and_shadow() -> None:
    check("九类 verdict 常量互斥齐全",
          len(set(VERDICTS)) == 9
          and set(ZERO_STATE_VERDICTS) <= set(VERDICTS), str(VERDICTS))
    try:
        InterceptVerdict(verdict="unknown_verdict")
        check("非法 verdict 构造被拦截", False, "未抛异常")
    except ValueError:
        check("非法 verdict 构造被拦截", True, "")
    v = InterceptVerdict(verdict=V_CONSUME, pending_id="p1",
                         candidate_id="c1", reason="r",
                         evidence={"k": 1})
    d = v.as_dict()
    check("InterceptVerdict frozen：字段赋值抛异常",
          v.__dataclass_params__.frozen, "")
    check("as_dict 形状齐全",
          set(d) == {"verdict", "pending_id", "candidate_id", "reason",
                     "evidence"} and d["evidence"] == {"k": 1}, str(d))
    # 影子记录字段齐全
    st, it = make("memory")
    st.create_pending("排海贝皇大排档", "plan_modify", "must_visit",
                      SNAP2)
    it.evaluate("第一个")
    rec = it.shadow_log[-1]
    check("影子记录字段齐全",
          set(rec) == {"utterance", "verdict", "pending_id",
                       "candidate_id", "reason", "evidence", "ts"},
          str(rec))


# ------------------------------------------------ Group L：双实现生命周期对拍

def _scenario(impl: str) -> list:
    st, it = make(impl, session="sX")
    out = []
    st.create_pending("排海贝皇大排档", "plan_modify", "must_visit",
                      SNAP2)
    v = it.evaluate("第一个")
    out += [v.verdict, it.act(v).ok]
    out.append(it.evaluate("第一个").verdict)          # duplicate
    st.create_pending("换个", "poi_qa", "query", SNAP2)
    v = it.evaluate("介绍一下总统府", pr_entity())
    out += [v.verdict, it.act(v).ok,
            st.get_active_pending() is None]
    st.create_pending("再问", "poi_qa", "query", SNAP2)
    v = it.evaluate("算了")
    out += [v.verdict, it.act(v).ok]
    st.create_pending("again", "poi_qa", "query", SNAP2)
    v = it.evaluate("南京有什么好吃的", pr_fuzzy())
    out += [v.verdict, st.get_active_pending() is not None]
    v = it.evaluate("改去成都了，排海贝皇大排档", pr_dest("成都"))
    out += [v.verdict, it.act(v).ok, st.get_active_pending() is None]
    out.append(it.evaluate("今天天气如何", pr_weather()).verdict)
    return out


def test_dual_impl_parity() -> None:
    mem = _scenario("memory")
    sql = _scenario("sqlite")
    check("内存桩与 SQLite 全场景 verdict 序列逐位一致",
          mem == sql, f"mem={mem}\nsql={sql}")


# ------------------------------------------------ R-01（I-2）：S3 有界化

def _probe_nodes(obj) -> int:
    if isinstance(obj, dict):
        return sum(1 + _probe_nodes(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(1 + _probe_nodes(v) for v in obj)
    return 1


def test_r01_s3_bounded_shared_pool() -> None:
    """T24 / T29 / T38：``_v`` 与 gate 两写入点共用同一容器与容量——
    60 + 60 次 → 容器 100（不按 schema 各留 100）、计数 120、淘汰 20；
    stats 首次成功前 None，成功后就位且复用同一对象。"""
    from types import SimpleNamespace
    from travel_agent.orchestrator.orchestrator import Orchestrator
    from travel_agent.retention import SHADOW_CAPACITY
    st, it = make("memory")
    st.create_pending("排海贝皇大排档", "plan_modify", "must_visit", SNAP2)
    check("[T38·S3] 首次追加前 stats is None",
          it.shadow_stats is None, str(it.shadow_stats))
    check("[T38·S3] dead 初值 False", it.shadow_dead is False, "")
    for _ in range(60):
        it.evaluate("第一个")
    rt = SimpleNamespace(interceptor=it)
    for i in range(60):
        Orchestrator._shadow_gate(rt, "consume", "gate-%02d" % i,
                                  detail="d", pending_id="p1")
    check("[T24] 共用池封顶 100", len(it.shadow_log) == SHADOW_CAPACITY,
          str(len(it.shadow_log)))
    check("[T24] total_appended == 120",
          it.shadow_stats.total_appended == 120,
          str(it.shadow_stats.total_appended))
    check("[T24] total_evicted == 20",
          it.shadow_stats.total_evicted == 20,
          str(it.shadow_stats.total_evicted))
    check("[T29·S3] current_size == len",
          it.shadow_stats.current_size == len(it.shadow_log),
          f"{it.shadow_stats.current_size} != {len(it.shadow_log)}")
    ts = [r["ts"] for r in it.shadow_log if "ts" in r]
    check("[T24] _v 记录 ts 序不减（未被重排）",
          ts == sorted(ts), str(ts[:5]))
    check("[T24] gate 记录仍在同一容器（stage=gate 可见）",
          any(r.get("stage") == "gate" for r in it.shadow_log),
          str(it.shadow_log[-1]))
    stats_before = it.shadow_stats
    it._v(V_NONE, "u", 1)
    check("[T38·S3] 后续追加复用同一 stats 对象",
          it.shadow_stats is stats_before, "")
    check("[T38·S3] capacity 为冻结常量 100",
          it.shadow_stats.capacity == SHADOW_CAPACITY,
          str(it.shadow_stats.capacity))


def test_r01_s3_field_clip() -> None:
    """T25 / T37：7 键集不变；超长字段按冻结上限裁剪；非 str 字段收敛为
    占位；返回的 verdict 字段**原样**（裁剪只作用于记录副本）。"""
    from travel_agent.retention import LIM_ID, LIM_REASON, LIM_UTTERANCE
    st, it = make("memory")
    long_ut = "海" * 500
    v = it._v(V_HAND_BACK, long_ut, 1000, pending_id="p" * 100,
              reason="r" * 500, evidence={"k": "v"})
    rec = it.shadow_log[-1]
    check("[T25] 记录仍为 7 键",
          set(rec) == {"utterance", "verdict", "pending_id",
                       "candidate_id", "reason", "evidence", "ts"},
          str(sorted(rec)))
    check("[T25] utterance 裁剪到 200",
          len(rec["utterance"]) == LIM_UTTERANCE,
          str(len(rec["utterance"])))
    check("[T25] pending_id 裁剪到 64",
          len(rec["pending_id"]) == LIM_ID, str(len(rec["pending_id"])))
    check("[T25] reason 裁剪到 200",
          len(rec["reason"]) == LIM_REASON, str(len(rec["reason"])))
    check("[T25] verdict 原值", rec["verdict"] == V_HAND_BACK, "")
    check("[T25] 返回的 verdict 字段不变（未被裁剪回写）",
          v.pending_id == "p" * 100 and v.reason == "r" * 500,
          str(v.as_dict()))
    # ---- T37：非 str 注入 ----
    v2 = it._v(V_NONE, 123, 1001, reason=123)
    rec2 = it.shadow_log[-1]
    check("[T37·S3] utterance 非 str → <non-str:int>",
          rec2["utterance"] == "<non-str:int>", str(rec2["utterance"]))
    check("[T37·S3] reason 非 str → <non-str:int>",
          rec2["reason"] == "<non-str:int>", str(rec2["reason"]))
    check("[T37·S3] verdict 返回值保持原对象值",
          v2.reason == 123 and v2.verdict == V_NONE, str(v2.as_dict()))


def test_r01_s3_evidence_clip() -> None:
    """T26：evidence 深嵌套/超量裁剪生效（根恒 dict、键数/深度/预算有界），
    且 ``v.evidence`` 原样不被回写。"""
    from travel_agent.retention import (EVID_BUDGET_MARK, EVID_MAX_KEYS,
                                        EVID_NODE_BUDGET, EVID_STR_MAX)
    st, it = make("memory")
    deep = {"a": {"b": {"c": {"d": 1}}},
            "s": "x" * 300,
            "l": [[1, 2], [3]],
            "n": {"k%02d" % i: i for i in range(40)}}
    v = it._v(V_NONE, "u", 1, evidence=deep)
    rec = it.shadow_log[-1]["evidence"]
    check("[T26] 根仍为 dict", isinstance(rec, dict), str(type(rec)))
    check("[T26] 键数 ≤ 16", len(rec) <= EVID_MAX_KEYS, str(len(rec)))
    check("[T26] 字符串按 200 裁剪", len(rec["s"]) == EVID_STR_MAX,
          str(len(rec["s"])))
    check("[T26] 深度 3 的容器被摘要替换（<dict>）",
          rec["a"]["b"]["c"] == "<dict>", str(rec["a"]["b"]))
    check("[T26] 节点预算未被突破",
          _probe_nodes(rec) <= EVID_NODE_BUDGET, str(_probe_nodes(rec)))
    check("[T26] v.evidence 原样（未回写裁剪结果）",
          v.evidence == deep and v.evidence is deep, str(v.evidence))
    check("[T26] 记录 evidence 不是原对象（副本）",
          rec is not v.evidence, "")
    big = {"big": [{"k%02d" % j: "v" * 10 for j in range(20)}
                   for _ in range(10)]}
    it._v(V_NONE, "u2", 2, evidence=big)
    rec2 = it.shadow_log[-1]["evidence"]
    check("[T26] 预算耗尽 → 根层插入终止标记",
          rec2.get(EVID_BUDGET_MARK) is True, str(sorted(rec2))[:120])
    check("[T26] 预算耗尽后仍是有界 dict",
          isinstance(rec2, dict) and len(rec2) <= EVID_MAX_KEYS,
          str(len(rec2)))


def _ref_nodes(obj) -> int:
    """**冻结定义同构**的参考计数（评审 2026-11-23 第 7 项）。

    冻结语义（``retention._Budget`` 注释 + ``EVID_NODE_BUDGET``）：
    「dict 键值对 1 个 / list 元素 1 个；容器本身不计数」，预算终止标记
    ``EVID_BUDGET_MARK`` 不占业务节点预算。

    与既有 ``_probe_nodes`` 的区别：后者对每个标量叶子额外 +1，属**更严
    但不同源**的上界探针；本函数只按定义计数，用于等值断言。
    """
    from travel_agent.retention import EVID_BUDGET_MARK
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == EVID_BUDGET_MARK:
                continue
            n += 1
            n += _ref_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            n += 1
            n += _ref_nodes(v)
    return n


def test_r01_s3_evidence_budget_definition() -> None:
    """T26 补强（评审第 7 项）：断言与冻结的 128 节点定义**同构**。

    ① 深嵌套耗尽且根键数 < 16（不触发让位删除）→ 消耗节点数**恰等于**
       128：由实现「循环头 budget.used >= 128 才返回 exhausted」可知，
       进入 exhausted 时 used 必为 128 整，输出节点数与之一致，故等值
       断言同时否证「少算」与「多算」。
    ② 宽根 + 深末值：触发「满 16 键末位让位」，被删键的整棵子树不计入，
       因此只断言 ≤128 + 标记存在 + 让位后仍 ≤16 键。
    ③ 未耗尽：无标记，且节点数等于定义口径的确定值。
    """
    from travel_agent.retention import (EVID_BUDGET_MARK, EVID_MAX_KEYS,
                                        EVID_NODE_BUDGET)
    st, it = make("memory")

    # ① 深嵌套耗尽、无让位删除 → 等值 128
    deep = {"big": [{"k%02d" % j: "v" * 10 for j in range(20)}
                    for _ in range(10)]}
    it._v(V_NONE, "u", 1, evidence=deep)
    rec = it.shadow_log[-1]["evidence"]
    check("[T26·同构①] 预算终止标记存在",
          rec.get(EVID_BUDGET_MARK) is True, str(sorted(rec))[:120])
    check("[T26·同构①] 根键数未达 16（未触发让位删除）",
          len(rec) - 1 < EVID_MAX_KEYS, str(len(rec)))
    check("[T26·同构①] 消耗节点数恰为 128（等值，非上界）",
          _ref_nodes(rec) == EVID_NODE_BUDGET, str(_ref_nodes(rec)))

    # ② 宽根 + 深末值 → 触发让位
    wide = {("k%02d" % i): 0 for i in range(15)}
    wide["m"] = [[j for j in range(16)] for _ in range(16)]
    it._v(V_NONE, "u", 2, evidence=wide)
    rec2 = it.shadow_log[-1]["evidence"]
    check("[T26·同构②] 标记存在", rec2.get(EVID_BUDGET_MARK) is True,
          str(sorted(rec2))[:120])
    check("[T26·同构②] 让位后仍 ≤16 键（根恒 dict）",
          isinstance(rec2, dict) and len(rec2) <= EVID_MAX_KEYS,
          str(len(rec2)))
    check("[T26·同构②] 让位丢弃整棵子树（业务节点仅剩 15 个标量）",
          _ref_nodes(rec2) == 15, str(_ref_nodes(rec2)))
    check("[T26·同构②] 未突破预算", _ref_nodes(rec2) <= EVID_NODE_BUDGET,
          str(_ref_nodes(rec2)))

    # ③ 未耗尽：定义口径的确定值
    ok = {"s": ["x"] * 40, "n": {"a": 1}}
    it._v(V_NONE, "u", 3, evidence=ok)
    rec3 = it.shadow_log[-1]["evidence"]
    check("[T26·同构③] 无终止标记", EVID_BUDGET_MARK not in rec3,
          str(sorted(rec3)))
    check("[T26·同构③] list 元素封顶 16", len(rec3["s"]) == 16,
          str(len(rec3["s"])))
    check("[T26·同构③] 节点数 == 1+16+1+1（定义口径）",
          _ref_nodes(rec3) == 19, str(_ref_nodes(rec3)))
    check("[T26·同构③] v.evidence 原样（裁剪只作用于副本）",
          it.shadow_log[-1]["evidence"] is not ok, "")


def test_r01_s3_sink_only_after_append() -> None:
    """修订 1 验收（四情形）：① 追加成功 → sink 1 次；② RIE → dead=True、
    sink 0 次；③ 普通 Exception → 不崩、sink 0 次；④ dead 后再调用 →
    append 与 sink 均 0 次。"""
    import travel_agent.pending_interceptor as pi_mod
    from travel_agent.retention import RetentionInvariantError
    st, _unused = make("memory")
    orig = pi_mod.append_bounded
    calls = []

    def sink(rec):
        calls.append(rec)

    # ① 成功
    it1 = PendingInterceptor(st, shadow_sink=sink)
    v1 = it1._v(V_NONE, "u1", 1)
    check("[T-S3Sink①] 追加成功 → sink 恰 1 次", len(calls) == 1,
          str(len(calls)))
    check("[T-S3Sink①] 记录已入容器", len(it1.shadow_log) == 1, "")
    check("[T-S3Sink①] stats 已提交",
          it1.shadow_stats is not None
          and it1.shadow_stats.total_appended == 1, str(it1.shadow_stats))
    check("[T-S3Sink①] 返回 verdict 正常", v1.verdict == V_NONE, "")

    # ② RIE
    def rie(*a, **kw):
        raise RetentionInvariantError("mutation")

    pi_mod.append_bounded = rie
    try:
        it2 = PendingInterceptor(st, shadow_sink=sink)
        v2 = it2._v(V_NONE, "u2", 2)
    finally:
        pi_mod.append_bounded = orig
    check("[T-S3Sink②] RIE → dead=True", it2.shadow_dead is True, "")
    check("[T-S3Sink②] RIE → sink 0 次（总数仍 1）", len(calls) == 1,
          str(len(calls)))
    check("[T-S3Sink②] RIE → 容器无新增", len(it2.shadow_log) == 0, "")
    check("[T-S3Sink②] RIE → 业务判定不受影响", v2.verdict == V_NONE, "")

    # ③ 普通 Exception
    def boom(*a, **kw):
        raise RuntimeError("append boom")

    pi_mod.append_bounded = boom
    try:
        it3 = PendingInterceptor(st, shadow_sink=sink)
        v3 = it3._v(V_NONE, "u3", 3)
    finally:
        pi_mod.append_bounded = orig
    check("[T-S3Sink③] 普通异常整轮不崩（verdict 正常返回）",
          v3.verdict == V_NONE, "")
    check("[T-S3Sink③] 普通异常 → sink 0 次", len(calls) == 1,
          str(len(calls)))
    check("[T-S3Sink③] 普通异常不置 dead", it3.shadow_dead is False, "")
    check("[T-S3Sink③] 普通异常 → 容器无新增", len(it3.shadow_log) == 0, "")

    # ④ dead 后再调用
    counter = {"n": 0}

    def counting(*a, **kw):
        counter["n"] += 1
        raise AssertionError("dead 后不得再调用 append_bounded")

    pi_mod.append_bounded = counting
    try:
        v4 = it2._v(V_NONE, "u4", 4)
    finally:
        pi_mod.append_bounded = orig
    check("[T-S3Sink④] dead 后 append 调用 0 次", counter["n"] == 0,
          str(counter))
    check("[T-S3Sink④] dead 后 sink 仍 0 次（总数仍 1）", len(calls) == 1,
          str(len(calls)))
    check("[T-S3Sink④] dead 后返回值正常", v4.verdict == V_NONE, "")


def test_r01_s3_dead_persistent() -> None:
    """T45（S3）：注入 RIE 后 dead 跨轮保持，后续 3 轮 append 与 sink 均
    0 次；`_v` 返回值字段与干净实例逐字段一致（业务零影响）。"""
    import travel_agent.pending_interceptor as pi_mod
    from travel_agent.retention import RetentionInvariantError
    st, it = make("memory")
    calls = []
    it.shadow_sink = lambda rec: calls.append(rec)
    orig = pi_mod.append_bounded

    def rie(*a, **kw):
        raise RetentionInvariantError("commit")

    pi_mod.append_bounded = rie
    try:
        v_bad = it._v(V_NONE, "u", 1, pending_id="p1")
    finally:
        pi_mod.append_bounded = orig
    check("[T45·S3] RIE 后 dead=True", it.shadow_dead is True, "")
    check("[T45·S3] RIE 轮 sink 0 次", len(calls) == 0, str(len(calls)))

    counter = {"n": 0}

    def counting(*a, **kw):
        counter["n"] += 1
        raise AssertionError("dead 后不得再调用 append_bounded")

    pi_mod.append_bounded = counting
    try:
        for i in range(3):
            it._v(V_NONE, "u", 100 + i, pending_id="p1")
    finally:
        pi_mod.append_bounded = orig
    check("[T45·S3] 后续 3 轮 append 调用 0 次", counter["n"] == 0,
          str(counter))
    check("[T45·S3] 后续 3 轮 sink 调用 0 次", len(calls) == 0,
          str(len(calls)))
    check("[T45·S3] 容器仍为空", len(it.shadow_log) == 0, "")
    st2, it2 = make("memory")
    v_ok = it2._v(V_NONE, "u", 1, pending_id="p1")
    check("[T45·S3] 返回值与干净实例逐字段一致",
          v_bad.as_dict() == v_ok.as_dict(), str(v_bad.as_dict()))


def main() -> int:
    for fn in (test_order_selection, test_name_city_selection,
               test_conservative_red_lines, test_clear_new_question,
               test_restate_explicit_requests,
               test_destination_change, test_expiry_handling,
               test_explicit_cancel, test_duplicate_selection,
               test_duplicate_cross_session_sqlite,
               test_evaluate_purity, test_act_honesty,
               test_verdict_contract_and_shadow, test_dual_impl_parity,
               # ---- R-01（I-2）：S3 有界化（5 个）----
               test_r01_s3_bounded_shared_pool,
               test_r01_s3_field_clip,
               test_r01_s3_evidence_clip,
               test_r01_s3_evidence_budget_definition,
               test_r01_s3_sink_only_after_append,
               test_r01_s3_dead_persistent):
        fn()
    print(f"\npending interceptor: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
