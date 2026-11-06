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


def test_interest_matcher() -> None:
    check("「大熊猫」命中大熊猫基地",
          composer.interest_hit("大熊猫", CHENGDU[0]))
    check("「喝茶」命中茶馆/公园",
          composer.interest_hit("喝茶", CHENGDU[3]))
    check("「老街」命中巷子类",
          composer.interest_hit("老街", CHENGDU[2]))
    check("无关兴趣不误命中",
          not composer.interest_hit("滑雪", CHENGDU[1]))


# ------------------------------------------------------------------ 渲染


def _fake_result(**over):
    res = {
        "slots": dict(SLOTS),
        "origin": None,
        "has_origin": False,
        "assumptions": [],
        "weather": dict(WEATHER),
        "outbound": None, "inbound": None,
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
    # 有出发地时不应再写"不含大交通"
    text2 = agent.render(_fake_result(origin="杭州", has_origin=True))
    check("有出发地时不标注不含大交通", "不含往返大交通" not in text2)


def test_pace_noop_is_honest() -> None:
    """已经是最松档时点「别太赶」：不静默给相同方案，要明说。"""
    s = Session(today=date(2026, 11, 6))
    r1 = s.turn("11月20号从杭州去大理玩4天，2人，预算6000，轻松一点")
    check("前置：第一版出来了", r1.mode == "plan", r1.mode)
    r2 = s.turn("行程别太赶，轻松一点")
    check("第二次仍出方案（不追问）", r2.mode == "plan", r2.mode)
    check("已是最松档时明说没有压缩空间",
          "最松" in r2.render, r2.render[:200])


def main() -> int:
    for fn in (test_no_time_overlap, test_uncertain_geo_no_fake_route,
               test_interests_landed, test_interest_matcher,
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
