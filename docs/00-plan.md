# AI 浏览器兼容性助手 — 重构方案（第 0 步：设计评审）

> 状态：待你确认后进入第 1 步（后端骨架 + tinycss2 CSS 检测）。
> 本文件是后续所有步骤的唯一事实来源（single source of truth），改动请在此同步。

---

## 0. 现状盘点（已读取工作区）

| 文件 | 性质 | 处置 |
|---|---|---|
| `pipeline.py` | 旧方案：正则特征提取 → ChromaDB 检索 → LLM 出报告 | **停用**。且其 `from category_correction import ...`、`from alias_resolver import ...` 在本仓库中并不存在，当前已无法导入 |
| `init_kb.py` | 旧方案：把 `data/*.md` 灌入 ChromaDB（依赖 `DASHSCOPE_API_KEY`） | **停用** |

旧实现的根本问题正好是你点名要废掉的那一点：特征提取靠手写正则、结论由 LLM 自由生成，既不确定也不可复现（同一份代码两次跑出不同版本号）。新架构把"判定"和"解释"彻底切开：**L1 是唯一有权说"某浏览器某版本不支持"的组件，且它的答案只来自 MDN BCD 数据**。

处置建议：第 1 步开始前，把两个旧文件移入 `legacy/`（只移动、不删除、内容不改），保留追溯。**未经你确认我不会动它们。**

---

## 1. 目录结构（目标形态）

```
CompatAI/
├─ backend/
│  ├─ app/
│  │  ├─ main.py                 # FastAPI 实例、CORS、路由挂载、/healthz
│  │  ├─ api/
│  │  │  ├─ detect.py            # POST /api/detect       (L1)
│  │  │  ├─ explain.py           # POST /api/explain-fix  (L2)
│  │  │  └─ verify.py            # POST /api/verify       (L3)
│  │  ├─ core/
│  │  │  ├─ config.py            # 环境变量、模型名、超时
│  │  │  └─ schemas.py           # Pydantic 契约模型（唯一契约定义处）
│  │  ├─ l1/
│  │  │  ├─ detector.py          # 编排：targets → 三解析器 → 查 BCD → 组装 issues
│  │  │  ├─ targets.py           # "chrome 120" 解析、浏览器名校验、版本比较
│  │  │  ├─ bcd.py               # 索引加载 + 支持性查询（含 mirror/context/partial）
│  │  │  ├─ severity.py          # 严重等级确定性矩阵
│  │  │  ├─ features.py          # 统一 Feature 数据结构（type/name/path/line/col/context）
│  │  │  └─ parsers/
│  │  │     ├─ css_parser.py     # tinycss2
│  │  │     ├─ js_parser.py      # esprima AST + 自写 tokenizer 兜底
│  │  │     └─ html_parser.py    # BeautifulSoup4
│  │  ├─ l2/
│  │  │  ├─ llm.py               # OpenAI SDK 封装、重试、JSON 校验
│  │  │  ├─ prompts.py           # 防幻觉 System Prompt
│  │  │  └─ fallback.py          # 模型挂了/输出非法时的确定性降级
│  │  └─ l3/
│  │     ├─ recheck.py           # 修复码回灌 L1，做前后对比
│  │     └─ jsdom_runner.mjs     # Node 端沙箱执行 + DOM 快照
│  ├─ data/
│  │  ├─ bcd_index.json          # 生成物，勿手改（含上游版本号）
│  │  └─ overrides.json          # 人工校正表（见 §3.4）
│  ├─ scripts/
│  │  └─ build_bcd_index.py      # npm 包 data.json → 精简索引
│  ├─ node/package.json          # 仅 jsdom
│  └─ requirements.txt
├─ frontend/
│  ├─ index.html                 # 单页壳
│  ├─ app.js                     # 原生 JS，无框架
│  └─ tokens.css                 # 设计 token（CSS 变量）
└─ docs/00-plan.md               # ≤ 本文件
```

---

## 2. 数据流（一次完整请求）

```
[前端] --代码+targets--> POST /api/detect
   └─ L1: 解析(3 个解析器并行无关) → Feature[] → BCD 查询 → Issue[]  (确定性，无网络、无 LLM)
   ←-- {status, issues[], unknown_features[]} --
[前端] --(L1 response + 原始代码)--> POST /api/explain-fix
   └─ L2: Prompt 只允许针对 issues 里的 issue_id 解释 → JSON Schema 校验 → 不一致则重试/降级
   ←-- {status, explanations[]} --
[前端] --(修复码 + targets)--> POST /api/verify
   └─ L3: 修复码回灌 L1（问题数应下降）+ jsdom 前后 DOM 快照 diff
   ←-- {before, after, resolved[], remaining[], dom_diff} --
```

关键约束：**每一层的输入输出都必须能独立复现**。L1 两次调用必须字节级一致（issue 的 `id` 依赖稳定排序，见 §4.3）。

---

## 3. L1 的四个真实坑（这是本方案的核心增量）

我实际拉了 BCD 数据核对，以下不是理论风险，是会直接导致"你的示例代码检测不出问题"的坑。

### 3.1 CSS 的 `gap` 是上下文相关的（实测修正版）

`css/properties/gap.json` 的真实结构是：

```
css.properties.gap                 → safari: 10.1   ← 只查这个会**过于乐观**
css.properties.gap.flex_context    → safari: 14.1   ← Flex 布局里的真实门槛
css.properties.gap.grid_context    → safari: 12
css.properties.gap.multicol_context → safari: 14.1
```

**这里必须纠正我在评审阶段说错的一件事**：我最初写"示例 `safari 15.0` 应报错"是错的。
15.0 ≥ 14.1，所以 Safari 15.0 **支持** Flex gap，正确结果是 0 条 issue（已写成单测
`test_contract_example_at_safari_15_0_has_no_issue`）。契约 3.1 里那份"期望报一条"的示例数据本身不成立。

真正的陷阱方向恰好相反 —— **基础路径是过于乐观**：目标写 `safari 12` 时，
只查 `gap`（10.1）会得出"支持"，而 Flex 容器里的真实要求是 14.1，实际不成立。
实测确认（第 1 步交付时跑的真实服务）：

| 目标 | 只查基础路径 | 上下文优先（本实现） |
|---|---|---|
| safari 11 | 不支持 | 不支持 |
| safari 12 | ❌ 误判为支持 | ✅ 不支持（14.1） |
| safari 14.0 | ❌ 误判为支持 | ✅ 不支持（14.1） |
| safari 14.1 | 支持 | 支持 |
| safari 15.0 | 支持 | 支持 |

所以 CSS 解析不能只抽"属性名"，必须抽 **声明 + 所在规则上下文**，并按 `display` 值选子路径：

* `display: flex|inline-flex` → 尝试 `gap.flex_context`，存在则用它，否则回落 `gap`
* `display: grid|inline-grid` → `gap.grid_context`
* `column-count|column-width|columns` → `gap.multicol_context`
* 无从判断 → 用基础路径，并在 issue 上标注 `confidence: "context_unknown"`（契约里用可选字段承载，不破坏既有字段）

同一条规则里的多条声明要一起解析（`display` 可能写在 `gap` 之后）。第 1 步只做**同一规则块内**的上下文推断，跨选择器推断留到后续步骤 —— 这是有意识的取舍，先把确定性链路跑通。

### 3.2 `mirror` 与多形态 `support`

BCD 里 `"chrome_android": "mirror"` 这类占位必须解析成对应桌面浏览器的数据（BCD 规定的镜像关系：`chrome_android→chrome`、`firefox_android→firefox`、`opera_android→opera`、`safari_ios→safari`、`webview_android→chrome`、`webview_ios→safari`、`samsunginternet_android→chrome_android`）。`support` 节点的取值可能是：

* `{version_added: "84"}` 正常
* `[{version_added, alternative_name}, ...]` 多条目（旧前缀名/别名）
* `{version_added: null}` 从未支持
* `{version_added: true}` 支持但版本未知 / `false` 不支持
* `"preview"` / `"≤37"` 这类非数字版本串
* `partial_implementation: true` 部分支持
* `version_removed` 已被移除

索引构建阶段就统一归一化成 `{added, removed, partial, prefix}`，运行期查询只做纯比较，不碰原始 JSON 的形态差异。

### 3.3 版本比较必须自己写，不能用字符串比较

`"15.0" vs "15"`、`"10.10" vs "10.9"`、`"≤37"`、`"preview"` 都不能靠字符串或 `float()`。方案：`parse_version() → (int tuple, qualifier)`，`≤x` 视为 `(-1,)` 上界语义（保守判定为"支持"），`preview` 视为无穷大。第 1 步会把这张比较表写成单元测试（`chrome 120 / safari 15.0 / firefox 115` 三组固定用例）。

### 3.4 `overrides.json`：一处显式的人工校正出口

有些真实兼容性问题 BCD 结构表达不出来（例如老 Safari 的 `position: sticky` 需要 `-webkit-` 前缀才能生效、`flex-basis` 在 IE 的怪癖）。与其在代码里偷偷加 if，不如开一个**数据文件**：

```json
{ "css.properties.position.sticky": { "safari": { "version_added": "7", "requires_prefix": "-webkit-" } } }
```

加载时以 override 覆盖 BCD 值，并在 `source_data` 里标明来源 `MDN BCD + local override`。好处：可审查、可答辩、可回滚。**第 1 步会先留空文件 + 校验 schema，不预先塞假数据。**

---

## 4. 契约细节（在你给的 JSON 之上只做加法，不改既有字段）

### 4.1 `/api/detect` 请求校验

* `html` / `css` / `js` 三个字段均可省略（缺省 `""`），**至少一个非空**，否则 422。
* 单请求总字符上限 200 KB（防解析器 DoS）。
* `targets` 非空，每项必须匹配 `^(chrome|safari|firefox|edge|opera|ios_saf|chrome_android|firefox_android|edge|samsunginternet_android|webview_android|webview_ios) \d+(\.\d+){0,2}$`，且在 BCD `browsers/` 名单内；非法项返回 422 且**列出全部**非法项（不要只报第一个）。
* 浏览器别名归一：`ios_saf → safari_ios`、`and_chr → chrome_android` 等，避免前端两套命名。

### 4.2 issue 字段语义（把歧义定死）

| 字段 | 语义 | 说明 |
|---|---|---|
| `id` | `issue_1..n` | 由稳定排序生成，见 §4.3 |
| `type` | `css` \| `js` \| `html` | 与来源解析器一一对应 |
| `feature` | 人类可读名 | CSS 属性名 / JS 成员路径 `Array.prototype.at` / HTML 标签名 |
| `bcd_path` | **新增**：`css.properties.gap.flex_context` | L2 与 L3 都靠它精确定位，也便于你写论文取证 |
| `line` / `column` | 1-based | tinycss2/BS4/esprima 都是 1-based；JS 的 `column` esprima 给的是 0-based，需 +1 对齐（第 1 步就统一，别等联调时踩） |
| `unsupported_browsers` | 仅列`targets`中被判定不支持的 | 顺序 = 请求中 targets 的顺序（可复现） |
| `severity` | `high` \| `medium` \| `low` | 矩阵见 §4.4 |
| `source_data` | 人可读的 BCD 依据 | 模板固定，见 §4.4 |

### 4.3 稳定排序（决定 `id`，别小看）

排序键：`(type, line, column, bcd_path)`，全部升序。同一份输入两次调用得到完全相同的 `id` 序列 —— 这是 L3 做前后对比、以及 L2 结果回填的前提。

### 4.4 severity 与 source_data 的确定性生成

`ratio = len(unsupported_browsers) / len(targets)`

| 条件 | severity |
|---|---|
| `ratio == 1.0`（全部目标都不支持） | `high` |
| `ratio >= 0.5` | `high` |
| `0 < ratio < 0.5` | `medium`（你给的示例：1/3 → `medium` ✅） |
| 仅在部分实现/需前缀时命中 | `low` |

`source_data` 模板（不拼接 LLM 的话术，避免"看起来像模型编的"）：

* 不支持：`MDN BCD: requires Safari 14.1`（附 `(css.properties.gap.flex_context)`）
* 从未支持：`MDN BCD: not supported in Safari`
* 部分支持：`MDN BCD: Safari 15.0 partial implementation`
* 需前缀：`MDN BCD: Safari 15.0 requires -webkit- prefix (local override)`
* 版本未知：`MDN BCD: Safari support version unknown`

### 4.5 `unknown_features` 的判定

查不到 BCD 路径 → 进 `unknown_features`（**不猜、不降级成 medium issue**）。但要先过滤已知无害项：CSS 自定义属性 `--x`、厂商前缀属性、`@keyframes` 内的合法属性、SVG 的 presentation attributes 白名单等，避免列表噪音把真实缺口埋掉。

### 4.6 新增 `/api/verify`（第 4 步实现，契约先定）

```json
{ "status": "success",
  "before_count": 3, "after_count": 0,
  "resolved": ["issue_1"], "remaining": ["issue_2"],
  "dom_diff": { "changed": true, "added_nodes": 2, "removed_nodes": 0, "errors": [] } }
```

---

## 5. L2 防幻觉的四道闸门

你的硬性要求是"严禁 LLM 幻想版本号"，我用**结构约束 + 校验 + 降级**三层实现，而不是靠 prompt 里骂它：

1. **输入白名单**：prompt 里只出现 `issues` 数组。原始代码只给**命中行 ±20 行**的片段（省 token，且防止它跑去"顺手修"别的地方）。
2. **输出结构强制**：`response_format={"type":"json_schema", ...}`（若所选模型不支持 json_schema，退回 `json_object` + 严格解析）。Schema 里 `issue_id` 用 `enum` 直接枚举本批合法 id，`explanations` 的条数在 schema 里无法硬约束，靠第 3 步校验。
3. **后置校验（决定性）**：
   * `issue_id` 必须 ∈ 本批 id 集合，且**不重不漏**（漏的用 fallback 补齐）
   * `explain_text` 中若出现 `\b(Safari|Chrome|Firefox|Edge) \d+(\.\d+)*\b`，该版本串必须能在对应 issue 的 `source_data` 里找到，否则判"幻觉" → 重试一次 → 仍失败则降级
   * `fix_code_css` / `fix_code_js` / `fix_code_html` 若存在，必须能被对应解析器重新解析通过（语法门禁，这一步能拦掉大半"看起来对"的坏修复）
4. **确定性降级**：模型不可用时，`explain_text = source_data` 的模板化扩写，`fix_*` 置 `null` 并附 `needs_human: true`。宁可给"没有修复"，也不给编造的修复。

`temperature=0`，`seed` 固定（OpenAI 支持时），超时 60s，重试 2 次（指数退避）。

---

## 6. L3 的边界（说清楚，答辩时不被追问）

* **回灌检测**：把 `fix_code_css/js/html` 拼回源码再跑 L1，比对 `bcd_path` 集合。注意这是"同一特性还在不在"，**不是**"问题解决了" —— 例如把 `gap` 换成 `margin`，`gap` 消失即 huu resolved；但如果修复引入了新特性（`margin` 是 CSS2 老属性，安全），也要检查 `after` 里是否冒出新 issue，冒出来要报 `new_issues`。
* **jsdom**：Node 24 已就绪，用它跑原码与修复码，比较维度限定为：执行期异常、`console` 输出、最终 `document.body.innerHTML`、被调用的 API 名单。**jsdom 没有布局引擎和 CSSOM 计算能力**，所以它验证不了 "flex gap 缺失导致的视觉间距问题" —— 这一点在 UI 上要如实标注"JS 行为等价性已验证 / CSS 视觉未验证"，别让评委以为测了像素。
* 明确不用 Playwright（按你的要求），因此 L3 报告的口径 = "行为一致 + 静态检测归零"，不含截图比对。

---

## 7. 前端实现要点（第 2 步落地）

* Tailwind 用 **v3.4 CDN（play CDN）** 并在 `<script>tailwind.config</script>` 内 extend 设计 token；不选 v4 browser 版是因为 play CDN 的 config 内联写法更稳、类名行为与文档一致。
* Token 双份：`tokens.css` 里定义 CSS 变量（`:root{--bg:#F4F4F0;...}`），Tailwind `extend.colors` 里引用同名值。ECharts 的配色也从 CSS 变量读，保证图表和 UI 同源。
* 规范落实清单：`rounded-[24px]`（卡片）/`rounded-xl`（12px，内部）/`rounded-2xl`（16px，按钮）；`shadow-[0px_10px_30px_rgba(0,0,0,0.04)]`；侧栏 `#232323` 240px、激活项左侧 6px 蓝紫圆点 + 半透明白底；顶部搜索框 `rounded-full bg-white`；三张统计卡（`#D9E5D6` / `#F7E4A6` / `linear-gradient(135deg,#8E95F9,#A6B4FE)`）；图表 7:5 分栏；底部 issue 列表行 = 图标 + 描述 + 严重度 pill（high=`#F7E4A6` 系、medium=浅蓝紫系、low=`#D9E5D6` 系，同一套 token 派生，不新增颜色）。
* 状态机：`idle → detecting → explained → verified`，每个状态对应骨架屏/Spinner，报错用行内 banner（不用 `alert`）。
* 无障碍：所有 pill 不只靠颜色区分（加文字），`aria-live` 播报检测结果条数。

---

## 8. 分步交付与验收标准

| 步骤 | 产出 | 验收（可执行） |
|---|---|---|
| **1. 后端骨架 + CSS 检测** | `main.py`、`schemas.py`、`targets.py`、`bcd.py`、`severity.py`、`features.py`、`scripts/build_bcd_index.py`、`css_parser.py`、`detector.py`、`api/detect.py` + 45 项 pytest | ✅ **已完成**。索引 13,163 条 / 6.0 MB；两次调用逐字节相等（794 B）；`gap` 上下文命中 `flex_context`；14.0 报、14.1 不报的边界用例通过 |
| 2. 前端壳子 | `frontend/index.html`、`assets/tokens.css`、`tests/ui-smoke.cjs` | ✅ **已完成**。headless Chrome 渲染 + 像素采样核对 token；jsdom 30 项 DOM/交互断言全过 |
| 3. 联调 | `frontend/app.js`、`backend/app/main.py`（同源静态托管）、`/api/meta` 扩展 | ✅ **已完成**。34 项假后端断言 + 12 项真后端端到端断言全过；`safari 14.1→14.0` 的结论翻转在页面上复现 |
| **3.5 JS/HTML 解析器 + CDN 离线化** | `parsers/js_parser.py`、`parsers/html_parser.py`、`DetectStats` 扩展、`assets/vendor/` | ✅ **已完成**。88 项 pytest；一次请求同时产出 CSS 6 / HTML 4 / JS 1；断网截图验证通过 |
| **4. L2 + L3** | `app/l2/*`、`app/l3/*`、`/api/explain-fix`、`/api/verify`、前端修复面板 | ✅ **已完成**。L2 四道闸门（编造版本 / 未知 id / 口径错误全部拦下）；L3 回灌 + jsdom 对比（全量 133 项测试）；页面跑通"一键修复 → Diff → 验证"（已解决 11 / 新增 0） |

依赖（第 1 步装）：`fastapi uvicorn[standard] pydantic-settings tinycss2 esprima beautifulsoup4 lxml python-multipart httpx pytest`。第 4 步再加 `openai`。jsdom 单独在 `backend/node/` 里 `npm i jsdom`（Python 侧用 `subprocess` 调 `node`）。

---

## 9. 已拍板的 3 个决策（2026-10-22）

1. **旧文件**：`init_kb.py`、`pipeline.py` 已移入 `legacy/`，内容零改动，保留追溯。
2. **BCD 获取**：本地安装 + 转换脚本，运行期**零网络依赖**，版本锁定在 `backend/node/package.json`。
   已实测：npm 包只发布打包版 `data.json`（`package.json` 的 `main` 即它），无分模块 `data/` 目录，
   转换脚本读单个大 JSON 即可。
3. **目标浏览器**：核心 4 个（chrome / safari / firefox / edge）+ **任意合法版本号**（1~4 段数字）。
   后端不做版本白名单。用户最终选定默认集为历史兼容基准线：
   `chrome 90 / safari 14.1 / firefox 88 / edge 90`（常量在 `app/l1/targets.py: DEFAULT_TARGETS`，
   通过 `GET /api/meta` 下发给前端）。前端第 2 步用 **Chips 标签**呈现，允许自由增删改版本号。

---

## 10. 环境记录（实测，Windows）

* Python 3.13.0（`D:\python`）→ 项目依赖装在仓库内 `.venv`
* Node 24.9.0 / npm 11.19.0 → BCD 数据包放 `backend/node/`，`jsdom` 后续同处
* 你的 npm 默认指向已失效的本地代理 `127.0.0.1:12450`，安装时需
  `--registry=https://registry.npmjs.org --proxy=null`
* 沙箱注意：`tempfile.mkdtemp` 产生的 0700 目录会被拦，**pip 安装必须在提权模式下执行一次**；
  运行服务、跑测试、跑转换脚本都不需要提权

---

## 11. 已知边界（答辩时会被问，先写清楚）

诚实清单，逐条都可以用现有测试/实测复现：

1. **CSS 值级别的特性不检测**。`position: sticky` 只查 `css.properties.position`（Safari 3 起支持）
   → 默认目标集下报 0 条，但 `sticky` 值本身在老 Safari 上是经典坑。
   同理 `width: min-content`、`color: color-mix(...)` 这类值级特性都不覆盖。
   要覆盖需在值 token 层面建"属性 + 值"的候选路径，属于后续增量。
2. **前缀敏感的条目会偏向严格**。`backdrop-filter` 在索引里是 Safari 18（BCD 记的是无前缀版本），
   实际 `-webkit-backdrop-filter` 在 Safari 9 就能用。本实现如实反映 BCD，不猜前缀；
   需要放宽时通过 `data/overrides.json` 显式校正，并会在 `source_data` 里标 `[local override]`。
3. **CSS 上下文只在本规则块内推断**：`.a { display: flex } .b { gap: 10px }` 这种跨选择器场景
   会按基础路径判定并标 `context_unknown`。
4. **JS 不追踪类型**（第 3.5 步新增）。`items.at(-1)` 这种"对象类型未知"的调用靠方法名表推断，
   会标 `confidence: context_unknown`；`Array.prototype.at` 这类显式写法才是 `certain`。
   推论：**不要**把 `at()` 当成"必然报错"，它依赖目标版本与推断。
5. **HTML 属性列号可能退化**：属性写在同一行时列号精确；起始标签跨多行时退化为标签列号。
6. **未知特性不猜**：查不到 BCD 路径（含 JS 候选逐级截断后仍不存在的）进 `unknown_features`，
   不生成 issue。

### 默认目标集下的实测表现（`chrome 90 / safari 14.1 / firefox 88 / edge 90`）

| 片段 | 结果 | 说明 |
|---|---|---|
| `display: flex; gap: 10px` | 0 条 | Safari 14.1 恰好达标，**这就是边界正确性的活证据** |
| `display: grid; gap: 10px` | 0 条 | grid 门槛是 12 |
| `aspect-ratio: 16/9` | 1 条 high | safari 14.1、firefox 88 不达标 |
| `accent-color` | 1 条 high | 4 个目标全不达标 |
| `container-type` | 1 条 high | 需 Chrome 105 / Safari 16 / Firefox 110 |
| `backdrop-filter` | 1 条 high | 见边界 2（前缀问题） |
| `inset: 0` | 0 条 | Chrome 87 / Safari 14.1 / Firefox 66 均达标 |

**演示建议**：把 `accent-color` 或 `container-type` 放进默认演示代码，能稳定触发 high 级 issue；
想演示 `gap` 就用 `safari 14.0`（或前端把 chip 改成 14.0），这样能顺带展示"改一个版本号，结论翻转"。

---

## 12. 第 2 步交付：前端壳子（静态，像素级核对）

### 12.1 产出

| 文件 | 作用 |
|---|---|
| `frontend/index.html` | 单页壳：侧边栏、标题+胶囊搜索、警告 banner、目标 chips、三张统计卡、代码输入、7:5 图表区、问题列表 |
| `frontend/assets/tokens.css` | 设计 token（CSS 变量）+ 组件类（`.card` / `.card-accent` / `.chip` / `.pill` / `.skeleton`） |
| `frontend/tests/ui-smoke.cjs` | jsdom 冒烟测试：30 项结构与交互断言 |

数据不是编的：页面里的 6 条 issue 是 **真实 L1 对该段 CSS 的输出原样嵌入**（high 4 / medium 1 / low 1，
Chrome 3 / Safari 6 / Firefox 4 / Edge 3）。第 3 步只把 `DEMO` 换成 `fetch('/api/detect')` 的响应。

### 12.2 像素采样核对（headless Chrome 1440×2100 → 采样确定坐标）

| 目标 token | 规范值 | 采样值 | 结论 |
|---|---|---|---|
| 卡片背景 | `#FFFFFF` | `#FFFFFF` | ✅ 精确 |
| 侧边栏 | `#232323` | `#232323` | ✅ 精确 |
| 浅绿卡 | `#D9E5D6` | `#D9E5D6` | ✅ 精确 |
| 浅黄卡 | `#F7E4A6` | `#F7E4A6` | ✅ 精确 |
| 渐变卡（右下） | `#A6B4FE` | `#A1AEFD` | ✅ 渐变混合区，符合 |
| 全局背景 | `#F4F4F0` | `#F3F3EF` | ⚠️ 差 1/255，Chrome 色彩管理所致，源码写的就是 `#F4F4F0` |
| 激活项指示点 | `#8E95F9` | `#8990F0` | ✅ 6px 圆点，抗锯齿边缘取色 |

### 12.3 一个已知的可访问性问题（待你决定是否处理）

渐变卡上的白字对比度：`#8E95F9` 上白字约 **2.69:1**，`#A6B4FE` 侧约 **1.98:1**，低于 WCAG AA
大字标准（3:1）。这完全源于规范指定的 token 色值本身。

两个一行改法（都不新增色值）：

* 把卡片文字改成主文字色 `#111111`（对比度约 7:1）；
* 或在渐变上叠 12% 黑：`background-image: linear-gradient(rgba(0,0,0,.12), rgba(0,0,0,.12)), linear-gradient(135deg, var(--accent-from), var(--accent-to))`（约 3.0:1）。

当前按"严格 1:1 还原 token"处理，保持白字未改（**已确认：保持现状**）。

---

## 13. 第 3 步交付：前后端联调

### 13.1 运行方式（两行命令）

```powershell
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://127.0.0.1:8000/
```

`main.py` 已把 `frontend/` 挂到 `/`（`StaticFiles(html=True)`），因此前端与接口**同源**，不涉及 CORS。
路由顺序上 `/api/*` 先注册、静态挂载在后，实测 `POST /api/detect` 不会被静态目录吞掉。

用 `file://` 直接打开也能用：`app.js` 检测到非 http 协议时把 API 基址回落到 `http://127.0.0.1:8000`。

### 13.2 交付内容

| 文件 | 变化 |
|---|---|
| `frontend/app.js` | 新增。全部逻辑：meta 初始化、chips、三个编辑器 tab、fetch 检测、渲染、四类错误处理、Ctrl/⌘+Enter |
| `frontend/index.html` | 内联脚本抽空，改为 `<script src="app.js" defer>`；banner 变为动态容器；新增 `#unknown-features` 与 `#datalists` |
| `backend/app/main.py` | 新增静态托管；`/api/meta` 增加 `bcd_version` 与 `releases`（各浏览器版本候选，供输入框 datalist） |
| `frontend/tests/ui-smoke.cjs` | 重写为"假后端"测试：34 项断言，覆盖成功 / 422 / 503 / 网络不可达 / 前端校验 |
| `frontend/tests/e2e-live.cjs` | 新增"真后端"端到端：jsdom 跑真实 `app.js`，fetch 打到真实 uvicorn |

### 13.3 测试矩阵

| 场景 | 断言 | 结果 |
|---|---|---|
| 初始化 | chips 来自 `/api/meta` 的 `default_targets`，datalist 来自 `releases` | ✅ |
| 首次自动检测 | 15 行 / 6 条问题 / high 4 · medium 1 · low 1 | ✅ |
| 真实 partial | `parse_warnings` 进入顶部 banner，文案含 `partial` | ✅ |
| 未识别特性 | `unknown_features` 列出但不计入问题数 | ✅ |
| 版本翻转 | 页面上把 `safari 14.1` 改成 `14.0` → 出现 `gap` 问题 | ✅ |
| 422 | 展示后端返回的每条 `detail[].msg` | ✅ |
| 503 | 提示索引缺失并给出 `build_bcd_index.py` | ✅ |
| 网络不可达 | banner 提示启动 uvicorn + 用 8000 端口打开 | ✅ |
| 前端校验 | 目标为空 / 版本号非法时**不发请求** | ✅ |
| 幂等 | 连点两次检测，结果条数不变 | ✅ |

### 13.4 过程中修掉的两个真实缺陷

1. **初始化覆盖示例代码**：`setTab()` 会把当前 textarea 内容写回 `state.code`，而 bootstrap 先调 `setTab` 时 textarea 还是空的 → `SAMPLE.css` 被空串覆盖 → 首次自动检测发出**空请求**，直接吃 422。
   修法：先灌默认代码再 `setTab`；并加了一条断言（"代码区预填示例 CSS"）钉死。
2. **页面模板的 off-by-one**（第 2 步遗留）：已随 `_line_count` 的尾随换行修正闭环，现在页面与接口行数一致（15 行）。

---

## 14. 第 3.5 步：JS / HTML 解析器 + CDN 离线化

### 14.1 JS 解析：为什么要两套机制

esprima 4.0.1 只到 ES2018 左右，实测 `const x = obj?.name;` 直接抛
`Error: Line 1: Unexpected token .`。如果只用 AST，**一份用了可选链的文件会整份失去检测**，
而 esprima 失败的代价不止是漏掉语法特性 —— 后面的 `arr.at(-1)` 也一起没了。

所以分成两条并行通道：

| 通道 | 何时运行 | 负责什么 |
|---|---|---|
| tokenizer（自写，~200 行） | **总是** | 语法级特性：`?.` `??` `??=` `&&=` `||=` `**` `...` `await` `async` 私有字段 `static {}` `1_000` `123n` `import()` 可选 catch 绑定 |
| esprima AST | 能解析时 | API 级特性：调用/构造的成员链（`Array.prototype.at`、`document.querySelector`、`new IntersectionObserver`） |
| token 链扫描 | esprima 失败时 | 兜底还原 `a.b.c(` 形状，保证 API 检测不陪葬 |

tokenizer 要点：跳过字符串/注释/正则字面量（正则与除号靠"前一个 token 是否允许正则"判定），
模板字面量里的 `${...}` 会按代码继续切词，因此 `` `${a?.b}` `` 里的可选链也能抓到。

API 路径策略：
* 根名必须在 BCD 里存在（`javascript.builtins.<X>` 或 `api.<X>` 的 X），否则视为局部变量跳过
  —— 这样 `obj.someMethod()` 不会污染 `unknown_features`；
* `window.` / `self.` / `globalThis.` 前缀剥掉；`document` → `api.Document`；
* 候选路径**从具体到宽泛逐级截断**：`console.log` 会落到 `api.console`，不误报也不制造未知噪音；
* 对象类型未知的调用（`items.at(-1)`）走 9 条方法名表兜底，并标 `confidence: context_unknown`。

### 14.2 HTML 解析：必须用 `html.parser`

实测：`BeautifulSoup(html, "lxml")` 的 `sourceline` / `sourcepos` **全是 None**；
`html.parser` 两者都有。没有行号就无法把 issue 指回源码，所以放弃 lxml。

产出：
* 标签 → `html.elements.<tag>`；属性 → `html.elements.<tag>.<attr>`，查不到回落 `html.global_attributes.<attr>`
* 自定义元素（含 `-`）、`data-*`、`aria-*`、`class`/`id`/`role` 不扫，避免噪音
* `<style>` / `<script>`（无 `src`）内容交给 CSS/JS 解析器，**行号按内嵌位置平移**
  （用 `source.find(content, cursor)` 求偏移，多段同内容也不会错位）
* `style="display:flex;…"` 走 `scan_declarations()`（裸声明列表，不能用 `scan_css`）

### 14.3 契约的加法（仍然只加不改）

`DetectStats` 新增 `js_statements`、`html_elements`，前端统计卡副文案据此显示
"声明 / 语句 / 标签" 三组数字。`status: partial` 的语义保留：真出现解析异常时仍会降级并给 `parse_warnings`。

### 14.4 CDN 离线化

| 文件 | 版本 | 大小 | 说明 |
|---|---|---|---|
| `frontend/assets/vendor/tailwind.js` | 3.4.16 | 451,131 B | Play CDN 完整脚本，本地加载后照常 JIT 编译 |
| `frontend/assets/vendor/echarts.min.js` | 5.5.1 | 1,030,855 B | 图表库 |
| Google Fonts | — | — | **已移除**，改系统字体栈（Inter 优先，缺失自动回落） |

版本与 SHA-256 记录在 `frontend/assets/vendor/README.md`，含重新下载的命令。
`index.html` / `app.js` / `tokens.css` 中已无任何 `https://` 资源引用（`app.js` 里出现的
`http://127.0.0.1:8000` 是 API 基址兜底，不是外部资源）。

**离线验证方法**（可复现）：Chrome 加 `--host-resolver-rules="MAP * 127.0.0.1"`，
把所有域名解析到本机（等于切断外部网络），再打开 `http://127.0.0.1:8000/`。
实测结果：Tailwind 样式、ECharts 柱状图/环形图、接口数据全部正常渲染，无任何外链请求。

### 14.5 测试

| 套件 | 覆盖 | 结果 |
|---|---|---|
| `backend/tests/test_detect_js.py` | 可选链/空值合并/幂/展开/BigInt/数字分隔符/私有字段/静态块；esprima 失败时的兜底；字符串·注释·正则不误报；token 位置 | ✅ |
| `backend/tests/test_detect_html.py` | 标签、元素属性、全局属性回落、自定义元素跳过、内嵌 style/script 行号平移、inline style | ✅ |
| `backend/tests/test_detect_mixed.py` | 一次请求三合一：CSS 6 / HTML 4 / JS 1，共 11 条；等级 8/2/1；浏览器分布；确定性；stats | ✅ |
| `backend` 全量 pytest | 88 项 | ✅ 88 passed |
| `frontend/tests/ui-smoke.cjs` | 34 项（假后端） | ✅ |
| `frontend/tests/e2e-live.cjs` | 12 项（真后端） | ✅ |

### 14.6 演示用的默认示例（前端预填，与接口输出一致）

```css
.card { display:flex; gap:12px; aspect-ratio:16/9; accent-color:#8E95F9;
        container-type:inline-size; backdrop-filter:blur(6px);
        color-interpolation:sRGB; scroll-behavior:smooth; }
```

```js
const name = user?.profile?.name;
const total = count ?? 0;
const last = items.at(-1);
```

```html
<dialog class="modal" open>
  <img src="cover.png" loading="lazy" decoding="async">
  <div inert>内容</div>
</dialog>
```

默认目标集下 → **11 条问题：CSS 6 / HTML 4 / JS 1**，等级 high 8 · medium 2 · low 1。
若把 Safari 改回 `14.0`，`gap` 会额外出现（14.1 边界），可用于现场演示"改一行目标、结论翻转"。

---

## 15. 第 4 步（上）：L2 解释与修复

### 15.1 文件与职责

| 文件 | 职责 |
|---|---|
| `app/l2/prompts.py` | System Prompt、口径分类（`classify`）、按 issue 组装的上下文、严格 JSON Schema |
| `app/l2/validation.py` | 后置校验：白名单 id / 版本封闭 / 解释口径 / 修复代码语法门禁 / JSON 抽取 |
| `app/l2/llm.py` | OpenAI SDK 接入（支持 `base_url`）、结构化输出降级链、重试、降级决策 |
| `app/l2/fallback.py` | 确定性模板化解释（`needs_human=True`，不给修复代码） |
| `app/api/explain.py` | `POST /api/explain-fix`，**永远 200**，最差 `degraded=True` |

### 15.2 四道闸门

1. **输入白名单**：提示词只含 L1 命中的 issue，附命中行 ±20 行上下文（超长自动折半）。
   代码里不含整份文件，模型没有"自由发挥"的素材。
2. **结构化输出**：优先 `response_format=json_schema`（`issue_id` 用 enum 枚举本批 id），
   模型不支持时依次降级 `json_object` → 纯文本 + `extract_json` 容错。
3. **后置校验（决定性）**：
   * `issue_id` 不重不漏，且不得出现白名单外的 id；
   * 解释里出现的版本号必须能在**该条** `source_data` / 目标列表里找到（依据写 15.4，写 15 就被拦）；
   * 口径检查：语法级必须谈"解析/语法/SyntaxError/脚本"；API 级必须谈"undefined/不存在/TypeError"；
     CSS 谈"忽略/回退/样式"；HTML 谈"未知/忽略/标签属性"；
   * 修复代码门禁：CSS 交给 tinycss2 解析、JS 括号配平（复用 tokenizer）、HTML 标签闭合扫描。
4. **确定性降级**：任一门未过 → 带着"错在哪"重试（默认 2 次）→ 仍失败则全量走 `fallback.py`，
   `degraded=True`、每条 `needs_human=True`、`fix_*` 为 `None`。

### 15.3 配置

`backend/.env`：

```ini
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   # 可选，兼容代理/百炼
OPENAI_MODEL=qwen-plus                                              # 可选
```

`llm_timeout_seconds=60`、`llm_max_retries=2`、`llm_max_output_tokens=2000`、`llm_context_lines=20`
均在 `app/core/config.py`，可用 `COMPAT_*` 环境变量覆盖。

### 15.4 实测：三次不同的幻觉分别被三道闸门拦下

| 尝试 | 模型输出 | 拦截原因 |
|---|---|---|
| 1 | CSS 解释写 "Safari 15"（依据是 15.4） | `issue_1 的解释里出现了依据中不存在的版本号：15（仅允许 12, 15.4）` |
| 2 | 多给了一条 `issue_7` | `issue_id 'issue_7' 不在白名单里（可用：issue_1, issue_2）` |
| 3 | 把语法级问题写成"没有这个方法，运行时 undefined" | `issue_2 属于 syntax 类问题，但解释没有提到 解析/语法/SyntaxError/脚本` |

三次都失败 → `degraded=True`，改用模板：
`safari 12 不支持该语法（可选链 ?.）：解析阶段直接抛 SyntaxError，整个脚本不会执行。依据：MDN BCD: requires Safari 13.1 (...)`

### 15.5 测试

| 套件 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_l2_validation.py` | 22 | 合法通过、乱序重排、Markdown 围栏、未知/重复/缺失 id、编造版本（含裸版本）、版本按 issue 隔离、双向口径错误、CSS/JS/HTML 修复门禁、JSON 抽取 |
| `tests/test_l2_llm.py` | 11 | 合法采用、schema enum、口径分别注入、幻觉重试后成功、持续幻觉降级、调用异常降级、无 Key 降级、空 issues 不调用、HTTP 200/422 |
| 全量 | **121 passed** | 含 L1 的 88 项 |

### 15.6 已知边界

* 口径检查是**关键词级**的，措辞过于绕的合法解释可能被要求重试（重试成本 +1，最终仍会降级兜底）；
* 版本检查是**逐 issue 隔离**的：CSS 问题里提 JS 问题的版本号会被拦，这是刻意的严格；
* L2 不做验证，也不声称修好了 —— 是否真的解决由 L3 回灌判定；
* 无流式输出（一次请求一次响应），演示时按钮需等待；
* 模型选择与计费不在本项目范围内，`max_tokens` 仅作成本护栏。

---

## 16. 第 4 步（下）：L3 回灌验证 + 前端一键修复

### 16.1 文件

| 文件 | 职责 |
|---|---|
| `app/l3/jsdom_runner.mjs` | 主进程：读 stdin、批量执行、**超时控制**、结构化输出 |
| `app/l3/jsdom_worker.mjs` | 真正跑用户代码的 worker 线程（含 jsdom 快照） |
| `app/l3/recheck.py` | 回灌 L1 比对 + jsdom 调用 + DOM 差异计算 |
| `app/api/verify.py` | `POST /api/verify` |
| `frontend/app.js` | 一键修复流程、LCS 行差异、验证结果渲染 |

### 16.2 超时：踩了两个坑才写对

用户提醒的"Node 卡死拖死主线程"确实存在，而且比预想的更麻烦：

1. **`setTimeout` 在死循环里根本不会触发**：`while (true) {}` 同步阻塞事件循环，
   任何"自己定时自杀"的方案都不成立。
2. **`.CMD` 包装层**：本机 `node` 解析到 DSH 的 `node.cmd`，`Popen.kill()` 只杀掉 cmd.exe，
   真正的 node.exe 变成孤儿并继续占着管道 → `communicate()` **永久阻塞**。

最终方案是两层：

| 层 | 机制 | 作用 |
|---|---|---|
| node 内部 | 用户代码跑在 **worker_threads**，主线程计时到点调 `worker.terminate()` | V8 层面截断，同步死循环也能打断 |
| Python 侧 | `Popen` + `taskkill /F /T` 杀进程树 + 有界等待 | 兜底：node 主进程本身卡死时 |

实测：`while (true) {}` 在 2 秒超时下约 3.4 秒返回（含 node 启动），
`dom_diff.ran=false` + `status=partial` + 明确错误文案，不会拖住请求。

### 16.3 new_issues 的判定

问题身份用 `(type, bcd_path, feature)` 三元组，**不含行号与 id**：

* 行号会因修复整体位移，用行号比对会把"同一个问题"误判成"已解决 + 新问题"；
* `resolved` / `remaining` 是**修复前** issues 的 id；`new_issues` 是**修复后**新出现的 issue 对象。

实测用例：把 `accent-color` 换成 `container-type` → `resolved=["issue_1"]`、`new_issues=[container-type]`，
这正是"修一个坏一个"的典型场景。另有 `test_line_shift_does_not_count_as_resolved` 钉住行号问题。

### 16.4 DOM 对比的口径（必须说清楚）

`dom_diff.ran=true` 只代表"JS 执行期行为与 DOM 结构已比对"：

* 比较项：执行期异常、`document.body.innerHTML` 归一化后是否变化、各标签数量增减；
* jsdom **没有布局引擎**，所以 Flex gap 之类的视觉问题它验证不了；
* 没有 JS/HTML 时 `ran=false`，此时**不等于通过**；`status=partial` 表示验证未完成（超时）。

### 16.5 前端一键修复

流程：`一键修复` → `POST /api/explain-fix` → 合并修复 → `POST /api/verify` → 展开「修复与验证」卡片。

* **合并规则**（写死在 `composeFixed`）：同一语言只有一条修复就直接用；多条时取**行号最小**的那条，
  其余在 banner 里标为"未合并"。这是刻意保守——程序无法安全地把两份"整文件级"修复叠加。
* **Diff**：自实现行级 LCS，`+` 行浅绿底、`−` 行浅黄底，不引第三方 diff 库（保持离线）。
* **验证结果高亮**：状态 pill 优先级为 `引入新问题 > 验证超时 > 全部解决 > 部分解决`；
  摘要行给出 `已解决 / 未解决 / 新增问题 / DOM 变化`，并把 `new_issues` 与 jsdom 执行期错误逐条列出。
* **无 API Key 也能演示**：提供"填入示例修复"按钮（手写的等价旧写法），
  直接走 L3 回灌。实测默认示例：**已解决 11 / 未解决 0 / 新增问题 0 / DOM 有变化 +2/−1**。

### 16.6 测试

| 套件 | 项数 | 结果 |
|---|---|---|
| `tests/test_l3_recheck.py` | 12 | ✅ 含 resolved/remaining/new_issues、行号位移、DOM 增删、执行期异常、**超时**、HTTP |
| 全量 pytest | **133** | ✅ |
| `frontend/tests/ui-smoke.cjs` | 50 项断言 | ✅ 含修复面板、Diff、标签切换、新问题高亮 |
| `frontend/tests/e2e-live.cjs` | 23 项断言 | ✅ 真后端跑通"降级 → 示例修复 → 已验证" |

### 16.7 已知边界

* 同一语言的多条修复不会自动合并（见 §16.5），需要人工或用大模型重出一版；
* jsdom 单次执行约 2.6 秒（node 启动占大头），验证阶段页面会有 3–5 秒等待；
* jsdom 不做布局/像素比对，视觉类问题只能靠 L1 静态判定；
* `new_issues` 只对比"特性集合"，不判断严重度变化（同一特性从 medium 变 high 不会被单独报出来）。

---

## 17. 修复形态升级：从「整份文件」到「行区间替换」

### 17.1 起因：8 条修复无法落地

接入真实模型后，默认示例（11 条问题）拿到了 10 条修复代码，但只有 2 条能应用：

```
同一语言有 8 条修复建议未合并
已应用行号最小的一条；未合并：issue_2, issue_3, issue_4, issue_5, issue_6, issue_8, issue_9, issue_10
```

根因不是程序，而是**修复的载体形态**：模型返回的 `fix_code_css` 是"整份文件级"片段
（每个 issue 都给出一份完整的 `.card { ... }`），文本拼接会产生重复规则、后写覆盖前者，
所以当时只能取一条。

### 17.2 方案：模型只给"改哪几行"

新增 `FixEdit`：

```json
{"language": "css", "start_line": 4, "end_line": 4, "replacement": "  position: relative;"}
```

* 行号 1-based、含两端；`replacement` 为空串表示删除该行区间；
* 不同 issue 的区间**不得重叠**；合成时按行号**倒序**应用，避免位移；
* `fix_code_*`（整份文件）保留为兼容旧输出，两者同时存在时以 `fix_edits` 为准。

四类校验（都在 `validation.py`，确定性代码）：

| 检查 | 拦截的具体问题 |
|---|---|
| 范围 | 行号越界（`css 只有 10 行，收到 12–12`）、目标语言源码为空 |
| 格式 | `replacement` 里带了行号前缀（` 12 | ...`，模型常见偷懒） |
| 冲突 | 两条修复的区间重叠 |
| 合成门禁 | 全部编辑应用回源码后，CSS 可被 tinycss2 解析、JS 括号配平、HTML 标签闭合 |

### 17.3 真机实测（DeepSeek `deepseek-chat`）

| 指标 | 结果 |
|---|---|
| 耗时 | 13.6 s（含一次校验失败重试） |
| 第 1 次尝试 | ❌ `issue_7 与 issue_8 的编辑区间在 html 上重叠：1–1 与 1–1` |
| 第 2 次尝试 | ✅ 通过校验（反馈重试生效） |
| 产出编辑 | 11 条（CSS 6 / JS 1 / HTML 4），全部合并 |
| 验证结果 | **修复前 11 → 修复后 0；resolved 11；new_issues 0** |
| `needs_human` | 1 条（issue_8 未给编辑，如实标注） |

合并后的 CSS 是干净结果：`aspect-ratio` 换成 `position:relative + padding-top` 占位，
其余 5 个不支持的属性被整行删除。

### 17.4 判定口径修正：resolved 不再与「查不到」混为一谈（已实现）

`issue_8`（`<dialog open>`）被算作 resolved，但它并没有被真修好：
模型的编辑把 `<dialog open>` 改成了 `<div class="modal" open>`，而 `open` 不是 `div` 的属性、
BCD 里也没有全局 `open`，于是 L1 **查不到**它。

实测确认：

```
issues: []
unknown_features: ['<div open>']
```

也就是说，"resolved" 里混着两类结果：**真的修好了** 与 **变得无法识别了**。
修复后的 `unknown_features` 目前只在 `/api/detect` 暴露，`/api/verify` 没有回传，
所以界面上两者长得一样。

**已修正**：`VerifyResponse` 增加 `unknown_features`（修复后查不到的特性）与
`needs_review`（虽已 resolved、但特性名与未识别项重合的 issue id）。

判定细节（`recheck._needs_review`）：把未识别项拆成词元，与"已解决 issue 的身份词元"求交。
HTML 条目的身份词元取**属性名而非标签名**——否则 `<div inert>` 与 `<div open>`
会因为共享 `div` 而互相误伤（这是第一版实现里真实出现过的假阳性，已用
`test_generic_tag_name_does_not_cause_false_positive` 钉住）。

用 §17.3 那次真实模型的 11 条编辑复验：

```
before/after: 11 -> 0
resolved: 11
needs_review: ['issue_8']          ← 只有<dialog open> 那条被标待复核
unknown_features: ['<div open>']
new_issues: []
```

前端表现：摘要多一枚"待复核 N"，状态 pill 优先级变为
`引入新问题 > 验证超时 > 需人工复核 > 全部解决 > 部分解决`，
并在问题区列出未识别特性与解释，提示"可能只是查不到，请人工确认"。

### 17.5 变更文件与测试

| 文件 | 变化 |
|---|---|
| `app/core/schemas.py` | 新增 `FixEdit`；`IssueExplanation.fix_edits` |
| `app/l2/edits.py` | 新增：`apply_edits` / `count_lines` |
| `app/l2/prompts.py` | 提示词改为行区间格式；Schema 只允许 `fix_edits` |
| `app/l2/validation.py` | 新增 edits 四类校验 + 合成门禁 |
| `app/l2/llm.py` | `validate(..., code=)`；`needs_human` 同时看 edits |
| `frontend/app.js` | `applyEdits` + 新的 `composeFixed`（优先行级修复） |
| `tests/test_l2_edits.py` | 新增 12 项：合并归零、越界、行号前缀、重叠、合成语法 |

全量测试：**146 passed**（原 134 + 12）。


### 17.6 A 方案的测试与变更

| 文件 | 变化 |
|---|---|
| `app/core/schemas.py` | `VerifyResponse` 增加 `unknown_features` / `needs_review` |
| `app/l3/recheck.py` | 计算两类结果；`_tokens` / `_issue_tokens` / `_needs_review` |
| `frontend/app.js` | 摘要"待复核 N"、状态"需人工复核"、问题区列出未识别特性 |
| `tests/test_l3_recheck.py` | +4 项：真·消失判定、干净修复无标记、通用标签名假阳性回归、HTTP 字段 |

全量测试：**150 passed**。

### 17.7 前端缓存导致的一次"假故障"

改完 B/A 之后，页面上出现提示：`模型没有给出可直接应用的修复`。但抓原始响应后确认后端完全正常：

```
degraded=false, total_edits=11
raw_preview: {"explanations": [{"issue_id": "issue_1", ..., "fix_edits": [{...}]}]}
```

原因是**浏览器仍在跑缓存的旧 `app.js`**：旧版 `composeFixed` 只读 `fix_code_*`，
而接口已经改成返回 `fix_edits`，于是"拿到了补丁却一个都用不上"。

修复三件：

1. `main.py` 增加中间件：HTML / 自家 JS / CSS 一律 `Cache-Control: no-store`；
   `assets/vendor/*`（Tailwind / ECharts，1.5 MB 且极少变）保留默认缓存；
2. `index.html` 的资源引用加版本号（`app.js?v=3`、`tokens.css?v=3`），
   即便换成别的静态服务器也能失效缓存；
3. 文案细化：区分"模板兜底"与"模型确实没给补丁"（`fix_edits` 为空）。

验证（8011 端口实测响应头）：

```
/                         200  Cache-Control='no-store, max-age=0'
/app.js?v=3               200  Cache-Control='no-store, max-age=0'
/assets/tokens.css?v=3    200  Cache-Control='no-store, max-age=0'
/assets/vendor/tailwind.js 200 Cache-Control=None        ← 保持缓存
```

教训：前后端契约变更时，**前端产物必须可失效**，否则"代码是对的、界面是错的"这种故障
最难排查——它看起来像模型没干活。

---

## 18. 页面拆分 + 特性查询（第 5 步）

### 18.1 起因

用户反馈两条：**"有些页面还没做"**、**"主页面搜索不能用"**。核实后确认：

* 侧边栏 `Code Analysis` / `Settings` 是 `href="#"` 占位，点了只是跳回顶部；
* 搜索框只在**已检测出的 issue 列表**内做子串过滤，搜不到源码与中文解释，
  且无结果时的空状态文案会误导成"没有兼容性问题"。

### 18.2 页面拆分

采用 hash 路由（无构建、无依赖）：`#/dashboard` / `#/analysis` / `#/settings`。

| 页面 | 内容 |
|---|---|
| Dashboard | 三张统计卡、柱状图/环形图、最近问题 Top 5、"查看全部 →" |
| Code Analysis | 目标浏览器 Chips、代码检测（三语种）、问题列表、修复与验证面板 |
| Settings | 运行配置只读：BCD 版本、默认目标集、模型/端点/Key 状态、L2 超时重试、jsdom 状态、上限 |

`/api/meta` 相应扩展了 `llm` / `l3` / `features` 三组**只读**信息；
**密钥永不回传**，页面只显示"是否已配置"。

### 18.3 特性查询（替代原来的列表过滤）

新增 `POST /api/lookup`，与 L1 共用同一份 BCD 与同一套版本比较，**不经过任何模型**：

| 输入 | 解析结果 |
|---|---|
| `accent-color` | `css.properties.accent-color` |
| `Array.at` / `array.at` | `javascript.builtins.Array.at` |
| `img loading` / `img.loading` | `html.elements.img.loading` |
| `inert` | 元素属性查不到 → 回落 `html.global_attributes.inert` |
| `accent-colour`（拼错） | 未命中，给出 difflib 纠错后的候选路径 |

前端：Enter 立即查询、输入 400ms 防抖、Esc 关闭；每个目标给 `✓ / ~ / ✗` 与 BCD 依据；
未命中时把候选路径做成可点击的胶囊，点一下用去掉前缀的名字重查。

### 18.4 压力测试与由此发现的问题

| 输入 | 耗时 | 结果 |
|---|---|---|
| 500 行 CSS（13.6 KB） | 928 ms（含首次索引加载） | 500 行 / 500 声明 / 252 条问题 |
| 2000 行 CSS（55.6 KB） | 152 ms | 2000 行 / **问题截断到 500 条** |
| 228,000 字符 | 立即拒绝 | 超出 `max_source_chars=200000` 上限 |

压力测试暴露一个诚实性缺口：**问题数被截断时没有任何提示**，用户会以为"就这 500 条"。
已修复：截断时追加 `parse_warnings`（"结果已截断，请缩小范围"）并置 `status=partial`，
前端会因此出现顶部黄色 banner。新增测试 `test_truncation_is_reported_explicitly` 钉住。

### 18.5 测试

| 套件 | 项数 | 结果 |
|---|---|---|
| `backend/tests`（新增 `test_lookup.py` 11 项 + 截断 1 项） | **162** | ✅ |
| `frontend/tests/ui-smoke.cjs`（新增查表 6 项、路由/设置 10 项、Top5 2 项） | **71** | ✅ |
| `frontend/tests/e2e-live.cjs`（新增真后端查表 3 项） | **25** | ✅ |

e2e 同时把写死的 `sleep` 全部换成**轮询等待**——L3 验证实测需要 5.4 秒，原来固定等 5 秒会假失败。

### 18.6 仍未做

* 导出（JSON/CSV）与检测历史（前端本地存最近若干次）；
* 窄屏（<1024px）真机验收、键盘 Tab 顺序验收；
* 真实框架代码（Vue SFC / React JSX / 压缩 CSS）的兼容性表现。
