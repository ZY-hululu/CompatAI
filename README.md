# CompatAI — AI 驱动的浏览器兼容性检测与验证系统

> **在大模型开始写代码之后，"能不能跑"变成了一个需要重新回答的问题。**
> CompatAI 用「确定性查表」判定兼容性，用「受约束的大模型」解释与给出修复，再用「沙箱回灌」验证修复是否真的有效。

---

## 1. 项目简介

### 1.1 问题：AI 擅长生成，不擅长验证

在 AI 辅助编码普及之后，浏览器兼容性问题的形态变了：代码产出的速度大幅提升，但**兼容性判断的依据并没有随之变得可靠**。

把源码直接丢给大模型问"这段代码在 Safari 14.1 能跑吗"，会得到两种典型失败：

- **幻觉**：模型给出看似精确、实则错误的版本号（"Safari 15 起支持"而真实门槛是 15.4）；
- **不可复现**：同一段代码两次询问，结论不一致；换一个模型，结论又变一次。

对课程作业而言，这是"说法"；对结题报告而言，这是缺陷。**兼容性结论必须是可追溯、可复现的事实，而不是模型的表达。**

### 1.2 我们的答案：把"判定"和"解释"切开

CompatAI 将系统拆成三层，每层只做一件事：

| 层 | 做什么 | 谁来做 | 可否产生"事实" |
|---|---|---|---|
| **L1** | 从源码中提取 Web 特性，逐条查询 MDN BCD | 解析器 + 本地索引，**无网络、无 LLM** | ✅ 唯一的事实来源 |
| **L2** | 把 L1 的结论翻译成自然语言，给出等价修复 | 大模型，但被四道闸门约束 | ❌ 只解释，不判定 |
| **L3** | 把修复后的代码重新回灌 L1，并用 jsdom 对比行为 | 确定性代码 + 沙箱 | ✅ 可验证的结论 |

核心命题：**大模型可以参与"怎么解释"，但绝不能参与"哪些浏览器不支持"。**

### 1.3 三条设计原则

1. **确定性优先**：同一份输入 + 同一个索引 ⇒ 逐字节相同的输出（已有测试钉死）。
2. **失败要显性**：拿不到证据时说"未知"，不猜；模型跑不通时降级并标注，不装作成功。
3. **离线可复现**：BCD 数据、前端依赖、图表库全部本地化，断网仍可完整演示。

---

## 2. 核心特性

### 2.1 L1 · 确定性静态检测（CSS / HTML / JS 三维）

- **CSS**：`tinycss2` 解析样式表与内联 `style`，并**结合同规则块的 `display` 值推断布局上下文**。
  这一点直接决定正确性：`gap` 在 BCD 里是上下文相关的——基础路径 `css.properties.gap` 的 Safari 门槛是 10.1，
  而 Flex 布局的真实门槛在子路径 `gap.flex_context`（Safari 14.1）。只查基础路径会**过于乐观**，
  把 Safari 12 误判为"支持"。
- **JS**：`esprima` AST + **自写 tokenizer 双通道**。esprima 4.0.1 停在 ES2018 附近，
  遇到 `obj?.name` 会直接抛语法错误；若只用 AST，一份用到可选链的文件会**整份失去检测**。
  因此 tokenizer 常驻负责语法级特性（`?.` `??` `??=` `**` 私有字段 `static {}` `1_000` `123n` `import()` …），
  AST 负责 API 级特性（`Array.prototype.at`、`document.querySelector`、`new IntersectionObserver`），
  解析失败时退回 token 链扫描兜底。
- **HTML**：`BeautifulSoup4` + `html.parser` 提取标签与属性（元素专属属性查不到时回落 `html.global_attributes`），
  并把内嵌的 `<style>` / `<script>` / `style=""` 交给对应解析器，**行号按内嵌位置平移**。
  （实测：`lxml` 解析器不提供 `sourceline`，`html.parser` 才提供，故弃用 lxml。）

数据源为本地化的 **MDN BCD 5.6.11**：13,196 条归一化特性、6.0 MB 索引。
`mirror`、`alternative_name`、`partial_implementation`、`≤37` / `preview` 等 8 种形态在**构建期**统一归一化，
运行期只做纯版本比较。

### 2.2 L2 · 防幻觉解释（四道闸门）

| 闸门 | 机制 | 拦截什么 |
|---|---|---|
| ① 输入白名单 | Prompt 只含 L1 命中的 issue + 命中行 ±20 行上下文 | 模型自行"发现"别的问题、解释越权 |
| ② 结构化输出 | `response_format=json_schema`，`issue_id` 用 `enum` 枚举本批 id；不支持时退 `json_object` → 纯文本 + 容错解析 | 编造 issue_id、输出自由文本 |
| ③ 后置校验 | 版本号封闭（依据写 15.4，写 15 即拦）、口径检查、修复代码语法门禁（tinycss2 / 括号配平 / 标签闭合） | 幻觉版本号、把语法错误解释成 API 缺失 |
| ④ 确定性降级 | 重试（默认 2 次）仍失败 ⇒ 模板化解释，`degraded=true` + `needs_human=true`，`fix_*=null` | 半可信输出混进结果 |

**口径隔离**是硬要求：语法级问题（`javascript.operators.*` 等）必须解释为
"旧浏览器**解析阶段**失败，整段脚本不执行"；API 级问题（`javascript.builtins.*` / `api.*`）必须解释为
"旧浏览器**没有该成员**，运行时得到 `undefined` / 抛 `TypeError`"。两者写反会被校验拦下并要求重试。

> 实测（Mock LLM 连续注入三种幻觉）：`Safari 15`（依据为 15.4）→ 版本号拦截；
> `issue_7`（不在批内）→ 白名单拦截；把语法错误写成"没有这个方法"→ 口径拦截；最终降级为模板解释。

### 2.3 L3 · 沙箱验证（回灌重检 + 行为对比）

- **回灌重检**：把修复后的代码重新交给 L1，按 `(type, bcd_path, feature)` 三元组比对，
  输出 `resolved` / `remaining` / `new_issues`。刻意不含行号与 id——修复会让行号整体位移，
  用行号比对会把"同一个问题"误判成"已解决 + 新问题"。
- **行级修复合并**：L2 的修复以「行区间替换」表达（`fix_edits`），不同 issue 的改动互不重叠，
  合成时按行号倒序依次应用，**N 条修复可以叠加成一次修复**（实测 11 条全部合并、问题归零）。
- **行为对比**：jsdom 执行原始与修复后的代码，比较执行期异常、`body.innerHTML` 变化、各标签数量增减。
- **强制超时**：用户代码里的 `while (true) {}` 是**同步阻塞**，`setTimeout` 永远不会触发；
  同时本机 `node` 可能被 `.CMD` 包装脚本代理，`kill()` 只杀掉外壳、真身继续占用管道，
  使父进程 `communicate()` 永久阻塞。因此采用两层机制：
  1. 用户代码跑在 **worker_threads**，主线程到点 `worker.terminate()`（V8 级截断）；
  2. Python 侧 `taskkill /F /T` 杀**进程树** + 有界等待，作为兜底。
- 超时后返回 `dom_diff.ran=false` + `status="partial"` + 明确错误文案，**不会拖死请求线程**。

### 2.4 100% 离线前端

- 深色侧边栏 + 微拟物卡片式 Dashboard，设计 token 集中于 `assets/tokens.css`
  （背景 `#F4F4F0` / 侧栏 `#232323` / 卡圆角 24px / 阴影 `0 10px 30px rgba(0,0,0,.04)`）。
- Tailwind CSS 3.4.16 与 ECharts 5.5.1 **全部本地化**至 `assets/vendor/`（版本与 SHA-256 见该目录 README），
  页面无任何 `https://` 资源引用，断网可完整演示（已在 Chrome `--host-resolver-rules` 断网下实测）。
- 交互：目标浏览器 Chips 可增删改任意版本号、三语种代码编辑器、一键修复 → 行级 Diff → 验证结果高亮。

---

## 3. 系统架构

### 3.1 数据流

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Browser · 前端（纯静态，无外链）                                           │
│  index.html + app.js   Tailwind 3.4.16 / ECharts 5.5.1（assets/vendor/）    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  POST /api/detect  { html, css, js, targets }
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L1 · 确定性静态检测（无网络、无 LLM）                                       │
│                                                                           │
│   tinycss2 ─┐                                                             │
│   esprima  ─┼──► Feature[]  ──►  BCD lookup  ──►  Issue[]                  │
│   BS4      ─┘      │                 ▲                 │                   │
│                    │                 │                 │                   │
│         （上下文优先的候选路径）  data/bcd_index.json    （severity / 证据链）│
│                                  13,196 条 · 6.0 MB                        │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  issues + 原始源码
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L2 · 解释与修复（LLM，受约束）                                             │
│                                                                           │
│   ① 白名单 Prompt ──► ② JSON Schema（enum 封死 id）                        │
│           ▲                        │                                      │
│           │                        ▼                                      │
│           │              ③ 后置校验（版本封闭 / 口径 / 语法门禁）             │
│           │                   │ 失败                │ 通过                  │
│           └── 带反馈重试 ◄────┘                    ▼                      │
│                                        explanations + fix_code_{css,js,html}│
│   ④ 重试耗尽 ──► fallback.py（模板化解释，degraded=true）                    │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  修复后代码
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  L3 · 沙箱验证                                                            │
│                                                                           │
│   re-detect(fixed) ──► resolved / remaining / new_issues                  │
│   jsdom（worker 线程 + 5s 硬超时）──► DOM 差异 + 执行期错误                  │
└───────────────────────────────────────────────────────────────────────────┘
```

### 3.2 接口清单

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/meta` | 默认目标集、浏览器版本候选、BCD 版本 |
| `POST` | `/api/detect` | **L1**：返回 `issues[]` / `unknown_features[]` / `stats` |
| `POST` | `/api/explain-fix` | **L2**：返回 `explanations[]`；永远 200，最差 `degraded=true` |
| `POST` | `/api/verify` | **L3**：返回 `resolved` / `remaining` / `new_issues` / `dom_diff` |
| `GET` | `/healthz` | 存活检查（不触碰索引） |

`/api/detect` 响应片段（真实输出）：

```json
{
  "status": "success",
  "issues": [
    {
      "id": "issue_11",
      "type": "js",
      "feature": "at()",
      "bcd_path": "javascript.builtins.Array.at",
      "line": 3, "column": 14,
      "unsupported_browsers": ["chrome 90", "safari 14.1", "firefox 88", "edge 90"],
      "severity": "high",
      "source_data": "MDN BCD: requires Chrome 92; requires Safari 15.4; requires Firefox 90; requires Edge 92 (javascript.builtins.Array.at)",
      "confidence": "context_unknown"
    }
  ],
  "unknown_features": [],
  "targets_resolved": [{ "browser": "safari", "version": "14.1", "label": "safari 14.1" }],
  "parse_warnings": [],
  "stats": { "total_lines": 17, "declarations_scanned": 8, "js_statements": 12, "html_elements": 3, "issues_total": 11 }
}
```

### 3.3 为什么必须是三层

- **可审计**：每条 issue 都带 `bcd_path` 与 `source_data`，能一路追到 MDN 原始数据；
- **可复现**：L1 没有任何随机性，两次调用逐字节相同；
- **可替换**：L2 换模型、换供应商甚至关掉，L1/L3 完全不受影响（无 Key 时全流程仍可演示）。

---

## 4. 快速开始

### 4.1 环境要求

| 依赖 | 版本要求 | 用途 |
|---|---|---|
| Python | ≥ 3.10（实测 3.13） | 后端 |
| Node.js | ≥ 18（实测 24） | BCD 数据转换、jsdom |
| npm | 随 Node | 安装 `@mdn/browser-compat-data` 与 `jsdom` |

### 4.2 安装依赖

```powershell
# 1) 仓库根目录创建虚拟环境
python -m venv .venv
.\.venv\Scripts\python -m pip install -r backend\requirements.txt

# 2) 安装 Node 侧依赖（含 BCD 数据包与 jsdom）
cd backend\node
npm install
cd ..\..
```

> 国内网络环境下若 npm 走不通，可显式指定官方源：
> `npm install --registry=https://registry.npmjs.org --proxy=null`

### 4.3 生成 BCD 索引（必做一次）

```powershell
cd backend
..\.venv\Scripts\python scripts\build_bcd_index.py
```

预期输出：

```
  css.properties        1862 条
  html.elements          698 条
  html.global_attributes  33 条
  javascript            1370 条
  api                   9233 条

写出 .../backend/data/bcd_index.json
  BCD 版本 : 5.6.11
  特性总数 : 13196
  文件体积 : 6.0 MB
```

索引是**可提交的构建产物**：生成后运行期完全离线。后续修改 `NAMESPACES` 或升级 BCD 时重跑即可。

### 4.4 配置 `.env`（可选，但推荐了解）

复制为 `backend/.env`：

```ini
# 不填也能跑：L2 会自动降级为模板解释，L1/L3 与前端全部照常工作
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxx
# 兼容国内代理 / 百炼等 OpenAI 兼容端点
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen-plus
```

> **换供应商时 `base_url` 与模型名必须成对修改**，否则会出现"key 无效"或"返回空内容"这类看似玄学的问题。
> 已实测可用的组合：
>
> | 供应商 | `OPENAI_BASE_URL` | `OPENAI_MODEL` |
> |---|---|---|
> | DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
> | 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
>
> 注意：DeepSeek 官方 API **没有** `deepseek-flash` 这个模型，调用它会返回 200 但内容为空；
> 官方模型名是 `deepseek-chat` / `deepseek-reasoner`。
> 该端点为不支持 `response_format: json_schema`（返回 400），系统会自动降级到 `json_object`，无需手工配置。

其余可调项在 `backend/app/core/config.py`（均可用 `COMPAT_*` 环境变量覆盖）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMPAT_LLM_TIMEOUT_SECONDS` | 60 | 单次模型调用超时 |
| `COMPAT_LLM_MAX_RETRIES` | 2 | 校验失败后的重试次数 |
| `COMPAT_LLM_MAX_OUTPUT_TOKENS` | 2000 | 输出上限（成本护栏） |
| `COMPAT_LLM_CONTEXT_LINES` | 20 | 喂给模型的上下文行数（命中行 ±N） |
| `COMPAT_JSDOM_TIMEOUT_SECONDS` | 5 | jsdom 执行硬超时 |

**重要**：不填 `OPENAI_API_KEY` 时，`/api/explain-fix` 返回 `degraded=true` 的模板化解释，
`/api/verify` 与前端页面完全可用。答辩现场即使没有网络与 Key，也能完整演示三层链路。

### 4.5 启动

```powershell
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

打开 <http://127.0.0.1:8000/> 即可（后端已把 `frontend/` 同源托管，无 CORS 配置成本）。

静态页面也可以脱离后端单独打开（`file://`），此时 `app.js` 会把 API 基址回落到 `http://127.0.0.1:8000`。

### 4.6 冒烟验证

```powershell
# 单元测试（133 项）
cd backend
..\.venv\Scripts\python -m pytest

# 前端假后端测试（50 项断言）
node ..\frontend\tests\ui-smoke.cjs

# 真后端端到端（需先在 8010 端口起服务，或设置 API_URL）
..\.venv\Scripts\python -m uvicorn app.main:app --port 8010
node ..\frontend\tests\e2e-live.cjs
```

命令行调用示例（Windows 下请用 `curl.exe`，避免 PowerShell 的 `curl` 别名）：

```powershell
curl.exe -s -X POST http://127.0.0.1:8000/api/detect `
  -H "Content-Type: application/json" `
  -d "{\"css\": \".box{display:flex;gap:10px}\", \"targets\": [\"safari 14.0\"]}"
```

### 4.7 常见问题

| 现象 | 原因与处理 |
|---|---|
| `BCD 索引不存在` → HTTP 503 | 未执行 §4.3，或 `data/bcd_index.json` 被清理 |
| `/api/verify` 的 `dom_diff.ran=false` | 未安装 node 或 `jsdom`；在 `backend/node` 执行 `npm install` |
| 页面图表明明有数据却空白 | 检查 `assets/vendor/echarts.min.js` 是否存在（离线依赖勿删） |
| npm 安装失败（`ECONNREFUSED`） | 本机 npm 代理配置失效，见 §4.2 的 `--proxy=null` 写法 |
| 提示"模型没有给出可应用的补丁"，但后端日志正常 | 浏览器缓存了旧版 `app.js`（接口已返回 `fix_edits`，旧前端只认 `fix_code_*`）。按 Ctrl+F5 硬刷新；服务端已对 HTML/JS/CSS 加 `Cache-Control: no-store`，且 `index.html` 引用带 `?v=` 版本号 |

---

## 5. 技术栈

| 层 | 技术 | 选型理由 |
|---|---|---|
| 后端框架 | **FastAPI** + Uvicorn | 原生 Pydantic 契约、自动 OpenAPI、异步友好 |
| 契约校验 | **Pydantic v2** | 请求模型 `extra="forbid"`，字段拼错立刻 422 |
| CSS 解析 | **tinycss2** | 纯 Python、保留 `source_line/column`，可遍历 at-rule |
| JS 解析 | **esprima** + 自写 tokenizer | AST 精确但止步 ES2018；tokenizer 兜底现代语法 |
| HTML 解析 | **BeautifulSoup4**（`html.parser`） | 唯一提供 `sourceline` / `sourcepos` 的内置解析器 |
| 兼容数据 | **@mdn/browser-compat-data 5.6.11** | 权威、可离线、结构可归一化 |
| 行为对比 | **jsdom 30**（worker_threads） | 无浏览器依赖即可执行 JS 并观察 DOM |
| 大模型 | **OpenAI Python SDK 3.x** | 支持 `base_url`，可接代理 / 百炼 |
| 前端 | **原生 JS + Tailwind CSS 3.4.16** | 无构建步骤，单文件可直接打开 |
| 图表 | **ECharts 5.5.1** | 本地化后离线可用，配色从 CSS 变量读取 |

---

## 6. 项目结构

```
rag/
├─ backend/
│  ├─ app/
│  │  ├─ main.py                 # FastAPI 装配、静态托管
│  │  ├─ api/{detect,explain,verify}.py
│  │  ├─ core/{config,schemas}.py
│  │  ├─ l1/                     # 确定性检测层
│  │  │  ├─ bcd.py               # 索引加载、版本比较、上下文判定
│  │  │  ├─ detector.py          # 编排：解析 → 查表 → 定级 → 稳定排序
│  │  │  ├─ severity.py          # 严重度矩阵 + 证据文本模板
│  │  │  ├─ targets.py           # 任意版本号解析与比较
│  │  │  └─ parsers/{css,js,html}_parser.py
│  │  ├─ l2/                     # 解释与修复层
│  │  │  ├─ prompts.py           # System Prompt / 口径 / JSON Schema
│  │  │  ├─ validation.py        # 四类后置校验
│  │  │  ├─ llm.py               # SDK 接入、重试、降级决策
│  │  │  └─ fallback.py          # 确定性模板
│  │  └─ l3/                     # 验证层
│  │     ├─ recheck.py           # 回灌比对 + jsdom 调度
│  │     ├─ jsdom_runner.mjs     # 主进程：批量执行 + 超时
│  │     └─ jsdom_worker.mjs     # worker：执行用户代码、DOM 快照
│  ├─ data/bcd_index.json        # 生成的索引（运行期唯一数据源）
│  ├─ scripts/build_bcd_index.py
│  ├─ node/package.json          # BCD 数据包 + jsdom
│  ├─ tests/                     # 133 项 pytest
│  ├─ requirements.txt
│  └─ pytest.ini
├─ frontend/
│  ├─ index.html · app.js
│  ├─ assets/tokens.css          # 设计 token（唯一色值来源）
│  ├─ assets/vendor/             # Tailwind / ECharts + 校验和清单
│  └─ tests/{ui-smoke,e2e-live}.cjs
├─ docs/00-plan.md               # 设计决策、实测数据、已知边界
├─ legacy/                       # 重构前的旧实现（保留追溯，不参与运行）
└─ README.md
```

---

## 7. 测试与实测数据

| 套件 | 规模 | 覆盖重点 |
|---|---|---|
| `backend/tests` | **133 项** | 版本比较边界、上下文优先、JS 兜底、HTML 内嵌行号、L2 幻觉拦截、L3 超时 |
| `frontend/tests/ui-smoke.cjs` | **50 项断言** | 初始化、渲染、四类错误、修复面板、Diff、标签切换（假后端） |
| `frontend/tests/e2e-live.cjs` | **24 项断言** | 真实 uvicorn 下的检测 → 修复 → 验证闭环 |

关键实测结论（均可复现）：

- **确定性**：同一请求连续两次，响应**逐字节相同**（794 B 样例）。
- **上下文优先**：`display:flex; gap` 在 `safari 12` / `14.0` 报错、`14.1` / `15.0` 通过
  —— 这正是"只查基础路径会过于乐观"的证据。
- **三维合流**：默认示例一次请求产出 **11 条问题（CSS 6 / HTML 4 / JS 1）**，
  等级 high 8 / medium 2 / low 1，浏览器分布 safari 11 / firefox 8 / chrome 5 / edge 5。
- **幻觉拦截**：三次不同幻觉分别被版本号、白名单、口径三道闸门拦下，最终降级为模板解释。
- **超时兜底**：`while (true) {}` 在 2 秒超时下约 3.4 秒返回结构化错误，主线程不受影响。
- **离线**：Chrome 以 `--host-resolver-rules="MAP * 127.0.0.1"` 断网渲染，
  Tailwind 样式、ECharts 图表、接口数据全部正常。

---

## 8. 已知边界

这一节刻意写得直白：系统的价值取决于它的结论可信，而可信的前提是知道哪里不可信。

1. **CSS 只到属性级，不做值级分析**。`position: sticky` 只会查 `css.properties.position`
   （Safari 3 起支持），因此在本项目的默认目标集下不会报错；
   `width: min-content`、`color: color-mix(...)` 同理不覆盖。
2. **前缀敏感条目偏严格**。`backdrop-filter` 在 BCD 中记录的是无前缀版本（Safari 18），
   实际 `-webkit-backdrop-filter` 在 Safari 9 即可用。实现如实反映 BCD，不猜前缀；
   需要放宽时通过 `data/overrides.json` 显式校正，并在 `source_data` 中标记 `[local override]`。
3. **CSS 布局上下文只在前规则块内推断**。`display` 写在另一个选择器里时，会按基础路径判定并标
   `confidence: "context_unknown"`。
4. **JS 不追踪类型**。`items.at(-1)` 靠方法名表推断（标 `context_unknown`）；
   只有 `Array.prototype.at` 这类显式写法才是 `certain`。**不要把它当作"必然报错"**。
5. **HTML 属性列号可能退化**：起始标签跨多行时，属性列号回落为标签列号。
6. **jsdom 不验证视觉**：没有布局引擎，Flex gap 之类的视觉缺失无法判定。
   `dom_diff.ran=true` 只代表"执行期行为与 DOM 结构已比对"。
7. **"已解决"可能只是"查不到"**：修复若把特性改写成无法识别的形态（例如把 `<dialog open>`
   换成 `<div open>`，而 `open` 不是 `div` 的属性），L1 只是不再识别它。
   系统会把这类情况单列为 `needs_review`（修复后的 `unknown_features` 与已解决 issue 的
   属性名求交），UI 显示"需人工复核"而不是"全部解决"。这是**启发式**而非证明：
   它能挡住"改个无关标签混过去"的修复，但仍建议人工确认。
8. **旧格式修复仍不可合并**：模型若仍返回整份文件（`fix_code_*`），同一语言多条时只能取一条；
   新格式 `fix_edits`（行区间替换）已支持全量合并，实测 11 条修复可一次应用。
9. **`new_issues` 只比对特性集合**：同一特性从 medium 升为 high 不会被单独报出。
10. **L2 口径检查是关键词级的**：措辞过于绕的合法解释可能被要求重试一次（最终仍会降级兜底）。

---

## 9. 未来工作

- **值级 CSS 检测**：在值 token 层面建立 `属性 + 值` 的候选路径（`position: sticky`、`min-content` 等）。
- **轻量类型推断**：结合 JSDoc / TypeScript 声明，把 `arr.at()` 这类推断从"方法名启发式"升级为"类型解析"。
- **多修复合并**：以 diff/patch 形式让模型输出片段级修复，从而支持同一语言多条修复的自动叠加。
- **视觉回归（可选）**：在 CI 中用 Playwright 做截图对比，作为 L3 的可选扩展（当前刻意不使用）。
- **索引增量更新**：BCD 版本升级时做差量重建，并对比新旧索引的判定差异。
- **流式输出与成本控制**：L2 改为流式响应，补充 prompt 缓存与预算上限。
- **工程化**：容器化、CI（pytest + 前端两套测试）、多语言界面。

---

## 10. 复现性与设计记录

- 所有判定逻辑都不含随机性；`issue.id` 由 `(type, line, column, bcd_path)` 稳定排序生成，便于前后比对。
- BCD 版本固定为 **5.6.11**；升级需重跑 `scripts/build_bcd_index.py` 并复跑测试。
- 详细的决策过程、踩坑记录与逐条实测数据见 [`docs/00-plan.md`](docs/00-plan.md)，
  其中最适合直接引用的三节：
  - §3.1 `gap` 上下文相关的实测修正表（含一处我们主动纠正的早期错误结论）；
  - §15.4 三次幻觉分别被三道闸门拦下的记录；
  - §16.2 Node 超时机制踩的两个坑（同步死循环 + `.CMD` 包装进程）。
- `legacy/` 目录保留了重构前的"正则 + RAG + 自由生成"实现，仅作追溯，不参与运行。

---

## 附录：诚实声明

本项目的核心主张是"兼容性结论必须来自可追溯的数据，而不是模型的表达"。
因此我们刻意公开了三类信息：**数据来源与版本**、**判定规则与阈值**、**未覆盖的场景**。

如果这份代码被用于真实项目，请把 `docs/00-plan.md` §11（已知边界）当作使用协议的组成部分。
