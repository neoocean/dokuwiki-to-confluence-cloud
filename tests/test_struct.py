"""struct cell 렌더링 + helper 단위 테스트."""

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
