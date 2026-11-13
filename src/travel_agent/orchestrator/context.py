"""TurnContext：单轮上下文对象（Orchestrator 架构第一阶段）。

对应设计文档第 5 节：避免函数之间传递大量零散参数，把「这一轮处理到哪一步、
判成什么意图、命中哪个守卫」集中在一个对象里。

职责边界（文档原文）：
- Session：保存跨轮状态（历史、长期槽位、当前行程）——不在本对象里；
- TurnContext：只保存当前这一轮的处理中间状态；
- Handler：通过明确接口返回结果或状态变更，不随意持有并修改整个 Session；
- 不在 TurnContext 中持久化独立会话状态，避免与 Session 出现双份数据源。
  （slots/last_plan 存的是引用快照，供 Handler 只读；真正的状态仍在 Session。）
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TurnContext:
    """一轮消息的处理中间状态。生命周期 = 一次 _turn_impl 调用。"""

    utterance: str
    history: list                     # Session.history 的引用（只读）
    slots: dict                       # Session.slots 的引用（只读；写回仍走 Session）
    last_plan: dict | None
    plan_made: bool

    city: str | None = None           # 本轮解析出的城市（本句 > 行程目的地 > 最近提及）
    intent: str | None = None         # IntentRouter 给出的最终意图
    intent_source: str | None = None  # rule / llm / rule:spot_qa
    entities: dict = field(default_factory=dict)

    tool_results: dict = field(default_factory=dict)   # 本轮工具调用摘要
    route: str | None = None          # qa_nearby / qa_plan_spot / qa / plan / guard
    last_guard: str | None = None     # 命中的守卫名（便于排查与观测）
    handled: bool = False
