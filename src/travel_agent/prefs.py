"""按用户隔离的偏好档案读写（硬约束偏好的持久化）。

为什么单独一个模块（2026-11-24）：
- 原 `dialogue.PreferenceMemory` 只有一个全局 JSON（data/.user_prefs.json），
  本机多账号会互相串味——A 说「不吃辣」，B 的下一份行程也被套上。
- 而且写侧从未接通：抽取层（agent.extract_slots / llm.coerce_slots）算出的
  hard_prefs 只活在当轮槽位里，进程重启就没了。plan_handler 里 `apply_to`
  只读不写，注释自承「memory 从未使用」。
- 存储复用账号库（auth.db_path()，即 .app.db）：与 conversations 同文件、
  同一套 sqlite 连接方式，零额外依赖，测试用 TRAVEL_APP_DB 一次重定向即隔离。

两个开关，默认都关闭 = 与旧行为逐字节一致：
- TRAVEL_PREFS_SCOPE=global|user   默认 global：沿用全局 JSON 档案；
                                   user：按 uid 读写 user_prefs 表（推荐）
- TRAVEL_PREFS_WRITE=0|1           默认 0：只读不写。无论聊天里自动记、还是
                                   面板手动改，关闭时一律不落盘（返回 False）

容量：列表键最多 LIM_PREFS 条、单条最长 LIM_ITEM_CHARS 字符，防脏输入把
档案撑大（与 R-01 容量治理同一口径：写入即有界，不做无上限追加）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from . import auth

# 允许入库的硬约束（与 llm.coerce_slots 的枚举、agent.extract_slots 的规则取并集）。
# 手工添加走白名单：任意外来文本进了 prompt 会影响抽取结果，不放开。
KNOWN_HARD_PREFS = (
    "不吃辣", "忌高海拔", "需无障碍", "带幼儿", "素食",
    "忌海鲜", "怕晒", "忌长时间步行", "需亲子设施", "需午休",
)

LIST_KEYS = ("hard_prefs", "soft_prefs", "avoid")   # 档案里按列表存的键
LIM_PREFS = 32                                      # 单个列表键的最大条数
LIM_ITEM_CHARS = 24                                 # 单条偏好最长字符数

_LOCK = threading.Lock()
_INITED: str | None = None      # 已建表过的库路径（TRAVEL_APP_DB 换库时重跑建表）

_SCOPE_ENV = "TRAVEL_PREFS_SCOPE"
_WRITE_ENV = "TRAVEL_PREFS_WRITE"


# ------------------------------------------------------------------ 开关

def scope() -> str:
    """偏好存储范围：user（按 uid 隔离）或 global（全局 JSON，旧行为）。"""
    return "user" if (os.environ.get(_SCOPE_ENV) or "global").strip().lower() == "user" \
        else "global"


def write_enabled() -> bool:
    """是否允许写档案。默认关闭——没显式打开就一个字节都不写。"""
    return (os.environ.get(_WRITE_ENV) or "0").strip() == "1"


def clip_item(value) -> str:
    """单条偏好的规整：去空白、压长度，非字符串一律丢弃（返回空串）。"""
    if not isinstance(value, str):
        return ""
    return value.strip().replace("\n", " ")[:LIM_ITEM_CHARS]


def _clean_list(items) -> list[str]:
    """白名单过滤 + 去重 + 保持原顺序 + 截断到 LIM_PREFS。"""
    out: list[str] = []
    if not isinstance(items, (list, tuple)):
        return out
    for it in items:
        v = clip_item(it)
        if v and v in KNOWN_HARD_PREFS and v not in out:
            out.append(v)
        if len(out) >= LIM_PREFS:
            break
    return out


# ------------------------------------------------------------------ 存储

def _conn() -> sqlite3.Connection:
    global _INITED
    db = str(auth.db_path())
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    if _INITED != db:          # 换库（测试重定向 / 部署切换）要重新建表
        with c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs(
              uid INTEGER NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL DEFAULT '[]',
              updated_at REAL NOT NULL,
              PRIMARY KEY(uid, key))""")
        _INITED = db
    return c


class PrefStore:
    """一个用户的偏好档案（sqlite 后端）。

    读永远回源：进程内不缓存，多进程/多窗口改完立刻可见；
    读失败（库坏了/只读）一律降级成空档案，不影响本轮对话。
    """

    def __init__(self, uid: int):
        self.uid = int(uid)

    # ---------------- 读

    def get(self, key: str, default=None):
        try:
            with _LOCK:
                c = _conn()
                try:
                    row = c.execute(
                        "SELECT value FROM user_prefs WHERE uid=? AND key=?",
                        (self.uid, key)).fetchone()
                finally:
                    c.close()
            if row is None:
                return default
            return json.loads(row["value"])
        except (sqlite3.Error, ValueError):
            return default

    def all(self) -> dict:
        out: dict = {}
        try:
            with _LOCK:
                c = _conn()
                try:
                    rows = c.execute(
                        "SELECT key, value FROM user_prefs WHERE uid=? ORDER BY key",
                        (self.uid,)).fetchall()
                finally:
                    c.close()
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except ValueError:
                    continue
        except sqlite3.Error:
            return {}
        return out

    # ---------------- 写
    # 闸门位置说明：TRAVEL_PREFS_WRITE 管的是「产品写入路径」——聊天自动记
    # （merge_list）与面板增删（merge_list / remove_item / clear）。
    # set() 是底层整键写入原语，不设闸门：它同时是测试/运维播种档案的入口，
    # 旧契约（PreferenceMemory.set 调用即落盘）依赖这一点。

    def set(self, key: str, value) -> bool:
        """整键覆盖（底层原语）。写成功返回 True，落盘失败返回 False。"""
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        try:
            with _LOCK:
                c = _conn()
                try:
                    with c:
                        c.execute(
                            "INSERT INTO user_prefs(uid,key,value,updated_at)"
                            " VALUES(?,?,?,?)"
                            " ON CONFLICT(uid,key) DO UPDATE SET"
                            " value=excluded.value, updated_at=excluded.updated_at",
                            (self.uid, key, payload, time.time()))
                finally:
                    c.close()
            return True
        except sqlite3.Error:
            return False

    def merge_list(self, key: str, items) -> bool:
        """列表键并集追加（去重、白名单、有界）。无新增不写库。"""
        if not write_enabled():
            return False
        add = _clean_list(items)
        if not add:
            return False
        cur = self.get(key) or []
        cur = [x for x in cur if isinstance(x, str)]
        merged = cur + [x for x in add if x not in cur]
        merged = merged[:LIM_PREFS]
        if merged == cur:
            return False
        return self.set(key, merged)

    def remove_item(self, key: str, item) -> bool:
        """列表键删一条。删掉了返回 True。"""
        if not write_enabled():
            return False
        v = clip_item(item)
        cur = self.get(key) or []
        if not isinstance(cur, list) or v not in cur:
            return False
        return self.set(key, [x for x in cur if x != v])

    def clear(self, key: str | None = None) -> bool:
        if not write_enabled():
            return False
        try:
            with _LOCK:
                c = _conn()
                try:
                    with c:
                        if key is None:
                            c.execute("DELETE FROM user_prefs WHERE uid=?", (self.uid,))
                        else:
                            c.execute("DELETE FROM user_prefs WHERE uid=? AND key=?",
                                      (self.uid, key))
                finally:
                    c.close()
            return True
        except sqlite3.Error:
            return False


# ------------------------------------------------------------------ 便捷函数

def get_store(uid: int) -> PrefStore:
    return PrefStore(uid)
