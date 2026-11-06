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
          "门票", "开放时间", "营业时间", "闭馆")
TRANSPORT_KW = ("高铁", "动车", "火车", "机票", "航班", "班次", "车次", "票价",
                "怎么去", "怎么走", "多久到", "怎么到")
PLAN_KW = ("行程", "安排", "规划", "计划", "排一下", "排个", "帮我排", "玩几天",
           "几天", "预算", "出发", "度假", "旅行", "旅游", "走一趟", "待几天")
SMALLTALK_KW = ("你是谁", "你叫什么", "你能做什么", "你会什么", "你好", "您好",
                "谢谢", "谢了", "再见", "拜拜", "在吗", "早上好", "晚上好", "晚安")

# 数据覆盖说明：只对这几个城市能排具体行程（真实 POI/酒店）
_SERVICEABLE_HINT = "大理、丽江、香格里拉、杭州、上海、邯郸"


def classify(text: str) -> str:
    """返回 weather / poi / transport / plan / smalltalk / other。

    优先级：规划意图 > 天气 > 景点 > 交通 > 闲聊。
    「北京天气好的话排个行程」这类混合句按规划处理（用户最终要的是行程）。
    """
    t = text or ""
    has_plan = (any(k in t for k in PLAN_KW)
                or re.search(r"玩\s*[一二三四五六七八九十\d]+\s*天", t) is not None
                or re.search(r"去.{2,6}(玩|旅游|旅行|度假)", t) is not None)
    if has_plan:
        return "plan"
    if any(k in t for k in WEATHER_KW):
        return "weather"
    if any(k in t for k in POI_KW):
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
              "问一下", "问下", "告诉我", "查一下", "查下", "看看", "一下", "听说")
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
    changed = True
    while changed and cand:
        changed = False
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
                       "时长分钟": p.get("duration_min"), "门票元": p.get("cost"),
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
            extra.append(f"门票 {p['cost']} 元")
        if p.get("open_hours"):
            extra.append(p["open_hours"])
        tail = f"（{'，'.join(extra)}）" if extra else ""
        nt = f" —— {p['note']}" if p.get("note") else ""
        lines.append(f"- **{p['name']}**{tail}{nt}")
    if note:
        lines.append(f"\n{note}")
    lines.append("\n想把它排进行程？说「我想去" + spot + "」我就重排。")
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
    "身份": "我是本地跑的旅行规划助手：真实天气预报、真实路网耗时、在售班次和酒店都在我这儿查。\n"
            "可以直接问我某地天气，也可以说「下个月去大理玩 4 天，2 人，预算 6000」，我给你排成可执行的行程。",
    "感谢": "不客气～还想问哪座城市的天气，或者要我直接排一版行程？",
    "告别": "随时回来问天气或排行程，我在。",
    "问候": "在的。想了解哪儿的情况？天气、班次、景点都能直接问，也可以让我排行程。",
}


def smalltalk_answer(text: str, client=None) -> str:
    t = text or ""
    if client is not None:
        scene = ("身份" if any(k in t for k in ("你是谁", "你叫什么", "你能做什么", "你会什么"))
                 else "感谢" if any(k in t for k in ("谢谢", "谢了"))
                 else "告别" if any(k in t for k in ("再见", "拜拜")) else "问候")
        facts = {"场景": scene,
                 "能做的事": ["查任意城市的实时天气", "查城际班次与票价",
                              "列某城市的真实景点", "按日期/天数/预算直接排行程"],
                 "能排具体行程的城市": _SERVICEABLE_HINT}
        said = _speak(client, "smalltalk", facts, t)
        if said:
            return said
    if any(k in t for k in ("你是谁", "你叫什么", "你能做什么", "你会什么")):
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
