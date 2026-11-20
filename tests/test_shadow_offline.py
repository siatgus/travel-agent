"""P2.6 批次 5.0 第二阶段 · 离线采集/报告测试（独立脚本直跑）。

运行：python tests/test_shadow_offline.py

覆盖评审 2026-11-20 CONDITIONAL PASS 的 T1–T7、T9：
  T1 src/ 零改动（工作区相对 HEAD）
  T2 生产链路零引用（src/ 不 import 两个脚本）
  T3 可复现（两次采集字节级一致）
  T4 离线性（网络尝试 0、mock 工具、autofetch 关闭、影子不增加 dispatch）
  T5 不落原文（utterance 与 entity_text 唯一标记都不进 JSONL）
  T6 fail-loud（缺键/多余键/容器类型错；路径守卫；拒绝覆盖；不留半成品）
  T7 统计守恒（分布之和 == 总数；case 对账）
  T9 开关冻结（进程内 main() 前后环境变量逐项恢复）

T8（17 套件回归）由验收证据承担，本文件不重复执行。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CORPUS = REPO / "tests" / "observ_corpus.tsv"
PY = sys.executable

sys.path.insert(0, str(SCRIPTS))

import shadow_collect as sc          # noqa: E402
import shadow_report as sr           # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


def run_collect(corpus: Path, out: Path, *extra: str) -> tuple[int, dict | None, str]:
    cmd = [PY, str(SCRIPTS / "shadow_collect.py"),
           "--corpus", str(corpus), "--out", str(out), *extra]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    data = None
    if r.returncode == 0 and r.stdout.strip():
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            data = None
    return r.returncode, data, (r.stdout + r.stderr)


# ---------------------------------------------------------------- T1

def test_t1_src_zero_diff() -> None:
    r = subprocess.run(["git", "status", "--porcelain", "--", "src/"],
                       cwd=str(REPO), capture_output=True, text=True)
    check("[T1] src/ 工作区相对 HEAD 无改动（git status --porcelain 空）",
          r.stdout.strip() == "", r.stdout[:300])
    r2 = subprocess.run(["git", "diff", "--stat", "HEAD", "--", "src/"],
                        cwd=str(REPO), capture_output=True, text=True)
    check("[T1] src/ 相对 HEAD 零 diff（git diff --stat 空）",
          r2.stdout.strip() == "", r2.stdout[:300])


# ---------------------------------------------------------------- T2

def test_t2_production_no_reference() -> None:
    hits: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in ("shadow_collect", "shadow_report"):
            if token in text:
                hits.append(f"{path.relative_to(REPO)}: {token}")
    check("[T2] src/ 内无 shadow_collect / shadow_report 引用", not hits, str(hits))

    server = (REPO / "src" / "travel_agent" / "server.py").read_text(
        encoding="utf-8", errors="replace")
    check("[T2] server.py 无离线脚本 import",
          "shadow_collect" not in server and "shadow_report" not in server)


# ---------------------------------------------------------------- T3

def test_t3_reproducible(tmpdir: Path) -> None:
    out1 = tmpdir / "r1.jsonl"
    out2 = tmpdir / "r2.jsonl"
    code1, d1, log1 = run_collect(CORPUS, out1)
    code2, d2, log2 = run_collect(CORPUS, out2)
    check("[T3] 两次采集均成功", code1 == 0 and code2 == 0, (log1 + log2)[:400])
    b1 = out1.read_bytes() if out1.exists() else b""
    b2 = out2.read_bytes() if out2.exists() else b""
    check("[T3] 两次 JSONL 字节级一致", b1 == b2 and len(b1) > 0,
          f"len1={len(b1)} len2={len(b2)}")
    check("[T3] sha256 一致",
          bool(d1 and d2 and d1["out_sha256"] == d2["out_sha256"]),
          f"{d1 and d1['out_sha256']} vs {d2 and d2['out_sha256']}")
    check("[T3] 记录数 == 语料 case 数（21）",
          bool(d1 and d1["exported_records"] == 21 and d1["corpus_cases"] == 21),
          str(d1 and (d1["exported_records"], d1["corpus_cases"])))


# ---------------------------------------------------------------- T4

def test_t4_offline(tmpdir: Path) -> None:
    on_out = tmpdir / "on.jsonl"
    code_on, d_on, log_on = run_collect(CORPUS, on_out)
    check("[T4] 影子开启采集成功", code_on == 0, log_on[:400])
    if d_on is None:
        check("[T4] 摘要可解析", False, log_on[:400])
        return
    off = d_on["offline"]
    check("[T4] 网络尝试（socket 闸门）为 0", off["network_attempts"] == 0,
          str(off["network_attempts"]))
    check("[T4] USE_REAL_DATA=0", off["use_real_data"] == "0", str(off["use_real_data"]))
    check("[T4] 工具层为 mock_tools（零 urllib）",
          off["tools_module"].endswith("mock_tools"), off["tools_module"])
    check("[T4] autofetch.ENABLED 为 False", off["autofetch_enabled"] is False,
          str(off["autofetch_enabled"]))
    check("[T4] socket 闸门完整安装（非降级）", off["guard_partial"] is False,
          str(off["guard_partial"]))

    # 对照：影子关闭时 dispatch 计数应逐项相同 → 影子贡献 0 次 dispatch
    base_out = tmpdir / "base.jsonl"
    code_off, d_off, log_off = run_collect(CORPUS, base_out, "--shadow-mode", "off")
    check("[T4] 对照运行（shadow=off）成功且零记录",
          code_off == 0 and d_off is not None and d_off["exported_records"] == 0,
          (log_off or "")[:400])
    if d_off is not None:
        check("[T4] 影子不增加 dispatch 调用（on==off 计数）",
              d_on["offline"]["dispatch_calls"] == d_off["offline"]["dispatch_calls"],
              f"on={d_on['offline']['dispatch_calls']} "
              f"off={d_off['offline']['dispatch_calls']}")
        check("[T4] 两次 dispatch 调用名序列完全一致",
              d_on["offline"]["dispatch_names"] == d_off["offline"]["dispatch_names"],
              f"on={d_on['offline']['dispatch_names']} off={d_off['offline']['dispatch_names']}")
        check("[T4] 对照运行同样零网络尝试",
              d_off["offline"]["network_attempts"] == 0)


# ---------------------------------------------------------------- T4b 闸门有效性

def test_t4b_network_guard_blocks() -> None:
    """证明闸门不是装饰品：主动发起连接必须被拦截并计数。

    没有这一条，「采集期间 network_attempts == 0」可能只说明闸门没被调用，
    而不能说明它真的能拦住网络（评审的核心质疑）。
    """
    import socket as sockmod

    guard = sc.NetworkGuard()
    guard.install()
    try:
        raised_create = False
        try:
            sockmod.create_connection(("127.0.0.1", 9), timeout=0.2)
        except sc.NetworkAttempted:
            raised_create = True
        check("[T4b] 闸门拦截 create_connection", raised_create,
              str(guard.attempts))

        raised_gai = False
        try:
            sockmod.getaddrinfo("example.invalid", 80)
        except sc.NetworkAttempted:
            raised_gai = True
        check("[T4b] 闸门拦截 getaddrinfo", raised_gai, str(guard.attempts))

        raised_connect = False
        s = sockmod.socket()
        try:
            s.connect(("127.0.0.1", 9))
        except sc.NetworkAttempted:
            raised_connect = True
        except OSError:
            raised_connect = False        # 没被闸门拦住就是失败
        finally:
            s.close()
        check("[T4b] 闸门拦截 socket.connect", raised_connect, str(guard.attempts))

        check("[T4b] 三次尝试均被计数", len(guard.attempts) == 3,
              str(guard.attempts))
        check("[T4b] 闸门完整安装（非降级）", guard.partial is False)
    finally:
        guard.uninstall()

    # 卸载后必须恢复原实现，否则会污染同进程后续操作
    restored = False
    try:
        sockmod.getaddrinfo("127.0.0.1", 9)
        restored = True
    except sc.NetworkAttempted:
        restored = False
    except OSError:
        restored = True                   # 解析失败也算已恢复（不再被闸门拦）
    check("[T4b] 卸载后 socket 行为恢复", restored)
    check("[T4b] 卸载后 attempts 不再增长", len(guard.attempts) == 3,
          str(len(guard.attempts)))


# ---------------------------------------------------------------- T5
def test_t5_no_raw_text(tmpdir: Path) -> None:
    token = "ZQXOBSTOKEN"
    corpus = tmpdir / "tok_corpus.tsv"
    corpus.write_text(
        "case_id\tcategory\tutterance\tcontext\n"
        f"E1\tpoi\t{token} 帮我看看成都宽窄巷子\t"
        '{"slots":{"destination":"成都"}}\n',
        encoding="utf-8")
    out = tmpdir / "tok.jsonl"
    code, d, log = run_collect(corpus, out)
    text = out.read_text(encoding="utf-8") if out.exists() else ""
    check("[T5] 含标记的语料采集成功", code == 0, log[:400])
    check("[T5] utterance 唯一标记不出现在 JSONL", token not in text,
          text[:300])

    # 单元层：构造 entity_text 含标记的内部记录，投影后必须被剔除
    rec = {k: None for k in sc.INTERNAL_KEYS}
    rec.update({"intent": "poi_query", "takeover": False, "batch_enabled": False,
                "action": "clarify", "reason": "r", "entity_text": f"{token}-ENT",
                "statuses": ["ambiguous"], "city_source": "query_city",
                "entity_city": "成都", "confidence": 0.9, "candidates_count": 1})
    projected = sc.project_record("E2", "poi", rec)
    check("[T5] 导出记录不含 entity_text 键", "entity_text" not in projected,
          str(sorted(projected)))
    check("[T5] entity_text 的唯一标记不随投影泄漏",
          token not in json.dumps(projected, ensure_ascii=False),
          json.dumps(projected, ensure_ascii=False))
    check("[T5] 导出记录恰 12 字段", len(projected) == 12 and
          set(projected) == set(sc.EXPORT_KEYS), str(sorted(projected)))


# ---------------------------------------------------------------- T6

def test_t6_fail_loud(tmpdir: Path) -> None:
    base = {k: None for k in sc.INTERNAL_KEYS}
    base.update({"intent": "i", "takeover": False, "batch_enabled": False,
                 "action": None, "reason": "x", "entity_text": None,
                 "statuses": [], "city_source": None, "entity_city": None,
                 "confidence": None, "candidates_count": 0})

    missing = dict(base)
    missing.pop("city_source")
    try:
        sc.validate_record("C1", missing)
        ok_missing = False
        detail = "未抛异常"
    except sc.ContractError as exc:
        ok_missing = "缺失=['city_source']" in str(exc)
        detail = str(exc)
    check("[T6] 缺键 → ContractError 且报出缺失键", ok_missing, detail)

    extra = dict(base)
    extra["utterance_echo"] = "leak"
    try:
        sc.validate_record("C2", extra)
        ok_extra = False
        detail = "未抛异常"
    except sc.ContractError as exc:
        ok_extra = "多余=['utterance_echo']" in str(exc)
        detail = str(exc)
    check("[T6] 多余键 → ContractError 且报出多余键", ok_extra, detail)

    try:
        sc.validate_record("C3", "not-a-dict")
        ok_type = False
        detail = "未抛异常"
    except sc.ContractError as exc:
        ok_type = "应为 dict" in str(exc)
        detail = str(exc)
    check("[T6] 记录容器类型错误 → ContractError", ok_type, detail)

    # 路径守卫：--out 指向仓库内 → 退出码 2，且不留下任何文件
    inside = REPO / "P26_SHOULD_NOT_EXIST.jsonl"
    if inside.exists():
        inside.unlink()
    cmd = [PY, str(SCRIPTS / "shadow_collect.py"),
           "--corpus", str(CORPUS), "--out", str(inside)]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("[T6] --out 在仓库内 → 非 0 退出", r.returncode != 0, str(r.returncode))
    check("[T6] 路径守卫失败不留产物", not inside.exists())
    tmps = list(REPO.glob("P26_SHOULD_NOT_EXIST.jsonl.tmp.*"))
    check("[T6] 路径守卫失败不留临时文件", not tmps, str(tmps))

    # 已有文件默认拒绝覆盖
    exists = tmpdir / "exists.jsonl"
    exists.write_text("{}\n", encoding="utf-8")
    code, d, log = run_collect(CORPUS, exists)
    check("[T6] 已有输出默认拒绝覆盖（非 0 退出）", code != 0, str(code))
    check("[T6] 拒绝覆盖时原文件内容不变",
          exists.read_text(encoding="utf-8") == "{}\n",
          exists.read_text(encoding="utf-8")[:80])


# ---------------------------------------------------------------- T7

def test_t7_conservation(tmpdir: Path) -> None:
    out = tmpdir / "cons.jsonl"
    code, d, log = run_collect(CORPUS, out)
    if code != 0 or not out.exists():
        check("[T7] 采集成功（守恒前置）", False, log[:400])
        return
    records = sr.load_jsonl(out)
    corpus = sc.load_corpus(CORPUS)
    rep = sr.build_report(records, [c["case_id"] for c in corpus])
    bad = [k for k, v in rep["checks"].items() if not v]
    check("[T7] 全部守恒校验通过", not bad, str(bad))
    check("[T7] 分布之和 == 总数",
          sum(rep["by_action"].values()) == rep["total"] == 21,
          f"{sum(rep['by_action'].values())} vs {rep['total']}")
    check("[T7] 每个 case 仅归入一个 category",
          all(len({r["category"]}) == 1 for r in records))
    check("[T7] 语料与记录 case 集合一致（无缺失/无多余）",
          not rep["missing_cases"] and not rep["extra_cases"],
          f"missing={rep['missing_cases']} extra={rep['extra_cases']}")

    # 人工构造缺失：删掉一条 → 报告必须报出该 case（区分零记录与失败）
    partial = [r for r in records if r["case_id"] != "W01"]
    rep2 = sr.build_report(partial, [c["case_id"] for c in corpus])
    check("[T7] 缺一条时报告能定位到具体 case（区分零记录）",
          rep2["missing_cases"] == ["W01"], str(rep2["missing_cases"]))
    check("[T7] 缺失时总数相应减少",
          rep2["total"] == 20 and sum(rep2["by_category"].values()) == 20,
          f"{rep2['total']}")


# ---------------------------------------------------------------- T9

def test_t9_env_restored(tmpdir: Path) -> None:
    keys = sorted(set(sc.ENV_PINS) | {"TRAVEL_RESOLVER_ONLINE", "TRAVEL_KB_DB",
                                      "TRAVEL_APP_DB"})
    before = {k: os.environ.get(k, "<unset>") for k in keys}
    out = tmpdir / "env.jsonl"
    captured = tmpdir / "stdout.txt"
    # 用真实文件重定向：agent.py 在 import 时会 sys.stdout.reconfigure()，
    # StringIO 没有该方法（这也是一条值得记录的装具约束）。
    with open(captured, "w", encoding="utf-8") as fh:
        with contextlib.redirect_stdout(fh):
            code = sc.main(["--corpus", str(CORPUS), "--out", str(out)])
    stdout_text = captured.read_text(encoding="utf-8")
    after = {k: os.environ.get(k, "<unset>") for k in keys}
    check("[T9] 进程内 main() 返回 0", code == 0, str(code))
    check("[T9] 采集后环境变量逐项恢复", before == after,
          str({k: (before[k], after[k]) for k in keys if before[k] != after[k]}))
    check("[T9] 采集期间确实固定过开关（输出含 env_during）",
          '"TRAVEL_DECISION_SHADOW": "1"' in stdout_text)


# ---------------------------------------------------------------- main

def main() -> int:
    if not CORPUS.is_file():
        print(f"[FATAL] 语料缺失：{CORPUS}")
        return 2
    tmpdir = Path(tempfile.mkdtemp(prefix="p26off-"))
    try:
        for fn in (test_t1_src_zero_diff,
                   test_t2_production_no_reference):
            fn()
        test_t3_reproducible(tmpdir)
        test_t4_offline(tmpdir)
        test_t4b_network_guard_blocks()
        test_t5_no_raw_text(tmpdir)
        test_t6_fail_loud(tmpdir)
        test_t7_conservation(tmpdir)
        test_t9_env_restored(tmpdir)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = total - passed
    print(f"\nshadow offline (P2.6 st.2): {passed} pass / {failed} fail")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
