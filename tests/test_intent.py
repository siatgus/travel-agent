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
    ]
    bad = [(t, want, intent.spot_mention(t, c)) for t, c, want in cases
           if intent.spot_mention(t, c) != want]
    check("具体景点点名与泛问题的区分", not bad, f"不符={bad}")


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


def main() -> int:
    for fn in (test_classify, test_city_parsing, test_weather_answer_format,
               test_weather_never_becomes_plan, test_weather_then_plan_switch,
               test_poi_answer_no_invent, test_spot_mention,
               test_spot_answer_template, test_poi_answer_autofetch,
               test_smalltalk, test_question_shape,
               test_nearby_answer_template,
               test_no_destination_no_plan):
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
