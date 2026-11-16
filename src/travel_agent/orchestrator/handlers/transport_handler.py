"""TransportHandler（设计文档 §8）：城际班次咨询。

复用 Session._transport_answer：班次/时刻/票价来自真实班次库，
起点缺省用会话 origin，终点缺省用当前行程目的地。
"""

from __future__ import annotations

from ._base import resolve_city


class TransportHandler:

    def handle(self, session, utterance: str) -> str:
        return session._transport_answer(resolve_city(session, utterance),
                                         utterance)
