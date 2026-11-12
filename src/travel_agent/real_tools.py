"""真实数据工具集 —— 与 mock_tools 同接口，数据源全部换成真实数据。

三类来源，均可离线降级：
  1. 实时 API（免费无 Key）：
     - 天气：Open-Meteo（≤16 天用真实预报；更远用 ERA5 2023-2025 同期历史均值）
     - 路网耗时：OSRM public server（真实道路距离与行车时间，替代"直线距离÷车速"估算）
  2. 2026-10-29 联网检索核实的静态真实数据（携程/同程/12306 口径）：
     - 景点门票与开放时间（崇圣寺三塔 75 元、洗马潭索道 320 元…）
     - 城际班次（JD5621/MU9698 直飞、大理→丽江 C 字头动车真实时刻与票价）
     - 酒店在售价（大理古城/才村/龙龛/磻溪 8 家真实在营客栈民宿）
  3. 降级路径：API 不可用时回落到内置气候均值与"直线距离×1.3 道路系数"估算，
     保证断网也能出完整行程（结构不变，精度下降，note 里如实标注）。

返回统一遵循 error_contract：
    成功 -> {"ok": true, ...}
    失败 -> {"ok": false, "error": {"code": ..., "message": ..., "retryable": ...}}
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

# 缓存目录统一走 data/.cache（见 paths.py；.pyz 只读环境自动退到工作目录）
from .city_geo import EXTRA_CITY_GEO
from .paths import cache_dir as _cache_dir

CACHE_DIR = _cache_dir()
WX_CACHE = CACHE_DIR / "weather.json"
ROUTE_CACHE = CACHE_DIR / "routes.json"
NETWORK_TIMEOUT = 6

DATA_RETRIEVED = "2026-10-29"


# ---------------------------------------------------------------- 知识库（SQLite，可写数据源）

from . import kb as _kb  # noqa: E402  置于模块常量之后，避免循环导入（kb 种子内部才 import 本模块）


def _kb_pois(city: str) -> list[dict[str, Any]] | None:
    db = _kb.get_kb()
    return db.list_pois(city) if db else None


def _kb_hotels(city: str) -> list[dict[str, Any]] | None:
    db = _kb.get_kb()
    return db.list_hotels(city) if db else None


def _kb_schedules(a: str, b: str) -> list[dict[str, Any]] | None:
    db = _kb.get_kb()
    return db.list_schedules(a, b) if db else None


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 基础数据（真实）

CITY_GEO: dict[str, list[float]] = {
    # 有 POI 数据的城市
    "大理": [25.6930, 100.1610],
    "丽江": [26.8720, 100.2300],
    "杭州": [30.2460, 120.2100],
    # 有本地 POI/酒店/班次数据的城市，必须进表：否则「邯郸天气」只能靠正则兜底，
    # 「邯郸现在天气」会把「现在」并进城市名（2026-10-31 用户报的问题）
    "邯郸": [36.6250, 114.5390],
    # 天气问答要覆盖的常用城市（问天气不需要 POI 数据，只要坐标）
    "北京": [39.9040, 116.4070],
    "上海": [31.2300, 121.4700],
    "天津": [39.1300, 117.2000],
    "重庆": [29.5630, 106.5510],
    "成都": [30.5720, 104.0660],
    "西安": [34.3410, 108.9400],
    "广州": [23.1290, 113.2640],
    "深圳": [22.5430, 114.0580],
    "南京": [32.0600, 118.7960],
    "苏州": [31.2990, 120.5850],
    "武汉": [30.5920, 114.3050],
    "长沙": [28.2280, 112.9380],
    "青岛": [36.0670, 120.3820],
    "厦门": [24.4790, 118.0890],
    "三亚": [18.2520, 109.5120],
    "海口": [20.0440, 110.1990],
    "昆明": [25.0380, 102.7180],
    "桂林": [25.2740, 110.2990],
    "贵阳": [26.6470, 106.6300],
    "南宁": [22.8170, 108.3660],
    "福州": [26.0740, 119.2960],
    "南昌": [28.6820, 115.8580],
    "合肥": [31.8200, 117.2270],
    "郑州": [34.7460, 113.6250],
    "济南": [36.6510, 117.1200],
    "太原": [37.8700, 112.5480],
    "石家庄": [38.0420, 114.5140],
    "沈阳": [41.8050, 123.4310],
    "长春": [43.8170, 125.3230],
    "哈尔滨": [45.8030, 126.5350],
    "大连": [38.9140, 121.6140],
    "呼和浩特": [40.8420, 111.7490],
    "银川": [38.4870, 106.2310],
    "兰州": [36.0610, 103.8340],
    "西宁": [36.6170, 101.7780],
    "乌鲁木齐": [43.8250, 87.6160],
    "拉萨": [29.6520, 91.1720],
    "张家界": [29.1170, 110.4790],
    "黄山": [29.7140, 118.3370],
    "婺源": [29.2480, 117.8610],
    "乌镇": [30.7440, 120.4890],
    "平遥": [37.2020, 112.1760],
    "敦煌": [40.1420, 94.6620],
    "洛阳": [34.6190, 112.4540],
    "开封": [34.7970, 114.3070],
    "扬州": [32.3940, 119.4130],
    "无锡": [31.4910, 120.3120],
    "宁波": [29.8680, 121.5440],
    "绍兴": [30.0020, 120.5810],
    "温州": [27.9940, 120.6990],
    "舟山": [29.9850, 122.2070],
    "泉州": [24.8740, 118.6760],
    "珠海": [22.2710, 113.5770],
    "北海": [21.4810, 109.1200],
    "秦皇岛": [39.9350, 119.6000],
    "承德": [40.9510, 117.9630],
    "烟台": [37.4640, 121.4480],
    "威海": [37.5130, 122.1200],
    "曲阜": [35.5810, 116.9860],
    "九寨沟": [33.2600, 103.9200],
    "峨眉山": [29.6010, 103.4840],
    "香格里拉": [27.8290, 99.7060],
    "西双版纳": [22.0010, 100.7970],
    "呼伦贝尔": [49.2120, 119.7650],
    "长白山": [42.0060, 128.0560],
    "喀什": [39.4700, 75.9900],
    "伊宁": [43.9160, 81.3170],
}

# 全国地级政区补充表（city_geo.py，280 城）：只补缺，不覆盖上面人工核实过的坐标
for _name, _geo in EXTRA_CITY_GEO.items():
    CITY_GEO.setdefault(_name, _geo)

_GEO_CACHE: dict[str, list[float] | None] = {}

# 「廊坊市」「延边州」这类带行政后缀的说法也要认（原词优先，剥后缀只在未命中时用）
_CITY_SUFFIX = ("特别行政区", "自治区", "自治州", "地区", "盟", "市", "州")


def city_variants(city: str) -> list[str]:
    """「廊坊市」「延边州」→ 同时给出剥掉行政后缀的写法。"""
    out = [city]
    for suf in _CITY_SUFFIX:
        if city.endswith(suf) and len(city) - len(suf) >= 2:
            stripped = city[: -len(suf)]
            if stripped not in out:
                out.append(stripped)
    return out


def _latin_only(name: str) -> bool:
    return bool(name) and all(ord(ch) < 128 for ch in name)


def _kb_city_center(city: str) -> list[float] | None:
    """KB 已收录 POI 的坐标均值（≥1 个即返回）。境外城市（京都等）不在
    国内城市表里，但 autofetch 抓到的景点坐标就是本市位置，均值即城心。"""
    try:
        db = _kb.get_kb()
        pois = db.list_pois(city) if db else None
    except Exception:
        return None
    geos = [p.get("geo") for p in (pois or [])
            if p.get("geo") and len(p["geo"]) == 2]
    if not geos:
        return None
    return [round(sum(g[i] for g in geos) / len(geos), 4) for i in (0, 1)]


def resolve_geo(city: str) -> list[float] | None:
    """城市 → [纬度, 经度]。本地表（351 城）+ 拉丁输入的地理编码兜底。

    天气问答不能只认有 POI 的几个城市——用户问「廊坊天气」是很自然的事。
    **中文名只查本地表**：Open-Meteo 的地理编码只吃拉丁输入，喂中文会返回
    风马牛不相及的城镇（实测「昆山」拿回福建一个点、「腾冲」拿回佛山），
    坐标全错还看不出来。查不到就如实说没收录，绝不拿别的城市顶替。
    """
    if not city:
        return None
    city = city.strip()
    if city in _GEO_CACHE:
        return _GEO_CACHE[city]
    names = city_variants(city)
    for name in names:
        if name in CITY_GEO:
            _GEO_CACHE[city] = CITY_GEO[name]
            return CITY_GEO[name]
    # 本地表没有（境外城市、新收录）：用 KB 里该城市已收录 POI 的坐标均值
    # 当城心。这不是"拿别的城市顶替"——坐标就来自本市景点，均值≈城区中心。
    # 实测「去京都」：京都 12 个景点全在库、却因城心查不到全部被距离闸否决，
    # 编排器退回大理默认坐标，行程里一个景点都没有还毫无提示。
    geo = _kb_city_center(city)
    if geo:
        _GEO_CACHE[city] = geo
        return geo
    if not _latin_only(city):
        _GEO_CACHE[city] = None
        return None
    geo = None
    for name in names:
        try:
            url = ("https://geocoding-api.open-meteo.com/v1/search?count=1&language=zh"
                   f"&format=json&name={urllib.parse.quote(name)}")
            data = _http_json(url)
            rows = (data or {}).get("results") or []
            if rows:
                geo = [float(rows[0]["latitude"]), float(rows[0]["longitude"])]
                break
        except Exception:
            continue
    _GEO_CACHE[city] = geo
    return geo

CITY_DB: dict[str, dict[str, Any]] = {
    "大理": {
        "climate": {   # 气候常年均值，仅作 API 不可用时的降级数据
            11: {"low": 8, "high": 21, "rain_prob": 15, "desc": "晴到多云"},
            10: {"low": 12, "high": 23, "rain_prob": 25, "desc": "多云"},
        },
        "transport_hubs": {"train": "大理站", "flight": "大理凤仪机场"},
        "areas": ["大理古城", "才村码头", "龙龛码头", "磻溪S湾", "喜洲", "双廊", "下关"],
    },
    "丽江": {
        "climate": {11: {"low": 3, "high": 18, "rain_prob": 10, "desc": "晴"}},
        "transport_hubs": {"train": "丽江站", "flight": "三义机场"},
        "areas": ["大研古城", "束河", "白沙"],
    },
    "杭州": {
        "climate": {11: {"low": 12, "high": 20, "rain_prob": 30, "desc": "多云转阴"}},
        "transport_hubs": {"train": "杭州东站", "flight": "萧山机场"},
        "areas": ["西湖", "滨江"],
    },
}

# 班次为 12306/航司 2026-10 查询口径的整理；票价随季节浮动，以购票日实际为准。
ROUTES_DB: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("杭州", "大理"): [
        {"mode": "flight", "code": "JD5621", "depart": "06:55", "arrive": "10:50",
         "duration_min": 235, "price": 520,
         "note": f"首都航空 直飞 萧山T3→凤仪机场，11月2.2~2.5折（{DATA_RETRIEVED} 查询）"},
        {"mode": "flight", "code": "TV6017", "depart": "10:00", "arrive": "13:40",
         "duration_min": 220, "price": 748, "note": "西藏航空 直飞"},
        {"mode": "flight", "code": "MU9698", "depart": "13:40", "arrive": "17:35",
         "duration_min": 235, "price": 780, "note": "东方航空 直飞"},
        {"mode": "train", "code": "高铁昆明中转", "depart": "08:00", "arrive": "20:30",
         "duration_min": 750, "price": 639,
         "note": "杭州东→昆明南→大理，二等座合计约 639 元，全程 9~10 小时"},
    ],
    ("大理", "杭州"): [
        {"mode": "flight", "code": "MU7603", "depart": "16:00", "arrive": "19:40",
         "duration_min": 220, "price": 760,
         "note": f"东方航空 直飞 回程，凤仪机场→萧山T3（{DATA_RETRIEVED} 查询）"},
    ],
    ("大理", "丽江"): [
        {"mode": "train", "code": "C323", "depart": "07:00", "arrive": "08:36",
         "duration_min": 96, "price": 80, "note": "最快班次 1h36m"},
        {"mode": "train", "code": "C985", "depart": "07:45", "arrive": "09:35",
         "duration_min": 110, "price": 80, "note": ""},
        {"mode": "train", "code": "C373", "depart": "09:13", "arrive": "11:29",
         "duration_min": 136, "price": 72, "note": "票价最低（折扣席）"},
        {"mode": "train", "code": "C325", "depart": "16:35", "arrive": "19:05",
         "duration_min": 150, "price": 80, "note": "经停鹤庆"},
        {"mode": "train", "code": "C321", "depart": "18:18", "arrive": "20:19",
         "duration_min": 121, "price": 80, "note": "傍晚班次"},
        {"mode": "train", "code": "Y767", "depart": "08:17", "arrive": "10:45",
         "duration_min": 148, "price": 24, "note": "普快硬座，慢但便宜"},
    ],
    ("丽江", "大理"): [
        {"mode": "train", "code": "D8698", "depart": "09:35", "arrive": "11:49",
         "duration_min": 134, "price": 80, "note": ""},
        {"mode": "train", "code": "D8150", "depart": "20:20", "arrive": "22:32",
         "duration_min": 132, "price": 80, "note": "晚班"},
    ],
    # 邯郸东→上海虹桥 高铁约 6.5h；K字头夕发朝至（gaotie.cn/同程 2026-10-29 查询）
    ("邯郸", "上海"): [
        {"mode": "train", "code": "G2811", "depart": "09:05", "arrive": "15:59",
         "duration_min": 414, "price": 622,
         "note": f"高铁 邯郸东→上海虹桥，二等座公布价 656（{DATA_RETRIEVED} 查询）"},
        {"mode": "train", "code": "G1958", "depart": "11:21", "arrive": "17:44",
         "duration_min": 383, "price": 551, "note": "高铁，票价最低档"},
        {"mode": "train", "code": "G1954", "depart": "14:56", "arrive": "21:14",
         "duration_min": 378, "price": 609, "note": "高铁 傍晚抵达"},
        {"mode": "train", "code": "K233", "depart": "14:31", "arrive": "09:05",
         "duration_min": 1114, "price": 279,
         "note": "普快 邯郸→上海 夕发朝至（硬卧中铺），次日 09:05 到，预算紧张时的选择"},
    ],
    ("上海", "邯郸"): [
        {"mode": "train", "code": "G1952", "depart": "08:10", "arrive": "14:35",
         "duration_min": 385, "price": 609,
         "note": f"高铁 上海虹桥→邯郸东（{DATA_RETRIEVED} 查询）"},
        {"mode": "train", "code": "G2812", "depart": "16:25", "arrive": "22:42",
         "duration_min": 377, "price": 598, "note": "高铁 傍晚出发"},
        {"mode": "train", "code": "K234", "depart": "11:10", "arrive": "04:06",
         "duration_min": 1016, "price": 261, "note": "普快 上海→邯郸 硬卧，次日 04:06 到"},
    ],
}

# 酒店为携程/同程在营房源（2026-10-29 查询起价），坐标为实际所在村落位置。
HOTELS_DB: dict[str, list[dict[str, Any]]] = {
    "大理": [
        {"hotel_id": "DALI-001", "name": "大理古城青青客栈", "area": "大理古城",
         "star": 2, "price": 77, "geo": [25.7005, 100.1630], "cancel_free": False,
         "dist_to_core_km": 0.6, "rating": 4.8, "note": "银苍路171号，起价"},
        {"hotel_id": "DALI-002", "name": "大理古城798客栈", "area": "大理古城",
         "star": 2, "price": 81, "geo": [25.6960, 100.1680], "cancel_free": False,
         "dist_to_core_km": 0.2, "rating": 4.8, "note": "玉洱路477号，白族风情"},
        {"hotel_id": "DALI-003", "name": "大理才村隐居小院", "area": "才村码头",
         "star": 2, "price": 132, "geo": [25.7080, 100.1900], "cancel_free": True,
         "dist_to_core_km": 2.8, "rating": 4.8, "note": "生态廊道才村码头店"},
        {"hotel_id": "DALI-004", "name": "李家大院民宿(才村码头店)", "area": "才村码头",
         "star": 2, "price": 161, "geo": [25.7065, 100.1910], "cancel_free": True,
         "dist_to_core_km": 3.0, "rating": 4.9, "note": "出门十几米就是洱海"},
        {"hotel_id": "DALI-005", "name": "非梵海景酒店(洱海店)", "area": "磻溪S湾",
         "star": 3, "price": 222, "geo": [25.7350, 100.1700], "cancel_free": True,
         "dist_to_core_km": 6.5, "rating": 4.5, "note": "S湾生态廊道上段，可看日出"},
        {"hotel_id": "DALI-006", "name": "洱海之境花园海景酒店(龙龛码头店)", "area": "龙龛码头",
         "star": 3, "price": 399, "geo": [25.6995, 100.1820], "cancel_free": True,
         "dist_to_core_km": 2.5, "rating": 4.9, "note": "龙龛码头舒适型热卖 No.1"},
        {"hotel_id": "DALI-007", "name": "大理无为普若海景民宿(才村码头店)", "area": "才村码头",
         "star": 3, "price": 420, "geo": [25.7075, 100.1890], "cancel_free": True,
         "dist_to_core_km": 3.0, "rating": 4.9, "note": "高档型，一线海景"},
        {"hotel_id": "DALI-008", "name": "大理古城八月客栈", "area": "大理古城",
         "star": 3, "price": 327, "geo": [25.6930, 100.1610], "cancel_free": True,
         "dist_to_core_km": 0.3, "rating": 4.9, "note": "洪武路月溪村，闹中取静"},
    ],
    "丽江": [
        {"hotel_id": "LJG-001", "name": "丽枫酒店(丽江大研古城店)", "area": "大研古城",
         "star": 3, "price": 260, "geo": [26.8740, 100.2330], "cancel_free": True,
         "dist_to_core_km": 0.5, "rating": 4.6, "note": "连锁品牌参考价"},
    ],
    # 邯郸/上海为连锁品牌在营门店（2026-10-29 淡季参考价，以预订平台实时价为准）
    "邯郸": [
        {"hotel_id": "HDG-001", "name": "邯郸丛台大酒店", "area": "丛台区",
         "star": 3, "price": 260, "geo": [36.6140, 114.4930], "cancel_free": True,
         "dist_to_core_km": 0.4, "rating": 4.6, "note": "中华北大街，近丛台公园"},
        {"hotel_id": "HDG-002", "name": "全季酒店(邯郸火车站店)", "area": "邯山区",
         "star": 3, "price": 210, "geo": [36.5960, 114.4770], "cancel_free": True,
         "dist_to_core_km": 1.8, "rating": 4.7, "note": "连锁品牌参考价"},
        {"hotel_id": "HDG-003", "name": "汉庭酒店(邯郸丛台公园店)", "area": "丛台区",
         "star": 2, "price": 150, "geo": [36.6150, 114.4940], "cancel_free": False,
         "dist_to_core_km": 0.3, "rating": 4.5, "note": "连锁品牌参考价"},
        {"hotel_id": "HDG-004", "name": "尚客优精选(邯郸学院北路店)", "area": "邯山区",
         "star": 2, "price": 110, "geo": [36.5850, 114.5000], "cancel_free": False,
         "dist_to_core_km": 2.5, "rating": 4.4, "note": "经济型参考价"},
    ],
    "上海": [
        {"hotel_id": "SHH-001", "name": "全季酒店(上海人民广场店)", "area": "人民广场",
         "star": 3, "price": 420, "geo": [31.2320, 121.4720], "cancel_free": True,
         "dist_to_core_km": 0.5, "rating": 4.7, "note": "连锁品牌淡季参考价"},
        {"hotel_id": "SHH-002", "name": "锦江之星品尚(上海南京路步行街店)", "area": "南京路",
         "star": 2, "price": 330, "geo": [31.2360, 121.4790], "cancel_free": False,
         "dist_to_core_km": 0.3, "rating": 4.5, "note": "连锁品牌淡季参考价"},
        {"hotel_id": "SHH-003", "name": "汉庭酒店(上海豫园店)", "area": "老城厢",
         "star": 2, "price": 280, "geo": [31.2240, 121.4870], "cancel_free": False,
         "dist_to_core_km": 0.6, "rating": 4.4, "note": "连锁品牌淡季参考价"},
        {"hotel_id": "SHH-004", "name": "宜必思酒店(上海豫园外滩店)", "area": "外滩",
         "star": 2, "price": 260, "geo": [31.2250, 121.4900], "cancel_free": False,
         "dist_to_core_km": 0.8, "rating": 4.3, "note": "经济型淡季参考价"},
    ],
}

# 门票/开放时间按同程、携程 2026-10-29 查询口径整理。
POIS_DB: dict[str, list[dict[str, Any]]] = {
    "大理": [
        {"poi_id": "P1", "name": "大理古城（人民路·复兴路）", "category": "attraction",
         "area": "大理古城", "geo": [25.6930, 100.1648], "appeal": 4,
         "duration_min": 150, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["古城", "夜市", "步行"], "intensity": "low",
         "indoor_alternative": "大理市博物馆"},
        {"poi_id": "P2", "name": "洱海生态廊道（才村段）", "category": "attraction",
         "area": "才村码头", "geo": [25.7070, 100.1870], "appeal": 5,
         "duration_min": 180, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["洱海", "骑行", "户外"], "intensity": "medium",
         "indoor_alternative": "周城扎染坊体验", "note": "廊道内禁机动车，租车约 30~50 元/天"},
        {"poi_id": "P3", "name": "崇圣寺三塔文化旅游区", "category": "attraction",
         "area": "古城北", "geo": [25.7120, 100.1470], "appeal": 4,
         "duration_min": 120, "cost": 75,
         "open_hours": "07:30-18:30", "open_days": "每天", "need_booking": False,
         "tags": ["古建", "人文"], "intensity": "low",
         "indoor_alternative": "三塔出土文物陈列馆", "note": "18:20 停止入园"},
        {"poi_id": "P4", "name": "喜洲古镇", "category": "attraction",
         "area": "喜洲", "geo": [25.8540, 100.1330], "appeal": 4,
         "duration_min": 120, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["古镇", "白族", "洱海"], "intensity": "low",
         "indoor_alternative": "严家大院（25 元）"},
        {"poi_id": "P5", "name": "苍山洗马潭索道", "category": "attraction",
         "area": "苍山", "geo": [25.6790, 100.1360], "appeal": 5,
         "duration_min": 240, "cost": 320,
         "open_hours": "08:30-16:00", "open_days": "每天", "need_booking": True,
         "tags": ["苍山", "高海拔", "户外"], "intensity": "high",
         "indoor_alternative": "寂照庵半日", "note": "全程往返 320 元含苍山门票，16:00 停止入园"},
        {"poi_id": "P6", "name": "海舌公园", "category": "attraction",
         "area": "喜洲", "geo": [25.8700, 100.1240], "appeal": 3,
         "duration_min": 60, "cost": 0,
         "open_hours": "09:00-18:00", "open_days": "每天", "need_booking": True,
         "tags": ["洱海", "摄影", "户外"], "intensity": "low",
         "indoor_alternative": "喜洲早市", "note": "免费但限流，需小程序提前一天预约"},
        {"poi_id": "P7", "name": "龙龛码头", "category": "attraction",
         "area": "龙龛码头", "geo": [25.7000, 100.1800], "appeal": 4,
         "duration_min": 60, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["洱海", "日出", "摄影"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P8", "name": "双廊古镇", "category": "attraction",
         "area": "双廊", "geo": [25.9500, 100.1890], "appeal": 4,
         "duration_min": 150, "cost": 0,
         "open_hours": "全天", "open_days": "每天", "need_booking": False,
         "tags": ["洱海", "古镇", "日落"], "intensity": "low",
         "indoor_alternative": "南诏风情岛（50 元）", "note": "距古城约 29km，适合顺环海西线一日"},
        {"poi_id": "P9", "name": "周城扎染坊", "category": "attraction",
         "area": "周城", "geo": [25.8890, 100.1290], "appeal": 3,
         "duration_min": 90, "cost": 40,
         "open_hours": "09:00-17:00", "open_days": "每天", "need_booking": False,
         "tags": ["非遗", "手作", "亲子"], "intensity": "low",
         "indoor_alternative": "", "note": "入园免费，扎染体验约 40 元"},
        {"poi_id": "P10", "name": "寂照庵", "category": "attraction",
         "area": "苍山", "geo": [25.6660, 100.1400], "appeal": 4,
         "duration_min": 120, "cost": 20,
         "open_hours": "08:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["素斋", "多肉", "清静"], "intensity": "low",
         "indoor_alternative": "", "note": "门票免费，斋饭约 20 元，11:30~13:00 供应"},
        {"poi_id": "P11", "name": "石井私房菜", "category": "restaurant",
         "area": "大理古城", "geo": [25.6925, 100.1655], "appeal": 4,
         "duration_min": 80, "cost": 80,
         "open_hours": "11:00-21:30", "open_days": "每天", "need_booking": False,
         "tags": ["本地菜", "可做不辣", "晚餐"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P12", "name": "梅子井酒家", "category": "restaurant",
         "area": "大理古城", "geo": [25.6945, 100.1640], "appeal": 4,
         "duration_min": 80, "cost": 75,
         "open_hours": "11:00-21:00", "open_days": "每天", "need_booking": False,
         "tags": ["白族菜", "可做不辣", "晚餐"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P13", "name": "喜洲破酥粑粑（四方街店）", "category": "restaurant",
         "area": "喜洲", "geo": [25.8535, 100.1345], "appeal": 3,
         "duration_min": 40, "cost": 20,
         "open_hours": "07:00-18:00", "open_days": "每天", "need_booking": False,
         "tags": ["小吃", "午餐", "不辣"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "P14", "name": "双廊酸辣鱼渔家", "category": "restaurant",
         "area": "双廊", "geo": [25.9490, 100.1900], "appeal": 4,
         "duration_min": 90, "cost": 90,
         "open_hours": "10:00-21:00", "open_days": "每天", "need_booking": False,
         "tags": ["本地菜", "晚餐", "酸辣鱼"], "intensity": "low",
         "indoor_alternative": ""},
    ],
    "丽江": [
        {"poi_id": "L1", "name": "大研古城", "category": "attraction", "area": "大研古城",
         "geo": [26.8721, 100.2357], "appeal": 4,
         "duration_min": 180, "cost": 0, "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["古城"], "intensity": "low",
         "indoor_alternative": "木府（40 元）", "note": "古城维护费 2019 年起已取消"},
        {"poi_id": "L2", "name": "束河古镇", "category": "attraction", "area": "束河",
         "geo": [26.9180, 100.2060], "appeal": 4,
         "duration_min": 120, "cost": 0, "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["古镇", "清静"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "L3", "name": "狮子山万古楼", "category": "attraction", "area": "大研古城",
         "geo": [26.8690, 100.2360], "appeal": 3,
         "duration_min": 60, "cost": 50, "open_hours": "08:00-19:00", "open_days": "每天",
         "need_booking": False, "tags": ["观景", "全景"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "L4", "name": "象山市场腊排骨", "category": "restaurant", "area": "大研古城",
         "geo": [26.8850, 100.2340], "appeal": 4,
         "duration_min": 90, "cost": 70, "open_hours": "10:00-22:00", "open_days": "每天",
         "need_booking": False, "tags": ["本地菜", "晚餐"], "intensity": "low",
         "indoor_alternative": ""},
    ],
    # 邯郸：门票按携程/同程/去哪儿 2026-10-29 查询口径（娲皇宫 68、北响堂 60、广府套票 50）
    "邯郸": [
        {"poi_id": "H1", "name": "丛台公园（武灵丛台）", "category": "attraction",
         "area": "丛台区", "geo": [36.6161, 114.4878], "appeal": 4,
         "duration_min": 90, "cost": 0,
         "open_hours": "全天（登丛台 08:00-17:30）", "open_days": "每天",
         "need_booking": False, "tags": ["赵国遗迹", "公园", "免费"], "intensity": "low",
         "indoor_alternative": "邯郸市博物馆"},
        {"poi_id": "H2", "name": "邯郸市博物馆", "category": "attraction",
         "area": "丛台区", "geo": [36.6100, 114.5169], "appeal": 4,
         "duration_min": 100, "cost": 0,
         "open_hours": "09:00-17:00", "open_days": "周二至周日（周一闭馆）",
         "need_booking": True, "tags": ["博物馆", "赵文化", "免费"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "H3", "name": "回车巷历史文化街区", "category": "attraction",
         "area": "串城街", "geo": [36.6064, 114.4799], "appeal": 3,
         "duration_min": 80, "cost": 0,
         "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["成语典故", "古街", "免费"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "H4", "name": "广府古城（永年）", "category": "attraction",
         "area": "永年区", "geo": [36.6993, 114.7261], "appeal": 5,
         "duration_min": 210, "cost": 50,
         "open_hours": "08:30-17:30", "open_days": "每天",
         "need_booking": False,
         "tags": ["5A", "水城", "太极之乡", "城墙"], "intensity": "medium",
         "indoor_alternative": "武禹襄故居",
         "note": "进城免费；城墙+广平府署+故居套票 50 元"},
        {"poi_id": "H5", "name": "娲皇宫（涉县）", "category": "attraction",
         "area": "涉县", "geo": [36.6412, 113.6108], "appeal": 5,
         "duration_min": 240, "cost": 68,
         "open_hours": "08:00-17:30", "open_days": "每天",
         "need_booking": False,
         "tags": ["5A", "女娲祭祀", "悬壁古建"], "intensity": "medium",
         "indoor_alternative": "朝元宫", "note": "市区车程约 1.5 小时，建议单独安排一天"},
        {"poi_id": "H6", "name": "北响堂山石窟", "category": "attraction",
         "area": "峰峰矿区", "geo": [36.5437, 114.0886], "appeal": 4,
         "duration_min": 180, "cost": 60,
         "open_hours": "08:30-17:30", "open_days": "每天",
         "need_booking": False,
         "tags": ["北齐石窟", "皇家石窟", "国宝"], "intensity": "medium",
         "indoor_alternative": "响堂山石窟艺术博物馆（19 元）",
         "note": "市区车程约 1 小时；南响堂 19 元可作替代"},
        {"poi_id": "H7", "name": "一篓油水饺（中华大街店）", "category": "restaurant",
         "area": "丛台区", "geo": [36.6040, 114.4890], "appeal": 4,
         "duration_min": 60, "cost": 40, "open_hours": "10:30-21:00", "open_days": "每天",
         "need_booking": False, "tags": ["老字号", "水饺"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "H8", "name": "老驴头驴肉火烧", "category": "restaurant",
         "area": "丛台区", "geo": [36.6120, 114.4860], "appeal": 4,
         "duration_min": 45, "cost": 25, "open_hours": "06:30-19:30", "open_days": "每天",
         "need_booking": False, "tags": ["驴肉火烧", "小吃"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "H9", "name": "圣旨骨酥鱼（滏东店）", "category": "restaurant",
         "area": "邯山区", "geo": [36.6090, 114.5390], "appeal": 3,
         "duration_min": 70, "cost": 70, "open_hours": "11:00-21:30", "open_days": "每天",
         "need_booking": False, "tags": ["骨酥鱼", "本地菜"], "intensity": "low",
         "indoor_alternative": ""},
    ],
    # 上海：门票按携程/同程 2026-10-29 查询口径（东方明珠 199、豫园 40）
    "上海": [
        {"poi_id": "S1", "name": "外滩", "category": "attraction",
         "area": "黄浦区", "geo": [31.2397, 121.4903], "appeal": 5,
         "duration_min": 90, "cost": 0,
         "open_hours": "全天（夜景最佳）", "open_days": "每天",
         "need_booking": False, "tags": ["万国建筑", "夜景", "免费"], "intensity": "low",
         "indoor_alternative": "外滩历史纪念馆"},
        {"poi_id": "S2", "name": "东方明珠", "category": "attraction",
         "area": "陆家嘴", "geo": [31.2397, 121.4998], "appeal": 4,
         "duration_min": 120, "cost": 199,
         "open_hours": "09:00-21:00", "open_days": "每天",
         "need_booking": True, "tags": ["地标", "观景塔"], "intensity": "low",
         "indoor_alternative": "上海城市历史发展陈列馆",
         "note": "二球联票 199 元起，节假日建议提前订"},
        {"poi_id": "S3", "name": "豫园", "category": "attraction",
         "area": "老城厢", "geo": [31.2272, 121.4922], "appeal": 4,
         "duration_min": 120, "cost": 40,
         "open_hours": "09:00-16:30（16:00 停止入园）", "open_days": "每天",
         "need_booking": False, "tags": ["江南园林", "明代", "城隍庙商圈"],
         "intensity": "medium", "indoor_alternative": "豫园商城（免费）"},
        {"poi_id": "S4", "name": "南京路步行街", "category": "attraction",
         "area": "黄浦区", "geo": [31.2354, 121.4802], "appeal": 4,
         "duration_min": 90, "cost": 0,
         "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["商业街", "百年老店", "免费"], "intensity": "low",
         "indoor_alternative": "第一百货"},
        {"poi_id": "S5", "name": "上海博物馆（人民广场馆）", "category": "attraction",
         "area": "人民广场", "geo": [31.2286, 121.4747], "appeal": 4,
         "duration_min": 120, "cost": 0,
         "open_hours": "09:00-17:00", "open_days": "周二至周日（周一闭馆）",
         "need_booking": True, "tags": ["青铜器", "书画", "免费"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "S6", "name": "田子坊", "category": "attraction",
         "area": "泰康路", "geo": [31.2104, 121.4666], "appeal": 3,
         "duration_min": 90, "cost": 0,
         "open_hours": "全天（店铺 10:00-22:00）", "open_days": "每天",
         "need_booking": False, "tags": ["弄堂", "文创", "免费"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "S7", "name": "陆家嘴滨江步道", "category": "attraction",
         "area": "浦东", "geo": [31.2390, 121.5010], "appeal": 4,
         "duration_min": 60, "cost": 0,
         "open_hours": "全天", "open_days": "每天",
         "need_booking": False, "tags": ["江景", "天际线", "免费"], "intensity": "low",
         "indoor_alternative": "浦东美术馆"},
        {"poi_id": "S8", "name": "南翔馒头店（豫园店）", "category": "restaurant",
         "area": "豫园", "geo": [31.2270, 121.4910], "appeal": 4,
         "duration_min": 60, "cost": 60, "open_hours": "09:00-21:00", "open_days": "每天",
         "need_booking": False, "tags": ["小笼包", "百年老店"], "intensity": "low",
         "indoor_alternative": ""},
        {"poi_id": "S9", "name": "绿波廊（豫园店）", "category": "restaurant",
         "area": "老城厢", "geo": [31.2267, 121.4905], "appeal": 4,
         "duration_min": 80, "cost": 150, "open_hours": "11:00-14:00, 17:00-20:30",
         "open_days": "每天", "need_booking": True, "tags": ["本帮菜", "老字号"],
         "intensity": "low", "indoor_alternative": ""},
        {"poi_id": "S10", "name": "老正兴菜馆（福州路店）", "category": "restaurant",
         "area": "黄浦区", "geo": [31.2340, 121.4780], "appeal": 3,
         "duration_min": 80, "cost": 120, "open_hours": "11:00-13:30, 16:30-20:00",
         "open_days": "每天", "need_booking": False, "tags": ["本帮菜", "米其林必比登"],
         "intensity": "low", "indoor_alternative": ""},
        {"poi_id": "S11", "name": "沈大成（南京路店）", "category": "restaurant",
         "area": "南京路", "geo": [31.2350, 121.4790], "appeal": 3,
         "duration_min": 45, "cost": 40, "open_hours": "07:00-20:00", "open_days": "每天",
         "need_booking": False, "tags": ["点心", "糕团", "老字号"], "intensity": "low",
         "indoor_alternative": ""},
    ],
}

# ---------------------------------------------------------------- 真实天气（Open-Meteo）


def _http_json(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "travel-agent-demo/1.0"})
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _fmt_desc(rain_prob: int, low: float, high: float) -> str:
    if rain_prob >= 60:
        return "可能有雨，备雨具"
    if rain_prob >= 30:
        return "多云，局地有雨"
    if low <= 5:
        return "晴冷，注意保暖"
    if high >= 26:
        return "晴热，注意防晒"
    return "晴到多云"


def _wx_forecast(geo: list[float], start: str, end: str) -> dict[str, dict] | None:
    """真实预报（Open-Meteo，实测可请求到「今天 + 15 天」）。

    越界是**整段 400**，不是少给最后一天：结束日期超窗时接口直接拒绝，
    `_http_json` 返回 None，调用方就把这四天全部回落成历史同期——用户看到的是
    "11/20 距今 14 天也标历史同期"，而前两天本来是能拿到真预报的。
    所以这里把 end 逐级往回收再试，宁可少几天预报，也不要整段静默降级。
    """
    lat, lon = geo
    try:
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
    except ValueError:
        return None
    today = date.today()
    tries = [e]
    for back in (15, 12, 10, 7, 5):
        c = today + timedelta(days=back)
        if s <= c < e:
            tries.append(c)
    for _end in tries:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
               f"&timezone=Asia%2FShanghai&start_date={s.isoformat()}&end_date={_end.isoformat()}")
        data = _http_json(url)
        if not data or "daily" not in data:
            continue
        d = data["daily"]
        out = {}
        for i, day in enumerate(d["time"]):
            out[day] = {
                "low": round(d["temperature_2m_min"][i]),
                "high": round(d["temperature_2m_max"][i]),
                "rain_prob": int(d["precipitation_probability_max"][i] or 0),
            }
        return out
    return None


def _wx_archive(geo: list[float], month: int, day_lo: int, day_hi: int) -> dict | None:
    """ERA5 历史同期（2023-2025）均值，用于超出预报窗口的日期。"""
    lat, lon = geo
    years = [2023, 2024, 2025]
    lows, highs, rainy, total = [], [], 0, 0
    for y in years:
        start = f"{y}-{month:02d}-{day_lo:02d}"
        end = f"{y}-{month:02d}-{day_hi:02d}"
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
               f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
               f"&timezone=Asia%2FShanghai&start_date={start}&end_date={end}")
        data = _http_json(url)
        if not data or "daily" not in data:
            continue
        d = data["daily"]
        lows += [v for v in d["temperature_2m_min"] if v is not None]
        highs += [v for v in d["temperature_2m_max"] if v is not None]
        rainy += sum(1 for v in d["precipitation_sum"] if v and v >= 1.0)
        total += len(d["time"])
    if not lows or not highs:
        return None
    return {
        "low": round(sum(lows) / len(lows)),
        "high": round(sum(highs) / len(highs)),
        "rain_prob": round(rainy / total * 100) if total else 20,
    }


def get_weather(city: str, start_date: str, end_date: str) -> dict[str, Any]:
    geo = resolve_geo(city)          # 本地城市表 + 地理编码兜底（问天气不该只认有 POI 的城市）
    if not geo:
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

    cache = _load_cache(WX_CACHE)
    # 缓存键带版本号：窗口口径改过一次（16→15 天、越界自愈），旧缓存里存着
    # "四天全是历史同期"的错误结果，不清掉会继续命中 6 小时。
    ckey = f"v2|{city}|{start_date}|{end_date}"
    if ckey in cache and time.time() - cache[ckey].get("_t", 0) < 6 * 3600:
        days = cache[ckey]["days"]
        return _ok(city=city, days=days, note=cache[ckey]["note"])

    # 预报窗口取「今天 + 15 天」——Open-Meteo 实测上限就在这里，写 16 会让
    # 请求越界整段失败（见 _wx_forecast 的说明），前几天也跟着变成历史数据。
    forecast_limit = date.today() + timedelta(days=15)
    days: list[dict[str, Any]] = []
    fc: dict[str, dict] = {}
    arc: dict | None = None
    have_fc = have_arc = False
    cur = start
    while cur <= end:
        iso = cur.isoformat()
        if cur <= forecast_limit:
            if iso not in fc and not have_fc:
                fc = _wx_forecast(geo, start_date, min(end, forecast_limit).isoformat()) or {}
                have_fc = True
            if iso in fc:
                v = fc[iso]
                days.append({"date": iso, **v, "desc": _fmt_desc(v["rain_prob"], v["low"], v["high"]),
                             "is_forecast": True})
            elif not days or days[-1]["is_forecast"]:
                # 预报窗口内但接口没给这一天（常见于窗口末尾）：回落到历史同期，
                # 不能留「暂无数据 0~0℃」——方案里那行看着像天气就是 0 度。
                if arc is None:
                    arc = _wx_archive(geo, cur.month, min(cur.day, 28), min(end.day, 28))
                if arc:
                    days.append({"date": iso, **arc,
                                 "desc": _fmt_desc(arc["rain_prob"], arc["low"], arc["high"])
                                         + "（历史同期）",
                                 "is_forecast": False})
                else:
                    clim = CITY_DB.get(city, {}).get("climate", {}).get(cur.month)
                    if clim:
                        days.append({"date": iso, **clim,
                                     "desc": _fmt_desc(clim["rain_prob"], clim["low"],
                                                       clim["high"]) + "（常年均值）",
                                     "is_forecast": False})
                    else:
                        days.append({"date": iso, "low": None, "high": None,
                                     "rain_prob": None, "desc": "待查",
                                     "is_forecast": False})
        else:
            if arc is None:
                arc = _wx_archive(geo, cur.month, min(cur.day, 28), min(end.day, 28))
            if arc:
                days.append({"date": iso, **arc,
                             "desc": _fmt_desc(arc["rain_prob"], arc["low"], arc["high"]),
                             "is_forecast": False})
            else:
                clim = CITY_DB.get(city, {}).get("climate", {}).get(cur.month)
                if clim:
                    days.append({"date": iso, **clim,
                                 "desc": _fmt_desc(clim["rain_prob"], clim["low"],
                                                   clim["high"]) + "（常年均值）",
                                 "is_forecast": False})
                else:
                    # 既没历史同期也没有常年均值：宁可写「待查」，也不能编 10~20℃
                    days.append({"date": iso, "low": None, "high": None,
                                 "rain_prob": None, "desc": "待查",
                                 "is_forecast": False})
        cur += timedelta(days=1)

    any_fc = any(d["is_forecast"] for d in days)
    all_fc = all(d["is_forecast"] for d in days)
    _fallback = any(("历史同期" in (d.get("desc") or "")
                     or "常年均值" in (d.get("desc") or "")) for d in days)
    if all_fc:
        note = "Open-Meteo 真实预报"
    elif any_fc:
        note = "前段为 Open-Meteo 预报，后段为历史同期/常年均值"
    elif _fallback or have_arc:
        note = "超出预报窗口：历史同期均值与常年气候，仅供参考"
    else:
        note = "暂无可靠天气数据，出行前请再查一次"

    cache[ckey] = {"_t": time.time(), "days": days, "note": note}
    _save_cache(WX_CACHE, cache)
    return _ok(city=city, days=days, note=note)


# ---------------------------------------------------------------- 城际交通（真实班次）


def search_transport(from_city: str, to_city: str, date: str,
                     transport_type: str = "any", party_size: int = 1) -> dict[str, Any]:
    options = _kb_schedules(from_city, to_city)
    if options is None:
        options = ROUTES_DB.get((from_city, to_city))
    if not options:
        return {"ok": False, "error": {"code": "NO_ROUTE",
                "message": f"暂无 {from_city} → {to_city} 的直达班次数据",
                "retryable": True}}
    if transport_type != "any":
        options = [o for o in options if o["mode"] == transport_type]
    items = [{**o, "total_price": o["price"] * party_size} for o in options]
    return _ok(from_city=from_city, to_city=to_city, date=date, items=items,
               note=f"班次为 12306/航司查询口径（{DATA_RETRIEVED}），票价随季节浮动")


# ---------------------------------------------------------------- 酒店（真实在售价）


def search_hotel(city: str, check_in: str, check_out: str, guests: int,
                 rooms: int = 1, max_price_per_night: float | None = None,
                 preferred_area: str | None = None, star_level: int | None = None) -> dict[str, Any]:
    hotels = _kb_hotels(city)
    if hotels is None:
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
    return _ok(items=[{**h, "rooms": rooms, "guests": guests} for h in result],
               note=f"在营房源起价（{DATA_RETRIEVED} 查询），节假日上浮 30%~50%")


# ---------------------------------------------------------------- POI（真实门票/时间/坐标）


def search_poi(city: str, keyword: str | None = None, category: str = "any",
               limit: int = 8, near: str | None = None) -> dict[str, Any]:
    # KB 优先：管理后端导入/编辑的城市立即可查；库不可用回落静态数据
    pois = _kb_pois(city)
    if pois is None:
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
    return _ok(items=result[:limit], total=len(result),
               note=f"门票与开放时间为 {DATA_RETRIEVED} 查询口径")


# ---------------------------------------------------------------- 预算与偏好（不变）


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


# ---------------------------------------------------------------- 真实路网耗时（OSRM）

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"
ROUTE_MEM: dict[str, tuple[float, float] | None] = {}


def _ckey(a: list[float], b: list[float]) -> str:
    return f"{round(a[0], 4)},{round(a[1], 4)}->{round(b[0], 4)},{round(b[1], 4)}"


def _osrm_drive(a: list[float], b: list[float]) -> tuple[float, float] | None:
    """真实驾车路径：(道路公里, 行车分钟)。失败返回 None（调用方自行降级）。"""
    key = _ckey(a, b)
    if key in ROUTE_MEM:
        return ROUTE_MEM[key]
    disk = _load_cache(ROUTE_CACHE)
    if key in disk:
        ROUTE_MEM[key] = (disk[key][0], disk[key][1])
        return ROUTE_MEM[key]
    try:
        url = (f"{OSRM_BASE}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=false")
        data = _http_json(url)
        if data and data.get("code") == "Ok":
            r = data["routes"][0]
            val = (round(r["distance"] / 1000, 1), round(r["duration"] / 60))
        else:
            val = None
    except Exception:
        val = None
    ROUTE_MEM[key] = val
    if val is not None:
        disk[key] = val
        _save_cache(ROUTE_CACHE, disk)
    return val


def transit_estimate(km: float, a: list[float] | None = None,
                     b: list[float] | None = None) -> tuple[str, int]:
    """交通方式与耗时（分钟）。

    给了起讫坐标 → 走 OSRM 真实路网（含真实道路绕行系数）；
    只给直线距离 → 按"道路系数 1.3"估算（用 OSRM 实测标定：
    大理古城→喜洲 直线 17.8km / 实驾 23.1km，系数约 1.3）。
    市内平均车速 25km/h，环海路 30km/h，含等车 6~8 分钟。
    """
    if a and b:
        real = _osrm_drive(a, b)
        if real is not None:
            road_km, drive_min = real
            if road_km <= 0.8:
                return "步行", int(road_km / 4.5 * 60) + 1
            return "打车", int(drive_min) + 6
    road = km * 1.3
    if road <= 0.8:
        return "步行", int(road / 4.5 * 60) + 1
    if road <= 3:
        return "打车", int(road / 25 * 60) + 8
    return "打车", int(road / 30 * 60) + 10


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
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(dispatch("get_weather", {
        "city": "大理", "start_date": "2026-11-08", "end_date": "2026-11-11"
    }), ensure_ascii=False, indent=2))
    km = haversine_km([25.6930, 100.1648], [25.8540, 100.1330])
    print(f"古城→喜洲 直线 {km}km；OSRM 估时：{transit_estimate(km, [25.6930, 100.1648], [25.8540, 100.1330])}")
