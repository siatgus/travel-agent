"""意图路由与信息直答测试。

背景（用户实测反馈）：问「北京的真实天气」被回「排行程还差 2 件事」，
连问三次后系统直接给了一份**上海**行程——目标城市、日期全是编的。
这里锁死三件事：

1. 天气/景点/班次/闲聊不该进排行程流水线；
2. 答这类问题不得改动规划槽位、不得消耗追问轮次、不得产出方案；
3. 没有数据的城市绝不拿别的城市顶替。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import intent
from travel_agent import real_tools
from travel_agent.dialogue import Session

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"　{detail}"))


# ------------------------------------------------------------------ 分类

def test_classify() -> None:
    cases = [
        ("北京的真实天气怎么样", "weather"),
        ("北京天气", "weather"),
        ("上海这周末下雨吗", "weather"),
        ("杭州今天多少度", "weather"),
        ("北京有什么好玩的", "poi"),
        ("杭州到上海怎么走", "transport"),
        ("你好", "smalltalk"),
        ("你是谁", "smalltalk"),
        ("11月8号去大理玩4天，2人，预算6000", "plan"),
        ("北京天气好的话帮我排个行程", "plan"),   # 混合句以规划为准
        ("下个月想去大理玩五天", "plan"),
        # 行程问答必须赢过"行程"这个规划关键词
        ("将行程总结成一段话", "plan_qa"),
        ("帮我概括一下这个行程", "plan_qa"),
        ("这个行程怎么样", "plan_qa"),
        ("把行程总结一下发我", "plan_qa"),
        # 改行程的说法不是问答
        ("重新排一下这个行程", "plan"),
        ("这个行程加上迪士尼再排", "plan"),
        # 点名 + 周边/附近 → 景点问答
        ("天安门广场周边有什么", "poi"),
        ("外滩附近有什么好玩的", "poi"),
        ("介绍一下故宫", "poi"),
        # 疑问句里没有规划动词：在问事，不是要行程（曾把"日本"当景点排进去）
        ("去日本旅游需要办签证吗", "other"),
        ("去北京玩怎么样？", "other"),
        ("大理旅游贵不贵", "other"),
        # 有规划动词的问句仍是规划请求
        ("能帮我安排一下吗", "plan"),
        ("帮我排个大理的行程吧？", "plan"),
    ]
    bad = [(t, want, intent.classify(t)) for t, want in cases
           if intent.classify(t) != want]
    check("意图分类覆盖天气/景点/交通/闲聊/规划", not bad, f"不符={bad}")


def test_city_parsing() -> None:
    check("识别「北京」", intent.city_in("北京的真实天气怎么样") == "北京")
    check("识别「上海」", intent.city_in("上海这周末天气如何") == "上海")
    check("无城市时返回 None", intent.city_in("今天天气怎么样") is None)
    check("不把「大理古城」误判为未知词",
          intent.city_in("大理古城天气") == "大理",
          str(intent.city_in("大理古城天气")))
    # 2026-10-31 用户实测：问「邯郸现在的天气」，城市被截成「邯郸现在」
    check("城市名后带「现在」不并进地名",
          intent.city_in("询问邯郸现在的天气") == "邯郸",
          str(intent.city_in("询问邯郸现在的天气")))
    check("表外城市带时间尾巴也能剥干净",
          intent.city_in("衡水现在天气") == "衡水",
          str(intent.city_in("衡水现在天气")))
    check("「最近北京天气」剥掉前置时间词",
          intent.city_in("最近北京天气") == "北京",
          str(intent.city_in("最近北京天气")))
    check("纯时间词不当城市", intent.city_in("现在天气如何") is None,
          str(intent.city_in("现在天气如何")))
    check("有本地数据的邯郸进城市表（免地理编码）",
          "邯郸" in real_tools.CITY_GEO)


# ------------------------------------------------------------------ 天气直答

def _fake_weather(city, start, end):
    d0 = date.fromisoformat(start)
    days = [{"date": (d0 + timedelta(days=i)).isoformat(), "low": 5, "high": 16,
             "rain_prob": 20, "desc": "多云", "is_forecast": True} for i in range(3)]
    return {"ok": True, "city": city, "days": days, "note": "Open-Meteo 真实预报"}


def test_weather_answer_format() -> None:
    old_get, old_geo = real_tools.get_weather, real_tools.resolve_geo
    real_tools.get_weather = _fake_weather
    real_tools.resolve_geo = lambda c: [39.9, 116.4] if c == "北京" else None
    try:
        text = intent.weather_answer("北京", "北京这周末天气如何")
        ok = ("北京" in text and "℃" in text and "降水" in text
              and "Open-Meteo" in text)
        check("天气回答：含城市、温度区间、降水、数据来源", ok, text[:80])
        check("天气回答不再出现「排行程还差」", "排行程还差" not in text)
        text2 = intent.weather_answer("北京", "北京一周天气")
        check("问一周就查更长窗口", "接下来" in text2, text2[:60])
        unknown = intent.weather_answer("不存在的城市某某", "")
        check("查不到的城市如实说明，不编", "没找到" in unknown, unknown[:40])
    finally:
        real_tools.get_weather, real_tools.resolve_geo = old_get, old_geo


# ------------------------------------------------------------------ 会话级行为

def _session() -> Session:
    return Session(today=date(2026, 10, 31))


def test_weather_never_becomes_plan() -> None:
    """核心回归：连问三次天气，不能出现方案，也不能改槽位。"""
    s = _session()
    replies = [s.turn(q) for q in ("北京的真实天气怎么样", "北京天气",
                                   "北京这周末天气如何")]
    modes = [r.mode for r in replies]
    check("问天气三轮都是直接回答，没有追问", modes == ["answer"] * 3, str(modes))
    check("问天气不产出方案", all(r.plan is None for r in replies))
    check("问天气不消耗追问轮次", all(r.ask_rounds == 0 for r in replies),
          str([r.ask_rounds for r in replies]))
    check("问天气不写规划槽位（目的地仍是空）",
          not s.slots.get("destination"), str(s.slots))
    check("问天气记得住城市（后续排行程可复用）", s.last_city == "北京", str(s.last_city))


def test_weather_then_plan_switch() -> None:
    """先问天气、再决定排行程：意图切换要正常。"""
    s = _session()
    r1 = s.turn("杭州今天天气怎么样")
    check("先问天气 → answer", r1.mode == "answer", r1.mode)
    r2 = s.turn("那帮我排11月8号去大理4天的行程，2人，预算6000")
    check("随后说排行程 → 正常出方案",
          r2.mode == "plan" and r2.plan and r2.slots.get("destination") == "大理",
          f"mode={r2.mode} dest={r2.slots.get('destination')}")


def test_poi_answer_no_invent() -> None:
    # 之前用「北京」测"没数据不瞎编"，后来在线补全真把北京的景点抓进了库——
    # 功能变好了，用例反而挂了。改用虚构城市并先清库，不再依赖知识库现状。
    from travel_agent import kb as _kb
    fake = "鹤壁"                     # 城市表里真实存在，但知识库默认没有它的景点
    db = _kb.get_kb()
    if db:
        for row in db.search_pois(q=fake):
            db.delete_poi(row["poi_id"])
    s = _session()
    r = s.turn(fake + "有什么好玩的")
    check("没有景点数据的城市不瞎编推荐",
          "还没有" in r.render or "没" in r.render, r.render[:60])
    r2 = s.turn("大理有什么好玩的")
    check("有数据的城市列出真实景点",
          r2.mode == "answer" and ("大理" in r2.render and "-" in r2.render),
          r2.render[:80])


def test_spot_mention() -> None:
    """「问某个具体景点」要从问句里剥出景点名；泛问题必须返回 None。"""
    cases = [
        ("上海迪士尼门票多少钱", "上海", "迪士尼"),
        ("上海迪士尼好玩吗", "上海", "迪士尼"),
        ("迪士尼怎么样", None, "迪士尼"),
        ("外滩值得去吗", None, "外滩"),
        ("豫园开放时间", None, "豫园"),
        ("上海有什么好玩的", "上海", None),
        ("大理好玩吗", "大理", None),
        ("周围有什么景点", None, None),
        ("大理的美食推荐", "大理", None),
        # 带括号分店名 + 双问句：邯郸实测「圣旨骨酥鱼（滏东店）」点名落空，
        # 被当成泛推荐、用户以为助手忘了已排好的行程
        ("圣旨骨酥鱼（滏东店）怎么样？门票多少钱", "邯郸", "圣旨骨酥鱼"),
        ("一篓油水饺（中华大街店）怎么样", "邯郸", "一篓油水饺"),
        ("娲皇宫（涉县）值得去吗", "邯郸", "娲皇宫"),
        ("广府古城怎么样？门票多少钱", "邯郸", "广府古城"),
        # 位置类问法 + 指代前缀：邯郸实测「你给我排的老驴头驴肉火烧在哪里」
        # 剥不净 → 点名落空 → 回一串不相干景点（驴头不对马嘴）
        ("老驴头驴肉火烧在哪里", "邯郸", "老驴头驴肉火烧"),
        ("老驴头驴肉火烧在哪", None, "老驴头驴肉火烧"),
        ("你给我排的老驴头驴肉火烧在哪里", None, "老驴头驴肉火烧"),
        ("你排的丛台公园在哪", "邯郸", "丛台公园"),
        ("帮我安排的圣旨骨酥鱼（滏东店）味道怎么样", "邯郸", "圣旨骨酥鱼"),
        ("圣旨骨酥鱼味道怎么样", None, "圣旨骨酥鱼"),
        ("丛台公园地址", None, "丛台公园"),
    ]
    bad = [(t, want, intent.spot_mention(t, c)) for t, c, want in cases
           if intent.spot_mention(t, c) != want]
    check("具体景点点名与泛问题的区分", not bad, f"不符={bad}")


def test_reco_routing() -> None:
    """「推荐几家X」是问答不是规划：归 poi，套话剥出实体；规划词优先。"""
    check("推荐句归 poi 问答",
          intent.classify("推荐几家驴肉火烧") == "poi"
          and intent.classify("哪家火锅好吃") == "poi",
          f"{intent.classify('推荐几家驴肉火烧')}")
    check("含规划词的推荐句仍归 plan",
          intent.classify("帮我推荐个行程") == "plan",
          f"{intent.classify('帮我推荐个行程')}")
    cases = [
        ("推荐几家驴肉火烧", "驴肉火烧"),
        ("推荐一家的火锅店", "火锅店"),
        ("推荐个玩法", "玩法"),
        ("有哪些景点必去", None),   # 泛问走 poi_answer，不是推荐某类实体
        ("帮我推荐个行程", None),
    ]
    bad = [(t, w, intent.reco_keyword(t, "邯郸")) for t, w in cases
           if intent.reco_keyword(t, "邯郸") != w]
    check("推荐句剥套话取实体", not bad, f"不符={bad}")
    out = intent.recommend_answer("邯郸", "驴肉火烧",
                                  [{"name": "老驴头驴肉火烧", "area": "丛台区",
                                    "category": "restaurant", "cost": 25,
                                    "open_hours": "06:30-19:30",
                                    "note": "趁热吃"}])
    check("推荐回答列出店名与人均", "老驴头驴肉火烧" in out and "25" in out, out[:100])
    empty = intent.recommend_answer("邯郸", "驴肉火烧", [])
    check("库里没收录如实说", "没收录" in empty, empty[:80])


def test_spot_answer_template() -> None:
    out = intent.spot_answer("上海", "迪士尼",
                             [{"name": "上海迪士尼乐园", "area": "浦东",
                               "duration_min": 480, "cost": 475,
                               "open_hours": "08:30-20:30", "note": "测试"}],
                             note="测试说明")
    check("景点问答模板带名称/票价/时长",
          "迪士尼" in out and "475" in out and "480" in out, out[:80])


def test_poi_answer_autofetch() -> None:
    """问没收录的城市：先联网/模型补一次再答，不能一句「没有数据」推走。"""
    import travel_agent.autofetch as af

    calls: list[str] = []

    def fake(city, **kw):
        calls.append(city)
        return {"status": "fetched", "city": city, "pois": 3}

    orig = af.ensure_city
    af.ensure_city = fake
    try:
        out = intent.poi_answer("七台河")
    finally:
        af.ensure_city = orig
    check("问未收录城市先自动补全再回答", calls == ["七台河"], f"calls={calls}")
    check("补不到数据时如实说明，不编造景点", "没" in out, out[:80])


def test_smalltalk() -> None:
    s = _session()
    r = s.turn("你好")
    check("闲聊不追问槽位", r.mode == "answer" and "还差" not in r.render, r.render[:50])
    r2 = s.turn("你是谁")
    check("问身份给出能力说明", r2.mode == "answer" and "行程" in r2.render, r2.render[:50])


def test_identity_variants() -> None:
    """问助手本人的各种说法都要归 smalltalk（实测「你是什么模型」答不上来）。"""
    for q in ("你是什么模型", "你是做什么的", "你用的哪个模型", "你是干什么的",
              "你能干嘛", "你是机器人吗"):
        check(f"「{q}」是闲聊", intent.classify(q) == "smalltalk",
              intent.classify(q))
    check("身份判定复用同一张词表", intent.is_identity_question("你是什么模型"))
    check("问景点不会被当身份问题",
          not intent.is_identity_question("大理有什么景点")
          and intent.classify("大理有什么景点") != "smalltalk")
    # 无模型时模板也要答得上，而不是甩一句改行程的话术
    s = _session()
    r = s.turn("你是什么模型")
    check("无模型时身份问题有答案",
          r.mode == "answer" and "行程已经在上面了" not in r.render
          and "助手" in r.render, r.render[:80])


def test_no_destination_no_plan() -> None:
    """只说「想出去玩」：引导问城市，不能拿默认城市凑方案。"""
    s = _session()
    r = s.turn("想出去玩几天")
    check("缺目的地时不出方案", r.mode == "clarify" and r.plan is None, r.mode)
    check("引导语提到城市而不是别的城市名",
          "城市" in r.question or "哪儿" in r.question, r.question[:60])


def test_question_shape() -> None:
    """通用问句探测 + 周边问句剥词：LLM 兜底分类和周边问答的门卫。"""
    check("「天安门广场周边有什么」是问句",
          intent.looks_like_question("天安门广场周边有什么"))
    check("「预算6000」不是问句",
          not intent.looks_like_question("预算6000"))
    check("「杭州」不是问句", not intent.looks_like_question("杭州"))
    check("「去大理玩四天吧」不是问句",
          not intent.looks_like_question("去大理玩四天吧"))
    check("「需要办签证吗」是问句",
          intent.looks_like_question("去日本需要办签证吗"))
    # 周边问句要能剥出中心景点名
    check("剥出「天安门广场」",
          intent.spot_mention("天安门广场周边有什么", "北京") == "天安门广场")
    check("剥出「外滩」",
          intent.spot_mention("外滩附近有什么", "上海") == "外滩")
    # 泛问（城市+周边）不剥出假景点
    check("「上海周边有什么」不剥出景点",
          intent.spot_mention("上海周边有什么", "上海") is None)


def test_comparison_question() -> None:
    """「像迪士尼一样的地方」要按类型找同类，不能拿本地景点清单硬答（实测答成古城石窟）。"""
    yes = ("邯郸有没有像迪士尼一样的地方", "类似西湖这种", "跟丽江差不多的古城",
           "有没有和环球影城一样的地方", "差不多的景点还有吗")
    no = ("大理有什么好玩的", "上海迪士尼门票多少钱", "帮我排邯郸2天行程",
          "推荐几家驴肉火烧")
    for q in yes:
        check(f"类比问句命中：{q}", intent.is_comparison_question(q))
    for q in no:
        check(f"非类比问句：{q}", not intent.is_comparison_question(q))


def test_poi_category_question() -> None:
    """品类问句（游乐园/温泉…）要认出品类，并按品类词筛本地景点。

    实测（2026-11-13）：「本地或周边的中小型游乐园有哪些」回了广府古城、
    娲皇宫、丛台公园，而库里明明有「邯郸方特国色春秋」。
    """
    cat = intent.poi_category("本地或周边的中小型游乐园有哪些")
    check("认出「游乐园」品类", cat is not None and cat[0] == "游乐园", f"{cat}")
    check("水上乐园优先于游乐园",
          (intent.poi_category("附近有水世界吗") or ("",))[0] == "水上乐园")
    check("温泉/博物馆也认", (intent.poi_category("邯郸有温泉吗") or ("",))[0] == "温泉"
          and (intent.poi_category("北京有哪些博物馆") or ("",))[0] == "博物馆")
    for q in ("北京有什么好玩的", "推荐几家驴肉火烧", "帮我排邯郸2天行程"):
        check(f"泛问不算品类：{q}", intent.poi_category(q) is None,
              f"{intent.poi_category(q)}")
    check("品类词进 poi 意图",
          intent.classify("邯郸有温泉吗") == "poi")
    check("提要求带品类词不算问品类",
          not intent.is_category_question("我一定要去喜洲古镇")
          and intent.classify("我一定要去喜洲古镇") != "poi",
          f"{intent.is_category_question('我一定要去喜洲古镇')}")

    pois = [{"name": "邯郸方特国色春秋", "category": "attraction"},
            {"name": "广府古城（永年）", "category": "attraction"},
            {"name": "一篓油水饺（中华大街店）", "category": "restaurant"}]
    hit = intent.category_pois(pois, cat[1])
    check("只留同品类", [p["name"] for p in hit] == ["邯郸方特国色春秋"], f"{hit}")
    check("饭馆不掺进品类",
          intent.category_pois([{"name": "温泉火锅", "category": "restaurant"}],
                               ("温泉",)) == [])


def test_food_stay_questions() -> None:
    """吃住问句是问答不是规划：「有什么酒店」不能触发整单重排（实测回了全新行程）。"""
    check("特色美食认成吃的",
          intent.is_food_question("邯郸还有什么特色美食")
          and intent.is_food_question("这有什么好吃的"))
    check("有什么酒店认成住宿",
          intent.is_stay_question("有什么酒店")
          and intent.is_stay_question("邯郸有什么酒店"))
    check("提住宿要求不算问句", not intent.is_stay_question("酒店要五星的"))
    check("改口味不算问句", not intent.is_food_question("想吃辣的"))
    check("吃住问句进 poi 意图",
          intent.classify("邯郸还有什么特色美食") == "poi"
          and intent.classify("有什么酒店") == "poi")


def test_food_question_split() -> None:
    """美食问句拆两路：问菜品文化 vs 找真实店铺，不能混（2026-11-13 修）。"""
    check("特色美食是知识问",
          intent.food_question_kind("成都有什么特色美食") == "knowledge")
    check("菜品特色是知识问",
          intent.food_question_kind("成都火锅有什么特色") == "knowledge")
    check("火锅店是找店",
          intent.food_question_kind("成都有哪些好吃的火锅店") == "restaurant")
    check("片区菜馆是找店",
          intent.food_question_kind("春熙路附近有哪些川菜馆") == "restaurant")
    check("推荐句式算找店",
          intent.food_question_kind("推荐火锅") == "restaurant")
    check("非美食问句不细分",
          intent.food_question_kind("想吃辣的") is None
          and intent.food_question_kind("有什么酒店") is None)
    check("品类词剥取：火锅店",
          intent.food_keyword("成都有哪些好吃的火锅店", "成都") == "火锅")
    check("品类词剥取：菜系保字",
          intent.food_keyword("春熙路附近有哪些川菜馆") == "川菜")
    check("品类词剥取：火锅特色",
          intent.food_keyword("成都火锅有什么特色", "成都") == "火锅")
    check("纯美食泛问剥不出品类",
          intent.food_keyword("成都有什么特色美食", "成都") is None)
    check("片区词剥取", intent.food_area("春熙路附近有哪些川菜馆") == "春熙路")
    check("无片区词返回 None", intent.food_area("成都有什么特色美食") is None)


def test_nearby_answer_template() -> None:
    """周边问答的模板路径：有数据按距离列、没数据如实说。"""
    pois = [{"name": "崇圣寺三塔", "area": "古城北", "cost": 75,
             "duration_min": 150, "_km": 2.8},
            {"name": "洱海生态廊道", "area": "才村码头", "cost": 0,
             "duration_min": 120, "_km": 2.7}]
    out = intent.nearby_answer("大理", "大理古城", pois)
    check("模板按距离列出周边景点", "崇圣寺三塔" in out and "2.8" in out, out[:80])
    check("模板中心点写全名", "大理古城" in out)
    empty = intent.nearby_answer("大理", "无名点", [])
    check("没数据时如实说，不编", "不敢瞎推荐" in empty or "不够" in empty)


def test_nearby_question_routing() -> None:
    """周边问句三分流：找吃的/找住的/两点路线，都不是「问这个景点」。"""
    check("锚点剥取", intent.nearby_anchor("宽窄巷子附近有什么好吃的")
          == "宽窄巷子")
    check("锚点剥掉城市前缀",
          intent.nearby_anchor("成都宽窄巷子附近有什么好吃的", "成都")
          == "宽窄巷子")
    check("无附近词不剥锚点",
          intent.nearby_anchor("宽窄巷子是什么地方") is None)
    check("周边餐饮问句", intent.is_nearby_food_question(
        "宽窄巷子附近有什么好吃的"))
    check("周边餐厅问句", intent.is_nearby_food_question(
        "宽窄巷子附近有餐厅吗"))
    check("周边餐饮不当酒店问句", not intent.is_nearby_hotel_question(
        "宽窄巷子附近有什么好吃的"))
    check("周边酒店问句", intent.is_nearby_hotel_question(
        "宽窄巷子附近有什么酒店"))
    check("景点问句不判周边餐饮",
          not intent.is_nearby_food_question("宽窄巷子是什么地方"))
    check("两点路线句式",
          intent.nearby_route_ends("从宽窄巷子怎么去锦里")
          == ("宽窄巷子", "锦里"))
    check("路线句式含尾缀",
          intent.nearby_route_ends("从成都东站到宽窄巷子怎么走")
          == ("成都东站", "宽窄巷子"))
    check("路线句式问距离",
          intent.nearby_route_ends("从大理古城到喜洲古镇有多远")
          == ("大理古城", "喜洲古镇"))
    check("规划句不判路线",
          intent.nearby_route_ends("从邯郸出发去成都玩3天") is None)


def test_trim_optional_intent() -> None:
    """「把可去可不去的删掉」要识别成泛指删点，而不是当景点名抽取（冒烟实测）。"""
    yes = ("还是有点赶，把可去可不去的删掉", "删掉一些景点",
           "把可有可无的去掉", "少排几个点", "非必去的就删了吧")
    no = ("去掉灵隐寺", "不想去雷峰塔", "杭州玩2天，加上灵隐寺",
          "西湖怎么样", "成都到杭州的高铁班次")
    for t in yes:
        check(f"泛指删点命中：{t}", intent.wants_trim_optional(t))
    for t in no:
        check(f"点名/无关句不命中：{t}", not intent.wants_trim_optional(t))
    # 抽取层不再产出脏槽位：泛指词组进不了 destination / must_visit / avoid
    from travel_agent import agent as _agent
    s = _agent.extract_slots_rules("还是有点赶，把可去可不去的删掉")
    check("指令碎片不进 destination", not s.get("destination"),
          str(s.get("destination")))
    s2 = _agent.extract_slots_rules("删掉一些景点")
    check("裸数量词不进 avoid", not s2.get("avoid"), str(s2.get("avoid")))
    check("指令碎片不进 must_visit", not s2.get("must_visit"), str(s2.get("must_visit")))


def main() -> int:
    for fn in (test_classify, test_city_parsing, test_weather_answer_format,
               test_weather_never_becomes_plan, test_weather_then_plan_switch,
               test_poi_answer_no_invent, test_spot_mention,
               test_reco_routing,
               test_spot_answer_template, test_poi_answer_autofetch,
               test_smalltalk, test_identity_variants, test_comparison_question, test_poi_category_question,
               test_food_stay_questions,
               test_food_question_split,
               test_question_shape,
               test_nearby_answer_template,
               test_nearby_question_routing,
               test_no_destination_no_plan,
               test_trim_optional_intent):
        fn()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条意图路由断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
