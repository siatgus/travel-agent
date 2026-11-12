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

DAY_COLORS = ["#3d7bff", "#ff7a59", "#7a5cff", "#0aa574", "#b44df0"]
BUDGET_COLORS = {
    "transport": "#3d7bff",
    "hotel": "#ff7a59",
    "ticket": "#7a5cff",
    "food": "#f0a03a",
    "local_transit": "#2ec4a5",
}
BUDGET_LABELS = {
    "transport": "交通", "hotel": "住宿", "ticket": "门票",
    "food": "餐饮", "local_transit": "市内", "other": "其他",
}


def esc(s) -> str:
    return html.escape(str(s))


def stay_text(days) -> str:
    """天数口径：单日行程是「当日往返」，不能写「1 天 1 晚」。"""
    d = int(days or 1)
    n = max(d - 1, 0)
    return f"{d} 天 {n} 晚" if n else f"{d} 天 · 当日往返"


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
        parts.append(f'<line x1="{gx}" y1="0" x2="{gx}" y2="{h}" stroke="#e6eaf5" stroke-width="1"/>')
    for gy in range(0, h, 44):
        parts.append(f'<line x1="0" y1="{gy}" x2="{w}" y2="{gy}" stroke="#e6eaf5" stroke-width="1"/>')

    if home:
        hx, hy = pt(home)
        parts.append(f'<rect x="{hx-5:.1f}" y="{hy-5:.1f}" width="10" height="10" rx="2" fill="#1d2333"/>')
        parts.append(f'<text x="{hx+10:.1f}" y="{hy+4:.1f}" font-size="11" fill="#7a8299">住宿</text>')

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
                    parts.append(f'<text x="{x:.1f}" y="{y+21:.1f}" font-size="10" fill="#e05c3d" '
                                 f'text-anchor="middle">{esc(n["name"][:10])}·位置估算</text>')
                continue
            seq += 1
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="{color}"/>')
            parts.append(f'<text x="{x:.1f}" y="{y+3.5:.1f}" font-size="10" fill="#fff" '
                         f'text-anchor="middle">{seq}</text>')
            # 标签避让：与已放置标签过近则省略，避免文字糊成一团
            if all(abs(x - px) > 62 or abs(y - py) > 15 for px, py in placed):
                placed.append((x, y))
                parts.append(f'<text x="{x:.1f}" y="{y+21:.1f}" font-size="10" fill="#1d2333" '
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
    img 例外：它不走 innerHTML，而是进 CSS background-image:url(...)
    ——CSS 不做 HTML 反转义，esc 会把 & 变 &amp; 打坏查询参数（图片就没了）。
    改为剥危险字符 < > " ' ( )（防注入目的不变），& 与参数保留；
    前端 imgCss 还会再剥一遍括号/引号/空白，双保险。
    """
    links = result.get("poi_links") or {}
    # 城市中心：算「距市中心直线 X 公里」用（弹窗信息卡按行程类 App 风格）
    dest = str((result.get("slots") or {}).get("destination") or "").strip()
    center = None
    try:
        from .real_tools import resolve_geo
        center = resolve_geo(dest)
    except Exception:
        center = None
    days = []
    for d in result["days_plan"]:
        nodes = []
        for n in d["nodes"]:
            if not n.get("geo"):
                continue
            node = {"name": esc(n["name"]), "time": n["time"],
                    "lat": n["geo"][0], "lng": n["geo"][1],
                    "type": n["type"],
                    "day": d["day"],
                    "uncertain": bool(n.get("geo_uncertain"))}
            # 距市中心直线距离（弹窗第二行），城市中心拿不到就不给
            if center:
                node["dist"] = round(haversine_km(n["geo"], center), 1)
            # 门票口径与 uncertain_ticket_notes 一致：
            # 已核实 cost=0 → 免费；估算点 cost>0 → 「约」；估算点 cost=0 → 未核实
            if n["type"] == "attraction":
                c = n.get("cost") or 0
                if n.get("geo_uncertain"):
                    node["price"] = f"门票约 ¥{c}/人" if c > 0 else "门票未核实"
                else:
                    node["price"] = "免费" if c == 0 else f"门票 ¥{c}/人"
            if n["type"] == "attraction":
                info = links.get(n["name"])
                if info and info.get("url"):
                    node["link"] = {
                        "url": esc(str(info["url"])),
                        "site": esc(str(info.get("site") or "")),
                        "snippet": esc(str(info.get("snippet") or "")),
                        "img": re.sub(r'[<>"\'()]', "", str(info.get("img") or "")),
                    }
            nodes.append(node)
        days.append({"day": d["day"], "nodes": nodes})
    home = (result["hotel"].get("items") or [{}])[0].get("geo")
    return {"days": days, "home": home, "colors": DAY_COLORS, "city": esc(dest)}


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
      // 上半序号 + 下半小图标（类别小图标必须与序号同轴心：12px 图标格
      // 缩放 0.78 后宽 9.36，translate x = 15 - 9.36/2 = 10.3，否则
      // 数字和类别标志看着不匹配——用户的原话）
      inner = '<text x="15" y="12.4" font-size="11" font-family="Arial,sans-serif"'
        + ' font-weight="bold" fill="#FFFFFF" text-anchor="middle">' + txt + '</text>'
        + '<g transform="translate(10.3,14.2) scale(0.78)" opacity="0.95">'
        + glyph(kind) + '</g>';
    } else {
      inner = '<g transform="translate(9.3,7.6) scale(0.95)" opacity="0.95">'
        + glyph(kind) + '</g>';
    }
    var body = '<path d="M10 23.5L15 38.5l5-15z" fill="' + color + '"/>'
      + '<circle cx="15" cy="13.5" r="12.2" fill="' + color + '" stroke="#FFFFFF"'
      + ' stroke-width="1.8"/>'
      + inner;
    // 弹入动画必须放在 SVG 内部（SMIL）：img 元素上的 CSS transform 是
    // 天地图框架的定位机制（translate3d），CSS keyframes 一动它图标就被
    // 甩到图层面板原点附近——录屏里「图标在其他地方弹出来」的元凶。
    // SMIL 随新 src 加载自动播放，围绕尾尖 (15,38.5) 从 0.45 弹到 1.15 回 1，
    // 外层 translate 组合实现绕点缩放；fill=freeze 停在原尺寸。
    // 注意 <g> 必须逐层闭合（img 里 SVG 是严格 XML，少一个 </g> 整张破图）
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="40" viewBox="0 0 30 40">'
      + '<g transform="translate(15,38.5)"><g opacity="0.35">'
      + '<animate attributeName="opacity" values="0.35;1;1" keyTimes="0;0.65;1"'
      + ' dur="0.22s" fill="freeze"/>'
      + '<g><animateTransform attributeName="transform" type="scale"'
      + ' values="0.45;1.15;1" keyTimes="0;0.65;1" dur="0.22s" fill="freeze"/>'
      + '<g transform="translate(-15,-38.5)">' + body + '</g>'
      + '</g></g></g></svg>';
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }
  // 未选中态小圆点（用户指定样式，同「我的位置」蓝点语言但用当天配色）：
  // 选中（悬停/点按）才展开成完整圆章图标，展开即 rt-pop 过渡
  function mDot(color) {
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
      + '<circle cx="11" cy="11" r="7.5" fill="' + color + '" stroke="#FFFFFF"'
      + ' stroke-width="2.6"/></svg>';
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
  // 卡片贴地图边缘被裁的解法：不动地图，挪卡片自己。
  // 天地图 T.InfoWindow 有官方 setOffset(Point)/getOffset()/update()
  // （真实 SDK 实测确认），偏移语义即屏幕像素方向：x 正=右移、y 正=下移。
  // 开卡后拿卡片与地图容器的真实矩形比，超出多少像素就把 offset 补多少。
  // 注意不要改 .tdt-infowindow 的 CSS——它的定位由框架 transform 管理，改了会打架。
  // 取「当前可见」的卡片矩形：天地图关闭 InfoWindow 后可能留下 display:none 的旧节点，
  // 隐藏节点量出来是 0 尺寸，会把贴边判定带偏
  function visibleCardRect() {
    try {
      var scope = map && map.getContainer ? (map.getContainer() || null) : null;
      if (!scope) { scope = document.getElementById('route-map'); }
      if (!scope || !scope.querySelectorAll) { return null; }
      var els = scope.querySelectorAll('.rt-card'), best = null, fallback = null;
      for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        // 阈值 40：迷你卡（餐位/车站等）只有 ~55px 高，80 会把它滤掉
        if (r.width > 80 && r.height > 40) {
          fallback = r;
          if (els[i].offsetParent !== null) { best = r; }   // 真正渲染中的优先
        }
      }
      return best || fallback;
    } catch (e) { return null; }
  }
  // 选中图钉的图标矩形：用光标位置识别（文档级 mousemove 全程跟踪 RT.mouse，
  // 悬停图钉前必先移动鼠标，坐标永远新鲜——不依赖天地图事件对象自带 clientX，
  // 实测它可能没有；锚点换算/最近图标匹配两种方案也都实测选错过）。
  // 取光标 30px 内最近的可见图标
  function iconRectAt(mx, my) {
    try {
      var scope = map && map.getContainer ? map.getContainer()
                                          : document.getElementById('route-map');
      if (!scope || !scope.querySelectorAll) { return null; }
      var els = scope.querySelectorAll('.tdt-marker-icon');
      var best = null, nd = 1e18;
      for (var i = 0; i < els.length; i++) {
        if (els[i].offsetParent === null) { continue; }   // 隐藏图层
        var r = els[i].getBoundingClientRect();
        if (r.width < 4) { continue; }
        var dx = Math.max(r.left - mx, mx - r.right, 0);
        var dy = Math.max(r.top - my, my - r.bottom, 0);
        var d = dx * dx + dy * dy;
        if (d < nd) { nd = d; best = r; }
      }
      return (best && nd <= 900) ? best : null;          // 30px 内才算选中
    } catch (e) { return null; }
  }
  function overlapArea(a, q) {
    var oy = Math.min(a.bottom, q.bottom) - Math.max(a.top, q.top);
    var ox = Math.min(a.right, q.right) - Math.max(a.left, q.left);
    return (oy > 0 && ox > 0) ? oy * ox : 0;
  }
  // 沿 axis('x'/'y') sign(+1/-1) 方向推 t，直到不与任何 guard 相交。
  // 迭代收敛：推开一层后可能又压到更远的图标（最多 6 轮）。
  // 返回 {d, out}：位移量与出界量；d=0 表示本来就不相交
  function clearShift(t, guards, axis, sign, m, pad) {
    var d = 0;
    for (var it = 0; it < 6; it++) {
      var need = 0;
      for (var i = 0; i < guards.length; i++) {
        var a = { top: t.top + (axis === 'y' ? sign * d : 0),
                  bottom: t.bottom + (axis === 'y' ? sign * d : 0),
                  left: t.left + (axis === 'x' ? sign * d : 0),
                  right: t.right + (axis === 'x' ? sign * d : 0) };
        if (overlapArea(a, guards[i]) <= 0) { continue; }
        // 分离位移按方向算（对称 overlap 推不够）：
        // 上推 = t 底缘-图标顶缘，下推 = 图标底缘-t 顶缘，水平同理
        var sep = (axis === 'y')
          ? (sign < 0 ? a.bottom - guards[i].top : guards[i].bottom - a.top)
          : (sign < 0 ? a.right - guards[i].left : guards[i].right - a.left);
        if (sep > need) { need = sep; }
      }
      if (!need) { break; }
      d += need + 2;   // +2px 余量
    }
    var a0 = { top: t.top + (axis === 'y' ? sign * d : 0),
               bottom: t.bottom + (axis === 'y' ? sign * d : 0),
               left: t.left + (axis === 'x' ? sign * d : 0),
               right: t.right + (axis === 'x' ? sign * d : 0) };
    var out = Math.max(0, m.top + pad - a0.top)
            + Math.max(0, a0.bottom - (m.bottom - pad))
            + Math.max(0, m.left + pad - a0.left)
            + Math.max(0, a0.right - (m.right - pad));
    var hit = 0;
    for (var j = 0; j < guards.length; j++) { hit += overlapArea(a0, guards[j]); }
    return { d: Math.round(d), out: Math.round(out), hit: hit };
  }
  // 卡片位置收敛（挪卡片不动地图），一次算好一次挪：
  // ① 钳进地图可视区；② 避让选中的图标——四向「推开相交图标」的最小位移里
  // 按「不压图标 > 尽量不出地图 > 位移最小」挑最优，与钳制合并成一次
  // setOffset（多轮分步挪用户看得见跳动 =「乱动」）；
  // 620ms 后静默复测一轮兜残差，残差为零就不动。
  // offset 以 RTbase（首次记录的原始锚点偏移）为基准，每次开卡先复位，
  // 保证换缩放级别后不会跨次累计漂移
  function fitCardInMap(iw, round) {
    if (!iw || !iw.setOffset || !iw.getOffset) { return; }
    if (round > 1) { return; }
    if (!round) {
      if (!iw.RTbase) {
        try {
          var o0 = iw.getOffset();
          iw.RTbase = (o0 && typeof o0.x === 'number') ? { x: o0.x, y: o0.y }
                                                      : { x: 0, y: -38 };
        } catch (e) { iw.RTbase = { x: 0, y: -38 }; }
      }
      try { iw.setOffset(new T.Point(iw.RTbase.x, iw.RTbase.y)); iw.update(); } catch (e) {}
    }
    setTimeout(function () {
      try {
        var r = visibleCardRect();
        var box = document.getElementById('route-map');
        if (!r || !box) { return; }
        var m = box.getBoundingClientRect();
        // 守卫矩形只认「选中的图标」：优先用 openCard 瞬间采集的
        // iw.RTguard0（那一刻光标必然压在本图钉上，不会选错），
        // 复测轮沿用同一个——复测时光标可能已移进卡片，实时识别会换来换去
        if (round === 0) {
          iw.RTguard = iw.RTguard0
            || (RT.mouse ? iconRectAt(RT.mouse.x, RT.mouse.y) : null);
        }
        var guards = iw.RTguard ? [iw.RTguard] : [];
        var pad = 8, cx = 0, cy = 0;
        if (r.top < m.top + pad) { cy = Math.round(m.top + pad - r.top); }
        else if (r.bottom > m.bottom - pad) { cy = -Math.round(r.bottom - (m.bottom - pad)); }
        if (r.left < m.left + pad) { cx = Math.round(m.left + pad - r.left); }
        else if (r.right > m.right - pad) { cx = -Math.round(r.right - (m.right - pad)); }
        var t = { top: r.top + cy, bottom: r.bottom + cy,
                  left: r.left + cx, right: r.right + cx };
        var ax = 0, ay = 0;
        if (guards.length) {
          var dirs = [
            { axis: 'y', sign: -1 }, { axis: 'y', sign: 1 },
            { axis: 'x', sign: -1 }, { axis: 'x', sign: 1 }
          ];
          var best = null;
          for (var i = 0; i < dirs.length; i++) {
            var c = clearShift(t, guards, dirs[i].axis, dirs[i].sign, m, pad);
            if (best === null || c.hit < best.hit ||
                (c.hit === best.hit && (c.out < best.out - 4 ||
                 (Math.abs(c.out - best.out) <= 4 && c.d < best.d)))) {
              best = { hit: c.hit, out: c.out, d: c.d, axis: dirs[i].axis, sign: dirs[i].sign };
            }
          }
          if (best && best.d > 0) {
            if (best.axis === 'x') { ax = best.sign * best.d; } else { ay = best.sign * best.d; }
          }
        }
        if (!cx && !cy && !ax && !ay) { return; }
        var o = iw.getOffset();
        if (!o || typeof o.x !== 'number') { o = iw.RTbase || { x: 0, y: -38 }; }
        iw.setOffset(new T.Point(o.x + cx + ax, o.y + cy + ay));
        iw.update();
        fitCardInMap(iw, round + 1);
      } catch (e) {}
      // 首轮 0ms：openInfoWindow 同步渲染完 DOM，立即量立即挪——把
      // 「base 位置闪一帧（可能压住图标）」压到肉眼不可见；复测轮留 620ms
    }, round ? 620 : 0);
  }
  function addMarker(pos, color, kind, txt, infoHtml, uncertain) {
    var mk;
    try {
      // 未选中态默认小圆点；选中才展开完整圆章（两态 + 过渡，见 selMk）
      mk = new T.Marker(pos, { icon: mIcon(mDot(color), 22, 22, 11, 11) });
    } catch (e) { mk = new T.Marker(pos); }
    map.addOverLay(mk);
    mk.RTpin = { color: color, kind: kind, txt: txt };   // 两态图标参数
    try {
      var iw = new T.InfoWindow(infoHtml, { offset: new T.Point(0, -38),
                                            maxWidth: 250 });
      function openCard() {
        // 同一图钉已开卡就不重开：mouseover 在卡片段落间移动可能再触发，
        // 重开会把 offset 复位重收敛，卡片看得见地跳（用户反馈「乱动」）
        if (RT.openMk === mk) { hoverStay(); return; }
        if (RT.openMk) { selMk(RT.openMk, false); }   // 换选：旧钉回落小圆点
        hoverStay(); RT.openMk = mk;
        selMk(mk, true);   // 先展开圆章，守卫量的才是展开后的图标矩形
        mk.openInfoWindow(iw);
        // 避让守卫必须在 openCard 这一刻采集：触发它的是 mouseover，
        // 光标必然正压在本图钉上，iconRectAt 不会选错；拖到 fitCardInMap
        // 的定时器里再量，光标可能已移进卡片 → 守卫 null → 卡片压住图标
        iw.RTguard0 = RT.mouse ? iconRectAt(RT.mouse.x, RT.mouse.y) : null;
        fitCardInMap(iw, 0);
      }
      // hover 开卡；关卡不挂任何 DOM 边界事件——卡片随 fitCardInMap 挪位时
      // mouseenter/mouseleave 挪前挪后各误报一次，事件驱动的开关在静止光标
      // 下也死循环（浏览器在 DOM 变化下照样派发边界事件）：卡片盖图标 →
      // mouseout → 关 → 图标露出 → mouseover → 开 → 又盖住……就是用户
      // 录屏里的「一直闪烁、图标跟着闪」。关卡唯一裁判 = 光标坐标，见 hoverTick
      mk.addEventListener('mouseover', openCard);
      if (uncertain) {
        // 只有估算点用 click 打开（它没有 hover 卡片，保留点按习惯）
        mk.addEventListener('click', openCard);
      }
    } catch (e) {}
    return mk;
  }
  function mIcon(url, w, h, ax, ay) {
    return new T.Icon({ iconUrl: url, iconSize: new T.Point(w, h),
                        iconAnchor: new T.Point(ax, ay) });
  }
  // 两态图标切换：on=选中（完整圆章+弹入过渡）/未选（回落小圆点）。
  // 弹入动画在 SVG 内部（SMIL）随新 src 加载自动播放；img 元素的
  // transform 是框架定位，绝不能碰（CSS keyframes 动它=图标飞到别处）
  function selMk(mk, on) {
    try {
      var p = mk.RTpin;
      if (!p || typeof mk.setIcon !== 'function') { return; }
      mk.setIcon(on ? mIcon(pin(p.color, p.kind, p.txt), 30, 40, 15, 38)
                    : mIcon(mDot(p.color), 22, 22, 11, 11));
    } catch (e) {}
  }
  // hover 卡片的开关状态机（window.RThover 供 InfoWindow 内联事件回调）
  var RT = { timer: null, openMk: null, mouse: null };
  // 文档级跟踪光标：悬停图钉前必先移动鼠标，开卡时 RT.mouse 永远新鲜。
  // 不用事件对象自带的 clientX——天地图 marker 事件对象实测可能不带
  document.addEventListener('mousemove', function (ev) {
    RT.mouse = { x: ev.clientX, y: ev.clientY };
    hoverTick();
  });
  function hoverStay() {
    if (RT.timer) { clearTimeout(RT.timer); RT.timer = null; }
  }
  // 光标是否压在卡片矩形内
  function cursorOnCard(p) {
    var r = visibleCardRect();
    return !!(r && p.x >= r.left && p.x <= r.right
                && p.y >= r.top && p.y <= r.bottom);
  }
  // 光标是否压在任一可见图标上（±6px 余量；压住才算，30px 邻近不算，
  // 否则卡片粘着不放）
  function cursorOnPin(p) {
    try {
      var scope = map && map.getContainer ? map.getContainer()
                                          : document.getElementById('route-map');
      if (!scope || !scope.querySelectorAll) { return false; }
      var els = scope.querySelectorAll('.tdt-marker-icon');
      for (var i = 0; i < els.length; i++) {
        if (els[i].offsetParent === null) { continue; }   // 隐藏图层
        var r = els[i].getBoundingClientRect();
        if (r.width < 4) { continue; }
        if (p.x >= r.left - 6 && p.x <= r.right + 6
            && p.y >= r.top - 6 && p.y <= r.bottom + 6) { return true; }
      }
    } catch (e) {}
    return false;
  }
  // 关卡唯一裁判：光标既不在卡片上、也不压着任何图钉，才延时 350ms 关。
  // 只由 mousemove 驱动，与 DOM 边界事件完全无关——静止光标不会产生
  // mousemove，也就绝不会误关，闪烁死循环从机制上不存在。
  // 卡片内联 RThover.stay/leave 保留：leave 后下一次 mousemove 会自然接手
  function hoverTick() {
    if (!RT.openMk) { return; }
    var p = RT.mouse;
    if (!p) { return; }
    if (cursorOnCard(p) || cursorOnPin(p)) { hoverStay(); return; }
    if (!RT.timer) {
      RT.timer = setTimeout(function () {
        RT.timer = null;
        var q = RT.mouse;
        if (q && (cursorOnCard(q) || cursorOnPin(q))) { return; }   // 定时期间又移回来了
        var m = RT.openMk;
        try { m.closeInfoWindow(); } catch (e) {}
        if (RT.openMk === m) { RT.openMk = null; }
        selMk(m, false);   // 卡片关了，图标回落小圆点
      }, 350);
    }
  }
  function hoverClose() { hoverTick(); }
  window.RThover = { stay: hoverStay, leave: hoverClose };
  // 无介绍可用的点位（餐位/车站/住宿/没搜到介绍的景点/估算点）：
  // 统一给一张迷你卡——名称 + 当日色标签 + 时间，不再是裸 <b>名字</b>。
  // 复用 rt-card 类名让 visibleCardRect 能量到它（防贴边裁切与误关）
  function miniCard(n, color, tag, note) {
    return '<div class="rt-card rt-mini" style="border-left-color:' + darken(color, 0.75)
      + ';" onmouseenter="RThover.stay()" onmouseleave="RThover.leave()">'
      + '<div class="rt-mini-name">' + n.name + '</div>'
      + '<div class="rt-mini-meta"><span class="rt-mini-tag" style="background:'
      + darken(color, 0.75) + ';">' + tag + '</span>'
      + (n.time ? '<span>' + n.time + '</span>' : '')
      + '</div>'
      + locRow(n)
      + (note ? '<div class="rt-mini-note">' + note + '</div>' : '')
      + '</div>';
  }
  // 景点介绍卡片（效仿行程类 App 的 hover 大卡）：图 + 名 + 来源 + 摘要 + 跳转
  // 「图片卡」标记：介绍页没搜到、只有搜图来的图，站点名固定为百度图片
  var IMG_ONLY_TAG = '百度图片';
  // 「📍城市 · 距市中心直线X公里」信息行（行程类 App 风格），dist 没有就不给
  function locRow(n) {
    if (n.dist == null && !DATA.city) { return ''; }
    return '<div class="rt-poi-loc">📍 ' + (DATA.city || '')
      + (n.dist != null ? ' · 距市中心直线' + n.dist + '公里' : '') + '</div>';
  }
  function poiCard(n) {
    var L = n.link || {};
    // CSS url() 不带引号，危险字符直接剥掉（缩略图 URL 本来也不含这些）
    var imgCss = String(L.img || '').replace(/[()\\s'"]/g, '');
    var img = imgCss
      ? '<div class="rt-card-img" style="background-image:url(' + imgCss + ');"></div>'
      : '<div class="rt-card-img rt-card-noimg">' + String(n.name).slice(0, 1) + '</div>';
    return '<div class="rt-card" onmouseenter="RThover.stay()" onmouseleave="RThover.leave()">' + img
      + '<div class="rt-card-body"><div class="rt-card-name">' + n.name + '</div>'
      + '<div class="rt-poi-row"><span class="rt-poi-day">第' + n.day + '天</span>'
      + '<span>已添加至行程</span>'
      + (n.price ? '<span class="rt-poi-price">' + n.price + '</span>' : '')
      + '</div>'
      + (L.site ? '<div class="rt-card-src">' + (L.site == IMG_ONLY_TAG
        ? '图片来源：' + L.site : '介绍来源：' + L.site
        + ' · 点击下方链接查看完整攻略') + '</div>' : '')
      + (L.snippet ? '<div class="rt-card-snip">' + L.snippet + '</div>' : '')
      + locRow(n)
      + '</div>'
      + (L.url ? '<a class="rt-card-link" href="' + L.url
        + '" target="_blank" rel="noopener">'
        + (L.site == IMG_ONLY_TAG ? '查看更多图片 ↗' : '查看介绍 ↗') + '</a>'
        : '<div class="rt-card-link">✓ 已添加</div>')
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
  // 不用 SDK 的 T.DrivingRoute：实测它走 HTTPS，而天地图 HTTPS /drive 挂死
  // 不响应（HTTP 正常），回调永远不触发 → 永远回落直线。改走本服务端代理
  // /api/tianditu/drive（HTTP 上游 + XML 解析成 JSON），前端 fetch 自己的接口。
  // 坐标同为 CGCS2000，直接可用，无需转换。
  function planSeg(a, b, cb) {
    var finished = false;
    function done(path) {
      if (finished) return;
      finished = true;
      cb(path);
    }
    var timer = setTimeout(function () { done(null); }, 12000);
    try {
      var q = 'orig=' + encodeURIComponent(a.lng + ',' + a.lat)
            + '&dest=' + encodeURIComponent(b.lng + ',' + b.lat);
      fetch('/api/tianditu/drive?' + q).then(function (resp) { return resp.json(); })
        .then(function (d) {
          if (finished) return;
          clearTimeout(timer);
          if (d && d.ok && d.pts && d.pts.length > 1) {
            var pts = [];
            for (var i = 0; i < d.pts.length; i++) {
              if (d.pts[i] && d.pts[i].length === 2) {
                pts.push(new T.LngLat(d.pts[i][0], d.pts[i][1]));
              }
            }
            done(pts.length > 1 ? pts : null);
          } else { done(null); }
        })
        .catch(function () { if (!finished) { clearTimeout(timer); done(null); } });
    } catch (e) { clearTimeout(timer); done(null); }
  }
  function thin(pts, maxPts) {
    if (pts.length <= maxPts) return pts;
    var step = Math.ceil(pts.length / maxPts), out = [], i;
    for (i = 0; i < pts.length; i += step) { out.push(pts[i]); }
    if (out[out.length - 1] !== pts[pts.length - 1]) { out.push(pts[pts.length - 1]); }
    return out;
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
      var color = colors[i % colors.length] || '#2b5fe0';
      var rec = { idx: i, day: d.day, color: color, ov: [], pts: [], chain: [],
                  ghost: null, badge: null };
      var seq = 0;
      (d.nodes || []).forEach(function (n) {
        var pos = ll(n.lng, n.lat);
        rec.pts.push(pos);
        if (n.uncertain) {
          // 估算坐标的点：单独标注，绝不进连线（连出来的线是假的）
          rec.ov.push(addMarker(pos, '#a0a8bd', 'unknown', '?',
            miniCard(n, '#a0a8bd', '位置估算',
                     '位置待确认 · 坐标按市区估算，交通时间未计入'), true));
          rec.ov.push(addLabel(n.name + '·位置估算', pos, -44));
          return;
        }
        rec.chain.push(pos);
        if (n.type === 'restaurant') {
          // 餐位也插旗（刀叉图标），但不加名字标签，避免景点名被盖住
          rec.ov.push(addMarker(pos, color, 'restaurant', '', miniCard(n, color, '用餐'), false));
          return;
        }
        if (n.type === 'transit') {
          // 车站/抵达：列车图标
          rec.ov.push(addMarker(pos, color, 'transit', '', miniCard(n, color, '交通'), false));
          return;
        }
        if (n.type !== 'attraction') { return; }   // 午休/自由活动只进动线不插旗
        seq += 1;
        // hover 弹介绍卡片（图片+来源+摘要+跳转链接）；没搜到就退回名字气泡
        rec.ov.push(addMarker(pos, color, 'sight', String(seq),
                              n.link ? poiCard(n) : miniCard(n, color, '景点'), false));
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
      homeOv = addMarker(hp, '#1d2333', 'hotel', '',
                         miniCard({name: '住宿'}, '#2c3550', '酒店'), false);
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
        hint = ('<div style="font-size:12px;color:#a0a8bd;margin-top:10px;">'
                '当前为示意图。在管理后台「模型设置」填入天地图 Key，或设置环境变量 '
                'TIANDITU_KEY（console.tianditu.gov.cn 免费申请，应用类型选'
                '「浏览器端」）后，这里会显示真实地图。</div>')
        return f'{svg}{hint}'
    data = json.dumps(route_map_data(result), ensure_ascii=False).replace("<", "\\u003c")
    safe_key = "".join(ch for ch in key if ch.isalnum())
    if not safe_key:
        return svg
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
        f'<style>'
        f'.rt-tabs{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;}}'
        f'.rt-tabs button{{border:1px solid #e6eaf5;background:#FFFFFF;color:#1d2333;'
        f'border-radius:14px;padding:3px 12px;font-size:12px;cursor:pointer;'
        f'display:inline-flex;align-items:center;gap:5px;font-family:inherit;}}'
        f'.rt-tabs button i{{width:8px;height:8px;border-radius:50%;display:inline-block;}}'
        f'.rt-tabs button.on{{background:#2b5fe0;color:#FFFFFF;border-color:#2b5fe0;}}'
        f'.rt-tabs button.on i{{background:#FFFFFF;}}'
        # 线上「第N天」徽标与景点名标签：透明底 + 白描边字，接近行程 App 观感
        f'.tdt-label{{background:transparent!important;border:none!important;'
        f'box-shadow:none!important;font-size:12px;color:#2c3550;'
        f'text-shadow:0 0 3px #FFF,0 0 3px #FFF,0 0 3px #FFF;white-space:nowrap;}}'
        f'.rt-badge{{display:inline-block;color:#FFFFFF;font-weight:bold;'
        f'text-shadow:0 1px 2px rgba(0,0,0,.35);border-radius:10px;'
        f'padding:1px 9px;font-size:12px;white-space:nowrap;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.25);}}'
        f'.rt-wrap{{position:relative;}}'
        f'#rt-locate{{position:absolute;top:10px;right:10px;z-index:20;display:none;'
        f'border:1px solid #e6eaf5;background:#FFFFFF;color:#2b5fe0;border-radius:16px;'
        f'padding:5px 12px;font-size:12px;cursor:pointer;font-family:inherit;'
        f'box-shadow:0 1px 4px rgba(0,0,0,.15);}}'
        f'#rt-locate:disabled{{color:#a0a8bd;cursor:default;}}'
        f'#rt-locate-tip{{position:absolute;left:10px;bottom:10px;z-index:20;display:none;'
        f'font-size:12px;color:#2c3550;background:rgba(255,255,255,.92);'
        f'padding:2px 9px;border-radius:10px;max-width:74%;line-height:1.5;}}'
        f'#rt-locate-tip:empty{{display:none;}}'
        # 景点介绍卡片（hover 图钉弹出，效仿行程类 App 的大卡）
        f'.rt-card{{width:238px;font-family:inherit;}}'
        f'/* 迷你卡自带白底会跟 InfoWindow 的白色气泡叠成「框外框」，'
        f'所以卡片只留左侧当日色条，气泡就是唯一的框 */'
        f'.rt-mini{{width:196px;padding:3px 2px 4px 10px;'
        f'border-left:3px solid #2b5fe0;}}'
        f'.rt-mini-name{{font-size:13.5px;font-weight:bold;color:#2c3550;line-height:1.4;}}'
        f'.rt-mini-meta{{display:flex;align-items:center;gap:7px;margin-top:6px;'
        f'font-size:11px;color:#7a8299;}}'
        f'.rt-mini-tag{{color:#FFFFFF;border-radius:999px;padding:2px 8px;'
        f'font-size:10.5px;font-weight:600;letter-spacing:.05em;flex:none;}}'
        f'.rt-mini-note{{font-size:10.5px;color:#a0a8bd;margin-top:5px;line-height:1.5;}}'
        f'.rt-card-img{{height:120px;border-radius:8px 8px 0 0;background:#dfe6f5 center/cover no-repeat;}}'
        f'.rt-card-noimg{{display:flex;align-items:center;justify-content:center;'
        f'background:linear-gradient(135deg,#2b5fe0,#7a5cff);color:#FFFFFF;'
        f'font-size:34px;font-weight:bold;}}'
        f'.rt-card-body{{padding:8px 10px 2px;}}'
        f'.rt-card-name{{font-size:14px;font-weight:bold;color:#2c3550;}}'
        f'.rt-card-src{{font-size:11px;color:#7a8299;margin-top:3px;}}'
        f'.rt-card-snip{{font-size:12px;color:#3c4358;margin-top:5px;line-height:1.55;'
        f'word-break:break-all;}}'
        f'.rt-card-link{{display:block;margin:8px 10px 10px;padding:7px 0;text-align:center;'
        f'background:#2b5fe0;color:#FFFFFF!important;border-radius:7px;font-size:13px;'
        f'font-weight:bold;letter-spacing:.05em;text-decoration:none!important;}}'
        f'.rt-card-link:hover{{background:#1e4fd0;color:#FFFFFF!important;}}'
        f'.rt-poi-row{{display:flex;align-items:center;gap:7px;margin-top:5px;'
        f'font-size:11.5px;color:#3c4358;flex-wrap:wrap;}}'
        f'.rt-poi-day{{background:#2b5fe0;color:#FFFFFF;border-radius:4px;'
        f'padding:1.5px 7px;font-size:11px;font-weight:600;flex:none;}}'
        f'.rt-poi-price{{color:#B26A00;}}'
        f'.rt-poi-loc{{font-size:11px;color:#7a8299;margin-top:5px;}}'
        f'</style>'
        f'{tabs}'
        f'<div class="rt-wrap">'
        f'<div id="route-map" style="height:620px;border-radius:8px;display:none;"></div>'
        f'<button id="rt-locate" type="button" title="定位到我的当前位置">◎ 我的位置</button>'
        f'<div id="rt-locate-tip"></div>'
        f'</div>'
        f'<div id="route-fallback">{svg}</div>'
        f'<div style="font-size:12px;color:#a0a8bd;margin-top:8px;">'
        f'<span id="rt-route-note">正在按道路规划路线…</span><br>'
        f'底图与路径规划：天地图 · 国家地理信息公共服务平台（www.tianditu.gov.cn）。'
        f'图钉：序号+楼=景点，刀叉=餐饮，列车=车站，床=住宿，灰色?为位置估算点；'
        f'鼠标悬停景点图钉可看介绍卡片，点击「查看介绍」跳转攻略网页。'
        f'动线为驾车路线规划结果，非实时路况，也不代表实际步行/公交走法；'
        f'位置估算点不参与连线。</div>'
        f'<script>{js}</script>')


def render_budget_bar(result: dict) -> str:
    b = result["budget"]
    total = b["total"] or 1
    rows = []
    for item in b["breakdown"]:
        pct = item["amount"] / total * 100
        color = BUDGET_COLORS.get(item["category"], "#8892ab")
        rows.append(
            f'<span style="display:inline-block;height:22px;width:{pct:.2f}%;background:{color};" '
            f'title="{esc(item["label"])} {item["amount"]}"></span>')
    pct = b["contingency"] / total * 100
    rows.append(f'<span style="display:inline-block;height:22px;width:{pct:.2f}%;'
                f'background:#dfe6f5;"></span>')

    legend = []
    seen = set()
    for item in b["breakdown"]:
        cat = item["category"]
        if cat in seen:
            continue
        seen.add(cat)
        amount = sum(x["amount"] for x in b["breakdown"] if x["category"] == cat)
        legend.append(
            f'<div style="display:flex;align-items:center;gap:8px;font-size:13px;color:#1d2333;">'
            f'<i style="width:10px;height:10px;border-radius:2px;background:'
            f'{BUDGET_COLORS.get(cat, "#8892ab")};display:inline-block;"></i>'
            f'{BUDGET_LABELS.get(cat, cat)} <b style="font-weight:500;">¥{amount:,.0f}</b></div>')

    note = ""
    if not any(x["category"] == "transport" and (x["amount"] or 0) > 0
               for x in b["breakdown"]):
        # 没有交通项时必须写出来：光看一个 ¥3,553 会以为是整趟花费
        note = ('<div style="font-size:12px;color:#B26A00;margin-top:10px;">'
                '合计不含往返大交通（班次票价未查到或未指定出发地）</div>')
    return (f'<div style="display:flex;height:22px;border-radius:4px;overflow:hidden;'
            f'background:#eef2fc;">{"".join(rows)}</div>'
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
                 "rest": "background:#c9d2e8;"}
    for i, n in enumerate(nodes):
        first, last = (i == 0), (i == len(nodes) - 1)
        # 兴趣标记：让用户看得见这一站是靠哪条偏好排上的；弱命中用另一套颜色，
        # 避免"人民公园"被当成"喝茶"已经专门安排好了。
        badges = ""
        for it in (n.get("interests") or []):
            _s = it.get("level") == "strong"
            _bg = "#E8F0FF" if _s else "#FFF4E8"
            _fg = "#2b5fe0" if _s else "#B26A00"
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
        right = (f'<div style="font-size:12px;color:#3c4358;line-height:1.5;">'
                 f'{esc(transit)}'
                 + (f'<br><span style="color:#a0a8bd;">{esc(dist)}</span>' if dist else "")
                 + '</div>'
                 + f'<div style="font-size:12px;color:#7a8299;margin-top:3px;">'
                   f'{stay_label.get(n["type"], "停留")} {n["duration_min"]} 分钟'
                 + ('　<span style="color:#B26A00;">需预约</span>'
                    if n["booking_required"] else "")
                 + '</div>')
        # 时间轴：上下两段竖线 + 一个类型圆点（首行不画上段，末行不画下段）
        axis = (
            ('' if first else
             f'<div style="position:absolute;left:50%;top:0;height:18px;width:2px;'
             f'margin-left:-1px;background:#e6eaf5;"></div>')
            + ('' if last else
               f'<div style="position:absolute;left:50%;top:18px;bottom:0;width:2px;'
               f'margin-left:-1px;background:#e6eaf5;"></div>')
            + f'<div style="position:absolute;left:50%;top:13px;width:10px;height:10px;'
              f'margin-left:-5px;border-radius:50%;box-shadow:0 0 0 3px #FFFFFF;'
              f'{dot_style.get(n["type"], "background:#c9d2e8;")}"></div>')
        rows.append(f"""
        <tr>
          <td style="width:54px;padding:13px 10px 13px 18px;vertical-align:top;text-align:right;
                     font-variant-numeric:tabular-nums;font-size:13px;color:#1d2333;
                     white-space:nowrap;">{esc(n['time'])}</td>
          <td style="width:24px;padding:0;vertical-align:top;position:relative;
                     border-bottom:none;">{axis}</td>
          <td style="padding:13px 10px;vertical-align:top;">
            <div style="font-size:14px;color:#1d2333;line-height:1.5;">{esc(n['name'])}{badges}</div>
            <div style="font-size:12px;color:#7a8299;margin-top:3px;line-height:1.55;">{esc(n['tip'])}</div>
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
    wcolor = "#e05c3d" if ((rain or 0) >= 60) else "#7a8299"
    wtxt = ""
    if w:
        wtxt = (f'<span style="font-size:12px;color:{wcolor};">'
                f'　☁ {esc(w.get("desc", "") or "待查")}'
                + (f' {lo}~{hi}℃' if known else "")
                + (f'　降水 {rain}%' if (known and rain is not None) else "")
                + ('　<span style="color:#B26A00;">历史同期参考</span>'
                   if w.get("is_forecast") is False else "")
                + '</span>')
    if day.get("geo_uncertain"):
        wtxt += ('<span style="font-size:12px;color:#e05c3d;">　⚠ 有点位位置待确认，'
                 '交通时间未计入</span>')
    return f"""
    <section style="border:1px solid #e6eaf5;border-radius:12px;overflow:hidden;
                    margin-bottom:22px;background:#fff;">
      <div style="padding:14px 18px;border-bottom:1px solid #eef1fa;
                  display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;row-gap:6px;">
        <span style="font-size:13px;padding:3px 9px;border-radius:4px;background:{color};
                     color:#fff;">Day {day['day']}</span>
        <span style="font-size:14px;color:#1d2333;">{esc(day['date'])} · {esc(day['theme'])}</span>
        {wtxt}
        <span style="margin-left:auto;font-size:12px;color:#7a8299;">
          移动 {day['move_min']} 分钟 · 约 ¥{day['day_budget']:,}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;">{' '.join(rows)}</table>
    </section>"""


def render_checks(result: dict) -> str:
    rows = []
    for c in self_check(result):
        ok = c["result"] == "pass"
        color = "#2b5fe0" if ok else "#e05c3d"
        mark = "✓" if ok else "!"
        evidence = (f'<span style="color:#a0a8bd;">　{esc(c["evidence"])}</span>'
                    if c.get("evidence") else "")
        rows.append(
            f'<div style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;'
            f'border-bottom:1px dashed #eef1fa;font-size:13px;">'
            f'<span style="color:{color};width:12px;">{mark}</span>'
            f'<span style="color:#1d2333;">{esc(c["item"])}{evidence}</span></div>')
    return "".join(rows)


def render_transport(result: dict) -> str:
    # 卡片必须写出方向：去程 origin→dest，返程 dest→origin。
    # 只写"返程 + 班次号"时，用户拿这张卡去查票很容易查成出发方向
    # （2026-11-11 实测：上海去南京，返程也写成了上海→南京）。
    origin = str(result.get("origin") or "")
    dest = str((result.get("slots") or {}).get("destination") or "")
    parts = []
    for label, res in (("去程", result.get("outbound")), ("返程", result.get("inbound"))):
        title = label
        if origin and dest:
            route = (f"{origin} → {dest}" if label == "去程"
                     else f"{dest} → {origin}")
            title = f"{label} {route}"
        if res is None:
            # 没有出发地就不查班次：明说未安排，而不是渲染一张假卡片
            parts.append(
                f'<div><span style="color:#7a8299;font-size:12px;">{esc(title)}</span><br>'
                f'<span style="font-size:14px;color:#a0a8bd;">未安排</span>　'
                f'<span style="font-size:12px;color:#a0a8bd;">未指定出发地</span></div>')
            continue
        items = res.get("items") or []
        it = items[0] if items else {}
        fare = it.get("total_price") or 0
        if not it or not fare:
            # 班次/票价没查到：显示 ¥0 会被当成"免费"，必须明说没查到、没计入
            parts.append(
                f'<div><span style="color:#7a8299;font-size:12px;">{esc(title)}</span><br>'
                f'<span style="font-size:14px;color:#a0a8bd;">未查到班次/票价</span><br>'
                f'<span style="font-size:12px;color:#a0a8bd;">未计入预算，出发前请自行确认</span></div>')
            continue
        parts.append(
            f'<div><span style="color:#7a8299;font-size:12px;">{esc(title)}</span><br>'
            f'<span style="font-size:14px;">{esc(it.get("code", "—"))}</span>　'
            f'<span style="font-size:13px;color:#3c4358;">'
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
    advice_html = (f'<div style="border:1px solid #d8e3ff;background:#f2f6ff;'
                   f'border-radius:12px;padding:14px 20px;margin-bottom:10px;'
                   f'font-size:13px;color:#2b5fe0;">☂ 天气提醒：{esc(_adv)}</div>'
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
            '<div style="border:1px solid #d8e3ff;background:#f2f6ff;border-radius:12px;'
            'padding:14px 20px;margin-top:14px;">'
            '<div style="font-size:13px;color:#2b5fe0;margin-bottom:6px;">假设与建议</div>'
            + "".join(f'<div style="font-size:13px;color:#3c4358;line-height:1.7;">'
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
        tag = (f'<span style="font-size:11px;color:#a0a8bd;margin-left:4px;">{esc(note)}</span>'
               if note else "")
        return (f'<div><div style="font-size:12px;color:#7a8299;margin-bottom:2px;">{label}</div>'
                f'<div style="font-size:15px;color:#1d2333;">{value}{tag}</div></div>')

    facts_html = "".join([
        fact("出发地 → 目的地", f"{origin_disp} → {esc(s['destination'])}", origin_note),
        fact("出发日期", f"{start} 至 {end}"),
        fact("行程天数", stay_text(s['days'])),
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
            f'<span style="color:#2b5fe0;">　省约 ¥{p["save"]}</span>'
            f'<div style="color:#7a8299;font-size:12px;margin-top:2px;">代价：{esc(p["cost_of_change"])}</div></li>'
            for p in result["cut_plans"])
        cuts = f"""
        <div style="border:1px solid #d8e3ff;background:#f2f6ff;border-radius:12px;
                    padding:18px 22px;margin-top:20px;">
          <div style="font-size:14px;color:#2b5fe0;margin-bottom:10px;">
            超预算 ¥{b['total'] - b['target']:,.0f} · 三个取舍方案</div>
          <ol style="margin:0;padding-left:20px;font-size:13px;color:#1d2333;line-height:1.7;">{items}</ol>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(s['destination'])} {s['days']} 天行程</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#f7f9ff; color:#1d2333;
          font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
          line-height:1.6; -webkit-font-smoothing:antialiased; }}
  .wrap {{ max-width:860px; margin:0 auto; padding:44px 28px 60px; }}
  h1 {{ font-size:26px; font-weight:500; margin:0 0 8px; letter-spacing:-0.01em; }}
  h2 {{ font-size:15px; font-weight:500; margin:36px 0 14px; color:#1d2333;
        padding-bottom:8px; border-bottom:1px solid #e6eaf5; }}
  .chip {{ display:inline-block; font-size:12px; padding:3px 9px; border-radius:4px;
           background:#eef1fa; color:#3c4358; margin-right:6px; }}
  .card {{ background:#fff; border:1px solid #e6eaf5; border-radius:12px; padding:22px 24px; }}
  /* 地图卡横向突破正文 860px 栏宽（大屏放大看图，窄屏自动回落 100%） */
  .rt-mapcard {{ width:min(1160px, calc(100vw - 24px)); margin-left:50%;
                 transform:translateX(-50%); }}
  @media (max-width:900px) {{
    .rt-mapcard {{ width:100%; margin-left:0; transform:none; }}
    #route-map {{ height:440px !important; }}
  }}
  /* 手机端：收窄留白，超宽表格/地图在卡片内横向滚动而不是撑破版心 */
  @media (max-width:640px) {{
    .wrap {{ padding:26px 14px 44px; }}
    h1 {{ font-size:22px; }}
    .card {{ padding:14px 16px; overflow-x:auto; }}
    table {{ font-size:12px; }}
    #route-map {{ height:360px !important; }}
  }}
  table td {{ border-bottom:1px solid #eef1fa; }}
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

  <div style="font-size:12px;color:#7a8299;letter-spacing:0.08em;margin-bottom:10px;">
    TRAVEL PLAN · 由旅行规划智能体生成
  </div>
  <h1>{esc(s['destination'])} · {stay_text(s['days'])}</h1>
  <div style="font-size:13px;color:#7a8299;margin-bottom:18px;">
    需求原文：{esc(utterance)}
  </div>
  <div class="card" style="margin-bottom:10px;padding:18px 22px;">
    <div style="font-size:12px;color:#7a8299;letter-spacing:0.08em;margin-bottom:12px;">
      出行概要</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
                gap:14px 24px;">{facts_html}</div>
  </div>

  {advice_html}
  <div class="card" style="display:flex;gap:36px;flex-wrap:wrap;align-items:flex-end;
                           margin-bottom:6px;">
    <div>
      <div style="font-size:12px;color:#7a8299;">预算合计（含 10% 机动{('，不含往返大交通' if _no_tr else '')}）</div>
      <div style="font-size:34px;font-weight:500;letter-spacing:-0.02em;">¥{b['total']:,.0f}</div>
    </div>
    <div>
      <div style="font-size:12px;color:#7a8299;">人均</div>
      <div style="font-size:20px;">¥{b['per_person']:,.0f}</div>
    </div>
    <div>
      <div style="font-size:12px;color:#7a8299;">预算目标</div>
      <div style="font-size:20px;color:{'#e05c3d' if over else '#2b5fe0'};">
        {f"¥{b['target']:,.0f}" if b.get('target') else '未设定'}</div>
    </div>
  </div>
  <div class="card" style="margin-top:10px;">{render_budget_bar(result)}</div>

  {transport_html}

  {_assumptions_html}

  <h2>路线</h2>
  <div style="margin-bottom:10px;font-size:12px;color:#7a8299;">
    {''.join(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;">'
             f'<i style="width:9px;height:9px;border-radius:50%;background:'
             f'{DAY_COLORS[i % len(DAY_COLORS)]};display:inline-block;"></i>Day {d["day"]}</span>'
             for i, d in enumerate(days))}
    <span style="display:inline-flex;align-items:center;gap:6px;">
      <i style="width:9px;height:9px;border-radius:2px;background:#1d2333;display:inline-block;"></i>住宿</span>
    <span style="display:inline-flex;align-items:center;gap:6px;">
      <i style="width:9px;height:9px;border-radius:50%;border:2px dashed #e05c3d;display:inline-block;"></i>位置估算（坐标不可信，仅示意）</span>
  </div>
  <div class="card rt-mapcard" style="padding:12px;">{render_route_map(result)}</div>

  <h2>每日行程</h2>
  {days_html}

  {cuts}

  <h2>质检结果</h2>
  <div class="card">{checks_html}</div>

  <div style="margin-top:36px;padding-top:16px;border-top:1px solid #e6eaf5;
              font-size:11px;color:#a0a8bd;line-height:1.8;">
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
