"""P2.5b 重验证专项测试：PendingRevalidator（归属再校验 + fresh
resolve + 四分支 + D3 六契约回填）。

设计依据 docs/PHASE5_P25_PLAN.md §4.1–4.3 / §10.1 D3 / §10.3；
开工批准（评审 2026-11-19）验收要求第 1–3 项。运行：
python tests/test_pending_revalidator.py

边界：只测重验证装配层——store 用真实 PendingStore（五方法契约
零修改），resolve 用桩/注入（真实判定链保持执行），不触碰
evaluate/act、Handler、orchestrator 接线。
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from travel_agent import pending_revalidator as _rvmod
from travel_agent.entity_resolver import (MatchRow, ResolvedCandidate,
                                          ResolveResult)
from travel_agent.pending import PendingStore
from travel_agent.pending_revalidator import PendingRevalidator

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"FAIL {name}: {detail}")


def fixed_clock():
    return lambda: datetime(2026, 11, 19, 12, 0, 0)


def m(city, name, source="", area=""):
    return MatchRow(city=city, name=name, category="restaurant",
                    area=area, source=source, origin="kb")


def kbrow(city, name, source="", area="", category="restaurant"):
    """kb_provider 行契约：[(city, dict)]（_kb_index 只读契约）。"""
    return (city, {"name": name, "category": category, "area": area,
                   "source": source})


SNAP = [
    {"candidate_id": "c1", "display_order": 1, "entity_id": "e-nj",
     "name": "海贝皇大排档（南京店）", "city": "南京", "address": "玄武区"},
    {"candidate_id": "c2", "display_order": 2, "entity_id": "e-sh",
     "name": "海贝皇大排档（上海店）", "city": "上海", "address": "浦东新区"},
]


def consumed_store(session_id="s-rv", candidates=None):
    """创建并消费一个 pending，返回 (store, pending_id, candidate_id,
    candidate_name, candidate_city, original_intent, original_role,
    original_utterance)。"""
    st = PendingStore(session_id=session_id, now=fixed_clock())
    p = st.create_pending("哪家海贝皇大排档好", "poi_query", "query",
                          candidates or SNAP)
    out = st.consume_pending(p.pending_id, "c1")
    assert out.ok
    return (st, p.pending_id, "c1", "海贝皇大排档（南京店）", "南京",
            "poi_query", "query", "哪家海贝皇大排档好")


def spy_resolver(kb_rows):
    """真实判定链 + KB 桩 + ctx 捕获（G 矩阵用）。"""
    captured = {"ctx": []}

    def resolver(candidates, context=None, **kw):
        captured["ctx"].append(dict(context or {}))
        return _rvmod._resolve_entities(candidates, context,
                                        kb_provider=lambda: list(kb_rows),
                                        **kw)
    return resolver, captured


def rb():
    out = []

    def builder(text):
        out.append(text)
        return f"REPLY::{text}"
    return builder, out


# ================================================== 输入边界


def test_boundary_checks() -> None:
    """candidate_id 归属再校验（不重复信任消费事务）+ 定位失败保守。"""
    rv = PendingRevalidator()
    (st, pid, cid, name, city, intent, role, ut) = consumed_store()
    # ① candidate_id 不在原快照 → status=""（B3 路径），不回填
    out = rv.revalidate(st, session_id="s-rv", pending_id=pid,
                        candidate_id="c999", candidate_name=name,
                        query_city="南京")
    check("[边界①] 快照外 candidate_id → B3 路径",
          out.status == "" and "快照" in out.detail, str(out))
    rec = st._latest_consumption()
    check("[边界①] 不回填（三列保持 NULL）",
          rec["revalidate_status"] is None
          and rec["revalidated_at"] is None, str(rec))
    # ② pending 不可回查（错误 ID）→ B3 路径
    out2 = rv.revalidate(st, session_id="s-rv", pending_id="nope",
                         candidate_id=cid, candidate_name=name)
    check("[边界②] pending 不可回查 → B3 路径", out2.status == "", str(out2))
    # ③ 非 consumed 状态不可重验证（open 不可达——消费才触发，
    #    防御性锁定）
    st2 = PendingStore(session_id="s-rv2", now=fixed_clock())
    p2 = st2.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    out3 = rv.revalidate(st2, session_id="s-rv2", pending_id=p2.pending_id,
                         candidate_id="c1", candidate_name=name)
    check("[边界③] 非 consumed → B3 路径", out3.status == "", str(out3))
    # ④ resolve 异常 → status=""，消费结果不被撤销
    (st4, pid4, cid4, name4, *_rest) = consumed_store("s-rv4")

    def boom(candidates, context=None, **kw):
        raise RuntimeError("注入：resolve 故障")
    out4 = rv.revalidate(st4, session_id="s-rv4", pending_id=pid4,
                         candidate_id=cid4, candidate_name=name4,
                         resolver=boom)
    check("[边界④] resolve 异常 → B3 路径（消费不撤销）",
          out4.status == "" and "RuntimeError" in out4.detail
          and st4.get_pending_by_id(pid4).status == "consumed", str(out4))
    check("[边界④] 异常不回填（契约 6：三列 NULL）",
          st4._latest_consumption()["revalidate_status"] is None, "")
    check("[边界④] shadow 记录异常",
          any(x["action"] == "revalidate_exception" for x in rv.shadow_log),
          str(rv.shadow_log[-2:]))


# ================================================== G1–G5 城市矩阵


def test_g_matrix_hc1_two_concepts() -> None:
    """§10.3 G1–G5：双概念城市——selected candidate city 仅身份属性，
    query context city 按 HC-1 重建（明确 ＞ hints 仅辅助 ＞ destination）。"""
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj", area="玄武区")]

    # G1 用户明确城市 = 候选城市 → 按既有规则判定（confirmed）
    rv1 = PendingRevalidator()
    (st, pid, cid, name, city, *_r) = consumed_store("s-g1")
    res1, cap1 = spy_resolver(kb)
    o1 = rv1.revalidate(st, session_id="s-g1", pending_id=pid,
                        candidate_id=cid, candidate_name=name,
                        query_city="南京", resolver=res1)
    check("[G1] confirmed（城市一致按既有规则）",
          o1.status == "confirmed" and o1.decision is not None, str(o1))
    check("[G1] effective=query_city",
          o1.effective_city == "南京" and o1.city_source == "query_city",
          str(o1))
    check("[G1] resolve 收到期望城市 南京",
          cap1["ctx"] and cap1["ctx"][0].get("slots", {})
          .get("destination") == "南京", str(cap1["ctx"]))

    # G2 用户明确城市 ≠ 候选城市 → Resolver 既有规则重判，不强行 confirmed
    rv2 = PendingRevalidator()
    (st2, pid2, cid2, name2, *_r) = consumed_store("s-g2")
    res2, cap2 = spy_resolver(kb)
    o2 = rv2.revalidate(st2, session_id="s-g2", pending_id=pid2,
                        candidate_id=cid2, candidate_name=name2,
                        query_city="上海", resolver=res2)
    check("[G2] 冲突 → Resolver 重判 ambiguous（绝不强行 confirmed）",
          o2.status == "ambiguous" and o2.decision is None, str(o2))
    check("[G2] 回填 ambiguous（如实流转）",
          o2.backfill_ok is True
          and st2._latest_consumption()["revalidate_status"] == "ambiguous",
          str(st2._latest_consumption()))

    # G3 无明确城市、仅有 LLM city_hints → hint 仅辅助，不得升级
    rv3 = PendingRevalidator()
    (st3, pid3, cid3, name3, *_r) = consumed_store("s-g3")
    res3, cap3 = spy_resolver(kb)
    o3 = rv3.revalidate(st3, session_id="s-g3", pending_id=pid3,
                        candidate_id=cid3, candidate_name=name3,
                        llm_city_hints=("北京",), resolver=res3)
    check("[G3] hint 未升级为期望城市（effective 为空）",
          o3.effective_city == "" and o3.city_source == "", str(o3))
    check("[G3] resolve 未收到任何期望城市",
          cap3["ctx"] and not cap3["ctx"][0].get("slots", {})
          .get("destination"), str(cap3["ctx"]))
    check("[G3] shadow 记录 hints 仅辅助",
          any("仅辅助" in x.get("detail", "")
              for x in rv3.shadow_log), str(rv3.shadow_log[:2]))

    # G4 无明确城市无 hints、仅 destination → 按既有回退链
    rv4 = PendingRevalidator()
    (st4, pid4, cid4, name4, *_r) = consumed_store("s-g4")
    res4, cap4 = spy_resolver(kb)
    o4 = rv4.revalidate(st4, session_id="s-g4", pending_id=pid4,
                        candidate_id=cid4, candidate_name=name4,
                        destination="南京", resolver=res4)
    check("[G4] destination 回退链生效",
          o4.effective_city == "南京" and o4.city_source == "destination"
          and o4.status == "confirmed", str(o4))

    # G5 三者同时存在 → 优先级不变（明确 ＞ hints ＞ destination）
    rv5 = PendingRevalidator()
    (st5, pid5, cid5, name5, *_r) = consumed_store("s-g5")
    res5, cap5 = spy_resolver(kb)
    o5 = rv5.revalidate(st5, session_id="s-g5", pending_id=pid5,
                        candidate_id=cid5, candidate_name=name5,
                        query_city="南京", llm_city_hints=("北京",),
                        destination="上海", resolver=res5)
    check("[G5] 明确城市最高优先",
          o5.effective_city == "南京" and o5.city_source == "query_city"
          and cap5["ctx"][0]["slots"]["destination"] == "南京", str(o5))
    check("[G5] hints/destination 未覆盖（记录可溯）",
          any("北京" in x.get("detail", "") and "上海" in x.get("detail", "")
              for x in rv5.shadow_log), str(rv5.shadow_log[:1]))


# ================================================== B1 fresh / B2 探测


def test_b1_fresh_revalidation() -> None:
    """B1：mock KB 变化 → 重验证结果跟随变化（绝不复用旧 matches）。"""
    # KB 名不带店缀 → 快照展示名部分匹配双行 → 跨城 ambiguous
    kb = [kbrow("南京", "海贝皇大排档", source="e-nj"),
          kbrow("上海", "海贝皇大排档", source="e-sh")]
    rv = PendingRevalidator()
    (st, pid, cid, name, *_r) = consumed_store("s-b1")
    res, cap = spy_resolver(kb)
    builder, texts = rb()
    o1 = rv.revalidate(st, session_id="s-b1", pending_id=pid,
                       candidate_id=cid, candidate_name=name,
                       query_city="南京", resolver=res,
                       reply_builder=builder)
    check("[B1] 消费时双城 KB → fresh 判 ambiguous",
          o1.status == "ambiguous" and o1.new_pending_id is not None,
          str(o1))
    # 新 Pending 的快照来自 fresh matches（此时 KB 两行同名）
    np = st.get_pending_by_id(o1.new_pending_id)
    check("[B1] 新快照 = fresh matches（两条同名候选 1..2）",
          np is not None and len(np.candidates) == 2
          and all(c.name == "海贝皇大排档" for c in np.candidates),
          str(np.candidates if np else None))
    # KB 变化：上海行下架 → 用户再选（消费新 Pending）→ 重验证跟随
    # 变化（confirmed）。重验证仅发生在消费成功后（§4.1 触发点）。
    kb.pop()
    out_c = st.consume_pending(o1.new_pending_id,
                               np.candidates[0].candidate_id)
    check("[B1] 新 Pending 可正常消费（链式重选）", out_c.ok, str(out_c))
    o2 = rv.revalidate(st, session_id="s-b1",
                       pending_id=o1.new_pending_id,
                       candidate_id=np.candidates[0].candidate_id,
                       candidate_name=np.candidates[0].name,
                       query_city="南京", resolver=res,
                       reply_builder=builder)
    check("[B1] KB 变化后重验证跟随（fresh 不复用旧 matches）",
          o2.status == "confirmed" and o2.decision is not None, str(o2))
    check("[B1] fresh entity 指向现 KB（非旧快照数据）",
          o2.decision.entity["entity_id"] == "e-nj"
          and o2.decision.entity["name"] == "海贝皇大排档",
          str(o2.decision.entity))
    check("[B1] resolve 被真实调用两次（每次全新）",
          len(cap["ctx"]) == 2, str(len(cap["ctx"])))


def test_b2_probe_status_truthful() -> None:
    """B2：探测缓存命中 / quota_exceeded / unavailable / KB-only 未探测
    ——probe_status 如实标注，绝不把「没探测」伪装成「探测过没找到」。"""
    # ① KB-only（在线关）：KB 命中 → not_applicable
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj")]
    rv1 = PendingRevalidator()
    (st, pid, cid, name, *_r) = consumed_store("s-b2a")
    o1 = rv1.revalidate(st, session_id="s-b2a", pending_id=pid,
                        candidate_id=cid, candidate_name=name,
                        kb_provider=lambda: kb)
    check("[B2①] KB 命中 → probe=not_applicable 如实",
          o1.status == "confirmed" and o1.probe_status == "not_applicable",
          str(o1))
    # ② KB-only 未命中 → not_requested + unverified（未探测 ≠ 没找到）
    rv2 = PendingRevalidator()
    (st2, pid2, cid2, name2, *_r) = consumed_store("s-b2b")
    o2 = rv2.revalidate(st2, session_id="s-b2b", pending_id=pid2,
                        candidate_id=cid2, candidate_name="不存在的店",
                        kb_provider=lambda: kb)
    check("[B2②] KB-only 未命中 → unverified + not_requested",
          o2.status == "unverified" and o2.probe_status == "not_requested",
          str(o2))
    check("[B2②] 未探测如实回填（绝不伪装 rejected）",
          st2._latest_consumption()["revalidate_status"] == "unverified"
          and st2._latest_consumption()["revalidate_probe_status"]
          == "not_requested", str(st2._latest_consumption()))
    # ③ 在线探测命中（缓存命中路径）→ success_hit
    rv3 = PendingRevalidator()
    (st3, pid3, cid3, name3, *_r) = consumed_store("s-b2c")
    o3 = rv3.revalidate(st3, session_id="s-b2c", pending_id=pid3,
                        candidate_id=cid3, candidate_name="不存在的店",
                        query_city="南京", allow_online=True,
                        probe_fn=lambda c, n, t: [m("南京", "不存在的店",
                                                    source="e-x")],
                        review_fn=lambda c, n, t: [m("南京", "不存在的店",
                                                     source="e-x")])
    check("[B2③] 在线命中 → confirmed + success_hit",
          o3.status == "confirmed" and o3.probe_status == "success_hit",
          str(o3))
    # ④ quota_exceeded / unavailable → unverified，绝不等于 not_found
    from travel_agent.entity_resolver import ProbeQuotaError
    rv4 = PendingRevalidator()
    (st4, pid4, cid4, name4, *_r) = consumed_store("s-b2d")

    def quota_boom(c, n, t):
        raise ProbeQuotaError("注入：限额")
    o4 = rv4.revalidate(st4, session_id="s-b2d", pending_id=pid4,
                        candidate_id=cid4, candidate_name="不存在的店",
                        query_city="南京", allow_online=True,
                        probe_fn=quota_boom)
    check("[B2④] quota 超限 → unverified + unavailable（绝不 rejected）",
          o4.status == "unverified" and o4.probe_status == "unavailable",
          str(o4))
    rv5 = PendingRevalidator()
    (st5, pid5, cid5, name5, *_r) = consumed_store("s-b2e")

    def net_boom(c, n, t):
        raise RuntimeError("网络故障")
    o5 = rv5.revalidate(st5, session_id="s-b2e", pending_id=pid5,
                        candidate_id=cid5, candidate_name="不存在的店",
                        query_city="南京", allow_online=True,
                        probe_fn=net_boom)
    check("[B2⑤] 探测异常 → unverified（铁律：没做成 ≠ 没找到）",
          o5.status == "unverified", str(o5))


# ================================================== 四分支产出


def test_branch_confirmed_query() -> None:
    """confirmed 查询类：DecisionResult(execute) 全链归因 + fresh entity。"""
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj-fresh",
                area="玄武区")]
    rv = PendingRevalidator()
    (st, pid, cid, name, city, intent, role, ut) = consumed_store("s-c1")
    builder, texts = rb()
    o = rv.revalidate(st, session_id="s-c1", pending_id=pid,
                      candidate_id=cid, candidate_name=name,
                      query_city="南京", reply_builder=builder,
                      kb_provider=lambda: kb)
    d = o.decision
    check("[confirmed] decision=execute + intent 保持",
          d is not None and d.action == "execute" and d.intent == "poi_query",
          str(d))
    check("[confirmed] entity 为 fresh 数据（非快照旧 entity_id）",
          d.entity["entity_id"] == "e-nj-fresh"
          and d.entity["status"] == "confirmed", str(d.entity))
    check("[confirmed] reason 全链归因（pending/candidate/revalidate）",
          "pending_revalidate" in d.reason and pid in d.reason
          and cid in d.reason and "confirmed" in d.reason, d.reason)
    check("[confirmed] city/city_source = HC-1 重建值",
          d.city == "南京" and d.city_source == "query_city", str(d))
    check("[confirmed] reply=None（交消费点）", o.reply is None, str(o))
    check("[confirmed] 回填成功且三列落位",
          o.backfill_ok is True
          and st._latest_consumption()["revalidate_status"] == "confirmed"
          and isinstance(st._latest_consumption()["revalidated_at"], int),
          str(st._latest_consumption()))


def test_branch_confirmed_plan_and_hc4() -> None:
    """confirmed 规划类 → decision=None（只确认不写行程）；HC-4 角色
    防御 → 同样绝不构造 execute。"""
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj")]
    # ① 规划类
    st = PendingStore(session_id="s-c2", now=fixed_clock())
    p = st.create_pending("把海贝皇排进第二天", "plan_modify", "query", SNAP)
    st.consume_pending(p.pending_id, "c1")
    rv = PendingRevalidator()
    o = rv.revalidate(st, session_id="s-c2", pending_id=p.pending_id,
                      candidate_id="c1", candidate_name="海贝皇大排档（南京店）",
                      query_city="南京", kb_provider=lambda: kb)
    check("[confirmed 规划类] decision=None（绝不 execute）",
          o.status == "confirmed" and o.decision is None, str(o))
    check("[confirmed 规划类] 回填 confirmed（业务结论如实）",
          st._latest_consumption()["revalidate_status"] == "confirmed", "")
    # ② HC-4 角色防御
    st2 = PendingStore(session_id="s-c3", now=fixed_clock())
    p2 = st2.create_pending("不要海贝皇", "poi_query", "negated", SNAP)
    st2.consume_pending(p2.pending_id, "c1")
    o2 = rv.revalidate(st2, session_id="s-c3", pending_id=p2.pending_id,
                       candidate_id="c1",
                       candidate_name="海贝皇大排档（南京店）",
                       query_city="南京", kb_provider=lambda: kb)
    check("[HC-4 防御] negated 角色 → decision=None",
          o2.status == "confirmed" and o2.decision is None, str(o2))


def test_branch_ambiguous_new_pending() -> None:
    """ambiguous：fresh matches → 新 Pending（新 ID/新 TTL/新快照）；
    旧 Pending 保持 consumed 不复活；不执行 Handler。"""
    kb = [kbrow("南京", "海贝皇大排档", source="e-nj"),
          kbrow("上海", "海贝皇大排档", source="e-sh")]
    rv = PendingRevalidator()
    (st, pid, cid, name, *_r) = consumed_store("s-a1")
    builder, texts = rb()
    o = rv.revalidate(st, session_id="s-a1", pending_id=pid,
                      candidate_id=cid, candidate_name=name,
                      reply_builder=builder, kb_provider=lambda: kb)
    check("[ambiguous] 新 Pending 创建",
          o.status == "ambiguous" and o.new_pending_id
          and o.new_pending_id != pid, str(o))
    np = st.get_pending_by_id(o.new_pending_id)
    check("[ambiguous] 新 Pending open + 快照来自 fresh matches（1..2）",
          np is not None and np.status == "open"
          and [c.display_order for c in np.candidates] == [1, 2]
          and np.candidates[0].name == "海贝皇大排档", str(np))
    # 旧 Pending 保持 consumed（不复活）：memory 单槽被新 Pending
    # 替换后不可按 ID 回查（既有契约，§12.4-5）；间接强证明——
    # 新 Pending 创建成功 ⇔ 旧 Pending 已不在 open 态（否则规则 8
    # 拒绝创建），且消费记录可查。
    check("[ambiguous] 旧 Pending 不复活（规则 8 间接强证明）",
          st._latest_consumption()["pending_id"] == pid
          and st._latest_consumption()["revalidate_status"] == "ambiguous",
          str(st._latest_consumption()))
    check("[ambiguous] 回复为新快照 1..N 展示",
          o.reply is not None and "1. 海贝皇大排档" in str(o.reply)
          and "2. 海贝皇大排档" in str(o.reply), str(o.reply))
    check("[ambiguous] 回填 ambiguous",
          st._latest_consumption()["revalidate_status"] == "ambiguous", "")
    check("[ambiguous] shadow 新旧 ID 关联",
          any(x["action"] == "revalidate_new_pending_created"
              and x["pending_id"] == pid for x in rv.shadow_log),
          str(rv.shadow_log[-2:]))


def test_branch_ambiguous_rebuild_failure() -> None:
    """ambiguous 重建失败（规则 8 撞车 / store 异常）→ 保守重试话术，
    绝不伪造成功、绝不复活旧 Pending。"""
    kb = [kbrow("南京", "海贝皇大排档", source="e-nj"),
          kbrow("上海", "海贝皇大排档", source="e-sh")]
    # ① 规则 8 撞车：store 已有另一 open pending（模拟并发新建）
    class Rule8Store(PendingStore):
        reject = False
        def create_pending(self, *a, **kw):
            if self.reject:
                raise ValueError("已存在 active pending（规则 8 拒绝）")
            return super().create_pending(*a, **kw)
    st8 = Rule8Store(session_id="s-a2", now=fixed_clock())
    p8 = st8.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    st8.consume_pending(p8.pending_id, "c1")
    st8.reject = True   # setup 完成后再启用规则 8 拒绝
    rv = PendingRevalidator()
    builder, texts = rb()
    o = rv.revalidate(st8, session_id="s-a2", pending_id=p8.pending_id,
                      candidate_id="c1",
                      candidate_name="海贝皇大排档（南京店）",
                      reply_builder=builder, kb_provider=lambda: kb)
    check("[F2①] 重建失败 → 保守重试话术（非确认非新快照）",
          o.status == "ambiguous" and o.new_pending_id is None
          and o.reply is not None and "稍后重试" in str(o.reply), str(o))
    check("[F2①] 原 Pending 保持 consumed（不复活）",
          st8.get_pending_by_id(p8.pending_id).status == "consumed", "")
    check("[F2①] shadow 记录重建失败",
          any(x["action"] == "revalidate_rebuild_failed"
              for x in rv.shadow_log), str(rv.shadow_log[-2:]))
    # ② store 异常 → 同保守重试
    class BoomStore(PendingStore):
        boom = False
        def create_pending(self, *a, **kw):
            if self.boom:
                raise IOError("注入：store 故障")
            return super().create_pending(*a, **kw)
    stb = BoomStore(session_id="s-a3", now=fixed_clock())
    pb = stb.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    stb.consume_pending(pb.pending_id, "c1")
    stb.boom = True   # setup 完成后再启用 store 故障
    rv2 = PendingRevalidator()
    o2 = rv2.revalidate(stb, session_id="s-a3", pending_id=pb.pending_id,
                        candidate_id="c1",
                        candidate_name="海贝皇大排档（南京店）",
                        reply_builder=rb()[0], kb_provider=lambda: kb)
    check("[F2②] store 异常 → 保守重试（异常不传播）",
          o2.status == "ambiguous" and o2.reply is not None, str(o2))


def test_branch_unverified_and_rejected() -> None:
    """unverified 保守回复（绝不伪装 rejected）；rejected 规定话术
    （保留原始表达）；skipped 防御不回填。"""
    # ① unverified（KB-only 未命中）
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj")]
    rv = PendingRevalidator()
    (st, pid, cid, name, *_r) = consumed_store("s-u1")
    builder, texts = rb()
    o = rv.revalidate(st, session_id="s-u1", pending_id=pid,
                      candidate_id=cid, candidate_name="不存在的店",
                      kb_provider=lambda: kb, reply_builder=builder)
    check("[unverified] 保守回复含引导（不执行 Handler）",
          o.status == "unverified" and o.reply is not None
          and "暂时查不到" in str(o.reply), str(o))
    check("[unverified] 绝不出现 rejected 话术",
          "没有找到" not in str(o.reply), str(o.reply))
    check("[unverified] 回填 unverified + probe 如实",
          st._latest_consumption()["revalidate_status"] == "unverified"
          and st._latest_consumption()["revalidate_probe_status"]
          == "not_requested", str(st._latest_consumption()))
    # ② rejected（分支级：注入 resolver 产出双源充分空证据结论——
    #    真实链路中 rejected 需 ≥2 适用源均空，unknown 类型复核源不
    #    适用属 Resolver 冻结行为，此处仅验证四分支的 rejected 产出）
    rv2 = PendingRevalidator()
    (st2, pid2, cid2, name2, *_r) = consumed_store("s-u2")
    rejected_rc = ResolvedCandidate(
        original={"text": name2, "entity_type": "unknown", "role": "query"},
        status="rejected", confidence=0.3,
        probe_status="not_found",
        probe_reason="cross_source_empty",
        probe_results=[{"source": "main", "outcome": "empty", "reason": ""},
                       {"source": "review", "outcome": "empty", "reason": ""}])
    o2 = rv2.revalidate(st2, session_id="s-u2", pending_id=pid2,
                        candidate_id=cid2, candidate_name=name2,
                        resolver=lambda cands, ctx=None, **kw:
                            ResolveResult(resolved=[rejected_rc]),
                        reply_builder=rb()[0])
    check("[rejected] 规定话术 + 保留原始表达",
          o2.status == "rejected" and o2.reply is not None
          and "规定数据源与检索范围内没有找到" in str(o2.reply)
          and name2 in str(o2.reply), str(o2.reply))
    check("[rejected] 回填 rejected + probe 溯源",
          st2._latest_consumption()["revalidate_status"] == "rejected"
          and st2._latest_consumption()["revalidate_probe_status"]
          == "not_found", str(st2._latest_consumption()))
    # ②' 真实链路单源空证据 → 保守 unverified（绝不 rejected）：
    #     probe 注入空 + 复核源对 unknown 类型不适用（Resolver 冻结行为）
    rv2b = PendingRevalidator()
    (st2b, pid2b, cid2b, name2b, *_r) = consumed_store("s-u2b")
    o2b = rv2b.revalidate(st2b, session_id="s-u2b", pending_id=pid2b,
                          candidate_id=cid2b, candidate_name="不存在的店",
                          query_city="南京", allow_online=True,
                          probe_fn=lambda c, n, t: [])
    check("[rejected'] 单源空证据 → unverified（绝不 rejected）",
          o2b.status == "unverified", str(o2b))
    # ③ skipped 防御：不回填（非四枚举）、保守回复
    rv3 = PendingRevalidator()
    (st3, pid3, cid3, name3, *_r) = consumed_store("s-u3")
    skipped = ResolvedCandidate(
        original={"text": name3, "entity_type": "unknown", "role": "query"},
        status="skipped", confidence=0.0, probe_status="not_applicable")
    o3 = rv3.revalidate(st3, session_id="s-u3", pending_id=pid3,
                        candidate_id=cid3, candidate_name=name3,
                        resolver=lambda cands, ctx=None, **kw:
                            ResolveResult(resolved=[skipped]))
    check("[skipped 防御] 保守回复 + 不回填（三列 NULL）",
          o3.status == "unverified"
          and st3._latest_consumption()["revalidate_status"] is None
          and "skipped" in o3.detail, str(o3))


# ================================================== D3 六契约回填


def test_backfill_six_contracts() -> None:
    """六契约：条件更新不插入 / 命中 1 行成功 / 0 行 False+shadow /
    异常不改消费 / 合法值四枚举、异常与未探测保持 NULL。"""
    rv = PendingRevalidator()
    (st, pid, cid, name, *_r) = consumed_store("s-d3")
    # 契约 1+2：条件更新命中既有记录（绝不插入）→ True
    ok = st.record_revalidation("s-d3", pid, cid, "confirmed",
                                "not_applicable")
    rec = st._latest_consumption()
    check("[契约1/2] 条件更新命中恰一条 → True（记录数不变）",
          ok is True and rec["pending_id"] == pid
          and rec["candidate_id"] == cid
          and rec["revalidate_status"] == "confirmed"
          and isinstance(rec["revalidated_at"], int), str(rec))
    # 契约 3：rowcount=0 三形态全 False，绝不当作成功、不写数据
    for sid, p, c, tag in (("s-other", pid, cid, "跨session"),
                           ("s-d3", "other-pid", cid, "跨pending"),
                           ("s-d3", pid, "c999", "候选不符")):
        ok0 = st.record_revalidation(sid, p, c, "confirmed", None)
        check(f"[契约3] {tag} → False", ok0 is False, str(ok0))
    # 契约 3+6：合法值固定四枚举，非法值拒绝（不写）
    for bad in ("skipped", "executed", ""):
        okb = st.record_revalidation("s-d3", pid, cid, bad, None)
        check(f"[契约6] 非法值 {bad!r} → False", okb is False, str(okb))
    check("[契约6] 非法值不写（原值保持）",
          st._latest_consumption()["revalidate_status"] == "confirmed", "")
    # 契约 4：store 异常 → False，不抛出，消费结果不变
    class BoomStore(PendingStore):
        def record_revalidation(self, *a, **kw):
            raise IOError("注入：store 故障")
    stb = BoomStore(session_id="s-d3b", now=fixed_clock())
    pb = stb.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    stb.consume_pending(pb.pending_id, "c1")
    rv2 = PendingRevalidator()
    o = rv2.revalidate(stb, session_id="s-d3b", pending_id=pb.pending_id,
                       candidate_id="c1",
                       candidate_name="海贝皇大排档（南京店）",
                       query_city="南京",
                       kb_provider=lambda: [kbrow("南京", "海贝皇大排档（南京店）",
                                   source="e-nj")])
    check("[契约4] 回填异常 → 业务流不变（分支照常产出）",
          o.status == "confirmed" and o.decision is not None
          and o.backfill_ok is False, str(o))
    check("[契约4] 消费结果不被撤销",
          stb.get_pending_by_id(pb.pending_id).status == "consumed", "")
    check("[契约4] shadow 归因记录",
          any("backfill" in x["action"] for x in rv2.shadow_log),
          str(rv2.shadow_log[-2:]))
    # 契约 6：异常路径（resolve 抛出）不回填——三列保持 NULL
    (st6, pid6, cid6, name6, *_r) = consumed_store("s-d3c")

    def boom(candidates, context=None, **kw):
        raise RuntimeError("注入")
    o6 = rv.revalidate(st6, session_id="s-d3c", pending_id=pid6,
                       candidate_id=cid6, candidate_name=name6,
                       resolver=boom)
    rec6 = st6._latest_consumption()
    check("[契约6] 异常/未探测 → 三列保持 NULL",
          o6.status == "" and rec6["revalidate_status"] is None
          and rec6["revalidate_probe_status"] is None
          and rec6["revalidated_at"] is None, str(rec6))


def test_f4_backfill_business_separation() -> None:
    """F4：回填失败与业务流分离——不改变四分支结果、无重复消费。"""
    kb = [kbrow("南京", "海贝皇大排档（南京店）", source="e-nj")]
    st = PendingStore(session_id="s-f4", now=fixed_clock())
    p = st.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    st.consume_pending(p.pending_id, "c1")
    consume_count = {"n": 0}
    orig_consume = st.consume_pending

    def counting_consume(pending_id, candidate_id):
        consume_count["n"] += 1
        return orig_consume(pending_id, candidate_id)
    st.consume_pending = counting_consume
    rv = PendingRevalidator()
    builder, texts = rb()
    o = rv.revalidate(st, session_id="s-f4", pending_id=p.pending_id,
                      candidate_id="c1",
                      candidate_name="海贝皇大排档（南京店）",
                      query_city="南京", reply_builder=builder,
                      kb_provider=lambda: kb)
    check("[F4] 回填正常时分支结果完整",
          o.status == "confirmed" and o.decision is not None
          and o.backfill_ok is True, str(o))
    check("[F4] 重验证全程零重复消费",
          consume_count["n"] == 0, str(consume_count))
    # 回填失败注入：分支结果不变
    st2 = PendingStore(session_id="s-f4b", now=fixed_clock())
    p2 = st2.create_pending("哪家海贝皇大排档好", "poi_query", "query", SNAP)
    st2.consume_pending(p2.pending_id, "c1")
    st2.record_revalidation = lambda *a, **kw: False
    rv2 = PendingRevalidator()
    o2 = rv2.revalidate(st2, session_id="s-f4b", pending_id=p2.pending_id,
                        candidate_id="c1",
                        candidate_name="海贝皇大排档（南京店）",
                        query_city="南京", reply_builder=rb()[0],
                        kb_provider=lambda: kb)
    check("[F4] 回填失败不改变分支结果",
          o2.status == "confirmed" and o2.decision is not None
          and o2.backfill_ok is False, str(o2))


# ================================================== main


def main() -> int:
    test_boundary_checks()
    test_g_matrix_hc1_two_concepts()
    test_b1_fresh_revalidation()
    test_b2_probe_status_truthful()
    test_branch_confirmed_query()
    test_branch_confirmed_plan_and_hc4()
    test_branch_ambiguous_new_pending()
    test_branch_ambiguous_rebuild_failure()
    test_branch_unverified_and_rejected()
    test_backfill_six_contracts()
    test_f4_backfill_business_separation()
    print(f"\npending revalidator P2.5b: {_PASS} pass / {_FAIL} fail")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
