"""WeatherHandler（设计文档 §8）：天气咨询。

复用 Session._weather_answer：真实预报优先，超出预报窗口自动用
历史同期均值；首次天气问答带「可排行程」提示，之后不再重复。
"""

from __future__ import annotations

from ._base import resolve_city


class WeatherHandler:

    def handle(self, session, utterance: str) -> str:
        return session._weather_answer(resolve_city(session, utterance),
                                       utterance)
