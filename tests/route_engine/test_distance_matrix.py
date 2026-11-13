"""DistanceMatrix 测试（文档 §6.1）：矩阵对称、来源标记、OSRM 配额、降级。"""

from _base import check, finish

from travel_agent.route_engine import DistanceMatrix, RouteNode
from travel_agent.route_engine import distance_matrix as dm


def _n(id_: str, lat: float, lon: float, **kw) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=lat, longitude=lon, **kw)


def test_matrix_estimate_only() -> None:
    # TRAVEL_ROUTE_OSRM=0（_base 已设）：全部走道路系数估算
    a = _n("A", 30.66, 104.05)
    b = _n("B", 30.65, 104.06)      # 相距约 1.4km
    m = DistanceMatrix([a, b])
    check("矩阵：建库自动完成", len(m._pair) == 1)
    check("矩阵：对称查询", m.get_duration("A", "B") == m.get_duration("B", "A"))
    check("矩阵：估算来源标记", m.leg("A", "B")["source"] == "estimate"
          and m.leg("A", "B")["estimated"] is True)
    check("矩阵：道路距离 ≥ 直线距离", m.get_distance_km("A", "B") >= 1.0)
    check("矩阵：耗时为正", m.get_duration("A", "B") > 0)
    check("矩阵：summary 统计", m.summary()["estimated"] == 1
          and m.summary()["osrm"] == 0 and m.estimated_ratio() == 1.0)

    unknown = m.leg("A", "X")
    check("矩阵：未知对返回 None 且数值接口安全",
          unknown is None and m.get_distance_km("A", "X") == 0.0
          and m.get_duration("A", "X") == 0)

    # 估算点不进矩阵
    u = RouteNode(id="U", name="估算", geo_uncertain=True)
    m2 = DistanceMatrix([a, u])
    check("矩阵：估算坐标节点不参与建库", len(m2._pair) == 0
          and m2.summary()["nodes"] == 2)


def test_matrix_osrm_source_and_cap() -> None:
    # 打桩 OSRM：前 2 对返回真实路网，验证来源标记与配额上限
    import os
    calls = {"n": 0}

    def fake_osrm(a, b):
        calls["n"] += 1
        if calls["n"] <= 2:
            return (round(_haversine(a, b) * 1.25, 2), 12)
        return None

    orig = dm._osrm_drive
    dm._osrm_drive = fake_osrm
    saved_cap = dm.OSRM_MAX_PAIRS
    dm.OSRM_MAX_PAIRS = 2
    saved_env = os.environ.get("TRAVEL_ROUTE_OSRM")
    os.environ["TRAVEL_ROUTE_OSRM"] = "1"       # 本用例临时放开 OSRM 闸
    try:
        nodes = [_n("A", 30.66, 104.05), _n("B", 30.65, 104.06),
                 _n("C", 30.64, 104.07)]
        m = DistanceMatrix(nodes)
        leg_ab = m.leg("A", "B")
        leg_ac = m.leg("A", "C")
        leg_bc = m.leg("B", "C")
        check("矩阵：配额内 OSRM 标记真实来源",
              leg_ab["source"] == "osrm" and leg_ab["estimated"] is False
              and leg_ac["source"] == "osrm")
        check("矩阵：超配额降级估算并标记",
              leg_bc["source"] == "estimate"
              and leg_bc["estimated"] is True)
        check("矩阵：OSRM 失败降级不崩溃", leg_bc["min"] > 0)
        check("矩阵：osrm/estimated 计数",
              m.osrm_calls == 2 and m.estimated_calls == 1)
    finally:
        dm._osrm_drive = orig
        dm.OSRM_MAX_PAIRS = saved_cap
        if saved_env is None:
            os.environ.pop("TRAVEL_ROUTE_OSRM", None)
        else:
            os.environ["TRAVEL_ROUTE_OSRM"] = saved_env


def test_matrix_from_start() -> None:
    a = _n("A", 30.66, 104.05)
    m = DistanceMatrix([a], start_geo=[30.6570, 104.0810])
    leg = m.from_start("A")
    check("矩阵：起点腿可用", leg is not None and leg["min"] > 0)
    m2 = DistanceMatrix([a])
    check("矩阵：未配置起点返回 None", m2.from_start("A") is None)


def _haversine(a, b):
    from travel_agent.tools import haversine_km
    return haversine_km(a, b)


if __name__ == "__main__":
    test_matrix_estimate_only()
    test_matrix_osrm_source_and_cap()
    test_matrix_from_start()
    finish("距离矩阵")
