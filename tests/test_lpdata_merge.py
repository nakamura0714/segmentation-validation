"""2つの lp-data データセットJSONの統合。

**このファイルが担保するのは「重複した画像で何を残すか」。**

- 画素由来の値（``image_file`` / マスク / 面積）は**必ず primary**。
  ここを崩すと人手で整備したGTが黙って差し替わる
- ラベルは補強のみで**下げない**
- ``abnormal_finding_status`` は統合で触らない
- 確信度は裁定に使わない
"""

from __future__ import annotations

import json

import pytest

from segmentation_validation.lpdata_export.merge import (
    MergeError,
    MergeOptions,
    merge_lpdata,
)
from segmentation_validation.lpdata_export.writer import build_dataset, write_dataset

STRUCTURE = {
    "patient_id": {"type": "str"},
    "dicom_file": {"type": "Path"},
    "image_file": {"type": "Path"},
    "pneumothorax_case": {"type": "bool"},
    "abnormal_finding_status": {"type": "str"},
    "pneumothorax_side": {"type": "str"},
    "bulla_bleb_status": {"type": "str"},
    "pneumothorax_mask": {"type": "Mask2D"},
    "pneumothorax_area_pix2": {"type": "int"},
}

META = {
    "content_type": "dataset",
    "dataset_name": "synth",
    "dataset_id": "synth_001",
    "date": "2026-01-01",
    "project": "synth",
    "owner": "tester",
    "structure": STRUCTURE,
}


def meta_with(negatives: list[str] | None = None, **overrides) -> dict:
    """``provenance.case_evidence`` を持つ meta。

    統合は明示的な気胸陰性をここから読む。``abnormal_finding_status`` では
    代用しない（あれは気胸以外も含む異常所見の有無で、意味が違う）。
    """
    provenance = {
        "case_evidence": {
            "schema_version": 1,
            "explicit_negative_normal": negatives or [],
            "explicit_negative_report": [],
        }
    }
    return dict(META, provenance=provenance, **overrides)


def make_sample(
    *,
    case: bool = False,
    status: str = "unknown",
    side: str | None = None,
    bulla: str = "unknown",
    mask: str | None = None,
    area: int | None = None,
    image: str = "images/a.png",
    patient: str = "P1",
) -> dict:
    return {
        "patient_id": patient,
        "dicom_file": "/mnt/medical2/synth/a.dcm",
        "image_file": image,
        "pneumothorax_case": case,
        "abnormal_finding_status": status,
        "pneumothorax_side": side,
        "bulla_bleb_status": bulla,
        "pneumothorax_mask": {"pixel_array": mask},
        "pneumothorax_area_pix2": area,
    }


def write(path, samples, meta=None):
    write_dataset(
        path, build_dataset(meta if meta is not None else meta_with(), samples)
    )
    return path


def run(tmp_path, primary, secondary, negatives=None, **kwargs):
    """``negatives`` は primary 側の明示的な気胸陰性の sample_id。"""
    p = write(tmp_path / "primary.json", primary, meta_with(negatives))
    s = write(tmp_path / "secondary.json", secondary)
    options = MergeOptions(
        primary_path=p,
        secondary_path=s,
        out_path=tmp_path / "out" / "merged.json",
        **kwargs,
    )
    return merge_lpdata(options), options


# ------------------------------------------------------------------ 件数


def test_重複しないサンプルは両方そのまま出る(tmp_path):
    report, options = run(tmp_path, {"a": make_sample()}, {"b": make_sample(case=True)})
    merged = json.loads(options.out_path.read_text(encoding="utf-8"))
    assert set(merged["samples"]) == {"a", "b"}
    assert report.duplicates == 0
    assert report.added_from_secondary == 1


def test_サンプル数は重複を除いた和になる(tmp_path):
    report, _ = run(
        tmp_path,
        {"a": make_sample(), "b": make_sample()},
        {"b": make_sample(), "c": make_sample()},
    )
    assert report.merged_samples == 3
    assert report.duplicates == 1
    assert report.added_from_secondary == 1


# ------------------------------------------------------------ 画素由来の値


def test_重複サンプルはprimaryの画素由来の値を残す(tmp_path):
    """**人手で整備したGTを secondary のもので差し替えない。**"""
    _, options = run(
        tmp_path,
        {"a": make_sample(case=True, mask="masks/dev.png", area=100, image="dev.png")},
        {"a": make_sample(case=True, mask="masks/ofc.png", area=999, image="ofc.png")},
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_mask"]["pixel_array"] == "masks/dev.png"
    assert sample["pneumothorax_area_pix2"] == 100
    assert sample["image_file"] == "dev.png"


def test_マスクの有無が食い違えば記録しprimaryを採る(tmp_path):
    report, options = run(
        tmp_path,
        {"a": make_sample(case=True, mask="masks/dev.png", area=100)},
        {"a": make_sample(case=True)},
    )
    assert len(report.mask_conflicts) == 1
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_mask"]["pixel_array"] == "masks/dev.png"


def test_マスクの面積が食い違えば記録する(tmp_path):
    report, _ = run(
        tmp_path,
        {"a": make_sample(case=True, mask="m.png", area=100)},
        {"a": make_sample(case=True, mask="m.png", area=200)},
    )
    assert len(report.mask_conflicts) == 1


# -------------------------------------------------------- pneumothorax_case


def test_caseはfalseからtrueへ上がる(tmp_path):
    report, options = run(
        tmp_path, {"a": make_sample(case=False)}, {"a": make_sample(case=True)}
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is True
    assert report.enriched == 1


def test_caseはtrueからfalseへ落ちない(tmp_path):
    """**下げる方向には一切動かさない。**"""
    report, options = run(
        tmp_path, {"a": make_sample(case=True)}, {"a": make_sample(case=False)}
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is True
    assert "pneumothorax_case (never_downgrade)" in report.conflict_counts


def test_no_enrichなら補強しない(tmp_path):
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False)},
        {"a": make_sample(case=True, side="left")},
        no_enrich=True,
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is False
    assert sample["pneumothorax_side"] is None


# -------------------------------------------------------- side / bulla_bleb


def test_sideはnullのときだけ埋まる(tmp_path):
    _, options = run(
        tmp_path,
        {"a": make_sample(case=True), "b": make_sample(case=True, side="left")},
        {
            "a": make_sample(case=True, side="right"),
            "b": make_sample(case=True, side="right"),
        },
    )
    samples = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]
    assert samples["a"]["pneumothorax_side"] == "right"
    assert samples["b"]["pneumothorax_side"] == "left"


def test_sideは気胸症例のときだけ埋まる(tmp_path):
    """primary が false のまま（secondary も false）なら side は載らない。"""
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False)},
        {"a": make_sample(case=False, side="right")},
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_side"] is None


def test_bulla_blebはunknownからだけ上がる(tmp_path):
    _, options = run(
        tmp_path,
        {"a": make_sample(bulla="unknown"), "b": make_sample(bulla="present")},
        {"a": make_sample(bulla="present"), "b": make_sample(bulla="absent")},
    )
    samples = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]
    assert samples["a"]["bulla_bleb_status"] == "present"
    assert samples["b"]["bulla_bleb_status"] == "present"


def test_abnormal_finding_statusは統合で変えない(tmp_path):
    """secondary は policy 未確定で常に unknown。写すと判定を潰す。"""
    _, options = run(
        tmp_path,
        {"a": make_sample(status="present")},
        {"a": make_sample(status="unknown")},
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["abnormal_finding_status"] == "present"


# ------------------------------------------------------------------ 同一性


def test_patient_idが食い違えば記録し値は変えない(tmp_path):
    report, options = run(
        tmp_path,
        {"a": make_sample(patient="P1")},
        {"a": make_sample(patient="P2")},
    )
    assert len(report.identity_conflicts) == 1
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["patient_id"] == "P1"


# ------------------------------------------------------------------ 構造


def test_structureが食い違えば統合しない(tmp_path):
    other_meta = dict(META, structure={**STRUCTURE, "extra": {"type": "str"}})
    p = write(tmp_path / "primary.json", {"a": make_sample()})
    s = write(tmp_path / "secondary.json", {"b": make_sample()}, meta=other_meta)
    with pytest.raises(MergeError, match="structure"):
        merge_lpdata(
            MergeOptions(
                primary_path=p, secondary_path=s, out_path=tmp_path / "out.json"
            )
        )


def test_structureに無いキーを持つサンプルは弾く(tmp_path):
    broken = make_sample()
    broken["surprise"] = 1
    p = write(tmp_path / "primary.json", {"a": broken})
    s = write(tmp_path / "secondary.json", {"b": make_sample()})
    with pytest.raises(MergeError, match="食い違う"):
        merge_lpdata(
            MergeOptions(
                primary_path=p, secondary_path=s, out_path=tmp_path / "out.json"
            )
        )


# ------------------------------------------------------------------ dry-run


def test_dry_runではデータセットJSONを書かない(tmp_path):
    report, options = run(
        tmp_path, {"a": make_sample()}, {"a": make_sample(case=True)}, dry_run=True
    )
    assert not options.out_path.exists()
    assert report.merged_samples == 1


def test_dry_runでも要約と裁定一覧は書く(tmp_path):
    """**統合前に人が裁定を確認するための経路。**"""
    report, _ = run(
        tmp_path, {"a": make_sample()}, {"a": make_sample(case=True)}, dry_run=True
    )
    summary = report.artifacts["summary"].read_text(encoding="utf-8")
    assert "DRY RUN" in summary
    payload = json.loads(report.artifacts["resolutions"].read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["entries"][0]["sample_id"] == "a"


def test_裁定一覧はcertaintyを裁定に使っていないと明示する(tmp_path):
    report, _ = run(
        tmp_path, {"a": make_sample()}, {"a": make_sample(case=True)}, dry_run=True
    )
    payload = json.loads(report.artifacts["resolutions"].read_text(encoding="utf-8"))
    assert payload["certainty_used_in_resolution"] is False


# -------------------------------------------------------------------- meta


def test_metaのprovenanceに両方の入力とsha256が記録される(tmp_path):
    _, options = run(tmp_path, {"a": make_sample()}, {"b": make_sample()})
    meta = json.loads(options.out_path.read_text(encoding="utf-8"))["meta"]
    inputs = meta["provenance"]["inputs"]
    assert [i["role"] for i in inputs] == ["primary", "secondary"]
    assert all(len(i["sha256"]) == 64 for i in inputs)
    assert meta["provenance"]["merge"]["pixel_fields_from"] == "primary"
    assert meta["provenance"]["label_source"] == "annotations+structured_report"


def test_metaのstructureはprimaryから丸写しされる(tmp_path):
    _, options = run(tmp_path, {"a": make_sample()}, {"b": make_sample()})
    meta = json.loads(options.out_path.read_text(encoding="utf-8"))["meta"]
    assert meta["structure"] == STRUCTURE


def test_統合後も不変条件を満たす(tmp_path):
    report, _ = run(
        tmp_path,
        {"a": make_sample(case=False)},
        {"a": make_sample(case=True, side="bilateral")},
    )
    assert report.violations == []


def test_明示的陰性のcaseはレポートpresentでも上げない(tmp_path):
    """**確認済みの陰性をレポートで塗り潰さない。**

    根拠は ``provenance.case_evidence``。``abnormal_finding_status`` ではない。
    """
    report, options = run(
        tmp_path,
        {"a": make_sample(case=False, status="absent")},
        {"a": make_sample(case=True, side="right")},
        negatives=["a"],
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is False
    # side も載らない（case が false のままなので）。
    assert sample["pneumothorax_side"] is None
    assert "pneumothorax_case (explicit_negative_wins)" in report.conflict_counts
    assert report.protected == 1


def test_status_presentでも明示的陰性なら上げない(tmp_path):
    """実データ167件の形の再現。

    ``No Findings/normal`` があっても他の所見が併存すると
    ``abnormal_finding_status`` は ``present`` になる。旧実装はここを
    ``status == "absent"`` で判定していたので**素通りしていた**。
    """
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False, status="present")},
        {"a": make_sample(case=True, side="right")},
        negatives=["a"],
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is False


def test_status_absentでも明示的陰性に無ければ上げる(tmp_path):
    """``abnormal_finding_status`` は判定に使っていないことの確認。"""
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False, status="absent")},
        {"a": make_sample(case=True, side="right")},
        negatives=[],
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is True


def test_未確認のcaseはレポートpresentで上がる(tmp_path):
    """``unconfirmed`` は「陰性」ではなく「確認できていない」。ここは上げてよい。"""
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False, status="unknown")},
        {"a": make_sample(case=True, side="right")},
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is True
    assert sample["pneumothorax_side"] == "right"


def test_所見ありでも気胸未確認なら上がる(tmp_path):
    """他の所見がアノテーション済みなのは気胸の陰性根拠にならない。"""
    _, options = run(
        tmp_path,
        {"a": make_sample(case=False, status="present")},
        {"a": make_sample(case=True)},
    )
    sample = json.loads(options.out_path.read_text(encoding="utf-8"))["samples"]["a"]
    assert sample["pneumothorax_case"] is True


# ------------------------------------------------------------ case_evidence


def test_case_evidenceが無いprimaryは止まる(tmp_path):
    """根拠が無いまま昇格するのが、この仕組みで直している障害そのもの。"""
    p = write(tmp_path / "primary.json", {"a": make_sample()}, META)
    s = write(tmp_path / "secondary.json", {"a": make_sample(case=True)})
    with pytest.raises(MergeError, match="case_evidence"):
        merge_lpdata(
            MergeOptions(
                primary_path=p, secondary_path=s, out_path=tmp_path / "out.json"
            )
        )


def test_case_evidenceが無くてもno_enrichなら通る(tmp_path):
    """ラベルを触らないなら根拠は要らない。古い出力の統合だけはできる。"""
    p = write(tmp_path / "primary.json", {"a": make_sample()}, META)
    s = write(tmp_path / "secondary.json", {"b": make_sample()})
    report = merge_lpdata(
        MergeOptions(
            primary_path=p,
            secondary_path=s,
            out_path=tmp_path / "out" / "m.json",
            no_enrich=True,
        )
    )
    assert report.merged_samples == 2


def test_case_evidenceのschema_versionが未知なら止まる(tmp_path):
    meta = meta_with()
    meta["provenance"]["case_evidence"]["schema_version"] = 99
    p = write(tmp_path / "primary.json", {"a": make_sample()}, meta)
    s = write(tmp_path / "secondary.json", {"a": make_sample(case=True)})
    with pytest.raises(MergeError, match="形式版"):
        merge_lpdata(
            MergeOptions(
                primary_path=p, secondary_path=s, out_path=tmp_path / "out.json"
            )
        )


def test_secondaryにcase_evidenceが無くても止まらない(tmp_path):
    """secondary は補強の材料であって保護対象ではない。"""
    p = write(tmp_path / "primary.json", {"a": make_sample()}, meta_with())
    s = write(tmp_path / "secondary.json", {"a": make_sample(case=True)}, META)
    report = merge_lpdata(
        MergeOptions(
            primary_path=p, secondary_path=s, out_path=tmp_path / "out" / "m.json"
        )
    )
    assert report.enriched == 1
