"""``issues.json`` / ``issues.csv`` の書き出し。

「プログラムが何を検出したか」を記録する。1 annotation に複数の Issue があり得るので
**1 annotation = 1行ではない**。全 annotation の採否は ``selection_decisions`` が持つ。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..checks.base import Issue
from ..config import Config
from ..selection.decisions import Policy, policy_for

CSV_COLUMNS: tuple[str, ...] = (
    "check_id",
    "category",
    "severity",
    "status",
    "review_priority",
    "review_required",
    "dataset_id",
    "source_json",
    "institution",
    "study",
    "series",
    "file",
    "geometry_uid",
    "annotation_type",
    "image_path",
    "mask_path",
    "message",
    "cannot_determine_reason",
    "duplicate_group_id",
    "related_geometry_uids",
    "detail",
)


def issue_to_row(issue: Issue, config: Config) -> dict[str, Any]:
    """Issue を CSV 1行へ平坦化する。

    ``review_required`` は Issue 自体には持たせず、ここで設定のポリシーから導く。
    ポリシーを変えたときにチェックを再実行せずに済む。
    """
    return {
        "check_id": issue.check_id,
        "category": issue.category.value,
        "severity": issue.severity.value,
        "status": issue.status.value,
        "review_priority": issue.review_priority.value,
        "review_required": policy_for(issue.check_id, config) is Policy.REVIEW_REQUIRED,
        "dataset_id": issue.dataset_id,
        "source_json": issue.source_json,
        "institution": issue.institution,
        "study": issue.study,
        "series": issue.series,
        "file": issue.file,
        "geometry_uid": issue.geometry_uid or "",
        "annotation_type": issue.annotation_type or "",
        "image_path": issue.image_path,
        "mask_path": issue.mask_path or "",
        "message": issue.message,
        "cannot_determine_reason": issue.cannot_determine_reason or "",
        "duplicate_group_id": issue.duplicate_group_id or "",
        "related_geometry_uids": "|".join(issue.related_geometry_uids),
        "detail": json.dumps(issue.detail, ensure_ascii=False, sort_keys=True),
    }


def summarize(issues: Sequence[Issue]) -> dict[str, Any]:
    """check_id 別・severity 別・status 別の件数。"""
    by_check: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_institution: dict[str, int] = {}
    cannot_determine_reasons: dict[str, int] = {}
    for issue in issues:
        by_check[issue.check_id] = by_check.get(issue.check_id, 0) + 1
        by_severity[issue.severity.value] = by_severity.get(issue.severity.value, 0) + 1
        by_status[issue.status.value] = by_status.get(issue.status.value, 0) + 1
        by_institution[issue.institution] = by_institution.get(issue.institution, 0) + 1
        if issue.cannot_determine_reason:
            cannot_determine_reasons[issue.cannot_determine_reason] = (
                cannot_determine_reasons.get(issue.cannot_determine_reason, 0) + 1
            )
    return {
        "total": len(issues),
        "by_check": dict(sorted(by_check.items())),
        "by_severity": by_severity,
        "by_status": by_status,
        "by_institution": dict(sorted(by_institution.items())),
        "cannot_determine_reasons": dict(sorted(cannot_determine_reasons.items())),
    }


def write_issues_json(
    path: Path, issues: Sequence[Issue], config: Config, meta: dict[str, Any]
) -> None:
    payload = {
        "meta": {
            **meta,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "summary": summarize(issues),
        "issues": [_issue_to_json(issue) for issue in issues],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_issues_csv(path: Path, issues: Iterable[Issue], config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for issue in issues:
            writer.writerow(issue_to_row(issue, config))


def _issue_to_json(issue: Issue) -> dict[str, Any]:
    data = asdict(issue)
    # StrEnum は json.dumps でそのまま出るが、値を明示しておく。
    for key in ("category", "severity", "status", "review_priority"):
        data[key] = getattr(issue, key).value
    data["related_geometry_uids"] = list(issue.related_geometry_uids)
    return data
