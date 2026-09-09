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
from ..selection.decisions import Decision, DecisionSource, SelectionDecision
from ..selection.image_decisions import ImageDecision
from .export_assets import AssetPaths
from .review_schema import MACHINE_REASONS, auto_tag, effective_reasons


@dataclass
class ManifestAnnotation:
    """FiftyOne の1 Detection になる。"""

    geometry_uid: str
    label: str
    bounding_box: list[float]
    mask_path: str | None
    # S04（修正前後の食い違い）目視用。final とは別レイヤーの Detection になる。
    original_mask_path: str | None = None
    original_bounding_box: list[float] | None = None
    # 機械が付けるタグ。再構築のたびに貼り直す。
    auto_tags: list[str] = field(default_factory=list)
    # 現在の採否。人間の判定が既にあればそれが入る。
    review_status: str = Decision.PENDING.value
    # 人間が選んだ理由。**機械の理由は入れない**（下の _human_reasons 参照）。
    review_reasons: list[str] = field(default_factory=list)
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
    # series内でのこのファイルの位置（0始まり）と series 内の総ファイル数。
    # UNANNOTATED_VIEW / UNANNOTATED_ORPHAN を「series内の何枚目か」まで区別するため。
    series_image_index: int = 0
    series_image_count: int = 1
    # 機械の自動判定理由（例: unannotated_orphan_unused / negative_case）。
    # 人間の理由（review_reasons/review_reason）とは別軸で、
    # decision_source が DEFAULT/OVERRIDE のときだけ入る（HUMAN なら空）。
    # override後もここは変えない —— 監査用に「元々なぜexclude/keepだったか」を残す。
    auto_decision_reason: str = ""
    # データ管理者承認による一括 exclude→keep（override）の監査情報。
    override_id: str = ""
    override_note: str = ""
    override_approved_by: str = ""
    override_approved_at: str = ""
    # 画像単位の採否（未アノテーションのビューはここでしか判定できない）
    review_status: str = Decision.KEEP.value
    review_reason: str = ""
    review_reasons: list[str] = field(default_factory=list)
    reviewer: str = ""
    reviewed_at: str = ""
    auto_tags: list[str] = field(default_factory=list)
    annotations: list[ManifestAnnotation] = field(default_factory=list)
    # 未アノテーション画像のデータセット別スポットチェック（select_spot_check_groups）
    # で選ばれた画像か。採否には影響しない、目視の便宜のためだけの印。
    spot_check: bool = False


def build_manifest(
    groups: Sequence[FileGroup],
    assets: Mapping[str, AssetPaths],
    decisions: Sequence[SelectionDecision],
    image_decisions: Sequence[ImageDecision],
    issues: Sequence[Issue],
    config: Config,
    spot_check_file_uids: frozenset[str] | None = None,
) -> dict[str, Any]:
    """アセットが書き出せた画像だけを載せた manifest を作る。

    ``spot_check_file_uids`` は ``select_spot_check_groups`` で選ばれた画像の
    ``file_uid`` 集合（未アノテーション画像のデータセット別スポットチェック用）。
    """
    by_uid = {d.geometry_uid: d for d in decisions}
    by_file = {d.file_uid: d for d in image_decisions}
    spot_check_file_uids = spot_check_file_uids or frozenset()

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
        # D03/D04 の重複相手は同じ画像内にしかいないので、group内だけで完結する。
        records_by_uid = {r.geometry_uid: r for r in group.records}
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
                    original_mask_path=(
                        str(asset.originals.get(record.geometry_uid) or "") or None
                    ),
                    original_bounding_box=asset.original_boxes.get(record.geometry_uid),
                    auto_tags=sorted(
                        {auto_tag(i.check_id) for i in found if _is_auto_worthy(i)}
                    ),
                    review_status=(
                        decision.final_decision.value
                        if decision
                        else Decision.PENDING.value
                    ),
                    review_reasons=_human_reasons(decision),
                    review_reason=_human_note(decision),
                    reviewer=(decision.reviewer or "") if decision else "",
                    reviewed_at=(decision.reviewed_at or "") if decision else "",
                    attributes={
                        **_attributes(record, decision),
                        **_display_note(asset.adjusted.get(record.geometry_uid)),
                        **_duplicate_hint(record, decision, records_by_uid),
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
                series_image_index=group.series_image_index,
                series_image_count=group.series_image_count,
                auto_decision_reason=_auto_decision_reason(image_decision),
                override_id=_override_field(image_decision, "override_id"),
                override_note=_override_field(image_decision, "override_note"),
                override_approved_by=_override_field(
                    image_decision, "override_approved_by"
                ),
                override_approved_at=_override_field(
                    image_decision, "override_approved_at"
                ),
                review_status=(
                    image_decision.final_decision.value
                    if image_decision
                    else Decision.KEEP.value
                ),
                review_reasons=_human_reasons(image_decision),
                review_reason=_human_note(image_decision),
                reviewer=(image_decision.reviewer or "") if image_decision else "",
                reviewed_at=(image_decision.reviewed_at or "")
                if image_decision
                else "",
                auto_tags=sorted(
                    {auto_tag(i.check_id) for i in file_issues if _is_auto_worthy(i)}
                ),
                annotations=annotations,
                spot_check=group.file_uid in spot_check_file_uids,
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


def _human_reasons(decision: Any) -> list[str]:
    """採否から**人間が選んだ理由だけ**を取り出す。

    ``decision.reason`` は機械の理由と人間の理由が同じ1列を共有している。
    目視待ちの annotation では機械の ``review_required`` が入っているので、
    そのまま App の理由欄へ流すと「人間が review_required と入力した」ことに
    なってしまう。実際にそうなっていて、目視102件の理由が全部
    ``review_required`` になっていた。候補に無い値はここで落とす。
    """
    if decision is None:
        return []
    return effective_reasons(decision.reason)


def _auto_decision_reason(decision: Any) -> str:
    """機械の自動判定理由だけを取り出す（例: unannotated_orphan_unused）。

    ``decision.reason`` は機械・人間・overrideのどれで決まった行でも同じ1列を
    共有しているので、``decision_source`` が ``DEFAULT``（機械の自動判定）か
    ``OVERRIDE``（データ管理者承認による一括exclude→keep。reasonは元の自動判定理由の
    ままにしてある）のときだけ返す。``HUMAN`` なら人間の理由なので空にする。
    """
    if decision is None:
        return ""
    source = getattr(decision, "decision_source", None)
    if source in (DecisionSource.DEFAULT, DecisionSource.OVERRIDE):
        return decision.reason or ""
    return ""


def _override_field(decision: Any, field_name: str) -> str:
    """override監査フィールド（``override_id`` 等）を空文字許容で取り出す。"""
    if decision is None:
        return ""
    return getattr(decision, field_name, None) or ""


def _human_note(decision: Any) -> str:
    """自由記述の理由。候補に当てはまらない人間の記述だけを残す。

    機械の理由（``review_required`` 等）は空にする。App の入力欄に前回の値が
    残っていること自体は親切だが、機械の値が残っていると
    「理由が記録されている」ように見えて記録されない。
    """
    if decision is None:
        return ""
    note = decision.reason or ""
    if note in MACHINE_REASONS or effective_reasons(note):
        # 機械の理由、または候補で表せる理由。自由記述欄には何も残さない。
        return ""
    return note


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


def _elapsed_ja(seconds: float) -> str:
    """経過時間を読める形に丸める。

    桁を揃えるより「14秒差」なのか「3日差」なのかが判断を分ける。
    14秒差は同一作業の連続保存（どちらを残しても実質同じ）、
    3日差は別作業（新しい方が修正版である可能性が高い）。
    """
    seconds = abs(seconds)
    for unit, label in ((86400.0, "日"), (3600.0, "時間"), (60.0, "分")):
        if seconds >= unit:
            return f"{int(seconds // unit)}{label}"
    return f"{int(seconds)}秒"


def _duplicate_hint(
    record: Any,
    decision: SelectionDecision | None,
    records_by_uid: Mapping[str, Any],
) -> dict[str, Any]:
    """重複ペアの相手と timestamp を比べ、どちらが新しいか目視の参考として出す。

    D01（``selection/automatic.py``）と同じ基準: 欠損・同値・比較不能なら
    判定しない。あくまで人間の判断材料であり、ここで採否を決めるわけではない。

    **相手の実値も返す。** ``newer_in_pair`` の真偽だけだと、App で片方を開いた
    人間は「では相手はいつなのか」を確かめるためにもう片方を開き直すことになる。
    どちらを exclude するかはこの比較そのものなので、1つの Detection で
    判断できる形にする。

    ``records_by_uid`` は**同じ画像内のレコードしか持たない**（呼び出し側が
    ``group.records`` から作る）。D05（クロスデータセット重複）の相手は別画像に
    いるので引けず、その場合は何も返さない。
    """
    if decision is None or decision.related_geometry_uid is None:
        return {}
    partner = records_by_uid.get(decision.related_geometry_uid)
    if partner is None:
        return {}

    hint: dict[str, Any] = {
        "partner_timestamp": partner.timestamp or "",
        "partner_annotator": partner.user or "",
    }
    own_ts = record.parsed_timestamp
    partner_ts = partner.parsed_timestamp
    if own_ts is None or partner_ts is None:
        return {**hint, "newer_in_pair": None, "pair_verdict": "比較不能"}
    if own_ts == partner_ts:
        return {**hint, "newer_in_pair": None, "pair_verdict": "同時刻"}

    newer = own_ts > partner_ts
    diff = _elapsed_ja((own_ts - partner_ts).total_seconds())
    return {
        **hint,
        "newer_in_pair": newer,
        "pair_verdict": f"{'自分' if newer else '相手'}が新しい（{diff}差）",
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
