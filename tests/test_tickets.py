from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import generator, tickets, tickets_backfill
from pipeline import state as state_mod
from pipeline.config import Settings
from pipeline.errors import PipelineError, TicketError
from pipeline.models import Amendment, Summary
from tests.conftest import make_event
from tests.test_models import valid_summary

REPO = "owner/laws"


class FakeGitHub:
    """GitHub REST API の最小模擬（labels / issues）。"""

    def __init__(self, *, labels: list[str] | None = None, fail: list[int] | None = None, rate_limit_once: bool = False, raise_once: bool = False) -> None:
        self.labels = set(labels or [])
        self.issues: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = list(fail or [])  # 先頭から順に返すステータス（空になったら正常応答）
        self.rate_limit_once = rate_limit_once
        self.raise_once = raise_once
        self.label_pages = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path + ("?" + request.url.query.decode() if request.url.query else "")))
        if self.raise_once:
            self.raise_once = False
            raise httpx.ConnectError("boom", request=request)
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(403, headers={"Retry-After": "1"}, text="API rate limit exceeded")
        if self.fail:
            return httpx.Response(self.fail.pop(0), json={"message": "fail"})
        path = request.url.path
        if request.method == "GET" and path == f"/repos/{REPO}/labels":
            page = int(request.url.params.get("page", "1"))
            names = sorted(self.labels)
            if self.label_pages > 1:
                chunk = names if page == 1 else []
                return httpx.Response(200, json=[{"name": n} for n in chunk])
            return httpx.Response(200, json=[{"name": n} for n in names])
        if request.method == "POST" and path == f"/repos/{REPO}/labels":
            body = json.loads(request.content)
            self.labels.add(body["name"])
            return httpx.Response(201, json=body)
        if request.method == "POST" and path == f"/repos/{REPO}/issues":
            body = json.loads(request.content)
            number = len(self.issues) + 1
            self.issues[number] = body
            return httpx.Response(201, json={"number": number, "html_url": f"https://github.com/{REPO}/issues/{number}"})
        if request.method == "PATCH" and path.startswith(f"/repos/{REPO}/issues/"):
            number = int(path.rsplit("/", 1)[1])
            self.issues[number] = json.loads(request.content)
            return httpx.Response(200, json={"number": number, "html_url": f"https://github.com/{REPO}/issues/{number}"})
        return httpx.Response(404, json={"message": "not found"})  # pragma: no cover


def make_client(gh: FakeGitHub, **kwargs: Any) -> tickets.GitHubIssues:
    return tickets.GitHubIssues(REPO, "token", transport=httpx.MockTransport(gh.handler), sleep=lambda s: None, **kwargs)


def record(**overrides: Any) -> Amendment:
    return Amendment(event=make_event(**overrides), summary=Summary.model_validate(valid_summary()), generated_at="2026-11-14T00:00:00+00:00", provider="template")


# ----------------------------------------------------------------- building
def test_build_title_labels_body() -> None:
    rec = record()
    fm = generator.frontmatter(rec)
    t = tickets.build(fm, generator.render_markdown(rec), "https://law.example/amendments/x/")
    assert t.revision_id == rec.event.revision_id
    assert t.title.startswith("[High] 試験法：") and "（施行 2027-01-01）" in t.title
    assert t.labels == ["分野:情報セキュリティ・IT", "影響度:High", "施行:未施行", "種別:一部改正"]
    assert t.body.startswith(f"<!-- lawtrack:revision_id={rec.event.revision_id} -->")
    assert "- 記事: https://law.example/amendments/x/" in t.body
    assert "<details><summary>条文差分（機械抽出）</summary>" in t.body and "```diff" in t.body
    assert tickets.DISCLAIMER in t.body
    assert tickets.MARKER_RE.search(t.body).group(1) == rec.event.revision_id  # type: ignore[union-attr]


def test_build_variants() -> None:
    # 本文なし（404）・施行日なし・長い題名・長い差分
    rec = record(text_available=False, old_text="", new_text="", diff="", enforcement_date=None, amending_law_title="あ" * 300)
    fm = generator.frontmatter(rec)
    t = tickets.build(fm, generator.render_markdown(rec))
    assert "本文ファイルを提供していない" in t.body and "<details>" not in t.body and "- 記事:" not in t.body
    assert "（施行 未定）" not in t.title and len(t.title) == tickets.MAX_TITLE_CHARS and t.title.endswith("…")
    long_diff = record(diff="+" + "あ" * (tickets.MAX_BODY_DIFF_CHARS + 10))
    t2 = tickets.build(generator.frontmatter(long_diff), generator.render_markdown(long_diff))
    assert "切り詰めた" in t2.body
    plain = tickets.build({"revision_id": "r", "enforcement_status": "Repeal"}, "", None)
    assert plain.labels == ["影響度:Low", "施行:廃止", "種別:一部改正"] and "（未分類）" in plain.body
    with pytest.raises(TicketError, match="no revision_id"):
        tickets.build({}, "", None)


def test_build_from_markdown_and_label_color() -> None:
    rec = record()
    t = tickets.build_from_markdown(generator.render_markdown(rec))
    assert t.revision_id == rec.event.revision_id
    assert tickets.label_color("影響度:High") == "b60205"
    assert tickets.label_color("分野:知財") == "0e8a9b"
    assert tickets.label_color("謎") == "ededed"


# ------------------------------------------------------------------ client
def test_client_creates_labels_and_issue_then_updates() -> None:
    gh = FakeGitHub(labels=["影響度:High"])
    client = make_client(gh)
    t = tickets.build_from_markdown(generator.render_markdown(record()))
    state = state_mod.State()
    url = tickets.sync(client, state, t)
    assert url == f"https://github.com/{REPO}/issues/1"
    assert state.tickets[t.revision_id] == 1
    assert gh.labels >= set(t.labels)
    created_labels = [c for c in gh.calls if c == ("POST", f"/repos/{REPO}/labels")]
    assert len(created_labels) == 3  # 影響度:High は既存
    # 2 回目は PATCH で、ラベル一覧は再取得しない
    before = len(gh.calls)
    assert tickets.sync(client, state, t) == url
    assert ("PATCH", f"/repos/{REPO}/issues/1") in gh.calls[before:]
    assert ("GET", f"/repos/{REPO}/labels?per_page=100&page=1") not in gh.calls[before:]
    client.close()


def test_client_label_pagination() -> None:
    gh = FakeGitHub(labels=[f"l{i}" for i in range(100)])
    gh.label_pages = 2
    client = make_client(gh)
    assert client._ensure_labels(["l1", "new"]) == 1
    assert ("GET", f"/repos/{REPO}/labels?per_page=100&page=2") in gh.calls


def test_client_retries_rate_limit_and_connection_error() -> None:
    gh = FakeGitHub(rate_limit_once=True, raise_once=True)
    client = make_client(gh)
    t = tickets.build_from_markdown(generator.render_markdown(record()))
    number, _ = client.upsert(t, None)
    assert number == 1


@pytest.mark.parametrize(("fail", "message"), [([500, 502, 503], "after 3 attempts"), ([422], "failed: 422")])
def test_client_errors(fail: list[int], message: str) -> None:
    client = make_client(FakeGitHub(fail=fail))
    with pytest.raises(TicketError, match=message):
        client.upsert(tickets.Ticket("r", "t", "b"), None)


def test_client_requires_owner_slash_repo() -> None:
    with pytest.raises(TicketError, match="owner/name"):
        tickets.GitHubIssues("bad", "token")


# ------------------------------------------------------------- set_issue_url
def test_set_issue_url(tmp_path: Path) -> None:
    path = generator.write(record(), tmp_path)
    tickets.set_issue_url(path, "https://github.com/x/y/issues/1")
    text = path.read_text(encoding="utf-8")
    assert "\nissue_url: https://github.com/x/y/issues/1\n---\n" in text and "\r" not in text
    tickets.set_issue_url(path, "https://github.com/x/y/issues/2")
    text = path.read_text(encoding="utf-8")
    assert text.count("issue_url:") == 1 and "issues/2" in text
    legacy = tmp_path / "legacy.md"
    legacy.write_text("---\ntitle: x\negov_url: u\n---\n\nbody\n", encoding="utf-8")
    tickets.set_issue_url(legacy, "https://github.com/x/y/issues/3")
    assert legacy.read_text(encoding="utf-8") == "---\ntitle: x\negov_url: u\nissue_url: https://github.com/x/y/issues/3\n---\n\nbody\n"
    bad = tmp_path / "bad.md"
    bad.write_text("---\nno end", encoding="utf-8")
    with pytest.raises(TicketError, match="no frontmatter"):
        tickets.set_issue_url(bad, "u")


# ---------------------------------------------------------------- backfill
def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    base = {"content_dir": tmp_path / "content", "state_file": tmp_path / "state.json", "issues_repo": REPO, "issues_token": "t", "site_url": "https://law.example"}
    return Settings(**{**base, **overrides})


def test_backfill_creates_then_skips_then_updates(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    p1 = generator.write(record(), settings.content_dir)
    generator.write(record(event_id="500AC0000000001_20280101_508AC0000000099", revision_id="500AC0000000001_20280101_508AC0000000099", enforcement_date="2028-01-01"), settings.content_dir)
    gh = FakeGitHub()
    done, failed, skipped = tickets_backfill.backfill(settings, limit=None, update=False, client=make_client(gh))
    assert (done, failed, skipped) == (2, 0, 0)
    assert "issue_url: https://github.com/owner/laws/issues/" in p1.read_text(encoding="utf-8")
    assert "- 記事: https://law.example/amendments/" in gh.issues[1]["body"]
    assert len(state_mod.load(settings.state_file).tickets) == 2
    assert tickets_backfill.backfill(settings, limit=None, update=False, client=make_client(gh)) == (0, 0, 2)
    assert tickets_backfill.backfill(settings, limit=1, update=True, client=make_client(gh)) == (1, 0, 0)


def test_backfill_counts_failures_and_requires_repo(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, site_url="")
    generator.write(record(), settings.content_dir)
    gh = FakeGitHub(fail=[422])
    assert tickets_backfill.backfill(settings, limit=None, update=False, client=make_client(gh)) == (0, 1, 0)
    with pytest.raises(PipelineError, match="LAWTRACK_ISSUES_REPO"):
        tickets_backfill.backfill(settings_for(tmp_path, issues_repo=""), limit=None, update=False)


def test_backfill_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings = settings_for(tmp_path)
    generator.write(record(), settings.content_dir)
    monkeypatch.setattr(tickets_backfill.Settings, "from_env", classmethod(lambda cls: settings))
    fake = make_client(FakeGitHub())
    monkeypatch.setattr(tickets_backfill.tickets, "GitHubIssues", lambda *a, **k: fake)
    assert tickets_backfill.main(["--env-file", "tests/.no-such-env"]) == 0
    assert "tickets=1 failed=0 skipped=0" in capsys.readouterr().out
    monkeypatch.setattr(tickets_backfill.Settings, "from_env", classmethod(lambda cls: settings_for(tmp_path, issues_repo="")))
    assert tickets_backfill.main(["--env-file", "tests/.no-such-env"]) == 1


# ------------------------------------------------------------------ config
def test_settings_issue_validation() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        Settings(issues_repo="bad", issues_token="t")
    with pytest.raises(ValueError, match="requires LAWTRACK_ISSUES_TOKEN"):
        Settings(issues_repo="a/b")
    with pytest.raises(ValueError, match="ISSUES_INTERVAL"):
        Settings(issues_repo="a/b", issues_token="t", issues_interval_sec=-1)
    assert Settings(issues_repo="a/b", issues_token="t").issues_interval_sec == 1.0
