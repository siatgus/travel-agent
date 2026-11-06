"""智能体多轮文本上下文测试：走真实 HTTP 链路（server.py 8787）。

场景：
1. 首轮残缺信息 → 追问 → 次轮补齐（此前槽位必须保留）
2. 跨轮显式修改：改预算/改天数（只覆盖被改字段）
3. 指代切换目的地：「还是去丽江吧」（目的地覆盖，其余保留）
4. 追问去重：模糊回答不触发重复追问同一槽位
5. 重开新会话（reset）后上下文清空
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
BASE = os.environ.get("CTX_BASE", "http://127.0.0.1:8787")
SID = "ctx-test"


def _login() -> str:
    def post(path, payload):
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    j = post("/api/auth/send_code", {"phone": "13700003333"})
    j = post("/api/auth/login", {"phone": "13700003333", "code": j["dev_code"]})
    assert j["ok"], j
    return j["access"]


TOKEN = _login()


def chat(message: str) -> dict:
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"sid": SID, "message": message}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def slots_of(j: dict) -> dict:
    s = j.get("slots") or {}
    return {k: v for k, v in s.items()
            if k in ("destination", "origin", "days", "party_size",
                     "budget_total", "budget_per_person", "date_range")}


def show(step: str, j: dict) -> None:
    mode = j["mode"]
    head = (j.get("question") or j["text"]).split("\n")[0][:52]
    print(f"  [{mode}] {head}")
    print(f"      slots: {json.dumps(slots_of(j), ensure_ascii=False)}")


print("== 场景1：残缺 → 追问 → 补齐，此前槽位保留 ==")
j = chat("11月8号去大理玩4天")
show("T1", j)
j = chat("2个人，预算5000")
show("T2", j)
s = slots_of(j)
assert s.get("destination") == "大理" and s.get("days") == 4, "此前槽位丢失"
assert j["mode"] == "plan", "信息齐了应直接出方案"
print("  ✓ 目的地/天数跨轮保留，补齐后直接出方案\n")

print("== 场景2：出方案后跨轮显式修改预算与天数 ==")
j = chat("预算改成4000，改成5天")
show("T3", j)
s = slots_of(j)
assert s.get("budget_total") == 4000 and s.get("days") == 5, "修改未生效"
assert s.get("destination") == "大理", "不该动的字段被动了"
print("  ✓ 只覆盖被改字段（预算4000/5天），目的地不动\n")

print("== 场景3：指代切换目的地 ==")
j = chat("还是去丽江吧")
show("T4", j)
s = slots_of(j)
assert s.get("destination") == "丽江", "目的地未切换"
assert s.get("days") == 5 and s.get("budget_total") == 4000, "其余槽位应保留"
print("  ✓ 目的地切到丽江，天数/预算原样保留\n")

print("== 场景4：追问去重（同槽位不重复问） ==")
j = chat("新话题：想去云南玩")
show("T5", j)
asked1 = j.get("asked") or []
j = chat("就说大概吧")
show("T6", j)
asked2 = j.get("asked") or []
overlap = set(asked1 or []) & set(asked2 or [])
print(f"  两轮追问槽位：{asked1} → {asked2}，重叠={overlap or '无'}")
print("  ✓" if not overlap or j["mode"] == "plan" else "  ✗ 重复追问", "\n")

print("== 场景5：reset 后上下文清空 ==")
req = urllib.request.Request(BASE + "/api/reset",
                             data=json.dumps({"sid": SID}).encode(),
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {TOKEN}"})
urllib.request.urlopen(req, timeout=10).read()
j = chat("预算5000")
show("T7", j)
s = slots_of(j)
assert s.get("destination") is None and "大理" not in json.dumps(s), "reset 后仍有残留"
print("  ✓ 新会话干净，无上一段对话残留")

print("\n全部 5 个上下文场景测试完成")
