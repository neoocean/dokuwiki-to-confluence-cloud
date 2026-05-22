# tests/ — pytest 회귀 매핑

현재 **190 통과**. 각 파일별로 무엇을 보호하는지 한눈에. 회귀 작성·확장 시
*어느 파일에 추가할까* 결정 가이드.

## 영역별 매핑

| 파일 | 테스트 수 | 영역 | run.py 코드 |
|------|----------|------|-------------|
| `test_convert.py` | 28 | 변환기 회귀 — chrome strip / 이미지 / 표 / 코드 / 인용 / smiley / wrap callout / 등 | `_convert_html_to_storage` 와 그 하위 변환기 (§ S3 Convert) |
| `test_audit_3way.py` | 22 | source ↔ rendered ↔ confluence 3-측 invariant audit. 신호 S1/S2/D1/D2/D3 + INTENDED_TRANSFORMATIONS 화이트리스트 | `cmd_audit_3way / _audit_3way_*` (§ audit-3way) |
| `test_revision_header.py` | 21 | revision 헤더 형식 (8종) + 기존 헤더 strip 회귀 | `_revision_header` (§ history-* track) |
| `test_visual_signals.py` | 21 | Phase 4 추가 신호 7개 (pixel-diff / tile-phash / element / OCR / bbox-LCS / storage-AST / color-hist) | `_vc_*` 함수 (§ verify) |
| `test_struct.py` | 19 | struct cell 렌더링 + helper | `_struct_*` (§ struct-* track) |
| `test_verify.py` | 19 | verify 서브커맨드 (시각 검수 큐) | `_verify_*` (§ verify) |
| `test_calendar.py` | 18 | monthcal fallback + Google Calendar iframe + encryptedpasswords (5종) | `_convert_monthcal_fallback / _convert_google_calendar_iframe / _convert_encrypted_passwords / _preprocess_encrypted_passwords` |
| `test_decrypt.py` | 10 | encryptedpasswords cipher 복호화 round-trip + KDF + 잘못된 password | `decrypt_encryptedpasswords / _evp_bytes_to_key` (§ decrypt / link-check) |
| `test_dev_plugins.py` | 9 | dev 컨테이너 plugin 자동 감지/설치 + sanity check (`.htaccess` / NFC / ACL bypass / `_dev_*`) | `_dev_detect_plugins / _dev_install_plugins / _dev_ensure_htaccess / _dev_normalize_filenames_to_nfc` (§ dev) |
| `test_plugin_scan.py` | 8 | 페이지 본문 → 미설치 플러그인 식별 | `_scan_plugin_usage / cmd_plugin_scan` (§ dev) |
| `test_visual_signals_phase3.py` | 8 | Phase 3 자동 신호 (sentence/artifact/code/heading/link) | `_sentence_align / _compare_artifacts / _compare_code_blocks / _compare_heading_seq / _link_resolution_rate` (§ verify) |
| `test_link_check.py` | 4 | link-check (placeholder 잔존 + unresolved title + 외부 URL) 정규식 | `cmd_link_check` (§ decrypt / link-check) |
| `test_wizard.py` | 3 | wizard 상태 전이 + WIZARD_STEPS + report body | `_wizard_* / WIZARD_STEPS / _wizard_build_report_body` (§ wizard) |

## 공통

- `conftest.py` — pytest fixture 공유 (`project_root`, `convert`,
  `make_dokuwiki`).
- 새 변환기/명령 추가 시 위 표의 매핑 따라 *영역별 파일에 추가*. 새
  영역이면 `test_<domain>.py` 신설.

## 실행

```sh
.venv/bin/pytest tests/             # 전체
.venv/bin/pytest tests/test_convert.py -q  # 한 파일
.venv/bin/pytest tests/ -k decrypt   # 키워드 매치
```
