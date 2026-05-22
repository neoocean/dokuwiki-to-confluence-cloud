# 라이브 마이그레이션 결과

본 문서는 운영 로그로 *날짜별 섹션* 으로 누적된다. 최신 통계는 마지막
day 의 §0 표. 과거 로그는 그대로 보존.

---

# Day 5 — 2026-05-20 (변환기 6종 추가 + 1565 페이지 헤더 재포맷 + 508 페이지 갱신)

CL 53121-53269. *대규모 변환기 보강 + 라이브 적용 사이클*.

## Day 5 §0 라이브 결과 한 줄

- **revision 헤더 재포맷**: 1,565 페이지 (3 <p> → panel + shift+enter, idempotent)
- **변환기 추가 6종 + 라이브 적용**: monthcal (105 매크로 → 정적 캘린더 표) /
  Google Calendar iframe (`u:lam:calendar`) / youtube fallback / encryptedpasswords
  `<decrypt>` → expand + inline code (11건, cipher 100% 보존) / todo 체크박스
  강화 (mixed/inline → task-list)
- **plugin-scan 신규 도구**: 7 미설치 식별 → 4 자동 설치 (encrypt / iframe /
  tagging / discussion). 1569 페이지 데이터 기반.
- **전체 convert --force + upload**: 1567 변환 / 508 updated / 1167 skipped
  (content_hash 동일) / 0 failed (단 큰 본문 1건 별도 `u:neoocean:j:2019:09:08`)
- **신규 도구**: decrypt (AES-256-CBC 복호화) / link-check (placeholder 잔존
  + unresolved title + external HEAD) / history-rewrite-headers (idempotent
  헤더 형식 변경)
- **plugin 자동 설치 sanity check**: plugin.info.txt / syntax.php 등 부재 시
  거부 — pld-linux 의 RPM spec wrapper 가 정상 plugin 으로 잘못 잡히던 버그 fix.

## Day 5 §1 신규 변환기 (run.py)

| 함수 | 역할 | 영향 |
|------|------|------|
| `_convert_monthcal_fallback` | monthcal 매크로 미설치 fallback → 정적 캘린더 `<table>` (요일 헤더 + 날짜 셀 + namespace dwc-link) | 105 매크로 / 10+ 페이지 |
| `_convert_youtube_fallback` | `{{youtube>VID}}` 깨진 fallback → Confluence iframe (youtube embed) | 본 인스턴스 0건 (사용자 데이터엔 정상 hyperlink) |
| `_convert_google_calendar_iframe` | Google Calendar `<iframe>` 또는 escape 된 텍스트 → Confluence iframe 매크로 | 1 페이지 |
| `_convert_encrypted_passwords` | (1) plugin 미활성 escape `<decrypt>...</decrypt>` raw HTML pre-process (2) plugin 활성 `<span class="encryptedpasswords" title="cipher">` → expand + inline code (cipher 보존) | 11건 / 6 페이지 |
| `_preprocess_encrypted_passwords` | raw HTML 단계 escape `&lt;decrypt&gt;...&lt;/decrypt&gt;` → expand + code 매크로 (multi-line cipher 안 inline 마크업 잔재 회피 + `</decrypt>` 뒤 자동 줄바꿈) | (위 통합) |
| `_convert_todos` 강화 | mixed ul / li 안 todo + 텍스트 / inline → task-list (single-task) / 또는 unicode 글리프 | 5,285 `<todo>` 매크로 영향 |
| `_revision_header(fmt=)` | 8 형식 (none/panel/info/note/tip/warning/quote/table/paragraphs) — 기본 panel + shift+enter | 1,565 페이지 |

## Day 5 §2 신규 명령

| 명령 | 역할 |
|------|------|
| `plugin-scan` | 페이지 본문 스캔 → 매크로/태그 사용 → 미설치 플러그인 식별. `--install` 로 자동 설치 (PLUGIN_DOWNLOADS 매핑 22종) |
| `decrypt` | encryptedpasswords cipher 복호화 (OpenSSL AES-256-CBC + EVP_BytesToKey MD5). `-p PASS --cipher / --page / --confluence-id` |
| `link-check` | Confluence 측 페이지의 (1) dwc-link 잔존 (2) unresolved page title (3) external URL HEAD |
| `history-rewrite-headers` | 이미 업로드된 페이지의 revision 헤더만 새 형식으로 재PUT. 기존 모양 자동 감지·strip |
| `compare-publish` | 주요 페이지의 DokuWiki/Confluence 양측 풀-페이지 스크린샷을 캡쳐해 Confluence 루트 페이지 하위에 비교 갤러리 발행/갱신. 10 카테고리 자동 selection (메인/iframe/encrypt/표/이미지/info·note·warning/매크로 다양/코드/대용량) + per-category count (`sample/8`) 로 `--sample 20` 같은 큰 갤러리도 지원 |

## Day 5 §3 결정적 발견

- **Confluence Cloud Database API**: 컬럼/row endpoint 미공개 (Day 4 발견 재확인)
- **plugin 자동 설치의 잘못된 패키지 식별**: pld-linux/dokuwiki-plugin-encryptedpasswords
  는 RPM spec 만 — 정상 dokuwiki plugin 아님. `_dev_install_plugins` 에 sanity
  check 추가 (plugin.info.txt / syntax.php 등 marker 파일 필수)
- **encryptedpasswords cipher 형식**: gibberish-aes.js + OpenSSL AES-256-CBC +
  EVP_BytesToKey(MD5, 1 iter) — Python `decrypt_encryptedpasswords` 구현으로
  복호화 가능 (round-trip 테스트 10건 통과)
- **Convert --force 버그**: UPLOADED status 페이지 누락 → 변환기 변경 후 재변환
  안 됨. fix 적용.

## Day 5 §4 테스트

164 → 169+ 통과. 신규 케이스:
- test_revision_header.py (21): 8 형식 + strip 회귀
- test_calendar.py (14): monthcal 5 / Google Calendar iframe 4 / encrypt 5
- test_decrypt.py (10): KDF + round-trip + invalid password
- test_link_check.py (4): 정규식 패턴
- tests/conftest.py: pytest 공통 fixture (project_root / convert / make_dokuwiki)

## Day 5 §5 코드 정리

- 모듈 docstring 에 코드 섹션 인덱스 (`# §` anchor)
- 섹션 헤더 표준화 (20곳)
- `_add_confluence_creds_args` / `_add_confluence_space_args` 로 argparse
  boilerplate -38%
- **DDL 통합** (CL 53278): `MAIN_SCHEMA_DDL` / `HISTORY_SCHEMA_DDL` /
  `STRUCT_SCHEMA_DDL` 상수 분리, `db_init()` 가 단일 진입점 (history/struct
  도 메인 db_init 에서 동시에 생성) — schema 정의 산재 → 한곳에 모임
- **build_parser 9 도메인 helper 분리** (CL 53279): ~460줄 거대 함수 →
  25줄 orchestrator + 9개 `_build_*_subcommands()` (pipeline / history /
  struct / oversized / audit_report / verify / dev / tool / wizard). 새 명령
  추가 위치가 helper 명으로 자명해짐
- **cmd_history_upload / cmd_struct_upload 본체 분리** (CL 53281):
  - history-upload (100→43): `_history_upload_select_pages` + `_replay_one_page`
  - struct-upload (280→57): `_probe_database_api` + `_select_schemas` +
    `_snapshot_schema` + `_indexed_schema`
- **return type hint 보강** (CL 53282): ast 검사로 누락 14개 발견 → 모두
  명시 (`_confluence_session`, `_request_with_retry`, `_struct_resolve_*`,
  `_struct_post_page`, `_diff_page`, `_vc_pil_open`, `_wizard_get`, q1, etc).
  `from __future__ import annotations` 활성 → 외부 import 없는 forward ref 안전
- 전체 164 tests 무회귀, 모든 helper 분리는 순수 구조 리팩토링 (동작 동일)

## Day 5 §6 비교 갤러리 + 후속 issue fix 사이클 (Day 5 후반)

사용자 검토 중 발견된 4개 issue 를 한 사이클로 fix:

| issue | 원인 | fix |
|-------|------|-----|
| 비교 갤러리의 Confluence 측 이미지 깨짐 | `/wiki/download/attachments/...` 는 OAuth 만 받음 (Basic 거부) + 캡쳐 ctx 인증 헤더 누락 | `_compare_rewrite_attachment_urls` (v1 endpoint 으로 src rewrite) + ctx_c 의 `extra_http_headers={"Authorization": ...}` + `<base href>` 추가 |
| 비교 갤러리의 Confluence 측 빈 페이지 | `wait_until="domcontentloaded"` + 짧은 wait — 미완성 렌더에서 캡쳐 | `networkidle` + `wait_for_timeout(1500ms)` |
| BnSR 같은 거대 페이지 (이미지 100+) 가 110MB PNG | 풀-페이지 캡쳐가 모든 콘텐츠 다 포함 | 페이지 scrollHeight 측정 후 viewport 동적 조절 → 12000px clip (첨부 100MB 한도 + 갤러리 비대화 회피) |
| lightsail 의 decrypt cipher 가 escape 텍스트로 노출 | cipher 안 우연히 형성된 DokuWiki 마크업 잔재 (`<em>`, `<u>` 등) 로 텍스트 노드 split → walker 매치 실패 | raw HTML 단계 pre-process `_preprocess_encrypted_passwords` 추가 (bs4 파싱 전에 직접 storage 매크로 치환). 또한 plugin 활성 시 `<span class="encryptedpasswords" title="...">` 도 별도 변환 |
| u:lam:calendar 의 `<html><iframe>` 가 escape 텍스트로 노출 | DokuWiki Jack Jackrum (2023-04) 부터 `<html>...</html>` core 매크업 제거 | saggi-dw/dokuwiki-plugin-htmlok 설치 + plugin conf 활성. (encrypt 디렉터리 → encryptedpasswords rename 도 같이) |

비교 갤러리 (cid=2526937148) 8 페이지 → 20 페이지 갱신, 첨부 40/40 OK,
이미지·표·iframe 모두 정상.

## Day 5 §7 audit-3way 시나리오 구현 + 비교 갤러리 5종 추가 fix + 로테이션

사용자 발견 + 후속 검토 사이클. 코드 6 CL 누적.

### audit-3way 구현 — 1675 페이지 정수 검사

`docs/3way-audit.md` 시나리오의 P1+P3 구현 (CL 53522, 53529).

| 단계 | violation 페이지 | 비율 |
|------|------------------|------|
| 초기 audit | 149 | 8.9% |
| S1 wrap regex 보강 | 114 | 6.8% |
| S2 known plugin 필터 | 11 | 0.7% |
| youtube VID 변환기 + intent 화이트리스트 | **7** | **0.42%** |

- converter.high / medium: **모두 0** (변환기 완벽)
- 변환기 fix 자율 (4 페이지): `<p>VID</p>` 단독 paragraph → Confluence
  iframe macro (`_convert_youtube_fallback` 의 `_YOUTUBE_VID_ONLY_RE` 케이스)
- 남은 7 페이지: source 측 plugin 누락 / 환경 issue (NFD 정규화 등)
- 신규 22 unit tests (`tests/test_audit_3way.py`)

### 비교 갤러리 5종 추가 fix

| issue | 원인 | fix |
|-------|------|-----|
| 빈 PNG 캡쳐 (b:edit:start 등 작은 콘텐츠) | full_page=False + set_viewport_size(scrollHeight) → 빈 영역 노출 | viewport clip + PIL ImageChops.difference trim (CL 53638/53644) |
| 거대 페이지 OOM hang (u:oh:모든_기록) | full_page=True 가 13MB+ PNG 메모리 폭증 | viewport 12000px clip + PIL skip threshold 5MB |
| iframe placeholder 페이지 빈 박스 | view body API 가 iframe macro 를 빈 placeholder 로 응답 | placeholder 위에 노란 안내 박스 injection (CL 53638) |
| 같은 filename 첨부 갱신 미작동 (한국어 페이지 32KB 빈 PNG 잔존) | POST /child/attachment 가 400 "same file name" → ok 마킹만 + 갱신 안 함 | 2-step upload: 400 시 GET 으로 aid 조회 후 POST /child/attachment/{aid}/data (CL 53828) |
| 95 이미지 다수 페이지 모두 깨짐 (u:lam:j:2019:09:27 검은사막) | view body img src=`download/thumbnails/...` (OAuth-only) — 기존 rewrite 가 `attachments` 만 매치 | rewrite 함수 확장: `(src\|data-image-src)="...download/(attachments\|thumbnails)/..."` + srcset 별도 처리 (CL 53840) |

### `--rotate` 로테이션 옵션 (CL 53845)

같은 페이지 반복 발행 한계 해소:
- `state.db meta.compare_publish_history` 누적 (개행 구분 doku_id list)
- `--rotate`: 이전 발행 페이지 selection 제외 → 매번 새 batch
- `--reset-rotation`: 이력 초기화
- `--no-track`: 발행 후 이력에 추가 안 함 (테스트)
- macOS APFS NFD 한국어 doku_id ↔ NFC seed mismatch 대응 — 양측 NFC 정규화

비교 갤러리 20 페이지 새 batch 발행 — start / 게임구매내역 등 *이전 20*
제외, blog:draft:start / u:neoocean:c:pt03b / u:lam:출퇴근기록 / wiki:til
등 *새 20* 페이지 (rotation 이력 총 40).

---

# Day 4 — 2026-05-19 (struct → native+properties 라이브 적용)

snapshot 페이지 4개로만 있던 struct 데이터를 *Confluence 측에서도
"데이터베이스처럼" 동작* 하는 형태로 재구성. 각 schema 당:

1. **빈 Confluence Database 객체** (dwc-struct-{tbl}) 를 공간 루트에 생성
   → `struct_schemas.confluence_db_id` 저장. (API 한계로 컬럼/row 입력
   불가 — 추후 API 가용 시 자동 채움 코드 경로 준비됨.)
2. **인덱스 페이지** 본문을 교체 — 기존 snapshot 페이지 4개의 page id 를
   재사용. 본문: schema 메타 + Confluence Database 객체 webui 링크 + 컬럼
   table + Page Properties Report (cql `label = "dokuwiki-struct-{tbl}"`).
3. **row 별 자식 페이지** 1,213개 — `details` 매크로 본문 + 라벨
   `dokuwiki-struct-{tbl}`. 셀은 컬럼 타입별로 렌더 (Wiki→ri:page,
   Media→ri:attachment/ac:image, Url→`<a>`, Date→`<time>`, Decimal/Text→text).
4. **bound 페이지 임베드** — brevet_event(col23 Wiki)/brevet_course(col2
   doku_id)/brevet_uri_cppage(col1 doku_id) 의 bound 페이지 본문 끝에
   "관련 struct 데이터" 패널 추가. 마커 `<!-- struct-embed:start -->` 기준
   idempotent 교체.

## Day 4 §0 struct 트랙 종료 통계

| 단계 | 결과 |
|------|------|
| `struct-upload --probe` | Database 생성 200 OK / 컬럼·row endpoint 7개 모두 500 → native A 모드는 *쉘만* |
| `struct-convert --mode native --reconvert` | 4 schema, 1,213 row XML 재생성 (~1초) |
| `struct-upload --mode native` 라이브 | 4 schema × Database (4 빈 쉘) + 4 인덱스 PUT + **1,213 row POST + 1,213 label, 0 실패** (~25분, ~50 row/min) |
| `struct-upload --mode native --index-only` (URL 보정 재돌림) | 4 인덱스 PUT (8초) |
| `struct-embed-on-bound-pages` 라이브 | **208 bound page PUT** + 5 unresolved skip (DokuWiki 페이지 미존재) — `<h2>관련 struct 데이터</h2>` sentinel 로 idempotent 보장 |

## Day 4 §1 데이터 모델

```
공간 루트
├─ dokuwiki struct: brevet_course (page, 인덱스, 기존 snapshot id 재사용)
│   ├─ brevet_course: 출발  (row 페이지 ×744, 라벨 dokuwiki-struct-brevet_course)
│   ├─ brevet_course: 일죽
│   └─ …
├─ dokuwiki struct: brevet_event  (×106)
├─ dokuwiki struct: brevet_place  (×98)
├─ dokuwiki struct: brevet_uri_cppage  (×265)
└─ dwc-struct-{tbl}  (빈 Database 객체 ×4, sibling — webui 페이지로 표시)
```

bound 페이지 (`b:2019-s200d-1`, `b:2019-s200i-1`, …) 본문 끝에 `관련
struct 데이터` 섹션이 들어가 — brevet_event row 링크 + brevet_course
체크포인트 링크 + Page Properties Report (CQL 라벨 매칭) 가 함께 표시.

## Day 4 §2 API 한계 발견 (struct-migration.md §3.1 갱신됨)

`struct-upload --probe` 결과 — Confluence Cloud v2 REST 의 Database API 는
**create/get/delete 의 3가지만 공개**. 컬럼 정의나 row 입력 endpoint 가
존재하지 않는다 (시도한 7개 모두 500). storage format 의 `<ri:database>`
임베드도 거부. 따라서 *데이터*는 Page Properties 매크로 조합으로 들어가고
*Database 객체*는 그 자체로 빈 쉘로만 존재. Atlassian 이 후속 API 추가
시 동일 코드 경로 (mode=native) 가 그대로 풀 native 마이그레이션을 수행
가능 — `confluence_db_id` 가 schema 별로 이미 채워져 있음.

## Day 4 §3 코드 적용 (CL 53122+)

| 컴포넌트 | 변경 |
|----------|------|
| `_struct_render_cell` | DokuWiki class → storage XML cell (Wiki/Media/Url/Date/Decimal/Text/Dropdown) |
| `_struct_resolve_page` / `_struct_resolve_attachment` | 메인 파이프라인의 pages / attachments 테이블에서 lookup, 기본명 suffix 폴백 |
| `_struct_row_to_details_macro` | 단일 row → `<ac:structured-macro ac:name="details">` 본문 |
| `_struct_row_title` | 첫 Text 또는 name='code/name/title' 우선, fallback `{tbl}#{pid}` |
| `_struct_build_index_xml` | 인덱스 페이지 본문 — Database 안내 박스 + 컬럼 table + Page Properties Report |
| `cmd_struct_convert` | snapshot/properties/native 3모드 + `--reconvert` |
| `cmd_struct_upload` | `--mode {auto,native,properties,snapshot}` + `--no-native-shell` + `--only-tbl` + `--probe-keep` |
| `cmd_struct_embed_on_bound_pages` | bound 페이지에 "관련 struct 데이터" 패널 |
| `tests/test_struct.py` | 27 케이스 (struct 18 + visual-audit Phase 3 자동 신호 9) |

## Day 4 §4 outstanding

| 항목 | 상태 | 다음 |
|------|------|------|
| Atlassian Database API 컬럼/row | 미지원 | 6개월 간격 재probe |
| brevet_place page binding | 부재 (장소명 free text) | 별도 마스터 페이지 1개로 묶음 — Day 5 후보 |
| 컬럼 label 부재 (struct config 의 `label.<lang>` 비어있음) | colN 로 표시 중 | manual 라벨 매핑 JSON 추가 가능 |
| struct row 의 multi-row 빈 cell 표시 (brevet_uri_cppage 일부) | 빈 row 그대로 | 빈 row strip 옵션 후보 |

---

# Day 3 — 2026-05-19 새벽 (history 트랙 라이브 실행)

history-render → history-convert → history-upload 본격 실행. 5 라운드의
PUT replay 끝에 약 50% 회복. *큰 페이지의 뒤쪽 rev* 가 Confluence 본문
parsing 한도로 영구 거부되는 패턴 관측.

## Day 3 §0 history 트랙 종료 통계

| 단계 | 결과 |
|------|------|
| `history-render` 37,947 리비전 시도 | 37,281 RENDERED / 666 SKIPPED (404 또는 빈 본문) / **0 FAILED** |
| `history-convert` 37,281 → storage XML | 37,279 CONVERTED / 2 FAILED (변환 오류) — ~1시간 cpu-bound |
| `history-upload` 5 라운드 누적 | **18,729 UPLOADED / 18,503 CONVERTED 잔존 / 50 FAILED** |

라운드별 회복 (resume-safe; 매 라운드 FAILED → CONVERTED reset 후 재시도):

| 라운드 | 시작 시각 | 처리 페이지 | rev_ok | rev_fail | 누적 UPLOADED | 누적률 |
|--------|-----------|-------------|--------|----------|---------------|--------|
| 1 | 00:58 | 1,566 | 10,433 | 284 | 10,433 | 28% |
| 2 | 04:00 | 284 | 3,829 | 128 | 14,262 | 38% |
| 3 | ~05:00 | 128 | 2,228 | 76 | 16,490 | 44% |
| 4 | ~05:38 | 76 | 1,044 | 58 | 17,534 | 47% |
| 5 | ~06:02 | 58 | 1,195 | 47 | 18,729 | **50%** |

페이지 단위 종료 분포:

- 완전 처리 (모든 rev UPLOADED): **1,520 페이지** (대부분)
- 부분 처리 (일부 rev 만 UPLOADED): **46 페이지** (큰 일지 페이지들)
- 0 처리 (한 rev 도 UPLOADED 안 됨): **63 페이지** (large_body_fallback `u:neoocean:2020` 의 2,895 rev + `p:start` 649 rev + 작은 페이지들 한두 rev)

## Day 3 §1 큰 페이지 영구 fail 패턴

같은 페이지의 *처음 N rev* 는 통과하지만 그 뒤 rev 가 거부되는 패턴.
원인 추정: dokuwiki 의 매 edit 마다 본문이 누적되어 *후반 rev*
storage XML 이 매우 크고 복잡해짐 → Confluence 본문 parsing 한도 초과
(`no resp`).

| 페이지 | 총 rev | UPLOADED | CONVERTED 잔존 |
|--------|--------|----------|-----------------|
| `u:lam:2019` | 5,531 | 20 | 5,509 |
| `u:lam:2020` | 3,535 | 125 | 3,409 |
| `u:neoocean:2020` (large_body_fallback) | 2,895 | 0 | 2,895 |
| `u:neoocean:2019` | 2,195 | 125 | 2,067 |
| `u:oh:start` | 1,102 | 264 | 837 |
| `p:start` | 649 | 0 | 649 |

가장 큰 페이지의 *처음 ~5%* 만 보존. 나머지 95% 의 history 는 시작
지점(메인 파이프라인이 옮긴 최신 본문) + 이미 옮긴 일부 historic rev
로 한정. *Confluence 의 본문 한도* 가 본질적 제약이라 더 시도해도
같은 위치에서 거부됨.

## Day 3 §2 누적 통계 (Day 3 종료)

| 항목 | 값 | Day 2 대비 |
|------|----|----|
| 메인 페이지 UPLOADED | 1,675 / 1,675 (100%) | 동일 |
| 메인 첨부 UPLOADED | 10,732 | 동일 |
| **history rev UPLOADED** | **18,729** / 37,279 (50%) | +18,729 (라이브 시작) |
| history page 완전 처리 | 1,520 / 1,629 | (라이브 시작) |
| history page 부분 처리 (큰 페이지) | 46 | (라이브 시작) |
| history page 0 처리 | 63 (`u:neoocean:2020` + `p:start` + 기타) | (라이브 시작) |
| struct 페이지 | 4 (1,213 rows) | 동일 |

## Day 3 §3 코드 적용

이번 라운드는 *추가 코드 변경 없음*. CL 52882 의 history pipeline
구현이 그대로 사용됨. resume-safe (history_meta.last_replayed_rev_ts)
+ 페이지별 fail 시 break + FAILED → CONVERTED 수동 reset 후 재시도
패턴이 5 라운드 동안 의도대로 동작.

## Day 3 §4 outstanding

| 항목 | 상태 | 권장 대응 |
|------|------|----------|
| 큰 페이지의 후반 rev (18,503 CONVERTED 잔존) | **영구** | 메인 페이지 본문은 이미 OK; history 의 *과거 시점* 만 일부 손실. 후속 추가 가능 — 옵션: (a) 본문 압축 + 다시 시도, (b) zip 첨부로 보존, (c) 그대로 기록. |
| `u:neoocean:2020` (large_body_fallback) 의 2,895 rev | **영구** | 본 페이지는 이미 skeleton + zip 첨부 패턴 적용. history 는 zip 안에 보존 가능 (현 구현 미포함). |
| `p:start` 649 rev (0 처리) | 미진단 | 별도 확인 권장 — 콘텐츠 패턴 거부 가능 |

## Day 3 §5 다음 단계 (Day 4 후보)

- 사후 정리: API 토큰 revoke, `.secrets/confluence.env` 갱신, 로그 archive.
- 휴지통 1,465 페이지 *영구 purge* 또는 30일 자동 만료 결정.
- 큰 페이지의 history 추가 회복 시도 (옵션) — Day 3 결과로 *영구 제약*
  확인됐으므로 *추가 큰 코드 없이* 추가 시도는 무의미.
- 사용자가 마이그레이션 결과 시각 확인 + 정상 운영 모드 (검색, 편집, 공유) 시작.

---

# Day 2 — 2026-05-19 (별도 트랙 적용 + 공간 정리)

후속 follow-up 작업 9건 (#4~#12) 자율 진행 + 별도 트랙 일부 실행 +
공간 안 비-마이그레이션 페이지 1,465건 휴지통 정리.

## Day 2 §0 누적 통계 (2026-05-19 종료 시점)

| 항목 | 값 | 변화 |
|------|----|----|
| pages UPLOADED | **1,675 / 1,675 (100%)** | +1 ← `u:neoocean:2020` C-mode 회복 |
| attachments UPLOADED | **10,732** | +119 (이 페이지의 자식 첨부) |
| attachments OVERSIZED → note 박스 | 10 | +1 발견, 9 B-mode 적용 (5 페이지 v↑) |
| attachments FAILED (missing src) | 143 | 동일 |
| links resolved / unresolved | 5,180 / 1,317 | 동일 |
| large-body fallback (skeleton + zip 첨부) | 1 | +1 ← `u:neoocean:2020` |
| struct schema UPLOADED | 4 / 5 | +4 (snapshot 모드 라이브) |
| struct rows 옮겨감 | 1,213 | +1,213 (4 페이지의 표) |
| 휴지통 (30일 회복 가능) | 1,465 페이지 | +1,465 (공간 정리) |
| 공간 안 current 페이지 | 1,680 | 1,679 (마이그레이션 트리) + 1 (root) |
| history-render | 진행 중 (3,813 / 37,947 ~10%) | start 22:37 |

## Day 2 §1 적용된 follow-up (CL 52881, 52882)

| # | 작업 | 결과 |
|---|------|------|
| #5 | OVERSIZED 9건 → B-mode note 박스 | 5 페이지 v↑ (rewrite-oversized 라이브). 본문에 파일명 + 크기 + 백업 위치 안내 박스 |
| #9 | `/tag/<value>` 링크 → Confluence page label | 변환기에서 tag 값 추출 → meta `page_tags:<id>` 저장 → upload 후 `POST /rest/api/content/{id}/label` 적용. tag 값 sanitize (lowercase + alphanumeric + 한글) |
| #10 | audit 분류기 정밀화 | 새 합산 카테고리 `link_total` / `task_total` (변환기 reclassification noise 흡수). per-category critical 완화. |
| #11 | `wrap_*` layout 잔여 클래스 | NOISE_CLASS_PREFIXES 에 `wrap_` 추가 (의미 클래스는 매크로 변환기가 먼저 처리) |
| #12 | 풋노트 anchor (li/sup id) Confluence 에서 제거됨 | `_convert_footnotes` 가 `<ac:structured-macro ac:name=anchor>` 매크로를 fn__N / fnt__N target 으로 삽입. 양방향 jump 동작 |
| #4 | 큰 페이지 1건 (`u:neoocean:2020`) | C-mode: skeleton info 박스 + 원본 storage XML zip 첨부. 페이지 + 119 자식 첨부 회복 |
| #6 | history pipeline (render/convert/upload) 구현 | history-render 진행 중. convert/upload 코드는 준비 — resume-safe, version.message 에 `DokuWiki rev <ts>` |
| #7 | struct pipeline (convert/upload) 구현 | snapshot 모드 라이브 적용 — 4 schema × 1 페이지. properties / native 모드는 stub |
| #8 | `--users-map` JSON 매핑 flag | `_load_users_map` + `_format_user` (mapped → `<ri:user account-id>` link; unmapped → 텍스트). history-upload 에서 사용 |

## Day 2 §2 라이브 적용 상세

### 2.1 struct snapshot 4 페이지 (push=4, fail=0)

| schema | rows | Confluence page |
|--------|------|-----------------|
| brevet_course | 744 | 2518882818 |
| brevet_event | 106 | 2518720467 |
| brevet_place | 98 | 2518720487 |
| brevet_uri_cppage | 265 | 2518882838 |
| test (빈) | 0 | SKIPPED |

각 페이지 본문 = `<h1>` + 짧은 설명 + 전체 row 를 `<table>` (header + N 행).

### 2.2 OVERSIZED 첨부 9건 → 5 페이지 note 박스 (push=5, fail=0)

* 영향 페이지: 5건 (각 1-9 OVERSIZED 첨부)
* 본문 안 `<ri:attachment>` reference → `<ac:structured-macro ac:name="note">` 메타 박스 치환
* 박스 내용: 파일명 + 크기(MB) + "Confluence 100MB 한도" 안내 + 호스트 P4 백업 위치

### 2.3 큰 페이지 C-mode 적용

`u:neoocean:2020` (448KB storage / 1,971 li / 495 dwc-link placeholder) —
Confluence 가 본문 POST/PUT 모두 `no resp` 로 거부. C-mode:

1. `rewrite-oversized-pages` 가 skeleton 본문으로 페이지 생성 (2519047777)
2. 원본 storage XML 을 `<doku_id>.xml.zip` 으로 압축 → 첨부 업로드
3. `state.db` 메타에 `large_body_fallback:<doku_id>` 마킹
4. 이후 `upload --only` 가 본문 PUT 은 skip + **120 자식 첨부 정상 업로드** (119 ok / 1 추가 OVERSIZED 발견)
5. `rewrite-oversized` 도 large_body_fallback 페이지 자동 skip

### 2.4 루트 페이지 대시보드 (v2)

`dokuwiki-migration` (root 2519826441) 의 본문 281KB 로 갱신:

* 통계 info 박스 (페이지/첨부/링크/struct/history/대용량 페이지 카운트)
* `children` 매크로 (depth 2 자동 자식 트리)
* struct 4 페이지 직접 링크
* `expand` 매크로 안 namespace 별 그룹 (14 ns) — 각 그룹 펼치면 그 안 모든 마이그레이션 페이지 ac:link
* 별도 트랙 안내 (history / OVERSIZED / 큰 본문 페이지)

### 2.5 공간 'dokuwiki' 비-마이그레이션 페이지 1,465건 휴지통 이동

분석 단계에서 **위험 발견 + 해결**:

* Confluence `/api/v2/pages/{id}/descendants` API 가 *불완전* (385/1,679). 그대로 사용했다면 마이그레이션 트리의 자식 1,294 페이지가 *잘못 삭제* 됐을 것
* **수정**: state.db 를 ground truth 로 사용 (모든 `confluence_page_id` + `snapshot_page_id` + `root_page_id` 합산 = 1,680). 공간 전체 페이지 (3,145) 와 차집합 → 1,465.
* 안전 점검: `keep ∩ to_delete = 0` 확인 후 진행
* 깊이 desc 정렬 (자식부터 — Confluence DELETE 는 자식 cascade 안 함)
* DELETE → 휴지통 (30일 회복 가능)
* 결과: **1,465 ok / 0 fail**. 공간 안 current 페이지 1,680 (정확히 마이그레이션 트리 + root)

대다수가 id prefix `2304*` / `2305*` 대역 — 이전 마이그레이션 시도 잔재. 우리 작업은 `2517*-2520*` 대역.

## Day 2 §3 코드 변경 (CL 52881 + 52882)

| CL | 신규 |
|----|------|
| 52881 | `cmd_rewrite_oversized` (B-mode 자동화). `_apply_page_labels` (v1 label API). `_convert_footnotes` 에 anchor 매크로 삽입. `<a rel=tag>` 추출 + `page_tags:<id>` 메타. `_compare_features` 의 `link_total` / `task_total` 합산. `NOISE_CLASS_PREFIXES` 에 `wrap_`. `_convert_html_to_storage` 5-tuple 반환 (page_tags 추가). 22 unit tests still pass. |
| 52882 | `cmd_rewrite_oversized_pages` (C-mode skeleton + zip). `cmd_history_render/convert/upload` (37k revision pipeline; resume-safe). `cmd_struct_convert/upload` (snapshot/properties/native). `_load_users_map` / `_format_user` (`--users-map` JSON). `_revision_header` (note 매크로). `docs/oversized-pages.md` 신규. 서브커맨드 13 → **22** (4 history + 4 struct + 2 rewrite). 22 unit tests still pass. |

## Day 2 §4 outstanding (이전 Day 1 §5 이후 변화)

| 이전 § | 항목 | 상태 |
|--------|------|------|
| 5.1 | 큰 페이지 1건 | **해결** (C-mode 적용; §2.3) |
| 5.2 | OVERSIZED 9건 | **해결** (B-mode 적용; §2.2) |
| 5.3 | audit 분류기 정밀화 | **해결** (#10; §1) |
| 5.4 | tag → label 매핑 | **해결** (#9; §1) |
| 5.5 | history / struct 별도 트랙 | **부분 해결**: struct snapshot 라이브 (§2.1). history-render 진행 중. |

### 새 outstanding

* **history-render 완료 대기** → history-convert (~5분) → history-upload (~37k PUT, 하룻밤 잡; resume-safe).
* **dokuwiki tag 가 Confluence label 로 적용된 것 사용자 시각 확인 권장** — UI 의 페이지 라벨 표시.
* **휴지통 1,465 페이지 영구 삭제 여부** — 30일 후 자동 또는 즉시 purge. 본 마이그레이션 결과와 별개라 사용자 결정.

---

# Day 1 — 2026-05-18 (첫 라이브 실행)

자체 운영 중인 DokuWiki 의 첫 라이브 마이그레이션 실행 결과 기록. 본
문서는 *what happened*, *what landed in Confluence*, *what broke and
how it was fixed* 를 한 곳에 모아 추후 재실행/유지보수에 참고하기
위한 운영 로그다. 익명화: 특정 페이지/파일명은 일반화된 표현으로
대체.

## 1. 작업 개요

| 항목 | 값 |
|------|----|
| 시작 시각 | 2026-05-18 11:40 (현지) |
| 라이브 업로드 완료 | 2026-05-18 14:48 |
| 첫 audit 완료 | 15:17 |
| 4개 변환기 fix + 재업로드 + 두 번째 audit | 15:41 |
| 두 번째 rewrite-links + 세 번째 audit | 15:56 |
| 총 실시간 | 약 4시간 16분 |
| 자격증명 | API 토큰 기반 Basic auth (실측 후 revoke 예정) |
| 대상 공간 | `dokuwiki` (전용 임시 공간, space_id=2304968223) |
| 대상 루트 페이지 | `2519826441` ('dokuwiki-migration') |

## 2. 최종 통계 (`run.py status` / `report` / `audit` 출처)

### 2.1 페이지

| 상태 | 카운트 | 비고 |
|------|--------|------|
| UPLOADED | **1,674** | Confluence 페이지 생성/갱신 완료 |
| FAILED | 1 | 단일 일지 페이지 (§5 참조) |
| DISCOVERED | 1 | `pages/.txt` dot-file (정상 건너뜀) |
| **DISCOVER 합계** | **1,569** | 원본 `pages/*.txt` 개수 |
| **자동 stub** | **+107** | namespace start 누락 → 자동 placeholder |
| **promoted SKIPPED** | **+1** | 빈 본문이지만 자식이 있는 chain parent |
| **CONVERT/UPLOAD 합계** | **1,675** | |

### 2.2 첨부 (Confluence attachments API)

| 상태 | 카운트 | 비고 |
|------|--------|------|
| UPLOADED | **10,613** | 정상 업로드 |
| DISCOVERED | 120 | FAILED 페이지의 자식 (해당 페이지 미생성으로 미업로드) |
| FAILED | 143 | 호스트 디스크에 missing 미디어 (정상 데이터 상태) |
| OVERSIZED | 9 | 100MB 초과 — `oversized-attachments.md` 별도 트랙 |

### 2.3 링크 (S7 rewrite-links 결과)

| 상태 | 카운트 |
|------|--------|
| resolved → `<ac:link><ri:page>` | 5,180 |
| unresolved → 평문 격하 | 1,317 (대상 페이지 부재/SKIPPED) |
| **합계 dwc-link placeholder** | 6,497 |

### 2.4 Confluence 매크로 생성 (storage 안)

| 매크로 | 카운트 |
|--------|--------|
| code | ~1,345 |
| panel | ~147 |
| info | ~42 |
| tip | ~41 |
| note | ~31 |
| warning | ~11 |
| task-list | 88 (1,547 task 항목) |
| `[x]/[ ]` 텍스트 마커 (mixed todo) | 189 파일 |

### 2.5 라이브 페이스

분당 약 6-10 페이지. 첨부 헤비 페이지(일지 종류)일수록 느림.
첨부 multipart 가 페이지당 평균 ~6 회 호출 + 페이지 본문 PUT 1 회 →
시간이 대부분 첨부 업로드에 소요.

429 hit 9회 발생 → `_request_with_retry` 의 지수 백오프로 회복.
401/403 없음.

## 3. audit 3회 진행 (텍스트 + 구조적 비교)

`run.py audit --full` 로 전체 1,674 페이지를 Confluence 에서 다시 받아
*dokuwiki raw* 와 카테고리별 카운트 비교:

| 카테고리 | audit1 (초기 업로드) | audit2 (4 fix + 재업로드) | audit3 (rewrite-links 재실행) |
|----------|---------------------|---------------------------|------------------------------|
| OK | 664 | 592 | **677** |
| EMPTY_DOKU (정상; raw 빈 페이지) | 396 | 396 | 396 |
| STRUCT_DIVERGED | 582 | 682 | 586 |
| TEXT_DIVERGED | 11 | 3 | 3 |
| TEXT_AND_STRUCT_DIVERGED | 18 | 1 | 12 |

`OK + EMPTY_DOKU = 1,073` 페이지(64%)가 *명백 통과*. 나머지 36% 중
*텍스트 콘텐츠 실손실*은 0건 (spot check 결과).

STRUCT_DIVERGED 586건은 대부분 **audit 분류기의 정밀성 한계**:

- **attachment_link / external_link 분류 불일치**: 변환기가 외부
  fetch.php proxy URL 을 external 로 재분류하는 게 정확하지만, dokuwiki
  raw 측에서는 attachment 로 카운트.
- **del / task / li 누적 delta**: dokuwiki 의 todo span 1개 = del+task
  각 1개 카운트이지만, Confluence storage 의 `<ac:task>` 는
  `<ac:task-list>` 안에 별도 li 자식. 의미 동등, 구조 다름.
- **sup / blockquote delta**: 풋노트 재작성 — dokuwiki raw 는 본문
  sup + 별도 footnotes div, 변환기는 통합.
- **dokuwiki `/tag/...` 링크**: tag namespace 는 dokuwiki 의 *동적 view*
  로 일급 페이지가 아니다. placeholder 가 미해결 → 평문 격하. *정상*.

남은 15건 TEXT-class divergence 직접 검토 결과 — 모두 (a) tag 링크
정상 격하 또는 (b) 코드 블록 GeSHi span 토큰 분할 차이. 콘텐츠 손실 0.

## 4. audit 결과로 발견된 버그 + fix (CL 52878)

라이브 결과 audit 으로 4개의 변환기 결함이 드러나 같은 CL 에 모두
수정.

### 4.1 chrome strip 가 정상 콘텐츠를 통째로 삭제

DokuWiki 가 `<blockquote>` 안의 URL 리스트 등을 `<div class='no'>` 로
감싸는 패턴이 있다. 변환기의 chrome 제거 룰(`breadcrumbs`, `trace`,
`tools`, `docInfo`, `no`, `headings`) 가 `class='no'` 도 chrome 으로
오인해 div 통째 decompose. 안에 든 URL 링크들이 함께 사라졌다.

- **영향**: 224 페이지의 `<blockquote></blockquote>` 빈 잔존 + 안의
  URL/콘텐츠 손실
- **심각도**: 이번 라운드 최대 콘텐츠 손실
- **fix**: chrome 제거 목록에서 `"no"` 제거. 코멘트로 *content
  wrapper 임* 명시
- **재마이그레이션**: 영향 페이지 전체 재변환 (+ 다른 fix 영향 포함 총
  544 페이지 PUT 됨)

### 4.2 smiley 이미지가 깨진 링크로

DokuWiki 코어가 `:-)` 같은 텍스트를 `<img class='icon smiley'
src='/lib/images/smileys/smile.svg' alt=':-)'>` 로 렌더링. 변환기는
이걸 그대로 통과 → Confluence 에서 *dokuwiki 서버를 가리키는 깨진
링크* 가 됨.

- **영향**: 5 페이지 (24 smiley 인스턴스)
- **fix**: `SMILEY_EMOJI_MAP` (24 entry) + `_convert_smileys(soup)` 가
  dokuwiki smiley `<img>` 를 유니코드 emoji 텍스트로 치환
- **재마이그레이션**: 자동 (재변환 시 본문 변경 → upload 자동 PUT)

### 4.3 disambiguated title 손실로 update 거부

cmd_upload 의 reactive disambiguation 이 *Confluence 가 400 반환 시*
title 에 `(<doku_id>)` 접미를 붙여 재시도. 그러나 *그 다음
cmd_convert 가 재실행* 되면 h1 추출 결과로 title 을 덮어써
disambiguation 손실. 다음 update PUT 이 *공간 내 다른 페이지와 title
충돌* 로 400 거부.

- **영향**: 4 페이지 (작은 7KB 페이지도 fail, 크기 무관)
- **fix**: cmd_convert 의 h1-덮어쓰기 가 기존 title 이 이미
  `<h1> (<...>)` 또는 `<h1> [<...>]` 형태면 보존
- **수동 복구**: Confluence 측 실제 title 을 state.db 로 sync → 3
  페이지 회복 (1건은 별도 §5 참조)

### 4.4 audit 도구 확장

샘플 비교에서 *전체 페이지 + 구조적 비교* 로 격상. `_structural_features`
가 H1-H6 / 인라인 포맷 / 표 / 리스트 / blockquote / void elements /
이미지 (internal/external/smiley 분리) / 링크 (page/attachment/external
+ placeholder) / 매크로 (info/tip/note/warning/panel/code) / task /
text-marker 까지 카운트. `_compare_features` 가 양측 카운트 매핑.

- 새 옵션: `--full`, `--sample N`, `--failed-only`, `--output-json`,
  `--output-html`, `--body-format`
- HTML 리포트는 mismatch 심각도별 색상 (OK=초록 / 경계=주황 /
  심각=빨강)

## 5. 미해결 항목 (별도 트랙)

### 5.1 가장 큰 일지 페이지 (한 페이지)

- 본문 storage XML **448 KB**, `<li>` **1,971**개, page_link placeholder
  **495**개
- POST 시 Confluence 응답 6회 backoff 후에도 body 없음 (`create no resp`)
- 같은 페이지 update 도 동일 패턴 (다른 디버그에서 작은 페이지도
  같은 응답 받은 이력 있음 — Confluence 측 일시적 backend 거부)
- 영향: 그 페이지 + 120개 첨부 (페이지 미생성으로 함께 대기)
- **권장 대응**:
  1. 본문을 N개 자식 페이지로 분할 (`<h2>` 단위 등)
  2. 또는 skeleton 페이지만 만들고 본문은 attachment (PDF/HTML) 로
  3. 또는 외부 호스팅 + URL 안내 매크로
  - 결정 보류. 별도 시나리오 문서 권장.

### 5.2 OVERSIZED 첨부 9건

- Confluence 단일 첨부 100MB 한도 초과 (개발 일지 PDF 등)
- 본문에 broken `<ri:attachment>` reference 만 남음
- **권장 대응**: `docs/oversized-attachments.md` 의 6모드 매트릭스
  참고. 6모드 중 채택안 결정 필요.

### 5.3 audit 분류기 정밀화 (옵션)

STRUCT_DIVERGED 586 중 대부분이 audit 의 카테고리 매핑 한계. 실제
콘텐츠 손실이 아니지만 mismatch 표시. 분류기 더 정교화로 노이즈
감소 가능. 우선순위 낮음.

### 5.4 dokuwiki tag 링크 → Confluence label

`/tag/<value>` 형태의 tag 링크는 현재 평문 격하. Confluence 의 *페이지
label* (별도 API) 로 매핑하면 의미 보존. 단, 본문 안 *visual* 표시는
사라지므로 trade-off 있음. 후속 PR 후보.

### 5.5 history / struct / 100MB 첨부 별도 트랙

각각 [`history-migration.md`](history-migration.md),
[`struct-migration.md`](struct-migration.md),
[`oversized-attachments.md`](oversized-attachments.md) 의 채택안에
따른 후속 구현 미시작. 현 라운드에는 *현재 시점 본문만* 이전.

## 6. 사후 정리 권장

| 항목 | 절차 |
|------|------|
| API 토큰 revoke | <https://id.atlassian.com/manage-profile/security/api-tokens> → 작업 라벨 토큰 revoke. 만료 설정한 경우 자동. |
| `.secrets/confluence.env` | 토큰 갱신 또는 파일 삭제 (작업 끝나면 더 필요 없음) |
| dev container | `python run.py dev down --purge` (이미 정리됨) |
| 로그 archive | `/tmp/upload*.log`, `/tmp/rewrite*.log`, `/tmp/audit*.log`, `/tmp/audit*.json/html` 필요 시 별도 보관 |
| state.db | 본 CL (52878) 에서 P4 에 보존됨. 다음 재실행 시 자동 resume 가능 |

## 7. 향후 재실행 시 주의

state.db 가 이제 P4 에 추적되므로 *다음 실행은 자동 resume*:

- `discover` 가 신규 페이지만 INSERT (기존 row 는 갱신만)
- `render` 는 `status=RENDERED` 페이지 건너뜀 (`--force` 로 강제 갱신)
- `convert` 는 `status=CONVERTED` 페이지 건너뜀 (`--force` 로 강제)
- `upload` 는 `uploaded_hash:<doku_id>` 메타와 `content_hash` 비교
  → 변경된 페이지만 PUT
- `rewrite-links` 는 `links.resolved=0` 인 행만 대상 (영구 평문 격하된
  것 포함; 재실행 시 `UPDATE links SET resolved=0` 로 리셋 후 호출
  가능)

새 dokuwiki 콘텐츠 ↔ 기존 Confluence 페이지 매핑이 그대로 유지되므로
중복 페이지 생성 위험 없음. 단 *Confluence 측에서 페이지를 수동 삭제*
하면 state.db 의 confluence_page_id 가 무효 — 그 때는 해당 행을
`UPDATE pages SET confluence_page_id=NULL, status='CONVERTED' WHERE
doku_id=?` 로 리셋하고 `upload --only` 로 재생성.

## 8. 코드/문서 변경 추적

이번 라운드의 모든 변경:

- **CL 52878** — 4 converter fixes + audit 확장 + state.db (binary,
  20MB) 첫 추적
- 별도 doc CL 들 (52871 oversized-attachments, 52697 element-mapping,
  52693 history-migration, 52695 struct-migration, 52709 runbook 등)
은 라이브 결과에 영향 받지 않은 사전 작업
