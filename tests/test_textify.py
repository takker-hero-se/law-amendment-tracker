from __future__ import annotations

import pytest

from pipeline.textify import law_tree_to_text, law_xml_to_text
from tests.conftest import TREE_NEW, law_tree


def test_tree_to_text_one_line_per_article_and_paragraph() -> None:
    text = law_tree_to_text(TREE_NEW)
    lines = text.splitlines()
    assert lines[0] == "試験法"
    assert lines[1] == "第一条 この法律は、試験のための法律である。"
    assert "第三条 事業者は、漏えいが生じたときは、遅滞なく主務大臣に報告しなければならない。" in lines
    assert text.endswith("\n")
    assert "令和八年法律第一号" not in text  # LawNum は落とす


def test_tree_skips_toc_and_ruby_and_collapses_whitespace() -> None:
    tree = law_tree(("第一条", "  本文　　二重  空白 "))
    tree["children"][1]["children"].insert(1, {"tag": "TOC", "attr": {}, "children": [{"tag": "TOCLabel", "attr": {}, "children": ["目次"]}]})
    tree["children"][1]["children"][2]["children"][0]["children"].append({"tag": "Ruby", "attr": {}, "children": ["漢", {"tag": "Rt", "attr": {}, "children": ["かん"]}]})
    text = law_tree_to_text(tree)
    assert "目次" not in text
    assert "かん" not in text
    assert "本文 二重 空白" in text


def test_tree_with_string_root_and_empty_children() -> None:
    assert law_tree_to_text("ただの文字列") == "ただの文字列\n"
    assert law_tree_to_text({"tag": "Law", "attr": {}, "children": []}) == "\n"
    assert law_tree_to_text({"tag": "Law"}) == "\n"


def test_xml_to_text_matches_tree_text() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Law Era="Reiwa" Year="8"><LawNum>令和八年法律第一号</LawNum><LawBody><LawTitle>試験法</LawTitle>'
        '<MainProvision><Article Num="1"><ArticleTitle>第一条</ArticleTitle><Paragraph Num="1"><ParagraphNum/>'
        "<ParagraphSentence><Sentence>この法律は、試験のための法律である。</Sentence></ParagraphSentence></Paragraph></Article>"
        '<Article Num="2"><ArticleTitle>第二条</ArticleTitle><Paragraph Num="1"><ParagraphNum/><ParagraphSentence>'
        "<Sentence>事業者は、<Ruby>帳<Rt>ちょう</Rt></Ruby>簿を備えなければならない。</Sentence></ParagraphSentence></Paragraph></Article>"
        "</MainProvision></LawBody></Law>"
    ).encode()
    text = law_xml_to_text(xml)
    assert text.splitlines() == ["試験法", "第一条 この法律は、試験のための法律である。", "第二条 事業者は、簿を備えなければならない。"]
    assert "ちょう" not in text


def test_xml_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid law XML"):
        law_xml_to_text(b"<Law><unclosed>")
