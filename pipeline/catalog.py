"""公開済み記事（frontmatter 付き Markdown）の読み取りと問い合わせ。MCP サーバのデータ源。

データ源は 2 通りで、どちらも同じ `AmendmentRecord` を返す。
- `LocalCatalog`: リポジトリ内の `frontend/src/content/amendments/*.md` を直接読む（クローン済みの環境向け）。
- `RemoteCatalog`: 公開サイトの索引 `/api/amendments.json` と、GitHub の raw Markdown を読む（クローン不要）。

frontmatter は観測事実（e-Gov の値）と LLM の推定（分野・影響度・要旨）が同居しているので、
MCP ツール側で分けて返す（`mcp_server.LawTrackTools.get_amendment`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from pipeline.errors import PipelineError
from pipeline.models import CATEGORIES, CATEGORY_SLUGS

IMPACTS = ("High", "Medium", "Low")
STATUSES = ("CurrentEnforced", "UnEnforced", "PreviousEnforced", "Repeal")
DIFF_HEADING = "## 条文差分（機械抽出）"
MAX_LIMIT = 200
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG = re.compile(r"^[a-z0-9-]+$")


class CatalogError(PipelineError):
    """記事の読み取り失敗（ファイル・索引・raw 取得・スキーマ不一致）。"""


class AmendmentRecord(BaseModel):
    """記事 1 件の索引項目。frontmatter と同じ項目に `slug` と `url` を足したもの。本文は含まない。"""

    model_config = ConfigDict(extra="ignore")

    slug: str
    url: str
    title: str
    law_id: str
    law_num: str
    law_type: str
    revision_id: str
    prev_revision_id: str | None = None
    amendment_type: str
    amending_law_id: str | None = None
    amending_law_title: str | None = None
    amending_law_num: str | None = None
    promulgation_date: str
    enforcement_date: str | None = None
    enforcement_note: str = ""
    enforcement_status: str
    categories: list[str]
    category_slugs: list[str] = []
    impact_level: str
    target_audience: list[str]
    summary_points: list[str]
    egov_url: str
    issue_url: str | None = None
    provider: str = ""
    generated_at: str = ""

    @field_validator("promulgation_date", "enforcement_date", "enforcement_note", "generated_at", mode="before")
    @classmethod
    def _dates_to_str(cls, value: object) -> object:
        """YAML が引用符なしの日付を date にしてしまう場合に備えて ISO 文字列へ戻す。"""
        return value.isoformat() if isinstance(value, date) else value


def _url(site_url: str, slug: str) -> str:
    return f"{site_url.rstrip('/')}/amendments/{slug}/"


def parse_markdown(text: str, *, slug: str, site_url: str) -> tuple[AmendmentRecord, str]:
    """frontmatter 付き Markdown を (索引項目, 本文) に分ける。本文は frontmatter の後ろ全部（条文差分の節を含む）。"""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise CatalogError(f"{slug}: frontmatter not found")
    head, body = text[4:].split("\n---\n", 1)
    data = yaml.safe_load(head) or {}
    try:
        record = AmendmentRecord.model_validate({**data, "slug": slug, "url": _url(site_url, slug)})
    except ValidationError as exc:
        raise CatalogError(f"{slug}: invalid frontmatter: {exc.error_count()} error(s)") from exc
    return record, body.strip()


def strip_diff(body: str) -> str:
    """本文から「条文差分（機械抽出）」の節を落とす（差分は最大 6 万字あり、要旨だけ欲しい用途には重い）。"""
    idx = body.find(DIFF_HEADING)
    return body if idx < 0 else body[:idx].rstrip()


class Catalog(Protocol):  # pragma: no cover - 実装を持たない型（本体は `...` だけ）
    def records(self) -> list[AmendmentRecord]: ...

    def body(self, slug: str, *, include_diff: bool = False) -> str: ...


class LocalCatalog:
    """リポジトリ内の Markdown を読む。初回に全件を読み、以後はキャッシュ。"""

    def __init__(self, content_dir: Path, *, site_url: str = "") -> None:
        self.content_dir = Path(content_dir)
        self.site_url = site_url
        self._records: list[AmendmentRecord] | None = None
        self._bodies: dict[str, str] = {}

    def records(self) -> list[AmendmentRecord]:
        if self._records is None:
            if not self.content_dir.is_dir():
                raise CatalogError(f"content dir not found: {self.content_dir}")
            out: list[AmendmentRecord] = []
            for path in sorted(self.content_dir.glob("*.md")):
                record, body = parse_markdown(path.read_text(encoding="utf-8"), slug=path.stem, site_url=self.site_url)
                out.append(record)
                self._bodies[record.slug] = body
            self._records = out
        return self._records

    def body(self, slug: str, *, include_diff: bool = False) -> str:
        self.records()
        if slug not in self._bodies:
            raise CatalogError(f"amendment not found: {slug}")
        text = self._bodies[slug]
        return text if include_diff else strip_diff(text)


class RemoteCatalog:
    """公開サイトの索引と GitHub raw の Markdown を読む。索引はプロセス内でキャッシュ。"""

    DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/takker-hero-se/law-amendment-tracker/main/frontend/src/content/amendments"

    def __init__(self, site_url: str, *, raw_base: str = DEFAULT_RAW_BASE, client: httpx.Client | None = None, timeout_sec: float = 30.0) -> None:
        self.site_url = site_url.rstrip("/")
        self.index_url = f"{self.site_url}/api/amendments.json"
        self.raw_base = raw_base.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_sec, follow_redirects=True, headers={"User-Agent": "law-amendment-tracker-mcp"})
        self._records: list[AmendmentRecord] | None = None

    def _get(self, url: str) -> httpx.Response:
        try:
            res = self._client.get(url)
        except httpx.HTTPError as exc:
            raise CatalogError(f"GET {url} failed: {exc}") from exc
        if res.status_code != 200:
            raise CatalogError(f"GET {url} returned {res.status_code}")
        return res

    def records(self) -> list[AmendmentRecord]:
        if self._records is None:
            res = self._get(self.index_url)
            try:
                raw = res.json()
            except ValueError as exc:
                raise CatalogError(f"{self.index_url}: not JSON") from exc
            try:
                self._records = [AmendmentRecord.model_validate(item) for item in raw]
            except (ValidationError, TypeError) as exc:
                raise CatalogError(f"{self.index_url}: invalid index: {exc}") from exc
        return self._records

    def body(self, slug: str, *, include_diff: bool = False) -> str:
        if not _SLUG.fullmatch(slug):
            raise CatalogError(f"invalid slug: {slug!r}")
        text = self._get(f"{self.raw_base}/{slug}.md").text
        _, body = parse_markdown(text, slug=slug, site_url=self.site_url)
        return body if include_diff else strip_diff(body)


# ---- 問い合わせ（純関数） -----------------------------------------------------


def _category_name(value: str) -> str:
    if value in CATEGORIES:
        return value
    for name, slug in CATEGORY_SLUGS.items():
        if slug == value:
            return name
    raise ValueError(f"unknown category: {value!r} (use one of {list(CATEGORIES)} or slugs {list(CATEGORY_SLUGS.values())})")


def _iso(name: str, value: str) -> str:
    if not _ISO.fullmatch(value):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {value!r}")
    return value


def _sort_key(r: AmendmentRecord) -> tuple[str, str]:
    return (r.promulgation_date, r.title)


def search(
    records: list[AmendmentRecord],
    *,
    query: str = "",
    category: str | None = None,
    impact: str | None = None,
    status: str | None = None,
    law_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> list[AmendmentRecord]:
    """絞り込み → 公布日の新しい順 → 上位 limit 件。閾値（since/until）は両端を含む。"""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be 1..{MAX_LIMIT}")
    cat = _category_name(category) if category else None
    if impact is not None:
        impact = impact.capitalize()
        if impact not in IMPACTS:
            raise ValueError(f"impact must be one of {IMPACTS}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    lo = _iso("since", since) if since else None
    hi = _iso("until", until) if until else None
    needle = query.strip().lower()

    def hit(r: AmendmentRecord) -> bool:
        if cat and cat not in r.categories:
            return False
        if impact and r.impact_level != impact:
            return False
        if status and r.enforcement_status != status:
            return False
        if law_id and r.law_id != law_id:
            return False
        if lo and r.promulgation_date < lo:
            return False
        if hi and r.promulgation_date > hi:
            return False
        if needle:
            hay = " ".join([r.title, r.law_num, r.amending_law_title or "", *r.summary_points, *r.target_audience]).lower()
            return needle in hay
        return True

    return sorted((r for r in records if hit(r)), key=_sort_key, reverse=True)[:limit]


@dataclass(frozen=True)
class Upcoming:
    start: str
    end: str
    items: list[AmendmentRecord]
    undetermined: int


def upcoming(records: list[AmendmentRecord], *, today: date, days: int = 180, category: str | None = None) -> Upcoming:
    """今日から days 日後まで（両端を含む）に施行される未施行の改正。施行日が未定（政令委任）の件数も返す。"""
    if days < 0:
        raise ValueError("days must be >= 0")
    cat = _category_name(category) if category else None
    start, end = today.isoformat(), (today + timedelta(days=days)).isoformat()
    pool = [r for r in records if r.enforcement_status == "UnEnforced" and (cat is None or cat in r.categories)]
    items = sorted((r for r in pool if r.enforcement_date and start <= r.enforcement_date <= end), key=lambda r: (r.enforcement_date or "", r.title))
    undetermined = sum(1 for r in pool if r.enforcement_date is None)
    return Upcoming(start, end, items, undetermined)


def lineage(records: list[AmendmentRecord], law_id: str) -> list[AmendmentRecord]:
    """同じ法令の履歴を公布日 → 施行日の順に並べる（変遷図の「系譜」と同じ順）。"""
    return sorted((r for r in records if r.law_id == law_id), key=lambda r: (r.promulgation_date, r.enforcement_date or "9999-12-31"))


@dataclass(frozen=True)
class LawSummary:
    law_id: str
    title: str
    law_num: str
    count: int
    unenforced: int
    latest_promulgation: str
    primary_category: str


def _mode_category(values: list[str]) -> str:
    counts = {c: values.count(c) for c in set(values)}
    return max(counts, key=lambda c: (counts[c], -CATEGORIES.index(c)))


def laws(records: list[AmendmentRecord], *, category: str | None = None) -> list[LawSummary]:
    """法令ごとの件数と最新公布日。分野は履歴ごとの第 1 分野の最頻値（同数なら CATEGORIES の並び順）。"""
    cat = _category_name(category) if category else None
    groups: dict[str, list[AmendmentRecord]] = {}
    for r in records:
        groups.setdefault(r.law_id, []).append(r)
    out: list[LawSummary] = []
    for law_id, items in groups.items():
        primary = _mode_category([r.categories[0] for r in items])
        if cat and primary != cat:
            continue
        latest = max(items, key=_sort_key)
        out.append(LawSummary(law_id, latest.title, latest.law_num, len(items), sum(1 for r in items if r.enforcement_status == "UnEnforced"), latest.promulgation_date, primary))
    return sorted(out, key=lambda x: (-x.count, x.law_id))


@dataclass(frozen=True)
class CategoryCount:
    name: str
    slug: str
    count: int


def categories(records: list[AmendmentRecord]) -> list[CategoryCount]:
    """分野ごとの記事数（1 記事が複数分野を持てば重複して数える）。"""
    return [CategoryCount(name, CATEGORY_SLUGS[name], sum(1 for r in records if name in r.categories)) for name in CATEGORIES]
