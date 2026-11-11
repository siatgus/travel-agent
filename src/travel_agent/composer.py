"""混合编排器 —— 规则生成候选、评分择优、几何校验。

设计动机：纯规则编排只会"按区域顺序取前 N 个"，无法解释"为什么去这个不去那个"；
纯 LLM 编排则会排出地理上不可行的路线（上午城东、下午城西）。
这里把职责切开：

    候选生成（规则：可玩时长、开放日、天气可用性）
        ↓
    打分排序（可解释的加权分数，权重在 WEIGHTS 里可直接调）
        ↓
    贪心成团（距离约束：相邻节点直线距离 ≤ max_hop，超了换天）
        ↓
    时间轴装配（真实距离 → 交通方式与耗时 → 冲突自动后移）
        ↓
    LLM 重排（可选钩子：在 top-k 内按用户偏好微调，见 rerank_hook）

所有决策都返回 reasons 字段，便于在 demo 里 --explain 打印，也便于评测断言。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

from .tools import haversine_km, transit_estimate

WEIGHTS = {
    "appeal": 2.0,          # 景点本身吸引力 1-5
    "must_visit": 4.0,      # 用户点名必去
    "interest_match": 1.5,  # 与软偏好关键词重合
    "intensity_penalty": -2.5,
    "high_altitude_penalty": -6.0,
    "rain_penalty": -3.5,
    "far_penalty": -0.15,   # 每公里
    # LLM 重排的名次加成上限，决定"模型能翻转多大的分差"。
    # 实测标定：3.0 能翻转同分与接近分（索道与洱海同为 10 分时，模型头名生效），
    # 但翻不过 1 个吸引力档位（2.0 分）——这是故意的：分差代表真实质量/成本差异，
    # 模型的偏好猜测不该推翻它。用户"一定要去某地"走 must_visit（必进），不是走这里。
    "rerank_bonus": 3.0,
}

PACE_CAP = {"relaxed": 2, "moderate": 3, "intensive": 4}
MAX_HOP_KM = 15.0          # 与 prompts/04 子提示词 B 规则 2 对齐
DAY_MOVE_BUDGET_MIN = 150  # 单日市内移动上限 2.5h
# 景点最晚结束时刻。没有这条约束时会排出"21:05 逛完古城、21:28 吃晚饭"的行程——
# 营业时间上它合法（古城全天开放），但真实体验不成立。
DAY_END_LATEST = 20 * 60
# 返程日：发车前必须留出「去车站 + 取票安检」的时间。车站远近未知，按
# 90 分钟保守估——18:00 的车，最晚 16:30 收尾。没有这条线时会排出
# "17:03 逛完灵隐寺、18:03 才吃完午饭赶 18:00 的车"的时间表（杭州实测）。
STATION_BUFFER_MIN = 90
# 单日景点净时长上限（不含交通与用餐）。8 小时是"一天逛到闭园"的真实上限；
# 没有这条线时 240 分钟的远郊点会和两个市区点凑一天，晚餐排到 19:48（成都实测）。
DAY_SIGHT_CAP_MIN = 8 * 60
# 晚餐最晚开始时刻。DAY_END_LATEST 只管景点收尾，不管吃饭：景点 19:48 结束、
# 晚餐 19:48 开始，实际吃完已过九点。晚于这条线说明当天排太满。
DINNER_START_LATEST = 19 * 60 + 30

# 兴趣词的同义扩展，**分两档**：
#
# strong（具体线索）：命中了就说明这个点确实满足该兴趣——"喝茶"撞上
#   "茶社/盖碗/茶馆"，"老街"撞上"巷"。这类点有资格占当天的锚点。
# broad（泛线索）：只是沾边——"喝茶"撞上"公园"（公园里通常有茶座，但
#   望江楼公园也符合），"爬山"撞上"山"。这类只算弱命中，永远不许抢锚点。
#
# 不分档的后果是真实踩过的：「喝茶」的同义词里原本有「公园」，于是望江楼公园
# 也算命中，把用户真正想看的大熊猫挤出了行程；而行程里只写"人民公园"三个字，
# 用户根本看不出这是"喝茶"落了地，还是随便塞了个公园。
INTEREST_SYNONYMS: dict[str, tuple[str, ...]] = {
    "大熊猫": ("熊猫", "繁育研究基地"),
    "熊猫": ("大熊猫", "繁育研究基地"),
    "喝茶": ("茶馆", "茶社", "茶楼", "茶室", "茶座", "盖碗", "茶"),
    "茶": ("茶馆", "茶社", "茶楼", "茶室", "盖碗", "喝茶"),
    "老街": ("巷", "古街", "古镇", "老城", "老街"),
    "古镇": ("古城", "老街", "镇"),
    "美食": ("小吃", "餐厅", "火锅", "food"),
    "小吃": ("美食", "餐厅", "火锅"),
    "博物馆": ("博物", "展览", "遗址"),
    "夜景": ("夜游", "灯光", "江边", "河畔"),
    "爬山": ("登山", "索道", "山"),
    "公园": ("园林",),
    "寺庙": ("寺", "庙", "宫", "观"),
}

INTEREST_BROAD: dict[str, tuple[str, ...]] = {
    "喝茶": ("公园", "园林"),
    "茶": ("公园",),
    "老街": ("街", "古建"),
    "古镇": ("古村",),
}


def _poi_hay(poi: dict) -> str:
    """名字/区域/备注/标签拼成的检索串。"""
    return (" ".join(str(poi.get(k) or "") for k in ("name", "area", "note"))
            + " " + " ".join(poi.get("tags") or []))


def _geo_uncertain(poi: dict) -> bool:
    """这个点的坐标是不是"按市区估算"的（未收录景点兜底）。

    autofetch 的最后兜底会把用户点名的景点按城市中心落点，凑合排进行程。
    这种坐标不能拿去算距离：都江堰会被算成"离武侯祠 8.81 公里、打车 16 分钟"，
    而它其实在另一个市。位置不可靠就不给精确路线。
    """
    tags = poi.get("tags") or []
    return ("用户点名" in tags or "位置估算" in tags
            or "位置按市区估算" in (poi.get("note") or ""))


def interest_match(interest: str, poi: dict) -> tuple[str, str] | None:
    """兴趣怎么命中的：返回 ("strong"|"weak", 命中的那个词)；没命中返回 None。

    行程里必须能说清楚"这条兴趣是靠什么落地的"。只返回布尔值的话，用户看到
    「人民公园」永远分不清是"喝茶"真的排上了，还是公园这个词沾了边。
    """
    i = (interest or "").strip()
    if len(i) < 2:
        return None
    hay = _poi_hay(poi)
    if i in hay:
        return ("strong", i)
    for w in INTEREST_SYNONYMS.get(i, ()):
        if w and w in hay:
            return ("strong", w)
    for w in INTEREST_BROAD.get(i, ()):
        if w and w in hay:
            return ("weak", w)
    return None


def interest_note(hits: list[dict], poi: dict) -> str:
    """把兴趣命中翻成一句人能读懂的备注（写进日程备注列）。

    strong：「兴趣「喝茶」（命中"茶社"）：鹤鸣茶社，盖碗茶」——把库里的
            具体线索一起带出来，这就是"喝茶"落成的具体活动；
    weak  ：「兴趣「喝茶」为近似匹配（公园）…」——直说只是沾边，
            不许让用户误以为专门安排了喝茶这一项。
    """
    if not hits:
        return ""
    parts = []
    raw = (poi.get("note") or "").strip()
    # "模型整理，坐标为近似值，门票为参考价" 这类运维备注对用户没有信息量
    useful = bool(raw) and len(raw) <= 30 and not any(
        k in raw for k in ("模型整理", "近似值", "参考价", "请自行确认", "估算"))
    for h in hits:
        if h["level"] == "strong":
            tail = f"：{raw}" if useful else f"（命中「{h['via']}」）"
            parts.append(f"兴趣「{h['word']}」{tail}")
        else:
            parts.append(f"兴趣「{h['word']}」为近似匹配（命中「{h['via']}」），"
                         f"不是专门为它安排的活动")
    return "；".join(parts)


def interest_hit(interest: str, poi: dict, strong_only: bool = False) -> bool:
    """兴趣词是否命中这个点。

    strong：兴趣原词或具体线索直接对上（大熊猫 → 大熊猫基地；喝茶 → 鹤鸣茶社）；
    weak  ：只靠泛线索沾边（喝茶 → 人民公园，因为公园里通常有茶座）。
    名额紧张时只有 strong 才有资格当"兴趣锚点"——否则「喝茶」的泛线索
    「公园」会让望江楼公园顶掉用户真正想看的大熊猫。
    """
    m = interest_match(interest, poi)
    if m is None:
        return False
    return m[0] == "strong" if strong_only else True


def _core_name(name: str) -> str:
    """去掉通用后缀后的核心词：「成都大熊猫繁育研究基地」→「成都大熊猫繁育研究」。"""
    n = (name or "").strip()
    for suf in ("繁育研究基地", "研究基地", "基地", "风景区", "景区", "乐园",
                "古镇", "古城", "公园", "博物馆", "纪念馆", "寺", "庙"):
        if n.endswith(suf) and len(n) > len(suf) + 1:
            return n[:-len(suf)]
    return n


def name_hit(fragment: str, poi_name: str) -> bool:
    """用户点名的景点与 POI 名是否指同一个地方：双向包含。

    只做单向子串会漏掉两种常见说法：用户说「上海迪士尼」而库里叫
    「上海迪士尼乐园」（fragment in name，本来就能中）；用户照抄全名
    「上海迪士尼乐园」而库里叫「迪士尼乐园」（name in fragment，单向会漏）。
    「不想去豫园」这类排除若漏匹配， unwanted 景点照进行程，比漏掉必去更招投诉。

    第三档：去掉「基地/景区/乐园」这类通用后缀后再比核心词。用户说
    「大熊猫基地」、库里叫「成都大熊猫繁育研究基地」，两者互不为子串，
    于是命中了库里另一条没有坐标的兜底记录（「大熊猫基地·位置估算」），
    真实景点被当成位置待确认白占一天。核心词只在长度 ≥3 且**不从前缀**
    命中时生效：「人民公园」「望江楼公园」不会因为都带「公园」算一处，
    同一个城市名开头的「测试城海洋馆」「测试城古城」也不会被认成一个点。
    """
    f, n = (fragment or "").strip(), (poi_name or "").strip()
    if len(f) < 2 or len(n) < 2:
        return False
    if f in n or n in f:
        return True
    cf, cn = _core_name(f), _core_name(n)
    if len(cf) < 3 or len(cn) < 3:
        return False
    # 前缀相同只说明"同一个城市/品牌下的不同景点"，不算同一处
    return (cf in cn and not cn.startswith(cf)) or (cn in cf and not cf.startswith(cn))


def score_poi(poi: dict, slots: dict, rain_prob: int, last_geo: list[float] | None,
              weights: dict | None = None,
              pref_rank: dict[str, int] | None = None) -> tuple[float, list[str]]:
    """给单个 POI 打分，同时返回人类可读的理由。

    pref_rank：LLM 重排得到的 poi_id → 名次。只作为小幅加成参与打分，
    不改变 veto（硬约束）和几何约束——模型再喜欢一个点，也进不了关门的景区。
    """
    w = weights or WEIGHTS
    s = poi.get("appeal", 3) * w["appeal"]
    reasons = [f"吸引力 {poi.get('appeal', 3)}"]

    must = [m for m in slots.get("must_visit") or [] if name_hit(m, poi["name"])]
    if must:
        s += w["must_visit"]
        reasons.append("用户点名必去")

    prefs = slots.get("soft_prefs") or []
    hit_words = [w for w in prefs if interest_hit(w, poi)]
    if hit_words:
        s += w["interest_match"] * len(hit_words)
        reasons.append(f"命中偏好 {'/'.join(hit_words)}")

    # 位置估算的点（未收录景点）：坐标是城市中心兜底值，不能当距离用
    uncertain = _geo_uncertain(poi)
    if uncertain:
        reasons.append("位置待确认（估算坐标，不计交通）")

    if slots.get("pace") == "relaxed" and poi["intensity"] == "high":
        s += w["intensity_penalty"]
        reasons.append("偏累，与轻松节奏冲突")
    if slots.get("pace") == "intensive" and poi["intensity"] == "low":
        s += w["intensity_penalty"] / 2
        reasons.append("强度偏低")

    if "忌高海拔" in (slots.get("hard_prefs") or []) and "高海拔" in poi.get("tags", []):
        s += w["high_altitude_penalty"]
        reasons.append("用户忌高海拔")

    if "带幼儿" in (slots.get("hard_prefs") or []) and (
            poi["intensity"] == "high" or poi["duration_min"] >= 240):
        s += w["high_altitude_penalty"]
        reasons.append("带孩子出行，强度/时长不适")

    rain_prob = rain_prob or 0
    if rain_prob > 60 and poi["intensity"] != "low":
        s += w["rain_penalty"]
        reasons.append(f"降雨概率 {rain_prob}%")

    if last_geo and poi.get("geo") and not uncertain:
        km = haversine_km(last_geo, poi["geo"])
        s += w["far_penalty"] * km
        if km > MAX_HOP_KM:
            s -= 5
            reasons.append(f"距上一站 {km}km，超出单日半径")

    rank = (pref_rank or {}).get(poi.get("poi_id"))
    if rank is not None:
        # 加成随名次平方衰减：第 1 名拿满额，第 2 名 56%（span=4），末位 0。
        # 早期用线性衰减时，第 3 名还能拿 50%，导致头名的微弱优势被抹平，
        # 重排永远翻不动 1 个吸引力档位（2.0 分）的差距。
        span = max(len(pref_rank or {}) - 1, 1)
        decay = max(0.0, 1 - rank / span) ** 2
        s += w["rerank_bonus"] * decay
        reasons.append(f"偏好重排第 {rank + 1} 位")

    return round(s, 2), reasons


def _must_start(poi: dict) -> int:
    """必须开始的最晚时刻（分钟）。闭馆早且耗时长者更紧迫。"""
    hours = parse_hours(poi.get("open_hours", ""))
    return hours[1] - poi["duration_min"] if hours else 24 * 60


def veto(poi: dict, slots: dict) -> str | None:
    """硬约束否决：不参与打分，直接不进候选。返回否决理由，None 表示通过。"""
    hp = slots.get("hard_prefs") or []
    if "忌高海拔" in hp and "高海拔" in poi.get("tags", []):
        return "用户忌高海拔"
    if "带幼儿" in hp and poi["intensity"] == "high":
        return "带孩子出行，不宜高强度"
    # 用户明确排除的景点是最高优先级硬约束——"我不想豫园"之后豫园还出现在
    # 行程里，用户只会认为系统没在听。排除比必去更优先：两者冲突时不去。
    av = slots.get("avoid") or []
    if any(name_hit(a, poi["name"]) for a in av if a):
        return "用户明确不想去"
    return None


def _open_on(poi: dict, d: date) -> bool:
    """闭馆日判断：博物馆类默认周一闭馆。"""
    if "博物馆" in poi["name"] or "博物院" in poi["name"]:
        return d.weekday() != 0
    return True


def _with_kid(slots: dict) -> bool:
    """这趟带没带幼儿：偏好标签、人数构成、孩子年龄任一命中就算。"""
    return ("带幼儿" in (slots.get("hard_prefs") or [])
            or (slots.get("children") or 0) > 0
            or slots.get("child_age") is not None)


# 推车通行情况库里有数据的不多，按景点类型的常识给口径，措辞必须留"以现场为准"
_KID_ROUGH = ("古镇", "古城", "老街", "巷", "山", "梯", "坡", "洞", "长城")
_KID_SMOOTH = ("博物馆", "馆", "动物园", "海洋", "乐园", "公园", "园区")


def _kid_tip(name: str) -> str:
    if any(k in name for k in _KID_ROUGH):
        return "带娃提示：石板路/台阶可能多，推车不便，建议用背带"
    if any(k in name for k in _KID_SMOOTH):
        return "带娃提示：园区道路一般平整可推车（台阶情况以现场为准）"
    return "带娃提示：路面情况未核实，推车通行以现场为准"


def _insert_kid_nap(nodes: list[dict], clock: int, day_idx: int, days: int,
                    with_kid: bool, label: str | None = None) -> int:
    """带幼儿或用户点名午休：午饭后写死 90 分钟午休，返回新 clock。

    "带幼儿"不能只是概要里的一个标签——幼儿下午没有这一觉，晚饭前必崩。
    用户明确说"每天午休 90 分钟"（需午休）即使没带孩子也照插，只是措辞
    不提幼儿。截止 16:00：午餐晚收尾（景点拖堂）也照插，只有 16 点后
    才不再后移——再晚 90 分钟睡醒就撞晚饭，硬偏好不能变成摆设
    （亲子实测：Day3 午餐结束过 15 点，午休被自由活动顶替）。
    抵达/返程日行程本来就轻，不插。
    """
    if not with_kid or day_idx in (0, days - 1) or clock >= 16 * 60:
        return clock
    nodes.append(_node(fmt(clock), label or "午休（带幼儿，回酒店小睡）", "rest",
                       90, "—",
                       "幼儿作息排进时间轴：午饭后睡 90 分钟再出发，"
                       "下午的安排已整体顺延；离酒店远就改车里睡/找母婴室歇脚"
                       if label is None else
                       "按你的要求排进时间轴：午饭后歇 90 分钟再出发，"
                       "下午的安排已整体顺延"))
    return clock + 90


# ------------------------------------------------------------------ 分钟级锚点改排

# 平移边界：出发再早不早于 07:30（赶不上开馆还打乱早餐），最晚收摊 23:00
_ANCHOR_START_EARLIEST = 7 * 60 + 30
_ANCHOR_END_LATEST = 23 * 60


def _find_meal_node(nodes: list[dict], target: str) -> dict | None:
    """按 type+时段窗口找餐节点——餐节点的 name 是餐厅名不是「午餐」。

    午休节点名以「午休」开头（rest 类型），直接按名字找。
    """
    if target == "午休":
        for n in nodes:
            if n["name"].startswith("午休"):
                return n
        return None
    lo, hi = {"早餐": (0, 10 * 60 + 30), "午餐": (11 * 60, 15 * 60 + 30),
              "晚餐": (16 * 60 + 30, 24 * 60)}[target]
    for n in nodes:
        if n["type"] == "restaurant" and lo <= _tmin(n["time"]) < hi:
            return n
    return None


def _tmin(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def apply_anchor(days_plan: list[dict], anchor: dict) -> tuple[bool, str, list]:
    """把分钟级锚点要求落到已生成的 days_plan 上。

    做法：**整体平移目标日的时间轴**，让目标节点达到要求的时刻——
    平移不动任何景点时长与交通耗时，相邻关系原样保留，是改动最小、
    最不容易出鬼的改法；目标日的「抵达 + 入住」绑定真实班次时刻，
    只平移它之后的节点（等于压缩了入住后的休整，如实说明）。

    返回 (changed, 说明, new_days_plan)；changed=False 时说明里给原因，
    days_plan 原样返回（内部是深拷贝，绝不改调用方的数据）。
    """
    new_days = [dict(d, nodes=[dict(n) for n in d["nodes"]]) for d in days_plan]
    days = len(new_days)
    target = anchor["target"]
    want = anchor["minute"]
    mode = anchor["mode"]
    nap_min = anchor.get("nap_min")

    # 落在哪几天：点名天数 → 那一天；「每天」→ 中间日（抵达/返程日
    # 的时刻绑着真实班次，不接「每天」）；都没说 → 第一个含目标的中间日
    if anchor.get("day") is not None:
        idxs = [anchor["day"] - 1]
        if not (1 <= anchor["day"] <= days):
            return False, f"行程只有 {days} 天，没有第 {anchor['day']} 天", new_days
    elif anchor.get("every_day"):
        idxs = list(range(1, days - 1)) if days >= 3 else [days - 1]
        if not idxs:
            return False, "单天行程的时刻绑定班次，暂不支持按时刻平移", new_days
    else:
        idxs = [i for i in range(1, days - 1)
                if _find_meal_node(new_days[i]["nodes"], target)]
        if not idxs:
            idxs = [i for i in range(days) if _find_meal_node(new_days[i]["nodes"], target)]
        if not idxs:
            return False, f"行程里没有找到可调的{target}时段", new_days

    details: list[str] = []
    changed_any = False
    for i in idxs:
        nodes = new_days[i]["nodes"]
        node = _find_meal_node(nodes, target)
        if node is None:
            details.append(f"Day {i + 1} 没有{target}节点"
                           + ("（午休只在带幼儿或点名午休时插入）"
                              if target == "午休" else ""))
            continue
        nap_node = next((n for n in nodes if n["name"].startswith("午休")), None)
        nap_change = bool(nap_min and nap_node
                          and nap_node["duration_min"] != nap_min)
        # 平移起点：抵达日跳过「抵达 + 入住」（班次时刻不可动）
        frm = 0
        if i == 0 and nodes and nodes[0]["name"].startswith("抵达"):
            frm = 1
        delta = 0
        clamped = False
        if want is not None:
            cur = _tmin(node["time"]) + (node["duration_min"] if mode == "end" else 0)
            delta = want - cur
            # 边界钳制：出发不早于 07:30、收摊不晚于 23:00
            if frm < len(nodes):
                lo = _ANCHOR_START_EARLIEST - _tmin(nodes[frm]["time"])
                end_last = _tmin(nodes[-1]["time"]) + nodes[-1]["duration_min"]
                hi = _ANCHOR_END_LATEST - end_last
                clamped = delta != max(lo, min(delta, hi))
                delta = max(lo, min(delta, hi))
            if abs(delta) <= 2 and not nap_change:
                details.append(f"Day {i + 1} {target}已经是 "
                               f"{fmt(want)}{'前结束' if mode == 'end' else '开始'}，"
                               "时间轴没动")
                continue
        elif not nap_change:
            details.append(f"Day {i + 1} 午休已经是 "
                           f"{(nap_node or {}).get('duration_min')} 分钟，时间轴没动")
            continue
        # 落地：纯平移——节点之间还有交通耗时形成的空隙（transit 不是节点，
        # 顺序重推会把空隙压没），所以每个节点原时刻 + delta，空隙原样保留；
        # 午休时长调整作为第二段平移，只影响它之后的节点
        if want is not None and abs(delta) > 2:
            for n in nodes[frm:]:
                n["time"] = fmt(_tmin(n["time"]) + delta)
        line_nap = ""
        if nap_change:
            extra = nap_min - nap_node["duration_min"]
            nap_node["duration_min"] = nap_min
            _nap_t = _tmin(nap_node["time"])
            for n in nodes:
                if _tmin(n["time"]) > _nap_t:
                    n["time"] = fmt(_tmin(n["time"]) + extra)
            line_nap = f"；午休改为 {nap_min} 分钟"
        changed_any = True
        if want is not None:
            cur = _tmin(node["time"]) - delta + (node["duration_min"] if mode == "end" else 0)
            got = cur + delta
            line = (f"Day {i + 1} {target}{ '结束' if mode == 'end' else '开始' }调到 "
                    f"{fmt(got)}（原 {fmt(cur)}，全天时间轴"
                    f"{'顺延' if delta > 0 else '提前'} {abs(delta)} 分钟）")
            if clamped:
                line += (f"——要求超出 {fmt(_ANCHOR_START_EARLIEST)}~"
                         f"{fmt(_ANCHOR_END_LATEST)} 的可行范围，已取最近可行时刻")
        else:
            line = f"Day {i + 1} 午休时长已改为 {nap_min} 分钟"
            line_nap = ""   # 时长变化已在本句说明，别再拼一遍
        # 平移把当天拖得太晚要明说，别让人到跟前才发现晚饭快九点
        end_last = _tmin(nodes[-1]["time"]) + nodes[-1]["duration_min"]
        if end_last > 21 * 60 + 30:
            line += f"；注意：当天最晚一个安排将到 {fmt(end_last)} 结束，偏晚"
        details.append(line + line_nap)
    return changed_any, "；".join(details), new_days


def compose(slots: dict, pois_res: dict, weather_res: dict,
            outbound_arrive: str, outbound_depart_last: str = "18:00",
            hotel_geo: list[float] | None = None,
            explain: bool = False,
            reranker: Callable[[list[dict], dict], list[str] | None] | None = None,
            ) -> tuple[list[dict], dict]:
    """outbound_depart_last：最后一天返程班次发车时刻，决定当天上午能否排景点。

    reranker：可选的 LLM 重排函数，只在候选集内给出偏好次序。
    返回 None 表示重排失败，编排自动沿用规则排序——不抛异常、不空转。
    """
    all_pois = pois_res.get("items") or []
    attractions = [p for p in all_pois if p["category"] == "attraction"]
    # 位置估算的兜底点（用户点名/位置估算）只在**用户真的点名**时才进候选。
    # 它们本来是 autofetch 为"库里没有的景点"临时造的占位（坐标是城市中心），
    # 但吸引力给得高、距离又像"就在市中心"，于是每份行程都会被选中、还因为
    # 「位置待确认」独占一整天——实测成都 5 天里都江堰、大熊猫基地各吃掉一天，
    # 真正的市区景点反而没位置。
    _must_terms = [m for m in (slots.get("must_visit") or []) if m]
    attractions = [p for p in attractions
                   if not _geo_uncertain(p)
                   or any(name_hit(m, p["name"]) for m in _must_terms)]
    foods = [p for p in all_pois if p["category"] == "restaurant"]
    # 「不吃辣」这类硬口径：有就驱动餐厅筛选与点单提示，不许只躺在槽位里
    _restrict = " ".join((slots.get("diet") or []) + (slots.get("hard_prefs") or [])
                         + (slots.get("avoid") or []))
    spicy_free = "辣" in _restrict
    with_kid = _with_kid(slots)
    # 午休触发面：带幼儿，或用户显式要午休（需午休）；后者措辞不提幼儿
    _nap_asked = "需午休" in (slots.get("hard_prefs") or [])
    want_nap = with_kid or _nap_asked
    _nap_label = None if with_kid else "午休（回酒店小睡）"
    days = slots["days"]
    cap = PACE_CAP[slots["pace"]]
    start = date.fromisoformat(slots["date_range"]["start"])
    home = hotel_geo or [25.693, 100.165]
    log: dict = {"candidates": [], "days": [], "violations": []}

    remaining = [p for p in attractions if p.get("geo")]
    pref_rank = build_pref_rank(remaining, slots, reranker, log)
    plan: list[dict] = []

    for i in range(days):
        d = start + timedelta(days=i)
        w = (weather_res.get("days") or [{}])[min(i, len(weather_res.get("days", [])) - 1)]
        # 天气缺数据时 rain_prob 是 None（超出预报窗口又无历史值时），按 0 算
        rain = w.get("rain_prob") or 0
        nodes: list[dict] = []

        # 抵达日：容量由到站时刻决定；离开日：不留新景点
        start_min = 9 * 60
        dep_min = ret_deadline = None
        if i == 0:
            hh, mm = (int(x) for x in outbound_arrive.split(":"))
            arrive = hh * 60 + mm
            nodes.append(_node(outbound_arrive, "抵达 + 入住", "transit", 90,
                               "车站/机场打车", "先放行李再出门，稍作休整"))
            start_min = arrive + 90
            day_cap = 1 if arrive < 15 * 60 else 0
        elif i == days - 1:
            nodes.append(_node("09:00", "早市 / 伴手礼", "attraction", 60, "步行",
                               "当地特产在早市买更便宜，景区包装款溢价高"))
            hh, mm = (int(x) for x in outbound_depart_last.split(":"))
            day_cap = 1 if hh >= 15 else 0
            start_min = 10 * 60
            dep_min = hh * 60 + mm
            # 返程日当天所有活动都要赶在这之前收尾
            ret_deadline = dep_min - STATION_BUFFER_MIN
        else:
            day_cap = cap

        # 选点：先取最高分做锚点，再按距离就近成团
        # 位置待确认的点不排进抵达/返程日——它可能是跨城一日游，需要完整一天；
        # 排在"13:30 出发、4 小时逛完"的抵达日下午是最容易崩的排法。
        if i in (0, days - 1):
            pool_remaining = [p for p in remaining if not _geo_uncertain(p)]
        else:
            pool_remaining = remaining
        picked = _pick_chain(pool_remaining, slots, rain, home, day_cap, d, log, pref_rank)
        # 紧迫度高（闭馆早、耗时长）的先占时段，同等紧迫度保持原有成团顺序
        picked = sorted(picked, key=lambda x: (_must_start(x), picked.index(x)))

        # 时间轴装配
        # clock 必须从 start_min 起：返程日 09:00 已排了「早市/伴手礼」（60 分钟），
        # 景点若也从 09:00 起排，就会出现「早市 09:00-10:00、望江楼 09:15」的重叠。
        clock = start_min
        prev_geo = home
        prev_uncertain = False
        lunch_done = False
        used_food: set[str] = set()
        move_total = 0
        dropped: list[dict] = []
        used_pois: list[dict] = []
        for p in picked:
            uncertain = _geo_uncertain(p)
            if uncertain or prev_uncertain:
                # 位置估算的点不算里程、不给精确耗时——估出来的"16 分钟"是假的
                km, tmin, mode = None, 0, "位置待确认"
                transit_txt = "位置待确认，交通时间未计入"
            else:
                km = haversine_km(prev_geo, p["geo"])
                mode, tmin = transit_estimate(km, prev_geo, p["geo"])
                transit_txt = f"{mode} {tmin}分钟（{km}km）"
            move_total += tmin
            clock += tmin
            rainy_swap = rain > 60 and p["intensity"] != "low" and p.get("indoor_alternative")
            name = p["indoor_alternative"] if rainy_swap else p["name"]
            tip = (f"原计划 {p['name']}，降雨概率 {rain}% 已调整" if rainy_swap
                   else (f"需提前 1 天预约；{p['open_hours']} 开放" if p["need_booking"]
                         else f"{p['open_hours']} 开放"))

            # 进景点前先看会不会跨过饭点：11:26 开始逛 150 分钟，等于 14:00 才吃午饭。
            # 只在已经接近饭点（11:00 后）时才插，避免一早上就吃午饭。
            if (not lunch_done and i not in (0, days - 1) and clock >= 11 * 60
                    and clock + p["duration_min"] > 12 * 60 + 30):
                lunch = _pick_meal(foods, [("午餐", prev_geo, 3.0),
                                           ("晚餐", prev_geo, 6.0)],
                                   spicy_free, used_food)
                if lunch:
                    meal, lmin = _meal_node(lunch, "午餐", prev_geo, clock,
                                            spicy_free, slots, "先吃饭再逛，避免拖到下午两点")
                    clock += lmin
                    nodes.append(meal)
                    used_food.add(lunch["poi_id"])
                    clock += lunch["duration_min"]
                else:
                    nodes.append(_meal_fallback(clock, "午餐", spicy_free))
                    clock += 60      # 兜底餐也要吃时间：否则下一站从 12:27+23min=12:50 排，与餐位重叠
                lunch_done = True
                # 吃完再算一次到景点的路程（估算坐标不算）
                if uncertain or prev_uncertain:
                    tmin = 0
                    transit_txt = "位置待确认，交通时间未计入"
                else:
                    km = haversine_km(prev_geo, p["geo"])
                    mode, tmin = transit_estimate(km, prev_geo, p["geo"])
                    transit_txt = f"{mode} {tmin}分钟（{km}km）"
                move_total += tmin
                clock += tmin
            # 营业时间校验：到达 + 时长是否越过闭馆
            hours = parse_hours(p.get("open_hours", ""))
            if hours and clock + p["duration_min"] > hours[1]:
                # 当天确实排不进去 → 顺延到次日，而不是硬塞并标警告
                dropped.append({"day": i + 1, "poi": p["name"],
                                "why": f"按当前动线 {fmt(clock)} 到达，无法在 {fmt(hours[1])} 闭馆前完成"})
                clock -= tmin + (0 if not nodes else 0)
                move_total -= tmin
                continue
            day_latest = ret_deadline if ret_deadline is not None else DAY_END_LATEST
            if clock + p["duration_min"] > day_latest:
                # 营业时间允许、但会把一天拖到深夜/赶不上返程 → 同样顺延
                if i == days - 1:
                    why = (f"{fmt(clock)} 才开始，结束将晚于 {fmt(day_latest)}，"
                           f"赶不上 {outbound_depart_last} 的返程班次"
                           f"（发车前要留 {STATION_BUFFER_MIN} 分钟去车站）；"
                           f"一定要去的话加一天，或换更晚的返程班次")
                else:
                    why = (f"{fmt(clock)} 才开始，结束将晚于 {fmt(DAY_END_LATEST)}，"
                           f"移到次日更合理")
                dropped.append({"day": i + 1, "poi": p["name"],
                                "why": why})
                clock -= tmin
                move_total -= tmin
                continue
            if hours and clock < hours[0]:
                tip += f"　离开门 {fmt(hours[0])} 还有 {hours[0] - clock} 分钟，可先逛周边"
            if uncertain:
                tip += "　位置为估算（未收录），可能是市郊/跨城，出发前确认交通"
            if with_kid:
                tip += f"　{_kid_tip(name)}"
            # 无障碍诉求（婴儿车/少台阶/轮椅）：库里没有各景点的无障碍数据，
            # 排进来不等于"已确保满足"——每个点都要把"未核实"写明
            if "需无障碍" in (slots.get("hard_prefs") or []):
                tip += "　无障碍设施未核实（台阶/坡道以现场为准）"
            # 兴趣标记先挂在节点上，备注前缀等整份行程排完再统一生成——
            # 弱命中（喝茶→公园）如果每天各标一个，会变成两个公园都写"近似匹配"。
            ints = []
            for _w in (slots.get("soft_prefs") or []):
                _m = interest_match(_w, p)
                if _m:
                    ints.append({"word": _w, "level": _m[0], "via": _m[1]})
            nodes.append(_node(fmt(clock), name, "attraction", p["duration_min"],
                               transit_txt, tip,
                               0 if rainy_swap else p["cost"],
                               booking_required=p["need_booking"] and not rainy_swap,
                               poi_id=p["poi_id"], geo=p["geo"], interests=ints,
                               geo_uncertain=uncertain))
            clock += p["duration_min"]
            # 估算坐标不能当天后续节点的基准点，否则误差会一路传下去
            if not uncertain:
                prev_geo = p["geo"]
            prev_uncertain = uncertain
            used_pois.append(p)

            # 午餐：本段结束后已过 12:00 且当天还没吃，就近补一顿
            if (not lunch_done and i != days - 1 and clock >= 12 * 60
                    and clock <= 14 * 60 + 30):
                lunch = _pick_meal(foods, [("午餐", prev_geo, 3.0),
                                           ("晚餐", prev_geo, 6.0)],
                                   spicy_free, used_food)
                if lunch:
                    meal, lmin = _meal_node(lunch, "午餐", prev_geo, clock,
                                            spicy_free, slots, "当地口味，按你的忌口筛过一轮")
                    clock += lmin
                    nodes.append(meal)
                    used_food.add(lunch["poi_id"])
                    clock += lunch["duration_min"]
                else:
                    nodes.append(_meal_fallback(clock, "午餐", spicy_free))
                    clock += 60      # 同上：兜底餐占 60 分钟，时间轴照常往后推
                lunch_done = True
                clock = _insert_kid_nap(nodes, clock, i, days, want_nap,
                                        _nap_label)

        # 兜底午餐：景点排完仍没吃上（如上午只有一个景点），在自由活动前就近补一顿。
        # 库里没有餐厅也得留出这段时间——"每天有明确用餐点"不是有数据才成立的承诺。
        if not lunch_done and i not in (0, days - 1) and used_pois:
            clock = max(clock, 11 * 60 + 30)
            lunch = _pick_meal(foods, [("午餐", prev_geo, 6.0),
                                       ("晚餐", prev_geo, 6.0)],
                               spicy_free, used_food)
            if lunch:
                meal, lmin = _meal_node(lunch, "午餐", prev_geo, clock,
                                        spicy_free, slots, "当天唯一一顿坐下来吃的，不用赶")
                clock += lmin
                nodes.append(meal)
                used_food.add(lunch["poi_id"])
                clock += lunch["duration_min"]
            else:
                nodes.append(_meal_fallback(clock, "午餐", spicy_free))
                clock += 60
            lunch_done = True
            clock = _insert_kid_nap(nodes, clock, i, days, with_kid)

        # 返程日：下午/傍晚才发车的话，中午也得坐下来吃一顿再走。
        # 原来只在非返程日补餐，16 点的班次配着"早市 + 一个景点"就是饿着肚子赶车，
        # 质检还因为"最后一天豁免"假装通过。
        # 但饭必须真的吃得完：17:03 才吃完景点还硬塞 60 分钟午餐，
        # 排出 18:03 吃完饭赶 18:00 的车（杭州实测）。装不下就如实改成
        # 「打包 + 提前动身」，不排假时间表。
        if i == days - 1 and not any(n["type"] == "restaurant" for n in nodes):
            try:
                dep_h = int(str(outbound_depart_last).split(":")[0])
            except (ValueError, AttributeError):
                dep_h = 18
            if dep_h >= 13 and ret_deadline is not None:
                clock = max(clock, 12 * 60)
                lunch = _pick_meal(foods, [("午餐", prev_geo, 6.0),
                                           ("晚餐", prev_geo, 6.0)],
                                   spicy_free, used_food)
                if lunch:
                    meal, lmin = _meal_node(lunch, "午餐", prev_geo, clock,
                                            spicy_free, slots, "下午才发车，吃饱再走")
                    ltotal = lmin + lunch["duration_min"]
                else:
                    ltotal = 60
                if clock + ltotal <= ret_deadline:
                    if lunch:
                        clock += lmin
                        nodes.append(meal)
                        used_food.add(lunch["poi_id"])
                        clock += lunch["duration_min"]
                    else:
                        nodes.append(_meal_fallback(clock, "午餐", spicy_free))
                        clock += 60
                else:
                    nodes.append(_node(
                        fmt(clock), "打包简餐，提前出发去车站", "transit",
                        max(dep_min - clock, 30), "—",
                        f"{outbound_depart_last} 发车，坐下来吃再赶车时间不够——"
                        "候车时或车上解决，别为了吃饭误车"))

        # 晚餐：定位到当日最后一个点位附近
        remaining = [x for x in remaining if x not in used_pois]
        # 同一个点名可能对应两条记录：权威记录（有坐标）和早先 autofetch 落的
        # 兜底记录（位置估算）。权威记录排过之后，兜底那条不能再单独占一天——
        # 实测成都 5 天里「成都大熊猫繁育研究基地」排 Day3、同名的
        # 「大熊猫基地」又排 Day4，用户看到同一个景点玩两遍。
        remaining = [p for p in remaining
                     if not (_geo_uncertain(p)
                             and any(name_hit(p["name"], u["name"]) for u in used_pois))]

        if i < days - 1:
            dinner = _pick_meal(foods, [("晚餐", prev_geo, 6.0),
                                        ("晚餐", home, 6.0)],
                                spicy_free, used_food)
            if dinner:
                dkm = haversine_km(prev_geo, dinner.get("geo") or prev_geo)
                dmode, dmin = transit_estimate(dkm, prev_geo, dinner.get("geo") or prev_geo)
                dinner_start = max(clock + dmin + 20, 18 * 60)
                if dinner_start - clock > 120:
                    nodes.append(_node(fmt(clock + 30), "自由活动 / 回客栈休息", "rest",
                                       dinner_start - clock - 30, "—",
                                       "不强塞景点：这一段留白，累了就早回"))
                d_tip = "回住宿方向顺路吃"
                if spicy_free:
                    d_tip += "；已按「不吃辣」优先不辣的店，点单时再确认一次"
                nodes.append(_node(fmt(dinner_start), dinner["name"],
                                   "restaurant", dinner["duration_min"],
                                   f"{dmode} {dmin}分钟（{dkm}km）", d_tip,
                                   dinner["cost"], poi_id=dinner["poi_id"],
                                   geo=dinner.get("geo"),
                                   interests=_meal_interests(dinner, slots)))
            else:
                fb = _meal_fallback(max(clock, 18 * 60), "晚餐", spicy_free)
                nodes.append(fb)
                clock = max(clock, 18 * 60) + 60   # 与午餐兜底一致：兜底餐也占时间

        nodes.sort(key=lambda x: x["time"])
        cost = sum(n["cost"] for n in nodes if n["type"] == "attraction")
        # 主题必须与当天实际角色一致：旧写法 [抵达适应,核心景区,人文深度,
        # 返程收尾][min(i,3)] 会让 5 天行程的 Day4、Day5 都标「返程收尾」，
        # 可 Day4 还排着景点和晚餐（用户实测）。只有真正发车的最后一天
        # 才是返程日；单天行程是「一日往返」。
        if days == 1:
            theme = "一日往返"
        elif i == days - 1:
            theme = "返程收尾"
        else:
            theme = ["抵达适应", "核心景区", "人文深度"][min(i, 2)]
        plan.append({
            "day": i + 1,
            "date": d.isoformat(),
            "theme": theme,
            "intensity": "high" if cost >= 200 else ("low" if cost == 0 else "medium"),
            "nodes": nodes,
            "move_min": move_total,
            "day_budget": cost * slots["party_size"] + 150 * slots["party_size"],
            "rainy_plan": "已按降雨概率调整" if rain > 60 else "户外优先",
            # 当天有点位坐标不可靠：前端/文案据此标注"位置待确认"
            "geo_uncertain": any(_geo_uncertain(p) for p in used_pois),
        })
        if dropped:
            log.setdefault("deferred", []).extend(dropped)
        log["days"].append({"day": i + 1, "move_min": move_total,
                            "picked": [p["name"] for p in used_pois]})

    # 顺延后仍未排入的点，才是真正的冲突（而非已修复）
    deferred_names = {d["poi"] for d in log.get("deferred", [])}
    for p in remaining:
        if p["name"] in deferred_names:
            log["violations"].append({"day": 0, "poi": p["name"],
                                      "why": "全程未能排入，受闭馆时间与动线限制"})
    finalize_interests(plan, slots, {p["poi_id"]: p for p in attractions})
    return plan, log


def _pick_chain(remaining: list[dict], slots: dict, rain: int, home: list[float],
                quota: int, d: date, log: dict,
                pref_rank: dict[str, int] | None = None) -> list[dict]:
    """锚点 + 就近成团，受单日移动时长上限约束。"""
    if quota <= 0:
        return []
    pool, blocked = [], []
    for p in remaining:
        if not _open_on(p, d):
            continue
        why = veto(p, slots)
        if why:
            blocked.append({"name": p["name"], "why": why})
            continue
        pool.append(p)
    if blocked and log is not None:
        log.setdefault("vetoed", []).extend(blocked)
    if not pool:
        return []

    scored = []
    for p in pool:
        s, why = score_poi(p, slots, rain, home, pref_rank=pref_rank)
        scored.append((s, p, why))
        if log is not None:
            log["candidates"].append({"name": p["name"], "score": s, "reasons": why})
    scored.sort(key=lambda x: -x[0])

    # 用户点名的必去点先占位——这是"需求"，不是"偏好"。
    # 只靠 +4.0 加分会被距离成本压掉（远处景点 -5 半径惩罚 + 线性距离惩罚），
    # 结果就是用户说"一定要去喜洲"，系统却因为 18km 而换了个近的。
    must_names = [m for m in (slots.get("must_visit") or []) if m]
    must = []
    for m in must_names:
        hits = [p for p in pool if name_hit(m, p["name"])]
        if not hits:
            continue
        # 同一个点名可能命中多条：库里有权威记录（有坐标），也有一条早先
        # autofetch 落的兜底记录（无坐标、标位置估算）。两条都收进来会
        # 触发"位置待确认独占一天"，真实景点反而被当成不确定点。
        hits.sort(key=lambda p: (_geo_uncertain(p), -p.get("appeal", 0)))
        if hits[0] not in must:
            must.append(hits[0])
    if must and log is not None:
        log.setdefault("must_visit", []).extend(
            [{"name": p["name"], "day": d.isoformat()} for p in must])

    picked = list(must)
    if not picked:
        # 位置待确认的点必须有一整天。它在分数竞争里永远输给市区景点
        # （坐标按城市中心算，看着"不顺路"就出局），成都实测 4 天行程
        # 根本排不进，只能落进「未能排入」。用户点名了它就给它一天，
        # 这一天的其余名额由上面的 early return 清空。
        unc = [p for p in pool if _geo_uncertain(p)]
        if unc:
            _by_id = {p["poi_id"]: s for s, p, _ in scored}
            picked = [max(unc, key=lambda p: _by_id.get(p.get("poi_id"), 0))]
        else:
            # 起手点 = 分数最高者（含 LLM 偏好加成）。
            # 闭馆紧迫度只在后面决定"谁先排"，不参与"选谁"——否则用户明说想去的点
            # 会被一个"关门早"的点顶掉，偏好就失效了。
            #
            # 但"分数最高"不够：名额紧张时（4 天里有 1 天给了跨城一日游），
            # 用户点名的兴趣（大熊猫）会被几个近处的热门点挤出局。
            # 兴趣点因此优先占锚点位置——这是"用户说了想看"，不是"系统觉得好"。
            prefs = slots.get("soft_prefs") or []
            # 先看"名字直接对上"的兴趣（大熊猫 → 大熊猫基地），这类必须优先占位；
            # 只有同义词命中的（喝茶 → 公园）放在次一档，避免把名额占满。
            strong = [p for p in pool
                      if any(interest_hit(w, p, strong_only=True) for w in prefs)]
            weak = [p for p in pool
                    if p not in strong and any(interest_hit(w, p) for w in prefs)]
            score_by_id = {p["poi_id"]: s for s, p, _ in scored}
            cand = strong or weak or [scored[0][1] if scored else None]
            cand = [p for p in cand if p]
            picked = [max(cand, key=lambda p: score_by_id.get(p.get("poi_id"), 0))] if cand else []

    # 位置估算的点单独占一天：交通时间未知，硬跟市区景点凑团只会排出假动线。
    # 它不是"顺路多逛一个"，而是一整段需要用户确认的行程。
    if any(_geo_uncertain(p) for p in picked):
        return [p for p in picked if _geo_uncertain(p)]

    move, prev = 0, picked[0]["geo"]
    for p in picked[1:]:
        move += transit_estimate(haversine_km(prev, p["geo"]), prev, p["geo"])[1]
        prev = p["geo"]
    sight_min = sum(p["duration_min"] for p in picked)
    while len(picked) < max(quota, len(must)):
        cand = []
        for s, p, why in scored:
            if p in picked:
                continue
            # 位置估算的点**绝不**和市区点凑同一天。上面的 early return 只兜住了
            # "起手点/必去点本身是估算点"的情况；一旦当天起手是可靠点（如必去
            # 人民公园），补位循环会把它当顺路点拉进来——成都实测：人民公园 +
            # 川大博物馆 + 都江堰（240 分钟、位置待确认）挤在 Day2，交通时间还
            # 没计入，晚餐拖到 19:48。它必须自己占一天。
            if _geo_uncertain(p):
                continue
            # 单日净游玩时长封顶：240 分钟远郊点 + 两个市区点 = 一整天逛到天黑
            if sight_min + p["duration_min"] > DAY_SIGHT_CAP_MIN:
                continue
            km = haversine_km(prev, p["geo"])
            if km > MAX_HOP_KM:
                continue
            if move + transit_estimate(km, prev, p["geo"])[1] > DAY_MOVE_BUDGET_MIN:
                continue
            cand.append((s - km * 0.4, p, km))
        if not cand:
            break
        cand.sort(key=lambda x: -x[0])
        _, nxt, km = cand[0]
        picked.append(nxt)
        sight_min += nxt["duration_min"]
        move += transit_estimate(km, prev, nxt["geo"])[1]
        prev = nxt["geo"]
    return picked


def _nearest(foods: list[dict], label: str, geo: list[float] | None,
             max_km: float | None = None) -> dict | None:
    cands = [f for f in foods if label in f.get("tags", [])] or foods
    if not cands:
        return None
    if not geo:
        return cands[0]
    best = min(cands, key=lambda f: haversine_km(geo, f.get("geo") or geo))
    if max_km is not None and haversine_km(geo, best.get("geo") or geo) > max_km:
        return None
    return best


# 「不吃辣」能不能自动落实，取决于候选餐厅自己带不带辣线索：
# 名字/标签/备注里出现麻辣词且没有"清汤/不辣"这类豁免标记的，先往后排。
_SPICY_WORDS = ("麻辣", "红油", "火锅", "串串", "辣", "麻婆", "水煮", "香锅", "冒菜")
_MILD_WORDS = ("清汤", "不辣", "免辣", "清淡", "素", "甜", "豆花", "茶")


def _looks_spicy(f: dict) -> bool:
    hay = f["name"] + "".join(f.get("tags") or []) + (f.get("note") or "")
    if any(w in hay for w in _MILD_WORDS):
        return False
    return any(w in hay for w in _SPICY_WORDS)


def _food_pool(foods: list[dict], spicy_free: bool) -> list[dict]:
    """不吃辣时优先不辣的店；全城只有辣的也没办法，仍给全部，靠点单提示兜底。"""
    if not spicy_free:
        return foods
    safe = [f for f in foods if not _looks_spicy(f)]
    return safe or foods


def _pick_meal(foods: list[dict], attempts: list[tuple[str, list[float] | None, float]],
               spicy_free: bool, used_food: set[str]) -> dict | None:
    """跨天选餐位。attempts 按顺序尝试（标签, 参照点, 最远距离）。

    不吃辣的三级候选：没用过的不辣店 → 不辣店（哪怕中午刚去过——
    回头吃茶馆也好过把人推进麻辣川菜）→ 实在没有才放开到全部。
    """
    unused = [f for f in foods if f.get("poi_id") not in used_food]
    if spicy_free:
        cands = ([f for f in unused if not _looks_spicy(f)]
                 or [f for f in foods if not _looks_spicy(f)]
                 or unused)
    else:
        cands = unused
    for label, geo, mk in attempts:
        pick = _nearest(cands, label, geo, max_km=mk)
        if pick:
            return pick
    return None


def _meal_fallback(clock: int, label: str, spicy_free: bool) -> dict:
    """资料库没有餐厅也要照常留用餐时段：如实写"就近自选"，不冒充具体店。"""
    tip = ("当前行程点位附近暂无合适的餐厅候选，建议在上一站周边就近用餐，"
           "出发前看点评确认营业情况")
    if spicy_free:
        tip += "；不吃辣的话点单时说明要清汤/不辣"
    return _node(fmt(clock), f"{label} · 就近自选", "restaurant", 60,
                 "步行（就近）", tip, 60)


def _meal_node(pick: dict, label: str, from_geo: list[float] | None,
               clock: int, spicy_free: bool, slots: dict, tip: str) -> tuple[dict, int]:
    """库里选中的餐位 → 节点。不吃辣时提示已筛过、点单再确认；茶馆强命中也挂兴趣标记。"""
    km = haversine_km(from_geo or pick.get("geo") or [0, 0],
                      pick.get("geo") or from_geo or [0, 0])
    mode, tmin = transit_estimate(km, from_geo, pick.get("geo"))
    full_tip = tip
    if spicy_free:
        full_tip += "；已按「不吃辣」优先不辣的店，点单时再确认一次"
    node = _node(fmt(clock + tmin), pick["name"], "restaurant",
                 pick["duration_min"], f"{mode} {tmin}分钟（{km}km）",
                 full_tip, pick["cost"], poi_id=pick["poi_id"],
                 geo=pick.get("geo"), interests=_meal_interests(pick, slots))
    return node, tmin


def _meal_interests(poi: dict, slots: dict) -> list[dict]:
    """餐位也满足兴趣：喝茶这类就是靠茶馆落地的。只认强命中，弱命中别凑热闹。"""
    ints = []
    for w in (slots.get("soft_prefs") or []):
        m = interest_match(w, poi)
        if m and m[0] == "strong":
            ints.append({"word": w, "level": m[0], "via": m[1]})
    return ints


def parse_hours(spec: str) -> tuple[int, int] | None:
    """解析 "08:30-16:00" → (510, 960)；"全天"/空值返回 None。"""
    if not spec or "-" not in spec:
        return None
    try:
        a, b = spec.split("-")
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except ValueError:
        return None


def finalize_interests(plan: list[dict], slots: dict,
                       pois_by_id: dict[str, dict]) -> None:
    """整份行程范围内收敛兴趣标记，并把备注前缀写上去。

    两件事：
    1. 一个弱命中不能到处标。「喝茶」靠"公园"沾边，人民公园和望江楼公园都符合，
       可用户看到两站都写"喝茶·近似"只会觉得在凑数。强命中全留，弱命中只留第一个。
    2. 备注里写清"靠什么落地的"，用户才知道这条偏好到底满足没有。
    """
    for w in (slots.get("soft_prefs") or []):
        holders = [n for d in plan for n in d["nodes"]
                   if any(i["word"] == w for i in n.get("interests") or [])]
        strong = [n for n in holders
                  if any(i["word"] == w and i["level"] == "strong"
                         for i in n.get("interests") or [])]
        keep = {id(n) for n in (strong or holders[:1])}
        for n in holders:
            if id(n) in keep:
                continue
            n["interests"] = [i for i in n.get("interests") or []
                              if i["word"] != w]
    for d in plan:
        for n in d["nodes"]:
            ints = n.get("interests") or []
            if not ints or (n.get("tip") or "").startswith("兴趣「"):
                continue
            note = interest_note(ints, pois_by_id.get(n.get("poi_id")) or {})
            if note:
                n["tip"] = f"{note}　{n.get('tip') or ''}".strip()


def _node(time_s: str, name: str, type_: str, dur: int, transit: str, tip: str,
          cost: float = 0, booking_required: bool = False,
          poi_id: str | None = None, geo: list[float] | None = None,
          interests: list[dict] | None = None,
          geo_uncertain: bool = False) -> dict:
    return {"time": time_s, "name": name, "type": type_, "duration_min": dur,
            "transit": transit, "tip": tip, "cost": cost,
            "booking_required": booking_required,
            "poi_id": poi_id, "geo": geo,
            # 这一站满足了哪些兴趣、是强命中还是沾边（渲染层据此打标签）
            "interests": list(interests or []),
            # 坐标不可信：地图画虚线空心圈、不算里程（渲染层据此改样式）
            "geo_uncertain": bool(geo_uncertain)}


def fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build_pref_rank(candidates: list[dict], slots: dict,
                    reranker: Callable[[list[dict], dict], list[str] | None] | None,
                    log: dict) -> dict[str, int]:
    """把 top-k 候选交给 reranker，得到 poi_id → 名次，作为打分的微调项。

    只调一次而不是逐日重排：逐日会因天气变化反复请求，成本高且排序不稳定。
    reranker 返回 None（模型不可用/校验失败）时返回空字典，编排完全按规则走。
    """
    if reranker is None or len(candidates) < 2:
        return {}

    pool = []
    for p in candidates:
        if veto(p, slots):
            continue
        s, _ = score_poi(p, slots, 0, None)
        pool.append({**p, "_score": s})
    if len(pool) < 2:
        return {}

    pool.sort(key=lambda x: -x["_score"])
    top = pool[:5]
    allowed = {p["poi_id"] for p in top}

    try:
        order = reranker(top, slots)
    except Exception as e:                      # 钩子内部异常绝不能打断编排
        log["rerank"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {}

    if not order:
        log["rerank"] = {"ok": False, "error": "重排未返回有效次序，沿用规则排序"}
        return {}

    # 白名单二次兜底：即使 reranker 没做校验，这里也不接受候选集外的 id
    order = [pid for pid in order if pid in allowed]
    if not order:
        log["rerank"] = {"ok": False, "error": "重排结果全部非法，沿用规则排序"}
        return {}

    log["rerank"] = {"ok": True, "order": order,
                     "names": [p["name"] for p in top if p["poi_id"] in order]}
    return {pid: i for i, pid in enumerate(order)}
