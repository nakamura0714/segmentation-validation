"""D01 完全一致の重複（同一ラベル）。

同一画像内の別の ``geometry_uid`` で、マスクが**画素単位で完全一致**し、
``(code_system, code)`` も同じもの。データ管理上の重複と考えてよい。

**唯一、自動で採否を決められるチェック**（`selection.automatic` が担当）。
社内確認の結果「データが完全一致なのでどちらでも学習上の影響はない。
採用基準としては日付が新しい方を使うのが自然」とのことなので、
``timestamp`` が新しい方を残す。``geometry_uid`` の大小は使わない。

``is_latest`` は検出条件に使わない（両方 1 でも存在し得る）が、
severity には使う。片方が履歴なら単なる版管理の痕跡なので INFO へ落とす。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._emit import emit_pair
from .grouping import DuplicateKind, build_groups, classify

CHECK_ID = "D01_EXACT_DUPLICATE"
CATEGORY = Category.DUPLICATE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "完全一致の重複（同一ラベル）"
DESCRIPTION = "画素完全一致かつ同一ラベル。timestamp が新しい方を自動で残せる。"


def run(ctx: CheckContext) -> Iterator[Issue]:
    classified = classify(ctx.pairs, ctx.config)
    groups = build_groups(classified)
    for item in classified:
        if item.kind is not DuplicateKind.EXACT_SAME_LABEL:
            continue
        # 両方が最新なら実質的な二重登録。片方が履歴なら版管理の痕跡。
        severity = Severity.ERROR if item.pair.both_latest else Severity.INFO
        yield from emit_pair(
            ctx,
            CHECK_ID,
            item,
            "同一画像内でマスクが画素単位で完全一致し、ラベルも同一"
            + ("" if item.pair.both_latest else "（片方は is_latest=0 の履歴）"),
            severity,
            ReviewPriority.HIGH,
            groups,
        )
