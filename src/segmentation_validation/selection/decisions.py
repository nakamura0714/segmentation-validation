"""採否の正本（SelectionDecision）。

``issues.csv`` が「何を検出したか」なのに対し、こちらは
「最終的に開発データとして使うか」を全 annotation について1行ずつ持つ。
**Issueあり ≠ exclude** —— tiny region として検出されても目視で妥当なら keep になる。

不変条件: 行数 == AnnotationRecord の件数。Issue が一度も出なかった正常な
annotation も ``keep`` として必ず1行出す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from ..checks.base import CheckStatus, Issue, match_rank
from ..config import Config
from ..core.records import AnnotationRecord

NONE = "none"


class Decision(StrEnum):
    KEEP = "keep"
    EXCLUDE = "exclude"
    UNCERTAIN = "uncertain"
    PENDING = "pending"


class DecisionSource(StrEnum):
    AUTOMATIC = "automatic"
    HUMAN = "human"
    # Issue が無い、または判定不能のまま残したもの。人も機械も判断していない。
    DEFAULT = "default"
    NONE = "-"


class ReviewStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    REVIEWED = "reviewed"


class Reason(StrEnum):
    NO_ISSUE = "no_issue_detected"
    # 一部の check が判定不能のまま keep した（体外判定が未実施 等）。
    # 「検査して問題なし」と区別するために別 reason にする。
    KEPT_WITHOUT_FULL_CHECK = "kept_without_full_check"
    OLDER_EXACT_DUPLICATE = "older_exact_duplicate"
    REVIEW_REQUIRED = "review_required"


class Policy(StrEnum):
    AUTO_DECIDABLE = "auto_decidable"
    REVIEW_REQUIRED = "review_required"
    INFORMATIONAL = "informational"


@dataclass(frozen=True)
class AutomaticDecision:
    """機械が確定させた採否。現状 D01（完全一致の重複）のみ。"""

    geometry_uid: str
    decision: Decision
    reason: str
    kept_geometry_uid: str | None = None
    related_geometry_uid: str | None = None
    duplicate_group_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HumanDecision:
    """FiftyOne で人間が入力した採否。``review_decisions.json`` の1行。"""

    geometry_uid: str
    decision: Decision
    reason: str = ""
    reviewer: str | None = None
    reviewed_at: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class SelectionDecision:
    """全 annotation について1行。採否の master table。"""

    dataset_id: str
    source_json: str
    institution: str
    study: str
    series: str
    file_id: str
    file_uid: str
    geometry_uid: str
    annotation_type: str
    code_system: str
    code: str
    code_text: str
    user: str | None
    timestamp: str | None

    detected_checks: str
    unverified_checks: str
    issue_count: int
    max_severity: str
    review_required: bool
    review_status: ReviewStatus
    final_decision: Decision
    reason: str
    decision_source: DecisionSource

    overrides_automatic: bool = False
    related_geometry_uid: str | None = None
    kept_geometry_uid: str | None = None
    duplicate_group_id: str | None = None
    reviewer: str | None = None
    reviewed_at: str | None = None
    comment: str | None = None

    @property
    def is_unverified(self) -> bool:
        return self.unverified_checks != NONE


# 列の順序。CSV とレポートで共有する。
COLUMNS: tuple[str, ...] = (
    "dataset_id",
    "source_json",
    "institution",
    "study",
    "series",
    "file_id",
    "file_uid",
    "geometry_uid",
    "annotation_type",
    "code_system",
    "code",
    "code_text",
    "user",
    "timestamp",
    "detected_checks",
    "unverified_checks",
    "issue_count",
    "max_severity",
    "review_required",
    "review_status",
    "final_decision",
    "reason",
    "decision_source",
    "overrides_automatic",
    "related_geometry_uid",
    "kept_geometry_uid",
    "duplicate_group_id",
    "reviewer",
    "reviewed_at",
    "comment",
)

_SEVERITY_ORDER = {"error": 3, "warning": 2, "info": 1, NONE: 0}


def policy_for(check_id: str, config: Config) -> Policy:
    """check_id をポリシー分類へ写す。

    check_id をコードへ埋めず設定から引くのは、判断を後から変えられるようにするため。

    照合は**具体的な指定が勝つ**。``M07`` 全体を informational にしつつ、
    ``M07_BBOX_DEGENERATE`` だけを review_required に上げる、といった書き方ができる。
    """
    policy = config.decision_policy
    candidates = (
        (match_rank(check_id, policy.auto_decidable), Policy.AUTO_DECIDABLE),
        (match_rank(check_id, policy.review_required), Policy.REVIEW_REQUIRED),
        (match_rank(check_id, policy.informational), Policy.INFORMATIONAL),
    )
    rank, result = max(candidates, key=lambda item: item[0])
    if rank == 0:
        # 未分類のチェックは安全側（人が見る）に倒す。
        return Policy.REVIEW_REQUIRED
    return result


def build_decisions(
    records: Iterable[AnnotationRecord],
    issues: Iterable[Issue],
    config: Config,
    automatic: Mapping[str, AutomaticDecision] | None = None,
    human: Mapping[str, HumanDecision] | None = None,
) -> list[SelectionDecision]:
    """全 annotation の採否を確定する。

    優先順位（最初に当たったものが確定）:

    1. human decision がある → それ。自動判定を覆したなら ``overrides_automatic``
    2. automatic が exclude → exclude で確定
    3. automatic が keep → **これは「D01では除外されない」だけ**で最終keepではない。
       4へ進む
    4. review_required な Issue がある → pending
    5. cannot_determine を返した check がある → keep（``kept_without_full_check``）
    6. それ以外 → keep（``no_issue_detected``）

    3が重要。D01 で残った側が体外領域にも引っかかっていれば pending にする。
    片方のチェックだけで採用を決めない。
    """
    automatic = automatic or {}
    human = human or {}

    detected: dict[str, list[str]] = {}
    unverified: dict[str, list[str]] = {}
    severity: dict[str, str] = {}
    review_flag: dict[str, bool] = {}
    counts: dict[str, int] = {}

    for issue in issues:
        uid = issue.geometry_uid
        if uid is None:
            continue
        policy = policy_for(issue.check_id, config)

        if issue.status is CheckStatus.CANNOT_DETERMINE:
            _append_unique(unverified.setdefault(uid, []), issue.check_id)
            if config.decision_policy.cannot_determine_as_review_required:
                review_flag[uid] = True
            continue
        if issue.status is CheckStatus.NOT_APPLICABLE:
            continue

        counts[uid] = counts.get(uid, 0) + 1
        _append_unique(detected.setdefault(uid, []), issue.check_id)
        if _SEVERITY_ORDER[issue.severity.value] > _SEVERITY_ORDER.get(
            severity.get(uid, NONE), 0
        ):
            severity[uid] = issue.severity.value
        if policy is Policy.REVIEW_REQUIRED:
            review_flag[uid] = True

    decisions: list[SelectionDecision] = []
    for record in records:
        uid = record.geometry_uid
        auto = automatic.get(uid)
        verdict = human.get(uid)
        needs_review = review_flag.get(uid, False)
        has_unverified = uid in unverified

        overrides = False
        if verdict is not None:
            decision = verdict.decision
            reason = verdict.reason or decision.value
            source = DecisionSource.HUMAN
            status = ReviewStatus.REVIEWED
            overrides = auto is not None and auto.decision is not verdict.decision
        elif auto is not None and auto.decision is Decision.EXCLUDE:
            decision, reason = Decision.EXCLUDE, auto.reason
            source, status = DecisionSource.AUTOMATIC, ReviewStatus.NOT_NEEDED
        elif needs_review:
            decision, reason = Decision.PENDING, Reason.REVIEW_REQUIRED.value
            source, status = DecisionSource.NONE, ReviewStatus.PENDING
        elif has_unverified:
            decision = Decision.KEEP
            reason = Reason.KEPT_WITHOUT_FULL_CHECK.value
            source, status = DecisionSource.DEFAULT, ReviewStatus.NOT_NEEDED
        else:
            decision, reason = Decision.KEEP, Reason.NO_ISSUE.value
            source, status = DecisionSource.DEFAULT, ReviewStatus.NOT_NEEDED

        label = record.labels[0] if record.labels else None
        decisions.append(
            SelectionDecision(
                dataset_id=record.dataset_id,
                source_json=record.source_json,
                institution=record.institution,
                study=record.study,
                series=record.series,
                file_id=record.file,
                file_uid=record.file_uid,
                geometry_uid=uid,
                annotation_type=record.annotation_type,
                code_system=label.code_system if label else "",
                code=label.code if label else "",
                code_text=label.code_text if label else "",
                user=record.user,
                timestamp=record.timestamp,
                detected_checks="|".join(detected.get(uid, [])) or NONE,
                unverified_checks="|".join(unverified.get(uid, [])) or NONE,
                issue_count=counts.get(uid, 0),
                max_severity=severity.get(uid, NONE),
                review_required=needs_review,
                review_status=status,
                final_decision=decision,
                reason=reason,
                decision_source=source,
                overrides_automatic=overrides,
                related_geometry_uid=auto.related_geometry_uid if auto else None,
                kept_geometry_uid=auto.kept_geometry_uid if auto else None,
                duplicate_group_id=auto.duplicate_group_id if auto else None,
                reviewer=verdict.reviewer if verdict else None,
                reviewed_at=verdict.reviewed_at if verdict else None,
                comment=verdict.comment if verdict else None,
            )
        )
    return decisions


def summarize(decisions: list[SelectionDecision]) -> dict[str, Any]:
    """採否の内訳。summary と ``build-dataset`` のゲート判定に使う。"""
    by_decision: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    review_status: dict[str, int] = {}
    for decision in decisions:
        by_decision[decision.final_decision.value] = (
            by_decision.get(decision.final_decision.value, 0) + 1
        )
        by_reason[decision.reason] = by_reason.get(decision.reason, 0) + 1
        by_source[decision.decision_source.value] = (
            by_source.get(decision.decision_source.value, 0) + 1
        )
        by_type[decision.annotation_type] = by_type.get(decision.annotation_type, 0) + 1
        review_status[decision.review_status.value] = (
            review_status.get(decision.review_status.value, 0) + 1
        )

    pending = by_decision.get(Decision.PENDING.value, 0)
    uncertain = by_decision.get(Decision.UNCERTAIN.value, 0)
    return {
        "total": len(decisions),
        "by_decision": by_decision,
        "by_reason": by_reason,
        "by_source": by_source,
        "by_annotation_type": by_type,
        "by_review_status": review_status,
        "unverified": sum(1 for d in decisions if d.is_unverified),
        "review_targets": sum(1 for d in decisions if d.review_required),
        "pending": pending,
        "uncertain": uncertain,
        "ready_to_build": pending == 0 and uncertain == 0,
    }


def assert_invariants(
    decisions: list[SelectionDecision], records: list[AnnotationRecord]
) -> None:
    """採否マスタの不変条件。壊れていれば即座に落とす。

    「全 annotation が必ず1行」が成果物の意味そのものなので、
    静かに欠けるくらいなら失敗させる。
    """
    if len(decisions) != len(records):
        raise AssertionError(
            f"selection_decisions の行数 {len(decisions)} が "
            f"annotation 件数 {len(records)} と一致しない"
        )
    uids = {decision.geometry_uid for decision in decisions}
    if len(uids) != len(decisions):
        raise AssertionError("selection_decisions に geometry_uid の重複がある")
    if uids != {record.geometry_uid for record in records}:
        raise AssertionError(
            "selection_decisions と annotation の geometry_uid が一致しない"
        )

    for decision in decisions:
        if decision.review_status is ReviewStatus.NOT_NEEDED and (
            decision.final_decision not in (Decision.KEEP, Decision.EXCLUDE)
        ):
            raise AssertionError(
                f"{decision.geometry_uid}: review_status=not_needed なのに "
                f"final_decision={decision.final_decision}"
            )
        if decision.decision_source is DecisionSource.HUMAN and not decision.reviewer:
            raise AssertionError(
                f"{decision.geometry_uid}: human 判定なのに reviewer が無い"
            )
        # 逆は成立しない。判定不能を持つ annotation でも review_required な Issue が
        # あれば pending が優先される（§優先順位ラダーの4が5より前）。
        if decision.reason == Reason.KEPT_WITHOUT_FULL_CHECK.value and (
            not decision.is_unverified
        ):
            raise AssertionError(
                f"{decision.geometry_uid}: kept_without_full_check なのに "
                "unverified_checks が空"
            )


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)
