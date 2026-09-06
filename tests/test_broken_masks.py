"""M01-M05 の自動 exclude。

★**実データでは全て0件**（`path_mask` は 1665/1665 が規定を満たしている）ので、
この分岐は合成データでしか検証できない。

絞り込み条件が3つあり、どれが緩んでも事故になる:

- ``severity=ERROR`` でないと拾わない → original マスクの INFO（RGBAは正常）を
  巻き込むと1400件超が自動 exclude されてしまう
- ``status=checked`` でないと拾わない → 判定不能を「壊れている」と誤読しない
- ``category=machine`` かつポリシーが ``auto_decidable`` でないと拾わない
  → 目視すべき D/S 系を機械が勝手に落とさない
"""

from __future__ import annotations

from dataclasses import replace

from conftest import make_issue

from segmentation_validation.checks.base import Category, CheckStatus, Severity
from segmentation_validation.selection.automatic import (
    BROKEN_MASK,
    build_broken_decisions,
)
from segmentation_validation.selection.decisions import Decision

# config.decision_policy.auto_decidable に載っている machine check。
AUTO_EXCLUDABLE = (
    "M01_MASK_RESOLUTION",
    "M02_MASK_CHANNELS",
    "M03_MASK_BINARY",
    "M04_MASK_NOT_EMPTY",
    "M05_FILE_EXISTS",
)


def test_壊れたマスクは自動excludeになる(config):
    for check_id in AUTO_EXCLUDABLE:
        issues = [make_issue(check_id, geometry_uid="A")]
        decisions = build_broken_decisions(issues, config)

        assert decisions["A"].decision is Decision.EXCLUDE, check_id
        assert decisions["A"].reason == BROKEN_MASK
        assert decisions["A"].detail["check_id"] == check_id


def test_派生IDでも自動excludeになる(config):
    """``M05_FILE_MISSING`` は ``M05_FILE_EXISTS`` の派生。

    照合はモジュール単位でも効くこと（そうでないと実データの派生IDが漏れる）。
    """
    decisions = build_broken_decisions([make_issue("M05_FILE_MISSING")], config)
    assert decisions["A"].decision is Decision.EXCLUDE


def test_severityがerrorでなければ自動excludeしない(config):
    """★ここが緩むと original マスクの INFO を巻き込んで大事故になる。"""
    for severity in (Severity.WARNING, Severity.INFO):
        issues = [make_issue("M02_MASK_CHANNELS", severity=severity)]
        assert build_broken_decisions(issues, config) == {}


def test_判定不能は自動excludeしない(config):
    """「検査できなかった」を「壊れている」と読み替えてはいけない。"""
    for status in (CheckStatus.CANNOT_DETERMINE, CheckStatus.NOT_APPLICABLE):
        issues = [make_issue("M01_MASK_RESOLUTION", status=status)]
        assert build_broken_decisions(issues, config) == {}


def test_machine以外のカテゴリは自動excludeしない(config):
    """D/S 系は severity=error でも目視。機械が勝手に落とさない。"""
    for category in (Category.DUPLICATE, Category.SUSPICIOUS):
        issues = [make_issue("M01_MASK_RESOLUTION", category=category)]
        assert build_broken_decisions(issues, config) == {}


def test_目視対象のmachine_checkは自動excludeしない(config):
    """M07（座標が壊れた bbox）は machine だが目視に回す。"""
    issues = [make_issue("M07_BBOX_DEGENERATE")]
    assert build_broken_decisions(issues, config) == {}


def test_記録のみのmachine_checkは自動excludeしない(config):
    issues = [make_issue("M06_PATH_LEADING_SLASH")]
    assert build_broken_decisions(issues, config) == {}


def test_画像単位のissueは自動excludeしない(config):
    """``geometry_uid`` が None の Issue（M09）は annotation の採否に使えない。"""
    issues = [make_issue("M09_UNANNOTATED_VIEW", geometry_uid=None)]
    assert build_broken_decisions(issues, config) == {}


def test_ポリシーから外すと自動excludeしなくなる(config):
    """判断は config で変えられること。check_id をコードへ埋めていない証明。"""
    policy = replace(config.decision_policy, auto_decidable=[])
    narrowed = replace(config, decision_policy=policy)

    issues = [make_issue("M01_MASK_RESOLUTION")]
    assert build_broken_decisions(issues, config) != {}
    assert build_broken_decisions(issues, narrowed) == {}


def test_同じannotationに複数の欠陥があっても1件にまとまる(config):
    issues = [
        make_issue("M01_MASK_RESOLUTION", geometry_uid="A"),
        make_issue("M04_MASK_NOT_EMPTY", geometry_uid="A"),
    ]
    decisions = build_broken_decisions(issues, config)

    assert list(decisions) == ["A"]
    # 先に来たものが残る（どちらでも exclude なので採否は変わらない）。
    assert decisions["A"].detail["check_id"] == "M01_MASK_RESOLUTION"
