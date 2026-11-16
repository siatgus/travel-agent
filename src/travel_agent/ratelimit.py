"""限流中间件：令牌桶，user（主）+ ip（辅）双维度。

企业化路线图 3.1 的第一道中间件——在业务代码之前拦住两类风险：

1. **短信轰炸**：send_code 按「手机号」+「IP」两把桶分别限，
   同号 60 秒 1 条、同 IP 每 2 分钟补 1 条（峰值 5）；
2. **刷接口/烧 LLM**：chat 按登录用户限流（默认峰值 10、每 6 秒补 1），
   未登录流量与共享出口按 IP 兜底；地图代理（OSRM/高德/天地图）按 IP 限，
   防止被当免费路由服务薅。

设计：
- **令牌桶**而非固定窗口：允许短突发、平滑长期速率，retry_after 精确到秒；
- **内存态**（线程安全）：单实例够用；多实例部署时把 _BUCKETS 换成 Redis
  即可，hit() 的签名不变；
- **阈值可配**：环境变量 TRAVEL_RATE_LIMITS 传 JSON 覆盖默认值
  （如 '{"chat_user": [5, 0.2]}' → 峰值 5、每秒补 0.2 个），
  管理后台热切换沿用 llm.settings 的文件指纹模式即可接入。

用法（server.py 的 do_POST 入口处）：
    ok, retry = ratelimit.hit(f"phone:{phone}", "send_code_phone")
    if not ok:
        self._json({"ok": False, "error": "rate_limited",
                    "retry_after": retry, "message": "..."}, 429)
"""

from __future__ import annotations

import json
import os
import threading
import time

_LOCK = threading.Lock()
_BUCKETS: dict[tuple[str, str], list] = {}   # (bucket名, key) -> [tokens, last_ts]

# bucket 名: (峰值容量, 每秒补充速率)
DEFAULT_CONFIG: dict[str, tuple[float, float]] = {
    "send_code_phone": (1, 1 / 60),      # 同一手机号：60 秒补 1 条，无突发
    "send_code_ip": (5, 1 / 120),        # 同一 IP 发码：峰值 5，2 分钟补 1
    "login_ip": (10, 1 / 30),            # 同一 IP 登录：峰值 10，30 秒补 1
    "chat_user": (10, 1 / 6),            # 每用户对话：峰值 10 次，6 秒补 1
    "chat_user_paid": (20, 1 / 2),       # 付费档对话：峰值 20 次，2 秒补 1
    "chat_ip": (20, 1 / 3),              # 同一 IP 对话兜底（多设备共享出口）
    "proxy_ip": (30, 1 / 2),             # 地图/路由代理：峰值 30，2 秒补 1
}


def _config() -> dict[str, tuple[float, float]]:
    cfg = dict(DEFAULT_CONFIG)
    raw = os.environ.get("TRAVEL_RATE_LIMITS", "").strip()
    if raw:
        try:
            over = json.loads(raw)
            for k, v in over.items():
                if k in cfg and isinstance(v, (list, tuple)) and len(v) == 2:
                    cfg[k] = (float(v[0]), float(v[1]))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass                             # 配置坏了不拦业务，回落默认值
    return cfg


def hit(key: str, bucket: str) -> tuple[bool, int]:
    """消耗 1 个令牌。(ok, retry_after)；retry_after 秒后重试（ok=True 时为 0）。"""
    cfg = _config().get(bucket)
    if cfg is None:
        return True, 0                       # 未登记的桶不拦（防御性）
    capacity, rate = cfg
    now = time.monotonic()
    with _LOCK:
        slot = _BUCKETS.get((bucket, key))
        if slot is None:
            slot = [capacity, now]
            _BUCKETS[(bucket, key)] = slot
        tokens, last = slot
        tokens = min(capacity, tokens + (now - last) * rate)
        if tokens >= 1.0:
            slot[0] = tokens - 1.0
            slot[1] = now
            return True, 0
        slot[0] = tokens
        slot[1] = now
        retry = int(-(-(1.0 - tokens) / rate))       # ceil((1-tokens)/rate)
        return False, max(1, retry)


def client_ip(handler) -> str:
    """取客户端 IP：反代场景吃 X-Forwarded-For 第一跳，否则 socket 对端。"""
    xff = handler.headers.get("X-Forwarded-For") or ""
    first = xff.split(",")[0].strip()
    if first:
        return first
    return handler.client_address[0] if handler.client_address else "unknown"


def reset(bucket: str | None = None) -> None:
    """清空桶（测试用）。传 bucket 只清该类。"""
    with _LOCK:
        if bucket is None:
            _BUCKETS.clear()
        else:
            for k in [k for k in _BUCKETS if k[0] == bucket]:
                _BUCKETS.pop(k, None)
