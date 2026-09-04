"""機械が確定させられる採否。

現時点で自動化できるのは **D01（完全一致・同一ラベル）だけ**。
社内確認の結果:

    データが完全一致なのでどちらでも学習上の影響はない。
    採用基準としては日付が新しい方を使うのが自然。

時系列を表すフィールドは ``timestamp`` しかないので、これで順序を決める。
``geometry_id`` の大小は使わない（実データでは timestamp と一致するが、
一致する保証はどこにも無い）。``is_latest`` も候補の絞り込みには使わない。

``timestamp`` が同値・欠損・解釈不能なら**自動決定しない**。
現データでは1件も該当しないが、再エクスポートや別データセットでは起こり得る。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Mapping, Sequence

from ..checks.duplicate.grouping import (
    DuplicateKind,
    build_groups,
    classify,
    group_members,
)
from ..config import Config
from ..core.measure import PairMeasurement
from ..core.records import AnnotationRecord
from .decisions import AutomaticDecision, Decision, Reason

logger = logging.getLogger(__name__)

#: 自動決定できなかった理由。目視へ回す根拠として記録する。
UNDECIDABLE_TIE = "timestamp_tie"
UNDECIDABLE_MISSING = "timestamp_missing"


def build_automatic_decisions(
    records: Sequence[AnnotationRecord],
    pairs: Sequence[PairMeasurement],
    config: Config,
) -> tuple[dict[str, AutomaticDecision], dict[str, str]]:
    """D01 の重複グループについて自動採否を作る。

    戻り値は ``(geometry_uid -> 決定, geometry_uid -> 自動決定できなかった理由)``。
    後者は ``select`` が目視へ回すために使う。
    """
    by_uid = {record.geometry_uid: record for record in records}
    classified = classify(pairs, config)
    exact = [item for item in classified if item.kind is DuplicateKind.EXACT_SAME_LABEL]
    if not exact:
        return {}, {}

    # グループIDは全分類で共通に振る（レポートの一貫性のため）。
    all_groups = build_groups(classified)
    # 自動採否の対象は完全一致だけで束ね直す。
    exact_groups = build_groups(exact)
    members = group_members(exact_groups)

    decisions: dict[str, AutomaticDecision] = {}
    undecidable: dict[str, str] = {}

    for uids in members.values():
        group_id = all_groups.get(uids[0])
        stamps: list[tuple[datetime | None, str]] = []
        for uid in uids:
            record = by_uid.get(uid)
            stamps.append((record.parsed_timestamp if record else None, uid))

        if any(stamp is None for stamp, _ in stamps):
            reason = UNDECIDABLE_MISSING
        elif len({stamp for stamp, _ in stamps}) < len(stamps):
            # 最新が複数あると「新しい方を残す」が一意に決まらない。
            reason = UNDECIDABLE_TIE
        else:
            reason = ""

        if reason:
            for uid in uids:
                undecidable[uid] = reason
            logger.debug("自動決定できない重複グループ %s: %s", group_id, reason)
            continue

        stamps.sort(key=lambda item: item[0])  # type: ignore[arg-type,return-value]
        kept = stamps[-1][1]
        for _, uid in stamps[:-1]:
            decisions[uid] = AutomaticDecision(
                geometry_uid=uid,
                decision=Decision.EXCLUDE,
                reason=Reason.OLDER_EXACT_DUPLICATE.value,
                kept_geometry_uid=kept,
                related_geometry_uid=kept,
                duplicate_group_id=group_id,
                detail={"group_size": len(uids)},
            )
        decisions[kept] = AutomaticDecision(
            geometry_uid=kept,
            decision=Decision.KEEP,
            reason="newest_of_exact_duplicate_group",
            kept_geometry_uid=kept,
            duplicate_group_id=group_id,
            detail={"group_size": len(uids)},
        )

    return decisions, undecidable


def summarize_automatic(
    decisions: Mapping[str, AutomaticDecision], undecidable: Mapping[str, str]
) -> dict[str, int]:
    excluded = sum(1 for d in decisions.values() if d.decision is Decision.EXCLUDE)
    kept = sum(1 for d in decisions.values() if d.decision is Decision.KEEP)
    return {
        "automatic_exclude": excluded,
        "automatic_keep": kept,
        "undecidable": len(undecidable),
        "duplicate_groups": kept,
    }
