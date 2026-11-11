from __future__ import annotations

from pathlib import Path

import yaml

from pipeline import generator
from pipeline.models import Amendment, Summary
from tests.conftest import make_event
from tests.test_models import valid_summary


def record(**overrides: object) -> Amendment:
    return Amendment(event=make_event(**overrides), summary=Summary.model_validate(valid_summary()), generated_at="2026-11-12T00:00:00+00:00", provider="template")


def test_frontmatter_fields() -> None:
    fm = generator.frontmatter(record())
    assert fm["title"] == "試験法"
    assert fm["category_slugs"] == ["it-security"]
    assert fm["enforcement_date"] == "2027-01-01"
    assert fm["egov_url"] == "https://laws.e-gov.go.jp/law/500AC0000000001"
    assert fm["stats"] == {"added_lines": 2, "removed_lines": 1, "changed_chars": record().event.stats.changed_chars}
    assert generator.frontmatter(record(enforcement_date=None))["enforcement_date"] is None


def test_render_and_write(tmp_path: Path) -> None:
    rec = record()
    path = generator.write(rec, tmp_path / "content")
    text = path.read_text(encoding="utf-8")
    assert path.name == "500ac0000000001-20270101-508ac0000000056.md"
    head, body = text.split("---\n", 2)[1:]
    parsed = yaml.safe_load(head)
    assert parsed["impact_level"] == "High" and parsed["summary_points"] == ["何が", "なぜ", "どう"]
    assert body.startswith("\n## 変更の概要")
    assert "## 条文差分（機械抽出）" in body and "```diff" in body
    assert "切り詰めた" not in body
    assert "\r" not in path.read_bytes().decode("utf-8")  # LF 固定（Windows でも CRLF にしない）


def test_render_without_diff_and_with_truncation() -> None:
    no_diff = record(old_text="x\n", new_text="x\n", diff="")
    assert "条文差分" not in generator.render_markdown(no_diff)
    long = record(diff="+" + "あ" * (generator.MAX_DIFF_CHARS + 10))
    text = generator.render_markdown(long)
    assert "切り詰めた" in text
    assert text.count("あ") == generator.MAX_DIFF_CHARS - 1
