"""多轮对话的行为测试：把提示词里的追问规则变成可断言项。

这些断言对应的都是提示词原文，不是我自己编的期望：

    第 47 行  缺目的地或日期 → 必须追问，一次只问最关键的 1~2 个，并给默认建议
    第 48 行  缺预算 → 不追问，给三档估算请用户选
    第 50 行  追问最多连续 2 轮；第 2 轮仍缺 → 带假设出方案并标注

之前这些规则只在提示词里写着，代码一条没实现（find_missing 打印一句就 return）。
所以这里的每条断言都是一条"承诺兑现检查"。

顺带覆盖了三个只有多轮才会暴露的崩溃/错值：
- 日期只到月份 → 编排器 KeyError
- 单人出行 → 酒店空结果越界
- 第二轮只说"4 天" → 把第一轮的目的地冲成空

运行：python test_dialogue.py    失败时退出码为 1。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent.dialogue import Session, PreferenceMemory, serviceable_cities

TODAY = date(2026, 10, 29)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def session() -> Session:
    return Session(today=TODAY)


# ------------------------------------------------------------------ 一次说全


def test_one_shot() -> None:
    s = session()
    r = s.turn("我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶，我老婆不吃辣")
    ok = r.mode == "plan" and r.ask_rounds == 0 and r.plan is not None
    check("信息完整时一次出方案，不追问", ok, f"mode={r.mode} ask={r.ask_rounds}")


def test_one_shot_no_assumption() -> None:
    s = session()
    r = s.turn("11月8号从杭州去大理玩4天，2人，预算一共6000")
    ok = r.mode == "plan" and not r.assumptions
    check("信息齐备时不产生任何假设", ok, f"assumptions={r.assumptions}")


# ------------------------------------------------------------------ 追问规则


def test_ask_at_most_two_fields() -> None:
    s = session()
    r = s.turn("想去云南")
    ok = r.mode == "clarify" and len(r.asked) <= 2
    check("一次只问 1~2 个槽位（第 47 行）", ok, f"asked={r.asked}")


def test_ask_has_default_hint() -> None:
    s = session()
    r = s.turn("11月8号出发，2人，预算6000")
    ok = r.mode == "clarify" and "（" in r.question and "）" in r.question
    check("每个追问都带默认建议（第 47 行）", ok, f"q={r.question[:40]}")


def test_two_rounds_then_plan() -> None:
    # 缺口逐个补齐：每轮只问一件，问满就带假设出方案，不会无限追问
    s = session()
    r1 = s.turn("想去大理")
    r2 = s.turn("你看着办")
    rounds = 2
    r = r2
    while r.mode == "clarify" and rounds < 4:
        r = s.turn("你看着办")
        rounds += 1
    ok = (r1.mode == "clarify" and r.mode == "plan"
          and r.plan is not None and r.assumptions and rounds <= 3)
    check("缺口逐个问、问满即带假设出方案，不反复追问（第 50 行）", ok,
          f"{r1.mode}/{r.mode} rounds={rounds} assumptions={r.assumptions}")


def test_no_plan_when_city_has_no_data() -> None:
    """目的地没数据时宁可说清，也不能拿别的城市顶上去。"""
    s = session()
    r1 = s.turn("想去云南")
    r2 = s.turn("你看着办")
    r3 = s.turn("随便，都行")
    ok = (r1.mode == "clarify" and r2.mode == "clarify" and r3.mode == "clarify"
          and r3.plan is None)
    check("目的地无数据时不硬凑方案（旧逻辑会换成别的城市）", ok,
          f"{r1.mode}/{r2.mode}/{r3.mode}")


def test_no_repeat_question() -> None:
    s = session()
    r1 = s.turn("想去云南")
    r2 = s.turn("嗯")
    ok = r1.mode == r2.mode == "clarify" and r1.question != r2.question
    check("第二轮不重复问同一句", ok, f"q1={r1.question[:20]} q2={r2.question[:20]}")


def test_assumption_marked_in_text() -> None:
    s = session()
    r = s.turn("想去大理")
    for _ in range(3):
        if r.mode == "plan":
            break
        r = s.turn("都行")
    ok = r.mode == "plan" and r.assumptions and "假设：" in r.render
    check("假设必须写进输出文本（第 50 行）", ok, f"假设={r.assumptions}")


# ------------------------------------------------------------------ 槽位合并


def test_slots_not_clobbered() -> None:
    s = session()
    s.turn("11月8号去大理玩4天，2人")
    r = s.turn("预算6000")
    ok = (r.slots["destination"] == "大理" and r.slots["days"] == 4
          and r.slots["party_size"] == 2 and r.slots["budget_total"] == 6000)
    check("后续轮次只更新提到的字段，不冲掉已有槽位", ok,
          f"dest={r.slots.get('destination')} days={r.slots.get('days')} "
          f"pax={r.slots.get('party_size')}")


def test_per_person_tracks_party_size() -> None:
    s = session()
    s.turn("11月8号去大理玩3天，一人3000")
    r = s.turn("2人")
    ok = r.slots["budget_per_person"] == 3000 and r.slots["budget_total"] == 6000
    check("先说人均后改人数 → 总额跟着重算", ok,
          f"per={r.slots.get('budget_per_person')} total={r.slots.get('budget_total')}")


def test_explicit_total_is_sticky() -> None:
    s = session()
    s.turn("11月8号去大理玩3天，预算一共6000")
    r = s.turn("2人")
    ok = r.slots["budget_total"] == 6000
    check("用户明说的总额不因人数变化而改动", ok,
          f"total={r.slots.get('budget_total')}")


def test_change_mind() -> None:
    s = session()
    s.turn("11月8号去大理玩4天，2人，预算6000")
    r = s.turn("算了，改去丽江，玩5天")
    ok = r.slots["destination"] == "丽江" and r.slots["days"] == 5
    check("用户改主意时覆盖目的地与天数", ok,
          f"dest={r.slots.get('destination')} days={r.slots.get('days')}")


def test_clock_precise_reschedule_honest() -> None:
    """分钟级锚点改排：能落地的真平移时间轴，落不了的如实说明。"""
    from travel_agent import intent
    from travel_agent.composer import _tmin
    check("分钟时刻+改排动词被识别",
          intent.wants_clock_schedule("把 Day 3 午餐排到 15:20 结束后接 90 分钟午休"))
    check("餐目标+时刻无需规划动词",
          intent.wants_clock_schedule("每天晚餐7点开始"))
    check("整点说法不误伤",
          not intent.wants_clock_schedule("下午3点出发去大理"))
    check("班次时刻不误入", not intent.wants_clock_schedule("15:20 的高铁"))
    check("只改午休时长也被识别",
          intent.wants_clock_schedule("把午休改成2小时"))
    # 解析：时刻归一化
    a = intent.parse_clock_anchor("把 Day 3 午餐排到 15:20 结束后接 90 分钟午休")
    check("锚点解析出天/目标/时刻/时长",
          a and a["day"] == 3 and a["target"] == "午餐" and a["mode"] == "end"
          and a["minute"] == 15 * 60 + 20 and a["nap_min"] == 90, str(a))
    a2 = intent.parse_clock_anchor("每天晚餐7点开始")
    check("晚餐整点按晚上归一（7点=19:00）",
          a2 and a2["every_day"] and a2["minute"] == 19 * 60, str(a2))
    check("无目标节点不硬猜", intent.parse_clock_anchor("15:20 的高铁") is None)

    s = session()
    s.turn("11月8号去大理玩4天，2人，预算6000")
    before = s.plan_made and s.last_plan is not None
    orig_day3 = [_tmin(n["time"]) for n in s.last_plan["days_plan"][2]["nodes"]]
    r = s.turn("把 Day 3 午餐排到 15:20 结束")
    check("锚点改排返回新方案",
          r.mode == "plan" and "已按你的时刻要求调整" in r.render, f"mode={r.mode}")
    day3 = s.last_plan["days_plan"][2]["nodes"]
    lunch = next(n for n in day3 if n["type"] == "restaurant"
                 and 11 * 60 <= _tmin(n["time"]) < 15 * 60 + 30)
    check("午餐结束精确落在 15:20",
          _tmin(lunch["time"]) + lunch["duration_min"] == 15 * 60 + 20,
          f"{lunch['time']}+{lunch['duration_min']}")
    new_day3 = [_tmin(n["time"]) for n in day3]
    check("平移保持相邻间隙（交通耗时不丢）",
          all(b - a_ == d - c for a_, b, c, d
              in zip(orig_day3, orig_day3[1:], new_day3, new_day3[1:])))
    day2 = [_tmin(n["time"]) for n in s.last_plan["days_plan"][1]["nodes"]]
    check("Day 2 时间轴不受影响（未被整体平移）", day2 != new_day3)
    r2 = s.turn("把 Day 3 午餐排到 15:20 结束")
    check("重复要求如实说已满足",
          r2.mode == "answer" and "时间轴没动" in r2.render, r2.render[:60])
    r3 = s.turn("把第 9 天午餐排到 12 点结束")
    check("越界天数如实拒绝",
          r3.mode == "answer" and "没有第 9 天" in r3.render, r3.render[:60])
    r4 = s.turn("按 15:20 精确调整一下行程")
    check("解析不出目标仍走诚实提示",
          r4.mode == "answer" and "哪一餐" in r4.render, r4.render[:60])
    check("普通改排不受影响",
          s.turn("去掉苍山").mode == "plan" and before)


# ------------------------------------------------------------------ 预算三档


def test_budget_tiers_when_absent() -> None:
    s = session()
    r = s.turn("11月8号去大理玩4天，2人")
    ok = (r.mode == "plan" and r.budget_tiers is not None
          and set(r.budget_tiers) == {"经济", "舒适", "品质"}
          and all(t["total"] > 0 for t in r.budget_tiers.values()))
    check("缺预算不追问，改为给三档估算（第 48 行）", ok,
          f"tiers={list((r.budget_tiers or {}))}")


def test_no_tiers_when_budget_given() -> None:
    s = session()
    r = s.turn("11月8号去大理玩4天，2人，预算6000")
    check("用户给了预算就不再给三档", r.mode == "plan" and r.budget_tiers is None)


# ------------------------------------------------------------------ 数据覆盖


def test_province_asks_city() -> None:
    s = session()
    r = s.turn("下个月想去云南玩几天")
    ok = (r.mode == "clarify" and "destination" in r.asked
          and any(c in r.question for c in serviceable_cities()))
    check("省级目的地追问具体城市并列出可选城市", ok, f"q={r.question[:50]}")


def test_unsupported_destination_never_replaced() -> None:
    """旧行为：无数据城市被替换成「数据最全的城市」并静默排方案。
    这正是「问北京天气却得到上海行程」的根源，现在必须改成如实说明。"""
    s = session()
    r = s.turn("想去云南玩几天")
    for u in ("随便", "都行"):
        r = s.turn(u)
    ok = (r.mode == "clarify" and r.plan is None
          and any(c in r.question for c in serviceable_cities()))
    check("无数据城市不被替换，改为说明可服务城市", ok,
          f"mode={r.mode} dest={r.slots.get('destination')} q={r.question[:40]}")


# ------------------------------------------------------------------ 崩溃回归


def test_month_only_date() -> None:
    s = session()
    r = s.turn("下个月想去大理玩4天，2人，预算6000")
    ok = (r.mode == "plan" and r.slots["date_range"].get("start")
          and any("第二个周六" in a for a in r.assumptions))
    check("日期只到月份不崩溃，折算后标注（原 KeyError）", ok,
          f"date={r.slots.get('date_range')}")


def test_solo_traveler() -> None:
    s = session()
    r = s.turn("11月8号去大理玩3天，一人3000")
    ok = r.mode == "plan" and r.plan is not None and len(r.plan["days_plan"]) == 3
    check("单人出行不崩溃（原酒店空结果越界）", ok,
          f"mode={r.mode}")


def test_hotel_relax_notice() -> None:
    # 放宽重试由「预算×8% 的价格上限筛空」触发。mock 数据最低房价 260 元，
    # 预算 3000（上限 240 元/晚）即可筛空；换成真实数据后大理古城客栈最低 77 元，
    # 上限需压到 77 元以下（预算 < 963 元）才会触发同一行为路径。
    s = session()
    r = s.turn("11月8号去大理玩3天，一人500")
    ok = any("放宽" in a for a in r.assumptions)
    check("酒店价格上限过严时放宽并告知", ok, f"假设={r.assumptions}")


# ------------------------------------------------------------------ P0 抽取修复

def test_chinese_numerals_and_route() -> None:
    s = session()
    r = s.turn("一个人邯郸到上海，下个月一号出发，五天时间，预算5000")
    ok = (r.slots.get("days") == 5 and r.slots.get("origin") == "邯郸"
          and r.slots.get("budget_total") == 5000)
    check("中文数字天数与无「从」字出发地被识别（P0）", ok,
          f"days={r.slots.get('days')} origin={r.slots.get('origin')} "
          f"total={r.slots.get('budget_total')}")


def test_next_month_exact_day() -> None:
    from travel_agent import agent
    slots = agent.extract_slots_rules("下个月一号去大理玩4天", today=TODAY)
    dr = slots["date_range"]
    ok = dr.get("month") == "2026-11" and dr.get("day") == 1 and dr.get("exact")
    check("「下个月一号」解析出具体日（P0）", ok, f"date_range={dr}")


def test_exact_day_no_saturday_assumption() -> None:
    s = session()
    r = s.turn("下个月一号去大理玩4天，2人，预算6000")
    ok = (r.mode == "plan"
          and r.slots["date_range"].get("start") == "2026-11-01"
          and not any("第二个周六" in a for a in r.assumptions))
    check("给了具体日子不再折算第二个周六（P0）", ok,
          f"date={r.slots.get('date_range')} 假设={r.assumptions}")


def test_origin_without_route_noted() -> None:
    s = session()
    r = s.turn("从邯郸出发去大理，11月8号走，玩4天，预算8000")
    if r.mode == "clarify":          # 人数没说会先问一次
        r = s.turn("两个人")
    ok = r.mode == "plan" and any(
        "邯郸" in a and ("暂无班次" in a or "大交通" in a) for a in r.assumptions)
    check("出发地无班次数据时如实标注，不静默按默认城市（P0）", ok,
          f"假设={r.assumptions}")


def test_session_uses_memory_prefs() -> None:
    with tempfile.TemporaryDirectory() as d:
        mem = PreferenceMemory(Path(d) / "p.json")
        mem.set("hard_prefs", ["不吃辣"])
        s = Session(today=TODAY, memory=mem)
        r = s.turn("11月8号去大理玩4天，2人，预算6000")
        ok = "不吃辣" in (r.slots.get("hard_prefs") or [])
        check("会话挂上偏好记忆后硬约束自动并入（P0）", ok,
              f"prefs={r.slots.get('hard_prefs')}")


# ------------------------------------------------------------------ 偏好记忆


def test_preference_memory_persists() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "prefs.json"
        m1 = PreferenceMemory(path)
        m1.set("hard_prefs", ["不吃辣", "忌高海拔"])
        m2 = PreferenceMemory(path)          # 新实例，模拟进程重启
        slots = m2.apply_to({"hard_prefs": ["带幼儿"]})
        ok = set(slots["hard_prefs"]) == {"不吃辣", "忌高海拔", "带幼儿"}
        check("偏好跨进程持久化并并入新槽位", ok, f"prefs={slots.get('hard_prefs')}")


def test_context_retention_and_switch() -> None:
    """跨轮上下文：槽位保留 / 显式覆盖 / 指代切换 / 模糊回答降级接聊过的城市。"""
    s = session()
    r1 = s.turn("11月8号去大理玩4天")
    s.turn("2个人，预算5000")
    r2 = s.turn("预算改成4000，改成5天")
    check("上下文：只覆盖被改字段，目的地不动",
          r2.mode == "plan" and r2.slots.get("budget_total") == 4000
          and r2.slots.get("days") == 5 and r2.slots.get("destination") == "大理")
    r3 = s.turn("还是去丽江吧")
    check("上下文：指代切换目的地，其余槽位保留",
          r3.mode == "plan" and r3.slots.get("destination") == "丽江"
          and r3.slots.get("days") == 5 and r3.slots.get("budget_total") == 4000)
    s2 = session()
    s2.turn("11月8号去大理玩4天")
    s2.turn("2个人，预算5000")
    s2.turn("还是去丽江吧")
    s2.turn("新话题：想去云南玩")            # 触发无数据说明
    r4 = s2.turn("就说大概吧")
    check("上下文：无数据城市不擅自换成别的城市，改为说明",
          r4.mode == "clarify" and r4.plan is None
          and r4.slots.get("destination") == "云南",
          f"mode={r4.mode} dest={r4.slots.get('destination')}")
    check("上下文：第二次追问换说法并给出实际信息（天气/可排城市）",
          ("天气" in r4.question or "能排" in r4.question)
          and r4.question != s2.history[-3]["content"],
          r4.question[:60])
    _ = r1


# ------------------------------------------------------------------ 追问节奏

# 这批断言对应的是一次真实体感事故：
#   用户：一个人去大理玩
#   系统：什么时候出发、打算玩几天？        ← 一次抛两件，像表单
#   用户：你不问我从哪里出发吗              ← 在反问，并不是回答
#   系统：## 大理 4 天行程 …                ← 直接出方案，还写"哪里-大理 暂无班次"
# 根因是"问过"被当成"答过了"：问过的字段进 asked_fields，第二轮就没有可问的了，
# 于是"没有缺口"直接走到出方案。下面每条都是针对这个塌陷的防线。


def test_ask_one_thing_at_a_time() -> None:
    s = session()
    r = s.turn("一个人去大理玩")
    check("追问一次只问一件（不再一次抛两件）",
          r.mode == "clarify" and r.asked == ["date_range"],
          f"asked={r.asked} q={r.question[:40]}")


def test_backquestion_does_not_trigger_plan() -> None:
    s = session()
    s.turn("一个人去大理玩")
    r = s.turn("你不问我从哪里出发吗")
    check("用户在反问时不出方案，先正面回应再问回去",
          r.mode == "clarify" and r.plan is None,
          f"mode={r.mode} q={r.question[:50]}")
    check("反问里的「哪里」不会被当成出发地",
          s.slots.get("origin") is None, f"origin={s.slots.get('origin')}")


def test_retry_changes_wording() -> None:
    s = session()
    r1 = s.turn("一个人去大理玩")
    r2 = s.turn("嗯")
    check("没答到点上 → 再问一次，且换说法不复读",
          r2.mode == "clarify" and r2.asked == ["date_range"]
          and r2.question != r1.question,
          f"q1={r1.question[:24]} q2={r2.question[:24]}")


def test_assume_after_three_asks() -> None:
    s = session()
    s.turn("一个人去大理玩")
    s.turn("嗯")
    s.turn("嗯")
    r = s.turn("嗯")
    check("连问 3 轮仍没答 → 按假设出方案，不无限追问",
          r.mode == "plan" and r.plan is not None,
          f"mode={r.mode} ask_rounds={r.ask_rounds}")
    check("假设必须写清补了什么（天数/出发地）",
          any("4 天" in a for a in r.assumptions)
          and any("出发地" in a for a in r.assumptions),
          f"假设={r.assumptions}")


def test_vague_answer_stops_asking() -> None:
    s = session()
    s.turn("一个人去大理玩")
    r2 = s.turn("还没定")
    check("时间答「还没定」→ 不再追同一件，改问下一件",
          r2.mode == "clarify" and r2.asked == ["days"],
          f"asked={r2.asked}")
    r3 = s.turn("随便")
    check("再说「随便」→ 直接按假设排，不把模糊当没听见",
          r3.mode == "plan" and any("还没定" in a for a in r3.assumptions),
          f"mode={r3.mode} 假设={r3.assumptions}")


def test_origin_default_is_stated() -> None:
    """没说出发地就按没有出发地排：不填默认城市，明说大交通未含。"""
    s = session()
    # 人数没说会被问一次（默认 1 人算不出房型），答完才出方案
    r = s.turn("11月8号去大理玩4天")
    if r.mode == "clarify":
        r = s.turn("两个人")
    text = r.render
    check("没说出发地时不出默认城市，概要写「未指定」且假设说明大交通未含",
          r.mode == "plan" and "未指定" in text and "杭州" not in text
          and any("出发地" in a and "大交通" in a for a in r.assumptions),
          f"假设={r.assumptions} text={text[:160]}")
    check("没说出发地时不算往返交通费（预算里没有班次项）",
          all("往 " not in it["label"] and "返 " not in it["label"]
              for it in r.plan["budget"]["breakdown"]),
          str(r.plan["budget"]["breakdown"]))
    check("没说出发地时不查班次（trace 里没有 search_transport）",
          all(t["tool"] != "search_transport" for t in r.trace),
          str([t["tool"] for t in r.trace]))

    s2 = session()
    r2 = s2.turn("11月8号从成都出发去大理玩4天")
    check("用户说了出发地就照用，不再出缺省假设",
          r2.slots.get("origin") == "成都"
          and not any("未提供出发地" in a for a in r2.assumptions),
          f"origin={r2.slots.get('origin')} 假设={r2.assumptions}")


def test_plan_states_trip_facts() -> None:
    """方案开头必须写清「从哪走、几个人、预算多少」——用户不该自己反推。"""
    s = session()
    r = s.turn("11月8号从成都出发去大理玩4天，3人，预算9000")
    text = r.render
    ok = (r.mode == "plan" and "出行概要" in text
          and "成都 → 大理" in text and "3 人" in text
          and "预算合计" in text and "9,000" in text
          and "4 天 3 晚" in text)
    check("方案写明出发地/人数/预算/天数（出行概要）", ok, text[:220])

    # 出发地没说就明说未指定，不能冒出默认城市
    s2 = session()
    r2 = s2.turn("11月8号去大理玩4天，2人，预算6000")
    check("未指定出发地在概要与假设里都标注",
          "未指定" in r2.render and "杭州" not in r2.render
          and any("出发地" in a for a in r2.assumptions),
          r2.render[:200])


# ------------------------------------------------------------------ 行程修改（必去/排除）


def test_extract_must_and_avoid() -> None:
    from travel_agent import agent
    d = agent.extract_slots_rules("我想去上海迪士尼玩，我不想去豫园", today=TODAY)
    check("一句话同时抽出必去与排除",
          d["must_visit"] == ["上海迪士尼"] and d["avoid"] == ["豫园"],
          f"{d['must_visit']} / {d['avoid']}")


def test_avoid_is_not_destination() -> None:
    from travel_agent import agent
    d = agent.extract_slots_rules("我不想去豫园，太挤了", today=TODAY)
    check("「不想去X」不把X当目的地，只进排除清单",
          d["destination"] is None and d["avoid"] == ["豫园"],
          f"dest={d['destination']} avoid={d['avoid']}")


def test_avoid_wins_over_must() -> None:
    from travel_agent.dialogue import merge_slots, _explicit_fields
    from travel_agent import agent
    base = {"must_visit": ["豫园"]}
    d = agent.extract_slots_rules("我不想去豫园", today=TODAY)
    merged, _ = merge_slots(base, d, _explicit_fields("我不想去豫园", d), False)
    check("后说的排除会从必去清单里拿掉同名景点",
          merged["avoid"] == ["豫园"] and merged["must_visit"] == [],
          f"must={merged['must_visit']} avoid={merged['avoid']}")


def test_plan_removes_excluded_poi() -> None:
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    target = next((n for d in r1.plan["days_plan"] for n in d["nodes"]
                   if n["type"] == "attraction"), None)
    check("首轮方案含景点（行程修改前置）", target is not None,
          "首轮没有可排除的景点")
    if target is None:
        return
    r2 = s.turn(f"我不想去{target['name']}，换个别的")
    base_name = target["name"].split("（")[0]
    names2 = {n["name"] for d in r2.plan["days_plan"] for n in d["nodes"]}
    gone = all(base_name not in n for n in names2)
    noted = any("已按你的要求避开" in a for a in r2.assumptions)
    check("方案后说「不想去X」→ 重排后X消失且明确告知",
          r2.mode == "plan" and gone and noted,
          f"gone={gone} noted={noted} nodes={sorted(names2)}")


def test_plan_adds_must_visit_poi() -> None:
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    names1 = {n["name"] for d in r1.plan["days_plan"] for n in d["nodes"]}
    r2 = s.turn("我一定要去喜洲古镇")
    names2 = {n["name"] for d in r2.plan["days_plan"] for n in d["nodes"]}
    added = any("喜洲" in n for n in names2)
    noted = any("已确保安排" in a for a in r2.assumptions)
    check("方案后点名必去 → 重排后进方案且明确告知",
          r2.mode == "plan" and added and noted,
          f"added={added} noted={noted} before={sorted(names1)}")


def test_spot_qa_fetches_missing_spot() -> None:
    """问库里没有的具体景点 → 在线/模型补全入库后作答，而不是当泛推荐敷衍。"""
    import travel_agent.autofetch as af

    calls: list[tuple[str, str]] = []

    def fake_ensure_spot(city, name, *, db=None, client=None, **kw):
        calls.append((city, name))
        return {"status": "fetched", "city": city, "name": name,
                "poi": {"name": "测试梦幻乐园", "area": "测试区",
                        "duration_min": 300, "cost": 320,
                        "open_hours": "09:00-20:00", "note": "测试数据"},
                "source": "test", "message": "补到「测试梦幻乐园」"}

    orig = af.ensure_spot
    af.ensure_spot = fake_ensure_spot
    try:
        s = session()
        r = s.turn("上海测试梦幻乐园门票多少钱")
    finally:
        af.ensure_spot = orig
    hit = len(calls) == 1 and calls[0] == ("上海", "测试梦幻乐园")
    check("问库里没有的景点 → 触发 ensure_spot 补全", hit, f"calls={calls}")
    check("补全后按景点作答（含票价/时长与来源说明）",
          r.mode == "answer" and "320" in r.render and "300" in r.render
          and "在线查" in r.render, r.render[:100])

    # 泛问题不能误触发补全
    calls.clear()
    af.ensure_spot = fake_ensure_spot
    try:
        s2 = session()
        r2 = s2.turn("上海有什么好玩的")
    finally:
        af.ensure_spot = orig
    check("泛推荐不触发景点补全", not calls and r2.mode == "answer", f"calls={calls}")


def test_restore_intent_revokes_avoid() -> None:
    """删除后加不回来：avoid 一直压着 must，用户怎么说都加不回去。"""
    from travel_agent import agent
    from travel_agent.dialogue import merge_slots, _explicit_fields
    base = {"destination": "大理", "avoid": ["崇圣寺三塔"]}
    u = "把崇圣寺三塔加回来"
    d = agent.extract_slots_rules(u, today=TODAY)
    merged, _ = merge_slots(base, d, _explicit_fields(u, d), False)
    check("「把X加回来」→ 撤销排除并转为必去",
          merged["must_visit"] == ["崇圣寺三塔"] and merged["avoid"] == [],
          f"must={merged['must_visit']} avoid={merged['avoid']}")

    u2 = "崇圣寺三塔还是安排上吧"
    d2 = agent.extract_slots_rules(u2, today=TODAY)
    m2, _ = merge_slots(base, d2, _explicit_fields(u2, d2), False)
    check("倒装说法（X+还是安排上）同样能抽到景点",
          "崇圣寺三塔" in (m2.get("must_visit") or []), f"{m2.get('must_visit')}")


def test_plan_restores_removed_poi() -> None:
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    target = next((n for d in r1.plan["days_plan"] for n in d["nodes"]
                   if n["type"] == "attraction"), None)
    if target is None:
        check("恢复行程修改的前置：首轮方案含景点", False, "首轮无景点")
        return
    name = target["name"]
    s.turn(f"我不想去{name}，换个别的")
    r3 = s.turn(f"把{name}加回来")
    names3 = {n["name"] for d in r3.plan["days_plan"] for n in d["nodes"]}
    base_name = name.split("（")[0]
    back = any(base_name in n for n in names3)
    noted = any("已确保安排" in a for a in r3.assumptions)
    check("删掉的景点说「加回来」→ 重新进方案且明确告知",
          r3.mode == "plan" and back and noted,
          f"back={back} noted={noted} nodes={sorted(names3)}")


# ------------------------------------------------------------------ 景点/城市混淆
#
# 真实投诉：行程排好之后说「安排去迪士尼的行程」，助手回答"没有迪士尼的数据，
# 要不要按你刚才聊过的上海排？"，接着再让重排时已经把上一份行程忘光。
# 三件事必须同时成立：迪士尼当景点不当城市、沿用原行程、重排不重问。


def test_spot_not_treated_as_city() -> None:
    """「安排去迪士尼的行程」：迪士尼是景点，归到上海，不进"没有数据"分支。"""
    s = session()
    r = s.turn("安排去迪士尼的行程")
    ok = (r.slots.get("destination") == "上海"
          and any("迪士尼" in m for m in r.slots.get("must_visit") or [])
          and "听着不像城市名" not in (r.question or "")
          and "没有景点和酒店数据" not in (r.question or ""))
    check("景点名不当城市：迪士尼归位到上海并记为必去", ok,
          f"dest={r.slots.get('destination')} must={r.slots.get('must_visit')} "
          f"q={r.question[:40]}")


def test_spot_added_to_existing_plan() -> None:
    """已排好上海行程后加迪士尼：沿用原日期/天数，迪士尼真的进每天的节点。"""
    s = session()
    r1 = s.turn("11月20号去上海玩4天，2个人")
    if r1.mode != "plan":
        check("加景点前置：首轮出方案", False, r1.question[:40])
        return
    start = r1.slots["date_range"]["start"]
    r2 = s.turn("安排去迪士尼的行程")
    names = [n["name"] for d in (r2.plan or {}).get("days_plan", []) for n in d["nodes"]]
    ok = (r2.mode == "plan"
          and r2.slots.get("destination") == "上海"
          and r2.slots["date_range"]["start"] == start
          and r2.slots.get("days") == 4
          and any("迪士尼" in n for n in names)
          and "是景点不是城市" in (r2.render or ""))
    check("已排好的行程里加景点：日期天数不变、景点排进去、说明它是景点", ok,
          f"mode={r2.mode} start={r2.slots.get('date_range', {}).get('start')} "
          f"days={r2.slots.get('days')} hit={any('迪士尼' in n for n in names)}")


def test_replan_remembers_plan() -> None:
    """「重新排一下」= 改现有行程：不重问人数/日期，并说明在沿用哪一份。"""
    s = session()
    r1 = s.turn("11月20号去上海玩4天")     # 没说人数
    while r1.mode == "clarify":
        r1 = s.turn("2个人")
    r2 = s.turn("重新排一下")
    ok = (r2.mode == "plan" and r2.slots.get("days") == 4
          and r2.slots.get("destination") == "上海"
          and "接着刚才那份" in (r2.render or ""))
    check("「重新排」不丢已确认条件、不重问人数", ok,
          f"mode={r2.mode} days={r2.slots.get('days')} "
          f"head={(r2.render or '')[:24]}")


def test_unknown_spot_asks_city_then_keeps_spot() -> None:
    """库里没有的景点：问它在哪个城市，答了城市要把它一起安排。"""
    from travel_agent import kb as _kb
    spot = "幻影基地"                    # 用专属名字，先清掉上一轮可能入库的记录
    db = _kb.get_kb()
    if db:
        for row in db.search_pois(q=spot):
            db.delete_poi(row["poi_id"])
    s = session()
    r1 = s.turn(f"安排去{spot}的行程")
    r2 = s.turn(f"{spot}在上海")
    r3 = s.turn("12月5号，3天，2个人")
    ok = ("在哪个城市" in (r1.question or "")
          and r2.slots.get("destination") == "上海"
          and any(spot in m for m in r3.slots.get("must_visit") or []))
    check("未收录景点先问城市，答完仍保留该景点", ok,
          f"q1={r1.question[:24]} dest={r2.slots.get('destination')} "
          f"must={r3.slots.get('must_visit')}")


def test_province_not_matched_as_spot() -> None:
    """省名不能被当景点塞进别的城市（库里有条 POI 就叫「云南」）。"""
    s = session()
    r = s.turn("想去云南玩几天")
    ok = (r.slots.get("destination") == "云南" and not r.slots.get("must_visit"))
    check("省名不按景点归位，仍按「请具体到城市」处理", ok,
          f"dest={r.slots.get('destination')} must={r.slots.get('must_visit')}")


# ------------------------------------------------------------------ 汇总


def test_bare_day_answered_once() -> None:
    """答「20号」要被认成有效日期，同一个"几月走"不能问两遍。"""
    s = session()
    r1 = s.turn("我想去上海玩4天")
    r2 = s.turn("20号")
    ok1 = r2.mode != "clarify" or "时间" not in (r2.question or "")
    check("答「20号」后不再追问时间", ok1, f"第二轮问的是：{(r2.question or '')[:40]}")
    r3 = s.turn("2个人")
    dr = r3.slots.get("date_range") or {}
    # TODAY=10-29，本月 20 号已过 → 顺延下月 11-20，绝不折成第二个周六（11-14）
    check("「20号」解析为最近将来的 20 号（11-20）",
          dr.get("start") == "2026-11-20", f"date_range={dr}")


def test_month_then_day_merges() -> None:
    """先答「这个月」再补「20号」：两轮信息要合成 11-20，不能折成周六。"""
    from travel_agent import agent as _agent
    s = session()
    s.turn("我想去上海玩4天")
    s.turn("这个月")
    r = s.turn("20号")
    dr = r.slots.get("date_range") or {}
    ok = bool(dr.get("day") == 20 and dr.get("month"))
    resolved, _ = _agent.resolve_date_range(dict(r.slots), today=TODAY)
    ok = ok and (resolved["date_range"]["start"] == "2026-11-20")
    check("「这个月」+「20号」合成 2026-11-20", ok, f"date_range={dr}")


def test_day_then_month_not_overridden() -> None:
    """先答「20号」再说「这个月」：已答过的日子不能被月份假设推翻。"""
    from travel_agent import agent as _agent
    s = session()
    s.turn("我想去上海玩4天")
    s.turn("20号")
    r = s.turn("这个月")
    dr = r.slots.get("date_range") or {}
    ok = bool(dr.get("day") == 20 and dr.get("month"))
    resolved, _ = _agent.resolve_date_range(dict(r.slots), today=TODAY)
    ok = ok and (resolved["date_range"]["start"] == "2026-11-20")
    check("先「20号」后「这个月」仍是 11-20", ok, f"date_range={dr}")


def test_ask_party_size() -> None:
    """没说人数要问一次：默认 1 人直接排，预算和房型都跟实际人数无关。"""
    s = session()
    r1 = s.turn("11月8号去大理玩4天")
    asked = r1.mode == "clarify" and "几个人" in r1.question
    r2 = s.turn("两个人")
    ok = (asked and r2.mode == "plan"
          and r2.slots.get("party_size") == 2)
    check("没说人数先问一次，答了再排", ok,
          f"{r1.mode}/{r2.mode} party={r2.slots.get('party_size')} q={r1.question[:30]}")


def test_new_trip_asks_again() -> None:
    """换目的地 = 另一趟旅行：上一趟问过的模糊标记不能带过来，
    否则新行程一次都不问就直接出方案（天数人数全靠猜）。"""
    s = session()
    s.turn("想去上海玩")
    s.turn("随便")
    r = s.turn("随便")
    first_plan = r.mode == "plan"
    r2 = s.turn("再帮我排个丽江的")
    ok = first_plan and r2.mode == "clarify" and bool(r2.question)
    check("换城市后重新问关键信息，不沿用上一趟的假设", ok,
          f"plan={first_plan} next={r2.mode} q={r2.question[:40]}")


def test_day_weather_rendered() -> None:
    """方案里每天都要有天气：只写"首日天气"的话，后两天下不下雨用户看不到。"""
    from travel_agent import render_html
    day = {"day": 1, "date": "2026-11-08", "theme": "老城漫步",
           "move_min": 40, "day_budget": 600, "nodes": []}
    h = render_html.render_day(day, "#2F6F6B",
                               {"desc": "可能有雨", "low": 14, "high": 19,
                                "rain_prob": 99})
    check("逐日天气写进当天卡片（含降水概率）",
          "可能有雨" in h and "14~19" in h and "99%" in h, h[:100])


def test_plan_weather_advice() -> None:
    s = session()
    r = s.turn("11月8号去上海玩3天，2个人")
    days = (r.plan or {}).get("weather", {}).get("days") or []
    rainy = [d for d in days if (d.get("rain_prob") or 0) >= 60]
    ok = (not rainy) or bool(r.plan["weather"].get("advice"))
    check("下雨天方案里主动给天气提醒", ok,
          f"rainy={[d['rain_prob'] for d in rainy]} advice={r.plan['weather'].get('advice')}")


# ------------------------------------------------------------------ 行程问答 / 周边问答 / 通用问题


def test_plan_summary_not_replan() -> None:
    """「将行程总结成一段话」要总结刚出的方案，而不是重排一份。"""
    s = session()
    p = s.turn("11月8号从杭州去大理玩4天，2人，预算6000")
    check("前置：方案已出", p.mode == "plan" and p.plan is not None)
    r = s.turn("将行程总结成一段话")
    check("总结按问答处理，不重排", r.mode == "answer" and r.plan is None,
          f"mode={r.mode}")
    check("总结包含目的地与逐日安排",
          "大理" in r.render and "第 1 天" in r.render, r.render[:120])
    # 没出过方案时，总结类话术不能拦住正常排行程
    s2 = session()
    r2 = s2.turn("帮我把行程总结成一段话")
    check("没有行程时总结话术不拦路", r2.mode == "clarify", f"mode={r2.mode}")


def test_nearby_qa() -> None:
    """「大理古城周边有什么」按距离报周边景点，不进排行程。"""
    s = session()
    s.turn("11月8号从杭州去大理玩4天，2人，预算6000")
    r = s.turn("大理古城周边有什么")
    check("周边问答直接答", r.mode == "answer" and r.plan is None,
          f"mode={r.mode}")
    check("周边答案带距离与具体景点",
          "公里" in r.render and ("崇圣寺" in r.render or "廊道" in r.render
                                  or "码头" in r.render), r.render[:160])


def test_general_question_fallback() -> None:
    """通用问题：有模型走「LLM 兜底分类 + 联网检索」，检索失败/无模型不崩、不编。"""
    from travel_agent.llm import ScriptedClient
    import travel_agent.websearch as ws

    orig = ws.search
    try:
        # 1) 规则认不出 → LLM 分类为 general → 改写检索词 → 检索有结果 → 模型组织
        ws.search = lambda q, **k: {"ok": True, "note": "联网检索完成",
            "results": [{"title": "日本签证办理指南",
                         "url": "https://www.example.cn/visa",
                         "snippet": "需提前通过指定旅行社递签",
                         "site": "example.cn"}]}
        c = ScriptedClient(['{"intent":"general"}',
                            "日本 旅游签证 办理",
                            "去日本需要提前办理签证，一般通过指定旅行社递签，"
                            "建议出发前一个月开始准备，具体要求请以官方为准。"])
        s = Session(today=TODAY, client=c)
        r = s.turn("去日本需要办签证吗")
        check("通用问题被 LLM 兜底接住", r.mode == "answer", f"mode={r.mode}")
        check("回答注明联网来源", "example.cn" in r.render, r.render[:120])
        # 2) 检索失败但有模型 → 仍给回答（模型自答）
        ws.search = lambda q, **k: {"ok": False, "results": [],
                                    "note": "联网检索失败"}
        c2 = ScriptedClient(['{"intent":"general"}',
                             "旅行 治安 注意事项",
                             "这个我暂时没法联网核实，凭常识看问题不大，"
                             "出行前请再确认最新要求。"])
        s2 = Session(today=TODAY, client=c2)
        r2 = s2.turn("那边治安怎么样")
        check("检索失败仍能作答并声明不实时", r2.mode == "answer", r2.render[:80])
        # 3) 分类失败（脚本用尽）→ 回落排行程流水线，不崩
        c3 = ScriptedClient([])
        s3 = Session(today=TODAY, client=c3)
        r3 = s3.turn("那边治安怎么样")
        check("兜底失败回落原流程", r3.mode in ("clarify", "plan"),
              f"mode={r3.mode}")
    finally:
        ws.search = orig


def main() -> int:
    for fn in (test_one_shot, test_one_shot_no_assumption,
               test_plan_summary_not_replan, test_nearby_qa,
               test_general_question_fallback,
               test_ask_at_most_two_fields, test_ask_has_default_hint,
               test_two_rounds_then_plan, test_no_plan_when_city_has_no_data,
               test_no_repeat_question,
               test_ask_one_thing_at_a_time, test_backquestion_does_not_trigger_plan,
               test_retry_changes_wording, test_assume_after_three_asks,
               test_vague_answer_stops_asking, test_origin_default_is_stated,
               test_assumption_marked_in_text,
               test_ask_party_size, test_new_trip_asks_again,
               test_day_weather_rendered, test_plan_weather_advice,
               test_slots_not_clobbered, test_per_person_tracks_party_size,
               test_explicit_total_is_sticky, test_change_mind,
               test_clock_precise_reschedule_honest,
               test_budget_tiers_when_absent, test_no_tiers_when_budget_given,
               test_province_asks_city, test_unsupported_destination_never_replaced,
               test_month_only_date, test_solo_traveler, test_hotel_relax_notice,
               test_chinese_numerals_and_route, test_next_month_exact_day,
               test_exact_day_no_saturday_assumption, test_origin_without_route_noted,
               test_plan_states_trip_facts,
               test_session_uses_memory_prefs,
               test_preference_memory_persists,
               test_context_retention_and_switch,
               test_extract_must_and_avoid, test_avoid_is_not_destination,
               test_avoid_wins_over_must,                test_plan_removes_excluded_poi,
               test_plan_adds_must_visit_poi,
               test_spot_qa_fetches_missing_spot,
               test_restore_intent_revokes_avoid,
               test_plan_restores_removed_poi,
               test_spot_not_treated_as_city, test_spot_added_to_existing_plan,
               test_replan_remembers_plan, test_unknown_spot_asks_city_then_keeps_spot,
               test_province_not_matched_as_spot,
               test_bare_day_answered_once, test_month_then_day_merges,
               test_day_then_month_not_overridden):
        fn()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条对话断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
