# AI 에이전트 진입점 (AGENT.md)

이 파일은 AI 코딩 어시스턴트가 본 저장소에 진입할 때 *제일 먼저* 읽는
요약 + 길잡이다. 사람이 처음 진입할 때도 동일하게 유용.

본 저장소는 **자체 호스팅 DokuWiki → Confluence Cloud** 일회성/반복
마이그레이션 도구. 본 인스턴스의 라이브 마이그레이션은 2026-05-19 시점
**Day 1-4 모두 완료** 상태. 코드는 다른 인스턴스로도 재사용 가능.

---

## 1 분 빠른 안내

```sh
# 인자 없이 실행하면 도움말. 처음이라면:
python run.py wizard           # 대화형 14 단계 진행 (중단/재개 안전)
python run.py wizard --status  # 진행 상황만 출력

# DokuWiki 컨테이너 (full install 또는 data-only 자동 분기)
python run.py dev up           # DOKUWIKI_SRC 또는 기본 경로
python run.py dev down

# 결과 보고서를 Confluence 페이지로 발행/갱신
python run.py report-publish
```

자세한 절차는 [`docs/runbook.md`](docs/runbook.md). 환경 변수 + 자격증명
설정은 §0 / §1.

---

## 핵심 파일 한눈에

| 파일 | 역할 |
|------|------|
| `run.py` | 단일 진입 스크립트. 모든 서브커맨드 (~7500 line) |
| `state.db` | SQLite — pages/attachments/revisions/struct_*/wizard_state/verify_decisions/meta. P4 추적. |
| `docs/MEMORY.md` | 세션 간 지속 메모리. *코드만 봐선 안 보이는* 결정/주의사항 위주 |
| `docs/scenarios.md` | S1~S10 설계 + 새 엣지 케이스 발견 절차 (§7.2) |
| `docs/runbook.md` | 라이브 마이그레이션 단계별 절차 |
| `docs/element-mapping.md` | DokuWiki 요소 → Confluence 변환 매트릭스 |
| `docs/plugin-validation.md` | 플러그인별 동작 검증 결과 |
| `docs/visual-audit.md` | 사용자 시각 검수 자동화 (Phase 1-3) |
| `docs/migration-result.md` | 날짜별 라이브 결과 누적 로그 (Day 1-4) |
| `docs/struct-migration.md` | struct 플러그인 데이터 이전 시나리오 + 라이브 결과 |
| `docs/history-migration.md` | 과거 리비전 이전 시나리오 + 라이브 결과 |
| `docs/oversized-attachments.md` / `oversized-pages.md` | 본문/첨부 한도 초과 폴백 |
| `dev/dokuwiki-local/docker-compose.yml` | 로컬 DokuWiki 테스트 컨테이너 |
| `tests/test_*.py` | pytest 회귀 (현재 84 통과) |
| `.secrets/confluence.env` | 자격증명 (커밋 금지) |

---

## 파이프라인 (S1-S7 + 별도 트랙)

```
discover  →  render  →  convert  →  upload  →  rewrite-links
                                       ↓             ↓
                                    history-*    struct-*
                                       ↓             ↓
                                    audit  →   verify  →  report  →  report-publish
```

| 단계 | 명령 | 역할 |
|------|------|------|
| S1 | `discover` | `pages/*.txt` 트리 인벤토리 |
| S2 | `render` | DokuWiki `?do=export_xhtmlbody` 캐시 (`raw/`) |
| S3 | `convert` | bs4 로 storage XML (`storage/`) — bs4 placeholder 로 내부 링크는 `dwc-link:<id>` |
| S4-6 | `upload` | Confluence v2 페이지/첨부 생성/갱신. content_hash 비교로 변경 시만 PUT |
| S7 | `rewrite-links` | placeholder → `<ac:link><ri:page>` 2-pass |
| | `history-*` | attic + meta/.changes 시간순 PUT replay |
| | `struct-*` | meta/struct.sqlite3 → native (Database 쉘 + Page Properties + Report + bound 페이지 임베드) |
| | `rewrite-oversized*` | 100MB 초과 첨부 / 본문 한도 거부 폴백 |
| | `audit` | Confluence 측 다시 받아 비교 |
| | `verify build/import/status` | 사용자 시각 검수 큐 + Phase 3 자동 사전 거름 |
| | `report` / `report-publish` | 통계 / Confluence 페이지 자동 발행 |

전체를 한 명령으로: `python run.py wizard` (14 단계 step-by-step).

---

## 운영 관례 (반드시 지킬 것)

### 자격증명
- 절대 코드/문서에 하드코딩 금지
- `.secrets/confluence.env` 가 표준 — `git` / `p4` 추적 제외
- env vars 표준 이름: `CONFLUENCE_EMAIL` / `CONFLUENCE_API_TOKEN` /
  `CONFLUENCE_SPACE_KEY` / `CONFLUENCE_ROOT_PAGE_ID` / `CONFLUENCE_BASE_URL`,
  `DOKUWIKI_SRC` / `DOKUWIKI_BASE_URL`, `ANTHROPIC_API_KEY`

### Perforce 정책 (2026-05-17 확정)
- **디폴트 체인지리스트 금지**. 항상 번호 매긴 CL + 상세 description
- 제출 후 GitHub 미러 (`neoocean/dokuwiki-to-confluence-cloud`) 푸시
- 깃 커밋 본문 끝에 `P4 CL <N>` 한 줄 (디포 경로 prefix 없이)

```sh
# 1) 변경된 파일 명시 add/edit (reconcile 금지 — 잡쓰레기 잡힘)
p4 --field "Description=...상세 설명..." change -o | p4 change -i  # CL 번호 받음
p4 edit -c <CL> file1 file2
p4 add  -c <CL> new_file
p4 submit -c <CL>

# 2) GitHub 미러
git add file1 file2 new_file
git commit -m "..."   # 본문 끝에 'P4 CL <N>'
git push
```

### Runtime artifacts 추적 제외
`.gitignore` / `.p4ignore`:
- `state.db`, `state.db-wal`, `state.db-shm`
- `raw/`, `raw_history/`, `storage/`, `storage_history/`, `storage_struct/`
- `logs/`, `.venv/`, `.secrets/`
- 글로벌 P4IGNORE 가 `__pycache__` / `*.pyc` / `.DS_Store` / `.claude/` / `venv` 만 잡으니 `.venv/` 는 별도로 잡혀야 함 — 커밋 직전 `p4 status` 확인

### 새 엣지 케이스 발견 절차
1. `grep -lr <pattern> storage --include='*.xml'` 로 영향 페이지 수 측정
2. 원인 진단
3. `_convert_html_to_storage` 에 룰 추가 (또는 해당 헬퍼)
4. re-convert `--force` → 다시 grep 으로 0 확인
5. `docs/scenarios.md §7.2` 표에 행 추가
6. CL 분리 제출 (변환기 fix 1 + 문서 1)

---

## 본 인스턴스 상태 (참고용, 다른 인스턴스 적용 시 무시)

| 트랙 | 상태 | 통계 |
|------|------|------|
| 메인 페이지 | ✅ 100% | 1,675 / 1,675 + 10,725 첨부 |
| 내부 링크 | ✅ resolved | 5,180 / unresolved 1,317 |
| OVERSIZED 첨부 (B mode) | ✅ | 9건 → note 매크로 |
| 본문 한도 거부 페이지 (C mode) | ✅ | 1건 → skeleton + 119 자식 첨부 |
| struct (native, Day 4) | ✅ | 4 Database + 4 인덱스 + 1,213 row + 208 bound 임베드 |
| history (B+A) | ⚠️ 영구 제약 | 18,729 / 37,279 (50%, 큰 페이지 후반 rev 거부) |
| 공간 정리 | ✅ | 1,465 비-마이그레이션 페이지 휴지통 |
| 결과 보고서 | ✅ | page id 2522513553 |

자세한 통계 + 날짜별 로그는 [`docs/migration-result.md`](docs/migration-result.md).

---

## 주의 (이전 시행착오)

다음은 *코드만 보면 모르는* 결정 / 함정 — 손대기 전에 [`docs/MEMORY.md`](docs/MEMORY.md)
의 해당 절을 먼저 읽을 것:

- **컨테이너**: APFS clonefile 필수 / ACL bypass 패치 / PHP warning 억제
  필수 / bitnami 이미지 사용 불가 ([MEMORY.md §dev 환경](docs/MEMORY.md))
- **DokuWiki 출력 함정**: ACL 거부 시 풀 HTML 응답 / fetch.php proxy
  외부 이미지 / dynamic plugin class / wrap callout 매핑 / todo 두 모드
- **Confluence Database API 한계** (Day 4 발견): create/get/delete 만
  공개. 컬럼/row endpoint 없음. storage 측 ri:database 임베드 거부.
  → 데이터는 Page Properties 매크로 조합으로, Database 는 쉘만.
- **본문 한도**: 큰 일지 페이지의 history 후반 rev 가 본문 한도 초과해
  영구 거부. *서버 측 정확한 한도 미공개* — 50% 도달 후 추가 라운드 무의미.
- **`<!-- HTML 코멘트 -->` 는 Confluence storage 정규화 시 strip**.
  Idempotent 마커로 쓸 수 없음 → struct-embed 는 `<h2>관련 struct 데이터</h2>`
  를 sentinel 로 사용.

---

## 작업 시 체크리스트

새 변경 PR 전:
- [ ] `pytest tests/` 통과
- [ ] 라이브 영향 명령 (`upload`/`rewrite-links`/`history-upload`/
      `struct-upload`/`struct-embed-on-bound-pages`/`rewrite-oversized*`)
      변경했으면 `--dry-run` 또는 `--limit N` 으로 소규모 라이브 검증
- [ ] 새 엣지 케이스면 `docs/scenarios.md §7.2` 표 갱신
- [ ] 새 명령이면 `docs/runbook.md` 갱신
- [ ] state.db 스키마 변경이면 `db_init` 의 `CREATE TABLE IF NOT EXISTS` +
      `ALTER TABLE` 안전 migration 추가
- [ ] `.secrets/` / `state.db` / `raw*/` / `storage*/` 가 staging 에
      잡혔는지 확인 (`p4 status` / `git status`)

새 자동화 / 도구 추가 시:
- [ ] 멱등성 — 재실행해도 안전한가?
- [ ] 중단 후 재개 가능한가? (긴 작업이면 `wizard_state` 또는 자체 status 컬럼)
- [ ] 자격증명 누락 시 명확한 에러 메시지 + `CREDENTIAL_HELP` 출력

---

## 더 읽을 거리

- 본 프로젝트의 *왜* / *어떻게* / *함정*: [`docs/MEMORY.md`](docs/MEMORY.md)
- 단계별 라이브 절차: [`docs/runbook.md`](docs/runbook.md)
- 설계 + 새 엣지 케이스 발견 절차: [`docs/scenarios.md`](docs/scenarios.md)
- 라이브 결과 (Day 1-4): [`docs/migration-result.md`](docs/migration-result.md)
- 변환 매트릭스 (요소별): [`docs/element-mapping.md`](docs/element-mapping.md)
- 플러그인 동작: [`docs/plugin-validation.md`](docs/plugin-validation.md)
- 시각 검수 자동화: [`docs/visual-audit.md`](docs/visual-audit.md)
- struct 이전: [`docs/struct-migration.md`](docs/struct-migration.md)
- history 이전: [`docs/history-migration.md`](docs/history-migration.md)

본 파일을 갱신할 때는 README.md / docs/MEMORY.md / docs/runbook.md 의
대응 절도 같이 봐서 일치시킬 것.
