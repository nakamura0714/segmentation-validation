"""DICOM → 16bit PNG 変換を守る。

実装の正典は med-chest-metry-pi6 の ``dataprep/dicom_to_png.py``。あちらへ
パッケージ依存を張れないので移植してあり、**同じ画素が出ること**をここで固定する。

特に MONOCHROME1 の反転は「per-image の max」ではなく「``BitsStored`` 由来の
固定レンジ」で行う。per-image にすると画像ごとに濃度スケールが変動し、
固定スケール契約（学習側の windowing が前提にしている）が黙って崩れる。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pydicom
import pytest
from conftest import make_group
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from segmentation_validation.core.imageio import (
    UINT16_MAX,
    convert_dicom_to_png,
    save_binary_mask,
)
from segmentation_validation.lpdata_export.images import (
    CONVERTED,
    MISSING_DICOM,
    REUSED,
    MissingDicomError,
    prepare_images,
)

BITS_STORED = 16


def make_dicom(
    path: Path,
    pixels: np.ndarray,
    *,
    photometric: str = "MONOCHROME2",
    bits_stored: int = BITS_STORED,
) -> Path:
    """合成 DICOM を書く。実データを使わずに変換の分岐を突くため。"""
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = pydicom.uid.ComputedRadiographyImageStorage
    meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()

    dataset = Dataset()
    dataset.file_meta = meta
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.Rows, dataset.Columns = pixels.shape
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = photometric
    dataset.BitsAllocated = 16
    dataset.BitsStored = bits_stored
    dataset.HighBit = bits_stored - 1
    dataset.PixelRepresentation = 0
    dataset.PixelSpacing = [0.143, 0.143]
    dataset.PixelData = pixels.astype(np.uint16).tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(str(path), enforce_file_format=True)
    return path


def read_png(path: Path) -> np.ndarray:
    return cv2.imread(str(path), cv2.IMREAD_UNCHANGED)


# ------------------------------------------------------------------ 変換本体


def test_16bitPNGとして書き出される(tmp_path: Path):
    """``utils/io.load_grayscale16`` が uint16 以外を拒否するので契約を守る。"""
    pixels = np.arange(16 * 20, dtype=np.uint16).reshape(16, 20) * 3
    dicom = make_dicom(tmp_path / "in.dcm", pixels)
    png = tmp_path / "out.png"

    provenance = convert_dicom_to_png(dicom, png)
    loaded = read_png(png)

    assert loaded.dtype == np.uint16
    # per-image の min-max 正規化をしない＝画素がそのまま往復する。
    assert np.array_equal(loaded, pixels)
    assert provenance["output_dtype"] == "uint16"
    assert provenance["inverted_monochrome1"] is False
    assert provenance["pixel_spacing"] == [0.143, 0.143]


def test_MONOCHROME1はBitsStored由来の固定レンジで反転する(tmp_path: Path):
    """per-image の max で反転すると、画像ごとに濃度スケールが変わってしまう。"""
    pixels = np.arange(16 * 20, dtype=np.uint16).reshape(16, 20) * 3
    dicom = make_dicom(tmp_path / "in.dcm", pixels, photometric="MONOCHROME1")
    png = tmp_path / "out.png"

    provenance = convert_dicom_to_png(dicom, png)
    loaded = read_png(png)

    assert provenance["inverted_monochrome1"] is True
    assert np.array_equal(loaded, ((1 << BITS_STORED) - 1) - pixels)
    # per-image の max（= pixels.max()）基準ではないこと。
    assert not np.array_equal(loaded, pixels.max() - pixels)


def test_変換に失敗したら出力ファイルを残さない(tmp_path: Path, monkeypatch):
    """空画像・中途半端なPNGを置かない。

    「存在する＝完成している」に冪等な再実行（既存を skip）が依存している。
    """
    import segmentation_validation.core.imageio as imageio

    pixels = np.zeros((4, 4), dtype=np.uint16)
    dicom = make_dicom(tmp_path / "in.dcm", pixels)
    png = tmp_path / "out.png"
    monkeypatch.setattr(imageio.cv2, "imwrite", lambda *a, **k: False)

    with pytest.raises(OSError):
        convert_dicom_to_png(dicom, png)

    assert not png.exists()
    assert list(tmp_path.glob("*.tmp.png")) == []


def test_マスクは0と255で往復する(tmp_path: Path):
    mask = np.zeros((6, 8), dtype=bool)
    mask[2:4, 3:6] = True
    path = save_binary_mask(tmp_path / "m.png", mask)

    loaded = read_png(path)

    assert set(np.unique(loaded).tolist()) == {0, 255}
    assert np.array_equal(loaded > 0, mask)


def test_UINT16_MAXは学習側と同じ値():
    assert UINT16_MAX == 65535


# ------------------------------------------------------------- ドライバ


def group_with_dicom(tmp_path: Path, name: str = "S1", exists: bool = True):
    """``resolved_image_path`` を tmp_path 配下に差し替えた FileGroup。"""
    dicom = tmp_path / "dcm" / f"{name}.dcm"
    if exists:
        make_dicom(dicom, np.full((4, 4), 100, dtype=np.uint16))
    return replace(make_group(file=name), resolved_image_path=dicom)


def test_DICOMが無ければskipしてimage_fileはnull(tmp_path: Path):
    group = group_with_dicom(tmp_path, exists=False)

    results = prepare_images(
        [group], image_output_dir=tmp_path / "images", mode="convert"
    )

    assert results[group.file_uid].status == MISSING_DICOM
    assert results[group.file_uid].png_path is None
    # 空画像を作らない。
    assert not (tmp_path / "images").exists()


def test_on_missing_dicom_errorなら停止する(tmp_path: Path):
    group = group_with_dicom(tmp_path, exists=False)

    with pytest.raises(MissingDicomError):
        prepare_images(
            [group],
            image_output_dir=tmp_path / "images",
            mode="convert",
            on_missing_dicom="error",
        )


def test_既存PNGは再変換せずforceなら作り直す(tmp_path: Path):
    group = group_with_dicom(tmp_path)
    out = tmp_path / "images"

    first = prepare_images([group], image_output_dir=out, mode="convert")
    assert first[group.file_uid].status == CONVERTED

    # 中身を書き換えておき、再変換されたかを画素で判定する。
    png = first[group.file_uid].png_path
    save_binary_mask(png, np.ones((4, 4), dtype=bool))

    second = prepare_images([group], image_output_dir=out, mode="convert")
    assert second[group.file_uid].status == REUSED
    assert read_png(png).max() == 255  # 書き換えたまま

    third = prepare_images([group], image_output_dir=out, mode="convert", force=True)
    assert third[group.file_uid].status == CONVERTED
    assert read_png(png).max() == 100  # 元のDICOM由来に戻っている


def test_plannedは変換せず予定パスだけ返す(tmp_path: Path):
    group = group_with_dicom(tmp_path)
    out = tmp_path / "images"

    results = prepare_images([group], image_output_dir=out, mode="planned")

    assert results[group.file_uid].png_path == out / f"{group.file}.png"
    assert not out.exists()


def test_noneはimage_fileを持たない(tmp_path: Path):
    group = group_with_dicom(tmp_path)

    results = prepare_images([group], image_output_dir=tmp_path / "images", mode="none")

    assert results[group.file_uid].png_path is None


# --------------------------------------------- 移植元への追随（drift 検知）

#: 移植元 ``convert_dicom_to_png`` の**順序に意味のある**処理。
#: 反転（stored 値空間・固定レンジ）→ modality LUT の順であることが前提。
UPSTREAM_ORDERED_STEPS = (
    'getattr(ds, "PhotometricInterpretation", "") == "MONOCHROME1"',
    "(1 << int(bits_stored)) - 1 if bits_stored else int(raw.max())",
    "ceiling - raw",
    "apply_modality_lut(raw, ds)",
)
#: ファイル内のどこかにあればよいもの（移植元では別ヘルパにある）。
UPSTREAM_STEPS = ("np.clip(array, 0, UINT16_MAX)",)


def upstream_path():
    from segmentation_validation.config import Config

    return (
        Config().project_root.parent
        / "med-chest-metry-pi6/src/chest_metry_pi6/dataprep/dicom_to_png.py"
    )


@pytest.mark.realdata
def test_DICOM変換が移植元と同じ実装である():
    """med-chest-metry-pi6 の ``dicom_to_png.py`` が変わったら落ちる。

    あちらへパッケージ依存を張れない（torch / lightning / lpdata を引き、lpdata が
    ``opencv-python`` を要求して本リポジトリの headless 統一と衝突する）ため実装を
    移植してある。移植は**気づかないうちに古くなる**ので、ここで検知する。

    落ちたときは向こうの差分を読み、``core.imageio.convert_dicom_to_png`` を
    追随させること（正典は向こう）。
    """
    import ast

    path = upstream_path()
    if not path.exists():
        pytest.skip(f"移植元が無い: {path}")

    source = path.read_text(encoding="utf-8")
    missing = [step for step in UPSTREAM_STEPS if step not in source]
    assert not missing, f"移植元 {path} の処理が変わっている: {missing}"

    body = next(
        (
            ast.get_source_segment(source, node)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "convert_dicom_to_png"
        ),
        None,
    )
    assert body is not None, f"移植元に convert_dicom_to_png が無い: {path}"

    missing = [step for step in UPSTREAM_ORDERED_STEPS if step not in body]
    assert not missing, (
        f"移植元 {path} の convert_dicom_to_png が変わっている: {missing}。"
        "core/imageio.py の convert_dicom_to_png を追随させる"
    )

    positions = [body.index(step) for step in UPSTREAM_ORDERED_STEPS]
    assert positions == sorted(positions), (
        f"移植元 {path} の処理順が変わっている。"
        "MONOCHROME1 の反転は modality LUT を当てる**前**に行うことが前提"
    )
