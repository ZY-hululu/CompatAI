"""L2：调用大模型解释并给出修复 —— 四道闸门的落地位置。

闸门顺序（任何一道拦下都会重试，重试仍失败则降级）：

1. **输入白名单**：提示词里只出现 L1 命中的 issue，附命中行上下文的片段；
2. **结构化输出**：优先 ``response_format=json_schema``（``issue_id`` 用 enum 封死），
   模型不支持时依次降级到 ``json_object``、纯文本（再靠 extract_json 兜）；
3. **后置校验**：``validation.validate`` —— 白名单 / 版本封闭 / 口径 / 语法门禁；
4. **确定性降级**：``fallback.explain_all``，``degraded=True`` + ``needs_human=True``。

本模块不做任何兼容性判断：所有事实都来自传入的 L1 issues。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import Settings, get_settings
from app.core.schemas import (
    ExplainFixRequest,
    ExplainFixResponse,
    Issue,
    IssueExplanation,
)
from app.l2 import fallback, prompts, validation

logger = logging.getLogger("compat.l2")

# 区分"调用方没传 client"（走真实 SDK）与"显式传 None"（强制降级，测试用）
_AUTO = object()


class ChatClient(Protocol):
    """最小客户端契约：给定消息与可选的 JSON Schema，返回原始文本。

    只依赖这一个方法，测试里注入假客户端即可，完全不需要真实网络或 openai 包。
    """

    def complete(self, messages: list[dict[str, str]], schema: dict | None) -> str: ...


@dataclass(slots=True)
class Attempt:
    index: int
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class ExplainOutcome:
    response: ExplainFixResponse
    attempts: list[Attempt] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 真实客户端（OpenAI SDK，支持 base_url 以兼容国内代理 / 百炼等）
# --------------------------------------------------------------------------- #

class OpenAiChatClient:
    """薄封装：把 SDK 的差异（response_format 支持情况）收敛在这一层。"""

    def __init__(self, client, model: str, timeout: float, max_tokens: int) -> None:
        self._client = client
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens

    def complete(self, messages: list[dict[str, str]], schema: dict | None) -> str:
        formats: list[dict | None] = []
        if schema is not None:
            formats.append(
                {
                    "type": "json_schema",
                    "json_schema": {"name": "explain_fix", "strict": True, "schema": schema},
                }
            )
        formats.extend([{"type": "json_object"}, None])

        last_error: Exception | None = None
        for response_format in formats:
            payload: dict = {
                "model": self._model,
                "messages": messages,
                "temperature": 0.0,
                "timeout": self._timeout,
                "max_tokens": self._max_tokens,
            }
            if response_format is not None:
                payload["response_format"] = response_format
            try:
                completion = self._client.chat.completions.create(**payload)
                return completion.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 - 需要区分"格式不支持"与其他错误
                if response_format is not None and _format_unsupported(exc):
                    logger.warning("模型不支持 %s，降级重试：%s", response_format.get("type"), exc)
                    last_error = exc
                    continue
                raise
        raise RuntimeError(f"模型不接受任何 JSON 输出模式：{last_error}")


def _format_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = ("response_format", "json_schema", "json mode", "unsupported", "unknown parameter")
    return any(marker in text for marker in markers)


def build_default_client(settings: Settings) -> OpenAiChatClient | None:
    """没配 key 或没装 SDK 时返回 ``None``（调用方走降级），绝不抛异常打断请求。"""
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai SDK 未安装，L2 将走确定性降级")
        return None

    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url or None,
        timeout=settings.llm_timeout_seconds,
    )
    return OpenAiChatClient(
        client=client,
        model=settings.openai_model,
        timeout=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_output_tokens,
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _mark_unfixed(explanations: list[IssueExplanation]) -> list[IssueExplanation]:
    """没有给出任何修复代码的条目，一律标 ``needs_human=True``。

    真机验证时发现的缺口：模型可以只写 ``fix_diff``（"改用负 margin 方案"）却不给代码，
    此时前端拿不到可应用的修复，而 ``needs_human`` 仍是 ``False``，会让人误以为"已经修好了"。
    这里做一次确定性收尾，不依赖模型自觉。
    """
    for explanation in explanations:
        has_edits = bool(explanation.fix_edits)
        has_whole_file = bool(
            explanation.fix_code_css
            or explanation.fix_code_js
            or explanation.fix_code_html
        )
        if not (has_edits or has_whole_file):
            explanation.needs_human = True
    return explanations


def explain_and_fix(
    payload: ExplainFixRequest,
    *,
    client: ChatClient | None | object = _AUTO,
    settings: Settings | None = None,
) -> ExplainFixResponse:
    """解释 + 修复。**任何情况下都返回 200 响应**，最差是 degraded=True。"""
    outcome = run_explain(payload, client=client, settings=settings)
    return outcome.response


def run_explain(
    payload: ExplainFixRequest,
    *,
    client: ChatClient | None | object = _AUTO,
    settings: Settings | None = None,
) -> ExplainOutcome:
    settings = settings or get_settings()
    issues: list[Issue] = payload.detect.issues
    targets = [target.label for target in payload.detect.targets_resolved]

    if not issues:
        return ExplainOutcome(response=ExplainFixResponse(status="success", explanations=[]))

    active_client: ChatClient | None
    if client is _AUTO:
        active_client = build_default_client(settings)
    else:
        active_client = client  # type: ignore[assignment]

    if active_client is None:
        logger.info("L2 无可用模型，走确定性降级（%d 条）", len(issues))
        return ExplainOutcome(
            response=ExplainFixResponse(
                status="success",
                explanations=fallback.explain_all(issues),
                model=None,
                degraded=True,
            )
        )

    messages = prompts.build_messages(
        issues, payload.code, targets, context_lines=settings.llm_context_lines
    )
    schema = prompts.build_schema([issue.id for issue in issues])

    attempts: list[Attempt] = []
    last_detail = ""

    for index in range(settings.llm_max_retries + 1):
        try:
            raw = active_client.complete(messages, schema)
        except Exception as exc:  # noqa: BLE001 - 网络/鉴权错误一律重试后降级
            last_detail = f"模型调用失败：{exc}"
            attempts.append(Attempt(index=index, ok=False, detail=last_detail))
            logger.warning("L2 第 %d 次调用失败：%s", index + 1, exc)
            continue

        result = validation.validate(raw, issues, targets, payload.code)
        attempts.append(
            Attempt(
                index=index,
                ok=result.ok,
                detail="通过校验" if result.ok else "；".join(result.problems[:5]),
            )
        )

        if result.ok:
            logger.info("L2 第 %d 次输出通过校验（%d 条）", index + 1, len(result.explanations))
            return ExplainOutcome(
                response=ExplainFixResponse(
                    status="success",
                    explanations=_mark_unfixed(result.explanations),
                    model=settings.openai_model,
                    degraded=False,
                ),
                attempts=attempts,
            )

        last_detail = "；".join(result.problems[:5])
        logger.warning("L2 第 %d 次输出未通过校验：%s", index + 1, last_detail)
        # 把"错在哪"喂回去，比盲重试有效得多
        messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {"role": "user", "content": result.feedback()},
        ]

    logger.warning("L2 重试耗尽，降级处理。最后错误：%s", last_detail)
    return ExplainOutcome(
        response=ExplainFixResponse(
            status="success",
            explanations=fallback.explain_all(issues),
            model=settings.openai_model,
            degraded=True,
        ),
        attempts=attempts,
    )


__all__ = [
    "Attempt",
    "ChatClient",
    "ExplainOutcome",
    "OpenAiChatClient",
    "build_default_client",
    "explain_and_fix",
    "run_explain",
]
