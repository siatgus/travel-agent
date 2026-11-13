"""RouteOptimizer 测试（文档 §6.2/§6.3）：最近邻、2-opt、硬顺序、确定性。"""

from _base import check, finish

from travel_agent.route_engine import DistanceMatrix, RouteNode, RouteOptimizer


def _n(id_: str, lat: float, lon: float, **kw) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=lat, longitude=lon, **kw)


# 合成城市：城心 (30.66, 104.06)，三点分布使其存在「交叉绕行」
#   A 东 2.7km / C 在 A 旁边 / B 西 3.8km —— 唯一最优序 A→C→B
HOME = [30.66, 104.06]
_A = _n("A", 30.66, 104.088)
_B = _n("B", 30.66, 104.020)
_C = _n("C", 30.66, 104.0893)


def test_nearest_neighbor() -> None:
    m = DistanceMatrix([_A, _B, _C], start_geo=HOME)
    opt = RouteOptimizer(m, 540, 1200)
    route, _ = opt.nearest_neighbor([_A, _B, _C])
    check("贪心：从起点最近者出发", route[0].id == "A")
    check("贪心：就近成团（A 之后是同侧的 C）",
          [n.id for n in route] == ["A", "C", "B"],
          str([n.id for n in route]))
    check("贪心：覆盖全部节点",
          sorted(n.id for n in route) == ["A", "B", "C"])


def test_two_opt_improves_crossing() -> None:
    m = DistanceMatrix([_A, _B, _C], start_geo=HOME)
    opt = RouteOptimizer(m, 540, 1200)
    crossing = [_A, _B, _C]          # 东→西→东：折返
    improved = opt.two_opt(crossing)
    # 最优序有两个等价解（A,C,B 与 C,A,B 总成本相同——差别只在
    # 「起点近」还是「终点近」），2-opt 按轨迹收敛到其一
    check("2-opt：识别折返并改进", improved is not None
          and [n.id for n in improved] in (["A", "C", "B"], ["C", "A", "B"]),
          str(improved and [n.id for n in improved]))
    check("2-opt：改进后成本严格更低",
          opt.route_cost(improved) < opt.route_cost(crossing))
    check("2-opt：改进序与另一最优序成本持平",
          opt.route_cost(improved) == opt.route_cost([_A, _C, _B]))
    # 已最优的顺序：2-opt 不动它（返回 None = 保持原序）
    best = [_A, _C, _B]
    check("2-opt：最优序不再改动", opt.two_opt(best) is None)


def test_optimize_not_worse_than_initial() -> None:
    m = DistanceMatrix([_A, _B, _C], start_geo=HOME)
    opt = RouteOptimizer(m, 540, 1200)
    initial = [_A, _C, _B]           # 已经是更优序
    route, algo = opt.optimize([_A, _B, _C], initial=initial)
    check("优化：不劣于初始解（保留原序）",
          [n.id for n in route] == ["A", "C", "B"] and algo in ("input", "2-opt"),
          f"algo={algo} order={[n.id for n in route]}")
    check("优化：成本不超过初始解",
          opt.route_cost(route) <= opt.route_cost(initial) + 1e-9)


def test_preferred_order_pinned() -> None:
    a = _n("A", 30.66, 104.088, preferred_order=0)
    b = _n("B", 30.66, 104.033, preferred_order=1)
    c = _n("C", 30.66, 104.0893)
    m = DistanceMatrix([a, b, c], start_geo=HOME)
    opt = RouteOptimizer(m, 540, 1200)
    route, _ = opt.optimize([c, b, a], initial=[c, b, a])
    pos = {n.id: i for i, n in enumerate(route)}
    check("硬顺序：用户指定相对顺序不被优化破坏",
          pos["A"] < pos["B"], str([n.id for n in route]))
    check("硬顺序：全部节点仍在", sorted(pos) == ["A", "B", "C"])


def test_geo_uncertain_passthrough() -> None:
    u = RouteNode(id="U", name="估算", geo_uncertain=True)
    m = DistanceMatrix([_A], start_geo=HOME)
    opt = RouteOptimizer(m, 540, 1200)
    route, algo = opt.optimize([_A, u], initial=[_A, u])
    check("估算点：混入估算坐标时原序返回不评估",
          [n.id for n in route] == ["A", "U"] and algo == "input")


def test_determinism() -> None:
    nodes = [_A, _B, _C]
    results = []
    for _ in range(3):
        m = DistanceMatrix(list(nodes), start_geo=HOME)
        opt = RouteOptimizer(m, 540, 1200)
        route, algo = opt.optimize(list(nodes))
        results.append(([n.id for n in route], algo))
    check("确定性：相同输入三次结果一致",
          results[0] == results[1] == results[2], str(results))


if __name__ == "__main__":
    test_nearest_neighbor()
    test_two_opt_improves_crossing()
    test_optimize_not_worse_than_initial()
    test_preferred_order_pinned()
    test_geo_uncertain_passthrough()
    test_determinism()
    finish("路线优化")
