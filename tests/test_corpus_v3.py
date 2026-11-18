"""corpus v3：第三阶段候选识别专项语料（2026-11-18）。

25 条 = 历史 12 条（C01-C12）+ 新增 13 条专项（T01-T13）。
断言对象是 CandidateExtractor 的候选（text / Span / entity_type / role /
matched），与 v1/v2 的行为断言互补：
- Span 契约：左闭右开、基于原始输入，round-trip 必须还原候选文本
- negatives：no_city=上海 / no_poi / no_destination / no_poi_with_suffix=X

同时输出新旧解析器差异（旧：parse_utterance().poi_candidates 的剥词产物；
新：extract() 五层瀑布候选），逐条分类：recall-gain（新多出的候选）、
dropped（旧有新无，需人工确认）、role/type-diff（都有但属性不同）。

运行：python tests/test_corpus_v3.py（纯解析、无联网、秒级）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 纯解析无写库，但仍隔离数据目录，避免读到真实 kb.db 里的漂移实体
os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="corpus3-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="corpus3-"), "t.db"))

from travel_agent.candidate_extractor import extract        # noqa: E402
from travel_agent.parser import parse_utterance             # noqa: E402

TSV = Path(__file__).resolve().parent / "corpus_v3.tsv"
REPORT = Path(__file__).resolve().parent / "corpus_v3_report.txt"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def load_cases() -> list[dict]:
    import csv
    with open(TSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return rows


def run_case(case: dict) -> None:
    cid, utt = case["case_id"], case["utterance"]
    ctx = json.loads(case["context"] or "{}")
    exp_pois = json.loads(case["expected_pois"] or "[]")
    exp_cities = json.loads(case["expected_cities"] or "[]")
    negatives = [n for n in (case["negatives"] or "").split(";") if n]

    r = extract(utt, ctx)

    # ---- Span 契约（对所有候选一律检查）----
    span_ok = True
    for c in r.poi_candidates + r.city_candidates:
        if utt[c.span_start:c.span_end] != c.text:
            span_ok = False
    check(f"[{cid}] Span 左闭右开且还原原文", span_ok,
          str([(c.text, c.span_start, c.span_end)
               for c in r.poi_candidates]))

    # ---- POI 候选逐条对比（按 text 配对）----
    got = {c.text: c for c in r.poi_candidates}
    for exp in exp_pois:
        c = got.get(exp["text"])
        ok = c is not None
        detail = "" if ok else f"缺失；实际 {[c.text for c in r.poi_candidates]}"
        if ok:
            for field in ("entity_type", "role", "match_type"):
                if exp.get(field) and getattr(c, field) != exp[field]:
                    ok, detail = False, (f"{field}: 期望 {exp[field]} "
                                         f"实际 {getattr(c, field)}")
            if "matched" in exp and c.matched != exp["matched"]:
                ok, detail = False, f"matched: 期望 {exp['matched']} 实际 {c.matched}"
        check(f"[{cid}] POI「{exp['text']}」符合预期", ok, detail)
    extra = [c.text for c in r.poi_candidates
             if c.text not in {e["text"] for e in exp_pois}]
    check(f"[{cid}] 无多余 POI 候选", not extra, str(extra))

    # ---- 城市候选 ----
    got_c = {c.text: c for c in r.city_candidates}
    for exp in exp_cities:
        c = got_c.get(exp["text"])
        ok = c is not None and c.role == exp["role"]
        detail = "" if ok else (
            f"缺失或角色不符；实际 "
            f"{[(x.text, x.role) for x in r.city_candidates]}")
        check(f"[{cid}] 城市「{exp['text']}」role={exp['role']}", ok, detail)
    extra_c = [c.text for c in r.city_candidates
               if c.text not in {e["text"] for e in exp_cities}]
    check(f"[{cid}] 无多余城市候选", not extra_c, str(extra_c))

    # ---- 负向断言 ----
    for neg in negatives:
        neg = neg.strip()
        if not neg:
            continue
        if neg == "no_poi":
            check(f"[{cid}] 负向：不产生 POI 候选", not r.poi_candidates,
                  str([c.text for c in r.poi_candidates]))
        elif neg == "no_destination":
            check(f"[{cid}] 负向：无 destination 角色",
                  not any(c.role == "destination" for c in r.city_candidates),
                  str([(c.text, c.role) for c in r.city_candidates]))
        elif neg.startswith("no_city="):
            city = neg.split("=", 1)[1]
            check(f"[{cid}] 负向：不产生城市候选「{city}」",
                  not any(c.text == city for c in r.city_candidates),
                  str([(c.text, c.role) for c in r.city_candidates]))
        elif neg.startswith("no_poi_with_suffix="):
            suf = neg.split("=", 1)[1]
            check(f"[{cid}] 负向：无以「{suf}」泛指产生的候选",
                  not any(c.text.endswith(suf) and c.match_type == "suffix_inferred"
                          for c in r.poi_candidates),
                  str([c.text for c in r.poi_candidates]))


def old_pipeline_candidates(utt: str, ctx: dict) -> list[dict]:
    """旧解析链的候选产物（第二阶段 ParseResult.poi_candidates）。"""
    p = parse_utterance(utt, context=ctx)
    return [{"text": c.name, "entity_type": c.entity_type,
             "role": c.source, "matched": None}
            for c in p.poi_candidates]


def build_report(rows: list[dict]) -> str:
    lines = []
    sep = "=" * 72
    lines.append(sep)
    lines.append("corpus v3 报告（Phase 3 候选识别专项 + 新旧解析器差异）")
    lines.append(f"生成：候选层五层瀑布（candidate_extractor.extract）")
    lines.append(sep)
    counts = {"PASS": 0, "FAIL": 0}
    for case in rows:
        cid, utt = case["case_id"], case["utterance"]
        ctx = json.loads(case["context"] or "{}")
        r = extract(utt, ctx)
        before = len(RESULTS)
        run_case(case)
        case_results = RESULTS[before:]
        n_pass = sum(1 for _, ok, _ in case_results if ok)
        n_all = len(case_results)
        status = "PASS" if n_pass == n_all else "FAIL"
        counts[status] += 1
        lines.append("")
        lines.append(f"[{cid}] {case['category']}  「{utt}」  →  {status}"
                     f"（{n_pass}/{n_all} 断言）")
        for c in r.poi_candidates:
            lines.append(f"    POI  {c.as_dict()}")
        for c in r.city_candidates:
            lines.append(f"    CITY {c.as_dict()}")
        if r.notes:
            lines.append(f"    notes: {r.notes}")
        if status == "FAIL":
            for name, ok, detail in case_results:
                if not ok:
                    lines.append(f"    FAIL: {name}　{detail}")
        # ---- 新旧差异（影子对比，不判 PASS/FAIL，只记录分类）----
        old = old_pipeline_candidates(utt, ctx)
        new = [{"text": c.text, "entity_type": c.entity_type, "role": c.role,
                "matched": c.matched} for c in r.poi_candidates]
        old_texts = {o["text"] for o in old}
        new_texts = {n["text"] for n in new}
        gains = [n for n in new if n["text"] not in old_texts]
        dropped = [o for o in old if o["text"] not in new_texts]
        both = [n for n in new if n["text"] in old_texts]
        if gains:
            lines.append(f"    差异[recall-gain]  新增候选: {gains}")
        if dropped:
            lines.append(f"    差异[dropped]      旧有新无（需人工确认）: {dropped}")
        for n in both:
            o = next(x for x in old if x["text"] == n["text"])
            if o["role"] != n["role"]:
                lines.append(f"    差异[role-diff]    「{n['text']}」"
                             f"旧 role={o['role']} 新 role={n['role']}")
    lines.append("")
    lines.append(sep)
    lines.append(f"总计 {len(rows)} 条 | PASS {counts['PASS']} | FAIL {counts['FAIL']}")
    lines.append("Span 契约与全部负向断言内嵌于各条断言中。")
    lines.append("差异分类说明：recall-gain=新瀑布识别出旧剥词漏掉的候选；")
    lines.append("dropped=旧剥词产出而新瀑布没有（多为误报清除或待人工确认）；")
    lines.append("role-diff=双方都有但角色判定不同（新瀑布按 L3 角色规则）。")
    lines.append(sep)
    return "\n".join(lines)


def main() -> int:
    rows = load_cases()
    report = build_report(rows)
    REPORT.write_text(report, encoding="utf-8")
    print(report.splitlines()[-4])
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条语料断言通过（报告：{REPORT.name}）")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
