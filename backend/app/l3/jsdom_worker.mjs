/**
 * jsdom 执行器的 worker：真正跑用户代码的地方。
 *
 * 为什么要单独一个线程
 * ------------------
 * 用户代码里的 `while (true) {}` 会同步阻塞事件循环，主线程的 setTimeout 永远不会触发。
 * 唯一可靠的中断方式是主线程调用 `worker.terminate()`——它在 V8 层面截断，不需要对方配合。
 *
 * 本文件只负责"跑一次并回报快照"，超时策略全在 jsdom_runner.mjs（主线程）里。
 */

import { parentPort, workerData } from 'node:worker_threads';
import { createRequire } from 'node:module';

// jsdom 装在 backend/node/node_modules 下；本文件在 backend/app/l3/，默认解析走不到那里
const require = createRequire(new URL('../../node/package.json', import.meta.url));
const { JSDOM, VirtualConsole } = require('jsdom');

const MAX_CONSOLE_LINES = 50;

function snapshot(config) {
  const errors = [];
  const consoleLines = [];

  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (error) => {
    errors.push(String((error && error.message) || error));
  });
  for (const level of ['log', 'info', 'warn', 'error']) {
    virtualConsole.on(level, (...args) => {
      if (consoleLines.length < MAX_CONSOLE_LINES) {
        consoleLines.push(`${level}: ${args.map(String).join(' ')}`);
      }
    });
  }

  let dom = null;
  try {
    dom = new JSDOM(config.html || '<!doctype html><html><body></body></html>', {
      url: config.url || 'http://localhost/',
      runScripts: 'dangerously',
      virtualConsole,
      pretendToBeVisual: true,
      beforeParse(window) {
        window.addEventListener('error', (event) => {
          errors.push(String(event.message || 'window error'));
        });
        window.addEventListener('unhandledrejection', (event) => {
          errors.push(`unhandledrejection: ${String(event.reason)}`);
        });
      },
    });
  } catch (error) {
    errors.push(`构造 DOM 失败：${String(error)}`);
  }

  if (dom && config.js) {
    try {
      dom.window.eval(config.js);
    } catch (error) {
      errors.push(String((error && error.message) || error));
    }
  }

  let html = '';
  const counts = {};
  try {
    if (dom) {
      const body = dom.window.document.body;
      html = body ? body.innerHTML : '';
      dom.window.document.querySelectorAll('*').forEach((element) => {
        const tag = element.tagName.toLowerCase();
        counts[tag] = (counts[tag] || 0) + 1;
      });
    }
  } catch (error) {
    errors.push(`读取 DOM 失败：${String(error)}`);
  }

  if (dom) {
    try { dom.window.close(); } catch { /* 清理异常忽略 */ }
  }

  return { ok: errors.length === 0, errors, console: consoleLines, html, counts };
}

try {
  parentPort.postMessage(snapshot(workerData || {}));
} catch (error) {
  parentPort.postMessage({
    ok: false,
    errors: [`执行器异常：${String((error && error.stack) || error)}`],
    console: [],
    html: '',
    counts: {},
  });
}
