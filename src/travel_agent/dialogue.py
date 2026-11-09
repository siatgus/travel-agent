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
from datetime import date

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
    if delta.get("soft_prefs"):
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
        """
        # 城市判定顺序：本句里的城市 > 当前行程目的地 > 最近提到的城市。
        # 「天安门广场周边有什么」一句里未必带"北京"，但行程本身在北京。
        city = (intent.city_in(utterance) or self.slots.get("destination")
                or self.last_city or self.trip_dest)
        if kind == "weather":
            if not city:
                return intent.weather_answer("", utterance)
            self.last_city = city
            first = self.weather_rounds == 0
            self.weather_rounds += 1
            return intent.weather_answer(city, utterance, with_hint=first,
                                         client=self.client)
        if kind == "poi":
            if not city:
                return intent.poi_answer("")
            self.last_city = city
            # 「上海迪士尼门票多少钱」是问具体景点，不是问「上海有什么好玩的」。
            # 先按名字查库；库里没有就联网/问模型补一条入库再答——而不是把
            # 具体点名当成泛推荐，回一串不相干的热门景点。
            spot = intent.spot_mention(utterance, city)
            if spot:
                if re.search(r"周边|附近|周围", utterance):
                    return self._nearby_qa(city, spot)
                return self._spot_qa(city, spot)
            return intent.poi_answer(city, client=self.client)
        if kind == "transport":
            origin, dest = agent._extract_route(utterance)
            origin = origin or self.slots.get("origin")
            dest = dest or self.slots.get("destination") or None
            if dest:
                self.last_city = dest
            return intent.transport_answer(utterance, origin, dest,
                                           client=self.client)
        if kind == "smalltalk":
            return intent.smalltalk_answer(utterance, client=self.client)
        if kind == "plan_qa":
            # 没出过方案就不拦：这句可能是在闲聊，交回排行程流程正常处理
            if not (self.plan_made and self.last_plan):
                return None
            return self._plan_qa_answer(utterance)
        if kind == "general":
            return self._general_answer(utterance)
        return None

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

    def _turn_impl(self, utterance: str,
                   history_text: str | None = None) -> Reply:
        self.turn_no += 1
        self.history.append({"role": "user",
                             "content": history_text or utterance})

        # ---- 先判断意图：问天气/景点/班次/闲聊时直接答，不进排行程流水线 ----
        # 之前所有输入都被当成「补槽位」，于是问天气会被回「排行程还差 2 件事」，
        # 问到第三轮还会硬凑出一份别的城市的行程。
        kind = intent.classify(utterance)
        # 规则认不出（other）但句子像个提问时，让模型兜底分类一次。
        # 「故宫需要预约吗」这类问法词表覆盖不全，靠这一层接住；
        # 分类失败/说 none 就照旧走排行程流水线。
        if kind == "other" and self.client is not None \
                and intent.looks_like_question(utterance):
            alt = llm.classify_intent(self.client, utterance)
            if alt in ("weather", "poi", "transport", "plan_qa", "general"):
                kind = alt
        if kind in ("weather", "poi", "transport", "smalltalk",
                    "plan_qa", "general"):
            text = self._answer(kind, utterance)
            if text is not None:
                self.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=self.turn_no,
                             ask_rounds=self.ask_rounds, slots=dict(self.slots),
                             render=text)

        # 「把去程到达时间改为 09:00」——交通时刻修改请求：班次时刻来自
        # 真实查询，不能凭空改数字；这类指令必须拦在槽位抽取之前，否则
        # 目的地兜底正则会把「达时间改为」吃成城市/景点，污染整份方案
        # （用户实测：进了必去景点、假设和行前准备）。有无方案都拦。
        if intent.is_transport_time_change(utterance):
            text = (
                "这条先没动行程：**去程/返程的时刻来自真实班次查询，"
                "我不能直接改数字**——改了就不是真实存在的班次了。\n\n"
                "想赶更早或更晚的班次，可以：\n"
                "1. 直接问班次（如「上海到成都的高铁班次」），我看到合适的"
                "真实班次后按它重排行程；\n"
                "2. 告诉我换出发日期或出发地，我重新查班次再排。\n\n"
                "如果你其实是想排行程或改行程，直接说需求（去哪儿、玩几天），"
                "班次时刻我来查。")
            self.history.append({"role": "agent", "content": text})
            return Reply(mode="answer", turn_no=self.turn_no,
                         ask_rounds=self.ask_rounds, slots=dict(self.slots),
                         render=text)

        # 「把 Day 3 午餐排到 15:20 结束」——分钟级锚点改排：整体平移目标日
        # 时间轴（composer.apply_anchor），不动景点时长与交通。放在意图直答
        # 之后：天气/交通/景点问答里出现时刻不归这管。解析不出具体锚点
        # （有时刻没目标节点）仍走诚实提示，绝不静默重排装没听见。
        if self.plan_made and intent.wants_clock_schedule(utterance):
            anchor = intent.parse_clock_anchor(utterance)
            if not anchor:
                text = (
                    "想按具体时刻（比如 15:20）改时间轴，得说清楚是**哪一餐/午休**：\n\n"
                    "可以这样说：「Day 3 午餐排到 15:20 结束」「每天晚餐 7 点开始」"
                    "「把午休改成 2 小时」。")
            else:
                changed, detail, new_days = composer.apply_anchor(
                    (self.last_plan or {}).get("days_plan") or [], anchor)
                if not changed:
                    text = f"这条没动时间轴：{detail}。"
                else:
                    # 写回 last_plan：渲染、存档、页面刷新都走既有链路；
                    # 之后换/加景点会重新编排，锚点是即时的，需要再说一次
                    self.last_plan = dict(self.last_plan, days_plan=new_days)
                    text = (f"> 已按你的时刻要求调整：{detail}。\n\n"
                            "> 注意：之后再换/删/加景点或改天数会重新编排时间轴，"
                            "这条时刻要求如还需生效请再说一次。\n\n"
                            + agent.render(self.last_plan))
            self.history.append({"role": "agent", "content": text})
            mode = "plan" if (anchor and changed) else "answer"
            return Reply(mode=mode, turn_no=self.turn_no,
                         ask_rounds=self.ask_rounds, slots=dict(self.slots),
                         plan=self.last_plan if mode == "plan" else None,
                         render=text)

        delta = agent.extract_slots(utterance, today=self.today, client=self.client)
        explicit = _explicit_fields(utterance, delta)
        lead_notes: list[str] = []
        # 这轮改动前的槽位：用来判断"用户要的调整是不是本来就已经满足"
        prev_slots = dict(self.slots or {})

        # 「加上都江堰，安排一天」不是"整趟 1 天"——是给新加的景点留一整天；
        # 「多住一天 / 延长两天 / 加一天」是顺延，不是"整趟 N 天"。
        # 规则层已挡一轮（agent.extract_slots），这里兜住 LLM 抽出来的结果。
        _dm = re.search(r"(安排|排|留|住|延长|加|多玩)\s*([一二两三四五六七八九十\d]+)\s*天",
                        utterance)
        if _dm and self.plan_made:
            _n = agent.cn_to_int(_dm.group(2)) or 1
            _verb = _dm.group(1)
            _is_ext = (_verb in ("延长", "加", "多玩")
                       or bool(re.search(r"多住|多玩|多待|多加|延长", utterance)))
            if _is_ext:
                _new = (prev_slots.get("days") or 1) + _n
                if (delta.get("days") or 0) != _new or delta.get("days_extend"):
                    delta = dict(delta)
                    delta["days"] = _new
                    delta.pop("days_extend", None)
                    explicit.add("days")
                    lead_notes.append(f"按「{_dm.group(0)}」把行程延长到 {_new} 天")
            elif "days" in explicit and (prev_slots.get("days") or 0) > (delta.get("days") or 0):
                delta = dict(delta)
                delta["days"] = None
                delta.pop("days_extend", None)
                explicit.discard("days")
                lead_notes.append(
                    f"「{_dm.group(0)}」按给新加的景点留出整天理解，"
                    f"行程仍是 {prev_slots.get('days')} 天")

        # 上一轮问过「X 在哪个城市」，这一轮用户答的城市要把 X 带上。
        # 少了这一步，"迪士尼在哪个城市？"→"上海" 会把迪士尼本身弄丢，
        # 用户看到的是一份跟迪士尼毫无关系的上海行程。
        if self.pending_spot and "destination" in explicit:
            delta = dict(delta)
            delta["must_visit"] = sorted(set(
                (delta.get("must_visit") or []) + [self.pending_spot]))
            explicit.add("must_visit")
            self.pending_must.append(self.pending_spot)
            lead_notes.append(f"把「{self.pending_spot}」加进{delta.get('destination')}的行程")
            self.pending_spot = None

        # ---- 景点被当成了城市：「安排去迪士尼的行程」 ----
        # 抽取器会把「去迪士尼」里的迪士尼写进 destination。可迪士尼不是城市，
        # 库里当然没有"迪士尼"的景点和酒店数据，于是系统一本正经地回
        # 「我这边没有迪士尼的数据…要不要按你刚才聊过的上海排？」——
        # 用户只是想往已排好的行程里加一站，却被告知排不了，还差点把上一份行程弄丢。
        # 正确的做法：认得出它是景点，就归到它所在的城市，按"必去"加进现有行程。
        d_dest = delta.get("destination")
        if (d_dest and "destination" in explicit and not agent.is_city(d_dest)
                and not agent.is_region(d_dest)):
            hit = agent.spot_city(d_dest, prefer=self.slots.get("destination"))
            cur = self.slots.get("destination")
            if hit or cur:
                delta = dict(delta)
                delta["destination"] = None
                explicit.discard("destination")
                spot_name = d_dest
                city = (hit[0] if hit else cur) or ""
                if hit:
                    # 用户只说核心词，库里有全名就用全名（迪士尼 → 上海迪士尼乐园）：
                    # 编排器按名字互相包含匹配，全名更容易命中候选池
                    spot_name = d_dest if len(d_dest) >= len(hit[1]) else hit[1]
                if city and city != cur:
                    # 景点在别的城市（大理的行程 + 迪士尼 → 上海）：这是新的一趟
                    delta["destination"] = city
                    explicit.add("destination")
                    lead_notes.append(
                        f"「{d_dest}」是景点不是城市，按它在的{city}排")
                else:
                    lead_notes.append(
                        f"「{d_dest}」是景点不是城市，已加进{city}这份行程")
                delta["must_visit"] = sorted(set(
                    (delta.get("must_visit") or []) + [spot_name]))
                explicit.add("must_visit")
                self.pending_must.append(spot_name)

        # 只说了个景点名（"迪士尼"）：已经排好行程时，按"再加一站"理解。
        # 否则它会走到"没有目的地"的分支，反问你"你想去哪儿"——刚排好的行程像丢了。
        _bare = utterance.strip().strip("。！!？?")
        if (not explicit and self.plan_made and self.slots.get("destination")
                and 2 <= len(_bare) <= 10):
            _hit = agent.spot_city(_bare, prefer=self.slots.get("destination"))
            if _hit and _hit[0] == self.slots.get("destination"):
                delta = dict(delta)
                delta["must_visit"] = [(_bare if len(_bare) >= len(_hit[1]) else _hit[1])]
                explicit.add("must_visit")
                self.pending_must.append(delta["must_visit"][0])
                lead_notes.append(f"把「{_bare}」加进刚才那份行程")

        # 「一定要去喜洲古镇」：景点名不是城市。已有有效目的地时，本轮点名/
        # 排除清单里的片段不该把目的地顶掉——它是 must_visit/avoid，
        # 不是 destination。否则大理的行程会被"喜洲古镇没有数据"打断。
        # 但「想去云南玩」是换目的地（带玩/旅游字眼的目的地级陈述），
        # 该拦的还是不拦——守卫只作用于纯景点点名。
        d_dest = delta.get("destination")
        if (d_dest and self.slots.get("destination")
                and "destination" in explicit
                and not re.search(r"玩|旅游|度假|旅行", utterance)
                and d_dest not in serviceable_cities()
                and d_dest in ((delta.get("must_visit") or [])
                               + (delta.get("avoid") or []))):
            delta = dict(delta)
            delta["destination"] = None
            explicit.discard("destination")

        # 撤销排除的口语说法：「…了，还是加回来吧」「那个豫园还是安排上吧」。
        # 这类句子里景点名在动词前面，规则正则未必抓得到（也可能只抓到虚词），
        # 但句子里明明白白出现了之前被排除的那个景点——按名字共现捞回来，
        # 否则用户反复说"加回来"都只会得到一句"已按你的要求避开：豫园"。
        if self.slots.get("avoid") and _restore_intent(utterance):
            back = [a for a in self.slots["avoid"] if a and a in utterance]
            if back:
                delta = dict(delta)
                delta["must_visit"] = sorted(set((delta.get("must_visit") or []) + back))
                # 模型有时会把"加回来"的景点同时写进两个数组，这里以本轮改口为准
                delta["avoid"] = [a for a in (delta.get("avoid") or [])
                                  if not any(name_hit(a, b) for b in back)]
                explicit.add("must_visit")

        if self.slots:
            self.slots, self.total_is_derived = merge_slots(
                self.slots, delta, explicit, self.total_is_derived)
        else:
            self.slots = delta
            # 总额是"人均×人数"派生出来的，还是用户直接说的？
            # 派生值要在人数变化时跟着重算，用户明说的总额则不能动。
            self.total_is_derived = (bool(delta.get("budget_per_person"))
                                     and "budget_total" not in explicit)
            self.slots["_source"] = delta.get("_source", "rules")

        # 跨会话记住的硬约束（不吃辣等）每轮并入；之前 Session 收了 memory
        # 却从未使用，--memory 参数实际是摆设
        if self.memory is not None:
            self.slots = self.memory.apply_to(self.slots)

        # 人数：用户明说才算"有了"。party_size 默认 1，不看这句话会把
        # "没说几个人" 当成 "说了 1 个人"，直接出一份单人预算的行程。
        if "party_size" in explicit or (self.slots.get("party_size") or 1) != 1:
            self.party_stated = True

        dest = self.slots.get("destination")
        # 换了目的地 = 用户要的是另一趟旅行：追问额度、模糊标记都该重来。
        # 旧实现里 field_asks 只增不减，一次会话里排过一份行程之后，
        # 后面每次说新城市都直接出方案——天数、日期、人数全是猜的。
        prev_trip = self.trip_dest
        if dest and self.trip_dest and dest != self.trip_dest:
            self.field_asks.clear()
            self.asked_fields.clear()
            self.vague_fields.clear()
            self.ask_rounds = 0
            self.vague_hits = 0
        if dest:
            self.trip_dest = dest

        missing = [f for f, _, _ in ASK_RULES if not self._has(f)]
        # 先判"是不是城市/景点"，再谈覆盖：景点名（迪士尼/外滩）不该被送去
        # ensure_city 假装补一座同名城市；但没收录的真实城市（样例城这种）
        # 仍要走自动补全，不能被误拦。
        city_like = bool(dest) and agent.is_city(dest)
        needs_city = bool(dest) and not city_like and agent.looks_like_spot(dest)
        if dest and not needs_city:
            needs_city = not self._coverage(dest)
        # 记住最近有效的目的地：降级时"接着你刚才聊的城市排"，而不是按字典序随便挑
        if dest and not needs_city:
            self.last_good_dest = dest
        if dest:
            self.last_city = dest

        # 用户明说"还没定/看情况/随便"时，把那件事标记成模糊：不再追问，直接假设。
        # 「过几天」「看情况」本身就是一个答案，追着问第二遍只会让人烦。
        # 但"还没定"是针对某一件的："随便/你看着办"才是整体甩手——两者不能混。
        # 「随便」当回答用时只解决被问的那一件：问"什么时候出发"回"随便"，
        # 不代表天数和人数也随便——否则一次回答就把后面所有问题都吞掉，
        # 系统随后直接出一份天数、人数全靠猜的行程（用户投诉的正是这个）。
        if any(k in utterance for k in _GLOBAL_VAGUE):
            pend = self.pending_field
            self.vague_hits += 1
            # 第一次「你看着办」只当回答被问的那一件；连说两次就是真甩手了，
            # 把所有缺口一次性按常用情况假设，不再一件一件问下去。
            if (self.vague_hits >= 2
                    or pend not in ("date_range", "days", "party_size")
                    or self._has(pend)):
                self.vague_fields |= {f for f in ("date_range", "days", "party_size")
                                      if not self._has(f)}
            else:
                self.vague_fields.add(pend)
        elif explicit:
            self.vague_hits = 0
        else:
            if (self.pending_field in ("date_range", "days")
                    and not self._has(self.pending_field)
                    and self._vague(self.pending_field, utterance)):
                self.vague_fields.add(self.pending_field)
            if any(k in utterance for k in _TIME_TOPIC) and self._vague("date_range", utterance):
                self.vague_fields.add("date_range")
            if any(k in utterance for k in _DAYS_TOPIC) and self._vague("days", utterance):
                self.vague_fields.add("days")

        # 这轮是不是在反问我（"你不问我从哪出发吗"）——那不是回答，得先回应再问回去
        backq = utterance if (not explicit and self._is_backquestion(utterance)) else ""

        # 追问策略（第二次改）：
        # 目的地是硬门槛，没有它绝不排方案（旧逻辑会拿字典序第一个城市顶上，
        # 于是连问三次北京天气就被排去了上海）。
        # 时间/天数是一次只问一件：问过没答就换个说法再问，问满 2 次仍空着
        # 就按常见情况假设并出方案——绝不因为"问过了"就当"答过了"。
        question = asked = None
        if not dest:
            asked = ["destination"]
            if self.last_city:
                question = (f"你是想排「{self.last_city}」的行程吗？确认一下我就开始。"
                            f"\n如果只是想知道那边天气，直接问我，我查给你看。")
            else:
                question = ("你想去哪儿？给我城市名就能开始排。"
                            "\n（只想先了解情况也可以问：某地天气、城际班次、城市景点）")
        elif needs_city:
            asked = ["destination"]
            cities = "、".join(serviceable_cities())
            res = self.fetched.get(dest) or {}
            why = {
                "failed": "在线数据源没连上",
                "empty": "在线也没检索到可用景点",
                "unresolved": "这个地名没能定位到坐标",
                "disabled": "在线检索功能关着",
            }.get(res.get("status", ""), "")
            why = f"（我刚试着在线补了一次：{why}）" if why else ""
            if not city_like and not agent.is_region(dest):
                # 不是城市，也不是省：景点/商圈/写错的地名。别把"没有景点数据"
                # 当成结论——它连城市都不是，问一句它在哪个城市更有用。
                # 记住这个名字：用户下一句答的城市要把它一起安排。
                self.pending_spot = dest
                question = (f"「{dest}」听着不像城市名，我这边是按城市排行程的。"
                            f"\n它在哪个城市？说一句「{dest}在XX」或者直接给我城市名，"
                            f"我把它安排进去。")
            elif "destination" in self.asked_fields:
                # 已经说过一次没有数据了，别复读同一句：能定位就给天气，定位不了
                # （省份、拼错的地名）就直接把可服务城市摊开，别绕弯
                weather = (intent.weather_answer(dest, "未来三天", client=self.client)
                           if real_tools.resolve_geo(dest) else "")
                if weather and "没找到" not in weather:
                    question = weather
                else:
                    question = (f"「{dest}」我这边确实定位不到、也排不了行程。{why}"
                                f"\n能排出具体行程的是 {cities}；"
                                f"说上面任意一个城市名，我马上开始。")
            else:
                alt = (f"要不要按你刚才聊过的「{self.last_good_dest}」排？"
                       if self.last_good_dest and self.last_good_dest != dest else "")
                question = (f"「{dest}」我这边没有景点和酒店数据，硬排会给你一份空行程，所以先不排。"
                            f"{why}"
                            f"\n{alt}能排出具体行程的是 {cities}；"
                            f"{dest}的天气和班次我倒是可以查。")
        else:
            # 已经出过方案时，"重新排一下"是改现有行程，不是从零开始：
            # 已确认的目的地/天数/日期都还在，不该当缺口重问一遍。
            replan = bool(_REPLAN_RE.search(utterance)) and self.plan_made
            if replan and self.last_plan_desc:
                lead_notes.append(f"接着刚才那份「{self.last_plan_desc}」重排")
            # 改的是同一趟行程（加/减景点、重新排）：已确认过的软信息不再重问。
            # 换了目的地则算另一趟，该问的照问（人数不能从上趟沿用）。
            same_trip = bool(self.plan_made and dest and dest == prev_trip)
            field = (None if dest and (replan or same_trip)
                     else self._next_ask(missing))
            if field:
                attempt = self.field_asks.get(field, 0) + 1
                asked = [field]
                question = self._compose_question(asked, attempt=attempt, backq=backq)
        # 其余情况（城市有了、软信息问满或用户说过"还没定"）→ 带假设出方案

        if question is not None:
            self.ask_rounds += 1
            self.asked_fields.update(asked)
            for f in asked:
                if f == "destination":
                    continue        # 目的地是硬门槛，不占软问题的追问预算
                self.field_asks[f] = self.field_asks.get(f, 0) + 1
            self.pending_field = asked[0] if asked else None
            self.history.append({"role": "agent", "content": question})
            return Reply(mode="clarify", turn_no=self.turn_no,
                         ask_rounds=self.ask_rounds, question=question,
                         asked=asked, slots=dict(self.slots),
                         render=question)
        self.pending_field = None

        # 出方案：缺口用假设补齐并标注
        slots, assumptions = self._fill_assumptions(missing)
        self.slots = slots

        # 「别太赶」而节奏本来就在最松的一档：重排出来必然一模一样。
        # 这里**不许**再跑一遍 plan_trip（白等 50 秒），更不许说"这一版只把
        # 时间与动线重排了一遍"——行程和预算一个字都没变，那是在骗人。
        # 但"没得再松"≠"没事可做"：行程真超额（质检口径）时要有实际动作——
        # 删得动的（非必去点）直接删；全是必去点装不下就如实摊牌让用户取舍。
        # （杭州实测：质检报"3 个景点 > 上限 2"，点别太赶却回"已经最松"）
        if "pace" in explicit and self.plan_made:
            old_pace, new_pace = prev_slots.get("pace"), slots.get("pace")
            if new_pace == old_pace:
                self.slots = slots
                trimmed = self._trim_overcap(new_pace)
                if trimmed is not None:
                    note, _removed = trimmed
                    text = note + agent.render(self.last_plan)
                    self.history.append({"role": "agent", "content": text})
                    return Reply(mode="plan", turn_no=self.turn_no,
                                 ask_rounds=self.ask_rounds,
                                 slots=dict(self.slots), plan=self.last_plan,
                                 render=text)
                text = self._pace_noop_answer(new_pace)
                self.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=self.turn_no,
                             ask_rounds=self.ask_rounds, slots=dict(self.slots),
                             render=text)

        trace: list = []
        self._phase("正在核对目的地资料，准备编排…")
        result = agent.plan_trip(slots, trace, client=self.client, today=self.today,
                                 on_phase=self._phase)
        # 把折算后的槽位写回会话：plan_trip 内部会把"下个月"折算成具体日期，
        # 不回写的话后续轮次拿到的还是 month-only，且 Reply.slots 与方案不一致。
        self.slots = result["slots"]
        slots = result["slots"]
        assumptions = result.get("assumptions", []) + assumptions

        # 位置是估算的点（未收录景点）必须在方案抬头就说明，不能只写在当天卡片里：
        # 用户看到"都江堰"以为交通时间都算过了，实际这一站根本没算路。
        _uncertain = sorted({n["name"] for d in result["days_plan"]
                             for n in d["nodes"]
                             if "位置待确认" in (n.get("transit") or "")})
        if _uncertain:
            assumptions.append(
                "、".join(_uncertain) + " 的位置是估算的（资料库没收录确切坐标），"
                "这一站没有计入交通时间；如果它在市郊或跨城，单程可能要 1~2 小时，"
                "告诉我我按跨城一日游留足往返时间")

        # 兴趣到底进没进行程：用户说「大熊猫 / 喝茶 / 老街」，行程里一个都没有
        # 就等于没听见。能对上的已经排进去了；一个都对不上就如实说明，
        # 而不是让这份"热门景点拼盘"冒充个性化行程。
        # 对上也要分强弱：「喝茶」排到人民公园是靠"公园"沾边，不是真的茶馆，
        # 这份差别必须说出来，否则用户永远不知道这条偏好到底满足了没有。
        _prefs = slots.get("soft_prefs") or []
        if _prefs:
            items = (result.get("pois") or {}).get("items") or []
            planned = {n.get("name") for d in result["days_plan"]
                       for n in d["nodes"]}
            landed: dict[str, str] = {}
            for p in _prefs:
                lv = ""
                for it in items:
                    if it.get("name") not in planned:
                        continue
                    m = composer.interest_match(p, it)
                    if m and (m[0] == "strong" or not lv):
                        lv = m[0]
                    if lv == "strong":
                        break
                if lv:
                    landed[p] = lv
            hit = list(landed)
            weak = [p for p, lv in landed.items() if lv == "weak"]
            if weak:
                assumptions.append(
                    "兴趣「" + "、".join(weak) + "」在"
                    + str(slots.get("destination") or "当地")
                    + "的资料里没有直接对应的景点，只按相近的点就近安排了"
                      "（行程里标为「近似匹配」）；想要专门的安排，说个具体名字我加进去")
            missed = [p for p in _prefs if p not in hit]
            if missed:
                # 分清两种"没安排"：库里压根没有对应景点，还是有名额但没排进去。
                # 混成一句"没有对应景点"会让用户以为系统不认识大熊猫。
                exists = [p for p in missed
                          if any(composer.interest_hit(p, it) for it in items)]
                if exists:
                    assumptions.append(
                        "你的兴趣「" + "、".join(exists) + "」对应的景点这版没排进去"
                        "（天数/节奏装不下），加一天或删掉一个别的我就补上")
                rest = [p for p in missed if p not in exists]
                if rest:
                    assumptions.append(
                        "你的兴趣「" + "、".join(rest) + "」在"
                        + str(slots.get("destination") or "当地")
                        + "的资料里没有直接对应的景点，这版只能先按当地热门点排；"
                          "说个具体名字（比如某家茶馆、某条老街），我加进去")

        # 行程修改的可见反馈：用户点名/排除之后必须明说"改了什么"。
        # 否则重排出来的方案看起来和上一份一样（"不想去豫园"像被无视），
        # 必去点排没排进去用户也要自己去行程里翻。
        if (explicit & {"must_visit", "avoid"}) or self.pending_must:
            rep = result.get("must_report") or {}
            placed = rep.get("placed") or []
            missed = rep.get("missed") or []
            unknown = rep.get("unresolved") or []
            if placed:
                assumptions.append("已确保安排：" + "、".join(placed))
            if rep.get("fetched"):
                assumptions.append("这次为你的点名补了资料："
                                   + "、".join(rep["fetched"]))
            if rep.get("custom"):
                assumptions.append(
                    "按你说的名称安排：" + "、".join(rep["custom"])
                    + "（资料库没收录、在线也没查到，位置是按市区估算的，"
                      "所以这一站不给距离和交通时间；如果它其实在市区外或跨城，"
                      "单程可能 1~2 小时，告诉我我按一日游重排，"
                      "开放时间和门票也请在出发前确认）")
            if unknown:
                # 和"排不下"分开说：库里压根没有这个景点，加天数也变不出来
                assumptions.append(
                    "没查到 " + "、".join(unknown) + " 的可用资料（资料库和在线源都没有），"
                    "所以没排进行程；写全名或告诉我它所在的城市/区，我再补一次")
            if missed:
                assumptions.append("点名景点本轮未能排入：" + "、".join(missed)
                                   + "（受闭馆时间与动线限制，可让我加天数或换日期再试）")
            if slots.get("avoid"):
                assumptions.append("已按你的要求避开：" + "、".join(slots["avoid"]))

        # 无障碍诉求（婴儿车/少台阶/轮椅）：不管有没有点名必去点都要说清——
        # 库里没有无障碍数据，"已确保安排"这句话对它不成立，必须给出
        # 取舍通道（杭州实测：概要标了「需无障碍」却没有任何落地说明）
        if "需无障碍" in (slots.get("hard_prefs") or []):
            assumptions.append(
                "无障碍需求（婴儿车/少台阶）我记下了，但景点资料里没有"
                "无障碍设施数据，**我没法确保每个点都满足**——台阶/坡道"
                "情况请以现场为准。对台阶特别敏感的话直接点名"
                "（比如「XX 台阶多，换一个」），我换平缓的景点重排")

        tiers = None
        if not slots.get("budget_total"):
            tiers = self._budget_tiers(result)

        text = agent.render(result)
        if tiers:
            text += "\n\n" + self._render_tiers(tiers)

        # 先说清"这轮相对上一份改了什么/沿用了什么"，再给方案。
        # 用户说"加个迪士尼"时，最怕的是系统装没听见上一份行程、重新给一份陌生的。
        if lead_notes:
            text = "> " + "；".join(dict.fromkeys(lead_notes)) + "。\n\n" + text

        # 记下这次出过方案：后续"重新排/再加一个"都按改现有行程处理
        self.plan_made = True
        self.last_plan = result
        self.last_plan_desc = f"{slots.get('destination')} {slots.get('days')} 天"
        self.pending_spot = None
        self.pending_must = []
        if assumptions:
            # assumptions = 方案自带假设 + 这里补的假设，是完整清单。
            # 直接整行替换「> 假设：」行。旧的"按；切分再补差集"会翻车：
            # 假设文案本身含「；」（如「不含大交通；定了告诉我」）会被切成
            # 两段，整句匹配不上，于是同一条假设被追加第二遍。
            line = "> 假设：" + "；".join(assumptions)
            if re.search(r"^> 假设：", text, re.M):
                text = re.sub(r"^> 假设：.*$", lambda _: line, text, count=1, flags=re.M)
            else:
                text = text.replace("\n", "\n" + line + "\n", 1)

        # 数据来源：这次联网补的数据要写明是补的、哪些是估算，
        # 不然用户会以为是资料库里的既有事实（门票/房价最容易被当真）。
        note = self._fetch_note(slots.get("destination") or "")
        if note:
            text += f"\n\n> 数据说明：{note}"

        self.history.append({"role": "agent", "content": text})
        return Reply(mode="plan", turn_no=self.turn_no, ask_rounds=self.ask_rounds,
                     slots=dict(slots), plan=result, assumptions=assumptions,
                     budget_tiers=tiers, trace=trace, render=text,
                     fetched=dict(self.fetched))

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

    def _spot_qa(self, city: str, spot: str) -> str:
        """回答「XX景点怎么样/门票多少钱」：库里查，查不到就在线补，再答不了如实说。"""
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
                # disabled / no_kb / unresolved / not_found：一律不装作有数据
                return (f"「{spot}」暂时查不到——{city}的景点数据我这边也没有"
                        f"联网检索通道，不想编。\n可以说个我收录的城市，比如"
                        f"{intent._SERVICEABLE_HINT}。")
        return intent.spot_answer(city, spot, items, note=note,
                                  client=self.client)

    def _overcap_days(self, pace: str) -> list[tuple[int, int, list[dict]]]:
        """按质检口径找超额天：返回 [(天号, 上限, 当天景点节点列表)]。"""
        cap = composer.PACE_CAP.get(pace, 3)
        out = []
        for d in (self.last_plan or {}).get("days_plan") or []:
            atts = [n for n in d.get("nodes") or [] if n.get("type") == "attraction"]
            if len(atts) > cap:
                out.append((d.get("day"), cap, atts))
        return out

    def _trim_overcap(self, pace: str) -> tuple[str, list[str]] | None:
        """节奏没变但行程超额时，「别太赶」的实际动作：删超额站。

        只删用户没有点名必去的景点（从当天最后一个往前删，保住锚点）；
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
            f"按「别太赶」删掉超额景点：{'、'.join(removed)}"
            f"（{pn}节奏每天最多 {cap} 个景点）"]
        note = (f"> 已按「别太赶」处理：删掉超额景点 {'、'.join(removed)}"
                f"——{pn}节奏每天最多 {cap} 个景点。\n\n")
        return note, removed

    def _pace_noop_answer(self, pace: str) -> str:
        """节奏没变（通常已是最松档）时的回答。

        两种情况分开说，不能笼统一句"已经最松了"：
        - 行程真超额（质检口径）且超额的全是用户点名的必去点——如实摊牌
          "必去点装不下"，给加一天/取消必去的取舍（杭州实测：质检报
          "3 个景点 > 上限 2"，回复却说"没有继续往下压的空间"）。
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
                f"现在已经是「{pn}」节奏，可 Day {day_no} 还是排了 {len(atts)} 个景点"
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
            res = autofetch.ensure_city(dest, client=self.client)
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
        return {
            "经济": {"total": round(total * 0.75), "per_person": round(total * 0.75 / n),
                     "note": "住宿降到经济型、少吃一顿正餐、只保留 1 个付费景点"},
            "舒适": {"total": round(total), "per_person": round(total / n),
                     "note": "当前这版方案，含 10% 机动"},
            "品质": {"total": round(total * 1.35), "per_person": round(total * 1.35 / n),
                     "note": "住宿升一档、增加包车、正餐换成口碑餐厅"},
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
