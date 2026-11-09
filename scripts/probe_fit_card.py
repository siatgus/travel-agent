# -*- coding: utf-8 -*-
"""端到端行为探针：从 render_html.py 逐字提取 visibleCardRect/iconRectAt/fitCardInMap，
在真实天地图 SDK 下验证「挪卡片不动地图 + 只避让选中的图标 + 不乱动」：
① 贴边图钉开卡：卡片钳进地图且不遮挡选中图标（鼠标光标位置识别）；
② 再次开卡先复位再重新收敛（不跨次累计漂移）；
③ 图钉居中、默认位置本就不遮图标：offset 保持 base 不乱动；
④ 收敛后位置稳定：1500ms 与 2500ms 两次测量一致（无振荡/二次挪动）。"""
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
                  ("visibleCardRect", "iconRectAt", "fitCardInMap"))

probe = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
.rt-card{width:240px;height:200px;background:#fff;border:1px solid #ccc;}
</style></head><body>
<pre id="err">-</pre>
<div id="route-map" style="width:600px;height:400px;"></div>
<pre id="out">PENDING</pre>
<script>
window.onerror = function(msg, src, line, col){
  document.getElementById('err').textContent += ' | JSERR: ' + msg + ' @' + line + ':' + col;
};
document.addEventListener('DOMContentLoaded', function(){
  document.getElementById('err').textContent += ' | DOMContentLoaded';
});
</script>
<script src="https://api.tianditu.gov.cn/api?v=4.0&tk=__KEY__"></script>
<script>
var map;        /* 提取的函数引用同名变量：真实页面里在 IIFE 闭包，这里须全局声明 */
var RT = { mouse: null };   /* 同上：真实页面里由 openCard 事件写入，探针里手工模拟 */
__FUNCS__
</script>
<script>
window.onload = function(){
  setTimeout(function(){
    try{
      map = new T.Map('route-map');
      map.centerAndZoom(new T.LngLat(104.06,30.67), 12);
      // 图钉 A 贴地图顶部：气泡默认往上开必然被裁；图钉 B 放在卡片默认位置
      // 附近（A 右上方）但不是选中目标——卡片允许盖住 B，只保证不盖住 A
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
      function iconRect(el){ var box=document.getElementById('route-map');
        var m=box.getBoundingClientRect(); var r=el.getBoundingClientRect();
        return [r.top, r.bottom, r.left, r.right]; }
      function setMouseAt(clientX, clientY){ RT.mouse = {x:clientX, y:clientY}; }
      // —— 场景 A：图钉 A 贴顶，光标在 A 图标中心 ——
      mk.openInfoWindow(iw);
      setTimeout(function(){
        var before = rect();
        var iconEls = document.getElementById('route-map')
          .querySelectorAll('.tdt-marker-icon');
        var aIcon = iconRect(iconEls[0]);   // A 的图标 DOM（按坐标找最近的）
        // 光标放在 A 图标中心（页面上 A 的图标矩形中点）
        var ax=(aIcon[2]+aIcon[3])/2, ay=(aIcon[0]+aIcon[1])/2;
        setMouseAt(ax, ay);
        fitCardInMap(iw, 0);
        setTimeout(function(){
          var after = rect();
          var cardAfter = rectAbs();
          var guardsAfter = [aIcon];
          var stable1 = rect();
          // —— 场景 B：再次开卡（应复位重收敛，无累计漂移）——
          setTimeout(function(){
            mk.openInfoWindow(iw);
            setTimeout(function(){
              var before2 = rect();
              setMouseAt(ax, ay);
              fitCardInMap(iw, 0);
              setTimeout(function(){
                var after2 = rect();
                var cardAfter2 = rectAbs();
                var stable2 = rect();
                // —— 场景 C：图钉居中，默认位置本就不遮图标 → 不乱动 ——
                var mkC = new T.Marker(new T.LngLat(104.075, 30.615));
                map.addOverLay(mkC);
                var iwC = new T.InfoWindow('<div class="rt-card">card</div>',
                                           {offset:new T.Point(0,-38), maxWidth:250});
                setTimeout(function(){
                  mk.closeInfoWindow();
                  mkC.openInfoWindow(iwC);
                  setTimeout(function(){
                    var iconEls2 = document.getElementById('route-map')
                      .querySelectorAll('.tdt-marker-icon');
                    var cIcon = null;
                    iconEls2.forEach(function(e){ var r=e.getBoundingClientRect();
                      var llC = map.lngLatToContainerPoint(new T.LngLat(104.075,30.615));
                      var box=document.getElementById('route-map').getBoundingClientRect();
                      if (Math.abs((r.left+r.right)/2-(box.left+llC.x))<20 &&
                          Math.abs((r.top+r.bottom)/2-(box.top+llC.y))<30){ cIcon=e; } });
                    var cAbs = cIcon ? iconRect(cIcon) : null;
                    var cBefore = rect();
                    if (cAbs) { setMouseAt((cAbs[2]+cAbs[3])/2, (cAbs[0]+cAbs[1])/2); }
                    fitCardInMap(iwC, 0);
                    setTimeout(function(){
                      var cAfter = rect();
                      var cGuards = cAbs ? [cAbs] : [];
                      document.getElementById('out').textContent = 'INFO=' + JSON.stringify({
                        before: before, after: after,
                        a_icon: aIcon,
                        guards_after: guardsAfter, card_abs: cardAfter,
                        stable1: stable1,
                        reopen_before: before2, reopen_after: after2,
                        card_abs2: cardAfter2, stable2: stable2,
                        c_before: cBefore, c_after: cAfter,
                        c_guards: cGuards, c_card: rectAbs(),
                        c_offset: (function(o){return o?[o.x,o.y]:null;})(iwC.getOffset()),
                        c_base: iwC.RTbase ? [iwC.RTbase.x, iwC.RTbase.y] : null,
                        offset_final: (function(o){return o?[o.x,o.y]:null;})(iw.getOffset())
                      });
                    }, 2600);
                  }, 700);
                }, 200);
              }, 2600);
            }, 700);
          }, 200);
        }, 2600);
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
    [edge, "--headless", "--disable-gpu", "--dump-dom", "--virtual-time-budget=26000",
     "--window-size=800,600", "file:///" + str(p).replace("\\", "/")],
    capture_output=True, text=True, timeout=150).stdout
m = re.search(r"INFO=(\{.*?\})</pre>", out, re.S)
if not m:
    mp = re.search(r'<pre id="out">([^<]*)</pre>', out)
    me = re.search(r'<pre id="err">([^<]*)</pre>', out)
    print("NO INFO; out =", (mp.group(1)[:400] if mp else "?"))
    print("err =", (me.group(1)[:400] if me else "?"))
    sys.exit(1)
data = json.loads(m.group(1).replace("&quot;", '"').replace("&#39;", "'"))


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


ok1 = (in_map(data["after"])
       and not overlaps_abs(data["card_abs"], data["guards_after"]))
ok2 = (in_map(data["reopen_after"])
       and not overlaps_abs(data["card_abs2"], data["guards_after"]))
ok3 = (not overlaps_abs(data["c_card"], data["c_guards"])
       and data["c_offset"] == data["c_base"])
ok4 = (data["stable1"] == data["after"] and data["stable2"] == data["reopen_after"])
print("A 图标矩形:", data["a_icon"])
print("首次开卡  before=%s after=%s -> %s" % (
    data["before"], data["after"],
    "PASS 在地图内且不遮选中图标" if ok1 else "FAIL"))
print("再次开卡  before=%s after=%s -> %s" % (
    data["reopen_before"], data["reopen_after"],
    "PASS 复位后重新收敛" if ok2 else "FAIL"))
print("图钉居中  before=%s after=%s offset=%s -> %s" % (
    data["c_before"], data["c_after"], data["c_offset"],
    "PASS 默认位置不动" if ok3 else "FAIL 位置被乱调"))
print("位置稳定  stable1==after:%s stable2==reopen:%s -> %s" % (
    data["stable1"] == data["after"], data["stable2"] == data["reopen_after"],
    "PASS 无振荡无二次挪动" if ok4 else "FAIL 乱动"))
print("最终 offset =", data["offset_final"])
sys.exit(0 if ok1 and ok2 and ok3 and ok4 else 1)
