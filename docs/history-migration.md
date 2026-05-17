# 히스토리/과거 버전 보존 시나리오

본 문서는 [`scenarios.md` §3 비범위](scenarios.md) 에서 명시적으로 제외했던
"DokuWiki 페이지의 과거 리비전 / 변경 이력 / 미디어 히스토리"를 Confluence
Cloud 로 이전할 때의 옵션과 트레이드오프를 정리한다. 이 문서를 검토한
뒤 사용자가 어느 옵션을 채택할지 결정하면 구현 PR 을 분리해 진행한다.

## 1. 문제 정의

원래 파이프라인은 *현재 시점의 DokuWiki 페이지가 렌더링된 최종 상태*만
이전한다. 다음은 모두 손실된다:

- 페이지의 과거 텍스트 리비전 (`data/attic/<ns>/<page>.<unix-ts>.txt.gz`)
- 누가 언제 무엇을 어떻게 바꿨는지 (`data/meta/<ns>/<page>.changes`)
- 미디어의 과거 버전 (`data/media_attic/`)
- 미디어 변경 이력 (`data/media_meta/<ns>/<media>.changes`)
- "변경됨" 표시(코어), 사용자 계정 매핑

이걸 어디까지 보존할지, 어떤 형태로 옮길지 결정하는 것이 본 문서의 목적.

## 2. DokuWiki 측 데이터 (2026-05-18 기준 실측)

| 항목                   | 크기   | 항목 수                          |
|------------------------|--------|----------------------------------|
| `data/attic/`          | 955 MB | 37,287 리비전 / 1,630 고유 페이지 |
| `data/media_attic/`    | 522 MB | 193 파일                         |
| `data/meta/.../*.changes` | <1 MB  | 페이지마다 한 파일               |

페이지별 리비전 평균 ~23. 상위 네임스페이스는 `u/oh` (11,662), `u/lam`
(10,775), `u/neoocean` (7,206) — 일지 형식 페이지가 대부분의 리비전을
차지한다.

### 2.1 `attic` 파일 명명

```
attic/<ns_path>/<page>.<unix_timestamp>.txt.gz
attic/u/lam/2020.1577065128.txt.gz
attic/u/lam/2020.1578012867.txt.gz
...
```

`.gz` 로 압축된 raw DokuWiki 마크업. 디컴프레션 후 dokuwiki 의 `?rev=`
파라미터로 그 시점 렌더링을 받을 수 있다.

### 2.2 `changes` 로그 포맷

`meta/<ns_path>/<page>.changes` — 페이지당 한 파일, TSV.

```
<unix_ts>\t<ip>\t<type>\t<page_id>\t<user>\t<comment>\t<extra>
1577065128	69.169.18.21	C	u:lam:2020	neoocean	만듦	345
1578012867	5.181.235.199	E	u:lam:2020	neoocean	[2019-01-03] 	869
1578013424	5.181.235.199	e	u:lam:2020	neoocean	할 일 선택됨: ...	21
```

type 코드:

- `C` create
- `E` edit (일반 수정)
- `e` minor edit (소소한 수정 플래그)
- `R` revert (과거 버전으로 되돌리기)
- `D` delete

마지막 `extra` 컬럼은 변화량(byte delta) 이거나 추가 정보.

### 2.3 DokuWiki 의 과거 버전 렌더링 API

`/doku.php?id=<page>&rev=<unix_ts>&do=export_xhtmlbody` 로 attic 의
특정 시점 본문을 정상 export 와 동일한 형식의 XHTML 본문으로 받을 수
있다. 즉 변환 파이프라인(`render` → `convert`) 을 그대로 재활용 가능.

## 3. Confluence Cloud 측 제약

**핵심 제약: 버전의 `createdAt` 과 `author` 를 백데이트할 수 없다.**

- `POST /wiki/api/v2/pages` → 항상 version 1, `version.createdAt = now()`,
  `version.authorId = <인증된 API 사용자>` 로 고정.
- `PUT /wiki/api/v2/pages/{id}` → version N+1, 동일하게 now/현재 사용자.
- 어드민 / 클라우드 어드민 API 모두 history 항목을 backdate 하는
  공개 엔드포인트가 없다.
- "내가 *원본* dokuwiki 사용자 ID 를 변경자로 표시하고 싶다" → API 로 불가.
  Confluence 사용자 계정으로의 매핑이 있어야 하고 그래도 *변경 시각*은
  여전히 now.

따라서 *"진짜 버전 히스토리가 그대로 옮겨가는 마이그레이션"* 은 Confluence
정책상 불가능. 옵션은 **버전 체인의 *구조*만 보존하되 시각/작성자는 본문
안에 메타 텍스트로 박는** 형태 또는 **별도 보관소**로 격하하는 형태로 나뉜다.

## 4. 옵션 매트릭스

| 옵션 | 보존 대상                                              | Confluence UI 노출                            | 구현 복잡도 | API 호출 수 (1569 × 23 ≈ 36k 리비전) | 손실/한계                                                        |
|------|--------------------------------------------------------|-----------------------------------------------|-------------|--------------------------------------|------------------------------------------------------------------|
| A    | 최신 버전만 + 본문 푸터에 요약 메타                    | 페이지 끝 작은 박스: "원본 dokuwiki 페이지로부터 마이그레이션됨. 최초 작성: 2020-01-23 / 마지막 수정: 2024-12-15 / 총 변경 횟수: 27" | 낮음 (현 변환기에 푸터 한 줄 추가) | 0 추가                               | 과거 본문 내용 자체는 보존 안 됨. 누가 무엇을 바꿨는지 불명. |
| B    | 시간순으로 모든 리비전을 PUT 으로 재생 → Confluence 버전 체인 보존 | "이전 버전 보기" UI 에서 각 버전 diff 가능. 본문 헤더에 원본 ts/user/comment 박힘 | 높음        | 36,000+ (페이지당 평균 23)            | createdAt 은 모두 now, authorId 모두 API 사용자. UI 의 "수정자" 컬럼은 무의미. |
| C    | 별도 자식 페이지 `<Page>/히스토리` 생성, 모든 리비전 본문 또는 changes.log 정리 | 사이드 트리에 자식 페이지 1개. 클릭하면 시간순 리비전 묶음 | 중간        | 페이지당 +1                           | 본격적인 diff 비교는 안 됨 (텍스트 나열). 트리에 잡음 페이지가 생김. |
| D    | content property 에 changes.log + attic 인덱스 JSON 저장 | UI 비표시 (개발자 도구로만 조회). 추후 재구축 가능 | 낮음        | 페이지당 +1                           | 사용자에게 직접 보이지 않음. 별도 viewer 필요. |
| E    | attic 의 raw 마크업 파일들을 페이지 첨부로 일괄 업로드 | 첨부 섹션에 `<page>.<ts>.txt` N개            | 낮음        | 페이지당 +N (37k 첨부)               | 마크업 그대로라 사용자가 읽기 어렵다. 첨부 개수 폭증. |
| F    | 미디어 attic 만 Confluence 첨부 버전 체인으로 PUT | 첨부의 "이전 버전" UI                         | 중간        | 미디어 평균 ~3 버전 × 193 ≈ 600     | 미디어 attachment 도 backdate 불가; 텍스트 페이지 미해결 |

## 5. 권장 조합

사용자가 "히스토리 *데이터*만 잃지 않으면 된다" 라면:

- **Default = A + D** (가벼움, 사용자가 거의 항상 만족하는 균형)
  - 메인 페이지 본문 끝에 §A 푸터 박스.
  - 페이지 content property `dokuwiki.history` 에 changes.log 전체와
    attic 파일 인덱스(파일명 + sha256 + byte size) 를 JSON 으로 저장.
  - attic 의 raw `.txt.gz` 자체는 *Confluence 에 올리지 않음*. 호스트
    P4 백업에 이미 존재하므로 단일 source of truth 유지.

사용자가 "Confluence UI 의 *버전 보기*에서 과거 본문을 직접 비교할 수
있어야 한다" 라면:

- **B + 본문 헤더 메타** (실현 가능한 최대 보존)
  - `history-replay` 서브커맨드로 페이지마다 시간순 PUT.
  - 각 버전 본문 최상단에 회색 박스:
    ```
    > 이 리비전은 DokuWiki 의 2020-01-23 03:18 UTC 시점 본문입니다.
    > 작성자: neoocean (5.181.235.199), 변경 코멘트: "[2019-01-03] "
    ```
  - 페이지의 *Confluence 버전 N* 은 dokuwiki revision N 에 대응. 다만
    `version.createdAt` / `version.authorId` 는 마이그레이션 시각과
    API 사용자.

미디어:

- **F** 단독 또는 **A 의 푸터에 "원본 미디어 N개" 만 명시**.
  실측 522MB / 193 파일이라 작은 편이라 F 가 부담 없다.

## 6. 구현 스케치

### 6.1 새 서브커맨드

```
python run.py history-discover   # attic + changes 인덱싱
python run.py history-render     # attic 의 각 리비전을 ?rev= 로 캐시
python run.py history-convert    # 리비전별 storage XML 생성
python run.py history-upload     # 선택된 옵션(A/B/C/D/E) 에 따라 업로드
```

### 6.2 state.db 스키마 확장

```sql
CREATE TABLE revisions (
    doku_id              TEXT NOT NULL,
    rev_ts               INTEGER NOT NULL,   -- unix epoch
    type                 TEXT,               -- C / E / e / R / D
    user                 TEXT,
    ip                   TEXT,
    comment              TEXT,
    extra                TEXT,
    attic_path           TEXT,               -- on-disk .txt.gz 위치
    raw_xhtml_path       TEXT,               -- ?rev= 로 받은 캐시 위치
    storage_path         TEXT,               -- 변환 결과
    content_hash         TEXT,               -- 변환 storage 의 sha256
    status               TEXT NOT NULL,      -- DISCOVERED / RENDERED / CONVERTED / UPLOADED / SKIPPED / FAILED
    last_error           TEXT,
    last_checked_at      TEXT,
    PRIMARY KEY (doku_id, rev_ts)
);
CREATE INDEX revisions_doku_idx ON revisions(doku_id);
CREATE INDEX revisions_status_idx ON revisions(status);

CREATE TABLE history_meta (
    doku_id        TEXT PRIMARY KEY,
    total_revs     INTEGER,
    first_ts       INTEGER,
    last_ts        INTEGER,
    confluence_property_id TEXT,   -- 옵션 D 의 content property
    history_child_page_id  TEXT    -- 옵션 C 의 자식 페이지
);
```

### 6.3 본문 푸터 (옵션 A) 의 storage XML 모양

```xml
<ac:structured-macro ac:name="info">
  <ac:parameter ac:name="title">DokuWiki 원본 정보</ac:parameter>
  <ac:rich-text-body>
    <p>이 페이지는 자체 운영 중인 DokuWiki 에서 마이그레이션되었습니다.</p>
    <ul>
      <li>최초 작성: <time>2020-01-23T03:18:48+09:00</time> — neoocean</li>
      <li>마지막 수정: <time>2024-12-15T20:30:01+09:00</time> — neoocean</li>
      <li>총 변경: 27회 (C 1 / E 19 / e 7 / R 0)</li>
    </ul>
  </ac:rich-text-body>
</ac:structured-macro>
```

### 6.4 시간순 replay (옵션 B) 의 본문 헤더

각 리비전 본문 최상단에:

```xml
<ac:structured-macro ac:name="note">
  <ac:rich-text-body>
    <p>DokuWiki revision: <time>2020-01-23T03:18:48+09:00</time></p>
    <p>Author: neoocean — IP 5.181.235.199</p>
    <p>Type: E — comment: <code>[2019-01-03]</code></p>
  </ac:rich-text-body>
</ac:structured-macro>
```

### 6.5 content property (옵션 D) 의 JSON 모양

`PUT /wiki/api/v2/pages/{id}/properties` body:

```json
{
  "key": "dokuwiki.history",
  "value": {
    "schema": 1,
    "total_revisions": 27,
    "first_ts": 1577065128,
    "last_ts": 1734272001,
    "changes": [
      {"ts": 1577065128, "type": "C", "user": "neoocean", "ip": "69.169.18.21", "comment": "만듦", "extra": "345"},
      ...
    ],
    "attic": [
      {"ts": 1577065128, "file": "u/lam/2020.1577065128.txt.gz", "size": 1327, "sha256": "..."},
      ...
    ]
  }
}
```

## 7. 데이터량 / 시간 추정

| 시나리오 | API 호출 수 | 60 RPM 기준 소요 | 100 RPM 기준 |
|----------|-------------|------------------|--------------|
| A 만     | 0 추가       | 0                | 0            |
| A + D    | 1,569 추가   | ~27분            | ~16분        |
| A + C    | 1,569 추가   | ~27분            | ~16분        |
| B (전체) | ~37,287 PUT  | ~10시간 25분     | ~6시간 12분  |
| E (첨부) | ~37,287      | 동일             | 동일         |
| F (미디어) | ~600        | ~10분            | ~6분         |

Confluence Cloud 의 실효 rate limit 은 변동성 큼 (plan, 동시성, 시간대).
백오프 + 재시도가 이미 변환기에 있으니 기능적으로는 안전. **B 는
하룻밤 돌리는 비동기 잡** 으로 봐야 한다.

## 8. 사용자 매핑

DokuWiki 의 user 칼럼은 dokuwiki 의 로컬 사용자명 (`neoocean`, `lam`).
Confluence 사용자 accountId 로의 매핑은 별도 작업.

- **권장**: 매핑 안 함. 본문 메타에 dokuwiki 사용자명 텍스트로 박음.
- **선택**: `users.json` (CLI 인자로 받음) 으로
  `{"neoocean": "<confluence-accountId>"}` 매핑 제공 시 본문 메타에
  `@mention` 으로 표시. 다만 *변경자* 필드는 여전히 API 사용자로 고정.

## 9. 미디어 히스토리 (별도 결정)

193 파일 / 522MB. 옵션:

- 무시 (가장 단순). 호스트 P4 에 백업 있음.
- F: Confluence 첨부의 *버전* 체인으로 시간순 업로드. 첨부 페이지는
  `media_attic/<ns>/<file>.<ts>.<ext>` 의 파일명에서 메인 미디어 매핑.
  업로드 후 첨부의 "이전 버전" 메뉴에서 과거 파일 다운로드 가능.

권장: 옵션 무시 + content property `dokuwiki.media_history` 에 인덱스만
저장 (필요 시 호스트에서 재구성).

## 10. 결정 보류 항목

1. **사용자가 어떤 옵션을 채택할지** — A+D (가벼움) / B (UI 비교 가능, 시간 큼) / 둘의 혼합.
2. **삭제된 페이지 (`D` 타입)** 처리: Confluence 에 없는 페이지에 history 만 박을 수 없다. 별도 "삭제된 페이지 묶음" 페이지에 모두 모을지, 아예 무시할지.
3. **revert (`R`)** 가 만든 리비전: dokuwiki 가 새 revision 으로 기록하지만 본문은 과거 revision 의 복제. 옵션 B 에선 중복 버전 생성됨. dedup 룰 (content_hash 같으면 skip) 필요.
4. **dokuwiki 사용자 매핑 JSON** 의 출처와 검증 절차.
5. **공개 vs 비공개**: 일부 페이지는 ACL 로 막혀있었다. 히스토리에 그런 페이지의 과거 본문이 들어가면 *그 시점의 권한 상태* 가 사라진다. 신뢰선 재검토 필요.
6. **언어/문자 인코딩**: attic 의 일부 오래된 파일이 깨진 인코딩일 수 있다. 첫 단계에서 디코드 실패 케이스 통계 필요.

## 11. 다음 단계

1. 사용자가 `§4` 매트릭스 중 한 가지(또는 조합)를 채택.
2. 채택안에 맞춰 `§6` 의 서브커맨드와 schema 구현을 별도 PR 로 진행.
3. 작은 샘플 페이지 (예: `wiki:syntax`) 에 대해 dry-run 으로 검증.
4. 전체 corpus 에 대해 비동기 실행, 결과 audit.
