"""差分検知の取得元。

- `EgovSource`  e-Gov 法令 API。監視リスト（watchlist）の各法令について履歴を取り、
                 スナップショット（law_id → 最新履歴 ID）と比較して新しい履歴を改正イベントにする。
                 `scan_law_types` を与えると `/laws` の全件走査でも新履歴を拾う。
- `GitSource`   法令 1 本 = 1 ファイルのローカル Git ミラー（houritsu 形式: Markdown + YAML frontmatter）。
                 前回コミットと HEAD の差分をイベントにする。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import yaml

from pipeline.differ import diff_stats, unified_diff
from pipeline.egov import FAR_FUTURE, EgovClient
from pipeline.errors import SourceError
from pipeline.models import AmendmentEvent, EnforcementStatus, LawRef
from pipeline.state import State
from pipeline.textify import law_tree_to_text


class WatchEntry(dict[str, Any]):
    """watchlist.json の 1 行（law_id, law_title, category など）。"""


def load_watchlist(path: Path) -> list[WatchEntry]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SourceError(f"watchlist {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise SourceError(f"watchlist {path} must be a JSON list")
    entries: list[WatchEntry] = []
    for item in data:
        if not isinstance(item, dict) or "law_id" not in item:
            raise SourceError(f"watchlist {path} has an entry without law_id")
        entries.append(WatchEntry(item))
    return entries


class Source(Protocol):  # pragma: no cover - 型だけ
    name: str

    def detect(self, state: State, *, since: date | None, limit: int) -> list[AmendmentEvent]: ...


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise SourceError(f"bad date from e-Gov: {value!r}") from exc


def _law_ref(law_info: dict[str, Any], revision: dict[str, Any]) -> LawRef:
    return LawRef(
        law_id=str(law_info["law_id"]),
        law_num=str(law_info.get("law_num") or ""),
        law_title=str(revision.get("law_title") or law_info.get("law_id")),
        law_type=str(law_info.get("law_type") or "Act"),
        egov_category=revision.get("category"),
        abbrev=revision.get("abbrev"),
    )


def event_from_revision(law_info: dict[str, Any], revision: dict[str, Any], prev_revision_id: str | None) -> AmendmentEvent:
    """e-Gov の law_info + revision_info から本文なしのイベントを作る。"""
    promulgated = _parse_date(revision.get("amendment_promulgate_date")) or _parse_date(law_info.get("promulgation_date"))
    if promulgated is None:
        raise SourceError(f"revision {revision.get('law_revision_id')} has no promulgation date")
    status_raw = revision.get("current_revision_status") or "CurrentEnforced"
    try:
        status = EnforcementStatus(status_raw)
    except ValueError as exc:
        raise SourceError(f"unknown current_revision_status {status_raw!r}") from exc
    kind = AmendmentEvent.kind_from_egov(str(revision.get("amendment_type") or "3"))
    if revision.get("repeal_status") not in (None, "None"):
        kind = "廃止"
    revision_id = str(revision["law_revision_id"])
    return AmendmentEvent(
        event_id=revision_id,
        law=_law_ref(law_info, revision),
        revision_id=revision_id,
        prev_revision_id=prev_revision_id,
        amendment_kind=kind,
        amending_law_id=revision.get("amendment_law_id"),
        amending_law_title=revision.get("amendment_law_title"),
        amending_law_num=revision.get("amendment_law_num"),
        promulgation_date=promulgated,
        enforcement_date=_parse_date(revision.get("amendment_enforcement_date")),
        enforcement_comment=revision.get("amendment_enforcement_comment"),
        enforcement_status=status,
        source="egov",
    )


class EgovSource:
    name = "egov"

    def __init__(self, client: EgovClient, watchlist: Iterable[WatchEntry], *, scan_law_types: list[str] | None = None, scan_category_codes: list[str] | None = None) -> None:
        self._client = client
        self._watchlist = list(watchlist)
        self._scan_law_types = scan_law_types or []
        self._scan_category_codes = scan_category_codes or []

    def category_hint(self, law_id: str) -> str | None:
        for entry in self._watchlist:
            if entry["law_id"] == law_id:
                return entry.get("category")
        return None

    # -------------------------------------------------------------- detection
    def detect(self, state: State, *, since: date | None, limit: int) -> list[AmendmentEvent]:
        """新しい履歴を改正イベントにして返す。

        スナップショットは、その法令の候補をすべて今回処理できたときだけ進める。`limit` で
        切った残りや本文取得で失敗した候補は次回また検知される（処理済みは seen_revision_ids で除外）。
        """
        candidates: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
        latest: dict[str, str] = {}
        bootstrap = not state.snapshot
        for law_id in self._law_ids_to_check(state):
            found, newest = self._new_revisions_for(law_id, state, since=since, bootstrap=bootstrap)
            candidates.extend(found)
            if newest:
                latest[law_id] = newest
        candidates.sort(key=lambda c: (str(c[1].get("amendment_promulgate_date") or ""), c[1]["law_revision_id"]), reverse=True)
        batch = candidates[:limit]
        events: list[AmendmentEvent] = []
        for law_info, revision, prev_id in batch:
            event = event_from_revision(law_info, revision, prev_id)
            self._attach_texts(event)
            events.append(event)
        deferred = {str(c[0]["law_id"]) for c in candidates[limit:]}
        for law_id, newest in latest.items():
            if law_id not in deferred:
                state.snapshot[law_id] = newest
        return events

    def _law_ids_to_check(self, state: State) -> list[str]:
        ids: list[str] = [str(e["law_id"]) for e in self._watchlist]
        if self._scan_law_types:
            params: dict[str, Any] = {"asof": FAR_FUTURE, "law_type": ",".join(self._scan_law_types)}
            if self._scan_category_codes:
                params["category_cd"] = ",".join(self._scan_category_codes)
            for law in self._client.iter_laws(**params):
                info = law.get("law_info") or {}
                rev = law.get("revision_info") or {}
                law_id = str(info.get("law_id") or "")
                if not law_id:
                    continue
                latest = str(rev.get("law_revision_id") or "")
                if law_id in ids:
                    continue
                if state.snapshot.get(law_id) != latest:
                    ids.append(law_id)
        return list(dict.fromkeys(ids))

    def _new_revisions_for(self, law_id: str, state: State, *, since: date | None, bootstrap: bool) -> tuple[list[tuple[dict[str, Any], dict[str, Any], str | None]], str | None]:
        """`(候補, その法令の最新履歴 ID)` を返す。スナップショットは更新しない。"""
        law_info, data = self._client.law_revisions(law_id)
        if not data:
            return [], None
        known = state.snapshot.get(law_id)
        found: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
        # data は施行日の降順。known（前回の最新）より前の要素が新しい履歴。
        for index, revision in enumerate(data):
            rid = str(revision["law_revision_id"])
            if rid == known:
                break
            if state.has_seen(rid):
                continue
            promulgated = _parse_date(revision.get("amendment_promulgate_date"))
            if since and promulgated and promulgated < since:
                continue
            if bootstrap and since is None:
                continue
            prev_id = str(data[index + 1]["law_revision_id"]) if index + 1 < len(data) else None
            found.append((law_info, revision, prev_id))
        return found, str(data[0]["law_revision_id"])

    def _attach_texts(self, event: AmendmentEvent) -> None:
        """本文を付ける。e-Gov が本文ファイルを提供しない履歴（404）はメタデータのみのイベントにする。"""
        try:
            new_tree = self._client.law_data(event.revision_id)["law_full_text"]
        except SourceError as exc:
            if "HTTP 404" not in str(exc):
                raise
            event.text_available = False
            return
        event.new_text = law_tree_to_text(new_tree)
        if event.prev_revision_id:
            try:
                old_tree = self._client.law_data(event.prev_revision_id)["law_full_text"]
            except SourceError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                old_tree = None
            if old_tree is not None:
                event.old_text = law_tree_to_text(old_tree)
        event.diff = unified_diff(event.old_text, event.new_text)
        event.stats = diff_stats(event.diff)


# ------------------------------------------------------------------ git mirror
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[match.end() :]


class GitSource:
    """ローカルにクローン済みの法令ミラー（1 法令 = 1 Markdown、frontmatter にメタデータ）。"""

    name = "git"

    def __init__(self, repo_dir: Path, *, glob: str = "**/*.md") -> None:
        try:
            from git import Repo
        except ImportError as exc:  # pragma: no cover - GitPython is a declared dependency
            raise SourceError("GitPython is required for the git source") from exc
        if not repo_dir.exists():
            raise SourceError(f"git mirror directory not found: {repo_dir}")
        self._repo = Repo(repo_dir)
        self._glob = glob

    def head(self) -> str:
        return str(self._repo.head.commit.hexsha)

    def detect(self, state: State, *, since: date | None, limit: int) -> list[AmendmentEvent]:
        head = self._repo.head.commit
        base = state.git_last_commit
        if base is None:
            state.git_last_commit = head.hexsha
            return []
        if base == head.hexsha:
            return []
        try:
            diffs = list(self._repo.commit(base).diff(head))
        except Exception as exc:
            raise SourceError(f"git base commit {base} not found or diff failed: {exc}") from exc
        events: list[AmendmentEvent] = []
        for diff in diffs:
            path = diff.b_path or diff.a_path
            if not path or not path.endswith(".md"):
                continue
            old_raw = diff.a_blob.data_stream.read().decode("utf-8") if diff.a_blob else ""
            new_raw = diff.b_blob.data_stream.read().decode("utf-8") if diff.b_blob else ""
            old_meta, old_body = split_frontmatter(old_raw)
            new_meta, new_body = split_frontmatter(new_raw)
            meta = new_meta or old_meta
            kind: str = "一部改正"
            if diff.new_file:
                kind = "新設"
            elif diff.deleted_file:
                kind = "廃止"
            promulgated = _parse_date(meta.get("amendment_promulgate_date") or meta.get("promulgation_date")) or head.committed_datetime.date()
            if since and promulgated < since:
                continue
            law_id = str(meta.get("law_id") or Path(path).stem)
            revision_id = f"{law_id}_{head.hexsha[:12]}"
            if state.has_seen(revision_id):
                continue
            event = AmendmentEvent(
                event_id=revision_id,
                law=LawRef(law_id=law_id, law_num=str(meta.get("law_num") or ""), law_title=str(meta.get("law_title") or meta.get("title") or law_id), law_type=str(meta.get("law_type") or "Act"), egov_category=meta.get("category")),
                revision_id=revision_id,
                prev_revision_id=f"{law_id}_{base[:12]}" if diff.a_blob else None,
                amendment_kind=kind,  # type: ignore[arg-type]
                amending_law_id=meta.get("amendment_law_id"),
                amending_law_title=meta.get("amendment_law_title"),
                amending_law_num=meta.get("amendment_law_num"),
                promulgation_date=promulgated,
                enforcement_date=_parse_date(meta.get("amendment_enforcement_date") or meta.get("enforcement_date")),
                enforcement_status=EnforcementStatus.CURRENT,
                source="git",
                old_text=old_body,
                new_text=new_body,
            )
            event.diff = unified_diff(event.old_text, event.new_text)
            event.stats = diff_stats(event.diff)
            events.append(event)
            if len(events) >= limit:
                break
        state.git_last_commit = head.hexsha
        return events
