# 실제 마이그레이션 실행 런북 (Confluence 키 도착 후)

본 문서는 Confluence API 키와 대상 공간/루트 페이지가 결정된 *직후*
실제 라이브 업로드를 안전하게 수행하기 위한 절차다. 키 받기 *전*에
준비해 둘 수 있는 모든 검증은 [`element-mapping.md`](element-mapping.md)
/ [`plugin-validation.md`](plugin-validation.md) /
[`scenarios.md`](scenarios.md) 의 풀 dry-run 으로 이미 끝나 있다고
가정한다.

## 1. 준비물 체크리스트

| 항목 | 어디서 받는가 | 본 인스턴스 추정값 |
|------|---------------|-------------------|
| Atlassian 이메일 | 자신의 Atlassian 계정 | `me@woojinkim.org` |
| API 토큰 | <https://id.atlassian.com/manage-profile/security/api-tokens> 에서 생성. 본 작업 전용 토큰을 분리 발급 권장 | (별도 발급) |
| 공간 키 (`spaceKey`) | Confluence UI 의 공간 설정 → 공간 키. 영문 대문자. | 미정 (e.g. `MIGRATE` 등 임시 공간 신규 생성 권장) |
| 루트 페이지 ID | UI 에서 페이지 → "..." → "페이지 정보" 또는 URL 의 `pageId=` 파라미터에 보임 | 미정 (해당 공간의 빈 페이지 한 개 생성 후 그 ID) |
| 본인 권한 | 공간의 페이지 생성·삭제·첨부 권한 | 본인 owner 공간이면 OK |

권장: **신규 공간 + 임시 루트 페이지**를 새로 만들어 거기에 *통째로* 올린
다음, 검증 끝나면 그 트리를 정식 위치로 옮기거나 폐기. 라이브 공간 안에
바로 올리면 롤백이 까다롭다.

## 2. 환경 변수 설정

```sh
export CONFLUENCE_EMAIL=me@woojinkim.org
export CONFLUENCE_API_TOKEN='ATATT3xFf...'
export CONFLUENCE_SPACE_KEY=MIGRATE          # 위에서 결정한 공간 키
export CONFLUENCE_ROOT_PAGE_ID=123456789     # 위에서 결정한 페이지 ID
export CONFLUENCE_BASE_URL=https://woojinkim.atlassian.net/wiki   # 기본값과 같음
```

`.env` 파일 + `direnv` 또는 단일 세션 내 export 둘 다 OK. 환경변수
이름 (CONFLUENCE_*) 은 `run.py` 의 default 와 일치.

## 3. 사전 점검 (한 명령어)

```sh
python run.py status      # state.db 의 페이지/첨부 카운트
python run.py report      # 매크로/크기/충돌 분석
python run.py lint        # 1675 storage XML 유효성 — 0 실패 기대
```

이 셋이 모두 깨끗하면 다음 단계.

## 4. 첫 라이브 실행 (소규모, 검증용)

```sh
python run.py upload --only wiki:syntax    # 한 페이지만 라이브 업로드
```

- Confluence UI 에서 결과 확인: 페이지 생성됨, h1 = 'Formatting Syntax',
  내부 링크는 *우선* placeholder dwc-link: 형태로 들어감 (S7 미실행).
- 첨부도 같이 업로드됨 (이 페이지의 첨부 1개 추정: wiki:dokuwiki-128.png).
- 매크로 박스 (`info`/`tip`/`note`/`warning`/`panel`/`code`) 가 정상
  렌더되는지 확인.

문제 발견 시: 그 페이지 Confluence UI 에서 삭제 → `state.db` 의 그 행에서
`confluence_page_id = NULL`, `status = 'CONVERTED'` 로 리셋 → 변환기 fix
→ `convert --only wiki:syntax --force` → 다시 `upload --only ...`.

## 5. 전체 업로드

```sh
python run.py upload --dry-run > /tmp/final-forecast.log    # 최종 forecast
grep '^\[.*upload 완료' /tmp/final-forecast.log              # created/updated/skipped/failed 카운트 확인
```

- 본 코퍼스 기준: created=1675 / 0 미수신.

문제 없으면 라이브:

```sh
python run.py upload > /tmp/upload.log 2>&1 &
tail -f /tmp/upload.log
```

- 약 60-120분 (rate limit 의존). 진행 중 `python run.py status` /
  `python run.py report` 로 모니터링.
- 중간에 SIGINT 받으면 다음 실행이 *이미 업로드된 페이지는 skip* 하며
  이어서 계속 (`uploaded_hash:<doku_id>` meta 기반).

## 6. 링크 2-pass

```sh
python run.py rewrite-links > /tmp/rewrite.log 2>&1
```

- 모든 페이지 업로드된 다음 실행. `dwc-link:<id>` placeholder 를 실제
  Confluence page reference 로 치환.
- 변경된 페이지만 PUT 으로 갱신.

## 7. 결과 감사

```sh
python run.py report
sqlite3 state.db "SELECT status, COUNT(*) FROM pages GROUP BY status"
sqlite3 state.db "SELECT status, COUNT(*) FROM attachments GROUP BY status"
sqlite3 state.db "SELECT resolved, COUNT(*) FROM links GROUP BY resolved"
```

기대값:
- pages: UPLOADED=1675, FAILED=0.
- attachments: UPLOADED=10643, OVERSIZED+FAILED 합=140 (디스크 미디어 누락 — 데이터 사실).
- links: resolved=N (자주 사용되는 페이지 링크 다수), unresolved=일부 (대상 페이지 없음 — 의도된 격하).

FAILED 페이지가 있으면 `--only <doku_id>` 로 개별 재시도. 매번 `last_error`
컬럼 확인.

## 8. 별도 트랙 + 사후 처리 서브커맨드

라이브 1차 후 *오류 / 한도 초과* 처리 자동화 (2026-05-19 적용):

```sh
# OVERSIZED 첨부 (>100MB) → 본문에 note 매크로 메타 박스
python run.py rewrite-oversized
# 본문 거부된 페이지 → skeleton + 원본 storage zip 첨부 (자식 첨부 회복)
python run.py rewrite-oversized-pages
```

별도 트랙:

```sh
# 과거 리비전 이전 (~37k 호출, 하룻밤 잡; resume-safe)
python run.py history-discover                                        # attic 인덱싱
python run.py history-render --base-url http://127.0.0.1:18080 --delay 0.05
python run.py history-convert                                         # storage XML + 헤더 박스
python run.py history-upload [--users-map users.json] [--limit N]    # 시간순 PUT replay

# struct 데이터 (4 schema / 1,213 row)
python run.py struct-discover                                         # sqlite 인덱싱
python run.py struct-upload --probe                                   # Database API 가용성
python run.py struct-convert --mode snapshot                          # 또는 properties / native
python run.py struct-upload                                           # 라이브
```

## 9. 롤백 / 실패 대응

### 9.1 라이브 업로드 중 부분 실패

- `upload` 가 도중 멈춰도 *이미 업로드된 페이지는 skip*. 다시 실행하면
  이어서 계속.
- 특정 페이지가 반복 FAILED 면 그 페이지의 raw HTML 과 storage XML 을
  `python run.py preview --doku-id <id>` 로 시각 검토 → 변환기 fix →
  `convert --only` + `upload --only`.

### 9.2 전체 롤백 (잘못된 공간으로 올렸을 경우)

본 도구는 **삭제 기능을 제공하지 않는다**. 의도적 제약 — 잘못해서 실제
공간을 지울 수 없게 함. 롤백 절차:

1. **선호: Confluence UI 에서 트리 통째 휴지통 이동**. 루트 페이지를
   "삭제" → 자식 페이지 전체가 함께 휴지통으로. 휴지통에서 30일 보존.
2. **API 로 강제 삭제 필요시**: 별도 스크립트 작성. *주의*: state.db 의
   `confluence_page_id` 가 채워진 페이지를 모두 DELETE → state.db 의
   `confluence_page_id`/`confluence_version` 컬럼 리셋. 검증 후만 사용.
3. **state.db 만 리셋해 처음부터 다시**: `confluence_page_id` 와
   `confluence_version` NULL 로, `status='CONVERTED'`, 그리고 `meta`
   테이블의 `uploaded_hash:*` 키들 모두 삭제. Confluence 의 페이지는
   별개로 정리해야 함 — state.db 만 리셋하면 다시 업로드 시 *중복 트리*
   생성.

```sql
-- state.db 리셋 (Confluence 페이지는 별도 정리 필요!)
UPDATE pages SET confluence_page_id=NULL, confluence_version=NULL,
                 status='CONVERTED', uploaded_at=NULL;
UPDATE attachments SET confluence_attachment_id=NULL, confluence_page_id=NULL,
                        status='DISCOVERED', uploaded_at=NULL
 WHERE status='UPLOADED';
DELETE FROM meta WHERE key LIKE 'uploaded_hash:%';
```

### 9.3 rate limit 누적 / 계정 락

Confluence 가 429 를 반복 반환하면 변환기의 `_request_with_retry` 가
`Retry-After` 기반 백오프. 그래도 계속 실패하면:
- `python run.py status` 로 어디까지 갔는지 확인
- 1-2시간 후 다시 시도
- 그래도 안 풀리면 Atlassian 지원 티켓 (계정 임시 정지 가능)

## 10. 사후 정리

```sh
# 1) 로그 보존
mkdir -p logs/$(date +%Y%m%d)
mv /tmp/upload.log /tmp/rewrite.log /tmp/final-forecast.log logs/$(date +%Y%m%d)/

# 2) state.db 백업 (P4 외부)
cp state.db logs/$(date +%Y%m%d)/state-snapshot.db

# 3) dev 컨테이너 정리 (라이브에는 불필요)
python run.py dev down --purge
```

state.db 의 confluence_page_id 매핑은 *향후 동기화/재실행에 필수*. P4
에는 ignore 되어 있지만 별도 보관 권장.

## 11. 변환기 변경 후 부분 재실행

마이그레이션 후에 변환 룰이 바뀌어 *특정 페이지 본문만 갱신*하려면:

```sh
python run.py convert --only <doku_id> --force   # 새 storage XML 생성
python run.py upload --only <doku_id>            # content_hash 변경 감지 → PUT
```

각 페이지의 `uploaded_hash:<doku_id>` meta 가 직전 업로드 본문의 해시.
변경 없으면 skip.
