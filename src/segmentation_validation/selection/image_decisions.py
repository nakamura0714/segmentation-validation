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

import json
import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
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

logger = logging.getLogger(__name__)

NONE = "none"


class ImageReason(StrEnum):
    HAS_ANNOTATIONS = "has_annotations"
    # 正常例。陰性サンプルとして残す。
    NEGATIVE_CASE = "negative_case"
    # 未アノテーションのビュー。側面像かどうか目視で判断する。
    REVIEW_REQUIRED = "review_required"
    # series全体にアノテーションが無く、正常例のラベルも無い（単独型）。
    # 所見ラベルの手掛かりが一切無い候補プール画像で、1枚ずつ目視するのは
    # 非現実的な件数になる（実測1万件超）。開発データには使わない方針。
    UNANNOTATED_ORPHAN_UNUSED = "unannotated_orphan_unused"
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
    # データ管理者承認による一括 exclude→keep（override）の監査情報。
    # override が無ければ全て None。``reason`` はoverride適用後も一切変更しない
    # ―― 「元々なぜexclude/keepだったか」を常に辿れるようにするため。
    override_id: str | None = None
    override_note: str | None = None
    override_approved_by: str | None = None
    override_approved_at: str | None = None
    # series内でのこのファイルの位置（0始まり）と series 内の総ファイル数。
    # UNANNOTATED_VIEW / UNANNOTATED_ORPHAN の区別だけでなく「series内の何枚目か」
    # まで条件指定できるようにするため（FileGroup.series_image_index と同じ値）。
    # 既定値を持たせてあるのは、この変更より前に書かれた image_decisions.json
    # （``series_image_index`` キーを持たない）を ``ImageDecision(**data)`` で
    # 読み戻しても壊れないようにするため（cli.py の ``_read_image_decisions``）。
    series_image_index: int = 0
    series_image_count: int = 1


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
        "series_image_index",
        "series_image_count",
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
        "override_id",
        "override_note",
        "override_approved_by",
        "override_approved_at",
    )
)


@dataclass(frozen=True)
class ImageDecisionOverride:
    """データ管理者が承認した「条件付き exclude→keep」の1規則。

    ``review_policy/image_decision_overrides.json`` から読む。
    **機械の自動判定（``DecisionSource.DEFAULT``）にしか適用しない**——
    人間が明示的に exclude と判断したものを条件一致だけで黙って
    上書きしないための安全側ガードは ``build_image_decisions`` 側で行う。
    """

    id: str
    # キーは "dataset_id" / "auto_decision_reason" / "image_class" /
    # "series_image_index"。値が None のキーはワイルドカード（絞り込まない）。
    # 値はリストで、含まれていれば一致（OR）。複数キーはAND条件。
    match: Mapping[str, tuple[Any, ...] | None]
    action: str
    approved_by: str
    approved_at: str
    note: str = ""

    def matches(
        self, *, dataset_id: str, reason: str, image_class: str, series_image_index: int
    ) -> bool:
        candidates: dict[str, Any] = {
            "dataset_id": dataset_id,
            "auto_decision_reason": reason,
            "image_class": image_class,
            "series_image_index": series_image_index,
        }
        for key, allowed in self.match.items():
            if allowed is None:
                continue
            if candidates.get(key) not in allowed:
                return False
        return True


def load_image_decision_overrides(path: Path) -> list[ImageDecisionOverride]:
    """承認済みの一括ルールを読む。ファイルが無ければ空（何も上書きしない）。"""
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.error("%s を読めない。overrideは適用しない: %s", path, error)
        return []

    overrides: list[ImageDecisionOverride] = []
    for raw in payload.get("overrides", []):
        match: dict[str, tuple[Any, ...] | None] = {}
        for key, value in (raw.get("match") or {}).items():
            if value is None:
                match[key] = None
            elif isinstance(value, list):
                match[key] = tuple(value)
            else:
                match[key] = (value,)
        overrides.append(
            ImageDecisionOverride(
                id=raw["id"],
                match=match,
                action=raw.get("action", "keep"),
                approved_by=raw.get("approved_by", ""),
                approved_at=raw.get("approved_at", ""),
                note=raw.get("note", ""),
            )
        )
    return overrides


def _find_matching_override(
    overrides: Sequence[ImageDecisionOverride],
    *,
    dataset_id: str,
    reason: str,
    image_class: str,
    series_image_index: int,
) -> ImageDecisionOverride | None:
    for override in overrides:
        if override.action != "keep":
            continue
        if override.matches(
            dataset_id=dataset_id,
            reason=reason,
            image_class=image_class,
            series_image_index=series_image_index,
        ):
            return override
    return None


def build_image_decisions(
    groups: Sequence[FileGroup],
    issues: Iterable[Issue],
    config: Config,
    human: Mapping[str, HumanDecision] | None = None,
    overrides: Sequence[ImageDecisionOverride] | None = None,
) -> list[ImageDecision]:
    """全画像の採否を決める。**1画像 = 1行**、全画像が必ず1行持つ。

    優先順位:

    1. human decision がある → それ
    2. annotation を持つ → keep（annotation 側で個別に判定済み）
    3. 単独型の未アノテーション（series全体に所見の手掛かりが無い） → exclude
       （開発データには使わない方針。1万件超あり目視は非現実的）
    4. 目視対象の画像レベル Issue がある → pending
    5. 正常例 → keep（陰性サンプル）
    6. それ以外 → keep（目視に回さない設定のもの）

    上記で決まった結果が**機械の自動exclude（3番、``DecisionSource.DEFAULT``）**の
    ときだけ、``overrides``（データ管理者が承認した一括ルール）に一致すれば
    ``keep`` へ引き上げる。人間判定（1番）には絶対に適用しない。
    """
    human = human or {}
    overrides = overrides or []
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
        elif image_class is ImageClass.UNANNOTATED_ORPHAN:
            decision = Decision.EXCLUDE
            reason = ImageReason.UNANNOTATED_ORPHAN_UNUSED.value
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

        # 承認済みの一括ルールで、機械の自動exclude（DEFAULT）だけを引き上げる。
        # 人間判定（HUMAN）には絶対に適用しない（安全側）。
        # ``reason`` は変更しない —— 「元々なぜexcludeだったか」を監査用に残すため。
        override_id = override_note = override_approved_by = override_approved_at = None
        if decision is Decision.EXCLUDE and source is DecisionSource.DEFAULT:
            matched = _find_matching_override(
                overrides,
                dataset_id=group.dataset_id,
                reason=reason,
                image_class=image_class.value,
                series_image_index=group.series_image_index,
            )
            if matched is not None:
                decision = Decision.KEEP
                source = DecisionSource.OVERRIDE
                override_id = matched.id
                override_note = matched.note
                override_approved_by = matched.approved_by
                override_approved_at = matched.approved_at

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
                series_image_index=group.series_image_index,
                series_image_count=group.series_image_count,
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
                override_id=override_id,
                override_note=override_note,
                override_approved_by=override_approved_by,
                override_approved_at=override_approved_at,
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
