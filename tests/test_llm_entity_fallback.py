"""P1.5 契约测试：LLMCandidateAdapter Schema/白名单/非法输入 + 回流桩。

设计依据 docs/PHASE5_DESIGN.md v3.2.1 §2.4/§6。全部使用 LLM 测试桩
（不调用真实 LLM、不接业务链路、不改 Orchestrator）。
运行：python tests/test_llm_entity_fallback.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent.llm_entity_fallback import LLMCandidateAdapter
from travel_agent import entity_resolver as er

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL {name}: {detail}")


ORIGINAL = "南京那个民国时候的广播台旧址介绍一下"


def test_valid_payload_and_span() -> None:
    """交付 4：Schema 校验 + 白名单放行 + Span 真实性（T-I 首条）。"""
    ad = LLMCandidateAdapter()
    payload = {
        "schema_version": 1,
        "intent": "poi_qa",
        "entity_candidates": [
            {"text": "国民政府中央广播电台旧址", "entity_type": "attraction"},
            {"text": "广播台旧址", "entity_type": "attraction"},  # 原文精确子串
        ],
        "city_hints": {"query_city": "南京"},
        "notes": "补全为具体旧址名称",
    }
    r = ad.parse_payload(payload, ORIGINAL, role="query")
    check("合法 payload → ok", r.ok, str(r.notes))
    check("白名单五键全放行", len(r.candidates) == 2, str(len(r.candidates)))
    c0 = r.candidates[0]
    check("candidate_source=llm（与 user 候选严格区分）",
          c0["candidate_source"] == "llm" and c0["source"] == "llm_fallback",
          str(c0.get("candidate_source")))
    check("LLM 补全文本不在原文 → Span null 绝不伪造",
          c0["span_start"] is None and c0["span_end"] is None, str(c0))
    c1 = r.candidates[1]
    check("原文精确子串 → 真实 Span（左闭右开）",
          c1["span_start"] == 9 and c1["span_end"] == 14
          and ORIGINAL[9:14] == c1["text"],
          f"{c1['span_start']}..{c1['span_end']}")
    check("city_hints 只留 query_city",
          r.city_hints == {"query_city": "南京"}, str(r.city_hints))
    check("role 由调用方传入（LLM 无权决定）",
          all(c["role"] == "query" for c in r.candidates), "")
    check("intent 仅透传参考", r.intent == "poi_qa", r.intent)


def test_privileged_keys_dropped() -> None:
    """越权字段丢弃并记 notes：destination/plan/must_visit/role。"""
    ad = LLMCandidateAdapter()
    payload = {
        "schema_version": 1,
        "intent": "plan_modify",
        "entity_candidates": [
            {"text": "总统府", "entity_type": "attraction",
             "role": "must_visit"},                     # 候选内越权
        ],
        "destination": "南京",                          # 顶层越权
        "plan": {"days": 2},                            # 顶层越权
        "must_visit": ["总统府"],                       # 顶层越权
        "city_hints": {"query_city": "南京",
                       "destination": "南京"},          # hints 内越权
    }
    r = ad.parse_payload(payload, "介绍一下南京的总统府")
    check("越权字段不阻断候选产出", r.ok and len(r.candidates) == 1,
          str(r.notes))
    check("候选 role 保持调用方传入值，绝不变 must_visit",
          r.candidates[0]["role"] == "query", str(r.candidates[0]["role"]))
    joined = " ".join(r.notes)
    for kw in ("'destination'", "'plan'", "'must_visit'"):
        check(f"越权字段 {kw} 已记 notes", kw in joined, joined)
    check("city_hints.destination 被丢弃",
          "destination" not in r.city_hints, str(r.city_hints))
    check("越权字段不出现在任何输出结构",
          all(k not in r.candidates[0] for k in ("status", "confidence",
              "decision", "poi_id")), str(r.candidates[0]))


def test_invalid_inputs_conservative() -> None:
    """非法输入 → ok=False 整体丢弃走保守，绝不抛异常。"""
    ad = LLMCandidateAdapter()
    cases = [
        ("payload 非 dict", "not a dict"),
        ("schema_version 缺失", {"intent": "poi_qa",
                                 "entity_candidates": [{"text": "总统府"}]}),
        ("schema_version 不符", {"schema_version": 2,
                                 "entity_candidates": [{"text": "总统府"}]}),
        ("entity_candidates 缺失", {"schema_version": 1}),
        ("entity_candidates 非 list", {"schema_version": 1,
                                       "entity_candidates": "总统府"}),
        ("候选全无效", {"schema_version": 1,
                       "entity_candidates": [{"text": ""},
                                             "裸字符串"]}),
    ]
    for why, payload in cases:
        r = ad.parse_payload(payload, ORIGINAL)
        check(f"非法输入整体丢弃：{why}",
              r.ok is False and r.candidates == [], str(r.notes))

    r2 = ad.parse_payload(None, ORIGINAL)
    check("None payload 也不抛异常", r2.ok is False and r2.candidates == [],
          str(r2.notes))


def test_type_normalization_and_dedup() -> None:
    """类型白名单归 unknown；重复候选去重；上限截断。"""
    ad = LLMCandidateAdapter()
    payload = {
        "schema_version": 1,
        "entity_candidates": [
            {"text": "总统府", "entity_type": "sightseeing"},  # 白名单外
            {"text": "总统府"},                                 # 重复
            {"text": "南京博物院", "entity_type": "restaurant"},
        ],
    }
    r = ad.parse_payload(payload, "总统府和南京博物院")
    check("白名单外类型归 unknown 并记 notes",
          r.candidates[0]["entity_type"] == "unknown"
          and any("不在白名单" in n for n in r.notes), str(r.notes))
    check("重复候选去重", len(r.candidates) == 2, str(len(r.candidates)))
    check("合法类型照常放行",
          r.candidates[1]["entity_type"] == "restaurant", "")
    check("layer=0（兜底候选不属于识别层 1..5）",
          all(c["layer"] == 0 for c in r.candidates), "")

    many = {"schema_version": 1,
            "entity_candidates": [{"text": f"景点{i}"} for i in range(8)]}
    r2 = ad.parse_payload(many, "")
    check("超出上限截断为 5 并记 notes",
          len(r2.candidates) == 5 and any("上限" in n for n in r2.notes),
          str(len(r2.candidates)))


def test_llm_stub_reflow_to_resolver() -> None:
    """交付 5：LLM 测试桩——候选回流 EntityResolver 全链（无真实 LLM）。"""
    ad = LLMCandidateAdapter()

    class FakeLLM:
        """测试桩：模拟 LLM 结构化输出（P4 接真实模型前永远用这个）。"""
        def __init__(self, payload):
            self.payload = payload
            self.calls = 0

        def complete(self, prompt: str) -> dict:
            self.calls += 1
            return self.payload

    stub = FakeLLM({
        "schema_version": 1,
        "intent": "poi_qa",
        "entity_candidates": [
            {"text": "国民政府中央广播电台旧址", "entity_type": "attraction"}],
        "city_hints": {"query_city": "南京"},
    })
    r = ad.parse_payload(stub.complete("兜底提示词"), ORIGINAL, role="query")
    check("测试桩候选经适配器 ok", r.ok, str(r.notes))

    # 回流：adapter 产物直接交给 EntityResolver（KB 注入，纯离线）
    # ctx 用 query_city 提供 city_hints 对应的验证上下文（resolver 的
    # expected_city 只认 destination/trip_dest/last_city——city_hints
    # 由调用方决定如何并入 context，适配器本身不写 slots）
    kb = [("南京", {"name": "国民政府中央广播电台旧址",
                    "category": "attraction", "area": "鼓楼区"})]
    res = er.resolve(r.candidates, {"slots": {"destination": "南京"}},
                     kb_provider=lambda: kb, allow_online=False)
    rc = res.resolved[0]
    check("回流 Resolver：KB 命中 → confirmed",
          rc.status == "confirmed", str(rc.status))
    check("回流候选不被 KB-only 影子误伤",
          rc.probe_status == "not_applicable", str(rc.probe_status))
    check("桩只被调用一次（成本闸：每轮最多一次）", stub.calls == 1, "")

    # 兜底失败路径：整体丢弃 → 调用方拿不到候选走保守，不报错
    bad = ad.parse_payload({"schema_version": 1, "entity_candidates": []},
                           ORIGINAL)
    check("兜底产出为空 → ok=False 走保守不报错",
          bad.ok is False and bad.candidates == [], str(bad.notes))


def main() -> int:
    for fn in (test_valid_payload_and_span, test_privileged_keys_dropped,
               test_invalid_inputs_conservative,
               test_type_normalization_and_dedup,
               test_llm_stub_reflow_to_resolver):
        fn()
    print(f"\nllm adapter contract: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
