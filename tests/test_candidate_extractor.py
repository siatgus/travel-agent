"""CandidateExtractor 五层瀑布的单元回归（Phase 3，2026-11-18）。

断言全部来自第三阶段验收要求与语料病例，不是我自己编的期望：
- Span 契约：左闭右开、基于原始用户输入
- 「排上海贝皇大排档」绝不产生上海城市候选（城市前缀并入店名）
- 「从大理到玩5天」绝不产生 POI 候选（路线碎片）
- 否定（不想去）与删除（去掉）必须区分
- 品类词（火锅）不是 POI
- 普通描述不虚构 POI
- 多实体各自独立（去完总统府再去鸡鸣寺）

运行：python tests/test_candidate_extractor.py
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

import os
import tempfile
os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="cand-test-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="cand-test-"), "t.db"))

from travel_agent.candidate_extractor import extract  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def pois(text, ctx=None):
    return extract(text, ctx).poi_candidates


def cities(text, ctx=None):
    return extract(text, ctx).city_candidates


def test_span_contract() -> None:
    """Span 契约：左闭右开、基于原始输入，round-trip 必须还原候选文本。"""
    r = extract("那给我排上海贝皇大排档",
                {"slots": {"destination": "成都"}, "plan_made": True})
    c = r.poi_candidates[0]
    check("候选完整含城市前缀", c.text == "上海贝皇大排档", c.text)
    check("Span 还原原文（左闭右开）",
          "那给我排上海贝皇大排档"[c.span_start:c.span_end] == c.text,
          f"{c.span_start}:{c.span_end}")
    check("城市前缀不发独立城市候选",
          not any(x.text == "上海" for x in r.city_candidates),
          str([(x.text, x.role) for x in r.city_candidates]))


def test_layer1_kb_longest_match() -> None:
    """L1：KB 全名最长匹配优先、matched=True。"""
    r = extract("国民政府中央广播电台旧址介绍一下")
    c = r.poi_candidates[0]
    check("长全名整体识别", c.text == "国民政府中央广播电台旧址", c.text)
    check("KB 命中标记", c.matched and c.match_type == "kb_full", c.match_type)
    r2 = extract("国民政府中央广播电台旧址介绍一下")
    check("Span 与原文一致",
          "国民政府中央广播电台旧址介绍一下"[
              r2.poi_candidates[0].span_start:r2.poi_candidates[0].span_end]
          == "国民政府中央广播电台旧址")
    # 长名优先于部分命中：不得只抽出「广播电台」这类碎片
    check("没有碎片候选", len(r.poi_candidates) == 1,
          str([c.text for c in r.poi_candidates]))


def test_layer2_type_suffix() -> None:
    """L2：类型后缀生成未知候选，matched=False、类型按词表优先级。"""
    r = extract("那给我排上海贝皇大排档",
                {"slots": {"destination": "成都"}, "plan_made": True})
    c = r.poi_candidates[0]
    check("未知餐厅候选", c.entity_type == "restaurant", c.entity_type)
    check("matched=False（词典未命中，不代表不存在）", c.matched is False)
    check("match_type=suffix_inferred", c.match_type == "suffix_inferred",
          c.match_type)
    check("规划句默认角色 must_visit", c.role == "must_visit", c.role)


def test_layer2b_intent_only() -> None:
    """L2b：动词挂着的无后缀名词（小笼包）保留为 unknown 候选。"""
    r = extract("第一天给我安排小笼包",
                {"slots": {"destination": "上海"}, "plan_made": True})
    cands = [c for c in r.poi_candidates if c.text == "小笼包"]
    check("无后缀点名保留 unknown 候选", len(cands) == 1,
          str([c.text for c in r.poi_candidates]))
    if cands:
        check("unknown 候选 matched=False", cands[0].matched is False)
        check("unknown 候选角色 must_visit", cands[0].role == "must_visit",
              cands[0].role)


def test_layer3_roles() -> None:
    """L3：否定与删除必须区分；周边锚点与点名区分。"""
    r = extract("不想去豫园", {"slots": {"destination": "上海"},
                              "plan_made": True})
    check("否定角色 negated", r.poi_candidates[0].role == "negated",
          r.poi_candidates[0].role)
    r2 = extract("去掉苍山", {"slots": {"destination": "大理"},
                              "plan_made": True})
    check("删除角色 remove", r2.poi_candidates[0].role == "remove",
          r2.poi_candidates[0].role)
    r3 = extract("南京中山陵景区附近有什么吃的")
    check("周边锚点角色", r3.poi_candidates[0].role == "nearby_anchor",
          r3.poi_candidates[0].role)


def test_layer4_cities() -> None:
    """L4：城市只用未占用 Span；三角色分立；路线框架特殊。"""
    r = extract("上海有哪些好吃的")
    cs = r.city_candidates
    check("查询城市 query_city", len(cs) == 1 and cs[0].role == "query_city",
          str([(c.text, c.role) for c in cs]))
    check("查询句不产生 POI 候选", not r.poi_candidates,
          str([c.text for c in r.poi_candidates]))
    r2 = extract("12月20号去上海玩3天", {"plan_made": False})
    check("规划句城市 destination", r2.city_candidates[0].role == "destination",
          r2.city_candidates[0].role)
    r3 = extract("从大理到丽江")
    roles = {c.text: c.role for c in r3.city_candidates}
    check("路线框架出发地", roles.get("大理") == "departure_city", str(roles))
    check("路线框架目的地", roles.get("丽江") == "destination", str(roles))
    check("路线表达不产生 POI 候选", not r3.poi_candidates,
          str([c.text for c in r3.poi_candidates]))
    # 城市识别排除：城市名在 POI Span 内（南翔馒头店在上海语境不算城市候选）
    r4 = extract("南京总统府附近有什么餐厅")
    check("城市名未占用时才发候选",
          any(c.text == "南京" and c.role == "query_city"
              for c in r4.city_candidates),
          str([(c.text, c.role) for c in r4.city_candidates]))


def test_layer5_conflict() -> None:
    """L5：重叠丢弃 + 多实体保留。"""
    r = extract("去完总统府再去鸡鸣寺", {"plan_made": True})
    texts = [c.text for c in r.poi_candidates]
    check("两个独立景点都保留",
          "总统府" in texts and "鸡鸣寺" in texts, str(texts))
    spans = [(c.span_start, c.span_end) for c in r.poi_candidates]
    check("Span 互不重叠",
          all(not (s < e2 and s2 < e)
              for i, (s, e) in enumerate(spans)
              for s2, e2 in spans[i + 1:]),
          str(spans))


def test_negative_cases() -> None:
    """验收负向断言：路线碎片、普通描述、品类不是 POI。"""
    r = extract("从大理到玩5天")
    check("「从大理到玩5天」无 POI 候选", not r.poi_candidates,
          str([c.text for c in r.poi_candidates]))
    check("「到」后无城市时目的地留空并注明",
          not any(c.role == "destination" for c in r.city_candidates)
          and any("目的地未知" in n for n in r.notes),
          str(r.notes))
    r2 = extract("这个地方风景很好")
    check("普通描述不虚构 POI", not r2.poi_candidates and not r2.city_candidates,
          str([c.text for c in r2.poi_candidates]))
    r3 = extract("成都有哪些好吃的火锅店")
    cats = [c for c in r3.poi_candidates if c.entity_type == "category"]
    check("火锅是品类不是 POI", len(cats) == 1 and cats[0].text == "火锅",
          str([c.text for c in r3.poi_candidates]))
    check("品类句城市进 query_city",
          any(c.text == "成都" and c.role == "query_city"
              for c in r3.city_candidates),
          str([(c.text, c.role) for c in r3.city_candidates]))


def test_branch_suffix() -> None:
    """分店表达：完整分店名作为一条候选。"""
    r = extract("海贝皇海鲜大排档郑和南路店")
    check("完整分店名一条候选",
          r.poi_candidates[0].text == "海贝皇海鲜大排档郑和南路店",
          str([c.text for c in r.poi_candidates]))
    check("店名内的品类词不算独立候选",
          not any(c.text == "海鲜" for c in r.poi_candidates),
          str([c.text for c in r.poi_candidates]))


def test_shadow_no_business_impact() -> None:
    """影子运行：ParseResult.shadow_candidates 只记录，不改变旧行为。"""
    from travel_agent.parser import parse_utterance
    p = parse_utterance("排上海贝皇大排档",
                        context={"slots": {"destination": "成都"},
                                 "plan_made": True})
    check("影子候选已记录", any(c["text"] == "上海贝皇大排档"
                                for c in p.shadow_candidates),
          str(p.shadow_candidates))
    check("query_city 不经影子写入 destination",
          p.destination_new is None and p.destination == "成都",
          f"dest={p.destination} new={p.destination_new}")
    p2 = parse_utterance("从大理到丽江玩5天")
    d = p2.to_legacy_delta()
    check("旧行为不变：路线句兼容层照旧", d.get("destination") == "丽江"
          and p2.departure_city == "大理",
          f"dest={d.get('destination')} dep={p2.departure_city}")


def main() -> int:
    test_span_contract()
    test_layer1_kb_longest_match()
    test_layer2_type_suffix()
    test_layer2b_intent_only()
    test_layer3_roles()
    test_layer4_cities()
    test_layer5_conflict()
    test_negative_cases()
    test_branch_suffix()
    test_shadow_no_business_impact()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条候选识别断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
