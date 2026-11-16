"""PlanQAHandler（设计文档 §8）：询问已有行程内容（「之前安排的酒店在哪里」）。

没出过方案就不拦：这句可能是在闲聊，交回排行程流程正常处理——
拦截条件与拆分前 Session._answer 的 plan_qa 分支逐字一致。
"""

from __future__ import annotations


class PlanQAHandler:

    def handle(self, session, utterance: str) -> str | None:
        if not (session.plan_made and session.last_plan):
            return None
        return session._plan_qa_answer(utterance)
