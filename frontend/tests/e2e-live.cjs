/**
 * 真端到端：jsdom 里跑真实 app.js，fetch 打到真实 uvicorn。
 *
 * 与 ui-smoke.cjs 的分工：
 *  - ui-smoke：假后端，验证前端对各种响应（含错误）的渲染；
 *  - 本脚本：真后端，验证"页面 + 接口 + BCD 索引"确实能串起来。
 *
 * 前置：先启动后端
 *   cd backend && python -m uvicorn app.main:app --port 8010
 * 运行：
 *   node frontend/tests/e2e-live.cjs            # 默认 http://127.0.0.1:8010
 *   API_URL=http://127.0.0.1:8000 node frontend/tests/e2e-live.cjs
 */

const path = require('path');
const fs = require('fs');

const { JSDOM } = require(path.join(__dirname, '..', '..', 'backend', 'node', 'node_modules', 'jsdom'));

const API_URL = (process.env.API_URL || 'http://127.0.0.1:8010').replace(/\/$/, '');
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const failures = [];
function check(name, condition, detail = '') {
  if (condition) console.log(`  ✓ ${name}`);
  else {
    failures.push(`${name}${detail ? ` — ${detail}` : ''}`);
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

function buildPage(html) {
  let out = html.replace(/<script\b[^>]*\bsrc=[^>]*><\/script>/gi, '');
  out = out.replace(/<script>([\s\S]*?)<\/script>/gi, (match, body) => (
    body.includes('tailwind.config') ? '' : match
  ));
  const appSource = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  return out.replace('</body>', `<script>\n${appSource}\n</script>\n</body>`);
}

(async () => {
  console.log(`真端到端 · ${API_URL}`);

  // 先确认后端活着，避免把"没起服务"误报成页面 bug
  try {
    const health = await fetch(`${API_URL}/healthz`);
    if (!health.ok) throw new Error(`healthz ${health.status}`);
  } catch (error) {
    console.error(`后端不可达（${API_URL}）：${error.message}`);
    console.error('请先运行：cd backend && python -m uvicorn app.main:app --port 8010');
    process.exit(2);
  }

  const html = buildPage(fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8'));
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `${API_URL}/`,
    beforeParse(window) {
      window.fetch = (input, options) => fetch(new URL(String(input), `${API_URL}/`), options);
    },
  });

  const { window } = dom;
  const doc = window.document;
  const $ = (selector) => doc.querySelector(selector);
  const $$ = (selector) => Array.from(doc.querySelectorAll(selector));
  const click = (element) => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  /** 轮询等待条件成立；比写死 sleep 稳，jsdom 每次启动就有 ~2.6s 抖动 */
  async function waitFor(predicate, timeoutMs = 30000, interval = 200) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (predicate()) return true;
      await sleep(interval);
    }
    return false;
  }

  await waitFor(() => doc.querySelectorAll('.issue-row').length > 0, 30000);

  console.log('\n[1] /api/meta → chips');
  const chipBrowser = $$('.chip select').map((el) => el.value);
  const chipVersion = $$('.chip input').map((el) => el.value);
  check('4 个目标', chipBrowser.length === 4, `实际 ${chipBrowser.length}`);
  check('默认目标 = 用户选定基准线',
    chipBrowser.join(',') === 'chrome,safari,firefox,edge' && chipVersion.join(',') === '90,14.1,88,90',
    `${chipBrowser.join(',')} / ${chipVersion.join(',')}`);

  console.log('\n[2] POST /api/detect → 页面（CSS + JS + HTML 三合一）');
  check('问题行 = 11', $$('.issue-row').length === 11, `实际 ${$$('.issue-row').length}`);
  check('总行数 = 17（CSS 10 + JS 3 + HTML 4）', $('#stat-lines').textContent === '17',
    `实际 ${$('#stat-lines').textContent}`);
  check('问题数 = 11', $('#stat-issues').textContent === '11');
  check('等级分布 high 8 / medium 2 / low 1',
    $('#stat-issues-sub').textContent.includes('high 8')
    && $('#stat-issues-sub').textContent.includes('medium 2')
    && $('#stat-issues-sub').textContent.includes('low 1'),
    $('#stat-issues-sub').textContent);
  check('全量解析成功 → 无警告 banner', $('#banner').classList.contains('hidden'));
  check('卡片副文案含 声明/语句/标签',
    $('#stat-lines-sub').textContent.includes('声明 8')
    && $('#stat-lines-sub').textContent.includes('语句')
    && $('#stat-lines-sub').textContent.includes('标签 3'),
    $('#stat-lines-sub').textContent);

  const glyphs = $$('.issue-row').map((row) => row.querySelector('div')?.textContent.trim());
  check('三个维度都有行（# / JS / </>）',
    glyphs.includes('#') && glyphs.includes('JS') && glyphs.includes('</>'),
    [...new Set(glyphs)].join(' '));

  const firstRow = $$('.issue-row')[0];
  check('首行为 high 级', firstRow?.querySelector('.pill-high') !== null);
  check('行内展示 BCD 依据', /MDN BCD:/.test(firstRow?.textContent || ''));
  check('行内展示 bcd_path', /(css\.properties|javascript\.|api\.|html\.)/.test(firstRow?.textContent || ''));
  check('方法名推断标注了上下文未知',
    $$('.issue-row').some((row) => row.textContent.includes('上下文未知')));

  console.log('\n[2b] 顶部搜索 = 特性查询（真后端）');
  const searchInput = $('#search');
  searchInput.value = 'accent-color';
  searchInput.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await waitFor(() => !$('#lookup-panel').classList.contains('hidden'), 20000);
  check('查表返回 BCD 路径', $('#lookup-title').textContent.includes('css.properties.accent-color'),
    $('#lookup-title').textContent);
  check('按目标逐条给出结论', $$('#lookup-body .pill').length === 4,
    String($$('#lookup-body .pill').length));
  check('结论取自 BCD（含 requires）', $('#lookup-body').textContent.includes('requires Chrome 93'));

  console.log('\n[3] 再点一次检测（幂等）');
  const before = $$('.issue-row').length;
  $('#detect').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => !$('#detect').disabled, 30000);
  check('问题数不变', $$('.issue-row').length === before, `${before} → ${$$('.issue-row').length}`);

  console.log('\n[4] L3 验证（真后端 · 用示例修复，不依赖模型可用性）');
  click($('#use-sample-fix'));
  await waitFor(() => !$('#fix-state').textContent.includes('待执行')
    && !$('#fix-state').textContent.includes('待验证'), 40000);
  check('修复面板展开', !$('#fix-panel').classList.contains('hidden'));
  check('状态 = 全部解决', $('#fix-state').textContent.includes('全部解决'),
    $('#fix-state').textContent);
  check('已解决 11', $('#fix-summary').textContent.includes('已解决 11'),
    $('#fix-summary').textContent);
  check('新增问题 0', $('#fix-summary').textContent.includes('新增问题 0'));
  check('DOM 已对比', $('#fix-summary').textContent.includes('DOM'));
  check('差异视图含增删行', /[+−]/.test($('#fix-diff').textContent));
  check('原始代码的执行错误被记录', $('#fix-issues').textContent.includes('原始代码'));

  console.log('\n[5] 回归：safari 14.1 → 14.0 后结论翻转');
  const safariInput = $$('.chip input')[1];
  safariInput.value = '14.0';
  safariInput.dispatchEvent(new window.Event('input', { bubbles: true }));
  $('#detect').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => !$('#detect').disabled, 30000);
  const rows = $$('.issue-row').map((row) => row.textContent.replace(/\s+/g, ' '));
  check('出现 gap（flex_context）问题', rows.some((text) => /gap/.test(text) && /14\.1/.test(text)),
    `共 ${rows.length} 行`);

  console.log(`\n结果：${failures.length === 0 ? '全部通过' : `${failures.length} 项失败`}`);
  failures.forEach((item) => console.log(`  - ${item}`));
  process.exit(failures.length === 0 ? 0 : 1);
})().catch((error) => {
  console.error('运行异常：', error);
  process.exit(1);
});
