"""テンプレートを正典として扱えているかを守る。

``meta.structure`` をコード側に書き直さない（テンプレートから丸写しする）という
設計がここで成立している。ずれたら**出力してから気づく**のではなく起動時に落ちること。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from segmentation_validation.config import Config
from segmentation_validation.lpdata_export.sample import FIELD_BUILDERS
from segmentation_validation.lpdata_export.template import (
    TemplateDriftError,
    load_template,
    unresolved_placeholders,
    validate_coverage,
)

MINIMAL = {
    "meta": {
        "content_type": "dataset",
        "dataset_name": "t",
        "dataset_id": "t_001",
        "date": "2026-07-25",
        "project": "p",
        "owner": "<owner>",
        "split": "train",
        "structure": {
            "image_file": {"type": "Path"},
            "mask": {"type": "Mask2D", "defaults": {"keep_margin": True}},
            "finding_labels": {"type": "str", "multiple": True},
        },
    },
    "samples": {},
}


def write_template(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "template.yaml"
    path.write_text(
        yaml.safe_dump(payload or MINIMAL, allow_unicode=True), encoding="utf-8"
    )
    return path


def test_テンプレートのstructureをそのまま出力する(tmp_path: Path):
    """``defaults: {keep_margin: true}`` を落とさないこと。

    これは lp-data v2.0.0 の仕様で、本リポジトリの ``docs/dataset_format.md``
    （古いスナップショット）には無い。手で structure を書くと必ず落とす項目。
    """
    template = load_template(write_template(tmp_path))

    assert template.structure["mask"]["defaults"] == {"keep_margin": True}
    assert template.multiple_keys() == {"finding_labels"}


def test_出力側が決めるキーはmetaに引き継がない(tmp_path: Path):
    """``content_type`` / ``date`` / ``split`` はエクスポータが決める。"""
    template = load_template(write_template(tmp_path))

    assert "structure" not in template.meta
    assert "content_type" not in template.meta
    assert "date" not in template.meta
    assert "split" not in template.meta
    assert template.meta["project"] == "p"


def test_structureが空なら読み込みで落ちる(tmp_path: Path):
    payload = {"meta": {"structure": {}}, "samples": {}}
    with pytest.raises(TemplateDriftError, match="meta.structure が空"):
        load_template(write_template(tmp_path, payload))


def test_structureに属性が増えたら落ちる(tmp_path: Path):
    template = load_template(write_template(tmp_path))

    with pytest.raises(TemplateDriftError, match="ビルダに無い属性"):
        validate_coverage(template, {"image_file", "mask"})


def test_ビルダが余分なキーを持っていたら落ちる(tmp_path: Path):
    template = load_template(write_template(tmp_path))

    with pytest.raises(TemplateDriftError, match="structure に無いのに"):
        validate_coverage(
            template, set(template.structure) | {"pneumothorax_side_guess"}
        )


def test_キーが一致していれば通る(tmp_path: Path):
    template = load_template(write_template(tmp_path))

    validate_coverage(template, set(template.structure))


def test_未解決のplaceholderを検出する():
    assert unresolved_placeholders({"owner": "<owner>"}) == ["owner=<owner>"]
    assert unresolved_placeholders({"owner": "kosuke"}) == []
    # 文字列以外は見ない（structure のような入れ子は対象外）。
    assert unresolved_placeholders({"version": 1.0}) == []


# ----------------------------------------------- 実テンプレートへの追随


@pytest.mark.realdata
def test_実テンプレートとビルダのキーが一致する():
    """med-chest-metry-pi6 のテンプレート（PR #68）が更新されたら落ちる。

    テンプレート側が正典なので、落ちたら直すのは ``sample.FIELD_BUILDERS``。
    """
    config = Config()
    path = config.resolve(config.lpdata_export.template_path)
    if not path.exists():
        pytest.skip(f"テンプレートが無い: {path}")

    validate_coverage(load_template(path), set(FIELD_BUILDERS))
