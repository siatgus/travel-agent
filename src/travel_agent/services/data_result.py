"""统一数据返回协议 DataResult（行野 2.0 第二阶段，文档 §8）。

目标：所有数据查询（知识库 / 静态种子 / 外部 API / 自动补全）返回同一种
结构，业务 Handler 不再理解每个工具各自的返回协议。

四种状态必须严格区分（文档 §3.6）——绝不能把「API 超时」解释成
「目的地没有相关数据」：

    ok=True  data=[...]        查询成功且有数据
    ok=True  data=[]           查询成功但没有数据
    ok=False error_code=...    查询失败（上游异常 / 参数错误 / 熔断中）
    ok=True  data=[...] stale  查询成功但只返回过期数据（metadata 说明）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 数据来源常量（文档 §8.1）------------------------------------------

SOURCE_UNKNOWN = "unknown"            # 未标注（不应出现在新代码里）
SOURCE_KB = "knowledge_base"          # 本地 SQLite 知识库
SOURCE_STATIC = "static_seed"         # 静态种子数据（real_tools 内置核实数据）
SOURCE_API = "external_api"           # 外部 API（Open-Meteo / OSRM / 地图服务）
SOURCE_CACHE = "cache"                # 缓存数据（metadata.origin_source 记原始来源）
SOURCE_HISTORY = "historical"         # 历史参考数据（历史同期天气等）

# kb 行级 source → 原始来源归类
ORIGIN_BY_ROW_SOURCE = {
    "seed": "static_seed",
    "manual": "manual",
    "manual-import": "manual",
    "auto-fetch": "external_api",
    "auto-fetch-llm": "external_api",
    "overpass": "external_api",
}

# 旧工具「城市确实无数据」的错误码（retryable=True 那组）。
# 只有这些才允许触发自动补全；BAD_DATE / CITY_NOT_FOUND 等参数类错误绝不触发。
EMPTY_ERROR_CODES = ("NO_POI", "NO_HOTEL", "NO_ROUTE")


@dataclass
class DataResult:
    ok: bool = True
    data: Any = None

    source: str = SOURCE_UNKNOWN
    fetched_at: str | None = None        # 数据入库/获取时间（ISO 8601）

    stale: bool = False                  # 是否超过 DataPolicy 的 TTL
    cached: bool = False                 # 本次是否命中缓存/失败缓存短路

    error_code: str | None = None
    error_message: str | None = None

    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------ 视图
    @property
    def items(self) -> list:
        """业务条目列表；data 不是列表时返回空表。"""
        return self.data if isinstance(self.data, list) else []

    @property
    def has_data(self) -> bool:
        return bool(self.items)

    # ------------------------------------------------------------ 构造器
    @classmethod
    def success(cls, data: Any, source: str = SOURCE_UNKNOWN,
                fetched_at: str | None = None, **meta: Any) -> "DataResult":
        return cls(ok=True, data=data, source=source, fetched_at=fetched_at,
                   metadata=dict(meta))

    @classmethod
    def empty(cls, source: str = SOURCE_UNKNOWN,
              message: str = "") -> "DataResult":
        """查询成功但没有数据——与失败严格分开。"""
        return cls(ok=True, data=[], source=source,
                   error_message=message or None,
                   metadata={"empty": True})

    @classmethod
    def failure(cls, code: str, message: str,
                source: str = SOURCE_UNKNOWN, **meta: Any) -> "DataResult":
        return cls(ok=False, error_code=code, error_message=message,
                   source=source, metadata=dict(meta))

    # ------------------------------------------------------------ 旧协议适配
    @classmethod
    def from_tool_result(cls, res: dict, source: str = SOURCE_UNKNOWN,
                         fetched_at: str | None = None, **meta: Any) -> "DataResult":
        """把旧工具协议 {ok, ...业务字段, note} / {ok:False, error:{...}}
        包成 DataResult。原始返回完整保留在 metadata.raw，note 提到 metadata.note，
        业务字段（items/days 等）进 data——上层拿 items 就够了。
        """
        meta = dict(meta)
        meta.setdefault("raw", res)
        if res.get("ok"):
            if "items" in res:
                data = res["items"]
            elif "days" in res:
                data = res["days"]
            else:
                data = res
            note = res.get("note")
            if note:
                meta.setdefault("note", note)
            return cls(ok=True, data=data, source=source,
                       fetched_at=fetched_at, metadata=meta)
        err = res.get("error") or {}
        return cls(ok=False, data=None, source=source,
                   error_code=err.get("code") or "UNKNOWN",
                   error_message=err.get("message") or "",
                   metadata=meta)

    def retryable(self) -> bool:
        """失败是否值得重试（沿用旧工具 error.retryable 语义）。"""
        if self.ok:
            return False
        raw = (self.metadata.get("raw") or {}).get("error") or {}
        return bool(raw.get("retryable"))

    def is_empty_no_data(self) -> bool:
        """「查询成功但该城市确实无数据」——自动补全的触发条件。"""
        return (not self.ok and self.error_code in EMPTY_ERROR_CODES)
