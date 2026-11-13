"""ConstraintChecker 测试（文档 §8）：硬/软约束与 RouteResult 合并。"""

from _base import check, finish

from travel_agent.route_engine import (
    ConstraintChecker, RouteNode, RouteRequest, RouteResult, RouteStop,
)


def _n(id_: str, **kw) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=30.66, longitude=104.05, **kw)


def test_hard_constraints() -> None:
    a, b, c = _n("A"), _n("B"), _n("C")
    req = RouteRequest(nodes=[a, b, c], must_visit=["A", "B"])
    cc = ConstraintChecker()

    v, _ = cc.check_route([a, b, c], req)
    check("硬约束：必去齐全无违规", v == [])

    v, _ = cc.check_route([a, c], req)
    check("硬约束：缺必去报违规", any("B" in x for x in v), str(v))

    req_ex = RouteRequest(nodes=[a, b, c], excluded=["B"])
    v, _ = cc.check_route([a, b], req_ex)
    check("硬约束：排除点出现报违规", any("B" in x for x in v), str(v))

    a2 = _n("A", preferred_order=0)
    b2 = _n("B", preferred_order=1)
    req_ord = RouteRequest(nodes=[a2, b2], optimize=False)
    v, _ = cc.check_route([a2, b2], req_ord)
    check("硬约束：指定顺序满足", v == [])
    v2, _ = cc.check_route([b2, a2], req_ord)
    check("硬约束：指定顺序被破坏报违规", any("顺序" in x for x in v2), str(v2))


def test_soft_constraints() -> None:
    a = _n("A", visit_duration=8 * 60)
    b = _n("B", visit_duration=8 * 60)
    req = RouteRequest(nodes=[a, b])
    cc = ConstraintChecker(start_min=9 * 60, end_min=20 * 60)
    _, w = cc.check_route([a, b], req)
    check("软约束：净游玩超窗口只警告不违规",
          len(w) == 1 and not cc.check_route([a, b], req)[0], str(w))


def test_check_schedule_merge() -> None:
    a = _n("A", preferred_order=0)
    b = _n("B", preferred_order=1)
    req = RouteRequest(nodes=[a, b])
    result = RouteResult(order=["B", "A"], algorithm="2-opt")
    cc = ConstraintChecker()
    out = cc.check_schedule(result, req, {"A": a, "B": b})
    check("合并：顺序违规进 errors 且 success=False",
          not out.success and any("顺序" in e for e in out.errors),
          str(out.errors))

    ok = RouteResult(order=["A", "B"], algorithm="input")
    out2 = cc.check_schedule(ok, req, {"A": a, "B": b})
    check("合并：合规路线 success=True 且无错误",
          out2.success and out2.errors == [])


def test_required_nodes_helper() -> None:
    a, b = _n("A"), _n("B")
    req = RouteRequest(nodes=[a, b], must_visit=["A"])
    cc = ConstraintChecker()
    check("快捷项：必去覆盖检查", cc.check_required_nodes([a], req) == []
          and cc.check_required_nodes([b], req) == ["A"])


if __name__ == "__main__":
    test_hard_constraints()
    test_soft_constraints()
    test_check_schedule_merge()
    test_required_nodes_helper()
    finish("约束检查")
