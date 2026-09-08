"""D01（完全一致の重複）の自動採否。

★**実データでは検証できない分岐がここにある。** 実測で
`timestamp` は 1817/1817 が解釈可能、101ペアの tie も 0件なので、
「同値 / 欠損 / 解釈不能 → 自動決定せず目視へ」は実データで1件も発火しない。
再エクスポートや別データセットでは必ず起こるので、ここで担保する。
"""

from __future__ import annotations

from conftest import DATASET, NODULE, make_pair, make_record

from segmentation_validation.selection.automatic import (
    BROKEN_MASK,
    UNDECIDABLE_MISSING,
    UNDECIDABLE_TIE,
    build_automatic_decisions,
    merge_automatic,
    summarize_automatic,
)
from segmentation_validation.selection.decisions import (
    AutomaticDecision,
    Decision,
    Reason,
)

OLD = "2026-01-01 00:00:00+00:00"
NEW = "2026-06-01 00:00:00+00:00"

# build_automatic_decisions のキーは annotation_uid（f"{dataset_id}::{geometry_uid}"）。
KEY_A = f"{DATASET}::A"
KEY_B = f"{DATASET}::B"
KEY_C = f"{DATASET}::C"


def test_新しい方をkeepし古い方をexcludeする(config):
    records = [make_record("A", timestamp=OLD), make_record("B", timestamp=NEW)]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )

    assert undecidable == {}
    assert decisions[KEY_A].decision is Decision.EXCLUDE
    assert decisions[KEY_A].reason == Reason.OLDER_EXACT_DUPLICATE.value
    assert decisions[KEY_A].kept_geometry_uid == "B"
    assert decisions[KEY_B].decision is Decision.KEEP


def test_geometry_uidの大小ではなくtimestampで決める(config):
    """UID の並びと時刻の並びが逆でも、新しい方（= UID が小さい方）が残る。"""
    records = [make_record("A", timestamp=NEW), make_record("B", timestamp=OLD)]
    decisions, _ = build_automatic_decisions(records, [make_pair("A", "B")], config)

    assert decisions[KEY_A].decision is Decision.KEEP
    assert decisions[KEY_B].decision is Decision.EXCLUDE


def test_timestampが同値なら自動決定せず目視へ回す(config):
    """★実データ0件。tie では「新しい方」が一意に決まらない。"""
    records = [make_record("A", timestamp=OLD), make_record("B", timestamp=OLD)]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )

    assert decisions == {}
    assert undecidable == {"A": UNDECIDABLE_TIE, "B": UNDECIDABLE_TIE}


def test_timestampが欠損なら自動決定せず目視へ回す(config):
    """★実データ0件。"""
    records = [make_record("A", timestamp=None), make_record("B", timestamp=NEW)]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )

    assert decisions == {}
    assert undecidable == {"A": UNDECIDABLE_MISSING, "B": UNDECIDABLE_MISSING}


def test_timestampが解釈不能なら自動決定せず目視へ回す(config):
    """★実データ0件。壊れた文字列を「欠損」と同じ扱いにする。"""
    records = [make_record("A", timestamp="2026/01/01 broken"), make_record("B")]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )

    assert decisions == {}
    assert set(undecidable.values()) == {UNDECIDABLE_MISSING}


def test_3件のグループなら最新1件だけ残す(config):
    """実データにサイズ3の重複グループが3件ある（連結成分で束ねる必要がある）。"""
    records = [
        make_record("A", timestamp="2026-01-01 00:00:00+00:00"),
        make_record("B", timestamp="2026-02-01 00:00:00+00:00"),
        make_record("C", timestamp="2026-03-01 00:00:00+00:00"),
    ]
    # A≒B, B≒C しか与えない。連結成分で A/B/C が1グループになること。
    pairs = [make_pair("A", "B"), make_pair("B", "C")]
    decisions, undecidable = build_automatic_decisions(records, pairs, config)

    assert undecidable == {}
    kept = [uid for uid, d in decisions.items() if d.decision is Decision.KEEP]
    assert kept == [KEY_C]
    assert decisions[KEY_A].decision is Decision.EXCLUDE
    assert decisions[KEY_B].decision is Decision.EXCLUDE
    assert decisions[KEY_A].kept_geometry_uid == "C"


def test_ラベルが違う完全一致は自動採否しない(config):
    """D02。同一 geometry に別ラベルが付いているので、機械では決められない。"""
    records = [
        make_record("A", timestamp=OLD),
        make_record("B", timestamp=NEW, labels=(NODULE,)),
    ]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B", same_label_keys=False)], config
    )

    assert decisions == {}
    assert undecidable == {}


def test_完全一致でない重複は自動採否しない(config):
    """D03 / D04 は目視。閾値を超えていても機械では決めない。"""
    records = [make_record("A", timestamp=OLD), make_record("B", timestamp=NEW)]
    pairs = [make_pair("A", "B", pixel_identical=False, iou=0.97)]
    decisions, _ = build_automatic_decisions(records, pairs, config)

    assert decisions == {}


def test_壊れたマスクのexcludeがD01のkeepに勝つ():
    """D01 で「新しいから残す」と判定された側が壊れていたら落とす。"""
    d01_keep = {
        "A": AutomaticDecision(
            geometry_uid="A", decision=Decision.KEEP, reason="newest"
        )
    }
    broken = {
        "A": AutomaticDecision(
            geometry_uid="A", decision=Decision.EXCLUDE, reason=BROKEN_MASK
        )
    }

    # 順序を入れ替えても exclude が勝つこと。
    for sources in ((d01_keep, broken), (broken, d01_keep)):
        merged = merge_automatic(*sources)
        assert merged["A"].decision is Decision.EXCLUDE
        assert merged["A"].reason == BROKEN_MASK


def test_集計は重複グループ数をkeepの件数で数える(config):
    records = [make_record("A", timestamp=OLD), make_record("B", timestamp=NEW)]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )
    summary = summarize_automatic(decisions, undecidable)

    assert summary == {
        "automatic_exclude": 1,
        "automatic_keep": 1,
        "undecidable": 0,
        "duplicate_groups": 1,
    }
