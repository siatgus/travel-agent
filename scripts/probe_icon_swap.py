# -*- coding: utf-8 -*-
"""SDK 能力探针：验证「小圆点 ↔ 完整图标」两态切换 + SVG 内部 SMIL 弹入动画：
① T.Marker.setIcon 存在且换图标后 src 立即生效；
② 动画期间 img 元素位置零漂移（SMIL 在 SVG 文档内部播放，不碰框架
   定位 transform——CSS keyframes 动 transform 会把图标甩到图层面板
   原点附近，即用户录屏「图标在其他地方弹出来」）；
③ 图标尺寸切换正确（22 圆点 ↔ 40 圆章）。"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\WorkBuddy\2026-10-29-10-47-53\travel-agent")
KEY = json.load(open(ROOT / "data" / ".llm_settings.json", encoding="utf-8"))["tdt_key"]
src = (ROOT / "src" / "travel_agent" / "render_html.py").read_text(encoding="utf-8")


def extract(name):
    m = re.search(r"function %s\(" % name + r".*?\n  \}", src, re.S)
    assert m, name
    return m.group(0)


funcs = "\n".join(extract(n) for n in ("glyph", "pin"))

probe = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<pre id="err">-</pre>
<div id="route-map" style="width:600px;height:400px;"></div>
<pre id="out">PENDING</pre>
<script>
window.onerror = function(msg, src, line, col){
  document.getElementById('err').textContent += ' | JSERR: ' + msg + ' @' + line + ':' + col;
};
</script>
<script src="https://api.tianditu.gov.cn/api?v=4.0&tk=__KEY__"></script>
<script>
var map;
__FUNCS__
</script>
<script>
window.onload = function(){
  setTimeout(function(){
    try{
      map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(104.06,30.67), 12);
      var pinUrl = pin('#2F6F6B', 'sight', '1');   // 含 SMIL 弹入动画
      var dotUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
        + '<circle cx="11" cy="11" r="7.5" fill="#2F6F6B" stroke="#FFF"'
        + ' stroke-width="2.6"/></svg>');
      var mk = new T.Marker(new T.LngLat(104.06, 30.67),
        { icon: new T.Icon({ iconUrl: dotUrl, iconSize: new T.Point(22,22),
                             iconAnchor: new T.Point(11,11) }) });
      map.addOverLay(mk);
      setTimeout(function(){
        var img0 = document.querySelector('#route-map img.tdt-marker-icon');
        var r0 = img0.getBoundingClientRect();
        mk.setIcon(new T.Icon({ iconUrl: pinUrl, iconSize: new T.Point(30,40),
                                iconAnchor: new T.Point(15,38) }));
        var img1 = document.querySelector('#route-map img.tdt-marker-icon');
        var r1 = img1.getBoundingClientRect();       // 换图标瞬间
        var snap1 = [r1.top, r1.left, r1.width, r1.height];
        setTimeout(function(){
          var r2 = img1.getBoundingClientRect();     // SMIL 动画进行中
          var snap2 = [r2.top, r2.left, r2.width, r2.height];
          setTimeout(function(){
            var r3 = img1.getBoundingClientRect();   // 动画结束（freeze）
            var snap3 = [r3.top, r3.left, r3.width, r3.height];
            document.getElementById('out').textContent = 'INFO=' + JSON.stringify({
              hasSetIcon: typeof mk.setIcon === 'function',
              src_swapped: img1.getAttribute('src') === pinUrl,
              src_has_smil: img1.getAttribute('src').indexOf('animateTransform') > -1,
              same_img: img1 === img0,
              snap1: snap1, snap2: snap2, snap3: snap3
            });
          }, 400);
        }, 110);
      }, 500);
    }catch(e){ document.getElementById('out').textContent = 'ERR=' + e.message; }
  }, 300);
};
</script>
</body></html>
""".replace("__KEY__", KEY).replace("__FUNCS__", funcs)

p = Path(tempfile.gettempdir()) / "rtcheck" / "probe_icon.html"
p.parent.mkdir(exist_ok=True)
p.write_text(probe, encoding="utf-8")

edge = None
for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
    if Path(cand).exists():
        edge = cand
        break
out = subprocess.run(
    [edge, "--headless", "--disable-gpu", "--dump-dom", "--virtual-time-budget=12000",
     "--window-size=800,600", "file:///" + str(p).replace("\\", "/")],
    capture_output=True, text=True, timeout=120).stdout
m = re.search(r"INFO=(\{.*?\})</pre>", out, re.S)
if not m:
    mp = re.search(r'<pre id="out">([^<]*)</pre>', out)
    me = re.search(r'<pre id="err">([^<]*)</pre>', out)
    print("NO INFO; out =", (mp.group(1)[:400] if mp else "?"))
    print("err =", (me.group(1)[:400] if me else "?"))
    sys.exit(1)
data = json.loads(m.group(1).replace("&quot;", '"').replace("&#39;", "'"))
print(json.dumps(data, ensure_ascii=False, indent=1))
s1, s2, s3 = data["snap1"], data["snap2"], data["snap3"]
# 动画中与动画前后位置必须一致（容差 1px；宽高允许 SVG 渲染差异 ≤1px）
stable = all(abs(a - b) <= 1 for a, b in zip(s1, s2)) and \
         all(abs(a - b) <= 1 for a, b in zip(s2, s3))
ok = (data["hasSetIcon"] and data["src_swapped"] and data["src_has_smil"]
      and data["same_img"] and stable
      and s1[2] == 30 and s1[3] == 40)
print("PASS 图标动画期间零漂移" if ok else "FAIL 动画期间图标位置漂移！")
sys.exit(0 if ok else 1)
