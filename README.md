# dokuwiki-to-confluence-cloud

자체 운영 중인 DokuWiki(`bitnami/dokuwiki`)의 **DokuWiki 가 렌더링한 최종 상태**를
Confluence Cloud로 이전하기 위한 파이썬 스크립트.

DokuWiki 의 `?do=export_xhtmlbody` 출력을 받아 Confluence storage format 으로 변환,
네임스페이스 트리를 그대로 페이지 계층에 매핑하고 미디어를 첨부로 업로드한다.

자세한 설계와 단계별 시나리오는 [`docs/scenarios.md`](docs/scenarios.md) 참고.

## 상태

설계 단계. `docs/scenarios.md` 에 기술된 시나리오를 차례로 구현 예정.
