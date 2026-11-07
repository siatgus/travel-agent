"""方案质量回归：把一次真实试用暴露的问题变成可断言项。

2026-11-06 用户跑完「成都 + 都江堰 4 天」后反馈了 8 条问题，这里逐条钉住：

1. 位置查不到的景点（都江堰）不能给出"武侯祠打车 16 分钟、8.81 公里"这种精确路线；
   也不能排在抵达日下午，得单独占一天。
2. 返程日「早市 09:00-10:00」和「望江楼 09:15」时间重叠。
3. 兴趣偏好（大熊猫 / 喝茶 / 老街）要真的落进行程。
4. 「别太赶」在已经是最松档时要明说，而不是静默给一份一样的方案。
5. 没有出发地时，预算标题必须写「不含往返大交通」。
6. 天气口径：超出预报窗口的日期要标成历史同期，不能和实时预报长得一样。
7. 预算明细里不能出现 local_transit 这类英文键名。

运行：python tests/test_plan_quality.py　失败退出码 1。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path as _TA_PATH

_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

from travel_agent import agent, composer
from travel_agent.dialogue import Session

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


# ------------------------------------------------------------------ 固定数据集


def _poi(pid, name, geo, *, cat="attraction", appeal=3, dur=90, cost=0,
         hours="08:00-18:00", tags=None, note="", intensity="low"):
    return {"poi_id": pid, "name": name, "category": cat, "area": "成都",
            "geo": geo, "appeal": appeal, "duration_min": dur, "cost": cost,
            "open_hours": hours, "open_days": "每天", "need_booking": False,
            "tags": tags or ["attraction"], "intensity": intensity,
            "indoor_alternative": "", "note": note}


CHENGDU = [
    _poi("p1", "成都大熊猫繁育研究基地", [30.73, 104.15], appeal=4, dur=180,
         cost=55, hours="07:30-18:00"),
    _poi("p2", "武侯祠", [30.65, 104.05], appeal=4, cost=60, tags=["museum"]),
    _poi("p3", "宽窄巷子", [30.67, 104.06], dur=120, hours="全天", note="老街"),
    _poi("p4", "人民公园", [30.66, 104.06], hours="06:00-22:00",
         tags=["park"], note="鹤鸣茶社，盖碗茶"),
    _poi("p5", "望江楼公园", [30.63, 104.09], hours="06:00-21:00", tags=["park"]),
    _poi("p6", "都江堰", [30.572, 104.066], appeal=5, dur=240, cost=55, hours="全天",
         tags=["用户点名", "位置估算"], intensity="medium",
         note="用户点名安排；未收录，位置按市区估算"),
    _poi("f1", "陈麻婆豆腐", [30.66, 104.06], cat="restaurant", dur=60, cost=80,
         hours="11:00-21:00", tags=["food"]),
]

SLOTS = {
    "destination": "成都", "origin": None,
    "date_range": {"start": "2026-11-20", "end": "2026-11-23"},
    "days": 4, "party_size": 2, "pace": "relaxed", "budget_total": 8000,
    "must_visit": ["都江堰"], "soft_prefs": ["大熊猫", "喝茶", "老街"],
    "avoid": [], "hard_prefs": ["不吃辣"], "diet": ["不吃辣"],
    "stay_pref": "", "lodge": "",
}

WEATHER = {"days": [{"date": "2026-11-20", "low": 8, "high": 16,
                     "rain_prob": 10, "desc": "晴到多云（历史同期）",
                     "is_forecast": False}],
           "note": "超出预报窗口：历史同期均值与常年气候，仅供参考"}


def _compose():
    return composer.compose(SLOTS, {"items": CHENGDU}, WEATHER,
                            "12:00", "18:00", [30.66, 104.06])


def _minutes(t: str) -> int:
    hh, mm = (int(x) for x in t.split(":"))
    return hh * 60 + mm


# ------------------------------------------------------------------ 逐条断言


def test_no_time_overlap() -> None:
    """任何一天里，下一项的开始时间都不能早于上一项的结束时间。"""
    plan, _ = _compose()
    bad = []
    for d in plan:
        nodes = d["nodes"]
        for a, b in zip(nodes, nodes[1:]):
            if _minutes(b["time"]) < _minutes(a["time"]) + int(a["duration_min"]):
                bad.append(f"Day{d['day']} {a['time']}+{a['duration_min']}min 与 {b['time']} 重叠")
    check("每天节点时间不重叠（早市 09:00-10:00 之后才排景点）", not bad, "；".join(bad))
    last = plan[-1]["nodes"]
    market = [n for n in last if "早市" in n["name"]]
    check("返程日仍有早市这一站", bool(market))
    if market:
        check("早市之后的景点不早于 10:00",
              all(_minutes(n["time"]) >= 600 for n in last
                  if "早市" not in n["name"]), str([n["time"] for n in last]))


def test_uncertain_geo_no_fake_route() -> None:
    """位置按市区估算的点：不给距离/时长，也不排进抵达日。"""
    plan, _ = _compose()
    nodes = [(d, n) for d in plan for n in d["nodes"] if n["name"] == "都江堰"]
    check("都江堰仍按用户点名排进行程", bool(nodes))
    if not nodes:
        return
    d, n = nodes[0]
    check("估算坐标不给精确里程", "km" not in n["transit"] and "公里" not in n["transit"],
          n["transit"])
    check("交通一栏写明位置待确认", "位置待确认" in n["transit"], n["transit"])
    check("不排在抵达日（第 1 天）", d["day"] != 1, f"排在第 {d['day']} 天")
    check("当天标记 geo_uncertain（前端据此提示）", d.get("geo_uncertain") is True)
    check("单独占一天，不跟市区景点拼团",
          len([x for x in d["nodes"] if x["type"] == "attraction"]) == 1,
          str([x["name"] for x in d["nodes"]]))


def test_interests_landed() -> None:
    """兴趣偏好必须落进行程：大熊猫 / 喝茶 / 老街。"""
    plan, _ = _compose()
    names = " ".join(n["name"] for d in plan for n in d["nodes"])
    check("大熊猫落进行程", "大熊猫" in names, names)
    check("喝茶（人民公园）落进行程", "人民公园" in names, names)
    check("老街（宽窄巷子）落进行程", "宽窄巷子" in names, names)


def test_interest_is_explainable() -> None:
    """「喝茶」落到人民公园这件事，行程里必须写清楚是靠什么落地的。

    2026-11-06 反馈：只看到"人民公园"，分不清是兴趣命中还是"公园"沾边带进来的，
    也不知道喝茶到底落实成了什么活动。
    """
    plan, _ = _compose()
    node = next((n for d in plan for n in d["nodes"] if n["name"] == "人民公园"), None)
    check("人民公园排进了行程", node is not None)
    if node is None:
        return
    ints = node.get("interests") or []
    words = [i.get("word") for i in ints]
    check("节点带兴趣标记", "喝茶" in words, str(ints))
    tea = next((i for i in ints if i.get("word") == "喝茶"), {})
    # 库里有"鹤鸣茶社、盖碗茶"这种线索 → 判为强命中，并把线索写进备注
    check("「喝茶」是有据可依的强命中", tea.get("level") == "strong", str(tea))
    check("备注里写明具体活动线索（茶社/盖碗）",
          "茶社" in node["tip"] or "盖碗" in node["tip"], node["tip"])
    # 望江楼公园也含"公园"，但不该被标成"喝茶"——否则像在凑数
    other = [n for d in plan for n in d["nodes"] if n["name"] == "望江楼公园"]
    check("同一个兴趣不会给两个公园都打标",
          all(not (n.get("interests") or []) for n in other),
          str([(n["name"], n.get("interests")) for n in other]))


def test_interest_matcher() -> None:
    check("「大熊猫」命中大熊猫基地",
          composer.interest_hit("大熊猫", CHENGDU[0]))
    check("「喝茶」命中茶馆/公园",
          composer.interest_hit("喝茶", CHENGDU[3]))
    check("「老街」命中巷子类",
          composer.interest_hit("老街", CHENGDU[2]))
    check("无关兴趣不误命中",
          not composer.interest_hit("滑雪", CHENGDU[1]))
    # 强弱分档：只有具体线索才算 strong
    check("喝茶→人民公园（有茶社）判强命中",
          composer.interest_match("喝茶", CHENGDU[3])[0] == "strong")
    check("喝茶→望江楼公园只算弱命中",
          composer.interest_match("喝茶", CHENGDU[4])[0] == "weak",
          str(composer.interest_match("喝茶", CHENGDU[4])))
    check("弱命中不能抢锚点",
          not composer.interest_hit("喝茶", CHENGDU[4], strong_only=True))


def test_weather_window_is_per_day() -> None:
    """预报窗口必须逐日判断，不能因为最后一天越界就把前几天也说成历史同期。

    2026-11-06 反馈：11/20-23 四天分别距今 14/15/16/17 天，前两天本该是真预报，
    方案却四天全标历史同期。根因是请求的结束日期超出 Open-Meteo 上限（今天+15），
    整个请求 400，预报整批丢失。
    """
    wx = {"days": [
        {"date": "2026-11-20", "is_forecast": True},
        {"date": "2026-11-21", "is_forecast": True},
        {"date": "2026-11-22", "is_forecast": False},
        {"date": "2026-11-23", "is_forecast": False},
    ]}
    cal = agent._wx_caliber(wx)
    check("预报与历史同期分开说，并写明分界",
          "11-20~11-21" in cal and "11-22" in cal, cal)
    check("不再把整段说成历史同期", "这几天" not in cal, cal)


def test_forecast_window_self_heal() -> None:
    """结束日期越界会整段 400：要能把 end 收回来重试，而不是整段降级。"""
    from travel_agent import real_tools
    seen: list[str] = []

    def fake(url: str):
        seen.append(url)
        if "end_date=2026-12-31" in url:
            return None                     # 模拟越界 400
        return {"daily": {"time": ["2026-11-20", "2026-11-21"],
                          "temperature_2m_min": [8, 9],
                          "temperature_2m_max": [16, 17],
                          "precipitation_probability_max": [10, 20]}}

    orig = real_tools._http_json
    real_tools._http_json = fake
    try:
        out = real_tools._wx_forecast([30.66, 104.06], "2026-11-20", "2026-12-31")
    finally:
        real_tools._http_json = orig
    check("越界后自动收缩结束日期重试", len(seen) >= 2, str(len(seen)))
    check("最终仍拿到预报（不是整段历史同期）",
          bool(out) and "2026-11-20" in out, str(out))


# ------------------------------------------------------------------ 渲染


def _fake_result(**over):
    res = {
        "slots": dict(SLOTS),
        "origin": None,
        "has_origin": False,
        "assumptions": [],
        "weather": dict(WEATHER),
        "outbound": None, "inbound": None,
        "hotel": {"ok": True, "items": [{"name": "客栈", "price": 400,
                                         "geo": [30.66, 104.06]}]},
        "days_plan": [{
            "day": 1, "date": "2026-11-20", "theme": "抵达适应",
            "intensity": "low", "move_min": 20, "day_budget": 300,
            "rainy_plan": "户外优先", "geo_uncertain": False,
            "nodes": [{"time": "12:00", "name": "抵达 + 入住", "type": "transit",
                       "duration_min": 90, "transit": "车站打车", "tip": "",
                       "cost": 0, "booking_required": False, "poi_id": "n1",
                       "geo": [30.66, 104.06]}],
        }],
        "budget": {"total": 3718, "per_person": 1859, "target": 8000,
                   "contingency": 338, "within_budget": True,
                   "breakdown": [
                       {"category": "hotel", "label": "住宿 3 晚", "amount": 1200},
                       {"category": "ticket", "label": "门票", "amount": 230},
                       {"category": "food", "label": "餐饮", "amount": 1080},
                       {"category": "local_transit", "label": "市内交通", "amount": 480},
                   ]},
        "cut_plans": [], "must_report": {}, "trace": [],
    }
    res.update(over)
    return res


def test_render_budget_and_weather() -> None:
    text = agent.render(_fake_result())
    check("预算明细不出现英文键名 local_transit", "local_transit" not in text)
    check("预算分类用中文（市内交通）", "市内交通" in text)
    check("金额不带 .0 浮点尾巴（¥3,718）", "¥3,718" in text and "¥3,718.0" not in text,
          text[-200:])
    check("无出发地时合计标注不含往返大交通",
          "不含往返大交通" in text, text[-300:])
    check("概要里的预算也标注不含大交通",
          "预算合计（不含往返大交通）" in text)
    check("天气口径写明历史同期",
          "历史同期" in text and "预报" in text, text[:400])
    # 明细里确实有交通费时，才不该再写"不含大交通"
    _with_tr = _fake_result(origin="杭州", has_origin=True)
    _with_tr["budget"] = dict(_with_tr["budget"])
    _with_tr["budget"]["breakdown"] = _with_tr["budget"]["breakdown"] + [
        {"category": "transport", "label": "往 杭州-大理 ¥520", "amount": 1040}]
    text2 = agent.render(_with_tr)
    check("已含交通费时不标注不含大交通", "不含往返大交通" not in text2,
          text2[-300:])
    # 出发地填了、但班次票价没查到（明细里没有交通项）：金额同样不含路费，
    # 必须照旧标注——否则 ¥3,553 会被当成整趟花费。
    text3 = agent.render(_fake_result(origin="上海", has_origin=True))
    check("有出发地但班次票价缺失仍标注不含大交通",
          "预算合计（不含往返大交通）" in text3 and "班次票价没查到" in text3,
          text3[:400])


def test_pace_noop_is_honest() -> None:
    """已经是最松档时点「别太赶」：不许再跑一遍排程，也不许假装重排。

    2026-11-06 反馈：回复承认"已是最松"，却又说"这一版只把时间与动线重排了
    一遍"，而行程和预算一个字都没变——白等一轮，还像在骗人。
    正确行为：直接回答（mode=answer），明说"这一轮没有改行程"，并给出真能
    生效的选项。
    """
    s = Session(today=date(2026, 11, 6))
    r1 = s.turn("11月20号从杭州去大理玩4天，2人，预算6000，轻松一点")
    check("前置：第一版出来了", r1.mode == "plan", r1.mode)
    before = r1.plan
    r2 = s.turn("行程别太赶，轻松一点")
    check("节奏没变时直接回答，不再出一版新方案", r2.mode == "answer", r2.mode)
    check("明说已是最松档", "最松" in r2.render, r2.render[:200])
    check("明说这一轮没有改行程", "没有改行程" in r2.render, r2.render[:200])
    check("不再谎称重排过", "重排了一遍" not in r2.render, r2.render[:200])
    check("给出真能生效的选项（删站/减天）",
          "删掉" in r2.render and "改" in r2.render, r2.render[:400])
    check("原方案未被覆盖", s.last_plan is before)


def test_transport_card_no_zero_fare() -> None:
    """班次/票价没查到时不渲染「去程 — ¥0」：0 元会被当成免费。

    2026-11-06 反馈：页面已注明大交通未含，交通卡仍显示两行 ¥0 占位。
    """
    from travel_agent.render_html import render_transport
    h = render_transport(_fake_result(
        outbound={"ok": True, "items": []}, inbound={"ok": True, "items": []}))
    check("班次没查到时不渲染 ¥0", "¥0" not in h, h[:300])
    check("写明未查到与未计入预算", "未查到" in h and "未计入预算" in h, h[:300])
    h2 = render_transport(_fake_result(
        outbound={"ok": True, "items": [
            {"code": "G123", "depart": "08:00", "arrive": "11:30",
             "total_price": 520}]},
        inbound=None))
    check("有票价时正常显示班次与金额", "G123" in h2 and "¥520" in h2, h2[:300])


def test_meal_fallback_is_honest() -> None:
    """库里没有餐厅也要留用餐时段，且如实写「就近自选」，不冒充具体店。"""
    slots = dict(SLOTS)
    slots["days"] = 3
    slots["date_range"] = {"start": "2026-11-20", "end": "2026-11-22"}
    no_food = [p for p in CHENGDU if p["category"] != "restaurant"]
    plan, _ = composer.compose(slots, {"items": no_food}, WEATHER,
                               "12:00", "18:00", [30.66, 104.06])
    meals = [(d["day"], n) for d in plan for n in d["nodes"]
             if n["type"] == "restaurant"]
    check("无餐厅数据时仍排用餐时段", bool(meals),
          str([(d, n["name"]) for d, n in meals]))
    check("兜底餐如实写「就近自选」",
          all("就近自选" in n["name"] for _, n in meals),
          str([n["name"] for _, n in meals]))
    check("兜底餐说明资料库没有餐厅数据",
          all("资料库暂无当地餐厅" in n["tip"] for _, n in meals),
          str([n["tip"] for _, n in meals]))
    # 返程日不排正餐（早市/伴手礼后直接返程），抵达日与中间日都该有
    days_with_meal = {d for d, _ in meals}
    check("除返程日外每天都有用餐点", days_with_meal >= {1, 2},
          str(days_with_meal))


def test_diet_and_tea_land_in_meals() -> None:
    """「不吃辣」驱动选店，「喝茶」靠茶馆落地成具体活动。"""
    slots = dict(SLOTS)
    slots["days"] = 3
    slots["date_range"] = {"start": "2026-11-20", "end": "2026-11-22"}
    mild = _poi("t1", "鹤鸣茶社", [30.6596, 104.0565], cat="restaurant",
                dur=60, cost=30, hours="08:00-22:00",
                tags=["茶馆", "喝茶", "盖碗茶", "午餐", "晚餐", "不辣"],
                note="盖碗茶，竹椅露天")
    hot = _poi("t2", "陈麻婆豆腐", [30.66, 104.06], cat="restaurant",
               dur=60, cost=80, hours="11:00-21:00",
               tags=["辣", "川菜"], note="麻辣川菜")
    pool = [p for p in CHENGDU if p["category"] != "restaurant"] + [mild, hot]
    plan, _ = composer.compose(slots, {"items": pool}, WEATHER,
                               "12:00", "18:00", [30.66, 104.06])
    meals = [n for d in plan for n in d["nodes"]
             if n["type"] == "restaurant" and "就近自选" not in n["name"]]
    check("不吃辣时不选麻辣川菜",
          bool(meals) and all("陈麻婆" not in n["name"] for n in meals),
          str([n["name"] for n in meals]))
    tea = [n for d in plan for n in d["nodes"] if n["name"] == "鹤鸣茶社"]
    check("茶馆作为用餐位排进行程", bool(tea))
    if tea:
        ints = tea[0].get("interests") or []
        check("餐位节点也标兴趣（喝茶·强命中）",
              any(i["word"] == "喝茶" and i["level"] == "strong" for i in ints),
              str(ints))
        check("点单提示按不吃辣生成", "不吃辣" in tea[0]["tip"], tea[0]["tip"])


def test_spot_day_phrase_is_not_trip_days() -> None:
    """「加上都江堰，安排一天」是给点留时长，不是"整趟 1 天"。

    2026-11-07 反馈：这样一句话把 4 天行程改成"成都 1 天 1 晚"，
    都江堰也没排进去；改成「加上都江堰」才正常。
    """
    r1 = agent.extract_slots("加上都江堰，安排一天")
    check("「加上都江堰，安排一天」不把天数抽成 1", not r1.get("days"),
          str(r1.get("days")))
    r2 = agent.extract_slots("安排一天去大熊猫基地")
    check("「安排一天去X」天数不动", not r2.get("days"), str(r2.get("days")))
    check("「安排一天去X」X 进必去", "大熊猫基地" in (r2.get("must_visit") or []),
          str(r2.get("must_visit")))
    check("「帮我安排三天的行程」仍是 3 天",
          agent.extract_slots("帮我安排三天的行程").get("days") == 3)
    check("「去成都玩4天」仍是 4 天",
          agent.extract_slots("去成都玩4天").get("days") == 4)
    check("「安排三天玩」仍是 3 天",
          agent.extract_slots("安排三天玩").get("days") == 3)
    check("「多住一天」记为延长增量而不是天数 1",
          agent.extract_slots("多住一天").get("days_extend") == 1)
    # 对话层：已排 4 天后说「安排一天」，天数必须还是 4
    s = Session(today=date(2026, 11, 6))
    r = s.turn("11月20号从杭州去大理玩4天，2人，预算6000，轻松一点")
    check("前置：4 天方案出来了", r.mode == "plan", r.mode)
    s.turn("加上都江堰，安排一天")
    check("对话层兜住：天数保持 4", (s.slots or {}).get("days") == 4,
          str((s.slots or {}).get("days")))


def test_fallback_meal_consumes_time() -> None:
    """兜底餐「就近自选」也要占 60 分钟，否则下一站从餐位开始 23 分钟后排。

    2026-11-07 反馈：午餐 12:27-13:27，望江楼公园却排在 12:50。
    """
    slots = dict(SLOTS)
    slots["days"] = 3
    slots["date_range"] = {"start": "2026-11-20", "end": "2026-11-22"}
    no_food = [p for p in CHENGDU if p["category"] != "restaurant"]
    plan, _ = composer.compose(slots, {"items": no_food}, WEATHER,
                               "12:00", "18:00", [30.66, 104.06])
    bad = []
    for d in plan:
        ns = sorted(d["nodes"], key=lambda x: x["time"])
        for a, b in zip(ns, ns[1:]):
            if _minutes(b["time"]) < _minutes(a["time"]) + int(a["duration_min"]):
                bad.append(f"Day{d['day']} {a['name']}({a['time']}) 与 {b['name']}({b['time']})")
    check("兜底餐占时间后全行程无重叠", not bad, "；".join(bad))


def test_return_day_lunch_and_honest_check() -> None:
    """返程日 13 点后发车要有午餐；质检按发车时间判断，不再无条件豁免。"""
    plan, _ = _compose()          # outbound_depart_last 默认 18:00
    last = plan[-1]
    check("返程日（傍晚发车）排了午餐",
          any(n["type"] == "restaurant" for n in last["nodes"]),
          str([(n["time"], n["name"]) for n in last["nodes"]]))
    res = _fake_result(days_plan=plan)
    meal = next(c for c in agent.self_check(res) if "用餐点" in c["item"])
    check("质检：返程日有餐 → 通过", meal["result"] == "pass", meal.get("evidence", ""))
    # 人为删掉返程日的餐位：下午才发车却没饭，质检必须亮红灯
    stripped = [dict(d) for d in plan]
    stripped[-1] = dict(stripped[-1])
    stripped[-1]["nodes"] = [n for n in stripped[-1]["nodes"]
                             if n["type"] != "restaurant"]
    meal2 = next(c for c in agent.self_check(_fake_result(days_plan=stripped))
                 if "用餐点" in c["item"])
    check("质检：下午发车却没餐 → fail", meal2["result"] == "fail",
          str(meal2))


def test_uncertain_ticket_assumption() -> None:
    """位置估算的点，门票口径必须写明：有价=已计入估算，没价=未计入。"""
    plan, _ = _compose()
    notes = agent.uncertain_ticket_notes(plan)
    check("都江堰门票口径写进假设", any("都江堰" in x for x in notes), str(notes))
    if notes:
        check("写明按多少/人估算且未核实",
              all("元/人估算" in x and "未核实" in x for x in notes), str(notes))
    # 库里没票价（cost=0）：要说"没有计入预算"，不能让用户以为门票 ¥110 含它
    _no_cost = [dict(p) for p in CHENGDU]
    _no_cost[5] = dict(_no_cost[5])
    _no_cost[5]["cost"] = 0
    _slots = dict(SLOTS)
    plan2, _ = composer.compose(_slots, {"items": _no_cost}, WEATHER,
                                "12:00", "18:00", [30.66, 104.06])
    notes2 = agent.uncertain_ticket_notes(plan2)
    check("没查到票价时明说门票未计入",
          any("都江堰" in x and "没有计入预算" in x for x in notes2), str(notes2))


def test_map_marks_uncertain_points() -> None:
    """坐标估算的点在示意图上必须一眼能认出来：虚线空心圈 + 位置估算标注。"""
    from travel_agent.render_html import render_map
    plan, _ = _compose()
    svg = render_map(_fake_result(days_plan=plan))
    check("不确定点画虚线空心圈", 'stroke-dasharray="3 3"' in svg, svg[:400])
    check("旁边标注「位置估算」", "位置估算" in svg, svg[:400])
    # 确定坐标的点还是实心圆序号
    check("确定坐标的点仍是实心序号圆", 'fill="#fff"' in svg)


def test_route_map_real() -> None:
    """行程页路线图：配了 Key 用天地图（国家地理信息公共服务平台），没配回落 SVG。"""
    from travel_agent import render_html
    plan, _ = _compose()
    res = _fake_result(days_plan=plan)
    orig = render_html.get_tdt_key
    try:
        render_html.get_tdt_key = lambda: ""
        h = render_html.build_html(res, "成都4天")
        check("没配 Key 时不加载地图 SDK", "api.tianditu.gov.cn" not in h)
        check("不再引用腾讯地图", "map.qq.com" not in h)
        check("没配 Key 时仍有 SVG 示意图", "<svg" in h)
        check("没配 Key 时提示去哪里申请 Key", "console.tianditu.gov.cn" in h)

        render_html.get_tdt_key = lambda: "0123456789abcdef0123456789abcdef"
        h2 = render_html.build_html(res, "成都4天")
        check("配 Key 后加载天地图 JS API", "api.tianditu.gov.cn/api?v=4.0&tk=" in h2)
        check("地图容器与 SVG 兜底同时存在",
              'id="route-map"' in h2 and 'id="route-fallback"' in h2)
        check("行程点位注入地图脚本", "成都大熊猫繁育研究基地" in h2 or "宽窄巷子" in h2)
        check("Key 只以安全字符注入",
              'var TK = "0123456789abcdef0123456789abcdef"' in h2)
        check("标注底图来源是天地图", "国家地理信息公共服务平台" in h2)
        check("WGS-84 直接上图，不做 GCJ-02 纠偏",
              "w2g" not in h2 and "0.00669342162296594323" not in h2)
        check("估算点不进连线的语义写进页面", "不参与连线" in h2)
        check("页脚不再说数据来自 mock 工具层", "mock 工具层" not in h2)
        check("页脚写明真实数据口径", "Open-Meteo" in h2 and "历史同期" in h2)
        check("有天数筛选页签（总览/第N天）",
              'id="rt-tabs"' in h2 and 'data-idx="-1"' in h2 and "第2天" in h2)
        check("页签带每日配色圆点", ".rt-tabs button i" in h2)
        # hover 卡片：按钮白字必须 !important（天地图 InfoWindow 的 a 样式会盖掉普通声明）
        check("介绍按钮白字带 !important",
              ".rt-card-link{display:block" in h2.replace(" ", "")
              or "color:#FFFFFF!important" in h2)
        # hover 开、移出关：状态机 + 卡片内联事件必须同时在
        check("hover 关闭状态机存在",
              "function hoverStay" in h2 and "function hoverClose" in h2
              and "setTimeout" in h2)
        check("卡片挂移入/移出内联事件",
              'onmouseenter="RThover.stay()"' in h2
              and 'onmouseleave="RThover.leave()"' in h2
              and "window.RThover" in h2)
        check("图钉挂 mouseout 关闭", "addEventListener('mouseout'" in h2)
        check("地图容器 620px 高（放大版）",
              'id="route-map" style="height:620px' in h2)
        check("地图卡横向突破正文栏宽（rt-mapcard）",
              'card rt-mapcard' in h2 and ".rt-mapcard" in h2
              and "translateX(-50%)" in h2)
        check("窄屏回落为全宽 440 高",
              "@media (max-width:900px)" in h2 and "height:440px" in h2)
        check("地图不再嵌套双层卡片",
              h2.count('<div class="card rt-mapcard"') == 1
              and '<div class="card" style="padding:12px;">' not in h2)
        check("动线上有「第N天」徽标", "rt-badge" in h2 and "addBadge" in h2)
        check("徽标底色加深一档（白字不发飘）",
              "function darken" in h2 and "darken(color, 0.68)" in h2
              and "text-shadow:0 1px 2px" in h2)
        check("点击页签只看某天（show/hide 逻辑）",
              "setTab" in h2 and "o.show()" in h2 and "o.hide()" in h2)
        check("按天分色实线动线", "new T.Polyline(chain" in h2 or "new T.Polyline(" in h2)
        check("用天地图路径规划服务画真实道路",
              "new T.DrivingRoute(" in h2 and "getPlan(0).getPath()" in h2)
        check("规划结果按天替换直线占位", "replaceGhost" in h2 and "lineStyle: 'dashed'" in h2)
        check("路径规划失败也退回直线（不会白屏）",
              "规划失败已退回直线" in h2)
        check("有「我的位置」定位按钮", 'id="rt-locate"' in h2)
        check("定位走浏览器定位 + IP 定位兜底",
              "navigator.geolocation" in h2 and "new T.Geolocation()" in h2)
        check("定位点用蓝点 + 精度圈显示", "locCircle" in h2 and "new T.Circle(" in h2)
        check("页脚说明动线来自路径规划而非直线示意",
              "驾车路线规划" in h2 and "非实时路况" in h2)
        check("图钉分类型图标（楼/刀叉/列车/床）",
              all(f"case '{k}'" in h2 for k in ("sight", "restaurant", "transit", "hotel")))
        check("餐位/车站也插旗（不只景点）",
              "n.type === 'restaurant'" in h2 and "n.type === 'transit'" in h2)
        check("估算点是灰色?图钉", "'#9A9992', 'unknown', '?'" in h2)
        check("住宿用床图标", "'hotel', '', '住宿'" in h2)
        check("页脚有图钉图例", "序号+楼=景点" in h2 and "刀叉=餐饮" in h2)

        render_html.get_tdt_key = lambda: "abc';alert(1)//"
        h3 = render_html.build_html(res, "成都4天")
        check("非法字符的 Key 被过滤成纯字母数字", "alert(1)" not in h3)
    finally:
        render_html.get_tdt_key = orig

    # 环境变量优先于设置文件（部署时不想落盘就设 TIANDITU_KEY）
    import os
    orig_env = os.environ.get("TIANDITU_KEY")
    try:
        os.environ["TIANDITU_KEY"] = "envkey0123456789abcdef0123456789"
        check("TIANDITU_KEY 环境变量生效",
              render_html.get_tdt_key() == "envkey0123456789abcdef0123456789")
    finally:
        if orig_env is None:
            os.environ.pop("TIANDITU_KEY", None)
        else:
            os.environ["TIANDITU_KEY"] = orig_env


def test_route_map_js_syntax() -> None:
    """地图脚本是普通字符串模板：对模板本体做 node --check（有 node 才查）。"""
    import shutil
    import subprocess
    import tempfile
    from travel_agent import render_html
    node = shutil.which("node")
    if not node:
        print("[SKIP] 环境里没有 node，跳过 JS 语法检查")
        return
    js = (render_html.ROUTE_MAP_JS
          .replace("__DATA__", '{"days":[],"home":null,"colors":[]}')
          .replace("__KEY__", "TESTKEY"))
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        check("地图 JS 通过 node --check", r.returncode == 0, r.stderr[-300:])
    finally:
        _TA_PATH(tmp).unlink(missing_ok=True)


def test_wrap_contains_daily() -> None:
    """结构性回归：每日行程/质检结果必须还在 .wrap 容器里。

    2026-11-07 事故：地图页脚改文案时多写了一个 </div>，把 .wrap 提前关闭，
    「每日行程」之后全部掉出容器变全宽——div 数量恰好全局配平，字符串级
    测试（含 node --check）全查不出来。这里用真正的 HTML 解析器查嵌套。
    """
    from html.parser import HTMLParser
    from travel_agent import render_html
    plan, _ = _compose()
    res = _fake_result(days_plan=plan)
    h = render_html.build_html(res, "成都4天")

    class WrapTracker(HTMLParser):
        """喂入 HTML 前缀，报告「此刻是否还在 .wrap 里」。"""

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.depth = 0            # .wrap 内再嵌套的 div 层级
            self.in_wrap = False
        def handle_starttag(self, tag, attrs):
            if dict(attrs).get("class") == "wrap":
                self.in_wrap = True
                self.depth = 0
            elif self.in_wrap and tag == "div":
                self.depth += 1
        def handle_endtag(self, tag):
            if self.in_wrap and tag == "div":
                if self.depth == 0:
                    self.in_wrap = False   # .wrap 被提前关闭
                else:
                    self.depth -= 1

    for marker in ("每日行程", "质检结果"):
        pos = h.find(marker)
        assert pos > 0, marker
        t = WrapTracker()
        t.feed(h[:pos])
        check(f"「{marker}」仍在 .wrap 容器内", t.in_wrap,
              "wrap 已被提前关闭（DOM 结构破损，页面会变全宽）")


def test_kid_party_and_schedule() -> None:
    """2大1小：孩子计入人数与预算口径；带幼儿落成午休/推车/家庭房安排。"""
    s = agent.extract_slots_rules("2大1小（3岁）下月从杭州去大理玩4天，不太想赶")
    check("2大1小 人数=3（孩子算人）", s["party_size"] == 3, str(s.get("party_size")))
    check("大人与孩子拆开存", s["adults"] == 2 and s["children"] == 1)
    check("孩子年龄被抽到", s["child_age"] == 3)
    check("人数构成可读", "2 大 1 小" in (s["party_detail"] or "")
          and "3 岁" in (s["party_detail"] or ""))
    check("带幼儿偏好命中", "带幼儿" in s["hard_prefs"])
    s2 = agent.extract_slots_rules("一家三口去厦门玩3天")
    check("一家三口 = 2大1小", s2["party_size"] == 3 and s2["children"] == 1)
    s3 = agent.extract_slots_rules("我和老婆带5岁的娃去成都玩4天")
    check("我和老婆带5岁的娃 = 3人", s3["party_size"] == 3 and s3["child_age"] == 5,
          str({k: s3[k] for k in ("party_size", "child_age")}))
    check("纯大人句式不受影响",
          agent.extract_slots_rules("两个人去西安")["party_size"] == 2)
    # 预算口径：3 岁免票、8 岁半价、没说年龄也按半价；家庭房
    check("3 岁免票只按大人算门票", agent.kid_fare_heads(1, 3, 2, 3) == 2.0)
    check("8 岁按半价折算", agent.kid_fare_heads(1, 8, 2, 3) == 2.5)
    check("没说年龄也按半价", agent.kid_fare_heads(1, None, 2, 3) == 2.5)
    check("2大1小订 1 间家庭房", agent.hotel_rooms(1, 3) == (1, "家庭房/加床"))
    check("纯大人 3 人仍是 2 间", agent.hotel_rooms(0, 3) == (2, "间"))

    # 行程落地：午休节点 + 推车提示
    kid_slots = dict(SLOTS, party_size=3, adults=2, children=1, child_age=3,
                     party_detail="2 大 1 小（孩子 3 岁）")
    plan, _ = composer.compose(kid_slots, {"items": CHENGDU}, WEATHER,
                               "12:00", "18:00", [30.66, 104.06])
    naps = [n for d in plan for n in d["nodes"] if "午休" in n["name"]]
    check("中间日有 90 分钟午休节点",
          bool(naps) and all(n["duration_min"] == 90 for n in naps))
    check("午休不在抵达/返程日",
          all(d["day"] not in (1, len(plan)) for d in plan
              for n in d["nodes"] if "午休" in n["name"]))
    check("景点备注带推车/台阶提示",
          any("带娃提示" in (n.get("tip") or "")
              for d in plan for n in d["nodes"] if n["type"] == "attraction"))
    check("街巷类景点提示推车不便",
          any("推车不便" in (n.get("tip") or "")
              for d in plan for n in d["nodes"]))
    bad = []
    for d in plan:
        ns = d["nodes"]
        for a, b in zip(ns, ns[1:]):
            if _minutes(b["time"]) < _minutes(a["time"]) + int(a["duration_min"]):
                bad.append((d["day"], a["name"], b["name"]))
    check("插入午休后时间轴仍无重叠", not bad, str(bad[:3]))

    # 概要与可视化页都要说构成，不能只报"3 人"
    res = _fake_result(days_plan=plan)
    res["slots"] = kid_slots
    check("概要写人数构成",
          any("2 大 1 小" in ln and "共 3 人" in ln for ln in agent.summary_lines(res)),
          "\n".join(agent.summary_lines(res)))
    from travel_agent import render_html
    check("可视化页人数也写构成", "2 大 1 小" in render_html.build_html(res, "2大1小成都4天"))

    # 人数改口：2大1小 → 5个人，构成字段全部清空
    from travel_agent.dialogue import merge_slots, _explicit_fields
    delta = agent.extract_slots_rules("变成5个人")
    exp = _explicit_fields("变成5个人", delta)
    merged, _ = merge_slots(kid_slots, delta, exp, False)
    check("改口 5 个人后孩子字段清空",
          merged["party_size"] == 5 and not merged["children"]
          and not merged["party_detail"],
          str({k: merged.get(k) for k in ("party_size", "children", "party_detail")}))


def test_poi_info_cards() -> None:
    """景点介绍卡片：必应解析（离线样本）、来源优先级、断网降级、注入渲染与转义。"""
    from travel_agent import poi_links, render_html
    check("POI_LINKS 默认开启", poi_links.enabled())

    page = ('<html><body>'
            '<li class="b_algo"><h2><a href="https://www.mafengwo.cn/poi/123.html">'
            '宽窄巷子攻略</a></h2><p>宽窄巷子是成都著名历史街区…</p>'
            '<img class="rms_img" src="//th.bing.com/th?id=ABC"></li>'
            '<li class="b_algo"><h2><a href="https://www.zhihu.com/question/999">'
            '去成都宽窄巷子玩是什么体验？</a></h2>'
            '<p>本地人视角的宽窄巷子逛吃指南…</p></li>'
            '</body></html>')

    rs = poi_links._parse_page(page, 8)
    check("离线解析出 2 条候选", len(rs) == 2, str(len(rs)))
    check("缩略图补全 https 前缀", rs and rs[0]["img"] == "https://th.bing.com/th?id=ABC",
          str(rs[:1]))
    pick = poi_links._pick(rs, "宽窄巷子")
    check("介绍页优先选知乎", pick is not None and "zhihu.com" in pick["url"],
          str(pick and pick["url"]))
    check("结果不含景点名时宁缺毋滥",
          poi_links._pick([{"title": "完全无关", "url": "https://a.com/x",
                            "snippet": "别的", "site": "a.com"}], "宽窄巷子") is None)
    check("非 http 链接被丢弃", all(r["url"].startswith("http") for r in rs))

    orig_fetch = poi_links.websearch._fetch
    try:
        poi_links.websearch._fetch = lambda url, timeout=8: page
        poi_links._cache.pop("成都|宽窄巷子", None)
        info = poi_links.fetch_one("宽窄巷子", "成都")
        check("fetch_one 拿到知乎链接", bool(info) and "zhihu.com" in info["url"],
              str(info and info["url"]))
        poi_links._cache.pop("成都|都江堰", None)
        poi_links.websearch._fetch = lambda url, timeout=8: (_ for _ in ()).throw(
            IOError("断网"))
        check("断网时返回 None 不抛异常", poi_links.fetch_one("都江堰", "成都") is None)
    finally:
        poi_links.websearch._fetch = orig_fetch

    plan, _ = _compose()
    orig_fo = poi_links.fetch_one
    try:
        poi_links.fetch_one = lambda name, city, timeout=6: {
            "url": "https://www.zhihu.com/question/999", "site": "zhihu.com",
            "snippet": "本地人视角的宽窄巷子逛吃指南", "img": "https://th.bing.com/x.jpg"}
        res = _fake_result(days_plan=plan)
        poi_links.attach(res)
        got = res.get("poi_links") or {}
        check("attach 把卡片写进 poi_links",
              bool(got) and all("url" in v for v in got.values()), str(list(got))[:120])
        h = render_html.build_html(res, "成都4天")
        check("行程页带 hover 卡片结构", "rt-card" in h and "查看介绍" in h)
        check("缩略图 URL 注入", "th.bing.com/x.jpg" in h)
        check("卡片带来源与摘要", "介绍来源" in h and "逛吃指南" in h)
        check("悬停打开卡片（mouseover）", "mouseover" in h)
    finally:
        poi_links.fetch_one = orig_fo

    # 转义：url/snippet 里的恶意内容不能原样进 HTML
    res2 = _fake_result(days_plan=plan)
    res2["poi_links"] = {"宽窄巷子": {
        "url": "https://x.com/?a=1&b=<script>", "site": "x.com",
        "snippet": "<img src=x onerror=alert(1)>", "img": ""}}
    md = render_html.route_map_data(res2)
    links = [n["link"] for d in md["days"] for n in d["nodes"] if n.get("link")]
    check("route_map_data 转义恶意 url/snippet",
          links and "<script>" not in links[0]["url"]
          and "<img" not in links[0]["snippet"]
          and "&lt;" in links[0]["snippet"], str(links[:1]))
    check("没卡片的景点不带 link 字段",
          all(not n.get("link") for d in md["days"] for n in d["nodes"]
              if n["name"] != "宽窄巷子"))


def main() -> int:
    for fn in (test_no_time_overlap, test_uncertain_geo_no_fake_route,
               test_interests_landed, test_interest_is_explainable,
               test_interest_matcher, test_weather_window_is_per_day,
               test_forecast_window_self_heal, test_transport_card_no_zero_fare,
               test_meal_fallback_is_honest, test_diet_and_tea_land_in_meals,
               test_spot_day_phrase_is_not_trip_days,
               test_fallback_meal_consumes_time,
               test_return_day_lunch_and_honest_check,
               test_uncertain_ticket_assumption,
               test_kid_party_and_schedule,
               test_map_marks_uncertain_points, test_route_map_real,
               test_route_map_js_syntax, test_wrap_contains_daily,
               test_poi_info_cards,
               test_render_budget_and_weather, test_pace_noop_is_honest):
        fn()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条方案质量断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
