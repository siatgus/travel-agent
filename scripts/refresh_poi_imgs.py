# -*- coding: utf-8 -*-
"""存量行程卡片刷新：poi_links 抓取逻辑升级后（图片来源把关），把
conversations.plan_json 里已存的卡片全部重抓一遍，清掉张冠李戴的图。

- 每张卡片无条件 fetch_one 重抓（进程内缓存空，全部走新逻辑）；
- 重抓失败的保留旧卡（宁旧勿缺）；
- 只动 plan_json，不碰消息与账号数据。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from travel_agent import poi_links  # noqa: E402

DB = Path(__file__).resolve().parents[1] / "data" / ".app.db"


def main() -> None:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT uid, sid, dest, plan_json FROM conversations WHERE has_plan=1"
    ).fetchall()
    touched = 0
    for uid, sid, dest, pj in rows:
        try:
            plan = json.loads(pj or "{}")
        except Exception:
            continue
        links = plan.get("poi_links")
        if not isinstance(links, dict) or not links:
            continue
        changed = False
        for name, info in list(links.items()):
            if not isinstance(info, dict):
                continue
            old_img = str(info.get("img") or "")
            fresh = poi_links.fetch_one(name, str(dest or ""), timeout=8)
            if not fresh:
                continue
            # 图变了（或原来没图现在有）才回写，减少无谓写库
            if str(fresh.get("img") or "") != old_img:
                links[name] = fresh
                changed = True
                print(f"[刷新] {sid[:8]} {name}\n"
                      f"    旧图: {old_img[:70]}\n"
                      f"    新图: {str(fresh.get('img'))[:70]}")
        if changed:
            con.execute("UPDATE conversations SET plan_json=? WHERE uid=? AND sid=?",
                        (json.dumps(plan, ensure_ascii=False), uid, sid))
            touched += 1
    con.commit()
    con.close()
    print(f"完成：更新 {touched}/{len(rows)} 条行程")


if __name__ == "__main__":
    main()
