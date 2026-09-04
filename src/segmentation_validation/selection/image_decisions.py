"""画像単位の採否。

``selection_decisions`` は annotation 単位（1 annotation = 1行）なので、
**annotation を持たない画像は1行も持たない**。しかし
「この画像を開発データに含めるか」は annotation とは別の判断が要る:

- 正常例（No Findings）187枚 —— 陰性サンプルとして**残す**
- 未アノテーションのビュー 33枚 —— **側面像なら除外する必要がある**ので目視で判断

そこで画像単位の採否を別ファイル（``image_decisions.csv``）で持つ。
annotation 単位の不変条件（1817行）を壊さずに、画像の採否を明示できる。

``build-dataset`` は両方を見る。画像が ``exclude`` なら file entry ごと落とす。
これは「annotation が0件になっても file entry は残す」規則とは別で、
**画像を落とすという明示的な判断があったときだけ**母集団を変える。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from ..checks.base import IMAGE_CLASS_JA, ImageClass, Issue, classify_image
from ..config import Config
from ..core.records import FileGroup
from .decisions import (
    Decision,
    DecisionSource,
    HumanDecision,
    ReviewStatus,
    effective_review_required,
)

NONE = "none"


class ImageReason(StrEnum):
    HAS_ANNOTATIONS = "has_annotations"
    # 正常例。陰性サンプルとして残す。
    NEGATIVE_CASE = "negative_case"
    # 未アノテーションのビュー。側面像かどうか目視で判断する。
    REVIEW_REQUIRED = "review_required"
    # 検証対象外（分類はできたが目視に回さない設定）
    KEPT_WITHOUT_REVIEW = "kept_without_review"


@dataclass(frozen=True)
class ImageDecision:
    """画像1枚の採否。``file_uid`` が主キー。"""

    dataset_id: str
    source_json: str
    institution: str
    patient_id: str
    study: str
    series: str
    file_id: str
    file_uid: str
    image_path: str
    image_class: str
    image_class_ja: str
    n_annotations: int
    case_labels: str
    detected_checks: str
    review_required: bool
    review_status: ReviewStatus
    final_decision: Decision
    reason: str
    decision_source: DecisionSource
    reviewer: str | None = None
    reviewed_at: str | None = None
    comment: str | None = None


COLUMNS: tuple[str, ...] = tuple(
    f
    for f in (
        "dataset_id",
        "source_json",
        "institution",
        "patient_id",
        "study",
        "series",
        "file_id",
        "file_uid",
        "image_path",
        "image_class",
        "image_class_ja",
        "n_annotations",
        "case_labels",
        "detected_checks",
        "review_required",
        "review_status",
        "final_decision",
        "reason",
        "decision_source",
        "reviewer",
        "reviewed_at",
        "comment",
    )
)


def build_image_decisions(
    groups: Sequence[FileGroup],
    issues: Iterable[Issue],
    config: Config,
    human: Mapping[str, HumanDecision] | None = None,
) -> list[ImageDecision]:
    """全画像の採否を決める。**1画像 = 1行**、全画像が必ず1行持つ。

    優先順位:

    1. human decision がある → それ
    2. annotation を持つ → keep（annotation 側で個別に判定済み）
    3. 目視対象の画像レベル Issue がある → pending
    4. 正常例 → keep（陰性サンプル）
    5. それ以外 → keep（目視に回さない設定のもの）
    """
    human = human or {}
    annotated_series = {(g.dataset_id, g.study, g.series) for g in groups if g.records}
    review_classes = frozenset(config.decision_policy.review_image_classes)

    # 画像単位の Issue（geometry_uid が None）を file_uid でまとめる
    by_file: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.geometry_uid is None:
            by_file.setdefault(issue.file_uid, []).append(issue)

    decisions: list[ImageDecision] = []
    for group in groups:
        key = (group.dataset_id, group.study, group.series)
        image_class = classify_image(group, key in annotated_series)
        found = by_file.get(group.file_uid, [])
        detected = [
            i.check_id
            for i in found
            if effective_review_required(i.check_id, i.status, config)
        ]
        needs_review = bool(detected) and image_class.value in review_classes

        verdict = human.get(group.file_uid)
        if verdict is not None:
            decision = verdict.decision
            reason = verdict.reason or decision.value
            source, status = DecisionSource.HUMAN, ReviewStatus.REVIEWED
        elif group.records:
            decision = Decision.KEEP
            reason = ImageReason.HAS_ANNOTATIONS.value
            source, status = DecisionSource.DEFAULT, ReviewStatus.NOT_NEEDED
        elif needs_review:
            decision = Decision.PENDING
            reason = ImageReason.REVIEW_REQUIRED.value
            source, status = DecisionSource.NONE, ReviewStatus.PENDING
        elif image_class is ImageClass.NEGATIVE_CASE:
            decision = Decision.KEEP
            reason = ImageReason.NEGATIVE_CASE.value
            source, status = DecisionSource.DEFAULT, ReviewStatus.NOT_NEEDED
        else:
            decision = Decision.KEEP
            reason = ImageReason.KEPT_WITHOUT_REVIEW.value
            source, status = DecisionSource.DEFAULT, ReviewStatus.NOT_NEEDED

        decisions.append(
            ImageDecision(
                dataset_id=group.dataset_id,
                source_json=group.source_json,
                institution=group.institution,
                patient_id=group.patient_id,
                study=group.study,
                series=group.series,
                file_id=group.file,
                file_uid=group.file_uid,
                image_path=group.image_path,
                image_class=image_class.value,
                image_class_ja=IMAGE_CLASS_JA[image_class],
                n_annotations=len(group.records),
                case_labels="|".join(
                    label.qualified_code for label in group.case_labels
                )
                or NONE,
                detected_checks="|".join(dict.fromkeys(detected)) or NONE,
                review_required=needs_review,
                review_status=status,
                final_decision=decision,
                reason=reason,
                decision_source=source,
                reviewer=verdict.reviewer if verdict else None,
                reviewed_at=verdict.reviewed_at if verdict else None,
                comment=verdict.comment if verdict else None,
            )
        )
    return decisions


def summarize_images(decisions: Sequence[ImageDecision]) -> dict[str, Any]:
    by_class: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    for d in decisions:
        by_class[d.image_class] = by_class.get(d.image_class, 0) + 1
        by_decision[d.final_decision.value] = (
            by_decision.get(d.final_decision.value, 0) + 1
        )
        by_reason[d.reason] = by_reason.get(d.reason, 0) + 1
    pending = by_decision.get(Decision.PENDING.value, 0)
    return {
        "total": len(decisions),
        "by_class": by_class,
        "by_decision": by_decision,
        "by_reason": by_reason,
        "pending": pending,
        "uncertain": by_decision.get(Decision.UNCERTAIN.value, 0),
        "excluded": by_decision.get(Decision.EXCLUDE.value, 0),
    }


def to_row(decision: ImageDecision) -> dict[str, Any]:
    data = asdict(decision)
    for key in ("review_status", "final_decision", "decision_source"):
        data[key] = getattr(decision, key).value
    return {
        column: ("" if data[column] is None else data[column]) for column in COLUMNS
    }


def assert_image_invariants(
    decisions: Sequence[ImageDecision], groups: Sequence[FileGroup]
) -> None:
    """1画像 = 1行。全画像が必ず1行持つ。"""
    if len(decisions) != len(groups):
        raise AssertionError(
            f"image_decisions の行数 {len(decisions)} が "
            f"画像数 {len(groups)} と一致しない"
        )
    uids = {d.file_uid for d in decisions}
    if len(uids) != len(decisions):
        raise AssertionError("image_decisions に file_uid の重複がある")
    if uids != {g.file_uid for g in groups}:
        raise AssertionError("image_decisions と画像の file_uid が一致しない")
