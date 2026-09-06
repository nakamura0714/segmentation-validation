"""実データの既知値。**合わなければ実装が変わったということ。**

`uv run pytest -m realdata` で走る。既定の `uv run pytest` からは除外している
（`/mnt` のデータセットJSONと走査キャッシュが必要なため）。

チェックと採否を**その場で再計算**するので、成果物のファイルではなくコードを
検証する。走査キャッシュは読むだけなので数秒で終わる。

値の出所は plan の §21「既存実データの既知値」。閾値やポリシーを意図的に
変えたときは、ここも一緒に更新する（更新せずに通らなくなったら、
それは意図しない変更が入ったということ）。
"""

from __future__ import annotations

import collections

import pytest

from segmentation_validation.checks import ALL_CHECKS
from segmentation_validation.cli import _load_context
from segmentation_validation.config import load_config
from segmentation_validation.selection.automatic import (
    build_automatic_decisions,
    build_broken_decisions,
    merge_automatic,
)
from segmentation_validation.selection.decisions import (
    Decision,
    assert_invariants,
    build_decisions,
)
from segmentation_validation.selection.image_decisions import (
    assert_image_invariants,
    build_image_decisions,
)

pytestmark = pytest.mark.realdata

# --- 母集団（§3.5）
POPULATION = {"patients": 982, "studies": 1044, "images": 1083, "annotations": 1817}
ANNOTATED_IMAGES = 863

# --- check_id ごとの検出件数（§21 / 実装後の実測）
EXPECTED_ISSUES = {
    "D01_EXACT_DUPLICATE": 202,  # 101ペア × 2
    "D03_NEAR_DUPLICATE": 4,  # 2ペア × 2
    "D04_CONTAINED_DIFFERENT_LABEL": 72,  # 36ペア × 2
    "D04_CONTAINED_DUPLICATE": 6,  # 3ペア × 2
    "M06_PATH_LEADING_SLASH": 2,
    "M07_BBOX_DEGENERATE": 7,
    "M07_BBOX_OUT_OF_IMAGE": 4,
    "M07_SIZE_FIELDS_NULL": 152,
    "M09_NEGATIVE_CASE": 187,
    "M09_UNANNOTATED_VIEW": 33,
    "S03_STRAY_COMPONENT": 5,
    "S03_SUSPICIOUSLY_SMALL": 124,
    "S03_TINY_ANNOTATION": 1,
    "S04_ORIGINAL_FINAL_DIVERGENCE": 6,
    "S05_OUTSIDE_BODY": 50,
    "S05_REFERENCE_UNAVAILABLE": 509,
}
# ★0件が正しいもの。回帰検知のために明示する。
EXPECTED_ZERO = (
    "M01_MASK_RESOLUTION",
    "M02_MASK_NOT_GRAYSCALE",
    "M03_MASK_NOT_BINARY",
    "M04_MASK_EMPTY",
    "M05_FILE_MISSING",
    "M08_JSON_BBOX_MISMATCH",
    "M08_REGION_COUNT_MISMATCH",
    "D02_EXACT_MASK_LABEL_CONFLICT",
)

# --- 採否（目視前）
EXPECTED_DECISIONS = {"keep": 1465, "pending": 251, "exclude": 101}
EXPECTED_REASONS = {
    "no_issue_detected": 984,
    "kept_without_full_check": 481,
    "review_required": 251,
    "older_exact_duplicate": 101,
}
EXPECTED_IMAGE_DECISIONS = {"keep": 1050, "pending": 33}
EXPECTED_IMAGE_CLASSES = {
    "annotated": 863,
    "negative_case": 187,
    "unannotated_view": 33,
}

# --- 参照マスクのカバレッジ（§3）
REFERENCE_DETERMINABLE = 1156
REFERENCE_CANNOT_DETERMINE = 509


@pytest.fixture(scope="module")
def context():
    """実データ + 走査キャッシュ。無ければテストごとスキップする。"""
    config = load_config(None, [])
    ctx = _load_context(config)
    if ctx is None:
        pytest.skip("対象JSONが読めない（/mnt のデータセットが無い）")
    if not ctx.files:
        pytest.skip("走査キャッシュが無い。先に `scan` を実行する")
    return ctx


@pytest.fixture(scope="module")
def issues(context):
    found = []
    for check in ALL_CHECKS:
        found.extend(check.run(context))
    return found


@pytest.fixture(scope="module")
def counts(issues):
    return collections.Counter(issue.check_id for issue in issues)


# --------------------------------------------------------------- 母集団


def test_母集団が変わっていない(context):
    records = context.records + context.out_of_scope
    actual = {
        "patients": len({(r.dataset_id, r.patient_id) for r in context.groups}),
        "studies": len({(r.dataset_id, r.study) for r in context.groups}),
        "images": len(context.groups),
        "annotations": len(records),
    }
    assert actual == POPULATION


def test_annotationを持つ画像の枚数(context):
    annotated = {r.file_uid for r in context.records + context.out_of_scope}
    assert len(annotated) == ANNOTATED_IMAGES


def test_geometry_uidは全域で一意(context):
    records = context.records + context.out_of_scope
    uids = {r.geometry_uid for r in records}
    assert len(uids) == len(records)


def test_timestampは全件解釈できる(context):
    """★D01 の自動採否がこれに依存している。"""
    records = context.records + context.out_of_scope
    broken = [r.geometry_uid for r in records if r.parsed_timestamp is None]
    assert broken == []


# --------------------------------------------------------------- 検出件数


@pytest.mark.parametrize("check_id,expected", sorted(EXPECTED_ISSUES.items()))
def test_検出件数(counts, check_id, expected):
    assert counts.get(check_id, 0) == expected


@pytest.mark.parametrize("check_id", EXPECTED_ZERO)
def test_0件が正しいもの(counts, check_id):
    """`path_mask` は 1665/1665 が規定を満たしている。壊れたら気づけること。"""
    assert counts.get(check_id, 0) == 0


def test_issueの総数(issues):
    assert len(issues) == sum(EXPECTED_ISSUES.values())


def test_annotation単位と画像単位の内訳(issues):
    per_annotation = sum(1 for i in issues if i.geometry_uid is not None)
    per_image = sum(1 for i in issues if i.geometry_uid is None)

    assert (per_annotation, per_image) == (1144, 220)


def test_参照マスクのカバレッジ(counts, context):
    """01272 が丸ごと未整備。除けば 519/533 = 97.4%。"""
    assert counts.get("S05_REFERENCE_UNAVAILABLE", 0) == REFERENCE_CANNOT_DETERMINE

    brush = [r for r in context.records if r.has_mask]
    assert len(brush) - REFERENCE_CANNOT_DETERMINE == REFERENCE_DETERMINABLE


# --------------------------------------------------------------- 採否


@pytest.fixture(scope="module")
def decisions(context, issues):
    config = context.config
    duplicates, _ = build_automatic_decisions(context.records, context.pairs, config)
    automatic = merge_automatic(duplicates, build_broken_decisions(issues, config))
    return build_decisions(
        context.records + context.out_of_scope,
        issues,
        config,
        automatic=automatic,
        out_of_scope=frozenset(r.geometry_uid for r in context.out_of_scope),
    )


def test_自動採否はD01の101件だけ(context, issues):
    config = context.config
    duplicates, undecidable = build_automatic_decisions(
        context.records, context.pairs, config
    )
    excluded = [d for d in duplicates.values() if d.decision is Decision.EXCLUDE]
    kept = [d for d in duplicates.values() if d.decision is Decision.KEEP]

    assert len(excluded) == 101
    assert len(kept) == 101, "各グループはサイズ2なので keep も101件"
    assert undecidable == {}, "★timestamp の tie / 欠損 は実データに無い"
    # M01-M05 は実データで0件なので自動 exclude も0件。
    assert build_broken_decisions(issues, config) == {}


def test_採否の内訳(decisions):
    actual = collections.Counter(d.final_decision.value for d in decisions)
    assert dict(actual) == EXPECTED_DECISIONS


def test_採否の理由の内訳(decisions):
    actual = collections.Counter(d.reason for d in decisions)
    assert dict(actual) == EXPECTED_REASONS


def test_未検証の件数(decisions):
    """`unverified_checks != none` が509件。うち481が kept_without_full_check。

    差の28件は目視も必要なので pending が優先される（片方向の含意）。
    """
    unverified = [d for d in decisions if d.is_unverified]
    assert len(unverified) == REFERENCE_CANNOT_DETERMINE

    kept_without = [d for d in unverified if d.reason == "kept_without_full_check"]
    assert len(kept_without) == EXPECTED_REASONS["kept_without_full_check"]


def test_採否マスタの不変条件(context, decisions):
    records = list(context.records) + list(context.out_of_scope)
    assert len(decisions) == POPULATION["annotations"]
    assert_invariants(decisions, records)


def test_目視の実作業量(decisions):
    """annotation 251件 = 開く画像 177枚。工数の見積りは画像枚数で行う。"""
    pending = [d for d in decisions if d.final_decision is Decision.PENDING]
    assert len(pending) == 251
    assert len({d.file_uid for d in pending}) == 177


def test_画像単位の採否(context, issues):
    image_decisions = build_image_decisions(context.groups, issues, context.config)

    assert len(image_decisions) == POPULATION["images"]
    assert_image_invariants(image_decisions, context.groups)
    assert (
        dict(collections.Counter(d.final_decision.value for d in image_decisions))
        == EXPECTED_IMAGE_DECISIONS
    )
    assert (
        dict(collections.Counter(d.image_class for d in image_decisions))
        == EXPECTED_IMAGE_CLASSES
    )
