"""多轮对话入口：真正和人聊起来，而不是只跑测试。

用法：
    python chat.py                                    # 交互模式，/bye 退出
    python chat.py --script "想去云南|大理吧|4天，2人，预算6000"
    python chat.py --llm                              # 用真实模型做抽取（需 OPENAI_API_KEY）

每一轮都会显示：这轮是追问还是出方案、当前槽位、以及系统替你做了哪些假设。
"""

from __future__ import annotations

import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")


# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))
from travel_agent.dialogue import Session, PreferenceMemory

DIM = "\033[2m"
RESET = "\033[0m"


def show(reply, verbose: bool) -> None:
    if reply.mode == "clarify":
        print(f"\n{reply.question}")
        if verbose:
            print(f"{DIM}  [追问 {reply.ask_rounds}/2 · 问到 {reply.asked}]{RESET}")
        return

    print()
    print(reply.render)
    if verbose:
        b = reply.plan["budget"]
        print(f"{DIM}  [槽位] {json.dumps({k: v for k, v in reply.slots.items() if not k.startswith('_')}, ensure_ascii=False)}{RESET}")
        print(f"{DIM}  [工具] {len(reply.trace)} 次调用 · 合计 {b['total']} 元{RESET}")
        if reply.plan.get("decision_log", {}).get("vetoed"):
            vetoed = {v["name"] for v in reply.plan["decision_log"]["vetoed"]}
            print(f"{DIM}  [否决] {'、'.join(vetoed)}{RESET}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", help="用 | 分隔的多轮用户输入，一次跑完")
    ap.add_argument("--llm", action="store_true", help="用真实模型做槽位抽取（需 OPENAI_API_KEY）")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示槽位、工具次数、否决项")
    ap.add_argument("--memory", help="偏好记忆文件路径（默认不持久化）")
    a = ap.parse_args()

    client = None
    if a.llm:
        import llm as llm_layer
        client = llm_layer.from_env()
        if client is None:
            print("未配置 OPENAI_API_KEY，改用规则抽取。\n")

    mem = PreferenceMemory(a.memory) if a.memory else None
    session = Session(client=client, memory=mem)

    if a.script:
        for line in a.script.split("|"):
            line = line.strip()
            if not line:
                continue
            print(f"\n\033[1m[你] {line}{RESET}")
            show(session.turn(line), a.verbose)
        return 0

    print("说明你想去哪儿玩。输入 /bye 结束，/slots 查看当前槽位。\n")
    while True:
        try:
            line = input("\033[1m[你] \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/bye", "/exit", "退出"):
            break
        if line == "/slots":
            print(json.dumps({k: v for k, v in session.slots.items()
                              if not k.startswith("_")}, ensure_ascii=False, indent=2))
            continue
        show(session.turn(line), a.verbose)

    print("\n会话结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
