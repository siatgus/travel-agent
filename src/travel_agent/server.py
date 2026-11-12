"""本地部署入口：把对话状态机包成零依赖的本地 Web 服务。

用法：
    python server.py                 # 默认 127.0.0.1:8787
    python server.py --port 9000
    python server.py --open          # 启动后自动打开浏览器

设计取舍：
- 只用标准库 http.server，pip 依赖为零，任何装了 Python 3.10+ 的机器双击即跑。
- 默认绑 0.0.0.0：手机连同一 Wi-Fi 即可访问（数据按账号隔离，/api/chat 等全部
  要 Bearer token）；只想本机用可 --host 127.0.0.1。管理后台仍只绑本机。
- 会话在进程内存（sid -> Session），但每轮对话自动存档到 SQLite
  （conversations / conv_messages 表，与账号库同文件）：重启后按存档续聊，
  登录用户可在「历史行程」里回看每次对话的完整内容并继续。
- 上下文范围 = 用户产生的一次对话（一个 sid 一个 Session），跨对话不串味；
  跨会话的硬约束偏好（不吃辣等）持久化到 data/.user_prefs.json（PreferenceMemory）。
- /api/chat 每次调用持全局锁：编排器会打真实 API（天气/OSRM），并发编排没有收益，
  还会让 OSRM 触发限流。

接口：
    GET  /                 聊天页面
    GET  /api/health       存活检查
    POST /api/auth/send_code {"phone"} 发验证码（演示模式直接回显）
    POST /api/auth/login   {"phone","code"} 验证码登录，首登自动注册+赠送积分
    POST /api/auth/refresh {"refresh"} 换新 access token
    GET  /api/me           当前用户与积分余额（需 Bearer token）
    POST /api/chat         {"sid","message"} 需登录；LLM 调用按 token 计积分；
                           每轮自动存档（首轮建档 = 自动保存为一次行程）
    GET  /api/conversations         历史行程列表（需登录）
    GET  /api/conversations/{sid}   一次对话的完整内容（需登录）
    DELETE /api/conversations/{sid}   删除一次对话（需登录）
    POST /api/reset        {"sid"} 彻底删除该会话（内存 + 存档 + 行程页）
    GET  /plan/{sid}       该会话最近一次方案的完整 HTML 行程页（内存没有就从库里捞）
    GET  /api/lan          手机互通：返回局域网访问地址（供顶栏扫码弹窗用）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import auth
from . import conversations
from . import llm as llm_layer
from . import paths
from . import progress
from .dialogue import Session, PreferenceMemory
from .render_html import build_html, get_tdt_key

HOST = "127.0.0.1"          # 本机自访问地址（探测占用、默认展示）；对外绑定见 main()

_LAN_URL = "http://127.0.0.1:8787/"    # 手机互通地址，main() 启动时按绑定情况重算

_LOCK = threading.Lock()
_SESSIONS: dict[str, Session] = {}
_LAST_PLAN: dict[str, tuple[dict, str]] = {}   # sid -> (plan result, utterance)
_DRIVE_CACHE: dict[str, dict] = {}             # "orig|dest" -> 规划结果（路径基本不变）
_PREFETCHED: set[str] = set()                  # 已启动过预热的 sid，防重复
_MEM: PreferenceMemory | None = None
_LLM = "unset"                                  # 延迟初始化的 LLM 客户端（或 None）
_LLM_SIG = None                                 # 设置文件指纹 (mtime_ns, size)，变更即重建客户端


def _lan_ip() -> str:
    """本机在局域网里的地址，供手机扫码访问。

    枚举本机网卡地址，只认真实私网段（192.168 / 10 / 172.16-31）；
    UDP connect 选路由只作兜底——代理 TUN 网卡会给出 198.18.x 假地址
    （实测本机踩过），必须剔除，否则二维码扫出来根本打不开。
    """
    import socket
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        cands = [i[4][0] for i in infos]
    except OSError:
        cands = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))       # UDP connect 不产生流量，仅选一次路由
        cands.append(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    def usable(ip: str) -> bool:
        parts = ip.split(".")
        if len(parts) != 4 or ip.startswith(("127.", "169.254.", "198.18.", "198.19.")):
            return False
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            return False
        return a == 192 and b == 168 or a == 10 or (a == 172 and 16 <= b <= 31)

    for ip in cands:
        if usable(ip):
            return ip
    return cands[0] if cands else "127.0.0.1"


def _ok_pt(s: str) -> str | None:
    """坐标串 → 舍入到 6 位的 "lon,lat"，非法/越界返回 None。"""
    parts = str(s).split(",")
    if len(parts) != 2:
        return None
    try:
        lon, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return "%s,%s" % (round(lon, 6), round(lat, 6))


def drive_fetch(o: str, d: str) -> dict:
    """天地图驾车规划：缓存命中直接返回，否则打 HTTP 上游并解析点串。

    供 /api/tianditu/drive 与预热线程共用——预热让浏览器 fetch 时毫秒级
    命中缓存，不再受用户浏览器网络时序摆布（实测：无预热时真实浏览器
    每段 fetch 4~10s，7s 超时一碰就全线回落直线，用户看到的就是直线）。
    """
    cache_key = o + "|" + d
    hit = _DRIVE_CACHE.get(cache_key)
    if hit is not None:
        return hit
    key = get_tdt_key()
    if not key:
        return {"ok": False, "error": "no tdt key"}
    post = json.dumps({"orig": o, "dest": d, "style": "0"},
                      separators=(",", ":"))
    url = ("http://api.tianditu.gov.cn/drive?postStr="
           + urllib.parse.quote(post, safe="")
           + "&type=search&tk=" + urllib.parse.quote(key))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "upstream %s" % e}
    pts = []
    m = re.search(r"<routelatlon>([^<]*)</routelatlon>", body)
    if m:
        for pair in m.group(1).split(";"):
            p = _ok_pt(pair.strip())
            if p:
                a, b = p.split(",")
                pts.append([float(a), float(b)])
    if len(pts) < 2:
        return {"ok": False, "error": "no route"}
    if len(_DRIVE_CACHE) > 500:
        _DRIVE_CACHE.clear()
    out = {"ok": True, "pts": pts}
    _DRIVE_CACHE[cache_key] = out
    return out


def _drive_pairs(result: dict) -> list[tuple[str, str]]:
    """一份方案要预热的路段：每天取有坐标且非位置估算的节点相邻成对，
    与前端 rec.chain 构建规则一致（不一致缓存键就对不上，预热白做）。"""
    pairs: list[tuple[str, str]] = []
    for d in result.get("days_plan") or []:
        chain = [n["geo"] for n in (d.get("nodes") or [])
                 if n.get("geo") and not n.get("geo_uncertain")]
        for a, b in zip(chain, chain[1:]):
            oa, ob = _ok_pt(f"{a[1]},{a[0]}"), _ok_pt(f"{b[1]},{b[0]}")
            if oa and ob:
                pairs.append((oa, ob))
    return pairs


def prefetch_drive_routes(sid: str, result: dict) -> None:
    """后台预热一份方案的所有动线路段，让前端 fetch 全部命中缓存。

    串行 + 0.15s 间隔防天地图 QPS 限制。daemon 线程，失败只留空缓存，
    前端按原逻辑回落直线——预热只加速，不改变任何绘制行为。
    """
    if sid in _PREFETCHED:
        return
    _PREFETCHED.add(sid)
    if len(_PREFETCHED) > 300:
        _PREFETCHED.clear()
    pairs = [p for p in _drive_pairs(result) if (p[0] + "|" + p[1]) not in _DRIVE_CACHE]

    def _run():
        for oa, ob in pairs:
            drive_fetch(oa, ob)
            time.sleep(0.15)

    threading.Thread(target=_run, daemon=True, name="drive-prefetch").start()


def _get_llm():
    """模型客户端：管理后台设置文件优先，环境变量兜底；后台改配置即刻生效。"""
    global _LLM, _LLM_SIG
    from . import llm as llm_layer
    try:
        st = llm_layer.settings_path().stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None
    if _LLM == "unset" or sig != _LLM_SIG:
        _LLM_SIG = sig
        _LLM = llm_layer.from_settings()       # 没配好返回 None，调用方静默降级
        if _LLM is not None:
            print(f"[llm] 已接入真实模型：{_LLM.model} @ {_LLM.base_url}")
        else:
            print("[llm] 未配置模型（管理后台“模型设置”或环境变量 OPENAI_API_KEY），使用规则抽取")
    return _LLM


def _llm_state() -> dict:
    """供 /api/health 与后台展示：本进程此刻实际在用的模型（不含 Key）。"""
    c = _get_llm()
    info = llm_layer.active_info()
    return {"enabled": c is not None,
            "model": getattr(c, "model", "") or info.get("model", ""),
            "base_url": getattr(c, "base_url", "") or info.get("base_url", ""),
            "provider": info.get("provider", ""),
            "source": "settings" if c is not None and info.get("source") == "settings"
                      else ("env" if c is not None else ""),
            "key_masked": info.get("key_masked", "")}


def _get_memory() -> PreferenceMemory:
    """跨会话偏好档案，全服务共享一份（本地单用户）。"""
    global _MEM
    if _MEM is None:
        try:
            _MEM = PreferenceMemory(paths.user_prefs())
        except OSError:                       # 只读目录（.pyz）→ 退到工作目录
            _MEM = PreferenceMemory(Path.cwd() / ".user_prefs.json")
    return _MEM


def _metered(uid: int):
    """给用户包一层计量客户端：LLM 每次调用按 token 预扣-结算积分。"""
    base = _get_llm()
    return llm_layer.MeteredClient(base, uid, meter=auth) if base is not None else None


def _get_session(uid: int, sid: str) -> Session:
    key = f"u{uid}:{sid}"
    s = _SESSIONS.get(key)
    if s is None:
        s = Session(client=_metered(uid), memory=_get_memory())
        snap = conversations.get_state(uid, sid)
        if snap:                        # 重启/换设备后按存档续聊，上下文不断
            s.restore(snap)
        _SESSIONS[key] = s
        return s
    inner = getattr(s.client, "_inner", None)
    if inner is not _LLM:                      # 后台换模型后，老会话也换新客户端
        s.client = _metered(uid)
    return s


# ---------------------------------------------------------------- 聊天页面

PAGE_SRC = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>晴航 · 智能旅行规划助手</title>
<style>
  :root{--bg:#f7f9ff;--card:#fff;--ink:#1d2333;--dim:#7a8299;--line:#e6eaf5;
        --blue:#3d7bff;--blue-deep:#2b5fe0;--coral:#ff7a59;--mint:#2ec4a5;
        --grad:linear-gradient(105deg,#3d7bff 0%,#7a5cff 60%,#b44df0 100%);
        --brand:var(--blue);--brand-ink:#fff;--user:#3d7bff;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column}
  button{font-family:inherit;cursor:pointer}
  /* ---------- 顶栏 ---------- */
  header{background:var(--card);border-bottom:1px solid var(--line);
         padding:0 20px;height:60px;display:flex;align-items:center;gap:12px;flex:none}
  header .logo{width:34px;height:34px;border-radius:10px;background:var(--grad);
         color:#fff;display:flex;align-items:center;justify-content:center;font-size:17px;
         box-shadow:0 6px 16px rgba(122,92,255,.28)}
  header h1{font-size:15.5px;font-weight:700}
  header .sub{font-size:12px;color:var(--dim)}
  header button{border:1.5px solid var(--line);background:var(--card);
         border-radius:10px;padding:7px 14px;font-size:13px;cursor:pointer;color:var(--ink)}
  header button:hover{border-color:var(--blue);color:var(--blue-deep)}
  header #btn-new{border:none;background:var(--grad);color:#fff;font-weight:600;
         box-shadow:0 6px 18px rgba(122,92,255,.28)}
  header #btn-new:hover{filter:brightness(1.06);color:#fff}
  /* ---------- 聊天区 ---------- */
  #chat{flex:1;overflow-y:auto;padding:22px;display:flex;flex-direction:column;gap:15px}
  .msg{max-width:76%;border-radius:18px;padding:13px 17px;font-size:14px;line-height:1.75;
       white-space:pre-wrap;word-break:break-word}
  .msg.user{align-self:flex-end;background:var(--grad);color:#fff;
       border-bottom-right-radius:6px;box-shadow:0 6px 18px rgba(122,92,255,.22)}
  .msg.bot{align-self:flex-start;background:var(--card);border:1px solid var(--line);
       border-bottom-left-radius:6px;box-shadow:0 6px 18px rgba(29,35,51,.05)}
  .msg.bot h3{font-size:14.5px;margin:8px 0 6px;color:var(--blue-deep);
       display:flex;align-items:center;gap:7px}
  .msg.bot h3::before{content:'';width:8px;height:8px;border-radius:50%;
       background:var(--grad);flex:none}
  .msg.bot table{border-collapse:collapse;margin:8px 0;font-size:13px;white-space:normal;
       border-radius:10px;overflow:hidden}
  .msg.bot th,.msg.bot td{border:1px solid var(--line);padding:5px 11px;text-align:left}
  .msg.bot th{background:#eef4ff;color:var(--blue-deep)}
  .msg.bot blockquote{border-left:3px solid var(--blue);padding:4px 12px;margin:6px 0;
       color:var(--dim);font-size:13px;background:#f2f6ff;border-radius:0 10px 10px 0}
  .msg.bot code{background:#eef4ff;border-radius:5px;padding:1px 6px;font-size:13px;
       color:var(--blue-deep)}
  .meta{font-size:12px;color:var(--dim);margin-top:7px}
  .msg.bot>*{animation:fin .3s ease}
  @keyframes fin{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
  .plan-link{display:inline-block;margin-top:9px;background:var(--grad);color:#fff;
       text-decoration:none;border-radius:10px;padding:8px 18px;font-size:13px;font-weight:600;
       box-shadow:0 8px 20px rgba(122,92,255,.3)}
  .plan-link:hover{filter:brightness(1.06)}
  /* ---------- 行程面板 ---------- */
  #main{flex:1;display:flex;min-height:0;width:100%}
  #board{width:460px;flex:none;border-left:1px solid var(--line);background:var(--card);
       display:none;flex-direction:column;min-height:0}
  #board.on{display:flex}
  #bhead{padding:14px 18px 10px;border-bottom:1px solid var(--line)}
  #btitle{font-size:15px;font-weight:700;display:flex;justify-content:space-between;
       align-items:center;gap:8px}
  #btitle .bsub{font-size:12px;color:var(--dim);font-weight:400}
  #btabs{display:flex;gap:6px;overflow-x:auto;padding:10px 18px;flex-wrap:wrap}
  #btabs button{border:1.5px solid var(--line);background:#fff;border-radius:999px;
       padding:4px 13px;font-size:12.5px;cursor:pointer;color:var(--ink);white-space:nowrap}
  #btabs button.act{background:var(--grad);color:#fff;border-color:transparent;font-weight:600}
  #bbody{flex:1;overflow-y:auto;padding:14px 18px}
  .bday{margin-bottom:15px}
  .bday h4{font-size:13.5px;color:var(--blue-deep);margin-bottom:7px;display:flex;gap:8px;
       align-items:baseline;flex-wrap:wrap}
  .bday h4 .wx{font-weight:400;color:var(--dim);font-size:12px}
  .bcard{border:1px solid var(--line);border-radius:13px;padding:8px 10px;background:#fbfcff}
  .bnode{display:flex;align-items:center;gap:8px;padding:8px 9px;border-radius:10px;
       cursor:pointer}
  .bnode:hover{background:#eef4ff}
  .bnode .tm{font-size:12px;color:var(--dim);width:42px;flex:none}
  .bnode .ic{font-size:14px;flex:none}
  .bnode .nm{font-size:13.5px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
       white-space:nowrap}
  .bnode .mn{font-size:11px;color:var(--dim);flex:none}
  .bsec{margin:8px 0}
  .bsec h4{font-size:14px;margin-bottom:8px}
  .bnote{font-size:12px;color:var(--dim);line-height:1.7}
  .bitem{font-size:13px;line-height:1.9;border-bottom:1px dashed var(--line);padding:5px 2px}
  .bitem:last-child{border-bottom:none}
  .bempty{color:var(--dim);font-size:12.5px;text-align:center;padding:30px 10px;line-height:1.9}
  #bchips{padding:10px 18px;border-top:1px solid var(--line);display:flex;gap:6px;flex-wrap:wrap}
  #bchips button{border:1.5px solid var(--line);background:#fff;border-radius:999px;
       padding:4px 13px;font-size:12.5px;color:var(--ink)}
  #bchips button:hover{border-color:var(--blue);color:var(--blue-deep);background:#eef4ff}
  #bmenu{position:fixed;z-index:25;background:var(--card);border:1px solid var(--line);
       border-radius:14px;box-shadow:0 14px 40px rgba(29,35,51,.16);padding:6px;display:none;
       min-width:225px}
  #bmmask{position:fixed;inset:0;background:rgba(29,35,51,.35);display:none;z-index:17}
  #bmenu button{display:block;width:100%;text-align:left;border:none;background:none;
       padding:9px 12px;font-size:13px;border-radius:9px;cursor:pointer;color:var(--ink);
       white-space:nowrap}
  #bmenu button:hover{background:#eef4ff;color:var(--blue-deep)}
  /* 行程单抽屉（手机端）：遮罩/悬浮按钮/关闭键桌面端一律隐藏 */
  #boardmask{position:fixed;inset:0;background:rgba(29,35,51,.35);z-index:19;display:none}
  #board-fab{display:none;position:fixed;right:14px;bottom:88px;z-index:12;
       border:none;background:var(--grad);color:#fff;border-radius:999px;
       padding:10px 18px;font-size:13px;font-weight:600;cursor:pointer;
       box-shadow:0 10px 26px rgba(122,92,255,.35);align-items:center;gap:6px}
  #board-fab:active{filter:brightness(1.08)}
  #bclose{display:none;border:none;background:#f1f4fb;color:var(--dim);
       width:26px;height:26px;border-radius:8px;font-size:13px;cursor:pointer;
       flex:none;line-height:1}
  #bclose:hover{color:var(--ink)}
  @media(max-width:920px){#board{width:380px}}
  /* ---------- 手机访问弹窗（扫码互通） ---------- */
  #qrmask{position:fixed;inset:0;background:rgba(29,35,51,.4);display:none;
       align-items:center;justify-content:center;z-index:40}
  #qrmask .card{background:var(--card);border-radius:20px;padding:24px 26px;width:360px;
       max-width:92vw;box-shadow:0 24px 64px rgba(29,35,51,.24);text-align:center}
  #qrmask h2{font-size:16px;margin-bottom:8px}
  #qrmask .hint{font-size:12.5px;color:var(--dim);line-height:1.7;margin-bottom:14px;
       text-align:left}
  #qr-img{min-height:200px}
  #qr-img svg{width:190px;height:190px;display:block;margin:0 auto 12px}
  #qrmask .urlrow{display:flex;gap:8px;align-items:center;margin-bottom:16px}
  #qrmask code{flex:1;min-width:0;background:#eef4ff;color:var(--blue-deep);
       border-radius:9px;padding:9px 10px;font-size:12.5px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap}
  #qrmask .urlrow button{flex:none;border:1.5px solid var(--line);background:var(--card);
       border-radius:9px;padding:8px 12px;font-size:12.5px;cursor:pointer;color:var(--ink)}
  #qrmask .urlrow button:hover{border-color:var(--blue);color:var(--blue-deep)}
  #qrmask .btnrow button{border:1.5px solid var(--line);background:var(--card);
       border-radius:11px;padding:8px 20px;font-size:13.5px;cursor:pointer;color:var(--ink)}
  #qrmask .btnrow button:hover{border-color:var(--blue);color:var(--blue-deep)}
  /* ---------- 输入区 ---------- */
  .typing{align-self:flex-start;color:var(--dim);font-size:13px;padding:11px 16px;
       background:var(--card);border:1px solid var(--line);border-radius:18px;
       border-bottom-left-radius:6px;display:flex;align-items:center;gap:8px}
  .tdot{width:6px;height:6px;border-radius:50%;background:var(--blue);
       display:inline-block;animation:tb 1.2s infinite}
  .tdot:nth-child(2){animation-delay:.2s}
  .tdot:nth-child(3){animation-delay:.4s}
  @keyframes tb{0%,80%,100%{opacity:.25}40%{opacity:1}}
  #bar{background:var(--card);border-top:1px solid var(--line);padding:12px 20px}
  #row{display:flex;gap:10px}
  #inp{flex:1;border:2px solid var(--line);border-radius:16px;padding:11px 15px;
       font-size:14px;font-family:inherit;resize:none;outline:none;background:#fbfcff}
  #inp:focus{border-color:var(--blue)}
  #send{border:none;background:var(--grad);color:#fff;border-radius:16px;
       padding:0 24px;font-size:14px;font-weight:600;
       box-shadow:0 6px 18px rgba(122,92,255,.28)}
  #send:hover{filter:brightness(1.06)}
  #send:disabled{opacity:.5;cursor:default}
  #foot{text-align:center;font-size:11px;color:var(--dim);padding:6px 0 10px}
  /* ---------- 顶栏账号 ---------- */
  .userwrap{margin-left:auto;display:flex;align-items:center;gap:8px}
  .userbox{font-size:13px;color:var(--blue-deep);cursor:default;user-select:none;
       border:1.5px solid #d8e3ff;border-radius:999px;padding:5px 14px;background:#eef4ff;
       display:inline-flex;align-items:center;gap:6px}
  .userbox .sep{color:var(--dim)}
  .userbox .cr{font-weight:700}
  .quit{border:1.5px solid var(--line);border-radius:999px;padding:5px 13px;
       font-size:12.5px;color:var(--dim);cursor:pointer;background:var(--card);
       transition:color .12s,border-color .12s}
  .quit:hover{border-color:#ffc4b3;color:#e05c3d}
  #btn-hist{position:relative}
  /* ---------- 退出确认 ---------- */
  #logoutmask{position:fixed;inset:0;background:rgba(29,35,51,.4);display:none;
       align-items:center;justify-content:center;z-index:40}
  #logoutmask .card{background:var(--card);border-radius:20px;
       padding:24px 26px;width:320px;box-shadow:0 24px 64px rgba(29,35,51,.24)}
  #logoutmask h2{font-size:16px;margin-bottom:8px}
  #logoutmask .hint{font-size:12.5px;color:var(--dim);line-height:1.7;margin-bottom:18px}
  #logoutmask .btnrow{display:flex;gap:8px;justify-content:flex-end}
  #logoutmask button{border:1.5px solid var(--line);background:var(--card);border-radius:11px;
       padding:8px 16px;font-size:13.5px;cursor:pointer;color:var(--ink)}
  #logoutmask button:hover{border-color:var(--blue);color:var(--blue-deep)}
  #logoutmask .danger{background:var(--coral);border-color:var(--coral);color:#fff}
  #logoutmask .danger:hover{background:#f06540;border-color:#f06540;color:#fff}
  /* ---------- 登录页（整页） ---------- */
  /* align-items:flex-start 是手机端能否滚动的关键：showLogin 设 display:flex 后
     .lg-inner 会被默认 stretch 钳在视口高，超出内容被它自己的 overflow:hidden
     剪掉，#login 看不到溢出就永远滚不动（手机端实测踩过）。flex-start 让内层
     被内容撑高，短内容时仍有 min-height:100% 兜底铺满。 */
  #login{position:fixed;inset:0;z-index:30;display:none;overflow-y:auto;
       -webkit-overflow-scrolling:touch;align-items:flex-start;
       background:linear-gradient(180deg,#dcebff 0%,#f3e9ff 55%,#fff2e8 100%)}
  #login .lg-inner{min-height:100%;width:100%;display:flex;flex-direction:column;
       position:relative;overflow:hidden}
  #login .cloud{position:absolute;background:rgba(255,255,255,.75);border-radius:999px;
       filter:blur(1px);animation:drift 30s linear infinite;z-index:0}
  #login .cloud::before,#login .cloud::after{content:'';position:absolute;background:inherit;
       border-radius:50%}
  #login .cloud::before{width:60%;height:160%;left:12%;top:-70%}
  #login .cloud::after{width:42%;height:120%;right:12%;top:-46%}
  @keyframes drift{from{transform:translateX(-16vw)}to{transform:translateX(110vw)}}
  #login .lg-nav{display:flex;align-items:center;gap:12px;padding:22px 5vw;z-index:1}
  #login .lg-nav .logo{width:42px;height:42px;border-radius:13px;background:var(--grad);
       color:#fff;display:flex;align-items:center;justify-content:center;font-size:21px;
       box-shadow:0 8px 22px rgba(61,123,255,.35)}
  #login .lg-nav b{font-size:19px}
  #login .lg-main{flex:1;display:flex;align-items:center;justify-content:space-between;
       padding:10px 7vw 50px;gap:40px;position:relative;z-index:1;flex-wrap:wrap}
  #login .lg-copy{flex:1;min-width:280px}
  #login .lg-copy .tag{display:inline-block;background:#fff;border:1px solid var(--line);
       color:var(--blue);border-radius:999px;padding:7px 16px;font-size:13px;font-weight:600;
       box-shadow:0 4px 14px rgba(61,123,255,.12)}
  #login .lg-copy h1{font-size:clamp(28px,3.6vw,44px);font-weight:800;line-height:1.35;
       margin:20px 0 14px}
  #login .lg-copy h1 .hl{background:var(--grad);-webkit-background-clip:text;
       background-clip:text;color:transparent}
  #login .lg-copy p{color:var(--dim);font-size:15px;line-height:1.9;max-width:26em}
  #login .lg-cards{margin-top:28px;display:flex;gap:13px;flex-wrap:wrap}
  #login .mini{background:#fff;border:1px solid var(--line);border-radius:16px;
       padding:13px 17px;box-shadow:0 8px 22px rgba(29,35,51,.07);font-size:13.5px}
  #login .mini b{display:block;font-size:14.5px;margin-bottom:3px}
  #login .mini small{color:var(--dim)}
  #login .lg-panel{flex:none;width:410px;max-width:94vw;background:#fff;border-radius:24px;
       padding:32px 30px;box-shadow:0 30px 80px rgba(61,123,255,.18)}
  #login h2{font-size:22px;margin-bottom:6px}
  #login .hint{font-size:12.5px;color:var(--dim);line-height:1.6}
  #login .lg-panel>.hint{margin-bottom:22px}
  #login .f-label{font-size:13px;color:var(--dim);margin-bottom:7px}
  #login .row2{display:flex;gap:10px;margin-bottom:14px}
  #login input{width:100%;border:2px solid var(--line);border-radius:14px;
       padding:12px 15px;font-size:14.5px;outline:none;background:#fbfcff;
       font-family:inherit;margin-bottom:0;box-sizing:border-box}
  #login input:focus{border-color:var(--blue)}
  #login .row2 input{margin-bottom:0}
  #login .row2 button{flex:none;border:2px solid var(--blue);color:var(--blue);
       background:#eef4ff;border-radius:14px;padding:0 15px;font-size:13px;cursor:pointer;
       white-space:nowrap;display:flex;align-items:center;justify-content:center;line-height:1}
  #login .row2 button:disabled{color:var(--dim);border-color:var(--line);
       background:var(--card);cursor:default}
  #login .pri{width:100%;border:none;background:var(--grad);color:#fff;
       border-radius:14px;padding:14px 0;font-size:15.5px;font-weight:600;cursor:pointer;
       box-shadow:0 12px 30px rgba(122,92,255,.32)}
  #login .pri:hover{filter:brightness(1.06)}
  #login .pri:disabled{opacity:.5}
  #login .ghost{width:100%;margin-top:10px;border:1.5px solid var(--line);background:transparent;
       color:var(--dim);border-radius:14px;padding:10px 0;font-size:13px;cursor:pointer}
  #login .ghost:hover{border-color:var(--blue);color:var(--blue-deep)}
  #login #lg-msg{margin-top:12px;color:var(--blue-deep);background:#eef4ff;
       border-radius:10px;padding:8px 12px;font-size:12.5px;line-height:1.6}
  #login #lg-msg:empty{display:none}
  #guestbar{margin-left:auto;border:1.5px solid #d8e3ff;background:#eef4ff;
       color:var(--blue-deep);border-radius:999px;padding:6px 15px;font-size:13px;
       cursor:pointer;font-weight:600}
  #guestbar:hover{border-color:var(--blue)}
  /* ---------- 历史行程抽屉 ---------- */
  #histmask{position:fixed;inset:0;background:rgba(29,35,51,.35);display:none;z-index:14}
  #histpanel{position:fixed;top:0;right:0;bottom:0;width:340px;max-width:92vw;
       background:var(--card);border-left:1px solid var(--line);z-index:15;
       display:none;flex-direction:column;box-shadow:-12px 0 40px rgba(29,35,51,.14)}
  .hhead{display:flex;align-items:center;justify-content:space-between;
       padding:14px 16px;border-bottom:1px solid var(--line)}
  .hhead h2{font-size:15px}
  .hhead button{border:1.5px solid var(--line);background:#fff;border-radius:10px;
       padding:5px 13px;font-size:12.5px;cursor:pointer;color:var(--ink)}
  #histlist{flex:1;overflow-y:auto;padding:10px}
  .hitem{border:1.5px solid var(--line);border-radius:13px;padding:10px 12px;margin-bottom:8px;
       cursor:pointer;position:relative;background:#fbfcff}
  .hitem:hover{border-color:var(--blue)}
  .htitle{font-size:13.5px;font-weight:600;color:var(--ink);padding-right:46px}
  .hbadge{font-size:11px;color:var(--blue-deep);border:1px solid var(--blue);
       border-radius:999px;padding:1px 7px;margin-left:6px;font-weight:400}
  .hmeta{font-size:12px;color:var(--dim);margin-top:4px}
  .hdel{position:absolute;right:8px;top:8px;border:none;background:transparent;
       color:var(--dim);cursor:pointer;font-size:12px}
  .hdel:hover{color:#e05c3d}
  .hempty{color:var(--dim);font-size:13px;text-align:center;padding:30px 12px;line-height:1.9}
  /* ---------- 新对话规划表单（首页） ---------- */
  #planner{flex:1;overflow-y:auto;padding:24px 16px 34px;display:none}
  #planner .hero{max-width:920px;margin:0 auto 18px;border-radius:24px;padding:36px 38px;
    position:relative;overflow:hidden;color:#1d2333;
    background:linear-gradient(180deg,#dcebff 0%,#f0e7ff 60%,#fff0e4 100%);
    box-shadow:0 16px 44px rgba(61,123,255,.12)}
  #planner .hero .brand{display:inline-block;background:#fff;border:1px solid var(--line);
    color:var(--blue);border-radius:999px;padding:6px 15px;font-size:12px;font-weight:700;
    letter-spacing:2px}
  #planner .hero h2{font-size:clamp(24px,3vw,32px);line-height:1.4;margin:16px 0 10px;
    font-weight:800}
  #planner .hero h2 .hl{background:var(--grad);-webkit-background-clip:text;
    background-clip:text;color:transparent}
  #planner .hero p{font-size:13.5px;color:var(--dim)}
  #planner .hero .sun{position:absolute;right:80px;top:34px;width:58px;height:58px;
    border-radius:50%;background:linear-gradient(135deg,#ffd06e,#ffb937);
    box-shadow:0 0 0 14px rgba(255,185,55,.18)}
  #planner .hero .hill{position:absolute;right:-50px;bottom:-80px;width:300px;height:170px;
    border-radius:50%;background:rgba(61,123,255,.14)}
  #planner .hero .hill2{position:absolute;right:60px;bottom:-120px;width:340px;height:190px;
    border-radius:50%;background:rgba(122,92,255,.10)}
  #planner .card{max-width:920px;margin:0 auto;background:var(--card);border:1px solid var(--line);
    border-radius:20px;padding:26px 28px;box-shadow:0 14px 40px rgba(29,35,51,.07)}
  #planner .chead{display:flex;justify-content:space-between;align-items:baseline;
    margin-bottom:16px;gap:10px;flex-wrap:wrap}
  #planner .chead h3{font-size:16px}
  #planner .chead h3 .no{color:var(--blue-deep);font-size:13px;margin-right:6px;font-weight:700}
  #planner .chead .note{font-size:12px;color:var(--dim)}
  #planner .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 16px}
  /* min-width:0 必须加：网格子项默认 min-width:auto，不会收缩到输入框
     固有最小宽（约 20 字符）以下，窄屏上整格顶破卡片（实测踩过） */
  #planner .f{display:flex;flex-direction:column;gap:6px;min-width:0}
  #planner .f.w2{grid-column:span 2;min-width:0}
  #planner label{font-size:12.5px;color:#3c4358;font-weight:600}
  #planner label .opt{color:var(--dim);font-weight:400;margin-left:4px}
  #planner label .req{color:var(--coral);margin-left:2px}
  #planner input,#planner select{border:1.5px solid var(--line);border-radius:12px;
    padding:9px 12px;font-size:13.5px;background:#fbfcff;color:var(--ink);
    font-family:inherit;outline:none;width:100%;min-width:0}
  #planner input:focus,#planner select:focus{border-color:var(--blue);background:#fff}
  #planner .err{color:#e05c3d;font-size:12.5px;margin-top:10px;display:none}
  #planner .foot{display:flex;justify-content:space-between;align-items:center;margin-top:18px;
    padding-top:14px;border-top:1px solid var(--line)}
  #planner .foot .src{font-size:12px;color:var(--dim)}
  #planner .go{display:flex;align-items:center;gap:8px;background:var(--grad);color:#fff;
    border:none;border-radius:999px;padding:12px 28px;font-size:14px;font-weight:600;
    cursor:pointer;box-shadow:0 8px 22px rgba(122,92,255,.3)}
  #planner .go:hover{filter:brightness(1.06)}
  /* ---------- 手机端（≤760px）整体适配 ----------
     必须放在样式表最后：同名规则按源序覆盖，放前面会被
     planner/login 的桌面规则盖掉（实测手机上仍是 4 列溢出） */
  @media(max-width:760px){
       #main{flex-direction:column}
       /* 行程详情改抽屉：默认滑出屏外，.on 从底部滑入；
          常驻占屏 42vh 把对话挤没了（用户截图反馈） */
       #board{position:fixed;left:0;right:0;bottom:0;top:auto;width:auto;
            max-height:72vh;border-left:none;border-top:1px solid var(--line);
            border-radius:18px 18px 0 0;box-shadow:0 -14px 44px rgba(29,35,51,.2);
            z-index:20;display:flex;transform:translateY(105%);
            transition:transform .28s ease}
       #board.on{transform:translateY(0)}
       #board-fab{display:flex}
       #bclose{display:flex;align-items:center;justify-content:center}
       /* 顶栏一行放下：按钮一律不换行、全面收紧；手机号藏起只留积分，
          否则「历史行程(6)」「＋新对话」会被挤成两行竖排（截图实测） */
       header{padding:0 8px;gap:6px}
       header .sub{display:none}
       header h1{font-size:13px;white-space:nowrap}
       header .logo{width:28px;height:28px;font-size:14px;border-radius:8px}
       header button{padding:5px 8px;font-size:11.5px;border-radius:8px;white-space:nowrap}
       .userbox{padding:3px 8px;font-size:11px;white-space:nowrap}
       #ub-phone,#userbox .sep{display:none}
       #btn-qr{display:none}          /* 手机上再显示「手机访问」没有意义 */
       #chat{padding:12px 10px;gap:12px}
       .msg{max-width:88%;padding:11px 14px}
       /* 行程面板收紧：标题/页签/备注间距全面缩小 */
       #bhead{padding:10px 12px 8px}
       #btabs{padding:8px 10px;gap:5px}
       #btabs button{padding:3px 10px;font-size:11.5px}
       #bbody{padding:10px 12px}
       .bcard{padding:6px 8px}
       .bnode{padding:7px 6px;gap:6px}
       .bnode .tm{width:38px;font-size:11px}
       .bnode .nm{font-size:12.5px}
       #bchips{padding:8px 10px}
       #bchips button{padding:3px 10px;font-size:11.5px}
       /* 节点操作菜单 → 底部抽屉：从屏幕底滑出、带遮罩、顶部把手。
          定位由 JS 分支跳过（inline left/top 会盖掉 CSS），见 nodeMenu */
       #bmenu.drawer{top:auto;left:0;right:0;bottom:0;width:auto;min-width:0;
         border-radius:18px 18px 0 0;border-bottom:none;
         padding:12px 16px calc(14px + env(safe-area-inset-bottom));
         box-shadow:0 -14px 44px rgba(29,35,51,.22);animation:drawerup .22s ease}
       #bmenu.drawer::before{content:'';display:block;width:38px;height:4px;
         border-radius:2px;background:var(--line);margin:0 auto 6px}
       #bmenu.drawer button{padding:14px 12px;font-size:14.5px;border-radius:11px}
       @keyframes drawerup{from{transform:translateY(100%)}to{transform:none}}
       #bar{padding:8px 10px}
       #send{padding:0 14px}
       #foot{font-size:10px;padding:4px 6px 8px}
       #planner{padding:16px 10px 30px}
       #planner .hero{padding:24px 18px;border-radius:18px}
       #planner .hero h2{font-size:23px}
       #planner .card{padding:18px 14px}
       #planner .grid{grid-template-columns:repeat(2,1fr)}
       /* 预算格=金额+币种下拉，塞进半列必挤爆，手机上独占一整行 */
       #planner .fbudget{grid-column:1/-1}
       #planner .fbudget select{width:72px}
       #planner .go{padding:11px 20px;font-size:13px}
       #login .lg-nav{padding:16px 5vw}
       #login .lg-main{padding:6px 4vw 36px;gap:22px}
       #login .lg-copy{min-width:0}
       #login .lg-copy h1{font-size:26px}
       #login .lg-copy p{max-width:100%}
       #login .lg-panel{width:100%;max-width:94vw}
       #login .mini{font-size:12.5px;padding:11px 13px}
  }
</style>
</head>
<body>
<header>
  <div class="logo">&#129517;</div>
  <div><h1>晴航</h1>
       <div class="sub">会聊天的行程规划师 &middot; 真实班次 &middot; 逐日天气 &middot; 沿道路动线</div></div>
  <button id="guestbar" onclick="showLogin('登录后即可开始规划')" title="点击登录">未登录 · 点此登录</button>
  <div class="userwrap" id="userwrap" style="display:none">
    <span class="userbox" id="userbox" title="当前登录账号">
      <span id="ub-phone"></span><span class="sep">·</span><span class="cr" id="ub-credits"></span>
    </span>
    <button class="quit" id="btn-logout" onclick="askLogout()" title="退出登录">退出</button>
  </div>
  <button id="btn-hist" onclick="openHist()" style="display:none"
          title="你保存的历次行程，含与助手的完整对话，点开可继续">历史行程</button>
  <button id="btn-new" onclick="newChat()">＋ 新对话</button>
  <button id="btn-qr" onclick="openQr()" title="手机连同一 Wi-Fi，扫码即可打开">📱 手机访问</button>
</header>
<div id="histmask" onclick="closeHist()"></div>
<div id="histpanel">
  <div class="hhead"><h2>历史行程</h2><button onclick="closeHist()">关闭</button></div>
  <div id="histlist"></div>
</div>
<div id="login">
  <div class="lg-inner">
    <div class="cloud" style="width:120px;height:38px;top:12%;animation-duration:32s"></div>
    <div class="cloud" style="width:90px;height:30px;top:30%;animation-duration:40s;animation-delay:-14s"></div>
    <div class="cloud" style="width:150px;height:44px;top:64%;animation-duration:48s;animation-delay:-28s"></div>
    <div class="lg-nav"><div class="logo">&#129517;</div><b>晴航</b></div>
    <div class="lg-main">
      <div class="lg-copy">
        <span class="tag">&#10022; 会聊天的行程规划师</span>
        <h1>说一句「想去大理」<br><span class="hl">行程、班次、预算全备齐</span></h1>
        <p>真实班次查询、逐日天气、沿道路的动线地图、算到 10% 机动费的预算——像聊天一样把旅行定下来。</p>
        <div class="lg-cards">
          <div class="mini"><b>&#128646; 真实班次</b><small>往返车次按时段锁好</small></div>
          <div class="mini"><b>&#128506; 沿路动线</b><small>地图连线是真道路</small></div>
          <div class="mini"><b>&#128176; 预算透明</b><small>人均多少一目了然</small></div>
        </div>
      </div>
      <div class="lg-panel">
        <h2>欢迎来到晴航 &#9992;&#65039;</h2>
        <div class="hint">手机验证码登录，首次登录自动注册，并赠送 <b>500 积分</b>（约 15 次完整行程规划）</div>
        <div class="f-label">手机号</div>
        <div class="row2"><input id="lg-phone" maxlength="11" inputmode="numeric" placeholder="请输入 11 位手机号"></div>
        <div class="f-label">验证码</div>
        <div class="row2">
          <input id="lg-code" maxlength="6" inputmode="numeric" placeholder="6 位验证码">
          <button id="lg-send" onclick="sendCode()">获取验证码</button>
        </div>
        <button class="pri" id="lg-btn" onclick="doLogin()">登录 / 注册</button>
        <button class="ghost" onclick="closeLogin()">先看看，稍后登录</button>
        <div class="hint" id="lg-msg"></div>
      </div>
    </div>
  </div>
</div>
<div id="logoutmask">
  <div class="card">
    <h2>确认退出登录？</h2>
    <div class="hint">退出后需要重新获取验证码登录。<br>本机已有的对话记录不会删除，重新登录后可继续使用。</div>
    <div class="btnrow">
      <button onclick="closeLogout()">取消</button>
      <button class="danger" onclick="confirmLogout()">确认退出</button>
    </div>
  </div>
</div>
<div id="qrmask">
  <div class="card">
    <h2>📱 手机访问</h2>
    <div class="hint">手机连接到<b>同一个 Wi-Fi</b>，用相机扫二维码，或在手机浏览器输入下面的地址。
      用同一手机号登录，历史行程和对话全部同步，手机电脑接力规划。</div>
    <div id="qr-img"></div>
    <div class="urlrow"><code id="qr-url">获取中…</code>
      <button id="qr-copy" onclick="copyLan()">复制地址</button></div>
    <div class="btnrow"><button onclick="closeQr()">关闭</button></div>
  </div>
</div>
<div id="planner">
  <div class="hero">
    <div class="brand">&#10022; SMART TRAVEL PLANNER</div>
    <h2>说一句「想去大理」，<br><span class="hl">行程、班次、预算全备齐。</span></h2>
    <p>从目的地和日期开始。先探索真实地点，再一起编排行程。</p>
    <div class="sun"></div><div class="hill"></div><div class="hill2"></div>
  </div>
  <div class="card">
    <div class="chead">
      <h3><span class="no">01</span>先说说这趟旅行</h3>
      <span class="note">必填信息只有目的地和日期，其余都可以稍后补充。</span>
    </div>
    <div class="grid">
      <div class="f w2"><label>想去哪里<span class="req">*</span></label>
        <input id="pf-dest" placeholder="例如：大理，或上海、京都"></div>
      <div class="f"><label>从哪儿出发<span class="opt">可选</span></label>
        <input id="pf-origin" placeholder="填了才算往返大交通"></div>
      <div class="f"><label>旅行范围<span class="opt">可选</span></label>
        <select id="pf-scope"><option>自动判断</option><option>国内</option><option>出境</option></select></div>
      <div class="f"><label>出发日期<span class="req">*</span></label>
        <input id="pf-start" type="date"></div>
      <div class="f"><label>返程日期<span class="opt">可选</span></label>
        <input id="pf-end" type="date"></div>
      <div class="f fbudget"><label>预算总额<span class="opt">可选</span></label>
        <div style="display:flex;gap:6px">
          <input id="pf-budget" inputmode="numeric" placeholder="比如 8000" style="flex:1;min-width:0">
          <select id="pf-cur" style="width:80px;flex:none"><option>CNY ¥</option></select>
        </div></div>
      <div class="f"><label>同行人数<span class="opt">可选</span></label>
        <input id="pf-party" placeholder="比如 2 或 2大1小"></div>
      <div class="f"><label>旅行节奏<span class="opt">可选</span></label>
        <select id="pf-pace"><option>刚刚好</option><option>紧凑打卡</option><option>轻松慢逛</option></select></div>
      <div class="f"><label>兴趣偏好<span class="opt">可选</span></label>
        <input id="pf-interest" placeholder="大熊猫、喝茶、老街"></div>
      <div class="f"><label>住宿偏好<span class="opt">可选</span></label>
        <input id="pf-stay" placeholder="交通方便、近景点"></div>
      <div class="f"><label>饮食需求<span class="opt">可选</span></label>
        <input id="pf-food" placeholder="素食、不吃辣"></div>
      <div class="f"><label>无障碍需求<span class="opt">可选</span></label>
        <input id="pf-access" placeholder="少台阶、轮椅通行"></div>
    </div>
    <div class="err" id="pf-err"></div>
    <div class="foot">
      <span class="src">&#9679; 搜索结果附有来源，方便你核对</span>
      <button class="go" onclick="submitPlanner()">先去探索 &#8599;</button>
    </div>
  </div>
</div>
<div id="main">
<div id="chat"></div>
<div id="board">
  <div id="bhead">
    <div id="btitle"><span id="bname">行程面板</span>
      <span class="bsub" id="bsub"></span>
      <button id="bclose" onclick="toggleBoard()" title="收起行程单">&#10005;</button></div>
  </div>
  <div id="btabs"></div>
  <div id="bbody"><div class="bempty">排好行程后，这里会出现可点击的行程单：<br>点任一地点就能修改或查看介绍。</div></div>
  <div id="bchips">
    <button onclick="send('重新排一下，换个排法')">换个排法</button>
    <button onclick="send('行程别太赶，轻松一点')">别太赶</button>
    <button onclick="send('当地天气怎么样')">查天气</button>
  </div>
</div>
</div>
<div id="bmmask"></div>
<div id="bmenu"></div>
<div id="boardmask" onclick="toggleBoard()"></div>
<button id="board-fab" onclick="toggleBoard()" style="display:none"
        title="打开行程单">&#128203; 行程单</button>
<div id="bar">
  <div id="row">
    <textarea id="inp" rows="1" placeholder="说出你的旅行想法，回车发送…"></textarea>
    <button id="send" onclick="send()">发送</button>
  </div>
  <div id="foot">数据口径：天气 16 天内为 Open-Meteo 实时预报、更远日期为历史同期参考 &middot; OSRM 真实路网 &middot; 2026-10-29 查价的班次与酒店 &middot; 仅供演示</div>
</div>
<script>
// localStorage 在沙箱 iframe 里会抛 SecurityError，任何存储访问都必须兜住，
// 否则脚本初始化即死、所有按钮失灵。
const store = {
  get(k){ try{ return window.localStorage.getItem(k); }catch(e){ return null; } },
  set(k,v){ try{ window.localStorage.setItem(k,v); }catch(e){} }
};
const chat = document.getElementById('chat');
const inp  = document.getElementById('inp');
const sendBtn = document.getElementById('send');
// 刷新沿用上次的会话：正在聊的对话不能因为 F5 就断线重开。
// 恢复逻辑在 boot 里——登录态且该会话有消息才回放，否则仍落规划表单页。
let sid = store.get('ta_sid') || genSid();
store.set('ta_sid', sid);

// 任何运行期错误都显示在聊天区，而不是无声无息
window.addEventListener('error',e=>{ try{
  const d=document.createElement('div'); d.className='msg bot';
  d.style.borderColor='#c0392b';
  d.textContent='页面脚本出错：'+(e.message||e.type)+'（建议刷新页面）';
  chat.appendChild(d); chat.scrollTop=chat.scrollHeight; }catch(_){} });
window.addEventListener('unhandledrejection',e=>{ try{
  const d=document.createElement('div'); d.className='msg bot';
  d.style.borderColor='#c0392b';
  d.textContent='请求失败：'+(e.reason&&e.reason.message||e.reason||'网络异常')+'（确认服务是否在运行）';
  chat.appendChild(d); chat.scrollTop=chat.scrollHeight; }catch(_){} });

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function inline(s){return esc(s)
  .replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');}
function cells(row,tag){return row.trim().replace(/^\\||\\|$/g,'').split('|')
  .map(c=>'<'+tag+'>'+inline(c.trim())+'</'+tag+'>').join('');}

function mdBlocks(text){
  const lines=text.split('\\n'); const out=[];
  let i=0,table=null,quote=[];
  const close=()=>{ if(table){out.push(table);table=null;}
                    if(quote.length){out.push('<blockquote>'+quote.join('<br>')+'</blockquote>');quote=[];} };
  while(i<lines.length){
    const L=lines[i];
    if(/^\\s*\\|.*\\|\\s*$/.test(L) && /^\\s*\\|[\\s\\-|:]+\\|\\s*$/.test(lines[i+1]||'')){
      close(); table='<table><thead><tr>'+cells(L,'th')+'</tr></thead><tbody>'; i+=2; continue;
    }
    if(table && /^\\s*\\|.*\\|\\s*$/.test(L)){ table+='<tr>'+cells(L,'td')+'</tr>'; i++; continue; }
    close();
    const h=L.match(/^(#{1,4})\\s+(.*)/);
    if(h){ out.push('<h3>'+inline(h[2])+'</h3>'); i++; continue; }
    const q=L.match(/^>\\s?(.*)/);
    if(q){ quote.push(inline(q[1])); i++; continue; }
    if(L.trim()===''){ i++; continue; }
    out.push('<div>'+inline(L)+'</div>'); i++;
  }
  close(); return out;
}
function md(text){return mdBlocks(text).join('');}

function addUser(t){const d=document.createElement('div');d.className='msg user';
  d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
function addBot(html,meta,planUrl){const d=document.createElement('div');d.className='msg bot';
  // mdBlocks 返回数组；数组直接赋给 innerHTML 会被 toString 成逗号分隔的串，
  // 表现为"每个区块之间多一个逗号"（历史行程回放踩过）。
  d.innerHTML=Array.isArray(html)?html.join(''):html;
  if(meta){const m=document.createElement('div');m.className='meta';m.textContent=meta;d.appendChild(m);}
  if(planUrl){const a=document.createElement('a');a.className='plan-link';a.href=planUrl;
    a.target='_blank';a.textContent='🗺 查看可视化行程页';d.appendChild(a);}
  chat.appendChild(d);chat.scrollTop=chat.scrollHeight;}
// 思考气泡：点击发送立即出现，按等待时长分阶段更新文案
let thinkTimer=null;
// 服务端阶段（/api/progress）：真实进度优先，拿不到才退回按秒猜的文案
let lastPhase='', lastPhaseAt=0, progTimer=null;
function startProgress(){
  stopProgress(); lastPhase=''; lastPhaseAt=0;
  progTimer=setInterval(async()=>{
    try{
      const r=await fetch('/api/progress?sid='+encodeURIComponent(sid));
      const j=await r.json();
      if(j&&j.progress&&j.progress.phase){
        lastPhase=j.progress.phase+' · 已等 '+j.progress.elapsed+'s';
        lastPhaseAt=Date.now();
      }
    }catch(e){}
  },900);
}
function stopProgress(){
  if(progTimer){clearInterval(progTimer);progTimer=null;}
}
function typing(on){
  let t=document.querySelector('.typing');
  if(on){
    if(!t){t=document.createElement('div');t.className='typing';
      t.innerHTML='<span class="tdot"></span><span class="tdot"></span><span class="tdot"></span><span class="ttext">正在思考…</span>';
      chat.appendChild(t);}
    clearInterval(thinkTimer);
    const el=t.querySelector('.ttext'); const t0=Date.now();
    thinkTimer=setInterval(()=>{const s=(Date.now()-t0)/1000;
      // 5 秒内拿到过服务端阶段就显示真实阶段，别再显示猜的
      if(lastPhase&&Date.now()-lastPhaseAt<5000){el.textContent=lastPhase;return;}
      if(s>6)el.textContent='还在编排行程与预算，马上好…（已等 '+Math.round(s)+'s）';
      else if(s>2.2)el.textContent='正在查天气、路网耗时、酒店班次…';},600);
    chat.scrollTop=chat.scrollHeight;
  }else{
    clearInterval(thinkTimer);
    if(t)t.remove();
  }
}

// 最短思考展示：即使服务端瞬时返回，也让气泡至少停留一会，避免"秒回"显得机械
const THINK_MIN_CLARIFY=2000, THINK_MIN_PLAN=3000;
// 逐行显示：每个块（一行/表格/引用）间隔的毫秒数
const LINE_DELAY=180;
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

async function addBotStream(blocks,meta,planUrl){
  const d=document.createElement('div');d.className='msg bot';
  chat.appendChild(d);
  for(const b of blocks){
    d.insertAdjacentHTML('beforeend',b);
    chat.scrollTop=chat.scrollHeight;
    await sleep(LINE_DELAY);
  }
  if(meta){const m=document.createElement('div');m.className='meta';m.textContent=meta;
    m.style.animation='none';d.appendChild(m);}
  if(planUrl){const a=document.createElement('a');a.className='plan-link';a.href=planUrl;
    a.target='_blank';a.textContent='🗺 查看可视化行程页';a.style.animation='none';
    d.appendChild(a);}
  chat.scrollTop=chat.scrollHeight;
}

// ---------------- 账号与积分 ----------------
let token = store.get('ta_token') || '';
let refreshToken = store.get('ta_refresh') || '';
const userbox = document.getElementById('userbox');

function authHeaders(){ return token ? {'Authorization':'Bearer '+token} : {}; }

function showUser(phone, bal, unlimited){
  document.getElementById('ub-phone').textContent = phone;
  document.getElementById('ub-credits').textContent = unlimited ? '∞ 额度' : (bal + ' 积分');
  document.getElementById('userwrap').style.display = '';
  document.getElementById('guestbar').style.display = 'none';
  document.getElementById('btn-hist').style.display = '';
}

// 访客态：不弹窗，只在顶栏留一个登录入口
function showGuest(){
  document.getElementById('userwrap').style.display = 'none';
  document.getElementById('guestbar').style.display = '';
  document.getElementById('btn-hist').style.display = 'none';
}

async function loadMe(){
  try{
    const r = await fetch('/api/me', {headers: authHeaders()});
    if(r.status !== 200) return false;
    const j = await r.json();
    if(!j.ok) return false;
    showUser(j.phone, j.balance, j.unlimited);
    document.getElementById('login').style.display = 'none';
    refreshHistCount();
    return true;
  }catch(e){ return false; }
}

async function tryRefresh(){
  if(!refreshToken) return false;
  try{
    const r = await fetch('/api/auth/refresh', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({refresh:refreshToken})});
    const j = await r.json();
    if(j.ok){ token = j.access; store.set('ta_token', token); return true; }
  }catch(e){}
  token=''; refreshToken='';
  store.set('ta_token',''); store.set('ta_refresh','');
  return false;
}

// 登录框只在「用到需要账号的功能」时才弹，不做开屏强制登录
let pendingMsg = '';               // 未登录时点了发送：登录成功后自动补发

function showLogin(hint){
  document.getElementById('login').style.display = 'flex';
  document.getElementById('userwrap').style.display = 'none';
  document.getElementById('guestbar').style.display = 'none';
  document.getElementById('lg-msg').textContent = hint || '登录后即可开始对话';
  setTimeout(function(){                       // 弹窗出来顺手聚焦手机号
    const p = document.getElementById('lg-phone');
    if(p && !p.value) p.focus();
  }, 60);
}

function closeLogin(){
  document.getElementById('login').style.display = 'none';
  document.getElementById('lg-msg').textContent = '';
  pendingMsg = '';                 // 关掉就当放弃这次发送，不偷偷补发
  if(!token){ showGuest();
    // 「先看看」= 访客一律回规划首页；对话区有历史内容也不留在对话页
    showPlanner();
  }
}

async function sendCode(){
  const phone = document.getElementById('lg-phone').value.trim();
  const btn = document.getElementById('lg-send');
  document.getElementById('lg-msg').textContent = '';
  const r = await fetch('/api/auth/send_code', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({phone:phone})});
  const j = await r.json();
  if(!j.ok){ document.getElementById('lg-msg').textContent = j.error || '发送失败'; return; }
  let hint = '验证码已发送（5 分钟内有效）';
  if(j.dev_code) hint += '，演示模式验证码：' + j.dev_code;
  document.getElementById('lg-msg').textContent = hint;
  let left = 60; btn.disabled = true;
  const timer = setInterval(()=>{ left--; btn.textContent = left + 's';
    if(left <= 0){ clearInterval(timer); btn.disabled = false; btn.textContent = '获取验证码'; }
  }, 1000);
}

async function doLogin(){
  const phone = document.getElementById('lg-phone').value.trim();
  const code = document.getElementById('lg-code').value.trim();
  const btn = document.getElementById('lg-btn');
  btn.disabled = true;
  try{
    const r = await fetch('/api/auth/login', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({phone:phone, code:code})});
    const j = await r.json();
    if(!j.ok){
      document.getElementById('lg-msg').textContent = j.error || '登录失败';
      return;
    }
    token = j.access; refreshToken = j.refresh;
    store.set('ta_token', token); store.set('ta_refresh', refreshToken);
    showUser(j.phone, j.credits, j.unlimited);
    document.getElementById('login').style.display = 'none';
    document.getElementById('lg-msg').textContent = '';
    const queued = pendingMsg;                 // 登录前没发出去的那句，接着发
    pendingMsg = '';
    if(queued){ send(queued); }
    else if(j.new_user){
      addBotStream(mdBlocks('登录成功！新用户已赠送 **500 积分**。\\n直接说说你的旅行计划吧：目的地、日期、天数、人数、预算，说几样算几样～\\n（这次对话会自动保存为一次行程，随时从右上角「历史行程」找回）'));
    }
    else{ refreshHistHint(); }
  }finally{ btn.disabled = false; }
}

// 退出登录：先弹确认，避免误触顶栏直接掉线
function askLogout(){
  document.getElementById('logoutmask').style.display = 'flex';
}
function closeLogout(){
  document.getElementById('logoutmask').style.display = 'none';
}
function confirmLogout(){
  closeLogout();
  logout();
}

function logout(){
  token=''; refreshToken='';
  store.set('ta_token',''); store.set('ta_refresh','');
  pendingMsg='';
  // 退出后回到登录界面（点「先看看，稍后登录」仍可回到访客浏览态）
  showLogin('已退出登录，重新登录后继续规划');
}

// 点遮罩空白处 / 按 Esc 都算取消，不退出
document.getElementById('logoutmask').addEventListener('click', function(e){
  if(e.target === this) closeLogout();
});
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape'){ closeLogout(); hideNodeMenu(); closeQr(); }
});

// ---------------- 手机访问（扫码互通） ----------------
function openQr(){
  document.getElementById('qrmask').style.display='flex';
  const img=document.getElementById('qr-img');
  img.innerHTML='<div style="color:var(--dim);font-size:13px;padding:80px 0">生成二维码中…</div>';
  document.getElementById('qr-copy').textContent='复制地址';
  fetch('/api/lan').then(r=>r.json()).then(j=>{
    if(!j.ok||!j.url)throw new Error('no url');
    document.getElementById('qr-url').textContent=j.url;
    try{
      const qr=qrcode(0,'M'); qr.addData(j.url); qr.make();
      img.innerHTML=qr.createSvgTag({cellSize:5,margin:6});
    }catch(e2){
      img.innerHTML='<div style="color:var(--dim);font-size:13px;padding:80px 0">二维码生成失败，请直接输入地址</div>';
    }
  }).catch(()=>{
    document.getElementById('qr-url').textContent='获取局域网地址失败';
    img.innerHTML='';
  });
}
function closeQr(){ document.getElementById('qrmask').style.display='none'; }
function copyLan(){
  const t=document.getElementById('qr-url').textContent;
  if(!t)return;
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(()=>{
      document.getElementById('qr-copy').textContent='已复制 ✓';
      setTimeout(()=>{document.getElementById('qr-copy').textContent='复制地址';},1500);
    }).catch(()=>{});
  }
}
document.getElementById('qrmask').addEventListener('click', function(e){
  if(e.target === this) closeQr();
});

async function chatRequest(msg){
  const r = await fetch('/api/chat', {method:'POST',
    headers:Object.assign({'Content-Type':'application/json'}, authHeaders()),
    body:JSON.stringify({sid: sid, message: msg})});
  if(r.status === 401){
    if(await tryRefresh()) return chatRequest(msg);   // 换新 token 重试一次
    token=''; store.set('ta_token','');
    pendingMsg = msg;                                 // 重新登录后接着发这条
    showLogin('登录已过期，重新登录后继续发送这条消息');
    return null;
  }
  return r.json();
}

// ---------------- 行程面板（右侧交互式行程单）----------------
const board=document.getElementById('board');
const bbody=document.getElementById('bbody');
const btabs=document.getElementById('btabs');
const bmenu=document.getElementById('bmenu');
const bmmask=document.getElementById('bmmask');
function hideNodeMenu(){
  bmenu.style.display='none';
  bmmask.style.display='none';
}
let boardPlan=null,boardSlots=null,boardTab='all';
const TYPE_ICONS={attraction:'\\u{1F3DE}',restaurant:'\\u{1F35C}',hotel:'\\u{1F3E8}',
                  transport:'\\u{1F684}',other:'\\u{1F4CD}'};
function jsq(s){return String(s).replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");}
function nodeMenu(ev,name,ntype){
  ev.stopPropagation();
  bmenu.innerHTML='';
  const mk=(label,msg)=>{const b=document.createElement('button');b.textContent=label;
    b.onclick=()=>{hideNodeMenu();send(msg);};bmenu.appendChild(b);};
  // 菜单跟节点类型走：交通/休息/住宿和系统填充项（早市/伴手礼、自由活动、
  // 就近自选）不是景点，没有门票和介绍可看，弹「看看介绍和门票」只会
  // 换来一句"查不到"。只有真实景点/餐厅才给操作项；餐厅问人均不问门票。
  const filler=/^(早市|自由活动)/.test(name)||name.indexOf('就近自选')>=0;
  if(!filler&&(ntype==='attraction'||ntype==='restaurant')){
    mk('\\u274C 不想去这里，换个类似的','不想去'+name);
    if(ntype==='restaurant')mk('\\u{1F4D6} 看看介绍和人均',name+'怎么样？人均多少钱');
    else mk('\\u{1F4D6} 看看介绍和门票',name+'怎么样？门票多少钱');
  }
  if(!bmenu.children.length)return;
  // 手机端：底部抽屉（不定位，靠 CSS 贴底），带遮罩；桌面端：跟节点浮动
  if(window.matchMedia('(max-width:760px)').matches){
    bmenu.classList.add('drawer');
    bmenu.style.left='';bmenu.style.top='';
    bmmask.style.display='block';
    bmenu.style.display='block';
    return;
  }
  bmenu.classList.remove('drawer');
  bmenu.style.display='block';
  const rect=ev.currentTarget.getBoundingClientRect();
  const bw=240,bh=92;
  let x=Math.min(rect.left,window.innerWidth-bw-10);
  let y=rect.bottom+6;
  if(y+bh>window.innerHeight-10)y=Math.max(8,rect.top-bh-6);
  bmenu.style.left=Math.max(8,x)+'px';bmenu.style.top=y+'px';
}
document.addEventListener('click',hideNodeMenu);
function dayCard(d,idx){
  const wx=(boardPlan.weather&&boardPlan.weather.days?boardPlan.weather.days[idx]:null);
  let wtxt='';
  if(wx){
    wtxt=(wx.low!=null&&wx.high!=null)?(' '+wx.low+'~'+wx.high+'℃'):'';
    if(wx.desc)wtxt=esc(wx.desc)+wtxt;
    // 口径标注：超出预报窗口的日期是历史同期，不能和"实时预报"长得一样
    if(wx.is_forecast===false)wtxt+=' <span style="color:#B26A00">历史同期</span>';
  }
  let h='<div class="bday"><h4>第'+d.day+'天 · '+esc(d.theme||'')
    +'<span class="wx">'+esc(d.date||'')+wtxt+'</span></h4><div class="bcard">';
  (d.nodes||[]).forEach(n=>{
    const uncer=String(n.transit||'').indexOf('位置待确认')>=0;
    // 兴趣标记：强命中用青色、弱命中（沾边）用棕色，别让"近似"看起来像"已安排"
    let itag='';
    (n.interests||[]).forEach(it=>{
      const s=it.level==='strong';
      itag+=' <span style="font-size:11px;padding:1px 6px;border-radius:3px;background:'
        +(s?'#E8F0FF':'#FFF4E8')+';color:'+(s?'#2b5fe0':'#B26A00')+'">'
        +(s?'兴趣·':'近似·')+esc(it.word||'')+'</span>';
    });
    h+='<div class="bnode" onclick="nodeMenu(event,\\''+jsq(n.name)+'\\',\\''+(n.type||'other')+'\\')">'
      +'<span class="tm">'+esc(String(n.time||'').slice(0,5))+'</span>'
      +'<span class="ic">'+(TYPE_ICONS[n.type]||TYPE_ICONS.other)+'</span>'
      +'<span class="nm">'+esc(n.name)+itag+(uncer?' <span style="color:#e05c3d;font-size:11px">位置待确认</span>':'')+'</span>'
      +(n.duration_min?'<span class="mn">'+n.duration_min+'分钟</span>':'')
      +'</div>';
  });
  h+='</div>';
  if(d.geo_uncertain)h+='<div class="bnote" style="margin-top:4px;color:#e05c3d">\\u26A0 这天有点位没查到确切位置，交通时间未计入，出发前请确认</div>';
  if(d.move_min)h+='<div class="bnote" style="margin-top:4px">\\u{1F6B6} 当天步行/乘车约 '+d.move_min+' 分钟 · '+(d.rainy_plan||'')+'</div>';
  return h+'</div>';
}
function drawBoard(){
  const p=boardPlan;if(!p)return;
  const dest=(boardSlots&&boardSlots.destination)||'';
  let h='';
  if(boardTab==='all'){
    const st=(boardSlots&&boardSlots.date_range&&boardSlots.date_range.start)
      ||(p.days_plan[0]&&p.days_plan[0].date)||'';
    // 口径看"明细里有没有交通费"，不是看有没有出发地：出发地填了但班次没查到，
    // 金额照样不含路费，标题不写清楚就会被当成整趟花费。
    const hasTr=(p.budget&&(p.budget.breakdown||[]).some(x=>x.category==='transport'&&x.amount>0));
    const bud=(p.budget&&p.budget.total)
      ?(' · 预算约 ¥'+p.budget.total+(hasTr?'':'（不含往返大交通）')):'';
    h='<div class="bsec"><h4>总览 · '+esc(dest)+' '+p.days_plan.length+' 天'
      +(st?' · '+esc(st)+' 起':'')+bud+'</h4></div>';
    p.days_plan.forEach((d,i)=>{h+=dayCard(d,i);});
  }else if(boardTab==='todo'){
    const v=((p.decision_log&&p.decision_log.violations)||[])
      .concat((p.decision_log&&p.decision_log.deferred)||[]);
    if(v.length){
      h='<div class="bsec"><h4>待安排</h4>'+v.map(x=>
        '<div class="bitem">'+esc(x.poi||x.name||'')+(x.why?'<span style="color:var(--dim)"> — '+esc(x.why)+'</span>':'')
        +' <button onclick="send(\\''+jsq('还是想去'+(x.poi||x.name||''))+'\\')" style="border:1px solid var(--line);background:var(--bg);border-radius:999px;padding:2px 10px;font-size:12px;cursor:pointer;margin-left:6px;font-family:inherit">还是想去</button></div>').join('')+'</div>';
    }else{
      h='<div class="bempty">想去的地方都安排上了 \\u{1F389}</div>';
    }
  }else if(boardTab==='note'){
    const asm=p.assumptions||[];
    const mr=(p.must_report)||[];
    let notes=asm.map(a=>'<div class="bitem">\\u{1F4DD} '+esc(a)+'</div>').join('');
    if(mr.placed&&mr.placed.length)notes+='<div class="bitem">\\u2705 已确保安排：'+esc(mr.placed.join('、'))+'</div>';
    if(mr.missed&&mr.missed.length)notes+='<div class="bitem">\\u26A0\\uFE0F 没能安排：'+esc(mr.missed.join('、'))+'</div>';
    h=notes?'<div class="bsec">'+notes+'</div>':'<div class="bempty">暂无备注</div>';
  }else{
    const i=parseInt(boardTab.slice(1),10);
    h=(i>=0&&i<p.days_plan.length)?dayCard(p.days_plan[i],i):'';
  }
  bbody.innerHTML=h||'<div class="bempty">暂无内容</div>';
  bbody.scrollTop=0;
}
// ---------------- 行程单抽屉（手机端 bottom sheet） ----------------
const boardMask=document.getElementById('boardmask');
const boardFab=document.getElementById('board-fab');
const isNarrow=()=>window.matchMedia('(max-width:760px)').matches;
// 悬浮按钮要一直贴在输入区上缘：输入框随内容长高、脚注换行都会改变
// 输入区高度，写死 bottom 会把「发送」压在按钮底下（实测踩过）。
function placeFab(){
  if(!isNarrow())return;
  if(boardFab.style.display==='none')return;
  const bar=document.getElementById('bar');
  if(!bar)return;
  boardFab.style.bottom=(window.innerHeight-bar.getBoundingClientRect().top+12)+'px';
}
// open=true：桌面照常展开右侧面板；手机滑入抽屉、隐藏悬浮按钮。
// open=false：手机回到「亮着行程单按钮、抽屉收起」的待命态。
function syncBoardOpen(open){
  if(open){
    board.classList.add('on');
    boardMask.style.display=isNarrow()?'block':'none';
    boardFab.style.display='none';
  }else{
    board.classList.remove('on');
    boardMask.style.display='none';
    boardFab.style.display=(boardPlan&&isNarrow())?'':'none';
    placeFab();
  }
}
function toggleBoard(){
  syncBoardOpen(!board.classList.contains('on'));
}
// 跨过断点时把遮罩收干净（手机开抽屉 → 转成桌面窗口的残留）
window.addEventListener('resize',()=>{
  if(!isNarrow())boardMask.style.display='none';
  placeFab();
});
function hideBoard(){
  boardPlan=null;boardSlots=null;
  syncBoardOpen(false);
}
function renderBoard(plan,slots){
  boardPlan=plan;boardSlots=slots||null;
  if(!plan||!plan.days_plan||!plan.days_plan.length){hideBoard();return;}
  // 手机端不自动展开抽屉（常驻面板把对话挤没了），只亮「行程单」按钮；
  // 桌面端保持原来的自动展开
  syncBoardOpen(!isNarrow());
  const dest=(slots&&slots.destination)||'';
  document.getElementById('bname').textContent='行程详情';
  document.getElementById('bsub').textContent=dest;
  let tabs='<button data-t="all" class="act">总览</button>';
  plan.days_plan.forEach((d,i)=>{tabs+='<button data-t="d'+i+'">第'+(i+1)+'天</button>';});
  tabs+='<button data-t="todo">待安排</button><button data-t="note">备注</button>';
  btabs.innerHTML=tabs;
  btabs.querySelectorAll('button').forEach(b=>b.onclick=()=>{
    boardTab=b.dataset.t;
    btabs.querySelectorAll('button').forEach(x=>x.classList.toggle('act',x===b));
    drawBoard();});
  boardTab='all';
  drawBoard();
}

async function send(text){
  const msg=(text??inp.value).trim(); if(!msg)return;
  if(!token){
    pendingMsg = msg;                        // 记住这句，登录成功后自动发出
    showLogin('登录后立刻发送你刚才的消息（首次登录自动注册）');
    return;
  }
  inp.value=''; inp.style.height='auto';
  addUser(msg); sendBtn.disabled=true; typing(true); startProgress();
  const t0=Date.now();
  try{
    const j=await chatRequest(msg);
    if(!j){ sendBtn.disabled=false; return; }        // 401：已弹出登录框
    const min=j.mode==='plan'?THINK_MIN_PLAN:THINK_MIN_CLARIFY;
    const wait=min-(Date.now()-t0);
    if(wait>0)await sleep(wait);
    typing(false);
    if(j.unlimited){
      document.getElementById('ub-credits').textContent = '∞ 额度';
    }else if(j.credits_left !== undefined){
      document.getElementById('ub-credits').textContent = j.credits_left + ' 积分';
    }
    if(!j.ok){
      addBot(esc(j.error==='quota' ? (j.message||'积分不足') : ('出错了：'+(j.error||'未知错误'))));
    }
    else if(j.mode==='answer'){ await addBotStream(mdBlocks(j.text)); }
    else if(j.mode==='clarify'){ await addBotStream(mdBlocks(j.text), '追问 '+j.ask_rounds+'/2'); }
    else{
      const meta='第 '+j.turn_no+' 轮方案 · '+(j.tool_calls||0)+' 次工具调用'
        +(j.assumptions&&j.assumptions.length?('\\n假设：'+j.assumptions.join('；')):'');
      await addBotStream(mdBlocks(j.text), meta, j.has_plan?('/plan/'+sid):null);
      if(j.plan)renderBoard(j.plan,j.slots);   // 右侧行程面板随方案更新
    }
  }catch(e){ typing(false); addBot(esc('请求失败：'+e)); }
  finally{ stopProgress(); }
  sendBtn.disabled=false; inp.focus();
}

// 新对话：换一个 sid 即新的一次对话（上下文互不串味）。旧对话已自动存档，
// 不删除，随时从「历史行程」里找回继续。
function genSid(){
  return (window.crypto&&crypto.randomUUID)?crypto.randomUUID()
        :'s'+Date.now().toString(36)+Math.random().toString(36).slice(2);
}
// ---------------- 新对话规划表单 ----------------
// 新对话不直接进对话，先填「先说说这趟旅行」表单；提交后把表单内容
// 合成一句话走既有 /api/chat 链路（未登录会先弹登录再自动补发）。
const planner = document.getElementById('planner');
function showPlanner(){
  planner.style.display='block';
  chat.style.display='none';
  hideBoard();                       // 新规划开始，收起上一份行程面板
  // #main 也得藏起来：它 flex:1，不藏会和表单平分视口，表单下半被截断
  document.getElementById('main').style.display='none';
  document.getElementById('bar').style.display='none';
  const t=new Date();
  const iso=t.getFullYear()+'-'+String(t.getMonth()+1).padStart(2,'0')+'-'+String(t.getDate()).padStart(2,'0');
  document.getElementById('pf-start').min=iso;
  document.getElementById('pf-end').min=iso;
  planner.scrollTop=0;
  setTimeout(()=>{const d=document.getElementById('pf-dest'); if(d)d.focus();},60);
}
function hidePlanner(){
  planner.style.display='none';
  document.getElementById('main').style.display='flex';
  chat.style.display='flex';
  document.getElementById('bar').style.display='';
  inp.focus();
}
function submitPlanner(){
  const dest=document.getElementById('pf-dest').value.trim();
  const start=document.getElementById('pf-start').value;
  const err=document.getElementById('pf-err');
  const fail=t=>{err.textContent=t;err.style.display='block';};
  if(!dest){ fail('先填一下「想去哪里」——城市名即可，比如：大理、上海、京都。'); return; }
  if(!start){ fail('再选一下出发日期；只定个大概也行，之后随时改。'); return; }
  const sd=new Date(start+'T00:00:00');
  const end=document.getElementById('pf-end').value;
  let days=0;
  if(end){
    days=Math.round((new Date(end+'T00:00:00')-sd)/86400000)+1;
    if(days<1){ fail('返程日期要和出发日期同天或更晚。'); return; }
  }
  err.style.display='none';
  const parts=[];
  const origin=document.getElementById('pf-origin').value.trim();
  parts.push((origin?'从'+origin+'出发，':'')
    +(sd.getMonth()+1)+'月'+sd.getDate()+'号去'+dest+(days?'玩'+days+'天':''));
  const partyRaw=document.getElementById('pf-party').value.trim();
  if(partyRaw){
    // 「2大1小（3岁）」必须原样交给对话层拆——之前 parseInt 只抠出 2 再拼
    // 「2个人」，「1小（3岁）」整段丢掉：孩子从人数、预算、午休、家庭房里
    // 全部消失（亲子慢游实测反馈）。纯数字才走「N个人」句式。
    if(/^\\d{1,3}$/.test(partyRaw))parts.push(partyRaw+'个人');
    else parts.push(partyRaw);
  }
  const budget=parseFloat(document.getElementById('pf-budget').value);
  if(budget>0)parts.push('预算总共'+Math.round(budget));
  const pace=document.getElementById('pf-pace').value;
  if(pace==='紧凑打卡')parts.push('尽量多打卡几个地方');
  else if(pace==='轻松慢逛')parts.push('不想太赶，轻松休闲一点');
  const scope=document.getElementById('pf-scope').value;
  if(scope==='国内')parts.push('国内游');
  else if(scope==='出境')parts.push('出境游');
  const interest=document.getElementById('pf-interest').value.trim();
  if(interest)parts.push('喜欢'+interest);
  const stay=document.getElementById('pf-stay').value.trim();
  if(stay)parts.push('住宿希望'+stay);
  const food=document.getElementById('pf-food').value.trim();
  if(food)parts.push(food);
  const access=document.getElementById('pf-access').value.trim();
  if(access)parts.push('无障碍需求：'+access);
  hidePlanner();
  send(parts.join('，'));
}
planner.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target.tagName!=='SELECT'){e.preventDefault();submitPlanner();}
});
function newChat(){
  sid = genSid(); store.set('ta_sid', sid);
  chat.innerHTML='';
  showPlanner();
}

// ---------------- 历史行程 ----------------
function fmtTime(ts){
  const d=new Date(ts*1000), p=n=>(n<10?'0':'')+n;
  return (d.getMonth()+1)+'月'+d.getDate()+'日 '+p(d.getHours())+':'+p(d.getMinutes());
}
function closeHist(){
  document.getElementById('histpanel').style.display='none';
  document.getElementById('histmask').style.display='none';
}
function openHist(){
  if(!token){ showLogin('登录后可查看历史行程'); return; }
  document.getElementById('histpanel').style.display='flex';
  document.getElementById('histmask').style.display='block';
  refreshHistList();
}
async function refreshHistList(){
  const list=document.getElementById('histlist');
  list.innerHTML='<div class="hempty">加载中…</div>';
  let r=await fetch('/api/conversations',{headers:authHeaders()});
  if(r.status===401){
    if(await tryRefresh()){ return refreshHistList(); }
    closeHist(); showLogin('登录已过期，请重新登录'); return;
  }
  const j=await r.json();
  const items=(j.items||[]);
  if(!items.length){
    list.innerHTML='<div class="hempty">还没有历史行程。<br>发第一条消息就会自动保存成一次行程（含完整对话）。</div>';
    return;
  }
  list.innerHTML='';
  for(const it of items){
    const d=document.createElement('div'); d.className='hitem';
    d.innerHTML='<div class="htitle">'+esc(it.title||'未命名行程')
      +(it.has_plan?' <span class="hbadge">含方案</span>':'')+'</div>'
      +'<div class="hmeta">'+(it.dest?esc(it.dest)+' · ':'')+it.msg_count+' 条消息 · '+fmtTime(it.updated_at)+'</div>';
    const del=document.createElement('button'); del.className='hdel';
    del.textContent='删除'; del.title='删除这次对话';
    del.onclick=async ev=>{ ev.stopPropagation();
      const rr=await fetch('/api/conversations/'+encodeURIComponent(it.sid),
        {method:'DELETE', headers:authHeaders()});
      if(rr.status===401){ closeHist(); showLogin('登录已过期，请重新登录'); return; }
      if(it.sid===sid){ newChat(); }             // 删的是当前会话 → 切到新会话
      d.remove();
      if(!list.children.length) list.innerHTML='<div class="hempty">还没有历史行程。</div>';
    };
    d.onclick=()=>loadConv(it.sid);
    d.appendChild(del);
    list.appendChild(d);
  }
}
// 点开一次历史行程：整段对话原样回放，之后继续说话就接着这次排。
// pre = boot 恢复时已经取到的响应，避免同一会话连拉两次。
async function loadConv(sid_, pre){
  let j = pre;
  if(!j){
    let r=await fetch('/api/conversations/'+encodeURIComponent(sid_),{headers:authHeaders()});
    if(r.status===401){
      if(await tryRefresh()){ return loadConv(sid_); }
      showLogin('登录已过期，请重新登录'); return;
    }
    j=await r.json();
  }
  if(!j.ok){ return; }
  closeHist();
  sid=sid_; store.set('ta_sid', sid);
  hidePlanner();
  chat.innerHTML='';
  for(const m of (j.messages||[])){
    if(m.role==='user') addUser(m.content);
    else addBot(mdBlocks(m.content).join(''));
  }
  if(j.has_plan){
    addBot(mdBlocks('> 这是这次对话排出的行程。直接在下面继续说话就能在它基础上调整。').join(''), '', '/plan/'+sid);
    if(j.plan){ renderBoard(j.plan, j.slots||{}); }   // 恢复右侧交互式行程面板
  }
  chat.scrollTop=chat.scrollHeight;
}
async function refreshHistCount(){
  try{
    const r=await fetch('/api/conversations',{headers:authHeaders()});
    if(!r.ok) return;
    const j=await r.json();
    const n=(j.items||[]).length;
    document.getElementById('btn-hist').textContent = n ? '历史行程('+n+')' : '历史行程';
  }catch(e){}
}
async function refreshHistHint(){
  try{
    const r=await fetch('/api/conversations',{headers:authHeaders()});
    if(!r.ok) return;
    const j=await r.json();
    const n=(j.items||[]).length;
    document.getElementById('btn-hist').textContent = n ? '历史行程('+n+')' : '历史行程';
    if(n>0){
      addBotStream(mdBlocks('欢迎回来！你已保存 **'+n+'** 次行程对话，点右上角「历史行程」可回看（含当时的完整对话）或继续之前的行程。'));
    }
  }catch(e){}
}

(async function boot(){
  if(token){
    const ok = await loadMe();
    if(!ok && await tryRefresh()){ await loadMe(); }
    if(!token){                              // token 失效：静默退回访客，发送时再登录
      token=''; store.set('ta_token',''); showGuest();
    }
  }else{
    showGuest();                             // 打开网站不弹登录框
  }
  // #login 深链：直达登录页（分享/收藏登录入口用），不影响默认访客首屏
  if(location.hash==='#login') return showLogin();
  // 刷新恢复：登录态且上次会话已有消息 → 原样回放；任何走不通的分支都
  // 落回规划表单页（init 时表单被藏住了，这里负责兜底展示）
  if(!token){ showPlanner(); return; }
  if(!sid){ showPlanner(); return; }
  try{
    let r = await fetch('/api/conversations/'+encodeURIComponent(sid),
                        {headers:authHeaders()});
    if(r.status===401 && await tryRefresh()){
      r = await fetch('/api/conversations/'+encodeURIComponent(sid),
                      {headers:authHeaders()});
    }
    if(!r.ok){ showPlanner(); return; }
    const j = await r.json();
    if(j.ok && (j.messages||[]).length) loadConv(sid, j);
    else showPlanner();
  }catch(e){ showPlanner(); }
})();

inp.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
});
inp.addEventListener('input',()=>{inp.style.height='auto';
  inp.style.height=Math.min(inp.scrollHeight,120)+'px';placeFab();});

// 打开网页默认规划表单；但登录态且有存储会话时先全部藏住（不闪表单帧），
// 等 boot 决定回放对话还是兜底落回表单
if(token && store.get('ta_sid')){
  planner.style.display='none';
  document.getElementById('main').style.display='none';
  document.getElementById('bar').style.display='none';
}else{
  showPlanner();
}
</script>
<script>
// 二维码生成：qrcode-generator 1.4.4（MIT，github.com/kazuhikoarase/qrcode-generator）。
// 压缩后 base64 内嵌——源码里有正则转义，直接贴进 Python 字符串会被转义规则吃掉。
// 间接 eval 在全局作用域执行，UMD 库的 var qrcode 才能挂到 window 上；
// 任何失败只影响扫码弹窗，不影响其它功能。
try{ (0,eval)(atob("__QR_B64__")); }catch(e){}
</script>
</body>
</html>"""

# 二维码库 base64（见上方脚本注释）；占位符在模块加载时替换，PAGE 始终是成品
QR_LIB_B64 = (
    "LyoqCiAqIE1pbmlmaWVkIGJ5IGpzRGVsaXZyIHVzaW5nIFRlcnNlciB2NS4zNy4wLgogKiBPcmlnaW5hbCBmaWxlOiAvbnBtL3Fy"
    "Y29kZS1nZW5lcmF0b3JAMS40LjQvcXJjb2RlLmpzCiAqCiAqIERvIE5PVCB1c2UgU1JJIHdpdGggZHluYW1pY2FsbHkgZ2VuZXJh"
    "dGVkIGZpbGVzISBNb3JlIGluZm9ybWF0aW9uOiBodHRwczovL3d3dy5qc2RlbGl2ci5jb20vdXNpbmctc3JpLXdpdGgtZHluYW1p"
    "Yy1maWxlcwogKi8KdmFyIHFyY29kZT1mdW5jdGlvbigpe3ZhciB0PWZ1bmN0aW9uKHQscil7dmFyIGU9dCxuPWdbcl0sbz1udWxs"
    "LGk9MCxhPW51bGwsdT1bXSxmPXt9LGM9ZnVuY3Rpb24odCxyKXtvPWZ1bmN0aW9uKHQpe2Zvcih2YXIgcj1uZXcgQXJyYXkodCks"
    "ZT0wO2U8dDtlKz0xKXtyW2VdPW5ldyBBcnJheSh0KTtmb3IodmFyIG49MDtuPHQ7bis9MSlyW2VdW25dPW51bGx9cmV0dXJuIHJ9"
    "KGk9NCplKzE3KSxsKDAsMCksbChpLTcsMCksbCgwLGktNykscygpLGgoKSxkKHQsciksZT49NyYmdih0KSxudWxsPT1hJiYoYT1w"
    "KGUsbix1KSksdyhhLHIpfSxsPWZ1bmN0aW9uKHQscil7Zm9yKHZhciBlPS0xO2U8PTc7ZSs9MSlpZighKHQrZTw9LTF8fGk8PXQr"
    "ZSkpZm9yKHZhciBuPS0xO248PTc7bis9MSlyK248PS0xfHxpPD1yK258fChvW3QrZV1bcituXT0wPD1lJiZlPD02JiYoMD09bnx8"
    "Nj09bil8fDA8PW4mJm48PTYmJigwPT1lfHw2PT1lKXx8Mjw9ZSYmZTw9NCYmMjw9biYmbjw9NCl9LGg9ZnVuY3Rpb24oKXtmb3Io"
    "dmFyIHQ9ODt0PGktODt0Kz0xKW51bGw9PW9bdF1bNl0mJihvW3RdWzZdPXQlMj09MCk7Zm9yKHZhciByPTg7cjxpLTg7cis9MSlu"
    "dWxsPT1vWzZdW3JdJiYob1s2XVtyXT1yJTI9PTApfSxzPWZ1bmN0aW9uKCl7Zm9yKHZhciB0PUIuZ2V0UGF0dGVyblBvc2l0aW9u"
    "KGUpLHI9MDtyPHQubGVuZ3RoO3IrPTEpZm9yKHZhciBuPTA7bjx0Lmxlbmd0aDtuKz0xKXt2YXIgaT10W3JdLGE9dFtuXTtpZihu"
    "dWxsPT1vW2ldW2FdKWZvcih2YXIgdT0tMjt1PD0yO3UrPTEpZm9yKHZhciBmPS0yO2Y8PTI7Zis9MSlvW2krdV1bYStmXT0tMj09"
    "dXx8Mj09dXx8LTI9PWZ8fDI9PWZ8fDA9PXUmJjA9PWZ9fSx2PWZ1bmN0aW9uKHQpe2Zvcih2YXIgcj1CLmdldEJDSFR5cGVOdW1i"
    "ZXIoZSksbj0wO248MTg7bis9MSl7dmFyIGE9IXQmJjE9PShyPj5uJjEpO29bTWF0aC5mbG9vcihuLzMpXVtuJTMraS04LTNdPWF9"
    "Zm9yKG49MDtuPDE4O24rPTEpe2E9IXQmJjE9PShyPj5uJjEpO29bbiUzK2ktOC0zXVtNYXRoLmZsb29yKG4vMyldPWF9fSxkPWZ1"
    "bmN0aW9uKHQscil7Zm9yKHZhciBlPW48PDN8cixhPUIuZ2V0QkNIVHlwZUluZm8oZSksdT0wO3U8MTU7dSs9MSl7dmFyIGY9IXQm"
    "JjE9PShhPj51JjEpO3U8Nj9vW3VdWzhdPWY6dTw4P29bdSsxXVs4XT1mOm9baS0xNSt1XVs4XT1mfWZvcih1PTA7dTwxNTt1Kz0x"
    "KXtmPSF0JiYxPT0oYT4+dSYxKTt1PDg/b1s4XVtpLXUtMV09Zjp1PDk/b1s4XVsxNS11LTErMV09ZjpvWzhdWzE1LXUtMV09Zn1v"
    "W2ktOF1bOF09IXR9LHc9ZnVuY3Rpb24odCxyKXtmb3IodmFyIGU9LTEsbj1pLTEsYT03LHU9MCxmPUIuZ2V0TWFza0Z1bmN0aW9u"
    "KHIpLGM9aS0xO2M+MDtjLT0yKWZvcig2PT1jJiYoYy09MSk7Oyl7Zm9yKHZhciBnPTA7ZzwyO2crPTEpaWYobnVsbD09b1tuXVtj"
    "LWddKXt2YXIgbD0hMTt1PHQubGVuZ3RoJiYobD0xPT0odFt1XT4+PmEmMSkpLGYobixjLWcpJiYobD0hbCksb1tuXVtjLWddPWws"
    "LTE9PShhLT0xKSYmKHUrPTEsYT03KX1pZigobis9ZSk8MHx8aTw9bil7bi09ZSxlPS1lO2JyZWFrfX19LHA9ZnVuY3Rpb24odCxy"
    "LGUpe2Zvcih2YXIgbj1BLmdldFJTQmxvY2tzKHQsciksbz1iKCksaT0wO2k8ZS5sZW5ndGg7aSs9MSl7dmFyIGE9ZVtpXTtvLnB1"
    "dChhLmdldE1vZGUoKSw0KSxvLnB1dChhLmdldExlbmd0aCgpLEIuZ2V0TGVuZ3RoSW5CaXRzKGEuZ2V0TW9kZSgpLHQpKSxhLndy"
    "aXRlKG8pfXZhciB1PTA7Zm9yKGk9MDtpPG4ubGVuZ3RoO2krPTEpdSs9bltpXS5kYXRhQ291bnQ7aWYoby5nZXRMZW5ndGhJbkJp"
    "dHMoKT44KnUpdGhyb3ciY29kZSBsZW5ndGggb3ZlcmZsb3cuICgiK28uZ2V0TGVuZ3RoSW5CaXRzKCkrIj4iKzgqdSsiKSI7Zm9y"
    "KG8uZ2V0TGVuZ3RoSW5CaXRzKCkrNDw9OCp1JiZvLnB1dCgwLDQpO28uZ2V0TGVuZ3RoSW5CaXRzKCklOCE9MDspby5wdXRCaXQo"
    "ITEpO2Zvcig7IShvLmdldExlbmd0aEluQml0cygpPj04KnV8fChvLnB1dCgyMzYsOCksby5nZXRMZW5ndGhJbkJpdHMoKT49OCp1"
    "KSk7KW8ucHV0KDE3LDgpO3JldHVybiBmdW5jdGlvbih0LHIpe2Zvcih2YXIgZT0wLG49MCxvPTAsaT1uZXcgQXJyYXkoci5sZW5n"
    "dGgpLGE9bmV3IEFycmF5KHIubGVuZ3RoKSx1PTA7dTxyLmxlbmd0aDt1Kz0xKXt2YXIgZj1yW3VdLmRhdGFDb3VudCxjPXJbdV0u"
    "dG90YWxDb3VudC1mO249TWF0aC5tYXgobixmKSxvPU1hdGgubWF4KG8sYyksaVt1XT1uZXcgQXJyYXkoZik7Zm9yKHZhciBnPTA7"
    "ZzxpW3VdLmxlbmd0aDtnKz0xKWlbdV1bZ109MjU1JnQuZ2V0QnVmZmVyKClbZytlXTtlKz1mO3ZhciBsPUIuZ2V0RXJyb3JDb3Jy"
    "ZWN0UG9seW5vbWlhbChjKSxoPWsoaVt1XSxsLmdldExlbmd0aCgpLTEpLm1vZChsKTtmb3IoYVt1XT1uZXcgQXJyYXkobC5nZXRM"
    "ZW5ndGgoKS0xKSxnPTA7ZzxhW3VdLmxlbmd0aDtnKz0xKXt2YXIgcz1nK2guZ2V0TGVuZ3RoKCktYVt1XS5sZW5ndGg7YVt1XVtn"
    "XT1zPj0wP2guZ2V0QXQocyk6MH19dmFyIHY9MDtmb3IoZz0wO2c8ci5sZW5ndGg7Zys9MSl2Kz1yW2ddLnRvdGFsQ291bnQ7dmFy"
    "IGQ9bmV3IEFycmF5KHYpLHc9MDtmb3IoZz0wO2c8bjtnKz0xKWZvcih1PTA7dTxyLmxlbmd0aDt1Kz0xKWc8aVt1XS5sZW5ndGgm"
    "JihkW3ddPWlbdV1bZ10sdys9MSk7Zm9yKGc9MDtnPG87Zys9MSlmb3IodT0wO3U8ci5sZW5ndGg7dSs9MSlnPGFbdV0ubGVuZ3Ro"
    "JiYoZFt3XT1hW3VdW2ddLHcrPTEpO3JldHVybiBkfShvLG4pfTtmLmFkZERhdGE9ZnVuY3Rpb24odCxyKXt2YXIgZT1udWxsO3N3"
    "aXRjaChyPXJ8fCJCeXRlIil7Y2FzZSJOdW1lcmljIjplPU0odCk7YnJlYWs7Y2FzZSJBbHBoYW51bWVyaWMiOmU9eCh0KTticmVh"
    "aztjYXNlIkJ5dGUiOmU9bSh0KTticmVhaztjYXNlIkthbmppIjplPUwodCk7YnJlYWs7ZGVmYXVsdDp0aHJvdyJtb2RlOiIrcn11"
    "LnB1c2goZSksYT1udWxsfSxmLmlzRGFyaz1mdW5jdGlvbih0LHIpe2lmKHQ8MHx8aTw9dHx8cjwwfHxpPD1yKXRocm93IHQrIiwi"
    "K3I7cmV0dXJuIG9bdF1bcl19LGYuZ2V0TW9kdWxlQ291bnQ9ZnVuY3Rpb24oKXtyZXR1cm4gaX0sZi5tYWtlPWZ1bmN0aW9uKCl7"
    "aWYoZTwxKXtmb3IodmFyIHQ9MTt0PDQwO3QrKyl7Zm9yKHZhciByPUEuZ2V0UlNCbG9ja3ModCxuKSxvPWIoKSxpPTA7aTx1Lmxl"
    "bmd0aDtpKyspe3ZhciBhPXVbaV07by5wdXQoYS5nZXRNb2RlKCksNCksby5wdXQoYS5nZXRMZW5ndGgoKSxCLmdldExlbmd0aElu"
    "Qml0cyhhLmdldE1vZGUoKSx0KSksYS53cml0ZShvKX12YXIgZz0wO2ZvcihpPTA7aTxyLmxlbmd0aDtpKyspZys9cltpXS5kYXRh"
    "Q291bnQ7aWYoby5nZXRMZW5ndGhJbkJpdHMoKTw9OCpnKWJyZWFrfWU9dH1jKCExLGZ1bmN0aW9uKCl7Zm9yKHZhciB0PTAscj0w"
    "LGU9MDtlPDg7ZSs9MSl7YyghMCxlKTt2YXIgbj1CLmdldExvc3RQb2ludChmKTsoMD09ZXx8dD5uKSYmKHQ9bixyPWUpfXJldHVy"
    "biByfSgpKX0sZi5jcmVhdGVUYWJsZVRhZz1mdW5jdGlvbih0LHIpe3Q9dHx8Mjt2YXIgZT0iIjtlKz0nPHRhYmxlIHN0eWxlPSIn"
    "LGUrPSIgYm9yZGVyLXdpZHRoOiAwcHg7IGJvcmRlci1zdHlsZTogbm9uZTsiLGUrPSIgYm9yZGVyLWNvbGxhcHNlOiBjb2xsYXBz"
    "ZTsiLGUrPSIgcGFkZGluZzogMHB4OyBtYXJnaW46ICIrKHI9dm9pZCAwPT09cj80KnQ6cikrInB4OyIsZSs9JyI+JyxlKz0iPHRi"
    "b2R5PiI7Zm9yKHZhciBuPTA7bjxmLmdldE1vZHVsZUNvdW50KCk7bis9MSl7ZSs9Ijx0cj4iO2Zvcih2YXIgbz0wO288Zi5nZXRN"
    "b2R1bGVDb3VudCgpO28rPTEpZSs9Jzx0ZCBzdHlsZT0iJyxlKz0iIGJvcmRlci13aWR0aDogMHB4OyBib3JkZXItc3R5bGU6IG5v"
    "bmU7IixlKz0iIGJvcmRlci1jb2xsYXBzZTogY29sbGFwc2U7IixlKz0iIHBhZGRpbmc6IDBweDsgbWFyZ2luOiAwcHg7IixlKz0i"
    "IHdpZHRoOiAiK3QrInB4OyIsZSs9IiBoZWlnaHQ6ICIrdCsicHg7IixlKz0iIGJhY2tncm91bmQtY29sb3I6ICIsZSs9Zi5pc0Rh"
    "cmsobixvKT8iIzAwMDAwMCI6IiNmZmZmZmYiLGUrPSI7IixlKz0nIi8+JztlKz0iPC90cj4ifXJldHVybiBlKz0iPC90Ym9keT4i"
    "LGUrPSI8L3RhYmxlPiJ9LGYuY3JlYXRlU3ZnVGFnPWZ1bmN0aW9uKHQscixlLG4pe3ZhciBvPXt9OyJvYmplY3QiPT10eXBlb2Yg"
    "YXJndW1lbnRzWzBdJiYodD0obz1hcmd1bWVudHNbMF0pLmNlbGxTaXplLHI9by5tYXJnaW4sZT1vLmFsdCxuPW8udGl0bGUpLHQ9"
    "dHx8MixyPXZvaWQgMD09PXI/NCp0OnIsKGU9InN0cmluZyI9PXR5cGVvZiBlP3t0ZXh0OmV9OmV8fHt9KS50ZXh0PWUudGV4dHx8"
    "bnVsbCxlLmlkPWUudGV4dD9lLmlkfHwicXJjb2RlLWRlc2NyaXB0aW9uIjpudWxsLChuPSJzdHJpbmciPT10eXBlb2Ygbj97dGV4"
    "dDpufTpufHx7fSkudGV4dD1uLnRleHR8fG51bGwsbi5pZD1uLnRleHQ/bi5pZHx8InFyY29kZS10aXRsZSI6bnVsbDt2YXIgaSxh"
    "LHUsYyxnPWYuZ2V0TW9kdWxlQ291bnQoKSp0KzIqcixsPSIiO2ZvcihjPSJsIit0KyIsMCAwLCIrdCsiIC0iK3QrIiwwIDAsLSIr"
    "dCsieiAiLGwrPSc8c3ZnIHZlcnNpb249IjEuMSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIicsbCs9by5zY2Fs"
    "YWJsZT8iIjonIHdpZHRoPSInK2crJ3B4IiBoZWlnaHQ9IicrZysncHgiJyxsKz0nIHZpZXdCb3g9IjAgMCAnK2crIiAiK2crJyIg"
    "JyxsKz0nIHByZXNlcnZlQXNwZWN0UmF0aW89InhNaW5ZTWluIG1lZXQiJyxsKz1uLnRleHR8fGUudGV4dD8nIHJvbGU9ImltZyIg"
    "YXJpYS1sYWJlbGxlZGJ5PSInK3koW24uaWQsZS5pZF0uam9pbigiICIpLnRyaW0oKSkrJyInOiIiLGwrPSI+IixsKz1uLnRleHQ/"
    "Jzx0aXRsZSBpZD0iJyt5KG4uaWQpKyciPicreShuLnRleHQpKyI8L3RpdGxlPiI6IiIsbCs9ZS50ZXh0Pyc8ZGVzY3JpcHRpb24g"
    "aWQ9IicreShlLmlkKSsnIj4nK3koZS50ZXh0KSsiPC9kZXNjcmlwdGlvbj4iOiIiLGwrPSc8cmVjdCB3aWR0aD0iMTAwJSIgaGVp"
    "Z2h0PSIxMDAlIiBmaWxsPSJ3aGl0ZSIgY3g9IjAiIGN5PSIwIi8+JyxsKz0nPHBhdGggZD0iJyxhPTA7YTxmLmdldE1vZHVsZUNv"
    "dW50KCk7YSs9MSlmb3IodT1hKnQrcixpPTA7aTxmLmdldE1vZHVsZUNvdW50KCk7aSs9MSlmLmlzRGFyayhhLGkpJiYobCs9Ik0i"
    "KyhpKnQrcikrIiwiK3UrYyk7cmV0dXJuIGwrPSciIHN0cm9rZT0idHJhbnNwYXJlbnQiIGZpbGw9ImJsYWNrIi8+JyxsKz0iPC9z"
    "dmc+In0sZi5jcmVhdGVEYXRhVVJMPWZ1bmN0aW9uKHQscil7dD10fHwyLHI9dm9pZCAwPT09cj80KnQ6cjt2YXIgZT1mLmdldE1v"
    "ZHVsZUNvdW50KCkqdCsyKnIsbj1yLG89ZS1yO3JldHVybiBJKGUsZSwoZnVuY3Rpb24ocixlKXtpZihuPD1yJiZyPG8mJm48PWUm"
    "JmU8byl7dmFyIGk9TWF0aC5mbG9vcigoci1uKS90KSxhPU1hdGguZmxvb3IoKGUtbikvdCk7cmV0dXJuIGYuaXNEYXJrKGEsaSk/"
    "MDoxfXJldHVybiAxfSkpfSxmLmNyZWF0ZUltZ1RhZz1mdW5jdGlvbih0LHIsZSl7dD10fHwyLHI9dm9pZCAwPT09cj80KnQ6cjt2"
    "YXIgbj1mLmdldE1vZHVsZUNvdW50KCkqdCsyKnIsbz0iIjtyZXR1cm4gbys9IjxpbWciLG8rPScgc3JjPSInLG8rPWYuY3JlYXRl"
    "RGF0YVVSTCh0LHIpLG8rPSciJyxvKz0nIHdpZHRoPSInLG8rPW4sbys9JyInLG8rPScgaGVpZ2h0PSInLG8rPW4sbys9JyInLGUm"
    "JihvKz0nIGFsdD0iJyxvKz15KGUpLG8rPSciJyksbys9Ii8+In07dmFyIHk9ZnVuY3Rpb24odCl7Zm9yKHZhciByPSIiLGU9MDtl"
    "PHQubGVuZ3RoO2UrPTEpe3ZhciBuPXQuY2hhckF0KGUpO3N3aXRjaChuKXtjYXNlIjwiOnIrPSImbHQ7IjticmVhaztjYXNlIj4i"
    "OnIrPSImZ3Q7IjticmVhaztjYXNlIiYiOnIrPSImYW1wOyI7YnJlYWs7Y2FzZSciJzpyKz0iJnF1b3Q7IjticmVhaztkZWZhdWx0"
    "OnIrPW59fXJldHVybiByfTtyZXR1cm4gZi5jcmVhdGVBU0NJST1mdW5jdGlvbih0LHIpe2lmKCh0PXR8fDEpPDIpcmV0dXJuIGZ1"
    "bmN0aW9uKHQpe3Q9dm9pZCAwPT09dD8yOnQ7dmFyIHIsZSxuLG8saSxhPTEqZi5nZXRNb2R1bGVDb3VudCgpKzIqdCx1PXQsYz1h"
    "LXQsZz17IuKWiOKWiCI6IuKWiCIsIuKWiCAiOiLiloAiLCIg4paIIjoi4paEIiwiICAiOiIgIn0sbD17IuKWiOKWiCI6IuKWgCIs"
    "IuKWiCAiOiLiloAiLCIg4paIIjoiICIsIiAgIjoiICJ9LGg9IiI7Zm9yKHI9MDtyPGE7cis9Mil7Zm9yKG49TWF0aC5mbG9vcigo"
    "ci11KS8xKSxvPU1hdGguZmxvb3IoKHIrMS11KS8xKSxlPTA7ZTxhO2UrPTEpaT0i4paIIix1PD1lJiZlPGMmJnU8PXImJnI8YyYm"
    "Zi5pc0RhcmsobixNYXRoLmZsb29yKChlLXUpLzEpKSYmKGk9IiAiKSx1PD1lJiZlPGMmJnU8PXIrMSYmcisxPGMmJmYuaXNEYXJr"
    "KG8sTWF0aC5mbG9vcigoZS11KS8xKSk/aSs9IiAiOmkrPSLilogiLGgrPXQ8MSYmcisxPj1jP2xbaV06Z1tpXTtoKz0iXG4ifXJl"
    "dHVybiBhJTImJnQ+MD9oLnN1YnN0cmluZygwLGgubGVuZ3RoLWEtMSkrQXJyYXkoYSsxKS5qb2luKCLiloAiKTpoLnN1YnN0cmlu"
    "ZygwLGgubGVuZ3RoLTEpfShyKTt0LT0xLHI9dm9pZCAwPT09cj8yKnQ6cjt2YXIgZSxuLG8saSxhPWYuZ2V0TW9kdWxlQ291bnQo"
    "KSp0KzIqcix1PXIsYz1hLXIsZz1BcnJheSh0KzEpLmpvaW4oIuKWiOKWiCIpLGw9QXJyYXkodCsxKS5qb2luKCIgICIpLGg9IiIs"
    "cz0iIjtmb3IoZT0wO2U8YTtlKz0xKXtmb3Iobz1NYXRoLmZsb29yKChlLXUpL3QpLHM9IiIsbj0wO248YTtuKz0xKWk9MSx1PD1u"
    "JiZuPGMmJnU8PWUmJmU8YyYmZi5pc0RhcmsobyxNYXRoLmZsb29yKChuLXUpL3QpKSYmKGk9MCkscys9aT9nOmw7Zm9yKG89MDtv"
    "PHQ7bys9MSloKz1zKyJcbiJ9cmV0dXJuIGguc3Vic3RyaW5nKDAsaC5sZW5ndGgtMSl9LGYucmVuZGVyVG8yZENvbnRleHQ9ZnVu"
    "Y3Rpb24odCxyKXtyPXJ8fDI7Zm9yKHZhciBlPWYuZ2V0TW9kdWxlQ291bnQoKSxuPTA7bjxlO24rKylmb3IodmFyIG89MDtvPGU7"
    "bysrKXQuZmlsbFN0eWxlPWYuaXNEYXJrKG4sbyk/ImJsYWNrIjoid2hpdGUiLHQuZmlsbFJlY3QobipyLG8qcixyLHIpfSxmfTt0"
    "LnN0cmluZ1RvQnl0ZXM9KHQuc3RyaW5nVG9CeXRlc0Z1bmNzPXtkZWZhdWx0OmZ1bmN0aW9uKHQpe2Zvcih2YXIgcj1bXSxlPTA7"
    "ZTx0Lmxlbmd0aDtlKz0xKXt2YXIgbj10LmNoYXJDb2RlQXQoZSk7ci5wdXNoKDI1NSZuKX1yZXR1cm4gcn19KS5kZWZhdWx0LHQu"
    "Y3JlYXRlU3RyaW5nVG9CeXRlcz1mdW5jdGlvbih0LHIpe3ZhciBlPWZ1bmN0aW9uKCl7Zm9yKHZhciBlPVModCksbj1mdW5jdGlv"
    "bigpe3ZhciB0PWUucmVhZCgpO2lmKC0xPT10KXRocm93ImVvZiI7cmV0dXJuIHR9LG89MCxpPXt9Ozspe3ZhciBhPWUucmVhZCgp"
    "O2lmKC0xPT1hKWJyZWFrO3ZhciB1PW4oKSxmPW4oKTw8OHxuKCk7aVtTdHJpbmcuZnJvbUNoYXJDb2RlKGE8PDh8dSldPWYsbys9"
    "MX1pZihvIT1yKXRocm93IG8rIiAhPSAiK3I7cmV0dXJuIGl9KCksbj0iPyIuY2hhckNvZGVBdCgwKTtyZXR1cm4gZnVuY3Rpb24o"
    "dCl7Zm9yKHZhciByPVtdLG89MDtvPHQubGVuZ3RoO28rPTEpe3ZhciBpPXQuY2hhckNvZGVBdChvKTtpZihpPDEyOClyLnB1c2go"
    "aSk7ZWxzZXt2YXIgYT1lW3QuY2hhckF0KG8pXTsibnVtYmVyIj09dHlwZW9mIGE/KDI1NSZhKT09YT9yLnB1c2goYSk6KHIucHVz"
    "aChhPj4+OCksci5wdXNoKDI1NSZhKSk6ci5wdXNoKG4pfX1yZXR1cm4gcn19O3ZhciByLGUsbixvLGksYT0xLHU9MixmPTQsYz04"
    "LGc9e0w6MSxNOjAsUTozLEg6Mn0sbD0wLGg9MSxzPTIsdj0zLGQ9NCx3PTUscD02LHk9NyxCPShyPVtbXSxbNiwxOF0sWzYsMjJd"
    "LFs2LDI2XSxbNiwzMF0sWzYsMzRdLFs2LDIyLDM4XSxbNiwyNCw0Ml0sWzYsMjYsNDZdLFs2LDI4LDUwXSxbNiwzMCw1NF0sWzYs"
    "MzIsNThdLFs2LDM0LDYyXSxbNiwyNiw0Niw2Nl0sWzYsMjYsNDgsNzBdLFs2LDI2LDUwLDc0XSxbNiwzMCw1NCw3OF0sWzYsMzAs"
    "NTYsODJdLFs2LDMwLDU4LDg2XSxbNiwzNCw2Miw5MF0sWzYsMjgsNTAsNzIsOTRdLFs2LDI2LDUwLDc0LDk4XSxbNiwzMCw1NCw3"
    "OCwxMDJdLFs2LDI4LDU0LDgwLDEwNl0sWzYsMzIsNTgsODQsMTEwXSxbNiwzMCw1OCw4NiwxMTRdLFs2LDM0LDYyLDkwLDExOF0s"
    "WzYsMjYsNTAsNzQsOTgsMTIyXSxbNiwzMCw1NCw3OCwxMDIsMTI2XSxbNiwyNiw1Miw3OCwxMDQsMTMwXSxbNiwzMCw1Niw4Miwx"
    "MDgsMTM0XSxbNiwzNCw2MCw4NiwxMTIsMTM4XSxbNiwzMCw1OCw4NiwxMTQsMTQyXSxbNiwzNCw2Miw5MCwxMTgsMTQ2XSxbNiwz"
    "MCw1NCw3OCwxMDIsMTI2LDE1MF0sWzYsMjQsNTAsNzYsMTAyLDEyOCwxNTRdLFs2LDI4LDU0LDgwLDEwNiwxMzIsMTU4XSxbNiwz"
    "Miw1OCw4NCwxMTAsMTM2LDE2Ml0sWzYsMjYsNTQsODIsMTEwLDEzOCwxNjZdLFs2LDMwLDU4LDg2LDExNCwxNDIsMTcwXV0sZT0x"
    "MzM1LG49Nzk3MyxpPWZ1bmN0aW9uKHQpe2Zvcih2YXIgcj0wOzAhPXQ7KXIrPTEsdD4+Pj0xO3JldHVybiByfSwobz17fSkuZ2V0"
    "QkNIVHlwZUluZm89ZnVuY3Rpb24odCl7Zm9yKHZhciByPXQ8PDEwO2kociktaShlKT49MDspcl49ZTw8aShyKS1pKGUpO3JldHVy"
    "biAyMTUyMl4odDw8MTB8cil9LG8uZ2V0QkNIVHlwZU51bWJlcj1mdW5jdGlvbih0KXtmb3IodmFyIHI9dDw8MTI7aShyKS1pKG4p"
    "Pj0wOylyXj1uPDxpKHIpLWkobik7cmV0dXJuIHQ8PDEyfHJ9LG8uZ2V0UGF0dGVyblBvc2l0aW9uPWZ1bmN0aW9uKHQpe3JldHVy"
    "biByW3QtMV19LG8uZ2V0TWFza0Z1bmN0aW9uPWZ1bmN0aW9uKHQpe3N3aXRjaCh0KXtjYXNlIGw6cmV0dXJuIGZ1bmN0aW9uKHQs"
    "cil7cmV0dXJuKHQrciklMj09MH07Y2FzZSBoOnJldHVybiBmdW5jdGlvbih0LHIpe3JldHVybiB0JTI9PTB9O2Nhc2UgczpyZXR1"
    "cm4gZnVuY3Rpb24odCxyKXtyZXR1cm4gciUzPT0wfTtjYXNlIHY6cmV0dXJuIGZ1bmN0aW9uKHQscil7cmV0dXJuKHQrciklMz09"
    "MH07Y2FzZSBkOnJldHVybiBmdW5jdGlvbih0LHIpe3JldHVybihNYXRoLmZsb29yKHQvMikrTWF0aC5mbG9vcihyLzMpKSUyPT0w"
    "fTtjYXNlIHc6cmV0dXJuIGZ1bmN0aW9uKHQscil7cmV0dXJuIHQqciUyK3QqciUzPT0wfTtjYXNlIHA6cmV0dXJuIGZ1bmN0aW9u"
    "KHQscil7cmV0dXJuKHQqciUyK3QqciUzKSUyPT0wfTtjYXNlIHk6cmV0dXJuIGZ1bmN0aW9uKHQscil7cmV0dXJuKHQqciUzKyh0"
    "K3IpJTIpJTI9PTB9O2RlZmF1bHQ6dGhyb3ciYmFkIG1hc2tQYXR0ZXJuOiIrdH19LG8uZ2V0RXJyb3JDb3JyZWN0UG9seW5vbWlh"
    "bD1mdW5jdGlvbih0KXtmb3IodmFyIHI9ayhbMV0sMCksZT0wO2U8dDtlKz0xKXI9ci5tdWx0aXBseShrKFsxLEMuZ2V4cChlKV0s"
    "MCkpO3JldHVybiByfSxvLmdldExlbmd0aEluQml0cz1mdW5jdGlvbih0LHIpe2lmKDE8PXImJnI8MTApc3dpdGNoKHQpe2Nhc2Ug"
    "YTpyZXR1cm4gMTA7Y2FzZSB1OnJldHVybiA5O2Nhc2UgZjpjYXNlIGM6cmV0dXJuIDg7ZGVmYXVsdDp0aHJvdyJtb2RlOiIrdH1l"
    "bHNlIGlmKHI8Mjcpc3dpdGNoKHQpe2Nhc2UgYTpyZXR1cm4gMTI7Y2FzZSB1OnJldHVybiAxMTtjYXNlIGY6cmV0dXJuIDE2O2Nh"
    "c2UgYzpyZXR1cm4gMTA7ZGVmYXVsdDp0aHJvdyJtb2RlOiIrdH1lbHNle2lmKCEocjw0MSkpdGhyb3cidHlwZToiK3I7c3dpdGNo"
    "KHQpe2Nhc2UgYTpyZXR1cm4gMTQ7Y2FzZSB1OnJldHVybiAxMztjYXNlIGY6cmV0dXJuIDE2O2Nhc2UgYzpyZXR1cm4gMTI7ZGVm"
    "YXVsdDp0aHJvdyJtb2RlOiIrdH19fSxvLmdldExvc3RQb2ludD1mdW5jdGlvbih0KXtmb3IodmFyIHI9dC5nZXRNb2R1bGVDb3Vu"
    "dCgpLGU9MCxuPTA7bjxyO24rPTEpZm9yKHZhciBvPTA7bzxyO28rPTEpe2Zvcih2YXIgaT0wLGE9dC5pc0RhcmsobixvKSx1PS0x"
    "O3U8PTE7dSs9MSlpZighKG4rdTwwfHxyPD1uK3UpKWZvcih2YXIgZj0tMTtmPD0xO2YrPTEpbytmPDB8fHI8PW8rZnx8MD09dSYm"
    "MD09Znx8YT09dC5pc0Rhcmsobit1LG8rZikmJihpKz0xKTtpPjUmJihlKz0zK2ktNSl9Zm9yKG49MDtuPHItMTtuKz0xKWZvcihv"
    "PTA7bzxyLTE7bys9MSl7dmFyIGM9MDt0LmlzRGFyayhuLG8pJiYoYys9MSksdC5pc0RhcmsobisxLG8pJiYoYys9MSksdC5pc0Rh"
    "cmsobixvKzEpJiYoYys9MSksdC5pc0RhcmsobisxLG8rMSkmJihjKz0xKSwwIT1jJiY0IT1jfHwoZSs9Myl9Zm9yKG49MDtuPHI7"
    "bis9MSlmb3Iobz0wO288ci02O28rPTEpdC5pc0RhcmsobixvKSYmIXQuaXNEYXJrKG4sbysxKSYmdC5pc0RhcmsobixvKzIpJiZ0"
    "LmlzRGFyayhuLG8rMykmJnQuaXNEYXJrKG4sbys0KSYmIXQuaXNEYXJrKG4sbys1KSYmdC5pc0RhcmsobixvKzYpJiYoZSs9NDAp"
    "O2ZvcihvPTA7bzxyO28rPTEpZm9yKG49MDtuPHItNjtuKz0xKXQuaXNEYXJrKG4sbykmJiF0LmlzRGFyayhuKzEsbykmJnQuaXNE"
    "YXJrKG4rMixvKSYmdC5pc0RhcmsobiszLG8pJiZ0LmlzRGFyayhuKzQsbykmJiF0LmlzRGFyayhuKzUsbykmJnQuaXNEYXJrKG4r"
    "NixvKSYmKGUrPTQwKTt2YXIgZz0wO2ZvcihvPTA7bzxyO28rPTEpZm9yKG49MDtuPHI7bis9MSl0LmlzRGFyayhuLG8pJiYoZys9"
    "MSk7cmV0dXJuIGUrPU1hdGguYWJzKDEwMCpnL3Ivci01MCkvNSoxMH0sbyksQz1mdW5jdGlvbigpe2Zvcih2YXIgdD1uZXcgQXJy"
    "YXkoMjU2KSxyPW5ldyBBcnJheSgyNTYpLGU9MDtlPDg7ZSs9MSl0W2VdPTE8PGU7Zm9yKGU9ODtlPDI1NjtlKz0xKXRbZV09dFtl"
    "LTRdXnRbZS01XV50W2UtNl1edFtlLThdO2ZvcihlPTA7ZTwyNTU7ZSs9MSlyW3RbZV1dPWU7dmFyIG49e2dsb2c6ZnVuY3Rpb24o"
    "dCl7aWYodDwxKXRocm93Imdsb2coIit0KyIpIjtyZXR1cm4gclt0XX0sZ2V4cDpmdW5jdGlvbihyKXtmb3IoO3I8MDspcis9MjU1"
    "O2Zvcig7cj49MjU2OylyLT0yNTU7cmV0dXJuIHRbcl19fTtyZXR1cm4gbn0oKTtmdW5jdGlvbiBrKHQscil7aWYodm9pZCAwPT09"
    "dC5sZW5ndGgpdGhyb3cgdC5sZW5ndGgrIi8iK3I7dmFyIGU9ZnVuY3Rpb24oKXtmb3IodmFyIGU9MDtlPHQubGVuZ3RoJiYwPT10"
    "W2VdOyllKz0xO2Zvcih2YXIgbj1uZXcgQXJyYXkodC5sZW5ndGgtZStyKSxvPTA7bzx0Lmxlbmd0aC1lO28rPTEpbltvXT10W28r"
    "ZV07cmV0dXJuIG59KCksbj17Z2V0QXQ6ZnVuY3Rpb24odCl7cmV0dXJuIGVbdF19LGdldExlbmd0aDpmdW5jdGlvbigpe3JldHVy"
    "biBlLmxlbmd0aH0sbXVsdGlwbHk6ZnVuY3Rpb24odCl7Zm9yKHZhciByPW5ldyBBcnJheShuLmdldExlbmd0aCgpK3QuZ2V0TGVu"
    "Z3RoKCktMSksZT0wO2U8bi5nZXRMZW5ndGgoKTtlKz0xKWZvcih2YXIgbz0wO288dC5nZXRMZW5ndGgoKTtvKz0xKXJbZStvXV49"
    "Qy5nZXhwKEMuZ2xvZyhuLmdldEF0KGUpKStDLmdsb2codC5nZXRBdChvKSkpO3JldHVybiBrKHIsMCl9LG1vZDpmdW5jdGlvbih0"
    "KXtpZihuLmdldExlbmd0aCgpLXQuZ2V0TGVuZ3RoKCk8MClyZXR1cm4gbjtmb3IodmFyIHI9Qy5nbG9nKG4uZ2V0QXQoMCkpLUMu"
    "Z2xvZyh0LmdldEF0KDApKSxlPW5ldyBBcnJheShuLmdldExlbmd0aCgpKSxvPTA7bzxuLmdldExlbmd0aCgpO28rPTEpZVtvXT1u"
    "LmdldEF0KG8pO2ZvcihvPTA7bzx0LmdldExlbmd0aCgpO28rPTEpZVtvXV49Qy5nZXhwKEMuZ2xvZyh0LmdldEF0KG8pKStyKTty"
    "ZXR1cm4gayhlLDApLm1vZCh0KX19O3JldHVybiBufXZhciBBPWZ1bmN0aW9uKCl7dmFyIHQ9W1sxLDI2LDE5XSxbMSwyNiwxNl0s"
    "WzEsMjYsMTNdLFsxLDI2LDldLFsxLDQ0LDM0XSxbMSw0NCwyOF0sWzEsNDQsMjJdLFsxLDQ0LDE2XSxbMSw3MCw1NV0sWzEsNzAs"
    "NDRdLFsyLDM1LDE3XSxbMiwzNSwxM10sWzEsMTAwLDgwXSxbMiw1MCwzMl0sWzIsNTAsMjRdLFs0LDI1LDldLFsxLDEzNCwxMDhd"
    "LFsyLDY3LDQzXSxbMiwzMywxNSwyLDM0LDE2XSxbMiwzMywxMSwyLDM0LDEyXSxbMiw4Niw2OF0sWzQsNDMsMjddLFs0LDQzLDE5"
    "XSxbNCw0MywxNV0sWzIsOTgsNzhdLFs0LDQ5LDMxXSxbMiwzMiwxNCw0LDMzLDE1XSxbNCwzOSwxMywxLDQwLDE0XSxbMiwxMjEs"
    "OTddLFsyLDYwLDM4LDIsNjEsMzldLFs0LDQwLDE4LDIsNDEsMTldLFs0LDQwLDE0LDIsNDEsMTVdLFsyLDE0NiwxMTZdLFszLDU4"
    "LDM2LDIsNTksMzddLFs0LDM2LDE2LDQsMzcsMTddLFs0LDM2LDEyLDQsMzcsMTNdLFsyLDg2LDY4LDIsODcsNjldLFs0LDY5LDQz"
    "LDEsNzAsNDRdLFs2LDQzLDE5LDIsNDQsMjBdLFs2LDQzLDE1LDIsNDQsMTZdLFs0LDEwMSw4MV0sWzEsODAsNTAsNCw4MSw1MV0s"
    "WzQsNTAsMjIsNCw1MSwyM10sWzMsMzYsMTIsOCwzNywxM10sWzIsMTE2LDkyLDIsMTE3LDkzXSxbNiw1OCwzNiwyLDU5LDM3XSxb"
    "NCw0NiwyMCw2LDQ3LDIxXSxbNyw0MiwxNCw0LDQzLDE1XSxbNCwxMzMsMTA3XSxbOCw1OSwzNywxLDYwLDM4XSxbOCw0NCwyMCw0"
    "LDQ1LDIxXSxbMTIsMzMsMTEsNCwzNCwxMl0sWzMsMTQ1LDExNSwxLDE0NiwxMTZdLFs0LDY0LDQwLDUsNjUsNDFdLFsxMSwzNiwx"
    "Niw1LDM3LDE3XSxbMTEsMzYsMTIsNSwzNywxM10sWzUsMTA5LDg3LDEsMTEwLDg4XSxbNSw2NSw0MSw1LDY2LDQyXSxbNSw1NCwy"
    "NCw3LDU1LDI1XSxbMTEsMzYsMTIsNywzNywxM10sWzUsMTIyLDk4LDEsMTIzLDk5XSxbNyw3Myw0NSwzLDc0LDQ2XSxbMTUsNDMs"
    "MTksMiw0NCwyMF0sWzMsNDUsMTUsMTMsNDYsMTZdLFsxLDEzNSwxMDcsNSwxMzYsMTA4XSxbMTAsNzQsNDYsMSw3NSw0N10sWzEs"
    "NTAsMjIsMTUsNTEsMjNdLFsyLDQyLDE0LDE3LDQzLDE1XSxbNSwxNTAsMTIwLDEsMTUxLDEyMV0sWzksNjksNDMsNCw3MCw0NF0s"
    "WzE3LDUwLDIyLDEsNTEsMjNdLFsyLDQyLDE0LDE5LDQzLDE1XSxbMywxNDEsMTEzLDQsMTQyLDExNF0sWzMsNzAsNDQsMTEsNzEs"
    "NDVdLFsxNyw0NywyMSw0LDQ4LDIyXSxbOSwzOSwxMywxNiw0MCwxNF0sWzMsMTM1LDEwNyw1LDEzNiwxMDhdLFszLDY3LDQxLDEz"
    "LDY4LDQyXSxbMTUsNTQsMjQsNSw1NSwyNV0sWzE1LDQzLDE1LDEwLDQ0LDE2XSxbNCwxNDQsMTE2LDQsMTQ1LDExN10sWzE3LDY4"
    "LDQyXSxbMTcsNTAsMjIsNiw1MSwyM10sWzE5LDQ2LDE2LDYsNDcsMTddLFsyLDEzOSwxMTEsNywxNDAsMTEyXSxbMTcsNzQsNDZd"
    "LFs3LDU0LDI0LDE2LDU1LDI1XSxbMzQsMzcsMTNdLFs0LDE1MSwxMjEsNSwxNTIsMTIyXSxbNCw3NSw0NywxNCw3Niw0OF0sWzEx"
    "LDU0LDI0LDE0LDU1LDI1XSxbMTYsNDUsMTUsMTQsNDYsMTZdLFs2LDE0NywxMTcsNCwxNDgsMTE4XSxbNiw3Myw0NSwxNCw3NCw0"
    "Nl0sWzExLDU0LDI0LDE2LDU1LDI1XSxbMzAsNDYsMTYsMiw0NywxN10sWzgsMTMyLDEwNiw0LDEzMywxMDddLFs4LDc1LDQ3LDEz"
    "LDc2LDQ4XSxbNyw1NCwyNCwyMiw1NSwyNV0sWzIyLDQ1LDE1LDEzLDQ2LDE2XSxbMTAsMTQyLDExNCwyLDE0MywxMTVdLFsxOSw3"
    "NCw0Niw0LDc1LDQ3XSxbMjgsNTAsMjIsNiw1MSwyM10sWzMzLDQ2LDE2LDQsNDcsMTddLFs4LDE1MiwxMjIsNCwxNTMsMTIzXSxb"
    "MjIsNzMsNDUsMyw3NCw0Nl0sWzgsNTMsMjMsMjYsNTQsMjRdLFsxMiw0NSwxNSwyOCw0NiwxNl0sWzMsMTQ3LDExNywxMCwxNDgs"
    "MTE4XSxbMyw3Myw0NSwyMyw3NCw0Nl0sWzQsNTQsMjQsMzEsNTUsMjVdLFsxMSw0NSwxNSwzMSw0NiwxNl0sWzcsMTQ2LDExNiw3"
    "LDE0NywxMTddLFsyMSw3Myw0NSw3LDc0LDQ2XSxbMSw1MywyMywzNyw1NCwyNF0sWzE5LDQ1LDE1LDI2LDQ2LDE2XSxbNSwxNDUs"
    "MTE1LDEwLDE0NiwxMTZdLFsxOSw3NSw0NywxMCw3Niw0OF0sWzE1LDU0LDI0LDI1LDU1LDI1XSxbMjMsNDUsMTUsMjUsNDYsMTZd"
    "LFsxMywxNDUsMTE1LDMsMTQ2LDExNl0sWzIsNzQsNDYsMjksNzUsNDddLFs0Miw1NCwyNCwxLDU1LDI1XSxbMjMsNDUsMTUsMjgs"
    "NDYsMTZdLFsxNywxNDUsMTE1XSxbMTAsNzQsNDYsMjMsNzUsNDddLFsxMCw1NCwyNCwzNSw1NSwyNV0sWzE5LDQ1LDE1LDM1LDQ2"
    "LDE2XSxbMTcsMTQ1LDExNSwxLDE0NiwxMTZdLFsxNCw3NCw0NiwyMSw3NSw0N10sWzI5LDU0LDI0LDE5LDU1LDI1XSxbMTEsNDUs"
    "MTUsNDYsNDYsMTZdLFsxMywxNDUsMTE1LDYsMTQ2LDExNl0sWzE0LDc0LDQ2LDIzLDc1LDQ3XSxbNDQsNTQsMjQsNyw1NSwyNV0s"
    "WzU5LDQ2LDE2LDEsNDcsMTddLFsxMiwxNTEsMTIxLDcsMTUyLDEyMl0sWzEyLDc1LDQ3LDI2LDc2LDQ4XSxbMzksNTQsMjQsMTQs"
    "NTUsMjVdLFsyMiw0NSwxNSw0MSw0NiwxNl0sWzYsMTUxLDEyMSwxNCwxNTIsMTIyXSxbNiw3NSw0NywzNCw3Niw0OF0sWzQ2LDU0"
    "LDI0LDEwLDU1LDI1XSxbMiw0NSwxNSw2NCw0NiwxNl0sWzE3LDE1MiwxMjIsNCwxNTMsMTIzXSxbMjksNzQsNDYsMTQsNzUsNDdd"
    "LFs0OSw1NCwyNCwxMCw1NSwyNV0sWzI0LDQ1LDE1LDQ2LDQ2LDE2XSxbNCwxNTIsMTIyLDE4LDE1MywxMjNdLFsxMyw3NCw0Niwz"
    "Miw3NSw0N10sWzQ4LDU0LDI0LDE0LDU1LDI1XSxbNDIsNDUsMTUsMzIsNDYsMTZdLFsyMCwxNDcsMTE3LDQsMTQ4LDExOF0sWzQw"
    "LDc1LDQ3LDcsNzYsNDhdLFs0Myw1NCwyNCwyMiw1NSwyNV0sWzEwLDQ1LDE1LDY3LDQ2LDE2XSxbMTksMTQ4LDExOCw2LDE0OSwx"
    "MTldLFsxOCw3NSw0NywzMSw3Niw0OF0sWzM0LDU0LDI0LDM0LDU1LDI1XSxbMjAsNDUsMTUsNjEsNDYsMTZdXSxyPWZ1bmN0aW9u"
    "KHQscil7dmFyIGU9e307cmV0dXJuIGUudG90YWxDb3VudD10LGUuZGF0YUNvdW50PXIsZX0sZT17fTtyZXR1cm4gZS5nZXRSU0Js"
    "b2Nrcz1mdW5jdGlvbihlLG4pe3ZhciBvPWZ1bmN0aW9uKHIsZSl7c3dpdGNoKGUpe2Nhc2UgZy5MOnJldHVybiB0WzQqKHItMSkr"
    "MF07Y2FzZSBnLk06cmV0dXJuIHRbNCooci0xKSsxXTtjYXNlIGcuUTpyZXR1cm4gdFs0KihyLTEpKzJdO2Nhc2UgZy5IOnJldHVy"
    "biB0WzQqKHItMSkrM107ZGVmYXVsdDpyZXR1cm59fShlLG4pO2lmKHZvaWQgMD09PW8pdGhyb3ciYmFkIHJzIGJsb2NrIEAgdHlw"
    "ZU51bWJlcjoiK2UrIi9lcnJvckNvcnJlY3Rpb25MZXZlbDoiK247Zm9yKHZhciBpPW8ubGVuZ3RoLzMsYT1bXSx1PTA7dTxpO3Ur"
    "PTEpZm9yKHZhciBmPW9bMyp1KzBdLGM9b1szKnUrMV0sbD1vWzMqdSsyXSxoPTA7aDxmO2grPTEpYS5wdXNoKHIoYyxsKSk7cmV0"
    "dXJuIGF9LGV9KCksYj1mdW5jdGlvbigpe3ZhciB0PVtdLHI9MCxlPXtnZXRCdWZmZXI6ZnVuY3Rpb24oKXtyZXR1cm4gdH0sZ2V0"
    "QXQ6ZnVuY3Rpb24ocil7dmFyIGU9TWF0aC5mbG9vcihyLzgpO3JldHVybiAxPT0odFtlXT4+PjctciU4JjEpfSxwdXQ6ZnVuY3Rp"
    "b24odCxyKXtmb3IodmFyIG49MDtuPHI7bis9MSllLnB1dEJpdCgxPT0odD4+PnItbi0xJjEpKX0sZ2V0TGVuZ3RoSW5CaXRzOmZ1"
    "bmN0aW9uKCl7cmV0dXJuIHJ9LHB1dEJpdDpmdW5jdGlvbihlKXt2YXIgbj1NYXRoLmZsb29yKHIvOCk7dC5sZW5ndGg8PW4mJnQu"
    "cHVzaCgwKSxlJiYodFtuXXw9MTI4Pj4+ciU4KSxyKz0xfX07cmV0dXJuIGV9LE09ZnVuY3Rpb24odCl7dmFyIHI9YSxlPXQsbj17"
    "Z2V0TW9kZTpmdW5jdGlvbigpe3JldHVybiByfSxnZXRMZW5ndGg6ZnVuY3Rpb24odCl7cmV0dXJuIGUubGVuZ3RofSx3cml0ZTpm"
    "dW5jdGlvbih0KXtmb3IodmFyIHI9ZSxuPTA7bisyPHIubGVuZ3RoOyl0LnB1dChvKHIuc3Vic3RyaW5nKG4sbiszKSksMTApLG4r"
    "PTM7bjxyLmxlbmd0aCYmKHIubGVuZ3RoLW49PTE/dC5wdXQobyhyLnN1YnN0cmluZyhuLG4rMSkpLDQpOnIubGVuZ3RoLW49PTIm"
    "JnQucHV0KG8oci5zdWJzdHJpbmcobixuKzIpKSw3KSl9fSxvPWZ1bmN0aW9uKHQpe2Zvcih2YXIgcj0wLGU9MDtlPHQubGVuZ3Ro"
    "O2UrPTEpcj0xMCpyK2kodC5jaGFyQXQoZSkpO3JldHVybiByfSxpPWZ1bmN0aW9uKHQpe2lmKCIwIjw9dCYmdDw9IjkiKXJldHVy"
    "biB0LmNoYXJDb2RlQXQoMCktIjAiLmNoYXJDb2RlQXQoMCk7dGhyb3ciaWxsZWdhbCBjaGFyIDoiK3R9O3JldHVybiBufSx4PWZ1"
    "bmN0aW9uKHQpe3ZhciByPXUsZT10LG49e2dldE1vZGU6ZnVuY3Rpb24oKXtyZXR1cm4gcn0sZ2V0TGVuZ3RoOmZ1bmN0aW9uKHQp"
    "e3JldHVybiBlLmxlbmd0aH0sd3JpdGU6ZnVuY3Rpb24odCl7Zm9yKHZhciByPWUsbj0wO24rMTxyLmxlbmd0aDspdC5wdXQoNDUq"
    "byhyLmNoYXJBdChuKSkrbyhyLmNoYXJBdChuKzEpKSwxMSksbis9MjtuPHIubGVuZ3RoJiZ0LnB1dChvKHIuY2hhckF0KG4pKSw2"
    "KX19LG89ZnVuY3Rpb24odCl7aWYoIjAiPD10JiZ0PD0iOSIpcmV0dXJuIHQuY2hhckNvZGVBdCgwKS0iMCIuY2hhckNvZGVBdCgw"
    "KTtpZigiQSI8PXQmJnQ8PSJaIilyZXR1cm4gdC5jaGFyQ29kZUF0KDApLSJBIi5jaGFyQ29kZUF0KDApKzEwO3N3aXRjaCh0KXtj"
    "YXNlIiAiOnJldHVybiAzNjtjYXNlIiQiOnJldHVybiAzNztjYXNlIiUiOnJldHVybiAzODtjYXNlIioiOnJldHVybiAzOTtjYXNl"
    "IisiOnJldHVybiA0MDtjYXNlIi0iOnJldHVybiA0MTtjYXNlIi4iOnJldHVybiA0MjtjYXNlIi8iOnJldHVybiA0MztjYXNlIjoi"
    "OnJldHVybiA0NDtkZWZhdWx0OnRocm93ImlsbGVnYWwgY2hhciA6Iit0fX07cmV0dXJuIG59LG09ZnVuY3Rpb24ocil7dmFyIGU9"
    "ZixuPXQuc3RyaW5nVG9CeXRlcyhyKSxvPXtnZXRNb2RlOmZ1bmN0aW9uKCl7cmV0dXJuIGV9LGdldExlbmd0aDpmdW5jdGlvbih0"
    "KXtyZXR1cm4gbi5sZW5ndGh9LHdyaXRlOmZ1bmN0aW9uKHQpe2Zvcih2YXIgcj0wO3I8bi5sZW5ndGg7cis9MSl0LnB1dChuW3Jd"
    "LDgpfX07cmV0dXJuIG99LEw9ZnVuY3Rpb24ocil7dmFyIGU9YyxuPXQuc3RyaW5nVG9CeXRlc0Z1bmNzLlNKSVM7aWYoIW4pdGhy"
    "b3cic2ppcyBub3Qgc3VwcG9ydGVkLiI7IWZ1bmN0aW9uKCl7dmFyIHQ9bigi5Y+LIik7aWYoMiE9dC5sZW5ndGh8fDM4NzI2IT0o"
    "dFswXTw8OHx0WzFdKSl0aHJvdyJzamlzIG5vdCBzdXBwb3J0ZWQuIn0oKTt2YXIgbz1uKHIpLGk9e2dldE1vZGU6ZnVuY3Rpb24o"
    "KXtyZXR1cm4gZX0sZ2V0TGVuZ3RoOmZ1bmN0aW9uKHQpe3JldHVybn5+KG8ubGVuZ3RoLzIpfSx3cml0ZTpmdW5jdGlvbih0KXtm"
    "b3IodmFyIHI9byxlPTA7ZSsxPHIubGVuZ3RoOyl7dmFyIG49KDI1NSZyW2VdKTw8OHwyNTUmcltlKzFdO2lmKDMzMDg4PD1uJiZu"
    "PD00MDk1NiluLT0zMzA4ODtlbHNle2lmKCEoNTc0MDg8PW4mJm48PTYwMzUxKSl0aHJvdyJpbGxlZ2FsIGNoYXIgYXQgIisoZSsx"
    "KSsiLyIrbjtuLT00OTQ3Mn1uPTE5Mioobj4+PjgmMjU1KSsoMjU1Jm4pLHQucHV0KG4sMTMpLGUrPTJ9aWYoZTxyLmxlbmd0aCl0"
    "aHJvdyJpbGxlZ2FsIGNoYXIgYXQgIisoZSsxKX19O3JldHVybiBpfSxEPWZ1bmN0aW9uKCl7dmFyIHQ9W10scj17d3JpdGVCeXRl"
    "OmZ1bmN0aW9uKHIpe3QucHVzaCgyNTUmcil9LHdyaXRlU2hvcnQ6ZnVuY3Rpb24odCl7ci53cml0ZUJ5dGUodCksci53cml0ZUJ5"
    "dGUodD4+PjgpfSx3cml0ZUJ5dGVzOmZ1bmN0aW9uKHQsZSxuKXtlPWV8fDAsbj1ufHx0Lmxlbmd0aDtmb3IodmFyIG89MDtvPG47"
    "bys9MSlyLndyaXRlQnl0ZSh0W28rZV0pfSx3cml0ZVN0cmluZzpmdW5jdGlvbih0KXtmb3IodmFyIGU9MDtlPHQubGVuZ3RoO2Ur"
    "PTEpci53cml0ZUJ5dGUodC5jaGFyQ29kZUF0KGUpKX0sdG9CeXRlQXJyYXk6ZnVuY3Rpb24oKXtyZXR1cm4gdH0sdG9TdHJpbmc6"
    "ZnVuY3Rpb24oKXt2YXIgcj0iIjtyKz0iWyI7Zm9yKHZhciBlPTA7ZTx0Lmxlbmd0aDtlKz0xKWU+MCYmKHIrPSIsIikscis9dFtl"
    "XTtyZXR1cm4gcis9Il0ifX07cmV0dXJuIHJ9LFM9ZnVuY3Rpb24odCl7dmFyIHI9dCxlPTAsbj0wLG89MCxpPXtyZWFkOmZ1bmN0"
    "aW9uKCl7Zm9yKDtvPDg7KXtpZihlPj1yLmxlbmd0aCl7aWYoMD09bylyZXR1cm4tMTt0aHJvdyJ1bmV4cGVjdGVkIGVuZCBvZiBm"
    "aWxlLi8iK299dmFyIHQ9ci5jaGFyQXQoZSk7aWYoZSs9MSwiPSI9PXQpcmV0dXJuIG89MCwtMTt0Lm1hdGNoKC9eXHMkLyl8fChu"
    "PW48PDZ8YSh0LmNoYXJDb2RlQXQoMCkpLG8rPTYpfXZhciBpPW4+Pj5vLTgmMjU1O3JldHVybiBvLT04LGl9fSxhPWZ1bmN0aW9u"
    "KHQpe2lmKDY1PD10JiZ0PD05MClyZXR1cm4gdC02NTtpZig5Nzw9dCYmdDw9MTIyKXJldHVybiB0LTk3KzI2O2lmKDQ4PD10JiZ0"
    "PD01NylyZXR1cm4gdC00OCs1MjtpZig0Mz09dClyZXR1cm4gNjI7aWYoNDc9PXQpcmV0dXJuIDYzO3Rocm93ImM6Iit0fTtyZXR1"
    "cm4gaX0sST1mdW5jdGlvbih0LHIsZSl7Zm9yKHZhciBuPWZ1bmN0aW9uKHQscil7dmFyIGU9dCxuPXIsbz1uZXcgQXJyYXkodCpy"
    "KSxpPXtzZXRQaXhlbDpmdW5jdGlvbih0LHIsbil7b1tyKmUrdF09bn0sd3JpdGU6ZnVuY3Rpb24odCl7dC53cml0ZVN0cmluZygi"
    "R0lGODdhIiksdC53cml0ZVNob3J0KGUpLHQud3JpdGVTaG9ydChuKSx0LndyaXRlQnl0ZSgxMjgpLHQud3JpdGVCeXRlKDApLHQu"
    "d3JpdGVCeXRlKDApLHQud3JpdGVCeXRlKDApLHQud3JpdGVCeXRlKDApLHQud3JpdGVCeXRlKDApLHQud3JpdGVCeXRlKDI1NSks"
    "dC53cml0ZUJ5dGUoMjU1KSx0LndyaXRlQnl0ZSgyNTUpLHQud3JpdGVTdHJpbmcoIiwiKSx0LndyaXRlU2hvcnQoMCksdC53cml0"
    "ZVNob3J0KDApLHQud3JpdGVTaG9ydChlKSx0LndyaXRlU2hvcnQobiksdC53cml0ZUJ5dGUoMCk7dmFyIHI9YSgyKTt0LndyaXRl"
    "Qnl0ZSgyKTtmb3IodmFyIG89MDtyLmxlbmd0aC1vPjI1NTspdC53cml0ZUJ5dGUoMjU1KSx0LndyaXRlQnl0ZXMocixvLDI1NSks"
    "bys9MjU1O3Qud3JpdGVCeXRlKHIubGVuZ3RoLW8pLHQud3JpdGVCeXRlcyhyLG8sci5sZW5ndGgtbyksdC53cml0ZUJ5dGUoMCks"
    "dC53cml0ZVN0cmluZygiOyIpfX0sYT1mdW5jdGlvbih0KXtmb3IodmFyIHI9MTw8dCxlPTErKDE8PHQpLG49dCsxLGk9dSgpLGE9"
    "MDthPHI7YSs9MSlpLmFkZChTdHJpbmcuZnJvbUNoYXJDb2RlKGEpKTtpLmFkZChTdHJpbmcuZnJvbUNoYXJDb2RlKHIpKSxpLmFk"
    "ZChTdHJpbmcuZnJvbUNoYXJDb2RlKGUpKTt2YXIgZixjLGcsbD1EKCksaD0oZj1sLGM9MCxnPTAse3dyaXRlOmZ1bmN0aW9uKHQs"
    "cil7aWYodD4+PnIhPTApdGhyb3cibGVuZ3RoIG92ZXIiO2Zvcig7YytyPj04OylmLndyaXRlQnl0ZSgyNTUmKHQ8PGN8ZykpLHIt"
    "PTgtYyx0Pj4+PTgtYyxnPTAsYz0wO2d8PXQ8PGMsYys9cn0sZmx1c2g6ZnVuY3Rpb24oKXtjPjAmJmYud3JpdGVCeXRlKGcpfX0p"
    "O2gud3JpdGUocixuKTt2YXIgcz0wLHY9U3RyaW5nLmZyb21DaGFyQ29kZShvW3NdKTtmb3Iocys9MTtzPG8ubGVuZ3RoOyl7dmFy"
    "IGQ9U3RyaW5nLmZyb21DaGFyQ29kZShvW3NdKTtzKz0xLGkuY29udGFpbnModitkKT92Kz1kOihoLndyaXRlKGkuaW5kZXhPZih2"
    "KSxuKSxpLnNpemUoKTw0MDk1JiYoaS5zaXplKCk9PTE8PG4mJihuKz0xKSxpLmFkZCh2K2QpKSx2PWQpfXJldHVybiBoLndyaXRl"
    "KGkuaW5kZXhPZih2KSxuKSxoLndyaXRlKGUsbiksaC5mbHVzaCgpLGwudG9CeXRlQXJyYXkoKX0sdT1mdW5jdGlvbigpe3ZhciB0"
    "PXt9LHI9MCxlPXthZGQ6ZnVuY3Rpb24obil7aWYoZS5jb250YWlucyhuKSl0aHJvdyJkdXAga2V5OiIrbjt0W25dPXIscis9MX0s"
    "c2l6ZTpmdW5jdGlvbigpe3JldHVybiByfSxpbmRleE9mOmZ1bmN0aW9uKHIpe3JldHVybiB0W3JdfSxjb250YWluczpmdW5jdGlv"
    "bihyKXtyZXR1cm4gdm9pZCAwIT09dFtyXX19O3JldHVybiBlfTtyZXR1cm4gaX0odCxyKSxvPTA7bzxyO28rPTEpZm9yKHZhciBp"
    "PTA7aTx0O2krPTEpbi5zZXRQaXhlbChpLG8sZShpLG8pKTt2YXIgYT1EKCk7bi53cml0ZShhKTtmb3IodmFyIHU9ZnVuY3Rpb24o"
    "KXt2YXIgdD0wLHI9MCxlPTAsbj0iIixvPXt9LGk9ZnVuY3Rpb24odCl7bis9U3RyaW5nLmZyb21DaGFyQ29kZShhKDYzJnQpKX0s"
    "YT1mdW5jdGlvbih0KXtpZih0PDApO2Vsc2V7aWYodDwyNilyZXR1cm4gNjUrdDtpZih0PDUyKXJldHVybiB0LTI2Kzk3O2lmKHQ8"
    "NjIpcmV0dXJuIHQtNTIrNDg7aWYoNjI9PXQpcmV0dXJuIDQzO2lmKDYzPT10KXJldHVybiA0N310aHJvdyJuOiIrdH07cmV0dXJu"
    "IG8ud3JpdGVCeXRlPWZ1bmN0aW9uKG4pe2Zvcih0PXQ8PDh8MjU1Jm4scis9OCxlKz0xO3I+PTY7KWkodD4+PnItNiksci09Nn0s"
    "by5mbHVzaD1mdW5jdGlvbigpe2lmKHI+MCYmKGkodDw8Ni1yKSx0PTAscj0wKSxlJTMhPTApZm9yKHZhciBvPTMtZSUzLGE9MDth"
    "PG87YSs9MSluKz0iPSJ9LG8udG9TdHJpbmc9ZnVuY3Rpb24oKXtyZXR1cm4gbn0sb30oKSxmPWEudG9CeXRlQXJyYXkoKSxjPTA7"
    "YzxmLmxlbmd0aDtjKz0xKXUud3JpdGVCeXRlKGZbY10pO3JldHVybiB1LmZsdXNoKCksImRhdGE6aW1hZ2UvZ2lmO2Jhc2U2NCwi"
    "K3V9O3JldHVybiB0fSgpO3FyY29kZS5zdHJpbmdUb0J5dGVzRnVuY3NbIlVURi04Il09ZnVuY3Rpb24odCl7cmV0dXJuIGZ1bmN0"
    "aW9uKHQpe2Zvcih2YXIgcj1bXSxlPTA7ZTx0Lmxlbmd0aDtlKyspe3ZhciBuPXQuY2hhckNvZGVBdChlKTtuPDEyOD9yLnB1c2go"
    "bik6bjwyMDQ4P3IucHVzaCgxOTJ8bj4+NiwxMjh8NjMmbik6bjw1NTI5Nnx8bj49NTczNDQ/ci5wdXNoKDIyNHxuPj4xMiwxMjh8"
    "bj4+NiY2MywxMjh8NjMmbik6KGUrKyxuPTY1NTM2KygoMTAyMyZuKTw8MTB8MTAyMyZ0LmNoYXJDb2RlQXQoZSkpLHIucHVzaCgy"
    "NDB8bj4+MTgsMTI4fG4+PjEyJjYzLDEyOHxuPj42JjYzLDEyOHw2MyZuKSl9cmV0dXJuIHJ9KHQpfSxmdW5jdGlvbih0KXsiZnVu"
    "Y3Rpb24iPT10eXBlb2YgZGVmaW5lJiZkZWZpbmUuYW1kP2RlZmluZShbXSx0KToib2JqZWN0Ij09dHlwZW9mIGV4cG9ydHMmJiht"
    "b2R1bGUuZXhwb3J0cz10KCkpfSgoZnVuY3Rpb24oKXtyZXR1cm4gcXJjb2RlfSkpOwovLyMgc291cmNlTWFwcGluZ1VSTD0vc20v"
    "MjZiNGIwZDBiMWUyODNkNmIzZWM5ODU3YWM1OTdkN2E2MGM3NmFjMTdiZTFlZjRjOTY1ZjAzMDg2ZGU0MjZiYi5tYXA=")
PAGE = PAGE_SRC.replace("__QR_B64__", QR_LIB_B64)


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):  # 安静模式：只打异常不打刷屏日志
        pass

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _auth_user(self) -> dict | None:
        """解析 Authorization: Bearer <access>，合法返回 {uid, phone}。"""
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Bearer "):
            return None
        return auth.verify_access(h[7:].strip())

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._html(PAGE)
        elif path == "/api/health":
            self._json({"ok": True, "sessions": len(_SESSIONS), "llm": _llm_state()})
        elif path == "/api/lan":
            # 手机互通：顶栏「手机访问」弹窗据此显示地址与二维码。
            # 只有局域网地址，不含任何用户数据，无需鉴权。
            self._json({"ok": True, "url": _LAN_URL})
        elif path == "/api/progress":
            # 等待页轮询：只返回"现在在做什么"和已等待秒数，不含任何用户数据
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            sid = (q.get("sid") or [""])[0]
            st = progress.get(sid) if sid else None
            self._json({"ok": True, "progress": st})
        elif path == "/api/me":
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录"}, 401)
                return
            acct = auth.get_account(user["uid"])
            self._json({"ok": True, "phone": auth.mask_phone(user["phone"]),
                        "balance": acct["balance"], "frozen": acct["frozen"],
                        "unlimited": acct["unlimited"],
                        "bonus": auth.BONUS_CREDITS,
                        "daily_cap": auth.DAILY_CAP_CREDITS})
        elif path == "/api/conversations":
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录后再使用"}, 401)
                return
            self._json({"ok": True,
                        "items": conversations.list_conversations(user["uid"])})
        elif path.startswith("/plan/"):
            sid = path[len("/plan/"):]
            with _LOCK:
                saved = _LAST_PLAN.get(sid)
            if not saved:
                saved = conversations.get_plan_by_sid(sid)   # 重启过 → 从库里捞
            if not saved:
                self._html("<h1>还没有行程</h1><p>先在聊天里拿到一版方案。</p>", 404)
                return
            result, utterance = saved
            try:
                self._html(build_html(result, utterance))
            except Exception as e:   # 行程页失败不影响对话
                self._html(f"<h1>渲染失败</h1><pre>{e}</pre>", 500)
            finally:
                # 页面已回给用户，后台同步把动线路段预热进缓存——
                # 前端 fetch 毫秒级命中，不再赌用户浏览器的网络时序
                prefetch_drive_routes(sid, result)
        elif path.startswith("/api/conversations/"):
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录后再使用"}, 401)
                return
            csid = path[len("/api/conversations/"):]
            conv = conversations.get_conversation(user["uid"], csid)
            if conv is None:
                self._json({"ok": False, "error": "not found"}, 404)
                return
            self._json({"ok": True, **conv})
        elif path == "/api/tianditu/drive":
            self._tianditu_drive()
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _tianditu_drive(self):
        """代理天地图驾车路径规划，解析 XML routelatlon → JSON 点串。

        必须走服务端：SDK 的 T.DrivingRoute 走 HTTPS，而天地图 HTTPS /drive
        挂死不响应（HTTP 正常，实测），前端直调 HTTP 会被 CORS 拦。
        页面是 CGCS2000 坐标，天地图路径服务同为 CGCS2000，直接可用。"""
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        o = _ok_pt((q.get("orig") or [""])[0])
        d = _ok_pt((q.get("dest") or [""])[0])
        if not o or not d:
            self._json({"ok": False, "error": "bad coords"}, 400)
            return
        self._json(drive_fetch(o, d))

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        data = self._body()

        # ---- 账号接口（无需登录） ----
        if path == "/api/auth/send_code":
            r = auth.send_code(str(data.get("phone") or ""))
            self._json(r, 200 if r.get("ok") else 400)
            return
        if path == "/api/auth/login":
            r = auth.login(str(data.get("phone") or ""), str(data.get("code") or ""))
            self._json(r, 200 if r.get("ok") else 401)
            return
        if path == "/api/auth/refresh":
            r = auth.refresh_access(str(data.get("refresh") or ""))
            self._json(r, 200 if r.get("ok") else 401)
            return

        sid = str(data.get("sid") or "")[:64]
        if path == "/api/chat":
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录后再使用"}, 401)
                return
            uid = user["uid"]
            msg = str(data.get("message") or "").strip()
            if not sid or not msg or len(msg) > 2000:
                self._json({"ok": False, "error": "需要 sid 与 message（≤2000 字）"}, 400)
                return
            # 额度预检：余额低到连一次规划都不够时，直接明说，别让用户白等
            # （不限额测试账号跳过这道，否则余额用完会被拦在门口）
            acct = auth.get_account(uid)
            if not acct["unlimited"] and acct["balance"] < auth.MIN_BALANCE:
                self._json({"ok": False, "error": "quota", "reason": "insufficient",
                            "balance": acct["balance"],
                            "message": f"积分不足（余额 {acct['balance']}），请充值后再试"},
                           403)
                return
            try:
                with _LOCK:          # 编排涉及外部 API，串行执行
                    s = _get_session(uid, sid)
                    # 等待页进度：Session 进入每个阶段时写一条，前端轮询
                    # /api/progress?sid= 显示。会话对象复用，用完清掉回调。
                    progress.begin(sid)
                    s.on_phase = lambda txt: progress.phase(sid, txt)
                    try:
                        reply = s.turn(msg)
                    finally:
                        s.on_phase = None
                        progress.finish(sid)
                    if reply.mode == "plan" and reply.plan:
                        # 景点介绍卡片：搜介绍页（知乎/百科/攻略站）+ 缩略图，
                        # 行程页 hover 图钉显示、点击跳原网页。锦上添花，失败不影响方案。
                        try:
                            from . import poi_links
                            poi_links.attach(reply.plan)
                        except Exception:
                            pass
                        _LAST_PLAN[sid] = (reply.plan, msg)
                    # 自动存档：没有历史就按新对话建档保存，有就追加。
                    # 方案与会话状态一并落库——行程页重启后能捞、续聊能接上。
                    try:
                        conversations.record_turn(
                            uid, sid, msg, reply.render,
                            dest=reply.slots.get("destination") or "",
                            plan=reply.plan if reply.mode == "plan" else None,
                            utterance=msg, state=s.snapshot())
                    except Exception:
                        pass         # 存档失败只降级，不吞掉当轮回复
            except auth.QuotaError as e:       # 预扣失败（余额/每日上限）
                self._json({"ok": False, "error": "quota", "reason": e.reason,
                            "balance": e.balance, "message": e.message}, 403)
                return
            except Exception as e:
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
                return
            self._json({
                "ok": True, "mode": reply.mode, "text": reply.render,
                "turn_no": reply.turn_no, "ask_rounds": reply.ask_rounds,
                "assumptions": reply.assumptions,
                "tool_calls": len(reply.trace),
                "has_plan": reply.plan is not None,
                # 完整方案 JSON：前端行程面板据此渲染交互式行程单
                "plan": reply.plan if reply.mode == "plan" else None,
                "credits_left": auth.get_balance(uid),
                "unlimited": acct["unlimited"],
                "slots": {k: v for k, v in reply.slots.items() if not k.startswith("_")},
                # 本轮自动补全过的城市（前端可据此提示"数据是刚联网补的"）
                "fetched": reply.fetched,
            })
        elif path == "/api/reset":
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录后再使用"}, 401)
                return
            with _LOCK:
                _SESSIONS.pop(f"u{user['uid']}:{sid}", None)
                _LAST_PLAN.pop(sid, None)
            conversations.delete_conversation(user["uid"], sid)  # 存档一并清
            self._json({"ok": True})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_DELETE(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/api/conversations/"):
            user = self._auth_user()
            if user is None:
                self._json({"ok": False, "error": "unauthorized",
                            "message": "请先登录后再使用"}, 401)
                return
            csid = path[len("/api/conversations/"):]
            gone = conversations.delete_conversation(user["uid"], csid)
            with _LOCK:
                _SESSIONS.pop(f"u{user['uid']}:{csid}", None)
                _LAST_PLAN.pop(csid, None)
            self._json({"ok": True, "deleted": gone})
        else:
            self._json({"ok": False, "error": "not found"}, 404)


def _already_running(port: int, timeout: float = 0.8) -> bool:
    """端口上是否已有本服务在跑。

    Windows 下 allow_reuse_address 会让第二个实例也 bind 成功，于是两个进程
    同时监听同一端口、请求随机落到旧的那个——改了代码却"没生效"，排查半天。
    所以启动前先探一下，已有实例就直接退出（仍然打开浏览器）。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机探测别走代理
    try:
        with opener.open(f"http://{HOST}:{port}/api/health", timeout=timeout) as r:
            return b'"ok"' in r.read()
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="智能旅行规划助手 · 本地服务")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址：默认 0.0.0.0，手机连同一 Wi-Fi 即可访问；"
                         "只本机用传 --host 127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    # 探测占用/本机打开浏览器永远走 127.0.0.1；对外地址按绑定情况展示
    url = f"http://127.0.0.1:{a.port}/"
    global _LAN_URL
    _LAN_URL = (f"http://{_lan_ip()}:{a.port}/" if a.host == "0.0.0.0"
                else f"http://{a.host}:{a.port}/")
    if _already_running(a.port):
        print(f"127.0.0.1:{a.port} 上已经有一个实例在跑了，本次不重复启动：{url}")
        print("（改完代码要生效，请先关掉旧窗口 / 结束旧的 python 进程再启动）")
        if a.open:
            webbrowser.open(url)
        return 0
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"智能旅行规划助手已启动：{url}")
    if a.host == "0.0.0.0":
        print(f"手机互通（同一 Wi-Fi 下扫码或输入）：{_LAN_URL}")
    print("Ctrl+C 停止服务。")
    if a.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
