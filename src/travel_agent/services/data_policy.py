"""数据获取策略 DataPolicy（行野 2.0 第二阶段，文档 §9）。

不同数据类型的时效要求不同，不能用统一 TTL，也不能把过期数据当实时数据：

    景点基础信息   7 天    允许过期     空结果补全
    餐厅基础信息   1 天    允许过期     空结果补全
    酒店基础信息   1 天    允许过期     空结果补全（起价口径，绝不冒充实时房态）
    天气          30 分钟  谨慎允许     空结果补全（real_tools 已有 6h 文件缓存）
    交通班次       5 分钟  不允许长期   不补全（班次是 12306/航司静态口径，工具 note 已说明）
    美食知识       不设 TTL（菜品文化知识不会过期）
    历史同期天气   30 天    允许        不补全

stale 只标记不拦截：allow_stale=True 的类型过期照常返回但 stale=True，
由说话层决定要不要向用户说明口径；allow_stale=False 的类型（班次）
过期结果一律不作为「实时」依据返回——现在班次本来就是静态口径，
真到接实时班次 API 时这条策略就是拦截点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class DataPolicy:
    data_type: str
    ttl_seconds: int                      # 0 = 不设时效（知识类）
    allow_stale: bool = True
    autofetch_on_empty: bool = True
    autofetch_on_error: bool = False      # 失败一律不自动重试（熔断器负责节流）


POLICIES: dict[str, DataPolicy] = {
    "attraction": DataPolicy("attraction", 7 * 86400, True, True, False),
    "restaurant": DataPolicy("restaurant", 86400, True, True, False),
    "hotel": DataPolicy("hotel", 86400, True, True, False),
    "weather": DataPolicy("weather", 1800, True, True, False),
    "transport": DataPolicy("transport", 300, False, False, False),
    "food_knowledge": DataPolicy("food_knowledge", 0, True, False, False),
    "weather_history": DataPolicy("weather_history", 30 * 86400, True, False, False),
}


def policy_of(data_type: str) -> DataPolicy:
    """取某数据类型的策略；未知类型给保守默认（7 天、可补全）。"""
    return POLICIES.get(data_type) or DataPolicy(data_type, 7 * 86400)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def is_stale(policy: DataPolicy, fetched_at: str | None,
             row_sources: str | None = None, now: datetime | None = None) -> bool:
    """判断数据是否超过 TTL。

    规则：
    - ttl_seconds == 0：知识类数据永不过期。
    - 来源是静态种子/人工核实（row_sources 含 seed/manual）：核实过的基线，
      不按 TTL 标 stale——它本来就是「口径说明」而非「实时值」。
    - 来源是在线抓取（auto-fetch/overpass）且 updated_at 超过 TTL → stale。
    - 在线抓取但没有时间戳：来源不明，保守标 stale。
    - fetched_at 为空且不是在线来源：按基线处理，不标。
    """
    if policy.ttl_seconds <= 0:
        return False
    srcs = row_sources or ""
    online = any(s.strip() in ("auto-fetch", "auto-fetch-llm", "overpass")
                 for s in srcs.split(",") if s.strip())
    if not online:
        return False
    dt = _parse_iso(fetched_at)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() > policy.ttl_seconds
