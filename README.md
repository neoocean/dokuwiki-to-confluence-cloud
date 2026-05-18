# dokuwiki-to-confluence-cloud

자체 운영 중인 DokuWiki 의 **DokuWiki 가 렌더링한 최종 상태**를
Confluence Cloud 로 이전하는 파이썬 스크립트.

DokuWiki 의 `?do=export_xhtmlbody` 출력을 받아 Confluence storage
format 으로 변환, 네임스페이스 트리를 그대로 페이지 계층에 매핑하고
미디어/첨부를 함께 업로드한다.

## 상태

**1차 라이브 마이그레이션 완료 (2026-05-18)**:

| 지표 | 값 |
|------|----|
| 페이지 UPLOADED | 1,674 / 1,675 (1건 별도 트랙) |
| 첨부 UPLOADED | 10,613 (OVERSIZED 9 + missing 143 별도) |
| 내부 링크 resolved | 5,180 / unresolved 1,317 (평문 격하) |
| audit 통과율 | 64% 명백 OK + 36% 분류기 한계 (실손실 0) |
| 변환기 버그 fix | 4종 (라이브 audit 발견) |

자세한 결과는 [`docs/migration-result.md`](docs/migration-result.md).

## 파이프라인 (5단계)

```
discover  →  render  →  convert  →  upload  →  rewrite-links
   ↓           ↓           ↓          ↓             ↓
 pages/    raw/*.html  storage/   Confluence    ac:link
 walk      from        XML        POST/PUT      placeholder
 + meta    dokuwiki                              치환
```

| 단계 | 역할 |
|------|------|
| `discover` | `pages/*.txt` 트리에서 doku_id / namespace / parent / 메타 추출 |
| `render` | DokuWiki HTTP 로 `?do=export_xhtmlbody` 받아 캐시 |
| `convert` | bs4 로 storage XML 생성 + 풋노트/wrap/todo/이미지/링크 변환 |
| `upload` | Confluence v2 페이지/첨부 API; 네임스페이스 stub 자동 생성 + title disambiguation + content_hash 변경 시만 PUT |
| `rewrite-links` | 2-pass — `dwc-link:` placeholder 를 실제 `<ac:link><ri:page>` 로 |

## 빠른 사용

라이브 마이그레이션의 단계별 절차는 [`docs/runbook.md`](docs/runbook.md).
요약:

```sh
pip install -r requirements.txt

# 자격증명 (.secrets/confluence.env 에 KEY='VAL' 형식)
set -a; source .secrets/confluence.env; set +a

# 1) 로컬 DokuWiki 테스트 컨테이너 (선택, 라이브 wiki 없을 때)
python run.py dev up

# 2) 파이프라인
python run.py discover --src ~/p4/playground/docker/dokuwiki/data/data
python run.py render   --base-url http://127.0.0.1:18080 --delay 0.05
python run.py convert
python run.py upload   --dry-run                 # 점검
python run.py upload                              # 라이브
python run.py rewrite-links                       # 링크 2-pass

# 3) 검증
python run.py lint                                # storage XML 유효성
python run.py report                              # corpus 통계
python run.py audit --full --output-html /tmp/audit.html
python run.py preview --doku-id wiki:syntax       # side-by-side

# 4) 정리
python run.py dev down --purge
```

## 환경변수

| 변수 | 용도 |
|------|------|
| `CONFLUENCE_EMAIL` | Atlassian 계정 이메일 |
| `CONFLUENCE_API_TOKEN` | API 토큰 (https://id.atlassian.com/manage-profile/security/api-tokens) |
| `CONFLUENCE_SPACE_KEY` | 대상 공간 키 |
| `CONFLUENCE_ROOT_PAGE_ID` | 마이그레이션 트리 루트 페이지 ID |
| `CONFLUENCE_BASE_URL` | 기본 `https://woojinkim.atlassian.net/wiki` |
| `DOKUWIKI_BASE_URL` | render 단계의 dokuwiki HTTP 주소 |
| `DOKUWIKI_USER`/`DOKUWIKI_PASSWORD` | dokuwiki 인증 (ACL 켜진 경우) |
| `DOKUWIKI_SRC` | `--src` 의 기본값 |

`run.py upload`/`rewrite-links` 는 자격증명 누락 시 어떤 변수가
부족한지와 토큰 발급 링크를 한 번에 안내한다.

## 서브커맨드 일람

| 분류 | 명령 | 설명 |
|------|------|------|
| 파이프라인 | `discover` | `pages/*.txt` 인덱싱 |
|  | `render` | dokuwiki XHTML 캐시 |
|  | `convert` | storage XML 생성 |
|  | `upload` | Confluence 페이지/첨부 POST/PUT |
|  | `rewrite-links` | placeholder → ac:link |
| 검증 | `lint` | storage XML lxml parse 검증 |
|  | `report` | corpus 통계 (매크로/크기/충돌/FAILED) |
|  | `audit` | Confluence 와 dokuwiki raw 의 텍스트+구조 비교 (`--full` / `--sample N`) |
|  | `preview --doku-id <id>` | raw + storage side-by-side HTML |
|  | `status` | 상태 요약 |
| 운영 | `dev up` / `dev down [--purge]` | 로컬 DokuWiki 테스트 컨테이너 |
| 별도 트랙 (read-only) | `history-discover` / `history-status` | attic 인덱싱 |
|  | `struct-discover` / `struct-status` | struct.sqlite3 인덱싱 |

## 문서 구조

```
docs/
  scenarios.md             — 메인 시나리오 + 모든 별도 트랙 인덱스
  runbook.md               — 라이브 마이그레이션 단계별 절차 + 롤백
  migration-result.md      — 2026-05-18 첫 라이브 실행 운영 로그
  element-mapping.md       — DokuWiki 구성요소 → Confluence 변환 매트릭스
                             (Pass-through / Transformed / Partial /
                             Dropped / Separate-track)
  plugin-validation.md     — 활성 플러그인 검증 결과 + struct 플러그인
                             고아 데이터 정리
  oversized-attachments.md — 100MB 초과 첨부 이전 6모드 매트릭스
  history-migration.md     — 과거 리비전 이전 (B+A+F 채택, 구현 대기)
  struct-migration.md      — struct 스키마 → Confluence Database
                             매핑 (3모드 우선순위, 구현 대기)
  MEMORY.md                — 미래 세션이 먼저 읽을 컨텍스트 인덱스
```

## 별도 트랙 (라이브 후속)

본 파이프라인은 *현재 시점 페이지 본문* 만 다룬다. 다음은 별도 PR
대기:

- **과거 리비전 이전**: attic 의 ~37k 리비전을 시간순 PUT replay 로
  Confluence 버전 체인에 — `docs/history-migration.md`.
- **struct 데이터**: `meta/struct.sqlite3` 의 1,213 row 를 Confluence
  native Database 로 (A 우선, B 폴백) — `docs/struct-migration.md`.
- **100MB+ 첨부**: 본 corpus 9건 영향. 본문 메타 푸터 또는 외부
  호스팅 — `docs/oversized-attachments.md`.

## 개발

### 단위 테스트

```sh
pip install pytest
python -m pytest tests/ -q
```

22 테스트, ~0.1s.

### CI

`.github/workflows/ci.yml` — Python 3.11/3.12/3.13 matrix, 모든
서브커맨드 `--help` smoke + pytest.

### 변환기 변경 후

```sh
python run.py convert --force      # 전체 재변환
python run.py upload               # content_hash 변경된 페이지만 자동 PUT
python run.py rewrite-links        # placeholder 재해결 필요 시
python run.py audit --full         # 결과 검증
```

`state.db` 가 P4 에 추적되므로 재실행은 자동 resume. 새 페이지만 신규
생성되고 기존 페이지는 변경분만 PUT.

## 미해결 (별도 처리 대기)

- 큰 일지 페이지 1건 (`docs/migration-result.md §5.1`) — Confluence
  가 본문 parsing 거부. 분할/외부 호스팅 결정 필요.
- audit 분류기 정밀화 (실손실은 0이지만 분류 mismatch 가 시각적
  noise).
- 별도 트랙 (history / struct / oversized) 채택안 구현.

## 라이선스

LICENSE 참고.
