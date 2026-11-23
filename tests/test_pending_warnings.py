"""R-01-I-4 测试矩阵 W1–W10（独立脚本直跑）。

运行：python tests/test_pending_warnings.py

覆盖范围（任务书 r3 §七 + §7.3，W10 见开工申请 §6.2，2026-11-23 已批准）：

  W1  静态单一通道：AST 口径 12 处 `self._warn(...)` / 0 处
      `self._pending_warnings.append(...)`；按「所属方法 + 方法内序号 +
      `ast.unparse(args[0])`」与 §7.3 冻结签名逐字保序对应；方法分布
      2/3/3/1/3。**不断言行号**——真实行号与偏移量只输出，偏移量非递减
      作为 FAIL 判据（形态可疑即失败）。
  W2  容量 50 / FIFO：直调 60 次 → 保留后 50 条，淘汰 10 条。
  W3  单条 300 前缀截断：404 字符入参 → 恰 300，首 4 字符 HEAD。
  W4  类型收敛：非 str → "<non-str:int>"；None 丢弃、len 不变。
  W5  RIE 分支：首次写入即失败 → 不外抛、dead=True、stats 不提交；
      dead 后不再调用 append（计数装具）。
  W6  与 S1 失败域解耦：patch 编排模块的 `append_bounded` 抛 RIE →
      S1 置 dead，而 W 仍可写入（D4 的锁定用例）。
  W7  三生产开关全关 → 零写入（零装具）；5 键逐键快照—恢复。
  W8  12 站点可达性抽样：#1 / #10（身份缺失分支）各写 1 条。
  W9  真实站点容量压力：两站点各 60 次 → 120 条写入，保留第 71..120 次。
  W10 D1 非 RIE 分支：静默、不置 dead、stats 不提交，恢复后可正常写入。

纪律：
- 每个用例首行做能力探测（`hasattr(Orchestrator, "_warn")`）；在 `69353c9`
  （无 `_warn`）上记一条 FAIL 后返回，保证红对照输出可读而不是整脚本崩溃。
- 环境改动用逐键快照—恢复（区分 unset 与空串），禁止归零式恢复（F-01 教训）。
- 本文件自建快照函数与装具，不 import 其他测试文件的私有实现。
- 只测 W 容器与 `_warn` 通道，不跑完整 `handle`，不启用任何生产灰度。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import travel_agent.retention as retention                  # noqa: E402
import travel_agent.orchestrator.orchestrator as _orch_mod   # noqa: E402
from travel_agent.orchestrator import Orchestrator          # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("　" + detail) if detail and not cond else ""))


def _probe(case: str) -> bool:
    """能力探测：旧实现上没有 `_warn`，记 FAIL 后返回，不抛异常。"""
    if hasattr(Orchestrator, "_warn"):
        return True
    check(case + " 能力探测", False,
          "Orchestrator._warn 不存在（基线 69353c9 预期行为）")
    return False


# ---------------------------------------------------------------- 环境快照

# 与任务书 §7.1 完全一致的 5 键（自建，不 import 其他测试的私有常量）
_ENV_KEYS = ("TRAVEL_PENDING_CHANNEL", "TRAVEL_DECISION_SHADOW",
             "TRAVEL_DECISION_TAKEOVER", "TRAVEL_PENDING_STORE",
             "TRAVEL_PENDING_RUNTIME_MAX")
# 3 个生产开关（W7 前提断言对象）
_PROD_SWITCHES = ("TRAVEL_PENDING_CHANNEL", "TRAVEL_DECISION_SHADOW",
                  "TRAVEL_DECISION_TAKEOVER")


def _env_snapshot() -> dict[str, str | None]:
    """未设置 → None（≠ 空串），供逐键精确恢复。"""
    return {k: os.environ.get(k) for k in _ENV_KEYS}


def _env_restore(snap: dict[str, str | None]) -> None:
    for k in _ENV_KEYS:
        v = snap[k]
        if v is None:
            os.environ.pop(k, None)      # 原本 unset → 恢复 unset
        else:
            os.environ[k] = v            # 原本有值（含空串）→ 写回原值


def _env_set(**kwargs: str | None) -> None:
    """None = 移除（回到默认），其余写入原值。"""
    for k, v in kwargs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------- W1 装具

# 任务书 r3 §7.3 冻结签名（基线 69353c9，ast.unparse(Call.args[0]) 逐字）
# 第 3 列基线行号**只作交叉参照**，不参与断言。
FROZEN_SIGNATURES = [
    ("_pending_gate", 0, 159,
     "'pending gate bypass: session 身份未注入（pending_session_id 缺失）'"),
    ("_pending_gate", 1, 199,
     "'pending gate: act 异常且无法判定 store 状态（保守拦截，hint 保留）'"),
    ("_revalidate_after_consume", 0, 410,
     "f'pending revalidate 异常 {type(exc).__name__}: {exc}'"),
    ("_revalidate_after_consume", 1, 417,
     "'pending revalidate: ' + (re_out.detail or '未产出结论')"),
    ("_revalidate_after_consume", 2, 423,
     "'pending revalidate 回填失败: ' + (re_out.detail or '')"),
    ("_allow_handler_consume", 0, 507,
     "'pending consume execute: 注入前置缺失（kind/utterance/city）'"),
    ("_allow_handler_consume", 1, 543,
     "f'pending consume execute 注入失败 {type(exc).__name__}: {exc}'"),
    ("_allow_handler_consume", 2, 561,
     "'pending consume execute: 验收位回退（fresh 实体未见于回复 / "
     "other_city 冲突信号）→ 确认话术'"),
    ("_decision_shadow", 0, 671,
     "f'decision shadow: {type(exc).__name__}: {exc}'"),
    ("_pending_produce", 0, 697,
     "'pending produce bypass: session 身份未注入（pending_session_id 缺失）'"),
    ("_pending_produce", 1, 720,
     "f'pending produce: 决策链异常 {type(exc).__name__}: {exc}'"),
    ("_pending_produce", 2, 728,
     "'pending produce: clarify 但 HC-4 排除后无可入快照候选'"),
]

FROZEN_DISTRIBUTION = {"_pending_gate": 2, "_revalidate_after_consume": 3,
                       "_allow_handler_consume": 3, "_decision_shadow": 1,
                       "_pending_produce": 3}


def _is_self_attr_call(node, attr: str) -> bool:
    """`self.<attr>(...)` 形态判定。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self")


def _is_w_pending_append(node) -> bool:
    """`self._pending_warnings.append(...)` 形态判定（AST 口径，注释不计）。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_pending_warnings"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self")


def _walk_method(node, out_warn, out_append) -> None:
    """收集方法体内的调用点，**不进入**嵌套 def/class（避免重复计数）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            continue
        if _is_self_attr_call(child, "_warn"):
            out_warn.append(child)
        elif _is_w_pending_append(child):
            out_append.append(child)
        _walk_method(child, out_warn, out_append)


def test_w1_static_channel() -> None:
    case = "W1"
    if not _probe(case):
        return
    path = _SRC / "travel_agent" / "orchestrator" / "orchestrator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    warn_calls: list[tuple[str, int, int, str]] = []
    append_calls: list[tuple[str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for member in node.body:
            if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            w, a = [], []
            _walk_method(member, w, a)
            w.sort(key=lambda c: (c.lineno, c.col_offset))
            for i, call in enumerate(w):
                expr = ast.unparse(call.args[0]) if call.args else "<no-args>"
                warn_calls.append((member.name, i, call.lineno, expr))
            append_calls.extend((member.name, c.lineno) for c in a)

    warn_calls.sort(key=lambda t: t[2])
    append_calls.sort(key=lambda t: t[1])

    check(case + "-A1 self._warn 调用点 == 12", len(warn_calls) == 12,
          "实测 %d：%r" % (len(warn_calls),
                           [(m, i, ln) for m, i, ln, _e in warn_calls]))
    check(case + "-A2 self._pending_warnings.append 调用点 == 0",
          len(append_calls) == 0, "实测 %d：%r" % (len(append_calls),
                                                   append_calls))

    dist: dict[str, int] = {}
    for m, _i, _ln, _e in warn_calls:
        dist[m] = dist.get(m, 0) + 1
    check(case + "-A3 方法分布 == 2/3/3/1/3", dist == FROZEN_DISTRIBUTION,
          repr(dist))

    actual = [(m, i, e) for m, i, _ln, e in warn_calls]
    expect = [(m, i, e) for m, i, _b, e in FROZEN_SIGNATURES]
    same = len(actual) == len(expect) and all(
        a == b for a, b in zip(actual, expect))
    detail = ""
    if not same:
        for n, (a, b) in enumerate(zip(actual, expect)):
            if a != b:
                detail = "第 %d 项不符：实测 %r / 冻结 %r" % (n + 1, a, b)
                break
        else:
            detail = "长度不符：实测 %d / 冻结 %d" % (len(actual), len(expect))
    check(case + "-A4 12 项签名与 §7.3 冻结表逐字保序相等", same, detail)

    keys = [(m, i) for m, i, _ln, _e in warn_calls]
    check(case + "-A5 (方法, 序号) 无重复", len(keys) == len(set(keys)),
          repr(keys))

    offsets = [ln - base for (_m, _i, ln, _e), (_m2, _i2, base, _e2)
               in zip(warn_calls, FROZEN_SIGNATURES)]
    check(case + "-A6 偏移量非递减", all(
        offsets[i] <= offsets[i + 1] for i in range(len(offsets) - 1)),
          repr(offsets))

    # 行号只输出，不进断言（第 6 条要求）
    print("\n%s 行号对照（只输出，不断言）" % case)
    print("  #   方法                        序号  基线行  当前行  偏移")
    for n, ((m, i, ln, _e), (_m2, _i2, base, _e2)) in enumerate(
            zip(warn_calls, FROZEN_SIGNATURES), start=1):
        print("  %-3d %-26s  %-4d %-7d %-7d %+d"
              % (n, m, i, base, ln, ln - base))
    print("  offsets = %r（去重 %r）\n" % (offsets, sorted(set(offsets))))


# ---------------------------------------------------------------- W2–W5

def test_w2_capacity_fifo() -> None:
    case = "W2"
    if not _probe(case):
        return
    o = Orchestrator()
    for i in range(60):
        o._warn("w-%02d" % i)
    items = o._pending_warnings
    st = o._pending_warnings_stats
    check(case + "-1 len == 50", len(items) == 50, "实测 %d" % len(items))
    check(case + "-2 capacity == WARNING_CAPACITY(50)",
          st is not None and st.capacity == retention.WARNING_CAPACITY,
          repr(None if st is None else st.capacity))
    check(case + "-3 total_appended == 60",
          st is not None and st.total_appended == 60,
          repr(None if st is None else st.total_appended))
    check(case + "-4 total_evicted == 10",
          st is not None and st.total_evicted == 10,
          repr(None if st is None else st.total_evicted))
    check(case + "-5 首条 == 'w-10'", bool(items) and items[0] == "w-10",
          repr(items[:1]))
    check(case + "-6 末条 == 'w-59'", bool(items) and items[-1] == "w-59",
          repr(items[-1:]))
    check(case + "-7 current_size == len",
          st is not None and st.current_size == len(items),
          repr(None if st is None else st.current_size))


def test_w3_truncation() -> None:
    case = "W3"
    if not _probe(case):
        return
    raw = "HEAD" + "y" * 400
    o = Orchestrator()
    o._warn(raw)
    items = o._pending_warnings
    item = items[0] if items else None
    check(case + "-1 恰 1 条", len(items) == 1, "实测 %d" % len(items))
    check(case + "-2 长度 == 300", isinstance(item, str) and len(item) == 300,
          repr(None if item is None else len(item)))
    check(case + "-3 内容 == raw[:300]", item == raw[:300],
          "首 8 字符 %r" % (item[:8] if isinstance(item, str) else item))
    check(case + "-4 首 4 字符 == 'HEAD'",
          isinstance(item, str) and item[:4] == "HEAD",
          repr(item[:4] if isinstance(item, str) else item))
    check(case + "-5 其余全为 'y'",
          isinstance(item, str) and set(item[4:]) == {"y"},
          repr(sorted(set(item[4:])) if isinstance(item, str) else item))


def test_w4_type_coercion() -> None:
    case = "W4"
    if not _probe(case):
        return
    o = Orchestrator()
    o._warn(123)
    items = o._pending_warnings
    check(case + "-1 非 str → '<non-str:int>'",
          len(items) == 1 and items[0] == "<non-str:int>", repr(items))
    before = len(items)
    o._warn(None)
    check(case + "-2 None 不追加", len(o._pending_warnings) == before,
          "追加前 %d / 追加后 %d" % (before, len(o._pending_warnings)))


class _AppendBoom(list):
    """容器被换成这个：append 必抛（RIE 分支装具）。"""

    calls = 0

    def append(self, item):
        type(self).calls += 1
        raise RuntimeError("append boom")


def test_w5_rie_dead() -> None:
    case = "W5"
    if not _probe(case):
        return
    o = Orchestrator()
    _AppendBoom.calls = 0
    o._pending_warnings = _AppendBoom()
    # 计数装具打 retention.append_bounded（模块属性）——`_warn` 走的就是它；
    # 打 orchestrator.append_bounded 既测不到，也违背 §七 的口径要求。
    orig = retention.append_bounded
    n_calls: list[int] = []

    def _counting(*a, **k):
        n_calls.append(1)
        return orig(*a, **k)

    retention.append_bounded = _counting
    escaped = None
    try:
        o._warn("x")
    except BaseException as exc:          # 测试侧宽捕获，逐项断言
        escaped = exc
    finally:
        retention.append_bounded = orig
    check(case + "-1 _warn 不外抛", escaped is None, repr(escaped))
    check(case + "-2 dead is True", o._pending_warnings_dead is True,
          repr(o._pending_warnings_dead))
    check(case + "-3 stats 不提交（仍为 None）",
          o._pending_warnings_stats is None, repr(o._pending_warnings_stats))
    first_append = _AppendBoom.calls
    first_calls = len(n_calls)
    check(case + "-4 装具有效（首次确已调用 append_bounded 与 append）",
          first_calls == 1 and first_append == 1,
          "append_bounded=%d container.append=%d"
          % (first_calls, first_append))

    retention.append_bounded = _counting
    try:
        for _ in range(3):
            o._warn("y")
    finally:
        retention.append_bounded = orig
    check(case + "-5 dead 后不再调用 append_bounded",
          len(n_calls) == first_calls,
          "首次后 %d / 再调 3 次后 %d" % (first_calls, len(n_calls)))
    check(case + "-6 dead 后不再调用容器 append",
          _AppendBoom.calls == first_append,
          "首次后 %d / 再调 3 次后 %d" % (first_append, _AppendBoom.calls))


# ---------------------------------------------------------------- W6

def test_w6_s1_decoupling() -> None:
    case = "W6"
    if not _probe(case):
        return
    snap = _env_snapshot()
    orig = _orch_mod.append_bounded
    try:
        _env_set(TRAVEL_DECISION_SHADOW="1", TRAVEL_PENDING_CHANNEL=None)

        def _boom(*_a, **_k):
            raise retention.RetentionInvariantError("mutation")

        _orch_mod.append_bounded = _boom
        o = Orchestrator()
        session = SimpleNamespace()
        ctx = SimpleNamespace(parse=SimpleNamespace(
            shadow_resolved_objs=[], intent="", destination=""))
        escaped = None
        try:
            o._decision_shadow(session, ctx)
        except BaseException as exc:
            escaped = exc
        check(case + "-1 影子触达不外抛", escaped is None, repr(escaped))
        check(case + "-2 S1 置 dead", o._decision_shadow_dead is True,
              repr(o._decision_shadow_dead))
        check(case + "-3 S1 失败 → 恰 1 条 warning",
              len(o._pending_warnings) == 1, repr(o._pending_warnings))
        check(case + "-4 W 未 dead", o._pending_warnings_dead is False,
              repr(o._pending_warnings_dead))
        # 正向补强：patch 仍在生效时 W 仍可写（证明走的是 retention 模块属性）
        o._warn("W 与 S1 解耦")
        check(case + "-5 patch 生效期间 W 仍可写入",
              len(o._pending_warnings) == 2 and
              o._pending_warnings[-1] == "W 与 S1 解耦",
              repr(o._pending_warnings))
    finally:
        _orch_mod.append_bounded = orig
        _env_restore(snap)


# ---------------------------------------------------------------- W7

def test_w7_all_switches_off() -> None:
    case = "W7"
    if not _probe(case):
        return
    snap = _env_snapshot()
    on = [k for k in _PROD_SWITCHES if (snap[k] or "").strip() == "1"]
    check(case + "-0 前提：3 生产开关均非 '1'", not on,
          "宿主已开启 %r，前提不成立" % (on,))
    if on:
        return
    orig = retention.append_bounded
    calls: list[int] = []

    def _counting(*a, **k):
        calls.append(1)
        return orig(*a, **k)

    retention.append_bounded = _counting
    try:
        _env_set(TRAVEL_PENDING_CHANNEL=None, TRAVEL_DECISION_SHADOW=None,
                 TRAVEL_DECISION_TAKEOVER=None)
        # 装具自检：先在一个**一次性**实例上写 1 条，确认计数器可达（否则
        # 断言「0 次」可能只是因为装具没接上，属假通过）。该实例随即丢弃，
        # 不参与 W7 的三态断言。
        probe_o = Orchestrator()
        probe_o._warn("W7 装具自检")
        check(case + "-0b 装具自检：计数器可达（1 次）", len(calls) == 1,
              "实测 %d" % len(calls))
        base = len(calls)          # 不作 clear，保留计数供相对断言

        o = Orchestrator()
        check(case + "-1 新实例三态为初值",
              o._pending_warnings == [] and
              o._pending_warnings_stats is None and
              o._pending_warnings_dead is False,
              repr((o._pending_warnings, o._pending_warnings_stats,
                    o._pending_warnings_dead)))
        gate = o._pending_gate(SimpleNamespace(), "hi", None)
        produce = o._pending_produce(SimpleNamespace(), "hi", None)
        check(case + "-2 两钩子首行早退（返回 None）",
              gate is None and produce is None, repr((gate, produce)))
        check(case + "-3 三态不变",
              o._pending_warnings == [] and
              o._pending_warnings_stats is None and
              o._pending_warnings_dead is False,
              repr((o._pending_warnings, o._pending_warnings_stats,
                    o._pending_warnings_dead)))
        check(case + "-4 append_bounded 调用 0 次（相对装具自检后的基线）",
              len(calls) == base,
              "自检后基线 %d / 现值 %d" % (base, len(calls)))
    finally:
        retention.append_bounded = orig
        _env_restore(snap)
    now = _env_snapshot()
    check(case + "-5 finally 后 5 键逐键复原", now == snap,
          "快照 %r / 现值 %r" % (snap, now))


# ---------------------------------------------------------------- W8 / W9

GATE_BYPASS = "pending gate bypass: session 身份未注入（pending_session_id 缺失）"
PRODUCE_BYPASS = ("pending produce bypass: session 身份未注入"
                  "（pending_session_id 缺失）")


def test_w8_reachability() -> None:
    case = "W8"
    if not _probe(case):
        return
    snap = _env_snapshot()
    try:
        _env_set(TRAVEL_PENDING_CHANNEL="1")
        o = Orchestrator()
        session = SimpleNamespace()
        o._pending_gate(session, "hi", None)
        o._pending_produce(session, "hi", None)
        items = o._pending_warnings
        st = o._pending_warnings_stats
        check(case + "-1 两站点各写 1 条（共 2）", len(items) == 2, repr(items))
        check(case + "-2 文案含 '身份'",
              all(isinstance(x, str) and "身份" in x for x in items),
              repr(items))
        check(case + "-3 互不覆盖（gate / produce 各一）",
              items[:1] == [GATE_BYPASS] and items[1:] == [PRODUCE_BYPASS],
              repr(items))
        check(case + "-4 单条长度 <= 300",
              all(len(x) <= 300 for x in items),
              repr([len(x) for x in items]))
        check(case + "-5 total_appended 递增到 2",
              st is not None and st.total_appended == 2,
              repr(None if st is None else st.total_appended))
    finally:
        _env_restore(snap)


def test_w9_capacity_pressure() -> None:
    case = "W9"
    if not _probe(case):
        return
    snap = _env_snapshot()
    try:
        _env_set(TRAVEL_PENDING_CHANNEL="1")
        o = Orchestrator()
        session = SimpleNamespace()
        for _ in range(60):
            o._pending_gate(session, "hi", None)
            o._pending_produce(session, "hi", None)
        items = o._pending_warnings
        st = o._pending_warnings_stats
        check(case + "-1 len == 50", len(items) == 50, "实测 %d" % len(items))
        check(case + "-2 total_appended == 120",
              st is not None and st.total_appended == 120,
              repr(None if st is None else st.total_appended))
        check(case + "-3 total_evicted == 70",
              st is not None and st.total_evicted == 70,
              repr(None if st is None else st.total_evicted))
        # 交错写入：奇数次 = gate，偶数次 = produce；保留 71..120
        check(case + "-4 首条 == 第 71 次写入（gate）",
              bool(items) and items[0] == GATE_BYPASS, repr(items[:1]))
        check(case + "-5 末条 == 第 120 次写入（produce）",
              bool(items) and items[-1] == PRODUCE_BYPASS, repr(items[-1:]))
        check(case + "-6 保留段 gate/produce 各 25 条",
              items.count(GATE_BYPASS) == 25 and
              items.count(PRODUCE_BYPASS) == 25,
              "gate=%d produce=%d" % (items.count(GATE_BYPASS),
                                      items.count(PRODUCE_BYPASS)))
    finally:
        _env_restore(snap)


# ---------------------------------------------------------------- W10

def test_w10_non_rie() -> None:
    case = "W10"
    if not _probe(case):
        return
    orig = _orch_mod.coerce_text

    def _boom(_v, _lim):
        raise ValueError("coerce boom")

    o = Orchestrator()
    _orch_mod.coerce_text = _boom
    escaped = None
    try:
        o._warn("x")
    except BaseException as exc:
        escaped = exc
    finally:
        _orch_mod.coerce_text = orig
    check(case + "-1 非 RIE 不外抛", escaped is None, repr(escaped))
    check(case + "-2 容器不变（len == 0）", o._pending_warnings == [],
          repr(o._pending_warnings))
    check(case + "-3 stats 不提交（None）",
          o._pending_warnings_stats is None, repr(o._pending_warnings_stats))
    check(case + "-4 dead is False（D1：非 RIE 不置 dead）",
          o._pending_warnings_dead is False, repr(o._pending_warnings_dead))
    o._warn("y")
    check(case + "-5 恢复后可正常写入", o._pending_warnings == ["y"] and
          o._pending_warnings_dead is False,
          repr((o._pending_warnings, o._pending_warnings_dead)))


# ---------------------------------------------------------------- main

def main() -> int:
    test_w1_static_channel()
    test_w2_capacity_fifo()
    test_w3_truncation()
    test_w4_type_coercion()
    test_w5_rie_dead()
    test_w6_s1_decoupling()
    test_w7_all_switches_off()
    test_w8_reachability()
    test_w9_capacity_pressure()
    test_w10_non_rie()

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = total - passed
    print("\npending_warnings (R-01-I-4): %d pass / %d fail" % (passed, failed))
    if failed:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAILED: %s  %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
