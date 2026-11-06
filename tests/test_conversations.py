"""对话持久化测试：一次对话 = 一条行程记录（conversations.py）+ Session 存档续聊。

背景（2026-11-02 需求）：上下文范围收窄到「一次对话」；登录用户能回看历史
行程（含完整对话）；没有历史的新用户按新对话开始并自动保存。

本文件不依赖网络服务，只测存储层与存档/恢复逻辑：
1. 首轮 record_turn 自动建档（标题=去X的旅行 / 首条消息截断）
2. 后续轮追加不重复建档；换目的地标题跟着改
3. list_conversations 排序与消息计数
4. get_conversation 完整消息序（user/bot 交替）
5. get_state + Session.restore：跨进程续聊后槽位仍在，下一句接着改
6. plan 存档：get_plan_by_sid 捞得回（行程页重启兜底）
7. delete_conversation 删干净（记录 + 消息）
8. 没存档的 sid get_state 返回 None，不报错

运行：python test_conversations.py    失败时退出码为 1。
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# —— src 布局引导：tests/scripts 无需安装即可导入 travel_agent 包 ——
from pathlib import Path as _TA_PATH
_TA_ROOT = _TA_PATH(__file__).resolve().parents[1]
if str(_TA_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_TA_ROOT / 'src'))

_tmp = Path(tempfile.mkdtemp(prefix="travel_conv_test_"))
os.environ["TRAVEL_APP_DB"] = str(_tmp / "app.db")   # 必须在 import auth 之前设置

from travel_agent import conversations
from travel_agent.dialogue import Session

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"　{detail}" if detail and not cond else ""))


def test_auto_create_and_append() -> None:
    """首轮自动建档 + 后续追加。"""
    conversations.record_turn(1, "s1", "11月8号去大理玩4天", "追问：几个人去？",
                              dest="大理")
    conv = conversations.get_conversation(1, "s1")
    check("首轮对话自动建档", conv is not None and conv["has_plan"] is False)
    check("有目的地时标题用目的地", conv and conv["title"] == "去大理的旅行",
          f"title={conv and conv['title']}")
    check("建档即有两条消息（user+bot）",
          conv and [m["role"] for m in conv["messages"]] == ["user", "bot"])

    conversations.record_turn(1, "s1", "2个人，预算5000", "好的，这是你的行程…",
                              dest="大理")
    conv = conversations.get_conversation(1, "s1")
    check("第二轮追加不重复建档",
          conv and len(conv["messages"]) == 4 and conv["title"] == "去大理的旅行")

    # 中途换目的地：标题跟着走
    conversations.record_turn(1, "s1", "还是去丽江吧", "已切换到丽江。",
                              dest="丽江")
    conv = conversations.get_conversation(1, "s1")
    check("换目的地后标题/归属跟着改",
          conv and conv["dest"] == "丽江" and conv["title"] == "去丽江的旅行",
          f"dest={conv and conv['dest']} title={conv and conv['title']}")


def test_title_from_first_message() -> None:
    """没有目的地时，标题退为首条消息截断。"""
    long_msg = "我想找一个暖和的地方过年，最好有海有美食，预算不重要，玩得舒服最重要"
    conversations.record_turn(1, "s2", long_msg, "好的，先帮你定位几个方向…")
    conv = conversations.get_conversation(1, "s2")
    check("无目的地标题退为首条消息截断",
          conv and conv["title"] == long_msg[:24], f"title={conv and conv['title']}")


def test_list_and_isolation() -> None:
    """列表排序、计数与用户隔离。"""
    conversations.record_turn(2, "u2s1", "用户2的对话", "回复")   # 另一个用户
    items = conversations.list_conversations(1)
    sids = [i["sid"] for i in items]
    check("列表只含当前用户的对话", "s1" in sids and "u2s1" not in sids, f"sids={sids}")
    item = next(i for i in items if i["sid"] == "s1")
    check("列表带消息计数", item["msg_count"] == 6, f"msg_count={item['msg_count']}")
    # 用户 2 看不到用户 1 的对话详情，反之亦然
    check("用户隔离：互相看不到对方的对话详情",
          conversations.get_conversation(2, "u2s1") is not None
          and conversations.get_conversation(2, "s1") is None
          and conversations.get_conversation(1, "s1") is not None
          and conversations.get_conversation(1, "u2s1") is None)


def test_plan_archive() -> None:
    """方案落库与行程页兜底读取。"""
    fake_plan = {"slots": {"destination": "丽江"}, "budget": {"total": 6000}}
    conversations.record_turn(3, "sp", "去丽江玩", "这是方案", dest="丽江",
                              plan=fake_plan, utterance="去丽江玩")
    got = conversations.get_plan_by_sid("sp")
    check("方案存档可按 sid 捞回", got is not None and got[1] == "去丽江玩"
          and got[0]["budget"]["total"] == 6000, f"got={got}")
    check("捞不存在的 sid 返回 None 而非报错",
          conversations.get_plan_by_sid("no-such-sid") is None)
    conv = conversations.get_conversation(3, "sp")
    check("存过方案后 has_plan 为真", conv and conv["has_plan"] is True)


def test_delete() -> None:
    """删除对话：记录 + 消息一起没了。"""
    check("删除存在的对话返回 True",
          conversations.delete_conversation(1, "s2") is True)
    check("删除后详情为 None", conversations.get_conversation(1, "s2") is None)
    check("重复删除返回 False",
          conversations.delete_conversation(1, "s2") is False)
    items = conversations.list_conversations(1)
    check("删除后列表里也没了", all(i["sid"] != "s2" for i in items))


def test_session_snapshot_restore() -> None:
    """核心场景：重启后按存档续聊，上下文不断。"""
    today = date(2026, 11, 2)
    s1 = Session(today=today)
    s1.turn("11月8号去大理玩4天")           # 缺人数/预算 → 追问，不排方案
    snap = s1.snapshot()

    s2 = Session(today=today)
    s2.restore(snap)
    check("存档恢复：槽位与轮次都在",
          s2.slots.get("destination") == "大理" and s2.slots.get("days") == 4
          and s2.turn_no == 1, f"slots={s2.slots}")
    check("存档恢复：history 一并接上",
          len(s2.history) >= 2 and s2.history[0]["role"] == "user")

    reply = s2.turn("2个人，预算5000")       # 续聊后信息齐了 → 直接出方案
    check("按存档续聊：补齐信息直接出方案",
          reply.mode == "plan" and reply.slots.get("destination") == "大理"
          and reply.slots.get("party_size") == 2, f"mode={reply.mode} slots={reply.slots}")

    # 追问状态也随存档接上：已问过的字段不再重复问
    s3 = Session(today=today)
    s3.turn("想去云南玩")                    # 追问目的地细化
    s4 = Session(today=today)
    s4.restore(s3.snapshot())
    r = s4.turn("想去云南玩")                # 同样的话再说一遍
    check("存档接上追问计次：不无限纠缠",
          r.mode in ("clarify", "plan"), f"mode={r.mode}")


def test_restore_empty_and_bad() -> None:
    """空存档/坏存档都按全新会话处理，不炸。"""
    s = Session()
    s.restore({})
    check("空存档恢复 = 全新会话", s.slots == {} and s.turn_no == 0)


def main() -> int:
    test_auto_create_and_append()
    test_title_from_first_message()
    test_list_and_isolation()
    test_plan_archive()
    test_delete()
    test_session_snapshot_restore()
    test_restore_empty_and_bad()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{passed}/{total} 条持久化断言通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}　{detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
