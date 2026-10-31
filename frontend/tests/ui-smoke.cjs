/**
 * 前端联调冒烟测试（jsdom + 假 fetch，不需要真后端）。
 *
 * 验证的是"页面如何消费接口"，不是接口本身：
 *  - 初始化是否读 /api/meta 的 default_targets 填 chips
 *  - 自动检测是否把 POST /api/detect 的响应渲染成卡片/图表/列表/banner
 *  - 422 / 503 / 网络不可达 三种失败是否各有正确提示
 *  - 前端版本号校验是否在发请求之前拦住
 *
 * 真后端的端到端验证见 tests/e2e-live.cjs。
 * 运行：node frontend/tests/ui-smoke.cjs
 */

const path = require('path');
const fs = require('fs');

const { JSDOM } = require(path.join(__dirname, '..', '..', 'backend', 'node', 'node_modules', 'jsdom'));

const HTML_PATH = path.join(__dirname, '..', 'index.html');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* --------------------------------- 假数据 --------------------------------- */

const META = {
  browsers: ['chrome', 'safari', 'firefox', 'edge'],
  default_targets: ['chrome 90', 'safari 14.1', 'firefox 88', 'edge 90'],
  max_targets: 16,
  max_source_chars: 200000,
  bcd_index_ready: true,
  bcd_version: '5.6.11',
  bcd_feature_count: 13196,
  releases: { chrome: ['120', '90'], safari: ['16.0', '14.1'], firefox: ['115', '88'], edge: ['120', '90'] },
  llm: { model: 'test-model', base_url: 'https://api.example.com', key_configured: true,
         timeout_seconds: 60, max_retries: 2, max_output_tokens: 2000, context_lines: 20 },
  l3: { jsdom_timeout_seconds: 5, node_available: true, runner_available: true },
  features: { max_source_chars: 200000, max_issues: 500 },
};

const DETECT = {
  status: 'partial',
  issues: [
    { id: 'issue_1', type: 'css', feature: 'accent-color', bcd_path: 'css.properties.accent-color', line: 5, column: 3,
      unsupported_browsers: ['chrome 90', 'safari 14.1', 'firefox 88', 'edge 90'], severity: 'high', confidence: 'certain',
      source_data: 'MDN BCD: requires Chrome 93; requires Safari 15.4; requires Firefox 92; requires Edge 93 (css.properties.accent-color)' },
    { id: 'issue_2', type: 'css', feature: 'container-type', bcd_path: 'css.properties.container-type', line: 6, column: 3,
      unsupported_browsers: ['chrome 90', 'safari 14.1', 'firefox 88', 'edge 90'], severity: 'high', confidence: 'certain',
      source_data: 'MDN BCD: requires Chrome 105; requires Safari 16; requires Firefox 110 (css.properties.container-type)' },
    { id: 'issue_3', type: 'css', feature: 'aspect-ratio', bcd_path: 'css.properties.aspect-ratio', line: 4, column: 3,
      unsupported_browsers: ['safari 14.1', 'firefox 88'], severity: 'high', confidence: 'certain',
      source_data: 'MDN BCD: requires Safari 15; requires Firefox 89 (css.properties.aspect-ratio)' },
    { id: 'issue_4', type: 'css', feature: 'backdrop-filter', bcd_path: 'css.properties.backdrop-filter', line: 7, column: 3,
      unsupported_browsers: ['safari 14.1', 'firefox 88'], severity: 'high', confidence: 'certain',
      source_data: 'MDN BCD: requires Safari 18; requires Firefox 103 (css.properties.backdrop-filter)' },
    { id: 'issue_5', type: 'css', feature: 'scroll-behavior', bcd_path: 'css.properties.scroll-behavior', line: 9, column: 3,
      unsupported_browsers: ['safari 14.1'], severity: 'medium', confidence: 'certain',
      source_data: 'MDN BCD: requires Safari 15.4 (css.properties.scroll-behavior)' },
    { id: 'issue_6', type: 'css', feature: 'color-interpolation', bcd_path: 'css.properties.color-interpolation', line: 8, column: 3,
      unsupported_browsers: ['chrome 90', 'safari 14.1', 'edge 90'], severity: 'low', confidence: 'context_unknown',
      source_data: 'MDN BCD: Chrome 90 partial implementation; Safari 14.1 partial implementation (css.properties.color-interpolation)' },
  ],
  unknown_features: ['future-prop'],
  targets_resolved: [
    { browser: 'chrome', version: '90', label: 'chrome 90' },
    { browser: 'safari', version: '14.1', label: 'safari 14.1' },
    { browser: 'firefox', version: '88', label: 'firefox 88' },
    { browser: 'edge', version: '90', label: 'edge 90' },
  ],
  parse_warnings: [{ kind: 'js', message: '第 2 行存在未闭合的括号，已跳过该片段', line: 2 }],
  stats: { css_lines: 10, js_lines: 2, html_lines: 3, total_lines: 15, declarations_scanned: 8, features_scanned: 8, issues_total: 6 },
};

/* -------------------------------- 测试脚手架 ------------------------------- */

const failures = [];
function check(name, condition, detail = '') {
  if (condition) console.log(`  ✓ ${name}`);
  else {
    failures.push(`${name}${detail ? ` — ${detail}` : ''}`);
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

function makeFetcher(state) {
  return async (url, options = {}) => {
    state.calls.push({ url: String(url), method: options.method || 'GET', body: options.body });
    const pathname = new URL(String(url), 'http://localhost').pathname;
    const route = state.routes[`${options.method || 'GET'} ${pathname}`];
    if (!route) throw new TypeError(`未配置的假路由: ${options.method || 'GET'} ${pathname}`);
    if (route instanceof Error) throw route;
    const { status = 200, body } = route;
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: String(status),
      json: async () => body,
    };
  };
}

/**
 * 把真实页面装配成可跑的测试页：
 * 去掉 CDN <script src>（jsdom 不加载外部资源）与 tailwind.config 内联块，
 * 再把 app.js 的内容以内联方式注入，保证测的就是线上那份代码。
 */
function buildPage(html) {
  let out = html.replace(/<script\b[^>]*\bsrc=[^>]*><\/script>/gi, '');
  out = out.replace(/<script>([\s\S]*?)<\/script>/gi, (match, body) => (
    body.includes('tailwind.config') ? '' : match
  ));
  const appSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  return out.replace('</body>', `<script>\n${appSource}\n</script>\n</body>`);
}

(async () => {
  console.log('前端联调冒烟测试 · frontend/app.js');
  const html = buildPage(fs.readFileSync(HTML_PATH, 'utf8'));

  const net = {
    calls: [],
    routes: {
      'GET /api/meta': { body: META },
      'POST /api/detect': { body: DETECT },
    },
  };

  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    resources: undefined,          // 不加载外部 CSS/JS（CDN 由 jsdom 跳过）
    pretendToBeVisual: true,
    url: 'http://127.0.0.1:8000/',
    beforeParse(window) {
      window.fetch = makeFetcher(net);
    },
  });

  const { window } = dom;
  const doc = window.document;
  const $ = (selector) => doc.querySelector(selector);
  const $$ = (selector) => Array.from(doc.querySelectorAll(selector));
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  await sleep(120); // 等 app.js 的 bootstrap（meta → 自动检测）跑完

  console.log('\n[1] 初始化：读 /api/meta 填 chips');
  check('代码区预填示例 CSS', $('#code').value.includes('accent-color'), `实际「${$('#code').value.slice(0, 20)}」`);
  check('chips = 4（来自 default_targets）', $$('.chip').length === 4, `实际 ${$$('.chip').length}`);
  check('第一个 chip 是 chrome 90',
    $$('.chip select')[0]?.value === 'chrome' && $$('.chip input')[0]?.value === '90');
  check('safari 版本是 14.1', $$('.chip input')[1]?.value === '14.1');
  check('datalist 已按浏览器生成', $$('#datalists datalist').length === 4);
  check('datalist 选项来自 releases', $$('#datalists datalist')[0].querySelectorAll('option').length === 2);

  console.log('\n[2] 自动检测：渲染 POST /api/detect 的响应');
  check('检测请求发过一次', net.calls.filter((c) => c.url.includes('/api/detect')).length === 1);
  check('请求体带 targets', JSON.parse(net.calls.find((c) => c.url.includes('/api/detect')).body).targets.length === 4);
  check('问题行 = 6', $$('.issue-row').length === 6, `实际 ${$$('.issue-row').length}`);
  check('代码行数卡 = 15', $('#stat-lines').textContent === '15');
  check('问题数卡 = 6', $('#stat-issues').textContent === '6');
  check('等级分布 high 4 / medium 1 / low 1',
    $('#stat-issues-sub').textContent.includes('high 4')
    && $('#stat-issues-sub').textContent.includes('medium 1')
    && $('#stat-issues-sub').textContent.includes('low 1'));
  check('列表按严重程度排序（首行为 high）', $$('.issue-row')[0].querySelector('.pill-high') !== null);
  check('confidence 非 certain 时加标签',
    $$('.issue-row').some((row) => row.textContent.includes('上下文未知')));
  check('parse_warnings 进入顶部 banner',
    !$('#banner').classList.contains('hidden') && $('#banner').textContent.includes('partial'));
  check('banner 带出警告原文与行号', $('#banner').textContent.includes('未闭合') && $('#banner').textContent.includes('L2'));
  check('unknown_features 展示出来', $('#unknown-features').textContent.includes('future-prop'));
  check('donut 图例显示 4/1/1',
    $$('#donut-legend > span').map((s) => s.textContent.replace(/\s+/g, '')).join('|').includes('high4'));
  check('list-status 显示条数', $('#list-status').textContent.includes('共 6 条'));
  check('一键修复与开始检测在同一张卡里',
    $('#fix-all').closest('section') === $('#detect').closest('section'));
  check('一键修复不在 Dashboard 页',
    !$('[data-page="dashboard"]').contains($('#fix-all')));
  check('修复面板紧邻代码区（在问题列表之前）',
    $('#fix-panel').compareDocumentPosition($('#issue-list')) & 4);
  check('侧边栏特性数来自接口', $('#engine-note').textContent.includes('13,196'),
    $('#engine-note').textContent);

  check('首页最近问题只显示 5 条', $$('.top-row').length === 5, String($$('.top-row').length));
  check('最近问题计数 = 6', $('#top-count').textContent === '6');

  console.log('\n[3] chips 增删');
  click($('.chip-remove'));
  check('删除后 = 3', $$('.chip').length === 3);
  click($('#add-target'));
  check('添加后 = 4', $$('.chip').length === 4);

  console.log('\n[4] 标签页与代码区');
  click($('.tab[data-tab="html"]'));
  check('HTML 标签页内容', $('#code').value.includes('<dialog'));
  click($('.tab[data-tab="css"]'));
  check('切回 CSS 内容保留', $('#code').value.includes('accent-color'));
  check('三个 tab 的代码都会提交',
    JSON.parse(net.calls.find((c) => c.url.includes('/api/detect')).body).html.includes('<div'));
  click($('#load-contract'));
  check('载入契约示例', $('#code').value === '.box { display: flex; gap: 10px; }');

  console.log('\n[5] 顶部搜索 = 特性查询（本地 BCD 查表）');
  const search = $('#search');
  net.routes['POST /api/lookup'] = {
    body: {
      status: 'success', query: 'accent-color', found: true, kind: 'css',
      bcd_path: 'css.properties.accent-color', description: '',
      verdicts: [
        { browser: 'chrome', version: '90', label: 'chrome 90', supported: false, partial: false, confidence: 'certain', requirement: '93' },
        { browser: 'safari', version: '14.1', label: 'safari 14.1', supported: true, partial: false, confidence: 'certain', requirement: '15.4' },
      ],
      unsupported_browsers: ['chrome 90'],
      summary: 'MDN BCD: requires Chrome 93 (css.properties.accent-color)',
      alternatives: [],
    },
  };
  search.value = 'accent-color';
  search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await sleep(150);
  check('查询面板展开', !$('#lookup-panel').classList.contains('hidden'));
  check('显示命中的 BCD 路径', $('#lookup-title').textContent.includes('css.properties.accent-color'));
  check('逐目标给出结论', $$('#lookup-body .pill').length === 2,
    String($$('#lookup-body .pill').length));
  check('不支持目标标 ✗', $('#lookup-body').textContent.includes('✗ chrome 90'));

  net.routes['POST /api/lookup'] = {
    body: { status: 'success', query: 'accent-colour', found: false,
            alternatives: ['css.properties.accent-color'], summary: 'BCD 中查不到' },
  };
  search.value = 'accent-colour';
  search.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await sleep(150);
  check('未命中给出相近路径', $('#lookup-body').textContent.includes('accent-color'));
  $('#lookup-body [data-alt]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await sleep(150);
  const lastLookup = JSON.parse(net.calls.filter((c) => c.url.includes('/api/lookup')).pop().body);
  check('点击相近路径会去掉 BCD 前缀再查', lastLookup.feature === 'accent-color', lastLookup.feature);

  $('#lookup-close').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('关闭查询面板', $('#lookup-panel').classList.contains('hidden'));

  console.log('\n[6] 失败路径');
  net.routes['POST /api/detect'] = {
    status: 422,
    body: { detail: [{ loc: ['body', 'targets'], msg: "targets[1] 版本号 'abc' 非法：需为 1~4 段纯数字" }] },
  };
  click($('#detect'));
  await sleep(60);
  check('422 → banner 显示后端错误原文', $('#banner').textContent.includes('版本号'));
  check('422 → 状态栏标记失败', $('#list-status').textContent.includes('HTTP 422'));

  net.routes['POST /api/detect'] = new TypeError('Failed to fetch');
  click($('#detect'));
  await sleep(60);
  check('网络不可达 → banner 提示连接失败', $('#banner').textContent.includes('无法连接后端'));
  check('网络不可达 → 提示用 8000 端口打开', $('#banner').textContent.includes('127.0.0.1:8000'));

  net.routes['POST /api/detect'] = { status: 503, body: { detail: 'BCD 索引不存在：data/bcd_index.json' } };
  click($('#detect'));
  await sleep(60);
  check('503 → banner 给出构建命令', $('#banner').textContent.includes('build_bcd_index.py'));

  console.log('\n[7] 一键修复 + 验证（L2 → L3）');
  // 先固定一段三段式 CSS，便于按行号断言"行区间替换"的合并结果
  $('#code').value = '.a {\n  accent-color: red;\n}\n';
  $('#code').dispatchEvent(new window.Event('input', { bubbles: true }));

  net.routes['POST /api/explain-fix'] = {
    body: {
      status: 'success',
      model: 'test-model',
      degraded: false,
      explanations: [
        { issue_id: 'issue_1', explain_text: 'Safari 12 会忽略该声明，需要 Safari 15.4。',
          fix_diff: '换成 color 声明',
          fix_edits: [{ language: 'css', start_line: 2, end_line: 2, replacement: '  color: red;' }],
          fix_code_css: null, fix_code_js: null, fix_code_html: null, needs_human: false },
        { issue_id: 'issue_2', explain_text: 'Safari 12 解析阶段抛 SyntaxError。',
          fix_diff: '', fix_edits: [],
          fix_code_css: null, fix_code_js: null, fix_code_html: null, needs_human: true },
      ],
    },
  };
  net.routes['POST /api/verify'] = {
    body: {
      status: 'success', before_count: 6, after_count: 5,
      resolved: ['issue_1'], remaining: ['issue_2'], new_issues: [],
      unknown_features: [], needs_review: [],
      dom_diff: { ran: true, changed: true, added_nodes: 1, removed_nodes: 0, errors: [] },
    },
  };

  click($('#fix-all'));
  await sleep(150);
  check('修复面板展开', !$('#fix-panel').classList.contains('hidden'));
  check('摘要显示已解决 1', $('#fix-summary').textContent.includes('已解决 1'));
  check('摘要显示未解决 1', $('#fix-summary').textContent.includes('未解决 1'));
  check('状态标为部分解决', $('#fix-state').textContent.includes('部分解决'));
  check('差异视图出现增删行', /[+−]/.test($('#fix-diff').textContent));
  const mergedBody = JSON.parse(net.calls.filter((c) => c.url.includes('/api/verify')).pop().body);
  check('行级修复已合并进修复后代码',
    mergedBody.fixed.css.includes('color: red') && !mergedBody.fixed.css.includes('accent-color'),
    mergedBody.fixed.css);
  check('摘要显示行级修复条数', $('#fix-summary').textContent.includes('行级修复 1 条'));
  check('解释列表渲染', $('#fix-issues').textContent.includes('Safari 12'));
  check('needs_human 标注', $('#fix-issues').textContent.includes('需人工修复'));

  click($('.fixtab[data-fixtab="fixed"]'));
  check('切到"修复后"面板', !$('#fix-fixed').classList.contains('hidden') && $('#fix-diff').classList.contains('hidden'));
  click($('.fixtab[data-fixtab="diff"]'));
  check('切回"差异"面板', !$('#fix-diff').classList.contains('hidden'));

  const verifyCallsBefore = net.calls.filter((c) => c.url.includes('/api/verify')).length;
  net.routes['POST /api/verify'] = {
    body: {
      status: 'partial', before_count: 6, after_count: 7,
      resolved: ['issue_1'], remaining: [], new_issues: [
        { id: 'issue_9', type: 'css', feature: 'container-type', bcd_path: 'css.properties.container-type',
          line: 1, column: 3, unsupported_browsers: ['chrome 90'], severity: 'high', confidence: 'certain',
          source_data: 'MDN BCD: requires Chrome 105' },
      ],
      dom_diff: { ran: true, changed: false, added_nodes: 0, removed_nodes: 0, errors: ['修复后: boom'] },
    },
  };
  click($('#verify-fix'));
  await sleep(150);
  check('重新验证发了请求', net.calls.filter((c) => c.url.includes('/api/verify')).length === verifyCallsBefore + 1);
  check('引入新问题高亮', $('#fix-state').textContent.includes('引入新问题'));
  check('新问题列出', $('#fix-issues').textContent.includes('container-type'));
  check('执行期错误展示', $('#fix-issues').textContent.includes('boom'));

  // A 方案：resolved 里混着"只是查不到"的条目 → 必须标待复核
  net.routes['POST /api/verify'] = {
    body: {
      status: 'success', before_count: 6, after_count: 0,
      resolved: ['issue_1'], remaining: [], new_issues: [],
      unknown_features: ['<div open>'], needs_review: ['issue_1'],
      dom_diff: { ran: true, changed: false, added_nodes: 0, removed_nodes: 0, errors: [] },
    },
  };
  click($('#verify-fix'));
  await sleep(150);
  check('待复核状态高亮', $('#fix-state').textContent.includes('需人工复核'), $('#fix-state').textContent);
  check('摘要显示待复核 1', $('#fix-summary').textContent.includes('待复核 1'));
  check('列出未识别特性',
    $('#fix-issues').textContent.includes('<div open>'), $('#fix-issues').textContent.slice(0, 120));

  net.routes['POST /api/verify'] = {
    body: {
      status: 'success', before_count: 6, after_count: 0,
      resolved: ['issue_1'], remaining: [], new_issues: [],
      unknown_features: [], needs_review: [],
      dom_diff: { ran: true, changed: false, added_nodes: 0, removed_nodes: 0, errors: [] },
    },
  };
  click($('#use-sample-fix'));
  await sleep(150);
  const sampleBody = JSON.parse(net.calls.filter((c) => c.url.includes('/api/verify')).pop().body);
  check('示例修复带入了 margin 方案', sampleBody.fixed.css.includes('margin: -6px'));

  console.log('\n[8] 前端校验（发请求之前拦住）');
  const callsBefore = net.calls.length;
  while ($('.chip-remove')) click($('.chip-remove'));
  check('chips 已清空', $$('.chip').length === 0);
  net.routes['POST /api/detect'] = { body: DETECT };
  click($('#detect'));
  await sleep(60);
  check('无目标 → banner 提示', $('#banner').textContent.includes('至少需要一个目标浏览器'));
  check('无目标 → 未发请求', net.calls.length === callsBefore);

  console.log('\n[9] 导航路由与设置页');
  check('初始在 Dashboard',
    !$('[data-page="dashboard"]').classList.contains('hidden')
    && $('[data-page="analysis"]').classList.contains('hidden'));
  window.location.hash = '#/dashboard';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  $('#goto-analysis-card').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  check('渐变卡按钮直接跳到分析页', !$('[data-page="analysis"]').classList.contains('hidden'));
  check('页面标题同步', $('#page-title').textContent.includes('代码分析'), $('#page-title').textContent);
  check('导航激活态跟随', $('a[data-route="analysis"]').getAttribute('aria-current') === 'page');

  window.location.hash = '#/settings';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  check('切到 Settings', !$('[data-page="settings"]').classList.contains('hidden'));
  check('设置页展示模型名', $('#settings-body').textContent.includes('test-model'));
  check('设置页展示 BCD 版本', $('#settings-body').textContent.includes('5.6.11'));
  check('设置页展示 key 状态', $('#settings-body').textContent.includes('已配置'));
  check('设置页列出浏览器候选', $('#settings-releases').textContent.includes('chrome'));

  window.location.hash = '#/dashboard';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  check('切回 Dashboard', !$('[data-page="dashboard"]').classList.contains('hidden'));

  console.log(`\n结果：${failures.length === 0 ? '全部通过' : `${failures.length} 项失败`}`);
  failures.forEach((item) => console.log(`  - ${item}`));
  process.exit(failures.length === 0 ? 0 : 1);
})().catch((error) => {
  console.error('运行异常：', error);
  process.exit(1);
});
