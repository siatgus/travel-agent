"""线上冒烟自检：登录 → 规划 → 后续追问，走真实 HTTP + deepseek。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8787"
PHONE = "19900000000"


def post(path, data, token=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + token} if token else
                {"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def chat(sid, msg, token):
    r = post("/api/chat", {"sid": sid, "message": msg}, token)
    plan = r.get("plan")
    print("== 问:", msg)
    print("mode:", r.get("mode"), "| plan:", bool(plan),
          "| credits:", r.get("credits_left"), "| 工具调用:", r.get("tool_calls"))
    print("回:", (r.get("text") or "")[:400].replace("\n", " ⏎ "))
    for a in (r.get("assumptions") or []):
        print("  假设:", a)
    if plan:
        days = plan.get("days") or plan.get("itinerary") or []
        if isinstance(days, list) and days and isinstance(days[0], dict):
            for i, d in enumerate(days, 1):
                stops = [n.get("name", "?") for n in (d.get("nodes") or d.get("stops") or [])]
                print(f"  D{i}: {stops}")
    return r


# 1) 登录
code = post("/api/auth/send_code", {"phone": PHONE}).get("dev_code")
assert code, "拿不到演示验证码"
lg = post("/api/auth/login", {"phone": PHONE, "code": code})
tok = lg.get("access") or lg.get("access_token") or lg.get("token")
assert tok, "登录失败: " + json.dumps(lg, ensure_ascii=False)
print("登录 OK, uid:", lg.get("uid") or lg.get("user", {}).get("uid"))

sid = "smoke-%d" % __import__("time").time()
# 2) 规划
chat(sid, "成都出发去杭州玩3天，必去西湖、灵隐寺，轻松一点", tok)
# 3) 追问：别太赶
chat(sid, "还是有点赶，删掉一些吧", tok)
