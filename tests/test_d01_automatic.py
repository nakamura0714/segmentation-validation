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
    DecisionSource,
    HumanDecision,
    Reason,
    build_decisions,
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
    assert undecidable == {KEY_A: UNDECIDABLE_TIE, KEY_B: UNDECIDABLE_TIE}


def test_timestampが欠損なら自動決定せず目視へ回す(config):
    """★実データ0件。"""
    records = [make_record("A", timestamp=None), make_record("B", timestamp=NEW)]
    decisions, undecidable = build_automatic_decisions(
        records, [make_pair("A", "B")], config
    )

    assert decisions == {}
    assert undecidable == {KEY_A: UNDECIDABLE_MISSING, KEY_B: UNDECIDABLE_MISSING}


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


def test_同じgeometry_uidが別データセットにあっても両方が自動決着する(config):
    """★実データ由来の不具合。PTE/PTR/mask136 は同じ annotation を再エクスポート
    しており、``geometry_uid`` が**データセットを跨いで同じ文字列**になる。

    以前は素の geometry_uid をグルーピングのノードにしていたため、
    「PTE内のペア」と「PTR内のペア」が1グループへ融合し、自動採否が
    どちらか1データセット分にしか付かなかった。残った側は自動判定が無いまま
    D05のTIEで review_required になり pending で滞留する（実測84 geometry_uid /
    234 annotation）。両データセットが独立に決着することを固定する。
    """
    other, other_json = "OTHER", "other.json"
    records = [
        make_record("A", timestamp=OLD),
        make_record("B", timestamp=NEW),
        make_record("A", dataset_id=other, source_json=other_json, timestamp=OLD),
        make_record("B", dataset_id=other, source_json=other_json, timestamp=NEW),
    ]
    pairs = [make_pair("A", "B"), make_pair("A", "B", source_json=other_json)]
    decisions, undecidable = build_automatic_decisions(records, pairs, config)

    assert undecidable == {}
    assert set(decisions) == {KEY_A, KEY_B, f"{other}::A", f"{other}::B"}
    # 古い方（A）は**どちらのデータセットでも**exclude になる。
    assert decisions[KEY_A].decision is Decision.EXCLUDE
    assert decisions[f"{other}::A"].decision is Decision.EXCLUDE
    assert decisions[KEY_B].decision is Decision.KEEP
    assert decisions[f"{other}::B"].decision is Decision.KEEP
    # グループが融合していないこと（融合すると group_size が 4 になる）。
    assert all(d.detail["group_size"] == 2 for d in decisions.values())
    assert (
        decisions[KEY_A].duplicate_group_id
        != decisions[f"{other}::A"].duplicate_group_id
    )


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


# ------------------------------------------- 手動判定は修正の前後で変わらない


def _cross_dataset_case():
    """クロスデータセット再エクスポートの最小形。

    2データセット（SYNTH / OTHER）が同じ画像の同じ annotation（A=古い, B=新しい）を
    それぞれ持つ。修正前は自動採否が片方のデータセットにしか付かなかった。
    """
    other, other_json = "OTHER", "other.json"
    records = [
        make_record("A", timestamp=OLD),
        make_record("B", timestamp=NEW),
        make_record("A", dataset_id=other, source_json=other_json, timestamp=OLD),
        make_record("B", dataset_id=other, source_json=other_json, timestamp=NEW),
    ]
    pairs = [make_pair("A", "B"), make_pair("A", "B", source_json=other_json)]
    return records, pairs, other


def _by_annotation_uid(decisions):
    return {f"{d.dataset_id}::{d.geometry_uid}": d for d in decisions}


def test_手動判定は自動採否の有無で件数もannotation_uidも変わらない(config):
    """★修正で自動 exclude が増えても、**既存の手動判定は最優先で保持**される。

    「修正前」＝自動採否が付かなかった状態（``automatic={}``）、
    「修正後」＝``build_automatic_decisions`` が両データセット分を返す状態、
    として同じ入力で採否を組み、**手動判定の件数と annotation_uid 集合、
    および各手動判定の結論**が完全に一致することを固定する。
    """
    records, pairs, other = _cross_dataset_case()
    # 古い方（A）を人間が keep と判定済み。自動採否は exclude を出すので真っ向から
    # 食い違う組み合わせにして、人間側が勝つことを見る。
    human = {
        f"{DATASET}::A": HumanDecision(
            geometry_uid="A", decision=Decision.KEEP, reason="visually_valid",
            reviewer="reviewer@example.com", dataset_id=DATASET,
        ),
        f"{other}::B": HumanDecision(
            geometry_uid="B", decision=Decision.EXCLUDE, reason="invalid_annotation",
            reviewer="reviewer@example.com", dataset_id=other,
        ),
    }

    automatic, _ = build_automatic_decisions(records, pairs, config)
    before = build_decisions(records, [], config, {}, human)  # 修正前（自動採否なし）
    after = build_decisions(records, [], config, automatic, human)  # 修正後

    def manual(decisions):
        return {
            uid: (d.final_decision, d.reason)
            for uid, d in _by_annotation_uid(decisions).items()
            if d.decision_source is DecisionSource.HUMAN
        }

    # 件数・annotation_uid 集合・結論のいずれも差分が無い。
    assert set(manual(before)) == set(human)
    assert manual(after) == manual(before)
    assert len(manual(after)) == len(human) == 2

    # 自動 exclude と食い違っても人間の keep が勝ち、覆したことが記録される。
    before_by_uid = _by_annotation_uid(before)
    after_by_uid = _by_annotation_uid(after)
    assert automatic[f"{DATASET}::A"].decision is Decision.EXCLUDE
    assert after_by_uid[f"{DATASET}::A"].final_decision is Decision.KEEP
    assert after_by_uid[f"{DATASET}::A"].overrides_automatic is True

    # 差分は手動判定の無い annotation にしか出ない。
    changed = {
        uid
        for uid, d in after_by_uid.items()
        if d.final_decision is not before_by_uid[uid].final_decision
    }
    assert changed.isdisjoint(human)
    # 修正後は手動判定の無い OTHER::A が自動 exclude になる（これが今回の修正の効果）。
    assert after_by_uid[f"{other}::A"].final_decision is Decision.EXCLUDE
    assert after_by_uid[f"{other}::A"].decision_source is DecisionSource.AUTOMATIC
    assert changed == {f"{other}::A"}


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
