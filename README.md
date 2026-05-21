# dokuwiki-to-confluence-cloud

자체 운영 중인 DokuWiki 의 **DokuWiki 가 렌더링한 최종 상태**를
Confluence Cloud 로 이전하는 파이썬 스크립트.

DokuWiki 의 `?do=export_xhtmlbody` 출력을 받아 Confluence storage
format 으로 변환, 네임스페이스 트리를 그대로 페이지 계층에 매핑하고
미디어/첨부, 과거 리비전, struct 플러그인 데이터까지 함께 옮긴다.

> **AI 에이전트/사람 모두 처음 진입할 때**: [AGENT.md](AGENT.md) 를
> 먼저 읽을 것. Claude Code 자동 로드 컨텍스트는 [CLAUDE.md](CLAUDE.md).
> **다른 머신에 배포할 때**: [DEPLOY.md](DEPLOY.md) — 번들 구성 + 설치
> 절차 + 머신 의존 항목.

---

## 목차

- [현 상태](#현-상태)
- [요구 사항](#요구-사항)
- [설치](#설치)
- [환경 변수](#환경-변수)
- [빠른 시작 — wizard 한 줄](#빠른-시작--wizard-한-줄)
- [상세 사용법 (단계별)](#상세-사용법-단계별)
  - [Step 0. 컨테이너 준비 (`dev up`)](#step-0-컨테이너-준비-dev-up)
  - [Step 1. 페이지 인벤토리 (`discover`)](#step-1-페이지-인벤토리-discover)
  - [Step 2. XHTML 렌더 캐시 (`render`)](#step-2-xhtml-렌더-캐시-render)
  - [Step 3. storage 변환 (`convert`)](#step-3-storage-변환-convert)
  - [Step 4. Confluence 업로드 (`upload`)](#step-4-confluence-업로드-upload)
  - [Step 5. 내부 링크 해소 (`rewrite-links`)](#step-5-내부-링크-해소-rewrite-links)
  - [Step 6 (옵션). 과거 리비전 (`history-*`)](#step-6-옵션-과거-리비전-history-)
  - [Step 7 (옵션). struct 데이터 (`struct-*`)](#step-7-옵션-struct-데이터-struct-)
  - [Step 8 (옵션). 본문/첨부 한도 폴백 (`rewrite-oversized*`)](#step-8-옵션-본문첨부-한도-폴백-rewrite-oversized)
  - [Step 9. 검증 (`audit`/`lint`/`report`/`preview`)](#step-9-검증-auditlintreportpreview)
  - [Step 10. 시각 검수 (`verify build`)](#step-10-시각-검수-verify-build)
  - [Step 11. 결과 보고서 (`report-publish`)](#step-11-결과-보고서-report-publish)
  - [Step 12. 비교 갤러리 (`compare-publish`)](#step-12-비교-갤러리-compare-publish)
- [사용 시나리오 (Recipes)](#사용-시나리오-recipes)
- [상태 관리 (`state.db`)](#상태-관리-statedb)
- [트러블슈팅](#트러블슈팅)
- [서브커맨드 일람](#서브커맨드-일람)
- [문서 구조](#문서-구조)
- [개발](#개발)
- [미해결](#미해결)
- [라이선스](#라이선스)

---

## 현 상태

> 아래 표는 *본 도구의 작성자 인스턴스* 의 라이브 결과 (참고용).
> 다른 인스턴스에서는 본인의 결과로 채워질 것.

본 인스턴스의 라이브 마이그레이션 + 후속 정리 **완료** (2026-05-18 / 19):

| 지표 | 값 |
|------|----|
| 페이지 UPLOADED | **1,675 / 1,675 (100%)** |
| 첨부 UPLOADED | 10,725 (OVERSIZED 10건은 note 박스로 처리) |
| 내부 링크 resolved | 5,180 / unresolved 1,317 (평문 격하) |
| audit 통과율 | 64% 명백 OK + 36% 분류기 한계 (실손실 0) |
| 변환기 버그 fix | 9종 (라이브 + 후속 audit 발견) |
| struct (native, Day 4) | 4 Database 쉘 + 4 인덱스 + 1,213 row 자식 페이지 + 208 bound 페이지 임베드 |
| 공간 정리 | 1,465 비-마이그레이션 페이지 휴지통 (30일 회복 가능) |
| **history 리비전 보존** | **18,729 / 37,279 (50%)** — 큰 페이지의 후반 rev 는 본문 한도 초과로 영구 거부 |
| 결과 보고서 | Confluence 페이지 id `2522513553` 자동 발행 |

자세한 결과는 [`docs/migration-result.md`](docs/migration-result.md).

---

## 요구 사항

| 도구 | 버전 | 용도 |
|------|------|------|
| Python | 3.11+ | 본 스크립트 (3.13 권장) |
| Docker | 최신 | 로컬 DokuWiki 컨테이너 (`dev up`) — 옵션 |
| curl + tar | OS 기본 | data-only bootstrap 의 DokuWiki/플러그인 다운로드 |
| Atlassian 계정 | — | Confluence Cloud 권한 + API 토큰 |
| Perforce CLI | — | 본 저장소 내부용 — 외부 사용자는 git 만 사용 |

추가로 옵션 의존성 (`requirements.txt` 외):

| 의존성 | 용도 |
|--------|------|
| `playwright` + `chromium` | `verify build --with-screenshots` |
| `imagehash` + `pillow` | phash 유사도 계산 |
| `anthropic` SDK + `ANTHROPIC_API_KEY` | `verify build --with-vision` |

---

## 설치

```sh
# 1) 번들 풀기 (tar) 또는 git clone
tar -xzf dokuwiki-to-confluence-cloud.tar.gz
cd dokuwiki-to-confluence-cloud

# 2) 가상환경 + 의존성
python3 -m venv .venv          # Python 3.11+ 필요 (3.13 권장)
source .venv/bin/activate
pip install -r requirements.txt

# 3) 자격증명 (.env.example → .secrets/confluence.env 복사 후 편집)
mkdir -p .secrets
cp .env.example .secrets/confluence.env
$EDITOR .secrets/confluence.env       # 모든 placeholder 채우기

# 4) 매 세션마다 env 로드
set -a; source .secrets/confluence.env; set +a

# 5) 동작 확인
python -m pytest tests/ -q                # 84 통과 (~0.2초)
python run.py                              # 도움말 + exit 0
python run.py wizard --status              # wizard 진행 표 (빈 상태)
```

API 토큰 발급: <https://id.atlassian.com/manage-profile/security/api-tokens>

> **다른 머신에 처음 설치하는 경우**: [DEPLOY.md](DEPLOY.md) 참고 —
> 번들 구성 / 머신 요구사항 / 흔히 막히는 곳 / 보안 체크리스트.

---

## 환경 변수

| 변수 | 필수 | 용도 |
|------|------|------|
| `CONFLUENCE_EMAIL` | ✅ | Atlassian 계정 이메일 |
| `CONFLUENCE_API_TOKEN` | ✅ | API 토큰 |
| `CONFLUENCE_SPACE_KEY` | ✅ | 대상 공간 키 (UI → 공간 설정 → 공간 키) |
| `CONFLUENCE_ROOT_PAGE_ID` | ✅ | 마이그레이션 트리 루트 페이지 ID (URL `pageId=` 또는 페이지 정보) |
| `CONFLUENCE_BASE_URL` | ✅ | `https://<your-domain>.atlassian.net/wiki` |
| `DOKUWIKI_SRC` | ✅ | DokuWiki 데이터 디렉터리 (`pages/`, `media/`, `meta/` 의 상위 또는 full install root) |
| `DOKUWIKI_BASE_URL` | render 시 | dokuwiki HTTP base (라이브 또는 `dev up` 의 `http://127.0.0.1:18080`) |
| `DOKUWIKI_USER` / `DOKUWIKI_PASSWORD` | ACL 켜진 경우 | dokuwiki 로그인 (anonymous 로 export 안되는 페이지용) |
| `ANTHROPIC_API_KEY` | vision 옵션 시 | `verify build --with-vision` |

`run.py upload`/`rewrite-links`/`audit` 등 자격증명 누락 시 어떤 변수가
부족한지와 토큰 발급 링크를 한 번에 안내한다.

---

## 빠른 시작 — wizard 한 줄

> 실제 콘솔 화면 / 중단·재개 / 실패 복구 / 부분 재실행 / 비대화 자동화의
> 8 시나리오 walkthrough 는 [`docs/wizard-walkthrough.md`](docs/wizard-walkthrough.md).

대부분의 사용자는 `wizard` 한 명령으로 끝낸다. 14 단계가 차례로 진행
되며 각 단계 시작 시 `[Enter] 진행 / s skip / q quit / d done` 프롬프트.

```sh
set -a; source .secrets/confluence.env; set +a

python run.py wizard          # 처음 진행 / Ctrl+C 후 이어서
python run.py wizard --status # 진행 표만 출력
python run.py wizard --restart        # 모든 step state reset
python run.py wizard --from-step audit  # audit 단계부터만 (이전 영향 없음)
python run.py wizard --yes             # 모든 프롬프트 auto-yes
python run.py wizard --continue-on-error   # 실패해도 다음 단계로
```

### wizard 14 단계

| # | step | 동작 | 옵션 |
|---|------|------|------|
| 1 | `prereq` | env 변수 + docker 가용성 점검 | 필수 |
| 2 | `dev-up` | DokuWiki 컨테이너 기동 (full install 또는 data-only 자동 분기) | 선택 |
| 3 | `discover` | `pages/` 트리 인벤토리 | 필수 |
| 4 | `render` | DokuWiki XHTML 캐시 | 필수 |
| 5 | `plugin-audit` | `~~MACRO~~` 잔존 카운트 + 플러그인 설치 권장 | 필수 |
| 6 | `convert` | XHTML → Confluence storage XML | 필수 |
| 7 | `upload` | Confluence v2 페이지 + 첨부 | 필수 |
| 8 | `rewrite-links` | 2-pass `dwc-link:` placeholder → `<ri:page>` | 필수 |
| 9 | `history` | 과거 리비전 이전 (~30분~수시간) | 선택 |
| 10 | `struct` | meta/struct.sqlite3 있을 때만 | 선택 |
| 11 | `audit` | Confluence 측 본문 검증 (sample) | 필수 |
| 12 | `verify` | 사용자 시각 검수 큐 + 사람 검수 | 필수 |
| 13 | `report` | 결과 리포트 stdout | 필수 |
| 14 | `report-publish` | 결과 보고서 Confluence 페이지 발행 | 필수 |

중단/재개 모델:
- 정상 종료 → `wizard_state.status = done`
- `Ctrl+C` → `interrupted` (다음 실행 시 그 단계부터 자동 이어짐)
- 실패 → `failed` + error (기본은 즉시 halt — 원인 해결 후 재실행)
- 사용자가 `q` 선택 → 현재 단계 그대로
- `s` skip → `skipped` (다음 실행에서 자동 skip)

---

## 상세 사용법 (단계별)

각 단계를 wizard 없이 *직접* 실행하는 방법. 디버깅, 재실행, 부분 마이그레이션, CI/cron 자동화 등에 유용.

### Step 0. 컨테이너 준비 (`dev up`)

라이브 dokuwiki 가 있다면 그 URL 을 `DOKUWIKI_BASE_URL` 에 넣어 바로
[Step 2 render](#step-2-xhtml-렌더-캐시-render) 로 가도 된다. 본 인스턴스
처럼 데이터만 있으면 컨테이너로 띄워야 export 가능.

```sh
# 1) full install (doku.php + lib/ + inc/) — 자동 감지 → APFS clonefile
python run.py dev up --src /path/to/dokuwiki-install

# 2) data-only (pages/ + media/ 만) — 자동 분기:
#    DokuWiki stable tarball 자동 다운로드 → 데이터 overlay
#    → conf/plugins.local.php + meta/struct.sqlite3 + ~~MACRO~~ 스캔으로
#       플러그인 자동 감지 → release tarball 로 자동 설치
#    → conf/local.php 의 useacl=0 패치 → docker compose up -d
python run.py dev up --src ~/backup/wiki-data-only

# 3) 강제 bootstrap (full install 이어도 core 새로 받음)
python run.py dev up --src /path --bootstrap

# 4) 기존 클론에 누락 플러그인만 추가 설치 (재기동 없이)
python run.py dev install-plugins

# 5) 종료
python run.py dev down
python run.py dev down --purge   # 클론 /tmp/dwc_test_dokuwiki 도 삭제
```

자동 설치되는 플러그인 (release tarball URL 매핑 내장):
- 외부: wrap, struct, todo, discussion, blog, include, pagelist, tag, tagging, sqlite
- core 번들 (별도 설치 불필요): info, config, acl, extension, usermanager, styling, auth*(plain/ad/ldap/pdo), safefnrecode, upgrade

매핑에 없는 플러그인은 `unknown` 으로 표시 — 컨테이너 기동 후
`http://127.0.0.1:18080/doku.php?do=admin&page=extension` 에서 수동 설치.

자세한 절차: [`docs/runbook.md §0-A`](docs/runbook.md).

### Step 1. 페이지 인벤토리 (`discover`)

```sh
python run.py discover --src $DOKUWIKI_SRC
# 출력: pages 테이블에 N 페이지 (status='DISCOVERED')
```

`pages/*.txt` 를 walk 해 `doku_id` / `namespace` / `parent_doku_id` /
`is_namespace_index` / meta title 을 추출. `start.txt` 가 네임스페이스
인덱스.

### Step 2. XHTML 렌더 캐시 (`render`)

```sh
# 라이브 dokuwiki 가 있을 때
python run.py render --base-url https://wiki.example.com

# 로컬 dev 컨테이너로 (Step 0 후)
python run.py render --base-url http://127.0.0.1:18080 --delay 0.05

# 특정 페이지만
python run.py render --only wiki:syntax

# 이미 받은 페이지도 다시 받기
python run.py render --force
```

dokuwiki 의 `?do=export_xhtmlbody` 응답을 `raw/` 에 캐시. 이미
렌더링된 페이지는 skip (idempotent). `--delay` 로 요청 간 슬립.

### Step 3. storage 변환 (`convert`)

```sh
python run.py convert            # 새로 변환할 페이지만
python run.py convert --force    # 전체 재변환 (변환기 로직 바뀌면 사용)
python run.py convert --only wiki:syntax
```

bs4 로 `raw/*.xhtml` → Confluence storage XML 생성 (`storage/`).
풋노트 / wrap callout / todo / 이미지 / 내부 링크 / 외부 링크 / 코드
블록 / 표 / 리스트 / 헤딩 / 한국어 비-ASCII path 디코딩 등을 모두 처리.
내부 페이지 링크는 `dwc-link:<id>` placeholder 로 남기고 `links` 테이블
에 기록.

매크로/요소별 동작은 [`docs/element-mapping.md`](docs/element-mapping.md).

### Step 4. Confluence 업로드 (`upload`)

```sh
# 사전 점검 (실 호출 없음 — 트리/stub/첨부 forecast)
python run.py upload --dry-run

# 라이브
python run.py upload

# 특정 페이지만 + 부모 chain 자동 동반
python run.py upload --only wiki:syntax --include-parents

# 처음 N개만 (소규모 검증)
python run.py upload --limit 10
```

부모-자식 BFS 로 Confluence v2 페이지 생성 (`POST /api/v2/pages`) /
갱신 (`PUT`). `content_hash` 가 같으면 PUT 생략. 네임스페이스 인덱스
페이지가 dokuwiki 에 없으면 stub 자동 생성. 첨부는 v1
`/rest/api/content/{id}/child/attachment` 로 같이 업로드. 429/5xx
자동 backoff (최대 6회).

### Step 5. 내부 링크 해소 (`rewrite-links`)

```sh
python run.py rewrite-links              # 라이브
python run.py rewrite-links --dry-run    # 검증
python run.py rewrite-links --only wiki:syntax
```

`links` 테이블의 placeholder (`dwc-link:<target>`) 를 실제
`<ac:link><ri:page ri:content-title="..."/>` 로 치환. 변경된 페이지만
재 PUT.

### Step 6 (옵션). 과거 리비전 (`history-*`)

attic + media_attic + meta/.changes 를 시간순 PUT replay 해 Confluence
페이지 버전 체인 보존. **수시간 소요** (~37k PUT).

```sh
python run.py history-discover          # attic 인덱싱 (10-20초)
python run.py history-render \          # 각 리비전 ?rev= 받아 캐시
    --base-url $DOKUWIKI_BASE_URL --delay 0.05
python run.py history-convert           # storage XML + 헤더 박스
python run.py history-upload \          # 시간순 PUT replay
    [--users-map users.json] [--limit N]
python run.py history-status            # 진행 요약
```

`--users-map` JSON 형식: `{"doku_user": "557058:abcdef..."}` (Confluence
accountId 매핑). 매핑 없으면 사용자명을 텍스트로 표시.

**제약**: Confluence 의 `version.createdAt`/`authorId` 는 backdate 불가
→ 원본 ts/user/comment 는 본문 헤더 박스로만 보존. 큰 일지 페이지의
후반 rev 는 본문 한도 초과로 영구 거부 가능.

자세히: [`docs/history-migration.md`](docs/history-migration.md).

### Step 7 (옵션). struct 데이터 (`struct-*`)

`data/meta/struct.sqlite3` 가 있는 경우만. 각 schema 별로 빈 Confluence
Database 쉘 + 인덱스 페이지 + row 자식 페이지 + bound 페이지 임베드.

```sh
# 1) API 가용성 측정 (선택)
python run.py struct-upload --probe

# 2) sqlite 인덱싱
python run.py struct-discover

# 3) storage XML 변환 (3모드 — native 권장)
python run.py struct-convert --mode native --reconvert

# 4) 라이브 업로드
python run.py struct-upload --mode native
# 옵션: --only-tbl brevet_event / --row-limit 10 (디버깅)
# 옵션: --no-native-shell (빈 Database 객체 생성 skip)
# 옵션: --index-only (인덱스 페이지만 PUT, row 갱신 skip)

# 5) bound 페이지에 패널 임베드 (struct row 의 Wiki/doku_id 컬럼이 가리키는 페이지)
python run.py struct-embed-on-bound-pages

# 6) 상태
python run.py struct-status
```

**Confluence Cloud Database API 한계** (2026-05-19 probe 결과):
`create/get/delete` 만 공개, 컬럼/row endpoint 부재. 따라서 데이터는
Page Properties + Page Properties Report 매크로 조합으로 옮기고,
Database 객체는 빈 쉘로만 존재 (id 는 `struct_schemas.confluence_db_id`
에 저장됨 — Atlassian 후속 API 공개 시 자동 채움 코드 경로 준비됨).

자세히: [`docs/struct-migration.md`](docs/struct-migration.md).

### Step 8 (옵션). 본문/첨부 한도 폴백 (`rewrite-oversized*`)

```sh
# 100MB+ 첨부 → note 매크로 박스로 표시 (실제 파일은 P4 백업 안내)
python run.py rewrite-oversized

# 본문 거부된 페이지 → skeleton + 원본 storage XML 첨부 (zip)
python run.py rewrite-oversized-pages
```

자세히: [`docs/oversized-attachments.md`](docs/oversized-attachments.md) /
[`docs/oversized-pages.md`](docs/oversized-pages.md).

### Step 9. 검증 (`audit`/`lint`/`report`/`preview`)

```sh
# storage XML 유효성 (lxml parse)
python run.py lint

# corpus 통계 (매크로/크기/충돌/FAILED)
python run.py report

# Confluence 측 다시 받아 본문 비교 (Phase 3 자동 신호 5종 포함)
python run.py audit --sample 50            # 임의 50개
python run.py audit --full                 # 전체 (느림)
python run.py audit --failed-only          # FAILED 만
python run.py audit --output-html /tmp/audit.html

# raw vs storage 한 페이지 side-by-side
python run.py preview --doku-id wiki:syntax
```

audit 의 자동 신호 (Phase 3):
- 문장 정렬 ratio (difflib SequenceMatcher)
- artifact 보존 (숫자/일정/IP/버전 + URL + 이메일 집합 diff)
- 코드블록 해시 set 일치
- 헤딩 시퀀스 LCS
- 링크 해소율 (`ri:page` vs `dwc-link:`)

임계값 기반 자동 status 격상: SENTENCE_DIVERGED / ARTIFACT_LOSS /
CODE_DIVERGED / HEADING_DIVERGED.

### Step 10. 시각 검수 (`verify build`)

사용자가 브라우저에서 OK/NG/DEFER 분류 → JSON 다운로드 → import.

```sh
# 기본 (의존성 없음)
python run.py verify build --sample 100

# 권장 (실 본문 + 첨부 점검)
python run.py verify build --sample 100 \
    --with-confluence-view --with-attachment-check

# Playwright 스크린샷 + phash 유사도 (옵션)
pip install playwright imagehash pillow
playwright install chromium
python run.py dev up
python run.py verify build --sample 50 \
    --with-confluence-view --with-attachment-check --with-screenshots \
    --dokuwiki-base-url http://127.0.0.1:18080

# AI vision 자동 비교 (옵션)
export ANTHROPIC_API_KEY=...
python run.py verify build --sample 50 --with-screenshots --with-vision

# Phase 4 시각 비교 추가 신호 7가지 (모두 활성)
python run.py verify build --sample 50 --with-screenshots --with-all-extra-signals
# 개별로:
#   --with-pixel-diff      chrome 마스킹 후 본문 픽셀 diff + overlay PNG (Pillow)
#   --with-tile-phash      4×8 타일 분할 PHash + bad-tile overlay (imagehash + Pillow)
#   --with-element-compare bbox 시퀀스 LCS 짝짓기
#   --with-bbox-lcs        bbox tree LCS + 상대 너비 차이
#   --with-storage-ast     DokuWiki HTML / Confluence storage canonical 트리 LCS
#   --with-color-hist      색상 histogram cosine similarity
#   --with-ocr             OCR 백업 텍스트 비교 (pytesseract + tesseract)

# 브라우저에서 OK/NG/DEFER 분류 → 'JSON 다운로드'
python run.py verify import ~/Downloads/decisions.json

# 진행률
python run.py verify status
```

자세히: [`docs/visual-audit.md`](docs/visual-audit.md).

### Step 11. 결과 보고서 (`report-publish`)

```sh
python run.py report-publish
# state.db 의 통계 (pages/attachments/revisions/struct/verify) + wizard
# 진행표를 모아 Confluence 페이지로 생성/갱신. 'wizard_report_page_id'
# meta 에 page_id 저장 — 재실행 시 PUT (멱등).

# 제목 override
python run.py report-publish --report-title "DokuWiki 2026 마이그레이션 보고서"
```

### Step 12. 비교 갤러리 (`compare-publish`)

마이그레이션이 정성적으로 잘 됐는지 *시각적으로* 검증. 주요 페이지의
DokuWiki 와 Confluence 측을 헤드리스 Chromium 으로 풀-페이지 캡쳐 후
Confluence 루트 하위에 갤러리 페이지 발행/갱신.

```sh
python run.py compare-publish              # 기본 8 페이지 자동 선정
python run.py compare-publish --sample 20  # 카테고리당 2~3개로 늘림
python run.py compare-publish --select start,wiki:syntax,u:lam:calendar
                                           # 명시 페이지 list
python run.py compare-publish --dry-run    # 후보 + 캡쳐만, 발행 skip
python run.py compare-publish --no-recapture  # 기존 PNG 재사용 (본문만 갱신)
```

- 자동 selection 카테고리 10종: 메인 / 사용자 시작 / iframe / encrypt / 표
  풍부 / 이미지·첨부 / info·note·warning / 매크로 다양 / 코드 / 대용량
- per-category count = `sample/8` — `--sample 20` 이면 각 카테고리 2~3개
- 첨부 이미지는 v1 endpoint 으로 src rewrite (Confluence `/wiki/download`
  endpoint 가 Basic Auth 거부 → 302 redirect 통해 media binary fetch)
- 페이지 height 자동 clip 12000px (이미지 100+ 페이지가 100MB 첨부 한도
  초과·갤러리 비대화 회피)

---

## 사용 시나리오 (Recipes)

### A. 처음부터 끝까지 — wizard 한 줄

```sh
set -a; source .secrets/confluence.env; set +a
python run.py wizard       # 14 단계 차례로
```

### B. 도쿠위키 없이 데이터만 가지고 시작

원본 인스턴스/도커 이미지 없어도 OK. `dev up` 이 자동으로:
DokuWiki stable tarball 다운로드 + 데이터 overlay + 플러그인 자동 감지·설치.

```sh
export DOKUWIKI_SRC=/path/to/just-data-only
python run.py dev up                    # 자동 bootstrap
python run.py wizard --from-step discover
```

### C. 중단 후 이어서 (Ctrl+C / 실패)

```sh
# 어디까지 됐는지 확인
python run.py wizard --status

# 그대로 이어 (중단된 단계부터)
python run.py wizard

# 특정 단계만 다시
python run.py wizard --from-step audit
```

### D. 변환기 수정 후 재마이그레이션

```sh
python run.py convert --force           # 전체 재변환
python run.py upload                    # content_hash 다른 페이지만 자동 PUT
python run.py rewrite-links             # 필요 시 재해결
python run.py audit --full              # 결과 검증
python run.py report-publish            # 보고서 갱신
```

`state.db` 가 P4 추적되므로 재실행은 자동 resume. 새 페이지만 신규
생성되고 기존 페이지는 변경분만 PUT.

### E. 특정 네임스페이스만 부분 마이그레이션

```sh
# render / convert 는 그대로 전체
python run.py discover --src $DOKUWIKI_SRC
python run.py render
python run.py convert

# 업로드만 부분
python run.py upload --only wiki:syntax --include-parents
python run.py upload --only project:* --include-parents   # (와일드카드는 미지원 — 페이지별 호출)
```

### F. dry-run forecast 만

```sh
python run.py upload --dry-run > /tmp/upload-forecast.txt
python run.py rewrite-links --dry-run
```

### G. 보고서만 갱신

```sh
python run.py report-publish     # 같은 페이지 PUT (멱등)
```

### H. 결과 검증만 다시

```sh
python run.py audit --full --output-html /tmp/audit.html
python run.py verify build --sample 200 --with-confluence-view
```

### I. 자동화 (CI/cron)

```sh
# 비대화 모드
python run.py wizard --yes --continue-on-error \
    --audit-sample 100 --verify-sample 50
```

`wizard_state` 가 진행을 보존하므로 cron 으로 매시간 호출해도 한 번
done 된 단계는 자동 skip.

---

## 상태 관리 (`state.db`)

SQLite 단일 파일. 모든 단계가 idempotent 하게 동작하도록 상태 보관.

| 테이블 | 용도 |
|--------|------|
| `pages` | doku_id → confluence_page_id 매핑 + status (DISCOVERED/RENDERED/CONVERTED/UPLOADED/FAILED) |
| `attachments` | media_id → confluence_attachment_id + status |
| `links` | 내부 페이지 링크 placeholder 추적 (2-pass rewrite) |
| `revisions` | history 트랙 — doku_id × rev_ts |
| `history_meta` | 페이지별 history 진행 (last_replayed_rev_ts) |
| `media_revisions` | media history 트랙 |
| `struct_schemas` | struct schema → confluence_db_id + index page |
| `struct_columns` | 컬럼 정의 |
| `struct_rows` | row → confluence_page_id (자식 페이지) |
| `struct_references` | Wiki/Media/Lookup 컬럼의 cross-ref |
| `verify_decisions` | 사용자 시각 검수 결정 (OK/NG/DEFER + ng_tag + notes) |
| `wizard_state` | wizard 14 단계 진행 (status/started_at/finished_at/summary/error) |
| `meta` | dokuwiki_src, confluence_base_url, wizard_report_page_id 등 |

P4 에 추적 (resume-safe). 깨졌으면 `python run.py status` /
`python run.py wizard --status` 로 진단.

---

## 트러블슈팅

### 자격증명 누락
`upload` / `rewrite-links` / `audit` 등 라이브 명령이 어떤 env 변수가
부족한지와 토큰 발급 링크를 한 번에 안내한다.

```sh
[2026-05-19T...] 자격증명 누락 — Confluence API 호출 불가.
[2026-05-19T...]   CONFLUENCE_EMAIL=...
[2026-05-19T...]   API 토큰: https://id.atlassian.com/manage-profile/security/api-tokens
```

### ACL 잠긴 페이지 (anonymous 빈 응답)
`dev up` 이 클론된 `conf/local.php` 의 `$conf['useacl'] = 0` 으로 자동
패치. 그래도 안 되면 `docker exec dokuwiki-mig cat /var/www/html/conf/local.php | grep useacl`.

### 429 rate limit
`_request_with_retry` 가 `Retry-After` 헤더 기반 자동 backoff (최대 6회).
지속되면 `--limit N` 으로 batch 크기 줄이거나 시간차 재실행.

### 본문 한도 초과 (500/no-resp)
큰 페이지의 storage XML 이 Confluence 본문 한도를 넘으면 fail. `rewrite-oversized-pages`
가 skeleton + zip 첨부로 폴백. history 의 경우 영구 제약 — 후반 rev 는
보존 불가.

### 변환기에 ~~MACRO~~ 잔존
`plugin-audit` 단계에서 카운트됨. 두 가지 처리:
1. DokuWiki 측에 플러그인 설치 → re-render (`dev up` 의 admin 메뉴)
2. 의도적 strip — `_strip_dead_macro` (`run.py`) 에 패턴 추가

### Confluence 본문에 PHP warning 누수
`dev up` 의 compose 가 `display_errors=Off` + `error_reporting=E_ERROR`
를 설정. 외부 라이브 dokuwiki 라면 `php.ini` 도 같은 설정 필요.

### state.db 가 잠겨 있음
WAL 모드라 동시 read 는 OK 지만 long-running write 가 있으면 lock.
보통 다른 `python run.py *` 프로세스가 살아있는 것. `ps | grep run.py`.

---

## 서브커맨드 일람

| 분류 | 명령 | 설명 |
|------|------|------|
| 진입점 | (인자 없음) | 도움말 + exit 0 |
|  | `wizard` | 대화형 14 단계 (중단/재개) |
| 파이프라인 | `discover` | `pages/*.txt` 인덱싱 |
|  | `render` | dokuwiki XHTML 캐시 |
|  | `convert` | storage XML 생성 |
|  | `upload` | Confluence v2 페이지/첨부 POST/PUT |
|  | `rewrite-links` | placeholder → ac:link |
| 검증 | `lint` | storage XML lxml parse |
|  | `report` | corpus 통계 |
|  | `audit` | Confluence 와 dokuwiki raw 비교 (Phase 3 자동 신호 5종) |
|  | `preview --doku-id <id>` | raw + storage side-by-side |
|  | `status` | 상태 카운트 요약 |
| 운영 | `dev up [--bootstrap] [--install-plugins]` | 컨테이너 기동 (full / data-only 자동 분기) |
|  | `dev down [--purge]` | 컨테이너 종료 |
|  | `dev install-plugins` | 기존 클론에 플러그인 추가 설치 |
|  | `plugin-scan [--only-missing] [--install]` | DokuWiki 페이지 본문 스캔 → 미설치 플러그인 식별 (DokuWiki 동작 없이도) |
| 사후 처리 | `rewrite-oversized` | 100MB+ 첨부 → note 매크로 |
|  | `rewrite-oversized-pages` | 본문 거부 페이지 → skeleton + zip |
| history | `history-discover/render/convert/upload/status` | attic 인덱싱 → ?rev= 캐시 → storage + 헤더 → 시간순 PUT replay |
|  | `history-convert --header-format {panel\|info\|note\|quote\|table\|paragraphs\|none}` | revision 헤더 형식 (기본 panel + shift+enter) |
|  | `history-rewrite-headers --header-format X` | 이미 업로드된 페이지의 헤더만 새 형식으로 재PUT |
| struct | `struct-discover/convert/upload/embed-on-bound-pages/status` | sqlite 인덱싱 → snapshot/properties/native 변환 → 라이브 |
| 시각 검수 | `verify build [--with-screenshots] [--with-vision] [--with-attachment-check]` | DOM 큐 + Phase 2 옵션 + Phase 3 자동 신호 |
|  | `verify build --with-all-extra-signals` | Phase 4 시각 비교 7개 신호 (pixel-diff / tile-phash / 등) |
|  | `verify import <decisions.json>` | 결정 반영 |
|  | `verify status` | 진행 요약 |
| 링크 점검 | `link-check [--check-external] [--only X] [--limit N] [--verbose]` | Confluence 측 placeholder 잔존 / unresolved page link / 외부 URL HEAD |
| 보안 | `decrypt -p PASS CIPHER` | encryptedpasswords cipher (AES-256-CBC) 복호화 — pycryptodome 필요 |
|  | `decrypt -p PASS --page DOKU_ID` | state.db 페이지의 모든 cipher 일괄 복호화 |
|  | `decrypt -p PASS --confluence-id N` | Confluence 페이지의 모든 cipher 일괄 복호화 |
| 보고서 | `report-publish` | state.db 통계를 Confluence 페이지로 발행/갱신 |

각 명령의 상세 옵션은 `python run.py <command> --help`.

---

## 문서 구조

```
.
├── README.md               — 본 파일 (사용법 종합)
├── AGENT.md                — 사람/AI 진입점 (1분 안내 + 핵심)
├── CLAUDE.md               — Claude Code 자동 로드 컨텍스트
└── docs/
    ├── MEMORY.md           — 세션 간 지속 메모리 (인덱스)
    ├── scenarios.md        — 메인 시나리오 S1~S10 + 새 엣지 케이스 절차
    ├── runbook.md          — 라이브 단계별 절차 + wizard 안내
    ├── wizard-walkthrough.md — wizard 8 사용 시나리오 (Happy path / Ctrl+C 재개 / 실패 복구 / 부분 재실행 등)
    ├── migration-result.md — Day 1-4 운영 로그
    ├── element-mapping.md  — DokuWiki → Confluence 요소 매트릭스
    ├── plugin-validation.md — 플러그인 동작 검증
    ├── visual-audit.md     — 시각 검수 자동화 (Phase 1+2+3+4 구현 완료)
    ├── visual-comparison-proposal.md — 시각 비교 추가 자동화 8 후보 (1-7 채택, 8 보류)
    ├── struct-migration.md — struct → Confluence Database
    ├── history-migration.md — 과거 리비전 이전
    ├── oversized-attachments.md — 100MB+ 첨부 폴백
    └── oversized-pages.md  — 본문 한도 초과 폴백
```

---

## 별도 트랙 (적용 완료 / 진행 중)

| 트랙 | 상태 | 비고 |
|------|------|------|
| **OVERSIZED 첨부 → note 박스** | ✅ B 모드 적용 (9건) | `docs/oversized-attachments.md` |
| **큰 본문 페이지 → skeleton + zip** | ✅ C 모드 적용 (1건) | `docs/oversized-pages.md` |
| **struct → Confluence 페이지/Database** | ✅ native 모드 라이브 (4 schema, 1,213 row + 4 Database 쉘 + 208 bound 임베드) | `docs/struct-migration.md` |
| **과거 리비전 (~37k)** | ✅ 50% 라이브 (영구 제약 도달) | `docs/history-migration.md` |
| **공간 비-마이그레이션 정리** | ✅ 1,465 휴지통 | `docs/migration-result.md §2.5` |
| **시각 검수 자동화** | ✅ Phase 1+2+3+4 (DOM 큐 + iframe + 첨부 점검 + Playwright + AI vision + 5 자동 신호 + 7 시각 비교 신호: pixel-diff/tile-phash/element-compare/OCR/bbox-LCS/storage-AST/color-hist) | `docs/visual-audit.md` |
| **wizard + report-publish** | ✅ 14 단계 step-by-step + 결과 보고서 자동 발행 | `docs/runbook.md §0` |
| **data-only bootstrap** | ✅ DokuWiki core + 플러그인 자동 설치 | `docs/runbook.md §0-A` |

---

## 개발

### 단위 테스트

```sh
python -m pytest tests/ -q
# 84 통과, ~0.2s
```

### CI

`.github/workflows/ci.yml` — Python 3.11/3.12/3.13 matrix, 모든
서브커맨드 `--help` smoke + pytest.

### 변환기 변경 후

```sh
python run.py convert --force      # 전체 재변환
python run.py upload               # content_hash 변경된 페이지만 자동 PUT
python run.py rewrite-links        # placeholder 재해결 필요 시
python run.py audit --full         # 결과 검증
python run.py report-publish       # 보고서 갱신
```

### 새 엣지 케이스 발견 절차

1. `grep -lr <pattern> storage --include='*.xml'` 로 영향 페이지 측정
2. 원인 진단
3. `_convert_html_to_storage` 또는 해당 헬퍼에 룰 추가
4. `python run.py convert --force` → 다시 grep 으로 0 확인
5. `docs/scenarios.md §7.2` 표 갱신
6. CL 분리 제출 (변환기 fix + 문서)

### Perforce 정책

- 디폴트 체인지리스트 금지. 항상 번호 매긴 CL + 상세 description
- 제출 후 GitHub 미러 (저장소별 설정) 푸시
- 깃 커밋 본문 끝에 `P4 CL <N>` 한 줄

---

## 미해결

| 항목 | 상태 |
|------|------|
| 큰 페이지의 history 후반 rev (~18,503 잔존) | 영구 — Confluence 본문 한도. 추가 시도 시 *같은 위치* fail |
| `p:start` 649 rev 0 처리 | 별도 진단 후 처리 |
| struct Database 컬럼/row API | Confluence Cloud v2 가 미공개. 데이터는 Page Properties, Database 는 빈 쉘 — id 는 state.db 저장됨 (추후 API 가용 시 자동 채움) |
| 휴지통 1,465 페이지 영구 삭제 | 30일 자동 또는 사용자 즉시 purge 결정 |
| API 토큰 revoke | 마이그레이션 종료 시 |

---

## 라이선스

LICENSE 참고.
