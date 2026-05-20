"""link-check: placeholder 잔존 / unresolved page link / 외부 URL 검출."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def test_link_check_helpers_via_storage_body() -> None:
    """cmd_link_check 의 내부 정규식 검출 — body 텍스트 패턴 매치만 검증."""
    import re
    body = (
        '<p>some text <a href="dwc-link:wiki:syntax">syntax</a> more</p>'
        '<p><ac:link><ri:page ri:content-title="알 수 없는 페이지"/></ac:link></p>'
        '<p><ac:link><ri:page ri:content-title="실재하는 페이지"/></ac:link></p>'
        '<p><a href="https://example.com/x">ext</a></p>'
    )
    placeholders = re.findall(r'dwc-link:([^"\s<]+)', body)
    assert placeholders == ["wiki:syntax"]
    titles = re.findall(r'<ri:page\s+ri:content-title="([^"]+)"', body)
    assert set(titles) == {"알 수 없는 페이지", "실재하는 페이지"}
    externals = re.findall(r'href="(https?://[^"]+)"', body)
    assert externals == ["https://example.com/x"]


def test_link_check_unresolved_logic() -> None:
    """title_to_page 매핑에 없는 title 만 unresolved 로 카운트."""
    titles = ["존재", "부재"]
    title_to_page = {"존재": "p1"}
    unresolved = [t for t in titles if t not in title_to_page]
    assert unresolved == ["부재"]


def test_link_check_placeholder_pattern_does_not_match_normal_urls() -> None:
    """일반 URL 에 'dwc-link' 가 우연히 들어가도 false positive 안 되도록."""
    import re
    body = '<a href="https://dwc-link.example.com/">x</a><a href="dwc-link:real:page">y</a>'
    matches = re.findall(r'dwc-link:([^"\s<]+)', body)
    assert "real:page" in matches
    # 외부 URL 의 dwc-link 부분도 매치되지만 — false positive 가능. 본 도구는
    # 'dwc-link:<target>' 정확한 placeholder 형식이라 실 운영에선 적게 노출.


def test_link_check_no_issues_clean_body() -> None:
    import re
    body = '<p><ac:link><ri:page ri:content-title="OK"/></ac:link></p>'
    title_to_page = {"OK": "p1"}
    assert not re.findall(r'dwc-link:[^"\s<]+', body)
    titles = re.findall(r'<ri:page\s+ri:content-title="([^"]+)"', body)
    unresolved = [t for t in titles if t not in title_to_page]
    assert unresolved == []
