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


def _convert_html_to_storage(
    raw_html: str,
    src_root: Path,
) -> tuple[str, list[dict], list[dict], str | None]:
    """
    raw_html (DokuWiki export_xhtmlbody) -> (storage_xml, links, attachments, title).

    links:       [{'target': 'wiki:syntax', 'placeholder': 'dwc-link:wiki:syntax', 'anchor': '...'|None}, ...]
    attachments: [{'media_id': 'wiki:foo.png', 'filename': 'foo.png', 'src_path': str|None}, ...]
    title:       첫 h1 의 텍스트(없으면 None)
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
    # head/html/body 는 보이면 unwrap (children 만 살림).
    for tag_name in ("script", "style", "link", "meta", "noscript",
                     "iframe", "embed", "object", "form", "head"):
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
    for cls in ("breadcrumbs", "trace", "tools", "docInfo", "no", "headings"):
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
    )
    NOISE_CLASS_EXACT = {"toc", "page", "dokuwiki", "plugin_include_content"}
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

    return result, links, list(attachments.values()), title


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
            conn.execute(
                "UPDATE pages SET status='FAILED', last_error='no raw_xhtml_path', last_checked_at=? WHERE doku_id=?",
                (now_iso(), doku_id),
            )
            conn.commit()
            failed += 1
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
            storage_xml, links_out, attachments_out, h1_title = _convert_html_to_storage(
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

        # 제목은 h1 우선, 그다음 기존 값
        if h1_title:
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

    log(f"convert 완료: ok={ok} failed={failed}")
    conn.close()
    return 0


# ---------- S4~S6: Upload ----------

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100MB


def _confluence_session(args: argparse.Namespace):
    """인증된 requests.Session 반환. 자격증명 누락 시 None."""
    import requests

    if not args.email or not args.api_token:
        log("CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN 환경변수 또는 인자가 필요합니다.")
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
    if not args.space_key:
        log("--space-key (또는 CONFLUENCE_SPACE_KEY) 가 필요합니다.")
        return 2
    if not args.root_page_id:
        log("--root-page-id (또는 CONFLUENCE_ROOT_PAGE_ID) 가 필요합니다.")
        return 2

    STORAGE_DIR.mkdir(exist_ok=True)
    conn = db_connect(args.db)
    db_init(conn)
    db_set_meta(conn, "confluence_base_url", args.base_url.rstrip("/"))
    db_set_meta(conn, "confluence_space_key", args.space_key)
    db_set_meta(conn, "confluence_root_page_id", args.root_page_id)

    stub_count = _ensure_namespace_stubs(conn)
    if stub_count:
        log(f"네임스페이스 stub {stub_count}개 자동 생성")

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
        order = [d for d in order if d == args.only]
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
            prev_hash = db_get_meta(conn, f"uploaded_hash:{doku_id}")
            if prev_hash == content_hash:
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


def cmd_rewrite_links(args: argparse.Namespace) -> int:
    try:
        import bs4  # noqa: F401
    except ImportError:
        log("beautifulsoup4 가 필요합니다: pip install -r requirements.txt")
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
