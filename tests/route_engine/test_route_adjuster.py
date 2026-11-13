"""RouteAdjuster 测试（文档 §9）：增删改/顺序调整与失败回滚。"""

import copy

from _base import check, finish

from travel_agent.route_engine import RouteAdjuster, RouteNode

HOME = [30.66, 104.06]


def _n(id_: str, lat: float, lon: float, dur: int = 60,
       required: bool = False, **kw) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=lat, longitude=lon,
                     visit_duration=dur, required=required, **kw)


A = _n("A", 30.66, 104.088)          # 东 2.7km
B = _n("B", 30.66, 104.020)          # 西 3.8km
C = _n("C", 30.66, 104.0893)         # A 旁边（东 2.8km）


def test_add_node() -> None:
    adj = RouteAdjuster(540, 1200, HOME)
    before = [A, C]
    snapshot = [copy.deepcopy(n.__dict__) for n in before]
    new, algo = adj.add_node(before, B)
    check("增点：新点加入且全量保留",
          sorted(n.id for n in new) == ["A", "B", "C"])
    check("增点：局部重排有效（东西折返被消除）",
          [n.id for n in new] == ["A", "C", "B"],
          str([n.id for n in new]))
    check("增点：原路线对象不被修改（失败回滚保障）",
          [n.__dict__ for n in before] == snapshot)

    new2, _ = adj.add_node(before, A)      # 已存在：幂等
    check("增点：重复添加幂等", [n.id for n in new2] == ["A", "C"])


def test_remove_node() -> None:
    adj = RouteAdjuster(540, 1200, HOME)
    route = [A, C, B]
    new, _ = adj.remove_node(route, "B")
    check("删点：非必去点可删且重排", [n.id for n in new] == ["A", "C"])

    new2, _ = adj.remove_node(route, "X")   # 不在路线：幂等
    check("删点：不存在则原样返回", [n.id for n in new2] == ["A", "C", "B"])

    must = _n("M", 30.66, 104.05, required=True)
    route_m = [A, must]
    new3, _ = adj.remove_node(route_m, "M")
    check("删点：必去点不允许静默删除", [n.id for n in new3] == ["A", "M"])


def test_replace_node() -> None:
    adj = RouteAdjuster(540, 1200, HOME)
    d = _n("D", 30.66, 104.050)      # 城心附近
    new, _ = adj.replace_node([A, B], "B", d)
    check("换点：旧点移除新点加入", [n.id for n in new] == ["A", "D"]
          or sorted(n.id for n in new) == ["A", "D"])
    check("换点：换掉的点不再出现", all(n.id != "B" for n in new))
    new2, _ = adj.replace_node([A], "X", d)
    check("换点：旧点不存在原样返回", [n.id for n in new2] == ["A"])


def test_reorder() -> None:
    adj = RouteAdjuster(540, 1200, HOME)
    route = [A, B, C]
    new, _ = adj.reorder(route, ["B", "A"])
    pos = {n.id: i for i, n in enumerate(new)}
    check("调序：用户顺序为硬约束", pos["B"] < pos["A"],
          str([n.id for n in new]))
    check("调序：全部节点保留", sorted(pos) == ["A", "B", "C"])
    check("调序：入参路线对象不被修改", [n.id for n in route] == ["A", "B", "C"])


def test_uncertain_untouched() -> None:
    adj = RouteAdjuster(540, 1200, HOME)
    u = RouteNode(id="U", name="估算", geo_uncertain=True)
    new, algo = adj.add_node([A], u)
    check("估算点：加入估算点不参与优化（原相对顺序保持）",
          [n.id for n in new] == ["A", "U"] and algo == "input",
          f"algo={algo}")


if __name__ == "__main__":
    test_add_node()
    test_remove_node()
    test_replace_node()
    test_reorder()
    test_uncertain_untouched()
    finish("动态调整")
