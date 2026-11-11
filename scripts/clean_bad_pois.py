"""清理知识库里的脏 POI：名字根本不是景点的记录。

历史事故：误解析碎片会被 autofetch 按"用户点名、位置估算"落库，
之后每一份新方案都会命中它——「达时间改为」被排成成都 Day2 的
240 分钟景点，还带预约与门票说明（用户实测）。写入侧已加
valid_poi_name 闸门，这个脚本负责清存量。

用法：
    python scripts/clean_bad_pois.py            # 只扫描，列出可疑记录
    python scripts/clean_bad_pois.py --apply    # 扫描并删除
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from travel_agent import agent, kb          # noqa: E402


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    db = kb.get_kb()
    if db is None:
        print("知识库不可用")
        return 1

    bad: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()

    def mark(pid: str, city: str, name: str, why: str) -> None:
        if pid in seen:
            return
        seen.add(pid)
        bad.append((pid, city, name, why))

    by_name: dict[str, list[tuple[str, dict]]] = {}
    for city in db.cities():
        for p in (db.list_pois(city) or []):
            name = (p.get("name") or "").strip()
            if not agent.valid_poi_name(name):
                mark(p["poi_id"], city, name, ",".join(p.get("tags") or []))
            by_name.setdefault(name, []).append((city, p))

    # 跨城误落库：同一个景点名出现在多个城市，其中至少一条是"用户点名/
    # 位置估算"兜底记录。实测：大理行程里排出「都江堰」Day2（成都景点），
    # 门票与位置估算假设全跟着出现，还让「信息齐备零假设」的契约测试挂掉。
    # 另一城有权威记录 → 兜底那条直接算脏；两边都是兜底 → 列出来人工确认
    # （都江堰在成都确实存在，只是库里没收录，成都没错、大理错）。
    def _uncertain(p: dict) -> bool:
        tags = p.get("tags") or []
        return ("用户点名" in tags or "位置估算" in tags
                or "位置按市区估算" in (p.get("note") or ""))

    for name, recs in by_name.items():
        if len(recs) < 2:
            continue
        solid = [c for c, p in recs if not _uncertain(p)]
        loose = [(c, p) for c, p in recs if _uncertain(p)]
        if not loose:
            continue
        if solid:
            for c, p in loose:
                mark(p["poi_id"], c, name,
                     f"跨城兜底（{'、'.join(sorted(set(solid)))} 已有权威记录）")
        else:
            for c, p in loose:
                if p["poi_id"] not in seen:
                    print(f"  [待确认] {p['poi_id']} | {c} | {name!r} | "
                          f"同名记录也在 {'、'.join(sorted({x for x, _ in loose if x != c}))}")

    if not bad:
        print("没有发现脏 POI")
        return 0

    print(f"可疑记录 {len(bad)} 条：")
    for pid, city, name, tags in bad:
        print(f"  {pid} | {city} | {name!r} | {tags}")

    if not apply:
        print("\n只扫描未删除。确认无误后加 --apply 执行删除。")
        return 0

    for pid, city, name, _ in bad:
        db.delete_poi(pid)
        print(f"已删除 {pid} {city} {name!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
