# 3-측 렌더링 invariant audit (시나리오)

본 문서는 사용자가 비교 갤러리 (`compare-publish`) 의 양측 스크린샷을
*직접* 비교하여 발견한 4 사례를 일반화해 *코드로 자동 검출* 하는 방안을
정리한다. 기존 audit / verify Phase 1-4 가 못 잡는 *3-측 무결성* 신호.

상태: **시나리오 (미구현)**. 구현 시 `verify` 또는 새 명령 `audit-3way`
로 통합 후속 사이클.

---

## 1. 문제 정의

### 1.1 기존 자동화의 한계

| 도구 | 비교 차원 | 못 잡는 케이스 |
|------|----------|---------------|
| `audit` | dokuwiki rendered text ↔ confluence text/macro | *dokuwiki 가 이미 잘못 렌더링* 한 경우 (양측 같음 → OK) |
| `verify` Phase 1-3 | 매크로 카운트 + 문장 정렬 + LCS | dokuwiki 측 누락은 *양측 동시 누락* → mismatch 안 됨 |
| `verify` Phase 4 (pixel-diff / phash / OCR / bbox-LCS / storage-AST / color-hist) | 시각 픽셀 | *시각적으로 같은데 의미가 다른* 변형 (image cluster 분리 등) 못 잡음 |

→ *원본 dokuwiki source (.txt)* 가 비교 base 로 들어와야 함. *3-측 invariant*.

### 1.2 사용자 발견 4 사례

#### 사례 A. iframe 누락 (htmlok plugin 미설치)
- **source** (`u/lam/calendar.txt`):
  ```
  <html><iframe src="https://calendar.google.com/calendar/embed?..."></iframe></html>
  ```
- **dokuwiki rendered**: `&lt;html&gt;&lt;iframe&gt;...` (escape 텍스트)
- **confluence**: 같은 escape 텍스트
- **책임**: dokuwiki 환경 (DokuWiki Jack Jackrum 부터 `<html>` core 제거 + plugin 누락)
- **기존 비교**: 양측 같으므로 OK → 미발견

#### 사례 B. calendar 누락 (monthcal plugin 미설치)
- **source**: `~~monthcal~~` 또는 `{{calendar:start}}`
- **dokuwiki rendered**: 깨진 plugin 매크로 잔재 또는 빈 영역
- **confluence**: `_convert_monthcal_fallback` 가 정적 표로 처리 (선의), 또는 그대로 누락
- **책임**: dokuwiki 환경 (plugin 누락) — fallback 변환기 있으면 *허용*
- **기존 비교**: 양측 모두 빈 결과 → OK 분류

#### 사례 C. 연속 이미지 → embedding 변형 (변환기 책임)
- **source**: `{{:ride:f:img1.jpg}}{{:ride:f:img2.jpg}}{{:ride:f:img3.jpg}}`
- **dokuwiki rendered**: `<p><img/><img/><img/></p>` *연속 인라인 그룹*
- **confluence**: 각 img 가 *별도 block* / *gallery 매크로* / *embedding 형식* 으로 변형 → 시각 leg 다름
- **책임**: 변환기 (image cluster 보존 미흡)
- **기존 비교**: 매크로 카운트 일치 (img 3개 양측), text 동일 → OK

#### 사례 D. 색상 wrap → 코드블록 오변환 (변환기 책임)
- **source**: `<WRAP color #yellow>...</WRAP>` (예시)
- **dokuwiki rendered**: `<div class="wrap_color" style="background:yellow">...`
- **confluence**: `<ac:structured-macro ac:name="code">...` (의도와 정반대 매핑)
- **책임**: 변환기 (`_convert_wrap_callouts` 의 unknown wrap class fallback 이 잘못)
- **기존 비교**: 매크로 카운트 차이 — 잡힐 *가능성* 있으나 *왜* 가 불명

---

## 2. 3-측 비교 모델

### 2.1 세 측

| 측 | 위치 | 의미 |
|---|------|------|
| **S** source | `<dwdata>/pages/<doku_id>.txt` | *작성자의 의도* |
| **D** dokuwiki rendered | `raw/<doku_id>.html` (export_xhtmlbody 캐시) | *dokuwiki 환경의 해석* |
| **C** confluence storage | `storage/<doku_id>.xml` (또는 v2 GET) | *마이그레이션 결과* |

### 2.2 두 invariant 차원

| 차원 | 정의 | 위반 시 책임 |
|------|------|-------------|
| **S ↔ D** | source 의 매크로 marker 가 rendered 에 *결과 element* 로 나타남 | dokuwiki 환경 (plugin / 설정) |
| **D ↔ C** | rendered 의 element 가 confluence 에 *대응 매크로/구조* 로 매핑 | 변환기 |

S ↔ C 는 *위 두 차원의 합성* — 별도 신호 만들기보다 두 차원 위반을 합산.

---

## 3. 검출 신호 (15 후보)

### 그룹 1: S ↔ D (dokuwiki 환경)

#### S1. plugin marker / rendered element 부재
source 의 매크로 marker 가 있는데 rendered 의 결과 element 가 없으면 *plugin 누락 의심*.

매핑 테이블 (구현 시 dict):

| plugin | source 패턴 | rendered 결과 element |
|--------|------------|---------------------|
| `htmlok` | `<HTML>` / `<html>` (line-start) | `<iframe>` / `<script>` / `<embed>` (raw HTML) |
| `monthcal` | `~~monthcal\b` / `{{calendar:...}}` | `<table class="monthcal_...">` |
| `wrap` | `<WRAP>` / `<wrap>` | `<div class="wrap_...">` |
| `iframe` (Chris--S) | `{{url>http...}}` | `<iframe src=...>` |
| `todo` | `<todo>` | `<input type="checkbox">` / `<ul class="todo">` |
| `include` | `{{section>...}}` / `{{page>...}}` | `class="plugin_include"` |
| `struct` (datatable) | `<datatable>` / `<datatemplatelist>` | `class="plugin_struct"` |
| `youtube` | `{{youtube>VID}}` | `<iframe src="...youtube...">` |
| `bbcode` | `[b]...[/b]` / `[i]...[/i]` 등 | `<strong>` / `<em>` (외 element) |
| `discussion` | `~~DISCUSSION~~` | `<div class="comment_wrap">` |
| `tag` | `{{tag>...}}` | `<a class="tag">` |
| `gallery` | `{{gallery>...}}` | `<div class="plugin_gallery">` |

알고리즘:
```python
def signal_S1(source: str, rendered: str) -> list[dict]:
    violations = []
    for plugin, table_entry in PLUGIN_RENDER_INVARIANTS.items():
        if table_entry["source_re"].search(source):
            if not table_entry["rendered_required"].search(rendered):
                violations.append({
                    "signal": "S1.plugin_render_missing",
                    "plugin": plugin,
                    "responsibility": "source",
                    "severity": "high",
                    "fix_hint": table_entry["fix_hint"],
                })
    return violations
```

#### S2. escape text 노출
rendered 본문 텍스트 노드에 `&lt;TAG&gt;` (escape 된 매크로) 가 다수 있으면 *plugin 미해석 시그니처*.

```python
def signal_S2(rendered: str) -> list[dict]:
    text_nodes = extract_text_only(rendered)
    matches = re.findall(r"&lt;([a-zA-Z][a-zA-Z0-9_-]+)\b", text_nodes)
    if matches:
        return [{
            "signal": "S2.escape_text_exposed",
            "tags": Counter(matches).most_common(5),
            "responsibility": "source",
            "severity": "medium",
        }]
    return []
```

#### S3. plugin warning 박스
DokuWiki 가 *plugin 미설치 경고* 를 빨간 박스로 표시:
`<div class="error">Plugin installed incorrectly...</div>` 같은 패턴.

→ rendered 검색만으로 검출 가능.

### 그룹 2: D ↔ C (변환기)

#### D1. 매크로 카운트 mismatch
rendered 의 의미 element 카운트 vs confluence 의 대응 매크로 카운트:

| rendered element | confluence 매크로 |
|------------------|------------------|
| `<div class="wrap_info">` | `<ac:structured-macro ac:name="info">` |
| `<div class="wrap_tip">` | `<ac:structured-macro ac:name="tip">` |
| `<div class="wrap_note">` / `wrap_important` | `<ac:structured-macro ac:name="note">` |
| `<div class="wrap_alert">` / `wrap_warning` / `wrap_danger` | `<ac:structured-macro ac:name="warning">` |
| `<table class="monthcal_...">` | `<table>` (정적 캘린더) |
| `<iframe src="calendar.google.com">` | `<ac:structured-macro ac:name="iframe">` |
| `<span class="encryptedpasswords">` | `<ac:structured-macro ac:name="expand">` |
| `<input type="checkbox">` | `<ac:task-list>` 또는 `[x]`/`[ ]` 텍스트 |
| `<pre><code class="LANG">` | `<ac:structured-macro ac:name="code">` |
| `<a class="urlextern">` | `<a href>` (외부) |
| `<a class="media">` | `<ri:attachment>` |

위반: rendered 에 `wrap_info` 가 N 개인데 confluence 에 `ac:name="info"` 가 M ≠ N 개 → 변환기 매핑 오류.

#### D2. wrap class 색상/배경 → code 오매핑
가장 중요한 신호 (사례 D):

```python
WRAP_KNOWN_CLASSES = {"wrap_info", "wrap_tip", "wrap_note", "wrap_important",
                     "wrap_alert", "wrap_warning", "wrap_danger",
                     "wrap_em", "wrap_hi", "wrap_box", "wrap_round",
                     "wrap_left", "wrap_right", "wrap_center"}

def signal_D2(rendered: str, confluence: str) -> list[dict]:
    # rendered 의 wrap_X (X = unknown class, 보통 색상/배경) 추출
    wraps_unknown = re.findall(
        r'<div[^>]*class="([^"]*wrap_[a-z]+[^"]*)"', rendered
    )
    wraps_with_style = [w for w in wraps_unknown
                        if "background" in w or "color" in w]
    code_count_confluence = confluence.count('ac:name="code"')
    code_blocks_rendered = rendered.count('<pre>') + rendered.count('class="code')

    if wraps_with_style and code_count_confluence > code_blocks_rendered:
        return [{
            "signal": "D2.wrap_color_to_code_misroute",
            "wraps_lost": len(wraps_with_style),
            "code_excess": code_count_confluence - code_blocks_rendered,
            "responsibility": "converter",
            "severity": "high",
            "fix_hint": "_convert_wrap_callouts 의 unknown wrap_class fallback 검토 — code 로 가지 말 것",
        }]
    return []
```

#### D3. 이미지 cluster 보존 (사례 C)
rendered: `<p>` 안에 `<img>` 3+ 인라인. confluence: 각각 별도 `<p>` 또는 다른 형식.

```python
def signal_D3(rendered_soup, confluence_str: str) -> list[dict]:
    clusters_rendered = []
    for p in rendered_soup.find_all('p'):
        imgs = p.find_all('img')
        if len(imgs) >= 3:
            clusters_rendered.append([img.get('src') for img in imgs])

    # confluence: ac:image 가 같은 group (<p> 또는 ac:layout-cell) 안에 3+?
    # 간단히 storage XML 의 <ac:image> 그룹화
    cluster_confluence = analyze_image_groups(confluence_str)

    if len(clusters_rendered) > len(cluster_confluence):
        return [{
            "signal": "D3.image_cluster_split",
            "rendered_clusters": len(clusters_rendered),
            "confluence_clusters": len(cluster_confluence),
            "responsibility": "converter",
            "severity": "medium",
            "fix_hint": "img inline group 보존 — 별도 <p> scatter 방지",
        }]
    return []
```

#### D4. 표 셀 카운트
rendered `<table>` 의 cell 수 vs confluence `<table>` cell 수 (절대 일치).

#### D5. 코드블록 language attribute
rendered: `<pre><code class="LANG">`. confluence: `<ac:parameter ac:name="language">LANG</ac:parameter>`.

```python
def signal_D5(rendered_soup, confluence_str: str) -> list[dict]:
    rendered_langs = []
    for code in rendered_soup.find_all('code'):
        cls = code.get('class') or []
        for c in cls:
            if c.startswith('language-') or c in ('python', 'bash', 'sql', 'json'):
                rendered_langs.append(c.replace('language-', ''))
    confluence_langs = re.findall(
        r'<ac:parameter ac:name="language">([^<]+)</ac:parameter>',
        confluence_str
    )
    missing = set(rendered_langs) - set(confluence_langs)
    if missing:
        return [{
            "signal": "D5.code_language_lost",
            "langs": list(missing),
            "responsibility": "converter",
            "severity": "low",
        }]
    return []
```

#### D6. 외부 링크 vs 첨부 링크 분리
rendered `class="urlextern"` 은 외부 → confluence `<a href>`. `class="media"` 는 첨부 → `<ri:attachment>`. 교차 매핑이면 위반.

#### D7. 첨부 reference 누락
rendered 의 `<a class="media" href="?media=...">` 수 vs confluence 의 `<ri:attachment>` 수 — 절대 일치.

#### D8. 헤딩 레벨 유지
rendered `<h1>~<h6>` 시퀀스 vs confluence `<h1>~<h6>` — 같은 시퀀스.

### 그룹 3: 시각 보조 (V1~V3, Phase 4 미보완 영역)

V1, V2, V3 는 기존 verify Phase 4 와 일부 겹치므로 본 시나리오에선 *부속* — 우선순위 낮음.

---

## 4. 책임 분류 + severity

각 신호의 결과 카테고리:

| 분류 | 의미 | 대응 |
|------|------|------|
| `source.high` | dokuwiki 환경 문제 (plugin 누락 등) — 본 인스턴스 모든 페이지 동일 | dokuwiki 측 수정 (plugin 설치) |
| `source.medium` | 일부 페이지에만 영향 (특정 매크로 사용) | dokuwiki 측 수정 |
| `converter.high` | 변환기 버그 — 정보 손실 (사례 C, D) | 코드 fix |
| `converter.medium` | 변환기 변형 — 의도적 가능 (사례 B 의 fallback) | 검토 후 화이트리스트 또는 fix |
| `converter.low` | cosmetic (language attribute 등) | nice-to-have fix |
| `inconclusive` | 양측 모두 의심 | 수동 확인 |

규칙:
1. S ↔ D 위반 = `source` 그룹
2. D ↔ C 위반 = `converter` 그룹
3. 둘 다 위반 = 두 항목 별도 기록 (independent)

화이트리스트 (의도적 변형):
- `_convert_monthcal_fallback` 가 처리한 page: D ↔ C 의 `table-vs-monthcal` 차이는 *fallback 의도* — 허용. signal output 에 `"intent": "fallback"` 표시.
- `_convert_smileys` 가 처리한 smiley `<img>` → emoji 변환은 D ↔ C 의 image 손실로 잡힐 수 있는데 *의도된 cosmetic* — 허용.

---

## 5. 운영 모델

### 5.1 새 명령: `audit-3way`

```sh
python run.py audit-3way [--sample N] [--full] [--only doku_id]
                         [--with-source] [--dokuwiki-data PATH]
                         [--output-json PATH] [--output-html PATH]
                         [--severity-threshold high|medium|low]
```

흐름:
1. 대상 페이지 selection (sample 또는 명시)
2. 각 페이지 마다:
   1. source 읽기 — `<dwdata>/pages/<doku_id>.txt` (`--with-source`)
   2. rendered 읽기 — `raw/<doku_id>.html`
   3. confluence 읽기 — `storage/<doku_id>.xml` (로컬) 또는 v2 API GET
   4. 신호 계산 (S1, S2, S3, D1~D8)
   5. 책임 분류 + severity 부여
3. 출력 JSON + HTML 리포트 + state.db `audit_3way` 테이블 update

### 5.2 HTML 리포트 구조

```
페이지 N개 검사 / violation X개

[페이지별 카드]
  doku_id: u:lam:calendar
  source 측 violation 1:
    [S1.plugin_render_missing] plugin=htmlok severity=high
    fix_hint: saggi-dw/dokuwiki-plugin-htmlok 설치 + conf 활성
    [source snippet]   ← `<html><iframe src=...></iframe></html>`
    [rendered snippet] ← `&lt;html&gt;&lt;iframe&gt;...`
    [confluence snippet] ← (변화 없음)

  converter 측 violation 0
```

### 5.3 state.db 새 스키마

```sql
CREATE TABLE IF NOT EXISTS audit_3way (
    doku_id TEXT PRIMARY KEY,
    audited_at TEXT NOT NULL,
    content_hash TEXT,                  -- stale 추적 (storage 의 hash)
    rendered_hash TEXT,                 -- rendered 변화 추적
    violations_json TEXT NOT NULL,      -- list of {category, signal, severity, snippet, fix_hint}
    source_high INTEGER DEFAULT 0,
    source_medium INTEGER DEFAULT 0,
    converter_high INTEGER DEFAULT 0,
    converter_medium INTEGER DEFAULT 0,
    converter_low INTEGER DEFAULT 0,
    inconclusive INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_3way_severity_idx
    ON audit_3way (converter_high, source_high);
```

### 5.4 wizard 통합 (옵션)

새 wizard step 추가: **Step 13. 3-측 audit**
- audit-3way --full → state.db 갱신
- converter_high > 0 페이지가 있으면 wizard 멈춤 (변환기 버그 fix 권장)
- source_high > 0 페이지는 정보 표시만 (plugin 설치 권장, 진행은 가능)

---

## 6. 구체적 매핑 테이블 (구현 시작점)

### 6.1 PLUGIN_RENDER_INVARIANTS (S↔D)

```python
PLUGIN_RENDER_INVARIANTS: dict[str, dict] = {
    "htmlok": {
        "source_re": re.compile(r"</?(html|HTML)>"),
        "rendered_required": re.compile(r"<iframe|<script|<embed", re.I),
        "rendered_escape_marker": re.compile(r"&lt;(html|HTML|iframe|script)"),
        "fix_hint": "saggi-dw/dokuwiki-plugin-htmlok 설치 + "
                    "$conf['plugin']['htmlok']['htmlok'] = 1 활성. "
                    "DokuWiki Jack Jackrum 부터 <html> core 제거됨.",
    },
    "monthcal": {
        "source_re": re.compile(r"~~monthcal\b|\{\{calendar:"),
        "rendered_required": re.compile(r'<table[^>]*class="[^"]*monthcal'),
        "fix_hint": "monthcal plugin 설치 — 또는 _convert_monthcal_fallback 가 "
                    "정적 표로 처리 (변환기 측 처리, signal 화이트리스트 가능).",
    },
    "wrap": {
        "source_re": re.compile(r"<WRAP\b|<wrap\b", re.I),
        "rendered_required": re.compile(r'<div[^>]*class="wrap_'),
        "fix_hint": "wrap plugin 설치. 본 인스턴스에선 활성됨.",
    },
    "iframe": {  # Chris--S iframe plugin (별개 — {{url>...}} syntax)
        "source_re": re.compile(r"\{\{url>"),
        "rendered_required": re.compile(r"<iframe"),
        "fix_hint": "Chris--S/dokuwiki-plugin-iframe 설치 + 활성.",
    },
    "todo": {
        "source_re": re.compile(r"<todo\b"),
        "rendered_required": re.compile(r'<input type="checkbox|<ul[^>]*class="todo'),
        "fix_hint": "todo plugin 설치.",
    },
    "include": {
        "source_re": re.compile(r"\{\{(?:section|page)>|~~INCLUDE\b"),
        "rendered_required": re.compile(r'class="plugin_include'),
        "fix_hint": "include plugin 설치.",
    },
    "struct": {
        "source_re": re.compile(r"---- *datatable\b|<datatemplatelist"),
        "rendered_required": re.compile(r'class="(?:plugin_struct|struct_table)'),
        "fix_hint": "struct plugin 설치. "
                    "마이그레이션 시 별도 트랙 (docs/struct-migration.md).",
    },
    "youtube": {
        "source_re": re.compile(r"\{\{youtube>"),
        "rendered_required": re.compile(r'<iframe[^>]*src="[^"]*youtu'),
        "fix_hint": "youtube plugin 설치 — 또는 _convert_youtube_fallback "
                    "(미설치 fallback 변환기) 가 작동하면 OK.",
    },
    "encryptedpasswords": {
        "source_re": re.compile(r"<(?:decrypt|encrypt)>"),
        "rendered_required": re.compile(
            r'<span[^>]*class="encryptedpasswords"|&lt;(?:decrypt|encrypt)&gt;'
        ),
        "fix_hint": "encryptedpasswords plugin 설치 (rename: encryptedpasswords/) "
                    "또는 _preprocess_encrypted_passwords 가 escape 케이스 처리.",
    },
}
```

### 6.2 DOKUWIKI_TO_CONFLUENCE_MACROS (D↔C)

```python
DOKUWIKI_TO_CONFLUENCE_MACROS: list[dict] = [
    {
        "name": "wrap_info",
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_info'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="info"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_info → info 매핑.",
    },
    {
        "name": "wrap_tip",
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_tip'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="tip"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_tip → tip.",
    },
    {
        "name": "wrap_note",  # 또는 wrap_important
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_(note|important)'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="note"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_note/important → note.",
    },
    {
        "name": "wrap_warning",  # warning/alert/danger
        "rendered_re": re.compile(r'<div[^>]*class="[^"]*wrap_(warning|alert|danger)'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="warning"'),
        "fix_hint": "_convert_wrap_callouts 의 wrap_alert/warning/danger → warning.",
    },
    {
        "name": "google_calendar_iframe",
        "rendered_re": re.compile(r'<iframe[^>]*src="[^"]*calendar\.google'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="iframe"'),
        "fix_hint": "_convert_google_calendar_iframe.",
    },
    {
        "name": "encryptedpasswords_span",
        "rendered_re": re.compile(r'<span[^>]*class="encryptedpasswords"'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="expand"'),
        "fix_hint": "_convert_encrypted_passwords (plugin 활성 케이스).",
    },
    {
        "name": "todo_checkbox",
        "rendered_re": re.compile(r'<input[^>]*type="checkbox"'),
        "confluence_re": re.compile(
            r'<ac:task-list|<ac:placeholder ac:type="checkbox"'
        ),
        "fix_hint": "_convert_todos.",
    },
    {
        "name": "code_block",
        "rendered_re": re.compile(r'<pre><code\b|<pre[^>]*class="code'),
        "confluence_re": re.compile(r'<ac:structured-macro[^>]*ac:name="code"'),
        "fix_hint": "코드 블록 변환 (S3 변환 파이프라인 STEP 9).",
    },
]
```

### 6.3 화이트리스트 (의도적 변형)

```python
INTENDED_TRANSFORMATIONS: dict[str, str] = {
    "monthcal_fallback":
        "monthcal → 정적 <table> (_convert_monthcal_fallback) — "
        "rendered 의 <table class='monthcal_*'> 가 부재해도 "
        "confluence 가 정적 표를 만들었으면 D↔C 위반 아님.",
    "smiley_to_emoji":
        "smiley <img> → emoji 텍스트 (_convert_smileys) — "
        "rendered 의 <img class='smiley'> 가 confluence 에 unicode 가 되어 "
        "image 카운트 mismatch 정상.",
    "youtube_fallback":
        "fallback /_media/youtube/<id> → iframe embed 매크로 "
        "(_convert_youtube_fallback) — rendered 의 깨진 미디어 링크가 "
        "확장된 매크로로 되어 의도된 손실.",
    "todo_inline_text":
        "inline / mixed todo → [x]/[ ] 텍스트 (보수적 fallback) — "
        "rendered 의 <input type='checkbox'> 가 텍스트가 되어 "
        "carbon copy 변환이 아님.",
}
```

---

## 7. 한계 + 폴백

1. **3 측 정확 alignment 불가능**: dokuwiki source 와 rendered 의 element 정확
   매핑이 plugin 마다 다름. 매핑 테이블 점진 확장 + 미매핑 plugin 은 *unknown
   pattern* 카테고리로 별도 출력.
2. **거짓 양성**:
   - source 의 매크로가 *주석* (`<!-- ... -->`) 안에 있음
   - source 의 매크로가 *코드 블록* (`%% ... %%`) 안에 있음
   - source 의 매크로가 *escape 처리* (\<HTML\>) 됨
   → source 매칭 전 코드/주석 영역 strip 필요.
3. **변환기의 합법적 변형**: 위 6.3 화이트리스트 + 미발견 의도 변형은
   *수동 검토* 후 화이트리스트 추가.
4. **plugin 매크로의 fallback 매핑**: monthcal/youtube 의 fallback 변환기는
   *source 측 plugin 누락* 신호와 동시 발생. 변환기가 *fallback 의도* 로 처리
   했다면 그 페이지에 한해 화이트리스트 적용.
5. **3 측 데이터 미존재**: history 트랙의 과거 리비전은 source 매핑 어려움
   (그 시점 plugin 설치 상태 다름). 본 audit 은 *현재 latest revision* 에만
   적용.

---

## 8. 시범 적용 시나리오

### 8.1 phase 1 — 알려진 4 사례 검증

본 인스턴스 데이터로 실행:
```sh
python run.py audit-3way --only u:lam:calendar          # 사례 A 검증
python run.py audit-3way --only u:lam:연속이미지페이지  # 사례 C
python run.py audit-3way --only u:lam:wrap색상페이지    # 사례 D
```

기대 출력:
- u:lam:calendar → `S1.plugin_render_missing.htmlok` (source.high)
- 연속이미지 페이지 → `D3.image_cluster_split` (converter.high)
- wrap 색상 페이지 → `D2.wrap_color_to_code_misroute` (converter.high)

### 8.2 phase 2 — full sample (1675 페이지)

```sh
python run.py audit-3way --full --output-html audit-3way.html
```

분류:
- **source.high** 추정: <50 페이지 (특정 plugin 사용 페이지만)
- **converter.high** 추정: <30 페이지 (변환기 버그 가능성)
- **inconclusive**: <100 페이지 (매핑 테이블 미커버)

### 8.3 phase 3 — 매핑 테이블 보강

inconclusive 페이지 수동 검토 → 새 매핑 추가 → 재실행 → inconclusive ↓.

매핑 신뢰성 임계점: inconclusive < 5% 이면 안정.

---

## 9. 구현 단계 (P1~P3)

### P1: 핵심 신호 + CLI (1주 추정)

- 새 함수 `_audit_3way_signals(source, rendered, confluence)` → violations list
- 매핑 테이블 4 plugin (htmlok / monthcal / wrap / encryptedpasswords) + 4 매크로 매핑 (wrap_info / wrap_warning / google_calendar / image_cluster)
- 새 명령 `cmd_audit_3way` + argparse
- JSON 출력만 (HTML 리포트는 P2)
- 화이트리스트 4 개

### P2: HTML 리포트 + state.db 통합 (1주)

- HTML 리포트: 페이지별 카드 + violation snippet inline (source / rendered / confluence)
- state.db `audit_3way` 테이블
- 매핑 테이블 8 plugin + 12 매크로 매핑으로 확장

### P3: wizard 통합 + Phase 4 신호 머지 (1주)

- 새 wizard step `audit_3way`
- 기존 verify 의 `--with-3way-audit` 옵션 (선택적 통합)
- AI vision 으로 *이미지 cluster grouping* 신호 보완 (Phase 4 와 cross-reference)

---

## 10. 신호별 *예상* 발견 (본 인스턴스)

| 신호 | 예상 수 | 메모 |
|------|--------|------|
| `S1.plugin_render_missing.htmlok` | ~5 페이지 | u:lam:calendar 외 4 곳 |
| `S1.plugin_render_missing.monthcal` | ~10 페이지 | _convert_monthcal_fallback 가 처리 — 화이트리스트 |
| `S2.escape_text_exposed` | ~3 페이지 | iframe / decrypt 자투리 — fix 안 된 것 |
| `D2.wrap_color_to_code_misroute` | ~5-15 페이지 | 사례 D 일반화 |
| `D3.image_cluster_split` | ~20-50 페이지 | 일지 페이지의 연속 스크린샷 |
| `D5.code_language_lost` | ~10 페이지 | low severity |
| `D6/D7/D8` 등 | <10 페이지 | 분포 적음 |

검출 후 *converter 측* 들은 후속 사이클로 fix → re-convert → re-upload.

---

## 11. 관련 문서

- `docs/visual-audit.md` — verify Phase 1-3 + Phase 4 신호 7 종
- `docs/visual-comparison-proposal.md` — Phase 4 매트릭스 (8 후보 중 7 채택)
- `docs/element-mapping.md` — DokuWiki → Confluence 요소 매트릭스
- `docs/plugin-validation.md` — 플러그인 동작 검증 (S1 매핑 base)
- `docs/scenarios.md` §7 — 라이브 버그 패턴 (D 그룹 신호 출처)
- `docs/MEMORY.md` — 함정 절 (S1 의 fix_hint 출처)

본 시나리오 구현 시 위 docs 의 매핑 테이블 / 함정 / scenarios 가 *신호의 정답
근거* 로 cross-reference 됨.
