// 管理后台条件查询逻辑的单元验证：从真实页面 JS 里抽出筛选函数，用桩 DOM 跑断言
const fs = require('fs');
const pagePath = process.argv[2] || '.tmp/admin_page.js';
const src = fs.readFileSync(pagePath, 'utf8');

// 按函数名截取函数体（大括号配平）
function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('missing ' + name);
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error('unbalanced ' + name);
}

// ---- 桩 DOM ----
const nodes = {};
function el(id) {
  if (!nodes[id]) {
    nodes[id] = { value: '', textContent: '', innerHTML: '', style: {},
      classList: { toggle() {} }, scrollIntoView() {}, focus() {} };
  }
  return nodes[id];
}
global.$ = el;
global.PAGES = { fin: 1, ord: 1, user: 1, usage: 1, log: 1, mod: 1, fresh: 1 };
global.esc = s => String(s);
global.escq = s => String(s);
global.getPageSize = () => 100;
global.pagerHtml = () => '';
global.renderUsers = () => {};
global.renderUsage = () => {};
global.renderLogs = () => {};
global.renderModeration = () => {};
global.renderFresh = () => {};

// dayOf 与页面里同名函数保持一致
function dayOf(v) { var t = String(v == null ? '' : v); return t.slice(0, 10); }
global.dayOf = dayOf;

eval(grab('filterUsers') + grab('filterUsage') +
     grab('filterLogs') + grab('modRows') + grab('filterFresh'));

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('[PASS] ' + name); }
  else { fail++; console.log('[FAIL] ' + name + ' ' + (detail === undefined ? '' : detail)); }
}
const E = el;

// ---- 用户：状态 + 注册日期 + 排序（余额/档位筛选已随计费层移除） ----
global.USERS = [
  { id: 1, phone: '138****0001', used: 120, status: 'active', created_at: '2026-11-01' },
  { id: 2, phone: '138****0002', used: 3000, status: 'active', created_at: '2026-11-10' },
  { id: 3, phone: '139****0003', used: 0, status: 'disabled', created_at: '2026-11-16' }
];
['q-user', 'flt-status', 'user-from', 'user-to'].forEach(k => E(k).value = '');
E('sort-user').value = 'id';
filterUsers(true);
check('用户无条件返回全部', USER_ROWS.length === 3);
E('flt-status').value = 'disabled'; filterUsers(true);
check('用户按状态筛选', USER_ROWS.length === 1 && USER_ROWS[0].id === 3);
E('flt-status').value = '';
E('user-from').value = '2026-11-10'; filterUsers(true);
check('用户注册日期起过滤', USER_ROWS.length === 2, USER_ROWS.length);
E('user-to').value = '2026-11-10'; filterUsers(true);  // from 仍为 2026-11-10，区间内只剩 1 条
check('用户注册日期段过滤', USER_ROWS.length === 1 && USER_ROWS[0].id === 2);
E('user-from').value = ''; E('user-to').value = '';
E('sort-user').value = 'used'; filterUsers(true);
check('用户按累计消耗降序', USER_ROWS[0].id === 2);
E('sort-user').value = 'new'; filterUsers(true);
check('用户按注册时间降序', USER_ROWS[0].id === 3);
q = '0002'; USER_ROWS = [];
E('q-user').value = q; filterUsers(true);
check('用户按手机号尾号搜索', USER_ROWS.length === 1 && USER_ROWS[0].id === 2);
E('q-user').value = '';

// ---- 用量：任务 + 日期（免费站只记 token，无积分维度） ----
global.USAGE = [
  { user_id: 1, model: 'deepseek-chat', task: 'rerank', prompt_tokens: 120, completion_tokens: 45, created_at: '2026-11-16 10:00:00' },
  { user_id: 1, model: 'deepseek-chat', task: 'extract_slots', prompt_tokens: 80, completion_tokens: 30, created_at: '2026-11-15 09:00:00' },
  { user_id: 2, model: 'kimi', task: 'plan', prompt_tokens: 900, completion_tokens: 500, created_at: '2026-11-10 08:00:00' }
];
['q-usage', 'q-usage-model', 'q-usage-task', 'usage-from', 'usage-to']
  .forEach(k => E(k).value = '');
filterUsage(true);
check('用量无条件返回全部', USAGE_ROWS.length === 3);
E('q-usage-task').value = 're'; filterUsage(true);
check('用量按任务关键词筛选', USAGE_ROWS.length === 1 && USAGE_ROWS[0].task === 'rerank');
E('q-usage-task').value = ''; E('usage-from').value = '2026-11-15'; filterUsage(true);
check('用量日期起过滤（带时分时间戳可比）', USAGE_ROWS.length === 2, USAGE_ROWS.length);
E('usage-to').value = '2026-11-15'; filterUsage(true);
check('用量日期段过滤', USAGE_ROWS.length === 1 && USAGE_ROWS[0].task === 'extract_slots');

// ---- 日志：来源 + 关键字 + 时间 ----
global.LOGS = [
  { ts: '2026-11-16 09:00:00', city: '北京', source: 'overpass', status: 'failed', added: 0, message: '超时' },
  { ts: '2026-11-15 09:00:00', city: '大理', source: 'overpass', status: 'success', added: 12, message: 'ok' },
  { ts: '2026-11-10 09:00:00', city: '北京', source: 'llm', status: 'success', added: 8, message: '模型补' }
];
['q-log', 'flt-log-status', 'flt-log-src', 'log-from', 'log-to'].forEach(k => E(k).value = '');
filterLogs(true);
check('日志无条件返回全部', LOG_ROWS.length === 3);
E('flt-log-src').value = 'overpass'; filterLogs(true);
check('日志按来源筛选', LOG_ROWS.length === 2);
E('flt-log-src').value = ''; E('flt-log-status').value = 'failed'; filterLogs(true);
check('日志按状态筛选', LOG_ROWS.length === 1 && LOG_ROWS[0].city === '北京');
E('flt-log-status').value = ''; E('log-from').value = '2026-11-15'; filterLogs(true);
check('日志按时间起筛选', LOG_ROWS.length === 2, LOG_ROWS.length);
E('q-log').value = '北京'; filterLogs(true);
check('日志来源+关键词+时间组合', LOG_ROWS.length === 1 && LOG_ROWS[0].source === 'overpass');

// ---- 内容审计：处置 + 用户 + 关键词 + 日期 ----
global.MODERATION = [
  { id: 1, created_at: '2026-11-16 10:00:00', direction: 'input', category: 'gambling',
    words: '赌博', excerpt: '说个赌博的', uid: 1, disposition: 'blocked' },
  { id: 2, created_at: '2026-11-15 10:00:00', direction: 'output', category: 'abuse',
    words: '傻逼', excerpt: '回复带脏话', uid: 2, disposition: 'masked' },
  { id: 3, created_at: '2026-11-10 10:00:00', direction: 'input', category: 'abuse',
    words: '傻逼', excerpt: '用户骂人', uid: 2, disposition: 'blocked' }
];
['flt-mod-dir', 'flt-mod-cat', 'flt-mod-disp', 'q-mod-uid', 'q-mod-kw', 'mod-from', 'mod-to']
  .forEach(k => E(k).value = '');
check('内容审计无条件返回全部', modRows().length === 3);
E('flt-mod-disp').value = 'masked';
check('内容审计按处置筛选', modRows().length === 1 && modRows()[0].id === 2);
E('flt-mod-disp').value = ''; E('q-mod-uid').value = '2';
check('内容审计按用户筛选', modRows().length === 2);
E('q-mod-uid').value = ''; E('q-mod-kw').value = '赌博';
check('内容审计按命中词搜索', modRows().length === 1 && modRows()[0].id === 1);
E('q-mod-kw').value = ''; E('mod-from').value = '2026-11-15';
check('内容审计按日期起筛选', modRows().length === 2, modRows().length);
E('flt-mod-cat').value = 'abuse'; E('mod-to').value = '2026-11-15';
check('内容审计类别+日期段组合', modRows().length === 1 && modRows()[0].id === 2);

// ---- 新鲜度：城市 + 来源 + POI 量 + 更新日期 ----
global.FRESH_ALL = [
  { city: '大理', pois: 120, sources: 'overpass,seed', last_update: '2026-11-16' },
  { city: '北京', pois: 0, sources: '', last_update: '' },
  { city: '丽江', pois: 30, sources: 'seed', last_update: '2026-11-01' }
];
['q-fresh', 'flt-fresh-src', 'flt-fresh-poi', 'fresh-from', 'fresh-to'].forEach(k => E(k).value = '');
filterFresh(true);
check('新鲜度无条件返回全部', FRESH_ROWS.length === 3);
E('q-fresh').value = '大'; filterFresh(true);
check('新鲜度按城市关键词', FRESH_ROWS.length === 1 && FRESH_ROWS[0].city === '大理');
E('q-fresh').value = ''; E('flt-fresh-poi').value = 'zero'; filterFresh(true);
check('新鲜度筛「无 POI 待补」', FRESH_ROWS.length === 1 && FRESH_ROWS[0].city === '北京');
E('flt-fresh-poi').value = 'lt50'; filterFresh(true);
check('新鲜度筛少于 50 条', FRESH_ROWS.length === 1 && FRESH_ROWS[0].city === '丽江');
E('flt-fresh-poi').value = 'ge50'; filterFresh(true);
check('新鲜度筛 50 条以上', FRESH_ROWS.length === 1 && FRESH_ROWS[0].city === '大理');
E('flt-fresh-poi').value = ''; E('flt-fresh-src').value = 'seed'; filterFresh(true);
check('新鲜度按来源筛选（多来源字段包含匹配）', FRESH_ROWS.length === 2, FRESH_ROWS.length);
E('flt-fresh-src').value = ''; E('fresh-from').value = '2026-11-10'; filterFresh(true);
check('新鲜度按更新时间筛选（空时间不通过日期条件）',
      FRESH_ROWS.length === 1 && FRESH_ROWS[0].city === '大理', FRESH_ROWS.length);

console.log('\n' + pass + '/' + (pass + fail) + ' 条条件查询逻辑断言通过');
process.exit(fail ? 1 : 0);
