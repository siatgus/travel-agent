"""Guard Pipeline：方案修改守卫与前置拦截（保持原有顺序与语义）。

对应设计文档 2.4 / 7 节：这些守卫有明确优先级和业务语义，**不合并**成一个
宽泛的 check_plan_modification()。每个守卫就是一个独立方法，返回 None 表示
本守卫不接、交回流程；返回 Reply 表示已处理本轮。

执行顺序 = 原 _turn_impl 中的出现顺序（Orchestrator 按 steps 调度）：
1. stay_review          「之前安排的酒店在哪里」→ 答方案里的入住节点
2. transport_time_change 「把去程到达时间改为 09:00」→ 拒改真实班次时刻
3. clock_anchor          「Day 3 午餐排到 15:20」→ composer.apply_anchor 整体平移
4. trim_optional         「删掉可去可不去的」→ 就地删非必去点，不整体重排
"""

from __future__ import annotations

import re

from .. import agent, composer, intent
from ..dialogue import Reply


class GuardPipeline:
    """按固定顺序执行守卫；命中即返回该守卫的 Reply。"""

    steps = ("stay_review", "transport_time_change", "clock_anchor",
             "trim_optional")

    def run(self, session, utterance: str, intent_name: str, ctx):
        for step in self.steps:
            reply = getattr(self, step)(session, utterance, intent_name)
            if reply is not None:
                ctx.last_guard = step
                return reply
        return None

    # ------------------------------------------------ 守卫 1（保持原顺序）
    def stay_review(self, session, utterance: str,
                    intent_name: str) -> Reply | None:
        """「之前安排的酒店在哪里」→ 答方案里的入住节点，不当重排请求。"""
        # 出过方案后回看住宿安排（「之前安排的酒店在哪里」）：问的是方案
        # 内容，不是新规划请求——实测被分类成 plan 走了重排追问日期。
        if (session.plan_made and session.last_plan
                and intent_name in ("plan", "plan_qa")
                and re.search(r"酒店|住宿|住处|住哪", utterance)
                and re.search(r"之前|刚才|行程|安排的|订的|定的|已订", utterance)
                and not re.search(r"重新|重排|换[成掉]|改成|改一下|调整"
                                  r"|加[进上]|删|去掉|不要", utterance)):
            text = session._plan_stay_answer()
            if text:
                session.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=session.turn_no,
                             ask_rounds=session.ask_rounds, slots=dict(session.slots),
                             render=text)
        return None

    # ------------------------------------------------ 守卫 2
    def transport_time_change(self, session, utterance: str,
                              intent_name: str) -> Reply | None:
        """「把去程到达时间改为 09:00」→ 拒改：班次时刻必须来自真实查询。"""
        # 「把去程到达时间改为 09:00」——交通时刻修改请求：班次时刻来自
        # 真实查询，不能凭空改数字；这类指令必须拦在槽位抽取之前，否则
        # 目的地兜底正则会把「达时间改为」吃成城市/景点，污染整份方案
        # （用户实测：进了必去景点、假设和行前准备）。有无方案都拦。
        if intent.is_transport_time_change(utterance):
            text = (
                "这条先没动行程：**去程/返程的时刻来自真实班次查询，"
                "我不能直接改数字**——改了就不是真实存在的班次了。\n\n"
                "想赶更早或更晚的班次，可以：\n"
                "1. 直接问班次（如「上海到成都的高铁班次」），我看到合适的"
                "真实班次后按它重排行程；\n"
                "2. 告诉我换出发日期或出发地，我重新查班次再排。\n\n"
                "如果你其实是想排行程或改行程，直接说需求（去哪儿、玩几天），"
                "班次时刻我来查。")
            session.history.append({"role": "agent", "content": text})
            return Reply(mode="answer", turn_no=session.turn_no,
                         ask_rounds=session.ask_rounds, slots=dict(session.slots),
                         render=text)
        return None

    # ------------------------------------------------ 守卫 3
    def clock_anchor(self, session, utterance: str,
                     intent_name: str) -> Reply | None:
        """「Day 3 午餐排到 15:20 结束」→ composer.apply_anchor 整体平移。"""
        # 「把 Day 3 午餐排到 15:20 结束」——分钟级锚点改排：整体平移目标日
        # 时间轴（composer.apply_anchor），不动景点时长与交通。放在意图直答
        # 之后：天气/交通/景点问答里出现时刻不归这管。解析不出具体锚点
        # （有时刻没目标节点）仍走诚实提示，绝不静默重排装没听见。
        if session.plan_made and intent.wants_clock_schedule(utterance):
            anchor = intent.parse_clock_anchor(utterance)
            if not anchor:
                text = (
                    "想按具体时刻（比如 15:20）改时间轴，得说清楚是**哪一餐/午休**：\n\n"
                    "可以这样说：「Day 3 午餐排到 15:20 结束」「每天晚餐 7 点开始」"
                    "「把午休改成 2 小时」。")
            else:
                changed, detail, new_days = composer.apply_anchor(
                    (session.last_plan or {}).get("days_plan") or [], anchor)
                if not changed:
                    text = f"这条没动时间轴：{detail}。"
                else:
                    # 写回 last_plan：渲染、存档、页面刷新都走既有链路；
                    # 之后换/加景点会重新编排，锚点是即时的，需要再说一次
                    session.last_plan = dict(session.last_plan, days_plan=new_days)
                    text = (f"> 已按你的时刻要求调整：{detail}。\n\n"
                            "> 注意：之后再换/删/加景点或改天数会重新编排时间轴，"
                            "这条时刻要求如还需生效请再说一次。\n\n"
                            + agent.render(session.last_plan))
            session.history.append({"role": "agent", "content": text})
            mode = "plan" if (anchor and changed) else "answer"
            return Reply(mode=mode, turn_no=session.turn_no,
                         ask_rounds=session.ask_rounds, slots=dict(session.slots),
                         plan=session.last_plan if mode == "plan" else None,
                         render=text)
        return None

    # ------------------------------------------------ 守卫 4
    def trim_optional(self, session, utterance: str,
                      intent_name: str) -> Reply | None:
        """「删掉可去可不去的」→ 就地删非必去点，绝不整体重排。"""
        # 「把可去可不去的删掉」「删掉一些景点」——泛指删非必去点，不是加
        # 新景点/排除新地点：句子里的「一些/可去可不去的」不是地名，放过去
        # 会被抽成景点名污染方案还落库（实测「可不去的删掉」进了必去点）。
        # 拦在抽取之前，按节奏就地删超额站；句里带赶/挤字眼（「还是有点赶…」）
        # 视为同时要放松节奏，按轻松档处理。
        if session.plan_made and intent.wants_trim_optional(utterance) \
                and not re.search(r"加上|增加|添上|加一天|多玩一天|多住|延长"
                                  r"|换成|改[成到]|再加|再逛|再去|必去",
                                  utterance):
            _pre = agent.extract_slots_rules(utterance, today=session.today)
            if not any(_pre.get(k) for k in ("destination", "date_range",
                                             "days", "must_visit", "avoid",
                                             "origin")):
                old_pace = ((session.last_plan or {}).get("slots") or {}).get("pace") \
                    or (session.slots or {}).get("pace") or "moderate"
                pace = "relaxed" if re.search(r"[赶挤紧]|太满|太累", utterance) \
                    else old_pace
                if pace != old_pace:
                    session._sync_plan_pace(pace)
                    session.slots = dict(session.slots or {}, pace=pace)
                trimmed = session._trim_overcap(
                    pace, label="删掉可去可不去的景点")
                if trimmed is not None:
                    note, _removed = trimmed
                    text = note + agent.render(session.last_plan)
                    mode = "plan"
                else:
                    text = session._pace_noop_answer(pace, just_loosened=False)
                    mode = "answer"
                session.history.append({"role": "agent", "content": text})
                return Reply(mode=mode, turn_no=session.turn_no,
                             ask_rounds=session.ask_rounds,
                             slots=dict(session.slots),
                             plan=session.last_plan if mode == "plan" else None,
                             render=text)
        return None

