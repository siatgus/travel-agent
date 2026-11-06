"""城市地理解析测试。

背景（用户实测反馈）：问「廊坊天气」被回「没找到这个地名，是不是打错了？」
——廊坊是河北地级市，本不该查不到。根因是城市表只有 71 个，而地理编码
兜底只吃拉丁输入，中文名一律落空。

这里锁死四件事：

1. 全国地级政区（city_geo.py）全部可解析，不再冤报"打错了"；
2. 同名城市不串省（马鞍山在安徽、榆林在陕西、台州在浙江）；
3. 未收录的中文地名绝不拿别的城市坐标顶替（宁缺勿错）；
4. 带「市/州」后缀的口语说法也认。
"""

from __future__ import annotations

import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import intent, real_tools  # noqa: E402
from travel_agent.city_geo import EXTRA_CITY_GEO  # noqa: E402

_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  |  {detail}" if detail and not ok else ""))
    if not ok:
        _FAILED.append(name)


def near(geo, lat: float, lon: float, tol: float = 0.6) -> bool:
    """坐标落在预期城市附近（tol 度约 60km，足够区分城市、又能容忍取点在市区边缘）。"""
    return bool(geo) and abs(geo[0] - lat) <= tol and abs(geo[1] - lon) <= tol


def test_table_complete() -> None:
    assert len(EXTRA_CITY_GEO) > 250, len(EXTRA_CITY_GEO)
    missing = [name for name in real_tools.CITY_GEO if not real_tools.resolve_geo(name)]
    check("城市表覆盖 ≥300 个地级政区", len(real_tools.CITY_GEO) >= 300,
          f"{len(real_tools.CITY_GEO)}")
    check("表内每个城市都能解析出坐标", not missing, f"missing={missing[:5]}")
    # 表内坐标必须在中国境内（防串到国外/误取机场）
    out = [n for n, g in real_tools.CITY_GEO.items()
           if not (15 <= g[0] <= 55 and 70 <= g[1] <= 140)]
    check("坐标都在中国境内", not out, f"out={out}")


def test_langfang() -> None:
    """用户报障原样复现：廊坊天气必须能答，且不许说"打错了"。"""
    geo = real_tools.resolve_geo("廊坊")
    check("廊坊能解析出坐标", bool(geo), str(geo))
    check("廊坊坐标在京津之间（39.5N/116.7E）", near(geo, 39.52, 116.71), str(geo))
    text = intent.weather_answer("廊坊", "廊坊现在天气怎么样")
    check("廊坊天气有真实温度", "℃" in text, text[:80])
    check("不再甩「是不是打错了」", "打错" not in text, text[:80])
    check("答的是廊坊而不是别的城市", "廊坊" in text, text[:60])


def test_same_name_cities() -> None:
    """同音/同名城市不能串省：这是"坐标错了还看不出来"的高发区。"""
    cases = [
        ("马鞍山", 31.67, 118.51, "安徽"),
        ("榆林", 38.29, 109.74, "陕西"),
        ("台州", 28.66, 121.43, "浙江"),
        ("泰州", 32.49, 119.91, "江苏"),
        ("宿州", 33.64, 116.98, "安徽"),
        ("延安", 36.60, 109.49, "陕西"),
        ("资阳", 30.12, 104.65, "四川"),
        ("伊犁", 43.98, 81.52, "新疆"),
    ]
    for name, lat, lon, prov in cases:
        geo = real_tools.resolve_geo(name)
        check(f"{name} 落在{prov}（{lat}/{lon}）", near(geo, lat, lon), str(geo))


def test_suffix_variants() -> None:
    check("「廊坊市」也认", near(real_tools.resolve_geo("廊坊市"), 39.52, 116.71))
    check("「延边州」也认", near(real_tools.resolve_geo("延边州"), 42.89, 129.50))
    check("「杭州市」不误剥成别的城市", near(real_tools.resolve_geo("杭州市"), 30.27, 120.16))


def test_no_guessing() -> None:
    """没收录的中文地名必须说不知道，绝不返回别的城市坐标。"""
    for name in ("义乌", "昆山", "腾冲", "都江堰"):
        check(f"{name}（县级市）不猜坐标", real_tools.resolve_geo(name) is None)
    text = intent.weather_answer("义乌", "义乌天气")
    check("未收录地名如实说明且给出替代", "没找到" in text and "金华" in text, text[:80])
    check("未收录地名不甩锅给用户打错", "打错" not in text, text[:60])
    vague = intent.weather_answer("随便说说", "")
    check("明显非地名走另一套措辞", "不太像城市名" in vague, vague[:60])


def test_latin_geocoding() -> None:
    """拉丁输入仍走地理编码：中文名不猜，英文/拼音照常查。"""
    calls: list[str] = []
    old = real_tools._http_json

    def fake(url: str):
        calls.append(url)
        return {"results": [{"latitude": 35.6895, "longitude": 139.6917}]}

    real_tools._http_json = fake
    real_tools._GEO_CACHE.pop("Tokyo", None)
    try:
        geo = real_tools.resolve_geo("Tokyo")
        check("英文城市名走地理编码", near(geo, 35.69, 139.69, 0.05), str(geo))
        real_tools._GEO_CACHE.pop("义乌", None)
        check("中文名不走地理编码", real_tools.resolve_geo("义乌") is None)
        check("中文名没有发出请求", not any("义乌" in u for u in calls), str(calls[:2]))
    finally:
        real_tools._http_json = old


def main() -> None:
    test_table_complete()
    test_langfang()
    test_same_name_cities()
    test_suffix_variants()
    test_no_guessing()
    test_latin_geocoding()
    print()
    if _FAILED:
        print(f"{len(_FAILED)} 条失败：", "、".join(_FAILED))
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
