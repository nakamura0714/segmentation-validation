"""M08 JSONの宣言値と実マスクの一致。

``min_x/min_y/max_x/max_y`` と ``region_count`` は、現データでは
1665/1665 で実マスクと一致している。だからこそ**回帰検知として残す** ——
次のエクスポートで壊れたときに気付ける。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._common import for_role, readable

CHECK_ID = "M08_JSON_MASK_CONSISTENCY"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "JSONのbbox/region_countと実マスクの一致"
DESCRIPTION = "現状は全件一致。壊れたときに気付くための回帰検知。"

BBOX_MISMATCH = "M08_JSON_BBOX_MISMATCH"
REGION_COUNT_MISMATCH = "M08_REGION_COUNT_MISMATCH"


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement) or measurement.bbox is None:
            continue

        actual = tuple(measurement.bbox)
        if actual != tuple(record.json_bbox):
            yield ctx.issue(
                BBOX_MISMATCH,
                record,
                f"JSONのbbox {tuple(record.json_bbox)} と実マスク {actual} が不一致",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.NORMAL,
                json_bbox=list(record.json_bbox),
                mask_bbox=list(actual),
            )

        if (
            record.region_count is not None
            and measurement.n_components is not None
            and record.region_count != measurement.n_components
        ):
            yield ctx.issue(
                REGION_COUNT_MISMATCH,
                record,
                f"region_count {record.region_count} と連結成分数 "
                f"{measurement.n_components} が不一致",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.NORMAL,
                region_count=record.region_count,
                n_components=measurement.n_components,
            )
