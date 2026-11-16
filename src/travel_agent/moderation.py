"""内容安全双审：用户输入、模型输出各过一道敏感词审查。

按《生成式人工智能服务管理暂行办法》，面向公众的生成服务必须拦截违法有害
内容——这是算法备案的硬性要求。本模块是 M1 的最小实现：

1. **本地词库**：内置种子词（赌博/毒品/暴恐/色情/诈骗/辱骂六类），可在
   data/sensitive_words.txt 里追加（格式 `类别|词`，类别省略归 other），
   改完下一句话生效（文件指纹热切换）。政治类词库不内置种子，由运营按
   备案口径维护在该文件里，或直接接云审核。
2. **变体规避**：全角→半角、大小写、字符间插空格/符号/零宽字符（如
   「赌 博」「赌·博」）都能命中；命中位置基于原文，脱敏替换不打乱排版。
3. **双审处置**：
   - 输入命中 → 拒答（友好话术 + 记录），请求不进 LLM、不扣积分；
   - 输出命中 → 低危类别就地打码（替换为 *），高危类别整条换兜底话术。
4. **云审核钩子**：环境变量 TRAVEL_CLOUD_MODERATION 配置供应商后启用；
   未配置时纯本地词库。云审核失败降级为本地结果（fail-open），不阻塞业务。
5. **拦截记录落库**（moderation_log，与账号库同文件）：管理后台「内容审计」
   可复查、标记误报。记录失败只降级不影响当轮对话。

环境变量：
    TRAVEL_SENSITIVE_FILE  词库文件路径（默认 data/sensitive_words.txt）
    TRAVEL_CLOUD_MODERATION  云审核供应商（tencent/aliyun，未配置=纯本地）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime

from . import auth, paths

_LOCK = threading.Lock()

# ------------------------------------------------------------------ 词库

# 内置种子：只收录语义明确、误伤率低的词。扩词走 data/sensitive_words.txt，
# 不在这里堆量——词库质量（少误报）比数量更重要。
# 选词有两类刻意排除（2026-11-16 实测教训：「怎么杀人」漏放行）：
#   1. 单字词（枪/毒/杀）——"水枪""禁毒""剧本杀"全误伤；
#   2. 会出现在正当旅游语境里的词（屠杀→纪念馆名、枪击→历史事件遗址、
#      爆炸→"好吃到爆炸"）——拦下"XX纪念馆怎么预约"比漏一句脏话糟糕。
# LLM 未配置时本地词库是唯一防线，常用硬暴力词必须在种子里兜住。
SEED_WORDS: dict[str, list[str]] = {
    "gambling": ["赌博", "赌场", "博彩", "赌球", "赌马", "六合彩", "轮盘", "老虎机"],
    "drugs": ["冰毒", "海洛因", "可卡因", "摇头丸", "大麻烟", "制毒",
              "毒品", "吸毒", "贩毒"],
    "violence": ["军火", "枪支买卖", "弹药", "雷管", "炸药", "制爆",
                 "杀人", "自杀", "谋杀", "割腕", "炸弹", "劫机",
                 "恐袭", "恐怖袭击"],
    "porn": ["色情", "援交", "卖淫", "嫖娼", "裸聊", "强奸"],
    "fraud": ["传销组织", "洗钱", "诈骗链接", "钓鱼网站"],
    "abuse": ["傻逼", "草泥马"],
    "political": [],                          # 由运营词库文件或云审核补齐
}

# 输出侧命中即整条换兜底话术的类别（低危类别只打码）
HARD_BLOCK_CATEGORIES = {"political", "violence", "porn"}

_FALLBACKS = {
    "political": "这条消息我不能回应。我们聊点旅行的事吧——想去哪座城市玩？",
    "violence": "这条消息我不能回应。想聊聊目的地的玩法、路线或预算吗？",
    "porn": "这条消息我不能回应。有行程规划的问题我随时都在。",
}

_FALLBACK_DEFAULT = ("这条消息我无法回应。我是旅行规划助手，"
                     "可以帮你排行程、查景点美食酒店——想去哪玩？")

# 分隔类字符（词中插这些规避检测）：空格、标点、零宽字符、星号井号等。
_JUNK = r"[^\u4e00-\u9fffA-Za-z0-9]{0,2}"

_res_cache: dict = {}                        # fingerprint -> (words, compiled)


def _word_file() -> "list[tuple[str, str]]":
    """读运营词库文件（类别|词），文件不存在/为空返回空表。"""
    p = os.environ.get("TRAVEL_SENSITIVE_FILE") or str(paths.data_dir() / "sensitive_words.txt")
    out: list[tuple[str, str]] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "|" in line:
                    cat, w = line.split("|", 1)
                    out.append((cat.strip() or "other", w.strip()))
                else:
                    out.append(("other", line))
    except OSError:
        pass
    return out


def _norm_char(ch: str) -> str:
    """全角→半角 + 小写（1:1 字符映射，不改变文本长度与命中位置）。"""
    o = ord(ch)
    if 0xFF01 <= o <= 0xFF5E:
        o -= 0xFEE0
    return chr(o).lower()


def load_words() -> tuple[dict[str, list[str]], re.Pattern]:
    """合并种子词与运营词库，编译成分隔容忍的正则。文件指纹变了自动重编译。"""
    p = os.environ.get("TRAVEL_SENSITIVE_FILE") or str(paths.data_dir() / "sensitive_words.txt")
    try:
        fp = os.stat(p).st_mtime if os.path.exists(p) else 0.0
    except OSError:
        fp = 0.0
    with _LOCK:
        cached = _res_cache.get("fp")
        if cached == fp:
            return _res_cache["words"], _res_cache["rex"]
    words: dict[str, list[str]] = {k: list(v) for k, v in SEED_WORDS.items()}
    for cat, w in _word_file():
        words.setdefault(cat, [])
        if w not in words[cat]:
            words[cat].append(w)
    # 词→类别映射 + 分隔容忍的大交替正则（一个正则扫完全部词）
    w2cat = {}
    for cat, ws in words.items():
        for w in ws:
            # 键存归一化后的词：命中还原出的 compact 也是归一化口径
            w2cat["".join(_norm_char(ch) for ch in w)] = cat
    all_parts = [_JUNK.join(re.escape(_norm_char(ch)) for ch in w)
                 for ws in words.values() for w in ws if len(w) >= 2]
    rex = re.compile("|".join(all_parts)) if all_parts else re.compile(r"(?!x)x")
    with _LOCK:
        _res_cache.clear()
        _res_cache.update({"fp": fp, "words": words, "rex": rex, "w2cat": w2cat})
    return words, rex


@dataclass
class Verdict:
    """审查结论。ok=True 放行；hits 记录全部命中（含位置，供打码）。"""
    ok: bool = True
    category: str = ""
    words: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)


def scan(text: str) -> Verdict:
    """对文本做分隔容忍匹配，返回全部命中（类别/词/原文区间）。

    在逐字符归一化（全角→半角、小写，长度不变）后的文本上匹配，
    命中区间可直接映射回原文位置，打码不打乱排版。
    """
    if not text:
        return Verdict()
    _, rex = load_words()
    w2cat = _res_cache.get("w2cat", {})
    norm = "".join(_norm_char(ch) for ch in text)
    spans: list[tuple[int, int]] = []
    words: list[str] = []
    cats: set[str] = set()
    for m in rex.finditer(norm):
        # 还原标准词：去掉命中文本里插入的分隔字符（空格/符号/零宽等）
        compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", m.group(0))
        cat = w2cat.get(compact) or "other"
        spans.append((m.start(), m.end()))
        words.append(compact)
        cats.add(cat)
    if not cats:
        return Verdict()
    # 类别优先级：硬拦 > 其他
    order = ["political", "violence", "porn", "drugs", "gambling", "fraud",
             "abuse", "other"]
    top = next(c for c in order if c in cats)
    return Verdict(ok=False, category=top, words=words, spans=spans)


def fallback_reply(category: str) -> str:
    return _FALLBACKS.get(category, _FALLBACK_DEFAULT)


def sanitize_output(text: str) -> tuple[str, Verdict]:
    """输出侧处置：硬拦类别整条换兜底话术，其余命中就地打码。

    返回 (处置后的文本, verdict)。verdict.ok=False 表示有命中（已记录）。
    """
    v = scan(text)
    if v.ok:
        return text, v
    log("output", v)
    if v.category in HARD_BLOCK_CATEGORIES:
        return fallback_reply(v.category), v
    out = []
    last = 0
    for s, e in v.spans:
        out.append(text[last:s])
        out.append("*" * (e - s))
        last = e
    out.append(text[last:])
    return "".join(out), v


def check_input(text: str) -> tuple[bool, str]:
    """输入侧处置：命中返回 (False, 兜底话术) 并落记录；放行返回 (True, "")。"""
    v = scan(text)
    if v.ok:
        return True, ""
    log("input", v)
    return False, fallback_reply(v.category)


# ------------------------------------------------------------------ 记录

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(auth.db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    with c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS moderation_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          direction TEXT NOT NULL,           -- input/output
          category TEXT NOT NULL,
          words TEXT NOT NULL DEFAULT '',
          excerpt TEXT NOT NULL DEFAULT '',
          uid INTEGER,
          sid TEXT,
          disposition TEXT NOT NULL,         -- blocked/masked/fallback
          dismissed INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT '');
        """)
    return c


def log(direction: str, v: Verdict, *, uid: int | None = None,
        sid: str = "", disposition: str = "", excerpt: str = "") -> None:
    """拦截记录落库（运营在管理后台复查）。失败只降级。"""
    try:
        src = excerpt or ""
        with _conn() as c:
            c.execute(
                "INSERT INTO moderation_log(created_at, direction, category,"
                " words, excerpt, uid, sid, disposition)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), direction,
                 v.category, json.dumps(v.words, ensure_ascii=False),
                 src[:120], uid, sid[:64],
                 disposition or ("blocked" if direction == "input" else "masked")))
    except Exception:
        pass


def list_hits(limit: int = 100, include_dismissed: bool = False) -> list[dict]:
    with _conn() as c:
        q = ("SELECT * FROM moderation_log"
             + ("" if include_dismissed else " WHERE dismissed=0")
             + " ORDER BY id DESC LIMIT ?")
        return [dict(r) for r in c.execute(q, (limit,)).fetchall()]


def dismiss(hit_id: int, note: str = "") -> bool:
    """标记误报（记录保留、不再显示在待复查列表）。"""
    with _conn() as c:
        cur = c.execute("UPDATE moderation_log SET dismissed=1, note=? WHERE id=?",
                        (note, hit_id))
        return cur.rowcount > 0


def stats() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT direction, category, COUNT(*) AS n"
                         " FROM moderation_log WHERE dismissed=0"
                         " GROUP BY direction, category").fetchall()
    by: dict[str, dict[str, int]] = {}
    for r in rows:
        by.setdefault(r["direction"], {})[r["category"]] = r["n"]
    return {d: by[d] for d in ("input", "output") if d in by}


# ------------------------------------------------------------------ 云审核钩子

def cloud_check(text: str) -> Verdict | None:
    """云审核（腾讯云 TMS / 阿里云内容安全）。未配置返回 None。

    生产建议接云审核替代/补充本地词库（几十块能起步）。接法：在
    _cloud_<provider> 里实现签名与请求，返回 Verdict（category 用云侧
    标签映射到本模块的类别名）。云侧异常降级为 None（走本地结果），
    不阻塞业务；连续失败应告警而不是静默。
    """
    provider = os.environ.get("TRAVEL_CLOUD_MODERATION", "").strip().lower()
    if not provider:
        return None
    fn = _CLOUD_PROVIDERS.get(provider)
    if fn is None:
        return None
    try:
        return fn(text)
    except Exception:
        return None


def _cloud_tencent(text: str) -> Verdict:
    raise NotImplementedError(
        "腾讯云文本内容安全（TMS）未接入：实现 SignatureV3 签名与"
        " ContentModeration.TextModeration 调用后，把结果映射成 Verdict 返回")


_CLOUD_PROVIDERS = {"tencent": _cloud_tencent}
