# 100MB 초과 첨부 이전 시나리오

**상태 (2026-05-19): B 모드 라이브 적용 완료.** §4.1 의 note 매크로
박스 패턴으로 9 첨부 (5 페이지 v↑) 처리됨 + 라이브 중 OVERSIZED 1건
추가 발견 (총 10). 구현 + 적용 명령: `python run.py rewrite-oversized`.
`docs/migration-result.md §2.2` 참고.

---



본 문서는 Confluence Cloud 의 첨부 크기 한도(단일 파일 100MB)를 넘는
DokuWiki 미디어를 어떻게 옮길 수 있는지의 옵션과 트레이드오프를
정리한다. 본 인스턴스 측정값은 익명화된 통계만 인용한다 (실제 경로 /
파일명은 노출하지 않음).

## 1. 문제 정의

DokuWiki 의 `media/` 디렉터리에는 텍스트 페이지에서 인용하는 모든
첨부가 존재한다. 마이그레이션 파이프라인의 기본 동작은 *각 페이지에
딸린 첨부를 그 페이지의 Confluence 첨부로 같이 업로드*하는 것이며,
첨부 한 개당 단일 HTTP multipart 호출이다.

Confluence Cloud 의 단일 첨부 한도는 **100MB**. 한도를 넘는 파일은
변환기가 `OVERSIZED` 상태로 표시하고 업로드를 건너뛴다. storage XML
안에는 `<ri:attachment ri:filename=...>` reference 만 남아 *깨진 링크*
처럼 보이게 된다.

본 인스턴스 통계 (corpus 단위로 합산, 익명화):

| 항목 | 값 |
|------|----|
| 총 첨부 후보 | ~10,800 |
| 100MB 초과 (OVERSIZED) | **9건** |
| 가장 큰 파일 크기 | 약 268MB |
| 가장 작은 OVERSIZED 크기 | 약 121MB |
| 총 OVERSIZED 합산 크기 | 약 1.4GB |
| 카테고리 | PDF (개발/일지 문서) 다수, 지도 데이터 1건 |

OVERSIZED 가 corpus 의 0.08% 라 절대 양은 작지만, 본문에 reference 가
남아있는 페이지 사용자 시각에서는 깨진 링크처럼 보인다.

## 2. Confluence 측 제약

- **단일 첨부 100MB**: REST/storage API 양쪽 동일.
- **페이지당 첨부 개수 한도**: 명시 한도 없으나 *수십 건* 권장.
- **첨부 버전**: 같은 파일명으로 다시 업로드하면 *새 버전*. 한도는 동일.
- **외부 링크 허용**: storage 안 `<a href>` / `<img src>` 는 외부 HTTPS
  URL 허용. CORS / mixed-content 제약 없음 (사용자 브라우저에서 직접
  fetch).

## 3. 옵션 매트릭스

| # | 전략 | 보존 | 사용자 경험 | 구현 부담 | 호스트 자원 |
|---|------|------|-------------|-----------|-------------|
| A | 변환 안 함 — broken reference 그대로 둠 | 본문 reference | 깨진 첨부 아이콘 | 0 (현 상태) | 원본 P4 백업 그대로 |
| B | 본문 메타 푸터 + 호스트 백업 안내 | reference + 메타 (크기/체크섬/위치 hint) | "이 파일은 호스트 백업에 보관" 안내 박스 | 낮음 (본문 변환 1단계) | 원본 보존 |
| C | 외부 호스팅 + URL 링크 | 시각적 + 다운로드 동작 | 클릭하면 외부 URL | 중간 (호스팅 셋업 + 변환) | 외부 S3/GDrive 등 |
| D | 분할 압축 + N개 Confluence 첨부 | 시각적 + 다운로드 후 합치기 | 자식 페이지에 part 첨부 N개 + 안내 | 중간 (sibling 패턴 재사용 가능) | 압축 임시 디스크 |
| E | 손실 압축 (PDF 재압축 등) | 시각적 + 직접 첨부 | Confluence 첨부로 직접 | 높음 (도구 의존, 검증) | 변환 시 cpu |
| F | 무시 + 사용자 안내 (사용자가 수동 처리) | 없음 | 깨진 링크 | 0 | - |

## 4. 권장 (B + C 혼합)

**1차 (default)**: B — *본문 푸터 + 호스트 백업 안내*.
**2차 (사용자 결정)**: C — *외부 호스팅 + URL 링크*.

근거:
- 9건뿐이라 수동 처리 부담이 적다.
- 사용자(=호스트 운영자) 본인 자료라 외부 공유 의무 없음.
- 가장 큰 파일은 한 개 268MB — 호스트 P4 백업에 이미 보존됨.
- 분할 압축 (D) 은 *사용자가 받은 뒤 다시 합쳐야 하는* 단점.
- 손실 압축 (E) 은 원본 충실도 손실. 일지 PDF 의 경우 텍스트 검색
  기능을 잃을 수 있다.

### 4.1 B 모드 (본문 메타 푸터)

변환기가 `OVERSIZED` 첨부 reference 발견 시 그 reference 를 다음
구조로 치환:

```xml
<ac:structured-macro ac:name="note">
  <ac:parameter ac:name="title">대용량 첨부 미이전</ac:parameter>
  <ac:rich-text-body>
    <p>이 자리에는 원래 <code><파일명></code> 이 있었습니다.</p>
    <ul>
      <li>크기: 268.4 MB</li>
      <li>SHA-256: <code>…16자리…</code></li>
      <li>원본 위치: 호스트의 DokuWiki 백업 (P4 depot)</li>
      <li>이전되지 않은 이유: Confluence Cloud 단일 첨부 100MB 한도</li>
    </ul>
  </ac:rich-text-body>
</ac:structured-macro>
```

장점: Confluence UI 에서 *사라진 것처럼 안 보임*. 사용자가 어떤 파일이
있었는지 명확히 인지 가능. 후속 처리(외부 호스팅 / 압축 / 분할)도
필요 시 추가 가능.

### 4.2 C 모드 (외부 호스팅 + URL)

대용량 파일을 자체 호스팅 또는 클라우드 스토리지로 옮기고 본문의
`<ac:link><ri:attachment>` 를 외부 URL 의 `<a>` 로 치환.

옵션:
- **Google Drive / OneDrive / iCloud 공유 링크** — 가장 단순, 사용자
  계정에 묶임. 권한 관리 별도.
- **호스트의 정적 HTTP 서버** — 호스트 자체의 nginx/caddy 로 본인 도메인
  하위 `/dwc-attachments/<sha256>/<filename>` 패턴 노출. 가장 통제권 큼.
  Confluence 페이지에서 클릭하면 사용자 브라우저가 호스트로 직접 fetch.
- **S3 (presigned URL)** — 신뢰성/장기 보관에 좋음. 비용 미미. 자동화
  쉬움.

권장: 호스트의 정적 HTTP — 사용자 본인 환경 통제, 추가 비용 0, 기존
Tailscale 네트워크 통해 접근 제한 가능.

## 5. 분할 압축 (D 모드, 보조)

`sibling` 스크립트 (`upload_to_confluence/run.py`) 에 이미 검증된 패턴:

1. 큰 파일을 zip 한 개로 묶음 (`shutil.make_archive`).
2. 그 zip 을 `max_bytes` (예: 95MB) 단위로 split → `<base>.part001.zip`,
   `<base>.part002.zip`, ...
3. 각 part 를 Confluence 첨부로 업로드.
4. 페이지 본문 푸터에 합치는 방법 명시:
   ```
   원본 파일 복원:
     cat <base>.part001.zip <base>.part002.zip ... > <base>.zip
     unzip <base>.zip
   ```
5. state.db 의 `attachments.compressed_parts` 컬럼에 part 파일명 list
   기록 (sibling 스크립트가 이미 사용하는 컬럼).

장점: Confluence 안에 데이터가 자족적으로 남는다 (호스트 의존 0).
단점: 사용자가 받아서 합쳐야 함. 권장하지 않음.

## 6. 손실 압축 (E 모드, 마지막 수단)

PDF 한정 (지도 데이터 등 binary 는 부적합):

- **Ghostscript** 의 PDF 압축: `gs -sDEVICE=pdfwrite -dPDFSETTINGS=/ebook ...`
  로 50-70% 축소. 텍스트 layer 보존. 이미지 해상도 다운.
- **PyMuPDF** (`fitz`) 의 `garbage=4, deflate=True` 옵션: 비손실 압축.
  30% 정도 축소.

자동화 가능하지만 *손실 여부* 사용자가 사후 검증 필요. 본 인스턴스의
9건 중 PDF 가 대부분이라 50% 압축 시 100MB 이하로 들어가는 후보가 5-6건
정도로 추정 — 검증 필요. 가장 큰 268MB 는 압축으로도 100MB 이하 불가능
가능성.

## 7. 구현 스케치 (B 모드 1차 채택 시)

새 서브커맨드 / 기존 cmd_convert 확장 옵션:

```sh
python run.py rewrite-oversized [--mode=note-only|external-url|split|compress]
```

또는 메인 파이프라인의 cmd_convert 에 `--oversized-strategy` 추가하고
변환 단계에서 자동 처리.

### 7.1 state.db 변경

기존 `attachments` 테이블에 추가 컬럼 (옵션 적용 시):

```sql
ALTER TABLE attachments ADD COLUMN replacement_kind TEXT;
    -- 'note' / 'external_url' / 'split_parts' / 'compressed'
ALTER TABLE attachments ADD COLUMN replacement_locator TEXT;
    -- B 모드: NULL (본문에 직접 박힘)
    -- C 모드: 외부 URL
    -- D 모드: zip part 파일명 list (sibling 의 compressed_parts 활용)
    -- E 모드: 압축 후 새 첨부의 confluence_attachment_id
```

### 7.2 변환기 변경 (B 모드 핵심 알고리즘)

1. 메인 파이프라인의 `_convert_html_to_storage` 에서 `<img>` / `<a>`
   처리 시점에는 미디어 src_path 의 크기를 아직 모른다 (변환기는 raw
   HTML 만 본다).
2. *upload* 단계에서 100MB 초과 발견 시 attachment status='OVERSIZED'
   로 기록. 단순.
3. 별도 *post-pass* 서브커맨드 (`rewrite-oversized --mode=note-only`)
   가 OVERSIZED 행을 찾아:
   - 해당 page 의 storage XML 을 다시 읽음
   - `<ri:attachment ri:filename="<X>"/>` 를 §4.1 의 매크로 박스로
     치환
   - storage XML 재저장 + content_hash 갱신
   - page 의 confluence_page_id 가 있으면 자동 PUT (rewrite-links 와
     동일 패턴)

### 7.3 검증

- 9건이 적어 변환 후 사람 눈으로 시각 확인 가능.
- audit 서브커맨드(이미 구현됨) 의 결과에 영향을 안 받음 (텍스트
  비교 기준은 본문이라 매크로 텍스트 차이는 token_overlap 에 미미).

## 8. 결정 항목

| # | 항목 | 상태 |
|---|------|------|
| 1 | 어떤 모드를 채택 | **결정 보류** — 사용자 선택 |
| 2 | C 모드 채택 시 외부 호스팅 위치 | 호스트 자체 정적 HTTP 권장. 결정 보류 |
| 3 | E 모드 적용 시 손실 정도 허용 임계 | 미정. PDF 압축은 일반적으로 50% 권장 |
| 4 | 푸터 박스 안 표시할 메타데이터 항목 | §4.1 의 4개 (파일명, 크기, sha256, 원본 위치) 가 default. 사용자가 가감 가능 |
| 5 | 자동 vs 수동 처리 | 9건이라 수동 처리도 부담 적음. 자동 (B 모드) 이 일관성 우위 |
| 6 | 호스트 P4 백업의 외부 노출 정책 | C 모드 선택 시 결정 필요. 본인 자료라 사용자 선택권 큼 |

## 9. 다음 단계

1. 사용자가 §3 매트릭스 중 한 가지 (또는 조합) 채택.
2. 채택안에 맞춰 `rewrite-oversized` 서브커맨드 또는 cmd_convert 확장
   구현 (B 모드는 ~50줄 추정).
3. 9건에 적용 후 시각 검수.
4. element-mapping.md §C 와 runbook.md §9 에 결과 반영.

본 라이브 마이그레이션이 이미 진행 중이라 OVERSIZED 9건은 *현재
상태*로는 본문에 broken reference 로 남는다. 본 시나리오의 어느 모드든
*마이그레이션 후 추가 패스*로 적용 가능 — 라이브 진행을 중단하거나
되돌릴 필요 없다.
