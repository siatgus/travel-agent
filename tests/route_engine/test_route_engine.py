"""RouteEngine 完整链路与 composer 集成入口测试（文档 §10/§11/§13.2 场景）。

含八个场景测试的落地点：普通三景点、必去、营业时间冲突、增删景点、
路线服务不可用（全程估算降级）、指定顺序、路线不可行。
"""

from _base import check, finish

from travel_agent.route_engine import (
    DistanceMatrix, RouteAdjuster, RouteEngine, RouteNode, RouteRequest,
    TimeWindow, optimize_order, route_engine_enabled,
)

HOME = [30.66, 104.06]


def _n(id_: str, lat: float, lon: float, dur: int = 60,
       hours: str | None = None, required: bool = False) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=lat, longitude=lon,
                     visit_duration=dur, opening_time=hours,
                     required=required)


def test_full_plan_normal() -> None:
    """场景一：普通三景点路线（交叉 → 优化消除）。"""
    eng = RouteEngine()
    a = _n("A", 30.66, 104.088, 120)
    b = _n("B", 30.66, 104.020, 90)
    c = _n("C", 30.66, 104.0893, 90)
    req = RouteRequest(nodes=[a, b, c], start_geo=HOME,
                       start_time="09:00", end_time="20:00")
    r = eng.plan(req)
    check("场景一：全链路成功", r.success and len(r.stops) == 3, str(r.errors))
    check("场景一：交叉序被优化为 A→C→B",
          r.order == ["A", "C", "B"], str(r.order))
    check("场景一：时间表单调不回退",
          all(s.arrival_time <= s.departure_time for s in r.stops))
    check("场景一：汇总口径齐备",
          r.total_visit_time == 300 and r.total_travel_time > 0
          and r.algorithm in ("nearest_neighbor", "2-opt"),
          f"algo={r.algorithm}")


def test_must_visit_and_order() -> None:
    """场景二 + 场景七：必去保留、指定顺序不被优化破坏。"""
    eng = RouteEngine()
    a = _n("A", 30.66, 104.088, required=True)
    b = _n("B", 30.66, 104.020, required=True)
    c = _n("C", 30.66, 104.0893)
    c.preferred_order = 0
    a.preferred_order = 1
    req = RouteRequest(nodes=[a, b, c], must_visit=["A", "B"], start_geo=HOME)
    r = eng.plan(req)
    check("场景二：必去节点全保留", set(r.order) >= {"A", "B"}, str(r.order))
    check("场景七：用户指定顺序 C→A 成立",
          r.order.index("C") < r.order.index("A"), str(r.order))
    check("场景二：约束检查通过", r.success and r.errors == [], str(r.errors))


def test_closed_conflict() -> None:
    """场景三：09:00 出发但第一站 10:30 才开门 → 等待；闭馆 → 冲突说明。"""
    eng = RouteEngine()
    late = _n("L", 30.66, 104.088, 60, hours="10:30-18:00")
    req = RouteRequest(nodes=[late], start_geo=HOME, start_time="09:00")
    r = eng.plan(req)
    check("场景三：早到给出等待时间", r.total_wait_time >= 70
          and any("早到" in w for w in r.warnings), str(r.warnings))
    # 已闭馆才出发：返回冲突而不是伪造行程（必去点 → 硬约束失败）
    req2 = RouteRequest(nodes=[_n("Z", 30.66, 104.088, 60, "07:00-08:00")],
                        must_visit=["Z"], start_geo=HOME, start_time="09:00")
    r2 = eng.plan(req2)
    check("场景三：闭馆冲突如实报告", not r2.success
          and any("无法入场" in e or "已闭馆" in w
                  for e in r2.errors for w in r2.warnings),
          f"err={r2.errors} warn={r2.warnings}")


def test_add_remove_scenarios() -> None:
    """场景四 + 场景五：增加/删除景点只动给定序列。"""
    adj = RouteAdjuster(540, 1200, HOME)
    a = _n("A", 30.66, 104.088, 120)
    b = _n("B", 30.66, 104.020, 90)
    c = _n("C", 30.66, 104.0893, 90)
    d = _n("D", 30.66, 104.050, 60)
    # 场景四：第二天（给定序列）加一个点，重排不丢点
    added, _ = adj.add_node([a, c], d)
    check("场景四：加点后重排且不丢点",
          sorted(n.id for n in added) == ["A", "C", "D"])
    check("场景四：加点后相邻不再东西折返",
          [n.id for n in added] == ["A", "C", "D"]
          or [n.id for n in added] == ["D", "A", "C"],
          str([n.id for n in added]))
    # 场景五：删掉一个非必去点，其余重排
    removed, _ = adj.remove_node([a, c, b], "B")
    check("场景五：删点后保留其余并优化",
          [n.id for n in removed] == ["A", "C"], str([n.id for n in removed]))


def test_osrm_down_degrades() -> None:
    """场景六：路线服务不可用 → 全程估算降级 + 数据可信度标记。"""
    # _base 已把 _osrm_drive 打桩为 None（模拟服务全挂）
    m = DistanceMatrix([_n("A", 30.66, 104.088), _n("B", 30.66, 104.020)],
                       start_geo=HOME)
    check("场景六：OSRM 不可达时全部估算", m.estimated_ratio() == 1.0
          and m.leg("A", "B")["estimated"] is True)
    eng = RouteEngine()
    req = RouteRequest(nodes=[_n("A", 30.66, 104.088, 60),
                              _n("B", 30.66, 104.020, 60)], start_geo=HOME)
    r = eng.plan(req)
    check("场景六：降级不崩溃且成功出表", r.success and len(r.stops) == 2)
    check("场景六：估算段数如实标记", r.estimated_legs == 2
          and r.osrm_legs == 0)


def test_infeasible_reports() -> None:
    """场景八：约束无法同时满足 → 冲突说明，不伪造可行路线。"""
    eng = RouteEngine()
    a = _n("A", 30.66, 104.088, 120, required=True, hours="07:00-08:00")
    req = RouteRequest(nodes=[a], must_visit=["A"], start_geo=HOME,
                       start_time="09:00")
    r = eng.plan(req)
    check("场景八：不可行如实报失败", not r.success and r.errors,
          f"err={r.errors} warn={r.warnings}")


# ================================================================ composer 集成入口

def test_optimize_order_poi_dicts() -> None:
    """composer 集成形态：POI dict 进、POI dict 出、排列不变。"""
    a = {"poi_id": "P1", "name": "甲景点", "geo": [30.66, 104.088],
         "duration_min": 120, "open_hours": "全天", "appeal": 4, "cost": 0}
    b = {"poi_id": "P2", "name": "乙景点", "geo": [30.66, 104.020],
         "duration_min": 90, "open_hours": "全天", "appeal": 4, "cost": 0}
    c = {"poi_id": "P3", "name": "丙景点", "geo": [30.66, 104.0893],
         "duration_min": 90, "open_hours": "全天", "appeal": 4, "cost": 0}
    out, algo = optimize_order([a, b, c], home_geo=HOME)
    check("集成：POI dict 进出，返回同一集合的排列",
          [p["poi_id"] for p in out] == ["P1", "P3", "P2"]
          and all(p is orig for p, orig in zip(out, [a, c, b])),
          str([p["poi_id"] for p in out]) + f" algo={algo}")
    check("集成：算法名上报（NN 已最优则 2-opt 无改进空间）",
          algo in ("nearest_neighbor", "2-opt"), str(algo))
    # 单点/空：原样返回
    check("集成：单点原样返回", optimize_order([a], HOME)[1] is None
          and optimize_order([a], HOME)[0] == [a])

    # 估算点：原序返回（不评估）
    u = {"poi_id": "P4", "name": "估算点", "geo": [30.66, 104.06],
         "duration_min": 90, "open_hours": "全天", "geo_uncertain": True}
    out2, algo2 = optimize_order([a, u], home_geo=HOME)
    check("集成：估算点原序返回", [p["poi_id"] for p in out2] == ["P1", "P4"]
          and algo2 is None)

    # 重复 poi_id：放弃优化（守卫）
    out3, algo3 = optimize_order([a, dict(a)], home_geo=HOME)
    check("集成：重复 id 放弃优化", algo3 is None
          and [p["poi_id"] for p in out3] == ["P1", "P1"])


def test_switch_disabled() -> None:
    """回滚开关：TRAVEL_ROUTE_ENGINE=0 时原样返回。"""
    import importlib
    from travel_agent.route_engine import config
    a = {"poi_id": "P1", "name": "甲", "geo": [30.66, 104.088],
         "duration_min": 90, "open_hours": "全天"}
    b = {"poi_id": "P2", "name": "乙", "geo": [30.66, 104.020],
         "duration_min": 90, "open_hours": "全天"}
    saved = route_engine_enabled()
    try:
        import os
        os.environ["TRAVEL_ROUTE_ENGINE"] = "0"
        importlib.reload(config)
        check("开关：关闭后 route_engine_enabled 为 False",
              config.route_engine_enabled() is False)
        out, algo = optimize_order([a, b], home_geo=HOME)
        check("开关：关闭时原序返回且不调优化",
              [p["poi_id"] for p in out] == ["P1", "P2"] and algo is None)
    finally:
        import os
        os.environ.pop("TRAVEL_ROUTE_ENGINE", None)
        importlib.reload(config)
        check("开关：恢复默认开启", config.route_engine_enabled() is True)


if __name__ == "__main__":
    test_full_plan_normal()
    test_must_visit_and_order()
    test_closed_conflict()
    test_add_remove_scenarios()
    test_osrm_down_degrades()
    test_infeasible_reports()
    test_optimize_order_poi_dicts()
    test_switch_disabled()
    finish("RouteEngine 链路")
