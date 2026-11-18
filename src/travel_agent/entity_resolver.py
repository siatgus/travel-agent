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

# ---- 探测进度（与 status 正交：unavailable ≠ not_found） ----
PROBE_NOT_NEEDED = "not_needed"    # KB 已命中，无需探测
PROBE_KB_HIT = "kb_hit"
PROBE_ONLINE_HIT = "online_hit"
PROBE_NOT_FOUND = "not_found"      # 探测成功执行且确认没有
PROBE_UNAVAILABLE = "unavailable"  # 网络/Key/熔断/无坐标——探测没做成
PROBE_SKIPPED = "skipped"          # 本轮关闭在线探测

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
    """只读在线探测：景点 find_spot / 餐饮住宿 search_amap_keyword。

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
            kb_provider=None, probe_fn=None,
            allow_online: bool | None = None) -> ResolveResult:
    """对一组候选逐个验证。candidates 接受 PoCandidate / dict 混合列表。

    kb_provider：测试注入，替代 _default_kb_rows（只读契约）。
    probe_fn(city, name, entity_type) -> [MatchRow]：测试注入；抛异常
    一律落 probe_status=unavailable（绝不等于 not_found）。
    allow_online：None 时跟随环境变量 TRAVEL_RESOLVER_ONLINE（影子默认关）。
    """
    ctx = context or {}
    res = ResolveResult()
    probe = probe_fn or _default_probe
    online = _online_allowed(allow_online)
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
                probe_status=PROBE_NOT_NEEDED,
                notes=["品类/城市候选不是 POI，不验证"]))
            continue

        rc = ResolvedCandidate(original=c, status=STATUS_UNVERIFIED,
                               confidence=0.0, probe_status=PROBE_NOT_NEEDED)

        # ---- ①② KB 精确/部分匹配 ----
        kb_hits = list(idx.get(name) or [])
        partial = not kb_hits
        if not kb_hits:
            kb_hits = _partial_hits(name, idx)
        rc.matches = kb_hits

        if kb_hits:
            rc.probe_status = PROBE_KB_HIT
            rc.confidence = (_CONF_KB_PARTIAL if partial else _CONF_KB_EXACT)
            if partial:
                rc.notes.append("KB 部分匹配（双向包含），非全名相等")
            _verify_city_type(rc, exp_city, name)
            _triage_kb(rc, kb_hits, exp_city)
            res.resolved.append(rc)
            continue

        # ---- ⑥ KB 未命中 → 在线只读探测 ----
        if not online:
            rc.probe_status = PROBE_SKIPPED
            rc.notes.append("本轮为 KB-only 影子验证，未做在线探测")
            res.resolved.append(rc)
            continue
        probe_city = exp_city
        if not probe_city:
            rc.probe_status = PROBE_UNAVAILABLE
            rc.notes.append("无上下文城市，无法限定探测范围")
            res.resolved.append(rc)
            continue
        try:
            online_hits = probe(probe_city, name, etype)
        except Exception as e:
            # 铁律：探测没做成 ≠ 没找到。unavailable → unverified。
            rc.probe_status = PROBE_UNAVAILABLE
            rc.notes.append(f"在线探测不可用（{type(e).__name__}），"
                            "候选保持未验证")
            res.resolved.append(rc)
            continue
        rc.matches = online_hits
        if not online_hits:
            rc.probe_status = PROBE_NOT_FOUND
            rc.status = STATUS_REJECTED
            rc.confidence = 0.3
            rc.notes.append(f"在线探测确认{probe_city}没有「{name}」；"
                            "不进行程，保留原始表达")
            res.resolved.append(rc)
            continue
        rc.probe_status = PROBE_ONLINE_HIT
        exact = any(m.name == name for m in online_hits)
        rc.confidence = _CONF_ONLINE_EXACT if exact else _CONF_ONLINE_PARTIAL
        _verify_city_type(rc, exp_city, name)
        if len(online_hits) > 1:
            rc.status = STATUS_AMBIGUOUS
            rc.ambiguity_reason = AMBIG_REASON_MULTI_ONLINE
            rc.notes.append(f"在线命中 {len(online_hits)} 条同名/近似记录，"
                            "需用户消歧")
        elif rc.type_check == "mismatch":
            rc.status = STATUS_AMBIGUOUS
            rc.ambiguity_reason = AMBIG_REASON_TYPE
        else:
            rc.status = STATUS_CONFIRMED
            rc.notes.append(f"在线探测命中{probe_city}的「"
                            f"{online_hits[0].name}」")
        res.resolved.append(rc)

    return res


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
