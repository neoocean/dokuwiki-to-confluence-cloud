# 라이브 마이그레이션 결과

본 문서는 운영 로그로 *날짜별 섹션* 으로 누적된다. 최신 통계는 마지막
day 의 §0 표. 과거 로그는 그대로 보존.

---

# Day 2 — 2026-05-19 (별도 트랙 적용 + 공간 정리)

후속 follow-up 작업 9건 (#4~#12) 자율 진행 + 별도 트랙 일부 실행 +
공간 안 비-마이그레이션 페이지 1,465건 휴지통 정리.

## §0 누적 통계 (2026-05-19 종료 시점)

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

## §1 적용된 follow-up (CL 52881, 52882)

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

## §2 라이브 적용 상세

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

## §3 코드 변경 (CL 52881 + 52882)

| CL | 신규 |
|----|------|
| 52881 | `cmd_rewrite_oversized` (B-mode 자동화). `_apply_page_labels` (v1 label API). `_convert_footnotes` 에 anchor 매크로 삽입. `<a rel=tag>` 추출 + `page_tags:<id>` 메타. `_compare_features` 의 `link_total` / `task_total` 합산. `NOISE_CLASS_PREFIXES` 에 `wrap_`. `_convert_html_to_storage` 5-tuple 반환 (page_tags 추가). 22 unit tests still pass. |
| 52882 | `cmd_rewrite_oversized_pages` (C-mode skeleton + zip). `cmd_history_render/convert/upload` (37k revision pipeline; resume-safe). `cmd_struct_convert/upload` (snapshot/properties/native). `_load_users_map` / `_format_user` (`--users-map` JSON). `_revision_header` (note 매크로). `docs/oversized-pages.md` 신규. 서브커맨드 13 → **22** (4 history + 4 struct + 2 rewrite). 22 unit tests still pass. |

## §4 outstanding (이전 Day 1 §5 이후 변화)

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
