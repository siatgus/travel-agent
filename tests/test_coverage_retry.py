"""补库失败后的重试契约测试（2026-11-16 北京实测回归）。

实测场景：用户说「11月18号去北京玩7天」，第一次在线补库遇到网络抖动
失败后，失败结果被三层缓存钉死——

    1. autofetch._CACHE   失败缓存 6 小时，force=True 也照样命中
    2. session._cov       会话级缓存 False，且随存档跨重启持久化
    3. 触发条件           只有「就排北京」口令才重试，用户原样重发
                          「11月18号去北京玩7天」四遍，四遍全被拒

修完后三条契约都必须兑现：
    契约A  点名坚持（口令或重发规划请求）必须真的重试，不吃失败缓存
    契约B  缓存的"没有数据"不许被永久信任：库里一旦有数据（别的会话
           补的/后台录的/重试成功的），下一轮立刻放行
    契约C  失败缓存只挡"不坚持"的重复轮次（防止每轮都等一次网络超时）

运行：python tests/test_coverage_retry.py    失败时退出码为 1。
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

from travel_agent import agent, autofetch, kb
from travel_agent.dialogue import Session

TODAY = date(2026, 10, 29)
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def _poi(name: str, lat: float = 35.75, lon: float = 114.30) -> dict:
    """一条能过 _valid_row 校验的景点行（坐标取鹤壁市区）。"""
    return {
        "name": name, "category": "attraction", "area": "鹤壁",
        "geo": [lat, lon], "appeal": 4, "duration_min": 90, "cost": 0,
        "open_hours": "全天", "open_days": "每天", "need_booking": False,
        "tags": ["attraction"], "intensity": "low",
        "indoor_alternative": "", "note": "测试数据",
    }


def _hotel(name: str) -> dict:
    return {
        "name": name, "area": "淇滨区", "star": 3, "price": 260,
        "geo": [35.75, 114.30], "cancel_free": True,
        "dist_to_core_km": 1.0, "rating": 4.4, "note": "测试数据",
    }


def test_force_bypasses_fail_cache() -> None:
    """契约A：force=True 必须推翻失败缓存，真重试一次。"""
    autofetch.reset_cache()
    calls = {"n": 0}

    def flaky(city, center):
        calls["n"] += 1
        raise ConnectionError("模拟网络抖动")

    orig_center = autofetch._resolve_center
    autofetch._resolve_center = lambda city: [35.75, 114.30]
    try:
        res1 = autofetch.ensure_city("缓存重试城", fetcher=flaky)
        check("首次失败如实报 failed",
              res1["status"] == "failed", str(res1))
        res2 = autofetch.ensure_city("缓存重试城")
        check("不坚持时吃失败缓存不再等网络",
              res2["status"] == "failed" and res2.get("cached") is True,
              str(res2))
        check("失败缓存确实没再调上游", calls["n"] == 1, f"calls={calls['n']}")

        # 数据源恢复了：点名坚持（force）必须真重试并成功入库
        def ok_fetch(city, center):
            return [_poi(f"{city}公园")], [_hotel(f"{city}宾馆")]

        res3 = autofetch.ensure_city("缓存重试城", fetcher=ok_fetch, force=True)
        check("force 推翻失败缓存真重试",
              res3["status"] == "fetched", str(res3))
        check("重试真的调了上游", calls["n"] == 1 and res3["pois"] == 1,
              f"calls={calls['n']} pois={res3.get('pois')}")
    finally:
        autofetch._resolve_center = orig_center
        autofetch.reset_cache()
        db = kb.get_kb()
        for p in db.list_pois("缓存重试城") or []:
            db.delete_poi(p["poi_id"])
        for h in db.list_hotels("缓存重试城") or []:
            db.delete_hotel(h["hotel_id"])


def test_coverage_trusts_true_not_false() -> None:
    """契约B：缓存只信"有"不信"没有"。"""
    autofetch.reset_cache()
    db = kb.get_kb()
    db.upsert_poi("鹤壁", _poi("覆盖契约测试景点"), source="manual")
    s = Session(today=TODAY)
    s._cov["鹤壁"] = False          # 模拟：本会话早些时候补库失败留下的缓存
    check("库里有数据时 False 缓存必须让路", s._coverage("鹤壁") is True)
    s2 = Session(today=TODAY)
    s2._cov["桃花源"] = True        # 缓存"有"仍然可以直接信
    check("缓存为真时不再重复查询", s2._coverage("桃花源") is True)
    db = kb.get_kb()
    for p in db.list_pois("鹤壁") or []:
        if p.get("name") == "覆盖契约测试景点":
            db.delete_poi(p["poi_id"])


def test_user_restated_request_retries() -> None:
    """契约A端到端：补库失败后用户原样重发规划请求，第二遍必须放行。"""
    autofetch.reset_cache()
    db = kb.get_kb()
    inserted: list[str] = []
    orig_fetch = autofetch.ensure_city_data

    def net_down(city, dtype, **kw):
        return {"status": "failed", "city": city, "pois": 0, "hotels": 0,
                "message": "模拟网络抖动"}

    autofetch.ensure_city_data = net_down
    try:
        s = Session(today=TODAY)
        r1 = s.turn("11月18号去鹤壁玩3天")
        check("第一次补库失败如实说没有数据",
              r1.mode == "clarify" and "没有景点和酒店数据" in (r1.render or ""),
              f"mode={r1.mode} render={(r1.render or '')[:120]}")

        # "别的会话"把鹤壁补进库了（或网络恢复后重试成功）
        for i in range(1, 4):
            pid = db.upsert_poi("鹤壁", _poi(f"鹤壁契约景点{i}",
                                            35.75 + i * 0.01, 114.30 + i * 0.01),
                                source="manual")
            inserted.append(pid)
        hid = db.upsert_hotel("鹤壁", _hotel("鹤壁契约宾馆"), source="manual")

        # 用户把同一句话再说一遍（不带任何口令）——必须重新检查库并放行
        autofetch.ensure_city_data = orig_fetch
        r2 = s.turn("11月18号去鹤壁玩3天")
        bad = ("没有景点和酒店数据" in (r2.render or "")
               or "没补到" in (r2.render or ""))
        check("重发规划请求不再被拒绝", not bad, (r2.render or "")[:160])
        check("重发后进入追问或出方案",
              r2.mode in ("clarify", "plan"), f"mode={r2.mode}")
        db.delete_hotel(hid)
    finally:
        autofetch.ensure_city_data = orig_fetch
        autofetch.reset_cache()
        for pid in inserted:
            db.delete_poi(pid)


def main() -> int:
    test_force_bypasses_fail_cache()
    test_coverage_trusts_true_not_false()
    test_user_restated_request_retries()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
