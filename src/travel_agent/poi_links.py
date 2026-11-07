"""景点介绍卡片：hover 地图图钉显示图片与摘要，点击跳转介绍网页。

数据来源：必应国内版（cn.bing.com）搜索「<景点> <城市> 景点介绍」：
- 链接优先选知乎、百度百科、马蜂窝、携程等介绍页，都没有就取第一条；
- 缩略图直接用必应结果里的 rms_img（th.bing.com 代理，境内可直连热链）。

工程约束：
- 零依赖：抓取与解析复用 websearch 的标准库实现；
- 永不抛异常、永不阻塞太久：单景点超时 6s，整批并发 4、总预算可控；
- 进程内缓存 24h（同一景点反复生成行程不出网）；
- POI_LINKS=0 环境变量可整体关闭（测试用）。
"""

from __future__ import annotations

import base64
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import websearch

_DISABLED = os.environ.get("POI_LINKS", "").strip().lower() in {"0", "off", "false", "no"}

_TTL = 86400          # 缓存 24h
_TIMEOUT = 6          # 单景点搜索超时（秒）
_MAX_ITEMS = 12       # 一份行程最多搜多少个景点（超出的是小众点，没有也不影响）
_WORKERS = 4          # 并发数（别把必应打疼）

_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()

# 介绍页来源优先级：社区攻略 > 百科 > OTA 景点页
_PREFERRED = ("zhihu.com", "baike.baidu.com", "mafengwo.cn", "you.ctrip.com",
              "you.ctrip", "piao.qunar.com", "goto.ly.com", "tuniu.com",
              "dianping.com", "sina.com.cn", "sohu.com", "163.com")

_BLOCK = websearch._BING_BLOCK
_URL = websearch._BING_URL
_SNIPPET = websearch._BING_SNIPPET
_IMG = re.compile(r'<img[^>]+class="[^"]*rms_img[^"]*"[^>]+src="([^"]+)"', re.S)


def enabled() -> bool:
    return not _DISABLED


def _parse_page(page: str, limit: int) -> list[dict]:
    """从必应结果页解析 介绍页候选：title/url/snippet/site/img。离线可测。"""
    out: list[dict] = []
    for block in _BLOCK.findall(page):
        mu = _URL.search(block)
        if not mu:
            continue
        link, title = mu.group(1), websearch._strip_tags(mu.group(2))
        ms = _SNIPPET.search(block)
        snippet = websearch._strip_tags(ms.group(1)) if ms else ""
        # 必应有时给跳转链接，剥一层包装（与 websearch._bing 同款）
        m = re.search(r"u=a1([^&]+)", link)
        if m:
            try:
                link = base64.urlsafe_b64decode(
                    m.group(1) + "=" * (-len(m.group(1)) % 4)).decode("utf-8", "replace")
            except Exception:
                pass
        mi = _IMG.search(block)
        img = mi.group(1) if mi else ""
        if img.startswith("//"):
            img = "https:" + img
        if not title or not link.startswith("http"):
            continue
        out.append({"title": title, "url": link, "snippet": snippet[:120],
                    "site": websearch.host_of(link), "img": img})
        if len(out) >= limit:
            break
    return out


def _pick(results: list[dict], name: str) -> dict | None:
    """从候选里挑介绍页。

    必须先按「标题或摘要里真的提到这个景点」过滤——否则「宽窄巷子」会
    被必应的「宽窄（四川中烟商标）」百科顶掉，张冠李戴比没有卡片严重得多。
    过滤后按来源优先级选；一条都不含景点名就宁缺毋滥返回 None。
    """
    if not results:
        return None
    named = [r for r in results
             if name in (r.get("title") or "") or name in (r.get("snippet") or "")]
    if not named:
        return None
    for host in _PREFERRED:
        for r in named:
            if host in (r.get("site") or ""):
                return r
    return named[0]


def _search_one(name: str, city: str, timeout: int) -> dict | None:
    # 「早市 / 伴手礼」这类通用节点名搜不出有效介绍，跳过。
    # 查询词不加引号：实测必应对带引号的长景点名会整个放弃匹配、退回城市通用页；
    # 误配（如「宽窄巷子」→香烟）由 _pick 的「结果必须含景点名」过滤兜住。
    if "/" in name or len(name) < 2:
        return None
    # 不带城市词：景点名一般自带地名（"成都大熊猫繁育研究基地"），
    # 补城市词反而把必应稀释成"成都通用页"。误配由 _pick 的名字过滤兜住。
    tries = (f"{name} 攻略", f"{name} 景点 门票")
    for q in tries:
        try:
            page = websearch._fetch("https://cn.bing.com/search?" +
                                    urllib.parse.urlencode(
                                        {"q": q, "mkt": "zh-CN", "count": "15"}),
                                    timeout)
            hit = _pick(_parse_page(page, 8), name)
        except Exception:
            hit = None
        if hit:
            return hit
    return None


def fetch_one(name: str, city: str, timeout: int = _TIMEOUT) -> dict | None:
    """查一个景点的介绍卡片；失败返回 None（前端就没卡片，不报错）。"""
    key = f"{city}|{name}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    info: dict | None = None
    try:
        info = _search_one(name, city, timeout)
    except Exception:
        info = None
    with _lock:
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:250]:
                _cache.pop(k, None)
        _cache[key] = (now, info)
    return info


def attach(result: dict, timeout: int = _TIMEOUT, budget: float = 8.0) -> None:
    """给行程里的主要景点并发补介绍卡片，写入 result["poi_links"][景点名]。

    - 只搜有坐标的 attraction（没坐标的点本来就不上图）；
    - 总预算 budget 秒，超时的景点放弃（前端无卡片）；
    - 任何异常都不外抛——这功能是锦上添花，不能拖垮规划主流程。
    """
    if _DISABLED or not isinstance(result, dict):
        return
    days = result.get("days_plan") or []
    if not days:
        return
    dest = str((result.get("slots") or {}).get("destination") or "").strip()
    links = result.setdefault("poi_links", {})
    names: list[str] = []
    seen: set[str] = set()
    for d in days:
        for n in d.get("nodes") or []:
            if n.get("type") != "attraction" or not n.get("geo"):
                continue
            nm = str(n.get("name") or "").strip()
            if not nm or nm in seen or nm in links:
                continue
            seen.add(nm)
            names.append(nm)
    todo = names[:_MAX_ITEMS]
    if not todo:
        return
    ex = ThreadPoolExecutor(max_workers=_WORKERS)
    try:
        futs = {ex.submit(fetch_one, n, dest): n for n in todo}
        for f in as_completed(futs, timeout=budget):
            try:
                info = f.result()
            except Exception:
                info = None
            if info and info.get("url"):
                links[futs[f]] = info
    except Exception:
        pass          # 超预算/线程异常：放弃剩余的，已完成的保留
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
