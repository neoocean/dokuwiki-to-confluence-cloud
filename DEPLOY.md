# 배포 가이드 (DEPLOY.md)

이 문서는 본 도구를 *다른 머신/사용자에게* 배포해 사용하게 할 때의
번들 구성 / 설치 / 첫 실행 절차를 정의한다.

본 도구는 단일 파이썬 스크립트 + state.db 기반이고, OS 별 특수 코드
없이 macOS / Linux 에서 그대로 동작 (Windows 는 WSL2 권장).

---

## 1. 배포 번들에 포함할 것 (deployable set)

| 파일/디렉터리 | 포함 | 비고 |
|---------------|------|------|
| `run.py` | ✅ | 단일 진입 스크립트 |
| `requirements.txt` | ✅ | pip 의존성 |
| `tests/` | ✅ | pytest 회귀 (배포본 검증용) |
| `docs/` | ✅ | 문서 일체 |
| `dev/dokuwiki-local/docker-compose.yml` | ✅ | 로컬 컨테이너 설정 |
| `README.md` | ✅ | 사용법 종합 |
| `AGENT.md` | ✅ | 사람/AI 진입점 |
| `CLAUDE.md` | ✅ | Claude Code 컨텍스트 |
| `DEPLOY.md` | ✅ | 본 문서 |
| `.env.example` | ✅ | 환경 변수 템플릿 |
| `LICENSE` | ✅ | 라이선스 |
| `.gitignore` | ✅ | 추적 제외 패턴 (참고용) |

**제외할 것** (per-instance / 생성물 / 비밀):

| 파일/디렉터리 | 이유 |
|---------------|------|
| `state.db`, `state.db-wal`, `state.db-shm` | 각 인스턴스마다 새로 생성 |
| `raw/`, `raw_history/` | dokuwiki XHTML 캐시 — 첫 render 시 재생성 |
| `storage/`, `storage_history/`, `storage_struct/` | convert 산출물 |
| `logs/` | 런타임 로그 |
| `.venv/` | 가상환경 — 새로 만들어야 함 |
| `.secrets/` | 자격증명 — 절대 배포 금지 |
| `__pycache__/`, `*.pyc`, `.pytest_cache/` | 파이썬 캐시 |
| `.git/`, `.p4/` | 소스 관리 메타 (선택 — git clone 사용 시 자동) |
| `.DS_Store` | macOS Finder 메타 |
| `verify-gallery.html`, `verify-screenshots/` | 시각 검수 산출물 |
| `dev/dokuwiki-local/bitnami/` | 컨테이너 실행 시점에 mount 되는 데이터 |

### 빠르게 번들 만들기

```sh
# 1) git clone 으로 받아준 경우 — 그대로 깨끗
# 2) tar 로 묶어주는 경우 (예: 외부 USB 로 전달)
tar --exclude='.git' --exclude='.p4' --exclude='.venv' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.secrets' --exclude='state.db*' \
    --exclude='raw' --exclude='raw_history' \
    --exclude='storage' --exclude='storage_history' --exclude='storage_struct' \
    --exclude='logs' --exclude='verify-gallery.html' --exclude='verify-screenshots' \
    --exclude='dev/dokuwiki-local/bitnami' \
    -czf dokuwiki-to-confluence-cloud.tar.gz \
    dokuwiki-to-confluence-cloud/
```

또는:

```sh
git archive --format=tar.gz --output=dokuwiki-to-confluence-cloud.tar.gz HEAD
```

`git archive` 가 `.gitignore` 를 존중하지 않으므로 `.gitattributes` 에
`export-ignore` 패턴을 명시하거나 위 `tar --exclude` 방법 사용 권장.

---

## 2. 받는 머신의 요구 사항

| 도구 | 최소 버전 | 용도 | 설치 안내 |
|------|-----------|------|-----------|
| Python | 3.11+ (3.13 권장) | 본 스크립트 | macOS: `brew install python@3.13` / Ubuntu: `apt install python3.13-venv` |
| pip | Python 동봉 | 의존성 설치 | — |
| curl | OS 기본 | data-only bootstrap 의 DokuWiki/플러그인 다운로드 | macOS/Linux 기본 포함 |
| tar | OS 기본 | 동상 | 동상 |
| docker | 최신 | 로컬 DokuWiki 컨테이너 (`dev up`). 라이브 dokuwiki 가 따로 있으면 불필요 | <https://docs.docker.com/get-docker/> |

추가로 옵션 의존성 (모두 pip):

| 의존성 | 트리거 |
|--------|--------|
| `playwright` + chromium | `verify build --with-screenshots` |
| `imagehash` + `pillow` | phash 계산 |
| `anthropic` SDK | `verify build --with-vision` (`ANTHROPIC_API_KEY` 필요) |

---

## 3. 받는 머신의 첫 설치 (5단계, ~3분)

```sh
# 1) 번들 풀기
tar -xzf dokuwiki-to-confluence-cloud.tar.gz
cd dokuwiki-to-confluence-cloud

# 2) 가상환경 + 의존성
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3) 자격증명 (.env.example → .secrets/confluence.env 복사 후 편집)
mkdir -p .secrets
cp .env.example .secrets/confluence.env
$EDITOR .secrets/confluence.env

# 4) 환경 변수 로드 (매 세션마다)
set -a; source .secrets/confluence.env; set +a

# 5) 테스트 + 동작 확인
python -m pytest tests/ -q                  # 190 통과 (~0.3초)
python run.py                                # 도움말 + exit 0
python run.py wizard --status                # 빈 진행 표
```

설정 값 확인:

```sh
python run.py wizard --from-step prereq --yes
# 정상이면: env vars OK + docker/curl/tar available 표시
```

`.env.example` 의 값 형식 (모두 `.secrets/confluence.env` 로 옮긴 뒤
편집):

```ini
CONFLUENCE_EMAIL=you@example.org
CONFLUENCE_API_TOKEN=ATATT3xFf...                 # id.atlassian.com 에서 발급
CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_SPACE_KEY=MIGRATE                      # 공간 설정 → 공간 키
CONFLUENCE_ROOT_PAGE_ID=123456789                 # 빈 페이지 1개 만들어 그 id
DOKUWIKI_SRC=/Users/you/dokuwiki/data             # full install 또는 data-only
DOKUWIKI_BASE_URL=http://127.0.0.1:18080          # dev up 사용 시. 라이브면 그 URL
# 옵션
ANTHROPIC_API_KEY=sk-ant-...                      # verify build --with-vision 사용 시
```

---

## 4. 첫 마이그레이션 한 줄

```sh
python run.py wizard
# 14 단계 차례 — [Enter]/s/q/d 프롬프트
```

자세한 흐름은 [README.md](README.md) 의 `빠른 시작 — wizard 한 줄` +
실제 콘솔 출력 walkthrough 는 [`docs/wizard-walkthrough.md`](docs/wizard-walkthrough.md).

### macOS 사용자: `dev up` 자동 처리 4 단계

`python run.py dev up` 이 호스트 데이터 clone 직후 다음을 자동 수행
(원본 호스트 디렉터리는 손대지 않음):

1. **ACL bypass 패치** — clone 의 `conf/local.php` 에 `$conf['useacl'] = 0` 주입
2. **`.htaccess` 자동 생성** — `userewrite=1` 인 인스턴스의 `/_media/...` URL
   mod_rewrite rules. 부재 시 미디어가 모두 404
3. **플러그인 자동 감지·설치** — `conf/plugins.local.php` / `meta/struct.sqlite3` /
   `~~MACRO~~` 스캔 → release tarball URL 매핑 (`PLUGIN_DOWNLOADS`) 따라 다운로드
4. **한국어 파일명 NFC 정규화** — APFS 가 NFD 로 저장한 한국어 파일명을
   NFC name 으로 추가 cp. 안 하면 컨테이너 PHP 의 `file_exists()` 가
   byte-exact 비교로 404

Linux 호스트는 (3) 만 동작 — (1) 은 필요 시 수동, (2) 는 보통 dist
파일 존재, (4) 는 ext4 가 NFC 저장이라 불필요.

---

## 5. 머신간 이전 (마이그레이션 중간에 다른 머신으로)

`state.db` + `raw/` + `storage/` 를 함께 옮기면 그 머신에서 그대로
이어 진행 가능. 단:

- 모든 경로가 *상대경로* 로 저장되어 있어야 함 — `state.db` 의
  `raw_xhtml_path` / `storage_path` 컬럼이 `raw/foo.xhtml` 처럼
  저장되면 OK. 절대경로면 새 머신에서 재 render 필요.
- `DOKUWIKI_SRC` 만 새 머신에 맞게 env 갱신

옮기는 minimum set:

```
state.db
state.db-wal     # 있으면
state.db-shm     # 있으면
raw/             # 또는 새 머신에서 다시 render
storage/         # 또는 새 머신에서 다시 convert
.secrets/        # 받는 사용자의 자격증명
```

---

## 6. 머신 의존 동작 / 비호환 항목

| 항목 | 동작 | 영향 |
|------|------|------|
| `cp -cR` (APFS clonefile) | macOS APFS 에서만 동작 — 다른 FS 면 자동으로 `cp -R` 로 폴백 (디스크 14GB 정도 실제 소비) | dev up 시 |
| `/tmp/dwc_test_dokuwiki/dwdata` | Linux/macOS 의 `/tmp` 표준 위치. Windows WSL2 에선 그대로 동작. 네이티브 Windows 는 미지원 | dev up 시 |
| Docker Desktop / docker compose v2 | 모든 플랫폼 — Linux 는 docker engine + compose plugin | dev up 시 |
| `requests` library의 자격증명 처리 | 표준 HTTPS — 사내 프록시 환경이면 `HTTP_PROXY` env 추가 |
| Confluence API rate limit | 사내 망 / VPN 우회 환경에서 429 발생 시 `_request_with_retry` 가 자동 backoff (최대 6회) | upload/rewrite 시 |

---

## 7. 새 머신에서 흔히 막히는 곳

### `DOKUWIKI_SRC` 미설정
```
[2026-05-20T...] DokuWiki 데이터 경로 미지정.
[2026-05-20T...]   --src /path/to/dokuwiki/data 또는 DOKUWIKI_SRC env 설정.
```
→ `.secrets/confluence.env` 의 `DOKUWIKI_SRC` 채우고 다시 source.

### `CONFLUENCE_BASE_URL` 누락
```
[2026-05-20T...] 자격증명/설정 누락 — Confluence API 호출 불가. 누락: CONFLUENCE_BASE_URL
```
→ `.env.example` 보고 `https://<your-domain>.atlassian.net/wiki` 형식으로 설정.

### `docker compose` 없음
```
[2026-05-20T...] dev up failed: rc=...
```
→ Docker Desktop 설치 또는 라이브 dokuwiki 가 따로 있으면 `dev-up` 단계 skip.

### Python 버전 너무 낮음
```
TypeError: 'type' object is not subscriptable
```
→ Python 3.11+ 필요. `python --version` 확인.

### pip install 실패 (`lxml`)
macOS 에서 `libxml2` / `libxslt` 없으면 빌드 실패. `brew install libxml2 libxslt` 후 재시도. 또는:
```sh
pip install --only-binary=:all: lxml
```

### Confluence API 401
이메일/토큰 오타. 토큰은 *사용자 토큰* (계정 토큰 아님) — `id.atlassian.com/manage-profile/security/api-tokens` 에서 발급.

### Confluence API 403 (권한 없음)
공간에 페이지 생성 권한 필요. UI 에서 사용자에게 공간 admin 권한 부여 또는 신규 공간 생성.

---

## 8. 보안 체크리스트

배포 전:
- [ ] `.secrets/` 가 번들에 포함 안 됨
- [ ] `state.db` 가 번들에 포함 안 됨 (인스턴스 데이터 노출 위험)
- [ ] 본 저장소의 `docs/MEMORY.md` / `docs/migration-result.md` 등이
      *본 인스턴스의 라이브 결과* 를 담고 있음. 외부에 공개할 때는 본
      인스턴스 페이지 ID / 사용자명 등 식별자 마스킹 권장.

배포 후 (받는 쪽):
- [ ] API 토큰을 환경 변수로만 사용 — 절대 코드/문서 하드코딩 금지
- [ ] 신규 Confluence 공간에 마이그레이션 → 검증 후 정식 위치로 이동
- [ ] 마이그레이션 완료 후 API 토큰 revoke 권장

---

## 9. 다른 인스턴스 적용 시 변경할 것

본 도구는 *일반화된 DokuWiki → Confluence* 마이그레이션 도구지만, 각
인스턴스의 변환기 룰 / 플러그인 / 디렉토리 구조에 따라 약간의 튜닝이
필요할 수 있다. 우선순위 순:

| 항목 | 위치 | 비고 |
|------|------|------|
| 환경변수 | `.secrets/confluence.env` | 전부 필수 |
| `STRUCT_BINDINGS` | `run.py` (struct 데이터 있을 때) | schema 별 binding 컬럼 매핑 — 본 인스턴스는 brevet_* 하드코딩 |
| 플러그인 추가 매핑 | `run.py` `PLUGIN_DOWNLOADS` | 자동 설치 후보 추가 |
| 매크로 → 플러그인 매핑 | `run.py` `MACRO_TO_PLUGIN` | `~~MACRO~~` 패턴별 플러그인 |
| 변환기 룰 | `run.py` `_convert_html_to_storage` 및 helper | 새 엣지 케이스 발견 시 docs/scenarios.md §7.2 절차로 추가 |

본 인스턴스의 라이브 결과 데이터 (`docs/migration-result.md` Day 1-4) 는
참고용 — 다른 인스턴스에서는 본인의 결과로 채워질 것.

---

## 10. 한 줄 요약

```sh
# 받는 머신에서:
tar -xzf dokuwiki-to-confluence-cloud.tar.gz && cd dokuwiki-to-confluence-cloud
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .secrets/confluence.env && $EDITOR .secrets/confluence.env
set -a; source .secrets/confluence.env; set +a
python run.py wizard
```
