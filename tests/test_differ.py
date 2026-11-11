from __future__ import annotations

from pipeline.differ import diff_stats, format_for_llm, hunks, unified_diff

OLD = "第一条 甲\n第二条 乙\n第三条 丙\n"
NEW = "第一条 甲\n第二条 乙改\n第三条 丙\n第四条 丁\n"


def test_unified_diff_empty_when_equal() -> None:
    assert unified_diff(OLD, OLD) == ""


def test_unified_diff_and_stats() -> None:
    diff = unified_diff(OLD, NEW)
    assert "--- 旧" in diff and "+++ 新" in diff
    stats = diff_stats(diff)
    assert (stats.added_lines, stats.removed_lines) == (2, 1)
    assert stats.changed_chars == len("第二条 乙改") + len("第四条 丁") + len("第二条 乙")


def test_diff_stats_empty() -> None:
    s = diff_stats("")
    assert (s.added_lines, s.removed_lines, s.changed_chars) == (0, 0, 0)


def test_hunks_have_context() -> None:
    result = hunks(OLD, NEW, context=1)
    assert len(result) == 2
    first, second = result
    assert first.old_lines == ["第二条 乙"] and first.new_lines == ["第二条 乙改"]
    assert first.before == ["第一条 甲"] and first.after == ["第三条 丙"]
    assert second.old_lines == [] and second.new_lines == ["第四条 丁"]
    assert second.before == ["第三条 丙"] and second.after == []


def test_format_for_llm_full() -> None:
    text = format_for_llm(OLD, NEW, max_chars=10000)
    assert text.startswith("## 変更 1")
    assert "- 旧: 第二条 乙" in text
    assert "+ 新: 第二条 乙改" in text
    assert "[直前] 第一条 甲" in text
    assert "## 変更 2" in text
    assert "省略" not in text


def test_format_for_llm_truncates_at_budget() -> None:
    first_block_len = len(format_for_llm(OLD, NEW, max_chars=10000).split("\n\n")[0])
    text = format_for_llm(OLD, NEW, max_chars=first_block_len + 2)
    assert "## 変更 1" in text
    assert "残り 1 件の変更を省略" in text
    tiny = format_for_llm(OLD, NEW, max_chars=1)
    assert tiny.startswith("（文字数上限のため残り 2 件")


def test_format_for_llm_no_change() -> None:
    assert format_for_llm(OLD, OLD, max_chars=100) == ""
