"""catalog（MCP サーバのデータ源と問い合わせ）のテスト。ローカル Markdown と公開 JSON 索引の両方を、ネットワークに出ずに検証する。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from pipeline import catalog, generator
from pipeline.catalog import AmendmentRecord, CatalogError, LocalCatalog, RemoteCatalog
from pipeline.models import Amendment, EnforcementStatus, LawRef, Summary
from tests.conftest import make_event
from tests.test_models import valid_summary

SITE = "https://law.example.test"


def _record(law_id: str, promulgated: date, enforced: date | None, status: EnforcementStatus, *, title: str = "試験法", **summary: object) -> Amendment:
    rid = f"{law_id}_{promulgated:%Y%m%d}_508AC0000000056"
    event = make_event(
        event_id=rid,
        revision_id=rid,
        law=LawRef(law_id=law_id, law_num="令和八年法律第一号", law_title=title, egov_category="電気通信"),
        promulgation_date=promulgated,
        enforcement_date=enforced,
        enforcement_status=status,
    )
    base = valid_summary(enforcement_date=enforced.isoformat() if enforced else "不明（政令委任）", **summary)
    return Amendment(event=event, summary=Summary.model_validate(base), generated_at="2026-11-12T00:00:00+00:00", provider="template")


@pytest.fixture
def content_dir(tmp_path: Path) -> Path:
    d = tmp_path / "content"
    records = [
        _record("500AC0000000001", date(2026, 7, 17), date(2027, 1, 1), EnforcementStatus.UNENFORCED, impact_level="High"),
        _record("500AC0000000001", date(2025, 5, 1), date(2025, 6, 1), EnforcementStatus.PREVIOUS, impact_level="Low", summary_points=["帳簿の保存期間を延長", "なぜ", "どう"]),
        _record("500AC0000000001", date(2026, 3, 1), None, EnforcementStatus.UNENFORCED, impact_level="Medium"),
        _record("600AC0000000002", date(2026, 6, 1), date(2026, 6, 1), EnforcementStatus.CURRENT, title="労働試験法", categories=["人事・労務"], impact_level="Medium", target_audience=["人事部"]),
    ]
    for r in records:
        generator.write(r, d)
    (d / "README.txt").write_text("not markdown", encoding="utf-8")
    return d


@pytest.fixture
def local(content_dir: Path) -> LocalCatalog:
    return LocalCatalog(content_dir, site_url=SITE)


# ---- parse / local ---------------------------------------------------------


def test_local_loads_only_markdown_and_builds_urls(local: LocalCatalog) -> None:
    recs = local.records()
    assert len(recs) == 4
    assert all(isinstance(r, AmendmentRecord) for r in recs)
    first = next(r for r in recs if r.promulgation_date == "2026-07-17")
    assert first.slug == "500ac0000000001-20260717-508ac0000000056"
    assert first.url == f"{SITE}/amendments/{first.slug}/"
    assert first.enforcement_date == "2027-01-01" and first.impact_level == "High"
    # 2 回目はキャッシュ（ファイルを消しても同じ結果）
    for p in local.content_dir.glob("*.md"):
        p.unlink()
    assert len(local.records()) == 4


def test_local_body_strips_diff_by_default(local: LocalCatalog) -> None:
    slug = "500ac0000000001-20260717-508ac0000000056"
    body = local.body(slug)
    assert body.startswith("## 変更の概要") and "条文差分" not in body
    full = local.body(slug, include_diff=True)
    assert "## 条文差分（機械抽出）" in full and "```diff" in full


def test_local_body_unknown_slug(local: LocalCatalog) -> None:
    with pytest.raises(CatalogError, match="not found"):
        local.body("nope")


def test_local_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="content dir"):
        LocalCatalog(tmp_path / "missing", site_url=SITE).records()


def test_parse_markdown_rejects_missing_frontmatter() -> None:
    with pytest.raises(CatalogError, match="frontmatter"):
        catalog.parse_markdown("no frontmatter here", slug="x", site_url=SITE)


def test_parse_markdown_coerces_dates_and_empty_site() -> None:
    text = (
        "---\n"
        "title: 試験法\nlaw_id: A\nlaw_num: n\nlaw_type: Act\nrevision_id: r\namendment_type: 一部改正\n"
        "promulgation_date: 2026-07-17\nenforcement_date: 2027-01-01\nenforcement_note: '2027-01-01'\nenforcement_status: UnEnforced\n"
        "categories: [知財]\nimpact_level: Low\ntarget_audience: [a]\nsummary_points: [x, y, z]\negov_url: https://laws.e-gov.go.jp/law/A\n"
        "---\n\nbody\n"
    )
    rec, body = catalog.parse_markdown(text, slug="s", site_url="")
    assert rec.promulgation_date == "2026-07-17" and rec.enforcement_date == "2027-01-01"  # YAML の date を ISO 文字列に戻す
    assert rec.url == "/amendments/s/"
    assert body == "body"


def test_parse_markdown_invalid_schema() -> None:
    with pytest.raises(CatalogError, match="invalid frontmatter"):
        catalog.parse_markdown("---\ntitle: x\n---\nbody\n", slug="s", site_url=SITE)


# ---- queries ---------------------------------------------------------------


def test_search_filters_and_sorts(local: LocalCatalog) -> None:
    recs = local.records()
    all_ = catalog.search(recs)
    assert [r.promulgation_date for r in all_] == ["2026-07-17", "2026-06-01", "2026-03-01", "2025-05-01"]
    assert [r.law_id for r in catalog.search(recs, query="労働")] == ["600AC0000000002"]
    assert len(catalog.search(recs, query="帳簿の保存期間")) == 1  # 要旨にも当たる
    assert len(catalog.search(recs, query="人事部")) == 1  # 対象組織にも当たる
    assert len(catalog.search(recs, category="人事・労務")) == 1
    assert len(catalog.search(recs, category="hr-labor")) == 1  # slug でも可
    assert len(catalog.search(recs, impact="high")) == 1  # 大文字小文字を問わない
    assert len(catalog.search(recs, status="UnEnforced")) == 2
    assert len(catalog.search(recs, law_id="500AC0000000001")) == 3
    assert [r.promulgation_date for r in catalog.search(recs, since="2026-06-01", until="2026-06-01")] == ["2026-06-01"]  # 境界は含む
    assert len(catalog.search(recs, since="2026-06-02")) == 1
    assert len(catalog.search(recs, until="2026-05-31")) == 2
    assert len(catalog.search(recs, limit=2)) == 2


@pytest.mark.parametrize(
    ("kwargs", "msg"),
    [
        ({"category": "宇宙"}, "unknown category"),
        ({"impact": "Huge"}, "impact"),
        ({"status": "Soon"}, "status"),
        ({"since": "2026/01/01"}, "YYYY-MM-DD"),
        ({"until": "yesterday"}, "YYYY-MM-DD"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
    ],
)
def test_search_rejects_bad_filters(local: LocalCatalog, kwargs: dict[str, object], msg: str) -> None:
    with pytest.raises(ValueError, match=msg):
        catalog.search(local.records(), **kwargs)  # type: ignore[arg-type]


def test_upcoming_window_and_undetermined(local: LocalCatalog) -> None:
    recs = local.records()
    today = date(2026, 12, 1)
    res = catalog.upcoming(recs, today=today, days=31)
    assert [r.enforcement_date for r in res.items] == ["2027-01-01"]  # 31 日後ちょうどは含む
    assert res.undetermined == 1  # 施行日が政令委任のもの
    assert catalog.upcoming(recs, today=today, days=30).items == []  # 直前は含まない
    assert catalog.upcoming(recs, today=date(2027, 1, 1), days=0).items[0].enforcement_date == "2027-01-01"  # 当日
    assert catalog.upcoming(recs, today=date(2027, 1, 2), days=365).items == []  # 過ぎたものは出ない
    assert catalog.upcoming(recs, today=today, days=31, category="人事・労務").items == []
    with pytest.raises(ValueError, match="days"):
        catalog.upcoming(recs, today=today, days=-1)


def test_lineage_sorted_by_promulgation_then_enforcement(local: LocalCatalog) -> None:
    chain = catalog.lineage(local.records(), "500AC0000000001")
    assert [r.promulgation_date for r in chain] == ["2025-05-01", "2026-03-01", "2026-07-17"]
    assert catalog.lineage(local.records(), "nope") == []


def test_laws_and_categories(local: LocalCatalog) -> None:
    laws = catalog.laws(local.records())
    assert [(x.law_id, x.count) for x in laws] == [("500AC0000000001", 3), ("600AC0000000002", 1)]
    assert laws[0].latest_promulgation == "2026-07-17" and laws[0].primary_category == "情報セキュリティ・IT"
    assert laws[0].unenforced == 2 and laws[1].unenforced == 0
    cats = catalog.categories(local.records())
    assert [(c.name, c.slug, c.count) for c in cats] == [
        ("情報セキュリティ・IT", "it-security", 3),
        ("人事・労務", "hr-labor", 1),
        ("税務・会計", "tax-accounting", 0),
        ("企業法務・コンプライアンス", "legal-compliance", 0),
        ("知財", "ip", 0),
        ("その他", "other", 0),
    ]


def test_primary_category_tie_breaks_by_catalog_order() -> None:
    a = _record("700AC0000000003", date(2026, 1, 1), date(2026, 1, 1), EnforcementStatus.CURRENT, categories=["知財"])
    b = _record("700AC0000000003", date(2026, 2, 1), date(2026, 2, 1), EnforcementStatus.CURRENT, categories=["人事・労務"])
    recs = [catalog.parse_markdown(generator.render_markdown(r), slug=r.slug, site_url=SITE)[0] for r in (a, b)]
    assert catalog.laws(recs)[0].primary_category == "人事・労務"  # 同数なら CATEGORIES の並び順


# ---- remote ----------------------------------------------------------------


def _remote(local: LocalCatalog, handler: object) -> RemoteCatalog:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return RemoteCatalog(SITE, raw_base="https://raw.example.test/content", client=client)


def test_remote_index_and_body(local: LocalCatalog, content_dir: Path) -> None:
    index = [r.model_dump() for r in local.records()]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path == "/api/amendments.json":
            return httpx.Response(200, json=index)
        if req.url.host == "raw.example.test":
            name = req.url.path.rsplit("/", 1)[-1]
            p = content_dir / name
            return httpx.Response(200, text=p.read_text(encoding="utf-8")) if p.exists() else httpx.Response(404)
        return httpx.Response(500)  # pragma: no cover

    remote = _remote(local, handler)
    assert len(remote.records()) == 4
    assert len(remote.records()) == 4 and calls.count(f"{SITE}/api/amendments.json") == 1  # 索引はキャッシュ
    slug = "500ac0000000001-20260717-508ac0000000056"
    assert remote.body(slug).startswith("## 変更の概要")
    assert "```diff" in remote.body(slug, include_diff=True)
    with pytest.raises(CatalogError, match="404"):
        remote.body("nope")


def test_remote_index_errors(local: LocalCatalog) -> None:
    with pytest.raises(CatalogError, match="503"):
        _remote(local, lambda req: httpx.Response(503)).records()
    with pytest.raises(CatalogError, match="not JSON"):
        _remote(local, lambda req: httpx.Response(200, text="<html>")).records()
    with pytest.raises(CatalogError, match="invalid"):
        _remote(local, lambda req: httpx.Response(200, json=[{"title": "x"}])).records()

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    with pytest.raises(CatalogError, match="down"):
        _remote(local, boom).records()


def test_remote_default_client_and_slug_safety() -> None:
    remote = RemoteCatalog(SITE + "/")
    assert remote.index_url == f"{SITE}/api/amendments.json"
    with pytest.raises(CatalogError, match="slug"):
        remote.body("../etc/passwd")


def test_record_json_roundtrip(local: LocalCatalog) -> None:
    """サイトが出す索引（JSON）と Python 側の型が同じ項目を持つ。"""
    rec = local.records()[0]
    again = AmendmentRecord.model_validate(json.loads(rec.model_dump_json()))
    assert again == rec
