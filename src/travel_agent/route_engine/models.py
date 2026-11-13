"""路线数据模型（行野 2.0 第三阶段，文档 §5）。

坐标系约定（文档 §5.1 硬要求）：
- 本项目库内坐标统一 **WGS-84**（OSM/autofetch/OSRM 同源），
  coordinate_system 字段只作显式标注，入口处校验——绝不允许把
  高德 GCJ-02 坐标不经转换塞进来直接算距离。
- geo_uncertain 的节点（位置估算、无确切坐标）不能参与里程/耗时计算，
  距离矩阵与优化器对它一律跳过——沿用 composer 的既有契约。

模型尽量安全：缺字段给默认值，坐标异常（非数/越界）在构造时暴露，
不影响调用方崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass, field

COORD_WGS84 = "WGS-84"
COORD_GCJ02 = "GCJ-02"

# 合法纬度/经度范围（粗校验：挡住 None/字符串/数量级错误）
_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)


def valid_geo(latitude: float, longitude: float) -> bool:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:            # NaN
        return False
    return (_LAT_RANGE[0] <= lat <= _LAT_RANGE[1]
            and _LON_RANGE[0] <= lon <= _LON_RANGE[1])


@dataclass
class RouteNode:
    id: str
    name: str

    latitude: float | None = None
    longitude: float | None = None
    coordinate_system: str = COORD_WGS84

    visit_duration: int = 90                # 建议游玩时长（分钟）
    opening_time: str | None = None         # "08:30-17:00" 或 None=全天
    closing_time: str | None = None         # 预留：单独闭园时刻（本阶段未用）

    priority: int = 0                       # 分数/吸引力（优化时 tie-break）
    required: bool = False                  # 必去（硬约束）
    preferred_order: int | None = None      # 用户指定顺序（硬约束）
    geo_uncertain: bool = False             # 位置估算：不参与里程/耗时
    cost: float = 0.0                       # 门票（透传，优化不用）

    # ------------------------------------------------------------ 校验
    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("RouteNode.id 不能为空")
        if not self.name:
            raise ValueError("RouteNode.name 不能为空")
        if self.coordinate_system not in (COORD_WGS84, COORD_GCJ02):
            raise ValueError(f"未知坐标系：{self.coordinate_system}")
        if self.visit_duration < 0:
            raise ValueError("游玩时长不能为负")
        if self.geo_uncertain:
            # 估算点可能没有坐标：置空，防止下游误用
            self.latitude = None
            self.longitude = None
        elif (self.latitude is None or self.longitude is None
              or not valid_geo(self.latitude, self.longitude)):
            raise ValueError(
                f"{self.name}: 坐标缺失或非法"
                f"（lat={self.latitude}, lon={self.longitude}）")

    @property
    def geo(self) -> list[float] | None:
        """[lat, lon]（WGS-84）；估算点返回 None。"""
        if self.geo_uncertain or self.latitude is None:
            return None
        return [self.latitude, self.longitude]

    def has_geo(self) -> bool:
        return self.geo is not None


@dataclass
class RouteRequest:
    nodes: list[RouteNode] = field(default_factory=list)

    start_time: str = "09:00"               # 每日开始时刻
    end_time: str = "20:00"                 # 每日最晚收尾
    start_geo: list[float] | None = None    # 起点（酒店/城市中心），WGS-84

    travel_mode: str = "driving"

    max_visits: int | None = None
    must_visit: list[str] = field(default_factory=list)     # 节点 id
    excluded: list[str] = field(default_factory=list)       # 节点 id

    optimize: bool = True                   # False 只生成时间表不优化顺序

    def __post_init__(self) -> None:
        self.must_visit = [m for m in (self.must_visit or []) if m]
        self.excluded = [e for e in (self.excluded or []) if e]
        if self.max_visits is not None and self.max_visits < 0:
            raise ValueError("max_visits 不能为负")
        ids = {n.id for n in self.nodes}
        for m in self.must_visit:
            if m not in ids:
                raise ValueError(f"必去节点 {m!r} 不在 nodes 里")
        for e in self.excluded:
            if e not in ids:
                raise ValueError(f"排除节点 {e!r} 不在 nodes 里")


@dataclass
class RouteLeg:
    from_id: str
    to_id: str

    distance_meters: float                  # 道路距离（估算时=道路系数折算）
    duration_seconds: float
    source: str = "estimate"                # "osrm" / "estimate"
    estimated: bool = True                  # 直线折算必须显式标记（文档 §6.1.4）


@dataclass
class RouteStop:
    node: RouteNode

    arrival_time: str                       # "HH:MM"
    departure_time: str
    travel_duration: int = 0                # 到达该点前的交通分钟
    visit_duration: int = 90
    wait_minutes: int = 0                   # 早到等待（营业时间未开）
    transit_text: str = ""                  # 如「打车 12分钟（3.4km）」


@dataclass
class RouteResult:
    success: bool = True

    stops: list[RouteStop] = field(default_factory=list)
    order: list[str] = field(default_factory=list)          # 节点 id 顺序

    total_distance_km: float = 0.0
    total_travel_time: int = 0              # 分钟
    total_visit_time: int = 0               # 分钟
    total_wait_time: int = 0                # 分钟

    algorithm: str = "none"                 # "nearest_neighbor" / "2-opt" / "input"
    estimated_legs: int = 0                 # 估算段数（数据可信度）
    osrm_legs: int = 0

    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.order and self.stops:
            self.order = [s.node.id for s in self.stops]
