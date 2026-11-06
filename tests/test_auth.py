"""auth.py 账号与计费层测试：手机号登录、JWT、积分预扣-结算、计量客户端。

不联网：LLM 侧用 ScriptedClient 回放。HTTP 链路（send_code/login/me/chat 鉴权）
由 test_server.py 覆盖；本文件聚焦账本与 token 的正确性。

运行：python test_auth.py    失败时退出码为 1。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

TMP = Path(tempfile.mkdtemp(prefix="travel_auth_test_"))
os.environ["TRAVEL_APP_DB"] = str(TMP / "app.db")

from travel_agent import auth
from travel_agent import llm

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


# ---------------------------------------------------------------- 工具函数

def test_utils() -> None:
    check("tokens_to_credits 向上取整",
          auth.tokens_to_credits(1) == 1 and auth.tokens_to_credits(150) == 2
          and auth.tokens_to_credits(201) == 3,
          f"1→{auth.tokens_to_credits(1)},150→{auth.tokens_to_credits(150)},201→{auth.tokens_to_credits(201)}")
    check("mask_phone 中间打码", auth.mask_phone("13812345678") == "138****5678")


# ---------------------------------------------------------------- 验证码与登录

PHONE = "13812345678"


def _send_and_get_code(phone: str = PHONE) -> str:
    r = auth.send_code(phone)
    assert r["ok"], r
    return r["dev_code"]


def test_send_code() -> None:
    r = auth.send_code("12345")
    check("非法手机号被拒绝", r["ok"] is False and "格式" in r["error"])
    r = auth.send_code(PHONE)
    check("合法手机号发出验证码（演示模式回显 dev_code）",
          r["ok"] is True and len(r.get("dev_code", "")) == 6, f"r={r}")


def test_login_register() -> None:
    code = _send_and_get_code()
    r = auth.login(PHONE, "000000")
    check("错误验证码登录失败", r["ok"] is False)

    r = auth.login(PHONE, code)
    check("验证码登录成功且为新用户",
          r["ok"] is True and r["new_user"] is True, f"r={ {k: v for k, v in r.items() if k != 'access'} }")
    check("新用户赠送 500 积分", r["credits"] == auth.BONUS_CREDITS == 500,
          f"credits={r['credits']}")
    global UID, ACCESS, REFRESH
    UID, ACCESS, REFRESH = r["user_id"], r["access"], r["refresh"]

    code2 = _send_and_get_code()
    r2 = auth.login(PHONE, code2)
    check("再次登录不重复赠送（老用户）",
          r2["ok"] is True and r2["new_user"] is False and r2["credits"] == 500,
          f"credits={r2['credits']}")

    code3 = _send_and_get_code()
    check("同一验证码不能复用", auth.login(PHONE, code3)["ok"] is True
          and auth.login(PHONE, code3)["ok"] is False,
          "第二次用同一验证码应失败")


def test_jwt() -> None:
    p = auth.verify_access(ACCESS)
    check("access token 可验证且带 uid/phone",
          p is not None and p["uid"] == UID and p["phone"] == PHONE, f"p={p}")
    check("篡改的 token 被拒绝", auth.verify_access(ACCESS[:-3] + "abc") is None)
    expired = auth.issue_access(UID, PHONE, ttl=-10)
    check("过期 token 被拒绝", auth.verify_access(expired) is None)

    r = auth.refresh_access(REFRESH)
    check("refresh 换新 access 成功",
          r["ok"] is True and auth.verify_access(r["access"])["uid"] == UID)
    check("伪造 refresh 被拒绝", auth.refresh_access("forged-token")["ok"] is False)


# ---------------------------------------------------------------- 积分账本

def test_ledger() -> None:
    # 起始：500
    try:
        auth.freeze(UID, 100000, "t-over")
        check("余额不足时 freeze 抛 QuotaError", False)
    except auth.QuotaError as e:
        check("余额不足时 freeze 抛 QuotaError(insufficient)",
              e.reason == "insufficient" and e.balance == 500, f"e={e}")

    est = auth.freeze(UID, 60, "t-1")
    acct = auth.get_account(UID)
    check("冻结：余额减、冻结增",
          est == 60 and (acct["balance"], acct["frozen"]) == (440, 60), f"acct={acct}")

    spent = auth.settle(UID, "t-1", 30, model="test-model", task="unit",
                        prompt_tokens=2000, completion_tokens=1000)
    acct = auth.get_account(UID)
    check("结算少用：退回差额，冻结清零",
          spent == 30 and (acct["balance"], acct["frozen"]) == (470, 0), f"acct={acct}")
    usage = auth.list_usage(UID)
    check("用量流水已记录", len(usage) == 1 and usage[0]["credits"] == 30
          and usage[0]["model"] == "test-model", f"usage={usage[:1]}")

    auth.freeze(UID, 40, "t-2")
    auth.settle(UID, "t-2", 50)
    acct = auth.get_account(UID)
    check("结算超用：补扣差额", acct["balance"] == 420 and acct["frozen"] == 0,
          f"acct={acct}")

    auth.freeze(UID, 40, "t-3")
    auth.release(UID, "t-3")
    acct = auth.get_account(UID)
    check("调用失败 release：全额退回",
          (acct["balance"], acct["frozen"]) == (420, 0), f"acct={acct}")

    check("重复结算/释放同一 ref 为幂等空操作",
          auth.settle(UID, "t-3", 999) == 0 and auth.release(UID, "t-99") is None)

    old_cap = auth.DAILY_CAP_CREDITS
    try:
        auth.DAILY_CAP_CREDITS = 10      # 已用 80，冻结需要 >10 必触顶
        try:
            auth.freeze(UID, 40, "t-cap")
            check("超过每日上限被拒绝", False)
        except auth.QuotaError as e:
            check("超过每日上限被拒绝（daily_cap）", e.reason == "daily_cap", f"e={e}")
    finally:
        auth.DAILY_CAP_CREDITS = old_cap

    check("运营调账充值", auth.admin_adjust(UID, 100, "测试充值")
          and auth.get_balance(UID) == 520)
    check("运营调账扣到负数被拒", auth.admin_adjust(UID, -99999, "越权扣减") is False)


# ---------------------------------------------------------------- 计量客户端

def test_metered_client() -> None:
    meter = auth
    # 正常路径：预扣 → 回放成功 → 按估算+默认输出结算（ScriptedClient 无 usage）
    inner = llm.ScriptedClient(['{"destination":"大理"}'])
    mc = llm.MeteredClient(inner, UID, meter=meter)
    bal0 = auth.get_balance(UID)
    text = mc.chat("system-prompt-here", "user-message-here")
    bal1 = auth.get_balance(UID)
    frozen1 = auth.get_account(UID)["frozen"]
    check("计量调用成功：扣费且无冻结残留",
          text == '{"destination":"大理"}' and bal1 < bal0 and frozen1 == 0,
          f"bal {bal0}→{bal1}, frozen={frozen1}")
    check("计量客户端暴露 inner 的模型名", mc.model == inner.model)

    # 失败路径：脚本耗尽抛 LLMError → 预扣全额退回
    inner2 = llm.ScriptedClient([])      # 无脚本 → chat 抛 LLMError
    mc2 = llm.MeteredClient(inner2, UID, meter=meter)
    bal2 = auth.get_balance(UID)
    try:
        mc2.chat("s", "u")
        check("计量调用失败抛 LLMError", False)
    except llm.LLMError:
        pass
    acct = auth.get_account(UID)
    check("计量调用失败：预扣全额退回，无冻结残留",
          acct["balance"] == bal2 and acct["frozen"] == 0, f"acct={acct}")

    # 额度不足：MeteredClient 直接抛 QuotaError，且不触达 inner
    broke = llm.ScriptedClient(["x"])
    mc3 = llm.MeteredClient(broke, UID, meter=meter)
    old_bal = auth.get_balance(UID)
    auth.admin_adjust(UID, -old_bal, "清零测试")
    try:
        mc3.chat("s", "u")
        check("零余额时计量调用抛 QuotaError", False)
    except auth.QuotaError:
        check("零余额时计量调用抛 QuotaError（未触达模型）", True)
    finally:
        auth.admin_adjust(UID, old_bal, "恢复测试")


def test_unlimited_account() -> None:
    """不限额测试账号：绕过余额与每日上限，但账照记（用量/冻结/流水都不能漏）。"""
    phone = "19900000000"
    r = auth.create_unlimited(phone, credits=0)
    uid = r["user_id"]
    check("能建出不限额账号，且余额可以给 0", r["ok"] and r["credits"] == 0, f"r={r}")
    check("is_unlimited 能被读到", auth.is_unlimited(uid) is True)
    check("login 返回 unlimited 标记",
          auth.login(phone, _send_and_get_code(phone)).get("unlimited") is True)

    # 零余额仍可预扣+结算（普通账号这一步必然 QuotaError）
    mc = llm.MeteredClient(llm.ScriptedClient(["ok"]), uid, meter=auth)
    bal0 = auth.get_balance(uid)
    mc.chat("s", "u")
    acct = auth.get_account(uid)
    check("零余额也能调用，且调用后余额不变",
          acct["balance"] == bal0 and acct["frozen"] == 0,
          f"bal {bal0}→{acct['balance']}, frozen={acct['frozen']}")
    used = auth.list_usage(uid, 5)
    check("不限额也要记用量（后台能看到 token）",
          len(used) == 1 and used[0]["credits"] > 0, f"usage={used[:1]}")

    # 打满每日上限：普通账号会被拦，不限额账号照跑
    with auth._conn() as c:
        c.execute("INSERT INTO usage(user_id, model, task, prompt_tokens,"
                  " completion_tokens, credits, created_at) VALUES(?,?,?,?,?,?,?)",
                  (uid, "test", "fill", 0, 0, auth.DAILY_CAP_CREDITS + 100,
                   auth._now()))
    r2 = auth.freeze(uid, 10, "ref-cap")
    check("超过每日上限仍能预扣（普通账号此处会 QuotaError）", r2 == 10)
    auth.settle(uid, "ref-cap", 3, model="test", task="cap")
    check("结算后余额回到预扣前", auth.get_balance(uid) == bal0)

    # 普通账号的对照：同样零余额必须被拦
    other = auth.login("13712345678", _send_and_get_code("13712345678"))
    ouid = other["user_id"]
    with auth._conn() as c:
        c.execute("UPDATE credits SET balance=0 WHERE user_id=?", (ouid,))
    blocked = False
    try:
        auth.freeze(ouid, 10, "ref-normal")
    except auth.QuotaError:
        blocked = True
    check("普通账号零余额依然被拦（不限额没有污染全局规则）", blocked)

    auth.revoke_unlimited(phone)
    check("撤销后恢复普通账号", auth.is_unlimited(uid) is False)


def main() -> int:
    test_utils()
    test_send_code()
    test_login_register()
    test_jwt()
    test_ledger()
    test_metered_client()
    test_unlimited_account()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条账号计费断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
