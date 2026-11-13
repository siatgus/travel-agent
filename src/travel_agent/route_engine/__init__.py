"""Route Engine（行野 2.0 第三阶段）：独立、可测试的路线优化引擎。

职责边界（文档 §11.2）：
- RouteEngine 只负责「一天内景点的访问顺序优化 + 时间窗口约束」；
- 按天分配景点、餐饮住宿插入、完整行程生成仍归 Composer；
- 距离/耗时复用 real_tools 的 OSRM 与估算链路（含缓存），不建第二套；
- 估算坐标（geo_uncertain）绝不参与里程/耗时——沿用既有契约。

集成开关：TRAVEL_ROUTE_ENGINE=0 时 composer 完全走旧启发式排序，
新旧行为可随时切换回滚。
"""

from .config import route_engine_enabled
from .constraint_checker import ConstraintChecker
from .distance_matrix import DistanceMatrix
from .models import (
    COORD_GCJ02, COORD_WGS84, RouteLeg, RouteNode, RouteRequest,
    RouteResult, RouteStop,
)
from .route_adjuster import RouteAdjuster
from .route_engine import RouteEngine, optimize_order
from .route_optimizer import RouteOptimizer
from .schedule_builder import ScheduleBuilder
from .time_window import TimeWindow

__all__ = [
    "RouteEngine", "optimize_order", "route_engine_enabled",
    "DistanceMatrix", "RouteOptimizer", "TimeWindow", "ScheduleBuilder",
    "ConstraintChecker", "RouteAdjuster",
    "RouteNode", "RouteRequest", "RouteLeg", "RouteStop", "RouteResult",
    "COORD_WGS84", "COORD_GCJ02",
]
