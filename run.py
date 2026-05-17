#!/usr/bin/env python3
"""
DokuWiki -> Confluence Cloud migration.

DokuWiki 가 렌더링한 최종 XHTML 을 받아 Confluence storage format 으로
변환하고, 네임스페이스 트리를 그대로 페이지 계층에 매핑한다. 자세한
설계는 docs/scenarios.md 의 S1~S10 을 참고.

서브커맨드:
  discover       페이지 트리 발견 (S1)
  render         DokuWiki XHTML 캐시 (S2)
  convert        XHTML -> Confluence storage format (S3)
  upload         페이지/첨부 생성·갱신 (S4~S6)
  rewrite-links  2-pass 내부 링크 치환 (S7)
  status         상태 요약
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
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


# ---------- 유틸 ----------

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


# ---------- DB ----------

def db_connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
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
    )
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


# ---------- S1: Discover ----------

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
    import re

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


# ---------- S2: Render ----------

def cmd_render(args: argparse.Namespace) -> int:
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


# ---------- S3: Convert (스켈레톤) ----------

def cmd_convert(args: argparse.Namespace) -> int:
    # TODO(S3): BeautifulSoup 으로 raw/*.html 을 파싱해 Confluence storage format
    # 으로 변환한다. 내부 페이지 링크는 placeholder 토큰으로 남겨 두고
    # links 테이블에 (src_doku_id, placeholder, target_doku_id) 를 기록한다.
    # 미디어 참조는 attachments 테이블에 'DISCOVERED' 상태로 upsert.
    # 결과 storage XML 은 storage/<doku_id>.xml 로 저장하고 content_hash 갱신.
    log("convert: 미구현 (S3 — docs/scenarios.md 참고)")
    return 1


# ---------- S4~S6: Upload (스켈레톤) ----------

def cmd_upload(args: argparse.Namespace) -> int:
    # TODO(S4~S6):
    #   1) parent_doku_id 가 NULL 인 페이지를 args.root_page_id 아래로 매달고
    #      자식들을 BFS 로 생성·갱신한다.
    #   2) 각 페이지의 첨부(attachments where status='DISCOVERED') 를
    #      Confluence v2 attachments API 로 업로드. SHA-256 중복 시 스킵,
    #      100MB 초과는 OVERSIZED 표시.
    #   3) 페이지 본문은 content_hash 변경 시에만 PUT. 변경 없음은 호출 생략.
    #   4) 429 응답은 Retry-After 기반 지수 백오프.
    log("upload: 미구현 (S4~S6 — docs/scenarios.md 참고)")
    return 1


# ---------- S7: Rewrite links (스켈레톤) ----------

def cmd_rewrite_links(args: argparse.Namespace) -> int:
    # TODO(S7): links 테이블의 placeholder 를 confluence_page_id 로 치환한
    # storage format 을 재업로드한다. resolved=1 로 표시. 미해결 링크
    # (target_doku_id 가 미존재 또는 SKIPPED) 는 일반 텍스트로 격하.
    log("rewrite-links: 미구현 (S7 — docs/scenarios.md 참고)")
    return 1


# ---------- 보조: status ----------

def cmd_status(args: argparse.Namespace) -> int:
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


# ---------- argparse ----------

def env_default(key: str, fallback: str = "") -> str:
    return os.environ.get(key, fallback)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="DokuWiki -> Confluence Cloud migration (scenarios in docs/scenarios.md)",
    )
    p.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite state path (default: {DEFAULT_DB_PATH})")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    sp_convert.set_defaults(func=cmd_convert)

    sp_upload = sub.add_parser("upload", help="페이지/첨부 업로드 (S4~S6)")
    sp_upload.add_argument(
        "--space-key", default=env_default("CONFLUENCE_SPACE_KEY"), help="대상 Confluence space key"
    )
    sp_upload.add_argument(
        "--root-page-id", default=env_default("CONFLUENCE_ROOT_PAGE_ID"), help="루트 부모 페이지 ID"
    )
    sp_upload.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_upload.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_upload.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_upload.add_argument("--dry-run", action="store_true")
    sp_upload.add_argument("--only", help="특정 doku_id 만 업로드")
    sp_upload.set_defaults(func=cmd_upload)

    sp_rewrite = sub.add_parser("rewrite-links", help="내부 링크 2-pass 치환 (S7)")
    sp_rewrite.add_argument("--dry-run", action="store_true")
    sp_rewrite.set_defaults(func=cmd_rewrite_links)

    sp_status = sub.add_parser("status", help="상태 요약")
    sp_status.set_defaults(func=cmd_status)

    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
