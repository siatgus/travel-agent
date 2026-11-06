"""混合编排器 —— 规则生成候选、评分择优、几何校验。

设计动机：纯规则编排只会"按区域顺序取前 N 个"，无法解释"为什么去这个不去那个"；
纯 LLM 编排则会排出地理上不可行的路线（上午城东、下午城西）。
这里把职责切开：

    候选生成（规则：可玩时长、开放日、天气可用性）
        ↓
    打分排序（可解释的加权分数，权重在 WEIGHTS 里可直接调）
        ↓
    贪心成团（距离约束：相邻节点直线距离 ≤ max_hop，超了换天）
        ↓
    时间轴装配（真实距离 → 交通方式与耗时 → 冲突自动后移）
        ↓
    LLM 重排（可选钩子：在 top-k 内按用户偏好微调，见 rerank_hook）

所有决策都返回 reasons 字段，便于在 demo 里 --explain 打印，也便于评测断言。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from .tools import haversine_km, transit_estimate

WEIGHTS = {
    "appeal": 2.0,          # 景点本身吸引力 1-5
    "must_visit": 4.0,      # 用户点名必去
    "interest_match": 1.5,  # 与软偏好关键词重合
    "intensity_penalty": -2.5,
    "high_altitude_penalty": -6.0,
    "rain_penalty": -3.5,
    "far_penalty": -0.15,   # 每公里
    # LLM 重排的名次加成上限，决定"模型能翻转多大的分差"。
    # 实测标定：3.0 能翻转同分与接近分（索道与洱海同为 10 分时，模型头名生效），
    # 但翻不过 1 个吸引力档位（2.0 分）——这是故意的：分差代表真实质量/成本差异，
    # 模型的偏好猜测不该推翻它。用户"一定要去某地"走 must_visit（必进），不是走这里。
    "rerank_bonus": 3.0,
}

PACE_CAP = {"relaxed": 2, "moderate": 3, "intensive": 4}
MAX_HOP_KM = 15.0          # 与 prompts/04 子提示词 B 规则 2 对齐
DAY_MOVE_BUDGET_MIN = 150  # 单日市内移动上限 2.5h
# 景点最晚结束时刻。没有这条约束时会排出"21:05 逛完古城、21:28 吃晚饭"的行程——
# 营业时间上它合法（古城全天开放），但真实体验不成立。
DAY_END_LATEST = 20 * 60


def name_hit(fragment: str, poi_name: str) -> bool:
    """用户点名的景点与 POI 名是否指同一个地方：双向包含。

    只做单向子串会漏掉两种常见说法：用户说「上海迪士尼」而库里叫
    「上海迪士尼乐园」（fragment in name，本来就能中）；用户照抄全名
    「上海迪士尼乐园」而库里叫「迪士尼乐园」（name in fragment，单向会漏）。
    「不想去豫园」这类排除若漏匹配， unwanted 景点照进行程，比漏掉必去更招投诉。
    """
    f, n = (fragment or "").strip(), (poi_name or "").strip()
    return len(f) >= 2 and len(n) >= 2 and (f in n or n in f)


def score_poi(poi: dict, slots: dict, rain_prob: int, last_geo: list[float] | None,
              weights: dict | None = None,
              pref_rank: dict[str, int] | None = None) -> tuple[float, list[str]]:
    """给单个 POI 打分，同时返回人类可读的理由。

    pref_rank：LLM 重排得到的 poi_id → 名次。只作为小幅加成参与打分，
    不改变 veto（硬约束）和几何约束——模型再喜欢一个点，也进不了关门的景区。
    """
    w = weights or WEIGHTS
    s = poi.get("appeal", 3) * w["appeal"]
    reasons = [f"吸引力 {poi.get('appeal', 3)}"]

    must = [m for m in slots.get("must_visit") or [] if name_hit(m, poi["name"])]
    if must:
        s += w["must_visit"]
        reasons.append("用户点名必去")

    prefs = " ".join(slots.get("soft_prefs") or [])
    hits = [t for t in poi.get("tags", []) if t in prefs]
    if hits:
        s += w["interest_match"] * len(hits)
        reasons.append(f"命中偏好 {'/'.join(hits)}")

    if slots.get("pace") == "relaxed" and poi["intensity"] == "high":
        s += w["intensity_penalty"]
        reasons.append("偏累，与轻松节奏冲突")
    if slots.get("pace") == "intensive" and poi["intensity"] == "low":
        s += w["intensity_penalty"] / 2
        reasons.append("强度偏低")

    if "忌高海拔" in (slots.get("hard_prefs") or []) and "高海拔" in poi.get("tags", []):
        s += w["high_altitude_penalty"]
        reasons.append("用户忌高海拔")

    if "带幼儿" in (slots.get("hard_prefs") or []) and (
            poi["intensity"] == "high" or poi["duration_min"] >= 240):
        s += w["high_altitude_penalty"]
        reasons.append("带 3 岁以下幼儿，强度/时长不适")

    rain_prob = rain_prob or 0
    if rain_prob > 60 and poi["intensity"] != "low":
        s += w["rain_penalty"]
        reasons.append(f"降雨概率 {rain_prob}%")

    if last_geo and poi.get("geo"):
        km = haversine_km(last_geo, poi["geo"])
        s += w["far_penalty"] * km
        if km > MAX_HOP_KM:
            s -= 5
            reasons.append(f"距上一站 {km}km，超出单日半径")

    rank = (pref_rank or {}).get(poi.get("poi_id"))
    if rank is not None:
        # 加成随名次平方衰减：第 1 名拿满额，第 2 名 56%（span=4），末位 0。
        # 早期用线性衰减时，第 3 名还能拿 50%，导致头名的微弱优势被抹平，
        # 重排永远翻不动 1 个吸引力档位（2.0 分）的差距。
        span = max(len(pref_rank or {}) - 1, 1)
        decay = max(0.0, 1 - rank / span) ** 2
        s += w["rerank_bonus"] * decay
        reasons.append(f"偏好重排第 {rank + 1} 位")

    return round(s, 2), reasons


def _must_start(poi: dict) -> int:
    """必须开始的最晚时刻（分钟）。闭馆早且耗时长者更紧迫。"""
    hours = parse_hours(poi.get("open_hours", ""))
    return hours[1] - poi["duration_min"] if hours else 24 * 60


def veto(poi: dict, slots: dict) -> str | None:
    """硬约束否决：不参与打分，直接不进候选。返回否决理由，None 表示通过。"""
    hp = slots.get("hard_prefs") or []
    if "忌高海拔" in hp and "高海拔" in poi.get("tags", []):
        return "用户忌高海拔"
    if "带幼儿" in hp and poi["intensity"] == "high":
        return "带 3 岁以下幼儿，不宜高强度"
    # 用户明确排除的景点是最高优先级硬约束——"我不想豫园"之后豫园还出现在
    # 行程里，用户只会认为系统没在听。排除比必去更优先：两者冲突时不去。
    av = slots.get("avoid") or []
    if any(name_hit(a, poi["name"]) for a in av if a):
        return "用户明确不想去"
    return None


def _open_on(poi: dict, d: date) -> bool:
    """闭馆日判断：博物馆类默认周一闭馆。"""
    if "博物馆" in poi["name"] or "博物院" in poi["name"]:
        return d.weekday() != 0
    return True


def compose(slots: dict, pois_res: dict, weather_res: dict,
            outbound_arrive: str, outbound_depart_last: str = "18:00",
            hotel_geo: list[float] | None = None,
            explain: bool = False,
            reranker: Callable[[list[dict], dict], list[str] | None] | None = None,
            ) -> tuple[list[dict], dict]:
    """outbound_depart_last：最后一天返程班次发车时刻，决定当天上午能否排景点。

    reranker：可选的 LLM 重排函数，只在候选集内给出偏好次序。
    返回 None 表示重排失败，编排自动沿用规则排序——不抛异常、不空转。
    """
    all_pois = pois_res.get("items") or []
    attractions = [p for p in all_pois if p["category"] == "attraction"]
    foods = [p for p in all_pois if p["category"] == "restaurant"]
    days = slots["days"]
    cap = PACE_CAP[slots["pace"]]
    start = date.fromisoformat(slots["date_range"]["start"])
    home = hotel_geo or [25.693, 100.165]
    log: dict = {"candidates": [], "days": [], "violations": []}

    remaining = [p for p in attractions if p.get("geo")]
    pref_rank = build_pref_rank(remaining, slots, reranker, log)
    plan: list[dict] = []

    for i in range(days):
        d = start + timedelta(days=i)
        w = (weather_res.get("days") or [{}])[min(i, len(weather_res.get("days", [])) - 1)]
        # 天气缺数据时 rain_prob 是 None（超出预报窗口又无历史值时），按 0 算
        rain = w.get("rain_prob") or 0
        nodes: list[dict] = []

        # 抵达日：容量由到站时刻决定；离开日：不留新景点
        start_min = 9 * 60
        if i == 0:
            hh, mm = (int(x) for x in outbound_arrive.split(":"))
            arrive = hh * 60 + mm
            nodes.append(_node(outbound_arrive, "抵达 + 入住", "transit", 90,
                               "车站/机场打车", "先放行李再出门，老城住宿多在巷子里，导航到巷口下车"))
            start_min = arrive + 90
            day_cap = 1 if arrive < 15 * 60 else 0
        elif i == days - 1:
            nodes.append(_node("09:00", "早市 / 伴手礼", "attraction", 60, "步行",
                               "当地特产在早市买更便宜，景区包装款溢价高"))
            hh, mm = (int(x) for x in outbound_depart_last.split(":"))
            day_cap = 1 if hh >= 15 else 0
            start_min = 10 * 60
        else:
            day_cap = cap

        # 选点：先取最高分做锚点，再按距离就近成团
        picked = _pick_chain(remaining, slots, rain, home, day_cap, d, log, pref_rank)
        # 紧迫度高（闭馆早、耗时长）的先占时段，同等紧迫度保持原有成团顺序
        picked = sorted(picked, key=lambda x: (_must_start(x), picked.index(x)))

        # 时间轴装配
        clock = start_min if i == 0 else 9 * 60
        prev_geo = home
        lunch_done = False
        used_food: set[str] = set()
        move_total = 0
        dropped: list[dict] = []
        used_pois: list[dict] = []
        for p in picked:
            km = haversine_km(prev_geo, p["geo"])
            mode, tmin = transit_estimate(km, prev_geo, p["geo"])
            move_total += tmin
            clock += tmin
            rainy_swap = rain > 60 and p["intensity"] != "low" and p.get("indoor_alternative")
            name = p["indoor_alternative"] if rainy_swap else p["name"]
            tip = (f"原计划 {p['name']}，降雨概率 {rain}% 已调整" if rainy_swap
                   else (f"需提前 1 天预约；{p['open_hours']} 开放" if p["need_booking"]
                         else f"{p['open_hours']} 开放"))

            # 进景点前先看会不会跨过饭点：11:26 开始逛 150 分钟，等于 14:00 才吃午饭。
            # 只在已经接近饭点（11:00 后）时才插，避免一早上就吃午饭。
            if (not lunch_done and i not in (0, days - 1) and clock >= 11 * 60
                    and clock + p["duration_min"] > 12 * 60 + 30):
                pool = [f for f in foods if f.get("poi_id") not in used_food]
                lunch = (_nearest(pool, "午餐", prev_geo, max_km=3.0)
                         or _nearest(pool, "晚餐", prev_geo, max_km=6.0))
                if lunch:
                    lkm = haversine_km(prev_geo, lunch.get("geo") or prev_geo)
                    lmode, lmin = transit_estimate(lkm, prev_geo, lunch.get("geo") or prev_geo)
                    clock += lmin
                    nodes.append(_node(fmt(clock), lunch["name"], "restaurant",
                                       lunch["duration_min"],
                                       f"{lmode} {lmin}分钟（{lkm}km）",
                                       "先吃饭再逛，避免拖到下午两点", lunch["cost"],
                                       poi_id=lunch["poi_id"], geo=lunch["geo"]))
                    used_food.add(lunch["poi_id"])
                    clock += lunch["duration_min"]
                lunch_done = True
                # 吃完再算一次到景点的路程
                km = haversine_km(prev_geo, p["geo"])
                mode, tmin = transit_estimate(km, prev_geo, p["geo"])
                move_total += tmin
                clock += tmin
            # 营业时间校验：到达 + 时长是否越过闭馆
            hours = parse_hours(p.get("open_hours", ""))
            if hours and clock + p["duration_min"] > hours[1]:
                # 当天确实排不进去 → 顺延到次日，而不是硬塞并标警告
                dropped.append({"day": i + 1, "poi": p["name"],
                                "why": f"按当前动线 {fmt(clock)} 到达，无法在 {fmt(hours[1])} 闭馆前完成"})
                clock -= tmin + (0 if not nodes else 0)
                move_total -= tmin
                continue
            if clock + p["duration_min"] > DAY_END_LATEST:
                # 营业时间允许、但会把一天拖到深夜 → 同样顺延
                dropped.append({"day": i + 1, "poi": p["name"],
                                "why": f"{fmt(clock)} 才开始，结束将晚于 {fmt(DAY_END_LATEST)}，"
                                       f"移到次日更合理"})
                clock -= tmin
                move_total -= tmin
                continue
            if hours and clock < hours[0]:
                tip += f"　离开门 {fmt(hours[0])} 还有 {hours[0] - clock} 分钟，可先逛周边"
            nodes.append(_node(fmt(clock), name, "attraction", p["duration_min"],
                               f"{mode} {tmin}分钟（{km}km）", tip,
                               0 if rainy_swap else p["cost"],
                               booking_required=p["need_booking"] and not rainy_swap,
                               poi_id=p["poi_id"], geo=p["geo"]))
            clock += p["duration_min"]
            prev_geo = p["geo"]
            used_pois.append(p)

            # 午餐：本段结束后已过 12:00 且当天还没吃，就近补一顿
            if (not lunch_done and i != days - 1 and clock >= 12 * 60
                    and clock <= 14 * 60 + 30):
                pool = [f for f in foods if f.get("poi_id") not in used_food]
                lunch = (_nearest(pool, "午餐", prev_geo, max_km=3.0)
                         or _nearest(pool, "晚餐", prev_geo, max_km=6.0))
                if lunch:
                    lkm = haversine_km(prev_geo, lunch.get("geo") or prev_geo)
                    lmode, lmin = transit_estimate(lkm, prev_geo, lunch.get("geo") or prev_geo)
                    clock += lmin
                    nodes.append(_node(fmt(clock), lunch["name"], "restaurant",
                                       lunch["duration_min"],
                                       f"{lmode} {lmin}分钟（{lkm}km）",
                                       "当地小吃，不辣版本要主动说", lunch["cost"],
                                       poi_id=lunch["poi_id"], geo=lunch["geo"]))
                    used_food.add(lunch["poi_id"])
                    clock += lunch["duration_min"]
                lunch_done = True

        # 兜底午餐：景点排完仍没吃上（如上午只有一个景点），在自由活动前就近补一顿
        if not lunch_done and i not in (0, days - 1) and used_pois and foods:
            clock = max(clock, 11 * 60 + 30)
            pool = [f for f in foods if f.get("poi_id") not in used_food]
            lunch = (_nearest(pool, "午餐", prev_geo, max_km=6.0)
                     or _nearest(pool, "晚餐", prev_geo, max_km=6.0))
            if lunch:
                lkm = haversine_km(prev_geo, lunch.get("geo") or prev_geo)
                lmode, lmin = transit_estimate(lkm, prev_geo, lunch.get("geo") or prev_geo)
                clock += lmin
                nodes.append(_node(fmt(clock), lunch["name"], "restaurant",
                                   lunch["duration_min"], f"{lmode} {lmin}分钟（{lkm}km）",
                                   "当天唯一一顿坐下来吃的，不用赶", lunch["cost"],
                                   poi_id=lunch["poi_id"], geo=lunch["geo"]))
                used_food.add(lunch["poi_id"])
                clock += lunch["duration_min"]
            lunch_done = True

        # 晚餐：定位到当日最后一个点位附近
        remaining = [x for x in remaining if x not in used_pois]

        if i < days - 1 and foods:
            pool = [f for f in foods if f.get("poi_id") not in used_food]
            dinner = (_nearest(pool, "晚餐", prev_geo, max_km=6.0)
                      or _nearest(pool, "晚餐", home, max_km=6.0))
            if dinner:
                dkm = haversine_km(prev_geo, dinner.get("geo") or prev_geo)
                dmode, dmin = transit_estimate(dkm, prev_geo, dinner.get("geo") or prev_geo)
                dinner_start = max(clock + dmin + 20, 18 * 60)
                if dinner_start - clock > 120:
                    nodes.append(_node(fmt(clock + 30), "自由活动 / 回客栈休息", "rest",
                                       dinner_start - clock - 30, "—",
                                       "不强塞景点：这一段留白，累了就早回"))
                nodes.append(_node(fmt(dinner_start), dinner["name"],
                                   "restaurant", dinner["duration_min"],
                                   f"{dmode} {dmin}分钟（{dkm}km）",
                                   "回住宿方向顺路吃，点单说明不要辣不要蘸水", dinner["cost"],
                                   poi_id=dinner["poi_id"], geo=dinner["geo"]))

        nodes.sort(key=lambda x: x["time"])
        cost = sum(n["cost"] for n in nodes if n["type"] == "attraction")
        plan.append({
            "day": i + 1,
            "date": d.isoformat(),
            "theme": ["抵达适应", "核心景区", "人文深度", "返程收尾"][min(i, 3)],
            "intensity": "high" if cost >= 200 else ("low" if cost == 0 else "medium"),
            "nodes": nodes,
            "move_min": move_total,
            "day_budget": cost * slots["party_size"] + 150 * slots["party_size"],
            "rainy_plan": "已按降雨概率调整" if rain > 60 else "户外优先",
        })
        if dropped:
            log.setdefault("deferred", []).extend(dropped)
        log["days"].append({"day": i + 1, "move_min": move_total,
                            "picked": [p["name"] for p in used_pois]})

    # 顺延后仍未排入的点，才是真正的冲突（而非已修复）
    deferred_names = {d["poi"] for d in log.get("deferred", [])}
    for p in remaining:
        if p["name"] in deferred_names:
            log["violations"].append({"day": 0, "poi": p["name"],
                                      "why": "全程未能排入，受闭馆时间与动线限制"})
    return plan, log


def _pick_chain(remaining: list[dict], slots: dict, rain: int, home: list[float],
                quota: int, d: date, log: dict,
                pref_rank: dict[str, int] | None = None) -> list[dict]:
    """锚点 + 就近成团，受单日移动时长上限约束。"""
    if quota <= 0:
        return []
    pool, blocked = [], []
    for p in remaining:
        if not _open_on(p, d):
            continue
        why = veto(p, slots)
        if why:
            blocked.append({"name": p["name"], "why": why})
            continue
        pool.append(p)
    if blocked and log is not None:
        log.setdefault("vetoed", []).extend(blocked)
    if not pool:
        return []

    scored = []
    for p in pool:
        s, why = score_poi(p, slots, rain, home, pref_rank=pref_rank)
        scored.append((s, p, why))
        if log is not None:
            log["candidates"].append({"name": p["name"], "score": s, "reasons": why})
    scored.sort(key=lambda x: -x[0])

    # 用户点名的必去点先占位——这是"需求"，不是"偏好"。
    # 只靠 +4.0 加分会被距离成本压掉（远处景点 -5 半径惩罚 + 线性距离惩罚），
    # 结果就是用户说"一定要去喜洲"，系统却因为 18km 而换了个近的。
    must_names = [m for m in (slots.get("must_visit") or []) if m]
    must = [p for p in pool if any(name_hit(m, p["name"]) for m in must_names)]
    if must and log is not None:
        log.setdefault("must_visit", []).extend(
            [{"name": p["name"], "day": d.isoformat()} for p in must])

    picked = list(must)
    if not picked:
        # 起手点 = 分数最高者（含 LLM 偏好加成）。
        # 闭馆紧迫度只在后面决定"谁先排"，不参与"选谁"——否则用户明说想去的点
        # 会被一个"关门早"的点顶掉，偏好就失效了。
        picked = [scored[0][1]] if scored else []

    move, prev = 0, picked[0]["geo"]
    for p in picked[1:]:
        move += transit_estimate(haversine_km(prev, p["geo"]), prev, p["geo"])[1]
        prev = p["geo"]
    while len(picked) < max(quota, len(must)):
        cand = []
        for s, p, why in scored:
            if p in picked:
                continue
            km = haversine_km(prev, p["geo"])
            if km > MAX_HOP_KM:
                continue
            if move + transit_estimate(km, prev, p["geo"])[1] > DAY_MOVE_BUDGET_MIN:
                continue
            cand.append((s - km * 0.4, p, km))
        if not cand:
            break
        cand.sort(key=lambda x: -x[0])
        _, nxt, km = cand[0]
        picked.append(nxt)
        move += transit_estimate(km, prev, nxt["geo"])[1]
        prev = nxt["geo"]
    return picked


def _nearest(foods: list[dict], label: str, geo: list[float] | None,
             max_km: float | None = None) -> dict | None:
    cands = [f for f in foods if label in f.get("tags", [])] or foods
    if not cands:
        return None
    if not geo:
        return cands[0]
    best = min(cands, key=lambda f: haversine_km(geo, f.get("geo") or geo))
    if max_km is not None and haversine_km(geo, best.get("geo") or geo) > max_km:
        return None
    return best


def parse_hours(spec: str) -> tuple[int, int] | None:
    """解析 "08:30-16:00" → (510, 960)；"全天"/空值返回 None。"""
    if not spec or "-" not in spec:
        return None
    try:
        a, b = spec.split("-")
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except ValueError:
        return None


def _node(time_s: str, name: str, type_: str, dur: int, transit: str, tip: str,
          cost: float = 0, booking_required: bool = False,
          poi_id: str | None = None, geo: list[float] | None = None) -> dict:
    return {"time": time_s, "name": name, "type": type_, "duration_min": dur,
            "transit": transit, "tip": tip, "cost": cost,
            "booking_required": booking_required,
            "poi_id": poi_id, "geo": geo}


def fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build_pref_rank(candidates: list[dict], slots: dict,
                    reranker: Callable[[list[dict], dict], list[str] | None] | None,
                    log: dict) -> dict[str, int]:
    """把 top-k 候选交给 reranker，得到 poi_id → 名次，作为打分的微调项。

    只调一次而不是逐日重排：逐日会因天气变化反复请求，成本高且排序不稳定。
    reranker 返回 None（模型不可用/校验失败）时返回空字典，编排完全按规则走。
    """
    if reranker is None or len(candidates) < 2:
        return {}

    pool = []
    for p in candidates:
        if veto(p, slots):
            continue
        s, _ = score_poi(p, slots, 0, None)
        pool.append({**p, "_score": s})
    if len(pool) < 2:
        return {}

    pool.sort(key=lambda x: -x["_score"])
    top = pool[:5]
    allowed = {p["poi_id"] for p in top}

    try:
        order = reranker(top, slots)
    except Exception as e:                      # 钩子内部异常绝不能打断编排
        log["rerank"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {}

    if not order:
        log["rerank"] = {"ok": False, "error": "重排未返回有效次序，沿用规则排序"}
        return {}

    # 白名单二次兜底：即使 reranker 没做校验，这里也不接受候选集外的 id
    order = [pid for pid in order if pid in allowed]
    if not order:
        log["rerank"] = {"ok": False, "error": "重排结果全部非法，沿用规则排序"}
        return {}

    log["rerank"] = {"ok": True, "order": order,
                     "names": [p["name"] for p in top if p["poi_id"] in order]}
    return {pid: i for i, pid in enumerate(order)}
