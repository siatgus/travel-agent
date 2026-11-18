"""EntityResolver：实体存在性验证与消歧（Phase 4，影子运行，2026-11-18）。

第三阶段的 CandidateExtractor 回答「用户说了什么、有哪些候选」；本模块
回答「**候选究竟是什么、是否真实存在、属于哪个城市、能否进入行程**」。
逐候选独立验证，产出四档分流（对齐验收流程图）：

  confirmed   高置信度：唯一匹配、城市与类型一致 → 允许进入后续流程
  ambiguous   中置信度：多候选/跨城/类型不符 → 要求用户消歧
  rejected    低置信度：在线探测核实不存在 → 不进行程，保留原始表达
  unverified  未验证：探测不可用/未执行 → 记录候选，等待后续验证

红线（验收明确要求）：
- 本模块对 KB **只读**，绝不调用 autofetch.ensure_spot（写库）；
  未验证候选绝不进入 .kb.db。
- 在线探测失败 → probe_status="unavailable"，**绝不解释成 not_found**
  （网络故障 ≠ 用户输入错误）。unavailable 一律落 unverified。
- 名称匹配成功 ≠ 直接加入行程——本阶段是影子运行，结果只记录。
- 不用 LLM 判断实体存在（第五阶段）；不修 DEP-001（阶段边界）。

matched 语义延续第三阶段：matched=True 只说明词典命中，不保证归属城市/
类型/就是用户想要的；matched=False 不等于不存在——由在线探测补证。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import kb as kb_mod
from .real_tools import POIS_DB, resolve_geo

# ---- 四档分流状态（对齐验收流程图） ----
STATUS_CONFIRMED = "confirmed"    # 高置信度：唯一匹配，允许进入后续流程
STATUS_AMBIGUOUS = "ambiguous"   # 中置信度：多候选/跨城/类型不符，要求用户消歧
STATUS_REJECTED = "rejected"      # 低置信度：已核实不存在，不进行程（保留原始表达）
STATUS_UNVERIFIED = "unverified"  # 未验证：探测不可用/未执行，记录候选等待后续验证
STATUS_SKIPPED = "skipped"        # 非实体（category/city），只记录不验证

# ---- 探测结论（与 status 正交；v3.2.1 §8 冻结枚举） ----
PROBE_NOT_APPLICABLE = "not_applicable"  # KB 命中/品类词，无需探测
PROBE_NOT_REQUESTED = "not_requested"    # 策略关闭（KB-only 影子），未执行
PROBE_SUCCESS_HIT = "success_hit"        # 在线探测命中
PROBE_NOT_FOUND = "not_found"            # 探测成功执行且规定范围内无匹配
PROBE_UNAVAILABLE = "unavailable"        # 探测没做成（网络/Key/熔断/无坐标/超限）

# ---- probe_reason（v3.2.1 §8 冻结：unavailable 必带其一；
#      data_source_empty/cross_source_empty 仅配 not_found） ----
REASON_NETWORK_TIMEOUT = "network_timeout"
REASON_NO_KEY = "no_key"
REASON_CIRCUIT_OPEN = "circuit_open"
REASON_NO_CITY_CENTER = "no_city_center"
REASON_QUOTA_EXCEEDED = "quota_exceeded"
REASON_DISABLED_BY_POLICY = "disabled_by_policy"
REASON_DATA_SOURCE_EMPTY = "data_source_empty"      # 单源空（保守 → unverified）
REASON_CROSS_SOURCE_EMPTY = "cross_source_empty"    # 双源复核均空（→ rejected）

# ---- 逐源证据明细 outcome（v3.2.1 §8 冻结） ----
OUTCOME_HIT = "success_hit"
OUTCOME_EMPTY = "success_empty"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"

# ---- 双源复核（v3.2.1 §1.2：主源高德 + 复核源维基导游） ----
MAIN_SOURCE = "amap"
REVIEW_SOURCE = "wikivoyage"
_REVIEW_APPLICABLE_TYPES = {"attraction"}   # 复核源类型适用范围：仅景点
REASON_TYPE_NOT_COVERED = "type_not_covered"
REASON_NOT_CONFIGURED = "not_configured"    # 探测注入模式下复核源未提供

# 置信度基线与阈值（规则打分，无 LLM）
_CONF_KB_EXACT = 0.92
_CONF_KB_PARTIAL = 0.82
_CONF_ONLINE_EXACT = 0.80
_CONF_ONLINE_PARTIAL = 0.72
_CONF_NO_CITY_CTX = 0.12           # 无上下文城市可比对时的扣减
_CONF_TYPE_MISMATCH = 0.25         # 类型不符扣减，且强制 ambiguous
_CONF_CONFIRM_MIN = 0.75

_TYPE_CHECK_MAP = {"attraction": "attraction", "restaurant": "restaurant",
                   "hotel": "hotel"}

AMBIG_REASON_MULTI_CITY = "multi_city"
AMBIG_REASON_BRANCH = "branch_same_name"
AMBIG_REASON_CROSS_CITY = "cross_city_mismatch"
AMBIG_REASON_MULTI_ONLINE = "multi_online"
AMBIG_REASON_TYPE = "type_mismatch"


@dataclass
class MatchRow:
    """一条 KB/在线命中记录。分店与跨城记录全量保留，绝不静默合并。"""

    city: str
    name: str
    category: str
    area: str = ""
    source: str = ""
    origin: str = "kb"               # kb / online

    def as_dict(self) -> dict:
        return {"city": self.city, "name": self.name,
                "category": self.category, "area": self.area,
                "source": self.source, "origin": self.origin}


@dataclass
class ResolvedCandidate:
    """一个候选的验证结论。original 保留第三阶段候选 dict 原样。"""

    original: dict
    status: str
    confidence: float
    probe_status: str
    probe_reason: str = ""           # REASON_*；与 probe_status 组合受 §8 约束
    probe_source: str = ""           # 证据来源：kb / amap / wikivoyage
    probe_results: list[dict] = field(default_factory=list)
    #   逐源明细 [{source, outcome: success_hit|success_empty|failed|skipped, reason}]
    matches: list[MatchRow] = field(default_factory=list)
    city_check: str = "unknown"      # match / mismatch / unknown（无上下文可比对）
    type_check: str = "unknown"      # match / mismatch / unknown
    ambiguity_reason: str = ""       # AMBIG_REASON_*；仅 ambiguous 有值
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.original.get("text") or ""

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "span_start": self.original.get("span_start"),
            "span_end": self.original.get("span_end"),
            "entity_type": self.original.get("entity_type"),
            "role": self.original.get("role"),
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "probe_status": self.probe_status,
            "probe_reason": self.probe_reason,
            "probe_source": self.probe_source,
            "probe_results": [dict(p) for p in self.probe_results],
            "matches": [m.as_dict() for m in self.matches],
            "city_check": self.city_check,
            "type_check": self.type_check,
            "ambiguity_reason": self.ambiguity_reason,
            "notes": list(self.notes),
        }


@dataclass
class ResolveResult:
    resolved: list[ResolvedCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_status(self, status: str) -> list[ResolvedCandidate]:
        return [r for r in self.resolved if r.status == status]


# ---------------------------------------------------------------- KB 索引


def _default_kb_rows() -> list[tuple[str, dict]]:
    """KB 全量行 [(city, row)]：静态种子 + KB 数据库（只读）。"""
    rows: list[tuple[str, dict]] = []
    for city, pois in POIS_DB.items():
        for p in pois:
            rows.append((city, p))
    try:
        db = kb_mod.get_kb()
        if db is not None:
            for city in (db.cities() or []):
                for p in (db.list_pois(city) or []):
                    rows.append((city, p))
    except Exception:
        pass
    return rows


def _kb_index(kb_provider) -> dict[str, list[MatchRow]]:
    """name → [MatchRow]；同名多城/多分行全部保留（分店消歧的输入）。"""
    idx: dict[str, list[MatchRow]] = {}
    for city, p in (kb_provider() or []):
        nm = (p.get("name") or "").strip()
        if not nm:
            continue
        idx.setdefault(nm, []).append(MatchRow(
            city=city, name=nm,
            category=(p.get("category") or "").strip(),
            area=(p.get("area") or "").strip(),
            source=(p.get("source") or "").strip(), origin="kb"))
    return idx


def _partial_hits(name: str, idx: dict[str, list[MatchRow]]
                  ) -> list[MatchRow]:
    """双向包含的部分匹配。短名 ≥2 字；命中标注为 partial（置信降档）。"""
    hits: list[MatchRow] = []
    for kb_name, rows in idx.items():
        if kb_name == name:
            continue
        if len(name) >= 2 and len(kb_name) >= 2 and \
                (name in kb_name or kb_name in name):
            hits.extend(rows)
    return hits


# ---------------------------------------------------------------- 在线探测


def _default_probe(city: str, name: str, entity_type: str) -> list[MatchRow]:
    """只读在线探测（主源·高德）：景点 find_spot / 餐饮住宿 search_amap_keyword。

    任何异常向上抛，由 resolve 统一落 unavailable——这里绝不把失败
    伪装成"没找到"。"""
    from . import poi_sources
    center = resolve_geo(city)
    if not center:
        raise LookupError(f"解析不到「{city}」的坐标")
    kind = {"restaurant": "restaurant", "hotel": "hotel"}.get(
        entity_type, "attraction")
    if kind == "attraction":
        row = poi_sources.find_spot(city, name, center)
        rows = [row] if row else []
    else:
        rows = poi_sources.search_amap_keyword(city, center, name, kind,
                                               limit=5)
    out: list[MatchRow] = []
    for r in rows or []:
        nm = (r.get("name") or "").strip()
        if not nm:
            continue
        out.append(MatchRow(
            city=city, name=nm, category=(r.get("category") or kind),
            area=(r.get("area") or "").strip(),
            source="amap_online", origin="online"))
    return out


def _default_review_probe(city: str, name: str,
                          entity_type: str) -> list[MatchRow]:
    """只读复核探测（复核源·维基导游，免 Key）。

    适用范围检查由调用方完成（v3.2.1 §1.2 前置检查 2）——不适用源
    不强制执行。异常向上抛，绝不把失败伪装成"没找到"。"""
    from . import poi_sources
    center = resolve_geo(city)
    if not center:
        raise LookupError(f"解析不到「{city}」的坐标")
    rows = poi_sources.search_wikivoyage(city, center, kind="attraction")
    low = name.lower()
    out: list[MatchRow] = []
    for r in rows or []:
        nm = (r.get("name") or "").strip()
        if not nm:
            continue
        # 复核语义：确认该范围内是否收录了这个名字（精确或包含）
        if nm == name or (low and (low in nm.lower()
                                   or nm.lower() in low)):
            out.append(MatchRow(
                city=city, name=nm, category="attraction",
                area=(r.get("area") or "").strip(),
                source="wikivoyage_online", origin="online"))
    return out


class ProbeQuotaError(RuntimeError):
    """探测调用限额已用尽（v3.2.1 §1.4：保护外部接口压力）。"""

    def __init__(self) -> None:
        super().__init__("probe quota exceeded")


def _quota_limit() -> int:
    try:
        return max(1, int(os.environ.get("TRAVEL_RESOLVER_PROBE_QUOTA", "100")))
    except ValueError:
        return 100


class _ProbeBudget:
    """进程内探测调用限额。超限抛 ProbeQuotaError → unavailable +
    reason=quota_exceeded，绝不外推成"实体不存在"。"""

    def __init__(self, limit: int | None = None) -> None:
        self.remaining = limit if limit is not None else _quota_limit()

    def consume(self) -> None:
        if self.remaining <= 0:
            raise ProbeQuotaError()
        self.remaining -= 1


# 进程级缓存：同一 (city, name, type) 的成功探测结果只取一次外部接口；
# 失败结果不缓存（下次可重试）。缓存命中不消耗限额。
_PROBE_CACHE: dict[tuple[str, str, str], list[MatchRow]] = {}


def _budgeted_probe(budget: _ProbeBudget, fn, city: str, name: str,
                    etype: str) -> tuple[list[MatchRow], bool]:
    """限额 + 缓存包装。返回 (rows, from_cache)。异常向上抛。"""
    key = (city, (name or "").strip(), (etype or "").strip())
    hit = _PROBE_CACHE.get(key)
    if hit is not None:
        return list(hit), True
    budget.consume()
    rows = fn(city, name, etype)
    _PROBE_CACHE[key] = list(rows)
    return list(rows), False


def _classify_probe_error(exc: Exception) -> str:
    """异常 → probe_reason（v3.2.1 §8 枚举）。"""
    if isinstance(exc, ProbeQuotaError):
        return REASON_QUOTA_EXCEEDED
    if isinstance(exc, LookupError):
        return REASON_NO_CITY_CENTER
    if isinstance(exc, PermissionError):
        return REASON_NO_KEY
    return REASON_NETWORK_TIMEOUT


def _online_allowed(allow_online) -> bool:
    if allow_online is not None:
        return bool(allow_online)
    return os.environ.get("TRAVEL_RESOLVER_ONLINE", "").strip() == "1"


def _expected_city(ctx: dict) -> str:
    slots = ctx.get("slots") or {}
    return (slots.get("destination") or ctx.get("trip_dest")
            or ctx.get("last_city") or "").strip()


# ---------------------------------------------------------------- 主入口


def resolve(candidates, context: dict | None = None, *,
            kb_provider=None, probe_fn=None, review_fn=None,
            allow_online: bool | None = None,
            budget: _ProbeBudget | None = None) -> ResolveResult:
    """对一组候选逐个验证。candidates 接受 PoCandidate / dict 混合列表。

    kb_provider：测试注入，替代 _default_kb_rows（只读契约）。
    probe_fn(city, name, entity_type) -> [MatchRow]：主源探测注入；抛异常
    一律落 probe_status=unavailable（绝不等于 not_found）。
    review_fn：复核源探测注入（v3.2.1 双源复核）；None 时——生产模式
    （probe_fn 也为 None）用 _default_review_probe，测试注入模式记
    skipped/not_configured（证据不足保持保守）。
    allow_online：None 时跟随环境变量 TRAVEL_RESOLVER_ONLINE（影子默认关）。
    budget：探测限额（默认进程级 _ProbeBudget，超限 → quota_exceeded）。
    """
    ctx = context or {}
    res = ResolveResult()
    probe = probe_fn or _default_probe
    review = review_fn or (None if probe_fn else _default_review_probe)
    online = _online_allowed(allow_online)
    budget = budget or _ProbeBudget()
    idx = _kb_index(kb_provider or _default_kb_rows)
    exp_city = _expected_city(ctx)

    for cand in candidates:
        c = cand.as_dict() if hasattr(cand, "as_dict") else dict(cand)
        name = (c.get("text") or "").strip()
        etype = c.get("entity_type") or "unknown"

        # 非实体候选：品类词/城市只记录不验证（角色说明见设计 §6）
        if etype in ("category", "city") or not name:
            res.resolved.append(ResolvedCandidate(
                original=c, status=STATUS_SKIPPED, confidence=0.0,
                probe_status=PROBE_NOT_APPLICABLE,
                notes=["品类/城市候选不是 POI，不验证"]))
            continue

        rc = ResolvedCandidate(original=c, status=STATUS_UNVERIFIED,
                               confidence=0.0,
                               probe_status=PROBE_NOT_APPLICABLE)

        # ---- ①② KB 精确/部分匹配 ----
        kb_hits = list(idx.get(name) or [])
        partial = not kb_hits
        if not kb_hits:
            kb_hits = _partial_hits(name, idx)
        rc.matches = kb_hits

        if kb_hits:
            rc.probe_status = PROBE_NOT_APPLICABLE
            rc.probe_source = "kb"
            rc.probe_results = [{"source": "kb", "outcome": OUTCOME_HIT,
                                 "reason": ""}]
            rc.confidence = (_CONF_KB_PARTIAL if partial else _CONF_KB_EXACT)
            if partial:
                rc.notes.append("KB 部分匹配（双向包含），非全名相等")
            _verify_city_type(rc, exp_city, name)
            _triage_kb(rc, kb_hits, exp_city)
            res.resolved.append(rc)
            continue

        # ---- ⑥ KB 未命中 → 在线只读探测（双源，v3.2.1 §1.2） ----
        if not online:
            rc.probe_status = PROBE_NOT_REQUESTED
            rc.probe_reason = REASON_DISABLED_BY_POLICY
            rc.probe_results = [{"source": MAIN_SOURCE,
                                 "outcome": OUTCOME_SKIPPED,
                                 "reason": REASON_DISABLED_BY_POLICY}]
            rc.notes.append("本轮为 KB-only 影子验证，未做在线探测")
            res.resolved.append(rc)
            continue
        probe_city = exp_city
        if not probe_city:
            rc.probe_status = PROBE_UNAVAILABLE
            rc.probe_reason = REASON_NO_CITY_CENTER
            rc.probe_results = [{"source": MAIN_SOURCE,
                                 "outcome": OUTCOME_FAILED,
                                 "reason": REASON_NO_CITY_CENTER}]
            rc.notes.append("无上下文城市，无法限定探测范围")
            res.resolved.append(rc)
            continue
        try:
            if probe_fn is None:
                # 生产模式：默认探测走缓存 + 限额（保护外部接口）
                online_hits, _cached = _budgeted_probe(
                    budget, probe, probe_city, name, etype)
            else:
                # 测试注入模式：直接调用，不做跨函数缓存（不同注入源
                # 的结果不可混用），仍受限额约束
                budget.consume()
                online_hits = probe(probe_city, name, etype)
        except Exception as e:
            # 铁律：探测没做成 ≠ 没找到。unavailable → unverified。
            reason = _classify_probe_error(e)
            rc.probe_status = PROBE_UNAVAILABLE
            rc.probe_reason = reason
            rc.probe_results = [{"source": MAIN_SOURCE,
                                 "outcome": OUTCOME_FAILED,
                                 "reason": reason}]
            rc.notes.append(f"在线探测不可用（{type(e).__name__}），"
                            "候选保持未验证")
            res.resolved.append(rc)
            continue

        # ---- 主源命中：不需要复核，直接分流 ----
        if online_hits:
            rc.matches = online_hits
            rc.probe_status = PROBE_SUCCESS_HIT
            rc.probe_source = MAIN_SOURCE
            rc.probe_results = [{"source": MAIN_SOURCE,
                                 "outcome": OUTCOME_HIT, "reason": ""}]
            exact = any(m.name == name for m in online_hits)
            rc.confidence = (_CONF_ONLINE_EXACT if exact
                             else _CONF_ONLINE_PARTIAL)
            _verify_city_type(rc, exp_city, name)
            _triage_online(rc, online_hits, probe_city)
            res.resolved.append(rc)
            continue

        # ---- 主源空结果：双源复核（v3.2.1 §1.2） ----
        results = [{"source": MAIN_SOURCE, "outcome": OUTCOME_EMPTY,
                    "reason": ""}]
        review_rows: list[MatchRow] | None = None
        review_failed_reason = ""
        if review is None:
            # 测试注入主源但未提供复核源：证据不足，保守处理
            results.append({"source": REVIEW_SOURCE,
                            "outcome": OUTCOME_SKIPPED,
                            "reason": REASON_NOT_CONFIGURED})
        elif etype not in _REVIEW_APPLICABLE_TYPES:
            # 复核源类型适用性前置检查：不适用源不强制执行、不计入复核
            results.append({"source": REVIEW_SOURCE,
                            "outcome": OUTCOME_SKIPPED,
                            "reason": REASON_TYPE_NOT_COVERED})
        else:
            try:
                if review_fn is None:
                    review_rows, _ = _budgeted_probe(
                        budget, review, probe_city, name, etype)
                else:
                    budget.consume()
                    review_rows = review(probe_city, name, etype)
                results.append({"source": REVIEW_SOURCE,
                                "outcome": (OUTCOME_HIT if review_rows
                                            else OUTCOME_EMPTY),
                                "reason": ""})
            except Exception as e:
                review_failed_reason = _classify_probe_error(e)
                results.append({"source": REVIEW_SOURCE,
                                "outcome": OUTCOME_FAILED,
                                "reason": review_failed_reason})
                review_rows = None
        rc.probe_results = results

        if review_rows:
            # 复核源命中：证据存在，按命中分流（证据来源如实标注）
            rc.matches = review_rows
            rc.probe_status = PROBE_SUCCESS_HIT
            rc.probe_source = REVIEW_SOURCE
            rc.confidence = _CONF_ONLINE_PARTIAL
            rc.notes.append(f"主源无结果，复核源（{REVIEW_SOURCE}）命中")
            _verify_city_type(rc, exp_city, name)
            _triage_online(rc, review_rows, probe_city)
            res.resolved.append(rc)
            continue
        if review_failed_reason:
            # 任一适用源 failed → 整体 unavailable → unverified。
            # 绝不把「两个源都没返回实体」当成双源空结果。
            rc.probe_status = PROBE_UNAVAILABLE
            rc.probe_reason = review_failed_reason
            rc.notes.append("复核源探测失败，证据不足保持未验证")
            res.resolved.append(rc)
            continue
        # 复核未执行（not_configured / 不适用）或复核也为空：
        # 可用证据源不足 2 → 单源空结果，保守 unverified（绝不 rejected）
        applicable = [p for p in results if p["outcome"] in
                      (OUTCOME_EMPTY, OUTCOME_HIT)]
        rc.probe_status = PROBE_NOT_FOUND
        rc.probe_reason = (REASON_CROSS_SOURCE_EMPTY if len(applicable) >= 2
                           else REASON_DATA_SOURCE_EMPTY)
        if len(applicable) >= 2:
            # 充分证据：≥2 个适用且独立的源均成功且无匹配 → rejected
            rc.status = STATUS_REJECTED
            rc.confidence = 0.3
            rc.notes.append(
                f"双源复核（{MAIN_SOURCE}+{REVIEW_SOURCE}）均无匹配："
                "在规定数据源与检索范围内没有找到；不进行程，保留原始表达")
        else:
            rc.notes.append(
                f"可用证据源不足 2（当前仅{MAIN_SOURCE}）：单源无匹配按"
                "保守处理为未验证，不下存在性结论")
        res.resolved.append(rc)

    return res


def _triage_online(rc: ResolvedCandidate, hits: list[MatchRow],
                   probe_city: str) -> None:
    """在线/复核命中后的分流：唯一 → confirmed；多条/类型不符 → ambiguous。"""
    if len(hits) > 1:
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_MULTI_ONLINE
        rc.notes.append(f"在线命中 {len(hits)} 条同名/近似记录，"
                        "需用户消歧")
    elif rc.type_check == "mismatch":
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_TYPE
    else:
        rc.status = STATUS_CONFIRMED
        rc.notes.append(f"在线探测命中{probe_city}的「{hits[0].name}」")


# ---------------------------------------------------------------- 校验子步


def _verify_city_type(rc: ResolvedCandidate, exp_city: str, name: str) -> None:
    """③ 城市归属 + ④ 类型校验（对已命中的候选）。"""
    if not rc.matches:
        return
    first = rc.matches[0]
    if exp_city:
        rc.city_check = ("match" if any(m.city == exp_city
                                        for m in rc.matches) else "mismatch")
        if rc.city_check == "mismatch":
            rc.confidence -= _CONF_NO_CITY_CTX
            rc.notes.append(f"「{name}」资料记在{first.city}，"
                            f"不在当前目的地{exp_city}")
    else:
        rc.city_check = "unknown"
        rc.confidence -= _CONF_NO_CITY_CTX
        rc.notes.append("上下文无城市可比对，归属待确认")

    kb_type = _TYPE_CHECK_MAP.get((first.category or "").strip())
    cand_type = _TYPE_CHECK_MAP.get(rc.original.get("entity_type") or "")
    if kb_type and cand_type:
        rc.type_check = "match" if kb_type == cand_type else "mismatch"
        if rc.type_check == "mismatch":
            rc.confidence -= _CONF_TYPE_MISMATCH
            rc.notes.append(f"类型不符：候选标为 {cand_type}，"
                            f"资料里是 {kb_type}")


def _triage_kb(rc: ResolvedCandidate, hits: list[MatchRow],
               exp_city: str) -> None:
    """KB 命中后的四档分流：唯一匹配 → confirmed；多候选/跨城/类型 → ambiguous。"""
    cities = sorted({m.city for m in hits})
    if len(hits) > 1 and len(cities) == 1:
        # 同城同名多记录：分店/重复条目，列清单要求用户消歧，不静默合并
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_BRANCH
        rc.notes.append(f"同名 {len(hits)} 条记录（"
                        + "；".join(m.area or "未知区域" for m in hits)
                        + "），需用户选择分店")
        return
    if len(cities) > 1:
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_MULTI_CITY
        rc.notes.append("跨城同名：" + "/".join(cities) + "，需用户消歧")
        return
    if exp_city and rc.city_check == "mismatch":
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_CROSS_CITY
        return
    if rc.type_check == "mismatch":
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_TYPE
        return
    if rc.confidence >= _CONF_CONFIRM_MIN:
        rc.status = STATUS_CONFIRMED
    else:
        rc.status = STATUS_AMBIGUOUS
        rc.ambiguity_reason = AMBIG_REASON_TYPE


# ---------------------------------------------------------------- 组合校验
# （v3.2.1 §8：probe_status 与 probe_reason 空值规则 + probe_results
#   内部一致性不变量；测试与运行时共用同一判定。）


_NOT_FOUND_REASONS = {REASON_DATA_SOURCE_EMPTY, REASON_CROSS_SOURCE_EMPTY}
_UNAVAILABLE_REASONS = {REASON_NETWORK_TIMEOUT, REASON_NO_KEY,
                        REASON_CIRCUIT_OPEN, REASON_NO_CITY_CENTER,
                        REASON_QUOTA_EXCEEDED}


def validate_probe_combo(status: str, probe_status: str,
                         probe_reason: str | None = None,
                         probe_results: list[dict] | None = None
                         ) -> list[str]:
    """校验 (status, probe_status, probe_reason, probe_results) 合法性。

    返回违规列表；空列表 = 合法组合。判定依据：设计 §8 合法组合表 +
    probe_reason 空值规则 + 内部一致性不变量。"""
    bad: list[str] = []

    # ---- probe_reason 空值规则 ----
    reason = (probe_reason or "").strip() or None
    if probe_status == PROBE_NOT_APPLICABLE and reason is not None:
        bad.append(f"not_applicable 的 reason 必须为空，实际 {reason!r}")
    if probe_status == PROBE_NOT_REQUESTED and \
            reason != REASON_DISABLED_BY_POLICY:
        bad.append(f"not_requested 的 reason 固定为 "
                   f"{REASON_DISABLED_BY_POLICY}，实际 {reason!r}")
    if probe_status == PROBE_SUCCESS_HIT and reason is not None:
        bad.append(f"success_hit 的 reason 必须为空，实际 {reason!r}")
    if probe_status == PROBE_NOT_FOUND and reason not in _NOT_FOUND_REASONS:
        bad.append(f"not_found 的 reason 只能是 {sorted(_NOT_FOUND_REASONS)}"
                   f"，实际 {reason!r}")
    if probe_status == PROBE_UNAVAILABLE and \
            reason not in _UNAVAILABLE_REASONS:
        bad.append(f"unavailable 必须带具体失败原因 "
                   f"{sorted(_UNAVAILABLE_REASONS)}，实际 {reason!r}")

    # ---- status × probe_status 组合表 ----
    legal = {
        STATUS_CONFIRMED: {PROBE_NOT_APPLICABLE, PROBE_SUCCESS_HIT},
        STATUS_AMBIGUOUS: {PROBE_NOT_APPLICABLE, PROBE_SUCCESS_HIT},
        STATUS_REJECTED: {PROBE_NOT_FOUND},
        STATUS_UNVERIFIED: {PROBE_NOT_REQUESTED, PROBE_UNAVAILABLE,
                            PROBE_NOT_FOUND},
        STATUS_SKIPPED: {PROBE_NOT_APPLICABLE},
    }
    if status not in legal:
        bad.append(f"未知 status：{status!r}")
    elif probe_status not in legal[status]:
        bad.append(f"非法组合 ({status}, {probe_status})")

    # ---- rejected 证据闸（HC-6） ----
    if status == STATUS_REJECTED:
        if reason != REASON_CROSS_SOURCE_EMPTY:
            bad.append("rejected 必须 reason=cross_source_empty")
        if probe_results is not None:
            empties = [p for p in probe_results
                       if p.get("outcome") == OUTCOME_EMPTY]
            bad_or_skipped = [p for p in probe_results
                              if p.get("outcome") in (OUTCOME_FAILED,
                                                      OUTCOME_SKIPPED)]
            if len(empties) < 2 or bad_or_skipped:
                bad.append("rejected 必须 ≥2 个适用源 success_empty 且"
                           "无 failed/skipped（双源前置检查）")

    # ---- probe_results 内部一致性不变量 ----
    if probe_results:
        hits = [p for p in probe_results if p.get("outcome") == OUTCOME_HIT]
        failed = [p for p in probe_results
                  if p.get("outcome") == OUTCOME_FAILED]
        if failed and probe_status == PROBE_NOT_FOUND:
            bad.append("不变量：任一源 failed 时聚合不得为 not_found"
                       "（主源空+复核失败 → unverified，绝不 rejected）")
        for p in probe_results:
            oc = p.get("outcome")
            pr = (p.get("reason") or "").strip() or None
            if oc in (OUTCOME_FAILED, OUTCOME_SKIPPED) and pr is None:
                bad.append(f"probe_results：{oc} 必带 reason")
            if oc in (OUTCOME_HIT, OUTCOME_EMPTY) and pr is not None:
                bad.append(f"probe_results：{oc} 的 reason 必须为空")
        if probe_status == PROBE_SUCCESS_HIT and not hits:
            bad.append("聚合 success_hit 但明细中无 success_hit 源")
        if probe_status == PROBE_NOT_FOUND and hits:
            bad.append("聚合 not_found 但明细中有 success_hit 源")
        if probe_status == PROBE_UNAVAILABLE and not failed:
            bad.append("聚合 unavailable 但明细中无 failed 源")
        skipped_srcs = [p.get("source") for p in probe_results
                        if p.get("outcome") == OUTCOME_SKIPPED]
        if probe_status == PROBE_NOT_FOUND and \
                reason == REASON_CROSS_SOURCE_EMPTY and skipped_srcs:
            bad.append("cross_source_empty 计数不得包含 skipped 源")
    return bad
