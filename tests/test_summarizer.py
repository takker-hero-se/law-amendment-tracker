from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from pipeline import summarizer
from pipeline.config import Settings
from pipeline.errors import SchemaMismatch, SummarizerError
from pipeline.models import Summary
from pipeline.prompt_templates import SYSTEM_PROMPT, user_prompt
from tests.conftest import make_event
from tests.test_models import valid_summary


class RecordingProvider:
    name = "mock"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        assert system == SYSTEM_PROMPT
        assert "properties" in schema
        self.prompts.append(user)
        return self.outputs.pop(0)


# ------------------------------------------------------------------ template
def test_template_summary_uses_egov_category_and_stats() -> None:
    s = summarizer.template_summary(make_event())
    assert s.categories == ["情報セキュリティ・IT"]
    assert s.impact_level == "Low"
    assert s.enforcement_date == "2027-01-01"
    assert len(s.summary_points) == 3 and "+2 行 / -1 行" in s.summary_points[2]


def test_template_summary_hint_unknown_category_and_impacts() -> None:
    assert summarizer.template_summary(make_event(), category_hint="知財").categories == ["知財"]
    ev = make_event(law=make_event().law.model_copy(update={"egov_category": "謎"}))
    assert summarizer.template_summary(ev).categories == ["その他"]
    assert summarizer.template_summary(make_event(), category_hint="謎").categories == ["その他"]
    assert summarizer.template_summary(make_event(amendment_kind="新設")).impact_level == "High"
    big = make_event(new_text="\n".join(f"第{i}条 新しい条文 {i}" for i in range(45)) + "\n")
    assert summarizer.template_summary(big).impact_level == "Medium"
    unknown = make_event(enforcement_date=None, enforcement_comment="政令で定める日", amending_law_title=None, amending_law_num=None)
    s = summarizer.template_summary(unknown)
    assert s.enforcement_date.startswith("不明") and "政令で定める日" in s.enforcement_date
    assert "新規制定・廃止" in s.summary_points[1]


def test_template_provider_complete_is_not_callable() -> None:
    with pytest.raises(SummarizerError):
        summarizer.TemplateProvider().complete("s", "u", {})


def test_summarize_with_template_provider() -> None:
    s = summarizer.summarize(make_event(), summarizer.TemplateProvider(), max_input_chars=1000, category_hint="人事・労務")
    assert s.categories == ["人事・労務"]


# ----------------------------------------------------------------- summarize
def test_summarize_validates_and_retries_once() -> None:
    good = json.dumps(valid_summary(), ensure_ascii=False)
    provider = RecordingProvider(["{broken", good])
    s = summarizer.summarize(make_event(), provider, max_input_chars=2000, category_hint="知財")
    assert isinstance(s, Summary)
    assert len(provider.prompts) == 2
    assert "監視リスト上の分野: 知財" in provider.prompts[0]
    assert "スキーマ検証に失敗" in provider.prompts[1]


def test_summarize_fails_after_two_bad_outputs() -> None:
    provider = RecordingProvider([json.dumps(valid_summary(categories=["x"])), "null"])
    with pytest.raises(SummarizerError, match="failed schema validation twice"):
        summarizer.summarize(make_event(), provider, max_input_chars=2000)


def test_user_prompt_contents() -> None:
    ev = make_event(enforcement_comment="公布から一年", enforcement_date=None)
    text = user_prompt(ev, "## 変更 1")
    assert "施行日: 不明（施行期日の備考: 公布から一年）" in text
    assert "## 変更 1" in text
    assert "本文差分なし" in user_prompt(ev, "")


# --------------------------------------------------------------- anthropic
class _Parsed:
    def __init__(self, parsed: Summary | None, stop: str = "end_turn") -> None:
        self.parsed_output = parsed
        self.stop_reason = stop


class _Messages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Client:
    def __init__(self, result: Any) -> None:
        self.messages = _Messages(result)


def _status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(code, request=request, json={"error": {"message": "x"}})
    return cls("x", response=response, body=None)


class _RaisingMessages:
    def parse(self, **kwargs: Any) -> Any:
        try:
            Summary.model_validate(valid_summary(target_audience=["a", "b", "c", "d", "e", "f"]))
        except ValidationError as exc:
            raise exc
        raise AssertionError("unreachable")  # pragma: no cover


def test_anthropic_provider_sdk_validation_becomes_schema_mismatch() -> None:
    client = _Client(None)
    client.messages = _RaisingMessages()  # type: ignore[assignment]
    provider = summarizer.AnthropicProvider("claude-haiku-5-5", client=client)
    with pytest.raises(SchemaMismatch, match="at most 5 items"):
        provider.complete("s", "u", {})


class _MismatchThenGood:
    name = "mock"

    def __init__(self, good: str) -> None:
        self.good = good
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        self.prompts.append(user)
        if len(self.prompts) == 1:
            raise SchemaMismatch("too many audiences")
        return self.good


def test_summarize_retries_after_schema_mismatch() -> None:
    provider = _MismatchThenGood(json.dumps(valid_summary(), ensure_ascii=False))
    s = summarizer.summarize(make_event(), provider, max_input_chars=2000)
    assert isinstance(s, Summary) and len(provider.prompts) == 2
    assert "too many audiences" in provider.prompts[1]


def test_anthropic_provider_success() -> None:
    summary = Summary.model_validate(valid_summary())
    client = _Client(_Parsed(summary))
    provider = summarizer.AnthropicProvider("claude-haiku-5-5", client=client)
    raw = provider.complete("sys", "usr", {})
    assert Summary.model_validate_json(raw) == summary
    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["system"] == "sys" and call["output_format"] is Summary


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_Parsed(None), "no parsed output"),
        (_Parsed(Summary.model_validate(valid_summary()), stop="refusal"), "declined"),
        (_status_error(anthropic.RateLimitError, 429), "rate limited"),
        (_status_error(anthropic.InternalServerError, 500), "API error 500"),
        (anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")), "connection error"),
    ],
)
def test_anthropic_provider_errors(result: Any, message: str) -> None:
    provider = summarizer.AnthropicProvider("claude-haiku-5-5", client=_Client(result))
    with pytest.raises(SummarizerError, match=message):
        provider.complete("s", "u", {})


# ------------------------------------------------------------------- gemini
class _GenModels:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _GenClient:
    def __init__(self, result: Any) -> None:
        self.models = _GenModels(result)


class _GenResponse:
    def __init__(self, text: str | None) -> None:
        self.text = text


def test_gemini_provider_success_and_errors() -> None:
    client = _GenClient(_GenResponse('{"a": 1}'))
    provider = summarizer.GeminiProvider("gemini-2.5-flash", client=client)
    assert provider.complete("sys", "usr", {"type": "object"}) == '{"a": 1}'
    call = client.models.calls[0]
    assert call["model"] == "gemini-2.5-flash" and call["config"]["response_json_schema"] == {"type": "object"}
    with pytest.raises(SummarizerError, match="empty text"):
        summarizer.GeminiProvider("m", client=_GenClient(_GenResponse(None))).complete("s", "u", {})
    with pytest.raises(SummarizerError, match="gemini error: quota"):
        summarizer.GeminiProvider("m", client=_GenClient(RuntimeError("quota"))).complete("s", "u", {})


# ---------------------------------------------------------------- factory
def test_build_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(summarizer.build_provider(Settings(llm_provider="template")), summarizer.TemplateProvider)
    monkeypatch.setattr(summarizer.AnthropicProvider, "__init__", lambda self, model, client=None: setattr(self, "_model", model))
    monkeypatch.setattr(summarizer.GeminiProvider, "__init__", lambda self, model, client=None: setattr(self, "_model", model))
    assert isinstance(summarizer.build_provider(Settings(llm_provider="anthropic")), summarizer.AnthropicProvider)
    assert isinstance(summarizer.build_provider(Settings(llm_provider="gemini")), summarizer.GeminiProvider)


def test_template_summary_text_unavailable() -> None:
    s = summarizer.template_summary(make_event(text_available=False, old_text="", new_text="", diff=""))
    assert "本文ファイルを提供していない" in s.summary_points[2]
