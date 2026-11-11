"""新旧本文の差分を取り、LLM に渡す「新旧対照」形式へ整形する。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from pipeline.models import DiffStats


def unified_diff(old_text: str, new_text: str, *, context: int = 2) -> str:
    """行単位の unified diff。同一なら空文字。"""
    lines = difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile="旧",
        tofile="新",
        lineterm="",
        n=context,
    )
    return "\n".join(lines)


def diff_stats(diff: str) -> DiffStats:
    """unified diff から追加行・削除行・変更文字数（追加+削除の実文字数）を数える。"""
    added = removed = chars = 0
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added += 1
            chars += len(line[1:].strip())
        elif line.startswith("-"):
            removed += 1
            chars += len(line[1:].strip())
    return DiffStats(added_lines=added, removed_lines=removed, changed_chars=chars)


@dataclass(frozen=True)
class Hunk:
    """変更箇所 1 つと前後の条文コンテキスト。"""

    old_lines: list[str]
    new_lines: list[str]
    before: list[str]
    after: list[str]


def hunks(old_text: str, new_text: str, *, context: int = 1) -> list[Hunk]:
    """SequenceMatcher の opcode から、変更ブロックごとに前後 `context` 行を付けて返す。"""
    old = old_text.splitlines()
    new = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    result: list[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = new[max(0, j1 - context) : j1]
        after = new[j2 : j2 + context]
        result.append(Hunk(old_lines=old[i1:i2], new_lines=new[j1:j2], before=before, after=after))
    return result


def format_for_llm(old_text: str, new_text: str, *, max_chars: int, context: int = 1) -> str:
    """改め文を LLM が新旧対比として読める形に整形する。

    出力例::

        ## 変更 1
        [前後の条文] 第二条 ...
        - 旧: ...
        + 新: ...

    `max_chars` を超える場合は末尾の変更を落とし、その旨を注記する。
    """
    blocks: list[str] = []
    total = 0
    all_hunks = hunks(old_text, new_text, context=context)
    for index, hunk in enumerate(all_hunks, start=1):
        parts = [f"## 変更 {index}"]
        if hunk.before:
            parts.append("[直前] " + " / ".join(hunk.before))
        parts.extend(f"- 旧: {line}" for line in hunk.old_lines)
        parts.extend(f"+ 新: {line}" for line in hunk.new_lines)
        if hunk.after:
            parts.append("[直後] " + " / ".join(hunk.after))
        block = "\n".join(parts)
        if total + len(block) + 2 > max_chars:
            omitted = len(all_hunks) - index + 1
            blocks.append(f"（文字数上限のため残り {omitted} 件の変更を省略）")
            break
        blocks.append(block)
        total += len(block) + 2
    return "\n\n".join(blocks)
