"""LLM 语义兜底的候选适配器契约（Phase 5 P1.5）。

设计依据 docs/PHASE5_DESIGN.md v3.2.1 §2.4 / §6。本模块只交付
**适配器契约与校验逻辑**，不调用任何真实 LLM（P4 才接入），不接
业务链路（ Orchestrator 零改动），产物只能作为候选回流统一候选
集合 → EntityResolver → DecisionEngine，绝不允许旁路。

LLM 输出契约（§2.4 白名单五键，越权字段一律丢弃并记 notes）：
  {schema_version, intent, entity_candidates, city_hints, notes}

LLM 职责边界（v3.2.1 收缩后只有两项）：
  1. 实体文本补全（如「民国时候的广播台旧址」→ 具体候选名称）；
  2. 实体类型辅助识别（attraction / restaurant / hotel）。
LLM 不能决定角色（query/must_visit）、不能写入城市、不能产生任何
行程操作；role 由调用方（规则主链）显式传入，payload 中出现 role
一律按越权字段丢弃。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 适配器产出的 schema 版本（与 payload.schema_version 校验）
SCHEMA_VERSION = 1

# payload 顶层白名单五键（§2.4）
ALLOWED_TOP_KEYS = ("schema_version", "intent", "entity_candidates",
                    "city_hints", "notes")

# 越权字段：出现即丢弃并显式记 notes（§2.4 + 必测负向）
PRIVILEGED_KEYS = frozenset({
    "destination", "plan", "must_visit", "poi_id", "actions",
    "status", "confidence", "decision", "role",
})

# city_hints 内白名单：只接受 query_city（destination 出现即越权）
ALLOWED_HINT_KEYS = ("query_city",)

# 实体类型白名单（LLM 只辅助识别类型，白名单外归 unknown）
ENTITY_TYPES = ("attraction", "restaurant", "hotel", "unknown")

# 单次兜底候选上限（成本闸的一部分；超出丢弃并记 notes）
MAX_CANDIDATES = 5
MAX_NOTES = 5


@dataclass
class AdapterResult:
    """适配器产出：候选列表 + 归因说明，绝不包含决策字段。"""
    ok: bool = False                  # False = 整体丢弃走保守（不报错）
    intent: str = ""                  # LLM 建议的细分意图（仅供参考）
    candidates: list[dict] = field(default_factory=list)
    city_hints: dict = field(default_factory=dict)   # 只作验证上下文
    notes: list[str] = field(default_factory=list)


class LLMCandidateAdapter:
    """LLM JSON 输出 → 统一候选集合适配器（纯函数式，无副作用）。

    契约要点：
    - candidate_source 固定为 "llm"（与 user 候选严格区分，§6.1）；
    - Span 只在候选文本与原始用户输入**精确匹配**时给出真实
      （span_start/span_end，左闭右开）；改写/补全/推断一律 None，
      绝不伪造（§6.2）；
    - role 由调用方传入，payload 内任何 role 语义一律丢弃；
    - 输出 dict 与 candidate_extractor.PoCandidate.as_dict() 字段
      兼容（text/span_start/span_end/entity_type/role/match_type/
      matched/layer/city + candidate_source/source），可直接交给
      entity_resolver.resolve()。
    """

    def parse_payload(self, payload, original_text: str,
                      role: str = "query") -> AdapterResult:
        """解析 LLM payload。任何失败都降级为 ok=False + 说明，
        绝不抛异常打断业务（LLM 兜底是旁路，失败走保守）。"""
        out = AdapterResult()

        # ---- 顶层形状 ----
        if not isinstance(payload, dict):
            out.ok = False
            out.notes.append(f"payload 非 dict（{type(payload).__name__}），"
                             "整体丢弃走保守")
            return out

        # ---- schema_version ----
        ver = payload.get("schema_version")
        if ver != SCHEMA_VERSION:
            out.ok = False
            out.notes.append(f"schema_version 不符（{ver!r} ≠ "
                             f"{SCHEMA_VERSION}），整体丢弃走保守")
            return out

        # ---- 越权字段检测（先于白名单放行） ----
        privileged = [k for k in payload if k in PRIVILEGED_KEYS]
        for k in privileged:
            out.notes.append(f"越权字段 {k!r} 已丢弃（LLM 无权表达）")

        # ---- 白名单五键之外的未知键：忽略并记录 ----
        unknown = [k for k in payload
                   if k not in ALLOWED_TOP_KEYS and k not in PRIVILEGED_KEYS]
        for k in unknown:
            out.notes.append(f"未知字段 {k!r} 已忽略（白名单外）")

        # ---- intent（仅透传参考，不驱动路由） ----
        intent = payload.get("intent")
        if intent is not None:
            if isinstance(intent, str) and intent.strip():
                out.intent = intent.strip()
            else:
                out.notes.append("intent 非法（非空字符串），已忽略")

        # ---- entity_candidates ----
        raw_cands = payload.get("entity_candidates")
        if raw_cands is None:
            out.ok = False
            out.notes.append("entity_candidates 缺失，整体丢弃走保守")
            return out
        if not isinstance(raw_cands, list):
            out.ok = False
            out.notes.append(f"entity_candidates 非 list"
                             f"（{type(raw_cands).__name__}），整体丢弃走保守")
            return out

        seen_texts: set[str] = set()
        for i, item in enumerate(raw_cands):
            if not isinstance(item, dict):
                out.notes.append(f"候选[{i}] 非 dict，已跳过")
                continue
            # 候选内越权字段（role/must_visit 等）丢弃并记 notes
            bad = [k for k in item if k in PRIVILEGED_KEYS]
            for k in bad:
                out.notes.append(f"候选[{i}] 越权字段 {k!r} 已丢弃")
            text = (item.get("text") or "")
            if not isinstance(text, str) or not text.strip():
                out.notes.append(f"候选[{i}] text 非空字符串，已跳过")
                continue
            text = text.strip()
            if text in seen_texts:
                out.notes.append(f"候选[{i}] 与前序候选重复，已跳过")
                continue
            if len(out.candidates) >= MAX_CANDIDATES:
                out.notes.append(f"候选[{i}] 超出单次兜底上限"
                                 f"（{MAX_CANDIDATES}），已丢弃")
                continue
            etype = item.get("entity_type")
            if etype not in ENTITY_TYPES:
                if etype is not None:
                    out.notes.append(f"候选[{i}] entity_type={etype!r} "
                                     "不在白名单，归为 unknown")
                etype = "unknown"
            # Span：仅原文精确匹配才真实（左闭右开），否则 None
            span_start, span_end = None, None
            if original_text:
                pos = original_text.find(text)
                if pos >= 0:
                    span_start, span_end = pos, pos + len(text)
            out.candidates.append({
                "text": text,
                "span_start": span_start,
                "span_end": span_end,
                "entity_type": etype,
                "role": role,             # 调用方（规则主链）传入，非 LLM
                "match_type": "llm_fallback",
                "matched": False,         # 存在性校验是 EntityResolver 的事
                "layer": 0,               # 兜底候选不属于识别层 1..5
                "city": None,
                "candidate_source": "llm",
                "source": "llm_fallback",
            })
            seen_texts.add(text)

        if not out.candidates:
            out.ok = False
            out.notes.append("无有效候选，整体丢弃走保守")
            return out

        # ---- city_hints ----
        hints = payload.get("city_hints")
        if hints is not None:
            if not isinstance(hints, dict):
                out.notes.append(f"city_hints 非 dict"
                                 f"（{type(hints).__name__}），已忽略")
            else:
                for k, v in hints.items():
                    if k in ALLOWED_HINT_KEYS and isinstance(v, str) \
                            and v.strip():
                        out.city_hints[k] = v.strip()
                    elif k in PRIVILEGED_KEYS or k == "destination":
                        out.notes.append(f"city_hints.{k} 越权，已丢弃"
                                         "（LLM 不能写入城市）")
                    else:
                        out.notes.append(f"city_hints.{k} 不在白名单，"
                                         "已忽略")

        # ---- notes ----
        llm_notes = payload.get("notes")
        if llm_notes is not None:
            if isinstance(llm_notes, str) and llm_notes.strip():
                out.notes.append(f"LLM 备注：{llm_notes.strip()[:100]}")
            elif isinstance(llm_notes, list):
                for n in llm_notes[:MAX_NOTES]:
                    if isinstance(n, str) and n.strip():
                        out.notes.append(f"LLM 备注：{n.strip()[:100]}")

        out.ok = True
        return out
