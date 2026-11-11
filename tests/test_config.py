from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import Settings


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(__import__("os").environ):
        if key.startswith("LAWTRACK_"):
            monkeypatch.delenv(key)
    s = Settings.from_env()
    assert s.llm_provider == "template"
    assert s.source == "egov"
    assert s.law_types == ["Act"]
    assert s.category_codes == []
    assert s.git_mirror_dir is None
    assert s.max_events_per_run == 30
    assert s.egov_base_url == "https://laws.e-gov.go.jp/api/2"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAWTRACK_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LAWTRACK_SOURCE", "git")
    monkeypatch.setenv("LAWTRACK_GIT_MIRROR_DIR", "mirror")
    monkeypatch.setenv("LAWTRACK_LAW_TYPES", "Act, CabinetOrder,,")
    monkeypatch.setenv("LAWTRACK_CATEGORY_CODES", "015,020")
    monkeypatch.setenv("LAWTRACK_MAX_EVENTS_PER_RUN", "5")
    monkeypatch.setenv("LAWTRACK_REQUEST_INTERVAL_SEC", "0")
    monkeypatch.setenv("LAWTRACK_EGOV_BASE_URL", "https://egov.test/api/2/")
    s = Settings.from_env()
    assert s.llm_provider == "anthropic"
    assert s.source == "git"
    assert s.git_mirror_dir == Path("mirror")
    assert s.law_types == ["Act", "CabinetOrder"]
    assert s.category_codes == ["015", "020"]
    assert s.max_events_per_run == 5
    assert s.request_interval_sec == 0
    assert s.egov_base_url == "https://egov.test/api/2"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("LAWTRACK_MAX_EVENTS_PER_RUN", "abc", "must be an integer"),
        ("LAWTRACK_REQUEST_INTERVAL_SEC", "fast", "must be a number"),
        ("LAWTRACK_LLM_PROVIDER", "openai", "LAWTRACK_LLM_PROVIDER must be one of"),
        ("LAWTRACK_SOURCE", "ftp", "LAWTRACK_SOURCE must be one of"),
        ("LAWTRACK_MAX_EVENTS_PER_RUN", "0", ">= 1"),
        ("LAWTRACK_REQUEST_INTERVAL_SEC", "-1", ">= 0"),
        ("LAWTRACK_LLM_INPUT_MAX_CHARS", "999", ">= 1000"),
    ],
)
def test_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings.from_env()


@pytest.mark.parametrize(("value", "ok"), [("1", True), ("1000", True), ("999", False)])
def test_llm_input_boundary(monkeypatch: pytest.MonkeyPatch, value: str, ok: bool) -> None:
    monkeypatch.setenv("LAWTRACK_MAX_EVENTS_PER_RUN", value)
    monkeypatch.delenv("LAWTRACK_LLM_INPUT_MAX_CHARS", raising=False)
    s = Settings.from_env()
    assert s.max_events_per_run == int(value)
    assert ok or s.max_events_per_run == 999
