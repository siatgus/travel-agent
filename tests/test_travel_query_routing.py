"""行程问句路由回归测试（2026-11-13）：修一个问题，不能引入另一个问题。

背景：路由层修过好几轮——美食拆两路（知识/找店）、周边问句独立分流、
意图细分 refine_intent——每轮都动过拦截顺序。这里把 11 条典型问句的
预期去向固化下来，谁再改路由，跑一遍就知道有没有把别的路撞断。

覆盖三层：
1. 意图规则层：classify / refine_intent / is_spot_qa 的归属
2. 会话层 turn：每条问句拿到什么（mode=answer 的问题 / mode=plan 的规划）
3. 关键场景：先出「成都三天怎么玩」的方案，再问「春熙路附近有什么好吃的」
   ——原行程必须原封不动，绝不能重新生成一份三日游。

数据自足：临时 KB 预置 宽窄巷子/锦里/春熙路 + 周边餐厅酒店（带坐标），
不依赖真实 .kb.db，不打在线补库；跑法与其余测试一致：python tests/test_travel_query_routing.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

_tmp = Path(tempfile.mkdtemp(prefix="travel_route_test_"))
os.environ["TRAVEL_KB_DB"] = str(_tmp / "kb.db")   # 必须在 import kb 之前设置

from travel_agent import intent, kb                               # noqa: E402
from travel_agent.dialogue import Session                         # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not cond else ""))


TODAY = date(2026, 11, 13)


def _setup_kb() -> None:
    """预置路由测试所需数据：锚点景点（带坐标）+ 周边餐厅酒店 + 基础种子。"""
    db = kb.get_kb()
    db.seed_if_empty()   # POIS_DB/HOTELS_DB/ROUTES_DB/FOOD_KB 全量入库
    pois = [
        {"name": "宽窄巷子", "category": "attraction", "area": "青羊区",
         "geo": [30.6630, 104.0530], "appeal": 4, "duration_min": 120,
         "cost": 0, "note": "路由测试数据"},
        {"name": "锦里", "category": "attraction", "area": "武侯区",
         "geo": [30.6450, 104.0460], "appeal": 4, "duration_min": 120,
         "cost": 0, "note": "路由测试数据"},
        {"name": "春熙路", "category": "attraction", "area": "锦江区",
         "geo": [30.6570, 104.0810], "appeal": 3, "duration_min": 90,
         "cost": 0, "note": "路由测试数据"},
        {"name": "锦官阁川菜馆", "category": "restaurant", "area": "锦江区",
         "geo": [30.6575, 104.0815], "cost": 88,
         "note": "人均 88 元（测试数据）"},
        {"name": "少城小吃店", "category": "restaurant", "area": "青羊区",
         "geo": [30.6635, 104.0535], "cost": 45,
         "note": "人均 45 元（测试数据）"},
    ]
    for p in pois:
        db.upsert_poi("成都", p, source="seed")
    hotels = [
        {"name": "锦江商务酒店", "area": "锦江区", "star": 4, "price": 480,
         "geo": [30.6568, 104.0808], "dist_to_core_km": 1.0,
         "note": "测试数据"},
        {"name": "少城精品酒店", "area": "青羊区", "star": 3, "price": 360,
         "geo": [30.6628, 104.0528], "dist_to_core_km": 1.2,
         "note": "测试数据"},
    ]
    for h in hotels:
        db.upsert_hotel("成都", h, source="seed")


def _session() -> Session:
    """先用一句带城市的问答把 last_city 立起来（等价于刚聊过成都）。

    不直接改 slots——槽位有默认键与合并逻辑，手改会在 plan_trip 处缺键。
    之后「宽窄巷子…」这类不带城市名的句子，城市解析走 last_city 兜底。
    """
    s = Session(today=TODAY, client=None)
    s.turn("成都有哪些值得去的景点")
    return s


def _plan_snapshot(s: Session) -> str:
    return json.dumps(s.last_plan, ensure_ascii=False, sort_keys=True,
                      default=str)


# ------------------------------------------------------------------ 意图规则层

def test_intent_rule_layer() -> None:
    """11 条典型问句在规则层的归属：修路由先看这一层有没有被撞歪。"""
    expect_refine = [
        ("成都有什么特色美食", "food_query"),
        ("成都火锅有什么特色", "food_query"),
        ("成都有哪些好吃的火锅店", "restaurant_search"),
        ("春熙路附近有什么川菜馆", "restaurant_search"),
        ("推荐几家成都酒店", "hotel_search"),
        ("成都有哪些值得去的景点", "attraction_search"),
        ("宽窄巷子是什么地方", "attraction_search"),
        ("从宽窄巷子怎么去锦里", "route_query"),
    ]
    for t, exp in expect_refine:
        check(f"refine[{t}]={exp}", intent.refine_intent(t, "成都") == exp,
              intent.refine_intent(t, "成都"))
    check("classify[成都三天怎么玩]=plan",
          intent.classify("成都三天怎么玩") == "plan",
          intent.classify("成都三天怎么玩"))
    check("classify[宽窄巷子是什么地方]走景点问答兜底",
          intent.is_spot_qa("宽窄巷子是什么地方"), "is_spot_qa=False")


def test_intent_negatives() -> None:
    """反例：改需求句式和签证类问句不能被各路拦截误伤。"""
    check("改口味不算美食问句", not intent.is_food_question("想吃辣的"))
    check("提住宿要求不算住宿问句", not intent.is_stay_question("酒店要五星的"))
    check("加景点指令不走周边分流",
          intent.is_spot_qa("把宽窄巷子加进必去") is False)
    check("签证问句不判规划", intent.classify("去日本旅游需要办签证吗") != "plan")
    check("第N天怎么玩不算规划（改行程）",
          intent.classify("第2天怎么玩") != "plan",
          intent.classify("第2天怎么玩"))


# ------------------------------------------------------------------ 会话层路由

def test_turn_routing() -> None:
    """每条问句在会话里拿到的东西：问题就直答，规划才出方案。"""
    # 特色美食 → 菜品知识，不是餐厅名单
    r = _session().turn("成都有什么特色美食")
    check("美食知识按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("美食知识答的是菜品（火锅）", "火锅" in r.render, r.render[:120])

    # 菜品特色 → 美食知识（曾落景点问答）
    r = _session().turn("成都火锅有什么特色")
    check("火锅特色按美食知识处理", r.mode == "answer", f"mode={r.mode}")
    check("火锅特色答的是火锅本身", "火锅" in r.render, r.render[:120])

    # 火锅店 → 真实餐厅 POI
    r = _session().turn("成都有哪些好吃的火锅店")
    check("火锅店按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("火锅店不触发重排",
          "出行概要" not in r.render and "行程日期" not in r.render,
          r.render[:120])

    # 片区+菜系 → 餐厅搜索
    r = _session().turn("春熙路附近有什么川菜馆")
    check("片区川菜馆按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("片区川菜馆报出库里的店", "锦官阁川菜馆" in r.render,
          r.render[:160])

    # 酒店 → 住宿问答（起价口径）
    r = _session().turn("推荐几家成都酒店")
    check("酒店问句按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("酒店问答报的是库里的店与价格",
          "锦江商务酒店" in r.render and "每晚" in r.render, r.render[:160])

    # 景点泛推荐 → 景点清单
    r = _session().turn("成都有哪些值得去的景点")
    check("景点泛推荐按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("景点问答提到具体景点",
          any(k in r.render for k in ("宽窄巷子", "锦里", "春熙路")),
          r.render[:120])

    # N天怎么玩 → 原规划流程（曾分类成 poi 走泛推荐）。缺日期先追问，
    # 补齐后出方案——追问本身也是规划流程的一部分，绝不是景点清单。
    s = _session()
    r = s.turn("成都三天怎么玩")
    check("三天怎么玩进规划流程（先追问）", r.mode == "clarify", f"mode={r.mode}")
    check("追问的是出行日期不是景点推荐",
          "什么时候出发" in r.render and "值得去的景点" not in r.render,
          r.render[:120])
    r = s.turn("11月14号出发，两个人")
    check("补齐后方案出来了", r.mode == "plan", f"mode={r.mode}")
    check("方案带逐日行程", "出行概要" in r.render or "行程日期" in r.render,
          r.render[:120])

    # 周边：锚点在库里才接
    r = _session().turn("宽窄巷子附近有什么好吃的")
    check("周边美食按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("周边美食报的是餐厅不是景点介绍",
          "少城小吃店" in r.render
          and "人均" in r.render, r.render[:160])

    r = _session().turn("宽窄巷子附近有什么酒店")
    check("周边酒店按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("周边酒店报的是库里的酒店", "少城精品酒店" in r.render,
          r.render[:160])

    # 景点问答（other 兜底：无模型也不能被追问「想去哪儿」）
    r = _session().turn("宽窄巷子是什么地方")
    check("景点定义问按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("景点问答介绍的是宽窄巷子", "宽窄巷子" in r.render, r.render[:120])

    # 两点路线 → 距离+耗时
    r = _session().turn("从宽窄巷子怎么去锦里")
    check("两点路线按问答处理", r.mode == "answer", f"mode={r.mode}")
    check("路线答的是距离与耗时",
          "公里" in r.render and "分钟" in r.render, r.render[:160])


# ------------------------------------------------------------------ 关键场景

def test_plan_preserved_after_food_query() -> None:
    """出方案后问周边美食：原行程一页不动，绝不能重新生成三日游。"""
    s = _session()
    r1 = s.turn("成都三天怎么玩")
    check("规划流程先追问日期", r1.mode == "clarify", f"mode={r1.mode}")
    r1 = s.turn("11月14号出发，两个人")
    check("先出三日游方案", r1.mode == "plan", f"mode={r1.mode}")
    snapshot = _plan_snapshot(s)
    check("方案快照非空", len(snapshot) > 100, f"len={len(snapshot)}")

    r2 = s.turn("春熙路附近有什么好吃的")
    check("后续问句按问答处理", r2.mode == "answer", f"mode={r2.mode}")
    check("回答的是周边餐厅", "锦官阁川菜馆" in r2.render, r2.render[:160])
    check("回答里没有新行程", "出行概要" not in r2.render
          and "行程日期" not in r2.render, r2.render[:160])
    check("原行程一个字节都没动", _plan_snapshot(s) == snapshot)

    # 出过方案后问住宿安排：答方案里的入住节点，不是全城酒店清单
    r3 = s.turn("之前安排的酒店在哪里")
    check("住宿归属问按问答处理", r3.mode == "answer", f"mode={r3.mode}")
    check("答的是方案里的入住节点",
          "这趟行程安排的住宿" in r3.render and "入住：" in r3.render,
          r3.render[:200])
    check("住宿问也没有动行程", _plan_snapshot(s) == snapshot)


def main() -> int:
    _setup_kb()
    test_intent_rule_layer()
    test_intent_negatives()
    test_turn_routing()
    test_plan_preserved_after_food_query()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 条路由回归断言通过")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL {name}　{detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
