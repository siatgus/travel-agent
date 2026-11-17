
const $=id=>document.getElementById(id);
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
// 各种时间是不同格式（YYYY-MM-DD / ISO / 带时分）：条件查询一律按日期段比较
function dayOf(v){var t=String(v==null?'':v);return t.slice(0,10);}
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
  REGIONS = s.regions || [];
  rebuildCascade();
  const ft=$('fresh-table');
  if(ft){
    FRESH_ALL=(s.freshness||[]);
    // 来源下拉按实际出现过的来源生成（seed/overpass/manual 混用时不用改前端）
    const sel=$('flt-fresh-src');
    if(sel){
      const cur=sel.value, srcs=[];
      FRESH_ALL.forEach(function(f){
        String(f.sources||'').split(/[,\s]+/).forEach(function(x){
          if(x && srcs.indexOf(x)<0)srcs.push(x);});
      });
      srcs.sort();
      sel.innerHTML='<option value="">全部来源</option>'
        +srcs.map(function(x){return '<option value="'+escq(x)+'">'+esc(x)+'</option>';}).join('');
      sel.value=(srcs.indexOf(cur)>=0)?cur:'';
    }
    filterFresh();
  }
  return s;
}

// ---- 城市→省→城市→区县 四级级联 -------------------------------------
// REGIONS: [{country, provinces:[{province, cities:[...]}]}]（/api/stats 下发）
// 每级都有「全部」选项；城市级保留原 #city-sel（新增/同步等逻辑复用）。
// 选省不选城市时按该省全部城市批量过滤（/api/pois 的 cities 参数）。
function cascadeCities(){
  const c=$('country-sel').value||'*', p=$('prov-sel').value||'*';
  if(c==='*') return null;                       // 不限：不过滤
  const node=REGIONS.find(r=>r.country===c);
  const provs=(node&&node.provinces)||[];
  if(p==='*') return [].concat(...provs.map(x=>x.cities));
  const pv=provs.find(x=>x.province===p);
  return pv?pv.cities.slice():[];
}
function setSel(sel, opts, keep){
  const cur=keep?sel.value:null;
  sel.innerHTML=opts.map(o=>'<option value="'+esc(o[0])+'">'+esc(o[1])+'</option>').join('');
  if(cur!==null && opts.some(o=>o[0]===cur)) sel.value=cur;
}
function rebuildCascade(){
  if(!$('country-sel')) return;
  // loadPois 会调 loadStats，loadStats 又触发级联重建回到 loadPois——
  // 树没变就直接返回，砍断这个环
  const sig=JSON.stringify(REGIONS);
  if(sig===CASCADE_SIG) return;
  CASCADE_SIG=sig;
  setSel($('country-sel'),
    [['*','全部国家']].concat(REGIONS.map(r=>[r.country,r.country])), true);
  onCountry(true);
}
function onCountry(keepProv){
  const c=$('country-sel').value||'*';
  const node=REGIONS.find(r=>r.country===c);
  const provs=(node&&node.provinces)||[];
  setSel($('prov-sel'), [['*','全部省/州']].concat(provs.map(p=>[p.province,p.province])),
    keepProv===true);
  onProv(true);
}
function onProv(keepCity){
  const cities=cascadeCities();
  const all=cities?cities.map(c=>[c,c]):(FRESH_ROWS||[]).map(f=>[f.city,f.city]);
  setSel($('city-sel'), [['*','全部城市']].concat(all), keepCity===true);
  if(!keepCity) $('city-sel').value='*';
  onCity();
}
function onCity(){
  const city=$('city-sel').value||'*';
  if(city==='*'){ $('area-sel').innerHTML='<option value="*">全部区县</option>'; loadPois(); return; }
  api('/api/city-areas?city='+encodeURIComponent(city)).then(function(r){
    const areas=(r&&r.areas)||[];
    setSel($('area-sel'), [['*','全部区县']]
      .concat(areas.map(a=>[a.area, a.area+' · '+a.pois+'条'])));
    $('area-sel').value='*';
    loadPois();
  }).catch(function(){ $('area-sel').innerHTML='<option value="*">全部区县</option>'; loadPois(); });
}

let FRESH_ALL=[];   // 新鲜度全量（筛选前）

function filterFresh(reset){
  if(reset)PAGES.fresh=1;
  const q=$('q-fresh').value.trim().toLowerCase();
  const src=$('flt-fresh-src').value;
  const bucket=$('flt-fresh-poi').value;
  const from=$('fresh-from').value, to=$('fresh-to').value;
  FRESH_ROWS=FRESH_ALL.filter(function(f){
    if(q && String(f.city||'').toLowerCase().indexOf(q)<0)return false;
    if(src && String(f.sources||'').indexOf(src)<0)return false;
    const n=f.pois||0;
    if(bucket==='zero' && n!==0)return false;
    if(bucket==='lt50' && !(n>0 && n<50))return false;
    if(bucket==='ge50' && n<50)return false;
    const d=dayOf(f.last_update);
    if(from && (!d || d<from))return false;
    if(to && (!d || d>to))return false;
    return true;
  });
  const cnt=$('fresh-count');
  if(cnt)cnt.textContent='命中 '+FRESH_ROWS.length+' / '+FRESH_ALL.length+' 城';
  renderFresh();
}

function resetFreshFilter(){
  $('q-fresh').value=''; $('flt-fresh-src').value='';
  $('flt-fresh-poi').value='';
  $('fresh-from').value=''; $('fresh-to').value='';
  filterFresh(true);
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
const PAGES={fresh:1,poi:1,log:1,user:1,usage:1,ord:1,mod:1,fin:1};   // 各表当前页码
let FRESH_ROWS=[];
let REGIONS=[];                                     // 国家→省→城市级联树（/api/stats）
let CASCADE_SIG='';                                 // 上次重建时的树签名（防 loadPois↔loadStats 递归环）

function getPageSize(k){
  const v=Number(localStorage.getItem('admin_psize_'+k));
  return PAGE_SIZES.indexOf(v)>=0?v:20;
}

function pagerHtml(k,page,pages,total){
  let btns='';
  btns+='<button'+(page<=1?' disabled':'')+' onclick="gotoPage(\''+k+'\',1)">« 首页</button>';
  btns+='<button'+(page<=1?' disabled':'')+' onclick="gotoPage(\''+k+'\','+(page-1)+')">上一页</button>';
  const from=Math.max(1,page-2), to=Math.min(pages,page+2);
  if(from>1)btns+='<span class="pg-dots">…</span>';
  for(let p=from;p<=to;p++)
    btns+='<button class="'+(p===page?'pg-cur':'')+'" onclick="gotoPage(\''+k+'\','+p+')">'+p+'</button>';
  if(to<pages)btns+='<span class="pg-dots">…</span>';
  btns+='<button'+(page>=pages?' disabled':'')+' onclick="gotoPage(\''+k+'\','+(page+1)+')">下一页</button>';
  btns+='<button'+(page>=pages?' disabled':'')+' onclick="gotoPage(\''+k+'\','+pages+')">末页 »</button>';
  const sizes=PAGE_SIZES.map(function(n){
    return '<option value="'+n+'"'+(getPageSize(k)===n?' selected':'')+'>'+n+' 行/页</option>';
  }).join('');
  return btns
    +'<span class="pg-info">第 '+page+' / '+pages+' 页 · 共 '+total+' 条</span>'
    +'<select onchange="changePageSize(\''+k+'\',this.value)">'+sizes+'</select>'
    +'<input class="pg-jump" type="number" min="1" max="'+pages+'" placeholder="页码" '
    +'onkeydown="if(event.key===\'Enter\')jumpPage(\''+k+'\',this.value)">';
}

function gotoPage(k,p){
  PAGES[k]=p;
  if(k==='poi')renderPois();
  else if(k==='log')renderLogs();
  else if(k==='user')renderUsers();
  else if(k==='usage')renderUsage();
  else if(k==='fresh')renderFresh();
  else if(k==='fin')renderOrders();
  else if(k==='mod')renderModeration();
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
  const s=await loadStats();                       // 级联下拉先就绪
  const city=$('city-sel').value||'*';
  const area=$('area-sel')?$('area-sel').value:'*';
  const scope=cascadeCities();                     // 选了国家/省但没选城市 → 批量
  let qs='';
  if(city!=='*'){ qs='city='+encodeURIComponent(city); }
  else if(scope){ qs='cities='+encodeURIComponent(scope.join(',')); }
  qs+='&q='+encodeURIComponent($('q-poi').value.trim())
    +'&category='+encodeURIComponent($('flt-cat').value)
    +'&source='+encodeURIComponent($('flt-src').value)
    +(area&&area!=='*'?'&area='+encodeURIComponent(area):'');
  const r=await api('/api/pois?'+qs);
  if(!r.ok){show(false,'加载失败：'+(r.error||'未知错误'));return;}
  POI_CUR=r.items||[];
  POI_ROWS={};
  POI_CUR.forEach(function(p){POI_ROWS[p.poi_id]=p;});
  const fresh=(s.freshness||[]).find(function(f){return f.city===city;});
  const label = city!=='*' ? ('城市 '+city)
    : ($('country-sel').value!=='*'
        ? ($('prov-sel').value!=='*' ? $('prov-sel').value : $('country-sel').value)
        : '全部城市');
  $('poi-fresh').textContent=label
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
  const r=await api('/api/logs?limit=500');
  LOGS=r.logs||[];
  // 来源下拉按实际出现过的来源生成，避免写死一套和同步实现脱节
  const sel=$('flt-log-src');
  if(sel){
    const cur=sel.value;
    const srcs=[];
    LOGS.forEach(function(l){
      if(l.source && srcs.indexOf(l.source)<0)srcs.push(l.source);});
    srcs.sort();
    sel.innerHTML='<option value="">全部来源</option>'
      +srcs.map(function(x){return '<option value="'+escq(x)+'">'+esc(x)+'</option>';}).join('');
    sel.value=(srcs.indexOf(cur)>=0)?cur:'';
  }
  filterLogs();
}

function filterLogs(reset){
  if(reset)PAGES.log=1;
  const q=$('q-log').value.trim().toLowerCase();
  const st=$('flt-log-status').value;
  const src=$('flt-log-src').value;
  const from=$('log-from').value, to=$('log-to').value;
  LOG_ROWS=LOGS.filter(function(l){
    if(st && l.status!==st)return false;
    if(src && l.source!==src)return false;
    const d=dayOf(l.ts);
    if(from && (!d || d<from))return false;
    if(to && (!d || d>to))return false;
    if(!q)return true;
    return ((l.city||'')+' '+(l.source||'')+' '+(l.message||'')).toLowerCase().indexOf(q)>=0;
  });
  const added=LOG_ROWS.reduce(function(a,l){return a+(l.added||0);},0);
  $('log-count').textContent='命中 '+LOG_ROWS.length+' / '+LOGS.length+' 条 · 合计新增 '+added;
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

function resetLogFilter(){$('q-log').value='';$('flt-log-status').value='';$('flt-log-src').value='';$('log-from').value='';$('log-to').value='';filterLogs(true);}

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
  // 失败提示必须滚到眼前：保存按钮在表单中部，提示条挂在区块顶部，
  // 不滚动的话用户只会觉得"点了没反应"（2026-11-16 实测反馈）
  if(!ok && m.scrollIntoView) m.scrollIntoView({behavior:'smooth',block:'center'});
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
  $('llm-tdtkey').value='';
  $('llm-tdtkey').placeholder=s.has_tdt_key
    ?('已保存 '+s.tdt_masked+'（留空则不修改）'):'天地图 Key（选填）';
  $('llm-amapkey').value='';
  $('llm-amapkey').placeholder=s.has_amap_key
    ?('已保存 '+s.amap_masked+'（留空则不修改）'):'高德 Key（选填）';
  $('llm-amapsecurity').value='';
  $('llm-amapsecurity').placeholder=s.has_amap_security
    ?('已保存 '+s.amapsec_masked+'（留空则不修改）'):'高德安全密钥（选填）';
  $('llm-amapwebkey').value='';
  $('llm-amapwebkey').placeholder=s.has_amap_web_key
    ?('已保存 '+s.amapweb_masked+'（留空则不修改）'):'高德 Web 服务 Key（选填，景点数据源）';
  $('llm-fb-enabled').checked=!!s.fallback_enabled;
  $('llm-fb-provider').value=s.fallback_provider||'fallback';
  $('llm-fb-base').value=s.fallback_base_url||'';
  $('llm-fb-model').value=s.fallback_model||'';
  $('llm-fb-key').value='';
  $('llm-fb-key').placeholder=s.has_fallback_key
    ?('已保存 '+s.fallback_masked+'（留空则不修改）'):'备用 API Key';
  $('llm-light-enabled').checked=!!s.light_enabled;
  $('llm-light-provider').value=s.light_provider||'light';
  $('llm-light-base').value=s.light_base_url||'';
  $('llm-light-model').value=s.light_model||'';
  $('llm-light-tasks').value=s.light_tasks||'';
  $('llm-light-key').value='';
  $('llm-light-key').placeholder=s.has_light_key
    ?('已保存 '+s.light_masked+'（留空则不修改）'):'轻任务 API Key';
  $('llm-state').textContent=(s.chat&&s.chat.online)
    ?(s.chat.has_detail?'对话服务在线 · 实时读取':'对话服务在线 · 旧版本，按配置显示')
    :'对话服务未探测到 · 显示配置值';
  renderActive(s);
}

async function saveLlm(){
  let r;
  try {
    r = await api('/api/llm/settings',{enabled:$('llm-enabled').checked,
      provider:$('llm-provider').value, base_url:$('llm-base').value.trim(),
      model:$('llm-model').value.trim(), timeout:Number($('llm-timeout').value)||60,
      api_key:$('llm-key').value.trim(),
      tdt_key:$('llm-tdtkey').value.trim(),
      amap_key:$('llm-amapkey').value.trim(),
      amap_security:$('llm-amapsecurity').value.trim(),
      amap_web_key:$('llm-amapwebkey').value.trim(),
      fallback_enabled:$('llm-fb-enabled').checked,
      fallback_provider:$('llm-fb-provider').value,
      fallback_base_url:$('llm-fb-base').value.trim(),
      fallback_model:$('llm-fb-model').value.trim(),
      fallback_api_key:$('llm-fb-key').value.trim(),
      light_enabled:$('llm-light-enabled').checked,
      light_provider:$('llm-light-provider').value,
      light_base_url:$('llm-light-base').value.trim(),
      light_model:$('llm-light-model').value.trim(),
      light_api_key:$('llm-light-key').value.trim(),
      light_tasks:$('llm-light-tasks').value.trim()});
  } catch(e) {
    showLlm(false,'保存请求失败：'+e.message+'（请确认管理后台进程还在运行）');
    return;
  }
  showLlm(!!r.ok, r.ok
    ? (r.warning ? (r.warning+' 刷新后字段会显示「已保存」占位。')
                 : '已保存 —— 对话服务下一句话即刻生效')
    : ('保存失败：'+(r.error||'未知错误')));
  loadLlm();
}

async function testLlm(){
  showLlm(true,'正在请求模型，最长等待 20 秒…');
  const r=await api('/api/llm/test',{api_key:$('llm-key').value.trim(),
    base_url:$('llm-base').value.trim(), model:$('llm-model').value.trim()});
  showLlm(r.ok, r.ok?('连通正常：'+r.model+' · '+r.latency_ms+'ms · 回复「'+r.reply+'」')
    :('连接失败：'+(r.error||'未知错误')));
}

// ---------------- 用户管理：条件搜索 ----------------
let USERS=[];
let USER_ROWS=[];    // 当前过滤结果
let USAGE=[];
let USAGE_ROWS=[];   // 当前过滤结果

async function loadUsers(){
  const r=await api('/api/users');
  if(!r.ok)return;
  USERS=r.users||[];
  $('user-fresh').textContent='共 '+r.total+' 人';
  filterUsers();
}

function filterUsers(reset){
  if(reset)PAGES.user=1;
  const q=$('q-user').value.trim();
  const st=$('flt-status').value;
  const from=$('user-from').value, to=$('user-to').value;
  USER_ROWS=USERS.filter(function(u){
    if(st && u.status!==st)return false;
    const d=dayOf(u.created_at);
    if(from && (!d || d<from))return false;
    if(to && (!d || d>to))return false;
    if(!q)return true;
    return String(u.id).indexOf(q)>=0 || (u.phone||'').indexOf(q)>=0;
  });
  const sort=$('sort-user').value;
  if(sort==='used')USER_ROWS.sort(function(a,b){return (b.used||0)-(a.used||0);});
  else if(sort==='new')USER_ROWS.sort(function(a,b){
    return String(b.created_at||'').localeCompare(String(a.created_at||''));});
  else USER_ROWS.sort(function(a,b){return a.id-b.id;});
  const sumTok=USER_ROWS.reduce(function(a,u){return a+(u.used||0);},0);
  $('user-count').textContent='命中 '+USER_ROWS.length+' / '+USERS.length+' 人'
    +' · 累计消耗合计 '+sumTok;
  renderUsers();
}

function renderUsers(){
  const size=getPageSize('user');
  const total=USER_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.user||1; if(page>pages)page=pages; PAGES.user=page;
  const slice=USER_ROWS.slice((page-1)*size,(page-1)*size+size);
  const body=slice.map(function(u){
    let h='<tr><td>'+u.id+'</td><td>'+esc(u.phone)+'</td>'
      +'<td>'+(u.used||0)+'</td>'
      +'<td>'+esc(u.created_at||'')+'</td><td>'+esc(u.status||'')+'</td>'
      +'<td><button onclick="showUsage('+u.id+')">用量</button></td></tr>';
    return h;
  }).join('');
  $('user-table').innerHTML='<tr><th>ID</th><th>手机号</th><th>累计消耗</th>'
    +'<th>注册时间</th><th>状态</th><th>操作</th></tr>'
    +(body||'<tr><td colspan="6" class="empty">没有符合条件的用户</td></tr>');
  $('user-pager').innerHTML=pagerHtml('user',page,pages,total);
}

function resetUserFilter(){
  $('q-user').value=''; $('flt-status').value='';
  $('user-from').value=''; $('user-to').value='';
  $('sort-user').value='id';
  filterUsers(true);
}

function showUsage(uid){
  $('q-usage').value=uid;
  loadUsage(uid);
}

async function loadUsage(uid){
  const r=await api('/api/usage?limit=500'+(uid?('&uid='+uid):''));
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
  const tk=$('q-usage-task').value.trim().toLowerCase();
  const from=$('usage-from').value, to=$('usage-to').value;
  USAGE_ROWS=USAGE.filter(function(u){
    if(uid && String(u.user_id).indexOf(uid)<0)return false;
    if(m && (u.model||'').toLowerCase().indexOf(m)<0)return false;
    if(tk && (u.task||'').toLowerCase().indexOf(tk)<0)return false;
    const d=dayOf(u.created_at);
    if(from && (!d || d<from))return false;
    if(to && (!d || d>to))return false;
    return true;
  });
  const sum=USAGE_ROWS.reduce(function(a,u){
    return a+((u.prompt_tokens||0)+(u.completion_tokens||0));},0);
  $('usage-count').textContent='命中 '+USAGE_ROWS.length+' / '+USAGE.length
    +' 条 · token 合计 '+sum;
  renderUsage();
}

function renderUsage(){
  const size=getPageSize('usage');
  const total=USAGE_ROWS.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.usage||1; if(page>pages)page=pages; PAGES.usage=page;
  const slice=USAGE_ROWS.slice((page-1)*size,(page-1)*size+size);
  $('usage-table').innerHTML='<tr><th>时间</th><th>用户</th><th>模型</th><th>任务</th>'
    +'<th>输入tok</th><th>输出tok</th></tr>'
    +(slice.length?slice.map(function(u){
      return '<tr><td>'+esc(u.created_at)+'</td><td>'+u.user_id
        +'</td><td>'+esc(u.model||'')+'</td><td>'+esc(u.task||'')+'</td>'
        +'<td>'+(u.prompt_tokens??'-')+'</td><td>'+(u.completion_tokens??'-')+'</td></tr>';
    }).join(''):'<tr><td colspan="6" class="empty">没有符合条件的流水</td></tr>');
  $('usage-pager').innerHTML=pagerHtml('usage',page,pages,total);
}

function resetUsageFilter(){
  $('q-usage').value=''; $('q-usage-model').value=''; $('q-usage-task').value='';
  $('usage-from').value=''; $('usage-to').value='';
  filterUsage(true);
}

// ---------------- 内容审计 ----------------
let MODERATION=[];

async function loadModeration(){
  const r=await api('/api/moderation?limit=500');
  MODERATION=r.items||[];
  PAGES.mod=1;
  renderModeration();
}

function modRows(){
  const dir=$('flt-mod-dir').value, cat=$('flt-mod-cat').value;
  const disp=$('flt-mod-disp').value;
  const uid=$('q-mod-uid').value.trim();
  const kw=$('q-mod-kw').value.trim().toLowerCase();
  const from=$('mod-from').value, to=$('mod-to').value;
  return MODERATION.filter(function(m){
    if(dir && m.direction!==dir)return false;
    if(cat && m.category!==cat)return false;
    if(disp && m.disposition!==disp)return false;
    if(uid && String(m.uid==null?'':m.uid).indexOf(uid)<0)return false;
    const d=dayOf(m.created_at);
    if(from && (!d || d<from))return false;
    if(to && (!d || d>to))return false;
    if(!kw)return true;
    return ((m.words||'')+' '+(m.excerpt||'')+' '+(m.category||''))
      .toLowerCase().indexOf(kw)>=0;
  });
}

function renderModeration(){
  const rows=modRows();
  $('mod-count').textContent='命中 '+rows.length+' / '+MODERATION.length+' 条';
  const size=getPageSize('mod');
  const total=rows.length;
  const pages=Math.max(1,Math.ceil(total/size));
  let page=PAGES.mod||1; if(page>pages)page=pages; PAGES.mod=page;
  const slice=rows.slice((page-1)*size,(page-1)*size+size);
  const dirName={input:'输入',output:'输出'};
  const dispName={blocked:'已拒绝',masked:'已打码',fallback:'已换兜底'};
  $('mod-table').innerHTML='<tr><th>时间</th><th>方向</th><th>类别</th><th>命中词</th>'
    +'<th>内容摘录</th><th>用户</th><th>处置</th><th></th></tr>'
    +(slice.length?slice.map(function(m){
      return '<tr><td>'+esc(m.created_at)+'</td>'
        +'<td>'+(dirName[m.direction]||m.direction)+'</td>'
        +'<td><b>'+esc(m.category)+'</b></td>'
        +'<td>'+esc((m.words||'').replace(/^"|"$/g,''))+'</td>'
        +'<td style="max-width:260px;word-break:break-all;">'+esc(m.excerpt||'')+'</td>'
        +'<td>'+(m.uid??'-')+'</td>'
        +'<td>'+(dispName[m.disposition]||esc(m.disposition))+'</td>'
        +'<td><button onclick="dismissHit('+m.id+')">误报放行</button></td></tr>';
    }).join(''):'<tr><td colspan="8" class="empty">没有符合条件的拦截记录</td></tr>');
  $('mod-pager').innerHTML=pagerHtml('mod',page,pages,total);
}

function resetModFilter(){
  $('flt-mod-dir').value=''; $('flt-mod-cat').value=''; $('flt-mod-disp').value='';
  $('q-mod-uid').value=''; $('q-mod-kw').value='';
  $('mod-from').value=''; $('mod-to').value='';
  PAGES.mod=1;
  renderModeration();
}

async function dismissHit(id){
  const r=await api('/api/moderation/dismiss',{id:id});
  const m=$('mod-msg');
  if(r.ok){ m.className='msg ok'; m.textContent='已标记误报（记录保留）'; }
  else{ m.className='msg bad'; m.textContent=r.error||'操作失败'; }
  loadModeration();
}

// ---------------- 左侧导航 / 视图切换 ----------------
const VIEWS=['overview','data','users','moderation','model'];

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
  else if(v==='moderation') loadModeration();
  else if(v==='model') loadLlm();
  window.scrollTo({top:0});
}

function route(){
  const v=(location.hash||'').replace(/^#[/]?/,'');
  showView(VIEWS.indexOf(v)>=0?v:'overview');
}

window.addEventListener('hashchange',route);
route();
