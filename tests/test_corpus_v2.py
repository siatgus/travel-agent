# -*- coding: utf-8 -*-
"""第二阶段：语料库 v2 执行入口（Phase 2，2026-11-17）。

与 v1（tests/test_corpus.py）的差异：
1. 语料文件为 corpus_v2.tsv（v1 冻结不覆盖，见 CORPUS_CHANGELOG.md）；
2. Schema 字段来自统一解析层 parse_utterance（新 Schema 是唯一事实源）：
   destination / query_city / departure_city / people(None 语义) /
   候选 Span / confidence 均为真实字段；
3. 新增方案差量断言执行器：
   - plan_delta_no_new=<名字>：本轮不得新增排入该名字的节点；
   - plan_structure_keep：问句前后方案结构（名称/类型/所在天/顺序）不变；
4. 报告输出 v1→v2 的新旧结果差异。

用法：
    python tests/test_corpus_v2.py                # 跑全量，打印并保存报告
    python tests/test_corpus_v2.py --run-one C05  # 子进程模式（内部使用）
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
CORPUS = os.path.join(HERE, "corpus_v2.tsv")
REPORT = os.path.join(HERE, "corpus_v2_report.txt")
V1_REPORT = os.path.join(HERE, "corpus_report.txt")
FIXED_TODAY = date(2026, 11, 17)
CASE_TIMEOUT = 300


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


def plan_signature(plan: dict | None):
    """方案结构签名：天号 + 每天节点（名称, 类型）的顺序序列。

    plan_structure_keep 用它做差量对比——餐位、日期、餐次（节点类型）
    和顺序任何一处被意外修改都会导致签名不一致。
    """
    if not plan:
        return None
    sig = []
    for d in plan.get("days_plan") or []:
        sig.append((d.get("day"),
                    tuple((n.get("name") or "", n.get("type") or "")
                          for n in d.get("nodes") or [])))
    return tuple(sig)


# ---------------------------------------------------------------- 子进程模式

def run_one(case):
    """子进程内运行单条病例：先设隔离环境，再导入业务模块。"""
    data_dir = tempfile.mkdtemp(prefix="corpus2-data-")
    db_dir = tempfile.mkdtemp(prefix="corpus2-db-")
    os.environ["TRAVEL_DATA_DIR"] = data_dir
    os.environ["TRAVEL_APP_DB"] = os.path.join(db_dir, "t.db")

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
    from travel_agent import kb, parser as parser_mod
    from travel_agent.dialogue import Session

    out = {"case_id": case["case_id"], "error": None}
    try:
        s = Session(today=FIXED_TODAY)
        for t in case["context_turns"]:
            s.turn(t)
        dest_after_context = (s.slots or {}).get("destination")
        prev_plan = s.last_plan          # 问句前的方案（差量基线）

        # 统一解析（新 Schema 唯一事实源）：在消费本轮句子前解析，
        # 上下文状态 = 问句前的会话状态
        utt = case["utterance"]
        parsed = parser_mod.parse_utterance(utt, context=s, today=FIXED_TODAY)

        reply = s.turn(utt)
        post_plan = reply.plan if getattr(reply, "plan", None) else s.last_plan
        plan = post_plan
        text = (getattr(reply, "render", "") or getattr(reply, "question", "")
                or getattr(reply, "text", "") or "")

        out["actual"] = {
            "destination": parsed.destination,
            "destination_new": parsed.destination_new,
            "destination_writable": parsed.destination_writable,
            "session_destination": (s.slots or {}).get("destination"),
            "departure_city": parsed.departure_city,
            "query_city": parsed.query_city,
            "poi_candidates": [c.as_dict() for c in parsed.poi_candidates],
            "poi_category": parsed.poi_category,
            "date": parsed.date,
            "people": parsed.people,
            "intent_coarse": parsed.intent_coarse,
            "intent": parsed.intent,
            "confidence": parsed.confidence,
            "legacy_destination": parsed.to_legacy_delta()["destination"],
            "reply_mode": getattr(reply, "mode", ""),
            "reply_snippet": text[:120].replace("\n", " "),
            "plan_created": plan is not None,
            "dest_after_context": dest_after_context,
            "notes": list(parsed.notes),
        }

        # 负向断言逐条执行
        negs = []
        prev_names = set()
        for d in (prev_plan or {}).get("days_plan") or []:
            for n in d.get("nodes") or []:
                prev_names.add(n.get("name") or "")
        post_names = set()
        for d in (post_plan or {}).get("days_plan") or []:
            for n in d.get("nodes") or []:
                post_names.add(n.get("name") or "")
        new_names = post_names - prev_names
        all_pois = None
        try:
            k = kb.get_kb()
            all_pois = []
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
                ok = parsed.destination != v
                note = f"实际 destination={parsed.destination!r}"
            elif expr == "destination_keep":
                ok = (s.slots or {}).get("destination") == dest_after_context
                note = f"上下文后={dest_after_context!r}，最终={(s.slots or {}).get('destination')!r}"
            elif expr == "destination_null":
                ok = parsed.destination is None
                note = f"实际 destination={parsed.destination!r}"
            elif expr.startswith("kb_no_poi="):
                v = expr.split("=", 1)[1]
                if all_pois is None:
                    ok, note = None, "KB 不可读"
                else:
                    hit = [f"{c}:{n}" for c, n in all_pois if v in n]
                    ok, note = not hit, f"命中：{hit}" if hit else "KB 无污染"
            elif expr.startswith("plan_not_contains="):
                v = expr.split("=", 1)[1]
                hit = [n for n in post_names if v in n]
                ok, note = not hit, f"方案含：{hit}" if hit else "方案不含该点"
            elif expr.startswith("plan_delta_no_new="):
                v = expr.split("=", 1)[1]
                hit = [n for n in new_names if v in n]
                ok, note = (not hit,
                            (f"本轮新增排入：{hit}" if hit else
                             f"原有 {len(prev_names)} 个节点，本轮新增 {len(new_names)} 个"
                             + (f"（{sorted(new_names)}）" if new_names else "，差量为零")))
            elif expr == "plan_structure_keep":
                if prev_plan is None:
                    ok, note = None, "问句前无方案（无差量基线）"
                else:
                    ok = plan_signature(prev_plan) == plan_signature(post_plan)
                    if ok:
                        note = (f"方案结构保持：{len(prev_names)} 个节点、"
                                f"{len((prev_plan or {}).get('days_plan') or [])} 天，"
                                "名称/类型/天序/天内顺序均未变")
                    else:
                        note = "方案结构被修改！"
            elif expr.startswith("no_repeated_question="):
                v = expr.split("=", 1)[1]
                ok = v not in text
                note = f"回复含「{v}」" if not ok else "未复读追问"
            elif expr.startswith("candidate_not="):
                v = expr.split("=", 1)[1]
                hit = [c["name"] for c in out["actual"]["poi_candidates"]
                       if v in c["name"]]
                ok, note = not hit, f"候选含：{hit}" if hit else "候选无污染"
            elif expr.startswith("poi_category_not="):
                v = expr.split("=", 1)[1]
                ok = parsed.poi_category != v
                note = f"实际品类={parsed.poi_category!r}"
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

def _fuzzy(want, got):
    return want in got or got in want


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
         actual["destination"],
         note=f"会话槽位={actual['session_destination']!r}")
    mark("departure_city", actual["departure_city"] == exp["departure_city"],
         actual["departure_city"])
    # query_city：期望值依赖实体解析（句中无该城市、上下文也没有）时，
    # 本阶段如实记缺口——那是第三/四阶段识别瀑布与校验闭环的工作
    eq = actual["query_city"] == exp["query_city"]
    resolution_pending = (exp["query_city"] and not eq
                          and exp["query_city"] not in case["utterance"]
                          and exp["query_city"] != actual["dest_after_context"])
    mark("query_city", eq, actual["query_city"], gap=resolution_pending,
         note="需实体解析（POI→城市），第三/四阶段实现" if resolution_pending else "")
    if exp["poi_candidates"]:
        want = [c["name"] for c in exp["poi_candidates"]]
        got = [c["name"] for c in actual["poi_candidates"]]
        hit = any(any(_fuzzy(w, g) for g in got) for w in want)
        mark("poi_candidates", hit, got, gap=not got,
             note="候选抽取为部分映射，待第三阶段识别瀑布" if not hit else "")
    else:
        # 反向检查：期望无候选时，多出的候选是剥词污染（REGRESSION）
        extras = [c["name"] for c in actual["poi_candidates"]]
        mark("poi_candidates", not extras, extras,
             note="期望无候选，实际多出（剥词污染）" if extras else "")
    mark("poi_category", actual["poi_category"] == exp["poi_category"],
         actual["poi_category"])
    if exp["date"]:
        mark("date", (actual["date"] or {}).get("start") == exp["date"]["start"],
             actual["date"])
    else:
        mark("date", actual["date"] is None, actual["date"])
    mark("people", actual["people"] == exp["people"], actual["people"])
    mark("intent", actual["intent"] == exp["intent"],
         f"{actual['intent']}/{actual['intent_coarse']}")
    conf = actual["confidence"]
    mark("confidence", conf is not None
         and abs(conf - exp["confidence"]) <= 0.15 + 1e-9,
         conf, note="容差 ±0.15")

    # 写入权限表行为断言（不占状态，仅记录；违规记 FAIL）
    if not actual["destination_writable"] and actual["query_city"] \
            and actual["legacy_destination"] == actual["query_city"]:
        mark("write_permission", False, actual["legacy_destination"],
             note="查询城市经兼容层泄漏进 destination！")
    else:
        mark("write_permission", True,
             actual["legacy_destination"], note="兼容层未泄漏查询城市")
    return fields, ("REGRESSION" if has_regression else
                    ("SCHEMA-GAP" if has_gap else "PASS"))


def load_v1_statuses() -> dict:
    """从冻结的 v1 报告里读出每条病例的旧状态，供新旧差异对比。"""
    out = {}
    try:
        with open(V1_REPORT, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^\[(C\d+)\].*→\s*([A-Z-]+)\s*$", line.strip())
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def run_all():
    cases = load_corpus()
    v1 = load_v1_statuses()
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
    lines.append("语料库 v2 报告（第二阶段：统一解析 Schema + 三城市字段拆分）")
    lines.append(f"基线 commit：842170b（v1 冻结）    固定 today：{FIXED_TODAY}")
    lines.append("Schema 字段来自 parse_utterance（新 Schema 唯一事实源）")
    lines.append("=" * 72)
    diff_rows = []
    for r in results:
        case = r["case"]
        cid = case["case_id"]
        if "error" in r:
            counts["ERROR"] += 1
            lines.append(f"\n[{cid}] {case['utterance']}  →  ERROR")
            lines.append(f"    {r['error']}")
            diff_rows.append((cid, v1.get(cid, "?"), "ERROR"))
            continue
        actual = r["out"].get("actual")
        if actual is None:
            counts["ERROR"] += 1
            lines.append(f"\n[{cid}] {case['utterance']}  →  ERROR")
            lines.append(f"    {r['out'].get('error')}  {r['out'].get('trace', '')[:200]}")
            diff_rows.append((cid, v1.get(cid, "?"), "ERROR"))
            continue
        fields, status = compare(case, actual)
        neg_fail = [n for n in r["out"].get("negatives", []) if n["ok"] is False]
        neg_unsup = [n for n in r["out"].get("negatives", []) if n["ok"] is None]
        if neg_fail:
            status = "MIXED" if status == "SCHEMA-GAP" else "REGRESSION"
        counts[status] = counts.get(status, 0) + 1
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
        diff_rows.append((cid, v1.get(cid, "?"), status))

    lines.append("\n" + "=" * 72)
    lines.append(f"总计 {len(results)} 条 | PASS {counts['PASS']} | "
                 f"SCHEMA-GAP {counts['SCHEMA-GAP']}（待三/四阶段）"
                 f" | REGRESSION {counts['REGRESSION']} | MIXED {counts['MIXED']}"
                 f" | ERROR {counts['ERROR']}")
    lines.append("-" * 72)
    lines.append("新旧结果差异（v1 冻结基线 → v2 本阶段）：")
    for cid, old, new in diff_rows:
        flag = "（不变）" if old == new else ("  ← 改善" if old != new and new in ("PASS", "SCHEMA-GAP") and old in ("REGRESSION", "MIXED", "ERROR") else "  ← 变化")
        lines.append(f"    {cid}: {old} → {new}{flag}")
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
