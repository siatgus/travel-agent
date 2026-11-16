"""M2 收钱闭环测试：套餐/订单/演示支付 + 分享页 + 免费/付费分档。

防线：
1. billing 单元：套餐列表、下单、支付入账（走统一账本）、重复支付幂等、
   订单归属校验、订阅到期时间叠加、付费档判定与过期回落；
2. 每日上限分档：免费档 300/日、付费档 1500/日（auth.freeze）；
3. 分享：生成（无方案拒绝）、幂等复用、撤销后失效；
4. HTTP 全链路：/api/plans 公开可看、下单→收银页→支付→余额到账、
   未登录 401、/share 链接公开可访问、撤销后 404。

运行：python test_billing.py    失败时退出码为 1。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

_TMP = Path(tempfile.mkdtemp(prefix="travel_billing_test_"))
os.environ["TRAVEL_APP_DB"] = str(_TMP / "app.db")

from travel_agent import auth, billing, conversations, ratelimit  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def _fake_plan() -> dict:
    """能通过 build_html 渲染的最小方案（字段 schema 与真实方案一致）。"""
    node = {"time": "09:00", "name": "大理古城", "type": "attraction",
            "duration_min": 120, "cost": 0, "transit": "", "tip": "",
            "booking_required": 0, "geo": [25.69, 100.16], "note": ""}
    return {
        "slots": {"destination": "大理", "days": 1, "pax": 2, "budget": 3000,
                  "pace": "moderate", "party_size": 2,
                  "date_range": {"start": "2026-11-20", "end": "2026-11-20",
                                 "label": "11月20日-11月20日"}},
        "budget": {"total": 3000, "target": 3000, "per_person": 1500,
                   "contingency": 300,
                   "breakdown": [{"name": "住宿", "category": "hotel",
                                  "label": "客栈 1 晚", "amount": 600}],
                   "items": []},
        "transport": [], "hotel": {"items": []},
        "outbound": None, "inbound": None,
        "weather": {"days": [], "advice": ""},
        "days_plan": [{"day": 1, "date": "2026-11-20", "theme": "古城漫步",
                       "move_min": 30, "day_budget": 600, "nodes": [node]}],
    }


def _make_user(phone: str) -> int:
    """直接建号并送积分（绕开验证码，单元层更快）。"""
    auth._secret()
    with auth._DB_LOCK, auth._conn() as c:
        row = c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row:
            return row["id"]
        uid = c.execute("INSERT INTO users(phone, created_at) VALUES(?,?)",
                        (phone, "2026-11-16T10:00:00")).lastrowid
        c.execute("INSERT INTO credits(user_id, balance) VALUES(?,?)",
                  (uid, auth.BONUS_CREDITS))
        c.execute("INSERT INTO ledger(user_id, kind, delta, ref, reason, created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (uid, "bonus", auth.BONUS_CREDITS, "signup", "测试", "now"))
        return uid


def test_plans_and_orders() -> None:
    plans = billing.list_plans()
    check("套餐列表含充值包与订阅", len(plans) >= 4
          and any(p["kind"] == "sub" for p in plans), str(len(plans)))

    uid = _make_user("13700005001")
    bal0 = auth.get_balance(uid)
    r = billing.create_order(uid, "no_such")
    check("不存在的套餐拒绝下单", not r.get("ok"), str(r))
    r = billing.create_order(uid, "pack_1000")
    check("下单成功返回收银页地址", r.get("ok") and r["pay_url"].startswith("/pay/")
          and r["amount_cents"] == 990, str(r))
    order_no = r["order_no"]

    check("待支付订单可查", billing.order_info(order_no)["status"] == "created", "")
    r = billing.pay_order(order_no, uid=uid)
    check("支付成功并到账", r.get("ok") and not r.get("already_paid")
          and auth.get_balance(uid) == bal0 + 1000, f"{r} bal={auth.get_balance(uid)}")
    check("订单状态已付", billing.order_info(order_no)["status"] == "paid", "")
    # 幂等：重复回调不重复入账
    r = billing.pay_order(order_no, uid=uid)
    check("重复支付幂等（不二次入账）",
          r.get("ok") and r.get("already_paid")
          and auth.get_balance(uid) == bal0 + 1000, str(r))
    # 归属校验
    uid2 = _make_user("13700005002")
    r = billing.pay_order(order_no, uid=uid2)
    check("他人订单拒绝支付", not r.get("ok"), str(r))
    # 账本可对账
    with auth._conn() as c:
        led = c.execute("SELECT reason FROM ledger WHERE user_id=? AND kind='admin'"
                        " ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    check("入账写进账本并注明订单号", led and order_no in led["reason"], str(led))


def test_subscription() -> None:
    uid = _make_user("13700005003")
    check("免费档判定", billing.plan_of(uid) == "free", "")
    bal0 = auth.get_balance(uid)
    r = billing.create_order(uid, "sub_month")
    order_no = r["order_no"]
    r = billing.pay_order(order_no, uid=uid)
    check("订阅支付到账月卡积分", r.get("ok")
          and auth.get_balance(uid) == bal0 + 3000, f"{r}")
    check("订阅后为付费档", billing.plan_of(uid) == "paid"
          and billing.is_paid(uid), "")
    check("财务统计订阅中用户≥1", billing.finance_stats()["active_subs"] >= 1, "")

    # 到期回落：把 plan_expires 拨回过去
    with auth._DB_LOCK, auth._conn() as c:
        c.execute("UPDATE users SET plan_expires=1 WHERE id=?", (uid,))
    check("订阅到期自动回落免费档", billing.plan_of(uid) == "free"
          and not auth.is_paid_user(uid), "")

    # 续费叠加：重新订阅在旧到期时间上延长（旧到期已过则从当前起算）
    with auth._DB_LOCK, auth._conn() as c:
        c.execute("UPDATE users SET plan='paid', plan_expires=99999999999 WHERE id=?",
                  (uid,))
    r = billing.create_order(uid, "sub_month")
    billing.pay_order(r["order_no"], uid=uid)
    with auth._conn() as c:
        exp = c.execute("SELECT plan_expires FROM users WHERE id=?", (uid,)).fetchone()["plan_expires"]
    import time as _t
    want = 99999999999 + 30 * 86400
    check("续费在旧到期时间上叠加 30 天", abs(exp - want) < 3600,
          f"{exp} vs {want}")


def test_daily_cap_tiers() -> None:
    """免费 300/日 vs 付费 1500/日：直接把用量写满再试冻结。"""
    uid = _make_user("13700005004")
    # 免费档：今日已用 280，再冻 20 恰好到 300 上限；再冻 30 被拦
    with auth._conn() as c:
        c.execute("INSERT INTO usage(user_id, model, task, credits, created_at)"
                  " VALUES(?,?,?,?,date('now','localtime'))",
                  (uid, "test", "test", 280))
    try:
        auth.freeze(uid, 20, "cap-t1")
        ok1 = True
    except auth.QuotaError:
        ok1 = False
    check("免费档 300 内可预扣", ok1, "")
    try:
        auth.freeze(uid, 30, "cap-t2")
        check("免费档超 300/日被拦", False, "未触发上限")
        auth.release(uid, "cap-t2")
    except auth.QuotaError as e:
        check("免费档超 300/日被拦", e.reason == "daily_cap", str(e))
    auth.release(uid, "cap-t1")
    # 升级付费档：上限放宽到 1500
    with auth._DB_LOCK, auth._conn() as c:
        c.execute("UPDATE users SET plan='paid', plan_expires=99999999999 WHERE id=?",
                  (uid,))
    try:
        auth.freeze(uid, 300, "cap-p1")
        check("付费档同日可继续预扣（1500/日）", True, "")
        auth.release(uid, "cap-p1")
    except auth.QuotaError as e:
        check("付费档同日可继续预扣（1500/日）", False, str(e))


def test_share_unit() -> None:
    uid = _make_user("13700005005")
    check("没有方案的对话不能分享",
          conversations.create_share(uid, "sid-share") is None, "")
    # 塞一份方案进去
    conversations.record_turn(uid, "sid-share", "帮我排大理", "好的",
                              dest="大理", plan=_fake_plan(), utterance="帮我排大理")
    r = conversations.create_share(uid, "sid-share")
    check("有方案可分享", bool(r and r.get("token")), str(r))
    token = r["token"]
    check("重复分享复用同一令牌",
          conversations.create_share(uid, "sid-share")["token"] == token, "")
    check("令牌可取到方案", conversations.get_share(token) is not None, "")
    check("撤销返回成功", conversations.revoke_share(uid, "sid-share"), "")
    check("撤销后链接失效", conversations.get_share(token) is None, "")


# ---------------------------------------------------------------- HTTP 全链路

def _post(base, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except json.JSONDecodeError:
            return e.code, {}


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _login(base, phone):
    st, j = _post(base, "/api/auth/send_code", {"phone": phone})
    assert st == 200 and j.get("dev_code"), f"send_code: {st} {j}"
    st, j = _post(base, "/api/auth/login", {"phone": phone, "code": j["dev_code"]})
    assert st == 200 and j.get("ok"), f"login: {st} {j}"
    return j["access"]


def test_http(base: str) -> None:
    st, raw = _get(base, "/api/plans")
    j = json.loads(raw) if raw.lstrip().startswith("{") else {}
    check("套餐接口公开可看（未登录）", st == 200 and j.get("ok")
          and len(j.get("plans") or []) >= 4, f"{st}")

    token = _login(base, "13700006001")
    st, j = _post(base, "/api/orders", {"plan_id": "pack_1000"}, token)
    check("下单返回收银页地址", st == 200 and j.get("ok")
          and j.get("pay_url", "").startswith("/pay/"), f"{st} {j}")
    pay_url = j["pay_url"]
    st, html = _get(base, pay_url)
    check("收银页可访问且带确认按钮", st == 200 and "确认支付" in html
          and j["order_no"] in html, f"{st}")

    st, j = _post(base, "/api/pay/mock", {"order_no": j["order_no"]}, token)
    check("支付回调入账", st == 200 and j.get("ok"), f"{st} {j}")
    st, me = _get_auth(base, "/api/me", token)
    check("余额到账（500+1000）", me.get("balance") == 1500, str(me))

    st, j = _post(base, "/api/orders", {"plan_id": "pack_1000"})
    check("未登录下单 401", st == 401, f"{st}")

    # 分享全链路：先往这个用户的对话里塞一份方案（直接走 conversations 层）
    uid_q = "SELECT id FROM users WHERE phone='13700006001'"
    with auth._conn() as c:
        uid = c.execute(uid_q).fetchone()["id"]
    conversations.record_turn(uid, "http-share", "排一个丽江", "好",
                              dest="丽江", plan=_fake_plan(), utterance="排一个丽江")
    st, j = _post(base, "/api/share", {"sid": "http-share"}, token)
    check("分享链接生成", st == 200 and j.get("ok") and j.get("url"), f"{st} {j}")
    share_url = j["url"]
    st, html = _get(base, share_url)
    check("分享页公开可访问且带引导条", st == 200
          and "免费生成我的行程" in html, f"{st}")
    st, j = _post(base, "/api/share", {"sid": "http-share"}, token)
    check("重复分享幂等", j.get("url") == share_url, str(j))
    # 未登录者不能分享别人的东西：另一用户分享同一 sid → 该 sid 属于前者 → 拒绝
    token2 = _login(base, "13700006002")
    st, j = _post(base, "/api/share", {"sid": "http-share"}, token2)
    check("不能分享他人的对话", st == 400, f"{st} {j}")
    # 撤销
    req = urllib.request.Request(base + "/api/share/http-share", method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    check("撤销分享成功", j.get("ok"), str(j))
    st, _ = _get(base, share_url)
    check("撤销后分享页 404", st == 404, f"{st}")


def _get_auth(base, path, token):
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except json.JSONDecodeError:
            return e.code, {}


def main() -> int:
    test_plans_and_orders()
    test_subscription()
    test_daily_cap_tiers()
    test_share_unit()

    from travel_agent import server as srv
    ratelimit.reset()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"（测试服务已起：{base}）\n")
    test_http(base)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条 M2 断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
