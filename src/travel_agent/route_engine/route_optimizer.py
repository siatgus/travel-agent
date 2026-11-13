"""路线优化器（行野 2.0 第三阶段，文档 §6.2/§6.3）。

第一版：最近邻贪心 + 2-opt 局部改进，交通分钟为主指标、时间窗口
为惩罚项（顺路但闭馆的顺序会被重罚否决）。

硬约束（不得为省路程破坏）：
- required 节点必须全在结果里（优化只重排、不删点——排列不变性）；
- preferred_order 非空的节点按用户指定相对顺序固定：贪心用
  「骨架 + 空隙插入」生成、2-opt 用成本重罚否决违约候选；
- geo_uncertain 节点不参与成本计算——它们的位置本身就是估算，
  优化它们等于优化噪声；含估算点的序列直接返回原顺序。

确定性（文档 §14.9）：同输入同输出——遍历顺序固定、并列用输入序打破。
"""

from __future__ import annotations

from itertools import combinations

from .config import CLOSED_VIOLATION_PENALTY_MIN, TWO_OPT_MAX_ITERS, WAIT_COST_FACTOR
from .distance_matrix import DistanceMatrix
from .time_window import TimeWindow
from .models import RouteNode


class RouteOptimizer:

    def __init__(self, matrix: DistanceMatrix,
                 start_min: int = 9 * 60, end_min: int = 20 * 60,
                 window: TimeWindow | None = None):
        self.matrix = matrix
        self.window = window or TimeWindow()
        self.start_min = start_min
        self.end_min = end_min

    # ------------------------------------------------------------ 入口
    def optimize(self, nodes: list[RouteNode],
                 initial: list[RouteNode] | None = None) -> tuple[list[RouteNode], str]:
        """返回（优化后顺序, 算法名）。

        initial：调用方的启发式顺序（composer 的紧迫度排序）。
        成本不劣于 initial 时保留 initial——「优化结果不劣于原始」。
        """
        if len(nodes) <= 1:
            return list(nodes), "input"
        # 位置估算的点混在序列里：成本算不准，不做任何重排（完整返回输入序）
        if any(not n.has_geo() for n in nodes):
            return list(nodes), "input"
        base = self._complete(list(initial or nodes), nodes)
        base_cost = self.route_cost(base)
        nn, _ = self.nearest_neighbor(nodes)
        best, best_cost, algo = base, base_cost, "input"
        c = self.route_cost(nn)
        if c < best_cost - 1e-9:
            best, best_cost, algo = nn, c, "nearest_neighbor"
        opt = self.two_opt(best)
        if opt is not None:
            c = self.route_cost(opt)
            if c < best_cost - 1e-9:
                best, best_cost, algo = opt, c, "2-opt"
        return best, algo

    # ------------------------------------------------------------ 最近邻
    def nearest_neighbor(self, nodes: list[RouteNode]) -> tuple[list[RouteNode], int]:
        """最近邻贪心 + preferred_order 骨架空隙插入。

        用户指定顺序的节点构成骨架（按 preferred_order 相对顺序固定），
        其余节点贪心插空——贪心排序绝不破坏硬顺序。
        起点 = start_geo 距离最近者（无起点取输入首个）。
        """
        reliable = [n for n in nodes if n.has_geo()]
        if not reliable:
            return list(nodes), 0
        pinned = sorted((n for n in reliable if n.preferred_order is not None),
                        key=lambda n: n.preferred_order)
        free = [n for n in reliable if n.preferred_order is None]
        if pinned and free:
            free_route, _ = self._nn_free(free, pinned[0].geo)
            return self._interleave(pinned, free_route), 0
        if pinned:
            return list(pinned), 0
        return self._nn_free(reliable, self.matrix.start_geo)

    def _nn_free(self, free: list[RouteNode],
                 start_geo: list[float] | None) -> tuple[list[RouteNode], int]:
        if not free:
            return [], 0
        if start_geo:
            head = min(free, key=lambda n: (_dist_km(start_geo, n.geo),
                                            free.index(n)))
        else:
            head = free[0]
        route = [head]
        remaining = [n for n in free if n is not head]
        while remaining:
            cur = route[-1]
            nxt = min(remaining,
                      key=lambda n: (self.matrix.get_duration(cur.id, n.id),
                                     _dist_km(cur.geo, n.geo),
                                     remaining.index(n)))
            route.append(nxt)
            remaining.remove(nxt)
        return route, 0

    def _interleave(self, skeleton: list[RouteNode],
                    free_route: list[RouteNode]) -> list[RouteNode]:
        """把自由节点贪心插进骨架的空隙，取每步成本最小的位置。"""
        out = list(skeleton)
        for n in free_route:
            best_pos, best_cost = None, None
            for pos in range(len(out) + 1):
                cand = out[:pos] + [n] + out[pos:]
                c = self.route_cost(cand)
                if best_cost is None or c < best_cost - 1e-9:
                    best_pos, best_cost = pos, c
            out = out[:best_pos] + [n] + out[best_pos:]
        return out

    # ------------------------------------------------------------ 2-opt
    def two_opt(self, route: list[RouteNode]) -> list[RouteNode] | None:
        """段反转改进；无改进返回 None（调用方保留原序）。

        preferred_order 违约由 route_cost 重罚否决——2-opt 不会破坏硬顺序。
        迭代上限 TWO_OPT_MAX_ITERS 防失控（文档 §14.1）。
        """
        best = list(route)
        best_cost = self.route_cost(best)
        improved = True
        iters = 0
        while improved and iters < TWO_OPT_MAX_ITERS:
            improved = False
            iters += 1
            for i, j in combinations(range(len(best)), 2):
                cand = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                c = self.route_cost(cand)
                if c < best_cost - 1e-9:
                    best, best_cost = cand, c
                    improved = True
                    if iters >= TWO_OPT_MAX_ITERS:
                        break
        return best if best_cost < self.route_cost(route) - 1e-9 else None

    # ------------------------------------------------------------ 成本
    def route_cost(self, order: list[RouteNode]) -> float:
        """顺序总成本（分钟）：Σ交通 + 早到等待×系数 + 时间窗口/硬顺序违约重罚。

        起点段用 matrix.from_start（start_geo 未配置时按 0 计）。
        """
        cost = 0.0
        clock = self.start_min
        prev_id: str | None = None
        lunch_done = False
        for n in order:
            if not n.has_geo():
                return float("inf")     # 估算点混入：该顺序不可评估
            if prev_id is None:
                leg = self.matrix.from_start(n.id) or {}
            else:
                leg = self.matrix.leg(prev_id, n.id) or {}
            tmin = int(leg.get("min") or 0)
            cost += tmin
            clock += tmin
            # 午餐缓冲：装配层（composer）跨饭点时会插一顿 ~60 分钟的饭，
            # 成本模型不算它就会低估后续时刻——2-opt 会把大时长景点排到
            # 队尾然后被返程截止顺延掉（杭州单日西湖实测）。
            if not lunch_done and 11 * 60 <= clock <= 13 * 60 + 30:
                clock += 60
                lunch_done = True
            # 时间窗口
            slot = self.window.find_next_available(n, clock)
            if slot is None:
                cost += CLOSED_VIOLATION_PENALTY_MIN     # 已闭馆进不去
            else:
                enter, wait = slot
                cost += wait * WAIT_COST_FACTOR
                clock = enter
                if clock + n.visit_duration > (self.window.window(n) or (0, self.end_min))[1]:
                    cost += CLOSED_VIOLATION_PENALTY_MIN  # 玩不完就闭馆
            clock += n.visit_duration
            prev_id = n.id
        # preferred_order 硬顺序：违约对 × 双倍闭馆罚——任何交通收益都翻不了盘
        cost += self._order_violations(order) * 2 * CLOSED_VIOLATION_PENALTY_MIN
        return cost

    def _order_violations(self, order: list[RouteNode]) -> int:
        """pinned 相对顺序被破坏的对数（0 = 完全遵守）。"""
        pinned = [(n.preferred_order, n.id) for n in order
                  if n.preferred_order is not None]
        if len(pinned) < 2:
            return 0
        pinned.sort()
        pos = {nid: i for i, nid in enumerate(n.id for n in order)}
        seq = [pos[nid] for _, nid in pinned]
        return sum(1 for i in range(len(seq))
                   for j in range(i + 1, len(seq)) if seq[i] > seq[j])

    # ------------------------------------------------------------ 工具
    def _complete(self, order: list[RouteNode],
                  nodes: list[RouteNode]) -> list[RouteNode]:
        """把 order 规整为 nodes 的一个排列（补漏、去重）。"""
        by_id = {n.id: n for n in nodes}
        out: list[RouteNode] = []
        seen: set[str] = set()
        for n in order:
            if n.id in by_id and n.id not in seen:
                out.append(by_id[n.id])
                seen.add(n.id)
        out.extend(n for n in nodes if n.id not in seen)
        return out


def _dist_km(a: list[float], b: list[float]) -> float:
    from ..tools import haversine_km
    try:
        return haversine_km(a, b)
    except Exception:
        return 0.0
