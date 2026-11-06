"""工具层门面：真实数据 / Mock 数据一键切换。

默认走真实数据（real_tools：Open-Meteo 真实天气 + OSRM 真实路网 + 联网核实的
真实票价/班次/酒店）。设环境变量 USE_REAL_DATA=0 切回 mock_tools——
离线演示、CI 确定性回归或 API 不可用时用。

两个实现对外完全同构（同名导出、同一 error_contract），下游代码零感知。
"""

import os

if os.environ.get("USE_REAL_DATA", "1") != "0":
    from .real_tools import (  # noqa: F401
        CITY_DB, CITY_GEO, HOTELS_DB, POIS_DB, ROUTES_DB,
        TOOL_REGISTRY, calc_budget, dispatch, get_weather, haversine_km,
        save_preference, search_hotel, search_poi, search_transport,
        transit_estimate, DATA_RETRIEVED,
    )
else:
    from .mock_tools import (  # noqa: F401
        CITY_DB, HOTELS_DB, POIS_DB, ROUTES_DB,
        TOOL_REGISTRY, calc_budget, dispatch, get_weather, haversine_km,
        save_preference, search_hotel, search_poi, search_transport,
        transit_estimate,
    )
