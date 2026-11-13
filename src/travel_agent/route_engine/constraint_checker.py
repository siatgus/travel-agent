"""约束检查（行野 2.0 第三阶段，文档 §8）。

硬约束（violations）：违反则 success=False——
- 必去节点全部在路线里；
- 排除节点一个都不在；
- preferred_order 相对顺序成立；
- 必去节点不允许是「已闭馆进不去」的状态（伪造可行 = 最严重的欺骗）。

软约束（warnings）：尽量满足，不满足输出警告——
- 营业时间（早到等待/玩不完）、每日总时长、交通耗时合理性、
  估算数据占比。
"""

from __future__ import annotations

from .config import DEFAULT_END_MIN, DEFAULT_START_MIN
from .models import RouteNode, RouteRequest, RouteResult
from .time_window import TimeWindow


class ConstraintChecker:

    def __init__(self, window: TimeWindow | None = None,
                 start_min: int = DEFAULT_START_MIN,
                 end_min: int = DEFAULT_END_MIN):
        self.window = window or TimeWindow()
        self.start_min = start_min
        self.end_min = end_min

    # ------------------------------------------------------------ 路线级
    def check_route(self, order: list[RouteNode],
                    request: RouteRequest) -> tuple[list[str], list[str]]:
        """检查纯顺序（不含时刻）。返回（violations, warnings）。"""
        violations: list[str] = []
        warnings: list[str] = []
        ids = [n.id for n in order]
        # 必去
        must_miss = [m for m in request.must_visit if m not in ids]
        if must_miss:
            violations.append("必去节点未包含：" + "、".join(must_miss))
        # 排除
        leaked = [e for e in request.excluded if e in ids]
        if leaked:
            violations.append("排除节点出现在路线里：" + "、".join(leaked))
        # 用户指定顺序（相对顺序硬约束）
        pinned = [(n.preferred_order, n.id) for n in order
                  if n.preferred_order is not None]
        pinned.sort()
        if len(pinned) >= 2:
            pos = {nid: i for i, nid in enumerate(ids)}
            seq = [pos[nid] for _, nid in pinned]
            if seq != sorted(seq):
                violations.append(
                    "用户指定顺序被破坏：" +
                    " → ".join(nid for _, nid in pinned))
        # 每日净游玩时长
        visit_total = sum(n.visit_duration for n in order)
        if (request.end_time and self.end_min
                and visit_total > (self.end_min - self.start_min)):
            warnings.append(
                f"净游玩 {visit_total} 分钟超过当日窗口"
                f" {self.end_min - self.start_min} 分钟")
        if request.max_visits and len(order) > request.max_visits:
            violations.append(
                f"访问点数 {len(order)} 超过 max_visits={request.max_visits}")
        return violations, warnings

    # ------------------------------------------------------------ 时间表级
    def check_schedule(self, result: RouteResult,
                       request: RouteRequest,
                       nodes_by_id: dict[str, RouteNode] | None = None,
                       ) -> RouteResult:
        """把 check_route 的结论与时间表 warnings 合并进 RouteResult。"""
        order = [nodes_by_id[i] for i in result.order
                 if nodes_by_id and i in nodes_by_id]
        if order:
            violations, warnings = self.check_route(order, request)
        else:
            violations, warnings = [], []
        # 必去节点闭馆不可入：硬约束（schedule_builder 把它写进了 warnings）
        for nid in request.must_visit:
            for w in result.warnings:
                name = (nodes_by_id or {}).get(nid)
                if name and w.startswith(name.name) and "已闭馆" in w:
                    violations.append(f"必去节点 {nid} 按当前时间无法入场")
                    break
        result.errors = violations
        result.warnings = warnings + [w for w in result.warnings
                                      if w not in warnings]
        result.success = not violations
        return result

    # ------------------------------------------------------------ 快捷项
    def check_required_nodes(self, order: list[RouteNode],
                             request: RouteRequest) -> list[str]:
        """只查必去覆盖（文档 §8 建议接口）。"""
        ids = {n.id for n in order}
        return [m for m in request.must_visit if m not in ids]
