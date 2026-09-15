"""ラベル属性間の不変条件（med-chest-metry-pi6 PR #68）を守る。

**条件付きである点がこのテストの主目的。** 前身の無条件版は
「unknown ⇒ pneumothorax_case: false」を強制しており、
「気胸ラベルはあるが読影所見は未取得」が表現できず、規則どおりGTを作ると
**感度が黙って下がる**経路になっていた。

初期エクスポートではこの組み合わせは出ない（気胸 annotation があれば必ず present に
なるため）。それでも検査側が許容することをここで固定しておかないと、後日の
読影レポートCSV更新処理を入れた瞬間に正当なデータが違反扱いになる。
"""

from __future__ import annotations

import pytest

from segmentation_validation.lpdata_export.invariants import check_sample, check_samples


def sample(status, labels, case):
    return {
        "abnormal_finding_status": status,
        "finding_labels": list(labels),
        "pneumothorax_case": case,
    }


def rules(violations):
    return {v.rule for v in violations}


# ------------------------------------------------- 初期エクスポートで出る形


@pytest.mark.parametrize(
    "status,labels,case",
    [
        ("present", ["pneumothorax"], True),
        ("present", ["pneumothorax", "nodule"], True),
        ("present", ["nodule"], False),
        ("absent", [], False),
        ("unknown", [], False),
    ],
    ids=[
        "present×気胸",
        "present×気胸と他所見",
        "present×気胸以外",
        "absent",
        "unknown",
    ],
)
def test_初期エクスポートで出る組み合わせは違反しない(status, labels, case):
    assert check_sample("s", sample(status, labels, case)) == []


# --------------------------------------------------------- PR #68 の核心


def test_unknownかつpneumothorax_case_trueかつfinding_labels空は違反にしない():
    """★「気胸ラベルはあるが読影所見は未取得」。公開データセットではこれが主になる。

    初期エクスポートでは出ないが、後日のCSV更新処理が出せる形。
    """
    assert check_sample("s", sample("unknown", [], True)) == []


def test_presentのときだけ気胸ラベルの対応を課す():
    # present で気胸ラベルが無いのに case が真 → 違反
    assert rules(check_sample("s", sample("present", ["nodule"], True))) == {
        "4: present/absent 時の気胸ラベル対応"
    }
    # 同じ不一致でも unknown なら課さない（finding_labels は空なので1/2/3も無違反）
    assert check_sample("s", sample("unknown", [], True)) == []


# ----------------------------------------------------------------- 違反検出


def test_absentかつpneumothorax_case_trueは違反として検出される():
    """正常と判定されているのに気胸症例、という組み合わせは不正。"""
    found = rules(check_sample("s", sample("absent", [], True)))
    assert "5: absent かつ pneumothorax_case: true は不正" in found
    assert "4: present/absent 時の気胸ラベル対応" in found


def test_presentなのにfinding_labelsが空なら違反():
    assert "1: present ⇔ finding_labels 非空" in rules(
        check_sample("s", sample("present", [], False))
    )


@pytest.mark.parametrize("status", ["absent", "unknown"])
def test_present以外でfinding_labelsが非空なら違反(status):
    assert "1/2/3: present 以外は finding_labels 空" in rules(
        check_sample("s", sample(status, ["nodule"], False))
    )


def test_label_map外の値は違反():
    assert "label_map 外の値" in rules(check_sample("s", sample("normal", [], False)))


# ------------------------------------------------------------------ 使い勝手


def test_ラベル属性を持たないサンプルは検査対象外():
    """「持っていない」と「矛盾している」は別物。部分更新を違反にしない。"""
    assert check_sample("s", {"image_file": "images/s.png"}) == []


def test_check_samplesはdictでもペアの列でも受ける():
    samples = {
        "ok": sample("present", ["nodule"], False),
        "ng": sample("absent", [], True),
    }
    from_dict = check_samples(samples)
    from_pairs = check_samples(iter(samples.items()))
    assert [str(v) for v in from_dict] == [str(v) for v in from_pairs]
    assert {v.sample_id for v in from_dict} == {"ng"}
