"""plugin-scan: 페이지 본문 → 미설치 플러그인 식별."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def _make_dokuwiki(tmp_path: Path, pages: dict[str, str], installed: set[str] | None = None) -> Path:
    """tmp_path 에 minimal DokuWiki 데이터 구조 생성."""
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


def test_scan_tilde_macros(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "~~NOTOC~~\nhello\n~~DISCUSSION~~",
        "b.txt": "~~DISCUSSION~~\n~~NOCACHE~~",
    }, installed={"include"})
    out = run._scan_plugin_usage(root)
    assert out["n_files"] == 2
    by_name = {(r["kind"], r["name"]): r for r in out["macros"]}
    assert ("tilde", "NOTOC") in by_name
    assert by_name[("tilde", "NOTOC")]["count"] == 1
    assert by_name[("tilde", "NOTOC")]["core"] is True
    assert ("tilde", "DISCUSSION") in by_name
    assert by_name[("tilde", "DISCUSSION")]["count"] == 2
    # discussion is missing
    missing = {m["plugin"] for m in out["missing"]}
    assert "discussion" in missing


def test_scan_double_brace(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "{{monthcal>year=2020,month=1}}\n{{page>:other}}\n{{youtube>abc123}}",
    }, installed={"include"})
    out = run._scan_plugin_usage(root)
    by_name = {(r["kind"], r["name"]): r for r in out["macros"]}
    assert ("double_brace", "monthcal") in by_name
    assert ("double_brace", "page") in by_name
    assert ("double_brace", "youtube") in by_name
    assert by_name[("double_brace", "page")]["installed"] is True  # include installed
    assert by_name[("double_brace", "monthcal")]["installed"] is False
    missing = {m["plugin"] for m in out["missing"]}
    assert "monthcal" in missing
    assert "youtube" in missing
    assert "include" not in missing


def test_scan_ignores_media_namespace(tmp_path) -> None:
    """`{{namespace:path}}` 는 미디어 ID — `>` 가 없으면 플러그인이 아님."""
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "{{u:lam:pasted:20181130-091542.png}}\n{{:b:files:foo.jpg}}",
    })
    out = run._scan_plugin_usage(root)
    plugin_names = {r["name"] for r in out["macros"] if r["kind"] == "double_brace"}
    # 'u' 와 'b' 는 미디어 namespace — 플러그인으로 등록 안됨
    assert "u" not in plugin_names
    assert "b" not in plugin_names


def test_scan_block_tags_with_html_filter(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "<wrap info>hi</wrap>\n<p>standard html</p>\n<ele>29.6</ele>\n<decrypt>secret</decrypt>",
    }, installed={"wrap"})
    out = run._scan_plugin_usage(root)
    by_name = {(r["kind"], r["name"]): r for r in out["macros"]}
    assert ("block_tag", "wrap") in by_name
    # <p> 표준 HTML 무시
    assert ("block_tag", "p") not in by_name
    # <ele> GPS 데이터 무시
    assert ("block_tag", "ele") not in by_name
    # <decrypt> → encrypt 플러그인 (미설치)
    assert ("block_tag", "decrypt") in by_name
    missing = {m["plugin"] for m in out["missing"]}
    assert "encrypt" in missing
    assert "wrap" not in missing


def test_scan_explicit_installed_set(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "~~DISCUSSION~~",
    })  # plugins dir 없음
    out = run._scan_plugin_usage(root, installed={"discussion"})
    missing = {m["plugin"] for m in out["missing"]}
    assert "discussion" not in missing  # installed 명시했으므로


def test_scan_missing_carries_install_url(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "a.txt": "{{monthcal>year=2020}}",
        "b.txt": "{{youtube>abc}}",
        "c.txt": "<davcal>...</davcal>",  # davcal 은 매핑 None
    })
    out = run._scan_plugin_usage(root)
    urls = {m["plugin"]: m["install_url"] for m in out["missing"]}
    # PLUGIN_DOWNLOADS 매핑 있는 플러그인 — URL 가짐
    assert "monthcal" in urls
    if run.PLUGIN_DOWNLOADS.get("monthcal"):
        assert urls["monthcal"].startswith("http")
    # davcal — 매핑 명시적 None (수동 설치 권장)
    # davcal 은 본 데이터에 없을 수 있으므로 강제 검증 안 함


def test_scan_count_aggregates_across_kinds(tmp_path) -> None:
    """동일 플러그인이 여러 매크로 형식으로 참조되면 합산."""
    root = _make_dokuwiki(tmp_path, {
        # tag 플러그인 — {{tag>}} + {{topic>}} + <tag> 모두 같은 플러그인 매핑
        "a.txt": "{{tag>a}}\n{{tag>b}}\n{{topic>c}}\n<tag>d</tag>",
    })
    out = run._scan_plugin_usage(root)
    # tag 플러그인이 missing 에 한 번만, count = 합산
    tag_entries = [m for m in out["missing"] if m["plugin"] == "tag"]
    assert len(tag_entries) == 1
    assert tag_entries[0]["count"] >= 4


def test_scan_samples_recorded(tmp_path) -> None:
    root = _make_dokuwiki(tmp_path, {
        "page-a.txt": "~~DISCUSSION~~",
        "sub/page-b.txt": "~~DISCUSSION~~",
        "sub/page-c.txt": "~~DISCUSSION~~",
    })
    out = run._scan_plugin_usage(root)
    disc = next(r for r in out["macros"] if r["name"] == "DISCUSSION")
    assert len(disc["samples"]) >= 2
    # 페이지 경로 기록
    assert any("page-a" in s for s in disc["samples"])
