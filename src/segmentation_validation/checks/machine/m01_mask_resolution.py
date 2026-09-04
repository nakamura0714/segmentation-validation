"""M01 マスク解像度がDICOMと一致するか。

真値になり得るものが4つある: PNGの実サイズ / DICOM の Rows・Columns /
``series.shape`` / ``annotation.width,height``。要件が言う「DICOMと一致」は
PNG↔DICOM なのでこれを error とし、JSON側の宣言値とのズレは別IDの warning にする。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ...core.records import AnnotationRecord
from ..base import Category, CheckContext, CheckStatus, Issue, ReviewPriority, Severity
from ._common import dicom_shape, for_role, mask_shape

CHECK_ID = "M01_MASK_RESOLUTION"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "マスク解像度がDICOMと一致するか"
DESCRIPTION = "PNG・DICOM・series.shape・annotation.width/height の4者を突き合わせる。"

MISMATCH = "M01_MASK_RESOLUTION"
JSON_MISMATCH = "M01_MASK_RESOLUTION_JSON"


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record, measurement in for_role(ctx, ROLE_MASK):
        png = mask_shape(measurement)
        if png is None:
            continue
        dicom = dicom_shape(ctx.files.get(record.file_uid))

        if dicom is None:
            yield ctx.issue(
                MISMATCH,
                record,
                "DICOMのサイズを読めないので解像度を照合できない",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.CANNOT_DETERMINE,
                review_priority=ReviewPriority.LOW,
                cannot_determine_reason="no_dicom_header",
                mask_shape=list(png),
            )
        elif png != dicom:
            yield ctx.issue(
                MISMATCH,
                record,
                f"マスクのサイズ {png[1]}x{png[0]} が "
                f"DICOM {dicom[1]}x{dicom[0]} と不一致",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.CRITICAL,
                mask_shape=list(png),
                dicom_shape=list(dicom),
            )

        if dicom is None:
            continue
        for name, declared in (
            ("series.shape", record.series_shape),
            ("annotation.width/height", _declared_shape(record)),
        ):
            if declared is None or declared == dicom:
                continue
            yield ctx.issue(
                JSON_MISMATCH,
                record,
                f"JSONの {name} {declared[1]}x{declared[0]} が "
                f"DICOM {dicom[1]}x{dicom[0]} と不一致",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.NORMAL,
                field=name,
                declared_shape=list(declared),
                dicom_shape=list(dicom),
            )


def _declared_shape(record: AnnotationRecord) -> tuple[int, int] | None:
    if record.declared_size is None:
        return None
    width, height = record.declared_size
    return (height, width)
