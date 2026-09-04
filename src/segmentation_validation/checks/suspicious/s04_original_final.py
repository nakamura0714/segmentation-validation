"""S04 修正前マスクと修正後マスクの食い違い。

社内確認の結果:

    path_mask          = 修正後 = 開発データとして使用
    path_original_mask = Annotation Tool で作成された修正前

修正例は「閉曲線になっていない」「消しゴムの消し残り」。
つまり ``path_original_mask`` が**存在すること自体は正常**（1568/1665 = 94%）。
存在するもの全部を挙げても成果物にならないので、
**穴埋めした original が final と食い違うものだけ**を確認対象にする。

「original と違う → error」とはしない。修正が正常に行われた結果である
可能性が高いので review_required に留める。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_ORIGINAL
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ..machine._common import for_role, readable

CHECK_ID = "S04_ORIGINAL_FINAL_DIVERGENCE"
CATEGORY = Category.SUSPICIOUS
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "修正前後の食い違い"
DESCRIPTION = "original を穴埋めした結果が final と一致しないものだけを拾う。"

DIVERGES = "S04_ORIGINAL_FINAL_DIVERGENCE"

# 再エンコード誤差との線引き。完全一致でなければ拾うが、
# 差がごく僅かなものは severity を落として目視の順序を下げる。
NEGLIGIBLE_RATIO = 0.001


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record, measurement in for_role(ctx, ROLE_ORIGINAL):
        if not readable(measurement) or measurement.filled_iou_vs_mask is None:
            continue
        iou = measurement.filled_iou_vs_mask
        if iou >= 1.0:
            continue

        ratio = measurement.difference_ratio or 0.0
        negligible = ratio < NEGLIGIBLE_RATIO
        yield ctx.issue(
            DIVERGES,
            record,
            f"修正前を穴埋めした結果が修正後と一致しない: IoU={iou:.5f} "
            f"差分 {measurement.difference_pixels}px ({ratio:.5f})"
            + ("（再エンコード誤差の範囲）" if negligible else ""),
            category=CATEGORY,
            severity=Severity.INFO if negligible else Severity.WARNING,
            review_priority=ReviewPriority.NORMAL
            if negligible
            else ReviewPriority.HIGH,
            filled_iou=iou,
            difference_pixels=measurement.difference_pixels,
            difference_ratio=ratio,
            original_pixels=measurement.fg_pixels,
            filled_pixels=measurement.filled_pixels,
        )
