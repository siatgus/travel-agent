"""RouteNode / RouteRequest / RouteResult 数据模型测试（文档 §5）。"""

from _base import check, finish

from travel_agent.route_engine import (
    COORD_GCJ02, COORD_WGS84, RouteNode, RouteRequest, RouteResult, RouteStop,
)


def test_route_node() -> None:
    n = RouteNode(id="A", name="宽窄巷子", latitude=30.66, longitude=104.05,
                  visit_duration=120, opening_time="08:30-18:00")
    check("模型：正常构造与默认值", n.coordinate_system == COORD_WGS84
          and n.priority == 0 and n.required is False)
    check("模型：geo 出口 [lat, lon]", n.geo == [30.66, 104.05] and n.has_geo())

    for bad in ((None, 104.0), (30.0, None), (100.0, 104.0),
                (30.0, 900.0), ("abc", 104.0), (float("nan"), 104.0)):
        try:
            RouteNode(id="B", name="坏点", latitude=bad[0], longitude=bad[1])
            ok = False
        except ValueError:
            ok = True
        check(f"模型：非法坐标 ({bad[0]}, {bad[1]}) 构造被拒", ok)

    try:
        RouteNode(id="", name="无ID")
        ok = False
    except ValueError:
        ok = True
    check("模型：空 id 被拒", ok)

    try:
        RouteNode(id="C", name="负时长", latitude=30.0, longitude=104.0,
                  visit_duration=-5)
        ok = False
    except ValueError:
        ok = True
    check("模型：负游玩时长被拒", ok)

    u = RouteNode(id="D", name="估算点", latitude=30.0, longitude=104.0,
                  geo_uncertain=True)
    check("模型：估算点坐标被清空（防下游误用）",
          u.geo is None and not u.has_geo())

    try:
        RouteNode(id="E", name="错坐标系", latitude=30.0, longitude=104.0,
                  coordinate_system="BD-09")
        ok = False
    except ValueError:
        ok = True
    check("模型：未知坐标系被拒", ok)
    g = RouteNode(id="F", name="高德点", latitude=30.0, longitude=104.0,
                  coordinate_system=COORD_GCJ02)
    check("模型：GCJ-02 可显式标注（调用方负责先纠偏）",
          g.coordinate_system == COORD_GCJ02)


def test_route_request() -> None:
    a = RouteNode(id="A", name="A", latitude=30.66, longitude=104.05)
    b = RouteNode(id="B", name="B", latitude=30.65, longitude=104.06)
    r = RouteRequest(nodes=[a, b], must_visit=["A"], excluded=[],
                     start_time="09:00", end_time="20:00")
    check("请求：正常构造", r.max_visits is None and r.optimize is True)

    try:
        RouteRequest(nodes=[a, b], must_visit=["X"])
        ok = False
    except ValueError:
        ok = True
    check("请求：必去节点不在 nodes 里被拒", ok)

    try:
        RouteRequest(nodes=[a, b], excluded=["Z"])
        ok = False
    except ValueError:
        ok = True
    check("请求：排除节点不在 nodes 里被拒", ok)

    try:
        RouteRequest(nodes=[a, b], max_visits=-1)
        ok = False
    except ValueError:
        ok = True
    check("请求：负 max_visits 被拒", ok)

    r2 = RouteRequest(nodes=[a, b], must_visit=[""], excluded=[None])
    check("请求：空字符串/None 的约束项被过滤",
          r2.must_visit == [] and r2.excluded == [])


def test_route_result() -> None:
    a = RouteNode(id="A", name="A", latitude=30.66, longitude=104.05)
    res = RouteResult(stops=[RouteStop(node=a, arrival_time="09:00",
                                       departure_time="10:30")])
    check("结果：order 从 stops 自动派生", res.order == ["A"] and res.success)
    res2 = RouteResult(order=["B", "A"])
    check("结果：显式 order 优先", res2.order == ["B", "A"]
          and res2.stops == [] and res2.algorithm == "none")


if __name__ == "__main__":
    test_route_node()
    test_route_request()
    test_route_result()
    finish("模型")
