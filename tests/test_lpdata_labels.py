"""ラベル4属性の初期生成規則を守る。

**ここが lp-data エクスポートで最も壊れやすい。** 型の間違いは lp-data が弾くが、
ラベルの取り違えは何のエラーも出さず、学習の感度・特異度が静かにずれるだけになる。

特に守りたいのは「安全側に倒す」規則:

- annotation が無いだけでは ``absent`` にしない（未アノテーションと正常例は別物）
- 異常所見ではない annotation（``Difficulty`` / ``Grade`` / ``FP`` 等）を
  ``present`` の根拠にしない
"""

from __future__ import annotations

import pytest
from conftest import NODULE, PNEUMOTHORAX, make_group, make_record

from segmentation_validation.core.labels import Label
from segmentation_validation.lpdata_export.labels import build_labels

BULLA = Label(
    code_system="Findings",
    code="014",
    code_text="ブラ・ブレブ",
    code_text_eng="bulla_bleb",
    confidence=None,
    label_id=14,
)
#: 胸水。``code_text`` は塗りつぶし / 矩形 / 縁取りの3通りあるが
#: ``code_text_eng`` はこの1語に揃っている。
PLEURAL_EFFUSION = Label(
    code_system="Findings",
    code="007",
    code_text="胸水(塗りつぶし）",
    code_text_eng="pleural_effusion",
    confidence=None,
    label_id=7,
)
#: 明示の正常。``normal_evidence`` の既定に一致する唯一のラベル。
NO_FINDINGS_NORMAL = Label(
    code_system="No Findings",
    code="001",
    code_text="正常",
    code_text_eng="normal",
    confidence=None,
    label_id=101,
)


def labels_of(records=(), case_labels=(), **kwargs):
    return build_labels(make_group(records, case_labels=case_labels), **kwargs)


def with_labels(*labels: Label):
    """指定ラベルを持つ geometry annotation を1件だけ持つ画像。"""
    return (make_record("A", labels=labels),)


# ------------------------------------------------------------------ present


def test_Findings注釈があればpresent():
    result = labels_of(with_labels(NODULE))
    assert result.abnormal_finding_status == "present"
    assert result.finding_labels == ("nodule",)


def test_finding_labelsは重複排除してソートされる():
    records = (
        make_record("A", labels=(NODULE, PNEUMOTHORAX)),
        make_record("B", labels=(NODULE,)),
    )
    assert labels_of(records).finding_labels == ("nodule", "pneumothorax")


# ------------------------------------------------------------------- absent


@pytest.mark.parametrize(
    "where",
    ["geometry", "case"],
    ids=["geometryに付いている", "study_series（case_labels）に付いている"],
)
def test_明示のNoFindingsNormalがあればabsent(where):
    """正常の根拠は geometry / series / study のどこに付いていても採る。"""
    if where == "geometry":
        result = labels_of(with_labels(NO_FINDINGS_NORMAL))
    else:
        result = labels_of((), case_labels=(NO_FINDINGS_NORMAL,))
    assert result.abnormal_finding_status == "absent"
    assert result.finding_labels == ()
    assert result.normal_evidence_hits == ("No Findings/normal",)


def test_annotationが無いだけではabsentにしない():
    """★未アノテーションを正常と断定しない。実データの82%がこの経路に入る。"""
    result = labels_of()
    assert result.abnormal_finding_status == "unknown"
    assert result.finding_labels == ()


def test_マスクが無いだけでは正常とみなさない():
    """マスク無しの気胸 annotation（bbox等）でも present のまま。"""
    record = make_record("A", labels=(PNEUMOTHORAX,), has_mask=False)
    result = labels_of((record,))
    assert result.abnormal_finding_status == "present"
    assert result.pneumothorax_case is True


@pytest.mark.parametrize(
    "code_text_eng",
    ["bad_image_unreadable", "findings_in_CT", "other", "non_target_finding"],
)
def test_NoFindingsのnormal以外は正常の根拠にしない(code_text_eng):
    """読影不能・CTでは所見あり・その他は「正常」を意味しない。"""
    label = Label(
        code_system="No Findings",
        code="003",
        code_text="",
        code_text_eng=code_text_eng,
        confidence=None,
        label_id=1,
    )
    assert labels_of((), case_labels=(label,)).abnormal_finding_status == "unknown"


def test_StudyAnnoのabnormalはpresentにしない():
    """present にすると finding_labels が空のままになり不変条件1を破る。"""
    label = Label(
        code_system="StudyAnno",
        code="002",
        code_text="異常",
        code_text_eng="abnormal",
        confidence=None,
        label_id=1,
    )
    assert labels_of((), case_labels=(label,)).abnormal_finding_status == "unknown"


def test_normal_evidenceのallowlistで判定が変わる():
    """``StudyAnno/normal`` は既定では採らないが、設定に足せば absent になる。

    由来が確認できたときに config 1行で切り替えられること（実データで absent が
    853 → 1,500 になる）をここで担保する。
    """
    label = Label(
        code_system="StudyAnno",
        code="001",
        code_text="正常",
        code_text_eng="normal",
        confidence=None,
        label_id=1,
    )
    assert labels_of((), case_labels=(label,)).abnormal_finding_status == "unknown"
    widened = labels_of(
        (),
        case_labels=(label,),
        normal_evidence=["No Findings/normal", "StudyAnno/normal"],
    )
    assert widened.abnormal_finding_status == "absent"


# ------------------------------------------- 異常所見ではない annotation の除外


@pytest.mark.parametrize(
    "code_system,code_text_eng",
    [
        ("Difficulty", "difficulty_03"),
        (" Difficulty", "difficulty_04"),  # 前置空白のタイポが実データにある
        ("Grade", ""),  # code_text_eng を持たない
        ("Location", "lung_apex"),
        ("Body Parts", "body_lung"),
        ("FP", "nipple"),
        ("Disease", "tb_active"),
        ("Disease Evolution", "disease_progress"),
    ],
)
def test_異常所見ではないannotationはpresentの根拠にしない(code_system, code_text_eng):
    label = Label(
        code_system=code_system,
        code="003",
        code_text="",
        code_text_eng=code_text_eng,
        confidence=None,
        label_id=1,
    )
    result = labels_of(with_labels(label))
    assert result.finding_labels == ()
    assert result.abnormal_finding_status == "unknown"


def test_FPラベルは所見に入らない():
    """FP は「偽陽性として明示されたもの」。所見の存在証明ではない。"""
    fp = Label(
        code_system="FP",
        code="001",
        code_text="乳頭",
        code_text_eng="nipple",
        confidence=None,
        label_id=1,
    )
    result = labels_of(with_labels(PNEUMOTHORAX, fp))
    assert result.finding_labels == ("pneumothorax",)


# --------------------------------------------------------- pneumothorax_case


def test_気胸ラベルはcode_text_engで判定する():
    """``Findings/001`` は ``nodule`` と ``pneumothorax`` の両方に使われている。

    ``code`` で判定すると結節が気胸として混入する。
    """
    nodule_001 = Label(
        code_system="Findings",
        code="001",
        code_text="結節",
        code_text_eng="nodule",
        confidence=None,
        label_id=1,
    )
    pneumo_001 = Label(
        code_system="Findings",
        code="001",
        code_text="気胸",
        code_text_eng="pneumothorax",
        confidence=None,
        label_id=2,
    )
    assert labels_of(with_labels(nodule_001)).pneumothorax_case is False
    assert labels_of(with_labels(pneumo_001)).pneumothorax_case is True
    # Findings/010 側の気胸も拾う（データセットによって code が違う）。
    assert labels_of(with_labels(PNEUMOTHORAX)).pneumothorax_case is True


def test_pneumothorax_caseはfinding_labelsの気胸と一致する():
    result = labels_of(with_labels(PNEUMOTHORAX, NODULE))
    assert result.pneumothorax_case is True
    assert "pneumothorax" in result.finding_labels


# ----------------------------------------------------------- 残り2属性


def test_bulla_blebはpresentかunknownだけでabsentを出さない():
    """アノテーション対象だったかを既存 annotation から判定できないため。"""
    assert labels_of(with_labels(BULLA)).bulla_bleb_status == "present"
    assert labels_of(with_labels(NODULE)).bulla_bleb_status == "unknown"
    # 正常と判定されるサンプルでも absent にはしない。
    assert (
        labels_of((), case_labels=(NO_FINDINGS_NORMAL,)).bulla_bleb_status == "unknown"
    )


def test_胸水は3値すべてを出す():
    """**ブラ / ブレブとは規則が違う。** 明示正常のときだけ ``absent`` を出す。

    テンプレートが ``pleural_effusion_status`` を足した 2026-09-24 の変更に対応する。
    """
    assert labels_of(with_labels(PLEURAL_EFFUSION)).pleural_effusion_status == "present"
    # 明示の正常（``No Findings/normal``）は「胸水も無い」と言い切れる唯一の根拠。
    assert (
        labels_of((), case_labels=(NO_FINDINGS_NORMAL,)).pleural_effusion_status
        == "absent"
    )


def test_胸水はアノテーションが無いだけでabsentにしない():
    """未アノテーションと陰性は別物。ここを崩すと未アノテーションを陰性の教師信号にする。"""
    # 他の所見だけが付いている（胸水は付けていないだけ）。
    assert labels_of(with_labels(NODULE)).pleural_effusion_status == "unknown"
    # annotation が1件も無い。
    assert labels_of().pleural_effusion_status == "unknown"


def test_正常ラベルと所見が同居したらabsentにしない():
    """実データに 166 枚ある矛盾。**「所見あり」と「胸水は無い」を同時に言わない。**

    正常ラベルだけを条件にすると、結節を付けた画像まで「胸水は無い」と
    言い切ってしまう。`abnormal_finding_status` は同じ材料から `present` に
    倒れる（所見が勝つ）ので、胸水も陰性とは言い切らず `unknown` に留める。
    """
    result = labels_of(with_labels(NODULE), case_labels=(NO_FINDINGS_NORMAL,))

    assert result.abnormal_finding_status == "present"
    assert result.pleural_effusion_status == "unknown"

    # 胸水そのものが付いていれば、正常ラベルと同居していても present（実データ3枚）。
    both = labels_of(with_labels(PLEURAL_EFFUSION), case_labels=(NO_FINDINGS_NORMAL,))
    assert both.pleural_effusion_status == "present"


def test_胸水は気胸ラベルと独立している():
    """胸水は気胸の偽陽性要因。両方立つ（水気胸など）のも片方だけも正当。"""
    both = labels_of(with_labels(PNEUMOTHORAX, PLEURAL_EFFUSION))
    assert both.pneumothorax_case is True
    assert both.pleural_effusion_status == "present"

    effusion_only = labels_of(with_labels(PLEURAL_EFFUSION))
    assert effusion_only.pneumothorax_case is False
    assert effusion_only.pleural_effusion_status == "present"

    # 気胸だけのサンプルに胸水フラグを立てない（根拠が無い）。
    pneumothorax_only = labels_of(with_labels(PNEUMOTHORAX))
    assert pneumothorax_only.pleural_effusion_status == "unknown"


def test_pneumothorax_sideは常にnull():
    """患者基準の解剖学的左右。画像座標から起こすと全反転する。"""
    assert labels_of(with_labels(PNEUMOTHORAX)).pneumothorax_side is None
    assert labels_of().pneumothorax_side is None
