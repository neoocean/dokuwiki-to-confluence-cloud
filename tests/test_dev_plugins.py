"""dev container plugin 자동 감지/설치 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


def test_dev_is_full_install_recognizes_full(tmp_path) -> None:
    (tmp_path / "doku.php").write_text("<?php // dokuwiki\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "inc").mkdir()
    assert run._dev_is_full_install(tmp_path) is True


def test_dev_is_full_install_recognizes_data_only(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    assert run._dev_is_full_install(tmp_path) is False


def test_dev_data_root_case_a(tmp_path) -> None:
    # full install: src/data/pages
    (tmp_path / "doku.php").write_text("")
    (tmp_path / "lib").mkdir()
    (tmp_path / "inc").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "pages").mkdir()
    (tmp_path / "data" / "media").mkdir()
    assert run._dev_data_root(tmp_path) == tmp_path / "data"


def test_dev_data_root_case_b(tmp_path) -> None:
    # data root directly
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    assert run._dev_data_root(tmp_path) == tmp_path


def test_dev_detect_plugins_from_conf(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "plugins.local.php").write_text(
        "<?php\n$plugins['wrap'] = 1;\n$plugins['todo'] = 1;\n$plugins['info'] = 0;\n"
    )
    detected = run._dev_detect_plugins(tmp_path)
    assert "wrap" in detected
    assert "todo" in detected
    assert "info" not in detected


def test_dev_detect_plugins_from_struct_db(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "struct.sqlite3").write_bytes(b"\x00")
    detected = run._dev_detect_plugins(tmp_path)
    assert "struct" in detected


def test_dev_detect_plugins_from_macros(tmp_path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "media").mkdir()
    (tmp_path / "pages" / "x.txt").write_text("Foo\n~~DISCUSSION~~\nBar")
    detected = run._dev_detect_plugins(tmp_path)
    assert "discussion" in detected


def test_dev_install_plugins_already_present(tmp_path) -> None:
    (tmp_path / "lib" / "plugins" / "wrap").mkdir(parents=True)
    (tmp_path / "lib" / "plugins" / "wrap" / "syntax.php").write_text("")
    res = run._dev_install_plugins(tmp_path, ["wrap"])
    assert "wrap" in res["already"]
    assert not res["installed"]


def test_dev_install_plugins_bundled_skipped(tmp_path) -> None:
    (tmp_path / "lib" / "plugins").mkdir(parents=True)
    res = run._dev_install_plugins(tmp_path, ["info", "config", "acl"])
    assert set(res["bundled"]) == {"info", "config", "acl"}
    assert not res["installed"]
