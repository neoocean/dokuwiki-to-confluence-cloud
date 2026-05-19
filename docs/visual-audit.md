# 사용자 시각 검수 자동화 (visual-audit)

## 1. 왜 이게 별도 트랙인가

지금까지의 자동 검증 — `lint` (storage XML 문법), `report` (corpus
통계), `audit` (텍스트+구조 카운트 비교), `preview` (raw + storage
side-by-side HTML) — 은 모두 *입력 단계의 형식 보존*을 검증한다. 즉:

| 단계 | 답해주는 질문 |
|------|---------------|
| `lint` | storage XML 이 Confluence 가 파싱할 수 있는 형태인가 |
| `report` | 어떤 종류의 콘텐츠가 얼마나 들어있는가 |
| `audit` | dokuwiki raw 와 우리가 만든 storage 가 같은 *카운트* 인가 |
| `preview` | 한 페이지의 raw 와 storage 가 한 화면에서 어떻게 보이는가 |

이것들이 모두 통과해도 **사용자가 Confluence UI 에서 그 페이지를 열었을
때 dokuwiki 와 비교해 "쓸 만한" 모양인가**는 검증되지 않는다. 그 마지막
질문은 지금까지 사람이 한 페이지씩 열어보는 수밖에 없었다.

본 문서는 그 *시각 검수*를 자동화 또는 반자동화하는 방안의 옵션을
나열하고 권장안을 정한다.

## 2. 검수 대상 — 무엇이 "시각적으로" 같아야 하는가

페이지 1,675 개 × 점검할 시각 요소를 곱하면 사람이 일일이 보는 것은
비현실적. 우선순위:

| 시각 요소 | 검수 비용 | 자동화 난이도 |
|-----------|-----------|---------------|
| h1 ~ h6 위계 | 낮음 (구조 동일성) | DOM 비교로 99% 자동 |
| 본문 단락의 단어 누락 | 높음 (텍스트 diff) | 어느 정도 자동 |
| 표/리스트 행/열 수 | 낮음 | DOM 비교로 100% 자동 |
| 이미지 *위치 + 크기* | 중간 | 스크린샷 diff 만 |
| 매크로 박스 (info/tip/note/warning/code) 렌더 | 중간 | 스크린샷 diff 만 |
| 내부 링크가 *목적지 페이지* 로 가나 | 낮음 | DOM 의 링크 target 비교 |
| 외부 링크 라벨/href | 낮음 | 상동 |
| 첨부 다운로드 링크 클릭 시 파일 받기 | 중간 | HEAD 요청으로 200/404 |
| wrap 콜아웃이 *적절한* 박스로 매핑됐나 | 높음 | 사람 눈 필요 |
| 풋노트 점프 동작 | 낮음 | anchor 매크로 존재 확인 |
| smiley → 유니코드 이모지 | 낮음 | 텍스트 비교 |
| struct 데이터가 *읽을 만한지* | 높음 | 사람 눈 필요 |
| history 헤더 박스가 *알아볼 수 있는지* | 중간 | 사람 눈 필요 |

자동화로 처리 가능한 것은 이미 `audit` 이 한다. 본 문서는 **자동화로
못 잡는 시각 항목** (이미지 위치, 매크로 박스 렌더, wrap 콜아웃 매핑
품질, struct 가독성) 을 *사람이 가장 효율적으로 보게 만드는* 흐름을
설계한다.

## 3. 옵션 매트릭스

### A. DOM 기반 side-by-side 검수 큐

- dokuwiki 의 `?do=export_xhtmlbody` 출력과 Confluence 의 `?body-format=view`
  (또는 `anonymous_export_view`) 응답을 받아 한 페이지 안에서 좌우 분할
- 우선순위 큐: `audit` critical/낮은 점수 페이지 + 매크로/이미지/wrap 많은 페이지 + 큰 본문 페이지 + history-tail 페이지 + 무작위 N개
- 각 카드에 OK/NG/보류 라디오 + 메모 textarea + "Confluence 에서 열기" 딥링크
- 검수자가 입력한 결정은 `verify_decisions.json` 으로 저장 → 다음 실행은 미검수 페이지만
- 진행률 표시 ("120/200 reviewed, 7 flagged")

| 장점 | 단점 |
|------|------|
| 헤드리스 브라우저 불필요, Python + requests 만으로 가능 | DOM 텍스트만 비교 — 실제 *렌더 픽셀* 차이는 못 봄 |
| 모든 페이지 즉시 대상 | wrap 콜아웃 색상이나 매크로 박스 모양은 표시 안 됨 |
| `state.db` 기반 우선순위가 자연스럽게 들어옴 | dokuwiki body 의 CSS 가 export 출력에는 빠져 있어 *원본 모양 그대로*는 아님 |

### B. 헤드리스 스크린샷 perceptual hash 비교

- Playwright (Python) 로 dokuwiki 페이지 풀 URL (`http://dev/doku.php?id=<id>`) 과
  Confluence 페이지 URL (`https://woojinkim.atlassian.net/wiki/spaces/.../pages/<id>`) 둘 다 헤드리스 Chromium 으로 스크린샷
- ImageHash 의 phash 로 유사도 점수 계산
- 점수 임계 (예: 해밍 거리 > 16) 초과만 사람 검수 큐로
- 차이 큰 영역을 빨간 박스로 표시 (선택)

| 장점 | 단점 |
|------|------|
| 실제 사용자가 보는 모양 그대로 비교 | Playwright 의존성 + Chromium 다운로드 ~200MB |
| 매크로 박스/색상/이미지 위치 모두 포함 | dokuwiki 와 Confluence 의 *원래* 디자인 자체가 달라 phash 가 거의 항상 다르게 나옴 |
| 한 번 셋업하면 빠름 (페이지당 ~3초) | Confluence 로그인 자동화 필요 (basic auth + 쿠키) |
| | dokuwiki dev 컨테이너 + Confluence 양쪽 모두 살아있어야 함 |

### C. AI vision (VLM) 비교

- dokuwiki + Confluence 양쪽 스크린샷 (B 와 동일) 을 Claude/GPT-4o 같은 VLM 에 전달
- "이 두 페이지는 *같은 내용을 표현*하고 있는가? 누락된 영역, 잘못 매핑된 박스가 있는가?" 프롬프트
- 점수 + 자유 텍스트 description 받음
- 점수 낮은 페이지만 사람 검수 큐

| 장점 | 단점 |
|------|------|
| 사람이 보는 관점과 가장 가까움 | 1,675 페이지 × VLM call = 의미 있는 API 비용 |
| 미묘한 누락 (예: wrap 박스가 사라짐) 도 감지 가능 | dokuwiki 와 Confluence 가 원래 다르게 생겼다는 점 때문에 false positive 다수 |
| 추가 작업 없이 자연어 리포트 | 결과의 일관성 확보 어려움 (같은 페이지를 두 번 평가하면 점수가 다름) |

### D. A + B 하이브리드 (권장)

- *모든* 페이지에 대해 **A** (DOM 비교 + 검수 큐) 를 default 로 돌림
- 시각 요소가 결정적인 페이지 (`audit` critical / 매크로 많음 / wrap
  콜아웃 페이지 / 큰 본문 fallback / struct snapshot / oversized
  attachment) 에 대해서만 **B** (스크린샷) 를 추가로 붙여 카드에 함께 보여줌

| 장점 | 단점 |
|------|------|
| 검수 효율 극대화 — 사람이 *시각이 정말 중요한 페이지에서만* 픽셀 확인 | 두 가지 의존성 모두 들어옴 |
| 즉시 동작하는 부분 (A) 부터 단계적으로 확장 가능 | 스크린샷 풀은 시간 비용 (페이지당 ~3-5초) |
| 검수 결정 저장이 일관됨 | 실제 검수자의 *입력*은 여전히 필요 (완전 자동 아님) |

### E. 완전 자동 — 사람 검수 없음

- 모든 페이지의 자동 비교 점수만 받고 임계 초과만 보고
- 사람 검수 큐 자체를 없앰
- 적합한 경우: 1,000+ 페이지 코퍼스에서 *대표성 표본만* 보고 끝낼 때
- 본 작업에는 부적합. 이미 `audit` 이 같은 역할을 하고 있어 중복

## 4. 권장: D (A 우선, B 선택적 확장)

이유:

1. **A 만 있어도 검수 자동화의 80%** 가 풀린다. preview/audit/state.db 가 이미 갖고 있는 정보를 묶어주면 검수자가 "다음 페이지" 버튼만 누르면 되는 흐름이 만들어진다.
2. **B 는 dev 컨테이너 + Confluence 둘 다 살아있어야** 한다 — 마이그레이션 종료 후 dev 컨테이너 내릴 거면 B 는 다시 띄울 때만 의미가 있다.
3. **C (VLM)** 는 본 코퍼스 크기 (~1,675 페이지) 와 작업 1회성을 보면 비용/이득 비율이 낮다.
4. **state.db 의 검수 결정**을 저장해 두면 향후 변환기 변경 후 재마이그레이션 시 *변경 영향 받은 페이지만* 재검수 큐에 올릴 수 있다.

## 5. 구현 스케치

### 5.1 새 서브커맨드

```sh
# 검수 큐 생성 (실제 비교 + HTML 갤러리)
python run.py verify build [--sample N] [--strategy auto|all|critical-only]
                            [--with-screenshots]                  # B 활성화
                            [--output /tmp/verify/index.html]

# 검수자 결정 기록 (브라우저에서 사람이 입력 → state.db 저장)
python run.py verify serve [--port 8765]
# 또는 정적 HTML 의 form 결과를 받아 적용:
python run.py verify import /tmp/verify/decisions.json

# 진행률
python run.py verify status

# 우선순위 큐만 다시 생성 (검수 미완료 페이지만)
python run.py verify build --resume
```

### 5.2 데이터 모델 (state.db 추가)

```sql
CREATE TABLE verify_decisions (
  doku_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL,         -- OK / NG / DEFER
  notes TEXT,
  reviewer TEXT,                  -- 이메일 또는 식별자
  reviewed_at TEXT,
  source_hash TEXT,               -- 검수 당시 storage content_hash
  visual_score REAL,              -- 자동 비교 점수 (DOM 또는 phash)
  flags TEXT                      -- "macro,image,wrap" 등 카드에 표시한 시각 요소 태그
);
CREATE INDEX idx_verify_decision ON verify_decisions(decision);
```

검수 당시 `source_hash` 를 기록해 두면 **변환기가 바뀌어 페이지 본문이
다시 생성된 경우** decision 을 `STALE` 로 표시해 재검수 큐에 올릴 수
있다.

### 5.3 우선순위 큐 (default 전략 `auto`)

페이지 *모든* 1,675 개 중 다음 점수 합으로 정렬:

| 가중치 | 항목 |
|--------|------|
| +10 | `audit` 가 critical 로 분류 |
| +5 | 매크로 (info/tip/note/warning/panel/code) 가 5개 이상 |
| +5 | wrap 콜아웃 사용 |
| +5 | `large_body_fallback:<id>` 메타 보유 (C 모드 페이지) |
| +5 | OVERSIZED 첨부 보유 |
| +3 | 이미지 3개 이상 |
| +3 | history 가 50% 미만 보존된 페이지 |
| +3 | struct snapshot 페이지 |
| +1 | 본문 5KB 초과 |
| +1 | 외부 링크 5개 이상 |
| 기본 | 무작위 시드 (안정적 순서 보장) |

상위 200 페이지를 자동으로 카드화. 그 외 페이지는 `verify build --sample N` 의 N 으로 추가 무작위 추출 가능.

### 5.4 카드 1장의 구성 (정적 HTML)

```
┌───────────────────────────────────────────────────────────────────┐
│  [#42 of 200]  doku_id: wiki:syntax                               │
│  flags: macro:3, image:2, wrap:1                                  │
│  auto score: DOM diff 0.91 / phash 0.78 (스크린샷 모드 ON 시)     │
│  → [Confluence 에서 열기]  [dokuwiki 에서 열기]                   │
├───────────────────────┬───────────────────────────────────────────┤
│  dokuwiki (export_xhtml)                                          │
│  ─────────────────    │  Confluence (body-format=view)            │
│  <h1>Formatting       │  <h1>Formatting Syntax</h1>               │
│  Syntax</h1>          │  ...                                      │
│  ...                  │                                           │
├───────────────────────┴───────────────────────────────────────────┤
│  (선택) Side-by-side 스크린샷:                                    │
│  [dokuwiki.png][confluence.png]                                   │
├───────────────────────────────────────────────────────────────────┤
│  ( ) OK   ( ) NG   ( ) DEFER                                      │
│  메모: [_______________________________________________________]  │
│  [< 이전]  [Save]  [다음 >]                                       │
└───────────────────────────────────────────────────────────────────┘
```

진행률 바: `120/200 reviewed · 7 flagged · 12 deferred · 2 stale`.

### 5.5 변환기 변경 후 재검수 흐름

```sh
python run.py convert --only <doku_id> --force
python run.py upload --only <doku_id>
python run.py verify build --resume     # source_hash 바뀐 페이지는 STALE → 큐 상단
```

검수자가 "OK" 를 다시 눌러야 그 페이지의 `source_hash` 가 새 값으로 갱신.

## 6. 단계적 적용

### Phase 1 (즉시 가능) — A 만

- 의존성: 기존 + 표준 라이브러리 + (Confluence body-format=view 가져오기는 기존 v2 클라이언트로 가능)
- `verify build` 서브커맨드 + 정적 HTML 갤러리
- 검수자가 HTML 의 form 결과를 `verify_decisions.json` 으로 export → `verify import` 로 state.db 반영
- 예상 작업: ~500 줄

### Phase 2 — B 추가

- 의존성: `pip install playwright imagehash pillow` + `playwright install chromium`
- `--with-screenshots` 플래그로 활성화
- dokuwiki 는 dev 컨테이너 살아있는 동안만 사용 가능 → 미리 스크린샷 캐시
- Confluence 스크린샷은 토큰으로 인증된 페이지 view URL 호출
- 예상 작업: ~300 줄

### Phase 3 (선택) — 검수자 협업

- 정적 HTML 대신 가벼운 Flask `verify serve` 로 다중 검수자가 같은 큐를 공유
- 충돌 방지: page 별 lock + reviewer 필드
- 예상 작업: ~200 줄

## 7. 운영 절차 (Phase 1 적용 후)

```sh
# 1) 검수 큐 생성
python run.py verify build --output /tmp/verify/index.html
open /tmp/verify/index.html

# 2) 사람이 카드 200장을 확인하며 OK/NG/DEFER 클릭, 메모 작성
#    Save 누르면 브라우저가 decisions.json 다운로드

# 3) 결정 반영
python run.py verify import ~/Downloads/decisions.json

# 4) 진행률 확인
python run.py verify status
# 출력 예:
#   total queue: 200
#   OK:    180 (90%)
#   NG:      7 (3.5%) — 변환기 fix 필요
#   DEFER: 13 (6.5%)
#   stale:  0
#   NG 페이지 목록:
#     - u:foo:bar — "wrap 콜아웃이 누락됨"
#     - i:baz — "이미지 alt 텍스트 깨짐"
#     ...

# 5) NG 페이지 처리
python run.py preview --doku-id u:foo:bar    # 원인 분석
# → 변환기 fix → convert --only / upload --only / verify build --resume
```

## 8. 검수자가 잘 못 잡는 것 — 한계

- **첨부 다운로드 무결성**: 클릭해서 파일을 받아본 다음 *내용이 같은지*는
  사람도 어렵다. 별도 자동화 (`verify attachments` — 모든 `<ri:attachment>`
  대상에 HEAD 요청 + 콘텐츠 길이 비교) 가 필요. 본 문서는 시각 검수에
  집중하므로 별도 트랙.
- **검색·필터·매크로 인터랙션**: Confluence 의 `children` 매크로나 검색
  결과는 *시간이 지나면서* 달라진다. 검수 시점의 스냅샷만 본다.
- **사용자별 권한 차이**: 검수자가 owner 권한이면 모든 페이지가 보이지만
  실제 viewer 권한 사용자에게는 다르게 보일 수 있다. 별도 viewer-perspective
  체크는 본 문서 범위 밖.

## 9. 다음 결정 항목

- Phase 1 만 구현할지 vs Phase 1 + Phase 2 까지 한 번에 구현할지
- 초기 큐 크기 (default 200) 가 적절한지
- 검수자 정보 (`reviewer` 필드) 를 누구로 기록할지 — `CONFLUENCE_EMAIL`
  자동 사용 or 별도 입력
- 결정 export 형식: JSON form 다운로드 vs Flask 즉시 저장

권장 첫 단계: **Phase 1 + 큐 크기 200 + reviewer = CONFLUENCE_EMAIL +
JSON form 다운로드**. 동작 확인 후 Phase 2 추가.
