"""景点介绍卡片：hover 地图图钉显示图片与摘要，点击跳转介绍网页。

数据来源：必应国内版（cn.bing.com）搜索「<景点> 攻略」选介绍页：
- 链接优先选知乎、百度百科、马蜂窝、携程等介绍页，都没有就取第一条；
- 缩略图优先用介绍页结果里的 rms_img（th.bing.com 代理，境内可直连热链）；
- 没图的再单独搜必应图片「<景点> 风景」补一张，**图片来源优先旅游网站**
  （携程/去哪儿/马蜂窝/同程/点评/百科，按原图所在页 purl 判断）——
  旅游站的景点图是本尊实拍，杂图概率低；
- 介绍页宁缺毋滥没搜到的，也出「图片卡」（点击跳图片搜索结果页），
  不让景点光秃秃没图。

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

# 块内缩略图可信来源：百科/OTA 景点页的必应缩略图就是实体照片；
# 知乎/门户的块内图是「文章头图」，与景点经常无关（景山公园卡片
# 配动漫图的事故就是知乎文章头图）——这些来源的图必须换专用图片搜索
_TRUST_IMG = ("baike.baidu.com", "mafengwo.cn", "you.ctrip.com",
              "piao.qunar.com", "goto.ly.com", "tuniu.com", "dianping.com",
              "gov.cn")


def _site_is(site: str, pref: str) -> bool:
    """域名后缀严格匹配（不是子串）：pref 是 registrable 域名，
    site 必须等于它或是它的子域。子串匹配会把 xzhihu.com 之类仿冒域
    也算成知乎。"""
    s = (site or "").lower().split("/")[0].split(":")[0].strip(".")
    p = (pref or "").lower()
    return s == p or s.endswith("." + p)


def _img_trusted(site: str) -> bool:
    return any(_site_is(site, h) for h in _TRUST_IMG)

_BLOCK = websearch._BING_BLOCK
_URL = websearch._BING_URL
_SNIPPET = websearch._BING_SNIPPET
_IMG = re.compile(r'<img[^>]+class="[^"]*rms_img[^"]*"[^>]+src="([^"]+)"', re.S)

# 必应图片搜索：结果 JSON 藏在 m="{&quot;...&quot;}" 属性里，引号是 HTML 转义的
# turl 域名有两套：th.bing.com（国际版）/ ts*.mm.bing.net（国内版），都可热链
# purl = 原图所在网页，用来判断图片来源是不是旅游网站
_IMG_PAGE_M = re.compile(r' m="([^"]+)"', re.S)
_IMG_MURL = re.compile(r'"murl":"(https?:[^"]+)"')
_IMG_TURL = re.compile(r'"turl":"(https?://(?:th\.bing\.com|ts\d*\.mm\.bing\.net)[^"]+)"')
_IMG_PURL = re.compile(r'"purl":"(https?:[^"]+)"')

# 图片优先取自旅游网站（携程/去哪儿/马蜂窝/同程/大众点评/百科）：
# 这些站的景点图是「本尊实拍」，搜索引擎聚合成 RSS 头像/表情包的概率低。
# 图本身仍走必应 turl 代理热链（原图防盗链），purl 只用来挑来源。
_IMG_SRC_PREF = ("you.ctrip.com", "ctrip.com", "piao.qunar.com", "qunar.com",
                 "mafengwo.cn", "tuniu.com", "goto.ly.com", "dianping.com",
                 "baike.baidu.com")


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
            if _site_is(r.get("site") or "", host):
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


def _parse_image_page(page: str) -> str:
    """从必应图片搜索结果页解析一张可热链的缩略图 URL；离线可测。

    - 优先 turl（th.bing.com 代理，境内直连、无防盗链），murl 原图多有
      referer 防护，热链常 403，只用来兜底确认存在；
    - **来源优先旅游网站**（携程/去哪儿/马蜂窝/同程/点评/百科，见
      _IMG_SRC_PREF）：按 purl（原图所在网页）判断，先遍历完候选挑旅游站
      的图，一条都没有才退回任意来源——用户明确要求景点图出自旅游网站；
    - 把缩略图尺寸参数放大到 w=480（卡片背景 cover，480 够清晰）；
    - 只要 jpg/png/webp，跳过 svg/gif 动图。
    """
    cands: list[tuple[bool, str]] = []          # (是否旅游站来源, turl)
    for m in _IMG_PAGE_M.finditer(page):
        data = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
        mt = _IMG_TURL.search(data)
        if not mt:
            continue
        turl = mt.group(1)
        mm = _IMG_MURL.search(data)
        if mm and re.search(r"\.(svg|gif)(\?|$)", mm.group(1), re.I):
            continue                      # 跳过动图/矢量图
        mp = _IMG_PURL.search(data)
        host = ""
        if mp:
            purl = mp.group(1).replace("\\/", "/")
            host = websearch.host_of(urllib.parse.unquote(purl))
        from_travel = any(_site_is(host, h) for h in _IMG_SRC_PREF)
        # 统一放大到卡片够用的尺寸（无参数的补上，有参数的改写）
        if "?" not in turl:
            turl += "?w=480&h=320&c=7"
        else:
            turl = re.sub(r"([?&])w=\d+", r"\1w=480", turl)
            turl = re.sub(r"([?&])h=\d+", r"\1h=320", turl)
            if "w=" not in turl:
                turl += "&w=480&h=320&c=7"
        cands.append((from_travel, turl))
    for from_travel, turl in cands:           # 旅游站来源优先
        if from_travel:
            return turl
    return cands[0][1] if cands else ""


def _search_image(name: str, timeout: int) -> str:
    """必应图片搜索「<景点> 风景」，返回一张可热链缩略图；失败返回空串。"""
    try:
        page = websearch._fetch("https://cn.bing.com/images/search?" +
                                urllib.parse.urlencode(
                                    {"q": f"{name} 风景", "mkt": "zh-CN", "first": "1"}),
                                timeout)
        return _parse_image_page(page)
    except Exception:
        return ""


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
    if info is not None:
        # 图片来源把关：无图、或来源不属于「块内缩略图可信」名单（知乎/
        # 门户的块内图是文章头图，经常与景点无关——景山公园卡片配动漫图
        # 的事故），一律换专用图片搜索「<景点> 风景」：图片搜索按查询
        # 相关性排序，出来的就是景点本尊。专用搜索失败时保留块内图
        # （多数还是对的），总比没图强。
        if not info.get("img") or not _img_trusted(info.get("site") or ""):
            try:
                img2 = _search_image(name, timeout)
            except Exception:
                img2 = ""
            if img2:
                info["img"] = img2
    elif info is None:
        # 介绍页宁缺毋滥没搜到，但图片可以独立给：出「图片卡」，
        # 点击跳必应图片搜索结果页（比没卡片直观）
        try:
            img = _search_image(name, timeout)
        except Exception:
            img = ""
        if img:
            info = {"title": name,
                    "url": "https://cn.bing.com/images/search?" +
                           urllib.parse.urlencode({"q": f"{name} 风景", "mkt": "zh-CN"}),
                    "snippet": "", "site": "必应图片", "img": img}
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
