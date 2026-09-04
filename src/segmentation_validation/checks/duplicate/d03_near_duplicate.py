"""D03 近似重複（完全一致ではないが IoU が高い）。

完全一致ではないので自動採否はできない。どちらを採るかは目視で決める。
ラベルが違う場合は「別所見の重なり」として別IDにする —— 重複ではなく
同じ場所に複数の所見がある状態かもしれないため。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._emit import emit_pair
from .grouping import DuplicateKind, build_groups, classify

CHECK_ID = "D03_NEAR_DUPLICATE"
CATEGORY = Category.DUPLICATE
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "近似重複"
DESCRIPTION = "IoU が閾値以上だが画素完全一致ではない。自動採否は禁止。"

OVERLAPPING = "D03_OVERLAPPING_DIFFERENT_FINDING"


def run(ctx: CheckContext) -> Iterator[Issue]:
    classified = classify(ctx.pairs, ctx.config)
    groups = build_groups(classified)
    threshold = ctx.config.thresholds.duplicate_iou_near
    for item in classified:
        if item.kind is DuplicateKind.NEAR_SAME_LABEL:
            yield from emit_pair(
                ctx,
                CHECK_ID,
                item,
                f"ラベルが同じでIoU {item.pair.iou:.4f} >= {threshold}"
                "（完全一致ではない）",
                Severity.WARNING,
                ReviewPriority.HIGH,
                groups,
            )
        elif item.kind is DuplicateKind.NEAR_DIFFERENT_LABEL:
            yield from emit_pair(
                ctx,
                OVERLAPPING,
                item,
                f"ほぼ同じ領域に異なるラベルが付いている（IoU {item.pair.iou:.4f}）",
                Severity.WARNING,
                ReviewPriority.HIGH,
                groups,
            )
