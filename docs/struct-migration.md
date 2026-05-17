# DokuWiki struct → Confluence 데이터베이스 이전 시나리오

본 문서는 [`plugin-validation.md` §6.3](plugin-validation.md) 에서 채택된
**"schema → Confluence DB 매크로 매핑"** 옵션의 구체 설계다. 텍스트/표
스냅샷이 아니라 *Confluence 측에서도 데이터베이스로서 동작*하도록 옮기는
것이 목표.

**채택 결정 (2026-05-18)**: 옵션 3 — schema 를 Confluence 데이터베이스로
매핑. §4 의 우선순위 (A 우선 → 미지원 컬럼 / API 미성숙 시 B 폴백 →
최후 C 스냅샷) 로 단계 적용.

## 1. 현재 상태와 목표

- DokuWiki struct 데이터는 `data/meta/struct.sqlite3` 의
  `data_<schema>` / `multi_<schema>` 테이블에 직접 들어있다. 플러그인
  코드(`lib/plugins/struct/`) 는 이 인스턴스에서 부재해 *렌더링되지
  않지만 데이터는 보존됨*.
- 페이지 마크업에는 struct syntax 가 0건이라 메인 파이프라인의
  render/convert 가 직접 손대지 않는다.
- 따라서 struct 이전은 *별도 트랙*: sqlite 를 직접 읽어 Confluence
  데이터베이스로 옮긴다.

## 2. DokuWiki 측 데이터 (2026-05-18 측정)

### 2.1 활성 schema (`schemas.ts` 의 최신 버전만 사용)

| sid | tbl                    | columns | rows (latest=1) | 데이터 특성                                                  |
|-----|------------------------|---------|------------------|--------------------------------------------------------------|
| 72  | `brevet_uri_cppage`    | 3       | 265              | URI ↔ checkpoint page 매핑                                   |
| 80  | `brevet_event`         | 26      | 106              | 브레베 이벤트 (날짜, GPX/지도/프로필 미디어, 외부 URL, 페이지 링크) |
| 81  | `brevet_course`        | 6       | 744              | 코스의 checkpoint 시퀀스 (CP명, event id, 순번, type, place)  |
| 83  | `brevet_place`         | 4       | 98               | CP 장소 (이름, …)                                            |
| 38  | `test`                 | 2       | 0                | 빈 스키마 — skip                                             |

총 **1,213 활성 row** + 26 컬럼 짜리 풍부한 `brevet_event` 가 가장 무겁다.

### 2.2 column type 분포 (관측된 클래스)

`types.class` 에서 본 클래스:

| DokuWiki 클래스 | 의미                                       | 데이터 모양 (관측)                              |
|------------------|--------------------------------------------|-------------------------------------------------|
| Text             | 단일 줄 텍스트                             | `'2019-s200d-1'`, `'Brevet'`, `'서울'`          |
| Decimal          | 숫자 (정수/소수)                           | `202`, `2097`                                   |
| Date             | 날짜                                       | `2019-03-01`                                    |
| Url              | 외부 URL                                   | `http://korearandonneurs.kr/...`                |
| Media            | DokuWiki 미디어 id                         | `:b:files:s-200k-d-2019.gpx`                    |
| Wiki             | DokuWiki page id                           | `[[:b:2019-s200d-1\|동탄 200k]]`                |
| Dropdown         | 고정 옵션 (`types.config` 에 옵션 정의)    | `'Brevet'`, `'Day'`                             |
| Lookup *(미관측)*| 다른 schema 의 row 참조                    | -                                               |
| User *(미관측)* | DokuWiki 사용자                            | -                                               |

`types.config` 는 JSON 으로 각 컬럼의 prefix/postfix/label/options/검증
규칙 등을 담는다. 매핑 시 옵션 목록 등을 같이 추출해야 함.

### 2.3 페이지 binding

`schema_assignments_patterns` 와 `schema_assignments` 가 *모두 비어있다*.
이 인스턴스에서는 *글로벌 schema 적용 패턴이 정의되어 있지 않다* —
즉 어떤 페이지에 어떤 schema 가 자동 부착되는지의 매핑 자체가 없다.
대신 row 의 `pid` (integer PK) 가 *자체 식별자* 로 쓰이고, 페이지 ↔
row 관계는 row 내부의 Wiki 타입 컬럼 (예: `brevet_event.col21` 의
`[[:b:2019-s200d-1|...]]`) 또는 row 자체가 별도 페이지로 다뤄짐.

전략: row → 페이지 매핑이 명시적이지 않으므로 **schema 를 통째로
독립 Database** 로 옮기고, Wiki 타입 컬럼이 있으면 그 컬럼이 *page
reference* 가 되어 메인 마이그레이션의 Confluence 페이지를 가리키도록
한다 (S7 의 dwc-link placeholder 와 동일한 2-pass).

## 3. Confluence 측 옵션

### 3.1 A. Confluence Native Database (2024년 도입, 최우선)

Confluence Cloud 가 native database content type 을 지원한다 (atlassian-
documents-database). 페이지처럼 트리 안에 두는 컨텐츠 객체이고, 컬럼
타입을 정의하면 그 타입대로 입력/표시/필터/정렬이 된다.

- API 진입점: `/wiki/api/v2/databases` (생성/조회), `/wiki/api/v2/databases/{id}/rows` (row 입력)
- 컬럼 타입: Text, Number, Date, Person, Status, URL, Tag, Confluence Page Link, Database Reference, Files 등 — DokuWiki struct 의 대부분 클래스에 대응.
- Storage format 에서 임베드: `<ac:link><ri:database ri:database-id="<id>"/></ac:link>` (페이지 본문 안에서 참조 가능).

**리스크**: 2024-2026 사이 API 가 변동성 있게 진화 중. 실제 마이그레이션
실행 시점에 (a) 컬럼 정의 API, (b) row 입력 API, (c) Database Reference 가
프로그래밍적으로 설정 가능한지 *재확인* 필요. 미지원 항목이 있으면 자동
B 로 폴백.

### 3.2 B. Page Properties + Page Properties Report (전통 매크로, 폴백)

Confluence 의 오래된 표준 매크로 조합:

- 각 row → 별도 자식 페이지(또는 row 가 binding 된 dokuwiki 페이지 본문 안)
  에 `Page Properties` 매크로:
  ```xml
  <ac:structured-macro ac:name="details">
    <ac:rich-text-body>
      <table>
        <tr><th>code</th><td>2019-s200d-1</td></tr>
        <tr><th>year</th><td>2019</td></tr>
        ...
      </table>
    </ac:rich-text-body>
  </ac:structured-macro>
  ```
  + 페이지에 label `dokuwiki-struct-<schema>` 자동 추가.
- 인덱스 페이지(스키마당 하나) → `Page Properties Report`:
  ```xml
  <ac:structured-macro ac:name="detailssummary">
    <ac:parameter ac:name="cql">label = "dokuwiki-struct-brevet_event"</ac:parameter>
  </ac:structured-macro>
  ```
  → CQL 로 라벨 매칭 페이지를 모아 표로 보여줌. 필터/정렬은 매크로 옵션.

**손실**: 컬럼 타입 정보 (Number/Date/Url 의 형식 강제는 *시각적 보존만*,
입력 검증 없음). Database Reference 도 단순 텍스트 페이지 링크로.

### 3.3 C. 단순 표 스냅샷 (최후 폴백)

각 schema 의 row 들을 하나의 큰 `<table>` 로 만들어 한 페이지에 박는다.
*plugin-validation.md §6 의 "스냅샷 only" 동작과 같음*. 컬럼 타입/필터/정렬 없음.

### 3.4 우선순위와 폴백 규칙

```
default mode = "native"   # A
  ↓ (Database 생성 API 실패 또는 컬럼 타입 미지원 시 자동 폴백)
fallback     = "properties"  # B
  ↓ (Page Properties Report 매크로도 거부될 때)
last-resort  = "snapshot"    # C
```

각 schema 마다 폴백 결정을 별도로 내릴 수 있다 — 컬럼 타입이 단순한
schema (`brevet_uri_cppage`) 는 A 로, 복잡한 schema (`brevet_event` 의
Media/Wiki/Url 혼합) 는 A 가 부분 실패하면 그 schema 만 B 로 격하.

## 4. 컬럼 타입 매핑

| DokuWiki struct | Confluence Native (A)           | Page Properties (B)               | 비고                                                 |
|-----------------|----------------------------------|------------------------------------|------------------------------------------------------|
| Text            | Text                             | `<td>text</td>`                    | 직진                                                 |
| Decimal         | Number                           | `<td>123.45</td>` (텍스트)        | locale 의존 — 점/쉼표 통일                          |
| Date            | Date                             | `<time datetime="YYYY-MM-DD">`     | DokuWiki 는 항상 ISO-8601 (확인됨)                  |
| Url             | URL                              | `<a href>`                         | 직진                                                 |
| Media           | Files (또는 Page Link)            | `<ac:link><ri:attachment/>`        | media id → 메인 파이프라인 attachments 테이블 lookup → confluence attachment id 가 있으면 그것을 reference, 없으면 외부 링크 |
| Wiki            | Confluence Page Link             | `<ac:link><ri:page/>`              | doku_id → pages.confluence_page_id lookup. 미해결 시 S7 의 dwc-link placeholder 패턴 재사용 |
| Dropdown        | Status (또는 Tag)                | `<td>option</td>` (텍스트)         | `types.config` 의 옵션 목록을 같이 추출해 Native 모드에서 옵션 enum 으로 정의 |
| Lookup          | Database Reference (A)            | Page Link 텍스트 (B)               | 2-pass: 모든 schema 의 Database 생성 후 reference 컬럼 채움. 미관측이라 첫 실행에서 미지원 처리 가능 |
| User *(미관측)* | Person                           | 텍스트                              | 사용자 매핑 JSON 있으면 mention                      |
| Multi 값        | 같은 컬럼의 다중 값               | 쉼표 구분 텍스트                  | `multi_<schema>` 테이블 join 필요                   |

## 5. 구현 스케치

### 5.1 새 서브커맨드

```
python run.py struct-discover     # struct.sqlite3 -> state.db 의 struct_* 테이블
python run.py struct-convert      # 각 row 를 Confluence 측 입력 형태로 변환 + 미해결 reference placeholder 기록
python run.py struct-upload       # mode (native|properties|snapshot) 에 따라 Confluence 업로드. 2-pass: 1) schema/Database/index page 생성 2) row 업로드 + reference resolve
python run.py struct-status       # 진행도 확인
```

기본 `--mode native`, `--fallback {auto,properties,snapshot,fail}`.

### 5.2 state.db 확장

```sql
CREATE TABLE struct_schemas (
    sid                  INTEGER PRIMARY KEY,        -- DokuWiki sid (latest 한 개만)
    tbl                  TEXT NOT NULL UNIQUE,       -- 'brevet_event' 등
    row_count            INTEGER NOT NULL DEFAULT 0,
    column_count         INTEGER NOT NULL DEFAULT 0,
    confluence_db_id     TEXT,                       -- mode=native 후 채움
    properties_index_page_id TEXT,                   -- mode=properties 후 채움
    snapshot_page_id     TEXT,                       -- mode=snapshot 후 채움
    chosen_mode          TEXT,                       -- 'native' / 'properties' / 'snapshot'
    status               TEXT NOT NULL,              -- DISCOVERED / DEFINED / UPLOADED / FAILED
    last_error           TEXT,
    last_checked_at      TEXT
);

CREATE TABLE struct_columns (
    sid          INTEGER NOT NULL,
    colref       INTEGER NOT NULL,
    sort         INTEGER NOT NULL,
    name         TEXT,                  -- types.config 의 label.ko 등에서 추출. 없으면 colN
    dokuwiki_class TEXT NOT NULL,       -- Text / Decimal / Date / ...
    config_json  TEXT,                  -- types.config 그대로
    confluence_column_id TEXT,          -- native 모드에서 채움
    PRIMARY KEY (sid, colref)
);

CREATE TABLE struct_rows (
    sid                 INTEGER NOT NULL,
    pid                 INTEGER NOT NULL,            -- data_<tbl>.pid
    bound_doku_id       TEXT,                        -- row 의 Wiki 컬럼에서 추출한 page id (있으면)
    payload_json        TEXT NOT NULL,               -- {colref: value, ...} 형태로 정규화
    confluence_row_id   TEXT,                        -- native 모드의 row id
    confluence_page_id  TEXT,                        -- properties 모드의 자식 페이지 id
    status              TEXT NOT NULL,
    last_error          TEXT,
    PRIMARY KEY (sid, pid)
);

CREATE TABLE struct_references (
    -- Lookup / Wiki 컬럼의 cross-reference 추적 (2-pass 용)
    src_sid          INTEGER NOT NULL,
    src_pid          INTEGER NOT NULL,
    src_colref       INTEGER NOT NULL,
    target_kind      TEXT NOT NULL,                  -- 'schema_row' / 'page' / 'attachment'
    target_locator   TEXT NOT NULL,                  -- e.g. 'brevet_place:42' or 'b:files:s-200k-d.gpx'
    resolved         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (src_sid, src_pid, src_colref)
);
```

### 5.3 알고리즘

1. **struct-discover**
   - `meta/struct.sqlite3` 읽기.
   - `schemas` 테이블에서 (`tbl`, MAX(`ts`)) 그룹화 → 최신 버전 sid 만 활성.
   - `schema_cols` + `types` join → 각 컬럼의 class + config 추출. `name` 은 `types.config` 의 `label.<lang>` 또는 fallback `colN`.
   - `data_<tbl>` (WHERE latest=1) + `multi_<tbl>` 읽어 row 들을 정규화. multi 컬럼은 list 로 묶어 `payload_json` 에.
   - Wiki / Media / Lookup 컬럼은 `struct_references` 에 미해결로 등록.
   - 빈 schema (rows=0, columns 만 있는 `test`) 는 status=SKIPPED.

2. **struct-convert**
   - 각 row 의 `payload_json` 을 Confluence 측 입력으로 변환:
     - Native 모드: `{column_id: value}` 의 dict. Wiki/Media/Lookup 은 *placeholder* 토큰 (`dwc-page:<doku_id>` / `dwc-attach:<media_id>` / `dwc-row:<sid>:<pid>`).
     - Properties 모드: storage format 의 `<table>` row 들.
     - Snapshot 모드: 큰 표의 한 줄.
   - 변환 결과는 메모리/임시 디스크에만. 업로드 단계에서 placeholder 를 실제 id 로 치환.

3. **struct-upload — Pass 1: Database / Index 페이지 생성**
   - `--mode native`:
     - 각 schema 에 대해 `POST /wiki/api/v2/databases` 로 빈 Database 생성 → `confluence_db_id` 저장.
     - 컬럼 정의 API 호출 (지원되면) → `confluence_column_id` 저장.
     - 지원 안되는 컬럼 타입 (Lookup 등) → 그 schema 만 properties 모드로 격하.
   - `--mode properties`:
     - 각 schema 마다 `<root>/_struct/<schema>` 자식 페이지 생성 + `Page Properties Report` 매크로 본문.
   - `--mode snapshot`:
     - 각 schema 마다 자식 페이지 1개 + 큰 표.

4. **struct-upload — Pass 2: row 업로드 + reference resolve**
   - `struct_references` 의 미해결 항목을 *실제 id* 로 치환:
     - `target_kind='page'` → `pages.confluence_page_id` lookup (메인 파이프라인의 결과).
     - `target_kind='attachment'` → `attachments.confluence_attachment_id` lookup.
     - `target_kind='schema_row'` → `struct_rows.confluence_row_id` lookup.
   - Native 모드: `POST /wiki/api/v2/databases/{id}/rows` 또는 batch endpoint.
   - Properties 모드: 각 row → 자식 페이지 PUT + label 부착.
   - 실패 → `struct_rows.status='FAILED'` + last_error.

5. **메인 파이프라인과의 순서**

   ```
   discover -> render -> convert -> upload -> rewrite-links
                                          ↘
                                            struct-discover -> struct-convert -> struct-upload
   ```

   struct-upload 의 Pass 2 는 메인 `upload` 가 `pages.confluence_page_id` 와
   `attachments.confluence_attachment_id` 를 채운 뒤에 실행. 메인 파이프라인
   *후속 단계* 로 한 번에 묶는 것이 가장 간단.

## 6. 데이터량 / 시간 추정

- 활성 row 1,213 + 컬럼 정의 ~37개 + 4개 schema/Database 생성.
- Native 모드: ~1,300 API 호출 + 컬럼 정의 호출. **15분~30분** 추정 (rate limit 의존).
- Properties 모드: 1,213 자식 페이지 PUT + 4개 index 페이지. **20-40분**.
- Snapshot 모드: 4 페이지 PUT. **<1분**.
- 어떤 모드든 대량의 sqlite read 는 로컬에서 즉시.

## 7. 결정 항목 (현 시점 상태)

| # | 항목 | 상태 |
|---|------|------|
| 1 | 매핑 옵션 선택 (1/2/3) | **결정 (2026-05-18)**: 옵션 3 (schema → Confluence Database). 우선순위 A native → B properties → C snapshot. |
| 2 | 폴백 자동/수동 | **결정**: `--fallback auto` 기본. schema 별로 컬럼 타입 미지원 발견 시 그 schema 만 격하. `--fallback fail` 로 미지원 발견 시 중단 가능. |
| 3 | row → 페이지 binding 의 출처 | **결정**: row 의 첫 Wiki 타입 컬럼이 페이지 reference. 없으면 row 자체는 page-less (Database 안에만 존재). |
| 4 | Multi 컬럼 처리 | **결정**: Native 는 multi-value 컬럼으로, Properties 는 쉼표 구분 텍스트. |
| 5 | 스키마 이력 (DokuWiki schemas 의 여러 ts 버전) | **결정**: 최신 버전만 사용. 이전 버전 보존이 필요하면 history-migration 의 옵션 D (content property JSON) 와 합성. |
| 6 | Confluence Database API 의 실제 가능 범위 | **조사 보류**: 구현 시작 시 실측. `struct-upload --probe` 서브플래그로 빈 Database 와 컬럼 1개 만들어 응답 확인 → 미지원 시 자동 폴백. |
| 7 | Lookup 컬럼 cross-reference (이 인스턴스 미관측, 일반 케이스) | **계획**: 2-pass — 모든 Database 생성 후 row 두번째 패스에서 Database Reference 컬럼 채움. 첫 실행에서 충돌 시 그 schema 만 B 로. |
| 8 | 사용자 매핑 (User 타입, 미관측) | **재사용**: history-migration 과 동일 `--users-map` 옵션. 매핑 없으면 dokuwiki 사용자명을 텍스트로. |
| 9 | 빈 schema (`test`) | **결정**: SKIPPED (status 만 기록, Confluence 측 아무것도 안 만듦). |
| 10 | schema 가 페이지 binding 패턴을 *가지고* 있는 일반 케이스 (`schema_assignments_patterns` 채워져 있음) | **계획**: row 의 Wiki 컬럼 휴리스틱 대신 패턴 기반 매핑. 본 인스턴스는 빈 상태라 휴리스틱으로 충분. |

## 8. 다음 단계 (구현 로드맵)

1. **probe**: `struct-upload --probe` 로 Confluence Database API 의 실제 가용 컬럼 타입 / row 입력 형태 / Database Reference 지원 확인. 결과를 본 문서 §3.1 의 *리스크* 절에 업데이트.
2. **schema 정의 코드**: `struct-discover` 의 `struct_schemas` / `struct_columns` 채우기 + 빈 schema 스킵 처리.
3. **row 정규화**: `struct-discover` 의 `struct_rows` payload_json 생성 + Wiki/Media/Lookup 컬럼의 `struct_references` 등록.
4. **mode=snapshot 먼저 구현** (가장 단순, 검증 용이) → 4개 페이지 자식 트리 생성으로 베이스라인 확보.
5. **mode=properties** → Page Properties + Report 매크로 검증.
6. **mode=native** → API probe 결과대로 Database 생성/컬럼/row 입력 구현.
7. **메인 파이프라인 통합**: `upload` 완료 후 자동 호출 옵션 (`--with-struct`). 또는 별도 실행.
8. **검증**: 4 schema 모두 변환 후 Confluence UI 에서 (a) row 표시, (b) 정렬/필터 (native 만), (c) Wiki 컬럼의 page link 클릭 동작, (d) Media 컬럼의 attachment download 점검.
