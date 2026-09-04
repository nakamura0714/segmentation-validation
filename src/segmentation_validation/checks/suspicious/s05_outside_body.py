"""S05 体外領域。

PI6 固有の側方バンド（``datasets/pi6/outside_body``）に対して、
マスクがどれだけはみ出しているかを見る。

判定できない場合を**明示的に出す**のが要点。参照マスクは全体の3割で欠けており
（01272 は 0/296 ファイル）、黙って合格させると「問題なし」と「未検査」が
区別できなくなる。既定ではこれを目視対象には含めず、
``kept_without_full_check`` として採否マスタに記録する。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ...datasets.pi6.references import ReferenceStatus
from ..base import Category, CheckContext, CheckStatus, Issue, ReviewPriority, Severity
from ..machine._common import for_role, readable

CHECK_ID = "S05_OUTSIDE_BODY"
CATEGORY = Category.SUSPICIOUS
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "体外領域"
DESCRIPTION = "胸郭から再構成した側方バンドの外へマスクが出ていないかを見る。"

OUTSIDE = "S05_OUTSIDE_BODY"
UNAVAILABLE = "S05_REFERENCE_UNAVAILABLE"


def run(ctx: CheckContext) -> Iterator[Issue]:
    thresholds = ctx.config.thresholds
    margin = ctx.config.reference_masks.margin_mm

    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement):
            continue

        if measurement.lat_containment is None:
            status = measurement.reference_status or ReferenceStatus.NO_REFERENCE
            yield ctx.issue(
                UNAVAILABLE,
                record,
                f"参照マスクが使えないので体外判定ができない（{status}）",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.CANNOT_DETERMINE,
                review_priority=ReviewPriority.LOW,
                cannot_determine_reason=status,
                reference_status=status,
            )
            continue

        containment = measurement.lat_containment
        outside_mm2 = measurement.lat_outside_mm2 or 0.0

        if containment < thresholds.outside_body_error:
            severity, priority = Severity.ERROR, ReviewPriority.CRITICAL
        elif (
            containment < thresholds.outside_body_warn
            and outside_mm2 > thresholds.outside_body_warn_min_mm2
        ):
            severity, priority = Severity.WARNING, ReviewPriority.HIGH
        elif containment < 1.0:
            severity, priority = Severity.INFO, ReviewPriority.LOW
        else:
            continue

        yield ctx.issue(
            OUTSIDE,
            record,
            f"側方バンドの外へ出ている: 包含率 {containment:.4f} "
            f"(margin {margin}mm) はみ出し {outside_mm2:.0f}mm² "
            f"最大 {measurement.lat_max_mm:.1f}mm",
            category=CATEGORY,
            severity=severity,
            review_priority=priority,
            containment=containment,
            outside_mm2=outside_mm2,
            outside_pixels=measurement.lat_outside_pixels,
            max_distance_mm=measurement.lat_max_mm,
            margin_mm=margin,
            # アノテータ起因の系統誤差を疑うための手掛かり。
            annotator=record.user,
        )
