"""Provider：外部 API 调用层。

原则（文档 §11 + 审查结论）：real_tools.get_weather / search_transport
**就是**现成的 Provider——Open-Meteo 请求解析、6h 文件缓存（v2| 版本键）、
预报窗口越界自愈都在那边实现并被测试固化。这里只做转发与协议翻译，
绝不重复实现第二套 HTTP/缓存（风险二：缓存逻辑重复）。

Provider 不负责意图识别、行程编排和 Session 修改。
"""

from __future__ import annotations

from ... import tools
from ..data_result import SOURCE_API, SOURCE_KB, DataResult


class WeatherProvider:
    """天气：转发 tools.get_weather（Open-Meteo，自带 6h 文件缓存）。

    source=external_api 是「本次数据的最终出处」——即便命中了 get_weather
    的文件缓存，数据的原始来源仍是 Open-Meteo；缓存口径由该工具自己的
    note 说明，这里不猜。
    """

    def __init__(self, fn=None):
        self._fn = fn or tools.get_weather     # 测试可注入桩

    def get(self, city: str, start_date: str, end_date: str) -> DataResult:
        try:
            res = self._fn(city, start_date, end_date)
        except Exception as e:                 # 网络异常不向上抛：契约内错误
            return DataResult.failure("PROVIDER_ERROR", f"{type(e).__name__}: {e}",
                                      source=SOURCE_API, origin_tool="get_weather")
        return DataResult.from_tool_result(res, source=SOURCE_API,
                                           origin_tool="get_weather")


class TransportProvider:
    """城际班次：转发 tools.search_transport（kb/静态种子口径）。"""

    def __init__(self, fn=None):
        self._fn = fn or tools.search_transport

    def search(self, from_city: str, to_city: str, date: str,
               transport_type: str = "any", party_size: int = 1) -> DataResult:
        try:
            res = self._fn(from_city, to_city, date, transport_type, party_size)
        except Exception as e:
            return DataResult.failure("PROVIDER_ERROR", f"{type(e).__name__}: {e}",
                                      source=SOURCE_KB, origin_tool="search_transport")
        return DataResult.from_tool_result(res, source=SOURCE_KB,
                                           origin_tool="search_transport")
