# 본문이 큰 페이지 처리 시나리오

Confluence Cloud 가 *본문 자체가 너무 크거나 구조적으로 복잡한* 페이지의
POST/PUT 을 거부하는 경우의 대응 방안. `oversized-attachments.md` (첨부
파일 한도) 와는 *별개*. 본문 측 한계.

## 1. 문제 정의

본 인스턴스의 첫 라이브 마이그레이션에서 1건 발생:

| 페이지 특성 | 값 |
|------------|----|
| storage XML 크기 | 약 448 KB |
| `<li>` 개수 | 1,971 |
| `dwc-link:` placeholder (rewrite-links 전) | 495 |
| 카테고리 | 장기 일지 페이지 |

Confluence 응답:
- `POST /api/v2/pages` 도, 같은 본문의 `PUT` 도 *응답 body 없이 timeout*
  (`create no resp` / `update no resp`).
- `_request_with_retry` 의 6회 backoff 후에도 동일. transient 가
  아니라 *내용 자체의 거부*.

같은 시도가 *7KB 작은 페이지에서도* 한 차례 일어났는데 (재시도하면
회복) — 이는 Confluence backend 의 일시적 거부였다. 그러나 본 큰
페이지는 *반복적* 거부 → 콘텐츠 자체 문제.

영향: 그 페이지의 *첨부 120건* 도 함께 stuck (페이지 미생성으로 업로드
못 함).

## 2. 추정 원인

Confluence Cloud 의 본문 한도는 공식적으로 *수 MB* 까지 허용한다고
명시. 448KB 는 분명 한도 내. 그러나 실효적으로는:

1. **storage parsing 의 깊이/시간 한도** — 1,971개의 li + 495 비표준
   `<a href="dwc-link:...">` placeholder 가 parser 에 부담.
2. **단일 ac:task-list 안의 task 개수 한도** — 본 페이지는 다수의
   task-list 매크로 + 1547 개 task 항목.
3. **rich-text-body 안에 들어간 nested 매크로 깊이**.
4. **알 수 없는 backend 제약** — Atlassian 도 명시하지 않은 soft limit.

진단을 위한 binary search (본문을 절반씩 잘라 시도) 도 가능하지만
시간 큼.

## 3. 옵션 매트릭스

| # | 모드 | 보존 | 사용자 경험 | 복잡도 | API 호출 |
|---|------|------|-------------|--------|----------|
| A | 무시 — broken | 메타데이터 | 페이지 없음, 첨부 stuck | 0 | 0 |
| B | skeleton 페이지 + 본문 안내 | 메타 + 백업 안내 | "원본은 호스트 P4 백업" 텍스트 | 낮음 | 1 |
| C | skeleton + storage XML 을 첨부로 (.xml.zip) | 본문 통째 | 첨부 다운로드해서 열기 | 중간 | 1 + 1 |
| D | 본문 분할 (각 `<h2>` 단위 N 자식 페이지) | 의미 + 시각 | 평탄해 보이지만 트리 구조 변화 | 높음 | N + 1 (parent) |
| E | 외부 host static HTML | 본문 시각 | 클릭 → 외부 페이지 | 중간 | 1 + 호스팅 |
| F | 손실 simplify (placeholder 텍스트로 정리) | 일부 | placeholder 손실하지만 작은 본문 | 중간 | 1 |

## 4. 권장 — C 모드 (skeleton + storage 첨부)

본 인스턴스 1건만 영향이라 *수동 작업도 부담 없음*. 그러나 자동화하면
같은 패턴의 페이지가 늘어도 재사용 가능.

### 4.1 C 모드 동작

```
1. skeleton 페이지 본문 생성:
     <ac:structured-macro ac:name="info">
       <ac:rich-text-body>
         <p>이 페이지의 원본 본문은 Confluence 의 본문 parsing
            한계를 초과해 직접 표시되지 않습니다.</p>
         <ul>
           <li>크기: 448 KB</li>
           <li><li> 개수: 1,971</li>
           <li>원본 본문은 페이지 첨부의 <code><doku_id>.xml.zip</code>
               에 보존됨</li>
         </ul>
       </ac:rich-text-body>
     </ac:structured-macro>
2. storage XML 을 zip 후 첨부로 업로드.
3. dokuwiki 의 첨부 (120건) 도 같은 페이지에 정상 업로드.
4. state.db 의 confluence_page_id 채워짐 — 다음 파이프라인 일관성 유지.
```

### 4.2 fallback 로직

`cmd_upload` 의 create/update 경로가 `no resp` (6회 backoff 후 응답
없음) 를 *영구 실패* 가 아니라 *콘텐츠 한계* 로 추정해 *자동 fallback*
시도:

```
POST/PUT 6회 backoff → resp is None
   ↓
fallback: skeleton 본문으로 같은 endpoint 재시도
   ↓
성공 시: 원본 storage XML 을 zip 후 첨부 업로드
   ↓
state.db 에 'large_body_fallback' 메타 표시 (감사용)
```

## 5. 구현 스케치

새 서브커맨드 `rewrite-oversized-pages` (또는 cmd_upload 의 fallback
flag):

```sh
python run.py rewrite-oversized-pages [--threshold-kb 300]
```

대상 선정:
1. state.db 에 `status='FAILED'` AND `last_error LIKE '%no resp%'` 인
   페이지
2. 또는 `--threshold-kb N` 이상의 storage 를 가진 페이지 (사전 예방)

각 대상에 대해:
- storage XML 을 `attachments/<doku_id>.xml.zip` 으로 압축
- skeleton 본문 생성 + POST (또는 status='FAILED' 였으면 first create)
- 첨부 업로드
- `state.db` 에 `large_body:<doku_id>` 메타로 fallback 적용 기록

state.db schema 변경 없음 (meta key 사용).

## 6. 결정 항목

| # | 항목 | 상태 |
|---|------|------|
| 1 | 어떤 모드 채택 | C 권장 (skeleton + 본문 첨부). 결정 보류. |
| 2 | 자동 fallback vs 수동 | 자동 권장 — 같은 페이지가 늘어도 일관 처리. |
| 3 | threshold 설정 | 본 인스턴스는 1건이라 *threshold 없이 FAILED 만 대상*. |
| 4 | dokuwiki 측 첨부 처리 | C 모드는 skeleton 생성 후 *원래 첨부도 같은 페이지에 업로드*. 자동. |

## 7. 다음 단계

1. 사용자가 §3 매트릭스 중 모드 채택.
2. 채택안에 맞춰 `cmd_rewrite_oversized_pages` 또는 cmd_upload 의
   `--auto-fallback` 구현 (~80 LOC).
3. 본 인스턴스 1건에 적용 → 페이지 + 120 첨부 모두 정착.
4. `migration-result.md §5.1` 의 outstanding 표시 제거.

본 시나리오는 다음 라이브 마이그레이션 (다른 인스턴스 또는 본 인스턴스
재실행) 에도 같은 패턴이 나타날 가능성 대비. 일회성 spot-fix 대신
재사용 가능한 fallback 으로 두는 게 가치 큼.
