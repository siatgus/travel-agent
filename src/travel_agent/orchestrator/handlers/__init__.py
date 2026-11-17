"""QA Handler 层（晴航 2.0 设计文档 §4.1/§8 第二阶段落地）。

Handler 是职责清晰的业务模块，不是互相交接的 LLM Agent（文档 §3）；
本包内的 Handler 全部复用 Session 既有业务方法（_weather_answer 等），
不重写业务逻辑——拆的是「分发结构」，不是「业务实现」。

拆分约定（文档 §11 第二阶段「每拆一个 Handler，保留原有行为」）：
- 顶层 Handler（Weather/Transport/Smalltalk/PlanQA/General/POI）
  与 IntentRouter 的 QA_KINDS 一一对应，Orchestrator 按意图分发；
- poi 细分（Attraction/Food/Restaurant/Hotel/Route/Comparison）由
  POIHandler 按 refine_intent 顺序组成兜底链——某一路没接住继续往后，
  与拆分前 Session._poi_answer 逐字一致；
- Session 上对应业务方法保留原样（对话层与测试仍可直接调用）。
"""

from __future__ import annotations

from ..intent_router import QA_KINDS  # noqa: F401（re-export 供外部核对）
from .weather_handler import WeatherHandler
from .transport_handler import TransportHandler
from .smalltalk_handler import SmalltalkHandler
from .plan_qa_handler import PlanQAHandler
from .general_handler import GeneralHandler
from .poi_handler import POIHandler
from .plan_handler import PlanHandler
from ._base import resolve_city

__all__ = [
    "QA_KINDS", "QA_HANDLERS", "resolve_city",
    "WeatherHandler", "TransportHandler", "SmalltalkHandler",
    "PlanQAHandler", "GeneralHandler", "POIHandler", "PlanHandler",
]


QA_HANDLERS: dict = {
    "weather": WeatherHandler(),
    "poi": POIHandler(),
    "transport": TransportHandler(),
    "smalltalk": SmalltalkHandler(),
    "plan_qa": PlanQAHandler(),
    "general": GeneralHandler(),
}
