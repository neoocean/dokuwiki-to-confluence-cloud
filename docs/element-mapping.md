# DokuWiki → Confluence 구성요소 변환 매트릭스

본 문서는 DokuWiki 페이지의 각 구성요소가 Confluence Cloud 의 storage
format 으로 옮겨질 때 어떻게 처리되는지 한 곳에 모은 reference 다. 분류
기준:

- **A. Pass-through** — DokuWiki 가 렌더한 HTML 요소를 변환기가 *그대로*
  통과시키고 Confluence 가 *시각/의미 모두 보존*하는 경우.
- **B. Transformed** — DokuWiki 의 출력이 Confluence 의 *다른 표현*
  (매크로, ri:link, ac:image 등) 으로 *능동 변환*되는 경우.
- **C. Partial / no visual** — storage XML 에 들어가지만 Confluence 에
  대응 styling 이 없어 *시각 효과만 손실*되는 경우.
- **D. Dropped** — 변환기가 *의도적으로 제거*하는 경우 (위험 태그,
  chrome, dokuwiki 내부 메타 등).
- **E. Separate track** — 본 파이프라인 범위 밖. 별도 시나리오 문서에서
  다루는 경우 (히스토리, struct 데이터 등).

실측 기반: 1,567 페이지 corpus + dokuwiki 표준 [`wiki:syntax`](https://www.dokuwiki.org/wiki:syntax)
페이지의 raw/변환 결과 비교.

## A. 그대로 변환 (Pass-through)

DokuWiki 가 표준 HTML 로 렌더하고 Confluence storage 가 그대로 받아들이는
요소. 변환기는 별도 처리 없이 통과만 시킨다 (class 정리는 별도).

| DokuWiki 마크업              | DokuWiki HTML 출력                     | Confluence 보존 | 비고                                                          |
|------------------------------|----------------------------------------|-----------------|---------------------------------------------------------------|
| `====== H1 ======`           | `<h1 id="..." class="sectionedit1">`   | h1              | id 보존, class 제거 (D 참조)                                  |
| `===== H2 =====` ~ `H5`      | `<h2>`/`<h3>`/`<h4>`/`<h5>`            | 그대로          |                                                               |
| 단락 (빈 줄로 구분)          | `<p>`                                  | 그대로          |                                                               |
| `**bold**`                   | `<strong>`                             | 그대로          |                                                               |
| `//italic//`                 | `<em>`                                 | 그대로          |                                                               |
| `__underline__`              | `<em class="u">`                       | em.u            | 밑줄 class 보존 — Confluence 가 CSS 모르면 시각 효과 미약 (C 도 참조) |
| `''monospace''`              | `<code>`                               | 그대로          |                                                               |
| `<sub>X</sub>` / `<sup>X</sup>` | `<sub>` / `<sup>`                   | 그대로          |                                                               |
| `<del>X</del>`               | `<del>`                                | 그대로          |                                                               |
| `\\` (강제 줄바꿈)           | `<br/>`                                | self-closed 유지 | XML self-close 후처리 적용                                   |
| `> 인용문`                   | `<blockquote>`                         | 그대로          | 192 페이지 사용; 그대로 통과                                  |
| 리스트 `* item` / `- item`   | `<ul><li>` / `<ol><li>`                | 그대로          | dokuwiki 가 `<div class="li">` 래퍼를 박는데 그것도 통과     |
| 정의 리스트 (`;Term :Def`)   | `<dl><dt><dd>`                         | 그대로          |                                                               |
| 표 `| col |`                 | `<table class="inline"><tr><th><td>`   | table 구조 보존 | `class="inline"` 보존 (Confluence 무시; 정렬은 inline style 로 전달됨) |
| `----` (수평선)              | `<hr/>`                                | self-closed 유지 |                                                               |
| 외부 URL `http://...`        | `<a class="urlextern" href="...">`     | a href 보존     | `urlextern` class 보존; href 자체는 Confluence 가 인식        |
| 인터위키 `[[doku>X]]`        | `<a class="interwiki iw_doku" href="...">` | a href 보존  | interwiki* class 는 제거 (D), href 만 보존                    |
| 이메일 `<x@y>`               | `<a href="mailto:...">`                | 그대로          |                                                               |
| `<sub>`/`<sup>`/`<abbr>` 등 inline | 그대로                            | 그대로          |                                                               |

**검증**: wiki:syntax 의 raw 65 a / 14 h2 / 12 h3 / 9 blockquote / 7 em /
5 strong / 5 sup / 1 sub / 1 del / 3 br / 1 hr — 모두 storage XML 에서
구조 보존됨 (개수 일부 변화는 B 카테고리 변환의 부수 효과).

## B. Confluence 매크로/구조로 변환 (Transformed)

dokuwiki 만의 시각/의미 마크업을 Confluence 의 *동등한 native 표현*으로
능동 변환. 변환기 코드의 핵심 로직.

| DokuWiki | DokuWiki HTML 출력 | Confluence 결과 | 변환기 위치 |
|----------|--------------------|-----------------|-------------|
| 내부 페이지 링크 `[[page]]` | `<a class="wikilink1" data-wiki-id="..." href="...">` | 1차: `<a href="dwc-link:<id>[#anchor]">` placeholder. 2차 (S7 rewrite-links): `<ac:link><ri:page ri:content-title="<title>"/><ac:plain-text-link-body><![CDATA[text]]></...></ac:link>` | `_convert_html_to_storage` → `_rewrite_links_in_xml` |
| 이미지 `{{:file.png}}` | `<img src="/_media/..." class="media">` | `<ac:image ac:width=.. ac:alt=..><ri:attachment ri:filename="file.png"/></ac:image>` | `_convert_html_to_storage` step 3 |
| 클릭 가능한 이미지 `[[link\|{{img}}]]` | `<a><img/></a>` (단일 자식) | 외부 `<a>` 언래핑 후 `<ac:image>` 만 남김 | 같은 step 3 |
| 외부 이미지 proxy `{{http://...}}` | `<img src="/lib/exe/fetch.php?media=https%3A%2F%2F...">` | `<img src="https://...">` (디코딩된 실제 URL 로 교체) | `_categorize_href` + step 3 |
| 미디어 파일 링크 (비-이미지) | `<a class="media" href="/_media/...resume.pdf">` | `<ac:link><ri:attachment ri:filename="resume.pdf"/><ac:link-body>...</...></ac:link>` | step 4 |
| 코드 블록 `<code lang>...</code>` | `<pre class="code python">...</pre>` | `<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">python</...><ac:plain-text-body><![CDATA[...]]></...></...>` | step 5; `]]>` 는 canonical `]]]]><![CDATA[>` 로 이스케이프 |
| 파일 인용 `<file>...</file>` | `<pre class="file">` | 동일하게 code 매크로 | step 5 |
| `<WRAP info>` / 변형 | `<div class="wrap_info ... plugin_wrap">` | `<ac:structured-macro ac:name="info"><ac:rich-text-body>...</...></...>` | `_convert_wrap_callouts` |
| `<WRAP tip>` | `wrap_tip` | `tip` 매크로 | 동일 |
| `<WRAP important\|note>` | `wrap_important` / `wrap_note` | `note` 매크로 | 동일 |
| `<WRAP alert\|warning\|danger>` | `wrap_alert/warning/danger` | `warning` 매크로 | 동일 |
| `<WRAP box\|round>` (제목 없는 박스) | `wrap_box` / `wrap_round` | `panel` 매크로 | 동일 |
| `<wrap em>X</wrap>` (인라인 강조) | `<em class="wrap_em ...">X</em>` | `<strong>X</strong>` | 동일 (inline pass) |
| `<wrap hi>X</wrap>` (인라인 형광펜) | `<em class="wrap_hi ...">X</em>` | `<span style="background-color: #fff59d;">X</span>` | 동일 |
| todo `<todo>X</todo>` (pure-todo 목록) | `<ul><li><span class="todo"><input ...>...</span></li>...</ul>` | `<ac:task-list><ac:task><ac:task-id>N</...><ac:task-status>complete\|incomplete</...><ac:task-body>X</...></...>` | `_convert_todos` step-1 |
| todo (mixed/nested) | 위와 같은 마크업이 텍스트와 섞인 li | `[x] X` / `[ ] X` 인라인 텍스트 마커 (Confluence task-list 가 block-level 이라 inline 컨텍스트 회피) | `_convert_todos` step-2 |
| `?do=edit` 액션 링크 (section edit) | `<a href="?do=edit&id=...">` | 액션 링크는 textContent 만 남기고 `<a>` 언래핑 | `_categorize_href` action 분기 |
| HTML 코멘트 `<!-- EDIT{...} -->` | 그대로 | 모두 제거 (D 참조) | comment 일괄 제거 |

**보조 변환 (간접)**:
- `[[page|alias text]]` 의 alias text 가 dwc-link placeholder 의 anchor 내부 텍스트로 보존. S7 의 ac:plain-text-link-body 에 CDATA 로 들어감.
- 코드 블록의 syntax-highlight `<span>` 들은 get_text() 로 평탄화되어 평문으로.

## C. 부분 변환 / 시각 효과 손실

storage XML 에는 들어가지만 Confluence 가 dokuwiki 의 CSS class 를 모르기
때문에 시각 효과가 사라지는 케이스. *데이터는 보존*되지만 *외관은 일반
텍스트와 동일*.

| 요소 | DokuWiki 효과 | Confluence 결과 | 처리 가능성 |
|------|---------------|-----------------|-------------|
| `<WRAP left\|right\|center>` (정렬 전용) | div 의 text-align CSS | div 그대로 (class 무시됨, 시각 효과 0). 87 페이지 영향 | 후속 PR 에서 `style="text-align: ..."` 인라인 속성으로 변환 가능 (Confluence 가 인라인 style 은 받아들임) |
| `<WRAP clear\|indent\|outdent>` (레이아웃) | layout flow 효과 | div 보존 (효과 0) | 사용 사례 적어 격하 OK |
| `__underline__` | em.u 의 밑줄 스타일 | `<em class="u">` 보존 (Confluence 의 CSS 없음) | 후속 PR 에서 `<u>` 또는 `<span style="text-decoration: underline">` 로 변환 가능 |
| `[[page]]` 의 *broken page link* (대상 페이지 없음) | `<a class="wikilink2">` 빨강 표시 | 미해결 placeholder → S7 에서 텍스트로 격하 | 의도된 동작: 깨진 링크보단 일반 텍스트가 안전 |
| dokuwiki 의 표 정렬 (`| 가운데 |`) | `<td class="centeralign">` | class 보존 (Confluence CSS 없음) | 후속 PR 에서 인라인 style 로 변환 가능 |
| 외부 링크의 `target="_blank"` / `rel="noopener"` | 새 탭 열기 | 일부 attribute 보존 | Confluence 의 기본 외부 링크 정책에 맞춰 자동 처리됨 |

## D. 의도적 누락 (Dropped)

변환기가 *명시적으로 제거*하는 요소. 안전·노이즈 제거·재현 불가 때문.

### D.1 위험/인터랙티브 태그 (`_convert_html_to_storage` step 1)

전부 `decompose()` 로 트리에서 제거:

| 태그 | 이유 | 발견 빈도 (corpus pre-fix) |
|------|------|---------------------------|
| `<script>` | Confluence storage 거부, XSS 위험. dokuwiki 가 jquery 로더를 일부 페이지에 박는다. | 1208 파일 |
| `<style>` | inline CSS 거부 | 미관측 |
| `<link>` | 외부 stylesheet 참조 거부 | 1208 파일 |
| `<meta>` | head meta 거부 | 1208 파일 |
| `<noscript>` | 비활성 컨텐츠 | 미관측 |
| `<iframe>` / `<embed>` / `<object>` | 외부 embedding 거부 | 미관측 |
| `<form>` / `<input>` / `<button>` / `<select>` / `<option>` / `<textarea>` | 인터랙티브 폼 거부. dokuwiki 의 검색폼/로그인폼/도구 박스가 페이지에 묻어 들어옴. | form 1208, input 191 (todo plugin) |
| `<head>` | 헤더 누설 (export_xhtmlbody 가 풀 HTML 응답 시) | 1208 파일 |

### D.2 unwrap (children 보존, 컨테이너만 제거)

| 태그 | 이유 |
|------|------|
| `<html>` / `<body>` | 풀 HTML 응답 시 wrapper 만 제거하고 자식 보존 |

### D.3 DokuWiki chrome 제거 (id/class 기반)

ACL-denied 페이지나 풀 HTML 응답 시 묻어 들어오는 dokuwiki 사이트 chrome:

| 선택자 | 의미 |
|--------|------|
| `#dokuwiki__site`, `#dokuwiki__top`, `#dokuwiki__header`, `#dokuwiki__footer`, `#dokuwiki__pagetools`, `#dokuwiki__aside`, `#dokuwiki__usertools`, `#dokuwiki__sitetools` | 페이지 chrome (헤더/푸터/내비/도구) |
| `.breadcrumbs`, `.trace`, `.tools`, `.docInfo`, `.no`, `.headings` | chrome 부속 |

### D.4 DokuWiki 메타 마커

| 대상 | 이유 |
|------|------|
| `<a class="secedit" href="?do=edit...">` | 헤딩 옆 섹션 편집 앵커 (UI only) |
| `<div class="toc">`, `#dw__toc` | 자동 생성 TOC (Confluence 는 자체 TOC 매크로 사용) |
| `<!-- EDIT{...} -->` | 모든 HTML 코멘트 제거. dokuwiki section-edit JSON 메타가 일부 경로에서 가시 텍스트로 누수되는 이슈 회피 |

### D.5 노이즈 class 제거 (`NOISE_CLASS_PREFIXES` / `NOISE_CLASS_EXACT`)

요소 자체는 보존, class 속성만 정리. dokuwiki 가 부여한 메타용 class 들:

| prefix | 예시 | 이유 |
|--------|------|------|
| `sectionedit` | `sectionedit1`, `sectionedit42` | 섹션 카운터 (UI only) |
| `wikilink` | `wikilink1` (존재), `wikilink2` (broken) | 페이지 링크 상태 — placeholder 로 처리하므로 class 불필요 |
| `level` | `level1`, `level2`, `level3` | 헤딩 깊이 표시 (CSS only) |
| `media` | `media`, `mediafile`, `mediafile mf_pdf` | 미디어 종류 (변환기가 별도로 처리하므로 class 불필요) |
| `interwiki` | `interwiki`, `iw_doku` | 인터위키 short-name |
| `plugin_` | `plugin_wrap`, `plugin_include__<id>`, `plugin_tag` 등 | dynamic 플러그인 클래스 (e.g. include 의 페이지 ID 박은 동적 class) |

| exact | 이유 |
|-------|------|
| `toc` | TOC 컨테이너 (D.4 에서 통째 제거되지만 잔여 class 도 정리) |
| `page` | dokuwiki 의 `<div class="page">` (콘텐츠 추출 후 wrapper class 만 남음) |
| `dokuwiki` | top-level dokuwiki container class |
| `plugin_include_content` | include plugin 의 정확 매치 컨테이너 |

## E. 별도 트랙 (Out-of-pipeline)

본 파이프라인은 *현재 시점 페이지 본문*에 한정. 아래는 별도 시나리오:

| 영역 | 별도 문서 | 채택안 |
|------|-----------|--------|
| ACL / permissions | (없음) | 단일 boundary 가정. 과거 ACL 보존 안 함. |
| 과거 리비전 / 변경 이력 | `docs/history-migration.md` | B + A (시간순 PUT replay + 본문 푸터 메타) + F (미디어 attachment 버전 체인). 별도 PR. |
| struct 플러그인 데이터 (sqlite) | `docs/struct-migration.md` | A native Database → B Page Properties → C 스냅샷 우선순위. 별도 PR. |
| 코멘트 (`meta/_comments.changes`) | (없음) | 비범위 |
| 양방향 동기화 | (없음) | 비범위 |

## F. 미관측이지만 일반 케이스 대비 (handled by passthrough)

본 corpus 에 사용 안 됐으나 다른 인스턴스에서 발견 가능:

| dokuwiki | dokuwiki HTML | Confluence 결과 |
|----------|---------------|-----------------|
| 풋노트 `((text))` | `<sup><a href="#fn__1" class="fn_top">1)</a></sup>` + footer `<div class="footnotes">` | sup 보존, 풋노터 div 보존. 시각 효과는 일반 텍스트 (D 의 class 정리 후) — 후속 PR 에서 `<ac:structured-macro ac:name="footnote">` 으로 매핑 가능 |
| windows 공유 링크 `[[\\host\share]]` | `<a class="windows" href="file:...">` | a 그대로, file: 스킴 보존 |
| 다중 컬럼 표/병합 | `colspan` / `rowspan` | 그대로 보존 |
| `~~NOTOC~~`, `~~NOCACHE~~` | 출력 영향만 (dokuwiki TOC 생략 등) | 매크로 자체는 코어가 직접 처리, dokuwiki 출력에 마커 없음 |
| 매크로 `{{INFO>...}}` 등 | 플러그인별 출력 (info 플러그인 등) | 플러그인 마크업에 따라 D.1 (script 등) 으로 떨어지거나 일반 div/table 로 통과. 발견 시 `plugin-validation.md` 에 row 추가. |

## G. corpus 통계 요약 (1,567 페이지 변환 후)

| 항목 | 값 |
|------|----|
| 변환 성공 | 1,567 (100%) |
| Confluence 매크로 생성: code | 17,xxx 추정 (페이지마다 평균 수 개; wiki:syntax 단독 37개) |
| Confluence 매크로: info / tip / note / warning / panel | 33 / 36 / 17 / 9 / 48 파일 (합 143) |
| Confluence 매크로: task-list | 88 파일 / 1,547 task |
| `[x]/[ ]` 텍스트 폴백 (mixed todo) | 189 파일 |
| `<ac:image>` 변환 | 8 (wiki:syntax 한 페이지 기준; 전체는 더 많음) |
| 첨부 DISCOVERED | 10,659 |
| 잔존 위험 태그 (script/form/head/iframe/input/button/select/textarea) | **0** |

## H. 새 요소가 발견됐을 때

1. `grep -lr '<pattern>' storage --include='*.xml' | wc -l` 로 영향 페이지 수 측정.
2. raw HTML 에서 dokuwiki 가 어떻게 그 요소를 렌더하는지 확인.
3. 분류:
   - 표준 HTML 이고 Confluence storage 가 받음 → **A**. 별도 작업 없음.
   - dokuwiki 만의 시각/의미 마크업 → **B**. `_convert_html_to_storage` 또는 별도 helper 에 변환 룰 추가.
   - 위험/노이즈 → **D**. strip list 에 추가.
   - 시각 효과 손실 허용 가능 → **C**. 본 문서 §C 표에 한 줄 추가.
4. 변환기 코드 수정 후 `run.py convert --force` 로 전체 재변환.
5. 본 문서의 해당 카테고리 표에 한 줄 추가 + corpus 통계 §G 갱신.
6. 별도 CL 로 P4/Git 제출 (의미 있는 단위).
