"""struct cell 렌더링 + 헬퍼 단위 테스트."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE pages (
            doku_id TEXT PRIMARY KEY,
            title TEXT,
            confluence_page_id TEXT
        );
        CREATE TABLE attachments (
            page_doku_id TEXT,
            media_id TEXT PRIMARY KEY,
            confluence_attachment_id TEXT,
            confluence_page_id TEXT
        );
        INSERT INTO pages VALUES ('b:2019-s200d-1', '동탄 200k', 'p123');
        INSERT INTO pages VALUES ('b:checkpoint:a-bike-shop', 'A+ 자전거 정비샵', 'p999');
        INSERT INTO attachments VALUES
            ('b:2019-s200d-1', 'ride:files:s-200k-d-2019.gpx', 'att1', 'p123'),
            ('blog:2017', 'blog:files:img_2717.jpg', 'att2', 'p222');
        """
    )
    return conn


def test_render_text_escapes(db) -> None:
    out = run._struct_render_cell(db, "Text", "<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_date(db) -> None:
    out = run._struct_render_cell(db, "Date", "2019-03-01")
    assert '<time datetime="2019-03-01">2019-03-01</time>' == out


def test_render_url(db) -> None:
    out = run._struct_render_cell(db, "Url", "https://example.com/foo?a=1&b=2")
    assert 'href="https://example.com/foo?a=1&amp;b=2"' in out


def test_render_wiki_link_resolved(db) -> None:
    out = run._struct_render_cell(db, "Wiki", "[[:b:2019-s200d-1|동탄 200k]]")
    assert "<ri:page" in out
    assert 'ri:content-title="동탄 200k"' in out
    assert "동탄 200k" in out


def test_render_wiki_link_unresolved(db) -> None:
    out = run._struct_render_cell(db, "Wiki", "[[:b:nonexistent|missing]]")
    assert "dwc-unresolved-page" in out
    assert "missing" in out


def test_render_wiki_freetext(db) -> None:
    out = run._struct_render_cell(db, "Wiki", "그냥 텍스트 위키 마크업")
    assert "<p>" in out


def test_render_media_attachment_exact(db) -> None:
    out = run._struct_render_cell(db, "Media", ":ride:files:s-200k-d-2019.gpx")
    assert "<ri:attachment" in out
    assert 'ri:filename="s-200k-d-2019.gpx"' in out


def test_render_media_image_by_suffix(db) -> None:
    out = run._struct_render_cell(db, "Media", "blog:files:img_2717.jpg")
    assert "<ac:image>" in out
    assert "img_2717.jpg" in out


def test_render_media_unresolved(db) -> None:
    out = run._struct_render_cell(db, "Media", "b:files:does-not-exist.png")
    assert "dwc-unresolved-media" in out


def test_render_multi(db) -> None:
    out = run._struct_render_cell(db, "Text", ["a", "b", "c"])
    assert out == "a, b, c"


def test_resolve_page_basename_fallback(db) -> None:
    assert run._struct_resolve_page(db, "[[deep:ns:a-bike-shop]]") == ("p999", "A+ 자전거 정비샵")


def test_row_title_heuristic(db) -> None:
    cols = [(1, "code", "Text"), (9, "name", "Text"), (13, "qty", "Decimal")]
    out = run._struct_row_title({"1": "abc-1", "9": "Sample"}, cols, "brevet_event", 42)
    assert out == "brevet_event: abc-1"


def test_row_title_fallback(db) -> None:
    cols = [(1, "col1", "Wiki"), (2, "col2", "Media")]
    out = run._struct_row_title({}, cols, "brevet_event", 42)
    assert out == "brevet_event#42"


def test_binding_target_wiki(db) -> None:
    payload = {"23": "[[:b:2019-s200d-1|동탄 200k]]"}
    assert run._struct_binding_target(payload, 23, "wiki") == "b:2019-s200d-1"


def test_binding_target_doku_id(db) -> None:
    payload = {"2": "2019-s200d-1"}
    assert run._struct_binding_target(payload, 2, "doku_id") == "2019-s200d-1"


def test_binding_target_none_for_text_in_wiki_slot(db) -> None:
    payload = {"22": "long wiki markup, not a link"}
    assert run._struct_binding_target(payload, 22, "wiki") is None


def test_build_index_xml_with_db(db) -> None:
    cols = [(1, "code", "Text"), (2, "year", "Decimal")]
    out = run._struct_build_index_xml(
        "brevet_event", 80, "native", cols, 106, "999", "https://x.atlassian.net/wiki"
    )
    assert "brevet_event" in out
    assert "https://x.atlassian.net/wiki/database/999" in out
    assert "dwc-struct-brevet_event" in out
    assert "detailssummary" in out


def test_build_index_xml_with_space_key(db) -> None:
    cols = [(1, "x", "Text")]
    out = run._struct_build_index_xml(
        "foo", 1, "native", cols, 5, "42", "https://x.atlassian.net/wiki", space_key="mypace"
    )
    assert "https://x.atlassian.net/wiki/spaces/mypace/database/42" in out


def test_build_index_xml_no_db(db) -> None:
    cols = [(1, "x", "Text")]
    out = run._struct_build_index_xml("foo", 1, "properties", cols, 5, None, "")
    assert "Confluence Database" not in out
    assert "detailssummary" in out


# ---------- visual-audit 자동 신호 ----------


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


# ---------- wizard state ----------


def test_wizard_state_transitions() -> None:
    import sqlite3
    conn = sqlite3.connect(":memory:")
    run._wizard_init(conn)
    assert run._wizard_get(conn, "x") is None
    run._wizard_set(conn, "x", "running")
    row = run._wizard_get(conn, "x")
    assert row[0] == "running" and row[1] is not None and row[2] is None
    run._wizard_set(conn, "x", "done", summary="3 items")
    row = run._wizard_get(conn, "x")
    assert row[0] == "done" and row[2] is not None and row[3] == "3 items"
    run._wizard_set(conn, "x", "pending")
    row = run._wizard_get(conn, "x")
    assert row[0] == "pending" and row[1] is None and row[2] is None


def test_wizard_steps_declared() -> None:
    keys = [k for k, _t, _f, _o in run.WIZARD_STEPS]
    expected = [
        "prereq", "dev-up", "discover", "render", "plugin-audit",
        "convert", "upload", "rewrite-links", "history", "struct",
        "audit", "verify", "report", "report-publish",
    ]
    assert keys == expected


def test_dev_is_full_install_recognizes_full(tmp_path) -> None:
    (tmp_path / "doku.php").write_text("<?php // dokuwiki\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "inc").mkdir()
    assert run._dev_is_full_install(tmp_path) is True


def test_dev_is_full_install_recognizes_data_only(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    assert run._dev_is_full_install(tmp_path) is False


def test_dev_data_root_case_a(tmp_path) -> None:
    # full install: src/data/pages
    (tmp_path / "doku.php").write_text("")
    (tmp_path / "lib").mkdir()
    (tmp_path / "inc").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pages").mkdir()
    (tmp_path / "data" / "media").mkdir()
    assert run._dev_data_root(tmp_path) == tmp_path / "data"


def test_dev_data_root_case_b(tmp_path) -> None:
    # data root directly
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    assert run._dev_data_root(tmp_path) == tmp_path


def test_dev_detect_plugins_from_conf(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "plugins.local.php").write_text(
        "<?php\n$plugins['wrap'] = 1;\n$plugins['todo'] = 1;\n$plugins['info'] = 0;\n"
    )
    detected = run._dev_detect_plugins(tmp_path)
    assert "wrap" in detected
    assert "todo" in detected
    assert "info" not in detected


def test_dev_detect_plugins_from_struct_db(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "struct.sqlite3").write_bytes(b"\x00")
    detected = run._dev_detect_plugins(tmp_path)
    assert "struct" in detected


def test_dev_detect_plugins_from_macros(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "pages" / "x.txt").write_text("Foo\n~~DISCUSSION~~\nBar")
    detected = run._dev_detect_plugins(tmp_path)
    assert "discussion" in detected


def test_dev_install_plugins_already_present(tmp_path) -> None:
    (tmp_path / "lib" / "plugins" / "wrap").mkdir(parents=True)
    (tmp_path / "lib" / "plugins" / "wrap" / "syntax.php").write_text("")
    res = run._dev_install_plugins(tmp_path, ["wrap"])
    assert "wrap" in res["already"]
    assert not res["installed"]


def test_dev_install_plugins_bundled_skipped(tmp_path) -> None:
    (tmp_path / "lib" / "plugins").mkdir(parents=True)
    res = run._dev_install_plugins(tmp_path, ["info", "config", "acl"])
    assert set(res["bundled"]) == {"info", "config", "acl"}
    assert not res["installed"]


def test_wizard_report_body_minimal() -> None:
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE pages (doku_id TEXT, confluence_page_id TEXT);
        CREATE TABLE attachments (media_id TEXT, confluence_attachment_id TEXT);
        INSERT INTO pages VALUES ('a','1'),('b',NULL);
        INSERT INTO attachments VALUES ('m1','x'),('m2','y');
    """)
    body = run._wizard_build_report_body(conn)
    assert "<h1>" in body
    assert "1 / 2" in body  # pages_uploaded / pages_total
    assert "2 / 2" in body  # attachments
