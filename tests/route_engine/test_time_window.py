"""TimeWindow 测试（文档 §6.4）：营业判定、等待、入场可行性。"""

from _base import check, finish

from travel_agent.route_engine import RouteNode, TimeWindow


def _n(open_hours: str | None) -> RouteNode:
    return RouteNode(id="X", name="X", latitude=30.66, longitude=104.05,
                     opening_time=open_hours)


def test_parse() -> None:
    w = TimeWindow()
    check("时间窗：标准区间解析", w.window(_n("08:30-17:00")) == (510, 1020))
    check("时间窗：全天/空视为无约束", w.window(_n("全天")) is None
          and w.window(_n(None)) is None)
    check("时间窗：非法串安全为 None", w.window(_n("随时")) is None)


def test_is_open_and_wait() -> None:
    w = TimeWindow()
    n = _n("09:00-17:00")
    check("营业判定：窗口内", w.is_open(n, 9 * 60))
    check("营业判定：闭馆后不算营业", not w.is_open(n, 17 * 60 + 1))
    check("等待：早到 30 分钟", w.calculate_wait(n, 8 * 60 + 30) == 30)
    check("等待：已开门无等待", w.calculate_wait(n, 10 * 60) == 0)
    check("等待：全天开放无等待", w.calculate_wait(_n(None), 7 * 60) == 0)


def test_next_available() -> None:
    w = TimeWindow()
    n = _n("09:00-17:00")
    check("入场：早到等到开门", w.find_next_available(n, 8 * 60) == (9 * 60, 60))
    check("入场：营业中直接进", w.find_next_available(n, 10 * 60) == (10 * 60, 0))
    check("入场：已闭馆返回 None", w.find_next_available(n, 18 * 60) is None)
    check("入场：全天开放即到即进",
          w.find_next_available(_n(None), 7 * 60) == (7 * 60, 0))


def test_can_finish() -> None:
    w = TimeWindow()
    n = _n("09:00-17:00")
    check("可行性：16:00 进、60 分钟玩得完",
          w.can_finish(n, 16 * 60, 60))
    check("可行性：16:30 进、60 分钟玩不完",
          not w.can_finish(n, 16 * 60 + 30, 60))
    check("可行性：已闭馆不可入", not w.can_finish(n, 18 * 60, 60))
    check("可行性：全天开放总能排", w.can_finish(_n(None), 19 * 60, 90))


if __name__ == "__main__":
    test_parse()
    test_is_open_and_wait()
    test_next_available()
    test_can_finish()
    finish("时间窗口")
