"""景点介绍卡片：hover 地图图钉显示图片与摘要，点击跳转介绍网页。

数据来源：必应国内版（cn.bing.com）搜索「<景点> 攻略」选介绍页：
- 链接优先选知乎、百度百科、马蜂窝、携程等介绍页，都没有就取第一条；
- 缩略图优先用介绍页结果里的 rms_img（th.bing.com 代理，境内可直连热链）；
- 没图的再单独搜图（**百度图片**，2026-11-12 起弃用必应图片搜索）——
  必应对冷门中文景点经常凑数（响堂山石窟艺术博物馆配动漫图，用户实测
  弃用）；百度对中文景点索引强得多，缩略图 img*.baidu.com 可直连热链
  （无 referer 校验）。冷门全名在百度也常 0 命中，按**变体阶梯**降级：
  全名 → 剥括号 → 去类型后缀（「响堂山石窟艺术博物馆」落到「响堂山
  石窟」就是实拍——博物馆在石窟景区里，视觉一致）；
- 介绍页宁缺毋滥没搜到的，也出「图片卡」（点击跳百度图片结果页），
  不让景点光秃秃没图。

工程约束：
- 零依赖：抓取与解析复用 websearch 的标准库实现；
- 永不抛异常、永不阻塞太久：单景点超时 6s，整批并发 4、总预算可控；
- 进程内缓存 24h（同一景点反复生成行程不出网）；
- POI_LINKS=0 环境变量可整体关闭（测试用）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
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

# 图片优先取自旅游网站（携程/去哪儿/马蜂窝/同程/大众点评/百科）：
# 这些站的景点图是「本尊实拍」，搜索引擎聚合成 RSS 头像/表情包的概率低。
# 百度图片按 fromURLHost 判断来源。
_IMG_SRC_PREF = ("you.ctrip.com", "ctrip.com", "piao.qunar.com", "qunar.com",
                 "mafengwo.cn", "tuniu.com", "goto.ly.com", "dianping.com",
                 "baike.baidu.com")

# ------------------------------------------------------------- 百度图片搜索
#
# 2026-11-12 弃用必应图片搜索（冷门景点凑杂图，用户实测）。百度 acjson
# 返回 JSON，thumbURL（img*.baidu.com）可直连热链；参数集是实测可用
# 组合——缺 ct/z/fp/fm 这几项会整个返回空（并不是无结果）。
_BAIDU_IMG_URL = "https://image.baidu.com/search/acjson"
_BAIDU_IMG_PAGE = "https://image.baidu.com/search/index"

# 冷门全名查不到时的变体阶梯：全名 → 剥括号 → 去类型后缀。
# 「响堂山石窟艺术博物馆」百度全名 0 命中，落到「响堂山石窟」就是实拍
# （博物馆在石窟景区里，视觉一致）。分店括注（x店）不剥——剥了会命中
# 别家分店的图。
_IMG_SUFFIXES = ("艺术博物馆", "博物馆", "博物院", "纪念馆", "陈列馆",
                 "美术馆", "风景区", "景区", "旅游区", "度假区",
                 "遗址公园", "森林公园", "公园", "古城", "古镇")
_IMG_BRANCH = re.compile(r"[（(][^）)]*店[）)]\s*$")


def _img_variants(name: str) -> list[str]:
    out = [name]
    if not _IMG_BRANCH.search(name):
        bare = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
        if bare and bare != name and len(bare) >= 3:
            out.append(bare)
    for base in list(out):
        for suf in _IMG_SUFFIXES:
            if base.endswith(suf) and len(base) - len(suf) >= 3:
                out.append(base[:-len(suf)])
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _fetch_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": websearch._UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://image.baidu.com/",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _parse_baidu_images(data: dict, stem: str) -> list[tuple[int, str]]:
    """从 acjson 结果挑候选：跳过动图，标题含核心词/来源是旅游站优先。"""
    cands: list[tuple[int, str]] = []
    for it in data.get("data") or []:
        thumb = str(it.get("thumbURL") or "")
        if not thumb or str(it.get("is_gif") or "") == "1":
            continue
        if re.search(r"\.(gif|svg)(\?|$)", thumb, re.I):
            continue
        title = re.sub(r"<[^>]+>", "", str(it.get("fromPageTitle") or ""))
        host = str(it.get("fromURLHost") or "").lower()
        weight = (1 if stem and stem in title else 0) + (
            1 if any(_site_is(host, h) for h in _IMG_SRC_PREF) else 0)
        cands.append((weight, thumb))
    return cands


def _baidu_image_search(name: str, timeout: int) -> str:
    """百度图片搜「<景点名>」，返回一张可热链缩略图。

    变体阶梯：全名查不到 → 剥括号 → 去类型后缀。标题含核心词的候选
    排前面（防凑数杂图），其次旅游站来源。失败返回空串（前端没图，
    不报错）。"""
    for cand in _img_variants(name):
        try:
            params = {"tn": "resultjson_com", "ipn": "rj", "word": cand,
                      "pn": "0", "rn": "8", "ie": "utf-8", "oe": "utf-8",
                      "ct": "201326592", "z": "9", "fp": "result",
                      "queryWord": cand, "cl": "2", "lm": "-1",
                      "st": "-1", "fm": "result"}
            data = _fetch_json(_BAIDU_IMG_URL + "?" +
                               urllib.parse.urlencode(params), timeout)
        except Exception:
            continue
        stem = re.sub(r"[（(][^）)]*[）)]", "", cand)[:3]
        cands = _parse_baidu_images(data, stem)
        if cands:
            cands.sort(key=lambda x: -x[0])
            return cands[0][1]
    return ""


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
        # 的事故），一律换百度图片补图（按名字变体阶梯降级）：标题含
        # 核心词的候选排前面，凑数杂图概率低。补图失败时保留块内图
        # （多数还是对的），总比没图强。
        if not info.get("img") or not _img_trusted(info.get("site") or ""):
            try:
                img2 = _baidu_image_search(name, timeout)
            except Exception:
                img2 = ""
            if img2:
                info["img"] = img2
    elif info is None:
        # 介绍页宁缺毋滥没搜到，但图片可以独立给：出「图片卡」，
        # 点击跳百度图片结果页（比没卡片直观）
        try:
            img = _baidu_image_search(name, timeout)
        except Exception:
            img = ""
        if img:
            info = {"title": name,
                    "url": _BAIDU_IMG_PAGE + "?" + urllib.parse.urlencode(
                        {"tn": "baiduimage", "ie": "utf-8", "word": name}),
                    "snippet": "", "site": "百度图片", "img": img}
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
