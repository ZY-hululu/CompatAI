/**
 * 第 3 步：前后端联调。
 *
 * 原则：
 * 1. 页面不再内置任何 issue 数据，全部来自 POST /api/detect；
 * 2. 后端返回什么就显示什么，前端不"润色"、不补算严重等级；
 * 3. 三种失败分别处理：422（参数错误，逐条列出）、503（索引未建）、网络不可达。
 *
 * 打开方式：uvicorn 起来后访问 http://127.0.0.1:8000/（同源，无 CORS）。
 * 若用 file:// 打开，则回落到 http://127.0.0.1:8000 作为 API 基址。
 */
(() => {
  'use strict';

  /* -------------------------------- 常量 -------------------------------- */

  const API_BASE = window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : '';
  const PALETTE = ['#8E95F9', '#A6B4FE', '#F7E4A6', '#D9E5D6'];
  const VERSION_RE = /^\d+(\.\d+){0,3}$/;
  const CONFIDENCE_LABEL = { context_unknown: '上下文未知', version_unknown: '版本未知' };
  const TYPE_GLYPH = { css: '#', js: 'JS', html: '</>' };
  const BROWSER_FALLBACK = ['chrome', 'safari', 'firefox', 'edge'];
  const BROWSER_LABEL = {
    chrome: 'Chrome',
    safari: 'Safari',
    firefox: 'Firefox',
    edge: 'Edge',
    opera: 'Opera',
    safari_ios: 'iOS Safari',
    chrome_android: 'Chrome Android',
  };

  const SAMPLE = {
    css: `.card {
  display: flex;
  gap: 12px;
  aspect-ratio: 16 / 9;
  accent-color: #8E95F9;
  container-type: inline-size;
  backdrop-filter: blur(6px);
  color-interpolation: sRGB;
  scroll-behavior: smooth;
}
`,
    js: `const name = user?.profile?.name;
const total = count ?? 0;
const last = items.at(-1);
`,
    html: `<dialog class="modal" open>
  <img src="cover.png" loading="lazy" decoding="async">
  <div inert>内容</div>
</dialog>
`,
  };

  // 手工写的"修复后代码"，用于在没有 API Key 时演示 L3 的回灌验证流程
  const SAMPLE_FIX = {
    css: `.card {
  display: flex;
  margin: -6px;
  color: #111111;
}
.card > * {
  margin: 6px;
}
`,
    js: `const profile = (window.__user || {}).profile || {};
const name = profile.name || '匿名';
const total = typeof count === 'number' ? count : 0;
const items = [1, 2, 3];
const last = items[items.length - 1];
const note = document.createElement('p');
note.textContent = name + ' / ' + total + ' / ' + last;
document.body.appendChild(note);
`,
    html: `<div class="modal">
  <img src="cover.png" alt="">
  <div>内容</div>
</div>
`,
  };

  /* -------------------------------- 状态 -------------------------------- */

  const state = {
    meta: null,
    browsers: BROWSER_FALLBACK.slice(),
    releases: {},
    targets: [],
    tab: 'css',
    code: { ...SAMPLE },
    issues: [],
    unknown: [],
    loading: false,
    query: '',
    lastDetect: null,
    explanations: [],
    fixedCode: null,
    verify: null,
    fixTab: 'diff',
  };

  /* -------------------------------- 工具 -------------------------------- */

  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
  const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const browserLabel = (id) => BROWSER_LABEL[id] || id;
  const browserColor = (id) => PALETTE[Math.max(0, state.browsers.indexOf(id)) % PALETTE.length];

  function parseTarget(label) {
    const [browser, version = ''] = String(label).split(/\s+/);
    return { browser, version };
  }

  /* -------------------------------- 接口 -------------------------------- */

  async function request(path, options) {
    const response = await fetch(`${API_BASE}${path}`, options);
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.response = response;
      throw error;
    }
    return response.json();
  }

  async function loadMeta() {
    try {
      const meta = await request('/api/meta');
      state.meta = meta;
      state.browsers = meta.browsers?.length ? meta.browsers : BROWSER_FALLBACK;
      state.releases = meta.releases || {};
      state.targets = (meta.default_targets || []).map(parseTarget);
    } catch (error) {
      // 元信息拿不到时不要让页面变空白：用内置默认值，并提示后端未连接
      state.targets = ['chrome 90', 'safari 14.1', 'firefox 88', 'edge 90'].map(parseTarget);
      renderBanner({
        kind: 'error',
        title: '无法获取 /api/meta',
        lines: [
          error.message,
          '请确认后端已启动，并通过 http://127.0.0.1:8000/ 打开本页',
        ],
      });
    }
    renderDatalists();
    renderChips();
  }

  /* ------------------------------ 目标浏览器 ------------------------------ */

  function renderDatalists() {
    $('datalists').innerHTML = Object.entries(state.releases || {})
      .map(([browser, versions]) => `
        <datalist id="dl-${esc(browser)}">
          ${versions.map((version) => `<option value="${esc(version)}"></option>`).join('')}
        </datalist>`)
      .join('');
  }

  function renderChips() {
    const chips = state.targets.map((target, index) => {
      const color = browserColor(target.browser);
      const options = state.browsers.map((id) => (
        `<option value="${esc(id)}"${id === target.browser ? ' selected' : ''}>${esc(browserLabel(id))}</option>`
      )).join('');

      return `
        <span class="chip" data-index="${index}">
          <span class="dot" style="background:${color}"></span>
          <select class="chip-browser" aria-label="浏览器">${options}</select>
          <input class="chip-version" value="${esc(target.version)}" inputmode="decimal"
                 list="dl-${esc(target.browser)}" aria-label="${esc(browserLabel(target.browser))} 版本号"
                 placeholder="版本" />
          <button class="chip-remove" aria-label="移除 ${esc(browserLabel(target.browser))}">✕</button>
        </span>`;
    }).join('');

    const add = `
      <button id="add-target"
              class="inline-flex items-center gap-1 rounded-inner border border-dashed border-black/12 px-3 py-1.5 text-[12px] text-muted transition hover:border-black/20 hover:text-ink">
        + 添加目标
      </button>`;

    $('target-chips').innerHTML = chips + add;
  }

  function addTarget() {
    const used = new Set(state.targets.map((target) => target.browser));
    const next = state.browsers.find((id) => !used.has(id)) || state.browsers[0];
    state.targets.push({ browser: next, version: '90' });
    renderChips();
    const inputs = document.querySelectorAll('.chip-version');
    inputs[inputs.length - 1]?.select();
  }

  function validateTargets() {
    if (!state.targets.length) return '至少需要一个目标浏览器（版本号任意，如 safari 15.0）';
    if (state.meta?.max_targets && state.targets.length > state.meta.max_targets) {
      return `目标最多 ${state.meta.max_targets} 个，当前 ${state.targets.length} 个`;
    }
    const problems = [];
    state.targets.forEach((target, index) => {
      if (!target.version) problems.push(`第 ${index + 1} 个目标（${browserLabel(target.browser)}）缺少版本号`);
      else if (!VERSION_RE.test(target.version)) {
        problems.push(`${browserLabel(target.browser)} 的版本号「${target.version}」不合法：需 1–4 段数字`);
      }
    });
    return problems.length ? problems.join('；') : null;
  }

  function collectPayload() {
    return {
      html: state.code.html,
      css: state.code.css,
      js: state.code.js,
      targets: state.targets.map((target) => `${target.browser} ${target.version}`.trim()),
    };
  }

  /* -------------------------------- 渲染 -------------------------------- */

  function renderBanner({ kind = 'warn', title, lines = [] }) {
    const banner = $('banner');
    if (!title) {
      banner.className = 'hidden';
      banner.innerHTML = '';
      return;
    }
    const tone = kind === 'error' ? 'bg-warn ring-1 ring-ink/10' : 'bg-warn';
    banner.className = `mt-6 flex items-start gap-3 rounded-2xl px-5 py-4 ${tone}`;
    banner.innerHTML = `
      <span class="mt-0.5 grid h-5 w-5 flex-none place-items-center rounded-full bg-ink/10 text-[11px] font-bold">!</span>
      <div class="min-w-0 flex-1">
        <p class="text-[13px] font-semibold">${esc(title)}</p>
        ${lines.length ? `<ul class="mt-1 space-y-0.5 text-[12px] text-ink/65">${
          lines.filter(Boolean).map((line) => `<li>${esc(line)}</li>`).join('')
        }</ul>` : ''}
      </div>
      <button data-banner-close aria-label="关闭提示" class="text-ink/35 transition hover:text-ink">✕</button>`;
  }

  function renderBannerFromResult(body) {
    const warnings = body.parse_warnings || [];
    if (!warnings.length) {
      renderBanner({ title: '' });
      return;
    }
    renderBanner({
      kind: 'warn',
      title: `解析警告 · 本次检测状态为 ${body.status}`,
      lines: warnings.map((warning) => {
        const where = warning.line ? `L${warning.line}` : '';
        return `[${String(warning.kind).toUpperCase()}${where ? ` ${where}` : ''}] ${warning.message}`;
      }),
    });
  }

  function renderStats(body) {
    const stats = body.stats || {};
    $('stat-lines').textContent = String(stats.total_lines ?? 0);
    $('stat-lines-sub').textContent =
      `CSS ${stats.css_lines ?? 0} · JS ${stats.js_lines ?? 0} · HTML ${stats.html_lines ?? 0} 行`
      + ` · 声明 ${stats.declarations_scanned ?? 0} · 语句 ${stats.js_statements ?? 0}`
      + ` · 标签 ${stats.html_elements ?? 0}`;

    const severity = { high: 0, medium: 0, low: 0 };
    state.issues.forEach((issue) => { severity[issue.severity] = (severity[issue.severity] || 0) + 1; });

    $('stat-issues').textContent = String(state.issues.length);
    $('stat-issues-sub').textContent = `high ${severity.high} · medium ${severity.medium} · low ${severity.low}`;
    return severity;
  }

  function severityPill(severity) {
    return `<span class="pill pill-${severity}">${esc(severity)}</span>`;
  }

  function issueRow(issue) {
    const confidence = issue.confidence && issue.confidence !== 'certain'
      ? `<span class="pill pill-muted">${esc(CONFIDENCE_LABEL[issue.confidence] || issue.confidence)}</span>`
      : '';

    return `
      <div class="issue-row flex items-start gap-4 px-6 py-4">
        <div class="mt-0.5 grid h-9 w-9 flex-none place-items-center rounded-inner text-[11px] font-bold"
             style="background:var(--accent-soft);color:var(--ink)">${esc(TYPE_GLYPH[issue.type] || issue.type)}</div>

        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-[14px] font-semibold">${esc(issue.feature)}</span>
            <span class="font-mono text-[11px] text-muted">L${issue.line}:${issue.column}</span>
            ${confidence}
          </div>
          <p class="mt-0.5 truncate text-[12px] text-muted">${esc((issue.unsupported_browsers || []).join(' · '))}</p>
          <p class="mt-0.5 truncate font-mono text-[11px] text-muted/80">${esc(issue.source_data)}</p>
        </div>

        <div class="flex flex-none items-center gap-2 pt-0.5">${severityPill(issue.severity)}</div>
      </div>`;
  }

  function renderIssues() {
    const query = state.query.trim().toLowerCase();
    const rows = state.issues.filter((issue) => {
      if (!query) return true;
      return [
        issue.feature,
        issue.bcd_path,
        issue.source_data,
        ...(issue.unsupported_browsers || []),
      ].join(' ').toLowerCase().includes(query);
    });

    $('issue-count').textContent = String(rows.length);
    $('issue-list').innerHTML = rows.map(issueRow).join('');

    const empty = $('issue-empty');
    empty.classList.toggle('hidden', rows.length > 0);
    if (rows.length === 0) {
      empty.querySelector('p').textContent = state.issues.length === 0
        ? '没有发现兼容性问题'
        : '没有匹配的问题';
    }

    $('list-status').textContent = query
      ? `筛选「${state.query.trim()}」· 命中 ${rows.length} 条`
      : `共 ${state.issues.length} 条 · 按严重程度排序 · 来自 L1 确定性检测`;
  }

  function renderUnknown() {
    const el = $('unknown-features');
    if (!state.unknown.length) {
      el.classList.add('hidden');
      el.innerHTML = '';
      return;
    }
    el.classList.remove('hidden');
    el.innerHTML = `<span class="font-semibold text-ink/70">未识别特性</span>`
      + `（BCD 中查不到，不计入问题数，需人工确认）：<span class="font-mono">${
        state.unknown.map(esc).join(', ')}</span>`;
  }

  /* -------------------------------- 图表 -------------------------------- */

  let barChart = null;
  let donutChart = null;

  function renderDonutLegend(severity) {
    const fallback = { high: '#F7E4A6', medium: '#8E95F9', low: '#D9E5D6' };
    $('donut-legend').innerHTML = [
      ['high', severity.high || 0, token('--warn') || fallback.high],
      ['medium', severity.medium || 0, token('--accent-from') || fallback.medium],
      ['low', severity.low || 0, token('--safe') || fallback.low],
    ].map(([label, value, color]) => `
        <span class="flex items-center gap-1.5">
          <span class="h-2 w-2 rounded-full" style="background:${color}"></span>${label}
          <span class="font-semibold text-ink">${value}</span>
        </span>`).join('');
  }

  function initCharts() {
    if (typeof echarts === 'undefined') {
      ['chart-bar', 'chart-donut'].forEach((id) => {
        $(id).innerHTML =
          '<p class="grid h-full w-full place-items-center text-[12px] text-muted">图表库未加载（CDN 不可用）</p>';
      });
      return;
    }
    barChart = echarts.init($('chart-bar'));
    donutChart = echarts.init($('chart-donut'));
    window.addEventListener('resize', () => {
      barChart?.resize();
      donutChart?.resize();
    });
  }

  function renderCharts(severity) {
    renderDonutLegend(severity || { high: 0, medium: 0, low: 0 });
    if (!barChart || !donutChart) return;

    const accentFrom = token('--accent-from') || '#8E95F9';
    const accentTo = token('--accent-to') || '#A6B4FE';
    const warn = token('--warn') || '#F7E4A6';
    const safe = token('--safe') || '#D9E5D6';
    const muted = token('--muted') || '#888888';

    // 每个浏览器被多少条 issue 命中：一条 issue 可命中多个浏览器，因此总和会大于 issue 数
    const browsers = state.targets.map((target) => target.browser);
    const counts = browsers.map((browser) => state.issues.filter((issue) => (
      (issue.unsupported_browsers || []).some((label) => label.split(' ')[0] === browser)
    )).length);

    barChart.setOption({
      grid: { left: 4, right: 12, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: '#111111',
        borderWidth: 0,
        padding: [8, 12],
        textStyle: { color: '#ffffff', fontSize: 12 },
      },
      xAxis: {
        type: 'category',
        data: browsers.map(browserLabel),
        axisLine: { lineStyle: { color: 'rgba(0,0,0,0.08)' } },
        axisTick: { show: false },
        axisLabel: { color: muted, fontSize: 12 },
      },
      yAxis: {
        type: 'value',
        minInterval: 1,
        splitLine: { lineStyle: { color: 'rgba(0,0,0,0.05)', type: 'dashed' } },
        axisLabel: { color: muted, fontSize: 11 },
      },
      series: [{
        type: 'bar',
        data: counts,
        barWidth: '44%',
        itemStyle: {
          borderRadius: [10, 10, 0, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: accentFrom },
            { offset: 1, color: accentTo },
          ]),
        },
      }],
    }, true);

    donutChart.setOption({
      tooltip: {
        trigger: 'item',
        backgroundColor: '#111111',
        borderWidth: 0,
        padding: [8, 12],
        textStyle: { color: '#ffffff', fontSize: 12 },
        formatter: '{b}: {c} 条 ({d}%)',
      },
      series: [{
        type: 'pie',
        radius: ['58%', '80%'],
        center: ['50%', '52%'],
        label: { show: false },
        labelLine: { show: false },
        itemStyle: { borderColor: '#FFFFFF', borderWidth: 4, borderRadius: 8 },
        data: [
          { name: 'high', value: severity?.high || 0, itemStyle: { color: warn } },
          { name: 'medium', value: severity?.medium || 0, itemStyle: { color: accentFrom } },
          { name: 'low', value: severity?.low || 0, itemStyle: { color: safe } },
        ],
      }],
    }, true);
  }

  /* ------------------------------ 交互与流程 ------------------------------ */

  function setTab(tab) {
    state.code[state.tab] = $('code').value;
    state.tab = tab;
    $('code').value = state.code[tab];
    document.querySelectorAll('.tab').forEach((button) => {
      const active = button.dataset.tab === tab;
      button.setAttribute('aria-selected', String(active));
      button.classList.toggle('bg-white', active);
      button.classList.toggle('shadow-sm', active);
      button.classList.toggle('text-ink', active);
      button.classList.toggle('text-muted', !active);
    });
    $('code-hint').classList.add('hidden');
  }

  function setLoading(loading) {
    state.loading = loading;
    $('detect').disabled = loading;
    $('detect-label').textContent = loading ? '检测中…' : '开始检测';
    $('detect-spinner').classList.toggle('hidden', !loading);
    $('issue-skeleton').classList.toggle('hidden', !loading);
    $('issue-list').classList.toggle('opacity-40', loading);
  }

  async function handleHttpError(response) {
    let body = null;
    try { body = await response.json(); } catch { /* 无 JSON 体 */ }

    if (response.status === 422) {
      const details = Array.isArray(body?.detail)
        ? body.detail.map((item) => item.msg || String(item))
        : [String(body?.detail || '请求参数校验失败')];
      renderBanner({ kind: 'error', title: '请求被拒绝（422）', lines: details });
    } else if (response.status === 503) {
      renderBanner({
        kind: 'error',
        title: 'BCD 索引未就绪（503）',
        lines: [
          String(body?.detail || ''),
          '在 backend/ 目录运行：python scripts/build_bcd_index.py',
        ],
      });
    } else {
      renderBanner({
        kind: 'error',
        title: `接口错误（HTTP ${response.status}）`,
        lines: [String(body?.detail || response.statusText || '')],
      });
    }
    $('list-status').textContent = `检测失败 · HTTP ${response.status}`;
  }

  function applyResult(body) {
    state.lastDetect = body;
    state.explanations = [];
    state.fixedCode = null;
    state.verify = null;
    $('fix-panel').classList.add('hidden');

    state.issues = (body.issues || []).slice().sort((a, b) => {
      const order = { high: 0, medium: 1, low: 2 };
      return (order[a.severity] ?? 9) - (order[b.severity] ?? 9) || a.line - b.line;
    });
    state.unknown = body.unknown_features || [];

    const severity = renderStats(body);
    renderIssues();
    renderUnknown();
    renderCharts(severity);
    renderBannerFromResult(body);

    if (body.status === 'success' && !(body.parse_warnings || []).length) {
      $('list-status').textContent =
        `检测完成 · ${body.stats?.total_lines ?? 0} 行 · ${state.issues.length} 条问题`;
    }
  }

  async function runDetect() {
    if (state.loading) return;

    const invalid = validateTargets();
    if (invalid) {
      renderBanner({ kind: 'error', title: '目标浏览器配置不合法', lines: [invalid] });
      return;
    }

    setLoading(true);
    try {
      const body = await request('/api/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectPayload()),
      });
      applyResult(body);
    } catch (error) {
      if (error.response) {
        await handleHttpError(error.response);
      } else {
        renderBanner({
          kind: 'error',
          title: '无法连接后端',
          lines: [
            error.message,
            '请确认 uvicorn 已启动，并通过 http://127.0.0.1:8000/ 打开本页',
          ],
        });
        $('list-status').textContent = '后端未连接 · 结果未更新';
      }
    } finally {
      setLoading(false);
    }
  }

  function loadContractExample() {
    setTab('css');
    state.code.css = '.box { display: flex; gap: 10px; }';
    $('code').value = state.code.css;
    const hint = $('code-hint');
    hint.textContent = '契约示例在默认目标集下为 0 条问题：Safari 14.1 恰好是 Flex gap 的支持起点（BCD: 14.1）。把 safari 改成 14.0 再检测，可复现 1 条 medium。';
    hint.classList.remove('hidden');
  }

  /* ---------------------------- 修复与验证（L2 + L3） ---------------------------- */

  function linesOf(text) {
    return (text || '').split('\n');
  }

  /** 行级 LCS 差异；不引入第三方 diff 库，保证离线可用 */
  function diffLines(before, after) {
    const n = before.length;
    const m = after.length;
    const table = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i -= 1) {
      for (let j = m - 1; j >= 0; j -= 1) {
        table[i][j] = before[i] === after[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    const rows = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (before[i] === after[j]) {
        rows.push({ type: 'same', text: before[i] });
        i += 1; j += 1;
      } else if (table[i + 1][j] >= table[i][j + 1]) {
        rows.push({ type: 'del', text: before[i] });
        i += 1;
      } else {
        rows.push({ type: 'add', text: after[j] });
        j += 1;
      }
    }
    while (i < n) { rows.push({ type: 'del', text: before[i] }); i += 1; }
    while (j < m) { rows.push({ type: 'add', text: after[j] }); j += 1; }
    return rows;
  }

  function diffRow(row) {
    const mark = row.type === 'add' ? '+' : row.type === 'del' ? '−' : ' ';
    const style = row.type === 'add'
      ? 'background:rgba(217,229,214,0.55)'
      : row.type === 'del'
        ? 'background:rgba(247,228,166,0.45)'
        : '';
    const tone = row.type === 'same' ? 'color:#888888' : 'color:#111111';
    return `<div class="flex gap-2 rounded-[6px] px-2" style="${style}">`
      + `<span class="select-none" style="${tone}">${mark}</span>`
      + `<span class="whitespace-pre-wrap" style="${tone}">${esc(row.text)}</span></div>`;
  }

  function renderDiff(original, fixed) {
    const sections = [];
    for (const lang of ['css', 'js', 'html']) {
      const rows = diffLines(linesOf(original[lang]), linesOf(fixed[lang]));
      if (rows.every((row) => row.type === 'same')) continue;
      const added = rows.filter((row) => row.type === 'add').length;
      const removed = rows.filter((row) => row.type === 'del').length;
      sections.push(
        `<p class="mt-4 text-[11px] uppercase tracking-[0.12em] text-muted first:mt-0">`
        + `${lang} · +${added} / −${removed}</p>`
        + rows.map(diffRow).join('')
      );
    }
    return sections.join('') || '<p class="text-[12px] text-muted">原始代码与修复后代码一致</p>';
  }

  function renderCodePane(element, code) {
    const parts = [];
    for (const lang of ['css', 'js', 'html']) {
      const text = (code[lang] || '').trim();
      if (!text) continue;
      parts.push(
        `<p class="mt-4 text-[11px] uppercase tracking-[0.12em] text-muted first:mt-0">${lang}</p>`
        + `<pre class="whitespace-pre-wrap">${esc(text)}</pre>`
      );
    }
    element.innerHTML = parts.join('') || '<p class="text-[12px] text-muted">空</p>';
  }

  function setFixTab(tab) {
    state.fixTab = tab;
    $('fix-diff').classList.toggle('hidden', tab !== 'diff');
    $('fix-fixed').classList.toggle('hidden', tab !== 'fixed');
    $('fix-original').classList.toggle('hidden', tab !== 'original');
    document.querySelectorAll('.fixtab').forEach((button) => {
      const active = button.dataset.fixtab === tab;
      button.setAttribute('aria-selected', String(active));
      button.classList.toggle('bg-white', active);
      button.classList.toggle('shadow-sm', active);
      button.classList.toggle('text-ink', active);
      button.classList.toggle('text-muted', !active);
    });
  }

  function renderFixSummary() {
    const stateEl = $('fix-state');
    const summaryEl = $('fix-summary');
    const verify = state.verify;

    if (!verify) {
      stateEl.className = 'pill pill-muted';
      stateEl.textContent = state.explanations.length ? '待验证' : '待执行';
      const compose = state.lastCompose;
      summaryEl.innerHTML = state.explanations.length
        ? `<span>${state.explanations.length} 条解释</span>`
          + (compose && compose.totalEdits
            ? `<span class="pill pill-muted">行级修复 ${compose.totalEdits} 条</span>`
            : '')
        : '';
      return;
    }

    const compose = state.lastCompose;
    const dom = verify.dom_diff || {};
    const domText = dom.ran
      ? `DOM ${dom.changed ? '有变化' : '无变化'} +${dom.added_nodes ?? 0}/−${dom.removed_nodes ?? 0}`
      : 'DOM 未对比';
    const review = verify.needs_review || [];
    const editPill = compose && compose.totalEdits
      ? `<span class="pill pill-muted">行级修复 ${compose.totalEdits} 条 / 覆盖 ${compose.affectedIssues} 个问题</span>`
      : '';
    summaryEl.innerHTML = [
      editPill,
      `<span class="pill pill-low">已解决 ${verify.resolved.length}</span>`,
      `<span class="pill pill-muted">未解决 ${verify.remaining.length}</span>`,
      `<span class="pill ${verify.new_issues.length ? 'pill-high' : 'pill-muted'}">新增问题 ${verify.new_issues.length}</span>`,
      review.length ? `<span class="pill pill-high">待复核 ${review.length}</span>` : '',
      `<span>${esc(domText)}</span>`,
    ].filter(Boolean).join('');

    // 优先级：引入新问题 > 验证超时 > 全部解决 > 部分解决
    // （"修复把别的东西弄坏了"比"没验证完"更需要被看见）
    if (verify.new_issues.length) {
      stateEl.className = 'pill pill-high';
      stateEl.textContent = '引入新问题';
    } else if (verify.status === 'partial') {
      stateEl.className = 'pill pill-high';
      stateEl.textContent = '验证超时';
    } else if (review.length) {
      stateEl.className = 'pill pill-medium';
      stateEl.textContent = '需人工复核';
    } else if (verify.remaining.length === 0 && verify.resolved.length) {
      stateEl.className = 'pill pill-low';
      stateEl.textContent = '全部解决';
    } else {
      stateEl.className = 'pill pill-medium';
      stateEl.textContent = '部分解决';
    }
  }

  function renderFixIssues() {
    const parts = [];
    const verify = state.verify;
    const newIssues = (verify && verify.new_issues) || [];

    if (newIssues.length) {
      parts.push('<p class="text-[12px] font-semibold">修复引入的新问题</p>');
      parts.push(newIssues.map((issue) => `
        <div class="mt-2 rounded-inner bg-warn/40 px-3 py-2 text-[12px]">
          <span class="font-medium">${esc(issue.feature)}</span>
          <span class="ml-2 font-mono text-[11px] text-muted">${esc(issue.bcd_path)}</span>
          <p class="mt-0.5 text-muted">${esc(issue.source_data)}</p>
        </div>`).join(''));
    }

    const review = (verify && verify.needs_review) || [];
    const unknown = (verify && verify.unknown_features) || [];
    if (review.length) {
      parts.push('<p class="mt-3 text-[12px] font-semibold">待复核：已不在 issues 里，但修复后查不到对应特性</p>');
      parts.push(`<p class="mt-1 text-[11px] text-muted">${review.map(esc).join(', ')}</p>`);
      if (unknown.length) {
        parts.push(`<p class="mt-1 text-[11px] text-muted">修复后的未识别特性：`
          + `<span class="font-mono">${unknown.map(esc).join(', ')}</span>`
          + '（可能只是"查不到"，请人工确认是否真的改掉了）</p>');
      }
    }

    const errors = (verify && verify.dom_diff && verify.dom_diff.errors) || [];
    if (errors.length) {
      parts.push('<p class="mt-3 text-[12px] font-semibold">执行期信息（jsdom）</p>');
      parts.push(`<ul class="mt-1 space-y-0.5 text-[11px] text-muted">${
        errors.map((error) => `<li>${esc(error)}</li>`).join('')}</ul>`);
    }

    if (state.explanations.length) {
      parts.push('<p class="mt-3 text-[12px] font-semibold">解释（L2）</p>');
      parts.push(state.explanations.map((explanation) => `
        <div class="mt-2 text-[12px] leading-relaxed">
          <span class="font-mono text-[11px] text-muted">${esc(explanation.issue_id)}</span>
          ${explanation.needs_human ? '<span class="pill pill-muted ml-2">需人工修复</span>' : ''}
          <p class="mt-0.5">${esc(explanation.explain_text)}</p>
          ${explanation.fix_diff ? `<p class="mt-0.5 text-muted">${esc(explanation.fix_diff)}</p>` : ''}
        </div>`).join(''));
    }

    $('fix-issues').innerHTML = parts.join('');
  }

  function renderFixPanel() {
    $('fix-panel').classList.remove('hidden');
    $('fix-diff').innerHTML = renderDiff(state.code, state.fixedCode || state.code);
    renderCodePane($('fix-fixed'), state.fixedCode || state.code);
    renderCodePane($('fix-original'), state.code);
    renderFixSummary();
    renderFixIssues();
    setFixTab(state.fixTab || 'diff');
  }

  /** 应用行区间替换：按行号从大到小改，避免前面的改动让后面的行号位移。 */
  function applyEdits(source, edits) {
    const lines = (source || '').split('\n');
    [...edits]
      .sort((a, b) => b.start_line - a.start_line)
      .forEach((edit) => {
        const replacement = edit.replacement === '' ? [] : String(edit.replacement).split('\n');
        lines.splice(edit.start_line - 1, edit.end_line - edit.start_line + 1, ...replacement);
      });
    return lines.join('\n');
  }

  /** 把 L2 的修复合并成一份"修复后代码"。
   *
   *  首选 `fix_edits`（行区间替换）：不同 issue 的修改互不重叠，**全部**按行号倒序应用，
   *  所以 N 条修复能真正叠加成一份代码。
   *  `fix_code_*`（整份文件）只作为旧输出兼容：同语言多条时无法安全合并，取第一条并提示其余未合并。 */
  function composeFixed(code, explanations) {
    const fixed = { ...code };
    const grouped = { css: [], js: [], html: [] };
    const affected = new Set();

    for (const explanation of explanations) {
      for (const edit of explanation.fix_edits || []) {
        if (!grouped[edit.language]) continue;
        grouped[edit.language].push(edit);
        affected.add(explanation.issue_id);
      }
    }

    let totalEdits = 0;
    for (const lang of ['css', 'js', 'html']) {
      if (grouped[lang].length) {
        fixed[lang] = applyEdits(fixed[lang], grouped[lang]);
        totalEdits += grouped[lang].length;
      }
    }

    const legacySkip = [];
    let legacyApplied = 0;
    for (const lang of ['css', 'js', 'html']) {
      if (grouped[lang].length) continue;       // 已经有行级修复，不再用整份文件覆盖
      const key = `fix_code_${lang}`;
      const fixes = explanations.filter((explanation) => explanation[key]);
      if (!fixes.length) continue;
      fixed[lang] = fixes[0][key];
      legacyApplied += 1;
      legacySkip.push(...fixes.slice(1).map((explanation) => explanation.issue_id));
    }

    return {
      fixed,
      totalEdits,
      affectedIssues: affected.size,
      legacyApplied,
      legacySkip,
      applied: totalEdits > 0 || legacyApplied > 0,
    };
  }

  async function runVerify() {
    if (!state.fixedCode) return;
    const button = $('verify-fix');
    const label = button.textContent;
    button.disabled = true;
    button.textContent = '验证中…';
    try {
      const body = await request('/api/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          original: state.code,
          fixed: state.fixedCode,
          targets: collectPayload().targets,
        }),
      });
      state.verify = body;
      renderFixPanel();
    } catch (error) {
      if (error.response) await handleHttpError(error.response);
      else {
        renderBanner({
          kind: 'error',
          title: '验证失败：无法连接后端',
          lines: [error.message],
        });
      }
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  async function runFix() {
    if (!state.lastDetect || !state.lastDetect.issues.length) {
      renderBanner({ kind: 'warn', title: '请先检测出问题', lines: ['一键修复需要 L1 的检测结果'] });
      return;
    }

    const button = $('fix-all');
    const original = button.innerHTML;
    button.disabled = true;
    button.textContent = '修复中…';
    try {
      const body = await request('/api/explain-fix', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ detect: state.lastDetect, code: state.code }),
      });
      state.explanations = body.explanations || [];
      const composed = composeFixed(state.code, state.explanations);
      state.lastCompose = composed;
      state.fixedCode = composed.applied ? composed.fixed : null;
      state.verify = null;

      if (!composed.applied) {
        $('fix-panel').classList.remove('hidden');
        renderFixPanel();
        renderBanner({
          kind: 'warn',
          title: body.degraded
            ? 'L2 走的是模板兜底，没有可自动应用的修复'
            : '模型这次没有给出可应用的补丁（fix_edits 为空）',
          lines: ['可点“填入示例修复”体验 L3 回灌验证流程'],
        });
        return;
      }

      renderFixPanel();
      if (composed.legacySkip.length) {
        renderBanner({
          kind: 'warn',
          title: `有 ${composed.legacySkip.length} 条旧格式（整份文件）修复未合并`,
          lines: [`未合并：${composed.legacySkip.join(', ')}，可让模型改用行级修复重试`],
        });
      }
      await runVerify();
    } catch (error) {
      if (error.response) await handleHttpError(error.response);
      else {
        renderBanner({ kind: 'error', title: '修复失败：无法连接后端', lines: [error.message] });
      }
    } finally {
      button.disabled = false;
      button.innerHTML = original;
    }
  }

  async function useSampleFix() {
    state.fixedCode = { ...SAMPLE_FIX };
    state.verify = null;
    renderFixPanel();
    await runVerify();
  }

  /* -------------------------------- 事件 -------------------------------- */

  $('target-chips').addEventListener('click', (event) => {
    if (event.target.id === 'add-target') {
      addTarget();
      return;
    }
    const remove = event.target.closest('.chip-remove');
    if (!remove) return;
    state.targets.splice(Number(remove.closest('.chip').dataset.index), 1);
    renderChips();
  });

  $('target-chips').addEventListener('change', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip || !event.target.classList.contains('chip-browser')) return;
    const index = Number(chip.dataset.index);
    state.targets[index].browser = event.target.value;
    renderChips();
  });

  $('target-chips').addEventListener('input', (event) => {
    if (!event.target.classList.contains('chip-version')) return;
    // 词法与后端 parse_version 对齐：只允许数字和点，最多 4 段
    const cleaned = event.target.value.replace(/[^0-9.]/g, '');
    if (cleaned !== event.target.value) event.target.value = cleaned;
    const chip = event.target.closest('.chip');
    state.targets[Number(chip.dataset.index)].version = cleaned;
  });

  $('banner').addEventListener('click', (event) => {
    if (event.target.closest('[data-banner-close]')) renderBanner({ title: '' });
  });

  $('code').addEventListener('input', (event) => {
    state.code[state.tab] = event.target.value;
  });

  $('code').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      runDetect();
    }
  });

  $('search').addEventListener('input', (event) => {
    state.query = event.target.value;
    renderIssues();
  });

  document.querySelectorAll('.tab').forEach((button) => {
    button.addEventListener('click', () => setTab(button.dataset.tab));
  });

  $('detect').addEventListener('click', runDetect);
  $('load-contract').addEventListener('click', loadContractExample);
  $('fix-all').addEventListener('click', runFix);
  $('use-sample-fix').addEventListener('click', useSampleFix);
  $('verify-fix').addEventListener('click', runVerify);
  document.querySelectorAll('.fixtab').forEach((button) => {
    button.addEventListener('click', () => setFixTab(button.dataset.fixtab));
  });

  /* -------------------------------- 启动 -------------------------------- */

  (async function bootstrap() {
    // 先把示例代码灌进 textarea，再走 setTab —— setTab 会把当前 textarea 内容"存回" state，
    // 若顺序反了，空 textarea 会覆盖掉 SAMPLE.css，首次自动检测就会发空请求（422）。
    $('code').value = state.code[state.tab];
    setTab(state.tab);
    initCharts();
    await loadMeta();
    await runDetect();   // 首次加载自动检测一次，页面不会是空的
  })();
})();
