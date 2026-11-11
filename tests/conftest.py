"""共通フィクスチャ。外部 HTTP は httpx.MockTransport で代替し、ネットワークに出ない。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline.egov import EgovClient
from pipeline.models import AmendmentEvent, DiffStats, EnforcementStatus, LawRef

FIXTURES = Path(__file__).parent / "fixtures"


def law_tree(*lines: tuple[str, str]) -> dict[str, Any]:
    """`(条見出し, 本文)` の列から最小の法令 JSON 木を作る。"""
    articles = []
    for title, body in lines:
        articles.append(
            {
                "tag": "Article",
                "attr": {},
                "children": [
                    {"tag": "ArticleTitle", "attr": {}, "children": [title]},
                    {"tag": "Paragraph", "attr": {}, "children": [{"tag": "ParagraphNum", "attr": {}, "children": [""]}, {"tag": "ParagraphSentence", "attr": {}, "children": [{"tag": "Sentence", "attr": {}, "children": [body]}]}]},
                ],
            }
        )
    return {
        "tag": "Law",
        "attr": {"LawType": "Act"},
        "children": [
            {"tag": "LawNum", "attr": {}, "children": ["令和八年法律第一号"]},
            {"tag": "LawBody", "attr": {}, "children": [{"tag": "LawTitle", "attr": {}, "children": ["試験法"]}, {"tag": "MainProvision", "attr": {}, "children": articles}]},
        ],
    }


def revision(law_id: str, rid: str, *, promulgated: str, enforced: str, status: str = "UnEnforced", amendment_type: str = "3", amending: str | None = "508AC0000000056", repeal: str = "None") -> dict[str, Any]:
    return {
        "law_revision_id": rid,
        "law_type": "Act",
        "law_title": "試験法",
        "law_title_kana": "しけんほう",
        "abbrev": None,
        "category": "電気通信",
        "updated": "2026-07-24T11:43:13+09:00",
        "amendment_promulgate_date": promulgated,
        "amendment_enforcement_date": enforced,
        "amendment_enforcement_comment": None,
        "amendment_scheduled_enforcement_date": enforced,
        "amendment_law_id": amending,
        "amendment_law_title": "試験法の一部を改正する法律" if amending else None,
        "amendment_law_num": "令和八年法律第五十六号" if amending else None,
        "amendment_type": amendment_type,
        "repeal_status": repeal,
        "repeal_date": None,
        "remain_in_force": False,
        "mission": "New",
        "current_revision_status": status,
    }


LAW_INFO = {"law_type": "Act", "law_id": "500AC0000000001", "law_num": "令和八年法律第一号", "law_num_era": "Reiwa", "law_num_year": 8, "law_num_type": "Act", "law_num_num": "001", "promulgation_date": "2026-01-10"}

REV_NEW = revision("500AC0000000001", "500AC0000000001_20270101_508AC0000000056", promulgated="2026-07-17", enforced="2027-01-01")
REV_MID = revision("500AC0000000001", "500AC0000000001_20260401_507AC0000000010", promulgated="2025-06-01", enforced="2026-04-01", status="CurrentEnforced", amending="507AC0000000010")
REV_OLD = revision("500AC0000000001", "500AC0000000001_20260110_000000000000000", promulgated="2026-01-10", enforced="2026-01-10", status="PreviousEnforced", amendment_type="1", amending=None)

TREE_OLD = law_tree(("第一条", "この法律は、試験のための法律である。"), ("第二条", "事業者は、帳簿を備えなければならない。"))
TREE_MID = law_tree(("第一条", "この法律は、試験のための法律である。"), ("第二条", "事業者は、帳簿を備え、五年間保存しなければならない。"))
TREE_NEW = law_tree(
    ("第一条", "この法律は、試験のための法律である。"),
    ("第二条", "事業者は、帳簿を電磁的記録により備え、五年間保存しなければならない。"),
    ("第三条", "事業者は、漏えいが生じたときは、遅滞なく主務大臣に報告しなければならない。"),
)


class FakeEgov:
    """e-Gov API v2 の最小エミュレータ。`handler` を MockTransport に渡す。"""

    def __init__(self) -> None:
        self.laws: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, list[dict[str, Any]]] = {}
        self.texts: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_status: dict[str, list[int]] = {}
        self.raise_on: set[str] = set()
        self.bad_json: set[str] = set()

    def add_law(self, law_info: dict[str, Any], revs: list[dict[str, Any]], texts: dict[str, dict[str, Any]]) -> None:
        self.laws[law_info["law_id"]] = law_info
        self.revisions[law_info["law_id"]] = revs
        self.texts.update(texts)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path + ("?" + request.url.query.decode() if request.url.query else ""))
        if path in self.raise_on:
            raise httpx.ConnectError("boom", request=request)
        queued = self.fail_status.get(path)
        if queued:
            return httpx.Response(queued.pop(0), text="error")
        if path in self.bad_json:
            return httpx.Response(200, text="not json")
        if path.endswith("/laws"):
            return self._laws(request)
        if "/law_revisions/" in path:
            law_id = path.rsplit("/", 1)[1]
            if law_id not in self.revisions:
                return httpx.Response(404, json={"message": "not found"})
            params = dict(request.url.params)
            revs = self.revisions[law_id]
            if "amendment_promulgate_date_from" in params:
                revs = [r for r in revs if r["amendment_promulgate_date"] >= params["amendment_promulgate_date_from"]]
            return httpx.Response(200, json={"law_info": self.laws[law_id], "revisions": revs})
        if "/law_data/" in path:
            rid = path.rsplit("/", 1)[1]
            if rid not in self.texts:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"law_info": {}, "revision_info": {}, "law_full_text": self.texts[rid]})
        return httpx.Response(404, json={"message": "unknown"})

    def _laws(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 100))
        items = []
        for law_id, info in self.laws.items():
            revs = self.revisions[law_id]
            items.append({"law_info": info, "revision_info": revs[0]})
        page = items[offset : offset + limit]
        return httpx.Response(200, json={"total_count": len(items), "count": len(page), "next_offset": offset + len(page), "laws": page})


@pytest.fixture
def fake_egov() -> FakeEgov:
    fake = FakeEgov()
    fake.add_law(
        LAW_INFO,
        [REV_NEW, REV_MID, REV_OLD],
        {REV_NEW["law_revision_id"]: TREE_NEW, REV_MID["law_revision_id"]: TREE_MID, REV_OLD["law_revision_id"]: TREE_OLD},
    )
    return fake


@pytest.fixture
def make_client(fake_egov: FakeEgov) -> Callable[..., EgovClient]:
    def _make(**kwargs: Any) -> EgovClient:
        kwargs.setdefault("interval_sec", 0)
        kwargs.setdefault("sleep", lambda _s: None)
        return EgovClient("https://egov.test/api/2", transport=httpx.MockTransport(fake_egov.handler), **kwargs)

    return _make


@pytest.fixture
def client(make_client: Callable[..., EgovClient]) -> EgovClient:
    return make_client()


def make_event(**overrides: Any) -> AmendmentEvent:
    base: dict[str, Any] = {
        "event_id": "500AC0000000001_20270101_508AC0000000056",
        "law": LawRef(law_id="500AC0000000001", law_num="令和八年法律第一号", law_title="試験法", egov_category="電気通信"),
        "revision_id": "500AC0000000001_20270101_508AC0000000056",
        "prev_revision_id": "500AC0000000001_20260401_507AC0000000010",
        "amendment_kind": "一部改正",
        "amending_law_id": "508AC0000000056",
        "amending_law_title": "試験法の一部を改正する法律",
        "amending_law_num": "令和八年法律第五十六号",
        "promulgation_date": date(2026, 7, 17),
        "enforcement_date": date(2027, 1, 1),
        "enforcement_status": EnforcementStatus.UNENFORCED,
        "old_text": "第一条 この法律は、試験のための法律である。\n第二条 事業者は、帳簿を備え、五年間保存しなければならない。\n",
        "new_text": "第一条 この法律は、試験のための法律である。\n第二条 事業者は、帳簿を電磁的記録により備え、五年間保存しなければならない。\n第三条 事業者は、漏えいが生じたときは、遅滞なく主務大臣に報告しなければならない。\n",
    }
    base.update(overrides)
    event = AmendmentEvent(**base)
    if not event.diff:
        from pipeline.differ import diff_stats, unified_diff

        event.diff = unified_diff(event.old_text, event.new_text)
        event.stats = diff_stats(event.diff) if event.diff else DiffStats()
    return event


@pytest.fixture
def event() -> AmendmentEvent:
    return make_event()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
