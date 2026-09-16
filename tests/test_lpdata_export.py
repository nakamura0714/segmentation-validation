"""サンプルの組み立てと書き出しを守る。

型の間違いは lp-data が読み込み時に弾くが、**値の作り方の間違いは何も言わずに通る**。
半開区間の取り違え（1px 小さい矩形）、重なるマスクの面積の二重計上、
全ゼロマスクの実体化 —— どれも例外にならないので、ここで固定する。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from conftest import make_group
from test_merged_dataset import write_source

from segmentation_validation.config import Config
from segmentation_validation.core.imageio import save_binary_mask
from segmentation_validation.lpdata_export import (
    ExportOptions,
    export_lpdata,
    read_dataset,
)
from segmentation_validation.lpdata_export.masks import merge_masks
from segmentation_validation.lpdata_export.measure import SampleMeasurement
from segmentation_validation.lpdata_export.sample import (
    FIELD_BUILDERS,
    SampleContext,
    build_sample,
)
from segmentation_validation.selection.decisions import Decision

TEMPLATE_KEYS = sorted(FIELD_BUILDERS)


# ------------------------------------------------------- サンプル組み立て


def context(tmp_path: Path, *, group=None, measurement=None, **kwargs) -> SampleContext:
    group = group if group is not None else make_group()
    return SampleContext(
        group=group,
        labels=_labels(group),
        measurement=measurement
        or SampleMeasurement(sample_id=group.file, file_uid=group.file_uid),
        image_path=kwargs.get("image_path"),
        mask_path=kwargs.get("mask_path"),
        base=tmp_path,
        path_style=kwargs.get("path_style", "relative"),
    )


def _labels(group):
    from segmentation_validation.lpdata_export.labels import build_labels

    return build_labels(group)


def test_structureの全キーがサンプルに存在する(tmp_path: Path):
    """値が無くてもキーは省略しない（lp-data の要求）。"""
    sample = build_sample(context(tmp_path))

    assert sorted(sample) == TEMPLATE_KEYS


def test_リストになる属性は無い(tmp_path: Path):
    """現テンプレートに ``multiple: true`` の属性は無い。

    ``finding_labels`` が削除されたことで、リスト値を持つ属性は1つも無くなった。
    ``multiple: false`` の属性値をリストにすると lp-data が弾く。
    """
    sample = build_sample(context(tmp_path))

    assert [k for k, v in sample.items() if isinstance(v, list)] == []


def test_マスクなしでも属性は消えずpixel_array_nullになる(tmp_path: Path):
    """属性ごと null にすると「マスクというオブジェクトが無い」の意味になり、別物。"""
    sample = build_sample(context(tmp_path))

    for key in ("pneumothorax_mask", "lung_mask", "thorax_mask"):
        assert sample[key] == {"pixel_array": None}


def test_spacingが無ければpixel_spacingはnull(tmp_path: Path):
    group = replace(make_group(), spacing=(None, None))

    assert build_sample(context(tmp_path, group=group))["pixel_spacing"] is None


def test_spacingが0以下ならpixel_spacingはnull(tmp_path: Path):
    """``SpatialResolution`` は正の有限数であること（0・負値は不正）。"""
    group = replace(make_group(), spacing=(0.0, 0.15))

    assert build_sample(context(tmp_path, group=group))["pixel_spacing"] is None


def test_image_shapeはheightとwidthで持つ(tmp_path: Path):
    sample = build_sample(context(tmp_path))

    assert sample["image_shape"] == {"height": 2400, "width": 2000}


def test_view_positionはCRSeriesTypesから取る(tmp_path: Path):
    from segmentation_validation.core.labels import Label

    pa = Label(
        code_system="CRSeriesTypes",
        code="001",
        code_text="正面",
        code_text_eng="PA",
        confidence=None,
        label_id=1,
    )
    group = make_group(case_labels=(pa,))

    assert build_sample(context(tmp_path, group=group))["view_position"] == "PA"
    assert build_sample(context(tmp_path))["view_position"] is None


# ------------------------------------------------------------- lung_rect


def measurement_with(lung=None, thorax=None, **kwargs) -> SampleMeasurement:
    return SampleMeasurement(
        sample_id="s",
        file_uid="u",
        lung_bbox=list(lung) if lung else None,
        thorax_bbox=list(thorax) if thorax else None,
        lung_mask_path="/ref/lung.png" if lung else None,
        thorax_mask_path="/ref/thorax.png" if thorax else None,
        **kwargs,
    )


def test_lung_rectは半開区間になる():
    """``mask_stats.bbox`` は閉区間、lp-data の ``Rect`` は ``x_max`` を含まない。

    変換を忘れても lp-data は例外を出さず、1px 小さい矩形が黙って通る。
    """
    rect = measurement_with(
        lung=(10, 20, 99, 119), thorax=(10, 20, 99, 119)
    ).lung_rect()

    assert rect == {"x_min": 10, "y_min": 20, "x_max": 100, "y_max": 120}


def test_lung_rectは両方のマスクを包含する():
    rect = measurement_with(lung=(30, 40, 60, 70), thorax=(10, 50, 90, 65)).lung_rect()

    assert rect == {"x_min": 10, "y_min": 40, "x_max": 91, "y_max": 71}


@pytest.mark.parametrize(
    "lung,thorax",
    [((10, 20, 30, 40), None), (None, (10, 20, 30, 40)), (None, None)],
    ids=["肺野だけ", "胸郭だけ", "どちらも無い"],
)
def test_lung_rectは両方のマスクが要る(lung, thorax):
    """片方で代用すると、由来の違う矩形が同じ属性に混ざる（テンプレートの規約）。"""
    assert measurement_with(lung=lung, thorax=thorax).lung_rect() is None


def test_参照マスクは絶対パスのまま記録される(tmp_path: Path):
    sample = build_sample(
        context(
            tmp_path,
            measurement=measurement_with(lung=(1, 2, 3, 4), thorax=(1, 2, 3, 4)),
        )
    )

    assert sample["lung_mask"] == {"pixel_array": "/ref/lung.png"}
    assert sample["thorax_mask"] == {"pixel_array": "/ref/thorax.png"}


# ------------------------------------------------------------ マスク合成


def write_mask(path: Path, shape=(10, 10), box=None) -> Path:
    mask = np.zeros(shape, dtype=bool)
    if box:
        y0, y1, x0, x1 = box
        mask[y0:y1, x0:x1] = True
    return save_binary_mask(path, mask)


def test_複数の気胸マスクはOR合成してから面積を数える(tmp_path: Path):
    """各マスクの面積を足すと、重なった分が二重計上になる。"""
    a = write_mask(tmp_path / "a.png", box=(0, 4, 0, 4))  # 16px
    b = write_mask(tmp_path / "b.png", box=(2, 6, 2, 6))  # 16px、うち4px重なる

    merged = merge_masks([a, b])

    assert int(merged.mask.sum()) == 28  # 16 + 16 - 4
    assert merged.sources == [str(a), str(b)]


def test_全ゼロのマスクはマスク無しとして扱う(tmp_path: Path):
    """実体を置くマスクは非ゼロ領域を持つものに限る（テンプレートの規約）。"""
    empty = write_mask(tmp_path / "empty.png")

    assert merge_masks([empty]).mask is None


def test_形の違うマスクは合成せずエラーに残す(tmp_path: Path):
    """リサイズで辻褄を合わせるとGTの座標が黙ってずれる。"""
    a = write_mask(tmp_path / "a.png", shape=(10, 10), box=(0, 4, 0, 4))
    b = write_mask(tmp_path / "b.png", shape=(20, 20), box=(0, 4, 0, 4))

    merged = merge_masks([a, b])

    assert int(merged.mask.sum()) == 16  # a だけ
    assert merged.sources == [str(a)]
    assert len(merged.errors) == 1 and "形が合わない" in merged.errors[0]


def test_読めないマスクはエラーに残して合成を続ける(tmp_path: Path):
    a = write_mask(tmp_path / "a.png", box=(0, 4, 0, 4))

    merged = merge_masks([tmp_path / "missing.png", a])

    assert int(merged.mask.sum()) == 16
    assert len(merged.errors) == 1


# --------------------------------------------------------------- 面積


def test_spacingが無ければarea_mm2はnull(tmp_path: Path):
    measurement = SampleMeasurement(
        sample_id="s", file_uid="u", pneumothorax_pixels=100, pneumothorax_area_mm2=None
    )
    sample = build_sample(context(tmp_path, measurement=measurement))

    assert sample["pneumothorax_area_pix2"] == 100
    assert sample["pneumothorax_area_mm2"] is None


# ------------------------------------------------------- end-to-end


def make_template(tmp_path: Path) -> Path:
    """本物と同じ structure（キーだけ）を持つ最小テンプレート。"""
    structure = {
        key: (
            {"type": "str", "multiple": True}
            if key == "finding_labels"
            else {"type": "str"}
        )
        for key in FIELD_BUILDERS
    }
    payload = {
        "meta": {
            "content_type": "dataset",
            "dataset_name": "t",
            "dataset_id": "t_001",
            "date": "2026-07-25",
            "project": "chest_metry_pi6",
            "owner": "<owner>",
            "target": "pneumothorax",
            "structure": structure,
        },
        "samples": {},
    }
    path = tmp_path / "template.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def merge_sources(tmp_path: Path, trees: dict[str, dict]) -> Path:
    """``build_dataset`` の統合処理を通して統合JSONを作る。

    file entry にだけ ``dataset_id`` が付く形をここで再現する（統合処理の実装を
    再実装しないよう、``selection/build_dataset.py`` をそのまま通す）。
    """
    from segmentation_validation.selection import build_dataset as bd

    merged = bd.new_merged_payload()
    result = bd.MergeResult()
    config = Config()
    for dataset_id, tree in trees.items():
        source = write_source(tmp_path, dataset_id, tree)
        # 採否が付いていない annotation は build_development_payload に捨てられる。
        # ここでは採否の検証をしないので、全件 keep にしておく。
        by_uid = {
            uid: Decision.KEEP.value
            for studies in tree.values()
            for series in studies.values()
            for files in series.values()
            for uids in files.values()
            for uid in uids
        }
        payload, _ = bd.build_development_payload(
            source, by_uid, tmp_path / f"{dataset_id}.json", config, image_decisions={}
        )
        bd.merge_into(merged, payload, dataset_id, result)
    bd.finalize_merged(merged, result, {})
    path = tmp_path / "development_merged.json"
    path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return path


def patch_annotations(
    path: Path, labels_by_file: dict[str, list[dict]] | None = None
) -> None:
    """統合JSONの annotation を、アダプタが読める最小限まで埋める。

    ``write_source`` は統合処理のテスト用なので ``geometry_uid`` と
    ``annotation_type`` しか作らない。アダプタは ``geometry_id`` を必須にしており、
    ラベルも付けないので、ラベル依存のテストはここで足す。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    labels_by_file = labels_by_file or {}
    geometry_id = 0
    for studies in payload["dataset"].values():
        for study in studies.values():
            for series in study["series_list"].values():
                for file_id, entry in series["file_list"].items():
                    for annotation in entry["annotations"]:
                        geometry_id += 1
                        annotation.setdefault("geometry_id", geometry_id)
                        annotation["labels"] = labels_by_file.get(file_id, [])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def finding(code_text_eng: str, code: str = "010") -> dict:
    return {
        "label_id": 1,
        "code": code,
        "code_system": "Findings",
        "code_text": code_text_eng,
        "code_text_eng": code_text_eng,
        "confidence": None,
    }


def make_merged(tmp_path: Path) -> Path:
    """気胸ありの DS_A と、annotation を持たない DS_B。"""
    path = merge_sources(
        tmp_path,
        {
            "DS_A": {"inst": {"ST1": {"SE1": {"F1": ["u1"]}}}},
            "DS_B": {"inst": {"ST2": {"SE2": {"F2": []}}}},
        },
    )
    patch_annotations(path, {"F1": [finding("pneumothorax")]})
    return path


DEFAULTS = {
    "dataset_name": "n",
    "dataset_id": "i",
    "owner": "o",
    "image_mode": "none",
    "mask_mode": "none",
    "measure": False,
}


def export(tmp_path: Path, **overrides):
    out = tmp_path / "out"
    merged = overrides.pop("merged_path", None) or make_merged(tmp_path)
    options = ExportOptions(
        merged_path=merged,
        template_path=make_template(tmp_path),
        out_path=out / "ds.json",
        image_output_dir=out / "images",
        mask_output_dir=out / "masks",
        **(DEFAULTS | overrides),
    )
    return export_lpdata(Config(), options), options


def test_統合JSONを読んでlp_data形式で書き出せる(tmp_path: Path):
    result, options = export(tmp_path)

    dataset = read_dataset(options.out_path)
    assert result.samples == 2
    assert sorted(dataset["samples"]) == ["F1", "F2"]
    assert dataset["meta"]["content_type"] == "dataset"
    # 全サンプルが structure と同じキー集合を持つ（lp-data の要求）。
    keys = set(dataset["meta"]["structure"])
    assert all(set(s) == keys for s in dataset["samples"].values())


def test_ラベルのある画像とない画像が別々に判定される(tmp_path: Path):
    result, options = export(tmp_path)

    samples = read_dataset(options.out_path)["samples"]
    assert samples["F1"]["abnormal_finding_status"] == "present"
    assert samples["F1"]["pneumothorax_case"] is True
    # annotation が無いだけでは absent にしない。
    assert samples["F2"]["abnormal_finding_status"] == "unknown"
    assert samples["F2"]["pneumothorax_case"] is False
    assert result.status_counts == {"present": 1, "unknown": 1}
    # 所見語彙は出力属性ではないが、summary 用に集計されている。
    assert result.finding_vocabulary == {"pneumothorax": 1}


def test_統合JSONのfile単位dataset_idが引き当てに使われる(tmp_path: Path):
    """統合JSONは file entry にだけ由来を持つ。ここを取りこぼすと、
    データセット単位の集計がすべて1つに潰れる。
    """
    path = merge_sources(
        tmp_path,
        {
            "DS_pneumothorax": {"inst": {"ST1": {"SE1": {"F1": []}}}},
            "DS_other": {"inst": {"ST2": {"SE2": {"F2": []}}}},
        },
    )
    patch_annotations(path)

    result, _ = export(tmp_path, merged_path=path)

    # データセット名由来の followup が file 単位の dataset_id で振り分けられること。
    assert result.followups["pneumothorax_named_without_annotation"] == {
        "DS_pneumothorax": 1
    }


def test_sample_idはimage_fileのstemと一致する(tmp_path: Path):
    """学習側の ``tests/test_dataset_template.py`` がこの一致を検査する。"""
    _, options = export(tmp_path, image_mode="planned")

    dataset = read_dataset(options.out_path)
    for sample_id, sample in dataset["samples"].items():
        assert Path(sample["image_file"]).stem == sample_id


def test_書き出したJSONをreaderで読み戻せる(tmp_path: Path):
    """後日の読影レポートCSV更新処理はこの往復の上に乗る。"""
    _, options = export(tmp_path)

    dataset = read_dataset(options.out_path)

    assert dataset["meta"]["provenance"]["label_source"] == "annotations"
    assert dataset["meta"]["provenance"]["measured"] is False


def test_不変条件違反が無いこと(tmp_path: Path):
    result, _ = export(tmp_path)

    assert result.violations == []


def test_summaryが書かれる(tmp_path: Path):
    result, _ = export(tmp_path)

    summary = result.artifacts["summary"].read_text(encoding="utf-8")
    assert "lp-data エクスポート結果" in summary
    # 下見モードなので警告が出ていること。
    assert "学習には使わないこと" in summary


def test_placeholderが残っていたら書き出さない(tmp_path: Path):
    from segmentation_validation.lpdata_export import ExportError

    with pytest.raises(ExportError, match="ダミー値が解決されていない"):
        export(tmp_path, owner=None)

    assert not (tmp_path / "out" / "ds.json").exists()


def test_sample_idが重複したら止まる(tmp_path: Path):
    """1画像ぶんのGTを黙って上書きしない。"""
    from segmentation_validation.lpdata_export.export import ExportError

    merged = json.loads((make_merged(tmp_path)).read_text())
    # 別 institution に同じ file_id を作る（統合JSONでは起きない想定の破れ）。
    merged["dataset"]["other"] = {
        "ST9": {
            "patient_id": "p",
            "study_name": "ST9",
            "study_date": "2026-01-01",
            "series_list": {
                "SE9": {
                    "spacing": {"x": 0.15, "y": 0.15, "z": 1.0},
                    "shape": {"w": 10, "h": 10, "z": 1},
                    "manufacturer": "SYNTH",
                    "file_list": {
                        "F1": {"image_path": "medical2/x/F1.dcm", "annotations": []}
                    },
                    "groups": [],
                }
            },
        }
    }
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ExportError, match="sample_id が重複"):
        export(tmp_path, merged_path=path)


def test_only_datasetで絞れる(tmp_path: Path):
    result, options = export(tmp_path, only_datasets=("DS_A",))

    assert sorted(read_dataset(options.out_path)["samples"]) == ["F1"]
    assert result.samples == 1


@pytest.mark.parametrize(
    "dataset_id,expected",
    [
        ("PTE_CX_MT_PI3_pneumothorax", True),
        ("ETR_ChestMetry_PI6px_pneumothorax_add", True),
        # 名前が「気胸でない」と明示している。逆の意味で拾わないこと。
        ("ETR_ChestMetry_PI6px_abnormal_non_pneumothorax", False),
        ("ChestMetry_PI6px_normal", False),
    ],
)
def test_気胸データセット名の判定はnon_pneumothoraxを除く(dataset_id, expected):
    from segmentation_validation.lpdata_export.export import _named_pneumothorax

    assert _named_pneumothorax(dataset_id) is expected
