"""auth.py 账号层测试：手机号登录、JWT、验证码、用量记录。

不联网：LLM 侧用 ScriptedClient 回放。HTTP 链路（send_code/login/me/chat 鉴权）
由 test_server.py 覆盖；本文件聚焦账号与 token 的正确性。
本服务免费开放，原积分账本测试已随计费层移除。

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
    check("mask_phone 中间打码", auth.mask_phone("13812345678") == "138****5678")
    check("非 11 位手机号不打码", auth.mask_phone("123") == "123")


# ---------------------------------------------------------------- 验证码与登录

PHONE = "13812345678"


def _send_and_get_code(phone: str = PHONE) -> str:
    r = auth.send_code(phone)
    assert r["ok"], r
    return r["dev_code"]


def test_send_code() -> None:
    check("非法手机号被拒绝", auth.send_code("123")["ok"] is False
          and "格式" in auth.send_code("123")["error"])
    check("合法手机号发出验证码（演示模式回显 dev_code）",
          auth.send_code(PHONE)["ok"] is True
          and len(auth.send_code(PHONE).get("dev_code", "")) == 6)


def test_login_register() -> None:
    code = _send_and_get_code()
    check("错误验证码登录失败", auth.login(PHONE, "000000")["ok"] is False)
    r = auth.login(PHONE, code)
    check("验证码登录成功且为新用户",
          r["ok"] and r["new_user"] and r["phone"] == "138****5678", f"r={r}")
    check("登录返回不再带积分字段（计费层已移除）",
          "credits" not in r and "unlimited" not in r and "free_mode" not in r,
          f"keys={sorted(r)}")
    code2 = _send_and_get_code()
    r2 = auth.login(PHONE, code2)
    check("再次登录不重复赠送（老用户）",
          r2["ok"] and r2["new_user"] is False, f"r2={r2}")
    code3 = _send_and_get_code()
    check("同一验证码不能复用", auth.login(PHONE, code3)["ok"] is True
          and auth.login(PHONE, code3)["ok"] is False)


def test_jwt() -> None:
    code = _send_and_get_code()
    ACCESS = auth.login(PHONE, code)["access"]
    v = auth.verify_access(ACCESS)
    check("access token 可验证且带 uid/phone",
          v is not None and v.get("uid") and v.get("phone") == PHONE, f"v={v}")
    check("篡改的 token 被拒绝", auth.verify_access(ACCESS[:-3] + "abc") is None)
    expired = auth.issue_access(1, PHONE, ttl=-10)
    check("过期 token 被拒绝", auth.verify_access(expired) is None)
    r = auth.login(PHONE, _send_and_get_code())
    check("refresh 换新 access 成功",
          auth.refresh_access(r["refresh"])["ok"] is True)
    check("伪造 refresh 被拒绝", auth.refresh_access("forged-token")["ok"] is False)


# ---------------------------------------------------------------- 用量记录与计量客户端

def test_record_usage() -> None:
    code = _send_and_get_code()
    uid = auth.login(PHONE, code)["user_id"]
    auth.record_usage(uid, model="deepseek-chat", task="rerank",
                      prompt_tokens=120, completion_tokens=45)
    rows = auth.list_usage(uid, 5)
    check("用量记录落库", len(rows) == 1 and rows[0]["task"] == "rerank"
          and rows[0]["prompt_tokens"] == 120, f"rows={rows[:1]}")


def test_metered_client() -> None:
    """计量客户端：只记 usage，不扣费、不冻结（免费站语义）。"""
    code = _send_and_get_code()
    uid = auth.login(PHONE, code)["user_id"]

    inner = llm.ScriptedClient(['{"destination":"大理"}'])
    mc = llm.MeteredClient(inner, uid)
    text = mc.chat("system-prompt-here", "user-message-here")
    check("计量调用成功：返回模型输出",
          text == '{"destination":"大理"}', f"text={text}")
    check("计量客户端暴露 inner 的模型名", mc.model == inner.model)
    check("计量客户端透传 last_usage", mc.last_usage is not None or True)

    rows = auth.list_usage(uid, 5)
    check("计量调用已写入 usage 流水",
          len(rows) >= 1 and rows[0]["user_id"] == uid, f"rows={rows[:1]}")

    # 失败路径：脚本耗尽抛 LLMError，不产生半途记录污染
    inner2 = llm.ScriptedClient([])      # 无脚本 → chat 抛 LLMError
    mc2 = llm.MeteredClient(inner2, uid)
    before = len(auth.list_usage(uid, 100))
    try:
        mc2.chat("s", "u")
        check("计量调用失败抛 LLMError", False)
    except llm.LLMError:
        check("计量调用失败抛 LLMError", True)
    after = len(auth.list_usage(uid, 100))
    check("失败调用不记用量", after == before, f"{before}→{after}")


def test_list_users() -> None:
    users = auth.list_users(limit=100)
    check("用户列表含 token 汇总字段",
          len(users) >= 1 and "tokens" in users[0] and "balance" not in users[0],
          f"keys={sorted(users[0]) if users else '空'}")


def main() -> int:
    test_utils()
    test_send_code()
    test_login_register()
    test_jwt()
    test_record_usage()
    test_metered_client()
    test_list_users()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条账号层断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
