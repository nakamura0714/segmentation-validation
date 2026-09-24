"""読影レポートによるラベル補強の規則。

**このファイルが担保するのは「何を陰性の根拠と認めるか」。**
ここを崩すと、未確認の陽性を陰性の教師信号にする（感度が黙って下がる）か、
確認済みの陰性をレポートで塗り潰す。

優先順位: 明示的な陰性/陽性GT > 読影レポート > annotation/mask が無いだけの未確認状態
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import (
    NODULE,
    NORMAL,
    PNEUMOTHORAX,
    make_group,
    make_record,
    make_report_labels,
)

from segmentation_validation.core.labels import Label
from segmentation_validation.lpdata_export.labels import (
    ABSENT,
    PRESENT,
    UNKNOWN,
    CaseEvidence,
    build_labels,
)
from segmentation_validation.lpdata_export.report_labels import (
    ReportLabelError,
    enrich_labels,
    in_scope,
)

#: ブラ / ブレブ。元データは1語に結合されている。
BULLA_BLEB = Label(
    code_system="Findings",
    code="014",
    code_text="ブラ",
    code_text_eng="bulla_bleb",
    confidence=None,
    label_id=8,
)
#: 胸水。
PLEURAL_EFFUSION = Label(
    code_system="Findings",
    code="007",
    code_text="胸水(塗りつぶし）",
    code_text_eng="pleural_effusion",
    confidence=None,
    label_id=7,
)


def base_labels(*, records=(), case_labels=()):
    """既存 annotation から作った初期ラベル。"""
    return build_labels(make_group(records, case_labels=case_labels))


# --------------------------------------------------------- pneumothorax_case


def test_未確認のfalseはレポートpresentでtrueへ補完される():
    """annotation が無いだけの false は「陰性」ではないので補完してよい。"""
    base = base_labels()
    assert base.case_evidence is CaseEvidence.UNCONFIRMED
    assert base.pneumothorax_case is False

    result = enrich_labels(base, make_report_labels(status=PRESENT))

    assert result.labels.pneumothorax_case is True
    assert result.labels.case_evidence is CaseEvidence.REPORT_POSITIVE
    assert [c.field for c in result.changes] == ["pneumothorax_case"]
    assert not result.conflicts


def test_レポートがpresentならマスクが無くてもtrueになる():
    """**気胸ありと気胸マスクありを同一視しない。**

    テンプレートの ``img_0002`` が想定している形（true かつ
    ``pneumothorax_mask`` が ``pixel_array: null``）。
    """
    result = enrich_labels(base_labels(), make_report_labels(status=PRESENT))
    assert result.labels.pneumothorax_case is True


def test_他の所見がアノテーション済みでも明示的陰性とはみなさない():
    """結節を付けたことは「気胸が無い」の根拠にならない（付けていないだけ）。"""
    base = base_labels(records=(make_record("A", labels=(NODULE,)),))
    assert base.abnormal_finding_status == PRESENT
    assert base.case_evidence is CaseEvidence.UNCONFIRMED

    result = enrich_labels(base, make_report_labels(status=PRESENT))
    assert result.labels.pneumothorax_case is True


def test_明示的陰性normalはレポートpresentでも上書きしない():
    """``No Findings/normal`` は確認済みの陰性。レポートで覆さず conflict に出す。"""
    base = base_labels(case_labels=(NORMAL,))
    assert base.case_evidence is CaseEvidence.EXPLICIT_NEGATIVE_NORMAL

    result = enrich_labels(base, make_report_labels(status=PRESENT))

    assert result.labels.pneumothorax_case is False
    assert result.labels.case_evidence is CaseEvidence.EXPLICIT_NEGATIVE_NORMAL
    assert not result.changes
    assert [c.reason for c in result.conflicts] == ["explicit_negative_wins"]


def test_気胸annotationがあればレポートabsentでも降格しない():
    """**下げる方向には一切動かさない。** 実データに1件ある。"""
    base = base_labels(records=(make_record("A", labels=(PNEUMOTHORAX,)),))
    assert base.case_evidence is CaseEvidence.EXPLICIT_POSITIVE_ANNOTATION

    result = enrich_labels(base, make_report_labels(status=ABSENT, certainty_max=None))

    assert result.labels.pneumothorax_case is True
    assert [c.reason for c in result.conflicts] == ["never_downgrade"]


def test_レポートabsentかつ所見0件なら陰性根拠へ格上げされる():
    """値は false のまま。「未確認」から「確認済みの陰性」へ根拠だけが変わる。"""
    base = base_labels()
    result = enrich_labels(
        base,
        make_report_labels(
            status=ABSENT,
            observed_finding_count=0,
            evidence="structured_negative",
            certainty_max=None,
        ),
    )
    assert result.labels.pneumothorax_case is False
    assert result.labels.case_evidence is CaseEvidence.EXPLICIT_NEGATIVE_REPORT


def test_レポートabsentでも他の所見があれば未確認のまま():
    """気胸だけ陰性でも、所見の取りこぼしと区別できないので陰性と断定しない。"""
    result = enrich_labels(
        base_labels(),
        make_report_labels(
            status=ABSENT,
            observed_finding_count=3,
            evidence="structured_negative",
            certainty_max=None,
        ),
    )
    assert result.labels.case_evidence is CaseEvidence.UNCONFIRMED


def test_レポート未取得は陰性根拠にしない():
    """``no_report`` は「所見が無い」ではなく「読んでいない」。"""
    result = enrich_labels(
        base_labels(),
        make_report_labels(
            status=ABSENT,
            observed_finding_count=0,
            evidence="no_report",
            certainty_max=None,
        ),
    )
    assert result.labels.case_evidence is CaseEvidence.UNCONFIRMED


def test_statusがunknownなら何も変えない():
    """``unknown`` は「気胸でない」ではなく「主張していない」。陰性に変換しない。"""
    result = enrich_labels(
        base_labels(),
        make_report_labels(status=UNKNOWN, evidence="no_mention", certainty_max=None),
    )
    assert result.labels.pneumothorax_case is False
    assert result.labels.case_evidence is CaseEvidence.UNCONFIRMED
    assert not result.changes and not result.conflicts


def test_report_labelsがNoneなら何も変えない():
    base = base_labels()
    result = enrich_labels(base, None)
    assert result.labels == base
    assert not result.changes and not result.conflicts


# ------------------------------------------------------------------ certainty


@pytest.mark.parametrize("certainty", ["definite", "probable", "possible", "unlikely"])
def test_certaintyに関係なくpresentならtrueになる(certainty: str):
    """**確信度は判定条件に使わない。** 層別評価のためのメタ情報。"""
    result = enrich_labels(
        base_labels(),
        make_report_labels(
            status=PRESENT,
            certainty_max=certainty,
            certainty_counts=((certainty, 1),),
        ),
    )
    assert result.labels.pneumothorax_case is True


def test_certaintyがdefiniteでもstatusがunknownならtrueにしない():
    """確信度が高くても status が判断していないなら動かさない。"""
    result = enrich_labels(
        base_labels(),
        make_report_labels(
            status=UNKNOWN, certainty_max="definite", evidence="no_mention"
        ),
    )
    assert result.labels.pneumothorax_case is False


# -------------------------------------------------------- pneumothorax_side


def test_sideはnullのときだけ入る():
    result = enrich_labels(
        base_labels(), make_report_labels(status=PRESENT, side="right")
    )
    assert result.labels.pneumothorax_side == "right"


def test_既存のsideはレポートで上書きしない():
    base = base_labels().with_case(
        pneumothorax_case=True, case_evidence=CaseEvidence.EXPLICIT_POSITIVE_ANNOTATION
    )
    from dataclasses import replace

    base = replace(base, pneumothorax_side="left")

    result = enrich_labels(base, make_report_labels(status=PRESENT, side="right"))

    assert result.labels.pneumothorax_side == "left"
    assert [c.reason for c in result.conflicts] == ["side_already_set"]


def test_sideは気胸症例のときだけ入る():
    """``invariants`` の「side は case が true のときだけ」を構造的に満たす。"""
    base = base_labels(case_labels=(NORMAL,))
    result = enrich_labels(base, make_report_labels(status=PRESENT, side="right"))
    assert result.labels.pneumothorax_case is False
    assert result.labels.pneumothorax_side is None


def test_想定外のsideは止める():
    with pytest.raises(ReportLabelError):
        enrich_labels(base_labels(), make_report_labels(status=PRESENT, side="middle"))


# ------------------------------------------------------- bulla_bleb_status


def test_bulla_blebはunknownからだけ上げる():
    result = enrich_labels(
        base_labels(), make_report_labels(status=PRESENT, bulla=PRESENT)
    )
    assert result.labels.bulla_bleb_status == PRESENT


def test_bulla_blebをpresentからabsentへ落とさない():
    base = base_labels(records=(make_record("A", labels=(BULLA_BLEB,)),))
    assert base.bulla_bleb_status == PRESENT

    result = enrich_labels(base, make_report_labels(status=UNKNOWN, bulla=ABSENT))

    assert result.labels.bulla_bleb_status == PRESENT
    assert [c.reason for c in result.conflicts] == ["never_downgrade"]


# --------------------------------------------------- pleural_effusion_status


def test_胸水はunknownからだけ上げる():
    result = enrich_labels(
        base_labels(), make_report_labels(status=PRESENT, effusion=PRESENT)
    )
    assert result.labels.pleural_effusion_status == PRESENT
    assert "pleural_effusion_status" in [c.field for c in result.changes]


def test_胸水をpresentからabsentへ落とさない():
    base = base_labels(records=(make_record("A", labels=(PLEURAL_EFFUSION,)),))
    assert base.pleural_effusion_status == PRESENT

    result = enrich_labels(base, make_report_labels(status=UNKNOWN, effusion=ABSENT))

    assert result.labels.pleural_effusion_status == PRESENT
    assert [c.field for c in result.conflicts] == ["pleural_effusion_status"]
    assert [c.reason for c in result.conflicts] == ["never_downgrade"]


def test_明示正常のabsentはレポートのpresentより優先する():
    """``No Findings/normal`` は確認済みの陰性。レポートで覆さず裁定に残す。

    ``pneumothorax_case`` の ``explicit_negative_wins`` と同じ語彙にそろえてある。
    """
    base = base_labels(case_labels=(NORMAL,))
    assert base.pleural_effusion_status == ABSENT

    result = enrich_labels(base, make_report_labels(status=UNKNOWN, effusion=PRESENT))

    assert result.labels.pleural_effusion_status == ABSENT
    conflict = result.conflicts[0]
    assert conflict.field == "pleural_effusion_status"
    # 両方の根拠が残ること（片方だけだと後から突き合わせられない）。
    assert (conflict.annotation, conflict.report) == (ABSENT, PRESENT)
    assert conflict.kept == ABSENT
    assert conflict.reason == "explicit_negative_wins"


def test_水気胸は胸水フラグを立てない():
    """気胸には含めるが、胸水フラグは根拠を見て独立に判定する。

    上流は水気胸を ``pneumothorax_subtype`` で返し、胸水の観測リストには載せない。
    """
    report = replace(
        make_report_labels(status=PRESENT, effusion=UNKNOWN),
        pneumothorax_subtype="hydropneumothorax",
    )
    result = enrich_labels(base_labels(), report)

    assert result.labels.pneumothorax_case is True
    assert result.labels.pleural_effusion_status == UNKNOWN


def test_胸水はレポートが黙っていれば動かない():
    base = base_labels()
    assert base.pleural_effusion_status == UNKNOWN

    result = enrich_labels(base, make_report_labels(status=PRESENT, effusion=UNKNOWN))

    assert result.labels.pleural_effusion_status == UNKNOWN


# --------------------------------------------------------------- 水気胸


def test_水気胸は気胸に含める():
    """上流は水気胸を ``status: present`` + ``subtype: hydropneumothorax`` で返す。

    このモジュールは subtype を見ないので、そのまま気胸症例になる
    （2026-09-24 に確定した方針。除外したくなったら分析用CSVの subtype で引く）。
    """
    report = replace(
        make_report_labels(status=PRESENT),
        pneumothorax_subtype="hydropneumothorax",
    )
    result = enrich_labels(base_labels(), report)

    assert result.labels.pneumothorax_case is True
    assert result.labels.case_evidence is CaseEvidence.REPORT_POSITIVE


def test_水気胸でもin_scopeはstatusだけで決まる():
    report = replace(
        make_report_labels(status=PRESENT),
        pneumothorax_subtype="hydropneumothorax",
    )
    assert in_scope(report, ("present", "absent"))


# -------------------------------------------------- abnormal_finding_status


def test_abnormal_finding_statusはレポートで一切変わらない():
    """上流は policy 未確定で常に ``unknown``。写すと annotation 由来の判定を潰す。"""
    base = base_labels(case_labels=(NORMAL,))
    assert base.abnormal_finding_status == ABSENT

    result = enrich_labels(base, make_report_labels(status=PRESENT, abnormal=UNKNOWN))

    assert result.labels.abnormal_finding_status == ABSENT
    assert all(c.field != "abnormal_finding_status" for c in result.changes)


# ---------------------------------------------------------------- in_scope


def test_in_scopeはstatusのallowlistで判定する():
    assert in_scope(make_report_labels(status=PRESENT), ("present", "absent"))
    assert not in_scope(make_report_labels(status=UNKNOWN), ("present", "absent"))


def test_statusesが空なら常に対象になる():
    assert in_scope(None, ())
    assert in_scope(make_report_labels(status=UNKNOWN), ())


def test_report_labelsが無ければ絞り込み時は対象外():
    assert not in_scope(None, ("present",))


# ------------------------------------------------------------ schema_version


def test_schema_versionが上がったら止まる():
    """上流が形式を変えたら黙って古い解釈を続けない。"""
    with pytest.raises(ReportLabelError):
        enrich_labels(base_labels(), make_report_labels(schema_version=2))


# ----------------------------------------------------------------- 不変条件


def test_補強後も不変条件を満たす():
    from segmentation_validation.lpdata_export.invariants import check_sample

    result = enrich_labels(
        base_labels(), make_report_labels(status=PRESENT, side="bilateral")
    )
    sample = {
        "abnormal_finding_status": result.labels.abnormal_finding_status,
        "bulla_bleb_status": result.labels.bulla_bleb_status,
        "pleural_effusion_status": result.labels.pleural_effusion_status,
        "pneumothorax_case": result.labels.pneumothorax_case,
        "pneumothorax_side": result.labels.pneumothorax_side,
    }
    assert check_sample("s", sample) == []
