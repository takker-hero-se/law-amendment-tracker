"""パイプラインのデータ型。観測事実（差分）と LLM の推定（要約）は別の型に置く。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CATEGORIES: tuple[str, ...] = (
    "情報セキュリティ・IT",
    "人事・労務",
    "税務・会計",
    "企業法務・コンプライアンス",
    "知財",
    "その他",
)
CATEGORY_SLUGS: dict[str, str] = {
    "情報セキュリティ・IT": "it-security",
    "人事・労務": "hr-labor",
    "税務・会計": "tax-accounting",
    "企業法務・コンプライアンス": "legal-compliance",
    "知財": "ip",
    "その他": "other",
}

AmendmentKind = Literal["新設", "一部改正", "廃止"]
ImpactLevel = Literal["High", "Medium", "Low"]

_EGOV_AMENDMENT_TYPE: dict[str, AmendmentKind] = {"1": "新設", "3": "一部改正", "8": "廃止"}


class EnforcementStatus(StrEnum):
    """e-Gov `current_revision_status` の写し。"""

    CURRENT = "CurrentEnforced"
    UNENFORCED = "UnEnforced"
    PREVIOUS = "PreviousEnforced"
    REPEAL = "Repeal"


class LawRef(BaseModel):
    """法令の識別子（e-Gov の law_info 相当）。"""

    model_config = ConfigDict(frozen=True)

    law_id: str
    law_num: str
    law_title: str
    law_type: str = "Act"
    egov_category: str | None = None
    abbrev: str | None = None


class DiffStats(BaseModel):
    """差分の計測値（観測事実）。"""

    added_lines: int = 0
    removed_lines: int = 0
    changed_chars: int = 0


class AmendmentEvent(BaseModel):
    """検知した 1 件の改正（観測事実）。LLM の推定を含めない。"""

    event_id: str
    law: LawRef
    revision_id: str
    prev_revision_id: str | None = None
    amendment_kind: AmendmentKind
    amending_law_id: str | None = None
    amending_law_title: str | None = None
    amending_law_num: str | None = None
    promulgation_date: date
    enforcement_date: date | None = None
    enforcement_comment: str | None = None
    enforcement_status: EnforcementStatus = EnforcementStatus.CURRENT
    source: str = "egov"
    # e-Gov が本文ファイルを提供しない履歴（404）は False。メタデータだけを公開する。
    text_available: bool = True
    old_text: str = ""
    new_text: str = ""
    diff: str = ""
    stats: DiffStats = Field(default_factory=DiffStats)

    @staticmethod
    def kind_from_egov(code: str) -> AmendmentKind:
        try:
            return _EGOV_AMENDMENT_TYPE[code]
        except KeyError as exc:
            raise ValueError(f"unknown e-Gov amendment_type {code!r}") from exc


class Summary(BaseModel):
    """LLM の構造化出力。スキーマ外の値は拒否する（strict）。"""

    model_config = ConfigDict(extra="forbid")

    law_title: str = Field(min_length=1, max_length=200)
    law_num: str = Field(min_length=1, max_length=120)
    amendment_type: AmendmentKind
    promulgation_date: str = Field(description="YYYY-MM-DD")
    enforcement_date: str = Field(description="YYYY-MM-DD、または不明・政令委任の場合はその旨")
    categories: list[str] = Field(min_length=1, max_length=3)
    impact_level: ImpactLevel
    target_audience: list[str] = Field(min_length=1, max_length=5)
    summary_points: list[str] = Field(min_length=3, max_length=3, description="何が・なぜ・どう変わったか")
    body_markdown: str = Field(min_length=1, max_length=12000)

    @field_validator("categories")
    @classmethod
    def _known_categories(cls, value: list[str]) -> list[str]:
        unknown = [c for c in value if c not in CATEGORIES]
        if unknown:
            raise ValueError(f"unknown categories: {unknown}")
        return list(dict.fromkeys(value))

    @field_validator("promulgation_date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("promulgation_date must be YYYY-MM-DD")
        return value

    @field_validator("summary_points", "target_audience")
    @classmethod
    def _non_blank(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value]
        if any(not v for v in cleaned):
            raise ValueError("blank entries are not allowed")
        return cleaned


class Amendment(BaseModel):
    """公開用レコード（観測事実 + 要約）。Markdown の frontmatter に写す。"""

    event: AmendmentEvent
    summary: Summary
    generated_at: str
    provider: str

    @property
    def slug(self) -> str:
        return make_slug(self.event.event_id)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def make_slug(value: str) -> str:
    """ファイル名・URL に使える ASCII スラッグを作る。"""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug or "amendment"
