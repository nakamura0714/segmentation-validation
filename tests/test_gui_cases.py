"""症例（患者）単位の一覧（``report/gui.py`` の ``case_rows``）。

ダッシュボードの annotation 表は1 annotation = 1行なので、「exclude がどの施設に
出ているか」が読めない。それを症例で束ねるのが ``case_rows``。

**担保したいのは母集団**。``selection_decisions`` は annotation 単位なので
annotation を持たない患者は1行も持たない。しかし画像単位で exclude になった
（実データで33枚）患者はまさに追いたい対象なので、``image_decisions`` 側からも
拾って行を作らなければならない。ここが抜けると exclude が表から消える。
"""

from __future__ import annotations

from dataclasses import replace

from conftest import INSTITUTION, NORMAL, make_group, make_issue, make_record

from segmentation_validation.report.gui import CASE_STATUSES, _Vocab, case_rows
from segmentation_validation.selection.decisions import (
    AutomaticDecision,
    Decision,
    HumanDecision,
    build_decisions,
)
from segmentation_validation.selection.image_decisions import build_image_decisions

# case_rows の列位置。assets/dashboard/app.js の C と対応させる。
# fmt: off
(PAT, INST, DS, STU, IMG, ANN, KEEP, EXC, PEND, UNC, ST, RSN, IEXC, IREV,
 FLAG) = range(15)
# fmt: on


def build(decisions=(), image_decisions=()):
    """語彙を用意して ``case_rows`` を呼ぶ。語彙も一緒に返す。"""
    inst, ds, reason = _Vocab(), _Vocab(), _Vocab()
    table = case_rows(list(decisions), list(image_decisions), inst, ds, reason)
    return table, inst, ds, reason


def dropped(*records):
    """D01 相当の自動 exclude を作る。``build_decisions`` は annotation_uid で引く。"""
    return {
        r.annotation_uid: AutomaticDecision(
            geometry_uid=r.geometry_uid,
            decision=Decision.EXCLUDE,
            reason="older_exact_duplicate",
        )
        for r in records
    }


def status_of(row):
    return CASE_STATUSES[row[ST]]


def by_patient(table):
    return {row[PAT]: row for row in table["rows"]}


# --------------------------------------------------------------- 母集団


def test_annotationを持たない患者も行を持つ(config):
    """★これが抜けると画像単位 exclude が表から消える（実データ33枚）。"""
    group = make_group((), case_labels=(NORMAL,))
    images = build_image_decisions([group], [], config)

    table, _, _, _ = build(decisions=(), image_decisions=images)

    row = by_patient(table)["SYNTH00000001"]
    assert row[ANN] == 0
    assert row[IMG] == 1
    assert status_of(row) == "no_annotation"


def test_annotationと画像の両方から患者を集める(config):
    """annotation のある患者と、画像しか無い患者が1つの表に並ぶ。"""
    with_ann = make_record("A")
    only_image = replace(
        make_group((), case_labels=(NORMAL,)),
        study="SYNTH00000002_000",
        patient_id="SYNTH00000002",
        file="SYNTH00000002_000_000_000",
    )
    decisions = build_decisions([with_ann], [], config)
    images = build_image_decisions([make_group((with_ann,)), only_image], [], config)

    table, _, _, _ = build(decisions, images)

    assert set(by_patient(table)) == {"SYNTH00000001", "SYNTH00000002"}


def test_患者が複数studyを持っても1行にまとまる(config):
    first = make_record("A")
    second = replace(
        make_record("B"),
        study="SYNTH00000001_001",
        series="SYNTH00000001_001_000",
        file="SYNTH00000001_001_000_000",
    )
    decisions = build_decisions([first, second], [], config)

    table, _, _, _ = build(decisions)

    rows = table["rows"]
    assert len(rows) == 1
    assert rows[0][STU] == 2
    assert rows[0][IMG] == 2
    assert rows[0][ANN] == 2


# --------------------------------------------------------------- 採否の集計


def test_keepとexcludeが混在する患者はpartial(config):
    """症例状態は case_status() をそのまま使う。ここで再実装しない。"""
    kept, gone = make_record("A"), make_record("B")
    decisions = build_decisions([kept, gone], [], config, automatic=dropped(gone))

    table, _, _, reason = build(decisions)

    row = table["rows"][0]
    assert (row[KEEP], row[EXC]) == (1, 1)
    assert status_of(row) == "partial"
    assert [reason.values[i] for i in row[RSN]] == ["older_exact_duplicate"]


def test_pendingが1件でもあれば要目視(config):
    """安全側。1件でも未確定なら症例全体が未確定。"""
    issues = [make_issue("S03_TINY_REGION", geometry_uid="B")]
    decisions = build_decisions([make_record("A"), make_record("B")], issues, config)

    table, _, _, _ = build(decisions)

    row = table["rows"][0]
    assert (row[KEEP], row[PEND]) == (1, 1)
    assert status_of(row) == "needs_review"


def test_全excludeはall_excluded(config):
    record = make_record("A")
    decisions = build_decisions([record], [], config, automatic=dropped(record))

    table, _, _, _ = build(decisions)

    assert status_of(table["rows"][0]) == "all_excluded"


# --------------------------------------------------- 画像単位の採否は別カラム


def test_画像単位のexcludeはannotationのexcludeと別に数える(config):
    """1817 annotation と 1083 画像は母数が違うので、足して1つにしない。"""
    annotated = make_record("A")
    unannotated = replace(
        make_group(()),
        series="SYNTH00000001_000_001",
        file="SYNTH00000001_000_001_000",
    )
    groups = [make_group((annotated,)), unannotated]
    decisions = build_decisions([annotated], [], config)
    # 未アノテーションのビューを目視で除外した状態（実データの33枚がこれ）。
    # 画像の human decision は file_uid で引く。
    human = {
        unannotated.file_uid: HumanDecision(
            geometry_uid=unannotated.file_uid,
            decision=Decision.EXCLUDE,
            reason="review_required",
        )
    }
    images = build_image_decisions(groups, [], config, human=human)

    table, _, _, reason = build(decisions, images)

    row = table["rows"][0]
    assert row[EXC] == 0, "annotation は除外されていない"
    assert row[IEXC] == 1, "画像1枚が除外されている"
    flagged = row[FLAG]
    assert len(flagged) == 1
    assert flagged[0][0] == "SYNTH00000001_000_001_000"
    assert flagged[0][3] == "exclude"
    assert reason.values[flagged[0][4]] == "review_required"


def test_keepの画像は内訳に載せない(config):
    """keep 1050枚を全部載せると内訳が読めなくなる。"""
    record = make_record("A")
    decisions = build_decisions([record], [], config)
    images = build_image_decisions([make_group((record,))], [], config)

    table, _, _, _ = build(decisions, images)

    assert all(image.final_decision is Decision.KEEP for image in images)
    assert table["rows"][0][FLAG] == []


# --------------------------------------------------------------- 並びと語彙


def test_excludeが多い順に並ぶ(config):
    """「どの施設に exclude が出ているか」を先に見せるため。"""
    quiet = make_record("A")
    noisy = [
        replace(
            make_record(uid),
            study="SYNTH00000002_000",
            series="SYNTH00000002_000_000",
            file="SYNTH00000002_000_000_000",
            patient_id="SYNTH00000002",
        )
        for uid in ("B", "C")
    ]
    decisions = build_decisions([quiet, *noisy], [], config, automatic=dropped(*noisy))

    table, _, _, _ = build(decisions)

    assert [row[PAT] for row in table["rows"]] == [
        "SYNTH00000002",
        "SYNTH00000001",
    ]


def test_施設とデータセットは語彙のインデックスで持つ(config):
    """1817行 × 施設名をそのまま埋め込むとペイロードが膨らむ。"""
    decisions = build_decisions([make_record("A")], [], config)

    table, inst, ds, _ = build(decisions)

    row = table["rows"][0]
    assert inst.values[row[INST]] == INSTITUTION
    assert ds.values[row[DS]] == "SYNTH"


def test_状態のラベルと並びを一緒に返す(config):
    """JS 側で日本語ラベルを持たない（表記を1か所にする）。"""
    table, _, _, _ = build(build_decisions([make_record("A")], [], config))

    assert table["status_order"] == CASE_STATUSES
    assert table["status_labels"]["passed"] == "合格（全keep）"
