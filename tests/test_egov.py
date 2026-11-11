from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from pipeline.egov import EgovClient
from pipeline.errors import SourceError
from tests.conftest import LAW_INFO, REV_NEW, FakeEgov


def test_iter_laws_paginates(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    for i in range(3):
        info = dict(LAW_INFO, law_id=f"50{i}AC0000000009")
        fake_egov.add_law(info, [dict(REV_NEW, law_revision_id=f"{info['law_id']}_x")], {})
    with make_client() as client:
        laws = list(client.iter_laws(page_size=2, asof="2099-12-31"))
    assert len(laws) == 4
    assert [c for c in fake_egov.calls if "/laws?" in c] and all("asof=2099-12-31" in c for c in fake_egov.calls)
    assert sum(1 for c in fake_egov.calls if "offset=" in c) == 2


def test_iter_laws_empty(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.laws.clear()
    fake_egov.revisions.clear()
    assert list(make_client().iter_laws()) == []


def test_law_revisions_returns_info_and_list(client: EgovClient) -> None:
    info, revs = client.law_revisions("500AC0000000001")
    assert info["law_id"] == "500AC0000000001"
    assert next(r["law_revision_id"] for r in revs) == REV_NEW["law_revision_id"]


def test_law_revisions_without_law_info(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    original = fake_egov.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if "/law_revisions/" in request.url.path:
            payload = response.json()
            payload.pop("law_info")
            return httpx.Response(200, json=payload)
        return response

    client = EgovClient("https://egov.test/api/2", transport=httpx.MockTransport(handler), interval_sec=0)
    info, _ = client.law_revisions("500AC0000000001")
    assert info == {"law_id": "500AC0000000001"}


def test_law_revisions_bad_shape(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.revisions["500AC0000000001"] = None  # type: ignore[assignment]
    with pytest.raises(SourceError, match="no revisions list"):
        make_client().law_revisions("500AC0000000001")


def test_law_data_ok_and_missing(client: EgovClient) -> None:
    data = client.law_data(REV_NEW["law_revision_id"])
    assert data["law_full_text"]["tag"] == "Law"
    with pytest.raises(SourceError, match="HTTP 404"):
        client.law_data("nope")


def test_law_data_without_text(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.texts[REV_NEW["law_revision_id"]] = None  # type: ignore[assignment]
    with pytest.raises(SourceError, match="no law_full_text"):
        make_client().law_data(REV_NEW["law_revision_id"])


def test_retries_then_succeeds(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.fail_status["/api/2/law_revisions/500AC0000000001"] = [503, 429]
    sleeps: list[float] = []
    client = make_client(sleep=sleeps.append, max_retries=3)
    _, revs = client.law_revisions("500AC0000000001")
    assert len(revs) == 3
    assert sleeps == [2.0, 4.0]


def test_retries_exhausted_raises(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.fail_status["/api/2/law_revisions/500AC0000000001"] = [500, 500, 500]
    client = make_client(max_retries=2)
    with pytest.raises(SourceError, match="HTTP 500"):
        client.law_revisions("500AC0000000001")


def test_connection_error_retries_then_raises(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.raise_on.add("/api/2/law_revisions/500AC0000000001")
    client = make_client(max_retries=1)
    with pytest.raises(SourceError, match="failed after 1 retries"):
        client.law_revisions("500AC0000000001")
    assert client.request_count == 0


def test_connection_error_then_success(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    path = "/api/2/law_revisions/500AC0000000001"
    fake_egov.raise_on.add(path)
    original = fake_egov.handler
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 2:
            fake_egov.raise_on.discard(path)
        return original(request)

    client = EgovClient("https://egov.test/api/2", transport=httpx.MockTransport(handler), interval_sec=0, sleep=lambda _s: None, max_retries=3)
    _, revs = client.law_revisions("500AC0000000001")
    assert len(revs) == 3


def test_non_json_and_non_object(fake_egov: FakeEgov, make_client: Callable[..., EgovClient]) -> None:
    fake_egov.bad_json.add("/api/2/law_revisions/500AC0000000001")
    with pytest.raises(SourceError, match="non-JSON"):
        make_client().law_revisions("500AC0000000001")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    client = EgovClient("https://egov.test/api/2", transport=httpx.MockTransport(handler), interval_sec=0)
    with pytest.raises(SourceError, match="unexpected JSON shape"):
        client.law_revisions("x")


def test_throttle_sleeps_between_calls(fake_egov: FakeEgov) -> None:
    sleeps: list[float] = []
    client = EgovClient("https://egov.test/api/2", transport=httpx.MockTransport(fake_egov.handler), interval_sec=0.5, sleep=sleeps.append)
    client.law_revisions("500AC0000000001")
    client.law_revisions("500AC0000000001")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 0.5
    client.close()
