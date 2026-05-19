# wizard 사용 시나리오 (walkthrough)

`python run.py wizard` 의 실제 사용 흐름을 시간순 내러티브로 정리한
문서. 첫 실행 / 중단·재개 / 실패 복구 / 부분 재실행 / 비대화 자동화의
각 시나리오를 *실제 콘솔 출력 샘플* 과 함께 보여준다.

원리 / 명령 옵션은 [`runbook.md §0`](runbook.md), 단계별 동작 함수는
`run.py` 의 `_wiz_*` / `WIZARD_STEPS` 참고.

---

## 시나리오 A — 처음부터 끝까지 (Happy Path)

다른 머신에 도구를 막 설치한 사용자가 처음 한 번 끝까지 가는 흐름.

### A.1 사전 준비

```sh
# 도구 설치 (DEPLOY.md §3 참고)
tar -xzf dokuwiki-to-confluence-cloud.tar.gz && cd dokuwiki-to-confluence-cloud
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 자격증명
cp .env.example .secrets/confluence.env
$EDITOR .secrets/confluence.env       # 6개 값 모두 채움
set -a; source .secrets/confluence.env; set +a
```

### A.2 wizard 시작

```sh
$ python run.py wizard
== DokuWiki → Confluence 마이그레이션 wizard ==

step               status       요약                               시작
--------------------------------------------------------------------------------
· prereq           pending
· dev-up           pending
· discover         pending
· render           pending
· plugin-audit     pending
· convert          pending
· upload           pending
· rewrite-links    pending
· history          pending
· struct           pending
· audit            pending
· verify           pending
· report           pending
· report-publish   pending
```

처음이라 모든 단계가 `pending`. 진행 표 다음에 첫 단계 프롬프트:

```
[1/14] 사전 점검 (자격증명/경로/CLI)
  step: prereq | 현재: pending
  진행/skip/quit ? [Enter/s/q]
```

`Enter` — 실행:

```
  DOKUWIKI_SRC             = /Users/you/dokuwiki/data
  CONFLUENCE_BASE_URL      = https://YOUR-DOMAIN.atlassian.net/wiki
  CONFLUENCE_EMAIL         = you@example.org
  CONFLUENCE_API_TOKEN     = ***
  CONFLUENCE_SPACE_KEY     = MIGRATE
  CONFLUENCE_ROOT_PAGE_ID  = 123456789
  docker available:   True
  curl available:     True   (data-only bootstrap 시 필요)
  tar available:      True   (data-only bootstrap 시 필요)
  ✓ env vars OK (6개) docker=True curl=True tar=True
```

### A.3 dev-up — DokuWiki 컨테이너 (선택)

라이브 dokuwiki 가 따로 있으면 `s` 로 skip. 없으면 `Enter`:

```
[2/14] DokuWiki 컨테이너 기동 (기존 데이터 복제)
  step: dev-up | 현재: pending
  (선택) 이 단계는 건너뛸 수 있습니다.
  진행/skip/quit ? [Enter/s/q]
  ? DokuWiki 컨테이너를 기동할까요? (이미 떠 있으면 skip 가능) [Y/n]: y
```

자동 분기 (DOKUWIKI_SRC 에 `doku.php`+`lib/`+`inc/` 가 있는지에 따라):

**full install 인 경우** — APFS clonefile 14GB 9초 복제:
```
[...] 호스트 데이터 복제: /Users/you/dokuwiki/data -> /tmp/dwc_test_dokuwiki/dwdata
[...]   ACL 비활성화 (useacl=0) 패치 적용 — 클론 한정
[...] docker compose up -d
[...] 헬스 체크 — 최대 30s 대기
[...] 준비 완료: http://127.0.0.1:18080
  ✓ 컨테이너 healthy: http://127.0.0.1:18080
```

**data-only 인 경우** — DokuWiki stable tarball 자동 다운로드 + 플러그인 자동 설치:
```
[...] 감지: data-only — DokuWiki core 자동 다운로드 + 데이터 overlay + 플러그인 자동 설치.
[...]   DokuWiki core 다운로드: https://download.dokuwiki.org/src/dokuwiki/dokuwiki-stable.tgz
[...]   core 압축 풀기 → /tmp/dwc_test_dokuwiki/dwdata
[...]   데이터 overlay: pages (12345 항목)
[...]   데이터 overlay: media (8901 항목)
[...]   conf overlay: /Users/you/wiki-data/conf
[...] 감지된 플러그인 23: acl, authad, authldap, blog, config, discussion, ...
[...]   already (11): acl, info, config, extension, ...
[...]   installed (4): wrap, struct, todo, discussion
[...]   bundled (8): authad, authldap, ...
[...]   unknown (0):
  ✓ 컨테이너 healthy: http://127.0.0.1:18080
```

### A.4 discover → render → plugin-audit

```
[3/14] 페이지 인벤토리 (state.db)
  진행/skip/quit ? [Enter/s/q]
[...] discover: 1675 pages found
  ✓ 1675 페이지 발견

[4/14] DokuWiki XHTML 렌더 캐시
  진행/skip/quit ? [Enter/s/q]
[...] render 시작 (1675 페이지, base http://127.0.0.1:18080)
[...]   ... 100/1675
[...]   ... 200/1675
... (수분~수십분)
  ✓ 1675 페이지 XHTML 캐시 완료

[5/14] 잔존 매크로 점검 + 플러그인 설치 권장
  진행/skip/quit ? [Enter/s/q]
  1675 개 파일 점검
  잔존 매크로 (상위 10):
    ~~DISCUSSION~~ × 47
    ~~INFO~~ × 12
  플러그인을 추가 설치하려면:
    1) http://127.0.0.1:18080/doku.php?do=admin&page=extension 접속
    2) 필요한 플러그인 설치/활성화
    3) 본 wizard 를 다시 실행해 render 단계 reset
  ? render 결과가 만족스럽나요? (no 면 step render reset 후 종료) [Y/n]:
```

**여기서 `n` 입력하면** — render 가 `pending` 으로 reset 되고 wizard 종료. 사용자가 admin UI 에서 플러그인 설치 → 다시 `python run.py wizard` → render 부터 재실행.

**`y` 입력하면** — 잔존 매크로는 변환기가 strip 처리, 계속 진행:

```
  ✓ 잔존 매크로 59 (사용자 OK)
```

### A.5 convert → upload → rewrite-links

```
[6/14] XHTML → Confluence storage 변환
[...] convert 시작 (1675 페이지)
  ✓ 1675 페이지 storage XML 생성

[7/14] 페이지 + 첨부 업로드
[...] upload 시작 (1675 페이지 + ~10000 첨부)
[...]   ... uploaded 100/1675
... (30분~수시간, 페이지 + 첨부 크기에 따라)
  ✓ 1675 페이지 + 10725 첨부 업로드

[8/14] 내부 링크 2-pass 해소
[...] rewrite-links 시작
  ✓ 링크 해소 5180
```

### A.6 history — 옵션 단계

수시간 소요. 처음엔 skip 하고 후속 작업 후 별도로 돌리는 것 권장.

```
[9/14] 과거 리비전 이전 (옵션)
  (선택) 이 단계는 건너뛸 수 있습니다.
  진행/skip/quit ? [Enter/s/q]
  ? 과거 리비전(history) 도 이전할까요? (~30분-수시간) [Y/n]: n
  ✓ 사용자 skip
```

`y` 면 history-discover/render/convert/upload 4가지가 직렬 실행 — 본
인스턴스 기준 37k 리비전 × 5라운드 = 약 하룻밤.

### A.7 struct — 옵션 단계

`meta/struct.sqlite3` 자동 감지:

```
[10/14] struct 플러그인 데이터 이전 (옵션)
  진행/skip/quit ? [Enter/s/q]
  ? struct 플러그인 데이터를 이전할까요? [Y/n]: y
[...] → struct-discover
[...] struct-discover 완료: schemas=4, columns=39, rows=1213, refs=488
[...] → struct-convert --mode native --reconvert
[...] → struct-upload --mode native
[...]   [NATIVE] Database 쉘 생성 → id=2520357005
... (~25분 @ 50 row/min)
[...] → struct-embed-on-bound-pages
[...] 대상 bound page: 208개
[...] struct-embed 완료: pushed=208 failed=0 unresolved=5
  ✓ 1213 struct row 업로드
```

`meta/struct.sqlite3` 가 없으면:
```
  ✓ struct 플러그인 데이터 없음 (skip)
```

### A.8 audit → verify → report → report-publish

```
[11/14] Confluence 측 본문 검증 (sample)
[...] audit --sample 50
  ✓ sample=50, rc=0 — 상세 stdout

[12/14] 사용자 시각 검수 큐 빌드 + 사람 검수
[...] verify build --sample 100 --with-confluence-view --with-attachment-check
[...] verify 갤러리 → /Users/you/.../verify-gallery.html
  HTML 큐 → /Users/you/.../verify-gallery.html
  브라우저에서 카드를 OK/NG/DEFER 분류 후 'JSON 다운로드' →
  python run.py verify import <파일>
  ? 검수 완료했나요? (no 면 step 그대로 둠) [Y/n]:
```

사용자가 브라우저에서 사람 검수 끝낸 후 `y` 누르면 done. 안 끝났으면
`n` 입력 → step 그대로 두고 wizard 종료 → 검수 후 wizard 재실행.

```
[13/14] 결과 리포트 생성 (stdout)
[...] (corpus 통계 출력)
  ✓ report 출력됨 (stdout)

[14/14] 결과 보고서를 Confluence 페이지로 발행
[...] 보고서 신규 발행 → page 2522513553
  ✓ 보고서 신규 발행 → page 2522513553

== 모든 단계 완료 ==
step               status       요약                               시작
--------------------------------------------------------------------------------
✓ prereq           done         env vars OK (6개) docker=True ...     2026-05-20T...
✓ dev-up           done         컨테이너 healthy                       ...
✓ discover         done         1675 페이지 발견                      ...
... (전부 ✓)
```

---

## 시나리오 B — Ctrl+C 로 중단 후 이어서

긴 단계 (render / history-upload / struct-upload) 진행 중 `Ctrl+C`:

```
[7/14] 페이지 + 첨부 업로드
[...] upload 시작 (1675 페이지 + ~10000 첨부)
[...]   ... uploaded 100/1675
[...]   ... uploaded 200/1675
^C
중단됨. 다음 실행 시 이 단계부터 이어집니다.
$
```

state.db 의 `wizard_state.status` 가 `interrupted`. 다음 실행:

```sh
$ python run.py wizard --status
step               status       요약                               시작
--------------------------------------------------------------------------------
✓ prereq           done         ...
✓ dev-up           done         ...
✓ discover         done         ...
✓ render           done         ...
✓ plugin-audit     done         ...
✓ convert          done         ...
· upload           interrupted                                       2026-05-20T...
· rewrite-links    pending
...
```

`upload` 가 `interrupted` 로 떠있음. 이전 done 단계들은 그대로:

```sh
$ python run.py wizard
== DokuWiki → Confluence 마이그레이션 wizard ==
... (진행 표)

[1/14] 사전 점검 — done (skip)
[2/14] DokuWiki 컨테이너 기동 — done (skip)
[3/14] 페이지 인벤토리 — done (skip)
[4/14] DokuWiki XHTML 렌더 캐시 — done (skip)
[5/14] 잔존 매크로 점검 — done (skip)
[6/14] XHTML → Confluence storage 변환 — done (skip)

[7/14] 페이지 + 첨부 업로드
  step: upload | 현재: interrupted
  진행/skip/quit ? [Enter/s/q]
```

`Enter` 누르면 upload 재시작 — `cmd_upload` 가 자체적으로 idempotent
(이미 업로드된 페이지는 `content_hash` 비교로 PUT skip) 이라 중복
업로드 안 됨. 중단된 곳 이후 페이지만 실제로 PUT.

---

## 시나리오 C — 단계 실패 후 복구

예: 자격증명 만료로 upload 실패 →

```
[7/14] 페이지 + 첨부 업로드
[...] 자격증명/설정 누락 — 누락: CONFLUENCE_API_TOKEN
[...]   토큰: https://id.atlassian.com/manage-profile/security/api-tokens
  ✗ 실패: upload failed: rc=2
종료. 원인 해결 후 다시 실행하면 이 단계부터 이어집니다.
  팁: `python run.py wizard --from-step upload` 로 특정 단계만 재시도
$
```

원인 해결 후 (env 다시 set, 또는 토큰 새로 발급):

```sh
set -a; source .secrets/confluence.env; set +a   # 새 토큰 로드
python run.py wizard
# 자동으로 upload 부터 다시
```

또는 명시적으로:

```sh
python run.py wizard --from-step upload
```

---

## 시나리오 D — 부분 재실행 (특정 단계만 재실행)

audit 결과를 보고 변환기를 고친 다음, 재변환 + 재업로드만 하고 싶을 때:

```sh
# 변환기 고침
$EDITOR run.py

# convert / upload / rewrite-links / audit / report-publish 만 다시
python run.py wizard --from-step convert
```

`--from-step` 은 그 step 부터 끝까지의 상태를 `pending` 으로 reset 하고
실행. 이전 단계 (discover/render/...) 는 그대로 done 상태 유지 — 영향
없음.

---

## 시나리오 E — 비대화 / 자동화 (CI / cron)

```sh
python run.py wizard --yes --continue-on-error \
    --audit-sample 100 --verify-sample 50 \
    --dokuwiki-base http://127.0.0.1:18080 \
    --report-title "Nightly migration sync 2026-05-20"
```

`--yes` — 모든 프롬프트 auto-yes. `--continue-on-error` — 단계 실패해도
다음으로. cron 으로 매시간 호출해도 한 번 done 된 단계는 자동 skip 이라
incremental 작업만 수행.

주의: `--yes` 와 `--continue-on-error` 조합은 prereq 가 실패해도 계속
진행 — env 미설정 환경에서 사용 금지. 환경 검증 후에만 사용.

---

## 시나리오 F — 처음부터 다시

토큰 / 공간 / 데이터를 새로 받고 처음부터 깨끗하게:

```sh
# 1) 보존하고 싶지 않은 산출물 정리
rm -rf raw/ raw_history/ storage/ storage_history/ storage_struct/ logs/
rm state.db state.db-wal state.db-shm

# 2) 컨테이너 종료 + 클론 삭제
python run.py dev down --purge

# 3) wizard reset
python run.py wizard --restart

# 4) 새 진행
python run.py wizard
```

`--restart` 는 *wizard_state 만* reset. raw/storage 등은 별도. 산출물
삭제 전에 `git`/`p4` 로 백업 권장.

---

## 시나리오 G — 도중 dev 컨테이너 재기동

render 단계 진행 중 컨테이너가 죽었거나, 플러그인을 추가했을 때:

```sh
# 컨테이너 재기동 (클론은 유지)
python run.py dev down
python run.py dev up

# 또는 누락 플러그인 추가만 (재기동 없이)
python run.py dev install-plugins

# render 부터 다시
python run.py wizard --from-step render
```

추가 설치된 플러그인 덕분에 더 많은 매크로가 정상 렌더 → plugin-audit
의 `~~MACRO~~` 잔존 카운트가 줄어듦.

---

## 시나리오 H — 보고서만 갱신

이미 마이그레이션이 끝났고 새 정보 (history 추가 라운드 등) 가 있을 때
보고서만 다시 발행:

```sh
# wizard 안 거치고 단독 호출
python run.py report-publish

# 또는 wizard 의 마지막 단계만
python run.py wizard --from-step report-publish --yes
```

보고서 페이지 id 는 `meta.wizard_report_page_id` 에 저장됨 → PUT (멱등).

---

## 단계별 출력 형식 요약

| 출력 | 의미 |
|------|------|
| `[N/14] <title>` | 현재 단계 진입 |
| `  step: <key> \| 현재: <status>` | 단계 메타 (key 는 `--from-step` 인자) |
| `  (선택) ...` | 옵션 단계 (skip 가능) |
| `  진행/skip/quit ? [Enter/s/q]` | 사용자 선택 — Enter=실행, s=skip, q=종료, d=수동 done |
| `  ? <질문> [Y/n]:` | 단계 내부 추가 confirm |
| `[...] <log>` | 단계 안의 cmd_* 가 출력하는 표준 로그 |
| `  ✓ <summary>` | 단계 성공 (summary 가 `wizard_state.summary` 에 저장) |
| `  ✗ 실패: <error>` | 단계 실패 (error 가 `wizard_state.error` 에 저장) |
| `중단됨. 다음 실행 시 이 단계부터 이어집니다.` | Ctrl+C |
| `== 모든 단계 완료 ==` | 14 단계 전부 done/skipped |

---

## 상태 전이 모델

```
                                ┌──> done
                                │
pending ─[run]─> running ───────┤
   ▲                            ├──> failed ──[fix + re-run]──> running
   │                            │
   │     [user 'r']             ├──> interrupted ──[re-run]──> running
   │                            │
   └─[user 's']──> skipped      └──> done (--yes 모드)
```

`wizard_state` 테이블의 컬럼:
- `step_key` (PK)
- `status` — pending / running / done / skipped / failed / interrupted
- `started_at` — running 진입 시각 (자동)
- `finished_at` — done/failed/skipped 시각 (자동)
- `summary` — done 의 한 줄 요약 (보고서에 그대로 표시됨)
- `error` — failed/interrupted 의 사유

---

## 자주 묻는 질문

**Q. 단계 도중에 `q` (quit) 누르면 진행 상태는?**
A. 그 단계는 `pending` 그대로 — 다음 실행에서 같은 단계 프롬프트가 다시
   뜸. 입력 했던 부분 작업이 있다면 (예: upload 50 페이지 했다가 q) 그
   부분은 state.db 에 보존됨 — 단계 재실행 시 이어 처리.

**Q. 단계 도중에 `d` (done 수동) 누르면?**
A. 그 단계는 즉시 `done` 으로 마크. 실제 작업은 안 함 — 외부 도구로
   이미 처리했거나 무시하고 싶을 때.

**Q. `--from-step` 과 `--restart` 차이?**
A. `--restart` — 모든 단계 reset (처음부터 전체 재실행).
   `--from-step X` — X 부터 끝까지만 reset (이전 단계 done 유지).

**Q. plugin-audit 에서 render 를 reset 했는데 다른 단계도 영향 받나?**
A. render 만 pending 으로 돌려놓음. convert / upload 등 이미 done 된
   단계는 그대로지만, render 가 새 데이터로 다시 돌면 raw 가 갱신 →
   convert 가 *기존 storage 와 hash 다른 페이지만* 재변환 (`convert` 의
   내부 idempotent) → upload 도 변경된 페이지만 재 PUT. 즉 chain 이
   자연스럽게 재동작.

**Q. 컨테이너가 죽으면 render 단계가 실패하는데 자동 재기동?**
A. 자동 안 됨. `python run.py dev up` 으로 재기동 후 `python run.py
   wizard --from-step render` 로 이어 진행.

**Q. wizard 의 단계 함수 (`_wiz_*`) 가 호출하는 cmd_* 함수는 그 자체로
실행해도 동일?**
A. 동일. wizard 는 thin wrapper — 각 `_wiz_*` 가 `cmd_*` 를 직접
   호출하고 state.db 의 카운트를 읽어 summary 만 만듦. 디버깅 시 wizard
   대신 cmd_* 를 직접 호출해도 결과 동일.

---

## 더 읽기

- [`runbook.md §0`](runbook.md) — wizard 명령행 옵션 + 14 단계 표
- [`AGENT.md`](../AGENT.md) — 파이프라인 (S1-S7) + 운영 관례
- [`DEPLOY.md`](../DEPLOY.md) — 받는 머신의 첫 설치
- [`scenarios.md`](scenarios.md) — 메인 시나리오 + 새 엣지 케이스 절차
- [`migration-result.md`](migration-result.md) — 작성자 인스턴스의 Day 1-4 라이브 결과
