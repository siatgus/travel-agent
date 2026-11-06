"""真实模型接入检查：验证"接上模型之后"是否真的比规则更强。

它不是单元测试（结果不确定），而是一次**冒烟 + 体检**：
用 4 类典型输入跑真实模型，逐条打印抽取结果、尝试次数、降级原因，
最后给出通过/需调整的判定。目的是在改成线上之前，先看清模型的真实表现，
而不是等用户第一次使用时才发现它把"下个月"算成了去年。

用法：
    export OPENAI_API_KEY=sk-...
    python live_check.py                 # 真实调用
    python live_check.py --dry-run       # 不联网，用回放样本自检 harness

--dry-run 的意义：先证明校验层本身没问题，再去怀疑模型。
否则模型表现差时，你无法区分是 prompt 差还是代码差。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent import llm

TODAY = date(2026, 10, 29)

# (用户原话, 期望命中的关键字段, 说明)
CASES = [
    ("我和老婆11月8号从杭州出发去大理，玩4天，预算一人3000，不想太赶，我老婆不吃辣",
     {"destination": "大理", "days": 4, "party_size": 2, "pace": "relaxed"},
     "完整需求：应全部抽准，hard_prefs 含不吃辣"),
    ("下个月想去云南玩几天",
     {"destination": "云南"},
     "模糊需求：目的地明说应保留，但天数与精确日期必须留空"),
    ("带孩子（3岁）去大理玩4天，11月8号出发，预算8000",
     {"hard_prefs": ["带幼儿"], "days": 4},
     "隐性约束：带 3 岁孩子应映射为 带幼儿"),
    ("11月8号从杭州去大理玩4天，2人，总共7000，我恐高不能上高海拔",
     {"hard_prefs": ["忌高海拔"], "budget_total": 7000},
     "口语化硬约束：恐高 → 忌高海拔"),
]

# 回放样本：模拟一个"基本合格但有小毛病"的模型，用来验证 harness 本身
REPLAY = [
    # 用例 1：正确，但数字写成字符串、输出被围栏包住
    '```json\n{"destination":"大理","origin":"杭州",'
    '"date_range":{"start":"2026-11-08"},"days":"4","party_size":2,'
    '"budget_total":null,"budget_per_person":3000,"hard_prefs":["不吃辣"],'
    '"soft_prefs":[],"must_visit":[],"pace":"relaxed","uncertain_fields":[]}\n```',
    # 用例 2：正确留空，不编造
    '{"destination":"云南","origin":null,"date_range":{"month":"2026-11"},'
    '"days":null,"party_size":1,"budget_total":null,"budget_per_person":null,'
    '"hard_prefs":[],"soft_prefs":[],"must_visit":[],"pace":"moderate",'
    '"uncertain_fields":["days","date_range"]}',
    # 用例 3：编了一个不存在的硬约束，应被丢弃并告警
    '{"destination":"大理","origin":null,"date_range":{"start":"2026-11-08"},'
    '"days":4,"party_size":2,"budget_total":8000,"budget_per_person":null,'
    '"hard_prefs":["带幼儿","怕冷"],"soft_prefs":[],"must_visit":[],'
    '"pace":"moderate","uncertain_fields":[]}',
    # 用例 4：第一次把过去年份写错 → 应触发重问 → 修正
    '{"destination":"大理","origin":"杭州","date_range":{"start":"2025-11-08"},'
    '"days":4,"party_size":2,"budget_total":7000,"budget_per_person":null,'
    '"hard_prefs":["忌高海拔"],"soft_prefs":[],"must_visit":[],'
    '"pace":"moderate","uncertain_fields":[]}',
    '{"destination":"大理","origin":"杭州","date_range":{"start":"2026-11-08"},'
    '"days":4,"party_size":2,"budget_total":7000,"budget_per_person":null,'
    '"hard_prefs":["忌高海拔"],"soft_prefs":[],"must_visit":[],'
    '"pace":"moderate","uncertain_fields":[]}',
    # 重排：用户说想环洱海 → 洱海系提到最前
    '{"order":["P2","P3","P6","P5"]}',
]


def show(tag: str, slots: dict | None, rec) -> None:
    if slots is None:
        print(f"  {tag} 降级 → {rec.degraded}")
        return
    keys = ("destination", "date_range", "days", "party_size",
            "budget_total", "hard_prefs", "pace")
    brief = {k: slots.get(k) for k in keys}
    print(f"  {tag} 尝试 {rec.attempts} 次 / {rec.latency_ms}ms："
          f"{json.dumps(brief, ensure_ascii=False)}")
    for w in rec.warnings:
        print(f"      告警：{w}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="用回放样本自检，不联网")
    ap.add_argument("--model")
    a = ap.parse_args()

    if a.dry_run:
        client = llm.ScriptedClient(REPLAY)
        print("=== 回放模式（不联网）：先确认校验层本身是通的 ===\n")
    else:
        client = llm.from_env(**({"model": a.model} if a.model else {}))
        if client is None:
            print("未配置 OPENAI_API_KEY。\n"
                  "  真实调用：export OPENAI_API_KEY=sk-...\n"
                  "  只看离线自检：python live_check.py --dry-run")
            return 2
        print(f"=== 真实模型：{client.model} @ {client.base_url} ===\n")

    passed = 0
    for text, expect, note in CASES:
        print(f"「{text}」")
        print(f"  预期：{note}")
        slots, rec = llm.extract_slots_llm(client, text, TODAY)
        show("结果", slots, rec)

        ok = slots is not None
        if ok:
            for k, v in expect.items():
                actual = slots.get(k)
                if isinstance(v, list):
                    ok = ok and all(x in (actual or []) for x in v)
                elif v is not None:
                    ok = ok and actual == v
        # 模糊需求必须保持"问"的状态：天数与精确日期都不能编造
        if "云南" in text:
            dr = slots.get("date_range") or {}
            ok = ok and not slots["days"] and not dr.get("exact")
        print(f"  {'✓ 达标' if ok else '✗ 需调整 prompt 或校验规则'}\n")
        passed += ok

    # 重排：用第一条用例的槽位，看模型是否把用户偏好排到前面
    print("=== 重排：用户明说想环洱海 ===")
    base = {"destination": "大理", "days": 4, "party_size": 2, "pace": "moderate",
            "hard_prefs": [], "soft_prefs": ["洱海", "骑行"], "must_visit": []}
    cands = [
        {"poi_id": "P5", "name": "苍山洗马潭索道", "tags": ["苍山", "高海拔", "户外"],
         "intensity": "high", "duration_min": 240, "cost": 280, "_score": 12.0},
        {"poi_id": "P6", "name": "崇圣寺三塔", "tags": ["古建", "人文"],
         "intensity": "low", "duration_min": 120, "cost": 75, "_score": 9.0},
        {"poi_id": "P2", "name": "洱海生态廊道（才村段）", "tags": ["洱海", "骑行", "户外"],
         "intensity": "medium", "duration_min": 180, "cost": 60, "_score": 8.5},
        {"poi_id": "P3", "name": "喜洲古镇", "tags": ["古镇", "白族", "洱海"],
         "intensity": "low", "duration_min": 120, "cost": 0, "_score": 8.0},
    ]
    order, rec = llm.rerank_llm(client, cands, base)
    if order is None:
        print(f"  降级 → {rec.degraded}")
    else:
        names = {c["poi_id"]: c["name"] for c in cands}
        print("  模型排序：" + " → ".join(names.get(p, p) for p in order))
        top = order[0]
        hit = top in {"P2", "P3"}
        print(f"  {'✓ 洱海系景点被排到首位' if hit else '△ 首位不是洱海系，检查 prompt 是否强调了偏好权重'}")
        passed += hit

    total = len(CASES) + 1
    print(f"\n{passed}/{total} 达标")
    if not a.dry_run and passed < total:
        print("提示：模型表现不达标时先看 warnings 与降级原因，再调 SLOT_SYSTEM / RERANK_SYSTEM 的措辞。")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
