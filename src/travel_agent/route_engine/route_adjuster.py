"""动态路线调整（行野 2.0 第三阶段，文档 §9）。

原则：
- 用户明确指定的顺序/时间/必去是硬约束，不为省路程破坏
  （preferred_order 全程钉死，见 RouteOptimizer）；
- 调整失败时保留原方案——所有方法返回新对象，入参 current_route
  永不被修改（失败回滚 = 天然成立）；
- 调整只作用于给定的这一天，不触碰其他日期（调用方保证）；
- 调整后必须重新走 ConstraintChecker（route_engine.plan 统一收口）。

接入边界（2026-11-16 核对）：本模块引擎层已完成并有 14 条断言，但
**对话层尚未接线**——「第N天别去X / 倒一下顺序」目前走整单重排
（orchestrator 规划管道），功能正确但慢（重跑 plan_trip）。
直接接线需要先把 composer.compose 内联的按天时间轴装配（午餐插播、
儿童午休、返程截止顺延）抽成可单天重放的函数，否则局部改序后
餐饮/午休的位置就是错的。该重构属于设计文档 §17 第五阶段
（Dynamic Trip Management）的入口工作，届时以本模块为执行内核。
"""

from __future__ import annotations

from .distance_matrix import DistanceMatrix
from .models import RouteNode
from .route_optimizer import RouteOptimizer


class RouteAdjuster:

    def __init__(self, start_min: int = 9 * 60, end_min: int = 20 * 60,
                 start_geo: list[float] | None = None):
        self.start_min = start_min
        self.end_min = end_min
        self.start_geo = start_geo

    def _optimize(self, nodes: list[RouteNode],
                 initial: list[RouteNode] | None = None
                 ) -> tuple[list[RouteNode], str]:
        matrix = DistanceMatrix(nodes, self.start_geo)
        opt = RouteOptimizer(matrix, self.start_min, self.end_min)
        return opt.optimize(nodes, initial)

    # ------------------------------------------------------------ 增
    def add_node(self, current_route: list[RouteNode],
                 node: RouteNode) -> tuple[list[RouteNode], str]:
        """加入一个景点并局部重排（原路线对象不被修改）。"""
        if any(n.id == node.id for n in current_route):
            return list(current_route), "input"      # 已在路线：幂等
        nodes = list(current_route) + [node]
        return self._optimize(nodes, current_route)

    # ------------------------------------------------------------ 删
    def remove_node(self, current_route: list[RouteNode],
                    node_id: str) -> tuple[list[RouteNode], str]:
        rest = [n for n in current_route if n.id != node_id]
        if len(rest) == len(current_route):
            return list(current_route), "input"      # 不在路线：幂等
        if any(n.required for n in current_route if n.id == node_id):
            # 必去点不允许被静默删除：返回原路线（调用方应向用户摊牌）
            return list(current_route), "input"
        return self._optimize(rest, rest)

    # ------------------------------------------------------------ 换
    def replace_node(self, current_route: list[RouteNode], old_id: str,
                     new_node: RouteNode) -> tuple[list[RouteNode], str]:
        rest = [n for n in current_route if n.id != old_id]
        if len(rest) == len(current_route):
            return list(current_route), "input"
        return self.add_node(rest, new_node)

    # ------------------------------------------------------------ 序
    def reorder(self, current_route: list[RouteNode],
                preferred_order: list[str]) -> tuple[list[RouteNode], str]:
        """按用户给的 id 顺序固定相对次序，其余点在空位插空。"""
        by_id = {n.id: n for n in current_route}
        pinned = [by_id[i] for i in preferred_order
                  if i in by_id and by_id[i].preferred_order is None]
        # 把用户顺序写进节点的 preferred_order（副本上改，不动入参）
        copies: dict[str, RouteNode] = {}
        for n in current_route:
            c = RouteNode(**{**n.__dict__})
            copies[c.id] = c
        for rank, n in enumerate(pinned):
            copies[n.id].preferred_order = rank
        nodes = [copies[n.id] for n in current_route]
        return self._optimize(nodes, nodes)
