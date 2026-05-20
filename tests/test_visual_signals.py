"""visual-comparison-proposal.md 의 Phase 4 신호 7개 unit test.

이미지 의존성 (Pillow / imagehash) 가 없으면 해당 케이스 skip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run  # noqa: E402


# ---------- Pillow 의존 신호 (1, 2, 7) ----------


@pytest.fixture
def pillow():
    pil = pytest.importorskip("PIL")
    return pil


def _solid_png(tmp_path: Path, name: str, color: tuple[int, int, int], size=(200, 100)):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", size, color).save(p, "PNG")
    return p


def test_pixel_diff_identical(tmp_path, pillow) -> None:
    a = _solid_png(tmp_path, "a.png", (200, 200, 200))
    b = _solid_png(tmp_path, "b.png", (200, 200, 200))
    out = run._vc_pixel_diff(str(a), str(b))
    assert out["diff_ratio"] == 0.0


def test_pixel_diff_changed(tmp_path, pillow) -> None:
    a = _solid_png(tmp_path, "a.png", (255, 255, 255))
    b = _solid_png(tmp_path, "b.png", (0, 0, 0))
    out = run._vc_pixel_diff(str(a), str(b))
    assert out["diff_ratio"] == pytest.approx(1.0, abs=0.01)
    assert out["width"] == 200 and out["height"] == 100


def test_pixel_diff_partial(tmp_path, pillow) -> None:
    from PIL import Image
    a_img = Image.new("RGB", (200, 100), (255, 255, 255))
    b_img = Image.new("RGB", (200, 100), (255, 255, 255))
    # b 의 좌측 절반을 검정
    for x in range(100):
        for y in range(100):
            b_img.putpixel((x, y), (0, 0, 0))
    a = tmp_path / "a.png"; a_img.save(a)
    b = tmp_path / "b.png"; b_img.save(b)
    out = run._vc_pixel_diff(str(a), str(b))
    assert 0.4 < out["diff_ratio"] < 0.6


def test_pixel_diff_overlay_created(tmp_path, pillow) -> None:
    a = _solid_png(tmp_path, "a.png", (255, 255, 255))
    b = _solid_png(tmp_path, "b.png", (0, 0, 0))
    overlay = tmp_path / "diff.png"
    out = run._vc_pixel_diff(str(a), str(b), out_overlay=str(overlay))
    assert overlay.is_file()
    assert out["overlay"] == str(overlay)


def test_tile_phash_identical(tmp_path, pillow) -> None:
    pytest.importorskip("imagehash")
    a = _solid_png(tmp_path, "a.png", (128, 200, 128), size=(800, 800))
    out = run._vc_tile_phash(str(a), str(a))
    assert out["max_distance"] == 0
    assert out["n_bad_tiles"] == 0
    assert out["n_tiles"] == 32  # 8×4


def test_tile_phash_local_change(tmp_path, pillow) -> None:
    pytest.importorskip("imagehash")
    from PIL import Image, ImageDraw
    a = Image.new("RGB", (800, 800), (200, 200, 200))
    b = Image.new("RGB", (800, 800), (200, 200, 200))
    # b 의 한 타일 영역에 풍부한 contrast 패턴 (체커보드) — phash 가 잡기 쉬움
    draw = ImageDraw.Draw(b)
    for x in range(600, 800, 20):
        for y in range(0, 100, 20):
            draw.rectangle((x, y, x + 10, y + 10), fill=(0, 0, 0))
    pa = tmp_path / "a.png"; a.save(pa)
    pb = tmp_path / "b.png"; b.save(pb)
    # bad_threshold 를 낮춰 작은 변화도 잡도록
    out = run._vc_tile_phash(str(pa), str(pb), bad_threshold=2)
    assert out["max_distance"] > 0
    assert out["n_bad_tiles"] >= 1


def test_color_hist_identical(tmp_path, pillow) -> None:
    a = _solid_png(tmp_path, "a.png", (100, 150, 200))
    out = run._vc_color_hist(str(a), str(a))
    assert out["cosine"] == pytest.approx(1.0, abs=0.01)


def test_color_hist_different(tmp_path, pillow) -> None:
    a = _solid_png(tmp_path, "a.png", (0, 0, 0))
    b = _solid_png(tmp_path, "b.png", (255, 255, 255))
    out = run._vc_color_hist(str(a), str(b))
    assert out["cosine"] < 0.5


# ---------- 의존성 없는 신호 (3, 5, 6) ----------


def test_element_compare_identical() -> None:
    blocks = [
        {"tag": "h1", "text": "Title", "x": 0, "y": 0, "w": 800, "h": 40},
        {"tag": "p", "text": "para 1", "x": 0, "y": 50, "w": 800, "h": 20},
        {"tag": "table", "text": "", "x": 0, "y": 80, "w": 800, "h": 100},
    ]
    out = run._vc_element_compare(blocks, blocks)
    assert out["ratio"] == 1.0
    assert out["matched"] == 3
    assert out["missing"] == 0


def test_element_compare_loss() -> None:
    a = [
        {"tag": "h1", "text": "Title"},
        {"tag": "p", "text": "para 1"},
        {"tag": "table", "text": ""},
        {"tag": "p", "text": "para 2"},
    ]
    b = [
        {"tag": "h1", "text": "Title"},
        {"tag": "p", "text": "para 1"},
        {"tag": "p", "text": "para 2"},
    ]  # table 빠짐
    out = run._vc_element_compare(a, b)
    assert out["missing"] >= 1
    assert out["ratio"] < 1.0


def test_bbox_lcs_identical() -> None:
    blocks = [
        {"tag": "h1", "text": "T", "x": 0, "y": 0, "w": 800, "h": 40},
        {"tag": "p", "text": "p", "x": 0, "y": 50, "w": 800, "h": 20},
    ]
    out = run._vc_bbox_lcs_compare(blocks, blocks)
    assert out["lcs_ratio"] == 1.0
    assert out["mean_width_diff"] == 0.0


def test_bbox_lcs_different_width() -> None:
    a = [
        {"tag": "h1", "text": "T", "x": 0, "y": 0, "w": 800, "h": 40},
    ]
    b = [
        {"tag": "h1", "text": "T", "x": 0, "y": 0, "w": 400, "h": 40},
    ]  # 절반 너비 (페이지 너비도 절반이면 상대값은 같음 — 다른 케이스)
    # 페이지 너비를 동일하게 — a 페이지 = 800, b 페이지 = 800
    b_in_wide = [
        {"tag": "h1", "text": "T", "x": 0, "y": 0, "w": 400, "h": 40},
        {"tag": "p", "text": "filler", "x": 0, "y": 50, "w": 800, "h": 20},
    ]
    a_in_wide = [
        {"tag": "h1", "text": "T", "x": 0, "y": 0, "w": 800, "h": 40},
        {"tag": "p", "text": "filler", "x": 0, "y": 50, "w": 800, "h": 20},
    ]
    out = run._vc_bbox_lcs_compare(a_in_wide, b_in_wide)
    # h1 의 상대 너비가 1.0 vs 0.5 → 평균 차이 0.25
    assert out["mean_width_diff"] > 0.1


def test_canonical_tree_basic() -> None:
    html = "<h1>X</h1><p>foo</p><table><tr><td>1</td></tr></table>"
    tree = run._vc_canonical_tree(html, is_storage=False)
    tags = [t for _d, t, _txt in tree]
    assert "h1" in tags
    assert "p" in tags
    assert "table" in tags


def test_canonical_tree_strips_chrome() -> None:
    html = '<div id="dokuwiki__header">CHROME</div><h1>Real</h1>'
    tree = run._vc_canonical_tree(html, is_storage=False)
    texts = [txt for _d, _t, txt in tree]
    assert "Real" in texts
    assert "CHROME" not in texts


def test_canonical_tree_wrap_to_macro() -> None:
    html = '<div class="wrap_info">info text</div>'
    tree = run._vc_canonical_tree(html, is_storage=False)
    tags = [t for _d, t, _ in tree]
    assert "macro:info" in tags


def test_canonical_tree_storage_macro() -> None:
    storage = '<ac:structured-macro ac:name="note"><p>note text</p></ac:structured-macro>'
    tree = run._vc_canonical_tree(storage, is_storage=True)
    tags = [t for _d, t, _ in tree]
    assert "macro:note" in tags


def test_canonical_tree_diff_identical() -> None:
    a = run._vc_canonical_tree("<h1>x</h1><p>y</p>", is_storage=False)
    b = run._vc_canonical_tree("<h1>x</h1><p>y</p>", is_storage=False)
    out = run._vc_canonical_tree_diff(a, b)
    assert out["ratio"] == 1.0
    assert out["missing"] == 0


def test_canonical_tree_diff_loss() -> None:
    a = run._vc_canonical_tree("<h1>x</h1><p>y</p><table><tr><td>z</td></tr></table>", is_storage=False)
    b = run._vc_canonical_tree("<h1>x</h1><p>y</p>", is_storage=False)
    out = run._vc_canonical_tree_diff(a, b)
    assert out["missing"] >= 1
    assert out["ratio"] < 1.0


def test_canonical_tree_diff_wrap_macro_match() -> None:
    """dokuwiki <div class=wrap_info> 와 Confluence <ac:structured-macro name=info>
    는 canonical 후 같은 'macro:info' 로 정규화되어 매칭됨."""
    a = run._vc_canonical_tree('<div class="wrap_info">text</div>', is_storage=False)
    b = run._vc_canonical_tree('<ac:structured-macro ac:name="info"><p>text</p></ac:structured-macro>',
                                is_storage=True)
    out = run._vc_canonical_tree_diff(a, b)
    # 둘 다 'macro:info' 노드를 가짐 — 매칭 1+
    assert out["ratio"] > 0


# ---------- _vc_compute_all dispatcher ----------


def test_vc_compute_all_dispatch(tmp_path, pillow) -> None:
    pytest.importorskip("imagehash")
    img = _solid_png(tmp_path, "x.png", (128, 200, 128), size=(800, 400))
    enabled = {
        "pixel_diff": True,
        "tile_phash": True,
        "element_compare": True,
        "ocr": False,  # tesseract 의존 — skip
        "bbox_lcs": True,
        "storage_ast": True,
        "color_hist": True,
    }
    blocks = [{"tag": "h1", "text": "X", "x": 0, "y": 0, "w": 800, "h": 40}]
    out = run._vc_compute_all(
        d_full_png=str(img), c_full_png=str(img),
        d_main_png=None, c_main_png=None,
        bboxes_dwk=blocks, bboxes_cnf=blocks,
        raw_html="<h1>X</h1>", storage_xml="<h1>X</h1>",
        enabled=enabled,
    )
    assert "pixel_diff" in out
    assert "tile_phash" in out
    assert "element_compare" in out
    assert "ocr" not in out
    assert "bbox_lcs" in out
    assert "storage_ast" in out
    assert "color_hist" in out


def test_vc_compute_all_subset(tmp_path) -> None:
    """이미지 의존 신호만 비활성 — 다른 건 정상."""
    enabled = {
        "storage_ast": True,
        "bbox_lcs": True,
        "element_compare": True,
    }
    out = run._vc_compute_all(
        d_full_png=None, c_full_png=None,
        d_main_png=None, c_main_png=None,
        bboxes_dwk=[{"tag": "h1", "text": "X", "x": 0, "y": 0, "w": 800, "h": 40}],
        bboxes_cnf=[{"tag": "h1", "text": "X", "x": 0, "y": 0, "w": 800, "h": 40}],
        raw_html="<h1>X</h1>", storage_xml="<h1>X</h1>",
        enabled=enabled,
    )
    assert "storage_ast" in out
    assert "bbox_lcs" in out
    assert "element_compare" in out
    assert "pixel_diff" not in out
