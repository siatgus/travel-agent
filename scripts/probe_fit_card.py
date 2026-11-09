# -*- coding: utf-8 -*-
"""端到端行为探针：从 render_html.py 逐字提取 fitCardInMap/visibleCardRect，
在真实天地图 SDK + 贴边场景下验证「挪卡片不动地图」真的生效。"""
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


funcs = extract("visibleCardRect") + "\n" + extract("fitCardInMap")

probe = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
.rt-card{width:240px;height:200px;background:#fff;border:1px solid #ccc;}
</style></head><body>
<div id="route-map" style="width:600px;height:400px;"></div>
<pre id="out">PENDING</pre>
<script src="https://api.tianditu.gov.cn/api?v=4.0&tk=__KEY__"></script>
<script>
var map;   /* visibleCardRect 引用同名变量：真实页面里在 IIFE 闭包，这里须全局声明 */
__FUNCS__
</script>
<script>
window.onload = function(){
  setTimeout(function(){
    try{
      map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(104.06,30.67), 12);
      // 图钉放在地图顶部（贴上边），气泡默认往上开必然被裁
      var mk = new T.Marker(new T.LngLat(104.06, 30.672));
      map.addOverLay(mk);
      var iw = new T.InfoWindow('<div class="rt-card">card</div>',
                                {offset:new T.Point(0,-38), maxWidth:250});
      function rect(){ var box=document.getElementById('route-map');
        var c=box.querySelector('.rt-card'); var r=c.getBoundingClientRect();
        var m=box.getBoundingClientRect();
        return [Math.round(r.top-m.top), Math.round(r.bottom-m.top),
                Math.round(r.left-m.left), Math.round(r.right-m.left)]; }
      mk.openInfoWindow(iw);
      setTimeout(function(){
        var before = rect();
        fitCardInMap(iw, 0);
        setTimeout(function(){
          var after = rect();
          // 第二次开卡：offset 应先复位再重新收敛（不跨次累计漂移）
          setTimeout(function(){
            mk.openInfoWindow(iw);
            setTimeout(function(){
              var before2 = rect();
              fitCardInMap(iw, 0);
              setTimeout(function(){
                var after2 = rect();
                document.getElementById('out').textContent = 'INFO=' + JSON.stringify({
                  before: before, after: after,
                  reopen_before: before2, reopen_after: after2,
                  offset_final: (function(o){return o?[o.x,o.y]:null;})(iw.getOffset())
                });
              }, 900);
            }, 700);
          }, 200);
        }, 900);
      }, 700);
    }catch(e){ document.getElementById('out').textContent = 'ERR=' + e.message; }
  }, 300);
};
</script>
</body></html>
""".replace("__KEY__", KEY).replace("__FUNCS__", funcs)

p = Path(tempfile.gettempdir()) / "rtcheck" / "probe_fit.html"
p.parent.mkdir(exist_ok=True)
p.write_text(probe, encoding="utf-8")

edge = None
for cand in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
    if Path(cand).exists():
        edge = cand
        break
out = subprocess.run(
    [edge, "--headless", "--disable-gpu", "--dump-dom", "--virtual-time-budget=16000",
     "--window-size=800,600", "file:///" + str(p).replace("\\", "/")],
    capture_output=True, text=True, timeout=90).stdout
m = re.search(r"INFO=.*", out, re.S)
if not m:
    print("NO INFO; dump len =", len(out))
    sys.exit(1)
data = json.loads(m.group(0)[5:].rsplit("</pre>", 1)[0].strip())
top, bottom, left, right = data["after"]
ok1 = top >= 0 and bottom <= 400 and left >= 0 and right <= 600
t2, b2, l2, r2 = data["reopen_after"]
ok2 = t2 >= 0 and b2 <= 400 and l2 >= 0 and r2 <= 600
print("首次开卡  before=%s after=%s  -> %s" % (data["before"], data["after"],
      "PASS 卡片完整落在地图内" if ok1 else "FAIL 仍被裁"))
print("再次开卡  before=%s after=%s  -> %s" % (data["reopen_before"], data["reopen_after"],
      "PASS 复位后重新收敛" if ok2 else "FAIL"))
print("最终 offset =", data["offset_final"])
sys.exit(0 if ok1 and ok2 else 1)
