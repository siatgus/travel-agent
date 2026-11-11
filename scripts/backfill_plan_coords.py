# -*- coding: utf-8 -*-
"""存量方案坐标回填：plan_json 里写死的粗坐标（模型兜底 2 位小数）
按 KB 最新坐标（已经 fix_llm_poi_coords.py 校正）替换，让老方案的
地图动线也用真实位置。只动 geo 字段，交通文案不动——距离偏差本来就
在亚公里级，重新生成行程才是彻底干净的。

用法：python scripts/backfill_plan_coords.py [--dry]
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from travel_agent import kb            # noqa: E402

MIN_SHIFT = 0.0005                     # ~50m 内视为没变，不写


def main() -> int:
    dry = "--dry" in sys.argv
    g = kb.get_kb()
    db = sqlite3.connect(ROOT / "data" / ".app.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT uid, sid, plan_json FROM conversations "
        "WHERE has_plan=1 AND plan_json IS NOT NULL").fetchall()
    touched = nodes_fixed = 0
    for r in rows:
        try:
            plan = json.loads(r["plan_json"])
        except Exception:
            continue
        city = (plan.get("slots") or {}).get("destination")
        if not city:
            continue
        known = {}
        for p in (g.list_pois(city) or []):
            geo = p.get("geo")
            if geo and len(geo) == 2 and p.get("name"):
                known[p["name"]] = (float(geo[0]), float(geo[1]))
        changed = False
        for d in plan.get("days_plan") or []:
            for n in d.get("nodes") or []:
                geo = n.get("geo")
                if not geo or n.get("geo_uncertain"):
                    continue
                new = known.get(n.get("name") or "")
                if not new:
                    continue
                if (abs(new[0] - geo[0]) > MIN_SHIFT
                        or abs(new[1] - geo[1]) > MIN_SHIFT):
                    n["geo"] = [round(new[0], 4), round(new[1], 4)]
                    changed = True
                    nodes_fixed += 1
        if changed and not dry:
            db.execute("UPDATE conversations SET plan_json=? WHERE uid=? AND sid=?",
                       (json.dumps(plan, ensure_ascii=False), r["uid"], r["sid"]))
        if changed:
            touched += 1
            print(f"  [{'dry' if dry else '回填'}] {city} sid={r['sid'][:16]}…")
    if not dry:
        db.commit()
    print(f"完成：{touched} 份方案 / {nodes_fixed} 个节点坐标回填"
          f"{'（dry run，未写库）' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
