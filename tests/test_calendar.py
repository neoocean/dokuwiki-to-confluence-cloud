"""monthcal 플러그인 fallback + Google Calendar iframe 변환 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def _convert(html: str) -> str:
    storage, _links, _atts, _title, _tags = run._convert_html_to_storage(html, ROOT)
    return storage


# ─── monthcal fallback ─────────────────────────────────────────────


def test_monthcal_fallback_basic() -> None:
    """{{monthcal:align=right,namespace=.:j,month=12,year=2019,week_start_on=sunday}}
    가 DokuWiki 에서 monthcal 플러그인 미설치일 때 깨진 media 링크로 출력됨."""
    html = (
        '<p><a href="/_media/monthcal/align_right_namespace/j_month_12_year_2019_week_start_on_sunday" '
        'class="media mediafile mf_ wikilink2" title="monthcal:...">link text</a></p>'
    )
    out = _convert(html)
    assert "dwc-monthcal" in out
    assert "2019-12" in out
    # 요일 헤더 (sunday start)
    assert "<th>Sun</th>" in out
    assert "<th>Sat</th>" in out
    # 31일 모두 있음
    for d in (1, 15, 31):
        assert f">{d}</a>" in out
    # 페이지 링크 placeholder
    assert "dwc-link:j:2019:12:01" in out
    assert "dwc-link:j:2019:12:31" in out


def test_monthcal_monday_start_default() -> None:
    html = (
        '<p><a href="/_media/monthcal/namespace/j_month_06_year_2020" '
        'class="media mediafile">link</a></p>'
    )
    out = _convert(html)
    # default monday start
    assert "<th>Mon</th>" in out
    assert "<th>Sun</th>" in out


def test_monthcal_no_namespace() -> None:
    html = (
        '<p><a href="/_media/monthcal/namespace/_month_01_year_2020" '
        'class="media mediafile">x</a></p>'
    )
    out = _convert(html)
    # namespace 없으면 페이지 링크 없이 plain text 일/날짜
    assert "dwc-link:" not in out
    # 그래도 캘린더 표는 있음
    assert "dwc-monthcal" in out


def test_monthcal_multiple_on_page() -> None:
    """한 페이지에 monthcal 매크로가 여러 개 — 모두 변환."""
    html = (
        '<p><a href="/_media/monthcal/namespace/a_month_01_year_2020" class="media mediafile">x</a></p>'
        '<h2>중간</h2>'
        '<p><a href="/_media/monthcal/namespace/a_month_02_year_2020" class="media mediafile">y</a></p>'
    )
    out = _convert(html)
    assert out.count("dwc-monthcal") == 2
    assert "2020-01" in out
    assert "2020-02" in out


def test_monthcal_non_match_left_alone() -> None:
    """monthcal 패턴이 아닌 일반 media 링크는 영향 받지 않음."""
    html = (
        '<p><a href="/_media/regular/photo.jpg" class="media mediafile">photo</a></p>'
    )
    out = _convert(html)
    assert "dwc-monthcal" not in out


# ─── Google Calendar iframe ────────────────────────────────────────


def test_calendar_iframe_real_tag() -> None:
    """실제 iframe 태그 (html 플러그인 활성)."""
    html = (
        '<iframe src="https://calendar.google.com/calendar/embed?src=x" '
        'width="750" height="500" frameborder="0"></iframe>'
    )
    out = _convert(html)
    assert 'ac:name="iframe"' in out
    assert "calendar.google.com" in out


def test_youtube_vid_only_paragraph() -> None:
    """`{{youtube>VID}}` 완전 깨진 fallback — VID 만 단독 paragraph 로 노출.
    11자 base64-ish → Confluence iframe 매크로로 자동 변환."""
    html = "<p>NEbzsV6qzQ0</p>"
    out = _convert(html)
    assert 'ac:name="iframe"' in out
    assert "youtube.com/embed/NEbzsV6qzQ0" in out


def test_youtube_vid_only_paragraph_with_dash_underscore() -> None:
    """url-safe base64 alphabet — `-` 와 `_` 포함."""
    html = "<p>YeZ-iUdoa_0</p>"
    out = _convert(html)
    assert "youtube.com/embed/YeZ-iUdoa_0" in out


def test_youtube_vid_no_false_positive_short_text() -> None:
    """11자 미만 — VID 아님, 변환 안 됨."""
    html = "<p>short</p>"
    out = _convert(html)
    assert "youtube.com/embed" not in out


def test_youtube_vid_no_false_positive_with_inline_element() -> None:
    """<p> 안에 인라인 element 가 있으면 VID-only 아님."""
    html = "<p>NEbzsV6qzQ0 <a href='x'>link</a></p>"
    out = _convert(html)
    assert "youtube.com/embed" not in out


def test_calendar_iframe_escaped_text() -> None:
    """html 플러그인 미활성 — iframe 이 escape 되어 텍스트로 + URL 만 auto-link."""
    html = (
        '<p>&lt;iframe src=“'
        '<a href="https://calendar.google.com/calendar/embed?src=x" class="urlextern">'
        'https://calendar.google.com/...</a>'
        '” width=“750” height=“500”&gt;&lt;/iframe&gt;</p>'
    )
    out = _convert(html)
    assert 'ac:name="iframe"' in out
    assert "calendar.google.com" in out
    # 원본 escape 된 텍스트 잔존 안 함
    assert "&lt;iframe" not in out


def test_calendar_iframe_non_calendar_ignored() -> None:
    """Google Calendar 가 아닌 iframe 은 변환 안 됨 (기존 strip 동작)."""
    html = '<iframe src="https://example.com/widget" width="500" height="300"></iframe>'
    out = _convert(html)
    assert 'ac:name="iframe"' not in out
    # iframe 자체는 strip
    assert "<iframe" not in out


def test_calendar_iframe_extracts_width_height() -> None:
    html = (
        '<iframe src="https://calendar.google.com/calendar/embed?src=y" '
        'width="900" height="600"></iframe>'
    )
    out = _convert(html)
    assert 'ac:name="width">900</ac:parameter>' in out
    assert 'ac:name="height">600</ac:parameter>' in out


# ─── encryptedpasswords ─────────────────────────────────────────


def test_encrypt_decrypt_to_expand() -> None:
    html = "<p>password: &lt;decrypt&gt;U2FsdGVkX1/abc=&lt;/decrypt&gt;</p>"
    out = _convert(html)
    assert 'ac:name="expand"' in out
    # cipher 그대로 보존
    assert "U2FsdGVkX1/abc=" in out
    # inline code 형식
    assert "<code>" in out
    # 태그도 그대로 (escape) — 복호화 시 사용
    assert "&lt;decrypt&gt;" in out and "&lt;/decrypt&gt;" in out


def test_encrypt_tag_variant() -> None:
    html = "<p>x &lt;encrypt&gt;CIPHER&lt;/encrypt&gt; y</p>"
    out = _convert(html)
    assert 'ac:name="expand"' in out
    assert "CIPHER" in out
    assert "&lt;encrypt&gt;" in out


def test_encrypt_multiple_in_page() -> None:
    html = (
        "<p>a &lt;decrypt&gt;c1&lt;/decrypt&gt; b</p>"
        "<p>c &lt;decrypt&gt;c2&lt;/decrypt&gt; d</p>"
    )
    out = _convert(html)
    assert out.count('ac:name="expand"') == 2
    assert "c1" in out and "c2" in out


def test_encrypt_no_match_left_alone() -> None:
    html = "<p>plain text without decrypt</p>"
    out = _convert(html)
    assert 'ac:name="expand"' not in out


def test_encrypt_preserves_surrounding_text() -> None:
    html = "<p>before &lt;decrypt&gt;CIPHER&lt;/decrypt&gt; after</p>"
    out = _convert(html)
    assert "before" in out
    assert "after" in out
    assert "CIPHER" in out
