"""ScheduleBuilder 测试（文档 §7）：时间表、等待、闭馆冲突、汇总统计。"""

from _base import check, finish

from travel_agent.route_engine import (
    DistanceMatrix, RouteNode, ScheduleBuilder, TimeWindow,
)


def _n(id_: str, lat: float, lon: float, dur: int = 90,
       hours: str | None = None) -> RouteNode:
    return RouteNode(id=id_, name=id_, latitude=lat, longitude=lon,
                     visit_duration=dur, opening_time=hours)


HOME = [30.66, 104.06]


def test_basic_schedule() -> None:
    a = _n("A", 30.66, 104.088, dur=120)     # 东 3km
    b = _n("B", 30.66, 104.070, dur=90)      # 东 1.1km
    m = DistanceMatrix([a, b], start_geo=HOME)
    sb = ScheduleBuilder(TimeWindow())
    r = sb.build([a, b], m, 9 * 60, 20 * 60, HOME)
    check("时间表：成功且顺序正确", r.success and r.order == ["A", "B"])
    check("时间表：首站到达含起点交通",
          r.stops[0].arrival_time != "09:00"
          and r.stops[0].travel_duration > 0,
          r.stops[0].arrival_time)
    def _min(s: str) -> int:
        h, m = s.split(":")
        return int(h) * 60 + int(m)

    check("时间表：离开时刻 = 到达 + 游玩",
          _min(r.stops[0].departure_time) == _min(r.stops[0].arrival_time) + 120)
    check("时间表：游玩时长累计",
          r.total_visit_time == 210, str(r.total_visit_time))
    check("时间表：交通时长累计为正", r.total_travel_time > 0)
    check("时间表：估算段统计", r.estimated_legs == 2 and r.osrm_legs == 0)
    check("时间表：无警告", r.warnings == [], str(r.warnings))


def test_wait_and_closed() -> None:
    late_open = _n("LO", 30.66, 104.088, dur=60, hours="13:00-18:00")
    normal = _n("N", 30.66, 104.070, dur=60)
    m = DistanceMatrix([late_open, normal], start_geo=HOME)
    sb = ScheduleBuilder(TimeWindow())
    r = sb.build([late_open, normal], m, 9 * 60, 20 * 60, HOME)
    check("时间表：早到等待计入", r.total_wait_time > 0
          and r.stops[0].wait_minutes > 0
          and r.stops[0].arrival_time < "13:00",   # 到达时刻如实记录（早于开门）
          f"wait={r.stops[0].wait_minutes} arr={r.stops[0].arrival_time}")
    check("时间表：等待有警告", any("早到" in w for w in r.warnings))

    # 已闭馆：返回冲突说明，不伪造可行路线（文档 §13.2 场景八）
    r2 = sb.build([late_open], m, 19 * 60, 20 * 60, HOME)
    check("时间表：闭馆冲突如实输出", any("已闭馆" in w for w in r2.warnings),
          str(r2.warnings))


def test_end_time_warning() -> None:
    a = _n("A", 30.66, 104.088, dur=120)
    b = _n("B", 30.66, 104.020, dur=120)     # 西 3km：交通+游玩超窗口
    m = DistanceMatrix([a, b], start_geo=HOME)
    sb = ScheduleBuilder(TimeWindow())
    r = sb.build([a, b], m, 9 * 60, 11 * 60, HOME)   # 2 小时窗口装 4 小时
    check("时间表：超出窗口有收尾警告",
          any("晚于" in w for w in r.warnings), str(r.warnings))
    check("时间表：仍给出完整 stops（冲突说明而非空结果）",
          len(r.stops) == 2)


if __name__ == "__main__":
    test_basic_schedule()
    test_wait_and_closed()
    test_end_time_warning()
    finish("时间表")
