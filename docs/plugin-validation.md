# DokuWiki 플러그인 검증 시나리오

본 문서는 마이그레이션 대상 DokuWiki 인스턴스의 플러그인이
`dev/dokuwiki-local` 컨테이너에서 정상 렌더링되는지 확인하고, 변환기
(`run.py convert`)가 그 출력을 Confluence storage format 으로 손실
없이 옮기는지 검증하는 절차를 기술한다.

이 검증은 *현재 인스턴스 (`~/p4/playground/docker/dokuwiki/data`)*
기준이다. 다른 DokuWiki 환경에서 마이그레이션할 때는 §1 부터 다시
열거해야 한다.

## 1. 설치된 플러그인 열거

`lib/plugins/*/` 하위 디렉터리가 설치된 플러그인. `conf/plugins.local.php`
가 disabled 플래그를 덮어쓴다.

현재 인스턴스(2026-05-17):

| 플러그인       | 설치 | 활성 | 비고                                                |
|----------------|------|------|-----------------------------------------------------|
| acl            |  ✓   |  ✓   | 코어 ACL — dev 컨테이너에서는 `useacl=0` 으로 비활성화 |
| authad         |  ✓   |  ✗   | LDAP/AD 인증 — 비활성                               |
| authldap       |  ✓   |  ✗   |                                                     |
| authpdo        |  ✓   |  ✗   |                                                     |
| authplain      |  ✓   |  ✓   | 기본 사용자 DB                                      |
| blog           |  ✓   |  ✓   | `{{blog>}}` 매크로                                  |
| config         |  ✓   |  ✓   | 관리자 UI — 페이지 본문에 영향 없음                 |
| extension      |  ✓   |  ✓   |                                                     |
| include        |  ✓   |  ✓   | `{{page>}}`, `{{section>}}` 매크로                  |
| info           |  ✓   |  ✗   |                                                     |
| logviewer      |  ✓   |  ✓   |                                                     |
| pagelist       |  ✓   |  ✓   | tag, topic, blog 의 출력 포맷터로 사용              |
| popularity     |  ✓   |  ✓   |                                                     |
| revert         |  ✓   |  ✓   |                                                     |
| safefnrecode   |  ✓   |  ✓   | 파일명 인코딩 — 보조 기능                          |
| styling        |  ✓   |  ✓   |                                                     |
| tag            |  ✓   |  ✓   | `{{tag>}}` + `{{topic>}}` 매크로                    |
| todo           |  ✓   |  ✓   | `<todo>` 마크업                                     |
| upgrade        |  ✓   |  ✓   |                                                     |
| usermanager    |  ✓   |  ✓   |                                                     |
| wrap           |  ✓   |  ✓   | `<wrap>` 마크업                                     |

페이지 본문 렌더링에 실제로 영향을 주는 활성 플러그인은
**blog / include / pagelist / tag / todo / wrap** 6개.

## 2. 사용 빈도 (페이지 마크업 grep)

```sh
cd ~/p4/playground/docker/dokuwiki/data/data/pages
for pat in '{{tag>' '<wrap' '{{page>' '{{blog>' '{{topic>' '{{rss>' '<todo>'; do
  c=$(grep -rl "$pat" --include='*.txt' . | wc -l)
  echo "$c\t$pat"
done
```

결과 (2026-05-17):

| 매크로            | 페이지 수 |
|-------------------|-----------|
| `{{tag>...}}`     |       126 |
| `<wrap ...>...`   |        70 |
| `{{page>...}}`    |       176 |
| `{{blog>...}}`    |        12 |
| `{{topic>...}}`   |         3 |
| `{{rss>...}}`     |         2 |
| `<todo>...</todo>` | 다수     |
| `~~NOTOC~~`       |       210 |
| `~~NOCACHE~~`     |         3 |

`~~NOTOC~~`, `~~NOCACHE~~` 는 코어 마크업으로 변환기에 영향 없음.

## 3. 검증 절차

각 활성 매크로/마크업에 대해 다음 4단계를 수행한다.

1. **대표 페이지 식별** — 해당 매크로를 *실제로 호출*하는 페이지를
   골라낸다. 예) `{{topic>til}}` 처럼 인자가 들어간 경우만 — 코드
   블록 예시 안의 `{{topic>[list of tags]}}` 같은 설명문은 매크로가
   실행되지 않아 검증 대상이 아니다.
2. **dev 컨테이너에서 export 응답 조회** —
   `curl -s 'http://127.0.0.1:18080/doku.php?id=<doku_id>&do=export_xhtmlbody'`.
3. **렌더링 상태 판정** — 두 가지 기준:
   - *raw markup leak*: 응답에 `{{매크로>}}` 또는 `<wrap>` 등 원본
     마크업이 그대로 남아있으면 비정상.
   - *rendered marker*: 응답에 플러그인이 생성하는 클래스나 구조
     (`class="plugin_xxx"`, `<div class="tags">`, `<ul class="rss">`
     등) 가 있으면 정상.
4. **변환 결과 검사** — `python run.py convert --only <doku_id>
   --force` 후 `storage/<doku_id>.xml` 을 열어 Confluence storage 에
   부적합한 요소(`<input>`, `<script>`, `<form>` 등)가 잔존하지
   않는지, 의미있는 출력(텍스트, 표, 링크)이 보존되는지 확인.

## 4. 검증 결과 (2026-05-17)

### 4.1 정상 동작 + 변환 OK

| 플러그인  | 대표 페이지                    | dokuwiki 출력                                                | 변환 결과                                                              |
|-----------|--------------------------------|--------------------------------------------------------------|------------------------------------------------------------------------|
| tag       | `u:note:notetaking`            | `<div class="tags"><a href=/tag/... rel="tag">공부법</a></div>` | placeholder `<a href="dwc-link:tag:공부법">공부법</a>` (S7 후 해결됨)  |
| wrap      | `u:lam:2020`                   | `<em class="wrap_important plugin_wrap">목금 원격!!</em>`    | `<em class="wrap_important plugin_wrap">목금 원격!!</em>` 그대로 통과 |
| include   | `ride:구리300_cp목록`         | `<div class="plugin_include_content plugin_include__...">콘텐츠</div>` | div 의 plugin_ 클래스 제거, 콘텐츠 보존                                |
| blog      | `u:note:start`                 | `<div class="inclmeta">` + 페이지 메타/링크/태그                | 그대로 통과 (페이지 링크는 dwc-link placeholder 로 변환)               |
| topic     | `wiki:start` (`{{topic>til}}`) | `<table class="ul plgn__pglist">` 페이지 리스트 (9개)        | 표 + 링크 모두 보존, plgn__pglist 클래스도 보존                        |
| rss       | `wiki:syntax`                  | `<ul class="rss"><li>` slashdot 5개 항목, 외부 링크          | 외부 링크 그대로 통과; 캡쳐 시점 스냅샷                                |
| todo      | `u:lam:2020`                   | `<span class="todo"><input type=checkbox checked/>...텍스트` | `[x] 텍스트` 또는 `[ ] 텍스트` 인라인 마커 (CL 52689 이후)            |

검증 명령(요약):

```sh
PROBE() {
  resp=$(curl -s "http://127.0.0.1:18080/doku.php?id=$1&do=export_xhtmlbody")
  echo "$resp" | grep -oE "$2" | head -3
}
PROBE 'u:note:notetaking'         'class="plugin_tag[^"]*"|/_tag/'
PROBE 'ride:구리300_cp목록'      'class="plugin_include[^"]*"'
PROBE 'u:lam:2020'                'wrap_|plugin_wrap'
PROBE 'u:note:start'              'inclmeta|wikilink1 permalink'
PROBE 'wiki:start'                'plgn__pglist|tagslist'
PROBE 'wiki:syntax'               'class="rss"|<ul class="rss"'
PROBE 'u:lam:2020'                'class="todo"'
```

### 4.2 동작하지 않는 플러그인

**없음.** 활성 플러그인 6개(blog / include / pagelist / tag / todo / wrap) 모두 dev 컨테이너 (`php:8.2-apache` + DokuWiki 데이터
clone) 에서 정상 렌더링된다.

검증 도중 잠시 비정상으로 보였던 케이스와 진단:

| 증상                                                     | 원인                                       | 해결                                                  |
|----------------------------------------------------------|--------------------------------------------|-------------------------------------------------------|
| 일부 페이지가 로그인 폼 박힌 풀 HTML 응답 (`mode_denied`) | `useacl=1` + 광범위한 anonymous deny ACL   | `run.py dev up` 이 clone 의 `conf/local.php` 에 `useacl=0` 자동 패치 (CL 52688) |
| `wiki:dokuwiki-tag-plugin` 에서 topic 렌더링 안 보임     | 페이지 내용이 매크로 *설명문* (코드 블록 예시) — 매크로 실제 호출 아님 | 검증 대상이 아님; 실제 호출 케이스는 `wiki:start` 의 `{{topic>til}}` |
| `pages/.txt` 파일이 빈 렌더링                            | hidden file (dot file). DokuWiki 가 정식 페이지로 인식하지 않음 | render 가 자동 SKIPPED. 무시 가능                    |

### 4.3 부분 손실/주의

- **rss**: dokuwiki 가 export 시점의 외부 RSS 응답을 그대로 본문에
  박아 넣는다. Confluence 로 옮긴 후에는 *동적 갱신이 안 된다* — 옮긴
  시점의 스냅샷이 영구히 남는다. 자동 갱신이 필요하면 Confluence 의
  RSS 매크로로 수동 교체 필요. (현재 변환기는 rss 결과를 일반
  텍스트/링크 리스트로 보존.)
- **todo checkbox**: Confluence 의 task list 매크로로 변환하지 않고
  `[x] 텍스트` / `[ ] 텍스트` 단순 텍스트 마커로 격하. 시각적 상태는
  보존되지만 Confluence UI 에서 체크/언체크할 수 없다. 후속 수동
  작업이 필요한 경우 매크로 변환 룰을 추가 (현 변환기에는 미구현).

## 5. 재현 명령 한 줄

```sh
# 컨테이너 띄우기 + 한 페이지 검증 + 정리
python run.py dev up && \
  curl -s 'http://127.0.0.1:18080/doku.php?id=wiki:start&do=export_xhtmlbody' | head -20 && \
  python run.py dev down --purge
```

## 6. 새 플러그인을 추가했을 때

1. `lib/plugins/<name>/` 가 클론에 포함되었는지 확인 (`dev up` 이 새로
   복제할 때 자동 포함).
2. 페이지 마크업에서 그 플러그인의 syntax 빈도 측정 (`§2` 의 grep).
3. `§3` 의 4단계 검증을 새 플러그인에 대해 수행.
4. 결과를 `§4.1`/`§4.2`/`§4.3` 의 표에 행으로 추가.
5. 변환기가 부적합한 마크업을 만들면 `_convert_html_to_storage` 에
   변환 룰 추가 — 보통 `tag_name 일괄 제거` 또는 `class 단위 치환`
   으로 충분.
