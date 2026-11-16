"""SmalltalkHandler（设计文档 §8）：闲聊/身份问句。

复用 Session._smalltalk_answer：带人设的自由作答，不冒充商业型号、
不编数字。
"""

from __future__ import annotations


class SmalltalkHandler:

    def handle(self, session, utterance: str) -> str:
        return session._smalltalk_answer(utterance)
