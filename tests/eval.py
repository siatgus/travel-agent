"""评测集 —— 把"提示词写得好不好"变成可断言的行为。

为什么需要它：改一句提示词、调一个权重，输出就会变。没有回归测试，你只能靠肉眼
抽查，而且每次抽查的样本都不一样。这里的每条用例都断言**可观测行为**，不是断言
文案：是否追问、是否落在预算内、有没有折返、有没有闭馆冲突、有没有排进不该排的景点。

用法：
    python eval.py           # 跑全部用例
    python eval.py -v        # 附带每条断言的实际值
退出码：0 全通过，1 有失败（可直接挂到 CI）。
"""

from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent.agent import extract_slots, find_missing, plan_trip, self_check
from travel_agent.composer import MAX_HOP_KM


def evaluate(utterance: str) -> dict:
    slots = extract_slots(utterance)
    missing = find_missing(slots)
    ctx: dict = {"slots": slots, "missing": missing}
    if not missing:
        trace: list = []
        res = plan_trip(slots, trace)
        ctx["result"] = res
        ctx["checks"] = {c["item"]: c["result"] for c in self_check(res)}
        ctx["plan_text"] = " ".join(n["name"] for d in res["days_plan"] for n in d["nodes"])
        ctx["budget"] = res["budget"]
        ctx["violations"] = res["decision_log"]["violations"]
        ctx["cut_plans"] = res["cut_plans"]
    return ctx


def clarify(ctx: dict) -> bool:
    return bool(ctx["missing"])


def days_is(n: int):
    def _f(ctx: dict) -> bool:
        return ctx.get("slots", {}).get("days") == n
    return _f


def within_budget(ctx: dict) -> bool:
    b = ctx["budget"]
    return b.get("within_budget") is True


def over_budget_has_plans(ctx: dict) -> bool:
    b = ctx["budget"]
    if b.get("within_budget") is not False:
        return True
    return len(ctx["cut_plans"]) >= 2 and all(p["save"] > 0 for p in ctx["cut_plans"])


def no_hours_conflict(ctx: dict) -> bool:
    return not ctx["violations"]


def no_folding(ctx: dict) -> bool:
    from travel_agent.tools import haversine_km
    for d in ctx["result"]["days_plan"]:
        geos = [n["geo"] for n in d["nodes"]
                if n.get("geo") and n["type"] == "attraction"]
        for a, b in zip(geos, geos[1:]):
            if haversine_km(a, b) > MAX_HOP_KM:
                return False
    return True


def every_day_has_meal(ctx: dict) -> bool:
    plan = ctx["result"]["days_plan"]
    return all(any(n["type"] == "restaurant" for n in d["nodes"]) for d in plan[:-1])


def not_include(name: str):
    def _f(ctx: dict) -> bool:
        return name not in ctx["plan_text"]
    return _f


def include(name: str):
    def _f(ctx: dict) -> bool:
        return name in ctx["plan_text"]
    return _f


def each_day_max_attractions(n: int):
    """只数景点：用餐与留白不算"主要节点"，这是提示词里 3~4 个节点的本意。"""
    def _f(ctx: dict) -> bool:
        return all(sum(1 for x in d["nodes"] if x["type"] == "attraction") <= n
                   for d in ctx["result"]["days_plan"])
    return _f


CASES: list[dict] = [
    {
        "name": "完整需求直接出方案",
        "utterance": "我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶，我老婆不吃辣",
        "expect": [
            ("不追问", lambda c: not clarify(c)),
            ("天数=4", days_is(4)),
            ("每天有餐", every_day_has_meal),
            ("无折返", no_folding),
            ("无闭馆冲突", no_hours_conflict),
            ("轻松节奏单日景点≤3", each_day_max_attractions(3)),
        ],
    },
    {
        "name": "预算充足时不出取舍方案",
        "utterance": "11月8号从杭州去大理，玩3天，2个人，预算一人4000，节奏适中",
        "expect": [
            ("预算内", within_budget),
            ("不追问", lambda c: not clarify(c)),
        ],
    },
    {
        "name": "超预算必须给可执行取舍",
        "utterance": "我和老婆11月8号从杭州出发去大理，玩4天，预算总共4500，不想太赶，我老婆不吃辣",
        "expect": [
            ("识别超预算", lambda c: c["budget"].get("within_budget") is False),
            ("≥2 个降本方案且金额有效", over_budget_has_plans),
        ],
    },
    {
        "name": "缺天数应追问",
        "utterance": "下个月想去云南玩几天，帮我安排下",
        "expect": [("追问 days", lambda c: "days" in c["missing"])],
    },
    {
        "name": "缺目的地应追问",
        "utterance": "11月8号出发玩4天，2个人，预算6000",
        "expect": [("追问 destination", lambda c: "destination" in c["missing"])],
    },
    {
        "name": "忌高海拔不排索道",
        "utterance": "11月8号从杭州去大理玩4天，2人，预算7000，我恐高不能上高海拔",
        "expect": [
            ("未排洗马潭索道", not_include("洗马潭")),
            ("无闭馆冲突", no_hours_conflict),
        ],
    },
    {
        "name": "带3岁幼儿不排高强度长时景点",
        "utterance": "我和孩子（3岁）11月8号从杭州去大理玩4天，预算8000，不想太赶",
        "expect": [
            ("未排洗马潭索道", not_include("洗马潭")),
            ("每天有餐", every_day_has_meal),
        ],
    },
    {
        "name": "高强度偏好下仍保持地理合理",
        "utterance": "11月8号从杭州去大理玩5天，2人，预算9000，能多走就多走，特种兵",
        "expect": [
            ("无折返", no_folding),
            ("每天有餐", every_day_has_meal),
            ("单日景点≤5", each_day_max_attractions(5)),
        ],
    },
]


def rerank_boundary_checks() -> list[tuple[str, bool, str]]:
    """重排的作用边界——用纯函数断言，避免被行程顺延干扰。

    这三条锁住的是"模型能改变什么、不能改变什么"：
    同分时模型说了算；有真实质量差时规则说了算；用户明确点名时走 must_visit 而非重排。
    少了它们，后人把 rerank_bonus 调大一格就能悄悄推翻打分体系。
    """
    from travel_agent.composer import score_poi

    def poi(pid: str, appeal: int) -> dict:
        return {"poi_id": pid, "name": pid, "tags": [], "intensity": "low",
                "duration_min": 60, "cost": 0, "appeal": appeal, "geo": None}

    nirvana, ropeway, pagoda = poi("P2", 5), poi("P5", 5), poi("P6", 4)
    out: list[tuple[str, bool, str]] = []

    base_n, _ = score_poi(nirvana, {}, 0, None)
    base_r, _ = score_poi(ropeway, {}, 0, None)
    base_p, _ = score_poi(pagoda, {}, 0, None)

    # 同分（都是 10.0）：模型把头名给索道，索道应反超。
    # 名次按真实规模构造（top-5），否则跨度为 1 会让第 2 名直接归零，测不出真实衰减。
    rank = {"P5": 0, "P2": 1, "X1": 2, "X2": 3, "X3": 4}
    r_up, _ = score_poi(ropeway, {}, 0, None, pref_rank=rank)
    n_dn, _ = score_poi(nirvana, {}, 0, None, pref_rank=rank)
    out.append(("重排能在同分时裁决", r_up > n_dn, f"{r_up} vs {n_dn}"))

    # 差一个吸引力档位（10.0 vs 8.0）：三塔当头名也不该顶掉洱海
    rank2 = {"P6": 0, "P2": 1, "X1": 2, "X2": 3, "X3": 4}
    p_up, _ = score_poi(pagoda, {}, 0, None, pref_rank=rank2)
    n_fix, _ = score_poi(nirvana, {}, 0, None, pref_rank=rank2)
    out.append(("重排不能推翻一个档位的质量差", p_up < n_fix, f"{p_up} vs {n_fix}"))

    # 不在重排名单里的点不受影响
    out.append(("未参与重排的点不加不减",
                abs(base_n - score_poi(nirvana, {}, 0, None, pref_rank={"P5": 0})[0]) < 1e-9,
                f"base={base_n}"))

    # 必去点是"需求"不是"偏好"：18km 的喜洲也要排进去
    from travel_agent.composer import compose
    from travel_agent.agent import dispatch, extract_slots_rules
    from datetime import date
    slots = extract_slots_rules("11月8号从杭州去大理玩2天，2人，预算5000", date(2026, 10, 29))
    slots["must_visit"] = ["喜洲古镇"]
    plan, log = compose(
        slots, dispatch("search_poi", {"city": "大理", "category": "attraction"}),
        dispatch("get_weather", {"city": "大理", "start_date": "2026-11-08", "days": 2}),
        "11:30", outbound_depart_last="19:00")
    names = [n["name"] for d in plan for n in d["nodes"] if n["type"] == "attraction"]
    out.append(("用户点名的必去点即使超半径也排入",
                any("喜洲" in x for x in names), f"排点={names}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    total = passed = 0
    failures: list[str] = []

    for case in CASES:
        ctx = evaluate(case["utterance"])
        marks = []
        case_ok = True
        for label, fn in case["expect"]:
            total += 1
            ok = bool(fn(ctx))
            passed += ok
            marks.append(("PASS" if ok else "FAIL", label))
            if not ok:
                case_ok = False
                failures.append(f"{case['name']} → {label}")
        flag = "PASS" if case_ok else "FAIL"
        print(f"[{flag}] {case['name']}")
        if args.verbose or not case_ok:
            for status, label in marks:
                print(f"       {status}  {label}")
            if not case_ok and "result" in ctx:
                b = ctx["budget"]
                print(f"       实际：预算 {b.get('total')}/{b.get('target')}，"
                      f"节点 {[len(d['nodes']) for d in ctx['result']['days_plan']]}，"
                      f"冲突 {len(ctx['violations'])} 条")
                print(f"       排点：{ctx['plan_text']}")

    print("\n[重排作用边界]")
    boundary = rerank_boundary_checks()
    for label, ok, detail in boundary:
        total += 1
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}"
              + (f"　{detail}" if args.verbose or not ok else ""))
        if not ok:
            failures.append(f"重排边界 → {label}")

    print(f"\n{passed}/{total} 条断言通过，{len(CASES)} 个用例 + 4 条重排边界")
    if failures:
        print("失败项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
