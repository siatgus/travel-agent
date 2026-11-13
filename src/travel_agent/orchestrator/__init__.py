"""Orchestrator 包：调度层（xingye_orchestrator_design.md 第一阶段）。

    Session ──► Orchestrator（唯一调度入口）
                 ├─ Guard Pipeline（守卫与前置拦截，顺序固定）
                 ├─ Intent Router（规则优先 + LLM 兜底，结构化结果）
                 ├─ QA Handlers（本阶段复用 Session 既有问答方法）
                 ├─ Plan 管道（本阶段整段搬移，第三阶段拆 PlanHandler）
                 └─ Reply Builder（统一回复构建，协议不变）
"""

from .context import TurnContext
from .guard import GuardPipeline
from .intent_router import IntentResult, IntentRouter, QA_KINDS
from .orchestrator import Orchestrator
from .result import answer_reply, clarify_reply

__all__ = ["Orchestrator", "TurnContext", "IntentRouter", "IntentResult",
           "GuardPipeline", "QA_KINDS", "answer_reply", "clarify_reply"]
