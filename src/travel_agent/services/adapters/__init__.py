"""数据服务子层适配器：Repository / Provider / AutofetchAdapter。

- Repository：本地知识库访问与来源元数据（不调外部 API，不答业务问题）
- Provider：外部 API 调用（转发 real_tools 既有实现，不重复造 HTTP 层）
- AutofetchAdapter：适配 autofetch.py 的补全/缓存/熔断能力
"""

from .autofetch_adapter import AutofetchAdapter
from .providers import WeatherProvider, TransportProvider
from .repositories import (
    POIRepository, HotelRepository, TransportRepository, FoodRepository,
)

__all__ = [
    "AutofetchAdapter",
    "WeatherProvider", "TransportProvider",
    "POIRepository", "HotelRepository", "TransportRepository", "FoodRepository",
]
