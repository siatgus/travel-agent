"""RouteEngine：路线优化统一入口（晴航 2.0 第三阶段，文档 §10）。

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
        dropped: list[str] = []
        if request.max_visits:
            # max_visits 是"最多去几个点"，必须真的裁剪——旧实现 optimize=True
            # 时不裁剪直接交给 checker 判违规，结果永远是 success=False，
            # 这个参数在优化模式下等于不可用。裁剪规则：必去全保，其余按
            # 输入序补足；必去本身超限时保必去，让 checker 如实报不可行。
            nodes, dropped = _cap_visits(nodes, request.max_visits)
        start_min, end_min = _parse_hm(request.start_time), _parse_hm(request.end_time)
        matrix = DistanceMatrix(nodes, request.start_geo)
        optimizer = RouteOptimizer(matrix, start_min, end_min)
        if request.optimize:
            order, algo = optimizer.optimize(nodes)
        else:
            order, algo = list(nodes), "input"
        result = self.schedule_builder.build(
            order, matrix, start_min, end_min, request.start_geo, algorithm=algo)
        by_id = {n.id: n for n in nodes}
        # 约束检查按请求的时间窗判（旧实现用 checker 默认 9:00-20:00，
        # 自定义 start/end 时"净游玩超窗口"永远按 11 小时口径算错）
        checker = ConstraintChecker(self.window, start_min=start_min,
                                    end_min=end_min)
        checker.check_schedule(result, request, by_id)
        if dropped:
            result.warnings.append(
                "受 max_visits 限制未纳入：" + "、".join(dropped))
        return result


def optimize_order(picked: list, home_geo: list[float] | None = None,
                   start_min: int = DEFAULT_START_MIN,
                   end_min: int = DEFAULT_END_MIN,
                   preferred_names: list[str] | None = None
                   ) -> tuple[list, str | None]:
    """composer 集成入口：优化当日景点访问顺序。

    picked 是 composer 的 POI dict 列表（非 RouteNode）——在这里做
    dict ↔ RouteNode 转换，composer 侧零感知。

    preferred_names：用户指定的先后顺序（口语里的「先去A再去B」）。
    命中 ≥2 个点时写进 preferred_order 硬约束——贪心骨架插入、2-opt
    违约重罚都会遵守它（文档 §9/§13.2 场景七）。

    返回（新顺序, 算法名或 None）。任何情况下：
    - 返回序列与输入是同一集合的排列（绝不丢点/加点）；
    - 开关关闭 / 点数 ≤2 / 含估算点 / 任何异常 → 原序 + None。
    """
    if not route_engine_enabled() or len(picked) <= 1:
        return list(picked), None
    try:
        nodes = [_from_poi(p) for p in picked]
        if preferred_names:
            _apply_user_order(nodes, preferred_names)
        if any(not n.has_geo() for n in nodes):
            return list(picked), None
        matrix = DistanceMatrix(nodes, home_geo)
        optimizer = RouteOptimizer(matrix, start_min, end_min)
        order, algo = optimizer.optimize(nodes)
        # 排列不变性守卫：优化器出 bug 也不能弄丢用户的点
        if sorted(n.id for n in order) != sorted(n.id for n in nodes):
            return list(picked), None
        # 硬顺序守卫：即使成本模型有 bug 也不许破坏用户点名顺序
        if not _order_kept(order):
            return list(picked), None
        return [_to_poi(n, p) for n, p in zip(order, _match_back(order, picked))], algo
    except Exception:
        return list(picked), None


def _apply_user_order(nodes: list[RouteNode],
                      preferred_names: list[str]) -> None:
    """把口语顺序映射到节点的 preferred_order（name_hit 双向包含匹配）。"""
    from ..composer import name_hit
    matched: list[RouteNode] = []
    for name in preferred_names:
        for n in nodes:
            if n.preferred_order is None and name_hit(name, n.name):
                matched.append(n)
                break
    if len(matched) >= 2:
        for rank, n in enumerate(matched):
            n.preferred_order = rank


def _order_kept(order: list[RouteNode]) -> bool:
    """preferred_order 相对顺序是否成立（优化结果最后一道守卫）。"""
    pinned = [n for n in order if n.preferred_order is not None]
    if len(pinned) < 2:
        return True
    ranks = [n.preferred_order for n in pinned]
    return ranks == sorted(ranks)


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


def _cap_visits(nodes: list[RouteNode],
                max_visits: int) -> tuple[list[RouteNode], list[str]]:
    """按 max_visits 裁剪：必去全保，其余按输入序补足。

    返回（保留节点, 被裁掉的 id）。必去数量本身超限时保必去——
    让约束检查如实报"不可行"，而不是悄悄丢用户的必去点。
    """
    if len(nodes) <= max_visits:
        return nodes, []
    required = {n.id for n in nodes if n.required}
    keep = [n for n in nodes if n.id in required]
    rest = [n for n in nodes if n.id not in required]
    keep.extend(rest[:max(0, max_visits - len(keep))])
    keep_ids = {n.id for n in keep}
    dropped = [n.id for n in nodes if n.id not in keep_ids]
    return keep, dropped


def _parse_hm(spec: str) -> int:
    """'09:00' → 540（分钟制）。"""
    try:
        h, m = (int(x) for x in spec.split(":")[:2])
        return h * 60 + m
    except (ValueError, AttributeError):
        return DEFAULT_START_MIN
