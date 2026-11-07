"""把行程结果渲染成单文件 HTML（无外部依赖，可直接打印）。

用法：
    python render_html.py --utterance "..." -o ../output/大理行程.html
    python render_html.py                       # 用内置默认用例

设计取舍：
- 路线图：配置了天地图 Key（TIANDITU_KEY 环境变量或设置文件 tdt_key）时，
  用国家地理信息公共服务平台（天地图）JS API 4.0 渲染真实底图；SDK 加载
  失败或没配 Key 时回落到本地 SVG 示意图，页面永远有内容。
- 坐标系：天地图底图是 CGCS2000（与 WGS-84 差异在厘米级），而库里的点
  来自 OSM/Open-Meteo（WGS-84），所以直接上图、不做任何纠偏。反过来说，
  若像接 GCJ-02 底图那样做「火星坐标」转换，点位会整体偏出约 500 米。
- 位置估算的点（geo_uncertain）在地图上用灰色「?」标注，绝不参与连线——
  估算点连出来的线是假的。
- 预算用横向堆叠条而非饼图：金额差异大时，条状比较更准。
- 全部内联样式与 SVG 兜底，单文件双击可开，也方便直接打印成 PDF 带走。
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .agent import extract_slots, find_missing, plan_trip, self_check
from .tools import haversine_km

sys.stdout.reconfigure(encoding="utf-8")

DAY_COLORS = ["#2F6F6B", "#B4693F", "#4A5C8A", "#8A6A2F", "#6B4A7A"]
BUDGET_COLORS = {
    "transport": "#2F6F6B",
    "hotel": "#B4693F",
    "ticket": "#4A5C8A",
    "food": "#8A6A2F",
    "local_transit": "#6B7A6B",
}
BUDGET_LABELS = {
    "transport": "交通", "hotel": "住宿", "ticket": "门票",
    "food": "餐饮", "local_transit": "市内", "other": "其他",
}


def esc(s) -> str:
    return html.escape(str(s))


def project(points: list[list[float]], w: int, h: int, pad: int = 28):
    """经纬度 → 画布坐标，带纬度校正与边界扩展。"""
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    span_lat = max(max_lat - min_lat, 0.02)
    span_lng = max(max_lng - min_lng, 0.02)
    min_lat -= span_lat * 0.15
    max_lat += span_lat * 0.15
    min_lng -= span_lng * 0.15
    max_lng += span_lng * 0.15
    k = 1.0 / max(0.2, math.cos(math.radians((min_lat + max_lat) / 2)))

    def pt(geo):
        x = (geo[1] - min_lng) / (max_lng - min_lng)
        y = (max_lat - geo[0]) / (max_lat - min_lat)
        return pad + x * (w - 2 * pad) * min(1.0, k), pad + y * (h - 2 * pad)

    return pt


def render_map(result: dict, w: int = 660, h: int = 380) -> str:
    days = result["days_plan"]
    home = (result["hotel"].get("items") or [{}])[0].get("geo")
    pts = [n["geo"] for d in days for n in d["nodes"] if n.get("geo")]
    if home:
        pts.append(home)
    if not pts:
        return ""
    pt = project(pts, w, h)

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img">',
             '<title>行程路线示意</title>']
    # 极淡网格，仅作视觉参照，不含任何地理信息
    for gx in range(0, w, 44):
        parts.append(f'<line x1="{gx}" y1="0" x2="{gx}" y2="{h}" stroke="#EEEDE8" stroke-width="1"/>')
    for gy in range(0, h, 44):
        parts.append(f'<line x1="0" y1="{gy}" x2="{w}" y2="{gy}" stroke="#EEEDE8" stroke-width="1"/>')

    if home:
        hx, hy = pt(home)
        parts.append(f'<rect x="{hx-5:.1f}" y="{hy-5:.1f}" width="10" height="10" rx="2" fill="#444441"/>')
        parts.append(f'<text x="{hx+10:.1f}" y="{hy+4:.1f}" font-size="11" fill="#6B6A63">住宿</text>')

    for i, d in enumerate(days):
        color = DAY_COLORS[i % len(DAY_COLORS)]
        geos = [n["geo"] for n in d["nodes"] if n.get("geo")]
        if not geos:
            continue
        coords = [pt(g) for g in geos]
        # 坐标不可信的点位不进连线：估算点画出来的线段是假的，
        # 只画确定坐标之间的段，虚线圆圈单独示意。
        certain = [(n, c) for n, c in zip([x for x in d["nodes"] if x.get("geo")], coords)
                   if not n.get("geo_uncertain")]
        if len(certain) > 1:
            path = " ".join(f"{c[1][0]:.1f},{c[1][1]:.1f}" for c in certain)
            parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                         f'stroke-width="2" stroke-opacity="0.55" stroke-dasharray="6 4"/>')
        seq = 0
        placed: list[tuple[float, float]] = []
        for n, (x, y) in zip([x for x in d["nodes"] if x.get("geo")], coords):
            if n["type"] != "attraction":
                continue
            if n.get("geo_uncertain"):
                # 位置估算：虚线空心圈 + ？，明确"这个点的坐标不可信"
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="none" '
                             f'stroke="{color}" stroke-width="2" stroke-dasharray="3 3"/>')
                parts.append(f'<text x="{x:.1f}" y="{y+3.5:.1f}" font-size="10" fill="{color}" '
                             f'text-anchor="middle">?</text>')
                if all(abs(x - px) > 62 or abs(y - py) > 15 for px, py in placed):
                    placed.append((x, y))
                    parts.append(f'<text x="{x:.1f}" y="{y+21:.1f}" font-size="10" fill="#B4693F" '
                                 f'text-anchor="middle">{esc(n["name"][:10])}·位置估算</text>')
                continue
            seq += 1
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{color}"/>')
            parts.append(f'<text x="{x:.1f}" y="{y+3.5:.1f}" font-size="10" fill="#fff" '
                         f'text-anchor="middle">{seq}</text>')
            # 标签避让：与已放置标签过近则省略，避免文字糊成一团
            if all(abs(x - px) > 62 or abs(y - py) > 15 for px, py in placed):
                placed.append((x, y))
                parts.append(f'<text x="{x:.1f}" y="{y+21:.1f}" font-size="10" fill="#444441" '
                             f'text-anchor="middle">{esc(n["name"][:10])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def get_tdt_key() -> str:
    """天地图 Key（tk）：环境变量 TIANDITU_KEY 优先，其次设置文件的 tdt_key。"""
    k = (os.environ.get("TIANDITU_KEY") or "").strip()
    if k:
        return k
    try:
        from . import llm
        return str(llm.load_settings().get("tdt_key") or "").strip()
    except Exception:
        return ""


def route_map_data(result: dict) -> dict:
    """抽给前端地图的最小数据集：只传坐标点位与连线所需字段。

    景点带 poi_links 里的介绍卡片（搜索来的介绍页+缩略图），
    url/snippet 在这里统一做 HTML 属性转义，前端直接拼 innerHTML。
    """
    links = result.get("poi_links") or {}
    days = []
    for d in result["days_plan"]:
        nodes = []
        for n in d["nodes"]:
            if not n.get("geo"):
                continue
            node = {"name": esc(n["name"]), "time": n["time"],
                    "lat": n["geo"][0], "lng": n["geo"][1],
                    "type": n["type"],
                    "uncertain": bool(n.get("geo_uncertain"))}
            if n["type"] == "attraction":
                info = links.get(n["name"])
                if info and info.get("url"):
                    node["link"] = {
                        "url": esc(str(info["url"])),
                        "site": esc(str(info.get("site") or "")),
                        "snippet": esc(str(info.get("snippet") or "")),
                        "img": esc(str(info.get("img") or "")),
                    }
            nodes.append(node)
        days.append({"day": d["day"], "nodes": nodes})
    home = (result["hotel"].get("items") or [{}])[0].get("geo")
    return {"days": days, "home": home, "colors": DAY_COLORS}


# 天地图 JS API 4.0（国家地理信息公共服务平台，合规底图）。
# 纯字符串模板，不经 f-string 求值，便于对源码直接做 JS 语法检查。
# __DATA__ / __KEY__ 在渲染时替换。
ROUTE_MAP_JS = """
(function () {
  var DATA = __DATA__;
  var TK = "__KEY__";
  var box = document.getElementById('route-map');
  var fb = document.getElementById('route-fallback');
  var tabs = document.getElementById('rt-tabs');
  var note = document.getElementById('rt-route-note');
  var locBtn = document.getElementById('rt-locate');
  var locTip = document.getElementById('rt-locate-tip');
  function showFallback() {
    if (fb) fb.style.display = '';
    if (box) box.style.display = 'none';
    if (tabs) tabs.style.display = 'none';
    if (locBtn) locBtn.style.display = 'none';
    if (locTip) locTip.style.display = 'none';
  }
  // 坐标系：天地图底图是 CGCS2000（与 WGS-84 差厘米级），库里的点本来就是
  // WGS-84，所以直接上图即可，不做 GCJ-02 纠偏——纠了反而整体偏约 500 米。
  // 「圆章式」图钉（效仿行程类 App）：上半是序号/标识，下半是类型小图标。
  // 图标全部内联 SVG 白色线条，不引用任何外部图片资源。
  function glyph(kind) {
    var s = 'stroke="#FFFFFF" fill="none" stroke-linecap="round" stroke-linejoin="round"';
    switch (kind) {
      case 'sight':        // 地标建筑（景点）
        return '<rect x="4.6" y="3.2" width="2.8" height="7.3" fill="#FFFFFF"/>'
          + '<path d="M6 0.6v2.6M2.6 10.6h6.8" ' + s + ' stroke-width="1.2"/>';
      case 'restaurant':   // 刀叉（餐饮）
        return '<path d="M3.4 1v2.9M5 1v2.9M6.6 1v2.9M5 4.6v6.2" '
          + s + ' stroke-width="1.1"/>'
          + '<path d="M8.9 1c-1.1 1.6-1.1 4.3 0 5.5v4.3" ' + s + ' stroke-width="1.1"/>';
      case 'transit':      // 车站/列车
        return '<rect x="3.2" y="1.6" width="5.6" height="7" rx="1.5" '
          + s + ' stroke-width="1.2"/>'
          + '<path d="M3.2 5.1h5.6M4.7 8.6l-1.3 2.4M7.3 8.6l1.3 2.4" '
          + s + ' stroke-width="1.1"/>';
      case 'hotel':        // 床（住宿）
        return '<path d="M1.6 10.8V5.2M1.6 8.5h8.8v2.3M10.4 8.5V7c0-.9-.7-1.6-1.6-1.6H5.2" '
          + s + ' stroke-width="1.2"/>'
          + '<circle cx="3.6" cy="6.6" r="1.15" fill="#FFFFFF"/>';
      case 'rest':         // 月牙（午休/自由活动）
        return '<path d="M7.6 1.4a4.7 4.7 0 1 0 3 7.2A5 5 0 0 1 7.6 1.4z" fill="#FFFFFF"/>';
      default:
        return '';
    }
  }
  function pin(color, kind, txt) {
    var inner;
    if (txt) {
      // 上半序号 + 下半小图标
      inner = '<text x="15" y="12.2" font-size="10.5" font-family="Arial,sans-serif"'
        + ' font-weight="bold" fill="#FFFFFF" text-anchor="middle">' + txt + '</text>'
        + '<g transform="translate(9.5,13.8) scale(0.72)" opacity="0.95">'
        + glyph(kind) + '</g>';
    } else {
      inner = '<g transform="translate(9.3,7.6) scale(0.95)" opacity="0.95">'
        + glyph(kind) + '</g>';
    }
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="40" viewBox="0 0 30 40">'
      + '<path d="M10 23.5L15 38.5l5-15z" fill="' + color + '"/>'
      + '<circle cx="15" cy="13.5" r="12.2" fill="' + color + '" stroke="#FFFFFF" stroke-width="1.8"/>'
      + inner + '</svg>';
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }
  // 「我的位置」蓝点：内联 SVG，不引外部图
  function dot() {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
      + '<circle cx="10" cy="10" r="7" fill="#1A73E8" stroke="#FFFFFF" stroke-width="2.6"/></svg>';
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }
  var map, all = [], dayRecs = [], homeOv = null, curTab = -1;
  var locMarker = null, locCircle = null;
  function ll(lng, lat) {
    var p = new T.LngLat(lng, lat);
    all.push(p);
    return p;
  }
  function addMarker(pos, color, kind, txt, infoHtml, uncertain) {
    var mk;
    try {
      var icon = new T.Icon({ iconUrl: pin(color, kind, txt),
                              iconSize: new T.Point(30, 40),
                              iconAnchor: new T.Point(15, 38) });
      mk = new T.Marker(pos, { icon: icon });
    } catch (e) { mk = new T.Marker(pos); }
    map.addOverLay(mk);
    try {
      var iw = new T.InfoWindow(infoHtml, { offset: new T.Point(0, -38),
                                            maxWidth: 250 });
      mk.addEventListener('mouseover', function () { mk.openInfoWindow(iw); });
      if (uncertain) {
        // 只有估算点用 click 打开（它没有 hover 卡片，保留点按习惯）
        mk.addEventListener('click', function () { mk.openInfoWindow(iw); });
      }
    } catch (e) {}
    return mk;
  }
  // 景点介绍卡片（效仿行程类 App 的 hover 大卡）：图 + 名 + 来源 + 摘要 + 跳转
  function poiCard(n) {
    var L = n.link || {};
    // CSS url() 不带引号，危险字符直接剥掉（缩略图 URL 本来也不含这些）
    var imgCss = String(L.img || '').replace(/[()\\s'"]/g, '');
    var img = imgCss
      ? '<div class="rt-card-img" style="background-image:url(' + imgCss + ');"></div>'
      : '<div class="rt-card-img rt-card-noimg">' + String(n.name).slice(0, 1) + '</div>';
    return '<div class="rt-card">' + img
      + '<div class="rt-card-body"><div class="rt-card-name">' + n.name + '</div>'
      + (L.site ? '<div class="rt-card-src">介绍来源：' + L.site
        + ' · 点击下方链接查看完整攻略</div>' : '')
      + (L.snippet ? '<div class="rt-card-snip">' + L.snippet + '</div>' : '')
      + '</div>'
      + (L.url ? '<a class="rt-card-link" href="' + L.url
        + '" target="_blank" rel="noopener">查看介绍 ↗</a>' : '')
      + '</div>';
  }
  function addLabel(text, pos, dx) {
    try {
      var lb = new T.Label({ text: text, position: pos,
                             offset: new T.Point(dx, 12) });
      map.addOverLay(lb);
      return lb;
    } catch (e) { return null; }
  }
  // 线上的「第N天」徽标（效仿行程类 App：色块圆角标签骑在动线上）
  // 白字要压得住底色：每日色直接铺底时（如第4天的暗金）白字会发飘，
  // 徽标底色统一加深一档，再给字加投影
  function darken(hex, f) {
    var m = /^#?([0-9a-fA-F]{6})$/.exec(hex || '');
    if (!m) { return hex; }
    var n = parseInt(m[1], 16);
    return 'rgb(' + Math.round(((n >> 16) & 255) * f) + ','
      + Math.round(((n >> 8) & 255) * f) + ','
      + Math.round((n & 255) * f) + ')';
  }
  function addBadge(text, pos, color) {
    try {
      var lb = new T.Label({
        text: '<span class="rt-badge" style="background:' + darken(color, 0.68)
          + ';">' + text + '</span>',
        position: pos, offset: new T.Point(-26, -14) });
      map.addOverLay(lb);
      return lb;
    } catch (e) { return null; }
  }
  function visible(o, on) {
    if (!o) return;
    try { if (on) { o.show(); } else { o.hide(); } } catch (e) {}
  }
  function setTab(idx) {
    curTab = idx;
    var btns = tabs ? tabs.querySelectorAll('button[data-idx]') : [];
    dayRecs.forEach(function (rec, i) {
      var on = (idx < 0) || (i === idx);
      rec.ov.forEach(function (o) { visible(o, on); });
    });
    visible(homeOv, idx < 0);
    try {
      var pts = (idx < 0) ? all : dayRecs[idx].pts;
      if (pts && pts.length > 1) { map.setViewport(pts); }
      else if (pts && pts.length === 1) { map.centerAndZoom(pts[0], 14); }
    } catch (e) {}
    for (var k = 0; k < btns.length; k++) {
      btns[k].className =
        (btns[k].getAttribute('data-idx') === String(idx)) ? 'on' : '';
    }
  }
  function bindTabs() {
    if (!tabs) return;
    var btns = tabs.querySelectorAll('button[data-idx]');
    for (var k = 0; k < btns.length; k++) {
      (function (b) {
        b.addEventListener('click', function () {
          setTab(parseInt(b.getAttribute('data-idx'), 10));
        });
      })(btns[k]);
    }
  }

  // ---------- 沿道路画路径（天地图路径规划服务） ----------
  // T.DrivingRoute 正常是「传 map 就自动画」，但我们自己按天配色画线，
  // 所以先用不挂地图（或 4x4 隐藏地图）的实例只取点位，再自己画 Polyline。
  var router = null, routerMode = null, ghostMap = null;
  function ghostMapOf() {
    if (ghostMap) return ghostMap;
    var d = document.createElement('div');
    d.id = 'rt-ghost-map';
    d.style.cssText = 'position:absolute;left:-9999px;top:0;width:4px;height:4px;overflow:hidden;';
    document.body.appendChild(d);
    try { ghostMap = new T.Map('rt-ghost-map'); } catch (e) { ghostMap = null; }
    return ghostMap;
  }
  function routerOf(mode) {
    if (router && routerMode === mode) return router;
    router = null;
    try {
      if (mode === 'bare') {
        router = new T.DrivingRoute(null, { policy: 0 });
      } else {
        var gm = ghostMapOf();
        if (!gm) return null;
        router = new T.DrivingRoute(gm, { policy: 0 });
      }
    } catch (e) { router = null; }
    routerMode = router ? mode : null;
    return router;
  }
  // 从规划结果里取沿道路的点串（不同小版本字段有差异，三条路都试一遍）
  function extractPath(res) {
    var pts = [], k;
    try {
      var path = res.getPlan(0).getPath() || [];
      for (k = 0; k < path.length; k++) { if (path[k]) pts.push(path[k]); }
    } catch (e) {}
    if (pts.length < 2) {
      pts = [];
      try {
        var raw = res.result && res.result.result && res.result.result.routelatlon;
        var arr = raw ? String(raw).split(';') : [];
        var swap = false;
        if (arr.length) {
          var f = arr[0].split(',');
          if (f.length === 2 && Math.abs(parseFloat(f[0])) > 90) { swap = true; }
        }
        for (k = 0; k < arr.length; k++) {
          var xy = arr[k].split(',');
          if (xy.length !== 2) { continue; }
          var a = parseFloat(xy[0]), b = parseFloat(xy[1]);
          if (isNaN(a) || isNaN(b)) { continue; }
          pts.push(swap ? new T.LngLat(b, a) : new T.LngLat(a, b));
        }
      } catch (e) {}
    }
    return pts.length > 1 ? pts : null;
  }
  function thin(pts, maxPts) {
    if (pts.length <= maxPts) return pts;
    var step = Math.ceil(pts.length / maxPts), out = [], i;
    for (i = 0; i < pts.length; i += step) { out.push(pts[i]); }
    if (out[out.length - 1] !== pts[pts.length - 1]) { out.push(pts[pts.length - 1]); }
    return out;
  }
  function planSeg(a, b, cb) {
    var finished = false, tried = {}, seq = 0, timers = [];
    function done(path) {
      if (finished) return;
      finished = true;
      for (var i = 0; i < timers.length; i++) { clearTimeout(timers[i]); }
      cb(path);
    }
    // 先试「不挂地图」的实例；拿不到点位就换 4x4 隐藏地图实例再试一次
    function attempt(mode) {
      if (finished || tried[mode]) return;
      tried[mode] = true;
      var r = routerOf(mode);
      if (!r) return;
      var token = ++seq;
      timers.push(setTimeout(function () {
        if (finished || token !== seq) return;
        if (mode !== 'ghost' && !tried['ghost']) { attempt('ghost'); return; }
        done(null);
      }, 7000));
      try {
        if (r.setSearchCompleteCallback) {
          r.setSearchCompleteCallback(function (res) {
            if (finished || token !== seq) return;
            var p = extractPath(res);
            if (!p && mode !== 'ghost' && !tried['ghost']) { attempt('ghost'); return; }
            done(p);
          });
        }
        r.search(a, b);
      } catch (e) {
        if (mode !== 'ghost' && !tried['ghost']) { attempt('ghost'); } else { done(null); }
      }
    }
    attempt('bare');
  }
  // 全局串行队列：天地图路径服务有 QPS 限制，同城多段一次打出去容易被限流
  var jobQueue = [], jobRunning = false;
  function nextJob() {
    if (!jobQueue.length) { jobRunning = false; return; }
    var fn = jobQueue.shift();
    fn(function () { setTimeout(nextJob, 150); });
  }
  function enqueue(fn) {
    jobQueue.push(fn);
    if (!jobRunning) { jobRunning = true; nextJob(); }
  }
  // 一天的相邻点位逐段规划，失败段自动退回直线
  function planDay(rec, cb) {
    var chain = rec.chain, idx = 0, segs = [], okCount = 0;
    function schedule() {
      if (idx >= chain.length - 1) { cb(segs, okCount); return; }
      var a = chain[idx], b = chain[idx + 1];
      enqueue(function (release) {
        planSeg(a, b, function (path) {
          if (path) { segs.push(path); okCount += 1; }
          else { segs.push([a, b]); }
          idx += 1;
          release();
          schedule();
        });
      });
    }
    schedule();
  }
  function flatten(segs) {
    var out = [], i, k;
    for (i = 0; i < segs.length; i++) {
      for (k = 0; k < segs[i].length; k++) {
        var p = segs[i][k], last = out[out.length - 1];
        if (last && last.lng === p.lng && last.lat === p.lat) { continue; }
        out.push(p);
      }
    }
    return out;
  }
  function midOf(pts) { return pts[Math.floor(pts.length / 2)] || pts[0]; }
  function replaceGhost(rec, pts) {
    var real = null;
    try {
      real = new T.Polyline(thin(pts, 320), { color: rec.color, weight: 5, opacity: 0.85 });
      map.addOverLay(real);
    } catch (e) { return; }
    rec.ov = rec.ov.filter(function (o) { return o !== rec.ghost; });
    try { map.removeOverLay(rec.ghost); } catch (e) {}
    rec.ghost = null;
    rec.ov.push(real);
    if (rec.badge) {
      rec.ov = rec.ov.filter(function (o) { return o !== rec.badge; });
      try { map.removeOverLay(rec.badge); } catch (e) {}
    }
    rec.badge = addBadge('第' + rec.day + '天', midOf(pts), rec.color);
    rec.ov.push(rec.badge);
    var on = (curTab < 0) || (curTab === rec.idx);
    visible(real, on);
    visible(rec.badge, on);
  }

  // ---------- 定位：浏览器高精度定位 → IP 定位兜底 ----------
  function placeMe(lng, lat, acc) {
    var pos = new T.LngLat(lng, lat);
    if (!locMarker) {
      try {
        locMarker = new T.Marker(pos, { icon: new T.Icon({
          iconUrl: dot(), iconSize: new T.Point(20, 20),
          iconAnchor: new T.Point(10, 10) }) });
        map.addOverLay(locMarker);
      } catch (e) { locMarker = null; }
    } else {
      try { locMarker.setLngLat(pos); } catch (e) {}
    }
    try {
      if (locCircle) { map.removeOverLay(locCircle); locCircle = null; }
      if (acc && acc > 30) {
        locCircle = new T.Circle(pos, Math.min(acc, 3000), {
          color: '#1A73E8', weight: 1, fillColor: '#1A73E8', fillOpacity: 0.12 });
        map.addOverLay(locCircle);
      }
    } catch (e) {}
    try {
      var z = 15;
      try { z = Math.max(map.getZoom(), 15); } catch (e) {}
      map.centerAndZoom(pos, z);
    } catch (e) {}
  }
  function bindLocate() {
    if (!locBtn) return;
    locBtn.style.display = '';
    locBtn.addEventListener('click', function () {
      locBtn.disabled = true;
      locBtn.textContent = '定位中…';
      if (locTip) { locTip.style.display = ''; locTip.textContent = ''; }
      function ok(lng, lat, acc) {
        placeMe(lng, lat, acc);
        locBtn.disabled = false;
        locBtn.textContent = '◎ 我的位置';
        if (locTip) {
          locTip.textContent = '已定位到当前位置'
            + (acc ? '（浏览器精度约 ' + Math.round(acc) + ' 米）' : '（IP 粗定位）');
        }
      }
      function fail(msg) {
        locBtn.disabled = false;
        locBtn.textContent = '◎ 我的位置';
        if (locTip) { locTip.textContent = msg; }
      }
      function ipLocate() {
        try {
          var geo = new T.Geolocation();
          geo.getCurrentPosition(function (r) {
            var pt = null, st = 0;
            try { pt = (r && (r.point || r.lnglat)) || geo.getPoint(); } catch (e) {}
            try { st = geo.getStatus(); } catch (e) {}
            var lng = pt ? (pt.lng !== undefined ? pt.lng : pt.lon) : null;
            var lat = pt ? (pt.lat !== undefined ? pt.lat : pt.latitude) : null;
            if (st === 0 && lng !== null && lat !== null) { ok(lng, lat, 0); }
            else { fail('定位失败：浏览器未授权定位，IP 定位也不可用'); }
          });
        } catch (e) { fail('定位失败：当前浏览器不支持定位'); }
      }
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          function (p) { ok(p.coords.longitude, p.coords.latitude, p.coords.accuracy); },
          function () { ipLocate(); },
          { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 });
      } else { ipLocate(); }
    });
  }

  function init() {
    if (typeof T === 'undefined') { showFallback(); return; }
    var days = DATA.days || [];
    var colors = DATA.colors || [];
    var first = null, i;
    for (i = 0; i < days.length && !first; i++) {
      if (days[i].nodes && days[i].nodes.length) { first = days[i].nodes[0]; }
    }
    if (!first && DATA.home) { first = { lng: DATA.home[1], lat: DATA.home[0] }; }
    if (!first) { showFallback(); return; }
    // 容器必须先有尺寸再初始化，否则瓦片按 0x0 计算，地图是空白
    if (box) box.style.display = '';
    try {
      map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(first.lng, first.lat), 12);
    } catch (e) { showFallback(); return; }
    try { map.enableScrollWheelZoom(); } catch (e) {}
    try { map.addControl(new T.Control.Zoom()); } catch (e) {}
    try { map.addControl(new T.Control.MapType()); } catch (e) {}   // 矢量/影像切换

    days.forEach(function (d, i) {
      var color = colors[i % colors.length] || '#2F6F6B';
      var rec = { idx: i, day: d.day, color: color, ov: [], pts: [], chain: [],
                  ghost: null, badge: null };
      var seq = 0;
      (d.nodes || []).forEach(function (n) {
        var pos = ll(n.lng, n.lat);
        rec.pts.push(pos);
        if (n.uncertain) {
          // 估算坐标的点：单独标注，绝不进连线（连出来的线是假的）
          rec.ov.push(addMarker(pos, '#9A9992', 'unknown', '?',
            '<b>' + n.name + '</b><br><span style="font-size:11px;color:#8A8A82;">'
            + '位置待确认（坐标按市区估算，交通时间未计入）</span>', true));
          rec.ov.push(addLabel(n.name + '·位置估算', pos, -44));
          return;
        }
        rec.chain.push(pos);
        if (n.type === 'restaurant') {
          // 餐位也插旗（刀叉图标），但不加名字标签，避免景点名被盖住
          rec.ov.push(addMarker(pos, color, 'restaurant', '', '<b>' + n.name + '</b>', false));
          return;
        }
        if (n.type === 'transit') {
          // 车站/抵达：列车图标
          rec.ov.push(addMarker(pos, color, 'transit', '', '<b>' + n.name + '</b>', false));
          return;
        }
        if (n.type !== 'attraction') { return; }   // 午休/自由活动只进动线不插旗
        seq += 1;
        // hover 弹介绍卡片（图片+来源+摘要+跳转链接）；没搜到就退回名字气泡
        rec.ov.push(addMarker(pos, color, 'sight', String(seq),
                              n.link ? poiCard(n) : '<b>' + n.name + '</b>', false));
        rec.ov.push(addLabel(n.name, pos, -10));
      });
      if (rec.chain.length >= 2) {
        try {
          // 先铺一条虚线直线占位，路径规划回来后替换成沿道路的实线
          rec.ghost = new T.Polyline(rec.chain, { color: color, weight: 3,
                                                  opacity: 0.35,
                                                  lineStyle: 'dashed' });
          map.addOverLay(rec.ghost);
          rec.ov.push(rec.ghost);
          rec.badge = addBadge('第' + d.day + '天', midOf(rec.chain), color);
          rec.ov.push(rec.badge);
        } catch (e) {}
      }
      dayRecs.push(rec);
    });
    if (DATA.home) {
      var hp = ll(DATA.home[1], DATA.home[0]);
      homeOv = addMarker(hp, '#444441', 'hotel', '', '住宿', false);
      addLabel('住宿', hp, -22);
    }
    if (all.length > 1) {
      try { map.setViewport(all); } catch (e) {}   // 自动缩放到包含全部点位
    }
    if (fb) fb.style.display = 'none';
    bindTabs();
    bindLocate();

    // 逐天把直线占位换成真实道路走线；全失败也不影响页面可用
    var pending = dayRecs.filter(function (r) { return r.chain.length >= 2; }).length;
    var doneDays = 0, okSegs = 0, allSegs = 0;
    if (!pending) {
      if (note) { note.textContent = '点位不足，未生成动线。'; }
      return;
    }
    if (note) { note.textContent = '正在按道路规划路线（' + pending + ' 天）…'; }
    dayRecs.forEach(function (rec) {
      if (rec.chain.length < 2) { return; }
      planDay(rec, function (segs, ok) {
        if (segs.length) {
          replaceGhost(rec, flatten(segs));
        }
        okSegs += ok;
        allSegs += segs.length;
        doneDays += 1;
        if (note && doneDays >= pending) {
          if (okSegs === allSegs) {
            note.textContent = '动线已按驾车路线沿道路绘制（' + allSegs + ' 段全部规划成功）。';
          } else if (okSegs > 0) {
            note.textContent = '动线按驾车路线绘制，' + (allSegs - okSegs)
              + ' 段规划失败已退回直线连接（共 ' + allSegs + ' 段）。';
          } else {
            note.textContent = '路径规划服务不可用，动线为直线示意（不是实际道路）。';
          }
        }
      });
    });
  }
  var s = document.createElement('script');
  s.src = 'https://api.tianditu.gov.cn/api?v=4.0&tk=' + encodeURIComponent(TK);
  var settled = false;
  var timer = setTimeout(function () {
    if (!settled) { settled = true; showFallback(); }
  }, 8000);
  // 天地图偶发的「脚本 onload 了但 window.T 还没就绪」：短暂轮询几次再放弃
  var tries = 0;
  function boot() {
    if (typeof T !== 'undefined') {
      try { init(); } catch (e) { showFallback(); }
      return;
    }
    if (tries++ < 6) { setTimeout(boot, 250); return; }
    showFallback();
  }
  s.onload = function () {
    if (settled) return;
    settled = true; clearTimeout(timer);
    boot();
  };
  s.onerror = function () {
    if (settled) return;
    settled = true; clearTimeout(timer); showFallback();
  };
  (document.head || document.documentElement).appendChild(s);
})();
"""


def render_route_map(result: dict) -> str:
    """路线区：真实地图（天地图 JS API）优先，SVG 示意图兜底。

    - 没配 Key：只渲染 SVG 示意图，并提示去哪里申请；
    - 配了 Key：地图容器 + SVG 兜底（SDK 8 秒加载不出来就回落），
      弱网/打印时页面也不会是空白。
    """
    svg = render_map(result)
    key = get_tdt_key()
    if not key:
        hint = ('<div style="font-size:12px;color:#9A9992;margin-top:10px;">'
                '当前为示意图。在管理后台「模型设置」填入天地图 Key，或设置环境变量 '
                'TIANDITU_KEY（console.tianditu.gov.cn 免费申请，应用类型选'
                '「浏览器端」）后，这里会显示真实地图。</div>')
        return f'<div class="card" style="padding:12px;">{svg}</div>{hint}'
    data = json.dumps(route_map_data(result), ensure_ascii=False).replace("<", "\\u003c")
    safe_key = "".join(ch for ch in key if ch.isalnum())
    if not safe_key:
        return f'<div class="card" style="padding:12px;">{svg}</div>'
    js = ROUTE_MAP_JS.replace("__DATA__", data).replace("__KEY__", safe_key)
    # 天数页签（效仿行程类 App 的「总览/第1天/第2天…」筛选），点击只看某天的动线
    md = route_map_data(result)
    tabs = ('<div id="rt-tabs" class="rt-tabs">'
            '<button type="button" class="on" data-idx="-1">总览</button>')
    for i, d in enumerate(md["days"]):
        c = DAY_COLORS[i % len(DAY_COLORS)]
        tabs += (f'<button type="button" data-idx="{i}">'
                 f'<i style="background:{c};"></i>第{d["day"]}天</button>')
    tabs += '</div>'
    return (
        f'<div class="card" style="padding:12px;">'
        f'<style>'
        f'.rt-tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;}}'
        f'.rt-tabs button{{border:1px solid #D8D7D0;background:#FFFFFF;color:#444441;'
        f'border-radius:14px;padding:3px 12px;font-size:12px;cursor:pointer;'
        f'display:inline-flex;align-items:center;gap:5px;font-family:inherit;}}'
        f'.rt-tabs button i{{width:8px;height:8px;border-radius:50%;display:inline-block;}}'
        f'.rt-tabs button.on{{background:#2F6F6B;color:#FFFFFF;border-color:#2F6F6B;}}'
        f'.rt-tabs button.on i{{background:#FFFFFF;}}'
        # 线上「第N天」徽标与景点名标签：透明底 + 白描边字，接近行程 App 观感
        f'.tdt-label{{background:transparent!important;border:none!important;'
        f'box-shadow:none!important;font-size:12px;color:#3B3A36;'
        f'text-shadow:0 0 3px #FFF,0 0 3px #FFF,0 0 3px #FFF;white-space:nowrap;}}'
        f'.rt-badge{{display:inline-block;color:#FFFFFF;font-weight:bold;'
        f'text-shadow:0 1px 2px rgba(0,0,0,.35);border-radius:10px;'
        f'padding:1px 9px;font-size:12px;white-space:nowrap;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.25);}}'
        f'.rt-wrap{{position:relative;}}'
        f'#rt-locate{{position:absolute;top:10px;right:10px;z-index:20;display:none;'
        f'border:1px solid #D8D7D0;background:#FFFFFF;color:#2F6F6B;border-radius:16px;'
        f'padding:5px 12px;font-size:12px;cursor:pointer;font-family:inherit;'
        f'box-shadow:0 1px 4px rgba(0,0,0,.15);}}'
        f'#rt-locate:disabled{{color:#9A9992;cursor:default;}}'
        f'#rt-locate-tip{{position:absolute;left:10px;bottom:10px;z-index:20;display:none;'
        f'font-size:12px;color:#3B3A36;background:rgba(255,255,255,.92);'
        f'padding:2px 9px;border-radius:10px;max-width:74%;line-height:1.5;}}'
        f'#rt-locate-tip:empty{{display:none;}}'
        # 景点介绍卡片（hover 图钉弹出，效仿行程类 App 的大卡）
        f'.rt-card{{width:238px;font-family:inherit;}}'
        f'.rt-card-img{{height:120px;border-radius:8px 8px 0 0;background:#D8D6CE center/cover no-repeat;}}'
        f'.rt-card-noimg{{display:flex;align-items:center;justify-content:center;'
        f'background:linear-gradient(135deg,#2F6F6B,#4A5C8A);color:#FFFFFF;'
        f'font-size:34px;font-weight:bold;}}'
        f'.rt-card-body{{padding:8px 10px 2px;}}'
        f'.rt-card-name{{font-size:14px;font-weight:bold;color:#3B3A36;}}'
        f'.rt-card-src{{font-size:11px;color:#8A8A82;margin-top:3px;}}'
        f'.rt-card-snip{{font-size:12px;color:#55544E;margin-top:5px;line-height:1.55;'
        f'word-break:break-all;}}'
        f'.rt-card-link{{display:block;margin:8px 10px 10px;padding:6px 0;text-align:center;'
        f'background:#2F6F6B;color:#FFFFFF;border-radius:7px;font-size:13px;'
        f'text-decoration:none;}}'
        f'.rt-card-link:hover{{background:#255A57;}}'
        f'</style>'
        f'{tabs}'
        f'<div class="rt-wrap">'
        f'<div id="route-map" style="height:430px;border-radius:8px;display:none;"></div>'
        f'<button id="rt-locate" type="button" title="定位到我的当前位置">◎ 我的位置</button>'
        f'<div id="rt-locate-tip"></div>'
        f'</div>'
        f'<div id="route-fallback">{svg}</div>'
        f'<div style="font-size:12px;color:#9A9992;margin-top:8px;">'
        f'<span id="rt-route-note">正在按道路规划路线…</span><br>'
        f'底图与路径规划：天地图 · 国家地理信息公共服务平台（www.tianditu.gov.cn）。'
        f'图钉：序号+楼=景点，刀叉=餐饮，列车=车站，床=住宿，灰色?为位置估算点；'
        f'鼠标悬停景点图钉可看介绍卡片，点击「查看介绍」跳转攻略网页。'
        f'动线为驾车路线规划结果，非实时路况，也不代表实际步行/公交走法；'
        f'位置估算点不参与连线。</div>'
        f'</div><script>{js}</script>')


def render_budget_bar(result: dict) -> str:
    b = result["budget"]
    total = b["total"] or 1
    rows = []
    for item in b["breakdown"]:
        pct = item["amount"] / total * 100
        color = BUDGET_COLORS.get(item["category"], "#888780")
        rows.append(
            f'<span style="display:inline-block;height:22px;width:{pct:.2f}%;background:{color};" '
            f'title="{esc(item["label"])} {item["amount"]}"></span>')
    pct = b["contingency"] / total * 100
    rows.append(f'<span style="display:inline-block;height:22px;width:{pct:.2f}%;'
                f'background:#D8D6CE;"></span>')

    legend = []
    seen = set()
    for item in b["breakdown"]:
        cat = item["category"]
        if cat in seen:
            continue
        seen.add(cat)
        amount = sum(x["amount"] for x in b["breakdown"] if x["category"] == cat)
        legend.append(
            f'<div style="display:flex;align-items:center;gap:8px;font-size:13px;color:#444441;">'
            f'<i style="width:10px;height:10px;border-radius:2px;background:'
            f'{BUDGET_COLORS.get(cat, "#888780")};display:inline-block;"></i>'
            f'{BUDGET_LABELS.get(cat, cat)} <b style="font-weight:500;">¥{amount:,.0f}</b></div>')

    note = ""
    if not any(x["category"] == "transport" and (x["amount"] or 0) > 0
               for x in b["breakdown"]):
        # 没有交通项时必须写出来：光看一个 ¥3,553 会以为是整趟花费
        note = ('<div style="font-size:12px;color:#8A6A2F;margin-top:10px;">'
                '合计不含往返大交通（班次票价未查到或未指定出发地）</div>')
    return (f'<div style="display:flex;height:22px;border-radius:4px;overflow:hidden;'
            f'background:#F1EFE8;">{"".join(rows)}</div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:16px;margin-top:14px;">'
            f'{"".join(legend)}</div>' + note)


def render_day(day: dict, color: str, weather: dict | None = None) -> str:
    rows = []
    nodes = day["nodes"]
    # 右列语义：交通行写"怎么去 + 距离"，第二行写"这一站要待多久"，
    # 避免两个"分钟"并排被误读成重复/错乱
    stay_label = {"attraction": "游玩", "restaurant": "用餐",
                  "transit": "车程", "rest": "休息"}
    dot_style = {"attraction": f"background:{color};",
                 "restaurant": f"background:#FFFFFF;border:2.5px solid {color};",
                 "transit": f"background:{color};border-radius:2px;",
                 "rest": "background:#C9C7BE;"}
    for i, n in enumerate(nodes):
        first, last = (i == 0), (i == len(nodes) - 1)
        # 兴趣标记：让用户看得见这一站是靠哪条偏好排上的；弱命中用另一套颜色，
        # 避免"人民公园"被当成"喝茶"已经专门安排好了。
        badges = ""
        for it in (n.get("interests") or []):
            _s = it.get("level") == "strong"
            _bg = "#EAF3F1" if _s else "#F6F1E4"
            _fg = "#2F6F6B" if _s else "#8A6A2F"
            _tx = f'兴趣·{esc(it.get("word", ""))}' if _s else f'近似·{esc(it.get("word", ""))}'
            badges += (f'<span style="font-size:11px;padding:1px 6px;border-radius:3px;'
                       f'margin-left:6px;background:{_bg};color:{_fg};">{_tx}</span>')
        # 交通文案里的"(19.32km)"拆到第二行，主行只留"打车 27 分钟"
        transit = n["transit"] or ""
        dist = ""
        m_dist = re.search(r"\s*[（(]\s*([\d.]+\s*km)\s*[)）]", transit)
        if m_dist:
            dist = m_dist.group(1)
            transit = (transit[:m_dist.start()] + transit[m_dist.end():]).strip()
        transit = transit.strip(" ·，,") or "—"
        right = (f'<div style="font-size:12px;color:#5F5E5A;line-height:1.5;">'
                 f'{esc(transit)}'
                 + (f'<br><span style="color:#9A9992;">{esc(dist)}</span>' if dist else "")
                 + '</div>'
                 + f'<div style="font-size:12px;color:#7A7972;margin-top:3px;">'
                   f'{stay_label.get(n["type"], "停留")} {n["duration_min"]} 分钟'
                 + ('　<span style="color:#8A6A2F;">需预约</span>'
                    if n["booking_required"] else "")
                 + '</div>')
        # 时间轴：上下两段竖线 + 一个类型圆点（首行不画上段，末行不画下段）
        axis = (
            ('' if first else
             f'<div style="position:absolute;left:50%;top:0;height:18px;width:2px;'
             f'margin-left:-1px;background:#E8E6DE;"></div>')
            + ('' if last else
               f'<div style="position:absolute;left:50%;top:18px;bottom:0;width:2px;'
               f'margin-left:-1px;background:#E8E6DE;"></div>')
            + f'<div style="position:absolute;left:50%;top:13px;width:10px;height:10px;'
              f'margin-left:-5px;border-radius:50%;box-shadow:0 0 0 3px #FFFFFF;'
              f'{dot_style.get(n["type"], "background:#C9C7BE;")}"></div>')
        rows.append(f"""
        <tr>
          <td style="width:54px;padding:13px 10px 13px 18px;vertical-align:top;text-align:right;
                     font-variant-numeric:tabular-nums;font-size:13px;color:#2C2C2A;
                     white-space:nowrap;">{esc(n['time'])}</td>
          <td style="width:24px;padding:0;vertical-align:top;position:relative;
                     border-bottom:none;">{axis}</td>
          <td style="padding:13px 10px;vertical-align:top;">
            <div style="font-size:14px;color:#1F1E1C;line-height:1.5;">{esc(n['name'])}{badges}</div>
            <div style="font-size:12px;color:#7A7972;margin-top:3px;line-height:1.55;">{esc(n['tip'])}</div>
          </td>
          <td style="width:152px;padding:13px 18px 13px 8px;vertical-align:top;">
            {right}
          </td>
        </tr>""")
    w = weather or {}
    rain = w.get("rain_prob")
    lo, hi = w.get("low"), w.get("high")
    known = (lo is not None and hi is not None
             and w.get("desc") not in ("待查", "暂无数据", ""))
    wcolor = "#A33" if ((rain or 0) >= 60) else "#7A7972"
    wtxt = ""
    if w:
        wtxt = (f'<span style="font-size:12px;color:{wcolor};">'
                f'　☁ {esc(w.get("desc", "") or "待查")}'
                + (f' {lo}~{hi}℃' if known else "")
                + (f'　降水 {rain}%' if (known and rain is not None) else "")
                + ('　<span style="color:#8A6A2F;">历史同期参考</span>'
                   if w.get("is_forecast") is False else "")
                + '</span>')
    if day.get("geo_uncertain"):
        wtxt += ('<span style="font-size:12px;color:#B4693F;">　⚠ 有点位位置待确认，'
                 '交通时间未计入</span>')
    return f"""
    <section style="border:1px solid #E4E2DA;border-radius:12px;overflow:hidden;
                    margin-bottom:22px;background:#fff;">
      <div style="padding:14px 18px;border-bottom:1px solid #EFEDE6;
                  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;row-gap:6px;">
        <span style="font-size:13px;padding:3px 9px;border-radius:4px;background:{color};
                     color:#fff;">Day {day['day']}</span>
        <span style="font-size:14px;color:#1F1E1C;">{esc(day['date'])} · {esc(day['theme'])}</span>
        {wtxt}
        <span style="margin-left:auto;font-size:12px;color:#7A7972;">
          移动 {day['move_min']} 分钟 · 约 ¥{day['day_budget']:,}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">{' '.join(rows)}</table>
    </section>"""


def render_checks(result: dict) -> str:
    rows = []
    for c in self_check(result):
        ok = c["result"] == "pass"
        color = "#2F6F6B" if ok else "#A33"
        mark = "✓" if ok else "!"
        evidence = (f'<span style="color:#9A9992;">　{esc(c["evidence"])}</span>'
                    if c.get("evidence") else "")
        rows.append(
            f'<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;'
            f'border-bottom:1px dashed #EFEDE6;font-size:13px;">'
            f'<span style="color:{color};width:12px;">{mark}</span>'
            f'<span style="color:#444441;">{esc(c["item"])}{evidence}</span></div>')
    return "".join(rows)


def render_transport(result: dict) -> str:
    parts = []
    for label, res in (("去程", result.get("outbound")), ("返程", result.get("inbound"))):
        if res is None:
            # 没有出发地就不查班次：明说未安排，而不是渲染一张假卡片
            parts.append(
                f'<div><span style="color:#8A8A82;font-size:12px;">{label}</span><br>'
                f'<span style="font-size:14px;color:#9A9992;">未安排</span>　'
                f'<span style="font-size:12px;color:#9A9992;">未指定出发地</span></div>')
            continue
        items = res.get("items") or []
        it = items[0] if items else {}
        fare = it.get("total_price") or 0
        if not it or not fare:
            # 班次/票价没查到：显示 ¥0 会被当成"免费"，必须明说没查到、没计入
            parts.append(
                f'<div><span style="color:#8A8A82;font-size:12px;">{label}</span><br>'
                f'<span style="font-size:14px;color:#9A9992;">未查到班次/票价</span><br>'
                f'<span style="font-size:12px;color:#9A9992;">未计入预算，出发前请自行确认</span></div>')
            continue
        parts.append(
            f'<div><span style="color:#8A8A82;font-size:12px;">{label}</span><br>'
            f'<span style="font-size:14px;">{esc(it.get("code", "—"))}</span>　'
            f'<span style="font-size:13px;color:#5F5E5A;">'
            f'{esc(it.get("depart", ""))} - {esc(it.get("arrive", ""))}　'
            f'¥{fare:,.0f}</span></div>')
    return (f'<div class="card" style="margin-top:14px;display:flex;gap:48px;'
            f'flex-wrap:wrap;">{"".join(parts)}</div>')


def build_html(result: dict, utterance: str) -> str:
    s, b = result["slots"], result["budget"]
    w0 = (result["weather"].get("days") or [{}])[0]
    days = result["days_plan"]
    over = b.get("target") and b["total"] > b["target"]

    _adv = (result.get("weather") or {}).get("advice") or ""
    advice_html = (f'<div style="border:1px solid #E8DFC8;background:#FBF7EE;'
                   f'border-radius:12px;padding:14px 20px;margin-bottom:10px;'
                   f'font-size:13px;color:#7A5B22;">☂ 天气提醒：{esc(_adv)}</div>'
                   if _adv else "")
    days_html = "".join(
        render_day(d, DAY_COLORS[i % len(DAY_COLORS)],
                   next((w for w in (result["weather"].get("days") or [])
                         if w.get("date") == d.get("date")), None))
        for i, d in enumerate(days))
    checks_html = render_checks(result)
    transport_html = render_transport(result)
    # 假设与建议（住宿片区、位置估算说明、预算口径）同步到页面，
    # 别让用户只在聊天文本里见过一次，打开方案页却找不到依据。
    _assum = result.get("assumptions") or []
    _assumptions_html = ""
    if _assum:
        _assumptions_html = (
            '<div style="border:1px solid #E8D9C8;background:#FCF8F2;border-radius:12px;'
            'padding:14px 20px;margin-top:14px;">'
            '<div style="font-size:13px;color:#8A5A26;margin-bottom:6px;">假设与建议</div>'
            + "".join(f'<div style="font-size:13px;color:#5F5E5A;line-height:1.7;">'
                      f'· {esc(a)}</div>' for a in _assum)
            + '</div>')

    # 出行概要：出发地、日期、天数、人数、预算目标、节奏与偏好。
    # 出发地没说就写「未指定」并注明大交通未含——不填默认城市，不假装查过班次。
    origin = result.get("origin") or s.get("origin")
    # 预算里到底有没有路费：出发地填了但班次查不到时，金额同样不含大交通。
    _no_tr = not any(x["category"] == "transport" and (x["amount"] or 0) > 0
                     for x in b["breakdown"])
    origin_note = "大交通未含" if _no_tr else ""
    origin_disp = esc(origin) if origin else "未指定"
    start = s["date_range"]["start"]
    end = (datetime.fromisoformat(start)
           + timedelta(days=max(s["days"] - 1, 0))).date().isoformat()
    pace = ("轻松" if s["pace"] == "relaxed"
            else ("特种兵" if s["pace"] == "intensive" else "适中"))
    prefs = "、".join(s.get("hard_prefs") or []) or "无"

    def fact(label: str, value: str, note: str = "") -> str:
        tag = (f'<span style="font-size:11px;color:#9A9992;margin-left:4px;">{esc(note)}</span>'
               if note else "")
        return (f'<div><div style="font-size:12px;color:#8A8A82;margin-bottom:2px;">{label}</div>'
                f'<div style="font-size:15px;color:#1F1E1C;">{value}{tag}</div></div>')

    facts_html = "".join([
        fact("出发地 → 目的地", f"{origin_disp} → {esc(s['destination'])}", origin_note),
        fact("出发日期", f"{start} 至 {end}"),
        fact("行程天数", f"{s['days']} 天 {max(s['days'] - 1, 1)} 晚"),
        fact("出行人数", (f"{s['party_size']} 人（{esc(s['party_detail'])}）"
                          if s.get("party_detail") else f"{s['party_size']} 人")),
        fact("预算目标", f"¥{b['target']:,.0f}" if b.get("target") else "未设定"),
        fact("节奏 / 硬性偏好", f"{pace} / {esc(prefs)}"),
        fact("首日天气",
             (f"{esc(w0.get('desc') or '待查')} {w0['low']}~{w0['high']}℃"
              if w0.get("low") is not None and w0.get("high") is not None
              else "待查")),
    ])

    cuts = ""
    if result.get("cut_plans"):
        items = "".join(
            f'<li style="margin-bottom:10px;"><b style="font-weight:500;">{esc(p["change"])}</b>'
            f'<span style="color:#2F6F6B;">　省约 ¥{p["save"]}</span>'
            f'<div style="color:#7A7972;font-size:12px;margin-top:2px;">代价：{esc(p["cost_of_change"])}</div></li>'
            for p in result["cut_plans"])
        cuts = f"""
        <div style="border:1px solid #E8D9C8;background:#FCF8F2;border-radius:12px;
                    padding:18px 22px;margin-top:20px;">
          <div style="font-size:14px;color:#8A5A26;margin-bottom:10px;">
            超预算 ¥{b['total'] - b['target']:,.0f} · 三个取舍方案</div>
          <ol style="margin:0;padding-left:20px;font-size:13px;color:#444441;line-height:1.7;">{items}</ol>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(s['destination'])} {s['days']} 天行程</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#F7F6F1; color:#1F1E1C;
          font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
          line-height:1.6; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:44px 28px 60px; }}
  h1 {{ font-size:26px; font-weight:500; margin:0 0 8px; letter-spacing:-0.01em; }}
  h2 {{ font-size:15px; font-weight:500; margin:36px 0 14px; color:#1F1E1C;
        padding-bottom:8px; border-bottom:1px solid #E4E2DA; }}
  .chip {{ display:inline-block; font-size:12px; padding:3px 9px; border-radius:4px;
           background:#EFEDE6; color:#5F5E5A; margin-right:6px; }}
  .card {{ background:#fff; border:1px solid #E4E2DA; border-radius:12px; padding:22px 24px; }}
  table td {{ border-bottom:1px solid #EDEBE3; }}
  table tr:last-child td {{ border-bottom:none; }}
  @media print {{
    body {{ background:#fff; }}
    .wrap {{ padding:0; max-width:none; }}
    section, .card {{ break-inside:avoid; }}
  }}
</style>
</head>
<body>
<div class="wrap">

  <div style="font-size:12px;color:#8A8A82;letter-spacing:0.08em;margin-bottom:10px;">
    TRAVEL PLAN · 由旅行规划智能体生成
  </div>
  <h1>{esc(s['destination'])} · {s['days']} 天 {max(s['days']-1,1)} 晚</h1>
  <div style="font-size:13px;color:#7A7972;margin-bottom:18px;">
    需求原文：{esc(utterance)}
  </div>
  <div class="card" style="margin-bottom:10px;padding:18px 22px;">
    <div style="font-size:12px;color:#8A8A82;letter-spacing:0.08em;margin-bottom:12px;">
      出行概要</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
                gap:14px 24px;">{facts_html}</div>
  </div>

  {advice_html}
  <div class="card" style="display:flex;gap:36px;flex-wrap:wrap;align-items:flex-end;
                           margin-bottom:6px;">
    <div>
      <div style="font-size:12px;color:#8A8A82;">预算合计（含 10% 机动{('，不含往返大交通' if _no_tr else '')}）</div>
      <div style="font-size:34px;font-weight:500;letter-spacing:-0.02em;">¥{b['total']:,.0f}</div>
    </div>
    <div>
      <div style="font-size:12px;color:#8A8A82;">人均</div>
      <div style="font-size:20px;">¥{b['per_person']:,.0f}</div>
    </div>
    <div>
      <div style="font-size:12px;color:#8A8A82;">预算目标</div>
      <div style="font-size:20px;color:{'#A33' if over else '#2F6F6B'};">
        {f"¥{b['target']:,.0f}" if b.get('target') else '未设定'}</div>
    </div>
  </div>
  <div class="card" style="margin-top:10px;">{render_budget_bar(result)}</div>

  {transport_html}

  {_assumptions_html}

  <h2>路线</h2>
  <div style="margin-bottom:10px;font-size:12px;color:#7A7972;">
    {''.join(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;">'
             f'<i style="width:9px;height:9px;border-radius:50%;background:'
             f'{DAY_COLORS[i % len(DAY_COLORS)]};display:inline-block;"></i>Day {d["day"]}</span>'
             for i, d in enumerate(days))}
    <span style="display:inline-flex;align-items:center;gap:6px;">
      <i style="width:9px;height:9px;border-radius:2px;background:#444441;display:inline-block;"></i>住宿</span>
    <span style="display:inline-flex;align-items:center;gap:6px;">
      <i style="width:9px;height:9px;border-radius:50%;border:2px dashed #B4693F;display:inline-block;"></i>位置估算（坐标不可信，仅示意）</span>
  </div>
  <div class="card" style="padding:12px;">{render_route_map(result)}</div>

  <h2>每日行程</h2>
  {days_html}

  {cuts}

  <h2>质检结果</h2>
  <div class="card">{checks_html}</div>

  <div style="margin-top:36px;padding-top:16px;border-top:1px solid #E4E2DA;
              font-size:11px;color:#9A9992;line-height:1.8;">
    天气：16 天内为 Open-Meteo 实时预报，更远日期为历史同期参考（每日卡片已标注）；
    景点坐标来自知识库与 OSM，位置估算项均已标注「位置待确认」；
    路线图底图与路径规划来自天地图（国家地理信息公共服务平台），动线为驾车路线
    规划结果（非实时路况，规划失败段回退直线示意）；
    班次、票价与开放时间可能缺失或有变动，出行前请自行确认。<br>
    价格为参考值，以实际预订页为准 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
</div>
</body>
</html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--utterance", default="我和老婆11月8号从杭州出发去大理，玩4天，"
                                          "预算一人3000，不想太赶，我老婆不吃辣")
    ap.add_argument("-o", "--out", default="../output/行程.html")
    ap.add_argument("--llm", action="store_true",
                    help="用真实模型抽取槽位与重排候选（需 OPENAI_API_KEY）")
    ap.add_argument("--tdt-key", "--tmap-key", dest="tdt_key", default="",
                    help="天地图 Key（也可用 TIANDITU_KEY 环境变量或设置文件 tdt_key）")
    args = ap.parse_args()

    client = None
    if args.llm:
        from . import llm as llm_layer
        client = llm_layer.from_env()
        if client is None:
            print("未配置 OPENAI_API_KEY，改用规则实现。")
    slots = extract_slots(args.utterance, client=client)
    missing = find_missing(slots)
    if missing:
        print(f"信息不足，需先追问：{missing}")
        return
    result = plan_trip(slots, [], client=client)
    if args.tdt_key:
        os.environ["TIANDITU_KEY"] = args.tdt_key
    html_text = build_html(result, args.utterance)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"已生成：{out.resolve()}（{len(html_text)//1024} KB）")


if __name__ == "__main__":
    main()
