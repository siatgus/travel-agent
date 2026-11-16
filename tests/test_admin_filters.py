"""管理后台「条件查询」逻辑测试（Node 跑真实页面里的筛选函数）。

后端接口只负责给足数据，筛选条件全在前端：状态/档位/金额区间/日期段/
关键词/任务类型/来源……组合起来容易写错边界（比如金额按元还是按分、
空时间要不要通过日期条件）。这里从 admin.PAGE 抽出真实的筛选函数，
用桩 DOM 喂数据跑断言——测的是上线那份代码，不是复制品。

跑法：python tests/test_admin_filters.py（机器上没有 node 时自动跳过）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import re                                                        # noqa: E402

from travel_agent import admin                                    # noqa: E402


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("跳过：本机没有 node（条件查询逻辑测试需要 node 执行页面 JS）")
        return 0
    js = max(re.findall(r"<script>(.*?)</script>", admin.PAGE, re.S), key=len)
    tmp = Path(tempfile.mkdtemp(prefix="travel_admin_filter_"))
    page = tmp / "admin_page.js"
    page.write_text(js, encoding="utf-8")
    cases = Path(__file__).with_name("admin_filter_cases.js")
    r = subprocess.run([node, str(cases), str(page)],
                       capture_output=True, text=True, encoding="utf-8")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.startswith("[FAIL]") or "条条件查询逻辑断言通过" in line:
            print(line)
    if r.returncode != 0:
        print(out)
        print("条件查询逻辑断言失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
