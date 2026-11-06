"""把行程结果渲染成单文件 HTML（无外部依赖，可直接打印）。

用法：
    python render_html.py --utterance "..." -o ../output/大理行程.html
    python render_html.py                       # 用内置默认用例

设计取舍：
- 地图用等距圆柱投影 + 纬度校正，够用且不需要地图瓦片服务；不涉及行政边界，
  只画点位与连线，避免地图数据合规问题。
- 预算用横向堆叠条而非饼图：金额差异大时，条状比较更准。
- 全部内联样式与 SVG，单文件双击可开，也方便直接打印成 PDF 带走。
"""

from __future__ import annotations

import argparse
import html
import math
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
        if len(coords) > 1:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" '
                         f'stroke-width="2" stroke-opacity="0.55" stroke-dasharray="6 4"/>')
        seq = 0
        placed: list[tuple[float, float]] = []
        for n, (x, y) in zip([x for x in d["nodes"] if x.get("geo")], coords):
            if n["type"] != "attraction":
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
    for n in day["nodes"]:
        icon = {"attraction": "◦", "restaurant": "◇", "transit": "→", "rest": "·"}.get(n["type"], "·")
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
        rows.append(f"""
        <tr>
          <td style="width:62px;padding:10px 8px;vertical-align:top;font-variant-numeric:tabular-nums;
                     font-size:13px;color:#2C2C2A;">{esc(n['time'])}</td>
          <td style="width:16px;padding:10px 0;vertical-align:top;color:{color};font-size:14px;">{icon}</td>
          <td style="padding:10px 8px;vertical-align:top;">
            <div style="font-size:14px;color:#1F1E1C;">{esc(n['name'])}{badges}</div>
            <div style="font-size:12px;color:#7A7972;margin-top:3px;line-height:1.55;">{esc(n['tip'])}</div>
          </td>
          <td style="width:150px;padding:10px 8px;vertical-align:top;font-size:12px;color:#7A7972;">
            {esc(n['transit'])}<br>{n['duration_min']} 分钟
            {f'<br><span style="color:#8A6A2F;">需预约</span>' if n['booking_required'] else ''}
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
      <div style="padding:14px 20px;border-bottom:1px solid #EFEDE6;
                  display:flex;align-items:baseline;gap:12px;">
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
        items = res.get("items") or [{}]
        it = items[0]
        parts.append(
            f'<div><span style="color:#8A8A82;font-size:12px;">{label}</span><br>'
            f'<span style="font-size:14px;">{esc(it.get("code", "—"))}</span>　'
            f'<span style="font-size:13px;color:#5F5E5A;">'
            f'{esc(it.get("depart", ""))} - {esc(it.get("arrive", ""))}　'
            f'¥{it.get("total_price", 0):,.0f}</span></div>')
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
        fact("出行人数", f"{s['party_size']} 人"),
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
  table td {{ border-bottom:1px solid #F4F2EC; }}
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

  <h2>路线</h2>
  <div style="margin-bottom:10px;font-size:12px;color:#7A7972;">
    {''.join(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:16px;">'
             f'<i style="width:9px;height:9px;border-radius:50%;background:'
             f'{DAY_COLORS[i % len(DAY_COLORS)]};display:inline-block;"></i>Day {d["day"]}</span>'
             for i, d in enumerate(days))}
    <span style="display:inline-flex;align-items:center;gap:6px;">
      <i style="width:9px;height:9px;border-radius:2px;background:#444441;display:inline-block;"></i>住宿</span>
  </div>
  <div class="card" style="padding:12px;">{render_map(result)}</div>

  <h2>每日行程</h2>
  {days_html}

  {cuts}

  <h2>质检结果</h2>
  <div class="card">{checks_html}</div>

  <div style="margin-top:36px;padding-top:16px;border-top:1px solid #E4E2DA;
              font-size:11px;color:#9A9992;line-height:1.8;">
    价格为参考值，以实际预订页为准 · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}<br>
    本页由 prompts/01-system-prompt.md 定义的输出规范渲染，数据来自 mock 工具层。
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
    html_text = build_html(result, args.utterance)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    print(f"已生成：{out.resolve()}（{len(html_text)//1024} KB）")


if __name__ == "__main__":
    main()
