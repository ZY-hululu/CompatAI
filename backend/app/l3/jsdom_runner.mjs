/**
 * jsdom 执行器（主进程）：超时控制 + 批量执行 + 结构化输出。
 *
 * 协议：stdin 收一个 JSON，stdout 吐一个 JSON。
 *
 *   输入（批量）：
 *     { timeoutMs?: number, runs: [{ html: string, js: string, url?: string }, ...] }
 *   输入（单次，等价于 runs 只有一个元素）：
 *     { html: string, js: string }
 *
 *   输出：
 *     批量 → { results: [snapshot, ...] }
 *     单次 → snapshot
 *
 *   snapshot = { ok, errors[], console[], html, counts{tag:n}, timeout }
 *
 * 关键点：用户代码跑在 worker（jsdom_worker.mjs）里，主线程只负责计时。
 * 一旦超时，`worker.terminate()` 会从 V8 层面截断那个线程，
 * 所以哪怕是 `while (true) {}` 也拖不住主线程。
 */

import { Worker } from 'node:worker_threads';

const WORKER_URL = new URL('./jsdom_worker.mjs', import.meta.url);
const DEFAULT_TIMEOUT_MS = 5000;

function timeoutSnapshot(timeoutMs) {
  return {
    ok: false,
    timeout: true,
    errors: [`jsdom 执行超时（>${timeoutMs}ms），工作线程已终止（用户代码可能是死循环）`],
    console: [],
    html: '',
    counts: {},
  };
}

function runOne(config, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    let stopping = false;

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };

    const worker = new Worker(WORKER_URL, { workerData: config, stdout: false, stderr: false });

    const timer = setTimeout(async () => {
      if (settled) return;
      stopping = true;
      try {
        await worker.terminate();
      } catch {
        /* terminate 失败也要给出结论，不能挂住 */
      }
      finish(timeoutSnapshot(timeoutMs));
    }, timeoutMs);

    worker.once('message', (message) => {
      finish({ ...message, timeout: false });
      worker.terminate().catch(() => {});
    });
    worker.once('error', (error) => {
      if (stopping) return;
      finish({
        ok: false,
        timeout: false,
        errors: [`worker 异常：${String(error)}`],
        console: [],
        html: '',
        counts: {},
      });
    });
    worker.once('exit', (code) => {
      // 被 terminate 的 worker 会以退出码 1 结束，不能把它误报成"执行失败"
      if (stopping || code === 0) return;
      finish({
        ok: false,
        timeout: false,
        errors: [`worker 退出码 ${code}`],
        console: [],
        html: '',
        counts: {},
      });
    });
  });
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let buffer = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { buffer += chunk; });
    process.stdin.on('end', () => resolve(buffer));
    process.stdin.on('error', reject);
  });
}

(async () => {
  try {
    const raw = await readStdin();
    const config = JSON.parse(raw || '{}');
    const timeoutMs = Number(config.timeoutMs) > 0 ? Number(config.timeoutMs) : DEFAULT_TIMEOUT_MS;
    const batch = Array.isArray(config.runs);

    const runs = batch ? config.runs : [config];
    const results = [];
    for (const run of runs) {
      results.push(await runOne(run, timeoutMs));
    }

    process.stdout.write(JSON.stringify(batch ? { results } : results[0]));
    process.exit(0);
  } catch (error) {
    process.stdout.write(JSON.stringify({
      ok: false,
      timeout: false,
      errors: [`执行器异常：${String((error && error.stack) || error)}`],
      console: [],
      html: '',
      counts: {},
    }));
    process.exit(0);
  }
})();
