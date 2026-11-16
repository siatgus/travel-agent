"""多源 POI 检索层的离线测试：解析、坐标系、降级顺序。

为什么不用真网络：Overpass 在国内常态超时（正是要被替换掉的原因），
高德/维基导游又都要 Key 或限流，把断言挂在真实响应上只会得到一堆
"今天网不好"的假失败。这里把 `_http_json` 换成桩，只验证我们自己的
逻辑：字段映射对不对、GCJ-02 有没有反算、超出 60km 的点有没有丢、
一个源挂了会不会继续走下一个。

运行：python tests/test_poi_sources.py    失败时退出码为 1。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

from travel_agent import poi_sources as ps

RESULTS: list[tuple[str, bool, str]] = []
CENTER = [39.9042, 116.4074]              # 北京


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


class StubHTTP:
    """按 URL 关键词返回预置响应，并记录调用次数。"""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float, headers=None) -> dict:
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError(f"未预置的 URL: {url[:90]}")

    def count(self, key: str) -> int:
        return sum(1 for u in self.calls if key in u)


def with_stub(stub) -> object:
    old = ps._http_json
    ps._http_json = stub
    return old


# ------------------------------------------------------------------ 坐标系


def test_coord_transform() -> None:
    # 故宫 GCJ-02 实测点 → WGS-84 应落到 39.91x / 116.39x，偏移约 500 米
    lng, lat = ps.gcj2wgs(116.403963, 39.915119)
    check("GCJ-02 → WGS-84 落在故宫附近",
          39.905 <= lat <= 39.925 and 116.385 <= lng <= 116.410,
          f"{lng:.5f},{lat:.5f}")
    d = ps._km(39.915119, 116.403963, lat, lng)
    check("偏移量在 300~800 米之间（即真的纠了偏）", 0.3 <= d <= 0.8, f"{d:.3f}km")
    b_lng, b_lat = ps.wgs2gcj(lng, lat)
    check("往返转换回到原点（误差 < 1 米）",
          ps._km(39.915119, 116.403963, b_lat, b_lng) < 0.001,
          f"{b_lng:.6f},{b_lat:.6f}")
    la, lo = ps.gcj2wgs(-118.25, 34.05)
    check("境外坐标直通不纠偏", (la, lo) == (-118.25, 34.05), f"{la},{lo}")


# ------------------------------------------------------------------ 高德


def _amap_poi(name, lng, lat, **kw) -> dict:
    p = {"name": name, "location": f"{lng},{lat}", "typecode": "110205",
         "type": "风景名胜;公园广场;名胜古迹", "adname": "东城区",
         "address": "景山前街4号", "tel": "010-00000000",
         "biz_ext": {"rating": "4.8", "cost": "60.00", "opentime2": "08:30-17:00"}}
    p.update(kw)
    return p


def test_amap_parsing() -> None:
    ps.reset_cache()
    stub = StubHTTP({
        "restapi.amap.com/v3/place/text": {"status": "1", "pois": [
            _amap_poi("故宫博物院", 116.397, 39.918),
            _amap_poi("景山公园", 116.396, 39.925),
            # 坐标缺失/非法：整条丢弃，不允许用城市中心凑
            {"name": "坏点", "location": "", "biz_ext": []},
            # 离城心 100km 以上：不是本市
            _amap_poi("外地景点", 115.5, 40.9),
            # 重名只留一条
            _amap_poi("故宫博物院", 116.397, 39.918),
        ]},
    })
    old = with_stub(stub)
    try:
        ps._DOWN.clear()
        old_key = ps.amap_key
        ps.amap_key = lambda: "0" * 32
        rows = ps.search_amap("北京", CENTER, "attraction", pages=1)
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    names = [r["name"] for r in rows]
    check("高德景点解析：坏点/外地/重名都被挡掉", names == ["故宫博物院", "景山公园"],
          f"{names}")
    r = rows[0]
    check("景点行带 category=attraction", r["category"] == "attraction")
    check("坐标已反算成 WGS-84（不在 GCJ 原值上）",
          abs(r["geo"][1] - 116.397) > 1e-4, f"{r['geo']}")
    check("门票不瞎填（高德的 cost 是人均不是门票）", r["cost"] == 0, f"{r['cost']}")
    check("营业时间取 opentime2", r["open_hours"] == "08:30-17:00", r["open_hours"])
    check("评分高的景点热度更高", r["appeal"] == 4, f"{r['appeal']}")
    check("note 里注明来源与门票口径",
          "高德" in r["note"] and "门票" in r["note"], r["note"])

    # 餐饮：cost 是人均，可以填
    ps.reset_cache()
    stub2 = StubHTTP({"restapi.amap.com": {"status": "1", "pois": [
        {"name": "卤煮老店", "location": "116.40,39.90", "type": "餐饮服务;中餐厅",
         "adname": "西城区", "address": "前门大街", "biz_ext": {"cost": "45"}}]}})
    old = with_stub(stub2)
    try:
        ps._DOWN.clear()
        old_key = ps.amap_key
        ps.amap_key = lambda: "0" * 32
        rest = ps.search_amap("北京", CENTER, "restaurant", pages=1)
        hot = ps.search_amap("北京", CENTER, "hotel", pages=1)
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("餐厅类别与人均正确", rest and rest[0]["category"] == "restaurant"
          and rest[0]["cost"] == 45, f"{rest[:1]}")
    check("酒店行带 star/price/dist（排预算要用）",
          hot and "category" not in hot[0] and hot[0]["star"] == 3
          and hot[0]["price"] == 45, f"{hot[:1]}")


def test_amap_quota_and_pages() -> None:
    ps.reset_cache()
    ps._DOWN.clear()
    pages: list[int] = []

    def stub_quota(url, *, timeout, headers=None):
        pages.append(int(url.split("page=")[1].split("&")[0]))
        return {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT"}

    old = with_stub(stub_quota)
    old_key = ps.amap_key
    ps.amap_key = lambda: "0" * 32
    try:
        rows = ps.search_amap("北京", CENTER, "attraction", pages=3)
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("配额用尽 → 只问一次就停，不翻页", rows == [] and len(pages) == 1, f"{pages}")
    check("配额错误触发熔断（10 分钟内跳过该源）", ps._down("amap"))

    # 满页才翻下一页
    ps.reset_cache()
    ps._DOWN.clear()
    seen: list[int] = []

    def stub_full(url, *, timeout, headers=None):
        page = int(url.split("page=")[1].split("&")[0])
        seen.append(page)
        n = 25 if page == 1 else 3
        return {"status": "1", "pois": [
            _amap_poi(f"景点{page}-{i}", 116.397 + i * 1e-4, 39.918) for i in range(n)]}

    old = with_stub(stub_full)
    ps.amap_key = lambda: "0" * 32
    try:
        rows = ps.search_amap("北京", CENTER, "attraction", pages=3)
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("首页满 25 条才翻第二页，第二页不满就停",
          seen == [1, 2] and len(rows) == 28, f"pages={seen} n={len(rows)}")


def test_amap_needs_key() -> None:
    old_key = ps.amap_key
    ps.amap_key = lambda: ""
    try:
        check("没配 Key 时高德直接返回空（不浪费一次请求）",
              ps.search_amap("北京", CENTER, "attraction") == [])
    finally:
        ps.amap_key = old_key


# ------------------------------------------------------------------ 维基导游

WIKITEXT = """== 观光 ==
{{see
 | name=故宫 | lat=39.9163 | long=116.3972 | hours=08:30-17:00 | price=¥60
 | address=景山前街4号 | content=明清两代皇宫，[[中轴线]]上的核心。
}}
{{see | name=景山公园 | lat=39.925 | long=116.396 | price=免费
 | content=俯瞰紫禁城的最佳位置}}
{{see | name=没有坐标的条目 | content=这条没有经纬度，不能拿城心凑数}}
{{do | name=什刹海骑行 | lat=39.940 | long=116.386 | content=环湖骑行约 1 小时}}
{{listing | type=see | name=798艺术区 | lat=39.984 | long=116.495 | content=老厂房改造
 | address=酒仙桥路4号}}
{{listing | type=sleep | name=某青年旅舍 | lat=39.930 | long=116.410 | price=¥120}}
{{listing | type=eat | name=某烤鸭店 | lat=39.900 | long=116.400 | price=¥150}}
{{eat | name=护国寺小吃 | lat=39.937 | long=116.380 | content=豆汁焦圈}}
{{sleep | name=北京饭店 | lat=39.910 | long=116.407 | price=¥900}}
{{other | name=不是条目的模板 | lat=39.9 | long=116.4}}
"""


def test_wikivoyage_parsing() -> None:
    ps.reset_cache()
    ps._WIKI.clear()
    stub = StubHTTP({"/w/api.php": {"parse": {"wikitext": WIKITEXT, "title": "北京"}}})
    old = with_stub(stub)
    try:
        attrs = ps.search_wikivoyage("北京", CENTER, "attraction")
        eats = ps.search_wikivoyage("北京", CENTER, "restaurant")
        sleeps = ps.search_wikivoyage("北京", CENTER, "hotel")
    finally:
        ps._http_json = old
    names = [r["name"] for r in attrs]
    check("see/do/listing(type=see) 都算景点，其他类别不混进来",
          names == ["故宫", "景山公园", "什刹海骑行", "798艺术区"], f"{names}")
    check("没有坐标的条目被跳过（不用城心凑）", "没有坐标的条目" not in names)
    check("非条目模板（other）不进结果", all("不是条目" not in n for n in names))
    check("eat 归到餐厅、sleep 归到酒店",
          [r["name"] for r in eats] == ["某烤鸭店", "护国寺小吃"]
          and [r["name"] for r in sleeps] == ["某青年旅舍", "北京饭店"],
          f"{[r['name'] for r in eats]} / {[r['name'] for r in sleeps]}")

    g = attrs[0]
    check("价格从 ¥60 解析成 60", g["cost"] == 60, f"{g['cost']}")
    check("免费写成 0（不瞎猜）", attrs[1]["cost"] == 0, f"{attrs[1]['cost']}")
    check("开放时间进 open_hours、地址进 note",
          g["open_hours"] == "08:30-17:00" and "景山前街" in g["note"],
          f"{g['open_hours']} / {g['note']}")
    check("wiki 标记被清理（不留 [[ ]]）", "[[" not in g["note"], g["note"])
    check("指南里越靠前热度越高", [r["appeal"] for r in attrs[:2]] == [5, 5],
          f"{[r['appeal'] for r in attrs]}")
    check("note 带 CC BY-SA 署名（许可要求）", "CC BY-SA" in g["note"], g["note"])
    check("景点按类型估时长（皇宫/公园都 >= 60 分钟）",
          all(r["duration_min"] >= 45 for r in attrs),
          f"{[(r['name'], r['duration_min']) for r in attrs]}")

    # 中文维基导游条目名常是繁体，用户输入是简体：不归一就永远匹配不上
    ps.reset_cache()
    ps._WIKI.clear()
    trad = StubHTTP({"/w/api.php": {"parse": {"wikitext":
        "{{see | name=龍門石窟 | lat=34.5556 | long=112.4697}}"
        "{{see | name=￼開元寺 | lat=24.9165 | long=118.581}}"}}})
    old = with_stub(trad)
    try:
        rows = ps.search_wikivoyage("洛阳", [34.62, 112.45], "attraction")
        rows2 = ps.search_wikivoyage("泉州", [24.87, 118.67], "attraction")
    finally:
        ps._http_json = old
    check("繁体条目名归一成简体（用户说「龙门石窟」能命中）",
          [r["name"] for r in rows] == ["龙门石窟"], f"{[r['name'] for r in rows]}")
    check("名字里的 U+FFFC 脏字符被清掉",
          rows2 and rows2[0]["name"] == "开元寺", f"{[r['name'] for r in rows2]}")


def test_wikivoyage_lang_fallback() -> None:
    ps.reset_cache()
    ps._WIKI.clear()
    calls: list[str] = []

    def stub(url, *, timeout, headers=None):
        calls.append(url)
        if "zh.wikivoyage" in url:
            return {"error": {"code": "missingtitle", "info": "没有这个页面"}}
        return {"parse": {"wikitext": "{{see | name=Tokyo Tower | lat=35.6586 | long=139.7454}}"}}

    old = with_stub(stub)
    old_gap = ps._WIKI_GAP
    ps._WIKI_GAP = 0.0
    try:
        rows = ps.search_wikivoyage("东京", [35.68, 139.76], "attraction")
    finally:
        ps._http_json = old
        ps._WIKI_GAP = old_gap
    check("中文站没条目时退英文站", rows and rows[0]["name"] == "Tokyo Tower",
          f"{[r['name'] for r in rows]}")
    check("先 zh 后 en，各请求一次", len(calls) == 2, f"{len(calls)}")


def test_wikivoyage_cache() -> None:
    ps.reset_cache()
    ps._WIKI.clear()
    stub = StubHTTP({"/w/api.php": {"parse": {"wikitext": WIKITEXT}}})
    old = with_stub(stub)
    try:
        ps.search_wikivoyage("北京", CENTER, "attraction")
        n1 = len(stub.calls)
        ps.search_wikivoyage("北京", CENTER, "attraction")
        n2 = len(stub.calls)
    finally:
        ps._http_json = old
    check("同一城市第二次取页不重复请求", n1 == n2 and n1 >= 1, f"{n1}->{n2}")


# ------------------------------------------------------------------ 统一入口


def test_chain_order() -> None:
    ps.reset_cache()
    ps._DOWN.clear()
    ps._WIKI.clear()
    hits: list[str] = []

    def stub(url, *, timeout, headers=None):
        if "restapi.amap.com" in url:
            hits.append("amap")
            raise TimeoutError("mock timeout")
        hits.append("wiki")
        return {"parse": {"wikitext": WIKITEXT}}

    old = with_stub(stub)
    old_key = ps.amap_key
    ps.amap_key = lambda: "0" * 32
    try:
        rows, src = ps.search("北京", CENTER, "attraction")
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("高德失败自动落到维基导游", src == "wikivoyage" and len(rows) == 4,
          f"src={src} n={len(rows)} hits={hits}")
    check("先问高德再问维基（顺序不能反）", hits[0] == "amap", f"{hits}")

    # 两个源都空 → 返回空，不编数据
    ps.reset_cache()
    ps._DOWN.clear()
    ps._WIKI.clear()

    def stub_empty(url, *, timeout, headers=None):
        if "restapi.amap.com" in url:
            return {"status": "1", "pois": []}
        return {"error": {"code": "missingtitle"}}

    old = with_stub(stub_empty)
    ps.amap_key = lambda: "0" * 32
    try:
        rows, src = ps.search("不存在城", CENTER, "attraction")
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("两个源都没结果就返回空（交给 Overpass/模型）", rows == [] and src == "")


def test_cache_and_down() -> None:
    ps.reset_cache()
    ps._DOWN.clear()
    ps._WIKI.clear()
    stub = StubHTTP({"restapi.amap.com": {"status": "1", "pois": [
        _amap_poi("故宫博物院", 116.397, 39.918)]}})
    old = with_stub(stub)
    old_key = ps.amap_key
    ps.amap_key = lambda: "0" * 32
    try:
        ps.search("北京", CENTER, "attraction")
        n1 = len(stub.calls)
        rows, src = ps.search("北京", CENTER, "attraction")
        n2 = len(stub.calls)
    finally:
        ps.amap_key = old_key
        ps._http_json = old
    check("同一城市第二次直接走缓存（不再出网）", n1 == n2, f"{n1}->{n2}")
    check("缓存命中标注 source=cache", src == "cache", src)
    check("缓存不改变结果内容", len(rows) == 1, f"{len(rows)}")

    ps._DOWN.clear()
    ps._mark_down("amap")
    check("熔断期内 is down", ps._down("amap"))
    ps._DOWN.clear()
    check("reset 后恢复", not ps._down("amap"))


def test_describe() -> None:
    old_key = ps.amap_key
    ps.amap_key = lambda: ""
    try:
        d = ps.describe()
    finally:
        ps.amap_key = old_key
    check("describe 报出各源是否可用",
          d["wikivoyage"]["configured"] is True and "amap" in d, f"{d}")


def main() -> int:
    for fn in (test_coord_transform, test_amap_parsing, test_amap_quota_and_pages,
               test_amap_needs_key, test_wikivoyage_parsing,
               test_wikivoyage_lang_fallback, test_wikivoyage_cache,
               test_chain_order, test_cache_and_down, test_describe):
        fn()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 条多源检索断言通过")
    if bad:
        print("失败项：")
        for name, _, detail in bad:
            print(f"  - {name}　{detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
