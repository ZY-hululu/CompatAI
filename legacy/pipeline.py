"""
浏览器兼容性诊断助手 — 完整链路
代码输入 → 特征提取 → 检索知识库 → LLM生成诊断报告
"""
from openai import OpenAI, Stream
import chromadb
import os
from dotenv import load_dotenv
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from unicodedata import category
import json
import re
from category_correction import correct_features_categories
from alias_resolver import resolve as alias_resolve

load_dotenv()

# ====================== 配置 ======================

API_KEY = os.getenv("DASHSCOPE_API_KEY")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_MODEL = "qwen3.7-text-embedding"
CHAT_MODEL = "qwen3.8-max"
CHROMA_DB_PATH = "./chroma_db"
COLLECTION_NAME = "browser_compat"
DATA_DIR = "./data"

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# import os
# os.environ["DASHSCOPE_API_KEY"] = ""

# ==================== 别名标准化（后处理）======================
def normalize_properties(issues: list, verbose: bool = False) -> list:
    """
    将 LLM 输出的 property 字段标准化为知识库中的标准名称。
    解决问题：LLM 可能返回"箭头函数"、"backdropFilter"、"display: flex"等变体，
    统一映射为 KB 中的标准名（arrow-function、backdrop-filter、flex 等）。
    """
    if not isinstance(issues, list):
        return issues
    normalized = []
    for issue in issues:
        if isinstance(issue, dict) and "property" in issue:
            issue_copy = issue.copy()
            original = issue_copy["property"]
            resolved = alias_resolve(original)
            if resolved != original:
                if verbose:
                    print(f"   🔗 别名标准化: '{original}' → '{resolved}'")
                issue_copy["property"] = resolved
            normalized.append(issue_copy)
        else:
            normalized.append(issue)
    return normalized


# ====================== 系统级提示词（固定不变）======================
# 定义角色、输出格式、判断标准、诊断规则。每次调用都带，基本不改。

SYSTEM_PROMPT = """你是一名资深前端兼容性分析专家。
你的任务是分析用户提供的前端代码，结合知识库检索结果，识别在主流浏览器中不支持或行为不一致的语法、API或样式，并输出结构化的诊断报告。

## 严重程度判断标准
- 🔴高：主流开发常用 + 多个浏览器不支持（如Promise、const/let、箭头函数、flex）
- 🟡中：部分场景使用 + 少数浏览器不支持（如Array.from、Object.assign、CSS变量）
- 🟢低：很少使用 + 只有老旧浏览器不支持（如冷门CSS属性、已废弃标签）

判断维度：
1. 使用频率：这个语法/属性在现代开发中常用吗？
2. 影响范围：多少浏览器不支持？
3. 浏览器普及度：不支持的浏览器还有人用吗？

## 输出格式
必须且仅输出合法的 JSON 数组格式，严禁包含任何 Markdown 标记（如 ```json）、前缀或额外解释性文字。
若未发现问题，必须输出空数组 []。

[
  {
    "category": "问题分类，只能是 'JS'、'CSS' 或 'HTML'",
     "property": "必须与知识库检索结果中的「特性名(property)」字段完全一致，英文连字符命名，禁止中文、空格、下划线、驼峰（如 'optional-chaining', 'css-has-selector', 'array-at'）",
    "severity": "严重程度，只能是 'high'、'medium' 或 'low'",
    "affected_browser": "受影响的浏览器及版本号（如 'IE11', 'Safari < 15.4'）",
    "problem": "具体问题描述，说明为什么在目标浏览器中会报错或失效",
    "fix": "具体的修复方案或降级建议"
  }
]

## 诊断规则
1. 对每个兼容性问题，输出：严重程度、属性名、问题描述、影响浏览器、修复方案
2. 如果代码没有兼容性问题，输出空数组 []
3. 修复方案要具体，给出代码或工具名
4. 按严重程度从高到低排列
5. **ES6 语法判断规则**:
   - 如果 target_browsers 包含 IE（或未指定 target_browsers），检测到 ES6 语法必须标记为高危
   - 如果 target_browsers 只包含现代浏览器（Chrome/Firefox/Safari/Edge），且这些浏览器都支持该 ES6 语法，可以输出空数组 []
6. **【重要】合并规则**：如果多个问题属于同一技术特性或API（如display: grid、grid-template-columns、gap都属于CSS Grid布局），必须合并为一个问题，严禁拆分成多个独立问题
7. **【最高优先级】版本号必须严格逐字引用自知识库原文**：
   - 每个问题的 affected_browser 必须与知识库中对应条目的版本号完全一致，禁止推测或改写。
   - 严禁混淆不同条目的版本号（如把箭头函数的 Safari<10 错误用在 const 上，const 的正确版本是 Safari<11）。
   - 如果无法确认某条目的版本号，在 problem 字段注明"详见知识库原文"，不要编造。
8. **【重要】禁止脑补**：只诊断代码中实际存在的特性，不要提及代码中没有使用的特性
9. **【最高优先级】绝对禁止报告 target_browsers 之外的浏览器问题。如果 target_browsers 不包含 IE，禁止输出任何关于 IE 的信息，违者输出空数组 []**
10. **【反讨好约束】如果知识库信息不足以判断某个兼容性问题，必须输出空数组 []，不要猜测或编造答案**
11. **【重要】CSS 属性分类规则**：通过 JS DOM API 访问的样式属性（如 el.style.containerType、el.style.backdropFilter、el.style.aspectRatio）本质上是 CSS 属性，category 必须标为 "CSS"，不能标为 "JS"。判断方法：如果属性名是驼峰式且对应某个 CSS 属性（containerType → container-type），则归类为 CSS。
12. **【最高优先级】property 必须逐字抄录知识库检索结果中的「特性名(property)」字段**：
    - 报告里每个问题的 property，必须与引用的那条知识库结果给出的 property 完全一致（英文连字符命名，如 optional-chaining、css-has-selector、array-at）。
    - 严禁自己翻译成中文（如"CSS自定义属性"）、严禁输出代码里出现的普通单词（如 main、content）、严禁使用驼峰或下划线变体。
    - 一条知识库结果对应一个 property，不要合并、不要新造名字。检索结果中没有对应特性时，按规则 10 输出空数组 []，不要硬编。
13. **【重要】JS 特性识别规则**：
    - property 必须是代码中真正引发兼容性问题的具体语法或 API，不是声明关键字。
    - 看到 const x = a?.b，property 是 optional-chaining，不是 const；看到 const y = arr.at(0)，property 是 array-at，不是 const；看到 str.replaceAll("a","b")，property 是 string-replaceall。
    - const/let 本身只有在 target_browsers 包含 IE（或未指定浏览器）时才作为独立特性报告；目标浏览器全是现代浏览器时不要报 const。
14. **【重要】当用户选择的某个浏览器未出现在某条知识库检索结果的 affected_browser 字段中时**，按以下优先级判断：
    a) 先阅读该条知识库的内容：如果内容里提到了该浏览器的支持版本，按内容里的信息输出；如果内容里明确说该浏览器不支持，告知用户不支持。
    b) 如果内容里也没提到该浏览器：
       - 对于 IE：如果该特性是现代 CSS/JS/HTML 特性（affected_browser 只含 Chrome/Safari/Firefox/Edge 的现代版本号），IE 几乎肯定不支持，请明确告知用户"此特性为现代特性，IE 不支持且已停止维护，无版本可解决"。不要因为 affected_browser 里没有 IE 的版本号就跳过不报。
       - 对于其他浏览器（如 Opera、Samsung Internet）：如实告知"知识库未收录该浏览器对此特性的兼容性数据"，不要编造版本号。
    注意：不要在没有依据的情况下默认某个浏览器完全支持。
"""


# ====================== 任务级提示词（每次变化）======================
# 定义这次做什么：目标浏览器、代码、特征、知识库结果。每次调用时动态拼接。

def build_task_prompt(code, features, knowledge, target_browsers=None):
    """构建任务级提示词"""

    # 目标浏览器信息
    if target_browsers:
        browser_info = f"用户只关心以下浏览器的兼容性：{', '.join(target_browsers)}。只报告这些浏览器中存在的兼容性问题，不要报告其他浏览器的问题。如果这些浏览器都支持代码中使用的特性，必须输出空数组 []。"
    else:
        browser_info = "用户未指定目标浏览器，请检查所有主流浏览器的兼容性。"

    # 特征信息
    features_text = ', '.join(features) if features else '未检测到明显兼容性特征'

    # 知识库信息
    knowledge_text = knowledge if knowledge else '未检索到相关信息'

    # 拼接任务级提示词
    task_prompt = f"""{browser_info}

## 用户代码
```
输入代码: {code}
```

## 检测到的兼容性特征
{features_text}

## 知识库检索结果
{knowledge_text}
"""

    return task_prompt


# ==================== 第1步：提取特征 ====================
def extract_features(code):
    """从代码中扫描可能的兼容性特征（关键词匹配）"""
    features = []

    # JS 语法特征
    js_syntax = {
        r"\blet\s": "let块级声明",
        r"\bconst\s": "const块级声明",
        r"=>": "箭头函数",
        r"\?\.\w": "optional chaining可选链",
        r"\?\?": "空值合并运算符",
        r"\.\.\.": "展开运算符",
        r"async\s": "async异步函数",
        r"await\s": "await异步等待",
        r"\bclass\s+\w": "class类语法",
        r"#[a-zA-Z]\w*\s*[=;(]": "私有类字段",
        r"`[^`]*`": "模板字符串",
        r"\bfor\s*\([^;]*\s+of\s+": "for-of循环",
        r"function\s*\*": "Generator生成器函数",
        r"yield\s": "yield关键字",
        r"\bSymbol\(": "Symbol类型",
        r"\bBigInt\(": "BigInt大整数",
        r"\bimport\(": "动态import()",
    }

    # JS API 特征
    js_api = {
        r"fetch\(": "fetch API",
        r"Promise": "Promise异步对象",
        r"\.finally\(": "Promise.finally",
        r"\.at\(": "Array.at方法",
        r"IntersectionObserver": "IntersectionObserver",
        r"new Map\(": "Map对象",
        r"new Set\(": "Set对象",
        r"Object\.assign": "Object.assign",
        r"Array\.from": "Array.from",
        r"Object\.entries": "Object.entries",
        r"Object\.values": "Object.values",
        r"Array\.isArray": "Array.isArray",
        r"\.includes\(": "includes方法",
        r"\.startsWith\(": "String.startsWith",
        r"\.endsWith\(": "String.endsWith",
        r"\.padStart\(": "String.padStart",
        r"\.padEnd\(": "String.padEnd",
        r"\.flat\(": "Array.flat",
        r"\.flatMap\(": "Array.flatMap",
        r"\.findIndex\(": "Array.findIndex",
        r"\.find\(": "Array.find",
        r"\.keys\(\)": "Array.keys",
        r"\.values\(\)": "Array.values",
        r"Object\.getOwnPropertyDescriptors": "Object.getOwnPropertyDescriptors",
        r"Object\.freeze": "Object.freeze",
        r"new WeakMap\(": "WeakMap",
        r"new WeakSet\(": "WeakSet",
        r"new Proxy\(": "Proxy代理",
        r"Reflect\.": "Reflect对象",
        r"globalThis": "globalThis",
        r"structuredClone\(": "structuredClone",
        r"ResizeObserver": "ResizeObserver",
        r"MutationObserver": "MutationObserver",
        r"BroadcastChannel": "BroadcastChannel",
        r"navigator\.clipboard": "Clipboard API",
        r"AbortController": "AbortController",
        r"URLSearchParams": "URLSearchParams",
        r"TextEncoder": "TextEncoder",
        r"TextDecoder": "TextDecoder",
        r"crypto\.randomUUID": "crypto.randomUUID",
        r"requestAnimationFrame": "requestAnimationFrame",
        r"new Worker\(": "Web Worker",
        r"navigator\.serviceWorker": "Service Worker",
        r"localStorage": "localStorage",
        r"sessionStorage": "sessionStorage",
        r"indexedDB": "IndexedDB",
        r"WebSocket": "WebSocket",
        r"new EventSource\(": "SSE服务端推送",
        r"history\.pushState": "history.pushState",
        r"new CustomEvent\(": "CustomEvent",
        r"new FormData\(": "FormData",
        r"ReadableStream": "ReadableStream",
        r"WritableStream": "WritableStream",
        r"Notification\.requestPermission": "Notification API",
        r"navigator\.geolocation": "Geolocation API",
        r"WebAnimation|\.animate\(": "Web Animation API",
    }

    # CSS 特征
    css_features = {
        r"display:\s*flex": "flex弹性布局",
        r"display:\s*grid": "grid网格布局",
        r"gap:": "gap间距属性",
        r"position:\s*sticky": "sticky粘性定位",
        r"backdrop-filter:": "backdrop-filter背景滤镜",
        r"aspect-ratio:": "aspect-ratio宽高比",
        r"-webkit-line-clamp": "line-clamp文本截断",
        r"place-content:": "place-content对齐",
        r"place-items:": "place-items对齐",
        r"container-type:": "container容器查询",
        r"--[\w-]+\s*:": "CSS自定义属性(变量)",
        r"clamp\(": "clamp函数",
        r"\bmin\(": "min函数",
        r"\bmax\(": "max函数",
        r"calc\(": "calc计算函数",
        r"scroll-snap-type:": "scroll-snap滚动吸附",
        r"scroll-behavior:": "scroll-behavior平滑滚动",
        r"overscroll-behavior:": "overscroll-behavior",
        r"mask-image:|mask:": "CSS Mask遮罩",
        r"clip-path:": "clip-path裁剪",
        r"object-fit:": "object-fit对象适配",
        r"object-position:": "object-position对象定位",
        r"mix-blend-mode:": "mix-blend-mode混合模式",
        r"(?<!backdrop-)filter:": "CSS filter滤镜",
        r"transform:": "CSS transform变换",
        r"transition:": "CSS transition过渡",
        r"@keyframes": "CSS animation动画",
        r"will-change:": "will-change性能优化",
        r"contain:": "CSS contain包含",
        r"content-visibility:": "content-visibility内容可见性",
        r"scroll-margin:": "scroll-margin滚动边距",
        r"scroll-padding:": "scroll-padding滚动内距",
        r":is\(": "is选择器",
        r":where\(": "where选择器",
        r":has\(": "has选择器",
        r"@layer": "@layer层级规则",
        r"@container": "@container容器查询规则",
        r"text-decoration-skip-ink:": "text-decoration-skip-ink",
        r"overflow-wrap:": "overflow-wrap换行",
        r"word-break:": "word-break断词",
        r"white-space:\s*break-spaces": "break-spaces空白处理",
        r"hyphens:": "hyphens连字符",
        r"writing-mode:": "writing-mode书写模式",
        r"margin-inline:|padding-inline:|margin-block:|padding-block:": "CSS逻辑属性",
        r"grid-template-": "grid-template网格模板",
        r"flex-wrap:": "flex-wrap换行",
        r"align-content:": "align-content对齐",
        r"justify-items:": "justify-items对齐",
        r"minmax\(": "minmax函数",
        r"repeat\(": "repeat函数",
        r"fr\b": "fr单位",
    }

    # HTML 元素特征
    html_elements = {
        # 语义标签
        r"<header[\s>]": "header语义标签",
        r"<footer[\s>]": "footer语义标签",
        r"<nav[\s>]": "nav语义标签",
        r"<article[\s>]": "article标签",
        r"<section[\s>]": "section标签",
        r"<aside[\s>]": "aside标签",
        r"<main[\s>]": "main标签",
        r"<figure[\s>]": "figure标签",
        r"<figcaption[\s>]": "figcaption标签",
        r"<mark[\s>]": "mark高亮标签",
        r"<time[\s>]": "time标签",
        # 交互组件
        r"<details[\s>]": "details展开折叠",
        r"<summary[\s>]": "summary标签",
        r"<dialog[\s>]": "dialog弹窗标签",
        r"<template[\s>]": "template模板标签",
        # 媒体标签
        r"<video[\s>]": "video标签",
        r"<audio[\s>]": "audio标签",
        r"<source[\s>]": "source媒体源",
        r"<track[\s>]": "track字幕标签",
        r"<picture[\s>]": "picture响应式图片",
        r"<embed[\s>]": "embed插件标签",
        # 表单元素
        r"<datalist[\s>]": "datalist自动补全",
        r"<meter[\s>]": "meter度量条",
        r"<progress[\s>]": "progress进度条",
        r'<input[^>]*type\s*=\s*["\']date["\']': "input-date类型",
        r'<input[^>]*type\s*=\s*["\']time["\']': "input-time类型",
        r'<input[^>]*type\s*=\s*["\']email["\']': "input-email类型",
        r'<input[^>]*type\s*=\s*["\']range["\']': "input-range类型",
        r'<input[^>]*type\s*=\s*["\']color["\']': "input-color类型",
        r'<input[^>]*type\s*=\s*["\']number["\']': "input-number类型",
        r'<input[^>]*type\s*=\s*["\']search["\']': "input-search类型",
        r'<input[^>]*type\s*=\s*["\']url["\']': "input-url类型",
        r'<input[^>]*type\s*=\s*["\']tel["\']': "input-tel类型",
        # iframe
        r"<iframe[\s>]": "iframe标签",
    }

    # HTML 属性特征
    html_attributes = {
        r"\bautoplay\b": "autoplay属性",
        r"\bposter\s*=": "poster封面图属性",
        r"\bsandbox\s*=": "sandbox沙箱属性",
        r"\bsrcset\s*=": "srcset响应式属性",
        r'\btype\s*=\s*["\']image/webp["\']': "WebP格式",
    }

    import re
    for pattern, name in {**js_syntax, **js_api, **css_features, **html_elements, **html_attributes}.items():
        if re.search(pattern, code, re.IGNORECASE):
            features.append(name)

    return features


# ==================== 第2步：生成检索问题 ====================
def features_to_queries(features):
    """把特征列表转成自然语言检索问题"""
    query_map = {
        "let块级声明": "let声明在旧浏览器中不支持怎么办",
        "const块级声明": "const声明在旧浏览器中不支持怎么办",
        "箭头函数": "箭头函数在IE中不支持怎么办",
        "optional chaining可选链": "optional chaining可选链在旧浏览器中报语法错误",
        "空值合并运算符": "空值合并运算符??在旧浏览器中不支持",
        "展开运算符": "展开运算符在旧浏览器中不支持",
        "async异步函数": "async函数在旧浏览器中不支持",
        "await异步等待": "await在旧浏览器中不支持",
        "fetch API": "fetch API在IE11中不支持怎么办",
        "Promise异步对象": "Promise在IE11中不支持怎么办",
        "Promise.finally": "Promise.finally在旧浏览器中不支持",
        "Array.at方法": "Array.at方法在旧浏览器中不支持",
        "IntersectionObserver": "IntersectionObserver在IE11中不支持",
        "Map对象": "Map在IE11中不支持",
        "Set对象": "Set在IE11中不支持",
        "Object.assign": "Object.assign在IE中不支持",
        "Array.from": "Array.from在IE中不支持",
        "Object.entries": "Object.entries在IE中不支持",
        "Object.values": "Object.values在IE中不支持",
        "flex弹性布局": "flex布局在IE11中有兼容性问题",
        "grid网格布局": "CSS grid网格布局在IE11中不支持",
        "gap间距属性": "gap间距属性在Safari中不生效",
        "sticky粘性定位": "sticky粘性定位在IE中不支持",
        "backdrop-filter背景滤镜": "backdrop-filter在Chrome中需要前缀",
        "aspect-ratio宽高比": "aspect-ratio在Safari中不支持",
        "line-clamp文本截断": "line-clamp在Firefox中不支持",
        "place-content对齐": "place-content在旧浏览器中不支持",
        "place-items对齐": "place-items在旧浏览器中不支持",
        "container容器查询": "container容器查询浏览器支持情况",
    }
    return [query_map.get(f, f + "的浏览器兼容性问题") for f in features]


# 检索问题改写功能（7/14）
def generate_queries(code, target_browsers=None):
    """用LLM生成检索query（替代老的特征映射方法）"""

    # 构建目标浏览器信息
    if target_browsers:
        target_info = f"用户只关心以下浏览器的兼容性：{', '.join(target_browsers)}。生成的检索问题应该只针对这些浏览器。"
    else:
        target_info = "用户未指定目标浏览器，默认检查所有主流浏览器（Chrome、Safari、Firefox、Edge、IE）的兼容性问题。"

    prompt = f"""{target_info}

请根据以下代码生成3-5个不同角度的检索问题，用于在知识库中检索相关的兼容性问题。

代码：
{code}

要求：
1. 每个问题关注一个具体的兼容性特性（如某个CSS属性、JS API、语法特性等）
2. 问题要具体，包含特性名称和目标浏览器
3. 用自然语言描述，不要用代码片段

只返回问题列表，每行一个，不要编号，不要其他内容。"""

    response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )

    queries = [q.strip() for q in response.choices[0].message.content.strip().split('\n') if q.strip()]
    return queries

# ==================== 第3步：检索知识库 ====================
def get_embeddings(texts):
    """调用百炼API获取文本向量"""
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    return [item.embedding for item in resp.data]


def retrieve(queries, category=None, top_k=2):
    client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    collection = client.get_collection(COLLECTION_NAME)

    # 一次性批量获取所有embedding（原来N次API调用 → 现在1次）
    all_embeddings = get_embeddings(queries)

    query_params = {
        "query_embeddings": all_embeddings,
        "n_results": top_k,
        "include": ["documents", "metadatas", "distances"]
    }
    if category and category != "":
        query_params["where"] = {"category": category}

    results = collection.query(**query_params)

    # 整理结果（注意：批量查询返回的是二维列表）
    all_results = []
    seen = set()
    for docs, metas, dists in zip(results["documents"], results["metadatas"], results["distances"]):
        for doc, meta, dist in zip(docs, metas, dists):
            if doc not in seen:
                seen.add(doc)
                all_results.append({
                    "content": doc,
                    "metadata": meta,
                    "distance": dist
                })

    all_results.sort(key=lambda x: x["distance"])
    return all_results


# ==================== 第4步：LLM生成诊断报告 ====================
# def filter_by_target_browsers(issues, target_browsers):
#     """过滤掉不在 target_browsers 中的问题"""
#     if not target_browsers:
#         return issues  # 没指定 target_browsers，返回全部
#
#     # 标准化浏览器名称（小写，去除版本号）
#     target_lower = [''.join(c for c in tb.lower() if c.isalpha()) for tb in target_browsers]
#
#     filtered_issues = []
#     for issue in issues:
#         affected = issue.get('affected_browser', '').lower()
#
#         # 提取 affected_browser 中的浏览器名称（去除版本号）
#         # 例如 "IE11" -> "ie", "Safari < 15.4" -> "safari", "Chrome 90+" -> "chrome"
#         browsers_in_issue = []
#         for part in affected.replace(',', ' ').replace(';', ' ').split():
#             # 提取纯字母部分
#             browser_name = ''.join(c for c in part if c.isalpha()).lower()
#             if browser_name:
#                 browsers_in_issue.append(browser_name)
#
#         # 检查是否有任何浏览器不在 target_browsers 中
#         should_filter = False
#         for browser in browsers_in_issue:
#             # 如果问题涉及的浏览器不在目标列表中，就过滤掉
#             if browser not in target_lower:
#                 should_filter = True
#                 break
#
#         if not should_filter:
#             filtered_issues.append(issue)
#
#     return filtered_issues


def generate_report(code, features, retrieved, target_browsers=None):
    """调用通义千问生成结构化诊断报告（使用分层提示词）"""

    # 拼接检索到的知识库内容（含完整元数据，供模型抄录 property/版本号）
    knowledge = "\n".join([
        f"- 分类: {r['metadata'].get('category', '')} | "
        f"特性名(property): {r['metadata'].get('property', '')} | "
        f"严重程度: {r['metadata'].get('severity', '')}\n"
        f"  影响浏览器: {r['metadata'].get('affected_browser', '')}\n"
        f"  内容: {r['content']}"
        for r in retrieved
    ])

    # 分层拼接：系统提示词 + 任务提示词
    task_prompt = build_task_prompt(code, features, knowledge, target_browsers)
    full_prompt = SYSTEM_PROMPT + "\n\n" + task_prompt

    response: ChatCompletion | Stream[ChatCompletionChunk] = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": full_prompt}],
        temperature=0.1
    )
    raw_output = response.choices[0].message.content
    issues = parse_llm_json_output(raw_output)

    # 不再使用 filter_by_target_browsers 硬过滤（AND 逻辑会误杀：
    # affected_browser 中只要混入一个用户未选的浏览器，整条结果被扔掉，
    # 连用户已选浏览器的问题也一起丢失）。
    # 浏览器筛选交给 SYSTEM_PROMPT 规则 9 和规则 14 由模型完成。
    return issues

def parse_llm_json_output(text: str):
    """从 LLM 输出中提取合法的 issues 列表"""
    try:
        # 尝试直接解析（大模型直接输出纯数组）
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    try:
        # 去除可能存在的 Markdown 标记 (```json ... ```)
        json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(1))
            if isinstance(data, list):
                return data
    except Exception:
        pass

    try:
        start = text.find('[')
        end = text.rfind(']')
        if start != -1 and end > start:
            json_str = text[start:end + 1]
            data = json.loads(json_str)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []  # 解析彻底失败返回空列表


# ==================== 第5步：完整链路 ====================
def diagnose(code, target_browsers=None, verbose=False):
    """完整诊断链路：正则扫描 + LLM生成query → 合并检索 → 报告"""

    # 路径1：正则全量扫描（毫秒级，保底不漏基础特征）
    if verbose:
        print("🔍 第1步：正则全量扫描...")
    regex_features = extract_features(code)
    if verbose:
        print(f"   正则检测到 {len(regex_features)} 个特征: {regex_features}")

    # 路径2：LLM 生成检索问题（补充冷门特征）
    if verbose:
        print("📚 第2步：LLM 生成检索问题...")
    llm_queries = generate_queries(code, target_browsers)
    if verbose:
        print(f"   LLM 生成了 {len(llm_queries)} 个检索问题:")
    if verbose:
        for i, q in enumerate(llm_queries, 1):
            print(f"   {i}. {q}")

    # 合并：正则特征转 query + LLM query，去重
    regex_queries = features_to_queries(regex_features) if regex_features else []
    all_queries = list(dict.fromkeys(regex_queries + llm_queries))  # 去重且保序
    if verbose:
        print(f"\n📚 第3步：合并检索（共 {len(all_queries)} 个query，正则{len(regex_queries)}个 + LLM{len(llm_queries)}个）...")

    results = retrieve(all_queries)
    if verbose:
        print(f"   检索到 {len(results)} 条相关知识")

    # print("\n🔍 检索到的知识库原文：")
    # for i, r in enumerate(results):
    #     print(f"  [{i+1}] {r['content']}")

    if not results:
        return "未检索到相关兼容性信息。"

    if verbose:
        print("🤖 第4步：生成诊断报告...")
    report = generate_report(code, regex_features, results, target_browsers)

    # ===== 第4.5步：分类修正（后处理兜底）=====
    # 解决问题：LLM将el.style.containerType等CSS属性误分类为JS
    # 原理：命中CSS_VIA_JS集合的property，强制将category改为CSS
    if isinstance(report, list):
        report = correct_features_categories(report)

    # ===== 第4.6步：属性名标准化（别名→知识库标准名）=====
    # 解决问题：LLM可能返回"箭头函数"、"backdropFilter"等变体名
    # 通过 aliases.json 统一映射为 KB 标准 property（arrow-function、backdrop-filter 等）
    if isinstance(report, list):
        if verbose:
            print("\n🔗 第4.6步：属性名标准化...")
        report = normalize_properties(report, verbose=verbose)

    # return json.dumps(report, ensure_ascii=False, indent=2)
    return report  # 直接返回 list

# ==================== 测试 ====================
if __name__ == "__main__":
    # 测试代码
#     test_code = """
#     .container {
#     display: grid;
#     grid-template-columns: 1fr 1fr 1fr;
#     gap: 20px;
# }
#     """
#
#     print("=" * 50)
#     print("浏览器兼容性诊断助手")
#     print("=" * 50)
#     print(f"\n📝 输入代码:\n{test_code}")
#
#     report = diagnose(test_code)
#
#     print("\n" + "=" * 50)
#     print("📋 诊断报告")
#     print("=" * 50)
#     print(report)

    # # 测试1：不传 category（检索所有）
    # results = retrieve(["const"])
    # print("不带过滤:", [r["metadata"]["category"] for r in results])
    #
    # # 测试2：只检索 JS
    # results = retrieve(["const"], category="JS")
    # print("只检索JS:", [r["metadata"]["category"] for r in results])
    #
    # # 测试3：只检索 CSS
    # results = retrieve(["flex"], category="CSS")
    # print("只检索CSS:", [r["metadata"]["category"] for r in results])
    #
    # test_code_3 = """
    # const items = [1, 2, 3];
    # const doubled = items.map(x => x * 2);
    # """
    #
    # print("\n" + "=" * 60)
    # print("测试3：ES6语法 - 只关心现代浏览器（不管IE）")
    # print("=" * 60)
    # report = diagnose(test_code_3, target_browsers=["Chrome", "Firefox", "Safari", "Edge"])
    # print("\n📋 诊断报告：\n")
    # print(report)
    #
    # test_code = """
    # const getData = async () => {
    #     const res = await fetch('/api/data');
    #     return res.json();
    # };
    # const items = [1, 2, 3];
    # const doubled = items.map(x => x * 2);
    # Promise.all([getData()]).then(console.log);
    # """
    # # 测两次：指定IE vs 只指定现代浏览器
    # report_ie = diagnose(test_code, target_browsers=["IE11"])
    # print("\n📋 诊断报告：\n")
    # print(report_ie)

    # report_modern = diagnose(test_code, target_browsers=["Chrome", "Firefox", "Safari", "Edge"])
    # print("\n📋 诊断报告：\n")
    # print(report_modern)

    test_code = """
 <template id="tpl">
  <picture>
    <source srcset="img.webp" type="image/webp">
    <img src="img.jpg" alt="test">
  </picture>
</template>
<style>
  .box { display: grid; gap: 10px; }
</style>
<script>
  const [a, ...rest] = [1, 2, 3];
  for (const item of rest) {
    console.log(item);
  }
</script>
    """
    report = diagnose(test_code)
    print("\n📋 诊断报告：\n")
    print(report)
