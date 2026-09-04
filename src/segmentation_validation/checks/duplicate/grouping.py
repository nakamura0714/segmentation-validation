"""重複ペアの分類と、グループ化。

重複は「形（幾何）」と「ラベル」の2軸で分ける。
「IoUが高い → 全部duplicate」という雑な判定を避けるため。

============================ ============ =====================================
幾何                          ラベル       分類
============================ ============ =====================================
pixel完全一致                 同じ         強い重複違反候補（自動採否できる）
pixel完全一致                 異なる       semantic conflict（自動採否は禁止）
IoU >= 閾値                   同じ         near duplicate（目視）
IoU >= 閾値                   異なる       別所見の重なり（目視）
containment >= 閾値           同じ         包含された重複候補（目視）
containment >= 閾値           異なる       入れ子の所見の可能性（原則目視）
============================ ============ =====================================

``is_latest`` は**検出条件には使わない**（両方 1 でも存在し得るし、実データでは
brush が全件 1）。severity の決定にだけ使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Sequence

from ...config import Config
from ...core.measure import PairMeasurement

GROUP_PREFIX = "DUP"


class DuplicateKind(StrEnum):
    EXACT_SAME_LABEL = "exact_same_label"
    EXACT_DIFFERENT_LABEL = "exact_different_label"
    NEAR_SAME_LABEL = "near_same_label"
    NEAR_DIFFERENT_LABEL = "near_different_label"
    CONTAINED_SAME_LABEL = "contained_same_label"
    CONTAINED_DIFFERENT_LABEL = "contained_different_label"


@dataclass(frozen=True)
class ClassifiedPair:
    pair: PairMeasurement
    kind: DuplicateKind
    max_containment: float

    @property
    def uids(self) -> tuple[str, str]:
        return (self.pair.uid_a, self.pair.uid_b)


def classify(pairs: Iterable[PairMeasurement], config: Config) -> list[ClassifiedPair]:
    """ペアを6分類する。どれにも当たらないものは返さない。"""
    iou_threshold = config.thresholds.duplicate_iou_near
    containment_threshold = config.thresholds.duplicate_containment

    result: list[ClassifiedPair] = []
    for pair in pairs:
        max_containment = max(pair.containment_a_in_b, pair.containment_b_in_a)
        same = pair.same_label_keys

        if pair.pixel_identical or pair.iou >= 1.0:
            kind = (
                DuplicateKind.EXACT_SAME_LABEL
                if same
                else DuplicateKind.EXACT_DIFFERENT_LABEL
            )
        elif pair.iou >= iou_threshold:
            kind = (
                DuplicateKind.NEAR_SAME_LABEL
                if same
                else DuplicateKind.NEAR_DIFFERENT_LABEL
            )
        elif max_containment >= containment_threshold:
            kind = (
                DuplicateKind.CONTAINED_SAME_LABEL
                if same
                else DuplicateKind.CONTAINED_DIFFERENT_LABEL
            )
        else:
            continue
        result.append(ClassifiedPair(pair, kind, max_containment))
    return result


def build_groups(pairs: Sequence[ClassifiedPair]) -> dict[str, str]:
    """重複ペアを連結成分として束ね、``geometry_uid -> group_id`` を返す。

    ``A≒B``, ``B≒C`` なら A/B/C を1グループにする。実データでもサイズ3の
    グループが3件あるので、ペア単位だけでは足りない。
    """
    parent: dict[str, str] = {}

    def find(uid: str) -> str:
        parent.setdefault(uid, uid)
        while parent[uid] != uid:
            parent[uid] = parent[parent[uid]]
            uid = parent[uid]
        return uid

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for classified in pairs:
        union(*classified.uids)

    # 出現順にグループ番号を振る（実行ごとに同じIDになるように）。
    numbers: dict[str, str] = {}
    assignment: dict[str, str] = {}
    for classified in pairs:
        for uid in classified.uids:
            root = find(uid)
            if root not in numbers:
                numbers[root] = f"{GROUP_PREFIX}_{len(numbers) + 1:04d}"
            assignment[uid] = numbers[root]
    return assignment


def group_members(assignment: dict[str, str]) -> dict[str, list[str]]:
    members: dict[str, list[str]] = {}
    for uid, group_id in assignment.items():
        members.setdefault(group_id, []).append(uid)
    return {key: sorted(value) for key, value in members.items()}
