"""バリデーションのチェック群。

登録は明示的なタプルで行う（自動探索はしない）。
レポートの順序が決定的になり、ファイル名のtypoが起動時のImportErrorになり、
何が走るのかがこのファイルをgrepするだけで分かる。
チェックの追加は「ファイル1つ＋タプルに1行」で済む。
"""

from .base import (
    Category,
    Check,
    CheckContext,
    CheckStatus,
    Issue,
    ReviewPriority,
    Severity,
    matches,
)
from .duplicate import (
    d01_exact_duplicate,
    d02_label_conflict,
    d03_near_duplicate,
    d04_contained_duplicate,
)
from .machine import (
    m01_mask_resolution,
    m02_mask_channels,
    m03_mask_binary,
    m04_mask_not_empty,
    m05_file_exists,
    m06_path_format,
    m07_bbox_geometry,
    m08_json_consistency,
    m09_unannotated_view,
)
from .suspicious import s03_tiny_region, s04_original_final, s05_outside_body

# ① 機械判定
MACHINE_CHECKS: tuple[Check, ...] = (
    m01_mask_resolution,
    m02_mask_channels,
    m03_mask_binary,
    m04_mask_not_empty,
    m05_file_exists,
    m06_path_format,
    m07_bbox_geometry,
    m08_json_consistency,
    m09_unannotated_view,
)

# ② 重複アノテーション（形 × ラベルの2軸）
DUPLICATE_CHECKS: tuple[Check, ...] = (
    d01_exact_duplicate,
    d02_label_conflict,
    d03_near_duplicate,
    d04_contained_duplicate,
)

# ② 疑わしい項目
SUSPICIOUS_CHECKS: tuple[Check, ...] = (
    s03_tiny_region,
    s04_original_final,
    s05_outside_body,
)

ALL_CHECKS: tuple[Check, ...] = MACHINE_CHECKS + DUPLICATE_CHECKS + SUSPICIOUS_CHECKS

# JSONだけで判定でき、scan（画素の走査）を必要としないチェック。
# 閾値やパス規約を詰めるときに数秒で回せる高速パス。
JSON_ONLY_CHECKS: frozenset[str] = frozenset({"M06", "M07", "S02"})


def selected_checks(
    only: list[str] | None = None, skip: list[str] | None = None
) -> tuple[Check, ...]:
    """``--only`` / ``--skip`` でチェックを絞る。"""
    checks = ALL_CHECKS
    if only:
        checks = tuple(c for c in checks if matches(c.CHECK_ID, only))
    if skip:
        checks = tuple(c for c in checks if not matches(c.CHECK_ID, skip))
    return checks


def requires_scan(checks: tuple[Check, ...]) -> bool:
    """選ばれたチェックが計測キャッシュを必要とするか。"""
    return any(c.CHECK_ID.split("_", 1)[0] not in JSON_ONLY_CHECKS for c in checks)


__all__ = [
    "ALL_CHECKS",
    "DUPLICATE_CHECKS",
    "JSON_ONLY_CHECKS",
    "MACHINE_CHECKS",
    "SUSPICIOUS_CHECKS",
    "Category",
    "Check",
    "CheckContext",
    "CheckStatus",
    "Issue",
    "ReviewPriority",
    "Severity",
    "matches",
    "requires_scan",
    "selected_checks",
]
