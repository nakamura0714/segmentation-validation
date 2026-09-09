"""合成データの組み立て。

**実データに存在しないケースを担保するのがこのテストの主目的。**
実測で `timestamp` の tie / 欠損 / parse不能 が 0件、D02 も 0件、
M01-M05 も 0件なので、これらの分岐は実データでは一切動かない。
合成データが唯一の担保になる。

ここでは「テストが読める最小限のレコード」を作る。全フィールドを毎回書くと
テストの意図が埋まるので、既定値を持たせて差分だけ指定できるようにしてある。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from segmentation_validation.checks.base import (
    Category,
    CheckStatus,
    Issue,
    ReviewPriority,
    Severity,
)
from segmentation_validation.config import Config
from segmentation_validation.core.labels import Label
from segmentation_validation.core.measure import ROLE_MASK, MaskMeasurement, PairMeasurement
from segmentation_validation.core.records import AnnotationRecord, FileGroup

DATASET = "SYNTH"
SOURCE = "synth.json"
INSTITUTION = "synth_hospital"
STUDY = "SYNTH00000001_000"
SERIES = "SYNTH00000001_000_000"
FILE = "SYNTH00000001_000_000_000"

#: 気胸。実データの主要ラベルと同じキーにしておく。
PNEUMOTHORAX = Label(
    code_system="Findings",
    code="010",
    code_text="気胸（塗りつぶし）",
    code_text_eng="pneumothorax",
    confidence=None,
    label_id=1,
)
#: 別ラベル。ラベル相違の分岐（D02 / D04 別ラベル）に使う。
NODULE = Label(
    code_system="Findings",
    code="001",
    code_text="結節",
    code_text_eng="nodule",
    confidence=None,
    label_id=2,
)
NORMAL = Label(
    code_system="No Findings",
    code="001",
    code_text="正常",
    code_text_eng="normal",
    confidence=None,
    label_id=3,
)


@pytest.fixture
def config() -> Config:
    """既定設定。テスト側では必要な項目だけ差し替える。"""
    return Config()


def make_record(
    uid: str,
    *,
    dataset_id: str = DATASET,
    source_json: str = SOURCE,
    timestamp: str | None = "2026-01-01 00:00:00+00:00",
    labels: tuple[Label, ...] = (PNEUMOTHORAX,),
    annotation_type: str = "brush",
    file: str = FILE,
    has_mask: bool = True,
    is_latest: int = 1,
    json_bbox: tuple[int, int, int, int] = (10, 10, 100, 100),
    region_count: int | None = 1,
) -> AnnotationRecord:
    """annotation 1件。``uid`` 以外は既定値で埋める。

    ``dataset_id``/``source_json`` を明示的に変えられるのは、クロスデータセット
    重複（D05）のテストで「同じ ``file``（＝同じ ``resolved_image_path``）だが
    別データセット」の2レコードを作るため。既定は単一データセットのテストと
    互換のまま。
    """
    mask = Path(f"/synth/mask/{uid}.png") if has_mask else None
    return AnnotationRecord(
        dataset_id=dataset_id,
        source_json=source_json,
        institution=INSTITUTION,
        study=STUDY,
        series=SERIES,
        file=file,
        patient_id=STUDY.split("_")[0],
        study_date="20260101",
        image_path=f"medical2/synth/{file}.dcm",
        resolved_image_path=Path(f"/mnt/medical2/synth/{file}.dcm"),
        series_shape=(2400, 2000),
        spacing=(0.15, 0.15),
        manufacturer="SYNTH",
        annotation_type=annotation_type,
        geometry_id=1,
        geometry_uid=uid,
        path_mask=f"annotation/synth/mask/{uid}.png" if has_mask else None,
        resolved_path_mask=mask,
        path_original_mask=None,
        resolved_path_original_mask=None,
        json_bbox=json_bbox,
        region_count=region_count,
        declared_size=(90, 90),
        is_latest=is_latest,
        version=1,
        user="synth@example.com",
        timestamp=timestamp,
        annotation_request=1,
        group_id=None,
        comment=None,
        labels=labels,
    )


def make_mask_measurement(
    record: AnnotationRecord,
    *,
    mask_hash: str | None = "hash1",
    role: str = ROLE_MASK,
) -> MaskMeasurement:
    """マスクの計測値1枚分。D05（クロスデータセット重複）は内容一致の判定に
    ``mask_hash`` を使うので、同じ ``mask_hash`` を渡せば「内容が一致するマスク」
    を合成できる。
    """
    return MaskMeasurement(
        file_uid=record.file_uid,
        geometry_uid=record.geometry_uid,
        role=role,
        dataset_id=record.dataset_id,
        source_json=record.source_json,
        annotation_type=record.annotation_type,
        mask_path=record.path_mask,
        exists=True,
        mask_hash=mask_hash,
    )


def make_group(
    records: tuple[AnnotationRecord, ...] = (),
    *,
    dataset_id: str = DATASET,
    file: str = FILE,
    series: str = SERIES,
    study: str = STUDY,
    case_labels: tuple[Label, ...] = (),
    series_image_index: int = 0,
    series_image_count: int = 1,
) -> FileGroup:
    """画像1枚。annotation を持たない画像も作れる（正常例 / 未アノテーション）。

    ``dataset_id`` を変えられるのは exclude→keep の一括override（データセット単位で
    絞り込む）のテスト用。``series_image_index``/``series_image_count`` は
    「series内の複数ファイルのうち何枚目か」のテスト用（実データでは39件のみだが
    UNANNOTATED_VIEW をこの位置まで区別する）。
    """
    return FileGroup(
        dataset_id=dataset_id,
        source_json=SOURCE,
        institution=INSTITUTION,
        study=study,
        series=series,
        file=file,
        patient_id=study.split("_")[0],
        study_date="20260101",
        image_path=f"medical2/synth/{file}.dcm",
        resolved_image_path=Path(f"/mnt/medical2/synth/{file}.dcm"),
        series_shape=(2400, 2000),
        spacing=(0.15, 0.15),
        manufacturer="SYNTH",
        records=records,
        case_labels=case_labels,
        series_image_index=series_image_index,
        series_image_count=series_image_count,
    )


def make_issue(
    check_id: str,
    *,
    geometry_uid: str | None = "A",
    category: Category = Category.MACHINE,
    severity: Severity = Severity.ERROR,
    status: CheckStatus = CheckStatus.CHECKED,
    file: str = FILE,
    cannot_determine_reason: str | None = None,
) -> Issue:
    """Issue 1件。採否ラダーの分岐を突くための最小構成。"""
    return Issue(
        check_id=check_id,
        category=category,
        severity=severity,
        status=status,
        review_priority=ReviewPriority.NORMAL,
        dataset_id=DATASET,
        source_json=SOURCE,
        institution=INSTITUTION,
        study=STUDY,
        series=SERIES,
        file=file,
        file_uid=f"{SOURCE}::{INSTITUTION}/{STUDY}/{SERIES}/{file}",
        geometry_uid=geometry_uid,
        annotation_type="brush",
        image_path=f"medical2/synth/{file}.dcm",
        mask_path=None,
        message=f"synthetic {check_id}",
        cannot_determine_reason=cannot_determine_reason,
    )


def make_pair(
    uid_a: str,
    uid_b: str,
    *,
    pixel_identical: bool = True,
    iou: float = 1.0,
    same_label_keys: bool = True,
    containment_a_in_b: float = 1.0,
    containment_b_in_a: float = 1.0,
    file: str = FILE,
) -> PairMeasurement:
    """同一ファイル内のマスク2枚の関係。既定は「画素完全一致・同一ラベル」= D01。"""
    return PairMeasurement(
        file_uid=f"{SOURCE}::{INSTITUTION}/{STUDY}/{SERIES}/{file}",
        uid_a=uid_a,
        uid_b=uid_b,
        iou=iou,
        intersection_pixels=1000,
        union_pixels=1000,
        containment_a_in_b=containment_a_in_b,
        containment_b_in_a=containment_b_in_a,
        pixel_identical=pixel_identical,
        same_label_keys=same_label_keys,
        both_latest=True,
        same_user=False,
        same_request=True,
        gid_a=1,
        gid_b=2,
        user_a="a@example.com",
        user_b="b@example.com",
        timestamp_a=None,
        timestamp_b=None,
        version_a=1,
        version_b=1,
    )
