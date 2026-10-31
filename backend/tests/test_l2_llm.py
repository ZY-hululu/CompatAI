"""L2 主流程测试（Mock LLM，不联网、不需要 API Key）。

覆盖四道闸门的联动：
* 合法输出 → 直接采用；
* 幻觉版本号 / 未知 issue_id / 口径错误 → 带反馈重试；
* 重试耗尽 / 调用异常 / 未配置 Key → 确定性降级（degraded=True，needs_human=True）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.schemas import DetectRequest, ExplainFixRequest, SourceCode
from app.l1.detector import detect
from app.l2 import llm
from app.l2.prompts import SYSTEM_PROMPT, classify
from app.main import app

CSS = ".a { accent-color: red; }"
JS = "const x = user?.name;"

CSS_TEXT = "Safari 12 会忽略该 CSS 声明，样式回退到默认表现；需要 Safari 15.4 才支持。"
JS_TEXT = "Safari 12 在解析阶段直接抛 SyntaxError，整个脚本都不会执行；需要 Safari 13.1。"


def item(issue_id: str, text: str, **overrides) -> dict:
    base = {
        "issue_id": issue_id,
        "explain_text": text,
        "fix_diff": "",
        "fix_code_css": None,
        "fix_code_js": None,
        "fix_code_html": None,
    }
    base.update(overrides)
    return base


def payload(*items: dict) -> str:
    return json.dumps({"explanations": list(items)}, ensure_ascii=False)


class FakeClient:
    """按顺序返回预设结果；元素是 Exception 时抛出。"""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], dict | None]] = []

    def complete(self, messages, schema):
        self.calls.append((messages, schema))
        if not self.responses:
            raise AssertionError("FakeClient 的预设响应已耗尽")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture()
def payload_pair() -> ExplainFixRequest:
    body = detect(DetectRequest(css=CSS, js=JS, targets=["safari 12"]))
    return ExplainFixRequest(
        detect=body,
        code=SourceCode(css=CSS, js=JS),
    )


@pytest.fixture()
def settings_one_retry():
    return get_settings().model_copy(update={"llm_max_retries": 1, "openai_model": "test-model"})


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

def test_valid_response_is_used(payload_pair, settings_one_retry) -> None:
    client = FakeClient([payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT))])
    outcome = llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    response = outcome.response
    assert response.degraded is False
    assert response.model == "test-model"
    assert [explanation.issue_id for explanation in response.explanations] == ["issue_1", "issue_2"]
    assert outcome.attempts[0].ok is True
    assert len(client.calls) == 1


def test_schema_enum_and_prompt_whitelist(payload_pair, settings_one_retry) -> None:
    client = FakeClient([payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT))])
    llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    messages, schema = client.calls[0]
    assert messages[0]["content"] == SYSTEM_PROMPT
    user = messages[1]["content"]
    assert "issue_1" in user and "issue_2" in user
    assert "MDN BCD:" in user
    enum = schema["properties"]["explanations"]["items"]["properties"]["issue_id"]["enum"]
    assert enum == ["issue_1", "issue_2"]


def test_explanation_guidance_differs_by_kind(payload_pair, settings_one_retry) -> None:
    """语法级与 API 级的口径要求必须分别写进提示词。"""
    body = detect(DetectRequest(js="const v = items.at(-1);", targets=["safari 12"]))
    api_messages = llm.prompts.build_messages(body.issues, SourceCode(js="const v = items.at(-1);"), ["safari 12"], context_lines=5)

    syntax_issues = [issue for issue in payload_pair.detect.issues if issue.type == "js"]
    syntax_messages = llm.prompts.build_messages(syntax_issues, payload_pair.code, ["safari 12"], context_lines=5)

    assert classify(syntax_issues[0]) == "syntax"
    assert classify(body.issues[0]) == "api"

    # 语法级：要求讲"解析阶段失败、整段脚本不执行"
    assert "解析阶段" in syntax_messages[1]["content"]
    # API 级：要求讲"运行时 undefined / TypeError"
    assert "undefined" in api_messages[1]["content"]
    assert "解析阶段" not in api_messages[1]["content"]


def test_explanation_without_fix_code_is_marked_needs_human(payload_pair, settings_one_retry) -> None:
    """真机发现的缺口：只给 fix_diff 不给代码时必须标 needs_human，避免前端误以为能一键修复。"""
    no_code = payload(
        item("issue_1", CSS_TEXT, fix_diff="改用负 margin 方案"),          # 无 fix_code_*
        item("issue_2", JS_TEXT, fix_code_js="const x = user && user.name;", fix_diff="改短路取值"),
    )
    client = FakeClient([no_code])

    response = llm.explain_and_fix(payload_pair, client=client, settings=settings_one_retry)

    assert response.degraded is False
    by_id = {explanation.issue_id: explanation for explanation in response.explanations}
    assert by_id["issue_1"].needs_human is True
    assert by_id["issue_2"].needs_human is False
    assert by_id["issue_2"].fix_code_js == "const x = user && user.name;"


# --------------------------------------------------------------------------- #
# 幻觉拦截 → 重试
# --------------------------------------------------------------------------- #

def test_hallucinated_version_triggers_retry_then_succeeds(payload_pair, settings_one_retry) -> None:
    bad = payload(item("issue_1", "Safari 12 会忽略该样式，需要 Safari 15。"), item("issue_2", JS_TEXT))
    good = payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT))
    client = FakeClient([bad, good])

    outcome = llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    assert len(client.calls) == 2
    assert outcome.attempts[0].ok is False
    assert "版本号" in outcome.attempts[0].detail
    assert outcome.attempts[1].ok is True
    assert outcome.response.degraded is False

    # 第二次调用必须把"错在哪"喂回去
    retry_messages = client.calls[1][0]
    assert retry_messages[-1]["role"] == "user"
    assert "上一轮输出存在以下问题" in retry_messages[-1]["content"]
    assert "版本号" in retry_messages[-1]["content"]


def test_unknown_issue_id_triggers_retry(payload_pair, settings_one_retry) -> None:
    bad = payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT), item("issue_99", JS_TEXT))
    good = payload(item("issue_1", CSS_TEXT), item("issue_2", JS_TEXT))
    client = FakeClient([bad, good])

    outcome = llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    assert len(client.calls) == 2
    assert "不在白名单" in outcome.attempts[0].detail
    assert outcome.response.degraded is False


def test_persistent_hallucination_falls_back(payload_pair, settings_one_retry) -> None:
    bad = payload(item("issue_1", "需要 Safari 15 才支持。"), item("issue_2", JS_TEXT))
    client = FakeClient([bad, bad])

    outcome = llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    assert len(client.calls) == 2
    assert outcome.response.degraded is True
    assert [explanation.issue_id for explanation in outcome.response.explanations] == ["issue_1", "issue_2"]
    assert all(explanation.needs_human for explanation in outcome.response.explanations)
    assert all(explanation.fix_code_css is None for explanation in outcome.response.explanations)


def test_client_exception_falls_back(payload_pair, settings_one_retry) -> None:
    client = FakeClient([RuntimeError("connection reset"), RuntimeError("connection reset")])
    outcome = llm.run_explain(payload_pair, client=client, settings=settings_one_retry)

    assert len(client.calls) == 2
    assert outcome.response.degraded is True
    assert any("模型调用失败" in attempt.detail for attempt in outcome.attempts)


# --------------------------------------------------------------------------- #
# 降级路径
# --------------------------------------------------------------------------- #

def test_no_api_key_degrades_without_calling_model(payload_pair, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"openai_api_key": None})
    outcome = llm.run_explain(payload_pair, client=llm._AUTO, settings=settings)

    assert outcome.response.degraded is True
    assert outcome.response.model is None
    assert all(explanation.needs_human for explanation in outcome.response.explanations)


def test_fallback_wording_matches_kind(payload_pair) -> None:
    response = llm.explain_and_fix(payload_pair, client=None)
    assert response.degraded is True

    by_id = {explanation.issue_id: explanation.explain_text for explanation in response.explanations}
    assert "SyntaxError" in by_id["issue_2"]          # 语法级：解析失败
    assert "忽略" in by_id["issue_1"]                  # CSS：样式降级
    assert "MDN BCD:" in by_id["issue_1"]


def test_no_issues_makes_no_call() -> None:
    body = detect(DetectRequest(css=".a { color: red; }", targets=["safari 12"]))
    assert body.issues == []
    client = FakeClient([])
    payload = ExplainFixRequest(detect=body, code=SourceCode(css=".a { color: red; }"))

    response = llm.explain_and_fix(payload, client=client)

    assert response.explanations == []
    assert response.degraded is False
    assert client.calls == []


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

def test_http_explain_returns_200_even_without_key(payload_pair, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "openai_api_key", None, raising=False)
    client = TestClient(app)

    response = client.post(
        "/api/explain-fix",
        json={
            "detect": payload_pair.detect.model_dump(mode="json"),
            "code": {"css": CSS, "js": JS, "html": ""},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["degraded"] is True
    assert len(body["explanations"]) == 2
    assert all(explanation["needs_human"] for explanation in body["explanations"])


def test_http_explain_rejects_invalid_payload() -> None:
    """结构非法要 422；注意空 issues 是合法的（前端没有问题时也可能调用）。"""
    client = TestClient(app)

    bad_issues = client.post(
        "/api/explain-fix",
        json={"detect": {"issues": "not-a-list"}, "code": {"css": "a{}"}},
    )
    assert bad_issues.status_code == 422

    missing_code = client.post(
        "/api/explain-fix",
        json={"detect": {"status": "success", "issues": []}},
    )
    assert missing_code.status_code == 422


def test_http_explain_with_empty_issues_is_success() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/explain-fix",
        json={"detect": {"status": "success", "issues": []}, "code": {"css": "a{}"}},
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "explanations": [],
        "model": None,
        "degraded": False,
    }
