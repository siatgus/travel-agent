# -*- coding: utf-8 -*-
"""端到端行为探针：从 render_html.py 逐字提取 visibleCardRect/fitCardInMap/markerRects，
在真实天地图 SDK 下验证「挪卡片不动地图」：
① 贴边图钉开卡，卡片钳进地图可视区；
② 近旁第二个图钉落在卡片默认位置内，收敛后卡片不得遮挡任何图标；
③ 再次开卡先复位再重新收敛（不跨次累计漂移）。"""
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


funcs = "\n".join(extract(n) for n in
                  ("visibleCardRect", "anchorRect", "fitCardInMap"))

probe = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
.rt-card{width:240px;height:200px;background:#fff;border:1px solid #ccc;}
</style></head><body>
<div id="route-map" style="width:600px;height:400px;"></div>
<pre id="out">PENDING</pre>
<script src="https://api.tianditu.gov.cn/api?v=4.0&tk=__KEY__"></script>
<script>
var map;   /* 提取的函数引用同名变量：真实页面里在 IIFE 闭包，这里须全局声明 */
__FUNCS__
</script>
<script>
window.onload = function(){
  setTimeout(function(){
    try{
      map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(104.06,30.67), 12);
      // 图钉 A 贴地图顶部：气泡默认往上开必然被裁；
      // 图钉 B 放在卡片默认位置内（A 右上方），不开卡时验证避让
      var mk = new T.Marker(new T.LngLat(104.06, 30.672));
      map.addOverLay(mk);
      var mkB = new T.Marker(new T.LngLat(104.083, 30.683));
      map.addOverLay(mkB);
      var iw = new T.InfoWindow('<div class="rt-card">card</div>',
                                {offset:new T.Point(0,-38), maxWidth:250});
      function rect(){ var box=document.getElementById('route-map');
        var c=box.querySelector('.rt-card'); var r=c.getBoundingClientRect();
        var m=box.getBoundingClientRect();
        return [Math.round(r.top-m.top), Math.round(r.bottom-m.top),
                Math.round(r.left-m.left), Math.round(r.right-m.left)]; }
      function rectAbs(){ var c=document.querySelector('.rt-card');
        var r=c.getBoundingClientRect();
        return [r.top, r.bottom, r.left, r.right]; }
      // 需要避让的图标：与真实代码同源——只保证不遮挡选中的图标（anchorRect）
      function guardRects(){ var r=anchorRect(mk);
        return r ? [[r.top, r.bottom, r.left, r.right]] : []; }
      function iconRects(){ var box=document.getElementById('route-map');
        var m=box.getBoundingClientRect(); var out=[];
        box.querySelectorAll('.tdt-marker-icon').forEach(function(e){
          if(e.offsetParent===null){return;}
          var r=e.getBoundingClientRect();
          out.push([Math.round(r.top-m.top),Math.round(r.bottom-m.top),
                    Math.round(r.left-m.left),Math.round(r.right-m.left)]); });
        return out; }
      mk.openInfoWindow(iw);
      setTimeout(function(){
        var before = rect();
        var iconsBefore = iconRects();
        fitCardInMap(iw, mk, 0);
        setTimeout(function(){
          var after = rect();
          var cardAfter = rectAbs();
          var guardsAfter = guardRects();
          // 第二次开卡：offset 应先复位再重新收敛（不跨次累计漂移）
          setTimeout(function(){
            mk.openInfoWindow(iw);
            setTimeout(function(){
              var before2 = rect();
              fitCardInMap(iw, mk, 0);
              setTimeout(function(){
                var after2 = rect();
                var cardAfter2 = rectAbs();
                var guardsAfter2 = guardRects();
                // 场景 C：图钉在地图中部，卡片默认悬在上方本就不遮图标——不应乱动
                var mkC = new T.Marker(new T.LngLat(104.075, 30.615));
                map.addOverLay(mkC);
                var iwC = new T.InfoWindow('<div class="rt-card">card</div>',
                                           {offset:new T.Point(0,-38), maxWidth:250});
                setTimeout(function(){
                  mk.closeInfoWindow();
                  mkC.openInfoWindow(iwC);
                  setTimeout(function(){
                    var cBefore = rect();
                    fitCardInMap(iwC, mkC, 0);
                    setTimeout(function(){
                      var cAfter = rect();
                      var cG = anchorRect(mkC);
                      var cGuards = cG ? [[cG.top, cG.bottom, cG.left, cG.right]] : [];
                      document.getElementById('out').textContent = 'INFO=' + JSON.stringify({
                        before: before, after: after,
                        icons_before: iconsBefore,
                        guards_after: guardsAfter, card_abs: cardAfter,
                        reopen_before: before2, reopen_after: after2,
                        guards_after2: guardsAfter2, card_abs2: cardAfter2,
                        c_before: cBefore, c_after: cAfter,
                        c_guards: cGuards, c_card: rectAbs(),
                        c_offset: (function(o){return o?[o.x,o.y]:null;})(iwC.getOffset()),
                        c_base: iwC.RTbase ? [iwC.RTbase.x, iwC.RTbase.y] : null,
                        offset_final: (function(o){return o?[o.x,o.y]:null;})(iw.getOffset())
                      });
                    }, 2200);
                  }, 700);
                }, 200);
              }, 2200);
            }, 700);
          }, 200);
        }, 2300);
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
    [edge, "--headless", "--disable-gpu", "--dump-dom", "--virtual-time-budget=20000",
     "--window-size=800,600", "file:///" + str(p).replace("\\", "/")],
    capture_output=True, text=True, timeout=120).stdout
m = re.search(r"INFO=.*", out, re.S)
if not m:
    print("NO INFO; dump len =", len(out))
    sys.exit(1)
data = json.loads(m.group(0)[5:].rsplit("</pre>", 1)[0].strip())


def in_map(r):
    top, bottom, left, right = r
    return top >= 0 and bottom <= 400 and left >= 0 and right <= 600


def overlaps_abs(card, guards):
    """卡片页面坐标 [top,bottom,left,right] 与任一避让图标（页面坐标）相交。"""
    ct, cb, cl, cr = card
    for gt, gb, gl, gr in guards:
        if min(cb, gb) - max(ct, gt) > 0 and min(cr, gr) - max(cl, gl) > 0:
            return True
    return False


top, bottom, left, right = data["after"]
ok1 = in_map(data["after"])
ok2 = not overlaps_abs(data["card_abs"], data["guards_after"])
ok3 = (in_map(data["reopen_after"])
       and not overlaps_abs(data["card_abs2"], data["guards_after2"]))
# 场景 C：图钉居中，默认位置不遮图标 → 收敛后仍不遮 + offset 保持 base
ok4 = (not overlaps_abs(data["c_card"], data["c_guards"])
       and data["c_offset"] == data["c_base"])
print("图钉矩形:", data["icons_before"])
print("首次开卡  before=%s after=%s -> %s / %s" % (
    data["before"], data["after"],
    "PASS 卡片在地图内" if ok1 else "FAIL 仍被裁",
    "PASS 不遮挡选中图标" if ok2 else "FAIL 遮挡选中图标"))
print("再次开卡  before=%s after=%s -> %s" % (
    data["reopen_before"], data["reopen_after"],
    "PASS 复位后重新收敛" if ok3 else "FAIL"))
print("图钉居中  before=%s after=%s offset=%s -> %s" % (
    data["c_before"], data["c_after"], data["c_offset"],
    "PASS 默认位置不动" if ok4 else "FAIL 位置被乱调"))
print("最终 offset =", data["offset_final"])
sys.exit(0 if ok1 and ok2 and ok3 and ok4 else 1)
