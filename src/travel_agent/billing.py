"""套餐与订单：积分充值包 + 订阅月卡（企业化路线图 M2「能收钱」）。

定价按路线图 2.3 建议起步：订阅制（个人版 ¥19/月）+ 按量充值包两条腿。
付费权益（限流精细化 + 每日上限放宽）由 users.plan 字段驱动，
auth.freeze / server 限流按 plan 分档取值——付费状态过期自动回落免费档。

支付：
- **演示模式**（TRAVEL_PAY_MOCK != "0"，默认）：/pay/{订单号} 收银页 +
  「确认支付」回调入账——与短信验证码的 mock 模式同一哲学，本地/测试闭环；
- **真实微信 Native 支付**：TRAVEL_PAY_MOCK=0 时走 wechat_native_pay()，
  当前为占位 stub（需要商户号/证书/回调域名，部署阶段接入）；
  接入后回调验签 → pay_order() 入账，本模块其他代码不动。

账本纪律：所有入账走 auth.admin_adjust（ledger kind=admin，原因注明订单号），
与既有积分账本可对账、可审计；重复支付幂等（已支付订单直接返回成功）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime

from . import auth

_LOCK = threading.Lock()
_INITED = False

# ------------------------------------------------------------------ 套餐

# price_cents 单位为分；credits 为到账积分；days 仅订阅类套餐有效
PLANS: dict[str, dict] = {
    "pack_1000": {"kind": "pack", "price_cents": 990, "credits": 1000,
                  "title": "体验包 · 1000 积分"},
    "pack_3500": {"kind": "pack", "price_cents": 2990, "credits": 3500,
                  "title": "标准包 · 3500 积分"},
    "pack_9000": {"kind": "pack", "price_cents": 6890, "credits": 9000,
                  "title": "畅玩包 · 9000 积分"},
    "sub_month": {"kind": "sub", "price_cents": 1900, "credits": 3000,
                  "days": 30, "title": "个人版月卡 · 每月 3000 积分"},
}


def pay_mode() -> str:
    """mock=演示收银台；wechat=真实微信支付（未接入前 create_order 会拒绝）。"""
    return "mock" if os.environ.get("TRAVEL_PAY_MOCK", "1") != "0" else "wechat"


def list_plans() -> list[dict]:
    out = []
    for pid, p in PLANS.items():
        out.append({"plan_id": pid, "kind": p["kind"],
                    "price_cents": p["price_cents"],
                    "price_yuan": p["price_cents"] / 100,
                    "credits": p["credits"], "title": p["title"]})
    return out


# ------------------------------------------------------------------ 存储

def db_path():
    return auth.db_path()


def _conn() -> sqlite3.Connection:
    global _INITED
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    if not _INITED:
        with c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS orders(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_no TEXT UNIQUE NOT NULL,
              uid INTEGER NOT NULL,
              plan_id TEXT NOT NULL,
              kind TEXT NOT NULL,               -- pack/sub
              amount_cents INTEGER NOT NULL,
              credits INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'created',   -- created/paid/failed
              created_at REAL NOT NULL,
              paid_at REAL,
              pay_ref TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS idx_orders_uid ON orders(uid, created_at);
            """)
            # 老库补列：付费层级（free/paid）与订阅到期时间
            cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
            if "plan" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL"
                          " DEFAULT 'free'")
            if "plan_expires" not in cols:
                c.execute("ALTER TABLE users ADD COLUMN plan_expires"
                          " REAL NOT NULL DEFAULT 0")
        _INITED = True
    return c


def _now_ts() -> float:
    return time.time()


def _ts_str(ts) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


# ------------------------------------------------------------------ 下单与支付

def create_order(uid: int, plan_id: str) -> dict:
    """创建订单。演示模式返回收银页地址；真实支付未接入时明确拒绝。"""
    p = PLANS.get(str(plan_id or ""))
    if not p:
        return {"ok": False, "error": "套餐不存在"}
    if pay_mode() == "wechat":
        return {"ok": False, "error": "微信支付未配置（部署阶段接入微信 Native 后开放）"}
    order_no = f"TA{datetime.now().strftime('%Y%m%d%H%M%S')}"
    with _LOCK, _conn() as c:
        while c.execute("SELECT 1 FROM orders WHERE order_no=?",
                        (order_no,)).fetchone():     # 秒级重号（并发下单）补随机后缀
            order_no = f"TA{datetime.now().strftime('%Y%m%d%H%M%S')}{os.urandom(2).hex()}"
        cur = c.execute(
            "INSERT INTO orders(order_no, uid, plan_id, kind, amount_cents,"
            " credits, created_at) VALUES(?,?,?,?,?,?,?)",
            (order_no, uid, plan_id, p["kind"], p["price_cents"],
             p["credits"], _now_ts()))
        oid = cur.lastrowid
    return {"ok": True, "order_id": oid, "order_no": order_no,
            "pay_url": f"/pay/{order_no}", "pay_mode": "mock",
            "amount_cents": p["price_cents"], "title": p["title"]}


def pay_order(order_no: str, uid: int | None = None, pay_ref: str = "mock") -> dict:
    """支付回调入账：标记已付 + 发积分 + （订阅）延长付费档。幂等。

    uid 传入时校验订单归属（服务端路由用）；None 表示内部调用（微信回调验签后）。
    注意连接纪律：不能在自己的事务里调 auth.admin_adjust——两个连接抢同一把
    SQLite 写锁会死锁。顺序：① 原子标记已付（条件 UPDATE，只可能有一方赢）
    → ② 事务外走统一账本入账 → ③ 入账失败回滚订单状态。
    """
    with _LOCK, _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE order_no=?", (order_no,)).fetchone()
        if not row:
            return {"ok": False, "error": "订单不存在"}
        if uid is not None and row["uid"] != uid:
            return {"ok": False, "error": "订单不属于当前用户"}
        p = PLANS.get(row["plan_id"])
        if not p:
            return {"ok": False, "error": "套餐配置已下架"}
        # 条件更新：并发回调只有一方能把 created → paid（天然幂等）
        cur = c.execute("UPDATE orders SET status='paid', paid_at=?, pay_ref=?"
                        " WHERE id=? AND status='created'",
                        (_now_ts(), pay_ref, row["id"]))
        already = cur.rowcount == 0
    if already:
        return {"ok": True, "already_paid": True, "credits": row["credits"]}
    # 入账走统一账本：可对账、可审计（充值失败/退款都可追）
    ok = auth.admin_adjust(row["uid"], row["credits"],
                           f"充值到账：{p['title']}（订单 {order_no}）")
    if not ok:
        # 理论不可达（用户必存在）；回滚订单状态，避免"钱收了分没到"
        with _LOCK, _conn() as c:
            c.execute("UPDATE orders SET status='created', paid_at=NULL,"
                      " pay_ref='' WHERE id=?", (row["id"],))
        return {"ok": False, "error": "入账失败，请重试"}
    if p["kind"] == "sub":
        with _LOCK, _conn() as c:
            c.execute("UPDATE users SET plan='paid',"
                      " plan_expires=MAX(plan_expires, ?)+? WHERE id=?",
                      (_now_ts(), p["days"] * 86400, row["uid"]))
    return {"ok": True, "already_paid": False, "credits": row["credits"],
            "kind": p["kind"]}


def order_info(order_no: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE order_no=?",
                        (order_no,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["created_str"] = _ts_str(row["created_at"])
    out["paid_str"] = _ts_str(row["paid_at"])
    plan = PLANS.get(row["plan_id"]) or {}
    out["title"] = plan.get("title") or row["plan_id"]
    return out


def list_orders(uid: int | None = None, limit: int = 50) -> list[dict]:
    with _conn() as c:
        if uid is not None:
            rows = c.execute("SELECT * FROM orders WHERE uid=? ORDER BY id DESC"
                             " LIMIT ?", (uid, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["created_str"] = _ts_str(r["created_at"])
        d["paid_str"] = _ts_str(r["paid_at"])
        d["title"] = (PLANS.get(r["plan_id"]) or {}).get("title") or r["plan_id"]
        with auth._conn() as c:
            u = c.execute("SELECT phone FROM users WHERE id=?",
                          (r["uid"],)).fetchone()
        d["phone"] = auth.mask_phone(u["phone"]) if u else ""
        out.append(d)
    return out


# ------------------------------------------------------------------ 付费层级

def is_paid(uid: int) -> bool:
    """付费档：有效订阅期内。到期自动回落免费档（状态在读时判定，无需定时任务）。"""
    with _conn() as c:
        r = c.execute("SELECT plan, plan_expires FROM users WHERE id=?",
                      (uid,)).fetchone()
    return bool(r and r["plan"] == "paid" and r["plan_expires"] > _now_ts())


def plan_of(uid: int) -> str:
    return "paid" if is_paid(uid) else "free"


def active_subs() -> int:
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM users WHERE plan='paid'"
                      " AND plan_expires>?", (_now_ts(),)).fetchone()["n"]
    return n


# ------------------------------------------------------------------ 财务统计

def finance_stats(days: int = 14) -> dict:
    """运营后台财务看板：收入、订单、积分消耗的总量与按日曲线。"""
    import time as _t
    today = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rev = c.execute("SELECT COALESCE(SUM(amount_cents),0) AS n,"
                        " COUNT(*) AS cnt FROM orders WHERE status='paid'").fetchone()
        rev_today = c.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS n, COUNT(*) AS cnt"
            " FROM orders WHERE status='paid' AND paid_at>=?",
            (_t.mktime(_t.strptime(today, "%Y-%m-%d")),)).fetchone()
        daily = c.execute(
            "SELECT date(paid_at,'unixepoch','localtime') AS d,"
            " SUM(amount_cents) AS cents, COUNT(*) AS cnt"
            " FROM orders WHERE status='paid' GROUP BY d ORDER BY d DESC LIMIT ?",
            (days,)).fetchall()
    with auth._conn() as c:
        used_daily = c.execute(
            "SELECT date(created_at) AS d, SUM(credits) AS credits,"
            " COUNT(*) AS calls FROM usage GROUP BY d ORDER BY d DESC LIMIT ?",
            (days,)).fetchall()
        used_today = c.execute(
            "SELECT COALESCE(SUM(credits),0) AS n FROM usage"
            " WHERE created_at>=?", (today,)).fetchone()["n"]
    return {
        "revenue_cents_total": rev["n"],
        "revenue_yuan_total": rev["n"] / 100,
        "orders_paid_total": rev["cnt"],
        "revenue_cents_today": rev_today["n"],
        "orders_paid_today": rev_today["cnt"],
        "active_subs": active_subs(),
        "credits_used_today": used_today,
        "daily_revenue": [{"date": r["d"], "yuan": r["cents"] / 100,
                           "orders": r["cnt"]} for r in daily],
        "daily_usage": [{"date": r["d"], "credits": r["credits"],
                         "calls": r["calls"]} for r in used_daily],
        "pay_mode": pay_mode(),
    }
