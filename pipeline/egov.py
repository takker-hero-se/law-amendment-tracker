"""e-Gov 法令 API v2 の薄いクライアント。認証不要・公開 API。

使うエンドポイントは 3 つに限る。
- `GET /laws`            法令一覧（`asof=2099-12-31` を付けると未施行を含む最新履歴が revision_info に入る）
- `GET /law_revisions/{law_id}`  1 法令の改正履歴（施行日の降順で返る）
- `GET /law_data/{revision_id}`  指定履歴の本文（JSON 木）
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from pipeline.errors import SourceError

FAR_FUTURE = "2099-12-31"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
JsonDict = dict[str, Any]


class EgovClient:
    """httpx ベースの同期クライアント。テストでは `transport` を差し替える。"""

    def __init__(
        self,
        base_url: str = "https://laws.e-gov.go.jp/api/2",
        *,
        timeout: float = 60.0,
        interval_sec: float = 0.3,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "law-amendment-tracker/0.1 (+open-source)"},
        )
        self._interval = interval_sec
        self._max_retries = max_retries
        self._sleep = sleep
        self._last_call = 0.0
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> EgovClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- low level
    def _get(self, path: str, params: dict[str, Any] | None = None) -> JsonDict:
        query = {"response_format": "json", **(params or {})}
        attempt = 0
        while True:
            self._throttle()
            attempt += 1
            try:
                response = self._client.get(path, params=query)
            except httpx.HTTPError as exc:
                if attempt > self._max_retries:
                    raise SourceError(f"e-Gov request failed after {attempt - 1} retries: {path}: {exc}") from exc
                self._sleep(min(2.0 ** attempt, 30.0))
                continue
            self.request_count += 1
            if response.status_code in RETRY_STATUSES and attempt <= self._max_retries:
                self._sleep(min(2.0 ** attempt, 30.0))
                continue
            if response.status_code != 200:
                raise SourceError(f"e-Gov returned HTTP {response.status_code} for {path}: {response.text[:200]}")
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceError(f"e-Gov returned non-JSON for {path}") from exc
            if not isinstance(data, dict):
                raise SourceError(f"e-Gov returned unexpected JSON shape for {path}")
            return data

    def _throttle(self) -> None:
        if self._interval <= 0:
            return
        now = time.monotonic()
        wait = self._interval - (now - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    # ---------------------------------------------------------------- endpoints
    def iter_laws(self, *, page_size: int = 100, **params: Any) -> Iterator[JsonDict]:
        """`/laws` をページングして 1 法令ずつ返す。"""
        offset = 0
        while True:
            data = self._get("/laws", {"limit": page_size, "offset": offset, "omit_current_revision_info": "true", **params})
            laws = data.get("laws") or []
            yield from laws
            total = int(data.get("total_count") or 0)
            offset += len(laws)
            if not laws or offset >= total:
                return

    def law_revisions(self, law_id: str, **params: Any) -> tuple[JsonDict, list[JsonDict]]:
        """`(law_info, revisions)` を返す。revisions は施行日の降順。"""
        data = self._get(f"/law_revisions/{law_id}", params)
        revisions = data.get("revisions")
        if not isinstance(revisions, list):
            raise SourceError(f"e-Gov law_revisions has no revisions list for {law_id}")
        law_info = data.get("law_info")
        if not isinstance(law_info, dict):
            law_info = {"law_id": law_id}
        return law_info, revisions

    def law_data(self, revision_id: str) -> JsonDict:
        data = self._get(f"/law_data/{revision_id}", {"law_full_text_format": "json"})
        text = data.get("law_full_text")
        if not isinstance(text, dict):
            raise SourceError(f"e-Gov law_data has no law_full_text for {revision_id}")
        return data
