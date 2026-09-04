"""D02 完全一致だがラベルが違う（semantic conflict）。

マスクが画素単位で一致するのに ``(code_system, code)`` が異なる。
これは「重複」ではなく**同一のgeometryに別のsemantic labelが付いている**問題で、
どちらが正しいかはデータからは決まらない。**自動exclude禁止**、高優先度で目視。

実データでは0件。次のエクスポートで発生したら気付くための回帰検知として置く。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._emit import emit_pair
from .grouping import DuplicateKind, build_groups, classify

CHECK_ID = "D02_EXACT_MASK_LABEL_CONFLICT"
CATEGORY = Category.DUPLICATE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "完全一致だがラベルが異なる"
DESCRIPTION = "同一geometryに別のsemantic labelが付いている。自動採否は禁止。"


def run(ctx: CheckContext) -> Iterator[Issue]:
    classified = classify(ctx.pairs, ctx.config)
    groups = build_groups(classified)
    for item in classified:
        if item.kind is not DuplicateKind.EXACT_DIFFERENT_LABEL:
            continue
        yield from emit_pair(
            ctx,
            CHECK_ID,
            item,
            "マスクは完全一致だがラベルが異なる（同一geometryへの別ラベル付与）",
            Severity.ERROR,
            ReviewPriority.CRITICAL,
            groups,
        )
