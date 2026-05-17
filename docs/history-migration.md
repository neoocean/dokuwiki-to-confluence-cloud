# 히스토리/과거 버전 보존 시나리오

본 문서는 [`scenarios.md` §3 비범위](scenarios.md) 에서 명시적으로 제외했던
"DokuWiki 페이지의 과거 리비전 / 변경 이력 / 미디어 히스토리"를 Confluence
Cloud 로 이전할 때의 옵션과 트레이드오프를 정리한다.

**채택 결정 (2026-05-18): 옵션 B — 시간순 replay 로 Confluence 버전 체인
보존.** §5 가 채택 결정과 그 근거. §6 의 구현 스케치는 B 에 맞춰
구체화되어 있다. §4 의 다른 옵션들은 *선택 가능한 보조 모드* 또는
*미래 대안* 으로 그대로 유지한다 — `--mode` 플래그로 차후 전환 가능
하도록 구현한다.

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

## 5. 채택 결정

### 5.1 결정 (2026-05-18)

**옵션 B + 본문 헤더 메타** 를 텍스트 페이지 히스토리의 default 모드로
채택. 미디어 히스토리는 **옵션 F** 채택.

### 5.2 채택 이유

- 사용자 요구: Confluence UI 의 "이전 버전 보기"에서 과거 본문을
  직접 비교할 수 있어야 한다.
- 본문 안에 dokuwiki 의 원본 ts/user/comment 를 헤더 박스로 같이 박으면
  *Confluence 의 버전 메타가 마이그레이션 시각/계정으로 고정*되는 한계
  도 본문 텍스트 차원에서는 보완된다.
- 추가 API 호출 ~37,287 PUT 은 부담이지만 일회성 마이그레이션이라
  허용 가능. 백오프 + 재시도가 이미 변환기에 있다.

### 5.3 옵션 B 와 다른 옵션의 관계

§4 의 다른 옵션(A / C / D / E / F) 은 **삭제하지 않고 유지**한다:

- **A (본문 푸터 메타)** — B 와 함께 사용. 메인 페이지(latest revision)
  의 본문 끝에 *전체 변경 요약* 박스를 같이 박는다. B 의 헤더 박스가
  각 버전의 metadata 라면, A 의 푸터는 페이지 라이프타임 합산 metadata.
- **D (content property JSON)** — 미래 대안 / 보조. B 채택했어도 추후
  외부 도구가 dokuwiki 원본 changes.log 를 기계적으로 조회해야 할 경우
  `history-upload --include-property` 플래그로 함께 저장 가능하게
  남긴다. 디폴트는 비활성.
- **C (자식 페이지)** — 미래 대안. B 채택 후 사용자가 "버전 비교 UI 보다
  단일 페이지에 모든 변경이 나열되는 게 좋다" 면 `--mode=child-page` 로
  전환. 본 PR 범위 밖.
- **E (attic raw 첨부)** — 보존 가치 낮음 (호스트 P4 백업에 이미 raw 가
  있음). 폐기보단 *옵션 자체는 유지*. `--mode=raw-attachments` 만
  남겨둔다. 일반 권장 안 함.
- **F (미디어 attic 첨부 버전 체인)** — *함께 채택*. 미디어는 522MB /
  193 파일이라 부담 적고 첨부의 "이전 버전" UI 가 자연스럽다.

### 5.4 채택안 한 줄 요약

> 텍스트는 **B + A** (시간순 replay + latest 페이지 푸터),
> 미디어는 **F** (Confluence 첨부 버전 체인).
> D / C / E 는 `--mode` 플래그로 옵션만 유지, 디폴트 비활성.

## 6. 구현 스케치 (B + A + F 채택안 기준)

### 6.1 새 서브커맨드

```
python run.py history-discover   # attic + changes + media_attic 인덱싱
python run.py history-render     # attic 의 각 리비전을 ?rev= 로 캐시
python run.py history-convert    # 리비전별 storage XML 생성 + 헤더 박스 prepend
python run.py history-upload     # 시간순 PUT replay (B). --include-footer 로 latest 페이지에 A 푸터 추가
python run.py history-media      # F: media_attic 의 시간순 PUT (Confluence 첨부 버전 체인)
```

기본 동작은 채택안 (B + A + F). `--mode {chronological|footer-only|child-page|content-property|raw-attachments}`
플래그로 다른 옵션을 명시적으로 선택 가능 (default = `chronological`).

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
    replay_started_at      TEXT,    -- B replay 시작 시각
    replay_completed_at    TEXT,    -- B replay 완료 시각
    last_replayed_rev_ts   INTEGER, -- 재개용. 다음 PUT 대상의 다음 ts.
    confluence_property_id TEXT,    -- 옵션 D (보조)
    history_child_page_id  TEXT     -- 옵션 C (보조)
);

CREATE TABLE media_revisions (
    media_id           TEXT NOT NULL,   -- 예: 'wiki:foo.png'
    rev_ts             INTEGER NOT NULL,
    src_path           TEXT,            -- media_attic/<...>.<ts>.<ext>
    size               INTEGER,
    sha256             TEXT,
    confluence_attachment_id TEXT,      -- F 업로드 후
    status             TEXT NOT NULL,   -- DISCOVERED / UPLOADED / FAILED / OVERSIZED
    last_error         TEXT,
    uploaded_at        TEXT,
    PRIMARY KEY (media_id, rev_ts)
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

### 6.5 B replay 알고리즘 (채택안 핵심)

1. `history-discover` 가 `revisions` 테이블을 채운다 (페이지당 모든
   `attic/.../*.txt.gz` + `meta/.../*.changes` 행 cross-join).
2. `history-render` 가 status=DISCOVERED 인 행을 `?rev=<ts>` 로 받아
   `raw_history/<doku_id>.<ts>.html` 에 캐시. 본문이 변경 없는
   리비전(같은 sha256) 도 일단 캐시 — dedup 은 convert 단계에서.
3. `history-convert` 가 `_convert_html_to_storage` 를 재사용해
   리비전별 storage XML 을 만든 뒤, *§6.4 의 헤더 박스를 본문
   최상단에 prepend*. revert 로 인한 본문 동일 리비전은
   `content_hash` 비교로 식별해 status='SKIPPED' (선택; 디폴트는
   skip — Confluence 의 버전 N 가 N-1 과 본문이 같으면 PUT 거부됨).
4. `history-upload` 가 페이지별로 작업:
   - 페이지의 `confluence_page_id` 가 비어있으면, 가장 오래된 revision
     으로 POST 하여 version 1 생성.
   - 그 다음 revisions 를 ts 오름차순으로 PUT. 각 PUT 의 `body.value`
     는 `storage_path` 의 콘텐츠, `version.number = 직전 cur_ver + 1`,
     `version.message = "DokuWiki rev <ts> by <user>: <comment>"`
     (Confluence 가 받아들이는 *짧은* 코멘트).
   - PUT 후 `revisions.status = UPLOADED` + `history_meta.last_replayed_rev_ts`
     갱신. 중단/재시작 안전.
   - 429 는 기존 `_request_with_retry` 의 백오프 적용.
   - 마지막 revision = 현재 dokuwiki latest. 이건 메인 파이프라인의
     `upload` 가 만든 페이지의 *최종 버전*과 동일해야 한다. 메인
     `upload` 가 이미 만든 페이지를 재활용해 history-upload 가
     이어서 N-1 개 PUT 을 추가하는 형태.

### 6.6 메인 파이프라인과의 통합

기존 `upload` 가 페이지를 만들고 latest 본문 + (옵션 A) 푸터 박스를
함께 박는다. `history-upload` 는 그 페이지에 이어서 *오래된 → 새것*
순으로 PUT 을 던진다 — 단, 시간 순서를 *지키려면 latest 가 마지막에
와야 한다*. 그래서 실제 순서는:

  1. 메인 `upload` 가 페이지 *없으면* skeleton 만 생성 (제목 + stub
     본문 + 푸터). `history-upload` 가 가장 오래된 revision 으로 첫
     PUT 을 던지면 버전 2 가 됨.
  2. 또는 `upload --skip-create-when-history` 로 메인 단계를 생략하고
     `history-upload` 가 첫 POST 부터 책임진다. 이쪽이 깔끔. 채택.

→ 결과: Confluence 페이지의 *version N* = dokuwiki 의 *N번째 revision*.
   최종 버전이 dokuwiki 의 latest = 메인 파이프라인의 출력과 일치.

### 6.7 content property (옵션 D, 보조) 의 JSON 모양

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

## 10. 결정 항목 (채택안에서 어떻게 풀렸는지)

| # | 항목 | 상태 |
|---|------|------|
| 1 | 어떤 옵션을 채택할지                          | **결정 (2026-05-18): B + A + F** (§5.1) |
| 2 | 삭제된 페이지 (`D` 타입) 처리                | **결정**: 별도 페이지 `<root>/_삭제된_페이지/` 자식 트리에 모두 모은다. 본문은 마지막 살아있던 revision 의 변환 결과 + 푸터에 "DokuWiki 에서 <ts> 에 삭제됨" 표시. 시간순 replay 는 적용하지 않음 (Confluence trash 와 별개). |
| 3 | revert (`R`) 가 만든 리비전 중복 처리          | **결정**: convert 단계에서 직전 revision 과 동일한 `content_hash` 면 status=SKIPPED. Confluence 가 동일 본문 PUT 을 어쨌든 거부하므로 사전 필터링이 안전. |
| 4 | dokuwiki 사용자 매핑 JSON 의 출처와 검증       | **보류**: 일단 매핑 없이 dokuwiki 사용자명을 본문 헤더에 텍스트로 기록. 사용자가 매핑 JSON 을 제공하면 `--users-map` 플래그로 받아 헤더에서 `@mention` 으로 렌더. 검증 절차는 매핑 도입 시 별도. |
| 5 | ACL-locked 과거 본문 노출                     | **결정**: 호스트 운영자(=사용자 본인)의 명시적 단독 환경. 마이그레이션 대상 공간은 사용자가 별도로 정한 access boundary 안. 따라서 *과거 ACL 은 보존하지 않는다* — Confluence 측 권한이 통일된 단일 boundary 다. 추후 다인 환경으로 확장하면 재검토. |
| 6 | 인코딩 깨짐 통계                              | **계획**: `history-discover` 단계에서 attic 의 각 `.txt.gz` 를 `errors='replace'` 로 디코드하고, replacement char (`�`) 가 들어간 리비전을 `revisions.last_error='decode-replaced'` 로 마킹. discover 종료 시 통계 출력. 실제 비율은 첫 실행으로 측정. |

남은 *진짜 결정* 항목은 #4 (사용자 매핑) 뿐. 나머지는 채택안의 동작이
정의되어 있다.

## 11. 다음 단계 (구현 로드맵)

1. **discover 단계**: `attic/` walk + `meta/*.changes` 파싱 → `revisions`
   + `history_meta` 테이블 채움. 인코딩 깨짐 통계(§10 #6) 함께 출력.
   `media_attic/` 도 walk → `media_revisions` 채움.
2. **render 단계**: dev 컨테이너의 `?rev=<ts>` 로 페이지별 모든
   revision 받기. ~37k 호출. 컨테이너에 부담 안 가게 `--delay` 디폴트
   상향 (예: 200ms). 1차 estimate: ~2시간.
3. **convert 단계**: 메인 `_convert_html_to_storage` 재사용. 본문에
   §6.4 헤더 박스 prepend. content_hash 동일 (revert 결과) revision
   은 status=SKIPPED. ~5분 (cpu-bound, bs4).
4. **upload 단계** (`history-upload`): §6.5 알고리즘. 페이지별로
   ts 오름차순 POST → PUT 반복. ~37k API 호출 → 비동기 백그라운드.
   재개 가능 (`last_replayed_rev_ts` 기반).
5. **media 단계** (`history-media`): 미디어별 ts 오름차순 첨부
   업로드. ~600 호출.
6. **사후 검증**: 임의의 5개 페이지를 골라 Confluence 의 "이전 버전 보기"
   에서 dokuwiki revision N 과 Confluence 버전 N 의 본문 일치 + 헤더
   메타 정확성 점검.
7. **삭제된 페이지** 처리 (§10 #2): `meta/_dokuwiki.changes` 의 `D` 타입
   이벤트로부터 deleted page 목록 추출 → `<root>/_삭제된_페이지/` 트리
   생성 → 마지막 revision 변환 + 푸터 박스로 마무리.
