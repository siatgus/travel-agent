"""R-01 容量治理共享机制：有界追加、追加统计、字段裁剪（纯标准库）。

设计依据：R-01-I 任务书 v5（复审 PASS，代码基线 e042814）。本模块只被
I-2 / I-4 的生产接入引用，自身**不 import 任何 travel_agent 业务模块**。

冻结契约（摘要）：

- ``append_bounded`` 仅接受 ``list``；分 P（前置校验）/ M（变更）/ C（提交）
  三段。P 区零变更；M 区任何异常**不回滚**，统一转 ``RIE("mutation")``；
  C 区异常转 ``RIE("commit")``；对外**只抛** ``RetentionInvariantError``
  （不冒出 TypeError / AttributeError 等），且**不捕获 BaseException**。
- ``RetentionInvariantError.size: int | None``；``None`` 仅表示「无法取得
  真实长度」，禁止用 0 / capacity / 预期值代替；四个现场属性一律实测。
- 六个容器的 ``*_dead`` 失效标记属 owner 职责（I-2），不在本模块。
- ``clip_evidence``：只接受 JSON-like 类型白名单；根对象恒为 dict；节点预算
  128 全局共享，耗尽后沿调用栈向上传播并在**根**插入唯一标记；``"<dict>"`` /
  ``"<list:N>"`` 摘要**仅**由深度限制触发、长度经 ``EVID_SUMMARY_MAX`` 裁剪，
  预算耗尽不走深度摘要。
- ``clip_text`` 是**类型兼容策略**，不是容量保证；声明为 str 的字段必须走
  ``coerce_text``。

边界披露（不在本模块保证范围内）：``type(v).__name__`` 属属性访问，元类
property 的理论边界、自定义 ``__len__`` / ``__iter__`` 的副作用不属保证范围；
``list`` 子类可在抛异常前做任意部分修改，故 M 区失败只声明「容器不可判定」，
不声明「容器未变」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    # 容量
    "SHADOW_CAPACITY", "WARNING_CAPACITY",
    # 字段长度
    "LIM_REASON", "LIM_ENTITY", "LIM_ENTITY_CITY", "LIM_ID",
    "LIM_DETAIL", "LIM_UTTERANCE", "LIM_WARNING",
    "STATUSES_MAX_ITEMS", "STATUS_ITEM_MAX",
    # evidence 结构
    "EVID_MAX_KEYS", "EVID_KEY_MAX", "EVID_STR_MAX", "EVID_LIST_MAX",
    "EVID_DEPTH_MAX", "EVID_SUMMARY_MAX", "EVID_NODE_BUDGET",
    "EVID_BUDGET_MARK", "EVID_UNSUPPORTED_KEY",
    # 机制
    "RetentionInvariantError", "RetentionStats", "new_stats",
    "append_bounded", "clip_text", "coerce_text", "clip_str_list",
    "clip_evidence",
]

# ---------------------------------------------------------------- 冻结常量

SHADOW_CAPACITY = 100          # S1–S5 五个 Shadow 容器（S3 两 schema 共用一池）
WARNING_CAPACITY = 50          # W _pending_warnings

LIM_REASON = 200               # reason（S1/S2/S3）
LIM_ENTITY = 120               # entity_text
LIM_ENTITY_CITY = 64           # entity_city
LIM_ID = 64                    # pending_id / candidate_id 兜底
LIM_DETAIL = 256               # detail（S3 gate / S4 / S5）
LIM_UTTERANCE = 200            # utterance（S3 _v，用户原文；不称脱敏）
LIM_WARNING = 300              # warning 字符串

STATUSES_MAX_ITEMS = 8         # statuses 列表元素数上限
STATUS_ITEM_MAX = 32           # statuses 单项长度上限

EVID_MAX_KEYS = 16             # 每层 dict 最多保留键数
EVID_KEY_MAX = 64              # 键长度上限
EVID_STR_MAX = 200             # 字符串值上限
EVID_LIST_MAX = 16             # 列表元素数上限
EVID_DEPTH_MAX = 2             # 根 depth=0，最多展开到 depth<=2（共三层容器）
EVID_SUMMARY_MAX = 32          # 深度摘要串上限
EVID_NODE_BUDGET = 128         # 全局节点预算（dict 键值对 / list 元素各计 1）

EVID_BUDGET_MARK = "<budget-exhausted>"   # 根层唯一终止标记键
EVID_UNSUPPORTED_KEY = "_unsupported"     # 根入参非 dict 时的占位键

_MAX_CLASS_NAME = 32           # 类名/占位文本中类名的截断长度
_INT_MAX = 2 ** 63 - 1
_MISSING = object()


# ---------------------------------------------------------------- 异常

def _as_int(value):
    """仅接受真正的 int（bool 除外），其余一律 None。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _safe_len(obj):
    """唯一一次 __len__ 调用；失败或非 int → None（禁止用 0 顶替）。"""
    if obj is _MISSING or obj is None:
        return None
    try:
        return _as_int(len(obj))
    except Exception:
        return None


def _safe_getattr(obj, name):
    if obj is _MISSING or obj is None:
        return None
    try:
        return getattr(obj, name)
    except Exception:
        return None


def _exc_label(exc):
    """异常标签：只取类型名，**绝不**调用 ``exc.__str__`` / ``repr()``。

    M/C 转换现场的原始异常可能自定义了会抛错的 ``__str__``；若在此处
    调用它，本该转为 ``RetentionInvariantError`` 的路径会被第二次异常
    覆盖，导致"对外只抛 RIE"的契约出现缝隙。原始异常仍通过
    ``raise ... from exc`` 保留在 ``__cause__`` 中供排查。
    """
    try:
        name = type(exc).__name__
    except Exception:
        return "Exception"
    if not isinstance(name, str) or not name:
        return "Exception"
    return name[:_MAX_CLASS_NAME]


class RetentionInvariantError(RuntimeError):
    """容器与统计无法保持一致；禁止静默修补，禁止继续向该容器追加。

    五属性均为**抛出瞬间实测**读数：

    ``stage``  "precondition" | "mutation" | "commit"
    ``size``   实测 ``len(records)``；无法取得时 None（不得用 0/容量顶替）
    ``capacity`` / ``total_appended`` / ``total_evicted``  实测读数或 None

    reason code（如 ``precondition:not-list``）只出现在 message 中，
    **不**额外暴露属性。
    """

    def __init__(self, code, *, size=_MISSING, records=_MISSING,
                 stats=_MISSING, detail=""):
        self.stage = code.split(":", 1)[0]
        self.size = _safe_len(records) if size is _MISSING else _as_int(size)
        self.capacity = _as_int(_safe_getattr(stats, "capacity"))
        self.total_appended = _as_int(_safe_getattr(stats, "total_appended"))
        self.total_evicted = _as_int(_safe_getattr(stats, "total_evicted"))
        msg = "append_bounded[%s]" % code
        if detail:
            msg = "%s %s" % (msg, detail)
        super().__init__(msg)


@dataclass
class RetentionStats:
    """单个容器的容量与追加统计（四个字段，不增第五个）。"""

    capacity: int
    current_size: int = 0
    total_appended: int = 0
    total_evicted: int = 0


def new_stats(records, capacity):
    """六容器统一工厂：``current_size`` 取自真实 ``len(records)``。"""
    return RetentionStats(capacity=capacity, current_size=len(records))


# ---------------------------------------------------------------- 有界追加

def append_bounded(records, record, stats):
    """向 ``records`` 追加 ``record``，超容量时淘汰最旧，返回淘汰条数。

    未满时返回 0（``total_evicted`` 只累计实际淘汰条数，不累计负 overflow）。
    P 区（零变更）校验顺序固定：not-list → bad-stats（``capacity`` 与
    ``current_size`` 均须为非 bool 的 int）→ capacity<1 → len-unavailable
    → over-capacity → size-drift。M 区失败不回滚，C 区失败不回退；两者都
    抛 ``RetentionInvariantError``，且 M/C 的 detail 只含原异常类型名
    （不调用原异常 ``__str__``）。
    """
    # ---- P：前置校验（零变更，O(1)）----
    size = _safe_len(records)                       # P-0：唯一一次 len
    if not isinstance(records, list):
        raise RetentionInvariantError(
            "precondition:not-list", size=size, stats=stats,
            detail="records 必须是 list（含子类），实际 %r"
                   % type(records).__name__)
    # P-2：capacity 与 current_size 同点读取并各自只读一次（缓存复用，
    # 避免重复访问带副作用的自定义属性）。任一缺失/不可读/非 int（bool
    # 亦非法）都判 bad-stats——不得把 current_size 的缺陷误报为 size-drift。
    cap = _as_int(_safe_getattr(stats, "capacity"))
    cur = _as_int(_safe_getattr(stats, "current_size"))
    if cap is None or cur is None:
        raise RetentionInvariantError(
            "precondition:bad-stats", size=size, stats=stats,
            detail="stats.capacity/current_size 不可读或类型非法")
    if cap < 1:
        raise RetentionInvariantError(
            "precondition:capacity<1", size=size, stats=stats,
            detail="capacity=%d" % cap)
    if size is None:
        raise RetentionInvariantError(
            "precondition:len-unavailable", size=None, stats=stats,
            detail="len(records) 不可用")
    if size > cap:
        raise RetentionInvariantError(
            "precondition:over-capacity", size=size, stats=stats,
            detail="len=%d capacity=%d" % (size, cap))
    if cur != size:
        raise RetentionInvariantError(
            "precondition:size-drift", size=size, stats=stats,
            detail="len=%d stats.current_size=%d" % (size, cur))

    # ---- M：变更区（唯一可能产生副作用的地方）----
    overflow = size + 1 - cap                       # 先算，后改
    evicted = overflow if overflow > 0 else 0       # 未满时淘汰数恒为 0
    try:
        if overflow > 0:
            del records[:overflow]
        records.append(record)
    except Exception as exc:
        raise RetentionInvariantError(
            "mutation", size=_safe_len(records), stats=stats,
            detail=_exc_label(exc)) from exc

    # ---- C：提交区（全部成功后才写计数）----
    try:
        stats.total_evicted += evicted               # 只累计实际淘汰条数
        stats.total_appended += 1
        stats.current_size = len(records)
    except Exception as exc:
        raise RetentionInvariantError(
            "commit", size=_safe_len(records), stats=stats,
            detail=_exc_label(exc)) from exc
    return evicted


# ---------------------------------------------------------------- 字段裁剪

def clip_text(value, limit):
    """类型兼容策略：None→None；str→前缀截断；其他类型**原样返回**。

    本函数**不是**容量保证——声明为 str 的字段必须走 ``coerce_text``。
    """
    if value is None or not isinstance(value, str):
        return value
    limit = 0 if limit < 0 else limit
    return value[:limit] if len(value) > limit else value


def coerce_text(value, limit):
    """str 字段收敛：None→None；str→截断；其他类型→固定占位文本。

    非 str 一律转 ``"<non-str:类名>"``（类名截 32），**不调用** ``repr()``
    或 ``str()``；占位文本本身也受 ``limit`` 约束（生产常量均 ≥64，
    占位 ≤44，实际不会触发二次截断）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        return clip_text(value, limit)
    text = "<non-str:%s>" % type(value).__name__[:_MAX_CLASS_NAME]
    return clip_text(text, limit)


def clip_str_list(values, max_items, item_limit):
    """列表裁剪：保 ``list``；限元素数与单项长度；元素走 ``coerce_text``。

    ``None`` 原样返回；非 list 入参退化为单元素列表的占位（有界且确定）。
    """
    if values is None:
        return None
    if not isinstance(values, list):
        return [coerce_text(values, item_limit)]
    out = []
    for item in values[:max_items]:
        out.append(coerce_text(item, item_limit))
    return out


# ---------------------------------------------------------------- evidence

class _Budget:
    """全局节点预算（dict 键值对 1 个 / list 元素 1 个；容器本身不计数）。"""

    __slots__ = ("used",)

    def __init__(self):
        self.used = 0


def _clip_key(raw, taken):
    """键截断 + 冲突改名（先到先得，后者 ``~2``/``~3``…，总长 ≤ 上限）。"""
    base = raw[:EVID_KEY_MAX] if len(raw) > EVID_KEY_MAX else raw
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = "~%d" % n
        cand = base[:max(0, EVID_KEY_MAX - len(suffix))] + suffix
        if cand not in taken:
            return cand
        n += 1


def _clip_scalar(value):
    """白名单标量；不在白名单的容器/对象由调用方处理。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _INT_MAX else "<big-int>"
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite-float>"
    if isinstance(value, str):
        return value[:EVID_STR_MAX] if len(value) > EVID_STR_MAX else value
    return _MISSING


def _clip_value(value, depth, budget):
    """返回 ``(裁剪后值, 是否耗尽)``；耗尽沿调用栈向上传播。"""
    scalar = _clip_scalar(value)
    if scalar is not _MISSING:
        return scalar, False
    if isinstance(value, dict):
        if depth + 1 > EVID_DEPTH_MAX:
            return clip_text("<dict>", EVID_SUMMARY_MAX), False
        return _clip_mapping(value, depth + 1, budget)
    if isinstance(value, list):
        if depth + 1 > EVID_DEPTH_MAX:
            return clip_text("<list:%d>" % len(value), EVID_SUMMARY_MAX), False
        return _clip_list(value, depth + 1, budget)
    return "<unsupported:%s>" % type(value).__name__[:_MAX_CLASS_NAME], False


def _clip_mapping(src, depth, budget, reserved=()):
    """dict 裁剪：非 str 键整条丢弃；每层最多 16 键；预算耗尽即终止。"""
    out = {}
    taken = set(reserved)
    for raw_key, raw_value in src.items():
        if not isinstance(raw_key, str):
            continue                                  # 非 str 键：整条丢弃
        if len(out) >= EVID_MAX_KEYS:
            break                                     # 键数上限：静默截断
        if budget.used >= EVID_NODE_BUDGET:
            return out, True                          # 预算耗尽：保留已构建部分
        budget.used += 1
        key = _clip_key(raw_key, taken)
        taken.add(key)
        value, exhausted = _clip_value(raw_value, depth, budget)
        out[key] = value
        if exhausted:
            return out, True
    return out, False


def _clip_list(src, depth, budget):
    out = []
    for item in src:
        if len(out) >= EVID_LIST_MAX:
            break
        if budget.used >= EVID_NODE_BUDGET:
            return out, True
        budget.used += 1
        value, exhausted = _clip_value(item, depth, budget)
        out.append(value)
        if exhausted:
            return out, True
    return out, False


def clip_evidence(mapping):
    """evidence 递归裁剪：根恒为 dict，预算耗尽时在根插入唯一终止标记。"""
    if not isinstance(mapping, dict):
        return {EVID_UNSUPPORTED_KEY: "non-mapping"}
    budget = _Budget()
    out, exhausted = _clip_mapping(
        mapping, 0, budget, reserved=(EVID_BUDGET_MARK,))
    if exhausted:
        if len(out) >= EVID_MAX_KEYS:
            del out[list(out)[-1]]                    # 让出键位：末位丢弃
        out[EVID_BUDGET_MARK] = True                  # 不占业务节点预算
    return out
