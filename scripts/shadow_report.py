#!/usr/bin/env python
"""P2.6 批次 5.0 第二阶段 · 离线影子报告（shadow_report）。

职责
----
只读 shadow_collect 产出的 JSONL，输出分布统计。**不做新旧差异三分类**
（那需要同时记录旧链决策，会触碰 src/，评审 2026-11-20 已裁定本轮不做）。

授权边界
--------
允许：本脚本、shadow_collect.py、tests/observ_corpus.tsv、
      tests/test_shadow_offline.py、文档 §26 实施记录。
禁止：修改 src/、生产 JSONL、Web API、R-01 容量策略、P3 / 灰度。

两条硬约束（评审 2026-11-20）
---------------------------
1. 每条记录必须自带 category（采集时由语料继承），**不得用 intent 反推**
   语料分类。报告分别展示 category 与 intent 两个维度。
2. 必须区分「零记录」与「采集失败」：
   - 零记录 = 语料里的 case 在 JSONL 中没有对应行（采集脚本成功但该轮无记录）；
   - 采集失败 = 采集脚本非 0 退出、没有产出 JSONL（本轮报告不负责兜底）。
   给出 --corpus 时，报告会做 case 对账并列出缺失 case_id。

失败判据（复审 R1 补充，评审 2026-11-20）
----------------------------------------
- **重复 case_id**：`load_jsonl` 直接抛 `ContractError`（退出码 4）；
  即便绕过它（例如直接传内存列表），`build_report` 的 `no_duplicate_cases`
  校验也会判失败（退出码 5）。
- **缺失 case**：给出 `--corpus` 时，`no_missing_cases` 校验失败 → 退出码 5。
  即「零记录」是**失败**，而不是仅作提示；报告仍会打印缺失清单供定位。

外部 schema 独立性（复审 R2，评审 2026-11-20）
---------------------------------------------
本脚本**独立维护** 12 字段白名单 `REPORT_EXPORT_KEYS`，不再 import 采集侧的
`EXPORT_KEYS`。两侧内容目前一致，但**不是同一个对象**：采集侧增删字段时，报告
会在 `load_jsonl` 处以契约错误（退出码 4）暴露出来，而不是「自动接受对方的
schema 变化」——这正是独立校验的意义。`ALLOWED_CATEGORIES` 仍从采集侧导入，
因为那是语料分类词表（同一份语义），如需独立亦请单独授权。

用法
----
    python scripts/shadow_report.py --jsonl /path/run.jsonl [--corpus tests/observ_corpus.tsv]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shadow_collect import (  # noqa: E402
    ALLOWED_CATEGORIES, ContractError, CorpusError, load_corpus,
)

# 外部 12 字段白名单：**本脚本独立硬编码**（复审 R2）。
# 刻意不 import 采集侧的 EXPORT_KEYS：若两侧共享同一对象，采集侧改字段时报告会
# 静默跟随，独立校验就失去意义。两侧漂移必须在报告侧表现为契约错误。
REPORT_EXPORT_KEYS = (
    "case_id", "category", "intent", "takeover", "batch_enabled",
    "action", "reason", "statuses", "city_source", "entity_city",
    "confidence", "candidates_count",
)

NONE_LABEL = "<none>"


def load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise ContractError(f"JSONL 不存在：{path}")
    rows: list[dict] = []
    seen: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContractError(f"第 {lineno} 行不是合法 JSON：{exc}") from exc
            if not isinstance(row, dict):
                raise ContractError(f"第 {lineno} 行不是 JSON 对象")
            if set(row) != set(REPORT_EXPORT_KEYS):
                missing = sorted(set(REPORT_EXPORT_KEYS) - set(row))
                extra = sorted(set(row) - set(REPORT_EXPORT_KEYS))
                raise ContractError(
                    f"第 {lineno} 行字段不符；缺失={missing}；多余={extra}")
            if row.get("category") not in ALLOWED_CATEGORIES:
                raise ContractError(
                    f"第 {lineno} 行 category 非法：{row.get('category')!r}")
            cid = row.get("case_id")
            if not isinstance(cid, str) or not cid:
                raise ContractError(f"第 {lineno} 行 case_id 非法：{cid!r}")
            if cid in seen:
                # 重复 ID 会让每条 case 的「恰一条记录」契约失效，属数据契约错误
                raise ContractError(
                    f"第 {lineno} 行 case_id 重复：{cid}（首次出现在第 {seen[cid]} 行）")
            seen[cid] = lineno
            rows.append(row)
    return rows


def _status_key(statuses) -> str:
    if not statuses:
        return "<empty>"
    return "+".join(str(s) for s in statuses)


def _tally(keys) -> dict[str, int]:
    out: dict[str, int] = {}
    for k in keys:
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_report(records: list[dict],
                 corpus_cases: list[str] | None = None) -> dict:
    total = len(records)
    by_category = _tally(r["category"] for r in records)
    by_action = _tally(NONE_LABEL if r["action"] is None else str(r["action"])
                       for r in records)
    by_intent = _tally(str(r["intent"]) for r in records)
    by_reason = _tally(str(r["reason"]) for r in records)
    by_city_source = _tally(NONE_LABEL if r["city_source"] is None
                            else str(r["city_source"]) for r in records)
    by_statuses = _tally(_status_key(r["statuses"]) for r in records)
    by_candidates = _tally(str(r["candidates_count"]) for r in records)

    # 交叉表：category → action（保留零出现组合的完整性由守恒校验兜）
    cross: dict[str, dict[str, int]] = {}
    for r in records:
        cat = r["category"]
        act = NONE_LABEL if r["action"] is None else str(r["action"])
        cross.setdefault(cat, {})
        cross[cat][act] = cross[cat].get(act, 0) + 1
    for cat in cross:
        cross[cat] = dict(sorted(cross[cat].items(), key=lambda kv: (-kv[1], kv[0])))

    # case 对账（区分零记录与采集失败）
    present = {r["case_id"] for r in records}
    missing: list[str] = []
    extra: list[str] = []
    if corpus_cases is not None:
        missing = [c for c in corpus_cases if c not in present]
        extra = sorted(present - set(corpus_cases))

    # 重复 case_id：即使绕过 load_jsonl（例如直接传内存列表）也必须被判定为失败
    counts: dict[str, int] = {}
    for r in records:
        counts[r["case_id"]] = counts.get(r["case_id"], 0) + 1
    duplicates = sorted(cid for cid, n in counts.items() if n > 1)

    # 曾有一条 `each_record_single_category == (len(records) == total)`，
    # 两边是同一个表达式，恒真、不提供任何保证；复审 R2 已删除，不再作为验收证据。
    checks = {
        "total_equals_sum_by_category": sum(by_category.values()) == total,
        "total_equals_sum_by_action": sum(by_action.values()) == total,
        "total_equals_sum_by_candidates": sum(by_candidates.values()) == total,
        "total_equals_sum_by_statuses": sum(by_statuses.values()) == total,
        "cross_table_conserved": all(
            sum(cross[c].values()) == by_category.get(c, 0) for c in cross),
        "no_duplicate_cases": not duplicates,
    }
    if corpus_cases is not None:
        checks["no_extra_cases"] = not extra
        checks["no_missing_cases"] = not missing
        checks["missing_plus_present_equals_corpus"] = (
            len(missing) + len(present) if not extra else False) == len(corpus_cases)

    return {
        "total": total,
        "by_category": by_category,
        "by_action": by_action,
        "by_intent": by_intent,
        "by_reason": by_reason,
        "by_city_source": by_city_source,
        "by_statuses": by_statuses,
        "by_candidates_count": by_candidates,
        "cross_category_action": cross,
        "corpus_cases": None if corpus_cases is None else len(corpus_cases),
        "missing_cases": missing,
        "extra_cases": extra,
        "duplicate_cases": duplicates,
        "checks": checks,
    }


def _pct(n: int, total: int) -> str:
    return f"{n / total * 100:5.1f}%" if total else "  n/a"


def _section(title: str, dist: dict[str, int], total: int, width: int = 20) -> list[str]:
    lines = [f"  {title}"]
    for k, v in dist.items():
        lines.append(f"    {k[:width]:<{width}} {v:4d}  {_pct(v, total)}")
    return lines


def render(rep: dict, jsonl_path: str, corpus_path: str | None) -> str:
    total = rep["total"]
    out: list[str] = []
    out.append("=" * 68)
    out.append("P2.6 影子观察报告（离线 · 仅分布，不做新旧差异三分类）")
    out.append("=" * 68)
    out.append("声明：本报告基于**人工构造的观察语料**，不是生产流量样本，")
    out.append("      不是权威分类基准，不构成真实用户行为分布证据。")
    out.append("      category 是语料作者指定的观察分类；intent 是决策记录中的")
    out.append("      细分意图，两者不是同一概念，分别统计、互不推导。")
    out.append("")
    out.append(f"JSONL：{jsonl_path}")
    if corpus_path:
        out.append(f"语料：{corpus_path}")
    out.append(f"记录总数：{total}")
    if rep["duplicate_cases"]:
        out.append(f"[错误] case_id 重复（{len(rep['duplicate_cases'])} 个）："
                   f"{', '.join(rep['duplicate_cases'])}"
                   f" —— 违反「每个 case 恰一条记录」，本次判为失败")
    else:
        out.append("重复 case_id：无")
    if rep["corpus_cases"] is not None:
        out.append(f"语料 case 数：{rep['corpus_cases']}")
        if rep["missing_cases"]:
            out.append(f"零记录 case（{len(rep['missing_cases'])} 个，"
                       f"采集脚本成功但未产出记录，本次判为失败）："
                       f"{', '.join(rep['missing_cases'])}")
        else:
            out.append("零记录 case：无")
        if rep["extra_cases"]:
            out.append(f"[错误] JSONL 中出现了语料之外的 case：{rep['extra_cases']}")
    out.append("")
    out.append("【总体分布】")
    out.extend(_section("action", rep["by_action"], total))
    out.extend(_section("intent（决策记录细分）", rep["by_intent"], total, 24))
    out.extend(_section("city_source", rep["by_city_source"], total))
    out.extend(_section("statuses 组合", rep["by_statuses"], total, 26))
    out.extend(_section("candidates_count", rep["by_candidates_count"], total, 10))
    out.extend(_section("reason", rep["by_reason"], total, 40))
    out.append("")
    out.append("【按 category 分组】")
    for cat in ALLOWED_CATEGORIES:
        n = rep["by_category"].get(cat, 0)
        out.append(f"  {cat:<12} {n:4d} 条  {_pct(n, total)}")
        for act, cnt in rep["cross_category_action"].get(cat, {}).items():
            out.append(f"    - action={act:<16} {cnt:4d}")
    out.append("")
    out.append("【守恒校验】")
    for name, ok in rep["checks"].items():
        out.append(f"  {'OK  ' if ok else 'FAIL'} {name}")
    out.append("")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="P2.6 离线影子报告（只读 JSONL）")
    ap.add_argument("--jsonl", required=True, help="采集产出的 JSONL")
    ap.add_argument("--corpus", default=None,
                    help="观察语料（可选；给出时做 case 对账，区分零记录）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        records = load_jsonl(Path(args.jsonl).expanduser().resolve())
        corpus_cases = None
        if args.corpus:
            corpus = load_corpus(Path(args.corpus).expanduser().resolve())
            corpus_cases = [c["case_id"] for c in corpus]
        rep = build_report(records, corpus_cases)
    except (ContractError, CorpusError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 4
    print(render(rep, args.jsonl, args.corpus))
    bad = [k for k, v in rep["checks"].items() if not v]
    if bad:
        print(f"[FATAL] 守恒校验未通过：{bad}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
