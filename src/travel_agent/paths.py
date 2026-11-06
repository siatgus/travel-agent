"""运行时数据目录的唯一定义点。

数据一律不放包目录里（src/ 是代码，data/ 是数据），也不散落在各模块：
- 环境变量 TRAVEL_DATA_DIR 可整体重定向（部署/测试）；
- 默认取项目根 data/（本文件位于 src/travel_agent/，parents[2] 即根）；
- 只读环境（.pyz 单文件包）目录建不出来 → 退到当前工作目录；
- 各数据文件仍保留原有的单项环境变量覆盖（TRAVEL_APP_DB 等），优先级最高。
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("TRAVEL_DATA_DIR")
    p = Path(env) if env else Path(__file__).resolve().parents[2] / "data"
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:                        # 只读目录（.pyz）→ 工作目录
        alt = Path.cwd()
        try:
            alt.mkdir(exist_ok=True)
        except OSError:
            pass
        return alt


def app_db() -> Path:
    """账号/积分/用量/对话存档库（auth + conversations 共用）。"""
    env = os.environ.get("TRAVEL_APP_DB")
    return Path(env) if env else data_dir() / ".app.db"


def kb_db() -> Path:
    """知识库（城市/POI/酒店/班次）。"""
    env = os.environ.get("TRAVEL_KB_DB")
    return Path(env) if env else data_dir() / ".kb.db"


def llm_settings() -> Path:
    """模型配置文件（管理后台「模型设置」写入）。"""
    env = os.environ.get("TRAVEL_LLM_SETTINGS")
    return Path(env) if env else data_dir() / ".llm_settings.json"


def user_prefs() -> Path:
    """跨会话硬约束偏好档案（PreferenceMemory）。"""
    env = os.environ.get("TRAVEL_USER_PREFS")
    return Path(env) if env else data_dir() / ".user_prefs.json"


def cache_dir() -> Path:
    """天气/路网缓存目录。"""
    d = data_dir() / ".cache"
    try:
        d.mkdir(exist_ok=True)
    except OSError:
        pass
    return d
