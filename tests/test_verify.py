"""
verify 서브커맨드(시각 검수 큐, docs/visual-audit.md)의 단위 테스트.

state.db 는 :memory: 로 만들고 최소 fixture 만 채워서 점수/큐/decision
import 흐름을 검증한다. Confluence/HTTP 호출은 없다.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


# ---------- 헬퍼 ----------

def _make_db() -> sqlite3.Connection:
    """run.db_init 와 같은 형태의 in-memory state.db."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    run.db_init(conn)
    run._ensure_verify_schema(conn)
    return conn


def _insert_page(
    conn: sqlite3.Connection,
    doku_id: str,
    content_hash: str = "h0",
    page_id: str = "p1",
    storage_path: str = "",
    status: str = "UPLOADED",
) -> None:
    conn.execute(
        "INSERT INTO pages (doku_id, src_path, namespace, parent_doku_id, "
        " title, raw_xhtml_path, storage_path, content_hash, "
        " confluence_page_id, status, discovered_at) "
        "VALUES (?, '', '', NULL, ?, '', ?, ?, ?, ?, datetime('now'))",
        (doku_id, doku_id, storage_path, content_hash, page_id, status),
    )
    conn.commit()


# ---------- 점수 계산 ----------

def test_macro_counts_basic() -> None:
    xml = (
        '<ac:structured-macro ac:name="info">x</ac:structured-macro>'
        '<ac:structured-macro ac:name="info">y</ac:structured-macro>'
        '<ac:structured-macro ac:name="warning">z</ac:structured-macro>'
        '<ac:image><ri:attachment ri:filename="a.png"/></ac:image>'
    )
    counts = run._verify_macro_counts(xml)
    assert counts["info"] == 2
    assert counts["warning"] == 1
    assert counts["image"] == 1


def test_score_oversized_body_high_priority() -> None:
    score, flags = run._verify_score_page(
        row=("doku", "title", "h", "p", ""),
        storage_xml="<p>tiny</p>",
        is_oversized_body=True,
        has_oversized_attachment=False,
        history_ratio=None,
        is_struct_snapshot=False,
        random_seed=0,
    )
    assert score >= 5
    assert "oversized-body" in flags


def test_score_history_low_ratio_bumps() -> None:
    s_low, f_low = run._verify_score_page(
        row=("doku", "t", "h", "p", ""),
        storage_xml="",
        is_oversized_body=False,
        has_oversized_attachment=False,
        history_ratio=0.30,
        is_struct_snapshot=False,
        random_seed=0,
    )
    s_full, _ = run._verify_score_page(
        row=("doku", "t", "h", "p", ""),
        storage_xml="",
        is_oversized_body=False,
        has_oversized_attachment=False,
        history_ratio=1.0,  # 100% 면 안 잡아야
        is_struct_snapshot=False,
        random_seed=0,
    )
    assert s_low > s_full
    assert any("history:" in f for f in f_low)


def test_score_callout_heavy_page() -> None:
    xml = "".join(
        f'<ac:structured-macro ac:name="info">x</ac:structured-macro>'
        for _ in range(5)
    )
    score, flags = run._verify_score_page(
        row=("doku", "t", "h", "p", ""),
        storage_xml=xml,
        is_oversized_body=False,
        has_oversized_attachment=False,
        history_ratio=None,
        is_struct_snapshot=False,
        random_seed=0,
    )
    assert score >= 5  # 5+ callout 이면 +5
    assert any(f.startswith("macro:") for f in flags)


# ---------- 큐 빌드 ----------

def test_queue_skips_non_uploaded_pages() -> None:
    conn = _make_db()
    _insert_page(conn, "a", status="UPLOADED")
    _insert_page(conn, "b", status="CONVERTED")
    queue = run._verify_build_queue(conn, sample=10, strategy="auto", resume=False)
    ids = [q["doku_id"] for q in queue]
    assert "a" in ids
    assert "b" not in ids


def test_queue_sorted_by_score_desc(tmp_path: Path) -> None:
    """oversized-body 페이지가 평범한 페이지보다 앞에 와야 한다."""
    conn = _make_db()
    sp_big = tmp_path / "big.xml"
    sp_big.write_text("<p>tiny</p>", encoding="utf-8")
    _insert_page(conn, "big", storage_path=str(sp_big))
    _insert_page(conn, "small")
    # big 페이지를 oversized 로 표시
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('large_body_fallback:big', '1')"
    )
    conn.commit()
    queue = run._verify_build_queue(conn, sample=10, strategy="auto", resume=False)
    assert queue[0]["doku_id"] == "big"
    assert "oversized-body" in queue[0]["flags"]


def test_queue_resume_excludes_ok_with_matching_hash() -> None:
    conn = _make_db()
    _insert_page(conn, "a", content_hash="HASH_A")
    _insert_page(conn, "b", content_hash="HASH_B")
    conn.execute(
        "INSERT INTO verify_decisions (doku_id, decision, source_hash, "
        " reviewed_at) VALUES ('a', 'OK', 'HASH_A', datetime('now'))"
    )
    conn.commit()

    queue = run._verify_build_queue(conn, sample=10, strategy="auto", resume=True)
    ids = [q["doku_id"] for q in queue]
    assert "a" not in ids   # 동일 hash 면 큐 제외
    assert "b" in ids


def test_queue_resume_keeps_stale_ok() -> None:
    """OK 인데 source_hash 가 다르면 (페이지가 다시 convert 되었다면)
    재검수가 필요하므로 큐에 다시 올라와야 한다."""
    conn = _make_db()
    _insert_page(conn, "a", content_hash="NEW_HASH")
    conn.execute(
        "INSERT INTO verify_decisions (doku_id, decision, source_hash, "
        " reviewed_at) VALUES ('a', 'OK', 'OLD_HASH', datetime('now'))"
    )
    conn.commit()
    queue = run._verify_build_queue(conn, sample=10, strategy="auto", resume=True)
    ids = [q["doku_id"] for q in queue]
    assert "a" in ids


def test_queue_critical_only_filters_low_score() -> None:
    conn = _make_db()
    _insert_page(conn, "plain")
    queue = run._verify_build_queue(
        conn, sample=10, strategy="critical-only", resume=False
    )
    # plain 페이지는 score ~0 → critical-only 에서 제외
    assert all(q["score"] >= 5 for q in queue)


def test_queue_sample_caps_size() -> None:
    conn = _make_db()
    for i in range(50):
        _insert_page(conn, f"page{i:02d}")
    queue = run._verify_build_queue(conn, sample=10, strategy="auto", resume=False)
    assert len(queue) == 10


# ---------- decision import ----------

class _Args:
    """argparse.Namespace 흉내."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_import_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON 다운로드 → import → status 의 한 사이클."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    run.db_init(conn)
    run._ensure_verify_schema(conn)
    _insert_page(conn, "a", content_hash="HA")
    _insert_page(conn, "b", content_hash="HB")
    conn.close()

    decisions = [
        {
            "doku_id": "a",
            "decision": "OK",
            "notes": "good",
            "source_hash": "HA",
            "reviewer": "me@example.com",
        },
        {
            "doku_id": "b",
            "decision": "NG",
            "notes": "wrap 누락",
            "source_hash": "HB",
        },
        # 무효 항목 — 무시되어야 한다
        {"doku_id": "", "decision": "OK"},
        {"doku_id": "x", "decision": "INVALID"},
    ]
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
    )

    rc = run.cmd_verify_import(_Args(db=str(db_path), path=str(decisions_path)))
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT doku_id, decision, source_hash FROM verify_decisions ORDER BY doku_id"
    ).fetchall()
    conn.close()
    assert rows == [("a", "OK", "HA"), ("b", "NG", "HB")]


def test_import_updates_existing_decision(tmp_path: Path) -> None:
    """같은 doku_id 로 다시 import 하면 갱신."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    run.db_init(conn)
    run._ensure_verify_schema(conn)
    _insert_page(conn, "a", content_hash="H1")
    conn.close()

    p1 = tmp_path / "d1.json"
    p1.write_text(json.dumps([{"doku_id": "a", "decision": "NG", "notes": "v1"}]),
                  encoding="utf-8")
    assert run.cmd_verify_import(_Args(db=str(db_path), path=str(p1))) == 0

    p2 = tmp_path / "d2.json"
    p2.write_text(json.dumps([{"doku_id": "a", "decision": "OK", "notes": "v2"}]),
                  encoding="utf-8")
    assert run.cmd_verify_import(_Args(db=str(db_path), path=str(p2))) == 0

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT decision, notes FROM verify_decisions WHERE doku_id='a'"
    ).fetchone()
    conn.close()
    assert row == ("OK", "v2")


def test_import_rejects_non_array(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    run.db_init(conn)
    run._ensure_verify_schema(conn)
    conn.close()

    p = tmp_path / "bad.json"
    p.write_text('{"doku_id": "a", "decision": "OK"}', encoding="utf-8")
    rc = run.cmd_verify_import(_Args(db=str(db_path), path=str(p)))
    assert rc == 1


# ---------- Phase 2: 시각 지표 / iframe / NG 분류 / 첨부 ----------

def test_compute_metrics_image_table(tmp_path: Path) -> None:
    """양측에 같은 카운트가 있으면 metric-ok 분류, 다르면 metric-bad."""
    conn = _make_db()
    raw_html = (
        '<div><h2>x</h2><img src="/_media/a.png">'
        '<img src="/_media/b.png"></div>'
    )
    storage_xml = (
        '<ac:image><ri:attachment ri:filename="a.png"/></ac:image>'
        '<ac:image><ri:attachment ri:filename="b.png"/></ac:image>'
        '<h2>x</h2>'
    )
    m = run._verify_compute_metrics("d", conn, raw_html, storage_xml, None)
    rows = {row[0]: row for row in m["rows"]}
    label, d, s, c, ok = rows["이미지"]
    assert d == 2 and s == 2 and ok


def test_compute_metrics_mismatch() -> None:
    """raw 에는 표 1개, storage 에는 0개 → metric-bad."""
    conn = _make_db()
    m = run._verify_compute_metrics(
        "d", conn,
        '<table><tr><td>x</td></tr></table>',
        '<p>x</p>',
        None,
    )
    rows = {row[0]: row for row in m["rows"]}
    _, d, s, _c, ok = rows["표"]
    assert d == 1 and s == 0 and not ok


def test_render_iframe_doc_escaping() -> None:
    """iframe srcdoc 안에 들어가는 HTML 은 CSS + body 로 포장."""
    out = run._verify_render_iframe_doc('<p>hello "world"</p>')
    assert "<style>" in out
    assert '<p>hello "world"</p>' in out
    assert "<body>" in out


def test_render_metrics_row_filters_zero_rows() -> None:
    """양측 0 인 항목은 미니 테이블에서 생략."""
    metrics = {
        "rows": [
            ("이미지", 0, 0, -1, True),       # 생략
            ("표", 2, 2, -1, True),           # 표시 (OK)
            ("h2", 3, 4, -1, False),          # 표시 (BAD)
        ],
    }
    out = run._verify_render_metrics_row(metrics)
    assert "이미지" not in out
    assert "표: 2" in out
    assert "h2: 3≠4" in out
    assert "metric-bad" in out
    assert "metric-ok" in out


def test_import_preserves_ng_tag(tmp_path: Path) -> None:
    """Phase 2 의 NG 분류 라디오 — JSON 의 ng_tag 가 state.db 에 들어가야."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    run.db_init(conn)
    run._ensure_verify_schema(conn)
    _insert_page(conn, "a", content_hash="H")
    conn.close()

    p = tmp_path / "d.json"
    p.write_text(
        json.dumps([{
            "doku_id": "a", "decision": "NG", "ng_tag": "table",
            "notes": "표 행 깨짐",
        }]),
        encoding="utf-8",
    )
    assert run.cmd_verify_import(_Args(db=str(db_path), path=str(p))) == 0

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT decision, ng_tag, notes FROM verify_decisions WHERE doku_id='a'"
    ).fetchone()
    conn.close()
    assert row == ("NG", "table", "표 행 깨짐")


def test_check_attachments_returns_zero_for_empty() -> None:
    """페이지에 첨부가 0건이면 (0, 0) 반환 (HTTP 호출 없음)."""
    conn = _make_db()
    _insert_page(conn, "lonely")
    # session=None, base="" — 호출 안 되어야
    ok, total = run._verify_check_attachments(conn, None, "", "lonely")
    assert (ok, total) == (0, 0)
