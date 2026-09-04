"""M03 背景が0・前景が255か。

**二値化前**の生画素値で判定する。二値化してしまうと中間値の存在が消える。
``core.imageio.load_mask_raw`` が生の値域を保持しているのはこのため。

``path_original_mask`` は**対象外**。alpha が連続値なのがブラシの仕様であり
（実測1568件中1459件が中間値を持つ）、これを issue にすると本物の検出が埋まる。
修正前後の食い違いは S04 が別の観点で見る。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._common import for_role, readable

CHECK_ID = "M03_MASK_BINARY"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "背景0・前景255の二値か"
DESCRIPTION = "path_mask の二値化前の画素値が {0,255} だけかを見る。反転も検出する。"

NOT_BINARY = "M03_MASK_NOT_BINARY"
INVERTED = "M03_MASK_INVERTED"

EXPECTED_VALUES = {0, 255}
# 前景が画像の大半を占め、かつ最小値が0でないなら前景/背景が逆の可能性がある。
INVERSION_RATIO = 0.5


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement) or measurement.n_unique_values is None:
            continue

        values = set(measurement.unique_values)
        if values - EXPECTED_VALUES or measurement.unique_values_truncated:
            ellipsis = "…" if measurement.unique_values_truncated else ""
            yield ctx.issue(
                NOT_BINARY,
                record,
                f"0/255 以外の画素値がある: {sorted(values)[:8]}{ellipsis}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.HIGH,
                unique_values=list(measurement.unique_values),
                n_unique_values=measurement.n_unique_values,
                value_min=measurement.value_min,
                value_max=measurement.value_max,
            )

        if (
            measurement.value_min not in (None, 0)
            and (measurement.fg_ratio or 0) > INVERSION_RATIO
        ):
            yield ctx.issue(
                INVERTED,
                record,
                f"前景と背景が逆の可能性がある: 最小値={measurement.value_min} "
                f"前景率={measurement.fg_ratio:.2f}",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.HIGH,
                value_min=measurement.value_min,
                fg_ratio=measurement.fg_ratio,
            )
