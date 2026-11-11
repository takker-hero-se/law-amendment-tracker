from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import state as state_mod


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    s = state_mod.load(tmp_path / "none.json")
    assert s.snapshot == {} and s.last_run_at is None


def test_roundtrip(tmp_path: Path) -> None:
    s = state_mod.State(snapshot={"a": "a_1"}, git_last_commit="abc")
    s.mark_seen("r1")
    s.mark_seen("r1")
    s.touch()
    path = tmp_path / "sub" / "state.json"
    state_mod.save(s, path)
    loaded = state_mod.load(path)
    assert loaded.snapshot == {"a": "a_1"}
    assert loaded.seen_revision_ids == ["r1"]
    assert loaded.git_last_commit == "abc"
    assert loaded.last_run_at and loaded.last_run_at.endswith("+00:00")
    assert not path.with_suffix(".json.tmp").exists()
    assert b"\r" not in path.read_bytes()  # LF 固定


def test_seen_cap() -> None:
    s = state_mod.State()
    for i in range(state_mod.SEEN_LIMIT + 10):
        s.mark_seen(f"r{i}")
    assert len(s.seen_revision_ids) == state_mod.SEEN_LIMIT
    assert not s.has_seen("r0") and s.has_seen(f"r{state_mod.SEEN_LIMIT + 9}")


def test_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt"):
        state_mod.load(path)
