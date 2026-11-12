"""LLM による構造化要約。プロバイダは差し替え可能で、資格情報がなければ `TemplateProvider` に退行する。

プロバイダは `Summary` の JSON 文字列を返す責務だけを負い、検証（pydantic）は `summarize()` が一括で行う。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import ValidationError

from pipeline.config import Settings
from pipeline.differ import format_for_llm
from pipeline.errors import SchemaMismatch, SummarizerError
from pipeline.models import CATEGORIES, AmendmentEvent, Summary
from pipeline.prompt_templates import SYSTEM_PROMPT, user_prompt

EGOV_CATEGORY_MAP: dict[str, str] = {
    "電気通信": "情報セキュリティ・IT",
    "警察": "情報セキュリティ・IT",
    "労働": "人事・労務",
    "社会保険": "人事・労務",
    "国家公務員": "人事・労務",
    "国税": "税務・会計",
    "地方税": "税務・会計",
    "地方財政": "税務・会計",
    "財務通則": "税務・会計",
    "商業": "企業法務・コンプライアンス",
    "民事": "企業法務・コンプライアンス",
    "金融・保険": "企業法務・コンプライアンス",
    "産業通則": "企業法務・コンプライアンス",
    "事業": "企業法務・コンプライアンス",
    "刑事": "企業法務・コンプライアンス",
    "外国為替・貿易": "企業法務・コンプライアンス",
    "工業": "知財",
    "文化": "知財",
}


class Provider(Protocol):
    name: str

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """JSON 文字列を返す。失敗は SummarizerError。"""


class TemplateProvider:
    """LLM を使わない決定的な要約。資格情報なしでも完全に動く既定値。"""

    name = "template"

    def __init__(self, *, category_hint: str | None = None) -> None:
        self._category_hint = category_hint

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        raise SummarizerError("TemplateProvider does not call an LLM; use template_summary()")


def template_summary(event: AmendmentEvent, *, category_hint: str | None = None) -> Summary:
    """差分の計測値だけから作る要約（推測を含まない）。"""
    category = category_hint or EGOV_CATEGORY_MAP.get(event.law.egov_category or "", "その他")
    if category not in CATEGORIES:
        category = "その他"
    changed = event.stats.added_lines + event.stats.removed_lines
    if event.amendment_kind in ("新設", "廃止"):
        impact = "High"
    elif changed >= 40:
        impact = "Medium"
    else:
        impact = "Low"
    enforcement = event.enforcement_date.isoformat() if event.enforcement_date else "不明（施行期日は改正法附則・政令による）"
    if event.enforcement_comment:
        enforcement = f"{enforcement}（{event.enforcement_comment}）"
    amending = event.amending_law_title or "（新規制定・廃止）"
    third = (
        f"本文の変更は +{event.stats.added_lines} 行 / -{event.stats.removed_lines} 行（{event.stats.changed_chars} 字）で、LLM 要約は未生成のため条文差分を参照されたい。"
        if event.text_available
        else "e-Gov はこの履歴の本文ファイルを提供していないため、条文差分は取得できていない。改正法の原文を参照されたい。"
    )
    points = [
        f"{event.law.law_title}が{event.amendment_kind}された（公布 {event.promulgation_date.isoformat()}）。",
        f"改正法令は{amending}（{event.amending_law_num or '-'}）である。",
        third,
    ]
    body = (
        "## 変更の概要\n"
        f"本ページは LLM 要約を生成していない（provider=template）。e-Gov 法令検索の履歴 `{event.revision_id}` と"
        f"直前の履歴 `{event.prev_revision_id or '-'}` の本文差分を機械的に抽出した結果のみを掲載する。\n\n"
        "## 実務上の留意点\n"
        "影響の評価は本文差分と改正法の附則を確認のうえ行うこと。本ページの impact_level は差分の行数だけから機械的に付与した暫定値である。\n\n"
        "## 対象となる主な組織\n"
        f"e-Gov の分野分類「{event.law.egov_category or '-'}」に関係する事業者。\n"
    )
    return Summary(
        law_title=event.law.law_title,
        law_num=event.law.law_num,
        amendment_type=event.amendment_kind,
        promulgation_date=event.promulgation_date.isoformat(),
        enforcement_date=enforcement,
        categories=[category],
        impact_level=impact,  # type: ignore[arg-type]
        target_audience=["事業会社法務", "該当業務の担当部門"],
        summary_points=points,
        body_markdown=body,
    )


class AnthropicProvider:
    """Anthropic Messages API。構造化出力（`client.messages.parse`）で Summary を直接得る。"""

    name = "anthropic"

    def __init__(self, model: str, *, client: Any | None = None) -> None:
        self._model = model
        if client is None:  # pragma: no cover - real SDK client is exercised only in production
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        import anthropic

        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=Summary,
            )
        except anthropic.RateLimitError as exc:
            raise SummarizerError(f"anthropic rate limited: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SummarizerError(f"anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise SummarizerError(f"anthropic connection error: {exc}") from exc
        except ValidationError as exc:
            # 構造化出力でも max_length 等の制約は API 側で強制されず、SDK の parse が pydantic の例外を投げる
            raise SchemaMismatch(str(exc)[:500]) from exc
        if response.stop_reason == "refusal":
            raise SummarizerError("anthropic declined the request (stop_reason=refusal)")
        parsed = response.parsed_output
        if parsed is None:
            raise SummarizerError("anthropic returned no parsed output")
        return parsed.model_dump_json()


class GeminiProvider:
    """Google Gemini（google-genai SDK）。`response_schema` で JSON を強制する。"""

    name = "gemini"

    def __init__(self, model: str, *, client: Any | None = None) -> None:
        self._model = model
        if client is None:  # pragma: no cover - real SDK client is exercised only in production
            from google import genai

            client = genai.Client()
        self._client = client

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=user,
                config={
                    "system_instruction": system,
                    "response_mime_type": "application/json",
                    "response_json_schema": schema,
                    "temperature": 0.2,
                },
            )
        except Exception as exc:  # google-genai raises several unrelated types
            raise SummarizerError(f"gemini error: {exc}") from exc
        text = getattr(response, "text", None)
        if not text:
            raise SummarizerError("gemini returned empty text")
        return str(text)


def build_provider(settings: Settings) -> Provider | TemplateProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings.anthropic_model)
    if settings.llm_provider == "gemini":
        return GeminiProvider(settings.gemini_model)
    return TemplateProvider()


def summarize(event: AmendmentEvent, provider: Provider | TemplateProvider, *, max_input_chars: int, category_hint: str | None = None) -> Summary:
    """イベント 1 件を要約する。LLM の出力はスキーマ検証し、失敗したら 1 回だけ再試行する。"""
    if isinstance(provider, TemplateProvider):
        return template_summary(event, category_hint=category_hint)
    llm_input = format_for_llm(event.old_text, event.new_text, max_chars=max_input_chars)
    prompt = user_prompt(event, llm_input)
    if category_hint:
        prompt += f"\n# 参考\n- 監視リスト上の分野: {category_hint}\n"
    schema = Summary.model_json_schema()
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = provider.complete(SYSTEM_PROMPT, prompt, schema)
            return Summary.model_validate_json(raw)
        except (SchemaMismatch, ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            prompt += f"\n\n# 前回の出力はスキーマ検証に失敗した（試行 {attempt + 1}）。指摘: {str(exc)[:500]}\n"
    raise SummarizerError(f"LLM output failed schema validation twice: {last_error}")
