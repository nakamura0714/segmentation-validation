"""M09 アノテーションを持たない画像。

``file_list`` には annotation を持たない画像も含まれる（実測 1083枚中220枚）。
その正体は2種類あり、**扱いがまったく違う**。

- **正常例（No Findings）** 187枚
  study 全体がアノテーションなしで、study レベルに ``No Findings/001 normal``
  が付いている。DICOM も実在する意図的な陰性症例。検証すべきマスクが無いだけで、
  開発データの陰性サンプルになる
- **未アノテーションのビュー** 33枚
  アノテーション済み study の2枚目以降で、分類ラベルも持たない。
  **側面像なら開発データから除外する必要がある**ので目視で判断する。
  実測では ``nagoya_daiichi`` の series の66%（41中27）がこの形で、
  施設の撮影習慣に見える

このチェックは **annotation ではなく画像**について報告する（``geometry_uid`` は None）。
採否も画像単位（``selection.image_decisions``）で持つ。
"""

from __future__ import annotations

from typing import Iterator

from ..base import (
    Category,
    CheckContext,
    CheckStatus,
    ImageClass,
    Issue,
    ReviewPriority,
    Severity,
    classify_image,
)

CHECK_ID = "M09_UNANNOTATED_VIEW"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "アノテーションを持たない画像"
DESCRIPTION = (
    "annotation の無い画像を、正常例（陰性症例）と未アノテーションのビューに分ける。"
    "後者は側面像なら除外が必要なので目視で判断する。"
)

UNANNOTATED = "M09_UNANNOTATED_VIEW"
ORPHAN = "M09_UNANNOTATED_SERIES"
NEGATIVE = "M09_NEGATIVE_CASE"


def run(ctx: CheckContext) -> Iterator[Issue]:
    # 同じ series の他の画像にアノテーションがあるかを先に集める。
    annotated_series: set[tuple[str, str, str]] = {
        (g.dataset_id, g.study, g.series) for g in ctx.groups if g.records
    }

    for group in ctx.groups:
        key = (group.dataset_id, group.study, group.series)
        image_class = classify_image(group, key in annotated_series)

        if image_class is ImageClass.ANNOTATED:
            continue

        if image_class is ImageClass.NEGATIVE_CASE:
            # 異常ではない。開発データの陰性サンプルとして残す判断の材料。
            yield ctx.file_issue(
                NEGATIVE,
                group,
                "annotation が無く、正常例（No Findings）として明示されている"
                "（意図的な陰性症例）",
                category=CATEGORY,
                severity=Severity.INFO,
                status=CheckStatus.NOT_APPLICABLE,
                review_priority=ReviewPriority.LOW,
                image_class=image_class.value,
                case_labels=[label.qualified_code for label in group.case_labels],
            )
            continue

        if image_class is ImageClass.UNANNOTATED_VIEW:
            yield ctx.file_issue(
                UNANNOTATED,
                group,
                "同じ series の他の画像にはアノテーションがあるが、この画像には無い"
                "（側面像などなら開発データから除外する必要がある）",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.HIGH,
                image_class=image_class.value,
                file_index=group.file.split("_")[-1],
            )
        else:
            # series 全体が未アノテーションで、正常例ラベルも無い。
            # 分類も判断もできないので目視へ回す。
            yield ctx.file_issue(
                ORPHAN,
                group,
                "series 全体にアノテーションが無く、正常例のラベルも無い",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.HIGH,
                image_class=image_class.value,
            )
