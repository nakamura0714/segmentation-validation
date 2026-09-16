"""ラベル属性間の不変条件を守る。

正典は med-chest-metry-pi6 の `dataset_template_pneumothorax.yaml`。
``feat(data)!: finding_labels を削除しラベルを独立属性に整理する``（2026-09-16）で
**ラベル属性どうしの結合が外れた**ので、このテストの主目的は
「**由来の違う属性を勝手に結び付けていないこと**」の担保になる。

前版は結び付けており、「unknown ⇒ pneumothorax_case: false」を強制していた。
規則どおりGTを作ると**感度が黙って下がる**経路で、テンプレート側もそれを理由に
結合を外している。ここで「正当な組み合わせが違反にならない」ことを固定しておかないと、
後日の読影レポートCSV更新を入れた瞬間に正当なデータが違反扱いになる。
"""

from __future__ import annotations

import pytest

from segmentation_validation.lpdata_export.invariants import check_sample, check_samples


def sample(**overrides):
    base = {
        "abnormal_finding_status": "unknown",
        "pneumothorax_case": False,
        "pneumothorax_side": None,
        "bulla_bleb_status": "unknown",
    }
    return base | overrides


def rules(violations):
    return {v.rule for v in violations}


# ------------------------------------------------- 結合しないことの担保


@pytest.mark.parametrize(
    "status,case",
    [
        ("present", True),
        ("present", False),
        ("absent", False),
        ("unknown", False),
        # ★どちらもテンプレートが明示的に正当とする組み合わせ。
        #   気胸の有無は気胸アノテーション由来、所見の有無は読影所見由来で、由来が違う。
        ("unknown", True),
        ("absent", True),
    ],
    ids=[
        "present×気胸",
        "present×気胸なし",
        "absent",
        "unknown",
        "unknown×気胸（読影所見が未取得）",
        "absent×気胸（由来が違うので結び付けない）",
    ],
)
def test_statusとcaseの組み合わせは結び付けない(status, case):
    assert (
        check_sample(
            "s", sample(abnormal_finding_status=status, pneumothorax_case=case)
        )
        == []
    )


def test_マスク未アノテーションの気胸症例は正当():
    """``pneumothorax_case: true`` かつマスクが空、はテンプレートが明示的に認める形。"""
    entry = sample(pneumothorax_case=True) | {
        "pneumothorax_mask": {"pixel_array": None}
    }

    assert check_sample("s", entry) == []


# ------------------------------------------------------------ 値の範囲


@pytest.mark.parametrize("key", ["abnormal_finding_status", "bulla_bleb_status"])
def test_label_map外の値は違反(key):
    assert "label_map 外の値" in rules(check_sample("s", sample(**{key: "normal"})))


@pytest.mark.parametrize("value", ["present", "absent", "unknown"])
def test_3値はすべて通る(value):
    assert check_sample("s", sample(abnormal_finding_status=value)) == []
    assert check_sample("s", sample(bulla_bleb_status=value)) == []


# --------------------------------------------------- pneumothorax_side


def test_気胸症例ならsideを持ってよい():
    assert (
        check_sample("s", sample(pneumothorax_case=True, pneumothorax_side="left"))
        == []
    )


def test_気胸症例でないのにsideがあれば違反():
    """由来の違う情報が紛れ込んでいる印。"""
    found = rules(
        check_sample("s", sample(pneumothorax_case=False, pneumothorax_side="left"))
    )

    assert "pneumothorax_side は気胸症例のときだけ" in found


def test_sideの値が範囲外なら違反():
    """患者から見た解剖学的左右。``L`` や ``both`` は受けない。"""
    found = rules(
        check_sample("s", sample(pneumothorax_case=True, pneumothorax_side="both"))
    )

    assert "pneumothorax_side の値" in found


# ------------------------------------------------------------------ 使い勝手


def test_ラベル属性を持たないサンプルは検査対象外():
    """「持っていない」と「矛盾している」は別物。部分更新を違反にしない。"""
    assert check_sample("s", {"image_file": "images/s.png"}) == []


def test_check_samplesはdictでもペアの列でも受ける():
    samples = {
        "ok": sample(),
        "ng": sample(pneumothorax_case=False, pneumothorax_side="right"),
    }
    from_dict = check_samples(samples)
    from_pairs = check_samples(iter(samples.items()))

    assert [str(v) for v in from_dict] == [str(v) for v in from_pairs]
    assert {v.sample_id for v in from_dict} == {"ng"}
