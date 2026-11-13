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


def test_transport_time_change_refused() -> None:
    """「把去程到达时间改为 09:00」必须拦在抽取之前，绝不污染方案。

    之前这句话被目的地兜底正则吃成城市「达时间改为」，进了必去景点、
    假设和行前准备（用户实测）。
    """
    from travel_agent import agent, intent
    check("交通时刻修改被识别",
          intent.is_transport_time_change("把去程到达时间改为 09:00"))
    check("到达时间陈述/提问不误拦",
          not intent.is_transport_time_change("我们9点到成都")
          and not intent.is_transport_time_change("到达时间是什么时候"))
    check("目的地抽取不再吃到时刻碎片",
          agent.extract_slots("把去程到达时间改为 09:00")["destination"] is None)
    check("混合句里正常目的地照抽",
          agent.extract_slots("把出发时间改成10点，去成都玩3天")["destination"] == "成都")
    _r = agent.extract_slots("去掉苍山")
    check("「去掉X」不再被截成地点「掉X」（曾闭环污染 KB）",
          _r["destination"] is None and _r["avoid"] == ["苍山"]
          and not _r["must_visit"], str(_r["destination"]))
    s = session()
    s.turn("11月8号去大理玩4天，2人，预算6000")
    before = s.last_plan
    r = s.turn("把去程到达时间改为 09:00")
    check("交通时刻修改返回明确拒绝",
          r.mode == "answer" and "真实班次" in r.render, r.render[:60])
    check("方案未被改动",
          s.last_plan is before
          and s.last_plan["slots"]["destination"] == "大理")
    check("必去景点未被污染", not s.last_plan["slots"]["must_visit"],
          str(s.last_plan["slots"]["must_visit"]))


def test_multi_city_request_not_poi() -> None:
    """「去成都、重庆玩5天」是多城请求：明确说明单城限制，绝不当景点。

    实测：LLM 把「成都、重庆」整串回给 destination → 被追问"在哪个
    城市"→ 用户补「先成都后重庆」→ 城市串被挂成位置估算景点排进
    Day2，最后变成重庆 5 天、没有成都行程。
    """
    from travel_agent import agent
    s1 = agent.extract_slots("从上海出发，11月24号去成都、重庆玩5天")
    check("多城请求拆出城市列表",
          s1.get("multi_cities") == ["成都", "重庆"]
          and s1.get("destination") is None,
          f"multi={s1.get('multi_cities')} dest={s1.get('destination')}")
    check("多城请求天数日期照抽",
          s1.get("days") == 5 and "11-24" in str((s1.get("date_range") or {}).get("start")),
          f"days={s1.get('days')} date={s1.get('date_range')}")
    check("城市串不进必去景点",
          all("、" not in m for m in (s1.get("must_visit") or [])),
          str(s1.get("must_visit")))
    s2 = agent.extract_slots("先成都后重庆")
    check("「先X后Y」也识别为多城",
          s2.get("multi_cities") == ["成都", "重庆"], str(s2.get("multi_cities")))
    s3 = agent.extract_slots("去南京的中山陵玩")
    check("城市+景点不被误判为多城",
          s3.get("destination") == "南京" and not s3.get("multi_cities"),
          f"dest={s3.get('destination')} multi={s3.get('multi_cities')}")
    s4 = agent.extract_slots("去成都玩5天")
    check("单城请求不受影响",
          s4.get("destination") == "成都" and not s4.get("multi_cities"),
          f"dest={s4.get('destination')}")
    # ---- 端到端：说明能力边界 → 追问 → 选定后清空 ----
    sess = session()
    r1 = sess.turn("从上海出发，11月24号去成都、重庆玩5天")
    check("多城请求明确说明一次只排一个城市",
          r1.mode == "clarify" and "一个城市" in r1.render, r1.render[:80])
    check("多城请求不追问在哪个城市", "在哪个城市" not in r1.render, r1.render[:80])
    check("多城请求不出方案", r1.plan is None and not sess.plan_made)
    r2 = sess.turn("先成都后重庆")
    check("再次表达多城仍说明并追问",
          r2.mode == "clarify" and "一个城市" in r2.render, r2.render[:80])
    sess.turn("成都")
    check("选定单城后目的地生效、多城清单清空",
          sess.slots.get("destination") == "成都"
          and not sess.slots.get("multi_cities"),
          f"dest={sess.slots.get('destination')} "
          f"multi={sess.slots.get('multi_cities')}")
    check("必去景点里没有城市串",
          all("、" not in m for m in (sess.slots.get("must_visit") or [])),
          str(sess.slots.get("must_visit")))


def test_mixed_transport_time_and_trip() -> None:
    """混合指令只拒绝改班次时刻，剩余的行程请求照常处理。

    之前「把出发时间改成10点，去成都玩3天」整句连坐拒绝，回复还
    建议"直接说去哪儿玩几天"——可目的地和天数用户明明都说了。
    """
    from travel_agent import intent
    rest, dropped = intent.split_transport_time_change(
        "把出发时间改成10点，去成都玩3天")
    check("混合句拆出交通时刻从句",
          dropped == "把出发时间改成10点" and rest == "去成都玩3天",
          f"rest={rest!r} dropped={dropped!r}")
    _r2, _d2 = intent.split_transport_time_change("把去程到达时间改为 09:00")
    check("纯时刻句剩余为空（走纯拒绝）", _r2 == "" and _d2 is not None,
          f"rest={_r2!r} dropped={_d2!r}")
    _r3, _d3 = intent.split_transport_time_change("去成都玩3天")
    check("普通行程句原样通过", _r3 == "去成都玩3天" and _d3 is None,
          f"rest={_r3!r} dropped={_d3!r}")
    _r4, _d4 = intent.split_transport_time_change("到达时间是什么时候")
    check("班次提问不误拆", _d4 is None and "到达时间是什么时候" in _r4,
          f"rest={_r4!r} dropped={_d4!r}")
    s = session()
    s.turn("11月8号去大理玩4天，2人，预算6000")
    before = s.last_plan
    r = s.turn("把出发时间改成10点，去成都玩3天")
    check("混合指令不再整句拒绝", "这条先没动行程" not in r.render,
          r.render[:80])
    check("回复说明时刻部分被拒", "真实班次" in r.render, r.render[:80])
    check("回复处理了剩余行程请求",
          "成都" in r.render
          and (r.mode in ("plan", "clarify") or r.plan is not None),
          f"mode={r.mode} {r.render[:80]}")
    check("新方案目的地是成都或正在追问成都",
          (r.plan or {}).get("slots", {}).get("destination") == "成都"
          or s.slots.get("destination") == "成都",
          str((r.plan or {}).get("slots", {}).get("destination")))
    check("原大理方案未被悄悄改动",
          before is None or s.last_plan is before
          or s.last_plan["slots"]["destination"] == "成都",
          str((s.last_plan or {}).get("slots", {}).get("destination")))


def test_pace_noop_trims_overcap() -> None:
    """「别太赶」对超额行程要有实际动作：删掉非必去的超额站并同步预算。

    之前质检报「3 个景点 > 上限 2」，点别太赶却回"已经是最松的一档，
    没有继续往下压的空间"——只看了节奏档位，没看行程本身（杭州实测）。
    """
    s = session()
    s.turn("11月20号去杭州玩2天，2人，轻松，预算5000")
    check("前置：已有轻松档方案",
          s.plan_made and s.slots.get("pace") == "relaxed",
          str(s.slots.get("pace")))
    last = s.last_plan["days_plan"][-1]
    last["nodes"].append({"time": "19:00", "name": "额外塞的景点",
                          "type": "attraction", "duration_min": 60,
                          "cost": 60, "transit": "", "tip": ""})
    before = (s.last_plan.get("budget") or {}).get("total")
    r = s.turn("行程别太赶，轻松一点")
    names = [n.get("name") for d in s.last_plan["days_plan"] for n in d["nodes"]]
    check("超额时别太赶给出新方案而非空话", r.mode == "plan",
          f"mode={r.mode} {r.render[:60]}")
    check("超额的非必去景点被删掉", "额外塞的景点" not in names, str(names))
    check("回复写明删了什么", "已按「别太赶」处理" in r.render, r.render[:80])
    after = (s.last_plan.get("budget") or {}).get("total")
    check("预算总额同步扣回门票（60 元 × 2 人）",
          before is None or abs((after or 0) - (before - 120)) < 0.01,
          f"{before} -> {after}")


def _attractions(plan: dict) -> list[str]:
    return [n.get("name") for d in plan.get("days_plan") or []
            for n in d.get("nodes") or [] if n.get("type") == "attraction"]


def test_pace_loosen_deletes_instead_of_relocating() -> None:
    """放松节奏要真删点，不能把装不下的点挪到别的天（2026-11-11 反馈）。

    杭州 3 天、点名西湖和灵隐寺：原 Day 2 排了西湖、灵隐寺、雷峰塔。点「别太赶」
    后 Day 2 降到两个景点，但雷峰塔被挪到返程日——每天都不超上限了，景点总数
    和预算却一个没少，等于没删。修法：放松节奏时不再整单重排，就地按新上限删点。
    """
    s = session()
    s.turn("11月20号去杭州玩3天，2人，预算5000")
    check("前置：初始是适中档",
          (s.slots.get("pace") or "moderate") == "moderate",
          str(s.slots.get("pace")))
    mid = s.last_plan["days_plan"][1]
    # 确定性夹具：真实 KB 数据会影响当天排几个点（非密闭测试），先把当天
    # 景点裁到 2 个、再塞一个 30 元票价的"可删景点"——放松到轻松档（上限 2）
    # 必然恰好删它，预算必然 -60（30 元 × 2 人），断言不再漂
    for n in [n for n in mid["nodes"] if n.get("type") == "attraction"][2:]:
        mid["nodes"].remove(n)
    mid["nodes"].append({"time": "19:00", "name": "可删景点", "type": "attraction",
                         "duration_min": 60, "cost": 30, "transit": "", "tip": ""})
    before = _attractions(s.last_plan)
    ret_before = [n.get("name") for n in s.last_plan["days_plan"][-1]["nodes"]]
    budget_before = (s.last_plan.get("budget") or {}).get("total")

    r = s.turn("行程别太赶，轻松一点")

    after = _attractions(s.last_plan)
    check("放松节奏给出新方案", r.mode == "plan", f"mode={r.mode} {r.render[:60]}")
    check("超额的点被删掉（总数 -1）", len(after) == len(before) - 1,
          f"{before} -> {after}")
    check("删掉的点没被挪到别的天", "可删景点" not in after, str(after))
    check("返程日没有被塞进超额点",
          [n.get("name") for n in s.last_plan["days_plan"][-1]["nodes"]] == ret_before,
          str([n.get("name") for n in s.last_plan["days_plan"][-1]["nodes"]]))
    check("回复写明删了什么", "已按「别太赶」处理" in r.render, r.render[:80])
    check("摘要把节奏改成轻松",
          "**节奏**：轻松" in r.render, r.render[:200])
    budget_after = (s.last_plan.get("budget") or {}).get("total")
    check("预算跟着门票同步下调",
          budget_before is None or (budget_after or 0) < budget_before,
          f"{budget_before} -> {budget_after}")


def test_trim_optional_phrase_routes_to_trim() -> None:
    """「把可去可不去的删掉」是泛指删点，不是新景点名（2026-11-11 冒烟实测）。

    之前 LLM 把「可不去的删掉」抽成必去景点，经 autofetch 落库污染 KB，
    真必去点反而"未能排入"。现在拦在抽取之前按节奏就地删超额站。
    """
    s = session()
    s.turn("11月20号去杭州玩3天，2人，轻松，预算5000")
    check("前置：已有方案", s.plan_made, f"plan_made={s.plan_made}")
    s.last_plan["days_plan"][1]["nodes"].append(
        {"time": "19:00", "name": "额外塞的景点", "type": "attraction",
         "duration_min": 60, "cost": 60, "transit": "", "tip": ""})
    before = _attractions(s.last_plan)
    r = s.turn("还是有点赶，把可去可不去的删掉")
    after = _attractions(s.last_plan)
    check("泛指删点给出新方案而非重排空转", r.mode == "plan",
          f"mode={r.mode} {r.render[:60]}")
    check("超额的非必去点被删掉",
          "额外塞的景点" not in after and len(after) == len(before) - 1,
          f"{before} -> {after}")
    check("指令碎片没被当成景点混进行程",
          "可不去的删掉" not in after and "一些" not in after and "一些景点" not in after,
          str(after))
    check("回复写明删除动作", "删掉可去可不去的景点" in r.render, r.render[:80])


def test_trim_optional_within_cap_is_noop() -> None:
    """行程本来就在节奏上限内时，泛指删点不动行程，给真能生效的选项。"""
    s = session()
    s.turn("11月20号去杭州玩3天，2人，轻松，预算5000")
    before = _attractions(s.last_plan)
    r = s.turn("删掉一些景点")
    check("上限内泛指删点不再重排", r.mode == "answer", f"mode={r.mode}")
    check("行程一个点没动", _attractions(s.last_plan) == before, str(before))
    check("回复给出可执行的下一步", "去掉" in r.render or "删掉一站" in r.render,
          r.render[:120])


def test_pace_loosen_without_excess_says_switched() -> None:
    """放松节奏但行程本来就在上限内：明说切了档、没删点、没重排。

    这条防的是另一个方向的谎：用户点了「轻松一点」，系统偷偷重排一遍，
    行程面目全非却说"只是把节奏放松了"。
    """
    s = session()
    s.turn("11月20号去杭州玩3天，2人，预算5000")
    # 把每天景点削到 1 个，制造"本来就不超额"的局面
    for d in s.last_plan["days_plan"]:
        seen = 0
        keep = []
        for n in d["nodes"]:
            if n.get("type") == "attraction":
                seen += 1
                if seen > 1:
                    continue
            keep.append(n)
        d["nodes"] = keep
    before = [list(d["nodes"]) for d in s.last_plan["days_plan"]]

    r = s.turn("行程别太赶，轻松一点")

    check("没超额时直接回答，不假装重排", r.mode == "answer", f"mode={r.mode}")
    check("明说已切到轻松", "已把节奏切到「轻松」" in r.render, r.render[:120])
    check("明说没有删点、没有重排", "没删点、也没重排" in r.render, r.render[:160])
    check("行程确实一个字没动",
          [list(d["nodes"]) for d in s.last_plan["days_plan"]] == before)
    check("方案的节奏已同步为轻松",
          (s.last_plan.get("slots") or {}).get("pace") == "relaxed",
          str((s.last_plan.get("slots") or {}).get("pace")))


def test_day_trip_schedules_sights_and_no_hotel() -> None:
    """单日行程要按"一整天"排，且不过夜、不收住宿费。

    2026-11-11 反馈（高优先级）：杭州、日期同一天、轻松节奏，只填西湖也没排进
    任何景点，还提示"天数/节奏装不下"；同时主题写「一日往返」，概要却写
    「1 天 1 晚」、预算收 ¥400 住宿、日程里还有 18:00「抵达 + 入住」。
    根因：编排把单日当成"抵达日"（18:00 抵达 → 容量 0），住宿按 max(days-1,1)
    算一晚。
    """
    s = session()
    r = s.turn("11月20号从上海去杭州玩1天，2人，想去西湖")
    check("前置：单日方案", r.mode == "plan" and s.slots.get("days") == 1,
          f"mode={r.mode} days={s.slots.get('days')}")
    plan = s.last_plan
    nodes = [n for d in plan["days_plan"] for n in d["nodes"]]
    names = [n.get("name") for n in nodes]
    check("点名的西湖排进了行程", any("西湖" in (x or "") for x in names), str(names))
    check("主题是「一日往返」", plan["days_plan"][0]["theme"] == "一日往返",
          plan["days_plan"][0]["theme"])
    check("没有「抵达 + 入住」", not any("入住" in (x or "") for x in names), str(names))
    check("收尾写了去车站候车", any("车站候车" in (x or "") for x in names), str(names))

    items = [i for i in (plan.get("budget") or {}).get("breakdown") or []]
    check("预算里没有住宿行", not any(i.get("category") == "hotel" for i in items),
          str([(i.get("category"), i.get("amount")) for i in items]))
    check("摘要写「当日往返」而不是 1 天 1 晚",
          "当日往返" in r.render and "1 天 1 晚" not in r.render,
          [l for l in r.render.splitlines() if "行程日期" in l][:1])
    from travel_agent import agent as _agent
    bad = [c for c in _agent.self_check(plan) if c.get("result") != "pass"]
    check("单日方案质检全绿（首日强度/闭馆冲突不再误报）", not bad, str(bad)[:160])


def test_day_trip_must_explain_what_does_not_fit() -> None:
    """一天装不下的必去点要如实说"装不下"，不能说成"符合安排"。"""
    s = session()
    r = s.turn("11月20号从上海去杭州玩1天，2人，"
               "想去西湖、灵隐寺、雷峰塔")
    placed = [m for m in ("西湖", "灵隐寺", "雷峰塔")
              if any(m in (n.get("name") or "")
                     for d in s.last_plan["days_plan"] for n in d["nodes"])]
    check("至少排进一个必去点", bool(placed), str(placed))
    joined = " ".join(r.assumptions)
    missed_lines = [a for a in r.assumptions if "未能排入" in a]
    if len(placed) < 3:
        check("排不下的必去点明说是当日往返时间不够",
              bool(missed_lines) and "一日往返" in missed_lines[0],
              str(missed_lines)[:160])
    else:
        check("三个都排进去了（不必提示装不下）", True)


def test_spot_interest_placed_and_nap_daily() -> None:
    """表单口径：兴趣栏写景点名 + 带 3 岁娃，两件事都要落地。

    2026-11-11 实测（成都 4 天、上海出发、2 大 1 小、轻松、不吃辣）：
    ① 兴趣栏填「大熊猫基地」，方案却排了杜甫草堂/武侯祠/川大博物馆/望江楼公园/
       锦里，然后说这条兴趣"天数/节奏装不下"；
    ② 午休只有 Day3 有，Day2 午餐后直接去武侯祠，只在自由活动里写"累了就早回"。
    """
    s = session()
    # 兴趣词只能由模型抽出（规则层没有开放词表），这里直接把表单结果灌进槽位
    s.slots = {"origin": "上海", "destination": "成都", "days": 4, "party_size": 3,
               "adults": 2, "children": 1, "child_age": 3,
               "party_detail": "2 大 1 小（孩子 3 岁）",
               "date_range": {"start": "2026-11-20"}, "budget_total": 6000,
               "pace": "relaxed", "soft_prefs": ["大熊猫基地"], "must_visit": [],
               "hard_prefs": ["不吃辣", "需无障碍", "带幼儿"], "diet": ["不吃辣"],
               "stay_pref": "安静，靠近地铁"}
    r = s.turn("就按这个排吧")
    plan = s.last_plan or {}
    days = plan.get("days_plan") or []
    names = [(d["day"], n.get("name")) for d in days for n in d["nodes"]]
    panda = [(day, nm) for day, nm in names if "大熊猫" in (nm or "")]
    check("兴趣点名的熊猫基地排进了行程", bool(panda), str(names))
    check("同一个景点没有排两遍（权威记录 + 估算副本）", len(panda) == 1, str(panda))
    check("不再说这条兴趣装不下",
          not any("大熊猫基地" in a and "没排进去" in a for a in r.assumptions),
          str([a for a in r.assumptions if "熊猫" in a]))

    nap_days = [d["day"] for d in days
                if any(n["name"].startswith("午休") for n in d["nodes"])]
    check("除抵达日外每天都有午休", nap_days == [2, 3, 4], str(nap_days))
    check("午休写在午餐之后（不是被自由活动顶替）",
          all(next(i for i, n in enumerate(d["nodes"])
                   if n["name"].startswith("午休"))
              > next(i for i, n in enumerate(d["nodes"])
                     if n["type"] == "restaurant")
              for d in days if any(n["name"].startswith("午休") for n in d["nodes"])),
          str([[n["name"] for n in d["nodes"]] for d in days]))


def test_interest_missed_explains_swap() -> None:
    """兴趣真排不进去时，要说清"名额被谁占了"并给一句能直接说的替换。"""
    s = session()
    s.slots = {"origin": "上海", "destination": "成都", "days": 1, "party_size": 2,
               "date_range": {"start": "2026-11-20"}, "budget_total": 6000,
               "pace": "relaxed", "soft_prefs": ["大熊猫基地"],
               "must_visit": ["武侯祠", "人民公园", "宽窄巷子"], "hard_prefs": []}
    r = s.turn("就按这个排吧")
    joined = " ".join(r.assumptions)
    check("说清是哪条兴趣没排进去",
          "大熊猫基地" in joined and "没排进去" in joined, joined[:200])
    check("给出可直接说的取舍（换点 / 加一天 / 去掉一个）",
          any(k in joined for k in ("换成", "加一天", "去掉一个")), joined[:200])


def test_return_leg_direction_in_plan() -> None:
    """方案里的返程方向必须是 目的地→出发地。

    2026-11-11 反馈：上海去南京、成都去西安，去程和返程都写成出发方向，
    用户会照着错的这行去查回程票。这里用内置城市对（上海→大理）跑完整流程，
    钉住"假设 + 正文交通行"两处文案。
    """
    s = session()
    r = s.turn("11月20号从上海去大理玩3天，2个人")
    check("前置：出了方案", r.mode == "plan", r.mode)
    joined = " ".join(r.assumptions)
    check("假设：去程是 上海→大理", "去程 上海→大理" in joined, joined[:120])
    check("假设：返程是 大理→上海", "返程 大理→上海" in joined, joined[:120])
    check("假设：返程没写成出发方向", "返程 上海→大理" not in joined, joined[:120])
    line = next((l for l in r.render.splitlines() if l.startswith("- 交通")), "")
    check("正文：往返方向各自正确",
          "去程 上海→大理" in line and "返程 大理→上海" in line, line)


def test_pace_word_not_treated_as_interest() -> None:
    """「轻松休闲一点」不能变成「你的兴趣『休闲』没被满足」。

    用户只在表单里勾了「轻松慢逛」（前端拼成"不想太赶，轻松休闲一点"），
    模型把这句抽成 soft_prefs=["休闲"]，于是每份方案都多一句
    「你的兴趣『休闲』在成都的资料里没有直接对应的景点」的假警报。
    这里直接注入脏槽位，验证合并后的每一层（对话/编排）都清得掉。
    """
    from travel_agent import agent  # 局部导入：只用于构造底座槽位

    s = session()
    slots = agent.extract_slots_rules(
        "11月8号从杭州去大理玩4天，2个人，预算6000", TODAY)
    slots["soft_prefs"] = ["休闲"]          # 模拟模型误抽
    slots["pace"] = "relaxed"
    s.slots = slots
    r = s.turn("喜欢苍山、古城")
    check("节奏词不会残留在兴趣槽位里", s.slots.get("soft_prefs") == [],
          str(s.slots.get("soft_prefs")))
    check("方案里不再出现「兴趣『休闲』没对应景点」",
          not any("休闲" in a for a in r.assumptions), str(r.assumptions))
    check("回复正文同样干净", "休闲" not in r.render, r.render[:80])


def test_pace_noop_must_overcap_honest() -> None:
    """超额全是用户点名的必去点：如实摊牌取舍，绝不静默删需求。"""
    s = session()
    s.turn("11月20号去杭州玩2天，2人，轻松，预算5000")
    s.slots["pace"] = "relaxed"
    s.slots["must_visit"] = ["必去甲", "必去乙", "必去丙"]
    s.last_plan["slots"] = dict(s.last_plan.get("slots") or {},
                                must_visit=["必去甲", "必去乙", "必去丙"],
                                party_size=2)
    d1 = s.last_plan["days_plan"][0]
    d1["nodes"] = [n for n in d1["nodes"] if n["type"] != "attraction"]
    for nm in ("必去甲", "必去乙", "必去丙"):
        d1["nodes"].append({"time": "10:00", "name": nm, "type": "attraction",
                            "duration_min": 90, "cost": 0, "transit": "",
                            "tip": ""})
    r = s.turn("行程别太赶，轻松一点")
    after = [n["name"] for d in s.last_plan["days_plan"] for n in d["nodes"]]
    check("必去点超额时如实回答不重排", r.mode == "answer", f"mode={r.mode}")
    check("三个必去点都还在行程里",
          all(nm in after for nm in ("必去甲", "必去乙", "必去丙")), str(after))
    check("回复点名超额事实与必去点身份",
          "个景点" in r.render and "必去点" in r.render, r.render[:120])
    check("给出加一天/取消必去的取舍", "多玩一天" in r.render and "去掉" in r.render,
          r.render[:200])


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
    # 只看景点节点：酒店/餐厅名（如「入住：大理古城八月客栈」）天然包含
    # 「大理古城」这类城市前缀，拿它们判 gone 会误报（RouteEngine 重排后
    # 首个景点可能是带城市前缀的名字，实测暴露）
    names2 = {n["name"] for d in r2.plan["days_plan"] for n in d["nodes"]
              if n["type"] == "attraction"}
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


def test_day_change_keeps_days_and_places_spot() -> None:
    """「把第2天的行程改成…」：4 天行程不能被截成 2 天（用户实测），
    新点的点还要真落进第 2 天。"""
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    check("前置：4 天方案", r1.mode == "plan"
          and len(r1.plan["days_plan"]) == 4)
    r2 = s.turn("把第2天的行程改成上午去洱海")
    dp = (r2.plan or {}).get("days_plan") or []
    check("改第2天后总天数仍是 4", r2.mode == "plan" and len(dp) == 4,
          f"mode={r2.mode} days={len(dp)}")
    hit = [i + 1 for i, d in enumerate(dp)
           if any((n["type"] == "attraction"
                   and ("洱海" in n["name"] or "洱海" in (n.get("tip") or "")))
                  for n in d["nodes"])]
    # 降水概率 >60% 时户外点会被换成室内替代（备注写明原计划），两种都算落位
    check("洱海排进第 2 天", hit == [2], str(hit))
    r3 = s.turn("改第9天的行程")
    check("超出天数如实拒绝", r3.mode == "answer" and "改不了第 9 天" in r3.render,
          r3.render[:60])


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


def test_filler_node_qa() -> None:
    """问行程里的填充项（早市/伴手礼）→ 按节点身份回答，不甩"查不到换个城市"。

    填充项在库里永远查不到，之前的路径是联网补全失败后回一句
    "邯郸的景点数据我这边也没有联网检索通道"——用户点的是自己行程
    里的安排，得到的却像是从没听过这座城市（实测反馈）。
    """
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    filler = next((n for d in r1.plan["days_plan"] for n in d["nodes"]
                   if n.get("name", "").startswith("早市")), None)
    check("多日行程返程日有早市/伴手礼填充项", filler is not None,
          f"nodes={[n.get('name') for d in r1.plan['days_plan'] for n in d['nodes']]}")
    if filler is None:
        return
    ans = s._spot_qa("大理", "早市 / 伴手礼")
    ok = ("查不到" not in ans and "早市" in ans
          and ("门票" in ans or "自由安排" in ans))
    check("点名填充项 → 按自由安排作答（不出现「查不到」）", ok, ans[:120])
    check("填充项回答给出下一步（换具体地方）", "说" in ans and "排" in ans,
          ans[:120])

    # 非填充节点不受影响：真实景点问答照旧查库（这里查不到走模型/模板，
    # 但不能被填充项分支误接）
    check("填充项识别只认系统补的占位安排",
          not Session._is_filler_node({"name": "崇圣寺三塔", "type": "attraction"})
          and Session._is_filler_node({"name": "午餐 · 就近自选", "type": "restaurant"})
          and Session._is_filler_node({"name": "前往车站候车", "type": "transit"}))


def test_plan_node_where_qa() -> None:
    """「你给我排的X在哪里」→ 答那个点，不回泛推荐。

    邯郸实测：老驴头驴肉火烧在行程里，这句却被抽不出名字 + 意图分类
    other，最后回一串不相干景点介绍（用户：驴头不对马嘴）。
    """
    import re
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    node = next((n for d in r1.plan["days_plan"] for n in d["nodes"]
                 if n.get("type") == "attraction"
                 and not Session._is_filler_node(n)
                 and len(re.sub(r"[（(][^（）()]*[)）]", "",
                                n.get("name") or "").strip()) >= 4), None)
    check("方案里有可点名的景点", node is not None,
          f"nodes={[n.get('name') for d in r1.plan['days_plan'] for n in d['nodes']]}")
    if node is None:
        return
    core = re.sub(r"[（(][^（）()]*[)）]", "", node["name"]).strip()
    r2 = s.turn(f"你给我排的{core}在哪里")
    ok = (r2.mode == "answer" and core in r2.render
          and "查不到" not in r2.render)
    check("点名行程内景点问位置 → 按该点作答", ok, r2.render[:120])

    # 改行程指令绝不被闸门截胡：不想去 → 照走重排
    r3 = s.turn(f"我不想去了{core}")
    check("同名的改行程指令不被问答闸门拦截",
          r3.mode == "plan", f"mode={r3.mode}")


def test_reco_restaurant_not_replan() -> None:
    """「推荐几家驴肉火烧」→ 列店作答，绝不能整单重排（用户实测无脑重排）。"""
    s = session()
    r1 = s.turn("11月16号从石家庄去邯郸玩3天，2人，预算2000")
    check("邯郸方案出成功", r1.mode == "plan", f"mode={r1.mode}")
    if r1.mode != "plan":
        return
    r2 = s.turn("推荐几家驴肉火烧")
    ok = (r2.mode == "answer" and r2.plan is None
          and "老驴头驴肉火烧" in r2.render)
    check("推荐餐厅 → 列店作答（不重排）", ok, r2.render[:120])
    # 错字容错：记错店名头一个字也能靠去首字重试命中
    r3 = s.turn("推荐几家老驴肉火烧")
    check("错字关键词去首字重试", r3.mode == "answer"
          and "老驴头驴肉火烧" in r3.render, r3.render[:120])
    # 没收录的品类如实说，不编店名
    r4 = s.turn("推荐几家螺蛳粉")
    check("没收录的品类如实说", r4.mode == "answer" and "没收录" in r4.render,
          r4.render[:120])


def test_no_new_slots_no_replan() -> None:
    """方案后说没有新条件的话（「嗯嗯」「那就这样」）→ 不许再排一遍行程。"""
    s = session()
    r1 = s.turn("11月8号去大理玩4天，2人，预算6000")
    check("首轮出方案", r1.mode == "plan", f"mode={r1.mode}")
    if r1.mode != "plan":
        return
    r2 = s.turn("嗯嗯，那就这样吧")
    ok = r2.mode == "answer" and r2.plan is None
    check("无新条件不重排", ok, f"mode={r2.mode} render={r2.render[:80]}")
    # 明确的修改指令照常重排
    r3 = s.turn("别太赶")
    check("明确修改指令仍走行程流程", r3.mode in ("plan", "answer"),
          f"mode={r3.mode}")


def test_chat_history_attached() -> None:
    """多轮上下文：闲聊层要能看到最近几轮，但要截断、且不含本轮这句。"""
    from travel_agent.llm import ScriptedClient
    s = session()
    s.turn("11月8号去大理玩4天，2人，预算6000")
    c = ScriptedClient(["{}", "接着刚才那份大理的安排说，你想改哪块？"])
    s.client = c
    r = s.turn("继续说")
    check("闲聊兜底被触发", r.mode == "answer" and "大理" in r.render, r.render[:80])
    h = c.histories[-1] if c.histories else []
    check("闲聊层拿到最近几轮",
          len(h) >= 2 and h[0]["role"] == "user"
          and any(x["role"] == "assistant" for x in h), f"{h}"[:160])
    check("历史里没有本轮这句", all(x["content"] != "继续说" for x in h), f"{h}"[:160])
    check("长回复（整份方案）被截断",
          all(len(x["content"]) <= 241 for x in h),
          f"{[len(x['content']) for x in h]}")
    check("历史总字数受控", sum(len(x["content"]) for x in h) <= 800,
          f"{sum(len(x['content']) for x in h)}")


def test_comparison_question_not_generic_list() -> None:
    """「邯郸有没有像迪士尼一样的地方」→ 按类型找同类，不甩本地景点清单。

    实测（2026-11-12）：这句被当成「邯郸有什么好玩的」，回了广府古城、
    娲皇宫、丛台公园——古城石窟，跟主题乐园半点关系没有。
    """
    from travel_agent.llm import ScriptedClient
    import travel_agent.websearch as ws

    orig = ws.search
    try:
        ws.search = lambda q, **k: {"ok": True, "note": "联网检索完成",
            "results": [{"title": "邯郸有主题乐园吗",
                         "url": "https://www.example.cn/hd",
                         "snippet": "邯郸以历史文化景点为主，没有大型主题乐园",
                         "site": "example.cn"}]}
        c = ScriptedClient(['{"intent":"poi"}',
                            "邯郸 主题乐园 类似迪士尼",
                            "邯郸没有迪士尼那种大型主题乐园，景点以古城和石窟为主。"
                            "想过山车那套，得看周边大城市，出行前再核实一下。"])
        s = Session(today=TODAY, client=c)
        r = s.turn("邯郸有没有像迪士尼一样的地方")
        check("类比问句走通用问答", r.mode == "answer", f"mode={r.mode}")
        check("不套本地景点清单", "想待几天" not in r.render, r.render[:120])
        check("回答点明「没有同类」", "主题乐园" in r.render, r.render[:120])
    finally:
        ws.search = orig


def test_poi_category_question_filters_by_type() -> None:
    """「中小型游乐园有哪些」→ 只报同品类，不甩古城石窟（实测答非所问）。

    库里确实有「邯郸方特国色春秋」，之前却被当成「邯郸有什么好玩的」，
    回了广府古城、娲皇宫、丛台公园——问题类型没识别，清单凑数。
    """
    from travel_agent.llm import ScriptedClient

    c = ScriptedClient(["邯郸方特国色春秋是本地唯一的主题乐园，"
                        "其余多是古城、石窟这类人文景点。"])
    s = Session(today=TODAY, client=c)
    r = s.turn("邯郸本地或周边的中小型游乐园有哪些")
    check("品类问句按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("只报同品类", "方特" in r.render, r.render[:160])
    check("不拿古城石窟凑数",
          "娲皇宫" not in r.render and "丛台公园" not in r.render, r.render[:160])


def test_poi_category_no_local_hit_goes_online() -> None:
    """本地没有这个品类 → 联网作答；联网也没有才如实说没收录。"""
    from travel_agent.llm import ScriptedClient
    import travel_agent.websearch as ws

    orig = ws.search
    try:
        ws.search = lambda q, **k: {"ok": True, "note": "联网检索完成",
            "results": [{"title": "邯郸周边温泉",
                         "url": "https://example.cn/hd-wq",
                         "snippet": "邯郸市区没有温泉，周边涉县、武安一带有几处",
                         "site": "example.cn"}]}
        c = ScriptedClient(["邯郸 温泉",
                            "邯郸市区没有温泉，涉县、武安一带有，得自驾过去。"])
        s = Session(today=TODAY, client=c)
        r = s.turn("邯郸有温泉吗")
        check("没有同品类时走联网", r.mode == "answer" and "涉县" in r.render,
              f"mode={r.mode} {r.render[:120]}")
        check("不拿不相干景点凑数", "娲皇宫" not in r.render, r.render[:120])

        # 连联网都没有：如实说没收录，不编名字
        c2 = ScriptedClient(["邯郸 温泉"])
        ws.search = lambda q, **k: {"ok": False, "note": "检索失败", "results": []}
        s2 = Session(today=TODAY, client=c2)
        r2 = s2.turn("邯郸有温泉吗")
        check("查不到就说不收录", "没收录" in r2.render, r2.render[:120])
    finally:
        ws.search = orig


def test_food_knowledge_answers() -> None:
    """「还有什么特色美食」→ 美食知识答（菜品文化），不拿景点或店名单凑数。"""
    s = Session(today=TODAY, client=None)
    s.slots["destination"] = "邯郸"
    r = s.turn("邯郸还有什么特色美食")
    check("美食知识问按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("报的是地方菜品", "二毛烧鸡" in r.render, r.render[:160])
    check("不拿景点凑数",
          "娲皇宫" not in r.render and "广府古城" not in r.render,
          r.render[:160])


def test_restaurant_question_lists_shops() -> None:
    """「好吃的馆子/火锅店」→ 搜真实餐厅 POI，不是菜品知识（2026-11-13 拆分）。"""
    s = Session(today=TODAY, client=None)
    s.slots["destination"] = "邯郸"
    r = s.turn("邯郸有什么好吃的馆子")
    check("找店问句按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("报的是馆子",
          "驴肉火烧" in r.render and "水饺" in r.render, r.render[:160])
    check("不拿景点凑数",
          "娲皇宫" not in r.render and "广府古城" not in r.render,
          r.render[:160])


def test_stay_question_lists_hotels_not_replan() -> None:
    """「有什么酒店」→ 报在售酒店，绝不触发整单重排（实测回了一份全新 5 天行程）。"""
    from travel_agent.llm import ScriptedClient

    c = ScriptedClient(["能订的有四家，丛台大酒店 260 起，尚客优精选 110 起，"
                        "都在市中心附近。"])
    s = Session(today=TODAY, client=c)
    s.slots["destination"] = "邯郸"
    r = s.turn("邯郸有什么酒店")
    check("住宿问句按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("报的是酒店与价格",
          "酒店" in r.render and "每晚" in r.render, r.render[:160])
    check("没有整单重排",
          "出行概要" not in r.render and "行程日期" not in r.render,
          r.render[:160])


def test_nearby_food_hotel_route_split() -> None:
    """周边问句独立分流（2026-11-13）：「宽窄巷子附近有什么好吃的」是找餐厅，
    「附近有什么酒店」是找住处、「从X怎么去Y」是问路线——都不能被景点
    点名拦截提前吃掉（实测景点一命中就按景点作答，周边查询提前结束）。"""
    from travel_agent.llm import ScriptedClient

    # 1) 周边餐饮：报真实餐厅，不是景点介绍
    c1 = ScriptedClient(["古城里的两家：石井私房菜本地味，梅子井酒家老字号。"])
    s1 = Session(today=TODAY, client=c1)
    s1.slots["destination"] = "大理"
    r1 = s1.turn("大理古城附近有什么好吃的")
    check("周边餐饮按问答处理", r1.mode == "answer", f"mode={r1.mode}")
    check("报的是真实餐厅", "石井私房菜" in r1.render, r1.render[:160])
    check("不是景点介绍", "门票" not in r1.render.split(">")[0], r1.render[:160])

    # 2) 周边酒店：以景点为圆心由近到远报在售酒店
    s2 = Session(today=TODAY, client=None)
    s2.slots["destination"] = "大理"
    r2 = s2.turn("大理古城附近有什么酒店")
    check("周边酒店按问答处理", r2.mode == "answer", f"mode={r2.mode}")
    check("以景点为中心报酒店",
          "以「大理古城" in r2.render and "青青客栈" in r2.render,
          r2.render[:160])
    check("没有整单重排",
          "出行概要" not in r2.render and "行程日期" not in r2.render,
          r2.render[:160])

    # 3) 两点路线：两点都在库里 → 距离 + 耗时
    s3 = Session(today=TODAY, client=None)
    s3.slots["destination"] = "大理"
    r3 = s3.turn("从大理古城怎么去喜洲古镇")
    check("路线问句按问答处理", r3.mode == "answer", f"mode={r3.mode}")
    check("给出距离与耗时",
          "公里" in r3.render and "分钟" in r3.render, r3.render[:160])
    check("注明估算口径", "估算" in r3.render, r3.render[-160:])

    # 4) 改方案的指令句不被周边分流拦下
    s4 = Session(today=TODAY, client=None)
    s4.slots["destination"] = "大理"
    check("改方案指令不进周边分流",
          s4._nearby_gate("把大理古城附近的餐厅加进必去") is None)


def test_stay_question_triggers_hotel_autofill() -> None:
    """「有什么酒店」→ 库里没酒店先在线补库再答，补到要注明非实时报价（2026-11-13）。"""
    from travel_agent import autofetch as af
    from travel_agent import kb as kb_mod

    calls = {"n": 0}
    orig = af.ensure_city_data

    def fake(city, dtype, **kw):
        calls["n"] += 1
        check("补库类型是 hotel", dtype == "hotel", dtype)
        kb_mod.get_kb().upsert_hotel(city, {
            "name": f"{city}补录大酒店", "area": "市中心", "star": 4,
            "price": 520, "geo": [30.02, 120.01], "cancel_free": True,
            "dist_to_core_km": 1.2, "rating": 4.6,
            "note": "房价为星级估算，非实时报价"})
        return {"status": "fetched", "city": city, "source": "osm",
                "pois": 0, "hotels": 1, "message": "在线抓取 1 家住宿并入库"}

    af.ensure_city_data = fake
    try:
        s = Session(today=TODAY, client=None)
        s.slots["destination"] = "酒店补库城"
        r = s.turn("酒店补库城有什么酒店")
        check("没酒店触发一次在线补库", calls["n"] == 1, f"calls={calls['n']}")
        check("补库后报出酒店名", "补录大酒店" in r.render, r.render[:160])
        check("注明数据来源与口径", "数据说明" in r.render
              and "未验证实时房态" in r.render, r.render[-200:])
        # 库里已有数据：换一个会话再问，直接报库里的，不再触发补库
        s2 = Session(today=TODAY, client=None)
        s2.slots["destination"] = "酒店补库城"
        r2 = s2.turn("酒店补库城有什么酒店")
        check("再次询问不再重复补库", calls["n"] == 1, f"calls={calls['n']}")
        check("第二次同样报出酒店", "补录大酒店" in r2.render, r2.render[:160])
    finally:
        af.ensure_city_data = orig
        for h in kb_mod.get_kb().list_hotels("酒店补库城") or []:
            kb_mod.get_kb().delete_hotel(h["hotel_id"])


def test_meta_question_answered() -> None:
    """问助手本人的话（「你是什么模型」「你是做什么的」）必须答得上，不许回「行程已经在上面了」。

    实测（2026-11-12，网页端）：方案出过之后问这两句，都被「无脑重排拦截」
    兜住，回了一段「行程已经在上面了。想改的话直接说…」——用户：这些问题
    都不能回答。根因是闲聊词表只认「你是谁/你能做什么」，而 LLM 兜底分类
    的标签表里又没有 smalltalk（闲聊被映射成 none），两层都漏。
    """
    from travel_agent.llm import ScriptedClient, Client as _StubClient

    # 1) 无模型：纯规则词表也要认得出是闲聊，按身份模板答
    s = session()
    r = s.turn("你是什么模型")
    check("无模型时身份问题照样作答",
          r.mode == "answer" and "行程已经在上面了" not in r.render
          and "助手" in r.render, f"mode={r.mode} {r.render[:80]}")
    r2 = s.turn("你是做什么的")
    check("「你是做什么的」不被当成改行程",
          r2.mode == "answer" and "行程已经在上面了" not in r2.render,
          f"mode={r2.mode} {r2.render[:80]}")

    # 2) 有模型：身份问题交给模型自己说人话（不冒充具体商业型号）
    c = ScriptedClient(["我是这个产品里的旅行规划助手，背后接的是后台配置的大模型，"
                        "可以换。天气、班次、景点都能查，也能直接排行程。"])
    s2 = Session(today=TODAY, client=c)
    r3 = s2.turn("你是什么模型")
    check("有模型时身份问题由模型作答",
          r3.mode == "answer" and "助手" in r3.render, r3.render[:80])

    # 3) 方案已出 + 规则认不出的句子：交给模型闲聊作答，而不是固定话术
    class _ChatOnly(_StubClient):
        """模拟"只会闲聊、抽不出槽位"的模型：结构化任务返回空对象，日常对话给固定回复。"""

        def __init__(self, reply: str):
            super().__init__(api_key="stub")
            self.reply = reply

        def chat(self, system: str, user: str, **_kw) -> str:
            return "{}" if ("JSON" in (system or "") or "只输出" in (system or "")) \
                else self.reply

    s3 = session()
    r4 = s3.turn("11月8号去大理玩4天，2人，预算6000")
    check("前置：出过方案", r4.mode == "plan", f"mode={r4.mode}")
    s3.client = _ChatOnly("我还在学，哪儿答得不好你直接说，我按你的想法改行程。")
    r5 = s3.turn("你有点笨啊")
    check("方案后的闲话由模型接住",
          r5.mode == "answer" and "我还在学" in r5.render,
          f"mode={r5.mode} {r5.render[:80]}")
    # 模型不可用（脚本用尽）时回落固定话术，不能崩
    s4 = session()
    s4.turn("11月8号去大理玩4天，2人，预算6000")
    s4.client = ScriptedClient([])
    r6 = s4.turn("你有点笨啊")
    check("模型不可用时回落固定话术且不崩",
          r6.mode == "answer" and "行程已经在上面了" in r6.render,
          f"mode={r6.mode} {r6.render[:80]}")


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
               test_transport_time_change_refused,
               test_multi_city_request_not_poi,
               test_mixed_transport_time_and_trip,
               test_pace_noop_trims_overcap,
               test_pace_loosen_deletes_instead_of_relocating,
               test_day_trip_schedules_sights_and_no_hotel,
               test_day_trip_must_explain_what_does_not_fit,
               test_pace_loosen_without_excess_says_switched,
               test_spot_interest_placed_and_nap_daily,
               test_interest_missed_explains_swap,
               test_return_leg_direction_in_plan,
               test_pace_word_not_treated_as_interest,
               test_pace_noop_must_overcap_honest,
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
               test_filler_node_qa, test_plan_node_where_qa,
               test_reco_restaurant_not_replan, test_no_new_slots_no_replan,
               test_bare_day_answered_once, test_month_then_day_merges,
               test_day_then_month_not_overridden,
               test_meta_question_answered,
               test_comparison_question_not_generic_list,
               test_poi_category_question_filters_by_type,
               test_food_knowledge_answers,
               test_restaurant_question_lists_shops,
               test_day_change_keeps_days_and_places_spot,
               test_stay_question_lists_hotels_not_replan,
               test_nearby_food_hotel_route_split,
               test_stay_question_triggers_hotel_autofill,
               test_poi_category_no_local_hit_goes_online,
               test_chat_history_attached):
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
