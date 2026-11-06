"""本地打包：把项目打成两个可直接分发的产物。

用法（项目根或本目录）：
    python scripts/package.py                 # 产出 dist/ 下两件
    python scripts/package.py --skip-pyz      # 只打 zip

产物：
1. dist/travel-agent-<日期>.zip   完整项目（src/ prompts/ scripts/ README）
   解压后双击 scripts/start_agent.bat（Windows）或运行
   ./scripts/start_agent.sh 即起服务。
2. dist/travel_agent.pyz          单文件可执行包（zipapp）
   运行 `python travel_agent.pyz` 即起服务，适合只发一个文件的场景。

两者都不含第三方依赖——纯标准库，目标机器只需 Python 3.10+。
tests/ 与 output/ 不打进包；data/（.app.db 账号库、.kb.db 知识库、
.llm_settings.json 含 API Key）一律排除——运行时数据不能分发，
知识库在目标机首次启动时自动播种重建。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]      # travel-agent/
SRC = ROOT / "src" / "travel_agent"
DIST = ROOT / "dist"

EXCLUDE_DIRS = {".cache", "__pycache__", ".git", "dist", "output", ".workbuddy",
                "data"}
EXCLUDE_FILES = {"package.py", ".kb.db", ".user_prefs.json", ".llm_settings.json",
                 ".app.db"}
# ^ 运行时数据不进包；.llm_settings.json 含明文 API Key，.app.db 是账号/积分库，均不能分发

# zip 里保留的顶层内容（测试也带上，方便对方自验）
TOP_INCLUDE = ["src", "prompts", "scripts", "tests", "README.md", "pyproject.toml"]


def _walk(base: Path):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.is_file() and p.name not in EXCLUDE_FILES and p.suffix != ".pyc":
            yield p


def build_zip() -> Path:
    DIST.mkdir(exist_ok=True)
    out = DIST / f"travel-agent-{date.today():%Y%m%d}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in TOP_INCLUDE:
            src = ROOT / name
            if not src.exists():
                continue
            if src.is_file():
                z.write(src, f"travel-agent/{name}")
            else:
                for p in _walk(src):
                    z.write(p, f"travel-agent/{p.relative_to(ROOT)}")
        # 可执行位：macOS/Linux 的启动脚本
        for info in z.infolist():
            if info.filename.endswith((".sh",)):
                info.external_attr = 0o755 << 16
    print(f"[zip] {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return out


def build_pyz() -> Path:
    """zipapp：src/travel_agent 平铺为 travel_agent/ 包，入口 server:main。"""
    out = DIST / "travel_agent.pyz"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(SRC.glob("*.py")):
            z.write(p, f"travel_agent/{p.name}")
        z.writestr("__main__.py",
                   "from travel_agent.server import main\nmain()\n")
        # README 方便拿到单文件的人知道怎么用
        readme = ROOT / "README.md"
        if readme.exists():
            z.writestr("README.md", readme.read_text(encoding="utf-8"))
    print(f"[pyz] {out}  ({out.stat().st_size / 1024:.0f} KB)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-pyz", action="store_true")
    a = ap.parse_args()
    build_zip()
    if not a.skip_pyz:
        build_pyz()
    print("\n部署方式：")
    print("  zip -> 解压后运行 scripts/start_agent.bat（Win）或 ./scripts/start_agent.sh（Mac/Linux）")
    print("  pyz -> python travel_agent.pyz（运行时数据落在工作目录，或用 TRAVEL_DATA_DIR 指定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
