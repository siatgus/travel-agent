"""GeneralHandler（设计文档 §8）：通用知识问题（联网检索兜底）。"""

from __future__ import annotations


class GeneralHandler:

    def handle(self, session, utterance: str) -> str | None:
        return session._general_answer(utterance)
