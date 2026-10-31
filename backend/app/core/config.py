"""运行时配置。

原则：所有可调项都有确定默认值，缺环境变量也能跑起来（L2 的 key 缺失时降级，而不是启动失败）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- L1 ----
    bcd_index_path: Path = PROJECT_ROOT / "data" / "bcd_index.json"
    bcd_overrides_path: Path = PROJECT_ROOT / "data" / "overrides.json"
    max_source_chars: int = 200_000   # 单请求单字段字符上限，防解析器 DoS
    max_targets: int = 16
    max_issues: int = 500             # 超出后截断并置 status=partial

    # ---- L2 ----
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "COMPAT_OPENAI_API_KEY")
    )
    openai_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_BASE_URL", "COMPAT_OPENAI_BASE_URL")
    )
    openai_model: str = Field(
        default="gpt-4o-mini", validation_alias=AliasChoices("OPENAI_MODEL", "COMPAT_OPENAI_MODEL")
    )
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_max_output_tokens: int = 2000
    llm_context_lines: int = 20       # 喂给模型的原码上下文行数（命中行 ± N）

    # ---- L3 ----
    node_binary: str = "node"
    jsdom_runner_path: Path = PROJECT_ROOT / "app" / "l3" / "jsdom_runner.mjs"
    jsdom_timeout_seconds: float = 5.0   # 强制超时：用户代码里的同步死循环只能靠父进程 kill

    # ---- Web ----
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:5500",   # VS Code Live Server
        "http://localhost:5500",
    )

    def require_llm(self) -> None:
        if not self.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY 未配置：L2 将走确定性降级路径")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
