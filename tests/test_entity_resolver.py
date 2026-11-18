"""EntityResolver 单元回归（Phase 4，2026-11-18）。

断言全部来自第四阶段验收要求，不是自己编的期望：
- 四档分流：confirmed / ambiguous / rejected / unverified（+skipped）
- 铁律：在线探测失败 → unavailable ⇒ unverified，绝不等于 not_found
- 分店消歧：同名多记录全量列出，绝不静默合并
- 类型验证：餐厅 ≠ 景点，mismatch 强制 ambiguous
- 城市归属：跨城命中如实标注，不自动确认
- 只读契约：resolve 全程不写 .kb.db（对比探测前后 KB 内容）
- 影子隔离：异常兜底，ParseResult.shadow_resolved 只记录不驱动业务

运行：python tests/test_entity_resolver.py
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
os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="resolver-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="resolver-"), "t.db"))
os.environ.pop("TRAVEL_RESOLVER_ONLINE", None)

from travel_agent import entity_resolver as er       # noqa: E402
from travel_agent.candidate_extractor import PoCandidate, extract  # noqa: E402
from travel_agent.parser import parse_utterance      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def kb_provider(rows: list[tuple[str, dict]]):
    return lambda: rows


def cand(text, entity_type="attraction", role="must_visit", matched=False):
    return PoCandidate(text=text, span_start=0, span_end=len(text),
                       entity_type=entity_type, role=role,
                       match_type="suffix_inferred", matched=matched, layer=2)


def test_kb_unique_confirmed() -> None:
    rows = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                      "area": "锦江区"})]
    r = er.resolve([cand("上海贝皇大排档", "restaurant")],
                   {"slots": {"destination": "成都"}},
                   kb_provider=kb_provider(rows))
    rc = r.resolved[0]
    check("KB 唯一命中 → confirmed", rc.status == "confirmed", rc.status)
    check("城市一致 city_check=match", rc.city_check == "match", rc.city_check)
    check("类型一致 type_check=match", rc.type_check == "match", rc.type_check)
    check("置信度 0.92", abs(rc.confidence - 0.92) < 1e-6, str(rc.confidence))
    check("无需在线探测", rc.probe_status == "kb_hit", rc.probe_status)


def test_cross_city_mismatch() -> None:
    rows = [("南京", {"name": "国民政府中央广播电台旧址", "category": "attraction",
                      "area": "鼓楼区"})]
    r = er.resolve([cand("国民政府中央广播电台旧址")],
                   {"slots": {"destination": "上海"}},
                   kb_provider=kb_provider(rows))
    rc = r.resolved[0]
    check("跨城命中 → ambiguous 不自动确认", rc.status == "ambiguous", rc.status)
    check("city_check=mismatch", rc.city_check == "mismatch", rc.city_check)
    check("notes 如实标注归属城市",
          any("南京" in n and "上海" in n for n in rc.notes), str(rc.notes))


def test_multi_city_and_branch() -> None:
    two_cities = [("大理", {"name": "白沙", "category": "attraction"}),
                  ("丽江", {"name": "白沙", "category": "attraction"})]
    r = er.resolve([cand("白沙")], {"slots": {"destination": "大理"}},
                   kb_provider=kb_provider(two_cities))
    rc = r.resolved[0]
    check("跨城同名 → ambiguous/multi_city",
          rc.status == "ambiguous"
          and rc.ambiguity_reason == "multi_city",
          f"{rc.status}/{rc.ambiguity_reason}")
    check("两条命中全量保留", len(rc.matches) == 2, str(len(rc.matches)))

    branches = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                          "area": "锦江区"}),
                ("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                          "area": "武侯区"})]
    r2 = er.resolve([cand("上海贝皇大排档", "restaurant")],
                    {"slots": {"destination": "成都"}},
                    kb_provider=kb_provider(branches))
    rc2 = r2.resolved[0]
    check("同城同名分店 → ambiguous/branch_same_name",
          rc2.status == "ambiguous"
          and rc2.ambiguity_reason == "branch_same_name",
          f"{rc2.status}/{rc2.ambiguity_reason}")
    check("分店记录不合并", len(rc2.matches) == 2
          and {m.area for m in rc2.matches} == {"锦江区", "武侯区"},
          str([m.area for m in rc2.matches]))


def test_type_mismatch() -> None:
    rows = [("上海", {"name": "东发道", "category": "restaurant",
                      "area": "静安区"})]
    r = er.resolve([cand("东发道博物馆")],
                   {}, kb_provider=kb_provider(rows))
    rc = r.resolved[0]
    check("类型不符 → ambiguous/type_mismatch",
          rc.status == "ambiguous"
          and rc.ambiguity_reason == "type_mismatch",
          f"{rc.status}/{rc.ambiguity_reason}")
    check("type_check=mismatch", rc.type_check == "mismatch", rc.type_check)
    check("类型不符扣置信", rc.confidence < 0.75, str(rc.confidence))


def test_probe_contract() -> None:
    """铁律：unavailable ≠ not_found；rejected 必须有 not_found 依据。"""
    ctx = {"slots": {"destination": "成都"}}

    def probe_ok(city, name, etype):
        return [er.MatchRow(city=city, name=name, category="attraction",
                            source="amap_online", origin="online")]

    def probe_empty(city, name, etype):
        return []

    def probe_down(city, name, etype):
        raise ConnectionError("网络不可用")

    base = kb_provider([])
    r1 = er.resolve([cand("幻影基地")], ctx, kb_provider=base,
                    probe_fn=probe_ok, allow_online=True)
    check("在线命中 → confirmed/online_hit",
          r1.resolved[0].status == "confirmed"
          and r1.resolved[0].probe_status == "online_hit",
          f"{r1.resolved[0].status}/{r1.resolved[0].probe_status}")

    r2 = er.resolve([cand("幻影基地")], ctx, kb_provider=base,
                    probe_fn=probe_empty, allow_online=True)
    rc2 = r2.resolved[0]
    check("探测成功确认没有 → rejected/not_found",
          rc2.status == "rejected" and rc2.probe_status == "not_found",
          f"{rc2.status}/{rc2.probe_status}")
    check("rejected 保留原始表达", rc2.text == "幻影基地", rc2.text)

    r3 = er.resolve([cand("幻影基地")], ctx, kb_provider=base,
                    probe_fn=probe_down, allow_online=True)
    rc3 = r3.resolved[0]
    check("探测异常 → unverified/unavailable（绝不 rejected）",
          rc3.status == "unverified"
          and rc3.probe_status == "unavailable",
          f"{rc3.status}/{rc3.probe_status}")
    check("unavailable ≠ not_found", rc3.probe_status != "not_found",
          rc3.probe_status)

    r4 = er.resolve([cand("幻影基地")], ctx, kb_provider=base,
                    probe_fn=probe_ok, allow_online=False)
    rc4 = r4.resolved[0]
    check("KB-only 影子 → unverified/skipped",
          rc4.status == "unverified" and rc4.probe_status == "skipped",
          f"{rc4.status}/{rc4.probe_status}")


def test_no_city_online_unavailable() -> None:
    calls = []

    def probe(city, name, etype):
        calls.append(city)
        return []

    r = er.resolve([cand("幻影基地")], {}, kb_provider=kb_provider([]),
                   probe_fn=probe, allow_online=True)
    rc = r.resolved[0]
    check("无上下文城市 → 不发探测", not calls, str(calls))
    check("无上下文城市 → unverified/unavailable",
          rc.status == "unverified" and rc.probe_status == "unavailable",
          f"{rc.status}/{rc.probe_status}")


def test_category_skipped() -> None:
    r = er.resolve([cand("游乐园", "category")], {}, kb_provider=kb_provider([]))
    rc = r.resolved[0]
    check("品类候选 → skipped 不验证", rc.status == "skipped", rc.status)


def test_negated_role_preserved() -> None:
    rows = [("上海", {"name": "豫园", "category": "attraction",
                      "area": "黄浦区"})]
    r = er.resolve([cand("豫园", role="negated", matched=True)],
                   {"slots": {"destination": "上海"}},
                   kb_provider=kb_provider(rows))
    rc = r.resolved[0]
    check("否定候选照常验证 → confirmed",
          rc.status == "confirmed", rc.status)
    check("role 保留在结论里", rc.original["role"] == "negated",
          rc.original["role"])


def test_readonly_contract() -> None:
    """只读契约：resolve 前后 KB 行数不变（绝不 probe-then-store）。"""
    from travel_agent import kb as kb_mod
    db = kb_mod.get_kb()
    before = sum(len(db.list_pois(c) or []) for c in db.cities())

    def probe(city, name, etype):
        return [er.MatchRow(city=city, name=name, category="attraction",
                            source="amap_online", origin="online")]

    er.resolve([cand("全新未收录景点")], {"slots": {"destination": "成都"}},
               kb_provider=kb_provider([]), probe_fn=probe, allow_online=True)
    after = sum(len(db.list_pois(c) or []) for c in db.cities())
    check("resolve 全程只读：KB 行数不变", before == after,
          f"{before} → {after}")


def test_accepts_extractor_objects() -> None:
    """与第三阶段衔接：直接吃 extract() 的 PoCandidate 列表。"""
    ext = extract("那给我排上海贝皇大排档",
                  {"slots": {"destination": "成都"}, "plan_made": True})
    rows = [("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                      "area": "锦江区"})]
    r = er.resolve(ext.poi_candidates,
                   {"slots": {"destination": "成都"}},
                   kb_provider=kb_provider(rows))
    rc = next(x for x in r.resolved if x.text == "上海贝皇大排档")
    check("瀑布候选直接可验证 → confirmed", rc.status == "confirmed", rc.status)
    check("Span 原样保留", rc.original["span_start"] == ext.poi_candidates[0].span_start
          and rc.original["span_end"] == ext.poi_candidates[0].span_end,
          str(rc.original))


def test_as_dict_shape() -> None:
    rows = [("成都", {"name": "上海贝皇大排档", "category": "restaurant"})]
    r = er.resolve([cand("上海贝皇大排档", "restaurant")],
                   {"slots": {"destination": "成都"}},
                   kb_provider=kb_provider(rows))
    d = r.resolved[0].as_dict()
    for key in ("text", "status", "confidence", "probe_status", "matches",
                "city_check", "type_check", "ambiguity_reason", "notes",
                "span_start", "span_end", "entity_type", "role"):
        check(f"as_dict 含 {key}", key in d, str(sorted(d)))
    check("matches 可 JSON 化",
          all(isinstance(m, dict) and "city" in m for m in d["matches"]),
          str(d["matches"]))


def test_parser_shadow_wiring() -> None:
    """影子接线：shadow_resolved 只记录不驱动业务；异常不影响旧链路。"""
    er._default_kb_rows = lambda: [
        ("成都", {"name": "上海贝皇大排档", "category": "restaurant",
                  "area": "锦江区"})]
    pr = parse_utterance("那给我排上海贝皇大排档",
                         {"slots": {"destination": "成都"},
                          "plan_made": True})
    check("shadow_resolved 覆盖全部 POI 候选",
          len(pr.shadow_resolved) >= 1
          and any(r["text"] == "上海贝皇大排档" for r in pr.shadow_resolved),
          str([r.get("text") for r in pr.shadow_resolved]))
    rc = next(r for r in pr.shadow_resolved if r["text"] == "上海贝皇大排档")
    check("影子结论 confirmed", rc["status"] == "confirmed", rc["status"])
    check("旧链路业务不变：店名前缀的上海绝不写入 destination"
          "（继承语义：delta 不写 → 会话保持原目的地）",
          pr.to_legacy_delta()["destination"] is None,
          str(pr.to_legacy_delta()))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    saved = er.resolve
    er.resolve = boom
    try:
        pr2 = parse_utterance("那给我排上海贝皇大排档",
                              {"slots": {"destination": "成都"},
                               "plan_made": True})
    finally:
        er.resolve = saved
    check("影子验证异常被兜底：旧链路照常",
          pr2.to_legacy_delta()["destination"] is None
          and any("影子实体验证异常" in n for n in pr2.notes),
          str(pr2.notes))


def main() -> int:
    for fn in (test_kb_unique_confirmed, test_cross_city_mismatch,
               test_multi_city_and_branch, test_type_mismatch,
               test_probe_contract, test_no_city_online_unavailable,
               test_category_skipped, test_negated_role_preserved,
               test_readonly_contract, test_accepts_extractor_objects,
               test_as_dict_shape, test_parser_shadow_wiring):
        fn()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n共 {len(RESULTS)} 项断言，失败 {len(fails)} 项")
    if fails:
        for name, _, detail in fails:
            print(f"  FAIL {name}　{detail}")
        return 1
    print("EntityResolver 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
