# 시각 비교 자동화 제안 (visual-comparison-proposal)

DokuWiki 렌더 페이지 ↔ Confluence 이전 페이지의 *시각적 동등성* 검증을
한 단계 더 자동화하기 위한 제안 모음.

> **2026-05-20 업데이트**: §2 의 제안 1-7 모두 **구현 완료** (Phase 4).
> 검수 카드의 `.metrics-vc` 줄에 신호 표시. CLI 옵션 / 헬퍼 함수 /
> 21 unit test 모두 포함. 제안 8 (AI vision 구조화) 은 보류. 자세한
> 적용 매트릭스는 [`visual-audit.md` Phase 4](visual-audit.md).
>
> **2026-05-21 후속**: 사용자가 비교 갤러리 (`compare-publish`) 의
> 양측 스크린샷을 *직접* 비교해 발견한 4 사례 — Phase 4 시각 신호로
> 못 잡는 *3-측 invariant* 영역. 별도 명령으로 구현:
> [`3way-audit.md`](3way-audit.md) (`audit-3way`, 신호 5종 + 화이트리스트,
> 1675 페이지 실측 violation 0.42% / converter 측 0).
>
> **`compare-publish` 의 라이브 적용 사이클** (Day 5 §6~§9) 에서 발견·fix:
> - `_compare_rewrite_attachment_urls` — 첨부 src + data-image-src + srcset
>   3 attribute 모두 v1 endpoint 으로 rewrite (`download/attachments` +
>   `download/thumbnails`). Basic Auth 통과.
> - `_compare_clip_oversize` — viewport 12000px clip + PIL trim (PIL skip
>   threshold 5MB, OOM 방지). 거대 페이지 100MB 첨부 한도 회피.
> - 같은 filename 첨부 2-step update (`POST /child/attachment` 400 →
>   `POST /child/attachment/{aid}/data`) — 이전 빈 PNG 영구 잔존 issue 해소.
> - iframe placeholder 페이지 안내 박스 injection (view body API 가 빈
>   placeholder 응답 시).
> - **기본 동작이 자동 exclude** (CL 53888) — 매 호출이 `compare_publish_history`
>   meta 누적 → 다음 selection 에서 제외. 구 `--rotate` flag 는 backward-compat
>   no-op. `--reset-rotation` 으로 초기화, `--no-track` 으로 일회성 발행.
> - `_COMPARE_PERMANENT_EXCLUDE = {"start", "sidebar"}` — 본문 빈 페이지 영구
>   제외. `--select` 명시 시 우회.

본 도구는 이미 [visual-audit Phase 1-3](visual-audit.md) 가 구축되어
있어 — 그 위에 *남은 갭* 을 메우는 방향으로 정리.

본 문서는 *원래는* 제안서였으나 채택 후 구현 완료. 본 인스턴스의 라이브
마이그레이션은 이미 Phase 1-3 + 사람 검수로 완료되어 있어, 본 제안의
가치는 *다음 인스턴스* 또는 *재마이그레이션* 에 더 큼.

---

## 1. 현재 자동화된 영역 (참고용)

| Phase | 신호 | 의존성 | 핵심 한계 |
|-------|------|--------|-----------|
| 1 | DOM side-by-side HTML 갤러리 (iframe 격리) | bs4 | 사람 눈으로 비교 — 시간 비례 |
| 2 카운트 | 양측 헤딩/표/리스트/이미지/링크/매크로 카운트 mismatch | bs4 | 카운트만 동일하고 *내용* 은 다를 수 있음 |
| 2 PHash | Playwright 전체 페이지 PNG → perceptual hash 유사도 | playwright + imagehash + pillow | 전체 페이지 = 1 score. 페이지 안 *어느 부분* 이 깨졌는지 모름 |
| 2 Vision | Claude vision (스크린샷 2장 → score + 누락 영역) | anthropic SDK + 키 | 비용 + 비전이 "이 표가 잘렸음" 같은 *구체 위치* 잘 못 짚음 |
| 3 자동 신호 | 문장 정렬 / artifact / 코드블록 해시 / 헤딩 LCS / 링크 해소율 | 표준 라이브러리 | 텍스트 기반 — *시각적 레이아웃* 차이 (정렬/색/여백) 잡지 못함 |
| 첨부 점검 | Confluence v2 attachments GET | requests | 본문 안 이미지 *해상도/위치/크기* 검증 아님 |

요약 정상 동작이 잘 되는 영역:
- 텍스트 등가성 (Phase 3 문장 정렬)
- 구조 등가성 (Phase 2 카운트)
- *대략적인* 전체 시각 유사도 (PHash + 비전)

요약 자동으로 잡지 못하는 영역:
- 페이지 *어느 부분* 이 시각적으로 다른지의 *위치 정보*
- 이미지/표의 *해상도·정렬·여백* 의 미세 차이
- 사이드바/헤더 등 *chrome 영역의 영향* (양쪽 다 다른 chrome 이어서 PHash 가 떨어짐)
- 동적 콘텐츠 (외부 차트, 임베드 등)
- 색상 / 다크모드 / contrast / accessibility 차이

---

## 2. 제안 — 8 가지 자동화 후보

### 제안 1. Chrome 영역 마스킹 후 픽셀 diff (★★★ 권장)

**문제**: DokuWiki 사이드바, Confluence 네비게이션 등 *둘 다 다른 chrome*
가 PHash 유사도를 떨어트림. 본문만 비교해야 의미 있는 신호.

**방법**:
1. Playwright 로 양측 페이지 캡쳐 시 `page.locator(<main 영역>).screenshot()`
   사용 → chrome 제외하고 본문만
2. 양측 본문 PNG 를 같은 너비로 normalize (resize)
3. `pixelmatch` (또는 Pillow `ImageChops.difference`) 로 픽셀 단위 diff →
   변경 픽셀 비율 + diff 이미지 생성 (빨간색 overlay)
4. diff 이미지를 verify 카드 안에 `<details>` 로 표시

**의존성**: 기존 playwright + pillow + (옵션) pixelmatch 또는 자체 구현.
**효과**: 정확한 "어디가 바뀌었나" 시각화. 카운트로는 안 잡히는 정렬/색상 변형 검출.
**난이도**: 중 (locator 선택자 양측 결정 필요 — DokuWiki `#dokuwiki__content > .page`, Confluence `[data-test-id="content-body"]` 등).
**리스크**: 양측의 폰트/줄높이 미세 차이로 *의미 없는* diff 가 다수 잡힐 수 있음 → 임계값/blur 전처리 필요.

### 제안 2. 타일 분할 PHash (★★★ 권장)

**문제**: 페이지 PHash 1점은 *어디가* 깨졌는지 모름.

**방법**:
1. 양측 본문 PNG 을 `N × M` (예: 4×8) 격자로 분할
2. 각 타일별 PHash 계산 → 매트릭스 거리
3. 거리 큰 타일을 빨간 박스로 overlay → 사용자가 "위에서 두 번째 표가 깨졌네" 같은 *지역 정보* 즉시 인식

**의존성**: 기존 playwright + imagehash + pillow.
**효과**: 페이지 어느 부분에 문제가 있는지 *시각적 hotspot*. PHash 1점으로는 불가능했던 정보.
**난이도**: 하 — imagehash 가 이미 phash 함수 제공. 격자 분할 + 거리 행렬만 추가.
**리스크**: 페이지 길이가 양측에서 다르면 (Confluence 가 더 짧음) 타일 정렬이 안 맞음 → 양측 모두 절대 px 가 아니라 *콘텐츠 비율 (h1 부터 끝까지 normalize)* 로 분할 필요.

### 제안 3. 요소 단위 (per-element) 캡쳐 + 비교 (★★ 권장)

**문제**: "표 1번이 잘렸나" 같은 *구체 요소* 의 시각 차이를 페이지 단위 diff 로는 잡기 어려움.

**방법**:
1. Playwright 로 양측에서 `<h1>`, `<h2>`, `<table>`, `<img>`, `<pre>` 의 bounding box + 스크린샷 캡쳐 (`element.screenshot()`)
2. 양측 요소를 *순서대로* 짝짓기 (이미 [`_compare_heading_seq`](visual-audit.md#phase-3) 가 헤딩 시퀀스 LCS 제공)
3. 짝지어진 각 요소 쌍을 phash 비교 → 카드 안 표/이미지/코드블록 별 OK/NG 띠

**의존성**: 기존 playwright + imagehash.
**효과**: "27번째 페이지의 3번 표가 잘림" 같은 *지표 + 위치* 동시 제공.
**난이도**: 중 — 짝짓기 로직 + 카드 렌더 변경.
**리스크**: 표/리스트의 양측 element 수가 다르면 짝짓기 휴리스틱 필요 (Phase 3 의 LCS 활용).

### 제안 4. OCR 백업 텍스트 비교 (★ 보류)

**문제**: 양측 본문에서 *텍스트가 이미지로 렌더된* 경우 (예: 첨부 이미지 안 글자) 텍스트 비교가 누락.

**방법**: Tesseract (또는 macOS Vision 프레임워크) 로 양측 스크린샷 → 텍스트 추출 → Phase 3 의 sentence_align 으로 비교.

**의존성**: `pytesseract` + tesseract 바이너리 (brew install) 또는 macOS 만이면 `vision` 모듈.
**효과**: 이미지 안 글자 누락 검출.
**난이도**: 중.
**리스크**: 본 인스턴스에서 텍스트 이미지가 거의 없음 (스크린샷 위주가 아닌 사진/지도 위주) → ROI 낮음. *다른 인스턴스에서 글자 이미지가 많으면* 재검토.

### 제안 5. 레이아웃 구조 (bbox tree) 비교 (★★ 권장)

**문제**: 양측의 *주요 블록 위치* (헤딩, 단락, 표, 이미지) 가 동일한 *시각적 흐름* 인지 확인.

**방법**:
1. Playwright `page.evaluate()` 로 양측에서 주요 블록 bbox (x/y/w/h) + tag 추출:
   ```js
   Array.from(document.querySelectorAll('h1,h2,h3,p,table,img,pre,ul,ol')).map(e => ({
     tag: e.tagName, x: e.offsetLeft, y: e.offsetTop, w: e.offsetWidth, h: e.offsetHeight,
     text: e.innerText.slice(0,40)
   }))
   ```
2. 양측 블록 시퀀스를 `text` 키 기준 LCS 정렬
3. 짝지어진 블록의 bbox 비율 비교 (양측 페이지 너비로 normalize)
4. 큰 차이 (예: 상대 너비 30% 차이, 비율 변동 ±20%) 만 NG 후보로

**효과**: 픽셀 diff 보다 의미 있는 *레이아웃 정합성* 측정. 폰트 차이 / 줄간격으로 인한 잡음 자연 흡수.
**난이도**: 중.
**리스크**: 양측의 블록 개수가 다르면 LCS 가 짧아짐 (이미 Phase 3 가 같은 알고리즘).

### 제안 6. 양측 storage / DOM canonical tree diff (★★ 권장)

**문제**: 렌더링은 시간/네트워크 의존. *결정론적* 인 storage XML 비교가 더 안정적.

**방법**:
1. DokuWiki XHTML → canonical AST (text 노드만, attribute 제거)
2. Confluence storage XML → canonical AST (`ac:*` 매크로는 normalized form 으로 변환)
3. `zss` (Zhang-Shasha tree edit distance) 또는 자체 LCS 로 트리 거리 계산
4. 임계값 초과 시 NG

**의존성**: 표준 라이브러리 (또는 `zss`).
**효과**: 가장 *결정론적* 인 신호. 동일 입력은 항상 동일 점수.
**난이도**: 중-상 — canonical form 정의 가 까다로움.
**리스크**: ac:macro 의 변환 정의가 1:N 인 경우 (예: dokuwiki `<div class="wrap_info">` → ac:info macro) 트리 모양이 달라져 distance 가 항상 큼. 도메인 매핑 룰을 알아야 정확한 canonical 생성.

### 제안 7. 색상 히스토그램 / 다크모드 / contrast (★ 보류)

**문제**: 양측의 *시각적 톤* 이 너무 다르면 사용자 체감 차이 큼.

**방법**: Pillow 로 본문 PNG 의 색상 히스토그램 (RGB 256bin × 3) → cosine similarity. 명도 대비 비율도 측정.

**효과**: 다크/라이트 모드 변경, 사이트 테마 차이 검출.
**난이도**: 하.
**리스크**: 본 인스턴스에서 양측 다 라이트 테마 — ROI 낮음.

### 제안 8. AI vision 의 *구조화된 per-요소 프롬프트* (★★ 권장)

**문제**: 현재 `_verify_ai_compare` 는 전체 페이지 두 장 → 1점 + 자유 텍스트. 비전이 "표 3번 자름" 같은 *구체 위치* 잘 못 짚음.

**방법**: 비전 프롬프트를 *체크리스트 형식* 으로:
```
다음 항목별로 OK/NG/UNCLEAR 와 사유를 JSON 으로 답변:
{
  "headings_match": ...,
  "tables_visually_intact": ...,
  "code_blocks_preserved": ...,
  "images_visible_and_aligned": ...,
  "callouts_styled_correctly": ...,
  "overall_layout_fidelity": ...
}
```
→ tool_use 또는 JSON mode 로 강제. 카드에 각 항목별 띠 표시.

**의존성**: 기존 anthropic SDK + 키.
**효과**: 자유 텍스트가 아닌 *체크 가능한* 응답. 사람 검수 시간 단축.
**난이도**: 중 — 프롬프트 + JSON 파싱.
**리스크**: 모델 응답 일관성 — 같은 입력에 다른 라벨이 나올 수 있음 (temperature 0 로 완화).

---

## 3. 권장 우선순위 (Top 3)

본 도구의 *다음 인스턴스 적용* 또는 *재마이그레이션* 을 가정. 가치/난이도
비율 기준:

### 1순위 — 제안 2 (타일 분할 PHash)

- 의존성 기존 한도 안 (playwright + imagehash + pillow)
- 사람 검수자가 *시각적 hotspot* 즉시 인지 → 처리 시간 50%+ 단축 기대
- 구현 ~100 LOC, 2-3시간

### 2순위 — 제안 1 (Chrome 마스킹 후 픽셀 diff)

- 1순위와 같은 의존성 위에 구축
- 정확한 "어디가 어떻게 바뀌었는지" 시각화
- pixelmatch 라이브러리 추가 또는 ImageChops 자체
- 구현 ~150 LOC, 4시간

### 3순위 — 제안 5 (bbox tree LCS)

- 픽셀 비교의 *폰트/줄간격 잡음* 우회
- 의미 있는 *레이아웃* 신호
- 구현 ~200 LOC, 1일

이 세 가지로 *vision 호출 빈도 추가 50% 절감* 기대. AI vision 은 잔여
모호 페이지 5-10% 에만 사용.

---

## 4. 권장 동작 매트릭스 (구현 후)

| 신호 | 의존성 | 결정론적 | 위치 정보 | 비용 |
|------|--------|----------|-----------|------|
| Phase 1 DOM | bs4 | ✓ | ✗ | 무료 |
| Phase 2 카운트 | bs4 | ✓ | ✗ | 무료 |
| Phase 2 PHash | playwright + imagehash | ✓ | ✗ (전체 1점) | 페이지당 ~3초 |
| Phase 3 자동 신호 | stdlib | ✓ | ✗ | 무료 |
| **제안 2 타일 PHash** | (이미 있음) | ✓ | ✓ (격자 단위) | 페이지당 ~1초 추가 |
| **제안 1 픽셀 diff** | + pixelmatch (옵션) | ✓ | ✓ (픽셀 단위) | 페이지당 ~2초 추가 |
| **제안 5 bbox LCS** | (이미 있음) | ✓ | ✓ (블록 단위) | 페이지당 ~0.5초 추가 |
| Phase 2 AI vision | anthropic 키 | ✗ (모델 응답 변동) | ✓ (자유 텍스트) | 페이지당 ~$0.01 |
| **제안 8 vision 구조화** | (이미 있음) | ✗ (단 변동 감소) | ✓ (체크리스트) | 페이지당 ~$0.01 |

결정론적 + 위치 정보 모두 가진 신호는 제안 1/2/5 — *비용 0, 추가 페이지당 1-3초*. 이걸로 잡히지 않는 5-10% 만 vision 으로.

---

## 5. 구현 시 통합 지점

각 제안은 *기존 verify build 의 옵션 플래그* 로 추가:

```sh
# 기존
python run.py verify build --sample 100 --with-screenshots

# 신규 (제안 1+2+5 통합)
python run.py verify build --sample 100 --with-screenshots \
    --with-tile-phash --with-pixel-diff --with-bbox-lcs

# AI vision 구조화 (제안 8)
python run.py verify build --sample 100 --with-vision --vision-structured
```

각 신호는 `_verify_compute_metrics` 에서 계산해 검수 카드의 `.metrics-auto`
줄에 추가. NG 자동 분류 (auto_ng) 임계값도 같이 보강:

```
tile_max_distance >= 0.4               → 시각
pixel_diff_ratio >= 0.15               → 시각
bbox_lcs_ratio < 0.7 (n_blocks >= 5)   → 레이아웃
vision_structured.images_intact == NG  → 이미지
```

---

## 6. 테스트 / 검증 계획

각 제안 채택 시:
1. 합성 fixture — 의도적으로 결함 주입한 페이지 쌍 (표 잘림 / 이미지 누락 / 헤딩 순서 바뀜) → 각 신호가 *해당 결함을* 검출하는지 unit test
2. 본 인스턴스의 verify_decisions 와 *상관관계* 측정 — 사람이 NG 친 페이지에서 각 신호가 얼마나 강하게 발화하는지 ROC curve
3. 임계값 튜닝 — false positive 와 false negative 비율 trade-off 시각화

신호별 unit test 는 `tests/test_visual_signals.py` (제안 시 추가) 에 따로 분리.

---

## 7. 결정 항목

| # | 항목 | 결정 |
|---|------|------|
| 1 | 제안 1/2/5 를 본 인스턴스 *재마이그레이션* 에 적용할 가치? | **채택 + 구현** (2026-05-20). 제안 1-7 모두 구현. CLI 플래그 + 카드 표시 + 21 unit test. 본 인스턴스 재마이그레이션 시 즉시 활용 가능. |
| 2 | 제안 8 (vision 구조화) 단독 도입? | **보류** — 현재 Phase 2 vision 으로 충분. 필요 시 추가. |
| 3 | 제안 6 (storage AST diff) 가치? | **채택 + 구현**. wrap_info/macro:info 매핑까지 canonical 정규화 — 변환기 변경 후 검증에도 유용. |
| 4 | 임계값을 어디서 튜닝? | 카드의 ok/warn/bad 색상 임계값 (visual-audit.md Phase 4 표 참고). 본 인스턴스의 verify_decisions 누적되면 ROC 분석으로 재조정. |

---

## 8. 다음 단계

§7 의 채택 결정 이후, **2026-05-20** 에 §2 의 제안 1-7 모두 구현 완료.
남은 단계:

1. ✅ 합성 fixture 로 unit test (21 케이스, tests/test_visual_signals.py)
2. ⏳ 실 corpus 적용 → ROC — 본 인스턴스 재마이그레이션 시 또는 다른
   인스턴스 적용 시
3. ⏳ 임계값 ROC 분석 후 미세 조정 — verify_decisions 가 누적되어야 가능
4. ⏳ 제안 8 (vision 구조화) 도입 결정 — 잔여 모호 페이지 비율 측정 후

CLI 사용:

```sh
# 권장 (7가지 모두 활성)
python run.py verify build --sample 100 --with-confluence-view \
    --with-attachment-check --with-screenshots --with-all-extra-signals

# 의존성 절약 (이미지 기반 제외)
python run.py verify build --sample 100 --with-confluence-view \
    --with-storage-ast --with-element-compare --with-bbox-lcs

# OCR 만 추가 (글자 이미지 많은 인스턴스)
pip install pytesseract && brew install tesseract
python run.py verify build --sample 100 --with-screenshots --with-ocr
```

---

## 더 읽기

- 현재 자동화 상태: [`visual-audit.md`](visual-audit.md) Phase 1-3
- 검수 카드 UX: [`visual-audit.md §5.4`](visual-audit.md)
- 사용자 결정 import 흐름: [`visual-audit.md §7`](visual-audit.md)
- Phase 3 자동 신호 구현: `run.py` `_sentence_align` / `_compare_artifacts`
  / `_compare_code_blocks` / `_compare_heading_seq` / `_link_resolution_rate`
