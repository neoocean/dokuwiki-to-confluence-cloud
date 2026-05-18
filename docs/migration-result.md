# 라이브 마이그레이션 결과 (2026-05-18)

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
