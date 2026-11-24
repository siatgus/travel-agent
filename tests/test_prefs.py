"""按用户隔离的偏好档案单元测试（独立脚本直跑）。

运行：python tests/test_prefs.py

覆盖（对应授权范围 1→5 的实施点）：

  T01  开关默认值（TRAVEL_PREFS_SCOPE=global / TRAVEL_PREFS_WRITE=0）
  T02  开关解析（大小写/空白/空串/非法值的兜底口径）
  T03  条目规整 clip_item（去空白/换行压平/超长截断/非字符串丢弃）
  T04  列表清洗 _clean_list（白名单/去重/保持顺序/有界 LIM_PREFS）
  T05  写闸关闭：set/merge_list/remove_item/clear 全部 False 且不落盘
        （set 不设例外口子：播种档案必须显式开闸）
  T06  写闸打开：并集追加、无新增不写、删除、清键的真实落盘
  T07  按 uid 隔离：A 的偏好不进 B 的档案（同一 .app.db）
  T08  PreferenceMemory(store=) 读出即用：apply_to / all 副本语义
  T09  PreferenceMemory(store=) 写侧：merge_hard_prefs / remove_hard_pref / clear
  T10  PreferenceMemory(path) 旧全局 JSON 模式：写开→落盘可复读；写关→一字节不写
  T11  读侧不受写闸影响（关闸后仍能读到已存内容，但改不动）
  T12  存储降级：库路径不可用 → get/all 返回空、set 返回 False，均不抛异常
  T13  server._prefs_view：只回已知列表键、未知键不外泄、条目按上限截断
  T14  server 路由与 UI 挂点存在性（GET/POST /api/prefs、顶栏按钮、抽屉节点）
  T15  plan_handler 写回顺序（静态文本检查：apply_to 之后才有 merge_hard_prefs）

**不覆盖**：真实浏览器交互、真实登录态下的端到端点击、多进程并发写同一 uid。
T14/T15 是静态源码检查，只证明「接点存在/顺序正确」，不证明运行时行为。

纪律：环境变量一律快照/逐键恢复（区分 unset 与有值），不得用「归零」当恢复；
每个用例自己建临时库，不用仓库 data/ 里的真实库。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import travel_agent.prefs as prefs              # noqa: E402
from travel_agent.dialogue import PreferenceMemory   # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
_ENV_KEYS = ("TRAVEL_APP_DB", "TRAVEL_PREFS_SCOPE", "TRAVEL_PREFS_WRITE",
             "TRAVEL_USER_PREFS")
_SNAP: dict[str, tuple[bool, str | None]] = {}


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                        ("　" + detail) if detail and not cond else ""))


def _env_snapshot() -> None:
    for k in _ENV_KEYS:
        _SNAP[k] = (k in os.environ, os.environ.get(k))


def _env_restore() -> None:
    for k, (present, val) in _SNAP.items():
        if present:
            os.environ[k] = val if val is not None else ""
        else:
            os.environ.pop(k, None)


def _env(**kw) -> None:
    """临时改环境；值 None 表示删掉该键（区分 unset 与空串）。"""
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _fresh_db(tag: str) -> str:
    d = tempfile.mkdtemp(prefix="prefs_%s_" % tag)
    return os.path.join(d, ".app.db")


# ---------------------------------------------------------------- T01/T02

def test_switches():
    _env(TRAVEL_PREFS_SCOPE=None, TRAVEL_PREFS_WRITE=None)
    check("T01a 默认 scope=global", prefs.scope() == "global", prefs.scope())
    check("T01b 默认写闸关闭", prefs.write_enabled() is False)

    cases = [("user", "user"), ("USER", "user"), (" User ", "user"),
             ("global", "global"), ("", "global"), ("weird", "global")]
    for raw, want in cases:
        _env(TRAVEL_PREFS_SCOPE=raw)
        check("T02a scope(%r)=%s" % (raw, want), prefs.scope() == want, prefs.scope())

    for raw, want in [("1", True), ("0", False), ("", False),
                      ("true", False), ("yes", False), (" 1 ", True)]:
        _env(TRAVEL_PREFS_WRITE=raw)
        check("T02b write(%r)=%s" % (raw, want),
              prefs.write_enabled() is want, str(prefs.write_enabled()))
    _env(TRAVEL_PREFS_SCOPE=None, TRAVEL_PREFS_WRITE=None)


# ---------------------------------------------------------------- T03/T04

def test_clean():
    check("T03a 去空白", prefs.clip_item(" 不吃辣 ") == "不吃辣")
    check("T03b 换行压平", prefs.clip_item("不吃\n辣") == "不吃 辣")
    check("T03c 超长截断", len(prefs.clip_item("x" * 60)) == prefs.LIM_ITEM_CHARS)
    check("T03d 非字符串丢弃", prefs.clip_item(None) == "" and prefs.clip_item(7) == "")
    check("T03e 空串", prefs.clip_item("   ") == "")

    got = prefs._clean_list(["不吃辣", "不吃辣", "乱写的", None, "素食", 3])
    check("T04a 白名单+去重+保序", got == ["不吃辣", "素食"], repr(got))
    check("T04b 非列表入参", prefs._clean_list("不吃辣") == []
          and prefs._clean_list(None) == [])
    many = [prefs.KNOWN_HARD_PREFS[i % len(prefs.KNOWN_HARD_PREFS)] for i in range(40)]
    check("T04c 重复项收敛到白名单全集",
          prefs._clean_list(many) == list(prefs.KNOWN_HARD_PREFS),
          repr(prefs._clean_list(many)))


# ---------------------------------------------------------------- T05/T06

def test_store_write_gate():
    db = _fresh_db("gate")
    _env(TRAVEL_APP_DB=db, TRAVEL_PREFS_WRITE="0")
    s = prefs.PrefStore(1)
    check("T05a set 被拒", s.set("hard_prefs", ["不吃辣"]) is False)
    check("T05b merge_list 被拒", s.merge_list("hard_prefs", ["不吃辣"]) is False)
    check("T05c remove_item 被拒", s.remove_item("hard_prefs", "不吃辣") is False)
    check("T05d clear 被拒", s.clear() is False)
    check("T05e 档案仍为空", s.all() == {} and s.get("hard_prefs") is None, repr(s.all()))

    _env(TRAVEL_PREFS_WRITE="1")
    check("T06a 首次并集追加", s.merge_list("hard_prefs", ["不吃辣", "素食"]) is True)
    check("T06b 重复追加不写库", s.merge_list("hard_prefs", ["素食"]) is False)
    check("T06c 增量追加", s.merge_list("hard_prefs", ["需午休"]) is True)
    check("T06d 读回", s.get("hard_prefs") == ["不吃辣", "素食", "需午休"],
          repr(s.get("hard_prefs")))
    check("T06e 非白名单不入库", s.merge_list("hard_prefs", ["随便写的"]) is False)
    check("T06f 删除成功", s.remove_item("hard_prefs", "素食") is True)
    check("T06g 删除不存在为 False", s.remove_item("hard_prefs", "素食") is False)
    check("T06h 删除后内容", s.get("hard_prefs") == ["不吃辣", "需午休"],
          repr(s.get("hard_prefs")))
    check("T06i set 整键覆盖", s.set("hard_prefs", ["怕晒"]) is True
          and s.get("hard_prefs") == ["怕晒"])
    check("T06j clear(key)", s.clear("hard_prefs") is True and s.get("hard_prefs") is None)

    # 容量兜底：库里被写入超长列表时，下一次并集追加把它截到上限
    s.set("hard_prefs", list(prefs.KNOWN_HARD_PREFS) * 4)      # 40 条（含重复）
    s.merge_list("hard_prefs", [prefs.KNOWN_HARD_PREFS[0]])
    check("T06l 并集追加后截到 LIM_PREFS", len(s.get("hard_prefs")) == prefs.LIM_PREFS,
          str(len(s.get("hard_prefs"))))
    s.clear()

    s.set("soft_prefs", ["安静"])
    check("T06k clear(全部)", s.clear() is True and s.all() == {}, repr(s.all()))


# ---------------------------------------------------------------- T07

def test_isolation():
    db = _fresh_db("iso")
    _env(TRAVEL_APP_DB=db, TRAVEL_PREFS_WRITE="1", TRAVEL_PREFS_SCOPE="user")
    a, b = prefs.PrefStore(1), prefs.PrefStore(2)
    a.merge_list("hard_prefs", ["不吃辣"])
    b.merge_list("hard_prefs", ["素食"])
    check("T07a A 只看自己", a.get("hard_prefs") == ["不吃辣"], repr(a.get("hard_prefs")))
    check("T07b B 只看自己", b.get("hard_prefs") == ["素食"], repr(b.get("hard_prefs")))
    check("T07c A 的档案不含 B 的项", "素食" not in (a.get("hard_prefs") or []))
    check("T07d all() 只含本 uid 的键", set(a.all()) == {"hard_prefs"}, repr(a.all()))
    b.clear()
    check("T07e 清空 B 不影响 A", a.get("hard_prefs") == ["不吃辣"],
          repr(a.get("hard_prefs")))


# ---------------------------------------------------------------- T08/T09

def test_memory_store():
    db = _fresh_db("mem")
    _env(TRAVEL_APP_DB=db, TRAVEL_PREFS_WRITE="1", TRAVEL_PREFS_SCOPE="user")
    m = PreferenceMemory(store=prefs.PrefStore(7))
    check("T08a uid 透传", m.uid == 7)
    check("T08b 空档案 apply_to 原样返回", m.apply_to({"destination": "大理"})
          == {"destination": "大理"})

    check("T09a 写回成功", m.merge_hard_prefs(["不吃辣", "乱写"]) is True)
    check("T09b 只落白名单项", m.get("hard_prefs") == ["不吃辣"], repr(m.get("hard_prefs")))
    check("T09c 重复写回不写库", m.merge_hard_prefs(["不吃辣"]) is False)

    slots = {"destination": "大理", "hard_prefs": ["素食"]}
    out = m.apply_to(slots)
    check("T08c 档案与本轮并集", out["hard_prefs"] == ["不吃辣", "素食"], repr(out["hard_prefs"]))
    check("T08d 不改原对象", slots["hard_prefs"] == ["素食"], repr(slots["hard_prefs"]))

    snap = m.all()
    snap["hard_prefs"].append("怕晒")
    check("T08e all() 是副本", m.get("hard_prefs") == ["不吃辣"], repr(m.get("hard_prefs")))

    check("T09d 删除", m.remove_hard_pref("不吃辣") is True and m.get("hard_prefs") == [],
          repr(m.get("hard_prefs")))
    m.merge_hard_prefs(["素食"])
    check("T09e clear(key)", m.clear("hard_prefs") is True and m.get("hard_prefs") is None)


# ---------------------------------------------------------------- T10/T11

def test_memory_json():
    d = tempfile.mkdtemp(prefix="prefs_json_")
    p = os.path.join(d, ".user_prefs.json")
    _env(TRAVEL_PREFS_WRITE="1", TRAVEL_PREFS_SCOPE="global")
    m = PreferenceMemory(p)
    check("T10a 初始空档案", m.all() == {})
    check("T10b 写回成功", m.merge_hard_prefs(["需无障碍"]) is True)
    check("T10c 文件已生成", os.path.exists(p))
    check("T10d 新实例可复读", PreferenceMemory(p).get("hard_prefs") == ["需无障碍"],
          repr(PreferenceMemory(p).get("hard_prefs")))
    check("T10e 无新增不写", m.merge_hard_prefs(["需无障碍"]) is False)
    check("T10f remove_hard_pref", m.remove_hard_pref("需无障碍") is True
          and m.get("hard_prefs") == [])

    m.merge_hard_prefs(["怕晒"])
    before = open(p, encoding="utf-8").read()
    _env(TRAVEL_PREFS_WRITE="0")
    check("T11a 关闸后仍可读", m.get("hard_prefs") == ["怕晒"], repr(m.get("hard_prefs")))
    check("T11b 关闸后写不进去", m.merge_hard_prefs(["素食"]) is False)
    check("T11c 关闸后 remove 无效", m.remove_hard_pref("怕晒") is False)
    check("T11d 关闸后 clear 无效", m.clear() is False)
    check("T11g 关闸后底层 set 也无效", m.set("hard_prefs", ["素食"]) is False)
    check("T11e 文件内容未变", open(p, encoding="utf-8").read() == before)
    check("T11f 内存内仍可见", m.get("hard_prefs") == ["怕晒"], repr(m.get("hard_prefs")))


# ---------------------------------------------------------------- T12

def test_degrade():
    d = tempfile.mkdtemp(prefix="prefs_bad_")
    _env(TRAVEL_APP_DB=d, TRAVEL_PREFS_WRITE="1")   # 目录当库文件 → 打开失败
    s = prefs.PrefStore(1)
    ok = True
    try:
        check("T12a get 返回 None", s.get("hard_prefs") is None)
        check("T12b all 返回空字典", s.all() == {})
        check("T12c set 返回 False", s.set("hard_prefs", ["不吃辣"]) is False)
        check("T12d merge 返回 False", s.merge_list("hard_prefs", ["不吃辣"]) is False)
        check("T12e clear 返回 False", s.clear() is False)
    except Exception as e:                       # noqa: BLE001 - 就是要证明不抛
        ok = False
        check("T12x 降级路径不得抛异常", False, "%s: %s" % (type(e).__name__, e))
    check("T12f 全程无异常", ok)


# ---------------------------------------------------------------- T13

def test_server_view():
    db = _fresh_db("view")
    _env(TRAVEL_APP_DB=db, TRAVEL_PREFS_WRITE="1", TRAVEL_PREFS_SCOPE="user")
    import travel_agent.server as server   # noqa: E402 - 延迟导入，环境已就绪

    s = prefs.PrefStore(11)
    s.merge_list("hard_prefs", ["不吃辣", "素食"])
    s.set("misc", ["不该外泄"])                 # 未知键
    s.set("hard_prefs", ["不吃辣", "x" * 60])    # 超长条目
    view = server._prefs_view(11)
    check("T13a 未知键不外泄", "misc" not in view, repr(view))
    check("T13b 只回已知列表键", set(view) <= set(prefs.LIST_KEYS), repr(set(view)))
    check("T13c 条目按上限截断",
          all(len(x) <= prefs.LIM_ITEM_CHARS for x in view.get("hard_prefs") or []),
          repr(view.get("hard_prefs")))
    check("T13d 另一个 uid 看不到", server._prefs_view(12) == {},
          repr(server._prefs_view(12)))
    check("T13e 同一 uid 拿到自己的",
          server._prefs_view(11).get("hard_prefs") == ["不吃辣", "x" * prefs.LIM_ITEM_CHARS],
          repr(server._prefs_view(11)))


# ---------------------------------------------------------------- T14/T15

def test_wiring():
    import travel_agent.server as server   # noqa: E402
    page = server.PAGE
    check("T14a 顶栏有偏好入口", 'id="btn-prefs"' in page)
    check("T14b 抽屉与列表节点", 'id="prefpanel"' in page and 'id="preflist"' in page)
    check("T14c 前端调 GET/POST", "fetch('/api/prefs'" in page
          and "'/api/prefs', {method:'POST'" in page)
    check("T14d 只读态隐藏编辑区", "prefadd').style.display = ro ? 'none' : 'flex'" in page)

    src = server.__file__
    handler = os.path.join(os.path.dirname(src), "orchestrator", "handlers", "plan_handler.py")
    with open(handler, encoding="utf-8") as f:
        code = f.read()
    i_apply = code.find("memory.apply_to(session.slots)")
    i_write = code.find("merge_hard_prefs(_new_hp)")
    check("T15a 写回调用存在", i_write > 0)
    check("T15b 写回在 apply_to 之后", 0 < i_apply < i_write,
          "apply_to=%d merge=%d" % (i_apply, i_write))


def main() -> int:
    _env_snapshot()
    try:
        test_switches()
        test_clean()
        test_store_write_gate()
        test_isolation()
        test_memory_store()
        test_memory_json()
        test_degrade()
        test_server_view()
        test_wiring()
    finally:
        _env_restore()

    total = len(RESULTS)
    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = total - passed
    print("\nprefs (per-user isolation): %d pass / %d fail" % (passed, failed))
    if failed:
        for name, ok, detail in RESULTS:
            if not ok:
                print("  FAILED: %s  %s" % (name, detail))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
