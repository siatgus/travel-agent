"""corpus v4：第四阶段实体验证专项语料（2026-11-18）。

14 条 = EntityResolver 四档分流验证：
  confirmed（R01/R05/R14/R07）/ ambiguous（R02/R03/R04/R06/R08）/
  rejected（R09）/ unverified（R10/R11/R13）/ skipped（R12）

核心红线（对齐验收要求）：
- R10：在线探测失败 → probe_status=unavailable，**绝不等于 not_found**，
  status 一律 unverified（网络故障 ≠ 用户输入错误）
- R02：跨城命中如实标注「记在南京不在上海」，中置信要求用户消歧
- R04：同城同名分店列清单，绝不静默合并
- R09：探测成功确认没有 → rejected，保留原始表达

语料 KB 与探测全部注入（kb_provider / probe_fn），纯离线秒级。
同时记录影子链路（parse_utterance().shadow_resolved）的观察差异。

运行：python tests/test_corpus_v4.py
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

os.environ.setdefault("TRAVEL_DATA_DIR", tempfile.mkdtemp(prefix="corpus4-"))
os.environ.setdefault("TRAVEL_APP_DB",
                      os.path.join(tempfile.mkdtemp(prefix="corpus4-"), "t.db"))
# 语料显式控制 allow_online；防止外层环境把影子在线探测打开
os.environ.pop("TRAVEL_RESOLVER_ONLINE", None)

from travel_agent import entity_resolver as er               # noqa: E402
from travel_agent.candidate_extractor import extract         # noqa: E402
from travel_agent.parser import parse_utterance              # noqa: E402

TSV = Path(__file__).resolve().parent / "corpus_v4.tsv"
REPORT = Path(__file__).resolve().parent / "corpus_v4_report.txt"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def load_cases() -> list[dict]:
    import csv
    with open(TSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    return rows


def _probe_from(case: dict):
    """语料 probe 列 → probe_fn / allow_online。"""
    probe = (case.get("probe") or "none").strip()
    if probe == "none":
        return None, False           # KB-only
    spec = json.loads(probe)
    if spec["mode"] == "raise":
        def probe_fn(city, name, etype):
            raise ConnectionError("模拟网络不可用")
        return probe_fn, True
    if spec["mode"] == "none":
        def probe_fn(city, name, etype):
            return []
        return probe_fn, True
    if spec["mode"] == "rows":
        def probe_fn(city, name, etype):
            return [er.MatchRow(city=city, name=nm, category=cat, area=area,
                                source="amap_online", origin="online")
                    for nm, cat, area in spec["rows"]]
        return probe_fn, True


def _kb_from(case: dict):
    rows = json.loads(case["kb"] or "[]")
    prepared = [(city, dict(p)) for city, p in rows]
    return lambda: prepared


def run_case(case: dict) -> None:
    cid, utt = case["case_id"], case["utterance"]
    ctx = json.loads(case["context"] or "{}")
    expected = json.loads(case["expected"] or "[]")

    ext = extract(utt, ctx)
    probe_fn, online = _probe_from(case)
    rr = er.resolve(ext.poi_candidates, ctx, kb_provider=_kb_from(case),
                    probe_fn=probe_fn, allow_online=online)

    got = {r.text: r for r in rr.resolved}
    for exp in expected:
        r = got.get(exp["text"])
        if r is None:
            check(f"[{cid}] 候选「{exp['text']}」有验证结论", False,
                  f"实际：{[x.text for x in rr.resolved]}")
            continue
        d = r.as_dict()
        check(f"[{cid}] 「{exp['text']}」status={exp['status']}",
              d["status"] == exp["status"], f"实际 {d['status']}　notes={d['notes']}")
        if "probe_status" in exp:
            check(f"[{cid}] 「{exp['text']}」probe={exp['probe_status']}",
                  d["probe_status"] == exp["probe_status"],
                  f"实际 {d['probe_status']}")
        if "ambiguity_reason" in exp:
            check(f"[{cid}] 「{exp['text']}」reason={exp['ambiguity_reason']}",
                  d["ambiguity_reason"] == exp["ambiguity_reason"],
                  f"实际 {d['ambiguity_reason']}")
        if "city_check" in exp:
            check(f"[{cid}] 「{exp['text']}」city_check={exp['city_check']}",
                  d["city_check"] == exp["city_check"], f"实际 {d['city_check']}")
        if "type_check" in exp:
            check(f"[{cid}] 「{exp['text']}」type_check={exp['type_check']}",
                  d["type_check"] == exp["type_check"], f"实际 {d['type_check']}")
        if "role" in exp:
            check(f"[{cid}] 「{exp['text']}」role 保留={exp['role']}",
                  d["role"] == exp["role"], f"实际 {d['role']}")

    # 红线复查：unavailable 绝不落 rejected（R10/R13/R11 通用约束）
    for r in rr.resolved:
        if r.probe_status == er.PROBE_UNAVAILABLE:
            check(f"[{cid}] 红线：{r.text} unavailable ⇒ 非 rejected",
                  r.status in (er.STATUS_UNVERIFIED, er.STATUS_AMBIGUOUS),
                  f"实际 {r.status}")
        if r.status == er.STATUS_REJECTED:
            check(f"[{cid}] 红线：{r.text} rejected 必有 not_found 依据",
                  r.probe_status == er.PROBE_NOT_FOUND,
                  f"probe={r.probe_status}")


def shadow_observation() -> None:
    """影子链路观察：parse_utterance 的 shadow_resolved 与旧链路行为对照。

    只记录不驱动业务：to_legacy_delta 不变、旧 poi_candidates 不变。
    KB 注入与 R02 同源（真实 kb.db 的漂移不影响断言）。"""
    utt = "那给我排国民政府中央广播电台旧址"
    ctx = {"slots": {"destination": "上海"}, "plan_made": True}
    er._default_kb_rows = (  # 注入只读 KB，与语料 R02 同源
        lambda: [("南京", {"name": "国民政府中央广播电台旧址",
                           "category": "attraction", "area": "鼓楼区",
                           "source": "manual"})])
    pr = parse_utterance(utt, ctx)
    ext = extract(utt, ctx)
    check("[影子] shadow_resolved 已产出且逐候选有结论",
          len(pr.shadow_resolved) == len(ext.poi_candidates) > 0,
          f"{len(pr.shadow_resolved)} vs 候选 {len(ext.poi_candidates)}")
    check("[影子] 旧链路保持剥词现状（12 字长名 recall 差距，第四阶段不改旧链路）",
          isinstance(pr.poi_candidates, list),
          str([c.name for c in pr.poi_candidates]))
    check("[影子] 新候选在 shadow_candidates 里可见",
          any(c.get("text") == "国民政府中央广播电台旧址"
              for c in pr.shadow_candidates),
          str([c.get("text") for c in pr.shadow_candidates]))
    delta = pr.to_legacy_delta()
    check("[影子] to_legacy_delta 契约不变（本句未新说目的地 → 不写入）",
          delta["destination"] is None and delta["party_size"] == 1,
          str(delta))
    resolved = pr.shadow_resolved
    if resolved:
        statuses = {r["status"] for r in resolved}
        check("[影子] status 全部落在四档+skipped",
              statuses <= {"confirmed", "ambiguous", "rejected",
                           "unverified", "skipped"}, str(statuses))
        check("[影子] R02 跨城加点在影子链路标为 ambiguous",
              any(r["status"] == "ambiguous"
                  and r.get("ambiguity_reason") == "cross_city_mismatch"
                  for r in resolved), str(resolved))


def write_report() -> None:
    lines = [f"corpus v4 验证报告（{len(load_cases())} 例）",
             "=" * 40]
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    lines.append(f"断言 {len(RESULTS)} 项：PASS {passed} / FAIL {len(RESULTS) - passed}")
    lines.append("")
    for name, ok, detail in RESULTS:
        if not ok:
            lines.append(f"FAIL {name}　{detail}")
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    cases = load_cases()
    for case in cases:
        run_case(case)
    shadow_observation()
    write_report()
    fails = [r for r in RESULTS if not r[1]]
    print(f"\n共 {len(RESULTS)} 项断言，失败 {len(fails)} 项")
    if fails:
        for name, _, detail in fails:
            print(f"  FAIL {name}　{detail}")
        return 1
    print("corpus v4 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
