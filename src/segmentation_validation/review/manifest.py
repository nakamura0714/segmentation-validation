"""FiftyOne へ渡す中間形式（``review_manifest.json``）。**fiftyone を import しない**。

検証結果とアセットのパスをまとめた、fiftyone に依存しない形。
これを挟むことで:

- fiftyone が入っていない環境でも「レビュー用の材料」まで作れる
- FiftyOne dataset の再構築が、検証の再実行なしにできる
- 何を FiftyOne に載せたかがファイルとして残る
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..checks.base import Issue
from ..config import Config
from ..core.records import FileGroup
from ..selection.decisions import Decision, SelectionDecision
from ..selection.image_decisions import ImageDecision
from .export_assets import AssetPaths
from .review_schema import auto_tag


@dataclass
class ManifestAnnotation:
    """FiftyOne の1 Detection になる。"""

    geometry_uid: str
    label: str
    bounding_box: list[float]
    mask_path: str | None
    # 機械が付けるタグ。再構築のたびに貼り直す。
    auto_tags: list[str] = field(default_factory=list)
    # 現在の採否。人間の判定が既にあればそれが入る。
    review_status: str = Decision.PENDING.value
    review_reason: str = ""
    reviewer: str = ""
    reviewed_at: str = ""
    # App 上で絞り込み・並べ替えに使う属性
    attributes: dict[str, Any] = field(default_factory=dict)
    # この annotation に出た Issue の要約（App で読む）
    issues: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ManifestImage:
    """FiftyOne の1 Sample になる。"""

    file_uid: str
    filepath: str
    band_path: str | None
    dataset_id: str
    institution: str
    patient_id: str
    study: str
    series: str
    file_id: str
    image_class: str
    image_class_ja: str
    # 画像単位の採否（未アノテーションのビューはここでしか判定できない）
    review_status: str
    review_reason: str
    reviewer: str = ""
    reviewed_at: str = ""
    auto_tags: list[str] = field(default_factory=list)
    annotations: list[ManifestAnnotation] = field(default_factory=list)


def build_manifest(
    groups: Sequence[FileGroup],
    assets: Mapping[str, AssetPaths],
    decisions: Sequence[SelectionDecision],
    image_decisions: Sequence[ImageDecision],
    issues: Sequence[Issue],
    config: Config,
) -> dict[str, Any]:
    """アセットが書き出せた画像だけを載せた manifest を作る。"""
    by_uid = {d.geometry_uid: d for d in decisions}
    by_file = {d.file_uid: d for d in image_decisions}

    issues_by_uid: dict[str, list[Issue]] = {}
    issues_by_file: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.geometry_uid is not None:
            issues_by_uid.setdefault(issue.geometry_uid, []).append(issue)
        else:
            issues_by_file.setdefault(issue.file_uid, []).append(issue)

    images: list[ManifestImage] = []
    for group in groups:
        asset = assets.get(group.file_uid)
        if asset is None or asset.image is None:
            continue
        image_decision = by_file.get(group.file_uid)
        file_issues = issues_by_file.get(group.file_uid, [])

        annotations: list[ManifestAnnotation] = []
        for record in group.records:
            box = asset.boxes.get(record.geometry_uid)
            if box is None:
                continue
            decision = by_uid.get(record.geometry_uid)
            found = issues_by_uid.get(record.geometry_uid, [])
            annotations.append(
                ManifestAnnotation(
                    geometry_uid=record.geometry_uid,
                    label=(
                        record.labels[0].code_text_eng
                        if record.labels
                        else record.annotation_type
                    ),
                    bounding_box=box,
                    mask_path=str(asset.masks.get(record.geometry_uid) or "") or None,
                    auto_tags=sorted(
                        {auto_tag(i.check_id) for i in found if _is_auto_worthy(i)}
                    ),
                    review_status=(
                        decision.final_decision.value
                        if decision
                        else Decision.PENDING.value
                    ),
                    review_reason=decision.reason if decision else "",
                    reviewer=(decision.reviewer or "") if decision else "",
                    reviewed_at=(decision.reviewed_at or "") if decision else "",
                    attributes={
                        **_attributes(record, decision),
                        **_display_note(asset.adjusted.get(record.geometry_uid)),
                    },
                    issues=[_issue_row(i) for i in found],
                )
            )

        images.append(
            ManifestImage(
                file_uid=group.file_uid,
                filepath=str(asset.image),
                band_path=str(asset.band) if asset.band else None,
                dataset_id=group.dataset_id,
                institution=group.institution,
                patient_id=group.patient_id,
                study=group.study,
                series=group.series,
                file_id=group.file,
                image_class=image_decision.image_class if image_decision else "",
                image_class_ja=image_decision.image_class_ja if image_decision else "",
                review_status=(
                    image_decision.final_decision.value
                    if image_decision
                    else Decision.KEEP.value
                ),
                review_reason=image_decision.reason if image_decision else "",
                reviewer=(image_decision.reviewer or "") if image_decision else "",
                reviewed_at=(image_decision.reviewed_at or "")
                if image_decision
                else "",
                auto_tags=sorted(
                    {auto_tag(i.check_id) for i in file_issues if _is_auto_worthy(i)}
                ),
                annotations=annotations,
            )
        )

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset_name": config.review.dataset_name,
            "n_images": len(images),
            "n_annotations": sum(len(i.annotations) for i in images),
            "n_pending_annotations": sum(
                1
                for i in images
                for a in i.annotations
                if a.review_status == Decision.PENDING.value
            ),
            "n_pending_images": sum(
                1 for i in images if i.review_status == Decision.PENDING.value
            ),
        },
        "images": [asdict(image) for image in images],
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ internal


def _is_auto_worthy(issue: Issue) -> bool:
    """``auto:`` タグを付ける価値がある Issue か。

    判定不能と対象外はタグにしない（509件の判定不能でタグが埋まる）。
    """
    return issue.status.value == "checked"


def _issue_row(issue: Issue) -> dict[str, Any]:
    return {
        "check_id": issue.check_id,
        "severity": issue.severity.value,
        "message": issue.message,
        "detail": issue.detail,
    }


def _display_note(adjusted: dict[str, Any] | None) -> dict[str, Any]:
    """表示のために枠を調整した場合、元の座標を属性として残す。

    座標が壊れていること自体が目視対象なので、
    「見えるように広げた」ことを隠さない。
    """
    if not adjusted:
        return {}
    return {
        "display_adjusted": adjusted.get("reason", ""),
        "original_bbox": str(adjusted.get("original_bbox", "")),
    }


def _attributes(record: Any, decision: SelectionDecision | None) -> dict[str, Any]:
    """App 上で絞り込み・並べ替えに使う属性。

    ``geometry_uid`` は検証結果と FiftyOne を結ぶキーなので必ず入れる。
    """
    attributes: dict[str, Any] = {
        "geometry_uid": record.geometry_uid,
        "dataset_id": record.dataset_id,
        "institution": record.institution,
        "patient_id": record.patient_id,
        "study": record.study,
        "file_id": record.file,
        "annotation_type": record.annotation_type,
        "annotator": record.user or "",
        "timestamp": record.timestamp or "",
        "is_latest": record.is_latest,
        "version": record.version,
    }
    if record.labels:
        label = record.labels[0]
        attributes.update(
            {
                "code_system": label.code_system,
                "code": label.code,
                "code_text": label.code_text,
            }
        )
    if decision is not None:
        attributes.update(
            {
                "detected_checks": decision.detected_checks,
                "unverified_checks": decision.unverified_checks,
                "max_severity": decision.max_severity,
                "duplicate_group_id": decision.duplicate_group_id or "",
                "related_geometry_uid": decision.related_geometry_uid or "",
            }
        )
    return attributes
