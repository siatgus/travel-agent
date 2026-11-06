"""方案生成进度：给等待中的用户一个"现在到哪一步"的诚实反馈。

背景：首轮方案要联网查天气/路网/酒店/景点，实测 50 秒左右。前端原来只有
「正在思考…」和一句靠计时器猜出来的文案——用户无法判断是卡住了还是在跑。
这里由后端在真正进入每个阶段时写一条状态，前端轮询 /api/progress 显示。

只存内存、按 sid 覆盖，不落盘。读不到就返回 None，前端回落原来的分阶段文案。
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}
_MAX = 500          # 兜底防泄漏：超过就清掉最旧的一半


def begin(key: str) -> None:
    now = time.time()
    with _LOCK:
        if len(_STATE) > _MAX:
            for k in sorted(_STATE, key=lambda k: _STATE[k].get("t0", 0))[:_MAX // 2]:
                _STATE.pop(k, None)
        _STATE[key] = {"t0": now, "ts": now, "phase": "正在理解你的需求…"}


def phase(key: str, text: str) -> None:
    with _LOCK:
        st = _STATE.get(key)
        if st is not None:
            st["phase"] = text
            st["ts"] = time.time()


def finish(key: str) -> None:
    with _LOCK:
        _STATE.pop(key, None)


def get(key: str) -> dict | None:
    with _LOCK:
        st = _STATE.get(key)
        if st is None:
            return None
        return {"phase": st["phase"],
                "elapsed": int(time.time() - st["t0"])}
