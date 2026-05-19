# CLAUDE.md

이 파일은 Claude Code (또는 다른 Claude 기반 도구) 가 본 저장소 진입 시
자동 로드하는 컨텍스트다. *내용*은 [AGENT.md](AGENT.md) 가 단일 원본 —
여기서는 핵심 규칙만 반복.

## 본 저장소 한 줄

자체 호스팅 DokuWiki → Confluence Cloud (`$CONFLUENCE_BASE_URL`) 일회성/
반복 마이그레이션 도구. 원 작성자 인스턴스의 라이브는 **Day 1-4 완료**
(2026-05-19). 다른 인스턴스 배포는 [DEPLOY.md](DEPLOY.md).

## 가장 먼저 할 것

1. [AGENT.md](AGENT.md) 의 *운영 관례* 섹션을 읽을 것 (Perforce 정책,
   자격증명 취급, runtime artifact 제외).
2. [docs/MEMORY.md](docs/MEMORY.md) 의 *주의* 절 — 코드만 봐선 안 보이는
   결정/함정 (APFS clonefile / ACL bypass / Confluence Database API 한도
   / 본문 한도 / `<!--HTML 코멘트-->` strip 등).

## 운영 관례 (반드시 지킬 것)

| 항목 | 규칙 |
|------|------|
| Perforce | 디폴트 CL 금지. 항상 번호 매긴 CL + 상세 description. 제출 후 GitHub 미러. 깃 커밋 본문 끝에 `P4 CL <N>` 한 줄. |
| 자격증명 | 절대 코드/문서 하드코딩 금지. `.secrets/confluence.env` 표준. |
| Runtime artifacts | `state.db` / `raw*/` / `storage*/` / `logs/` / `.venv/` / `.secrets/` 추적 제외. 커밋 직전 `p4 status` 확인. |
| 새 엣지 케이스 | grep 으로 영향 측정 → 변환기 fix → re-convert `--force` → grep 0 확인 → `docs/scenarios.md §7.2` 표 갱신. |
| 라이브 영향 명령 | `--dry-run` 또는 `--limit N` 으로 먼저 검증. |
| 사용자에 보고 | 한국어로. 짧고 구체적으로. |

## 자주 쓰는 명령

```sh
python run.py                  # 도움말 + exit 0
python run.py wizard           # 대화형 14 단계 (중단/재개 안전)
python run.py wizard --status  # 진행 표
python run.py status           # state.db 카운트 요약
python run.py dev up           # DokuWiki 컨테이너 (full / data-only 자동 분기)
python run.py audit --sample 50    # Confluence 측 본문 검증
python run.py verify build         # 시각 검수 큐
python run.py report-publish       # 결과 보고서 Confluence 페이지 발행
pytest tests/                  # 회귀 (현재 84 통과)
```

## 통신

사용자는 한국어 답변을 기대합니다. 영어 단어 (Confluence/REST API 명칭 등)
는 그대로. 코드/문법 설명도 한국어로 부드럽게 흐르도록.

## 더 읽기

- [AGENT.md](AGENT.md) — 본 컨텍스트의 *전체* 진입점 (사람/AI 공용)
- [docs/MEMORY.md](docs/MEMORY.md) — 세션 간 지속 메모리
- [docs/runbook.md](docs/runbook.md) — 라이브 단계별 절차
- [README.md](README.md) — 일반 소개
