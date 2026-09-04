"""M06 パス形式。

「/mnt以降の相対パス」という規約自体は実データでほぼ守られている（既知の例外は
先頭スラッシュ付き2件のみ）。そこで、より実効性のある不変条件も併せて検査する:

- ``image_path`` は ``medical2/`` 始まり・8階層
- ``path_mask`` / ``path_original_mask`` は ``annotation/`` 始まり
- **マスクのファイル名 stem == ``geometry_uid``**（1665/1665 で成立）
- DICOM のファイル名 stem == ``file_id``

最後の2つが破れていればアノテーションの紐付け間違いを意味するので、
「ルートが合っているか」より強い検査になる。

規約はデータセット形式ごとに違うので、値はアダプタの ``path_invariants()`` から取る。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterator

from ..base import Category, CheckContext, Issue, ReviewPriority, Severity

CHECK_ID = "M06_PATH_FORMAT"
CATEGORY = Category.MACHINE
DEFAULT_SEVERITY = Severity.WARNING
TITLE = "パス形式の統一"
DESCRIPTION = (
    "JSON内の相対パスがルート・階層・命名の規約に従っているかを検査する。"
    "マスクのファイル名が geometry_uid と一致するかが最も実効性が高い。"
)

LEADING_SLASH = "M06_PATH_LEADING_SLASH"
UNEXPECTED_ROOT = "M06_PATH_UNEXPECTED_ROOT"
UNEXPECTED_DEPTH = "M06_PATH_UNEXPECTED_DEPTH"
NAME_MISMATCH = "M06_PATH_NAME_MISMATCH"


def run(ctx: CheckContext) -> Iterator[Issue]:
    for record in ctx.records:
        invariants = ctx.path_invariants.get(record.dataset_id, {})
        image_root = invariants.get("image_path_root")
        annotation_root = invariants.get("annotation_path_root")
        depth = invariants.get("image_path_depth")

        fields = [("image_path", record.image_path)]
        if record.path_mask:
            fields.append(("path_mask", record.path_mask))
        if record.path_original_mask:
            fields.append(("path_original_mask", record.path_original_mask))

        for name, raw in fields:
            if raw.startswith("/"):
                yield ctx.issue(
                    LEADING_SLASH,
                    record,
                    f"{name} が先頭スラッシュ付きで格納されている"
                    "（そのまま連結すると基準ルートが捨てられる）",
                    category=CATEGORY,
                    severity=Severity.WARNING,
                    review_priority=ReviewPriority.LOW,
                    field=name,
                    value=raw,
                )

            expected_root = image_root if name == "image_path" else annotation_root
            parts = PurePosixPath(raw.lstrip("/")).parts
            if expected_root and parts and parts[0] != expected_root:
                yield ctx.issue(
                    UNEXPECTED_ROOT,
                    record,
                    f"{name} のルートが {expected_root!r} でない: {parts[0]!r}",
                    category=CATEGORY,
                    severity=Severity.ERROR,
                    review_priority=ReviewPriority.HIGH,
                    field=name,
                    value=raw,
                    expected_root=expected_root,
                )

        image_parts = PurePosixPath(record.image_path.lstrip("/")).parts
        if depth and image_parts and len(image_parts) != int(depth):
            yield ctx.issue(
                UNEXPECTED_DEPTH,
                record,
                f"image_path の階層数が {depth} でない: {len(image_parts)}"
                "（参照マスクのパス導出が壊れる）",
                category=CATEGORY,
                severity=Severity.WARNING,
                review_priority=ReviewPriority.NORMAL,
                value=record.image_path,
                depth=len(image_parts),
                expected_depth=int(depth),
            )

        # マスクのファイル名は geometry_uid と一致するはず。
        # 破れていれば別のアノテーションのマスクを指している可能性がある。
        if invariants.get("mask_stem_equals") == "geometry_uid":
            for name, raw in fields:
                if name == "image_path":
                    continue
                stem = PurePosixPath(raw).stem
                if stem != record.geometry_uid:
                    yield ctx.issue(
                        NAME_MISMATCH,
                        record,
                        f"{name} のファイル名 {stem!r} が geometry_uid と一致しない"
                        "（別アノテーションのマスクを指している可能性）",
                        category=CATEGORY,
                        severity=Severity.ERROR,
                        review_priority=ReviewPriority.CRITICAL,
                        field=name,
                        stem=stem,
                        geometry_uid=record.geometry_uid,
                    )

        if invariants.get("image_stem_equals") == "file_id":
            stem = PurePosixPath(record.image_path).stem
            if record.image_path and stem != record.file:
                yield ctx.issue(
                    NAME_MISMATCH,
                    record,
                    f"image_path のファイル名 {stem!r} が "
                    f"file_id {record.file!r} と一致しない",
                    category=CATEGORY,
                    severity=Severity.ERROR,
                    review_priority=ReviewPriority.CRITICAL,
                    field="image_path",
                    stem=stem,
                    file_id=record.file,
                )
