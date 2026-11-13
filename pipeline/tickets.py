"""改正 1 件 = GitHub Issue 1 件（チケット）。

サイトの記事（frontmatter 付き Markdown）と同じ内容から Issue の題名・本文・ラベルを組み立て、
GitHub REST API（httpx）で起票・更新する。冪等性は `State.tickets`（law_revision_id → Issue 番号）で担保し、
同じ履歴を二度起票しない。本文の先頭にはマーカー `<!-- lawtrack:revision_id=... -->` を埋め、状態ファイルを失っても
Issue 側から対応付けを復元できるようにする。

GitHub の二次レート制限（コンテンツ生成は毎分 80 件・毎時 500 件）に収まるよう、書き込みの間に `interval_sec` 空ける。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pipeline.errors import TicketError
from pipeline.fetcher import split_frontmatter
from pipeline.state import State

log = logging.getLogger(__name__)

MARKER_RE = re.compile(r"<!-- lawtrack:revision_id=(\S+) -->")
MAX_BODY_DIFF_CHARS = 30000
MAX_TITLE_CHARS = 200
MAX_RETRIES = 3

STATUS_JA = {"CurrentEnforced": "施行済", "UnEnforced": "未施行", "PreviousEnforced": "旧版", "Repeal": "廃止"}

# ラベル名の接頭辞ごとの色（GitHub は 6 桁 16 進、# なし）
LABEL_COLORS = {
    "分野": "0e8a9b",
    "影響度:High": "b60205",
    "影響度:Medium": "d93f0b",
    "影響度:Low": "0e8a16",
    "施行": "5319e7",
    "種別": "6b7280",
}

DISCLAIMER = "要旨・影響度・分野は機械生成（LLM）であり、法的助言ではない。条文は e-Gov 法令検索の提供データに基づき、官報と異なる場合は官報が優先する。"


@dataclass(frozen=True)
class Ticket:
    revision_id: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)


def label_color(name: str) -> str:
    if name in LABEL_COLORS:
        return LABEL_COLORS[name]
    prefix = name.split(":", 1)[0]
    return LABEL_COLORS.get(prefix, "ededed")


def labels_for(fm: dict[str, Any]) -> list[str]:
    labels = [f"分野:{c}" for c in fm.get("categories", [])]
    labels.append(f"影響度:{fm.get('impact_level', 'Low')}")
    labels.append(f"施行:{STATUS_JA.get(str(fm.get('enforcement_status')), '不明')}")
    labels.append(f"種別:{fm.get('amendment_type', '一部改正')}")
    return labels


def issue_title(fm: dict[str, Any]) -> str:
    impact = fm.get("impact_level", "Low")
    what = fm.get("amending_law_title") or fm.get("amendment_type") or "改正"
    when = fm.get("enforcement_date") or "未定"
    title = f"[{impact}] {fm.get('title', '')}：{what}（施行 {when}）"
    if len(title) > MAX_TITLE_CHARS:
        title = title[: MAX_TITLE_CHARS - 1] + "…"
    return title


def _extract_diff(markdown_body: str) -> str:
    match = re.search(r"```diff\n(.*?)\n```", markdown_body, re.S)
    return match.group(1) if match else ""


def issue_body(fm: dict[str, Any], markdown_body: str, article_url: str | None) -> str:
    rid = str(fm.get("revision_id", ""))
    lines = [f"<!-- lawtrack:revision_id={rid} -->", ""]
    lines.append("## 3 行要旨")
    lines.extend(f"- {p}" for p in fm.get("summary_points", []))
    lines.append("")
    lines.append("## 対象")
    lines.append("、".join(fm.get("target_audience", [])) or "（未分類）")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|---|---|")
    lines.append(f"| 法令 | {fm.get('title', '')}（{fm.get('law_num', '')}） |")
    lines.append(f"| 改正法 | {fm.get('amending_law_title') or '—'}（{fm.get('amending_law_num') or '—'}） |")
    lines.append(f"| 公布日 | {fm.get('promulgation_date', '—')} |")
    lines.append(f"| 施行日 | {fm.get('enforcement_date') or '未定'}（{fm.get('enforcement_note', '')}） |")
    lines.append(f"| 施行状態 | {STATUS_JA.get(str(fm.get('enforcement_status')), '不明')} |")
    lines.append(f"| 履歴 ID | `{rid}` |")
    lines.append("")
    lines.append("## リンク")
    if article_url:
        lines.append(f"- 記事: {article_url}")
    lines.append(f"- e-Gov 法令検索: {fm.get('egov_url', '')}")
    lines.append("")
    diff = _extract_diff(markdown_body)
    if diff:
        note = ""
        if len(diff) > MAX_BODY_DIFF_CHARS:
            diff = diff[:MAX_BODY_DIFF_CHARS]
            note = "\n（差分が長いため切り詰めた。全文は記事を参照）"
        lines.append("<details><summary>条文差分（機械抽出）</summary>")
        lines.append("")
        lines.append("```diff")
        lines.append(diff)
        lines.append("```" + note)
        lines.append("")
        lines.append("</details>")
        lines.append("")
    elif fm.get("text_available") is False:
        lines.append("e-Gov がこの履歴の本文ファイルを提供していないため、条文差分はない。")
        lines.append("")
    lines.append("---")
    lines.append(DISCLAIMER)
    return "\n".join(lines) + "\n"


def build(fm: dict[str, Any], markdown_body: str, article_url: str | None = None) -> Ticket:
    rid = str(fm.get("revision_id") or "")
    if not rid:
        raise TicketError("frontmatter has no revision_id")
    return Ticket(revision_id=rid, title=issue_title(fm), body=issue_body(fm, markdown_body, article_url), labels=labels_for(fm))


def build_from_markdown(text: str, article_url: str | None = None) -> Ticket:
    fm, body = split_frontmatter(text)
    return build(fm, body, article_url)


class GitHubIssues:
    """GitHub REST API の最小クライアント（ラベル作成・Issue 作成/更新）。"""

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        api_base: str = "https://api.github.com",
        timeout: float = 30.0,
        interval_sec: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if "/" not in repo:
            raise TicketError(f"issues repo must be owner/name, got {repo!r}")
        self.repo = repo
        self._interval = interval_sec
        self._sleep = sleep
        self._known_labels: set[str] | None = None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        self._client = httpx.Client(base_url=api_base.rstrip("/"), headers=headers, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        last = "no attempt"
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.request(method, path, json=payload)
            except httpx.HTTPError as exc:
                last = f"connection error: {exc}"
                self._sleep(min(2**attempt, 30))
                continue
            if response.status_code in (403, 429) and ("rate" in response.text.lower() or response.headers.get("Retry-After")):
                wait = float(response.headers.get("Retry-After", 2**attempt * 5))
                last = f"rate limited ({response.status_code})"
                log.warning("github %s %s: %s, waiting %.0fs", method, path, last, wait)
                self._sleep(min(wait, 120))
                continue
            if response.status_code >= 500:
                last = f"server error {response.status_code}"
                self._sleep(min(2**attempt, 30))
                continue
            if response.status_code >= 400:
                raise TicketError(f"github {method} {path} failed: {response.status_code} {response.text[:200]}")
            return response.json()
        raise TicketError(f"github {method} {path} failed after {MAX_RETRIES} attempts: {last}")

    def _ensure_labels(self, names: list[str]) -> int:
        if self._known_labels is None:
            known: set[str] = set()
            page = 1
            while True:
                items = self._request("GET", f"/repos/{self.repo}/labels?per_page=100&page={page}")
                known.update(item["name"] for item in items)
                if len(items) < 100:
                    break
                page += 1
            self._known_labels = known
        created = 0
        for name in names:
            if name in self._known_labels:
                continue
            self._request("POST", f"/repos/{self.repo}/labels", {"name": name, "color": label_color(name)})
            self._known_labels.add(name)
            created += 1
        return created

    def upsert(self, ticket: Ticket, existing_number: int | None) -> tuple[int, str]:
        """Issue を作成（existing_number が None）または更新する。(番号, URL) を返す。"""
        self._ensure_labels(ticket.labels)
        payload = {"title": ticket.title, "body": ticket.body, "labels": ticket.labels}
        if existing_number is None:
            data = self._request("POST", f"/repos/{self.repo}/issues", payload)
        else:
            data = self._request("PATCH", f"/repos/{self.repo}/issues/{existing_number}", payload)
        self._sleep(self._interval)
        return int(data["number"]), str(data["html_url"])


def sync(client: GitHubIssues, state: State, ticket: Ticket) -> str:
    """状態ファイルを見て作成か更新かを決め、Issue の URL を返す。"""
    number, url = client.upsert(ticket, state.tickets.get(ticket.revision_id))
    state.tickets[ticket.revision_id] = number
    return url


def set_issue_url(path: Path, url: str) -> None:
    """既存の記事 Markdown の frontmatter に issue_url を書き込む（あれば置き換える）。"""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if re.search(r"^issue_url: .*$", text, re.M):
        text = re.sub(r"^issue_url: .*$", f"issue_url: {url}", text, count=1, flags=re.M)
    else:
        head, sep, rest = text[3:].partition("\n---\n")
        if not sep:
            raise TicketError(f"{path} has no frontmatter")
        text = "---" + head + f"\nissue_url: {url}" + sep + rest
    path.write_text(text, encoding="utf-8", newline="\n")
