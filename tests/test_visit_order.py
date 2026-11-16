"""「先去A再去B」顺序硬约束回归（设计文档 §9 preferred_order / §13.2 场景七）。

三层各断一段：
- 抽取层：「先去」进必去清单、先后关系存 visit_order（旧实现外滩直接丢）；
- 引擎层：optimize_order 带 preferred_names 时顺序绝不被优化破坏；
- 优化器层：输入序违约时先归位出可行基准，再谈"不劣于原序"。

跑法与其余测试一致：python tests/test_visit_order.py（全程离线）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

_tmp = Path(tempfile.mkdtemp(prefix="ta_order_test_"))
os.environ["TRAVEL_KB_DB"] = str(_tmp / "kb.db")
os.environ["TRAVEL_DATA_DIR"] = str(_tmp)
os.environ["TRAVEL_AUTOFETCH"] = "0"

from travel_agent import agent, kb                     # noqa: E402
from travel_agent.route_engine import (                # noqa: E402
    RouteNode, RouteOptimizer, DistanceMatrix, optimize_order,
)

PASSED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(f"用例失败：{name} {detail}")


def finish(suite: str) -> None:
    print(f"\n{len(PASSED)} 条 {suite} 断言通过")


def test_extract() -> None:
    """抽取层：「先去」不再丢点，顺序落进 visit_order。"""
    s = agent.extract_slots_rules("上海玩2天，先去外滩再去豫园", today=date(2026, 11, 16))
    check("抽取：先去的点进必去清单", "外滩" in s["must_visit"], str(s["must_visit"]))
    check("抽取：再去的点进必去清单", "豫园" in s["must_visit"], str(s["must_visit"]))
    check("抽取：先后关系存 visit_order",
          s["visit_order"] == ["外滩", "豫园"], str(s["visit_order"]))
    s2 = agent.extract_slots_rules(
        "成都3天，先去武侯祠，然后去锦里，最后去杜甫草堂", today=date(2026, 11, 16))
    check("抽取：三连顺序完整保留",
          s2["visit_order"] == ["武侯祠", "锦里", "杜甫草堂"], str(s2["visit_order"]))
    s3 = agent.extract_slots_rules("上海玩2天，不想去外滩", today=date(2026, 11, 16))
    check("抽取：否定句不产生顺序", s3["visit_order"] == []
          and "外滩" in s3["avoid"], str(s3["visit_order"]))


def test_engine_keeps_order() -> None:
    """引擎层：无论几何上多顺路，用户点名顺序不被打乱。"""
    HOME = [30.66, 104.06]
    def poi(pid, name, lon):
        return {"poi_id": pid, "name": name, "geo": [30.66, lon],
                "duration_min": 90, "open_hours": "全天"}
    # 故意把几何顺序排成 B 最近 → A 远 → C，让贪心想先去 B
    picked = [poi("P1", "甲景点", 104.20), poi("P2", "乙景点", 104.07),
              poi("P3", "丙景点", 104.21)]
    out, algo = optimize_order(picked, home_geo=HOME,
                               preferred_names=["乙景点", "丙景点", "甲景点"])
    check("引擎：点名顺序绝对保留（几何再顺也不换）",
          [p["name"] for p in out] == ["乙景点", "丙景点", "甲景点"]
          and algo is not None, str([p["name"] for p in out]))
    check("引擎：排列不变性", sorted(p["poi_id"] for p in out)
          == ["P1", "P2", "P3"])
    # 只点名一个点：不构成顺序约束，正常优化
    out2, _ = optimize_order(picked, home_geo=HOME, preferred_names=["乙景点"])
    check("引擎：单点顺序不成约束", sorted(p["name"] for p in out2)
          == ["丙景点", "乙景点", "甲景点"])
    # 名字对不上任何点：安全无操作
    out3, _ = optimize_order(picked, home_geo=HOME, preferred_names=["不存在的点"])
    check("引擎：顺序名匹配不上时不影响结果",
          sorted(p["name"] for p in out3) == ["丙景点", "乙景点", "甲景点"])


def test_optimizer_respects_order() -> None:
    """优化器层：违约输入序先归位成可行基准。"""
    a = RouteNode(id="A", name="A", latitude=30.66, longitude=104.20)
    b = RouteNode(id="B", name="B", latitude=30.66, longitude=104.07)
    c = RouteNode(id="C", name="C", latitude=30.66, longitude=104.21)
    for n, rank in ((b, 0), (c, 1), (a, 2)):     # 输入序 A,B,C 违约 B<C<A
        n.preferred_order = rank
    m = DistanceMatrix([a, b, c], [30.66, 104.06])
    opt = RouteOptimizer(m, 9 * 60, 20 * 60)
    best, algo = opt.optimize([a, b, c])
    check("优化器：违约输入先归位，结果满足硬顺序",
          [n.id for n in best] == ["B", "C", "A"], str([n.id for n in best]))
    # 与贪心并列时保留 input 解也合法：关键是顺序没有被优化破坏
    check("优化器：算法名合法",
          algo in ("input", "nearest_neighbor", "2-opt"), algo)


if __name__ == "__main__":
    kb.get_kb().seed_if_empty()
    test_extract()
    test_engine_keeps_order()
    test_optimizer_respects_order()
    finish("顺序硬约束")
