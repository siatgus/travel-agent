"""城市自动补全测试：知识库没有的城市，在线查一次并入库，然后正常排行程。

需求（2026-11-02）：用户提到资料库里没有的城市时，助手应自己把该城市的信息
补进知识库，而不是回一句"我这边没有数据"把人推走。

本文件全部离线（数据源用注入的假抓取器），覆盖：
1. 省份/大区不触发抓取（云南能被地理编码解析成 ADM1，按省会中心抓一圈会把整省拼成一座城）
2. 已有数据的城市直接返回 exists，不打网络
3. 抓到 → 写 POI/酒店/城市坐标 + 落 sync_log + 进入可服务城市列表
4. 抓到之后第二次调用不再重复抓
5. 抓取失败 → failed + 失败缓存（同一城市短期内不重试）+ 失败日志
6. 地名解析不出坐标 → unresolved
7. 开关关闭 → disabled
8. 字段映射：fee=yes 才收门票、wikidata 提高吸引力、酒店星级→估算房价
9. 模型兜底通道：坐标超出城区 60km 的一律丢弃（防幻觉）
10. 对话层：陌生城市 → 自动补全 → 出方案（景点真的排进去）→ 方案里写明数据来源

运行：python test_autofetch.py    失败时退出码为 1。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

_tmp = Path(tempfile.mkdtemp(prefix="travel_af_test_"))
os.environ["TRAVEL_KB_DB"] = str(_tmp / "kb.db")     # 必须在 import kb 之前设置

from travel_agent import autofetch, kb, llm                     # noqa: E402
from travel_agent import real_tools                             # noqa: E402
from travel_agent.dialogue import Session                       # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


CENTER = [30.00, 120.00]          # 假城市中心（绍兴附近，与任何种子城市无关）
CITY = "测试城"
# main() 会把 tdt_geocode 整体换成桩；境外兜底测试要打真实现，先留档
_REAL_TDT_GEOCODE = autofetch.tdt_geocode


def _poi(name: str, lat: float, lon: float, **kw) -> dict:
    base = {"name": name, "category": "attraction", "area": CITY,
            "geo": [lat, lon], "appeal": 3, "duration_min": 90, "cost": 0,
            "open_hours": "08:30-17:30", "open_days": "每天", "need_booking": False,
            "tags": ["attraction"], "intensity": "low", "indoor_alternative": "",
            "note": ""}
    base.update(kw)
    return base


def fake_fetcher(city, center):
    return ([_poi(f"{city}古城", 30.01, 120.01),
             _poi(f"{city}博物馆", 30.03, 120.02),
             _poi(f"{city}山", 30.10, 120.06)],
            [{"name": f"{city}大酒店", "area": "市中心", "star": 4, "price": 520,
              "geo": [30.02, 120.01], "cancel_free": True,
              "dist_to_core_km": 1.2, "rating": 4.6, "note": ""},
             {"name": f"{city}客栈", "area": "老街", "star": 3, "price": 300,
              "geo": [30.00, 120.03], "cancel_free": False,
              "dist_to_core_km": 0.8, "rating": 4.4, "note": ""}])


def _patch_center(value):
    """临时替换坐标解析（避免测试打地理编码接口）。"""
    old = autofetch._resolve_center
    autofetch._resolve_center = lambda city: value
    return lambda: setattr(autofetch, "_resolve_center", old)


# ------------------------------------------------------------------ 用例


def test_plausible_city() -> None:
    check("省份不当城市抓取", not autofetch.plausible_city("云南")
          and not autofetch.plausible_city("新疆"))
    check("大区/后缀不当城市抓取", not autofetch.plausible_city("华北")
          and not autofetch.plausible_city("某某自治区"))
    check("正常城市名通过", autofetch.plausible_city("淄博")
          and autofetch.plausible_city("香格里拉"))


def test_existing_city_no_fetch() -> None:
    def boom(*_a, **_k):
        raise AssertionError("已有数据的城市不该触发抓取")
    r = autofetch.ensure_city("大理", fetcher=boom)
    check("已有城市返回 exists 且不抓取", r["status"] == "exists", f"{r}")


def test_fetch_and_store() -> None:
    autofetch.reset_cache()
    restore = _patch_center(CENTER)
    try:
        r = autofetch.ensure_city(CITY, fetcher=fake_fetcher)
    finally:
        restore()
    db = kb.get_kb()
    check("陌生城市抓取成功", r["status"] == "fetched" and r["pois"] == 3
          and r["hotels"] == 2, f"{r}")
    check("POI 写入知识库", len(db.list_pois(CITY) or []) == 3)
    check("酒店写入知识库", len(db.list_hotels(CITY) or []) == 2)
    check("城市登记了中心坐标", any(c == CITY for c in db.cities()))
    check("进入可服务城市列表（对话层可见）",
          CITY in __import__("travel_agent.dialogue", fromlist=["x"]).serviceable_cities())
    log = db.logs(1)[0]
    check("同步日志落库", log["city"] == CITY and log["source"] == "auto-fetch"
          and log["status"] == "success", f"{log}")


def test_second_call_is_noop() -> None:
    calls = {"n": 0}

    def counting(city, center):
        calls["n"] += 1
        return fake_fetcher(city, center)

    restore = _patch_center(CENTER)
    try:
        r = autofetch.ensure_city(CITY, fetcher=counting)
    finally:
        restore()
    check("同一城市不重复抓取", r["status"] == "exists" and calls["n"] == 0, f"{r}")


def test_failure_and_cache() -> None:
    autofetch.reset_cache()
    calls = {"n": 0}

    def broken(city, center):
        calls["n"] += 1
        raise ConnectionError("上游超时")

    restore = _patch_center(CENTER)
    try:
        r1 = autofetch.ensure_city("失败城", fetcher=broken)
        r2 = autofetch.ensure_city("失败城", fetcher=broken)
    finally:
        restore()
    check("抓取失败如实返回 failed", r1["status"] == "failed" and "上游超时" in r1["message"])
    check("失败结果被缓存，短期不重试", calls["n"] == 1 and r2.get("cached") is True,
          f"calls={calls['n']} r2={r2}")
    log = kb.get_kb().logs(1)[0]
    check("失败也落日志", log["city"] == "失败城" and log["status"] == "failed")


def test_unresolved_and_disabled() -> None:
    autofetch.reset_cache()
    restore = _patch_center(None)
    try:
        r = autofetch.ensure_city("无坐标城", fetcher=fake_fetcher)
    finally:
        restore()
    check("解析不出坐标 → unresolved", r["status"] == "unresolved", f"{r}")

    old = autofetch.ENABLED
    autofetch.ENABLED = False
    try:
        r = autofetch.ensure_city("关闭城", fetcher=fake_fetcher)
    finally:
        autofetch.ENABLED = old
    check("开关关闭 → disabled 且不抓取", r["status"] == "disabled", f"{r}")


def test_field_mapping() -> None:
    free = autofetch._poi("免费公园", "attraction", {"tourism": "attraction"},
                          CITY, 30.0, 120.0)
    paid = autofetch._poi("收费景区", "attraction", {"tourism": "attraction", "fee": "yes"},
                          CITY, 30.0, 120.0)
    famous = autofetch._poi("著名博物馆", "museum", {"tourism": "museum", "wikidata": "Q1"},
                            CITY, 30.0, 120.0)
    check("免费景点门票为 0", free["cost"] == 0)
    check("标注收费的景点收门票", paid["cost"] > 0)
    check("有 wikidata 的景点吸引力更高", famous["appeal"] >= 4)
    check("景点统一归为 attraction（编排器只认这个类别）",
          free["category"] == paid["category"] == famous["category"] == "attraction")

    h5 = autofetch._hotel("五星", {"stars": "5"}, CENTER, 30.02, 120.02)
    h3 = autofetch._hotel("三星", {"stars": "3"}, CENTER, 30.02, 120.02)
    check("酒店房价按星级估算", h5["price"] == 900 and h3["price"] == 350, f"{h5['price']}/{h3['price']}")
    check("酒店标注估算来源", "估算" in h5["note"])
    check("酒店距市中心按坐标算", 0 < h5["dist_to_core_km"] < 5, f"{h5['dist_to_core_km']}")


def test_llm_fallback_filters_hallucination() -> None:
    """模型给的坐标离城区太远 → 丢弃（这是防幻觉的唯一硬手段）。"""
    payload = {"pois": [
        {"name": "城中景点甲", "category": "attraction", "duration_min": 90,
         "ticket": 0, "lat": 30.01, "lon": 120.01},
        {"name": "城中景点乙", "category": "museum", "duration_min": 120,
         "ticket": 40, "lat": 30.03, "lon": 120.03},
        {"name": "邻市景点丙", "category": "attraction", "duration_min": 90,
         "ticket": 0, "lat": 30.10, "lon": 120.05},
        {"name": "错到外省丁", "category": "attraction", "duration_min": 90,
         "ticket": 0, "lat": 39.90, "lon": 116.40},
    ]}
    client = llm.ScriptedClient([json.dumps(payload, ensure_ascii=False)])
    pois, hotels = autofetch.fetch_city_llm(client, CITY, CENTER)
    names = [p["name"] for p in pois]
    check("模型兜底：本城景点保留", "城中景点甲" in names and "城中景点乙" in names, f"{names}")
    check("模型兜底：外省坐标被丢弃", "错到外省丁" not in names, f"{names}")
    check("模型兜底：不返回酒店（没有可靠报价）", hotels == [])
    check("模型兜底：标注坐标为近似值",
          all("近似" in p["note"] for p in pois))

    too_few = json.dumps({"pois": [{"name": "唯一景点", "lat": 30.0, "lon": 120.0},
                                   {"name": "x", "lat": 0, "lon": 0}]},
                         ensure_ascii=False)
    r, _rec = llm.discover_pois(llm.ScriptedClient([too_few]), CITY)
    check("有效景点不足 3 个视为失败", r is None)


def test_source_chain_osm_then_llm() -> None:
    """Overpass 不可达 → 自动落到模型通道，结果标记 source=llm。"""
    autofetch.reset_cache()
    old_fetch = autofetch.fetch_city

    def broken(city, center):
        raise ConnectionError("Overpass 全部镜像不可达")

    payload = json.dumps({"pois": [
        {"name": "落底景点甲", "category": "attraction", "duration_min": 90,
         "ticket": 0, "lat": 30.01, "lon": 120.01},
        {"name": "落底景点乙", "category": "attraction", "duration_min": 90,
         "ticket": 20, "lat": 30.02, "lon": 120.02},
        {"name": "落底景点丙", "category": "park", "duration_min": 120,
         "ticket": 0, "lat": 30.04, "lon": 120.04},
    ]}, ensure_ascii=False)
    client = llm.ScriptedClient([payload])
    restore = _patch_center(CENTER)
    autofetch.fetch_city = broken
    try:
        r = autofetch.ensure_city("落底城", client=client)
    finally:
        autofetch.fetch_city = old_fetch
        restore()
    check("OSM 失败后走模型兜底", r["status"] == "fetched" and r["source"] == "llm", f"{r}")
    check("兜底结果已入库", len(kb.get_kb().list_pois("落底城") or []) == 3)


def test_session_end_to_end() -> None:
    """陌生城市 → 自动补全 → 直接出方案，景点要真的排进去。"""
    autofetch.reset_cache()
    city = "样例城"                      # 与前面用例的城市区分，避免命中已入库数据
    old_fetch = autofetch.fetch_city
    old_weather = real_tools.TOOL_REGISTRY.get("get_weather")
    autofetch.fetch_city = lambda c, center: fake_fetcher(c, center)

    def fake_weather(city_name, start_date=None, end_date=None):
        return {"ok": True, "city": city_name, "days": [
            {"date": "2026-12-01", "low": 4, "high": 12, "rain_prob": 10, "desc": "晴"},
            {"date": "2026-12-02", "low": 5, "high": 13, "rain_prob": 10, "desc": "晴"},
            {"date": "2026-12-03", "low": 3, "high": 11, "rain_prob": 20, "desc": "多云"},
        ], "note": "测试数据"}

    real_tools.TOOL_REGISTRY["get_weather"] = fake_weather
    restore = _patch_center(CENTER)
    try:
        s = Session(today=date(2026, 11, 2))
        r = s.turn(f"12月1号去{city}玩3天，2人，预算5000")
    finally:
        autofetch.fetch_city = old_fetch
        real_tools.TOOL_REGISTRY["get_weather"] = old_weather
        restore()

    names = [n["name"] for d in (r.plan or {}).get("days_plan", []) for n in d["nodes"]]
    check("陌生城市也能直接出方案", r.mode == "plan" and r.plan is not None, f"mode={r.mode}")
    check("补全的景点真的排进了行程",
          any(city in n for n in names), f"nodes={names}")
    check("方案里写明数据来源", "数据说明" in r.render and city in r.render,
          f"tail={r.render[-160:]}")
    check("Reply 带回补全结果（前端/存档可用）",
          r.fetched.get(city, {}).get("status") == "fetched", f"{r.fetched}")


# ------------------------------------------------------------ 点名景点补全
#
# 用户说「我想去上海迪士尼」而库里只有外滩/豫园时，编排器匹配不到这个 POI，
# 整句点名被静默吞掉——这才是「景点加不进去」的真因，不是排序问题。
# 下面覆盖四条路：库里已有 / 定向抓取 / 模型给坐标 / 查不到也要排进去。


def test_spot_terms() -> None:
    terms = autofetch._spot_terms("上海迪士尼", "上海")
    check("搜索词从全名逐级拆到核心词",
          terms[0] == "上海迪士尼" and "迪士尼" in terms, f"{terms}")
    check("短名不做无意义的拆解", autofetch._spot_terms("外滩", "上海") == ["外滩"],
          f"{autofetch._spot_terms('外滩', '上海')}")


def test_spot_already_in_kb() -> None:
    db = kb.get_kb()
    db.upsert_poi(CITY, _poi("测试城古城", 30.01, 120.01))
    r = autofetch.ensure_spot(CITY, "测试城古城")
    check("点名景点库里已有 → exists，不打网络", r["status"] == "exists", f"{r}")
    r2 = autofetch.ensure_spot(CITY, "古城")
    check("名字不完全一致也能命中（双向包含）", r2["status"] == "exists", f"{r2}")


def test_spot_fetch_by_name() -> None:
    restore = _patch_center(CENTER)
    try:
        r = autofetch.ensure_spot(
            CITY, "测试城海洋馆",
            fetcher=lambda city, name, center: _poi("测试城海洋馆", 30.02, 120.02,
                                                    appeal=4, duration_min=150))
    finally:
        restore()
    db = kb.get_kb()
    check("点名景点不在库 → 定向补一条并入库",
          r["status"] == "fetched" and (r["poi"] or {}).get("name") == "测试城海洋馆",
          f"{r}")
    check("补进来的景点按名字可查（编排器能重查到）",
          any("海洋馆" in p["name"] for p in (db.list_pois(CITY) or [])))


def test_spot_llm_preferred() -> None:
    old = llm.lookup_poi
    row = {"name": "测试城极地馆", "lat": 30.05, "lon": 120.05,
           "category": "attraction", "duration_min": 150, "ticket": 120,
           "open_hours": "09:00-18:00", "note": ""}
    llm.lookup_poi = lambda client, city, name: (row, None)
    restore = _patch_center(CENTER)
    try:
        r = autofetch.ensure_spot(CITY, "极地馆", client=object())
    finally:
        llm.lookup_poi = old
        restore()
    check("有模型时先问模型（OSM 在本机常年不可达，等不起）",
          r["status"] == "fetched" and r["source"] == "llm", f"{r}")

    old_down = autofetch._OSM_DOWN_UNTIL
    llm.lookup_poi = lambda *a: ({"name": "外地景点", "lat": 39.90, "lon": 116.40,
                                  "category": "attraction", "duration_min": 120,
                                  "ticket": 0, "open_hours": "", "note": ""}, None)
    restore = _patch_center(CENTER)
    autofetch._OSM_DOWN_UNTIL = 1e12          # 假装 OSM 熔断，别真打网络
    try:
        r2 = autofetch.ensure_spot(CITY, "外地景点", client=object())
    finally:
        llm.lookup_poi = old
        autofetch._OSM_DOWN_UNTIL = old_down
        restore()
    check("模型编的坐标离城区 >60km → 不收，退回未收录安排",
          r2["status"] == "custom", f"{r2}")


def test_spot_custom_fallback() -> None:
    old_down = autofetch._OSM_DOWN_UNTIL
    restore = _patch_center(CENTER)
    autofetch._OSM_DOWN_UNTIL = 1e12
    try:
        r = autofetch.ensure_spot(CITY, "测试城太空馆")
    finally:
        autofetch._OSM_DOWN_UNTIL = old_down
        restore()
    p = r["poi"] or {}
    check("三条路都查不到 → 仍按点名排进行程，而不是回一句『查不到』",
          r["status"] == "custom" and p.get("name") == "测试城太空馆", f"{r}")
    check("未收录景点用市区坐标兜底 + 明确标注待核实",
          p.get("geo") == [30.0, 120.0] and "自行确认" in (p.get("note") or ""),
          f"{p.get('geo')} / {p.get('note')}")


def test_bad_spot_name_never_stored() -> None:
    """脏名字绝不能按"用户点名景点"入库。

    实测事故：「把去程到达时间改为 09:00」误抽出「达时间改为」，autofetch
    按未收录景点落库（用户点名+位置估算+240 分钟），之后每一份新方案都会
    命中它——全新成都方案把它排成 Day2 的 09:00 景点，还带预约和门票说明。
    """
    before = len(autofetch.kb.get_kb().list_pois(CITY) or [])
    for name in ("达时间改为", "成都、重庆", "云南", "班次",
                 "可不去的删掉", "一些景点"):
        r = autofetch.ensure_spot(CITY, name)
        check(f"脏名字「{name}」被拒绝入库", r["status"] == "rejected", str(r))
    after = len(autofetch.kb.get_kb().list_pois(CITY) or [])
    check("被拒绝的名字一条都没写进库", after == before, f"{before} → {after}")
    # 正常点名不受影响（都江堰曾因"都"字被旧前缀规则误杀）
    from travel_agent.agent import valid_poi_name
    check("真实景点名通过校验",
          all(valid_poi_name(n) for n in
              ("都江堰", "什刹海", "宽窄巷子", "大熊猫繁育研究基地", "喜洲古镇")),
          "都江堰/什刹海/宽窄巷子/大熊猫繁育研究基地/喜洲古镇")
    check("碎片与城市串被校验拦下",
          not any(valid_poi_name(n) for n in
                  ("达时间改为", "成都、重庆", "云南", "还是", "几天")),
          "达时间改为/成都、重庆/云南/还是/几天")


def test_llm_coord_refined() -> None:
    """模型粗坐标（2 位小数）要经天地图地理编码校正（2026-11-11 北京实测：
    图钉偏约 1km，驾车连线在错误起终点之间找路——"动线不是真实路径"）。"""
    old_lookup, old_geo = llm.lookup_poi, autofetch.tdt_geocode
    row = {"name": "测试城科技馆", "lat": 30.05, "lon": 120.05,
           "category": "attraction", "duration_min": 120, "ticket": 60,
           "open_hours": "", "note": ""}
    llm.lookup_poi = lambda client, city, name: (row, None)
    autofetch.tdt_geocode = lambda *a, **k: (30.0523, 120.0611)
    restore = _patch_center(CENTER)
    try:
        r = autofetch.ensure_spot(CITY, "科技馆", client=object())
    finally:
        llm.lookup_poi, autofetch.tdt_geocode = old_lookup, old_geo
        restore()
    geo = (r.get("poi") or {}).get("geo") or []
    check("模型坐标被地理编码校正",
          r["status"] == "fetched" and len(geo) == 2
          and abs(geo[0] - 30.0523) < 1e-6 and abs(geo[1] - 120.0611) < 1e-6,
          f"geo={geo}")

    # 校正点离城心太远（地理编码撞了同名地名）→ 保留模型原坐标
    row2 = {"name": "测试城天文馆", "lat": 30.05, "lon": 120.05,
            "category": "attraction", "duration_min": 120, "ticket": 60,
            "open_hours": "", "note": ""}
    llm.lookup_poi = lambda client, city, name: (row2, None)
    autofetch.tdt_geocode = lambda *a, **k: (40.0, 116.0)
    restore = _patch_center(CENTER)
    try:
        r2 = autofetch.ensure_spot(CITY, "天文馆", client=object())
    finally:
        llm.lookup_poi, autofetch.tdt_geocode = old_lookup, old_geo
        restore()
    geo2 = (r2.get("poi") or {}).get("geo") or []
    check("校正点离城过远（撞名）→ 保留模型原坐标",
          len(geo2) == 2 and abs(geo2[0] - 30.05) < 1e-6
          and abs(geo2[1] - 120.05) < 1e-6, f"geo={geo2}")


def test_overseas_nominatim_fallback() -> None:
    """境外城市天地图查不到（数据只覆盖国内）：坐标兜底退 Nominatim。

    2026-11-12 洛杉矶实测：模型近似坐标（2 位小数）原样入库，图钉偏约 1km。
    Nominatim 主名是英文，所以要点名/发现环节带出 name_en；这条兜底不依赖
    天地图 Key，境内城市绝不走 Nominatim（数据源白名单口径）。
    """
    old_key = autofetch._tdt_key
    old_nom = autofetch._nominatim_geocode
    autofetch._tdt_key = lambda: ""          # 无 Key 也要能走境外兜底
    autofetch._GEO_CACHE.clear()
    try:
        autofetch._nominatim_geocode = (
            lambda name, city, center: (34.1182, -118.3003))
        out = _REAL_TDT_GEOCODE("格里菲斯天文台", "洛杉矶",
                                [34.05, -118.24],
                                name_en="Griffith Observatory")
        check("境外坐标经 Nominatim 兜底校正",
              out == (34.1182, -118.3003), f"out={out}")

        nom_calls: list[str] = []
        autofetch._nominatim_geocode = (
            lambda name, city, center: nom_calls.append(name))
        out2 = _REAL_TDT_GEOCODE("故宫", "北京", [39.9, 116.4])
        check("境内无 Key 不走 Nominatim（保持 None 降级）",
              out2 is None and not nom_calls, f"out={out2} calls={nom_calls}")

        check("境内外判定：洛杉矶境外 / 成都境内",
              not autofetch._in_china([34.05, -118.24])
              and autofetch._in_china([30.65, 104.06]),
              "LA / 成都")
    finally:
        autofetch._tdt_key = old_key
        autofetch._nominatim_geocode = old_nom
        autofetch._GEO_CACHE.clear()


def test_llm_en_name_plumbed_to_refine() -> None:
    """模型给出的英文名要透传到坐标校正链路（境外 Nominatim 靠它命中）。"""
    captured: dict = {}
    old_lookup, old_geo = llm.lookup_poi, autofetch.tdt_geocode
    row = {"name": "格里菲斯天文台", "name_en": "Griffith Observatory",
           "lat": 34.12, "lon": -118.30, "category": "attraction",
           "duration_min": 120, "ticket": 0, "open_hours": "", "note": ""}

    def fake_geocode(name, city, center=None, name_en=None):
        captured["name"], captured["name_en"] = name, name_en
        return (34.1182, -118.3003)

    llm.lookup_poi = lambda client, city, name: (row, None)
    autofetch.tdt_geocode = fake_geocode
    restore = _patch_center([34.05, -118.24])
    try:
        r = autofetch.ensure_spot("洛杉矶", "格里菲斯天文台", client=object())
    finally:
        llm.lookup_poi, autofetch.tdt_geocode = old_lookup, old_geo
        restore()
    check("英文名透传到地理编码", captured.get("name_en") == "Griffith Observatory"
          and captured.get("name") == "格里菲斯天文台", str(captured))
    geo = (r.get("poi") or {}).get("geo") or []
    check("境外模型坐标被校正入库",
          r["status"] == "fetched" and abs(geo[0] - 34.1182) < 1e-6, f"geo={geo}")


def test_ensure_hotels_directed() -> None:
    """酒店定向补全：城市有景点没酒店时 ensure_city 会短路，ensure_hotels 只看酒店表。"""
    autofetch.reset_cache()
    calls = {"n": 0}

    def hotel_fetcher(city, center):
        calls["n"] += 1
        return [{"name": f"{city}大酒店", "area": "市中心", "star": 4,
                 "price": 520, "geo": [30.02, 120.01], "cancel_free": True,
                 "dist_to_core_km": 1.2, "rating": 4.6,
                 "note": "房价为星级估算，非实时报价"}]

    restore = _patch_center(CENTER)
    try:
        r1 = autofetch.ensure_hotels("无酒店城", fetcher=hotel_fetcher)
        r2 = autofetch.ensure_hotels("无酒店城", fetcher=hotel_fetcher)
        r3 = autofetch.ensure_hotels("无酒店城", fetcher=hotel_fetcher)
        # 已有酒店的城市绝不重复抓取
        def boom(*_a, **_k):
            raise AssertionError("已有酒店数据不该触发抓取")
        r4 = autofetch.ensure_hotels("邯郸", fetcher=boom)
    finally:
        restore()
    check("无酒店城市抓取成功", r1["status"] == "fetched" and r1["hotels"] == 1,
          f"{r1}")
    check("酒店写入知识库",
          any(h["name"] == "无酒店城大酒店"
              for h in kb.get_kb().list_hotels("无酒店城") or []))
    check("二次调用命中 exists 不重复抓", r2["status"] == "exists"
          and calls["n"] == 1, f"r2={r2} calls={calls['n']}")
    check("入库后的酒店带非实时报价标注",
          "非实时报价" in (kb.get_kb().list_hotels("无酒店城") or [{}])[0]["note"])
    check("入库后再次调用仍为 exists", r3["status"] == "exists", f"{r3}")
    check("已有酒店城市返回 exists", r4["status"] == "exists", f"{r4}")


def test_ensure_hotels_failure_and_empty() -> None:
    """补库失败/上游没住宿：如实返回，不编酒店；失败短期不重试。"""
    autofetch.reset_cache()
    calls = {"n": 0}

    def broken(city, center):
        calls["n"] += 1
        raise ConnectionError("上游超时")

    def empty(city, center):
        return []

    restore = _patch_center(CENTER)
    try:
        f1 = autofetch.ensure_hotels("失败酒店城", fetcher=broken)
        f2 = autofetch.ensure_hotels("失败酒店城", fetcher=broken)
        e1 = autofetch.ensure_hotels("空住宿城", fetcher=empty)
    finally:
        restore()
    check("抓取失败如实返回 failed", f1["status"] == "failed"
          and "上游超时" in f1["message"], f"{f1}")
    check("失败结果缓存不重试", calls["n"] == 1 and f2.get("cached") is True,
          f"calls={calls['n']} f2={f2}")
    check("上游没住宿返回 empty", e1["status"] == "empty", f"{e1}")
    check("失败/空结果不写酒店", not (kb.get_kb().list_hotels("失败酒店城")
                                      or kb.get_kb().list_hotels("空住宿城")))


def main() -> int:
    # 默认关掉真实地理编码：单测不打外网（专项测试自己临时替换桩）
    autofetch.tdt_geocode = lambda *a, **k: None
    test_plausible_city()
    test_existing_city_no_fetch()
    test_fetch_and_store()
    test_second_call_is_noop()
    test_failure_and_cache()
    test_ensure_hotels_directed()
    test_ensure_hotels_failure_and_empty()
    test_unresolved_and_disabled()
    test_field_mapping()
    test_llm_fallback_filters_hallucination()
    test_source_chain_osm_then_llm()
    test_session_end_to_end()
    test_spot_terms()
    test_spot_already_in_kb()
    test_spot_fetch_by_name()
    test_spot_llm_preferred()
    test_llm_coord_refined()
    test_overseas_nominatim_fallback()
    test_llm_en_name_plumbed_to_refine()
    test_spot_custom_fallback()
    test_bad_spot_name_never_stored()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 条自动补全断言通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
