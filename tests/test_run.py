from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from pipeline import run, summarizer
from pipeline import state as state_mod
from pipeline.config import Settings
from pipeline.egov import EgovClient
from pipeline.errors import PipelineError, SummarizerError
from pipeline.fetcher import EgovSource, GitSource
from tests.conftest import REV_MID, REV_NEW, REV_OLD, FakeEgov, write_json
from tests.test_models import valid_summary


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    write_json(tmp_path / "wl.json", [{"law_id": "500AC0000000001", "category": "情報セキュリティ・IT"}])
    return Settings(watchlist_file=tmp_path / "wl.json", content_dir=tmp_path / "content", state_file=tmp_path / "state.json", request_interval_sec=0, noise_min_changed_chars=1, noise_min_changed_lines=1)


def test_parse_args_defaults() -> None:
    args = run.parse_args([])
    assert args.since is None and args.law_id == [] and not args.dry_run and args.limit is None
    args = run.parse_args(["--since", "2026-01-01", "--law-id", "a", "--law-id", "b", "--limit", "3", "--dry-run", "--no-state", "-v"])
    assert args.since == date(2026, 1, 1) and args.law_id == ["a", "b"] and args.limit == 3 and args.dry_run and args.no_state and args.verbose


def test_execute_writes_markdown_and_state(settings: Settings, client: EgovClient) -> None:
    args = run.parse_args(["--since", "2026-01-01"])
    report = run.execute(settings, args, client=client)
    assert (report.detected, report.kept, report.dropped, report.written, report.failed) == (2, 2, 0, 2, 0)
    files = sorted(p.name for p in settings.content_dir.glob("*.md"))
    assert files == ["500ac0000000001-20260110-000000000000000.md", "500ac0000000001-20270101-508ac0000000056.md"]
    text = (settings.content_dir / files[1]).read_text(encoding="utf-8")
    assert "categories:\n- 情報セキュリティ・IT" in text
    state = state_mod.load(settings.state_file)
    assert state.has_seen(REV_NEW["law_revision_id"]) and state.last_run_at
    # 2 回目は重複しない
    report2 = run.execute(settings, args, client=client)
    assert report2.detected == 0 and report2.written == 0


def test_execute_dry_run_does_not_write(settings: Settings, client: EgovClient) -> None:
    args = run.parse_args(["--since", "2026-01-01", "--dry-run"])
    report = run.execute(settings, args, client=client)
    assert report.kept == 2 and report.written == 0
    assert not settings.content_dir.exists() and not settings.state_file.exists()


def test_execute_drops_noise_and_marks_seen(settings: Settings, client: EgovClient, fake_egov: FakeEgov) -> None:
    strict = Settings(**{**settings.__dict__, "noise_min_changed_chars": 10_000})
    args = run.parse_args(["--since", "2026-01-01", "--dry-run", "--no-state"])
    report = run.execute(strict, args, client=client)
    assert report.kept == 1 and report.dropped == 1  # 新設は常に残る
    assert report.dropped_reasons and "changed chars" in report.dropped_reasons[0]


def test_execute_counts_summarizer_failures(settings: Settings, client: EgovClient) -> None:
    class Failing:
        name = "mock"

        def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
            raise SummarizerError("down")

    args = run.parse_args(["--since", "2026-01-01", "--no-state"])
    report = run.execute(settings, args, client=client, provider=Failing())
    assert report.failed == 2 and report.written == 0


def test_execute_with_llm_provider_and_law_id_override(settings: Settings, client: EgovClient) -> None:
    class Good:
        name = "mock"

        def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
            return json.dumps(valid_summary(), ensure_ascii=False)

    args = run.parse_args(["--since", "2026-07-01", "--law-id", "500AC0000000001", "--no-state"])
    report = run.execute(settings, args, client=client, provider=Good())
    assert report.written == 1
    text = next(settings.content_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "provider: mock" in text


def test_execute_owns_client_when_not_injected(settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    created: list[EgovClient] = []

    def factory(*args: Any, **kwargs: Any) -> EgovClient:
        c = make_client()
        created.append(c)
        return c

    monkeypatch.setattr(run, "EgovClient", factory)
    report = run.execute(settings, run.parse_args(["--dry-run", "--no-state"]))
    assert report.detected == 0 and len(created) == 1
    with pytest.raises(RuntimeError):  # closed client cannot be reused
        created[0].law_revisions("500AC0000000001")


def test_build_source_variants(settings: Settings, client: EgovClient, tmp_path: Path) -> None:
    assert isinstance(run.build_source(settings, ["x"], client), EgovSource)
    scan = Settings(**{**settings.__dict__, "category_codes": ["015"]})
    src = run.build_source(scan, [], client)
    assert isinstance(src, EgovSource) and src._scan_law_types == ["Act"]
    with pytest.raises(PipelineError, match="requires a client"):
        run.build_source(settings, [], None)
    git_settings = Settings(**{**settings.__dict__, "source": "git"})
    with pytest.raises(PipelineError, match="LAWTRACK_GIT_MIRROR_DIR"):
        run.build_source(git_settings, [], None)
    from git import Repo

    Repo.init(tmp_path / "mirror")
    git_settings = Settings(**{**settings.__dict__, "source": "git", "git_mirror_dir": tmp_path / "mirror"})
    assert isinstance(run.build_source(git_settings, [], None), GitSource)


def test_execute_git_source_first_run(settings: Settings, tmp_path: Path) -> None:
    from git import Actor, Repo

    repo = Repo.init(tmp_path / "mirror")
    (tmp_path / "mirror" / "a.md").write_text("---\nlaw_id: A\n---\nx\n", encoding="utf-8")
    repo.index.add(["a.md"])
    repo.index.commit("init", author=Actor("t", "t@x"), committer=Actor("t", "t@x"))
    git_settings = Settings(**{**settings.__dict__, "source": "git", "git_mirror_dir": tmp_path / "mirror"})
    report = run.execute(git_settings, run.parse_args([]))
    assert report.detected == 0
    assert state_mod.load(git_settings.state_file).git_last_commit == repo.head.commit.hexsha


def test_main_success_and_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], make_client: Callable[..., EgovClient]) -> None:
    original = run.Settings.from_env
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(run, "EgovClient", lambda *a, **k: make_client())
    strict = Settings(**{**settings.__dict__, "noise_min_changed_chars": 10_000})
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: strict))
    assert run.main(["--env-file", "tests/.no-such-env", "--since", "2026-01-01", "--dry-run", "--no-state"]) == 0
    out = capsys.readouterr().out
    assert "detected=2 kept=1 dropped=1" in out and "dropped:" in out

    assert run.main(["--env-file", "tests/.no-such-env", "--since", "2000-01-01", "--dry-run", "--no-state"]) == 0
    assert "dropped:" in capsys.readouterr().out
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: settings))
    assert run.main(["--env-file", "tests/.no-such-env", "--dry-run", "--no-state"]) == 0
    assert "dropped:" not in capsys.readouterr().out
    assert run.main(["--env-file", "tests/.no-such-env", "--since", "2026-01-01", "--no-state"]) == 0
    assert "written=2" in capsys.readouterr().out
    monkeypatch.setattr(run.Settings, "from_env", original)
    monkeypatch.setenv("LAWTRACK_SOURCE", "bogus")
    assert run.main(["--env-file", "tests/.no-such-env", "--no-state"]) == 1


def test_main_pipeline_error(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    git_settings = Settings(**{**settings.__dict__, "source": "git"})
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: git_settings))
    assert run.main(["--env-file", "tests/.no-such-env", "--no-state"]) == 1


def test_run_report_text() -> None:
    assert run.RunReport(detected=1).as_text() == "detected=1 kept=0 dropped=0 written=0 failed=0"


def test_template_default_provider_path(settings: Settings, client: EgovClient) -> None:
    args = run.parse_args(["--since", "2026-01-01", "--no-state"])
    report = run.execute(settings, args, client=client)
    assert report.written == 2
    assert all("provider: template" in p.read_text(encoding="utf-8") for p in settings.content_dir.glob("*.md"))
    assert isinstance(summarizer.build_provider(settings), summarizer.TemplateProvider)
    assert REV_MID["law_revision_id"] != REV_OLD["law_revision_id"]


# ------------------------------------------------------------------ tickets
def test_execute_with_tickets(settings: Settings, client: EgovClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import tickets
    from tests.test_tickets import FakeGitHub, make_client

    with_tickets = Settings(**{**settings.__dict__, "issues_repo": "owner/laws", "issues_token": "t", "site_url": "https://law.example"})
    gh = FakeGitHub()
    args = run.parse_args(["--since", "2026-01-01"])
    report = run.execute(with_tickets, args, client=client, tickets_client=make_client(gh))
    assert report.written == 2 and report.tickets == 2 and report.tickets_failed == 0
    assert "tickets=2 tickets_failed=0" in report.as_text()
    texts = [p.read_text(encoding="utf-8") for p in with_tickets.content_dir.glob("*.md")]
    assert all("issue_url: https://github.com/owner/laws/issues/" in t for t in texts)
    assert len(state_mod.load(with_tickets.state_file).tickets) == 2
    assert "- 記事: https://law.example/amendments/" in gh.issues[1]["body"]

    # 起票に失敗しても記事は書き、失敗数を数える
    failing = FakeGitHub(fail=[422, 422])
    report = run.execute(with_tickets, run.parse_args(["--since", "2026-01-01", "--no-state"]), client=client, tickets_client=make_client(failing))
    assert report.written == 2 and report.tickets == 0 and report.tickets_failed == 2

    # issues_repo が設定されていれば execute が自前でクライアントを作り、終了時に閉じる
    closed: list[bool] = []
    own = make_client(FakeGitHub())
    monkeypatch.setattr(own, "close", lambda: closed.append(True))
    monkeypatch.setattr(run, "build_tickets_client", lambda s, transport=None: own)
    run.execute(with_tickets, run.parse_args(["--since", "2026-01-01", "--no-state"]), client=client)
    assert closed == [True]
    monkeypatch.undo()
    assert run.build_tickets_client(settings) is None
    real = run.build_tickets_client(with_tickets)
    assert isinstance(real, tickets.GitHubIssues) and real.repo == "owner/laws"
    real.close()
    assert run.article_url(settings, "x") is None and run.article_url(with_tickets, "x") == "https://law.example/amendments/x/"
