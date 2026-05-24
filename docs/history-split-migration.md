# 큰 페이지의 history 분할 보존 시나리오

**상태 (2026-05-24): PoC 완료 — u:neoocean:2020 라이브 적용.** 본 문서는
docs/oversized-pages.md (현재 latest 본문의 H 단위 분할) 와 docs/history-
migration.md (시간순 PUT replay) 의 *교집합* — *큰 페이지의 history 를
자연 경계로 분할해 자식 페이지들의 chain 으로 보존* 하는 후속 트랙.

**PoC 라이브 결과 (u:neoocean:2020)**:
- 자식 페이지 **8개** (2020-01 ~ 2020-08, 월별 H2 chunk)
- chunk rev **UPLOADED 3,333** (각 rev 의 변경된 chunk 만)
- SKIPPED 10,670 (no-change + _misc policy + 250 PUT no resp)
- 자식 페이지 가장 무거운 chunk (`2020-01`): 807 version
- parent 본문 → 인덱스 (8 자식 link + children 매크로)
- `large_body_fallback` 메타 자동 제거 (C-mode 해제)

PoC 소요: ~40분 (cur_ver cached counter 적용 후 ~75 PUT/min). `_misc`
preamble 은 정책상 skip — 매 rev 변동, 가치 낮음 (2,895 rev → SKIPPED).
250 fail (PUT no resp) 은 chunk chain 보존 (같은 chunk 다음 rev 시도
가능). 향후 retry 로 회복 가능.

## 1. 문제 정의

`history-upload` 의 시간순 replay 흐름은 *각 rev 의 본문 전체* 를 PUT.
일지 같은 *시간 누적형* 페이지는 후반 rev 가 본문 한도 초과 → 영구 거부.
Day 6 시점 상위 영향 페이지:

| 페이지 | total_revs | UPLOADED | SKIPPED/거부 | 본문 fallback |
|--------|-----------|----------|-------------|--------------|
| `u:neoocean:2020` | 2,895 | **0** | 0 | C-mode skeleton (별도 트랙) |
| `u:lam:2019` | 5,530 | 5,205 | 325 | 없음 |
| `u:lam:2020` | 3,535 | 3,428 | 106 | 없음 |
| `u:neoocean:2019` | 2,195 | 2,058 | 134 | 없음 |
| `u:neoocean:read` | 308 | 299 | 8 | 없음 |
| ... | | | | |

회복 잠재력: **~3,500 rev / +9.4%p** (86.8% → 96.2%).

`oversized-pages.md` 의 *C 모드* (skeleton + storage zip) 는 *현재 본문*
만 다루고 *history 보존 안 함*. `split-oversize` 명령은 H 단위 자식 분할
이지만 *latest 본문 1회* 만. → **history 분할 보존 트랙 별도 설계 필요**.

## 2. 채택 전략 — heading slug 기반 안정 매핑

### 2.1 핵심 아이디어

큰 페이지를 *자연 경계 (H1/H2/HR 등)* 로 N 개 chunk 분할하고:
- **parent 페이지** 본문 = *작은 인덱스* (chunk 별 자식 페이지 link + 안내)
- **자식 페이지 N 개** 본문 = *그 chunk 의 작은 본문* (한도 안 걸림)
- 각 자식의 *Confluence version chain* = 그 chunk 의 history rev 들

자식 본문이 작아 본문 한도 무관 → *모든 rev 가 보존*.

### 2.2 chunk 식별자 — heading slug

rev 마다 본문이 변하므로 *순번 (chunk_0, chunk_1)* 은 불안정. 대신
*chunk 의 첫 heading text* 의 slug 를 식별자로 사용.

```
chunk 의 첫 heading text  →  slug  →  자식 페이지 매핑
─────────────────────────────────────────────────────
"2020-01 (1월)"        →  "2020-01"   →  child "u:neoocean:2020 – 2020-01"
"2020-02 (2월)"        →  "2020-02"   →  child "u:neoocean:2020 – 2020-02"
"기타 — 분류 안 됨"     →  "_misc"    →  fallback child
```

각 rev 의 같은 slug 는 *같은 자식 페이지의 다음 version* — chain 보존.

### 2.3 chunk 단위 — 페이지마다 H 레벨 선택

| 페이지 패턴 | 권장 chunk H 레벨 | 평균 chunk 크기 |
|------------|------------------|----------------|
| 연 단위 일지 (`u:lam:2019`, `u:neoocean:2020`) | H2 (월/주 단위) | ~50-100KB |
| 누적형 노트 (`u:oh:perforceaddresses`) | H1 (큰 주제) | ~10-30KB |
| 토픽 인덱스 (`u:oh:archives`) | H1 + HR 보조 | ~10-50KB |

`split-oversize` 의 `_split_storage_by_heading(xml, max_chunk, start_level)`
이미 구현 — *재사용*.

## 3. state.db 스키마 (신규)

```sql
CREATE TABLE history_split_pages (
    -- 분할 대상 페이지 + 분할 정책
    doku_id              TEXT PRIMARY KEY,
    chunk_h_level        INTEGER NOT NULL,    -- 1=H1, 2=H2, ...
    chunk_max_size       INTEGER NOT NULL,    -- byte 임계 (자동 merge)
    parent_index_hash    TEXT,                -- parent 본문 (인덱스) 의 해시
    status               TEXT NOT NULL,       -- DISCOVERED / SPLIT / UPLOADED
    last_checked_at      TEXT
);

CREATE TABLE history_split_chunks (
    -- 각 chunk = 자식 페이지 1개
    doku_id              TEXT NOT NULL,       -- parent doku_id
    chunk_slug           TEXT NOT NULL,       -- heading text 의 slug
    chunk_title          TEXT,                -- 자식 페이지 title
    confluence_child_id  TEXT,
    last_synced_rev_ts   INTEGER,             -- 자식이 마지막으로 받은 rev
    last_content_hash    TEXT,                -- 자식 본문의 마지막 hash
    status               TEXT NOT NULL,       -- 'ACTIVE' / 'RETIRED'
    PRIMARY KEY (doku_id, chunk_slug)
);

CREATE TABLE history_split_rev_chunks (
    -- 각 (rev_ts, chunk_slug) → chunk 본문 hash. replay 진행 추적.
    doku_id              TEXT NOT NULL,
    rev_ts               INTEGER NOT NULL,
    chunk_slug           TEXT NOT NULL,
    chunk_body_hash      TEXT NOT NULL,
    chunk_body_len       INTEGER NOT NULL,
    status               TEXT NOT NULL,       -- 'PENDING' / 'UPLOADED' / 'SKIPPED'
    last_error           TEXT,
    PRIMARY KEY (doku_id, rev_ts, chunk_slug)
);
```

## 4. 알고리즘 (5 phase)

### Phase 1: discover — 대상 + 분할 정책

```
python run.py history-split-discover [--threshold 500000] [--only doku_id]
```

- `pages.content_hash` 가 큰 페이지 + history rev 가 본문 한도 초과한
  페이지 후보.
- 각 후보의 latest storage XML 을 `_split_storage_by_heading` 으로 시험
  분할 → chunk 수 / 평균 크기 측정.
- 적정 H 레벨 자동 선정 (chunk 수 4-30, 평균 30-200KB):
  - H2 으로 5+ chunk 가 나오면 H2 채택
  - 안 나오면 H1, fallback HR
- `history_split_pages` 에 INSERT (status='DISCOVERED').

### Phase 2: schema — chunk 인덱스 정의

```
python run.py history-split-define [--only doku_id]
```

- latest storage 를 채택 H 레벨로 분할 → chunk list.
- 각 chunk 의 첫 heading text → slug (한국어 NFC + lowercase + 공백→underscore).
- 너무 작은 chunk (< 5KB) 는 *직전 chunk 에 merge* (별도 자식 페이지
  생성 방지).
- 너무 큰 chunk (> max_size) 는 *H+1 level 재귀 분할*.
- `history_split_chunks` 에 INSERT (status='ACTIVE').

### Phase 3: rev 분할 + 매핑

```
python run.py history-split-convert [--only doku_id]
```

- 각 rev 의 storage XML 을 같은 H 레벨로 분할.
- 각 chunk 의 *첫 heading slug* 와 `history_split_chunks` 의 slug 매칭:
  - 매치 → `history_split_rev_chunks` INSERT (slug 기반)
  - 매치 안 됨 → fallback slug `_misc` 또는 *동적 새 chunk* (heading
    text 추가, 자식 페이지 신규 생성 예약)
- chunk body hash 계산 → 직전 rev 의 같은 chunk hash 와 비교:
  - 동일 → `status='SKIPPED'` (no change, PUT 안 함)
  - 다름 → `status='PENDING'`
- 결과: 각 rev 가 *변경된 chunk 만* 식별.

### Phase 4: child page 생성 + chunk replay

```
python run.py history-split-upload [--only doku_id] [--limit N]
```

- `history_split_chunks` 의 각 slug 마다 자식 페이지 생성:
  - title = `{parent_title} — {chunk_title}`
  - parent = parent doku_id 의 confluence_page_id
  - first rev 본문 = 그 slug 의 첫 등장 rev 의 chunk body
- 이후 `history_split_rev_chunks` 의 PENDING rev 를 *시간순* PUT:
  - 같은 자식 페이지의 다음 version. rev_ts 를 version message 에.
  - chunk body 가 작아 본문 한도 무관.
- `last_synced_rev_ts` 갱신, status='UPLOADED'.

### Phase 5: parent index 본문 갱신

```
python run.py history-split-finalize [--only doku_id]
```

- parent 페이지 본문을 *인덱스* 로 교체:
  ```
  <h1>{title}</h1>
  <note>이 페이지는 본문이 커서 N 개 자식 페이지로 분할 보존됩니다.
        각 자식의 *history* 가 원본 DokuWiki rev 입니다.</note>
  <ul>
    <li><a>chunk_1_title</a> ({last_rev_ts})</li>
    <li><a>chunk_2_title</a> ({last_rev_ts})</li>
    ...
  </ul>
  ```
- C-mode (`large_body_fallback`) 페이지면 meta 제거 → 정상 페이지화.
- parent 의 *history* 는 분할 시점부터 새 chain. *분할 전 history* 가
  필요하면 옵션 D (별도 트랙).

## 5. 결정 사항

### 5.1 대상 페이지 선정 정책

본 인스턴스의 권장 대상 (영향 큰 순):

| # | doku_id | rev | 회복 가능 | 메모 |
|---|---------|-----|---------|------|
| 1 | `u:neoocean:2020` | 2,895 | **2,895 (전부)** | C-mode skeleton 해제 + history 전체 보존 |
| 2 | `u:lam:2019` | 5,530 | ~325 (SKIPPED) | 후반 rev 의 분할 보존 |
| 3 | `u:lam:2020` | 3,535 | ~106 | 동상 |
| 4 | `u:neoocean:2019` | 2,195 | ~134 | 동상 |
| 5 | `u:neoocean:read` | 308 | ~8 | 영향 작음, 우선순위 낮음 |

다른 대형 페이지 (u:oh:2018 3.5MB / u:oh:2017 1.5MB) 는 *현재 UPLOADED*
+ history rev 1-5 개 — 분할 *불필요*.

### 5.2 chunk 단위 자동 결정

| 정책 | 기준 |
|------|------|
| 1순위 | H2 으로 5+ chunk, 평균 ≤ 200KB |
| 2순위 | H1 으로 3+ chunk |
| 3순위 | HR 경계 (split-oversize 의 hr-split 모드) |
| 4순위 | byte 단위 강제 분할 (last resort) |

`split-oversize` 의 휴리스틱 재사용.

### 5.3 fallback `_misc` chunk

- rev 분할 결과의 *맨 처음 chunk* (첫 H heading 이전 본문) 은 schema 에
  없을 수 있음 → slug `_misc` 자식 페이지 생성.
- 또는 *schema 에 정의 안 된 새 heading* → `_misc` 또는 *동적 새 chunk
  생성* 결정. 본 인스턴스는 *_misc 누적* 권장 (자식 페이지 수 폭증 방지).

### 5.4 latest 본문 처리

- 자식 분할 *후* parent 본문은 *인덱스만*. 사용자가 *전체 본문* 볼 수
  없게 됨 (각 자식 따로 봐야).
- 대안: parent 본문에 `<ac:structured-macro ac:name="include">` 로 자식
  본문 inline 전개 — Confluence 의 *page include* 매크로.
- 결정: *기본 인덱스만* (간단). include 매크로는 옵션 (`--with-include`).

### 5.5 history-append-skipped-footer 와의 관계

- 분할 보존 적용 페이지는 *SKIPPED rev 가 없어짐* (모든 rev 가 chunk 단위
  로 보존됨). → footer 부착 대상에서 자동 제외.
- 단, `_misc` 에 누적된 rev 가 한도 초과해 분할 후에도 skip 될 수 있음.

### 5.6 멱등성

- `history_split_chunks.last_synced_rev_ts` 로 마지막 처리 ts 추적.
- `history_split_rev_chunks.status='UPLOADED'` 면 재실행 시 skip.
- parent index 본문은 sentinel `<h1>{title}</h1>` + `<ac:structured-macro
  ac:name="note">` 의 *분할 안내 텍스트* 로 식별. 본문 갱신 시 멱등.

### 5.7 롤백

- 분할 후 *원본 본문 복원 불가능* (parent 본문이 인덱스로 교체됨).
- *Confluence version 1* 이 분할 직전 본문이므로 *parent 의 version
  history* 에서 *복원 가능* — 그러나 자식 페이지들도 같이 제거해야 함.
- `history-split-undo --only doku_id` 옵션 (자식 휴지통 + parent 본문 복원).

## 6. 한계 + 회피

### 6.1 heading text 가 자주 바뀌는 페이지

- 사용자가 "2020-01-15 (수)" → "2020-01-15" 로 헤딩 rename → 다른 slug.
- 옵션 1: *정규화* (`re.sub(r'\s*\(.*?\)', '', text)`) — 괄호 안 무시.
- 옵션 2: *fuzzy slug match* (Levenshtein < 3) — 같은 chunk 로 인식.
- 옵션 3: heading 이전 노드의 *그 rev 의 첫 timestamp / 날짜 패턴* 보조 매칭.

본 인스턴스는 *일지* 패턴이라 옵션 1 으로 90%+ 매칭 예상. 안 맞는 케이스
는 `_misc` 누적.

### 6.2 chunk 가 너무 많이 생기는 페이지

- u:lam:2019 의 5,530 rev × 매월 다른 H2 = 60+ 자식 페이지.
- 회피: H1 단위 (분기/반기) 로 격상 → 4-12 자식.
- 자동 휴리스틱: chunk 수 > 30 → H 레벨 1 격상.

### 6.3 자식 페이지의 navigation 부담

- 자식 60 개 = 사이드바 트리 폭증.
- 회피: parent 본문 + `<ac:structured-macro ac:name="children">` 매크로
  — 트리 동적 표시. 또는 *접기/펴기 (expand)* 매크로.

### 6.4 rev 의 chunk 본문이 *비어 있는* 시점

- 예: 2020-01 chunk 가 2020-03 rev 부터 작성됨. 2020-01 rev 에는 그 chunk
  없음. → 그 rev 에서는 *자식 페이지 미존재* 상태.
- 자식 페이지의 *첫 등장 rev* 가 *그 chunk 가 처음 등장한 rev*. 그 이전
  history 는 빈 본문.
- 결정: *첫 등장 rev 부터* 자식 생성. 이전 rev 는 skip.

### 6.5 large_body_fallback 페이지 (u:neoocean:2020)

- 현재 main 본문 = skeleton. *분할 정의용 latest storage* 가 *원본 본문*
  (storage/u/neoocean/2020.xml) 이지 *Confluence 본문* (skeleton) 이 아님.
- Phase 2 의 schema 정의는 *storage/ 의 원본* 사용.
- Phase 5 의 parent index 본문 갱신 시 `large_body_fallback` 메타 제거.

## 7. 구현 스케치 (서브커맨드)

```
python run.py history-split-discover [--threshold 500000] [--only doku_id] [--list-only]
python run.py history-split-define [--only doku_id]
python run.py history-split-convert [--only doku_id]
python run.py history-split-upload [--only doku_id] [--limit N]
python run.py history-split-finalize [--only doku_id]
python run.py history-split-status [--only doku_id]
python run.py history-split-undo --only doku_id [--confirm]
```

또는 하나의 wrapper:

```
python run.py history-split run --only u:neoocean:2020
```

`history-split run` = discover + define + convert + upload + finalize.

## 8. 본 인스턴스 예상 결과

### 8.1 u:neoocean:2020 (최우선) — **PoC 라이브 완료**

- latest storage: ~349 KB (변환기 개선 후 — 이전 819KB)
- H2 단위 분할 결과 실측: **9 chunk** (8 active + 1 _misc preamble)
- chunk 크기 분포: 2KB ~ 113KB (`2020-01` 113KB 최대)
- 자식 페이지 **8개** 생성 (cid 2532234398 ~ 2533034323)
- chunk rev UPLOADED **3,333**, SKIPPED 10,670 (no-change 7,775 + _misc
  policy 2,895), fail 250 (PUT no resp)
- _misc preamble 은 정책상 skip — 매 rev 변동 + 가치 낮음
- 자식 페이지의 가장 무거운 chunk: `2020-01` 807 version
- parent 본문 → 인덱스로 교체, large_body_fallback 메타 자동 제거

**소요 시간**: history-render 4분 + history-convert 12분 + split-run 40분 = 약 1시간

PoC 검증 결과 설계의 핵심 가정 모두 OK:
- ✅ chunk slug 안정성 (`2020-01` ~ `2020-08` 매칭 일관)
- ✅ chunk chain 보존 (PUT 실패 시 다음 rev OK)
- ✅ cur_ver cached counter 효과 (8배 가속)
- ✅ _misc policy 효과 (2,895 rev × PUT 절약)

개선 적용 결과 (2026-05-24, A1+A3+A5+A2+B3 통합):
- **A1 retry**: 250 PUT no resp → **244 회복** (`history-split-retry --sleep-ms 300`). 잔여 15.
- **A3 동적 schema 확장**: convert phase 가 새 slug 발견 시 schema INSERT.
  검증: 고아 6 chunk (`2020-01-1w` 3 + `링크` 3) → 자식 페이지 2개 추가 생성,
  6 chunk rev 모두 OK.
- **A5 NFC/NFD title**: parent 본문에서 link list 제거 (children 매크로만,
  자동 동기화). NFC/NFD title mismatch 위험 차단.
- **A2 sleep**: `--sleep-ms` 옵션 추가 (retry default 300ms).
- **B3 children only**: parent 본문 = info note + children 매크로. stale link
  list 제거.

**최종 결과:**
- 자식 페이지 **10개** (월별 8 + 보조 2)
- chunk rev UPLOADED **3,583** (이전 3,333 + 244 retry + 6 동적 schema)
- SKIPPED 10,423 (no-change 7,517 + _misc policy 2,895 + PUT 잔여 11)
- 잔여 PUT 실패 15 (추가 retry 가능, 수확 감소로 미진행)
- parent 본문 → children-only 인덱스 (자동 갱신, stale 위험 없음)

B1 (다른 fallback 페이지 재평가): 본 인스턴스에 `large_body_fallback`
메타 = 0 (u:neoocean:2020 PoC 후 정리). 다른 SKIPPED 페이지는 *이미
90%+ UPLOADED* + split 시 사용자가 보는 본문 분할 부작용 → **다른 페이지
적용 안 함. PoC 는 u:neoocean:2020 1 페이지로 종결.**

### 8.2 u:lam:2019 + u:lam:2020 + u:neoocean:2019

- 이미 UPLOADED 90%+. 분할로 *나머지 SKIPPED rev* (565 합계) 회복.
- 자식 페이지 30-50개 (3 페이지 합)
- 회복 rev: 565, UPLOADED 94.5% → 96.0%

### 8.3 합산 회복 잠재력

- u:neoocean:2020: +2,895 rev
- 큰 일지 3 페이지: +565 rev
- 자투리: +50 rev
- **총 +3,510 rev → 86.8% → 96.2%**

자식 페이지 폭증: +50-70개 (parent + children + retired). Confluence 측
2,818 → ~2,880 페이지 (+2%).

## 9. 권고 진행 순서

1. **u:neoocean:2020 1개 페이지로 PoC** — 가장 큰 가치 + C-mode 해제 검증
2. PoC 결과로 chunk 단위 / fallback 정책 다듬기
3. u:lam:2019/2020 / u:neoocean:2019 에 적용 (이미 90%+ uploaded — 위험
   낮음)
4. 자식 페이지 nav 트리 검토 후 children 매크로 추가
5. parent index 본문에 *원본 DokuWiki source* download link (P4 백업 안내)

## 10. 결정 보류 사항

- **chunk 단위 자동 휴리스틱의 임계** (현재 안: H2 5+ chunk) 는 PoC 후
  데이터 기반 조정.
- **자식 페이지의 history 헤더** (`history-rewrite-headers` 와 같이 시각
  포맷) 적용 여부 — 일관성 위해 *적용* 권장.
- **bound page 임베드** — struct 처럼 자식 페이지 목록을 parent 본문
  하단에 자동 임베드할지 — 인덱스 본문에 *이미 link 가 있으므로* 별도
  매크로 불필요.

---

본 시나리오는 *설계 단계*. PoC (u:neoocean:2020 1 페이지) 진행 후 본
문서 §8 결과 절을 *실측치* 로 갱신할 것. PoC 가 성공하면 다른 페이지
일괄 적용 가능.
