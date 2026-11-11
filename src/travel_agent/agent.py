"""智能旅行规划助手 · 可运行验证脚本

两种模式：
  1) dry（默认）：离线确定性流程。用规则模拟"抽取 → 追问 → 工具 → 编排 → 核算 → 自检"，
     用于验证提示词里定义的契约、工具参数和输出结构是否自洽，不需要 API Key。
  2) live：真实大模型 function-calling 循环。读取 prompts/01-system-prompt.md 作为
     system message，tools 来自 prompts/02-tool-schemas.json，工具执行走 mock_tools。
     需要环境变量：OPENAI_API_KEY，可选 OPENAI_BASE_URL、OPENAI_MODEL。

用法：
    python agent.py                          # dry 模式，跑内置用例
    python agent.py --utterance "..."        # dry 模式，跑自定义输入
    python agent.py --mode live --utterance "..."
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from . import autofetch
from . import llm as llm_layer
from . import real_tools
from .composer import compose, name_hit
from .tools import POIS_DB, dispatch

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"

# 出发地没有默认城市（2026-11-02 决策）：用户没说就不安排往返大交通。
# 以前的 DEFAULT_ORIGIN="杭州" 会让成都出发的用户收到「杭州-大理」的方案。


# ------------------------------------------------------------------ 1. 抽取

CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> int | None:
    """中文数字 → int（一~九十九，覆盖天数与日期的取值范围）。"""
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if any(ch not in CN_DIGIT and ch != "十" for ch in s):
        return None
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        return (CN_DIGIT.get(a, 1) if a else 1) * 10 + (CN_DIGIT.get(b, 0) if b else 0)
    return CN_DIGIT.get(s)


def _safe_date(y: int, m: int, d: int) -> date:
    """日子超出当月（如 2月30日）时收敛到月末，不让解析崩溃。"""
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


# 「20号到23号」「20日至23日」「20-23号」：用户给了起止，天数就该算出来，
# 不该再问一句"玩几天"。之前只认开始日期，说了"到23号"还要被追问一次。
_RANGE_END_RE = re.compile(
    r"(?:到|至|~|—|－|-|一直到)\s*(?:(\d{1,2})\s*[月/]\s*)?(\d{1,2})\s*[日号]")


def _range_end(text: str, start: date | None = None,
               start_day: int | None = None) -> date | None:
    """从"到 23 号"里推出结束日期。无法确定或跨得离谱时返回 None。"""
    m = _RANGE_END_RE.search(text)
    if not m:
        return None
    try:
        day = int(m.group(2))
        month = int(m.group(1)) if m.group(1) else None
    except (TypeError, ValueError):
        return None
    if not 1 <= day <= 31:
        return None
    if start is not None:
        mo, yr = (month or start.month), start.year
        if month and month < start.month:      # 11月20日到1月3日 → 跨年
            yr += 1
        end = _safe_date(yr, mo, day)
        if end < start:                        # 只说日：23号 < 20号 → 下个月
            yr, mo = (yr + 1, 1) if mo == 12 else (yr, mo + 1)
            end = _safe_date(yr, mo, day)
        if 1 <= (end - start).days + 1 <= 30:
            return end
        return None
    if start_day is not None and start_day < day <= 31:
        return date(2000, 1, day)              # 只用于算天数，日期本身无意义
    return None


# 出发地提取时剥掉的人称/意图前缀（长词在前，避免被短词截断）
_STRIP_PREFIX = re.compile(
    r"^(?:一个人|两个人|我们俩|他俩|我俩|我们|你们|他们|她们|大家|一起|"
    r"打算|准备|计划|想要|想去|想从|要去|想|我|你)*")

# 正则会把「想出去玩几天」里的「玩几天」当成目的地，这些不是地名
_NON_CITY_WORDS = {
    "玩几天", "几天", "多少天", "多久", "一趟", "玩玩", "玩一玩", "什么",
    "哪里", "哪儿", "这里", "那里", "那边", "这边", "附近", "随便", "都行",
    "玩的", "去的", "行程", "安排",
}

# 景点抽取的片头/片尾虚词：「…了，还是加回来吧」会被倒装正则截出「还是」，
# 「再安排上」会截出「再」。它们不可能是景点名。
_STOP_FRAG = (
    "还是", "再", "也", "又", "都", "就", "帮", "请", "麻烦",
    "重新", "恢复", "顺便", "另外", "那个", "这个", "之前", "原本",
)


_KNOWN_CACHE: list[str] | None = None


def _known_cities() -> list[str]:
    """可用于目的地识别的城市名：有坐标的地级政区（351）+ 有 POI 数据的城市。

    旧实现是写死的 6 个城市名，于是「再帮我排个西安的」这种不带"去/到"的说法
    识别不出目的地——用户明明换了城市，系统还拿上一个城市答天气。
    有坐标的城市既能查天气、也能在线补全景点，认它们不会产生假目的地。
    按长度倒序：先匹配「香格里拉」再匹配同前缀的短名。
    """
    global _KNOWN_CACHE
    if _KNOWN_CACHE:
        return _KNOWN_CACHE
    names = set(real_tools.CITY_GEO)
    try:
        names.update(POIS_DB)
    except Exception:
        pass
    try:
        from . import kb as _kb
        _db = _kb.get_kb()
        if _db:
            names.update(_db.cities())
    except Exception:
        pass
    _KNOWN_CACHE = sorted(names, key=lambda c: (-len(c), c))
    return _KNOWN_CACHE


# 「中山路」「上海站」「大理机场」里的城市名不是目的地。
# 只拦"城市名恰好是别人的前缀"的场合（中山陵→南京、上海站→车站）：
# 「大理古城」「杭州西湖」里的城市就是目的地，古城/西湖/园林不能拦。
_CITY_BAD_TAIL = ("路", "街", "巷", "站", "场", "院", "校", "楼", "桥", "塔",
                   "公园", "广场", "机场", "大学", "医院", "陵", "寺", "庙", "宫")


def _in_text(city: str, text: str) -> bool:
    """城市名在句子里出现，且不是「XX路/站/机场」这类地名的一部分。"""
    for m in re.finditer(re.escape(city), text):
        tail = text[m.end():m.end() + 2]
        if any(tail.startswith(w) for w in _CITY_BAD_TAIL):
            continue
        return True
    return False


# 多城连写的连接成分：「成都、重庆」「成都和重庆」「先成都后重庆」。
# 必须非空——「南京中山陵」里南京和中山（市）紧贴，是城市+景点不是多城；
# 「的」也不算连接词，「南京的中山陵」不是多城。
_MULTI_JOIN_RE = re.compile(r"^[、，,\s和与跟及先还后]+$")


def split_multi_city(text: str, known: list[str] | None = None) -> list[str]:
    """按出现顺序找出文本里"连写"的多个已知城市。

    「去成都、重庆玩」→ [成都, 重庆]；「先成都后重庆」→ [成都, 重庆]。
    两个城市之间隔着正常语句（「从上海出发…去成都」）不算——出发地
    由调用方预先从文本里剥掉。单城或找不到返回空列表。
    """
    t = text or ""
    known = known or _known_cities()
    occ: list[tuple[int, int, str]] = []
    for c in known:
        start = 0
        while (i := t.find(c, start)) != -1:
            occ.append((i, i + len(c), c))
            start = i + 1
    if len(occ) < 2:
        return []
    occ.sort()
    groups: list[list] = []       # [end_pos, [cities]]
    for s, e, c in occ:
        if groups and s >= groups[-1][0] and _MULTI_JOIN_RE.match(t[groups[-1][0]:s]) \
                and c not in groups[-1][1]:
            groups[-1][1].append(c)
            groups[-1][0] = e
        else:
            groups.append([e, [c]])
    best = max(groups, key=lambda g: len(g[1]))
    return best[1] if len(best[1]) >= 2 else []


def normalize_multi_dest(slots: dict, text: str) -> None:
    """目的地槽位混进多个城市时剥出来（就地修改）。

    「去成都、重庆玩5天」当前架构一次只能排一个城市：城市串绝不能
    当 destination（会被追问"在哪个城市"再被挂成景点），也不能混进
    must_visit。拆成 multi_cities 交给对话层明确说明并追问先排哪个。
    """
    known = _known_cities()
    t = text or ""
    # 剥掉出发地出现的位置：「从上海出发…成都、重庆」里上海是出发地
    for m in re.finditer(r"从([\u4e00-\u9fa5]{2,4}?)(?:出发|走|去|到|飞|动身|坐|搭)", t):
        t = t[:m.start(1)] + "\x00" * len(m.group(1)) + t[m.end(1):]
    dest = slots.get("destination")
    cands = split_multi_city(dest, known) if dest else []
    if len(cands) < 2:
        cands = split_multi_city(t, known)
        # 目的地已抽出单城时，文本里拆出的城市组必须包含它才算多城请求，
        # 否则无关场合提到的城市组合会误伤正常单城请求
        if dest and cands and dest not in cands:
            cands = []
    if len(cands) >= 2:
        slots["multi_cities"] = cands
        slots["destination"] = None


def is_city(name: str | None) -> bool:
    """这个名字是不是一座"能排行程"的城市，而不是景点/商圈/写错的地名。

    「安排去迪士尼的行程」里的迪士尼会被目的地正则抽成城市，系统随后
    一本正经地回「没有迪士尼的景点和酒店数据」——它压根不是城市。
    区分开之后，景点名可以归位到它所在的城市，而不是走"城市没覆盖"分支。
    """
    if not name:
        return False
    if name in real_tools.CITY_GEO:
        return True
    try:
        from . import kb as _kb
        db = _kb.get_kb()
        if db and name in db.cities():
            return True
    except Exception:
        pass
    try:
        return real_tools.resolve_geo(name) is not None
    except Exception:
        return False


# 「云南」「四川」是省级，不是城市也不是景点。用户说省名时正确的做法是
# 请他具体到城市，而不是把它当景点塞进某个城市的行程（库里有条名字就叫
# "云南"的 POI，双向包含会把整省匹配成丽江的一个点）。
_REGION_WORDS = {
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾", "内蒙古", "广西", "西藏", "宁夏",
    "新疆", "华北", "华东", "华南", "西南", "西北", "东北", "江南",
}


def is_region(name: str | None) -> bool:
    """省/自治区/大区名——需要用户具体到城市，不能当景点也不能当城市。"""
    n = (name or "").strip()
    if not n:
        return False
    return n.rstrip("省市自治区") in _REGION_WORDS or n in _REGION_WORDS


# 「迪士尼」「乐园」「古镇」这种词太泛，不能凭名字判定成某个具体景点
_SPOT_TOO_GENERIC = {"乐园", "公园", "古镇", "古城", "景区", "博物馆", "动物园",
                     "海洋馆", "步行街", "广场", "度假区", "风景名胜区"}


def spot_city(name: str, prefer: str | None = None
              ) -> tuple[str, str] | None:
    """把一个景点名归到它所在的城市：`迪士尼` → `("上海", "上海迪士尼乐园")`。

    名字来自用户口语，常只有核心词（迪士尼/外滩/兵马俑），而库里存的是全名。
    匹配用双向包含（与编排器同一套 name_hit），选城市的顺序是：
    名字里直接带城市（香港迪士尼→香港）> 当前行程的城市 > 全名最短的命中。
    找不到返回 None——绝不猜一个城市出来。
    """
    name = (name or "").strip()
    if (len(name) < 2 or name in _SPOT_TOO_GENERIC or is_city(name)
            or is_region(name)):
        return None

    hits: list[tuple[str, str]] = []
    for city, pois in (POIS_DB or {}).items():
        for p in pois:
            pn = p.get("name") or ""
            if pn and name_hit(name, pn):
                hits.append((city, pn))
    try:
        from . import kb as _kb
        db = _kb.get_kb()
        if db:
            for row in db.search_pois(q=name, limit=20):
                hits.append((row["city"], row["name"]))
    except Exception:
        pass
    if not hits:
        return None

    for city, pn in hits:                  # 名字自带城市前/后缀的最可信
        if city and city in name:
            return (city, pn)
    if prefer:
        for city, pn in hits:
            if city == prefer:
                return (city, pn)
    hits.sort(key=lambda h: (len(h[1]), h[1]))
    return hits[0]


def looks_like_spot(name: str | None) -> bool:
    """这名字像不像一个景点（而不是城市）：库里有同名的点，或以景点通名结尾。

    用途是拦住"把景点当城市去联网抓一圈数据"：用户说「迪士尼」，不该走
    ensure_city 假装去补一座叫迪士尼的城市，再回来说"没有数据"。
    但没收录的城市（样例城这种）不能因此被拦，所以只认通名和库内命中。
    """
    n = (name or "").strip()
    if not n or is_city(n) or is_region(n):
        return False
    if spot_city(n):
        return True
    return n.endswith(("乐园", "公园", "古镇", "古城", "古城", "景区", "风景区",
                       "博物馆", "博物院", "动物园", "海洋馆", "广场", "步行街",
                       "度假区", "小镇", "基地", "温泉", "滑雪场", "美术馆",
                       "纪念馆", "寺", "宫", "塔", "岛", "山"))


# 景点名里不可能出现的词：时刻/班次改动词。这类碎片来自误解析
# （「把去程到达时间改为 09:00」曾截出「达时间改为」），一旦入库
# 会被当成位置估算景点反复排进新行程——用户实测污染了全新的成都方案。
_BAD_POI_RE = re.compile(r"时间|时刻|改为|改成|改到|班次|车次|航班|高铁|去程|返程|回程")


def valid_poi_name(name: str | None) -> bool:
    """这个名字有没有资格当景点：一句话、没标点、不是城市/省份/虚词。

    规则抽取与 LLM 抽取共用这一道闸，autofetch 落库前也过它——
    脏名字进来一次，就会在知识库里被反复命中最久（实测「达时间改为」
    被当成成都景点排进 Day2，还带预约/门票/位置估算说明）。
    """
    n = (name or "").strip()
    if not (2 <= len(n) <= 20):
        return False
    if re.search(r"[、，,。；;！!？?\s]", n):        # 景点名没有标点
        return False
    if _BAD_POI_RE.search(n):                        # 时刻/班次碎片
        return False
    if n in _NON_CITY_WORDS or is_region(n) or is_city(n):
        return False
    # 疑问/代词只认整词：「什刹海」是景点，不能因为以"什"开头就被否
    if re.match(r"^(?:什么|哪儿|哪里|哪个|几个|多少|多久)", n):
        return False
    if re.fullmatch(r"[一二两三四五六七八九十\d]+\s*[天人均日个位]", n):
        return False
    # 虚词整词否掉；多字虚词才按首尾剥——单字（都/就/也/又）做首尾判断
    # 会误杀真实景点：「都江堰」曾因此被判成噪音（代码注释里就在用它举例）
    if n in _STOP_FRAG:
        return False
    if any(len(w) >= 2 and (n.startswith(w) or n.endswith(w)) for w in _STOP_FRAG):
        return False
    return True


def _is_place(name: str | None) -> bool:
    """候选串是不是一个像样的地名。虚词/疑问词必须拦掉：
    「你不问我从哪里出发吗」会被正则抽成出发地「哪里」，编排器随后真的
    去查「哪里→大理」的班次，方案里还会写出"哪里-大理 暂无班次"。"""
    if not name or not (2 <= len(name) <= 4):
        return False
    if name in _NON_CITY_WORDS or re.match(r"^[几多少什么哪]", name):
        return False
    return True


def _extract_route(text: str) -> tuple[str | None, str | None]:
    """识别"A到B / 从A出发"里的出发地与目的地候选。

    "一个人邯郸到上海" 这种没有"从"字的写法之前完全漏掉，导致编排器
    永远按默认出发地杭州查交通、查出来的班次与用户无关。
    返回 (origin, destination)，都可能为 None。
    """
    if m := re.search(r"从([\u4e00-\u9fa5]{2,4}?)(?:出发|走|去|到|飞|动身|坐|搭)", text):
        # 「从杭州去大理」是最常见的说法，之前只认"出发/走/飞"，导致出发地
        # 永远抽不到、编排器悄悄按杭州算——用户从成都走也给他排杭州的班次。
        # 这里必须用非贪婪：{2,4} 贪心会把「成都出发」整体当地名。
        return m.group(1) if _is_place(m.group(1)) else None, None
    if m := re.search(r"([\u4e00-\u9fa5]{2,10}?)[到→]"
                      r"([\u4e00-\u9fa5]{2,4})(?=[玩旅，。！？!?,\s]|$)", text):
        a = _STRIP_PREFIX.sub("", m.group(1))
        if len(a) > 4:
            a = a[-3:]          # 剥不干净时取末几字（多为城市名）
        origin = a if _is_place(a) else None
        dest = m.group(2) if _is_place(m.group(2)) else None
        return origin, dest
    return None, None


def strip_pace_prefs(slots: dict) -> dict:
    """把混进 soft_prefs 的节奏词清掉（「轻松休闲」≠ 兴趣）。

    LLM 抽取侧已经拦了一道（llm.coerce_slots），这里兜住另外几条路：
    多轮会话继承下来的旧槽位、调用方手工构造的 slots、以后新增的抽取入口。
    清空 soft_prefs 只影响"兴趣"的说明与打分，节奏仍由 pace 表达。
    """
    prefs = slots.get("soft_prefs") or []
    keep = [p for p in prefs
            if isinstance(p, str) and not llm_layer.is_pace_word(p)]
    if len(keep) != len(prefs):
        slots["soft_prefs"] = keep
    return slots


def strip_spot_interests(slots: dict) -> dict:
    """兴趣里混进"某个具体景点"要清掉——那是行程，不是偏好。

    两个真实反馈：
    1. 点名「都江堰」后，模型又把它抽进 soft_prefs，于是排进行程的那一站
       额外挂一条「兴趣·都江堰」——必去点看起来被重复命中，纯噪音。
    2. 点名「人民公园」后，兴趣里出现泛词「公园」，它会把望江楼公园、
       甚至备注写着"人民公园内"的鹤鸣茶社一起标成「兴趣·公园」，让用户
       以为餐馆是按公园兴趣安排的。

    判定只有一条：兴趣词与某个点名/排除的景点指同一个地方（name_hit 双向，
    含核心词档）——那就不是兴趣。泛词「公园」命中「人民公园」也走这条，
    因为它本来就是景点名被截断的产物。
    """
    spots = [s for s in (slots.get("must_visit") or []) + (slots.get("avoid") or [])
             if isinstance(s, str) and s]
    prefs = slots.get("soft_prefs") or []
    keep = [p for p in prefs
            if isinstance(p, str) and not any(name_hit(p, s) for s in spots)]
    if len(keep) != len(prefs):
        slots["soft_prefs"] = keep
    return slots


def sanitize_prefs(slots: dict) -> dict:
    """soft_prefs 的统一出口：先摘节奏词，再摘"其实是景点"的词。"""
    return strip_spot_interests(strip_pace_prefs(slots))


def extract_slots(text: str, today: date | None = None,
                  client: "llm_layer.Client | None" = None,
                  records: list | None = None) -> dict:
    """槽位抽取：有 LLM 走 LLM，校验失败或不可用时静默回落到规则版。

    规则版不是"过时的备份"——它承担两个职责：LLM 未配置时的默认实现，
    以及 LLM 输出不合规时的降级路径。两者输出结构完全一致，下游无感知。
    """
    today = today or date.today()
    if client is None:
        slots = extract_slots_rules(text, today)
        slots["_source"] = "rules"
        return sanitize_prefs(slots)

    slots, rec = llm_layer.extract_slots_llm(client, text, today)
    if records is not None:
        records.append(rec)
    if slots is not None:
        # 模型偶发漏抽："20号"这种裸日子它常返回 null，而规则版稳稳拿得到。
        # 对模型给 None 的字段用规则结果补齐（只补空，不覆盖模型判断），
        # 否则用户明明答了日期，助手还会把同一个问题问第二遍。
        rule_slots = extract_slots_rules(text, today)
        for key, val in rule_slots.items():
            if key.startswith("_") or val in (None, "", [], {}):
                continue
            if slots.get(key) in (None, "", [], {}):
                slots[key] = val
            elif (key == "date_range" and isinstance(slots.get(key), dict)
                  and isinstance(val, dict)):
                # 模型给一半（月份）、规则给另一半（日子）：合并，模型的键优先
                merged = dict(val)
                merged.update(slots[key])
                slots[key] = merged
        slots["_source"] = "llm"
        # 人数总数必须与构成自洽：模型常照字面把「1个人」回成 party_size=1，
        # 而「1 大 1 小」构成（婴儿车→1 小）来自规则补齐——总数以构成之和
        # 为准，否则概要「1 大 1 小，共 1 人」、预算按 1 人算（亲子实测）
        if (slots.get("adults") is not None
                and slots.get("children") is not None
                and slots.get("party_size") !=
                    slots["adults"] + slots["children"]):
            slots["party_size"] = slots["adults"] + slots["children"]
        # 模型会把「去成都、重庆玩」整串回给 destination（提示词只说"城市名"）：
        # 城市串进下游会被追问"在哪个城市"、还会被挂成位置估算景点（实测）。
        # 多城请求剥成 multi_cities，与规则路径行为一致。
        normalize_multi_dest(slots, text)
        # 模型给的必去/排除同样要过景点名校验：城市串、时刻碎片、省份名
        # 一旦留下来，autofetch 会把它当"未收录景点"落库并长期污染
        for key in ("must_visit", "avoid"):
            slots[key] = [m for m in (slots.get(key) or [])
                          if isinstance(m, str) and valid_poi_name(m)]
        return sanitize_prefs(slots)

    slots = extract_slots_rules(text, today)
    slots["_source"] = "rules"
    slots["_degraded"] = rec.degraded
    return sanitize_prefs(slots)


def extract_slots_rules(text: str, today: date | None = None) -> dict:
    """规则版槽位抽取器，对应 prompts/04 子提示词 A 的输出结构。"""
    today = today or date.today()
    # 交通时刻修改从句先剥掉：「把去程到达时间改为 09:00」的「到达时间
    # 改为」会被目的地兜底正则（去|到|玩…)吃成城市「达时间改为」，污染
    # 必去景点、假设和行前准备（用户实测）。剥完照常抽取其余槽位。
    text = re.sub(r"[^，。；,;]{0,8}(?:去程|返程|回程|出发|到达|抵达|发车|班次|车次|航班)"
                  r"[^，。；,;]{0,6}(?:时间|时刻)[^，。；,;]{0,10}"
                  r"(?:改为|改成|改到|换到|换成|改一下|修改|调整|提前|推迟|定在)"
                  r"[^，。；\s]{0,8}", "，", text)
    slots: dict = {
        "destination": None, "origin": None, "date_range": None, "days": None,
        "party_size": 1, "budget_total": None, "budget_per_person": None,
        "hard_prefs": [], "pace": "moderate",
        "must_visit": [], "avoid": [], "stay_pref": None,
        # 人数构成：「2大1小（3岁）」里的大人/孩子拆开存，儿童票、家庭房、
        # 午休安排都要用。party_size 永远是总人数（含孩子）。
        "adults": None, "children": None, "child_age": None,
        "party_detail": None,
    }
    uncertain: list[str] = []

    origin_r, dest_r = _extract_route(text)
    if origin_r:
        slots["origin"] = origin_r
    # 已知城市优先：整词命中最可靠，避免正则把「丽江吧」截成目的地。
    # 这里必须覆盖全部有坐标的地级政区（351 个），不能只认几个写死的城市：
    # 「再帮我排个西安的」不带"去/到"，旧实现认不出西安，用户换了城市也当没换。
    known = _known_cities()
    # 目的地的可信度排序：「A到B」明确给出的 B > 「去/到/往」后面紧跟的已知城市
    # > 句子里任意位置出现的已知城市 > 正则兜底的生词。
    # 之前只按 known 列表顺序取第一个命中，于是「从大理到丽江」被抽成了大理。
    # (?<![不别])(?<!不想)(?<!不用)：'不想去豫园'里'去'的前一个字是'想'，
    # 单字环视挡不住，必须再看一位——否则'豫园'会被当成目的地，
    # 还连带把'豫园'从排除清单里过滤掉（frag==destination 被丢弃）。
    # 「去掉苍山」的「去」是否定/删除动词的一部分——(?!掉|除) 挡住，
    # 否则会截出「掉苍山」当地点/景点，还写进 KB 造成数据污染（实测闭环）。
    _m_go = re.search(r"(?<![不别])(?<!不想)(?<!不用)(?:去|到|往|飞)(?!掉|除)([\u4e00-\u9fa5]{2,4})", text)
    _go_city = next((c for c in known
                     if _m_go and _in_text(c, _m_go.group(1))), None)
    dest = dest_r or _go_city or next((c for c in known if _in_text(c, text)), None)
    if not dest:
        # 「安排去迪士尼的行程」「去迪士尼的行程」：旧正则只认「去X玩/去X吧」，
        # 撞上"…的行程"整句一个目的地都抽不出来，于是被当成"用户没说去哪儿"，
        # 追问一句"想去哪儿"——用户明明刚说了要去迪士尼。
        if m := re.search(r"(?<![不别])(?<!不想)(?<!不用)(?:去|到|玩|安排去)(?!掉|除)"
                          r"([\u4e00-\u9fa5]{2,6}?)(?:的)?"
                          r"(?:行程|旅游|旅行|路线|计划|安排|玩|逛|吧|呀|啊|呢|哦|嘛|"
                          r"，|,|。|$|\s)", text):
            dest = m.group(1)
    if dest:
        # 兜底剥离句尾语气词（非贪婪正则可能拿不到边界时）
        dest = re.sub(r"[吧呀啊呢哦嘛了]+$", "", dest)
        # 兜底正则的第二道闸：时刻/班次类词混进来的碎片绝不是城市
        # （「达时间改为」「车次改成」这类，防线一剥漏时在这兜住）
        if len(dest) >= 2 and dest not in _NON_CITY_WORDS \
                and not re.search(r"时间|时刻|改为|改成|改到|提前|推迟|班次|车次|航班|高铁", dest) \
                and not re.match(r"^[几多少什么哪]", dest):
            slots["destination"] = dest

    if m := re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text):
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year + (1 if month < today.month else 0)
        start = _safe_date(year, month, day)
        if start < today:               # 当月已过的日子 → 顺延到明年
            start = _safe_date(year + 1, month, day)
        slots["date_range"] = {"start": start.isoformat(), "exact": True}
        # 给了起止日期就直接算天数：「11月20号到23号」= 4 天，
        # 不该再追问"玩几天"——用户刚说过。
        if end := _range_end(text, start=start):
            slots["date_range"]["end"] = end.isoformat()
            slots["days"] = (end - start).days + 1
    elif (m := re.search(r"下个?月\s*(?:的)?\s*([一二两三四五六七八九十\d]{1,3})\s*[日号]", text)):
        # "下个月一号出发"：月和日都给了，就是完整日期，不必再折算或追问
        d = cn_to_int(m.group(1))
        if d and 1 <= d <= 31:
            y = today.year + (1 if today.month == 12 else 0)
            mo = today.month % 12 + 1
            slots["date_range"] = {"month": f"{y}-{mo:02d}", "day": d, "exact": True}
    elif (m := re.search(r"(?:这个|本)月\s*(?:的)?\s*([一二两三四五六七八九十\d]{1,3})\s*[日号]", text)):
        d = cn_to_int(m.group(1))
        if d and 1 <= d <= 31:
            # this_month：日子若已过，指下个月的同一天（最近将来的那个 20 号），
            # 而不是像明说"10月20号"那样顺延一年
            slots["date_range"] = {"month": f"{today.year}-{today.month:02d}",
                                   "day": d, "exact": True, "this_month": True}
    elif re.search(r"(?:这个|本)月", text):
        # 只说了"这个月"：记月份，具体日子留给 resolve_date_range 折算或后续补答
        slots["date_range"] = {"month": f"{today.year}-{today.month:02d}",
                               "exact": False, "this_month": True}
        uncertain.append("date_range")
    elif (m := re.search(r"([一二两三四五六七八九十\d]{1,3})\s*号(?![日号\d])", text)):
        # 裸"20号"：单独回答"几号走"的常见说法。之前完全不认，导致助手把
        # 同一个问题问两遍；这里只记日子，月份由 resolve_date_range 按
        # "本月优先、已过顺延下月"补齐。
        d = cn_to_int(m.group(1))
        if d and 1 <= d <= 31:
            slots["date_range"] = {"day": d}
            # 「20号到23号」：起止都说了，天数直接算出来
            if (end := _range_end(text, start_day=d)) is not None:
                slots["days"] = end.day - d + 1
    elif m := re.search(r"下个?月", text):
        slots["date_range"] = {"month": f"{today.year}-{today.month % 12 + 1:02d}", "exact": False}
        uncertain.append("date_range")

    _spot_day = None   # 「给X安排一天 / 安排一天去X」里要留整天的那个点
    if m := re.search(r"([一二两三四五六七八九十\d]{1,3})\s*天", text):
        n = cn_to_int(m.group(1))
        pre = text[max(0, m.start() - 2):m.start()]
        # 「安排/留/住 + N天」有两种常见含义，都不是"整趟 N 天"：
        #   ① 句里有加点的说法（「加上都江堰，安排一天」）→ 给那个点留时长；
        #   ② 「给X安排一天」「安排一天去X」→ X 才是要去的地方。
        # 单独的「帮我安排三天行程」仍是整趟天数，照常生效。
        _spot_ctx = bool(re.search(r"加上|增加|添上|顺便去|顺便逛|再去|也去", text)) \
            or bool(re.search(r"给[\u4e00-\u9fa5A-Za-z0-9]{2,8}安排", text)) \
            or bool(re.search(r"[一二两三四五六七八九十\d]+\s*天\s*(?:去|逛)", text))
        if n and 1 <= n <= 30:
            if re.search(r"(?:安排|排|留|住)$", pre) and _spot_ctx:
                # 「给都江堰安排一天 / 安排一天去熊猫基地」：给点留时长，天数不动。
                # 点名的校验要用 _valid_poi（在下方定义），先记下候选，稍后落库。
                for pat in (r"给\s*([\u4e00-\u9fa5A-Za-z0-9]{2,8}?)\s*安排",
                            r"[一二两三四五六七八九十\d]+\s*天\s*(?:去|逛)"
                            r"\s*([\u4e00-\u9fa5A-Za-z0-9]{2,10}?)(?=[，。,；;\s]|$)"):
                    sm = re.search(pat, text)
                    if sm and sm.group(1):
                        _spot_day = sm.group(1)
                        break
            elif re.search(r"(?:延长|多玩|多住|多待|加)$", pre):
                # 「多住一天 / 延长两天 / 加一天」是顺延，不是"整趟 N 天"；
                # 基数在上一轮天数里，规则层先记增量，对话层（有上下文）落值
                slots["days_extend"] = n
            else:
                slots["days"] = n
    # ---- 人数：支持「2大1小（3岁）」「一家三口」「我和老婆带5岁的娃」----
    # 旧实现只认「N个人/我和老婆/孩子」三种句式，"2大1小"四个字全都不匹配，
    # 孩子被整个丢掉：餐饮、市内交通、门票全按大人的人数估，预算系统性低估。
    _adults: int | None = None
    _kids: int | None = None
    _fm = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*大(?:人)?\s*"
                    r"([一二两三四五六七八九十\d]{1,3})\s*(?:个)?"
                    r"(?:小|小孩|儿童|孩子|娃)", text)
    if _fm:
        _adults = cn_to_int(_fm.group(1))
        _kids = cn_to_int(_fm.group(2))
    else:
        _ma = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*(?:个)?(?:大人|成人)",
                        text)
        _mc = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*(?:个)?"
                        r"(?:小孩|儿童|孩子|娃)", text)
        if _ma:
            _adults = cn_to_int(_ma.group(1))
        if _mc:
            _kids = cn_to_int(_mc.group(1))
    _mk = re.search(r"一家([三四五六])口", text)
    if _mk and _adults is None and _kids is None:
        _adults, _kids = 2, (cn_to_int(_mk.group(1)) or 3) - 2
    _child_age = None
    # 「婴儿车友好」「租个推车」没提孩子，但推车本身就意味着带幼儿——
    # 亲子慢游实测里用户只写了"婴儿车友好、少台阶"，孩子数被漏算
    if (re.search(r"孩子|娃|宝宝|小孩|儿童|儿子|女儿|婴儿车|推车", text)
            or _fm or _mc):
        _ma2 = re.search(r"(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*岁", text)
        if _ma2:
            _a = cn_to_int(_ma2.group(1))
            if _a is not None and 0 <= _a <= 17:
                _child_age = _a
    # 「我和老婆/我们俩」补充大人数；提到孩子但没说几个 → 按 1 个算
    if _adults is None and re.search(r"我和(?:老婆|老公|爱人|对象)|夫妻"
                                     r"|(?:我们|我)(?:俩|两)", text):
        _adults = 2
    if _kids is None and (re.search(r"孩子|娃|宝宝|小孩|儿童|儿子|女儿|婴儿车|推车",
                                    text) or _child_age is not None):
        _kids = 1
    # 「N个人」显式总数：有构成信息时用来推大人数量，没有时直接当总人数
    _n_total = None
    _mn = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*(?:个)?人"
                    r"(?!\s*[¥￥]?\s*\d{3,5})", text)
    if _mn:
        _t = cn_to_int(_mn.group(1))
        if _t and 1 <= _t <= 20:
            _n_total = _t
    if _adults is None and _kids is not None and _n_total:
        _adults = max(_n_total - _kids, 1)
    if _adults is not None or _kids is not None:
        # 只说「带孩子」没提大人：按 1 大 1 小（沿用旧口径，不凭空造同行人）
        _adults = _adults if _adults is not None else 1
        _kids = _kids or 0
        if 1 <= _adults + _kids <= 20:
            slots["adults"] = _adults
            slots["children"] = _kids
            slots["child_age"] = _child_age
            slots["party_size"] = _adults + _kids
            if _kids:
                slots["party_detail"] = (f"{_adults} 大 {_kids} 小"
                                         + (f"（孩子 {_child_age} 岁）"
                                            if _child_age is not None else ""))
            elif re.search(r"我和(?:老婆|老公|爱人|对象)|夫妻", text):
                slots["party_detail"] = "夫妻2人"
    elif _n_total:
        # 「两个人」「三个人」原先只认阿拉伯数字，中文数字一律漏掉，
        # 结果用户说"两个人"系统还是按 1 人算房型和预算。
        # 结尾的负向断言挡住「预算一人3000」——那是人均预算，不是 1 个人。
        slots["party_size"] = _n_total

    if m := re.search(r"一人\s*(\d{3,5})|人均\s*(\d{3,5})", text):
        slots["budget_per_person"] = int(m.group(1) or m.group(2))
    if m := re.search(r"(?:总共|一共)\s*(\d{3,5})", text):
        slots["budget_total"] = int(m.group(1))
    # 注意："预算一人 4000" 是人均，不是总额。已识别到人均时不能再走这条，
    # 否则放宽后的正则会把"一人"当成连接词，把人均误读成总额。
    if (not slots["budget_total"] and not slots["budget_per_person"]
            and (m := re.search(r"预算[^\d]{0,6}(\d{3,5})", text))):
        slots["budget_total"] = int(m.group(1))
    if slots["budget_per_person"] and not slots["budget_total"]:
        slots["budget_total"] = slots["budget_per_person"] * slots["party_size"]

    for pref, keywords in {
        "不吃辣": ("不吃辣", "不能吃辣", "微辣都不行"),
        "忌高海拔": ("恐高", "高反", "心脏"),
        # 婴儿车/推车/少台阶都算无障碍诉求——推车友好本质就是坡道/电梯问题，
        # 不能只记成"带幼儿"然后哪都不落地（杭州实测：概要标了「需无障碍」，
        # 行程里却只有一句"以现场为准"，没有确认也没有取舍提示）
        "需无障碍": ("轮椅", "腿脚不便", "无障碍", "少台阶", "台阶少",
                     "婴儿车", "推车"),
        "带幼儿": ("孩子", "宝宝", "娃", "小孩", "儿童", "儿子", "女儿",
                   "婴儿车", "推车"),
    }.items():
        if any(k in text for k in keywords) and pref not in slots["hard_prefs"]:
            slots["hard_prefs"].append(pref)

    # 「每天午休 90 分钟」是显式作息要求，不一定伴随孩子（自己想午休也合法）；
    # 作曲层见到「需午休」同样插 90 分钟午休节点
    if re.search(r"午休|午睡|小睡|睡个午觉", text) and "需午休" not in slots["hard_prefs"]:
        slots["hard_prefs"].append("需午休")

    # 「2大1小」这种句式一个关键词都不含，但带娃的事实已从人数构成里确认——
    # 不能让孩子只影响预算、不影响行程强度与作息
    if (slots.get("children") or 0) > 0 and "带幼儿" not in slots["hard_prefs"]:
        slots["hard_prefs"].append("带幼儿")

    if re.search(r"不想太赶|轻松|躺平|休闲", text):
        slots["pace"] = "relaxed"
    elif re.search(r"特种兵|尽量多|打卡", text):
        slots["pace"] = "intensive"

    # ---- 住宿偏好：「住宿靠近地铁安静点」「住地铁口附近」----
    # 之前只认"住X晚"这种天数口径，偏好描述整句丢掉，选房建议无从谈起。
    _stay = None
    if m := re.search(r"(?:住宿|住的地方|酒店|民宿)[^，。；\n]{0,24}", text):
        _stay = m.group(0)
    elif m := re.search(r"住[^，。；\n]{0,4}(?:靠近|离|挨着|附近)[^，。；\n]{0,18}", text):
        _stay = m.group(0)
    if _stay:
        slots["stay_pref"] = _stay.strip()

    # ---- 点名必去 / 明确排除：行程修改的入口 ----
    # 「想去上海迪士尼玩，我不想去豫园」之后行程原封不动，是因为这两个槽位
    # 之前只有 LLM 抽取路径才有，规则路径完全没有。捕获到景点名后要过滤：
    # 与目的地同名（想去大理玩 → 大理是城市不是景点）或本身是已知城市的
    # 片段都不是"景点"，混进去会让全城 POI 吃到必去加成/被全城排除。
    known_set = set(known)

    def _poi_frag(frag: str) -> str:
        # 「了」也要剥：「不想去豫园了」抽出来的片段会带个"了"字，
        # 显示成"已避开：豫园了"，看着像系统没听懂人话。
        return re.sub(r"[玩的了吧呀啊呢哦嘛]+$", "", frag).strip()

    def _trim_stop(frag: str) -> str:
        """剥掉粘在景点名首尾的虚词：「那个豫园还是」→「豫园」。"""
        changed = True
        while changed:
            changed = False
            for w in _STOP_FRAG:
                if frag.startswith(w) and len(frag) - len(w) >= 2:
                    frag, changed = frag[len(w):], True
                elif frag.endswith(w) and len(frag) - len(w) >= 2:
                    frag, changed = frag[:-len(w)], True
        return frag

    def _valid_poi(frag: str) -> bool:
        # 「想去大理玩」的大理是目的地，不是景点；但「一定要去喜洲古镇」
        # 的喜洲古镇只是恰好被当成了目的地（不在已知城市表），必去标记要保留——
        # 用户接下来补了城市，这个点名还生效。
        # 具体规则收在 valid_poi_name：标点/时刻班次碎片/城市省份/虚词
        # 一律不是景点（「成都、重庆」「达时间改为」都在这被拦下）。
        if frag == slots.get("destination") and frag in known_set:
            return False
        return valid_poi_name(frag) and frag not in known_set

    # 停止字符集让「想去大理古城逛逛」截成「大理古城」，而不是吞进"逛逛"。
    # (?<![不别用]) 挡住"不想去/别去/不用去"——否则必去与排除会同时命中同一景点。
    # 「加上/增加」覆盖行程改不了的另一种说法："帮我加上迪士尼"。
    # 「顺便去/再去/也去」覆盖顺带点名："去上海顺便去迪士尼"——这类点名
    # 之前静默丢失。再去/也去要再挡"不想再去/别再去"（想/别紧贴动词前）；
    # 顺便类不用挡，"不顺便去"不是人话。
    _MUST_VB = r"([\u4e00-\u9fa5]{2,10}?)(?=[玩旅逛吧呀啊呢哦嘛的之，。、,；;！!\s]|$)"
    # 顿号并列：「想去大熊猫基地、人民公园、都江堰」是三个点名，不是一个。
    # 旧正则一路吃到标点才停，「大熊猫基地、人民公园、都江堰」被当成一个
    # 景点名，含「、」直接判非法——三个点名全丢，方案里一个都没排（实测）。
    for m in re.finditer(
            r"(?<![不别用])(?<![想])"
            r"(?:一定要去|必去|必须去|想去|要去|想逛|再去|也去)"
            r"([\u4e00-\u9fa5]{2,10}?(?:、[\u4e00-\u9fa5]{2,10}?)+)"
            r"(?=[玩旅逛吧呀啊呢哦嘛的之，。,；;！!\s]|$)", text):
        for part in re.split(r"[、]", m.group(1)):
            frag = _poi_frag(part)
            if _valid_poi(frag) and frag not in slots["must_visit"]:
                slots["must_visit"].append(frag)
    for m in re.finditer(
            r"(?<![不别用])(?<![想])"
            r"(?:一定要去|必去|必须去|想去|要去|想逛|再去|也去)" + _MUST_VB, text):
        frag = _poi_frag(m.group(1))
        if _valid_poi(frag) and frag not in slots["must_visit"]:
            slots["must_visit"].append(frag)
    for m in re.finditer(
            r"(?<![不别用])(?:顺便去|顺便逛|顺路去|加上|增加|添上)" + _MUST_VB, text):
        frag = _poi_frag(m.group(1))
        if _valid_poi(frag) and frag not in slots["must_visit"]:
            slots["must_visit"].append(frag)
    # 「给都江堰安排一天 / 安排一天去熊猫基地」：那个点收进必去（天数保持不动）
    if _spot_day and _valid_poi(_spot_day) and _spot_day not in slots["must_visit"]:
        slots["must_visit"].append(_spot_day)
    # 排除清单同样支持顿号并列：「不想去豫园、外滩」是两处都要避开。
    # 只认单个片段时整串含「、」被判非法，两处全部漏掉——不想去的点照样排。
    _AVOID_VB = (r"(?:不想去|不想逛|不去|别去|别逛|别安排|不要安排|不想安排|不安排|"
                 r"排除|去掉|删掉|避开)")
    for m in re.finditer(
            _AVOID_VB + r"([\u4e00-\u9fa5]{2,10}?(?:、[\u4e00-\u9fa5]{2,10}?)+)"
            r"(?=[玩旅逛吧呀啊呢哦嘛的之，。,；;！!\s]|$)", text):
        for part in re.split(r"[、]", m.group(1)):
            frag = _poi_frag(part)
            if _valid_poi(frag) and frag not in slots["avoid"]:
                slots["avoid"].append(frag)
    for m in re.finditer(
            _AVOID_VB + r"([\u4e00-\u9fa5]{2,10}?)(?=[玩旅逛吧呀啊呢哦嘛的之，。、,；;！!\s]|$)",
            text):
        frag = _poi_frag(m.group(1))
        if _valid_poi(frag) and frag not in slots["avoid"]:
            slots["avoid"].append(frag)

    # 倒装说法：「把豫园加回来」「豫园还是安排上吧」「又重新想去迪士尼了」。
    # 正向正则只能覆盖「加上X」这种语序，用户最常见的改口恰好是 X 在前，
    # 于是整句被当成闲聊——排除过的景点再也加不回来。
    for m in re.finditer(
            r"(?:把|将)?([\u4e00-\u9fa5]{2,10}?)(?:也|再|又|还是|重新)*"
            r"(?:加回来|加回去|加回|加进来|安排上|安排回来|恢复|保留|留着)", text):
        frag = _trim_stop(_poi_frag(m.group(1)))
        if _valid_poi(frag) and frag not in slots["must_visit"]:
            slots["must_visit"].append(frag)
    # 正向语序：「加回来上海迪士尼」「保留喜洲古镇」——动词在前，景点在后
    for m in re.finditer(
            r"(?:加回来|加回去|加回|加进来|安排上|安排回来|恢复|保留|留着)"
            r"([\u4e00-\u9fa5]{2,10}?)(?=[玩旅逛吧呀啊呢哦嘛的之，。,；;！!\s]|$)", text):
        frag = _trim_stop(_poi_frag(m.group(1)))
        if _valid_poi(frag) and frag not in slots["must_visit"]:
            slots["must_visit"].append(frag)

    # 两个清单都出现同一景点时，排除赢（veto 先于必去占位，保持一致）
    slots["must_visit"] = [m for m in slots["must_visit"] if m not in slots["avoid"]]

    # 多城请求剥出来：「去成都、重庆玩5天」的城市串绝不能当目的地或景点
    normalize_multi_dest(slots, text)

    slots["uncertain_fields"] = uncertain
    return slots


def find_missing(slots: dict) -> list[str]:
    """缺口判定：只把"不问就没法排"的项列为必问。"""
    missing = []
    if not slots["destination"]:
        missing.append("destination")
    if not slots["date_range"]:
        missing.append("date_range")
    if not slots["days"]:
        missing.append("days")
    return missing


def resolve_date_range(slots: dict, today: date | None = None
                       ) -> tuple[dict, str | None]:
    """把不精确的日期折算成可排程的具体日期，并返回一句假设说明。

    为什么需要：用户说"下个月去大理玩 4 天"时，date_range 只有 month 没有 start，
    find_missing 判它"不缺"，编排器却要 date_range["start"] —— 直接 KeyError。
    之前评测没抓到，是因为那条用例同时缺 days，在追问处就返回了。

    折算规则取「该月第二个周六」：周末出行最普遍，且避开月初出账日。
    """
    today = today or date.today()
    dr = slots.get("date_range") or {}
    if dr.get("start"):
        return slots, None

    # 只说了"20号"（没提月份）：指最近将来的那个 20 号——本月没过就是本月，
    # 已过就顺延到下个月。不能折成"第二个周六"，那会把用户明说的日子改掉。
    if dr.get("day") and not dr.get("month"):
        d = int(dr["day"])
        start = _safe_date(today.year, today.month, d)
        if start >= today:
            out = dict(slots)
            out["date_range"] = {"start": start.isoformat(), "exact": True}
            return out, None
        y2, m2 = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        start = _safe_date(y2, m2, d)
        out = dict(slots)
        out["date_range"] = {"start": start.isoformat(), "exact": True, "assumed": True}
        return out, f"本月 {d} 号已过，按 {start.isoformat()} 起算"

    if dr.get("month"):
        y, m = (int(x) for x in str(dr["month"]).split("-"))
        if dr.get("day"):
            # "下个月一号"：月日俱全，直接落成具体日期，无需假设
            start = _safe_date(y, m, int(dr["day"]))
            if start >= today:
                out = dict(slots)
                out["date_range"] = {"start": start.isoformat(), "exact": True}
                return out, None
            if dr.get("this_month"):
                # "这个月20号"（今天已过 20 号）→ 下个月的 20 号，不是明年
                y2, m2 = ((y + 1, 1) if m == 12 else (y, m + 1))
                start2 = _safe_date(y2, m2, int(dr["day"]))
                out = dict(slots)
                out["date_range"] = {"start": start2.isoformat(), "exact": True,
                                     "assumed": True}
                return out, f"本月 {dr['day']} 号已过，按 {start2.isoformat()} 起算"
            start2 = _safe_date(y + 1, m, int(dr["day"]))
            out = dict(slots)
            out["date_range"] = {"start": start2.isoformat(), "exact": True,
                                 "assumed": True}
            return out, f"{dr['month']} {dr['day']} 日已过，按明年 {start2.isoformat()} 起算"
        days = [date(y, m, i) for i in range(1, calendar.monthrange(y, m)[1] + 1)]
        saturdays = [d for d in days if d.weekday() == 5]
        start = saturdays[1] if len(saturdays) > 1 else saturdays[0]
        note = f"未定具体出发日，按 {start.isoformat()}（{dr['month']} 第二个周六）起算"
    else:
        start = today + timedelta(days=30)
        note = f"未定出行时间，按 30 天后 {start.isoformat()} 起算"

    out = dict(slots)
    out["date_range"] = {"start": start.isoformat(), "exact": False, "assumed": True}
    return out, note


# ------------------------------------------------------------------ 2. 编排


def uncertain_ticket_notes(days_plan: list[dict]) -> list[str]:
    """位置估算的点的门票口径，两种都要写明：

    - 查到价（cost>0）：按多少/人估、未核实、已计入；
    - 没查到价（cost=0）：**没有计入预算**——不说这句，用户看到门票 ¥110
      分不清是不是含都江堰（实际是别的景点的票）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for d in days_plan:
        for nd in d["nodes"]:
            if not nd.get("geo_uncertain") or nd["name"] in seen:
                continue
            seen.add(nd["name"])
            c = nd.get("cost") or 0
            if c > 0:
                out.append(
                    f"{nd['name']} 的门票按 {c} 元/人估算（位置与价格都未核实），"
                    f"已计入门票预算；实际票价与开放情况以景区官方为准")
            else:
                out.append(
                    f"{nd['name']} 的门票没有计入预算（资料库没查到票价，位置也未核实）；"
                    f"预留多少请按景区官方价估，或告诉我票价，我补进预算")
    return out


def kid_fare_heads(children: int | None, child_age: int | None,
                   adults: int, total: int) -> float:
    """门票按人头口径：6 岁以下多数景区免票，6 岁及以上按半价，没说年龄也按半价。

    返回"折算大人头数"（半价=0.5）。孩子不计或按半价，都写进假设里说清楚。
    """
    kids = children or 0
    if not kids:
        return float(total)
    if child_age is not None and child_age < 6:
        return float(adults)
    return adults + 0.5 * kids


def hotel_rooms(children: int | None, total: int) -> tuple[int, str]:
    """带孩子的房型：2大1小订 1 间家庭房/加床比两间标间便宜也方便。"""
    if (children or 0) and total <= 4:
        return 1, "家庭房/加床"
    return (1 if total <= 2 else (total + 1) // 2), "间"


def plan_trip(slots: dict, trace: list, client: "llm_layer.Client | None" = None,
              today: date | None = None,
              on_phase: "Callable[[str], None] | None" = None) -> dict:
    """on_phase：阶段回调（给等待页显示"现在在做什么"），不参与任何决策。"""
    def _phase(text: str) -> None:
        if on_phase:
            try:
                on_phase(text)
            except Exception:
                pass

    _phase("正在查天气、酒店与景点数据…")
    # today 必须可传入：Session 有自己的模拟日期，但这里原来用 date.today()，
    # 日期一跨月测试里「下个月一号」就翻成明年，同一份代码昨天绿今天红。
    slots, date_note = resolve_date_range(slots, today=today)
    assumptions = [date_note] if date_note else []
    dest = slots["destination"]
    start = date.fromisoformat(slots["date_range"]["start"])
    days = slots["days"]
    end = start + timedelta(days=days - 1)
    n = slots["party_size"]
    # 人数构成：party_size 是含孩子的总人数；门票/房型要按大人小孩分开算
    _kids = slots.get("children") or 0
    _child_age = slots.get("child_age")
    _adults = slots.get("adults") if slots.get("adults") is not None else n - _kids
    _adults = max(_adults, 1)
    # 儿童门票口径：6 岁以下多数景区免票，6 岁及以上按半价估；没说年龄也按半价
    # （折算在 kid_fare_heads 里做，这里只保留口径说明用的变量）
    # 出发地：用户说了才算数。没说就不安排往返大交通——以前悄悄按默认城市
    # （杭州）查班次、把交通费算进预算，用户从成都走也会收到一份「杭州-大理」。
    origin = slots.get("origin")
    has_origin = bool(origin)

    def call(name: str, args: dict, batch: int) -> dict:
        res = dispatch(name, args)
        trace.append({"batch": batch, "tool": name, "args": args,
                      "ok": res.get("ok"), "summary": summarize(name, res)})
        return res

    # 第一批：无依赖，并行（没有出发地就不查班次，也不假装查过）
    weather = call("get_weather", {"city": dest, "start_date": start.isoformat(),
                                   "end_date": end.isoformat()}, 1)
    outbound = None
    if has_origin:
        outbound = call("search_transport", {"from_city": origin,
                                             "to_city": dest, "date": start.isoformat(),
                                             "party_size": n}, 1)
    price_cap = round((slots["budget_total"] or 6000) * 0.08)
    # 当日往返不过夜：不查住宿、不收住宿费、也不给选房建议。以前不管天数都查，
    # 单日行程预算里照收一晚 400（杭州实测：1 天行程写「1 天 1 晚」+ ¥400 住宿）。
    _nights = max(days - 1, 0)
    hotel_relaxed = False
    if _nights:
        hotel = call("search_hotel", {"city": dest, "check_in": start.isoformat(),
                                      "check_out": end.isoformat(), "guests": n,
                                      "max_price_per_night": price_cap}, 1)
        if hotel.get("ok") and not hotel.get("items"):
            # 条件过严无匹配 → 换参数重试（放宽价格上限），而不是拿空列表往下走
            hotel = call("search_hotel", {"city": dest, "check_in": start.isoformat(),
                                          "check_out": end.isoformat(), "guests": n}, 1)
            hotel_relaxed = True
    else:
        hotel = {"ok": True, "items": [], "day_trip": True}
    pois = call("search_poi", {"city": dest, "limit": 20}, 1)

    # 点名景点在候选池里找不到 → 定向补一条再重查。
    # 否则「我想去上海迪士尼」遇上库里只有外滩/豫园的上海，整句点名等于没说：
    # 候选里没有这个 POI，打分/占位都谈不上，用户只会看到行程纹丝不动。
    must_report: dict = {"placed": [], "unresolved": [], "fetched": [],
                         "custom": []}
    must_names = [m for m in (slots.get("must_visit") or []) if m]
    if must_names:
        pool = pois.get("items") or []
        missing = [m for m in must_names
                   if not any(name_hit(m, p.get("name") or "") for p in pool)]
        got_new = False
        for m in missing:
            spot = autofetch.ensure_spot(dest, m, client=client)
            st = spot.get("status")
            ok_spot = st in ("exists", "fetched", "custom")
            trace.append({"batch": 2, "tool": "autofetch.ensure_spot",
                          "args": {"city": dest, "name": m},
                          "ok": ok_spot, "summary": spot.get("message") or ""})
            if ok_spot:
                got_new = got_new or st in ("fetched", "custom")
                bucket = "custom" if st == "custom" else "fetched"
                must_report[bucket].append(
                    (spot.get("poi") or {}).get("name") or m)
            else:
                must_report["unresolved"].append(m)
        if got_new:
            # 重查一次：补进来的 POI 要回到候选池才能参与打分与占位
            pois = call("search_poi", {"city": dest, "limit": 40}, 2)

    # 第二批：依赖第一批（回程日期由行程天数决定）
    inbound = None
    if has_origin:
        inbound = call("search_transport", {"from_city": dest,
                                            "to_city": origin,
                                            "date": end.isoformat(), "party_size": n}, 2)

    # 没有出发地：按保守时刻编排，往返大交通不查、不算、如实标注。
    # 当日往返不能按 18:00 抵达——那样一天里一个景点都排不进（杭州实测：
    # 单日行程填了西湖、三个必去点全没安排，还提示"天数/节奏装不下"）。
    _arr_default = "09:00" if days == 1 else "18:00"
    arrival = ((outbound or {}).get("items") or [{"arrive": _arr_default}])[0]["arrive"]
    depart_last = ((inbound or {}).get("items") or [{"depart": "18:00"}])[0]["depart"]
    if not has_origin:
        assumptions.append(
            "未提供出发地，方案不含往返大交通（班次与费用）；"
            "定了出发地告诉我，我补上班次并更新预算")
    else:
        # 出发地查不到班次（如小城市）：如实标注，而不是静默按 0 元交通算预算
        # 方向必须按腿算：返程是从目的地回出发地。两腿都写 origin→dest 时，
        # 用户会照着方案里那行去查「上海→南京」的回程票（实测：上海去南京，
        # 去程返程都显示「上海-南京」）。
        for leg_name, leg_res in (("去程", outbound), ("返程", inbound)):
            if not (leg_res or {}).get("ok"):
                # 单日行程的真实口径是"上午到、傍晚走"，照抄 18:00 抵/离会让
                # 用户以为当天只有晚上可用，也解释不了为什么排不进景点
                if days == 1:
                    _tail = ("按 09:00 抵达编排" if leg_name == "去程"
                             else "按 18:00 返程编排")
                else:
                    _tail = "按 18:00 抵/离保守编排"
                assumptions.append(
                    f"{leg_name} {leg_route(leg_name, origin, dest)} 暂无班次数据，"
                    f"{_tail}，大交通请自行确认")
    hotel_geo = (hotel.get("items") or [{}])[0].get("geo")
    if not hotel_geo:
        # 没有住宿数据时，用目的地中心当"家"。否则编排器退回内置默认坐标（大理），
        # 外地所有景点都会吃到 2000km 的距离惩罚 + 15km 半径否决，排出来的行程
        # 一个景点都没有——自动补全的城市（无酒店报价）正好会踩这一条。
        try:
            from .real_tools import resolve_geo
            hotel_geo = resolve_geo(dest)
        except Exception:
            hotel_geo = None
    reranker = None
    if client is not None:
        def reranker(cands, s_):  # noqa: ANN001 - 钩子签名固定
            order, rec = llm_layer.rerank_llm(client, cands, s_)
            trace.append({"batch": 0, "tool": "llm.rerank",
                          "args": {"候选": len(cands)},
                          "ok": rec.ok,
                          "summary": (f"重排 {' → '.join(order or [])}" if rec.ok
                                      else f"降级规则排序：{rec.degraded}")})
            return order

    _phase("正在编排每日动线与用餐…")
    days_plan, decision_log = compose(slots, pois, weather, arrival,
                                      outbound_depart_last=depart_last,
                                      hotel_geo=hotel_geo,
                                      reranker=reranker)

    # 位置估算的点连门票也是估的：¥110 明细里看不出是不是都江堰的票，
    # 必须写明"按多少/人估、未核实"，不然用户会当成查证过的景区票价。
    assumptions.extend(uncertain_ticket_notes(days_plan))

    # 天气主动提醒：查到的天气预报不能只当装饰——雨大的日子、极端温度
    # 要主动说一句，用户才知道该带伞、该挪室内。
    # 放在 weather["advice"] 而不是 assumptions：assumptions 是"我猜了什么"，
    # 天气提醒是查到的实际情况，混进去会让"信息齐备时不该有假设"失效。
    weather = dict(weather or {})
    _rainy = [(w.get("date"), w.get("rain_prob")) for w in (weather.get("days") or [])
              if (w.get("rain_prob") or 0) >= 60]
    _extreme = [(w.get("date"), w.get("high"), w.get("low"))
                for w in (weather.get("days") or [])
                if (w.get("high") or 0) >= 35 or (w.get("low") or 99) <= 0]
    _advice = []
    if _rainy:
        days_txt = "、".join(f"{d[5:]}（{p}%）" for d, p in _rainy[:3])
        _advice.append(f"{days_txt} 降水概率偏高，记得带伞；"
                       f"雨中不便的户外景点说一句「那天换室内的」，我重排")
    if _extreme:
        d, hi, lo = _extreme[0]
        _advice.append(f"{d[5:]} 温度 {lo}~{hi}℃，注意"
                       f"{'防暑' if (hi or 0) >= 35 else '保暖'}")
    if _advice:
        weather["advice"] = "；".join(_advice)

    # 预算
    hotel_price = (hotel.get("items") or [{}])[0].get("price", 400) if hotel.get("items") else 400
    nights = _nights
    rooms, room_label = hotel_rooms(_kids, n)
    if nights and not hotel.get("items"):
        assumptions.append(
            f"{dest} 暂无住宿报价，住宿按 {hotel_price} 元/晚的常见均价估算，"
            f"订房前以实际房价为准")
    # 住宿偏好要落到可执行的选房建议：按已排景点的几何中心指一个片区，
    # 而不是把「近地铁安静」原样复述一遍。依据是行程数据，不是编的地名。
    _stay_pref = " ".join(filter(None, [slots.get("stay_pref") or "", slots.get("lodge") or ""]))
    if _stay_pref and not nights:
        assumptions.append(
            f"你提了住宿偏好（{_stay_pref}），但这版是当日往返、不住宿，"
            f"所以没排订房；要过夜就说「住一晚」，我按这个偏好挑片区")
    elif _stay_pref:
        from .tools import haversine_km as _hk
        _spots = [(n["name"], n["geo"]) for d in days_plan for n in d["nodes"]
                  if n["type"] == "attraction" and n.get("geo")
                  and not n.get("geo_uncertain")]
        if _spots:
            _c = (sum(g[0] for _, g in _spots) / len(_spots),
                  sum(g[1] for _, g in _spots) / len(_spots))
            _near2 = sorted(_spots, key=lambda x: _hk(_c, x[1]))[:2]
            _area_txt = "、".join(nm for nm, _ in _near2)
            assumptions.append(
                f"住宿建议（偏好：{_stay_pref}）：大部分景点集中在 {_area_txt} 一带，"
                f"订房选这一片步行/一站地铁可达的位置最省路上时间；"
                f"要安静就避开主干道临街和酒吧区，优先小区型公寓/民宿")
        else:
            assumptions.append(
                f"住宿建议（偏好：{_stay_pref}）：景点位置数据不全，"
                f"建议等行程确定后再挑景点之间的位置订房")
    if hotel_relaxed and hotel.get("items"):
        assumptions.append(
            f"预算内（≤{price_cap} 元/晚）没有可用住宿，已放宽到 "
            f"{hotel['items'][0]['price']} 元/晚的{hotel['items'][0]['name']}")
    if nights and rooms == 1 and room_label == "家庭房/加床":
        assumptions.append(
            "带幼儿出行：按 1 间家庭房/加床估算（报价用城市双人间均价，"
            "家庭房差价未计入），订房时选「家庭房」或可加床房型")
    _ticket_base = sum(node["cost"] for d in days_plan for node in d["nodes"]
                       if node["type"] == "attraction")
    ticket_total = round(_ticket_base * kid_fare_heads(_kids, _child_age,
                                                       _adults, n))
    if _kids:
        if _child_age is not None and _child_age < 6:
            assumptions.append(
                f"{_child_age} 岁儿童按多数景区「6 岁以下/1.2m 以下免票」口径，"
                "门票只按大人算；个别景点现场要买儿童票的话告诉我，我重算预算")
        else:
            assumptions.append(
                "儿童门票按半价估算（多数景区 6 岁以下/1.2m 以下免票，"
                "以各景区现场口径为准）")
        # 高铁/机票儿童票折算说明挪到大交通判定处（见下方）——票价未查到
        # 时大交通根本不在预算里，再写"未单独折算"会让人以为已经算过
    items = []
    if has_origin:
        # 班次没查到（价格 0）就不要往预算里塞一行「¥0 交通（数据缺失）」：
        # 0 元大交通会让人以为路费不要钱，假设里已经写明"请自行确认"。
        for _leg, _res in (("往", outbound), ("返", inbound)):
            _amt = pick(_res, 0) * n
            if _amt:
                items.append({"category": "transport",
                              "label": f"{_leg} {outbound_label(_res)}",
                              "amount": _amt})
    if nights:
        # 当日往返不写住宿行：0 元的「住宿 0晚」既没用又容易被当成漏算
        items.append(
            {"category": "hotel",
             "label": (f"{(hotel.get('items') or [{}])[0].get('name', '住宿（按城市均价估算）')} "
                       f"{nights}晚×{rooms}{room_label}"),
             "amount": hotel_price * nights * rooms})
    items += [
        {"category": "ticket", "label": "景点门票与索道", "amount": ticket_total},
        {"category": "food", "label": f"餐饮 {days}天×{n}人（人均 180/天估算）",
         "amount": 180 * days * n},
        {"category": "local_transit", "label": f"市内交通 {days}天×{n}人（人均 60/天估算）",
         "amount": 60 * days * n},
    ]
    # 大交通口径必须与预算标题一致（亲子实测反馈：标题写「不含大交通」，
    # 假设却写「已按全价计入」，用户会误以为已经算进总额）。
    # 判定就看明细里有没有真的交通费——票价没查到时明细里不会有 0 元行。
    # 未计入必写；已计入且带娃才补儿童票折算说明（信息齐备时不多话）
    if not any(i["category"] == "transport" for i in items):
        assumptions.append("往返大交通未计入预算（班次票价未查到或未提供出发地），"
                           "出发前请自行查票价确认")
    elif _kids:
        assumptions.append(
            "往返大交通按查到的班次票价×全部人数计入预算，儿童票未单独折算"
            "（高铁 6 岁下免费、机票 2~11 岁约半价），实际通常比这里低")
    _phase("正在核算预算与校验行程可行性…")
    budget = dispatch("calc_budget", {"items": items, "party_size": n,
                                      "budget_target": slots["budget_total"],
                                      "contingency_rate": 0.1})
    trace.append({"batch": 3, "tool": "calc_budget",
                  "args": {"items": f"{len(items)} 项", "party_size": n},
                  "ok": budget.get("ok"), "summary": f"合计 {budget.get('total')} 元"})

    cut_plans = build_cut_plans(budget, hotel, nights, rooms, days_plan, n)

    # 必去点的落地结果：让对话层能分清"排进去了""库里根本没有""排不下"。
    # 三种情况对用户来说意味完全不同，用一句"未能排入"糊过去最招投诉。
    node_names = [n["name"] for d in days_plan for n in d["nodes"]]
    must_report["placed"] = [m for m in must_names
                             if any(name_hit(m, nm) for nm in node_names)]
    must_report["missed"] = [m for m in must_names
                             if m not in must_report["placed"]
                             and m not in must_report["unresolved"]]

    return {"slots": slots, "origin": origin, "has_origin": has_origin,
            "weather": weather, "pois": pois, "hotel": hotel,
            "outbound": outbound, "inbound": inbound,
            "days_plan": days_plan, "budget": budget, "cut_plans": cut_plans,
            "decision_log": decision_log, "assumptions": assumptions,
            "must_report": must_report}


def leg_route(leg_name: str, origin: str, dest: str) -> str:
    """这一腿的方向：去程 origin→dest，返程 dest→origin。

    只写 origin→dest 的后果是用户按错误方向查票——「上海去南京」的方案里
    返程也显示「上海→南京」（2026-11-11 实测，班次查不到时尤其明显）。
    """
    if (leg_name or "").startswith("返") or leg_name in ("回程", "返"):
        return f"{dest}→{origin}"
    return f"{origin}→{dest}"


def outbound_label(res: dict | None) -> str:
    if res is None:
        return "未安排"
    items = res.get("items") or []
    if not items:
        return "未查到班次/票价"
    it = items[0]
    if not (it.get("total_price") or it.get("price")):
        # 班次查到了但没票价：文本侧也不许只说"交通"，让人误以为免费
        return f"{it.get('code', '班次')}（票价未查到）"
    return f"{items[0]['code']} {items[0]['depart']}-{items[0]['arrive']}"


def pick(res: dict | None, idx: int) -> float:
    items = (res or {}).get("items") or []
    return float(items[idx]["price"]) if len(items) > idx else 0.0


def summarize(name: str, res: dict) -> str:
    if not res.get("ok"):
        return f"失败：{res['error']['code']}"
    if name == "get_weather":
        return f"{len(res['days'])} 天，首日 {_wx_line(res['days'][0])}"
    if name == "search_hotel":
        return f"{len(res.get('items', []))} 家候选"
    if name == "search_poi":
        return f"{res.get('total', 0)} 个 POI"
    if name == "search_transport":
        return f"{len(res.get('items', []))} 个班次"
    return "ok"


def build_cut_plans(budget: dict, hotel: dict, nights: int, rooms: int,
                    days_plan: list[dict], n: int) -> list[dict]:
    """对应子提示词 C：超预算时给 3 个可执行降本方案，按体验损失升序。"""
    if budget.get("within_budget") is not False:
        return []
    plans = []
    cur_price = (hotel.get("items") or [{}])[0].get("price", 0)
    all_prices = [h["price"] for h in (hotel.get("items") or [])]
    if nights and cur_price and len(all_prices) > 1:
        cheap = min(all_prices)
        plans.append({
            "change": f"住宿换到同城 {cheap} 元/晚 的客栈（多走约 10 分钟到古城）",
            "save": round((cur_price - cheap) * nights * rooms),
            "cost_of_change": "房间小一点，位置稍微偏，行程本身不受影响",
        })
    expensive = sorted(
        [n_ for d in days_plan for n_ in d["nodes"]
         if n_["type"] == "attraction" and n_["cost"] > 0],
        key=lambda x: -x["cost"])
    if expensive:
        e = expensive[0]
        plans.append({
            "change": f"取消「{e['name']}」（{e['cost']} 元/人），换同区域免费徒步路线",
            "save": e["cost"] * n,
            "cost_of_change": "少一个付费景点，腾出半天更轻松",
        })
    plans.append({
        "change": "餐饮从餐厅正餐改为一半本地小吃",
        "save": round(60 * len(days_plan) * n * 0.5),
        "cost_of_change": "少两顿坐下来吃的饭，体验感略降",
    })
    plans.append({
        "change": "市内出行以公交/步行为主，仅夜间打车",
        "save": round(35 * len(days_plan) * n),
        "cost_of_change": "单程多花 15~20 分钟，适合不赶时间的行程",
    })
    return plans[:3]


# ------------------------------------------------------------------ 3. 自检


def self_check(result: dict) -> list[dict]:
    """对应 system prompt 第六节自检清单的可判定部分，全部基于数据而非观感。"""
    from . import composer
    from .tools import haversine_km

    checks = []
    days_plan = result["days_plan"]
    budget = result["budget"]

    # 用餐点：抵达/返程日按班次时间判断，其余每天都必须有坐下来吃的时段。
    # 原来无条件豁免最后一天——16 点才发车还整天没饭，质检照样亮绿灯。
    def _dep_min() -> int | None:
        it = ((result.get("inbound") or {}).get("items") or [{}])[0]
        try:
            _hh, _mm = (int(x) for x in str(it.get("depart") or "18:00").split(":")[:2])
            return _hh * 60 + _mm
        except (ValueError, AttributeError):
            return 18 * 60

    dep_min = _dep_min()
    station_buf = composer.STATION_BUFFER_MIN

    def _node_ends(d: dict) -> list[int]:
        """一天里正经节点（景点/餐）的结束时刻，分钟制。"""
        ends = []
        for n in d["nodes"]:
            if n["type"] not in ("attraction", "restaurant"):
                continue
            hh, mm = (int(x) for x in n["time"].split(":"))
            ends.append(hh * 60 + mm + n["duration_min"])
        return ends

    no_meal = []
    for d in days_plan:
        if any(n["type"] == "restaurant" for n in d["nodes"]):
            continue
        if d["day"] == len(days_plan):
            if dep_min < 13 * 60:
                continue          # 中午前就发车，不安排正餐说得通
            # 返程日真来不及坐下吃（吃完 + 去车站会越过发车）不算违规——
            # 编排层此时会把午餐如实改成「打包 + 提前动身」
            ends = _node_ends(d)
            last_end = max(ends) if ends else None
            if last_end is not None and last_end + 60 + station_buf > dep_min:
                continue
        no_meal.append(f"Day{d['day']}")
    checks.append({
        "item": "每天有明确用餐点"
                + ("" if len(days_plan) < 2 or dep_min >= 13 * 60
                   else "（返程日在发车前不排正餐）"),
        "result": "pass" if not no_meal else "fail",
        "evidence": f"缺用餐点：{'、'.join(no_meal)}" if no_meal else "",
    })
    # 亲子午休：带幼儿（或点名要午休）的行程，除抵达日外每天都要有午休节点，
    # 或至少在某个节点里写明"今天在车上/候车时补"。成都 4 天实测：Day2 的午餐
    # 走"先吃饭再逛"那条分支，午休整段消失，只有 Day3 有——承诺变成了摆设。
    _hp = (result.get("slots") or {}).get("hard_prefs") or []
    if "带幼儿" in _hp or "需午休" in _hp:
        _no_nap = []
        for d in days_plan:
            if d["day"] == 1 and len(days_plan) > 1:
                continue                      # 抵达日到店已傍晚，晚餐后直接睡
            has = any(n["name"].startswith("午休") for n in d["nodes"])
            noted = any("午休" in (n.get("tip") or "") for n in d["nodes"])
            if not has and not noted:
                _no_nap.append(f"Day{d['day']}")
        checks.append({
            "item": "带幼儿每天有午休" + ("（抵达日除外）" if len(days_plan) > 1 else ""),
            "result": "pass" if not _no_nap else "fail",
            "evidence": f"缺午休：{'、'.join(_no_nap)}" if _no_nap else "",
        })
    # 口径与节奏承诺一致（亲子实测反馈：午餐/午休/自由活动都算节点，
    # 两处景点的轻松日被误报超额）——只数景点，上限取 PACE_CAP
    _cap = composer.PACE_CAP.get((result.get("slots") or {}).get("pace"), 3)
    over = []
    for d in days_plan:
        _na = sum(1 for n in d["nodes"] if n["type"] == "attraction")
        if _na > _cap:
            over.append(f"Day{d['day']}（{_na} 个景点 > 上限 {_cap}）")
    checks.append({"item": f"单日景点数不超节奏上限（每天最多 {_cap} 个）",
                   "result": "pass" if not over else "fail",
                   "evidence": f"超额天数：{over}" if over else ""})
    within = budget.get("within_budget")
    checks.append({
        "item": "预算落在目标区间",
        "result": "pass" if within is None or within else "fail",
        "evidence": (f"合计 {budget.get('total')} / 目标 {budget.get('target')}"
                     if budget.get("target") else f"合计 {budget.get('total')}，用户未设预算上限"),
    })
    # 一日往返没有"抵达日"这个概念：整天都能用，240 分钟的西湖是正常的，
    # 不该按"首日只安排轻活动"判失败（杭州单日实测：质检因此误报红灯）
    _one_day = (result.get("slots") or {}).get("days") == 1 or len(days_plan) == 1
    checks.append({
        "item": "首日不排高强度景点",
        "result": ("pass" if _one_day or
                   all(n["duration_min"] <= 150 for n in days_plan[0]["nodes"])
                   else "fail"),
        "evidence": ("一日往返按整天排，不受首日强度限制" if _one_day else ""),
    })

    # 折返检测：单日内相邻站点直线距离是否超阈值
    hops = []
    for d in days_plan:
        geos = [n["geo"] for n in d["nodes"]
                if n.get("geo") and n["type"] == "attraction"]
        for a, b in zip(geos, geos[1:]):
            hops.append(round(haversine_km(a, b), 1))
    worst = max(hops) if hops else 0
    checks.append({"item": f"相邻站点 ≤{composer.MAX_HOP_KM}km（不折返）",
                   "result": "pass" if worst <= composer.MAX_HOP_KM else "fail",
                   "evidence": f"最长单跳 {worst}km"})

    # 单日移动时长
    move_over = [f"Day{d['day']} {d['move_min']}min" for d in days_plan
                 if d["move_min"] > composer.DAY_MOVE_BUDGET_MIN]
    checks.append({"item": f"单日移动 ≤{composer.DAY_MOVE_BUDGET_MIN}min",
                   "result": "pass" if not move_over else "fail",
                   "evidence": "、".join(move_over)})

    # 营业时间冲突
    # 营业时间冲突。violations 里 day=0 是「全程未能排入」的内部标记
    # （composer 用 0 占位），用户看到 "Day0 灵隐寺" 会当成不存在的第 0 天
    # （杭州实测）——渲染成正常说法，编号只对真实行程天使用。
    def _vio_label(v: dict) -> str:
        day = v.get("day")
        if isinstance(day, int) and day > 0:
            return f"Day{day} {v['poi']}"
        return f"{v['poi']}（全程未能排入）"

    vio = (result.get("decision_log") or {}).get("violations") or []
    checks.append({"item": "无闭馆时间冲突",
                   "result": "pass" if not vio else "fail",
                   "evidence": "；".join(f"{_vio_label(v)}：{v['why']}" for v in vio)})

    # 景点不拖到深夜：营业时间允许但体验不成立的排法（21:05 逛完古城）
    late = []
    for d in days_plan:
        for n in d["nodes"]:
            if n["type"] != "attraction":
                continue
            hh, mm = (int(x) for x in n["time"].split(":"))
            end = hh * 60 + mm + n["duration_min"]
            if end > composer.DAY_END_LATEST:
                late.append(f"Day{d['day']} {n['name']} 到 {end // 60}:{end % 60:02d}")
    checks.append({"item": f"景点不晚于 {composer.DAY_END_LATEST // 60}:00 结束",
                   "result": "pass" if not late else "fail",
                   "evidence": "、".join(late)})

    # 午餐时段：11:26 开始逛 150 分钟等于 14:00 才吃饭。
    # 按实际排出的餐点时间判，最后一天不再整体豁免——豁免曾经把
    # 「17:03 才开始吃午餐」放成绿灯（杭州实测）。返程日发车早，
    # 午餐窗口放宽到 15:30（吃完还要留去车站的时间）。
    bad_lunch = []
    for d in days_plan:
        if d["day"] == 1:
            continue          # 抵达日不要求
        limit = (15 * 60 + 30) if d["day"] == len(days_plan) else 14 * 60
        lunches = [n for n in d["nodes"]
                   if n["type"] == "restaurant" and n["time"] < "16:00"]
        if not lunches:
            continue
        hh, mm = (int(x) for x in lunches[0]["time"].split(":"))
        if not (11 * 60 + 30 <= hh * 60 + mm <= limit):
            bad_lunch.append(f"Day{d['day']} {lunches[0]['name']} {lunches[0]['time']}")
    checks.append({"item": "午餐落在 11:30–14:00（返程日宽限到 15:30）",
                   "result": "pass" if not bad_lunch else "fail",
                   "evidence": "、".join(bad_lunch)})

    # 返程日赶车可行性：最后一个正经节点结束 + 去车站时间必须早于发车。
    # 之前时间轴能一路排到 18:03 吃完午饭赶 18:00 的车，这一项根本不存在。
    if len(days_plan) > 1 and dep_min:
        ends = _node_ends(days_plan[-1])
        last_end = max(ends) if ends else None
        ok = last_end is None or last_end + station_buf <= dep_min
        checks.append({
            "item": f"返程日 {composer.fmt(dep_min)} 发车前留出去车站时间",
            "result": "pass" if ok else "fail",
            "evidence": ("" if ok else
                         f"最后一个节点 {composer.fmt(last_end)} 结束，"
                         f"距发车只剩 {dep_min - last_end} 分钟"
                         f"（去车站至少要 {station_buf} 分钟）"),
        })
    # 位置待确认的景点必须单独成天。它的坐标是按城市中心估算的，交通时间
    # 压根没法算（都江堰与人民公园、川大博物馆挤在 Day2，晚餐拖到 19:48）。
    # 混排时动线与时长都不可信，质检必须报出来，而不是靠用户自己发现。
    mixed_geo = []
    for d in days_plan:
        atts = [n for n in d["nodes"] if n["type"] == "attraction"]
        unc = [n for n in atts if n.get("geo_uncertain")]
        solid = [n for n in atts if not n.get("geo_uncertain") and n.get("poi_id")]
        if unc and solid:
            mixed_geo.append(
                f"Day{d['day']}：{'、'.join(n['name'] for n in unc)}（位置待确认）与 "
                f"{'、'.join(n['name'] for n in solid)} 排在同一天")
    checks.append({"item": "位置待确认的景点单独占一天",
                   "result": "pass" if not mixed_geo else "fail",
                   "evidence": "；".join(mixed_geo)})

    # 单日总时长：景点净时长与当天收尾时刻双口径。景点数受节奏上限约束，
    # 但"个数合规、时长爆炸"照样成立——240 分钟远郊点 + 两个市区点
    # 就是一天逛到天黑，晚餐排到 19:48。
    overload = []
    for d in days_plan:
        sight = sum(n["duration_min"] for n in d["nodes"] if n["type"] == "attraction")
        ends = _node_ends(d)
        last = max(ends) if ends else None
        if sight > composer.DAY_SIGHT_CAP_MIN:
            overload.append(f"Day{d['day']} 景点净时长 {sight} 分钟 > "
                            f"{composer.DAY_SIGHT_CAP_MIN} 分钟")
        if last is not None and last > 21 * 60 + 30:
            overload.append(f"Day{d['day']} 最后一个安排到 {composer.fmt(last)} 才结束")
    checks.append({"item": f"单日景点净时长 ≤{composer.DAY_SIGHT_CAP_MIN // 60} 小时且不拖到 21:30",
                   "result": "pass" if not overload else "fail",
                   "evidence": "；".join(overload)})

    # 晚餐最晚开始时刻：DAY_END_LATEST 只管景点收尾，不管吃饭。
    late_dinner = []
    for d in days_plan:
        dinners = [n for n in d["nodes"]
                   if n["type"] == "restaurant" and n["time"] >= "16:00"]
        for n in dinners:
            hh, mm = (int(x) for x in n["time"].split(":"))
            if hh * 60 + mm > composer.DINNER_START_LATEST:
                late_dinner.append(f"Day{d['day']} {n['name']} {n['time']} 才开始")
    checks.append({"item": f"晚餐不晚于 {composer.fmt(composer.DINNER_START_LATEST)} 开始",
                   "result": "pass" if not late_dinner else "fail",
                   "evidence": "、".join(late_dinner)})
    return checks


# ------------------------------------------------------------------ 4. 输出


def _no_transport_reason(result: dict) -> str:
    """预算里为什么不含往返大交通。已含交通费时返回空串。

    只看"有没有出发地"是不够的：上海→成都这种班次查不到的情况，明细里没有
    交通项、金额为 0，标题照样写「预算合计」——用户会把 ¥3,553 当成整趟花费，
    到买票时才发现路费根本没算进去。标题必须如实反映"这钱花在哪儿了"。
    """
    b = result.get("budget") or {}
    for item in b.get("breakdown") or []:
        if item.get("category") == "transport" and (item.get("amount") or 0) > 0:
            return ""
    origin = result.get("origin") or (result.get("slots") or {}).get("origin")
    if not origin:
        return "出发地未指定，没查班次与票价"
    dest = (result.get("slots") or {}).get("destination") or ""

    def _missing(res) -> bool:
        """这一腿是不是"没查到可用班次/票价"（空结果与 0 元同罪）。"""
        items = (res or {}).get("items") or []
        return not items or not (items[0].get("total_price")
                                 or items[0].get("price"))

    # 只缺一腿就只报那一腿的方向：两条都说成 origin→dest 会让用户
    # 拿错误方向去查回程票（2026-11-11 实测：上海→南京，返程也这么写）
    legs = []
    if _missing(result.get("outbound")):
        legs.append(f"去程 {leg_route('去程', origin, dest)}")
    if _missing(result.get("inbound")):
        legs.append(f"返程 {leg_route('返程', origin, dest)}")
    if not legs:
        return f"往返 {origin}↔{dest} 的班次票价没查到"
    return "、".join(legs) + " 的班次票价没查到"


def summary_lines(result: dict) -> list[str]:
    """方案开头的「出行概要」：出发地、日期、天数、人数、预算。

    这几项以前散落在正文各处——出发地只出现在班次行（"杭州-大理"），预算只有
    一行合计，用户得自己拼出「我从哪走、几个人、这一版跟我说的目标差多少」。
    数值全部取自 result（工具返回值），不在这里重新推算。
    """
    s, b = result["slots"], result["budget"]
    origin = result.get("origin") or s.get("origin")
    dest = s["destination"]
    n = s.get("party_size") or 1
    # 人数要说构成：「2 人」会把"2大1小"里的孩子藏起来，用户以为没算孩子
    _party = s.get("party_detail") or f"{n} 人"
    if s.get("party_detail") and ("大" in _party or "小" in _party):
        _party += f"，共 {n} 人"
    start = date.fromisoformat(s["date_range"]["start"])
    days = s["days"]
    end = start + timedelta(days=max(days - 1, 0))
    # 单日行程没有"一晚"：写「1 天 1 晚」还收住宿费，等于把一日游当两天卖
    nights = max(days - 1, 0)
    _stay = f"{days} 天 {nights} 晚" if nights else f"{days} 天，当日往返"
    pace = {"relaxed": "轻松", "intensive": "特种兵"}.get(s.get("pace"), "适中")
    prefs = "、".join(s.get("hard_prefs") or [])
    # 没有出发地就明说「未指定」，并把后果（大交通未含）写出来。
    # 以前这里会悄悄填上默认城市，用户从成都走也收到一份「杭州-大理」。
    origin_disp = origin if origin else "未指定（大交通未含）"

    # 预算口径必须写在标题里：不含大交通时报一个光秃秃的"3,553 元"很容易
    # 被当成整趟花费。判定看的是"明细里到底有没有交通费"，而不是"有没有出发地"。
    _miss = _no_transport_reason(result)
    budget_head = ("- **预算合计（不含往返大交通）**" if _miss else "- **预算合计**")
    budget = (f"{budget_head}：约 {round(b['total']):,} 元 / {n} 人"
              f"（人均 {round(b['per_person']):,} 元，含 10% 机动）")
    if _miss:
        budget += f"；⚠ 大交通未计入：{_miss}"
    if b.get("target"):
        gap = b["target"] - b["total"]
        budget += (f"；你的目标 {round(b['target']):,} 元"
                   f"（人均 {round(b['target'] / n):,} 元），"
                   + (f"还剩 {round(gap):,} 元" if gap >= 0 else f"超 {round(-gap):,} 元"))
    else:
        budget += "；你没说预算，下面按三档给参考"

    return [
        f"- **出发地 → 目的地**：{origin_disp} → {dest}",
        f"- **行程日期**：{start.isoformat()} 至 {end.isoformat()}"
        f"（{_stay}）",
        f"- **出行人数**：{_party}　|　**节奏**：{pace}"
        + (f"　|　**硬性偏好**：{prefs}" if prefs else ""),
        budget,
    ]


def _wx_line(w: dict) -> str:
    """一句天气。没有可用的温度就别写成「0~0℃」——那比不写更容易误导。"""
    if not w:
        return "待查"
    desc = w.get("desc") or "待查"
    if w.get("low") is None or w.get("high") is None:
        return desc
    return f"{desc}，{w['low']}~{w['high']}℃"


# 预算分类的中文名（与 render_html.BUDGET_LABELS 同源；正文里不能直接打英文键）
_BUDGET_CN = {
    "transport": "往返大交通", "hotel": "住宿", "ticket": "门票",
    "food": "餐饮", "local_transit": "市内交通", "other": "其他",
}


def _md_range(dates: list[str]) -> str:
    """把日期列表压成「11-20~11-21」或「11-20、11-23」这样的短串。"""
    ds = sorted(d for d in dates if d)
    if not ds:
        return ""
    if len(ds) == 1:
        return ds[0][5:]
    try:
        seq = [date.fromisoformat(d) for d in ds]
    except ValueError:
        return "、".join(d[5:] for d in ds)
    contiguous = all((b - a).days == 1 for a, b in zip(seq, seq[1:]))
    return (f"{ds[0][5:]}~{ds[-1][5:]}" if contiguous
            else "、".join(d[5:] for d in ds))


def _wx_caliber(weather: dict) -> str:
    """天气口径一句话。

    预报窗口是**逐日**判定的：11/20 距今 14 天有真预报，11/23 距今 17 天没有。
    不能因为有几天超窗就把整段都说成历史同期——用户会以为连着的四天全是估算。
    """
    days = weather.get("days") or []
    if not days:
        return ""
    flagged = [d for d in days if d.get("is_forecast") is not None]
    if len(flagged) != len(days):
        # 数据源没给逐日标记（旧缓存/其他实现）：退回整体说明，不编造分界
        return weather.get("note") and f"天气数据：{weather['note']}" or ""
    fc = [d["date"] for d in days if d.get("is_forecast")]
    ar = [d["date"] for d in days if not d.get("is_forecast")]
    if not ar:
        return "天气数据：Open-Meteo 实时预报（逐日）"
    if not fc:
        return ("天气数据：这几天的日期超出 16 天预报窗口，按 2023-2025 年"
                "同期历史均值估算，不是实时预报，出行前请再查一次")
    return (f"天气数据：{_md_range(fc)} 是 Open-Meteo 实时预报；"
            f"{_md_range(ar)} 超出 16 天预报窗口，用 2023-2025 年同期历史均值"
            f"估算（每日卡片已标注），出行前请再查一次")


def render(result: dict) -> str:
    s, b = result["slots"], result["budget"]
    out = [
        f"## {s['destination']} {s['days']} 天行程 · 起 {s['date_range']['start']}",
    ]
    # 提示词要求：有假设时必须在开头用一行标注，不能让用户以为是自己说的
    if result.get("assumptions"):
        out.append(f"> 假设：{'；'.join(result['assumptions'])}")
    if (result.get("weather") or {}).get("advice"):
        out.append(f"> 天气提醒：{result['weather']['advice']}")
    _cal = _wx_caliber(result.get("weather") or {})
    if _cal:
        out.append(f"> {_cal}")
    # 两腿的方向写在班次前面：返程是 destination→origin，不是反过来。
    # 只写班次号时用户得自己猜这班车往哪儿开，很容易照去程方向查回程票。
    _org = result.get("origin") or s.get("origin") or ""
    _dst = s.get("destination") or ""
    _out_route = (leg_route("去程", _org, _dst) + " ") if (_org and _dst) else ""
    _inb_route = (leg_route("返程", _org, _dst) + " ") if (_org and _dst) else ""
    out += [
        "### 出行概要",
        *summary_lines(result),
        "",
        "### 行前准备",
        f"- 天气：{_wx_line((result['weather'].get('days') or [{}])[0])}",
        (f"- 交通：去程 {_out_route}{outbound_label(result['outbound'])}；"
         f"返程 {_inb_route}{outbound_label(result['inbound'])}"
         if result.get("outbound") is not None
         else "- 交通：未指定出发地，往返大交通未安排（预算未含）；定了出发地我补上班次"),
        f"- 需提前预约：{'、'.join(n['name'] for d in result['days_plan'] for n in d['nodes'] if n['booking_required']) or '无'}",
        "",
    ]
    for d in result["days_plan"]:
        out.append(f"### Day {d['day']} · {d['date']} · {d['theme']}（{d['intensity']}）")
        out.append("| 时间 | 安排 | 时长 | 交通 | 备注 |")
        out.append("|---|---|---|---|---|")
        for n in d["nodes"]:
            out.append(f"| {n['time']} | {n['name']} | {n['duration_min']}min | "
                       f"{n['transit']} | {n['tip']} |")
        out.append(f"\n**今日预算**：约 {d['day_budget']} 元　**雨天备案**：{d['rainy_plan']}\n")

    out.append("### 预算明细")
    for item in b["breakdown"]:
        # 以前直接打 category（local_transit 这种英文键名），用户看不懂；
        # 表格窄列还会把金额和说明挤断行。改成「中文项目 —— 金额：说明」的列表。
        cn = _BUDGET_CN.get(item["category"], item["category"])
        out.append(f"- **{cn}**　¥{round(item['amount']):,}　{item['label']}")
    out.append(f"- **机动（10% 预留）**　¥{round(b['contingency']):,}")
    _miss = _no_transport_reason(result)
    if _miss:
        out.append(f"- **合计**　**¥{round(b['total']):,}**　人均 ¥{round(b['per_person']):,}"
                   f"（**不含往返大交通**：{_miss}）")
    else:
        out.append(f"- **合计**　**¥{round(b['total']):,}**　人均 ¥{round(b['per_person']):,}")

    if result.get("cut_plans"):
        over = b["total"] - b["target"]
        out.append(f"\n### 超预算 {over:.0f} 元 · 三个取舍方案（按体验损失从小到大）")
        for idx, p in enumerate(result["cut_plans"], 1):
            out.append(f"{idx}. **{p['change']}** —— 省约 {p['save']} 元")
            out.append(f"   代价：{p['cost_of_change']}")
    return "\n".join(out)


def render_trace(trace: list) -> str:
    lines = ["### 工具调用轨迹"]
    for t in trace:
        flag = "OK " if t["ok"] else "ERR"
        args = json.dumps(t["args"], ensure_ascii=False)
        if len(args) > 90:
            args = args[:90] + "..."
        lines.append(f"- [批次{t['batch']}] {flag} `{t['tool']}` {args} → {t['summary']}")
    return "\n".join(lines)


def run_dry(utterance: str, explain: bool = False,
            client: "llm_layer.Client | None" = None) -> None:
    print(f"\n【用户】{utterance}\n")
    records: list = []
    slots = extract_slots(utterance, client=client, records=records)
    print("【Step1 槽位】", json.dumps(slots, ensure_ascii=False))
    for rec in records:
        state = "成功" if rec.ok else f"降级（{rec.degraded}）"
        print(f"【LLM 抽取】{state}　尝试 {rec.attempts} 次　{rec.latency_ms}ms"
              + (f"　告警 {len(rec.warnings)} 条" if rec.warnings else ""))

    missing = find_missing(slots)
    if missing:
        print(f"\n【Step2 缺口】{missing} → 触发追问，不编排")
        print("（对应输出模式 A：一次只问 1~2 个关键槽位，并给默认建议）")
        return

    trace: list = []
    result = plan_trip(slots, trace, client=client)
    print("\n" + render_trace(trace))
    print("\n【Step3/4 编排完成】\n")
    print(render(result))

    if explain:
        log = result["decision_log"]
        best: dict[str, dict] = {}
        for c in log["candidates"]:
            if c["name"] not in best or c["score"] > best[c["name"]]["score"]:
                best[c["name"]] = c
        print("\n### 决策日志（候选打分，仅展示前 8）")
        for c in sorted(best.values(), key=lambda x: -x["score"])[:8]:
            print(f"- {c['name']}：{c['score']}　（{'；'.join(c['reasons'])}）")
        if log.get("vetoed"):
            print("\n### 硬约束否决（不参与打分）")
            seen = set()
            for v in log["vetoed"]:
                if v["name"] in seen:
                    continue
                seen.add(v["name"])
                print(f"- {v['name']}：{v['why']}")
        if log.get("deferred"):
            print("\n### 因闭馆/动线顺延")
            for v in log["deferred"]:
                print(f"- Day{v['day']} {v['poi']}：{v['why']}")
        print("\n### 每日成团")
        for d in result["decision_log"]["days"]:
            print(f"- Day{d['day']} 移动 {d['move_min']}min：{' → '.join(d['picked']) or '无景点'}")

    print("\n### 自检")
    for c in self_check(result):
        mark = "通过" if c["result"] == "pass" else "不通过"
        ev = f"（{c['evidence']}）" if c.get("evidence") else ""
        print(f"- {c['item']}：{mark}{ev}")


# ------------------------------------------------------------------ 5. live


def load_system_prompt(slots_hint: dict) -> str:
    raw = (PROMPT_DIR / "01-system-prompt.md").read_text(encoding="utf-8")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (raw.replace("{{current_time}}", now)
               .replace("{{user_name}}", "用户")
               .replace("{{party_size}}", str(slots_hint.get("party_size", 1)))
               .replace("{{party_detail}}", slots_hint.get("party_detail", "未知"))
               .replace("{{user_profile}}", "（暂无）")
               .replace("{{conversation_summary}}", "（新会话）")
               .replace("{{destination}}", str(slots_hint.get("destination")))
               .replace("{{date_range}}", str(slots_hint.get("date_range")))
               .replace("{{days}}", str(slots_hint.get("days")))
               .replace("{{budget}}", str(slots_hint.get("budget_total")))
               .replace("{{budget_scope}}", "全队合计")
               .replace("{{hard_preferences}}", str(slots_hint.get("hard_prefs")))
               .replace("{{open_questions}}", str(find_missing(slots_hint))))


def run_live(utterance: str, max_turns: int = 8) -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("缺少 OPENAI_API_KEY，无法进入 live 模式。改用 dry 模式：\n")
        return run_dry(utterance)
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    tools = json.loads((PROMPT_DIR / "02-tool-schemas.json").read_text(encoding="utf-8"))["tools"]

    messages = [
        {"role": "system", "content": load_system_prompt(extract_slots(utterance))},
        {"role": "user", "content": utterance},
    ]

    for turn in range(max_turns):
        payload = {"model": model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "temperature": 0.4}
        req = urllib.request.Request(
            f"{base}/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
        msg = data["choices"][0]["message"]
        messages.append(msg)

        calls = msg.get("tool_calls") or []
        if not calls:
            print(msg.get("content", ""))
            return
        for c in calls:
            name = c["function"]["name"]
            args = json.loads(c["function"].get("arguments") or "{}")
            print(f"[tool] {name}({json.dumps(args, ensure_ascii=False)})")
            result = dispatch(name, args)
            print(f"       → {json.dumps(result, ensure_ascii=False)[:300]}")
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(result, ensure_ascii=False)})
    print("达到最大轮次，未收敛。")


# ------------------------------------------------------------------ main

CASES = [
    "我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶，我老婆不吃辣",
    "下个月想去云南玩几天，帮我安排下",
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dry", "live"], default="dry")
    ap.add_argument("--utterance")
    ap.add_argument("--explain", action="store_true", help="打印候选打分与每日成团日志")
    ap.add_argument("--llm", action="store_true",
                    help="用真实模型做槽位抽取与候选重排（需 OPENAI_API_KEY）")
    a = ap.parse_args()

    if a.mode == "live":
        run_live(a.utterance or CASES[0])
        sys.exit(0)

    client = None
    if a.llm:
        client = llm_layer.from_env()
        if client is None:
            print("未配置 OPENAI_API_KEY，本次仍用规则实现。\n"
                  "  设置方式：export OPENAI_API_KEY=sk-...\n"
                  "  可选：OPENAI_BASE_URL、OPENAI_MODEL、OPENAI_TIMEOUT\n")
        else:
            print(f"（LLM 已启用：{client.model} @ {client.base_url}）\n")

    if a.utterance:
        run_dry(a.utterance, a.explain, client=client)
    else:
        for case in CASES:
            print("=" * 72)
            run_dry(case, a.explain, client=client)
            print()
