"""D04 包含された重複。

IoU では拾えない形。小さいマスクが大きいマスクにほぼ完全に含まれる場合、
IoU は低く出るが（実測で 0.002 まで下がる）、包含率は 1.0 になる。

★ラベルが違う場合は重複とは限らない。実測39件中36件がラベル相違で
IoU 0.002〜0.09 —— つまり**小さい所見が大きい所見の内側にある「入れ子」**で、
結節が浸潤影の中にあるといった医学的に正当なケースが大半と思われる。
だから自動excludeは禁止で、severity も INFO に留めて目視の判断に委ねる。
Precision が低ければルールごと外せばよい。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._emit import emit_pair
from .grouping import DuplicateKind, build_groups, classify

CHECK_ID = "D04_CONTAINED_DUPLICATE"
CATEGORY = Category.DUPLICATE
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "包含された重複"
DESCRIPTION = "一方が他方にほぼ完全に含まれる。ラベル相違は入れ子の所見の可能性。"

DIFFERENT_LABEL = "D04_CONTAINED_DIFFERENT_LABEL"


def run(ctx: CheckContext) -> Iterator[Issue]:
    classified = classify(ctx.pairs, ctx.config)
    groups = build_groups(classified)
    threshold = ctx.config.thresholds.duplicate_containment
    for item in classified:
        if item.kind is DuplicateKind.CONTAINED_SAME_LABEL:
            yield from emit_pair(
                ctx,
                CHECK_ID,
                item,
                f"ラベルが同じで一方が他方に包含されている"
                f"（包含率 {item.max_containment:.4f} >= {threshold}、"
                f"IoU {item.pair.iou:.4f}）",
                Severity.WARNING,
                ReviewPriority.HIGH,
                groups,
            )
        elif item.kind is DuplicateKind.CONTAINED_DIFFERENT_LABEL:
            yield from emit_pair(
                ctx,
                DIFFERENT_LABEL,
                item,
                f"別ラベルの領域に包含されている"
                f"（包含率 {item.max_containment:.4f}、IoU {item.pair.iou:.4f}）"
                "。入れ子の所見として正当な可能性がある",
                Severity.INFO,
                ReviewPriority.NORMAL,
                groups,
            )
