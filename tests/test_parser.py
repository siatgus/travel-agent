"""统一解析层（Phase 2）回归测试：字段契约 + 三城市字段拆分。

对应第二阶段验收标准：
1. parse_utterance / ParseResult 是唯一事实源；兼容层（to_legacy_delta）
   绝不把 query_city 写进 destination；
2. destination / query_city / departure_city 独立读写（写入权限表）；
3. 三个已发现问题不再复现：
   - 「从大理到玩5天」不产生 POI 候选；
   - 未提供人数时 people 保持 None；
   - 「上海有哪些好吃的」不把上海当品类词。
4. 消费方迁移回归：IntentRouter 消费 ParseResult；查询类意图经完整
   会话轮次后 session.slots.destination 不被查询城市覆盖。

每条用例独立临时数据目录（与既有测试同风格），离线可跑。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 隔离数据目录（必须在导入业务模块之前设置）
os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="parser-test-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="parser-test-"), "t.db"))

# —— src 布局引导 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

from travel_agent import intent, parser
from travel_agent.parser import EntityCandidate, ParseResult, parse_utterance
from travel_agent.dialogue import Session

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"　{detail}"))


TODAY = date(2026, 11, 17)


# ------------------------------------------------------------------ Schema 契约

def test_schema_contract() -> None:
    p = parse_utterance("12月20号去上海玩3天，2个人", today=TODAY)
    check("plan_create: destination=上海", p.destination == "上海", p.destination)
    check("plan_create: destination_writable=True", p.destination_writable is True)
    check("plan_create: query_city=None", p.query_city is None, p.query_city)
    check("plan_create: people=2", p.people == 2, p.people)
    check("plan_create: date.start=2026-12-20",
          (p.date or {}).get("start") == "2026-12-20", p.date)
    check("plan_create: 候选带 Span",
          all(c.span_start is not None for c in p.poi_candidates if c.source),
          [c.as_dict() for c in p.poi_candidates])

    # people=None 语义：未提供人数时绝不默认 1
    p = parse_utterance("从大理到丽江玩5天", today=TODAY)
    check("people 缺失保持 None（不默认 1）", p.people is None, p.people)
    check("A到B: departure_city=大理", p.departure_city == "大理", p.departure_city)
    check("A到B: destination=丽江", p.destination == "丽江", p.destination)
    check("A到B: 目的地不是出发地", p.destination != "大理")


def test_people_none_rule() -> None:
    for utt in ("12月20号去大理玩3天", "去上海玩", "成都三天怎么玩"):
        p = parse_utterance(utt, today=TODAY)
        check(f"未提供人数 people=None「{utt}」", p.people is None, p.people)
    # 追问人数后裸数字回答 → people 有值
    p = parse_utterance("2", today=TODAY,
                        context={"pending_field": "party_size"})
    check("追问人数后答「2」→ people=2", p.people == 2, p.people)
    # 没有追问上下文时裸数字不是人数
    p = parse_utterance("2", today=TODAY)
    check("无追问上下文裸数字不当人数", p.people is None, p.people)


# ------------------------------------------------------------------ 三城市字段

def test_city_field_separation() -> None:
    # 查询另一座城市：query_city 绑定本句城市，destination 只继承上下文
    ctx = {"slots": {"destination": "成都",
                     "date_range": {"start": "2026-12-20", "exact": True},
                     "party_size": 2},
           "party_stated": True, "plan_made": True}
    p = parse_utterance("上海有哪些好吃的", context=ctx, today=TODAY)
    check("query_city=上海（本句城市）", p.query_city == "上海", p.query_city)
    check("destination 保持成都（查询不覆盖）", p.destination == "成都", p.destination)
    check("destination_writable=False", p.destination_writable is False)
    check("本句未新增 destination", p.destination_new is None, p.destination_new)

    # 兼容层铁律：query_city 绝不出现在旧字段增量里
    legacy = p.to_legacy_delta()
    check("兼容层 destination 不含上海", legacy["destination"] != "上海",
          legacy["destination"])
    check("兼容层不含 query_city 键", "query_city" not in legacy)
    check("兼容层保留上下文日期", legacy["date_range"] == ctx["slots"]["date_range"])

    # 规划类意图可写 destination
    p = parse_utterance("从南京到上海重排", today=TODAY)
    check("重排句 destination_writable=True", p.destination_writable is True)
    check("重排句 destination=上海", p.destination == "上海", p.destination)
    check("重排句 departure_city=南京", p.departure_city == "南京", p.departure_city)

    # 查询类意图彼此独立：天气问句只写 query_city
    p = parse_utterance("北京天气怎么样",
                        context={"slots": {"destination": "大理"}},
                        today=TODAY)
    check("天气查询 query_city=北京", p.query_city == "北京", p.query_city)
    check("天气查询 destination 保持大理", p.destination == "大理", p.destination)


# ------------------------------------------------------------------ 三个已发现问题

def test_fixed_problems() -> None:
    # 问题 1：「从大理到玩5天」不产生 POI 候选
    p = parse_utterance("从大理到玩5天", today=TODAY)
    check("路线碎片不产生候选", p.poi_candidates == [],
          [c.name for c in p.poi_candidates])
    check("路线碎片 intent 不误报景点问答",
          p.intent in ("plan_create", "plan_modify", "general"), p.intent)

    # 问题 2 已在 test_people_none_rule 覆盖；这里补语料病例口径
    p = parse_utterance("12月20号去邯郸玩6天", today=TODAY)
    check("规划句未提人数 people=None", p.people is None, p.people)

    # 问题 3：城市名不当美食品类
    check("food_keyword 不再返回城市名（无 city 参数）",
          intent.food_keyword("上海有哪些好吃的") is None,
          intent.food_keyword("上海有哪些好吃的"))
    check("food_keyword 正常品类词仍在",
          intent.food_keyword("成都有哪些好吃的火锅店", "成都") == "火锅",
          intent.food_keyword("成都有哪些好吃的火锅店", "成都"))
    p = parse_utterance("上海有哪些好吃的",
                        context={"slots": {"destination": "成都"},
                                 "party_stated": True}, today=TODAY)
    check("C11 品类=restaurant 而非城市名", p.poi_category == "restaurant",
          p.poi_category)
    check("C11 intent=food_query", p.intent == "food_query", p.intent)


# ------------------------------------------------------------------ 候选与 Span

def test_candidates_and_spans() -> None:
    # 点名景点候选带 Span
    p = parse_utterance("国民政府中央广播电台旧址介绍一下", today=TODAY)
    c = p.poi_candidates[0] if p.poi_candidates else None
    check("长名景点候选抽出", c is not None and c.name == "国民政府中央广播电台旧址",
          c.name if c else "无候选")
    check("候选 Span 指向原文",
          c and p.text[c.span_start:c.span_end] == c.name,
          f"{c.span_start}:{c.span_end}" if c else "")
    check("候选 entity_type=attraction", c is None or c.entity_type == "attraction")

    # 店名跨词候选（排上+海贝皇）
    p = parse_utterance("那给我排上海贝皇大排档",
                        context={"slots": {"destination": "成都"},
                                 "plan_made": True}, today=TODAY)
    names = [c.name for c in p.poi_candidates]
    check("店名候选=海贝皇大排档", "海贝皇大排档" in names, names)
    check("候选不含上海（跨词污染已挡）",
          not any("上海" in n for n in names), names)
    c = next((c for c in p.poi_candidates if c.name == "海贝皇大排档"), None)
    check("店名候选 Span 指向原文",
          c and p.text[c.span_start:c.span_end] == c.name,
          f"{c.span_start}:{c.span_end}" if c else "")
    check("店名句 destination 保持成都", p.destination == "成都", p.destination)

    # 位置问句：候选为餐厅时品类给 restaurant
    p = parse_utterance("老驴头驴肉火烧在哪里",
                        context={"slots": {"destination": "邯郸"},
                                 "party_stated": True, "plan_made": True},
                        today=TODAY)
    check("位置问句 intent=poi_location_query",
          p.intent == "poi_location_query", p.intent)
    check("位置问句候选=老驴头驴肉火烧",
          any(c.name == "老驴头驴肉火烧" for c in p.poi_candidates),
          [c.name for c in p.poi_candidates])
    check("位置问句 query_city 继承邯郸", p.query_city == "邯郸", p.query_city)
    check("位置问句品类=restaurant", p.poi_category == "restaurant",
          p.poi_category)


# ------------------------------------------------------------------ 消费方迁移回归

def test_router_and_session_migration() -> None:
    # 完整会话：先排成都行程，再查询上海美食 → 目的地绝不被覆盖
    s = Session(today=TODAY)
    r1 = s.turn("12月20号去成都玩3天，2个人")
    check("成都行程已建立", s.plan_made and s.slots.get("destination") == "成都",
          s.slots.get("destination"))
    r2 = s.turn("上海有哪些好吃的")
    check("查询后 session.destination 仍=成都",
          s.slots.get("destination") == "成都", s.slots.get("destination"))
    check("查询后 session.query_city=上海", s.query_city == "上海", s.query_city)
    check("查询轮没有产出新方案", r2.plan is None, getattr(r2, "plan", None))

    # 兜底路径也守写入权限表：路由认不出的查询句漏进规划管道时，
    # 本句城市不得写入 destination
    s2 = Session(today=TODAY)
    s2.turn("12月20号去成都玩3天，2个人")
    s2.turn("这个行程怎么样")
    check("行程问答不改目的地", s2.slots.get("destination") == "成都",
          s2.slots.get("destination"))

    # 会话存档兼容：query_city 进快照也能恢复
    snap = s.snapshot()
    s3 = Session(today=TODAY)
    s3.restore(snap)
    check("快照恢复 query_city", s3.query_city == s.query_city, s3.query_city)


def test_router_entities() -> None:
    """IntentRouter 消费 ParseResult：entities 里能读到三字段。"""
    from travel_agent.orchestrator.intent_router import IntentRouter
    s = Session(today=TODAY)
    s.turn("12月20号去成都玩3天，2个人")
    parsed = parse_utterance("上海有哪些好吃的", context=s, today=TODAY)
    ir = IntentRouter().route(s, "上海有哪些好吃的", parse=parsed)
    check("router entities.query_city=上海",
          ir.entities.get("query_city") == "上海", ir.entities)
    check("router entities.destination=成都",
          ir.entities.get("destination") == "成都", ir.entities)
    check("router entities.city=上海（查询口径）",
          ir.entities.get("city") == "上海", ir.entities)
    # 不传 parse 时退回原逻辑（行为兼容）
    ir2 = IntentRouter().route(s, "上海有哪些好吃的")
    check("无 parse 时 router city 仍=上海", ir2.entities.get("city") == "上海",
          ir2.entities)


def test_legacy_delta_no_query_city() -> None:
    """兼容层铁律专项：任何查询结果转旧增量，destination 都不含查询城市。"""
    for utt, ctx in (
        ("上海有哪些好吃的", {"slots": {"destination": "成都"}, "party_stated": True}),
        ("北京天气怎么样", {"slots": {"destination": "大理"}}),
        ("南京中山陵景区附近有什么吃的", {"slots": {"destination": "成都"}}),
    ):
        p = parse_utterance(utt, context=ctx, today=TODAY)
        legacy = p.to_legacy_delta()
        check(f"兼容层 destination 无查询城市「{utt}」",
              legacy["destination"] not in (p.query_city,) if p.query_city
              else legacy["destination"] in (None, ctx["slots"].get("destination")),
              legacy["destination"])


def test_write_permission_gate() -> None:
    """写入权限闸（PlanHandler）：只拦查询问句，不拦无关键词的规划陈述句。"""
    # 规划陈述句（classify 认不出，靠规划管道兜底）：目的地必须照常写入
    s = Session(today=TODAY)
    r = s.turn("我想下个月去大理")
    check("无规划关键词的陈述句仍能写目的地",
          s.slots.get("destination") == "大理", s.slots.get("destination"))
    check("陈述句进入追问/规划流程", r.mode in ("clarify", "plan"), r.mode)

    # 查询问句漏进规划管道：本句城市只记 query_city，不覆盖目的地
    s2 = Session(today=TODAY)
    s2.turn("12月20号去成都玩3天，2个人")
    s2.turn("大理的机场大吗")
    check("查询问句不覆盖既有目的地",
          s2.slots.get("destination") == "成都", s2.slots.get("destination"))
    check("查询问句的城市记入 query_city", s2.query_city == "大理", s2.query_city)


def main() -> None:
    test_schema_contract()
    test_people_none_rule()
    test_city_field_separation()
    test_fixed_problems()
    test_candidates_and_spans()
    test_router_and_session_migration()
    test_router_entities()
    test_legacy_delta_no_query_city()
    test_write_permission_gate()
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = total - passed
    print(f"\n==== parser 测试：{passed}/{total} 通过，{failed} 失败 ====")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
