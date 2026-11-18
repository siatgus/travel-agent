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


def main() -> int:
    for fn in (test_order_selection, test_name_city_selection,
               test_conservative_red_lines, test_clear_new_question,
               test_restate_explicit_requests,
               test_destination_change, test_expiry_handling,
               test_explicit_cancel, test_duplicate_selection,
               test_duplicate_cross_session_sqlite,
               test_evaluate_purity, test_act_honesty,
               test_verdict_contract_and_shadow, test_dual_impl_parity):
        fn()
    print(f"\npending interceptor: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
