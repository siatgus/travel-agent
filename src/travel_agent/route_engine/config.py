"""Route Engine 配置（行野 2.0 第三阶段，文档 §11.4/§14）。

所有开关都可以用环境变量覆盖，保证「新旧路线模式可随时切换回滚」：
    TRAVEL_ROUTE_ENGINE=0   关闭 Route Engine，composer 走原有启发式排序
    TRAVEL_ROUTE_OSRM=0     距离矩阵禁用 OSRM（只按道路系数估算）
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def route_engine_enabled() -> bool:
    """总开关：composer 是否用 RouteEngine 优化当日访问顺序。

    默认开启。关闭时 compose 行为与第三阶段之前逐字一致（回滚通道）。
    """
    return os.environ.get("TRAVEL_ROUTE_ENGINE", "1") != "0"


def osrm_enabled() -> bool:
    """距离矩阵是否允许调 OSRM（real_tools._osrm_drive 自带缓存）。"""
    return os.environ.get("TRAVEL_ROUTE_OSRM", "1") != "0"


# 距离矩阵一次构建允许的 OSRM 实调对数上限。OSRM 每对一次 HTTP（有磁盘
# 缓存，重复规划幂等），当日成团通常 ≤5 个点（10 对）；超出阈值的部分
# 直接走道路系数估算并标 estimated——路线优化的收益不值得一轮几十秒的
# 外部请求。
OSRM_MAX_PAIRS = _env_int("TRAVEL_ROUTE_OSRM_MAX_PAIRS", 16)

# 2-opt 最大改进迭代次数（每轮扫描全部邻域）。当日点数 ≤PACE_CAP 上限 4，
# 邻域极小，200 次是给「自由组合调用」（用户直接传十几个点）兜底的上限。
TWO_OPT_MAX_ITERS = _env_int("TRAVEL_ROUTE_2OPT_MAX_ITERS", 200)

# 最近邻贪心并列时的决胜半径（公里）：直线距离差小于它视为同距，保持输入
# 稳定顺序——保证相同输入产生确定性结果（文档 §14.9）。
TIE_EPS_KM = _env_float("TRAVEL_ROUTE_TIE_EPS_KM", 0.05)

# 时间窗口成本惩罚（分钟）：到达后玩不完就闭馆的顺序，罚这么多——
# 比任何合理节省的交通时间都大，保证 2-opt 不会选「顺路但闭馆」的顺序。
CLOSED_VIOLATION_PENALTY_MIN = _env_int(
    "TRAVEL_ROUTE_CLOSED_PENALTY", 240)

# 早到等待的软惩罚系数：等待 1 分钟计 0.5 分钟成本（宁可少等，不硬排）。
WAIT_COST_FACTOR = _env_float("TRAVEL_ROUTE_WAIT_COST", 0.5)

# 单日装配默认口径（与 composer 常量同源，composer 导入本模块避免循环依赖）
DEFAULT_START_MIN = 9 * 60      # 09:00 出发
DEFAULT_END_MIN = 20 * 60       # 20:00 最晚收尾（composer.DAY_END_LATEST）
