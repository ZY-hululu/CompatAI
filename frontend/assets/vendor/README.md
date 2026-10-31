# 离线依赖（vendor）

页面**不引用任何 CDN**：这三个文件就放在这里，断网可用。
升级时请更新版本号与 SHA-256，并跑一遍 `node frontend/tests/ui-smoke.cjs`。

| 文件 | 版本 | 大小 | SHA-256 | 来源 |
|---|---|---|---|---|
| `tailwind.js` | 3.4.16 | 451,131 B | `3f81aa7f6ecdb1acc14c202e513dfee00b6c7703cd81ce1be25bf5215a92e8cb` | https://cdn.tailwindcss.com/3.4.16 |
| `echarts.min.js` | 5.5.1 | 1,030,855 B | `e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf` | https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js |

## 重新下载

```powershell
# 在仓库根目录执行
python - <<'PY'
import urllib.request
from pathlib import Path
VENDOR = Path('frontend/assets/vendor')
for name, url in [
    ('tailwind.js', 'https://cdn.tailwindcss.com/3.4.16'),
    ('echarts.min.js', 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js'),
]:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    (VENDOR / name).write_bytes(urllib.request.urlopen(req, timeout=60).read())
PY
```

## 字体

不再加载 Google Fonts。`tailwind.config` 里的字体栈以 `Inter` 打头，本机装了就用，
没装自动落到 `system-ui / Segoe UI / Microsoft YaHei`，布局不受影响。
