"""M07 bbox / elliptical の座標健全性。

マスクを持たない annotation（``annotation_type`` が brush 以外）は、
形状の検査対象にできない代わりに、JSON上の座標だけは健全性を確認できる。

- ``min_x < max_x`` かつ ``min_y < max_y``
- bbox が画像の範囲内に収まっている

実測では degenerate（min >= max）が7件、範囲外が4件ある。
brush と同じ土俵で扱わず別枠にしてあるのは、これらが開発データの
セグメンテーション品質とは別の問題だから。件数はレポートに出す。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, CheckStatus, Issue, ReviewPriority, Severity

CHECK_ID = "M07_BBOX_GEOMETRY"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "bbox/elliptical の座標健全性"
DESCRIPTION = (
    "マスクを持たない annotation について min<max と画像範囲内であることを検査する。"
)

DEGENERATE = "M07_BBOX_DEGENERATE"
OUT_OF_IMAGE = "M07_BBOX_OUT_OF_IMAGE"
SIZE_FIELDS_NULL = "M07_SIZE_FIELDS_NULL"

MASK_TYPE = "brush"


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record in ctx.records:
        if record.annotation_type == MASK_TYPE:
            continue

        min_x, min_y, max_x, max_y = record.json_bbox
        if min_x >= max_x or min_y >= max_y:
            yield ctx.issue(
                DEGENERATE,
                record,
                f"bbox が退化している: ({min_x},{min_y})-({max_x},{max_y})",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.HIGH,
                bbox=[min_x, min_y, max_x, max_y],
                width=max_x - min_x,
                height=max_y - min_y,
            )

        shape = record.series_shape
        if shape is None:
            yield ctx.issue(
                OUT_OF_IMAGE,
                record,
                "series.shape が無いので bbox が画像内かを判定できない",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.CANNOT_DETERMINE,
                review_priority=ReviewPriority.LOW,
                cannot_determine_reason="no_series_shape",
                bbox=[min_x, min_y, max_x, max_y],
            )
        else:
            height, width = shape
            if min_x < 0 or min_y < 0 or max_x > width or max_y > height:
                yield ctx.issue(
                    OUT_OF_IMAGE,
                    record,
                    f"bbox が画像範囲 {width}x{height} をはみ出す: "
                    f"({min_x},{min_y})-({max_x},{max_y})",
                    category=CATEGORY,
                    severity=Severity.ERROR,
                    review_priority=ReviewPriority.HIGH,
                    bbox=[min_x, min_y, max_x, max_y],
                    image_width=width,
                    image_height=height,
                )

        # width/height は brush にしか入っていない。異常ではないので記録のみ。
        if record.declared_size is None:
            yield ctx.issue(
                SIZE_FIELDS_NULL,
                record,
                "width/height が null（マスクを持たない annotation では通常）",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.NOT_APPLICABLE,
                review_priority=ReviewPriority.LOW,
            )
