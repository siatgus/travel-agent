"""本地部署入口：把对话状态机包成零依赖的本地 Web 服务。

用法：
    python server.py                 # 默认 127.0.0.1:8787
    python server.py --port 9000
    python server.py --open          # 启动后自动打开浏览器

设计取舍：
- 只用标准库 http.server，pip 依赖为零，任何装了 Python 3.10+ 的机器双击即跑。
- 只绑 127.0.0.1：这是本地个人服务，不对局域网暴露，避免把未鉴权的会话接口开到公网。
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
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
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
from .render_html import build_html

HOST = "127.0.0.1"

_LOCK = threading.Lock()
_SESSIONS: dict[str, Session] = {}
_LAST_PLAN: dict[str, tuple[dict, str]] = {}   # sid -> (plan result, utterance)
_MEM: PreferenceMemory | None = None
_LLM = "unset"                                  # 延迟初始化的 LLM 客户端（或 None）
_LLM_SIG = None                                 # 设置文件指纹 (mtime_ns, size)，变更即重建客户端


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

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能旅行规划助手</title>
<style>
  :root{--bg:#f6f5f1;--card:#fff;--ink:#2b2a26;--dim:#8a8578;--brand:#2f6f6b;
        --brand-ink:#fff;--line:#e6e2d8;--user:#2f6f6b;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column}
  header{background:var(--card);border-bottom:1px solid var(--line);
         padding:14px 20px;display:flex;align-items:center;gap:12px}
  header .logo{width:34px;height:34px;border-radius:10px;background:var(--brand);
         color:var(--brand-ink);display:flex;align-items:center;justify-content:center;font-size:18px}
  header h1{font-size:16px;font-weight:600}
  header .sub{font-size:12px;color:var(--dim)}
  header button{border:1px solid var(--line);background:var(--card);
         border-radius:8px;padding:6px 12px;font-size:13px;cursor:pointer;color:var(--ink);
         font-family:inherit}
  header button:hover{border-color:var(--brand);color:var(--brand)}
  #chat{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:14px}
  .msg{max-width:76%;border-radius:14px;padding:12px 16px;font-size:14px;line-height:1.7;
       white-space:pre-wrap;word-break:break-word}
  .msg.user{align-self:flex-end;background:var(--user);color:var(--brand-ink);
       border-bottom-right-radius:4px}
  .msg.bot{align-self:flex-start;background:var(--card);border:1px solid var(--line);
       border-bottom-left-radius:4px}
  .msg.bot h3{font-size:15px;margin:2px 0 6px;color:var(--brand)}
  .msg.bot table{border-collapse:collapse;margin:8px 0;font-size:13px;white-space:normal}
  .msg.bot th,.msg.bot td{border:1px solid var(--line);padding:4px 10px;text-align:left}
  .msg.bot th{background:#f0eee7}
  .msg.bot blockquote{border-left:3px solid var(--brand);padding:2px 10px;margin:6px 0;
       color:var(--dim);font-size:13px}
  .msg.bot code{background:#f0eee7;border-radius:4px;padding:1px 5px;font-size:13px}
  .meta{font-size:12px;color:var(--dim);margin-top:6px}
  .msg.bot>*{animation:fin .3s ease}
  @keyframes fin{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  .plan-link{display:inline-block;margin-top:8px;background:var(--brand);color:var(--brand-ink);
       text-decoration:none;border-radius:8px;padding:6px 14px;font-size:13px}
  /* ---- 行程面板（右侧交互式行程单）---- */
  #main{flex:1;display:flex;min-height:0;width:100%}
  #board{width:460px;flex:none;border-left:1px solid var(--line);background:var(--card);
       display:none;flex-direction:column;min-height:0}
  #board.on{display:flex}
  #bhead{padding:12px 16px 8px;border-bottom:1px solid var(--line)}
  #btitle{font-size:15px;font-weight:600;display:flex;justify-content:space-between;
       align-items:center;gap:8px}
  #btitle .bsub{font-size:12px;color:var(--dim);font-weight:400}
  #btabs{display:flex;gap:6px;overflow-x:auto;padding:10px 16px 10px;flex-wrap:wrap}
  #btabs button{border:1px solid var(--line);background:var(--bg);border-radius:999px;
       padding:4px 12px;font-size:12.5px;cursor:pointer;color:var(--ink);white-space:nowrap;
       font-family:inherit}
  #btabs button.act{background:var(--brand);color:var(--brand-ink);border-color:var(--brand)}
  #bbody{flex:1;overflow-y:auto;padding:14px 16px}
  .bday{margin-bottom:14px}
  .bday h4{font-size:13.5px;color:var(--brand);margin-bottom:6px;display:flex;gap:8px;
       align-items:baseline;flex-wrap:wrap}
  .bday h4 .wx{font-weight:400;color:var(--dim);font-size:12px}
  .bcard{border:1px solid var(--line);border-radius:12px;padding:8px 10px;background:var(--bg)}
  .bnode{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:9px;
       cursor:pointer}
  .bnode:hover{background:#eef3f0}
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
  .bempty{color:var(--dim);font-size:12.5px;text-align:center;padding:30px 10px}
  #bchips{padding:10px 16px;border-top:1px solid var(--line);display:flex;gap:6px;flex-wrap:wrap}
  #bchips button{border:1px solid var(--line);background:var(--bg);border-radius:999px;
       padding:4px 12px;font-size:12.5px;cursor:pointer;color:var(--ink);font-family:inherit}
  #bchips button:hover{border-color:var(--brand);color:var(--brand)}
  #bmenu{position:fixed;z-index:18;background:var(--card);border:1px solid var(--line);
       border-radius:12px;box-shadow:0 12px 36px rgba(43,42,38,.18);padding:6px;display:none;
       min-width:225px}
  #bmenu button{display:block;width:100%;text-align:left;border:none;background:none;
       padding:9px 12px;font-size:13px;border-radius:8px;cursor:pointer;color:var(--ink);
       font-family:inherit;white-space:nowrap}
  #bmenu button:hover{background:var(--bg);color:var(--brand)}
  @media(max-width:920px){#board{width:380px}}
  @media(max-width:760px){#main{flex-direction:column}
       #board{width:auto;max-height:44vh;border-left:none;border-top:1px solid var(--line)}}
  .typing{align-self:flex-start;color:var(--dim);font-size:13px;padding:10px 16px;
       background:var(--card);border:1px solid var(--line);border-radius:14px;
       border-bottom-left-radius:4px;display:flex;align-items:center;gap:8px}
  .tdot{width:6px;height:6px;border-radius:50%;background:var(--dim);
       display:inline-block;animation:tb 1.2s infinite}
  .tdot:nth-child(2){animation-delay:.2s}
  .tdot:nth-child(3){animation-delay:.4s}
  @keyframes tb{0%,80%,100%{opacity:.25}40%{opacity:1}}
  #bar{background:var(--card);border-top:1px solid var(--line);padding:12px 20px}
  #sugs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
  #sugs button{border:1px solid var(--line);background:var(--bg);border-radius:999px;
       padding:5px 14px;font-size:13px;cursor:pointer;color:var(--ink)}
  #sugs button:hover{border-color:var(--brand);color:var(--brand)}
  #row{display:flex;gap:10px}
  #inp{flex:1;border:1px solid var(--line);border-radius:10px;padding:10px 14px;
       font-size:14px;font-family:inherit;resize:none;outline:none;background:var(--bg)}
  #inp:focus{border-color:var(--brand)}
  #send{border:none;background:var(--brand);color:var(--brand-ink);border-radius:10px;
       padding:0 22px;font-size:14px;cursor:pointer}
  #send:disabled{opacity:.5;cursor:default}
  #foot{text-align:center;font-size:11px;color:var(--dim);padding:6px 0 10px}
  .userwrap{margin-left:auto;display:flex;align-items:center;gap:8px}
  .userbox{font-size:13px;color:var(--brand);cursor:default;user-select:none;
       border:1px solid var(--line);border-radius:999px;padding:5px 14px;background:var(--bg);
       display:inline-flex;align-items:center;gap:6px}
  .userbox .sep{color:var(--dim)}
  .userbox .cr{font-weight:600}
  .quit{border:1px solid var(--line);border-radius:999px;padding:5px 13px;
       font-size:12.5px;color:var(--dim);cursor:pointer;background:var(--card);
       font-family:inherit;transition:color .12s,border-color .12s}
  .quit:hover{border-color:#d3a394;color:#a33a2a}
  /* 退出确认弹窗 */
  #logoutmask{position:fixed;inset:0;background:rgba(43,42,38,.45);display:none;
       align-items:center;justify-content:center;z-index:20}
  #logoutmask .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
       padding:22px 24px;width:308px;box-shadow:0 18px 48px rgba(43,42,38,.22)}
  #logoutmask h2{font-size:16px;margin-bottom:8px}
  #logoutmask .hint{font-size:12.5px;color:var(--dim);line-height:1.7;margin-bottom:18px}
  #logoutmask .btnrow{display:flex;gap:8px;justify-content:flex-end}
  #logoutmask button{border:1px solid var(--line);background:var(--card);border-radius:9px;
       padding:8px 16px;font-size:13.5px;cursor:pointer;color:var(--ink);font-family:inherit}
  #logoutmask button:hover{border-color:var(--brand);color:var(--brand)}
  #logoutmask .danger{background:#a33a2a;border-color:#a33a2a;color:#fff}
  #logoutmask .danger:hover{background:#8f3123;border-color:#8f3123;color:#fff}
  #login{position:fixed;inset:0;background:rgba(43,42,38,.45);display:none;
       align-items:center;justify-content:center;z-index:10}
  #login .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
       padding:26px;width:300px}
  #login h2{font-size:17px;margin-bottom:4px}
  #login .hint{font-size:12px;color:var(--dim);margin:4px 0 12px;line-height:1.5}
  #login input{width:100%;border:1px solid var(--line);border-radius:9px;
       padding:10px 12px;font-size:14px;margin-bottom:10px;outline:none;
       background:var(--bg);box-sizing:border-box;font-family:inherit}
  #login input:focus{border-color:var(--brand)}
  #login .row2{display:flex;gap:8px;align-items:stretch;margin-bottom:10px}
  #login .row2 input{flex:1;margin-bottom:0}     /* 去掉 input 下边距，否则按钮被撑高、压到登录按钮 */
  #login .row2 button{border:1px solid var(--line);background:var(--card);
       border-radius:9px;padding:0 14px;font-size:13px;cursor:pointer;color:var(--brand);
       white-space:nowrap;box-sizing:border-box;display:flex;align-items:center;
       justify-content:center;line-height:1;font-family:inherit;margin:0}
  #login .row2 button:hover:not(:disabled){border-color:var(--brand)}
  #login .row2 button:disabled{color:var(--dim);border-color:var(--line);cursor:default}
  #login .pri{width:100%;border:none;background:var(--brand);color:var(--brand-ink);
       border-radius:9px;padding:11px 0;font-size:14px;cursor:pointer}
  #login .pri:disabled{opacity:.5}
  #login .ghost{width:100%;margin-top:8px;border:1px solid var(--line);background:transparent;
       color:var(--dim);border-radius:9px;padding:9px 0;font-size:13px;cursor:pointer;
       font-family:inherit}
  #login .ghost:hover{border-color:var(--brand);color:var(--brand)}
  #guestbar{margin-left:auto;border:1px solid #e2d3c4;background:var(--bg);color:var(--brand);
       border-radius:999px;padding:5px 14px;font-size:13px;cursor:pointer;font-family:inherit}
  #guestbar:hover{border-color:var(--brand);background:var(--card)}
  /* 历史行程抽屉 */
  #histmask{position:fixed;inset:0;background:rgba(43,42,38,.35);display:none;z-index:14}
  #histpanel{position:fixed;top:0;right:0;bottom:0;width:340px;max-width:92vw;
       background:var(--card);border-left:1px solid var(--line);z-index:15;
       display:none;flex-direction:column;box-shadow:-12px 0 36px rgba(43,42,38,.16)}
  .hhead{display:flex;align-items:center;justify-content:space-between;
       padding:14px 16px;border-bottom:1px solid var(--line)}
  .hhead h2{font-size:15px}
  .hhead button{border:1px solid var(--line);background:var(--card);border-radius:8px;
       padding:5px 13px;font-size:12.5px;cursor:pointer;font-family:inherit;color:var(--ink)}
  #histlist{flex:1;overflow-y:auto;padding:10px}
  .hitem{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:8px;
       cursor:pointer;position:relative;background:var(--bg)}
  .hitem:hover{border-color:var(--brand)}
  .htitle{font-size:13.5px;font-weight:600;color:var(--ink);padding-right:46px}
  .hbadge{font-size:11px;color:var(--brand);border:1px solid var(--brand);
       border-radius:999px;padding:1px 7px;margin-left:6px;font-weight:400}
  .hmeta{font-size:12px;color:var(--dim);margin-top:4px}
  .hdel{position:absolute;right:8px;top:8px;border:none;background:transparent;
       color:var(--dim);cursor:pointer;font-size:12px;font-family:inherit}
  .hdel:hover{color:#a33a2a}
  .hempty{color:var(--dim);font-size:13px;text-align:center;padding:30px 12px;line-height:1.9}
  /* 新对话规划表单：先填目的地等，再进对话 */
  #planner{flex:1;overflow-y:auto;padding:24px 16px 34px;display:none}
  #planner .hero{max-width:920px;margin:0 auto 18px;border-radius:18px;padding:34px 36px;
    position:relative;overflow:hidden;color:#22301f;
    background:linear-gradient(135deg,#eef0e2,#e3ebd9 55%,#d9e7d3)}
  #planner .hero .brand{font-size:11.5px;letter-spacing:3px;color:#4b5f45;font-weight:600}
  #planner .hero h2{font-size:30px;line-height:1.4;margin:14px 0 10px;font-weight:700}
  #planner .hero p{font-size:13.5px;color:#5c6b55}
  #planner .hero .sun{position:absolute;right:76px;top:36px;width:62px;height:62px;
    border-radius:50%;background:#eec27c}
  #planner .hero .hill{position:absolute;right:-50px;bottom:-80px;width:280px;height:160px;
    border-radius:50%;background:#c9d8bd;opacity:.85}
  #planner .card{max-width:920px;margin:0 auto;background:var(--card);border:1px solid var(--line);
    border-radius:16px;padding:26px 28px}
  #planner .chead{display:flex;justify-content:space-between;align-items:baseline;
    margin-bottom:16px;gap:10px;flex-wrap:wrap}
  #planner .chead h3{font-size:16px}
  #planner .chead h3 .no{color:var(--brand);font-size:13px;margin-right:6px}
  #planner .chead .note{font-size:12px;color:var(--dim)}
  #planner .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 16px}
  #planner .f{display:flex;flex-direction:column;gap:6px}
  #planner .f.w2{grid-column:span 2}
  #planner label{font-size:12.5px;color:#4a4840;font-weight:600}
  #planner label .opt{color:var(--dim);font-weight:400;margin-left:4px}
  #planner label .req{color:#c0392b;margin-left:2px}
  #planner input,#planner select{border:1px solid var(--line);border-radius:9px;padding:9px 11px;
    font-size:13.5px;background:#fbfaf7;color:var(--ink);font-family:inherit;outline:none;width:100%}
  #planner input:focus,#planner select:focus{border-color:var(--brand);background:#fff}
  #planner .err{color:#c0392b;font-size:12.5px;margin-top:10px;display:none}
  #planner .foot{display:flex;justify-content:space-between;align-items:center;margin-top:18px;
    padding-top:14px;border-top:1px solid var(--line)}
  #planner .foot .src{font-size:12px;color:var(--dim)}
  #planner .go{display:flex;align-items:center;gap:8px;background:var(--brand);color:#fff;
    border:none;border-radius:999px;padding:11px 26px;font-size:14px;cursor:pointer;
    font-family:inherit}
  #planner .go:hover{background:#285e5a}
  @media(max-width:760px){#planner .grid{grid-template-columns:repeat(2,1fr)}
    #planner .hero h2{font-size:24px}}
</style>
</head>
<body>
<header>
  <div class="logo">&#9992;</div>
  <div><h1>智能旅行规划助手</h1>
       <div class="sub">天气 16 天内为实时预报 &middot; 真实路网耗时 &middot; 在售酒店班次 &middot; 全部本地运行</div></div>
  <button id="guestbar" onclick="showLogin('登录后即可开始规划')" title="点击登录">未登录 · 点此登录</button>
  <div class="userwrap" id="userwrap" style="display:none">
    <span class="userbox" id="userbox" title="当前登录账号">
      <span id="ub-phone"></span><span class="sep">·</span><span class="cr" id="ub-credits"></span>
    </span>
    <button class="quit" id="btn-logout" onclick="askLogout()" title="退出登录">退出</button>
  </div>
  <button id="btn-hist" onclick="openHist()" style="display:none"
          title="你保存的历次行程，含与助手的完整对话，点开可继续">历史行程</button>
  <button onclick="newChat()">新对话</button>
</header>
<div id="histmask" onclick="closeHist()"></div>
<div id="histpanel">
  <div class="hhead"><h2>历史行程</h2><button onclick="closeHist()">关闭</button></div>
  <div id="histlist"></div>
</div>
<div id="login">
  <div class="card">
    <h2>手机号登录</h2>
    <div class="hint">首次登录自动注册，并赠送 <b>500 积分</b>（约 15 次完整行程规划）</div>
    <input id="lg-phone" maxlength="11" inputmode="numeric" placeholder="手机号">
    <div class="row2">
      <input id="lg-code" maxlength="6" inputmode="numeric" placeholder="验证码">
      <button id="lg-send" onclick="sendCode()">获取验证码</button>
    </div>
    <button class="pri" id="lg-btn" onclick="doLogin()">登录 / 注册</button>
    <button class="ghost" onclick="closeLogin()">先看看，稍后登录</button>
    <div class="hint" id="lg-msg"></div>
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
<div id="planner">
  <div class="hero">
    <div class="brand">&#10022; SMART TRAVEL PLANNER</div>
    <h2>把想去的地方，<br>变成刚刚好的旅程。</h2>
    <p>从目的地和日期开始。先探索真实地点，再一起编排行程。</p>
    <div class="sun"></div><div class="hill"></div>
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
      <div class="f"><label>预算总额<span class="opt">可选</span></label>
        <div style="display:flex;gap:6px">
          <input id="pf-budget" inputmode="numeric" placeholder="比如 8000" style="flex:1;min-width:0">
          <select id="pf-cur" style="width:80px;flex:none"><option>CNY ¥</option></select>
        </div></div>
      <div class="f"><label>同行人数<span class="opt">可选</span></label>
        <input id="pf-party" inputmode="numeric" placeholder="比如 2 人"></div>
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
      <span class="bsub" id="bsub"></span></div>
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
<div id="bmenu"></div>
<div id="bar">
  <div id="sugs">
    <button>11月8号去大理玩4天，2人，预算6000</button>
    <button>我和孩子（3岁）下月从杭州去大理玩4天，不想太赶</button>
    <button>五一去云南玩</button>
  </div>
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
// 每次打开网页都从新对话开始：不读上次的 sid（刷新/重开一律新会话）。
// 旧对话已自动存档，登录后可在「历史行程」里回看或点开续聊。
let sid = genSid();
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
    // 访客从表单提交进来又取消登录：回到表单页，别留在空白聊天区
    if(!chat.children.length && planner.style.display==='none') showPlanner();
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
  document.getElementById('login').style.display = 'none';
  showGuest();
  addBot(esc('已退出登录。页面仍可正常浏览，需要发送对话时再点右上角登录即可。'));
}

// 点遮罩空白处 / 按 Esc 都算取消，不退出
document.getElementById('logoutmask').addEventListener('click', function(e){
  if(e.target === this) closeLogout();
});
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape') closeLogout();
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
let boardPlan=null,boardSlots=null,boardTab='all';
const TYPE_ICONS={attraction:'\\u{1F3DE}',restaurant:'\\u{1F35C}',hotel:'\\u{1F3E8}',
                  transport:'\\u{1F684}',other:'\\u{1F4CD}'};
function jsq(s){return String(s).replace(/\\\\/g,'\\\\\\\\').replace(/'/g,"\\\\'");}
function nodeMenu(ev,name){
  ev.stopPropagation();
  bmenu.innerHTML='';
  const mk=(label,msg)=>{const b=document.createElement('button');b.textContent=label;
    b.onclick=()=>{bmenu.style.display='none';send(msg);};bmenu.appendChild(b);};
  mk('\\u274C 不想去这里，换个类似的','不想去'+name);
  mk('\\u{1F4D6} 看看介绍和门票',name+'怎么样？门票多少钱');
  bmenu.style.display='block';
  const rect=ev.currentTarget.getBoundingClientRect();
  const bw=240,bh=92;
  let x=Math.min(rect.left,window.innerWidth-bw-10);
  let y=rect.bottom+6;
  if(y+bh>window.innerHeight-10)y=Math.max(8,rect.top-bh-6);
  bmenu.style.left=Math.max(8,x)+'px';bmenu.style.top=y+'px';
}
document.addEventListener('click',()=>{bmenu.style.display='none';});
function dayCard(d,idx){
  const wx=(boardPlan.weather&&boardPlan.weather.days?boardPlan.weather.days[idx]:null);
  let wtxt='';
  if(wx){
    wtxt=(wx.low!=null&&wx.high!=null)?(' '+wx.low+'~'+wx.high+'℃'):'';
    if(wx.desc)wtxt=esc(wx.desc)+wtxt;
    // 口径标注：超出预报窗口的日期是历史同期，不能和"实时预报"长得一样
    if(wx.is_forecast===false)wtxt+=' <span style="color:#8A6A2F">历史同期</span>';
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
        +(s?'#EAF3F1':'#F6F1E4')+';color:'+(s?'#2F6F6B':'#8A6A2F')+'">'
        +(s?'兴趣·':'近似·')+esc(it.word||'')+'</span>';
    });
    h+='<div class="bnode" onclick="nodeMenu(event,\\''+jsq(n.name)+'\\')">'
      +'<span class="tm">'+esc(String(n.time||'').slice(0,5))+'</span>'
      +'<span class="ic">'+(TYPE_ICONS[n.type]||TYPE_ICONS.other)+'</span>'
      +'<span class="nm">'+esc(n.name)+itag+(uncer?' <span style="color:#B4693F;font-size:11px">位置待确认</span>':'')+'</span>'
      +(n.duration_min?'<span class="mn">'+n.duration_min+'分钟</span>':'')
      +'</div>';
  });
  h+='</div>';
  if(d.geo_uncertain)h+='<div class="bnote" style="margin-top:4px;color:#B4693F">\\u26A0 这天有点位没查到确切位置，交通时间未计入，出发前请确认</div>';
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
function hideBoard(){
  board.classList.remove('on');
  boardPlan=null;boardSlots=null;
}
function renderBoard(plan,slots){
  boardPlan=plan;boardSlots=slots||null;
  if(!plan||!plan.days_plan||!plan.days_plan.length){hideBoard();return;}
  board.classList.add('on');
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
  const party=parseInt(document.getElementById('pf-party').value,10);
  if(party>0)parts.push(party+'个人');
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
// 点开一次历史行程：整段对话原样回放，之后继续说话就接着这次排
async function loadConv(sid_){
  let r=await fetch('/api/conversations/'+encodeURIComponent(sid_),{headers:authHeaders()});
  if(r.status===401){
    if(await tryRefresh()){ return loadConv(sid_); }
    showLogin('登录已过期，请重新登录'); return;
  }
  const j=await r.json();
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
})();

inp.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}
});
inp.addEventListener('input',()=>{inp.style.height='auto';
  inp.style.height=Math.min(inp.scrollHeight,120)+'px';});
document.getElementById('sugs').addEventListener('click',e=>{
  if(e.target.tagName==='BUTTON')send(e.target.textContent);});

// 打开网页先展示规划表单，不直接进对话；提交表单或从历史行程进入后才出现聊天区
showPlanner();
</script>
</body>
</html>"""


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
        else:
            self._json({"ok": False, "error": "not found"}, 404)

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
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    url = f"http://{HOST}:{a.port}/"
    if _already_running(a.port):
        print(f"{HOST}:{a.port} 上已经有一个实例在跑了，本次不重复启动：{url}")
        print("（改完代码要生效，请先关掉旧窗口 / 结束旧的 python 进程再启动）")
        if a.open:
            webbrowser.open(url)
        return 0
    server = ThreadingHTTPServer((HOST, a.port), Handler)
    print(f"智能旅行规划助手已启动：{url}")
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
