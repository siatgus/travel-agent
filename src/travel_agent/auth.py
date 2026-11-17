"""账号层：手机号登录、JWT、LLM 用量记录。

设计要点：
1. **验证码登录即注册**：首次登录自动建号，无需单独注册流程——
   国内主流产品的默认心智，也少一套密码存储的风险面。
2. **短信后端可插拔**：默认 mock（验证码直接返回给调用方，供本地/测试），
   生产接短信服务商时实现 send_sms() 换掉即可，上层流程不动。
3. **JWT 用标准库手写 HS256**：不引入 PyJWT，算法只有一种，签名常量时间比较。
4. **用量记录只追加**：每次 LLM 调用按实际 token 写一条 usage，供后台统计
   成本与排障。本服务免费开放，无积分/额度/账单概念（原计费层已移除）。

环境变量：
    TRAVEL_APP_DB   账号库路径（默认 data/.app.db；测试重定向到临时目录）
    TRAVEL_SMS_MOCK 1=验证码直接返回（默认），0=走真实短信（需自行实现 send_sms）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------ 常量

PHONE_RE = re.compile(r"^1\d{10}$")

ACCESS_TTL = 1800            # access token 30 分钟
REFRESH_TTL = 30 * 86400     # refresh token 30 天
CODE_TTL = 300               # 验证码 5 分钟


def mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[7:] if len(phone) == 11 else phone


# ------------------------------------------------------------------ 存储

_DB_LOCK = threading.Lock()


def db_path() -> Path:
    """账号库路径：TRAVEL_APP_DB > data/.app.db（见 paths.py）。"""
    from . import paths
    return paths.app_db()


_INITED = False


def _conn() -> sqlite3.Connection:
    global _INITED
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    if not _INITED:
        with c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS sms_codes(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT NOT NULL,
              code TEXT NOT NULL,
              expires_at REAL NOT NULL,
              used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS refresh_tokens(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              token_hash TEXT UNIQUE NOT NULL,
              expires_at REAL NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS usage(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              model TEXT, task TEXT,
              prompt_tokens INTEGER, completion_tokens INTEGER,
              credits INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(user_id, created_at);
            """)
            # 老库兼容：历史上 users 表带过 is_unlimited/plan/plan_expires 列、
            # 且存在 credits/ledger 积分账本表。计费层移除后不再建也不再读，
            # 老库里的残留列与表对运行无影响，无需迁移（SQLite 删列成本高）。
        _INITED = True
    return c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ JWT（HS256，标准库手写）

def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret() -> bytes:
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='jwt_secret'").fetchone()
        if row:
            return row["value"].encode()
        s = secrets.token_hex(32)
        c.execute("INSERT OR REPLACE INTO meta VALUES('jwt_secret',?)", (s,))
        return s.encode()


def issue_access(uid: int, phone: str, ttl: int = ACCESS_TTL) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"uid": uid, "phone": phone,
                                  "exp": int(time.time()) + ttl}).encode())
    sig = hmac.new(_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def verify_access(token: str) -> dict | None:
    """合法且未过期返回 {uid, phone}，否则 None。"""
    try:
        h, p, s = token.split(".")
        want = hmac.new(_secret(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(s, _b64url(want)):
            return None
        payload = json.loads(_b64url_dec(p))
        if payload.get("exp", 0) < time.time():
            return None
        return {"uid": int(payload["uid"]), "phone": str(payload["phone"])}
    except (ValueError, KeyError, TypeError):
        return None


# ------------------------------------------------------------------ 短信验证码

def send_sms(phone: str, code: str) -> None:
    """真实短信通道：接服务商时实现这里（阿里云/腾讯云 SMS），抛异常=发送失败。"""
    raise RuntimeError("真实短信通道未配置（当前为演示模式）")


def send_code(phone: str) -> dict:
    """发验证码。返回 {ok, expires_in, dev_code?}——dev_code 仅在 mock 模式返回。"""
    if not PHONE_RE.fullmatch(phone or ""):
        return {"ok": False, "error": "手机号格式不正确"}
    code = f"{secrets.randbelow(1000000):06d}"
    mock = os.environ.get("TRAVEL_SMS_MOCK", "1") != "0"
    if not mock:
        try:
            send_sms(phone, code)
        except Exception as e:
            return {"ok": False, "error": f"短信发送失败：{e}"}
    with _DB_LOCK, _conn() as c:
        c.execute("INSERT INTO sms_codes(phone, code, expires_at) VALUES(?,?,?)",
                  (phone, code, time.time() + CODE_TTL))
        c.execute("DELETE FROM sms_codes WHERE expires_at < ?", (time.time() - 3600,))
    out = {"ok": True, "expires_in": CODE_TTL}
    if mock:
        out["dev_code"] = code          # 演示/测试模式：验证码直接回显
    return out


def _check_code(c, phone: str, code: str) -> bool:
    row = c.execute(
        "SELECT id FROM sms_codes WHERE phone=? AND code=? AND used=0 AND expires_at>=?"
        " ORDER BY id DESC LIMIT 1",
        (phone, str(code).strip(), time.time())).fetchone()
    if not row:
        return False
    c.execute("UPDATE sms_codes SET used=1 WHERE id=?", (row["id"],))
    return True


# ------------------------------------------------------------------ 用户

def _issue_refresh(c, uid: int) -> str:
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO refresh_tokens(user_id, token_hash, expires_at) VALUES(?,?,?)",
              (uid, hashlib.sha256(token.encode()).hexdigest(),
               time.time() + REFRESH_TTL))
    return token


def login(phone: str, code: str) -> dict:
    """验证码登录，首次自动注册。"""
    if not PHONE_RE.fullmatch(phone or ""):
        return {"ok": False, "error": "手机号格式不正确"}
    _secret()          # 预热（可能建表写 secret），避免与下方事务争写锁
    with _DB_LOCK, _conn() as c:
        if not _check_code(c, phone, code):
            return {"ok": False, "error": "验证码错误或已过期"}
        row = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
        new_user = row is None
        if new_user:
            cur = c.execute("INSERT INTO users(phone, created_at) VALUES(?,?)",
                            (phone, _now()))
            uid = cur.lastrowid
        else:
            uid = row["id"]
        access = issue_access(uid, phone)
        refresh = _issue_refresh(c, uid)
        return {"ok": True, "access": access, "refresh": refresh,
                "expires_in": ACCESS_TTL, "user_id": uid,
                "phone": mask_phone(phone), "new_user": new_user}


def refresh_access(token: str) -> dict:
    th = hashlib.sha256(str(token or "").encode()).hexdigest()
    with _DB_LOCK, _conn() as c:
        row = c.execute("SELECT * FROM refresh_tokens WHERE token_hash=?", (th,)).fetchone()
        if not row or row["revoked"] or row["expires_at"] < time.time():
            return {"ok": False, "error": "登录已过期，请重新登录"}
        u = c.execute("SELECT phone FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not u:
            return {"ok": False, "error": "用户不存在"}
        return {"ok": True, "access": issue_access(row["user_id"], u["phone"]),
                "expires_in": ACCESS_TTL}


def revoke_refresh(token: str) -> None:
    th = hashlib.sha256(str(token or "").encode()).hexdigest()
    with _DB_LOCK, _conn() as c:
        c.execute("UPDATE refresh_tokens SET revoked=1 WHERE token_hash=?", (th,))


# ------------------------------------------------------------------ LLM 用量记录

def record_usage(uid: int, *, model: str = "", task: str = "",
                 prompt_tokens: int | None = None,
                 completion_tokens: int | None = None) -> None:
    """记录一次 LLM 调用的实际 token 消耗（后台成本统计用）。失败只降级。"""
    try:
        with _conn() as c:
            c.execute("INSERT INTO usage(user_id, model, task, prompt_tokens,"
                      " completion_tokens, credits, created_at) VALUES(?,?,?,?,?,0,?)",
                      (uid, model, task, prompt_tokens, completion_tokens, _now()))
    except Exception:
        pass


def list_usage(uid: int | None = None, limit: int = 50) -> list[dict]:
    with _conn() as c:
        if uid:
            rows = c.execute("SELECT * FROM usage WHERE user_id=? ORDER BY id DESC LIMIT ?",
                             (uid, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def list_users(limit: int = 200) -> list[dict]:
    """用户列表（后台用户管理）。used 为累计 LLM 调用 token 消耗（千为单位）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT u.id, u.phone, u.created_at, u.status,"
            " COALESCE((SELECT SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0))"
            "   FROM usage WHERE user_id=u.id),0) AS tokens"
            " FROM users u ORDER BY u.id LIMIT ?", (limit,)).fetchall()
        return [dict(r, phone=mask_phone(r["phone"])) for r in rows]
