"""
변환기(_convert_html_to_storage 등)의 회귀 방지 테스트.

라이브 dokuwiki 없이 합성 fixture 만 사용한다. CI 에서 빠르게 돌리는 게
목적 — corpus 통계 검증은 별도.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# run.py 를 sys.path 에 올려 import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


SRC_ROOT = ROOT  # 미디어 lookup 은 본 테스트에서 무관


def _convert(html: str) -> str:
    storage, _links, _atts, _title, _tags = run._convert_html_to_storage(html, SRC_ROOT)
    return storage


def test_doku_macro_residue_stripped_in_paragraph() -> None:
    """미설치 플러그인의 ~~MACRO~~ 가 단독 문단에 남아있으면 strip."""
    html = '<p>~~DISCUSSION~~</p>'
    out = _convert(html)
    assert "~~DISCUSSION~~" not in out
    # 빈 문단으로 남거나 자동 정리되거나 — 어느 쪽이든 매크로 텍스트는 사라짐


def test_doku_macro_residue_preserved_in_code() -> None:
    """code/pre 안의 ~~MACRO~~ 는 *문서 내용* — 보존."""
    html = '<p>예: <code>~~NOTOC~~</code> 매크로</p>'
    out = _convert(html)
    assert "~~NOTOC~~" in out


def test_doku_macro_residue_preserved_in_table_cell() -> None:
    """표 셀의 ~~MACRO~~ 는 데이터 — 보존."""
    html = (
        '<table><tr>'
        '<td>~~NOTOC~~</td><td>TOC 생성을 막는다</td>'
        '</tr></table>'
    )
    out = _convert(html)
    assert "~~NOTOC~~" in out


def test_doku_macro_residue_mixed_text() -> None:
    """문단 안 inline 매크로는 strip 하되 주변 텍스트는 보존."""
    html = '<p>앞 ~~INFO:syntaxplugins~~ 뒤</p>'
    out = _convert(html)
    assert "~~INFO:syntaxplugins~~" not in out
    assert "앞" in out and "뒤" in out


def test_internal_link_via_data_wiki_id() -> None:
    """userewrite 켜진 path-style 링크: data-wiki-id 가 1순위."""
    html = '<p>see <a href="/playground/playground" class="wikilink1" data-wiki-id="playground:playground">play</a>.</p>'
    out = _convert(html)
    assert 'href="dwc-link:playground:playground"' in out
    assert '>play<' in out


def test_internal_link_via_query_id() -> None:
    """userewrite 꺼진 ?id= 형태도 동일하게 placeholder 화."""
    html = '<p><a href="/doku.php?id=wiki:syntax#headline" class="wikilink1">syntax</a></p>'
    out = _convert(html)
    assert 'href="dwc-link:wiki:syntax#headline"' in out


def test_external_link_passthrough() -> None:
    """외부 URL 은 그대로."""
    html = '<a href="https://example.com" class="urlextern">ex</a>'
    out = _convert(html)
    assert 'href="https://example.com"' in out
    assert "dwc-link" not in out


def test_image_to_ac_image() -> None:
    """내부 이미지 -> <ac:image><ri:attachment/>; 클릭 가능한 <a> 래퍼 unwrap."""
    html = '<a href="/_media/wiki/x.png" class="media"><img src="/_media/wiki/x.png?w=200" width="200"/></a>'
    out = _convert(html)
    assert "<ac:image" in out
    assert 'ri:filename="x.png"' in out
    # <a> 래퍼는 없어야
    assert '<a href="/_media/wiki/x.png"' not in out


def test_fetch_proxy_external_image_rewritten() -> None:
    """fetch.php?media=http(s)://... 형태 -> <img src=<decoded>>."""
    html = '<img src="/lib/exe/fetch.php?w=200&amp;tok=abc&amp;media=https%3A%2F%2Fwww.php.net%2Fimages%2Fphp.gif" alt=""/>'
    out = _convert(html)
    assert "https://www.php.net/images/php.gif" in out
    assert "ri:attachment" not in out


def test_url_decoded_media_path() -> None:
    """한국어 path-style 미디어 경로 decode."""
    html = '<a href="/_media/ride/files/%EB%8F%84%EC%84%A0%EC%82%AC.gpx" class="media">gpx</a>'
    out = _convert(html)
    assert 'ri:filename="도선사.gpx"' in out


def test_html_comments_removed() -> None:
    html = '<p>x</p><!-- EDIT{"target":"section"} --><p>y</p>'
    out = _convert(html)
    assert "EDIT" not in out
    assert "<!--" not in out


def test_secedit_anchor_dropped() -> None:
    html = '<h1>T<a class="secedit" href="?do=edit">edit</a></h1>'
    out = _convert(html)
    assert "secedit" not in out
    assert ">T" in out


def test_code_block_to_macro_with_cdata_escape() -> None:
    """']]>' 가 본문에 들어가도 CDATA 가 보존."""
    html = '<pre class="code python">def x(): print("hi]]>")</pre>'
    out = _convert(html)
    assert 'ac:name="code"' in out
    assert 'ac:name="language"' in out
    # canonical CDATA escape
    assert "]]]]><![CDATA[>" in out


def test_wrap_callouts_to_macro() -> None:
    """block wrap 의미 클래스 -> info/tip/note/warning/panel 매크로."""
    html = '<div class="wrap_info plugin_wrap">x</div>'
    assert 'ac:name="info"' in _convert(html)
    html = '<div class="wrap_tip plugin_wrap">x</div>'
    assert 'ac:name="tip"' in _convert(html)
    html = '<div class="wrap_important plugin_wrap">x</div>'
    assert 'ac:name="note"' in _convert(html)
    html = '<div class="wrap_alert plugin_wrap">x</div>'
    assert 'ac:name="warning"' in _convert(html)
    html = '<div class="wrap_box plugin_wrap">x</div>'
    assert 'ac:name="panel"' in _convert(html)


def test_wrap_inline_em_hi() -> None:
    """wrap_em -> <strong>, wrap_hi -> 노란 background span."""
    html = '<em class="wrap_em plugin_wrap">A</em> and <em class="wrap_hi plugin_wrap">B</em>'
    out = _convert(html)
    assert "<strong>A</strong>" in out
    assert 'background-color: #fff59d' in out


def test_alignment_to_inline_style() -> None:
    """wrap_left/right/center, 표 셀 정렬 -> text-align inline style."""
    html = '<div class="wrap_center plugin_wrap">x</div>'
    assert "text-align: center" in _convert(html)
    html = '<table><tr><td class="centeralign">x</td></tr></table>'
    out = _convert(html)
    assert "text-align: center" in out
    assert 'class="centeralign"' not in out


def test_underline_em_to_u_tag() -> None:
    """<em class="u"> -> <u>."""
    html = '<em class="u">밑줄</em>'
    out = _convert(html)
    assert "<u>밑줄</u>" in out


def test_footnote_section_rewritten() -> None:
    """페이지 끝 footnotes div -> <hr/><p><strong>각주</strong></p><ol>."""
    html = (
        '<p>본문<sup><a class="fn_top" href="#fn__1" id="fnt__1">1)</a></sup></p>'
        '<div class="footnotes"><div class="fn">'
        '<sup><a class="fn_bot" href="#fnt__1" id="fn__1">1)</a></sup>'
        '<div class="content">노트</div>'
        '</div></div>'
    )
    out = _convert(html)
    assert "<strong>각주</strong>" in out
    # Confluence 가 <li id=> 의 id 를 제거하므로 anchor 매크로로 표시
    assert 'ac:name="anchor"' in out
    assert "fn__1" in out  # anchor target
    assert "<div class=\"footnotes\">" not in out
    # 본문의 sup 은 유지 (anchor link 동작)
    assert 'href="#fn__1"' in out


def test_risky_tags_stripped() -> None:
    """script/style/link/form/iframe/input 등 일괄 제거."""
    html = (
        "<p>ok</p>"
        '<script>alert(1)</script>'
        "<style>.x{}</style>"
        '<link rel="stylesheet"/>'
        '<form><input type="text"/></form>'
        '<iframe src="x"></iframe>'
    )
    out = _convert(html)
    for t in ("<script", "<style", "<link", "<form", "<input", "<iframe"):
        assert t not in out


def test_chrome_stripped() -> None:
    """dokuwiki chrome id/class 제거."""
    html = (
        '<div id="dokuwiki__header">hdr</div>'
        '<div class="breadcrumbs">crumbs</div>'
        '<div class="page">real</div>'
    )
    out = _convert(html)
    assert "hdr" not in out
    assert "crumbs" not in out
    assert "real" in out


def test_todo_pure_ul_to_task_list() -> None:
    """순수 todo <ul> -> <ac:task-list>."""
    html = (
        "<ul>"
        '  <li><div class="li"><span class="todo">'
        '    <input class="todocheckbox" type="checkbox" checked="checked"/>'
        '    <span class="todoinnertext">완료</span>'
        "  </span></div></li>"
        '  <li><div class="li"><span class="todo">'
        '    <input class="todocheckbox" type="checkbox"/>'
        '    <span class="todoinnertext">미완</span>'
        "  </span></div></li>"
        "</ul>"
    )
    out = _convert(html)
    assert "<ac:task-list>" in out
    assert "<ac:task-status>complete</ac:task-status>" in out
    assert "<ac:task-status>incomplete</ac:task-status>" in out
    # 본문 텍스트 보존
    assert "완료" in out and "미완" in out


def test_todo_mixed_falls_back_to_text_marker() -> None:
    """todo + 트레일링 텍스트 -> [x]/[ ] 텍스트 폴백."""
    html = (
        "<ul>"
        '<li><div class="li"><span class="todo">'
        '<input class="todocheckbox" type="checkbox"/>'
        '<span class="todoinnertext">A</span>'
        "</span>: 추가 텍스트</div></li>"
        "</ul>"
    )
    out = _convert(html)
    assert "[ ] A" in out
    assert "<ac:task-list>" not in out


def test_void_elements_self_closed() -> None:
    """<br>/<hr> XML self-close."""
    html = "<p>x<br></p><hr>"
    out = _convert(html)
    assert "<br/>" in out
    assert "<hr/>" in out


def test_h1_title_extracted() -> None:
    """첫 h1 텍스트가 title 후보."""
    html = '<h1 class="sectionedit1" id="t">Hello</h1><p>x</p>'
    _xml, _links, _atts, title, _tags = run._convert_html_to_storage(html, SRC_ROOT)
    assert title == "Hello"


def test_disambiguator_format() -> None:
    """_disambiguate_duplicate_titles 의 결과 형식."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    run.db_init(conn)
    rows = [
        ("a:b:c:05", "a:b:c", None, 0, "05"),
        ("x:y:z:05", "x:y:z", None, 0, "05"),
        ("solo", "", None, 1, "solo"),
    ]
    for doku_id, ns, parent, is_idx, title in rows:
        conn.execute(
            "INSERT INTO pages(doku_id, src_path, namespace, parent_doku_id, "
            "is_namespace_index, title, content_hash, status, discovered_at, last_checked_at) "
            "VALUES (?, '<f>', ?, ?, ?, ?, 'h', 'CONVERTED', ?, ?)",
            (doku_id, ns, parent, is_idx, title, run.now_iso(), run.now_iso()),
        )
    conn.commit()
    updated = run._disambiguate_duplicate_titles(conn)
    assert updated >= 2
    new_titles = dict(
        conn.execute("SELECT doku_id, title FROM pages").fetchall()
    )
    assert new_titles["a:b:c:05"] == "05 (a:b:c)"
    assert new_titles["x:y:z:05"] == "05 (x:y:z)"
    assert new_titles["solo"] == "solo"


def test_lint_passes_minimal_doc() -> None:
    """cmd_lint 는 valid storage XML 1개에 대해 0 반환."""
    import argparse
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.xml"
        p.write_text(
            '<ac:image><ri:attachment ri:filename="a.png"/></ac:image>'
            "<p>ok</p>",
            encoding="utf-8",
        )
        ns = argparse.Namespace(path=str(p), limit=20)
        rc = run.cmd_lint(ns)
        assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
