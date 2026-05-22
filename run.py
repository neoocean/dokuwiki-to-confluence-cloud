#!/usr/bin/env python3
"""
DokuWiki -> Confluence Cloud migration.

DokuWiki 가 렌더링한 최종 XHTML 을 받아 Confluence storage format 으로
변환하고, 네임스페이스 트리를 그대로 페이지 계층에 매핑한다. 자세한
설계는 docs/scenarios.md 의 S1~S10 을 참고.

## 메인 파이프라인 서브커맨드 (라이브 마이그레이션)

  discover       페이지 트리 발견 (S1)
  render         DokuWiki XHTML 캐시 (S2)
  convert        XHTML -> Confluence storage format (S3)
  upload         페이지/첨부 생성·갱신 (S4~S6)
  rewrite-links  2-pass 내부 링크 치환 (S7)
  status         상태 요약

## 별도 트랙

  history-*      과거 리비전 이전 (시간순 PUT replay)
  struct-*       struct plugin 데이터 → Confluence Database
  rewrite-oversized*  100MB+ 첨부 / 본문 한도 거부 폴백
  audit          Confluence 측 본문 ↔ DokuWiki 비교
  verify build/import/status   사용자 시각 검수 큐 + 자동 신호
  report / preview / lint      corpus 통계 / 페이지 미리보기 / XML lint

## 도구 / 운영

  dev up/down/install-plugins  로컬 DokuWiki 컨테이너 (full / data-only 자동)
  plugin-scan                  데이터에서 사용 매크로 → 미설치 플러그인 식별
  decrypt                      encryptedpasswords cipher 복호화 (AES-256-CBC)
  link-check                   Confluence 측 링크 정합성 검사
  wizard                       대화형 14 단계 (중단/재개 안전)
  report-publish               state.db 통계 → Confluence 페이지 자동 발행

## 코드 섹션 인덱스 (grep anchor — `grep -n '# §' run.py`)

코드가 진화해도 line 번호가 어긋날 수 있어 *anchor 키워드만* 명시. 위치는
`grep` 로 찾을 것 — 단일 source of truth.

  § 유틸                                — 유틸 (now_iso/hashing/log)
  § DB                                  — sqlite 연결 + meta + schema DDL
  § S1 Discover                         — `pages/*.txt` 트리 인벤토리
  § S2 Render                           — DokuWiki ?do=export_xhtmlbody 캐시
  § S3 Convert (변환기 본체)            — XHTML → Confluence storage XML
       `_convert_*_callouts/footnotes/todos/smileys/visual_residue`
       `_convert_monthcal_fallback / _convert_youtube_fallback`
       `_convert_google_calendar_iframe / _convert_encrypted_passwords`
       `_convert_html_to_storage` (메인 entry, 변환 파이프라인 표 포함)
  § S4-6 Upload                         — 페이지 + 첨부 PUT/POST
  § S7 Rewrite-links                    — placeholder → ri:page 2-pass
  § history-* track                     — attic 리비전 + meta/.changes
  § struct-* track                      — struct.sqlite3 → Database 쉘 + properties
  § rewrite-oversized-pages             — 본문 한도 거부 페이지 skeleton 폴백
  § rewrite-oversized                   — 100MB+ 첨부 note 박스 폴백
  § audit                               — Confluence 본문 받아 비교
  § report / preview / lint             — 통계 / 미리보기 / XML lint
  § dev (컨테이너 + plugin-scan)        — 로컬 DokuWiki + plugin 자동설치
  § verify (시각 검수 + Phase 4)        — DOM/screenshot 비교 + AI vision
  § decrypt / link-check                — encryptedpasswords + 링크 정합성
  § compare-publish                     — 양측 스크린샷 + 비교 갤러리 발행
  § audit-3way                          — source ↔ rendered ↔ confluence 3-측 invariant
                                          (docs/3way-audit.md; 사례 A~D 자동 검출)
  § wizard / report-publish             — 대화형 orchestration + 결과 보고
  § argparse                            — `_build_*_subcommands` 9 helper +
                                          `build_parser` orchestrator (메인 진입점)

## Import 정책

- *top-level*: 표준 라이브러리 + 모든 명령이 의존하는 외부 (`requests`,
  `beautifulsoup4`) — 미설치 시 즉시 에러.
- *함수 내부 lazy import*: **옵션 의존성만** — `playwright`, `PIL`,
  `imagehash`, `anthropic`, `Crypto/pycryptodome`, `pytesseract`,
  `requests_toolbelt`. 특정 명령 (verify --with-vision, compare-publish 등)
  에서만 필요. 미설치여도 다른 명령 영향 없음.
- 새 helper 작성 시 위 정책에 맞춰 결정.

## 예외 처리 정책

- `try: ... except Exception` 는 *비핵심 경로의 silent 실패만 흡수* 의도:
  (1) 옵션 의존성 미설치, (2) audit / verify 의 분석 신호 fallback,
  (3) Playwright 캡쳐 timeout. 핵심 데이터 (페이지/첨부 본문 PUT) 는
  `requests.RequestException` 같은 구체 type 으로 처리.
- `# noqa: BLE001` 표기는 위 의도 명시 (linter 의 broad-except 경고 suppress).
- 새 광범위 except 추가 시 위 카테고리 어디에 속하는지 1줄 주석 권장.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html as _h
import json as _json
import os
import re
import re as _re  # 함수 내부 별칭 호환용 (기존 코드가 _re.X 로 호출)
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1

DEFAULT_DB_PATH = "state.db"
RAW_DIR = Path("raw")
STORAGE_DIR = Path("storage")
LOGS_DIR = Path("logs")


# § 유틸

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


# § DB

def db_connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


HISTORY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS revisions (
    doku_id TEXT NOT NULL,
    rev_ts INTEGER NOT NULL,
    type TEXT,
    user TEXT,
    ip TEXT,
    comment TEXT,
    extra TEXT,
    attic_path TEXT,
    raw_xhtml_path TEXT,
    storage_path TEXT,
    content_hash TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    last_checked_at TEXT,
    PRIMARY KEY (doku_id, rev_ts)
);
CREATE INDEX IF NOT EXISTS revisions_doku_idx ON revisions(doku_id);
CREATE INDEX IF NOT EXISTS revisions_status_idx ON revisions(status);

CREATE TABLE IF NOT EXISTS history_meta (
    doku_id TEXT PRIMARY KEY,
    total_revs INTEGER,
    first_ts INTEGER,
    last_ts INTEGER,
    replay_started_at TEXT,
    replay_completed_at TEXT,
    last_replayed_rev_ts INTEGER,
    confluence_property_id TEXT,
    history_child_page_id TEXT
);

CREATE TABLE IF NOT EXISTS media_revisions (
    media_id TEXT NOT NULL,
    rev_ts INTEGER NOT NULL,
    src_path TEXT,
    size INTEGER,
    sha256 TEXT,
    confluence_attachment_id TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    uploaded_at TEXT,
    PRIMARY KEY (media_id, rev_ts)
);
"""


STRUCT_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS struct_schemas (
    sid INTEGER PRIMARY KEY,
    tbl TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL DEFAULT 0,
    column_count INTEGER NOT NULL DEFAULT 0,
    chosen_mode TEXT,
    confluence_db_id TEXT,
    properties_index_page_id TEXT,
    snapshot_page_id TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS struct_columns (
    sid INTEGER NOT NULL,
    colref INTEGER NOT NULL,
    sort INTEGER NOT NULL,
    name TEXT,
    dokuwiki_class TEXT NOT NULL,
    config_json TEXT,
    confluence_column_id TEXT,
    PRIMARY KEY (sid, colref)
);

CREATE TABLE IF NOT EXISTS struct_rows (
    sid INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    bound_doku_id TEXT,
    payload_json TEXT NOT NULL,
    confluence_row_id TEXT,
    confluence_page_id TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    PRIMARY KEY (sid, pid)
);

CREATE TABLE IF NOT EXISTS struct_references (
    src_sid INTEGER NOT NULL,
    src_pid INTEGER NOT NULL,
    src_colref INTEGER NOT NULL,
    target_kind TEXT NOT NULL,
    target_locator TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (src_sid, src_pid, src_colref)
);
"""


# 메인 파이프라인의 핵심 테이블 — pages / attachments / links / meta.
# 별도 트랙 DDL (HISTORY_SCHEMA_DDL / STRUCT_SCHEMA_DDL — 위쪽 정의,
# VERIFY_DECISIONS_DDL / WIZARD_DDL — 각자의 섹션에서 정의) 와 함께
# db_init 에서 한 번에 적용. 모두 CREATE TABLE IF NOT EXISTS — 멱등.
MAIN_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS pages (
    doku_id TEXT PRIMARY KEY,
    src_path TEXT NOT NULL,
    namespace TEXT NOT NULL,
    parent_doku_id TEXT,
    is_namespace_index INTEGER NOT NULL DEFAULT 0,
    title TEXT,
    raw_xhtml_path TEXT,
    storage_path TEXT,
    content_hash TEXT,
    confluence_page_id TEXT,
    confluence_version INTEGER,
    doku_last_change TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    discovered_at TEXT NOT NULL,
    rendered_at TEXT,
    converted_at TEXT,
    uploaded_at TEXT,
    last_checked_at TEXT
);

CREATE INDEX IF NOT EXISTS pages_status_idx ON pages(status);
CREATE INDEX IF NOT EXISTS pages_namespace_idx ON pages(namespace);

CREATE TABLE IF NOT EXISTS attachments (
    page_doku_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    src_path TEXT,
    size INTEGER,
    sha256 TEXT,
    confluence_attachment_id TEXT,
    confluence_page_id TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    uploaded_at TEXT,
    PRIMARY KEY (page_doku_id, media_id)
);

CREATE INDEX IF NOT EXISTS attachments_status_idx ON attachments(status);

CREATE TABLE IF NOT EXISTS links (
    src_doku_id TEXT NOT NULL,
    placeholder TEXT NOT NULL,
    target_doku_id TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (src_doku_id, placeholder)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def db_init(conn: sqlite3.Connection) -> None:
    """state.db 의 모든 schema 생성 (멱등). 메인 + history + struct.

    verify_decisions / wizard_state 는 각각 _ensure_verify_schema /
    _wizard_init 가 lazy 적용 — 그 명령을 처음 호출할 때만.
    """
    conn.executescript(MAIN_SCHEMA_DDL)
    conn.executescript(HISTORY_SCHEMA_DDL)
    conn.executescript(STRUCT_SCHEMA_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def db_set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
    conn.commit()


def db_get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# § S1: Discover

def path_to_doku_id(pages_root: Path, txt_path: Path) -> tuple[str, str, bool]:
    """
    pages_root 기준 .txt 경로를 (doku_id, namespace, is_namespace_index) 로 변환.

    예)
      pages/start.txt              -> ("start",           "",        True)
      pages/wiki/syntax.txt        -> ("wiki:syntax",     "wiki",    False)
      pages/wiki/start.txt         -> ("wiki:start",      "wiki",    True)
      pages/wiki/foo/bar.txt       -> ("wiki:foo:bar",    "wiki:foo", False)
    """
    rel = txt_path.relative_to(pages_root).with_suffix("")
    parts = rel.parts
    namespace = ":".join(parts[:-1])
    doku_id = ":".join(parts)
    is_index = parts[-1] == "start"
    return doku_id, namespace, is_index


def parent_doku_id(namespace: str, is_index: bool) -> str | None:
    """
    부모 페이지의 doku_id. 동일 네임스페이스의 start 페이지가 있다고 가정하고
    "<namespace>:start" 를 부모로 본다. 인덱스 페이지 자체의 부모는 한 단계 위.
    """
    if is_index:
        if not namespace:
            return None
        # 인덱스 자체의 부모는 상위 네임스페이스의 start
        parent_ns = ":".join(namespace.split(":")[:-1])
        return f"{parent_ns}:start" if parent_ns else "start"
    if not namespace:
        return "start"  # 루트의 일반 페이지는 root start 의 자식
    return f"{namespace}:start"


def read_meta_title(meta_root: Path, doku_id: str) -> tuple[str | None, str | None]:
    """
    meta/<ns>/<page>.meta 에서 title 과 last_change 시각(ISO) 추정.
    DokuWiki meta 는 PHP 직렬화 형식이라 정밀 파싱 대신 정규식 가벼운 추출만 한다.
    찾지 못하면 (None, None).
    """
    rel = Path(*doku_id.split(":"))
    meta_path = meta_root / rel.with_suffix(".meta")
    if not meta_path.exists():
        return None, None
    try:
        data = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None

    title = None
    m = re.search(r's:5:"title";s:\d+:"([^"]*)"', data)
    if m:
        title = m.group(1)

    last_change = None
    m2 = re.search(r's:4:"date";a:\d+:{s:7:"created";i:(\d+)', data)
    if not m2:
        m2 = re.search(r's:8:"modified";i:(\d+)', data)
    if m2:
        try:
            ts = int(m2.group(1))
            last_change = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OSError):
            pass

    return title, last_change


def title_from_doku_id(doku_id: str) -> str:
    last = doku_id.split(":")[-1]
    return last.replace("_", " ").strip() or doku_id


def cmd_discover(args: argparse.Namespace) -> int:
    """S1: DokuWiki 데이터 디렉터리를 스캔해 페이지/첨부 트리를 state.db 에 등록.

    state.db 갱신: `pages` (status='DISCOVERED'), `attachments` (status=
    'DISCOVERED'), `meta` (dokuwiki_src). 멱등 — 이미 발견된 항목은 SKIP,
    삭제된 페이지는 *제거 안 함* (history/audit 추적용 보존).
    """
    src = Path(args.src).expanduser().resolve()
    pages_root = src / "pages"
    meta_root = src / "meta"

    if not pages_root.is_dir():
        log(f"pages/ 디렉터리가 없습니다: {pages_root}")
        return 2

    conn = db_connect(args.db)
    db_init(conn)
    db_set_meta(conn, "dokuwiki_src", str(src))

    found = 0
    inserted = 0
    refreshed = 0

    for txt in sorted(pages_root.rglob("*.txt")):
        if not txt.is_file():
            continue
        # DokuWiki 가 빈 .txt 를 보존하기도 함. 아예 빈 파일은 건너뛴다.
        if txt.stat().st_size == 0:
            continue

        doku_id, namespace, is_index = path_to_doku_id(pages_root, txt)
        parent = parent_doku_id(namespace, is_index)
        title_hint, last_change = read_meta_title(meta_root, doku_id)
        title = title_hint or title_from_doku_id(doku_id)

        found += 1

        row = conn.execute(
            "SELECT status, src_path FROM pages WHERE doku_id=?", (doku_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO pages(
                    doku_id, src_path, namespace, parent_doku_id, is_namespace_index,
                    title, doku_last_change, status, discovered_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?, ?)
                """,
                (
                    doku_id,
                    str(txt),
                    namespace,
                    parent,
                    1 if is_index else 0,
                    title,
                    last_change,
                    now_iso(),
                    now_iso(),
                ),
            )
            inserted += 1
        else:
            # 경로/제목/메타가 바뀌었을 수 있으므로 갱신만. status 는 보존.
            conn.execute(
                """
                UPDATE pages
                   SET src_path=?, namespace=?, parent_doku_id=?, is_namespace_index=?,
                       title=COALESCE(?, title), doku_last_change=?, last_checked_at=?
                 WHERE doku_id=?
                """,
                (
                    str(txt),
                    namespace,
                    parent,
                    1 if is_index else 0,
                    title_hint,
                    last_change,
                    now_iso(),
                    doku_id,
                ),
            )
            refreshed += 1

    conn.commit()

    # 고아 부모 검증: parent_doku_id 가 실제로는 존재하지 않는 경우 NULL 로.
    conn.execute(
        """
        UPDATE pages
           SET parent_doku_id = NULL
         WHERE parent_doku_id IS NOT NULL
           AND parent_doku_id NOT IN (SELECT doku_id FROM pages)
        """
    )
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    log(f"discover 완료: 발견 {found}, 신규 {inserted}, 갱신 {refreshed}, DB 총 {total}")
    conn.close()
    return 0


# § S2: Render

def cmd_render(args: argparse.Namespace) -> int:
    """S2: DISCOVERED 페이지마다 DokuWiki `?do=export_xhtmlbody` 받아 `raw/`에 저장.

    state.db 갱신: `pages.raw_xhtml_path / rendered_at`, status='RENDERED'.
    `--force` 면 RENDERED 도 재 fetch. 실패 시 status='FAILED' + last_error.
    멱등 — `--force` 없으면 RENDERED 페이지는 SKIP.
    """
    try:
        import requests
    except ImportError:
        log("requests 가 필요합니다: pip install requests")
        return 2

    base_url = args.base_url.rstrip("/")
    if not base_url:
        log("--base-url 또는 DOKUWIKI_BASE_URL 이 필요합니다.")
        return 2

    RAW_DIR.mkdir(exist_ok=True)
    conn = db_connect(args.db)
    db_init(conn)
    db_set_meta(conn, "dokuwiki_base_url", base_url)

    session = requests.Session()
    if args.user and args.password:
        # DokuWiki 의 form 로그인. ?do=login 의 POST 로 세션 쿠키 획득.
        # _request_with_retry 우회 의도: 로그인 실패는 retry 무의미 (자격증명 오타),
        # 즉시 종료가 사용자에게 더 빠른 피드백.
        login_resp = session.post(
            f"{base_url}/doku.php",
            data={"do": "login", "u": args.user, "p": args.password},
            timeout=30,
            allow_redirects=True,
        )
        if login_resp.status_code >= 400:
            log(f"DokuWiki 로그인 실패: HTTP {login_resp.status_code}")
            return 1

    where = "status IN ('DISCOVERED', 'FAILED')" if not args.force else "1=1"
    if args.only:
        where = "doku_id = ?"
        params: tuple = (args.only,)
    else:
        params = ()

    rows = conn.execute(
        f"SELECT doku_id FROM pages WHERE {where} ORDER BY doku_id", params
    ).fetchall()

    log(f"render 대상: {len(rows)} 페이지")
    ok = failed = empty = 0

    for (doku_id,) in rows:
        url = f"{base_url}/doku.php"
        try:
            # _request_with_retry 우회 의도: 로컬 DokuWiki 가 일관 빠르고
            # 페이지마다 1회 호출. 실패 시 페이지 1건만 status=FAILED 로 두고
            # 다음 페이지 진행 — 사용자 입장에선 retry 보다 빠른 batch 가 우선.
            resp = session.get(
                url,
                params={"id": doku_id, "do": "export_xhtmlbody"},
                timeout=60,
            )
        except requests.RequestException as e:
            log(f"  [FAIL] {doku_id}: {e}")
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? WHERE doku_id=?",
                (str(e), now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue

        if resp.status_code == 404:
            log(f"  [EMPTY] {doku_id}: 404 (placeholder 추정)")
            conn.execute(
                "UPDATE pages SET status='SKIPPED', last_error='404 on export', last_checked_at=? WHERE doku_id=?",
                (now_iso(), doku_id),
            )
            conn.commit()
            empty += 1
            continue

        if resp.status_code >= 400:
            log(f"  [FAIL] {doku_id}: HTTP {resp.status_code}")
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? WHERE doku_id=?",
                (f"HTTP {resp.status_code}", now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue

        body = resp.text
        if not body.strip():
            log(f"  [EMPTY] {doku_id}: 본문 비어 있음")
            conn.execute(
                "UPDATE pages SET status='SKIPPED', last_error='empty body', last_checked_at=? WHERE doku_id=?",
                (now_iso(), doku_id),
            )
            conn.commit()
            empty += 1
            continue

        # 캐시 경로: raw/<doku_id 의 :를 /로>.html
        rel = Path(*doku_id.split(":")).with_suffix(".html")
        cache_path = RAW_DIR / rel
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(body, encoding="utf-8")

        conn.execute(
            """
            UPDATE pages
               SET raw_xhtml_path=?, status='RENDERED', last_error=NULL,
                   rendered_at=?, last_checked_at=?
             WHERE doku_id=?
            """,
            (str(cache_path), now_iso(), now_iso(), doku_id),
        )
        conn.commit()
        ok += 1

        if args.delay:
            time.sleep(args.delay)

    log(f"render 완료: ok={ok} empty={empty} failed={failed}")
    conn.close()
    return 0


# § S3: Convert

DOKU_LINK_SCHEME = "dwc-link"
CODE_BODY_SENTINEL_PREFIX = "__DWC_CODE_BODY_"


def _categorize_href(href: str) -> dict:
    """
    DokuWiki XHTML 의 href/src 를 분류한다.

    반환 형식:
      {'kind': 'page',     'id': 'wiki:syntax', 'anchor': str | None}
      {'kind': 'media',    'id': 'wiki:foo.png'}
      {'kind': 'action'}                          # ?do=edit 류
      {'kind': 'anchor',   'href': '#headline'}
      {'kind': 'external', 'href': '...'}
    """
    from urllib.parse import parse_qs, urlparse

    if not href:
        return {"kind": "external", "href": ""}
    if href.startswith("#"):
        return {"kind": "anchor", "href": href}

    parsed = urlparse(href)
    q = parse_qs(parsed.query)

    if "media" in q and q["media"]:
        media_val = q["media"][0]
        # DokuWiki 는 외부 URL 도 fetch.php 의 media= 에 URL-인코딩해 넘긴다
        # (썸네일/리사이즈용 프록시). 이건 첨부가 아니라 외부 이미지로 둔다.
        if media_val.startswith(("http://", "https://")):
            return {"kind": "external", "href": media_val}
        return {"kind": "media", "id": media_val}
    if "id" in q and q["id"]:
        return {"kind": "page", "id": q["id"][0], "anchor": parsed.fragment or None}
    if "do" in q:
        return {"kind": "action"}

    from urllib.parse import unquote
    path = unquote(parsed.path or "")  # 한국어/유니코드 미디어/페이지 명 URL-decode
    if path.startswith("/_media/"):
        return {"kind": "media", "id": path[len("/_media/"):].replace("/", ":")}
    if path.startswith("/_detail/"):
        return {"kind": "media", "id": path[len("/_detail/"):].replace("/", ":")}
    if path.startswith("/lib/exe/fetch.php"):
        # query 없는 fetch.php 는 흔치 않지만 방어적으로
        return {"kind": "external", "href": href}
    if path.startswith("/doku.php/"):
        page_id = path[len("/doku.php/"):].strip("/").replace("/", ":")
        return {"kind": "page", "id": page_id, "anchor": parsed.fragment or None}

    return {"kind": "external", "href": href}


def _media_filename(media_id: str) -> str:
    return media_id.rsplit(":", 1)[-1]


def _href_to_doku_id_via_path(href: str) -> str | None:
    """
    URL rewrite (`useslash`) 가 켜진 환경에서 dokuwiki 는 내부 페이지 링크를
    `?id=` 가 없는 path 형태로 출력한다: `/wiki/syntax`, `/playground/playground`.
    이 helper 는 그 path 부분을 doku_id 로 환산한다. 변환 실패 시 None.
    한국어 등 비-ASCII 페이지명은 URL-인코딩되어 들어오므로 unquote 한다.
    """
    from urllib.parse import unquote, urlparse

    parsed = urlparse(href)
    if parsed.scheme:
        return None
    path = unquote(parsed.path or "").strip("/")
    if not path:
        return None
    # `/doku.php/foo:bar` 등 명시적 prefix 는 별도 분류기가 처리한다
    if path.startswith(("doku.php", "_media/", "_detail/", "lib/exe/")):
        return None
    return path.replace("/", ":")


def _resolve_media_path(src_root: Path, media_id: str) -> Path | None:
    rel = Path(*media_id.split(":"))
    candidate = src_root / "media" / rel
    return candidate if candidate.is_file() else None


def _todo_checked_and_text(todo) -> tuple[bool, str]:
    """todo span 의 (checked, inner-text) 추출."""
    checkbox = todo.find("input", class_="todocheckbox")
    checked = bool(checkbox and checkbox.has_attr("checked"))
    inner = todo.find("span", class_="todoinnertext")
    text = (inner.get_text() if inner else todo.get_text()).strip()
    return checked, text


def _build_ac_task(soup, task_id: int, checked: bool, text: str) -> "Tag":
    """Confluence <ac:task> 엘리먼트 빌드."""
    task = soup.new_tag("ac:task")
    tid = soup.new_tag("ac:task-id")
    tid.string = str(task_id)
    task.append(tid)
    ts = soup.new_tag("ac:task-status")
    ts.string = "complete" if checked else "incomplete"
    task.append(ts)
    body = soup.new_tag("ac:task-body")
    body.string = text
    task.append(body)
    return task


SMILEY_EMOJI_MAP = {
    # DokuWiki 코어가 제공하는 smiley 이미지를 unicode emoji 로.
    # `<img class="icon smiley" alt=":-)">` 같은 픽셀 이미지가 Confluence 에서
    # 깨진 링크가 되지 않도록.
    ":-)": "🙂", ":)": "🙂", "=)": "🙂",
    ":-(": "🙁", ":(": "🙁",
    ":-D": "😀", ":D": "😀",
    ";-)": "😉", ";)": "😉",
    ":-P": "😛", ":P": "😛",
    ":-O": "😮", ":O": "😮",
    "8-)": "😎", "8-O": "😲",
    ":-/": "🤔",
    ":-\\": "🤔",
    ":-?": "🤔",
    ":-X": "🤐",
    ":-|": "😐",
    "^_^": "😊",
    "m(": "🤦",
    ":?:": "❓",
    ":!:": "❗",
    "LOL": "😆",
    "FIXME": "⚠️ FIXME",
    "DELETEME": "⚠️ DELETEME",
}


def _convert_smileys(soup) -> None:
    """DokuWiki 코어 smiley 이미지 → unicode emoji 텍스트."""
    for img in list(soup.find_all("img")):
        classes = img.get("class") or []
        if "smiley" not in classes:
            continue
        alt = img.get("alt", "") or ""
        emoji = SMILEY_EMOJI_MAP.get(alt, alt or "")
        img.replace_with(emoji)


WRAP_SEMANTIC_MAP = {
    "wrap_info":      "info",
    "wrap_help":      "info",
    "wrap_tip":       "tip",
    "wrap_important": "note",
    "wrap_note":      "note",
    "wrap_alert":     "warning",
    "wrap_warning":   "warning",
    "wrap_danger":    "warning",
}


def _convert_wrap_callouts(soup) -> None:
    """
    dokuwiki wrap 플러그인의 callout/panel/인라인 강조를 Confluence storage
    매크로(또는 일반 인라인 태그) 로 매핑한다.

    block 컨테이너 (<div class="wrap_..."> ): 의미 클래스가 있으면
        <ac:structured-macro ac:name=info|tip|note|warning> + ac:rich-text-body 로,
        없고 wrap_box/wrap_round 면 panel 매크로로.
    인라인 (<em>/<span class="wrap_em|wrap_hi">):
        wrap_em -> <strong>, wrap_hi -> <span style="background-color: #fff59d">.
    """
    # block-level callouts
    for div in list(soup.find_all("div")):
        classes = div.get("class") or []
        macro_name = None
        for c in classes:
            if c in WRAP_SEMANTIC_MAP:
                macro_name = WRAP_SEMANTIC_MAP[c]
                break
        if not macro_name:
            is_wrap_box = any(c in ("wrap_box", "wrap_round") for c in classes)
            is_wrap = "plugin_wrap" in classes or any(c.startswith("wrap_") for c in classes)
            if is_wrap_box and is_wrap:
                macro_name = "panel"
        if not macro_name:
            continue

        macro = soup.new_tag("ac:structured-macro")
        macro["ac:name"] = macro_name
        body = soup.new_tag("ac:rich-text-body")
        for child in list(div.children):
            body.append(child.extract())
        macro.append(body)
        div.replace_with(macro)

    # inline emphasis
    for tag in list(soup.find_all(["em", "span"])):
        classes = tag.get("class") or []
        if "wrap_em" in classes:
            strong = soup.new_tag("strong")
            for c in list(tag.children):
                strong.append(c.extract())
            tag.replace_with(strong)
        elif "wrap_hi" in classes:
            span = soup.new_tag("span")
            span["style"] = "background-color: #fff59d;"
            for c in list(tag.children):
                span.append(c.extract())
            tag.replace_with(span)


WRAP_ALIGN_STYLE_MAP = {
    "wrap_left":   "text-align: left;",
    "wrap_right":  "text-align: right;",
    "wrap_center": "text-align: center;",
    # 정렬 외 layout 클래스는 의도된 의미가 약해 매핑 안 함
}


TABLE_ALIGN_CLASS_MAP = {
    "leftalign":   "text-align: left;",
    "centeralign": "text-align: center;",
    "rightalign":  "text-align: right;",
}


# § 변환기: 플러그인 fallback / 외부 임베드
#
# 모두 _convert_html_to_storage 안에서 _convert_wrap_callouts 직후 호출:
#   1. _convert_monthcal_fallback        — monthcal 플러그인 미설치 fallback
#   2. _convert_youtube_fallback         — youtube 매크로 fallback
#   3. _convert_encrypted_passwords      — <decrypt>...</decrypt> → expand+code
#   4. _convert_google_calendar_iframe   — Google Calendar iframe 보존

_MONTHCAL_HREF_RE = re.compile(
    r'^/_media/monthcal/(?:align_(\w+)_)?namespace/'
    r'(?P<ns>[^_]*)_?month_(?P<m>\d+)_year_(?P<y>\d+)'
    r'(?:_week_start_on_(?P<w>\w+))?'
)


def _convert_monthcal_fallback(soup) -> None:
    """DokuWiki monthcal 플러그인 미설치 시 fallback 으로 출력되는 깨진 media
    링크 (`/_media/monthcal/align_X_namespace/NS_month_M_year_Y_...`) 를 감지해
    정적 캘린더 표로 교체.

    캘린더 셀의 각 날짜는 자동으로 `ns:Y:M:DD` 형식의 페이지 링크 (dokuwiki
    의 일지 페이지 컨벤션) — 본 도구의 `dwc-link:` placeholder 패턴 활용
    → rewrite-links 단계에서 실제 Confluence 페이지 ID 로 치환.
    """
    import calendar as _cal
    for a in list(soup.find_all("a", href=True)):
        m = _MONTHCAL_HREF_RE.match(str(a.get("href", "")))
        if not m:
            continue
        try:
            year = int(m.group("y"))
            month = int(m.group("m"))
        except (ValueError, TypeError):
            continue
        ns = (m.group("ns") or "").strip(":")
        wstart_str = m.group("w") or "monday"
        firstweekday = 6 if wstart_str == "sunday" else 0

        cal = _cal.Calendar(firstweekday=firstweekday)
        # header: 요일 (firstweekday 부터)
        weekday_names_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        ordered_weekdays = [
            weekday_names_en[(firstweekday + i) % 7] for i in range(7)
        ]
        header_cells = "".join(f"<th>{w}</th>" for w in ordered_weekdays)
        body_rows = []
        for week in cal.monthdatescalendar(year, month):
            cells = []
            for d in week:
                if d.month != month:
                    cells.append('<td style="color:#bbb;">·</td>')
                    continue
                # 일지 페이지 링크 (dwc-link placeholder — rewrite-links 가 해소)
                day = d.day
                if ns:
                    page_id = f"{ns}:{year:04d}:{month:02d}:{day:02d}"
                    cells.append(
                        f'<td><a href="dwc-link:{page_id}" '
                        f'data-wiki-id="{page_id}">{day}</a></td>'
                    )
                else:
                    cells.append(f"<td>{day}</td>")
            body_rows.append("<tr>" + "".join(cells) + "</tr>")

        title = f"📅 {year}-{month:02d}"
        if ns:
            title += f" — namespace <code>:{ns}:</code>"
        html = (
            '<div class="dwc-monthcal">'
            f'<p><strong>{title}</strong></p>'
            f'<table><thead><tr>{header_cells}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table>'
            '</div>'
        )
        from bs4 import BeautifulSoup as _BS
        new_node = _BS(html, "html.parser")
        # 부모 <p> 안에 단일 <a> 인 경우 (이 fallback 의 흔한 모양) 부모 째 교체
        parent = a.parent
        if parent and parent.name == "p" and len(list(parent.stripped_strings)) <= 1 \
                and len(parent.find_all("a")) == 1:
            parent.replace_with(new_node)
        else:
            a.replace_with(new_node)


_DECRYPT_RAW_RE = re.compile(
    r"&lt;(decrypt|encrypt)&gt;(.*?)&lt;/\1&gt;",
    re.S,
)
_DECRYPT_INNER_NOISE_RE = re.compile(
    r"</?(em|u|strong|i|b|sub|sup|tt|del|ins|span|small|big|s|mark)"
    r"(?:\s+[^>]*)?>",
    re.I,
)


def _preprocess_encrypted_passwords(raw_html: str) -> str:
    """raw HTML 의 escape 된 `<decrypt>cipher</decrypt>` / `<encrypt>...` 블록을
    *bs4 파싱 전* 에 직접 Confluence expand + code 매크로로 치환.

    이 단계가 필요한 이유 — cipher 가 multi-line base64 일 때 그 안에 우연히
    DokuWiki 마크업 시퀀스 (`__`, `//` 등) 가 형성되어 DokuWiki 가 unmatched
    `<em>` / `<u>` 등 inline tag 를 삽입함. bs4 가 그 inline tag 로 텍스트 노드를
    split 하면 기존 텍스트-노드 walker `_convert_encrypted_passwords` 가 패턴
    매치에 실패 → cipher 가 *escape 텍스트로 그대로 본문에 노출* 됨 (보안/가독성
    문제).

    pre-process 는 escape 텍스트 단계에서 매치하므로 cipher 안 inline tag 영향을
    받지 않음. 매치된 cipher 는:
      - inline 마크업 잔재 strip (base64 에 진짜로 들어있지 않음)
      - whitespace 보존하지만 outer trim
      - <ac:structured-macro name="expand"> 안 <ac:structured-macro name="code">
        + CDATA 로 보존 (cipher 원형 그대로 — decrypt 시 사용 가능)

    추가로 매크로 뒤에 비-공백 텍스트가 줄바꿈 없이 이어지는 경우 `\n` 삽입 —
    Confluence 가 block 매크로 다음 문단을 별개 paragraph 로 인식하도록.
    """
    def repl(m: re.Match) -> str:
        tag = m.group(1)
        inner = m.group(2)
        cleaned = _DECRYPT_INNER_NOISE_RE.sub("", inner)
        cleaned = cleaned.strip()
        # cipher 안에 있을 수 있는 HTML 엔티티 (`&lt;`, `&amp;` 등) 는 *그대로*
        # 보존 — bs4 가 텍스트 노드로 인식해 디코드 후 출력. 본 형식은 시각적으로
        # `<code>&lt;decrypt&gt;cipher&lt;/decrypt&gt;</code>` 로 노출 (cipher
        # 원형 = decrypt 시 사용 가능).
        cipher_esc = _h.escape(cleaned)
        return (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">'
            '🔒 encryptedpasswords (클릭해서 펼치기)'
            '</ac:parameter>'
            '<ac:rich-text-body>'
            f'<p><code>&lt;{tag}&gt;{cipher_esc}&lt;/{tag}&gt;</code></p>'
            '</ac:rich-text-body>'
            '</ac:structured-macro>'
        )

    out = _DECRYPT_RAW_RE.sub(repl, raw_html)
    # 매크로 종료 바로 뒤에 비-공백 문자가 줄바꿈 없이 붙어 있으면 \n 삽입 — 다음
    # 단락이 자연스럽게 분리되도록.
    out = re.sub(r"(</ac:structured-macro>)(\S)", r"\1\n\2", out)
    return out


def _convert_encrypted_passwords(soup) -> None:
    """encryptedpasswords plugin 활성 시 DokuWiki 가 출력하는
    `<span class="encryptedpasswords" title="<cipher>"><a ...>••••••</a></span>`
    형태를 expand+code 매크로로 변환. cipher 는 title 속성에 들어 있음.

    plugin 미활성 (escape `<decrypt>...</decrypt>` 텍스트) 케이스는
    `_preprocess_encrypted_passwords` 가 raw HTML 단계에서 이미 처리. 두 함수
    모두 같은 storage 결과 (expand 안 code 안 cipher 보존) 를 생성하여
    plugin 설치 여부와 무관하게 일관된 변환."""
    from bs4 import BeautifulSoup as _BS

    for span in list(soup.find_all("span", class_="encryptedpasswords")):
        cipher = (span.get("title") or "").strip()
        if not cipher:
            continue
        # cipher 안 줄바꿈 / 공백 정규화 — base64 가 multi-line attribute 일 수도.
        # 텍스트 노드 형태로 보존 (escape 처리).
        cipher_esc = _h.escape(cipher)
        macro = (
            '<ac:structured-macro ac:name="expand">'
            '<ac:parameter ac:name="title">'
            '🔒 encryptedpasswords (클릭해서 펼치기)'
            '</ac:parameter>'
            '<ac:rich-text-body>'
            f'<p><code>&lt;decrypt&gt;{cipher_esc}&lt;/decrypt&gt;</code></p>'
            '</ac:rich-text-body>'
            '</ac:structured-macro>'
        )
        wrapper = _BS(macro, "html.parser")
        # span 자체를 매크로로 교체 (inline → block 이지만 Confluence 가 자동 wrap)
        span.replace_with(*list(wrapper.children))


def _calendar_iframe_macro(src: str, width: str = "750", height: str = "500") -> str:
    return (
        f'<ac:structured-macro ac:name="iframe">'
        f'<ac:parameter ac:name="src"><ri:url ri:value="{_h.escape(src, quote=True)}"/></ac:parameter>'
        f'<ac:parameter ac:name="width">{_h.escape(str(width), quote=True)}</ac:parameter>'
        f'<ac:parameter ac:name="height">{_h.escape(str(height), quote=True)}</ac:parameter>'
        f'<ac:parameter ac:name="frameborder">no</ac:parameter>'
        f'<ac:parameter ac:name="scrolling">no</ac:parameter>'
        f'</ac:structured-macro>'
    )


_YOUTUBE_FALLBACK_RES = [
    # `{{youtube>VIDEO_ID}}` plugin 미설치 시 DokuWiki 가 보통:
    # /_media/youtube/VIDEO_ID 형식의 깨진 media 링크로 fallback
    re.compile(r"^/_media/youtube/([A-Za-z0-9_\-]+)"),
    re.compile(r"^https?://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_\-]+)"),
    re.compile(r"^https?://youtu\.be/([A-Za-z0-9_\-]+)"),
]

# `{{youtube>VID}}` 가 완전히 깨져 VID 만 단독 <p> 로 렌더된 케이스:
#   <p>NEbzsV6qzQ0</p>
# 11자 base64-ish (RFC 4648 url-safe alphabet, 64^11 = 7×10^19) — 우연히
# 다른 의미의 11자 텍스트가 단독 paragraph 일 가능성 매우 낮음.
_YOUTUBE_VID_ONLY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _convert_youtube_fallback(soup) -> None:
    """YouTube embed 후보 (`{{youtube>VID}}` 미설치 fallback, youtube.com/watch
    링크, youtu.be 링크) 를 Confluence iframe 매크로 (youtube embed) 로 교체.

    fallback 깨진 media 링크 (`/_media/youtube/VID` 형식) — 부모 <p> 째 교체.
    일반 hyperlink (youtube.com/watch?v=...) — 그대로 두고 가시성 위해 변경
    안 함 (사용자가 의도적으로 텍스트 링크일 수도).
    본 함수는 *fallback 매크로 잔재* 만 처리.
    """
    from bs4 import BeautifulSoup as _BS
    for a in list(soup.find_all("a", href=True)):
        href = str(a.get("href", ""))
        m = _YOUTUBE_FALLBACK_RES[0].match(href)
        if not m:
            continue
        # 부모 <p> 안의 단일 <a> 가 깨진 fallback 모양 → 매크로 교체
        parent = a.parent
        vid = m.group(1)
        macro = _calendar_iframe_macro(
            f"https://www.youtube.com/embed/{vid}",
            width="640", height="360",
        )
        if parent and parent.name == "p" and len(parent.find_all("a")) == 1:
            parent.replace_with(_BS(macro, "html.parser"))
        else:
            a.replace_with(_BS(macro, "html.parser"))

    # 새 케이스: <p> 안에 11자 base64-ish 텍스트만 — `{{youtube>VID}}` plugin
    # 완전 미설치 시 DokuWiki 가 VID 만 본문 노출. 우연 충돌 가능성 매우 낮음
    # — 64^11 ≈ 7×10^19. 페이지 본문에 11자 단독 텍스트가 다른 의미일 확률은
    # 무시 가능.
    for p in list(soup.find_all("p")):
        # 직접 자식이 element 가 아닌 (텍스트만 + br 정도)
        if any(getattr(c, "name", None) and c.name not in ("br",) for c in p.children):
            continue
        txt = (p.get_text() or "").strip()
        if not _YOUTUBE_VID_ONLY_RE.fullmatch(txt):
            continue
        macro = _calendar_iframe_macro(
            f"https://www.youtube.com/embed/{txt}",
            width="640", height="360",
        )
        p.replace_with(_BS(macro, "html.parser"))


def _convert_google_calendar_iframe(soup) -> None:
    """`<iframe src="https://calendar.google.com/...">` 를 Confluence iframe
    매크로로. 두 케이스 처리:

    A. 실제 iframe 태그 — DokuWiki 의 html 플러그인이 활성화된 페이지
    B. escape 된 텍스트 — html 플러그인 미활성 시 `&lt;iframe ...&gt;`
       텍스트로 표시되고 src URL 부분만 `<a class="urlextern">` 으로
       auto-linkify. 부모 `<p>` 의 자식이 escaped iframe 텍스트 + `<a>`
       (URL) 패턴이면 같이 교체.

    본 도구의 일반 변환기는 iframe 을 strip (Confluence storage 거부) —
    캘린더 iframe 만 ac:structured-macro 로 보존.
    """
    from bs4 import BeautifulSoup as _BS

    # A. 실제 iframe 태그
    for iframe in list(soup.find_all("iframe")):
        src = str(iframe.get("src", ""))
        if "calendar.google.com" not in src:
            continue
        iframe.replace_with(_BS(
            _calendar_iframe_macro(
                src,
                width=str(iframe.get("width") or "750"),
                height=str(iframe.get("height") or "500"),
            ),
            "html.parser",
        ))

    # B. escape 된 텍스트 — `<a class="urlextern" href="https://calendar.google.com/...">`
    #    의 부모 <p> 의 텍스트가 iframe 텍스트 (escape) 를 포함하면 부모째 교체.
    for a in list(soup.find_all("a", href=True)):
        href = str(a.get("href", ""))
        if "calendar.google.com" not in href:
            continue
        parent = a.parent
        if parent is None:
            continue
        parent_text = parent.get_text(" ", strip=True)
        # escape 된 iframe 흔적 검사
        if "iframe" not in parent_text:
            continue
        # width / height 추출 (있으면)
        w = _re.search(r'width\s*=\s*[“"\']?(\d+)', parent_text)
        h = _re.search(r'height\s*=\s*[“"\']?(\d+)', parent_text)
        width = w.group(1) if w else "750"
        height = h.group(1) if h else "500"
        macro = _calendar_iframe_macro(href, width=width, height=height)
        parent.replace_with(_BS(macro, "html.parser"))


def _convert_footnotes(soup) -> None:
    """
    DokuWiki 의 ((풋노트)) 출력은 두 부분이다:
      본문: <sup><a class="fn_top" href="#fn__N" id="fnt__N">N)</a></sup>
      페이지 끝: <div class="footnotes">
                  <div class="fn">
                    <sup><a class="fn_bot" href="#fnt__N" id="fn__N">N)</a></sup>
                    <div class="content">풋노트 내용</div>
                  </div>
                  ...
                </div>

    Confluence 에는 footnote 코어 매크로가 없어 표준 HTML 로 옮긴다.
    본문 sup 의 anchor 링크는 그대로 둔다 (Confluence 가 in-page anchor
    지원). 페이지 끝 footnotes div 를:

        <hr/><p><strong>각주</strong></p>
        <ol>
          <li id="fn__N">내용 <a href="#fnt__N">↑</a></li>
          ...
        </ol>

    로 변환해 클릭 가능한 양방향 anchor 가 동작하도록 한다.
    """
    from bs4 import BeautifulSoup
    # 본문의 풋노트 reference (sup > a class=fn_top id=fnt__N) 의 id 도
    # Confluence 가 제거하므로 anchor 매크로 직전 삽입.
    for sup in list(soup.find_all("sup")):
        a = sup.find("a", class_="fn_top")
        if not a:
            continue
        anchor_id = a.get("id")
        if not anchor_id:
            continue
        anchor_macro = BeautifulSoup(
            f'<ac:structured-macro ac:name="anchor">'
            f'<ac:parameter ac:name="">{anchor_id}</ac:parameter>'
            f'</ac:structured-macro>',
            "html.parser",
        )
        sup.insert_before(anchor_macro)
        # id 는 어차피 사라지므로 정리만
        if a.has_attr("id"):
            del a.attrs["id"]

    for outer in list(soup.find_all("div", class_="footnotes")):
        items: list[tuple[str | None, str | None, list]] = []
        for fn in outer.find_all("div", class_="fn", recursive=False):
            sup = fn.find("sup")
            content = fn.find("div", class_="content")
            anchor_id = None
            return_href = None
            if sup:
                a = sup.find("a")
                if a:
                    anchor_id = a.get("id")
                    return_href = a.get("href")
            content_children = list(content.children) if content else []
            items.append((anchor_id, return_href, content_children))

        if not items:
            outer.decompose()
            continue

        hr = soup.new_tag("hr")
        heading = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = "각주"
        heading.append(strong)
        ol = soup.new_tag("ol")
        for anchor_id, return_href, children in items:
            li = soup.new_tag("li")
            # Confluence 는 <li id=...> 의 id attribute 를 parsing 시 제거하므로
            # 공식 anchor 매크로를 첫 자식으로 삽입해 jump target 보존.
            if anchor_id:
                anchor_macro = BeautifulSoup(
                    f'<ac:structured-macro ac:name="anchor">'
                    f'<ac:parameter ac:name="">{anchor_id}</ac:parameter>'
                    f'</ac:structured-macro>',
                    "html.parser",
                )
                li.append(anchor_macro)
            for c in children:
                li.append(c.extract() if hasattr(c, "extract") else c)
            if return_href:
                back = soup.new_tag("a")
                back["href"] = return_href
                back.string = " ↑"
                li.append(back)
            ol.append(li)

        outer.replace_with(hr)
        hr.insert_after(heading)
        heading.insert_after(ol)


def _convert_visual_residue(soup) -> None:
    """
    element-mapping §C 의 '시각 효과 손실' 항목을 Confluence 가 인지할 수
    있는 inline style 또는 표준 태그로 격상.

    - <div class="wrap_left|wrap_right|wrap_center"> -> style="text-align: …;"
      Confluence storage 가 인라인 style 은 허용한다. 외관 보존됨.
    - <td|th class="leftalign|centeralign|rightalign"> -> 같은 인라인 style
    - <em class="u"> -> <u> (밑줄. DokuWiki 코어가 __underline__ 을 em.u 로
      낸다; Confluence 도 <u> 받음).

    `_convert_wrap_callouts` 가 의미 클래스 매크로 변환을 먼저 수행하므로
    여기 도착하는 wrap_left/right/center 는 *순수 정렬 div* 인 경우.
    """
    # 1) 정렬 div
    for div in list(soup.find_all("div")):
        classes = div.get("class") or []
        styles: list[str] = []
        for c in classes:
            if c in WRAP_ALIGN_STYLE_MAP:
                styles.append(WRAP_ALIGN_STYLE_MAP[c])
        if styles:
            existing = div.get("style", "")
            joined = " ".join(styles)
            div["style"] = (existing.rstrip(";") + "; " + joined).strip("; ").strip() if existing else joined

    # 2) 표 셀 정렬
    for cell in list(soup.find_all(["td", "th"])):
        classes = cell.get("class") or []
        styles: list[str] = []
        for c in classes:
            if c in TABLE_ALIGN_CLASS_MAP:
                styles.append(TABLE_ALIGN_CLASS_MAP[c])
        if styles:
            existing = cell.get("style", "")
            joined = " ".join(styles)
            cell["style"] = (existing.rstrip(";") + "; " + joined).strip("; ").strip() if existing else joined

    # 3) em.u -> <u>
    for em in list(soup.find_all("em")):
        classes = em.get("class") or []
        if "u" in classes:
            u = soup.new_tag("u")
            for c in list(em.children):
                u.append(c.extract())
            em.replace_with(u)


def _convert_todos(soup) -> None:
    """
    DokuWiki todo plugin 출력을 Confluence task-list (체크박스) 로 변환.

    Step 1: <ul> 의 모든 직접 <li> 가 단일 pure todo (li 의 텍스트와 todo
            span 의 텍스트가 동일) 이면 <ul> 전체를 <ac:task-list> 로 치환.
    Step 2: <ul> 안의 mixed list — todo + 비-todo li 가 섞임. 각 todo li 만
            *그 li 안에 단일 task-list* 로 wrap (Confluence 가 li 안의
            block task-list 받음). 비-todo li 는 그대로.
    Step 3: <li> 안에 todo 가 있지만 추가 텍스트도 있음 (`[x] do thing — note`).
            todo span 만 task-list 로 wrap, 나머지 li 텍스트는 그대로.
    Step 4: 부모 li 가 *전혀 없는* 인라인 todo — Confluence 는 인라인
            task-list 불가 → unicode 글리프 (☑ / ☐) 로 교체.
    """
    counter = [0]

    def _next_id() -> int:
        counter[0] += 1
        return counter[0]

    def _make_task_list_with_one(todo) -> "object":
        checked, text = _todo_checked_and_text(todo)
        task_list = soup.new_tag("ac:task-list")
        task_list.append(_build_ac_task(soup, _next_id(), checked, text))
        return task_list

    # Step 1: pure todo <ul> (모든 li 가 단일 todo + li 본문 == todo 본문)
    for ul in list(soup.find_all("ul")):
        lis = ul.find_all("li", recursive=False)
        if not lis:
            continue
        todos_in_lis = []
        all_pure = True
        for li in lis:
            spans = li.find_all("span", class_="todo")
            if len(spans) != 1:
                all_pure = False
                break
            todo = spans[0]
            if li.get_text(strip=True) != todo.get_text(strip=True):
                all_pure = False
                break
            todos_in_lis.append(todo)
        if not all_pure or not todos_in_lis:
            continue
        task_list = soup.new_tag("ac:task-list")
        for todo in todos_in_lis:
            checked, text = _todo_checked_and_text(todo)
            task_list.append(_build_ac_task(soup, _next_id(), checked, text))
        ul.replace_with(task_list)

    # Step 2 & 3: 남은 모든 todo span 처리
    for todo in list(soup.find_all("span", class_="todo")):
        # 부모 chain 에서 가장 가까운 li 또는 block 컨테이너 찾기
        li_ancestor = None
        cur = todo.parent
        while cur is not None and getattr(cur, "name", None) is not None:
            if cur.name == "li":
                li_ancestor = cur
                break
            if cur.name in ("ac:task-list", "ac:task", "ac:task-body",
                            "code", "pre", "ac:plain-text-body"):
                # 이미 task-list 안 또는 코드 안 — 건드리지 않음
                li_ancestor = "skip"
                break
            cur = cur.parent

        if li_ancestor == "skip":
            continue

        if li_ancestor is None:
            # 부모 li 없음 — inline todo. Confluence 는 inline task-list 불가
            # → unicode 글리프로
            checked, text = _todo_checked_and_text(todo)
            glyph = "☑" if checked else "☐"
            todo.replace_with(f"{glyph} {text}")
            continue

        li = li_ancestor
        # li 본문 = todo 본문이면 (Step 1 에서 잡혔어야 하지만 안 잡힌 케이스 —
        # mixed ul) → li 전체를 task-list 로 교체할 수 있도록 single-task
        # task-list 로 wrap, li 내부에 박음.
        li_text = li.get_text(strip=True)
        todo_text = todo.get_text(strip=True)
        if li_text == todo_text:
            # li 내용이 정확히 이 todo 하나 — li 안의 모든 children 을
            # 단일 task-list 로 교체
            task_list = _make_task_list_with_one(todo)
            li.clear()
            li.append(task_list)
        else:
            # li 안에 todo + 다른 텍스트 — todo span 만 task-list 로 교체,
            # li 의 나머지 텍스트는 보존. 다만 task-list 가 block 이라
            # 시각적으로 줄바꿈이 생김.
            todo.replace_with(_make_task_list_with_one(todo))


def _convert_html_to_storage(
    raw_html: str,
    src_root: Path,
) -> tuple[str, list[dict], list[dict], str | None, list[str]]:
    """
    DokuWiki export_xhtmlbody → Confluence storage format 변환 (메인 entry).

    ## 인자/반환

    raw_html:   `?do=export_xhtmlbody` 응답 본문
    src_root:   미디어 파일 lookup 의 base path

    반환 tuple:
      storage_xml:  Confluence storage XML 문자열
      links:        [{'target': 'wiki:syntax', 'placeholder': 'dwc-link:wiki:syntax',
                      'anchor': '...'|None}, ...]
      attachments:  [{'media_id': 'wiki:foo.png', 'filename': 'foo.png',
                      'src_path': str|None}, ...]
      title:        첫 h1 의 텍스트 (없으면 None)
      tags:         dokuwiki tag 플러그인의 page tag 값 리스트 (Confluence 페이지
                    label 로 매핑 후보)

    ## 변환 파이프라인 (순서 중요)

    이전 단계 변경이 다음 단계 입력 — 순서 바꾸면 깨짐. grep '# § STEP' 으로 anchor.

    ┌─────┬──────────────────────────────────────────────────────┬─────────────────┐
    │ 단계│ 역할                                                  │ 호출 함수       │
    ├─────┼──────────────────────────────────────────────────────┼─────────────────┤
    │ 0   │ full-HTML fallback — `<main id=dokuwiki__content>`   │ (인라인)        │
    │     │ 의 `<div class=page>` 만 살림                          │                 │
    │ 1   │ 플러그인 매크로 → Confluence 매크로 (위 src 보존)     │                 │
    │ 1.1 │  wrap callouts (info/tip/note/warning/panel)         │ _convert_wrap_callouts │
    │ 1.2 │  monthcal fallback → 정적 캘린더 <table>             │ _convert_monthcal_fallback │
    │ 1.3 │  youtube fallback → iframe embed                     │ _convert_youtube_fallback │
    │ 1.4 │  encryptedpasswords → expand+code (cipher 보존)      │ _convert_encrypted_passwords │
    │ 1.5 │  Google Calendar iframe → Confluence iframe          │ _convert_google_calendar_iframe │
    │ 1.6 │  smiley 이미지 → emoji 텍스트                          │ _convert_smileys │
    │ 1.7 │  정렬/밑줄/표 셀 정렬 → inline style                  │ _convert_visual_residue │
    │ 1.8 │  풋노트 → <hr/><strong>각주</strong> + anchor 매크로  │ _convert_footnotes │
    │ 1.9 │  todo → task-list / unicode 글리프                    │ _convert_todos │
    │ 2   │ 위험 태그 strip (script/style/iframe/form 등)         │ (인라인)        │
    │ 3   │ DokuWiki chrome strip (#dokuwiki__site 등)            │ (인라인)        │
    │ 4   │ secedit / toc / EDIT 코멘트 / 잔존 ~~MACRO~~ strip   │ (인라인)        │
    │ 5   │ 제목 추출 (첫 h1 또는 h2)                              │ (인라인)        │
    │ 6   │ <img> → <ac:image><ri:attachment>                    │ (인라인)        │
    │ 7   │ 내부 링크 → dwc-link: placeholder (2-pass rewrite)    │ (인라인)        │
    │ 8   │ 외부 링크 / 첨부 링크 정리                            │ (인라인)        │
    │ 9   │ 코드 블록 → ac:structured-macro[name=code]            │ (인라인)        │
    │ 10  │ tag 플러그인 → Confluence label 후보                   │ (인라인)        │
    │ 11  │ 잔존 class/id/data-* 속성 정리                        │ (인라인)        │
    │ 12  │ void 태그 self-close + serialize                       │ (인라인)        │
    └─────┴──────────────────────────────────────────────────────┴─────────────────┘
    """
    from bs4 import BeautifulSoup, Comment

    # 0.5) encryptedpasswords escape 텍스트 (multi-line cipher) 를 *파싱 전* 에
    # 직접 storage 매크로로 치환. cipher 안 inline 마크업 잔재 회피 — 자세한 사유는
    # _preprocess_encrypted_passwords docstring 참조.
    raw_html = _preprocess_encrypted_passwords(raw_html)

    soup = BeautifulSoup(raw_html, "html.parser")

    # 0) full-HTML 응답 폴백.
    # `do=export_xhtmlbody` 인데도 일부 페이지는 (ACL denial, 일부 플러그인,
    # 일부 버전에서) 헤더/풋터까지 포함한 풀 HTML 문서를 토해낸다.
    # `<main id="dokuwiki__content">` 안의 `<div class="page">` 가 실제
    # 콘텐츠. 그게 보이면 그 children 만 살리고 나머지는 통째로 버린다.
    main = soup.find("main", id="dokuwiki__content")
    if main is not None:
        page_div = main.find("div", class_="page") or main
        new_soup = BeautifulSoup("", "html.parser")
        for child in list(page_div.children):
            new_soup.append(child.extract())
        soup = new_soup

    # 1) Confluence storage 가 받아들이지 않거나 의미 없는 태그 일괄 제거.
    # script/style/link/meta/noscript/iframe/embed/object/form 은 위험 또는 노이즈.
    # input/button/select/option/textarea 는 폼 outside 에서도 dokuwiki plugin
    # (예: todo) 이 인라인으로 박는 인터랙티브 컨트롤 — Confluence storage 는
    # 거부. todo plugin 은 별도 변환 룰에서 텍스트 마커로 미리 교체된다 (아래
    # 1.5 단계).
    #
    # 1.4) wrap plugin / callouts -> Confluence info/tip/note/warning/panel
    #
    # dokuwiki wrap 의 의미 클래스:
    #   <WRAP info|help ...>    -> <div class="wrap_info ... plugin_wrap"> -> Confluence info 매크로
    #   <WRAP tip ...>          -> wrap_tip                                 -> tip 매크로
    #   <WRAP important|note>   -> wrap_important / wrap_note               -> note 매크로
    #   <WRAP alert|warning|danger> -> wrap_alert/warning/danger             -> warning 매크로
    #   <WRAP box|round ...>    -> wrap_box / wrap_round (제목 없는 박스)    -> panel 매크로
    #
    # 인라인 강조:
    #   <wrap em>X</wrap>   -> <em class="wrap_em plugin_wrap">X</em>   -> <strong>X</strong>
    #   <wrap hi>X</wrap>   -> <em class="wrap_hi plugin_wrap">X</em>   -> 노란 background-color span
    #
    # 정렬/레이아웃 클래스(wrap_left/right/center/clear/indent 등) 는 의미가
    # 없어서 별도 변환 없이 div 그대로 두고, class 정리 단계에서 떨군다.
    _convert_wrap_callouts(soup)

    # 1.41) monthcal 플러그인 미설치 fallback 링크 -> 정적 캘린더 표
    _convert_monthcal_fallback(soup)

    # 1.412) youtube 플러그인 미설치 fallback -> Confluence iframe 매크로 (youtube embed)
    _convert_youtube_fallback(soup)

    # 1.414) encryptedpasswords (<decrypt>...</decrypt>) → expand + inline code
    _convert_encrypted_passwords(soup)

    # 1.415) Google Calendar iframe -> Confluence iframe 매크로
    _convert_google_calendar_iframe(soup)

    # 1.42) DokuWiki 코어 smiley 이미지 -> emoji 텍스트
    _convert_smileys(soup)

    # 1.45) 정렬 / 밑줄 / 표 셀 정렬 -> inline style 또는 표준 태그
    _convert_visual_residue(soup)

    # 1.47) 풋노트 ((text)) -> <hr/><strong>각주</strong><ol>
    _convert_footnotes(soup)

    # 1.5) todo plugin -> Confluence task-list / text marker
    #
    # dokuwiki todo:
    #   <span class="todo">
    #     <input type=checkbox class=todocheckbox [checked]/>
    #     <span class="todouser">[✓ user, date]</span>
    #     <span class="todotext clickabletodo todohlght">
    #       <span class="todoinnertext">텍스트</span>
    #     </span>
    #   </span>
    #
    # 두 가지 모드:
    #   (a) <ul> 의 모든 직접 <li> 가 "오직 단일 todo" 인 경우 (li.get_text()
    #       와 todo.get_text() 가 같음) → 그 <ul> 을 통째로 <ac:task-list>
    #       로 치환해 클릭 가능한 Confluence 체크박스가 되도록 한다.
    #       Confluence 의 ac:task-list 는 block-level 이라 li 안에 박으면
    #       렌더링 깨질 수 있으므로 ul 전체 치환만 안전하다.
    #   (b) 그 외 (텍스트 섞인 li, nested ul, 인라인 todo) → 기존의
    #       `[x] 텍스트` / `[ ] 텍스트` 인라인 텍스트 마커 폴백.
    _convert_todos(soup)

    for tag_name in ("script", "style", "link", "meta", "noscript",
                     "iframe", "embed", "object", "form", "head",
                     "input", "button", "select", "option", "textarea"):
        for t in soup.find_all(tag_name):
            t.decompose()
    for tag_name in ("html", "body"):
        for t in soup.find_all(tag_name):
            t.unwrap()

    # DokuWiki 의 chrome (login 폼이 박힌 mode_denied 응답이나 풀 페이지에
    # 묻어 들어오는 헤더/푸터/내비) 제거.
    for sel_id in (
        "dokuwiki__site", "dokuwiki__top", "dokuwiki__header",
        "dokuwiki__footer", "dokuwiki__pagetools", "dokuwiki__aside",
        "dokuwiki__usertools", "dokuwiki__sitetools",
    ):
        for t in soup.find_all(id=sel_id):
            t.decompose()
    # 주의: 'no' 클래스는 DokuWiki 가 blockquote 안 내용을 감싸는 *정상
    # content wrapper* 로도 쓴다 (e.g. <blockquote><div class='no'>URL 리스트</div>
    # </blockquote>). chrome 제거 목록에서 제외.
    for cls in ("breadcrumbs", "trace", "tools", "docInfo", "headings"):
        for t in soup.find_all(class_=cls):
            t.decompose()

    # 2) 기존 노이즈 제거
    for a in soup.find_all("a", class_="secedit"):
        a.decompose()
    for div in soup.find_all("div", class_="toc"):
        div.decompose()
    for div in soup.find_all(id="dw__toc"):
        div.decompose()
    # DokuWiki 의 EDIT{...} section-edit 메타 코멘트 등 모든 HTML 코멘트 제거
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    # 미설치/비활성 플러그인이 처리하지 못해 plain text 로 새어나온 `~~MACRO~~`
    # 토큰 제거. dokuwiki 가 인식하면 HTML 로 변환되므로 raw 에 남았다는 것은
    # 동작하지 않은 매크로. 단 code/pre/td/th/kbd 안의 것은 *문서 내용* (syntax
    # 데모, 표 데이터) 일 가능성이 높으므로 보존.
    _doku_macro_residue = re.compile(r"~~[A-Za-z][A-Za-z0-9_:]*~~")
    _doku_macro_preserve_parents = ("code", "pre", "td", "th", "kbd")
    for s in list(soup.find_all(string=_doku_macro_residue)):
        if s.find_parent(_doku_macro_preserve_parents):
            continue
        new_text = _doku_macro_residue.sub("", str(s))
        s.replace_with(new_text)

    # 2) 제목 후보 (첫 h1, 없으면 첫 h2)
    title = None
    h = soup.find("h1") or soup.find("h2")
    if h:
        # 헤딩 내부의 a/span 등을 모두 평탄화한 텍스트
        title = h.get_text(strip=True) or None

    links: list[dict] = []
    attachments: dict[str, dict] = {}

    # 3) <img> -> <ac:image>
    for img in list(soup.find_all("img")):
        src_orig = img.get("src", "")
        cat = _categorize_href(src_orig)
        if cat["kind"] == "external":
            # DokuWiki fetch.php proxy 경유로 외부 이미지를 감싼 경우
            # (?media=http%3A...) src 를 실제 외부 URL 로 교체.
            if cat.get("href") and cat["href"] != src_orig:
                img["src"] = cat["href"]
            continue
        if cat["kind"] != "media":
            continue
        media_id = cat["id"]
        filename = _media_filename(media_id)
        src_path = _resolve_media_path(src_root, media_id)
        attachments.setdefault(
            media_id,
            {
                "media_id": media_id,
                "filename": filename,
                "src_path": str(src_path) if src_path else None,
            },
        )
        ac_image = soup.new_tag("ac:image")
        for attr_html, attr_ac in (("width", "ac:width"), ("height", "ac:height"), ("alt", "ac:alt"), ("title", "ac:title")):
            v = img.get(attr_html)
            if v:
                ac_image[attr_ac] = v
        ri = soup.new_tag("ri:attachment")
        ri["ri:filename"] = filename
        ac_image.append(ri)

        # 부모 <a> 가 단일 자식 형태(클릭 가능 이미지) 면 함께 제거하고 ac:image 만 남김
        parent = img.parent
        if (
            parent is not None
            and parent.name == "a"
            and sum(1 for _ in parent.children if getattr(_, "name", None) is not None or (isinstance(_, str) and _.strip())) == 1
        ):
            parent.replace_with(ac_image)
        else:
            img.replace_with(ac_image)

    # 4-pre) dokuwiki 의 tag 페이지 링크 (/tag/<value>?do=showtag&tag=<value>) 는
    # 일급 페이지가 아니라 동적 view 라 placeholder 가 미해결로 평문 격하된다.
    # 대신 *Confluence 페이지 label* 로 매핑하기 위해 tag 값을 별도 set 으로
    # 수집하고 a 자체는 <ac:label> 같이 의미 있는 inline marker 로.
    # storage XML 에는 직접 label 을 박을 수 없으므로 dwc-tag:<value> 라는
    # 토큰 placeholder 로 남기고 cmd_upload 단계에서 PUT 후 label API 로
    # 별도 적용.
    page_tags: list[str] = []
    for tag_a in list(soup.find_all("a", rel="tag")):
        href = tag_a.get("href", "")
        # /tag/<encoded>?do=showtag&tag=<encoded> 형태에서 tag 값 추출
        from urllib.parse import urlparse, parse_qs, unquote
        q = parse_qs(urlparse(href).query)
        tag_val = (q.get("tag") or [""])[0]
        if not tag_val:
            # path 에서 추출
            path = unquote(urlparse(href).path or "").strip("/")
            if path.startswith("tag/"):
                tag_val = path[4:]
        tag_val = (tag_val or "").rstrip(",").strip()
        if tag_val:
            page_tags.append(tag_val)
        # a 는 텍스트만 남기고 unwrap
        tag_a.replace_with(tag_a.get_text() or "")

    # 4) <a> 변환
    for a in list(soup.find_all("a")):
        href = a.get("href", "")
        # DokuWiki 가 부여하는 data-wiki-id / wikilink* 클래스가 있으면
        # URL rewrite 설정과 무관하게 가장 신뢰할 수 있는 단서다.
        wiki_id_attr = a.get("data-wiki-id")
        classes = a.get("class") or []
        is_wikilink = any(c.startswith("wikilink") for c in classes)
        if wiki_id_attr or (is_wikilink and not href.startswith(("http://", "https://"))):
            target_id = wiki_id_attr or _href_to_doku_id_via_path(href)
            if target_id:
                from urllib.parse import urlparse as _urlparse
                anchor = _urlparse(href).fragment or None
                cat = {"kind": "page", "id": target_id, "anchor": anchor}
            else:
                cat = _categorize_href(href)
        else:
            cat = _categorize_href(href)
        if cat["kind"] == "page":
            target = cat["id"]
            anchor = cat.get("anchor")
            placeholder = f"{DOKU_LINK_SCHEME}:{target}"
            if anchor:
                placeholder += f"#{anchor}"
            a["href"] = placeholder
            for attr in ("class", "title", "rel", "data-wiki-id"):
                if attr in a.attrs:
                    del a.attrs[attr]
            links.append({"target": target, "placeholder": placeholder, "anchor": anchor})
        elif cat["kind"] == "media":
            media_id = cat["id"]
            filename = _media_filename(media_id)
            src_path = _resolve_media_path(src_root, media_id)
            attachments.setdefault(
                media_id,
                {
                    "media_id": media_id,
                    "filename": filename,
                    "src_path": str(src_path) if src_path else None,
                },
            )
            ac_link = soup.new_tag("ac:link")
            ri = soup.new_tag("ri:attachment")
            ri["ri:filename"] = filename
            ac_link.append(ri)
            body = soup.new_tag("ac:link-body")
            body.string = a.get_text() or filename
            ac_link.append(body)
            a.replace_with(ac_link)
        elif cat["kind"] == "action":
            # 의미 없는 액션 링크는 텍스트만 남김
            a.replace_with(a.get_text())
        # anchor / external 은 그대로

    # 5) <pre class="code..."> -> code 매크로
    code_bodies: dict[str, str] = {}
    for idx, pre in enumerate(list(soup.find_all("pre"))):
        classes = pre.get("class") or []
        if "code" not in classes and "file" not in classes:
            continue
        lang = next((c for c in classes if c not in ("code", "file")), None)
        text = pre.get_text()
        sentinel = f"{CODE_BODY_SENTINEL_PREFIX}{idx}__"
        code_bodies[sentinel] = text

        macro = soup.new_tag("ac:structured-macro")
        macro["ac:name"] = "code"
        if lang:
            param = soup.new_tag("ac:parameter")
            param["ac:name"] = "language"
            param.string = lang
            macro.append(param)
        body = soup.new_tag("ac:plain-text-body")
        body.string = sentinel
        macro.append(body)
        pre.replace_with(macro)

    # 6) 잡 class/id 정리 (보존이 안전한 것은 남긴다)
    NOISE_CLASS_PREFIXES = (
        "sectionedit", "wikilink", "level", "media", "interwiki",
        "plugin_",  # plugin_include__<page-id> 같이 ID 가 박힌 dynamic class
        # 의미 클래스(wrap_info/tip/note/warning/box/round/em/hi) 는 별도
        # 변환 룰이 *이미 처리한 뒤*이므로, 여기 도착하는 wrap_* 잔여는
        # 의미 없는 layout 변형 (wrap_clear/indent/outdent/lo/pre 등). 정리.
        "wrap_",
    )
    NOISE_CLASS_EXACT = {
        "toc", "page", "dokuwiki", "plugin_include_content",
        # 표 셀 정렬 클래스: _convert_visual_residue 가 inline style 로 옮긴 후 잔여 class 제거
        "leftalign", "centeralign", "rightalign",
        # em.u 의 'u' 도 _convert_visual_residue 에서 <u> 로 격상 후 잔여 — 이미 element 통째 교체되어 보통 안 남지만 방어적
        "u",
    }
    for tag in soup.find_all(True):
        cls = tag.get("class")
        if not cls:
            continue
        kept = [
            c for c in cls
            if not any(c.startswith(p) for p in NOISE_CLASS_PREFIXES) and c not in NOISE_CLASS_EXACT
        ]
        if kept:
            tag["class"] = kept
        else:
            del tag.attrs["class"]

    # 7) 직렬화 + void element XML 자체 닫기 + CDATA 치환
    result = "".join(str(c) for c in soup.children)
    result = _re.sub(r"<(br|hr|img)([^>]*?)(?<!/)\s*>", r"<\1\2/>", result)

    for sentinel, text in code_bodies.items():
        safe = text.replace("]]>", "]]]]><![CDATA[>")
        result = result.replace(sentinel, f"<![CDATA[{safe}]]>")

    return result, links, list(attachments.values()), title, page_tags


def cmd_convert(args: argparse.Namespace) -> int:
    """S3: RENDERED 페이지의 raw XHTML 을 Confluence storage XML 로 변환.

    state.db 갱신: `pages.storage_path / content_hash / converted_at`, status=
    'CONVERTED'. 변환 흐름 + 매크로 매핑 표는 `_convert_html_to_storage` 의
    docstring 참고. `links` 와 `attachments` 테이블도 채움 (rewrite-links /
    upload 에서 사용). `--force` 면 UPLOADED 도 다시 변환 (storage 갱신
    후 content_hash 변경 시 다음 upload 가 PUT).
    """
    try:
        import bs4  # noqa: F401
    except ImportError:
        log("beautifulsoup4 가 필요합니다: pip install -r requirements.txt")
        return 2

    STORAGE_DIR.mkdir(exist_ok=True)
    conn = db_connect(args.db)
    db_init(conn)

    src_root_str = db_get_meta(conn, "dokuwiki_src")
    if not src_root_str:
        log("dokuwiki_src 메타가 없습니다. 먼저 discover 를 실행하세요.")
        return 2
    src_root = Path(src_root_str)

    if args.only:
        where, params = "doku_id = ?", (args.only,)
    elif args.force:
        # 이미 업로드된 페이지도 변환기 변경 시 재변환 필요 → UPLOADED 포함
        where, params = "status IN ('RENDERED', 'CONVERTED', 'FAILED', 'UPLOADED')", ()
    else:
        where, params = "status = 'RENDERED'", ()

    rows = conn.execute(
        f"SELECT doku_id, raw_xhtml_path FROM pages WHERE {where} ORDER BY doku_id", params
    ).fetchall()
    log(f"convert 대상: {len(rows)} 페이지")

    ok = failed = 0
    for doku_id, raw_path_str in rows:
        if not raw_path_str:
            # raw 가 없는 페이지(예: _ensure_namespace_stubs / _promote_skipped
            # 가 만든 placeholder)는 이미 storage_path 가 채워져 있고 다시
            # 변환할 입력 자체가 없으므로 silently skip 한다 — fail 아님.
            continue

        raw_path = Path(raw_path_str)
        if not raw_path.is_file():
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? WHERE doku_id=?",
                (f"raw missing: {raw_path}", now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue

        try:
            raw_html = raw_path.read_text(encoding="utf-8", errors="replace")
            storage_xml, links_out, attachments_out, h1_title, page_tags = _convert_html_to_storage(
                raw_html, src_root
            )
        except Exception as e:  # noqa: BLE001
            log(f"  [FAIL] {doku_id}: {e}")
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? WHERE doku_id=?",
                (f"convert error: {e!r}", now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue

        rel = Path(*doku_id.split(":")).with_suffix(".xml")
        out_path = STORAGE_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(storage_xml, encoding="utf-8")
        content_hash = sha256_bytes(storage_xml.encode("utf-8"))

        # tags: pages.title 등과 함께 별도 meta key 로 저장 (label API 단계에서 사용)
        if page_tags:
            db_set_meta(conn, f"page_tags:{doku_id}", "\n".join(page_tags))
        else:
            # 이전에 있던 tag 정리
            conn.execute("DELETE FROM meta WHERE key=?", (f"page_tags:{doku_id}",))

        # 멱등성: 이 페이지의 이전 links 와 미업로드 attachments 정리 후 재기록.
        # 이미 UPLOADED 인 첨부는 보존해 다음 run 에서 중복 업로드를 막는다.
        conn.execute("DELETE FROM links WHERE src_doku_id=?", (doku_id,))
        conn.execute(
            "DELETE FROM attachments WHERE page_doku_id=? AND status != 'UPLOADED'",
            (doku_id,),
        )

        for link in links_out:
            conn.execute(
                """
                INSERT OR REPLACE INTO links(src_doku_id, placeholder, target_doku_id, resolved)
                VALUES (?, ?, ?, 0)
                """,
                (doku_id, link["placeholder"], link["target"]),
            )

        for att in attachments_out:
            size = None
            sha = None
            if att["src_path"]:
                try:
                    p = Path(att["src_path"])
                    size = p.stat().st_size
                    sha = sha256_file(p)
                except OSError:
                    pass
            status = "DISCOVERED" if att["src_path"] else "FAILED"
            err = None if att["src_path"] else "media file not found under src/media/"
            conn.execute(
                """
                INSERT INTO attachments(
                    page_doku_id, media_id, src_path, size, sha256, status, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(page_doku_id, media_id) DO UPDATE SET
                    src_path = excluded.src_path,
                    size = excluded.size,
                    sha256 = excluded.sha256,
                    status = CASE WHEN attachments.status = 'UPLOADED' THEN attachments.status ELSE excluded.status END,
                    last_error = excluded.last_error
                """,
                (doku_id, att["media_id"], att["src_path"], size, sha, status, err),
            )

        # 제목은 h1 우선, 그다음 기존 값.
        # 단 기존 title 이 이미 *disambiguated* 형식 (e.g. 'DokuWiki (wiki:dokuwiki)'
        # 또는 'X [doku_id]') 이면 보존 — cmd_upload 의 reactive dis 결과나
        # _disambiguate_duplicate_titles 결과를 h1 으로 덮어쓰면 Confluence
        # 측 페이지 title 과 어긋나 다음 update 가 400 으로 거부된다.
        if h1_title:
            existing_row = conn.execute(
                "SELECT title FROM pages WHERE doku_id=?", (doku_id,)
            ).fetchone()
            existing = existing_row[0] if existing_row else None
            is_disambiguated = bool(
                existing
                and existing.startswith(h1_title)
                and len(existing) > len(h1_title)
                and existing[len(h1_title):].lstrip().startswith(("(", "["))
            )
            if not is_disambiguated:
                conn.execute(
                    "UPDATE pages SET title=? WHERE doku_id=?",
                    (h1_title, doku_id),
                )

        conn.execute(
            """
            UPDATE pages
               SET storage_path=?, content_hash=?, status='CONVERTED',
                   last_error=NULL, converted_at=?, last_checked_at=?
             WHERE doku_id=?
            """,
            (str(out_path), content_hash, now_iso(), now_iso(), doku_id),
        )
        conn.commit()
        ok += 1

    # convert 가 h1 추출로 title 을 덮어쓴 직후 dis 한 번 호출. upload 단계의
    # dis 호출과 멱등하므로 중복 호출이 안전하다. 이걸 빼면 convert 직후 잠시
    # 중복 title 이 노출되어 report 출력 등에서 혼란을 준다.
    dup = _disambiguate_duplicate_titles(conn)
    if dup:
        log(f"convert 후 중복 title disambiguation: {dup}개")

    log(f"convert 완료: ok={ok} failed={failed}")
    conn.close()
    return 0


# § S4~S6: Upload

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # Confluence Cloud 첨부 한도 (100MB)
# Confluence 본문 한도 — 직접 노출은 안 됨, 5MB 부근에서 400 + "body too long"
# 응답. rewrite-oversized-pages 가 그 한도 초과 페이지 skeleton 폴백 처리.

# § Confluence API 공통 상수 (retry / 캡쳐)
REQUEST_MAX_RETRIES = 6           # _request_with_retry 의 시도 횟수 (429 + 5xx)
REQUEST_BACKOFF_MAX_SEC = 60.0    # 지수 백오프 상한
CAPTURE_VIEWPORT_W = 1280         # Playwright viewport 폭 (DokuWiki / Confluence 양측)
CAPTURE_VIEWPORT_H = 900          # Playwright viewport 초기 높이 (캡쳐 시 동적 조절)
CAPTURE_MAX_HEIGHT_PX = 12000     # full-page 캡쳐 height clip — 이미지 100+ 페이지가
                                  # 100MB 첨부 한도 초과 + 갤러리 비대화 회피
                                  # (자세한 사유는 docs/MEMORY.md "배운 점" 참조)


CREDENTIAL_HELP = """
필요 환경변수:
  CONFLUENCE_BASE_URL    https://<your-domain>.atlassian.net/wiki
  CONFLUENCE_EMAIL       Atlassian 계정 이메일
  CONFLUENCE_API_TOKEN   API 토큰 (https://id.atlassian.com/manage-profile/security/api-tokens 에서 생성)
  CONFLUENCE_SPACE_KEY   대상 공간 키 (UI → 공간 설정)
  CONFLUENCE_ROOT_PAGE_ID  마이그레이션 트리 루트 페이지 ID
또는 --base-url / --email / --api-token / --space-key / --root-page-id 인자로 직접 전달.

.secrets/confluence.env 파일에 KEY=VALUE 로 적고 `set -a; source .secrets/confluence.env; set +a` 권장.
샘플은 저장소 루트의 .env.example 참고.""".strip()


def _load_users_map(path: str | None) -> dict[str, str]:
    """
    --users-map <json> 파일에서 dokuwiki 사용자명 -> Confluence accountId
    매핑을 로드.

    Format: { "alice": "5e7f1234...", "bob": "60a01234..." }

    매핑 없으면 빈 dict — 호출자가 fallback 으로 텍스트만 표시.
    """
    if not path:
        return {}
    try:
        data = _json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log(f"users-map: dict 형식 아님 → 무시")
            return {}
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError) as e:
        log(f"users-map 로드 실패 ({path}): {e}")
        return {}


def _format_user(user: str, users_map: dict[str, str]) -> str:
    """dokuwiki user name -> Confluence storage 의 user 표시 형식.

    매핑 있을 때: <ac:link><ri:user ri:account-id='...'/></ac:link>
    매핑 없을 때: 일반 텍스트 user name (escape 처리는 호출자)
    """
    account_id = users_map.get(user)
    if account_id:
        return f'<ac:link><ri:user ri:account-id="{account_id}"/></ac:link>'
    return user


def _apply_page_labels(session, base_url: str, page_id: str, labels: list[str]) -> None:
    """페이지에 Confluence label 들을 적용. v1 API (POST /rest/api/content/{id}/label)
    사용 — v2 의 label API 는 read-only 라 추가는 v1 endpoint 만 가능.
    Confluence label 은 lowercase + alphanumeric/-/_ 만 허용 — 자동 sanitize.
    """
    sanitized: list[dict] = []
    seen: set[str] = set()
    for raw in labels:
        # 공백/특수문자 → hyphen, lowercase
        clean = _re.sub(r"\s+", "-", raw.strip().lower())
        clean = _re.sub(r"[^a-z0-9가-힣_\-]", "", clean)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        sanitized.append({"prefix": "global", "name": clean})
    if not sanitized:
        return
    resp = _request_with_retry(
        session, "POST",
        f"{base_url}/rest/api/content/{page_id}/label",
        json=sanitized,
    )
    if resp is None or resp.status_code >= 400:
        log(f"    label 적용 실패 (page {page_id}): {resp.status_code if resp else 'no resp'}")


def _confluence_session(args: argparse.Namespace) -> "requests.Session | None":
    """인증된 requests.Session 반환. 자격증명/base_url 누락 시 None."""
    import requests

    missing = []
    if not getattr(args, "base_url", None):
        missing.append("CONFLUENCE_BASE_URL")
    if not args.email:
        missing.append("CONFLUENCE_EMAIL")
    if not args.api_token:
        missing.append("CONFLUENCE_API_TOKEN")
    if missing:
        log(f"자격증명/설정 누락 — Confluence API 호출 불가. 누락: {', '.join(missing)}")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return None
    s = requests.Session()
    s.auth = (args.email, args.api_token)
    s.headers.update({"Accept": "application/json"})
    return s


def _request_with_retry(session, method: str, url: str, **kwargs) -> "requests.Response | None":
    """429/5xx 에 대해 지수 백오프. 6회 시도 후 마지막 응답 반환."""
    import requests

    delay = 1.0
    last_resp = None
    for _attempt in range(REQUEST_MAX_RETRIES):
        try:
            resp = session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
        except requests.RequestException as e:
            log(f"    네트워크 에러, {delay}s 대기: {e}")
            time.sleep(delay)
            delay = min(delay * 2, REQUEST_BACKOFF_MAX_SEC)
            continue
        last_resp = resp
        if resp.status_code < 400:
            return resp
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
            log(f"    429, {wait}s 대기 후 재시도")
            time.sleep(wait)
            delay = min(delay * 2, REQUEST_BACKOFF_MAX_SEC)
            continue
        if 500 <= resp.status_code < 600:
            log(f"    {resp.status_code}, {delay}s 대기 후 재시도")
            time.sleep(delay)
            delay = min(delay * 2, REQUEST_BACKOFF_MAX_SEC)
            continue
        return resp
    return last_resp


def _resolve_space_id(session, base_url: str, space_key: str) -> str | None:
    resp = _request_with_retry(session, "GET", f"{base_url}/api/v2/spaces", params={"keys": space_key})
    if resp is None or resp.status_code >= 400:
        log(f"공간 조회 실패: {resp.status_code if resp else 'no response'}")
        return None
    results = resp.json().get("results", [])
    if not results:
        log(f"space_key={space_key} 에 해당하는 space 없음")
        return None
    return str(results[0]["id"])


def _get_page_version(session, base_url: str, page_id: str) -> int | None:
    resp = _request_with_retry(session, "GET", f"{base_url}/api/v2/pages/{page_id}")
    if resp is None or resp.status_code >= 400:
        return None
    return resp.json().get("version", {}).get("number")


def _stub_body(ns: str) -> str:
    return (
        f'<p>네임스페이스 <code>{ns}</code> 인덱스 페이지가 DokuWiki 에 없어 '
        f'마이그레이션 시 자동 생성됨.</p>'
    )


def _skipped_promote_body(doku_id: str, reason: str | None) -> str:
    return (
        f'<p>DokuWiki 의 <code>{doku_id}</code> 페이지가 비어있거나 응답이 '
        f'없어{f" ({reason})" if reason else ""} 마이그레이션 시 자동으로 '
        f'placeholder 페이지로 생성됨. 트리 구조 보존을 위해 만들어진 것이며 '
        f'필요 시 내용을 채우거나 자식 페이지들을 다른 부모로 옮길 수 있다.</p>'
    )


def _promote_skipped_pages_in_chain(conn: sqlite3.Connection) -> int:
    """
    SKIPPED 페이지가 *다른 페이지의 parent* 로 쓰이고 있으면 BFS 가
    그 chain 을 통과 못 한다 (root start 가 SKIPPED 이면 namespace
    start 전체가 reach 안 됨). 그런 SKIPPED 페이지를 stub placeholder
    로 자동 promote 해 트리 구조를 살린다.

    parent 로 쓰이지 않는 고립 SKIPPED 페이지는 그대로 둔다 (의도된
    skip 일 수도 있으므로).
    """
    # 어떤 이유로든 storage 가 없는 chain parent (SKIPPED, DISCOVERED,
    # RENDERED, FAILED) 를 모두 promote 한다. 자식이 있는 노드만 대상.
    rows = conn.execute(
        """
        SELECT DISTINCT p.doku_id, p.last_error
          FROM pages p
         WHERE p.status NOT IN ('CONVERTED', 'UPLOADED')
           AND p.doku_id IN (
                SELECT DISTINCT parent_doku_id
                  FROM pages
                 WHERE parent_doku_id IS NOT NULL
           )
        """
    ).fetchall()

    promoted = 0
    for doku_id, reason in rows:
        body = _skipped_promote_body(doku_id, reason)
        storage_path = STORAGE_DIR / Path(*doku_id.split(":")).with_suffix(".xml")
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(body, encoding="utf-8")
        conn.execute(
            """
            UPDATE pages
               SET status='CONVERTED', storage_path=?, content_hash=?,
                   last_error=NULL, converted_at=?, last_checked_at=?
             WHERE doku_id=?
            """,
            (
                str(storage_path),
                sha256_bytes(body.encode("utf-8")),
                now_iso(),
                now_iso(),
                doku_id,
            ),
        )
        promoted += 1
    conn.commit()
    return promoted


def _disambiguate_duplicate_titles(conn: sqlite3.Connection) -> int:
    """
    Confluence Cloud 는 공간 단위로 페이지 제목이 유일해야 한다 (400 에러).
    DokuWiki 의 일지/매일 페이지 트리(`u:lam:workhours:2019:12:05` 등) 는
    같은 title (`05`) 을 가진 형제가 많아 충돌 폭이 크다.

    Reactive disambiguation (`upload` 가 400 보면 한 번 재시도) 만으로는
    같은 title 그룹마다 PUT 두 배 + 첫 페이지가 plain title 을 차지하는
    BFS-순서 의존성이 생긴다. 여기서는 사전에 충돌이 예상되는 모든
    페이지에 일관된 형식의 suffix 를 부여한다:

        '<original_title> (<doku_id 의 마지막 segment 를 뺀 나머지>)'

    예) doku_id 'u:lam:workhours:2019:12:05' title '05'
        -> '05 (u:lam:workhours:2019:12)'

    return: 갱신된 페이지 수.
    """
    # 같은 disambiguator 로 또 겹치는 케이스(예: 'wiki:start' 와 'wiki:til'
    # 이 둘 다 title='#til' + ns_prefix='wiki') 가 있을 수 있으므로 충돌이
    # 사라질 때까지 반복.
    updated_total = 0
    for _iter in range(5):
        rows = conn.execute(
            """
            SELECT title FROM pages
             WHERE status='CONVERTED' AND title IS NOT NULL AND title != ''
             GROUP BY title HAVING COUNT(*) > 1
            """
        ).fetchall()
        if not rows:
            break
        for (title,) in rows:
            page_rows = conn.execute(
                "SELECT doku_id FROM pages WHERE title=? AND status='CONVERTED'",
                (title,),
            ).fetchall()
            for idx, (doku_id,) in enumerate(page_rows):
                parts = doku_id.split(":")
                if len(parts) >= 2:
                    # 첫 회: doku_id 의 namespace prefix 사용
                    # 후속 회 (또 충돌): doku_id 자체 append 로 강제 unique
                    base_suffix = ":".join(parts[:-1])
                    if base_suffix and f"({base_suffix})" not in title:
                        new_title = f"{title} ({base_suffix})"
                    else:
                        new_title = f"{title} [{doku_id}]"
                else:
                    new_title = f"{title} [{doku_id}]"
                conn.execute(
                    "UPDATE pages SET title=? WHERE doku_id=?",
                    (new_title, doku_id),
                )
                updated_total += 1
    conn.commit()
    return updated_total


def _ensure_namespace_stubs(conn: sqlite3.Connection) -> int:
    """모든 namespace 를 따라 누락된 <ns>:start 행을 STUB 으로 생성."""
    needed: set[str] = set()
    for (ns,) in conn.execute(
        "SELECT DISTINCT namespace FROM pages WHERE namespace != ''"
    ).fetchall():
        parts = ns.split(":")
        for i in range(1, len(parts) + 1):
            needed.add(":".join(parts[:i]))

    inserted = 0
    # shallow 우선으로 정렬: 'wiki' -> 'wiki:foo' 순서
    for ns in sorted(needed, key=lambda x: (x.count(":"), x)):
        stub_id = f"{ns}:start"
        if conn.execute("SELECT 1 FROM pages WHERE doku_id=?", (stub_id,)).fetchone():
            continue
        parent_ns = ":".join(ns.split(":")[:-1])
        parent_id = f"{parent_ns}:start" if parent_ns else "start"
        # 루트 start 가 없는 케이스: 부모를 NULL 로 (root_page_id 직속)
        if not conn.execute("SELECT 1 FROM pages WHERE doku_id=?", (parent_id,)).fetchone():
            parent_id = None
        title = ns.split(":")[-1].replace("_", " ") or ns
        body = _stub_body(ns)
        storage_path = STORAGE_DIR / Path(*stub_id.split(":")).with_suffix(".xml")
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(body, encoding="utf-8")
        conn.execute(
            """
            INSERT INTO pages(
                doku_id, src_path, namespace, parent_doku_id, is_namespace_index,
                title, storage_path, content_hash, status,
                discovered_at, converted_at, last_checked_at
            ) VALUES (?, '<stub>', ?, ?, 1, ?, ?, ?, 'CONVERTED', ?, ?, ?)
            """,
            (
                stub_id,
                ns,
                parent_id,
                title,
                str(storage_path),
                sha256_bytes(body.encode("utf-8")),
                now_iso(),
                now_iso(),
                now_iso(),
            ),
        )
        inserted += 1

    # 누락된 parent_doku_id 재연결: namespace != '' 인 페이지의 parent 가 NULL 이면
    # <namespace>:start 로 다시 매핑 (stub 이 방금 생겼을 수 있음).
    conn.execute(
        """
        UPDATE pages
           SET parent_doku_id = (namespace || ':start')
         WHERE parent_doku_id IS NULL
           AND namespace != ''
           AND doku_id != (namespace || ':start')
           AND (namespace || ':start') IN (SELECT doku_id FROM pages)
        """
    )
    conn.commit()
    return inserted


def _bfs_upload_order(conn: sqlite3.Connection) -> list[str]:
    """parent_doku_id 가 NULL 인 페이지(루트) 부터 BFS 정렬. STUB 포함."""
    children: dict[str | None, list[str]] = {}
    for doku_id, parent in conn.execute(
        "SELECT doku_id, parent_doku_id FROM pages "
        "WHERE status IN ('CONVERTED','UPLOADED','FAILED') "
        "ORDER BY doku_id"
    ).fetchall():
        children.setdefault(parent, []).append(doku_id)

    order: list[str] = []
    queue: list[str] = list(children.get(None, []))
    seen: set[str] = set()
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        order.append(cur)
        queue.extend(children.get(cur, []))
    return order


def _upload_attachments_for_page(
    conn: sqlite3.Connection,
    session,
    base_url: str,
    page_doku_id: str,
    confluence_page_id: str,
    dry_run: bool,
) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT media_id, src_path, size FROM attachments "
        "WHERE page_doku_id=? AND status='DISCOVERED'",
        (page_doku_id,),
    ).fetchall()
    if not rows:
        return 0, 0

    from requests_toolbelt.multipart import encoder as tb_encoder

    ok = fail = 0
    for media_id, src_path, size in rows:
        filename = _media_filename(media_id)
        if not src_path or not Path(src_path).is_file():
            conn.execute(
                "UPDATE attachments SET status='FAILED', last_error='src missing' "
                "WHERE page_doku_id=? AND media_id=?",
                (page_doku_id, media_id),
            )
            conn.commit()
            fail += 1
            continue
        if size and size > MAX_ATTACHMENT_BYTES:
            conn.execute(
                "UPDATE attachments SET status='OVERSIZED', last_error='exceeds 100MB' "
                "WHERE page_doku_id=? AND media_id=?",
                (page_doku_id, media_id),
            )
            conn.commit()
            fail += 1
            continue

        if dry_run:
            log(f"    [DRY ATTACH] {page_doku_id}: {filename} ({size} bytes)")
            ok += 1
            continue

        url = f"{base_url}/rest/api/content/{confluence_page_id}/child/attachment"
        try:
            # _request_with_retry 우회 의도: multipart streaming body — retry 시
            # 파일 핸들 재사용 불가. 같은 파일명 충돌은 200 으로 처리 (호출자
            # 측에서 UPLOADED 로 마킹).
            with open(src_path, "rb") as fp:
                m = tb_encoder.MultipartEncoder(
                    fields={"file": (filename, fp, "application/octet-stream")}
                )
                resp = session.post(
                    url,
                    headers={
                        "X-Atlassian-Token": "no-check",
                        "Content-Type": m.content_type,
                    },
                    data=m,
                    timeout=600,
                )
        except Exception as e:  # noqa: BLE001
            conn.execute(
                "UPDATE attachments SET status='FAILED', last_error=? "
                "WHERE page_doku_id=? AND media_id=?",
                (str(e), page_doku_id, media_id),
            )
            conn.commit()
            fail += 1
            continue

        if resp.status_code >= 400:
            text = resp.text or ""
            # upload_to_confluence 의 관례: 같은 파일명이 이미 있는 경우 UPLOADED 로 처리
            if resp.status_code == 400 and "same file name as an existing attachment" in text:
                conn.execute(
                    "UPDATE attachments SET status='UPLOADED', last_error=NULL, "
                    "confluence_page_id=?, uploaded_at=? WHERE page_doku_id=? AND media_id=?",
                    (confluence_page_id, now_iso(), page_doku_id, media_id),
                )
                conn.commit()
                ok += 1
                continue
            conn.execute(
                "UPDATE attachments SET status='FAILED', last_error=? "
                "WHERE page_doku_id=? AND media_id=?",
                (f"attach {resp.status_code}: {text[:300]}", page_doku_id, media_id),
            )
            conn.commit()
            fail += 1
            continue

        att_id = None
        try:
            data = resp.json()
            results = data.get("results", []) if isinstance(data, dict) else []
            if results:
                att_id = str(results[0].get("id"))
        except ValueError:
            pass
        conn.execute(
            "UPDATE attachments SET status='UPLOADED', confluence_attachment_id=?, "
            "confluence_page_id=?, uploaded_at=?, last_error=NULL "
            "WHERE page_doku_id=? AND media_id=?",
            (att_id, confluence_page_id, now_iso(), page_doku_id, media_id),
        )
        conn.commit()
        ok += 1
    return ok, fail


def _upload_validate_args(args: argparse.Namespace) -> list[str]:
    """cmd_upload 의 인자 검증 — 누락 항목 메시지 리스트 반환. 빈 리스트면 OK."""
    missing = []
    if not args.space_key:
        missing.append("--space-key (또는 환경변수 CONFLUENCE_SPACE_KEY) — 대상 Confluence 공간의 키")
    if not args.root_page_id:
        missing.append("--root-page-id (또는 환경변수 CONFLUENCE_ROOT_PAGE_ID) — 마이그레이션 트리의 루트가 될 부모 페이지 ID")
    if not args.dry_run:
        if not args.email:
            missing.append("--email / 환경변수 CONFLUENCE_EMAIL")
        if not args.api_token:
            missing.append("--api-token / 환경변수 CONFLUENCE_API_TOKEN (https://id.atlassian.com/manage-profile/security/api-tokens 에서 생성)")
    return missing


def _upload_prepare_namespace(conn: sqlite3.Connection) -> None:
    """업로드 전 네임스페이스 정리 — SKIPPED chain promote / stub / title disambig.

    각 함수의 결과 개수를 로깅. 동작 없음 (count 0) 이면 조용.
    """
    promoted = _promote_skipped_pages_in_chain(conn)
    if promoted:
        log(f"SKIPPED → placeholder 자동 promote: {promoted}개 (chain 부모로 쓰이던 페이지)")
    stub_count = _ensure_namespace_stubs(conn)
    if stub_count:
        log(f"네임스페이스 stub {stub_count}개 자동 생성")
    dup_count = _disambiguate_duplicate_titles(conn)
    if dup_count:
        log(f"중복 title 사전 disambiguation: {dup_count}개 (Confluence per-space unique 제약)")


def _upload_select_targets(
    conn: sqlite3.Connection, only: str | None, include_parents: bool, limit: int | None,
) -> list[str]:
    """BFS upload 순서 + --only/--include-parents/--limit 필터 적용."""
    order = _bfs_upload_order(conn)
    if only:
        # only 지정 시 부모 chain 도 함께 포함하지 않으면 SKIP 되므로,
        # --include-parents 또는 단일 페이지 의도 분기.
        selected = {only}
        if include_parents:
            cur = only
            while cur:
                row = conn.execute(
                    "SELECT parent_doku_id FROM pages WHERE doku_id=?", (cur,)
                ).fetchone()
                cur = row[0] if row else None
                if cur:
                    selected.add(cur)
        order = [d for d in order if d in selected]
    if limit:
        order = order[: limit]
    return order


def cmd_upload(args: argparse.Namespace) -> int:
    """S4~S6: CONVERTED 페이지 + 첨부를 Confluence 에 PUT/POST.

    부모-자식 BFS 로 순회 — 자식 페이지 POST 전에 부모 페이지 보장. 멱등
    short-circuit: 같은 content_hash 재발행 안 함. 첨부는 v1 multipart
    (같은 파일명이면 새 버전). state.db 갱신: `pages.confluence_page_id /
    confluence_version / uploaded_at`, status='UPLOADED'. 첨부도 같은
    필드. `--dry-run` 으로 호출 그래프만 검증 가능. 400 (title 충돌) 시
    자동 disambiguate.
    """
    missing = _upload_validate_args(args)
    if missing:
        log("upload 호출에 필요한 항목이 누락되었습니다:")
        for m in missing:
            log(f"  - {m}")
        log("dry-run 만으로 점검하려면 `--dry-run` 추가 시 인증은 생략 가능.")
        return 2

    STORAGE_DIR.mkdir(exist_ok=True)
    conn = db_connect(args.db)
    db_init(conn)
    db_set_meta(conn, "confluence_base_url", args.base_url.rstrip("/"))
    db_set_meta(conn, "confluence_space_key", args.space_key)
    db_set_meta(conn, "confluence_root_page_id", args.root_page_id)

    _upload_prepare_namespace(conn)

    base_url = args.base_url.rstrip("/")
    session = None
    space_id = "<dry-run-space>"
    if not args.dry_run:
        session = _confluence_session(args)
        if session is None:
            return 2
        space_id = _resolve_space_id(session, base_url, args.space_key)
        if not space_id:
            return 1

    order = _upload_select_targets(conn, args.only, args.include_parents, args.limit)

    log(f"upload 대상: {len(order)} 페이지 (space_id={space_id}, root={args.root_page_id})")

    created = updated = skipped = failed = 0
    att_ok = att_fail = 0

    for doku_id in order:
        row = conn.execute(
            "SELECT title, parent_doku_id, storage_path, content_hash, "
            "       confluence_page_id, confluence_version "
            "FROM pages WHERE doku_id=?",
            (doku_id,),
        ).fetchone()
        if not row:
            continue
        title, parent_doku_id, storage_path, content_hash, confluence_page_id, _ = row

        # 부모 Confluence id 결정
        if parent_doku_id:
            prow = conn.execute(
                "SELECT confluence_page_id FROM pages WHERE doku_id=?", (parent_doku_id,)
            ).fetchone()
            parent_page_id = prow[0] if prow and prow[0] else None
            if not parent_page_id and not args.dry_run:
                log(f"  [SKIP] {doku_id}: 부모 {parent_doku_id} 가 아직 업로드되지 않음")
                skipped += 1
                continue
            if not parent_page_id and args.dry_run:
                parent_page_id = f"<pending:{parent_doku_id}>"
        else:
            parent_page_id = args.root_page_id

        if not storage_path or not Path(storage_path).is_file():
            log(f"  [FAIL] {doku_id}: storage 파일 없음")
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error='storage missing', "
                "last_checked_at=? WHERE doku_id=?",
                (now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue

        body_xml = Path(storage_path).read_text(encoding="utf-8")
        body_obj = {"representation": "storage", "value": body_xml}
        page_title = title or doku_id

        if confluence_page_id is None:
            payload = {
                "spaceId": space_id,
                "parentId": parent_page_id,
                "title": page_title,
                "body": body_obj,
            }
            if args.dry_run:
                log(f"  [DRY CREATE] {doku_id} title={page_title!r} parent={parent_page_id}")
                created += 1
                # fall-through 하여 첨부 dry-attach 블록도 보여줌. confluence_page_id 는 None 유지.
            else:
                resp = _request_with_retry(
                    session, "POST", f"{base_url}/api/v2/pages", json=payload
                )
                err = None
                if resp is None or resp.status_code >= 400:
                    err = f"create {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:300]}"
                    # 제목 충돌이면 doku_id 접미로 1회 재시도
                    if (
                        resp is not None
                        and resp.status_code == 400
                        and "title" in (resp.text or "").lower()
                    ):
                        payload["title"] = f"{page_title} ({doku_id})"
                        resp = _request_with_retry(
                            session, "POST", f"{base_url}/api/v2/pages", json=payload
                        )
                if resp is None or resp.status_code >= 400:
                    log(f"  [FAIL] {doku_id}: {err}")
                    conn.execute(
                        "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? "
                        "WHERE doku_id=?",
                        (err, now_iso(), doku_id),
                    )
                    conn.commit()
                    failed += 1
                    continue
                page_title = payload["title"]
                data = resp.json()
                new_id = str(data["id"])
                new_ver = int(data.get("version", {}).get("number", 1))
                conn.execute(
                    "UPDATE pages SET confluence_page_id=?, confluence_version=?, title=?, "
                    "status='UPLOADED', last_error=NULL, uploaded_at=?, last_checked_at=? "
                    "WHERE doku_id=?",
                    (new_id, new_ver, page_title, now_iso(), now_iso(), doku_id),
                )
                db_set_meta(conn, f"uploaded_hash:{doku_id}", content_hash or "")
                conn.commit()
                confluence_page_id = new_id
                created += 1
                log(f"  [CREATED] {doku_id} -> page {new_id}")
        else:
            # large body fallback 적용된 페이지는 본문 PUT 을 영구히 skip
            # (storage 가 너무 커서 Confluence 가 거부). 첨부만 업로드.
            if db_get_meta(conn, f"large_body_fallback:{doku_id}"):
                log(f"  [SKIP] {doku_id}: large body fallback 적용됨 — 본문 PUT 생략, 첨부만 처리")
                skipped += 1
            elif (prev_hash := db_get_meta(conn, f"uploaded_hash:{doku_id}")) == content_hash:
                log(f"  [SKIP] {doku_id}: content_hash 변경 없음")
                skipped += 1
            else:
                if args.dry_run:
                    log(f"  [DRY UPDATE] {doku_id} confluence_id={confluence_page_id}")
                    updated += 1
                else:
                    cur_ver = _get_page_version(session, base_url, confluence_page_id)
                    if cur_ver is None:
                        log(f"  [FAIL] {doku_id}: 현재 버전 조회 실패")
                        failed += 1
                        continue
                    next_ver = cur_ver + 1
                    payload = {
                        "id": confluence_page_id,
                        "status": "current",
                        "title": page_title,
                        "body": body_obj,
                        "version": {"number": next_ver},
                    }
                    resp = _request_with_retry(
                        session, "PUT", f"{base_url}/api/v2/pages/{confluence_page_id}",
                        json=payload,
                    )
                    if resp is None or resp.status_code >= 400:
                        err = f"update {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:300]}"
                        log(f"  [FAIL] {doku_id}: {err}")
                        conn.execute(
                            "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? "
                            "WHERE doku_id=?",
                            (err, now_iso(), doku_id),
                        )
                        conn.commit()
                        failed += 1
                        continue
                    conn.execute(
                        "UPDATE pages SET confluence_version=?, status='UPLOADED', "
                        "last_error=NULL, uploaded_at=?, last_checked_at=? WHERE doku_id=?",
                        (next_ver, now_iso(), now_iso(), doku_id),
                    )
                    db_set_meta(conn, f"uploaded_hash:{doku_id}", content_hash or "")
                    conn.commit()
                    updated += 1
                    log(f"  [UPDATED] {doku_id} -> v{next_ver}")

        # tag → Confluence page label 적용 (dry-run 도 건너뜀)
        if not args.dry_run and confluence_page_id:
            tags_meta = db_get_meta(conn, f"page_tags:{doku_id}")
            if tags_meta:
                tags = [t for t in tags_meta.splitlines() if t.strip()]
                if tags:
                    _apply_page_labels(session, base_url, str(confluence_page_id), tags)

        # 첨부 업로드 (CREATED 또는 기존 UPLOADED 페이지 모두 대상)
        if not args.dry_run and confluence_page_id:
            o, f = _upload_attachments_for_page(
                conn, session, base_url, doku_id, str(confluence_page_id), dry_run=False
            )
            att_ok += o
            att_fail += f
        elif args.dry_run:
            o, f = _upload_attachments_for_page(
                conn, None, base_url, doku_id, "<dry>", dry_run=True
            )
            att_ok += o
            att_fail += f

    log(f"upload 완료: created={created} updated={updated} skipped={skipped} failed={failed}")
    log(f"  첨부: ok={att_ok} failed/oversized={att_fail}")
    conn.close()
    return 0 if failed == 0 else 1


# § S7: Rewrite links

LINK_BODY_SENTINEL_PREFIX = "__DWC_LINK_BODY_"


def _rewrite_links_in_xml(
    conn: sqlite3.Connection,
    src_doku_id: str,
    xml: str,
) -> tuple[str, list[str], list[str]]:
    """
    storage XML 안의 dwc-link:<target>[#anchor] placeholder 를 실제
    <ac:link><ri:page ri:content-title=...> 로 치환한다.

    반환: (new_xml, resolved_placeholders, unresolved_placeholders).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(xml, "html.parser")
    resolved: list[str] = []
    unresolved: list[str] = []
    link_body_texts: dict[str, str] = {}

    for idx, a in enumerate(list(soup.find_all("a"))):
        href = a.get("href", "")
        if not href.startswith(f"{DOKU_LINK_SCHEME}:"):
            continue

        rest = href[len(DOKU_LINK_SCHEME) + 1 :]
        if "#" in rest:
            target_id, anchor = rest.split("#", 1)
        else:
            target_id, anchor = rest, None

        target_row = conn.execute(
            "SELECT title, confluence_page_id, status FROM pages WHERE doku_id=?",
            (target_id,),
        ).fetchone()

        link_text = a.get_text() or target_id

        if not target_row or (target_row[2] not in ("UPLOADED", "CONVERTED")):
            # 미해결: 일반 텍스트로 격하
            unresolved.append(href)
            replacement_text = link_text
            a.replace_with(replacement_text)
            continue

        target_title = target_row[0] or target_id

        ac_link = soup.new_tag("ac:link")
        if anchor:
            ac_link["ac:anchor"] = anchor
        ri_page = soup.new_tag("ri:page")
        ri_page["ri:content-title"] = target_title
        ac_link.append(ri_page)
        body = soup.new_tag("ac:plain-text-link-body")
        sentinel = f"{LINK_BODY_SENTINEL_PREFIX}{idx}__"
        body.string = sentinel
        link_body_texts[sentinel] = link_text
        ac_link.append(body)
        a.replace_with(ac_link)
        resolved.append(href)

    result = "".join(str(c) for c in soup.children)

    result = _re.sub(r"<(br|hr|img)([^>]*?)(?<!/)\s*>", r"<\1\2/>", result)

    for sentinel, text in link_body_texts.items():
        safe = text.replace("]]>", "]]]]><![CDATA[>")
        result = result.replace(sentinel, f"<![CDATA[{safe}]]>")

    return result, resolved, unresolved


# return statement above belongs to _rewrite_links_in_xml — _convert_html_to_storage
# 의 마지막 return 도 tags 포함하도록 별도 갱신.


def cmd_rewrite_links(args: argparse.Namespace) -> int:
    """S7: storage XML 안의 `dwc-link:<doku_id>` placeholder 를 `<ac:link>
    <ri:page>` 매크로로 치환 (2-pass).

    1-pass: 모든 페이지가 confluence_page_id 를 가질 때까지 대기 (upload
    완료 후). 2-pass: 본문 PUT 으로 placeholder → 실제 링크. unresolved
    page (doku_id 가 state.db 에 없거나 미업로드) 는 평문으로 격하 (시각
    경고 + `dwc-unresolved-page` class).
    state.db 갱신: 본문 PUT 시 confluence_version 증가. 멱등 — 이미
    placeholder 가 없는 페이지는 SKIP.
    """
    try:
        import bs4  # noqa: F401
    except ImportError:
        log("beautifulsoup4 가 필요합니다: pip install -r requirements.txt")
        return 2

    if not args.dry_run and (not args.email or not args.api_token):
        log("rewrite-links (라이브 모드) 자격증명 누락:")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        log("storage XML 만 재작성 (Confluence 갱신 없이) 하려면 `--dry-run` 사용.")
        return 2

    conn = db_connect(args.db)
    db_init(conn)

    where = (
        "p.status IN ('UPLOADED','CONVERTED') "
        "AND p.storage_path IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM links l WHERE l.src_doku_id=p.doku_id AND l.resolved=0)"
    )
    params: tuple = ()
    if args.only:
        where = "p.doku_id=? AND p.storage_path IS NOT NULL"
        params = (args.only,)

    rows = conn.execute(
        f"SELECT p.doku_id, p.storage_path, p.content_hash, p.confluence_page_id, p.title "
        f"FROM pages p WHERE {where} ORDER BY p.doku_id",
        params,
    ).fetchall()

    log(f"rewrite-links 대상: {len(rows)} 페이지")

    session = None
    base_url = args.base_url.rstrip("/") if getattr(args, "base_url", None) else ""
    if not args.dry_run and rows:
        # 재업로드가 필요한 경우에만 인증 세션 구성
        needs_upload = any(r[3] for r in rows)
        if needs_upload:
            session = _confluence_session(args)
            if session is None:
                return 2

    rewritten = updated_on_confluence = no_change = not_uploaded = failed = 0
    total_resolved = total_unresolved = 0

    for doku_id, storage_path, old_hash, confluence_page_id, title in rows:
        sp = Path(storage_path)
        if not sp.is_file():
            log(f"  [FAIL] {doku_id}: storage 파일 없음 ({storage_path})")
            failed += 1
            continue

        xml = sp.read_text(encoding="utf-8")
        new_xml, resolved_phs, unresolved_phs = _rewrite_links_in_xml(conn, doku_id, xml)
        total_resolved += len(resolved_phs)
        total_unresolved += len(unresolved_phs)

        new_hash = sha256_bytes(new_xml.encode("utf-8"))
        # storage 가 안 바뀌었어도 Confluence 에 PUT 안 된 상태일 수 있음
        # (이전 dry-run 이 디스크 + content_hash 만 갱신하고 PUT skip 한 경우).
        # uploaded_hash 메타와 다르면 PUT 필요.
        uploaded_hash = db_get_meta(conn, f"uploaded_hash:{doku_id}") or ""
        needs_push = (new_hash != uploaded_hash) and bool(confluence_page_id)
        if new_hash == old_hash and not needs_push:
            no_change += 1
            # 그래도 links 테이블의 resolved 플래그는 갱신
            for ph in resolved_phs:
                conn.execute(
                    "UPDATE links SET resolved=1 WHERE src_doku_id=? AND placeholder=?",
                    (doku_id, ph),
                )
            conn.commit()
            continue

        # 디스크에 새 storage 쓰고 hash 갱신
        sp.write_text(new_xml, encoding="utf-8")
        conn.execute(
            "UPDATE pages SET content_hash=?, last_checked_at=? WHERE doku_id=?",
            (new_hash, now_iso(), doku_id),
        )
        for ph in resolved_phs:
            conn.execute(
                "UPDATE links SET resolved=1 WHERE src_doku_id=? AND placeholder=?",
                (doku_id, ph),
            )
        conn.commit()
        rewritten += 1

        if unresolved_phs:
            log(f"  [{doku_id}] 미해결 링크 {len(unresolved_phs)} 개 → 일반 텍스트로 격하")

        if not confluence_page_id:
            log(f"  [LOCAL] {doku_id}: 아직 업로드되지 않음 — storage 만 갱신 (다음 upload 호출이 반영)")
            not_uploaded += 1
            continue

        if args.dry_run:
            log(f"  [DRY UPDATE] {doku_id} confluence_id={confluence_page_id} 해결={len(resolved_phs)}")
            continue

        cur_ver = _get_page_version(session, base_url, confluence_page_id)
        if cur_ver is None:
            log(f"  [FAIL] {doku_id}: 현재 버전 조회 실패")
            failed += 1
            continue
        next_ver = cur_ver + 1
        payload = {
            "id": confluence_page_id,
            "status": "current",
            "title": title or doku_id,
            "body": {"representation": "storage", "value": new_xml},
            "version": {"number": next_ver},
        }
        resp = _request_with_retry(
            session, "PUT", f"{base_url}/api/v2/pages/{confluence_page_id}", json=payload
        )
        if resp is None or resp.status_code >= 400:
            err = f"update {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:300]}"
            log(f"  [FAIL] {doku_id}: {err}")
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error=?, last_checked_at=? WHERE doku_id=?",
                (err, now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
            continue
        conn.execute(
            "UPDATE pages SET confluence_version=?, status='UPLOADED', last_error=NULL, "
            "uploaded_at=?, last_checked_at=? WHERE doku_id=?",
            (next_ver, now_iso(), now_iso(), doku_id),
        )
        db_set_meta(conn, f"uploaded_hash:{doku_id}", new_hash)
        conn.commit()
        updated_on_confluence += 1
        log(f"  [REWRITTEN] {doku_id} -> v{next_ver}")

    log(
        f"rewrite-links 완료: rewritten={rewritten} "
        f"pushed={updated_on_confluence} no-change={no_change} "
        f"local-only={not_uploaded} failed={failed}"
    )
    log(f"  링크 해결={total_resolved} 미해결={total_unresolved}")
    conn.close()
    return 0 if failed == 0 else 1


# § history: discover

def cmd_history_discover(args: argparse.Namespace) -> int:
    """attic/ + meta/*.changes + media_attic/ 인덱싱. 라이브 호출 없음.

    attic 의 .txt.gz 한 파일이 한 리비전. meta/.../<page>.changes 는 TSV
    (ts, ip, type, page, user, comment, extra). 두 소스를 교차해
    revisions 테이블 채움. media_attic 은 별도 media_revisions.
    """
    conn = db_connect(args.db)
    db_init(conn)
    src = db_get_meta(conn, "dokuwiki_src")
    if not src:
        log("dokuwiki_src 메타 없음 — 먼저 discover 실행.")
        return 2

    src_root = Path(src)
    attic = src_root / "attic"
    meta = src_root / "meta"
    media_attic = src_root / "media_attic"

    if not attic.is_dir():
        log(f"attic 디렉터리 없음: {attic}")
        return 1

    # 1) attic 파일 인덱싱 -> revisions
    log("attic 인덱싱 중…")
    n_files = 0
    decode_replaced = 0
    import gzip
    rev_pattern = _re.compile(r"\.(\d{8,12})\.txt\.gz$")
    page_meta: dict[str, list[int]] = {}  # doku_id -> [ts, ts, ...]

    for f in attic.rglob("*.txt.gz"):
        m = rev_pattern.search(f.name)
        if not m:
            continue
        try:
            ts = int(m.group(1))
        except ValueError:
            continue
        # doku_id: relative path 에서 (basename - .<ts>.txt.gz) + 디렉터리는 :
        rel = f.relative_to(attic)
        parts = list(rel.parts)
        last = parts[-1]
        base = last[: -len(m.group(0))]  # strip '.<ts>.txt.gz'
        parts[-1] = base
        doku_id = ":".join(parts)

        # 디코딩 깨짐 확인. gzip 손상도 일부 attic 에서 발견됨.
        try:
            with gzip.open(f, "rb") as g:
                raw_bytes = g.read()
        except (OSError, EOFError, Exception) as e:
            # zlib.error / EOFError / 기타 gzip 손상
            if "zlib" in type(e).__module__ or isinstance(e, (OSError, EOFError)):
                conn.execute(
                    "INSERT OR REPLACE INTO revisions(doku_id, rev_ts, attic_path, status, last_error, last_checked_at) "
                    "VALUES (?, ?, ?, 'FAILED', ?, ?)",
                    (None or "", ts, str(f), f"gzip read: {e!r}", now_iso()),
                )
                continue
            raise
        try:
            raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            if "�" in raw_bytes.decode("utf-8", errors="replace"):
                decode_replaced += 1

        conn.execute(
            "INSERT OR REPLACE INTO revisions(doku_id, rev_ts, attic_path, status, last_checked_at) "
            "VALUES (?, ?, ?, 'DISCOVERED', ?)",
            (doku_id, ts, str(f), now_iso()),
        )
        page_meta.setdefault(doku_id, []).append(ts)
        n_files += 1

    conn.commit()
    log(f"attic: {n_files} revisions ({decode_replaced} 디코딩 깨짐 후보)")

    # 2) meta/*.changes 파싱 -> revisions 의 type/user/ip/comment 채움
    log("changes 로그 파싱 중…")
    n_changes = 0
    for f in meta.rglob("*.changes"):
        if f.name.startswith("_"):  # _dokuwiki.changes, _media.changes 같은 글로벌 로그
            continue
        rel = f.relative_to(meta)
        parts = list(rel.parts)
        last = parts[-1]
        if not last.endswith(".changes"):
            continue
        parts[-1] = last[:-len(".changes")]
        doku_id = ":".join(parts)
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                cols = line.split("\t")
                if len(cols) < 5:
                    continue
                ts_s, ip, ctype, page_id, user = cols[:5]
                comment = cols[5] if len(cols) > 5 else ""
                extra = cols[6] if len(cols) > 6 else ""
                try:
                    ts = int(ts_s)
                except ValueError:
                    continue
                # 기존 revisions 행에 메타 보강 (없으면 INSERT)
                conn.execute(
                    """
                    INSERT INTO revisions(doku_id, rev_ts, type, user, ip, comment, extra, status, last_checked_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?)
                    ON CONFLICT(doku_id, rev_ts) DO UPDATE SET
                         type=excluded.type, user=excluded.user, ip=excluded.ip,
                         comment=excluded.comment, extra=excluded.extra
                    """,
                    (doku_id, ts, ctype, user, ip, comment, extra, now_iso()),
                )
                n_changes += 1
        except OSError:
            continue
    conn.commit()
    log(f"changes 로그: {n_changes} entry 적용")

    # 3) history_meta 집계
    for doku_id, ts_list in page_meta.items():
        ts_sorted = sorted(ts_list)
        conn.execute(
            "INSERT OR REPLACE INTO history_meta(doku_id, total_revs, first_ts, last_ts) "
            "VALUES (?, ?, ?, ?)",
            (doku_id, len(ts_list), ts_sorted[0], ts_sorted[-1]),
        )
    conn.commit()

    # 4) media_attic 인덱싱
    log("media_attic 인덱싱 중…")
    n_media = 0
    if media_attic.is_dir():
        media_rev_pattern = _re.compile(r"\.(\d{8,12})(\..+)?$")
        for f in media_attic.rglob("*"):
            if not f.is_file():
                continue
            m = media_rev_pattern.search(f.name)
            if not m:
                continue
            try:
                ts = int(m.group(1))
            except ValueError:
                continue
            ext = m.group(2) or ""
            # media_id: rel path 에서 .<ts><.ext> 제거
            rel = f.relative_to(media_attic)
            parts = list(rel.parts)
            last = parts[-1]
            base = last[: -len(m.group(0))] + ext
            parts[-1] = base
            media_id = ":".join(parts)
            try:
                size = f.stat().st_size
                sha = sha256_file(f)
            except OSError:
                size, sha = None, None
            conn.execute(
                "INSERT OR REPLACE INTO media_revisions(media_id, rev_ts, src_path, size, sha256, status) "
                "VALUES (?, ?, ?, ?, ?, 'DISCOVERED')",
                (media_id, ts, str(f), size, sha),
            )
            n_media += 1
    conn.commit()

    log(f"history-discover 완료: revisions={n_files}, changes={n_changes}, media={n_media}, decode_replaced={decode_replaced}")
    # 요약
    n_pages = conn.execute("SELECT COUNT(*) FROM history_meta").fetchone()[0]
    log(f"  unique pages: {n_pages}, avg revs/page: {n_files / max(n_pages,1):.1f}")
    conn.close()
    return 0


HISTORY_RAW_DIR = Path("raw_history")
HISTORY_STORAGE_DIR = Path("storage_history")


def cmd_history_render(args: argparse.Namespace) -> int:
    """attic 의 각 리비전을 dokuwiki ?rev=<ts> 로 받아 캐시.

    revisions 테이블에서 status='DISCOVERED' 인 행만 처리. raw_history/
    <doku_id 의 :를 />.<ts>.html 로 저장.
    """
    try:
        import requests
    except ImportError:
        log("requests 필요: pip install -r requirements.txt")
        return 2

    conn = db_connect(args.db)
    base_url = args.base_url.rstrip("/")
    if not base_url:
        log("--base-url 필요 (DOKUWIKI_BASE_URL).")
        return 2

    HISTORY_RAW_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    if args.user and args.password:
        # _request_with_retry 우회 의도: DokuWiki form 로그인 (cmd_render 와 동일
        # 패턴). 실패 시 retry 무의미 — 자격증명 오타.
        session.post(
            f"{base_url}/doku.php",
            data={"do": "login", "u": args.user, "p": args.password},
            timeout=30, allow_redirects=True,
        )

    where = "status='DISCOVERED'" if not args.force else "1=1"
    params: tuple = ()
    if args.only:
        where = "doku_id=?"
        params = (args.only,)

    rows = conn.execute(
        f"SELECT doku_id, rev_ts FROM revisions WHERE {where} ORDER BY doku_id, rev_ts",
        params,
    ).fetchall()
    log(f"history-render 대상: {len(rows)} 리비전")

    ok = empty = failed = 0
    for i, (doku_id, rev_ts) in enumerate(rows, 1):
        url = f"{base_url}/doku.php"
        try:
            # _request_with_retry 우회 의도: cmd_render 동일 패턴 — DokuWiki
            # 로컬 호출, 실패 시 1건만 FAILED 로 두고 다음 진행.
            resp = session.get(
                url,
                params={"id": doku_id, "rev": rev_ts, "do": "export_xhtmlbody"},
                timeout=60,
            )
        except requests.RequestException as e:
            conn.execute(
                "UPDATE revisions SET status='FAILED', last_error=?, last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (str(e), now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            failed += 1
            continue

        if resp.status_code == 404 or not resp.text.strip():
            conn.execute(
                "UPDATE revisions SET status='SKIPPED', last_error='empty/404', last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            empty += 1
            continue

        if resp.status_code >= 400:
            conn.execute(
                "UPDATE revisions SET status='FAILED', last_error=?, last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (f"HTTP {resp.status_code}", now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            failed += 1
            continue

        rel = Path(*doku_id.split(":"))
        cache_path = HISTORY_RAW_DIR / rel.parent / f"{rel.name}.{rev_ts}.html"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(resp.text, encoding="utf-8")
        conn.execute(
            "UPDATE revisions SET raw_xhtml_path=?, status='RENDERED', "
            "last_error=NULL, last_checked_at=? WHERE doku_id=? AND rev_ts=?",
            (str(cache_path), now_iso(), doku_id, rev_ts),
        )
        conn.commit()
        ok += 1

        if args.delay:
            time.sleep(args.delay)
        if args.limit and ok >= args.limit:
            log(f"--limit {args.limit} 도달")
            break

    log(f"history-render 완료: ok={ok} empty={empty} failed={failed}")
    conn.close()
    return 0


REVISION_HEADER_FORMATS = ("none", "panel", "info", "note", "tip", "warning",
                            "quote", "table", "paragraphs")
REVISION_HEADER_DEFAULT = "panel"


def _revision_header(
    rev_ts: int, user: str | None, comment: str | None,
    type_code: str | None, users_map: dict[str, str],
    *,
    fmt: str = REVISION_HEADER_DEFAULT,
) -> str:
    """각 revision body 최상단에 박을 메타 헤더 (history-migration §6.4).

    fmt:
      - 'none'       헤더 박스 생략 (본문만)
      - 'panel'      panel 매크로 + 불릿 리스트 3 항목 (기본)
      - 'info'/'note'/'tip'/'warning'  같은 모양, 매크로 종류만 변경
      - 'quote'      <blockquote> 안에 3줄 (shift+enter)
      - 'table'      2열 표 (라벨/값 × 3행)
      - 'paragraphs' 기존 동작 — 3 개의 <p> 로 분리 (이전 호환용)
    """
    from datetime import datetime, timezone
    if fmt == "none":
        return ""

    dt = datetime.fromtimestamp(rev_ts, tz=timezone.utc).isoformat(timespec="seconds")
    user_repr = _format_user(user, users_map) if user else "(unknown)"
    type_label = {
        "C": "create", "E": "edit", "e": "minor edit",
        "R": "revert", "D": "delete",
    }.get(type_code or "", type_code or "?")
    comment_h = _h.escape(comment or "") if comment else "(no comment)"

    if fmt == "paragraphs":
        return (
            '<ac:structured-macro ac:name="note">'
            '<ac:rich-text-body>'
            f'<p>DokuWiki revision: <code>{dt}</code> ({type_label})</p>'
            f'<p>Author: {user_repr}</p>'
            f'<p>Comment: <code>{comment_h}</code></p>'
            '</ac:rich-text-body>'
            '</ac:structured-macro>'
        )

    if fmt == "quote":
        return (
            '<blockquote><p>'
            f'DokuWiki revision: <code>{dt}</code> ({type_label})<br/>'
            f'Author: {user_repr}<br/>'
            f'Comment: <code>{comment_h}</code>'
            '</p></blockquote>'
        )

    if fmt == "table":
        return (
            '<table>'
            f'<tr><th>DokuWiki revision</th><td><code>{dt}</code> ({type_label})</td></tr>'
            f'<tr><th>Author</th><td>{user_repr}</td></tr>'
            f'<tr><th>Comment</th><td><code>{comment_h}</code></td></tr>'
            '</table>'
        )

    # panel / info / note / tip / warning — 매크로 + 불릿 리스트 3 항목
    macro_name = fmt if fmt in ("panel", "info", "note", "tip", "warning") else "panel"
    return (
        f'<ac:structured-macro ac:name="{macro_name}">'
        '<ac:rich-text-body>'
        '<ul>'
        f'<li>DokuWiki revision: <code>{dt}</code> ({type_label})</li>'
        f'<li>Author: {user_repr}</li>'
        f'<li>Comment: <code>{comment_h}</code></li>'
        '</ul>'
        '</ac:rich-text-body>'
        '</ac:structured-macro>'
    )


def cmd_history_convert(args: argparse.Namespace) -> int:
    """raw_history/*.html 을 storage XML 로 변환. 본문 최상단에 §6.4 헤더 박스.

    content_hash 가 직전 revision 과 같으면 status='SKIPPED' (revert
    중복 본문 — Confluence 가 어차피 동일 body PUT 거부).
    """
    try:
        import bs4  # noqa: F401
    except ImportError:
        log("beautifulsoup4 필요")
        return 2

    conn = db_connect(args.db)
    src = db_get_meta(conn, "dokuwiki_src")
    if not src:
        log("dokuwiki_src 메타 없음 — discover 먼저")
        return 2
    src_root = Path(src)
    HISTORY_STORAGE_DIR.mkdir(exist_ok=True)

    # 헤더 형식 — CLI --header-format 우선, 없으면 meta 'revision_header_fmt', 없으면 기본값
    header_fmt = (getattr(args, "header_format", None)
                  or db_get_meta(conn, "revision_header_fmt")
                  or REVISION_HEADER_DEFAULT)
    if header_fmt not in REVISION_HEADER_FORMATS:
        log(f"알 수 없는 header-format: {header_fmt} (가능: {', '.join(REVISION_HEADER_FORMATS)})")
        return 2
    if getattr(args, "header_format", None):
        db_set_meta(conn, "revision_header_fmt", header_fmt)
    log(f"revision 헤더 형식: {header_fmt}")

    where = "status='RENDERED'" if not args.force else "status IN ('RENDERED','CONVERTED','FAILED')"
    params: tuple = ()
    if args.only:
        where = "doku_id=? AND status IN ('RENDERED','CONVERTED','FAILED')"
        params = (args.only,)

    rows = conn.execute(
        f"SELECT doku_id, rev_ts, raw_xhtml_path, type, user, comment FROM revisions "
        f"WHERE {where} ORDER BY doku_id, rev_ts",
        params,
    ).fetchall()
    log(f"history-convert 대상: {len(rows)} 리비전")

    users_map: dict[str, str] = {}  # history-upload 단계에서 받은 매핑 사용. 헤더에서는 텍스트 representation.
    ok = skipped = failed = 0
    prev_hash_by_page: dict[str, str | None] = {}

    for doku_id, rev_ts, raw_path, type_code, user, comment in rows:
        if not raw_path or not Path(raw_path).is_file():
            conn.execute(
                "UPDATE revisions SET status='FAILED', last_error='raw missing', last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (now_iso(), doku_id, rev_ts),
            )
            failed += 1
            continue
        try:
            raw_html = Path(raw_path).read_text(encoding="utf-8", errors="replace")
            storage_xml, _links, _atts, _title, _tags = _convert_html_to_storage(
                raw_html, src_root
            )
        except Exception as e:  # noqa: BLE001
            conn.execute(
                "UPDATE revisions SET status='FAILED', last_error=?, last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (f"convert error: {e!r}", now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            failed += 1
            continue

        header = _revision_header(
            rev_ts, user, comment, type_code, users_map,
            fmt=header_fmt,
        )
        full_body = header + storage_xml
        content_hash = sha256_bytes(full_body.encode("utf-8"))

        # revert / 본문 동일 dedup
        if prev_hash_by_page.get(doku_id) == content_hash:
            conn.execute(
                "UPDATE revisions SET status='SKIPPED', last_error='content_hash == prev', "
                "last_checked_at=? WHERE doku_id=? AND rev_ts=?",
                (now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            skipped += 1
            continue
        prev_hash_by_page[doku_id] = content_hash

        rel = Path(*doku_id.split(":"))
        out_path = HISTORY_STORAGE_DIR / rel.parent / f"{rel.name}.{rev_ts}.xml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(full_body, encoding="utf-8")
        conn.execute(
            "UPDATE revisions SET storage_path=?, content_hash=?, status='CONVERTED', "
            "last_error=NULL, last_checked_at=? WHERE doku_id=? AND rev_ts=?",
            (str(out_path), content_hash, now_iso(), doku_id, rev_ts),
        )
        conn.commit()
        ok += 1

    log(f"history-convert 완료: ok={ok} skipped={skipped} failed={failed}")
    conn.close()
    return 0


def _history_upload_select_pages(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> list[tuple[str, str, str]]:
    """history-upload 대상 페이지 SELECT (doku_id, confluence_page_id, title).

    args.only 지정 시 한 페이지로 제한. large_body_fallback 페이지는 호출자에서
    건너뜀 (선택 단계에서는 포함)."""
    # 이전: status='UPLOADED' 만 — pages 의 status 가 CONVERTED 같은 *다른* 흐름
    # 으로 떨어진 페이지 (실제로는 Confluence 에 본문 있음) 의 rev 가 누락됨.
    # confluence_page_id 가 있고 storage_path 가 있으면 history 적용 가능.
    where = "p.confluence_page_id IS NOT NULL AND p.storage_path IS NOT NULL"
    params: tuple = ()
    if args.only:
        where = "p.doku_id=?"
        params = (args.only,)
    return conn.execute(
        f"SELECT p.doku_id, p.confluence_page_id, p.title FROM pages p "
        f"WHERE {where} ORDER BY p.doku_id",
        params,
    ).fetchall()


def _history_upload_replay_one_page(
    session,
    base: str,
    conn: sqlite3.Connection,
    doku_id: str,
    cid: str,
    title: str,
    limit_left: int | None,
) -> tuple[int, int, bool]:
    """한 페이지의 CONVERTED 리비전을 ts 오름차순으로 PUT replay.

    Returns (rev_ok, rev_fail, hit_limit). hit_limit 이면 호출자에서 즉시 종료.
    동일 page 의 다음 rev 가 base version 충돌을 일으킬 수 있으므로 첫 실패 시
    break 후 다음 페이지로 넘어감."""
    hm = conn.execute(
        "SELECT last_replayed_rev_ts FROM history_meta WHERE doku_id=?", (doku_id,)
    ).fetchone()
    last_ts = hm[0] if hm and hm[0] else 0
    revs = conn.execute(
        "SELECT rev_ts, storage_path FROM revisions "
        "WHERE doku_id=? AND status='CONVERTED' AND rev_ts > ? "
        "ORDER BY rev_ts",
        (doku_id, last_ts),
    ).fetchall()
    if not revs:
        return (0, 0, False)
    log(f"  {doku_id}: {len(revs)} 리비전 replay")

    rev_ok = rev_fail = 0
    for rev_ts, sp in revs:
        if not sp or not Path(sp).is_file():
            rev_fail += 1
            continue
        body = Path(sp).read_text(encoding="utf-8")
        cur_ver = _get_page_version(session, base, cid)
        if cur_ver is None:
            rev_fail += 1
            break
        payload = {
            "id": cid, "status": "current", "title": title,
            "body": {"representation": "storage", "value": body},
            "version": {
                "number": cur_ver + 1,
                "message": f"DokuWiki rev {rev_ts}",
            },
        }
        resp = _request_with_retry(
            session, "PUT", f"{base}/api/v2/pages/{cid}", json=payload
        )
        if resp is None or resp.status_code >= 400:
            # fail 시 SKIPPED 로 마킹 후 *continue* (이전: break → 같은 페이지의
            # 다음 rev 모두 누락). 본문 한도 초과 같은 *영구* fail rev 가 chain
            # 을 막던 결함. Confluence current_version 은 fail PUT 으로 안 바뀌
            # 므로 다음 rev 의 cur+1 PUT 은 영향 없음.
            conn.execute(
                "UPDATE revisions SET status='SKIPPED', last_error=?, last_checked_at=? "
                "WHERE doku_id=? AND rev_ts=?",
                (f"PUT {resp.status_code if resp else 'no resp'} (skipped, chain 보존)",
                 now_iso(), doku_id, rev_ts),
            )
            conn.commit()
            rev_fail += 1
            continue
        conn.execute(
            "UPDATE revisions SET status='UPLOADED', last_error=NULL, last_checked_at=? "
            "WHERE doku_id=? AND rev_ts=?",
            (now_iso(), doku_id, rev_ts),
        )
        conn.execute(
            "INSERT INTO history_meta(doku_id, last_replayed_rev_ts) VALUES(?, ?) "
            "ON CONFLICT(doku_id) DO UPDATE SET last_replayed_rev_ts=excluded.last_replayed_rev_ts",
            (doku_id, rev_ts),
        )
        conn.commit()
        rev_ok += 1
        if limit_left is not None and rev_ok >= limit_left:
            _history_restore_latest_body(session, base, conn, doku_id, cid, title)
            return (rev_ok, rev_fail, True)

    # rev replay 끝 — Confluence 본문이 마지막 OK rev 로 남았으므로
    # latest storage 본문을 강제 PUT 해 *최신 상태 보장*. rev 일부가 fail 해도
    # 사용자가 보는 페이지 본문은 *언제나 latest*. 멱등 (uploaded_hash 매치 시 skip).
    _history_restore_latest_body(session, base, conn, doku_id, cid, title)
    return (rev_ok, rev_fail, False)


def _history_restore_latest_body(
    session,
    base: str,
    conn: sqlite3.Connection,
    doku_id: str,
    cid: str,
    title: str,
) -> bool:
    """history-upload 의 rev replay 후 *latest storage 본문* 을 PUT 보장.

    rev 한 개라도 fail 하면 replay 가 break — Confluence 본문이 옛 rev 로
    영구 남음. 이 helper 가 *replay 종료 후* latest 를 다시 push 해 *사용자가
    보는 본문 = 항상 latest* 를 유지.

    멱등: pages.content_hash == meta.uploaded_hash:<doku_id> 면 skip.
    """
    row = conn.execute(
        "SELECT storage_path, content_hash FROM pages WHERE doku_id=?", (doku_id,)
    ).fetchone()
    if not row or not row[0]:
        return False
    storage_path, content_hash = row
    uploaded_hash = db_get_meta(conn, f"uploaded_hash:{doku_id}") or ""
    if content_hash == uploaded_hash:
        return False  # 이미 latest

    sp = Path(storage_path)
    if not sp.is_file():
        return False
    body = sp.read_text(encoding="utf-8")
    cur_ver = _get_page_version(session, base, cid)
    if cur_ver is None:
        return False
    resp = _request_with_retry(
        session, "PUT", f"{base}/api/v2/pages/{cid}",
        json={
            "id": cid, "status": "current", "title": title or doku_id,
            "body": {"representation": "storage", "value": body},
            "version": {
                "number": cur_ver + 1,
                "message": "history-upload: latest 본문 복원 (rev replay 후)",
            },
        },
    )
    if resp is None or resp.status_code >= 400:
        log(f"    [WARN] {doku_id}: latest 복원 PUT 실패 "
            f"({resp.status_code if resp else 'no resp'})")
        return False
    db_set_meta(conn, f"uploaded_hash:{doku_id}", content_hash)
    conn.commit()
    return True


def cmd_history_upload(args: argparse.Namespace) -> int:
    """페이지마다 ts 오름차순으로 PUT replay. 각 PUT 의 version.message 에
    원본 dokuwiki rev 메타 (ts/user/comment) 동봉. resume 안전.

    *조심*: ~37k API 호출 가능. --limit 또는 --only 권장.
    """
    # 자격증명/base_url 검증은 _confluence_session 이 처리 — None 시 도움말 출력 + None 반환.
    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")
    _load_users_map(args.users_map)  # 검증 — message 에는 rev_ts 만 동봉

    pages = _history_upload_select_pages(conn, args)
    log(f"history-upload 페이지 후보: {len(pages)}")
    page_ok = rev_ok_total = rev_fail_total = 0

    for doku_id, cid, title in pages:
        # large body fallback 페이지 skip — 본문 PUT 거부
        if db_get_meta(conn, f"large_body_fallback:{doku_id}"):
            continue
        limit_left = (args.limit - rev_ok_total) if args.limit else None
        rev_ok, rev_fail, hit_limit = _history_upload_replay_one_page(
            session, base, conn, doku_id, cid, title, limit_left
        )
        rev_ok_total += rev_ok
        rev_fail_total += rev_fail
        if rev_ok > 0 or rev_fail > 0:
            page_ok += 1
        if hit_limit:
            log(f"--limit {args.limit} 도달")
            conn.close()
            return 0

    log(f"history-upload 완료: pages={page_ok} rev_ok={rev_ok_total} rev_fail={rev_fail_total}")
    conn.close()
    return 0 if rev_fail_total == 0 else 1


# Confluence 가 PUT 후 ac:schema-version / ac:macro-id 등 attr 를 자동 추가하므로
# 매크로 태그의 attr 부분은 [^>]*? 로 받아들임.
_REV_HEADER_PATTERNS = [
    # 'paragraphs' 형식 (기존 호환)
    re.compile(
        r'<ac:structured-macro[^>]*ac:name="(?:note|panel|info|tip|warning)"[^>]*>'
        r'<ac:rich-text-body>'
        r'<p>DokuWiki revision.*?</p>'
        r'<p>Author.*?</p>'
        r'<p>Comment.*?</p>'
        r'</ac:rich-text-body>'
        r'</ac:structured-macro>',
        re.S,
    ),
    # 매크로 + 불릿 리스트 3 항목 (현 기본 panel)
    re.compile(
        r'<ac:structured-macro[^>]*ac:name="(?:panel|info|note|tip|warning)"[^>]*>'
        r'<ac:rich-text-body>'
        r'\s*<ul>\s*'
        r'<li>\s*DokuWiki revision.*?</li>\s*'
        r'<li>\s*Author.*?</li>\s*'
        r'<li>\s*Comment.*?</li>\s*'
        r'</ul>\s*'
        r'</ac:rich-text-body>'
        r'</ac:structured-macro>',
        re.S,
    ),
    # panel/info/note/tip/warning (한 단락 + <br/>) — 이전 호환
    re.compile(
        r'<ac:structured-macro[^>]*ac:name="(?:panel|info|note|tip|warning)"[^>]*>'
        r'<ac:rich-text-body>'
        r'<p>\s*DokuWiki revision.*?Author.*?Comment.*?</p>'
        r'</ac:rich-text-body>'
        r'</ac:structured-macro>',
        re.S,
    ),
    # blockquote
    re.compile(r'<blockquote><p>\s*DokuWiki revision.*?Comment.*?</p></blockquote>', re.S),
    # table
    re.compile(
        r'<table>\s*<tr><th>DokuWiki revision.*?</tr>\s*<tr><th>Author.*?</tr>'
        r'\s*<tr><th>Comment.*?</tr>\s*</table>',
        re.S,
    ),
]


def _strip_revision_header(body: str) -> str:
    """페이지 본문 앞부분의 기존 revision 헤더 (어떤 형식이든) 제거.

    revision 헤더는 본문 *제일 앞* 에 있으므로 본문 초반의 매칭만 제거.
    여러 번 reformat 된 페이지의 *누적된* 헤더 모두 제거 — 한 번에 가능한
    한 모두 (loop).
    """
    # 본문 시작에서 공백/줄바꿈 후 첫 매크로까지의 위치 = 초기 위치 허용 범위
    # 누적된 헤더가 여러 개 있을 수 있으므로 (이전 reformat 의 잔재) loop.
    for _ in range(5):
        changed = False
        for pat in _REV_HEADER_PATTERNS:
            m = pat.search(body[:8192])
            if m and m.start() < 400:
                body = body[:m.start()] + body[m.end():]
                changed = True
                break
        if not changed:
            break
    return body


_REV_HEADER_EXTRACT_RES = {
    "rev_dt": re.compile(r"DokuWiki revision:\s*<code>([^<]+)</code>\s*\(([^)]+)\)"),
    "author": re.compile(r"Author:\s*([^<\n][^<\n]*?)(?:<br|</p|</td)"),
    "comment": re.compile(r"Comment:\s*<code>([^<]*)</code>"),
    "comment_alt": re.compile(r"Comment:\s*([^<\n][^<\n]*?)(?:</p|</td)"),
}


def _extract_revision_header_data(body: str) -> dict | None:
    """이미 업로드된 페이지 본문에서 revision 헤더의 데이터 (rev_dt/type_label/
    author/comment) 추출 — revisions 테이블이 없을 때 fallback.

    어떤 형식 (paragraphs/panel/info/quote/table) 이든 같은 텍스트 패턴.
    """
    head = body[:4096]
    rev_m = _REV_HEADER_EXTRACT_RES["rev_dt"].search(head)
    if not rev_m:
        return None
    rev_dt = rev_m.group(1).strip()
    type_label = rev_m.group(2).strip()
    author_m = _REV_HEADER_EXTRACT_RES["author"].search(head)
    author = author_m.group(1).strip() if author_m else "(unknown)"
    comment_m = (_REV_HEADER_EXTRACT_RES["comment"].search(head)
                 or _REV_HEADER_EXTRACT_RES["comment_alt"].search(head))
    comment = comment_m.group(1).strip() if comment_m else ""
    return {
        "rev_dt": rev_dt, "type_label": type_label,
        "author": author, "comment": comment,
    }


def _revision_header_from_extracted(d: dict, *, fmt: str) -> str:
    """추출된 헤더 데이터를 새 형식으로 재구성 — _revision_header 와 동일한
    템플릿이지만 rev_ts 가 아닌 이미 포맷된 dt 문자열 사용."""
    if fmt == "none":
        return ""
    dt = _h.escape(d["rev_dt"])
    type_label = _h.escape(d["type_label"])
    author = _h.escape(d["author"])
    comment = _h.escape(d["comment"]) if d["comment"] else "(no comment)"

    if fmt == "paragraphs":
        return (
            '<ac:structured-macro ac:name="note"><ac:rich-text-body>'
            f'<p>DokuWiki revision: <code>{dt}</code> ({type_label})</p>'
            f'<p>Author: {author}</p>'
            f'<p>Comment: <code>{comment}</code></p>'
            '</ac:rich-text-body></ac:structured-macro>'
        )
    if fmt == "quote":
        return (
            '<blockquote><p>'
            f'DokuWiki revision: <code>{dt}</code> ({type_label})<br/>'
            f'Author: {author}<br/>'
            f'Comment: <code>{comment}</code>'
            '</p></blockquote>'
        )
    if fmt == "table":
        return (
            '<table>'
            f'<tr><th>DokuWiki revision</th><td><code>{dt}</code> ({type_label})</td></tr>'
            f'<tr><th>Author</th><td>{author}</td></tr>'
            f'<tr><th>Comment</th><td><code>{comment}</code></td></tr>'
            '</table>'
        )
    macro_name = fmt if fmt in ("panel", "info", "note", "tip", "warning") else "panel"
    return (
        f'<ac:structured-macro ac:name="{macro_name}"><ac:rich-text-body>'
        '<ul>'
        f'<li>DokuWiki revision: <code>{dt}</code> ({type_label})</li>'
        f'<li>Author: {author}</li>'
        f'<li>Comment: <code>{comment}</code></li>'
        '</ul>'
        '</ac:rich-text-body></ac:structured-macro>'
    )


def cmd_history_rewrite_headers(args: argparse.Namespace) -> int:
    """이미 업로드된 페이지의 *현재 표시 중인* revision 헤더를 새 형식으로 교체.

    GET 으로 본문 받기 → _strip_revision_header 로 기존 헤더 제거 → 새 형식
    헤더 prepend → PUT. revisions 테이블의 *최신* rev 만 적용 (history 의
    이전 버전들은 새로 history-convert+upload 해야 반영됨).
    """
    # 자격증명 검증은 _confluence_session 이 처리.
    conn = db_connect(args.db)
    fmt = args.header_format or db_get_meta(conn, "revision_header_fmt") or REVISION_HEADER_DEFAULT
    if fmt not in REVISION_HEADER_FORMATS:
        log(f"알 수 없는 header-format: {fmt}")
        return 2
    db_set_meta(conn, "revision_header_fmt", fmt)

    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    users_map = _load_users_map(args.users_map)

    # 모든 UPLOADED 메인 페이지가 대상 — revisions 테이블 비어있으면 본문에서
    # 헤더 데이터 추출하는 fallback 사용.
    sql = "SELECT doku_id, confluence_page_id FROM pages WHERE confluence_page_id IS NOT NULL"
    params: tuple = ()
    if args.only:
        sql += " AND doku_id=?"
        params = (args.only,)
    sql += " ORDER BY doku_id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql, params).fetchall()
    log(f"대상 후보: {len(rows)} 페이지 (헤더 없는 페이지는 skip)")

    pushed = unchanged = no_header = failed = 0
    for doku_id, page_id in rows:
        # GET 현재 storage 본문
        r = _request_with_retry(
            session, "GET", f"{base}/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        if r is None or r.status_code >= 400:
            log(f"  [SKIP] {doku_id} GET 실패")
            failed += 1
            continue
        js = r.json()
        cur_body = (js.get("body") or {}).get("storage", {}).get("value", "") or ""
        cur_ver = js.get("version", {}).get("number", 1)
        title = js.get("title")

        # revisions 테이블의 최신 rev 우선, 없으면 본문에서 추출
        rev_row = conn.execute(
            "SELECT rev_ts, user, comment, type FROM revisions "
            "WHERE doku_id=? AND status='UPLOADED' ORDER BY rev_ts DESC LIMIT 1",
            (doku_id,),
        ).fetchone()

        if rev_row:
            rev_ts, user, comment, type_code = rev_row
            new_header = _revision_header(rev_ts, user, comment, type_code, users_map, fmt=fmt)
        else:
            extracted = _extract_revision_header_data(cur_body)
            if not extracted:
                no_header += 1
                continue
            new_header = _revision_header_from_extracted(extracted, fmt=fmt)

        stripped = _strip_revision_header(cur_body)
        new_body = new_header + stripped

        if new_body == cur_body:
            unchanged += 1
            continue

        if args.dry_run:
            log(f"  [dry] {doku_id} → fmt={fmt} (rev={rev_row or 'extracted'})")
            continue

        payload = {
            "id": str(page_id),
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": new_body},
            "version": {"number": cur_ver + 1},
        }
        r = _request_with_retry(session, "PUT", f"{base}/api/v2/pages/{page_id}", json=payload)
        if r is None or r.status_code >= 400:
            log(f"  [FAIL] {doku_id}: {r.status_code if r else 'no resp'}")
            failed += 1
            continue
        pushed += 1
        if pushed % 50 == 0:
            log(f"  ... rewritten={pushed}")

    log(f"history-rewrite-headers 완료: pushed={pushed} unchanged={unchanged} "
        f"no_header={no_header} failed={failed} fmt={fmt}")
    conn.close()
    return 0 if failed == 0 else 1


def cmd_history_status(args: argparse.Namespace) -> int:
    """history 트랙의 진행 상황 출력 (read-only).

    state.db 조회만 — 변경 없음. `revisions` 상태별 카운트 (DISCOVERED /
    RENDERED / CONVERTED / UPLOADED / FAILED) + `history_meta` 의 페이지별
    last_replayed_rev_ts 요약."""
    conn = db_connect(args.db)
    print("==== history_meta 요약 ====")
    n_pages, total_revs, max_revs = conn.execute(
        "SELECT COUNT(*), SUM(total_revs), MAX(total_revs) FROM history_meta"
    ).fetchone()
    print(f"  unique pages: {n_pages}")
    print(f"  total revisions: {total_revs}")
    print(f"  max revs per page: {max_revs}")
    print()
    print("==== revisions status 분포 ====")
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM revisions GROUP BY status"
    ).fetchall():
        print(f"  {status:12} {n}")
    print()
    print("==== type 분포 (changes 로그) ====")
    for t, n in conn.execute(
        "SELECT type, COUNT(*) FROM revisions WHERE type IS NOT NULL GROUP BY type ORDER BY n DESC"
        if False else "SELECT type, COUNT(*) FROM revisions WHERE type IS NOT NULL GROUP BY type"
    ).fetchall():
        print(f"  {t}: {n}")
    print()
    print("==== media_revisions ====")
    n_media, total_media, max_media = conn.execute(
        "SELECT COUNT(DISTINCT media_id), COUNT(*), MAX(rev_ts) FROM media_revisions"
    ).fetchone()
    print(f"  unique media: {n_media}")
    print(f"  total versions: {total_media}")
    print()
    print("==== 상위 5 revision-heavy 페이지 ====")
    for d, n in conn.execute(
        "SELECT doku_id, total_revs FROM history_meta ORDER BY total_revs DESC LIMIT 5"
    ).fetchall():
        print(f"  {d}: {n}")
    conn.close()
    return 0


# § struct: discover / convert

def _struct_db_path_from_meta(conn: sqlite3.Connection) -> Path | None:
    src = db_get_meta(conn, "dokuwiki_src")
    if not src:
        return None
    p = Path(src) / "meta" / "struct.sqlite3"
    return p if p.is_file() else None


def cmd_struct_discover(args: argparse.Namespace) -> int:
    """meta/struct.sqlite3 에서 활성 schema(=latest ts) + 컬럼 + row 를
    state.db 의 struct_* 테이블로 옮긴다. 라이브 Confluence 호출 없음."""
    conn = db_connect(args.db)
    db_init(conn)

    sd_path = _struct_db_path_from_meta(conn)
    if not sd_path:
        log("dokuwiki_src 또는 struct.sqlite3 를 찾을 수 없습니다 — 먼저 discover 실행 또는 --struct-db 직접 지정.")
        if args.struct_db:
            sd_path = Path(args.struct_db)
            if not sd_path.is_file():
                log(f"--struct-db 경로 없음: {sd_path}")
                return 2
        else:
            return 2

    sd = sqlite3.connect(str(sd_path))
    # 활성 schema = 각 tbl 의 최신 sid (MAX ts)
    rows = sd.execute(
        "SELECT id, tbl FROM schemas WHERE id IN ("
        "  SELECT id FROM schemas WHERE (tbl, ts) IN ("
        "    SELECT tbl, MAX(ts) FROM schemas GROUP BY tbl"
        "  )"
        ")"
    ).fetchall()

    n_schemas = 0
    n_cols = 0
    n_rows = 0
    n_refs = 0

    for sid, tbl in rows:
        data_tbl = f"data_{tbl}"
        try:
            row_count = sd.execute(f"SELECT COUNT(*) FROM {data_tbl} WHERE latest=1").fetchone()[0]
        except sqlite3.OperationalError:
            row_count = 0

        # 빈 schema (test 등) 도 일단 등록만, status=SKIPPED
        status = "DISCOVERED" if row_count > 0 else "SKIPPED"

        # 컬럼 정의
        col_rows = sd.execute(
            "SELECT sc.colref, sc.sort, sc.tid, t.class, t.config "
            "FROM schema_cols sc JOIN types t ON sc.tid=t.id "
            "WHERE sid=? AND sc.enabled=1 ORDER BY sc.sort",
            (sid,),
        ).fetchall()

        conn.execute(
            "INSERT OR REPLACE INTO struct_schemas(sid, tbl, row_count, column_count, status, last_checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sid, tbl, row_count, len(col_rows), status, now_iso()),
        )
        n_schemas += 1

        # types.config 에서 label.<lang> 추출
        col_name_map: dict[int, str] = {}
        for colref, sort, tid, cls, cfg_json in col_rows:
            name = None
            try:
                cfg = json.loads(cfg_json) if cfg_json else {}
                label = cfg.get("label", {})
                if isinstance(label, dict):
                    for lang in ("ko", "en", "*"):
                        if lang in label and label[lang]:
                            name = label[lang]
                            break
                    if name is None and label:
                        name = next(iter(label.values()), None)
            except (ValueError, TypeError):
                pass
            if not name:
                name = f"col{colref}"
            col_name_map[colref] = name
            conn.execute(
                "INSERT OR REPLACE INTO struct_columns(sid, colref, sort, name, dokuwiki_class, config_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, colref, sort, name, cls, cfg_json),
            )
            n_cols += 1

        if row_count == 0:
            conn.commit()
            continue

        # 데이터 row 들
        try:
            col_names = sd.execute(f"PRAGMA table_info({data_tbl})").fetchall()
        except sqlite3.OperationalError:
            conn.commit()
            continue
        col_list = [c[1] for c in col_names if c[1].startswith("col")]

        data_rows = sd.execute(
            f"SELECT pid, rev, latest, {', '.join(col_list)} FROM {data_tbl} WHERE latest=1"
        ).fetchall()

        # multi 컬럼: multi_<tbl> (pid, colref, row, value)
        multi_tbl = f"multi_{tbl}"
        multi_data: dict[tuple[int, int], list[str]] = {}
        try:
            for pid, colref, _row, value in sd.execute(
                f"SELECT pid, colref, row, value FROM {multi_tbl} WHERE latest=1 ORDER BY pid, colref, row"
            ).fetchall():
                multi_data.setdefault((pid, colref), []).append(value)
        except sqlite3.OperationalError:
            pass

        # 컬럼 class 룩업
        cls_map = {colref: cls for colref, _sort, _tid, cls, _cfg in col_rows}

        for row in data_rows:
            pid = row[0]
            payload: dict[str, object] = {}
            bound = None
            for i, colref in enumerate([int(c[3:]) for c in col_list]):
                v = row[3 + i]
                # multi 데이터 우선
                if (pid, colref) in multi_data:
                    v = multi_data[(pid, colref)]
                if v is None or v == "":
                    continue
                payload[str(colref)] = v
                cls = cls_map.get(colref)
                if cls == "Wiki":
                    # value 가 dokuwiki page id 또는 [[id|text]] 형식
                    target = v if isinstance(v, str) else None
                    if target and "[[" not in target:
                        if bound is None:
                            bound = target.lstrip(":")
                        conn.execute(
                            "INSERT OR REPLACE INTO struct_references(src_sid, src_pid, src_colref, target_kind, target_locator) "
                            "VALUES (?, ?, ?, 'page', ?)",
                            (sid, pid, colref, target.lstrip(":")),
                        )
                        n_refs += 1
                elif cls == "Media":
                    target = v if isinstance(v, str) else None
                    if target:
                        conn.execute(
                            "INSERT OR REPLACE INTO struct_references(src_sid, src_pid, src_colref, target_kind, target_locator) "
                            "VALUES (?, ?, ?, 'attachment', ?)",
                            (sid, pid, colref, target.lstrip(":")),
                        )
                        n_refs += 1
                elif cls == "Lookup":
                    # cross-schema reference — value 는 보통 "<tbl>:<pid>" 또는 정수
                    if isinstance(v, str):
                        conn.execute(
                            "INSERT OR REPLACE INTO struct_references(src_sid, src_pid, src_colref, target_kind, target_locator) "
                            "VALUES (?, ?, ?, 'schema_row', ?)",
                            (sid, pid, colref, v),
                        )
                        n_refs += 1

            conn.execute(
                "INSERT OR REPLACE INTO struct_rows(sid, pid, bound_doku_id, payload_json, status) "
                "VALUES (?, ?, ?, ?, 'DISCOVERED')",
                (sid, pid, bound, json.dumps(payload, ensure_ascii=False)),
            )
            n_rows += 1

        conn.commit()

    sd.close()

    log(f"struct-discover 완료: schemas={n_schemas}, columns={n_cols}, rows={n_rows}, refs={n_refs}")
    # 활성 schema 요약
    print("--- schemas ---")
    for sid, tbl, rc, cc, status in conn.execute(
        "SELECT sid, tbl, row_count, column_count, status FROM struct_schemas ORDER BY tbl"
    ).fetchall():
        print(f"  sid={sid:3} tbl={tbl:25} cols={cc} rows={rc} status={status}")
    conn.close()
    return 0


_WIKI_LINK_RE = re.compile(r"^\[\[\s*([^\|\]]+?)\s*(?:\|\s*(.*?)\s*)?\]\]$")


def _struct_resolve_page(conn, locator: str) -> tuple[str, str] | None:
    """DokuWiki page id (or [[id|label]]) → (confluence_page_id, title)."""
    if not locator:
        return None
    m = _WIKI_LINK_RE.match(locator.strip())
    if m:
        target = m.group(1).lstrip(":")
    else:
        target = locator.lstrip(":")
    row = conn.execute(
        "SELECT confluence_page_id, title FROM pages "
        "WHERE doku_id=? AND confluence_page_id IS NOT NULL",
        (target,),
    ).fetchone()
    if row:
        return row
    base = target.rsplit(":", 1)[-1]
    rows = conn.execute(
        "SELECT confluence_page_id, title FROM pages "
        "WHERE doku_id LIKE ? AND confluence_page_id IS NOT NULL",
        (f"%:{base}",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    return None


def _struct_resolve_attachment(conn, locator: str) -> tuple[str, str, str] | None:
    """media_id → (confluence_attachment_id, confluence_page_id, filename)."""
    if not locator:
        return None
    target = locator.lstrip(":")
    row = conn.execute(
        "SELECT confluence_attachment_id, confluence_page_id, media_id FROM attachments "
        "WHERE media_id=? AND confluence_attachment_id IS NOT NULL",
        (target,),
    ).fetchone()
    if row:
        return row
    base = target.rsplit(":", 1)[-1]
    rows = conn.execute(
        "SELECT confluence_attachment_id, confluence_page_id, media_id FROM attachments "
        "WHERE media_id LIKE ? AND confluence_attachment_id IS NOT NULL",
        (f"%:{base}",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    return None


_IMAGE_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)$", re.IGNORECASE)


def _struct_render_cell(conn, cls: str, value, *, multi_join: str = ", ") -> str:
    """단일 셀 (값+클래스)을 Confluence storage XML 의 cell 내용으로 렌더링.

    cls 가 Wiki/Media/Url/Date 면 ri 토큰 / 링크 / time 으로,
    Text/Decimal/Dropdown 은 escape 된 텍스트.
    multi 값은 같은 셀에 누적.
    """
    if value is None or value == "":
        return ""
    if isinstance(value, list):
        return multi_join.join(
            _struct_render_cell(conn, cls, v, multi_join=multi_join) for v in value if v not in (None, "")
        )
    sval = str(value)
    if cls == "Url":
        url = _h.escape(sval, quote=True)
        return f'<a href="{url}">{_h.escape(sval)}</a>'
    if cls == "Date":
        return f'<time datetime="{_h.escape(sval, quote=True)}">{_h.escape(sval)}</time>'
    if cls == "Wiki":
        m = _WIKI_LINK_RE.match(sval.strip())
        if m:
            target = m.group(1).lstrip(":")
            label = m.group(2) or target
            resolved = _struct_resolve_page(conn, target)
            if resolved:
                _cp, title = resolved
                return (
                    f'<ac:link><ri:page ri:content-title="{_h.escape(title or "", quote=True)}"/>'
                    f'<ac:plain-text-link-body><![CDATA[{label}]]></ac:plain-text-link-body>'
                    f'</ac:link>'
                )
            return f'<span class="dwc-unresolved-page" data-doku-id="{_h.escape(target, quote=True)}">{_h.escape(label)}</span>'
        return f'<p>{_h.escape(sval)}</p>'
    if cls == "Media":
        resolved = _struct_resolve_attachment(conn, sval)
        if resolved:
            _ca, _cp, media_id = resolved
            fname = media_id.rsplit(":", 1)[-1]
            if _IMAGE_EXT_RE.search(fname):
                return f'<ac:image><ri:attachment ri:filename="{_h.escape(fname, quote=True)}"/></ac:image>'
            return (
                f'<ac:link>'
                f'<ri:attachment ri:filename="{_h.escape(fname, quote=True)}"/>'
                f'<ac:plain-text-link-body><![CDATA[{fname}]]></ac:plain-text-link-body>'
                f'</ac:link>'
            )
        return f'<span class="dwc-unresolved-media" data-media-id="{_h.escape(sval, quote=True)}">{_h.escape(sval)}</span>'
    # Text / Decimal / Dropdown / Lookup / User / 기타
    return _h.escape(sval).replace("\n", "<br/>")


def _struct_row_to_details_macro(conn, sid: int, payload: dict, columns) -> str:
    """단일 struct row → Page Properties (details) 매크로 본문.

    columns 는 [(colref, name, cls)] 정렬된 리스트.
    """
    rows_html = []
    for colref, name, cls in columns:
        val = payload.get(str(colref))
        cell = _struct_render_cell(conn, cls, val)
        rows_html.append(
            f'<tr><th>{_h.escape(name or f"col{colref}")}</th><td>{cell}</td></tr>'
        )
    return (
        "<ac:structured-macro ac:name=\"details\">"
        "<ac:rich-text-body>"
        f"<table>{''.join(rows_html)}</table>"
        "</ac:rich-text-body>"
        "</ac:structured-macro>"
    )


def _struct_row_title(payload: dict, columns, tbl: str, pid: int) -> str:
    """행의 표시 제목을 결정. 첫 Text 컬럼 또는 name='code/name/title' 우선.

    fallback: "{tbl}#{pid}".
    """
    # 1) 이름이 'code', 'name', 'title' 인 컬럼 우선
    preferred_names = ("code", "name", "title", "이름", "코드", "제목")
    for colref, name, cls in columns:
        if name and name.lower() in preferred_names:
            v = payload.get(str(colref))
            if v and not isinstance(v, list):
                return f"{tbl}: {v}"
    # 2) 첫 Text/Decimal 컬럼의 값
    for colref, name, cls in columns:
        if cls in ("Text", "Decimal"):
            v = payload.get(str(colref))
            if v and not isinstance(v, list) and len(str(v)) <= 80:
                return f"{tbl}: {v}"
    return f"{tbl}#{pid}"


# Back-compat shim for older callers (snapshot mode).
def _struct_row_to_storage_table(conn, sid: int, payload: dict) -> str:
    cols = conn.execute(
        "SELECT colref, name, dokuwiki_class FROM struct_columns WHERE sid=? ORDER BY sort",
        (sid,),
    ).fetchall()
    return _struct_row_to_details_macro(conn, sid, payload, cols)


def cmd_struct_convert(args: argparse.Namespace) -> int:
    """struct_rows 의 payload_json 을 storage XML 표/리스트로 변환.

    mode=snapshot: 각 schema 마다 모든 row 를 큰 표로 (대량 페이지 1개)
    mode=properties: 각 row 마다 자식 페이지 + Page Properties 매크로
    mode=native: Confluence Database API 사용 (probe 필요; 미구현 fallback → properties)

    이 함수는 *storage 만 만들고 DB 행에 path 기록*. 업로드는 struct-upload.
    """
    conn = db_connect(args.db)
    out_dir = Path("storage_struct")
    out_dir.mkdir(exist_ok=True)
    mode = args.mode

    if args.reconvert:
        schemas = conn.execute(
            "SELECT sid, tbl, row_count, status FROM struct_schemas "
            "WHERE status != 'SKIPPED' ORDER BY tbl"
        ).fetchall()
    else:
        schemas = conn.execute(
            "SELECT sid, tbl, row_count, status FROM struct_schemas "
            "WHERE status NOT IN ('SKIPPED', 'UPLOADED') ORDER BY tbl"
        ).fetchall()
    if not schemas:
        log("struct-convert 대상 schema 없음.")
        return 0

    converted = 0
    for sid, tbl, row_count, status in schemas:
        if row_count == 0:
            continue
        log(f"=== {tbl} (sid={sid}, {row_count} rows, mode={mode}) ===")
        rows = conn.execute(
            "SELECT pid, bound_doku_id, payload_json FROM struct_rows "
            "WHERE sid=? ORDER BY pid",
            (sid,),
        ).fetchall()
        if not rows:
            continue

        cols = conn.execute(
            "SELECT colref, name, dokuwiki_class FROM struct_columns WHERE sid=? ORDER BY sort",
            (sid,),
        ).fetchall()
        col_headers = [_h.escape(name or f"col{cr}") for cr, name, _cls in cols]

        if mode == "snapshot":
            header_row = "<tr>" + "".join(f"<th>{h}</th>" for h in col_headers) + "</tr>"
            body_rows = []
            for pid, bound, payload_json in rows:
                payload = _json.loads(payload_json)
                cells = []
                for colref, _name, cls in cols:
                    val = payload.get(str(colref))
                    cells.append(f"<td>{_struct_render_cell(conn, cls, val)}</td>")
                body_rows.append("<tr>" + "".join(cells) + "</tr>")
            body = (
                f'<h1>{_h.escape(tbl)} ({row_count} rows)</h1>'
                f'<p>DokuWiki struct schema sid={sid} 로부터 자동 생성.</p>'
                f'<table>{header_row}{"".join(body_rows)}</table>'
            )
            out = out_dir / f"{tbl}.snapshot.xml"
            out.write_text(body, encoding="utf-8")
            conn.execute(
                "UPDATE struct_schemas SET chosen_mode='snapshot', status=CASE WHEN status='UPLOADED' THEN 'UPLOADED' ELSE 'DEFINED' END WHERE sid=?",
                (sid,),
            )
            log(f"  snapshot storage → {out}")
            converted += 1
        elif mode in ("properties", "native"):
            for pid, bound, payload_json in rows:
                payload = _json.loads(payload_json)
                details = _struct_row_to_details_macro(conn, sid, payload, cols)
                out = out_dir / f"{tbl}.row.{pid}.xml"
                out.write_text(details, encoding="utf-8")
            db_id_row = conn.execute(
                "SELECT confluence_db_id FROM struct_schemas WHERE sid=?", (sid,)
            ).fetchone()
            db_id = db_id_row[0] if db_id_row else None
            (out_dir / f"{tbl}.index.xml").write_text(
                _struct_build_index_xml(
                    tbl, sid, mode, cols, row_count, db_id,
                    db_get_meta(conn, "confluence_base_url") or "",
                    db_get_meta(conn, "confluence_space_key") or "",
                ),
                encoding="utf-8",
            )
            conn.execute(
                "UPDATE struct_schemas SET chosen_mode=?, status=CASE WHEN status='UPLOADED' THEN 'UPLOADED' ELSE 'DEFINED' END WHERE sid=?",
                (mode, sid),
            )
            log(f"  {mode} storage → {len(rows)} row + 1 index")
            converted += len(rows)

    conn.commit()
    log(f"struct-convert 완료: rows={converted}, schemas 처리됨")
    conn.close()
    return 0


def _struct_upload_probe_database_api(
    session, base: str, space_id: str, args: argparse.Namespace
) -> int:
    """Confluence Database API 가용성 probe — 빈 Database 생성 후 컬럼/row
    입력이 가능한 endpoint 가 있는지 후보 경로들을 체계적으로 탐색.

    Atlassian 이 비공개로 운영 중인 경로 (rows/entries/items/records 등) 도 시도.
    cleanup 으로 생성한 임시 Database 삭제 (--probe-keep 으로 보존 가능).
    Returns: 0=발견, 1=Database 생성 실패, 3=발견 못함."""
    log("=== Confluence Database API probe ===")
    resp = _request_with_retry(
        session, "POST", f"{base}/api/v2/databases",
        json={"spaceId": space_id, "title": "dwc-probe", "parentId": args.root_page_id},
    )
    log(f"  POST /api/v2/databases → {resp.status_code if resp else 'no resp'}")
    if not resp or resp.status_code >= 400:
        log(f"    body: {(resp.text if resp else '')[:300]}")
        log("=== probe 종료 (Database 생성 자체 실패) ===")
        return 1
    db_obj = resp.json()
    db_id = db_obj.get("id")
    log(f"  생성된 db: id={db_id} title={db_obj.get('title')}")

    column_probes = [
        ("POST", f"/api/v2/databases/{db_id}/columns", {"title": "txt", "type": "text"}),
        ("POST", f"/api/v2/databases/{db_id}/fields",  {"title": "txt", "type": "text"}),
        ("POST", f"/api/v2/databases/{db_id}/schema",  {"columns": [{"title": "txt", "type": "text"}]}),
        ("PATCH", f"/api/v2/databases/{db_id}",        {"columns": [{"title": "txt", "type": "text"}]}),
        ("PUT", f"/api/v2/databases/{db_id}",          {"title": "dwc-probe", "columns": [{"title": "txt", "type": "text"}]}),
    ]
    row_probes = [
        ("POST", f"/api/v2/databases/{db_id}/rows",      {"values": {"txt": "x"}}),
        ("POST", f"/api/v2/databases/{db_id}/entries",   {"values": {"txt": "x"}}),
        ("POST", f"/api/v2/databases/{db_id}/items",     {"values": {"txt": "x"}}),
        ("POST", f"/api/v2/databases/{db_id}/records",   {"values": {"txt": "x"}}),
    ]
    get_probes = [
        f"/api/v2/databases/{db_id}/columns",
        f"/api/v2/databases/{db_id}/fields",
        f"/api/v2/databases/{db_id}/rows",
        f"/api/v2/databases/{db_id}/entries",
        f"/api/v2/databases/{db_id}?include-properties=true",
    ]
    any_ok = False
    log("  -- column endpoints --")
    for method, path, payload in column_probes:
        r = _request_with_retry(session, method, f"{base}{path}", json=payload)
        log(f"    {method:5} {path} → {r.status_code if r else 'no resp'}")
        if r and r.status_code < 400:
            any_ok = True
            log(f"      body[:200]={(r.text or '')[:200]}")
    log("  -- row endpoints --")
    for method, path, payload in row_probes:
        r = _request_with_retry(session, method, f"{base}{path}", json=payload)
        log(f"    {method:5} {path} → {r.status_code if r else 'no resp'}")
        if r and r.status_code < 400:
            any_ok = True
            log(f"      body[:200]={(r.text or '')[:200]}")
    log("  -- GET probes --")
    for path in get_probes:
        r = _request_with_retry(session, "GET", f"{base}{path}")
        log(f"    GET   {path} → {r.status_code if r else 'no resp'}")
        if r and r.status_code < 400:
            log(f"      body[:200]={(r.text or '')[:200]}")

    log(f"  probe 결과: 컬럼/row 입력 endpoint {'발견됨' if any_ok else '없음'}.")
    if not args.probe_keep:
        r = _request_with_retry(session, "DELETE", f"{base}/api/v2/databases/{db_id}")
        log(f"  cleanup DELETE → {r.status_code if r else 'no resp'} (db_id={db_id})")
    else:
        log(f"  probe-keep: db_id={db_id} 유지")
    log("=== probe 종료 ===")
    return 0 if any_ok else 3


def _struct_upload_select_schemas(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> list[tuple[int, str, str]]:
    """업로드 대상 schemas (sid, tbl, mode). auto 면 각 schema 의 chosen_mode
    사용; explicit 이면 그 모드로 덮어쓰기. --only-tbl / --limit 적용."""
    if args.mode == "auto":
        sql = (
            "SELECT sid, tbl, COALESCE(chosen_mode,'snapshot') FROM struct_schemas "
            "WHERE status IN ('DEFINED','UPLOADED') ORDER BY tbl"
        )
        schemas = conn.execute(sql).fetchall()
    else:
        sql = "SELECT sid, tbl, ? FROM struct_schemas WHERE status IN ('DEFINED','UPLOADED') ORDER BY tbl"
        schemas = [(sid, tbl, args.mode) for sid, tbl, _ in conn.execute(sql, (args.mode,)).fetchall()]
    if args.only_tbl:
        schemas = [s for s in schemas if s[1] == args.only_tbl]
    if args.limit:
        schemas = schemas[: args.limit]
    return schemas


def _struct_upload_snapshot_schema(
    conn: sqlite3.Connection,
    session,
    base: str,
    space_id: str,
    args: argparse.Namespace,
    sid: int,
    tbl: str,
    out_dir: Path,
) -> bool:
    """snapshot 모드 한 schema 업로드 — 1 페이지에 큰 표 전체. Returns success."""
    sp = out_dir / f"{tbl}.snapshot.xml"
    if not sp.is_file():
        log(f"  storage 파일 없음: {sp}")
        return False
    page_id = conn.execute(
        "SELECT snapshot_page_id FROM struct_schemas WHERE sid=?", (sid,)
    ).fetchone()[0]
    storage = sp.read_text(encoding="utf-8")
    title = f"dokuwiki struct: {tbl}"
    if page_id:
        if not _struct_put_page(session, base, page_id, title=title, storage=storage):
            log(f"  [FAIL] PUT {tbl}")
            return False
        log(f"  [SNAPSHOT] {tbl} → page {page_id} (updated)")
    else:
        page_id = _struct_post_page(
            session, base, space_id, args.root_page_id,
            title=title, storage=storage, sid=sid,
        )
        if not page_id:
            log(f"  [FAIL] POST {tbl}")
            return False
        conn.execute(
            "UPDATE struct_schemas SET snapshot_page_id=?, status='UPLOADED', last_checked_at=? WHERE sid=?",
            (str(page_id), now_iso(), sid),
        )
        conn.commit()
        log(f"  [SNAPSHOT] {tbl} → page {page_id} (created)")
    return True


def _struct_upload_indexed_schema(
    conn: sqlite3.Connection,
    session,
    base: str,
    space_id: str,
    args: argparse.Namespace,
    sid: int,
    tbl: str,
    mode: str,
    out_dir: Path,
) -> tuple[bool, int, int]:
    """properties/native 모드 한 schema — index 페이지 + row 자식 페이지들.

    native 모드면 빈 Confluence Database 쉘 생성 시도 (없을 때만, --no-native-shell
    아니면). Returns (schema_ok, row_pushed, row_failed)."""
    index_sp = out_dir / f"{tbl}.index.xml"
    if not index_sp.is_file():
        log(f"  index storage 파일 없음: {index_sp}. struct-convert 먼저 실행.")
        return (False, 0, 0)

    # native: 빈 Confluence Database 객체 생성 (없을 때만)
    db_id_row = conn.execute(
        "SELECT confluence_db_id FROM struct_schemas WHERE sid=?", (sid,)
    ).fetchone()
    existing_db_id = db_id_row[0] if db_id_row else None
    if mode == "native" and not existing_db_id and not args.no_native_shell:
        r = _request_with_retry(
            session, "POST", f"{base}/api/v2/databases",
            json={"spaceId": space_id, "parentId": args.root_page_id, "title": f"dwc-struct-{tbl}"},
        )
        if r and r.status_code < 400:
            new_db_id = r.json().get("id")
            conn.execute(
                "UPDATE struct_schemas SET confluence_db_id=? WHERE sid=?",
                (str(new_db_id), sid),
            )
            conn.commit()
            log(f"  [NATIVE] Database 쉘 생성 → id={new_db_id}")
            _struct_rewrite_index(conn, sid, tbl, mode, out_dir, args.base_url, args.space_key)
            index_sp = out_dir / f"{tbl}.index.xml"
        else:
            log(f"  [WARN] Database 쉘 생성 실패 → fallback properties only")

    # 1) index 페이지 — snapshot_page_id 재사용 가능
    idx_existing = conn.execute(
        "SELECT properties_index_page_id, snapshot_page_id FROM struct_schemas WHERE sid=?",
        (sid,),
    ).fetchone()
    idx_page_id = idx_existing[0] or idx_existing[1]
    idx_storage = index_sp.read_text(encoding="utf-8")
    idx_title = f"dokuwiki struct: {tbl}"
    if idx_page_id:
        if not _struct_put_page(session, base, idx_page_id, title=idx_title, storage=idx_storage):
            log(f"  [FAIL] index PUT {tbl}")
            return (False, 0, 0)
        log(f"  index updated → page {idx_page_id}")
    else:
        idx_page_id = _struct_post_page(
            session, base, space_id, args.root_page_id,
            title=idx_title, storage=idx_storage, sid=sid,
        )
        if not idx_page_id:
            log(f"  [FAIL] index POST {tbl}")
            return (False, 0, 0)
        log(f"  index created → page {idx_page_id}")
    conn.execute(
        "UPDATE struct_schemas SET properties_index_page_id=?, snapshot_page_id=COALESCE(snapshot_page_id, ?), chosen_mode=?, status='UPLOADED', last_checked_at=? WHERE sid=?",
        (str(idx_page_id), str(idx_page_id), mode, now_iso(), sid),
    )
    conn.commit()

    if args.index_only:
        log(f"  --index-only: row 페이지 갱신 skip")
        return (True, 0, 0)

    # 2) 자식 row 페이지 업로드
    row_sql = "SELECT pid, payload_json, confluence_page_id FROM struct_rows WHERE sid=? ORDER BY pid"
    if args.row_limit:
        row_sql += f" LIMIT {int(args.row_limit)}"
    rows = conn.execute(row_sql, (sid,)).fetchall()
    cols = conn.execute(
        "SELECT colref, name, dokuwiki_class FROM struct_columns WHERE sid=? ORDER BY sort",
        (sid,),
    ).fetchall()
    row_pushed = row_failed = 0
    for pid, payload_json, existing_row_page in rows:
        row_sp = out_dir / f"{tbl}.row.{pid}.xml"
        if not row_sp.is_file():
            continue
        payload = _json.loads(payload_json)
        title = _struct_row_title(payload, cols, tbl, pid)
        storage = row_sp.read_text(encoding="utf-8")
        if existing_row_page:
            if not _struct_put_page(session, base, existing_row_page, title=title, storage=storage):
                conn.execute(
                    "UPDATE struct_rows SET status='FAILED', last_error='PUT row' WHERE sid=? AND pid=?",
                    (sid, pid),
                )
                row_failed += 1
                continue
            row_page_id = existing_row_page
        else:
            row_page_id = _struct_post_page(
                session, base, space_id, idx_page_id,
                title=title, storage=storage, sid=sid, pid=pid,
            )
            if not row_page_id:
                conn.execute(
                    "UPDATE struct_rows SET status='FAILED', last_error='POST row' WHERE sid=? AND pid=?",
                    (sid, pid),
                )
                row_failed += 1
                continue
            conn.execute(
                "UPDATE struct_rows SET confluence_page_id=?, status='UPLOADED' WHERE sid=? AND pid=?",
                (str(row_page_id), sid, pid),
            )
            conn.commit()
        _apply_page_labels(session, base, str(row_page_id), [f"dokuwiki-struct-{tbl}"])
        row_pushed += 1
        if row_pushed % 50 == 0:
            log(f"  ... rows pushed={row_pushed}")
    conn.commit()
    log(f"  완료: {tbl} index={idx_page_id} rows={len(rows)} (실패={row_failed})")
    return (True, row_pushed, row_failed)


def cmd_struct_upload(args: argparse.Namespace) -> int:
    """struct-convert 결과를 Confluence 에.

    --mode=native 면 먼저 --probe 로 API 가용성 확인.
    properties / snapshot 은 storage XML 을 페이지로 생성 (메인
    파이프라인의 cmd_upload 와 유사).
    """
    # 자격증명 검증은 _confluence_session 이 처리.
    if not args.space_key or not args.root_page_id:
        log("--space-key / --root-page-id 필요.")
        return 2

    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")
    space_id = _resolve_space_id(session, base, args.space_key)
    if not space_id:
        return 1

    if args.probe:
        return _struct_upload_probe_database_api(session, base, space_id, args)

    out_dir = Path("storage_struct")
    schemas = _struct_upload_select_schemas(conn, args)
    if not schemas:
        log("업로드 대상 schema 없음.")
        conn.close()
        return 0

    pushed = failed = row_pushed_total = row_failed_total = 0
    for sid, tbl, mode in schemas:
        log(f"=== {tbl} (mode={mode}, sid={sid}) ===")
        if mode == "snapshot":
            if _struct_upload_snapshot_schema(conn, session, base, space_id, args, sid, tbl, out_dir):
                pushed += 1
            else:
                failed += 1
        else:
            ok, rp, rf = _struct_upload_indexed_schema(
                conn, session, base, space_id, args, sid, tbl, mode, out_dir
            )
            row_pushed_total += rp
            row_failed_total += rf
            if ok:
                pushed += 1
            else:
                failed += 1

    log(f"struct-upload 완료: schemas pushed={pushed} failed={failed} / rows pushed={row_pushed_total} failed={row_failed_total}")
    conn.close()
    return 0 if failed == 0 and row_failed_total == 0 else 1


def _struct_put_page(session, base, page_id: str, *, title: str, storage: str) -> bool:
    """기존 페이지를 PUT 으로 갱신 (idempotent). version 자동 증가."""
    cur_ver = _get_page_version(session, base, page_id)
    if cur_ver is None:
        return False
    payload = {
        "id": str(page_id),
        "status": "current",
        "title": title,
        "body": {"representation": "storage", "value": storage},
        "version": {"number": cur_ver + 1},
    }
    r = _request_with_retry(session, "PUT", f"{base}/api/v2/pages/{page_id}", json=payload)
    return bool(r and r.status_code < 400)


def _struct_post_page(session, base, space_id: str, parent_id: str, *, title: str, storage: str, sid: int, pid: int | None = None) -> str | None:
    """새 페이지 POST. title 충돌 시 자동 disambiguate."""
    payload = {
        "spaceId": space_id,
        "parentId": parent_id,
        "title": title,
        "body": {"representation": "storage", "value": storage},
    }
    r = _request_with_retry(session, "POST", f"{base}/api/v2/pages", json=payload)
    if r is None or r.status_code >= 400:
        if r is not None and r.status_code == 400 and "title" in (r.text or "").lower():
            disambig = f"{title} ({pid})" if pid is not None else f"{title} ({sid})"
            payload["title"] = disambig
            r = _request_with_retry(session, "POST", f"{base}/api/v2/pages", json=payload)
    if r is None or r.status_code >= 400:
        log(f"    POST page 실패: {r.status_code if r else 'no resp'} body={(r.text if r else '')[:200]}")
        return None
    return r.json().get("id")


def _struct_build_index_xml(
    tbl: str, sid: int, mode: str, cols, row_count: int, db_id: str | None,
    base_url: str, space_key: str = "",
) -> str:
    """index 페이지의 storage XML 빌드. db_id 가 있으면 Database webui 링크 + 안내 박스 포함."""
    embed = ""
    if db_id and base_url:
        if space_key:
            href = f"{base_url.rstrip('/')}/spaces/{space_key}/database/{db_id}"
        else:
            href = f"{base_url.rstrip('/')}/database/{db_id}"
        embed = (
            "<ac:structured-macro ac:name=\"info\"><ac:rich-text-body>"
            f"<p><strong>Confluence Database</strong>: 이 schema 의 빈 Confluence Database 객체가 같은 공간에 있습니다 "
            f'(<a href="{href}">dwc-struct-{_h.escape(tbl)}</a>, id={_h.escape(db_id)}). '
            "Atlassian 의 Confluence Cloud Database API 가 컬럼/row 입력을 지원하면 자동 동기화될 예정. "
            "현재 데이터는 아래 Page Properties Report 로 표시.</p>"
            "</ac:rich-text-body></ac:structured-macro>"
        )
    col_info = "".join(
        f"<tr><td>col{cr}</td><td>{_h.escape(nm or '')}</td><td>{cls}</td></tr>"
        for cr, nm, cls in cols
    )
    return (
        f"<h1>{_h.escape(tbl)}</h1>"
        f"<p>DokuWiki struct schema → Confluence (mode={mode}). sid={sid}, "
        f"columns={len(cols)}, rows={row_count}.</p>"
        f"{embed}"
        "<h2>Columns</h2>"
        f"<table><tr><th>colref</th><th>label</th><th>dokuwiki class</th></tr>{col_info}</table>"
        "<h2>Rows</h2>"
        "<ac:structured-macro ac:name=\"detailssummary\">"
        f"<ac:parameter ac:name=\"cql\">label = \"dokuwiki-struct-{tbl}\"</ac:parameter>"
        "</ac:structured-macro>"
    )


def _struct_rewrite_index(conn, sid: int, tbl: str, mode: str, out_dir: Path, base_url: str = "", space_key: str = "") -> None:
    cols = conn.execute(
        "SELECT colref, name, dokuwiki_class FROM struct_columns WHERE sid=? ORDER BY sort",
        (sid,),
    ).fetchall()
    row = conn.execute(
        "SELECT confluence_db_id, row_count FROM struct_schemas WHERE sid=?", (sid,)
    ).fetchone()
    db_id = row[0] if row else None
    row_count = row[1] if row else 0
    bu = base_url or db_get_meta(conn, "confluence_base_url") or ""
    sk = space_key or db_get_meta(conn, "confluence_space_key") or ""
    (out_dir / f"{tbl}.index.xml").write_text(
        _struct_build_index_xml(tbl, sid, mode, cols, row_count, db_id, bu, sk),
        encoding="utf-8",
    )


# 패널의 시작을 알리는 h2 텍스트. Confluence storage 가 HTML 코멘트 마커를
# strip 하므로 본문에 *실제로 보이는* h2 heading 을 sentinel 로 사용.
# 패널은 항상 본문 끝에 부착되므로 별도 end marker 불필요 — 시작점부터 EOF 까지가 panel.
_STRUCT_EMBED_HEADER = "<h2>관련 struct 데이터</h2>"

# DokuWiki struct schema → "row 의 어느 컬럼이 페이지 binding 인지" 매핑.
# (colref, kind):  kind='wiki' → [[id|label]] 파싱, 'doku_id' → 값이 그대로 doku page id.
# 본 인스턴스 측정으로 결정 (struct-migration.md §2.3).
STRUCT_BINDINGS: dict[str, tuple[int, str]] = {
    "brevet_event":      (23, "wiki"),     # col23: [[:b:2019-s200d-1|동탄 200k]]
    "brevet_course":     (2,  "doku_id"),  # col2: '2019-s200d-1'  (event id)
    "brevet_uri_cppage": (1,  "doku_id"),  # col1: '2019-s200d-1'
    # brevet_place: 자체 page binding 없음 (장소명만 있음) — skip
}


def _struct_binding_target(payload: dict, colref: int, kind: str) -> str | None:
    v = payload.get(str(colref))
    if not v or isinstance(v, list):
        return None
    s = str(v).strip()
    if kind == "wiki":
        m = _WIKI_LINK_RE.match(s)
        if m:
            return m.group(1).lstrip(":")
        return None
    # doku_id
    return s.lstrip(":") or None


def cmd_struct_embed_on_bound_pages(args: argparse.Namespace) -> int:
    """각 bound page 의 본문 끝에 'Related struct data' 패널 (해당 row 페이지 목록 + Page Properties Report).

    마커 (<!-- struct-embed:start --> … <!-- struct-embed:end -->) 사이를 교체해 idempotent.
    """
    if not args.email or not args.api_token:
        log("자격증명 필요.")
        return 2
    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    # bound_page (doku_id) → [(tbl, pid, row_page_id, row_title)]
    bucket: dict[str, list[tuple[str, int, str, str]]] = {}
    schema_titles: dict[str, set[str]] = {}
    for tbl, (col, kind) in STRUCT_BINDINGS.items():
        sid_row = conn.execute("SELECT sid FROM struct_schemas WHERE tbl=?", (tbl,)).fetchone()
        if not sid_row:
            continue
        sid = sid_row[0]
        cols = conn.execute(
            "SELECT colref, name, dokuwiki_class FROM struct_columns WHERE sid=? ORDER BY sort",
            (sid,),
        ).fetchall()
        for pid, payload_json, row_page_id in conn.execute(
            "SELECT pid, payload_json, confluence_page_id FROM struct_rows "
            "WHERE sid=? AND status='UPLOADED' AND confluence_page_id IS NOT NULL ORDER BY pid",
            (sid,),
        ).fetchall():
            payload = _json.loads(payload_json)
            target = _struct_binding_target(payload, col, kind)
            if not target:
                continue
            title = _struct_row_title(payload, cols, tbl, pid)
            bucket.setdefault(target, []).append((tbl, pid, row_page_id, title))
            schema_titles.setdefault(target, set()).add(tbl)

    if args.only_doku:
        bucket = {k: v for k, v in bucket.items() if k == args.only_doku}
    if not bucket:
        log("bound page 없음 — STRUCT_BINDINGS 와 데이터 확인.")
        return 0
    log(f"대상 bound page: {len(bucket)}개")

    pushed = failed = unresolved = 0
    for doku_id, rows in sorted(bucket.items()):
        resolved = _struct_resolve_page(conn, doku_id)
        if not resolved:
            log(f"  [SKIP] {doku_id} → Confluence 미존재")
            unresolved += 1
            continue
        page_id, title = resolved

        # current storage body 가져오기
        r = _request_with_retry(
            session, "GET", f"{base}/api/v2/pages/{page_id}", params={"body-format": "storage"}
        )
        if r is None or r.status_code >= 400:
            log(f"  [SKIP] {doku_id} GET 실패")
            failed += 1
            continue
        js = r.json()
        cur_body = (js.get("body") or {}).get("storage", {}).get("value", "") or ""
        cur_ver = js.get("version", {}).get("number", 1)

        # 임베드 panel 빌드
        per_schema_items: dict[str, list[str]] = {}
        for tbl, pid, rpid, rtitle in rows:
            per_schema_items.setdefault(tbl, []).append(
                f'<li><ac:link><ri:page ri:content-title="{_h.escape(rtitle, quote=True)}"/>'
                f'<ac:plain-text-link-body><![CDATA[{rtitle}]]></ac:plain-text-link-body></ac:link></li>'
            )
        schema_sections = []
        for tbl, items in per_schema_items.items():
            idx_id = conn.execute(
                "SELECT COALESCE(properties_index_page_id, snapshot_page_id) FROM struct_schemas WHERE tbl=?",
                (tbl,),
            ).fetchone()
            idx_link = ""
            if idx_id and idx_id[0]:
                idx_link = (
                    f' (<ac:link><ri:page ri:content-title="dokuwiki struct: {_h.escape(tbl, quote=True)}"/>'
                    f'<ac:plain-text-link-body><![CDATA[전체 인덱스]]></ac:plain-text-link-body></ac:link>)'
                )
            schema_sections.append(
                f"<h3>{_h.escape(tbl)} ({len(items)}){idx_link}</h3><ul>{''.join(items)}</ul>"
            )
        panel = (
            f"{_STRUCT_EMBED_HEADER}"
            "<p>DokuWiki struct plugin 으로 관리되던 데이터가 별도 Confluence 페이지로 마이그레이션되었습니다.</p>"
            f"{''.join(schema_sections)}"
        )

        # 기존 panel 이 있으면 (h2 sentinel) 그 위치부터 본문 끝까지 잘라내고 panel 으로 교체.
        # panel 은 본문 끝에 항상 append 되므로 별도 end marker 불필요.
        start = cur_body.find(_STRUCT_EMBED_HEADER)
        if start >= 0:
            new_body = cur_body[:start] + panel
        else:
            new_body = cur_body + panel

        # PUT
        payload = {
            "id": str(page_id),
            "status": "current",
            "title": title,
            "body": {"representation": "storage", "value": new_body},
            "version": {"number": cur_ver + 1},
        }
        r = _request_with_retry(session, "PUT", f"{base}/api/v2/pages/{page_id}", json=payload)
        if r is None or r.status_code >= 400:
            log(f"  [FAIL] {doku_id}: {r.status_code if r else 'no resp'}")
            failed += 1
            continue
        pushed += 1
        if pushed % 20 == 0:
            log(f"  ... bound pages pushed={pushed}")

    log(f"struct-embed 완료: pushed={pushed} failed={failed} unresolved={unresolved}")
    conn.close()
    return 0 if failed == 0 else 1


def cmd_struct_status(args: argparse.Namespace) -> int:
    """struct 트랙의 진행 상황 출력 (read-only).

    state.db 조회만 — 변경 없음. `struct_schemas` 의 chosen_mode (snapshot/
    properties/native) + row_count + confluence_db_id + status, `struct_rows`
    의 상태별 카운트, `struct_columns` 의 dokuwiki_class 분포 요약."""
    conn = db_connect(args.db)
    print("==== struct_schemas ====")
    for sid, tbl, rc, cc, mode, status, db_id, idx_id in conn.execute(
        "SELECT sid, tbl, row_count, column_count, COALESCE(chosen_mode,'-'), status, "
        "COALESCE(confluence_db_id,'-'), COALESCE(properties_index_page_id, snapshot_page_id, '-') "
        "FROM struct_schemas ORDER BY tbl"
    ).fetchall():
        print(f"  sid={sid:3} tbl={tbl:25} cols={cc:2} rows={rc:5} mode={mode:10} status={status:9} db={db_id:>11} idx={idx_id}")
    print()
    print("==== struct_rows status 분포 ====")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM struct_rows GROUP BY status"
    ).fetchall()
    for status, n in rows:
        print(f"  {status:14} {n}")
    print()
    print("==== references ====")
    for kind, n in conn.execute(
        "SELECT target_kind, COUNT(*) FROM struct_references GROUP BY target_kind"
    ).fetchall():
        print(f"  {kind:14} {n}")
    print()
    print("==== references resolved 분포 ====")
    for resolved, n in conn.execute(
        "SELECT resolved, COUNT(*) FROM struct_references GROUP BY resolved"
    ).fetchall():
        print(f"  resolved={resolved}: {n}")
    conn.close()
    return 0


# § rewrite-oversized-pages: 본문 거부된 페이지 → skeleton + 첨부

_EMPTY_ATTACHMENT_LINK_RE = _re.compile(
    r'<ac:link>\s*<ri:attachment\s+ri:filename="\s*"\s*/?>'
    r'(?:\s*</ri:attachment>)?\s*'
    r'<ac:(?:plain-text-)?link-body>(.*?)</ac:(?:plain-text-)?link-body>\s*</ac:link>',
    _re.S,
)


def _sanitize_empty_attachment_links(xml: str) -> str:
    """`<ac:link><ri:attachment ri:filename=""></ri:attachment>...</ac:link>`
    같이 *빈 filename* 의 attachment link 를 평문 (link-body 내용) 으로 격하.

    원인: 변환기가 `[[/_media/...]]` 같은 *internal media URL* 을 첨부 link
    로 변환 시도 → 파일명 추출 실패 (빈 string). Confluence storage 가
    빈 ri:filename 을 500 INTERNAL_SERVER_ERROR 로 거부.

    영향: 본 sanitize 가 없으면 storage 한도 초과 분할 시점에도 chunk PUT
    fail. 변환기 자체 fix 가 본질적 해법이나 본 sanitize 는 *후행 정리*.
    """
    def repl(m: _re.Match) -> str:
        body = m.group(1).strip()
        # CDATA 안 텍스트 추출
        body = _re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', body, flags=_re.S)
        return body or ""
    return _EMPTY_ATTACHMENT_LINK_RE.sub(repl, xml)


def _split_storage_by_heading(
    xml: str,
    *,
    max_chunk: int = 100_000,
    start_level: int = 2,
) -> list[tuple[str, str]]:
    """본문을 H1/H2/H3 경계로 분할.

    1. `start_level` (default H2) 단위 경계 → chunk 들
    2. chunk 가 `max_chunk` 보다 크면 *다음 hN* 으로 재귀 분할
    3. 인접 chunk 가 max_chunk 안에 들어가면 누적 그룹화
    4. heading 없으면 단일 chunk 반환

    Returns: list of (label, chunk_xml).
    """
    pat = _re.compile(rf'<h{start_level}[^>]*>([^<]*)</h{start_level}>')
    matches = list(pat.finditer(xml))
    if not matches:
        if start_level < 4:
            return _split_storage_by_heading(
                xml, max_chunk=max_chunk, start_level=start_level + 1
            )
        return [("전체", xml)]

    chunks: list[tuple[str, str]] = []
    prefix = xml[: matches[0].start()]
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(xml)
        body = xml[start:end]
        if i == 0 and prefix.strip():
            body = prefix + body
        label = (m.group(1) or "").strip() or f"섹션 {i + 1}"
        chunks.append((label, body))

    refined: list[tuple[str, str]] = []
    for label, body in chunks:
        if len(body) > max_chunk and start_level < 4:
            sub = _split_storage_by_heading(
                body, max_chunk=max_chunk, start_level=start_level + 1
            )
            if len(sub) <= 1:
                refined.append((label, body))
            else:
                for sl, sb in sub:
                    refined.append((f"{label} – {sl}", sb))
        else:
            refined.append((label, body))

    grouped: list[tuple[str, str]] = []
    buf_label: str | None = None
    buf_body = ""
    for label, body in refined:
        if buf_label is None:
            buf_label, buf_body = label, body
            continue
        if len(buf_body) + len(body) <= max_chunk:
            buf_body += body
        else:
            grouped.append((buf_label, buf_body))
            buf_label, buf_body = label, body
    if buf_label is not None:
        grouped.append((buf_label, buf_body))

    return grouped


def cmd_split_oversize(args: argparse.Namespace) -> int:
    """본문 한도 초과 페이지를 H 경계로 분할.

    상위 (parent) 페이지: 짧은 info + Children Display 매크로.
    하위 (child) 페이지: `--max-chunk` 이하의 본문.

    `cmd_rewrite_oversized_pages` (C 모드: skeleton + zip 첨부) 와 달리
    *원본 본문을 잃지 않음* — Confluence 측에서 본문 자체로 탐색 가능.
    """
    if not args.dry_run:
        session = _confluence_session(args)
        if session is None:
            return 2
    else:
        session = None
    base = args.base_url.rstrip("/") if getattr(args, "base_url", None) else ""

    conn = db_connect(args.db)
    db_init(conn)

    if args.only:
        # macOS APFS 의 한국어 doku_id 가 NFD 로 저장됨 (shell argument 는
        # 보통 NFC) — 양쪽 normalize 폼 모두 시도.
        import unicodedata as _ud
        nfc = _ud.normalize("NFC", args.only)
        nfd = _ud.normalize("NFD", args.only)
        rows = conn.execute(
            "SELECT doku_id, title, storage_path, confluence_page_id "
            "FROM pages WHERE doku_id IN (?, ?)",
            (nfc, nfd),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doku_id, title, storage_path, confluence_page_id "
            "FROM pages WHERE status='FAILED' AND last_error LIKE '%no resp%' "
            "AND storage_path IS NOT NULL"
        ).fetchall()
    if not rows:
        log("split-oversize 대상 페이지 없음.")
        conn.close()
        return 0

    log(f"split-oversize 대상: {len(rows)} 페이지 (max_chunk={args.max_chunk:,d}b)")

    space_id = None
    if not args.dry_run and args.space_key:
        space_id = _resolve_space_id(session, base, args.space_key)
        if not space_id:
            log("--space-key 해결 실패.")
            conn.close()
            return 2

    pushed = failed_count = 0
    for doku_id, title, storage_path, cid in rows:
        sp = Path(storage_path)
        if not sp.is_file():
            log(f"  [SKIP] {doku_id}: storage 파일 없음")
            continue
        if not cid:
            log(f"  [SKIP] {doku_id}: confluence_page_id 없음")
            continue

        body = sp.read_text(encoding="utf-8")
        body = _sanitize_empty_attachment_links(body)
        chunks = _split_storage_by_heading(body, max_chunk=args.max_chunk)
        if len(chunks) <= 1:
            log(f"  [SKIP] {doku_id}: heading 분할 불가 (단일 chunk)")
            continue

        log(f"  [{doku_id}] {len(chunks)} chunks (parent cid={cid})")
        for lbl, ch in chunks:
            log(f"    - {lbl[:60]} ({len(ch):,d}b)")

        if args.dry_run:
            continue

        # parent 의 기존 children 을 title -> cid 로 인덱스 (idempotent 재실행)
        existing_children: dict[str, str] = {}
        cursor_url = f"{base}/api/v2/pages/{cid}/children?limit=250"
        while cursor_url:
            cr = _request_with_retry(session, "GET", cursor_url)
            if cr is None or cr.status_code >= 400:
                break
            j = cr.json()
            for r in j.get("results", []):
                existing_children[r["title"]] = str(r["id"])
            nxt = j.get("_links", {}).get("next")
            cursor_url = f"{base}{nxt}" if nxt else None

        # 각 chunk → child 페이지 POST (없으면) 또는 PUT (있으면)
        child_records: list[tuple[str, str]] = []
        any_fail = False
        title_str = title or doku_id
        for idx, (lbl, ch_xml) in enumerate(chunks, 1):
            child_title = f"{title_str} – {idx:02d}. {lbl}"
            existing_cid = existing_children.get(child_title)
            if existing_cid:
                # 기존 child 페이지 PUT 갱신
                cv = _get_page_version(session, base, existing_cid)
                if cv is None:
                    log(f"    [FAIL] chunk {idx}: 기존 child ver 조회 실패")
                    any_fail = True
                    break
                resp = _request_with_retry(
                    session, "PUT", f"{base}/api/v2/pages/{existing_cid}",
                    json={
                        "id": existing_cid, "status": "current", "title": child_title,
                        "body": {"representation": "storage", "value": ch_xml},
                        "version": {"number": cv + 1},
                    },
                )
                if resp is None or resp.status_code >= 400:
                    err = f"PUT {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:200]}"
                    log(f"    [FAIL] chunk {idx}: {err}")
                    any_fail = True
                    break
                child_records.append((child_title, existing_cid))
                log(f"    [UPDATE] chunk {idx} -> page {existing_cid} (v{cv+1})")
            else:
                payload = {
                    "spaceId": space_id,
                    "parentId": cid,
                    "title": child_title,
                    "body": {"representation": "storage", "value": ch_xml},
                }
                resp = _request_with_retry(
                    session, "POST", f"{base}/api/v2/pages", json=payload
                )
                if resp is None or resp.status_code >= 400:
                    err = f"create {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:200]}"
                    log(f"    [FAIL] chunk {idx}: {err}")
                    any_fail = True
                    break
                child_id = str(resp.json()["id"])
                child_records.append((child_title, child_id))
                log(f"    [CREATE] chunk {idx} -> page {child_id}")

        if any_fail or not child_records:
            failed_count += 1
            continue

        # parent 본문 = info + Children Display
        new_parent_body = (
            '<ac:structured-macro ac:name="info">'
            "<ac:rich-text-body>"
            f"<p>본문이 Confluence storage 한도를 초과해 {len(child_records)}개 "
            f"자식 페이지로 분할됨. 자식 페이지 목록:</p>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
            '<ac:structured-macro ac:name="children">'
            '<ac:parameter ac:name="all">true</ac:parameter>'
            "</ac:structured-macro>"
        )
        cur_ver = _get_page_version(session, base, cid)
        if cur_ver is None:
            log(f"    [FAIL] {doku_id}: parent 버전 조회 실패")
            failed_count += 1
            continue
        resp = _request_with_retry(
            session, "PUT", f"{base}/api/v2/pages/{cid}",
            json={
                "id": cid, "status": "current", "title": title_str,
                "body": {"representation": "storage", "value": new_parent_body},
                "version": {"number": cur_ver + 1},
            },
        )
        if resp is None or resp.status_code >= 400:
            err = f"parent PUT {resp.status_code if resp else 'no resp'}: {(resp.text if resp else '')[:200]}"
            log(f"    [FAIL] {doku_id}: {err}")
            failed_count += 1
            continue

        new_hash = sha256_bytes(new_parent_body.encode("utf-8"))
        conn.execute(
            "UPDATE pages SET status='UPLOADED', last_error=NULL, confluence_version=?, "
            "uploaded_at=?, last_checked_at=? WHERE doku_id=?",
            (cur_ver + 1, now_iso(), now_iso(), doku_id),
        )
        db_set_meta(conn, f"uploaded_hash:{doku_id}", new_hash)
        import json as _json
        db_set_meta(conn, f"split_into:{doku_id}", _json.dumps(child_records, ensure_ascii=False))
        conn.commit()
        pushed += 1
        log(f"  [SPLIT] {doku_id} -> {len(child_records)} children, parent v{cur_ver + 1}")

    log(f"split-oversize 완료: split={pushed} failed={failed_count}")
    conn.close()
    return 0 if failed_count == 0 else 1


def cmd_rewrite_oversized_pages(args: argparse.Namespace) -> int:
    """
    Confluence 본문 한계를 넘은 페이지 (status='FAILED' AND
    last_error LIKE '%no resp%') 를 skeleton 본문 + 원본 storage XML
    첨부로 처리. 상세: docs/oversized-pages.md (C 모드).
    """
    # 자격증명 검증은 _confluence_session 이 처리.
    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    # 대상 식별
    if args.only:
        rows = conn.execute(
            "SELECT doku_id, title, parent_doku_id, storage_path, confluence_page_id "
            "FROM pages WHERE doku_id=?", (args.only,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doku_id, title, parent_doku_id, storage_path, confluence_page_id "
            "FROM pages WHERE status='FAILED' AND last_error LIKE '%no resp%'"
        ).fetchall()
    if not rows:
        log("대상 페이지 없음.")
        return 0
    log(f"oversized-page fallback 대상: {len(rows)} 페이지")

    space_id = _resolve_space_id(session, base, args.space_key) if args.space_key else None
    if not space_id and not args.only:
        log("--space-key 필요 (또는 환경변수 CONFLUENCE_SPACE_KEY).")
        return 2

    import zipfile
    import io

    pushed = failed = 0
    for doku_id, title, parent_doku_id, storage_path, cur_page_id in rows:
        if not storage_path or not Path(storage_path).is_file():
            log(f"  [SKIP] {doku_id}: storage 파일 없음")
            continue

        # 부모 confluence_page_id 결정
        parent_page_id = args.root_page_id
        if parent_doku_id:
            prow = conn.execute(
                "SELECT confluence_page_id FROM pages WHERE doku_id=?", (parent_doku_id,)
            ).fetchone()
            if prow and prow[0]:
                parent_page_id = prow[0]

        # storage XML 크기 + li 카운트
        body = Path(storage_path).read_text(encoding="utf-8")
        size_kb = len(body.encode("utf-8")) / 1024
        li_count = body.count("<li")

        # skeleton 본문
        skeleton = (
            "<ac:structured-macro ac:name='info'>"
            "<ac:parameter ac:name='title'>본문 큰 페이지</ac:parameter>"
            "<ac:rich-text-body>"
            f"<p>이 페이지의 원본 본문은 Confluence 의 storage parsing 한계를 "
            f"넘어 직접 표시되지 않습니다.</p>"
            "<ul>"
            f"<li>크기: 약 {size_kb:.0f} KB</li>"
            f"<li>&lt;li&gt; 개수: {li_count:,}</li>"
            f"<li>원본 본문은 페이지 첨부의 <code>{doku_id}.xml.zip</code> 에 보존됨</li>"
            f"<li>호스트 백업: P4 depot 의 <code>storage/{doku_id.replace(':', '/')}.xml</code></li>"
            "</ul>"
            "</ac:rich-text-body>"
            "</ac:structured-macro>"
        )

        # POST 또는 PUT
        if cur_page_id:
            cur_ver = _get_page_version(session, base, cur_page_id)
            if cur_ver is None:
                failed += 1
                continue
            payload = {
                "id": cur_page_id, "status": "current", "title": title,
                "body": {"representation": "storage", "value": skeleton},
                "version": {"number": cur_ver + 1},
            }
            resp = _request_with_retry(
                session, "PUT", f"{base}/api/v2/pages/{cur_page_id}", json=payload
            )
            new_page_id = cur_page_id
        else:
            payload = {
                "spaceId": space_id,
                "parentId": parent_page_id,
                "title": title,
                "body": {"representation": "storage", "value": skeleton},
            }
            resp = _request_with_retry(
                session, "POST", f"{base}/api/v2/pages", json=payload
            )
            if resp is None or resp.status_code >= 400:
                # title 충돌 — disambiguation 재시도
                if resp is not None and resp.status_code == 400 and "title" in (resp.text or "").lower():
                    payload["title"] = f"{title} ({doku_id})"
                    resp = _request_with_retry(
                        session, "POST", f"{base}/api/v2/pages", json=payload
                    )
                    title = payload["title"]
            if resp is None or resp.status_code >= 400:
                err = f"skeleton create: {resp.status_code if resp else 'no resp'}"
                log(f"  [FAIL] {doku_id}: {err}")
                failed += 1
                continue
            new_page_id = str(resp.json()["id"])

        # storage XML 을 zip 으로
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{doku_id}.xml", body)
        zip_buf.seek(0)
        zip_bytes = zip_buf.read()
        zip_name = f"{doku_id.replace(':', '_')}.xml.zip"

        # 첨부 업로드 — v1 multipart
        # _request_with_retry 우회 의도: multipart streaming + 본 작업은
        # cmd_rewrite_oversized 의 *대형* 첨부 (10MB+ zip) 라 retry 시 비용 큼.
        # 실패 시 호출자 측 status='FAILED' 마킹.
        from requests_toolbelt.multipart import encoder as tb_encoder
        m = tb_encoder.MultipartEncoder(
            fields={"file": (zip_name, io.BytesIO(zip_bytes), "application/zip")}
        )
        att_resp = session.post(
            f"{base}/rest/api/content/{new_page_id}/child/attachment",
            headers={
                "X-Atlassian-Token": "no-check",
                "Content-Type": m.content_type,
            },
            data=m, timeout=120,
        )
        att_ok = att_resp.status_code < 400 or (
            att_resp.status_code == 400 and "same file name" in (att_resp.text or "")
        )

        # state.db 갱신
        conn.execute(
            "UPDATE pages SET confluence_page_id=?, status='UPLOADED', "
            "last_error=NULL, uploaded_at=?, last_checked_at=?, title=? WHERE doku_id=?",
            (new_page_id, now_iso(), now_iso(), title, doku_id),
        )
        db_set_meta(conn, f"large_body_fallback:{doku_id}", "C-mode skeleton + storage zip")
        conn.commit()

        log(f"  [FALLBACK] {doku_id} -> page {new_page_id}, storage zip attached={att_ok}")
        pushed += 1

    log(f"rewrite-oversized-pages 완료: pushed={pushed} failed={failed}")
    conn.close()
    return 0 if failed == 0 else 1


# § rewrite-oversized: OVERSIZED 첨부 reference 를 메타 박스로

def cmd_rewrite_oversized(args: argparse.Namespace) -> int:
    """
    100MB 초과 첨부 (status='OVERSIZED') 의 reference 를 본문에서
    'note' 매크로 메타 박스로 치환한다. 첨부 파일 자체는 Confluence
    에 안 들어가고 (사이즈 한도) 호스트 P4 백업에 보존. 본문에선
    어떤 파일이 어디에 있었는지 시각적으로 표시되어 깨진 reference
    가 아닌 *대용량 첨부 미이전* 안내로 보인다.

    상세 옵션 매트릭스: docs/oversized-attachments.md §3.
    이 함수는 그 중 옵션 B (note macro footer) 를 구현.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("beautifulsoup4 가 필요합니다: pip install -r requirements.txt")
        return 2

    conn = db_connect(args.db)
    db_init(conn)

    rows = conn.execute(
        "SELECT page_doku_id, media_id, size, last_error "
        "FROM attachments WHERE status='OVERSIZED' ORDER BY page_doku_id, media_id"
    ).fetchall()
    if not rows:
        log("OVERSIZED 첨부 없음.")
        return 0
    log(f"OVERSIZED 첨부: {len(rows)}건 (영향 페이지 식별 중)")

    # 페이지별로 묶기
    by_page: dict[str, list[tuple[str, int | None, str | None]]] = {}
    for page_id, media_id, size, err in rows:
        by_page.setdefault(page_id, []).append((media_id, size, err))

    changed_pages: list[str] = []
    for page_id, items in by_page.items():
        # large body fallback 페이지는 본문이 skeleton 으로 교체되어 ri:attachment
        # reference 자체가 없다. 또한 본문 PUT 도 거부되므로 skip.
        if db_get_meta(conn, f"large_body_fallback:{page_id}"):
            log(f"  [SKIP] {page_id}: large body fallback 페이지 (skeleton 본문)")
            continue
        row = conn.execute(
            "SELECT storage_path FROM pages WHERE doku_id=?", (page_id,)
        ).fetchone()
        if not row or not row[0]:
            log(f"  [SKIP] {page_id}: storage 없음")
            continue
        sp = Path(row[0])
        if not sp.is_file():
            continue
        xml = sp.read_text(encoding="utf-8")
        soup = BeautifulSoup(xml, "html.parser")

        # 각 OVERSIZED 첨부의 filename 으로 ri:attachment 찾기
        applied = 0
        for media_id, size, _err in items:
            filename = _media_filename(media_id)
            for ri in list(soup.find_all("ri:attachment")):
                if ri.get("ri:filename") != filename:
                    continue
                # ri:attachment 의 부모가 <ac:image> 또는 <ac:link>. 그 부모를 통째 매크로로 교체.
                parent = ri.find_parent(["ac:image", "ac:link"])
                if parent is None:
                    continue
                size_mb = f"{size / (1024*1024):.1f}" if size else "?"
                note = BeautifulSoup(
                    "<ac:structured-macro ac:name='note'>"
                    "<ac:parameter ac:name='title'>대용량 첨부 미이전</ac:parameter>"
                    "<ac:rich-text-body>"
                    f"<p>이 자리에는 원래 <code>{filename}</code> 이 있었습니다.</p>"
                    "<ul>"
                    f"<li>크기: 약 {size_mb} MB</li>"
                    "<li>원본 위치: 호스트의 DokuWiki 백업 (P4 depot)</li>"
                    "<li>이전되지 않은 이유: Confluence Cloud 단일 첨부 100MB 한도</li>"
                    "</ul>"
                    "</ac:rich-text-body>"
                    "</ac:structured-macro>",
                    "html.parser",
                )
                parent.replace_with(note)
                applied += 1

        if applied:
            new_xml = "".join(str(c) for c in soup.children)
            new_xml = _re.sub(r"<(br|hr|img)([^>]*?)(?<!/)\s*>", r"<\1\2/>", new_xml)
            new_hash = sha256_bytes(new_xml.encode("utf-8"))
            sp.write_text(new_xml, encoding="utf-8")
            conn.execute(
                "UPDATE pages SET content_hash=?, last_checked_at=? WHERE doku_id=?",
                (new_hash, now_iso(), page_id),
            )
            changed_pages.append(page_id)
            log(f"  [{page_id}] {applied}건의 OVERSIZED 참조 → note 박스")

    conn.commit()
    log(f"본문 갱신된 페이지: {len(changed_pages)}")

    if args.no_upload:
        log("--no-upload — storage 만 갱신, Confluence PUT 건너뜀")
        return 0

    if not changed_pages:
        return 0

    if not args.email or not args.api_token:
        log("자격증명 누락 — Confluence PUT 건너뜀. 다음 upload 호출이 자동 PUT 처리.")
        return 2

    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")
    failed = pushed = 0
    for doku_id in changed_pages:
        row = conn.execute(
            "SELECT confluence_page_id, title, content_hash, storage_path "
            "FROM pages WHERE doku_id=?",
            (doku_id,),
        ).fetchone()
        if not row or not row[0]:
            continue
        cid, title, ch, sp = row
        cur_ver = _get_page_version(session, base, cid)
        if cur_ver is None:
            log(f"  [FAIL] {doku_id}: 버전 조회 실패")
            failed += 1
            continue
        body = Path(sp).read_text(encoding="utf-8")
        payload = {
            "id": cid, "status": "current", "title": title,
            "body": {"representation": "storage", "value": body},
            "version": {"number": cur_ver + 1},
        }
        resp = _request_with_retry(
            session, "PUT", f"{base}/api/v2/pages/{cid}", json=payload
        )
        if resp is None or resp.status_code >= 400:
            failed += 1
            log(f"  [FAIL] {doku_id}: {resp.status_code if resp else 'no resp'}")
            continue
        conn.execute(
            "UPDATE pages SET confluence_version=?, uploaded_at=?, last_checked_at=? WHERE doku_id=?",
            (cur_ver + 1, now_iso(), now_iso(), doku_id),
        )
        db_set_meta(conn, f"uploaded_hash:{doku_id}", ch or "")
        conn.commit()
        pushed += 1
        log(f"  [REWRITTEN] {doku_id} -> v{cur_ver + 1}")

    log(f"rewrite-oversized 완료: pushed={pushed} failed={failed}")
    conn.close()
    return 0 if failed == 0 else 1


# § 보조: audit (dokuwiki vs Confluence 비교)

def _extract_visible_text(html_or_xml: str) -> str:
    """텍스트만 추출 + 정규화. dokuwiki HTML 와 Confluence storage/view
    양쪽에 동일하게 적용해 비교 가능한 형태로."""
    from bs4 import BeautifulSoup

    s = BeautifulSoup(html_or_xml, "html.parser")
    # 위험/노이즈 태그 제거 (dokuwiki 쪽 chrome 등)
    for tag in ("script", "style", "link", "meta", "noscript", "iframe",
                "head", "form", "input", "button", "select", "option"):
        for t in s.find_all(tag):
            t.decompose()
    # dokuwiki chrome / EDIT 코멘트 제거
    from bs4 import Comment
    for c in s.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()
    for sel_id in ("dokuwiki__site", "dokuwiki__top", "dokuwiki__header",
                   "dokuwiki__footer", "dokuwiki__pagetools",
                   "dokuwiki__usertools", "dokuwiki__sitetools"):
        for t in s.find_all(id=sel_id):
            t.decompose()
    for a in s.find_all("a", class_="secedit"):
        a.decompose()
    for div in s.find_all("div", class_="toc"):
        div.decompose()
    text = s.get_text(separator=" ", strip=True)
    # 공백 정규화
    return _re.sub(r"\s+", " ", text).strip()


def _confluence_get_page_body(session, base_url, page_id, body_format: str = "storage") -> str | None:
    """Confluence v2 페이지의 body 를 받음.

    body_format: storage / view / atlas_doc_format / export_view ...
    """
    resp = _request_with_retry(
        session, "GET",
        f"{base_url}/api/v2/pages/{page_id}",
        params={"body-format": body_format, "include-version": "true"},
    )
    if resp is None or resp.status_code >= 400:
        return None
    data = resp.json()
    body = data.get("body", {})
    if body_format in body and isinstance(body[body_format], dict):
        return body[body_format].get("value", "")
    if "storage" in body and isinstance(body["storage"], dict):
        return body["storage"].get("value", "")
    return ""


def _structural_features(html_or_xml: str, is_storage: bool) -> dict[str, int]:
    """페이지 본문에서 구조적 특징 카운트 (텍스트 + 형식 + 이미지 + 링크 +
    첨부 + 코드 블록 + 표 + 리스트 + callout/task 매크로).

    dokuwiki HTML 과 Confluence storage 양쪽에 같은 함수를 호출해
    is_storage 플래그로 분기. 비교에 쓸 *대표성 있는* 카운트만 추출.
    """
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(html_or_xml, "html.parser")
    # 노이즈/chrome 제거 (dokuwiki 측만 의미)
    if not is_storage:
        for tag in ("script", "style", "link", "meta", "noscript", "iframe",
                    "form", "input", "head"):
            for t in soup.find_all(tag):
                t.decompose()
        for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
            c.extract()
        for sel in ("dokuwiki__site", "dokuwiki__top", "dokuwiki__header",
                    "dokuwiki__footer", "dokuwiki__pagetools",
                    "dokuwiki__usertools", "dokuwiki__sitetools"):
            for t in soup.find_all(id=sel):
                t.decompose()
        for a in soup.find_all("a", class_="secedit"):
            a.decompose()
        for div in soup.find_all("div", class_="toc"):
            div.decompose()
        for div in soup.find_all(id="dw__toc"):
            div.decompose()

    f: dict[str, int] = {}
    # 헤딩
    for lv in range(1, 7):
        f[f"h{lv}"] = len(soup.find_all(f"h{lv}"))
    # 인라인 포맷팅
    for tag in ("strong", "em", "code", "sub", "sup", "del", "u"):
        f[tag] = len(soup.find_all(tag))
    # 표 / 리스트
    f["table"] = len(soup.find_all("table"))
    f["tr"] = len(soup.find_all("tr"))
    f["th"] = len(soup.find_all("th"))
    f["td"] = len(soup.find_all("td"))
    f["ul"] = len(soup.find_all("ul"))
    f["ol"] = len(soup.find_all("ol"))
    f["li"] = len(soup.find_all("li"))
    f["blockquote"] = len(soup.find_all("blockquote"))
    f["br"] = len(soup.find_all("br"))
    f["hr"] = len(soup.find_all("hr"))

    if is_storage:
        # Confluence: <ac:image>, <ac:link><ri:page>, <ac:link><ri:attachment>
        f["image_internal"] = len(soup.find_all("ac:image"))
        # <img> (외부 이미지 그대로 통과한 케이스)
        f["image_external"] = sum(
            1 for img in soup.find_all("img")
            if str(img.get("src", "")).startswith(("http://", "https://"))
        )
        f["image_total"] = f["image_internal"] + f["image_external"]

        page_links = 0
        attach_links = 0
        for a in soup.find_all("ac:link"):
            if a.find("ri:page"):
                page_links += 1
            if a.find("ri:attachment"):
                attach_links += 1
        # placeholder (S7 미수행 잔존) 도 page_link 로 합산
        placeholder = sum(
            1 for a in soup.find_all("a")
            if str(a.get("href", "")).startswith("dwc-link:")
        )
        f["page_link_placeholder"] = placeholder
        f["page_link"] = page_links + placeholder
        f["attachment_link"] = attach_links

        f["external_link"] = sum(
            1 for a in soup.find_all("a")
            if str(a.get("href", "")).startswith(("http://", "https://"))
        )
        # storage 안 smiley 잔여 (변환 누락 검증용)
        f["smiley"] = sum(
            1 for img in soup.find_all("img")
            if "smiley" in (img.get("class") or [])
        )

        # 매크로
        for name in ("info", "tip", "note", "warning", "panel", "code"):
            f[f"macro_{name}"] = sum(
                1 for m in soup.find_all("ac:structured-macro")
                if m.get("ac:name") == name
            )
        f["task_list"] = len(soup.find_all("ac:task-list"))
        f["task"] = len(soup.find_all("ac:task"))
        # 텍스트 마커 폴백 (mixed todo)
        text_all = soup.get_text()
        f["task_text_marker"] = len(_re.findall(r"\[(?:x| )\] ", text_all))
    else:
        # DokuWiki: smiley 와 일반 미디어 분리, 외부 proxy 와 내부 분리
        internal_img = 0
        external_img = 0
        smiley_count = 0
        for img in soup.find_all("img"):
            classes = img.get("class") or []
            if "smiley" in classes:
                smiley_count += 1
                continue
            src = str(img.get("src", ""))
            from urllib.parse import urlparse, parse_qs
            p = urlparse(src)
            q = parse_qs(p.query)
            m = q.get("media", [""])[0] if "media" in q else ""
            if m.startswith(("http://", "https://")):
                external_img += 1
            elif src.startswith(("/_media/", "/_detail/")):
                internal_img += 1
            elif "fetch.php" in p.path:
                internal_img += 1
            elif src.startswith(("http://", "https://")):
                external_img += 1
            else:
                external_img += 1
        f["image_internal"] = internal_img
        f["image_external"] = external_img
        f["smiley"] = smiley_count
        # 비교용 image_total: smiley 는 emoji 로 변환되어 text 가 되므로 제외
        f["image_total"] = internal_img + external_img

        page_links = 0
        attach_links = 0
        external_links = 0
        from urllib.parse import urlparse, parse_qs
        for a in soup.find_all("a"):
            classes = a.get("class") or []
            href = str(a.get("href", ""))
            p = urlparse(href)
            q = parse_qs(p.query)
            media_val = q.get("media", [""])[0] if "media" in q else ""
            # 외부 미디어 proxy 또는 외부 URL 은 external 로 (Confluence 측에서도 외부 a)
            is_external_url = href.startswith(("http://", "https://"))
            is_external_media_proxy = media_val.startswith(("http://", "https://"))
            if a.get("data-wiki-id") or any(c.startswith("wikilink") for c in classes):
                page_links += 1
            elif is_external_media_proxy or is_external_url:
                external_links += 1
            elif any(c in ("media", "mediafile") or c.startswith("mf_") for c in classes):
                attach_links += 1
            elif "media=" in href or href.startswith(("/_media/", "/_detail/", "/lib/exe/fetch.php")):
                attach_links += 1
        f["page_link"] = page_links
        f["page_link_placeholder"] = 0
        f["attachment_link"] = attach_links
        f["external_link"] = external_links

        # 코드 블록
        f["macro_code"] = len(
            [pre for pre in soup.find_all("pre")
             if pre.get("class") and ("code" in pre.get("class") or "file" in pre.get("class"))]
        )
        # callout 매크로 — dokuwiki 의 의미 클래스 갯수
        wrap_info_help = sum(
            1 for d in soup.find_all("div")
            if d.get("class") and any(c in ("wrap_info", "wrap_help") for c in d.get("class"))
        )
        f["macro_info"] = wrap_info_help
        f["macro_tip"] = sum(
            1 for d in soup.find_all("div")
            if d.get("class") and any(c == "wrap_tip" for c in d.get("class"))
        )
        f["macro_note"] = sum(
            1 for d in soup.find_all("div")
            if d.get("class") and any(c in ("wrap_important", "wrap_note") for c in d.get("class"))
        )
        f["macro_warning"] = sum(
            1 for d in soup.find_all("div")
            if d.get("class") and any(c in ("wrap_alert", "wrap_warning", "wrap_danger") for c in d.get("class"))
        )
        f["macro_panel"] = sum(
            1 for d in soup.find_all("div")
            if d.get("class") and any(c in ("wrap_box", "wrap_round") for c in d.get("class"))
        )
        # todo
        todos = soup.find_all("span", class_="todo")
        f["task"] = len(todos)
        f["task_list"] = 0  # dokuwiki 측에는 명시적 task-list 컨테이너 개념 없음
        f["task_text_marker"] = 0

    return f


def _split_sentences(text: str) -> list[str]:
    """문장 단위 분리. 한·영 mixed 를 위해 . ! ? … 줄바꿈 + 한국어 종결 어미 휴리스틱.

    완벽한 분리가 아니어도 양측에 *동일하게 적용* 하면 difflib 가 정렬을 잘 함.
    """
    # 줄바꿈을 보존하면서 구두점 뒤에 줄바꿈 삽입
    s = _re.sub(r"([.!?…])\s+", r"\1\n", text)
    s = _re.sub(r"([다요죠지요네까나]\.\s+|[다요죠].\s+)", r"\1\n", s)
    sents = [t.strip() for t in s.split("\n") if t.strip()]
    return [t for t in sents if len(t) >= 4]  # 4글자 미만은 노이즈


def _sentence_align(dokuwiki_text: str, confluence_text: str) -> dict:
    """양측 문장 시퀀스를 difflib 로 정렬. ratio + 누락/추가 카운트 + 첫 손실 문장 3개."""
    import difflib
    d = _split_sentences(dokuwiki_text)
    c = _split_sentences(confluence_text)
    if not d and not c:
        return {"sentence_ratio": 1.0, "d_sentences": 0, "c_sentences": 0,
                "missing": 0, "added": 0, "examples_missing": []}
    sm = difflib.SequenceMatcher(a=d, b=c, autojunk=False)
    ratio = sm.ratio()
    missing = added = 0
    examples_missing: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            missing += i2 - i1
            for s in d[i1:i2][:3 - len(examples_missing)]:
                if len(examples_missing) < 3:
                    examples_missing.append(s[:120])
        if tag in ("insert", "replace"):
            added += j2 - j1
    return {
        "sentence_ratio": round(ratio, 3),
        "d_sentences": len(d),
        "c_sentences": len(c),
        "missing": missing,
        "added": added,
        "examples_missing": examples_missing,
    }


_ARTIFACT_RES = [
    ("number_seq", re.compile(r"\b\d+(?:[\-/.:]\d+){1,}\b")),     # 전화/IP/날짜/버전
    ("url", re.compile(r"https?://[^\s<>\"']+")),
    ("email", re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")),
]


def _extract_artifacts(text: str) -> dict[str, set[str]]:
    """텍스트에서 누락 검출에 민감한 토큰 (전화/날짜/URL/이메일) 집합 추출."""
    out: dict[str, set[str]] = {}
    for kind, regex in _ARTIFACT_RES:
        out[kind] = set(regex.findall(text))
    return out


def _compare_artifacts(d_text: str, c_text: str) -> dict:
    """artifact set diff. 누락된 항목 카운트 + 샘플 3개."""
    d_a = _extract_artifacts(d_text)
    c_a = _extract_artifacts(c_text)
    out: dict = {}
    for kind in d_a:
        missing = sorted(d_a[kind] - c_a[kind])
        added = sorted(c_a[kind] - d_a[kind])
        out[kind] = {
            "d_count": len(d_a[kind]),
            "c_count": len(c_a[kind]),
            "missing": len(missing),
            "added": len(added),
            "examples_missing": missing[:3],
        }
    return out


def _extract_code_blocks(html_or_xml: str, is_storage: bool) -> list[str]:
    """양측에서 코드블록 텍스트 추출 → 비교용 정규화 (whitespace squash)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_or_xml, "html.parser")
    blocks: list[str] = []
    if is_storage:
        for m in soup.find_all("ac:structured-macro"):
            if m.get("ac:name") == "code":
                body = m.find("ac:plain-text-body")
                if body:
                    blocks.append(body.get_text())
    else:
        for pre in soup.find_all("pre"):
            classes = pre.get("class") or []
            if any(c in ("code", "file") for c in classes):
                blocks.append(pre.get_text())
    return [_re.sub(r"\s+", " ", b).strip() for b in blocks if b.strip()]


def _compare_code_blocks(d_html: str, c_storage: str) -> dict:
    d_blocks = _extract_code_blocks(d_html, is_storage=False)
    c_blocks = _extract_code_blocks(c_storage, is_storage=True)
    d_hashes = {hashlib.md5(b.encode("utf-8")).hexdigest()[:12] for b in d_blocks}
    c_hashes = {hashlib.md5(b.encode("utf-8")).hexdigest()[:12] for b in c_blocks}
    return {
        "d_code_blocks": len(d_blocks),
        "c_code_blocks": len(c_blocks),
        "matched": len(d_hashes & c_hashes),
        "missing": len(d_hashes - c_hashes),
        "added": len(c_hashes - d_hashes),
    }


def _link_resolution_rate(storage_xml: str) -> dict:
    """Confluence storage 의 page link 해소율 — placeholder vs ri:page 비율."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(storage_xml, "html.parser")
    resolved = sum(1 for a in soup.find_all("ac:link") if a.find("ri:page"))
    placeholder = sum(
        1 for a in soup.find_all("a")
        if str(a.get("href", "")).startswith("dwc-link:")
    )
    total = resolved + placeholder
    return {
        "resolved": resolved,
        "placeholder": placeholder,
        "rate": round(resolved / total, 3) if total else 1.0,
    }


def _extract_heading_seq(html_or_xml: str) -> list[tuple[int, str]]:
    """헤딩 (level, text) 시퀀스 추출 — 텍스트는 normalize."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_or_xml, "html.parser")
    out: list[tuple[int, str]] = []
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        try:
            lv = int(h.name[1:])
        except ValueError:
            continue
        txt = " ".join(h.get_text(" ", strip=True).split())[:80]
        if txt:
            out.append((lv, txt))
    return out


def _compare_heading_seq(d_html: str, c_body: str) -> dict:
    """LCS 기반 헤딩 시퀀스 비교. 누락/추가 카운트 + ratio."""
    import difflib
    d_seq = _extract_heading_seq(d_html)
    c_seq = _extract_heading_seq(c_body)
    if not d_seq and not c_seq:
        return {"d_headings": 0, "c_headings": 0, "lcs_ratio": 1.0,
                "missing": 0, "added": 0, "examples_missing": []}
    d_keys = [f"{lv}:{t}" for lv, t in d_seq]
    c_keys = [f"{lv}:{t}" for lv, t in c_seq]
    sm = difflib.SequenceMatcher(a=d_keys, b=c_keys, autojunk=False)
    missing = added = 0
    examples_missing: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            missing += i2 - i1
            for k in d_keys[i1:i2][:3 - len(examples_missing)]:
                if len(examples_missing) < 3:
                    examples_missing.append(k)
        if tag in ("insert", "replace"):
            added += j2 - j1
    return {
        "d_headings": len(d_seq),
        "c_headings": len(c_seq),
        "lcs_ratio": round(sm.ratio(), 3),
        "missing": missing,
        "added": added,
        "examples_missing": examples_missing,
    }


def _compare_features(d_feats: dict, c_feats: dict) -> tuple[list[dict], bool]:
    """카테고리별 카운트 차이 → mismatch list + has_critical_diff 반환.

    정밀화 (v2):
    - 변환기가 의도적으로 다른 카테고리로 옮기는 카운트는 *합산해서 비교*:
        * link_total = page_link + attachment_link + external_link
          (dokuwiki 의 fetch.php?media=https:// 같은 proxy 는 변환기가
          external 로 재분류 — 개별 카테고리 비교는 spurious mismatch)
        * todo: dokuwiki 측 task 와 Confluence 측 task + task_text_marker
          (mixed todo fallback 도 합산)
    - footnote 재구성으로 dokuwiki 측 sup 일부가 storage 의 macro_anchor
      로 옮겨가는 영향 보정: sup 차이만 따로 보지 않고 footnote 처리한
      페이지는 sup mismatch 를 ignore.
    - del 은 todo strikethrough 가 ac:task-status=complete 로 가서 사라짐
      → del 만 critical 아님. (이미 critical=False)
    - li 카운트 +차이 (Confluence 측 많음) 는 ac:task-list 의 task 가
      별도 li 자식으로 셈됨. critical=False.
    """
    rules = [
        ("h1", True), ("h2", True), ("h3", True), ("h4", False), ("h5", False),
        ("strong", False), ("em", False), ("code", False),
        ("sub", False), ("del", False), ("u", False),
        ("table", True), ("tr", False), ("th", False), ("td", False),
        ("ul", False), ("ol", False), ("li", False),
        ("blockquote", False),
        ("image_total", True),
        ("macro_info", True), ("macro_tip", True),
        ("macro_note", True), ("macro_warning", True),
        ("macro_panel", False),
        ("macro_code", True),
        ("smiley", True),
    ]
    mismatches = []
    has_crit = False
    for key, critical in rules:
        dv = d_feats.get(key, 0)
        cv = c_feats.get(key, 0)
        if dv == cv:
            continue
        diff = cv - dv
        mismatches.append({
            "category": key,
            "dokuwiki": dv,
            "confluence": cv,
            "diff": diff,
            "critical": critical,
        })
        if critical and abs(diff) > 0:
            has_crit = True

    # 합산 비교 — 변환기가 카테고리 옮긴 영향 흡수
    d_link_total = d_feats.get("page_link", 0) + d_feats.get("attachment_link", 0) + d_feats.get("external_link", 0)
    c_link_total = c_feats.get("page_link", 0) + c_feats.get("attachment_link", 0) + c_feats.get("external_link", 0)
    if d_link_total != c_link_total:
        # 큰 차이 (>20%) 면 진짜 손실
        ratio = abs(c_link_total - d_link_total) / max(d_link_total, 1)
        crit = ratio > 0.2 and d_link_total > 0
        mismatches.append({
            "category": "link_total",
            "dokuwiki": d_link_total,
            "confluence": c_link_total,
            "diff": c_link_total - d_link_total,
            "critical": crit,
        })
        if crit:
            has_crit = True

    # todo 합산
    d_task = d_feats.get("task", 0)
    c_task = c_feats.get("task", 0) + c_feats.get("task_text_marker", 0)
    if d_task != c_task:
        ratio = abs(c_task - d_task) / max(d_task, 1)
        crit = ratio > 0.2 and d_task > 0
        mismatches.append({
            "category": "task_total",
            "dokuwiki": d_task,
            "confluence": c_task,
            "diff": c_task - d_task,
            "critical": crit,
        })
        if crit:
            has_crit = True

    return mismatches, has_crit


def _diff_page(conn, session, base_url, doku_id, body_format: str = "storage") -> dict:
    """단일 페이지 비교. (status, dokuwiki_chars, confluence_chars,
    text_match, summary) 반환."""
    row = conn.execute(
        "SELECT raw_xhtml_path, storage_path, confluence_page_id, title "
        "FROM pages WHERE doku_id=?",
        (doku_id,),
    ).fetchone()
    if not row:
        return {"status": "NOT_IN_DB", "doku_id": doku_id}
    raw_path, storage_path, confluence_id, title = row
    if not confluence_id:
        return {"status": "NOT_UPLOADED", "doku_id": doku_id, "title": title}

    dokuwiki_text = ""
    if raw_path and Path(raw_path).is_file():
        dokuwiki_text = _extract_visible_text(Path(raw_path).read_text(encoding="utf-8"))

    confluence_body = _confluence_get_page_body(session, base_url, confluence_id, body_format)
    if confluence_body is None:
        return {"status": "GET_FAILED", "doku_id": doku_id, "title": title,
                "confluence_id": confluence_id}
    confluence_text = _extract_visible_text(confluence_body)

    # 텍스트 통계
    len_ratio = (
        min(len(dokuwiki_text), len(confluence_text))
        / max(len(dokuwiki_text), len(confluence_text), 1)
    )
    d_tokens = set(dokuwiki_text.split())
    c_tokens = set(confluence_text.split())
    overlap = len(d_tokens & c_tokens) / max(len(d_tokens | c_tokens), 1)

    # 구조적 카운트 비교 — dokuwiki raw vs Confluence storage (view 형식이면
    # 일부 ac:* 가 사라져 비교 부정확하므로 storage 우선)
    d_raw = ""
    if raw_path and Path(raw_path).is_file():
        d_raw = Path(raw_path).read_text(encoding="utf-8")
    d_feats = _structural_features(d_raw, is_storage=False) if d_raw else {}
    c_feats = _structural_features(confluence_body, is_storage=(body_format == "storage"))
    mismatches, has_crit = _compare_features(d_feats, c_feats)

    # 자동 신호 (스크립트만으로 NG 후보 탐지) — vision 호출 전 사전 검수
    sentence = _sentence_align(dokuwiki_text, confluence_text)
    artifacts = _compare_artifacts(dokuwiki_text, confluence_text)
    codes = _compare_code_blocks(d_raw, confluence_body)
    headings = _compare_heading_seq(d_raw, confluence_body)
    link_rate = _link_resolution_rate(confluence_body) if body_format == "storage" else {}

    judgement = "OK"
    notes = []
    if len_ratio < 0.5:
        judgement = "TEXT_DIVERGED"
        notes.append(f"len ratio {len_ratio:.2f}")
    if overlap < 0.6:
        judgement = "TEXT_DIVERGED"
        notes.append(f"token overlap {overlap:.2f}")
    if has_crit:
        judgement = "STRUCT_DIVERGED" if judgement == "OK" else "TEXT_AND_STRUCT_DIVERGED"
        crit_summary = ", ".join(
            f"{m['category']}({m['dokuwiki']}→{m['confluence']})"
            for m in mismatches if m["critical"]
        )
        notes.append(crit_summary)
    if dokuwiki_text and not confluence_text:
        judgement = "EMPTY_CONFLUENCE"
    if not dokuwiki_text:
        judgement = "EMPTY_DOKU"

    # 자동 신호 기반 추가 NG 격상
    if sentence["sentence_ratio"] < 0.7 and sentence["d_sentences"] >= 5:
        judgement = "SENTENCE_DIVERGED" if judgement == "OK" else judgement
        notes.append(f"sentence ratio {sentence['sentence_ratio']:.2f} (-{sentence['missing']})")
    art_missing_total = sum(a["missing"] for a in artifacts.values())
    if art_missing_total >= 3:
        notes.append(f"artifact missing {art_missing_total}")
        if judgement == "OK":
            judgement = "ARTIFACT_LOSS"
    if codes["missing"] >= 1 and codes["d_code_blocks"] > 0:
        notes.append(f"code blocks missing {codes['missing']}/{codes['d_code_blocks']}")
        if judgement == "OK":
            judgement = "CODE_DIVERGED"
    if headings["d_headings"] >= 3 and headings["lcs_ratio"] < 0.7:
        notes.append(f"heading lcs {headings['lcs_ratio']:.2f}")
        if judgement == "OK":
            judgement = "HEADING_DIVERGED"
    if link_rate and link_rate.get("placeholder", 0) >= 3:
        notes.append(f"unresolved page links {link_rate['placeholder']}")

    return {
        "status": judgement,
        "doku_id": doku_id,
        "title": title,
        "confluence_id": confluence_id,
        "dokuwiki_chars": len(dokuwiki_text),
        "confluence_chars": len(confluence_text),
        "len_ratio": round(len_ratio, 3),
        "token_overlap": round(overlap, 3),
        "mismatches": mismatches,
        "sentence": sentence,
        "artifacts": artifacts,
        "code_blocks": codes,
        "headings": headings,
        "link_resolution": link_rate,
        "notes": "; ".join(notes),
    }


def cmd_audit(args: argparse.Namespace) -> int:
    """업로드된 페이지를 Confluence 에서 다시 받아 dokuwiki raw 와 비교."""
    # 자격증명 검증은 _confluence_session 이 처리.

    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    if args.only:
        targets = [args.only]
    elif args.failed_only:
        targets = [
            d for (d,) in conn.execute(
                "SELECT doku_id FROM pages WHERE status='FAILED' ORDER BY doku_id"
            ).fetchall()
        ]
    else:
        # UPLOADED 페이지에서 무작위 또는 첫 N개
        if args.sample:
            targets = [
                d for (d,) in conn.execute(
                    "SELECT doku_id FROM pages WHERE status='UPLOADED' "
                    "AND confluence_page_id IS NOT NULL ORDER BY RANDOM() LIMIT ?",
                    (args.sample,),
                ).fetchall()
            ]
        else:
            targets = [
                d for (d,) in conn.execute(
                    "SELECT doku_id FROM pages WHERE status='UPLOADED' "
                    "AND confluence_page_id IS NOT NULL ORDER BY doku_id"
                ).fetchall()
            ]

    if not targets:
        log("audit 대상 페이지 없음.")
        return 1

    log(f"audit 대상: {len(targets)} 페이지")
    results: list[dict] = []
    judgement_counts: dict[str, int] = {}
    for i, doku_id in enumerate(targets, 1):
        r = _diff_page(conn, session, base, doku_id, body_format=args.body_format)
        results.append(r)
        judgement_counts[r["status"]] = judgement_counts.get(r["status"], 0) + 1
        if args.verbose or r["status"] not in ("OK",):
            notes = r.get("notes", "")
            log(
                f"  [{r['status']:18}] {doku_id} doku={r.get('dokuwiki_chars','?')} "
                f"conf={r.get('confluence_chars','?')} ratio={r.get('len_ratio','?')} "
                f"overlap={r.get('token_overlap','?')} {notes}"
            )

    log("=== audit 요약 ===")
    for k, v in sorted(judgement_counts.items(), key=lambda x: -x[1]):
        log(f"  {k:25} {v}")

    # 카테고리별 mismatch 빈도 집계
    cat_counts: dict[str, int] = {}
    cat_total_delta: dict[str, int] = {}
    for r in results:
        for m in r.get("mismatches", []) or []:
            cat = m["category"]
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            cat_total_delta[cat] = cat_total_delta.get(cat, 0) + m["diff"]
    if cat_counts:
        log("=== mismatch 카테고리 별 영향 페이지 수 + 누적 delta (c - d) ===")
        for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
            log(f"  {cat:25} pages={n:4}  total_delta={cat_total_delta[cat]:+d}")

    if args.output_json:
        Path(args.output_json).write_text(
            _json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"JSON 결과 저장 → {args.output_json}")

    if args.output_html:
        lines = [
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
            "<title>audit report</title>",
            "<style>body{font-family:-apple-system,sans-serif;font-size:13px;padding:1em;}",
            "table{border-collapse:collapse;width:100%}",
            "th,td{border:1px solid #ddd;padding:.3em .5em;text-align:left}",
            "tr.bad td{background:#fde8e6}",
            "tr.warn td{background:#fff5e6}",
            "tr.ok td{background:#ecf8ec}",
            "</style></head><body>",
            f"<h1>audit report ({len(results)} pages)</h1>",
            "<table><thead><tr>",
            "<th>status</th><th>doku_id</th><th>title</th><th>doku_chars</th>",
            "<th>conf_chars</th><th>ratio</th><th>overlap</th><th>notes</th>",
            "</tr></thead><tbody>",
        ]
        for r in results:
            cls = (
                "ok" if r["status"] == "OK"
                else ("bad" if r["status"] in ("DIVERGED", "EMPTY_CONFLUENCE",
                                                "GET_FAILED") else "warn")
            )
            lines.append(f"<tr class='{cls}'>")
            for k in ("status", "doku_id", "title", "dokuwiki_chars",
                      "confluence_chars", "len_ratio", "token_overlap", "notes"):
                v = r.get(k, "")
                lines.append(f"<td>{_h.escape(str(v))}</td>")
            lines.append("</tr>")
        lines.append("</tbody></table></body></html>")
        Path(args.output_html).write_text("\n".join(lines), encoding="utf-8")
        log(f"HTML 리포트 저장 → {args.output_html}")

    conn.close()
    # exit code: DIVERGED 또는 GET_FAILED 가 있으면 1
    bad = sum(judgement_counts.get(k, 0) for k in ("DIVERGED", "GET_FAILED", "EMPTY_CONFLUENCE"))
    return 0 if bad == 0 else 1


# § 보조: report (corpus 통계 + 분포)

def cmd_report(args: argparse.Namespace) -> int:
    """pages / attachments / 매크로 분포 / 큰 페이지 / 충돌 후보 등 corpus
    레벨 통계 출력. 라이브 업로드 직전 점검 + element-mapping §G 자동 산출."""
    if not Path(args.db).exists():
        log(f"DB 없음: {args.db}")
        return 1
    conn = db_connect(args.db)

    def header(title: str) -> None:
        print()
        print(f"==== {title} ====")

    header("pages 상태 분포")
    for status, n in conn.execute(
        "SELECT status, COUNT(*) FROM pages GROUP BY status ORDER BY status"
    ):
        print(f"  {status:12s}  {n}")

    header("attachments 상태 분포")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM attachments GROUP BY status ORDER BY status"
    ).fetchall()
    if rows:
        for status, n in rows:
            print(f"  {status:12s}  {n}")
    else:
        print("  (none)")

    header("links (S7 placeholder)")
    for resolved, n in conn.execute(
        "SELECT resolved, COUNT(*) FROM links GROUP BY resolved"
    ):
        label = "resolved" if resolved else "unresolved"
        print(f"  {label:12s}  {n}")

    header("storage XML 크기 분포")
    import glob as _glob

    sizes = sorted(
        Path(f).stat().st_size for f in _glob.glob("storage/**/*.xml", recursive=True)
    )
    if sizes:
        n = len(sizes)
        print(f"  files       {n}")
        print(f"  min/p50/p95/p99/max  {sizes[0]} / {sizes[n // 2]} / "
              f"{sizes[int(n * 0.95)]} / {sizes[int(n * 0.99)]} / {sizes[-1]}")
        big = [(s, f) for s, f in
               ((Path(f).stat().st_size, f) for f in _glob.glob("storage/**/*.xml", recursive=True))
               if s > 1024 * 1024]
        if big:
            print(f"  >1MB        {len(big)}  (Confluence 권장 한도 근접/초과)")
            for s, f in sorted(big, reverse=True)[: args.limit]:
                print(f"    {s:>9}  {f}")

    header("Confluence 매크로 카운트 (storage 안)")
    if sizes:
        macro_counts: dict[str, int] = {}
        for f in _glob.glob("storage/**/*.xml", recursive=True):
            txt = Path(f).read_text(encoding="utf-8", errors="replace")
            for m in _re.findall(r'ac:name="([^"]+)"', txt):
                macro_counts[m] = macro_counts.get(m, 0) + 1
        for name, n in sorted(macro_counts.items(), key=lambda x: -x[1]):
            print(f"  {name:18s}  {n}")
    else:
        print("  (storage/ 디렉터리 없음 — convert 실행 필요)")

    header("title 중복 (Confluence per-space-unique 위반 가능)")
    dup = conn.execute(
        "SELECT title, COUNT(*) FROM pages WHERE status='CONVERTED' AND title IS NOT NULL "
        "GROUP BY title HAVING COUNT(*) > 1 LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not dup:
        print("  (없음)")
    else:
        for t, n in dup:
            print(f"  {n}x  {t}")

    header("FAILED 첨부 샘플")
    for page, mid, err in conn.execute(
        "SELECT page_doku_id, media_id, last_error FROM attachments "
        "WHERE status='FAILED' LIMIT ?",
        (args.limit,),
    ).fetchall():
        print(f"  {page} / {mid}: {err}")

    header("meta")
    for k, v in conn.execute(
        "SELECT key, value FROM meta ORDER BY key"
    ).fetchall():
        if len(v) <= 80:
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: {v[:77]}...")

    conn.close()
    return 0


# § 보조: preview (raw + storage 나란히)

def cmd_preview(args: argparse.Namespace) -> int:
    """한 페이지의 dokuwiki raw HTML 과 Confluence storage XML 을 한
    HTML 페이지에 좌우로 배치해 시각 검수에 쓴다."""
    conn = db_connect(args.db)
    row = conn.execute(
        "SELECT doku_id, raw_xhtml_path, storage_path, title, content_hash, status "
        "FROM pages WHERE doku_id=?",
        (args.doku_id,),
    ).fetchone()
    if not row:
        log(f"doku_id={args.doku_id} 가 state.db 에 없습니다.")
        return 1
    doku_id, raw_path, storage_path, title, content_hash, status = row

    raw_html = ""
    if raw_path and Path(raw_path).is_file():
        raw_html = Path(raw_path).read_text(encoding="utf-8")

    storage_xml = ""
    if storage_path and Path(storage_path).is_file():
        storage_xml = Path(storage_path).read_text(encoding="utf-8")

    out = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>preview: {_h.escape(doku_id)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; padding: 1em; background: #f5f5f7; color: #1d1d1f; }}
  header {{ background: #fff; padding: .8em 1em; border-radius: 8px; margin-bottom: 1em; }}
  header h1 {{ margin: 0; font-size: 1.2em; }}
  .meta {{ color: #6e6e73; font-size: .85em; margin-top: .4em; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1em; }}
  .col {{ background: #fff; padding: 1em; border-radius: 8px; overflow: auto; max-height: 80vh; }}
  .col h2 {{ font-size: 1em; margin-top: 0; color: #6e6e73; }}
  .col.raw .body, .col.storage .body {{ font-size: .95em; line-height: 1.5; }}
  .col.storage pre {{ background: #f7f7f9; padding: .8em; border-radius: 6px; overflow-x: auto; font-size: .8em; }}
  /* storage XML 의 일부 매크로/tag 를 시각화하기 위한 간단 스타일 */
  ac\\:structured-macro {{ display: block; border-left: 4px solid #007aff; background: #f0f6ff; padding: .5em .8em; margin: .4em 0; }}
  ac\\:structured-macro[ac\\:name="info"] {{ border-color: #007aff; background: #e8f3ff; }}
  ac\\:structured-macro[ac\\:name="tip"]  {{ border-color: #34c759; background: #ecf8ec; }}
  ac\\:structured-macro[ac\\:name="note"] {{ border-color: #ff9500; background: #fff5e6; }}
  ac\\:structured-macro[ac\\:name="warning"] {{ border-color: #ff3b30; background: #fde8e6; }}
  ac\\:structured-macro[ac\\:name="panel"] {{ border-color: #8e8e93; background: #f5f5f7; }}
  ac\\:structured-macro[ac\\:name="code"] pre {{ background: #1d1d1f; color: #f5f5f7; }}
</style>
</head>
<body>
<header>
  <h1>{_h.escape(doku_id)} — {_h.escape(title or '')}</h1>
  <div class="meta">status={_h.escape(status)} / content_hash={_h.escape((content_hash or '')[:12])}…</div>
</header>
<div class="grid">
  <div class="col raw">
    <h2>DokuWiki raw (export_xhtmlbody)</h2>
    <div class="body">{raw_html}</div>
  </div>
  <div class="col storage">
    <h2>Confluence storage (approximate render)</h2>
    <div class="body">{storage_xml}</div>
    <details><summary>raw storage XML</summary><pre>{_h.escape(storage_xml)}</pre></details>
  </div>
</div>
</body>
</html>
"""
    out_path = Path(args.output) if args.output else Path(f"preview-{doku_id.replace(':', '_')}.html")
    out_path.write_text(out, encoding="utf-8")
    log(f"preview → {out_path}")
    conn.close()
    return 0


# § 보조: lint (storage XML 유효성 검사)

def cmd_lint(args: argparse.Namespace) -> int:
    """
    storage/*.xml 파일들이 valid XML 인지 일괄 검증. Confluence storage
    format 은 strict XML 이므로 invalid 가 있으면 업로드 단계의 400 으로
    이어진다. 사전에 잡아 변환기 결함을 잡는다.

    namespace prefix (ac:, ri:) 가 storage XML 본문에는 선언 없이 등장
    하므로 임시 wrapper 로 감싸 검증한다.
    """
    try:
        from lxml import etree
    except ImportError:
        log("lxml 이 필요합니다: pip install lxml")
        return 2

    target = Path(args.path) if args.path else STORAGE_DIR
    if not target.exists():
        log(f"경로 없음: {target}")
        return 2

    if target.is_dir():
        files = sorted(target.rglob("*.xml"))
    else:
        files = [target]
    if not files:
        log(f"storage XML 파일이 없습니다: {target}")
        return 1

    wrap_open = (b'<root xmlns:ac="http://atlassian.com/content" '
                 b'xmlns:ri="http://atlassian.com/resource/identifier">')
    wrap_close = b'</root>'

    failed: list[tuple[Path, str]] = []
    for f in files:
        try:
            body = f.read_bytes()
            etree.fromstring(wrap_open + body + wrap_close)
        except etree.XMLSyntaxError as e:
            failed.append((f, str(e).splitlines()[0]))
        except OSError as e:
            failed.append((f, f"OSError: {e}"))

    log(f"lint: {len(files)} 파일 검사, 실패 {len(failed)}")
    for path, err in failed[: args.limit]:
        print(f"  [INVALID] {path}: {err}")
    if len(failed) > args.limit:
        print(f"  ... (+{len(failed) - args.limit} more)")
    return 0 if not failed else 1


# § 보조: dev up/down (로컬 DokuWiki 테스트 컨테이너)

DEV_COMPOSE_REL = Path("dev/dokuwiki-local/docker-compose.yml")
DEV_CLONE_DST = Path("/tmp/dwc_test_dokuwiki/dwdata")
# DOKUWIKI_SRC 환경 변수 또는 --src 명시 필요 — 머신 별 경로 하드코딩 금지
DEV_DEFAULT_SRC: Path | None = None
DEV_BASE_URL = "http://127.0.0.1:18080"
DEV_HEALTH_PROBE = "/doku.php?id=wiki:syntax&do=export_xhtmlbody"
DEV_HEALTH_TIMEOUT = 30
DOKU_STABLE_TGZ = "https://download.dokuwiki.org/src/dokuwiki/dokuwiki-stable.tgz"

# 도쿠위키 데이터만 가진 경우 자동으로 받아 설치할 외부 플러그인 매핑.
# (정식 release tarball — DokuWiki 의 extension manager 가 가리키는 표준 URL).
# 미지원 플러그인은 사용자가 수동 설치 (admin → extensions). 본 맵은 자주 쓰이는 것만.
PLUGIN_DOWNLOADS: dict[str, str | None] = {
    # name → archive URL (tar.gz). None = DokuWiki core 번들 (별도 설치 불필요).
    "wrap":       "https://github.com/selfthinker/dokuwiki_plugin_wrap/archive/refs/heads/master.tar.gz",
    "struct":     "https://github.com/cosmocode/dokuwiki-plugin-struct/archive/refs/heads/main.tar.gz",
    "todo":       "https://github.com/dokufreaks/plugin-todo/archive/refs/heads/master.tar.gz",
    "discussion": "https://github.com/dokufreaks/plugin-discussion/archive/refs/heads/master.tar.gz",
    "blog":       "https://github.com/dokufreaks/plugin-blog/archive/refs/heads/master.tar.gz",
    "include":    "https://github.com/dokufreaks/plugin-include/archive/refs/heads/master.tar.gz",
    "pagelist":   "https://github.com/dokufreaks/plugin-pagelist/archive/refs/heads/master.tar.gz",
    "tag":        "https://github.com/dokufreaks/plugin-tag/archive/refs/heads/master.tar.gz",
    "tagging":    "https://github.com/cosmocode/tagging/archive/refs/heads/master.tar.gz",
    "sqlite":     "https://github.com/cosmocode/dokuwiki-plugin-sqlite/archive/refs/heads/master.tar.gz",
    "monthcal":   None,   # 변환기가 정적 표로 처리 (_convert_monthcal_fallback)
    "youtube":    None,   # 변환기가 Confluence iframe macro 로 처리
    "iframe":     "https://github.com/Chris--S/dokuwiki-plugin-iframe/archive/refs/heads/master.tar.gz",
    "encrypt":    "https://github.com/ssahara/dw-plugin-encryptedpasswords/archive/refs/heads/master.tar.gz",
    "encryptedpasswords": "https://github.com/ssahara/dw-plugin-encryptedpasswords/archive/refs/heads/master.tar.gz",
    "html":       None,   # 보안 위험 — 수동 설치만 권장
    "davcal":     None,   # 패키지 형식 다양 — 수동
    "box":        None,   # 다양한 fork — 수동
    "info":       None,   # core 번들 (위에서 처리)
    "logviewer":  None,   # bundled
    "info":       None,   # bundled
    "popularity": None,   # bundled
    "revert":     None,   # bundled
    "config":     None,   # bundled
    "extension":  None,   # bundled
    "acl":        None,   # bundled
    "usermanager": None,  # bundled
    "styling":    None,   # bundled
    "authplain":  None,   # bundled
    "authad":     None,   # bundled
    "authldap":   None,   # bundled
    "authpdo":    None,   # bundled
    "safefnrecode": None, # bundled
    "upgrade":    None,   # bundled
    "admin":      None,   # bundled
}

# 페이지 본문 ~~MACRO~~ → 플러그인 매핑 (자동 감지용)
# 값이 None 이면 DokuWiki core 매크로 (별도 플러그인 불필요).
MACRO_TO_PLUGIN: dict[str, str | None] = {
    # core macros
    "NOTOC": None, "NOCACHE": None,
    # plugin macros
    "DISCUSSION": "discussion",
    "INFO":       "info",       # info plugin 또는 core
    "BOX":        "box",
    "TODO":       "todo",
    "BLOG":       "blog",
    "FIRSTCHILD": "include",
    "MATHJAX":    "mathjax",
    "GRAPHVIZ":   "graphviz",
    "MERMAID":    "mermaid",
    "NEWPAGE":    "newpagetemplate",
    "REVEAL":     "reveal",
    "TAGS":       "tag",
    "INDEXMENU_N": "indexmenu",
    "STATS":      "stats",
}

# `{{plugin>...}}` 또는 `{{plugin?...}}` 형식의 플러그인 이름 매핑
DOUBLEBRACE_TO_PLUGIN: dict[str, str | None] = {
    "page": "include", "section": "include", "nopages": "include",
    "namespace": "include", "tagpage": "include",
    "tag": "tag", "topic": "tag", "count": "tag",
    "tagtopic": "tagging", "taglist": "tagging",
    "rss": None,
    "monthcal": "monthcal",
    "calendar": "davcal", "davcal": "davcal",
    "blog": "blog", "archive": "blog",
    "youtube": "youtube", "vimeo": "vimeo", "video": "video",
    "gallery": "gallery", "simplegallery": "simplegallery",
    "iframe": "iframe",
    "counter": "counter",
    "struct": "struct", "schema": "struct", "table": "struct",
    "form": "bureaucracy",
    "csv": "csv",
    "graphviz": "graphviz", "mermaid": "mermaid", "plantuml": "plantuml",
    "include": "include",
    "siteexport": "siteexport",
    "table": "structpublish",
}

# `<plugin>...</plugin>` 형식의 블록/인라인 태그 → 플러그인
# (HTML 표준 + DokuWiki core syntax 는 자동 제외 → STANDARD_BLOCK_TAGS)
BLOCKTAG_TO_PLUGIN: dict[str, str | None] = {
    "wrap": "wrap", "WRAP": "wrap",
    "box": "box",
    "color": "color",
    "note": "note",
    "todo": "todo",
    "html": "html", "HTML": "html",
    "php": "html", "PHP": "html",
    "csv": "csv",
    "form": "bureaucracy",
    "decrypt": "encrypt", "encrypt": "encrypt",
    "iframe": "iframe",
    "highlight": "highlight",
    "marquee": "marquee",
    "fold": "folded",
    "card": "cards",
    "tag": "tag",
}

# HTML 표준 / DokuWiki core / GPS 데이터 등 false-positive 제외 리스트
STANDARD_BLOCK_TAGS = {
    "p", "br", "hr", "b", "i", "u", "em", "strong", "code", "pre",
    "span", "div", "a", "img", "table", "tr", "td", "th",
    "ul", "ol", "li", "dl", "dt", "dd",
    "blockquote", "sup", "sub", "del", "ins",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "html", "head", "body",  # 일부 HTML 매크로 안에서 사용 — 별도 처리
    "abbr", "cite", "q", "small", "kbd", "samp", "var", "mark",
    "section", "article", "aside", "nav", "header", "footer",
    "figure", "figcaption", "details", "summary",
    "input", "button", "select", "option", "label", "form", "textarea",
    "audio", "video", "source", "track",
    "thead", "tbody", "tfoot",
    "fieldset", "legend",
    # DokuWiki core syntax
    "nowiki", "file", "code",  # 코어 ``` 또는 <code>
    "del",
    # GPS/GPX/TCX 데이터 (사용자 페이지에 잘라 붙은 경우)
    "ele", "time", "sym", "name", "desc", "type", "cmt",
    "lat", "lon", "trkpt", "trkseg", "trk", "rte", "rtept", "wpt",
    "extensions", "speed", "course", "fix", "sat", "hdop", "vdop",
    # Apache config 잔재
    "IfModule", "IfVersion", "VirtualHost", "Directory", "Location",
    "Limit", "LimitExcept", "Files", "FilesMatch",
}


def _scan_plugin_usage(src_path: Path, installed: set[str] | None = None) -> dict:
    """src_path/pages/**/*.txt 를 스캔해 DokuWiki 매크로/태그 사용 카운트 +
    설치된 플러그인 비교 → 미설치 플러그인 목록.

    반환: {
        'n_files': int,
        'installed': set[str],
        'macros': [{kind, name, plugin, count, installed, samples}],
        'missing': [{plugin, kind, name, count, samples, install_url}],
    }

    kind: 'tilde' (~~MACRO~~) / 'double_brace' ({{plugin>...}}) / 'block_tag' (<tag>)
    """
    from collections import Counter, defaultdict
    if installed is None:
        installed = set()
        for cand in (src_path / "lib" / "plugins",
                     src_path.parent / "lib" / "plugins",
                     _dev_data_root(src_path).parent / "lib" / "plugins" if _dev_data_root(src_path) else None):
            if cand and cand.is_dir():
                installed = {p.name for p in cand.iterdir()
                            if p.is_dir() and not p.name.startswith(".")}
                break

    pages_dir = _dev_data_root(src_path)
    if not pages_dir:
        pages_dir = src_path
    pages_dir = pages_dir / "pages" if (pages_dir / "pages").is_dir() else pages_dir

    TILDE = _re.compile(r"~~([A-Z][A-Z0-9_]+)(?::[^~]*)?~~")
    # `{{name>...}}` — `>` 가 핵심 separator (콜론은 namespace path 라 미디어 ID)
    DB = _re.compile(r"\{\{(?!\s)([a-zA-Z][a-zA-Z0-9_]*)>[^}]*\}\}")
    BLOCK = _re.compile(r"<(?!/)([a-zA-Z][a-zA-Z0-9_]*)(?:\s+[^>]*)?>", re.S)

    tilde_c: Counter[str] = Counter()
    db_c: Counter[str] = Counter()
    block_c: Counter[str] = Counter()
    sample: dict[str, list[str]] = defaultdict(list)
    n_files = 0

    for p in pages_dir.rglob("*.txt"):
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        n_files += 1
        try:
            rel = str(p.relative_to(pages_dir))
        except ValueError:
            rel = p.name
        for m in TILDE.findall(txt):
            tilde_c[m] += 1
            key = f"~~{m}~~"
            if len(sample[key]) < 3:
                sample[key].append(rel)
        for m in DB.findall(txt):
            db_c[m] += 1
            key = f"{{{{{m}>}}}}"
            if len(sample[key]) < 3:
                sample[key].append(rel)
        for m in BLOCK.findall(txt):
            if m.lower() in {t.lower() for t in STANDARD_BLOCK_TAGS}:
                continue
            block_c[m] += 1
            key = f"<{m}>"
            if len(sample[key]) < 3:
                sample[key].append(rel)

    rows: list[dict] = []
    missing: list[dict] = []

    def _row(kind: str, name: str, plugin: str | None, count: int) -> dict:
        is_core = plugin is None and kind == "tilde"  # core macro
        is_installed = plugin in installed if plugin else False
        key = f"~~{name}~~" if kind == "tilde" else (
            f"{{{{{name}>}}}}" if kind == "double_brace" else f"<{name}>"
        )
        return {
            "kind": kind, "name": name, "plugin": plugin,
            "count": count, "installed": is_installed, "core": is_core,
            "samples": sample.get(key, []),
        }

    for name, count in tilde_c.most_common():
        plugin = MACRO_TO_PLUGIN.get(name)
        rows.append(_row("tilde", name, plugin, count))
    for name, count in db_c.most_common():
        plugin = DOUBLEBRACE_TO_PLUGIN.get(name)
        # plugin 매핑 없음 → DokuWiki 미디어/namespace 일 가능성 (false positive 제외)
        if plugin is None and name not in DOUBLEBRACE_TO_PLUGIN:
            continue
        rows.append(_row("double_brace", name, plugin, count))
    for name, count in block_c.most_common():
        plugin = BLOCKTAG_TO_PLUGIN.get(name) or BLOCKTAG_TO_PLUGIN.get(name.lower())
        if plugin is None and name.lower() not in BLOCKTAG_TO_PLUGIN:
            continue
        rows.append(_row("block_tag", name, plugin, count))

    # missing — 매핑된 플러그인 중 미설치
    seen_missing: set[str] = set()
    for r in rows:
        plugin = r["plugin"]
        if not plugin or plugin in installed or plugin in seen_missing:
            continue
        seen_missing.add(plugin)
        missing.append({
            "plugin": plugin,
            "kind": r["kind"], "name": r["name"],
            "count": r["count"], "samples": r["samples"],
            "install_url": PLUGIN_DOWNLOADS.get(plugin),
        })
    # 같은 플러그인을 다른 매크로에서 참조한 카운트 합산
    plugin_counts: Counter[str] = Counter()
    for r in rows:
        if r["plugin"]:
            plugin_counts[r["plugin"]] += r["count"]
    for m in missing:
        m["count"] = plugin_counts[m["plugin"]]

    return {
        "n_files": n_files,
        "installed": installed,
        "macros": rows,
        "missing": missing,
    }


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _dev_clone_source(src: Path, dst: Path) -> int:
    """APFS clonefile (`cp -cR`) 를 우선 시도, 실패 시 평범한 `cp -R` 로 폴백."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    log(f"호스트 데이터 복제: {src} -> {dst}")
    rc = subprocess.call(["cp", "-cR", str(src), str(dst)])
    if rc == 0:
        return 0
    log("cp -cR 실패 (APFS 외 파일시스템 가능성). cp -R 로 재시도 — 디스크 사용량이 원본 크기만큼 증가합니다.")
    return subprocess.call(["cp", "-R", str(src), str(dst)])


def _dev_is_full_install(src: Path) -> bool:
    """src 가 DokuWiki 애플리케이션(=doku.php / lib/) 까지 갖춘 full install 인지."""
    return (src / "doku.php").is_file() and (src / "lib").is_dir() and (src / "inc").is_dir()


def _dev_data_root(src: Path) -> Path | None:
    """src 안에서 DokuWiki 의 *데이터* 루트 (pages/, media/ 의 부모) 를 찾는다.

    - case A: src 가 full install → return src/data
    - case B: src 가 data root → return src
    - case C: src/data 가 데이터 → return src/data
    """
    if (src / "pages").is_dir() and (src / "media").is_dir():
        return src
    cand = src / "data"
    if (cand / "pages").is_dir() and (cand / "media").is_dir():
        return cand
    return None


def _dev_bootstrap_download_core(tgz_path: Path) -> int:
    """DokuWiki stable tarball 다운로드 (없을 때만)."""
    if tgz_path.is_file() and tgz_path.stat().st_size > 1_000_000:
        log(f"  DokuWiki core 캐시 재사용: {tgz_path}")
        return 0
    tgz_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"  DokuWiki core 다운로드: {DOKU_STABLE_TGZ}")
    rc = subprocess.call(["curl", "-fsSL", "-o", str(tgz_path), DOKU_STABLE_TGZ])
    return rc


def _dev_bootstrap_extract(tgz_path: Path, dst: Path) -> int:
    dst.mkdir(parents=True, exist_ok=True)
    log(f"  core 압축 풀기 → {dst}")
    return subprocess.call(
        ["tar", "-xzf", str(tgz_path), "-C", str(dst), "--strip-components=1"]
    )


def _dev_overlay_user_data(data_root: Path, conf_root: Path | None, dst: Path) -> None:
    """data_root 와 conf_root 의 내용을 dst (DokuWiki core 가 풀린 곳) 에 overlay."""
    overlays = ("pages", "media", "media_attic", "attic", "meta", "media_meta", "index", "users")
    for sub in overlays:
        s = data_root / sub
        if not s.is_dir():
            continue
        d = dst / "data" / sub
        log(f"  데이터 overlay: {sub} ({sum(1 for _ in s.rglob('*'))} 항목)")
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d, copy_function=shutil.copy2)
    if conf_root and conf_root.is_dir():
        log(f"  conf overlay: {conf_root}")
        (dst / "conf").mkdir(exist_ok=True)
        for f in conf_root.iterdir():
            if f.is_file() and not f.name.endswith((".dist", ".bak")):
                try:
                    shutil.copy2(f, dst / "conf" / f.name)
                except OSError as e:
                    log(f"    overlay 스킵 {f.name}: {e}")


def _dev_detect_plugins(src: Path) -> dict[str, str]:
    """플러그인 감지 — 이름 → reason 매핑."""
    detected: dict[str, str] = {}
    data_root = _dev_data_root(src) or src
    conf_root = src / "conf" if (src / "conf").is_dir() else data_root.parent / "conf" if (data_root.parent / "conf").is_dir() else None
    # 1) conf/plugins.local.php 의 $plugins['name']=1
    if conf_root:
        pl = conf_root / "plugins.local.php"
        if pl.is_file():
            for name, val in _re.findall(
                r"\$plugins\['([^']+)'\]\s*=\s*(\d+)",
                pl.read_text(encoding="utf-8", errors="replace"),
            ):
                if val == "1":
                    detected[name] = "plugins.local.php"
    # 2) src/lib/plugins 또는 src/data/lib/plugins 에 *존재* 하는 디렉터리
    for cand in (src / "lib" / "plugins", data_root.parent / "lib" / "plugins"):
        if cand.is_dir():
            for sub in cand.iterdir():
                if sub.is_dir() and not sub.name.startswith("."):
                    detected.setdefault(sub.name, "lib/plugins/")
            break
    # 3) data/meta/struct.sqlite3 → struct
    if (data_root / "meta" / "struct.sqlite3").is_file():
        detected.setdefault("struct", "meta/struct.sqlite3")
    # 4) 페이지 본문 ~~MACRO~~ → 매크로별 플러그인
    pages_dir = data_root / "pages"
    if pages_dir.is_dir():
        macros_seen: set[str] = set()
        cnt = 0
        for txt in pages_dir.rglob("*.txt"):
            try:
                content = txt.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            macros_seen.update(_re.findall(r"~~([A-Z][A-Z0-9_:]+)~~", content))
            cnt += 1
            if cnt > 2000:
                break
        for m in macros_seen:
            if m in MACRO_TO_PLUGIN:
                detected.setdefault(MACRO_TO_PLUGIN[m], f"~~{m}~~ 매크로")
    return detected


def _dev_install_plugins(dokuwiki_root: Path, plugins: list[str]) -> dict:
    """누락된 플러그인을 PLUGIN_DOWNLOADS 매핑으로 받아 설치.
    이미 lib/plugins/<name> 이 있으면 skip. 매핑에 없으면 unknown."""
    import tempfile as _tf
    plugins_dir = dokuwiki_root / "lib" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {"installed": [], "already": [], "bundled": [], "unknown": [], "failed": []}
    for name in sorted(set(plugins)):
        plugin_dir = plugins_dir / name
        if plugin_dir.is_dir():
            result["already"].append(name)
            continue
        if name in PLUGIN_DOWNLOADS and PLUGIN_DOWNLOADS[name] is None:
            result["bundled"].append(name)
            continue
        url = PLUGIN_DOWNLOADS.get(name)
        if not url:
            result["unknown"].append(name)
            continue
        tmp = Path(_tf.mkdtemp(prefix="dwc-plugin-"))
        try:
            tgz = tmp / "p.tar.gz"
            rc = subprocess.call(["curl", "-fsSL", "-o", str(tgz), url])
            if rc != 0:
                result["failed"].append(f"{name} (curl rc={rc})")
                continue
            ex = tmp / "ex"
            ex.mkdir()
            rc = subprocess.call(["tar", "-xzf", str(tgz), "-C", str(ex)])
            if rc != 0:
                result["failed"].append(f"{name} (tar rc={rc})")
                continue
            subs = [p for p in ex.iterdir() if p.is_dir()]
            if not subs:
                result["failed"].append(f"{name} (압축 내부 디렉터리 없음)")
                continue
            cand = subs[0]
            # Sanity check — 정상 DokuWiki plugin 인지 확인.
            # plugin.info.txt 또는 syntax.php / action.php / admin.php 중 하나는
            # 반드시 있어야 함. 없으면 RPM spec 등 잘못된 패키지일 가능성.
            has_plugin_marker = any(
                (cand / fn).exists() for fn in
                ("plugin.info.txt", "syntax.php", "action.php", "admin.php",
                 "renderer.php", "helper.php")
            )
            if not has_plugin_marker:
                files_preview = ", ".join(sorted(p.name for p in cand.iterdir())[:5])
                result["failed"].append(
                    f"{name} (정상 plugin 아님 — plugin.info.txt/syntax.php 등 부재; 내용: {files_preview})"
                )
                continue
            shutil.move(str(cand), str(plugin_dir))
            result["installed"].append(name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return result


def _dev_patch_acl_off(clone_root: Path) -> None:
    """
    클론된 DokuWiki conf 의 useacl 을 0 으로 강제해 anonymous 가 모든 페이지를
    읽을 수 있게 한다. 이렇게 해야 ACL 로 잠긴 네임스페이스 (u:, ride:, blog:
    등) 가 정상 export 본문을 응답한다. 클론은 호스트 원본과 무관하므로
    로컬 dev 한정 변경이며 원본에는 손대지 않는다.

    추가로 `useacl` 항목이 conf/local.php 에 아예 없는 경우 (드물지만 발생)
    해당 라인을 append.
    """
    local_php = clone_root / "conf" / "local.php"
    if not local_php.is_file():
        log(f"  conf/local.php 없음 — ACL 패치 건너뜀: {local_php}")
        return
    text = local_php.read_text(encoding="utf-8", errors="replace")
    new_text = text
    if "$conf['useacl']" in new_text:
        new_text = _re.sub(
            r"\$conf\['useacl'\]\s*=\s*\d+\s*;",
            "$conf['useacl'] = 0;",
            new_text,
        )
    else:
        if not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "$conf['useacl'] = 0;\n"
    if new_text != text:
        local_php.write_text(new_text, encoding="utf-8")
        log("  ACL 비활성화 (useacl=0) 패치 적용 — 클론 한정")


# DokuWiki 공식 .htaccess (rewrite rules for /_media/, /_detail/, /_export/, 등)
# data-only bootstrap 시 자동 생성. 본 인스턴스의 dwk 스크린샷 캡쳐 시
# `/_media/...` 가 404 되어 *이미지가 모두 깨진* 케이스 회피.
_DOKUWIKI_HTACCESS = """RewriteEngine on
RewriteBase /
RewriteRule ^_media/(.*)              lib/exe/fetch.php?media=$1 [QSA,L]
RewriteRule ^_detail/(.*)             lib/exe/detail.php?media=$1 [QSA,L]
RewriteRule ^_export/([^/]+)/(.*)     doku.php?do=export_$1&id=$2 [QSA,L]
RewriteRule ^$                        doku.php  [L]
RewriteCond %{REQUEST_FILENAME}       !-f
RewriteCond %{REQUEST_FILENAME}       !-d
RewriteRule (.*)                      doku.php?id=$1  [QSA,L]
RewriteRule ^index.php$               doku.php
"""


def _dev_normalize_filenames_to_nfc(clone_root: Path) -> None:
    """macOS APFS 가 한국어 파일명을 NFD (자모 분리) 로 저장 — DokuWiki 가
    rendered HTML 에서 NFC (완성형) URL 생성 → byte mismatch → 404.

    해결: data/media 와 data/pages 하위의 *모든 비-ASCII 파일명* 을 NFC
    이름으로 *추가 cp* (rename 아님 — APFS 동등 비교라 mv 가 same file
    응답). cp 는 directory entry 에 NFC name 도 추가하므로 DokuWiki 의
    NFC URL → file_exists() 매치 가능.

    의도: dev 컨테이너 한정 적용. 원본 호스트 디렉터리는 손대지 않음
    (`_dev_clone_source` 가 clone 후 호출).
    """
    import unicodedata
    cp_count = 0
    err_count = 0
    for sub in ("data/media", "data/pages"):
        root = clone_root / sub
        if not root.is_dir():
            continue
        # bottom-up — 디렉터리 rename 도 안전
        for dirpath, dirs, files in os.walk(root, topdown=False):
            for name in files + dirs:
                if not any(ord(c) > 127 for c in name):
                    continue
                nfc = unicodedata.normalize("NFC", name)
                if nfc == name:
                    continue
                old = os.path.join(dirpath, name)
                new = os.path.join(dirpath, nfc)
                # macOS host 에선 cp 가 same file 응답하지만 컨테이너 안
                # Linux PHP 가 NFC byte 로 file_exists 시도 시 *NFC name
                # 도 directory entry 에 등록* 됨 (실험적 확인). file 만
                # 처리 (디렉터리는 같은 path 그대로).
                if os.path.isfile(old) and not os.path.lexists(new):
                    try:
                        shutil.copy2(old, new)
                        cp_count += 1
                    except OSError:
                        err_count += 1
    if cp_count:
        log(f"  한국어 파일명 NFC 정규화: {cp_count} cp (err={err_count})")


def _dev_ensure_htaccess(clone_root: Path) -> None:
    """`.htaccess` 가 부재하면 DokuWiki 공식 rewrite rules 생성.

    DokuWiki 의 `userewrite=1` 설정 시 미디어 URL 이 `/_media/...` 형식 —
    Apache 의 mod_rewrite 가 `lib/exe/fetch.php?media=...` 로 변환해야
    동작. .htaccess 부재 시 *모든 미디어가 404* → 비교 갤러리의 dwk
    스크린샷에 이미지가 깨진 채로 캡쳐됨.

    .htaccess.dist 가 있으면 그것을 우선 사용 (DokuWiki 버전별 권장 내용).
    없으면 위 _DOKUWIKI_HTACCESS 사용.
    """
    htaccess = clone_root / ".htaccess"
    if htaccess.is_file():
        return
    dist = clone_root / ".htaccess.dist"
    if dist.is_file():
        try:
            htaccess.write_text(dist.read_text(encoding="utf-8"), encoding="utf-8")
            log("  .htaccess 생성 (.htaccess.dist 복원)")
            return
        except OSError:
            pass
    try:
        htaccess.write_text(_DOKUWIKI_HTACCESS, encoding="utf-8")
        log("  .htaccess 생성 (DokuWiki 공식 rewrite rules)")
    except OSError as e:  # noqa: BLE001
        log(f"  [WARN] .htaccess 생성 실패: {e}")


def _dev_wait_healthy(timeout: int = DEV_HEALTH_TIMEOUT) -> bool:
    import urllib.error
    import urllib.request

    url = DEV_BASE_URL + DEV_HEALTH_PROBE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def _evp_bytes_to_key(password: bytes, salt: bytes,
                       key_len: int = 32, iv_len: int = 16) -> tuple[bytes, bytes]:
    """OpenSSL EVP_BytesToKey with MD5, 1 iteration — encryptedpasswords plugin
    (gibberish-aes.js) 의 KDF 와 호환."""
    dtot = b""
    d = b""
    while len(dtot) < key_len + iv_len:
        d = hashlib.md5(d + password + salt).digest()
        dtot += d
    return dtot[:key_len], dtot[key_len:key_len + iv_len]


# § decrypt / link-check (encryptedpasswords + Confluence 측 링크 정합성)
def decrypt_encryptedpasswords(cipher_b64: str, password: str) -> str:
    """encryptedpasswords plugin 의 cipher (base64-encoded OpenSSL AES-256-CBC)
    를 password 로 복호화.

    Format: base64("Salted__" + 8-byte salt + AES-256-CBC ciphertext)
    KDF: EVP_BytesToKey(MD5, 1 iter, key=32, iv=16)
    Padding: PKCS7
    """
    try:
        from Crypto.Cipher import AES  # type: ignore
    except ImportError:
        raise RuntimeError(
            "pycryptodome 미설치 — `pip install pycryptodome` 후 재시도"
        )
    raw = base64.b64decode(cipher_b64.strip())
    if not raw.startswith(b"Salted__"):
        raise ValueError("Invalid format — 'Salted__' prefix 부재 (OpenSSL AES 형식 아님)")
    salt = raw[8:16]
    ct = raw[16:]
    if len(ct) % 16 != 0:
        raise ValueError(f"Cipher length not multiple of 16 ({len(ct)})")
    key, iv = _evp_bytes_to_key(password.encode("utf-8"), salt)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pt = cipher.decrypt(ct)
    # PKCS7 unpad
    pad = pt[-1]
    if pad < 1 or pad > 16:
        raise ValueError("복호화 실패 — password 가 맞지 않습니다 (PKCS7 padding 오류)")
    return pt[:-pad].decode("utf-8", errors="replace")


def cmd_link_check(args: argparse.Namespace) -> int:
    """Confluence 측 마이그래이션 결과 페이지의 링크 정합성 검증.

    세 가지 검사:
    1. dwc-link: placeholder 잔존 (rewrite-links 가 처리 안 한 흔적)
    2. <ac:link><ri:page ri:content-title="X"/> 의 title 이 *실 존재* 페이지를
       가리키는지 (state.db.pages 의 title 과 매치)
    3. <a href="..."> 외부 URL HTTP HEAD (옵션, --check-external)

    결과 JSON / 콘솔 표.
    """
    if not args.email or not args.api_token:
        log("자격증명 필요")
        return 2
    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    # 모든 알려진 title → page_id 매핑 (resolve check 용)
    title_to_page: dict[str, str] = {}
    for title, pid in conn.execute(
        "SELECT title, confluence_page_id FROM pages "
        "WHERE confluence_page_id IS NOT NULL AND title IS NOT NULL AND title != ''"
    ):
        title_to_page.setdefault(title, pid)

    # 검사 대상
    sql = "SELECT doku_id, confluence_page_id FROM pages WHERE confluence_page_id IS NOT NULL"
    params: tuple = ()
    if args.only:
        sql += " AND doku_id=?"
        params = (args.only,)
    sql += " ORDER BY doku_id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql, params).fetchall()
    log(f"링크 점검 대상: {len(rows)} 페이지")

    summary = {
        "total_pages": len(rows),
        "checked": 0,
        "placeholder_residual": 0,
        "unresolved_page_links": 0,
        "broken_external": 0,
        "fetch_failed": 0,
        "details": [],
    }

    external_seen: set[str] = set()
    external_cache: dict[str, int] = {}

    for i, (doku_id, page_id) in enumerate(rows, 1):
        r = _request_with_retry(
            session, "GET", f"{base}/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        if r is None or r.status_code >= 400:
            summary["fetch_failed"] += 1
            continue
        body = (r.json().get("body") or {}).get("storage", {}).get("value", "") or ""
        summary["checked"] += 1

        page_issues: dict = {"doku_id": doku_id, "page_id": page_id,
                              "placeholders": [], "unresolved_pages": [],
                              "broken_external": []}

        # 1. placeholder 잔존
        for m in _re.finditer(r'dwc-link:([^"\s<]+)', body):
            page_issues["placeholders"].append(m.group(1))
        summary["placeholder_residual"] += len(page_issues["placeholders"])

        # 2. ri:page title 의 실재 여부
        for m in _re.finditer(r'<ri:page\s+ri:content-title="([^"]+)"', body):
            title = m.group(1)
            if title not in title_to_page:
                page_issues["unresolved_pages"].append(title)
        summary["unresolved_page_links"] += len(page_issues["unresolved_pages"])

        # 3. 외부 URL (옵션)
        if args.check_external:
            for m in _re.finditer(r'href="(https?://[^"]+)"', body):
                url = m.group(1)
                if url in external_seen:
                    if external_cache.get(url, 200) >= 400:
                        page_issues["broken_external"].append(url)
                    continue
                external_seen.add(url)
                # HEAD 요청 (간소화 — requests 직접 호출)
                try:
                    import requests
                    rr = requests.head(url, timeout=5, allow_redirects=True)
                    external_cache[url] = rr.status_code
                    if rr.status_code >= 400:
                        page_issues["broken_external"].append(url)
                except Exception:
                    external_cache[url] = 0
                    page_issues["broken_external"].append(url)
        summary["broken_external"] += len(page_issues["broken_external"])

        if any([page_issues["placeholders"], page_issues["unresolved_pages"],
                page_issues["broken_external"]]):
            summary["details"].append(page_issues)

        if i % 50 == 0:
            log(f"  ... checked {i}/{len(rows)}")

    log(f"링크 점검 완료: checked={summary['checked']}/{summary['total_pages']}")
    log(f"  placeholder 잔존:   {summary['placeholder_residual']}")
    log(f"  unresolved page:    {summary['unresolved_page_links']}")
    log(f"  broken external:    {summary['broken_external']}")
    log(f"  fetch 실패:         {summary['fetch_failed']}")
    log(f"  문제 있는 페이지:   {len(summary['details'])}")

    if args.output:
        Path(args.output).write_text(
            _json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"JSON → {args.output}")

    if args.verbose and summary["details"]:
        print()
        print("=== 문제 있는 페이지 ===")
        for d in summary["details"][:50]:
            print(f"\n[{d['doku_id']}] page {d['page_id']}")
            if d["placeholders"]:
                print(f"  placeholders ({len(d['placeholders'])}): {d['placeholders'][:5]}")
            if d["unresolved_pages"]:
                print(f"  unresolved ({len(d['unresolved_pages'])}): {d['unresolved_pages'][:5]}")
            if d["broken_external"]:
                print(f"  broken-ext ({len(d['broken_external'])}): {d['broken_external'][:5]}")

    conn.close()
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    """encryptedpasswords cipher 를 복호화.

    사용법:
      python run.py decrypt --password PASS CIPHER_B64
      python run.py decrypt --password PASS --page DOKU_ID    # 페이지의 모든 cipher
      python run.py decrypt --password PASS --confluence-id PAGE_ID  # Confluence 페이지의 모든 cipher
    """
    import getpass
    password = args.password
    if password is None:
        password = getpass.getpass("Password: ")
    if not password:
        log("password 필요")
        return 2

    targets: list[tuple[str, str]] = []  # (label, cipher_b64)

    if args.cipher:
        for c in args.cipher:
            targets.append(("(cli)", c))
    if args.page:
        # state.db 의 pages.raw_xhtml_path 또는 storage_path 에서 추출
        conn = db_connect(args.db)
        row = conn.execute(
            "SELECT raw_xhtml_path, storage_path FROM pages WHERE doku_id=?",
            (args.page,),
        ).fetchone()
        if not row:
            log(f"페이지 없음: {args.page}")
            return 2
        text = ""
        if row[0] and Path(row[0]).is_file():
            text = Path(row[0]).read_text(encoding="utf-8", errors="ignore")
        elif row[1] and Path(row[1]).is_file():
            text = Path(row[1]).read_text(encoding="utf-8", errors="ignore")
        # `<decrypt>cipher</decrypt>` 또는 escape `&lt;decrypt&gt;...&lt;/decrypt&gt;` 또는
        # `<encrypt>cipher</encrypt>` 모두 처리
        for m in _re.finditer(
            r"(?:<|&lt;)(?:decrypt|encrypt)(?:>|&gt;)([A-Za-z0-9+/=]+)(?:<|&lt;)/(?:decrypt|encrypt)(?:>|&gt;)",
            text,
        ):
            targets.append((args.page, m.group(1)))
    if args.confluence_id:
        if not args.email or not args.api_token:
            log("Confluence 페이지에서 복호화하려면 자격증명 필요")
            return 2
        session = _confluence_session(args)
        if session is None:
            return 2
        base = args.base_url.rstrip("/")
        r = _request_with_retry(
            session, "GET", f"{base}/api/v2/pages/{args.confluence_id}",
            params={"body-format": "storage"},
        )
        if r is None or r.status_code >= 400:
            log("GET 실패")
            return 2
        body = (r.json().get("body") or {}).get("storage", {}).get("value", "") or ""
        for m in _re.finditer(
            r"(?:<|&lt;)(?:decrypt|encrypt)(?:>|&gt;)([A-Za-z0-9+/=]+)(?:<|&lt;)/(?:decrypt|encrypt)(?:>|&gt;)",
            body,
        ):
            targets.append((f"page:{args.confluence_id}", m.group(1)))

    if not targets:
        log("복호화 대상 없음 — --cipher / --page / --confluence-id 중 하나 지정")
        return 2

    ok = fail = 0
    for label, cipher in targets:
        try:
            plain = decrypt_encryptedpasswords(cipher, password)
            print(f"[{label}] {cipher[:30]}...  →  {plain}")
            ok += 1
        except Exception as e:
            print(f"[{label}] {cipher[:30]}...  →  실패: {e}")
            fail += 1
    log(f"decrypt 완료: ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


def cmd_plugin_scan(args: argparse.Namespace) -> int:
    """DokuWiki 페이지 본문을 스캔해 사용된 매크로/태그를 카운트하고
    설치된 플러그인 (lib/plugins/) 과 비교 → 미설치 플러그인 목록.

    DokuWiki 가 동작 중일 필요 없음 — 데이터 디렉터리만 있으면 가능.
    """
    src = Path(args.src).expanduser().resolve() if args.src else None
    if src is None:
        conn = db_connect(args.db)
        src_str = db_get_meta(conn, "dokuwiki_src")
        if src_str:
            src = Path(src_str)
    if src is None or not src.is_dir():
        log("DokuWiki 데이터 경로 미지정. --src /path 또는 discover 먼저 실행.")
        return 2

    log(f"스캔 대상: {src}")
    result = _scan_plugin_usage(src)
    n = result["n_files"]
    installed = result["installed"]
    log(f"페이지 스캔: {n} 파일 / 설치된 플러그인: {len(installed)}개")

    if args.json:
        # 직렬화: set → list
        serializable = dict(result)
        serializable["installed"] = sorted(installed)
        Path(args.json).write_text(_json.dumps(serializable, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
        log(f"JSON → {args.json}")

    # 콘솔 표
    print()
    print(f"{'kind':14} {'name':16} {'plugin':14} {'count':>6} 상태  샘플 페이지")
    print("-" * 100)
    for r in result["macros"]:
        if args.only_missing and (r["installed"] or r["core"]):
            continue
        status = "core" if r["core"] else ("✓" if r["installed"] else "✗ 미설치")
        plugin = r["plugin"] or "-"
        samples = ", ".join(r["samples"][:2])
        print(f"{r['kind']:14} {r['name']:16} {plugin:14} {r['count']:>6}  {status:9} {samples[:60]}")
    print()
    if result["missing"]:
        print(f"=== 미설치 플러그인 {len(result['missing'])}개 ===")
        for m in result["missing"]:
            url = m["install_url"] or "(PLUGIN_DOWNLOADS 매핑 없음 — 수동 설치)"
            print(f"  {m['plugin']:18} ({m['count']:>5} 참조)  {url}")
    else:
        print("✓ 미설치 플러그인 없음 — 모든 참조된 플러그인이 설치됨")

    # 옵션: 자동 설치 (PLUGIN_DOWNLOADS 매핑이 있는 미설치 플러그인만)
    if args.install and result["missing"]:
        if not DEV_CLONE_DST.exists():
            log(f"클론이 없습니다 ({DEV_CLONE_DST}) — `dev up` 먼저 또는 --install-into 경로 지정")
            if not args.install_into:
                return 1
            dest = Path(args.install_into).expanduser().resolve()
        else:
            dest = DEV_CLONE_DST
        targets = [m["plugin"] for m in result["missing"] if m["install_url"]]
        if not targets:
            log("자동 설치 가능한 플러그인 없음 (매핑 부재)")
            return 0
        log(f"자동 설치 대상 {len(targets)}개 → {dest}/lib/plugins/")
        res = _dev_install_plugins(dest, targets)
        for kind, names in res.items():
            if names:
                log(f"  {kind} ({len(names)}): {', '.join(names)}")
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    """로컬 DokuWiki 테스트 컨테이너 (dev/dokuwiki-local) 관리.

    sub-action 별로 분기 (`args.action` = up/down/install-plugins):
    - up: full install vs data-only 자동 감지. data-only 면 DokuWiki
      stable tarball 다운로드 + 데이터 overlay + ACL bypass + 플러그인 자동
      감지·설치 (PLUGIN_DOWNLOADS 매핑 기반).
    - down: 컨테이너 종료. `--purge` 면 클론도 삭제.
    - install-plugins: 기존 클론에 누락 플러그인 추가 설치.

    state.db 변경 없음 (로컬 dev 환경만 손댐). 원본 DokuWiki 디렉터리
    (`$DOKUWIKI_SRC`) 는 *수정하지 않음* — `/tmp/dwc_test_dokuwiki/dwdata`
    같은 별도 클론에만 적용 (APFS clonefile + ACL bypass 패치 안전).
    """
    compose = _project_root() / DEV_COMPOSE_REL
    if not compose.is_file():
        log(f"compose 파일이 없습니다: {compose}")
        return 2

    if args.action == "up":
        if not args.src:
            log("DokuWiki 데이터 경로 미지정.")
            log("  --src /path/to/dokuwiki/data 또는 DOKUWIKI_SRC env 설정.")
            log("  data-only (pages/ + media/ 만) / full install (doku.php + lib/) 모두 가능.")
            return 2
        src = Path(args.src).expanduser().resolve()
        if not src.is_dir():
            log(f"DokuWiki 데이터 디렉터리 없음: {src}")
            return 2

        full_install = _dev_is_full_install(src)
        if getattr(args, "bootstrap", False):
            full_install = False  # 강제 bootstrap

        if not DEV_CLONE_DST.exists():
            if full_install:
                log("감지: full DokuWiki install (doku.php + lib/ + inc/). APFS clonefile 로 복제.")
                if _dev_clone_source(src, DEV_CLONE_DST) != 0:
                    log("데이터 복제 실패")
                    return 1
            else:
                log("감지: data-only — DokuWiki core 자동 다운로드 + 데이터 overlay + 플러그인 자동 설치.")
                tgz = Path("/tmp/dwc_doku_stable.tgz")
                if _dev_bootstrap_download_core(tgz) != 0:
                    log("DokuWiki core 다운로드 실패")
                    return 1
                if _dev_bootstrap_extract(tgz, DEV_CLONE_DST) != 0:
                    log("DokuWiki core 압축 풀기 실패")
                    return 1
                data_root = _dev_data_root(src)
                if not data_root:
                    log(f"DokuWiki 데이터 구조 인식 실패 (pages/ media/ 부재): {src}")
                    return 1
                conf_root = src / "conf" if (src / "conf").is_dir() else (
                    data_root.parent / "conf" if (data_root.parent / "conf").is_dir() else None
                )
                _dev_overlay_user_data(data_root, conf_root, DEV_CLONE_DST)
                # 플러그인 자동 감지 + 설치
                detected = _dev_detect_plugins(src)
                if detected:
                    log(f"감지된 플러그인 {len(detected)}: " + ", ".join(sorted(detected)))
                    install_res = _dev_install_plugins(DEV_CLONE_DST, list(detected))
                    for kind, names in install_res.items():
                        if names:
                            log(f"  {kind} ({len(names)}): {', '.join(names)}")
                    if install_res["unknown"]:
                        log("  → unknown 은 컨테이너 기동 후 admin → 확장기능 에서 수동 설치 가능")
                else:
                    log("  플러그인 자동 감지 0건 (core 만 사용)")
        else:
            log(f"기존 복제본 재사용: {DEV_CLONE_DST} (재 bootstrap 하려면 dev down --purge 후 dev up)")
            if getattr(args, "install_plugins", False):
                # 사용자가 추가 감지/설치 강제
                detected = _dev_detect_plugins(src)
                if detected:
                    log(f"플러그인 감지 {len(detected)}: " + ", ".join(sorted(detected)))
                    install_res = _dev_install_plugins(DEV_CLONE_DST, list(detected))
                    for kind, names in install_res.items():
                        if names:
                            log(f"  {kind} ({len(names)}): {', '.join(names)}")
        _dev_patch_acl_off(DEV_CLONE_DST)
        _dev_ensure_htaccess(DEV_CLONE_DST)
        _dev_normalize_filenames_to_nfc(DEV_CLONE_DST)

        log("docker compose up -d")
        if subprocess.call(["docker", "compose", "-f", str(compose), "up", "-d"]) != 0:
            return 1

        log(f"헬스 체크 — 최대 {DEV_HEALTH_TIMEOUT}s 대기")
        if not _dev_wait_healthy():
            log("타임아웃: 컨테이너가 응답하지 않음. `docker logs dokuwiki-mig` 로 진단.")
            return 1
        log(f"준비 완료: {DEV_BASE_URL}")
        log("  예) python run.py render --base-url " + DEV_BASE_URL + " --only wiki:syntax")
        return 0

    if args.action == "install-plugins":
        if not DEV_CLONE_DST.exists():
            log("dev up 먼저 실행해 클론이 만들어진 상태여야 합니다.")
            return 2
        src = Path(args.src).expanduser().resolve() if args.src else DEV_DEFAULT_SRC
        detected = _dev_detect_plugins(src if src.is_dir() else DEV_CLONE_DST)
        if not detected:
            log("플러그인 0건 감지.")
            return 0
        log(f"감지된 플러그인 {len(detected)}: " + ", ".join(sorted(detected)))
        install_res = _dev_install_plugins(DEV_CLONE_DST, list(detected))
        for kind, names in install_res.items():
            if names:
                log(f"  {kind} ({len(names)}): {', '.join(names)}")
        return 0

    if args.action == "down":
        log("docker compose down")
        rc = subprocess.call(["docker", "compose", "-f", str(compose), "down"])
        if args.purge:
            if DEV_CLONE_DST.exists():
                log(f"복제본 삭제: {DEV_CLONE_DST}")
                shutil.rmtree(DEV_CLONE_DST, ignore_errors=True)
            parent = DEV_CLONE_DST.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        return rc

    log(f"알 수 없는 action: {args.action}")
    return 2


# § 보조: status

# § verify (시각 검수 큐, docs/visual-audit.md)

VERIFY_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS verify_decisions (
    doku_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    notes TEXT,
    reviewer TEXT,
    reviewed_at TEXT,
    source_hash TEXT,
    visual_score REAL,
    flags TEXT
);
CREATE INDEX IF NOT EXISTS verify_decisions_decision_idx
    ON verify_decisions(decision);
"""


def _ensure_verify_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(VERIFY_DECISIONS_DDL)
    # ng_tag 컬럼 (Phase 2 추가) — 기존 DB 에는 없으니 ALTER 시도 후 무시
    try:
        conn.execute("ALTER TABLE verify_decisions ADD COLUMN ng_tag TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _verify_macro_counts(storage_xml: str) -> dict[str, int]:
    """매크로 종류별 개수. 정밀 파싱 대신 substring count.
    storage XML 1KB 미만 평균 페이지에서 충분히 정확."""
    if not storage_xml:
        return {}
    out: dict[str, int] = {}
    for name in (
        "info", "tip", "note", "warning", "panel", "code",
        "anchor", "details", "detailssummary", "expand",
    ):
        out[name] = storage_xml.count(f'ac:name="{name}"')
    out["image"] = storage_xml.count("<ac:image")
    out["task_list"] = storage_xml.count("<ac:task-list")
    out["page_link"] = storage_xml.count("<ri:page")
    out["attachment_link"] = storage_xml.count("<ri:attachment")
    return out


def _verify_score_page(
    row: tuple,
    storage_xml: str,
    is_oversized_body: bool,
    has_oversized_attachment: bool,
    history_ratio: float | None,
    is_struct_snapshot: bool,
    random_seed: int,
) -> tuple[float, list[str]]:
    """페이지 한 개의 우선순위 점수 + 플래그 라벨 리스트."""
    macros = _verify_macro_counts(storage_xml)
    score = 0.0
    flags: list[str] = []

    macro_callouts = sum(macros.get(n, 0) for n in
                         ("info", "tip", "note", "warning", "panel"))
    if macro_callouts >= 5:
        score += 5
        flags.append(f"macro:{macro_callouts}")
    elif macro_callouts >= 1:
        score += 2
        flags.append(f"macro:{macro_callouts}")

    if macros.get("image", 0) >= 3:
        score += 3
        flags.append(f"image:{macros['image']}")
    elif macros.get("image", 0) >= 1:
        score += 1
        flags.append(f"image:{macros['image']}")

    if is_oversized_body:
        score += 5
        flags.append("oversized-body")

    if has_oversized_attachment:
        score += 5
        flags.append("oversized-attach")

    if history_ratio is not None and 0.0 < history_ratio < 0.5:
        score += 3
        flags.append(f"history:{int(history_ratio*100)}%")

    if is_struct_snapshot:
        score += 5
        flags.append("struct-snapshot")

    body_len = len(storage_xml or "")
    if body_len >= 5000:
        score += 1
        flags.append(f"body:{body_len//1024}KB")

    # 안정적 무작위 순서 — 동점일 때 결정적 셔플
    score += (random_seed % 1000) / 100000.0

    return score, flags


def _verify_build_queue(
    conn: sqlite3.Connection,
    sample: int,
    strategy: str,
    resume: bool,
) -> list[dict]:
    """우선순위 큐 생성. 각 항목은 dict(doku_id, title, score, flags, ...)."""
    import random as _random

    rng = _random.Random(0xD0CC)

    rows = conn.execute(
        "SELECT doku_id, title, storage_path, raw_xhtml_path, "
        "       content_hash, confluence_page_id, namespace "
        "  FROM pages "
        " WHERE status='UPLOADED' AND confluence_page_id IS NOT NULL "
        " ORDER BY doku_id"
    ).fetchall()

    # large_body_fallback / struct snapshot 정보 사전 수집
    oversized_body_ids = {
        k.split(":", 1)[1] for (k,) in conn.execute(
            "SELECT key FROM meta WHERE key LIKE 'large_body_fallback:%'"
        ).fetchall()
    }
    oversized_attach_pages = {
        d for (d,) in conn.execute(
            "SELECT DISTINCT page_doku_id FROM attachments "
            " WHERE status='OVERSIZED'"
        ).fetchall()
    }
    struct_snapshot_ids: set[str] = set()
    try:
        struct_snapshot_ids = {
            d for (d,) in conn.execute(
                "SELECT bound_doku_id FROM struct_rows "
                " WHERE bound_doku_id IS NOT NULL"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        pass

    # history 보존율 (rev 단위)
    history_ratio: dict[str, float] = {}
    try:
        for d, total in conn.execute(
            "SELECT doku_id, total_revs FROM history_meta"
        ).fetchall():
            if not total:
                continue
            uploaded = conn.execute(
                "SELECT COUNT(*) FROM revisions "
                " WHERE doku_id=? AND status='UPLOADED'",
                (d,),
            ).fetchone()[0]
            history_ratio[d] = uploaded / total
    except sqlite3.OperationalError:
        pass

    # 이미 검수된 페이지 (resume 모드)
    resolved_ok: set[str] = set()
    if resume:
        for d, src_hash in conn.execute(
            "SELECT doku_id, source_hash FROM verify_decisions "
            " WHERE decision='OK'"
        ).fetchall():
            for r in rows:
                if r[0] == d and (r[4] or "") == (src_hash or ""):
                    resolved_ok.add(d)
                    break

    queue: list[dict] = []
    for doku_id, title, storage_path, raw_path, content_hash, page_id, ns in rows:
        if resume and doku_id in resolved_ok:
            continue
        storage_xml = ""
        if storage_path and Path(storage_path).is_file():
            try:
                storage_xml = Path(storage_path).read_text(encoding="utf-8")
            except OSError:
                storage_xml = ""
        score, flags = _verify_score_page(
            (doku_id, title, content_hash, page_id, ns),
            storage_xml,
            is_oversized_body=(doku_id in oversized_body_ids),
            has_oversized_attachment=(doku_id in oversized_attach_pages),
            history_ratio=history_ratio.get(doku_id),
            is_struct_snapshot=(doku_id in struct_snapshot_ids),
            random_seed=rng.randint(0, 99999),
        )
        queue.append({
            "doku_id": doku_id,
            "title": title or doku_id,
            "namespace": ns or "",
            "score": round(score, 3),
            "flags": flags,
            "content_hash": content_hash or "",
            "confluence_page_id": page_id or "",
            "storage_path": storage_path or "",
            "raw_xhtml_path": raw_path or "",
        })

    if strategy == "critical-only":
        queue = [q for q in queue if q["score"] >= 5]

    queue.sort(key=lambda q: q["score"], reverse=True)

    if strategy != "all":
        queue = queue[: max(sample, 0)]

    return queue


def _verify_fetch_confluence_view(
    session, base: str, page_id: str, body_format: str = "view",
) -> str | None:
    """Confluence v2 GET /pages/{id}?body-format=... 의 body.<format>.value.
    `body_format` 은 view (Confluence UI 렌더) 또는 export_view (정식 export)
    또는 storage 또는 atlas_doc_format."""
    try:
        url = f"{base.rstrip('/')}/api/v2/pages/{page_id}?body-format={body_format}"
        resp = _request_with_retry(session, "GET", url, timeout=30)
        if resp is None or resp.status_code != 200:
            return None
        data = resp.json()
        return ((data.get("body") or {}).get(body_format) or {}).get("value")
    except Exception:
        return None


def _verify_compute_metrics(
    doku_id: str,
    conn: sqlite3.Connection,
    raw_html: str,
    storage_xml: str,
    confluence_body: str | None,
) -> dict:
    """페이지 한 장에 대한 구조적 지표 — dokuwiki raw / 우리 storage /
    (있다면) Confluence body 양측 카운트 비교. _structural_features 재사용.

    반환 형식: {
        'rows': [(label, d, s, c, ok)],  # ok 는 d==s==(c or s) 일 때 True
        'attachment': None | (ok, total),
    }
    """
    cmp: list[tuple[str, int, int, int, bool]] = []
    try:
        d_feats = _structural_features(raw_html or "", is_storage=False)
    except Exception:
        d_feats = {}
    try:
        s_feats = _structural_features(storage_xml or "", is_storage=True)
    except Exception:
        s_feats = {}
    c_feats: dict[str, int] = {}
    if confluence_body:
        try:
            # body-format=view 응답은 storage 가 아니라 view-HTML 이지만
            # `_structural_features(is_storage=False)` 가 받아 카운트 가능 —
            # 단 ac:* 매크로는 이미 풀려있어 macro_* 는 항상 0.
            c_feats = _structural_features(confluence_body, is_storage=False)
        except Exception:
            c_feats = {}

    def row(label: str, key: str, key_c: str | None = None) -> None:
        d = int(d_feats.get(key, 0))
        s = int(s_feats.get(key, 0))
        ck = key_c or key
        c = int(c_feats.get(ck, 0)) if c_feats else -1
        # storage 와 dokuwiki 가 같으면 일단 OK. Confluence view 는
        # 매크로가 풀려 있으므로 image/table/list 정도만 보조 비교.
        if c < 0:
            ok = d == s
        else:
            ok = (d == s)
        cmp.append((label, d, s, c, ok))

    row("이미지", "image_total")
    row("표", "table")
    row("표 행", "tr")
    row("표 셀", "td")
    row("리스트(ul)", "ul")
    row("리스트(ol)", "ol")
    row("li", "li")
    row("h2", "h2")
    row("h3", "h3")
    row("코드", "macro_code")
    row("info", "macro_info")
    row("tip", "macro_tip")
    row("note", "macro_note")
    row("warning", "macro_warning")
    row("smiley→emoji", "smiley")
    row("외부링크", "external_link")

    # 자동 신호 — verify 카드에서 vision 없이 NG 분류
    auto: dict = {}
    try:
        d_text = _extract_visible_text(raw_html or "")
        c_text = _extract_visible_text(confluence_body or "")
        auto["sentence"] = _sentence_align(d_text, c_text)
        auto["artifacts"] = _compare_artifacts(d_text, c_text)
    except Exception:
        auto["sentence"] = {}
        auto["artifacts"] = {}
    try:
        auto["code_blocks"] = _compare_code_blocks(raw_html or "", storage_xml or "")
    except Exception:
        auto["code_blocks"] = {}
    try:
        auto["headings"] = _compare_heading_seq(raw_html or "", confluence_body or storage_xml or "")
    except Exception:
        auto["headings"] = {}
    try:
        auto["link_resolution"] = _link_resolution_rate(storage_xml or "")
    except Exception:
        auto["link_resolution"] = {}

    # auto-NG 추정 태그 (사용자가 라디오로 확인 가능)
    auto_ng = None
    sent = auto.get("sentence") or {}
    if sent.get("sentence_ratio", 1.0) < 0.7 and sent.get("d_sentences", 0) >= 5:
        auto_ng = "텍스트"
    arts = auto.get("artifacts") or {}
    if any(a.get("missing", 0) >= 2 for a in arts.values()):
        auto_ng = auto_ng or "텍스트"
    cb = auto.get("code_blocks") or {}
    if cb.get("missing", 0) >= 1:
        auto_ng = "매크로"  # 코드 매크로 손실
    hd = auto.get("headings") or {}
    if hd.get("d_headings", 0) >= 3 and hd.get("lcs_ratio", 1.0) < 0.7:
        auto_ng = auto_ng or "텍스트"
    lr = auto.get("link_resolution") or {}
    if lr.get("placeholder", 0) >= 3:
        auto_ng = auto_ng or "링크"
    auto["auto_ng"] = auto_ng

    return {"rows": cmp, "auto": auto}


def _verify_check_attachments(
    conn: sqlite3.Connection,
    session,
    base: str,
    doku_id: str,
) -> tuple[int, int]:
    """페이지의 모든 첨부 (UPLOADED) 에 대해 v2 attachments 메타 GET.

    HEAD 는 Confluence 가 일관 응답하지 않아 download URL 대신 attachment
    객체 자체를 GET — 가볍고 인증 단순.
    """
    rows = conn.execute(
        "SELECT confluence_attachment_id FROM attachments "
        " WHERE page_doku_id=? AND status='UPLOADED' "
        "   AND confluence_attachment_id IS NOT NULL",
        (doku_id,),
    ).fetchall()
    if not rows:
        return (0, 0)
    ok = 0
    for (aid,) in rows:
        try:
            url = f"{base.rstrip('/')}/api/v2/attachments/{aid}"
            resp = _request_with_retry(session, "GET", url, timeout=20)
            if resp is not None and resp.status_code == 200:
                ok += 1
        except Exception:
            pass
    return (ok, len(rows))


# DokuWiki / Confluence Cloud 의 본문 영역 selector 후보 (순차 시도).
_DWK_MAIN_SELECTORS = ("#dokuwiki__content", ".dokuwiki .page", "#content", "main")
_CNF_MAIN_SELECTORS = (
    '[data-test-id="content-body"]',
    "#main-content",
    "#content",
    '[role="main"]',
    ".wiki-content",
)


def _verify_capture_screenshots(
    queue: list[dict],
    out_dir: Path,
    dokuwiki_base: str,
    confluence_base: str,
    confluence_email: str,
    confluence_token: str,
    *,
    capture_main_only: bool = False,
    extract_bbox: bool = False,
    confluence_view_html: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Playwright + ImageHash 가 설치돼 있을 때만 동작. 양측 페이지를
    헤드리스 Chromium 으로 풀 렌더 → PNG → phash. 결과는 doku_id → dict.

    의존성이 없으면 빈 dict 반환 + 안내 로그. 외부 네트워크/큰 의존성
    이므로 디폴트 off.

    capture_main_only=True: chrome 제외한 본문 영역만 별도 PNG (`.dwk.main.png`
    / `.cnf.main.png`) 추가 캡쳐.
    extract_bbox=True: 블록 (h1-h6/p/table/img/pre/ul/ol) 의 bbox + text 를
    `bboxes_dwk` / `bboxes_cnf` 키로 결과에 추가.
    confluence_view_html: doku_id → Confluence body-format=view 의 본문 HTML.
        제공되면 `page.goto()` 대신 `page.set_content()` 로 렌더 — Confluence
        Cloud 의 페이지 UI 인증 (Basic Auth 만으로 view 접근 불가) 우회.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log("Playwright 미설치 — `pip install playwright && "
            "playwright install chromium` 후 재실행. 스크린샷 건너뜀.")
        return {}
    try:
        import imagehash  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        log("ImageHash/Pillow 미설치 — phash 계산 불가, 스크린샷만 캡쳐.")
        imagehash = None  # type: ignore
        Image = None  # type: ignore

    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}

    confluence_auth_header = ""
    if confluence_email and confluence_token:
        token = base64.b64encode(
            f"{confluence_email}:{confluence_token}".encode()
        ).decode()
        confluence_auth_header = f"Basic {token}"

    bbox_js = """
        () => Array.from(document.querySelectorAll(
            'h1,h2,h3,h4,h5,h6,p,table,img,pre,ul,ol'
        )).filter(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }).map(e => {
            const r = e.getBoundingClientRect();
            return {
                tag: e.tagName.toLowerCase(),
                x: r.left + window.scrollX,
                y: r.top + window.scrollY,
                w: r.width,
                h: r.height,
                text: (e.innerText || '').slice(0, 60).trim()
            };
        })
    """

    def _try_capture_main(p, selectors, dst_path) -> str | None:
        """selector 후보 중 첫 매칭 element 만 screenshot. 실패 시 None."""
        for sel in selectors:
            try:
                loc = p.locator(sel).first
                if loc.count() > 0:
                    loc.screenshot(path=str(dst_path))
                    return sel
            except Exception:
                continue
        return None

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            extra_http_headers=(
                {"Authorization": confluence_auth_header}
                if confluence_auth_header else {}
            ),
        )
        page = ctx.new_page()
        for i, q in enumerate(queue, 1):
            doku_id = q["doku_id"]
            page_id = q["confluence_page_id"]
            stem = doku_id.replace(':', '_')
            d_path = out_dir / f"{stem}.dwk.png"
            c_path = out_dir / f"{stem}.cnf.png"
            d_main_path = out_dir / f"{stem}.dwk.main.png"
            c_main_path = out_dir / f"{stem}.cnf.main.png"
            d_bboxes: list[dict] = []
            c_bboxes: list[dict] = []
            try:
                if dokuwiki_base:
                    page.goto(
                        f"{dokuwiki_base.rstrip('/')}/doku.php?id={doku_id}",
                        timeout=15000, wait_until="networkidle",
                    )
                    page.screenshot(path=str(d_path), full_page=True)
                    if capture_main_only:
                        _try_capture_main(page, _DWK_MAIN_SELECTORS, d_main_path)
                    if extract_bbox:
                        try:
                            d_bboxes = page.evaluate(bbox_js) or []
                        except Exception:
                            d_bboxes = []
            except Exception as e:
                log(f"  [dwk fail] {doku_id}: {e}")
            try:
                view_html = (confluence_view_html or {}).get(doku_id)
                if view_html:
                    # 옵션 3: body-format=view 의 HTML 을 set_content 로 직접 주입.
                    # Confluence Cloud UI 인증 우회 — 동일 viewport 에서 본문만 렌더.
                    # base href 로 상대 URL (이미지 등) 해소.
                    wrapped = (
                        '<!DOCTYPE html><html><head><meta charset="utf-8">'
                        f'<base href="{(confluence_base or "").rstrip("/")}/">'
                        '<style>'
                        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
                        'max-width:760px;margin:1em auto;padding:0 1em;line-height:1.5;color:#1d1d1f;}'
                        'table{border-collapse:collapse;margin:0.5em 0;}'
                        'th,td{border:1px solid #ddd;padding:0.3em 0.6em;}'
                        'img{max-width:100%;}'
                        'pre{background:#f5f5f5;padding:0.5em;overflow:auto;border-radius:4px;}'
                        '</style></head>'
                        f'<body>{view_html}</body></html>'
                    )
                    page.set_content(wrapped, wait_until="domcontentloaded")
                    page.screenshot(path=str(c_path), full_page=True)
                    if capture_main_only:
                        # view HTML 은 chrome 없음 — body 자체가 main
                        try:
                            page.locator("body").first.screenshot(path=str(c_main_path))
                        except Exception:
                            pass
                    if extract_bbox:
                        try:
                            c_bboxes = page.evaluate(bbox_js) or []
                        except Exception:
                            c_bboxes = []
                elif confluence_base and page_id:
                    # 폴백: 인증 가능한 페이지면 직접 goto (보통 Confluence Cloud
                    # 는 로그인 페이지로 redirect → confluence_view_html 권장).
                    page.goto(
                        f"{confluence_base.rstrip('/')}/pages/{page_id}",
                        timeout=20000, wait_until="networkidle",
                    )
                    page.screenshot(path=str(c_path), full_page=True)
                    if capture_main_only:
                        _try_capture_main(page, _CNF_MAIN_SELECTORS, c_main_path)
                    if extract_bbox:
                        try:
                            c_bboxes = page.evaluate(bbox_js) or []
                        except Exception:
                            c_bboxes = []
            except Exception as e:
                log(f"  [cnf fail] {doku_id}: {e}")

            ph_d = ph_c = None
            similarity = None
            if imagehash and Image and d_path.is_file() and c_path.is_file():
                try:
                    ph_d = imagehash.phash(Image.open(d_path))
                    ph_c = imagehash.phash(Image.open(c_path))
                    hamming = ph_d - ph_c
                    # phash 는 64bit → similarity = 1 - hamming/64
                    similarity = round(1.0 - hamming / 64.0, 3)
                except Exception:
                    pass

            results[doku_id] = {
                "dokuwiki_png": str(d_path) if d_path.is_file() else None,
                "confluence_png": str(c_path) if c_path.is_file() else None,
                "dokuwiki_main_png": str(d_main_path) if d_main_path.is_file() else None,
                "confluence_main_png": str(c_main_path) if c_main_path.is_file() else None,
                "phash_dokuwiki": str(ph_d) if ph_d else None,
                "phash_confluence": str(ph_c) if ph_c else None,
                "similarity": similarity,
                "bboxes_dwk": d_bboxes,
                "bboxes_cnf": c_bboxes,
            }
            if i % 10 == 0:
                log(f"  screenshot {i}/{len(queue)}")
        browser.close()

    return results


# ─── visual-comparison Phase 4 후보 (docs/visual-comparison-proposal.md) ───

def _vc_pil_open(path) -> "Image.Image | None":
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _vc_resize_match(img_a, img_b) -> tuple["Image.Image", "Image.Image"]:
    """양측을 같은 너비로 normalize (작은 쪽 너비 기준). 비율 유지."""
    from PIL import Image  # type: ignore
    w = min(img_a.width, img_b.width)
    if img_a.width != w:
        img_a = img_a.resize((w, int(img_a.height * w / img_a.width)), Image.LANCZOS)
    if img_b.width != w:
        img_b = img_b.resize((w, int(img_b.height * w / img_b.width)), Image.LANCZOS)
    # 짧은 쪽 높이로 자름 (양측 모두 같은 크기)
    h = min(img_a.height, img_b.height)
    img_a = img_a.crop((0, 0, w, h))
    img_b = img_b.crop((0, 0, w, h))
    return img_a, img_b


def _vc_pixel_diff(
    img_a_path: str, img_b_path: str, *, threshold: int = 32,
    out_overlay: str | None = None,
) -> dict:
    """제안 1: 양측 PNG 의 픽셀 단위 diff (Pillow ImageChops.difference).
    threshold 미만 차이는 잡음으로 처리.
    반환: {"diff_ratio": float, "width": int, "height": int, "overlay": str|None}
    """
    try:
        from PIL import Image, ImageChops  # type: ignore
    except ImportError:
        return {"error": "Pillow 미설치"}
    a = _vc_pil_open(img_a_path)
    b = _vc_pil_open(img_b_path)
    if a is None or b is None:
        return {"error": "이미지 로드 실패"}
    a, b = _vc_resize_match(a, b)
    diff = ImageChops.difference(a, b)
    # 픽셀별 max(R,G,B) > threshold 인 픽셀 수 — RGB bytes 로 빠르게
    raw = diff.tobytes()  # length = w*h*3
    n_changed = 0
    for i in range(0, len(raw), 3):
        if raw[i] > threshold or raw[i+1] > threshold or raw[i+2] > threshold:
            n_changed += 1
    total = a.width * a.height
    ratio = n_changed / total if total else 0.0

    overlay_path = None
    if out_overlay:
        # 빨간색 overlay (변경 픽셀만 표시)
        mask = diff.convert("L").point(lambda p: 255 if p > threshold else 0)
        red = Image.new("RGB", a.size, (255, 0, 0))
        overlay = Image.composite(red, a, mask)
        overlay.save(out_overlay, "PNG")
        overlay_path = out_overlay

    return {
        "diff_ratio": round(ratio, 4),
        "width": a.width,
        "height": a.height,
        "overlay": overlay_path,
    }


def _vc_tile_phash(
    img_a_path: str, img_b_path: str, *, rows: int = 8, cols: int = 4,
    bad_threshold: int = 16, out_overlay: str | None = None,
) -> dict:
    """제안 2: N×M 격자 분할 후 타일별 phash 거리.
    반환: {"max_distance", "mean_distance", "n_bad_tiles", "matrix", "overlay"}
    """
    try:
        import imagehash  # type: ignore
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        return {"error": "imagehash/Pillow 미설치"}
    a = _vc_pil_open(img_a_path)
    b = _vc_pil_open(img_b_path)
    if a is None or b is None:
        return {"error": "이미지 로드 실패"}
    a, b = _vc_resize_match(a, b)
    tw = a.width // cols
    th = a.height // rows
    if tw == 0 or th == 0:
        return {"error": "이미지가 격자보다 작음"}
    matrix: list[list[int]] = []
    bad_tiles: list[tuple[int, int]] = []
    max_d = 0
    total_d = 0
    n_tiles = 0
    for r in range(rows):
        row_d: list[int] = []
        for c in range(cols):
            box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
            ta = a.crop(box)
            tb = b.crop(box)
            d = imagehash.phash(ta) - imagehash.phash(tb)
            row_d.append(d)
            max_d = max(max_d, d)
            total_d += d
            n_tiles += 1
            if d >= bad_threshold:
                bad_tiles.append((r, c))
        matrix.append(row_d)
    mean_d = total_d / max(n_tiles, 1)

    overlay_path = None
    if out_overlay and bad_tiles:
        overlay = a.copy()
        draw = ImageDraw.Draw(overlay, "RGBA")
        for (r, c) in bad_tiles:
            box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
            draw.rectangle(box, outline=(255, 0, 0, 255), width=3, fill=(255, 0, 0, 60))
        overlay.save(out_overlay, "PNG")
        overlay_path = out_overlay

    return {
        "max_distance": max_d,
        "mean_distance": round(mean_d, 2),
        "n_bad_tiles": len(bad_tiles),
        "n_tiles": n_tiles,
        "matrix": matrix,
        "overlay": overlay_path,
    }


def _vc_element_compare(bboxes_a: list[dict], bboxes_b: list[dict]) -> dict:
    """제안 3: bboxes_a / bboxes_b 의 (tag, text) 시퀀스 LCS 짝짓기.

    실제 요소 캡쳐는 capture 단계의 부담이 커서 (요소 수 × 페이지 수) — 본
    함수는 bbox 메타만 비교. 텍스트가 같은 짝은 'matched' 로 카운트.
    """
    import difflib
    a_keys = [f"{b['tag']}:{b.get('text','').strip()[:40]}" for b in bboxes_a]
    b_keys = [f"{b['tag']}:{b.get('text','').strip()[:40]}" for b in bboxes_b]
    if not a_keys and not b_keys:
        return {"d_n": 0, "c_n": 0, "matched": 0, "missing": 0, "added": 0, "ratio": 1.0}
    sm = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
    matched = 0
    missing = added = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += i2 - i1
        elif tag in ("delete", "replace"):
            missing += i2 - i1
        if tag in ("insert", "replace"):
            added += j2 - j1
    return {
        "d_n": len(a_keys),
        "c_n": len(b_keys),
        "matched": matched,
        "missing": missing,
        "added": added,
        "ratio": round(sm.ratio(), 3),
    }


def _vc_ocr_text(img_path: str) -> str:
    """제안 4: pytesseract 로 텍스트 추출. 실패 시 빈 문자열."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return ""
    try:
        return pytesseract.image_to_string(Image.open(img_path), lang="kor+eng")
    except Exception:
        return ""


def _vc_ocr_compare(d_img_path: str, c_img_path: str) -> dict:
    """양측 이미지에서 OCR 추출한 텍스트를 sentence_align 으로 비교."""
    d_text = _vc_ocr_text(d_img_path)
    c_text = _vc_ocr_text(c_img_path)
    if not d_text and not c_text:
        return {"error": "OCR 실패 또는 pytesseract 미설치"}
    align = _sentence_align(d_text, c_text)
    align["d_chars"] = len(d_text)
    align["c_chars"] = len(c_text)
    return align


def _vc_bbox_lcs_compare(bboxes_a: list[dict], bboxes_b: list[dict]) -> dict:
    """제안 5: 양측 블록 시퀀스 LCS + 짝지어진 블록의 상대 너비/높이 비율 비교.

    페이지 너비로 normalize 한 후 (양측 페이지 너비가 다를 수 있으니),
    너비 비율의 평균 절대 차이를 계산.
    """
    import difflib
    if not bboxes_a or not bboxes_b:
        return {"d_n": len(bboxes_a), "c_n": len(bboxes_b), "lcs_ratio": 0.0,
                "matched": 0, "mean_width_diff": 0.0}
    # 페이지 너비 = bbox max(x+w)
    w_a = max((b["x"] + b["w"]) for b in bboxes_a) or 1
    w_b = max((b["x"] + b["w"]) for b in bboxes_b) or 1
    a_keys = [f"{b['tag']}:{b.get('text','').strip()[:40]}" for b in bboxes_a]
    b_keys = [f"{b['tag']}:{b.get('text','').strip()[:40]}" for b in bboxes_b]
    sm = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
    width_diffs: list[float] = []
    matched = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            ba = bboxes_a[i1 + k]
            bb = bboxes_b[j1 + k]
            rel_a = ba["w"] / w_a
            rel_b = bb["w"] / w_b
            width_diffs.append(abs(rel_a - rel_b))
            matched += 1
    mean_width_diff = (sum(width_diffs) / len(width_diffs)) if width_diffs else 0.0
    return {
        "d_n": len(bboxes_a),
        "c_n": len(bboxes_b),
        "lcs_ratio": round(sm.ratio(), 3),
        "matched": matched,
        "mean_width_diff": round(mean_width_diff, 3),
    }


def _vc_canonical_tree(html_or_xml: str, *, is_storage: bool) -> list:
    """제안 6: bs4 → canonical 노드 리스트 [(depth, tag, text_snippet)].

    구조 비교용 — attribute 제거, dokuwiki chrome / Confluence chrome 제거,
    매크로는 ac:* / wrap 클래스를 일반화 (note/info/tip/warning 등 매핑).
    """
    from bs4 import BeautifulSoup, Comment
    soup = BeautifulSoup(html_or_xml, "html.parser")
    # 노이즈 제거
    for tag in ("script", "style", "link", "meta", "noscript", "iframe",
                "form", "input", "head", "button", "select"):
        for t in soup.find_all(tag):
            t.decompose()
    for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
        c.extract()
    if not is_storage:
        for sid in ("dokuwiki__site", "dokuwiki__top", "dokuwiki__header",
                    "dokuwiki__footer", "dokuwiki__pagetools",
                    "dokuwiki__usertools", "dokuwiki__sitetools"):
            for t in soup.find_all(id=sid):
                t.decompose()
        for a in soup.find_all("a", class_="secedit"):
            a.decompose()
        for div in soup.find_all("div", class_="toc"):
            div.decompose()

    # tag 정규화 — dokuwiki wrap_info → info, ac:structured-macro[name=info] → info
    def norm_tag(el) -> str:
        name = (el.name or "").lower()
        if name in ("ac:structured-macro",):
            macro = el.get("ac:name") or el.get("ac:name", "")
            if macro:
                return f"macro:{macro}"
            return "macro:?"
        if name == "div":
            classes = el.get("class") or []
            if any(c in ("wrap_info", "wrap_help") for c in classes):
                return "macro:info"
            if any(c == "wrap_tip" for c in classes):
                return "macro:tip"
            if any(c in ("wrap_important", "wrap_note") for c in classes):
                return "macro:note"
            if any(c in ("wrap_alert", "wrap_warning", "wrap_danger") for c in classes):
                return "macro:warning"
            if any(c in ("wrap_box", "wrap_round") for c in classes):
                return "macro:panel"
            return "div"
        # ri:page / ri:attachment / ri:database 무시 — 부모 ac:link 가 대표
        return name

    keep = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "table", "tr", "td", "th",
            "ul", "ol", "li", "img", "pre", "code", "blockquote", "a",
            "div", "macro:info", "macro:tip", "macro:note", "macro:warning",
            "macro:panel", "macro:code", "macro:details", "macro:detailssummary",
            "ac:image", "ac:link", "ac:task-list", "ac:task"}

    out: list[tuple[int, str, str]] = []

    def walk(el, depth: int) -> None:
        t = norm_tag(el)
        if t in keep:
            txt = ""
            try:
                txt = el.get_text(" ", strip=True)[:60]
            except Exception:
                pass
            out.append((depth, t, txt))
            depth += 1
        for child in el.children:
            if getattr(child, "name", None):
                walk(child, depth)

    # body 또는 root
    root = soup.body if soup.body else soup
    for c in root.children:
        if getattr(c, "name", None):
            walk(c, 0)
    return out


def _vc_canonical_tree_diff(tree_a: list, tree_b: list) -> dict:
    """제안 6: canonical tree → 시퀀스 LCS distance.

    노드를 (depth, tag) 키로 줄세워 difflib SequenceMatcher 적용. 본격적인
    tree edit distance 가 아니라 시퀀스 거리지만 *대표 신호* 로 충분.
    """
    import difflib
    a_keys = [f"{d}:{t}" for d, t, _ in tree_a]
    b_keys = [f"{d}:{t}" for d, t, _ in tree_b]
    if not a_keys and not b_keys:
        return {"d_n": 0, "c_n": 0, "ratio": 1.0, "missing": 0, "added": 0}
    sm = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
    missing = added = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            missing += i2 - i1
        if tag in ("insert", "replace"):
            added += j2 - j1
    return {
        "d_n": len(a_keys),
        "c_n": len(b_keys),
        "ratio": round(sm.ratio(), 3),
        "missing": missing,
        "added": added,
    }


def _vc_color_hist(img_a_path: str, img_b_path: str) -> dict:
    """제안 7: 색상 histogram cosine similarity. RGB 각 256-bin.

    반환: {"cosine": float (0-1), "rms_diff": float}
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return {"error": "Pillow 미설치"}
    a = _vc_pil_open(img_a_path)
    b = _vc_pil_open(img_b_path)
    if a is None or b is None:
        return {"error": "이미지 로드 실패"}
    ha = a.histogram()  # 768 = 256*3
    hb = b.histogram()
    if len(ha) != len(hb):
        # 다른 모드 (RGBA 등) — RGB 만 사용
        ha = ha[:768]
        hb = hb[:768]
    import math
    na = math.sqrt(sum(v * v for v in ha))
    nb = math.sqrt(sum(v * v for v in hb))
    if na == 0 or nb == 0:
        return {"cosine": 0.0, "rms_diff": 0.0}
    dot = sum(a * b for a, b in zip(ha, hb))
    cos = dot / (na * nb)
    # 정규화한 후 RMS
    sa = sum(ha) or 1
    sb = sum(hb) or 1
    rms = math.sqrt(sum(((a / sa) - (b / sb)) ** 2 for a, b in zip(ha, hb)) / len(ha))
    return {"cosine": round(cos, 4), "rms_diff": round(rms, 5)}


def _vc_compute_all(
    d_full_png: str | None, c_full_png: str | None,
    d_main_png: str | None, c_main_png: str | None,
    bboxes_dwk: list, bboxes_cnf: list,
    raw_html: str, storage_xml: str,
    *,
    overlay_dir: Path | None = None, stem: str = "",
    enabled: dict[str, bool] | None = None,
) -> dict:
    """7개 신호 중 enabled 인 것만 한 번에 계산. 결과 dict 반환."""
    en = enabled or {}
    out: dict = {}
    main_a = d_main_png or d_full_png
    main_b = c_main_png or c_full_png
    if en.get("pixel_diff") and main_a and main_b:
        overlay = str(overlay_dir / f"{stem}.pxdiff.png") if overlay_dir else None
        out["pixel_diff"] = _vc_pixel_diff(main_a, main_b, out_overlay=overlay)
    if en.get("tile_phash") and main_a and main_b:
        overlay = str(overlay_dir / f"{stem}.tile.png") if overlay_dir else None
        out["tile_phash"] = _vc_tile_phash(main_a, main_b, out_overlay=overlay)
    if en.get("element_compare"):
        out["element_compare"] = _vc_element_compare(bboxes_dwk, bboxes_cnf)
    if en.get("ocr") and main_a and main_b:
        out["ocr"] = _vc_ocr_compare(main_a, main_b)
    if en.get("bbox_lcs"):
        out["bbox_lcs"] = _vc_bbox_lcs_compare(bboxes_dwk, bboxes_cnf)
    if en.get("storage_ast") and raw_html and storage_xml:
        d_tree = _vc_canonical_tree(raw_html, is_storage=False)
        c_tree = _vc_canonical_tree(storage_xml, is_storage=True)
        out["storage_ast"] = _vc_canonical_tree_diff(d_tree, c_tree)
    if en.get("color_hist") and main_a and main_b:
        out["color_hist"] = _vc_color_hist(main_a, main_b)
    return out


def _verify_ai_compare(
    queue: list[dict],
    screenshots: dict[str, dict],
    anthropic_api_key: str,
) -> dict[str, dict]:
    """Claude vision 으로 두 스크린샷 자동 비교. 디폴트 off. 페이지당 API
    호출 1회. 결과: doku_id → {score, description}."""
    if not anthropic_api_key:
        log("--with-vision 사용 시 ANTHROPIC_API_KEY 필요. 건너뜀.")
        return {}
    try:
        import anthropic  # type: ignore
    except ImportError:
        log("anthropic SDK 미설치 — `pip install anthropic` 후 재실행.")
        return {}

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    results: dict[str, dict] = {}
    prompt = (
        "두 스크린샷은 동일 문서의 dokuwiki(왼쪽) 와 Confluence(오른쪽) "
        "마이그레이션 결과입니다. 같은 내용을 같은 모양으로 표시하는지 "
        "평가하세요. 응답은 JSON 한 줄: "
        '{"score": 0-100, "summary": "주요 차이 한 문장", "missing": ["..."]}. '
        "score 는 100=거의 동일, 0=완전 다름. 표/이미지/매크로 박스를 우선 보세요."
    )
    for i, q in enumerate(queue, 1):
        doku_id = q["doku_id"]
        sh = screenshots.get(doku_id, {})
        d_png = sh.get("dokuwiki_png")
        c_png = sh.get("confluence_png")
        if not (d_png and c_png and Path(d_png).is_file() and Path(c_png).is_file()):
            continue
        try:
            d_b = base64.b64encode(Path(d_png).read_bytes()).decode()
            c_b = base64.b64encode(Path(c_png).read_bytes()).decode()
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png",
                            "data": d_b,
                        }},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/png",
                            "data": c_b,
                        }},
                    ],
                }],
            )
            text = resp.content[0].text.strip() if resp.content else ""
            m = _re.search(r"\{.*\}", text, _re.DOTALL)
            if m:
                parsed = _json.loads(m.group(0))
                results[doku_id] = {
                    "score": parsed.get("score"),
                    "summary": parsed.get("summary", ""),
                    "missing": parsed.get("missing", []),
                }
        except Exception as e:
            log(f"  [vision fail] {doku_id}: {e}")
        if i % 10 == 0:
            log(f"  vision {i}/{len(queue)}")
    return results


# 양측 iframe 안에 인라인할 기본 CSS — dokuwiki/Confluence 모두에서
# 표·매크로·코드가 *읽을 수 있게* 보이도록 최소화한 reset + typography.
_VERIFY_IFRAME_BASE_CSS = """
html, body { margin: 0; padding: .8em; font: 14px/1.6 -apple-system, sans-serif;
             color: #1d1d1f; word-wrap: break-word; }
h1 { font-size: 1.4em; margin: .5em 0 .3em; }
h2 { font-size: 1.2em; margin: .8em 0 .3em; }
h3 { font-size: 1.05em; margin: .6em 0 .3em; }
p { margin: .4em 0; }
table { border-collapse: collapse; margin: .5em 0; }
th, td { border: 1px solid #d2d2d7; padding: .25em .6em; vertical-align: top; }
th { background: #f5f5f7; font-weight: 600; }
pre, code { font-family: ui-monospace, Menlo, monospace; font-size: .9em;
           background: #f5f5f7; }
pre { padding: .6em .8em; border-radius: 6px; overflow: auto;
      border: 1px solid #e5e5ea; }
code { padding: 1px 4px; border-radius: 3px; }
img { max-width: 100%; height: auto; }
ul, ol { padding-left: 1.5em; }
blockquote { border-left: 3px solid #d2d2d7; margin: .4em 0;
             padding: .2em .8em; color: #6e6e73; }
a { color: #007aff; text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: 0; border-top: 1px solid #e5e5ea; }
/* dokuwiki wrap 의미 클래스 (좌측 영역) */
.wrap_info, .wrap_help { background: #e8f3ff; border-left: 4px solid #007aff;
                         padding: .5em .8em; margin: .4em 0; }
.wrap_tip { background: #ecf8ec; border-left: 4px solid #34c759;
            padding: .5em .8em; margin: .4em 0; }
.wrap_important, .wrap_note { background: #fff5e6;
                              border-left: 4px solid #ff9500;
                              padding: .5em .8em; margin: .4em 0; }
.wrap_alert, .wrap_warning, .wrap_danger { background: #fde8e6;
                                           border-left: 4px solid #ff3b30;
                                           padding: .5em .8em; margin: .4em 0; }
.wrap_box, .wrap_round { background: #fafafa; border: 1px solid #d2d2d7;
                         padding: .5em .8em; margin: .4em 0;
                         border-radius: 6px; }
/* Confluence body-format=view 의 매크로 placeholder */
.conf-macro, .confluence-information-macro { background: #f0f6ff;
                                             border-left: 4px solid #007aff;
                                             padding: .5em .8em; margin: .4em 0; }
.confluence-information-macro-tip { border-color: #34c759; background: #ecf8ec; }
.confluence-information-macro-note { border-color: #ff9500; background: #fff5e6; }
.confluence-information-macro-warning { border-color: #ff3b30; background: #fde8e6; }
/* dokuwiki 가 빠뜨린 매크로 잔존 (변환기 strip 후엔 안 보임) */
"""


def _verify_render_iframe_doc(body_html: str) -> str:
    """좌·우 영역에 들어갈 iframe srcdoc 본문 — 양측 CSS 충돌 격리."""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{_VERIFY_IFRAME_BASE_CSS}</style></head>"
        f"<body>{body_html}</body></html>"
    )


def _verify_render_metrics_row(metrics: dict) -> str:
    """양측 카운트 비교 미니 테이블 + 자동 신호 (sentence/artifact/code/heading/link)."""
    rows = metrics.get("rows", [])
    cells = []
    for label, d, s, c, ok in rows:
        if d == 0 and s == 0 and (c <= 0):
            continue
        c_disp = "—" if c < 0 else str(c)
        cls = "metric-ok" if ok else "metric-bad"
        cells.append(
            f'<span class="metric {cls}" title="dokuwiki={d} storage={s} '
            f'view={c_disp}">{_h.escape(label)}: {d}'
            + (f"≠{s}" if d != s else "")
            + (f"/v{c_disp}" if c >= 0 else "")
            + "</span>"
        )

    auto = metrics.get("auto", {}) or {}
    auto_cells = []
    sent = auto.get("sentence") or {}
    if sent.get("d_sentences"):
        r = sent.get("sentence_ratio", 1.0)
        cls = "metric-ok" if r >= 0.85 else ("metric-warn" if r >= 0.7 else "metric-bad")
        title = (
            f"문장 {sent['d_sentences']}→{sent['c_sentences']}, "
            f"누락 {sent['missing']}, 추가 {sent['added']}"
        )
        if sent.get("examples_missing"):
            title += " | 손실 예: " + " | ".join(sent["examples_missing"])
        auto_cells.append(
            f'<span class="metric {cls}" title="{_h.escape(title, quote=True)}">'
            f'문장 {r:.2f}</span>'
        )

    arts = auto.get("artifacts") or {}
    total_missing = sum(a.get("missing", 0) for a in arts.values())
    if total_missing > 0 or any(a.get("d_count", 0) for a in arts.values()):
        parts = []
        for kind, a in arts.items():
            if a.get("d_count", 0) == 0 and a.get("c_count", 0) == 0:
                continue
            parts.append(f"{kind}:{a.get('d_count',0)}→{a.get('c_count',0)} (-{a.get('missing',0)})")
        title = "; ".join(parts)
        cls = "metric-bad" if total_missing >= 3 else ("metric-warn" if total_missing > 0 else "metric-ok")
        auto_cells.append(
            f'<span class="metric {cls}" title="{_h.escape(title, quote=True)}">'
            f'artifact -{total_missing}</span>'
        )

    cb = auto.get("code_blocks") or {}
    if cb.get("d_code_blocks") or cb.get("c_code_blocks"):
        m = cb.get("missing", 0)
        cls = "metric-ok" if m == 0 else "metric-bad"
        cb_title = f"d={cb.get('d_code_blocks', 0)} c={cb.get('c_code_blocks', 0)} matched={cb.get('matched', 0)}"
        auto_cells.append(
            f'<span class="metric {cls}" title="{cb_title}">'
            f"코드 {cb.get('matched', 0)}/{cb.get('d_code_blocks', 0)}</span>"
        )

    hd = auto.get("headings") or {}
    if hd.get("d_headings", 0) >= 2:
        r = hd.get("lcs_ratio", 1.0)
        cls = "metric-ok" if r >= 0.85 else ("metric-warn" if r >= 0.7 else "metric-bad")
        title = f"d_headings={hd['d_headings']} c_headings={hd['c_headings']} 누락={hd['missing']}"
        if hd.get("examples_missing"):
            title += " | " + " | ".join(hd["examples_missing"])
        auto_cells.append(
            f'<span class="metric {cls}" title="{_h.escape(title, quote=True)}">'
            f'헤딩 LCS {r:.2f}</span>'
        )

    lr = auto.get("link_resolution") or {}
    if lr.get("placeholder", 0) > 0 or lr.get("resolved", 0) > 0:
        rate = lr.get("rate", 1.0)
        cls = "metric-ok" if rate >= 0.95 else ("metric-warn" if rate >= 0.8 else "metric-bad")
        lr_title = f"resolved={lr.get('resolved', 0)} placeholder={lr.get('placeholder', 0)}"
        auto_cells.append(
            f'<span class="metric {cls}" title="{lr_title}">'
            f'링크해소 {rate:.2f}</span>'
        )

    auto_ng = auto.get("auto_ng")
    if auto_ng:
        auto_cells.append(
            f'<span class="metric metric-bad" title="자동 추정 NG 사유">자동 NG: {_h.escape(auto_ng)}</span>'
        )

    # Phase 4 추가 시각 비교 신호
    vc = metrics.get("vc") or {}
    vc_cells = []
    pd = vc.get("pixel_diff") or {}
    if "diff_ratio" in pd:
        ratio = pd["diff_ratio"]
        cls = "metric-ok" if ratio < 0.05 else ("metric-warn" if ratio < 0.15 else "metric-bad")
        vc_cells.append(
            f'<span class="metric {cls}" title="픽셀 diff (chrome 마스킹 후)">'
            f'pixel {ratio*100:.1f}%</span>'
        )
    tp = vc.get("tile_phash") or {}
    if "max_distance" in tp:
        cls = "metric-ok" if tp.get("n_bad_tiles", 0) == 0 else (
            "metric-warn" if tp.get("n_bad_tiles", 0) <= 2 else "metric-bad"
        )
        n = tp.get("n_bad_tiles", 0)
        nt = tp.get("n_tiles", 0)
        title = f"max={tp['max_distance']} mean={tp['mean_distance']} bad={n}/{nt}"
        vc_cells.append(
            f'<span class="metric {cls}" title="{title}">타일 {n}/{nt}</span>'
        )
    ec = vc.get("element_compare") or {}
    if ec.get("d_n", 0) > 0 or ec.get("c_n", 0) > 0:
        r = ec.get("ratio", 0)
        cls = "metric-ok" if r >= 0.85 else ("metric-warn" if r >= 0.7 else "metric-bad")
        ec_title = f"elements d={ec.get('d_n', 0)} c={ec.get('c_n', 0)} matched={ec.get('matched', 0)}"
        vc_cells.append(
            f'<span class="metric {cls}" title="{ec_title}">요소 LCS {r:.2f}</span>'
        )
    oc = vc.get("ocr") or {}
    if oc.get("sentence_ratio") is not None:
        r = oc["sentence_ratio"]
        cls = "metric-ok" if r >= 0.85 else ("metric-warn" if r >= 0.6 else "metric-bad")
        vc_cells.append(
            f'<span class="metric {cls}" title="OCR 문장 정렬 (이미지 텍스트 비교)">'
            f'OCR {r:.2f}</span>'
        )
    bl = vc.get("bbox_lcs") or {}
    if bl.get("d_n", 0) > 0 or bl.get("c_n", 0) > 0:
        r = bl.get("lcs_ratio", 0)
        wd = bl.get("mean_width_diff", 0)
        cls = "metric-ok" if r >= 0.85 and wd < 0.15 else (
            "metric-warn" if r >= 0.7 else "metric-bad"
        )
        title = f"bbox LCS d={bl['d_n']} c={bl['c_n']} matched={bl.get('matched',0)} mean_w_diff={wd}"
        vc_cells.append(
            f'<span class="metric {cls}" title="{title}">레이아웃 {r:.2f}</span>'
        )
    sa = vc.get("storage_ast") or {}
    if sa.get("d_n", 0) > 0 or sa.get("c_n", 0) > 0:
        r = sa.get("ratio", 0)
        cls = "metric-ok" if r >= 0.85 else ("metric-warn" if r >= 0.7 else "metric-bad")
        title = f"storage AST d={sa['d_n']} c={sa['c_n']} missing={sa['missing']} added={sa['added']}"
        vc_cells.append(
            f'<span class="metric {cls}" title="{title}">AST {r:.2f}</span>'
        )
    ch = vc.get("color_hist") or {}
    if "cosine" in ch:
        cos = ch["cosine"]
        cls = "metric-ok" if cos >= 0.95 else ("metric-warn" if cos >= 0.85 else "metric-bad")
        vc_cells.append(
            f'<span class="metric {cls}" title="색상 histogram cosine sim">'
            f'색상 {cos:.3f}</span>'
        )

    parts_html = []
    if cells:
        parts_html.append('<div class="metrics">' + "".join(cells) + "</div>")
    if auto_cells:
        parts_html.append('<div class="metrics metrics-auto">' + "".join(auto_cells) + "</div>")
    if vc_cells:
        parts_html.append('<div class="metrics metrics-vc">' + "".join(vc_cells) + "</div>")
    return "".join(parts_html)


def _verify_render_html(
    queue: list[dict],
    confluence_bodies: dict[str, str | None],
    base_view_url: str | None,
    reviewer: str,
    metrics_map: dict[str, dict] | None = None,
    attachment_map: dict[str, tuple[int, int]] | None = None,
    screenshot_map: dict[str, dict] | None = None,
    vision_map: dict[str, dict] | None = None,
) -> str:
    """우선순위 큐를 받아 단일 정적 HTML 갤러리 생성. iframe 격리 + 자동
    지표 + 첨부 점검 + (옵션) 스크린샷 + (옵션) AI vision 점수."""
    metrics_map = metrics_map or {}
    attachment_map = attachment_map or {}
    screenshot_map = screenshot_map or {}
    vision_map = vision_map or {}
    total = len(queue)

    cards: list[str] = []
    for idx, q in enumerate(queue, 1):
        raw_path = q.get("raw_xhtml_path") or ""
        storage_path = q.get("storage_path") or ""
        raw_html = ""
        storage_xml = ""
        if raw_path and Path(raw_path).is_file():
            try:
                raw_html = Path(raw_path).read_text(encoding="utf-8")
            except OSError:
                pass
        if storage_path and Path(storage_path).is_file():
            try:
                storage_xml = Path(storage_path).read_text(encoding="utf-8")
            except OSError:
                pass
        confluence_body = confluence_bodies.get(q["doku_id"])

        doku_id = q["doku_id"]
        page_id = q["confluence_page_id"]
        flags = ", ".join(q["flags"]) if q["flags"] else "—"
        score = q["score"]
        title = q["title"]
        deeplink = (
            f"{base_view_url.rstrip('/')}/pages/{page_id}"
            if (base_view_url and page_id) else ""
        )

        # iframe 격리 — 좌측: dokuwiki raw, 우측: Confluence body 또는 storage
        left_src = _verify_render_iframe_doc(raw_html or "<p><em>raw 없음</em></p>")
        if confluence_body:
            right_src = _verify_render_iframe_doc(confluence_body)
            right_label = "Confluence 실제 렌더"
        else:
            right_src = _verify_render_iframe_doc(storage_xml or "<p><em>storage 없음</em></p>")
            right_label = "우리 storage XML (fallback)"

        # 지표 / 첨부 / 스크린샷 / vision
        metrics_html = _verify_render_metrics_row(metrics_map.get(doku_id, {}))
        att = attachment_map.get(doku_id)
        att_html = ""
        if att and att[1] > 0:
            ok, total_a = att
            cls = "metric-ok" if ok == total_a else "metric-bad"
            att_html = (
                f'<span class="metric {cls}" title="Confluence v2 attachments GET">'
                f'첨부 {ok}/{total_a}</span>'
            )

        sh = screenshot_map.get(doku_id, {})
        sim_html = ""
        if sh.get("similarity") is not None:
            sim = sh["similarity"]
            cls = "metric-ok" if sim >= 0.85 else ("metric-warn" if sim >= 0.6 else "metric-bad")
            sim_html = (
                f'<span class="metric {cls}" title="perceptual hash">'
                f'유사도 {sim}</span>'
            )

        screenshots_html = ""
        if sh.get("dokuwiki_png") or sh.get("confluence_png"):
            d_png = sh.get("dokuwiki_png") or ""
            c_png = sh.get("confluence_png") or ""
            screenshots_html = (
                f'<details class="screenshots"><summary>스크린샷 비교</summary>'
                f'<div class="shots">'
                + (f'<a href="{_h.escape(d_png)}" target="_blank">'
                   f'<img src="{_h.escape(d_png)}" alt="dokuwiki"></a>' if d_png else '')
                + (f'<a href="{_h.escape(c_png)}" target="_blank">'
                   f'<img src="{_h.escape(c_png)}" alt="confluence"></a>' if c_png else '')
                + '</div></details>'
            )

        vision = vision_map.get(doku_id)
        vision_html = ""
        if vision:
            v_score = vision.get("score")
            v_sum = vision.get("summary", "")
            v_miss = vision.get("missing") or []
            cls = (
                "metric-ok" if (v_score or 0) >= 85
                else ("metric-warn" if (v_score or 0) >= 60 else "metric-bad")
            )
            miss_str = ", ".join(str(m) for m in v_miss[:3])
            vision_html = (
                f'<div class="vision {cls}">'
                f'<strong>AI: {v_score}</strong> — {_h.escape(v_sum)}'
                + (f' <span class="vision-miss">누락: {_h.escape(miss_str)}</span>' if miss_str else '')
                + '</div>'
            )

        cards.append(f"""
<section class="card" id="card-{idx}" data-doku="{_h.escape(doku_id)}">
  <header>
    <span class="idx">[{idx} / {total}]</span>
    <code class="doku-id">{_h.escape(doku_id)}</code>
    <span class="title">{_h.escape(title)}</span>
    <span class="score">score {score}</span>
    <span class="flags">{_h.escape(flags)}</span>
    {f'<a class="link" href="{_h.escape(deeplink)}" target="_blank">Confluence 열기 →</a>' if deeplink else ''}
  </header>
  <div class="meta-row">{metrics_html}{att_html}{sim_html}</div>
  {vision_html}
  <div class="grid">
    <div class="col raw">
      <h3>DokuWiki raw (export_xhtmlbody)</h3>
      <iframe class="body" sandbox="allow-same-origin" srcdoc="{_h.escape(left_src, quote=True)}"></iframe>
    </div>
    <div class="col conf">
      <h3>{right_label}</h3>
      <iframe class="body" sandbox="allow-same-origin" srcdoc="{_h.escape(right_src, quote=True)}"></iframe>
    </div>
  </div>
  {screenshots_html}
  <footer>
    <label><input type="radio" name="d-{idx}" value="OK"> OK</label>
    <label><input type="radio" name="d-{idx}" value="NG"> NG</label>
    <label><input type="radio" name="d-{idx}" value="DEFER"> 보류</label>
    <select class="ng-tag">
      <option value="">사유 분류 (NG/보류 시)</option>
      <option value="text">텍스트 누락/오류</option>
      <option value="table">표 깨짐</option>
      <option value="image">이미지/위치</option>
      <option value="macro">매크로 스타일</option>
      <option value="attachment">첨부</option>
      <option value="link">링크</option>
      <option value="other">기타</option>
    </select>
    <input type="text" class="notes" placeholder="메모 (선택)" maxlength="500">
  </footer>
</section>""")

    queue_json = _json.dumps(
        [{"doku_id": q["doku_id"], "content_hash": q["content_hash"]} for q in queue],
        ensure_ascii=False,
    )

    js = """
<script>
const QUEUE = __QUEUE_JSON__;
const REVIEWER = "__REVIEWER__";

function gatherDecisions() {
  const out = [];
  document.querySelectorAll('section.card').forEach((card, i) => {
    const checked = card.querySelector('input[type=radio]:checked');
    if (!checked) return;
    const notes = card.querySelector('input.notes').value || '';
    const tag = card.querySelector('select.ng-tag').value || '';
    out.push({
      doku_id: QUEUE[i].doku_id,
      decision: checked.value,
      notes: notes,
      ng_tag: tag,
      source_hash: QUEUE[i].content_hash,
      reviewer: REVIEWER,
      reviewed_at: new Date().toISOString(),
    });
  });
  return out;
}

function updateBadge() {
  const decisions = gatherDecisions();
  const ok = decisions.filter(d => d.decision === 'OK').length;
  const ng = decisions.filter(d => d.decision === 'NG').length;
  const df = decisions.filter(d => d.decision === 'DEFER').length;
  const reviewed = decisions.length;
  // NG 분류 분포
  const tagCounts = {};
  decisions.filter(d => d.decision !== 'OK' && d.ng_tag).forEach(d => {
    tagCounts[d.ng_tag] = (tagCounts[d.ng_tag] || 0) + 1;
  });
  const tagStr = Object.entries(tagCounts)
    .sort((a,b) => b[1]-a[1])
    .map(([k,v]) => k + ':' + v).join(' ');
  document.getElementById('progress').textContent =
    reviewed + ' / ' + QUEUE.length + ' reviewed (OK ' + ok +
    ' / NG ' + ng + ' / DEFER ' + df + (tagStr ? ' · ' + tagStr : '') + ')';
}

document.addEventListener('change', updateBadge);
document.addEventListener('input', updateBadge);

document.getElementById('download').addEventListener('click', () => {
  const decisions = gatherDecisions();
  const blob = new Blob([JSON.stringify(decisions, null, 2)],
                       {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'verify_decisions.json';
  a.click();
  URL.revokeObjectURL(url);
});

// 키보드 단축키: 카드 안에서 1=OK, 2=NG, 3=DEFER, Enter=다음
document.addEventListener('keydown', (e) => {
  if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT')) return;
  const focused = document.activeElement && document.activeElement.closest('section.card');
  const card = focused || document.querySelector('section.card');
  if (!card) return;
  const map = {'1':'OK','2':'NG','3':'DEFER'};
  if (map[e.key]) {
    const r = card.querySelector('input[type=radio][value="'+map[e.key]+'"]');
    if (r) { r.checked = true; updateBadge(); }
  }
});

updateBadge();
</script>
""".replace("__QUEUE_JSON__", queue_json).replace("__REVIEWER__", reviewer)

    css = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         margin: 0; padding: 0; background: #f5f5f7; color: #1d1d1f; }
  .topbar { position: sticky; top: 0; background: #fff; padding: .8em 1.2em;
            border-bottom: 1px solid #d2d2d7; z-index: 10;
            display: flex; gap: 1em; align-items: center; }
  .topbar h1 { margin: 0; font-size: 1em; }
  #progress { font-weight: 600; }
  #download { padding: .4em 1em; border: 1px solid #007aff; background: #007aff;
              color: #fff; border-radius: 6px; cursor: pointer; }
  section.card { background: #fff; margin: 1em; padding: 1em; border-radius: 8px;
                 box-shadow: 0 1px 2px rgba(0,0,0,.04); }
  section.card header { display: flex; gap: .8em; align-items: center;
                        flex-wrap: wrap; margin-bottom: .8em; }
  .idx { color: #6e6e73; font-size: .85em; min-width: 5em; }
  .doku-id { background: #f0f0f3; padding: .15em .4em; border-radius: 4px;
             font-size: .9em; }
  .title { font-weight: 600; }
  .score { color: #6e6e73; font-size: .85em; }
  .flags { color: #007aff; font-size: .85em; }
  a.link { margin-left: auto; color: #007aff; text-decoration: none; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1em; }
  .col { background: #fafafa; padding: .8em; border-radius: 6px; }
  .col h3 { margin-top: 0; font-size: .85em; color: #6e6e73; }
  iframe.body { width: 100%; height: 60vh; border: 1px solid #e5e5ea;
                border-radius: 4px; background: #fff; }
  .meta-row { display: flex; gap: .4em; flex-wrap: wrap; margin: .4em 0 .6em; }
  .metrics { display: flex; gap: .3em; flex-wrap: wrap; }
  .metric { font-size: .78em; padding: .15em .5em; border-radius: 10px;
            background: #f0f0f3; color: #1d1d1f; }
  .metric.metric-ok   { background: #e6f4ea; color: #137333; }
  .metric.metric-warn { background: #fef3e0; color: #b06000; }
  .metric.metric-bad  { background: #fde8e6; color: #c5221f; }
  .vision { font-size: .9em; padding: .5em .8em; border-radius: 6px;
            margin: .4em 0; }
  .vision.metric-ok   { background: #e6f4ea; }
  .vision.metric-warn { background: #fef3e0; }
  .vision.metric-bad  { background: #fde8e6; }
  .vision-miss { color: #6e6e73; font-size: .85em; }
  .screenshots { margin-top: .6em; }
  .screenshots summary { cursor: pointer; color: #6e6e73; font-size: .85em; }
  .shots { display: grid; grid-template-columns: 1fr 1fr; gap: .8em;
           margin-top: .5em; }
  .shots img { width: 100%; border: 1px solid #d2d2d7; border-radius: 4px; }
  footer { margin-top: .8em; display: flex; gap: .8em; align-items: center;
           flex-wrap: wrap; }
  footer label { font-size: .9em; }
  footer .notes { flex: 1; padding: .3em .5em; border: 1px solid #d2d2d7;
                  border-radius: 4px; min-width: 12em; }
  footer .ng-tag { padding: .3em .4em; border: 1px solid #d2d2d7;
                   border-radius: 4px; font-size: .85em; }
  ac\\:structured-macro { display: block; border-left: 4px solid #007aff;
                           background: #f0f6ff; padding: .5em .8em; margin: .4em 0; }
  ac\\:structured-macro[ac\\:name="info"] { border-color: #007aff; background: #e8f3ff; }
  ac\\:structured-macro[ac\\:name="tip"]  { border-color: #34c759; background: #ecf8ec; }
  ac\\:structured-macro[ac\\:name="note"] { border-color: #ff9500; background: #fff5e6; }
  ac\\:structured-macro[ac\\:name="warning"] { border-color: #ff3b30; background: #fde8e6; }
</style>
"""

    head = (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        f'<title>verify — {total} pages</title>{css}</head><body>'
    )
    topbar = (
        '<div class="topbar">'
        f'<h1>verify ({total} 페이지)</h1>'
        '<span id="progress">0 / ? reviewed</span>'
        '<button id="download">JSON 다운로드</button>'
        f'<span style="color:#6e6e73;font-size:.85em">reviewer: {_h.escape(reviewer)}</span>'
        '</div>'
    )
    return head + topbar + "".join(cards) + js + "</body></html>"


def _verify_resolve_vc_flags(args: argparse.Namespace) -> dict[str, bool]:
    """Phase 4 옵션 7개 + --with-all-extra-signals 결합 → enabled dict."""
    extra_all = getattr(args, "with_all_extra_signals", False)
    return {
        "pixel_diff":      extra_all or getattr(args, "with_pixel_diff", False),
        "tile_phash":      extra_all or getattr(args, "with_tile_phash", False),
        "element_compare": extra_all or getattr(args, "with_element_compare", False),
        "ocr":             extra_all or getattr(args, "with_ocr", False),
        "bbox_lcs":        extra_all or getattr(args, "with_bbox_lcs", False),
        "storage_ast":     extra_all or getattr(args, "with_storage_ast", False),
        "color_hist":      extra_all or getattr(args, "with_color_hist", False),
    }


def _verify_fetch_view_bodies(
    session, base: str, queue: list[dict], body_format: str,
) -> dict[str, str | None]:
    """queue 의 각 페이지에 대해 Confluence body-format=view 본문 fetch."""
    bodies: dict[str, str | None] = {}
    log(f"Confluence body-format={body_format} fetch 시작 ({len(queue)} 페이지)")
    for i, q in enumerate(queue, 1):
        page_id = q["confluence_page_id"]
        if not page_id:
            continue
        bodies[q["doku_id"]] = _verify_fetch_confluence_view(
            session, base, page_id, body_format=body_format,
        )
        if i % 20 == 0:
            log(f"  fetched {i}/{len(queue)}")
    return bodies


def _verify_compute_phase4_signals(
    queue: list[dict], screenshot_map: dict[str, dict],
    vc_enabled: dict[str, bool], conn: sqlite3.Connection,
    shots_dir: Path,
) -> dict[str, dict]:
    """Phase 4 시각 비교 7신호 페이지별 계산. raw/storage 본문은 state.db
    의 pages 테이블에서 로드 (storage_ast 신호 용)."""
    out: dict[str, dict] = {}
    log(f"Phase 4 시각 비교 신호 계산: {[k for k,v in vc_enabled.items() if v]}")
    for i, q in enumerate(queue, 1):
        doku_id = q["doku_id"]
        sh = screenshot_map.get(doku_id, {})
        stem = doku_id.replace(":", "_")
        raw_html, storage_xml = "", ""
        row = conn.execute(
            "SELECT raw_xhtml_path, storage_path FROM pages WHERE doku_id=?",
            (doku_id,),
        ).fetchone()
        if row:
            rp, sp = row
            if rp and Path(rp).is_file():
                try:
                    raw_html = Path(rp).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
            if sp and Path(sp).is_file():
                try:
                    storage_xml = Path(sp).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    pass
        out[doku_id] = _vc_compute_all(
            d_full_png=sh.get("dokuwiki_png"),
            c_full_png=sh.get("confluence_png"),
            d_main_png=sh.get("dokuwiki_main_png"),
            c_main_png=sh.get("confluence_main_png"),
            bboxes_dwk=sh.get("bboxes_dwk") or [],
            bboxes_cnf=sh.get("bboxes_cnf") or [],
            raw_html=raw_html, storage_xml=storage_xml,
            overlay_dir=shots_dir, stem=stem,
            enabled=vc_enabled,
        )
        if i % 20 == 0:
            log(f"  Phase 4 신호 {i}/{len(queue)}")
    return out


def cmd_verify_build(args: argparse.Namespace) -> int:
    """verify build: 시각 검수 큐 + 단일 HTML 갤러리 생성 (docs/visual-audit.md).

    우선순위 큐 (score 기반) 로 UPLOADED 페이지 N개 (--sample) 또는 전체
    (--strategy=all) 선정. 옵션으로 (1) Confluence body-format=view fetch
    (2) Playwright 풀-페이지 스크린샷 + phash (3) AI vision (Claude) 자동
    비교 (4) Phase 4 추가 신호 7종 (pixel-diff / tile-phash / element-
    compare / OCR / bbox-LCS / storage-AST / color-histogram). 결과는
    단일 HTML 갤러리 + state.db 의 `verify_decisions` 테이블에 메타 저장.
    """
    conn = db_connect(args.db)
    _ensure_verify_schema(conn)

    queue = _verify_build_queue(
        conn,
        sample=args.sample,
        strategy=args.strategy,
        resume=args.resume,
    )
    if not queue:
        log("verify 큐가 비었습니다 (UPLOADED 페이지 0 또는 모두 검수됨).")
        conn.close()
        return 1

    log(f"verify 큐: {len(queue)} 페이지 (strategy={args.strategy}, "
        f"sample={args.sample}, resume={args.resume})")

    confluence_bodies: dict[str, str | None] = {}
    base_view_url: str | None = None
    session = None
    if args.with_confluence_view or args.with_attachment_check:
        if not args.email or not args.api_token:
            log("--with-confluence-view / --with-attachment-check 는 자격증명 필요.")
        else:
            session = _confluence_session(args)
            if session is None:
                log("Confluence 세션 생성 실패.")

    if args.with_confluence_view and session is not None:
        base = args.base_url.rstrip("/")
        base_view_url = base
        confluence_bodies = _verify_fetch_view_bodies(
            session, base, queue, args.body_format,
        )

    # 시각 지표 자동 계산 — raw / storage / (옵션) view 양측
    metrics_map: dict[str, dict] = {}
    log("structural metrics 계산 중...")
    for q in queue:
        doku_id = q["doku_id"]
        raw_html = ""
        storage_xml = ""
        raw_path = q.get("raw_xhtml_path") or ""
        storage_path = q.get("storage_path") or ""
        if raw_path and Path(raw_path).is_file():
            try:
                raw_html = Path(raw_path).read_text(encoding="utf-8")
            except OSError:
                pass
        if storage_path and Path(storage_path).is_file():
            try:
                storage_xml = Path(storage_path).read_text(encoding="utf-8")
            except OSError:
                pass
        metrics_map[doku_id] = _verify_compute_metrics(
            doku_id, conn, raw_html, storage_xml,
            confluence_bodies.get(doku_id),
        )

    # 첨부 HEAD 점검 (옵션)
    attachment_map: dict[str, tuple[int, int]] = {}
    if args.with_attachment_check and session is not None:
        base = args.base_url.rstrip("/")
        log(f"첨부 자동 점검 시작 ({len(queue)} 페이지)")
        for i, q in enumerate(queue, 1):
            attachment_map[q["doku_id"]] = _verify_check_attachments(
                conn, session, base, q["doku_id"]
            )
            if i % 20 == 0:
                log(f"  attachment check {i}/{len(queue)}")

    # Phase 4 옵션 결합
    vc_enabled = _verify_resolve_vc_flags(args)
    needs_main_capture = any(vc_enabled[k] for k in ("pixel_diff", "tile_phash", "ocr", "color_hist"))
    needs_bbox = any(vc_enabled[k] for k in ("element_compare", "bbox_lcs"))

    # Playwright 스크린샷 + phash (옵션)
    screenshot_map: dict[str, dict] = {}
    out_dir_for_shots = Path(args.output).parent if args.output else Path(".")
    shots_dir = out_dir_for_shots / "verify-screenshots"
    if args.with_screenshots or needs_main_capture or needs_bbox:
        log(f"Playwright 스크린샷 시작 → {shots_dir} ({len(queue)} 페이지)")
        # confluence_bodies (body-format=view) 를 Playwright 에 직접 주입 →
        # Confluence Cloud UI 인증 우회 (Basic Auth 만으로 페이지 view 접근 불가)
        screenshot_map = _verify_capture_screenshots(
            queue, shots_dir,
            dokuwiki_base=args.dokuwiki_base_url or env_default("DOKUWIKI_BASE_URL"),
            confluence_base=args.base_url,
            confluence_email=args.email or "",
            confluence_token=args.api_token or "",
            capture_main_only=needs_main_capture,
            extract_bbox=needs_bbox,
            confluence_view_html={k: v for k, v in confluence_bodies.items() if v},
        )

    # Phase 4 신호 계산 (페이지별)
    vc_map: dict[str, dict] = {}
    if any(vc_enabled.values()):
        vc_map = _verify_compute_phase4_signals(
            queue, screenshot_map, vc_enabled, conn, shots_dir,
        )

    # AI vision 비교 (옵션, 스크린샷 필수)
    vision_map: dict[str, dict] = {}
    if args.with_vision:
        if not screenshot_map:
            log("--with-vision 은 --with-screenshots 와 함께 사용. 건너뜀.")
        else:
            log(f"AI vision 비교 시작 ({len(queue)} 페이지)")
            vision_map = _verify_ai_compare(
                queue, screenshot_map,
                anthropic_api_key=env_default("ANTHROPIC_API_KEY"),
            )

    reviewer = args.reviewer or args.email or "anonymous"
    # vc_map 을 metrics_map 에 합쳐 카드 렌더링이 접근 가능하게
    for doku_id, sig in vc_map.items():
        if doku_id in metrics_map:
            metrics_map[doku_id]["vc"] = sig
        else:
            metrics_map[doku_id] = {"rows": [], "vc": sig}
    html = _verify_render_html(
        queue, confluence_bodies, base_view_url, reviewer,
        metrics_map=metrics_map,
        attachment_map=attachment_map,
        screenshot_map=screenshot_map,
        vision_map=vision_map,
    )

    out_path = Path(args.output) if args.output else Path("verify-gallery.html")
    out_path.write_text(html, encoding="utf-8")
    log(f"verify 갤러리 → {out_path}")
    log(f"  열어 검수 → 'JSON 다운로드' → "
        f"`python run.py verify import <파일>` 으로 반영")

    conn.close()
    return 0


def cmd_verify_import(args: argparse.Namespace) -> int:
    """verify import: 브라우저에서 다운로드한 verify_decisions.json 을 state.db
    에 반영.

    사용자가 verify-gallery.html 에서 OK/NG/DEFER 라디오 + 노트 + 분류를
    선택하고 "JSON 저장" 으로 받은 파일. state.db 갱신: `verify_decisions`
    테이블에 inserted/updated. content_hash 도 함께 저장 — 향후 페이지
    재변환되어 hash 변경 시 stale 표시 가능 (`verify status`).
    """
    if not Path(args.path).is_file():
        log(f"파일 없음: {args.path}")
        return 1

    conn = db_connect(args.db)
    _ensure_verify_schema(conn)

    try:
        items = _json.loads(Path(args.path).read_text(encoding="utf-8"))
    except _json.JSONDecodeError as e:
        log(f"JSON 파싱 실패: {e}")
        conn.close()
        return 1

    if not isinstance(items, list):
        log("JSON 의 최상위는 배열이어야 합니다.")
        conn.close()
        return 1

    inserted = 0
    updated = 0
    skipped = 0
    for item in items:
        doku_id = item.get("doku_id")
        decision = item.get("decision")
        if not doku_id or decision not in ("OK", "NG", "DEFER"):
            skipped += 1
            continue
        existing = conn.execute(
            "SELECT decision FROM verify_decisions WHERE doku_id=?",
            (doku_id,),
        ).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO verify_decisions "
            "(doku_id, decision, notes, reviewer, reviewed_at, "
            " source_hash, visual_score, flags, ng_tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doku_id,
                decision,
                item.get("notes") or "",
                item.get("reviewer") or "",
                item.get("reviewed_at") or now_iso(),
                item.get("source_hash") or "",
                item.get("visual_score"),
                item.get("flags") or "",
                item.get("ng_tag") or "",
            ),
        )
        if existing:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    conn.close()

    log(f"verify import: 신규 {inserted} / 갱신 {updated} / 무시 {skipped}")
    return 0


def cmd_verify_status(args: argparse.Namespace) -> int:
    """verify status: 검수 진행률 요약 (read-only).

    state.db 의 `verify_decisions` 의 decision 별 카운트 (OK/NG/DEFER) +
    stale (페이지 content_hash 변경된 항목) 식별 + NG/stale 페이지 목록
    (`--verbose`). state.db 변경 없음.
    """
    conn = db_connect(args.db)
    _ensure_verify_schema(conn)

    counts: dict[str, int] = {}
    for d, n in conn.execute(
        "SELECT decision, COUNT(*) FROM verify_decisions GROUP BY decision"
    ).fetchall():
        counts[d] = n
    total = sum(counts.values())

    # stale: source_hash 가 현재 content_hash 와 다른 OK
    stale_rows = conn.execute(
        "SELECT v.doku_id "
        "  FROM verify_decisions v "
        "  JOIN pages p ON p.doku_id=v.doku_id "
        " WHERE v.decision='OK' "
        "   AND COALESCE(v.source_hash,'') <> COALESCE(p.content_hash,'')"
    ).fetchall()
    stale = len(stale_rows)

    uploaded_total = conn.execute(
        "SELECT COUNT(*) FROM pages "
        " WHERE status='UPLOADED' AND confluence_page_id IS NOT NULL"
    ).fetchone()[0]

    print("==== verify status ====")
    print(f"  UPLOADED total:    {uploaded_total}")
    print(f"  decisions logged:  {total}")
    for k in ("OK", "NG", "DEFER"):
        n = counts.get(k, 0)
        pct = (n / uploaded_total * 100) if uploaded_total else 0
        print(f"  {k:6s}             {n:5d}  ({pct:.1f}% of uploaded)")
    print(f"  stale (변환 후 미재검수): {stale}")

    # NG/DEFER 사유 분포
    try:
        tag_rows = conn.execute(
            "SELECT COALESCE(ng_tag,''), COUNT(*) FROM verify_decisions "
            " WHERE decision IN ('NG','DEFER') AND COALESCE(ng_tag,'') <> '' "
            " GROUP BY ng_tag ORDER BY COUNT(*) DESC"
        ).fetchall()
        if tag_rows:
            print("\nNG/DEFER 사유 분포:")
            for tag, n in tag_rows:
                print(f"  {tag:12s} {n}")
    except sqlite3.OperationalError:
        pass

    if args.verbose:
        ng_rows = conn.execute(
            "SELECT doku_id, notes FROM verify_decisions "
            " WHERE decision='NG' ORDER BY doku_id"
        ).fetchall()
        if ng_rows:
            print("\nNG 페이지:")
            for d, notes in ng_rows:
                print(f"  - {d}  {notes or ''}")
        if stale_rows:
            print("\nstale 페이지:")
            for (d,) in stale_rows:
                print(f"  - {d}")

    conn.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """verify 부모 명령 — `args.action` (build/import/status) 으로 dispatch."""
    if args.action == "build":
        return cmd_verify_build(args)
    if args.action == "import":
        return cmd_verify_import(args)
    if args.action == "status":
        return cmd_verify_status(args)
    log(f"unknown verify action: {args.action}")
    return 2


# § wizard (대화형 step-by-step CLI, docs/runbook.md 의 시퀀스 자동화)

WIZARD_DDL = """
CREATE TABLE IF NOT EXISTS wizard_state (
    step_key      TEXT PRIMARY KEY,
    status        TEXT NOT NULL,    -- pending / running / done / failed / skipped / interrupted
    started_at    TEXT,
    finished_at   TEXT,
    summary       TEXT,
    error         TEXT
);
"""


def _wizard_init(conn: sqlite3.Connection) -> None:
    conn.executescript(WIZARD_DDL)
    conn.commit()


def _wizard_get(conn, step_key: str) -> tuple | None:
    return conn.execute(
        "SELECT status, started_at, finished_at, summary, error FROM wizard_state WHERE step_key=?",
        (step_key,),
    ).fetchone()


def _wizard_set(conn, step_key: str, status: str, *, summary: str | None = None, error: str | None = None) -> None:
    cur = _wizard_get(conn, step_key)
    started = cur[1] if cur else None
    finished = cur[2] if cur else None
    if status == "running" and not started:
        started = now_iso()
    if status in ("done", "failed", "skipped"):
        finished = now_iso()
    if status == "pending":
        started = None
        finished = None
    conn.execute(
        "INSERT OR REPLACE INTO wizard_state(step_key, status, started_at, finished_at, summary, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (step_key, status, started, finished, summary, error),
    )
    conn.commit()


# 각 step 은 (key, title, fn) — fn 은 (conn, args) -> str summary (또는 raise)
# fn 안에서 user 입력이 필요하면 직접 input() 호출

def _wiz_prereq(conn, args) -> str:
    needed = [
        ("DOKUWIKI_SRC", env_default("DOKUWIKI_SRC")),
        ("CONFLUENCE_BASE_URL", env_default("CONFLUENCE_BASE_URL")),
        ("CONFLUENCE_EMAIL", env_default("CONFLUENCE_EMAIL")),
        ("CONFLUENCE_API_TOKEN", env_default("CONFLUENCE_API_TOKEN")),
        ("CONFLUENCE_SPACE_KEY", env_default("CONFLUENCE_SPACE_KEY")),
        ("CONFLUENCE_ROOT_PAGE_ID", env_default("CONFLUENCE_ROOT_PAGE_ID")),
    ]
    missing = [k for k, v in needed if not v]
    for k, v in needed:
        masked = ("***" if "TOKEN" in k else v) if v else "(미설정)"
        print(f"  {k:24s} = {masked}")
    if missing:
        print()
        print("  → .env.example 을 .secrets/confluence.env 로 복사 후 값 채우고")
        print("    `set -a; source .secrets/confluence.env; set +a` 실행 후 재시도")
        raise RuntimeError(f"환경 변수 누락: {', '.join(missing)}")
    # docker / curl / tar 존재 확인 (배포 환경 휴대성)
    has_docker = shutil.which("docker") is not None
    has_curl = shutil.which("curl") is not None
    has_tar = shutil.which("tar") is not None
    print(f"  docker available:   {has_docker}")
    print(f"  curl available:     {has_curl}   (data-only bootstrap 시 필요)")
    print(f"  tar available:      {has_tar}   (data-only bootstrap 시 필요)")
    return f"env vars OK ({len(needed)}개) docker={has_docker} curl={has_curl} tar={has_tar}"


def _wiz_dev_up(conn, args) -> str:
    if not _wizard_ask(args, "DokuWiki 컨테이너를 기동할까요? (이미 떠 있으면 skip 가능)"):
        return "사용자 skip"
    ns = argparse.Namespace(db=args.db, action="up", src=env_default("DOKUWIKI_SRC"), purge=False)
    rc = cmd_dev(ns)
    if rc != 0:
        raise RuntimeError(f"dev up failed: rc={rc}")
    return f"컨테이너 healthy: {DEV_BASE_URL}"


def _wiz_discover(conn, args) -> str:
    ns = argparse.Namespace(db=args.db, src=env_default("DOKUWIKI_SRC"))
    rc = cmd_discover(ns)
    if rc != 0:
        raise RuntimeError(f"discover failed: rc={rc}")
    n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    return f"{n} 페이지 발견"


def _wiz_render(conn, args) -> str:
    base = args.dokuwiki_base or env_default("DOKUWIKI_BASE_URL") or DEV_BASE_URL
    ns = argparse.Namespace(
        db=args.db, base_url=base,
        user=env_default("DOKUWIKI_USER"), password=env_default("DOKUWIKI_PASSWORD"),
        force=False, only=None, delay=0.05,
    )
    rc = cmd_render(ns)
    if rc != 0:
        raise RuntimeError(f"render failed: rc={rc}")
    n = conn.execute("SELECT COUNT(*) FROM pages WHERE raw_xhtml_path IS NOT NULL").fetchone()[0]
    return f"{n} 페이지 XHTML 캐시 완료"


def _wiz_plugin_audit(conn, args) -> str:
    """raw/*.xhtml 을 훑어 ~~MACRO~~ / <... class=\"plugin_*\"> 잔존 확인.
    사용자가 플러그인을 추가 설치하고 re-render 할지 물음."""
    from collections import Counter
    macros: Counter[str] = Counter()
    n_files = 0
    for (raw_path,) in conn.execute(
        "SELECT raw_xhtml_path FROM pages WHERE raw_xhtml_path IS NOT NULL"
    ):
        if not raw_path or not Path(raw_path).is_file():
            continue
        try:
            txt = Path(raw_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        n_files += 1
        for m in _re.findall(r"~~([A-Z][A-Z0-9_:]+)~~", txt):
            macros[m] += 1
    print(f"  {n_files} 개 파일 점검")
    if not macros:
        return "잔존 ~~MACRO~~ 없음"
    print("  잔존 매크로 (상위 10):")
    for name, cnt in macros.most_common(10):
        print(f"    ~~{name}~~ × {cnt}")
    print("\n  플러그인을 추가 설치하려면:")
    print("    1) http://127.0.0.1:18080/doku.php?do=admin&page=extension 접속")
    print("    2) 필요한 플러그인 설치/활성화")
    print("    3) 본 wizard 를 다시 실행해 render 단계 reset")
    if not _wizard_ask(args, "render 결과가 만족스럽나요? (no 면 step render reset 후 종료)"):
        _wizard_set(conn, "render", "pending")
        raise RuntimeError("사용자 요청으로 render 단계 reset — 플러그인 설치 후 다시 wizard 실행")
    return f"잔존 매크로 {sum(macros.values())} (사용자 OK)"


def _wiz_convert(conn, args) -> str:
    ns = argparse.Namespace(db=args.db, force=False, only=None)
    rc = cmd_convert(ns)
    if rc != 0:
        raise RuntimeError(f"convert failed: rc={rc}")
    n = conn.execute("SELECT COUNT(*) FROM pages WHERE storage_path IS NOT NULL").fetchone()[0]
    return f"{n} 페이지 storage XML 생성"


def _wiz_upload(conn, args) -> str:
    ns = argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        space_key=env_default("CONFLUENCE_SPACE_KEY"),
        root_page_id=env_default("CONFLUENCE_ROOT_PAGE_ID"),
        dry_run=False, only=None, include_parents=False, limit=None,
    )
    rc = cmd_upload(ns)
    if rc != 0:
        raise RuntimeError(f"upload failed: rc={rc}")
    p = conn.execute("SELECT COUNT(*) FROM pages WHERE confluence_page_id IS NOT NULL").fetchone()[0]
    a = conn.execute("SELECT COUNT(*) FROM attachments WHERE confluence_attachment_id IS NOT NULL").fetchone()[0]
    return f"{p} 페이지 + {a} 첨부 업로드"


def _wiz_rewrite_links(conn, args) -> str:
    ns = argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        dry_run=False, only=None,
    )
    rc = cmd_rewrite_links(ns)
    if rc != 0:
        raise RuntimeError(f"rewrite-links failed: rc={rc}")
    resolved = conn.execute(
        "SELECT COUNT(*) FROM links WHERE confluence_page_id IS NOT NULL"
    ).fetchone()[0] if _has_table(conn, "links") else 0
    return f"링크 해소 {resolved}"


def _wiz_history(conn, args) -> str:
    if not _wizard_ask(args, "과거 리비전(history) 도 이전할까요? (~30분-수시간)"):
        return "사용자 skip"
    base = env_default("DOKUWIKI_BASE_URL") or DEV_BASE_URL
    log("→ history-discover")
    rc = cmd_history_discover(argparse.Namespace(db=args.db))
    if rc != 0:
        raise RuntimeError(f"history-discover failed")
    log("→ history-render")
    rc = cmd_history_render(argparse.Namespace(
        db=args.db, base_url=base,
        user=env_default("DOKUWIKI_USER"), password=env_default("DOKUWIKI_PASSWORD"),
        only=None, limit=None, delay=0.05, force=False,
    ))
    if rc != 0:
        raise RuntimeError(f"history-render failed")
    log("→ history-convert")
    rc = cmd_history_convert(argparse.Namespace(db=args.db, force=False, only=None))
    if rc != 0:
        raise RuntimeError(f"history-convert failed")
    log("→ history-upload")
    rc = cmd_history_upload(argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        only=None, limit=None, users_map=None,
    ))
    if rc != 0:
        raise RuntimeError(f"history-upload failed")
    n = conn.execute("SELECT COUNT(*) FROM revisions WHERE status='UPLOADED'").fetchone()[0]
    return f"{n} 리비전 업로드"


def _wiz_struct(conn, args) -> str:
    has_struct = False
    sd = _struct_db_path_from_meta(conn)
    has_struct = sd is not None and sd.is_file()
    if not has_struct:
        return "struct 플러그인 데이터 없음 (skip)"
    if not _wizard_ask(args, "struct 플러그인 데이터를 이전할까요?"):
        return "사용자 skip"
    log("→ struct-discover")
    rc = cmd_struct_discover(argparse.Namespace(db=args.db, struct_db=None))
    if rc != 0:
        raise RuntimeError("struct-discover failed")
    log("→ struct-convert --mode native --reconvert")
    rc = cmd_struct_convert(argparse.Namespace(db=args.db, mode="native", reconvert=True))
    if rc != 0:
        raise RuntimeError("struct-convert failed")
    log("→ struct-upload --mode native")
    rc = cmd_struct_upload(argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        space_key=env_default("CONFLUENCE_SPACE_KEY"),
        root_page_id=env_default("CONFLUENCE_ROOT_PAGE_ID"),
        mode="native", fallback="auto", probe=False, probe_keep=False,
        limit=None, no_native_shell=False, only_tbl=None, row_limit=None, index_only=False,
    ))
    if rc != 0:
        raise RuntimeError("struct-upload failed")
    log("→ struct-embed-on-bound-pages")
    rc = cmd_struct_embed_on_bound_pages(argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        only_doku=None,
    ))
    if rc != 0:
        raise RuntimeError("struct-embed failed")
    rows = conn.execute("SELECT COUNT(*) FROM struct_rows WHERE status='UPLOADED'").fetchone()[0]
    return f"{rows} struct row 업로드"


def _wiz_audit(conn, args) -> str:
    sample = args.audit_sample or 50
    ns = argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        only=None, sample=sample, full=False, failed_only=False,
        body_format="storage", verbose=False, output_json=None, output_html=None,
    )
    rc = cmd_audit(ns)
    return f"sample={sample}, rc={rc} — 상세 stdout"


def _wiz_verify(conn, args) -> str:
    out = Path("verify-gallery.html")
    sample = args.verify_sample or 100
    ns = argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        sample=sample, strategy="auto", output=str(out),
        reviewer=env_default("CONFLUENCE_EMAIL"),
        with_confluence_view=True, with_attachment_check=True,
        with_screenshots=False, with_vision=False,
        body_format="storage", dokuwiki_base_url=None,
    )
    rc = cmd_verify_build(ns)
    if rc != 0:
        raise RuntimeError(f"verify build failed: rc={rc}")
    print(f"  HTML 큐 → {out.resolve()}")
    print("  브라우저에서 카드를 OK/NG/DEFER 분류 후 'JSON 다운로드' → ")
    print("  python run.py verify import <파일>")
    if not _wizard_ask(args, "검수 완료했나요? (no 면 step 그대로 둠)"):
        raise RuntimeError("검수 미완료 — 본 단계 그대로 두고 종료")
    return f"verify queue {sample} 빌드 + 사용자 검수 완료"


def _wiz_report(conn, args) -> str:
    ns = argparse.Namespace(db=args.db, limit=20)
    rc = cmd_report(ns)
    return "report 출력됨 (stdout)"


def _wiz_report_publish(conn, args) -> str:
    """state.db 통계 기반 마이그레이션 결과 페이지를 Confluence 에 생성/갱신."""
    if not env_default("CONFLUENCE_EMAIL") or not env_default("CONFLUENCE_API_TOKEN"):
        raise RuntimeError("자격증명 누락")
    title = args.report_title or "DokuWiki → Confluence 마이그레이션 결과 보고서"
    body = _wizard_build_report_body(conn)
    ns = argparse.Namespace(
        db=args.db,
        base_url=env_default("CONFLUENCE_BASE_URL"),
        email=env_default("CONFLUENCE_EMAIL"),
        api_token=env_default("CONFLUENCE_API_TOKEN"),
        space_key=env_default("CONFLUENCE_SPACE_KEY"),
        root_page_id=env_default("CONFLUENCE_ROOT_PAGE_ID"),
    )
    session = _confluence_session(ns)
    if session is None:
        raise RuntimeError("Confluence session 실패")
    base = ns.base_url.rstrip("/")
    space_id = _resolve_space_id(session, base, ns.space_key)
    if not space_id:
        raise RuntimeError("space_id 해소 실패")
    # 기존 페이지 확인 (meta 에 report_page_id 저장)
    existing = db_get_meta(conn, "wizard_report_page_id")
    if existing:
        ok = _struct_put_page(session, base, existing, title=title, storage=body)
        if not ok:
            raise RuntimeError("report PUT 실패")
        return f"보고서 갱신 → page {existing}"
    pid = _struct_post_page(
        session, base, space_id, ns.root_page_id,
        title=title, storage=body, sid=0,
    )
    if not pid:
        raise RuntimeError("report POST 실패")
    db_set_meta(conn, "wizard_report_page_id", str(pid))
    return f"보고서 신규 발행 → page {pid}"


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _wizard_build_report_body(conn) -> str:
    """state.db 의 핵심 카운트를 모아 Confluence storage XML 본문 생성."""
    def q1(sql: str, default: int = 0) -> int:
        try:
            r = conn.execute(sql).fetchone()
            return r[0] if r and r[0] is not None else default
        except sqlite3.OperationalError:
            return default
    pages_total = q1("SELECT COUNT(*) FROM pages")
    pages_uploaded = q1("SELECT COUNT(*) FROM pages WHERE confluence_page_id IS NOT NULL")
    atts_total = q1("SELECT COUNT(*) FROM attachments")
    atts_uploaded = q1("SELECT COUNT(*) FROM attachments WHERE confluence_attachment_id IS NOT NULL")
    rev_total = q1("SELECT COUNT(*) FROM revisions") if _has_table(conn, "revisions") else 0
    rev_uploaded = q1("SELECT COUNT(*) FROM revisions WHERE status='UPLOADED'") if _has_table(conn, "revisions") else 0
    struct_schemas = q1("SELECT COUNT(*) FROM struct_schemas WHERE status='UPLOADED'") if _has_table(conn, "struct_schemas") else 0
    struct_rows = q1("SELECT COUNT(*) FROM struct_rows WHERE status='UPLOADED'") if _has_table(conn, "struct_rows") else 0
    verify_ok = q1("SELECT COUNT(*) FROM verify_decisions WHERE decision='OK'") if _has_table(conn, "verify_decisions") else 0
    verify_ng = q1("SELECT COUNT(*) FROM verify_decisions WHERE decision='NG'") if _has_table(conn, "verify_decisions") else 0
    verify_defer = q1("SELECT COUNT(*) FROM verify_decisions WHERE decision='DEFER'") if _has_table(conn, "verify_decisions") else 0

    # wizard step 상태
    step_rows = conn.execute(
        "SELECT step_key, status, summary, started_at, finished_at FROM wizard_state ORDER BY rowid"
    ).fetchall() if _has_table(conn, "wizard_state") else []

    rows_html = []
    for sk, st, sm, sa, fa in step_rows:
        rows_html.append(
            f"<tr><td>{_h.escape(sk)}</td><td>{_h.escape(st)}</td>"
            f"<td>{_h.escape(sm or '')}</td>"
            f"<td>{_h.escape((sa or '')[:19])}</td><td>{_h.escape((fa or '')[:19])}</td></tr>"
        )

    return (
        f'<h1>DokuWiki → Confluence 마이그레이션 결과 보고서</h1>'
        f'<p>생성 시각: <time datetime="{now_iso()}">{now_iso()}</time></p>'
        '<h2>1. 메인 콘텐츠</h2>'
        f'<table><tbody>'
        f'<tr><th>페이지 (uploaded / total)</th><td>{pages_uploaded} / {pages_total} '
        f'({(100*pages_uploaded//max(pages_total,1))}%)</td></tr>'
        f'<tr><th>첨부 (uploaded / total)</th><td>{atts_uploaded} / {atts_total} '
        f'({(100*atts_uploaded//max(atts_total,1))}%)</td></tr>'
        '</tbody></table>'
        '<h2>2. 과거 리비전 (history)</h2>'
        f'<table><tbody>'
        f'<tr><th>리비전 (uploaded / total)</th><td>{rev_uploaded} / {rev_total} '
        f'({(100*rev_uploaded//max(rev_total,1))}%)</td></tr>'
        '</tbody></table>'
        '<h2>3. struct 데이터</h2>'
        f'<table><tbody>'
        f'<tr><th>schema 업로드</th><td>{struct_schemas}</td></tr>'
        f'<tr><th>row 업로드</th><td>{struct_rows}</td></tr>'
        '</tbody></table>'
        '<h2>4. 시각 검수 (verify)</h2>'
        f'<table><tbody>'
        f'<tr><th>OK</th><td>{verify_ok}</td></tr>'
        f'<tr><th>NG</th><td>{verify_ng}</td></tr>'
        f'<tr><th>DEFER</th><td>{verify_defer}</td></tr>'
        '</tbody></table>'
        '<h2>5. wizard 단계 진행</h2>'
        '<table><thead><tr><th>step</th><th>status</th><th>요약</th>'
        '<th>시작</th><th>종료</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody></table>'
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        '<p>본 페이지는 <code>python run.py wizard</code> 또는 '
        '<code>python run.py report-publish</code> 가 자동 갱신합니다.</p>'
        '</ac:rich-text-body></ac:structured-macro>'
    )


# 단계 정의: (key, title, fn, optional)
WIZARD_STEPS: list[tuple[str, str, "callable", bool]] = [
    ("prereq",         "사전 점검 (자격증명/경로/CLI)",                     _wiz_prereq, False),
    ("dev-up",         "DokuWiki 컨테이너 기동 (기존 데이터 복제)",         _wiz_dev_up, True),
    ("discover",       "페이지 인벤토리 (state.db)",                         _wiz_discover, False),
    ("render",         "DokuWiki XHTML 렌더 캐시",                          _wiz_render, False),
    ("plugin-audit",   "잔존 매크로 점검 + 플러그인 설치 권장",             _wiz_plugin_audit, False),
    ("convert",        "XHTML → Confluence storage 변환",                   _wiz_convert, False),
    ("upload",         "페이지 + 첨부 업로드",                              _wiz_upload, False),
    ("rewrite-links",  "내부 링크 2-pass 해소",                             _wiz_rewrite_links, False),
    ("history",        "과거 리비전 이전 (옵션)",                           _wiz_history, True),
    ("struct",         "struct 플러그인 데이터 이전 (옵션)",                _wiz_struct, True),
    ("audit",          "Confluence 측 본문 검증 (sample)",                  _wiz_audit, False),
    ("verify",         "사용자 시각 검수 큐 빌드 + 사람 검수",              _wiz_verify, False),
    ("report",         "결과 리포트 생성 (stdout)",                         _wiz_report, False),
    ("report-publish", "결과 보고서를 Confluence 페이지로 발행",            _wiz_report_publish, False),
]


def _wizard_ask(args, question: str) -> bool:
    if args.yes:
        print(f"  ? {question}  → [auto-yes]")
        return True
    try:
        ans = input(f"  ? {question} [Y/n]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes", "예")


def _wizard_print_status(conn) -> None:
    print(f"{'step':18} {'status':12} {'요약':32} 시작")
    print("-" * 80)
    for key, title, _fn, opt in WIZARD_STEPS:
        row = _wizard_get(conn, key)
        st = row[0] if row else "pending"
        sm = (row[3] or "")[:30] if row else ""
        sa = (row[1] or "")[:19] if row else ""
        mark = "✓" if st == "done" else ("⊘" if st == "skipped" else ("✗" if st == "failed" else "·"))
        print(f"{mark} {key:16} {st:12} {sm:32} {sa}")


def cmd_wizard(args: argparse.Namespace) -> int:
    """대화형 step-by-step wizard. 중단/재개 가능. Ctrl+C 로 안전 종료."""
    conn = db_connect(args.db)
    db_init(conn)
    _wizard_init(conn)

    if args.restart:
        conn.execute("DELETE FROM wizard_state")
        conn.commit()
        log("처음부터 다시 시작 (모든 step state reset)")

    if args.status:
        _wizard_print_status(conn)
        conn.close()
        return 0

    # --from-step X 의미: X 부터 끝까지만 실행. 이전 단계는 그대로 둠 (건드리지 않음).
    start_idx = 0
    if args.from_step:
        keys = [k for k, *_ in WIZARD_STEPS]
        if args.from_step not in keys:
            log(f"존재하지 않는 step: {args.from_step}. 가능: {', '.join(keys)}")
            return 2
        start_idx = keys.index(args.from_step)
        # 해당 step 부터의 상태만 pending 으로 (재실행 가능하게)
        for key in keys[start_idx:]:
            cur = _wizard_get(conn, key)
            if cur and cur[0] in ("done", "skipped", "failed", "interrupted"):
                _wizard_set(conn, key, "pending")

    print("== DokuWiki → Confluence 마이그레이션 wizard ==")
    print()
    _wizard_print_status(conn)
    print()

    for idx, (key, title, fn, optional) in enumerate(WIZARD_STEPS, 1):
        if idx - 1 < start_idx:
            continue
        row = _wizard_get(conn, key)
        st = row[0] if row else "pending"

        if st in ("done", "skipped"):
            print(f"[{idx}/{len(WIZARD_STEPS)}] {title} — {st} (skip)")
            continue

        print()
        print(f"[{idx}/{len(WIZARD_STEPS)}] {title}")
        print(f"  step: {key} | 현재: {st}")
        if optional:
            print("  (선택) 이 단계는 건너뛸 수 있습니다.")

        if not args.yes:
            choice = input("  진행/skip/quit ? [Enter/s/q] ").strip().lower()
            if choice in ("q", "quit"):
                print("종료. 다음 실행 시 이 단계부터 이어집니다.")
                conn.close()
                return 0
            if choice in ("s", "skip"):
                _wizard_set(conn, key, "skipped", summary="사용자 skip")
                continue
            if choice in ("d", "done"):
                _wizard_set(conn, key, "done", summary="사용자 수동 done")
                continue

        _wizard_set(conn, key, "running")
        try:
            summary = fn(conn, args)
            _wizard_set(conn, key, "done", summary=summary)
            print(f"  ✓ {summary}")
        except KeyboardInterrupt:
            _wizard_set(conn, key, "interrupted", error="Ctrl+C")
            print("\n중단됨. 다음 실행 시 이 단계부터 이어집니다.")
            conn.close()
            return 130
        except Exception as e:
            _wizard_set(conn, key, "failed", error=str(e))
            print(f"  ✗ 실패: {e}")
            # 실패 시 기본 halt. --continue-on-error 일 때만 다음 단계로.
            if not args.continue_on_error:
                print("종료. 원인 해결 후 다시 실행하면 이 단계부터 이어집니다.")
                print("  팁: `python run.py wizard --from-step", key, "` 로 특정 단계만 재시도")
                conn.close()
                return 1

    print()
    print("== 모든 단계 완료 ==")
    _wizard_print_status(conn)
    conn.close()
    return 0


# § compare-publish (DokuWiki/Confluence 양측 스크린샷 + 비교 갤러리 발행)

# 비교 갤러리 영구 제외 목록 — 본문이 의도적으로 빈 페이지.
# 양측 모두 텅 빈 박스라 비교 가치가 없음. 사용자가 `--select` 로 명시하면 우회.
# (예: `start` 는 2024-05-30 이후 본문이 `~~NOTOC~~` 한 줄로 비워진 상태 —
#  woojinkim.org 가 dokuwiki → GitHub Pages 로 이주하면서 정리된 잔해.)
_COMPARE_PERMANENT_EXCLUDE: set[str] = {"start", "sidebar"}


def _compare_select_candidates(
    conn: sqlite3.Connection,
    *,
    sample: int = 8,
    explicit_ids: list[str] | None = None,
    exclude_ids: set[str] | None = None,
) -> list[tuple[str, str, str, str, int]]:
    """비교 갤러리 후보 페이지 선정. 카테고리별 대표 1 페이지씩 + 명시 list 지원.

    각 카테고리당 1 페이지 — 메인 / iframe / encrypt / 표 / 이미지·첨부 /
    info·note·warning / 매크로 다양 / 코드 매크로 / 최대 본문. 동일 페이지
    중복 선정 방지. 거대 본문 (>200KB) 은 톡톡 너무 길어 매크로 다양/표 등에선
    제외 (최대 본문 카테고리는 통과).

    `exclude_ids` 지정 시 그 doku_id 들을 모든 카테고리에서 제외 — 로테이션용
    (이미 비교 갤러리에 발행된 페이지 회피).

    Returns: list of (reason, doku_id, title, confluence_page_id, body_size).
    """
    from collections import Counter

    rows = conn.execute(
        "SELECT doku_id, title, storage_path, confluence_page_id "
        "FROM pages WHERE confluence_page_id IS NOT NULL AND storage_path IS NOT NULL"
    ).fetchall()
    scored: list[tuple[str, str, str, str, int, "Counter[str]"]] = []
    for d, t, sp, cid in rows:
        try:
            body = Path(sp).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        macs: Counter[str] = Counter()
        for m in re.findall(r'ac:name="([^"]+)"', body):
            macs[m] += 1
        macs["_table"] = body.count("<table")
        macs["_image"] = body.count("<ac:image")
        macs["_attach"] = body.count("<ri:attachment")
        macs["_distinct"] = len([k for k in macs if not k.startswith("_")])
        scored.append((d, t, sp, cid, len(body), macs))

    if explicit_ids:
        idx = {s[0]: s for s in scored}
        chosen: list[tuple[str, str, str, str, int]] = []
        for did in explicit_ids:
            s = idx.get(did)
            if s:
                chosen.append(("명시 (--select)", s[0], s[1], s[3], s[4]))
            else:
                log(f"  [WARN] 명시 페이지 미발견 또는 미업로드: {did}")
        return chosen

    # 로테이션 — 이미 발행된 doku_id 는 후보에서 제외.
    # macOS APFS 의 doku_id 가 NFD 로 저장된 경우 (한국어 등) NFC seed 와
    # byte mismatch — 양측 NFC 정규화 후 비교.
    import unicodedata as _ud
    excluded_nfc = {_ud.normalize("NFC", x) for x in (exclude_ids or set())}
    excluded_nfc |= _COMPARE_PERMANENT_EXCLUDE
    def _is_excluded(doku_id: str) -> bool:
        return _ud.normalize("NFC", doku_id) in excluded_nfc
    seen: set[str] = set()  # 같은 batch 안의 중복 방지
    chosen2: list[tuple[str, str, str, str, int]] = []

    def pick(reason: str, key_fn, filt=None, count: int = 1) -> None:
        cands = [s for s in scored
                 if s[0] not in seen and not _is_excluded(s[0])
                 and (filt is None or filt(s))]
        cands.sort(key=key_fn, reverse=True)
        for s in cands[:count]:
            seen.add(s[0])
            chosen2.append((reason, s[0], s[1], s[3], s[4]))

    # 중간 크기 (5KB ~ 200KB) — 한 화면에 톡톡 보기 좋음
    def medium(s):
        return 5_000 < s[4] < 200_000

    # 카테고리당 기본 1 페이지 — sample 이 크면 (sample/8 만큼) 각 카테고리에서
    # 추가 후보. e.g. sample=20 이면 카테고리당 2~3개. 메인/사용자 시작은 한 페이지
    # 만 의미 있으므로 항상 1.
    per_cat = max(1, sample // 8)

    # iframe/encrypt 같은 *특수 매크로* 카테고리는 큰 페이지에 묻혀 있을 수 있어
    # 작은 페이지를 우선 (-size 키) — 풀 페이지 스크린샷 timeout 회피.
    pick("메인 (start)",            lambda s: 1,
         filt=lambda s: s[0] == "start")
    pick("사용자 시작",              lambda s: 1,
         filt=lambda s: s[0].endswith(":start") and s[0] != "start" and s[4] > 200,
         count=per_cat)
    pick("iframe (캘린더/임베드)",  lambda s: -s[4],
         filt=lambda s: s[5].get("iframe", 0) > 0, count=per_cat)
    pick("encrypted-passwords",     lambda s: -s[4],
         filt=lambda s: s[5].get("expand", 0) > 0 and medium(s), count=per_cat)
    pick("표 풍부",                  lambda s: s[5]["_table"],
         filt=lambda s: s[5]["_table"] >= 3 and medium(s), count=per_cat)
    pick("이미지·첨부 다수",         lambda s: s[5]["_image"] + s[5]["_attach"],
         filt=lambda s: (s[5]["_image"] + s[5]["_attach"]) >= 2 and medium(s),
         count=per_cat)
    pick("info/note/warning 매크로", lambda s: s[5].get("info", 0) + s[5].get("note", 0) + s[5].get("warning", 0),
         filt=lambda s: (s[5].get("info", 0) + s[5].get("note", 0) + s[5].get("warning", 0)) > 0 and medium(s),
         count=per_cat)
    pick("매크로 다양",              lambda s: s[5]["_distinct"],
         filt=lambda s: s[5]["_distinct"] >= 5 and medium(s), count=per_cat)
    pick("코드 매크로 풍부",         lambda s: s[5].get("code", 0),
         filt=lambda s: s[5].get("code", 0) >= 3 and medium(s), count=per_cat)
    # 대용량 본문 fallback — sample 만큼 fill (마지막 카테고리)
    pick("대용량 본문",              lambda s: s[4], count=max(1, sample))

    return chosen2[:sample]


def _compare_rewrite_attachment_urls(
    view_html: str,
    page_id: str,
    confluence_origin: str,
    email: str,
    token: str,
) -> str:
    """view body 안 `/wiki/download/{attachments|thumbnails}/{pid}/{filename}?...`
    URL 을 v1 download endpoint 로 교체. v1 은 Basic Auth → 302 → media binary
    로 정상 작동. v2 download URL (OAuth 만) 회피.

    이미지 다수 페이지 (수십~수백 개) 의 핵심 — Confluence view body 가
    *thumbnails* URL 을 src 로, *attachments* URL 을 data-image-src 로 두는데
    img 의 *src* 가 thumbnails 라서 그것도 rewrite 필요.

    매치 영역:
    - `src=".../download/attachments/..."` (full size)
    - `src=".../download/thumbnails/..."` (썸네일, 200×150 등)
    - `srcset=".../download/thumbnails/..."` (responsive variants)
    - `data-image-src=".../download/attachments/..."` (lightbox full size)

    페이지의 첨부 list 를 한 번 GET 해 filename → attachment_id 매핑.
    매핑 실패 src 는 원본 그대로.
    """
    import urllib.parse
    import requests as _rq

    try:
        r = _rq.get(
            f"{confluence_origin}/api/v2/pages/{page_id}/attachments",
            auth=(email, token), params={"limit": 250}, timeout=30,
        )
        if not r.ok:
            return view_html
        filename_to_aid = {a.get("title"): a.get("id") for a in r.json().get("results", [])}
    except Exception:  # noqa: BLE001
        return view_html
    if not filename_to_aid:
        return view_html

    def rewrite_url(url: str) -> str | None:
        """`download/(attachments|thumbnails)/{pid}/{filename}?...` → v1 endpoint."""
        m = re.search(r"/download/(?:attachments|thumbnails)/\d+/([^?]+)", url)
        if not m:
            return None
        filename = urllib.parse.unquote(m.group(1))
        aid = filename_to_aid.get(filename)
        if not aid:
            return None
        return f"{confluence_origin}/rest/api/content/{page_id}/child/attachment/{aid}/download"

    # img src + data-image-src + srcset 모두 매치
    pattern = re.compile(
        r'((?:src|data-image-src)=")([^"]+/download/(?:attachments|thumbnails)/\d+/[^"]+)"',
        re.S,
    )

    def repl(m: re.Match) -> str:
        new = rewrite_url(m.group(2))
        if new is None:
            return m.group(0)
        return m.group(1) + new + '"'

    out = pattern.sub(repl, view_html)

    # srcset 은 *여러 URL* — 각각 rewrite (`URL 1x, URL 2x, ...` 형식)
    def srcset_repl(m: re.Match) -> str:
        srcset = m.group(2)
        parts = [p.strip() for p in srcset.split(",")]
        new_parts = []
        for p in parts:
            tokens = p.split(None, 1)
            if not tokens:
                continue
            url = tokens[0]
            desc = tokens[1] if len(tokens) > 1 else ""
            new = rewrite_url(url)
            if new:
                new_parts.append(f"{new} {desc}".strip())
            else:
                new_parts.append(p)
        return m.group(1) + ", ".join(new_parts) + '"'

    srcset_pattern = re.compile(r'(srcset=")([^"]+)"', re.S)
    out = srcset_pattern.sub(srcset_repl, out)

    return out


def _compare_view_body_limitation_note(view_html: str) -> str:
    """Confluence view body 가 *placeholder 만* 보여주는 매크로 (iframe / video
    embed 등) 만 들어있는 페이지면 안내 텍스트 반환. 비교 갤러리에서 빈
    이미지로 오해 방지."""
    import re as _re_local
    text_only = _re_local.sub(r"<[^>]+>", "", view_html).strip()
    if len(text_only) > 50:
        return ""
    if "conf-macro" not in view_html and "structured-macro" not in view_html:
        return ""
    # macro placeholder 만 있고 텍스트 거의 없음
    note = (
        '<div style="background:#fff3cd;border:1px solid #ffc107;'
        'padding:12px;margin:0 0 16px 0;border-radius:4px;color:#664d03;">'
        '<strong>안내:</strong> 본 페이지는 iframe / 임베드 매크로 위주로 구성됨. '
        'Confluence view body API 가 이 매크로를 빈 placeholder 박스로만 응답하는 '
        '한계 — 실제 Confluence 페이지에서는 정상 렌더링됨.'
        '</div>'
    )
    return note


def _compare_clip_oversize(png_path: Path) -> None:
    """캡쳐 PNG 의 (1) 흰색 빈 영역 trim + (2) CAPTURE_MAX_HEIGHT_PX 초과 시 clip.

    full_page=True 는 *scrollable page 전체* 캡쳐하지만 콘텐츠가 viewport
    보다 작으면 *viewport 크기* 로 캡쳐돼 아래쪽에 큰 빈 영역. PIL ImageChops
    로 흰 배경 (255,255,255) 과 다른 영역의 bbox 만 남김.

    그 후 height 가 12000px 초과면 위쪽만 남기는 추가 clip — 거대 페이지
    (이미지 100+) 가 첨부 100MB 한도 초과 회피.
    """
    try:
        from PIL import Image, ImageChops  # type: ignore
    except ImportError:
        return
    try:
        # 큰 파일 (>5MB) 은 PIL ImageChops.difference 가 메모리·시간 폭증
        # — trim skip (그대로 사용). 사용자 발견: u:oh:모든_기록 (13MB+) 가
        # hang 원인. 5MB 면 일반 페이지엔 영향 없음, 거대 페이지만 보호.
        if png_path.stat().st_size > 5 * 1024 * 1024:
            return
        img = Image.open(png_path).convert("RGB")
        # (1) 흰 배경 trim — body 콘텐츠 영역만
        bg = Image.new("RGB", img.size, (255, 255, 255))
        diff = ImageChops.difference(img, bg)
        bbox = diff.getbbox()
        if bbox:
            # 아래/오른쪽에 약간의 padding 보존 (시각적 여유)
            x0, y0, x1, y1 = bbox
            x0 = max(0, x0 - 8)
            y0 = max(0, y0 - 8)
            x1 = min(img.width, x1 + 8)
            y1 = min(img.height, y1 + 8)
            img = img.crop((x0, y0, x1, y1))
        # (2) height clip (거대 페이지)
        if img.height > CAPTURE_MAX_HEIGHT_PX:
            img = img.crop((0, 0, img.width, CAPTURE_MAX_HEIGHT_PX))
        img.save(png_path)
    except Exception:  # noqa: BLE001
        pass


def _compare_capture_screenshots(
    candidates: list[tuple[str, str, str, str, int]],
    out_dir: Path,
    *,
    dokuwiki_base: str,
    confluence_base: str,
    confluence_email: str,
    confluence_token: str,
    skip_existing: bool = False,
) -> dict[str, dict[str, Path | None]]:
    """양측 풀-페이지 1280px 폭 스크린샷. Playwright 미설치 시 빈 dict.

    Confluence 측은 v2 body-format=view 로 본문 HTML 받아 set_content 로 렌더
    — Cloud UI 인증 (SSO/cookie) 우회. DokuWiki 측은 실서버 (`?id=...`)."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log("Playwright 미설치 — `pip install playwright && "
            "playwright install chromium` 후 재실행. 스크린샷 건너뜀.")
        return {}
    import requests

    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Path | None]] = {}

    auth = ""
    if confluence_email and confluence_token:
        token = base64.b64encode(f"{confluence_email}:{confluence_token}".encode()).decode()
        auth = f"Basic {token}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx_d = browser.new_context(viewport={"width": CAPTURE_VIEWPORT_W, "height": CAPTURE_VIEWPORT_H})
        page_d = ctx_d.new_page()
        # Confluence 첨부 이미지 (`/wiki/download/attachments/...`) 는 인증 필요.
        # ctx 의 extra_http_headers 에 Authorization 추가 → set_content 안 <img>
        # 가 절대 URL 로 fetch 될 때도 인증 통과.
        ctx_c = browser.new_context(
            viewport={"width": CAPTURE_VIEWPORT_W, "height": CAPTURE_VIEWPORT_H},
            extra_http_headers={"Authorization": auth} if auth else {},
        )
        page_c = ctx_c.new_page()

        confluence_origin = confluence_base.rstrip("/")
        # `<base href>` 가 있어야 view body 안 상대 URL 도 정상 resolve.

        for i, (_reason, doku_id, _title, cid, _sz) in enumerate(candidates, 1):
            stem = doku_id.replace(":", "_").replace("/", "_")
            dwk_path = out_dir / f"{stem}.dwk.png"
            cnf_path = out_dir / f"{stem}.cnf.png"

            if skip_existing and dwk_path.is_file() and cnf_path.is_file():
                log(f"  [{i}/{len(candidates)}] {doku_id}: 기존 PNG 재사용")
                results[doku_id] = {"dwk": dwk_path, "cnf": cnf_path}
                continue

            log(f"  [{i}/{len(candidates)}] {doku_id}: 양측 캡쳐 중...")

            # DokuWiki
            try:
                url_d = f"{dokuwiki_base.rstrip('/')}/doku.php?id={doku_id}"
                page_d.goto(url_d, wait_until="networkidle", timeout=30_000)
                # viewport clip + full_page=False — 큰 페이지 (수만 px) 의 OOM
                # 방지. 작은 페이지의 빈 영역은 _compare_clip_oversize 가 PIL
                # trim 으로 보완.
                h = page_d.evaluate(
                    "() => Math.max(document.body.scrollHeight, "
                    "document.documentElement.scrollHeight)"
                )
                cap_h = min(int(h), CAPTURE_MAX_HEIGHT_PX)
                page_d.set_viewport_size({"width": CAPTURE_VIEWPORT_W, "height": cap_h})
                page_d.screenshot(path=str(dwk_path), full_page=False)
                _compare_clip_oversize(dwk_path)
            except Exception as e:  # noqa: BLE001
                log(f"    DokuWiki 캡쳐 실패: {e}")
                dwk_path = None  # type: ignore

            # Confluence — body-format=view 받아 set_content 렌더 (인증 우회)
            try:
                r = requests.get(
                    f"{confluence_origin}/api/v2/pages/{cid}",
                    auth=(confluence_email, confluence_token),
                    params={"body-format": "view"},
                    timeout=60,
                )
                if r.ok:
                    view_html = r.json().get("body", {}).get("view", {}).get("value", "")
                    # `<img src=".../wiki/download/attachments/{pid}/{filename}?...">` 는
                    # OAuth 만 받는 endpoint → Basic Auth 실패. v1 endpoint
                    # (`/rest/api/content/{pid}/child/attachment/{aid}/download`) 는
                    # Basic Auth → 302 → media binary. 모든 img src 를 rewrite.
                    view_html = _compare_rewrite_attachment_urls(
                        view_html, cid, confluence_origin,
                        confluence_email, confluence_token,
                    )
                    # Confluence view body 는 iframe / 일부 매크로를 빈 placeholder
                    # 박스로만 렌더. 비교 갤러리에서 빈 이미지로 오해되지 않도록
                    # *안내 텍스트* injection — 실제 페이지에선 정상 작동.
                    placeholder_note = _compare_view_body_limitation_note(view_html)
                    if placeholder_note:
                        view_html = placeholder_note + view_html
                    html = (
                        '<!doctype html><html><head><meta charset="utf-8">'
                        f'<base href="{confluence_origin}/">'
                        '<style>'
                        'body{font-family:-apple-system,Arial,sans-serif;'
                        'max-width:1100px;margin:0 auto;padding:24px;line-height:1.5;color:#222;}'
                        'h1,h2,h3{margin-top:1.2em;}'
                        'img{max-width:100%;height:auto;}'
                        'table{border-collapse:collapse;margin:8px 0;}'
                        'table,th,td{border:1px solid #ccc;padding:6px;}'
                        'th{background:#f4f5f7;}'
                        'pre{background:#f4f5f7;padding:8px;border-radius:4px;overflow:auto;}'
                        'code{background:#f4f5f7;padding:1px 4px;border-radius:3px;}'
                        'blockquote{border-left:3px solid #ccc;padding-left:12px;color:#555;}'
                        '.confluence-information-macro{border:1px solid #ddd;border-radius:4px;'
                        'padding:10px;margin:8px 0;background:#f4f5f7;}'
                        '.conf-macro{padding:6px 0;}'
                        '</style></head><body>' + view_html + '</body></html>'
                    )
                    # 첫 시도: networkidle (45초). timeout 시 domcontentloaded
                    # 로 fallback (외부 리소스 일부 fetch 안 됐어도 캡쳐 진행).
                    try:
                        page_c.set_content(html, wait_until="networkidle", timeout=30_000)
                    except Exception:
                        try:
                            page_c.set_content(html, wait_until="domcontentloaded",
                                              timeout=15_000)
                            log(f"    [WARN] {doku_id}: networkidle timeout — "
                                "domcontentloaded fallback")
                        except Exception as e:  # noqa: BLE001
                            log(f"    [WARN] {doku_id}: set_content 실패: {e}")
                    # 폰트/이미지 마저 layout settle 위한 짧은 대기
                    page_c.wait_for_timeout(1_500)
                    # viewport clip + full_page=False — 큰 페이지 (수만 px) 의
                    # OOM 방지. 작은 페이지의 빈 영역은 _compare_clip_oversize
                    # 가 PIL trim 으로 보완.
                    h = page_c.evaluate(
                        "() => Math.max(document.body.scrollHeight, "
                        "document.documentElement.scrollHeight)"
                    )
                    cap_h = min(int(h), CAPTURE_MAX_HEIGHT_PX)
                    page_c.set_viewport_size({"width": CAPTURE_VIEWPORT_W, "height": cap_h})
                    page_c.screenshot(path=str(cnf_path), full_page=False)
                    _compare_clip_oversize(cnf_path)
                else:
                    log(f"    Confluence GET {r.status_code}: {r.text[:150]}")
                    cnf_path = None  # type: ignore
            except Exception as e:  # noqa: BLE001
                log(f"    Confluence 캡쳐 실패: {e}")
                cnf_path = None  # type: ignore

            results[doku_id] = {
                "dwk": dwk_path if dwk_path and Path(str(dwk_path)).is_file() else None,
                "cnf": cnf_path if cnf_path and Path(str(cnf_path)).is_file() else None,
            }
        browser.close()
    return results


def _compare_build_storage_body(
    candidates: list[tuple[str, str, str, str, int]],
    screenshots: dict[str, dict[str, Path | None]],
    *,
    dokuwiki_base: str,
    confluence_base: str,
    space_key: str,
) -> str:
    """비교 갤러리 Confluence storage XML 빌드. 각 페이지마다 H2 + 양측 링크 +
    2열 표 (DokuWiki | Confluence) — 이미지는 첨부 참조."""
    parts: list[str] = []
    parts.append(
        '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
        '<p><strong>DokuWiki ↔ Confluence 마이그레이션 비교 갤러리</strong></p>'
        '<ul>'
        f'<li>생성: {now_iso()[:10]}</li>'
        f'<li>대상 페이지 수: {len(candidates)}</li>'
        '<li>좌측 = DokuWiki 원본 / 우측 = Confluence 마이그레이션 결과</li>'
        '<li>이미지는 1280px 폭 풀-페이지 스크린샷 (캡쳐 시점 기준)</li>'
        '<li>이 페이지는 <code>python run.py compare-publish</code> 로 갱신됩니다</li>'
        '</ul>'
        '</ac:rich-text-body></ac:structured-macro>'
    )
    parts.append('<p><ac:structured-macro ac:name="toc"/></p>')

    for i, (reason, doku_id, title, cid, size) in enumerate(candidates, 1):
        s = screenshots.get(doku_id, {})
        dwk = s.get("dwk")
        cnf = s.get("cnf")
        dwk_name = dwk.name if dwk else None
        cnf_name = cnf.name if cnf else None

        parts.append(f'<h2>{i}. {_h.escape(title or doku_id)}</h2>')
        parts.append(
            '<p>'
            f'<strong>분류:</strong> {_h.escape(reason)} · '
            f'<strong>doku_id:</strong> <code>{_h.escape(doku_id)}</code> · '
            f'<strong>본문 크기:</strong> {size:,} bytes'
            '</p>'
        )

        dwk_url = f"{dokuwiki_base.rstrip('/')}/doku.php?id={doku_id}"
        cnf_url = f"{confluence_base.rstrip('/')}/spaces/{space_key}/pages/{cid}"
        parts.append(
            '<p>'
            f'<a href="{_h.escape(dwk_url)}">↗ DokuWiki 원본</a>  ·  '
            f'<a href="{_h.escape(cnf_url)}">↗ Confluence 페이지</a>'
            '</p>'
        )

        td_dwk = (
            f'<ac:image ac:width="540"><ri:attachment ri:filename="{_h.escape(dwk_name)}"/></ac:image>'
            if dwk_name else '<p><em>(DokuWiki 캡쳐 실패)</em></p>'
        )
        td_cnf = (
            f'<ac:image ac:width="540"><ri:attachment ri:filename="{_h.escape(cnf_name)}"/></ac:image>'
            if cnf_name else '<p><em>(Confluence 캡쳐 실패)</em></p>'
        )
        parts.append(
            '<table><colgroup><col style="width: 50.0%;"/><col style="width: 50.0%;"/></colgroup>'
            '<tbody>'
            '<tr><th>DokuWiki</th><th>Confluence</th></tr>'
            f'<tr><td>{td_dwk}</td><td>{td_cnf}</td></tr>'
            '</tbody></table>'
        )
    return "\n".join(parts)


def _compare_find_or_create_page(
    session, base: str, space_id: str, root_page_id: str, title: str
) -> str | None:
    """제목으로 페이지 검색 → 있으면 그 id, 없으면 placeholder POST 후 id 반환."""
    sr = _request_with_retry(
        session, "GET", f"{base}/api/v2/pages",
        params={"space-id": space_id, "title": title, "limit": 1},
    )
    if sr is not None and sr.status_code < 400:
        items = sr.json().get("results", [])
        if items:
            return items[0].get("id")
    placeholder = {
        "spaceId": space_id, "parentId": root_page_id, "title": title,
        "body": {"representation": "storage", "value": '<p>(준비 중)</p>'},
    }
    r = _request_with_retry(session, "POST", f"{base}/api/v2/pages", json=placeholder)
    if r is None or r.status_code >= 400:
        log(f"  [FAIL] 페이지 생성: {r.status_code if r else 'no resp'} body={(r.text if r else '')[:200]}")
        return None
    return r.json().get("id")


def _compare_attach_screenshots(
    session, base: str, page_id: str,
    screenshots: dict[str, dict[str, Path | None]],
) -> tuple[int, int]:
    """모든 PNG 를 v1 multipart 로 page_id 에 첨부.

    같은 filename 이 이미 있으면 *새 버전* POST `/child/attachment/{aid}/data`
    — 이전 코드의 버그 (POST `/child/attachment` 에 같은 filename 시 400
    'same file name as an existing attachment' 응답을 ok 마킹만 하고 *실제
    새 버전 갱신 안 함* → 첫 빈 캡쳐가 그대로 남아있음) fix.

    Returns (ok, fail)."""
    from requests_toolbelt.multipart import encoder as tb_encoder

    def _post_multipart(url: str, p: Path) -> "requests.Response":
        with open(p, "rb") as fp:
            m = tb_encoder.MultipartEncoder(
                fields={"file": (p.name, fp, "image/png"), "minorEdit": "true"}
            )
            return session.post(
                url,
                headers={"X-Atlassian-Token": "no-check", "Content-Type": m.content_type},
                data=m,
                timeout=120,
            )

    def _existing_attachment_id(filename: str) -> str | None:
        """page 의 same filename 첨부 ID 조회 — 새 버전 PUT 위해."""
        r = session.get(
            f"{base}/rest/api/content/{page_id}/child/attachment",
            params={"filename": filename, "expand": "version"},
            timeout=30,
        )
        if not r.ok:
            return None
        results = r.json().get("results", [])
        return results[0]["id"] if results else None

    ok = fail = 0
    for paths in screenshots.values():
        for p in paths.values():
            if not p or not Path(str(p)).is_file():
                continue
            try:
                # 1차: 신규 첨부 POST
                resp = _post_multipart(
                    f"{base}/rest/api/content/{page_id}/child/attachment", p
                )
                if resp.status_code < 400:
                    ok += 1
                    continue
                # 2차: 'same file name as an existing attachment' → 새 버전 POST
                if "same file name" in (resp.text or "").lower():
                    aid = _existing_attachment_id(p.name)
                    if aid:
                        upd = _post_multipart(
                            f"{base}/rest/api/content/{page_id}/child/attachment/{aid}/data",
                            p,
                        )
                        if upd.status_code < 400:
                            ok += 1
                            continue
                        log(f"    [ATT-FAIL-UPDATE] {p.name}: {upd.status_code} "
                            f"{(upd.text or '')[:150]}")
                        fail += 1
                        continue
                    log(f"    [ATT-FAIL-AID] {p.name}: existing attachment id 조회 실패")
                    fail += 1
                    continue
                log(f"    [ATT-FAIL] {p.name}: {resp.status_code} "
                    f"{(resp.text or '')[:150]}")
                fail += 1
            except Exception as e:  # noqa: BLE001
                log(f"    [ATT-EXC] {p.name}: {e}")
                fail += 1
    return ok, fail


def cmd_compare_publish(args: argparse.Namespace) -> int:
    """주요 페이지의 DokuWiki / Confluence 스크린샷을 캡쳐해 비교 갤러리를
    Confluence 루트 페이지 하위에 발행/갱신.

    자동 후보 선정 (카테고리별 1) 또는 --select 명시 list. 양측을 헤드리스
    Chromium 으로 풀-페이지 캡쳐 → page POST → 첨부 → storage 본문 PUT.
    재실행 시 같은 제목 페이지 update (첨부도 같은 파일명이면 새 버전)."""
    # 자격증명 검증은 _confluence_session 이 처리.
    if not args.space_key or not args.root_page_id:
        log("--space-key / --root-page-id 필요.")
        return 2
    if not args.base_url:
        log("--base-url (CONFLUENCE_BASE_URL) 필요.")
        return 2
    dokuwiki_base = args.dokuwiki_base_url or env_default("DOKUWIKI_BASE_URL")
    if not dokuwiki_base:
        log("--dokuwiki-base-url (DOKUWIKI_BASE_URL) 필요.")
        return 2

    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")

    # --reset-rotation: 발행 이력 초기화 (다음 발행이 처음부터)
    if getattr(args, "reset_rotation", False):
        conn.execute("DELETE FROM meta WHERE key='compare_publish_history'")
        conn.commit()
        log("rotation 이력 초기화.")

    explicit = [s.strip() for s in args.select.split(",") if s.strip()] if args.select else None
    exclude_ids: set[str] = set()
    if not explicit:
        # 기본 동작: 이전 발행 이력 (compare_publish_history meta) 전체 제외
        # 매 호출이 새 페이지 batch 를 자동 선택. `--reset-rotation` 으로 초기화.
        # (구 `--rotate` flag 는 backward-compat no-op — 기본 동작이 됨.)
        raw = db_get_meta(conn, "compare_publish_history") or ""
        if raw:
            exclude_ids = set(line.strip() for line in raw.splitlines() if line.strip())
        if exclude_ids:
            log(f"기존 발행 이력 {len(exclude_ids)} 페이지 제외 "
                f"(--reset-rotation 으로 초기화).")
    candidates = _compare_select_candidates(
        conn, sample=args.sample, explicit_ids=explicit, exclude_ids=exclude_ids,
    )
    if not candidates:
        log("후보 페이지 없음 — --rotate 사용 중이면 --reset-rotation 으로 이력 초기화.")
        return 1

    log(f"=== compare-publish: {len(candidates)} 페이지 ===")
    for i, (reason, d, t, cid, sz) in enumerate(candidates, 1):
        log(f"  [{i}] {reason}: {d} ({t}) — {sz:,} bytes / cid={cid}")

    out_dir = Path(args.out_dir or "compare_screenshots")
    screenshots = _compare_capture_screenshots(
        candidates, out_dir,
        dokuwiki_base=dokuwiki_base,
        confluence_base=base,
        confluence_email=args.email,
        confluence_token=args.api_token,
        skip_existing=args.no_recapture,
    )
    captured_n = sum(1 for s in screenshots.values() if s.get("dwk") and s.get("cnf"))
    log(f"  캡쳐 완료: 양측 OK={captured_n} / 후보={len(candidates)}")

    if args.dry_run:
        log("--dry-run: 발행 skip.")
        return 0

    space_id = _resolve_space_id(session, base, args.space_key)
    if not space_id:
        return 1

    # default title 은 페이지 개수를 포함하지 않음 — 향후 --sample 바뀌어도 같은
    # 갤러리 페이지를 갱신 (`_compare_find_or_create_page` 가 title 검색).
    title = args.title or "DokuWiki ↔ Confluence 비교 갤러리"
    page_id = _compare_find_or_create_page(session, base, space_id, args.root_page_id, title)
    if not page_id:
        return 1
    log(f"  대상 페이지 id={page_id}")

    att_ok, att_fail = _compare_attach_screenshots(session, base, page_id, screenshots)
    log(f"  첨부 업로드: ok={att_ok} fail={att_fail}")

    body = _compare_build_storage_body(
        candidates, screenshots,
        dokuwiki_base=dokuwiki_base, confluence_base=base, space_key=args.space_key,
    )
    cur_ver = _get_page_version(session, base, page_id)
    if cur_ver is None:
        log("  [FAIL] 현재 version 조회 실패")
        return 1
    put_payload = {
        "id": page_id, "status": "current", "title": title,
        "body": {"representation": "storage", "value": body},
        "version": {"number": cur_ver + 1, "message": "compare-publish refresh"},
    }
    r = _request_with_retry(session, "PUT", f"{base}/api/v2/pages/{page_id}", json=put_payload)
    if r is None or r.status_code >= 400:
        log(f"  [FAIL] PUT: {r.status_code if r else 'no resp'} body={(r.text if r else '')[:300]}")
        return 1

    # 발행 성공 — history 누적 (다음 --rotate 발행 시 exclude 대상)
    published_ids = [c[1] for c in candidates]
    if getattr(args, "rotate", False) or not getattr(args, "no_track", False):
        prev_raw = db_get_meta(conn, "compare_publish_history") or ""
        prev_ids = set(line.strip() for line in prev_raw.splitlines() if line.strip())
        prev_ids.update(published_ids)
        db_set_meta(conn, "compare_publish_history", "\n".join(sorted(prev_ids)))
        log(f"  rotation 이력 갱신: 총 {len(prev_ids)} 페이지 (이번 {len(published_ids)} 추가)")

    log(f"=== 완료: {base}/spaces/{args.space_key}/pages/{page_id} ===")
    conn.close()
    return 0


def cmd_report_publish(args: argparse.Namespace) -> int:
    """state.db 통계 기반 보고서 페이지 발행/갱신 (wizard 의 마지막 단계 단독 호출)."""
    conn = db_connect(args.db)
    db_init(conn)
    _wizard_init(conn)
    try:
        summary = _wiz_report_publish(conn, args)
    except Exception as e:
        log(f"실패: {e}")
        return 1
    log(summary)
    conn.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """state.db 의 상태별 카운트 요약 (read-only).

    pages / attachments 의 status 별 카운트 + 진행률. state.db 변경 없음.
    빠른 진행 상황 확인용 — history/struct/verify 같은 별도 트랙은 각 자체
    `*-status` 명령 사용.
    """
    if not Path(args.db).exists():
        log(f"DB 없음: {args.db}")
        return 1
    conn = db_connect(args.db)

    print("==== pages ====")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM pages GROUP BY status ORDER BY status"
    ).fetchall()
    for status, count in rows:
        print(f"  {status:12s}  {count}")
    total = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    print(f"  {'TOTAL':12s}  {total}")

    print("\n==== attachments ====")
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM attachments GROUP BY status ORDER BY status"
    ).fetchall()
    if not rows:
        print("  (none)")
    for status, count in rows:
        print(f"  {status:12s}  {count}")

    print("\n==== meta ====")
    for k, v in conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall():
        if k in ("dokuwiki_src", "dokuwiki_base_url", "schema_version"):
            print(f"  {k}: {v}")

    conn.close()
    return 0


# § audit-3way (source ↔ rendered ↔ confluence 3-측 invariant audit)
#
# 자세한 시나리오는 docs/3way-audit.md. 본 절은 §6 의 매핑 테이블 +
# §3 의 신호 함수 + §5 의 명령 구현.

# PLUGIN_RENDER_INVARIANTS (S1 신호용 — S↔D)
# source 에 marker 가 있는데 rendered 에 결과 element 가 없으면 plugin 누락.
PLUGIN_RENDER_INVARIANTS: dict[str, dict] = {
    "htmlok": {
        # DokuWiki <html>...</html> 또는 <HTML>...</HTML> raw HTML embed
        "source_re": re.compile(r"</?(html|HTML)>"),
        "rendered_required": re.compile(r"<iframe\b|<script\b|<embed\b", re.I),
        "rendered_escape": re.compile(r"&lt;(html|HTML|iframe|script)\b"),
        "fix_hint": "saggi-dw/dokuwiki-plugin-htmlok 설치 + "
                    "$conf['plugin']['htmlok']['htmlok'] = 1 활성. "
                    "DokuWiki Jack Jackrum 부터 <html> core 제거됨.",
    },
    "monthcal": {
        "source_re": re.compile(r"~~monthcal\b|\{\{calendar:"),
        "rendered_required": re.compile(r'<table[^>]*class="[^"]*monthcal'),
        "fix_hint": "monthcal plugin 설치 — 또는 _convert_monthcal_fallback 가 "
                    "정적 표 처리 (변환기 측, INTENDED_TRANSFORMATIONS 화이트리스트).",
    },
    "wrap": {
        "source_re": re.compile(r"<WRAP\b|<wrap\b"),
        "rendered_required": re.compile(
            r'<(?:div|em|span)[^>]*class="[^"]*\bwrap_'
        ),
        "fix_hint": "wrap plugin 설치.",
    },
    "iframe_plugin": {  # Chris--S iframe plugin (별개 — {{url>...}} syntax)
        "source_re": re.compile(r"\{\{url>"),
        "rendered_required": re.compile(r"<iframe\b"),
        "fix_hint": "Chris--S/dokuwiki-plugin-iframe 설치 + 활성.",
    },
    "todo": {
        "source_re": re.compile(r"<todo\b"),
        "rendered_required": re.compile(
            r'<input[^>]*type="checkbox|<ul[^>]*class="(?:todo|plugin_todo)'
        ),
        "fix_hint": "todo plugin 설치.",
    },
    "include": {
        "source_re": re.compile(r"\{\{(?:section|page)>|~~INCLUDE\b"),
        "rendered_required": re.compile(r'class="plugin_include'),
        "fix_hint": "include plugin 설치.",
    },
    "struct": {
        "source_re": re.compile(r"---- *datatable\b|<datatemplatelist\b"),
        "rendered_required": re.compile(r'class="(?:plugin_struct|struct_table)'),
        "fix_hint": "struct plugin 설치. "
                    "마이그레이션 시 별도 트랙 (docs/struct-migration.md).",
    },
    "youtube": {
        "source_re": re.compile(r"\{\{youtube>"),
        "rendered_required": re.compile(r'<iframe[^>]*src="[^"]*youtu'),
        "fix_hint": "youtube plugin 설치 — 또는 _convert_youtube_fallback 가 "
                    "fallback 변환기 (INTENDED_TRANSFORMATIONS 화이트리스트).",
    },
    "encryptedpasswords": {
        "source_re": re.compile(r"<(?:decrypt|encrypt)>"),
        "rendered_required": re.compile(
            r'<span[^>]*class="encryptedpasswords"|&lt;(?:decrypt|encrypt)&gt;'
        ),
        "fix_hint": "encryptedpasswords plugin 설치 (dir name = encryptedpasswords/) "
                    "또는 _preprocess_encrypted_passwords 가 escape 케이스 처리.",
    },
}


# DOKUWIKI_TO_CONFLUENCE_MACROS (D1 신호용 — D↔C)
# rendered 의 element 가 confluence 의 매크로/element 로 매핑되어야.
DOKUWIKI_TO_CONFLUENCE_MACROS: list[dict] = [
    {
        "name": "wrap_info",
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_info'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="info"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_info → info 매핑.",
    },
    {
        "name": "wrap_tip",
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_tip'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="tip"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_tip → tip.",
    },
    {
        "name": "wrap_note",
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_(?:note|important)'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="note"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_note/important → note.",
    },
    {
        "name": "wrap_warning",
        "rendered_re": re.compile(
            r'<div[^>]*class="[^"]*wrap_(?:warning|alert|danger)'
        ),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="warning"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_warning/alert/danger → warning.",
    },
    {
        "name": "google_calendar_iframe",
        "rendered_re": re.compile(r'<iframe[^>]*src="[^"]*calendar\.google'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="iframe"'),
        "fix_hint": "_convert_google_calendar_iframe.",
    },
    {
        "name": "encryptedpasswords_expand",
        "rendered_re": re.compile(r'<span[^>]*class="encryptedpasswords"'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="expand"'),
        "fix_hint": "_convert_encrypted_passwords (plugin 활성 케이스).",
    },
    {
        "name": "todo_checkbox",
        "rendered_re": re.compile(r'<input[^>]*type="checkbox"'),
        "confluence_re": re.compile(
            r'<ac:task-list\b|<ac:placeholder[^>]*ac:type="checkbox"|\[\s*[xX ]?\s*\]'
        ),
        "fix_hint": "_convert_todos — mixed/inline todo 는 [x]/[ ] 텍스트 폴백 (의도).",
        "intent_always": "todo_inline_text",  # INTENDED 화이트리스트 자동 적용
    },
]


# INTENDED_TRANSFORMATIONS — 변환기의 의도된 변형 (화이트리스트). 신호 매칭 시
# 이 케이스로 분류되면 violation 으로 보고하지 않음.
INTENDED_TRANSFORMATIONS: dict[str, str] = {
    "monthcal_fallback":
        "monthcal → 정적 <table> (_convert_monthcal_fallback). source 측 plugin "
        "누락이어도 변환기가 처리하면 OK.",
    "smiley_to_emoji":
        "smiley <img> → unicode emoji (_convert_smileys). image 카운트 mismatch 정상.",
    "youtube_fallback":
        "fallback /_media/youtube/<id> → iframe embed (_convert_youtube_fallback).",
    "todo_inline_text":
        "inline / mixed todo → [x]/[ ] 텍스트 (보수적). checkbox element 없음 정상.",
}


def _audit_3way_load_source(doku_id: str, dwdata_root: Path) -> str | None:
    """dokuwiki source `.txt` 본문 읽기. 미존재 시 None.

    doku_id `a:b:c` → `<dwdata>/pages/a/b/c.txt`."""
    parts = doku_id.split(":")
    src_path = dwdata_root / "pages"
    for p in parts:
        src_path = src_path / p
    src_path = src_path.with_suffix(".txt")
    if not src_path.is_file():
        return None
    try:
        return src_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _audit_3way_strip_source_noise(source: str) -> str:
    """source 의 *코드 블록* 과 *주석* 제거 후 반환 — 매크로 marker 검색 시 거짓
    양성 회피.

    DokuWiki 의 코드 블록: `<code>...</code>` 또는 `%%...%%` 또는 `''...''`.
    주석: HTML 식 `<!-- ... -->`.
    """
    s = source
    # HTML 주석
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    # <code>...</code> 블록
    s = re.sub(r"<code[^>]*>.*?</code>", "", s, flags=re.S | re.I)
    # <file>...</file> 블록 (DokuWiki 의 file syntax)
    s = re.sub(r"<file[^>]*>.*?</file>", "", s, flags=re.S | re.I)
    # %%...%% (noformat / nowiki)
    s = re.sub(r"%%.*?%%", "", s, flags=re.S)
    # ''...'' (monospace inline) — 잘못 잡힐 위험 있어 보수적으로 skip
    return s


def _audit_3way_signal_S1(
    source_clean: str, rendered: str
) -> list[dict]:
    """S1 — plugin marker / rendered element 부재. source 에 marker 있고
    rendered 에 결과 element 없으면 plugin 누락 의심.

    Returns list of violation dicts.
    """
    out: list[dict] = []
    for plugin, table_entry in PLUGIN_RENDER_INVARIANTS.items():
        src_re = table_entry["source_re"]
        if not src_re.search(source_clean):
            continue
        if table_entry["rendered_required"].search(rendered):
            continue
        # 매크로가 escape 텍스트로 노출됐는지 (강한 시그니처)
        esc_re = table_entry.get("rendered_escape")
        severity = "high" if (esc_re and esc_re.search(rendered)) else "medium"
        out.append({
            "signal": "S1.plugin_render_missing",
            "plugin": plugin,
            "responsibility": "source",
            "severity": severity,
            "fix_hint": table_entry["fix_hint"],
        })
    return out


_S2_ESCAPE_RE = re.compile(r"&lt;([a-zA-Z][a-zA-Z0-9_-]+)\b")

# S2 신호 — *known plugin 매크로* 이름 매치만 위반. 임의 텍스트 (`<trkpt>`,
# `<Event>` 등 GPX/게임 데이터 본문) 는 false positive 회피.
_S2_KNOWN_PLUGIN_TAGS = {
    "html", "iframe", "decrypt", "encrypt", "todo", "wrap", "WRAP",
    "monthcal", "datatable", "datatemplatelist", "youtube", "include",
    "section", "page", "tagblock", "struct", "schema", "schema_assignments",
    "tag", "code-block",
}


def _audit_3way_signal_S2(rendered: str) -> list[dict]:
    """S2 — rendered 본문에 *known plugin 이름* 의 `&lt;TAG&gt;` escape 노출.

    *임의 텍스트* (GPX 의 trkpt/ele, 게임 데이터 Event/Roster 등) 는
    false positive — known plugin 이름 list 와 매치되는 경우만 위반.
    """
    matches = _S2_ESCAPE_RE.findall(rendered)
    if not matches:
        return []
    from collections import Counter
    counter = Counter(matches)
    # known plugin 이름과 매치 (대소문자 무관)
    plugin_matches = {k: v for k, v in counter.items()
                      if k.lower() in {t.lower() for t in _S2_KNOWN_PLUGIN_TAGS}}
    if not plugin_matches:
        return []
    top = sorted(plugin_matches.items(), key=lambda x: -x[1])[:5]
    return [{
        "signal": "S2.escape_text_exposed",
        "tags": top,
        "responsibility": "source",
        "severity": "medium",
        "fix_hint": "rendered 에 plugin 매크로 escape 텍스트 노출 — plugin "
                    "미해석 의심. S1 결과와 함께 검토.",
    }]


def _audit_3way_signal_D1(
    rendered: str, confluence: str, intent_whitelist: set[str] | None = None,
) -> list[dict]:
    """D1 — rendered 의 매크로 element 카운트 vs confluence 의 매크로 카운트
    mismatch.

    *변환에서 손실* (rendered > confluence) 만 위반. *추가* (confluence
    > rendered) 는 변환기가 정상적으로 만들어내는 부속 매크로 (revision
    header / 첨부 요약 panel 등) 가능성이 더 큼 → 위반 안 함.

    entry 의 `intent_always` 있으면 INTENDED_TRANSFORMATIONS 화이트리스트
    자동 적용 (예: todo_checkbox 의 inline 격하).
    """
    out: list[dict] = []
    intent = intent_whitelist or set()
    for entry in DOKUWIKI_TO_CONFLUENCE_MACROS:
        name = entry["name"]
        # 화이트리스트 자동 적용
        if entry.get("intent_always") in INTENDED_TRANSFORMATIONS:
            continue
        if name in intent:
            continue
        rendered_count = len(entry["rendered_re"].findall(rendered))
        confluence_count = len(entry["confluence_re"].findall(confluence))
        if rendered_count == 0 and confluence_count == 0:
            continue
        if rendered_count <= confluence_count:
            # 변환 시 추가는 정상 (revision header 등). 손실만 위반.
            continue
        out.append({
            "signal": "D1.macro_count_loss",
            "macro": name,
            "rendered_count": rendered_count,
            "confluence_count": confluence_count,
            "loss": rendered_count - confluence_count,
            "responsibility": "converter",
            "severity": "high",
            "fix_hint": entry["fix_hint"],
        })
    return out


# wrap 의 *알려진* 의미 클래스. 이 외 wrap_X 는 *unknown* — color/layout 추정.
_WRAP_KNOWN_CLASSES = {
    "wrap_info", "wrap_tip", "wrap_note", "wrap_important",
    "wrap_alert", "wrap_warning", "wrap_danger",
    "wrap_em", "wrap_hi", "wrap_box", "wrap_round",
    "wrap_left", "wrap_right", "wrap_center", "wrap_indent",
    "wrap_clear", "wrap_safari", "wrap_help",
}
_WRAP_CLASS_RE = re.compile(r'<div[^>]*class="([^"]*wrap_[a-z]+[^"]*)"')


def _audit_3way_signal_D2(rendered: str, confluence: str) -> list[dict]:
    """D2 — 색상 wrap → 코드블록 오변환 (사용자 발견 사례 D).

    rendered 에 *unknown* wrap 클래스 (예: wrap_color) 가 있는데 confluence 에
    code 매크로가 비정상적으로 많으면 변환기가 wrap → code 로 잘못 매핑한 의심.
    """
    found_unknown_wraps = []
    for cls_attr in _WRAP_CLASS_RE.findall(rendered):
        classes = cls_attr.split()
        for c in classes:
            if c.startswith("wrap_") and c not in _WRAP_KNOWN_CLASSES:
                found_unknown_wraps.append(c)
    if not found_unknown_wraps:
        return []
    code_count = confluence.count('ac:name="code"')
    pre_count = rendered.count("<pre>") + rendered.count('<pre ')
    code_excess = code_count - pre_count
    if code_excess > 0:
        return [{
            "signal": "D2.wrap_color_to_code_misroute",
            "unknown_wraps": list(set(found_unknown_wraps))[:10],
            "code_excess": code_excess,
            "responsibility": "converter",
            "severity": "high",
            "fix_hint": "_convert_wrap_callouts 의 unknown wrap_class fallback — "
                        "code 로 가지 말고 panel 또는 일반 div 로.",
        }]
    return []


def _audit_3way_signal_D3(rendered: str, confluence: str) -> list[dict]:
    """D3 — 이미지 cluster 분리 (사용자 발견 사례 C).

    rendered 의 `<p>` 안에 `<img>` 3+ 인라인 그룹이 confluence 에서 별도
    block 으로 분리됐는지. 간단 휴리스틱.
    """
    # rendered: <p>...<img/>...<img/>...<img/>...</p> 패턴 (3+ img in one p)
    rendered_clusters = 0
    for p_match in re.finditer(r"<p\b[^>]*>(.*?)</p>", rendered, re.S):
        body = p_match.group(1)
        if body.count("<img") >= 3:
            rendered_clusters += 1
    if rendered_clusters == 0:
        return []
    # confluence: <p>...<ac:image>...<ac:image>...<ac:image>...</p>
    confluence_clusters = 0
    for p_match in re.finditer(r"<p\b[^>]*>(.*?)</p>", confluence, re.S):
        body = p_match.group(1)
        if body.count("<ac:image") >= 3:
            confluence_clusters += 1
    if confluence_clusters < rendered_clusters:
        return [{
            "signal": "D3.image_cluster_split",
            "rendered_clusters": rendered_clusters,
            "confluence_clusters": confluence_clusters,
            "responsibility": "converter",
            "severity": "medium",
            "fix_hint": "img 인라인 group 보존 — Confluence 에서도 같은 <p> 안 "
                        "또는 ac:layout 으로 그룹 유지.",
        }]
    return []


def _audit_3way_analyze(
    doku_id: str,
    source: str | None,
    rendered: str | None,
    confluence: str | None,
) -> dict:
    """3-측 데이터로 신호 S1/S2/D1/D2/D3 모두 계산 후 종합.

    Returns dict — {doku_id, violations: [...], severity_counts: {...}}.
    """
    violations: list[dict] = []

    # 화이트리스트 — 페이지가 변환기에 의해 fallback 처리됐는지 검출
    intent: set[str] = set()
    if confluence:
        # _convert_monthcal_fallback — source 의 monthcal marker + confluence 의
        # 요일 헤더 표
        if re.search(r"~~monthcal\b|\{\{calendar:", source or ""):
            if re.search(r'<th[^>]*>(?:일|월|화|수|목|금|토)</th>', confluence):
                intent.add("monthcal_fallback")
        # _convert_youtube_fallback (VID-only paragraph 포함) — source 에
        # {{youtube>VID}} + confluence 에 youtube iframe macro
        if re.search(r"\{\{youtube>", source or ""):
            if re.search(r'<ri:url[^>]*ri:value="[^"]*youtube\.com', confluence):
                intent.add("youtube_fallback")

    # S 그룹 (S↔D)
    if source is not None and rendered is not None:
        source_clean = _audit_3way_strip_source_noise(source)
        s1 = _audit_3way_signal_S1(source_clean, rendered)
        # 화이트리스트 — 변환기가 처리한 fallback 은 S1 위반에서 제외
        if "monthcal_fallback" in intent:
            s1 = [v for v in s1 if v.get("plugin") != "monthcal"]
        if "youtube_fallback" in intent:
            s1 = [v for v in s1 if v.get("plugin") != "youtube"]
        violations.extend(s1)
        violations.extend(_audit_3way_signal_S2(rendered))

    # D 그룹 (D↔C)
    if rendered is not None and confluence is not None:
        violations.extend(_audit_3way_signal_D1(rendered, confluence, intent))
        violations.extend(_audit_3way_signal_D2(rendered, confluence))
        violations.extend(_audit_3way_signal_D3(rendered, confluence))

    # severity 카운트
    counts = {"source_high": 0, "source_medium": 0,
              "converter_high": 0, "converter_medium": 0,
              "converter_low": 0, "inconclusive": 0}
    for v in violations:
        key = f"{v['responsibility']}_{v['severity']}"
        if key in counts:
            counts[key] += 1
    return {
        "doku_id": doku_id,
        "violations": violations,
        "severity_counts": counts,
        "intent": list(intent),
    }


def cmd_audit_3way(args: argparse.Namespace) -> int:
    """audit-3way: source ↔ rendered ↔ confluence 3-측 invariant audit.

    각 페이지의 dokuwiki source (.txt) + rendered (raw/*.html) + confluence
    storage (storage/*.xml) 를 받아 docs/3way-audit.md §3 의 신호 (S1/S2 +
    D1/D2/D3) 모두 계산 후 violation 분류 (source vs converter).

    state.db 변경 없음 (read-only). 출력은 JSON 또는 stdout.
    """
    conn = db_connect(args.db)

    # 대상 페이지 선정
    if args.only:
        rows = conn.execute(
            "SELECT doku_id FROM pages WHERE doku_id=?", (args.only,)
        ).fetchall()
    elif args.sample:
        rows = conn.execute(
            "SELECT doku_id FROM pages "
            "WHERE confluence_page_id IS NOT NULL AND storage_path IS NOT NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (args.sample,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doku_id FROM pages "
            "WHERE confluence_page_id IS NOT NULL AND storage_path IS NOT NULL "
            "ORDER BY doku_id"
        ).fetchall()
    target_ids = [r[0] for r in rows]
    log(f"audit-3way 대상: {len(target_ids)} 페이지")

    # dokuwiki source 디렉터리 결정
    dwdata = None
    if args.with_source:
        dwdata_path = args.dokuwiki_data or db_get_meta(conn, "dokuwiki_src")
        if dwdata_path:
            dwdata = Path(dwdata_path).expanduser().resolve()
            if not (dwdata / "pages").is_dir():
                log(f"  [WARN] dwdata/pages 부재: {dwdata} — --with-source 무시")
                dwdata = None
        else:
            log("  [WARN] --with-source 지정됐으나 dokuwiki_src 미발견. "
                "--dokuwiki-data 명시 또는 discover 먼저.")

    # 결과 집계
    all_results: list[dict] = []
    summary = {"source_high": 0, "source_medium": 0,
               "converter_high": 0, "converter_medium": 0,
               "converter_low": 0, "inconclusive": 0,
               "pages_with_violation": 0,
               "pages_clean": 0}

    for i, doku_id in enumerate(target_ids, 1):
        # source 읽기 (옵션)
        source = _audit_3way_load_source(doku_id, dwdata) if dwdata else None
        # rendered 읽기
        raw_path = RAW_DIR / Path(*doku_id.split(":")).with_suffix(".html")
        rendered = raw_path.read_text(encoding="utf-8", errors="ignore") \
            if raw_path.is_file() else None
        # confluence storage 읽기 (로컬)
        storage_row = conn.execute(
            "SELECT storage_path FROM pages WHERE doku_id=?", (doku_id,)
        ).fetchone()
        storage_path = storage_row[0] if storage_row else None
        confluence = None
        if storage_path and Path(storage_path).is_file():
            confluence = Path(storage_path).read_text(encoding="utf-8", errors="ignore")

        result = _audit_3way_analyze(doku_id, source, rendered, confluence)
        all_results.append(result)

        if result["violations"]:
            summary["pages_with_violation"] += 1
        else:
            summary["pages_clean"] += 1
        for key, cnt in result["severity_counts"].items():
            summary[key] = summary.get(key, 0) + cnt

        # 진행 출력
        if i % 100 == 0 or i == len(target_ids):
            log(f"  [{i}/{len(target_ids)}] 진행...")

    # 출력
    log("=== audit-3way 요약 ===")
    log(f"  페이지 전체: {len(target_ids)}")
    log(f"  violation 있는 페이지: {summary['pages_with_violation']}")
    log(f"  깨끗한 페이지:         {summary['pages_clean']}")
    log(f"  source.high:     {summary['source_high']}")
    log(f"  source.medium:   {summary['source_medium']}")
    log(f"  converter.high:  {summary['converter_high']}")
    log(f"  converter.medium:{summary['converter_medium']}")

    # JSON 출력 (선택)
    if args.output_json:
        out_path = Path(args.output_json)
        out_path.write_text(
            _json.dumps({"summary": summary, "results": all_results},
                        ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"  JSON 결과: {out_path}")

    # severity threshold 에 따른 exit code
    threshold = args.severity_threshold
    if threshold == "high":
        bad = summary["source_high"] + summary["converter_high"]
    elif threshold == "medium":
        bad = (summary["source_high"] + summary["converter_high"]
               + summary["source_medium"] + summary["converter_medium"])
    else:
        bad = 0
    conn.close()
    return 0 if bad == 0 else 1




def env_default(key: str, fallback: str = "") -> str:
    return os.environ.get(key, fallback)


def _add_confluence_creds_args(parser: argparse.ArgumentParser) -> None:
    """모든 Confluence API 호출 명령에 공통인 자격증명 옵션 (--base-url /
    --email / --api-token) 일괄 추가. env_default 로 .env 자동 fill.

    이 helper 가 없으면 매 명령마다 3줄 boilerplate. 새 명령 추가 시 자격증명
    인자 누락 방지.
    """
    parser.add_argument("--base-url", default=env_default("CONFLUENCE_BASE_URL"))
    parser.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    parser.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))


def _add_confluence_space_args(parser: argparse.ArgumentParser) -> None:
    """Confluence 자격증명 + space/root 옵션 — upload / struct-upload /
    report-publish 등 *페이지 생성* 명령에 필요."""
    _add_confluence_creds_args(parser)
    parser.add_argument("--space-key", default=env_default("CONFLUENCE_SPACE_KEY"))
    parser.add_argument("--root-page-id", default=env_default("CONFLUENCE_ROOT_PAGE_ID"))


def _build_pipeline_subcommands(sub) -> None:
    """메인 파이프라인 (S1~S7): discover / render / convert / upload /
    rewrite-links / status."""
    sp_discover = sub.add_parser("discover", help="페이지 트리 발견 (S1)")
    sp_discover.add_argument(
        "--src",
        default=env_default("DOKUWIKI_SRC"),
        required=not bool(env_default("DOKUWIKI_SRC")),
        help="DokuWiki data 디렉터리 (pages/, media/, meta/ 의 상위)",
    )
    sp_discover.set_defaults(func=cmd_discover)

    sp_render = sub.add_parser("render", help="DokuWiki XHTML 캐시 (S2)")
    sp_render.add_argument(
        "--base-url",
        default=env_default("DOKUWIKI_BASE_URL"),
        help="DokuWiki HTTP base URL (예: http://dokuwiki.local)",
    )
    sp_render.add_argument("--user", default=env_default("DOKUWIKI_USER"))
    sp_render.add_argument("--password", default=env_default("DOKUWIKI_PASSWORD"))
    sp_render.add_argument("--force", action="store_true", help="이미 렌더링된 페이지도 다시 받음")
    sp_render.add_argument("--only", help="특정 doku_id 하나만 처리")
    sp_render.add_argument("--delay", type=float, default=0.0, help="요청 간 지연 (초)")
    sp_render.set_defaults(func=cmd_render)

    sp_convert = sub.add_parser("convert", help="XHTML -> storage format 변환 (S3)")
    sp_convert.add_argument("--force", action="store_true", help="이미 변환된 페이지도 다시 변환")
    sp_convert.add_argument("--only", help="특정 doku_id 만 변환")
    sp_convert.set_defaults(func=cmd_convert)

    sp_upload = sub.add_parser("upload", help="페이지/첨부 업로드 (S4~S6)")
    sp_upload.add_argument(
        "--space-key", default=env_default("CONFLUENCE_SPACE_KEY"), help="대상 Confluence space key"
    )
    sp_upload.add_argument(
        "--root-page-id", default=env_default("CONFLUENCE_ROOT_PAGE_ID"), help="루트 부모 페이지 ID"
    )
    _add_confluence_creds_args(sp_upload)
    sp_upload.add_argument("--dry-run", action="store_true")
    sp_upload.add_argument("--only", help="특정 doku_id 만 업로드")
    sp_upload.add_argument(
        "--include-parents", action="store_true",
        help="--only 사용 시 그 페이지의 부모 chain 도 함께 업로드"
    )
    sp_upload.add_argument("--limit", type=int, help="처음 N 개만 업로드")
    sp_upload.set_defaults(func=cmd_upload)

    sp_rewrite = sub.add_parser("rewrite-links", help="내부 링크 2-pass 치환 (S7)")
    _add_confluence_creds_args(sp_rewrite)
    sp_rewrite.add_argument("--dry-run", action="store_true")
    sp_rewrite.add_argument("--only", help="특정 doku_id 만 처리")
    sp_rewrite.set_defaults(func=cmd_rewrite_links)

    sp_status = sub.add_parser("status", help="상태 요약")
    sp_status.set_defaults(func=cmd_status)


def _build_history_subcommands(sub) -> None:
    """history 트랙: discover / render / convert / upload / status /
    rewrite-headers."""
    sp_hd = sub.add_parser(
        "history-discover", help="attic/ + meta/*.changes + media_attic/ 인덱싱"
    )
    sp_hd.set_defaults(func=cmd_history_discover)

    sp_hr = sub.add_parser("history-render", help="attic 리비전을 ?rev= 로 받아 캐시")
    sp_hr.add_argument("--base-url", default=env_default("DOKUWIKI_BASE_URL"))
    sp_hr.add_argument("--user", default=env_default("DOKUWIKI_USER"))
    sp_hr.add_argument("--password", default=env_default("DOKUWIKI_PASSWORD"))
    sp_hr.add_argument("--force", action="store_true")
    sp_hr.add_argument("--only", help="특정 doku_id 만")
    sp_hr.add_argument("--delay", type=float, default=0.0)
    sp_hr.add_argument("--limit", type=int, help="처음 N 개만")
    sp_hr.set_defaults(func=cmd_history_render)

    sp_hc = sub.add_parser("history-convert", help="raw_history → storage_history + 헤더 박스")
    sp_hc.add_argument("--force", action="store_true")
    sp_hc.add_argument("--only", help="특정 doku_id 만")
    sp_hc.add_argument(
        "--header-format", choices=REVISION_HEADER_FORMATS,
        help=f"revision 헤더 형식 (기본: meta 의 revision_header_fmt 또는 {REVISION_HEADER_DEFAULT}). "
        "none=헤더 생략, panel/info/note/tip/warning=매크로+shift-enter 줄바꿈, "
        "quote=blockquote, table=2열 표, paragraphs=기존 3개 <p>",
    )
    sp_hc.set_defaults(func=cmd_history_convert)

    sp_hu = sub.add_parser("history-upload", help="시간순 PUT replay → Confluence 버전 체인")
    _add_confluence_creds_args(sp_hu)
    sp_hu.add_argument("--only", help="특정 doku_id 만 replay")
    sp_hu.add_argument("--limit", type=int, help="처음 N revision PUT 후 종료")
    sp_hu.add_argument("--users-map", help="dokuwiki user → Confluence accountId JSON 매핑")
    sp_hu.set_defaults(func=cmd_history_upload)

    sp_hs = sub.add_parser("history-status", help="history 진행 상황 요약")
    sp_hs.set_defaults(func=cmd_history_status)

    sp_hrh = sub.add_parser(
        "history-rewrite-headers",
        help="이미 업로드된 페이지의 revision 헤더만 새 형식으로 교체 (PUT)",
    )
    _add_confluence_creds_args(sp_hrh)
    sp_hrh.add_argument(
        "--header-format", choices=REVISION_HEADER_FORMATS,
        help=f"새 헤더 형식 (기본: meta 의 revision_header_fmt 또는 {REVISION_HEADER_DEFAULT})",
    )
    sp_hrh.add_argument("--only", help="특정 doku_id 만")
    sp_hrh.add_argument("--limit", type=int, help="처음 N 페이지만")
    sp_hrh.add_argument("--users-map", help="dokuwiki user → Confluence accountId JSON")
    sp_hrh.add_argument("--dry-run", action="store_true")
    sp_hrh.set_defaults(func=cmd_history_rewrite_headers)


def _build_struct_subcommands(sub) -> None:
    """struct 트랙: discover / convert / upload / status / embed-on-bound-pages."""
    sp_sd = sub.add_parser(
        "struct-discover", help="meta/struct.sqlite3 → state.db 의 struct_* 인덱싱"
    )
    sp_sd.add_argument("--struct-db", help="명시적 struct.sqlite3 경로 (기본: <dokuwiki_src>/meta/struct.sqlite3)")
    sp_sd.set_defaults(func=cmd_struct_discover)

    sp_sc = sub.add_parser("struct-convert", help="struct rows → storage XML (snapshot/properties/native)")
    sp_sc.add_argument(
        "--mode", default="snapshot",
        choices=("snapshot", "properties", "native"),
        help="변환 모드 (snapshot=1 페이지 큰 표; properties=row 당 자식 페이지; native=동일 + Database 쉘 임베드)",
    )
    sp_sc.add_argument("--reconvert", action="store_true", help="UPLOADED 상태도 다시 변환")
    sp_sc.set_defaults(func=cmd_struct_convert)

    sp_su = sub.add_parser("struct-upload", help="struct-convert 결과를 Confluence 에 업로드")
    _add_confluence_space_args(sp_su)
    sp_su.add_argument("--probe", action="store_true", help="Confluence Database API 가용성만 측정")
    sp_su.add_argument("--probe-keep", action="store_true", help="probe 후 임시 Database 삭제 안 함")
    sp_su.add_argument(
        "--mode", default="auto",
        choices=("auto", "native", "properties", "snapshot"),
        help="업로드 모드. auto=각 schema 의 chosen_mode 사용. native 시도 후 미지원 컬럼이면 properties 폴백",
    )
    sp_su.add_argument(
        "--fallback", default="auto",
        choices=("auto", "properties", "snapshot", "fail"),
        help="native 모드에서 미지원 컬럼/row endpoint 일 때 격하 정책",
    )
    sp_su.add_argument("--limit", type=int, help="schema 처음 N개만 처리")
    sp_su.add_argument("--no-native-shell", action="store_true", help="native 모드에서 Confluence Database 빈 쉘 생성을 생략")
    sp_su.add_argument("--only-tbl", help="특정 schema tbl 만 처리")
    sp_su.add_argument("--row-limit", type=int, help="schema 별 row 처음 N개만 처리 (디버깅)")
    sp_su.add_argument("--index-only", action="store_true", help="인덱스 페이지만 PUT (row 페이지 갱신 skip)")
    sp_su.set_defaults(func=cmd_struct_upload)

    sp_ss = sub.add_parser("struct-status", help="struct 진행 상황 요약")
    sp_ss.set_defaults(func=cmd_struct_status)

    sp_se = sub.add_parser(
        "struct-embed-on-bound-pages",
        help="struct row 의 bound 페이지에 '관련 struct 데이터' 패널 임베드",
    )
    _add_confluence_creds_args(sp_se)
    sp_se.add_argument("--only-doku", help="특정 doku_id 한 페이지만 처리")
    sp_se.set_defaults(func=cmd_struct_embed_on_bound_pages)


def _build_oversized_subcommands(sub) -> None:
    """rewrite-oversized-pages / rewrite-oversized."""
    sp_rop = sub.add_parser(
        "rewrite-oversized-pages",
        help="본문 거부된 페이지를 skeleton + storage XML 첨부로 fallback (docs/oversized-pages.md C 모드)",
    )
    _add_confluence_space_args(sp_rop)
    sp_rop.add_argument("--only", help="특정 doku_id 만 처리")
    sp_rop.set_defaults(func=cmd_rewrite_oversized_pages)

    sp_ro = sub.add_parser(
        "rewrite-oversized",
        help="OVERSIZED 첨부 reference 를 note 매크로 메타 박스로 (docs/oversized-attachments.md §4.1 B 모드)",
    )
    _add_confluence_creds_args(sp_ro)
    sp_ro.add_argument("--no-upload", action="store_true", help="storage 만 갱신, Confluence PUT 안 함")
    sp_ro.set_defaults(func=cmd_rewrite_oversized)

    sp_so = sub.add_parser(
        "split-oversize",
        help="본문 한도 초과 페이지를 H1/H2/H3 단위로 child 페이지 분할 (parent = 목차)",
    )
    _add_confluence_space_args(sp_so)
    sp_so.add_argument("--only", help="특정 doku_id 만 처리")
    sp_so.add_argument("--max-chunk", type=int, default=100_000,
                       help="child 한 페이지 최대 본문 크기 (bytes, default 100KB)")
    sp_so.add_argument("--dry-run", action="store_true",
                       help="실제 PUT/POST 없이 분할 결과만 출력")
    sp_so.set_defaults(func=cmd_split_oversize)


def _build_audit_report_subcommands(sub) -> None:
    """audit / report / preview / lint."""
    sp_audit = sub.add_parser(
        "audit", help="Confluence 의 실제 페이지를 받아 dokuwiki raw 와 비교"
    )
    _add_confluence_creds_args(sp_audit)
    sp_audit.add_argument("--only", help="특정 doku_id 만 비교")
    sp_audit.add_argument("--sample", type=int, help="UPLOADED 중 무작위 N개")
    sp_audit.add_argument("--full", action="store_true", help="전체 UPLOADED 페이지 비교")
    sp_audit.add_argument("--failed-only", action="store_true", help="FAILED 페이지만 비교")
    sp_audit.add_argument(
        "--body-format", default="storage",
        choices=("storage", "view", "atlas_doc_format", "export_view"),
        help="Confluence body 받을 형식 (기본 storage)"
    )
    sp_audit.add_argument("--verbose", "-v", action="store_true")
    sp_audit.add_argument("--output-json", help="결과 JSON 저장 경로")
    sp_audit.add_argument("--output-html", help="HTML 리포트 경로")
    sp_audit.set_defaults(func=cmd_audit)

    sp_report = sub.add_parser(
        "report", help="corpus 통계 (pages/attachments/매크로/크기/title 충돌)"
    )
    sp_report.add_argument(
        "--limit", type=int, default=10,
        help="섹션별 상위 항목 표시 개수"
    )
    sp_report.set_defaults(func=cmd_report)

    sp_preview = sub.add_parser(
        "preview", help="한 페이지의 raw + storage 를 나란히 보여주는 HTML 생성"
    )
    sp_preview.add_argument("--doku-id", required=True, help="대상 doku_id")
    sp_preview.add_argument(
        "--output", help="출력 HTML 경로 (기본: preview-<doku_id>.html)"
    )
    sp_preview.set_defaults(func=cmd_preview)

    sp_lint = sub.add_parser("lint", help="storage XML 유효성 검사")
    sp_lint.add_argument(
        "--path", help=f"검사 대상 파일/디렉터리 (기본: {STORAGE_DIR})"
    )
    sp_lint.add_argument("--limit", type=int, default=20, help="실패 항목 출력 최대 개수")
    sp_lint.set_defaults(func=cmd_lint)

    sp_3way = sub.add_parser(
        "audit-3way",
        help="source ↔ rendered ↔ confluence 3-측 invariant audit "
             "(docs/3way-audit.md)",
    )
    sp_3way.add_argument("--only", help="특정 doku_id 만 검사")
    sp_3way.add_argument("--sample", type=int, help="UPLOADED 중 무작위 N 페이지")
    sp_3way.add_argument("--with-source", action="store_true",
                         help="dokuwiki source `.txt` 도 읽어 S↔D 신호 활성 "
                              "(--dokuwiki-data 또는 state.db meta dokuwiki_src 사용)")
    sp_3way.add_argument("--dokuwiki-data",
                         help="dokuwiki dwdata 디렉터리 경로 (--with-source 시)")
    sp_3way.add_argument("--output-json", help="결과 JSON 저장 경로")
    sp_3way.add_argument(
        "--severity-threshold", default="high",
        choices=("none", "high", "medium"),
        help="exit code 1 임계점 (기본: high 이상이면 1)",
    )
    sp_3way.set_defaults(func=cmd_audit_3way)


def _build_verify_subcommands(sub) -> None:
    """verify (build / import / status) — visual-audit 큐 + Phase 4 신호."""
    sp_verify = sub.add_parser(
        "verify",
        help="시각 검수 큐 (docs/visual-audit.md Phase 1: DOM side-by-side)",
    )
    verify_sub = sp_verify.add_subparsers(dest="action", required=True)

    sp_verify_build = verify_sub.add_parser(
        "build", help="우선순위 큐 + 단일 HTML 갤러리 생성"
    )
    sp_verify_build.add_argument(
        "--sample", type=int, default=200,
        help="큐 크기 (default 200, strategy=all 이면 무시)",
    )
    sp_verify_build.add_argument(
        "--strategy", default="auto",
        choices=("auto", "all", "critical-only"),
        help="auto=상위 sample 개; all=모든 UPLOADED; critical-only=score≥5",
    )
    sp_verify_build.add_argument(
        "--resume", action="store_true",
        help="이미 OK 결정된(현재 content_hash 와 같은) 페이지는 큐에서 제외",
    )
    sp_verify_build.add_argument(
        "--with-confluence-view", action="store_true",
        help="Confluence body-format=view 도 fetch (자격증명 필요)",
    )
    sp_verify_build.add_argument(
        "--body-format", default="view",
        choices=("view", "export_view", "storage", "atlas_doc_format"),
        help="--with-confluence-view 사용 시 body 포맷 (default view)",
    )
    sp_verify_build.add_argument(
        "--with-attachment-check", action="store_true",
        help="페이지의 모든 첨부에 v2 GET → 200 확인 (자격증명 필요)",
    )
    sp_verify_build.add_argument(
        "--with-screenshots", action="store_true",
        help="Playwright 로 양측 풀 렌더 PNG + phash 유사도 계산 "
             "(playwright + imagehash + pillow 필요)",
    )
    sp_verify_build.add_argument(
        "--with-vision", action="store_true",
        help="AI vision (Claude) 으로 스크린샷 자동 비교. "
             "--with-screenshots 와 ANTHROPIC_API_KEY 필요",
    )
    sp_verify_build.add_argument(
        "--dokuwiki-base-url",
        default=env_default("DOKUWIKI_BASE_URL"),
        help="--with-screenshots 사용 시 dokuwiki HTTP base",
    )
    # Phase 4 (visual-comparison-proposal.md) — 시각 비교 추가 신호 7가지
    sp_verify_build.add_argument(
        "--with-pixel-diff", action="store_true",
        help="(Phase 4 #1) chrome 마스킹 후 본문 픽셀 diff — Pillow 필요, with-screenshots 권장",
    )
    sp_verify_build.add_argument(
        "--with-tile-phash", action="store_true",
        help="(Phase 4 #2) 4×8 타일 분할 PHash → hotspot — imagehash+Pillow",
    )
    sp_verify_build.add_argument(
        "--with-element-compare", action="store_true",
        help="(Phase 4 #3) 블록 시퀀스 LCS 짝짓기 — bbox 메타 비교",
    )
    sp_verify_build.add_argument(
        "--with-ocr", action="store_true",
        help="(Phase 4 #4) OCR 백업 텍스트 비교 — pytesseract+tesseract 바이너리 필요",
    )
    sp_verify_build.add_argument(
        "--with-bbox-lcs", action="store_true",
        help="(Phase 4 #5) bbox tree LCS + 상대 너비 비교 — Playwright bbox 필요",
    )
    sp_verify_build.add_argument(
        "--with-storage-ast", action="store_true",
        help="(Phase 4 #6) raw HTML / Confluence storage canonical 트리 LCS",
    )
    sp_verify_build.add_argument(
        "--with-color-hist", action="store_true",
        help="(Phase 4 #7) 색상 histogram cosine similarity — Pillow",
    )
    sp_verify_build.add_argument(
        "--with-all-extra-signals", action="store_true",
        help="(Phase 4) 위 7가지 시각 비교 추가 신호 모두 활성",
    )
    _add_confluence_creds_args(sp_verify_build)
    sp_verify_build.add_argument("--reviewer", help="검수자 식별자 (default: --email)")
    sp_verify_build.add_argument(
        "--output", help="출력 HTML 경로 (default: verify-gallery.html)"
    )
    sp_verify_build.set_defaults(func=cmd_verify)

    sp_verify_import = verify_sub.add_parser(
        "import", help="브라우저에서 다운로드한 verify_decisions.json 을 state.db 에 반영"
    )
    sp_verify_import.add_argument("path", help="verify_decisions.json 경로")
    sp_verify_import.set_defaults(func=cmd_verify)

    sp_verify_status = verify_sub.add_parser(
        "status", help="검수 진행률 요약"
    )
    sp_verify_status.add_argument(
        "--verbose", "-v", action="store_true",
        help="NG 페이지 / stale 페이지 목록 표시"
    )
    sp_verify_status.set_defaults(func=cmd_verify)


def _build_dev_subcommands(sub) -> None:
    """dev (up / down / install-plugins) — 로컬 컨테이너."""
    sp_dev = sub.add_parser(
        "dev",
        help="로컬 DokuWiki 테스트 컨테이너 (dev/dokuwiki-local) up/down",
    )
    dev_sub = sp_dev.add_subparsers(dest="action", required=True)

    sp_dev_up = dev_sub.add_parser("up", help="컨테이너 기동 — full install 자동 감지 또는 data-only bootstrap")
    sp_dev_up.add_argument(
        "--src",
        default=env_default("DOKUWIKI_SRC"),
        help=f"DokuWiki 데이터 / full install 디렉터리 (기본: {DEV_DEFAULT_SRC}). "
        "lib/+doku.php 가 있으면 full install 로 인식해 통째로 복제, 없으면 data-only 로 보고 "
        "DokuWiki core 자동 다운로드 + 데이터 overlay + 플러그인 자동 설치.",
    )
    sp_dev_up.add_argument(
        "--bootstrap", action="store_true",
        help="full install 처럼 보여도 강제로 data-only bootstrap (core 새로 받음)",
    )
    sp_dev_up.add_argument(
        "--install-plugins", action="store_true",
        help="기존 클론이 있을 때도 누락된 플러그인을 추가 설치",
    )
    sp_dev_up.set_defaults(func=cmd_dev)

    sp_dev_down = dev_sub.add_parser("down", help="컨테이너 종료")
    sp_dev_down.add_argument(
        "--purge",
        action="store_true",
        help=f"종료 후 복제본 {DEV_CLONE_DST} 도 삭제",
    )
    sp_dev_down.set_defaults(func=cmd_dev)

    sp_dev_install = dev_sub.add_parser(
        "install-plugins",
        help="기존 클론에 플러그인 (자동 감지) 설치/재설치",
    )
    sp_dev_install.add_argument(
        "--src", default=env_default("DOKUWIKI_SRC"),
        help="감지에 쓸 데이터 디렉터리 (기본: 클론 자체에서 감지)",
    )
    sp_dev_install.set_defaults(func=cmd_dev)


def _build_tool_subcommands(sub) -> None:
    """plugin-scan / decrypt / link-check — 운영 보조 툴."""
    sp_ps = sub.add_parser(
        "plugin-scan",
        help="DokuWiki 페이지 본문을 스캔해 사용된 매크로/태그 → 미설치 플러그인 식별",
    )
    sp_ps.add_argument(
        "--src", default=env_default("DOKUWIKI_SRC"),
        help="스캔할 DokuWiki 데이터 / install 디렉터리",
    )
    sp_ps.add_argument(
        "--only-missing", action="store_true",
        help="미설치 + 비-core 플러그인 만 표시 (출력 압축)",
    )
    sp_ps.add_argument("--json", help="결과를 JSON 파일로 저장")
    sp_ps.add_argument(
        "--install", action="store_true",
        help="미설치 + PLUGIN_DOWNLOADS 매핑 있는 플러그인 자동 다운로드·설치 "
        "(기본 대상: /tmp/dwc_test_dokuwiki/dwdata, --install-into 로 override)",
    )
    sp_ps.add_argument("--install-into", help="자동 설치 대상 디렉터리 override")
    sp_ps.set_defaults(func=cmd_plugin_scan)

    sp_dec = sub.add_parser(
        "decrypt",
        help="encryptedpasswords plugin 의 cipher (AES-256-CBC) 복호화 "
        "— pycryptodome 필요. password 와 cipher 받아 평문 출력",
    )
    sp_dec.add_argument("--password", "-p", help="복호화 비밀번호 (생략 시 stdin getpass)")
    sp_dec.add_argument("cipher", nargs="*", help="base64-encoded cipher 1+ (생략 시 --page 또는 --confluence-id)")
    sp_dec.add_argument("--page", help="state.db 의 페이지 (raw/storage 본문에서 모든 cipher 추출 + 복호화)")
    sp_dec.add_argument("--confluence-id", help="Confluence 페이지 ID — 본문 GET 후 모든 cipher 복호화")
    _add_confluence_creds_args(sp_dec)
    sp_dec.set_defaults(func=cmd_decrypt)

    sp_lc = sub.add_parser(
        "link-check",
        help="Confluence 측 페이지의 링크 정합성 검증 (placeholder 잔존 / "
        "unresolved page link / 외부 URL HTTP HEAD)",
    )
    _add_confluence_creds_args(sp_lc)
    sp_lc.add_argument("--only", help="특정 doku_id 만")
    sp_lc.add_argument("--limit", type=int, help="처음 N 페이지")
    sp_lc.add_argument("--check-external", action="store_true",
                        help="외부 URL HTTP HEAD 검사 (느림 — 캐싱)")
    sp_lc.add_argument("--output", help="결과 JSON 경로")
    sp_lc.add_argument("--verbose", "-v", action="store_true",
                        help="문제 페이지 상세 출력 (최대 50개)")
    sp_lc.set_defaults(func=cmd_link_check)

    sp_cp = sub.add_parser(
        "compare-publish",
        help="DokuWiki/Confluence 양측 풀-페이지 스크린샷 + 비교 갤러리 발행/갱신",
    )
    _add_confluence_space_args(sp_cp)
    sp_cp.add_argument("--sample", type=int, default=8,
                       help="자동 선정 페이지 수 (기본 8). --select 지정 시 무시")
    sp_cp.add_argument("--select",
                       help="명시 페이지 list (쉼표 구분 doku_id) — 자동 선정 대체")
    sp_cp.add_argument("--title",
                       help="결과 페이지 제목 (기본: 'DokuWiki ↔ Confluence 비교 갤러리 …')")
    sp_cp.add_argument("--out-dir", default="compare_screenshots",
                       help="스크린샷 출력 디렉터리 (기본 compare_screenshots/)")
    sp_cp.add_argument("--no-recapture", action="store_true",
                       help="기존 PNG 재사용 (캡쳐 skip — 본문/첨부만 재발행)")
    sp_cp.add_argument("--dry-run", action="store_true",
                       help="발행 skip, 후보·캡쳐 결과만 출력")
    sp_cp.add_argument("--dokuwiki-base-url", default=env_default("DOKUWIKI_BASE_URL"),
                       help="DokuWiki HTTP base URL (스크린샷용)")
    sp_cp.add_argument("--rotate", action="store_true",
                       help="(no-op, 기본 동작이 됨) 매 호출이 이전 발행 페이지를 "
                            "제외하고 새 페이지를 selection. backward-compat 만 유지.")
    sp_cp.add_argument("--reset-rotation", action="store_true",
                       help="발행 이력 초기화 (다음 발행이 처음부터 selection). "
                            "--rotate 와 함께 또는 단독 사용 가능.")
    sp_cp.add_argument("--no-track", action="store_true",
                       help="발행 후 이력에 *추가 안 함* (테스트 / 일회성 발행). "
                            "기본은 매 발행마다 누적.")
    sp_cp.set_defaults(func=cmd_compare_publish)


def _build_wizard_subcommands(sub) -> None:
    """wizard / report-publish — orchestration + 보고서 발행."""
    sp_wiz = sub.add_parser(
        "wizard",
        help="대화형 step-by-step 마이그레이션 — 중단/재개 안전",
    )
    sp_wiz.add_argument("--restart", action="store_true", help="모든 step state reset 후 처음부터")
    sp_wiz.add_argument("--status", action="store_true", help="현재 진행 상황만 출력 후 종료")
    sp_wiz.add_argument("--from-step", dest="from_step", help="지정한 step 부터 다시 (이후 모두 pending)")
    sp_wiz.add_argument("--yes", action="store_true", help="모든 프롬프트 자동 yes (비대화)")
    sp_wiz.add_argument("--continue-on-error", action="store_true",
                        help="단계 실패해도 다음 단계로 진행 (기본: 실패 시 즉시 종료)")
    sp_wiz.add_argument("--audit-sample", type=int, help="audit 단계의 sample 수 (기본 50)")
    sp_wiz.add_argument("--verify-sample", type=int, help="verify 단계의 sample 수 (기본 100)")
    sp_wiz.add_argument("--dokuwiki-base", help="render 단계의 DokuWiki base URL override")
    sp_wiz.add_argument("--report-title", help="report-publish 단계의 페이지 제목 override")
    sp_wiz.set_defaults(func=cmd_wizard)

    sp_rp = sub.add_parser(
        "report-publish",
        help="state.db 통계 기반 결과 보고서를 Confluence 페이지로 발행/갱신",
    )
    _add_confluence_space_args(sp_rp)
    sp_rp.add_argument("--report-title", help="페이지 제목 (기본: DokuWiki → Confluence 마이그레이션 결과 보고서)")
    sp_rp.set_defaults(func=cmd_report_publish)


def build_parser() -> argparse.ArgumentParser:
    """argparse 트리 구성 (orchestrator) — 도메인별 _build_*_subcommands 에 위임.

    각 helper 는 sub (add_subparsers 결과) 를 받아 자기 도메인의 add_parser 만 등록한다.
    이 분리는 동작 변경 없는 순수 구조 리팩토링이며, 새 명령을 추가할 때 어느 helper 에
    넣을지로 위치를 결정한다."""
    p = argparse.ArgumentParser(
        prog="run.py",
        description="DokuWiki -> Confluence Cloud migration (scenarios in docs/scenarios.md)",
    )
    p.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite state path (default: {DEFAULT_DB_PATH})")
    # required=False — 인자 없이 실행하면 main() 이 도움말 출력
    sub = p.add_subparsers(dest="cmd", required=False)

    _build_pipeline_subcommands(sub)
    _build_history_subcommands(sub)
    _build_struct_subcommands(sub)
    _build_oversized_subcommands(sub)
    _build_audit_report_subcommands(sub)
    _build_verify_subcommands(sub)
    _build_dev_subcommands(sub)
    _build_tool_subcommands(sub)
    _build_wizard_subcommands(sub)

    return p


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "cmd", None):
        # 인자 없이 실행 → 도움말 출력 후 종료 (exit code 0)
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
