"""零依赖联网检索：给"库里没有、规则也答不了"的通用问题兜底。

设计原则：
- 只用标准库 urllib，不引入任何第三方依赖（与 server 零依赖一致）。
- 首选必应国内版（cn.bing.com，境内直连可用），失败回落 DuckDuckGo HTML 版。
- 只抓标题 + 摘要 + 链接，不抓正文——摘要足够让模型组织答案，
  也避免在会话线程里做重量级抓取。
- 结果带来源站点，说话层（llm.compose_reply kind="general"）会要求注明来源；
  没配模型时直接渲染模板列表，同样带来源，宁可糙也不编。
- 进程内缓存 30 分钟，同一问题反复问不重复出网。
"""

from __future__ import annotations

import html as _html
import re
import time
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TIMEOUT = 8          # 单引擎超时（秒）
_CACHE_TTL = 1800     # 缓存 30 分钟
_CACHE_MAX = 200
_cache: dict[str, tuple[float, dict]] = {}


def host_of(url: str) -> str:
    """取展示用站点名：https://www.mafengwo.cn/x -> mafengwo.cn"""
    m = re.match(r"https?://(?:www\.)?([^/?:#]+)", url or "")
    return m.group(1) if m else ""


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return _html.unescape(re.sub(r"\s+", " ", s)).strip()


def _fetch(url: str, timeout: int = _TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 不要 gzip：标准库不自动解压，拿到压缩流会解析失败
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    charset = "utf-8"
    try:
        charset = resp.headers.get_content_charset() or "utf-8"
    except Exception:
        pass
    return raw.decode(charset, errors="replace")


# ---------------------------------------------------------------- 必应


_BING_BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_URL = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


def _bing(query: str, limit: int, timeout: int) -> list[dict]:
    url = ("https://cn.bing.com/search?" +
           urllib.parse.urlencode({"q": query, "mkt": "zh-CN", "count": "15"}))
    page = _fetch(url, timeout)
    out: list[dict] = []
    for block in _BING_BLOCK.findall(page):
        mu = _BING_URL.search(block)
        if not mu:
            continue
        link, title = mu.group(1), _strip_tags(mu.group(2))
        ms = _BING_SNIPPET.search(block)
        snippet = _strip_tags(ms.group(1)) if ms else ""
        # 必应有时给跳转链接，剥一层包装
        m = re.search(r"u=a1([^&]+)", link)
        if m:
            import base64
            try:
                link = base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode("utf-8", "replace")
            except Exception:
                pass
        if not title or not snippet:
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:300],
                    "site": host_of(link)})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- DuckDuckGo


_DDG_BLOCK = re.compile(r'<div class="result[^"]*results_links.*?(?=<div class="result|</body>)', re.S)
_DDG_URL = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_DDG_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _ddg(query: str, limit: int, timeout: int) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    page = _fetch(url, timeout)
    out: list[dict] = []
    for block in _DDG_BLOCK.findall(page):
        mu = _DDG_URL.search(block)
        if not mu:
            continue
        link, title = mu.group(1), _strip_tags(mu.group(2))
        m = re.search(r"uddg=([^&]+)", link)
        if m:
            link = urllib.parse.unquote(m.group(1))
        ms = _DDG_SNIPPET.search(block)
        snippet = _strip_tags(ms.group(1)) if ms else ""
        if not title or not snippet:
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:300],
                    "site": host_of(link)})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 对外入口


def _clean_query(q: str) -> str:
    """搜索词清洗：去掉问号、句尾语气词，不然搜索引擎会拿整句问话去匹配。"""
    s = re.sub(r"[？?。！!，,。～~\s]+$", "", (q or "").strip())
    changed = True
    while changed and len(s) > 4:
        changed = False
        for w in ("吗", "呢", "吧", "啊", "么"):
            if s.endswith(w):
                s, changed = s[:-1], True
    return s


# 搜索引擎只认关键词：整句自然语言会被稀释成前两个字（实测
# 「中国游客去日本旅游需要签证吗」返回的全是"中国"的百科页）。
# 这里把虚词挖掉、留实词，再用空格拼起来当搜索词。
_STOPWORDS = (
    "是不是", "能不能", "要不要", "需不需要", "可不可以", "有没有", "怎么样",
    "怎么办", "怎么", "需要", "应该", "请问", "帮帮忙", "帮忙", "帮忙",
    "多少", "什么", "哪个", "哪些", "哪里", "为什么", "还是", "或者",
    "我", "你", "他", "她", "它", "我们", "你们",
    "想", "打算", "准备", "计划", "请问", "咨询", "了解", "告诉",
    "去", "到", "在", "从", "的", "了", "是", "有", "能", "可以", "要",
    "吗", "呢", "吧", "啊", "嘛", "么", "呀", "哦",
)


def keywords(q: str) -> str:
    """无模型时的关键词兜底提取：挖掉虚词、留实词。"""
    s = _clean_query(q)
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    parts = [p for p in s.split() if len(p) >= 2]
    return " ".join(parts[:3]) if parts else _clean_query(q)


def search(query: str, limit: int = 5, timeout: int = _TIMEOUT) -> dict:
    """联网搜一个问题。返回 {"ok", "results", "note"}；失败时 ok=False、results 为空。

    永不抛异常——联网失败只是降级，调用方有自己的兜底话术。
    """
    q = keywords(query)
    if len(q) < 2:
        return {"ok": False, "results": [], "note": "问题太短，没法检索"}
    now = time.time()
    key = f"{q}|{limit}"
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        cached = hit[1]
        return {"ok": True, "results": list(cached["results"]), "note": "（缓存）联网检索完成"}
    if len(_cache) > _CACHE_MAX:
        # 简单清理：丢掉最旧的一半
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:_CACHE_MAX // 2]:
            _cache.pop(k, None)
    errors: list[str] = []
    for name, fn in (("bing", _bing), ("ddg", _ddg)):
        try:
            results = fn(q, limit, timeout)
        except Exception as e:                       # 网络层任何失败都降级
            errors.append(f"{name}: {type(e).__name__}")
            continue
        if results:
            res = {"ok": True, "results": results,
                   "note": f"联网检索完成（{name}）"}
            _cache[key] = (now, res)
            return {"ok": True, "results": list(results), "note": res["note"]}
        errors.append(f"{name}: 无结果")
    return {"ok": False, "results": [],
            "note": "联网检索失败（" + "；".join(errors) + "）"}
