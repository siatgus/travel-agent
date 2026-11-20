"""P2.6 批次 5.0 第二阶段 · 离线采集/报告测试（独立脚本直跑）。

运行：python tests/test_shadow_offline.py

覆盖评审 2026-11-20 CONDITIONAL PASS 的 T1–T7、T9（含复审 R1 的 T4c/T7b、
复审 R2 的 T4d/T4e/T7c/T7d）：
  T1 src/ 零改动（工作区相对 HEAD）
  T2 生产链路零引用（src/ 不 import 两个脚本）
  T3 可复现（两次采集字节级一致）
  T4 离线性（三个受监控 socket 入口 0 次尝试、mock 工具、autofetch 关闭、影子不增加 dispatch）
  T4b 闸门有效性（主动证明三类入口确实被拦，卸载后恢复）
  T4c 闸门安装不完整必须 fail-closed（退出码 7、不进 collect、不写 JSONL）
  T4d 闸门事务式安装（中途失败回滚已替换入口，不留半装状态）
  T4e 清理失败不阻断恢复（uninstall 抛出仍恢复 dispatch/环境变量，并 [WARN] 上报）
  T5 不落原文（utterance 与 entity_text 唯一标记都不进 JSONL）
  T6 fail-loud（缺键/多余键/容器类型错；路径守卫；拒绝覆盖；不留半成品）
  T7 统计守恒（分布之和 == 总数；case 对账）
  T7b 报告负向（重复 case_id 必须失败、缺失 case 必须失败）
  T7c 报告 schema 独立（本地 12 字段白名单，不跟随采集侧漂移）
  T7d 恒真校验已删除（checks 中不再有 each_record_single_category）
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


# ---------------------------------------------------------------- T4c 闸门降级 fail-closed

class _RejectConnectMeta(type):
    """模拟 C 槽位不可写：对 connect 的属性赋值抛 TypeError。"""

    def __setattr__(cls, name, value):
        if name == "connect":
            raise TypeError("cannot set 'connect' attribute of immutable type")
        super().__setattr__(name, value)


class _FakeSocket(metaclass=_RejectConnectMeta):
    def connect(self, address):          # 不应被调用
        raise AssertionError("real socket.connect reached")


class _FakeSocketModule:
    """替身 socket 模块：只有 install() 需要的三个属性。"""

    socket = _FakeSocket
    create_connection = None
    getaddrinfo = None


def test_t4c_guard_partial_fail_closed(tmpdir: Path) -> None:
    """闸门安装不完整（partial=True）时必须中止，且不产生任何产物。

    没有这一条，「摘要里写 guard_partial: true 但照样产出 JSONL」不会被发现。
    """
    real_socket = sc.socket
    out = tmpdir / "partial.jsonl"
    captured_out = tmpdir / "partial.out.txt"
    captured_err = tmpdir / "partial.err.txt"
    called: list[int] = []
    orig_collect = sc.collect
    sc.socket = _FakeSocketModule()
    sc.collect = lambda *a, **k: (called.append(1), ([], {}))[1]
    try:
        with open(captured_out, "w", encoding="utf-8") as fo, \
                open(captured_err, "w", encoding="utf-8") as fe:
            with contextlib.redirect_stdout(fo), contextlib.redirect_stderr(fe):
                code = sc.main(["--corpus", str(CORPUS), "--out", str(out)])
    finally:
        sc.socket = real_socket
        sc.collect = orig_collect
    err = captured_err.read_text(encoding="utf-8")
    check("[T4c] 闸门降级 → main() 返回 7（fail-closed）", code == 7, str(code))
    check("[T4c] 闸门降级 → 未进入 collect()", not called, str(called))
    check("[T4c] 闸门降级 → 不产生 JSONL", not out.exists())
    check("[T4c] 闸门降级 → 不留临时文件",
          not list(tmpdir.glob("partial.jsonl.tmp.*")))
    check("[T4c] 错误信息指明闸门安装不完整",
          "安装不完整" in err, err.strip()[:200])


# ---------------------------------------------------------------- T4d 事务式安装回滚

class _Sentinel:
    """可辨识的原始入口：用 `is` 比较即可证明「确实还原成了原来那个对象」。"""

    def __init__(self, name: str):
        self.name = name

    def __call__(self, *a, **k):           # pragma: no cover
        raise AssertionError(f"原始入口 {self.name} 不应被调用")

    def __repr__(self) -> str:
        return f"<orig {self.name}>"


_ORIG_CREATE = _Sentinel("create_connection")
_ORIG_GAI = _Sentinel("getaddrinfo")


class _PlainSocket:
    connect = _Sentinel("connect")

    def __init__(self):
        raise AssertionError("不应创建真连接")   # pragma: no cover


class _RejectGaiModule:
    """替身 socket 模块：第一个入口可写、第二个入口赋值即失败。"""

    create_connection = _ORIG_CREATE
    getaddrinfo = _ORIG_GAI
    socket = _PlainSocket

    def __init__(self) -> None:
        object.__setattr__(self, "_fail_on", "getaddrinfo")

    def __setattr__(self, name, value):
        if name == self._fail_on:
            raise RuntimeError(f"injected: {name} 赋值失败")
        object.__setattr__(self, name, value)


def test_t4d_install_transactional(tmpdir: Path) -> None:
    """install() 中途失败必须回滚已完成替换，不得留「半装」状态。

    R1 版本的缺口：前两个入口的赋值不在 try 内，第二步抛出时 active 仍为 False，
    main() 外层 `if guard.active` 不会还原 → 进程里留着已被替换的 create_connection。
    """
    real_socket = sc.socket
    # 单元层：直接看 guard 内部状态
    g = sc.NetworkGuard()
    raised = False
    sc.socket = _RejectGaiModule()
    try:
        try:
            g.install()
        except sc.GuardIncomplete:
            raised = True
    finally:
        sc.socket = real_socket
    check("[T4d] 安装第 2 步失败 → 抛 GuardIncomplete", raised)
    check("[T4d] 回滚后 active 为 False（无半装状态）", g.active is False,
          f"active={g.active}")
    check("[T4d] 已替换的 create_connection 被还原（同一对象）",
          g._orig.get("create_connection") is None, str(g._orig))

    # 端到端层：main() 必须 fail-closed，且不进入 collect()
    fake = _RejectGaiModule()
    out = tmpdir / "rollback.jsonl"
    err_path = tmpdir / "rollback.err.txt"
    called: list[int] = []
    orig_collect = sc.collect
    sc.socket = fake
    sc.collect = lambda *a, **k: (called.append(1), ([], {}))[1]
    try:
        with open(err_path, "w", encoding="utf-8") as fe:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(fe):
                code = sc.main(["--corpus", str(CORPUS), "--out", str(out)])
    finally:
        sc.socket = real_socket
        sc.collect = orig_collect
    err = err_path.read_text(encoding="utf-8")
    check("[T4d] main() 返回 7（安装失败 fail-closed）", code == 7, str(code))
    check("[T4d] main() 未进入 collect()", not called, str(called))
    check("[T4d] 不产生 JSONL", not out.exists())
    check("[T4d] 替身模块的 create_connection 已还原（同一对象）",
          fake.create_connection is _ORIG_CREATE, repr(fake.create_connection))
    check("[T4d] 第二个入口始终未被替换", fake.getaddrinfo is _ORIG_GAI)
    check("[T4d] 报错含回滚说明", "安装失败" in err and "回滚" in err,
          err.strip()[:200])


# ---------------------------------------------------------------- T4e 清理失败不阻断恢复

def _flaky_guard_factory(made: list) -> type:
    """返回一个「第一次 uninstall 抛异常、之后正常」的 NetworkGuard 子类工厂。"""

    class _FlakyUninstallGuard(sc.NetworkGuard):
        def __init__(self) -> None:
            super().__init__()
            self.uninstall_calls = 0

        def uninstall(self):
            self.uninstall_calls += 1
            if self.uninstall_calls == 1:
                raise RuntimeError("injected uninstall failure")
            return super().uninstall()

    def factory():
        guard = _FlakyUninstallGuard()
        made.append(guard)
        return guard

    return factory


def test_t4e_cleanup_layers(tmpdir: Path) -> None:
    """uninstall() 抛异常时，dispatch 与环境变量仍必须恢复，且失败要被记录。

    R1 版本的缺口：`finally: guard.uninstall(); agent.dispatch = ...` 顺序执行，
    uninstall 抛出就会跳过两个 dispatch 恢复；外层 finally 若再抛，环境变量与
    临时目录也一并跳过，且错误被吞掉。
    """
    src = str(REPO / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    import travel_agent.agent as agent_mod      # noqa: PLC0415
    import travel_agent.tools as tools_mod      # noqa: PLC0415

    before_agent = agent_mod.dispatch
    before_tools = tools_mod.dispatch
    keys = sorted(set(sc.ENV_PINS) | {"TRAVEL_RESOLVER_ONLINE", "TRAVEL_KB_DB",
                                      "TRAVEL_APP_DB"})
    before_env = {k: os.environ.get(k, "<unset>") for k in keys}

    made: list = []
    orig_guard = sc.NetworkGuard
    out = tmpdir / "cleanup.jsonl"
    err_path = tmpdir / "cleanup.err.txt"
    cap = io.StringIO()
    sc.NetworkGuard = _flaky_guard_factory(made)
    try:
        with open(err_path, "w", encoding="utf-8") as fe:
            with contextlib.redirect_stdout(cap), \
                    contextlib.redirect_stderr(fe):
                code = sc.main(["--corpus", str(CORPUS), "--out", str(out)])
    finally:
        sc.NetworkGuard = orig_guard
    err = err_path.read_text(encoding="utf-8")
    stdout_text = cap.getvalue()
    after_env = {k: os.environ.get(k, "<unset>") for k in keys}

    check("[T4e] 清理失败 → 非 0 退出（不声称成功）", code == 6, str(code))
    check("[T4e] 清理失败被记录为 [WARN] 而非静默",
          "[WARN] 清理未完成" in err and "injected uninstall failure" in err,
          err.strip()[:240])
    check("[T4e] agent.dispatch 已恢复原对象（未因 uninstall 抛出而跳过）",
          agent_mod.dispatch is before_agent, repr(agent_mod.dispatch))
    check("[T4e] tools.dispatch 已恢复原对象",
          tools_mod.dispatch is before_tools, repr(tools_mod.dispatch))
    check("[T4e] 环境变量逐项恢复（外层 finally 未被打断）",
          before_env == after_env,
          str({k: (before_env[k], after_env[k])
               for k in keys if before_env[k] != after_env[k]}))
    check("[T4e] 外层 finally 重试过卸载（uninstall 被调用 2 次）",
          bool(made) and made[0].uninstall_calls == 2,
          str([g.uninstall_calls for g in made]))
    check("[T4e] 重试后闸门确实卸载干净（active False）",
          bool(made) and made[0].active is False)
    check("[T4e] 采集本身成功过（产物存在），但摘要不输出",
          out.exists() and '"ok": true' not in stdout_text,
          f"stdout={stdout_text[:80]!r}")


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


# ---------------------------------------------------------------- T7b 报告负向
def test_t7b_report_negative(tmpdir: Path) -> None:
    """重复 case_id 与缺失 case 都必须让报告失败（非 0 退出）。

    「重复一条」与「缺失一条」是两种真实故障：前者说明同一轮被写了两次，
    后者说明某轮没有记录。二者都不能只打印提示而返回 0。
    """
    out = tmpdir / "neg.jsonl"
    code, d, log = run_collect(CORPUS, out)
    check("[T7b] 前置采集成功", code == 0 and out.exists(), log[:300])
    if not out.exists():
        return
    lines = [l for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    cases = [c["case_id"] for c in sc.load_corpus(CORPUS)]

    def run_report(path: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PY, "scripts/shadow_report.py", "--jsonl", str(path),
             "--corpus", "tests/observ_corpus.tsv"],
            cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace")

    # 1) 重复一条（把 W01 再写一次）
    dup = tmpdir / "dup.jsonl"
    dup.write_text("\n".join(lines + [lines[0]]) + "\n", encoding="utf-8")
    r = run_report(dup)
    check("[T7b] 重复 case_id → 报告退出码 4（契约错误）",
          r.returncode == 4, f"{r.returncode} {r.stderr.strip()[:160]}")
    check("[T7b] 重复 case_id 报出具体 ID 与行号",
          "case_id 重复" in r.stderr and "W01" in r.stderr
          and "首次出现在第 1 行" in r.stderr, r.stderr.strip()[:200])

    # 2) 绕过 load_jsonl（直接传内存列表）也必须判失败
    dup_records = [json.loads(l) for l in lines] + [json.loads(lines[0])]
    rep = sr.build_report(dup_records, cases)
    check("[T7b] 内存列表重复 → no_duplicate_cases 为 False",
          rep["checks"]["no_duplicate_cases"] is False,
          str(rep["duplicate_cases"]))

    # 3) 缺失一条（删掉 W01）
    miss = tmpdir / "miss.jsonl"
    miss.write_text("\n".join(
        l for l in lines if json.loads(l)["case_id"] != "W01") + "\n",
        encoding="utf-8")
    r2 = run_report(miss)
    check("[T7b] 缺失 case → 报告退出码 5（守恒/对账未通过）",
          r2.returncode == 5, f"{r2.returncode} {r2.stderr.strip()[:160]}")
    check("[T7b] 缺失 case 在 stdout 列出，且 stderr 点名 no_missing_cases",
          "W01" in r2.stdout and "no_missing_cases" in r2.stderr,
          r2.stderr.strip()[:200])


# ---------------------------------------------------------------- T7c 报告 schema 独立性

def test_t7c_report_schema_independent(tmpdir: Path) -> None:
    """报告必须维护自己的 12 字段白名单，不能跟随采集侧 schema 漂移。

    R1 版本 `from shadow_collect import EXPORT_KEYS`：采集侧改字段时报告自动接受，
    独立校验形同虚设（评审 R2 指出）。
    """
    expected = ("case_id", "category", "intent", "takeover", "batch_enabled",
                "action", "reason", "statuses", "city_source", "entity_city",
                "confidence", "candidates_count")
    check("[T7c] 报告侧独立定义 12 字段白名单",
          tuple(sr.REPORT_EXPORT_KEYS) == expected, str(sr.REPORT_EXPORT_KEYS))
    check("[T7c] 报告模块不再持有 EXPORT_KEYS 名字",
          not hasattr(sr, "EXPORT_KEYS"))
    check("[T7c] 两侧白名单不是同一对象（内容可相同）",
          sr.REPORT_EXPORT_KEYS is not sc.EXPORT_KEYS)

    # 源级判定：赋值右值必须是字符串字面量元组，不能是 import / 调用 / 变量引用。
    # 只看运行时对象相等不够——`X = __import__("shadow_collect").EXPORT_KEYS`
    # 的对象同样「内容正确」，但那是共享，不是独立硬编码。
    import ast                                  # noqa: PLC0415
    tree = ast.parse((SCRIPTS / "shadow_report.py").read_text(encoding="utf-8"))
    literal_assign = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "REPORT_EXPORT_KEYS"
                for t in node.targets):
            literal_assign = (
                isinstance(node.value, ast.Tuple)
                and bool(node.value.elts)
                and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                        for e in node.value.elts))
    check("[T7c] REPORT_EXPORT_KEYS 由本模块字面量元组硬编码（AST 判定）",
          literal_assign)

    out = tmpdir / "schema.jsonl"
    code, d, log = run_collect(CORPUS, out)
    check("[T7c] 前置采集成功", code == 0 and out.exists(), log[:300])
    if not out.exists():
        return

    # 行为证明：篡改采集侧白名单后，报告仍按自己的 12 字段判定（无活依赖）
    orig_keys = sc.EXPORT_KEYS
    sc.EXPORT_KEYS = tuple(orig_keys) + ("extra_field",)
    try:
        rows = sr.load_jsonl(out)
        independent = len(rows) == 21
        err_msg = ""
    except Exception as exc:                   # noqa: BLE001
        independent = False
        err_msg = f"{type(exc).__name__}: {exc}"
    finally:
        sc.EXPORT_KEYS = orig_keys
    check("[T7c] 采集侧白名单被篡改后，报告仍能读自己的 12 字段",
          independent, err_msg)

    # 反向证明：报告侧白名单确实在生效——缺一个字段必须报错
    first = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    first.pop("confidence")
    bad = tmpdir / "bad_schema.jsonl"
    bad.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
    rejected = False
    msg = ""
    try:
        sr.load_jsonl(bad)
    except sc.ContractError as exc:
        rejected = True
        msg = str(exc)
    check("[T7c] 少一个字段 → 报告抛 ContractError（用自己的白名单判定）",
          rejected and "缺失" in msg and "confidence" in msg, msg[:200])


# ---------------------------------------------------------------- T7d 恒真校验清理

def test_t7d_no_tautological_check(tmpdir: Path) -> None:
    """`each_record_single_category = (len(records) == total)` 恒真，已删除。

    留着它会被当成一条「守恒校验通过」的证据，实际不提供任何保证。
    """
    out = tmpdir / "taut.jsonl"
    code, d, log = run_collect(CORPUS, out)
    check("[T7d] 前置采集成功", code == 0 and out.exists(), log[:300])
    if not out.exists():
        return
    rep = sr.build_report(sr.load_jsonl(out),
                          [c["case_id"] for c in sc.load_corpus(CORPUS)])
    check("[T7d] checks 中不再有 each_record_single_category",
          "each_record_single_category" not in rep["checks"],
          str(sorted(rep["checks"])))
    check("[T7d] 剩余 9 条校验均为实质校验且全部通过",
          len(rep["checks"]) == 9 and all(rep["checks"].values()),
          f"{len(rep['checks'])} 条：{rep['checks']}")


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
        test_t4c_guard_partial_fail_closed(tmpdir)
        test_t4d_install_transactional(tmpdir)
        test_t4e_cleanup_layers(tmpdir)
        test_t5_no_raw_text(tmpdir)
        test_t6_fail_loud(tmpdir)
        test_t7_conservation(tmpdir)
        test_t7b_report_negative(tmpdir)
        test_t7c_report_schema_independent(tmpdir)
        test_t7d_no_tautological_check(tmpdir)
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
