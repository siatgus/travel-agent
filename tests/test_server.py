"""server.py 部署层测试：把「本地部署」也纳入断言范围。

背景：三个测试套件（eval / test_llm / test_dialogue）都覆盖不到部署层，
曾漏掉一个致命 bug——嵌在 Python 字符串里的页面 JS，因为 '\n' 被 Python
求值成真实换行符，送出去就是 SyntaxError，整页脚本死亡、所有按钮失灵。
当时的 node 校验查的是 Python 源码原文而非求值后的产物，所以没拦住。

本文件的防线：
1. 对**求值后**的 PAGE 提取 <script>，交给 node --check（有 node 才跑）；
2. 起真实服务，走一遍 HTTP 全链路：首页 / 健康 / 追问 / 出方案 / 行程页 / 重置；
3. 边界：空消息 400、未知 sid 的行程页 404。

运行：python test_server.py    失败时退出码为 1。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

# 景点介绍卡片要联网搜索，测试里一律关掉：不真出网、也不拖慢用例
os.environ["POI_LINKS"] = "0"
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import server as srv

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post(base: str, path: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def _login(base: str, phone: str) -> str:
    """走真实 HTTP 链路注册/登录一个测试用户（演示模式验证码回显）。"""
    st, j = _post(base, "/api/auth/send_code", {"phone": phone})
    assert st == 200 and j.get("dev_code"), f"send_code: {st} {j}"
    st, j = _post(base, "/api/auth/login", {"phone": phone, "code": j["dev_code"]})
    assert st == 200 and j.get("ok"), f"login: {st} {j}"
    return j["access"]


def _get_auth(base: str, path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path,
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def _delete(base: str, path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="DELETE",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def find_node() -> str | None:
    exe = shutil.which("node")
    if exe:
        return exe
    managed = Path(r"C:/Users/Administrator/.workbuddy/binaries/node/versions/"
                   r"22.22.2-6/node.exe")
    return str(managed) if managed.exists() else None


def test_page_served(base: str) -> None:
    status, html = _get(base, "/")
    check("首页 200 且是完整 HTML", status == 200 and "<!DOCTYPE html>" in html,
          f"status={status}")
    check("首页含应用标题", "智能旅行规划助手" in html)
    check("退出需二次确认：页面含确认弹窗与确认函数",
          'id="logoutmask"' in html and "confirmLogout()" in html
          and "确认退出登录？" in html)

    # 顶栏账号胶囊不能直接触发退出（防止误触即掉线）
    box = re.search(r'<div class="userwrap".*?</div>', html, re.S)
    check("顶栏账号区不绑定直接退出，只留独立「退出」按钮",
          'onclick="logout()"' not in html and 'onclick="askLogout()"' in html
          and box is not None and 'onclick="logout()"' not in box.group(0))
    check("点遮罩/按 Esc 视为取消",
          "if(e.target === this) closeLogout()" in html
          and "e.key === 'Escape'" in html)


def test_guest_mode(base: str) -> None:
    """未登录首屏不弹窗，只有用到功能（发送）时才要求登录。"""
    status, html = _get(base, "/")
    check("首屏不强制登录：boot 走访客态而非弹窗",
          "showGuest();" in html and "if(!token) showLogin();" not in html,
          "boot 里仍有强制弹窗" if "if(!token) showLogin();" in html else "")
    check("顶栏提供访客登录入口",
          'id="guestbar"' in html and '未登录 · 点此登录' in html)
    check("登录框可关闭（先看看，稍后登录）",
          "function closeLogin" in html and "先看看，稍后登录" in html)
    # 发送时才弹登录，并记住这句话，登录成功后补发
    check("点发送时才弹登录并暂存待发消息",
          "pendingMsg = msg;" in html and "showLogin('登录后立刻发送你刚才的消息" in html)
    check("登录成功后自动补发暂存消息", "send(queued);" in html)
    check("关闭登录框不偷偷补发", "pendingMsg = '';" in html)


def test_evaluated_js_syntax() -> None:
    """对求值后的 PAGE 做 JS 语法检查——查源码原文会漏掉 \n 求值类错误。"""
    node = find_node()
    if not node:
        print("[SKIP] 未找到 node，跳过 JS 语法检查")
        return
    js = re.search(r"<script>(.*?)</script>", srv.PAGE, re.S).group(1)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        check("求值后页面 JS 语法合法（防换行转义回退）", r.returncode == 0,
              r.stderr.strip()[:200])
    finally:
        Path(tmp).unlink(missing_ok=True)


def test_health(base: str) -> None:
    status, raw = _get(base, "/api/health")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    check("健康检查 200 且 ok", status == 200 and isinstance(body, dict)
          and body.get("ok") is True, f"status={status} body={raw[:80]}")


def test_clarify_flow(base: str, token: str) -> None:
    status, j = _post(base, "/api/chat",
                      {"sid": "t-clarify", "message": "想去云南玩"}, token=token)
    ok = (status == 200 and j.get("mode") == "clarify"
          and j.get("ask_rounds") == 1 and "大理" in j.get("text", ""))
    check("追问流程：省级目的地被拦截并列出可选城市", ok, f"status={status} j={j}")


def test_chat_no_auth(base: str) -> None:
    status, j = _post(base, "/api/chat", {"sid": "t-x", "message": "去大理"})
    ok = status == 401 and j.get("error") == "unauthorized"
    check("未登录调用 chat 被拒（401）", ok, f"status={status} j={j}")


def test_auth_endpoints(base: str) -> None:
    status, j = _post(base, "/api/auth/send_code", {"phone": "bad"})
    check("非法手机号 send_code 400", status == 400 and j.get("ok") is False,
          f"status={status}")
    status, j = _post(base, "/api/auth/login", {"phone": "13900002222", "code": "000000"})
    check("错误验证码登录 401", status == 401, f"status={status}")
    st, me = _get(base, "/api/me")
    check("无 token 查 /api/me 401", "401" in str(st) or st == 401, f"status={st}")


def test_plan_flow(base: str, token: str) -> None:
    status, j = _post(base, "/api/chat",
                      {"sid": "t-plan", "message": "11月8号去大理玩4天，2人，预算6000"},
                      token=token)
    text = j.get("text", "")
    ok = (status == 200 and j.get("mode") == "plan" and j.get("has_plan") is True
          # 没说出发地 → 不查往返班次，trace 是 4 次调用（天气/酒店/POI/预算）
          and j.get("tool_calls", 0) >= 4 and "预算合计" in text)
    check("出方案流程：真实工具调用与预算输出", ok, f"status={status} keys={list(j)}")
    # 用户要能一眼看到「从哪走、几个人、预算多少」，而不是从班次行反推
    check("方案文本带出行概要（出发地/人数/预算/天数）",
          "出行概要" in text and "出发地 → 目的地" in text
          and "2 人" in text and "4 天 3 晚" in text,
          text[:200])
    check("出方案响应带积分余额", isinstance(j.get("credits_left"), int)
          and j["credits_left"] >= 0, f"credits_left={j.get('credits_left')}")


def test_me(base: str, token: str) -> None:
    status, raw = _get(base, "/api/me")  # 无 token，应 401
    check("无 token /api/me 拒绝", status == 401, f"status={status}")
    req = urllib.request.Request(base + "/api/me",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read())
    check("/api/me 返回手机号与余额",
          j.get("ok") is True and "****" in j.get("phone", "")
          and isinstance(j.get("balance"), int), f"j={j}")


def test_unlimited_account(base: str) -> None:
    """不限额测试账号必须真的穿透 HTTP 链路：预检、计费、/api/me 标记。"""
    from travel_agent import auth
    phone = "19900001111"
    auth.create_unlimited(phone, credits=0)
    token = _login(base, phone)
    req = urllib.request.Request(base + "/api/me",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        me = json.loads(r.read())
    check("/api/me 标出不限额且余额可以是 0",
          me.get("unlimited") is True and me.get("balance") == 0, f"me={me}")
    status, j = _post(base, "/api/chat",
                      {"sid": "t-unl", "message": "一个人去大理玩"}, token=token)
    check("零余额的不限额账号仍能对话（MIN_BALANCE 预检被放行）",
          status == 200 and j.get("ok") is True, f"status={status} j={j}")
    check("对话响应带 unlimited 标记", j.get("unlimited") is True, f"j={j}")


def test_conversations_endpoints(base: str, token: str) -> None:
    """历史行程 HTTP 全链路：自动建档 / 详情回放 / 重启续聊 / 落库兜底 / 删除。"""
    # 出方案后的 t-plan 应已自动建档（前面 test_plan_flow 的产物）
    st, j = _get_auth(base, "/api/conversations", token)
    item = next((i for i in (j.get("items") or []) if i["sid"] == "t-plan"), None)
    check("历史行程列表 200 且出方案后自动建档",
          st == 200 and j.get("ok") and item is not None and item.get("has_plan"),
          f"status={st} items={j.get('items')}")
    st, j = _get_auth(base, "/api/conversations/t-plan", token)
    msgs = j.get("messages") or []
    check("行程详情含完整对话内容（用户原话 + 助手回复）",
          st == 200 and any(m["role"] == "user" and "大理" in m["content"] for m in msgs)
          and any(m["role"] == "bot" for m in msgs),
          f"status={st} n_msgs={len(msgs)}")
    st, _ = _get(base, "/api/conversations")
    check("未登录拉历史行程 401", st == 401, f"status={st}")
    st, _ = _get_auth(base, "/api/conversations/t-nothing", token)
    check("不存在的对话详情 404", st == 404, f"status={st}")

    # 重启续聊：清掉内存会话（模拟进程重启），下一句按存档接着排
    with srv._LOCK:
        srv._SESSIONS.clear()
    st, j = _post(base, "/api/chat",
                  {"sid": "t-plan", "message": "预算改成8000"}, token=token)
    slots = j.get("slots") or {}
    check("重启后按存档续聊：改预算仍落在同一次行程上",
          st == 200 and j.get("ok") and slots.get("budget_total") == 8000
          and slots.get("destination") == "大理", f"status={st} slots={slots}")

    # 行程页兜底：内存方案丢了，从库里捞
    with srv._LOCK:
        srv._LAST_PLAN.clear()
    st, html = _get(base, "/plan/t-plan")
    check("行程页在内存丢失后从存档恢复", st == 200 and "预算合计" in html,
          f"status={st}")

    # 用户隔离：别人看不到这条对话
    other = _login(base, "13700009999")
    st, _ = _get_auth(base, "/api/conversations/t-plan", other)
    check("别人的对话详情 404", st == 404, f"status={st}")

    # 新用户没有历史：首轮对话即自动建档
    t2 = _login(base, "13600007777")
    st, _ = _get_auth(base, "/api/conversations", t2)
    st2, j2 = _get_auth(base, "/api/conversations", t2)
    check("新用户初始无历史行程", st2 == 200 and not (j2.get("items") or []),
          f"items={j2.get('items')}")
    _post(base, "/api/chat", {"sid": "t-new", "message": "11月20号去厦门玩3天"},
          token=t2)
    st, j = _get_auth(base, "/api/conversations", t2)
    check("新用户首轮对话自动建档保存",
          st == 200 and any(i["sid"] == "t-new" for i in (j.get("items") or [])),
          f"items={j.get('items')}")

    # 删除历史行程：记录、行程页一起消失
    st, j = _delete(base, "/api/conversations/t-plan", token)
    check("删除历史行程成功", st == 200 and j.get("deleted") is True, f"j={j}")
    st, _ = _get(base, "/plan/t-plan")
    check("删除后行程页 404", st == 404, f"status={st}")
    st, j = _get_auth(base, "/api/conversations", token)
    check("删除后列表里也没了",
          all(i["sid"] != "t-plan" for i in (j.get("items") or [])))


def test_plan_page(base: str) -> None:
    status, html = _get(base, "/plan/t-plan")
    check("行程页 200 且含预算表", status == 200 and "预算合计" in html,
          f"status={status}")
    check("行程页出行概要写明出发地/日期/人数/预算目标",
          "出行概要" in html and "出发地 → 目的地" in html
          and "出行人数" in html and "预算目标" in html,
          f"status={status}")


def test_reset(base: str, token: str) -> None:
    _post(base, "/api/reset", {"sid": "t-plan"}, token=token)
    status, _ = _get(base, "/plan/t-plan")
    check("重置后行程页清空（404）", status == 404, f"status={status}")


def test_bad_request(base: str, token: str) -> None:
    status, _ = _post(base, "/api/chat", {"sid": "t-bad", "message": ""}, token=token)
    check("空消息返回 400", status == 400, f"status={status}")


def test_drive_prefetch() -> None:
    """动线预热：路段提取与前端 chain 规则一致、坏坐标不进、uncertain 不进。

    真实浏览器 fetch 每段 4~10s 时会全线超时回落直线（用户实拍南京方案
    全直线）——预热把天地图上游调用挪到服务端后台，前端 fetch 毫秒级命中。
    """
    from travel_agent.server import _drive_pairs, _ok_pt
    check("_ok_pt 舍入到 6 位", _ok_pt("118.8405123,32.055123") == "118.840512,32.055123",
          str(_ok_pt("118.8405123,32.055123")))
    check("_ok_pt 拒绝坏值", _ok_pt("bad") is None and _ok_pt("999,999") is None, "")
    result = {"days_plan": [
        {"day": 1, "nodes": [
            {"name": "A", "geo": [32.05, 118.84], "type": "attraction"},
            {"name": "午休", "geo": [32.051, 118.841], "type": "rest"},
            {"name": "B", "geo": [32.06, 118.85], "type": "attraction"},
            {"name": "估算点", "geo": [32.07, 118.86], "type": "attraction",
             "geo_uncertain": True},
        ]},
        {"day": 2, "nodes": [
            {"name": "C", "geo": [32.08, 118.87], "type": "attraction"},
            {"name": "无坐标", "type": "attraction"},
            {"name": "D", "geo": [32.09, 118.88], "type": "attraction"},
        ]},
    ]}
    pairs = _drive_pairs(result)
    check("每天相邻可靠点成对（跨 uncertain/无坐标不连）",
          pairs == [("118.84,32.05", "118.841,32.051"),
                    ("118.841,32.051", "118.85,32.06"),
                    ("118.87,32.08", "118.88,32.09")], str(pairs))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="travel_server_test_"))
    os.environ["TRAVEL_APP_DB"] = str(tmp / "app.db")   # 测试账号库隔离

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    print(f"（测试服务已起：{base}）\n")

    test_page_served(base)
    test_guest_mode(base)
    test_evaluated_js_syntax()
    test_health(base)
    test_auth_endpoints(base)
    test_chat_no_auth(base)
    token = _login(base, "13800001111")
    test_me(base, token)
    test_unlimited_account(base)
    test_clarify_flow(base, token)
    test_plan_flow(base, token)
    test_plan_page(base)
    test_drive_prefetch()
    test_conversations_endpoints(base, token)
    test_reset(base, token)
    test_bad_request(base, token)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条部署断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
