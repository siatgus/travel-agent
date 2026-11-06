"""「说话层」测试：模型是否真的参与、跑偏时是否安全回退。

背景（用户反馈）：后台能看到启用了模型、Key 也填了，但用起来所有回答
都像模板，token 用量也没动静——因为之前模型只负责抽槽位和重排，
真正说给用户听的每一句都是字符串拼的。

这里锁死：
1. 有模型时，天气/景点/班次/闲聊/追问都由模型组织语言；
2. 模型漏关键数字、或编出事实里没有的金额温度 → 一律回退模板；
3. 额度不足抛 QuotaError，不能被"降级"吞掉（那是拒绝服务，不是降级）；
4. 没配模型时行为与之前完全一致。
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
from travel_agent import llm
from travel_agent import real_tools
from travel_agent.dialogue import Session

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"　{detail}"))


def _fake_weather(city, start, end):
    d0 = date.fromisoformat(start)
    days = [{"date": (d0 + timedelta(days=i)).isoformat(), "low": 9, "high": 17,
             "rain_prob": 100, "desc": "有雨", "is_forecast": True} for i in range(3)]
    return {"ok": True, "city": city, "days": days, "note": "Open-Meteo 真实预报"}


def _patch_weather():
    old_get, old_geo = real_tools.get_weather, real_tools.resolve_geo
    real_tools.get_weather = _fake_weather
    real_tools.resolve_geo = lambda c: [36.6, 114.5] if c else None
    return old_get, old_geo


def _unpatch_weather(old):
    real_tools.get_weather, real_tools.resolve_geo = old


# ------------------------------------------------------------------ 事实校验

def test_numbers_faithful() -> None:
    facts = {"城市": "邯郸", "天气": [{"日期": "10月31日（周六）", "最低温": 9,
                                       "最高温": 17, "降水概率": 100}]}
    ok = llm._numbers_faithful("邯郸今天 9~17℃，降水概率 100%，带伞。", facts)
    check("事实内的数字可以通过校验", ok, "9~17/100 应放行")
    bad = llm._numbers_faithful("邯郸今天 25℃，降水概率 100%。", facts)
    check("编造的温度被拦下", not bad, "25 不在事实里")
    fake_price = llm._numbers_faithful("门票 128 元，玩 3 小时。", {"门票": 50})
    check("编造的金额被拦下", not fake_price, "128 不在事实里")
    small = llm._numbers_faithful("挑 3 个地方，玩 2 天。", {})
    check("叙述性的个位数放行", small, "3/2 是叙述不是数据")


def test_compose_reply_guards() -> None:
    facts = {"城市": "邯郸", "最低温": 9, "最高温": 17, "降水概率": 100}
    check("没有模型时返回 None（交给模板）",
          llm.compose_reply(None, "weather", facts) is None)
    good = llm.ScriptedClient(["邯郸今天 9~17℃，降水概率 100%，记得带伞。"])
    out = llm.compose_reply(good, "weather", facts, "邯郸天气",
                            must_keep=[9, 17, 100])
    check("模型输出合规时被采用", bool(out) and "9~17" in out, str(out))
    miss = llm.ScriptedClient(["邯郸今天要下雨，记得带伞。"])
    check("模型漏掉关键数字 → 回退",
          llm.compose_reply(miss, "weather", facts, "", must_keep=[9, 17, 100]) is None)
    boom = llm.ScriptedClient([llm.LLMError("HTTP 500")])
    check("调用失败 → 回退", llm.compose_reply(boom, "weather", facts) is None)
    fence = llm.ScriptedClient(["```text\n邯郸 9~17℃，降水 100%。\n```"])
    out2 = llm.compose_reply(fence, "weather", facts, "", must_keep=[9, 17])
    check("去掉代码围栏", bool(out2) and "```" not in out2, str(out2))


# ------------------------------------------------------------------ 各条回答

def test_weather_uses_llm() -> None:
    old = _patch_weather()
    try:
        client = llm.ScriptedClient(
            ["邯郸今天有雨，9~17℃，降水概率 100%。出门记得带伞，路面会滑。"])
        text = intent.weather_answer("邯郸", "邯郸现在的天气", client=client)
        check("天气回答由模型组织", "路面会滑" in text, text[:80])
        check("模型回答仍带数据来源", "Open-Meteo" in text, text[-60:])
        check("模型回答保留温度与概率", "9~17" in text and "100" in text)
        check("确实发生了模型调用", len(client.prompts) == 1, str(len(client.prompts)))
        # 「今天/明天/后天」必须由代码算好塞进事实，否则模型会自己推算，
        # 出现过「明天晴…后天11月3日」这种同一天两种叫法的自相矛盾
        check("事实里带了算好的相对日（今天/明天/后天）",
              "相对日" in client.prompts[0] and "今天" in client.prompts[0],
              client.prompts[0][:120])
    finally:
        _unpatch_weather(old)

    old = _patch_weather()
    try:
        bad = llm.ScriptedClient(["邯郸今天 25℃，很热，穿短袖。"])
        text2 = intent.weather_answer("邯郸", "邯郸天气", client=bad)
        check("模型编温度时回退模板（模板含真实温度区间）",
              "9~17" in text2 and "25" not in text2, text2[:80])
        failing = llm.ScriptedClient([llm.LLMError("网络错误")])
        text3 = intent.weather_answer("邯郸", "邯郸天气", client=failing)
        check("模型不可用时天气照常回答（模板）", "9~17" in text3, text3[:80])
    finally:
        _unpatch_weather(old)


def test_poi_and_transport_use_llm() -> None:
    client = llm.ScriptedClient(
        ["邯郸最值得去的是广府古城和娲皇宫，一个在永年、一个在涉县，"
         "前者门票 50 元、后者 68 元，都需要留出半天以上。"])
    text = intent.poi_answer("邯郸", client=client)
    check("景点回答由模型组织", "广府古城" in text and "值得去" in text, text[:80])
    check("景点回答仍带下一步引导", "告诉我天数" in text)

    tclient = llm.ScriptedClient(
        ["邯郸东到上海虹桥坐 G2811 最省事，二等座 656 元，六个多小时到。"])
    r = real_tools.search_transport("邯郸", "上海", date.today().isoformat(), "any", 1)
    if r.get("ok"):
        items = r.get("items") or []
        t = intent.transport_answer("邯郸到上海怎么走", "邯郸", "上海", client=tclient)
        check("交通回答由模型组织", "G2811" in t or "656" in t, t[:80])
        check("交通回答仍带下一步引导", "串进行程" in t)
    else:
        check("交通样例数据可用（跳过模型断言）", True, "库里无班次")


def test_smalltalk_uses_llm() -> None:
    client = llm.ScriptedClient(
        ["我在，随时能帮你查天气、看班次，或者直接排一版能走通的行程。"])
    text = intent.smalltalk_answer("你在吗", client=client)
    check("闲聊由模型回答", "随时能帮你" in text, text[:60])


def test_session_passes_client() -> None:
    """会话层必须把 client 传下去，否则配了模型也白搭。"""
    old = _patch_weather()
    try:
        client = llm.ScriptedClient(
            ["邯郸今天有雨，9~17℃，降水概率 100%。伞别忘了带。"])
        s = Session(client=client, today=date(2026, 10, 31))
        r = s.turn("询问邯郸现在的天气")
        check("会话里天气回答走模型", "伞别忘了带" in r.render, r.render[:60])
        check("会话级确有模型调用", len(client.prompts) == 1, str(len(client.prompts)))
        check("天气回答不改槽位", r.slots.get("destination") is None)
        check("天气回答不产出方案", r.plan is None)
    finally:
        _unpatch_weather(old)


def test_quota_error_not_swallowed() -> None:
    """额度不足要拒绝服务，不能被当成"模型不可用"悄悄降级。

    真实链路抛的是 auth.QuotaError（freeze 失败），server 靠它回 403；
    任何"宽泛的 except"把它吃掉，等于给欠费用户白送服务。
    """
    from travel_agent import auth
    client = llm.ScriptedClient([auth.QuotaError("insufficient", 0, "积分不足")])
    s = Session(client=client, today=date(2026, 10, 31))
    old = _patch_weather()
    try:
        raised = False
        try:
            s.turn("邯郸天气")
        except auth.QuotaError:
            raised = True
        check("额度不足抛 QuotaError 而非降级", raised)
    finally:
        _unpatch_weather(old)


def test_clarify_uses_llm_without_inventing() -> None:
    """追问也要像人话，但不能替用户编同行人数。"""
    client = llm.ScriptedClient(["大理打算待几天？周边游 4 天最舒服，赶一点 3 天也够。"])
    s = Session(client=client, today=date(2026, 10, 31))
    r = s.turn("我想下个月去大理")
    check("追问由模型组织", "4 天最舒服" in r.render, r.render[:60])
    check("追问仍是 clarify 且只问一个字段", r.mode == "clarify" and r.asked == ["days"],
          f"{r.mode}/{r.asked}")
    prompt = client.prompts[0] if client.prompts else ""
    check("不把默认人数「1 人」当已知信息喂给模型",
          "party_size" not in prompt and "一个人" not in prompt, prompt[:120])


def main() -> None:
    test_numbers_faithful()
    test_compose_reply_guards()
    test_weather_uses_llm()
    test_poi_and_transport_use_llm()
    test_smalltalk_uses_llm()
    test_session_passes_client()
    test_quota_error_not_swallowed()
    test_clarify_uses_llm_without_inventing()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 条说话层断言通过")
    if passed != len(RESULTS):
        sys.exit(1)


if __name__ == "__main__":
    main()
