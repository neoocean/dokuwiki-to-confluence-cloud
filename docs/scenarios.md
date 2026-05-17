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
- 과거 리비전(`attic/`) 마이그레이션.
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
