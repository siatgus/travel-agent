"""Orchestrator 架构回归测试（2026-11-13，第一阶段）。

对应 xingye_orchestrator_design.md 的第一阶段落地：
- TurnContext：单轮上下文对象（字段与职责边界）
- IntentRouter：规则优先 + LLM 兜底（受限）的路由决策，结构化 IntentResult
- GuardPipeline：四道方案修改守卫按固定顺序执行
- Orchestrator.handle：调度顺序与原 _turn_impl 等价（周边 gate → 行程节点
  gate → 意图路由 → 问答分发 → 守卫 → 规划管道）

关键约定（文档第 5/6/7 节）：
- TurnContext 只存本轮中间状态，不复制会话状态（slots/last_plan 是引用）；
- LLM 兜底只补分类，绝不因置信度把规划决策交给模型；
- 守卫顺序不可合并、不可改。

跑法与其余测试一致：python tests/test_orchestrator.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import fields as dc_fields
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

_tmp = Path(tempfile.mkdtemp(prefix="travel_orch_test_"))
os.environ["TRAVEL_KB_DB"] = str(_tmp / "kb.db")   # 必须在 import kb 之前设置

from travel_agent import kb                                        # noqa: E402
from travel_agent.dialogue import Session                          # noqa: E402
from travel_agent.orchestrator import (                            # noqa: E402
    GuardPipeline, IntentResult, IntentRouter, Orchestrator, TurnContext,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


TODAY = date(2026, 11, 13)


def _setup_kb() -> None:
    """预置最小数据：宽窄巷子（周边锚点）+ 一家周边餐厅 + 种子全量。"""
    db = kb.get_kb()
    db.seed_if_empty()
    db.upsert_poi("成都", {"name": "宽窄巷子", "category": "attraction",
                           "area": "青羊区", "geo": [30.6630, 104.0530],
                           "appeal": 4, "duration_min": 120, "cost": 0,
                           "note": "编排器测试数据"}, source="seed")
    db.upsert_poi("成都", {"name": "少城小吃店", "category": "restaurant",
                           "area": "青羊区", "geo": [30.6635, 104.0535],
                           "cost": 45, "note": "人均 45 元（测试数据）"},
                  source="seed")


_setup_kb()


def _session() -> Session:
    """先用一句带城市的问答把 last_city 立起来（不手改 slots，见路由测试）。"""
    s = Session(today=TODAY, client=None)
    s.turn("成都有哪些值得去的景点")
    return s


# ------------------------------------------------------------------ TurnContext

def test_turn_context() -> None:
    """单轮上下文：字段齐全、中间状态与会话状态分离。"""
    names = {f.name for f in dc_fields(TurnContext)}
    need = {"utterance", "history", "slots", "last_plan", "plan_made",
            "city", "intent", "intent_source", "entities", "tool_results",
            "route", "last_guard", "handled"}
    check("TurnContext 字段齐全（文档第 5 节）", need <= names,
          str(need - names))
    ctx = TurnContext(utterance="你好", history=[], slots={},
                      last_plan=None, plan_made=False)
    check("默认值：未路由/未处理", ctx.route is None and not ctx.handled
          and ctx.tool_results == {})
    # 职责边界：slots 存引用，不复制会话状态
    s = _session()
    ctx2 = TurnContext(utterance="x", history=s.history, slots=s.slots,
                       last_plan=None, plan_made=False)
    check("slots/last_plan 是 Session 状态的引用（无双份数据源）",
          ctx2.slots is s.slots)


# ------------------------------------------------------------------ IntentRouter

def test_intent_router() -> None:
    """规则优先路由出结构化结果；无模型时不调 LLM 兜底。"""
    router = IntentRouter()
    s = _session()
    cases = [
        ("成都明天有雨吗", "weather", "rule"),
        ("你好", "smalltalk", "rule"),
        ("成都到重庆怎么走", "transport", "rule"),
        ("成都有什么特色美食", "poi", "rule"),      # 粗分类 poi，细分离由 _poi_answer
        ("成都三天怎么玩", "plan", "rule"),
        ("宽窄巷子是什么地方", "poi", "rule:spot_qa"),
    ]
    for t, exp_kind, exp_src in cases:
        ir = router.route(s, t)
        check(f"route[{t}]={exp_kind}({exp_src})",
              isinstance(ir, IntentResult) and ir.name == exp_kind
              and ir.source == exp_src,
              f"{ir.name}/{ir.source}")
    check("实体带城市（承接 last_city）",
          router.route(s, "宽窄巷子是什么地方").entities.get("city") == "成都",
          str(router.route(s, "宽窄巷子是什么地方").entities))


# ------------------------------------------------------------------ GuardPipeline

def test_guard_pipeline() -> None:
    """四道守卫按固定顺序注册；班次时刻守卫拦截改时刻指令。"""
    g = GuardPipeline()
    check("守卫顺序固定（文档 2.4：不合并成宽泛检查）",
          g.steps == ("stay_review", "transport_time_change",
                      "clock_anchor", "trim_optional"), str(g.steps))
    check("四道守卫方法都存在",
          all(hasattr(g, m) for m in g.steps))
    s = _session()
    orch = Orchestrator()
    r = orch.handle(s, "把去程到达时间改为09:00，去成都玩3天")
    check("改班次时刻被守卫拦截", r.mode == "answer"
          and "不能直接改数字" in r.render, r.render[:80])
    check("命中守卫记录在 TurnContext",
          orch.last_ctx is not None
          and orch.last_ctx.last_guard == "transport_time_change"
          and orch.last_ctx.route == "guard",
          f"{orch.last_ctx.last_guard}/{orch.last_ctx.route}")
    check("守卫拦下后没进规划管道（没出方案）", s.plan_made is False)


# ------------------------------------------------------------------ Orchestrator

def test_orchestrator_routing() -> None:
    """调度顺序等价：周边 gate → 节点 gate → 意图路由 → 问答 → 守卫 → 规划。"""
    orch = Orchestrator()
    s = _session()
    # 明确咨询预判：周边餐饮抢在最前
    r = orch.handle(s, "宽窄巷子附近有什么好吃的")
    check("周边问句走 qa_nearby", r.mode == "answer"
          and "少城小吃店" in r.render, r.render[:120])
    check("route=qa_nearby 且未进规划",
          orch.last_ctx.route == "qa_nearby" and orch.last_ctx.handled
          and s.plan_made is False)
    # 意图路由 + 问答分发
    r = orch.handle(s, "你好")
    check("闲聊走问答分发", r.mode == "answer"
          and orch.last_ctx.intent == "smalltalk"
          and orch.last_ctx.route == "qa", f"{orch.last_ctx.intent}")
    # 规划管道：缺日期先追问
    r = orch.handle(s, "成都三天怎么玩")
    check("规划句进规划管道先追问", r.mode == "clarify"
          and orch.last_ctx.route == "plan", f"{r.mode}")
    r = orch.handle(s, "11月14号出发，两个人")
    check("补齐后出方案", r.mode == "plan" and s.plan_made, f"{r.mode}")
    check("plan_trip 工具结果记录在 TurnContext",
          "plan_trip" in orch.last_ctx.tool_results,
          str(orch.last_ctx.tool_results))
    check("出方案后意图/城市落进上下文",
          orch.last_ctx.city == "成都", str(orch.last_ctx.city))


def test_orchestrator_plan_state_preserved() -> None:
    """跨轮状态：咨询与守卫不动行程（文档 12.3 跨轮状态测试）。"""
    orch = Orchestrator()
    s = _session()
    s.turn("成都三天怎么玩")
    s.turn("11月14号出发，两个人")
    check("前置：方案已出", s.plan_made and s.last_plan)
    snap = json.dumps(s.last_plan, ensure_ascii=False, sort_keys=True,
                      default=str)
    slots_snap = json.dumps(s.slots, ensure_ascii=False, sort_keys=True,
                            default=str)
    orch.handle(s, "宽窄巷子附近有什么好吃的")
    check("咨询后行程原封不动",
          json.dumps(s.last_plan, ensure_ascii=False, sort_keys=True,
                     default=str) == snap)
    check("咨询不污染规划槽位",
          json.dumps(s.slots, ensure_ascii=False, sort_keys=True,
                     default=str) == slots_snap)
    orch.handle(s, "把去程到达时间改为09:00")
    check("守卫拒绝后行程原封不动",
          json.dumps(s.last_plan, ensure_ascii=False, sort_keys=True,
                     default=str) == snap)


def main() -> int:
    test_turn_context()
    test_intent_router()
    test_guard_pipeline()
    test_orchestrator_routing()
    test_orchestrator_plan_state_preserved()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} "
          f"条 Orchestrator 架构断言通过")
    for name, _, detail in failed:
        print(f"  - 失败：{name}　{detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
