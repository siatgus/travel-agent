"""对话持久化：一次会话（sid）= 一条行程记录，含完整对话内容与最近方案。

产品语义（2026-11-02 需求）：
- 上下文范围收窄到「用户产生的一次对话」：一个 sid 对应一个 Session，互不串味；
- 登录用户能在「历史行程」里回看每次对话的完整内容，点开即续聊；
- 没有历史行程的用户按新对话开始，第一条消息发出即自动建档——之后每轮追加
  消息、出方案就存方案，全程无需手动保存。

存储：与账号库同一个 SQLite 文件（auth.db_path()，可用 TRAVEL_APP_DB 重定向），
零 pip 依赖；测试重定向一次就整体隔离。存档失败只降级不影响当轮对话。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from . import auth

_LOCK = threading.Lock()
_INITED = False


def _conn() -> sqlite3.Connection:
    global _INITED
    c = sqlite3.connect(auth.db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    if not _INITED:
        with c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS conversations(
              uid INTEGER NOT NULL,
              sid TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              dest TEXT NOT NULL DEFAULT '',
              has_plan INTEGER NOT NULL DEFAULT 0,
              plan_json TEXT NOT NULL DEFAULT '',
              plan_utterance TEXT NOT NULL DEFAULT '',
              state_json TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY(uid, sid));
            CREATE TABLE IF NOT EXISTS conv_messages(
              uid INTEGER NOT NULL,
              sid TEXT NOT NULL,
              seq INTEGER NOT NULL,
              role TEXT NOT NULL,
              content TEXT NOT NULL,
              created_at REAL NOT NULL,
              PRIMARY KEY(uid, sid, seq));
            """)
        _INITED = True
    return c


def _json(value, default=None) -> str:
    return json.dumps(value, ensure_ascii=False, default=default or str)


# ------------------------------------------------------------------ 写入

def record_turn(uid: int, sid: str, user_text: str, bot_text: str,
                dest: str = "", plan: dict | None = None,
                utterance: str = "", state: dict | None = None) -> None:
    """一轮对话落库：没有记录就自动建档（历史行程的源头），有就追加。

    - 标题：有目的地用「去X的旅行」，否则用首条消息截断；中途换目的地跟着改。
    - plan / state：整存整取，重启后按 state 续聊、按 plan 重渲染行程页。
    """
    now = time.time()
    with _LOCK, _conn() as c:
        row = c.execute("SELECT dest FROM conversations WHERE uid=? AND sid=?",
                        (uid, sid)).fetchone()
        if row is None:
            title = f"去{dest}的旅行" if dest else (user_text or "新的行程对话")[:24]
            c.execute(
                "INSERT INTO conversations(uid,sid,title,dest,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (uid, sid, title, dest or "", now, now))
        else:
            sets, args = ["updated_at=?"], [now]
            if dest and dest != row["dest"]:
                sets.append("dest=?")
                args.append(dest)
                sets.append("title=?")
                args.append(f"去{dest}的旅行")
            args += [uid, sid]
            c.execute(f"UPDATE conversations SET {','.join(sets)} "
                      "WHERE uid=? AND sid=?", args)

        seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM conv_messages "
                        "WHERE uid=? AND sid=?", (uid, sid)).fetchone()[0]
        c.execute("INSERT INTO conv_messages(uid,sid,seq,role,content,created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (uid, sid, seq, "user", user_text, now))
        if bot_text:
            c.execute("INSERT INTO conv_messages(uid,sid,seq,role,content,created_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (uid, sid, seq + 1, "bot", bot_text, now))

        if plan is not None:
            c.execute("UPDATE conversations SET has_plan=1, plan_json=?, "
                      "plan_utterance=? WHERE uid=? AND sid=?",
                      (_json(plan), utterance, uid, sid))
        if state is not None:
            c.execute("UPDATE conversations SET state_json=? WHERE uid=? AND sid=?",
                      (_json(state), uid, sid))


def delete_conversation(uid: int, sid: str) -> bool:
    """彻底删除一次对话（含消息与方案存档），返回是否真的删了东西。"""
    with _LOCK, _conn() as c:
        cur = c.execute("DELETE FROM conversations WHERE uid=? AND sid=?", (uid, sid))
        c.execute("DELETE FROM conv_messages WHERE uid=? AND sid=?", (uid, sid))
        return cur.rowcount > 0


# ------------------------------------------------------------------ 读取

def list_conversations(uid: int) -> list[dict]:
    """当前用户的历史行程，最近更新的在前。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT cv.sid, cv.title, cv.dest, cv.has_plan, cv.created_at,"
            " cv.updated_at,"
            " (SELECT COUNT(*) FROM conv_messages m"
            "   WHERE m.uid=cv.uid AND m.sid=cv.sid) AS msg_count"
            " FROM conversations cv WHERE cv.uid=?"
            " ORDER BY cv.updated_at DESC", (uid,)).fetchall()
    return [dict(r) for r in rows]


def get_conversation(uid: int, sid: str) -> dict | None:
    """一次对话的完整内容：元数据 + 逐条消息（按时间序）。"""
    with _conn() as c:
        row = c.execute("SELECT sid,title,dest,has_plan,created_at,updated_at"
                        " FROM conversations WHERE uid=? AND sid=?",
                        (uid, sid)).fetchone()
        if row is None:
            return None
        msgs = c.execute("SELECT role,content,created_at FROM conv_messages"
                         " WHERE uid=? AND sid=? ORDER BY seq",
                         (uid, sid)).fetchall()
    out = dict(row)
    out["has_plan"] = bool(out["has_plan"])
    out["messages"] = [dict(m) for m in msgs]
    return out


def get_state(uid: int, sid: str) -> dict | None:
    """续聊存档：Session.restore 的原料；没有存档返回 None（全新会话）。"""
    with _conn() as c:
        row = c.execute("SELECT state_json FROM conversations WHERE uid=? AND sid=?",
                        (uid, sid)).fetchone()
    if not row or not row["state_json"]:
        return None
    try:
        return json.loads(row["state_json"])
    except json.JSONDecodeError:
        return None


def get_plan_by_sid(sid: str) -> tuple[dict, str] | None:
    """行程页兜底：内存里没有（重启过）就从库里捞最近一次方案。

    按 sid 全局查（行程页不带 token，在新标签页直接打开）；sid 由前端生成
    的随机 UUID，碰撞概率可忽略，本地单机场景无越权风险。
    """
    with _conn() as c:
        row = c.execute("SELECT plan_json, plan_utterance FROM conversations"
                        " WHERE has_plan=1 AND sid=? ORDER BY updated_at DESC",
                        (sid,)).fetchone()
    if not row or not row["plan_json"]:
        return None
    try:
        return json.loads(row["plan_json"]), row["plan_utterance"]
    except json.JSONDecodeError:
        return None
