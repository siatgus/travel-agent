"""多轮会话：把提示词里的"追问规则"变成真正的状态机。

之前 `find_missing()` 只是打印一句"触发追问"就 return，提示词第 47~50 行的三条规则
一条都没落地——每轮都从零开始，用户答了什么系统不记得。真实对话里这直接崩：

    用户：下个月想去云南玩几天
    系统：打算玩几天？          ← 然后就没有然后了

这里补上四件事，每件都对应提示词里的一句话：

1. **选择性追问**（第 47 行）：一次只问最关键的 1~2 个槽位，并给默认建议。
   把所有缺口一次性抛给用户是最常见的偷懒做法。
2. **两轮上限**（第 50 行）：连续追问 2 轮仍不全，第 3 次必须带假设出方案。
3. **假设标注**（第 50 行）：自动补的值必须显式写出来，不能让用户以为是自己说的。
4. **缺预算不追问**（第 48 行）：给出经济/舒适/品质三档让用户选，而不是逼他报数。

槽位合并是这里的核心难点：第二轮用户只说"4 天"时，抽取器会把它当成一个全新的
需求（目的地为空、人数为默认 1）。所以必须区分"用户这轮说了什么"和"抽取器的默认值"，
只覆盖前者。`_explicit_fields()` 就是为了这件事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import agent
from . import autofetch
from . import composer
from . import intent
from . import llm
from . import real_tools
from . import websearch
from .composer import name_hit   # 点名/排除与 POI 名的双向匹配（与编排器同源）
from .tools import haversine_km


# ------------------------------------------------------------------ 追问规则

# (字段, 问法, 默认建议)。提示词要求"给出默认建议"，所以每句都带一个 hint。
ASK_RULES: list[tuple[str, str, str]] = [
    ("destination", "想去哪儿？", "没想好的话，10 月底大理和婺源天气都不错，都不算赶"),
    ("date_range", "大概什么时候出发？", "错开节假日，机票酒店能便宜三成"),
    ("days", "打算玩几天？", "周边游 4 天最舒服，赶一点 3 天也够"),
    # 人数以前从没问过：用户只说"想去上海玩"，系统就按 1 人算房型、算人均，
    # 出来一份跟实际人数无关的预算，这是"没问清楚就定行程"最典型的一处。
    ("party_size", "几个人出行？", "人数决定房型和人均，说个大概就行，1 人也照排"),
]

MAX_ASKS_PER_FIELD = 2  # 同一件事最多问 2 次；第 3 次还空着就按常见情况假设
MAX_CLARIFY_ROUNDS = 3  # 整个追问阶段最多 3 轮，再多就不像聊天、像审讯了

# 每个字段的问法按"第几次问"升级：第一遍正常问，第二遍承认没听清再换个说法。
# 复读同一句会让用户觉得系统没在听，这是最伤体感的一种"追问"。
ASK_TEXT: dict[str, list[tuple[str, str]]] = {
    "destination": [
        ("想去哪儿？", "给我城市名就行，能排具体行程的是 {cities}"),
        ("目的地还没定下来？先说个城市，我先看看那边值不值得去。",
         "大理、丽江、厦门这类都能直接排"),
    ],
    "date_range": [
        ("大概什么时候出发？", "错开节假日，机票酒店能便宜三成"),
        ("时间这块我还没听清——这周、下个月，或者具体某一天，给个大概就够。",
         "只到月份也行，具体日期我按月初算"),
    ],
    "days": [
        ("打算玩几天？", "周边游 4 天最舒服，赶一点 3 天也够"),
        ("天数也没说——3 天、5 天都行，给个大概我就按它排。",
         "没想好的话我先按 4 天，回头改起来也快"),
    ],
    "party_size": [
        ("几个人出行？", "人数影响房型和人均预算，1 人也照排"),
        ("人数这块还没听清——一个人、两个人，还是带家人一起？",
         "说个大概就行，我按最接近的房型算"),
    ],
}

# 用户在反问我（"你不问我从哪出发吗"）时，先正面回一句，再把问题接回去。
# 直接无视他的问题会让对话明显断掉——他那句话是在指出我漏了信息。
_BACKQ_PREFIX = {
    "origin": "出发地你不说我就先不安排往返大交通，行程照排；"
              "你说个城市，我马上把班次和交通费补进去。",
    "budget": "预算没说我就先按人均 1500～2500 这一档算，超了会提醒你。",
    "default": "这个我待会儿一起说，先把关键的两件定了。",
}


# 用户说「还没定/看情况/随便」时不要追着问——那是答案，只是模糊。
# 继续追问只会让人烦，正确做法是给出假设并说明可以随时改。
_VAGUE = {
    "date_range": ("还没定", "没定下来", "不确定", "看情况", "再说", "到时候",
                   "过几天", "过些天", "有空", "不知道"),
    "days": ("还没定", "不确定", "看情况", "不知道", "没想好", "再说"),
}

# 「你看着办」是对整件事的甩手，不是只针对刚问的那一件：所有缺口一起假设。
# 而「还没定」只针对被问的那件——不问清楚是哪件就一起假设，会把用户
# 明明愿意回答的问题也一起跳过。
_GLOBAL_VAGUE = ("随便", "都行", "你看着办", "看着办", "你定", "你决定", "听你的")
_TIME_TOPIC = ("时间", "日期", "几号", "哪天", "什么时候", "出发")
_DAYS_TOPIC = ("几天", "天数", "玩多久", "待几天")


def serviceable_cities() -> list[str]:
    """有完整 POI/酒店数据的城市。

    真实产品这里应该查 POI 服务覆盖列表；mock 阶段直接读数据集。
    用途是拦住"用户说云南、系统按城市查、工具返回 NO_POI、编排器却照排一份
    只有交通住宿的空行程"这种静默失败。
    """
    from .tools import POIS_DB
    cities = set(POIS_DB)
    try:                       # 知识库里由管理后端导入/同步的城市同样可服务
        from . import kb
        db = kb.get_kb()
        if db:
            cities.update(db.cities())
    except Exception:
        pass
    return sorted(cities)


@dataclass
class Reply:
    """一轮系统输出。mode 只有两种：追问或出方案。"""

    mode: str                                  # "clarify" | "plan"
    turn_no: int = 0
    ask_rounds: int = 0
    question: str = ""
    asked: list[str] = field(default_factory=list)
    slots: dict = field(default_factory=dict)
    plan: dict | None = None
    assumptions: list[str] = field(default_factory=list)
    budget_tiers: dict | None = None
    trace: list = field(default_factory=list)
    render: str = ""
    fetched: dict = field(default_factory=dict)   # 本轮自动补全的城市 → 结果

    def as_dict(self) -> dict:
        return {"mode": self.mode, "turn_no": self.turn_no,
                "ask_rounds": self.ask_rounds, "question": self.question,
                "asked": self.asked, "assumptions": self.assumptions,
                "budget_tiers": self.budget_tiers,
                "fetched": self.fetched,
                "has_plan": self.plan is not None}


# ------------------------------------------------------------------ 槽位合并


_RESTORE_RE = re.compile(
    r"加回来|加回去|加回|加进来|重新安排|安排上|安排回来|恢复|保留|留着|"
    r"还是想去|还是要去|又想去了|还是去吧")

# 「重新排一下」「重排一遍」：改现有行程，不是从零开始。
# 之前这类说法被当成全新的空需求，目的地/天数/人数重新问一遍，
# 用户会觉得"刚聊完的行程它全忘了"。
_REPLAN_RE = re.compile(
    r"重新排|重排|再排一?遍|重新安排|再安排一次|重新规划|重新来|重做|"
    r"换个排法|换一版|再给我排")


def _restore_intent(text: str) -> bool:
    """这句话是不是在"把之前排除的景点拿回来"。"""
    return bool(_RESTORE_RE.search(text or ""))


# 节奏档位：数字越大越紧。用来判断这轮是"放松"还是"收紧"——
# 放松要就地删点，收紧才该重排（加点）。
_PACE_RANK = {"relaxed": 0, "moderate": 1, "intensive": 2}


def _pace_rank(pace: str | None) -> int:
    return _PACE_RANK.get(str(pace or ""), 1)


def _explicit_fields(text: str, delta: dict) -> set[str]:
    """判断这一轮用户到底"说了什么"，而不是抽取器"默认填了什么"。

    没有这一步，用户第二轮回答"4 天"会把第一轮的目的地、人数、节奏全部冲掉。
    """
    got: set[str] = set()
    if delta.get("destination"):
        got.add("destination")
    if delta.get("origin"):
        got.add("origin")
    if delta.get("date_range"):
        got.add("date_range")
    if re.search(r"[一二两三四五六七八九十\d]+\s*天", text):
        got.add("days")
    if re.search(r"[一二两三四五六七八九十\d]+\s*(?:个)?人|我和|夫妻|孩子|娃|宝宝|"
                 r"小孩|儿童|一家[三四五六]口|全家|(?:我们|我)(?:俩|两)|"
                 r"[一二两三四五六七八九十\d]+\s*大\s*[一二两三四五六七八九十\d]+\s*小",
                 text):
        got.add("party_size")
        # 人数构成的字段跟着人数一起算"这轮说过"，否则 merge 会留上一轮的旧值
        for k in ("adults", "children", "child_age", "party_detail"):
            if delta.get(k) not in (None, "", [], {}):
                got.add(k)
    if delta.get("budget_per_person"):
        got.add("budget_per_person")
    if re.search(r"(?:总共|一共|预算)[^\d]{0,6}\d{3,6}", text) and delta.get("budget_total"):
        got.add("budget_total")
    if delta.get("hard_prefs"):
        got.add("hard_prefs")
    if any(not llm.is_pace_word(p) for p in (delta.get("soft_prefs") or [])):
        # 「轻松休闲一点」是节奏，不是"这轮说了兴趣"——标成显式会让
        # 下游以为兴趣已经答过，兴趣追问就此跳过，而 soft_prefs 其实是空的
        got.add("soft_prefs")
    if delta.get("must_visit"):
        got.add("must_visit")
    if delta.get("avoid"):
        got.add("avoid")
    if re.search(r"不想太赶|轻松|躺平|休闲|特种兵|尽量多|打卡", text):
        got.add("pace")
    if delta.get("stay_pref"):
        got.add("stay_pref")
    return got


def _dedupe_names(names: list[str]) -> list[str]:
    """同一景点的不同叫法只留一个（迪士尼 / 上海迪士尼乐园）。

    模型常把「迪士尼」写进 must_visit，而补全后库里的全名是「上海迪士尼乐园」。
    两个都留着，方案里会重复报告"已确保安排：上海迪士尼乐园、迪士尼"，
    打分也会重复加成。保留更长的那个（信息更全）。
    """
    out: list[str] = []
    for n in sorted({x.strip() for x in (names or []) if x and x.strip()},
                    key=lambda s: -len(s)):
        if not any(name_hit(n, kept) for kept in out):
            out.append(n)
    return out


def merge_slots(base: dict, delta: dict, explicit: set[str],
                total_is_derived: bool) -> tuple[dict, bool]:
    """把本轮明确说到的字段合并进来，未提及的保持原值。

    返回 (新槽位, total 是否为派生值)。派生标记用于：用户先说"一人 3000"再说"2 人"时，
    总额应跟着重算成 6000；而用户明说"总共 6000"时，人数变化不该改动总额。
    """
    out = dict(base)
    for k in explicit:
        if k in ("hard_prefs", "soft_prefs", "must_visit", "avoid"):
            out[k] = sorted(set((base.get(k) or []) + (delta.get(k) or [])))
            if k in ("must_visit", "avoid"):
                out[k] = _dedupe_names(out[k])
        else:
            v = delta.get(k)
            if v not in (None, "", [], {}):
                if k == "date_range" and isinstance(v, dict) and isinstance(base.get(k), dict):
                    # 日期可能分两轮说全：先「这个月」再补「20号」（或反序）。
                    # 部分信息要合并而不是覆盖——覆盖会把上一轮说的一半丢掉，
                    # 剩下半个就折成"第二个周六"这种用户没说过的日子。
                    prev = dict(base[k])
                    if "start" in v:
                        out[k] = dict(v)          # 完整日期直接生效
                    else:
                        prev.update(v)            # 部分信息：本轮的键优先
                        out[k] = prev
                else:
                    out[k] = v

    # 用户改主意：本轮点名的景点要从排除清单里撤回来。
    # 「把豫园加回来」如果只往 must_visit 里加、不撤 avoid，下面那条
    # 「排除赢过必去」会立刻把它删掉——用户怎么说都加不回去。
    new_must = [m for m in (delta.get("must_visit") or []) if m]
    if new_must and out.get("avoid"):
        out["avoid"] = [a for a in out["avoid"]
                        if not any(name_hit(m, a) for m in new_must)]

    # 这轮只说了「N 个人」而没提构成 → 大人/孩子/年龄的旧值全部作废。
    # 否则先说"2大1小"再说"5个人"，children 还挂着 1，门票口径就乱了。
    if "party_size" in explicit and "party_detail" not in explicit:
        for k in ("adults", "children", "child_age", "party_detail"):
            out[k] = None

    # 排除赢过必去：这轮刚说"不想去豫园"而 must_visit 里还留着它（LLM 上一轮
    # 抽进去的）时，必须从必去清单里拿掉，否则两份约束互相打架。
    if out.get("avoid"):
        out["must_visit"] = [m for m in (out.get("must_visit") or [])
                             if not any(name_hit(m, a) for a in out["avoid"])]

    if "budget_total" in explicit:
        total_is_derived = False
    elif "budget_per_person" in explicit and out.get("budget_per_person"):
        out["budget_total"] = out["budget_per_person"] * (out.get("party_size") or 1)
        total_is_derived = True
    elif total_is_derived and "party_size" in explicit:
        # 用户先报人均，后改人数 → 总额跟着变
        out["budget_total"] = (out.get("budget_per_person") or 0) * (out.get("party_size") or 1)
    return out, total_is_derived


# ------------------------------------------------------------------ 会话


class Session:
    """一次对话的全部状态。每次 turn() 消化一句用户输入。"""

    def __init__(self, client=None, today: date | None = None,
                 memory=None, use_llm: bool = False):
        self.client = client
        self.today = today or date.today()
        self.memory = memory
        self.turn_no = 0
        self.ask_rounds = 0
        self.field_asks: dict[str, int] = {}   # 每个槽位问过几次（问满即假设，不无限纠缠）
        self.vague_fields: set[str] = set()    # 用户明说"还没定"的槽位，不再追着问
        self.pending_field: str | None = None  # 上一轮在等哪个字段的答案
        self.total_is_derived = False
        self.slots: dict = {}
        self.history: list[dict] = []
        self._cov: dict[str, bool] = {}
        self.asked_fields: set[str] = set()   # 已经问过的槽位，第二轮不再重复问
        self.last_good_dest: str | None = None  # 最近一次确认过有数据的目的地
        self.last_city: str | None = None     # 最近提到的城市（问天气后接着排行程要用）
        self.weather_rounds = 0               # 连续问天气的轮数（避免每轮重复同一段引导）
        self.fetched: dict[str, dict] = {}    # 自动补全过的城市 → 结果（会话内去重）
        self.max_autofetch = 3                # 一次会话最多为几个新城市联网抓数据
        # 酒店定向补库记录：城市 → ensure_hotels 结果（住宿问答用，与景点补全分开）
        self._hotel_fetched: dict[str, dict] = {}
        self._rest_fetched: dict[str, dict] = {}
        # 人数是否被用户明确说过。party_size 默认就是 1，光看槽位分不清
        # "用户说了一个人" 和 "用户压根没提"，后者要问一次再排。
        self.party_stated = False
        self.trip_dest: str | None = None     # 当前这份行程的目的地（换城市=新一轮）
        self.vague_hits = 0                   # 连续「你看着办」的次数（第 2 次即整体假设）
        # 本会话是否已经出过方案。用来区分"从零开始"和"改现有行程"：
        # 已经排好之后用户说"重新排/加个迪士尼"，不该再把已确认的条件丢掉重问。
        self.plan_made = False
        self.last_plan_desc: str | None = None   # 上一份方案的一句话描述（上海 4 天）
        # 问过"它在哪个城市"的那个景点名：用户接下来答的城市要把它带上，
        # 否则"迪士尼在哪个城市？"→"上海"会把迪士尼本身丢掉。
        self.pending_spot: str | None = None
        # 已经记进 must_visit、但还没出过方案的点名（跨轮带到出方案那一轮去汇报）
        self.pending_must: list[str] = []
        # 最近一次出方案的完整结果（plan_trip 返回值）。「总结一下这个行程」
        # 这类行程问答的事实来源——没有它就只能重新算一遍。
        self.last_plan: dict | None = None
        # 阶段回调（服务端注入，用于等待页进度）；None 表示不上报
        self.on_phase = None

    # -------------------------------------------------- 存档（历史行程/续聊）
    # 上下文范围 = 一次对话（一个 sid 一个 Session）。存档让这份上下文在进程
    # 重启、换设备后也能接上：恢复的是槽位与追问状态，而不是把整段 history
    # 塞回模型——模型依旧只见当前这句，跨轮记忆始终由规则层维护。
    def snapshot(self) -> dict:
        """可 JSON 序列化的会话存档。"""
        return {
            "slots": dict(self.slots),
            "turn_no": self.turn_no,
            "ask_rounds": self.ask_rounds,
            "field_asks": dict(self.field_asks),
            "vague_fields": sorted(self.vague_fields),
            "pending_field": self.pending_field,
            "total_is_derived": self.total_is_derived,
            "asked_fields": sorted(self.asked_fields),
            "last_good_dest": self.last_good_dest,
            "last_city": self.last_city,
            "weather_rounds": self.weather_rounds,
            "party_stated": self.party_stated,
            "trip_dest": self.trip_dest,
            "vague_hits": self.vague_hits,
            "plan_made": self.plan_made,
            "last_plan_desc": self.last_plan_desc,
            "pending_spot": self.pending_spot,
            "pending_must": list(self.pending_must),
            "last_plan": self.last_plan,
            "history": [dict(m) for m in self.history],
            "_cov": dict(self._cov),
        }

    def restore(self, snap: dict) -> None:
        """按存档重建会话状态；字段缺失按零值处理（容忍旧版本存档）。"""
        self.slots = dict(snap.get("slots") or {})
        self.turn_no = int(snap.get("turn_no") or 0)
        self.ask_rounds = int(snap.get("ask_rounds") or 0)
        self.field_asks = {k: int(v) for k, v in (snap.get("field_asks") or {}).items()}
        self.vague_fields = set(snap.get("vague_fields") or ())
        self.pending_field = snap.get("pending_field")
        self.total_is_derived = bool(snap.get("total_is_derived"))
        self.asked_fields = set(snap.get("asked_fields") or ())
        self.last_good_dest = snap.get("last_good_dest")
        self.last_city = snap.get("last_city")
        self.weather_rounds = int(snap.get("weather_rounds") or 0)
        self.party_stated = bool(snap.get("party_stated"))
        self.trip_dest = snap.get("trip_dest")
        self.vague_hits = int(snap.get("vague_hits") or 0)
        self.plan_made = bool(snap.get("plan_made"))
        self.last_plan_desc = snap.get("last_plan_desc")
        self.pending_spot = snap.get("pending_spot")
        self.pending_must = list(snap.get("pending_must") or [])
        self.last_plan = snap.get("last_plan") or None
        self.history = [dict(m) for m in (snap.get("history") or [])]
        self._cov = dict(snap.get("_cov") or {})

    # -------------------------------------------------- 意图直答
    def _answer(self, kind: str, utterance: str) -> str | None:
        """信息类问题的直接回答；返回 None 表示交回排行程流程处理。

        配了模型时，工具查到的事实会交给模型组织成人话（intent 里做）；
        没配模型或模型输出跑偏，返回的就是模板——两条路的事实完全一致。

        意图分发表（2026-11-13 拆分）：kind 仍是 classify 的粗分类
        （规则优先 + LLM 兜底，规则没推翻）；poi 类再经 intent.refine_intent
        细分成景点/美食/餐厅/酒店/路线等子意图，各自独立方法，分支不再互相
        堆在一个函数里。城市解析与会话状态（last_city/轮次计数）留在这里。
        """
        # 城市判定顺序：本句里的城市 > 当前行程目的地 > 最近提到的城市。
        # 「天安门广场周边有什么」一句里未必带"北京"，但行程本身在北京。
        city = (intent.city_in(utterance) or self.slots.get("destination")
                or self.last_city or self.trip_dest)
        if kind == "weather":
            return self._weather_answer(city, utterance)
        if kind == "poi":
            return self._poi_answer(city, utterance)
        if kind == "transport":
            return self._transport_answer(city, utterance)
        if kind == "smalltalk":
            return self._smalltalk_answer(utterance)
        if kind == "plan_qa":
            # 没出过方案就不拦：这句可能是在闲聊，交回排行程流程正常处理
            if not (self.plan_made and self.last_plan):
                return None
            return self._plan_qa_answer(utterance)
        if kind == "general":
            return self._general_answer(utterance)
        return None

    def _weather_answer(self, city: str | None, utterance: str) -> str:
        """天气问句：真实预报优先，超出窗口自动用历史同期均值。"""
        if not city:
            return intent.weather_answer("", utterance)
        self.last_city = city
        first = self.weather_rounds == 0
        self.weather_rounds += 1
        return intent.weather_answer(city, utterance, with_hint=first,
                                     client=self.client)

    def _transport_answer(self, city: str | None, utterance: str) -> str:
        """城际交通问句：班次/时刻/票价来自真实班次库。"""
        origin, dest = agent._extract_route(utterance)
        origin = origin or self.slots.get("origin")
        dest = dest or self.slots.get("destination") or None
        if dest:
            self.last_city = dest
        return intent.transport_answer(utterance, origin, dest,
                                       client=self.client)

    def _smalltalk_answer(self, utterance: str) -> str:
        """闲聊/身份问句：带人设的自由作答（不冒充商业型号、不编数字）。"""
        return intent.smalltalk_answer(utterance, client=self.client)

    def _poi_answer(self, city: str, utterance: str) -> str:
        """poi 粗分类下的细分分流。

        refine_intent 只决定「先试哪一路」；某一路没接住（返回 None，比如
        联网失败）就按原链路继续往后试，兜底仍是泛推荐——拆分前后行为一致。
        """
        if not city:
            return intent.poi_answer("")
        self.last_city = city
        chain = (("general", self._comparison_answer),
                 ("route_query", self._route_answer),
                 ("attraction_search", self._attraction_answer),
                 ("food_query", self._food_answer),
                 ("restaurant_search", self._restaurant_answer),
                 ("hotel_search", self._hotel_answer))
        fine = intent.refine_intent(utterance, city)
        names = [n for n, _ in chain]
        start = names.index(fine) if fine in names else 0
        for _, fn in chain[start:]:
            said = fn(city, utterance)
            if said:
                return said
        return intent.poi_answer(city, client=self.client)

    def _comparison_answer(self, city: str, utterance: str) -> str | None:
        """类比问句（「有没有像迪士尼一样的地方」）→ 联网检索。

        库里没有类型标签，套本地清单会答成古城石窟（实测答非所问）；
        联网没接住返回 None，交回链路继续。
        """
        if not intent.is_comparison_question(utterance):
            return None
        return self._general_answer(utterance)

    def _attraction_answer(self, city: str, utterance: str) -> str | None:
        """景点咨询三路：点名景点（含周边泛问）、品类问句、按关键词推荐。"""
        # 「上海迪士尼门票多少钱」是问具体景点，不是问「上海有什么好玩的」。
        # 先按名字查库；库里没有就联网/问模型补一条入库再答——而不是把
        # 具体点名当成泛推荐，回一串不相干的热门景点。
        spot = intent.spot_mention(utterance, city)
        if spot:
            if re.search(r"周边|附近|周围", utterance):
                return self._nearby_qa(city, spot)
            return self._spot_qa(city, spot)
        # 问的是「某个品类」（游乐园/动物园/温泉…）：库里没有类型标签，
        # 泛推荐会把广府古城、娲皇宫端上来（实测「中小型游乐园」就这么答的）。
        # 先按品类词筛本地收录：有就只报这些；没有再去联网/问模型；
        # 最后才如实说这类没收录——绝不拿不相干的景点凑数。
        cat = intent.poi_category(utterance)
        if cat:
            label, words = cat
            allr = agent.dispatch("search_poi", {"city": city, "limit": 200})
            allp = (allr.get("items") or []) if allr.get("ok") else []
            hit = intent.category_pois(allp, words)
            if hit:
                return intent.recommend_answer(city, label, hit[:5],
                                               client=self.client)
            said = self._general_answer(f"{city}有没有{label}")
            if said:
                return said
            return (f"{city}的资料里我还没收录「{label}」这类地方，不敢瞎报名字。\n"
                    f"可以说个具体名字，我查到就给你介绍；或者说清想玩什么，"
                    f"排行程时我按这个挑点。")
        # 「推荐几家驴肉火烧」：问的是这一类里有什么，不是点名单个店。
        # 之前落到别的分支被当成改行程整单重排（实测）。
        kw = intent.reco_keyword(utterance, city)
        if kw:
            r = agent.dispatch("search_poi",
                               {"city": city, "keyword": kw, "limit": 5})
            items = (r.get("items") or []) if r.get("ok") else []
            if not items and len(kw) >= 4:
                # 用户常记错店名头一个字（「老驴肉火烧」≠「老驴头驴肉火烧」）：
                # 掉首字再试一次，别急着说没收录
                r2 = agent.dispatch("search_poi",
                                    {"city": city, "keyword": kw[1:],
                                     "limit": 5})
                items = (r2.get("items") or []) if r2.get("ok") else []
            return intent.recommend_answer(city, kw, items,
                                           client=self.client)
        return None

    def _food_answer(self, city: str, utterance: str) -> str | None:
        """美食知识问句（「有什么特色美食/火锅有什么特色」）→ 菜品文化。"""
        if not (intent.is_food_question(utterance)
                and intent.food_question_kind(utterance) != "restaurant"):
            return None
        return self._food_knowledge_answer(city, utterance)

    def _restaurant_answer(self, city: str, utterance: str) -> str | None:
        """餐厅搜索问句（「火锅店/川菜馆/春熙路附近的馆子」）→ 真实商家。"""
        if not (intent.is_food_question(utterance)
                and intent.food_question_kind(utterance) == "restaurant"):
            return None
        return self._restaurant_poi_answer(city, utterance)

    def _hotel_answer(self, city: str, utterance: str) -> str | None:
        """住宿问句（「有什么酒店」）→ 报在售酒店（起价口径），不是新规划。"""
        if not intent.is_stay_question(utterance):
            return None
        # 出过方案时先看是不是在问这份行程安排的住宿：「之前安排的酒店在哪
        # 里」问的是方案里的入住节点，回一张全城酒店清单是答非所问。
        if (self.plan_made and self.last_plan
                and re.search(r"酒店|住宿|住处|住哪", utterance)
                and re.search(r"之前|刚才|行程|安排的|订的|定的|已订", utterance)):
            stay = self._plan_stay_answer()
            if stay:
                return stay
        return self._stay_answer(city, utterance)

    def _plan_stay_answer(self) -> str | None:
        """回答「之前安排的酒店在哪里」：列出方案里的入住节点。"""
        rows: list[str] = []
        names: list[str] = []
        for i, d in enumerate((self.last_plan or {}).get("days_plan") or [], 1):
            for n in d.get("nodes") or []:
                if n.get("type") == "hotel" and n.get("name"):
                    rows.append(f"- 第 {i} 天晚上：{n['name']}"
                                + (f"（{n['tip']}）" if n.get("tip") else ""))
                    core = re.sub(r"^入住：", "", n["name"])
                    if core not in names:
                        names.append(core)
        if not rows:
            return None
        return ("这趟行程安排的住宿：" + "、".join(names) + "。\n"
                + "\n".join(rows)
                + "\n\n要换酒店的话，说个预算或片区，我重新按它排。")

    def _route_answer(self, city: str, utterance: str) -> str | None:
        """市内两点路线（「从X怎么去Y」）：两端都在库里才接，否则交回链路。"""
        ends = intent.nearby_route_ends(utterance)
        if not ends:
            return None

        def strip_city(s: str) -> str:
            s2 = s.replace(city, "").strip("的了呀呢吧，, 　")
            return s2 or s

        pa = self._resolve_spot(city, strip_city(ends[0]))
        pb = self._resolve_spot(city, strip_city(ends[1]))
        if not (pa and pb) or (pa.get("name") or ends[0]) == (pb.get("name") or ends[1]):
            return None
        return self._route_qa(ends[0], ends[1], pa, pb)

    def _llm_history(self, limit: int = 6, per_msg: int = 240,
                     total: int = 800) -> list[dict]:
        """给说话层/闲聊层的最近几轮（不含本轮这句，它单独作为 user 消息传）。

        只喂「说话」和「闲聊」两个入口：让回答接得上上一轮的指代（「那它呢」）
        和语气。分类、槽位抽取、数据校验一律单句——历史进去会让模型把上一轮
        的意图/日期带到这一轮，判错面变大。
        行程正文很长（一整份方案），按 per_msg 截断、按 total 总字数从旧往新
        砍，避免每轮把整份行程塞进 prompt。
        """
        turns = [m for m in self.history[:-1] if (m.get("content") or "").strip()]
        out: list[dict] = []
        for m in turns[-limit:]:
            role = "assistant" if m.get("role") == "agent" else "user"
            c = str(m["content"]).strip()
            if len(c) > per_msg:
                c = c[:per_msg].rstrip() + "…"
            out.append({"role": role, "content": c})
        while out and sum(len(x["content"]) for x in out) > total:
            out.pop(0)
        return out

    # -------------------------------------------------- 主入口
    def turn(self, utterance: str) -> Reply:
        """混合指令拆分：交通时刻修改从句拒绝，其余行程请求照常处理。

        「把出发时间改成10点，去成都玩3天」是两件事——之前整句连坐
        拒绝，还反问"直接说去哪儿玩几天"，可目的地和天数用户明明都
        说了。这里只把时刻从句剔掉，剩余部分原样走主流程。
        """
        rest, dropped = intent.split_transport_time_change(utterance)
        if dropped is None or not rest.strip():
            return self._turn_impl(utterance)
        reply = self._turn_impl(rest, history_text=utterance)
        note = (f"> 「{dropped}」这条没照办：**去程/返程的时刻来自真实"
                "班次查询，我不能直接改数字**——想换班次就换出发日期或"
                "出发地，我查到真实班次后再排。下面只处理剩下的行程请求：")
        reply.render = note + "\n\n" + reply.render
        return reply

    def _plan_spot_gate(self, utterance: str) -> str | None:
        """点名行程里的真实店/景点问细节 → 直接按该 POI 作答，不赌意图分类。

        「你给我排的老驴头驴肉火烧在哪里」带着指代前缀和位置尾巴，规则
        分类只会给 other、spot_mention 也剥不净——曾被当成泛推荐回一串
        不相干景点（实测「驴头不对马嘴」）。只要句式是问细节、又不是改
        行程指令，行程里点到了名就答那个点。
        """
        if not (self.plan_made and self.last_plan):
            return None
        if not re.search(r"哪里|在哪|地址|位置|怎么样|怎么去|怎么走|多少钱|门票"
                         r"|好吃|味道|几点|远不远|值得|吗|[？?]", utterance):
            return None
        # 改行程的指令绝不拦：「不想去X」「把X换成Y」「X加进必去」……
        if re.search(r"不想去|换[成掉]|删|去掉|不要|排除|加[进上到]|改成|必去"
                     r"|排进|重排|加一天|多住|天气|下雨|气温|温度|穿什么", utterance):
            return None
        hits: list[str] = []
        for d in (self.last_plan or {}).get("days_plan") or []:
            for n in d.get("nodes") or []:
                if self._is_filler_node(n):
                    continue
                nm = n.get("name") or ""
                core = re.sub(r"[（(][^（）()]*[)）]", "", nm).strip()
                # 短于 4 字的核心名太泛（「午餐」），不当点名依据
                if len(core) >= 4 and core in utterance and core not in hits:
                    hits.append(core)
        if not hits:
            return None
        city = (self.slots.get("destination") or self.trip_dest
                or self.last_city or "")
        answers = [self._spot_qa(city, core) for core in hits[:3]]
        return "\n\n".join(answers)

    # -------------------------------------------------- 周边问句分流

    def _nearby_gate(self, utterance: str) -> str | None:
        """周边问句独立分流（放在景点点名拦截之前）：

        「宽窄巷子附近有什么好吃的」→ 餐厅搜索；「附近有什么酒店」→ 酒店搜索；
        「从宽窄巷子怎么去锦里」→ 两点路线。都不是「宽窄巷子是什么地方」。

        分流以「锚点/两端真的在库里且带坐标」为闸：查不到一律交回普通流程
        （改方案的指令句也交回），绝不凭空答。
        """
        # 改行程的指令绝不拦：「把宽窄巷子附近的餐厅排进第二天」是改方案
        if re.search(r"不想去|换[成掉]|删|去掉|不要|排除|加[进上到]|改成|必去"
                     r"|排进|重排|加一天|多住", utterance):
            return None
        if intent.classify(utterance) in ("plan", "plan_qa"):
            return None
        city = (intent.city_in(utterance) or self.slots.get("destination")
                or self.last_city or self.trip_dest)
        if not city:
            return None

        def strip_city(s: str) -> str:
            s2 = s.replace(city, "").strip("的了呀呢吧，, 　")
            return s2 or s

        # 1) 两点路线：「从X怎么去Y」
        ends = intent.nearby_route_ends(utterance)
        if ends:
            pa = self._resolve_spot(city, strip_city(ends[0]))
            pb = self._resolve_spot(city, strip_city(ends[1]))
            if pa and pb and (pa.get("name") or ends[0]) != (pb.get("name") or ends[1]):
                return self._route_qa(ends[0], ends[1], pa, pb)
            return None          # 两端没查全：多半是城际/规划句，交回普通流程
        # 2) 锚点周边：找吃的 / 找住的（「XX附近有什么」泛问留给景点周边）
        if not (intent.is_nearby_food_question(utterance)
                or intent.is_nearby_hotel_question(utterance)):
            return None
        anchor = intent.nearby_anchor(utterance, city) or ""
        base = self._resolve_spot(city, anchor)
        if not (base and base.get("geo")):
            return None          # 锚点没收录：交回普通景点/餐厅流程
        if intent.is_nearby_food_question(utterance):
            return self._restaurant_nearby_qa(city, utterance, anchor, base)
        return self._hotel_nearby_qa(city, utterance, anchor, base)

    def _resolve_spot(self, city: str, name: str) -> dict | None:
        """按名字查库里的 POI（要带坐标）；查不到返回 None，调用方交回普通流程。"""
        if not name or len(name) < 2:
            return None
        r = agent.dispatch("search_poi", {"city": city, "keyword": name,
                                          "limit": 3})
        items = (r.get("items") or []) if r.get("ok") else []
        for p in items:
            nm = p.get("name") or ""
            if p.get("geo") and (name in nm or nm in name):
                return p
        for p in items:          # 放宽：keyword 命中即认，取第一个带坐标的
            if p.get("geo"):
                return p
        return None

    @staticmethod
    def _within(lat: float, lon: float, items: list[dict],
                center: str, radius_km: float) -> list[dict]:
        """筛出离（lat, lon）radius_km 公里内、按距离由近到远排序的条目。"""
        out = []
        for p in items:
            geo = p.get("geo")
            if not geo or not (geo[0] or geo[1]):
                continue
            nm = p.get("name") or ""
            if nm == center or nm in center or center in nm:
                continue          # 圆心自己不算"周边"
            km = haversine_km((lat, lon), geo)
            if km <= radius_km:
                out.append({**p, "_km": round(km, 1)})
        out.sort(key=lambda p: p["_km"])
        return out

    def _restaurant_nearby_qa(self, city: str, utterance: str,
                              anchor: str, base: dict) -> str:
        """「宽窄巷子附近有什么好吃的」：以点名景点为圆心搜真实餐厅。"""
        kw = intent.food_keyword(utterance, city)
        disp = base.get("name") or anchor
        lat, lon = base["geo"][0], base["geo"][1]
        args = {"city": city, "category": "restaurant", "limit": 200}
        r = agent.dispatch("search_poi", args)
        items = (r.get("items") or []) if r.get("ok") else []
        if not items:
            # 整座城市没收录餐厅：先走一次在线补库再答
            self._coverage(city)
            r = agent.dispatch("search_poi", args)
            items = (r.get("items") or []) if r.get("ok") else []
        pool = items
        if kw:
            hit = [p for p in items
                   if kw in (p.get("name") or "")
                   or any(kw in t for t in (p.get("tags") or []))]
            pool = hit or pool   # 品类词不在招牌上时放宽（与餐厅搜索同策略）
        for radius in (3, 10):
            near = self._within(lat, lon, pool, disp, radius)
            if not near:
                continue
            label = f"{disp}周边·{kw}" if kw else f"{disp}周边美食"
            text = intent.recommend_answer(city, label, near[:6],
                                           client=self.client)
            if radius > 3:
                text += (f"\n\n> 「{disp}」3 公里内暂无收录餐厅，"
                         f"以上是 {radius} 公里内由近到远的。")
            return text
        return (f"「{disp}」周边的餐厅我收录得还不够，不敢瞎报名字。\n"
                f"可以问「{city}有什么好吃的」，或排行程时让我安排用餐。")

    def _hotel_nearby_qa(self, city: str, utterance: str,
                         anchor: str, base: dict) -> str:
        """「宽窄巷子附近有什么酒店」：以点名景点为圆心由近到远报在售酒店。"""
        disp = base.get("name") or anchor
        lat, lon = base["geo"][0], base["geo"][1]
        din = (self.today + timedelta(days=1)).isoformat()
        dout = (self.today + timedelta(days=2)).isoformat()
        guests = int(self.slots.get("party_size") or 2)
        args = {"city": city, "check_in": din, "check_out": dout,
                "guests": guests}
        hotels, note = self._hotels_with_fetch(city, args)
        for radius in (3, 10):
            near = self._within(lat, lon, hotels, disp, radius)
            if not near:
                continue
            text = intent.stay_answer(city, near[:5], client=self.client,
                                      note=note)
            head = f"以「{disp}」为中心由近到远：\n\n"
            tail = "" if radius <= 3 else \
                (f"\n\n> 「{disp}」3 公里内暂无收录酒店，"
                 f"以上是 {radius} 公里内最近的。")
            return head + text + tail
        return (f"「{disp}」周边的酒店我收录得还不够，不敢瞎报名字和价格。\n"
                f"可以问「{city}有什么酒店」，或排行程时按住宿片区帮你选。")

    def _route_qa(self, a: str, b: str, pa: dict, pb: dict) -> str:
        """「从宽窄巷子怎么去锦里」：两点都在库里 → 真实路网给距离与耗时。"""
        disp_a = pa.get("name") or a
        disp_b = pb.get("name") or b
        km = haversine_km(pa["geo"], pb["geo"])
        mode, mins = real_tools.transit_estimate(km, pa["geo"], pb["geo"])
        km_txt = f"{km:.1f} 公里" if km >= 1 else f"{int(km * 1000)} 米"
        facts = {"起点": disp_a, "终点": disp_b, "直线距离": km_txt,
                 "方式": mode, "约几分钟": mins}
        said = intent._speak(self.client, "route", facts, f"从{a}怎么去{b}",
                             [mode, mins])
        tail = ("距离与耗时按地图路网估算，实际以实时路况为准；"
                "公交换乘未核实，不敢瞎报。")
        if said:
            return f"{said}\n\n> {tail}"
        if mode == "步行":
            return (f"「{disp_a}」离「{disp_b}」很近（约 {km_txt}），"
                    f"步行约 {mins} 分钟就能到。\n\n> {tail}")
        return (f"「{disp_a}」到「{disp_b}」直线约 {km_txt}，"
                f"{mode}约 {mins} 分钟。\n\n> {tail}")

    def _turn_impl(self, utterance: str,
                   history_text: str | None = None) -> Reply:
        """单轮主流程（薄入口）：交给 Orchestrator 调度。

        2026-11-13 Orchestrator 架构第一阶段（xingye_orchestrator_design.md
        第 11 节）：控制流迁入 orchestrator/ 包——TurnContext（单轮上下文）、
        IntentRouter（意图路由）、GuardPipeline（方案修改守卫）、Reply
        Builder（统一回复构建）。本方法与各业务处理方法（问答、守卫的原子
        操作、追问与规划流水线的步骤）保留在 Session 供 Handler 复用；
        第三阶段再把槽位合并/追问/plan_trip 入口迁入独立 PlanHandler。
        """
        from .orchestrator import Orchestrator
        orch = getattr(self, "_orchestrator", None)
        if orch is None:          # 会话级单例：last_ctx 观测点跨轮可查
            orch = self._orchestrator = Orchestrator()
        return orch.handle(self, utterance, history_text)

    # -------------------------------------------------- 内部
    def _phase(self, text: str) -> None:
        """上报"现在在做什么"给等待页；回调异常绝不影响排程本身。"""
        cb = getattr(self, "on_phase", None)
        if cb is None:
            return
        try:
            cb(text)
        except Exception:
            pass

    def _has(self, f: str) -> bool:
        if f == "date_range":
            dr = self.slots.get("date_range") or {}
            # 只到月份或只到日子都算"有"——resolve_date_range 会折算并标注，
            # 不必再问一遍。之前裸"20号"被当没答过，同一个问题问两次。
            return bool(dr.get("start") or dr.get("month") or dr.get("day"))
        if f == "party_size":
            # 默认值 1 不算"有"：用户没提人数时要问一次
            return self.party_stated
        return bool(self.slots.get(f))

    # -------------------------------------------------- 行程内节点问答
    @staticmethod
    def _is_filler_node(n: dict) -> bool:
        """系统补的衔接/填充项：不是具体景点，没有门票或介绍可查。

        早市/伴手礼、自由活动、就近自选是编排器补的占位安排，
        transit/rest/hotel 本来就不是景点——它们在库里永远查不到，
        走完联网补全再甩"换个城市"是最伤人的一路（用户实测）。
        """
        nm = n.get("name") or ""
        return (nm.startswith("早市") or nm.startswith("自由活动")
                or "就近自选" in nm
                or n.get("type") in ("transit", "rest", "hotel"))

    def _plan_node(self, spot: str) -> dict | None:
        """在当前方案里找用户点名的节点（填充项与真实景点都算）。"""
        if not (self.plan_made and self.last_plan) or not spot:
            return None
        for d in (self.last_plan or {}).get("days_plan") or []:
            for n in d.get("nodes") or []:
                nm = n.get("name") or ""
                if nm and (name_hit(spot, nm) or spot in nm):
                    return n
        return None

    def _filler_node_answer(self, node: dict) -> str:
        """用户问行程里的填充项：按它本来的身份答，而不是当景点查库。"""
        nm = node.get("name") or "这一项"
        if nm.startswith("早市"):
            return (f"「{nm}」不是收费景点，是我在返程日上午留的自由安排："
                    "早市本身不要门票，逛多久自己定，买特产也比景区包装款便宜。"
                    "想换成具体的地方（早市街、特产店都行），直接说名字，"
                    "我查到了就帮你排进去。")
        if nm.startswith("自由活动"):
            return (f"「{nm}」是行程里留的机动时间，不是具体景点，没有门票。"
                    "想把它换成某个景点或体验，说个名字就行。")
        if "就近自选" in nm:
            return (f"「{nm}」是附近没查到靠谱餐厅时留的用餐时段，不是具体店，"
                    "所以没有人均可查。说个你想吃的店名，我查到就帮你排进去。")
        tip = node.get("tip") or ""
        return (f"「{nm}」是行程里的衔接安排（{tip}），不是景点，没有门票可查。")

    def _spot_qa(self, city: str, spot: str) -> str:
        """回答「XX景点怎么样/门票多少钱」：库里查，查不到就在线补，再答不了如实说。"""
        # 点名的是行程里的填充项（早市/伴手礼等）：它在库里永远查不到，
        # 没必要联网补全，直接按节点本身回答。
        node = self._plan_node(spot)
        if node is not None and self._is_filler_node(node):
            return self._filler_node_answer(node)
        r = agent.dispatch("search_poi", {"city": city, "keyword": spot, "limit": 3})
        items = (r.get("items") or []) if r.get("ok") else []
        note = ""
        if not items:
            res = autofetch.ensure_spot(city, spot, client=self.client)
            st = res.get("status")
            if st in ("exists", "fetched"):
                items = [res.get("poi") or {}]
                if st == "fetched":
                    note = "这条是刚帮你在线查的：坐标与票价为检索值，出行前请再核实。"
            elif st == "custom":
                return (f"「{spot}」我没能查到可靠资料（在线数据源与模型都没拿到），"
                        f"不敢编门票和位置。\n要排进行程的话可以直接说"
                        f"「我想去{spot}」，我按你说的名字安排，细节你再确认。")
            else:
                # disabled / no_kb / unresolved / not_found：一律不装作有数据。
                # 先让模型把话说得像人（带上当前行程的上下文），模型不在或
                # 输出不可信再回模板——模板是底线，不该是唯一的面孔。
                smart = llm.unknown_spot_reply(
                    self.client, city, spot,
                    has_plan=bool(self.plan_made and self.last_plan))
                if smart:
                    return smart
                return (f"「{spot}」暂时查不到——{city}的景点数据我这边也没有"
                        f"联网检索通道，不想编。\n可以说个我收录的城市，比如"
                        f"{intent._SERVICEABLE_HINT}。")
        return intent.spot_answer(city, spot, items, note=note,
                                  client=self.client)

    def _sync_plan_pace(self, pace: str) -> None:
        """就地改节奏时，把新档写回已有方案。

        概要的「节奏」、之后每轮的质检与「别太赶」判断都读 plan["slots"]；
        不写回去，用户点了「轻松一点」，方案抬头还写着「适中」。
        """
        ps = (self.last_plan or {}).get("slots")
        if isinstance(ps, dict) and pace:
            ps["pace"] = pace

    def _overcap_days(self, pace: str) -> list[tuple[int, int, list[dict]]]:
        """按质检口径找超额天：返回 [(天号, 上限, 当天景点节点列表)]。"""
        cap = composer.PACE_CAP.get(pace, 3)
        out = []
        for d in (self.last_plan or {}).get("days_plan") or []:
            atts = [n for n in d.get("nodes") or [] if n.get("type") == "attraction"]
            if len(atts) > cap:
                out.append((d.get("day"), cap, atts))
        return out

    def _trim_overcap(self, pace: str, label: str = "别太赶") -> tuple[str, list[str]] | None:
        """按新节奏就地删超额站（「别太赶」的实际动作，节奏没变或刚放松都走这里）。

        只删用户没有点名必去的景点（从当天最后一个往前删，保住锚点）；
        **删掉就是删掉，不挪到别的天**——把装不下的点塞进返程日，看着每天都不超，
        实际一个点没少（杭州实测：雷峰塔从 Day 2 挪到返程日）。
        删完同步当日预算、门票明细与总额——不能一边删景点一边留虚高的账。
        超额的全是必去点时返回 None：那是用户的需求装不下，得他本人取舍。
        """
        plan = self.last_plan
        if not plan:
            return None
        cap = composer.PACE_CAP.get(pace, 3)
        must_names = ((plan.get("slots") or self.slots or {}).get("must_visit")
                      or [])
        party = (plan.get("slots") or self.slots or {}).get("party_size") or 1
        removed: list[str] = []
        ticket_cut = 0.0
        for d in plan.get("days_plan") or []:
            atts = [n for n in d.get("nodes") or [] if n.get("type") == "attraction"]
            excess = len(atts) - cap
            if excess <= 0:
                continue
            removable = [n for n in atts
                         if not any(composer.name_hit(m, n.get("name") or "")
                                    for m in must_names)]
            for n in reversed(removable):
                if excess <= 0:
                    break
                d["nodes"].remove(n)
                removed.append(f"Day {d.get('day')} {n.get('name')}")
                ticket_cut += float(n.get("cost") or 0)
                excess -= 1
        if not removed:
            return None
        if ticket_cut:
            b = plan.get("budget") or {}
            for it in b.get("breakdown") or []:
                if it.get("category") == "ticket":
                    it["amount"] = round((it.get("amount") or 0)
                                         - ticket_cut * party, 2)
            if b.get("total") is not None:
                b["total"] = round(b["total"] - ticket_cut * party, 2)
            for d in plan.get("days_plan") or []:
                cost = sum(n.get("cost") or 0 for n in d["nodes"]
                           if n.get("type") == "attraction")
                d["day_budget"] = cost * party + 150 * party
        pn = {"relaxed": "轻松", "moderate": "适中", "intensive": "紧凑"}.get(pace, pace)
        plan["assumptions"] = (plan.get("assumptions") or []) + [
            f"按「{label}」删掉超额景点：{'、'.join(removed)}"
            f"（{pn}节奏每天最多 {cap} 个景点，删掉的不再挪到别的天）"]
        note = (f"> 已按「{label}」处理：删掉超额景点 {'、'.join(removed)}"
                f"——{pn}节奏每天最多 {cap} 个景点，删掉的不再挪到别的天。\n\n")
        return note, removed

    def _pace_noop_answer(self, pace: str, just_loosened: bool = False) -> str:
        """没有可删的点时的回答。

        三种情况分开说，不能笼统一句"已经最松了"：
        - 行程真超额（质检口径）且超额的全是用户点名的必去点——如实摊牌
          "必去点装不下"，给加一天/取消必去的取舍（杭州实测：质检报
          "3 个景点 > 上限 2"，回复却说"没有继续往下压的空间"）。
        - 这轮刚把节奏放松、但行程本来就在新上限内——说清"切了档、没删点"，
          别让用户以为我们背着他重排了一遍。
        - 行程本来没超额——说清"没得再松"，并给出真能生效的选项。
        """
        pn = {"relaxed": "轻松", "moderate": "适中", "intensive": "紧凑"}.get(pace, pace)
        loosenest = pace == "relaxed"
        plan = self.last_plan or {}
        days_plan = plan.get("days_plan") or []
        over = self._overcap_days(pace)
        heavy = None
        for d in days_plan:
            k = len([x for x in d.get("nodes") or [] if x.get("type") == "attraction"])
            if heavy is None or k > heavy[1]:
                heavy = (d.get("day"), k, [x.get("name") for x in d.get("nodes") or []
                                          if x.get("type") == "attraction"])
        total_days = len(days_plan) or (plan.get("slots") or {}).get("days")

        lines = []
        if over:
            day_no, cap, atts = over[0]
            must_names = ((plan.get("slots") or {}).get("must_visit")
                          or (self.slots or {}).get("must_visit") or [])
            must_in = [n.get("name") for n in atts
                       if any(composer.name_hit(m, n.get("name") or "")
                              for m in must_names)]
            names = "、".join(x.get("name") for x in atts)
            lines.append(
                (f"已切到「{pn}」节奏" if just_loosened else f"现在已经是「{pn}」节奏")
                + f"，可 Day {day_no} 还是排了 {len(atts)} 个景点"
                f"（{names}），超过每天 {cap} 个的上限——超出的"
                + (f"（{'、'.join(must_in)}）是你点名的必去点，我不能替你删。"
                   if must_in else "这轮我没动手，删哪些由你定。"))
            lines.append("真要把它松下来：")
            lines.append(f"- 加一天：说「多玩一天」，Day {day_no} 就装得下了")
            if must_in:
                lines.append(f"- 取消一个必去点：说「去掉{'、'.join(must_in[:1])}」，我马上删")
            lines.append("- 换住宿位置：住到景点更集中的片区，每天路上的时间能省一大截")
            lines.append("选一条告诉我，我按你的选择重排；不选我就保留现在这版。")
            return "\n".join(lines)

        if just_loosened:
            cap = composer.PACE_CAP.get(pace, 3)
            k = heavy[1] if heavy else 0
            head = (f"已把节奏切到「{pn}」（每天最多 {cap} 个景点）。"
                    f"现在这版每天最多 {k} 个景点，本来就在上限内，"
                    f"所以这次没删点、也没重排——原来的时间和动线都保留。")
            lines = [head]
        else:
            head = (f"现在已经是「{pn}」节奏，是最松的一档（每天最多 2 个景点），"
                    f"景点密度没有继续往下压的空间了。" if loosenest else
                    f"这就是「{pn}」节奏下的排法。")
            lines = [head]
            if loosenest:
                lines[0] += "这一轮我没有改行程——改也是同样的结果。"
            else:
                lines[0] += "说「轻松一点」我就把节奏降一档重排。"

        moves = []
        if heavy and heavy[1] >= 2:
            names = "、".join(heavy[2])
            moves.append(f"删掉一站：Day {heavy[0]} 现在有 {heavy[1]} 个景点（{names}），"
                         f"说「Day {heavy[0]} 去掉一个」我立刻重排")
        if total_days and isinstance(total_days, int) and total_days > 2:
            moves.append(f"少排一天：{total_days} 天改 {total_days - 1} 天，"
                         f"剩下的每天还能再空出一段")
        moves.append("换住宿位置：住到景点更集中的片区，每天路上的时间能省一大截")
        if moves:
            lines.append("真要更空，这几步确实有效：")
            lines.extend(f"- {m}" for m in moves)
            lines.append("选一条告诉我，我按你的选择重排；不选我就保留现在这版。")
        return "\n".join(lines)

    def _plan_qa_answer(self, utterance: str) -> str:
        """回答「总结一下这个行程」「这个行程怎么样」：事实来自最近一次方案。"""
        r = self.last_plan or {}
        s = r.get("slots") or {}
        days_plan = r.get("days_plan") or []
        budget = r.get("budget") or {}
        facts = {
            "目的地": s.get("destination"),
            "出发地": s.get("origin") or "未指定",
            "出行日期": s.get("date_range"),
            "天数": s.get("days"),
            "人数": (f"{s.get('party_size')} 人（{s.get('party_detail')}）"
                     if s.get("party_detail") else s.get("party_size")),
            "预算总额元": budget.get("total"),
            "每天安排": [{"第几天": d.get("day"),
                        "内容": [n.get("name") for n in d.get("nodes") or []]}
                       for d in days_plan],
            "需要提前预约": sorted({n.get("name") for d in days_plan
                              for n in d.get("nodes") or []
                              if n.get("booking_required")}),
            "备注假设": (r.get("assumptions") or [])[:4],
        }
        said = llm.compose_reply(self.client, "plan_qa", facts, utterance,
                                 must_keep=[x for x in (s.get("destination"),)
                                            if x])
        if said:
            return said
        lines = [f"这份行程概括：{s.get('destination')} {s.get('days')} 天"]
        if s.get("origin"):
            lines[0] += f"（从{s['origin']}出发）"
        for d in days_plan:
            names = " → ".join(n.get("name") for n in d.get("nodes") or [])
            lines.append(f"- 第 {d.get('day')} 天：{names}")
        if budget.get("total"):
            lines.append(f"- 预算合计约 {budget['total']} 元")
        return "\n".join(lines)

    def _food_knowledge_answer(self, city: str, utterance: str) -> str:
        """美食知识答：「有什么特色美食/火锅有什么特色」——菜品文化，不查商家。

        数据来自 .kb.db 的 food_knowledge 表（与真实餐厅 POI 分表存放）；
        问的品类库里有就答，没有转联网检索并标注来源；都拿不到才如实说没收录。
        """
        from . import kb as kb_mod
        rows = None
        db = kb_mod.get_kb()
        if db is not None:
            try:
                rows = db.list_food(city)
            except Exception:
                rows = None
        kw = intent.food_keyword(utterance, city)
        if rows and kw:
            hit = [r for r in rows if kw in r["name"] or kw in r["category"]
                   or kw in r["intro"] or kw in r["features"]]
            rows = hit or None          # 问的品类知识库没有 → 转联网
        if not rows:
            said = self._general_answer(f"{city}特色美食{kw or ''}".strip())
            if said:
                return said
            return (f"{city}的美食知识我还没收录，不敢瞎编菜品故事。\n"
                    f"想找具体馆子可以说「{city}有哪些好吃的火锅店」，"
                    f"我按库里的真实餐厅答。")
        facts = {"城市": city,
                 "问的": f"{city}特色美食" + (f"·{kw}" if kw else ""),
                 "菜品": [{"名称": r["name"],
                          "类别": r["category"] or "美食",
                          "介绍": r["intro"],
                          "特色": r["features"]} for r in rows[:6]]}
        said = intent._speak(self.client, "poi", facts,
                             utterance, [r["name"] for r in rows[:2]])
        if said:
            return said
        lines = [f"{city}的特色美食，给你介绍几个代表："]
        for r in rows[:6]:
            cat = r["category"] or "美食"
            lines.append(f"- **{r['name']}**（{cat}）：{r['intro']} {r['features']}")
        return "\n".join(lines)

    def _restaurant_poi_answer(self, city: str, utterance: str) -> str:
        """餐厅搜索答：「火锅店/川菜馆/春熙路附近的馆子」——真实商家 POI。

        带品类词（火锅/川菜）和片区词（春熙路）就按条件筛；品类词不在店名里
        （「川菜」未必写在招牌上）自动放宽再试；城市没数据先触发在线补库。
        """
        kw = intent.food_keyword(utterance, city)
        near = intent.food_area(utterance)
        args: dict = {"city": city, "category": "restaurant", "limit": 6}
        if kw:
            args["keyword"] = kw
        if near:
            args["near"] = near
        r = agent.dispatch("search_poi", args)
        items = (r.get("items") or []) if r.get("ok") else []
        if not items and city not in self._rest_fetched:
            # 库里没有餐厅：可能是整座城市没收录——按餐饮标签定向补库再答
            # （ensure_city 看景点 covered 就短路，补不了餐厅）
            res = autofetch.ensure_city_data(city, "restaurant")
            self._rest_fetched[city] = res
            if res.get("status") == "fetched":
                r = agent.dispatch("search_poi", args)
                items = (r.get("items") or []) if r.get("ok") else []
        if not items and kw:
            r2 = agent.dispatch("search_poi", {k: v for k, v in args.items()
                                               if k != "keyword"})
            items = (r2.get("items") or []) if r2.get("ok") else []
        label = (f"{near}·{kw}" if (near and kw) else
                 (kw or (f"{near}附近的餐厅" if near else "特色餐厅")))
        return intent.recommend_answer(city, label, items, client=self.client)

    def _stay_answer(self, city: str, utterance: str) -> str:
        """「有什么酒店/住哪」：按明晚入住、当前人数查在售酒店作答。

        日期没定就按明晚起价口径（facts 里写明），不拿这个当排行程信号——
        这句是问答，不是新规划需求。
        库里没酒店先走一次在线补库（ensure_hotels 只看酒店表，城市"有景点
        没酒店"时 ensure_city 会短路）；补到就报参考价并注明来源，补不到
        才如实说没收录。
        """
        din = (self.today + timedelta(days=1)).isoformat()
        dout = (self.today + timedelta(days=2)).isoformat()
        guests = int(self.slots.get("party_size") or 2)
        args = {"city": city, "check_in": din, "check_out": dout,
                "guests": guests}
        hotels, note = self._hotels_with_fetch(city, args)
        return intent.stay_answer(city, hotels, client=self.client, note=note)

    def _hotels_with_fetch(self, city: str,
                           args: dict) -> tuple[list[dict], str]:
        """查在售酒店；空了先在线补库一次（每城一次）。

        返回（酒店列表, 补录说明）——说明非空表示数据是刚抓的，只能报参考价。
        """
        r = agent.dispatch("search_hotel", args)
        hotels = (r.get("items") or []) if r.get("ok") else []
        note = ""
        if not hotels and city not in self._hotel_fetched:
            # 在线补库：只按住宿标签抓真实门店，抓不到不硬编
            res = autofetch.ensure_city_data(city, "hotel")
            self._hotel_fetched[city] = res
            if res.get("status") == "fetched":
                r = agent.dispatch("search_hotel", args)
                hotels = (r.get("items") or []) if r.get("ok") else []
                if hotels:
                    note = (f"{city}原先没有酒店数据，已按在线地图补录 "
                            f"{res.get('hotels', 0)} 家；房价为星级估算的参考价，"
                            f"未验证实时房态，预订前请以预订平台实时价格与房态为准")
        return hotels, note

    def _general_answer(self, utterance: str) -> str | None:
        """通用旅行问题：先联网检索，再让模型组织；没模型就渲染带来源的摘要列表。

        检索词：模型改写 > websearch.keywords() 启发式。整句自然语言直接
        丢给搜索引擎会被稀释成前两个字，这一步决定检索质量。
        返回 None 表示连联网也没查到且没有模型可用——交回原流程，
        宁可被追问一句，也不编一段像模像样的假答案。
        """
        q = websearch.keywords(utterance)
        if self.client is not None:
            better = llm.rewrite_query(self.client, utterance)
            if better:
                q = better
        res = websearch.search(q)
        results = res.get("results") or []
        facts = {"问题": utterance,
                 "检索状态": res.get("note") or "",
                 "检索结果": [{"标题": x.get("title"), "摘要": x.get("snippet"),
                             "来源": x.get("site")} for x in results]}
        said = llm.compose_reply(self.client, "general", facts, utterance)
        if said:
            sources = list(dict.fromkeys(
                x.get("site") for x in results if x.get("site")))[:4]
            if sources:
                said += "\n\n> 信息来源：" + "、".join(sources) + "（联网检索）"
            return said
        if not results:
            return None
        lines = ["我在网上查了一下，摘几条供参考："]
        for x in results[:4]:
            lines.append(f"- **{x.get('title')}**：{x.get('snippet')}"
                         f"（{x.get('site')}）")
        lines.append("\n以上是网页摘要；涉及价格、政策、时刻的，请以官方渠道为准。")
        return "\n".join(lines)

    def _nearby_qa(self, city: str, spot: str) -> str:
        """「XX周边/附近有什么」：以点名景点为圆心，报出库里最近的几个景点。"""
        r = agent.dispatch("search_poi", {"city": city, "keyword": spot,
                                          "limit": 1})
        base = (r.get("items") or [None])[0] if r.get("ok") else None
        if not (base and base.get("geo")):
            # 点名没查到坐标：按普通景点问答处理（在线补资料的那套都在那边）
            return self._spot_qa(city, spot)
        # 展示名用库里的全名：城市剥前缀后 spot 可能只剩「古城」半个词
        spot_disp = base.get("name") or spot
        lat, lon = base["geo"][0], base["geo"][1]
        allp = agent.dispatch("search_poi", {"city": city, "limit": 60})
        items = (allp.get("items") or []) if allp.get("ok") else []
        near = []
        for p in items:
            if not p.get("geo") or p.get("category") != "attraction":
                continue
            pname = p.get("name") or ""
            if pname == spot_disp or pname in spot or spot in pname:
                continue          # 中心点自己不算"周边"
            km = haversine_km((lat, lon), p["geo"])
            if km <= 20:
                near.append({**p, "_km": round(km, 1)})
        near.sort(key=lambda p: p["_km"])
        if not near:
            return (f"「{spot_disp}」周边 20 公里内的景点数据我收录得还不够，不敢瞎推荐。\n"
                    f"可以问「{city}有什么好玩的」，或者直接让我排一份{city}的行程。")
        return intent.nearby_answer(city, spot_disp, near[:5], client=self.client)

    def _coverage(self, dest: str) -> bool:
        """目的地是否有可排数据；没有就先在线查一次并入库（每城一次）。

        旧行为是查到 NO_POI 就直接回"我这边没有数据"，把用户推走。真实产品不该这样：
        城市是公开信息，在线有 OSM 这类数据源，查一次存下来，下次（包括别的用户）
        直接就有的用。抓不到才退回原来的"没有数据"追问——绝不静默排空行程。
        """
        if dest in self._cov:
            return self._cov[dest]
        ok = bool(agent.dispatch("search_poi", {"city": dest}).get("ok"))
        if not ok and len(self.fetched) < self.max_autofetch:
            res = autofetch.ensure_city_data(dest, "attraction",
                                             client=self.client)
            self.fetched[dest] = res
            # 以入库后重新查询为准：酒店有数据但景点为空的城市仍算"缺"
            ok = bool(agent.dispatch("search_poi", {"city": dest}).get("ok"))
        self._cov[dest] = ok
        return ok

    def _fetch_note(self, dest: str) -> str:
        """把"这城市的数据是刚补的"如实写进方案，而不是让用户以为是资料库原有数据。"""
        res = self.fetched.get(dest) or {}
        if res.get("status") != "fetched":
            return ""
        if res.get("source") == "llm":
            return (f"「{dest}」原先不在我的资料库，在线数据源这次没连上，改由模型整理了 "
                    f"{res.get('pois', 0)} 个景点入库；坐标为近似值、门票为参考价，"
                    f"住宿按城市均价估算，出行前请再核对一下营业时间与票价")
        return (f"「{dest}」原先不在我的资料库，已在线检索 {res.get('pois', 0)} 个景点、"
                f"{res.get('hotels', 0)} 家住宿入库；门票、房价与游玩时长按类别估算，"
                f"到店/购票以实际为准，也可以让我按你的偏好重排")

    def _vague(self, f: str, text: str) -> bool:
        """"还没定/看情况/随便"也算回答，只是模糊——再问一遍就是骚扰。"""
        t = text or ""
        return any(k in t for k in _VAGUE.get(f, ()))

    @staticmethod
    def _is_backquestion(text: str) -> bool:
        """这轮是在反问我（带问号或"…吗/呢"结尾），而不是在回答上一个问题。"""
        t = (text or "").strip()
        if not t or len(t) > 40:
            return False
        return bool(re.search(r"[？?]|[吗呢]\s*[。.]?$", t))

    def _next_ask(self, missing: list[str]) -> str | None:
        """下一个该问的字段：按 ASK_RULES 顺序，跳过用户说过"还没定"的、
        以及已经问满 MAX_ASKS_PER_FIELD 次的。"""
        if sum(self.field_asks.values()) >= MAX_CLARIFY_ROUNDS:
            return None
        for f in missing:
            if f == "destination" or f in self.vague_fields:
                continue
            if self.field_asks.get(f, 0) >= MAX_ASKS_PER_FIELD:
                continue
            return f
        return None

    def _backq_prefix(self, backq: str) -> str:
        """用户反问时先正面回一句，别装没看见。"""
        if not backq:
            return ""
        if re.search(r"出发|从哪|从哪儿|哪里走|哪个城市", backq):
            return _BACKQ_PREFIX["origin"]
        if re.search(r"预算|多少钱|花费", backq):
            return _BACKQ_PREFIX["budget"]
        return _BACKQ_PREFIX["default"]

    def _dropped_note(self, exclude: str = "") -> str:
        """已经放弃追问、准备按假设处理的字段，用一句话交代清楚。"""
        notes = []
        for f, why_ask in (("date_range", "时间"), ("days", "天数")):
            if f == exclude or self._has(f):
                continue
            if f in self.vague_fields:
                notes.append(f"{why_ask}你说还没定，我先按常见的算，随时能改")
            elif self.field_asks.get(f, 0) >= MAX_ASKS_PER_FIELD:
                notes.append(f"{why_ask}先按常见的算，定了告诉我")
        return ("；".join(notes) + "。") if notes else ""

    def _compose_question(self, asked: list[str], attempt: int = 1,
                          backq: str = "") -> str:
        # 不用「排行程还差 N 件事」的清单句式——那读起来像表单，不像在聊天
        label = {"destination": "想去哪儿", "date_range": "什么时候出发",
                 "days": "打算玩几天", "party_size": "几个人出行"}
        hint = {f: h for f, _, h in ASK_RULES}
        f = asked[0] if asked else ""
        seq = ASK_TEXT.get(f) or [("再补充一点信息我就开始排？", "")]
        q, h = seq[min(max(attempt, 1), len(seq)) - 1]
        if "{cities}" in h:
            h = h.format(cities="、".join(serviceable_cities()))
        tpl = f"{q}\n（{h}）" if h else q
        pre = self._backq_prefix(backq)
        if pre:
            tpl = f"{pre}\n{tpl}"
        # 前一件问过没答、已经决定按假设处理时，明说一句再问下一件——
        # 否则用户会觉得"我问的时间它怎么不问了"，像漏了一件事。
        dropped = self._dropped_note(exclude=f)
        if dropped:
            tpl = f"{dropped}\n{tpl}"

        # 配了模型就让它把这一问说得像人话；模型不可用时用上面的模板
        said = self._say(client=self.client, kind="clarify", facts={
            "要问的": [{"字段": label.get(f, f), "给用户的建议": hint.get(f, ""),
                        "这是第几次问": attempt}],
            "这次只问这一件": True,
            "用户刚才的话": backq or "（没有回答上一个问题）",
            "需要先回应的话": pre,
            "已经改成假设的": dropped or "（无）",
            # 只放用户真的说过的字段：party_size 默认 1，传过去模型会说成
            # 「你一个人去」——用户从没这么说，等于凭空安了个同行人。
            "已经知道的": {k: v for k, v in self.slots.items()
                          if k in ("destination", "origin", "days", "date_range",
                                   "budget_total") and v},
            "能排具体行程的城市": serviceable_cities(),
        })
        if not said:
            return tpl
        return said if said.rstrip().endswith(("？", "?", "。")) else said.rstrip() + "？"

    @staticmethod
    def _say(client, kind: str, facts: dict, must_keep: list | None = None) -> str | None:
        """模型不可用/输出跑偏时统一返回 None，调用方回退模板。QuotaError 会向上抛。"""
        if client is None:
            return None
        from . import llm as llm_layer
        return llm_layer.compose_reply(client, kind, facts, must_keep=must_keep)

    def _fill_assumptions(self, missing: list[str]) -> tuple[dict, list[str]]:
        """缺口用合理假设补齐，每一条都可解释。"""
        slots = dict(self.slots)
        notes: list[str] = []
        for f in missing:
            if f == "days":
                slots["days"] = 4
                if f in self.vague_fields:
                    notes.append("天数你说还没定，先按 4 天排，给个数字我立刻改")
                else:
                    notes.append("天数没问到，按周边游常见的 4 天排")
            elif f == "party_size":
                slots["party_size"] = slots.get("party_size") or 1
                notes.append(f"人数没问清，先按 {slots['party_size']} 人算房型与人均；"
                             f"说个数字我立刻改")
            # date_range 不在这里补：plan_trip 里的 resolve_date_range 会折算并
            # 生成自己的说明，两边都补会出现两条意思重复的假设。
        # 目的地绝不在这里猜：上游已保证有城市且库里有数据。
        # 旧代码用 `serviceable_cities()[0]` 兜底，等于用户问北京天气、
        # 系统却给他排了一份上海行程，比拒绝服务更糟。
        # 出发地同样不在这里兜底（2026-11-02 决策）：没说就是没说，
        # plan_trip 会照排行程但不含大交通，并在假设里说明——
        # 旧版悄悄按杭州算，用户从成都走也收到「杭州-大理」的班次。
        return slots, notes

    def _budget_tiers(self, result: dict) -> dict:
        """提示词第 48 行：缺预算不追问，给三档让用户选。"""
        total = result["budget"]["total"]
        n = result["slots"]["party_size"] or 1
        # 一日往返没有住宿，"住宿降档"这种说法在这里是假的
        if (result["slots"].get("days") or 1) == 1:
            notes = ("打车改地铁公交、少吃一顿正餐",
                     "当前这版方案，含 10% 机动（当天往返，不含住宿）",
                     "打车为主、正餐换口碑餐厅、加一个付费景点")
        else:
            notes = ("住宿降到经济型、少吃一顿正餐、只保留 1 个付费景点",
                     "当前这版方案，含 10% 机动",
                     "住宿升一档、增加包车、正餐换成口碑餐厅")
        return {
            "经济": {"total": round(total * 0.75), "per_person": round(total * 0.75 / n),
                     "note": notes[0]},
            "舒适": {"total": round(total), "per_person": round(total / n),
                     "note": notes[1]},
            "品质": {"total": round(total * 1.35), "per_person": round(total * 1.35 / n),
                     "note": notes[2]},
        }

    @staticmethod
    def _render_tiers(tiers: dict) -> str:
        out = ["### 你没说预算，先给三档参考（选一档我按它调）", "",
               "| 档位 | 合计 | 人均 | 怎么做到 |", "|---|---|---|---|"]
        for name, t in tiers.items():
            out.append(f"| {name} | {t['total']} 元 | {t['per_person']} 元 | {t['note']} |")
        return "\n".join(out)


# ------------------------------------------------------------------ 偏好记忆


class PreferenceMemory:
    """跨会话的用户偏好。写本地 JSON，进程重启仍有效。

    与 `mock_tools.save_preference` 的区别：那个是工具层的内存字典（供 Agent 调用），
    这个是编排层直接读写的持久化档案。真实产品里两者会合并成一个带用户 ID 的存储。
    """

    def __init__(self, path):
        import json
        import pathlib
        self.path = pathlib.Path(path)
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        import json
        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except OSError:
            # 只读环境（打成 .pyz 运行）落盘失败：本次进程内仍然生效
            pass

    def apply_to(self, slots: dict) -> dict:
        """把记住的硬约束并进新一轮的槽位。"""
        prefs = self.get("hard_prefs") or []
        if prefs:
            slots = dict(slots)
            slots["hard_prefs"] = sorted(set(slots.get("hard_prefs") or []) | set(prefs))
        return slots
