"""时间窗口（行野 2.0 第三阶段，文档 §6.4）。

第一版只支持每日单个连续营业窗口（"08:30-17:00"），
"全天"/空值视为无约束。分时段/午休闭馆/节假日留给后续扩展。

时刻统一用分钟制（0=00:00），与 composer 的 parse_hours/fmt 同口径；
parse_hours 直接复用 composer 的实现，保证两处语义永不漂移。
"""

from __future__ import annotations

from .models import RouteNode


def parse_open_hours(spec: str | None) -> tuple[int, int] | None:
    """解析营业时间字符串 → (开, 闭) 分钟；全天/空/非法返回 None（无约束）。"""
    from ..composer import parse_hours
    return parse_hours(spec or "")


class TimeWindow:

    def window(self, node: RouteNode) -> tuple[int, int] | None:
        """该节点的 (开, 闭) 分钟窗口；无约束返回 None。"""
        return parse_open_hours(node.opening_time)

    def is_open(self, node: RouteNode, arrival_min: int) -> bool:
        """到达时刻是否在营业窗口内（恰好等于闭馆时刻算营业）。"""
        w = self.window(node)
        if w is None:
            return True
        return w[0] <= arrival_min <= w[1]

    def calculate_wait(self, node: RouteNode, arrival_min: int) -> int:
        """早到需要等待的分钟数（营业后才放行）；已营业返回 0。"""
        w = self.window(node)
        if w is None or arrival_min >= w[0]:
            return 0
        return w[0] - arrival_min

    def find_next_available(self, node: RouteNode,
                            arrival_min: int) -> tuple[int, int] | None:
        """返回 (可入场时刻, 等待分钟)；全天开放返回 (arrival, 0)。

        到达时刻已闭馆返回 None——这一天进不去了，上层要么换顺序要么换天。
        """
        w = self.window(node)
        if w is None:
            return arrival_min, 0
        if arrival_min > w[1]:
            return None
        if arrival_min < w[0]:
            return w[0], w[0] - arrival_min
        return arrival_min, 0

    def can_finish(self, node: RouteNode, arrival_min: int,
                   visit_duration: int) -> bool:
        """到达后能否在闭馆前完成建议游玩时长（无窗口视为可以）。"""
        w = self.window(node)
        if w is None:
            return True
        start, _ = self.find_next_available(node, arrival_min) or (None, 0)
        if start is None:
            return False
        return start + visit_duration <= w[1]
