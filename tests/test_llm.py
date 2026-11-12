"""LLM 接入层的契约测试：不联网、不需要 API Key。

这里不断言"模型答得好不好"（那需要真实调用，且每次结果不同），
只断言**模型出问题时系统的行为**——这才是接大模型时真正会翻车的地方：

- 输出被 ``` 围栏包住 / 带前后解释
- 数字被写成 "4天"、"4,500元"
- 编造不在白名单里的硬约束、不在候选集里的景点
- 一次输出不合规（必须重问而不是直接放弃）
- 两次都不合规 / 超时（必须降级，且不能抛异常）

运行：python test_llm.py    失败时退出码为 1。
"""

from __future__ import annotations

import sys
from datetime import date


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import llm
from travel_agent import agent

TODAY = date(2026, 10, 29)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"　{detail}" if detail and not cond else ""))


def good_slots_json(**over) -> str:
    payload = {
        "destination": "大理", "origin": "杭州",
        "date_range": {"start": "2026-11-08"},
        "days": 4, "party_size": 2, "budget_total": 6000,
        "budget_per_person": None, "hard_prefs": ["不吃辣"],
        "soft_prefs": ["洱海"], "must_visit": [], "pace": "relaxed",
        "uncertain_fields": [],
    }
    payload.update(over)
    import json
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------ 1. 解析容错


def test_fenced_json() -> None:
    client = llm.ScriptedClient([
        "好的，结果如下：\n```json\n" + good_slots_json() + "\n```\n希望有帮助"])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    check("围栏包裹 + 前后废话仍能解析", rec.ok and slots is not None,
          f"attempts={rec.attempts} degraded={rec.degraded}")


def test_numeric_coercion() -> None:
    client = llm.ScriptedClient([
        good_slots_json(days="4天", budget_total="4,500元", party_size="2人")])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = (rec.ok and slots["days"] == 4
          and slots["budget_total"] == 4500 and slots["party_size"] == 2)
    check('"4天"/"4,500元" 被清洗为整数', ok,
          f"days={slots and slots['days']} total={slots and slots['budget_total']}")


# ------------------------------------------------------------------ 2. 白名单


def test_avoid_coerced_and_wins_over_must() -> None:
    client = llm.ScriptedClient([
        good_slots_json(must_visit=["豫园"], avoid=["豫园", 123])])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = (rec.ok and slots["avoid"] == ["豫园"] and slots["must_visit"] == [])
    check("avoid 槽位入库；与 must_visit 冲突时排除赢",
          ok, f"must={slots and slots['must_visit']} avoid={slots and slots['avoid']}")


def test_unknown_hard_pref_dropped() -> None:
    client = llm.ScriptedClient([
        good_slots_json(hard_prefs=["不吃辣", "怕冷", "必须住民宿"])])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = (rec.ok and slots["hard_prefs"] == ["不吃辣"]
          and len([w for w in rec.warnings if "硬约束" in w]) == 2)
    check("白名单外的硬约束被丢弃而非透传", ok,
          f"hard_prefs={slots and slots['hard_prefs']} warnings={rec.warnings}")


def test_unknown_field_dropped() -> None:
    client = llm.ScriptedClient([good_slots_json(flight_number="MU5678")])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = rec.ok and "flight_number" not in slots
    check("未知字段不污染槽位", ok)


def test_pace_word_is_not_interest() -> None:
    """「轻松休闲」是节奏，不是兴趣。

    表单的「轻松慢逛」拼成「不想太赶，轻松休闲一点」，模型常把「休闲」抽进
    soft_prefs；任何城市的库里都没有叫"休闲"的景点，于是每份方案都多一句
    「你的兴趣『休闲』没有直接对应的景点」——用户只勾了节奏，却被通知兴趣没被满足。
    """
    client = llm.ScriptedClient([
        good_slots_json(soft_prefs=["休闲", "轻松休闲", "休闲一点", "摄影"],
                        pace="relaxed")])
    slots, rec = llm.extract_slots_llm(client, "不想太赶，轻松休闲一点", TODAY)
    ok = (rec.ok and slots["soft_prefs"] == ["摄影"]
          and len([w for w in rec.warnings if "节奏词" in w]) == 3)
    check("节奏词不进 soft_prefs，真兴趣照常保留", ok,
          f"soft_prefs={slots and slots['soft_prefs']} warnings={rec.warnings}")

    # 反向：带实体词的照旧当兴趣，别把「轻松徒步」误杀成节奏
    for w, expect in (("轻松徒步", False), ("摄影游", False),
                      ("大熊猫", False), ("慢逛", True), ("休闲游", True),
                      ("特种兵", True), ("", True)):
        if llm.is_pace_word(w) != expect:
            check(f"节奏词判定 {w!r}", False, f"got {llm.is_pace_word(w)}")
            return
    check("节奏词判定：只吃纯节奏串，不误伤实体兴趣", True)


# ------------------------------------------------------------------ 3. 重问


def test_repair_on_invalid_pace() -> None:
    client = llm.ScriptedClient([
        good_slots_json(pace="很轻松"),          # 第一次：枚举非法
        good_slots_json(pace="relaxed"),        # 回灌错误后：修正
    ])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = rec.ok and rec.attempts == 2 and slots["pace"] == "relaxed"
    check("非法枚举触发回灌重问并修正", ok, f"attempts={rec.attempts}")


def test_repair_hint_contains_error() -> None:
    client = llm.ScriptedClient([
        good_slots_json(days=99),
        good_slots_json(days=4)])
    llm.extract_slots_llm(client, "随便一句", TODAY)
    fed_back = len(client.prompts) == 2 and "超出合理区间" in client.prompts[1]
    check("重问时把具体错误回灌给模型", fed_back,
          f"prompts={len(client.prompts)}")


def test_past_date_rejected_then_fixed() -> None:
    client = llm.ScriptedClient([
        good_slots_json(date_range={"start": "2025-11-08"}),   # 过去日期
        good_slots_json(date_range={"start": "2026-11-08"}),
    ])
    slots, rec = llm.extract_slots_llm(client, "11月8号去大理", TODAY)
    ok = rec.ok and slots["date_range"]["start"] == "2026-11-08"
    check("过去日期被拒并重问修正", ok,
          f"start={slots and slots['date_range']}")


# ------------------------------------------------------------------ 4. 降级


def test_degrade_after_two_bad_outputs() -> None:
    client = llm.ScriptedClient([
        "抱歉，我无法理解这个请求。",
        "这不是 JSON"])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    ok = slots is None and rec.degraded and rec.attempts == 2
    check("两次坏输出 → 判定降级", ok, f"degraded={rec.degraded}")


def test_degrade_on_timeout() -> None:
    client = llm.ScriptedClient([llm.LLMError("网络错误: timed out"),
                                 llm.LLMError("网络错误: timed out")])
    slots, rec = llm.extract_slots_llm(client, "随便一句", TODAY)
    check("超时两次 → 判定降级且不抛异常", slots is None and rec.degraded)


def test_agent_falls_back_to_rules() -> None:
    client = llm.ScriptedClient(["不是 JSON", "还不是 JSON"])
    records: list = []
    slots = agent.extract_slots(
        "我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶，我老婆不吃辣",
        today=TODAY, client=client, records=records)
    ok = (slots["destination"] == "大理" and slots["days"] == 4
          and slots["_source"] == "rules" and slots.get("_degraded")
          and slots["party_size"] == 2)
    check("LLM 不可用时规则版接管，结果完整可用", ok,
          f"source={slots.get('_source')} dest={slots.get('destination')}")


# ------------------------------------------------------------------ 5. 重排约束


def test_rerank_hallucination_filtered() -> None:
    allowed = ["P1", "P2", "P3"]
    order, errors, warnings = llm.coerce_ranking(
        {"order": ["P3", "P9", "P3", "P1"]}, allowed)
    ok = (not errors and order[:2] == ["P3", "P1"]
          and "P2" in order and "P9" not in order
          and len([w for w in warnings if "幻觉" in w]) == 1)
    check("重排的幻觉 id 被丢弃、重复被去重、缺失被补齐", ok, f"order={order}")


def test_rerank_all_invalid() -> None:
    order, errors, _ = llm.coerce_ranking(["X1", "X2"], ["P1", "P2"])
    check("全部非法 id → 判定失败（触发重问/降级）",
          order is None and bool(errors))


def test_rerank_end_to_end_survives_failure() -> None:
    """重排彻底失败时，端到端仍必须产出方案——这是最容易被忽略的回归点。"""
    client = llm.ScriptedClient([
        llm.LLMError("网络错误: timed out"),
        llm.LLMError("网络错误: timed out"),
        llm.LLMError("网络错误: timed out"),
        llm.LLMError("网络错误: timed out"),
    ])
    slots = agent.extract_slots_rules(
        "我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶", TODAY)
    trace: list = []
    result = agent.plan_trip(slots, trace, client=client)
    rerank_note = result["decision_log"].get("rerank")
    ok = (len(result["days_plan"]) == 4
          and sum(len(d["nodes"]) for d in result["days_plan"]) > 0
          and rerank_note is not None and not rerank_note["ok"])
    check("重排失败后端到端仍出完整方案", ok, f"rerank={rerank_note}")


def test_rerank_bonus_does_not_break_geometry() -> None:
    """让模型把最远的点排第一，几何约束仍应拦住跨城折返。"""
    slots = agent.extract_slots_rules(
        "我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶", TODAY)
    trace: list = []
    base = agent.plan_trip(slots, trace)

    far = llm.ScriptedClient(['{"order": ["P5"]}'])   # 索道，最远且高海拔

    def reranker(cands, s):
        order, _ = llm.rerank_llm(far, cands, s)
        return order

    trace2: list = []
    bumped = agent.plan_trip(slots, trace2, client=None)
    from travel_agent.composer import compose
    enforced, _ = compose(slots, agent.dispatch("search_poi", {"city": "大理", "category": "attraction"}),
                          agent.dispatch("get_weather", {"city": "大理", "start_date": "2026-11-08", "days": 4}),
                          "09:00", reranker=reranker)
    hops = []
    for d in enforced:
        geos = [n["geo"] for n in d["nodes"] if n.get("geo") and n["type"] == "attraction"]
        for a, b in zip(geos, geos[1:]):
            hops.append(round(__import__("travel_agent.tools", fromlist=["haversine_km"]).haversine_km(a, b), 1))
    ok = all(h <= 15.0 for h in hops) and len(base["days_plan"]) == len(enforced)
    check("人为把最远点排第一，单日相邻景点仍 ≤15km", ok, f"hops={hops}")


# ------------------------------------------------------------------ 定向查景点


def test_lookup_poi_coerce() -> None:
    ok_json = ('{"found":true,"name":"上海迪士尼乐园","category":"attraction",'
               '"duration_min":480,"ticket":475,"lat":31.14,"lon":121.66,'
               '"open_hours":"08:30-20:30","note":"提前购票"}')
    row, rec = llm.lookup_poi(llm.ScriptedClient([ok_json]), "上海", "上海迪士尼")
    check("定向查景点：正常返回名称/坐标/时长/门票",
          rec.ok and row["name"] == "上海迪士尼乐园" and row["lat"] == 31.14
          and row["ticket"] == 475, f"{row}")

    row2, _ = llm.lookup_poi(llm.ScriptedClient(['{"found":false}']), "上海", "不存在")
    check("模型说没有这个景点 → None（不拿城市中心凑）", row2 is None, f"{row2}")

    bad = '{"found":true,"name":"乱来景点","lat":999,"lon":0}'
    row3, _ = llm.lookup_poi(llm.ScriptedClient([bad, bad]), "上海", "乱来景点")
    check("坐标越界的输出被丢弃", row3 is None, f"{row3}")


def test_unknown_spot_reply() -> None:
    """查不到的点名：模型兜底把话说得像人，输出含数字（脑补票价）整句弃用。"""
    good = ("这个名字我这边没查到可靠资料，不乱说票价。"
            "它听起来像个本地市集类的地方，你可以换个具体景点，"
            "或者说「我想去XX」我按名字排进行程。")
    out = llm.unknown_spot_reply(llm.ScriptedClient([good]), "邯郸", "某市集")
    check("unknown_spot_reply：干净输出原样返回",
          out is not None and "不乱说" in out, f"{out}")

    out2 = llm.unknown_spot_reply(
        llm.ScriptedClient(["门票大概80元，营业时间9点到17点"]), "邯郸", "某景点")
    check("unknown_spot_reply：输出含数字（脑补票价）→ 弃用回模板",
          out2 is None, f"{out2}")

    out3 = llm.unknown_spot_reply(
        llm.ScriptedClient(["这地方免费开放的，直接去就行"]), "邯郸", "某景点")
    check("unknown_spot_reply：断言「免费」也是编造 → 弃用", out3 is None, f"{out3}")

    check("unknown_spot_reply：无模型 → None",
          llm.unknown_spot_reply(None, "邯郸", "某景点") is None)

    bad = llm.ScriptedClient([llm.LLMError("网络错误")])
    check("unknown_spot_reply：模型异常 → None",
          llm.unknown_spot_reply(bad, "邯郸", "某景点") is None)


def test_plan_intro_guard() -> None:
    """方案导览段：模型写的开场必须忠实于事实——数字超纲整段弃用回模板。"""
    facts = {"目的地": "邯郸", "天数": 3, "出发地": "石家庄", "节奏": "适中",
             "每天": [{"Day": 1, "主题": "古城与碑刻", "主打": ["广府古城", "邯郸市博物馆"]},
                      {"Day": 2, "主题": "响堂山石窟", "主打": ["响堂山石窟"]},
                      {"Day": 3, "主题": "市区收尾", "主打": ["丛台公园"]}]}
    good = ("邯郸这趟三天，走的是「从古城看到石窟」的赵文化主线："
            "Day1 广府古城加博物馆打底，Day2 专程留给响堂山石窟，"
            "Day3 丛台公园收尾，节奏不紧不慢，适合不赶时间的周末游。")
    out = llm.compose_reply(llm.ScriptedClient([good]), "plan_intro", facts,
                            must_keep=["邯郸"])
    check("plan_intro：忠实输出原样返回", out is not None and "主线" in out,
          f"{out}")

    # 温度/日期/价格都不在事实里——写出来就是脑补，整段弃用
    for bad_text, label in (
            ("邯郸 11 月 16 日出发正合适，白天 16℃ 挺舒服，三天玩得刚刚好。",
             "出现日期/温度 → 弃用"),
            ("邯郸人均消费 200 元出头，三天吃住行都能安排得很体面。",
             "出现价格 → 弃用")):
        out2 = llm.compose_reply(llm.ScriptedClient([bad_text]), "plan_intro",
                                 facts, must_keep=["邯郸"])
        check(f"plan_intro：{label}", out2 is None, f"{out2}")

    out3 = llm.compose_reply(llm.ScriptedClient([llm.LLMError("超时")]),
                             "plan_intro", facts, must_keep=["邯郸"])
    check("plan_intro：模型异常 → None 回退模板", out3 is None)
    out4 = llm.compose_reply(None, "plan_intro", facts)
    check("plan_intro：无模型 → None 回退模板", out4 is None)


# ------------------------------------------------------------------ 汇总

def test_history_scope() -> None:
    """历史只喂说话层/闲聊层：分类与槽位抽取必须单句（否则上一轮意图会污染判断）。"""
    turns = [{"role": "user", "content": "我想去大理"},
             {"role": "assistant", "content": "大理可以安排几天？"}]
    c = llm.ScriptedClient(['{"intent":"poi"}', "大理古城和洱海都值得去"])
    llm.attach_history(c, turns)
    llm.classify_intent(c, "那边有什么好玩的")
    check("分类调用不带历史", c.histories[-1] == [], f"{c.histories[-1]}")
    out = llm.compose_reply(c, "poi", {"城市": "大理"}, "那边有什么好玩的")
    check("说话层带历史", len(c.histories[-1]) == 2, f"{c.histories[-1]}")
    check("说话层正常返回", out is not None and "大理" in out, f"{out}")

    # 槽位抽取：json_task 内部多次调用，全都不许带历史
    c2 = llm.ScriptedClient(["{}", "{}", "{}", "{}"])
    llm.attach_history(c2, turns)
    llm.extract_slots_llm(c2, "大理有什么好玩的", TODAY)
    check("槽位抽取不带历史", all(h == [] for h in c2.histories),
          f"{c2.histories}")

    # 闲聊层也带历史，且没有历史时退化成单句（不能报错）
    c3 = llm.ScriptedClient(["我在的，你想问哪儿的行程？"])
    llm.attach_history(c3, turns)
    out3 = llm.chat_reply(c3, "你还在吗")
    check("闲聊层带历史", len(c3.histories[-1]) == 2 and out3 is not None,
          f"{c3.histories}")
    c4 = llm.ScriptedClient(["我在的"])
    out4 = llm.chat_reply(c4, "你还在吗")
    check("无历史时闲聊照常单句", out4 == "我在的" and c4.histories[-1] == [],
          f"{c4.histories}")

    # 线上一定是 MeteredClient 包一层：历史要能透到内层，否则"加了上下文"
    # 只在测试里成立、线上每句都失忆
    class _FakeMeter:
        def freeze(self, *a, **k): pass
        def release(self, *a, **k): pass
        def settle(self, *a, **k): pass
        def tokens_to_credits(self, n): return n

    inner = llm.ScriptedClient(["大理古城和洱海都值得去"])
    mc = llm.MeteredClient(inner, 1, _FakeMeter())
    llm.attach_history(mc, turns)
    llm.compose_reply(mc, "poi", {"城市": "大理"}, "那边呢")
    check("计量客户端把历史透到内层", len(inner.histories[-1]) == 2,
          f"{inner.histories}")
    llm.classify_intent(mc, "那边有什么好玩的")
    check("计量客户端下分类仍不带历史", inner.histories[-1] == [],
          f"{inner.histories}")


def main() -> int:
    for fn in (test_fenced_json, test_numeric_coercion,
               test_avoid_coerced_and_wins_over_must,
               test_unknown_hard_pref_dropped, test_unknown_field_dropped,
               test_pace_word_is_not_interest,
               test_repair_on_invalid_pace, test_repair_hint_contains_error,
               test_past_date_rejected_then_fixed,
               test_degrade_after_two_bad_outputs, test_degrade_on_timeout,
               test_agent_falls_back_to_rules,
               test_rerank_hallucination_filtered, test_rerank_all_invalid,
               test_rerank_end_to_end_survives_failure,
               test_rerank_bonus_does_not_break_geometry,
               test_lookup_poi_coerce, test_unknown_spot_reply,
               test_plan_intro_guard, test_history_scope):
        fn()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条契约断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
