from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.egov import EgovClient
from pipeline.errors import SourceError
from pipeline.fetcher import EgovSource, GitSource, WatchEntry, event_from_revision, load_watchlist, split_frontmatter
from pipeline.state import State
from tests.conftest import LAW_INFO, REV_MID, REV_NEW, REV_OLD, FakeEgov, revision, write_json

WATCH = [WatchEntry({"law_id": "500AC0000000001", "category": "情報セキュリティ・IT"})]


# ------------------------------------------------------------------ watchlist
def test_load_watchlist(tmp_path: Path) -> None:
    path = tmp_path / "wl.json"
    assert load_watchlist(path) == []
    write_json(path, [{"law_id": "a"}])
    assert load_watchlist(path)[0]["law_id"] == "a"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SourceError, match="not valid JSON"):
        load_watchlist(path)
    write_json(path, {"law_id": "a"})
    with pytest.raises(SourceError, match="must be a JSON list"):
        load_watchlist(path)
    write_json(path, [{"title": "x"}])
    with pytest.raises(SourceError, match="without law_id"):
        load_watchlist(path)


# ------------------------------------------------------- event_from_revision
def test_event_from_revision_fields() -> None:
    ev = event_from_revision(LAW_INFO, REV_NEW, REV_MID["law_revision_id"])
    assert ev.amendment_kind == "一部改正"
    assert ev.promulgation_date == date(2026, 7, 17)
    assert ev.enforcement_date == date(2027, 1, 1)
    assert ev.enforcement_status.value == "UnEnforced"
    assert ev.law.law_title == "試験法" and ev.law.egov_category == "電気通信"
    assert ev.prev_revision_id == REV_MID["law_revision_id"]


def test_event_from_revision_new_and_repeal_and_fallbacks() -> None:
    ev = event_from_revision(LAW_INFO, REV_OLD, None)
    assert ev.amendment_kind == "新設" and ev.amending_law_id is None
    repealed = revision("500AC0000000001", "r", promulgated="", enforced="", status="Repeal", repeal="Repeal")
    ev2 = event_from_revision(LAW_INFO, repealed, None)
    assert ev2.amendment_kind == "廃止"
    assert ev2.promulgation_date == date(2026, 1, 10)  # law_info にフォールバック
    assert ev2.enforcement_date is None
    minimal = dict(revision("500AC0000000001", "r2", promulgated="2026-01-01", enforced="2026-01-01"), law_title=None, current_revision_status=None, amendment_type=None)
    ev3 = event_from_revision({"law_id": "500AC0000000001"}, minimal, None)
    assert ev3.law.law_title == "500AC0000000001" and ev3.law.law_num == "" and ev3.amendment_kind == "一部改正"


def test_event_from_revision_errors() -> None:
    with pytest.raises(SourceError, match="no promulgation date"):
        event_from_revision({"law_id": "x"}, revision("x", "r", promulgated="", enforced=""), None)
    with pytest.raises(SourceError, match="bad date"):
        event_from_revision(LAW_INFO, revision("x", "r", promulgated="2026/01/01", enforced=""), None)
    with pytest.raises(SourceError, match="unknown current_revision_status"):
        event_from_revision(LAW_INFO, revision("x", "r", promulgated="2026-01-01", enforced="", status="Weird"), None)


# ---------------------------------------------------------------- EgovSource
def test_egov_source_bootstrap_without_since_records_snapshot_only(client: EgovClient) -> None:
    state = State()
    events = EgovSource(client, WATCH).detect(state, since=None, limit=10)
    assert events == []
    assert state.snapshot["500AC0000000001"] == REV_NEW["law_revision_id"]


def test_egov_source_since_filters_and_attaches_diff(client: EgovClient, fake_egov: FakeEgov) -> None:
    state = State()
    src = EgovSource(client, WATCH)
    events = src.detect(state, since=date(2026, 1, 1), limit=10)
    assert [e.revision_id for e in events] == [REV_NEW["law_revision_id"], REV_OLD["law_revision_id"]]
    new = events[0]
    assert new.prev_revision_id == REV_MID["law_revision_id"]
    assert "+第三条" in new.diff and new.stats.added_lines == 2 and new.stats.removed_lines == 1
    first = events[1]
    assert first.prev_revision_id is None and first.old_text == "" and first.stats.added_lines >= 2
    assert src.category_hint("500AC0000000001") == "情報セキュリティ・IT"
    assert src.category_hint("nope") is None
    assert sum(1 for c in fake_egov.calls if "/law_data/" in c) == 3


def test_egov_source_incremental_after_snapshot(client: EgovClient, fake_egov: FakeEgov) -> None:
    state = State(snapshot={"500AC0000000001": REV_MID["law_revision_id"]})
    events = EgovSource(client, WATCH).detect(state, since=None, limit=10)
    assert [e.revision_id for e in events] == [REV_NEW["law_revision_id"]]
    assert state.snapshot["500AC0000000001"] == REV_NEW["law_revision_id"]
    # 2 回目は何も出ない
    assert EgovSource(client, WATCH).detect(state, since=None, limit=10) == []


def test_egov_source_skips_seen_and_respects_limit(client: EgovClient) -> None:
    state = State(seen_revision_ids=[REV_NEW["law_revision_id"]])
    events = EgovSource(client, WATCH).detect(state, since=date(2000, 1, 1), limit=1)
    assert [e.revision_id for e in events] == [REV_OLD["law_revision_id"]]


def test_egov_source_revision_without_promulgate_date_is_kept(client: EgovClient, fake_egov: FakeEgov) -> None:
    undated = dict(REV_NEW, amendment_promulgate_date=None, law_revision_id="500AC0000000001_20270101_undated")
    fake_egov.revisions["500AC0000000001"] = [undated, REV_MID, REV_OLD]
    fake_egov.texts[undated["law_revision_id"]] = fake_egov.texts[REV_NEW["law_revision_id"]]
    events = EgovSource(client, WATCH).detect(State(), since=date(2026, 7, 1), limit=5)
    assert [e.revision_id for e in events] == [undated["law_revision_id"]]
    assert events[0].promulgation_date == date(2026, 1, 10)


def test_egov_source_empty_revisions(client: EgovClient, fake_egov: FakeEgov) -> None:
    fake_egov.revisions["500AC0000000001"] = []
    assert EgovSource(client, WATCH).detect(State(), since=date(2000, 1, 1), limit=5) == []


def test_egov_source_scan_adds_changed_laws(client: EgovClient, fake_egov: FakeEgov) -> None:
    other = dict(LAW_INFO, law_id="500AC0000000002")
    rev = revision("500AC0000000002", "500AC0000000002_20260901_508AC0000000001", promulgated="2026-08-01", enforced="2026-09-01")
    fake_egov.add_law(other, [rev], {rev["law_revision_id"]: fake_egov.texts[REV_NEW["law_revision_id"]]})
    fake_egov.add_law(dict(LAW_INFO, law_id=""), [rev], {})  # law_id が空の行は無視
    state = State(snapshot={"500AC0000000001": REV_NEW["law_revision_id"], "500AC0000000002": "old"})
    src = EgovSource(client, WATCH, scan_law_types=["Act"], scan_category_codes=["015"])
    events = src.detect(state, since=None, limit=10)
    assert [e.law.law_id for e in events] == ["500AC0000000002"]
    assert any("category_cd=015" in c and "law_type=Act" in c for c in fake_egov.calls)
    # 分類コードなしの走査でも同じ結果
    plain = EgovSource(client, WATCH, scan_law_types=["Act"])
    assert [e.law.law_id for e in plain.detect(State(snapshot={"500AC0000000001": REV_NEW["law_revision_id"], "500AC0000000002": "old"}), since=None, limit=10)] == ["500AC0000000002"]
    # スナップショット一致なら走査対象に入らない
    state2 = State(snapshot={"500AC0000000001": REV_NEW["law_revision_id"], "500AC0000000002": rev["law_revision_id"]})
    assert src.detect(state2, since=None, limit=10) == []


# ----------------------------------------------------------------- GitSource
def test_split_frontmatter() -> None:
    meta, body = split_frontmatter("---\nlaw_id: a\n---\n本文\n")
    assert meta == {"law_id": "a"} and body == "本文\n"
    assert split_frontmatter("本文") == ({}, "本文")
    assert split_frontmatter("---\n[unclosed\n---\nx")[0] == {}
    assert split_frontmatter("---\n- 1\n---\nx")[0] == {}
    assert split_frontmatter("---\r\nlaw_id: b\r\n---\r\nx")[0] == {"law_id": "b"}


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    from git import Actor, Repo

    repo = Repo.init(tmp_path / "mirror")
    root = Path(repo.working_dir)
    author = Actor("t", "t@example.com")

    def commit(files: dict[str, str | None], message: str) -> str:
        for name, content in files.items():
            path = root / name
            if content is None:
                repo.index.remove([name], working_tree=True)
            else:
                path.write_text(content, encoding="utf-8")
                repo.index.add([name])
        return repo.index.commit(message, author=author, committer=author).hexsha

    commit({"a.md": "---\nlaw_id: A\nlaw_title: 甲法\nlaw_num: 一号\n---\n第一条 甲。\n", "b.md": "---\nlaw_id: B\nlaw_title: 乙法\n---\n第一条 乙。\n", "README.txt": "x"}, "init")
    (root / ".base").write_text(repo.head.commit.hexsha, encoding="utf-8")
    commit(
        {
            "a.md": "---\nlaw_id: A\nlaw_title: 甲法\nlaw_num: 一号\namendment_promulgate_date: 2026-05-01\namendment_enforcement_date: 2026-10-01\n---\n第一条 甲改。\n第二条 新設条文。\n",
            "b.md": None,
            "c.md": "---\nlaw_id: C\ntitle: 丙法\n---\n第一条 丙。\n",
            "README.txt": "y",
        },
        "amend",
    )
    return root


def test_git_source_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="not found"):
        GitSource(tmp_path / "nope")


def test_git_source_first_run_records_head(mirror: Path) -> None:
    src = GitSource(mirror)
    state = State()
    assert src.detect(state, since=None, limit=10) == []
    assert state.git_last_commit == src.head()
    assert src.detect(state, since=None, limit=10) == []  # 変化なし


def test_git_source_detects_changes(mirror: Path) -> None:
    src = GitSource(mirror)
    base = (mirror / ".base").read_text(encoding="utf-8")
    state = State(git_last_commit=base)
    events = src.detect(state, since=None, limit=10)
    by_id = {e.law.law_id: e for e in events}
    assert set(by_id) == {"A", "B", "C"}
    a = by_id["A"]
    assert a.amendment_kind == "一部改正" and a.promulgation_date == date(2026, 5, 1) and a.enforcement_date == date(2026, 10, 1)
    assert a.law.law_title == "甲法" and a.law.law_num == "一号" and a.prev_revision_id == f"A_{base[:12]}"
    assert "+第二条 新設条文。" in a.diff and a.source == "git"
    assert by_id["B"].amendment_kind == "廃止" and by_id["B"].prev_revision_id is not None
    assert by_id["C"].amendment_kind == "新設" and by_id["C"].law.law_title == "丙法" and by_id["C"].prev_revision_id is None
    assert state.git_last_commit == src.head()


def test_git_source_since_limit_and_seen(mirror: Path) -> None:
    src = GitSource(mirror)
    base = (mirror / ".base").read_text(encoding="utf-8")
    head = src.head()
    # since で A（2026-05-01）だけ残る？ B/C はコミット日（今日）なので残る → since を未来にすると A だけ落ちる
    state = State(git_last_commit=base)
    events = src.detect(state, since=date(2026, 6, 1), limit=10)
    assert "A" not in {e.law.law_id for e in events}
    state = State(git_last_commit=base, seen_revision_ids=[f"A_{head[:12]}"])
    events = src.detect(state, since=None, limit=1)
    assert len(events) == 1 and events[0].law.law_id != "A"


def test_git_source_bad_base(mirror: Path) -> None:
    with pytest.raises(SourceError, match="not found or diff failed"):
        GitSource(mirror).detect(State(git_last_commit="0" * 40), since=None, limit=1)


def test_egov_source_text_unavailable_404_keeps_metadata(client: EgovClient, fake_egov: FakeEgov) -> None:
    del fake_egov.texts[REV_NEW["law_revision_id"]]
    events = EgovSource(client, WATCH).detect(State(), since=date(2026, 7, 1), limit=5)
    assert len(events) == 1
    ev = events[0]
    assert ev.text_available is False and ev.diff == "" and ev.new_text == ""
    from pipeline import filter as noise_filter

    assert noise_filter.decide(ev, min_changed_chars=40, min_changed_lines=2).reason == "text unavailable on e-Gov; metadata only"


def test_egov_source_prev_text_404_diffs_against_empty(client: EgovClient, fake_egov: FakeEgov) -> None:
    del fake_egov.texts[REV_MID["law_revision_id"]]
    events = EgovSource(client, WATCH).detect(State(), since=date(2026, 7, 1), limit=5)
    assert events[0].text_available is True and events[0].old_text == "" and events[0].stats.removed_lines == 0


def test_egov_source_non_404_text_error_propagates(client: EgovClient, fake_egov: FakeEgov) -> None:
    fake_egov.fail_status["/api/2/law_data/" + REV_NEW["law_revision_id"]] = [500, 500, 500, 500]
    state = State()
    with pytest.raises(SourceError, match="HTTP 500"):
        EgovSource(client, WATCH).detect(state, since=date(2026, 7, 1), limit=5)
    assert state.snapshot == {}  # 失敗した実行ではスナップショットを進めない


def test_egov_source_limit_defers_snapshot(client: EgovClient) -> None:
    state = State()
    src = EgovSource(client, WATCH)
    first = src.detect(state, since=date(2000, 1, 1), limit=1)
    assert [e.revision_id for e in first] == [REV_NEW["law_revision_id"]]
    assert state.snapshot == {}  # 候補が残っているのでまだ進めない
    state.mark_seen(REV_NEW["law_revision_id"])
    rest = src.detect(state, since=date(2000, 1, 1), limit=10)
    assert [e.revision_id for e in rest] == [REV_OLD["law_revision_id"], REV_MID["law_revision_id"]]
    assert state.snapshot["500AC0000000001"] == REV_NEW["law_revision_id"]


def test_egov_source_prev_text_non_404_error_propagates(client: EgovClient, fake_egov: FakeEgov) -> None:
    fake_egov.fail_status["/api/2/law_data/" + REV_MID["law_revision_id"]] = [503, 503, 503, 503]
    with pytest.raises(SourceError, match="HTTP 503"):
        EgovSource(client, WATCH).detect(State(), since=date(2026, 7, 1), limit=5)
