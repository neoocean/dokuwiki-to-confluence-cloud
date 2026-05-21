"""3-측 invariant audit (docs/3way-audit.md) 의 신호 함수 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


# --- S1: plugin marker / rendered element 부재 ---


def test_S1_htmlok_missing_detected() -> None:
    """사례 A — source 에 <html><iframe>... 인데 rendered 에 escape 텍스트."""
    source = '<html><iframe src="https://calendar.google.com/embed"></iframe></html>'
    rendered = '<p>&lt;html&gt;&lt;iframe src="..."&gt;&lt;/iframe&gt;&lt;/html&gt;</p>'
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    htmlok = [v for v in vios if v.get("plugin") == "htmlok"]
    assert htmlok, f"htmlok 위반 감지 안 됨: {vios}"
    assert htmlok[0]["responsibility"] == "source"
    assert htmlok[0]["severity"] == "high"  # escape 노출이라 high


def test_S1_htmlok_present_no_violation() -> None:
    """htmlok 가 정상 작동하면 iframe 가 raw HTML 로 rendered — 위반 없음."""
    source = "<html><iframe src='x'></iframe></html>"
    rendered = '<p><iframe src="x"></iframe></p>'
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    assert not any(v.get("plugin") == "htmlok" for v in vios)


def test_S1_monthcal_missing() -> None:
    """사례 B — source 의 ~~monthcal~~ 가 rendered 에서 결과 element 부재."""
    source = "===== Calendar =====\n\n~~monthcal~~\n"
    rendered = "<h1>Calendar</h1>\n<p>~~monthcal~~</p>"  # plugin 미설치
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    assert any(v.get("plugin") == "monthcal" for v in vios)


def test_S1_wrap_missing() -> None:
    source = "<wrap info>안녕</wrap>"
    rendered = "<p>&lt;wrap info&gt;안녕&lt;/wrap&gt;</p>"
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    assert any(v.get("plugin") == "wrap" for v in vios)


def test_S1_decrypt_escape_no_violation() -> None:
    """encryptedpasswords source + rendered 에 escape 텍스트 — 변환기가
    _preprocess_encrypted_passwords 로 처리 가능. S1 위반 아님 (informational)."""
    source = "<decrypt>cipher123</decrypt>"
    rendered = "<p>&lt;decrypt&gt;cipher123&lt;/decrypt&gt;</p>"
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    # rendered_required 가 escape 도 매치 → 위반 아님
    assert not any(v.get("plugin") == "encryptedpasswords" for v in vios)


def test_S1_decrypt_completely_missing_violation() -> None:
    """source 에 <decrypt> 인데 rendered 에 marker 자체 부재 — S1 위반."""
    source = "<decrypt>cipher</decrypt>"
    rendered = "<p>(plugin 미해석으로 marker 까지 사라짐)</p>"
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    assert any(v.get("plugin") == "encryptedpasswords" for v in vios)


# --- source noise strip (코드 블록 안 marker 는 무시) ---


def test_strip_code_block_avoids_false_positive() -> None:
    """source 의 <code>...</code> 안에 <html> 같은 marker 가 있어도 S1 안 잡힘."""
    source = (
        "본문\n<code>\n<html><iframe src='x'></iframe></html>\n</code>\n끝"
    )
    rendered = "<p>본문 끝</p>"  # html 매크로 element 부재
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    # code 안 marker 는 noise — S1 매치 안 함
    assert not any(v.get("plugin") == "htmlok" for v in vios)


def test_strip_html_comment() -> None:
    source = "<!-- <html><iframe></iframe></html> -->\n본문"
    rendered = "<p>본문</p>"
    src_clean = run._audit_3way_strip_source_noise(source)
    vios = run._audit_3way_signal_S1(src_clean, rendered)
    assert not any(v.get("plugin") == "htmlok" for v in vios)


# --- S2: escape text 노출 ---


def test_S2_macro_escape_exposed() -> None:
    rendered = "<p>&lt;decrypt&gt;cipher&lt;/decrypt&gt; 그리고 &lt;monthcal&gt;</p>"
    vios = run._audit_3way_signal_S2(rendered)
    assert vios
    # tags 에 decrypt 또는 monthcal 포함
    tags = dict(vios[0]["tags"])
    assert "decrypt" in tags or "monthcal" in tags


def test_S2_no_false_positive_on_html_tags() -> None:
    """rendered 의 &lt;a&gt; / &lt;p&gt; 같은 HTML 태그 escape 는 noise 제거."""
    rendered = "<p>코드 예: &lt;a href='x'&gt;링크&lt;/a&gt;</p>"
    vios = run._audit_3way_signal_S2(rendered)
    # a 는 noise list 에 있어 제거됨 — 다른 매크로 없으면 비어 있어야
    assert not vios


# --- D1: 매크로 카운트 mismatch ---


def test_D1_wrap_info_match() -> None:
    rendered = (
        '<div class="wrap_info"><p>x</p></div>'
        '<div class="wrap_info"><p>y</p></div>'
    )
    confluence = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>x</ac:rich-text-body></ac:structured-macro>'
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>y</ac:rich-text-body></ac:structured-macro>'
    )
    vios = run._audit_3way_signal_D1(rendered, confluence)
    assert not any(v.get("macro") == "wrap_info" for v in vios)


def test_D1_wrap_info_loss() -> None:
    """rendered 는 wrap_info 2개인데 confluence 는 1개 — 손실 위반."""
    rendered = (
        '<div class="wrap_info"><p>x</p></div>'
        '<div class="wrap_info"><p>y</p></div>'
    )
    confluence = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>x</ac:rich-text-body></ac:structured-macro>'
    )
    vios = run._audit_3way_signal_D1(rendered, confluence)
    matches = [v for v in vios if v.get("macro") == "wrap_info"]
    assert matches
    assert matches[0]["signal"] == "D1.macro_count_loss"
    assert matches[0]["responsibility"] == "converter"
    assert matches[0]["loss"] == 1


def test_D1_macro_addition_not_violation() -> None:
    """confluence 가 추가 매크로 생성 (revision header 등) 는 위반 아님."""
    rendered = '<div class="wrap_info"><p>x</p></div>'
    confluence = (
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>header</ac:rich-text-body></ac:structured-macro>'
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>x</ac:rich-text-body></ac:structured-macro>'
    )
    vios = run._audit_3way_signal_D1(rendered, confluence)
    assert not any(v.get("macro") == "wrap_info" for v in vios)


def test_D1_todo_inline_always_whitelisted() -> None:
    """todo_checkbox 매크로 매핑은 intent_always (inline 격하) — 항상 화이트리스트."""
    rendered = '<input type="checkbox"/>' * 10
    confluence = "[x]"  # 1 개만 매치
    vios = run._audit_3way_signal_D1(rendered, confluence)
    assert not any(v.get("macro") == "todo_checkbox" for v in vios)


# --- D2: wrap color → code 오변환 (사례 D) ---


def test_D2_wrap_color_misroute_detected() -> None:
    """rendered 에 wrap_yellow (unknown class) + confluence 에 code 매크로 비정상 많음."""
    rendered = (
        '<div class="wrap_yellow" style="background:yellow"><p>강조</p></div>'
        '<div class="wrap_pink" style="background:pink"><p>강조2</p></div>'
    )
    confluence = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>강조</ac:plain-text-body></ac:structured-macro>'
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>강조2</ac:plain-text-body></ac:structured-macro>'
    )
    vios = run._audit_3way_signal_D2(rendered, confluence)
    assert vios
    assert vios[0]["signal"] == "D2.wrap_color_to_code_misroute"
    assert "wrap_yellow" in vios[0]["unknown_wraps"] or "wrap_pink" in vios[0]["unknown_wraps"]
    assert vios[0]["code_excess"] == 2


def test_D2_known_wraps_no_violation() -> None:
    """rendered 의 wrap_info (known class) 는 D2 위반 아님."""
    rendered = '<div class="wrap_info"><p>x</p></div>'
    confluence = '<ac:structured-macro ac:name="code"><ac:plain-text-body>x</ac:plain-text-body></ac:structured-macro>'
    vios = run._audit_3way_signal_D2(rendered, confluence)
    # wrap_info 는 known — D2 위반 아님 (D1 이 매크로 매핑 mismatch 잡음)
    assert not vios


# --- D3: 이미지 cluster 분리 (사례 C) ---


def test_D3_image_cluster_split() -> None:
    """rendered 의 <p><img><img><img></p> 가 confluence 에서 별도 <p> 로 분리."""
    rendered = '<p><img src="a"/><img src="b"/><img src="c"/></p>'
    confluence = (
        '<p><ac:image><ri:attachment ri:filename="a"/></ac:image></p>'
        '<p><ac:image><ri:attachment ri:filename="b"/></ac:image></p>'
        '<p><ac:image><ri:attachment ri:filename="c"/></ac:image></p>'
    )
    vios = run._audit_3way_signal_D3(rendered, confluence)
    assert vios
    assert vios[0]["signal"] == "D3.image_cluster_split"
    assert vios[0]["rendered_clusters"] == 1
    assert vios[0]["confluence_clusters"] == 0


def test_D3_image_cluster_preserved() -> None:
    rendered = '<p><img src="a"/><img src="b"/><img src="c"/></p>'
    confluence = (
        '<p><ac:image><ri:attachment ri:filename="a"/></ac:image>'
        '<ac:image><ri:attachment ri:filename="b"/></ac:image>'
        '<ac:image><ri:attachment ri:filename="c"/></ac:image></p>'
    )
    vios = run._audit_3way_signal_D3(rendered, confluence)
    assert not vios  # 양측 모두 클러스터 1개


def test_D3_single_image_no_violation() -> None:
    """2 미만 img 는 cluster 아님."""
    rendered = '<p><img src="a"/></p><p><img src="b"/></p>'
    confluence = (
        '<p><ac:image><ri:attachment ri:filename="a"/></ac:image></p>'
        '<p><ac:image><ri:attachment ri:filename="b"/></ac:image></p>'
    )
    vios = run._audit_3way_signal_D3(rendered, confluence)
    assert not vios


# --- 종합 analyze ---


def test_analyze_full_violations() -> None:
    """사례 A + C 양쪽 모두 violation 생성."""
    source = '<html><iframe src="x"></iframe></html>'
    rendered = (
        '<p>&lt;html&gt;&lt;iframe&gt;...&lt;/iframe&gt;&lt;/html&gt;</p>'
        '<p><img src="a"/><img src="b"/><img src="c"/></p>'
    )
    confluence = (
        '<p>&lt;html&gt;&lt;iframe&gt;...&lt;/iframe&gt;&lt;/html&gt;</p>'
        '<p><ac:image><ri:attachment ri:filename="a"/></ac:image></p>'
        '<p><ac:image><ri:attachment ri:filename="b"/></ac:image></p>'
        '<p><ac:image><ri:attachment ri:filename="c"/></ac:image></p>'
    )
    result = run._audit_3way_analyze("test:page", source, rendered, confluence)
    sigs = {v["signal"] for v in result["violations"]}
    assert "S1.plugin_render_missing" in sigs
    assert "D3.image_cluster_split" in sigs
    assert result["severity_counts"]["source_high"] >= 1
    assert result["severity_counts"]["converter_medium"] >= 1


def test_analyze_clean_page() -> None:
    """모든 매크로가 정상 — violation 없음."""
    rendered = (
        '<h1>제목</h1>'
        '<div class="wrap_info"><p>안내</p></div>'
        '<p>본문</p>'
    )
    confluence = (
        '<h1>제목</h1>'
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>안내</ac:rich-text-body></ac:structured-macro>'
        '<p>본문</p>'
    )
    result = run._audit_3way_analyze("test:clean", None, rendered, confluence)
    # rendered/confluence 만 — D 그룹만 검사
    assert not result["violations"]
    assert all(v == 0 for v in result["severity_counts"].values())


def test_analyze_monthcal_fallback_whitelisted() -> None:
    """source 에 ~~monthcal~~ + confluence 에 정적 표 — fallback 화이트리스트."""
    source = "~~monthcal~~"
    rendered = "<p>~~monthcal~~</p>"  # plugin 미설치
    confluence = '<table><tr><th>일</th><th>월</th><th>화</th></tr></table>'
    result = run._audit_3way_analyze("test:cal", source, rendered, confluence)
    # monthcal_fallback 이 intent 에 들어가야
    assert "monthcal_fallback" in result["intent"]
    # S1.monthcal violation 제거됨
    assert not any(v.get("plugin") == "monthcal" for v in result["violations"])
