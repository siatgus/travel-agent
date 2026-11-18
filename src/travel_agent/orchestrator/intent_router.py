"""IntentRouter：统一意图路由（规则优先 + LLM 兜底，规则没推翻）。

对应设计文档第 6 节。分类结果结构化（IntentResult），置信度只是辅助信号——
LLM 兜底只补足分类，绝不因置信度把规划决策交给模型；判成 plan 也不在这里
执行规划，照旧回落规则槽位管道。

现状与目标标签的映射：classify 的粗分类（weather/poi/transport/plan_qa/
smalltalk/general/other/plan）保持不变；poi 细分仍由 Session._poi_answer 内的
refine_intent 链路承担（第二阶段再拆独立 Handler）。本路由把原来散在
_turn_impl 里的「规则分类 → LLM 兜底 → spot_qa 规则兜底」收敛为一个入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import intent, llm

# LLM 兜底允许给出的标签（与原 _turn_impl 白名单一致，plan 不在其中——
# 模型判成规划也不直接执行，必须回到规则管道）
_LLM_KINDS = ("weather", "poi", "transport", "plan_qa", "smalltalk", "general")

# 非规划问答类：命中即由 Orchestrator 分发给 QA Handler，不进规划管道
QA_KINDS = ("weather", "poi", "transport", "smalltalk", "plan_qa", "general")


@dataclass
class IntentResult:
    """结构化分类结果（文档 6.4）。confidence 仅作观测信号，不参与决策。"""

    name: str
    confidence: float
    source: str                       # rule / llm / rule:spot_qa
    entities: dict = field(default_factory=dict)


class IntentRouter:
    """规则优先、LLM 兜底（受限）的意图路由。"""

    def route(self, session, utterance: str,
              parse=None) -> IntentResult:
        # ---- 规则分类 ----
        kind = intent.classify(utterance)
        source = "rule"
        # ---- LLM 兜底：规则认不出（other）且句子像提问时才调，一次 ----
        if kind == "other" and session.client is not None \
                and intent.looks_like_question(utterance):
            alt = llm.classify_intent(session.client, utterance)
            if alt in _LLM_KINDS:
                kind, source = alt, "llm"
        # ---- 城市实体：消费统一解析结果（Phase 2 迁移）----
        # ParseResult.query_city 已按「本句城市 > 行程目的地 > 最近城市」
        # 的同一优先级绑定查询城市；destination 是独立字段，查询类意图
        # 不会吸收本句城市（写入权限表）。无解析结果时走原逻辑兜底。
        if parse is not None:
            city = (parse.query_city or parse.destination
                    or session.last_city or session.trip_dest)
            entities = {"city": city,
                        "query_city": parse.query_city,
                        "destination": parse.destination}
        else:
            city = (intent.city_in(utterance) or session.slots.get("destination")
                    or session.last_city or session.trip_dest)
            entities = {"city": city}
        # ---- 规则兜底：other 且点名了收录景点在问细节（「宽窄巷子是什么地方」）
        # → 按景点问答处理。无模型时实测这句落进补槽位流程，被追问「想去哪儿」。
        if kind == "other" and intent.is_spot_qa(utterance) and city:
            kind, source = "poi", "rule:spot_qa"
        confidence = {"rule": 1.0, "rule:spot_qa": 0.95, "llm": 0.6}[source]
        return IntentResult(name=kind, confidence=confidence, source=source,
                            entities=entities)
