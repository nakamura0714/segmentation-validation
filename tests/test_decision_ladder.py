"""採否の優先順位ラダーと不変条件。

```
1. human               → その判定
2. automatic exclude   → exclude で確定
3. automatic keep      → ★確定しない。4へ進む
4. review_required     → pending
5. cannot_determine    → keep（kept_without_full_check）
6. out_of_scope        → keep
7. それ以外            → keep（no_issue_detected）
```

**3が一番壊れやすい。** D01 で残った側が体外領域にも引っかかっていれば
pending にしなければならない。片方のチェックだけで採用を決めた瞬間、
目視すべきものが黙って開発データへ入る。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import DATASET, make_issue, make_record

from segmentation_validation.checks.base import CheckStatus, Severity
from segmentation_validation.selection.decisions import (
    NONE,
    AutomaticDecision,
    Decision,
    DecisionSource,
    HumanDecision,
    Policy,
    Reason,
    ReviewStatus,
    assert_invariants,
    build_decisions,
    count_cases,
    effective_review_required,
    policy_for,
)


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


# --------------------------------------------------------------- ラダー


def test_issueが無ければkeep(config):
    d = only(build_decisions([make_record("A")], [], config))

    assert d.final_decision is Decision.KEEP
    assert d.reason == Reason.NO_ISSUE.value
    assert d.decision_source is DecisionSource.DEFAULT
    assert d.review_status is ReviewStatus.NOT_NEEDED
    assert d.detected_checks == NONE
    assert d.unverified_checks == NONE


def test_目視対象のissueがあればpending(config):
    issues = [make_issue("S05_OUTSIDE_BODY", severity=Severity.WARNING)]
    d = only(build_decisions([make_record("A")], issues, config))

    assert d.final_decision is Decision.PENDING
    assert d.reason == Reason.REVIEW_REQUIRED.value
    assert d.decision_source is DecisionSource.NONE
    assert d.review_status is ReviewStatus.PENDING
    assert d.review_required is True


def test_記録のみのissueはkeepのまま(config):
    issues = [make_issue("M06_PATH_LEADING_SLASH", severity=Severity.WARNING)]
    d = only(build_decisions([make_record("A")], issues, config))

    assert d.final_decision is Decision.KEEP
    assert d.reason == Reason.NO_ISSUE.value
    assert d.detected_checks == "M06_PATH_LEADING_SLASH"
    assert d.review_required is False


def test_判定不能はkeepだが未検証と記録する(config):
    """★「検査して問題なし」と「検査できなかった」を混ぜないこと。"""
    issues = [
        make_issue(
            "S05_REFERENCE_UNAVAILABLE",
            status=CheckStatus.CANNOT_DETERMINE,
            cannot_determine_reason="no_reference",
        )
    ]
    d = only(build_decisions([make_record("A")], issues, config))

    assert d.final_decision is Decision.KEEP
    assert d.reason == Reason.KEPT_WITHOUT_FULL_CHECK.value
    assert d.unverified_checks == "S05_REFERENCE_UNAVAILABLE"
    # 検出ではないので detected_checks には入らない。
    assert d.detected_checks == NONE


#: build_decisions の automatic/out_of_scope は annotation_uid
#: （f"{dataset_id}::{geometry_uid}"）キー。human は bare geometry_uid のまま。
KEY_A = f"{DATASET}::A"


def test_自動excludeが確定する(config):
    automatic = {
        KEY_A: AutomaticDecision(
            geometry_uid="A",
            decision=Decision.EXCLUDE,
            reason=Reason.OLDER_EXACT_DUPLICATE.value,
            kept_geometry_uid="B",
        )
    }
    d = only(build_decisions([make_record("A")], [], config, automatic=automatic))

    assert d.final_decision is Decision.EXCLUDE
    assert d.reason == Reason.OLDER_EXACT_DUPLICATE.value
    assert d.decision_source is DecisionSource.AUTOMATIC
    assert d.kept_geometry_uid == "B"


def test_自動keepは最終keepではない(config):
    """★ラダーの3。D01 で残った側が体外領域にも当たっていれば pending。"""
    automatic = {
        KEY_A: AutomaticDecision(
            geometry_uid="A", decision=Decision.KEEP, reason="newest_of_group"
        )
    }
    issues = [make_issue("S05_OUTSIDE_BODY", severity=Severity.WARNING)]
    d = only(build_decisions([make_record("A")], issues, config, automatic=automatic))

    assert d.final_decision is Decision.PENDING, "D01のkeepだけで採用を決めてはいけない"
    assert d.reason == Reason.REVIEW_REQUIRED.value


def test_自動keepでほかにissueが無ければkeep(config):
    automatic = {
        KEY_A: AutomaticDecision(
            geometry_uid="A", decision=Decision.KEEP, reason="newest_of_group"
        )
    }
    d = only(build_decisions([make_record("A")], [], config, automatic=automatic))

    assert d.final_decision is Decision.KEEP


def test_人間の判定が最優先(config):
    issues = [make_issue("S03_TINY_ANNOTATION", severity=Severity.WARNING)]
    human = {
        "A": HumanDecision(
            geometry_uid="A",
            decision=Decision.KEEP,
            reason="true_small_lesion",
            reviewer="me@example.com",
            reviewed_at="2026-09-04T09:00:00+00:00",
        )
    }
    d = only(build_decisions([make_record("A")], issues, config, human=human))

    assert d.final_decision is Decision.KEEP
    assert d.reason == "true_small_lesion"
    assert d.decision_source is DecisionSource.HUMAN
    assert d.review_status is ReviewStatus.REVIEWED
    # ★検出の記録は消えない（Precision の分母になる）。
    assert d.detected_checks == "S03_TINY_ANNOTATION"


def test_人間が自動判定を覆したら監査できる(config):
    automatic = {
        KEY_A: AutomaticDecision(
            geometry_uid="A", decision=Decision.EXCLUDE, reason="older_exact_duplicate"
        )
    }
    human = {
        "A": HumanDecision(
            geometry_uid="A",
            decision=Decision.KEEP,
            reason="actually_different",
            reviewer="me@example.com",
        )
    }
    d = only(
        build_decisions(
            [make_record("A")], [], config, automatic=automatic, human=human
        )
    )

    assert d.final_decision is Decision.KEEP
    assert d.overrides_automatic is True


def test_検証対象外はout_of_scopeとして残る(config):
    """チェックを走らせていないので ``no_issue_detected`` と混ぜない。"""
    d = only(
        build_decisions(
            [make_record("A")], [], config, out_of_scope=frozenset({KEY_A})
        )
    )

    assert d.final_decision is Decision.KEEP
    assert d.reason == Reason.OUT_OF_SCOPE.value


def test_判定不能が目視より優先されることはない(config):
    """★ラダーの4が5より前。両方持つ annotation は pending になる。"""
    issues = [
        make_issue("S05_REFERENCE_UNAVAILABLE", status=CheckStatus.CANNOT_DETERMINE),
        make_issue("S03_TINY_ANNOTATION", severity=Severity.WARNING),
    ]
    d = only(build_decisions([make_record("A")], issues, config))

    assert d.final_decision is Decision.PENDING
    # unverified の記録自体は残る。
    assert d.unverified_checks == "S05_REFERENCE_UNAVAILABLE"


# --------------------------------------------------------------- ポリシー


def test_具体的な指定がモジュール指定に勝つ(config):
    """★2段階照合。M07 全体は記録のみだが、座標が壊れた派生IDは目視。"""
    assert policy_for("M07_BBOX_GEOMETRY", config) is Policy.INFORMATIONAL
    assert policy_for("M07_SIZE_FIELDS_NULL", config) is Policy.INFORMATIONAL
    assert policy_for("M07_BBOX_DEGENERATE", config) is Policy.REVIEW_REQUIRED
    assert policy_for("M07_BBOX_OUT_OF_IMAGE", config) is Policy.REVIEW_REQUIRED


def test_未分類のチェックは安全側で目視になる(config):
    """新しい check を足してポリシーに書き忘れたら、黙って keep にはしない。"""
    assert policy_for("Z99_BRAND_NEW_CHECK", config) is Policy.REVIEW_REQUIRED


def test_判定不能を目視に回すかはconfigで切り替わる(config):
    """509件を後から目視に回すためのスイッチ。"""
    check, status = "S05_OUTSIDE_BODY", CheckStatus.CANNOT_DETERMINE
    assert effective_review_required(check, status, config) is False

    policy = replace(config.decision_policy, cannot_determine_as_review_required=True)
    switched = replace(config, decision_policy=policy)
    assert effective_review_required(check, status, switched) is True


def test_not_applicableは常に目視不要(config):
    """スイッチを入れても「このデータには適用されない」は目視に回さない。"""
    policy = replace(config.decision_policy, cannot_determine_as_review_required=True)
    switched = replace(config, decision_policy=policy)

    for cfg in (config, switched):
        assert (
            effective_review_required(
                "M07_SIZE_FIELDS_NULL", CheckStatus.NOT_APPLICABLE, cfg
            )
            is False
        )


def test_判定不能を目視に回すと採否がpendingになる(config):
    """スイッチと採否が連動していること（issues.csv と食い違わない）。"""
    issues = [
        make_issue("S05_REFERENCE_UNAVAILABLE", status=CheckStatus.CANNOT_DETERMINE)
    ]
    policy = replace(config.decision_policy, cannot_determine_as_review_required=True)
    switched = replace(config, decision_policy=policy)

    d = only(build_decisions([make_record("A")], issues, switched))
    assert d.final_decision is Decision.PENDING


# --------------------------------------------------------------- 不変条件


def test_全annotationが必ず1行になる(config):
    records = [make_record(uid) for uid in ("A", "B", "C")]
    # Issue は1件だけ。残り2件も必ず行を持つこと。
    issues = [make_issue("S03_TINY_ANNOTATION", geometry_uid="B")]
    decisions = build_decisions(records, issues, config)

    assert len(decisions) == len(records)
    assert {d.geometry_uid for d in decisions} == {"A", "B", "C"}
    assert_invariants(decisions, records)


def test_行数が合わなければassertで落ちる(config):
    records = [make_record("A"), make_record("B")]
    decisions = build_decisions(records, [], config)

    with pytest.raises(AssertionError, match="一致しない"):
        assert_invariants(decisions[:1], records)


def test_human判定にreviewerが無ければassertで落ちる(config):
    """誰が判断したか追えない human 判定を通してはいけない。"""
    human = {
        "A": HumanDecision(geometry_uid="A", decision=Decision.KEEP, reviewer=None)
    }
    records = [make_record("A")]
    decisions = build_decisions(records, [], config, human=human)

    with_reviewer = "reviewer が無い"
    with pytest.raises(AssertionError, match=with_reviewer):
        assert_invariants(decisions, records)


def test_症例数は重複を除いて数える(config):
    """1患者が複数 study を持つ例が実在する（実測51患者）。"""
    records = [
        make_record("A", file="F1"),
        make_record("B", file="F1"),  # 同じ画像の2件目
        make_record("C", file="F2"),
    ]
    decisions = build_decisions(records, [], config)
    counts = count_cases(decisions)

    assert counts["images"] == 2
    assert counts["patients"] == 1
    assert counts["studies"] == 1
