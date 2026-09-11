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

# --- 母集団
# ★2026-09-09に対象が3データセット（01272/1298/ETR_mask136）から
# 12データセットへ増えた。数値は12データセット構成での実測値。
# 増分の内訳や「なぜ変わったか」は各テストのコメント参照。
POPULATION = {
    "patients": 46127,
    "studies": 47015,
    "images": 47075,
    "annotations": 18495,
}
ANNOTATED_IMAGES = 7391

# --- check_id ごとの検出件数（12データセット構成での実測）
EXPECTED_ISSUES = {
    "D01_EXACT_DUPLICATE": 668,
    # ★新規（3データセットでは0件でEXPECTED_ZERO側にあった）。
    "D02_EXACT_MASK_LABEL_CONFLICT": 46,
    # ★D03/D04 は「重複グループのノードキーを file_uid 込みにした」修正で半減した
    # （16→8 / 28→12）。減った24件は**実体の無い誤帰属**だった: ペアは
    # データセットごとのファイル単位で作られるのに、相手レコードを素の
    # geometry_uid で引いていたため、自分のデータセットでは D05 非代表として
    # checks から隠された annotation のペアが、**別データセットの同名
    # geometry_uid のレコード**に issue を張っていた。同じ重複関係は正しい
    # データセット側で報告されており、関係そのものは1件も失われていない
    # （修正前後の issue 集合を突き合わせて確認済み）。
    "D03_NEAR_DUPLICATE": 8,
    "D04_CONTAINED_DIFFERENT_LABEL": 3,
    "D04_CONTAINED_DUPLICATE": 12,
    # ★新規。複数データセットに同一annotationが重複エクスポートされている
    # （PTE/PTR/kaggle等）ケースは、単一データセットだけでは検出できない。
    "D05_CROSS_DATASET_DUPLICATE": 5786,
    "D05_CROSS_DATASET_DUPLICATE_TIE": 207,
    "D05_CROSS_DATASET_MISMATCH": 40,
    "M06_PATH_LEADING_SLASH": 2,
    # ★新規（3データセットでは0件でEXPECTED_ZERO側にあった）。
    "M08_JSON_BBOX_MISMATCH": 1,
    "M09_NEGATIVE_CASE": 1084,
    # ★新規。kaggle系・ChestMetry_PI6px_normal 等、series全体が未アノテーションで
    # 正常例ラベルも無い画像が大量にある新規データセット由来。
    "M09_UNANNOTATED_SERIES": 35714,
    # 変化なし。複数ファイルseriesは全47,036件中39件のみで、
    # 旧3データセット内で完結する。
    "M09_UNANNOTATED_VIEW": 33,
    "S03_STRAY_COMPONENT": 817,
    "S03_SUSPICIOUSLY_SMALL": 2040,
    "S03_TINY_ANNOTATION": 64,
    "S04_ORIGINAL_FINAL_DIVERGENCE": 21,
    "S05_OUTSIDE_BODY": 28,
    "S05_REFERENCE_UNAVAILABLE": 3183,
}
# ★0件が正しいもの。回帰検知のために明示する。
EXPECTED_ZERO = (
    "M01_MASK_RESOLUTION",
    "M02_MASK_NOT_GRAYSCALE",
    "M03_MASK_NOT_BINARY",
    "M04_MASK_EMPTY",
    "M05_FILE_MISSING",
    # ★新規。3データセットのときは検出されていた annotation が、新たに検出される
    # ようになったクロスデータセット重複（D05）で自動除外され、context.records
    # （per-annotationチェックの対象）から外れたため0件化した。
    "M07_BBOX_DEGENERATE",
    "M07_BBOX_OUT_OF_IMAGE",
    "M07_SIZE_FIELDS_NULL",
    "M08_REGION_COUNT_MISMATCH",
)

# --- 採否（目視前）
# ★D01の自動exclude が 101→167 に増え、その分 pending が 2392→2326 に減った。
# 再エクスポートで geometry_uid がデータセットを跨いで重複していると、
# 重複グループが素の geometry_uid で融合し、自動採否が**どれか1データセット分に
# しか付かなかった**（残りは自動判定を持たないまま D05 の TIE で pending に滞留）。
# 増えた66件の annotation は、いずれも目視で既に exclude と判定済みのものと
# 一致した（66/66）。
EXPECTED_DECISIONS = {"keep": 16002, "pending": 2326, "exclude": 167}
EXPECTED_REASONS = {
    "no_issue_detected": 14932,
    "kept_without_full_check": 1070,
    "review_required": 2326,
    "older_exact_duplicate": 167,
}
EXPECTED_IMAGE_DECISIONS = {"keep": 11328, "pending": 33, "exclude": 35714}
EXPECTED_IMAGE_CLASSES = {
    "annotated": 10244,
    "negative_case": 1084,
    "unannotated_view": 33,
    # ★新規キー。旧3データセットには単独型未アノテーション画像が0件だった
    # （Counterは0件のキーを持たないので辞書に現れていなかった）。
    "unannotated_orphan": 35714,
}

# --- 参照マスクのカバレッジ
REFERENCE_DETERMINABLE = 741
REFERENCE_CANNOT_DETERMINE = 3183


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


def test_geometry_uidはデータセット単位で一意(context):
    """★3データセットのときは ``geometry_uid`` 単独でも全域一意だったが、
    12データセットでは369件が複数データセットにまたがって重複する
    （実測: 全て別データセット間の重複で、同一データセット内の重複は0件）。
    これは同一annotationの再エクスポート（PTE/PTR/kaggle等）によるもので、
    ``AnnotationRecord.annotation_uid``（``dataset_id::geometry_uid``）や
    ``cli.py`` の ``by_uid_per_dataset`` が元々この前提で設計されている
    （データセットごとに分けて持つ）。一意性の単位を
    ``(dataset_id, geometry_uid)`` に修正する。
    """
    records = context.records + context.out_of_scope
    uids = {(r.dataset_id, r.geometry_uid) for r in records}
    assert len(uids) == len(records)

    # 参考: geometry_uid 単独での重複はすべてクロスデータセット
    # （同一データセット内での重複は0件＝再エクスポート以外の原因ではない）。
    dataset_ids_by_geometry_uid: dict[str, set[str]] = {}
    for r in records:
        dataset_ids_by_geometry_uid.setdefault(r.geometry_uid, set()).add(r.dataset_id)
    cross_dataset_dupes = [
        uid for uid, ds_ids in dataset_ids_by_geometry_uid.items() if len(ds_ids) > 1
    ]
    assert len(cross_dataset_dupes) == 369


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

    # annotation単位が24件減っているのは D03/D04 の誤帰属分（EXPECTED_ISSUES 参照）。
    assert (per_annotation, per_image) == (12926, 36831)


def test_参照マスクのカバレッジ(counts, context):
    """741/(741+3183) = 18.9%。母集団が12データセットに増え、参照マスクが
    未整備のデータセット（kaggle系等）が大半を占めるようになったため、
    3データセットのときの割合（97.4%）から大きく下がった。"""
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


def test_D01の自動採否は167件でundecidableは0件(context, issues):
    """★101→167、undecidable 2→0。どちらも「重複グループのノードキーを
    ``file_uid`` 込みにした」修正の結果。

    101件だったのは、再エクスポートで ``geometry_uid`` がデータセットを跨いで
    重複していると、素の geometry_uid をノードにした union-find が別データセット
    のペア同士を1グループへ融合し、自動採否が**どれか1データセット分にしか
    付かなかった**ため（実測84 geometry_uid / 234 annotation が該当）。

    undecidable が2件出ていたのも同じ衝突の産物だった。実体は
    ``ETR_ChestMetry_PI6px_abnormal_non_pneumothorax`` の**両端点とも対象外
    ラベル**のペア1組で、本来そもそも自動決定する対象が無い。素の geometry_uid
    で引いていたため、別データセットの同名 geometry_uid の in-scope レコードを
    掴んで「timestamp が引けない2件」に化けていた。
    """
    config = context.config
    duplicates, undecidable = build_automatic_decisions(
        context.records, context.pairs, config
    )
    excluded = [d for d in duplicates.values() if d.decision is Decision.EXCLUDE]
    kept = [d for d in duplicates.values() if d.decision is Decision.KEEP]

    assert len(excluded) == 167
    assert len(kept) == 167, "各グループはサイズ2なので keep も167件"
    assert undecidable == {}
    # M01-M05 は実データで0件なので自動 exclude も0件（ここは変わらず）。
    assert build_broken_decisions(issues, config) == {}


def test_採否の内訳(decisions):
    actual = collections.Counter(d.final_decision.value for d in decisions)
    assert dict(actual) == EXPECTED_DECISIONS


def test_採否の理由の内訳(decisions):
    actual = collections.Counter(d.reason for d in decisions)
    assert dict(actual) == EXPECTED_REASONS


def test_未検証の件数(decisions):
    """`unverified_checks != none` が3183件。うち1070が kept_without_full_check。

    差の2113件は目視も必要なので pending が優先される（片方向の含意）。
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
    """annotation 2326件 = 開く画像 2278枚。工数の見積りは画像枚数で行う。

    annotation は66件減ったが開く画像枚数は変わらない（D01で自動excludeに
    なった66件は、いずれも目視対象が他にも載っている画像の中にある）。
    """
    pending = [d for d in decisions if d.final_decision is Decision.PENDING]
    assert len(pending) == 2326
    assert len({d.file_uid for d in pending}) == 2278


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
