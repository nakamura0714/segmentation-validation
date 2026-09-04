"""M01-M08 が共有する小さな道具。

``path_mask`` と ``path_original_mask`` を同じ severity で扱わないのが要点。
original は Annotation Tool が保存した生のブラシストロークで、RGBA の
アンチエイリアス付きが**仕様上正常**（179件中73件）。同一視すると
73件の偽errorが本物の検出を埋めてしまう。
"""

from __future__ import annotations

from typing import Any, Iterator

from ...core.measure import MaskMeasurement
from ...core.records import AnnotationRecord
from ..base import CheckContext


def for_role(
    ctx: CheckContext, role: str
) -> Iterator[tuple[AnnotationRecord, MaskMeasurement]]:
    """レコードと計測値の組を返す。計測が無いものは飛ばす。"""
    for record in ctx.records:
        measurement = ctx.masks.get((record.geometry_uid, role))
        if measurement is not None:
            yield record, measurement


def mask_shape(measurement: MaskMeasurement) -> tuple[int, int] | None:
    if measurement.image_height is None or measurement.image_width is None:
        return None
    return (measurement.image_height, measurement.image_width)


def dicom_shape(file_measurement: Any) -> tuple[int, int] | None:
    if file_measurement is None:
        return None
    if file_measurement.dicom_rows is None or file_measurement.dicom_columns is None:
        return None
    return (file_measurement.dicom_rows, file_measurement.dicom_columns)


def readable(measurement: MaskMeasurement) -> bool:
    """形状量が計測できている（＝実際に読めた）か。"""
    return measurement.exists and measurement.read_error is None
