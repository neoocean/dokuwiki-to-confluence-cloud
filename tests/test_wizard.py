"""wizard 상태 전이 + WIZARD_STEPS + report body 단위 테스트."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


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
