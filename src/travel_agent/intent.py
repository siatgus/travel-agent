"""意图路由 + 信息类直答。

之前所有输入都进「排行程」流水线，于是出现这种对话：

    用户：北京的真实天气怎么样
    系统：排行程还差 2 件事：1. 想去哪儿？ 2. 大概什么时候出发？
    用户：北京天气
    系统：排行程还差 1 件事：1. 打算玩几天？
    用户：北京这周末天气如何
    系统：## 上海 4 天行程 …（凭空换了城市，还编了个杭州出发）

三处毛病：不认意图、不认城市（只认有 POI 的 6 个）、追问到上限就硬凑方案。
这里补前两处：先判断用户到底想干什么，能直接答的就用真实工具直接答，
不进槽位、不追问、不消耗追问轮次。

设计原则：
- **规则优先**：意图分类是确定性的，不依赖 LLM（没配 Key 也能跑）。
- **诚实**：没有数据就说没有，绝不拿别的城市顶上去。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from . import real_tools

# ------------------------------------------------------------------ 词表

WEATHER_KW = ("天气", "气温", "多少度", "几度", "冷不冷", "热不热", "下雨", "雨吗",
              "下雪", "晒不晒", "穿什么", "预报", "气候", "风大")
POI_KW = ("有什么好玩", "有啥好玩", "好玩的地方", "好玩吗", "必去", "值得去",
          "景点", "打卡", "攻略", "怎么玩", "推荐玩", "有什么可以玩",
          "门票", "开放时间", "营业时间", "闭馆",
          # 「天安门广场周边有什么」「外滩附近有什么」这类点名+泛问，也归景点问答
          "周边", "附近", "周围", "介绍一下", "介绍下")
# 推荐句式：「推荐几家驴肉火烧」曾被分成 other 落进排行程流水线——
# 已有方案时直接被当成"改行程"整单重排（用户体感：聊两句它又自顾自排行程）。
# 推荐是问答不是规划；「推荐个行程/安排一下」这类含规划词的仍归 plan
# （classify 里 has_plan 检查在本规则之前）。
_RECO_RE = re.compile(r"(?:推荐|介绍|找找|找|想想|列)(?:几|一|些)?(?:家|个|些)"
                      r"|有哪些|哪家|多来几家")
TRANSPORT_KW = ("高铁", "动车", "火车", "机票", "航班", "班次", "车次", "票价",
                "怎么去", "怎么走", "多久到", "怎么到")
PLAN_KW = ("行程", "安排", "规划", "计划", "排一下", "排个", "帮我排", "玩几天",
           "几天", "预算", "出发", "度假", "旅行", "旅游", "走一趟", "待几天")
# 闲聊词表：只是「没配模型/模型失败」时的兜底——配了模型时规则判 other
# 的句子会交给 LLM 兜底分类（含 smalltalk 标签），覆盖面由模型补齐。
SMALLTALK_KW = ("你是谁", "你叫什么", "你叫啥", "你是什么", "你是啥",
                "什么模型", "哪个模型", "啥模型", "多少亿参数",
                "你是做什么的", "你是干什么的", "你是干嘛的",
                "你能做什么", "你能干嘛", "你会什么", "你会做什么", "你会干嘛",
                "你是机器人", "你是人工智能", "你是ai",
                # 追问/质疑助手刚才说的话：也是闲聊，不能当"要排行程"追问城市
                "你确定", "确定吗", "真的假的", "真的吗",
                "你好", "您好",
                "谢谢", "谢了", "再见", "拜拜", "在吗", "早上好", "晚上好", "晚安")

# 追问/质疑类（「你确定吗」「真的假的」）：场景要给「追问」而不是「问候」，
# 否则模型会把「你在质疑我刚才那句」答成「在的，想了解哪儿的情况」。
_FOLLOWUP_KW = ("你确定", "确定吗", "真的假的", "真的吗", "是吗")

# 身份类问法（「你是什么模型」「你是做什么的」实测都答不上来）：场景
# 判定与模板兜底共用一张表，别再各写一份各漏一半。
_IDENTITY_KW = ("你是谁", "你叫什么", "你叫啥", "你是什么", "你是啥",
                "什么模型", "哪个模型", "啥模型", "多少亿参数",
                "你是做什么的", "你是干什么的", "你是干嘛的",
                "你能做什么", "你能干嘛", "你会什么", "你会做什么", "你会干嘛",
                "你是机器人", "你是人工智能", "你是ai")


def is_identity_question(text: str) -> bool:
    """在问助手自己的身份/能力吗（你是谁、什么模型、能做什么……）。"""
    t = (text or "").lower()
    return any(k in t for k in _IDENTITY_KW)

# 行程问答：用户要总结/评价当前这份行程，不是要重排。
# 必须赶在 has_plan 之前判断——「将行程总结成一段话」里的"行程"会撞上 PLAN_KW，
# 然后被拿去重新排一份一模一样的。
_PLAN_QA_RE = re.compile(
    r"总结|概括|汇总|一段话|几句话|简述|这个行程|这份行程|该行程|"
    r"行程怎么样|行程如何|行程合理|行程有什么问题|行程点评|点评一下行程|"
    r"讲讲这份|说说这份")
# 这些是"改行程"的说法，虽然也带"行程"二字，但不是问答
_PLAN_EDIT_RE = re.compile(
    r"重新排|重排|再排|重新安排|换个排法|改一下|改成|调整|"
    r"加上|加个|加一个|去掉|删掉|不要去|避开|换成")
# 明确的"动手排"动词：句子里有它才算规划请求
_PLAN_VERB_RE = re.compile(r"排|规划|安排|做.{0,3}攻略|做.{0,3}行程|定制")
# 分钟级精确时刻：15:20 / 15点20(分) / 3点半。只有带分钟的才算——
# 「下午3点出发」这种整点常是班次/到站信息，交给对应问答处理
_CLOCK_PRECISE_RE = re.compile(
    r"[01]?\d\s*[:：]\s*[0-5]\d|[01]?\d\s*点\s*[0-5]\d\s*分?|[01]?\d\s*点半"
    r"|[01]?\d\s*点(?!\s*[0-5]?\d|半)")


def wants_clock_schedule(text: str) -> bool:
    """想按某个具体时刻（如 15:20）精确改排时间轴吗。

    页面支持分钟级锚点改排：整体平移目标日的时间轴（见
    composer.apply_anchor）。三条触发路：规划动词 + 精确时刻
    （「排到 15:20 结束」）、餐食/午休目标词 + 精确时刻（「每天
    晚餐7点开始」——没有规划动词但语义毫不含糊）、只改午休时长
    （「午休改成2小时」）。班次句子里不会出现「午餐」这类词，
    「15:20 的高铁」仍不会误入。真正的解析在 parse_clock_anchor，
    解析不出目标时调用方走诚实提示。
    """
    t = text or ""
    if not _CLOCK_PRECISE_RE.search(t):
        # 没有时刻也可能在改排：只动午休时长（「午休改成2小时」）
        return bool(re.search(r"午[休睡][^，。；]{0,8}"
                              r"[一二两三四五六七八九十\d]{1,2}\s*(?:个)?(?:小时|钟头)", t))
    return bool(_PLAN_VERB_RE.search(t)
                or re.search(r"调整|改到|挪到|重排|改排", t)
                or any(k in t for _, kws in _ANCHOR_TARGETS for k in kws))


# ------------------------------------------------------------------ 锚点解析

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s: str | None) -> int | None:
    """「3」「三」「十」「二十三」→ int；认不出返回 None。"""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return ((_CN_NUM.get(a, 1) if a else 1) * 10
                + (_CN_NUM.get(b, 0) if b else 0))
    return _CN_NUM.get(s)


# 锚点目标词表：编排器真实拥有时间轴的节点才可作锚点
_ANCHOR_TARGETS = (("早餐", ("早餐", "早饭")), ("午餐", ("午餐", "午饭")),
                   ("晚餐", ("晚餐", "晚饭")), ("午休", ("午休", "午睡")))


# ------------------------------------------------------------------ 交通时刻修改

# 交通时刻段词：去程/返程/到达/出发这类绑真实班次的对象。
# 「把去程到达时间改为 09:00」之前被目的地兜底正则吃成景点/城市
# 「达时间改为」，污染必去景点、假设和行前准备（用户实测）。
_TRANS_SEG_RE = re.compile(
    r"去程|返程|回程|到达|抵达|出发|发车|开车|班次|车次|航班|高铁|动车|火车|机票|大交通")
_TRANS_TIME_RE = re.compile(r"时间|时刻")
_TRANS_CHANGE_RE = re.compile(
    r"改为|改成|改到|换到|换成|改一下|修改|调整|提前|推迟|定在")


def is_transport_time_change(text: str) -> bool:
    """想直接指定/修改去程、返程的时刻吗。

    班次时刻来自真实查询，助手无权凭空改数字——这类指令必须拦在
    槽位抽取与排程之前如实拒绝，指引用户走「查真实班次 → 按班次
    重排」的正路。要求交通对象 + 时刻词 + 修改动词三要素齐备，
    「我们 9 点到成都」「到达时间是什么时候」这类陈述/提问不拦。
    """
    t = text or ""
    return bool(_TRANS_SEG_RE.search(t) and _TRANS_TIME_RE.search(t)
                and _TRANS_CHANGE_RE.search(t))


# 混合指令按标点切成从句逐句判断，切完用中文逗号拼回去
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！？!？\n]+")


def split_transport_time_change(text: str) -> tuple[str, str | None]:
    """把混合指令拆成「交通时刻修改从句」+「其余行程请求」。

    「把出发时间改成10点，去成都玩3天」是两件事——改班次时刻要拒绝，
    但「去成都玩3天」是正经排行程请求，不能整句连坐（用户实测：整句
    被拒后还反问"直接说去哪儿玩几天"，可目的地和天数明明都说了）。
    按标点切从句、逐句过 is_transport_time_change，
    返回 (剩余文本, 被剔除的从句或 None)；没有标点的整句命中时
    剩余为空串，调用方按纯拒绝处理。
    """
    t = (text or "").strip()
    if not (_TRANS_SEG_RE.search(t) and _TRANS_TIME_RE.search(t)
            and _TRANS_CHANGE_RE.search(t)):
        return t, None          # 快路径：整句都不含时刻修改，原样返回
    kept: list[str] = []
    dropped: list[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(t):
        clause = clause.strip()
        if not clause:
            continue
        if is_transport_time_change(clause):
            dropped.append(clause)
        else:
            kept.append(clause)
    return "，".join(kept), ("，".join(dropped) if dropped else None)


def parse_clock_anchor(text: str) -> dict | None:
    """从「把 Day 3 午餐排到 15:20 结束后接 90 分钟午休」解析锚点。

    返回 {"day": int|None, "every_day": bool, "target": str,
          "mode": "start"|"end", "minute": int|None, "nap_min": int|None}；
    minute 为 None 表示只改午休时长（「午休改成2小时」）；
    解析不出目标节点、或目标/时刻/时长一样都落不了地就返回 None
    （调用方走诚实提示兜底）。

    只接餐食/午休——编排器只对这几类节点拥有时间轴主权；抵达/返程
    时刻绑定真实班次，「出发」「到达」类说法不接（平移等于改班次，
    必须如实拒绝）。整点说法（「下午3点出发」「15:20 的高铁」）多为
    班次信息，wants_clock_schedule 的规划动词闸门 + 目标词表双重过滤。
    """
    t = (text or "").strip()
    m = _CLOCK_PRECISE_RE.search(t)
    target = None
    for name, kws in _ANCHOR_TARGETS:
        if any(k in t for k in kws):
            target = name
            break
    if target is None:
        return None
    mode = "end" if re.search(r"结束|吃完|离[开店]|之后|以后", t) else "start"
    # 时刻 → 当天分钟数。带「下午/晚上/傍晚」前缀按 12 小时制 +12；
    # 没前缀时按目标推：晚餐必在晚上（「晚餐7点」=19:00），
    # 午餐/午休的 1~2 点是下午（「午餐1点」=13:00）。
    hour = minute = None
    if m:
        s = m.group(0)
        hm = re.match(r"([01]?\d)\s*[:：]\s*([0-5]\d)", s)
        if hm:
            hour, minute = int(hm.group(1)), int(hm.group(2))
        else:
            hm2 = re.match(r"([01]?\d)\s*点\s*(?:([0-5]\d)\s*分?|(半))?", s)
            hour = int(hm2.group(1))
            minute = 30 if hm2.group(3) else int(hm2.group(2) or 0)
        if re.search(r"下午|傍晚|晚上", t) and hour < 12:
            hour += 12
        elif target == "晚餐" and hour <= 11:
            hour += 12
        elif target in ("午餐", "午休") and hour <= 2:
            hour += 12
    # 第几天：不写天数时由编排层决定落在哪天（every_day 或含目标的首日）
    day = None
    dm = re.search(r"第\s*([一二两三四五六七八九十\d]{1,3})\s*天", t) \
        or re.search(r"day\s*([一二两三四五六七八九十\d]{1,3})", t, re.I)
    if dm:
        day = _cn_int(dm.group(1))
    every_day = day is None and bool(re.search(r"每天|每日|天天", t))
    # 「接 90 分钟午休」「午休改成 2 小时」：给目标日的午休节点改时长
    nap_min = None
    nm = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*分钟\s*的?\s*午[休睡]"
                   r"|午[休睡]\s*([一二两三四五六七八九十\d]{1,3})\s*分钟", t)
    if nm:
        nap_min = _cn_int(nm.group(1) or nm.group(2))
    else:
        nm2 = re.search(r"午[休睡][^，。；]{0,8}?([一二两三四五六七八九十\d]{1,2})"
                        r"\s*(?:个)?(?:小时|钟头)", t)
        if nm2:
            _h = _cn_int(nm2.group(1))
            if _h:
                nap_min = _h * 60
    # 「午休改成 2 小时」这类只改时长的锚点：没有时刻也成立，
    # 但目标 + 时刻 + 时长至少要有一样能落地的
    if hour is None and nap_min is None:
        return None
    return {"day": day, "every_day": every_day, "target": target,
            "mode": mode,
            "minute": hour * 60 + minute if hour is not None else None,
            "nap_min": nap_min}
# 疑问句式：问句 + 没有规划动词 ≈ 在问问题，不是要出行程
# （「去日本旅游需要办签证吗」之前被"旅游"关键词劫去重排行程）
_INTERROG_RE = re.compile(
    r"[？?]\s*$|[吗呢]\s*[。.？?！!]?\s*$|是不是|能不能|要不要|需不需要|可不可以"
    r"|贵不贵|便不便宜|值不值|好不好玩|方不方便|远不远|难不难|累不累|多不多")

# 数据覆盖说明：只对这几个城市能排具体行程（真实 POI/酒店）
_SERVICEABLE_HINT = "大理、丽江、香格里拉、杭州、上海、邯郸"


def is_plan_qa(text: str) -> bool:
    """这句话是在总结/评价当前行程吗（而不是要重排）。"""
    t = text or ""
    if not _PLAN_QA_RE.search(t):
        return False
    return not _PLAN_EDIT_RE.search(t)


# 「把可去可不去的删掉」「删掉一些景点」：泛指删非必去点。
# 这类话抽不出具体名字，会被当成景点名污染方案（实测「可不去的删掉」
# 进了必去点、还经 autofetch 落库）——必须在抽取之前识别出来。
_TRIM_OPT_RE = re.compile(
    r"(?:可去可不去|可有可无|非必去|不必去|不太必要)[^，。；!?！？]{0,6}"
    r"(?:删掉|删了|删去|去掉|去除|拿掉|排除)"
    r"|(?:删掉|删了|删去|去掉|去除|拿掉)[^，。；!?！？]{0,4}"
    r"(?:一些|某些|部分|几个|几处|可去可不去|可有可无|非必去|不必去)"
    r"|(?:少去|少排|减掉)[^，。；!?！？]{0,4}(?:一些|部分|几个|几处|景点|站点)")


def wants_trim_optional(text: str) -> bool:
    """用户在泛指地要求删掉可去可不去的点（「别太赶」的另一种说法）。

    与「去掉灵隐寺」这类**点名**删除不同：点名删除由 avoid 抽取接住走重排；
    这里只接泛指——句子里的「一些/可去可不去的」不是地名，别让抽取层拿它当景点。
    """
    return bool(_TRIM_OPT_RE.search(text or ""))


# 通用问句探测：给 LLM 兜底分类做门卫，也决定要不要走联网问答
_QUESTION_RE = re.compile(
    r"[?？]|什么|怎么|怎样|多少|几[点号月天个]|为什么|是否|能不能|可不可以|"
    r"有没有|是不是|哪[里儿个座条]|介绍一下|讲讲|说说|告诉我|[吗呢]\s*[。.]?$")


def looks_like_question(text: str) -> bool:
    """像不像一个提问（而非在回答追问、补充信息）。超过 60 字的长句不拦。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return False
    return bool(_QUESTION_RE.search(t))


# 类比问句：「邯郸有没有像迪士尼一样的地方」「类似西湖这种」「跟丽江差不多的」
# 这类问题要的不是「这城市有什么景点」，而是「按某种类型找替代」——库里没有
# 类型标签，硬套本地景点清单会答成古城石窟（实测主题乐园问句回了广府古城）。
_COMPARE_RE = re.compile(
    r"像[^，。？?！!]{1,12}(?:一样|那样|那种|似的)"
    r"|类似[^，。？?！!]{1,10}|类似于"
    r"|(?:和|跟|同)[^，。？?！!]{1,12}(?:差不多|类似|一样)"
    r"|差不多的(?:地方|景点)|同款|这种地方|这类地方")


def is_comparison_question(text: str) -> bool:
    """在按「像 X 一样/类似 X」找同类地方吗（不是问这城市有什么）。"""
    return bool(_COMPARE_RE.search(text or ""))


# 品类问句：「本地或周边的中小型游乐园有哪些」「邯郸有温泉吗」问的是一个**品类**，
# 不是「这城市有什么景点」。库里没有类型标签、只有名字，所以只能按名字里的品类词
# 判定：命中就只报这些，没命中再去联网，绝不能拿热门景点凑数——实测「中小型游乐园」
# 回了广府古城、娲皇宫，而库里其实躺着「邯郸方特国色春秋」。
POI_CATEGORIES = (
    # (品类名, 用户问法里的词, 景点名里的词)
    ("水上乐园", ("水上乐园", "水世界", "水公园"), ("水上乐园", "水世界", "水公园")),
    ("游乐园", ("游乐园", "游乐场", "乐园", "主题公园", "欢乐谷", "方特", "过山车"),
     ("乐园", "游乐", "欢乐谷", "方特", "主题公园")),
    ("动物园", ("动物园", "野生动物园"), ("动物园",)),
    ("海洋馆", ("海洋馆", "水族馆", "海底世界"), ("海洋馆", "水族馆", "海底世界")),
    ("植物园", ("植物园",), ("植物园",)),
    ("滑雪场", ("滑雪场", "滑雪"), ("滑雪",)),
    ("温泉", ("温泉",), ("温泉",)),
    ("博物馆", ("博物馆", "博物院"), ("博物馆", "博物院")),
    ("古镇古城", ("古镇", "古城"), ("古镇", "古城")),
    ("演出", ("演出", "表演", "话剧", "音乐剧"), ("剧场", "剧院", "演出")),
)


def poi_category(text: str) -> tuple[str, tuple[str, ...]] | None:
    """问的是一个品类吗？返回 (品类名, 景点名匹配词)；泛问返回 None。

    顺序有意义：「水上乐园」必须先于「游乐园」，否则水世界会被当成乐园。
    """
    t = text or ""
    for label, ask_words, name_words in POI_CATEGORIES:
        if any(w in t for w in ask_words):
            return label, name_words
    return None


def category_pois(pois: list[dict] | None, name_words: tuple[str, ...]) -> list[dict]:
    """从本地景点里筛出这个品类的条目：按名字匹配，饭馆不掺进来。"""
    hit = []
    for p in pois or []:
        if p.get("category") == "restaurant":
            continue
        name = p.get("name") or ""
        if any(w in name for w in name_words):
            hit.append(p)
    return hit


# 品类词出现在句子里不等于在问品类：「我一定要去喜洲古镇」是改行程句式，
# 带「古镇」两字，不能被当成「有哪些古镇」抢走（实测会答成景点介绍、不重排）。
_CAT_ASK_RE = re.compile(r"有哪些|有没有|有什么|哪[里儿个家]|推荐|附近|周边|周围"
                         r"|吗|呢|[?？]")


def is_category_question(text: str) -> bool:
    """在问某个品类有没有/有哪些（而不是在点名某个地方或提要求）。"""
    return poi_category(text) is not None and bool(_CAT_ASK_RE.search(text or ""))


def classify(text: str) -> str:
    """返回 weather / poi / transport / plan / plan_qa / smalltalk / other。

    优先级：行程问答 > 规划意图 > 天气 > 景点 > 交通 > 闲聊。
    「北京天气好的话排个行程」这类混合句按规划处理（用户最终要的是行程）。
    """
    t = text or ""
    if is_plan_qa(t):
        return "plan_qa"
    has_plan = (any(k in t for k in PLAN_KW)
                or re.search(r"玩\s*[一二三四五六七八九十\d]+\s*天", t) is not None
                or re.search(r"去.{2,6}(玩|旅游|旅行|度假)", t) is not None)
    if has_plan:
        # 疑问句里没有规划动词的不当规划处理：「去日本旅游需要办签证吗」
        # 是在问事，不是要行程——留给后面的意图（或 LLM 兜底）接。
        if not (_INTERROG_RE.search(t) and not _PLAN_VERB_RE.search(t)):
            return "plan"
    if any(k in t for k in WEATHER_KW):
        return "weather"
    if any(k in t for k in POI_KW) or _RECO_RE.search(t) or is_category_question(t):
        return "poi"
    if any(k in t for k in TRANSPORT_KW):
        return "transport"
    if any(k in t for k in SMALLTALK_KW) and len(t) <= 16:
        return "smalltalk"
    return "other"


# ------------------------------------------------------------------ 城市识别

_NOT_CITY = {"今天", "明天", "后天", "最近", "现在", "这里", "那里", "那边",
             "这边", "周末", "本地", "当地", "最近几天", "这几天"}

# 句子里裹在城市名前后的时间／语气／动词，都不是地名的一部分。
# 之前「询问邯郸现在的天气」被截成「邯郸现在」，就是只认「今天/明天/的」三个尾巴。
_NOISE = ("最近几天", "今明两天", "未来几天", "这几天", "这两天", "这周末",
          "现在", "今天", "明天", "后天", "最近", "周末", "本周", "目前",
          "实时", "此刻", "当下", "接下来",
          "帮我查一下", "帮我看看", "帮我查", "查一下", "查询一下", "查询",
          "查查", "查下", "看一下", "看看", "查看", "想问一下", "想问",
          "想了解", "想知道", "问一下", "问下", "请问", "询问", "咨询",
          "告诉我", "报一下")


def _clean_city_name(raw: str) -> str | None:
    """把候选串里的时间词/动词剥掉，得到真正的地名。"""
    cand = raw or ""
    changed = True
    while changed and cand:
        changed = False
        for w in _NOISE:
            if len(cand) > len(w):
                if cand.startswith(w):
                    cand, changed = cand[len(w):], True
                elif cand.endswith(w):
                    cand, changed = cand[:-len(w)], True
        trimmed = cand.strip("的了啊呀吧呢")
        if trimmed != cand:
            cand, changed = trimmed, True
    if len(cand) < 2 or cand in _NOT_CITY:
        return None
    if cand.startswith(("几", "多少", "什么", "哪", "这", "那")):
        return None
    return cand


def city_in(text: str) -> str | None:
    """从一句话里找出城市名：先命中本地城市表（最长优先），再按「X 天气」句式兜底。"""
    t = text or ""
    hits = [c for c in real_tools.CITY_GEO if c in t]
    if hits:
        # 最长优先：避免「上海」抢先匹配「上海南站」这类更长地名
        return max(hits, key=len)
    # 兜底：城市名后面可以直接跟「天气」，也可以先跟 现在/今天/的 这类尾巴
    m = re.search(r"(?:去|到|在|从)?([\u4e00-\u9fa5]{2,6}?)"
                  r"(?:今天|明天|后天|现在|最近|目前|实时|此刻|这周末|周末|的)*"
                  r"(?:天气|气温|多少度|冷不冷|热不热|下雨|下雪)", t)
    if m:
        return _clean_city_name(m.group(1))
    return None


def _window(text: str) -> tuple[int, str]:
    """根据问法决定查几天：(天数, 说明)。"""
    t = text or ""
    if "周末" in t:
        today = date.today()
        to_sunday = (6 - today.weekday()) % 7          # 周日=6
        return max(2, to_sunday + 1), "这周末"
    for kw, n in (("一周", 7), ("七天", 7), ("7天", 7), ("三天", 3), ("后天", 3),
                  ("明天", 2), ("今天", 1), ("现在", 1)):
        if kw in t:
            return n, kw
    return 3, "未来三天"


def _wx_word(rain: int, low: int, high: int) -> str:
    if rain >= 70:
        return "有雨"
    if rain >= 40:
        return "可能有阵雨"
    if rain >= 20:
        return "多云"
    if high - low >= 12:
        return "晴，昼夜温差大"
    return "晴到多云"


def _wx_tip(days: list[dict]) -> str:
    tips: list[str] = []
    lows = [d["low"] for d in days]
    highs = [d["high"] for d in days]
    rains = [d["rain_prob"] for d in days]
    if highs and max(highs) >= 30:
        tips.append("白天偏晒，防晒和补水备好")
    if lows and min(lows) <= 5:
        tips.append("早晚偏冷，厚外套或羽绒服更稳")
    if highs and lows and max(highs) - min(lows) >= 12:
        tips.append("早晚温差 10℃ 以上，建议洋葱式穿搭")
    if rains and max(rains) >= 50:
        tips.append("有较明显降水，记得带伞")
    if not tips:
        tips.append("天气平稳，常规外套就够")
    return "；".join(tips)


def _speak(client, kind: str, facts: dict, user_text: str = "",
           must_keep: list | None = None) -> str | None:
    """让模型把事实说成人话；没配模型、调用失败或输出跑偏时返回 None，由模板兜底。"""
    if client is None:
        return None
    from . import llm as llm_layer
    return llm_layer.compose_reply(client, kind, facts, user_text, must_keep)


def weather_answer(city: str, text: str = "", with_hint: bool = True,
                   client=None) -> str:
    """直接回答天气：真实预报优先，超出预报窗口自动用历史同期均值。

    with_hint=False 用于连续追问：结论照给，但不再重复「我能排哪些城市」这段尾巴。
    配了模型时由它把预报讲成人话；模型不可用/数字对不上就退回模板。
    """
    if not city:
        return "你想问哪个城市的天气？告诉我城市名，我直接查真实预报。"
    geo = real_tools.resolve_geo(city)
    if not geo:
        # 分两种：像地名但没收录（县级市/景区）与明显不是地名。
        # 一律说"打错了"是错的——「廊坊」曾经就这样被冤枉过。
        noise = ("怎么", "什么", "说说", "聊聊", "查", "问", "天气", "吗", "呢", "吧")
        proper = (bool(re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", city))
                  and not any(w in city for w in noise))
        if proper:
            return (f"没找到「{city}」的位置信息，暂时查不了它的天气。\n"
                    "它可能是县级市或景区——说所属的地级市我也能查（比如「义乌」换成「金华」）；"
                    "地级市、省会和热门旅游城市我这边都收录了。")
        return (f"没找到「{city}」这个地名，听着不太像城市名。\n"
                "说个城市名我就能查实时天气，比如「廊坊」「北京」。")

    days_n, label = _window(text)
    today = date.today()
    end = today + timedelta(days=days_n - 1)
    r = real_tools.get_weather(city, today.isoformat(), end.isoformat())
    if not r.get("ok"):
        msg = (r.get("error") or {}).get("message") or "查询失败"
        return f"「{city}」的天气暂时没查到（{msg}），稍后再试一次？"

    days = r.get("days") or []
    if not days:
        return f"「{city}」这几天没有可用的天气数据。"

    def fmt(d: dict) -> str:
        day = date.fromisoformat(d["date"])
        wd = "一二三四五六日"[day.weekday()]
        return (f"{day.month}月{day.day}日（周{wd}）"
                f"{_wx_word(d['rain_prob'], d['low'], d['high'])}，"
                f"{d['low']}~{d['high']}℃，降水概率 {d['rain_prob']}%")

    head = days[0]
    if days_n == 1:
        body = f"**{city}今天** {fmt(head)}。"
    else:
        rest = "；".join(fmt(d) for d in days[1:])
        body = f"**{city}今天** {fmt(head)}。"
        if rest:
            body += f"\n接下来：{rest}。"
    body += f"\n出门提示：{_wx_tip(days)}。"
    note = r.get("note", "Open-Meteo")

    # 相对日（今天/明天/后天）由代码算好给模型抄——否则模型会自己推算，
    # 出现过「明天晴…后天11月3日」这种同一天两种叫法的自相矛盾
    rel_words = ["今天", "明天", "后天"]

    def _rel(i: int) -> str:
        return rel_words[i] if i < len(rel_words) else f"{i}天后"

    facts = {
        "城市": city,
        "时间口径": label,
        "天气": [{"相对日": _rel(i), "日期": fmt(d),
                  "最低温": d["low"], "最高温": d["high"],
                  "降水概率": d["rain_prob"]} for i, d in enumerate(days)],
        "出门建议": _wx_tip(days),
        "数据来源": note,
    }
    said = _speak(client, "weather", facts, text,
                  [head["low"], head["high"], head["rain_prob"]])
    body = (said + f"\n（数据：{note}）") if said else (body + f"\n（数据：{note}）")

    # 顺势引导，但不追问、不逼用户排行程
    if with_hint:
        if _has_poi(city):
            body += f"\n\n需要的话，我可以按这个天气直接排出{city}的行程。"
        else:
            body += (f"\n\n想在{city}玩的话直接说「就排{city}」，我可以现场联网补一份"
                     f"景点和住宿再排；资料现成的城市是 {_SERVICEABLE_HINT}。")
    return body


def _has_poi(city: str) -> bool:
    try:
        from . import tools
        return bool(tools.search_poi(city, limit=1).get("ok"))
    except Exception:
        return False


def poi_answer(city: str, client=None) -> str:
    """直接回答「有什么好玩的」：列出真实 POI，不做行程编排。

    库里没有这个城市时不能一句「没有数据」把用户推走——城市是公开信息，
    先在线检索一次（OSM/模型）并入库，再按真实数据回答；都查不到才如实说。
    """
    if not city:
        return "你想问哪个城市的景点？告诉我城市，我把值得去的地方列出来。"
    try:
        from . import tools
        r = tools.search_poi(city, limit=8)
    except Exception:
        r = {"ok": False}
    fetch_note = ""
    if not r.get("ok"):
        # 主动补数据：这是「问一个没收录的城市」而不是「这城市真没景点」
        try:
            from . import autofetch
            res = autofetch.ensure_city(city, client=client)
            if res.get("status") == "fetched":
                fetch_note = (f"\n\n> 数据说明：「{city}」原先不在我的资料库，"
                              f"刚在线检索了 {res.get('pois', 0)} 个景点入库；"
                              f"坐标为检索值，票价与时长供参考。")
                try:
                    r = tools.search_poi(city, limit=8)
                except Exception:
                    r = {"ok": False}
        except Exception:
            pass
    if not r.get("ok"):
        return (f"我这边还没有「{city}」的景点数据，在线也没检索到——"
                f"不想编。\n能排具体行程的城市目前是 {_SERVICEABLE_HINT}。"
                f"如果你要{city}的行程，我可以先把景点库补上再排。")

    items = sorted(r.get("items") or [], key=lambda p: -p.get("appeal", 0))[:6]
    facts = {"城市": city,
             "景点": [{"名称": p.get("name"), "区域": p.get("area"),
                       "时长分钟": p.get("duration_min"), "门票元": p.get("cost"),
                       "备注": p.get("note")} for p in items],
             "可排行程城市": _SERVICEABLE_HINT}
    said = _speak(client, "poi", facts, f"{city}有什么好玩的",
                  [p["name"] for p in items[:3] if p.get("name")])
    if said:
        return (said + "\n\n想待几天？告诉我天数，我可以按这些地方排一版带真实交通和天气的行程。"
                + fetch_note)

    lines = [f"**{city}** 值得去的地方（按我的评分排序）："]
    for p in items:
        extra = []
        if p.get("area"):
            extra.append(p["area"])
        if p.get("duration_min"):
            extra.append(f"约 {p['duration_min']} 分钟")
        if p.get("cost"):
            extra.append(f"门票 {p['cost']} 元")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        note = f" —— {p['note']}" if p.get("note") else ""
        lines.append(f"- **{p['name']}**{tail}{note}")
    lines.append("\n想待几天？告诉我天数，我可以按这些地方排一版带真实交通和天气的行程。")
    return "\n".join(lines) + fetch_note


# ---------------------------------------------------------------- 具体景点问答

# 「上海迪士尼好玩吗」要从问句里剥出「迪士尼」。这些词缀出现在候选两端时反复剥离。
_SPOT_EDGE = ("门票多少钱", "门票多少", "门票贵吗", "门票要钱吗", "门票", "开放时间",
              "营业时间", "闭馆时间", "几点关门", "几点开门", "值不值得去", "值得去吗",
              "值得去", "好不好玩", "好玩吗", "好玩不", "好玩", "怎么样", "怎么去",
              "怎么玩", "怎么走", "推荐一下", "值得吗", "推荐吗", "推荐", "攻略",
              "多少钱", "多少", "需要预约吗", "需要预约", "要预约吗", "预约",
              "想问问", "想知道", "想了解", "想去", "想去逛", "想逛", "请问",
              "问一下", "问下", "告诉我", "查一下", "查下", "看看", "一下", "听说",
              "周边有什么", "附近有什么", "周围有什么", "周边", "附近", "周围",
              # 位置类问法：「老驴头驴肉火烧在哪里」曾整个当名字查库，
              # 查不到就落进泛推荐，答一串不相干景点（实测驴头不对马嘴）
              "地址在哪里", "位置在哪里", "地址在哪", "位置在哪",
              "在哪里", "在哪儿", "在哪", "地址", "位置",
              "味道怎么样", "口味怎么样", "味道如何", "味道", "口味")
# 剥完还剩这些/以这些开头，说明用户问的是泛问题，不是某个具体景点
_SPOT_GENERIC = ("景点", "好玩", "地方", "推荐", "天气", "行程", "几天", "交通",
                 "美食", "住宿", "酒店")
_SPOT_START_RE = re.compile(r"^(有|哪|什么|几|怎么|多少|这|那|周围|附近|周边|还有|再|又|也)")


def spot_mention(text: str, city: str | None = None) -> str | None:
    """从「问某个具体景点」的句子里剥出景点名；泛问题返回 None。

    「上海迪士尼门票多少钱」→「迪士尼」；「上海有什么好玩的」→ None。
    只在意图为 poi 且用户不是在问泛推荐时用——拿它去库里查，查不到
    就走在线补全（ensure_spot），而不是把整句当「城市+泛推荐」处理。
    """
    cand = (text or "").strip("？?。！!，, 　~")
    if city and city in cand:
        cand = cand.replace(city, "", 1).strip("的了呀啊呢吧去到玩在是，, ")
    # 「你给我排的老驴头驴肉火烧在哪里」——指名行程里已排的点时，前缀
    # 全是代词和动词，不剥掉就会顶到长度上限、点名落空（实测）。
    m = re.match(r"^(?=(?:你|您|咱|我们)|(?:给我|帮我|给我们|给我来)"
                 r"|(?:排|安排|规划|推荐|订|找|加))"
                 r"(?:你|您|咱|我们)*(?:给我|帮我|给我们|给我来)?"
                 r"(?:排|安排|规划|推荐|订|找|加)?的(?=.)", cand)
    if m:
        cand = cand[m.end():]
    # 括号里的分店/说明是名字的一部分但常拦住库匹配：「圣旨骨酥鱼（滏东店）」
    # 按核心名查（keyword 子串照样命中库里的全名），长度也按核心名算。
    # 实测带括号问句超长 + 尾巴剥不净，点名直接落空、被当成泛推荐。
    cand = re.sub(r"[（(][^（）()]*[)）]", "", cand)
    changed = True
    while changed and cand:
        changed = False
        # 每轮先剥标点：剥掉「门票多少钱」后尾部会露出「怎么样？」，
        # 问号不清掉「怎么样」就永远剥不掉（实测广府古城问句残留）。
        trimmed = cand.strip("？?。！!，, 　~")
        if trimmed != cand:
            cand, changed = trimmed, True
        for w in _SPOT_EDGE:
            if len(cand) > len(w):
                if cand.startswith(w):
                    cand, changed = cand[len(w):], True
                if cand.endswith(w):
                    cand, changed = cand[:-len(w)], True
        trimmed = cand.strip("的呢吧吗啊呀的了")
        if trimmed != cand:
            cand, changed = trimmed, True
    cand = cand.strip("的了呀啊呢吧，, ")
    if len(cand) < 2 or len(cand) > 10:
        return None
    if _SPOT_START_RE.match(cand):
        return None
    if any(w in cand for w in _SPOT_GENERIC):
        return None
    return cand


def spot_answer(city: str, spot: str, pois: list[dict], note: str = "",
                client=None) -> str:
    """回答「某个具体景点怎么样/门票多少钱」：事实来自知识库或刚补的数据。"""
    if not pois:
        return (f"我这边没有「{spot}」的可靠资料，在线也没查到——不想编数字。\n"
                f"可以说说{city}别的地方，或者直接让我排{city}的行程。")
    facts = {"城市": city, "问的景点": spot,
             "结果": [{"名称": p.get("name"), "区域": p.get("area"),
                       "时长分钟": p.get("duration_min"),
                       ("人均元" if p.get("category") == "restaurant" else "门票元"):
                       p.get("cost"),
                       "开放时间": p.get("open_hours"), "备注": p.get("note")}
                      for p in pois[:3]],
             "补充说明": note}
    said = _speak(client, "poi", facts, f"{spot}怎么样",
                  [p.get("name") for p in pois[:1]] + [spot])
    if said:
        return said + (f"\n\n{note}" if note else "")
    lines = [f"**{city} · {spot}**："]
    for p in pois[:3]:
        extra = []
        if p.get("area"):
            extra.append(p["area"])
        if p.get("duration_min"):
            extra.append(f"建议留 {p['duration_min']} 分钟")
        if p.get("cost"):
            # 餐厅的 cost 是人均消费口径，说「门票」会让用户愣一下
            money_word = ("人均" if p.get("category") == "restaurant" else "门票")
            extra.append(f"{money_word} {p['cost']} 元")
        if p.get("open_hours"):
            extra.append(p["open_hours"])
        tail = f"（{'，'.join(extra)}）" if extra else ""
        nt = f" —— {p['note']}" if p.get("note") else ""
        lines.append(f"- **{p['name']}**{tail}{nt}")
    if note:
        lines.append(f"\n{note}")
    lines.append("\n想把它排进行程？说「我想去" + spot + "」我就重排。")
    return "\n".join(lines)


# 「推荐几家X」里的推荐套话：剥掉后剩下的才是要推荐的实体
_RECO_NOISE = ("推荐", "介绍", "找找", "找", "想想", "列一", "列", "几家", "一家",
               "几个", "哪些", "什么", "多来", "好吗", "我想", "想", "给我",
               "帮我", "一下", "当地", "本地", "有没有")


def reco_keyword(text: str, city: str | None = None) -> str | None:
    """「推荐几家驴肉火烧」→「驴肉火烧」；剥不出实体返回 None。

    推荐句不点名具体店（点名走 spot_mention），问的是"这一类里有什么"。
    """
    cand = (text or "").strip("？?。！!，, 　~")
    if city and city in cand:
        cand = cand.replace(city, "", 1)
    for w in _RECO_NOISE:
        cand = cand.replace(w, " ")
    cand = re.sub(r"\s+", "", cand).strip("的了呀啊呢吧，, ")
    # 剥掉推荐套话后开头残留的量词（「推荐个玩法」→「个玩法」→「玩法」）
    cand = re.sub(r"^[个家些]+", "", cand)
    if len(cand) < 2 or len(cand) > 10:
        return None
    if _SPOT_START_RE.match(cand) or any(w in cand for w in _SPOT_GENERIC):
        return None
    return cand


def recommend_answer(city: str, keyword: str, pois: list[dict],
                     client=None) -> str:
    """回答「推荐几家X」：按关键词列出库里的店/景点，事实全部来自库。"""
    if not pois:
        return (f"{city}的资料里我还没收录「{keyword}」相关的店，不敢瞎报店名。\n"
                f"可以说个具体店名，我查到就给你介绍；排行程时选餐厅我也会按"
                f"你的口味来。")
    def _poi_fact(p: dict) -> dict:
        d = {"名称": p.get("name"), "区域": p.get("area"),
             "开放时间": p.get("open_hours"), "备注": p.get("note")}
        # cost 缺失就不放这个键——留 None 模型会说成"门票0元"
        if p.get("cost"):
            d["人均元" if p.get("category") == "restaurant" else "门票元"] = p["cost"]
        return d

    facts = {"城市": city, "问的": f"推荐{keyword}",
             "结果": [_poi_fact(p) for p in pois[:5]]}
    said = _speak(client, "poi", facts, f"推荐几家{keyword}",
                  [p.get("name") for p in pois[:2]])
    if said:
        return said
    lines = [f"**{city} · {keyword}**相关的（按库里的资料）："]
    for p in pois[:5]:
        extra = []
        if p.get("area"):
            extra.append(p["area"])
        if p.get("cost"):
            money = "人均" if p.get("category") == "restaurant" else "门票"
            extra.append(f"{money} {p['cost']} 元")
        if p.get("open_hours"):
            extra.append(p["open_hours"])
        tail = f"（{'，'.join(extra)}）" if extra else ""
        nt = f" —— {p['note']}" if p.get("note") else ""
        lines.append(f"- **{p['name']}**{tail}{nt}")
    lines.append("\n想把哪一家排进行程，直接说「我想去XX」。")
    return "\n".join(lines)


def nearby_answer(city: str, spot: str, pois: list[dict], client=None) -> str:
    """回答「XX周边/附近有什么」：事实是库里按距离排好的邻近景点。"""
    if not pois:
        return (f"「{spot}」周边的景点数据我收录得还不够，不敢瞎推荐。\n"
                f"可以问「{city}有什么好玩的」，或者直接让我排{city}的行程。")
    facts = {"城市": city, "中心景点": spot,
             "周边": [{"名称": p.get("name"), "区域": p.get("area"),
                       "距离公里": p.get("_km"), "门票元": p.get("cost"),
                       "建议游玩分钟": p.get("duration_min")}
                      for p in pois[:5]]}
    said = _speak(client, "poi", facts, f"{spot}周边有什么好玩的",
                  [p.get("name") for p in pois[:2]] + [spot])
    if said:
        return said
    lines = [f"**{spot}** 周边值得看看的（按距离从近到远）："]
    for p in pois[:5]:
        extra = []
        if p.get("_km") is not None:
            extra.append(f"约 {p['_km']} 公里")
        if p.get("area"):
            extra.append(p["area"])
        if p.get("cost"):
            extra.append(f"门票 {p['cost']} 元")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"- **{p['name']}**{tail}")
    lines.append("\n想把哪个加进行程？说「我想去XX」就行。")
    return "\n".join(lines)


def transport_answer(text: str, origin: str | None, dest: str | None,
                     client=None) -> str:
    """直接回答交通问题：两个城市都有就查真实班次，缺就问缺的那个。"""
    if not origin and not dest:
        return "你问的是哪条线路？告诉我从哪儿到哪儿（比如「杭州到上海」），我查真实班次。"
    if not dest:
        return f"从{origin}出发，想去哪个城市？"
    if not origin:
        return f"去{dest}之前，你从哪个城市出发？"
    from . import tools
    r = tools.search_transport(origin, dest, date.today().isoformat(), "any", 1)
    if not r.get("ok"):
        return (f"我没查到 {origin} → {dest} 的直达班次数据（这条线路库里还没有）。"
                f"\n可以换个城市组合，或者我给你按中转方案估个时间。")
    items = (r.get("items") or [])[:4]
    facts = {"出发": origin, "到达": dest,
             "班次": [{"车次": it.get("code"), "发车": it.get("depart"),
                       "到达时刻": it.get("arrive"), "方式": it.get("mode"),
                       "票价元": it.get("price")} for it in items],
             "数据来源": r.get("note", "")}
    said = _speak(client, "transport", facts, text,
                  [items[0].get("code"), items[0].get("price")] if items else None)
    if said:
        return said + "\n\n要我把这段交通串进行程里吗？告诉我天数和人数就行。"

    lines = [f"{origin} → {dest} 的班次（{r.get('note', '')}）："]
    for it in items:
        lines.append(f"- {it.get('code', '')} {it.get('depart', '')}→{it.get('arrive', '')}"
                     f"，{it.get('mode', '')}，¥{it.get('price', '')}")
    lines.append("\n要我把这段交通串进行程里吗？告诉我天数和人数就行。")
    return "\n".join(lines)


SMALLTALK_REPLIES = {
    "身份": "我是这个产品里的旅行规划助手，背后接的是后台配置的大模型（OpenAI 兼容接口，"
            "可以换），不是某个固定品牌。\n"
            "我能查真实天气预报、真实路网耗时、在售班次和酒店，也能把这些排成可执行的行程。\n"
            "可以直接问我某地天气，也可以说「下个月去大理玩 4 天，2 人，预算 6000」。",
    "感谢": "不客气～还想问哪座城市的天气，或者要我直接排一版行程？",
    "告别": "随时回来问天气或排行程，我在。",
    "问候": "在的。想了解哪儿的情况？天气、班次、景点都能直接问，也可以让我排行程。",
}


def smalltalk_answer(text: str, client=None) -> str:
    t = text or ""
    if client is not None:
        scene = ("身份" if is_identity_question(t)
                 else "追问" if any(k in t for k in _FOLLOWUP_KW)
                 else "感谢" if any(k in t for k in ("谢谢", "谢了"))
                 else "告别" if any(k in t for k in ("再见", "拜拜")) else "问候")
        facts = {"场景": scene,
                 "能做的事": ["查任意城市的实时天气", "查城际班次与票价",
                              "列某城市的真实景点", "按日期/天数/预算直接排行程"],
                 "能排具体行程的城市": _SERVICEABLE_HINT}
        said = _speak(client, "smalltalk", facts, t)
        if said:
            return said
    if is_identity_question(t):
        return SMALLTALK_REPLIES["身份"]
    if any(k in t for k in ("谢谢", "谢了")):
        return SMALLTALK_REPLIES["感谢"]
    if any(k in t for k in ("再见", "拜拜")):
        return SMALLTALK_REPLIES["告别"]
    return SMALLTALK_REPLIES["问候"]


def other_answer(text: str, last_city: str | None, slots: dict) -> str:
    """听不懂时不要装懂，也不要硬排行程——把能做的事摆出来，问一句。"""
    city = city_in(text) or last_city
    if city:
        return (f"我没太确定你的意思。关于「{city}」我可以直接查：天气、班次、景点；"
                f"也可以按日期天数帮你排行程——你想先看哪个？")
    return ("我主要能帮两件事：查实时信息（某地天气、城际班次、城市景点），"
            "或者把行程排成可执行的一版（给我城市、时间、人数、预算里任意几项就行）。\n"
            "你想先了解哪座城市？")
