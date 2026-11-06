"""城市自动补全：用户提到的城市不在知识库里时，在线查一次并写入知识库。

设计边界：
- **只补"有没有数据"这一层**，不改编排逻辑。抓到的景点是真实 OSM 数据（名称/坐标/
  开放时间/是否收费），门票与游玩时长按类别估算并标注；酒店只有名称与星级，价格按
  星级估算并标注——宁可写清"估算"，也不假装是实时报价。
- 抓取失败（地名不存在、网络不通、上游无返回）只返回失败状态，让对话层沿用原有
  "这边没有数据"的处理，绝不静默排一份空行程。
- 成功结果落 SQLite（source='auto-fetch'），后台看板可见、可编辑、可删除；同一城市
  只抓一次（库里已有数据即跳过），失败结果有 TTL，避免每轮都卡在网络超时上。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from . import kb

# 关闭开关：离线演示/CI 里不想因为一句"去XX"卡十几秒，设 TRAVEL_AUTOFETCH=0 即可
ENABLED = os.environ.get("TRAVEL_AUTOFETCH", "1") != "0"
# 整次抓取的总预算（含多镜像重试）：用户在聊天里等，不能让 Overpass 拖到一分钟
TIMEOUT = float(os.environ.get("TRAVEL_AUTOFETCH_TIMEOUT", "10"))
# 单个景点的定向搜索预算：夹在一次对话中间，宁可早点交给下一档
SPOT_TIMEOUT = float(os.environ.get("TRAVEL_SPOT_TIMEOUT", "4"))
MAX_POIS = int(os.environ.get("TRAVEL_AUTOFETCH_MAX_POIS", "60"))
MAX_HOTELS = int(os.environ.get("TRAVEL_AUTOFETCH_MAX_HOTELS", "20"))
# 模型给的坐标离城市中心超过这个距离，就当作幻觉（或根本没在本市）丢弃
LLM_MAX_KM = 60.0

# 失败/空结果缓存：同一城市 6 小时内不再重试，避免"每次说话都等一次超时"
FAIL_TTL_SEC = 6 * 3600
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
# Overpass 整体不可达时，10 分钟内不再为每个新城市各等一遍超时
_OSM_DOWN_UNTIL = 0.0
OSM_DOWN_TTL = 600.0

# tourism 标签 → (游玩时长分钟, 无 fee 标签时的门票估算)
_POI_KIND = {
    "museum": (120, 40),
    "viewpoint": (45, 0),
    "zoo": (150, 80),
    "theme_park": (240, 180),
    "artwork": (20, 0),
    "attraction": (90, 0),
}

# 星级 → 每晚价格估算（OSM 没有报价，只能按星级给区间中位数）
_STAR_PRICE = {5: 900, 4: 550, 3: 350, 2: 220, 1: 150}
_LODGING = ("hotel", "guest_house", "hostel", "apartment", "motel")

# 省/自治区/大区不是城市：地理编码能返回它们的坐标，但按省排行程毫无意义。
# 这些名字一律不触发自动补全，让对话层沿用"追问具体城市"的原逻辑。
_REGIONS = {
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏",
    "新疆", "香港", "澳门", "华北", "华南", "华东", "华中", "西南", "西北",
    "东北", "江南", "中原", "长三角", "珠三角", "京津冀", "川西", "滇西",
}
_REGION_SUFFIX = ("省", "自治区", "特别行政区", "地区", "大区", "半岛", "草原")


def plausible_city(city: str) -> bool:
    """像不像一个具体城市。省份/大区/超长地名一律不当城市抓取。"""
    city = (city or "").strip()
    if not city or len(city) > 8:
        return False
    if city in _REGIONS or city.endswith(_REGION_SUFFIX):
        return False
    return True


def enabled() -> bool:
    return ENABLED


def covered(city: str, db=None) -> bool:
    """知识库或静态库里已有该城市的可排数据。"""
    if not city:
        return False
    try:
        from .tools import HOTELS_DB, POIS_DB
        if POIS_DB.get(city):
            return True
    except Exception:
        pass
    db = db or kb.get_kb()
    if db is None:
        return False
    return bool(db.list_pois(city) or db.list_hotels(city))


def ensure_city(city: str, *, db=None, fetcher=None, client=None,
                force: bool = False) -> dict:
    """保证这个城市有可排数据；缺就抓一次并入库。

    数据源顺序：Overpass（真实 OSM）→ 模型整理 + 距离校验（在线源不可达时）。
    返回 {"status", "city", "pois", "hotels", "source", "message", "cached"}：
      exists     已有数据，什么都没做
      fetched    抓到并入库
      empty      上游没有景点（小地名/海域等）
      unresolved 地名解析不出坐标
      no_kb      知识库不可用（只读打包环境）
      disabled   开关关闭
      skip       不像具体城市（省/大区）
      failed     抓取异常（网络/上游）
    """
    city = (city or "").strip()
    if not city:
        return {"status": "no_kb" if db is None else "empty", "city": "",
                "pois": 0, "hotels": 0, "message": "没有城市名"}
    if covered(city, db):
        return {"status": "exists", "city": city, "pois": 0, "hotels": 0,
                "message": "知识库已有该城市"}
    if not ENABLED and not force:
        return {"status": "disabled", "city": city, "pois": 0, "hotels": 0,
                "message": "自动补全已关闭（TRAVEL_AUTOFETCH=0）"}
    if not plausible_city(city) and not force:
        return {"status": "skip", "city": city, "pois": 0, "hotels": 0,
                "message": "不像具体城市名，不抓取"}

    with _CACHE_LOCK:
        hit = _CACHE.get(city)
        if hit and time.time() - hit[0] < FAIL_TTL_SEC:
            return {**hit[1], "cached": True}

    db = db or kb.get_kb()
    if db is None:
        return {"status": "no_kb", "city": city, "pois": 0, "hotels": 0,
                "message": "知识库不可用"}

    center = _resolve_center(city)
    if not center:
        res = {"status": "unresolved", "city": city, "pois": 0, "hotels": 0,
               "message": f"地名「{city}」解析不到坐标"}
        _remember(city, res)
        _log(db, city, "unresolved", res["message"])
        return res

    try:
        poi_rows, hotel_rows, source = _load(city, center, fetcher, client)
    except Exception as e:                        # 网络/上游异常：降级，不阻塞对话
        msg = f"{type(e).__name__}: {str(e)[:150]}"
        res = {"status": "failed", "city": city, "pois": 0, "hotels": 0,
               "message": msg}
        _remember(city, res)
        _log(db, city, "failed", msg)
        return res

    if not poi_rows:
        res = {"status": "empty", "city": city, "pois": 0, "hotels": 0,
               "message": "上游没有返回可用景点"}
        _remember(city, res)
        _log(db, city, "empty", res["message"])
        return res

    bbox = _bbox(center)
    try:
        db.upsert_city(city, center, bbox)
    except Exception:
        pass
    for p in poi_rows[:MAX_POIS]:
        db.upsert_poi(city, p, source="auto-fetch" if source == "osm" else "auto-fetch-llm")
    for h in hotel_rows[:MAX_HOTELS]:
        db.upsert_hotel(city, h, source="auto-fetch")

    res = {"status": "fetched", "city": city, "source": source,
           "pois": len(poi_rows[:MAX_POIS]), "hotels": len(hotel_rows[:MAX_HOTELS]),
           "message": f"在线抓取 {len(poi_rows)} 个景点、{len(hotel_rows)} 家住宿并入库"}
    _remember(city, res)
    _log(db, city, "success", f"{source}: " + res["message"])
    return res


# ---------------------------------------------------------------- 点名景点补全
#
# 「我想去上海迪士尼」而库里只有外滩/豫园时，编排器匹配不到这个 POI，
# 用户点名的景点就被静默吞掉——这是「加不上景点」最常见的真因，不是排序问题。
# 与 ensure_city 的分工：那边补整座城，这边只按名字补一个点，走同一条
# 距离校验（离城中心 >60km 一律不当本市的）。

_SPOT_SUFFIX = ("旅游度假区", "度假区", "风景区", "景区", "乐园", "公园",
                "博物院", "博物馆", "古镇", "古城", "步行街", "广场",
                "大桥", "大剧院", "游乐园")


def _spot_terms(name: str, city: str) -> list[str]:
    """把点名拆成由宽到窄的搜索词：全名 → 去城市前缀 → 去类型后缀。

    OSM 里「上海迪士尼」常写作「上海迪士尼乐园」或「迪士尼乐园」，按全名
    精确匹配会全军覆没；「迪士尼」这种核心词反而最稳。
    """
    base = (name or "").strip()
    if not base:
        return []
    out = [base]
    if city and base.startswith(city) and len(base) - len(city) >= 2:
        out.append(base[len(city):])
    for t in list(out):
        for suf in _SPOT_SUFFIX:
            if t.endswith(suf) and len(t) - len(suf) >= 2:
                out.append(t[:-len(suf)])
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:3]


def _search_spot_osm(term: str, center: list[float], city: str) -> dict | None:
    """在本城包围盒内按名字搜一个景点，返回离城中心最近的命中。"""
    s, w, n, e = _bbox(center)
    safe = re.sub(r'["\\\n\r]', "", term)
    if len(safe) < 2:
        return None
    q = (f'[out:json][timeout:{int(TIMEOUT)}];'
         f'(nwr["name"~"{safe}",i]({s},{w},{n},{e}););out center 20;')
    data = _overpass(q, budget=SPOT_TIMEOUT)
    best: dict | None = None
    best_km = float("inf")
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        nm = (tags.get("name") or "").strip()
        if not nm:
            continue
        c = el.get("center") or {}
        lat = el.get("lat", c.get("lat"))
        lon = el.get("lon", c.get("lon"))
        if lat is None or lon is None:
            continue
        km = _km(center[0], center[1], float(lat), float(lon))
        if km > LLM_MAX_KM or km >= best_km:
            continue
        kind = (tags.get("tourism") or tags.get("leisure")
                or tags.get("amenity") or "attraction")
        best = _poi(nm, kind, tags, city, float(lat), float(lon))
        best_km = km
    return best


def ensure_spot(city: str, name: str, *, db=None, client=None,
                center: list[float] | None = None,
                fetcher: "Callable | None" = None) -> dict:
    """用户点名的景点库里没有时，定向补一条进来。

    顺序：库里模糊命中 → 模型定位（有 client 时，快）→ Overpass 按名搜索
    → 都没拿到就按"未收录景点"排进行程（位置按市区估算并标注）。
    返回 {"status","city","name","poi","source","message"}：
      exists      库里已有（名字不完全一致但在同一景点）
      fetched     补到并入库
      custom      没查到，但按用户点名的名称入库以便排程（信息待核实）
      disabled    在线补全已关闭
      no_kb       知识库不可用
      unresolved  城市坐标解析不出来
    """
    from .composer import name_hit

    city, name = (city or "").strip(), (name or "").strip()
    base = {"status": "not_found", "city": city, "name": name,
            "poi": None, "source": "", "message": ""}
    if not city or len(name) < 2:
        return {**base, "message": "景点名太短，无法检索"}
    db = db or kb.get_kb()
    if db is None:
        return {**base, "status": "no_kb", "message": "知识库不可用"}

    for p in (db.list_pois(city) or []):
        if name_hit(name, p.get("name") or ""):
            return {**base, "status": "exists", "poi": p,
                    "source": p.get("source") or "",
                    "message": f"资料库里已有「{p['name']}」"}
    if not ENABLED:
        return {**base, "status": "disabled", "message": "在线补全已关闭"}
    center = center or _resolve_center(city)
    if not center:
        return {**base, "status": "unresolved",
                "message": f"解析不到「{city}」的坐标"}

    if fetcher is not None:                        # 测试/离线注入
        hit = fetcher(city, name, center)
        if hit:
            pid = db.upsert_poi(city, hit, source="auto-fetch")
            hit["poi_id"] = pid
            return {**base, "status": "fetched", "poi": hit, "source": "custom",
                    "message": f"补到「{hit['name']}」"}

    global _OSM_DOWN_UNTIL
    # 顺序上把模型放在 OSM 前面：这一步夹在一次对话中间，用户正在等方案。
    # OSM 在本机常常整体不可达（每个镜像都要等到超时），而模型 2~4 秒就能给出
    # 名字+坐标，坐标再用 60km 校验兜住幻觉——比让用户干等 4 秒以上更划算。
    if client is not None:
        try:
            from . import llm as llm_layer
            row, rec = llm_layer.lookup_poi(client, city, name)
        except Exception as e:                     # 额度不足在这里显式上抛
            from .llm import QuotaError
            if isinstance(e, QuotaError):
                raise
            row = None
        if row and _km(center[0], center[1], row["lat"], row["lon"]) <= LLM_MAX_KM:
            poi = {
                "name": row["name"], "category": row["category"],
                "area": city, "geo": [row["lat"], row["lon"]],
                "appeal": 4, "duration_min": row["duration_min"],
                "cost": int(row["ticket"]),
                "open_hours": row["open_hours"] or "全天",
                "open_days": "每天",
                "need_booking": row["ticket"] > 100,
                "tags": [row["category"]],
                "intensity": ("medium" if row["duration_min"] >= 240 else "low"),
                "indoor_alternative": "",
                "note": (row["note"] or "") + "（模型整理，门票与时长仅供参考）",
            }
            pid = db.upsert_poi(city, poi, source="auto-fetch-llm")
            poi["poi_id"] = pid
            db.record_log(city, "auto-fetch", "success", 1, 0,
                          f"点名景点补全（模型）：{name} → {poi['name']}")
            return {**base, "status": "fetched", "poi": poi, "source": "llm",
                    "message": f"补到「{poi['name']}」（模型整理，坐标为近似值）"}

    if time.time() >= _OSM_DOWN_UNTIL:
        for term in _spot_terms(name, city):
            try:
                hit = _search_spot_osm(term, center, city)
            except Exception:                      # 网络/上游异常 → 按未收录安排
                _OSM_DOWN_UNTIL = time.time() + OSM_DOWN_TTL
                hit = None
                break
            if hit:
                pid = db.upsert_poi(city, hit, source="auto-fetch")
                hit["poi_id"] = pid
                db.record_log(city, "auto-fetch", "success", 1, 0,
                              f"点名景点补全（OSM）：{name} → {hit['name']}")
                return {**base, "status": "fetched", "poi": hit, "source": "osm",
                        "message": f"在线补到「{hit['name']}」"}

    # 最后兜底：用户已经明确说了要去，系统至少要把这件事排进行程。
    # 回一句"查不到"是最差的结果——他要的是行程里有这一站，不是要一个检索报告。
    # 位置按城市中心估算、门票记 0（不虚构价格），并在方案假设里写明待核实。
    poi = {
        "name": name, "category": "attraction", "area": city,
        "geo": [round(center[0], 4), round(center[1], 4)],
        "appeal": 5, "duration_min": 240, "cost": 0,
        "open_hours": "全天", "open_days": "每天",
        "need_booking": True, "tags": ["用户点名"],
        "intensity": "medium", "indoor_alternative": "",
        "note": "用户点名安排；未收录，位置按市区估算，开放时间与门票请自行确认",
    }
    try:
        pid = db.upsert_poi(city, poi, source="user-named")
        poi["poi_id"] = pid
        db.record_log(city, "auto-fetch", "success", 1, 0,
                      f"点名景点按未收录安排：{name}")
    except Exception:
        return {**base, "message": f"资料库与在线源都没有「{name}」的可靠信息"}
    return {**base, "status": "custom", "poi": poi, "source": "user-named",
            "message": f"「{name}」没有收录，按你点名的景点排进行程（信息待核实）"}


def _load(city: str, center: list[float], fetcher=None,
          client=None) -> tuple[list[dict], list[dict], str]:
    """按数据源优先级取数：注入的 fetcher > 模型整理 > Overpass。

    模型放在 OSM 前面：这一步夹在一次对话中间，用户正在等回复。本机到
    Overpass 常整体不可达（每个镜像都等超时，10 秒起），而模型 2~4 秒就能
    给出景点名+坐标，坐标再用 60km 校验兜住幻觉——比让用户干等更划算。
    """
    global _OSM_DOWN_UNTIL
    if fetcher is not None:
        pois, hotels = fetcher(city, center)
        return pois, hotels, "custom"
    err: Exception | None = None
    if client is not None:
        try:
            pois, _hotels = fetch_city_llm(client, city, center)
            if pois:
                return pois, [], "llm"
        except Exception as e:                    # 额度不足要上抛，不给假数据
            from .llm import QuotaError
            if isinstance(e, QuotaError):
                raise
            err = e
    if time.time() >= _OSM_DOWN_UNTIL:
        try:
            pois, hotels = fetch_city(city, center)
            if pois:
                _OSM_DOWN_UNTIL = 0.0
                return pois, hotels, "osm"
        except Exception as e:                    # 换下一档数据源
            err = e
            _OSM_DOWN_UNTIL = time.time() + OSM_DOWN_TTL
    else:
        err = err or ConnectionError("Overpass 最近不可达，已跳过重试")
    if err is not None:
        raise err
    return [], [], "osm"


def fetch_city_llm(client, city: str, center: list[float]
                   ) -> tuple[list[dict], list[dict]]:
    """模型整理候选景点，坐标必须落在城区 60km 内，否则丢弃。"""
    from . import llm as llm_layer
    rows, rec = llm_layer.discover_pois(client, city, limit=MAX_POIS)
    if not rows:
        raise RuntimeError(f"模型未能给出可用景点：{(rec.errors or ['未知'])[:1]}")
    out: list[dict] = []
    for r in rows:
        if _km(center[0], center[1], r["lat"], r["lon"]) > LLM_MAX_KM:
            continue                              # 坐标离城区太远：不是本市或纯属编造
        out.append({
            "name": r["name"],
            "category": "attraction" if r["category"] in ("attraction", "park")
            else "attraction",
            "area": city,
            "geo": [r["lat"], r["lon"]],
            "appeal": 3,
            "duration_min": r["duration_min"],
            "cost": int(r["ticket"]),
            "open_hours": "全天",
            "open_days": "每天",
            "need_booking": False,
            "tags": [r["category"]],
            "intensity": "medium" if r["category"] == "park" else "low",
            "indoor_alternative": "",
            "note": "模型整理，坐标为近似值，门票为参考价",
        })
    if not out:
        raise RuntimeError("模型给出的坐标全部不在城区范围内")
    return out, []


# ------------------------------------------------------------------ 抓取实现


def fetch_city(city: str, center: list[float]) -> tuple[list[dict], list[dict]]:
    """一次 Overpass 请求同时取景点与住宿，按标签分流。"""
    s, w, n, e = _bbox(center)
    q = (f'[out:json][timeout:{int(TIMEOUT)}];'
         f'('
         f'node["tourism"~"^(attraction|museum|viewpoint|zoo|theme_park|artwork)$"]["name"]'
         f'({s},{w},{n},{e});'
         f'way["tourism"~"^(attraction|museum|viewpoint|zoo|theme_park)$"]["name"]'
         f'({s},{w},{n},{e});'
         f');out center {MAX_POIS};'
         f'('
         f'node["tourism"~"^({"|".join(_LODGING)})$"]["name"]({s},{w},{n},{e});'
         f');out center {MAX_HOTELS};')
    data = _overpass(q)
    pois: list[dict] = []
    hotels: list[dict] = []
    seen_p: set[str] = set()
    seen_h: set[str] = set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        c = el.get("center") or {}
        lat = el.get("lat", c.get("lat"))
        lon = el.get("lon", c.get("lon"))
        if lat is None or lon is None:
            continue
        kind = tags.get("tourism", "")
        if kind in _LODGING:
            if name in seen_h:
                continue
            seen_h.add(name)
            hotels.append(_hotel(name, tags, center, float(lat), float(lon)))
        else:
            if name in seen_p:
                continue
            seen_p.add(name)
            pois.append(_poi(name, kind, tags, city, float(lat), float(lon)))
    return pois, hotels


def _poi(name: str, kind: str, tags: dict, city: str,
         lat: float, lon: float) -> dict:
    dur, cost = _POI_KIND.get(kind, (90, 0))
    fee = str(tags.get("fee", "")).lower()
    if fee == "yes":
        cost = max(cost, 40)
    appeal = 3
    if tags.get("wikidata") or tags.get("wikipedia"):
        appeal = 4
    if kind in ("theme_park", "museum") or tags.get("heritage"):
        appeal = max(appeal, 4)
    if tags.get("historic") in ("castle", "monument", "memorial", "ruins"):
        appeal = max(appeal, 4)
    return {
        "name": name,
        "category": "attraction",
        "area": tags.get("addr:district") or tags.get("addr:city") or city,
        "geo": [round(lat, 4), round(lon, 4)],
        "appeal": appeal,
        "duration_min": dur,
        "cost": cost,
        "open_hours": tags.get("opening_hours") or "全天",
        "open_days": "每天",
        "need_booking": kind == "theme_park",
        "tags": [v for k, v in tags.items()
                 if k in ("tourism", "historic", "leisure", "museum")][:3],
        "intensity": "medium" if kind in ("theme_park", "zoo") else "low",
        "indoor_alternative": "",
        "note": "在线抓取（OSM）；门票与游玩时长为估算值",
    }


def _hotel(name: str, tags: dict, center: list[float],
           lat: float, lon: float) -> dict:
    raw_star = str(tags.get("stars") or "").strip()
    star = int(raw_star) if raw_star.isdigit() and 1 <= int(raw_star) <= 5 else 3
    price = _STAR_PRICE.get(star, 300)
    return {
        "name": name,
        "area": tags.get("addr:district") or tags.get("addr:street") or "",
        "star": star,
        "price": price,
        "geo": [round(lat, 4), round(lon, 4)],
        "cancel_free": False,
        "dist_to_core_km": round(_km(center[0], center[1], lat, lon), 1),
        "rating": 4.3,
        "note": f"在线抓取（OSM）；{star} 星，房价为星级估算，非实时报价",
    }


def _overpass(query: str, budget: float | None = None) -> dict:
    """多镜像轮询，但整次请求共享总预算：超时就换下一个，预算用完直接抛错。

    budget 是给「点名景点补全」用的：那一步夹在一次对话中间，用户在等方案，
    不能为了一个景点搜掉 10 秒——4 秒拿不到就交给下一档数据源。
    """
    data = urllib.parse.urlencode({"data": query}).encode()
    deadline = time.time() + (budget or TIMEOUT)
    last: Exception | None = None
    for host in kb.OVERPASS_MIRRORS:
        left = deadline - time.time()
        if left <= 2:
            break
        try:
            req = urllib.request.Request(
                host, data=data, headers={"User-Agent": "travel-agent/1.0"})
            with urllib.request.urlopen(req, timeout=left) as r:
                return json.loads(r.read())
        except Exception as e:                    # 换镜像重试
            last = e
    raise ConnectionError(f"Overpass 全部镜像不可达，最后错误: {last}")


def _resolve_center(city: str) -> list[float] | None:
    """城市坐标：先本地核实表，再地理编码。

    地理编码只认"人口聚居地"（Open-Meteo 的 feature_code 以 PPL 开头，或有人口数）：
    省/国家/地区返回的是 ADM1/PCLI，拿它的中心点抓一圈景点，会把整个省拼成一座城。
    """
    if not plausible_city(city):
        return None
    try:
        from .tools import CITY_GEO
        if city in CITY_GEO:
            return CITY_GEO[city]
    except Exception:
        pass
    try:
        url = ("https://geocoding-api.open-meteo.com/v1/search?count=5&language=zh"
               f"&format=json&name={urllib.parse.quote(city)}")
        req = urllib.request.Request(url, headers={"User-Agent": "travel-agent/1.0"})
        with urllib.request.urlopen(req, timeout=min(TIMEOUT, 12)) as r:
            data = json.loads(r.read())
        for row in (data or {}).get("results") or []:
            fc = str(row.get("feature_code") or "")
            if fc.startswith("PPL") or row.get("population"):
                return [float(row["latitude"]), float(row["longitude"])]
    except Exception:
        return None
    return None


def _bbox(center: list[float], span: float = 0.35) -> tuple[float, float, float, float]:
    """城市包围盒：纬度 ±span（约 39km），经度按纬度收窄，覆盖主城区+近郊。"""
    lat, lon = float(center[0]), float(center[1])
    return (round(lat - span, 4), round(lon - span * 1.25, 4),
            round(lat + span, 4), round(lon + span * 1.25, 4))


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _remember(city: str, res: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[city] = (time.time(), dict(res))


def _log(db, city: str, status: str, message: str) -> None:
    try:
        db.record_log(city, "auto-fetch", status, 0, 0, message)
    except Exception:
        pass


def reset_cache() -> None:
    """测试用：清掉失败缓存与 Overpass 熔断状态。"""
    global _OSM_DOWN_UNTIL
    with _CACHE_LOCK:
        _CACHE.clear()
    _OSM_DOWN_UNTIL = 0.0
