# -*- coding: utf-8 -*-
"""第一阶段：回归语料库执行入口（Phase 1，2026-11-17）。

约束（来自方案评审，见 docs 与对话记录）：
- 本文件与 tests/corpus.tsv 是本阶段唯一新增物；禁止修改任何业务文件。
- 每条病例在独立子进程 + 全新临时数据目录中运行，互不污染、不打线上 KB。
- 现有解析结果映射到新 Schema 时，无法映射的字段如实记 None 并注明原因，
  不猜测、不为让测试通过而修改期望结果。
- 允许病例失败：失败是第二~五阶段的基线，失败分类见报告
  （REGRESSION=行为与预期矛盾；SCHEMA-GAP=字段尚不存在，属预期缺口）。

用法：
    python tests/test_corpus.py                # 跑全量，打印并保存报告
    python tests/test_corpus.py --run-one C01  # 子进程模式（内部使用）
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus.tsv")
REPORT = os.path.join(HERE, "corpus_report.txt")
FIXED_TODAY = date(2026, 11, 17)
CASE_TIMEOUT = 300          # 单病例秒数（含离线兜底的联网超时）

FOOD_SUFFIX_RE = re.compile(
    r"餐厅|餐馆|饭店|菜馆|火锅|烧烤|小吃|面馆|大排档|火烧|粉|汤|菜|鱼|鸭|店$")
HOTEL_SUFFIX_RE = re.compile(r"酒店|宾馆|民宿|客栈|饭店$")


def load_corpus():
    cases = []
    with open(CORPUS, encoding="utf-8-sig") as f:
        header = None
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            if header is None:
                header = parts
                continue
            row = dict(zip(header, parts))
            row["expected_schema"] = json.loads(row["expected_schema"])
            row["negative_assertions"] = json.loads(row["negative_assertions"])
            row["context_turns"] = [t for t in (row.get("context") or "").split("||") if t]
            cases.append(row)
    return cases


# ---------------------------------------------------------------- 子进程模式

def guess_entity_type(name: str) -> str:
    if HOTEL_SUFFIX_RE.search(name or ""):
        return "hotel"
    if FOOD_SUFFIX_RE.search(name or ""):
        return "restaurant"
    return "attraction"


def run_one(case):
    """子进程内运行单条病例：先设隔离环境，再导入业务模块。"""
    data_dir = tempfile.mkdtemp(prefix="corpus-data-")
    db_dir = tempfile.mkdtemp(prefix="corpus-db-")
    os.environ["TRAVEL_DATA_DIR"] = data_dir
    os.environ["TRAVEL_APP_DB"] = os.path.join(db_dir, "t.db")

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
    from travel_agent import agent, intent, kb
    from travel_agent.dialogue import Session

    out = {"case_id": case["case_id"], "error": None}
    try:
        s = Session(today=FIXED_TODAY)
        for t in case["context_turns"]:
            s.turn(t)
        dest_after_context = (s.slots or {}).get("destination")
        reply = s.turn(case["utterance"])
        utt = case["utterance"]
        dest = (s.slots or {}).get("destination")
        origin = (s.slots or {}).get("origin")

        # 候选抽取：忠实映射现有产出（must_visit / spot_mention / food_keyword）
        d = agent.extract_slots(utt, today=FIXED_TODAY)
        cands = []
        for m in (d.get("must_visit") or []):
            cands.append({"name": m, "entity_type": guess_entity_type(m),
                          "city": None, "source": "must_visit"})
        sm = intent.spot_mention(utt, dest)
        if sm and not any(c["name"] == sm for c in cands):
            cands.append({"name": sm, "entity_type": guess_entity_type(sm),
                          "city": None, "source": "spot_mention"})
        # 品类：美食关键词 / 品类词，仅记录，不猜
        fk = (intent.food_keyword(utt, dest)
              if intent.is_food_question(utt) else None)
        cat = intent.poi_category(utt)
        poi_category = fk or (cat[0] if cat else None)

        cls = intent.classify(utt)
        if cls == "poi":
            cls_detail = intent.refine_intent(utt, dest)
        else:
            cls_detail = cls
        # 意图粗分类：classify 是句子层判定；裸数字/上下文补槽（「1」）
        # classify 看不出来，要结合本轮回复是否进入了规划/澄清动作。
        # 注意不能拿"会话里存在方案"当依据——查询轮也会带着历史方案。
        mode = getattr(reply, "mode", "")
        if cls == "plan" or mode in ("plan", "clarify"):
            coarse = "plan"
        elif cls in ("poi", "food", "weather", "transport", "plan_qa"):
            coarse = "query"
        else:
            coarse = "other"
        plan = reply.plan if getattr(reply, "plan", None) else s.last_plan
        text = (getattr(reply, "render", "") or getattr(reply, "question", "")
                or getattr(reply, "text", "") or "")

        # 现有代码没有 query_city / confidence 字段：如实记 None
        out["actual"] = {
            "destination": dest,
            "departure_city": origin,
            "query_city": None,
            "query_city_note": "现解析层无 query_city 字段（destination 被"
                               "查询/规划意图共用）——SCHEMA-GAP",
            "conflation_observed": dest if cls in ("poi", "food", "weather", "other") else None,
            "poi_candidates": cands,
            "poi_category": poi_category,
            "date": (s.slots or {}).get("date_range"),
            "people": (s.slots or {}).get("party_size"),
            "intent_coarse": coarse,
            "intent_detail": cls_detail,
            "confidence": None,
            "confidence_note": "现解析层无置信度字段——SCHEMA-GAP",
            "reply_mode": getattr(reply, "mode", ""),
            "reply_snippet": text[:120].replace("\n", " "),
            "plan_created": plan is not None,
            "dest_after_context": dest_after_context,
        }

        # 负向断言逐条执行
        negs = []
        plan_nodes = []
        if plan:
            plan_nodes = [n.get("name") or "" for d2 in plan.get("days_plan", [])
                          for n in d2.get("nodes", [])]
        all_pois = []
        try:
            k = kb.get_kb()
            for c in (k.cities() or []):
                for p in (k.list_pois(c) or []):
                    all_pois.append((c, p.get("name") or ""))
        except Exception as e:
            all_pois = None
            out["kb_error"] = str(e)
        for expr in case["negative_assertions"]:
            ok, note = True, ""
            if expr.startswith("destination_not="):
                v = expr.split("=", 1)[1]
                ok = dest != v
                note = f"实际 destination={dest!r}"
            elif expr == "destination_keep":
                ok = dest == dest_after_context
                note = f"上下文后={dest_after_context!r}，最终={dest!r}"
            elif expr == "destination_null":
                ok = dest is None
                note = f"实际 destination={dest!r}"
            elif expr.startswith("kb_no_poi="):
                v = expr.split("=", 1)[1]
                if all_pois is None:
                    ok, note = None, "KB 不可读"
                else:
                    hit = [f"{c}:{n}" for c, n in all_pois if v in n]
                    ok, note = not hit, f"命中：{hit}" if hit else "KB 无污染"
            elif expr.startswith("plan_not_contains="):
                v = expr.split("=", 1)[1]
                hit = [n for n in plan_nodes if v in n]
                ok, note = not hit, f"方案含：{hit}" if hit else "方案不含该点"
            elif expr.startswith("no_repeated_question="):
                v = expr.split("=", 1)[1]
                ok = v not in text
                note = f"回复含「{v}」" if not ok else "未复读追问"
            elif expr.startswith("candidate_not="):
                v = expr.split("=", 1)[1]
                hit = [c["name"] for c in cands if v in c["name"]]
                ok, note = not hit, f"候选含：{hit}" if hit else "候选无污染"
            elif expr.startswith("poi_category_not="):
                v = expr.split("=", 1)[1]
                ok = poi_category != v
                note = f"实际品类={poi_category!r}"
            elif expr.startswith("kb_no_write_city="):
                city, _, rest = expr.partition(",")
                v = city.split("=", 1)[1]
                want = rest.split("name_contains=", 1)[1]
                if all_pois is None:
                    ok, note = None, "KB 不可读"
                else:
                    hit = [n for c2, n in all_pois if c2 == v and want in n]
                    ok, note = not hit, f"{v} 库含：{hit}" if hit else f"{v} 库无污染"
            else:
                ok, note = None, "不支持的断言表达式"
            negs.append({"expr": expr, "ok": ok, "note": note})
        out["negatives"] = negs
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()[-500:]
    print("###RESULT###" + json.dumps(out, ensure_ascii=False))


# ---------------------------------------------------------------- 主进程模式

def compare(case, actual):
    """逐字段对比，返回 (field_results, status)。"""
    exp = case["expected_schema"]
    fields = []
    has_regression = has_gap = False

    def mark(name, ok, actual_v, gap=False, note=""):
        nonlocal has_regression, has_gap
        state = "PASS" if ok else ("SCHEMA-GAP" if gap else "FAIL")
        if state == "FAIL":
            has_regression = True
        if state == "SCHEMA-GAP":
            has_gap = True
        fields.append({"field": name, "state": state,
                       "expected": exp.get(name), "actual": actual_v,
                       "note": note})

    mark("destination", actual["destination"] == exp["destination"],
         actual["destination"])
    mark("departure_city", actual["departure_city"] == exp["departure_city"],
         actual["departure_city"])
    mark("query_city", False, actual["query_city"], gap=True,
         note=actual["query_city_note"])
    if exp["poi_candidates"]:
        want = [c["name"] for c in exp["poi_candidates"]]
        got = [c["name"] for c in actual["poi_candidates"]]
        hit = any(any(_fuzzy(w, g) for g in got) for w in want)
        mark("poi_candidates", hit, got,
             gap=not got, note="候选抽取为部分映射" if not hit else "")
    else:
        mark("poi_candidates", True, [], gap=False)
    mark("poi_category", actual["poi_category"] == exp["poi_category"],
         actual["poi_category"], gap=actual["poi_category"] is None
         and exp["poi_category"] is None)
    if exp["date"]:
        mark("date", (actual["date"] or {}).get("start") == exp["date"]["start"],
             actual["date"])
    else:
        mark("date", True, actual["date"])
    mark("people", actual["people"] == exp["people"], actual["people"])
    coarse_want = {"plan_create": "plan", "plan_modify": "plan",
                   "slot_fill_plan_create": "plan"}.get(exp["intent"], "query")
    mark("intent", actual["intent_coarse"] == coarse_want,
         f"{actual['intent_coarse']}/{actual['intent_detail']}")
    mark("confidence", False, actual["confidence"], gap=True,
         note=actual["confidence_note"])
    return fields, ("REGRESSION" if has_regression else
                    ("SCHEMA-GAP" if has_gap else "PASS"))


def _fuzzy(want, got):
    return want in got or got in want


def run_all():
    cases = load_corpus()
    results = []
    for case in cases:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            p = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--run-one", case["case_id"]],
                capture_output=True, text=True, encoding="utf-8",
                timeout=CASE_TIMEOUT, env=env, cwd=HERE)
            line = next((l for l in p.stdout.splitlines()
                         if l.startswith("###RESULT###")), None)
            if line is None:
                results.append({"case": case, "error":
                                f"子进程无结果：{p.stdout[-200:]} {p.stderr[-300:]}"})
                continue
            out = json.loads(line[len("###RESULT###"):])
            results.append({"case": case, "out": out})
        except subprocess.TimeoutExpired:
            results.append({"case": case, "error": f"超时（>{CASE_TIMEOUT}s）"})

    lines = []
    counts = {"PASS": 0, "SCHEMA-GAP": 0, "REGRESSION": 0, "MIXED": 0,
              "ERROR": 0}
    lines.append("=" * 72)
    lines.append("语料库第一阶段基线报告（只读运行，未修改任何业务代码）")
    lines.append(f"基线 commit：7364d40    固定 today：{FIXED_TODAY}")
    lines.append("=" * 72)
    for r in results:
        case = r["case"]
        cid = case["case_id"]
        if "error" in r:
            counts["ERROR"] += 1
            lines.append(f"\n[{cid}] {case['utterance']}  →  ERROR")
            lines.append(f"    {r['error']}")
            continue
        actual = r["out"].get("actual")
        if actual is None:
            counts["ERROR"] += 1
            lines.append(f"\n[{cid}] {case['utterance']}  →  ERROR")
            lines.append(f"    {r['out'].get('error')}  {r['out'].get('trace', '')[:200]}")
            continue
        fields, status = compare(case, actual)
        counts[status] += 1
        neg_fail = [n for n in r["out"].get("negatives", []) if n["ok"] is False]
        neg_unsup = [n for n in r["out"].get("negatives", []) if n["ok"] is None]
        if neg_fail:
            status = "MIXED" if status == "SCHEMA-GAP" else "REGRESSION"
        lines.append(f"\n[{cid}] {case['category']}  「{case['utterance']}」"
                     f"  →  {status}")
        lines.append(f"    回复：mode={actual['reply_mode']}  "
                     f"plan={actual['plan_created']}  "
                     f"「{actual['reply_snippet']}」")
        for f in fields:
            if f["state"] != "PASS":
                lines.append(f"    {f['field']}: {f['state']}  "
                             f"期望={f['expected']}  实际={f['actual']}"
                             + (f"  ({f['note']})" if f["note"] else ""))
        for n in r["out"].get("negatives", []):
            tag = {True: "ok", False: "FAIL", None: "N/A"}[n["ok"]]
            lines.append(f"    负向[{tag}] {n['expr']}  {n['note']}")
        if neg_unsup:
            lines.append(f"    注意：{len(neg_unsup)} 条负向断言暂无执行器支持")

    lines.append("\n" + "=" * 72)
    lines.append(f"总计 {len(results)} 条 | PASS {counts['PASS']} | "
                 f"SCHEMA-GAP {counts['SCHEMA-GAP']}（预期缺口，二~五阶段消除）"
                 f" | REGRESSION {counts['REGRESSION']} | MIXED {counts['MIXED']}"
                 f" | ERROR {counts['ERROR']}")
    lines.append("=" * 72)
    report = "\n".join(lines)
    print(report)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(f"\n报告已保存：{REPORT}")


if __name__ == "__main__":
    if "--run-one" in sys.argv:
        cid = sys.argv[sys.argv.index("--run-one") + 1]
        case = next(c for c in load_corpus() if c["case_id"] == cid)
        run_one(case)
    else:
        run_all()
