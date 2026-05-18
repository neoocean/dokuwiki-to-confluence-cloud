# DokuWiki → Confluence Cloud 마이그레이션 시나리오

이 문서는 자체 운영 중인 DokuWiki(`bitnami/dokuwiki`, Tailscale 백엔드)의
"도쿠위키에 의해 렌더링된 상태"를 Confluence Cloud로 일회성/반복 마이그레이션하는
파이썬 스크립트가 다뤄야 할 시나리오를 정리한다.

## 0. 전제

- 원본 위치: `~/p4/playground/docker/dokuwiki/data/data/`
  - `pages/<namespace>/<page>.txt` — DokuWiki 마크업 원본
  - `media/<namespace>/<file>` — 첨부(이미지, PDF 등)
  - `meta/<namespace>/<page>.meta` — 마지막 수정자/시각 등 메타데이터
  - `attic/` — 과거 리비전(이번 범위 밖)
- 렌더링 책임: DokuWiki 본체. 별도 파서를 구현하지 않고
  `http://<dokuwiki>/doku.php?id=<page>&do=export_xhtmlbody` 의 출력을 사용한다.
  → 플러그인/매크로/테마가 적용된 "최종 화면 상태"를 그대로 보존하기 위함.
- 대상: Confluence Cloud (`https://woojinkim.atlassian.net/wiki`), API v2 + storage format.
- 인증: Atlassian API 토큰(이메일과 Basic 인증). `upload_to_confluence/run.py` 와 동일한 자격증명 관례를 따른다.
- 상태 저장: SQLite (`state.db`). 페이지/첨부 단위 멱등성 확보.

## 1. 핵심 시나리오

### S1. 페이지 트리 발견 (Discovery)

- `pages/` 를 재귀 순회해 `*.txt` 를 수집한다.
- 각 파일 경로를 DokuWiki ID로 정규화한다. 예) `pages/wiki/syntax.txt` → `wiki:syntax`.
- `start.txt` 는 해당 네임스페이스의 인덱스 페이지로 취급한다(폴더 root).
- 결과: `(doku_id, src_path, namespace, title_candidate)` 의 리스트.

### S2. 페이지 렌더링 (Render via DokuWiki)

- DokuWiki HTTP 엔드포인트로 페이지별 XHTML 본문을 받아온다.
  - `GET /doku.php?id=<id>&do=export_xhtmlbody` — `<body>` 내부만 반환.
  - 인증이 켜져 있으면 세션 쿠키 또는 `u`/`p` 파라미터로 로그인 후 재호출.
- 응답이 비어 있거나 404 인 경우 원본 텍스트가 비어있는 placeholder 페이지로 추정 → 건너뛰고 로그.
- 받아온 XHTML 은 `raw/<doku_id>.html` 로 캐시한다(재실행 시 재요청 방지, 환경변수로 강제 갱신 가능).

### S3. XHTML → Confluence Storage Format 변환

- BeautifulSoup 으로 XHTML 을 파싱해 다음을 변환한다.
  - 내부 페이지 링크 `<a href="/doku.php?id=wiki:syntax">` → 변환 결과 페이지의 Confluence link.
    1차 통과 시점에 대상 페이지가 아직 없을 수 있으므로 **2-pass**: 1차에선 placeholder id 로 기록, 모든 페이지 생성 후 2차에서 실제 Confluence pageId 로 치환.
  - 미디어 링크 `<img src="/lib/exe/fetch.php?media=wiki:foo.png">` →
    해당 미디어를 첨부로 업로드한 뒤 `<ac:image><ri:attachment ri:filename="..."/></ac:image>` 로 치환.
  - 코드 블록 `<pre class="code <lang>">` → `<ac:structured-macro ac:name="code">` 로 변환.
  - 표/인용/리스트 등 표준 HTML 은 storage format 이 그대로 수용하므로 보존.
  - DokuWiki 가 부여한 `class`, `id`, 섹션 편집 앵커(`<a class="secedit">`)는 제거.
- 변환 결과를 `storage/<doku_id>.xml` 로 저장(디버깅/재실행용).

### S4. 페이지 계층 매핑

- DokuWiki 네임스페이스 트리를 Confluence 부모-자식 트리에 그대로 매핑한다.
  - 루트 공간/부모 페이지는 CLI 파라미터(`--space-key`, `--root-page-id`)로 지정.
  - `wiki:syntax` → `wiki` 부모 페이지(없으면 자동 생성) 아래의 자식 페이지 `syntax`.
  - 네임스페이스의 `start` 페이지가 존재하면 그것을 부모 페이지의 본문으로 사용, 없으면 빈 본문으로 생성.
- 페이지 제목은 다음 우선순위로 결정: ① 변환된 XHTML 의 첫 `<h1>` 텍스트 ② `meta/.../<page>.meta` 의 `title` ③ DokuWiki ID 의 마지막 세그먼트.
- 동일 부모 아래 제목 충돌 시 ID 뒷부분을 suffix 로 붙인다(`Foo`, `Foo (wiki:bar:foo)`).

### S5. 첨부 업로드

- S3 에서 수집한 미디어 참조 집합을 페이지별로 묶어 Confluence 첨부 API 로 업로드한다.
  - `POST /wiki/api/v2/pages/{id}/attachments` (multipart).
  - 동일 파일명이 이미 존재하면 SHA-256 비교 후 동일하면 스킵, 다르면 새 버전 업로드.
- 100MB 초과 파일은 경고만 남기고 본문에서 외부 링크로 치환한다(Confluence 단일 첨부 제한).

### S6. 페이지 생성/업데이트 (Upsert)

- 매 페이지에 대해 `state.db` 의 `(doku_id, content_hash, confluence_page_id, version)` 을 보고 결정.
  - 신규: `POST /wiki/api/v2/pages` 로 생성, 반환 id 를 저장.
  - 본문 해시 변경: 현재 버전을 가져온 뒤 `PUT /wiki/api/v2/pages/{id}` 로 업데이트.
  - 변경 없음: 호출 생략.
- DokuWiki meta 의 `last_change.date` 를 변경 감지 보조 신호로 사용한다.

### S7. 링크 재작성 (Second pass)

- 모든 페이지가 생성되어 doku_id → confluenceId 매핑이 완성된 후,
  S3 에서 placeholder 로 남겨둔 내부 링크를 실제 페이지로 치환하고 필요한 페이지만 재업데이트한다.
- 미해결 링크(외부/삭제된 페이지)는 빨간 표시 대신 일반 텍스트 + 주석으로 남긴다.

### S8. 멱등성과 재실행

- 모든 네트워크 호출은 `state.db` 에 결과를 기록하고, 재실행 시 `--force`/`--only <id>` 등으로 선택적 재처리.
- 트랜잭션 경계: 페이지 단위. 페이지 중간에 실패하면 해당 페이지만 다음 실행에서 재시도.
- Confluence API rate limit (HTTP 429) 발생 시 `Retry-After` 헤더 기반으로 지수 백오프.

### S9. Dry-run / Diff

- `--dry-run`: 모든 변환을 수행하되 Confluence 쓰기 호출은 생략. 어떤 페이지가 신규/변경/스킵될지 요약 출력.
- `--diff <doku_id>`: 한 페이지의 현재 storage format 과 Confluence 의 현재 본문을 비교 출력.

### S10. 로깅과 진행 표시

- `logs/run-<timestamp>.log` 에 INFO 이상 기록, 콘솔은 `rich` progress bar (페이지/첨부 카운트).
- 페이지별 결과를 마지막에 표로 요약: created / updated / skipped / failed.

## 2. 비기능 요구사항

- 외부 의존성은 `requests`, `requests-toolbelt`, `beautifulsoup4`, `lxml`, `rich` 정도로 제한.
- 단일 파일 진입점 `run.py` + `state.db` (`upload_to_confluence/` 의 운영 관례 재사용).
- 자격증명은 CLI/환경변수만, 소스 코드에 하드코딩 금지(기존 `upload_to_confluence/run.py` 에 노출된 토큰은 별도 회전 필요).
- 백업: 실행 전 `data/` 스냅샷은 기존 P4 체크인으로 갈음.

## 3. 비범위 (Out of Scope, 초기 버전)

- DokuWiki ACL → Confluence permission 매핑.
- 과거 리비전(`attic/`) 마이그레이션 — *현재 파이프라인 범위 밖이지만
  채택안 확정됨*. 별도 시나리오:
  [`docs/history-migration.md`](history-migration.md). 핵심 사실 +
  채택안:
  - DokuWiki side 데이터량: attic 955MB / 37,287 리비전 / 1,630 페이지
    (평균 23 rev), media_attic 522MB / 193 파일.
  - Confluence Cloud 의 결정적 제약: `version.createdAt` /
    `version.authorId` 의 backdate API 없음 — "원본 그대로"는 불가능.
  - 6가지 옵션 매트릭스 그대로 유지 (A 푸터 메타 / B 시간순 replay /
    C 자식 페이지 / D content property / E raw 첨부 / F 미디어 버전
    체인) — `--mode` 플래그로 미래 전환 가능.
  - **채택 (2026-05-18): 텍스트 = B + A** (시간순 PUT replay 로
    Confluence 버전 체인 보존 + latest 페이지에 푸터 박스로 변경
    요약), **미디어 = F** (첨부 버전 체인). D / C / E 는 옵션만 유지,
    디폴트 비활성.
  - 구현은 별도 PR 예정. 신규 서브커맨드 `history-discover` /
    `history-render` / `history-convert` / `history-upload` /
    `history-media` + `revisions` / `media_revisions` /
    `history_meta` 스키마 추가.
- 코멘트(`meta/_comments.changes`) 이전.
- 양방향 동기화. 본 스크립트는 DokuWiki → Confluence 단방향.

## 4. 마이그레이션 실행 순서 (Runbook)

1. `python run.py discover --src ~/p4/playground/docker/dokuwiki/data/data` → 페이지 목록 확정.
2. `python run.py render --base-url http://dokuwiki.local` → XHTML 캐시 채움.
3. `python run.py convert` → storage format 생성, 첨부 목록 추출.
4. `python run.py upload --space-key WIKI --root-page-id <id> --dry-run` 으로 점검.
5. 동일 명령 `--dry-run` 제거 후 실제 업로드.
6. `python run.py rewrite-links` 로 2차 통과.
7. 결과 요약 확인 → 실패 항목 `--only` 로 재시도.

## 5. 로컬 DokuWiki 테스트 환경

라이브 테스트는 호스트의 `~/p4/playground/docker/dokuwiki/data` 를 절대
수정하지 않으면서 `?do=export_xhtmlbody` 가 응답하는 인스턴스를 띄워야 한다.
실제로 시도해 본 결과를 정리한다.

### 5.1 시도해본 옵션과 결론

| 시도 | 결과 |
|------|------|
| `bitnami/dokuwiki:latest` (기존 운영 컴포즈 재사용) | Docker Hub 접근 거부 — Bitnami 가 공개 카탈로그를 정리해 더 이상 풀할 수 없음. |
| 원본 데이터를 `:ro` 로 그대로 마운트 | DokuWiki 부팅 시 `data/pages`, `data/attic`, `data/meta`, `data/media` 각각에 대해 writable 검증을 수행해 거의 모든 서브디렉터리가 writable 이어야 통과한다. `:ro` 마운트로는 부팅 자체가 안 됨. |
| 원본 `:ro` + writable 디렉터리만 부분 overlay | tmpfs overlay 가 root 소유로 마운트돼 apache (`www-data`) 가 쓰지 못함. 시작 스크립트에서 chown 해도 mediadir 검증을 끝내 통과하지 못함. |
| **APFS clonefile (`cp -cR`) 로 트리 전체 복제 후 writable 마운트** | 채택. 14GB 가 9.5초·디스크 사용 0 으로 복제되고 원본은 손대지 않음. |

### 5.2 채택한 셋업

`dev/dokuwiki-local/docker-compose.yml` 에 보존. 핵심:

- 이미지: `php:8.2-apache` (DokuWiki 트리에 `doku.php` 와 `vendor/` 가 모두 포함돼 있어 별도 dokuwiki 이미지 불필요).
- 데이터 마운트: `/tmp/dwc_test_dokuwiki/dwdata:/var/www/html:rw` — `cp -cR /Users/neoocean/p4/playground/docker/dokuwiki/data /tmp/dwc_test_dokuwiki/dwdata` 로 만든 APFS clonefile 복제본.
- 포트: `127.0.0.1:18080:80` (로컬 한정).
- PHP 8.2 deprecation/warning 억제: 컨테이너 시작 시 `display_errors=Off`, `error_reporting=E_ERROR` 를 `/usr/local/etc/php/conf.d/zz-quiet.ini` 에 주입. 그러지 않으면 export 응답 본문 앞에 `<br /><b>Warning</b>: Trying to access array offset on value of type bool ...` 가 섞여 들어와 bs4 가 잘못 파싱한다.
- mod_rewrite 활성화 (`a2enmod rewrite`) — DokuWiki 의 path 스타일 URL 처리에 필요.

### 5.3 실행/정리

위 절차(데이터 복제 + docker compose + 헬스체크 + 정리)는 `run.py dev` 서브커맨드로 패키징되어 있다.

```sh
# 컨테이너 기동 — 처음이면 호스트 데이터를 APFS clonefile 로 복제하고
# (cp -cR; 실패 시 cp -R 폴백), docker compose 로 띄운 뒤 export 엔드포인트가
# 200 을 반환할 때까지 최대 30초 대기.
python run.py dev up
# (필요하면 다른 원본 디렉터리로: `python run.py dev up --src /other/path`)

# 한 페이지로 end-to-end 검증
python run.py discover --src /Users/neoocean/p4/playground/docker/dokuwiki/data/data
python run.py render   --base-url http://127.0.0.1:18080 --only wiki:syntax
python run.py convert  --only wiki:syntax
python run.py status

# 컨테이너만 종료 (복제본은 다음 `dev up` 에서 즉시 재사용)
python run.py dev down

# 복제본까지 정리해 디스크/inode 도 회수
python run.py dev down --purge
```

내부 동작은 다음과 같다.

- `dev up`
  1. `/tmp/dwc_test_dokuwiki/dwdata` 가 없으면 `cp -cR <src> <dst>` 실행. macOS APFS 면 clonefile 로 0-byte. 다른 파일시스템이면 자동으로 `cp -R` 로 폴백한다(이 경우 디스크가 원본만큼 늘어남).
  2. `dev/dokuwiki-local/docker-compose.yml` 를 사용해 `docker compose up -d`.
  3. `http://127.0.0.1:18080/doku.php?id=wiki:syntax&do=export_xhtmlbody` 가 HTTP 200 을 반환할 때까지 1초 간격으로 폴링(최대 30초). 타임아웃 시 종료 코드 1.
- `dev down`
  1. `docker compose down` 실행.
  2. `--purge` 옵션 시 `/tmp/dwc_test_dokuwiki/dwdata` 와 빈 부모를 정리.

이미 컨테이너가 떠 있을 때 `dev up` 을 다시 호출해도 안전하다 — 복제본이 있으면 그대로 쓰고 compose 가 idempotent 하게 동작한다.

## 6. DokuWiki 렌더링 출력에서 확인된 사실

`?do=export_xhtmlbody` 출력을 실제로 읽어보고 변환기가 알아야 할 패턴들.
모든 인스턴스에 일반화되는 것은 아니므로 새 인스턴스에서 변환기를 돌릴 때는
다시 한 페이지부터 검증해야 한다.

### 6.1 내부 페이지 링크

URL rewrite (`useslash`/`userewrite`) 설정에 따라 모양이 갈린다.

- `userewrite=0` (기본): `<a href="/doku.php?id=wiki:syntax">…</a>`
- `userewrite=1` (path-rewrite, 본 인스턴스): `<a href="/wiki/syntax">…</a>` — `?id=` 가 사라짐.

다행히 두 모드 모두 `<a>` 에 다음 두 단서를 같이 박는다:

- `class="wikilink1"` — 페이지가 존재
- `class="wikilink2"` — 페이지 없음 (broken link)
- `data-wiki-id="<doku_id>"` — 항상 절대 doku id

따라서 변환기는 **href 보다 `data-wiki-id` 와 `wikilink*` class 를 먼저** 보고 그 값을 doku_id 로 채택한다. URL 분석은 fallback.

### 6.2 미디어 / 첨부 URL

- 내부 미디어: `/_media/<ns>/<file>` 혹은 `/lib/exe/fetch.php?media=<ns>:<file>`
- 이미지 디테일 페이지: `/_detail/<ns>/<file>`
- **외부 이미지 proxy**: `/lib/exe/fetch.php?w=200&h=50&tok=...&media=https%3A%2F%2Fwww.php.net%2Fimages%2Fphp.gif` — DokuWiki 의 리사이즈/캐시 프록시 기능. `media=` 가 URL-인코딩된 외부 URL 을 담는다.

변환기 규칙: `media=` 값이 `http://` 또는 `https://` 로 시작하면 첨부가 아니라 **외부 이미지** 로 분류하고 `<img src>` 를 디코딩된 실제 URL 로 교체한다. 첨부 테이블에는 넣지 않는다.

### 6.3 인터위키 / 외부 링크

- `<a class="interwiki iw_<shortname>" href="https://...">` — 외부지만 dokuwiki 가 prefixed 단축 표기로 만들어 준 링크 (e.g. `interwiki iw_doku` → `dokuwiki>foo` 표기).
- `<a class="urlextern" href="https://...">` — 일반 외부 URL.

둘 다 storage format 그대로 통과 (Confluence 가 보통의 `<a>` 를 수용한다). class 는 정리 단계에서 떨군다.

### 6.4 section-edit 메타 코멘트

각 섹션 끝에 다음 마커가 박힌다:

```html
<!-- EDIT{"target":"section","name":"Formatting Syntax","hid":"formatting_syntax",
         "codeblockOffset":0,"secid":1,"range":"1-472"} -->
```

DokuWiki 의 "이 섹션만 편집" 기능을 위한 메타. Confluence 에는 의미 없고
bs4 의 일부 시리얼라이즈 경로에서 코멘트 내부 JSON 이 가시 텍스트로 새는
사례가 있어 변환기는 **모든 HTML 코멘트를 일괄 제거**한다.

### 6.5 헤딩과 secedit 앵커

```html
<h1 class="sectionedit1" id="formatting_syntax">
  Formatting Syntax
  <a class="secedit" href="?do=edit&id=wiki:syntax&rev=&sectid=1" title="Edit">
    <span>Edit</span>
  </a>
</h1>
```

`<a class="secedit">` 는 제거. `class="sectionedit<N>"` 도 노이즈로 떨어뜨림. `id` 는 보존 (페이지 내 앵커 링크가 가리킴).

### 6.6 자동 생성된 TOC

```html
<div class="toc">…</div>
<div id="dw__toc">…</div>
```

DokuWiki 가 본문 위에 자동 삽입하는 목차. Confluence 는 별도 `<ac:structured-macro ac:name="toc"/>` 를 갖고 있으므로 변환기는 이 div 를 통째로 제거한다.

### 6.7 코드 블록

```html
<pre class="code python">
<span class="kw1">def</span> hello(): …
</pre>
```

- `class="code"` 또는 `class="code <lang>"`
- 내부에 GeSHi 신택스 하이라이트용 `<span>` 들이 들어있으면 그것까지 합쳐 `get_text()` 로 평탄화한다 — Confluence 의 `code` 매크로는 plain text + 별도 language 파라미터를 받으므로 안전.

### 6.8 파일 인용

`<pre class="file">` 는 dokuwiki 의 `<file ...>` 매크로 출력. 변환기는 `code` 와 동일하게 처리.

### 6.9 인라인 포맷팅

`<strong>`, `<em>`, `<em class="u">` (밑줄), `<code>` (monospace), `<del>`/`<s>`, `<sub>`, `<sup>` — 모두 storage format 그대로 보존.

## 7. 라이브 테스트로 발견된 버그

### 7.1 wiki:syntax 한 페이지에서 발견 (CL 52684)

`dev/dokuwiki-local` 인스턴스에서 한 페이지만 돌려도 다음 네 가지가 드러났다.

1. **path-style 내부 링크 미인식.** `userewrite` 가 켜진 인스턴스는 `?id=` 없는 path 형 URL 을 보내 변환기가 external 로 분류. → `data-wiki-id` 와 `wikilink*` class 를 1순위로.
2. **`<!-- EDIT{...} -->` 메타가 텍스트로 누수.** bs4 의 코멘트 핸들링이 일부 경로에서 내부 JSON 을 가시 노드로 만든다. → 모든 HTML 코멘트 제거.
3. **외부 이미지 proxy 가 첨부로 잘못 분류.** `fetch.php?media=https%3A%2F%2F...` 형태가 `media=` 분류에 잡혀 attachments 테이블에 가짜 행을 만들었다. → `media=` 값이 `http(s)://` 로 시작하면 external 로 재분류, `<img src>` 를 실제 URL 로 교체.
4. **재변환 시 FAILED 첨부 잔존.** 이전 run 의 FAILED 행이 정리되지 않아 디버그 루프 마다 attachments 테이블이 누적. → re-convert 가 `status != 'UPLOADED'` 인 행을 모두 정리. UPLOADED 는 보존해 Confluence 가 이미 받은 첨부를 두 번 올리지 않는다.

### 7.2 전체 1569 페이지 풀 트리에서 발견 (CL 52687, 52688, 52689)

`dev up → discover → render → convert` 일괄 실행 후 storage XML 을 grep 으로 감사하면서 발견.

| # | 증상 (corpus 통계)                                     | 원인                                                                                                                                                    | 수정 (CL)                                                                                                                                                |
|---|--------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| 5 | `<script>`, `<link>`, `<form>`, `<meta>` 각 1208 파일에 잔존 | DokuWiki 의 export_xhtmlbody 가 ACL-denied 페이지 또는 일부 플러그인 출력 시 풀 HTML 문서(헤더/푸터 포함)를 토함                                          | `_convert_html_to_storage` 가 `<main id="dokuwiki__content">` 발견 시 그 안의 `<div class="page">` 만 추출. 위험 태그(`script/style/link/meta/noscript/iframe/embed/object/form/head`) 일괄 decompose. `<html>/<body>` 는 unwrap. dokuwiki chrome 도 id/class 단위로 제거 (CL 52687)         |
| 6 | URL-encoded 미디어 경로로 한국어 파일 resolve 실패. e.g. `ride:files:%EB%8F%84%EC%84%A0%EC%82%AC.gpx` | `_categorize_href` 가 `parsed.path` 를 raw 그대로 사용. `parse_qs` 는 자동 디코딩하지만 path 는 안 함                                                       | `urllib.parse.unquote(parsed.path)` 후 prefix 매칭. `_href_to_doku_id_via_path` 도 동일 (CL 52687)                                                       |
| 7 | `plugin_include__<page-id>` 같은 dynamic class 가 166 파일에 잔존 | include 플러그인이 transcluded section 마다 페이지 ID 를 박은 class 부여                                                                                | 노이즈 class prefix 목록에 `plugin_` 추가 (CL 52687)                                                                                                    |
| 8 | ACL 로 anonymous deny 된 네임스페이스가 dev 인스턴스에서 풀 로그인 폼 응답 (1208 페이지) | 호스트의 `acl.auth.php` 가 c:, u:, ride:, lam:, blog:, p:, oh:, j:, g:, gd:, reads:, um:, user: 등 광범위하게 `@ALL=0` (no permission). dev 컨테이너도 동일 conf 사용 | `run.py dev up` 이 clone 직후 `conf/local.php` 의 `useacl` 을 0 으로 패치 → 모든 페이지 anonymous 읽기 허용. 원본 호스트 데이터는 손대지 않음 (CL 52688) |
| 9 | todo 플러그인의 `<input type="checkbox">` 가 191 파일에 잔존. Confluence storage 는 인터랙티브 컨트롤 거부 | dokuwiki todo 플러그인이 인라인 체크박스를 그대로 출력                                                                                                  | 두 모드 변환: `<ul>` 의 모든 직접 `<li>` 가 단일 pure todo 인 경우 `<ul>` 통째를 `<ac:task-list>` 로 치환해 **클릭 가능한 Confluence 체크박스** 로 만든다. 그 외 (mixed/nested) 는 `[x] / [ ]` 텍스트 마커 폴백. 또한 `input/button/select/option/textarea` 도 안전망으로 strip (CL 52689; task-list 변환은 CL 52691) |

### 7.3 누적 통계

- 1569 페이지 디스커버 → 1567 RENDERED + 2 SKIPPED (`pages/.txt` 와 빈 본문의 root `start`).
- 1567 CONVERTED, 실패 0.
- 첨부 10659 DISCOVERED, 140 FAILED (디스크에 미디어 없음 — 정상 데이터 상태).
- 위험 태그 잔존 0건 (`script/form/head/iframe/input/button/select/textarea`).
- URL-encoded 잔존은 외부 medium.com URL 의 8 파일 — 그대로 보존 정책.

## 8. 플러그인 검증

별도 문서 [`docs/plugin-validation.md`](plugin-validation.md) 참고. 요약:

- 활성 플러그인 6개(blog / include / pagelist / tag / todo / wrap) 모두 dev 컨테이너에서 정상 렌더링 확인.
- 변환기가 모든 플러그인 출력을 Confluence storage 호환 형태로 보존.
- 부분 손실 1건: **rss** 는 export 시점 스냅샷만 보존 (자동 갱신 안됨). **todo** 는 대부분 (841 pure `<ul>` 그룹 / 1547 task 항목) Confluence task-list 매크로로 변환되어 **클릭 가능한 체크박스**가 되고, mixed/nested 케이스만 `[x]/[ ]` 텍스트 마커로 폴백.

## 9. 구성요소 변환 매트릭스

DokuWiki 의 각 마크업 요소가 Confluence 로 옮겨질 때 (A) 그대로 통과,
(B) Confluence 매크로/구조로 변환, (C) 부분 변환·시각 효과 손실,
(D) 의도적 누락, (E) 별도 트랙 중 어디에 속하는지 한 곳에 정리한
reference: [`docs/element-mapping.md`](element-mapping.md). 새 dokuwiki
요소가 발견되면 §H 절차로 분류.

## 10. struct 플러그인 데이터 이전 (별도 트랙)

`docs/plugin-validation.md §6` 에서 발견된 고아 struct 데이터를 살아있는
Confluence 데이터베이스로 옮기는 시나리오는
[`docs/struct-migration.md`](struct-migration.md). 채택안 (2026-05-18):
schema → Confluence native Database 우선, 미지원 컬럼 시 Page
Properties + Report 폴백, 최후 단순 표 스냅샷. 새 서브커맨드
`struct-discover/convert/upload/status` + `struct_schemas` /
`struct_columns` / `struct_rows` / `struct_references` 테이블.
1,213 활성 row (4 schema) 가 대상.

## 11. 실제 마이그레이션 런북

Confluence 키 도착 후 라이브 업로드를 안전하게 수행하는 단계별 절차와
롤백 대응은 [`docs/runbook.md`](runbook.md) 에 별도 정리. 준비물
체크리스트(이메일/API 토큰/공간 키/루트 페이지 ID), 사전 점검 명령
(`status`/`report`/`lint`), 소규모 검증(`upload --only`) → 전체 →
`rewrite-links` 순서, FAILED 대응, 롤백 SQL.

## 12. 다음 단계

- `upload --dry-run` 으로 트리 / stub / 첨부 예상치 출력 확인.
- Confluence 공간/루트 페이지 결정 → 실제 업로드 → `rewrite-links` 로 placeholder 해결.
- 새 플러그인이 추가되면 `docs/plugin-validation.md` §6 절차로 재검증.
