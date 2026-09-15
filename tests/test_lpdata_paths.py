"""出力JSONに書くパスの表記を守る。

lp-data は相対パスを ``load_dataset(root_dir=...)`` 基準で解決し、データセット
ファイルの所在ディレクトリへはフォールバックしない。基準を間違えると、
**読み込み時に「ファイルが無い」ではなく cwd 基準の別ファイルを掴む**こともある。
"""

from __future__ import annotations

from pathlib import Path

from conftest import FILE, make_group

from segmentation_validation.lpdata_export.paths import (
    image_path_for,
    mask_path_for,
    record_path,
    sample_id_for,
)


def test_sample_idはfile_listのキーを使う():
    """DICOMファイル名の stem と一致する。学習側のテンプレート検査がこれを見る。"""
    group = make_group()

    assert sample_id_for(group) == FILE
    assert Path(group.image_path).stem == sample_id_for(group)


def test_出力先配下は相対パスで記録される(tmp_path: Path):
    base = tmp_path / "out"
    path = image_path_for(base / "images", "s0")

    assert record_path(path, base) == "images/s0.png"


def test_配下でなければ絶対パスになる(tmp_path: Path):
    """``../../..`` を並べるより絶対パスの方が壊れにくく、所在も読める。"""
    base = tmp_path / "out"
    outside = tmp_path / "elsewhere" / "images" / "s0.png"

    assert record_path(outside, base) == str(outside)


def test_path_style_absoluteなら配下でも絶対になる(tmp_path: Path):
    base = tmp_path / "out"
    path = image_path_for(base / "images", "s0")

    assert record_path(path, base, "absolute") == str(path)


def test_Noneはそのまま通す(tmp_path: Path):
    assert record_path(None, tmp_path) is None


def test_マスクの出力先はsample_idで決まる(tmp_path: Path):
    assert mask_path_for(tmp_path / "masks", "s0") == tmp_path / "masks" / "s0.png"


def test_相対パスもbase基準で解決してから判定する(tmp_path: Path, monkeypatch):
    """呼び出し側が相対パスを渡しても、絶対化してから配下かを見る。"""
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "out"

    assert record_path(Path("out/images/s0.png"), base) == "images/s0.png"
