"""Reply Builder：统一回复构建（文档 4.1 的 Reply Builder 层）。

Reply 协议（mode/render/plan/slots）短期保持不变（文档第 10 节——前端按
mode 更新界面/地图，改协议必须先核对服务端序列化与前端消费）。
事实保真校验沿用 llm.compose_reply 既有机制（数字保真不达标回退模板）。

这些构造器只是把原来散在 _turn_impl 里的 Reply(...) 重复模式收拢；
规划 Reply 的字段多（assumptions/trace/tiers），仍由规划管道就地构造。
"""

from __future__ import annotations

from ..dialogue import Reply


def answer_reply(session, text: str) -> Reply:
    """问答类回复（mode=answer）：不携带方案，前端不刷地图。"""
    return Reply(mode="answer", turn_no=session.turn_no,
                 ask_rounds=session.ask_rounds, slots=dict(session.slots),
                 render=text)


def clarify_reply(session, question: str, asked: list[str]) -> Reply:
    """追问类回复（mode=clarify）：question/asked 供服务端与测试断言。"""
    return Reply(mode="clarify", turn_no=session.turn_no,
                 ask_rounds=session.ask_rounds, question=question,
                 asked=asked, slots=dict(session.slots), render=question)
