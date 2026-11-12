# -*- coding: utf-8 -*-
"""存量修复：把 auto-fetch-llm 来源 POI 的粗坐标（2 位小数，误差约 1km）
用地理编码校正成真实位置。

境内走天地图；境外天地图查不到（数据不覆盖），由 autofetch 内部退
Nominatim（OSM），而 OSM 主名是英文——所以境外城市先用模型把中文名
批量译成英文名再校正（2026-11-12 洛杉矶实测：模型近似坐标图钉偏约 1km）。

只修「geocode 命中且离原坐标 ≤ 20km」的：修的是亚公里级误差，不是搬家；
超出视为撞名，保留原坐标并报告。user-named（位置估算）不动——它们有
专门的「位置待确认」契约，本来就不参与连线。

用法：python scripts/fix_llm_poi_coords.py [--dry]
"""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from travel_agent import autofetch, llm     # noqa: E402
from travel_agent.autofetch import _km      # noqa: E402

MAX_SHIFT_KM = 20.0


def main() -> int:
    dry = "--dry" in sys.argv
    db = sqlite3.connect(ROOT / "data" / ".kb.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT poi_id, name, city, geo_lat, geo_lon FROM pois "
        "WHERE source='auto-fetch-llm' AND geo_lat IS NOT NULL "
        "AND geo_lon IS NOT NULL ORDER BY city, name").fetchall()
    print(f"待校正 {len(rows)} 条（auto-fetch-llm）")

    # 城市中心 + 境外城市的英文名对照表（一次批量译完，不再逐条调模型）
    centers: dict[str, list[float]] = {}
    en_names: dict[str, str] = {}
    for r in rows:
        if r["city"] in centers:
            continue
        centers[r["city"]] = autofetch._resolve_center(r["city"])
        c = centers[r["city"]]
        if c and not autofetch._in_china(c):
            try:
                s = llm.load_settings()
                if s.get("api_key"):
                    client = llm.Client(
                        api_key=s["api_key"], base_url=s.get("base_url") or
                        "https://api.openai.com/v1",
                        model=s.get("model") or "gpt-4o-mini",
                        timeout=30.0, max_attempts=2)
                    en_names.update(llm.poi_names_en(
                        client, r["city"],
                        [x["name"] for x in rows if x["city"] == r["city"]])[0])
                    print(f"  [译名] {r['city']}："
                          f"{len(en_names)} 条英文名（模型批量翻译）")
            except Exception as e:                # 译名失败只是降级，不阻断
                print(f"  [译名失败] {r['city']}：{type(e).__name__}: {str(e)[:80]}")

    fixed = kept = miss = 0
    for r in rows:
        if r["city"] not in centers or not centers[r["city"]]:
            continue
        if time.time() < autofetch._GEO_DOWN_UNTIL:
            print("  !! 天地图已熔断（连续异常/QPS 限制），等 10 分钟后重跑本脚本")
            break
        old = (r["geo_lat"], r["geo_lon"])
        hit = autofetch.tdt_geocode(r["name"], r["city"], centers[r["city"]],
                                    name_en=en_names.get(r["name"]))
        time.sleep(0.25)                       # 天地图地理编码有 QPS 限制
        if not hit:
            miss += 1
            print(f"  [未命中] {r['city']} {r['name']}")
            continue
        shift = _km(old[0], old[1], hit[0], hit[1])
        if shift > MAX_SHIFT_KM:
            kept += 1
            print(f"  [超阈保留] {r['city']} {r['name']} 偏移 {shift:.1f}km，"
                  f"疑似撞名不改")
            continue
        if shift < 0.05:                       # 已经够准，不写库
            kept += 1
            continue
        if not dry:
            db.execute("UPDATE pois SET geo_lat=?, geo_lon=? WHERE poi_id=?",
                       (round(hit[0], 4), round(hit[1], 4), r["poi_id"]))
        fixed += 1
        print(f"  [{'dry' if dry else '校正'}] {r['city']} {r['name']} {old} -> "
              f"({hit[0]:.4f}, {hit[1]:.4f}) 偏移 {shift:.2f}km")
    if not dry:
        db.commit()
    print(f"\n完成：校正 {fixed} / 保留 {kept} / 未命中 {miss}，共 {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
