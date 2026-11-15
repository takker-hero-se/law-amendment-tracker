"""MCP サーバ（ツール層）のテスト。MCP の登録は薄く、ロジックは LawTrackTools に置く。"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from pipeline.catalog import CatalogError, LocalCatalog, RemoteCatalog
from pipeline.config import Settings
from pipeline.mcp_server import DISCLAIMER, LawTrackTools, build_catalog, build_server
from tests.test_catalog import SITE, content_dir, local  # noqa: F401  (fixtures)

SLUG = "500ac0000000001-20260717-508ac0000000056"


@pytest.fixture
def tools(local: LocalCatalog) -> LawTrackTools:  # noqa: F811
    return LawTrackTools(local, today=lambda: date(2026, 12, 1))


def test_search_amendments_compact_rows(tools: LawTrackTools) -> None:
    res = tools.search_amendments(query="試験", impact="High")
    assert res["total"] == 1 and len(res["items"]) == 1
    row = res["items"][0]
    assert row["slug"] == SLUG and row["url"].startswith(SITE)
    assert set(row) >= {"title", "law_id", "promulgation_date", "enforcement_date", "enforcement_status", "categories", "impact_level", "summary_points", "egov_url"}
    assert "body_markdown" not in row  # 一覧は軽く
    assert res["disclaimer"] == DISCLAIMER


def test_search_amendments_defaults_and_limit(tools: LawTrackTools) -> None:
    assert tools.search_amendments()["total"] == 4
    assert len(tools.search_amendments(limit=1)["items"]) == 1
    with pytest.raises(ValueError):
        tools.search_amendments(impact="Huge")


def test_get_amendment_separates_facts_and_estimate(tools: LawTrackTools) -> None:
    res = tools.get_amendment(SLUG)
    facts, est = res["facts"], res["llm_estimate"]
    assert facts["title"] == "試験法" and facts["promulgation_date"] == "2026-07-17" and facts["enforcement_status"] == "UnEnforced"
    assert facts["egov_url"].endswith("500AC0000000001")
    assert est["impact_level"] == "High" and est["categories"] == ["情報セキュリティ・IT"] and len(est["summary_points"]) == 3
    assert est["body_markdown"].startswith("## 変更の概要") and "条文差分" not in est["body_markdown"]
    assert res["url"].endswith(f"/{SLUG}/") and res["disclaimer"] == DISCLAIMER
    assert "```diff" in tools.get_amendment(SLUG, include_diff=True)["llm_estimate"]["body_markdown"]


def test_get_amendment_by_revision_id(tools: LawTrackTools) -> None:
    assert tools.get_amendment("500AC0000000001_20260717_508AC0000000056")["facts"]["revision_id"] == "500AC0000000001_20260717_508AC0000000056"
    with pytest.raises(CatalogError, match="not found"):
        tools.get_amendment("nope")


def test_list_upcoming(tools: LawTrackTools) -> None:
    res = tools.list_upcoming(days=31)
    assert res["from"] == "2026-12-01" and res["to"] == "2027-01-01"
    assert [i["enforcement_date"] for i in res["items"]] == ["2027-01-01"]
    assert res["undetermined"] == 1
    assert tools.list_upcoming(days=31, category="hr-labor")["items"] == []


def test_law_lineage(tools: LawTrackTools) -> None:
    res = tools.law_lineage("500AC0000000001")
    assert res["title"] == "試験法" and [r["promulgation_date"] for r in res["revisions"]] == ["2025-05-01", "2026-03-01", "2026-07-17"]
    assert res["revisions"][1]["enforcement_date"] is None
    empty = tools.law_lineage("nope")
    assert empty["revisions"] == [] and empty["title"] is None


def test_list_laws_and_categories(tools: LawTrackTools) -> None:
    laws = tools.list_laws()
    assert laws["total"] == 2 and laws["items"][0]["count"] == 3
    assert tools.list_laws(category="人事・労務")["total"] == 1
    cats = tools.list_categories()
    assert [c["slug"] for c in cats["items"]][:2] == ["it-security", "hr-labor"]
    assert cats["items"][0]["count"] == 3


def test_as_list_names(tools: LawTrackTools) -> None:
    assert [f.__name__ for f in tools.as_list()] == ["search_amendments", "get_amendment", "list_upcoming", "law_lineage", "list_laws", "list_categories"]


# ---- wiring ----------------------------------------------------------------


def test_build_catalog_local_and_remote(content_dir: Path) -> None:  # noqa: F811
    local_settings = Settings(content_dir=content_dir, site_url=SITE)
    cat = build_catalog(local_settings)
    assert isinstance(cat, LocalCatalog) and cat.content_dir == content_dir
    remote = build_catalog(Settings(mcp_source="https://law.example.test/"))
    assert isinstance(remote, RemoteCatalog) and remote.index_url == "https://law.example.test/api/amendments.json"


def test_build_server_registers_tools(local: LocalCatalog) -> None:  # noqa: F811
    server = build_server(catalog_=local)
    assert server.name == "law-amendment-tracker"
    assert "法的助言ではない" in (server.instructions or "")


def test_build_server_from_settings(content_dir: Path) -> None:  # noqa: F811
    server = build_server(settings=Settings(content_dir=content_dir, site_url=SITE))
    assert server.name == "law-amendment-tracker"


def test_server_lists_and_calls_tools(local: LocalCatalog) -> None:  # noqa: F811
    server = build_server(catalog_=local)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"search_amendments", "get_amendment", "list_upcoming", "law_lineage", "list_laws", "list_categories"} <= names
    result = asyncio.run(server.call_tool("law_lineage", {"law_id": "500AC0000000001"}))
    assert "2025-05-01" in str(result)
    # 入力不正・該当なしは理由つきのツールエラーになる（SDK 既定の「Error executing tool」で隠さない）
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="amendment not found: nope"):
        asyncio.run(server.call_tool("get_amendment", {"slug_or_revision_id": "nope"}))
    with pytest.raises(ToolError, match="impact must be one of"):
        asyncio.run(server.call_tool("search_amendments", {"impact": "Huge"}))


def test_settings_mcp_source_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().mcp_source == "local"
    assert Settings(mcp_source="https://x.test").mcp_source == "https://x.test"
    with pytest.raises(ValueError, match="LAWTRACK_MCP_SOURCE"):
        Settings(mcp_source="ftp://x")
    monkeypatch.setenv("LAWTRACK_MCP_SOURCE", "http://localhost:4321/")
    monkeypatch.setenv("LAWTRACK_MCP_RAW_BASE", "https://raw.test/x/")
    s = Settings.from_env()
    assert s.mcp_source == "http://localhost:4321" and s.mcp_raw_base == "https://raw.test/x"
