"""Mock 工具集 —— 与 prompts/02-tool-schemas.json 一一对应。

设计目的：在没有真实 API Key 的情况下，验证 agent 的工具调用流程、参数传递、
错误降级与预算核算逻辑。数据结构及字段命名与真实接口保持一致，后续替换
只需改 _query_* 内部实现，agent 侧代码零改动。

返回统一遵循 error_contract：
    成功 -> {"ok": true, ...}
    失败 -> {"ok": false, "error": {"code": ..., "message": ..., "retryable": ...}}
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Callable

# ---------------------------------------------------------------- 基础数据

CITY_DB: dict[str, dict[str, Any]] = {
    "大理": {
        "climate": {
            11: {"low": 8, "high": 21, "rain_prob": 15, "desc": "晴到多云"},
            10: {"low": 12, "high": 23, "rain_prob": 25, "desc": "多云"},
        },
        "transport_hubs": {"train": "大理站", "flight": "大理机场"},
        "areas": ["古城南门", "古城北门", "才村", "下关"],
    },
    "丽江": {
        "climate": {
            11: {"low": 3, "high": 18, "rain_prob": 10, "desc": "晴"},
        },
        "transport_hubs": {"train": "丽江站", "flight": "三义机场"},
        "areas": ["大研古城", "束河", "白沙"],
    },
    "杭州": {
        "climate": {11: {"low": 12, "high": 20, "rain_prob": 30, "desc": "多云转阴"}},
        "transport_hubs": {"train": "杭州东", "flight": "萧山机场"},
        "areas": ["西湖", "滨江"],
    },
}

ROUTES_DB: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("杭州", "大理"): [
        {"mode": "train", "code": "G1373", "depart": "07:20", "arrive": "17:50",
         "duration_min": 630, "price": 430, "note": "需在昆明南换乘"},
        {"mode": "flight", "code": "MU5961", "depart": "08:10", "arrive": "11:55",
         "duration_min": 225, "price": 1180, "note": "直飞，含 20kg 托运"},
    ],
    ("大理", "杭州"): [
        {"mode": "train", "code": "G1374", "depart": "11:30", "arrive": "22:10",
         "duration_min": 640, "price": 430, "note": "需在昆明南换乘"},
        {"mode": "flight", "code": "MU5962", "depart": "19:20", "arrive": "22:40",
         "duration_min": 200, "price": 1060, "note": "直飞"},
    ],
    ("丽江", "香格里拉"): [
        {"mode": "coach", "code": "大巴", "depart": "08:00", "arrive": "11:40",
         "duration_min": 220, "price": 78, "note": "经停虎跳峡"},
    ],
}

HOTELS_DB: dict[str, list[dict[str, Any]]] = {
    "大理": [
        {"hotel_id": "DALI-001", "name": "南门里·院景客栈", "area": "古城南门",
         "star": 3, "price": 380, "geo": [25.6925, 100.1648], "cancel_free": True, "dist_to_core_km": 0.3},
        {"hotel_id": "DALI-002", "name": "洱海云端海景酒店", "area": "才村",
         "star": 4, "price": 620, "geo": [25.7062, 100.1828], "cancel_free": True, "dist_to_core_km": 4.2},
        {"hotel_id": "DALI-003", "name": "北门青瓦小院", "area": "古城北门",
         "star": 2, "price": 260, "geo": [25.6998, 100.1660], "cancel_free": False, "dist_to_core_km": 0.9},
    ],
    "丽江": [
        {"hotel_id": "LJG-001", "name": "束河听水客栈", "area": "束河",
         "star": 3, "price": 320, "geo": [26.9180, 100.2160], "cancel_free": True, "dist_to_core_km": 1.1},
    ],
}

POIS_DB: dict[str, list[dict[str, Any]]] = {
    "大理": [
        {"poi_id": "P1", "name": "大理古城人民路", "category": "attraction",
         "area": "古城南门", "geo": [25.6930, 100.1650], "appeal": 4,
         "duration_min": 150, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["古城", "夜市", "步行"], "intensity": "low",
         "indoor_alternative": "大理市博物馆"},
        {"poi_id": "P2", "name": "洱海生态廊道（才村段）", "category": "attraction",
         "area": "才村", "geo": [25.7060, 100.1830], "appeal": 5,
         "duration_min": 180, "cost": 60,
         "open_hours": "07:00-19:00", "open_days": "每天", "need_booking": False,
         "tags": ["洱海", "骑行", "户外"], "intensity": "medium",
         "indoor_alternative": "周城扎染体验"},
        {"poi_id": "P3", "name": "喜洲古镇", "category": "attraction",
         "area": "喜洲", "geo": [25.8540, 100.1330], "appeal": 4,
         "duration_min": 120, "cost": 0,
         "open_hours": "08:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["古镇", "白族", "洱海"], "intensity": "low",
         "indoor_alternative": "严家大院"},
        {"poi_id": "P4", "name": "海舌公园", "category": "attraction",
         "area": "喜洲", "geo": [25.8690, 100.1300], "appeal": 3,
         "duration_min": 60, "cost": 0,
         "open_hours": "09:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["洱海", "摄影", "户外"], "intensity": "low",
         "indoor_alternative": "喜洲早市"},
        {"poi_id": "P5", "name": "苍山洗马潭索道", "category": "attraction",
         "area": "苍山", "geo": [25.6720, 100.1230], "appeal": 5,
         "duration_min": 240, "cost": 280,
         "open_hours": "08:30-16:00", "open_days": "每天", "need_booking": True,
         "tags": ["苍山", "高海拔", "户外"], "intensity": "high",
         "indoor_alternative": "寂照庵半日"},
        {"poi_id": "P6", "name": "崇圣寺三塔", "category": "attraction",
         "area": "古城北", "geo": [25.7050, 100.1480], "appeal": 4,
         "duration_min": 120, "cost": 75,
         "open_hours": "08:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["古建", "人文"], "intensity": "low",
         "indoor_alternative": "三塔文物陈列馆"},
        {"poi_id": "P7", "name": "石井私房菜", "category": "restaurant",
         "area": "古城南门", "geo": [25.6920, 100.1660], "appeal": 4,
         "duration_min": 80, "cost": 80,
         "open_hours": "11:00-21:30", "open_days": "每天", "need_booking": False,
         "tags": ["本地菜", "可做不辣", "晚餐"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P8", "name": "梅子井酒家", "category": "restaurant",
         "area": "古城南门", "geo": [25.6960, 100.1630], "appeal": 4,
         "duration_min": 80, "cost": 70,
         "open_hours": "11:00-21:00", "open_days": "每天", "need_booking": False,
         "tags": ["白族菜", "可做不辣", "晚餐"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P9", "name": "喜洲破酥粑粑", "category": "restaurant",
         "area": "喜洲", "geo": [25.8535, 100.1340], "appeal": 3,
         "duration_min": 40, "cost": 30,
         "open_hours": "08:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["小吃", "午餐", "不辣"], "intensity": "low",
         "indoor_alternative": ""},
    ],
    "丽江": [
        {"poi_id": "L1", "name": "大研古城", "category": "attraction", "area": "大研古城",
         "geo": [26.8720, 100.2340], "appeal": 4,
         "duration_min": 180, "cost": 50, "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["古城"], "intensity": "low",
         "indoor_alternative": "木府"},
    ],
}

# ---------------------------------------------------------------- 工具实现


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def get_weather(city: str, start_date: str, end_date: str) -> dict[str, Any]:
    city_data = CITY_DB.get(city)
    if not city_data:
        return {"ok": False, "error": {"code": "CITY_NOT_FOUND",
                "message": f"暂不支持城市：{city}", "retryable": False}}
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return {"ok": False, "error": {"code": "BAD_DATE",
                "message": "日期格式需为 YYYY-MM-DD", "retryable": False}}
    if end < start:
        return {"ok": False, "error": {"code": "BAD_RANGE",
                "message": "结束日期早于开始日期", "retryable": False}}

    days = []
    cur = start
    while cur <= end:
        clim = city_data["climate"].get(cur.month)
        if not clim:
            clim = {"low": 10, "high": 20, "rain_prob": 20, "desc": "暂无数据"}
        # 与真实数据源保持一致：逐日判断，窗口是今天 +15 天
        is_forecast = (cur - date.today()).days <= 15
        days.append({
            "date": cur.isoformat(),
            "low": clim["low"], "high": clim["high"],
            "rain_prob": clim["rain_prob"], "desc": clim["desc"],
            "is_forecast": is_forecast,
        })
        cur += timedelta(days=1)
    return _ok(city=city, days=days,
               note="超出 16 天窗口的为历史同期均值"
               if not all(d["is_forecast"] for d in days) else "预报数据")


def search_transport(from_city: str, to_city: str, date: str,
                     transport_type: str = "any", party_size: int = 1) -> dict[str, Any]:
    options = ROUTES_DB.get((from_city, to_city))
    if not options:
        return {"ok": False, "error": {"code": "NO_ROUTE",
                "message": f"暂无 {from_city} → {to_city} 的直达班次数据",
                "retryable": True}}
    if transport_type != "any":
        options = [o for o in options if o["mode"] == transport_type]
    items = [{**o, "total_price": o["price"] * party_size} for o in options]
    return _ok(from_city=from_city, to_city=to_city, date=date, items=items)


def search_hotel(city: str, check_in: str, check_out: str, guests: int,
                 rooms: int = 1, max_price_per_night: float | None = None,
                 preferred_area: str | None = None, star_level: int | None = None) -> dict[str, Any]:
    hotels = HOTELS_DB.get(city, [])
    if not hotels:
        return {"ok": False, "error": {"code": "NO_HOTEL",
                "message": f"{city} 暂无酒店数据", "retryable": True}}
    result = hotels
    if max_price_per_night:
        result = [h for h in result if h["price"] <= max_price_per_night]
    if star_level:
        result = [h for h in result if h["star"] >= star_level]
    if preferred_area:
        exact = [h for h in result if preferred_area in h["area"]]
        result = exact or result
    if not result:
        return _ok(items=[], note="条件过严，无匹配结果，建议放宽价格或星级")
    result = sorted(result, key=lambda h: (not h["cancel_free"], h["dist_to_core_km"]))
    return _ok(items=[{**h, "rooms": rooms, "guests": guests} for h in result])


def search_poi(city: str, keyword: str | None = None, category: str = "any",
               limit: int = 8, near: str | None = None) -> dict[str, Any]:
    pois = POIS_DB.get(city, [])
    if not pois:
        return {"ok": False, "error": {"code": "NO_POI",
                "message": f"{city} 暂无 POI 数据", "retryable": True}}
    result = pois
    if category != "any":
        result = [p for p in result if p["category"] == category]
    if keyword:
        result = [p for p in result
                  if keyword in p["name"] or keyword in p["tags"]
                  or any(keyword in t for t in p["tags"])]
    if near:
        result = sorted(result, key=lambda p: 0 if near in p["area"] else 1)
    return _ok(items=result[:limit], total=len(result))


def calc_budget(items: list[dict[str, Any]], party_size: int,
                budget_target: float | None = None,
                contingency_rate: float = 0.1) -> dict[str, Any]:
    base = sum(float(i["amount"]) for i in items)
    contingency = round(base * contingency_rate)
    total = base + contingency
    per_person = round(total / party_size, 1) if party_size else 0
    out: dict[str, Any] = {
        "ok": True,
        "breakdown": items,
        "subtotal": base,
        "contingency": contingency,
        "total": total,
        "per_person": per_person,
    }
    if budget_target:
        out["target"] = budget_target
        out["delta"] = total - budget_target
        out["within_budget"] = total <= budget_target
    return out


def save_preference(key: str, value: str, confidence: float = 0.9,
                    source: str = "user_explicit") -> dict[str, Any]:
    MEMORY_STORE[key] = {"value": value, "confidence": confidence, "source": source}
    return _ok(saved=True, key=key, value=value)


MEMORY_STORE: dict[str, dict[str, Any]] = {}


def haversine_km(a: list[float], b: list[float]) -> float:
    """两点直线距离（公里）。用于折返检测与动线聚类。"""
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1, lat2, lon2 = map(radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return round(2 * 6371 * asin(sqrt(h)), 2)


def transit_estimate(km: float, a: list[float] | None = None,
                     b: list[float] | None = None) -> tuple[str, int]:
    """按距离估算交通方式与耗时（分钟）。市内平均车速取 25km/h，含等车 8 分钟。

    a/b 参数仅 real_tools 使用（OSRM 真实路网）；mock 版忽略，保持签名兼容。
    """
    if km <= 0.8:
        return "步行", int(km / 4.5 * 60) + 1
    if km <= 3:
        return "打车", int(km / 25 * 60) + 8
    return "打车", int(km / 30 * 60) + 10


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_weather": get_weather,
    "search_transport": search_transport,
    "search_hotel": search_hotel,
    "search_poi": search_poi,
    "calc_budget": calc_budget,
    "save_preference": save_preference,
}


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """统一入口：按工具名分发，异常兜底为契约内错误。"""
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        return {"ok": False, "error": {"code": "TOOL_NOT_FOUND",
                "message": f"未注册工具：{name}", "retryable": False}}
    try:
        return fn(**arguments)
    except TypeError as e:
        return {"ok": False, "error": {"code": "BAD_ARGUMENTS",
                "message": str(e), "retryable": False}}


if __name__ == "__main__":
    print(json.dumps(dispatch("get_weather", {
        "city": "大理", "start_date": "2026-11-08", "end_date": "2026-11-11"
    }), ensure_ascii=False, indent=2))
