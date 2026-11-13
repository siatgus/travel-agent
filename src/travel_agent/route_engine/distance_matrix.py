"""距离矩阵（行野 2.0 第三阶段，文档 §6.1）。

职责：一次批量构建节点两两之间的（道路距离, 交通分钟, 来源）矩阵。

复用契约（审查结论，不重复实现）：
- 交通耗时统一走 tools.transit_estimate——它内部在给了坐标时调
  real_tools._osrm_drive（ROUTE_MEM 内存缓存 + ROUTE_CACHE 磁盘缓存），
  失败自动回落「道路系数 1.3 + 分段车速」估算。矩阵层因此只需：
  ①控制 OSRM 实调对数上限（config.OSRM_MAX_PAIRS）；
  ②区分来源（osrm vs estimate）并保留在矩阵里。
- 直线距离用 tools.haversine_km（WGS-84，与库内坐标同系，不混 GCJ-02）。

估算段绝不冒充道路距离：RouteLeg.estimated=True + source="estimate"，
上层（约束检查/结果元数据）据此输出数据可信度提示。
"""

from __future__ import annotations

from .config import OSRM_MAX_PAIRS, osrm_enabled
from .models import RouteNode


class DistanceMatrix:
    """对称矩阵：get(km|duration, a, b)。节点 ≤2 个时矩阵为空但接口安全。"""

    def __init__(self, nodes: list[RouteNode] | None = None,
                 start_geo: list[float] | None = None):
        self.nodes = list(nodes or [])
        self.start_geo = start_geo
        self._pair: dict[tuple[str, str], dict] = {}
        self._start_legs: dict[str, dict] = {}      # 起点 → 首节点
        self.osrm_calls = 0
        self.estimated_calls = 0
        if self.nodes:
            self.build()

    # ------------------------------------------------------------ 构建
    def build(self, nodes: list[RouteNode] | None = None,
              travel_mode: str = "driving") -> "DistanceMatrix":
        if nodes is not None:
            self.nodes = list(nodes)
        reliable = [n for n in self.nodes if n.has_geo()]
        pairs = [(a, b) for i, a in enumerate(reliable)
                 for b in reliable[i + 1:]]
        # OSRM 配额：按节点对序分配给前 OSRM_MAX_PAIRS 对，其余估算。
        # 同一批尽量批量（一次 build 内集中决策），避免散调。
        osrm_budget = OSRM_MAX_PAIRS if osrm_enabled() and travel_mode == "driving" else 0
        # 起点腿优先占配额（它决定贪心起手），其余分给节点对
        if self.start_geo:
            for n in reliable:
                use = osrm_budget > 0
                if use:
                    osrm_budget -= 1
                self._start_legs[n.id] = self._measure_geo(self.start_geo, n, use)
        for a, b in pairs:
            use_osrm = osrm_budget > 0
            if use_osrm:
                osrm_budget -= 1
            self._pair[(a.id, b.id)] = self._measure(a, b, use_osrm)
        return self

    def _measure(self, a: RouteNode, b: RouteNode,
                 allow_osrm: bool) -> dict:
        ga, gb = a.geo, b.geo
        km = _haversine(ga, gb)
        if allow_osrm:
            road_km, drive_min = _osrm_drive(ga, gb) or (None, None)
            if road_km is not None:
                self.osrm_calls += 1
                return {"km": road_km, "min": int(drive_min),
                        "source": "osrm", "estimated": False}
        self.estimated_calls += 1
        mode, tmin = _transit_estimate(km, ga, gb)
        return {"km": round(km * 1.3, 2), "min": int(tmin),
                "source": "estimate", "estimated": True}

    def _measure_geo(self, geo: list[float], n: RouteNode,
                     allow_osrm: bool) -> dict:
        fake = RouteNode(id="__start__", name="__start__",
                         latitude=geo[0], longitude=geo[1])
        return self._measure(fake, n, allow_osrm)

    # ------------------------------------------------------------ 查询
    def _key(self, a: str, b: str) -> tuple[str, str] | None:
        if (a, b) in self._pair:
            return (a, b)
        if (b, a) in self._pair:
            return (b, a)
        return None

    def leg(self, from_id: str, to_id: str) -> dict | None:
        k = self._key(from_id, to_id)
        return dict(self._pair[k]) if k else None

    def get_distance_km(self, from_id: str, to_id: str) -> float:
        leg = self.leg(from_id, to_id)
        return leg["km"] if leg else 0.0

    def get_duration(self, from_id: str, to_id: str) -> int:
        leg = self.leg(from_id, to_id)
        return leg["min"] if leg else 0

    def from_start(self, node_id: str) -> dict | None:
        """起点（酒店/城心）到某节点的腿。无起点配置返回 None。"""
        leg = self._start_legs.get(node_id)
        return dict(leg) if leg else None

    def estimated_ratio(self) -> float:
        total = self.osrm_calls + self.estimated_calls
        return self.estimated_calls / total if total else 1.0

    def summary(self) -> dict:
        return {"nodes": len(self.nodes),
                "pairs": len(self._pair),
                "osrm": self.osrm_calls,
                "estimated": self.estimated_calls}


# ---------------------------------------------------------------- 工具转发
def _haversine(a: list[float], b: list[float]) -> float:
    from ..tools import haversine_km
    return haversine_km(a, b)


def _osrm_drive(a: list[float], b: list[float]) -> tuple[float, float] | None:
    try:
        from ..real_tools import _osrm_drive as drive
        return drive(a, b)               # USE_REAL_DATA=0 时无此函数 → ImportError
    except Exception:
        return None


def _transit_estimate(km: float, a: list[float], b: list[float]) -> tuple[str, int]:
    from ..tools import transit_estimate
    try:
        return transit_estimate(km, a, b)
    except Exception:
        road = km * 1.3
        return ("打车", int(road / 30 * 60) + 10)
