"""账号与计费层：手机号登录、JWT、积分账本（预扣-结算）。

设计要点：
1. **验证码登录即注册**：首次登录自动建号并赠送新户积分，无需单独注册流程——
   国内主流产品的默认心智，也少一套密码存储的风险面。
2. **短信后端可插拔**：默认 mock（验证码直接返回给调用方，供本地/测试），
   生产接短信服务商时实现 send_sms() 换掉即可，上层流程不动。
3. **积分账本只追加**：每次余额变动都留一条 ledger（赠送/冻结/结算/回补/调整），
   可对账、可审计。预扣-结算保证"LLM 调用失败不扣钱、扣钱不超实际用量太多"。
4. **JWT 用标准库手写 HS256**：不引入 PyJWT，算法只有一种，签名常量时间比较。

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
from datetime import date, datetime
from pathlib import Path

# ------------------------------------------------------------------ 常量

PHONE_RE = re.compile(r"^1\d{10}$")

BONUS_CREDITS = 500          # 新用户注册赠送
DAILY_CAP_CREDITS = 300      # 免费档每日消耗上限（约 3-6 次完整规划）
MIN_BALANCE = 40             # 低于此余额拒绝发起（一次规划的成本量级）
ACCESS_TTL = 1800            # access token 30 分钟
REFRESH_TTL = 30 * 86400     # refresh token 30 天
CODE_TTL = 300               # 验证码 5 分钟
TOKENS_PER_CREDIT = 100      # 1 积分 = 100 tokens（ DeepSeek 档系数为 1）


class QuotaError(Exception):
    """额度不足 / 触发每日上限。reason: insufficient | daily_cap"""

    def __init__(self, reason: str, balance: int, message: str):
        super().__init__(message)
        self.reason = reason
        self.balance = balance
        self.message = message


def tokens_to_credits(tokens: int) -> int:
    return max(1, (int(tokens) + TOKENS_PER_CREDIT - 1) // TOKENS_PER_CREDIT)


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
            CREATE TABLE IF NOT EXISTS credits(
              user_id INTEGER PRIMARY KEY REFERENCES users(id),
              balance INTEGER NOT NULL DEFAULT 0,
              frozen INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS ledger(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              kind TEXT NOT NULL,             -- bonus/hold/settle/release/admin
              delta INTEGER NOT NULL,         -- 余额变动（正=入账）
              frozen_delta INTEGER NOT NULL DEFAULT 0,
              ref TEXT, reason TEXT,
              created_at TEXT NOT NULL);
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
              credits INTEGER NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(user_id, created_at);
            """)
            # 老库补列：is_unlimited 是后加的（不限额测试账号），
            # CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            if "is_unlimited" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN is_unlimited"
                          " INTEGER NOT NULL DEFAULT 0")
        _INITED = True
    return c


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ JWT（HS256，标准库手写）

def _b64url(data: bytes) -> str:
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
    """验证码登录，首次自动注册并赠送积分。"""
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
            c.execute("INSERT INTO credits(user_id, balance) VALUES(?,?)",
                      (uid, BONUS_CREDITS))
            c.execute("INSERT INTO ledger(user_id, kind, delta, ref, reason, created_at)"
                      " VALUES(?,?,?,?,?,?)",
                      (uid, "bonus", BONUS_CREDITS, "signup",
                       "新用户注册赠送", _now()))
        else:
            uid = row["id"]
        access = issue_access(uid, phone)
        refresh = _issue_refresh(c, uid)
        bal = c.execute("SELECT balance FROM credits WHERE user_id=?", (uid,)).fetchone()
        return {"ok": True, "access": access, "refresh": refresh,
                "expires_in": ACCESS_TTL, "user_id": uid,
                "phone": mask_phone(phone), "new_user": new_user,
                "unlimited": bool(row["is_unlimited"]) if not new_user else False,
                "credits": bal["balance"] if bal else 0}


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


# ------------------------------------------------------------------ 积分账本

def get_account(uid: int) -> dict:
    with _conn() as c:
        r = c.execute("SELECT c.balance, c.frozen, u.is_unlimited FROM users u"
                      " LEFT JOIN credits c ON c.user_id=u.id WHERE u.id=?",
                      (uid,)).fetchone()
        if not r:
            return {"balance": 0, "frozen": 0, "unlimited": False}
        return {"balance": r["balance"] or 0, "frozen": r["frozen"] or 0,
                "unlimited": bool(r["is_unlimited"])}


def is_unlimited(uid: int) -> bool:
    """测试账号：不计费、不设每日上限，但用量照记（后台仍能看到 token 消耗）。"""
    with _conn() as c:
        r = c.execute("SELECT is_unlimited FROM users WHERE id=?", (uid,)).fetchone()
        return bool(r and r["is_unlimited"])


def get_balance(uid: int) -> int:
    return get_account(uid)["balance"]


def _ledger(c, uid, kind, delta, frozen_delta=0, ref=None, reason=None):
    c.execute("INSERT INTO ledger(user_id, kind, delta, frozen_delta, ref, reason, created_at)"
              " VALUES(?,?,?,?,?,?,?)",
              (uid, kind, delta, frozen_delta, ref, reason, _now()))


def freeze(uid: int, credits: int, ref: str) -> int:
    """预扣：余额够则冻结并返回预扣数，不够抛 QuotaError。同时检查每日上限。

    不限额测试账号（users.is_unlimited=1）跳过余额与每日上限两道检查，
    但照常走"冻结 → 结算"流程：用量流水、token 消耗、后台统计一条都不少，
    只是结算时把预扣全额退回，余额不变。
    """
    credits = max(1, int(credits))
    with _DB_LOCK, _conn() as c:
        row = c.execute("SELECT c.balance, u.is_unlimited FROM users u"
                        " LEFT JOIN credits c ON c.user_id=u.id WHERE u.id=?",
                        (uid,)).fetchone()
        if row is None:
            raise QuotaError("insufficient", 0, "账号不存在")
        bal = row["balance"] or 0
        unlimited = bool(row["is_unlimited"])
        if not unlimited:
            if bal < credits:
                raise QuotaError("insufficient", bal, f"积分不足：需要约 {credits}，余额 {bal}")
            used = c.execute(
                "SELECT COALESCE(SUM(credits),0) AS n FROM usage"
                " WHERE user_id=? AND created_at>=?", (uid, date.today().isoformat())).fetchone()["n"]
            if used + credits > DAILY_CAP_CREDITS:
                raise QuotaError("daily_cap", bal,
                                 f"今日免费额度已用完（{DAILY_CAP_CREDITS} 积分/日），明天再来或充值")
        c.execute("UPDATE credits SET balance=balance-?, frozen=frozen+? WHERE user_id=?",
                  (credits, credits, uid))
        _ledger(c, uid, "hold", -credits, frozen_delta=credits, ref=ref,
                reason="测试账号预扣（结算全额退回）" if unlimited else "LLM 调用预扣")
    return credits


def _active_hold(c, uid: int, ref: str) -> sqlite3.Row | None:
    """该 ref 最新一条流水是 hold 才算未结算（天然幂等：settle/release 后即失效）。"""
    return c.execute("SELECT delta FROM ledger WHERE user_id=? AND ref=?"
                     " ORDER BY id DESC LIMIT 1", (uid, ref)).fetchone()


def settle(uid: int, ref: str, actual: int, *, model: str = "", task: str = "",
           prompt_tokens: int | None = None, completion_tokens: int | None = None) -> int:
    """结算：解冻预扣，按实际用量多退少补。返回本次实扣积分（0=无活跃预扣）。"""
    actual = max(0, int(actual))
    with _DB_LOCK, _conn() as c:
        hold = _active_hold(c, uid, ref)
        if not hold or hold["delta"] >= 0:
            return 0
        est = -hold["delta"]
        row = c.execute("SELECT c.balance, u.is_unlimited FROM users u"
                        " LEFT JOIN credits c ON c.user_id=u.id WHERE u.id=?",
                        (uid,)).fetchone()
        bal = (row["balance"] or 0) if row else 0
        unlimited = bool(row["is_unlimited"]) if row else False
        c.execute("UPDATE credits SET frozen=MAX(0, frozen-?) WHERE user_id=?",
                  (est, uid))
        if unlimited:
            # 测试账号：预扣全额退回，余额回到调用前；用量按实际写进 usage 表，
            # 后台照样能看到 token 和"本应扣多少"，只是不真扣。
            diff, reason = est, f"测试账号不计费（实际 {actual} 积分，已记用量）"
        else:
            diff = est - actual                      # 正=退回，负=补扣
            if diff < 0 and bal + diff < 0:
                diff = -bal                          # 补扣只扣到 0，不允许负余额
            reason = f"LLM 结算：实扣 {actual}（预扣 {est}）"
        c.execute("UPDATE credits SET balance=balance+? WHERE user_id=?", (diff, uid))
        _ledger(c, uid, "settle", diff, ref=ref, reason=reason)
        c.execute("INSERT INTO usage(user_id, model, task, prompt_tokens,"
                  " completion_tokens, credits, created_at) VALUES(?,?,?,?,?,?,?)",
                  (uid, model, task, prompt_tokens, completion_tokens, actual, _now()))
    return 0 if unlimited else actual


def release(uid: int, ref: str) -> None:
    """调用失败：全额退回预扣（ref 已结算/已释放时为空操作）。"""
    with _DB_LOCK, _conn() as c:
        hold = _active_hold(c, uid, ref)
        if not hold or hold["delta"] >= 0:
            return
        est = -hold["delta"]
        c.execute("UPDATE credits SET balance=balance+?, frozen=MAX(0,frozen-?) WHERE user_id=?",
                  (est, est, uid))
        _ledger(c, uid, "release", est, ref=ref, reason="LLM 调用失败退回")


def admin_adjust(uid: int, delta: int, reason: str) -> bool:
    """运营手工调账（充值/扣减），必须写清原因。"""
    with _DB_LOCK, _conn() as c:
        r = c.execute("SELECT balance FROM credits WHERE user_id=?", (uid,)).fetchone()
        if not r or r["balance"] + delta < 0:
            return False
        c.execute("UPDATE credits SET balance=balance+? WHERE user_id=?", (delta, uid))
        _ledger(c, uid, "admin", delta, reason=reason or "运营调整")
    return True


def create_unlimited(phone: str, *, credits: int = BONUS_CREDITS) -> dict:
    """建/升级一个不限额度的测试账号。

    不限额 ≠ 不记账：余额、每日上限、模型花费都不再拦截，
    但每次调用仍写 ledger 与 usage，后台能照常看到 token 和用量。
    余额只是显示用的起点，用光也不影响使用。
    """
    if not PHONE_RE.fullmatch(phone or ""):
        return {"ok": False, "error": "手机号格式不正确"}
    _secret()
    with _DB_LOCK, _conn() as c:
        row = c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        created = row is None
        if created:
            uid = c.execute("INSERT INTO users(phone, created_at) VALUES(?,?)",
                            (phone, _now())).lastrowid
        else:
            uid = row["id"]
        c.execute("UPDATE users SET is_unlimited=1, status='active' WHERE id=?", (uid,))
        if not c.execute("SELECT 1 FROM credits WHERE user_id=?", (uid,)).fetchone():
            c.execute("INSERT INTO credits(user_id, balance) VALUES(?,?)", (uid, credits))
            _ledger(c, uid, "admin", credits, reason="测试账号初始积分")
        _ledger(c, uid, "admin", 0, ref="unlimited", reason="设为不限额测试账号")
        bal = c.execute("SELECT balance FROM credits WHERE user_id=?", (uid,)).fetchone()
        return {"ok": True, "user_id": uid, "phone": mask_phone(phone),
                "created": created, "credits": bal["balance"] if bal else 0,
                "unlimited": True}


def revoke_unlimited(phone: str) -> dict:
    """撤销不限额（改回普通账号，余额与用量不动）。"""
    if not PHONE_RE.fullmatch(phone or ""):
        return {"ok": False, "error": "手机号格式不正确"}
    with _DB_LOCK, _conn() as c:
        row = c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"ok": False, "error": "账号不存在"}
        c.execute("UPDATE users SET is_unlimited=0 WHERE id=?", (row["id"],))
        _ledger(c, row["id"], "admin", 0, ref="unlimited", reason="取消不限额测试账号")
        return {"ok": True, "user_id": row["id"], "phone": mask_phone(phone)}


def list_unlimited() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT u.id, u.phone, u.created_at, COALESCE(c.balance,0) AS balance"
                         " FROM users u LEFT JOIN credits c ON c.user_id=u.id"
                         " WHERE u.is_unlimited=1 ORDER BY u.id").fetchall()
        return [dict(r, phone=mask_phone(r["phone"])) for r in rows]


def list_users(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT u.id, u.phone, u.created_at, u.status, u.is_unlimited,"
            " c.balance, c.frozen,"
            " COALESCE((SELECT SUM(credits) FROM usage WHERE user_id=u.id),0) AS used"
            " FROM users u LEFT JOIN credits c ON c.user_id=u.id"
            " ORDER BY u.id LIMIT ?", (limit,)).fetchall()
        return [dict(r, phone=mask_phone(r["phone"])) for r in rows]


def list_usage(uid: int | None = None, limit: int = 50) -> list[dict]:
    with _conn() as c:
        if uid:
            rows = c.execute("SELECT * FROM usage WHERE user_id=? ORDER BY id DESC LIMIT ?",
                             (uid, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------ 命令行

def _cli(argv: list[str]) -> int:
    """测试账号管理。用法：

        python auth.py --unlimited 19900000000      # 建/升级为不限额账号
        python auth.py --unlimited 19900000000 --credits 5000
        python auth.py --revoke 19900000000         # 撤销不限额
        python auth.py --list                       # 列出所有不限额账号
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(_cli.__doc__)
        return 0
    cmd = argv[0]
    if cmd == "--list":
        rows = list_unlimited()
        if not rows:
            print("还没有不限额账号。用 --unlimited <手机号> 建一个。")
            return 0
        print(f"{'uid':>4}  {'手机号':<13} {'余额':>6}  创建时间")
        for r in rows:
            print(f"{r['id']:>4}  {r['phone']:<13} {r['balance']:>6}  {r['created_at']}")
        return 0
    if cmd in ("--unlimited", "--revoke"):
        if len(argv) < 2:
            print("缺少手机号。例：python auth.py --unlimited 19900000000")
            return 2
        phone = argv[1]
        credits = BONUS_CREDITS
        if "--credits" in argv:
            i = argv.index("--credits")
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                credits = int(argv[i + 1])
        if cmd == "--unlimited":
            r = create_unlimited(phone, credits=credits)
            if not r.get("ok"):
                print("失败：", r.get("error"))
                return 1
            print(f"{'已创建' if r['created'] else '已升级'}不限额账号  uid={r['user_id']}  "
                  f"手机号={r['phone']}  余额={r['credits']}")
            print("登录方式：网站上用 199xxxx 这类号码走验证码登录即可（演示模式验证码会回显）。")
            return 0
        r = revoke_unlimited(phone)
        if not r.get("ok"):
            print("失败：", r.get("error"))
            return 1
        print(f"已撤销不限额  uid={r['user_id']}  {r['phone']}")
        return 0
    print(f"未知参数：{cmd}\n{_cli.__doc__}")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
