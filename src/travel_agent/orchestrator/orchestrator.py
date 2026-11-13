"""Orchestrator：唯一调度入口（文档 4.1 目标架构第一阶段）。

一轮消息的处理顺序（= 原 _turn_impl 的真实顺序，逐段搬移、行为零变化）：

    1. 创建 TurnContext（单轮上下文）
    2. 明确咨询预判：周边餐饮/酒店/两点路线（_nearby_gate，最高优先级）
    3. 行程节点问答 gate（_plan_spot_gate）
    4. 意图路由（IntentRouter：规则 → LLM 兜底 → spot_qa 规则兜底）
    5. 非规划问答分发（Session 既有 _answer / QA 处理方法，本阶段复用）
    6. 方案修改守卫（GuardPipeline，保持原顺序与业务语义）
    7. 规划管道（槽位抽取合并 → 追问/无脑重排拦截 → plan_trip）
       ——第三阶段再迁入独立 PlanHandler，本阶段整段搬移到此

业务原则不变：真实班次/价格/距离由规则与工具负责；模型只在受控范围内
兜底分类和润色表达；规划请求宁可进规则管道，也不让 LLM 自由生成行程。
"""

from __future__ import annotations

import re

from .. import agent, composer, intent, llm, real_tools
from ..composer import name_hit               # noqa: F401（规划管道显式使用）
from ..dialogue import (
    ASK_RULES,
    _DAYS_TOPIC,
    _GLOBAL_VAGUE,
    _REPLAN_RE,
    _TIME_TOPIC,
    _explicit_fields,
    _pace_rank,
    _restore_intent,
    merge_slots,
    serviceable_cities,
    Reply,
)
from .context import TurnContext
from .guard import GuardPipeline
from .intent_router import IntentRouter, QA_KINDS
from .result import answer_reply


class Orchestrator:
    """单轮流程调度。Handler 复用 Session 既有业务方法（第一阶段）。"""

    def __init__(self):
        self.router = IntentRouter()
        self.guards = GuardPipeline()
        self.last_ctx: TurnContext | None = None   # 最近一轮的观测点（排查/测试用）

    # -------------------------------------------------- 调度入口
    def handle(self, session, utterance: str,
               history_text: str | None = None) -> Reply:
        # ---- 1. 创建单轮上下文；跨轮状态仍归 Session ----
        ctx = TurnContext(utterance=utterance, history=session.history,
                          slots=session.slots, last_plan=session.last_plan,
                          plan_made=session.plan_made)
        self.last_ctx = ctx
        session.turn_no += 1
        session.history.append({"role": "user",
                                "content": history_text or utterance})
        # 说话层/闲聊层带最近几轮（分类、抽取、校验层不读它，见 llm.attach_history）
        llm.attach_history(session.client, session._llm_history())

        # ---- 2. 明确咨询预判：周边问句分流（找吃的/找住的/两点路线）----
        # 「宽窄巷子附近有什么好吃的」一旦被当成「问宽窄巷子这个景点」，
        # 周边查询就提前结束；「从X怎么去Y」的「怎么去」还会撞上行程点名拦截。
        nearby = session._nearby_gate(utterance)
        if nearby is not None:
            session.history.append({"role": "agent", "content": nearby})
            ctx.route, ctx.handled = "qa_nearby", True
            return answer_reply(session, nearby)

        # ---- 3. 行程节点问答 gate：点名行程里的店/景点问细节 ----
        gated = session._plan_spot_gate(utterance)
        if gated is not None:
            session.history.append({"role": "agent", "content": gated})
            ctx.route, ctx.handled = "qa_plan_spot", True
            return answer_reply(session, gated)

        # ---- 4. 意图路由：问天气/景点/班次/闲聊时直接答，不进排行程流水线 ----
        # 之前所有输入都被当成「补槽位」，于是问天气会被回「排行程还差 2 件事」，
        # 问到第三轮还会硬凑出一份别的城市的行程。
        ir = self.router.route(session, utterance)
        ctx.intent, ctx.intent_source = ir.name, ir.source
        ctx.city = ir.entities.get("city")

        # ---- 5. 非规划问答分发（QA Handler 层，本阶段复用 Session 方法）----
        if ir.name in QA_KINDS:
            text = session._answer(ir.name, utterance)
            if text is not None:
                session.history.append({"role": "agent", "content": text})
                ctx.route, ctx.handled = "qa", True
                return answer_reply(session, text)

        # ---- 6. 方案修改守卫（GuardPipeline，保持原有顺序与语义）----
        reply = self.guards.run(session, utterance, ir.name, ctx)
        if reply is not None:
            ctx.route, ctx.handled = "guard", True
            return reply

        # ---- 7. 规划管道：槽位抽取合并 → 追问/拦截 → plan_trip ----
        ctx.route = "plan"
        return self.run_plan_pipeline(session, utterance, ctx)

    # -------------------------------------------------- 规划管道
    def run_plan_pipeline(self, session, utterance: str, ctx) -> Reply:
        """槽位抽取与合并、追问、缺口假设、plan_trip 执行与回复构建。

        整段自原 _turn_impl 逐行搬移（第三阶段再拆 PlanHandler 与
        prepare_slots / clarify_or_plan 子阶段）；业务语义与顺序不变。
        """
        delta = agent.extract_slots(utterance, today=session.today, client=session.client)
        explicit = _explicit_fields(utterance, delta)
        lead_notes: list[str] = []
        # 这轮改动前的槽位：用来判断"用户要的调整是不是本来就已经满足"
        prev_slots = dict(session.slots or {})

        # 「把第2天的行程改成…」：点名某一天。先验天数合法——超出方案天数
        # 就如实说，不能把第 9 天硬塞进 4 天的行程里装没听见。
        _day_no = intent.parse_day_target(utterance)
        if session.plan_made and _day_no is not None:
            _plan_days = len(((session.last_plan or {}).get("days_plan")) or [])
            if _plan_days and _day_no > _plan_days:
                text = (f"这份行程一共 {_plan_days} 天，改不了第 {_day_no} 天。\n\n"
                        f"想加天先说「多玩一天」，我重排完你再点那一天的安排。")
                session.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=session.turn_no,
                             ask_rounds=session.ask_rounds,
                             slots=dict(session.slots), render=text)

        # 「加上都江堰，安排一天」不是"整趟 1 天"——是给新加的景点留一整天；
        # 「多住一天 / 延长两天 / 加一天」是顺延，不是"整趟 N 天"。
        # 规则层已挡一轮（agent.extract_slots），这里兜住 LLM 抽出来的结果。
        _dm = re.search(r"(安排|排|留|住|延长|加|多玩)\s*([一二两三四五六七八九十\d]+)\s*天",
                        utterance)
        if _dm and session.plan_made:
            _n = agent.cn_to_int(_dm.group(2)) or 1
            _verb = _dm.group(1)
            _is_ext = (_verb in ("延长", "加", "多玩")
                       or bool(re.search(r"多住|多玩|多待|多加|延长", utterance)))
            if _is_ext:
                _new = (prev_slots.get("days") or 1) + _n
                if (delta.get("days") or 0) != _new or delta.get("days_extend"):
                    delta = dict(delta)
                    delta["days"] = _new
                    delta.pop("days_extend", None)
                    explicit.add("days")
                    lead_notes.append(f"按「{_dm.group(0)}」把行程延长到 {_new} 天")
            elif "days" in explicit and (prev_slots.get("days") or 0) > (delta.get("days") or 0):
                delta = dict(delta)
                delta["days"] = None
                delta.pop("days_extend", None)
                explicit.discard("days")
                lead_notes.append(
                    f"「{_dm.group(0)}」按给新加的景点留出整天理解，"
                    f"行程仍是 {prev_slots.get('days')} 天")

        # 上一轮问过「X 在哪个城市」，这一轮用户答的城市要把 X 带上。
        # 少了这一步，"迪士尼在哪个城市？"→"上海" 会把迪士尼本身弄丢，
        # 用户看到的是一份跟迪士尼毫无关系的上海行程。
        if session.pending_spot and "destination" in explicit:
            # 含分隔符的串是城市组合不是景点（多城请求），绝不能挂成必去点
            if re.search(r"[、，,]", session.pending_spot):
                session.pending_spot = None
            else:
                delta = dict(delta)
                delta["must_visit"] = sorted(set(
                    (delta.get("must_visit") or []) + [session.pending_spot]))
                explicit.add("must_visit")
                session.pending_must.append(session.pending_spot)
                lead_notes.append(f"把「{session.pending_spot}」加进{delta.get('destination')}的行程")
                session.pending_spot = None

        # ---- 景点被当成了城市：「安排去迪士尼的行程」 ----
        # 抽取器会把「去迪士尼」里的迪士尼写进 destination。可迪士尼不是城市，
        # 库里当然没有"迪士尼"的景点和酒店数据，于是系统一本正经地回
        # 「我这边没有迪士尼的数据…要不要按你刚才聊过的上海排？」——
        # 用户只是想往已排好的行程里加一站，却被告知排不了，还差点把上一份行程弄丢。
        # 正确的做法：认得出它是景点，就归到它所在的城市，按"必去"加进现有行程。
        d_dest = delta.get("destination")
        if (d_dest and "destination" in explicit and not agent.is_city(d_dest)
                and not agent.is_region(d_dest)):
            hit = agent.spot_city(d_dest, prefer=session.slots.get("destination"))
            cur = session.slots.get("destination")
            if hit or cur:
                delta = dict(delta)
                delta["destination"] = None
                explicit.discard("destination")
                spot_name = d_dest
                city = (hit[0] if hit else cur) or ""
                if hit:
                    # 用户只说核心词，库里有全名就用全名（迪士尼 → 上海迪士尼乐园）：
                    # 编排器按名字互相包含匹配，全名更容易命中候选池
                    spot_name = d_dest if len(d_dest) >= len(hit[1]) else hit[1]
                if city and city != cur:
                    # 景点在别的城市（大理的行程 + 迪士尼 → 上海）：这是新的一趟
                    delta["destination"] = city
                    explicit.add("destination")
                    lead_notes.append(
                        f"「{d_dest}」是景点不是城市，按它在的{city}排")
                else:
                    lead_notes.append(
                        f"「{d_dest}」是景点不是城市，已加进{city}这份行程")
                delta["must_visit"] = sorted(set(
                    (delta.get("must_visit") or []) + [spot_name]))
                explicit.add("must_visit")
                session.pending_must.append(spot_name)

        # 只说了个景点名（"迪士尼"）：已经排好行程时，按"再加一站"理解。
        # 否则它会走到"没有目的地"的分支，反问你"你想去哪儿"——刚排好的行程像丢了。
        _bare = utterance.strip().strip("。！!？?")
        if (not explicit and session.plan_made and session.slots.get("destination")
                and 2 <= len(_bare) <= 10):
            _hit = agent.spot_city(_bare, prefer=session.slots.get("destination"))
            if _hit and _hit[0] == session.slots.get("destination"):
                delta = dict(delta)
                delta["must_visit"] = [(_bare if len(_bare) >= len(_hit[1]) else _hit[1])]
                explicit.add("must_visit")
                session.pending_must.append(delta["must_visit"][0])
                lead_notes.append(f"把「{_bare}」加进刚才那份行程")

        # 「一定要去喜洲古镇」：景点名不是城市。已有有效目的地时，本轮点名/
        # 排除清单里的片段不该把目的地顶掉——它是 must_visit/avoid，
        # 不是 destination。否则大理的行程会被"喜洲古镇没有数据"打断。
        # 但「想去云南玩」是换目的地（带玩/旅游字眼的目的地级陈述），
        # 该拦的还是不拦——守卫只作用于纯景点点名。
        d_dest = delta.get("destination")
        if (d_dest and session.slots.get("destination")
                and "destination" in explicit
                and not re.search(r"玩|旅游|度假|旅行", utterance)
                and d_dest not in serviceable_cities()
                and d_dest in ((delta.get("must_visit") or [])
                               + (delta.get("avoid") or []))):
            delta = dict(delta)
            delta["destination"] = None
            explicit.discard("destination")

        # 撤销排除的口语说法：「…了，还是加回来吧」「那个豫园还是安排上吧」。
        # 这类句子里景点名在动词前面，规则正则未必抓得到（也可能只抓到虚词），
        # 但句子里明明白白出现了之前被排除的那个景点——按名字共现捞回来，
        # 否则用户反复说"加回来"都只会得到一句"已按你的要求避开：豫园"。
        if session.slots.get("avoid") and _restore_intent(utterance):
            back = [a for a in session.slots["avoid"] if a and a in utterance]
            if back:
                delta = dict(delta)
                delta["must_visit"] = sorted(set((delta.get("must_visit") or []) + back))
                # 模型有时会把"加回来"的景点同时写进两个数组，这里以本轮改口为准
                delta["avoid"] = [a for a in (delta.get("avoid") or [])
                                  if not any(name_hit(a, b) for b in back)]
                explicit.add("must_visit")

        # 点名「第N天」时，这轮新加的必去点归到那一天，不再全局撒——
        # 全局必去在第一天就会被占位，「第3天去喜洲」实际会排到 Day 1。
        if session.plan_made and _day_no is not None and delta.get("must_visit"):
            _moved = [m for m in delta["must_visit"] if m]
            delta = dict(delta)
            delta["must_visit"] = []
            explicit.discard("must_visit")
            _dt = dict(delta.get("day_targets") or {})
            _dt[str(_day_no)] = sorted(set(
                (_dt.get(str(_day_no)) or []) + _moved))
            delta["day_targets"] = _dt
            explicit.add("day_targets")
            # 老的必去清单里同名点也挪过去：留着的话它在别的天照样被当必去占位
            if session.slots:
                _old = [m0 for m0 in (session.slots.get("must_visit") or [])
                        if any(name_hit(m0, mv) for mv in _moved)]
                if _old:
                    session.slots = dict(session.slots)
                    session.slots["must_visit"] = [
                        m0 for m0 in (session.slots.get("must_visit") or [])
                        if m0 not in _old]
            lead_notes.append(f"「{'、'.join(_moved)}」排进第 {_day_no} 天")

        if session.slots:
            session.slots, session.total_is_derived = merge_slots(
                session.slots, delta, explicit, session.total_is_derived)
        else:
            session.slots = delta
            # 总额是"人均×人数"派生出来的，还是用户直接说的？
            # 派生值要在人数变化时跟着重算，用户明说的总额则不能动。
            session.total_is_derived = (bool(delta.get("budget_per_person"))
                                     and "budget_total" not in explicit)
            session.slots["_source"] = delta.get("_source", "rules")

        # 跨会话记住的硬约束（不吃辣等）每轮并入；之前 Session 收了 memory
        # 却从未使用，--memory 参数实际是摆设
        if session.memory is not None:
            session.slots = session.memory.apply_to(session.slots)

        # 节奏词不能当兴趣流通：抽取侧（llm.coerce_slots / agent.extract_slots）
        # 已经拦了一道，这里再清一次是为了历史槽位与跨轮继承——否则上一轮留下的
        # 「休闲」会让之后每份方案都带一句"兴趣没被满足"的假警报。
        agent.sanitize_prefs(session.slots)

        # 用户选定了单个城市 → 多城待选清单完成使命（「成都、重庆」→「成都」）
        if session.slots.get("destination") and "destination" in explicit:
            session.slots.pop("multi_cities", None)
            session.pending_spot = None

        # 人数：用户明说才算"有了"。party_size 默认 1，不看这句话会把
        # "没说几个人" 当成 "说了 1 个人"，直接出一份单人预算的行程。
        if "party_size" in explicit or (session.slots.get("party_size") or 1) != 1:
            session.party_stated = True

        dest = session.slots.get("destination")
        # 换了目的地 = 用户要的是另一趟旅行：追问额度、模糊标记都该重来。
        # 旧实现里 field_asks 只增不减，一次会话里排过一份行程之后，
        # 后面每次说新城市都直接出方案——天数、日期、人数全是猜的。
        prev_trip = session.trip_dest
        if dest and session.trip_dest and dest != session.trip_dest:
            session.field_asks.clear()
            session.asked_fields.clear()
            session.vague_fields.clear()
            session.ask_rounds = 0
            session.vague_hits = 0
        if dest:
            session.trip_dest = dest

        missing = [f for f, _, _ in ASK_RULES if not session._has(f)]
        # 先判"是不是城市/景点"，再谈覆盖：景点名（迪士尼/外滩）不该被送去
        # ensure_city 假装补一座同名城市；但没收录的真实城市（样例城这种）
        # 仍要走自动补全，不能被误拦。
        city_like = bool(dest) and agent.is_city(dest)
        needs_city = bool(dest) and not city_like and agent.looks_like_spot(dest)
        if dest and not needs_city:
            needs_city = not session._coverage(dest)
        # 记住最近有效的目的地：降级时"接着你刚才聊的城市排"，而不是按字典序随便挑
        if dest and not needs_city:
            session.last_good_dest = dest
        if dest:
            session.last_city = dest

        # 用户明说"还没定/看情况/随便"时，把那件事标记成模糊：不再追问，直接假设。
        # 「过几天」「看情况」本身就是一个答案，追着问第二遍只会让人烦。
        # 但"还没定"是针对某一件的："随便/你看着办"才是整体甩手——两者不能混。
        # 「随便」当回答用时只解决被问的那一件：问"什么时候出发"回"随便"，
        # 不代表天数和人数也随便——否则一次回答就把后面所有问题都吞掉，
        # 系统随后直接出一份天数、人数全靠猜的行程（用户投诉的正是这个）。
        if any(k in utterance for k in _GLOBAL_VAGUE):
            pend = session.pending_field
            session.vague_hits += 1
            # 第一次「你看着办」只当回答被问的那一件；连说两次就是真甩手了，
            # 把所有缺口一次性按常用情况假设，不再一件一件问下去。
            if (session.vague_hits >= 2
                    or pend not in ("date_range", "days", "party_size")
                    or session._has(pend)):
                session.vague_fields |= {f for f in ("date_range", "days", "party_size")
                                      if not session._has(f)}
            else:
                session.vague_fields.add(pend)
        elif explicit:
            session.vague_hits = 0
        else:
            if (session.pending_field in ("date_range", "days")
                    and not session._has(session.pending_field)
                    and session._vague(session.pending_field, utterance)):
                session.vague_fields.add(session.pending_field)
            if any(k in utterance for k in _TIME_TOPIC) and session._vague("date_range", utterance):
                session.vague_fields.add("date_range")
            if any(k in utterance for k in _DAYS_TOPIC) and session._vague("days", utterance):
                session.vague_fields.add("days")

        # 这轮是不是在反问我（"你不问我从哪出发吗"）——那不是回答，得先回应再问回去
        backq = utterance if (not explicit and session._is_backquestion(utterance)) else ""

        # 追问策略（第二次改）：
        # 目的地是硬门槛，没有它绝不排方案（旧逻辑会拿字典序第一个城市顶上，
        # 于是连问三次北京天气就被排去了上海）。
        # 时间/天数是一次只问一件：问过没答就换个说法再问，问满 2 次仍空着
        # 就按常见情况假设并出方案——绝不因为"问过了"就当"答过了"。
        question = asked = None
        # 多城连玩请求（「去成都、重庆玩5天」「先成都后重庆」）：当前一次
        # 只能排一个城市的详细行程。绝不能把城市串追问成"在哪个城市"、
        # 更不能挂成景点硬排（实测变成重庆 5 天+「成都、重庆」假景点）——
        # 明确说明能力边界，让用户挑先排哪个，这才对得起他的输入。
        _mc = [c for c in (session.slots.get("multi_cities") or []) if c]
        if len(_mc) >= 2 and not dest:
            asked = ["destination"]
            question = (f"{'、'.join(_mc)}——多城市连线暂时排不了，"
                        f"我一次只能出**一个城市**的详细行程。\n"
                        f"先排哪个？说城市名我就开始（另一个想玩的时候，"
                        f"再说一句「去XX玩N天」就行）。")
        elif not dest:
            asked = ["destination"]
            if session.last_city:
                question = (f"你是想排「{session.last_city}」的行程吗？确认一下我就开始。"
                            f"\n如果只是想知道那边天气，直接问我，我查给你看。")
            else:
                question = ("你想去哪儿？给我城市名就能开始排。"
                            "\n（只想先了解情况也可以问：某地天气、城际班次、城市景点）")
        elif needs_city:
            asked = ["destination"]
            cities = "、".join(serviceable_cities())
            res = session.fetched.get(dest) or {}
            why = {
                "failed": "在线数据源没连上",
                "empty": "在线也没检索到可用景点",
                "unresolved": "这个地名没能定位到坐标",
                "disabled": "在线检索功能关着",
            }.get(res.get("status", ""), "")
            why = f"（我刚试着在线补了一次：{why}）" if why else ""
            if not city_like and not agent.is_region(dest):
                # 不是城市，也不是省：景点/商圈/写错的地名。别把"没有景点数据"
                # 当成结论——它连城市都不是，问一句它在哪个城市更有用。
                # 记住这个名字：用户下一句答的城市要把它一起安排。
                session.pending_spot = dest
                question = (f"「{dest}」听着不像城市名，我这边是按城市排行程的。"
                            f"\n它在哪个城市？说一句「{dest}在XX」或者直接给我城市名，"
                            f"我把它安排进去。")
            elif "destination" in session.asked_fields:
                # 已经说过一次没有数据了，别复读同一句：能定位就给天气，定位不了
                # （省份、拼错的地名）就直接把可服务城市摊开，别绕弯
                weather = (intent.weather_answer(dest, "未来三天", client=session.client)
                           if real_tools.resolve_geo(dest) else "")
                if weather and "没找到" not in weather:
                    question = weather
                else:
                    question = (f"「{dest}」我这边确实定位不到、也排不了行程。{why}"
                                f"\n能排出具体行程的是 {cities}；"
                                f"说上面任意一个城市名，我马上开始。")
            else:
                alt = (f"要不要按你刚才聊过的「{session.last_good_dest}」排？"
                       if session.last_good_dest and session.last_good_dest != dest else "")
                question = (f"「{dest}」我这边没有景点和酒店数据，硬排会给你一份空行程，所以先不排。"
                            f"{why}"
                            f"\n{alt}能排出具体行程的是 {cities}；"
                            f"{dest}的天气和班次我倒是可以查。")
        else:
            # 已经出过方案时，"重新排一下"是改现有行程，不是从零开始：
            # 已确认的目的地/天数/日期都还在，不该当缺口重问一遍。
            replan = bool(_REPLAN_RE.search(utterance)) and session.plan_made
            if replan and session.last_plan_desc:
                lead_notes.append(f"接着刚才那份「{session.last_plan_desc}」重排")
            # 改的是同一趟行程（加/减景点、重新排）：已确认过的软信息不再重问。
            # 换了目的地则算另一趟，该问的照问（人数不能从上趟沿用）。
            same_trip = bool(session.plan_made and dest and dest == prev_trip)
            field = (None if dest and (replan or same_trip)
                     else session._next_ask(missing))
            if field:
                attempt = session.field_asks.get(field, 0) + 1
                asked = [field]
                question = session._compose_question(asked, attempt=attempt, backq=backq)
            elif question is None and session.plan_made and not explicit \
                    and not replan and not lead_notes:
                # 无脑重排拦截：方案已出、这句没带来任何新条件、也没说"重新排"
                # ——再跑一遍 plan_trip 只会原样再出一份（白等几十秒）。实测
                # 「推荐几家驴肉火烧」就这样被整单重排，用户体感是"聊两句它
                # 又自顾自排行程"。
                # 这类「规则认不出、又不是改行程」的话（「你是什么模型」
                # 「你太笨了」）先交给模型自由作答：它有上下文、能说人话；
                # 模型不可用/答得跑偏才回落到把改法亮出来的固定话术。
                said = llm.chat_reply(session.client, utterance,
                                      plan_desc=session.last_plan_desc)
                text = said or (
                    "行程已经在上面了。想改的话直接说：加减景点（「加个XX」"
                    "「不想去XX」）、改节奏（「别太赶」）、改天数、换城市都行；"
                    "问景点、问吃的、问天气也随便聊。")
                session.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=session.turn_no,
                             ask_rounds=session.ask_rounds,
                             slots=dict(session.slots), render=text)
        # 其余情况（城市有了、软信息问满或用户说过"还没定"）→ 带假设出方案

        if question is not None:
            session.ask_rounds += 1
            session.asked_fields.update(asked)
            for f in asked:
                if f == "destination":
                    continue        # 目的地是硬门槛，不占软问题的追问预算
                session.field_asks[f] = session.field_asks.get(f, 0) + 1
            session.pending_field = asked[0] if asked else None
            session.history.append({"role": "agent", "content": question})
            return Reply(mode="clarify", turn_no=session.turn_no,
                         ask_rounds=session.ask_rounds, question=question,
                         asked=asked, slots=dict(session.slots),
                         render=question)
        session.pending_field = None

        # 出方案：缺口用假设补齐并标注
        slots, assumptions = session._fill_assumptions(missing)
        session.slots = slots

        # 「别太赶」而节奏本来就在最松的一档：重排出来必然一模一样。
        # 这里**不许**再跑一遍 plan_trip（白等 50 秒），更不许说"这一版只把
        # 时间与动线重排了一遍"——行程和预算一个字都没变，那是在骗人。
        # 但"没得再松"≠"没事可做"：行程真超额（质检口径）时要有实际动作——
        # 删得动的（非必去点）直接删；全是必去点装不下就如实摊牌让用户取舍。
        # （杭州实测：质检报"3 个景点 > 上限 2"，点别太赶却回"已经最松"）
        if "pace" in explicit and session.plan_made:
            old_pace, new_pace = prev_slots.get("pace"), slots.get("pace")
            # 只放松节奏（这轮没同时改景点/天数/目的地）：就地按新上限删点，不重排。
            # 重排会把当天装不下的点"挪"到别的天——杭州 3 天实测：点「别太赶」，
            # Day 2 的雷峰塔被挪到返程日，景点总数与预算一个没少；用户要的是
            # "少去一个点"，不是"把点藏到别的天"。重排还要白等 50 秒。
            # 如果同时说了别的（加/去景点、改天数），必须整单重排，不能只删。
            loosening = (_pace_rank(new_pace) < _pace_rank(old_pace)
                         and explicit <= {"pace"})
            if new_pace == old_pace or loosening:
                session.slots = slots
                # 概要里的「节奏」跟着改：不重排的话 plan["slots"] 还是旧档
                session._sync_plan_pace(new_pace)
                trimmed = session._trim_overcap(new_pace)
                if trimmed is not None:
                    note, _removed = trimmed
                    text = note + agent.render(session.last_plan)
                    session.history.append({"role": "agent", "content": text})
                    return Reply(mode="plan", turn_no=session.turn_no,
                                 ask_rounds=session.ask_rounds,
                                 slots=dict(session.slots), plan=session.last_plan,
                                 render=text)
                text = session._pace_noop_answer(new_pace, just_loosened=loosening)
                session.history.append({"role": "agent", "content": text})
                return Reply(mode="answer", turn_no=session.turn_no,
                             ask_rounds=session.ask_rounds, slots=dict(session.slots),
                             render=text)

        trace: list = []
        session._phase("正在核对目的地资料，准备编排…")
        result = agent.plan_trip(slots, trace, client=session.client, today=session.today,
                                 on_phase=session._phase)
        ctx.tool_results["plan_trip"] = {"days": len(result.get("days_plan") or [])}
        # 把折算后的槽位写回会话：plan_trip 内部会把"下个月"折算成具体日期，
        # 不回写的话后续轮次拿到的还是 month-only，且 Reply.slots 与方案不一致。
        session.slots = result["slots"]
        slots = result["slots"]
        assumptions = result.get("assumptions", []) + assumptions

        # 位置是估算的点（未收录景点）必须在方案抬头就说明，不能只写在当天卡片里：
        # 用户看到"都江堰"以为交通时间都算过了，实际这一站根本没算路。
        _uncertain = sorted({n["name"] for d in result["days_plan"]
                             for n in d["nodes"]
                             if "位置待确认" in (n.get("transit") or "")})
        if _uncertain:
            assumptions.append(
                "、".join(_uncertain) + " 的位置是估算的（资料库没收录确切坐标），"
                "这一站没有计入交通时间；如果它在市郊或跨城，单程可能要 1~2 小时，"
                "告诉我我按跨城一日游留足往返时间")

        # 兴趣到底进没进行程：用户说「大熊猫 / 喝茶 / 老街」，行程里一个都没有
        # 就等于没听见。能对上的已经排进去了；一个都对不上就如实说明，
        # 而不是让这份"热门景点拼盘"冒充个性化行程。
        # 对上也要分强弱：「喝茶」排到人民公园是靠"公园"沾边，不是真的茶馆，
        # 这份差别必须说出来，否则用户永远不知道这条偏好到底满足了没有。
        _prefs = slots.get("soft_prefs") or []
        if _prefs:
            items = (result.get("pois") or {}).get("items") or []
            planned = {n.get("name") for d in result["days_plan"]
                       for n in d["nodes"]}
            landed: dict[str, str] = {}
            for p in _prefs:
                lv = ""
                for it in items:
                    if it.get("name") not in planned:
                        continue
                    m = composer.interest_match(p, it)
                    if m and (m[0] == "strong" or not lv):
                        lv = m[0]
                    if lv == "strong":
                        break
                if lv:
                    landed[p] = lv
            hit = list(landed)
            weak = [p for p, lv in landed.items() if lv == "weak"]
            if weak:
                assumptions.append(
                    "兴趣「" + "、".join(weak) + "」在"
                    + str(slots.get("destination") or "当地")
                    + "的资料里没有直接对应的景点，只按相近的点就近安排了"
                      "（行程里标为「近似匹配」）；想要专门的安排，说个具体名字我加进去")
            missed = [p for p in _prefs if p not in hit]
            if missed:
                # 分清两种"没安排"：库里压根没有对应景点，还是有名额但没排进去。
                # 混成一句"没有对应景点"会让用户以为系统不认识大熊猫。
                exists = [p for p in missed
                          if any(composer.interest_hit(p, it) for it in items)]
                if exists:
                    # 只说"天数/节奏装不下"等于没说：明明是别的点把名额占了，
                    # 用户既不知道占位的是谁，也不知道说一句就能换（成都实测：
                    # 「大熊猫基地」落空，方案里却排着杜甫草堂、武侯祠、望江楼公园）。
                    _days = slots.get("days") or 1
                    _pn = {"relaxed": "轻松", "moderate": "适中",
                           "intensive": "紧凑"}.get(slots.get("pace") or "", "")
                    _cap = composer.PACE_CAP.get(slots.get("pace") or "moderate", 3)
                    _musts = [m for m in (slots.get("must_visit") or []) if m]
                    filler: list[str] = []
                    for d in result["days_plan"]:
                        for n in d["nodes"]:
                            if n.get("type") != "attraction":
                                continue
                            nm = n.get("name") or ""
                            # 系统补的填充项（早市/伴手礼）不是用户的选择，
                            # 拿它当"换掉它"的建议等于让人拿自己的行程换自己
                            if nm.startswith("早市") or nm.startswith("自由活动"):
                                continue
                            if any(composer.name_hit(m, nm) for m in _musts):
                                continue
                            if any(composer.name_hit(e, nm) for e in exists):
                                continue
                            if nm not in filler:
                                filler.append(nm)
                    _why = ("当天往返的时间装不下" if _days == 1
                            else f"{_pn}节奏每天最多 {_cap} 个景点，名额被先排上的点占了")
                    _msg = ("你点名的「" + "、".join(exists) + "」这版没排进去——"
                            + _why
                            + ("，这几天排的是" + "、".join(filler) if filler else "")
                            + "。")
                    if filler:
                        _msg += (f"想优先它，说一句「把{filler[-1]}换成{exists[0]}」我就换；"
                                 "或者说「加一天」我重排")
                    else:
                        _msg += "加一天，或去掉一个已排的点，我就把它排进去"
                    assumptions.append(_msg)
                rest = [p for p in missed if p not in exists]
                if rest:
                    assumptions.append(
                        "你的兴趣「" + "、".join(rest) + "」在"
                        + str(slots.get("destination") or "当地")
                        + "的资料里没有直接对应的景点，这版只能先按当地热门点排；"
                          "说个具体名字（比如某家茶馆、某条老街），我加进去")

        # 行程修改的可见反馈：用户点名/排除之后必须明说"改了什么"。
        # 否则重排出来的方案看起来和上一份一样（"不想去豫园"像被无视），
        # 必去点排没排进去用户也要自己去行程里翻。
        if (explicit & {"must_visit", "avoid"}) or session.pending_must:
            rep = result.get("must_report") or {}
            placed = rep.get("placed") or []
            missed = rep.get("missed") or []
            unknown = rep.get("unresolved") or []
            if placed:
                assumptions.append("已确保安排：" + "、".join(placed))
            if rep.get("fetched"):
                assumptions.append("这次为你的点名补了资料："
                                   + "、".join(rep["fetched"]))
            if rep.get("custom"):
                assumptions.append(
                    "按你说的名称安排：" + "、".join(rep["custom"])
                    + "（资料库没收录、在线也没查到，位置是按市区估算的，"
                      "所以这一站不给距离和交通时间；如果它其实在市区外或跨城，"
                      "单程可能 1~2 小时，告诉我我按一日游重排，"
                      "开放时间和门票也请在出发前确认）")
            if unknown:
                # 和"排不下"分开说：库里压根没有这个景点，加天数也变不出来
                assumptions.append(
                    "没查到 " + "、".join(unknown) + " 的可用资料（资料库和在线源都没有），"
                    "所以没排进行程；写全名或告诉我它所在的城市/区，我再补一次")
            if missed:
                if (slots.get("days") or 1) == 1:
                    _why = ("（一日往返要赶当晚的车，可用时间装不下；"
                            "换成晚一点的返程班次，或把行程加成两天就能排进去）")
                else:
                    _why = "（受闭馆时间与动线限制，可让我加天数或换日期再试）"
                assumptions.append("点名景点本轮未能排入：" + "、".join(missed) + _why)
            if slots.get("avoid"):
                assumptions.append("已按你的要求避开：" + "、".join(slots["avoid"]))

        # 无障碍诉求（婴儿车/少台阶/轮椅）：不管有没有点名必去点都要说清——
        # 库里没有无障碍数据，"已确保安排"这句话对它不成立，必须给出
        # 取舍通道（杭州实测：概要标了「需无障碍」却没有任何落地说明）
        if "需无障碍" in (slots.get("hard_prefs") or []):
            assumptions.append(
                "无障碍需求（婴儿车/少台阶）我记下了，但景点资料里没有"
                "无障碍设施数据，**我没法确保每个点都满足**——台阶/坡道"
                "情况请以现场为准。对台阶特别敏感的话直接点名"
                "（比如「XX 台阶多，换一个」），我换平缓的景点重排")

        tiers = None
        if not slots.get("budget_total"):
            tiers = session._budget_tiers(result)

        text = agent.render(result, client=session.client)
        if tiers:
            text += "\n\n" + session._render_tiers(tiers)

        # 先说清"这轮相对上一份改了什么/沿用了什么"，再给方案。
        # 用户说"加个迪士尼"时，最怕的是系统装没听见上一份行程、重新给一份陌生的。
        if lead_notes:
            text = "> " + "；".join(dict.fromkeys(lead_notes)) + "。\n\n" + text

        # 记下这次出过方案：后续"重新排/再加一个"都按改现有行程处理
        session.plan_made = True
        session.last_plan = result
        session.last_plan_desc = f"{slots.get('destination')} {slots.get('days')} 天"
        session.pending_spot = None
        session.pending_must = []
        if assumptions:
            # assumptions = 方案自带假设 + 这里补的假设，是完整清单。
            # 直接整行替换「> 假设：」行。旧的"按；切分再补差集"会翻车：
            # 假设文案本身含「；」（如「不含大交通；定了告诉我」）会被切成
            # 两段，整句匹配不上，于是同一条假设被追加第二遍。
            line = "> 假设：" + "；".join(assumptions)
            if re.search(r"^> 假设：", text, re.M):
                text = re.sub(r"^> 假设：.*$", lambda _: line, text, count=1, flags=re.M)
            else:
                text = text.replace("\n", "\n" + line + "\n", 1)

        # 数据来源：这次联网补的数据要写明是补的、哪些是估算，
        # 不然用户会以为是资料库里的既有事实（门票/房价最容易被当真）。
        note = session._fetch_note(slots.get("destination") or "")
        if note:
            text += f"\n\n> 数据说明：{note}"

        session.history.append({"role": "agent", "content": text})
        return Reply(mode="plan", turn_no=session.turn_no, ask_rounds=session.ask_rounds,
                     slots=dict(slots), plan=result, assumptions=assumptions,
                     budget_tiers=tiers, trace=trace, render=text,
                     fetched=dict(session.fetched))

