# -*- coding: utf-8 -*-
"""存量修复：把知识库里的粗坐标用地理编码校正成真实位置。

覆盖来源（2026-11-12 扩到 seed）：
- auto-fetch-llm：模型兜底坐标（2 位小数，误差约 1km）；
- seed：real_tools.py 的核实数据——人工整理也有错（邯郸市博物馆被标到
  东环路立交桥，用户实测截图），且播种幂等，改 real_tools 只救新装环境，
  存量库必须靠本脚本校正。
user-named（位置估算）不动——有专门的「位置待确认」契约，本来就不参与
连线；auto-fetch（OSM）坐标本身是真实抓取，不重查。

境内走天地图；境外天地图查不到（数据不覆盖），由 autofetch 内部退
Nominatim（OSM），而 OSM 主名是英文——境外城市先用模型把中文名批量
译成英文名再校正。

只修「POI 级搜索命中且离原坐标 ≤ 20km」的：修的是亚公里级误差，不是搬家；
超出视为撞名，保留原坐标并报告。种子名常带括号附注（「丛台公园（武灵
丛台）」），天地图未必认——按全名 → 剥括号 → 去通用后缀逐个试，但
**分店括注不剥**（「南翔馒头店（豫园店）」剥成「南翔馒头店」会命中别家
分店，实测偏 3.5km），geocoder 的模糊降级层也不走（把「陆家嘴滨江步道」
配到 9km 外的徐汇滨江，实测）——境内只认天地图 POI 级搜索。

用法：python scripts/fix_llm_poi_coords.py [--dry]
"""
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from travel_agent import autofetch, llm     # noqa: E402
from travel_agent.autofetch import _km      # noqa: E402

MAX_SHIFT_KM = 20.0
_SOURCES = ("auto-fetch-llm", "seed")

# 天地图搜不到全名时的名字变体：全名 → 剥括号 → 去类型后缀。
# 分店括注（内容以「店」结尾，如 豫园店/南京路店）绝不剥——剥了会命中
# 同名连锁的别家分店，比原来的近似坐标错得更远。
_SUFFIXES = ("历史文化街区", "风景区", "景区", "旅游区", "遗址公园", "公园",
             "博物馆", "博物院", "古城", "古镇", "大剧院")
_BRANCH = re.compile(r"[（(][^）)]*店[）)]\s*$")


def _name_variants(name: str) -> list[str]:
    out = [name]
    if not _BRANCH.search(name):
        bare = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
        if bare and bare != name and len(bare) >= 3:
            out.append(bare)
    for base in list(out):
        for suf in _SUFFIXES:
            if base.endswith(suf) and len(base) - len(suf) >= 3:
                out.append(base[:-len(suf)])
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _geocode_poi(name: str, city: str, center: list[float],
                 name_en: str | None) -> tuple[float, float] | None:
    """POI 级坐标：境内只走天地图搜索（绕开 geocoder 模糊降级层）；
    境外天地图不覆盖，走 tdt_geocode 的 Nominatim 兜底。"""
    if center and not autofetch._in_china(center):
        return autofetch.tdt_geocode(name, city, center, name_en=name_en)
    return autofetch._tdt_search_poi(name, center)


def main() -> int:
    dry = "--dry" in sys.argv
    db = sqlite3.connect(ROOT / "data" / ".kb.db")
    db.row_factory = sqlite3.Row
    marks = ",".join("?" * len(_SOURCES))
    rows = db.execute(
        f"SELECT poi_id, name, city, geo_lat, geo_lon, source FROM pois "
        f"WHERE source IN ({marks}) AND geo_lat IS NOT NULL "
        f"AND geo_lon IS NOT NULL ORDER BY city, name", _SOURCES).fetchall()
    print(f"待校正 {len(rows)} 条（{'/'.join(_SOURCES)}）")

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
        hit = None
        used = None
        variant = False
        for cand in _name_variants(r["name"]):
            hit = _geocode_poi(cand, r["city"], centers[r["city"]],
                               en_names.get(r["name"]))
            time.sleep(0.25)                   # 天地图地理编码有 QPS 限制
            if hit:
                used = cand
                variant = cand != r["name"]
                break
        if not hit:
            miss += 1
            print(f"  [未命中] {r['city']} {r['name']}")
            continue
        shift = _km(old[0], old[1], hit[0], hit[1])
        # 全名命中（天地图确认了这个确切名字）可到 20km；变体命中是泛化
        # 名字（剥括号/后缀），挪 1km 以上就有撞错实体的风险——「南京城墙
        # （中华门段）」剥成「南京城墙」会命中别的城墙段，宁可不改。
        if shift > (MAX_SHIFT_KM if not variant else 1.0):
            kept += 1
            print(f"  [超阈保留] {r['city']} {r['name']} 偏移 {shift:.1f}km"
                  f"（按「{used}」命中），疑似撞名不改")
            continue
        if shift < 0.05:                       # 已经够准，不写库
            kept += 1
            continue
        if not dry:
            db.execute("UPDATE pois SET geo_lat=?, geo_lon=? WHERE poi_id=?",
                       (round(hit[0], 4), round(hit[1], 4), r["poi_id"]))
        fixed += 1
        print(f"  [{'dry' if dry else '校正'}] {r['city']} {r['name']}"
              f"（按「{used}」命中） {old} -> "
              f"({hit[0]:.4f}, {hit[1]:.4f}) 偏移 {shift:.2f}km")
    if not dry:
        db.commit()
    print(f"\n完成：校正 {fixed} / 保留 {kept} / 未命中 {miss}，共 {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
