# dokuwiki-to-confluence-cloud

자체 운영 중인 DokuWiki(`bitnami/dokuwiki`)의 **DokuWiki 가 렌더링한 최종 상태**를
Confluence Cloud로 이전하기 위한 파이썬 스크립트.

DokuWiki 의 `?do=export_xhtmlbody` 출력을 받아 Confluence storage format 으로 변환,
네임스페이스 트리를 그대로 페이지 계층에 매핑하고 미디어를 첨부로 업로드한다.

자세한 설계와 단계별 시나리오는 [`docs/scenarios.md`](docs/scenarios.md) 참고.

## 상태

구현 진행 중. `docs/scenarios.md` 의 S1~S2 가 동작하고, S3~S7 은 스켈레톤.

## 사용

```sh
pip install -r requirements.txt

# S1: 페이지 트리 발견
python run.py discover --src ~/p4/playground/docker/dokuwiki/data/data

# S2: DokuWiki 가 렌더링한 XHTML 캐시
python run.py render --base-url http://dokuwiki.local

# 상태 요약
python run.py status
```

자격증명은 환경변수로:
- `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN` (업로드용)
- `DOKUWIKI_BASE_URL`, `DOKUWIKI_USER`, `DOKUWIKI_PASSWORD` (렌더링용)
- `DOKUWIKI_SRC` (`--src` 기본값)
