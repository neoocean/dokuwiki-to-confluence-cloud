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
import re
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


def db_init(conn: sqlite3.Connection) -> None:
    conn.executescript(HISTORY_SCHEMA_DDL)
    conn.executescript(STRUCT_SCHEMA_DDL)
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


# ---------- S3: Convert ----------

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


def _build_ac_task(soup, task_id: int, checked: bool, text: str):
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
    DokuWiki todo plugin 출력을 Confluence task-list / 텍스트 마커로 변환.

    Step 1: <ul> 의 모든 직접 <li> 가 단일 pure todo (li 의 텍스트와 todo
            span 의 텍스트가 동일) 이면 <ul> 전체를 <ac:task-list> 로 치환.
            Confluence 의 task-list 는 block-level 이라 안전한 위치에만 둠.
    Step 2: 남은 모든 todo span → `[x] 텍스트` / `[ ] 텍스트` 인라인 마커.
    """
    counter = [0]

    def _next_id() -> int:
        counter[0] += 1
        return counter[0]

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

    # 남은 todo 들은 인라인 텍스트 마커로
    for todo in list(soup.find_all("span", class_="todo")):
        checked, text = _todo_checked_and_text(todo)
        prefix = "[x] " if checked else "[ ] "
        todo.replace_with(prefix + text)


def _convert_html_to_storage(
    raw_html: str,
    src_root: Path,
) -> tuple[str, list[dict], list[dict], str | None, list[str]]:
    """
    raw_html (DokuWiki export_xhtmlbody) -> (storage_xml, links, attachments, title, tags).

    links:       [{'target': 'wiki:syntax', 'placeholder': 'dwc-link:wiki:syntax', 'anchor': '...'|None}, ...]
    attachments: [{'media_id': 'wiki:foo.png', 'filename': 'foo.png', 'src_path': str|None}, ...]
    title:       첫 h1 의 텍스트(없으면 None)
    tags:        dokuwiki tag 플러그인의 page tag 값 리스트 (Confluence 페이지 label 로 매핑 후보)
    """
    from bs4 import BeautifulSoup, Comment

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
    import re as _re

    result = "".join(str(c) for c in soup.children)
    result = _re.sub(r"<(br|hr|img)([^>]*?)(?<!/)\s*>", r"<\1\2/>", result)

    for sentinel, text in code_bodies.items():
        safe = text.replace("]]>", "]]]]><![CDATA[>")
        result = result.replace(sentinel, f"<![CDATA[{safe}]]>")

    return result, links, list(attachments.values()), title, page_tags


def cmd_convert(args: argparse.Namespace) -> int:
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
        where, params = "status IN ('RENDERED', 'CONVERTED', 'FAILED')", ()
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


# ---------- S4~S6: Upload ----------

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100MB


CREDENTIAL_HELP = """
필요 환경변수:
  CONFLUENCE_EMAIL       Atlassian 계정 이메일
  CONFLUENCE_API_TOKEN   API 토큰 (https://id.atlassian.com/manage-profile/security/api-tokens 에서 생성)
또는 --email / --api-token 인자로 직접 전달.""".strip()


def _load_users_map(path: str | None) -> dict[str, str]:
    """
    --users-map <json> 파일에서 dokuwiki 사용자명 -> Confluence accountId
    매핑을 로드.

    Format: { "neoocean": "5e7f1234...", "lam": "60a01234..." }

    매핑 없으면 빈 dict — 호출자가 fallback 으로 텍스트만 표시.
    """
    if not path:
        return {}
    import json as _json
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
    import re as _re

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


def _confluence_session(args: argparse.Namespace):
    """인증된 requests.Session 반환. 자격증명 누락 시 None."""
    import requests

    if not args.email or not args.api_token:
        log("자격증명 누락 — Confluence API 호출 불가.")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return None
    s = requests.Session()
    s.auth = (args.email, args.api_token)
    s.headers.update({"Accept": "application/json"})
    return s


def _request_with_retry(session, method: str, url: str, **kwargs):
    """429/5xx 에 대해 지수 백오프. 6회 시도 후 마지막 응답 반환."""
    import requests

    delay = 1.0
    last_resp = None
    for _attempt in range(6):
        try:
            resp = session.request(method, url, timeout=kwargs.pop("timeout", 60), **kwargs)
        except requests.RequestException as e:
            log(f"    네트워크 에러, {delay}s 대기: {e}")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue
        last_resp = resp
        if resp.status_code < 400:
            return resp
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay
            log(f"    429, {wait}s 대기 후 재시도")
            time.sleep(wait)
            delay = min(delay * 2, 60.0)
            continue
        if 500 <= resp.status_code < 600:
            log(f"    {resp.status_code}, {delay}s 대기 후 재시도")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
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


def cmd_upload(args: argparse.Namespace) -> int:
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

    promoted = _promote_skipped_pages_in_chain(conn)
    if promoted:
        log(f"SKIPPED → placeholder 자동 promote: {promoted}개 (chain 부모로 쓰이던 페이지)")
    stub_count = _ensure_namespace_stubs(conn)
    if stub_count:
        log(f"네임스페이스 stub {stub_count}개 자동 생성")
    dup_count = _disambiguate_duplicate_titles(conn)
    if dup_count:
        log(f"중복 title 사전 disambiguation: {dup_count}개 (Confluence per-space unique 제약)")

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

    order = _bfs_upload_order(conn)
    if args.only:
        # only 지정 시 부모 chain 도 함께 포함하지 않으면 SKIP 되므로,
        # --include-parents 또는 단일 페이지 의도 분기.
        selected = {args.only}
        if args.include_parents:
            cur = args.only
            while cur:
                row = conn.execute(
                    "SELECT parent_doku_id FROM pages WHERE doku_id=?", (cur,)
                ).fetchone()
                cur = row[0] if row else None
                if cur:
                    selected.add(cur)
        order = [d for d in order if d in selected]
    if args.limit:
        order = order[: args.limit]

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


# ---------- S7: Rewrite links ----------

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

    import re as _re

    result = _re.sub(r"<(br|hr|img)([^>]*?)(?<!/)\s*>", r"<\1\2/>", result)

    for sentinel, text in link_body_texts.items():
        safe = text.replace("]]>", "]]]]><![CDATA[>")
        result = result.replace(sentinel, f"<![CDATA[{safe}]]>")

    return result, resolved, unresolved


# return statement above belongs to _rewrite_links_in_xml — _convert_html_to_storage
# 의 마지막 return 도 tags 포함하도록 별도 갱신.


def cmd_rewrite_links(args: argparse.Namespace) -> int:
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
        if new_hash == old_hash:
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


# ---------- history: discover ----------

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
    import re as _re

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


def _revision_header(rev_ts: int, user: str | None, comment: str | None,
                     type_code: str | None, users_map: dict[str, str]) -> str:
    """각 revision body 최상단에 박을 메타 헤더 박스 (history-migration §6.4)."""
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(rev_ts, tz=timezone.utc).isoformat(timespec="seconds")
    user_repr = _format_user(user, users_map) if user else "(unknown)"
    type_label = {
        "C": "create", "E": "edit", "e": "minor edit",
        "R": "revert", "D": "delete",
    }.get(type_code or "", type_code or "?")
    import html as _h
    comment_h = _h.escape(comment or "") if comment else "(no comment)"
    return (
        f'<ac:structured-macro ac:name="note">'
        f'<ac:rich-text-body>'
        f'<p>DokuWiki revision: <code>{dt}</code> ({type_label})</p>'
        f'<p>Author: {user_repr}</p>'
        f'<p>Comment: <code>{comment_h}</code></p>'
        f'</ac:rich-text-body>'
        f'</ac:structured-macro>'
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

        header = _revision_header(rev_ts, user, comment, type_code, users_map)
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


def cmd_history_upload(args: argparse.Namespace) -> int:
    """페이지마다 ts 오름차순으로 PUT replay. 각 PUT 의 version.message 에
    원본 dokuwiki rev 메타 (ts/user/comment) 동봉. resume 안전.

    *조심*: ~37k API 호출 가능. --limit 또는 --only 권장.
    """
    if not args.email or not args.api_token:
        log("자격증명 필요.")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return 2

    conn = db_connect(args.db)
    session = _confluence_session(args)
    if session is None:
        return 2
    base = args.base_url.rstrip("/")
    users_map = _load_users_map(args.users_map)

    # 대상 페이지: 메인 파이프라인에서 UPLOADED 된 페이지 + CONVERTED revision 가진 페이지
    where = "p.status='UPLOADED' AND p.confluence_page_id IS NOT NULL"
    params: tuple = ()
    if args.only:
        where = "p.doku_id=?"
        params = (args.only,)

    pages = conn.execute(
        f"SELECT p.doku_id, p.confluence_page_id, p.title FROM pages p "
        f"WHERE {where} ORDER BY p.doku_id",
        params,
    ).fetchall()

    log(f"history-upload 페이지 후보: {len(pages)}")
    page_ok = page_fail = rev_ok = rev_fail = 0

    for doku_id, cid, title in pages:
        # large body fallback 페이지 skip — 본문 PUT 거부
        if db_get_meta(conn, f"large_body_fallback:{doku_id}"):
            continue
        # 마지막 replayed_rev_ts 확인 (resume)
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
            continue
        log(f"  {doku_id}: {len(revs)} 리비전 replay")

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
                conn.execute(
                    "UPDATE revisions SET status='FAILED', last_error=?, last_checked_at=? "
                    "WHERE doku_id=? AND rev_ts=?",
                    (f"PUT {resp.status_code if resp else 'no resp'}", now_iso(), doku_id, rev_ts),
                )
                conn.commit()
                rev_fail += 1
                break  # 같은 페이지의 다음 rev 도 같은 base version 충돌 우려
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
            if args.limit and rev_ok >= args.limit:
                log(f"--limit {args.limit} 도달")
                conn.close()
                return 0
        page_ok += 1

    log(f"history-upload 완료: pages={page_ok} rev_ok={rev_ok} rev_fail={rev_fail}")
    conn.close()
    return 0 if rev_fail == 0 else 1


def cmd_history_status(args: argparse.Namespace) -> int:
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


# ---------- struct: discover / convert ----------

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
        import json
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

            import json
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


def _struct_row_to_storage_table(conn, sid: int, payload: dict) -> str:
    """단일 struct row 를 본문에 박을 storage XML 표로. snapshot/properties 모드 공용."""
    cols = conn.execute(
        "SELECT colref, name, dokuwiki_class FROM struct_columns "
        "WHERE sid=? AND enabled=1 OR sid=? ORDER BY sort",
        (sid, sid),
    ).fetchall()
    import html as _h
    rows_html = []
    for colref, name, cls in cols:
        val = payload.get(str(colref))
        if val is None:
            display = ""
        elif isinstance(val, list):
            display = ", ".join(str(v) for v in val)
        else:
            display = str(val)
        rows_html.append(
            f"<tr><th>{_h.escape(name or f'col{colref}')}</th>"
            f"<td>{_h.escape(display)}</td></tr>"
        )
    return f"<table>{''.join(rows_html)}</table>"


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

    schemas = conn.execute(
        "SELECT sid, tbl, row_count, status FROM struct_schemas "
        "WHERE status NOT IN ('SKIPPED', 'UPLOADED') ORDER BY tbl"
    ).fetchall()
    if not schemas:
        log("struct-convert 대상 schema 없음.")
        return 0

    import json as _json
    import html as _h

    converted = 0
    for sid, tbl, row_count, status in schemas:
        if row_count == 0:
            continue
        log(f"=== {tbl} (sid={sid}, {row_count} rows, mode={mode}) ===")
        rows = conn.execute(
            "SELECT pid, bound_doku_id, payload_json FROM struct_rows "
            "WHERE sid=? AND status='DISCOVERED' ORDER BY pid",
            (sid,),
        ).fetchall()
        if not rows:
            continue

        cols = conn.execute(
            "SELECT colref, name FROM struct_columns WHERE sid=? ORDER BY sort",
            (sid,),
        ).fetchall()
        col_headers = [_h.escape(name or f"col{cr}") for cr, name in cols]

        if mode == "snapshot":
            # 한 페이지에 모든 row 를 표로
            header_row = "<tr>" + "".join(f"<th>{h}</th>" for h in col_headers) + "</tr>"
            body_rows = []
            for pid, bound, payload_json in rows:
                payload = _json.loads(payload_json)
                cells = []
                for colref, _name in cols:
                    val = payload.get(str(colref), "")
                    if isinstance(val, list):
                        val = ", ".join(str(v) for v in val)
                    cells.append(f"<td>{_h.escape(str(val))}</td>")
                body_rows.append("<tr>" + "".join(cells) + "</tr>")
            body = (
                f'<h1>{_h.escape(tbl)} ({row_count} rows)</h1>'
                f'<p>DokuWiki struct schema sid={sid} 로부터 자동 생성.</p>'
                f'<table>{header_row}{"".join(body_rows)}</table>'
            )
            out = out_dir / f"{tbl}.snapshot.xml"
            out.write_text(body, encoding="utf-8")
            conn.execute(
                "UPDATE struct_schemas SET chosen_mode='snapshot', snapshot_page_id=NULL, status='DEFINED' WHERE sid=?",
                (sid,),
            )
            log(f"  snapshot storage → {out}")
            converted += 1
        elif mode == "properties":
            # 각 row 마다 별도 storage 파일 + index 페이지 storage
            for pid, bound, payload_json in rows:
                payload = _json.loads(payload_json)
                pp = _struct_row_to_storage_table(conn, sid, payload)
                pp_macro = (
                    "<ac:structured-macro ac:name='details'>"
                    "<ac:rich-text-body>" + pp + "</ac:rich-text-body>"
                    "</ac:structured-macro>"
                )
                out = out_dir / f"{tbl}.row.{pid}.xml"
                out.write_text(pp_macro, encoding="utf-8")
            # index 페이지: Page Properties Report
            index = (
                f"<h1>{_h.escape(tbl)} index</h1>"
                "<ac:structured-macro ac:name='detailssummary'>"
                f"<ac:parameter ac:name='cql'>label = \"dokuwiki-struct-{tbl}\"</ac:parameter>"
                "</ac:structured-macro>"
            )
            (out_dir / f"{tbl}.index.xml").write_text(index, encoding="utf-8")
            conn.execute(
                "UPDATE struct_schemas SET chosen_mode='properties', status='DEFINED' WHERE sid=?",
                (sid,),
            )
            log(f"  properties storage → {len(rows)} row 페이지 + 1 index")
            converted += len(rows)
        elif mode == "native":
            log(f"  native: Database API probe 필요 — struct-upload 단계에서.")
            conn.execute(
                "UPDATE struct_schemas SET chosen_mode='native', status='DEFINED' WHERE sid=?",
                (sid,),
            )

    conn.commit()
    log(f"struct-convert 완료: schemas 처리됨")
    conn.close()
    return 0


def cmd_struct_upload(args: argparse.Namespace) -> int:
    """struct-convert 결과를 Confluence 에.

    --mode=native 면 먼저 --probe 로 API 가용성 확인.
    properties / snapshot 은 storage XML 을 페이지로 생성 (메인
    파이프라인의 cmd_upload 와 유사).
    """
    if not args.email or not args.api_token:
        log("자격증명 필요.")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return 2
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
        # 빈 Database 1개 만들고 컬럼 추가 가능한지 시도
        log("=== Confluence Database API probe ===")
        resp = _request_with_retry(
            session, "POST", f"{base}/api/v2/databases",
            json={"spaceId": space_id, "title": "dwc-probe"},
        )
        log(f"  POST /api/v2/databases → {resp.status_code if resp else 'no resp'}")
        if resp and resp.status_code < 400:
            db_id = resp.json().get("id")
            log(f"  생성된 db: {db_id}")
            log("  추가 column/row API 는 별도 — 본 probe 는 endpoint 가용성만 확인.")
        log("=== probe 종료 ===")
        return 0

    out_dir = Path("storage_struct")
    schemas = conn.execute(
        "SELECT sid, tbl, chosen_mode FROM struct_schemas WHERE status='DEFINED' ORDER BY tbl"
    ).fetchall()

    pushed = failed = 0
    for sid, tbl, mode in schemas:
        log(f"=== {tbl} (mode={mode}) ===")
        if mode == "snapshot":
            sp = out_dir / f"{tbl}.snapshot.xml"
            if not sp.is_file():
                continue
            payload = {
                "spaceId": space_id,
                "parentId": args.root_page_id,
                "title": f"dokuwiki struct: {tbl}",
                "body": {"representation": "storage", "value": sp.read_text(encoding="utf-8")},
            }
            resp = _request_with_retry(session, "POST", f"{base}/api/v2/pages", json=payload)
            if resp is None or resp.status_code >= 400:
                # title 충돌 disambig
                if resp is not None and resp.status_code == 400 and "title" in (resp.text or "").lower():
                    payload["title"] = f"{payload['title']} ({sid})"
                    resp = _request_with_retry(session, "POST", f"{base}/api/v2/pages", json=payload)
            if resp is None or resp.status_code >= 400:
                log(f"  [FAIL] {tbl}: {resp.status_code if resp else 'no resp'}")
                failed += 1
                continue
            page_id = resp.json()["id"]
            conn.execute(
                "UPDATE struct_schemas SET snapshot_page_id=?, status='UPLOADED', last_checked_at=? WHERE sid=?",
                (str(page_id), now_iso(), sid),
            )
            conn.commit()
            log(f"  [SNAPSHOT] {tbl} → page {page_id}")
            pushed += 1
        else:
            log(f"  mode={mode} upload 미구현 — TODO. struct-migration.md §5 참고.")
    log(f"struct-upload 완료: pushed={pushed} failed={failed}")
    conn.close()
    return 0 if failed == 0 else 1


def cmd_struct_status(args: argparse.Namespace) -> int:
    conn = db_connect(args.db)
    print("==== struct_schemas ====")
    for sid, tbl, rc, cc, mode, status in conn.execute(
        "SELECT sid, tbl, row_count, column_count, COALESCE(chosen_mode,'-'), status FROM struct_schemas ORDER BY tbl"
    ).fetchall():
        print(f"  sid={sid:3} tbl={tbl:25} cols={cc:2} rows={rc:5} mode={mode:8} status={status}")
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


# ---------- rewrite-oversized-pages: 본문 거부된 페이지 → skeleton + 첨부 ----------

def cmd_rewrite_oversized_pages(args: argparse.Namespace) -> int:
    """
    Confluence 본문 한계를 넘은 페이지 (status='FAILED' AND
    last_error LIKE '%no resp%') 를 skeleton 본문 + 원본 storage XML
    첨부로 처리. 상세: docs/oversized-pages.md (C 모드).
    """
    if not args.email or not args.api_token:
        log("자격증명 필요.")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return 2

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


# ---------- rewrite-oversized: OVERSIZED 첨부 reference 를 메타 박스로 ----------

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
            import re as _re
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


# ---------- 보조: audit (dokuwiki vs Confluence 비교) ----------

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
    import re as _re
    return _re.sub(r"\s+", " ", text).strip()


def _confluence_get_page_body(session, base_url, page_id, body_format="storage"):
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
        import re as _re
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


def _diff_page(conn, session, base_url, doku_id, body_format="storage"):
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
        "notes": "; ".join(notes),
    }


def cmd_audit(args: argparse.Namespace) -> int:
    """업로드된 페이지를 Confluence 에서 다시 받아 dokuwiki raw 와 비교."""
    if not args.email or not args.api_token:
        log("audit 은 자격증명 필요.")
        for line in CREDENTIAL_HELP.splitlines():
            log("  " + line)
        return 2

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
        import json as _json
        Path(args.output_json).write_text(
            _json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"JSON 결과 저장 → {args.output_json}")

    if args.output_html:
        import html as _h
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


# ---------- 보조: report (corpus 통계 + 분포) ----------

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
        import re as _re
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


# ---------- 보조: preview (raw + storage 나란히) ----------

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

    import html as _h

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


# ---------- 보조: lint (storage XML 유효성 검사) ----------

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


# ---------- 보조: dev up/down (로컬 DokuWiki 테스트 컨테이너) ----------

DEV_COMPOSE_REL = Path("dev/dokuwiki-local/docker-compose.yml")
DEV_CLONE_DST = Path("/tmp/dwc_test_dokuwiki/dwdata")
DEV_DEFAULT_SRC = Path("/Users/neoocean/p4/playground/docker/dokuwiki/data")
DEV_BASE_URL = "http://127.0.0.1:18080"
DEV_HEALTH_PROBE = "/doku.php?id=wiki:syntax&do=export_xhtmlbody"
DEV_HEALTH_TIMEOUT = 30


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
        import re as _re
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


def cmd_dev(args: argparse.Namespace) -> int:
    compose = _project_root() / DEV_COMPOSE_REL
    if not compose.is_file():
        log(f"compose 파일이 없습니다: {compose}")
        return 2

    if args.action == "up":
        src = Path(args.src).expanduser().resolve() if args.src else DEV_DEFAULT_SRC
        if not src.is_dir():
            log(f"호스트 DokuWiki 데이터 디렉터리가 없습니다: {src}")
            return 2
        if not DEV_CLONE_DST.exists():
            if _dev_clone_source(src, DEV_CLONE_DST) != 0:
                log("데이터 복제 실패")
                return 1
        else:
            log(f"기존 복제본 재사용: {DEV_CLONE_DST}")
        _dev_patch_acl_off(DEV_CLONE_DST)

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


# ---------- 보조: status ----------

# ---------- verify (시각 검수 큐, docs/visual-audit.md) ----------

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
    session, base: str, page_id: str
) -> str | None:
    """Confluence v2 GET /pages/{id}?body-format=view 의 body.view.value."""
    try:
        url = f"{base.rstrip('/')}/api/v2/pages/{page_id}?body-format=view"
        resp = _request_with_retry(session, "GET", url, timeout=30)
        if resp is None or resp.status_code != 200:
            return None
        data = resp.json()
        return ((data.get("body") or {}).get("view") or {}).get("value")
    except Exception:
        return None


def _verify_render_html(
    queue: list[dict],
    confluence_bodies: dict[str, str | None],
    base_view_url: str | None,
    reviewer: str,
) -> str:
    """우선순위 큐를 받아 단일 정적 HTML 갤러리 생성."""
    import html as _h
    import json as _json

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

        right_pane = (
            f'<div class="body confluence-view">{confluence_body}</div>'
            if confluence_body
            else (
                '<div class="body storage-fallback"><em>Confluence body 조회 실패 '
                f'또는 자격증명 없음 — 우리 storage XML 추정 렌더:</em>'
                f'<div>{storage_xml}</div></div>'
            )
        )
        right_label = (
            "Confluence 실제 렌더 (body-format=view)"
            if confluence_body else "우리 storage XML (fallback)"
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
  <div class="grid">
    <div class="col raw">
      <h3>DokuWiki raw</h3>
      <div class="body">{raw_html}</div>
    </div>
    <div class="col conf">
      <h3>{right_label}</h3>
      {right_pane}
    </div>
  </div>
  <footer>
    <label><input type="radio" name="d-{idx}" value="OK"> OK</label>
    <label><input type="radio" name="d-{idx}" value="NG"> NG</label>
    <label><input type="radio" name="d-{idx}" value="DEFER"> 보류</label>
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
    out.push({
      doku_id: QUEUE[i].doku_id,
      decision: checked.value,
      notes: notes,
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
  document.getElementById('progress').textContent =
    reviewed + ' / ' + QUEUE.length + ' reviewed (OK ' + ok +
    ' / NG ' + ng + ' / DEFER ' + df + ')';
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
  .col { background: #fafafa; padding: .8em; border-radius: 6px;
         max-height: 60vh; overflow: auto; }
  .col h3 { margin-top: 0; font-size: .85em; color: #6e6e73; }
  .body { font-size: .9em; line-height: 1.5; }
  footer { margin-top: .8em; display: flex; gap: 1em; align-items: center; }
  footer label { font-size: .9em; }
  footer .notes { flex: 1; padding: .3em .5em; border: 1px solid #d2d2d7;
                  border-radius: 4px; }
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


def cmd_verify_build(args: argparse.Namespace) -> int:
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
    if args.with_confluence_view:
        if not args.email or not args.api_token:
            log("--with-confluence-view 는 자격증명 필요. "
                "fallback 으로 storage XML 표시.")
        else:
            session = _confluence_session(args)
            if session is None:
                log("Confluence 세션 생성 실패. storage XML fallback.")
            else:
                base = args.base_url.rstrip("/")
                base_view_url = base
                log(f"Confluence body-format=view fetch 시작 ({len(queue)} 페이지)")
                for i, q in enumerate(queue, 1):
                    page_id = q["confluence_page_id"]
                    if not page_id:
                        continue
                    body = _verify_fetch_confluence_view(session, base, page_id)
                    confluence_bodies[q["doku_id"]] = body
                    if i % 20 == 0:
                        log(f"  fetched {i}/{len(queue)}")

    reviewer = args.reviewer or args.email or "anonymous"
    html = _verify_render_html(queue, confluence_bodies, base_view_url, reviewer)

    out_path = Path(args.output) if args.output else Path("verify-gallery.html")
    out_path.write_text(html, encoding="utf-8")
    log(f"verify 갤러리 → {out_path}")
    log(f"  열어 검수 → 'JSON 다운로드' → "
        f"`python run.py verify import <파일>` 으로 반영")

    conn.close()
    return 0


def cmd_verify_import(args: argparse.Namespace) -> int:
    import json as _json

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
            " source_hash, visual_score, flags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doku_id,
                decision,
                item.get("notes") or "",
                item.get("reviewer") or "",
                item.get("reviewed_at") or now_iso(),
                item.get("source_hash") or "",
                item.get("visual_score"),
                item.get("flags") or "",
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
    if args.action == "build":
        return cmd_verify_build(args)
    if args.action == "import":
        return cmd_verify_import(args)
    if args.action == "status":
        return cmd_verify_status(args)
    log(f"unknown verify action: {args.action}")
    return 2


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
    sp_upload.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_upload.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_upload.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_upload.add_argument("--dry-run", action="store_true")
    sp_upload.add_argument("--only", help="특정 doku_id 만 업로드")
    sp_upload.add_argument(
        "--include-parents", action="store_true",
        help="--only 사용 시 그 페이지의 부모 chain 도 함께 업로드"
    )
    sp_upload.add_argument("--limit", type=int, help="처음 N 개만 업로드")
    sp_upload.set_defaults(func=cmd_upload)

    sp_rewrite = sub.add_parser("rewrite-links", help="내부 링크 2-pass 치환 (S7)")
    sp_rewrite.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_rewrite.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_rewrite.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_rewrite.add_argument("--dry-run", action="store_true")
    sp_rewrite.add_argument("--only", help="특정 doku_id 만 처리")
    sp_rewrite.set_defaults(func=cmd_rewrite_links)

    sp_status = sub.add_parser("status", help="상태 요약")
    sp_status.set_defaults(func=cmd_status)

    sp_hd = sub.add_parser(
        "history-discover", help="attic/ + meta/*.changes + media_attic/ 인덱싱"
    )
    sp_hd.set_defaults(func=cmd_history_discover)

    sp_hr = sub.add_parser("history-render", help="attic 리비전을 ?rev= 로 받아 캐시")
    sp_hr.add_argument(
        "--base-url", default=env_default("DOKUWIKI_BASE_URL"),
    )
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
    sp_hc.set_defaults(func=cmd_history_convert)

    sp_hu = sub.add_parser("history-upload", help="시간순 PUT replay → Confluence 버전 체인")
    sp_hu.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_hu.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_hu.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_hu.add_argument("--only", help="특정 doku_id 만 replay")
    sp_hu.add_argument("--limit", type=int, help="처음 N revision PUT 후 종료")
    sp_hu.add_argument("--users-map", help="dokuwiki user → Confluence accountId JSON 매핑")
    sp_hu.set_defaults(func=cmd_history_upload)

    sp_hs = sub.add_parser("history-status", help="history 진행 상황 요약")
    sp_hs.set_defaults(func=cmd_history_status)

    sp_sd = sub.add_parser(
        "struct-discover", help="meta/struct.sqlite3 → state.db 의 struct_* 인덱싱"
    )
    sp_sd.add_argument("--struct-db", help="명시적 struct.sqlite3 경로 (기본: <dokuwiki_src>/meta/struct.sqlite3)")
    sp_sd.set_defaults(func=cmd_struct_discover)

    sp_sc = sub.add_parser("struct-convert", help="struct rows → storage XML (snapshot/properties)")
    sp_sc.add_argument(
        "--mode", default="snapshot",
        choices=("snapshot", "properties", "native"),
        help="변환 모드 (snapshot=1 페이지에 큰 표; properties=row 당 자식 페이지; native=Database API)",
    )
    sp_sc.set_defaults(func=cmd_struct_convert)

    sp_su = sub.add_parser("struct-upload", help="struct-convert 결과를 Confluence 에 업로드")
    sp_su.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_su.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_su.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_su.add_argument("--space-key", default=env_default("CONFLUENCE_SPACE_KEY"))
    sp_su.add_argument("--root-page-id", default=env_default("CONFLUENCE_ROOT_PAGE_ID"))
    sp_su.add_argument("--probe", action="store_true", help="Confluence Database API 가용성만 측정")
    sp_su.set_defaults(func=cmd_struct_upload)

    sp_ss = sub.add_parser("struct-status", help="struct 진행 상황 요약")
    sp_ss.set_defaults(func=cmd_struct_status)

    sp_rop = sub.add_parser(
        "rewrite-oversized-pages",
        help="본문 거부된 페이지를 skeleton + storage XML 첨부로 fallback (docs/oversized-pages.md C 모드)",
    )
    sp_rop.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_rop.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_rop.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_rop.add_argument("--space-key", default=env_default("CONFLUENCE_SPACE_KEY"))
    sp_rop.add_argument("--root-page-id", default=env_default("CONFLUENCE_ROOT_PAGE_ID"))
    sp_rop.add_argument("--only", help="특정 doku_id 만 처리")
    sp_rop.set_defaults(func=cmd_rewrite_oversized_pages)

    sp_ro = sub.add_parser(
        "rewrite-oversized",
        help="OVERSIZED 첨부 reference 를 note 매크로 메타 박스로 (docs/oversized-attachments.md §4.1 B 모드)",
    )
    sp_ro.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_ro.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_ro.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
    sp_ro.add_argument("--no-upload", action="store_true", help="storage 만 갱신, Confluence PUT 안 함")
    sp_ro.set_defaults(func=cmd_rewrite_oversized)

    sp_audit = sub.add_parser(
        "audit", help="Confluence 의 실제 페이지를 받아 dokuwiki raw 와 비교"
    )
    sp_audit.add_argument(
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_audit.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_audit.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
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
        "--base-url",
        default=env_default("CONFLUENCE_BASE_URL", "https://woojinkim.atlassian.net/wiki"),
    )
    sp_verify_build.add_argument("--email", default=env_default("CONFLUENCE_EMAIL"))
    sp_verify_build.add_argument("--api-token", default=env_default("CONFLUENCE_API_TOKEN"))
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

    sp_dev = sub.add_parser(
        "dev",
        help="로컬 DokuWiki 테스트 컨테이너 (dev/dokuwiki-local) up/down",
    )
    dev_sub = sp_dev.add_subparsers(dest="action", required=True)

    sp_dev_up = dev_sub.add_parser("up", help="컨테이너 기동 (필요시 APFS clonefile 복제)")
    sp_dev_up.add_argument(
        "--src",
        default=env_default("DOKUWIKI_SRC"),
        help=f"복제할 원본 DokuWiki 데이터 디렉터리 (기본: {DEV_DEFAULT_SRC})",
    )
    sp_dev_up.set_defaults(func=cmd_dev)

    sp_dev_down = dev_sub.add_parser("down", help="컨테이너 종료")
    sp_dev_down.add_argument(
        "--purge",
        action="store_true",
        help=f"종료 후 복제본 {DEV_CLONE_DST} 도 삭제",
    )
    sp_dev_down.set_defaults(func=cmd_dev)

    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
