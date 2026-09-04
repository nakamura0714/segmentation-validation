"""重複チェックが共有する Issue の組み立て。

ペアは本質的に2件の annotation の関係だが、成果物の粒度は annotation なので
**1ペアにつき2つの Issue** を出す。相手を ``related_geometry_uids`` に、
同じ ``duplicate_group_id`` で束ねる。こうすると annotation で絞っても
対応関係が消えず、group で畳めばペアの視点にも戻せる。
"""

from __future__ import annotations

from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from .grouping import ClassifiedPair


def emit_pair(
    ctx: CheckContext,
    check_id: str,
    classified: ClassifiedPair,
    message: str,
    severity: Severity,
    review_priority: ReviewPriority,
    groups: dict[str, str],
) -> Iterator[Issue]:
    pair = classified.pair
    for uid, other in ((pair.uid_a, pair.uid_b), (pair.uid_b, pair.uid_a)):
        record = getattr(ctx, "by_uid", {}).get(uid)
        if record is None:
            continue
        yield ctx.issue(
            check_id,
            record,
            message,
            category=Category.DUPLICATE,
            severity=severity,
            review_priority=review_priority,
            related_geometry_uids=(other,),
            duplicate_group_id=groups.get(uid),
            kind=classified.kind.value,
            iou=round(pair.iou, 6),
            containment=round(classified.max_containment, 6),
            intersection_pixels=pair.intersection_pixels,
            union_pixels=pair.union_pixels,
            pixel_identical=pair.pixel_identical,
            same_label_keys=pair.same_label_keys,
            # 検出条件には使わないが、絞り込みと severity 判定に残す。
            both_latest=pair.both_latest,
            same_user=pair.same_user,
            same_request=pair.same_request,
            partner_user=pair.user_b if uid == pair.uid_a else pair.user_a,
            partner_timestamp=(
                pair.timestamp_b if uid == pair.uid_a else pair.timestamp_a
            ),
        )
