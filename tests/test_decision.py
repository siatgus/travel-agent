"""DecisionEngine 契约测试（Phase 5 P1，2026-11-18）。

断言全部来自 PHASE5_DESIGN.md v3.2.1 与 P1 验收标准，不是自己编的：
- 五档→六档固定映射（§2.3.1 逐格）
- 9 条阻断性验收项（错误城市不被高置信覆盖 / 类型冲突不 execute /
  分店不静默选 / 单源空≠rejected / entity=None 不 execute /
  pending 只在 clarify / 无候选不产生 skipped / deferred 非实体状态 /
  TAKEOVER=0 行为不变）
- DecisionResult 构造校验与序列化契约
- shadow_decision 纯记录

运行：python tests/test_decision.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import copy
import os
import tempfile
import types
os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="decision-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="decision-"), "t.db"))
os.environ.pop("TRAVEL_RESOLVER_ONLINE", None)
os.environ.pop("TRAVEL_DECISION_TAKEOVER", None)

from travel_agent import entity_resolver as er       # noqa: E402
from travel_agent import decision as de              # noqa: E402
from travel_agent.candidate_extractor import PoCandidate  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


def kb_provider(rows):
    return lambda: rows


def cand(text, entity_type="attraction", role="must_visit"):
    return PoCandidate(text=text, span_start=0, span_end=len(text),
                       entity_type=entity_type, role=role,
                       match_type="suffix_inferred", matched=False, layer=2)


def parse(text="介绍一下总统府", intent="poi_query", coarse="poi",
          query_city=None, destination=None):
    return types.SimpleNamespace(
        text=text, intent=intent, intent_coarse=coarse,
        query_city=query_city, destination=destination)


def resolve(cands, ctx, kb_rows=None, probe_fn=None, review_fn=None,
            allow_online=None):
    return er.resolve(cands, ctx, kb_provider=kb_provider(kb_rows or []),
                      probe_fn=probe_fn, review_fn=review_fn,
                      allow_online=allow_online)


def engine(**kw):
    return de.DecisionEngine(**kw)


# ------------------------------------------------ 映射（§2.3.1 逐格）


def test_five_to_six_mapping() -> None:
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"})]
    pr = parse(destination="成都")
    eng = engine()

    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    d = eng.decide(pr, r.resolved, ctx, takeover=True)
    check("confirmed → execute", d is not None and d.action == "execute",
          str(d and d.action))
    check("execute 带已验证实体", d.entity and d.entity.get("name")
          == "上海贝皇大排档", str(d.entity))
    check("city 归因 destination", d.city == "成都"
          and d.city_source == "destination", f"{d.city}/{d.city_source}")

    # ambiguous（跨城）→ clarify
    kb2 = [("南京", {"name": "国民政府中央广播电台旧址",
                     "category": "attraction", "area": "鼓楼区"})]
    r2 = resolve([cand("国民政府中央广播电台旧址")], ctx, kb2)
    d2 = eng.decide(parse(destination="上海"), r2.resolved, ctx, takeover=True)
    check("ambiguous → clarify", d2 is not None and d2.action == "clarify",
          str(d2 and d2.action))
    check("clarify 携带消歧说明与命中的硬约束编号",
          d2.reason and "HC-" in d2.reason, d2.reason)
    check("DecisionResult 无 status 字段（五档只在 Engine 内部）",
          not hasattr(d2, "status") and "status" not in d2.to_dict(), "")

    # rejected（双源均空）→ reject_reply
    def probe_empty(city, name, etype):
        return []

    def review_empty(city, name, etype):
        return []

    r3 = resolve([cand("幻影基地")], ctx, [], probe_fn=probe_empty,
                 review_fn=review_empty, allow_online=True)
    d3 = eng.decide(pr, r3.resolved, ctx, takeover=True)
    check("rejected → reject_reply", d3 is not None
          and d3.action == "reject_reply", str(d3 and d3.action))

    # unverified（单源空）→ conservative
    r4 = resolve([cand("幻影基地")], ctx, [], probe_fn=probe_empty,
                 allow_online=True)
    d4 = eng.decide(pr, r4.resolved, ctx, takeover=True)
    check("unverified（单源空）→ conservative", d4 is not None
          and d4.action == "conservative", str(d4 and d4.action))

    # skipped（品类词）→ category_path（意图需在批次白名单内才消费）
    r5 = resolve([cand("游乐园", "category")], ctx, [])
    eng5 = engine(batches=("category_query",))
    d5 = eng5.decide(parse(intent="category_query", coarse="food"),
                     r5.resolved, ctx, takeover=True)
    check("skipped（品类词）→ category_path", d5 is not None
          and d5.action == "category_path", str(d5 and d5.action))

    # 映射表本身：deferred 不在五档映射里
    check("STATUS_TO_ACTION 恰好五格",
          len(de.STATUS_TO_ACTION) == 5
          and "deferred" not in de.STATUS_TO_ACTION.values(),
          str(de.STATUS_TO_ACTION))


# ------------------------------------------------ 9 条阻断性验收项


def test_block1_city_not_rescued_by_confidence() -> None:
    """验收点1：错误城市不能被高置信度覆盖；HC-1 按意图选城市字段。"""
    ctx = {"slots": {"destination": "上海"}}
    kb = [("南京", {"name": "国民政府中央广播电台旧址", "category": "attraction",
                    "area": "鼓楼区"})]
    eng = engine()
    r = resolve([cand("国民政府中央广播电台旧址")], ctx, kb)
    check("前置：KB 全名命中 0.92，跨城扣分后仍 0.80（高置信）",
          abs(r.resolved[0].confidence - 0.80) < 1e-6,
          str(r.resolved[0].confidence))

    d = eng.decide(parse(destination="上海"), r.resolved, ctx, takeover=True)
    check("错误城市 0.92 分也 → clarify 不 execute", d is not None
          and d.action == "clarify", str(d and d.action))

    # 查询类：query_city=南京 与 destination=上海 不同 → 绝不判冲突
    d2 = eng.decide(parse(query_city="南京", destination="上海"),
                    r.resolved, ctx, takeover=True)
    check("HC-1：query_city 命中 → execute（合理决策差异）",
          d2 is not None and d2.action == "execute"
          and d2.city == "南京" and d2.city_source == "query_city",
          f"{d2 and d2.action}/{d2 and d2.city}/{d2 and d2.city_source}")


def test_block2_type_conflict_no_execute() -> None:
    ctx = {}
    kb = [("上海", {"name": "东发道", "category": "restaurant",
                    "area": "静安区"})]
    eng = engine()
    r = resolve([cand("东发道博物馆")], ctx, kb)
    d = eng.decide(parse(destination="上海"), r.resolved, ctx, takeover=True)
    check("验收点2：类型冲突 → clarify 不 execute", d is not None
          and d.action == "clarify", str(d and d.action))


def test_block3_branch_no_silent_pick() -> None:
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"}),
          ("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "武侯区"})]
    eng = engine()
    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    d = eng.decide(parse(destination="成都"), r.resolved, ctx, takeover=True)
    check("验收点3：分店歧义 → clarify 不静默选", d is not None
          and d.action == "clarify" and d.entity is None,
          f"{d and d.action}/{d and d.entity}")


def test_block4_single_source_empty_not_rejected() -> None:
    ctx = {"slots": {"destination": "成都"}}

    def probe_empty(city, name, etype):
        return []

    eng = engine()
    r = resolve([cand("幻影基地")], ctx, [], probe_fn=probe_empty,
                allow_online=True)
    check("前置：resolver 单源空 → unverified",
          r.resolved[0].status == "unverified", r.resolved[0].status)
    d = eng.decide(parse(destination="成都"), r.resolved, ctx, takeover=True)
    check("验收点4：单源空 → conservative 绝不 reject_reply", d is not None
          and d.action == "conservative" and d.action != "reject_reply",
          str(d and d.action))


def test_block56_contract_validation() -> None:
    """验收点5/6：构造校验——entity=None 不 execute；pending 只在 clarify。"""
    try:
        de.DecisionResult(action="execute", intent="poi_query", entity=None)
        check("验收点5：entity=None 不得 execute", False, "未抛出")
    except ValueError:
        check("验收点5：entity=None 不得 execute", True)
    try:
        de.DecisionResult(action="clarify", intent="poi_query",
                          pending=object())
        check("验收点6：clarify 可携带 pending", True)
    except ValueError:
        check("验收点6：clarify 可携带 pending", False, "误抛出")
    try:
        de.DecisionResult(action="execute", intent="poi_query",
                          entity={"name": "x"}, pending=object())
        check("pending 只能出现在 clarify", False, "未抛出")
    except ValueError:
        check("pending 只能出现在 clarify", True)
    try:
        de.DecisionResult(action="fly_to_moon", intent="x")
        check("非法 action 拒绝", False, "未抛出")
    except ValueError:
        check("非法 action 拒绝", True)


def test_block7_no_candidates_no_skipped() -> None:
    """验收点7：普通无候选结果 → None，绝不自动产生 skipped/category_path。"""
    eng = engine()
    r = resolve([], {"slots": {"destination": "成都"}}, [])
    d = eng.decide(parse(destination="成都"), r.resolved,
                   {"slots": {"destination": "成都"}},
                   takeover=True)  # 已接管 + 白名单意图，仍不形成结论
    check("验收点7：无候选 → None（非 skipped/category_path）", d is None,
          str(d))


def test_block8_deferred_not_entity_status() -> None:
    """验收点8：deferred 是执行策略动作，不进五档状态机。"""
    ctx = {"slots": {"destination": "上海"}}
    kb = [("上海", {"name": "豫园", "category": "attraction",
                    "area": "黄浦区"})]
    eng = engine()
    r = resolve([cand("豫园", role="negated")], ctx, kb)
    check("前置：negated 实体验证照常 → confirmed",
          r.resolved[0].status == "confirmed", r.resolved[0].status)
    d = eng.decide(parse(destination="上海"), r.resolved, ctx, takeover=True)
    check("HC-4：negated → deferred（交回排除链路）", d is not None
          and d.action == "deferred", str(d and d.action))
    check("deferred 决策不带 must_visit 语义",
          d.reason and "must_visit" not in d.reason, d.reason)
    check("五档状态集合不含 deferred",
          set(de.STATUS_TO_ACTION) == {"confirmed", "ambiguous", "rejected",
                                       "unverified", "skipped"}, "")


def test_block9_takeover_off_zero_change() -> None:
    """验收点9：TAKEOVER=0 → 返回 None + 影子记录；旧链路行为不变。"""
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"})]
    eng = engine()
    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    ctx_before = copy.deepcopy(ctx)
    resolved_before = [copy.deepcopy(x.as_dict()) for x in r.resolved]

    d = eng.decide(parse(destination="成都"), r.resolved, ctx)  # 默认 takeover=0
    check("验收点9：TAKEOVER=0 → None（决策已形成但不消费）", d is None,
          str(d))
    check("影子决策已记录", len(eng.shadow_log) == 1
          and eng.shadow_log[0]["action"] == "execute"
          and eng.shadow_log[0]["takeover"] is False,
          str(eng.shadow_log))
    check("context/slots 零改变", ctx == ctx_before, str(ctx))
    check("resolved 列表零改变",
          [copy.deepcopy(x.as_dict()) for x in r.resolved] == resolved_before,
          "")
    os.environ["TRAVEL_DECISION_TAKEOVER"] = "1"
    try:
        d2 = eng.decide(parse(destination="成都"), r.resolved, ctx)
        check("环境变量 TAKEOVER=1 + 默认白名单含 poi_query → 返回决策",
              d2 is not None and d2.action == "execute", str(d2))
    finally:
        os.environ.pop("TRAVEL_DECISION_TAKEOVER", None)


def test_batch_whitelist_and_shadow_sink() -> None:
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"})]
    sink_records = []
    eng = engine(batches=("plan_modify",), shadow_sink=sink_records.append)
    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    d = eng.decide(parse(destination="成都"), r.resolved, ctx, takeover=True)
    check("意图不在批次白名单 → None（已决策不接管=deferred 双语义 None 形态）",
          d is None, str(d))
    check("影子通过 sink 回调记录", len(sink_records) == 1
          and sink_records[0]["batch_enabled"] is False, str(sink_records))


def test_p1_acceptance_shadow_sink_isolation() -> None:
    """P1 验收注意事项①（P1.5 落地）：shadow_sink 抛异常必须被兜底
    隔离——不影响决策结果、不影响 shadow_log、不让用户请求失败。"""
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"})]

    def broken_sink(rec):
        raise RuntimeError("影子存储宕机")

    eng = engine(shadow_sink=broken_sink)
    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    d = eng.decide(parse(destination="成都", intent="poi_query",
                         coarse="poi"), r.resolved, ctx, takeover=True)
    check("sink 异常不影响决策产出（照常返回 DecisionResult）",
          d is not None and d.action == "execute", str(d))
    check("sink 异常不影响 shadow_log 追加",
          len(eng.shadow_log) == 1, str(eng.shadow_log))
    d2 = eng.decide(parse(destination="成都", intent="poi_query",
                          coarse="poi"), r.resolved, ctx, takeover=False)
    check("sink 异常不影响 TAKEOVER=0 语义（仍返回 None）", d2 is None,
          str(d2))


def test_p1_acceptance_city_source_fallback_attribution() -> None:
    """P1 验收注意事项②（P1.5 落地）：query_city 缺失回退继承
    destination 时，city_source 必须如实记录 destination——不能只记
    最终城市值（归因不可靠 Handler 猜）。"""
    ctx = {"slots": {"destination": "成都"}}
    kb = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                    "area": "锦江区"})]
    eng = engine()
    r = resolve([cand("上海贝皇大排档", "restaurant")], ctx, kb)
    # 查询类意图、本轮无 query_city → 回退继承目的地
    d = eng.decide(parse(destination="成都", intent="poi_query",
                         coarse="poi"), r.resolved, ctx, takeover=True)
    check("查询类无 query_city → city=继承的目的地值",
          d is not None and d.city == "成都", str(d and d.city))
    check("city_source 如实记 destination（不是 query_city）",
          d is not None and d.city_source == "destination",
          str(d and d.city_source))
    # 对照：本轮明确 query_city 时 city_source=query_city
    r2 = resolve([cand("总统府")],
                 {"slots": {"destination": "上海", "query_city": "南京"}},
                 [("南京", {"name": "总统府", "category": "attraction",
                            "area": "玄武区"})])
    d2 = eng.decide(parse(text="介绍一下南京的总统府", intent="poi_qa",
                          coarse="poi", query_city="南京",
                          destination="上海"), r2.resolved, {}, takeover=True)
    check("本轮明确 query_city → city_source=query_city",
          d2 is not None and d2.city == "南京"
          and d2.city_source == "query_city",
          f"{d2 and d2.city}/{d2 and d2.city_source}")


def test_engine_never_specifies_handler_or_writes() -> None:
    """边界：DecisionEngine 不指定 Handler、不改行程、不写 slots。"""
    import inspect
    src = inspect.getsource(de)
    check("决策模块无 Handler 直调（不出现 _spot_qa/plan_trip 调用）",
          "_spot_qa(" not in src and "plan_trip(" not in src, "")
    check("决策模块不写 destination（无 slots 赋值语句）",
          "slots[" not in src.replace("slots\") or \"", "")
          and "slots] =" not in src, "")


def test_serialization_contract() -> None:
    d = de.DecisionResult(action="conservative", intent="poi_query",
                          city="南京", city_source="query_city",
                          confidence=0.82,
                          reason="证据不足保持未验证")
    dd = d.to_dict()
    for key in ("action", "intent", "entity", "city", "city_source",
                "confidence", "reason", "pending"):
        check(f"to_dict 含 {key}", key in dd, str(sorted(dd)))
    check("confidence 保留三位", dd["confidence"] == 0.82, str(dd))
    check("conservative 无实体", dd["entity"] is None, str(dd))


def main() -> int:
    for fn in (test_five_to_six_mapping,
               test_block1_city_not_rescued_by_confidence,
               test_block2_type_conflict_no_execute,
               test_block3_branch_no_silent_pick,
               test_block4_single_source_empty_not_rejected,
               test_block56_contract_validation,
               test_block7_no_candidates_no_skipped,
               test_block8_deferred_not_entity_status,
               test_block9_takeover_off_zero_change,
               test_batch_whitelist_and_shadow_sink,
               test_p1_acceptance_shadow_sink_isolation,
               test_p1_acceptance_city_source_fallback_attribution,
               test_engine_never_specifies_handler_or_writes,
               test_serialization_contract):
        fn()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n共 {len(RESULTS)} 项断言，失败 {len(fails)} 项")
    if fails:
        for name, _, detail in fails:
            print(f"  FAIL {name}　{detail}")
        return 1
    print("DecisionEngine 契约测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
