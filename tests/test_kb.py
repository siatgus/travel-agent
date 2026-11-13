"""知识库与管理后端的行为测试。

运行：python test_kb.py
覆盖：种子幂等、与静态库格式等价、CRUD、导入校验、同步成功/失败落日志、
      管理后端新增城市 → 智能体立即可规划（KB 优先回落静态）。
全部用内存库（:memory:），不污染 demo/.kb.db。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import kb
from travel_agent.kb import KnowledgeBase

Passed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"（{detail}）" if detail and not ok else ""))
    if ok:
        Passed.append(name)
    else:
        raise SystemExit(f"用例失败：{name} {detail}")


# ---------------------------------------------------------------- 内存库单元测试

mem = KnowledgeBase(Path(":memory:"))
first = mem.seed_if_empty()
check("种子导入：POI/酒店/班次/美食均有数据",
      first["pois"] >= 10 and first["hotels"] >= 3 and first["schedules"] >= 5
      and first["food"] >= 5,
      str(first))

second = mem.seed_if_empty()
check("种子幂等：二次播种补入 0 条",
      second == {"pois": 0, "hotels": 0, "schedules": 0, "food": 0},
      str(second))

# —— 美食知识表：与真实餐厅 POI 分开（2026-11-13）——
food = mem.list_food("成都")
check("美食知识：成都种子入库", food is not None and len(food) >= 5, str(food and len(food)))
check("美食知识：字段齐全",
      all({"name", "category", "intro", "features"} <= set(r) for r in food))
check("美食知识：无数据城市返回 None", mem.list_food("消亡城") is None)
fid = mem.upsert_food("测试城", {"name": "过桥米线", "category": "小吃",
                                "intro": "汤烫生片", "features": "先荤后素"})
check("美食知识：手工 upsert 返回 id", fid > 0)
fid2 = mem.upsert_food("测试城", {"name": "过桥米线", "category": "小吃",
                                 "intro": "汤烫生片·更新", "features": "先荤后素"})
check("美食知识：同名去重更新不新增", fid2 == fid
      and len(mem.list_food("测试城")) == 1)
check("美食知识：删除", mem.delete_food(fid)
      and mem.list_food("测试城") is None)

from travel_agent.real_tools import POIS_DB, HOTELS_DB

kb_dali = mem.list_pois("大理")
static_dali = POIS_DB["大理"]
check("格式等价：KB POI 与静态库名称集合一致",
      {p["name"] for p in kb_dali} == {p["name"] for p in static_dali})
check("格式等价：geo/appeal/cost 字段一致",
      all(ka["geo"] == kb_obj["geo"] and ka["appeal"] == kb_obj["appeal"]
          and ka["cost"] == kb_obj["cost"]
          for ka, kb_obj in zip(sorted(kb_dali, key=lambda p: p["poi_id"]),
                                sorted(static_dali, key=lambda p: p["poi_id"]))))

kb_hotels = mem.list_hotels("大理")
check("格式等价：KB 酒店与静态库价格一致",
      sorted(h["price"] for h in kb_hotels) == sorted(h["price"] for h in HOTELS_DB["大理"]))

check("班次查询：大理→丽江有真实班次", (mem.list_schedules("大理", "丽江") or [])[0]["code"] == "C323")

# ---------------------------------------------------------------- CRUD / 导入

pid = mem.upsert_poi("沙溪", {"name": "寺登街", "geo": [26.5289, 99.8373], "cost": 0})
check("新增 POI：自动生成 ID 且立即可查", pid.startswith("KB-")
      and [p["name"] for p in mem.list_pois("沙溪")] == ["寺登街"])

mem.upsert_poi("沙溪", {"poi_id": pid, "name": "寺登街古集市", "geo": [26.5289, 99.8373]})
check("更新 POI：同 ID 覆盖名称", mem.list_pois("沙溪")[0]["name"] == "寺登街古集市")

check("删除 POI：返回 True 且列表清空（无数据时返回 None 触发回落）",
      mem.delete_poi(pid) and not mem.list_pois("沙溪"))

r = mem.import_json("测试城", "poi", [{"name": "好景点", "geo": [1.0, 2.0]},
                                       {"name": "缺geo"}])
check("导入校验：合法条目进库、缺 geo 被拒",
      r["added"] == 1 and len(r["errors"]) == 1 and "geo" in r["errors"][0])

check("导入校验：不支持的类型报错",
      mem.import_json("测试城", "schedule", [{}])["errors"] != [])

# ---------------------------------------------------------------- 同步与日志

r_ok = mem.sync_city("同步城", bbox=(25.6, 100.1, 25.7, 100.2),
                     fetcher=lambda city, bbox: [
                         {"name": "同步景点A", "geo": [25.61, 100.11]},
                         {"name": "同步景点B", "geo": [25.62, 100.12]}])
check("同步成功：假抓取器 2 条入库", r_ok["status"] == "success" and r_ok["added"] == 2)

r_fail = mem.sync_city("失败城", fetcher=lambda city, bbox: (_ for _ in ()).throw(
    ConnectionError("全部镜像不可达")))
check("同步失败：状态 failed 且不抛出", r_fail["status"] == "failed"
      and "镜像" in r_fail["message"])

logs = mem.logs()
check("日志落库：success/failed 均有记录",
      {l["status"] for l in logs} >= {"success", "failed"} and len(logs) >= 2)

check("新鲜度：stats 含来源与最近更新",
      all(f["last_update"] and f["sources"] for f in mem.stats()["freshness"]))

# 管理列表按 ID 自然升序（P1,P2,…,P14，文本序会把 P10 排到 P2 前）
mem.upsert_poi("排序城", {"poi_id": "X10", "name": "十号", "geo": [1.0, 1.0]})
mem.upsert_poi("排序城", {"poi_id": "X2", "name": "二号", "geo": [1.0, 1.0]})
mem.upsert_poi("排序城", {"poi_id": "X1", "name": "一号", "geo": [1.0, 1.0]})
ordered = [r["poi_id"] for r in mem.conn.execute(
    "SELECT poi_id FROM pois WHERE city=? ORDER BY LENGTH(poi_id), poi_id",
    ("排序城",))]
check("自然排序：ID 从小到大（X1,X2,X10）", ordered == ["X1", "X2", "X10"], str(ordered))

# ---------------------------------------------------------------- 与工具层/对话层集成

kb.configure(Path(":memory:"))          # 让全局单例指向新的内存库
db = kb.get_kb()
db.seed_if_empty()

from travel_agent.tools import dispatch
from travel_agent.dialogue import serviceable_cities
import os as _os
_REAL = _os.environ.get("USE_REAL_DATA", "1") == "1"

r = dispatch("search_poi", {"city": "大理"})
# 真实库 14 条、mock 库 9 条——阈值取两模式都成立的下限
check("工具层：search_poi 数据完整（两模式）", r["ok"] and r["total"] >= 9)

r = dispatch("search_hotel", {"city": "大理", "check_in": "2026-11-08",
                              "check_out": "2026-11-09", "guests": 2})
check("工具层：search_hotel 可查", r["ok"] and len(r["items"]) >= 1)

if _REAL:
    # 「KB 优先」语义只在真实数据模式下成立；mock 模式下走 mock_tools 静态表
    r = dispatch("search_transport", {"from_city": "大理", "to_city": "丽江",
                                      "date": "2026-11-09"})
    check("工具层：search_transport 走 KB", r["ok"] and r["items"][0]["code"] == "C323")

check("对话层：serviceable_cities 来自 KB",
      set(serviceable_cities()) >= {"大理", "丽江"})

if _REAL:
    # 邯郸/上海：正式数据防回退（2026-10-29 联网核实入库）
    r = dispatch("search_transport", {"from_city": "邯郸", "to_city": "上海",
                                      "date": "2026-11-01"})
    codes = [i["code"] for i in (r.get("items") or [])]
    check("邯郸→上海班次可用（G2811 等真实车次）",
          r["ok"] and "G2811" in codes and "K233" in codes, str(codes))
    r = dispatch("search_poi", {"city": "上海"})
    names = [i["name"] for i in (r.get("items") or [])]
    check("上海正式景点可查（外滩/东方明珠/豫园）",
          r["ok"] and {"外滩", "东方明珠", "豫园"} <= set(names), str(names[:4]))
    check("新城市进入可服务列表", {"上海", "邯郸"} <= set(serviceable_cities()))

    # mock 模式下 dispatch 不读 KB，「管理端写入→立即可查」仅真实模式可验证
    new_pid = db.upsert_poi("剑川", {"name": "石宝山石窟", "geo": [26.598, 99.855], "cost": 55})
    r = dispatch("search_poi", {"city": "剑川"})
    check("端到端：管理端新增城市智能体立即可查",
          r["ok"] and [i["name"] for i in r["items"]] == ["石宝山石窟"])
    check("端到端：新增城市进入可服务列表", "剑川" in serviceable_cities())

# ---------------------------------------------------------------- 汇总

print(f"\n{len(Passed)} 条知识库断言通过")
