"""法改正トラッカーの MCP サーバ。

公開済みの改正記事を、MCP クライアント（Claude Desktop / Claude Code など）から検索・参照できるようにする。
ツールのロジックは `LawTrackTools`（テスト可能なプレーンなクラス）に置き、MCP への登録は薄く保つ。

データ源（`LAWTRACK_MCP_SOURCE`）:
- `local`（既定）: リポジトリ内の Markdown（`LAWTRACK_CONTENT_DIR`）を読む。
- サイト URL（例 https://law.quietforensics.com）: 公開索引 `/api/amendments.json` と GitHub raw の Markdown を読む。クローン不要。

不変条件: 観測事実（e-Gov の値）と LLM の推定（分野・影響度・要旨）を別のキー（`facts` / `llm_estimate`）で返し、
すべての応答に免責（機械生成・法的助言ではない・官報が優先）を付ける。

起動:
    python -m pipeline.mcp_server      # stdio
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import date
from typing import Any

from pipeline import catalog
from pipeline.catalog import AmendmentRecord, Catalog, CatalogError, LocalCatalog, RemoteCatalog
from pipeline.config import Settings

SERVER_NAME = "law-amendment-tracker"
DISCLAIMER = "分野・影響度・要旨は LLM による機械生成の推定であり法的助言ではない。条文・日付は e-Gov 法令 API の値であり、官報が優先する。"
INSTRUCTIONS = (
    "日本の法律（e-Gov 法令 API）の改正履歴を検索・参照するツール。"
    "search_amendments で絞り込み、get_amendment で 1 件の詳細（事実と LLM 推定を分けて返す）、"
    "list_upcoming で施行予定、law_lineage で同一法令の系譜、list_laws / list_categories で収録範囲を確認できる。"
    f"{DISCLAIMER}"
)

_ROW_KEYS = (
    "slug", "url", "title", "law_id", "law_num", "amendment_type", "amending_law_title",
    "promulgation_date", "enforcement_date", "enforcement_note", "enforcement_status",
    "categories", "impact_level", "summary_points", "egov_url", "issue_url",
)
_FACT_KEYS = (
    "title", "law_id", "law_num", "law_type", "revision_id", "prev_revision_id", "amendment_type",
    "amending_law_id", "amending_law_title", "amending_law_num", "promulgation_date", "enforcement_date",
    "enforcement_status", "egov_url",
)
_ESTIMATE_KEYS = ("categories", "impact_level", "target_audience", "summary_points", "enforcement_note")


def _row(r: AmendmentRecord) -> dict[str, Any]:
    d = r.model_dump()
    return {k: d[k] for k in _ROW_KEYS}


class LawTrackTools:
    """MCP ツールの実体。Catalog だけに依存する。"""

    def __init__(self, source: Catalog, *, today: Callable[[], date] = date.today) -> None:
        self._cat = source
        self._today = today

    def search_amendments(
        self,
        query: str = "",
        category: str | None = None,
        impact: str | None = None,
        status: str | None = None,
        law_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """改正記事を検索する。query は法令名・改正法名・3 行要旨・対象組織に部分一致。category は分野名か slug、impact は High/Medium/Low、status は CurrentEnforced/UnEnforced/PreviousEnforced/Repeal、since/until は公布日 (YYYY-MM-DD)。公布日の新しい順。"""
        rows = catalog.search(self._cat.records(), query=query, category=category, impact=impact, status=status, law_id=law_id, since=since, until=until, limit=limit)
        return {"total": len(rows), "items": [_row(r) for r in rows], "disclaimer": DISCLAIMER}

    def get_amendment(self, slug_or_revision_id: str, include_diff: bool = False) -> dict[str, Any]:
        """改正 1 件の詳細。facts（e-Gov の観測事実）と llm_estimate（分野・影響度・要旨・本文）を分けて返す。include_diff=true で条文差分（最大 6 万字）も本文に含める。"""
        rec = next((r for r in self._cat.records() if slug_or_revision_id in (r.slug, r.revision_id)), None)
        if rec is None:
            raise CatalogError(f"amendment not found: {slug_or_revision_id}")
        d = rec.model_dump()
        facts = {k: d[k] for k in _FACT_KEYS}
        estimate = {k: d[k] for k in _ESTIMATE_KEYS}
        estimate["body_markdown"] = self._cat.body(rec.slug, include_diff=include_diff)
        return {"slug": rec.slug, "url": rec.url, "issue_url": rec.issue_url, "facts": facts, "llm_estimate": estimate,
                "provider": rec.provider, "generated_at": rec.generated_at, "disclaimer": DISCLAIMER}

    def list_upcoming(self, days: int = 180, category: str | None = None) -> dict[str, Any]:
        """今日から days 日以内に施行される未施行の改正を施行日順に返す。施行日が政令委任で未定の件数は undetermined。"""
        res = catalog.upcoming(self._cat.records(), today=self._today(), days=days, category=category)
        return {"from": res.start, "to": res.end, "total": len(res.items), "items": [_row(r) for r in res.items], "undetermined": res.undetermined, "disclaimer": DISCLAIMER}

    def law_lineage(self, law_id: str) -> dict[str, Any]:
        """同じ法令（e-Gov law_id）の改正履歴を公布日順に返す。段階施行は同じ公布日で施行日が異なる複数件になる。"""
        chain = catalog.lineage(self._cat.records(), law_id)
        latest = chain[-1] if chain else None
        return {"law_id": law_id, "title": latest.title if latest else None, "law_num": latest.law_num if latest else None,
                "total": len(chain), "revisions": [_row(r) for r in chain], "disclaimer": DISCLAIMER}

    def list_laws(self, category: str | None = None) -> dict[str, Any]:
        """収録している法令の一覧（件数の多い順）。category で主分野を絞れる。"""
        items = catalog.laws(self._cat.records(), category=category)
        return {"total": len(items), "items": [x.__dict__ for x in items]}

    def list_categories(self) -> dict[str, Any]:
        """分野の一覧（名前・slug・記事数）。"""
        return {"items": [c.__dict__ for c in catalog.categories(self._cat.records())]}

    def as_list(self) -> list[Any]:
        return [self.search_amendments, self.get_amendment, self.list_upcoming, self.law_lineage, self.list_laws, self.list_categories]


def build_catalog(settings: Settings) -> Catalog:
    if settings.mcp_source == "local":
        return LocalCatalog(settings.content_dir, site_url=settings.site_url)
    return RemoteCatalog(settings.mcp_source, raw_base=settings.mcp_raw_base, timeout_sec=settings.http_timeout_sec)


def build_server(catalog_: Catalog | None = None, settings: Settings | None = None):  # type: ignore[no-untyped-def]
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    source = catalog_ or build_catalog(settings or Settings.from_env())
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    for fn in LawTrackTools(source).as_list():
        server.add_tool(_passthrough_errors(fn, ToolError))
    return server


def _passthrough_errors(fn: Callable[..., Any], tool_error: type[Exception]) -> Callable[..., Any]:
    """CatalogError / ValueError（入力不正・該当なし）の文言をクライアントに返す。素の例外だと SDK が理由を隠して「Error executing tool」にする。"""

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (CatalogError, ValueError) as exc:
            raise tool_error(str(exc)) from exc

    return wrapped


def main() -> None:  # pragma: no cover - stdio ランタイム
    from pathlib import Path

    from pipeline.config import load_dotenv

    load_dotenv(Path(".env"))
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
