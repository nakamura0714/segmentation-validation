"""M02 マスクが8bit単チャンネルか。

``path_mask`` は全件が mode=L の単チャンネル。RGB/RGBA が混ざっていれば
学習側の前処理が壊れるので error。

``path_original_mask`` の RGBA は**仕様上正常**（ブラシのアンチエイリアスが
alpha に入る。実測 1568件中 1462件）。alpha があれば描画領域が一意に決まるので、
これを issue として出すと1400件超のノイズになり本物の検出が埋まる。
original 側で拾うのは**二値化が曖昧になる形**（alphaの無い多チャンネル）だけ。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK, ROLE_ORIGINAL
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._common import for_role, readable

CHECK_ID = "M02_MASK_CHANNELS"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "マスクが8bit単チャンネルか"
DESCRIPTION = "二値化前の mode / 次元数 / dtype を見る。original の RGBA は正常。"

NOT_GRAYSCALE = "M02_MASK_NOT_GRAYSCALE"
NOT_8BIT = "M02_MASK_NOT_8BIT"
AMBIGUOUS_ORIGINAL = "M02_ORIGINAL_AMBIGUOUS_CHANNELS"

EXPECTED_NDIM = 2
EXPECTED_DTYPE = "uint8"
# alpha を持つ形。ここから描画領域が一意に決まる。
ALPHA_CHANNELS = (2, 4)


def run(ctx: CheckContext) -> Iterator[Issue]:
    yield from _check_mask(ctx)
    yield from _check_original(ctx)


def _check_mask(ctx: CheckContext) -> Iterator[Issue]:
    """``path_mask`` は単チャンネル8bit以外を許さない。"""
    for record, measurement in for_role(ctx, ROLE_MASK):
        if not readable(measurement):
            continue

        if measurement.ndim != EXPECTED_NDIM:
            yield ctx.issue(
                NOT_GRAYSCALE,
                record,
                f"単チャンネルでない: mode={measurement.pil_mode} "
                f"channels={measurement.n_channels}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.HIGH,
                role=ROLE_MASK,
                pil_mode=measurement.pil_mode,
                n_channels=measurement.n_channels,
                binarized_from=measurement.binarized_from,
            )

        if measurement.dtype != EXPECTED_DTYPE:
            yield ctx.issue(
                NOT_8BIT,
                record,
                f"8bitでない: dtype={measurement.dtype}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.HIGH,
                role=ROLE_MASK,
                dtype=measurement.dtype,
            )


def _check_original(ctx: CheckContext) -> Iterator[Issue]:
    """``path_original_mask`` は二値化が曖昧になる形だけを拾う。"""
    for record, measurement in for_role(ctx, ROLE_ORIGINAL):
        if not readable(measurement):
            continue
        if (
            measurement.ndim == EXPECTED_NDIM
            or measurement.n_channels in ALPHA_CHANNELS
        ):
            continue

        yield ctx.issue(
            AMBIGUOUS_ORIGINAL,
            record,
            f"修正前マスクの二値化が曖昧: mode={measurement.pil_mode} "
            f"channels={measurement.n_channels}"
            "（alphaが無いため最大値チャンネルで代用している）",
            category=CATEGORY,
            severity=Severity.INFO,
            review_priority=ReviewPriority.NORMAL,
            role=ROLE_ORIGINAL,
            pil_mode=measurement.pil_mode,
            n_channels=measurement.n_channels,
            binarized_from=measurement.binarized_from,
        )
