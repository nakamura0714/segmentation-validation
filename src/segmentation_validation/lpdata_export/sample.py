"""1サンプル（画像1枚）の属性を組み立てる。

``FIELD_BUILDERS`` のキー集合が、テンプレートの ``meta.structure`` と一致していなければ
ならない（``template.validate_coverage`` が起動時に検査する）。**テンプレート側が正典**
なので、食い違ったら直すのは常にこちら。

守る規約:

- ``structure`` に宣言された属性は**全サンプルにキーとして必ず存在**する。
  値が無いときは単一値なら ``null``、``multiple: true`` なら ``[]``
- マスク3種は必ず ``{"pixel_array": ...}`` の dict で書く。``pixel_array`` キーごと
  省略すると lp-data が ``TypeError`` にする。属性ごと ``null`` にするのとも別物
  （あちらは「マスクというオブジェクトが無い」、こちらは「領域が無い」）
- ``lung_rect`` だけは属性ごと ``null``。``Rect`` に空表現が無く、lp-data は
  ``x_min >= x_max`` の退化矩形を ``ValueError`` で弾く
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..core.records import FileGroup
from .labels import SampleLabels
from .measure import SampleMeasurement
from .paths import record_path

#: 撮影方向を持つ series レベルの分類ラベル。
VIEW_POSITION_SYSTEM = "CRSeriesTypes"


@dataclass(frozen=True)
class SampleContext:
    """1サンプルを組み立てるのに要るものすべて。"""

    group: FileGroup
    labels: SampleLabels
    measurement: SampleMeasurement
    #: ``image_file`` に書くパス。変換しない / できなかったなら None。
    image_path: Path | None
    #: ``pneumothorax_mask.pixel_array`` に書くパス。マスクが無いなら None。
    mask_path: Path | None
    #: 相対化の基準（出力JSONの親ディレクトリ）。
    base: Path
    path_style: str = "relative"

    def record(self, path: Path | str | None) -> str | None:
        if path is None:
            return None
        return record_path(Path(path), self.base, self.path_style)


# ------------------------------------------------------------------ 入力


def _patient_id(ctx: SampleContext) -> str | None:
    return ctx.group.patient_id or None


def _dicom_file(ctx: SampleContext) -> str | None:
    # 原本は読み取り専用で出力先の外にあるため、実質常に絶対パスになる。
    return ctx.record(ctx.group.resolved_image_path)


def _image_file(ctx: SampleContext) -> str | None:
    return ctx.record(ctx.image_path)


def _pixel_spacing(ctx: SampleContext) -> dict[str, float] | None:
    """``{x, y}`` のみ。``z`` を書くと lp-data が 3D と解釈する。

    0・負値は仕様上不正（正の有限数であること）なので、値が無いのと同じに扱う。
    """
    x, y = ctx.group.spacing
    if x is None or y is None or x <= 0 or y <= 0:
        return None
    return {"x": float(x), "y": float(y)}


def _image_shape(ctx: SampleContext) -> dict[str, int] | None:
    shape = ctx.group.series_shape
    if shape is None:
        return None
    height, width = shape
    return {"height": int(height), "width": int(width)}


# --------------------------------------------------------------- 由来メタ


def _facility_id(ctx: SampleContext) -> str | None:
    return ctx.group.institution or None


def _vendor_id(ctx: SampleContext) -> str | None:
    return ctx.group.manufacturer or None


def _view_position(ctx: SampleContext) -> str | None:
    for label in ctx.group.case_labels:
        if label.code_system == VIEW_POSITION_SYSTEM and label.code_text_eng:
            return label.code_text_eng
    return None


# ------------------------------------------------------------------ ラベル


def _pneumothorax_case(ctx: SampleContext) -> bool:
    return ctx.labels.pneumothorax_case


def _abnormal_finding_status(ctx: SampleContext) -> str:
    return ctx.labels.abnormal_finding_status


def _pneumothorax_side(ctx: SampleContext) -> None:
    return ctx.labels.pneumothorax_side


def _bulla_bleb_status(ctx: SampleContext) -> str:
    return ctx.labels.bulla_bleb_status


# ------------------------------------------------------------------ マスク


def _pneumothorax_mask(ctx: SampleContext) -> dict[str, Any]:
    return {"pixel_array": ctx.record(ctx.mask_path)}


def _lung_mask(ctx: SampleContext) -> dict[str, Any]:
    return {"pixel_array": ctx.record(ctx.measurement.lung_mask_path)}


def _thorax_mask(ctx: SampleContext) -> dict[str, Any]:
    return {"pixel_array": ctx.record(ctx.measurement.thorax_mask_path)}


# ------------------------------------------------------------------ 導出値


def _lung_rect(ctx: SampleContext) -> dict[str, int] | None:
    return ctx.measurement.lung_rect()


def _pneumothorax_area_pix2(ctx: SampleContext) -> int | None:
    return ctx.measurement.pneumothorax_pixels


def _pneumothorax_area_mm2(ctx: SampleContext) -> float | None:
    return ctx.measurement.pneumothorax_area_mm2


#: 属性名 → その値を作る関数。キー集合がテンプレートの structure と一致すること。
FIELD_BUILDERS: dict[str, Callable[[SampleContext], Any]] = {
    "patient_id": _patient_id,
    "dicom_file": _dicom_file,
    "image_file": _image_file,
    "pixel_spacing": _pixel_spacing,
    "image_shape": _image_shape,
    "facility_id": _facility_id,
    "vendor_id": _vendor_id,
    "view_position": _view_position,
    "pneumothorax_case": _pneumothorax_case,
    "abnormal_finding_status": _abnormal_finding_status,
    "pneumothorax_side": _pneumothorax_side,
    "bulla_bleb_status": _bulla_bleb_status,
    "pneumothorax_mask": _pneumothorax_mask,
    "lung_mask": _lung_mask,
    "thorax_mask": _thorax_mask,
    "lung_rect": _lung_rect,
    "pneumothorax_area_pix2": _pneumothorax_area_pix2,
    "pneumothorax_area_mm2": _pneumothorax_area_mm2,
}


def build_sample(ctx: SampleContext) -> dict[str, Any]:
    """1サンプルの dict を作る。キーは ``FIELD_BUILDERS`` と同じ集合。"""
    return {name: builder(ctx) for name, builder in FIELD_BUILDERS.items()}
