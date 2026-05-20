"""visual-audit Phase 3 자동 신호 (sentence/artifact/code/heading/link) 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def test_sentence_align_identical() -> None:
    d = "안녕하세요. 마이그레이션 도구 입니다. 페이지를 옮깁니다."
    out = run._sentence_align(d, d)
    assert out["sentence_ratio"] == 1.0
    assert out["missing"] == 0
    assert out["added"] == 0


def test_sentence_align_loss() -> None:
    d = "첫 문장입니다. 두 번째 문장입니다. 세 번째 문장도 있습니다. 네 번째 문장은 길이가 더 있다네요."
    c = "첫 문장입니다. 네 번째 문장은 길이가 더 있다네요."
    out = run._sentence_align(d, c)
    assert out["sentence_ratio"] < 1.0
    assert out["missing"] >= 1
    assert out["d_sentences"] >= 3
    assert any("두 번째" in s or "세 번째" in s for s in out["examples_missing"])


def test_extract_artifacts() -> None:
    text = "전화 010-1234-5678 / 회의는 2026-05-19 에. https://example.com/x 메일 me@x.com"
    out = run._extract_artifacts(text)
    assert "010-1234-5678" in out["number_seq"]
    assert "2026-05-19" in out["number_seq"]
    assert "https://example.com/x" in out["url"]
    assert "me@x.com" in out["email"]


def test_compare_artifacts_missing() -> None:
    d = "전화 010-1234-5678 / 일정 2026-05-19. https://a.example.com"
    c = "전화 010-1234-5678 / 일정 2026-05-19."  # URL 빠짐
    diff = run._compare_artifacts(d, c)
    assert diff["url"]["missing"] == 1
    assert "https://a.example.com" in diff["url"]["examples_missing"]


def test_compare_code_blocks_match() -> None:
    d = '<pre class="code">def x():\n    return 42</pre>'
    c = ('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
         '<![CDATA[def x():\n    return 42]]></ac:plain-text-body>'
         '</ac:structured-macro>')
    out = run._compare_code_blocks(d, c)
    assert out["d_code_blocks"] == 1
    assert out["c_code_blocks"] == 1
    assert out["matched"] == 1


def test_compare_code_blocks_loss() -> None:
    d = ('<pre class="code">block one</pre>'
         '<pre class="file">block two</pre>')
    c = ('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
         '<![CDATA[block one]]></ac:plain-text-body></ac:structured-macro>')
    out = run._compare_code_blocks(d, c)
    assert out["d_code_blocks"] == 2
    assert out["c_code_blocks"] == 1
    assert out["missing"] >= 1


def test_link_resolution_rate() -> None:
    storage = (
        '<ac:link><ri:page ri:content-title="a"/></ac:link>'
        '<ac:link><ri:page ri:content-title="b"/></ac:link>'
        '<a href="dwc-link:foo">unresolved</a>'
    )
    out = run._link_resolution_rate(storage)
    assert out["resolved"] == 2
    assert out["placeholder"] == 1
    assert out["rate"] == round(2 / 3, 3)


def test_heading_seq_lcs() -> None:
    d = "<h1>Intro</h1><h2>API</h2><h2>Examples</h2><h2>FAQ</h2>"
    c = "<h1>Intro</h1><h2>API</h2><h2>FAQ</h2>"  # Examples 빠짐
    out = run._compare_heading_seq(d, c)
    assert out["d_headings"] == 4
    assert out["c_headings"] == 3
    assert out["missing"] == 1
    assert any("Examples" in e for e in out["examples_missing"])
