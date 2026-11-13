from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.models import Amendment, AmendmentEvent, Summary, make_slug
from tests.conftest import make_event


def valid_summary(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "law_title": "試験法",
        "law_num": "令和八年法律第一号",
        "amendment_type": "一部改正",
        "promulgation_date": "2026-07-17",
        "enforcement_date": "2027-01-01",
        "categories": ["情報セキュリティ・IT", "情報セキュリティ・IT"],
        "impact_level": "High",
        "target_audience": ["情シス・CISO"],
        "summary_points": ["何が", "なぜ", "どう"],
        "body_markdown": "## 変更の概要\n本文",
    }
    base.update(overrides)
    return base


def test_summary_dedups_categories() -> None:
    s = Summary.model_validate(valid_summary())
    assert s.categories == ["情報セキュリティ・IT"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"categories": ["未知の分野"]},
        {"categories": []},
        {"promulgation_date": "2026/07/17"},
        {"summary_points": ["a", "b"]},
        {"summary_points": ["a", "b", "c", "d"]},
        {"summary_points": ["a", " ", "c"]},
        {"target_audience": []},
        {"impact_level": "Critical"},
        {"amendment_type": "全部改正"},
        {"extra_field": 1},
        {"body_markdown": ""},
    ],
)
def test_summary_rejects_bad_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Summary.model_validate(valid_summary(**overrides))


def test_kind_from_egov() -> None:
    assert AmendmentEvent.kind_from_egov("1") == "新設"
    assert AmendmentEvent.kind_from_egov("3") == "一部改正"
    assert AmendmentEvent.kind_from_egov("8") == "廃止"
    with pytest.raises(ValueError, match="unknown e-Gov amendment_type"):
        AmendmentEvent.kind_from_egov("9")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("500AC0000000001_20270101_508AC0000000056", "500ac0000000001-20270101-508ac0000000056"),
        ("ＡＢＣ-123", "abc-123"),
        ("個人情報保護法", "amendment"),
        ("--x--", "x"),
    ],
)
def test_make_slug(value: str, expected: str) -> None:
    assert make_slug(value) == expected


def test_amendment_slug() -> None:
    record = Amendment(event=make_event(), summary=Summary.model_validate(valid_summary()), generated_at="2026-11-12T00:00:00+00:00", provider="template")
    assert record.slug == "500ac0000000001-20270101-508ac0000000056"


def test_blank_list_entries_are_dropped_before_length_check() -> None:
    """LLM が空文字で配列を水増しする（10 要素中 7 個が空）事例への対処。空要素は落とし、残りで長さを検証する。"""
    s = Summary.model_validate(valid_summary(summary_points=["定義が変わった", " ", "整備法のため", "", "義務なし", "", "", "", "", ""], target_audience=["事業者", "", "  "]))
    assert s.summary_points == ["定義が変わった", "整備法のため", "義務なし"]
    assert s.target_audience == ["事業者"]
    with pytest.raises(ValidationError, match="at least 3 items"):
        Summary.model_validate(valid_summary(summary_points=["定義が変わった", "", "整備法のため"]))
    with pytest.raises(ValidationError, match="at least 1 item"):
        Summary.model_validate(valid_summary(target_audience=["", " "]))
    with pytest.raises(ValidationError, match="list"):
        Summary.model_validate(valid_summary(summary_points="定義が変わった"))


def test_summary_points_labels_are_stripped() -> None:
    s = Summary.model_validate(valid_summary(summary_points=["何が: 定義が変わった", "なぜ：整備法のため", "どう変わったか: 義務なし"]))
    assert s.summary_points == ["定義が変わった", "整備法のため", "義務なし"]
    plain = Summary.model_validate(valid_summary(summary_points=["定義が変わった", "整備法のため", "義務なし"]))
    assert plain.summary_points == ["定義が変わった", "整備法のため", "義務なし"]
    with pytest.raises(ValidationError, match="label only"):
        Summary.model_validate(valid_summary(summary_points=["何が:", "なぜ: x", "どう: y"]))

