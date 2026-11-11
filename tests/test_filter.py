from __future__ import annotations

import pytest

from pipeline import filter as noise_filter
from pipeline.filter import is_renumbering_only, normalize_for_renumber_check
from tests.conftest import make_event


def test_normalize_collapses_numbering_and_punctuation() -> None:
    assert normalize_for_renumber_check("第三条の二 甲、乙。") == normalize_for_renumber_check("第四条の三　甲乙")
    assert normalize_for_renumber_check("５年") == normalize_for_renumber_check("10年")


def test_renumbering_only() -> None:
    assert is_renumbering_only("第二条 事業者は帳簿を備える。", "第三条 事業者は帳簿を備える。")
    assert not is_renumbering_only("同一", "同一")
    assert not is_renumbering_only("第二条 帳簿を備える。", "第二条 帳簿を電磁的に備える。")


def test_decide_keeps_substantive_change() -> None:
    d = noise_filter.decide(make_event(), min_changed_chars=40, min_changed_lines=2)
    assert d.keep and d.reason == "substantive change"


@pytest.mark.parametrize("kind", ["新設", "廃止"])
def test_decide_always_keeps_new_and_repeal(kind: str) -> None:
    ev = make_event(amendment_kind=kind, old_text="", new_text="", diff="")
    assert noise_filter.decide(ev, min_changed_chars=40, min_changed_lines=2).keep


def test_decide_drops_no_change() -> None:
    ev = make_event(old_text="x\n", new_text="x\n", diff="")
    d = noise_filter.decide(ev, min_changed_chars=40, min_changed_lines=2)
    assert not d.keep and d.reason == "no textual change"


def test_decide_drops_by_line_threshold_boundary() -> None:
    ev = make_event()  # +2 / -1 = 3 changed lines
    assert noise_filter.decide(ev, min_changed_chars=0, min_changed_lines=3).keep
    assert noise_filter.decide(ev, min_changed_chars=0, min_changed_lines=4).reason.startswith("changed lines 3 < 4")


def test_decide_drops_by_char_threshold_boundary() -> None:
    ev = make_event()
    chars = ev.stats.changed_chars
    assert noise_filter.decide(ev, min_changed_chars=chars, min_changed_lines=0).keep
    assert noise_filter.decide(ev, min_changed_chars=chars + 1, min_changed_lines=0).reason.startswith("changed chars")


def test_decide_drops_renumbering_only() -> None:
    old = "第一条 甲は乙をしなければならない。\n第二条 丙は丁をしなければならない。\n"
    new = "第二条 甲は乙をしなければならない。\n第三条 丙は丁をしなければならない。\n"
    ev = make_event(old_text=old, new_text=new)
    d = noise_filter.decide(ev, min_changed_chars=1, min_changed_lines=1)
    assert not d.keep and d.reason == "renumbering / punctuation only"


def test_decide_drops_disallowed_law_type() -> None:
    ev = make_event()
    d = noise_filter.decide(ev, min_changed_chars=0, min_changed_lines=0, allowed_law_types=["CabinetOrder"])
    assert not d.keep and "law_type Act" in d.reason


def test_apply_splits_kept_and_dropped() -> None:
    kept, dropped = noise_filter.apply([make_event(), make_event(old_text="x\n", new_text="x\n", diff="")], min_changed_chars=40, min_changed_lines=2)
    assert len(kept) == 1 and len(dropped) == 1
    assert dropped[0][1] == "no textual change"
