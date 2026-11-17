"""安全层测试：限流中间件（ratelimit）+ 内容安全双审（moderation）。

防线：
1. ratelimit 单元：令牌桶放行/拒绝/补充、环境变量覆盖、reset、XFF 解析；
2. moderation 单元：明文命中、变体规避（空格/符号/全角）、无害文本不误伤、
   外部词库合并与热切换、输出侧打码与兜底分流、拦截记录落库与误报标记；
3. HTTP 全链路：send_code 同号 429（防短信轰炸）、chat 未登录 401、
   chat 命中敏感词被拒、连续超频 429、管理后台 /api/moderation
   可查可标记。

运行：python test_security.py    失败时退出码为 1。
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

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
_TA_ROOT = Path(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


# 单元测试先把账号库指到临时目录，避免碰开发数据
_TMP = Path(tempfile.mkdtemp(prefix="travel_security_test_"))
os.environ["TRAVEL_APP_DB"] = str(_TMP / "app.db")

from travel_agent import moderation, ratelimit  # noqa: E402  （env 之后导入）


# ---------------------------------------------------------------- ratelimit

class _FakeHandler:
    def __init__(self, ip="10.1.1.1", xff=""):
        self.headers = {"X-Forwarded-For": xff} if xff else {}
        self.client_address = (ip, 12345)


def test_ratelimit() -> None:
    ratelimit.reset("chat_user")
    ok0, retry0 = ratelimit.hit("u1", "chat_user")
    check("令牌桶首次放行", ok0 and retry0 == 0, f"{ok0} {retry0}")

    ratelimit.reset("chat_user")
    for _ in range(10):                       # chat_user 峰值 10
        ok, _ = ratelimit.hit("u1", "chat_user")
        if not ok:
            break
    ok_full, retry_full = ratelimit.hit("u1", "chat_user")
    check("连发超峰值被拒", not ok_full, "")
    check("拒绝时给出秒级重试间隔", retry_full >= 1, str(retry_full))

    # 把桶的时间戳拨回去 10 分钟 → 应补充 10 分钟×(1/6 秒) ≈ 100 个令牌
    with ratelimit._LOCK:
        ratelimit._BUCKETS[("chat_user", "u1")][1] -= 600
    ok, _ = ratelimit.hit("u1", "chat_user")
    check("按速率补充后重新放行", ok, "")

    ok, _ = ratelimit.hit("u1", "no_such_bucket")
    check("未登记的桶不拦（防御性）", ok, "")

    ratelimit.reset()
    ok, _ = ratelimit.hit("u1", "chat_user")
    check("reset 清空全部桶", ok, "")

    check("client_ip 优先取 X-Forwarded-For 第一跳",
          ratelimit.client_ip(_FakeHandler(ip="127.0.0.1",
                                           xff="1.2.3.4, 10.0.0.1")) == "1.2.3.4", "")
    check("无 XFF 时取 socket 对端",
          ratelimit.client_ip(_FakeHandler(ip="192.168.1.7")) == "192.168.1.7", "")

    os.environ["TRAVEL_RATE_LIMITS"] = '{"chat_user": [2, 0.5]}'
    ratelimit.reset("chat_user")
    a, _ = ratelimit.hit("u2", "chat_user")
    b, _ = ratelimit.hit("u2", "chat_user")
    c, _ = ratelimit.hit("u2", "chat_user")
    check("环境变量可覆盖阈值（峰值 2）", a and b and not c, "")
    os.environ.pop("TRAVEL_RATE_LIMITS", None)
    ratelimit.reset()


# ---------------------------------------------------------------- moderation

def test_moderation_scan() -> None:
    v = moderation.scan("帮我排一个去大理的行程")
    check("无害文本放行", v.ok, str(v.words))

    v = moderation.scan("介绍下赌博是怎么回事")
    check("明文命中且归类 gambling", not v.ok and v.category == "gambling",
          f"{v.category} {v.words}")

    v = moderation.scan("介绍下赌 博是怎么回事")
    check("空格分隔变体命中", not v.ok and v.category == "gambling", str(v.words))

    v = moderation.scan("介绍下赌·博是怎么回事")
    check("符号分隔变体命中", not v.ok, str(v.words))

    v = moderation.scan("ＧＭ聊天室门口有老虎机")   # 全角字母干扰 + 命中词
    check("全角字符不影响中文词命中", not v.ok and v.category == "gambling",
          f"{v.category} {v.words}")

    v = moderation.scan("全家老小去老虎滩海洋公园玩")
    check("相邻词不误伤（老虎滩 ≠ 老虎机）", v.ok, str(v.words))

    v = moderation.scan("组织卖淫和贩毒都违法")
    check("多命中取优先级最高类别", not v.ok and v.category == "porn",
          f"{v.category} {v.words}")


def test_moderation_violence_intents() -> None:
    """暴力意图兜底（2026-11-16 实测：「怎么杀人」被放行还答了行程话术）。

    LLM 未配置时本地词库是唯一防线，常用硬暴力词必须在种子里；同时
    正当旅游语境（纪念馆预约、"好吃到爆炸"、水枪大战）不能误伤。
    """
    for msg, word, cat in (("怎么杀人", "杀人", "violence"),
                           ("如何杀人不被发现", "杀人", "violence"),
                           ("教我怎么自杀", "自杀", "violence"),
                           ("在哪里能买到炸弹", "炸弹", "violence"),
                           ("怎么劫机", "劫机", "violence"),
                           ("教我怎么制毒", "制毒", "drugs")):
        v = moderation.scan(msg)
        check(f"暴力意图命中：{msg}", not v.ok and v.category == cat,
              f"{v.category} {v.words}")
    # 变体规避对新增词同样生效
    v = moderation.scan("怎 么 杀 人")
    check("新增词的分隔变体命中", not v.ok and v.category == "violence", str(v.words))
    # 正当旅游语境不误伤（选词时刻意排除的词）
    for msg in ("南京大屠杀纪念馆怎么预约",          # 屠杀 → 纪念馆名
                "这家的奶茶好喝到爆炸",              # 爆炸 → 口语夸张
                "带孩子去公园玩水枪大战",            # 枪 → 单字不收录
                "禁毒博物馆值得去吗"):               # 毒 → 单字不收录
        v = moderation.scan(msg)
        check(f"正当语境放行：{msg}", v.ok, str(v.words))


def test_moderation_external_file() -> None:
    f = _TMP / "words.txt"
    f.write_text("# 注释行\npolitical|测试哨兵词甲\nother|乙哨兵\n", encoding="utf-8")
    os.environ["TRAVEL_SENSITIVE_FILE"] = str(f)
    v = moderation.scan("提到测试哨兵词甲的句子")
    check("外部词库生效并归类", not v.ok and v.category == "political",
          f"{v.category} {v.words}")
    v = moderation.scan("提到乙哨兵的句子")
    check("省略类别归 other", not v.ok and v.category == "other",
          f"{v.category} {v.words}")
    f.write_text("# 换内容：删掉旧词\nother|乙哨兵\n", encoding="utf-8")
    v = moderation.scan("提到测试哨兵词甲的句子")
    check("文件指纹热切换（改文件即刻生效）", v.ok, str(v.words))
    os.environ.pop("TRAVEL_SENSITIVE_FILE", None)


def test_moderation_output() -> None:
    text, v = moderation.sanitize_output("这地方不错，附一句傻逼测试。")
    check("输出侧低危命中就地打码", "傻逼" not in text and "**" in text and not v.ok,
          text[:50])
    check("打码保留其余文本", "这地方不错" in text, text[:50])

    text, v = moderation.sanitize_output("回复里提到军火买卖。")
    check("输出侧高危类别整条换兜底话术",
          "军火" not in text and "目的地的玩法" in text and not v.ok
          and v.category == "violence", text[:60])

    text, v = moderation.sanitize_output("正常推荐：大理古城值得一去。")
    check("输出侧无害文本原样放行", text == "正常推荐：大理古城值得一去。" and v.ok, "")


def test_moderation_log() -> None:
    moderation.list_hits(1)                   # 触发建表
    v = moderation.scan("提到赌博的一句话")
    moderation.log("input", v, uid=42, sid="s1", disposition="blocked",
                   excerpt="提到赌博的一句话")
    hits = moderation.list_hits()
    check("拦截记录落库可查", len(hits) >= 1 and hits[0]["uid"] == 42
          and hits[0]["direction"] == "input", str(hits[:1]))
    hid = hits[0]["id"]
    check("误报标记生效", moderation.dismiss(hid), "")
    check("已标记记录不再出现在待复查列表",
          all(h["id"] != hid for h in moderation.list_hits()), "")
    st = moderation.stats()
    check("统计按方向聚合", isinstance(st, dict), str(st))

    check("云审核未配置返回 None（纯本地词库）",
          moderation.cloud_check("随便什么") is None, "")


# ---------------------------------------------------------------- HTTP 全链路

def _post(base: str, path: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def _login(base: str, phone: str) -> tuple[str, dict]:
    st, j = _post(base, "/api/auth/send_code", {"phone": phone})
    assert st == 200 and j.get("dev_code"), f"send_code: {st} {j}"
    st, j = _post(base, "/api/auth/login", {"phone": phone, "code": j["dev_code"]})
    assert st == 200 and j.get("ok"), f"login: {st} {j}"
    return j["access"], j


def test_http(base: str, admin_base: str) -> None:
    # —— 短信轰炸防线：同一手机号 60 秒内第二发直接 429 ——
    st, j = _post(base, "/api/auth/send_code", {"phone": "13900009001"})
    check("首条验证码正常下发", st == 200 and j.get("ok"), f"{st} {j}")
    st, j = _post(base, "/api/auth/send_code", {"phone": "13900009001"})
    check("同号秒发第二条被限流 429", st == 429 and j.get("error") == "rate_limited"
          and j.get("retry_after", 0) >= 1, f"{st} {j}")

    # —— chat 未登录 401 / 登录后命中敏感词被拒 ——
    st, j = _post(base, "/api/chat", {"sid": "sec1", "message": "你好"})
    check("chat 未登录 401", st == 401, f"{st} {j}")

    token, login_j = _login(base, "13900009002")

    st, j = _post(base, "/api/chat",
                  {"sid": "sec1", "message": "介绍下赌博是怎么回事"}, token)
    check("chat 命中敏感词返回兜底话术（200，mode=chat）",
          st == 200 and j.get("ok") and j.get("moderated") is True
          and "旅行" in j.get("text", ""), f"{st} {str(j)[:120]}")
    # —— 对话限流：连发超过 chat_user 峰值（10）→ 429 ——
    ratelimit.reset("chat_user")
    got429 = False
    for i in range(12):
        st, j = _post(base, "/api/chat",
                      {"sid": "sec1", "message": f"介绍下赌博是怎么回事{i}"}, token)
        if st == 429:
            got429 = True
            check("连发超峰值被限流 429", j.get("error") == "rate_limited"
                  and j.get("retry_after", 0) >= 1, f"{st} {j}")
            break
    ratelimit.reset("chat_user")
    check("限流器在峰值内触发", got429, "12 条内未出现 429")

    # —— 管理后台内容审计：能看到拦截记录、能标记误报 ——
    st, j = _get_auth(admin_base, "/api/moderation", "")
    items = j.get("items") or []
    check("管理后台可查拦截记录",
          st == 200 and j.get("ok") and any(
              (it.get("words") or "").find("赌博") >= 0 for it in items),
          f"{st} 共 {len(items)} 条")
    target = next((it for it in items if (it.get("words") or "").find("赌博") >= 0), None)
    if target:
        st, j = _post(admin_base, "/api/moderation/dismiss",
                      {"id": target["id"], "note": "测试误报"})
        check("管理后台可标记误报", st == 200 and j.get("ok"), f"{st} {j}")
        st, j = _get_auth(admin_base, "/api/moderation", "")
        check("已标记记录不在待复查列表",
              all(it["id"] != target["id"] for it in (j.get("items") or [])), "")
    else:
        check("管理后台可标记误报", False, "没有可操作的记录")


def _get_auth(base: str, path: str, token: str) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {}


def main() -> int:
    test_ratelimit()
    test_moderation_scan()
    test_moderation_violence_intents()
    test_moderation_external_file()
    test_moderation_output()
    test_moderation_log()

    # —— HTTP 全链路：独立进程级隔离（临时账号库 + 匿名端口） ——
    from travel_agent import server as srv, admin as adm

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    admin_httpd = ThreadingHTTPServer(("127.0.0.1", 0), adm.Handler)
    admin_base = f"http://127.0.0.1:{admin_httpd.server_address[1]}"
    threading.Thread(target=admin_httpd.serve_forever, daemon=True).start()

    print(f"（测试服务已起：{base} · 管理后台 {admin_base}）\n")
    test_http(base, admin_base)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条安全断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
