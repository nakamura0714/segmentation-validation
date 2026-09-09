"""未アノテーション画像のスポットチェック抽出と、アセット書き出しの並列化。"""

from __future__ import annotations

import sys
from pathlib import Path

from conftest import NORMAL, make_group, make_record

from segmentation_validation.review.export_assets import (
    export_assets,
    select_spot_check_groups,
)
from segmentation_validation.selection.image_decisions import build_image_decisions

# ``review/__init__.py`` は ``from .export_assets import export_assets`` を
# 実行するため、パッケージ属性 ``segmentation_validation.review.export_assets``
# は（``import ... as`` 経由でも）サブモジュールではなく同名関数に上書きされる。
# ``monkeypatch`` でサブモジュール側の ``export_group`` を差し替えるには
# ``sys.modules`` から本物のモジュールを取る必要がある。
export_assets_module = sys.modules["segmentation_validation.review.export_assets"]


# --------------------------------------------------------- select_spot_check_groups


def test_annotationのある画像はスポットチェック対象にならない(config):
    group = make_group((make_record("A"),), file="F1")
    decisions = build_image_decisions([group], [], config)

    selected = select_spot_check_groups([group], decisions, per_bucket=10)

    assert selected == []


def test_バケットごとに上限件数までしか選ばれない(config):
    groups = [
        make_group((), file=f"F{i}", series=f"S{i}", series_image_index=0)
        for i in range(5)
    ]
    decisions = build_image_decisions(groups, [], config)

    selected = select_spot_check_groups(groups, decisions, per_bucket=2)

    # 全て同じ (dataset_id, image_class=unannotated_orphan, series_image_index=0)
    # の1バケットに入るので、per_bucket=2 なら2件だけになる。
    assert len(selected) == 2


def test_同じ選択は毎回同じ結果になる(config):
    """乱数ではなくfile_uidソートで決定的に選ぶこと。

    既存アセットのスキップが効き続けるためには、再実行のたびに違う画像が
    選ばれてはいけない。
    """
    groups = [
        make_group((), file=f"F{i}", series=f"S{i}", series_image_index=0)
        for i in range(5)
    ]
    decisions = build_image_decisions(groups, [], config)

    first = select_spot_check_groups(groups, decisions, per_bucket=2)
    second = select_spot_check_groups(groups, decisions, per_bucket=2)

    assert [g.file_uid for g in first] == [g.file_uid for g in second]
    # 一番file_uidが若い2件が選ばれる。
    assert {g.file for g in first} == {"F0", "F1"}


def test_異なるdataset分類位置は別バケットになる(config):
    """(dataset_id, image_class, series_image_index) が違えば独立にper_bucketが効く。"""
    negative = make_group((), file="N1", series="SN", case_labels=(NORMAL,))
    orphan = make_group((), file="O1", series="SO")
    view = make_group((), file="V1", series="SV")
    # V1 と同じ series に annotation 済みの別ファイルを置いて UNANNOTATED_VIEW にする。
    annotated_sibling = make_group((make_record("A"),), file="V0", series="SV")
    groups = [negative, orphan, view, annotated_sibling]
    decisions = build_image_decisions(groups, [], config)

    selected = select_spot_check_groups(groups, decisions, per_bucket=10)
    files = {g.file for g in selected}

    assert files == {"N1", "O1", "V1"}  # annotation済みのV0は含まれない


# ------------------------------------------------------------------- export_assets


def test_並列実行でも逐次実行と同じ結果になる(config, tmp_path, monkeypatch):
    """``jobs`` を変えても書き出し結果（AssetPaths）自体は変わらないこと。

    DICOMの実読み込みはしない —— ``export_group`` を決定的なスタブに差し替え、
    オーケストレーション（ThreadPoolExecutor化）だけを検証する。
    """
    groups = [make_group((), file=f"F{i}") for i in range(20)]

    def fake_export_group(group, cfg, out_dir, reference_path=None, force=False):
        return export_assets_module.AssetPaths(
            file_uid=group.file_uid, image=Path(f"{out_dir}/images/{group.file}.png")
        )

    monkeypatch.setattr(export_assets_module, "export_group", fake_export_group)

    sequential = export_assets(groups, config, tmp_path / "seq", jobs=1)
    parallel = export_assets(groups, config, tmp_path / "par", jobs=8)

    assert set(sequential) == set(parallel) == {g.file_uid for g in groups}
    for file_uid in sequential:
        # 出力先ディレクトリが違うので image パス自体は比較しない。
        # ファイル名（file）部分が一致していれば同じ入力から同じ結果ということ。
        assert sequential[file_uid].image.name == parallel[file_uid].image.name


def test_jobsが1でも複数でも書き出し件数が全画像分になる(config, tmp_path, monkeypatch):
    groups = [make_group((), file=f"F{i}") for i in range(7)]

    def fake_export_group(group, cfg, out_dir, reference_path=None, force=False):
        return export_assets_module.AssetPaths(file_uid=group.file_uid)

    monkeypatch.setattr(export_assets_module, "export_group", fake_export_group)

    for jobs in (1, 4):
        results = export_assets(groups, config, tmp_path / f"jobs{jobs}", jobs=jobs)
        assert len(results) == len(groups)
