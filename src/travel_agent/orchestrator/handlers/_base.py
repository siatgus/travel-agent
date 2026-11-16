"""QA Handler 共享工具（避免 handlers/__init__ 与子模块循环导入）。"""

from __future__ import annotations


def resolve_city(session, utterance: str) -> str | None:
    """城市判定顺序（原 Session._answer 口径，逐字搬移）：
    本句里的城市 > 当前行程目的地 > 最近提到的城市 > 当前趟目的地。

    「天安门广场周边有什么」一句里未必带"北京"，但行程本身在北京。
    """
    from ... import intent
    return (intent.city_in(utterance) or session.slots.get("destination")
            or session.last_city or session.trip_dest)
