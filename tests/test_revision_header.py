"""revision 헤더 형식 옵션 + 기존 헤더 strip 회귀 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


# rev_ts = 2019-01-03T06:21:35Z (사용자 스크린샷의 예시 시각)
RT = 1546496495
UM: dict[str, str] = {}


def test_header_default_is_panel() -> None:
    assert run.REVISION_HEADER_DEFAULT == "panel"


def test_header_none_returns_empty() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="none")
    assert out == ""


def test_header_panel_uses_br() -> None:
    """기본 (panel) — 한 단락 + shift+enter (<br/>) 줄바꿈."""
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="panel")
    assert 'ac:name="panel"' in out
    assert "<p>" in out and "</p>" in out
    # 3개 라인이 한 <p> 안에 br/ 로 구분
    assert out.count("<br/>") == 2
    assert "DokuWiki revision" in out
    assert "Author: alice" in out
    assert "Comment:" in out
    # 3개의 별도 <p> 가 아님
    assert out.count("<p>") == 1


def test_header_info_macro() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="info")
    assert 'ac:name="info"' in out
    assert out.count("<br/>") == 2


def test_header_note_macro() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="note")
    assert 'ac:name="note"' in out
    assert out.count("<br/>") == 2


def test_header_tip_macro() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="tip")
    assert 'ac:name="tip"' in out


def test_header_warning_macro() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="warning")
    assert 'ac:name="warning"' in out


def test_header_quote() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="quote")
    assert out.startswith("<blockquote>")
    assert "<ac:structured-macro" not in out
    assert out.count("<br/>") == 2


def test_header_table() -> None:
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="table")
    assert out.startswith("<table>")
    # 3 행 × 2 열
    assert out.count("<tr>") == 3
    assert out.count("<th>") == 3
    assert out.count("<td>") == 3
    assert "<br/>" not in out


def test_header_paragraphs_legacy() -> None:
    """기존 형식 — 3개의 <p>, note 매크로."""
    out = run._revision_header(RT, "alice", "msg", "E", UM, fmt="paragraphs")
    assert 'ac:name="note"' in out
    assert out.count("<p>") == 3
    assert "<br/>" not in out


def test_header_type_label_mapping() -> None:
    e = run._revision_header(RT, "a", "c", "E", UM, fmt="panel")
    assert "(edit)" in e
    c = run._revision_header(RT, "a", "c", "C", UM, fmt="panel")
    assert "(create)" in c
    me = run._revision_header(RT, "a", "c", "e", UM, fmt="panel")
    assert "(minor edit)" in me


def test_header_unknown_user() -> None:
    out = run._revision_header(RT, None, "msg", "E", UM, fmt="panel")
    assert "(unknown)" in out


def test_header_empty_comment() -> None:
    out = run._revision_header(RT, "a", None, "E", UM, fmt="panel")
    assert "(no comment)" in out


def test_header_xss_escape_in_comment() -> None:
    out = run._revision_header(RT, "a", "<script>alert(1)</script>", "E", UM, fmt="panel")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# ---------- _strip_revision_header ----------


def test_strip_paragraphs_legacy() -> None:
    """기존 paragraphs 형식 (3 <p>) 제거."""
    body = run._revision_header(RT, "alice", "msg", "E", UM, fmt="paragraphs")
    body += "<h1>Real content</h1>"
    stripped = run._strip_revision_header(body)
    assert "DokuWiki revision" not in stripped
    assert "<h1>Real content</h1>" in stripped


def test_strip_panel_format() -> None:
    body = run._revision_header(RT, "alice", "msg", "E", UM, fmt="panel")
    body += "<h1>Real</h1>"
    stripped = run._strip_revision_header(body)
    assert "DokuWiki revision" not in stripped
    assert "<h1>Real</h1>" in stripped


def test_strip_info_format() -> None:
    body = run._revision_header(RT, "alice", "msg", "E", UM, fmt="info")
    body += "<p>본문</p>"
    stripped = run._strip_revision_header(body)
    assert "<p>본문</p>" == stripped


def test_strip_quote_format() -> None:
    body = run._revision_header(RT, "alice", "msg", "E", UM, fmt="quote")
    body += "<h1>X</h1>"
    stripped = run._strip_revision_header(body)
    assert "DokuWiki revision" not in stripped
    assert "<h1>X</h1>" in stripped


def test_strip_table_format() -> None:
    body = run._revision_header(RT, "alice", "msg", "E", UM, fmt="table")
    body += "<p>content</p>"
    stripped = run._strip_revision_header(body)
    assert "DokuWiki revision" not in stripped
    assert "<p>content</p>" in stripped


def test_strip_preserves_inline_panel_in_body() -> None:
    """본문 *중간* 의 panel 매크로 (revision 헤더가 아닌 진짜 콘텐츠) 보존."""
    body = (
        "<h1>Title</h1>"
        "<p>some content</p>"
        '<ac:structured-macro ac:name="panel"><ac:rich-text-body>'
        "<p>이건 진짜 콘텐츠 panel</p>"
        "</ac:rich-text-body></ac:structured-macro>"
    )
    stripped = run._strip_revision_header(body)
    assert stripped == body  # 변경 없음 — 매크로가 본문 시작이 아니므로


def test_strip_no_header() -> None:
    body = "<h1>X</h1><p>y</p>"
    assert run._strip_revision_header(body) == body
