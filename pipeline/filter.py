"""ノイズ除去と対象法令フィルタ。

落とすもの:
1. 本文に差分がない履歴（正誤・メタデータのみの更新）
2. 変更文字数・変更行数が閾値未満の微修正
3. 条番号・項番号の機械的な繰り下げ／繰り上げ、または空白・句読点の違いだけの変更
4. 対象外の法令種別（設定で絞る場合）

新設（amendment_kind=新設）と廃止は本文差分の大小に関係なく通す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.models import AmendmentEvent

_KANJI_NUM = "[一二三四五六七八九十百千〇]+"
_NUMBERING_RE = re.compile(rf"第{_KANJI_NUM}(?:条|項|号|章|節|款|目|編)(?:の{_KANJI_NUM})*")
_ARABIC_RE = re.compile(r"[0-9０-９]+")
_PUNCT_WS_RE = re.compile(r"[\s、。，．・「」『』（）()\[\]【】]")


@dataclass(frozen=True)
class FilterDecision:
    keep: bool
    reason: str


def normalize_for_renumber_check(text: str) -> str:
    """条番号・数字・空白・句読点を潰し、本質的な語句だけを残す。"""
    text = _NUMBERING_RE.sub("第N", text)
    text = _ARABIC_RE.sub("N", text)
    return _PUNCT_WS_RE.sub("", text)


def is_renumbering_only(old_text: str, new_text: str) -> bool:
    """条番号の繰り下げ・空白・句読点のみの違いなら True。"""
    if old_text == new_text:
        return False
    return normalize_for_renumber_check(old_text) == normalize_for_renumber_check(new_text)


def decide(event: AmendmentEvent, *, min_changed_chars: int, min_changed_lines: int, allowed_law_types: list[str] | None = None) -> FilterDecision:
    """1 件の改正イベントを通すか落とすか。理由を文字列で返す。"""
    if allowed_law_types and event.law.law_type not in allowed_law_types:
        return FilterDecision(False, f"law_type {event.law.law_type} not in {allowed_law_types}")
    if event.amendment_kind in ("新設", "廃止"):
        return FilterDecision(True, f"{event.amendment_kind} is always kept")
    if not event.text_available:
        return FilterDecision(True, "text unavailable on e-Gov; metadata only")
    if not event.diff.strip():
        return FilterDecision(False, "no textual change")
    changed_lines = event.stats.added_lines + event.stats.removed_lines
    if changed_lines < min_changed_lines:
        return FilterDecision(False, f"changed lines {changed_lines} < {min_changed_lines}")
    if event.stats.changed_chars < min_changed_chars:
        return FilterDecision(False, f"changed chars {event.stats.changed_chars} < {min_changed_chars}")
    if is_renumbering_only(event.old_text, event.new_text):
        return FilterDecision(False, "renumbering / punctuation only")
    return FilterDecision(True, "substantive change")


def apply(events: list[AmendmentEvent], *, min_changed_chars: int, min_changed_lines: int, allowed_law_types: list[str] | None = None) -> tuple[list[AmendmentEvent], list[tuple[AmendmentEvent, str]]]:
    """通過したイベントと、落としたイベント＋理由を返す。"""
    kept: list[AmendmentEvent] = []
    dropped: list[tuple[AmendmentEvent, str]] = []
    for event in events:
        decision = decide(event, min_changed_chars=min_changed_chars, min_changed_lines=min_changed_lines, allowed_law_types=allowed_law_types)
        if decision.keep:
            kept.append(event)
        else:
            dropped.append((event, decision.reason))
    return kept, dropped
