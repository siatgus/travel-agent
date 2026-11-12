"""LLM 接入层：结构化输出校验 + 修复重问 + 静默降级。

三条设计原则（也是接真实模型时最容易踩的三个坑）：

1. **模型只做判断，不做创造。** 槽位输出落进枚举白名单，重排输出落进候选集白名单。
   模型幻觉出的城市、景点、日期一律在校验层被拦掉，不会流向编排器。
2. **校验失败不是抛异常，而是把错误回灌重问一次。** 第二次仍失败才降级到规则实现。
   用户不该为模型的格式问题付出一次失败或一段空白等待。
3. **降级必须可观测。** 每次调用都记账（attempts / degraded / errors），
   评测可以断言"模型坏了系统仍然出方案"，而不是靠肉眼检查。

不使用任何第三方依赖：标准库 urllib 足够，避免为一个 HTTP POST 引入 SDK。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# ------------------------------------------------------------------ 常量

PACES = {"relaxed", "moderate", "intensive"}

# 硬约束白名单：编排器只认这几个 key，模型编出的新词会被丢弃而不是静默传入
HARD_PREF_VOCAB = {
    "不吃辣", "忌高海拔", "需无障碍", "带幼儿", "素食",
    "忌海鲜", "怕晒", "忌长时间步行", "需亲子设施", "需午休",
}

# 景点标签白名单（软偏好只能是这些，否则打分函数无法匹配）
SOFT_PREF_VOCAB = {
    "古城", "古镇", "洱海", "苍山", "骑行", "徒步", "摄影", "人文",
    "古建", "夜市", "小吃", "白族", "户外", "博物馆", "温泉", "索道",
}

# 节奏 / 强度词——它们描述"怎么玩"，不是"想看什么"。
# 表单的「轻松慢逛」会拼成「不想太赶，轻松休闲一点」，模型很容易把「休闲」
# 抽进 soft_prefs；编排层在任何一个城市的库里都找不到名叫"休闲"的景点，
# 于是每份方案都多出一句「你的兴趣『休闲』在成都的资料里没有直接对应的景点」
# ——用户只勾了个节奏，却被通知"你的兴趣没被满足"（实测反馈）。
# 归入 pace 就够了，soft_prefs 见到一律丢弃。
PACE_WORDS = {
    "轻松", "休闲", "慢逛", "悠闲", "放松", "躺平", "慢节奏", "慢游",
    "不赶", "别太赶", "不想太赶", "特种兵", "打卡", "深度游", "自由行",
    "度假",
}

# 判断时先剥掉的虚词/程度词：「休闲一点」「轻松些」与「休闲」等价
_PACE_FILLER = re.compile(r"[\s,，、。.~～!！?？一点些更比较的啦啊吧就行]+")
# 剥掉收尾的泛化字（「休闲游」「度假之旅」）——只要还剩实体词就当兴趣保留
_PACE_TAIL = re.compile(r"(?:之旅|游玩|游|玩法|想法|需求)+$")


def is_pace_word(word: str) -> bool:
    """整串只由节奏语素构成 → 这是 pace，不是兴趣。

    「轻松休闲」「休闲一点」「休闲游」算；「轻松徒步」「摄影游」不算（有实体词）。
    """
    w = _PACE_FILLER.sub("", str(word or ""))
    if not w:
        return True
    rest = w
    for tok in sorted(PACE_WORDS, key=len, reverse=True):
        rest = rest.replace(tok, "")
    rest = _PACE_TAIL.sub("", rest)
    return not rest


# ------------------------------------------------------------------ 记账


@dataclass
class CallRecord:
    """一次逻辑调用（可能含多次尝试）的结果，供 trace 与评测断言使用。"""

    task: str
    ok: bool = False
    attempts: int = 0
    degraded: str | None = None      # 非空表示已降级，值为原因
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    latency_ms: int = 0

    def as_dict(self) -> dict:
        return {"task": self.task, "ok": self.ok, "attempts": self.attempts,
                "degraded": self.degraded, "errors": self.errors,
                "warnings": self.warnings, "latency_ms": self.latency_ms}


class LLMError(RuntimeError):
    """网络层错误（超时、HTTP 非 2xx、响应体不合法）。"""


class QuotaError(RuntimeError):
    """用户积分不足/触发每日上限——与 LLMError 分开：额度问题不该触发规则降级，
    而应该直接拒绝本次服务（否则等于免费送服务）。"""


# ------------------------------------------------------------------ 客户端


class Client:
    """OpenAI 兼容协议的极简客户端。只有 chat() 一个网络入口。"""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-4o-mini", timeout: float = 30.0,
                 max_attempts: int = 2, temperature: float = 0.2):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.temperature = temperature
        self.records: list[CallRecord] = []
        self.last_usage: dict | None = None     # 最近一次调用的 token 用量（计费用）
        self.task = ""                          # 当前调用属于哪类任务（写进用量流水）

    # -------------------------------------------------- 网络
    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature if temperature is None else temperature,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            raise LLMError(f"HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"网络错误: {e}") from e
        except json.JSONDecodeError as e:
            raise LLMError(f"响应非 JSON: {e}") from e

        try:
            self.last_usage = {
                "prompt": (data.get("usage") or {}).get("prompt_tokens"),
                "completion": (data.get("usage") or {}).get("completion_tokens"),
            }
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"响应结构异常: {str(data)[:200]}") from e

    # -------------------------------------------------- 结构化任务
    def json_task(self, task: str, system: str, user: str, coerce) -> tuple[dict | None, CallRecord]:
        """调用 + 校验 + 失败回灌重问。coerce(raw) -> (value, errors, warnings)。

        返回 (value 或 None, 记账)。value 为 None 时调用方应走规则降级。
        """
        rec = CallRecord(task=task)
        started = time.time()
        feedback = ""
        last_reason = ""

        for attempt in range(1, self.max_attempts + 1):
            rec.attempts = attempt
            self.task = task
            try:
                text = self.chat(system, user + feedback)
            except LLMError as e:
                last_reason = f"第 {attempt} 次调用失败：{e}"
                rec.errors.append(str(e))
                feedback = "\n\n（上一次请求失败，请直接返回 JSON，不要任何解释。）"
                continue

            raw, parse_err = extract_json(text)
            if parse_err:
                last_reason = f"第 {attempt} 次输出无法解析：{parse_err}"
                rec.errors.append(parse_err)
                feedback = (f"\n\n上一次输出无法解析（{parse_err}）。"
                            f"请只返回一个 JSON 对象，不要 ``` 代码围栏，不要任何解释文字。")
                continue

            value, errors, warnings = coerce(raw)
            rec.warnings.extend(warnings)
            if not errors:
                rec.ok = True
                rec.latency_ms = int((time.time() - started) * 1000)
                self.records.append(rec)
                return value, rec

            last_reason = f"第 {attempt} 次输出未通过校验：{'；'.join(errors)}"
            rec.errors.extend(errors)
            feedback = (f"\n\n上一次输出有以下问题：{'；'.join(errors)}。"
                        f"请修正后只返回 JSON 对象，不要解释。")

        rec.degraded = last_reason or "未知原因"
        rec.latency_ms = int((time.time() - started) * 1000)
        self.records.append(rec)
        return None, rec


class MeteredClient(Client):
    """计量客户端：包一层任意 Client，每次 chat() 走积分预扣-结算。

    不重写 json_task——它内部调的就是 self.chat，所以槽位抽取/重排
    的每一次模型调用都会被记账。调用失败全额退回预扣。
    额度不足抛 QuotaError（不是 LLMError）：额度问题应拒绝服务而非降级。
    """

    def __init__(self, inner: Client, uid: int, meter=None):
        self._inner = inner
        self.uid = uid
        self._meter = meter                      # auth 模块（延迟注入避免循环导入）
        self.model = inner.model
        self.base_url = inner.base_url
        self.api_key = inner.api_key
        self.temperature = inner.temperature
        self.timeout = inner.timeout
        self.max_attempts = inner.max_attempts
        self.records = inner.records
        self.last_usage = None
        self.task = ""

    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        import uuid
        chars = len(system) + len(user)
        est_tokens = min(6000, max(400, chars // 2 + 400))   # 粗估，结算时多退少补
        ref = f"u{self.uid}-{uuid.uuid4().hex[:10]}"
        self._meter.freeze(self.uid, self._meter.tokens_to_credits(est_tokens), ref)
        try:
            text = self._inner.chat(system, user, temperature=temperature)
        except Exception:
            self._meter.release(self.uid, ref)
            raise
        u = getattr(self._inner, "last_usage", None) or {}
        actual_tokens = ((u.get("prompt") or est_tokens)
                         + (u.get("completion") or 200))
        self._meter.settle(self.uid, ref,
                           self._meter.tokens_to_credits(actual_tokens),
                           model=self._inner.model,
                           task=getattr(self, "task", "") or "chat",
                           prompt_tokens=u.get("prompt"),
                           completion_tokens=u.get("completion"))
        self.last_usage = u
        return text


class ScriptedClient(Client):
    """按脚本返回响应，用于测试校验与降级路径（不联网、不需要 API Key）。

    响应项可以是字符串，也可以是异常实例（模拟超时/HTTP 错误）。
    """

    def __init__(self, responses: list):
        super().__init__(api_key="scripted", max_attempts=2)
        self._responses = list(responses)
        self._i = 0
        self.prompts: list[str] = []

    def chat(self, system: str, user: str, *, temperature: float | None = None) -> str:
        self.prompts.append(user)
        if self._i >= len(self._responses):
            raise LLMError("脚本已用尽")
        item = self._responses[self._i]
        self._i += 1
        if isinstance(item, Exception):
            raise item
        return item


# ------------------------------------------------------------------ 解析与校验


def extract_json(text: str) -> tuple[dict | list | None, str | None]:
    """从模型输出里抠出 JSON。容忍 ``` 围栏与前后噪声，不做单引号修补。"""
    if not text or not text.strip():
        return None, "输出为空"
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s), None
    except json.JSONDecodeError:
        pass
    # 退一步：取第一个 { 或 [ 到最后一个配对的 } 或 ]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = s.find(open_c), s.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1]), None
            except json.JSONDecodeError:
                continue
    return None, "找不到合法 JSON 结构"


def _to_int(v, lo: int, hi: int, label: str) -> tuple[int | None, str | None]:
    if isinstance(v, bool) or v is None:
        return None, f"{label} 不是数字"
    if isinstance(v, int):
        n = v
    elif isinstance(v, float):
        if not v.is_integer():
            return None, f"{label} 应为整数，收到 {v}"
        n = int(v)
    elif isinstance(v, str):
        m = re.search(r"\d+", v.replace(",", "").replace("，", ""))
        if not m:
            return None, f"{label} 无法解析为数字：{v!r}"
        n = int(m.group())
    else:
        return None, f"{label} 类型不支持：{type(v).__name__}"
    if not (lo <= n <= hi):
        return None, f"{label}={n} 超出合理区间 [{lo},{hi}]"
    return n, None


def coerce_slots(raw: dict, today: date | None = None):
    """把模型输出清洗成编排器能吃的槽位。返回 (slots, errors, warnings)。"""
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        return None, ["顶层必须是 JSON 对象"], []

    known = {"destination", "origin", "date_range", "days", "party_size",
             "budget_total", "budget_per_person", "hard_prefs", "soft_prefs",
             "pace", "must_visit", "avoid", "stay_pref",
             "child_age", "party_detail",
             "uncertain_fields", "confidence", "note"}
    for k in raw:
        if k not in known:
            warnings.append(f"丢弃未知字段 {k}")

    slots: dict = {
        "destination": None, "origin": None, "date_range": None, "days": None,
        "party_size": 1, "budget_total": None, "budget_per_person": None,
        "hard_prefs": [], "soft_prefs": [], "must_visit": [], "avoid": [],
        "pace": "moderate", "uncertain_fields": [], "stay_pref": None,
        "adults": None, "children": None, "child_age": None,
        "party_detail": None,
    }

    # 住宿偏好：一句话原样保留，选房建议要用它指片区
    v = raw.get("stay_pref")
    if isinstance(v, str) and v.strip():
        slots["stay_pref"] = v.strip()[:60]

    for key, lo, hi in (("days", 1, 30), ("party_size", 1, 20),
                        ("budget_total", 100, 200000),
                        ("budget_per_person", 100, 100000)):
        v = raw.get(key)
        if v is None:
            continue
        n, err = _to_int(v, lo, hi, key)
        if err:
            errors.append(err)
        else:
            slots[key] = n

    # 孩子年龄与人数构成：门票口径、家庭房、午休安排都靠它们，
    # 只拿 party_size 一个总数会把"2大1小"压扁成"3个大人"
    if raw.get("child_age") is not None:
        v, err = _to_int(raw.get("child_age"), 0, 17, "child_age")
        if err:
            errors.append(err)
        else:
            slots["child_age"] = v
    v = raw.get("party_detail")
    if isinstance(v, str) and v.strip():
        slots["party_detail"] = v.strip()[:60]
        _fm = re.search(r"(\d{1,2})\s*大(?:人)?\s*(\d{1,2})\s*(?:个)?"
                        r"(?:小|小孩|儿童|孩子|娃)", slots["party_detail"])
        if _fm:
            slots["adults"], slots["children"] = int(_fm.group(1)), int(_fm.group(2))
    # 大人/孩子齐了就以之和为准——模型常只回 party_detail 漏 party_size，
    # 默认值 1 会把「1 大 1 小」写成「共 1 人」，餐饮/交通全按 1 人算
    # （亲子实测反馈：构成写 1 大 1 小、总数却是 1）
    if slots.get("adults") is not None and slots.get("children") is not None:
        slots["party_size"] = slots["adults"] + slots["children"]

    for key in ("destination", "origin"):
        v = raw.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            errors.append(f"{key} 应为字符串")
        elif not (2 <= len(v.strip()) <= 12):
            errors.append(f"{key} 长度异常：{v!r}")
        else:
            slots[key] = v.strip()

    dr = raw.get("date_range")
    if dr is not None:
        if not isinstance(dr, dict):
            errors.append("date_range 应为对象")
        else:
            start = dr.get("start")
            if start:
                try:
                    sd = date.fromisoformat(str(start))
                except ValueError:
                    errors.append(f"date_range.start 不是合法日期：{start!r}")
                else:
                    if sd < today:
                        errors.append(f"date_range.start={sd} 早于今天 {today}，"
                                      f"用户说的是未来出行")
                    else:
                        slots["date_range"] = {"start": sd.isoformat(), "exact": True}
            elif dr.get("month"):
                m = re.fullmatch(r"(\d{4})-(\d{2})", str(dr["month"]))
                if not m:
                    errors.append(f"date_range.month 格式应为 YYYY-MM：{dr['month']!r}")
                else:
                    slots["date_range"] = {"month": str(dr["month"]), "exact": False}
            elif dr.get("day"):
                # 裸"20号"：只给日子没有月份。记下来交给 resolve_date_range
                # 按"本月优先、已过顺延下月"补齐——别当"未提供"，否则同一个
                # 问题要问两遍。
                try:
                    d = int(dr["day"])
                except (TypeError, ValueError):
                    d = 0
                if 1 <= d <= 31:
                    slots["date_range"] = {"day": d}
                else:
                    warnings.append(f"date_range.day 越界：{dr['day']!r}")
            else:
                warnings.append("date_range 既无 start 也无 month，视为未提供")

    hp = raw.get("hard_prefs")
    if hp is not None:
        if not isinstance(hp, list):
            errors.append("hard_prefs 应为数组")
        else:
            for item in hp:
                if not isinstance(item, str):
                    warnings.append(f"丢弃非字符串硬约束 {item!r}")
                elif item in HARD_PREF_VOCAB:
                    slots["hard_prefs"].append(item)
                else:
                    warnings.append(f"未知硬约束 {item!r} 已丢弃（不在白名单）")

    for key, vocab in (("soft_prefs", SOFT_PREF_VOCAB),):
        v = raw.get(key)
        if not isinstance(v, list):
            continue
        for item in v:
            if not isinstance(item, str):
                warnings.append(f"丢弃非字符串软偏好 {item!r}")
                continue
            word = item.strip()
            if not word:
                continue
            # 节奏词先摘出去：它属于 pace，「休闲」永远匹配不到景点，
            # 留在 soft_prefs 只会生成一条"你的兴趣没被满足"的假警报
            if is_pace_word(word):
                warnings.append(f"软偏好 {word!r} 是节奏词，已归入 pace，不算兴趣")
                continue
            # 兴趣是开放词表：用户说「大熊猫 / 喝茶 / 老街」，白名单里没有也不能丢——
            # 丢了就等于"偏好没被听见"，行程里自然一个都不出现。
            # 只做长度与数量限制，避免模型塞进一整句话。
            if word in vocab or 2 <= len(word) <= 8:
                slots[key].append(word)
                if word not in vocab:
                    warnings.append(f"软偏好 {word!r} 不在标准词表，按自由兴趣保留")
            else:
                warnings.append(f"软偏好 {word!r} 过长，已丢弃")
        slots[key] = list(dict.fromkeys(slots[key]))[:6]

    mv = raw.get("must_visit")
    if isinstance(mv, list):
        for item in mv:
            if isinstance(item, str) and 2 <= len(item) <= 20:
                slots["must_visit"].append(item)
            else:
                warnings.append(f"丢弃异常必去点 {item!r}")

    av = raw.get("avoid")
    if isinstance(av, list):
        for item in av:
            if isinstance(item, str) and 2 <= len(item) <= 20:
                slots["avoid"].append(item)
            else:
                warnings.append(f"丢弃异常排除项 {item!r}")
    # 排除赢过必去：两个清单同时点名一个景点时，以"不想去"为准
    slots["must_visit"] = [m for m in slots["must_visit"] if m not in slots["avoid"]]

    pace = raw.get("pace")
    if pace is not None:
        if pace in PACES:
            slots["pace"] = pace
        else:
            errors.append(f"pace={pace!r} 不在 {sorted(PACES)} 之内")
    elif not slots["destination"]:
        pass

    if slots["budget_per_person"] and not slots["budget_total"]:
        slots["budget_total"] = slots["budget_per_person"] * slots["party_size"]

    unc = raw.get("uncertain_fields")
    if isinstance(unc, list):
        slots["uncertain_fields"] = [str(x) for x in unc if isinstance(x, (str, int))]

    return slots, errors, warnings


def coerce_ranking(raw, allowed: list[str]):
    """校验重排结果：只能是从候选集里挑，不能新增、不能重复。

    幻觉 id 只记 warning 并丢弃；一个有效 id 都没有才算 error（触发重问）。
    缺失的候选按原相对顺序补到尾部，保证"重排"永远不会丢景点。
    """
    errors: list[str] = []
    warnings: list[str] = []

    if isinstance(raw, dict):
        for key in ("order", "ranking", "poi_ids", "ids"):
            if key in raw:
                raw = raw[key]
                break
        else:
            raw = []
    if not isinstance(raw, list):
        return None, ["返回结构不是数组或 {order:[...]}"], []

    allowed_set = set(allowed)
    out: list[str] = []
    for item in raw:
        pid = None
        if isinstance(item, str):
            pid = item
        elif isinstance(item, dict):
            for key in ("poi_id", "id", "name"):
                if key in item:
                    pid = item[key]
                    break
        if not isinstance(pid, str):
            warnings.append(f"丢弃无法识别的条目 {item!r}")
            continue
        pid = pid.strip()
        if pid not in allowed_set:
            warnings.append(f"丢弃不在候选集内的 {pid!r}（疑似幻觉）")
        elif pid in out:
            warnings.append(f"重复 id {pid!r} 已去重")
        else:
            out.append(pid)

    if not out:
        # 一个有效 id 都没有 = 完全幻觉。此时不该把候选全量补齐假装有序，
        # 而应明确判失败，交由重问或降级处理。
        errors.append("没有任何有效 poi_id")
        return None, errors, warnings

    # 未覆盖的候选补到尾部：保持规则排序下的相对顺序
    out.extend([a for a in allowed if a not in out])
    return out, errors, warnings


# ------------------------------------------------------------------ 高层任务


SLOT_SYSTEM = """你是行程需求抽取器。只输出一个 JSON 对象，不要任何解释、不要代码围栏。

字段定义：
- destination / origin: 城市名，字符串或 null。**只填一个城市名**；用户想多城连玩（「成都、重庆」「先成都后重庆」）时填 null，不要把城市串塞进来
- date_range: {"start":"YYYY-MM-DD"} 或 {"month":"YYYY-MM"} 或 {"day":N}（只说了"20号"这类裸日子时），无法确定填 null
- days: 整数 1-30，出发日与返程日算在同一天数内
- party_size: 整数 1-20，含本人，**孩子也算人**（「2大1小」→ 3）
- child_age: 最小孩子年龄（整数 0-17），没提年龄填 null
- party_detail: 人数构成一句话（「2 大 1 小（孩子 3 岁）」「夫妻2人」），没提填 null
- budget_total: 全队合计预算（整数，元），未提及填 null
- budget_per_person: 人均预算（整数，元），未提及填 null
- hard_prefs: 数组，只能从这些里选：不吃辣、忌高海拔、需无障碍、带幼儿、素食、忌海鲜、怕晒、忌长时间步行、需亲子设施、需午休
- soft_prefs: 数组，用户的兴趣偏好。常见值：古城、古镇、骑行、徒步、摄影、人文、古建、夜市、小吃、博物馆、温泉、索道；
  也**保留用户原话里的具体兴趣**（如大熊猫、喝茶、老街、看展、逛菜市场），每个 2~8 个字，最多 6 个。
  **节奏/强度词不算兴趣**（轻松、休闲、慢逛、悠闲、躺平、特种兵、打卡、深度游）——它们只决定 pace，不要写进 soft_prefs
- must_visit: 用户点名的景点名数组（「想去X」「必去X」「一定要去X」「帮我加上X」「增加X」）
- avoid: 用户明确不想去/要求排除的景点名数组（「不想去X」「不去X」「别安排X」「去掉X」）
- 改口：「把X加回来」「X还是安排上吧」「不去X了，改主意了」一律把 X 写进 must_visit，
  并且**不要**同时写进 avoid（两个数组互为反义，同时出现会让需求自相矛盾）
- pace: "relaxed" | "moderate" | "intensive"
- stay_pref: 住宿偏好一句话原样摘录（「住宿靠近地铁安静点」「住古城里」），没提填 null
- uncertain_fields: 你不确定的字段名数组

规则：
1. 今天是 {today}。"下个月"按今天推算；"11月8号"若已过则指明年。
2. 不要推测用户没说的事。没提到就填 null / 空数组，不要编造数字。
3. 金额一律取整数元，不要带单位符号。
"""


def extract_slots_llm(client: Client, text: str, today: date | None = None
                      ) -> tuple[dict | None, CallRecord]:
    """LLM 槽位抽取。失败返回 (None, 记账)，调用方回落规则抽取。"""
    today = today or date.today()

    def coerce(raw):
        return coerce_slots(raw, today)

    return client.json_task(
        task="extract_slots",
        # 用 replace 而不是 format：模板里含 JSON 示例的花括号，format 会误判为占位符
        system=SLOT_SYSTEM.replace("{today}", today.isoformat()),
        user=f"用户原话：{text}",
        coerce=coerce,
    )


RERANK_SYSTEM = """你是行程偏好排序器。给定候选景点与用户情况，按"用户更可能想去"排序。

硬性要求：
1. 只能使用给定的 poi_id，绝对不允许输出列表外的任何 id。
2. 严格输出 {"order": ["P3","P1",...]} —— 必须覆盖全部候选，不要解释、不要围栏。
3. 排序要基于用户明说的偏好（节奏、同行人、兴趣标签），不要凭景点名气。
"""


def rerank_llm(client: Client, candidates: list[dict], slots: dict
               ) -> tuple[list[str] | None, CallRecord]:
    """把 top-k 候选交给模型重排，返回 poi_id 顺序。失败返回 (None, 记账)。"""
    allowed = [c["poi_id"] for c in candidates]
    lines = []
    for c in candidates:
        lines.append(
            f"- poi_id={c['poi_id']}｜{c['name']}｜标签 {'/'.join(c.get('tags') or [])}"
            f"｜强度 {c.get('intensity')}｜时长 {c.get('duration_min')}min"
            f"｜门票 {c.get('cost')}元｜规则分 {c.get('_score')}")
    user = (f"用户情况：目的地 {slots.get('destination')}，{slots.get('days')} 天，"
            f"{slots.get('party_size')} 人，节奏 {slots.get('pace')}，"
            f"硬约束 {'/'.join(slots.get('hard_prefs') or []) or '无'}，"
            f"兴趣 {'/'.join(slots.get('soft_prefs') or []) or '未提'}。\n"
            f"候选（共 {len(candidates)} 个）：\n" + "\n".join(lines))

    def coerce(raw):
        return coerce_ranking(raw, allowed)

    return client.json_task("rerank", RERANK_SYSTEM, user, coerce)


# ----------------------------------------------------- 城市景点发现（知识库补全兜底）
#
# 在线数据源（Overpass）在部分网络环境下不可达。此时让模型按城市列一份候选
# 景点，坐标交给调用方做距离校验：编一个城市名容易，编一个"恰好落在城区
# 60 公里内"的坐标很难，这道校验能挡掉绝大部分幻觉。

DISCOVER_SYSTEM = """你是城市景点资料员。给定一个城市（境内或境外均可），列出该城市最值得去、真实存在的景点。

硬性要求：
1. 只输出 JSON：{"pois":[{"name":"景点常用名","name_en":"景点通用英文名","category":"attraction","duration_min":90,"ticket":0,"lat":36.80,"lon":118.05}]}
2. 8~12 个，必须是这个城市的景点；不确定的宁可不写。
3. category 只能是 attraction / museum / viewpoint / park。
4. duration_min 为建议游玩分钟数（30~300），ticket 为成人门票人民币整数（免费填 0）。
5. lat/lon 必须是该景点的真实坐标（WGS84，保留 2 位小数），不允许用城市中心凑数。
6. name_en 为该景点的通用英文名（用于境外地理编码核对位置；确实没有就填空字符串）。
不要解释、不要代码围栏。"""

_POI_CATEGORIES = {"attraction", "museum", "viewpoint", "park"}


def discover_pois(client: Client, city: str, limit: int = 12
                  ) -> tuple[list[dict] | None, CallRecord]:
    """模型给出的候选景点（含近似坐标）。有效条目不足 3 个视为失败。"""

    def coerce(raw):
        rows = raw.get("pois") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return None, ["pois 必须是数组"], []
        out: list[dict] = []
        warns: list[str] = []
        seen: set[str] = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = str(r.get("name") or "").strip()
            if not (2 <= len(name) <= 30):
                warns.append(f"丢弃非法名称 {name!r}")
                continue
            if name in seen:
                continue
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except (KeyError, TypeError, ValueError):
                warns.append(f"丢弃无坐标景点 {name}")
                continue
            if not (-90 < lat < 90 and -180 < lon < 180):
                warns.append(f"丢弃坐标越界景点 {name}")
                continue
            dur = _to_int(r.get("duration_min"), 20, 480, "duration_min")[0] or 90
            ticket = _to_int(r.get("ticket"), 0, 600, "ticket")[0]
            seen.add(name)
            out.append({
                "name": name,
                "name_en": str(r.get("name_en") or "").strip()[:60],
                "category": (r.get("category")
                             if r.get("category") in _POI_CATEGORIES else "attraction"),
                "duration_min": dur,
                "ticket": ticket or 0,
                "lat": round(lat, 4), "lon": round(lon, 4),
            })
            if len(out) >= limit:
                break
        if len(out) < 3:
            return None, [f"可用景点不足（{len(out)} 个）"], warns
        return out, [], warns

    return client.json_task("poi_discover", DISCOVER_SYSTEM, f"城市：{city}", coerce)


# ------------------------------------------------- 定向查单个景点（点名景点补全）
#
# 用户说「我想去上海迪士尼」而库里只有外滩/豫园时，编排器找不到这个 POI，
# 整句点名就被静默吞掉了。城市级抓取（discover_pois）回答不了"这一个在哪"，
# 所以要有一条按名字查的通道，坐标同样交给调用方做城区距离校验。

LOOKUP_SYSTEM = """你是景点资料员。给定城市和一个景点名，补全这一个景点的资料。

硬性要求：
1. 只输出 JSON：
{"found":true,"name":"景点正式全名","name_en":"English name","category":"attraction","duration_min":120,"ticket":0,
 "lat":31.14,"lon":121.66,"open_hours":"08:30-20:30","note":"一句话实用提示"}
2. 不确定它是否在该城市、或不确定它的位置，就输出 {"found":false}，不要猜。
3. lat/lon 必须是该景点的真实坐标（WGS84，保留 2 位小数），不允许用城市中心凑数。
4. duration_min 为建议游玩分钟数（30~480），ticket 为成人票价人民币整数（免费填 0）。
5. category 只能是 attraction / museum / viewpoint / park。
6. name_en 为该景点的通用英文名（用于境外地理编码核对位置；确实没有就填空字符串）。
不要解释、不要代码围栏。"""


def lookup_poi(client: Client, city: str, name: str
               ) -> tuple[dict | None, CallRecord]:
    """按名字查一个景点。found=false 或坐标越界都返回 None（由调用方决定下一步）。"""

    def coerce(raw):
        if not isinstance(raw, dict):
            return None, ["不是 JSON 对象"], []
        if not raw.get("found"):
            return None, ["模型认为该城市没有这个景点"], []
        nm = str(raw.get("name") or name).strip()
        if not (2 <= len(nm) <= 30):
            return None, ["名称非法"], []
        try:
            lat, lon = float(raw["lat"]), float(raw["lon"])
        except (KeyError, TypeError, ValueError):
            return None, ["缺少坐标"], []
        if not (-90 < lat < 90 and -180 < lon < 180):
            return None, ["坐标越界"], []
        return {
            "name": nm,
            "name_en": str(raw.get("name_en") or "").strip()[:60],
            "category": (raw.get("category")
                         if raw.get("category") in _POI_CATEGORIES else "attraction"),
            "duration_min": _to_int(raw.get("duration_min"), 20, 480,
                                    "duration_min")[0] or 120,
            "ticket": _to_int(raw.get("ticket"), 0, 999, "ticket")[0] or 0,
            "lat": round(lat, 4), "lon": round(lon, 4),
            "open_hours": str(raw.get("open_hours") or "")[:40],
            "note": str(raw.get("note") or "")[:80],
        }, [], []

    return client.json_task("poi_lookup", LOOKUP_SYSTEM,
                            f"城市：{city}\n景点：{name}", coerce)


# ------------------------------------------------- 景点名中译英（境外坐标校正）
#
# 天地图只覆盖国内，境外坐标兜底走 Nominatim（OSM），而 OSM 的主名几乎都是
# 英文——中文名直接查通常全军覆没（洛杉矶实测）。让模型顺带给出英文名：
# 城市级发现/点名补全在主调用里捎带（零额外开销），存量修复用这里的批量版。

_POI_EN_SYSTEM = """你是翻译。把给定的景点中文名翻译成该景点的通用英文名。

硬性要求：
1. 只输出 JSON：{"items":[{"zh":"中文名","en":"English name"}]}
2. items 必须与输入列表一一对应、顺序一致；不知道英文名就填空字符串，不要猜。
不要解释、不要代码围栏。"""


def poi_names_en(client: Client, city: str, names: list[str]
                 ) -> tuple[dict[str, str], CallRecord]:
    """批量把景点中文名译成英文名，返回 {中文名: 英文名}（查不到的不在表里）。"""

    def coerce(raw):
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return None, ["items 必须是数组"], []
        out: dict[str, str] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            zh = str(it.get("zh") or "").strip()
            en = str(it.get("en") or "").strip()
            if zh and en:
                out[zh] = en[:80]
        return out, [], []

    listing = "\n".join(f"- {n}" for n in names)
    val, rec = client.json_task("poi_name_en", _POI_EN_SYSTEM,
                                f"城市：{city}\n景点：\n{listing}", coerce)
    return (val or {}), rec


# ------------------------------------------------------- 自然语言回复（"说话"这一层）
#
# 之前的模型只干两件苦力：抽槽位、给候选重排。真正落到用户眼前的每一句
# （天气怎么说、景点怎么介绍、缺信息怎么问）都是字符串模板拼出来的，
# 所以「后台 token 不动」和「说话像机器人」是同一个根因。
# 这一层把工具查到的事实交给模型组织成人话，模板退成兜底。

_REPLY_STYLE = """你是「旅行规划助手」，一个在本地跑的、说话像真人的助手。

说话要求：
1. 像朋友聊天：短句、口语。不要「您好，根据您提供的信息」「以下是为您」这类客服腔。
2. 只使用【事实】里的数字和名称。禁止新增、改写、四舍五入任何数字（温度、价格、时刻、概率、距离、时长）。
3. 不要编造事实里没有的景点、酒店、车次、天气，也不要承诺代订、占座、支付。
4. 3~5 句，不超过 220 字。不要标题、表格、代码块、表情堆砌。
5. 不复述用户原话，不复述任务，直接给结论。
"""

_KIND_TASK = {
    "weather": "用户问天气。先说今天（或所问那天）到底怎么样，再补充后面一两天；"
               "最后给一句真正能用的出门建议，别写正确的废话。"
               "日期一律照抄【事实】里每一天的「相对日」和「日期」，不要自己推算"
               "明天是几号、几号是周几。",
    "poi": "用户问有什么好玩的。从事实里挑 3~5 个最值得去的，说清在哪个区、大概玩多久、"
           "门票多少、有什么讲究。不要把所有条目都念一遍。",
    "transport": "用户问交通。用事实里的车次/时刻/价格说清怎么走、哪班更划算或更省时间。",
    "smalltalk": "用户在打招呼、问你是谁/是什么模型/能做什么、道谢或告别。简短回应："
                 "问身份就说清你是这个产品里的旅行规划助手，背后接的是后台配置的大模型"
                 "（OpenAI 兼容接口，可换），不要自称某个具体商业型号（别冒充 GPT、文心等）；"
                 "再顺带说清你能做什么：查真实天气、真实班次、城市景点，也能直接排行程。",
    "clarify": "用户想排行程但信息不全。只问【要问的】里的那一件事（已经只列了一件，"
               "不要自己再加问题），并附一句具体建议让用户容易回答。"
               "如果【用户刚才的话】是在反问我而不是回答（比如问「你不问我从哪出发吗」），"
               "先用一句话正面回应【需要先回应的话】，再把问题接回去；"
               "这是第几次问已经写明，第二次问要换说法，别复读上一轮。",
    "assumption": "信息不全但要先给方案。用【假设】里的内容说清你替他定了什么，"
                  "语气是商量（「我先按…」），不是通知。",
    "plan_qa": "用户要你总结/概括/评价当前这份行程。只根据【事实】里的内容回答："
               "说清每天去哪儿、住哪、大概花销和要注意的假设；"
               "用户要一段话就给一段通顺的话，不要列表。"
               "禁止新增事实里没有的景点、日期或金额。",
    "plan_intro": "行程方案刚生成，为方案写开头一段「这趟怎么玩」导览：2~4 句"
                  "讲清整体玩法主线——这几天每天的主打体验怎么串起来，"
                  "可以让开头带一点对目的地氛围的常识性描绘（像朋友安利，"
                  "不是百科口吻）。只用【事实】里的城市、天数、每日主题和景点名；"
                  "禁止编造事实里没有的景点、酒店、价格、时刻、距离，"
                  "不要出现具体日期和温度。",
    "general": "用户问了一个通用旅行问题，【检索】是刚从网上搜到的结果摘要。"
               "优先依据检索内容回答，关键信息顺带注明来源（如「据马蜂窝」）；"
               "检索结果为空或与问题无关时，凭常识回答，并明确说一句"
               "这不是实时查证的信息、出行相关细节请再核实。"
               "不编造具体价格、政策条文和时刻。",
}

_REPLY_LIMIT = 700


def _clean_reply(text: str, limit: int = _REPLY_LIMIT) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    s = re.sub(r"^(回复|回答|答案)[:：]\s*", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s[:limit].rstrip() if len(s) > limit else s


def _num_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


def _collect_numbers(obj, out: set | None = None) -> set[str]:
    """把事实里出现过的所有数字抠出来（含字符串里的数字，如「10月31日」）。"""
    out = out if out is not None else set()
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.add(_num_str(obj))
    elif isinstance(obj, str):
        out.update(re.findall(r"\d+(?:\.\d+)?", obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _collect_numbers(k, out)
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            _collect_numbers(v, out)
    return out


def _numbers_faithful(text: str, facts: dict) -> bool:
    """文本里的大额数字必须来自事实。拦住「票价 128」「体感 25 度」这类脑补。

    1~10 的个位数放行：模型常写「挑 3 个」「两点建议」，那是叙述不是数据。
    """
    allowed = _collect_numbers(facts)
    for n in re.findall(r"\d+(?:\.\d+)?", text):
        if n in allowed:
            continue
        try:
            v = float(n)
        except ValueError:
            continue
        if "." not in n and 0 <= v <= 10:
            continue
        return False
    return True


def compose_reply(client: Client | None, kind: str, facts: dict,
                  user_text: str = "", must_keep: list | None = None) -> str | None:
    """让模型把结构化事实说成人话。任何异常/跑偏都返回 None，由调用方回退模板。

    must_keep 是关键数字/名称清单：模型漏掉任何一个就判定这轮输出不可信；
    _numbers_faithful 再拦一遍脑补出来的金额/温度。宁可回退模板，
    也不能让用户看到被改过的价格。
    额度不足抛 QuotaError，这里不捕获——那是拒绝服务，不是降级。
    """
    if client is None:
        return None
    task = _KIND_TASK.get(kind)
    if not task:
        return None
    parts = [f"【任务】{task}",
             f"【事实】{json.dumps(facts, ensure_ascii=False)}",
             f"【用户原话】{user_text or '（无）'}"]
    if must_keep:
        parts.append(f"【必须原样出现】{json.dumps([str(k) for k in must_keep], ensure_ascii=False)}")
    try:
        client.task = f"reply_{kind}"
    except Exception:
        pass
    try:
        text = client.chat(_REPLY_STYLE, "\n".join(parts))
    except LLMError:
        return None
    text = _clean_reply(text)
    if not text:
        return None
    if must_keep and not all(str(k) in text for k in must_keep):
        return None
    if not _numbers_faithful(text, facts):
        return None
    return text


_CHAT_SYS = """你是「旅行规划助手」，正在跟用户闲聊。规则：
1. 用户问你是谁、是什么模型、能做什么：说清你是这个产品里的旅行规划助手，
   背后接的是后台配置的大模型（OpenAI 兼容接口，可以换），不要冒充某个具体
   商业型号（GPT、文心一言等），也不要编造参数量、版本号。
2. 只聊助手本身、旅行、以及用户当前这趟行程的轻量话题。不知道的事直说不知道，
   不编造景点、价格、政策、时刻、开放时间。
3. 如果用户其实想改行程，用一句话点明怎么说（「加个XX」「不想去XX」「别太赶」
   「改成 5 天」「换个城市」）。
4. 2~4 句，口语，不超过 150 字。不要标题、列表、代码块，不要客服腔。"""


def chat_reply(client: Client | None, text: str,
               plan_desc: str = "") -> str | None:
    """闲聊/问助手身份时的自由作答；模型不可用或输出不可信时返回 None。

    这类问题没有【事实】可以照抄（「你是什么模型」不是查得到的数据），
    所以不能走 compose_reply 的事实约束模板：给它一套独立的闲聊规则，
    仍然硬拦"编造数字"——闲聊里出现价格/里程/时刻只可能是脑补。
    QuotaError 不捕获：额度不足要拒绝服务，不是降级。
    """
    if client is None:
        return None
    t = (text or "").strip()
    if not t:
        return None
    parts = [f"【用户说的】{t}"]
    if plan_desc:
        parts.append(f"【当前行程】{plan_desc}（已经排好了，不用再排一遍）")
    try:
        client.task = "reply_chat"
    except Exception:
        pass
    try:
        raw = client.chat(_CHAT_SYS, "\n".join(parts))
    except LLMError:
        return None
    out = _clean_reply(raw, 200)
    if not out:
        return None
    # 闲聊里冒出价格/时刻/里程只可能是脑补（「门票 80 元」「9 点开门」）；
    # 但「改成 5 天」这类引导语里的数字是正常的，所以只拦带单位的硬事实。
    for m in re.findall(r"\d+(?:\.\d+)?\s*(?:元|块|公里|千米|分钟|小时|点|℃|度|米)", out):
        if m.replace(" ", "") not in (text or "").replace(" ", "") \
                and m.replace(" ", "") not in (plan_desc or "").replace(" ", ""):
            return None
    return out


def unknown_spot_reply(client: Client | None, city: str, spot: str,
                       has_plan: bool = False) -> str | None:
    """库里和在线都查不到的点名：让模型把「查不到」说得像人话。

    不是补事实——事实没有。是让拒绝带上上下文和出路（这个名字通常是
    什么地方、接下来可以怎么说），而不是甩一句"换个我收录的城市"。
    提示词硬性禁止编造价格/时间等数字；输出里出现数字就整句弃用回模板
    ——查不到的地点还报数字，只可能是脑补。QuotaError 不捕获（拒绝服务≠降级）。
    """
    if client is None:
        return None
    sys_p = ("你是旅行规划助手。用户问的地点在资料库和联网检索里都查不到可靠资料。"
             "要求：如实承认查不到它的门票等具体信息，绝不编造价格、距离、营业时间等数字；"
             "可以凭常识说明这个名字通常指什么；结合上下文给一句有用的下一步建议"
             "（比如换个具体景点，或者说「我想去XX」强行排进行程）。"
             "中文口语，不超过4句，不要用列表和标题格式。")
    parts = [f"用户在问：「{spot}」", f"城市：{city or '未提及'}"]
    if has_plan:
        parts.append("用户当前已有一份这个城市的行程，问题很可能与行程里的安排有关。")
    try:
        text = _clean_reply(client.chat(sys_p, "\n".join(parts)))
    except LLMError:
        return None
    text = (text or "").strip()
    if not text or re.search(r"\d", text) or "免费" in text:
        return None
    return text


def rewrite_query(client: Client | None, question: str) -> str | None:
    """把用户的自然语言问题改写成搜索引擎关键词（空格分隔）。

    实测必应对整句自然语言会稀释成前两个字，必须给关键词。
    任何失败返回 None，调用方回落 websearch.keywords() 启发式。
    """
    if client is None:
        return None
    q = (question or "").strip()
    if len(q) < 2:
        return None
    sys_p = ("把旅行问题改写成 2~4 个适合搜索引擎的关键词，用空格分隔。"
             "只输出关键词本身：不要标点、不要引号、不要解释。"
             "例：「去日本旅游需要办签证吗」→ 日本 旅游签证 办理")
    try:
        text = _clean_reply(client.chat(sys_p, q), 60)
        text = text.strip().strip("\"'“”‘’「」")
        if not text or len(text) > 60:
            return None
        return text
    except LLMError:
        return None


# ------------------------------------------------------------------ 意图兜底

_INTENT_LABELS = ("weather", "poi", "transport", "plan_qa", "smalltalk",
                  "general", "plan", "none")

_INTENT_SYS = (
    "你是旅行助手的意图分类器。判断用户这句话想做什么，只输出 JSON："
    '{"intent":"..."}，不要解释。可选值：\n'
    "weather=问天气；poi=问景点/地方/门票/介绍/周边；transport=问交通班次；\n"
    "plan_qa=要总结或评价当前行程；\n"
    "plan=想排行程或改行程（说去哪儿玩几天、加/删/换景点、改天数酒店等需求）；\n"
    "smalltalk=打招呼、问你是谁/是什么模型/能做什么、道谢、告别等关于助手"
    "本人的对话；\n"
    "general=其他问题（签证、证件、注意事项、旅行常识等）；\n"
    "none=都不是——比如在回答追问、报信息。"
)


def classify_intent(client: Client | None, text: str) -> str | None:
    """规则分类器判为 other 时的 LLM 兜底：这句话到底是问什么。

    只在调用方觉得"这句像个问题但规则认不出"时用（省一次调用）。
    返回 _INTENT_LABELS 之一；模型/网络/输出格式任何问题都返回 None，
    调用方回落原流程。QuotaError 不在这里捕——额度不足要拒绝服务。
    """
    if client is None:
        return None
    t = (text or "").strip()
    if len(t) < 2:
        return None
    try:
        raw = client.chat(_INTENT_SYS, t)
        data, _ = extract_json(raw)
        v = data.get("intent") if isinstance(data, dict) else None
        return v if v in _INTENT_LABELS else None
    except LLMError:
        return None


# ------------------------------------------------------------------ 工厂


def from_env(**overrides) -> Client | None:
    """按环境变量构造客户端。没有 key 时返回 None，调用方应继续用规则实现。"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    kwargs = {"api_key": api_key,
              "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
              "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
              "timeout": float(os.environ.get("OPENAI_TIMEOUT", "30"))}
    kwargs.update(overrides)
    return Client(**kwargs)


# ------------------------------------------------------- 管理后台的模型设置
#
# 设置文件 data/.llm_settings.json（见 paths.py，可用 TRAVEL_LLM_SETTINGS 重定向），
# 由管理后台（admin.py）读写；优先级高于环境变量——用户在界面上显式配置的
# 意图，应当盖过启动脚本里写死的默认值。测试可用 TRAVEL_LLM_SETTINGS 重定向。

_SETTINGS_NAME = ".llm_settings.json"


def settings_path() -> Path:
    from . import paths
    return paths.llm_settings()


def load_settings() -> dict:
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_settings(cfg: dict) -> dict:
    """合并式保存，附 updated_at。返回实际落盘内容。"""
    merged = {**load_settings(), **cfg,
              "updated_at": datetime.now().isoformat(timespec="seconds")}
    settings_path().write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    return merged


def mask_key(key: str) -> str:
    """脱敏展示：只露头尾，中间打码。"""
    key = str(key or "")
    if len(key) <= 8:
        return key[:2] + "****" if key else ""
    return f"{key[:6]}****{key[-4:]}"


def active_info() -> dict:
    """当前真正会生效的配置（脱敏）。用于后台展示「正在用哪个模型」。

    source：settings=后台设置文件；env=环境变量兜底；""=未启用（纯规则）。
    与 from_settings() 的判断顺序保持一致，避免展示与实际不符。
    """
    s = load_settings()
    if s.get("enabled") and s.get("api_key"):
        return {"enabled": True, "source": "settings",
                "provider": str(s.get("provider") or ""),
                "model": str(s.get("model") or "gpt-4o-mini"),
                "base_url": str(s.get("base_url") or "https://api.openai.com/v1"),
                "key_masked": mask_key(s.get("api_key")),
                "timeout": s.get("timeout") or 60,
                "updated_at": str(s.get("updated_at") or "")}
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return {"enabled": True, "source": "env", "provider": "",
                "model": os.environ.get("OPENAI_MODEL", ""),
                "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                "key_masked": mask_key(env_key),
                "timeout": os.environ.get("OPENAI_TIMEOUT", "30"),
                "updated_at": ""}
    return {"enabled": False, "source": "", "provider": "", "model": "",
            "base_url": "", "key_masked": "", "timeout": s.get("timeout") or 60,
            "updated_at": str(s.get("updated_at") or "")}


def settings_summary() -> dict:
    """给管理后台展示的脱敏摘要（不含明文 Key）。"""
    s = load_settings()
    return {
        "enabled": bool(s.get("enabled")),
        "provider": str(s.get("provider") or ""),
        "base_url": str(s.get("base_url") or ""),
        "model": str(s.get("model") or ""),
        "timeout": s.get("timeout") or 60,
        "has_key": bool(s.get("api_key")),
        "key_masked": mask_key(s.get("api_key")),
        # 天地图 Key（行程页真实地图用），同一设置文件、同样只显示脱敏形式
        "has_tdt_key": bool(s.get("tdt_key")),
        "tdt_masked": mask_key(s.get("tdt_key")),
        # 高德 JS API Key 与安全密钥（行程页首选底图，用户指定），同样脱敏
        "has_amap_key": bool(s.get("amap_key")),
        "amap_masked": mask_key(s.get("amap_key")),
        "has_amap_security": bool(s.get("amap_security")),
        "amapsec_masked": mask_key(s.get("amap_security")),
        "updated_at": str(s.get("updated_at") or ""),
        "env_fallback": bool(os.environ.get("OPENAI_API_KEY")),
        "env_model": os.environ.get("OPENAI_MODEL", ""),
        "active": active_info(),          # 当前实际生效的那套（含来源）
    }


def from_settings(**overrides) -> Client | None:
    """构造客户端：设置文件（已启用且有 Key）优先，其次环境变量，都没有返回 None。"""
    s = load_settings()
    if s.get("enabled") and s.get("api_key"):
        kwargs = {"api_key": str(s["api_key"]),
                  "base_url": str(s.get("base_url") or "https://api.openai.com/v1"),
                  "model": str(s.get("model") or "gpt-4o-mini"),
                  "timeout": float(s.get("timeout") or 30)}
        kwargs.update(overrides)
        return Client(**kwargs)
    return from_env(**overrides)
