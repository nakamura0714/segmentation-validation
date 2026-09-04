"""M05 ファイルの実在と可読性。

「存在しない」と「存在するが開けない」を別IDで区別する。
後者は実在する（参照マスク側に21バイトの ``Internal Server Error`` という
テキストファイルが混ざっている）。パイプラインの再実行が要るのはこちらだけなので、
同じ扱いにすると対処が決まらない。
"""

from __future__ import annotations

from typing import Iterator

from ...core.measure import ROLE_MASK, ROLE_ORIGINAL
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity
from ._common import for_role

CHECK_ID = "M05_FILE_EXISTS"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "DICOM・マスクの実在と可読性"
DESCRIPTION = "存在しない場合と、存在するが壊れている場合を分けて報告する。"

MISSING = "M05_FILE_MISSING"
UNREADABLE = "M05_FILE_UNREADABLE"


def run(ctx: CheckContext) -> Iterator[Issue]:
    seen_files: set[str] = set()

    for role in (ROLE_MASK, ROLE_ORIGINAL):
        for record, measurement in for_role(ctx, role):
            if not measurement.exists:
                yield ctx.issue(
                    MISSING,
                    record,
                    f"{role} が存在しない: {measurement.mask_path}",
                    category=CATEGORY,
                    severity=Severity.ERROR,
                    review_priority=ReviewPriority.CRITICAL,
                    role=role,
                    path=measurement.mask_path,
                )
            elif measurement.read_error is not None:
                yield ctx.issue(
                    UNREADABLE,
                    record,
                    f"{role} は存在するが読めない: {measurement.read_error}",
                    category=CATEGORY,
                    severity=Severity.ERROR,
                    review_priority=ReviewPriority.CRITICAL,
                    role=role,
                    path=measurement.mask_path,
                    file_bytes=measurement.file_bytes,
                    error=measurement.read_error,
                )

    # DICOM はファイル単位。同じファイルの annotation ごとに重複して出さない。
    for record in ctx.records:
        file_measurement = ctx.files.get(record.file_uid)
        if file_measurement is None or record.file_uid in seen_files:
            continue
        seen_files.add(record.file_uid)

        if not file_measurement.image_exists:
            yield ctx.issue(
                MISSING,
                record,
                f"DICOMが存在しない: {record.image_path}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.CRITICAL,
                role="dicom",
                path=record.image_path,
            )
        elif file_measurement.dicom_read_error is not None:
            yield ctx.issue(
                UNREADABLE,
                record,
                f"DICOMは存在するが読めない: {file_measurement.dicom_read_error}",
                category=CATEGORY,
                severity=Severity.ERROR,
                review_priority=ReviewPriority.CRITICAL,
                role="dicom",
                path=record.image_path,
                error=file_measurement.dicom_read_error,
            )
