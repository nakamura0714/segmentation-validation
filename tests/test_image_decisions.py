"""画像単位の採否（1画像 = 1行）。

`selection_decisions` は annotation 単位なので、**annotation を持たない
画像は1行も持てない**。しかし「この画像を開発データに含めるか」は別の判断が要る:

- 正常例（No Findings）187枚 → keep（陰性サンプル）
- 未アノテーションのビュー 33枚 → pending（**側面像なら除外が必要**）

この2つを取り違えると、意図的な陰性症例が消えるか、側面像が混入する。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import NORMAL, make_group, make_issue, make_record

from segmentation_validation.checks.base import ImageClass, classify_image
from segmentation_validation.selection.decisions import (
    Decision,
    DecisionSource,
    HumanDecision,
    ReviewStatus,
)
from segmentation_validation.selection.image_decisions import (
    ImageDecisionOverride,
    ImageReason,
    assert_image_invariants,
    build_image_decisions,
    summarize_images,
)


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


# --------------------------------------------------------------- 分類


def test_annotationを持つ画像はannotated():
    group = make_group((make_record("A"),))
    assert classify_image(group, series_annotated=True) is ImageClass.ANNOTATED


def test_正常例ラベルを持つ未アノテーション画像はnegative_case():
    """★187/187 が `No Findings/001 normal` を持つ意図的な陰性症例。"""
    group = make_group((), case_labels=(NORMAL,))
    assert classify_image(group, series_annotated=False) is ImageClass.NEGATIVE_CASE


def test_他ビューがアノテーション済みならunannotated_view():
    """★33枚。側面像の可能性があるので目視に回す。"""
    group = make_group(())
    assert classify_image(group, series_annotated=True) is ImageClass.UNANNOTATED_VIEW


def test_series全体が未アノテーションで正常例ラベルも無ければorphan():
    group = make_group(())
    actual = classify_image(group, series_annotated=False)
    assert actual is ImageClass.UNANNOTATED_ORPHAN


def test_annotationがあれば正常例ラベルがあってもannotated():
    """所見が付いている画像を陰性症例と誤分類しないこと。"""
    group = make_group((make_record("A"),), case_labels=(NORMAL,))
    assert group.is_negative_case is False
    assert classify_image(group, series_annotated=True) is ImageClass.ANNOTATED


# --------------------------------------------------------------- 採否


def test_annotationを持つ画像はkeep(config):
    groups = [make_group((make_record("A"),))]
    d = only(build_image_decisions(groups, [], config))

    assert d.final_decision is Decision.KEEP
    assert d.reason == ImageReason.HAS_ANNOTATIONS.value
    assert d.n_annotations == 1


def test_正常例はkeepで陰性サンプルとして残る(config):
    groups = [make_group((), case_labels=(NORMAL,))]
    d = only(build_image_decisions(groups, [], config))

    assert d.final_decision is Decision.KEEP
    assert d.reason == ImageReason.NEGATIVE_CASE.value
    assert d.image_class == ImageClass.NEGATIVE_CASE.value


def test_未アノテーションのビューはpendingになる(config):
    """★これが keep に倒れると側面像が開発データへ混入する。"""
    annotated = make_group((make_record("A"),), file="F1")
    unannotated = make_group((), file="F2")
    issues = [make_issue("M09_UNANNOTATED_VIEW", geometry_uid=None, file="F2")]

    decisions = build_image_decisions([annotated, unannotated], issues, config)
    by_file = {d.file_id: d for d in decisions}

    assert by_file["F2"].final_decision is Decision.PENDING
    assert by_file["F2"].reason == ImageReason.REVIEW_REQUIRED.value
    assert by_file["F2"].review_status is ReviewStatus.PENDING
    assert by_file["F1"].final_decision is Decision.KEEP


def test_単独型の未アノテーションは自動exclude(config):
    """★開発データに使わない方針。1万件超あり目視は非現実的。"""
    unannotated = make_group((), file="F3")
    issues = [make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F3")]

    d = only(build_image_decisions([unannotated], issues, config))

    assert d.final_decision is Decision.EXCLUDE
    assert d.reason == ImageReason.UNANNOTATED_ORPHAN_UNUSED.value
    assert d.review_status is ReviewStatus.NOT_NEEDED
    assert d.image_class == ImageClass.UNANNOTATED_ORPHAN.value


def test_目視に回す分類はconfigで変えられる(config):
    """正常例187枚も見たくなったら config に足すだけで済むこと。"""
    groups = [make_group((), case_labels=(NORMAL,))]
    issues = [make_issue("M09_NEGATIVE_CASE", geometry_uid=None)]

    assert only(build_image_decisions(groups, issues, config)).final_decision is (
        Decision.KEEP
    )

    policy = replace(
        config.decision_policy,
        review_image_classes=[
            "unannotated_view",
            "unannotated_orphan",
            "negative_case",
        ],
        review_required=[*config.decision_policy.review_required, "M09_NEGATIVE_CASE"],
    )
    switched = replace(config, decision_policy=policy)
    d = only(build_image_decisions(groups, issues, switched))
    assert d.final_decision is Decision.PENDING


def test_人間の判定が画像の採否に反映される(config):
    """側面像と判定したら画像ごと落とす。"""
    group = make_group(())
    human = {
        group.file_uid: HumanDecision(
            geometry_uid=group.file_uid,
            decision=Decision.EXCLUDE,
            reason="lateral_view",
            reviewer="me@example.com",
            reviewed_at="2026-09-04T09:00:00+00:00",
        )
    }
    d = only(build_image_decisions([group], [], config, human=human))

    assert d.final_decision is Decision.EXCLUDE
    assert d.reason == "lateral_view"
    assert d.decision_source is DecisionSource.HUMAN
    assert d.review_status is ReviewStatus.REVIEWED


# --------------------------------------------------------------- override（一括承認）


def _override(
    id: str = "test-override",
    *,
    dataset_id: str | None = "SYNTH",
    reason: str | None = "unannotated_orphan_unused",
) -> ImageDecisionOverride:
    match: dict[str, tuple | None] = {}
    if dataset_id is not None:
        match["dataset_id"] = (dataset_id,)
    if reason is not None:
        match["auto_decision_reason"] = (reason,)
    return ImageDecisionOverride(
        id=id,
        match=match,
        action="keep",
        approved_by="manager@example.com",
        approved_at="2026-09-12T00:00:00+00:00",
        note="スポットチェック済み",
    )


def test_overrideが無ければ既存の挙動のまま(config):
    """overrideファイル無し（既定）で挙動が変わらないこと。"""
    unannotated = make_group((), file="F3")
    issues = [make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F3")]

    d = only(build_image_decisions([unannotated], issues, config))

    assert d.final_decision is Decision.EXCLUDE
    assert d.decision_source is DecisionSource.DEFAULT
    assert d.override_id is None


def test_一致するoverrideでexcludeがkeepに変わる(config):
    """自動exclude（unannotated_orphan_unused）だけが対象。reasonは変えない。"""
    unannotated = make_group((), file="F3")
    issues = [make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F3")]
    overrides = [_override()]

    d = only(build_image_decisions([unannotated], issues, config, overrides=overrides))

    assert d.final_decision is Decision.KEEP
    assert d.decision_source is DecisionSource.OVERRIDE
    # ★reasonは元のまま。監査用に「元々なぜexcludeだったか」を残す。
    assert d.reason == ImageReason.UNANNOTATED_ORPHAN_UNUSED.value
    assert d.override_id == "test-override"
    assert d.override_approved_by == "manager@example.com"
    assert d.override_note == "スポットチェック済み"


def test_条件が一致しないoverrideは適用されない(config):
    """dataset_idが違えば対象外。他データセットまで巻き込まない安全性。"""
    unannotated = make_group((), file="F3")
    issues = [make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F3")]
    overrides = [_override(dataset_id="OTHER_DATASET")]

    d = only(build_image_decisions([unannotated], issues, config, overrides=overrides))

    assert d.final_decision is Decision.EXCLUDE
    assert d.decision_source is DecisionSource.DEFAULT
    assert d.override_id is None


def test_人間判定はoverrideで上書きされない(config):
    """★安全側ガード。人間が明示的にexcludeと判断したものを条件一致だけで
    黙って上書きしないこと。"""
    group = make_group(())
    human = {
        group.file_uid: HumanDecision(
            geometry_uid=group.file_uid,
            decision=Decision.EXCLUDE,
            reason="lateral_view",
            reviewer="me@example.com",
            reviewed_at="2026-09-04T09:00:00+00:00",
        )
    }
    # 人間判定のreasonにたまたま一致するoverrideを作っても効かないこと。
    overrides = [_override(reason="lateral_view")]

    d = only(
        build_image_decisions([group], [], config, human=human, overrides=overrides)
    )

    assert d.final_decision is Decision.EXCLUDE
    assert d.decision_source is DecisionSource.HUMAN
    assert d.override_id is None


def test_seriesの位置もoverrideの条件にできる(config):
    """``series_image_index`` を条件にすると、同じ ``image_class``（ORPHAN）でも
    位置が違えば別扱いできること。"""
    index0 = make_group(
        (), file="F1", series="S1", series_image_index=0, series_image_count=1
    )
    index1 = make_group(
        (), file="F2", series="S2", series_image_index=1, series_image_count=2
    )
    issues = [
        make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F1"),
        make_issue("M09_UNANNOTATED_SERIES", geometry_uid=None, file="F2"),
    ]
    override = ImageDecisionOverride(
        id="view-index-1",
        match={"dataset_id": ("SYNTH",), "series_image_index": (1,)},
        action="keep",
        approved_by="manager@example.com",
        approved_at="2026-09-12T00:00:00+00:00",
    )

    decisions = build_image_decisions(
        [index0, index1], issues, config, overrides=[override]
    )
    by_file = {d.file_id: d for d in decisions}

    # 両方とも series 全体が未アノテーション（ORPHAN）で自動exclude対象だが、
    # override が series_image_index=1 だけを指定しているので F1 は変わらない。
    assert by_file["F1"].image_class == ImageClass.UNANNOTATED_ORPHAN.value
    assert by_file["F1"].final_decision is Decision.EXCLUDE
    assert by_file["F1"].override_id is None

    assert by_file["F2"].image_class == ImageClass.UNANNOTATED_ORPHAN.value
    assert by_file["F2"].final_decision is Decision.KEEP
    assert by_file["F2"].override_id == "view-index-1"
    assert by_file["F2"].reason == ImageReason.UNANNOTATED_ORPHAN_UNUSED.value


# --------------------------------------------------------------- 不変条件


def test_全画像が必ず1行になる(config):
    groups = [
        make_group((make_record("A", file="F1"),), file="F1"),
        make_group((), file="F2", case_labels=(NORMAL,)),
        make_group((), file="F3"),
    ]
    decisions = build_image_decisions(groups, [], config)

    assert len(decisions) == len(groups)
    assert_image_invariants(decisions, groups)
    assert summarize_images(decisions)["total"] == 3


def test_行数が合わなければassertで落ちる(config):
    groups = [make_group((), file="F1"), make_group((), file="F2")]
    decisions = build_image_decisions(groups, [], config)

    with pytest.raises(AssertionError, match="一致しない"):
        assert_image_invariants(decisions[:1], groups)
