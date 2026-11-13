"""services：统一数据服务层（行野 2.0 第二阶段）。

对外入口：get_data_service() 单例。业务 Handler（dialogue / orchestrator）
通过它取数据，不再直接理解各工具返回协议；real_tools 六工具保留为兼容
入口（ADR-003），规划链路（composer/plan_trip）暂不迁移。
"""

from __future__ import annotations

import threading

from .data_policy import POLICIES, DataPolicy, is_stale, policy_of
from .data_result import (
    EMPTY_ERROR_CODES, SOURCE_API, SOURCE_CACHE, SOURCE_HISTORY, SOURCE_KB,
    SOURCE_STATIC, SOURCE_UNKNOWN, DataResult,
)
from .data_service import DataService

__all__ = [
    "DataService", "DataResult", "DataPolicy",
    "get_data_service", "reset_data_service",
    "POLICIES", "policy_of", "is_stale",
    "EMPTY_ERROR_CODES",
    "SOURCE_UNKNOWN", "SOURCE_KB", "SOURCE_STATIC", "SOURCE_API",
    "SOURCE_CACHE", "SOURCE_HISTORY",
]

_SVC: DataService | None = None
_SVC_LOCK = threading.Lock()


def get_data_service() -> DataService:
    """进程级单例；DataService 本身无状态（缓存/熔断都在 autofetch 模块内）。"""
    global _SVC
    if _SVC is None:
        with _SVC_LOCK:
            if _SVC is None:
                _SVC = DataService()
    return _SVC


def reset_data_service() -> None:
    """测试/重载用：丢弃单例。"""
    global _SVC
    with _SVC_LOCK:
        _SVC = None
