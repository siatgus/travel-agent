# -*- coding: utf-8 -*-
"""存量行程卡片补图：旧代码生成的 plan_json 里 poi_links 无 img（或整卡缺失），
用当前 poi_links.fetch_one / attach 逻辑补全后回写 conversations.plan_json。

- img 为空的卡片：fetch_one 整条替换（url/site/snippet/img 一致地刷新）；
- poi_links 缺失或空的行程：按 days_plan 里的景点整体 attach；
- 只动 plan_json，不碰消息与账号数据；进程内缓存 24h 让重跑几乎零开销。
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
        changed = False
        if isinstance(links, dict) and links:
            for name, info in list(links.items()):
                if isinstance(info, dict) and not info.get("img"):
                    fresh = poi_links.fetch_one(name, str(dest or ""), timeout=8)
                    if fresh and fresh.get("img"):
                        links[name] = fresh
                        changed = True
                        print(f"[补图] {sid[:8]} {name} -> {fresh['img'][:60]}")
        else:
            # 整卡缺失：走 attach（内部按 days_plan 挑有坐标的景点并发补）
            poi_links.attach(plan, budget=30.0)
            new_links = plan.get("poi_links") or {}
            if new_links:
                changed = True
                print(f"[补卡] {sid[:8]} {dest}: {len(new_links)} 张卡片")
        if changed:
            con.execute("UPDATE conversations SET plan_json=? WHERE uid=? AND sid=?",
                        (json.dumps(plan, ensure_ascii=False), uid, sid))
            touched += 1
    con.commit()
    con.close()
    print(f"完成：更新 {touched}/{len(rows)} 条行程")


if __name__ == "__main__":
    main()
