"""e-Gov 法令本文（JSON 木 / XML）を、条・項・号ごとに 1 行の平文へ変換する。

平文化の目的は差分の単位を「条文の行」に揃えることである。
条（Article）・項（第 2 項以降の ParagraphNum）・号（Item）・目（Subitem）・見出し（章・節）を行の区切りにし、
それ以外の要素（Sentence, Ruby など）は行の中に連結する。
"""

from __future__ import annotations

from typing import Any

from lxml import etree

JsonNode = dict[str, Any] | str

# これらの要素は新しい行を開始する
LINE_START_TAGS = frozenset(
    {
        "LawTitle",
        "EnactStatement",
        "TOCLabel",
        "TOCPreambleLabel",
        "PartTitle",
        "ChapterTitle",
        "SectionTitle",
        "SubsectionTitle",
        "DivisionTitle",
        "ArticleCaption",
        "Article",
        "Item",
        "Subitem1",
        "Subitem2",
        "Subitem3",
        "Subitem4",
        "SupplProvisionLabel",
        "AppdxTableTitle",
        "AppdxNoteTitle",
        "TableRow",
        "Remarks",
    }
)
# 目次や付録の見出しなど、差分に意味を持たない要素は丸ごと落とす
SKIP_TAGS = frozenset({"TOC", "Ruby", "Rt", "LawNum"})
# 番号ラベルの後ろに空白を入れて読みやすくする
LABEL_TAGS = frozenset({"ArticleTitle", "ParagraphNum", "ItemTitle", "Subitem1Title", "Subitem2Title", "Subitem3Title", "Subitem4Title"})


def law_tree_to_text(node: JsonNode) -> str:
    """`law_full_text`（JSON 木）を平文へ。"""
    lines: list[str] = []
    _walk_json(node, lines)
    return _finalize(lines)


def law_xml_to_text(xml_bytes: bytes) -> str:
    """法令標準 XML を平文へ（Git ミラーなど XML 入力用）。"""
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"invalid law XML: {exc}") from exc
    lines: list[str] = []
    _walk_xml(root, lines)
    return _finalize(lines)


def _finalize(lines: list[str]) -> str:
    cleaned = [" ".join(line.split()) for line in lines]
    return "\n".join(line for line in cleaned if line) + "\n"


def _append(lines: list[str], text: str, *, new_line: bool) -> None:
    if new_line or not lines:
        lines.append(text)
    else:
        lines[-1] += text


def _has_text(children: list[JsonNode]) -> bool:
    return any(isinstance(c, str) and c.strip() for c in children)


def _walk_json(node: JsonNode, lines: list[str]) -> None:
    if isinstance(node, str):
        _append(lines, node, new_line=False)
        return
    tag = str(node.get("tag", ""))
    if tag in SKIP_TAGS:
        return
    children = node.get("children") or []
    # 第 2 項以降（番号付きの項）は独立した行にする。第 1 項は条見出しと同じ行に置く。
    if tag in LINE_START_TAGS or (tag == "ParagraphNum" and _has_text(children)):
        _append(lines, "", new_line=True)
    for child in children:
        _walk_json(child, lines)
    if tag in LABEL_TAGS:
        _append(lines, " ", new_line=False)


def _walk_xml(element: etree._Element, lines: list[str]) -> None:
    tag = etree.QName(element).localname
    if tag in SKIP_TAGS:
        return
    if tag in LINE_START_TAGS or (tag == "ParagraphNum" and (element.text or "").strip()):
        _append(lines, "", new_line=True)
    if element.text:
        _append(lines, element.text, new_line=False)
    for child in element:
        _walk_xml(child, lines)
        if child.tail:
            _append(lines, child.tail, new_line=False)
    if tag in LABEL_TAGS:
        _append(lines, " ", new_line=False)
