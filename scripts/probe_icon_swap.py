# -*- coding: utf-8 -*-
"""SDK 能力探针：验证「小圆点 ↔ 完整图标」两态切换的可行性：
① T.Marker.setIcon 存在且换图标后锚点生效；
② setIcon 同步替换 img（.tdt-marker-icon 的 src 立即等于新 URL）；
③ 给 img 加 CSS 动画 class 后元素不被 SDK 重建（动画可见）。"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"C:\Users\Administrator\WorkBuddy\2026-10-29-10-47-53\travel-agent")
KEY = json.load(open(ROOT / "data" / ".llm_settings.json", encoding="utf-8"))["tdt_key"]

probe = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
@keyframes rt-pop { 0%{transform:scale(.45);opacity:.35;}
                    65%{transform:scale(1.15);opacity:1;}
                    100%{transform:scale(1);} }
img.tdt-marker-icon.rt-pop { animation: rt-pop .22s ease-out; transform-origin: 50% 100%; }
</style></head><body>
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
window.onload = function(){
  setTimeout(function(){
    try{
      var map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(104.06,30.67), 12);
      var pinUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="40">'
        + '<path d="M10 23.5L15 38.5l5-15z" fill="#2F6F6B"/>'
        + '<circle cx="15" cy="13.5" r="12.2" fill="#2F6F6B" stroke="#FFF" stroke-width="1.8"/>'
        + '<text x="15" y="12.2" font-size="10.5" fill="#FFF" text-anchor="middle">1</text></svg>');
      var dotUrl = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22">'
        + '<circle cx="11" cy="11" r="8" fill="#2F6F6B" stroke="#FFF" stroke-width="2.5"/></svg>');
      var mk = new T.Marker(new T.LngLat(104.06, 30.67),
        { icon: new T.Icon({ iconUrl: dotUrl, iconSize: new T.Point(22,22),
                             iconAnchor: new T.Point(11,11) }) });
      map.addOverLay(mk);
      setTimeout(function(){
        var hasSetIcon = typeof mk.setIcon === 'function';
        var img0 = document.querySelector('#route-map img.tdt-marker-icon');
        var src0 = img0 ? img0.getAttribute('src') : null;
        var r0 = img0 ? img0.getBoundingClientRect() : null;
        mk.setIcon(new T.Icon({ iconUrl: pinUrl, iconSize: new T.Point(30,40),
                                iconAnchor: new T.Point(15,38) }));
        var img1 = document.querySelector('#route-map img.tdt-marker-icon');
        var src1 = img1 ? img1.getAttribute('src') : null;
        var r1 = img1 ? img1.getBoundingClientRect() : null;
        img1.classList.add('rt-pop');
        var clsAfterAdd = img1.className;
        setTimeout(function(){
          var img2 = document.querySelector('#route-map img.tdt-marker-icon');
          document.getElementById('out').textContent = 'INFO=' + JSON.stringify({
            hasSetIcon: hasSetIcon,
            src_is_dot: src0 === dotUrl,
            dot_h: r0 ? Math.round(r0.height) : null,
            src_swapped: src1 === pinUrl,
            same_img: img1 === img0,
            pin_h: r1 ? Math.round(r1.height) : null,
            cls: clsAfterAdd,
            img_kept: img2 === img1,
            n_imgs: document.querySelectorAll('#route-map img.tdt-marker-icon').length
          });
        }, 300);
      }, 500);
    }catch(e){ document.getElementById('out').textContent = 'ERR=' + e.message; }
  }, 300);
};
</script>
</body></html>
""".replace("__KEY__", KEY)

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
ok = (data["hasSetIcon"] and data["src_is_dot"] and data["src_swapped"]
      and data["img_kept"] and data["dot_h"] == 22 and data["pin_h"] == 40)
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
