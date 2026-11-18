"""CandidateExtractor：五层候选识别瀑布（Phase 3，影子运行，2026-11-18）。

取代"减法剥词"的识别机制：从**原始句子**生成实体候选，保留候选的
Span（原始文本下标，左闭右开）、实体类型与上下文角色。五层是**识别
顺序**，不是五个独立解析器：

  L1 POI 全名/别名匹配      KB 与历史行程里已有实体，最长匹配优先，matched=True
  L2 实体类型线索未知候选    大排档/旧址/乐园等类型后缀 → 未知实体（matched=False）；
                            类型后缀是**明确词表**（有优先级与冲突规则），不是任意尾部词
  L3 角色判定               must_visit / query / nearby_anchor / negated / remove
  L4 城市识别               只用未被上层占用的 Span；区分 destination /
                            query_city / departure_city；路线框架（从A到B）
                            的城市给路线角色，绝不当作 POI
  L5 冲突消歧               Span 重叠处理：上层已确认的身份优先，长匹配优先，
                            不因最长匹配误删语义独立的多个实体

边界（本阶段明确不做）：存在性校验（probe-then-store）、置信度分流、
在线写库、LLM——候选 matched=False 只表示"词典没命中"，绝不代表
实体不存在；校验是第四阶段的职责。

Span 契约：span_start 含起始字符、span_end 不含结束字符；全部基于
**原始用户输入**。左扩展会在已知城市名处截断（店名常带城市前缀，
「排上海贝皇大排档」的城市前缀并入候选，绝不另发城市候选）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import intent
from .real_tools import CITY_GEO, POIS_DB

# ---- 词典：明确区分 KB 实体 / 历史行程实体 / 城市 / 品类 / 类型后缀 ----

# 类型后缀词表（L2）。顺序即优先级：酒店 > 餐饮 > 景点。同名歧义
# （「饭店」既是住宿也常指餐馆）按优先级取先命中的一类，并在
# match_type 里如实标注 suffix_inferred。
_TYPE_SUFFIXES: list[tuple[str, str]] = (
    [(s, "hotel") for s in
     ("宾馆", "民宿", "客栈", "酒店")]
    + [(s, "restaurant") for s in
       ("大排档", "餐馆", "菜馆", "饭馆", "面馆", "米线店", "米粉店",
        "火锅店", "烧烤店", "小吃店", "汤包店", "烧饼店", "馒头店",
        "包子铺", "糕团店", "甜品店", "奶茶店", "咖啡店", "酒楼",
        "食堂", "餐厅")]
    + [(s, "attraction") for s in
       ("博物馆", "纪念馆", "陈列馆", "美术馆", "展览馆", "科技馆",
        "遗址", "旧址", "故居", "陵", "塔", "寺", "庙", "祠", "府",
        "宫", "观", "窟", "公园", "乐园", "广场", "古镇", "古城",
        "步行街", "街区", "老街", "风景区", "景区", "度假区", "湿地",
        "瀑布", "溶洞", "石窟", "索道", "码头", "廊道", "基地", "戏台")]
)
_TYPE_SUFFIXES.sort(key=lambda x: -len(x[0]))

# 品类词（不是实体）：命中即品类候选，类型后缀推断不得覆盖它的 Span。
_CATEGORY_WORDS: tuple[str, ...] = (
    "火锅", "烧烤", "小吃", "海鲜", "本帮菜", "川菜", "粤菜", "湘菜",
    "点心", "甜品", "面食", "米线", "游乐园", "主题公园", "水族馆",
    "植物园", "动物园", "免费景点", "夜市",
)
_CATEGORY_WORDS = tuple(sorted(_CATEGORY_WORDS, key=lambda w: -len(w)))

# 名词短语左边界停用词（从候选左侧剥掉的句法成分；按长度优先）
_STOP_PREFIX = (
    "安排上", "安排", "推荐一下", "推荐", "介绍一下", "介绍下", "介绍",
    "给我", "点一下", "点名", "想去", "要去", "必去", "排上", "排个",
    "排", "加上", "加个", "加", "带上", "先去", "再去", "去完", "去了",
    "第一二三四五六七八",  # 占位防误配，真实匹配见 _DAY_PREFIX_RE
    "那", "就", "再", "先", "想", "要", "在", "从", "到", "完", "了",
    "的", "个", "家", "间", "位", "上",
)
_DAY_PREFIX_RE = re.compile(
    r"(?:第[一二三四五六七八九十百\d]+天|第[一二三四五六七八九十百\d]+站)")
# 名词短语右侧停（动词/语气词/时间量词直接截断）
_TRAILING_STOP_RE = re.compile(
    r"(?:玩|旅游|度假|几天|多天|半天|一整天|一天|两三天|[一二三四五六七八九十\d]+天|"
    "怎么样|好不好|好玩吗|门票|开放时间|地址|哪里|哪儿|好吃|好玩|怎么样)+$")
# 分店后缀（右扩展）：（豫园店）/ 郑和南路店 / 南京路店
_BRANCH_RE = re.compile(
    r"(?:[（(][^（）()]{1,10}[店点][）)]|[一-龥]{1,8}(?:路|街|巷|村|大道|南路|北路)店)")

# 角色判定的动词窗口（L3）
_REMOVE_RE = re.compile(r"(?:去掉|删掉|删除|删了|划掉|从行程.{0,4}去掉)\s*$")
_NEGATED_RE = re.compile(r"(?:不想去|不想玩|不去|别去|不要去|排除|避开|不要)\s*$")
_NEARBY_RE = re.compile(r"(?:附近|周边|旁边|楼上|楼下)")
_MUST_RE = re.compile(
    r"(?:排|加|安排|点名|想去|要去|必去|带上|先去|再去|去完|预留)\s*$")
_QUERY_RE = re.compile(
    r"(?:介绍|好玩|怎么样|在哪|哪儿|地址|位置|门票|值得|是什么|什么地方|"
    r"怎么样|值得去|怎么去|怎么玩|攻略)\s*$")
# 后窗不锚定行尾：候选后紧跟「在哪里/怎么样」等即查询角色
_QUERY_POST_RE = re.compile(
    r"^(?:在哪里|在哪|哪儿|怎么样|好不好|好玩吗|好玩不|值得去吗|值得吗|"
    r"介绍|怎么去|怎么玩|门票|开放时间|营业时间)")

# 路线框架：「从A到B」
_ROUTE_RE = re.compile(r"从([一-龥]{1,8})到")

_CJK_RUN_RE = re.compile(r"[一-龥]+")


def _known_city(word: str) -> bool:
    return word in CITY_GEO


def _kb_names(context: dict) -> tuple[list[str], dict[str, str]]:
    """L1 词典：静态种子 + KB 数据库 + 历史行程实体（最长优先）。

    返回 (names, name→category)；只读，本阶段绝不写 KB。"""
    names: list[str] = []
    cats: dict[str, str] = {}
    seen: set[str] = set()

    def _add(nm: str, cat: str | None) -> None:
        nm = (nm or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)
            if cat:
                cats[nm] = cat

    for city_pois in POIS_DB.values():
        for p in city_pois:
            _add(p.get("name"), p.get("category"))
    try:
        from . import kb as kb_mod
        db = kb_mod.get_kb()
        if db is not None:
            for city in (db.cities() or []):
                for p in (db.list_pois(city) or []):
                    _add(p.get("name"), p.get("category"))
    except Exception:
        pass
    # 历史行程/上下文里已出现的实体（别名的主要来源）
    slots = context.get("slots") or {}
    for k in ("must_visit", "soft_prefs", "avoid"):
        for nm in (slots.get(k) or []):
            _add(nm, None)
    plan = context.get("last_plan") or {}
    for d in (plan.get("days_plan") or []):
        for n in (d.get("nodes") or []):
            nm = (n.get("name") or "").strip()
            if nm and nm.startswith(("午餐", "晚餐", "午休", "自由活动",
                                     "抵达", "入住", "早市", "前往", "返程")):
                continue
            _add(nm, n.get("type") if n.get("type") == "restaurant" else None)
    names.sort(key=lambda s: -len(s))
    return names, cats


@dataclass
class PoCandidate:
    """一个实体候选。matched=False 只说明词典未命中，不代表实体不存在。"""

    text: str
    span_start: int                  # 左闭
    span_end: int                    # 右开；基于原始输入
    entity_type: str                 # attraction / restaurant / hotel / category / unknown
    role: str                        # must_visit / query / nearby_anchor / negated / remove
    match_type: str                  # kb_full / context / suffix_inferred / intent_only / category_lex
    matched: bool                    # 词典命中与否（存在性校验是第四阶段的事）
    layer: int                       # 产生该候选的识别层（1..5）
    city: str | None = None

    def as_dict(self) -> dict:
        return {"text": self.text, "span_start": self.span_start,
                "span_end": self.span_end, "entity_type": self.entity_type,
                "role": self.role, "match_type": self.match_type,
                "matched": self.matched, "layer": self.layer,
                "city": self.city}


@dataclass
class ExtractResult:
    poi_candidates: list[PoCandidate] = field(default_factory=list)
    city_candidates: list[PoCandidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _suffix_type(suffix: str) -> str:
    for s, t in _TYPE_SUFFIXES:
        if s == suffix:
            return t
    return "attraction"


def _left_extend(text: str, suffix_start: int, notes: list[str]) -> str:
    """从类型后缀向左扩展出完整名词短语。

    规则：取后缀前紧连的中文连续段；剥掉左侧句法成分（动词/指示词/
    天数前缀）；已知城市名前缀（店名常带城市前缀）**并入候选**而不是
    另发城市候选——「排上海贝皇大排档」绝不能把上海当城市。"""
    start = suffix_start
    while start > 0 and _CJK_RUN_RE.match(text[start - 1]):
        start -= 1
    run = text[start:suffix_start]
    changed = True
    while changed:
        changed = False
        # 已知城市开头 = 店名的城市前缀，整体保留（不再剥任何停用词）：
        # 「排上海贝皇大排档」剥完「排」就是 上海贝皇大排档，上海属于店名
        if any(run.startswith(c) for c in CITY_GEO):
            break
        stripped = _DAY_PREFIX_RE.sub("", run)
        if stripped != run:
            run, changed = stripped, True
            continue
        cands = [w for w in _STOP_PREFIX
                 if run.startswith(w) and len(run) > len(w)]
        if cands:
            # 「排上」两读：排上+海贝皇 / 排+上海贝皇大排档（店名带城市
            # 前缀）。所有可用剥法里优先选「剥完落在已知城市名」的那种
            chosen = next(
                (w for w in cands
                 if any(run[len(w):].startswith(c) for c in CITY_GEO)),
                cands[0])
            run = run[len(chosen):]
            changed = True
            continue
        break
    # 右侧句法成分剥尾：附近/有什么 等不是实体名的一部分
    _tail = re.compile(r"(?:附近|周边|旁边|旁边有|有什么|有哪些|哪家|哪些|"
                       r"什么|好吃|好玩|一家|一些|有)+$")
    run = _tail.sub("", run)
    # 城市前缀并入候选（不另发城市候选）
    for cname in sorted((c for c in CITY_GEO if run.startswith(c)),
                        key=len, reverse=True):
        rest = run[len(cname):]
        if rest:
            notes.append(f"城市前缀「{cname}」并入候选实体名，不发独立城市候选")
            break
    return run


def _right_extend(text: str, end: int) -> int:
    """分店后缀右扩展：海贝皇海鲜大排档郑和南路店 → 一条完整候选。"""
    m = _BRANCH_RE.match(text, end)
    if m and m.group(0):
        return m.end()
    return end


def extract(text: str, context: dict | None = None,
            plan_like: bool | None = None) -> ExtractResult:
    """五层识别顺序的主入口。context 与 parser._norm_context 同形状。

    plan_like：规划语义标记（决定角色与城市角色的默认值）；
    None 时按 context 的 plan_made / classify 推断。
    """
    t = text or ""
    ctx = context or {}
    res = ExtractResult()
    if not t:
        return res
    if plan_like is None:
        plan_like = bool(ctx.get("plan_made")) \
            or intent.classify(t) == "plan"
    lexicon, kb_cats = _kb_names(ctx)

    # ---- L1：POI 全名及别名（最长匹配优先，贪心不重叠） ----
    occupied: list[tuple[int, int]] = []
    pois: list[PoCandidate] = []
    for name in lexicon:
        i = t.find(name)
        while i >= 0:
            span = (i, i + len(name))
            if not any(s < span[1] and span[0] < e for s, e in occupied):
                pois.append(PoCandidate(
                    text=name, span_start=span[0], span_end=span[1],
                    entity_type=kb_cats.get(name, "attraction"), role="query",
                    match_type="kb_full", matched=True, layer=1))
                occupied.append(span)
            i = t.find(name, i + 1)
    for c in pois:
        # 历史行程/上下文实体：match_type 更具体
        if c.text in (ctx.get("slots") or {}).get("must_visit", []) \
                or c.text in (ctx.get("slots") or {}).get("soft_prefs", []):
            c.match_type = "context"

    # ---- L2：类型线索未知候选（后缀词表；不覆盖 L1 与品类 Span） ----
    cat_spans: list[tuple[int, int]] = []
    for w in _CATEGORY_WORDS:
        j = t.find(w)
        while j >= 0:
            span = (j, j + len(w))
            if not any(s < span[1] and span[0] < e
                       for s, e in occupied + cat_spans):
                cat_spans.append(span)
            j = t.find(w, j + 1)
    for s, e in cat_spans:
        pois.append(PoCandidate(
            text=t[s:e], span_start=s, span_end=e,
            entity_type="category", role="query",
            match_type="category_lex", matched=True, layer=2))
    occupied.extend(cat_spans)

    used_layers: list[tuple[int, int]] = list(occupied)
    kb_spans: list[tuple[int, int]] = [(c.span_start, c.span_end)
                                       for c in pois if c.layer == 1]
    # 泛指后缀：直接跟在 什么/哪些/哪家 后面的类型词是问品类，不是实体
    _GENERIC_PRE_RE = re.compile(r"(?:什么|哪些|哪家|哪家|怎么样|一家|一些|几个)$")
    for suffix, etype in _TYPE_SUFFIXES:
        j = t.find(suffix)
        while j >= 0:
            span = (j, j + len(suffix))
            j = t.find(suffix, j + 1)
            # 后缀完整落在已确认 Span 内 → 跳过（类型词不是独立实体）
            if any(s <= span[0] and span[1] <= e for s, e in used_layers):
                continue
            if any(s < span[1] and span[0] < e for s, e in used_layers):
                continue
            # 与已确认 KB 实体紧邻（中山陵|景区）→ 是同一实体的后缀延伸，跳过
            # （只对 L1 实体判断；品类词紧邻店名是正常的——海贝皇海鲜|大排档）
            if any(e == span[0] or s == span[1] for s, e in kb_spans):
                continue
            # 「附近有什么餐厅」：泛指用法，不是实体
            if _GENERIC_PRE_RE.search(t[max(0, span[0] - 3):span[0]]):
                continue
            head = _left_extend(t, span[0], res.notes)
            if not head:
                continue
            full_start = span[0] - len(head)
            full_end = _right_extend(t, span[1])
            full = t[full_start:full_end]
            # 扩出来整段是已知城市 → 那是城市不是未知 POI（L4 处理）
            if _known_city(full):
                continue
            used_layers.append((full_start, full_end))
            pois.append(PoCandidate(
                text=full, span_start=full_start, span_end=full_end,
                entity_type=etype, role="query",
                match_type="suffix_inferred", matched=False, layer=2))

    # L2b：动词直接挂着的名字（无类型后缀但有实体指向意图）
    # 「安排小笼包」「加个幻影基地」——宁可保留未知候选交给第四阶段校验
    for m in re.finditer(
            r"(?:排上|排|安排|加个|加上|加|想去|要去|必去|点名)"
            r"([一-龥]{2,16})(?=[，。！？!？、\s]|$)", t):
        cand = m.group(1)
        cand = _TRAILING_STOP_RE.sub("", cand)
        cand = _DAY_PREFIX_RE.sub("", cand)
        if len(cand) < 2:
            continue
        span = (m.start(1), m.start(1) + len(cand))
        if any(s < span[1] and span[0] < e for s, e in used_layers):
            continue
        if _known_city(cand):
            continue          # 城市交给 L4
        if re.fullmatch(r"[一二两三四五六七八九十\d]+天?|[一二两\d]+人", cand):
            continue
        used_layers.append(span)
        pois.append(PoCandidate(
            text=cand, span_start=span[0], span_end=span[1],
            entity_type="unknown", role="query",
            match_type="intent_only", matched=False, layer=2))

    # ---- L3：角色判定（否定/删除 > 周边锚点 > 点名 > 查询） ----
    for c in pois:
        pre = t[max(0, c.span_start - 8):c.span_start]
        post = t[c.span_end:c.span_end + 6]
        if _REMOVE_RE.search(pre):
            c.role = "remove"
        elif _NEGATED_RE.search(pre):
            c.role = "negated"
        elif c.entity_type == "category":
            c.role = "query"
        elif _NEARBY_RE.search(post):
            c.role = "nearby_anchor"
        elif _MUST_RE.search(pre):
            c.role = "must_visit"
        elif _QUERY_RE.search(pre) or _QUERY_POST_RE.match(
                t[c.span_end:c.span_end + 8]):
            c.role = "query"
        else:
            c.role = "must_visit" if plan_like else "query"

    # ---- L4：城市识别（只用未被占用的 Span） ----
    def _span_taken(s: int, e: int) -> bool:
        return any(s < ce and cs < e for cs, ce in used_layers)

    city_cands: list[PoCandidate] = []
    route = list(_ROUTE_RE.finditer(t))
    route_roles: dict[str, str] = {}
    if route:
        m = route[0]
        dep, rest = m.group(1), t[m.end():]
        if _known_city(dep) and not _span_taken(m.start(1), m.end(1)):
            route_roles[dep] = "departure_city"
        # 到后面的第一个已知城市是目的地；到后面不是城市（「到玩5天」）
        # 就如实留空，绝不把动词截出来当城市
        dm = re.match(r"([一-龥]{1,8})", rest)
        if dm:
            frag = dm.group(1)
            for cname in sorted((c for c in CITY_GEO if frag.startswith(c)),
                                key=len, reverse=True):
                s, e = m.end(0), m.end(0) + len(cname)
                if not _span_taken(s, e):
                    route_roles[cname] = "destination"
                break
            else:
                res.notes.append("「到」后面没有城市名，目的地未知")
    for cname in sorted((c for c in CITY_GEO if c in t), key=len, reverse=True):
        i = t.find(cname)
        while i >= 0:
            s, e = i, i + len(cname)
            i = t.find(cname, i + 1)
            if _span_taken(s, e):
                continue
            role = route_roles.get(cname)
            if role:
                pass
            elif plan_like:
                role = "destination"
            else:
                role = "query_city"
            city_cands.append(PoCandidate(
                text=cname, span_start=s, span_end=e, entity_type="city",
                role=role, match_type="city_lex", matched=True, layer=4))

    # ---- L5：冲突消歧（重叠丢弃 + 排序；不误删语义独立的实体） ----
    pois.sort(key=lambda c: (c.layer, -(c.span_end - c.span_start),
                             c.span_start,
                             0 if c.entity_type != "unknown" else 1))
    kept: list[PoCandidate] = []
    kept_spans: list[tuple[int, int]] = []
    for c in pois:
        if any(c.span_start < e and s < c.span_end for s, e in kept_spans):
            res.notes.append(f"候选「{c.text}」与已确认候选 Span 重叠，丢弃")
            continue
        kept.append(c)
        kept_spans.append((c.span_start, c.span_end))
    kept.sort(key=lambda c: c.span_start)
    city_cands.sort(key=lambda c: c.span_start)
    res.poi_candidates = kept
    res.city_candidates = city_cands
    return res
