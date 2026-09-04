"""M04 空マスク・全塗りでないか。

空マスク（全画素が同じ値）はアノテーションとして成立しない。
逆に画像のほぼ全面が前景のものも、領域指定として意味を成していない疑いがある。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._common import for_role, readable

CHECK_ID = "M04_MASK_NOT_EMPTY"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "空マスク・全塗りでないか"
DESCRIPTION = "前景0px / 全画素同値 / 極端な全塗り を検出する。"

EMPTY = "M04_MASK_EMPTY"
FULL = "M04_MASK_FULL"


def run(ctx: CheckContext) -> Iterator[Issue]:
    threshold = ctx.config.thresholds.mask_full_ratio
    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement) or measurement.fg_pixels is None:
            continue

        if measurement.fg_pixels == 0 or measurement.n_unique_values == 1:
            yield ctx.issue(
                EMPTY,
                record,
                f"空マスク: 前景 {measurement.fg_pixels}px / "
                f"画素値の種類 {measurement.n_unique_values}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.CRITICAL,
                fg_pixels=measurement.fg_pixels,
                n_unique_values=measurement.n_unique_values,
            )
        elif (measurement.fg_ratio or 0) > threshold:
            yield ctx.issue(
                FULL,
                record,
                f"画像のほぼ全面が前景: {measurement.fg_ratio:.3f} > {threshold}",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.HIGH,
                fg_ratio=measurement.fg_ratio,
                threshold=threshold,
            )
