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
    _poi("p6", "都江堰", [30.572, 104.066], appeal=5, dur=240, hours="全天",
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
    """行程页路线图：配了 Key 用真实腾讯地图（合规底图），没配回落 SVG 示意图。"""
    from travel_agent import render_html
    plan, _ = _compose()
    res = _fake_result(days_plan=plan)
    orig = render_html.get_tmap_key
    try:
        render_html.get_tmap_key = lambda: ""
        h = render_html.build_html(res, "成都4天")
        check("没配 Key 时不加载地图 SDK", "map.qq.com/api/gljs" not in h)
        check("没配 Key 时仍有 SVG 示意图", "<svg" in h)
        check("没配 Key 时提示去哪里申请 Key", "lbs.qq.com" in h)

        render_html.get_tmap_key = lambda: "TESTKEY1234567890"
        h2 = render_html.build_html(res, "成都4天")
        check("配 Key 后加载腾讯地图 GL JS", "map.qq.com/api/gljs" in h2)
        check("地图容器与 SVG 兜底同时存在",
              'id="route-map"' in h2 and 'id="route-fallback"' in h2)
        check("行程点位注入地图脚本", "成都大熊猫繁育研究基地" in h2 or "宽窄巷子" in h2)
        check("Key 只以安全字符注入", "TESTKEY1234567890" in h2)
        check("估算点不进连线的语义写进页面", "不参与连线" in h2)
    finally:
        render_html.get_tmap_key = orig


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


def main() -> int:
    for fn in (test_no_time_overlap, test_uncertain_geo_no_fake_route,
               test_interests_landed, test_interest_is_explainable,
               test_interest_matcher, test_weather_window_is_per_day,
               test_forecast_window_self_heal, test_transport_card_no_zero_fare,
               test_meal_fallback_is_honest, test_diet_and_tea_land_in_meals,
               test_map_marks_uncertain_points, test_route_map_real,
               test_route_map_js_syntax,
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
