"""统一解析层：parse_utterance(text, context) → ParseResult（Phase 2，2026-11-17）。

病根回顾（方案评审定稿）：此前 spot_mention / extract_slots / food_keyword /
refine_intent 四处各剥各的词，减法剥词开环无校验，于是出现：
「从大理到玩5天」被当景点候选、未提供人数默认填 1、「上海有哪些好吃的」
把上海当品类词。第二阶段不重写识别（那是第三阶段的五层瀑布），先把
**字段契约和数据流**统一：

1. 唯一入口：一句话只在这里完整解析一次；旧函数（intent.spot_mention /
   food_keyword 等）暂不删除，降级为本模块的底层信号源，下游逐步改为
   消费 ParseResult——不允许各模块再对原始句子独立执行互相冲突的抽取。
2. 三城市字段分离：destination / query_city / departure_city 独立存储。
   **destination 写入权限表**：仅规划类意图（新建/修改/补槽）可写；
   查询类意图只允许写 query_city。query_city 绝不通过兼容层写进
   slots.destination（见 to_legacy_delta 与对应测试）。
3. people 语义修复：用户未提供人数时保持 None（旧 slots 默认 1 的行为
   由兼容层保留，新 Schema 是唯一事实源）。
4. 实体候选带 Span：保留候选在原始文本中的位置，供第三阶段瀑布继续使用。

边界（本阶段明确不做）：五层识别瀑布、EntityResolver / probe-then-store、
置信度分流都不在这里——本阶段只解决字段语义和数据流。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from . import agent
from . import intent
from .dialogue import _explicit_fields

# 裸数字回答：追问人数/天数后的「1」「三」
_BARE_NUM_RE = re.compile(r"\s*([0-9]+|[一二两三四五六七八九十]+)\s*")
# 候选实体类型后缀（与 tests/test_corpus.guess_entity_type 同口径）
_HOTEL_SUFFIX_RE = re.compile(r"酒店|宾馆|民宿|客栈|饭店$")
_FOOD_SUFFIX_RE = re.compile(
    r"餐厅|餐馆|饭店|菜馆|火锅|烧烤|小吃|面馆|大排档|火烧|粉|汤|菜|鱼|鸭|店$")
# 点名候选若以删除/否定动词开头，是剥词残留不是实体（「去掉苍山」）
_NEG_PREFIX_RE = re.compile(r"^(?:不想去|不去|别去|去掉|删掉|删了|排除|避开|不要)")


def guess_entity_type(name: str) -> str:
    """按名字后缀猜实体类型：hotel / restaurant / attraction（第三阶段由
    瀑布的实体类型层取代，这里只是让候选结构完整）。"""
    if _HOTEL_SUFFIX_RE.search(name or ""):
        return "hotel"
    if _FOOD_SUFFIX_RE.search(name or ""):
        return "restaurant"
    return "attraction"


@dataclass
class EntityCandidate:
    """一个实体候选。span 是它在原始文本里的位置（第三阶段校验闭环用）。"""

    name: str
    entity_type: str = "attraction"
    city: str | None = None
    source: str = ""                      # must_visit / spot_mention / nearby_anchor
    span_start: int | None = None
    span_end: int | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "entity_type": self.entity_type,
                "city": self.city, "source": self.source,
                "span": ([self.span_start, self.span_end]
                         if self.span_start is not None else None)}


@dataclass
class ParseResult:
    """一句话的统一解析结果（新 Schema，唯一事实源）。

    destination / query_city / departure_city 三字段语义：
    - destination：当前行程目的地。**只有规划类意图可写**；查询类意图
      最多继承上下文里的值，绝不吸收本句查询城市。
    - query_city：本句查询绑定的城市（「上海有哪些好吃的」的上海）。
      与 destination 无关，也不回写旧 slots.destination。
    - departure_city：「从A到B」句式里的出发地，仅规划语义使用。
    """

    text: str
    intent: str                     # 细分意图（slot_fill_plan_create / plan_modify / …）
    intent_coarse: str              # classify 粗分类（plan / poi / weather / …）
    destination: str | None = None
    query_city: str | None = None
    departure_city: str | None = None
    destination_writable: bool = False   # 本句是否有权写 destination（写入权限表）
    destination_new: str | None = None   # 本句新说的目的地（None=继承上下文）
    poi_candidates: list[EntityCandidate] = field(default_factory=list)
    poi_category: str | None = None      # restaurant / 游乐园 / …（问句语义口径）
    food_keyword: str | None = None      # 菜品词（火锅/烤鸭），与品类分开
    date: dict | None = None             # 有效日期（本句新说的，否则继承上下文）
    days: int | None = None
    people: int | None = None            # 未提供时保持 None（绝不默认 1）
    confidence: float = 0.5
    notes: list[str] = field(default_factory=list)

    # -------------------------------------------------- 兼容层
    def to_legacy_delta(self) -> dict:
        """旧 slots 形状的增量（过渡兼容）。铁律：**query_city 绝不出现在
        这里**——查询类意图本句新说的城市永远不映射成 destination；
        destination_writable=False 时 destination 也为 None。人数缺省 1
        只是旧 slots 的形状要求，语义事实以 self.people 为准。"""
        return {
            "destination": (self.destination_new
                            if self.destination_writable else None),
            "origin": self.departure_city,
            "date_range": self.date,
            "days": self.days,
            "party_size": self.people if self.people is not None else 1,
        }


def _norm_context(context) -> dict:
    """Session 对象 / 普通字典都接受，统一成 dict 读取。"""
    if context is None:
        return {"slots": {}, "pending_field": None, "party_stated": False,
                "last_city": None, "trip_dest": None, "plan_made": False}
    if hasattr(context, "slots"):
        return {
            "slots": dict(context.slots or {}),
            "pending_field": getattr(context, "pending_field", None),
            "party_stated": bool(getattr(context, "party_stated", False)),
            "last_city": getattr(context, "last_city", None),
            "trip_dest": getattr(context, "trip_dest", None),
            "plan_made": bool(getattr(context, "plan_made", False)),
        }
    return {
        "slots": dict(context.get("slots") or {}),
        "pending_field": context.get("pending_field"),
        "party_stated": bool(context.get("party_stated")),
        "last_city": context.get("last_city"),
        "trip_dest": context.get("trip_dest"),
        "plan_made": bool(context.get("plan_made")),
    }


def _span_of(text: str, name: str) -> tuple[int, int] | None:
    """候选名在原句中的位置；剥词产物不在原句时返回 None（如实标注）。"""
    if not name:
        return None
    i = text.find(name)
    return (i, i + len(name)) if i >= 0 else None


def parse_utterance(text: str, context=None, today: date | None = None,
                    slots_delta: dict | None = None) -> ParseResult:
    """统一解析入口：一句话 + 会话上下文 → ParseResult。

    slots_delta：调用方已经抽好的本轮槽位增量（如 PlanHandler 的
    agent.extract_slots 结果）。传入就不再重复抽取——一句话一轮只解析
    一次；不传时内部做一次规则抽取。
    """
    today = today or date.today()
    t = text or ""
    ctx = _norm_context(context)
    ctx_slots = ctx["slots"]

    # ---- 底层信号：一轮只抽一次 ----
    delta = slots_delta if slots_delta is not None \
        else agent.extract_slots_rules(t, today)
    explicit = set(_explicit_fields(t, delta))
    coarse = intent.classify(t)
    utt_city = intent.city_in(t)

    res = ParseResult(text=t, intent=coarse, intent_coarse=coarse)

    # ---- 人数（问题修复 2）：未提供保持 None ----
    people: int | None = None
    days_fill: int | None = None
    bare = _BARE_NUM_RE.fullmatch(t.strip()) if t.strip() else None
    pend = ctx.get("pending_field")
    if bare and pend in ("party_size", "days"):
        n = agent.cn_to_int(bare.group(1))
        if n and pend == "party_size" and 1 <= n <= 30:
            people = n
        elif n and pend == "days" and 1 <= n <= 60:
            days_fill = n
    if "party_size" in explicit and delta.get("party_size"):
        people = delta["party_size"]
    elif people is None and ctx.get("party_stated"):
        # 上下文里明确说过人数（party_stated 区分「说了 1 人」和默认 1）
        people = ctx_slots.get("party_size")
    res.people = people

    # ---- 规划意图判定（destination 写入权限表） ----
    has_points = bool(delta.get("must_visit") or delta.get("avoid"))
    is_edit = bool(intent._PLAN_EDIT_RE.search(t))
    verb_only = bool(intent._PLAN_VERB_RE.search(t))
    slot_fill = bool(pend) and (bool(explicit) or bool(bare))
    plan_like = (coarse == "plan" or has_points or is_edit or slot_fill
                 or (verb_only and ctx.get("plan_made")))
    res.destination_writable = plan_like

    if plan_like:
        if slot_fill and coarse not in ("plan",):
            res.intent = "slot_fill_plan_create"
            res.confidence = 0.95
        elif ctx.get("plan_made") or has_points or is_edit:
            res.intent = "plan_modify"
            # 只有规划动词、抽不出任何点名/排除时置信降档（「那给我排XX旧址」）
            res.confidence = 0.9 if (has_points or delta.get("destination")
                                     or delta.get("origin")) else 0.75
        else:
            res.intent = "plan_create"
            res.confidence = 0.9
    else:
        res.intent = _fine_query_intent(t, coarse)
        res.confidence = _query_confidence(t, coarse)

    # ---- 三城市字段 ----
    # destination：本句新说的目的地来自规则抽取（内部已做店名遮罩、
    # 「从A出发」纠偏等闸），查询句里本句城市只进 query_city。
    res.destination_new = delta.get("destination") if plan_like else None
    if res.destination_new and res.destination_new == utt_city and not plan_like:
        res.destination_new = None
    res.destination = res.destination_new or ctx_slots.get("destination")

    # query_city：仅查询类意图绑定；本句城市优先，其次继承上下文目的地。
    if not plan_like:
        res.query_city = utt_city or ctx_slots.get("destination") \
            or ctx.get("last_city") or ctx.get("trip_dest")

    # departure_city：「从A到B」的 A，仅规划语义
    res.departure_city = delta.get("origin") or None

    # ---- 日期/天数：本句新说的优先，否则继承上下文 ----
    res.date = delta.get("date_range") or ctx_slots.get("date_range") or None
    res.days = days_fill or delta.get("days") or ctx_slots.get("days") or None

    # ---- 实体候选（带 Span）----
    cands: list[EntityCandidate] = []
    anchor: str | None = None
    if plan_like:
        # 规划句：点名必去才是候选；排除清单不是候选（「不想去豫园」的
        # 豫园是要避开的地方，「去掉苍山」更不能当候选）
        for m in delta.get("must_visit") or []:
            cands.append(EntityCandidate(
                name=m, entity_type=guess_entity_type(m),
                source="must_visit", span_start=None, span_end=None))
    else:
        # 查询句：点名景点/店 + 周边锚点
        sm = intent.spot_mention(t, utt_city or ctx_slots.get("destination"))
        if sm and not _NEG_PREFIX_RE.match(sm):
            cands.append(EntityCandidate(
                name=sm, entity_type=guess_entity_type(sm),
                source="spot_mention"))
        anchor = None
        if res.intent in ("nearby_food_query", "hotel_search") \
                or intent.is_nearby_food_question(t) \
                or intent.is_nearby_hotel_question(t):
            anchor = intent.nearby_anchor(t, res.query_city)
            if anchor and not any(c.name == anchor for c in cands):
                cands.append(EntityCandidate(
                    name=anchor, entity_type=guess_entity_type(anchor),
                    source="nearby_anchor"))
    for c in cands:
        sp = _span_of(t, c.name)
        if sp:
            c.span_start, c.span_end = sp
    res.poi_candidates = cands

    # ---- 品类 / 菜品词 ----
    res.food_keyword = intent.food_keyword(t, utt_city)
    if res.intent in ("food_query", "restaurant_search", "nearby_food_query"):
        res.poi_category = "restaurant"
    elif res.intent == "poi_location_query" \
            and any(c.entity_type == "restaurant" for c in cands):
        res.poi_category = "restaurant"
    elif res.intent == "category_query":
        cat = intent.poi_category(t)
        res.poi_category = cat[0] if cat else None
    # 周边美食：锚点地名不是菜品词（「中山陵景区附近有什么吃的」）
    if res.intent == "nearby_food_query" and res.food_keyword and anchor \
            and (res.food_keyword in anchor or anchor in res.food_keyword):
        res.notes.append(f"「{res.food_keyword}」是周边锚点不是菜品词，已清掉")
        res.food_keyword = None

    return res


def _fine_query_intent(t: str, coarse: str) -> str:
    """查询句的细分意图（与 dialogue/POIHandler 的拦截链同一顺序：
    类比 > 市内路线 > 周边找吃/找住 > 找店 > 美食 > 住 > 品类 > 点名）。"""
    if coarse in ("weather", "transport", "plan_qa", "smalltalk"):
        return coarse
    if intent.is_nearby_food_question(t):
        return "nearby_food_query"
    if intent.is_nearby_hotel_question(t) or intent.is_stay_question(t):
        return "hotel_search"
    if intent._is_dish_shop(t) or (intent.is_food_question(t)
                                   and intent.food_question_kind(t) == "restaurant"):
        return "restaurant_search"
    if intent.is_food_question(t):
        return "food_query"
    if intent.is_category_question(t):
        return "category_query"
    sm = intent.spot_mention(t)
    if sm and re.search(r"在哪|哪儿|地址|位置", t):
        return "poi_location_query"
    if sm:
        return "poi_query"
    return "general"


def _query_confidence(t: str, coarse: str) -> float:
    """查询类句子的置信度（规则信号强度，第三阶段接置信分流）。"""
    if coarse in ("weather", "transport", "plan_qa"):
        return 0.9
    if intent.is_nearby_food_question(t) or intent.is_nearby_hotel_question(t):
        return 0.85
    if intent._is_dish_shop(t):
        return 0.85
    if intent.is_food_question(t):
        return 0.9
    if intent.is_category_question(t):
        return 0.85
    if intent.is_stay_question(t):
        return 0.85
    if re.search(r"在哪|哪儿|地址|位置", t):
        return 0.85          # 位置问句
    if intent.spot_mention(t) is not None:
        return 0.85
    return 0.5 if coarse in ("other", "smalltalk") else 0.85
