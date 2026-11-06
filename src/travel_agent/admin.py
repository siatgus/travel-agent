"""数据管理后端：知识库（SQLite）的可视化管理服务。

用法：
    python admin.py                 # 默认 127.0.0.1:8788
    python admin.py --port 9001

与对话服务（server.py 8787）分离：管理端口只绑 127.0.0.1，供本地运营用。

功能（页面左侧导航分四个视图，hash 路由：#/overview、#/data、#/users、#/model）：
- 概览：城市/POI/酒店/班次总量、各城市数据新鲜度（来源与最近更新时间）
- 数据管理：POI / 酒店 / 班次增删改、批量导入（JSON 数组走 import_json 校验）、
  一键 Overpass 同步（主站+镜像轮询）、同步日志最近 50 条
- 用户与积分：用户列表（脱敏）、手工调账（充值/扣减+原因入账本）、LLM 用量流水
- 模型设置：切换/配置大语言模型与 API Key（写 .llm_settings.json，
  优先级高于环境变量；对话服务每句话都检查文件指纹，改完即生效）

页面 JS 全部是普通 DOM 操作；嵌在 Python 字符串里的 JS 换行必须写 \\n，
且 test_admin.py 会对求值后的页面做 node --check（防 \\n 求值回退）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from . import kb
from . import llm
from . import auth

HOST = "127.0.0.1"
# 对话服务地址：后台用它确认「此刻真正在用的模型」，避免只展示配置文件里的话
CHAT_URL = os.environ.get("TRAVEL_CHAT_URL", "http://127.0.0.1:8787")
_LOCK = threading.Lock()      # SQLite 管理操作串行，避免写冲突


# ---------------------------------------------------------------- 管理页面

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>旅行助手 · 数据管理</title>
<style>
  :root{--bg:#f4f4f0;--card:#fff;--ink:#26251f;--dim:#87816f;--brand:#8a5a2b;
        --line:#e3ded2;--ok:#2f6f4f;--bad:#a33a2a;--side:#26241d;--side-line:#3a362c}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       background:var(--bg);color:var(--ink);font-size:14px}
  .app{display:flex;min-height:100vh}
  /* ---------- 左侧导航 ---------- */
  .side{width:212px;flex:0 0 212px;background:var(--side);color:#cfc9b8;
        padding:18px 12px 14px;display:flex;flex-direction:column;
        position:sticky;top:0;height:100vh}
  .brand{padding:2px 9px 13px;border-bottom:1px solid var(--side-line)}
  .brand b{display:block;color:#fff;font-size:15px}
  .brand span{display:block;color:#807962;font-size:11px;margin-top:4px}
  .nav{margin-top:8px;display:flex;flex-direction:column;gap:2px}
  .nav-group{font-size:10.5px;letter-spacing:.14em;color:#6f6957;padding:13px 9px 5px}
  .nav a{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:8px;
         color:#cfc9b8;text-decoration:none;font-size:13.5px}
  .nav a:hover{background:#332f27;color:#fff}
  .nav a.on{background:var(--brand);color:#fff;font-weight:600}
  .nav a .ic{width:15px;text-align:center;opacity:.8}
  .nav a.on .ic{opacity:1}
  .side .foot{margin-top:auto;padding:14px 9px 0;font-size:11px;color:#6f6957;line-height:1.7}
  /* ---------- 主内容区 ---------- */
  .main{flex:1;min-width:0;padding:24px 26px 48px}
  .view{display:none}
  .view.on{display:block}
  .view-head{margin-bottom:16px}
  .view-head h1{font-size:19px;margin-bottom:5px}
  .view-head .sub{color:var(--dim);font-size:12px}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:14px 20px;min-width:120px}
  .card b{font-size:22px;display:block}
  .card span{color:var(--dim);font-size:12px}
  section{background:var(--card);border:1px solid var(--line);border-radius:10px;
          padding:16px;margin-bottom:16px}
  h2{font-size:15px;margin-bottom:10px}
  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{border:1px solid var(--line);padding:5px 8px;text-align:left}
  th{background:#f0ede4}
  button{border:1px solid var(--line);background:var(--card);border-radius:7px;
         padding:5px 12px;cursor:pointer;font-size:13px;color:var(--ink)}
  button.pri{background:var(--brand);color:#fff;border-color:var(--brand)}
  button:hover{filter:brightness(0.95)}
  button.del{color:var(--bad)}
  input,select,textarea{border:1px solid var(--line);border-radius:7px;padding:6px 9px;
         font-size:13px;font-family:inherit;background:var(--bg)}
  textarea{width:100%;min-height:110px}
  .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}
  .fresh{font-size:12px;color:var(--dim)}
  .tag{display:inline-block;border-radius:5px;padding:1px 6px;font-size:11px;
       background:#eee6d8;margin-right:4px}
  .msg{padding:8px 12px;border-radius:8px;margin:8px 0;display:none;
       border:1px solid var(--line);background:var(--card)}
  .msg.ok{display:block;border-color:var(--ok);color:var(--ok)}
  .msg.bad{display:block;border-color:var(--bad);color:var(--bad)}
  .empty{color:var(--dim);font-size:12px;padding:6px 0}
  /* 分页条 */
  .pager{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin:9px 0 2px;
         font-size:12.5px}
  .pager:empty{display:none}
  .pager button{padding:2px 9px;font-size:12.5px}
  .pager button[disabled]{opacity:.4;cursor:default}
  .pager .pg-cur{background:var(--brand);color:#fff;border-color:var(--brand)}
  .pager .pg-dots{color:var(--dim);padding:0 1px}
  .pager .pg-info{color:var(--dim);margin:0 5px}
  .pager select{padding:3px 6px;font-size:12.5px}
  .pager .pg-jump{width:52px;padding:3px 6px;font-size:12.5px;text-align:center}
  /* 条件搜索条 */
  .filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px;
           padding:9px 10px;background:#faf8f2;border:1px solid var(--line);border-radius:9px}
  .filters .fresh{margin-left:auto}
  /* 行内编辑 */
  td input.cell,td select.cell{padding:3px 5px;font-size:12.5px;background:#fffdf6;
                               border-color:#cdc4af;border-radius:5px}
  tr.editing{background:#fffdf0}
  tr.editing td{border-color:#d9cdb2}
  tr.editrow td{background:#fbf7ec;font-size:13px}
  tr.editrow input{padding:4px 7px}
  details summary{cursor:pointer;font-size:13.5px;font-weight:600;color:var(--brand)}
  details[open] summary{margin-bottom:8px}
  /* 当前生效模型卡片 */
  .active{border:1px solid #d9cdb2;border-left:4px solid var(--ok);border-radius:10px;
         background:#fbfaf4;padding:14px 18px;margin-bottom:14px}
  .active.off{border-left-color:#b8b1a0}
  .active.err{border-left-color:var(--bad);background:#fdf7f5}
  .ac-top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .ac-title{font-size:12px;letter-spacing:.08em;color:var(--dim)}
  .ac-badge{font-size:11.5px;border-radius:999px;padding:2px 9px;font-weight:600;
            background:#e6efe6;color:var(--ok);border:1px solid #c8ddc8}
  .ac-badge.gray{background:#eeeae0;color:var(--dim);border-color:#ddd7c8}
  .ac-badge.red{background:#f7e7e3;color:var(--bad);border-color:#e6c8c0}
  .ac-model{font-size:20px;font-weight:700;letter-spacing:.3px;
            font-family:ui-monospace,SFMono-Regular,Consolas,monospace;line-height:1.3;
            word-break:break-all}
  .ac-model.none{color:var(--dim);font-weight:600;font-size:16px}
  .ac-meta{font-size:12.5px;color:var(--dim);margin-top:6px;line-height:1.8}
  .ac-meta code{font-family:ui-monospace,Consolas,monospace;background:#f0ece0;
                padding:1px 5px;border-radius:4px;color:var(--ink);font-size:12px}
  .ac-live{margin-top:8px;padding-top:8px;border-top:1px dashed var(--line);
           font-size:12.5px;color:var(--dim)}
  .ac-live b{color:var(--ink)}
  @media(max-width:780px){
    .app{flex-direction:column}
    .side{width:auto;flex:none;height:auto;position:static;flex-direction:row;
          flex-wrap:wrap;align-items:center;gap:6px;padding:10px 12px}
    .brand{border:none;padding:0 10px 0 0}
    .brand span{display:none}
    .nav{flex-direction:row;flex-wrap:wrap;margin-top:0;gap:4px}
    .nav-group{display:none}
    .side .foot{display:none}
    .main{padding:16px}
  }
</style>
</head>
<body>
<div class="app">
<aside class="side">
  <div class="brand"><b>旅行助手</b><span>运营管理后台</span></div>
  <nav class="nav">
    <div class="nav-group">总览</div>
    <a href="#/overview"><span class="ic">▤</span>概览</a>
    <div class="nav-group">数据</div>
    <a href="#/data"><span class="ic">◈</span>数据管理</a>
    <div class="nav-group">运营</div>
    <a href="#/users"><span class="ic">◍</span>用户与积分</a>
    <a href="#/model"><span class="ic">✦</span>模型设置</a>
  </nav>
  <div class="foot">知识库 <code>.kb.db</code><br>账号库 <code>.app.db</code><br>仅监听 127.0.0.1</div>
</aside>

<main class="main">
<div class="view" id="v-overview">
  <div class="view-head"><h1>概览</h1>
    <div class="sub">知识库与账号体系整体状态 · 数据改动对对话服务即时生效</div></div>
  <div class="cards" id="cards"></div>
  <section>
    <h2>各城市数据新鲜度</h2>
    <div style="overflow-x:auto"><table id="fresh-table"></table></div>
    <div class="pager" id="fresh-pager"></div>
  </section>
</div>

<div class="view" id="v-data">
  <div class="view-head"><h1>数据管理</h1>
    <div class="sub">SQLite 知识库 · 对话服务（8787）优先读本库 · 编辑 / 导入 / 同步后立即生效，无需重启</div></div>

<section>
  <h2>POI 管理 <span class="fresh" id="poi-fresh"></span></h2>
  <div class="filters">
    <select id="city-sel" onchange="loadPois()"></select>
    <input id="q-poi" placeholder="名称 / 区域 / ID 关键词" size="22" oninput="debouncePoi()">
    <select id="flt-cat" onchange="loadPois()">
      <option value="">全部类型</option>
      <option value="attraction">景点 attraction</option>
      <option value="restaurant">餐厅 restaurant</option>
    </select>
    <select id="flt-src" onchange="loadPois()">
      <option value="">全部来源</option>
      <option value="seed">seed 内置</option>
      <option value="overpass">overpass 同步</option>
      <option value="manual">manual 人工</option>
    </select>
    <button class="pri" onclick="loadPois()">查询</button>
    <button onclick="resetPoiFilter()">重置</button>
    <span class="fresh" id="poi-count"></span>
  </div>
  <div class="row">
    <button class="pri" onclick="syncCity()">⟳ Overpass 实时同步</button>
    <button onclick="delAllCheck()">查看同步日志</button>
  </div>
  <div id="msg" class="msg"></div>
  <div style="overflow-x:auto"><table id="poi-table"></table></div>
  <div class="pager" id="poi-pager"></div>
  <h2 style="margin-top:16px">新增 POI <span class="fresh">（改已有数据请点表格行内「编辑」，直接在表上改）</span></h2>
  <div class="row">
    <input id="f-id" placeholder="poi_id（留空自动生成）" size="20">
    <input id="f-name" placeholder="名称*" size="22">
    <input id="f-area" placeholder="区域" size="10">
    <input id="f-geo" placeholder="纬度,经度*" size="16">
    <select id="f-cat"><option>attraction</option><option>restaurant</option></select>
    <input id="f-appeal" placeholder="吸引力1-5" size="8" value="3">
    <input id="f-dur" placeholder="时长(分)" size="7" value="90">
    <input id="f-cost" placeholder="费用(元)" size="7" value="0">
    <input id="f-hours" placeholder="开放时间" size="12" value="全天">
    <button class="pri" onclick="savePoi()">新增</button>
    <button onclick="clearPoiForm()">清空</button>
  </div>
</section>

<section>
  <h2>批量导入（JSON 数组，走校验通道）</h2>
  <div class="row">
    <select id="imp-kind"><option value="poi">POI</option><option value="hotel">酒店</option></select>
    <input id="imp-city" placeholder="所属城市*" size="12">
    <button class="pri" onclick="importJson()">导入</button>
  </div>
  <textarea id="imp-json" placeholder='[{"name":"例：沙溪古镇","geo":[26.53,99.84],"appeal":4,"cost":0,"tags":["古镇"]}]'></textarea>
</section>

<section>
  <h2>同步日志（最近 50 条）</h2>
  <div class="filters">
    <input id="q-log" placeholder="城市 / 来源 / 说明 关键词" size="22" oninput="filterLogs(true)">
    <select id="flt-log-status" onchange="filterLogs(true)">
      <option value="">全部状态</option>
      <option value="success">success</option>
      <option value="failed">failed</option>
    </select>
    <button onclick="resetLogFilter()">重置</button>
    <span class="fresh" id="log-count"></span>
  </div>
  <div style="overflow-x:auto"><table id="log-table"></table></div>
  <div class="pager" id="log-pager"></div>
</section>
</div>

<div class="view" id="v-users">
  <div class="view-head"><h1>用户与积分</h1>
    <div class="sub">手机号登录即注册 · 余额与冻结额度、手工调账、模型调用流水</div></div>
  <section>
    <h2>用户列表 <span class="fresh" id="user-fresh"></span></h2>
    <div class="msg" id="user-msg"></div>
    <div class="filters">
      <input id="q-user" placeholder="手机号 / 用户 ID" size="20" oninput="filterUsers(true)">
      <select id="flt-status" onchange="filterUsers(true)">
        <option value="">全部状态</option>
        <option value="active">active</option>
        <option value="disabled">disabled</option>
      </select>
      <input id="flt-min" type="number" placeholder="余额 ≥" size="8" oninput="filterUsers(true)">
      <button onclick="resetUserFilter()">重置</button>
      <span class="fresh" id="user-count"></span>
    </div>
    <div style="overflow-x:auto"><table id="user-table"></table></div>
    <div class="pager" id="user-pager"></div>
    <div class="fresh" style="margin-top:8px">点行内「调账」可直接改该用户积分，无需输入用户 ID。</div>
  </section>
  <section>
    <details>
      <summary>按用户 ID 直接调账（正数充值 / 负数扣减）</summary>
      <div class="row">
        <input id="adj-uid" placeholder="用户 ID" size="8">
        <input id="adj-delta" placeholder="变动积分，如 100 或 -50" size="18">
        <input id="adj-reason" placeholder="原因*" size="30">
        <button class="pri" onclick="adjustCredits()">确认调整</button>
      </div>
      <div class="fresh">每次调账都会写入账本流水，可追溯。</div>
    </details>
  </section>
  <section>
    <h2>LLM 用量流水（最近 50 条）</h2>
    <div class="filters">
      <input id="q-usage" placeholder="用户 ID" size="10" oninput="filterUsage(true)">
      <input id="q-usage-model" placeholder="模型关键词" size="16" oninput="filterUsage(true)">
      <button onclick="resetUsageFilter()">重置</button>
      <span class="fresh" id="usage-count"></span>
    </div>
    <div style="overflow-x:auto"><table id="usage-table"></table></div>
    <div class="pager" id="usage-pager"></div>
  </section>
</div>

<div class="view" id="v-model">
  <div class="view-head"><h1>模型设置</h1>
    <div class="sub">切换大语言模型与 API Key · 保存后对话服务下一句话即刻生效</div></div>
  <section>
    <h2>当前生效模型 <span class="fresh" id="llm-state"></span></h2>
    <div class="active off" id="active-card">
      <div class="ac-top">
        <span class="ac-title">正在使用</span>
        <span class="ac-badge gray" id="ac-badge">读取中</span>
      </div>
      <div class="ac-model none" id="ac-model">—</div>
      <div class="ac-meta" id="ac-meta"></div>
      <div class="ac-live" id="ac-live"></div>
    </div>
  </section>
  <section>
    <h2>大语言模型设置 <span class="fresh" id="llm-msg-hint"></span></h2>
    <div class="msg" id="llm-msg"></div>
    <div class="row">
      <label><input type="checkbox" id="llm-enabled"> 启用 LLM 抽取/重排（不勾选则全走规则）</label>
    </div>
    <div class="row">
      <select id="llm-provider" onchange="applyPreset()">
        <option value="deepseek">DeepSeek</option>
        <option value="openai">OpenAI</option>
        <option value="moonshot">Moonshot Kimi</option>
        <option value="qwen">通义千问</option>
        <option value="zhipu">智谱 GLM</option>
        <option value="openrouter">OpenRouter</option>
        <option value="custom">自定义</option>
      </select>
      <input id="llm-base" placeholder="Base URL" size="42">
      <input id="llm-model" placeholder="模型名，如 deepseek-chat" size="20">
    </div>
    <div class="row">
      <input id="llm-key" type="password" placeholder="API Key" size="30">
      <input id="llm-timeout" placeholder="超时(秒)" size="7" value="60">
      <button class="pri" onclick="saveLlm()">保存</button>
      <button onclick="testLlm()">测试连接</button>
    </div>
    <div class="row">
      <input id="llm-tmapkey" type="password" placeholder="腾讯地图 Key（选填）" size="30">
      <span style="font-size:12px;color:#8A8A82;align-self:center;">
        行程页真实地图用 · lbs.qq.com 免费申请（勾选 WebServiceAPI）</span>
    </div>
    <div class="fresh">保存后对话服务下一句话即刻生效，无需重启。Key 只保存在本机设置文件中，界面上永远只显示脱敏形式；留空表示不修改。</div>
  </section>
</div>
</main>
</div>

<script>
const $=id=>document.getElementById(id);
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escq(s){return esc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
function show(ok,text){const m=$('msg');m.className='msg '+(ok?'ok':'bad');m.textContent=text;}

async function api(path, body){
  const opt = body ? {method:'POST',headers:{'Content-Type':'application/json'},
                      body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  return r.json();
}

async function loadStats(){
  const s = await api('/api/stats');
  const cards=$('cards');
  if(cards) cards.innerHTML = [['城市',s.cities],['POI',s.pois],['酒店',s.hotels],['班次',s.schedules]]
    .map(c=>'<div class="card"><b>'+c[1]+'</b><span>'+c[0]+'</span></div>').join('');
  const sel=$('city-sel');
  if(sel){
    const cur=sel.value;
    sel.innerHTML = '<option value="*">全部城市</option>'
      + (s.freshness||[]).map(f=>'<option>'+esc(f.city)+'</option>').join('');
    if(cur==='*' || (cur && s.freshness.some(f=>f.city===cur))) sel.value=cur;
    else sel.value='*';
  }
  const ft=$('fresh-table');
  if(ft){
    FRESH_ROWS=(s.freshness||[]);
    renderFresh();
  }
  return s;
}

function renderFresh(){
  const ft=$('fresh-table'); if(!ft)return;
  const size=getPageSize('fresh');
  const total=FRESH_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.fresh||1; if(page>pages)page=pages; PAGES.fresh=page;
  const slice=FRESH_ROWS.slice((page-1)*size,(page-1)*size+size);
  const rows=slice.map(f=>'<tr><td>'+esc(f.city)+'</td><td>'+(f.pois??0)+'</td>'
    +'<td>'+esc(f.sources||'-')+'</td><td>'+esc(f.last_update||'-')+'</td></tr>').join('');
  ft.innerHTML='<tr><th>城市</th><th>POI 条数</th><th>数据来源</th><th>最近更新</th></tr>'
    +(rows||'<tr><td colspan="4" class="empty">暂无数据</td></tr>');
  $('fresh-pager').innerHTML=pagerHtml('fresh',page,pages,total);
}

// ---------------- 通用分页 ----------------
const PAGE_SIZES=[10,20,50,100];
const PAGES={fresh:1,poi:1,log:1,user:1,usage:1};   // 各表当前页码
let FRESH_ROWS=[];

function getPageSize(k){
  const v=Number(localStorage.getItem('admin_psize_'+k));
  return PAGE_SIZES.indexOf(v)>=0?v:20;
}

function pagerHtml(k,page,pages,total){
  let btns='';
  btns+='<button'+(page<=1?' disabled':'')+' onclick="gotoPage(\\''+k+'\\',1)">« 首页</button>';
  btns+='<button'+(page<=1?' disabled':'')+' onclick="gotoPage(\\''+k+'\\','+(page-1)+')">上一页</button>';
  const from=Math.max(1,page-2), to=Math.min(pages,page+2);
  if(from>1)btns+='<span class="pg-dots">…</span>';
  for(let p=from;p<=to;p++)
    btns+='<button class="'+(p===page?'pg-cur':'')+'" onclick="gotoPage(\\''+k+'\\','+p+')">'+p+'</button>';
  if(to<pages)btns+='<span class="pg-dots">…</span>';
  btns+='<button'+(page>=pages?' disabled':'')+' onclick="gotoPage(\\''+k+'\\','+(page+1)+')">下一页</button>';
  btns+='<button'+(page>=pages?' disabled':'')+' onclick="gotoPage(\\''+k+'\\','+pages+')">末页 »</button>';
  const sizes=PAGE_SIZES.map(function(n){
    return '<option value="'+n+'"'+(getPageSize(k)===n?' selected':'')+'>'+n+' 行/页</option>';
  }).join('');
  return btns
    +'<span class="pg-info">第 '+page+' / '+pages+' 页 · 共 '+total+' 条</span>'
    +'<select onchange="changePageSize(\\''+k+'\\',this.value)">'+sizes+'</select>'
    +'<input class="pg-jump" type="number" min="1" max="'+pages+'" placeholder="页码" '
    +'onkeydown="if(event.key===\\'Enter\\')jumpPage(\\''+k+'\\',this.value)">';
}

function gotoPage(k,p){
  PAGES[k]=p;
  if(k==='poi')renderPois();
  else if(k==='log')renderLogs();
  else if(k==='user')renderUsers();
  else if(k==='usage')renderUsage();
  else if(k==='fresh')renderFresh();
}

function jumpPage(k,v){
  v=parseInt(v,10);
  if(isFinite(v)&&v>=1)gotoPage(k,v);
}

function changePageSize(k,v){
  try{localStorage.setItem('admin_psize_'+k,v);}catch(e){}
  PAGES[k]=1;
  gotoPage(k,1);
}

// ---------------- POI：条件搜索 + 表格行内编辑 ----------------
let POI_CUR=[];        // 当前查询结果
let POI_ROWS={};       // poi_id -> 原始行（含 tags/note 等未展示字段，保存时原样回传）
let POI_EDIT=null;     // 正在行内编辑的 poi_id
let POI_T=null;        // 搜索防抖定时器

function num(v){return (v===null||v===undefined)?'-':v;}

function debouncePoi(){clearTimeout(POI_T);POI_T=setTimeout(loadPois,250);}

async function loadPois(){
  const s=await loadStats();                       // 城市下拉先就绪
  const city=$('city-sel').value||'*';
  const qs='city='+encodeURIComponent(city)
    +'&q='+encodeURIComponent($('q-poi').value.trim())
    +'&category='+encodeURIComponent($('flt-cat').value)
    +'&source='+encodeURIComponent($('flt-src').value);
  const r=await api('/api/pois?'+qs);
  if(!r.ok){show(false,'加载失败：'+(r.error||'未知错误'));return;}
  POI_CUR=r.items||[];
  POI_ROWS={};
  POI_CUR.forEach(function(p){POI_ROWS[p.poi_id]=p;});
  const fresh=(s.freshness||[]).find(function(f){return f.city===city;});
  $('poi-fresh').textContent=(city==='*'?'全部城市':'城市 '+city)
    +(fresh?(' · 共 '+fresh.pois+' 条 · 来源 '+(fresh.sources||'-')+' · 最近更新 '+fresh.last_update):'');
  $('poi-count').textContent='命中 '+POI_CUR.length+' 条';
  POI_EDIT=null;
  PAGES.poi=1;
  renderPois();
}

function renderPois(){
  const size=getPageSize('poi');
  const total=POI_CUR.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.poi||1; if(page>pages)page=pages; PAGES.poi=page;
  const start=(page-1)*size;
  const slice=POI_CUR.slice(start,start+size);
  const head='<tr><th>城市</th><th>ID</th><th>名称</th><th>类型</th><th>区域</th>'
    +'<th>坐标</th><th>吸引</th><th>分钟</th><th>¥</th><th>来源</th><th>操作</th></tr>';
  const body=slice.map(function(p,i){
    return (p.poi_id===POI_EDIT)?editRowHtml(p,start+i):viewRowHtml(p,start+i);
  }).join('');
  $('poi-table').innerHTML=head+(body
    ||'<tr><td colspan="11" class="empty">没有符合条件的 POI</td></tr>');
  $('poi-pager').innerHTML=pagerHtml('poi',page,pages,total);
}

function viewRowHtml(p,i){
  return '<tr><td>'+esc(p.city)+'</td><td>'+esc(p.poi_id)+'</td><td>'+esc(p.name)+'</td>'
    +'<td>'+esc(p.category)+'</td><td>'+esc(p.area||'')+'</td>'
    +'<td>'+num(p.geo_lat)+', '+num(p.geo_lon)+'</td>'
    +'<td>'+p.appeal+'</td><td>'+p.duration_min+'</td><td>'+p.cost+'</td>'
    +'<td><span class="tag">'+esc(p.source)+'</span></td>'
    +'<td><button onclick="startEdit('+i+')">编辑</button> '
    +'<button class="del" onclick="delPoiAt('+i+')">删</button></td></tr>';
}

function editRowHtml(p,i){
  let extra='';
  if(p.category && ['attraction','restaurant'].indexOf(p.category)<0){
    extra='<option value="'+escq(p.category)+'" selected>'+esc(p.category)+'</option>';
  }
  return '<tr class="editing"><td>'+esc(p.city)+'</td><td>'+esc(p.poi_id)+'</td>'
    +'<td><input class="cell" id="e-name" value="'+escq(p.name)+'" size="16"></td>'
    +'<td><select class="cell" id="e-cat">'+extra
      +'<option value="attraction"'+((p.category==='attraction')?' selected':'')+'>attraction</option>'
      +'<option value="restaurant"'+((p.category==='restaurant')?' selected':'')+'>restaurant</option>'
    +'</select></td>'
    +'<td><input class="cell" id="e-area" value="'+escq(p.area||'')+'" size="9"></td>'
    +'<td><input class="cell" id="e-lat" type="number" step="0.0001" value="'+num(p.geo_lat)+'" style="width:84px"> '
    +'<input class="cell" id="e-lon" type="number" step="0.0001" value="'+num(p.geo_lon)+'" style="width:84px"></td>'
    +'<td><input class="cell" id="e-appeal" type="number" min="1" max="5" value="'+p.appeal+'" style="width:56px"></td>'
    +'<td><input class="cell" id="e-dur" type="number" min="0" value="'+p.duration_min+'" style="width:62px"></td>'
    +'<td><input class="cell" id="e-cost" type="number" min="0" value="'+p.cost+'" style="width:62px"></td>'
    +'<td><span class="tag">'+esc(p.source)+'</span></td>'
    +'<td><button class="pri" onclick="saveRow('+i+')">保存</button> '
    +'<button onclick="cancelRow()">取消</button></td></tr>';
}

function startEdit(i){
  const p=POI_CUR[i]; if(!p)return;
  POI_EDIT=p.poi_id; renderPois();
  const el=$('e-name'); if(el){el.focus();el.select();}
}

function cancelRow(){POI_EDIT=null;renderPois();}

async function saveRow(i){
  const p=POI_CUR[i]; if(!p)return;
  const name=$('e-name').value.trim();
  const lat=Number($('e-lat').value), lon=Number($('e-lon').value);
  if(!name){show(false,'名称不能为空');return;}
  if(!isFinite(lat)||!isFinite(lon)){show(false,'纬度 / 经度必须是数字');return;}
  let tags=[]; try{tags=JSON.parse(p.tags||'[]');}catch(e){tags=[];}
  // 未展示字段原样回传，否则 upsert 会用默认值覆盖 tags/note/intensity
  const item={poi_id:p.poi_id, name:name, category:$('e-cat').value,
    area:$('e-area').value.trim(), geo:[lat,lon],
    appeal:Number($('e-appeal').value)||3,
    duration_min:Number($('e-dur').value)||0,
    cost:Number($('e-cost').value)||0,
    open_hours:p.open_hours||'全天', open_days:p.open_days||'每天',
    need_booking:!!p.need_booking, tags:tags,
    intensity:p.intensity||'low', indoor_alternative:p.indoor_alternative||'',
    note:p.note||''};
  const r=await api('/api/poi/save',{city:p.city,item:item});
  if(!r.ok){show(false,'保存失败：'+(r.error||'未知错误'));return;}
  show(true,'已保存「'+name+'」—— 对话服务立即可用');
  POI_EDIT=null;
  loadPois();
}

function resetPoiFilter(){
  $('q-poi').value=''; $('flt-cat').value=''; $('flt-src').value='';
  $('city-sel').value='*';
  loadPois();
}

async function savePoi(){
  const city=$('city-sel').value;
  if(!city||city==='*'){show(false,'新增前请先在筛选条选择一个具体城市');return;}
  const geo=$('f-geo').value.split(',').map(Number);
  if(!$('f-name').value.trim()||geo.length!==2||!isFinite(geo[0])||!isFinite(geo[1])){
    show(false,'需要名称与 纬度,经度');return;
  }
  const r = await api('/api/poi/save',{city:city,item:{
    poi_id:$('f-id').value.trim()||undefined, name:$('f-name').value.trim(),
    area:$('f-area').value.trim(), geo:geo, category:$('f-cat').value,
    appeal:Number($('f-appeal').value)||3, duration_min:Number($('f-dur').value)||0,
    cost:Number($('f-cost').value)||0, open_hours:$('f-hours').value||'全天'}});
  show(r.ok,'已新增 '+(r.poi_id||'')+' —— 对话服务立即可用');
  if(r.ok){clearPoiForm();loadPois();}
}

function clearPoiForm(){
  ['f-id','f-name','f-area','f-geo'].forEach(function(k){$(k).value='';});
  $('f-appeal').value=3; $('f-dur').value=90; $('f-cost').value=0;
  $('f-hours').value='全天';
}

async function delPoiAt(i){
  const p=POI_CUR[i]; if(!p)return;
  if(!confirm('删除 '+p.poi_id+'「'+p.name+'」？'))return;
  const r=await api('/api/poi/delete',{poi_id:p.poi_id});
  show(r.ok,'已删除 '+p.poi_id);
  if(r.ok)loadPois();
}

async function syncCity(){
  const city=$('city-sel').value;
  if(!city||city==='*'){show(false,'请先在筛选条选择一个具体城市再同步');return;}
  show(true,'正在同步 '+city+'，Overpass 抓取可能需要几十秒…');
  const r=await api('/api/sync',{city:city});
  show(r.status==='success', r.status==='success'
    ? ('同步成功：新增 '+r.added+' 条') : ('同步失败：'+r.message));
  loadPois(); loadLogs();
}

async function importJson(){
  let items;
  try{ items=JSON.parse($('imp-json').value); }
  catch(e){ show(false,'JSON 解析失败：'+e.message); return; }
  if(!Array.isArray(items)){ show(false,'需要 JSON 数组'); return; }
  const city=$('imp-city').value.trim()||$('city-sel').value;
  if(!city||city==='*'){ show(false,'请填写所属城市，或先在筛选条选择具体城市'); return; }
  const r=await api('/api/import',{city:city,kind:$('imp-kind').value,items:items});
  show(r.ok,'导入完成：新增 '+r.added+'，更新 '+r.updated
    +(r.errors.length?('，被拒：'+r.errors.join('；')):''));
  loadPois(); loadStats(); loadLogs();
}

// ---------------- 同步日志：条件搜索 ----------------
let LOGS=[];
let LOG_ROWS=[];

async function loadLogs(){
  const r=await api('/api/logs');
  LOGS=r.logs||[];
  filterLogs();
}

function filterLogs(reset){
  if(reset)PAGES.log=1;
  const q=$('q-log').value.trim().toLowerCase();
  const st=$('flt-log-status').value;
  LOG_ROWS=LOGS.filter(function(l){
    if(st && l.status!==st)return false;
    if(!q)return true;
    return ((l.city||'')+' '+(l.source||'')+' '+(l.message||'')).toLowerCase().indexOf(q)>=0;
  });
  $('log-count').textContent='命中 '+LOG_ROWS.length+' / '+LOGS.length+' 条';
  renderLogs();
}

function renderLogs(){
  const size=getPageSize('log');
  const total=LOG_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.log||1; if(page>pages)page=pages; PAGES.log=page;
  const slice=LOG_ROWS.slice((page-1)*size,(page-1)*size+size);
  $('log-table').innerHTML='<tr><th>时间</th><th>城市</th><th>来源</th><th>状态</th><th>新增</th><th>说明</th></tr>'
    +(slice.length?slice.map(function(l){
      return '<tr><td>'+esc(l.ts)+'</td><td>'+esc(l.city)+'</td><td>'+esc(l.source)
        +'</td><td style="color:'+(l.status==='success'?'var(--ok)':'var(--bad)')+'">'+esc(l.status)
        +'</td><td>'+l.added+'</td><td>'+esc(l.message)+'</td></tr>';
    }).join(''):'<tr><td colspan="6" class="empty">没有符合条件的日志</td></tr>');
  $('log-pager').innerHTML=pagerHtml('log',page,pages,total);
}

function resetLogFilter(){$('q-log').value='';$('flt-log-status').value='';filterLogs(true);}

function delAllCheck(){
  showView('data');
  setTimeout(function(){
    const t=$('log-table'); if(t)t.scrollIntoView({behavior:'smooth',block:'center'});
  },150);
}

// ---------------- 大语言模型设置 ----------------
const LLM_PRESETS={
  deepseek:['https://api.deepseek.com/v1','deepseek-chat'],
  openai:['https://api.openai.com/v1','gpt-4o-mini'],
  moonshot:['https://api.moonshot.cn/v1','moonshot-v1-8k'],
  qwen:['https://dashscope.aliyuncs.com/compatible-mode/v1','qwen-plus'],
  zhipu:['https://open.bigmodel.cn/api/paas/v4','glm-4-flash'],
  openrouter:['https://openrouter.ai/api/v1','openai/gpt-4o-mini'],
  custom:['','']};

function applyPreset(){
  const p=LLM_PRESETS[$('llm-provider').value];
  if(p&&p[0]){$('llm-base').value=p[0];$('llm-model').value=p[1];}
}

function showLlm(ok,text){
  const m=$('llm-msg');m.className='msg '+(ok?'ok':'bad');m.textContent=text;
}

// ---------------- 当前生效模型卡片 ----------------
const LLM_NAMES={deepseek:'DeepSeek',openai:'OpenAI',moonshot:'Moonshot Kimi',
  qwen:'通义千问',zhipu:'智谱 GLM',openrouter:'OpenRouter',custom:'自定义'};
const SOURCE_NAMES={settings:'后台设置文件',env:'环境变量兜底'};

function renderActive(s){
  const a=s.active||{}, chat=s.chat||{};
  const card=$('active-card'), badge=$('ac-badge'), model=$('ac-model');
  const meta=$('ac-meta'), live=$('ac-live');
  const online=!!chat.online;
  // 只有对话服务明确上报了模型信息才以它为准；旧版服务/离线一律以配置为准，
  // 否则「服务在线但没上报」会被误显示成未启用。
  const detailed=online && !!chat.has_detail;
  const using=detailed?!!chat.enabled:!!a.enabled;
  const name=((detailed?chat.model:a.model)||'').trim();
  const src=(detailed?chat.source:a.source)||'';
  const provider=(detailed?chat.provider:a.provider)||'';
  const baseUrl=(detailed?chat.base_url:a.base_url)||'';
  const key=(detailed?chat.key_masked:a.key_masked)||'';

  // 状态：配置不完整 > 已启用 > 纯规则
  const broken=!!a.enabled && !a.key_masked;
  card.className='active'+(broken?' err':(using?'':' off'));
  if(broken){
    badge.className='ac-badge red'; badge.textContent='配置不完整：缺 Key';
  }else if(using){
    badge.className='ac-badge'; badge.textContent='已启用';
  }else{
    badge.className='ac-badge gray'; badge.textContent='未启用 · 纯规则';
  }

  model.className='ac-model'+(using?'':' none');
  model.textContent=using?(name||'（未填模型名）'):'未启用大语言模型，抽取与重排全部走规则';

  const bits=[];
  if(src) bits.push('来源 '+esc(SOURCE_NAMES[src]||src));
  if(provider&&LLM_NAMES[provider]) bits.push(LLM_NAMES[provider]);
  if(baseUrl) bits.push('<code>'+esc(baseUrl)+'</code>');
  if(key) bits.push('Key <code>'+esc(key)+'</code>');
  const to=a.timeout||chat.timeout;
  if(to) bits.push('超时 '+esc(to)+'s');
  if(src==='settings'&&a.updated_at) bits.push('保存于 '+esc(String(a.updated_at).replace('T',' ')));
  meta.innerHTML=bits.join(' &middot; ');

  if(detailed){
    if(chat.enabled){
      const how=chat.source==='env'?'由环境变量提供':'由后台设置提供';
      live.innerHTML='对话服务实时确认：正在使用 <b>'+esc(chat.model||'')+'</b>（'+how+'）'
        +' · 改动保存后下一句话自动切换，无需重启';
    }else{
      live.innerHTML='对话服务在线，但当前未加载模型（纯规则模式）。勾选启用并保存后，下一句话即生效。';
    }
  }else if(online){
    live.innerHTML='对话服务在线，但版本较旧未上报模型信息，此处按配置显示。'
      +'重启对话服务（start_agent）后可看到实时确认。';
  }else{
    live.innerHTML='未探测到对话服务（可能未启动或未响应）· 以上为配置文件中的设置，服务启动后自动生效';
  }
}

async function loadLlm(){
  const r=await api('/api/llm/settings');
  if(!r.ok)return;
  const s=r.settings||{};
  $('llm-enabled').checked=!!s.enabled;
  $('llm-provider').value=s.provider||'custom';
  $('llm-base').value=s.base_url||'';
  $('llm-model').value=s.model||'';
  $('llm-timeout').value=s.timeout||60;
  $('llm-key').value='';
  $('llm-key').placeholder=s.has_key?('已保存 '+s.key_masked+'（留空则不修改）'):'API Key';
  $('llm-tmapkey').value='';
  $('llm-tmapkey').placeholder=s.has_tmap_key
    ?('已保存 '+s.tmap_masked+'（留空则不修改）'):'腾讯地图 Key（选填）';
  $('llm-state').textContent=(s.chat&&s.chat.online)
    ?(s.chat.has_detail?'对话服务在线 · 实时读取':'对话服务在线 · 旧版本，按配置显示')
    :'对话服务未探测到 · 显示配置值';
  renderActive(s);
}

async function saveLlm(){
  const r=await api('/api/llm/settings',{enabled:$('llm-enabled').checked,
    provider:$('llm-provider').value, base_url:$('llm-base').value.trim(),
    model:$('llm-model').value.trim(), timeout:Number($('llm-timeout').value)||60,
    api_key:$('llm-key').value.trim(),
    tmap_key:$('llm-tmapkey').value.trim()});
  showLlm(r.ok, r.ok?'已保存 —— 对话服务下一句话即刻生效':('保存失败：'+(r.error||'未知错误')));
  loadLlm();
}

async function testLlm(){
  showLlm(true,'正在请求模型，最长等待 20 秒…');
  const r=await api('/api/llm/test',{api_key:$('llm-key').value.trim(),
    base_url:$('llm-base').value.trim(), model:$('llm-model').value.trim()});
  showLlm(r.ok, r.ok?('连通正常：'+r.model+' · '+r.latency_ms+'ms · 回复「'+r.reply+'」')
    :('连接失败：'+(r.error||'未知错误')));
}

// ---------------- 用户与积分：条件搜索 + 行内调账 ----------------
let USERS=[];
let USER_ROWS=[];    // 当前过滤结果
let ADJ_UID=null;          // 展开行内调账的用户 ID
let USAGE=[];
let USAGE_ROWS=[];   // 当前过滤结果

function showUser(ok,text){
  const m=$('user-msg');m.className='msg '+(ok?'ok':'bad');m.textContent=text;
}

async function loadUsers(){
  const r=await api('/api/users');
  if(!r.ok)return;
  USERS=r.users||[];
  $('user-fresh').textContent='共 '+r.total+' 人 · 注册赠 '+r.bonus+' 积分 · 免费档日上限 '+r.daily_cap;
  filterUsers();
}

function filterUsers(reset){
  if(reset)PAGES.user=1;
  const q=$('q-user').value.trim();
  const st=$('flt-status').value;
  const mv=$('flt-min').value;
  const min=mv===''?-Infinity:Number(mv);
  USER_ROWS=USERS.filter(function(u){
    if(st && u.status!==st)return false;
    if((u.balance||0)<min)return false;
    if(!q)return true;
    return String(u.id).indexOf(q)>=0 || (u.phone||'').indexOf(q)>=0;
  });
  $('user-count').textContent='命中 '+USER_ROWS.length+' / '+USERS.length+' 人';
  renderUsers();
}

function renderUsers(){
  const size=getPageSize('user');
  const total=USER_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.user||1; if(page>pages)page=pages; PAGES.user=page;
  const slice=USER_ROWS.slice((page-1)*size,(page-1)*size+size);
  const body=slice.map(function(u){
    const bal=u.is_unlimited
      ? '<span class="tag">∞ 额度</span>'
      : ('<b>'+u.balance+'</b>（冻结 '+u.frozen+'）');
    const st=u.is_unlimited?'不限额测试':(u.status||'');
    let h='<tr><td>'+u.id+'</td><td>'+esc(u.phone)+'</td>'
      +'<td>'+bal+'</td><td>'+u.used+'</td>'
      +'<td>'+esc(u.created_at||'')+'</td><td>'+esc(st)+'</td>'
      +'<td><button onclick="adjRow('+u.id+')">调账</button> '
      +'<button onclick="showUsage('+u.id+')">用量</button></td></tr>';
    if(u.id===ADJ_UID){
      h+='<tr class="editrow"><td colspan="7">调账用户 #'+u.id+' '+esc(u.phone)+'：'
        +'<input id="radj-delta" placeholder="变动积分，如 100 或 -50" size="18"> '
        +'<input id="radj-reason" placeholder="原因*" size="26"> '
        +'<button class="pri" onclick="applyRowAdjust('+u.id+')">确认</button> '
        +'<button onclick="adjRow('+u.id+')">取消</button>'
        +'<span class="fresh" style="margin-left:8px">当前余额 '+u.balance+'</span></td></tr>';
    }
    return h;
  }).join('');
  $('user-table').innerHTML='<tr><th>ID</th><th>手机号</th><th>余额</th><th>累计消耗</th>'
    +'<th>注册时间</th><th>状态</th><th>操作</th></tr>'
    +(body||'<tr><td colspan="7" class="empty">没有符合条件的用户</td></tr>');
  $('user-pager').innerHTML=pagerHtml('user',page,pages,total);
}

function adjRow(uid){
  ADJ_UID=(ADJ_UID===uid)?null:uid;
  filterUsers();
  if(ADJ_UID){const el=$('radj-delta'); if(el)el.focus();}
}

async function applyRowAdjust(uid){
  const delta=Number($('radj-delta').value);
  const reason=$('radj-reason').value.trim();
  if(!delta||!reason){showUser(false,'需要变动积分和原因');return;}
  const r=await api('/api/user/credits',{user_id:uid,delta:delta,reason:reason});
  showUser(r.ok, r.ok?('已调整：用户 '+uid+' '+delta+' 积分'):('调整失败：'+(r.error||'')));
  if(r.ok){ADJ_UID=null;loadUsers();}
}

function resetUserFilter(){
  $('q-user').value=''; $('flt-status').value=''; $('flt-min').value='';
  filterUsers(true);
}

async function adjustCredits(){
  const uid=Number($('adj-uid').value), delta=Number($('adj-delta').value);
  const reason=$('adj-reason').value.trim();
  if(!uid||!delta||!reason){showUser(false,'需要用户 ID、变动积分和原因');return;}
  const r=await api('/api/user/credits',{user_id:uid,delta:delta,reason:reason});
  showUser(r.ok, r.ok?('已调整：用户 '+uid+' '+delta+' 积分'):('调整失败：'+(r.error||'')));
  if(r.ok){$('adj-uid').value='';$('adj-delta').value='';$('adj-reason').value='';}
  loadUsers();
}

function showUsage(uid){
  $('q-usage').value=uid;
  loadUsage(uid);
}

async function loadUsage(uid){
  const r=await api('/api/usage'+(uid?('?uid='+uid):''));
  if(!r.ok)return;
  USAGE=r.usage||[];
  if(uid)$('q-usage').value=uid;
  filterUsage(!!uid);                              // 换了用户查询，回到第 1 页
  const t=$('usage-table'); if(t)t.scrollIntoView({behavior:'smooth',block:'center'});
}

function filterUsage(reset){
  if(reset)PAGES.usage=1;
  const uid=$('q-usage').value.trim();
  const m=$('q-usage-model').value.trim().toLowerCase();
  USAGE_ROWS=USAGE.filter(function(u){
    if(uid && String(u.user_id).indexOf(uid)<0)return false;
    if(m && (u.model||'').toLowerCase().indexOf(m)<0)return false;
    return true;
  });
  $('usage-count').textContent='命中 '+USAGE_ROWS.length+' / '+USAGE.length+' 条';
  renderUsage();
}

function renderUsage(){
  const size=getPageSize('usage');
  const total=USAGE_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.usage||1; if(page>pages)page=pages; PAGES.usage=page;
  const slice=USAGE_ROWS.slice((page-1)*size,(page-1)*size+size);
  $('usage-table').innerHTML='<tr><th>时间</th><th>用户</th><th>模型</th><th>任务</th>'
    +'<th>输入tok</th><th>输出tok</th><th>积分</th></tr>'
    +(slice.length?slice.map(function(u){
      return '<tr><td>'+esc(u.created_at)+'</td><td>'+u.user_id
        +'</td><td>'+esc(u.model||'')+'</td><td>'+esc(u.task||'')+'</td>'
        +'<td>'+(u.prompt_tokens??'-')+'</td><td>'+(u.completion_tokens??'-')+'</td>'
        +'<td><b>'+u.credits+'</b></td></tr>';
    }).join(''):'<tr><td colspan="7" class="empty">没有符合条件的流水</td></tr>');
  $('usage-pager').innerHTML=pagerHtml('usage',page,pages,total);
}

function resetUsageFilter(){
  $('q-usage').value=''; $('q-usage-model').value=''; filterUsage(true);
}

// ---------------- 左侧导航 / 视图切换 ----------------
const VIEWS=['overview','data','users','model'];

function showView(v){
  if(VIEWS.indexOf(v)<0)v='overview';
  let i, els, as;
  els=document.querySelectorAll('.view');
  for(i=0;i<els.length;i++) els[i].classList.toggle('on', els[i].id==='v-'+v);
  as=document.querySelectorAll('.nav a');
  for(i=0;i<as.length;i++) as[i].classList.toggle('on', as[i].getAttribute('href')==='#/'+v);
  if(v==='overview') loadStats();
  else if(v==='data') loadPois().then(loadLogs);
  else if(v==='users'){ loadUsers(); loadUsage(); }
  else if(v==='model') loadLlm();
  window.scrollTo({top:0});
}

function route(){
  const v=(location.hash||'').replace(/^#[/]?/,'');
  showView(VIEWS.indexOf(v)>=0?v:'overview');
}

window.addEventListener('hashchange',route);
route();
</script>
</body>
</html>"""


# ---------------------------------------------------------------- HTTP 服务

def _chat_llm_state() -> dict:
    """探测对话服务此刻实际加载的模型。

    配置文件和「进程里真正在用的」可能不同（服务没起、刚改还没触发重载），
    后台要能看出这个差别，所以直接问 /api/health；连不上就标 offline 而不是报错。

    has_detail 表示对方是否上报了模型信息：旧版对话服务的 health 没有 llm 字段，
    此时 disabled 不能当成「未启用」——那会把配置里明明启用的模型误显示成纯规则。
    """
    try:
        with urllib.request.urlopen(CHAT_URL + "/api/health", timeout=1.5) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        st = j.get("llm")
        if not isinstance(st, dict):           # 旧版服务：健康检查不含 llm
            return {"online": True, "has_detail": False, "enabled": None,
                    "model": "", "base_url": "", "provider": "",
                    "source": "", "key_masked": ""}
        return {"online": True, "has_detail": True,
                "enabled": bool(st.get("enabled")),
                "model": st.get("model") or "",
                "base_url": st.get("base_url") or "",
                "provider": st.get("provider") or "",
                "source": st.get("source") or "",
                "key_masked": st.get("key_masked") or ""}
    except Exception as e:                     # 服务未启动 / 超时 / 返回异常都算离线
        return {"online": False, "has_detail": False,
                "error": f"{type(e).__name__}: {str(e)[:60]}"}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/admin"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/llm/settings":
            summary = llm.settings_summary()
            summary["chat"] = _chat_llm_state()      # 对话服务实时状态（不在线也不报错）
            self._json({"ok": True, "settings": summary})
            return
        if path == "/api/users":
            users = auth.list_users()
            self._json({"ok": True, "total": len(users), "users": users,
                        "bonus": auth.BONUS_CREDITS,
                        "daily_cap": auth.DAILY_CAP_CREDITS})
            return
        if path == "/api/usage":
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            uid = int(q["uid"][0]) if q.get("uid") else None
            self._json({"ok": True, "usage": auth.list_usage(uid)})
            return
        db = kb.get_kb()
        if db is None:
            self._json({"ok": False, "error": "知识库不可用"}, 500)
            return
        try:
            if path == "/api/stats":
                self._json({"ok": True, **db.stats()})
            elif path == "/api/pois":
                import urllib.parse
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                items = db.search_pois(
                    city=(qs.get("city") or [""])[0],
                    q=(qs.get("q") or [""])[0].strip(),
                    category=(qs.get("category") or [""])[0].strip(),
                    source=(qs.get("source") or [""])[0].strip())
                self._json({"ok": True, "items": items, "total": len(items)})
            elif path == "/api/logs":
                self._json({"ok": True, "logs": db.logs()})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        data = self._body()
        # 模型设置不依赖知识库，也无需拿 SQLite 锁（test 接口最长阻塞 20s）
        if path == "/api/llm/settings":
            self._save_llm_settings(data)
            return
        if path == "/api/llm/test":
            self._test_llm(data)
            return
        if path == "/api/user/credits":
            try:
                uid = int(data.get("user_id") or 0)
                delta = int(data.get("delta") or 0)
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "user_id/delta 应为整数"}, 400)
                return
            if delta == 0:
                self._json({"ok": False, "error": "变动量不能为 0"}, 400)
                return
            ok = auth.admin_adjust(uid, delta, str(data.get("reason") or ""))
            self._json({"ok": ok,
                        "error": None if ok else "用户不存在或扣减后余额为负"},
                        200 if ok else 400)
            return
        db = kb.get_kb()
        if db is None:
            self._json({"ok": False, "error": "知识库不可用"}, 500)
            return
        try:
            with _LOCK:
                if path == "/api/poi/save":
                    item = data.get("item") or {}
                    if not item.get("name"):
                        self._json({"ok": False, "error": "name 必填"}, 400)
                        return
                    pid = db.upsert_poi(data.get("city", ""), item)
                    self._json({"ok": True, "poi_id": pid})
                elif path == "/api/poi/delete":
                    ok = db.delete_poi(str(data.get("poi_id") or ""))
                    self._json({"ok": ok, "error": None if ok else "未找到该 POI"})
                elif path == "/api/import":
                    r = db.import_json(data.get("city", ""), data.get("kind", "poi"),
                                       data.get("items") or [])
                    self._json({"ok": not r["errors"], **r})
                elif path == "/api/sync":
                    r = db.sync_city(data.get("city", ""))
                    self._json({"ok": r["status"] == "success", **r})
                else:
                    self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------- 大语言模型设置 ----------------

    def _save_llm_settings(self, data: dict) -> None:
        enabled = bool(data.get("enabled"))
        base_url = str(data.get("base_url") or "").strip()
        model = str(data.get("model") or "").strip()
        try:
            timeout = max(5.0, min(300.0, float(data.get("timeout") or 60)))
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "超时秒数应为数字"}, 400)
            return
        if enabled and not model:
            self._json({"ok": False, "error": "启用 LLM 需要填写模型名"}, 400)
            return
        if enabled and not base_url.startswith("http"):
            self._json({"ok": False, "error": "Base URL 需以 http(s) 开头"}, 400)
            return

        cfg = {"enabled": enabled,
               "provider": str(data.get("provider") or "custom")[:40],
               "base_url": base_url,
               "model": model,
               "timeout": timeout}
        key = str(data.get("api_key") or "").strip()
        if key:
            if len(key) < 8:
                self._json({"ok": False, "error": "API Key 长度异常（至少 8 位）"}, 400)
                return
            cfg["api_key"] = key
        elif enabled and not llm.load_settings().get("api_key"):
            self._json({"ok": False, "error": "启用 LLM 需要先填写 API Key"}, 400)
            return
        # 腾讯地图 Key（行程页真实地图用），选填；留空表示不修改
        tkey = str(data.get("tmap_key") or "").strip()
        if tkey:
            if not all(ch.isalnum() or ch == "-" for ch in tkey) or len(tkey) < 10:
                self._json({"ok": False,
                            "error": "腾讯地图 Key 格式异常（字母/数字/连字符，"
                                     "lbs.qq.com 控制台可查）"}, 400)
                return
            cfg["tmap_key"] = tkey
        llm.save_settings(cfg)
        self._json({"ok": True})

    def _test_llm(self, data: dict) -> None:
        """用表单当前值（含未保存的 Key）真实请求一次模型；空字段回落已保存配置。"""
        saved = llm.load_settings()
        api_key = (str(data.get("api_key") or "").strip()
                   or str(saved.get("api_key") or ""))
        base_url = (str(data.get("base_url") or "").strip()
                    or str(saved.get("base_url") or "https://api.openai.com/v1"))
        model = (str(data.get("model") or "").strip()
                 or str(saved.get("model") or ""))
        if not api_key:
            self._json({"ok": False, "error": "未配置 API Key（表单与已保存设置里都没有）"})
            return
        if not model:
            self._json({"ok": False, "error": "未指定模型名"})
            return
        client = llm.Client(api_key=api_key, base_url=base_url, model=model,
                            timeout=20.0, max_attempts=1)
        t0 = time.time()
        try:
            reply = client.chat("你是连通性探针。", "只回复两个字：正常")
            self._json({"ok": True, "model": model, "base_url": client.base_url,
                        "reply": (reply or "").strip()[:50],
                        "latency_ms": int((time.time() - t0) * 1000)})
        except llm.LLMError as e:
            self._json({"ok": False, "error": str(e)[:300],
                        "latency_ms": int((time.time() - t0) * 1000)})


def main() -> int:
    ap = argparse.ArgumentParser(description="旅行助手 · 数据管理后端")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    a = ap.parse_args()

    db = kb.get_kb()
    if db is None:
        print("知识库初始化失败")
        return 1
    n = db.seed_if_empty()
    url = f"http://{HOST}:{a.port}/"
    print(f"数据管理后端已启动：{url}")
    print(f"知识库当前：{json.dumps({k: v for k, v in db.stats().items() if k != 'freshness'}, ensure_ascii=False)}"
          f"（本次播种补入 {n}）")
    print("Ctrl+C 停止服务。")
    if a.open:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        ThreadingHTTPServer((HOST, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
