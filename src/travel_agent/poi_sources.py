"""POI 多源检索层：把「只能靠 Overpass」换成按稳定性排队的多源链。

## 为什么要换

Overpass 在国内基本不可用：2026-11-16 实测，`overpass-api.de` 与
`overpass.kumi.systems` 对「北京城区 tourism=attraction」这种小查询都要
21 秒才超时，`overpass.osm.ch` 返回 0 条（只有瑞士数据）。用户每问一座
没收录的城市就要干等 10 秒，然后拿到一句「这边没有数据」——这不是网络
抖动，是唯一数据源的选择问题。

## 现在的顺序（谁先有结果谁赢）

1. **高德 Web 服务**（需 `amap_web_key`，个人开发者免费额度足够）
   全国覆盖、POI 级精度、带评分/人均/营业时间，一次 25 条可翻 3 页。
   返回 GCJ-02，出口统一反算成 WGS-84 与库内点位同系（不反算会整体偏
   300~500 米，图钉和驾车连线都会画歪）。
2. **维基导游 Wikivoyage**（免 Key）
   中文旅游指南的真实景点清单，带简介/价格/开放时间/地址。实测坐标
   覆盖率高：上海 47/49、成都 28/28。条目按指南推荐顺序排列，正好当
   热度排序用；量少（一城 20~50 条）但质量高，适合行程规划。
3. **Overpass**（免 Key）——留在 autofetch 里当兜底，境外与长尾城市才可能命中。

## 边界

- 只产「原始行」（name/category/geo/...），字段校验仍由 autofetch._valid_row 把关；
  这里不写库、不做距离闸，避免两套规则打架。
- 任何源失败都只是返回空，不抛异常——上游链还要继续往下走。
- 宁可少给也不编：上游没给的字段就写清「未核实」，不用城市中心凑坐标。
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from .overseas import en_name

_UA = ("travel-agent/1.0 (+https://github.com/siatgus/travel-agent)")

# 只保留离城心这么远的点位（与 autofetch.LLM_MAX_KM 同口径）
MAX_KM = 60.0

CACHE_TTL = 6 * 3600          # 单源结果缓存：同一城市反复问不出网
DOWN_TTL = 600.0              # 上游异常熔断：10 分钟内不再试这一个源

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()
_DOWN: dict[str, float] = {}  # source -> 恢复时间戳


# ------------------------------------------------------------------ 公共工具


def _http_json(url: str, *, timeout: float, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    r = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _near(center: list[float], lat: float, lon: float) -> bool:
    return _km(center[0], center[1], lat, lon) <= MAX_KM


def _cache_get(key: str) -> list[dict] | None:
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return [dict(r) for r in hit[1]]
    return None


def _cache_put(key: str, rows: list[dict]) -> None:
    with _CACHE_LOCK:
        if len(_CACHE) > 400:
            _CACHE.clear()
        _CACHE[key] = (time.time(), [dict(r) for r in rows])


def _down(source: str) -> bool:
    return time.time() < _DOWN.get(source, 0.0)


def _mark_down(source: str) -> None:
    _DOWN[source] = time.time() + DOWN_TTL


def reset_cache() -> None:
    """测试用：清缓存与熔断状态。"""
    with _CACHE_LOCK:
        _CACHE.clear()
    _DOWN.clear()


# ------------------------------------------------------------------ 坐标系
#
# 高德返回 GCJ-02，库内点位是 WGS-84/CGCS2000。两者在景点级差 300~500 米，
# 直接入库会让「故宫」的图钉落在隔壁街区——与 render_html 里出图时的
# wgs2gcj 正好是逆向操作，参数必须同源，否则来回纠偏会漂。

_GCJ_PI = 3.14159265358979323846
_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _out_of_china(lng: float, lat: float) -> bool:
    return lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271


def _t_lat(x: float, y: float) -> float:
    from math import sin, sqrt
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * sqrt(abs(x)))
    ret += (20.0 * sin(6.0 * x * _GCJ_PI) + 20.0 * sin(2.0 * x * _GCJ_PI)) * 2.0 / 3.0
    ret += (20.0 * sin(y * _GCJ_PI) + 40.0 * sin(y / 3.0 * _GCJ_PI)) * 2.0 / 3.0
    ret += (160.0 * sin(y / 12.0 * _GCJ_PI) + 320 * sin(y * _GCJ_PI / 30.0)) * 2.0 / 3.0
    return ret


def _t_lng(x: float, y: float) -> float:
    from math import sin, sqrt
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
           + 0.1 * sqrt(abs(x)))
    ret += (20.0 * sin(6.0 * x * _GCJ_PI) + 20.0 * sin(2.0 * x * _GCJ_PI)) * 2.0 / 3.0
    ret += (20.0 * sin(x * _GCJ_PI) + 40.0 * sin(x / 3.0 * _GCJ_PI)) * 2.0 / 3.0
    ret += (150.0 * sin(x / 12.0 * _GCJ_PI) + 300.0 * sin(x / 30.0 * _GCJ_PI)) * 2.0 / 3.0
    return ret


def wgs2gcj(lng: float, lat: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（与 render_html 前端算法一致）。"""
    from math import cos, radians, sin, sqrt
    if _out_of_china(lng, lat):
        return lng, lat
    x, y = lng - 105.0, lat - 35.0
    d_lat, d_lng = _t_lat(x, y), _t_lng(x, y)
    rad = radians(lat)
    magic = 1 - _GCJ_EE * sin(rad) ** 2
    sm = sqrt(magic)
    d_lat = (d_lat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sm) * _GCJ_PI)
    d_lng = (d_lng * 180.0) / (_GCJ_A / sm * cos(rad) * _GCJ_PI)
    return lng + d_lng, lat + d_lat


def gcj2wgs(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS-84：迭代反算（3 轮收敛到厘米级，够景点用）。"""
    if _out_of_china(lng, lat):
        return lng, lat
    w_lng, w_lat = lng, lat
    for _ in range(3):
        g_lng, g_lat = wgs2gcj(w_lng, w_lat)
        w_lng -= g_lng - lng
        w_lat -= g_lat - lat
    return w_lng, w_lat


# ------------------------------------------------------------------ 高德


AMAP_TYPES = {"attraction": "110000", "restaurant": "050000", "hotel": "100000"}
# 高德 POI 分类码 → (游玩时长, 是否室内)
_AMAP_DUR = {
    "110100": (90, False),    # 公园广场
    "110200": (150, False),   # 动物园
    "110201": (120, False),   # 植物园
    "110202": (120, True),    # 水族馆
    "110203": (45, False),    # 城市广场
    "110205": (90, False),    # 名胜古迹
    "110206": (60, False),    # 寺庙道观
    "110207": (45, True),     # 教堂
    "110209": (90, False),    # 其它景点
}


def amap_key() -> str:
    """高德「Web 服务」Key：环境变量优先，其次设置文件 amap_web_key。

    注意与 amap_key（JS API，画图用）是两种类型，混用会返回 INVALID_USER_KEY，
    所以单独一个字段，不做互相兜底。
    """
    k = (os.environ.get("AMAP_WEB_KEY") or "").strip()
    if k:
        return k
    try:
        from . import llm as llm_layer
        return str(llm_layer.load_settings().get("amap_web_key") or "").strip()
    except Exception:
        return ""


def _amap_num(v) -> float | None:
    """高德把数字都塞在字符串里，缺失时是 []（空列表），统一成 float/None。"""
    try:
        if isinstance(v, (list, tuple)):
            return None
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _amap_row(p: dict, kind: str, city: str, center: list[float]) -> dict | None:
    """高德 POI → 库内行。坐标在这里完成 GCJ-02 → WGS-84。"""
    name = str(p.get("name") or "").strip()
    loc = str(p.get("location") or "")
    parts = loc.split(",")
    if not name or len(parts) != 2:
        return None
    try:
        g_lng, g_lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    lng, lat = gcj2wgs(g_lng, g_lat)
    if not _near(center, lat, lng):
        return None
    biz = p.get("biz_ext") or {}
    if isinstance(biz, list):
        biz = {}
    rating = _amap_num(biz.get("rating"))
    cost = _amap_num(biz.get("cost"))
    hours = str(biz.get("opentime2") or biz.get("opentime") or "").strip()
    area = str(p.get("adname") or "").strip()
    address = str(p.get("address") or "").strip()
    tel = str(p.get("tel") or "").strip()
    typecode = str(p.get("typecode") or "")

    if kind == "restaurant":
        tags = [t for t in str(p.get("type") or "").split(";") if t][-1:]
        note = "高德地图；人均为平台标注值，以到店实际消费为准"
        return {
            "name": name, "category": "restaurant", "area": area,
            "geo": [round(lat, 4), round(lng, 4)],
            "cost": int(cost) if cost else 0,
            "open_hours": hours or "营业时间未核实",
            "tags": tags or ["餐厅"],
            "note": (f"地址：{address}；{note}" if address else note),
        }

    if kind == "hotel":
        price = int(cost) if cost and cost > 0 else 0
        note = ("高德地图；房价为平台参考价，非实时房态"
                if price else "高德地图；房价未核实，以预订页为准")
        return {
            "name": name, "area": area or city,
            "star": 3, "price": price,
            "geo": [round(lat, 4), round(lng, 4)],
            "cancel_free": False,
            "dist_to_core_km": round(_km(center[0], center[1], lat, lng), 1),
            "rating": round(rating, 1) if rating else 4.3,
            "note": note,
        }

    dur, indoor = _AMAP_DUR.get(typecode[:6], (90, False))
    appeal = 4 if (rating or 0) >= 4.5 else 3
    who = str(p.get("type") or "").split(";")[-1:] or ["景点"]
    return {
        "name": name, "category": "attraction", "area": area or city,
        "geo": [round(lat, 4), round(lng, 4)],
        "appeal": appeal, "duration_min": dur, "cost": 0,
        "open_hours": hours or "全天", "open_days": "每天",
        "need_booking": False, "tags": [who[0]],
        "intensity": "medium" if dur >= 150 else "low",
        "indoor_alternative": "",
        "note": ("高德地图；门票未提供，请以景区公告为准"
                 + (f"；地址：{address}" if address else "")
                 + (f"；电话：{tel}" if tel else "")),
    }


def search_amap(city: str, center: list[float], kind: str = "attraction",
                *, pages: int = 3, page_size: int = 25,
                timeout: float = 6.0) -> list[dict]:
    """高德分类检索：按 types 取该城全部景点/餐饮/住宿，最多 pages 页。"""
    key = amap_key()
    if not key:
        return []
    types = AMAP_TYPES.get(kind)
    if not types:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        params = urllib.parse.urlencode({
            "key": key, "types": types, "city": city, "citylimit": "true",
            "offset": str(page_size), "page": str(page), "extensions": "all",
            "output": "json",
        })
        url = f"https://restapi.amap.com/v3/place/text?{params}"
        try:
            data = _http_json(url, timeout=timeout)
        except Exception:
            _mark_down("amap") if page == 1 else None
            break
        if str(data.get("status")) != "1":
            info = str(data.get("info") or "")
            # 配额/Key 问题直接熔断，别对每一页都问一遍
            if "LIMIT" in info or "KEY" in info or "QUOTA" in info:
                _mark_down("amap")
            break
        pois = data.get("pois") or []
        if not pois:
            break
        for p in pois:
            row = _amap_row(p, kind, city, center)
            if row and row["name"] not in seen:
                seen.add(row["name"])
                out.append(row)
        if len(pois) < page_size:
            break
    return out


def search_amap_keyword(city: str, center: list[float], keyword: str,
                        kind: str = "restaurant", *,
                        limit: int = 10, timeout: float = 6.0) -> list[dict]:
    """高德关键词检索：按「烤鸭/火锅/川菜」这类词搜真实门店。

    与 search_amap（按 types 分类拉全城）不同，这里走 place/text 的
    keywords 参数——具体菜品/品类词（烤鸭、生煎、日料）只有它能命中。
    关键词检索不传 types（会互相打架），只限 citylimit 保证不跨城。
    """
    key = amap_key()
    if not key or not keyword or not city or not center:
        return []
    params = urllib.parse.urlencode({
        "key": key, "keywords": keyword, "city": city, "citylimit": "true",
        "offset": str(min(limit, 25)), "page": "1",
        "extensions": "all", "output": "json",
    })
    try:
        data = _http_json(f"https://restapi.amap.com/v3/place/text?{params}",
                          timeout=timeout)
    except Exception:
        _mark_down("amap")
        return []
    if str(data.get("status")) != "1":
        info = str(data.get("info") or "")
        if "LIMIT" in info or "KEY" in info or "QUOTA" in info:
            _mark_down("amap")
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for p in data.get("pois") or []:
        row = _amap_row(p, kind, city, center)
        if row and row["name"] not in seen:
            seen.add(row["name"])
            out.append(row)
    return out


def find_spot(city: str, name: str, center: list[float],
              *, timeout: float = 6.0) -> dict | None:
    """按名字在高德找单个景点（用户点名补全用）。

    命中判定与 autofetch 同口径：完全相等 > 双向包含 > 放弃。同名多处时取
    离城心最近的那个——「温泉」这种泛名在别的城市也有，不能跨城乱配。
    """
    key = amap_key()
    if not key or not name or not city:
        return None
    params = urllib.parse.urlencode({
        "key": key, "keywords": name, "city": city, "citylimit": "true",
        "offset": "10", "page": "1", "extensions": "all", "output": "json",
    })
    try:
        data = _http_json(f"https://restapi.amap.com/v3/place/text?{params}",
                          timeout=timeout)
    except Exception:
        _mark_down("amap")
        return None
    if str(data.get("status")) != "1":
        return None
    best: dict | None = None
    best_score = -1.0
    for p in data.get("pois") or []:
        nm = str(p.get("name") or "")
        if name == nm:
            score = 3.0
        elif name in nm or (nm and nm in name):
            score = 2.0
        else:
            continue
        row = _amap_row(p, "attraction", city, center)
        if not row:
            continue
        dist_km = _km(center[0], center[1], row["geo"][0], row["geo"][1])
        score -= dist_km / 1000.0             # 同分时近者优先
        if score > best_score:
            best, best_score = row, score
    return best


# ------------------------------------------------------------------ 维基导游
_WIKI_LANGS = ("zh", "en")
_WIKI_HOST = "https://{lang}.wikivoyage.org/w/api.php?{params}"
_WIKI_LAST = 0.0                 # 全局节流：Wikimedia 对连续请求直接 429
_WIKI_GAP = 1.2
_WIKI_TTL = 6 * 3600
_WIKI: dict[tuple[str, str], tuple[float, str] | None] = {}

# 模板名 → 数据类别。listing 用 type= 参数区分。
_WIKI_KIND = {"see": "attraction", "do": "attraction", "listing": "",
              "eat": "restaurant", "sleep": "hotel"}
# 名称/正文关键词 → (时长, 室内)。顺序即优先级。
_WIKI_DUR = (
    ("主题乐园", 300, False), ("动物园", 150, False), ("植物园", 120, False),
    ("水族馆", 120, True), ("博物馆", 120, True), ("美术馆", 90, True),
    ("纪念馆", 60, True), ("展览馆", 90, True), ("古镇", 180, False),
    ("古城", 180, False), ("寺", 60, False), ("庙", 45, False),
    ("宫", 90, False), ("塔", 45, False), ("园", 90, False),
    ("山", 180, False), ("湖", 120, False), ("街", 60, False),
    ("广场", 45, False), ("桥", 30, False), ("温泉", 150, True),
)


def _wiki_text(city: str) -> tuple[str, str] | None:
    """取城市页 wikitext，返回 (text, lang)；没有页面返回 None。

    zh 优先，没页面（如东京在中文站没有条目）再退 en——境外城市英文站更全。
    同一城市 6 小时内只取一次，且请求之间强制间隔（429 是硬限流）。
    """
    for lang in _WIKI_LANGS:
        ck = (lang, city)
        if ck in _WIKI:
            hit = _WIKI[ck]
            if hit and time.time() - hit[0] < _WIKI_TTL:
                return hit[1], lang
            if hit is None:
                continue
        # 英文站页名是英文：拿中文名查 en 只会 404，境外城市必然取不到
        page = city if lang == "zh" else en_name(city)
        global _WIKI_LAST
        wait = _WIKI_LAST + _WIKI_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _WIKI_LAST = time.time()
        params = urllib.parse.urlencode({
            "action": "parse", "page": page, "prop": "wikitext",
            "format": "json", "formatversion": "2", "redirects": "1",
        })
        try:
            data = _http_json(_WIKI_HOST.format(lang=lang, params=params),
                              timeout=8.0)
        except Exception:
            continue
        if "error" in data:
            _WIKI[ck] = None
            continue
        text = str(((data.get("parse") or {}).get("wikitext")) or "")
        if not text:
            _WIKI[ck] = None
            continue
        _WIKI[ck] = (time.time(), text)
        return text, lang
    return None


def _iter_templates(text: str):
    """按嵌套深度切出顶层模板体（`{{see|...}}` → `see|...`）。"""
    i = 0
    n = len(text)
    while i < n - 1:
        start = text.find("{{", i)
        if start < 0:
            return
        depth, j = 0, start
        while j < n - 1:
            if text.startswith("{{", j):
                depth += 1
                j += 2
            elif text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
            else:
                j += 1
        if depth != 0:
            return
        yield text[start + 2:j - 2]
        i = j


def _split_params(body: str) -> list[str]:
    """按顶层 `|` 切分模板参数（`{{...}}`/`[[...]]` 内部的竖线不算）。"""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    i, n = 0, len(body)
    while i < n:
        if body.startswith("{{", i) or body.startswith("[[", i):
            depth += 1
            buf.append(body[i:i + 2])
            i += 2
            continue
        if body.startswith("}}", i) or body.startswith("]]", i):
            depth -= 1
            buf.append(body[i:i + 2])
            i += 2
            continue
        ch = body[i]
        if ch == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _clean(s: str) -> str:
    """去 wiki 标记与 HTML：`[[链接|显示]]` 留显示，`<ref>` 去掉，压空白。"""
    s = s or ""
    s = re.sub(r"<ref[^>/]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]*)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"'''?", "", s)
    s = _html.unescape(s)
    # 图片语法被吃掉后常留下「对象替换符」U+FFFC（实测「￼开元寺」），
    # 零宽字符同理：留着会让 name_hit 的双向包含永远匹配不上用户输入
    s = re.sub(r"[\ufffc\u200b-\u200f\ufeff\u00a0]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# 中文维基导游的编者以港台为主，条目名常是繁体（「龍門石窟」「開元寺」），
# 而用户和库内数据都是简体——不做归一，双向包含一条都匹配不上，用户点名
# 「龙门石窟」会被判成"库里没有"而重新入库一条繁体同景点。这里只收地名/
# 景点高频字，够覆盖绝大多数条目，不做完整繁简转换（那要引入词表依赖）。
_T2S_PAIRS = (
    "龍龙 門门 開开 關关 東东 華华 國国 廣广 寧宁 蘇苏 蘭兰 雲云 貴贵 陝陕 "
    "遼辽 內内 豐丰 樂乐 鄉乡 觀观 瀾澜 漢汉 黃黄 馬马 鳳凤 鶴鹤 嶽岳 瀋沈 "
    "齊齐 魯鲁 贛赣 閩闽 粵粤 瓊琼 隴陇 銀银 鐵铁 錫锡 銅铜 湯汤 溝沟 澗涧 "
    "灣湾 嶼屿 島岛 巖岩 嶺岭 峽峡 閣阁 壇坛 寶宝 靈灵 聖圣 賢贤 鹽盐 監监 "
    "舊旧 疊叠 參参 雙双 雞鸡 鳥鸟 陽阳 陰阴 濟济 慶庆 興兴 義义 鎮镇 莊庄 "
    "臺台 萬万 與与 為为 學学 廳厅 園园 圓圆 圖图 樓楼 橋桥 腳脚 墳坟 廟庙 "
    "宮宫 縣县 區区 樹树 葉叶 邊边 闊阔 滿满 長长 見见 現现 實实 產产 業业 "
    "點点 線线 車车 場场 機机 電电 話话 價价 錢钱 費费 時时 間间 遊游 覽览 "
    "館馆 藝艺 術术 劇剧 團团 隊队 師师 傳传 統统 歷历 羅罗 兒儿 頭头 體体 "
    "髮发 濟济 遼辽 滬沪 渝渝 濱滨 陰阴 陘陉 "
    "頤颐 灤滦 瀘泸 灕漓 瀟潇 濰潍 瀏浏 漣涟 嶗崂 嶧峄 灃沣 潯浔 淶涞 涇泾 "
    "鄖郧 鄴邺 澮浍 濼泺"
)
_T2S = str.maketrans({p[0]: p[1] for p in _T2S_PAIRS.split() if len(p) == 2})


def _t2s(s: str) -> str:
    return (s or "").translate(_T2S)


def _price_of(s: str) -> int | None:
    """从 price/content 里提门票价：`¥60`、`60元`、`门票60`、`免费` → 0。"""
    s = s or ""
    if re.search(r"免费|不需门票|无须门票|免票", s):
        return 0
    m = re.search(r"[¥￥]\s*(\d{1,4})|(\d{1,4})\s*元|门票\D{0,6}(\d{1,4})", s)
    if not m:
        return None
    val = next((g for g in m.groups() if g), None)
    try:
        return int(val) if val is not None else None
    except ValueError:
        return None


def _wiki_duration(name: str, content: str) -> tuple[int, bool]:
    blob = f"{name} {content}"
    for kw, dur, indoor in _WIKI_DUR:
        if kw in blob:
            return dur, indoor
    return 90, False


def _wiki_row(name: str, params: dict, kind: str, city: str,
              center: list[float], order: int) -> dict | None:
    name = _t2s(_clean(name))
    if len(name) < 2:
        return None
    try:
        lat = float(str(params.get("lat") or "").strip())
        lon = float(str(params.get("long") or params.get("lon") or "").strip())
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    if not _near(center, lat, lon):
        return None
    content = _clean(str(params.get("content") or ""))
    hours = _clean(str(params.get("hours") or ""))
    price_src = _clean(str(params.get("price") or "")) + " " + content
    price = _price_of(price_src)
    address = _clean(str(params.get("address") or ""))
    phone = _clean(str(params.get("phone") or ""))
    site = "维基导游（CC BY-SA 4.0）"

    if kind == "restaurant":
        note = f"{site}；人均未提供，以到店实际消费为准"
        return {
            "name": name, "category": "restaurant", "area": city,
            "geo": [round(lat, 4), round(lon, 4)],
            "cost": price if price and price < 2000 else 0,
            "open_hours": hours or "营业时间未核实",
            "tags": ["餐厅"], "note": note,
        }
    if kind == "hotel":
        return {
            "name": name, "area": city, "star": 3,
            "price": price if price and price < 20000 else 350,
            "geo": [round(lat, 4), round(lon, 4)],
            "cancel_free": False,
            "dist_to_core_km": round(_km(center[0], center[1], lat, lon), 1),
            "rating": 4.3,
            "note": f"{site}；"
                    + ("房价为指南标注值" if price else "房价未提供，按中档酒店参考价估算"),
        }

    dur, indoor = _wiki_duration(name, content)
    # 指南条目顺序本身就是推荐顺序：越靠前越值得去，直接当热度用
    appeal = 5 if order < 5 else (4 if order < 15 else 3)
    note = site
    if content:
        note += "；" + content[:120]
    if address:
        note += f"；地址：{address}"
    if phone:
        note += f"；电话：{phone}"
    return {
        "name": name, "category": "attraction", "area": city,
        "geo": [round(lat, 4), round(lon, 4)],
        "appeal": appeal, "duration_min": dur,
        "cost": price if price is not None and price < 2000 else 0,
        "open_hours": hours or "全天", "open_days": "每天",
        "need_booking": price is not None and price > 100,
        "tags": ["景点"],
        "intensity": "high" if dur >= 240 else ("medium" if dur >= 120 else "low"),
        "indoor_alternative": "",
        "note": note,
    }


def search_wikivoyage(city: str, center: list[float], kind: str = "attraction",
                      *, timeout: float = 8.0) -> list[dict]:
    """从维基导游城市页抽景点/餐饮/住宿清单（免 Key，中文优先）。"""
    got = _wiki_text(city)
    if not got:
        return []
    text, _lang = got
    out: list[dict] = []
    seen: set[str] = set()
    for body in _iter_templates(text):
        parts = _split_params(body)
        if not parts:
            continue
        tpl = parts[0].strip().lower()
        if tpl not in _WIKI_KIND:
            continue
        params: dict[str, str] = {}
        for p in parts[1:]:
            k, sep, v = p.partition("=")
            if sep:
                params[k.strip().lower()] = v.strip()
        row_kind = _WIKI_KIND[tpl]
        if tpl == "listing":
            row_kind = {"see": "attraction", "do": "attraction",
                        "eat": "restaurant", "drink": "restaurant",
                        "sleep": "hotel"}.get(
                            str(params.get("type") or "").strip().lower(), "")
        if row_kind != kind:
            continue
        name = str(params.get("name") or params.get("title") or "")
        row = _wiki_row(name, params, kind, city, center, len(out))
        if row and row["name"] not in seen:
            seen.add(row["name"])
            out.append(row)
    return out


# ------------------------------------------------------------------ 统一入口


def search(city: str, center: list[float], kind: str = "attraction",
           *, limit: int = 60) -> tuple[list[dict], str]:
    """按优先级取一批 POI：高德（有 Key）→ 维基导游。

    返回 (rows, source)，source 为 "" 表示两个源都没给出可用结果——调用方
    继续往 Overpass/模型降级，绝不在这里编数据。
    """
    if not city or not center:
        return [], ""
    ck = f"{kind}|{city}"
    hit = _cache_get(ck)
    if hit:
        return hit[:limit], "cache"
    for name, fn in (("amap", search_amap), ("wikivoyage", search_wikivoyage)):
        if _down(name):
            continue
        try:
            rows = fn(city, center, kind)
        except Exception:
            _mark_down(name)
            continue
        if rows:
            _cache_put(ck, rows)
            return rows[:limit], name
    return [], ""


def describe() -> dict:
    """给后台/诊断用：各源当前是否可用。"""
    return {
        "amap": {"configured": bool(amap_key()), "down": _down("amap")},
        "wikivoyage": {"configured": True, "down": _down("wikivoyage")},
    }
