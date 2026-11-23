"""R-01-I-1 有界追加与裁剪 helper 单元测试（独立脚本直跑）。

运行：python tests/test_retention.py

覆盖 I-1 范围（R-01-I v5 + I-1 授权文本补充）：

  T01-T05 容量行为与淘汰计数（未满 / 恰好满 / 超 1 / 连续 300 / 精确计数）
  T06     两组 (records, stats) 互不干扰
  T08/T30 长度常量生效（clip_text / coerce_text / clip_str_list / statuses 双层）
  T09     append 失败 → RIE("mutation")，计数与容器不增
  T10     外部串行锁约束下的并发调用兼容性（**非** helper 线程安全证明）
  T29     每次成功操作后 current_size == len(records)
  T33     evidence 类型白名单（不调用 repr()/str()，探针计数 0）
  T34     键规则（非 str 丢弃 / 截断冲突 ~2 / 根层预算标记预占改名）
  T35     数值边界（big-int / non-finite-float / bool 保真）
  T36     深度与列表（depth2 展开 / depth3 摘要 / 交错嵌套 / 根非 dict）
          + 深度摘要受 EVID_SUMMARY_MAX 约束（常量置小验证生效）
  T38     new_stats 的 current_size 取真实 len、capacity 透传
  T41     节点计数与预算终止（嵌套耗尽 / 根层耗尽 / 根满 16 键让位）
  T44(a)  前置区全分支：a1 非 list 可取长度 / a2 非 list 不可取 / a3 __len__ 抛
          / a3b list 子类 __len__ 抛（边界补充）/ a4 capacity<1
          / a5 size-drift + over-capacity / a6 bad-stats
          / a7 capacity 合法但 current_size 缺失（单次读取）/ a8 current_size 类型非法
  T44(b1) 删除部分元素后抛（不回滚）
  T44(b2) 删除成功、追加失败（不回滚）
  T44(c)  提交区失败（只断言 RIE 现场属性）
  T44(d1) M 区原异常 __str__ 抛 → 仍为 RIE，且未调用原异常 __str__
  T44(d2) C 区原异常 __str__ 抛 → 同上

**不覆盖**（归 I-2~I-5，本文件不得声称通过）：T07、T11-T28、T31、T32、
T37、T39、T40、T42、T43、T45。

纪律：T10 只证明"调用方持同一把锁时的兼容性"，不证明 helper 自身线程安全；
T44(b1)/(b2) 不断言失败后容器恢复；T44(c) 不把部分写入的 stats 解释为完整统计。
"""

from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import travel_agent.retention as nw      # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("　" + detail) if detail and not cond else ""))


def state(st) -> tuple:
    """(capacity, current_size, total_appended, total_evicted)。"""
    return (st.capacity, st.current_size, st.total_appended, st.total_evicted)


def catch(fn, *args):
    """执行并返回异常对象；无异常返回 None。"""
    try:
        fn(*args)
    except BaseException as exc:       # 测试侧宽捕获，逐项断言类型
        return exc
    return None


def node_count(obj) -> int:
    """按冻结计数规则统计节点：dict 键值对 1 / list 元素 1 / 容器 0。"""
    if isinstance(obj, dict):
        return sum(1 + node_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(1 + node_count(v) for v in obj)
    return 0


# ---------------------------------------------------------------- T01-T05

def test_capacity_core() -> None:
    items = [{"i": i} for i in range(300)]

    recs: list = []
    st = nw.new_stats(recs, 5)
    rets = [nw.append_bounded(recs, items[i], st) for i in range(3)]
    check("T01 未满：len/三计数/返回值",
          (len(recs), state(st), rets) == (3, (5, 3, 3, 0), [0, 0, 0]),
          repr((len(recs), state(st), rets)))

    recs = []
    st = nw.new_stats(recs, 5)
    rets = [nw.append_bounded(recs, items[i], st) for i in range(5)]
    check("T02 恰好满：无淘汰",
          (len(recs), state(st), rets) == (5, (5, 5, 5, 0), [0, 0, 0, 0, 0]),
          repr((len(recs), state(st))))

    recs = []
    st = nw.new_stats(recs, 5)
    for i in range(5):
        nw.append_bounded(recs, items[i], st)
    over = nw.append_bounded(recs, items[5], st)
    check("T03 超 1 条：返回 1，淘汰最旧",
          (over, len(recs), state(st), recs[0] is items[1],
           recs[-1] is items[5]) == (1, 5, (5, 5, 6, 1), True, True),
          repr((over, len(recs), state(st))))

    recs = []
    st = nw.new_stats(recs, 100)
    sizes_ok = True
    for i in range(300):
        nw.append_bounded(recs, items[i], st)
        if st.current_size != len(recs):        # T29 逐步复验
            sizes_ok = False
    check("T04 连续 300 条：容量 100 / 计数 300 / 淘汰 200",
          (len(recs), state(st)) == (100, (100, 100, 300, 200)),
          repr((len(recs), state(st))))
    check("T04 保留最新 100 条（首尾身份）",
          recs[0] is items[200] and recs[-1] is items[299])
    check("T29 每次成功后 current_size == len(records)", sizes_ok)

    recs = []
    st = nw.new_stats(recs, 3)
    for i in range(10):
        nw.append_bounded(recs, items[i], st)
    check("T05 淘汰计数精确（cap3×10 → 淘汰 7）",
          (state(st), [r["i"] for r in recs]) == ((3, 3, 10, 7), [7, 8, 9]),
          repr((state(st), [r["i"] for r in recs])))


# ---------------------------------------------------------------- T06

def test_independent_pairs() -> None:
    a: list = []
    b: list = []
    sa = nw.new_stats(a, 3)
    sb = nw.new_stats(b, 5)
    for i in range(4):
        nw.append_bounded(a, i, sa)
    for i in range(2):
        nw.append_bounded(b, i, sb)
    check("T06 两组互不干扰",
          (state(sa), state(sb), len(a), len(b)) ==
          ((3, 3, 4, 1), (5, 2, 2, 0), 3, 2),
          repr((state(sa), state(sb))))


# ---------------------------------------------------------------- T08/T30

def test_length_limits() -> None:
    check("T08 clip_text None → None", nw.clip_text(None, 10) is None)
    check("T08 clip_text 边界（limit-1/limit/limit+1）",
          [len(nw.clip_text("x" * n, 10)) for n in (9, 10, 11)] == [9, 10, 10])
    check("T08 clip_text limit=0 → 空串", nw.clip_text("abc", 0) == "")
    sentinel = object()
    check("T08 clip_text 非 str 原样返回（同一对象）",
          nw.clip_text(sentinel, 5) is sentinel)

    check("T08 coerce_text None → None", nw.coerce_text(None, 64) is None)
    check("T08 coerce_text str 截断",
          nw.coerce_text("y" * 70, nw.LIM_ID) == "y" * nw.LIM_ID)
    check("T08 coerce_text 非 str → 固定占位",
          nw.coerce_text(123, 64) == "<non-str:int>")
    long_name = type("N" * 40, (), {})
    check("T08 coerce_text 类名截 32",
          nw.coerce_text(long_name(), 100) == "<non-str:%s>" % ("N" * 32),
          nw.coerce_text(long_name(), 100))
    check("T08 coerce_text 占位自身受 limit 约束",
          nw.coerce_text(1, 4) == "<non")

    long_status = "s" * 50
    check("T30 statuses 双层限制（12×50 → 8×32）",
          nw.clip_str_list([long_status] * 12,
                           nw.STATUSES_MAX_ITEMS,
                           nw.STATUS_ITEM_MAX) == ["s" * 32] * 8)
    check("T30 statuses 非 str 元素走占位",
          nw.clip_str_list([1, "ok"], 8, 32) == ["<non-str:int>", "ok"])
    check("T30 clip_str_list None → None",
          nw.clip_str_list(None, 8, 32) is None)
    check("T30 clip_str_list 非 list → 单元素有界列表",
          nw.clip_str_list("abc", 8, 32) == ["abc"])


# ---------------------------------------------------------------- T09

class _BoomAppend(list):
    def append(self, item):
        raise RuntimeError("boom-append")


def test_append_raises() -> None:
    recs = _BoomAppend()
    st = nw.new_stats(recs, 10)          # cap 10 → overflow 为负，不触发 del
    exc = catch(nw.append_bounded, recs, "r", st)
    check("T09 append 抛 → RIE(mutation)，容器/计数零变化",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "mutation"
          and str(exc).startswith("append_bounded[mutation]")
          and len(recs) == 0 and exc.size == 0 and state(st) == (10, 0, 0, 0),
          repr((type(exc).__name__, getattr(exc, "stage", None),
                getattr(exc, "size", None), state(st))))


# ---------------------------------------------------------------- T10

def test_concurrent_under_lock() -> None:
    """外部串行锁约束下的兼容性：**不**证明 helper 自身线程安全。"""
    recs: list = []
    st = nw.new_stats(recs, 20)
    lock = threading.Lock()
    errors: list = []

    def worker(tid: int) -> None:
        for i in range(50):
            try:
                with lock:
                    nw.append_bounded(recs, (tid, i), st)
            except Exception as exc:      # noqa: BLE001 - 测试收集
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("T10 外部锁下：无异常 / 容量 20 / 计数 200 / 淘汰 180",
          (errors, len(recs), state(st)) == ([], 20, (20, 20, 200, 180)),
          repr((errors, len(recs), state(st))))
    check("T10 无重复追加（20 个元素互不相同）",
          len(set(recs)) == 20)


# ---------------------------------------------------------------- T33

class _Probe:
    calls: list = []

    def __repr__(self):
        _Probe.calls.append("repr")
        return "REPR"

    def __str__(self):
        _Probe.calls.append("str")
        return "STR"

    def __iter__(self):
        _Probe.calls.append("iter")
        return iter(())


def test_evidence_whitelist() -> None:
    probe = _Probe()
    out = nw.clip_evidence({"t": (1, 2), "s": {1, 2}, "b": b"xy", "o": probe})
    check("T33 非白名单容器 → 固定占位",
          (out["t"], out["s"], out["b"]) ==
          ("<unsupported:tuple>", "<unsupported:set>",
           "<unsupported:bytes>"),
          repr(out))
    check("T33 自定义对象 → 固定占位", out["o"] == "<unsupported:_Probe>")
    check("T33 未调用 repr()/str()/__iter__", _Probe.calls == [],
          repr(_Probe.calls))


# ---------------------------------------------------------------- T34

def test_evidence_keys() -> None:
    k64 = "k" * 64
    out = nw.clip_evidence({k64: 1, k64 + "tail": 2, 7: "drop", "ok": 3})
    want_dup = "k" * 62 + "~2"
    check("T34 非 str 键整条丢弃（值不残留）",
          "drop" not in out.values() and all(isinstance(k, str)
                                             for k in out))
    check("T34 键截断冲突 → ~2 且不覆盖已有键",
          (out.get(k64), out.get(want_dup), len(out)) == (1, 2, 3),
          repr(out))
    check("T34 键长全部 ≤64", all(len(k) <= 64 for k in out))

    out = nw.clip_evidence({nw.EVID_BUDGET_MARK: "biz", "x": 1})
    check("T34 根层预算标记预占：业务同名键改名 ~2 并保值",
          out == {"<budget-exhausted>~2": "biz", "x": 1}, repr(out))
    check("T34 未耗尽时不得出现预算标记", nw.EVID_BUDGET_MARK not in out)


# ---------------------------------------------------------------- T35

def test_evidence_numbers() -> None:
    out = nw.clip_evidence({
        "big": 2 ** 64,
        "edge": 2 ** 63 - 1,
        "neg": -(2 ** 63),
        "nan": float("nan"),
        "inf": float("inf"),
        "t": True,
        "f": False,
        "fl": 1.5,
    })
    check("T35 超界 int → <big-int>（含 -2**63）",
          (out["big"], out["neg"]) == ("<big-int>", "<big-int>"), repr(out))
    check("T35 2**63-1 原样保留", out["edge"] == 2 ** 63 - 1)
    check("T35 nan/inf → <non-finite-float>",
          (out["nan"], out["inf"]) ==
          ("<non-finite-float>", "<non-finite-float>"))
    check("T35 bool 保真（不入 1/0）",
          out["t"] is True and out["f"] is False)
    check("T35 有限 float 原样", out["fl"] == 1.5 and isinstance(out["fl"],
                                                                float))


# ---------------------------------------------------------------- T36

def test_evidence_depth() -> None:
    check("T36 depth2 展开（根+2 层）",
          nw.clip_evidence({"a": {"b": {"c": 1}}}) ==
          {"a": {"b": {"c": 1}}})
    check("T36 depth3 dict → \"<dict>\" 摘要",
          nw.clip_evidence({"a": {"b": {"c": {"d": 1}}}}) ==
          {"a": {"b": {"c": "<dict>"}}},
          repr(nw.clip_evidence({"a": {"b": {"c": {"d": 1}}}})))
    check("T36 depth3 list → \"<list:N>\" 摘要",
          nw.clip_evidence({"a": {"b": {"c": [1, 2]}}}) ==
          {"a": {"b": {"c": "<list:2>"}}})
    check("T36 dict↔list 交错嵌套（depth 与列表规则同一路径）",
          nw.clip_evidence({"a": [{"b": [1, 2]}]}) ==
          {"a": [{"b": "<list:2>"}]},
          repr(nw.clip_evidence({"a": [{"b": [1, 2]}]})))
    check("T36 列表 >16 截断", nw.clip_evidence({"a": list(range(20))}) ==
          {"a": list(range(16))})
    check("T36 根非 dict → 占位映射",
          nw.clip_evidence([1, 2]) == {"_unsupported": "non-mapping"}
          and nw.clip_evidence(None) == {"_unsupported": "non-mapping"})
    check("T36 6 节点基准用例（容器不计数）",
          nw.clip_evidence({"a": [{"x": 1, "y": 2}, {"z": 3}]}) ==
          {"a": [{"x": 1, "y": 2}, {"z": 3}]}
          and node_count({"a": [{"x": 1, "y": 2}, {"z": 3}]}) == 6)

    # 深度摘要必须真正受 EVID_SUMMARY_MAX 约束（临时改小常量验证生效）
    saved = nw.EVID_SUMMARY_MAX
    try:
        nw.EVID_SUMMARY_MAX = 3
        got_dict = nw.clip_evidence({"a": {"b": {"c": {"d": 1}}}})
        got_list = nw.clip_evidence({"a": {"b": {"c": [1, 2]}}})
    finally:
        nw.EVID_SUMMARY_MAX = saved
    check("T36 深度摘要受 EVID_SUMMARY_MAX 裁剪（常量置 3 时截为 3 字符）",
          got_dict == {"a": {"b": {"c": "<di"}}}
          and got_list == {"a": {"b": {"c": "<li"}}},
          repr((got_dict, got_list)))
    check("T36 EVID_SUMMARY_MAX 复原后默认摘要不变",
          nw.EVID_SUMMARY_MAX == saved
          and nw.clip_evidence({"a": {"b": {"c": {"d": 1}}}}) ==
          {"a": {"b": {"c": "<dict>"}}})


# ---------------------------------------------------------------- T38

def test_new_stats() -> None:
    empty: list = []
    st = nw.new_stats(empty, nw.SHADOW_CAPACITY)
    check("T38 空容器：capacity 常量 / current_size 0 / 计数 0",
          isinstance(st, nw.RetentionStats)
          and state(st) == (100, 0, 0, 0), repr(state(st)))
    recs = [1, 2, 3]
    st2 = nw.new_stats(recs, 5)
    check("T38 current_size 取真实 len、capacity 透传",
          state(st2) == (5, 3, 0, 0) and st2 is not st,
          repr(state(st2)))
    check("T38 四字段齐备（无第五字段）",
          sorted(f for f in st.__dataclass_fields__) ==
          ["capacity", "current_size", "total_appended", "total_evicted"])


# ---------------------------------------------------------------- T41

def _wide(k: int) -> dict:
    return {"a": {"i%d" % i: {"j%d" % j: 1 for j in range(k)}
                  for i in range(16)}}


def test_evidence_budget() -> None:
    # (a) 1 + 16 + 16*6 = 113 节点：不耗尽
    out = nw.clip_evidence(_wide(6))
    check("T41 113 节点不触发预算（无标记、结构完整）",
          nw.EVID_BUDGET_MARK not in out and node_count(out) == 113
          and out == _wide(6), repr(node_count(out)))

    # (b) 1 + 16 + 16*7 = 129 节点：第 129 个节点前终止，沿栈传到根
    out = nw.clip_evidence(_wide(7))
    marks = []

    def collect_marks(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == nw.EVID_BUDGET_MARK:
                    marks.append(k)
                collect_marks(v)
        elif isinstance(obj, list):
            for v in obj:
                collect_marks(v)

    collect_marks(out)
    check("T41 预算耗尽：根仍为 dict、含唯一标记且为末键",
          isinstance(out, dict) and marks == [nw.EVID_BUDGET_MARK]
          and list(out)[-1] == nw.EVID_BUDGET_MARK, repr(list(out)))
    check("T41 业务节点恰为 128（标记不占预算）",
          node_count(out) - 1 == 128, repr(node_count(out)))
    check("T41 耗尽处嵌套容器保留已构建部分（不替换为字符串）",
          isinstance(out["a"], dict) and len(out["a"]) == 16
          and len(out["a"]["i0"]) == 7 and len(out["a"]["i15"]) == 6,
          repr({k: len(v) for k, v in list(out["a"].items())[:1]}))

    # (c) 根满 16 键时耗尽：让出键位（末位丢弃），根仍 ≤16 键
    root_full = {("k%d" % i): ({"s0": 1} if i < 15 else
                               {"i%d" % n: {"j%d" % m: 1
                                            for m in range(6)}
                                for n in range(16)})
                 for i in range(16)}
    out = nw.clip_evidence(root_full)
    check("T41 根满 16 键 + 耗尽：丢弃末位业务键后插入标记",
          len(out) == 16 and "k15" not in out and "k14" in out
          and out.get(nw.EVID_BUDGET_MARK) is True, repr(list(out)))
    check("T41 根满案例：让位丢弃的子树不保留，输出业务节点 == 30",
          node_count(out) - 1 == 30, repr(node_count(out)))
    check("T41 根满案例：输出业务节点 ≤128（预算守恒上限）",
          node_count(out) - 1 <= 128)


# ---------------------------------------------------------------- T44(a)

class _BoomLenNonList:
    def __init__(self):
        self.len_calls = 0
        self.other_calls = 0

    def __len__(self):
        self.len_calls += 1
        raise TypeError("no len")

    def __repr__(self):
        self.other_calls += 1
        return "X"

    def __str__(self):
        self.other_calls += 1
        return "X"

    def __iter__(self):
        self.other_calls += 1
        return iter(())


class _BoomLenList(list):
    def __len__(self):
        raise TypeError("no len")


class _StatsNoCurrentSize:
    """capacity 合法、current_size 缺失；记录缺失属性的访问次数。"""

    def __init__(self):
        self.capacity = 5
        self.total_appended = 0
        self.total_evicted = 0
        self.reads = []

    def __getattr__(self, name):
        self.reads.append(name)      # 仅缺失属性会走到这里
        raise AttributeError(name)


class _StatsBadCurrentSize:
    """capacity 合法、current_size 类型非法（bool / str / float）。"""

    def __init__(self, current_size):
        self.capacity = 5
        self.current_size = current_size
        self.total_appended = 0
        self.total_evicted = 0


def test_precondition_branches() -> None:
    st = nw.new_stats([], 5)
    tup = ("x", "y", "z")
    exc = catch(nw.append_bounded, tup, "r", st)
    check("T44(a1) 非 list（长度可取）→ size 为实测 3，stats 零变更",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "not-list" in str(exc)
          and exc.size == 3 and tup == ("x", "y", "z")
          and state(st) == (5, 0, 0, 0),
          repr((getattr(exc, "size", None), state(st))))

    sentinel = object()
    exc = catch(nw.append_bounded, sentinel, "r", st)
    check("T44(a2) 非 list（无 len）→ size is None（不得为 0）",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "not-list" in str(exc)
          and exc.size is None and state(st) == (5, 0, 0, 0),
          repr(getattr(exc, "size", "missing")))

    probe = _BoomLenNonList()
    exc = catch(nw.append_bounded, probe, "r", st)
    check("T44(a3) __len__ 抛 → size None / __len__ 恰 1 次 / repr·str·iter 0 次",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and exc.size is None
          and probe.len_calls == 1 and probe.other_calls == 0,
          repr((getattr(exc, "size", None), probe.len_calls,
                probe.other_calls)))

    boom_list = _BoomLenList()
    st_b = nw.RetentionStats(capacity=5, current_size=0)
    exc = catch(nw.append_bounded, boom_list, "r", st_b)
    check("T44(a3b) list 子类 __len__ 抛 → len-unavailable，容器/stats 不变",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition"
          and "len-unavailable" in str(exc) and exc.size is None
          and len(list.copy(boom_list)) == 0
          and state(st_b) == (5, 0, 0, 0),
          repr((getattr(exc, "size", None), str(exc)[:60])))

    o1, o2 = object(), object()
    recs = [o1, o2]
    st0 = nw.RetentionStats(capacity=0, current_size=2)
    exc = catch(nw.append_bounded, recs, "r", st0)
    check("T44(a4) capacity<1 → size 实测 2，元素身份/顺序不变，stats 零变更",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "capacity<1" in str(exc)
          and exc.size == 2 and recs[0] is o1 and recs[1] is o2
          and state(st0) == (0, 2, 0, 0),
          repr((getattr(exc, "size", None), state(st0))))

    st1 = nw.RetentionStats(capacity=5, current_size=1)
    exc = catch(nw.append_bounded, recs, "r", st1)
    check("T44(a5) size-drift → RIE，容器元素身份/顺序不变",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "size-drift" in str(exc)
          and exc.size == 2 and recs[0] is o1 and recs[1] is o2
          and state(st1) == (5, 1, 0, 0),
          repr((getattr(exc, "size", None), state(st1))))

    st2 = nw.RetentionStats(capacity=2, current_size=3)
    recs3 = [o1, o2, o1]
    exc = catch(nw.append_bounded, recs3, "r", st2)
    check("T44(a5b) over-capacity → RIE，容器逐项身份/顺序不变",
          isinstance(exc, nw.RetentionInvariantError)
          and "over-capacity" in str(exc) and exc.size == 3
          and len(recs3) == 3
          and recs3[0] is o1 and recs3[1] is o2 and recs3[2] is o1
          and state(st2) == (2, 3, 0, 0),
          repr((getattr(exc, "size", None), state(st2))))

    exc = catch(nw.append_bounded, recs, "r", None)
    check("T44(a6) stats 不可读 → bad-stats，size 仍实测，capacity None",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "bad-stats" in str(exc)
          and exc.size == 2 and exc.capacity is None,
          repr((getattr(exc, "size", None), getattr(exc, "capacity", "x"))))

    # a7/a8：bad-stats 必须覆盖 current_size（不得误报为 size-drift）
    probe = _StatsNoCurrentSize()
    exc = catch(nw.append_bounded, [o1, o2], "r", probe)
    check("T44(a7) capacity 合法/current_size 缺失 → bad-stats（非 size-drift）",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "precondition" and "bad-stats" in str(exc)
          and "size-drift" not in str(exc)
          and exc.size == 2 and exc.capacity == 5,
          repr((str(exc), getattr(exc, "size", None))))
    check("T44(a7) current_size 只被读取 1 次（缓存复用，无重复访问）",
          probe.reads == ["current_size"], repr(probe.reads))

    bad_cases = []
    for bad_cur in (True, "3", 2.0):
        st_bad = _StatsBadCurrentSize(bad_cur)
        e = catch(nw.append_bounded, [o1, o2], "r", st_bad)
        bad_cases.append((bad_cur,
                          isinstance(e, nw.RetentionInvariantError)
                          and e.stage == "precondition"
                          and "bad-stats" in str(e)
                          and "size-drift" not in str(e)
                          and e.size == 2 and e.capacity == 5))
    check("T44(a8) current_size 类型非法（bool/str/float）→ bad-stats",
          all(ok for _v, ok in bad_cases), repr(bad_cases))


# ---------------------------------------------------------------- T44(b)

class _BoomDelOnce(list):
    """先删一个元素再抛：模拟"删除已产生部分副作用"。"""

    def __delitem__(self, key):
        super().__delitem__(0)
        raise RuntimeError("boom-del")


class _BoomAppendFull(list):
    """删除正常、append 抛：模拟"删除成功、追加失败"。"""

    def append(self, item):
        raise RuntimeError("boom-append")


def test_mutation_failures() -> None:
    recs = _BoomDelOnce(["a", "b", "c"])
    st = nw.new_stats(recs, 3)
    exc = catch(nw.append_bounded, recs, "d", st)
    check("T44(b1) 删除部分后抛 → RIE(mutation)，不回滚（len 变 2）",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "mutation" and exc.size == 2
          and list(recs) == ["b", "c"] and state(st) == (3, 3, 0, 0),
          repr((getattr(exc, "size", None), list(recs), state(st))))

    recs = _BoomAppendFull(["a", "b", "c"])
    st = nw.new_stats(recs, 3)
    exc = catch(nw.append_bounded, recs, "d", st)
    check("T44(b2) 删除成功、追加失败 → 旧记录不回滚（len == cap-1）",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "mutation" and exc.size == 2
          and list(recs) == ["b", "c"] and "a" not in list(recs),
          repr((getattr(exc, "size", None), list(recs))))
    check("T44(b2) total_evicted / total_appended 均未增加（冻结快照）",
          state(st) == (3, 3, 0, 0), repr(state(st)))
    check("T44(b2) 无自动恢复（stats 未重置、容器未重建、长度未回填）",
          st.capacity == 3 and st.current_size == 3 and len(recs) == 2)


# ---------------------------------------------------------------- T44(c)

class _BoomStats:
    """capacity/current_size 可读，写 total_evicted 时抛（模拟提交区失败）。"""

    def __init__(self):
        self.capacity = 3
        self.current_size = 0
        self.total_appended = 0
        self._evicted = 0

    @property
    def total_evicted(self):
        return self._evicted

    @total_evicted.setter
    def total_evicted(self, value):
        raise RuntimeError("boom-commit")


def test_commit_failure() -> None:
    recs: list = []
    st = _BoomStats()
    exc = catch(nw.append_bounded, recs, "r", st)
    check("T44(c) 提交区失败 → RIE(commit)，容器已变更不回滚",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "commit" and exc.size == 1
          and list(recs) == ["r"], repr((getattr(exc, "size", None), recs)))
    check("T44(c) 只断言 RIE 现场属性（不把部分写入解释为完整统计）",
          exc.capacity == 3 and exc.total_appended == 0
          and exc.total_evicted == 0 and st.total_appended == 0,
          repr((exc.capacity, exc.total_appended, exc.total_evicted)))


# ---------------------------------------------------------------- T44(d)

class _BoomStr(Exception):
    """__str__ 自身抛错：用于验证 M/C 转换不依赖原异常的 __str__。"""

    calls = 0

    def __str__(self):
        type(self).calls += 1
        raise RuntimeError("boom-str")


class _RaisingAppend(list):
    def append(self, item):
        raise _BoomStr()


class _RaisingCommitStats:
    """capacity/current_size/total_appended 可读，写 total_evicted 抛 _BoomStr。"""

    def __init__(self):
        self.capacity = 3
        self.current_size = 0
        self.total_appended = 0

    @property
    def total_evicted(self):
        return 0

    @total_evicted.setter
    def total_evicted(self, value):
        raise _BoomStr()


def test_exc_str_failure() -> None:
    _BoomStr.calls = 0
    recs = _RaisingAppend()
    st = nw.new_stats(recs, 5)               # cap 5：不触发 del
    exc = catch(nw.append_bounded, recs, "r", st)
    check("T44(d1) M 区原异常 __str__ 抛 → 仍为 RIE(mutation)",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "mutation" and exc.size == 0
          and isinstance(exc.__cause__, _BoomStr),
          repr((type(exc).__name__, getattr(exc, "stage", None))))
    check("T44(d1) 未调用原异常 __str__，RIE 自身可 str()",
          _BoomStr.calls == 0 and str(exc).startswith("append_bounded[mutation]"),
          repr((_BoomStr.calls, str(exc))))

    recs = []
    st = _RaisingCommitStats()
    exc = catch(nw.append_bounded, recs, "r", st)
    check("T44(d2) C 区原异常 __str__ 抛 → 仍为 RIE(commit)",
          isinstance(exc, nw.RetentionInvariantError)
          and exc.stage == "commit" and exc.size == 1
          and isinstance(exc.__cause__, _BoomStr) and list(recs) == ["r"],
          repr((type(exc).__name__, getattr(exc, "stage", None))))
    check("T44(d2) 未调用原异常 __str__，RIE 自身可 str()",
          _BoomStr.calls == 0 and str(exc).startswith("append_bounded[commit]"),
          repr((_BoomStr.calls, str(exc))))


# ---------------------------------------------------------------- main

def main() -> int:
    test_capacity_core()
    test_independent_pairs()
    test_length_limits()
    test_append_raises()
    test_concurrent_under_lock()
    test_evidence_whitelist()
    test_evidence_keys()
    test_evidence_numbers()
    test_evidence_depth()
    test_new_stats()
    test_evidence_budget()
    test_precondition_branches()
    test_mutation_failures()
    test_commit_failure()
    test_exc_str_failure()

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = total - passed
    print("\nretention (R-01-I-1): %d pass / %d fail" % (passed, failed))
    if failed:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAILED: %s  %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
