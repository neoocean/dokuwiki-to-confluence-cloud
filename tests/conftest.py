"""pytest 공통 fixture + sys.path 설정.

여러 test 파일이 같은 helper (`_convert`, ROOT 경로 등) 를 가지지 않도록
공유. pytest 가 conftest.py 를 자동 로드함.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# run.py 를 sys.path 에 (모든 test 파일에서 `import run` 가능)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def project_root() -> Path:
    """저장소 루트 — _convert_html_to_storage 의 src_root 인자 등에 사용."""
    return ROOT


@pytest.fixture
def convert():
    """DokuWiki HTML → Confluence storage 변환 함수 (테스트용 단축)."""
    import run

    def _do(html: str) -> str:
        storage, _links, _atts, _title, _tags = run._convert_html_to_storage(html, ROOT)
        return storage

    return _do


@pytest.fixture
def make_dokuwiki(tmp_path):
    """tmp_path 에 minimal DokuWiki 데이터 구조 생성하는 factory.

    plugin-scan / dev-up 등 src 디렉터리가 필요한 테스트에서 재사용.
    """
    def _build(pages: dict[str, str], installed: set[str] | None = None) -> Path:
        root = tmp_path / "doku"
        pages_dir = root / "data" / "pages"
        pages_dir.mkdir(parents=True)
        (root / "data" / "media").mkdir()
        if installed:
            plugins = root / "lib" / "plugins"
            plugins.mkdir(parents=True)
            for name in installed:
                (plugins / name).mkdir()
        for rel, content in pages.items():
            p = pages_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return root

    return _build
