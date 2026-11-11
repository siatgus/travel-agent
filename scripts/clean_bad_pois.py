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
    for city in db.cities():
        for p in (db.list_pois(city) or []):
            name = (p.get("name") or "").strip()
            if not agent.valid_poi_name(name):
                bad.append((p["poi_id"], city, name,
                            ",".join(p.get("tags") or [])))

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
