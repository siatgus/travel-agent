"""route_engine 测试共享引导：src 布局、离线桩、check 断言。

所有 route_engine 测试文件 import 本模块完成环境准备：
- sys.path 注入 src；
- TRAVEL_ROUTE_OSRM=0（矩阵不申请 OSRM 配额）；
- real_tools._osrm_drive 打桩为 None（transit_estimate 走估算路径，
  全程零网络，耗时确定）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TA_ROOT = Path(__file__).resolve().parents[2]
if str(_TA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / "src"))

os.environ.setdefault("TRAVEL_ROUTE_OSRM", "0")

from travel_agent import real_tools          # noqa: E402

real_tools._osrm_drive = lambda a, b: None   # 离线：所有路网请求走估算降级

PASSED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    PASSED.append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"　{detail}" if detail and not ok else ""))
    if not ok:
        raise SystemExit(f"用例失败：{name} {detail}")


def finish(suite: str) -> None:
    print(f"\n{len(PASSED)} 条 {suite} 断言通过")
