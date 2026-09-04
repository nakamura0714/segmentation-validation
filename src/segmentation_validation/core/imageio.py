"""DICOM・マスクPNG・参照マスクの読み込み。

``core.measure`` 以外からは、単発の表示や検証にだけ使う。
一括走査で画素ファイルを開くのは ``core.measure`` に一本化してある
（1665枚のマスクをディスクから1度だけ読むという設計の要）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from PIL import Image, UnidentifiedImageError
from pydicom.pixels import apply_voi_lut

logger = logging.getLogger(__name__)

# 生画素値の一覧を保持する上限。0/255 の二値かどうかを見るには十分で、
# 連続値のマスクでキャッシュ行が膨らむのを防ぐ。
MAX_UNIQUE_VALUES = 8
# 参照マスクの一部が 21 バイトの ``Internal Server Error`` という
# テキストファイルになっている。
# PNG シグネチャを確認して、PIL の例外に頼らず早期に弾く。
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MIN_PNG_BYTES = 200


class MaskReadError(RuntimeError):
    """マスク画像が存在するのに読めない（壊れている）。"""


@dataclass(frozen=True)
class DicomHeader:
    """DICOMのヘッダだけを読んだ結果。

    解像度チェックには画素データが要らないので ``stop_before_pixels=True`` で読む。
    実測 22ms/件、1083件で24秒。全画素読みは4分40秒かかるので差は大きい。
    """

    path: Path
    exists: bool
    rows: int | None = None
    columns: int | None = None
    bits_allocated: int | None = None
    bits_stored: int | None = None
    photometric: str | None = None
    spacing_x: float | None = None
    spacing_y: float | None = None
    manufacturer: str | None = None
    read_error: str | None = None

    @property
    def shape(self) -> tuple[int, int] | None:
        """``(height, width)``。マスクの ``ndarray.shape`` と直接比較できる向き。"""
        if self.rows is None or self.columns is None:
            return None
        return (self.rows, self.columns)


@dataclass(frozen=True)
class RawMask:
    """マスクPNGを二値化する前の姿。

    ``M02_MASK_CHANNELS`` / ``M03_MASK_BINARY`` は二値化前の値でしか判定できないので、
    二値化と生属性の取得を必ず分ける。
    """

    path: Path
    exists: bool
    pil_mode: str | None = None
    width: int | None = None
    height: int | None = None
    ndim: int | None = None
    n_channels: int | None = None
    dtype: str | None = None
    value_min: int | None = None
    value_max: int | None = None
    n_unique_values: int | None = None
    unique_values: tuple[int, ...] = ()
    unique_values_truncated: bool = False
    binarized_from: str | None = None
    file_bytes: int | None = None
    read_error: str | None = None
    array: np.ndarray | None = None

    @property
    def shape(self) -> tuple[int, int] | None:
        if self.height is None or self.width is None:
            return None
        return (self.height, self.width)

    def binarize(self) -> np.ndarray:
        """非ゼロを前景とする bool 配列を返す。"""
        if self.array is None:
            raise MaskReadError(f"マスクを読めていない: {self.path}")
        return _select_channel(self.array) > 0


def load_dicom_header(path: Path) -> DicomHeader:
    """DICOMのヘッダのみを読む。画素データには触れない。"""
    if not path.exists():
        return DicomHeader(path=path, exists=False)
    try:
        dcm = pydicom.dcmread(path, stop_before_pixels=True)
    except Exception as error:  # 壊れたDICOMがあっても走査は続ける
        return DicomHeader(
            path=path, exists=True, read_error=f"{type(error).__name__}: {error}"
        )

    spacing = getattr(dcm, "PixelSpacing", None) or getattr(
        dcm, "ImagerPixelSpacing", None
    )
    spacing_y, spacing_x = (None, None)
    if spacing is not None and len(spacing) >= 2:
        # DICOM の PixelSpacing は (row spacing, column spacing) = (y, x)。
        spacing_y, spacing_x = float(spacing[0]), float(spacing[1])

    return DicomHeader(
        path=path,
        exists=True,
        rows=_as_int(getattr(dcm, "Rows", None)),
        columns=_as_int(getattr(dcm, "Columns", None)),
        bits_allocated=_as_int(getattr(dcm, "BitsAllocated", None)),
        bits_stored=_as_int(getattr(dcm, "BitsStored", None)),
        photometric=_as_str(getattr(dcm, "PhotometricInterpretation", None)),
        spacing_x=spacing_x,
        spacing_y=spacing_y,
        manufacturer=_as_str(getattr(dcm, "Manufacturer", None)),
    )


def load_dicom_image(path: Path) -> np.ndarray:
    """DICOMを表示用の float 配列 (0.0-1.0) として読み込む。

    VOI LUT と MONOCHROME1 の反転を適用する。元配列は破壊しない。
    """
    dcm = pydicom.dcmread(path)
    array = dcm.pixel_array

    try:
        array = apply_voi_lut(array, dcm)
    except Exception:  # LUTが壊れている症例があり得るので描画は続行する
        logger.warning("apply_voi_lut に失敗したため生画素を使用する: %s", path)

    array = array.astype(np.float32)
    if getattr(dcm, "PhotometricInterpretation", "") == "MONOCHROME1":
        array = array.max() - array

    lo, hi = np.percentile(array, [0.5, 99.5])
    if hi <= lo:
        lo, hi = float(array.min()), float(array.max())
    if hi <= lo:
        return np.zeros_like(array)
    return np.clip((array - lo) / (hi - lo), 0.0, 1.0)


def load_mask_raw(path: Path, keep_array: bool = True) -> RawMask:
    """マスクPNGを二値化前の属性込みで読む。

    ``path_mask`` は L モードの 0/255 だが ``path_original_mask`` は RGBA のことがあり
    （ブラシストロークのアンチエイリアスで alpha が連続値）、これは仕様上正常。
    その差を潰さないよう、生の mode / 値域 / チャンネル数をすべて残す。
    """
    if not path.exists():
        return RawMask(path=path, exists=False)

    size = path.stat().st_size
    try:
        with Image.open(path) as image:
            pil_mode = image.mode
            width, height = image.size
            array = np.asarray(image)
    except (UnidentifiedImageError, OSError) as error:
        return RawMask(
            path=path,
            exists=True,
            file_bytes=size,
            read_error=f"{type(error).__name__}: {error}",
        )

    channel = _select_channel(array)
    unique = np.unique(channel)
    return RawMask(
        path=path,
        exists=True,
        pil_mode=pil_mode,
        width=width,
        height=height,
        ndim=int(array.ndim),
        n_channels=int(array.shape[2]) if array.ndim == 3 else 1,
        dtype=str(array.dtype),
        value_min=int(unique.min()),
        value_max=int(unique.max()),
        n_unique_values=int(unique.size),
        unique_values=tuple(int(v) for v in unique[:MAX_UNIQUE_VALUES]),
        unique_values_truncated=bool(unique.size > MAX_UNIQUE_VALUES),
        binarized_from=_channel_source(array),
        file_bytes=size,
        array=array if keep_array else None,
    )


def load_binary_mask(path: Path) -> np.ndarray:
    """マスクPNGを bool 配列として読み込む。読めなければ例外。"""
    raw = load_mask_raw(path)
    if not raw.exists:
        raise FileNotFoundError(path)
    if raw.read_error is not None:
        raise MaskReadError(f"{path}: {raw.read_error}")
    return raw.binarize()


def load_reference_mask(path: Path) -> np.ndarray:
    """胸郭系の参照マスクを bool 配列として読み込む。

    参照マスクは別パイプラインの生成物で、実際に 21 バイトの
    ``Internal Server Error`` というテキストファイルが混ざっている。
    PIL の例外任せにすると走査が途中で落ちるので、サイズとシグネチャでも防御する。
    """
    if not path.exists():
        raise FileNotFoundError(path)
    if path.stat().st_size < MIN_PNG_BYTES:
        with path.open("rb") as handle:
            head = handle.read(len(PNG_SIGNATURE))
        if not head.startswith(PNG_SIGNATURE):
            raise MaskReadError(f"{path}: PNGではない（{path.stat().st_size} バイト）")
    try:
        with Image.open(path) as image:
            array = np.asarray(image)
    except (UnidentifiedImageError, OSError) as error:
        raise MaskReadError(f"{path}: {type(error).__name__}: {error}") from error
    return _select_channel(array) > 0


# ------------------------------------------------------------------ helpers


def _select_channel(array: np.ndarray) -> np.ndarray:
    """多チャンネル画像から前景を表すチャンネルを選ぶ。

    RGBA / LA では alpha が描画領域を表すので alpha を使う。
    RGB では最大値を取る（どのチャンネルに描かれていても拾うため）。
    """
    if array.ndim != 3:
        return array
    if array.shape[2] in (2, 4):
        return array[..., -1]
    return array.max(axis=2)


def _channel_source(array: np.ndarray) -> str:
    if array.ndim != 3:
        return "luminance"
    return "alpha" if array.shape[2] in (2, 4) else "max_channel"


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
