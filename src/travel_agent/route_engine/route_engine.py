"""RouteEngine：路线优化统一入口（行野 2.0 第三阶段，文档 §10）。

完整链路 plan(request)：
    距离矩阵 → 优化顺序 → 时间表 → 约束检查 → RouteResult

供 composer 使用的轻量入口 optimize_order(picked, ...)：只做「顺序优化」，
不建时间表、不产出 RouteResult——composer 的时间轴装配（午餐/午休/
晚餐/闭馆顺延）保留原样，RouteEngine 只替换访问顺序这一步。

设计约束（审查结论）：
- 估算坐标点（geo_uncertain）不参与优化：成本算不准，原序返回；
- 优化结果不劣于原顺序（初始解 = composer 的紧迫度启发式）；
- 任何异常都回退原顺序并带原因——路线优化绝不能让规划链路挂掉；
- 开关关闭（TRAVEL_ROUTE_ENGINE=0）时直接原序返回。
"""

from __future__ import annotations

from .config import DEFAULT_END_MIN, DEFAULT_START_MIN, route_engine_enabled
from .constraint_checker import ConstraintChecker
from .distance_matrix import DistanceMatrix
from .models import RouteNode, RouteRequest, RouteResult
from .route_optimizer import RouteOptimizer
from .schedule_builder import ScheduleBuilder
from .time_window import TimeWindow

__all__ = ["RouteEngine", "optimize_order", "route_engine_enabled"]


class RouteEngine:
    """完整链路编排（独立使用 / 测试入口）。"""

    def __init__(self):
        self.window = TimeWindow()
        self.schedule_builder = ScheduleBuilder(self.window)
        self.checker = ConstraintChecker(self.window)

    def plan(self, request: RouteRequest) -> RouteResult:
        nodes = [n for n in request.nodes if n.id not in request.excluded]
        if request.max_visits:
            nodes = nodes[:request.max_visits] if not request.optimize \
                else nodes          # max_visits 由 checker 判定，优化器不裁剪
        matrix = DistanceMatrix(nodes, request.start_geo)
        optimizer = RouteOptimizer(matrix, _parse_hm(request.start_time),
                                   _parse_hm(request.end_time))
        if request.optimize:
            order, algo = optimizer.optimize(nodes)
        else:
            order, algo = list(nodes), "input"
        result = self.schedule_builder.build(
            order, matrix, _parse_hm(request.start_time),
            _parse_hm(request.end_time), request.start_geo, algorithm=algo)
        by_id = {n.id: n for n in nodes}
        self.checker.check_schedule(result, request, by_id)
        return result


def optimize_order(picked: list, home_geo: list[float] | None = None,
                   start_min: int = DEFAULT_START_MIN,
                   end_min: int = DEFAULT_END_MIN) -> tuple[list, str | None]:
    """composer 集成入口：优化当日景点访问顺序。

    picked 是 composer 的 POI dict 列表（非 RouteNode）——在这里做
    dict ↔ RouteNode 转换，composer 侧零感知。

    返回（新顺序, 算法名或 None）。任何情况下：
    - 返回序列与输入是同一集合的排列（绝不丢点/加点）；
    - 开关关闭 / 点数 ≤2 / 含估算点 / 任何异常 → 原序 + None。
    """
    if not route_engine_enabled() or len(picked) <= 1:
        return list(picked), None
    try:
        nodes = [_from_poi(p) for p in picked]
        if any(not n.has_geo() for n in nodes):
            return list(picked), None
        matrix = DistanceMatrix(nodes, home_geo)
        optimizer = RouteOptimizer(matrix, start_min, end_min)
        order, algo = optimizer.optimize(nodes)
        # 排列不变性守卫：优化器出 bug 也不能弄丢用户的点
        if sorted(n.id for n in order) != sorted(n.id for n in nodes):
            return list(picked), None
        return [_to_poi(n, p) for n, p in zip(order, _match_back(order, picked))], algo
    except Exception:
        return list(picked), None


# ---------------------------------------------------------------- 内部转换
def _from_poi(p: dict) -> RouteNode:
    geo = p.get("geo")
    uncertain = bool(p.get("geo_uncertain")) or not (
        isinstance(geo, (list, tuple)) and len(geo) == 2
        and all(isinstance(x, (int, float)) and x == x for x in geo))
    return RouteNode(
        id=str(p.get("poi_id") or p.get("name")),
        name=p.get("name") or "",
        latitude=geo[0] if not uncertain and geo else None,
        longitude=geo[1] if not uncertain and geo else None,
        visit_duration=int(p.get("duration_min") or 90),
        opening_time=p.get("open_hours") or None,
        priority=float(p.get("appeal") or 0),
        required=False,          # 必去占位在 _pick_chain 已完成，进到这里的都是当日点
        geo_uncertain=uncertain,
        cost=float(p.get("cost") or 0),
    )


def _match_back(order: list[RouteNode], picked: list) -> list[dict]:
    """优化后 RouteNode 顺序 → 对应的原始 POI dict 顺序。"""
    by_id: dict[str, dict] = {}
    dup: set[str] = set()
    for p in picked:
        key = str(p.get("poi_id") or p.get("name"))
        if key in by_id:
            dup.add(key)         # 同 id 两条（理论上不该有）：保守放弃优化
        by_id[key] = p
    if dup:
        raise ValueError("picked 存在重复 poi_id，放弃优化")
    return [by_id[n.id] for n in order]


def _to_poi(_node: RouteNode, p: dict) -> dict:
    return p                     # 只换顺序，dict 原样透传（composer 后续按引用用）


def _parse_hm(spec: str) -> int:
    """'09:00' → 540（分钟制）。"""
    try:
        h, m = (int(x) for x in spec.split(":")[:2])
        return h * 60 + m
    except (ValueError, AttributeError):
        return DEFAULT_START_MIN
