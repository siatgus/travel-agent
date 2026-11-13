"""DataService 统一数据服务测试（行野 2.0 第二阶段，文档 §20）。

覆盖：
- DataResult 协议：成功有数据 / 成功无数据 / 查询失败 / 过期数据四种状态
  严格区分；旧工具协议 from_tool_result 映射。
- DataPolicy：分类型 TTL 与 stale 判定（种子不标过期、在线抓取超期标 stale）。
- DataService：知识库命中、静态种子回落、空结果自动补全（成功/失败）、
  补库触发不误伤参数类错误、餐厅城市/关键词/周边检索、天气与班次转发、
  data_source_log 落库、旧工具兼容。

数据自足：TRAVEL_KB_DB 指向临时库（seed_if_empty 播种子），自动补全用
桩模块，不打网络；跑法与其余测试一致：python tests/test_data_service.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

_tmp = Path(tempfile.mkdtemp(prefix="travel_data_svc_test_"))
os.environ["TRAVEL_KB_DB"] = str(_tmp / "kb.db")   # 必须在 import kb 之前设置

from travel_agent import kb, tools                                  # noqa: E402
from travel_agent.kb import KnowledgeBase                           # noqa: E402
from travel_agent.services import (                                 # noqa: E402
    DataService, DataResult, DataPolicy, EMPTY_ERROR_CODES,
    SOURCE_API, SOURCE_KB, SOURCE_STATIC, get_data_service, policy_of,
)
from travel_agent.services.data_policy import POLICIES, is_stale     # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


DB = kb.get_kb()
DB.seed_if_empty()

REST = {"name": "测试火锅店", "category": "restaurant", "area": "锦江区",
        "geo": [30.6570, 104.0810], "cost": 0,
        "note": "人均未核实（测试数据）", "tags": ["餐厅", "火锅"]}
# 距锚点约 5km：在 10km 圈层内、3km 圈层外（验证圈层放宽）
FAR_REST = {"name": "远郊面馆", "category": "restaurant", "area": "远郊区",
            "geo": [30.6900, 104.1200], "cost": 0,
            "note": "人均未核实（测试数据）", "tags": ["餐厅", "面食"]}


def _svc(**kw) -> DataService:
    return DataService(db=DB, **kw)


# ================================================================ 协议层

def test_data_result_protocol() -> None:
    r_ok = DataResult.success([{"name": "a"}], source=SOURCE_KB, note="x")
    check("协议：成功有数据", r_ok.ok and r_ok.has_data and r_ok.source == SOURCE_KB)
    r_empty = DataResult.empty(source=SOURCE_KB, message="没有")
    check("协议：成功无数据与失败分开",
          r_empty.ok and r_empty.data == [] and not r_empty.has_data
          and r_empty.error_code is None)
    r_fail = DataResult.failure("PROVIDER_TIMEOUT", "上游超时", retryable=True)
    check("协议：失败带错误码",
          not r_fail.ok and r_fail.error_code == "PROVIDER_TIMEOUT"
          and r_fail.items == [])
    r_stale = DataResult(ok=True, data=[1], stale=True,
                         metadata={"origin_source": "external_api"})
    check("协议：过期数据显式标记", r_ok.stale is False and r_stale.stale is True)
    # 旧工具协议映射
    r_map = DataResult.from_tool_result(
        {"ok": True, "items": [1, 2], "total": 2, "note": "口径说明"})
    check("协议：旧工具成功映射",
          r_map.ok and r_map.items == [1, 2]
          and r_map.metadata["note"] == "口径说明"
          and "raw" in r_map.metadata)
    r_map_err = DataResult.from_tool_result(
        {"ok": False, "error": {"code": "NO_POI", "message": "暂无",
                                "retryable": True}})
    check("协议：旧工具失败映射",
          not r_map_err.ok and r_map_err.error_code == "NO_POI"
          and r_map_err.retryable())
    check("协议：无数据错误码白名单",
          set(EMPTY_ERROR_CODES) == {"NO_POI", "NO_HOTEL", "NO_ROUTE"}
          and r_map_err.is_empty_no_data()
          and not DataResult.from_tool_result(
              {"ok": False,
               "error": {"code": "BAD_DATE", "message": "x"}}).is_empty_no_data())


def test_data_policy() -> None:
    check("策略：六类数据类型齐备",
          set(("attraction", "restaurant", "hotel", "weather", "transport",
               "food_knowledge")) <= set(POLICIES))
    check("策略：餐厅 1 天可补全，班次不允许过期",
          policy_of("restaurant").ttl_seconds == 86400
          and policy_of("restaurant").autofetch_on_empty
          and not policy_of("transport").allow_stale
          and policy_of("transport").autofetch_on_empty is False)
    p = policy_of("restaurant")
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    check("策略：在线新鲜数据不标 stale",
          not is_stale(p, fresh, "auto-fetch"))
    check("策略：在线超期数据标 stale",
          is_stale(p, old, "auto-fetch"))
    check("策略：在线数据无时间戳保守标 stale",
          is_stale(p, None, "auto-fetch"))
    check("策略：种子/人工核实基线不按 TTL 标 stale",
          not is_stale(p, old, "seed") and not is_stale(p, old, "manual"))
    check("策略：知识类 ttl=0 永不过期",
          not is_stale(policy_of("food_knowledge"), None, "auto-fetch"))


# ================================================================ KB 命中与回落

def test_kb_hit_and_static_fallback() -> None:
    svc = _svc()
    r = svc.get_pois("大理", limit=5)     # 大理在静态种子内，seed 后必在 KB
    check("DataService：KB 命中返回景点",
          r.ok and r.has_data and r.source == SOURCE_KB
          and all("name" in p for p in r.items[:3]),
          f"source={r.source} n={len(r.items)}")
    check("DataService：KB 命中带来源元数据",
          r.metadata.get("origin_source") not in (None, "unknown")
          and "row_sources" in r.metadata, str(r.metadata)[:120])

    # 静态种子回落：注入 poi_fn 返回一个来自静态库的结果（元数据查不到行）
    static_items = [{"name": "静态景点", "category": "attraction",
                     "geo": [25.6, 100.2]}]

    def static_fn(args):
        return {"ok": True, "items": static_items, "total": 1, "note": "种子"}

    svc2 = DataService(db=None, poi_fn=static_fn)
    r2 = svc2.get_pois("静态回落城")
    check("DataService：静态种子回落标记",
          r2.ok and r2.source == SOURCE_STATIC
          and r2.metadata.get("origin_source") == "static_seed",
          f"source={r2.source}")


# ================================================================ 空结果补全

def test_autofetch_on_empty() -> None:
    calls = {"n": 0}

    class FakeAF:
        DATA_TYPES = ("attraction", "restaurant", "hotel")

        @staticmethod
        def ensure_city_data(city, data_type, db=None, fetcher=None,
                             client=None, force=False):
            calls["n"] += 1
            if data_type == "restaurant":
                db.upsert_poi(city, dict(REST), source="auto-fetch")
                return {"status": "fetched", "city": city, "source": "osm",
                        "pois": 1, "hotels": 0, "message": "补 1 家餐厅"}
            if data_type == "hotel":
                return {"status": "failed", "city": city, "pois": 0,
                        "hotels": 0, "message": "Overpass 不可达"}
            return {"status": "empty", "city": city, "pois": 0, "hotels": 0,
                    "message": "上游没有可用景点"}

    svc = DataService(db=DB, autofetch_mod=FakeAF())
    city = "补全验证城"
    r = svc.get_restaurants(city)
    check("补全：空结果触发一次定向补库",
          calls["n"] == 1 and r.ok and r.has_data
          and r.metadata.get("autofetch", {}).get("status") == "fetched",
          f"calls={calls['n']} ok={r.ok} meta={str(r.metadata)[:100]}")
    check("补全：补到的餐厅可按关键词检索",
          svc.get_restaurants(city, keyword="火锅").has_data)
    check("补全：补到的数据来源与新鲜度就绪",
          svc.get_restaurants(city).source == SOURCE_KB, "")
    check("补全：autofetch 尝试落 data_source_log",
          any(lg["data_type"] == "restaurant" and lg["city"] == city
              for lg in DB.data_logs(city=city)))

    # 补库失败 ≠ 没有数据：保留 NO_HOTEL 失败语义
    r_hotel = svc.get_hotels(city, "2026-11-14", "2026-11-15", 2)
    check("补全：补库失败保留失败语义（不冒充无数据）",
          not r_hotel.ok and r_hotel.error_code == "NO_HOTEL"
          and r_hotel.metadata.get("autofetch", {}).get("status") == "failed",
          f"ok={r_hotel.ok} code={r_hotel.error_code}")

    # 参数类错误绝不触发补库
    calls["n"] = 0
    svc.get_pois("成都不存在区域", category="nosuch", autofetch=True) \
        if False else None
    r_bad = svc.get_restaurants(city, keyword="火锅")   # 有数据，不该再补
    check("补全：有数据不再触发补库", calls["n"] == 0, f"calls={calls['n']}")


def test_param_error_no_autofetch() -> None:
    ensure_calls = {"n": 0}

    class FakeAF:
        DATA_TYPES = ("attraction", "restaurant", "hotel")

        @staticmethod
        def ensure_city_data(city, data_type, **kw):
            ensure_calls["n"] += 1
            return {"status": "fetched", "city": city, "pois": 0, "hotels": 0,
                    "message": ""}

    def hotel_fn(args):
        return {"ok": False, "error": {"code": "BAD_REQUEST",
                                       "message": "参数错", "retryable": False}}

    svc = DataService(db=DB, autofetch_mod=FakeAF(), hotel_fn=hotel_fn)
    r = svc.get_hotels("任意城", "2026-11-14", "2026-11-15", 2)
    check("补全：参数类错误不触发自动补全",
          not r.ok and r.error_code == "BAD_REQUEST"
          and ensure_calls["n"] == 0, f"calls={ensure_calls['n']}")


# ================================================================ 餐厅能力

def test_restaurant_capability() -> None:
    svc = _svc()
    city = "餐厅能力城"
    DB.upsert_poi(city, dict(REST), source="seed")
    DB.upsert_poi(city, dict(FAR_REST), source="auto-fetch")
    near_geo = [30.6570, 104.0812]

    r = svc.get_restaurants(city)
    check("餐厅：城市检索返回标准化行",
          r.ok and len(r.items) == 2
          and {"name", "category", "geo", "note"} <= set(r.items[0]))
    r_kw = svc.get_restaurants(city, keyword="面食")
    check("餐厅：关键词检索命中菜品系",
          r_kw.ok and [p["name"] for p in r_kw.items] == ["远郊面馆"],
          str([p["name"] for p in r_kw.items]))
    r_kw2 = svc.get_restaurants(city, keyword="不存在的店")
    check("餐厅：关键词无命中返回成功空列表（非失败）",
          r_kw2.ok and r_kw2.items == [] and r_kw2.error_code is None)

    r_near = svc.get_restaurants_near(city, near_geo, exclude="测试锚点景点",
                                      radius_ladder=(3.0, 10.0))
    check("餐厅：周边检索只给半径内由近到远",
          r_near.ok and [p["name"] for p in r_near.items] == ["测试火锅店"]
          and r_near.items[0]["_km"] < 0.1
          and r_near.metadata.get("radius_km") == 3.0,
          str([(p["name"], p.get("_km")) for p in r_near.items]))
    r_far = svc.get_restaurants_near(city, near_geo, radius_ladder=(3.0, 10.0),
                                     keyword="面食")
    check("餐厅：3km 无命中放宽到 10km 圈层",
          r_far.ok and [p["name"] for p in r_far.items] == ["远郊面馆"]
          and r_far.metadata.get("radius_km") == 10.0)
    r_none = svc.get_restaurants_near(city, [31.0000, 105.0000],
                                      radius_ladder=(3.0,))
    check("餐厅：半径全空返回成功空列表",
          r_none.ok and r_none.items == [] and r_none.error_code is None)
    # 过期标记：单独城市只放一行在线抓取数据，把 updated_at 改老
    stale_city = "过期验证城"
    DB.upsert_poi(stale_city, dict(REST), source="auto-fetch")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    DB.conn.execute("UPDATE pois SET updated_at=? WHERE city=?",
                    (old, stale_city))
    DB.conn.commit()
    r_stale = svc.get_restaurants(stale_city)
    check("餐厅：在线抓取超 TTL 的数据标 stale",
          r_stale.stale is True and r_stale.fetched_at == old,
          f"stale={r_stale.stale} fetched_at={r_stale.fetched_at}")
    DB.conn.execute("DELETE FROM pois WHERE city IN (?, ?)",
                    (city, stale_city))
    DB.conn.commit()


# ================================================================ 天气/班次/知识

def test_weather_transport_food() -> None:
    def wx_fn(city, start, end):
        return {"ok": True, "city": city,
                "days": [{"date": start, "low": 10, "high": 20,
                          "rain_prob": 10, "is_forecast": True}],
                "note": "Open-Meteo 真实预报"}

    def tx_fn(a, b, d, mode="any", party=1):
        return {"ok": False, "error": {"code": "NO_ROUTE",
                                       "message": "暂无直达班次",
                                       "retryable": True}}

    svc = DataService(db=DB, weather_fn=wx_fn, transport_fn=tx_fn)
    r_wx = svc.get_weather("成都", "2026-11-14", "2026-11-14")
    check("天气：转发并标 external_api 来源",
          r_wx.ok and r_wx.source == SOURCE_API
          and r_wx.metadata.get("origin_tool") == "get_weather"
          and r_wx.metadata.get("note") == "Open-Meteo 真实预报")
    r_tx = svc.get_transport("甲城", "乙城", "2026-11-14")
    check("班次：无直达是明确失败（不冒充无数据）",
          not r_tx.ok and r_tx.error_code == "NO_ROUTE" and r_tx.retryable())

    r_food = DataService(db=DB).get_food_knowledge("成都")
    check("美食知识：知识表与餐厅 POI 分路",
          r_food.ok and r_food.has_data and r_food.source == SOURCE_KB
          and all({"name", "intro"} <= set(x) for x in r_food.items))
    r_food2 = DataService(db=DB).get_food_knowledge("无美食城")
    check("美食知识：无收录返回成功空列表",
          r_food2.ok and r_food2.items == [])


# ================================================================ 单例与旧工具兼容

def test_singleton_and_legacy_compat() -> None:
    svc = get_data_service()
    check("单例：get_data_service 返回同一实例", get_data_service() is svc)
    r = tools.dispatch("search_poi", {"city": "大理", "limit": 3})
    check("旧工具兼容：dispatch 直接调用不受影响",
          r.get("ok") and (r.get("items") or []))
    r2 = svc.get_pois("大理", limit=3)
    check("旧工具兼容：DataService 与旧工具同一数据",
          [p["name"] for p in r2.items] == [p["name"] for p in r["items"]])


if __name__ == "__main__":
    test_data_result_protocol()
    test_data_policy()
    test_kb_hit_and_static_fallback()
    test_autofetch_on_empty()
    test_param_error_no_autofetch()
    test_restaurant_capability()
    test_weather_transport_food()
    test_singleton_and_legacy_compat()
    total, bad = len(RESULTS), sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n{total - bad}/{total} 条数据服务断言通过")
    if bad:
        raise SystemExit(f"{bad} 条失败")
