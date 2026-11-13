"""每日时间表生成（行野 2.0 第三阶段，文档 §7）。

给定优化后的访问顺序 + 距离矩阵，生成带到达/离开时刻的访问时间表。
午餐、午休、晚餐、酒店节点由 Composer 继续管理——本模块只处理
「景点访问」这一层，绝不擅自把餐饮当景点排（文档 §7 注意事项）。

时间口径与 composer 一致：分钟制，"HH:MM" 字符串出口用 fmt。
"""

from __future__ import annotations

from .config import DEFAULT_END_MIN, DEFAULT_START_MIN
from .distance_matrix import DistanceMatrix
from .models import RouteNode, RouteResult, RouteStop
from .time_window import TimeWindow


def fmt(minutes: int) -> str:
    from ..composer import fmt as _fmt
    return _fmt(minutes)


class ScheduleBuilder:

    def __init__(self, window: TimeWindow | None = None):
        self.window = window or TimeWindow()

    def build(self, route: list[RouteNode], matrix: DistanceMatrix,
              start_time: int = DEFAULT_START_MIN,
              end_time: int = DEFAULT_END_MIN,
              start_geo: list[float] | None = None,
              algorithm: str = "schedule") -> RouteResult:
        """顺序 → RouteResult（含 stops 时间表与汇总统计）。

        闭馆进不去/玩不完的节点不伪造时间：进 stops 但 arrival/departure
        仍按可行最早时刻写，同时在 warnings 里说明冲突（文档 §13.2 场景八：
        返回冲突说明而不是伪造可行路线）。
        """
        result = RouteResult(success=True, algorithm=algorithm)
        clock = start_time
        prev_id: str | None = None
        for n in route:
            if prev_id is None:
                leg = matrix.from_start(n.id) or {}
            else:
                leg = matrix.leg(prev_id, n.id) or {}
            tmin = int(leg.get("min") or 0)
            km = leg.get("km") or 0.0
            source = leg.get("source") or "estimate"
            estimated = bool(leg.get("estimated", True))
            if source == "osrm":
                result.osrm_legs += 1
            else:
                result.estimated_legs += 1
            clock += tmin
            result.total_travel_time += tmin
            result.total_distance_km += km

            mode = "打车" if tmin > 8 else ("步行" if tmin else "—")
            transit_text = (f"{mode} {tmin}分钟（{km}km）"
                            + ("" if not estimated else "·估算") ) if tmin else "—"

            slot = self.window.find_next_available(n, clock)
            wait = 0
            if slot is None:
                # 已闭馆：今天进不去了——如实写冲突，时间仍按到达时刻
                result.warnings.append(
                    f"{n.name}：{fmt(clock)} 到达已闭馆"
                    f"（{n.opening_time or '全天'}），需换顺序或换天")
                result.stops.append(RouteStop(
                    node=n, arrival_time=fmt(clock),
                    departure_time=fmt(clock + n.visit_duration),
                    travel_duration=tmin, visit_duration=n.visit_duration,
                    wait_minutes=0, transit_text=transit_text))
                result.total_visit_time += n.visit_duration
                prev_id = n.id
                clock += n.visit_duration
                continue
            arrive_min = clock            # 实际到达时刻（早到也如实记录）
            enter, wait = slot
            if wait:
                result.warnings.append(
                    f"{n.name}：{fmt(clock)} 早到，开门前需等待 {wait} 分钟")
            if enter + n.visit_duration > (self.window.window(n) or (0, end_time))[1]:
                result.warnings.append(
                    f"{n.name}：{fmt(enter)} 进入后 {n.visit_duration} 分钟"
                    f"超出闭馆时刻，游玩时长需压缩或改期")
            clock = enter
            result.total_wait_time += wait
            depart = clock + n.visit_duration
            result.stops.append(RouteStop(
                node=n, arrival_time=fmt(arrive_min),
                departure_time=fmt(depart),
                travel_duration=tmin, visit_duration=n.visit_duration,
                wait_minutes=wait, transit_text=transit_text))
            result.total_visit_time += n.visit_duration
            clock = depart
            prev_id = n.id
        if clock > end_time:
            result.warnings.append(
                f"收尾 {fmt(clock)} 晚于 {fmt(end_time)}：当天偏赶，"
                "建议减少一个点或压缩游玩时长")
        result.order = [s.node.id for s in result.stops]
        return result
