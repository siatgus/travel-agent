// 冒烟：在 node 中执行求值后的管理页 JS，验证分页组件输出
const fs = require('fs');
const { execSync } = require('child_process');
const py = 'C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe';
// 用 Python 求值 admin.PAGE 并导出 <script> 内容到临时文件
execSync(py + ' -c "import sys; sys.path.insert(0,\'src\'); '
  + 'from travel_agent import admin; import re; '
  + 'js = re.search(r\'<script>(.*?)</script>\', admin.PAGE, re.S).group(1); '
  + 'open(\'data/_page_script.js\',\'w\',encoding=\'utf-8\').write(js)"',
  { cwd: __dirname + '/..', stdio: 'inherit' });

// 浏览器桩
global.window = { addEventListener(){}, scrollTo(){} };
global.location = { hash: '' };
global.document = { getElementById(){ return null; }, querySelectorAll(){ return []; } };
global.localStorage = { getItem(){ return null; }, setItem(){} };

const js = fs.readFileSync(__dirname + '/../data/_page_script.js', 'utf8');
eval(js);   // 定义 PAGE_SIZES / PAGES / pagerHtml / gotoPage 等

const h = pagerHtml('user', 3, 12, 234);
console.log('--- pagerHtml 输出（前 190 字符）---');
console.log(h.slice(0, 190));
const okCur = h.includes('class="pg-cur" onclick="gotoPage(\'user\',3)">3</button>');
const okJump = h.includes("onkeydown=\"if(event.key==='Enter')jumpPage('user',this.value)\"");
const okSize = h.includes('<option value="50">50 行/页</option>');
const okInfo = h.includes('第 3 / 12 页 · 共 234 条');
console.log('当前页高亮 pg-cur:', okCur);
console.log('回车跳页:', okJump);
console.log('每页行数选项:', okSize);
console.log('页码信息文案:', okInfo);
PAGES.user = 5; gotoPage('user', 6);
console.log('gotoPage 分发后 PAGES.user =', PAGES.user);
console.log(okCur && okJump && okSize && okInfo ? 'SMOKE PASS' : 'SMOKE FAIL');
