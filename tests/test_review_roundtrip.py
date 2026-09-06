"""FiftyOne の往復。`uv run pytest -m fiftyone` で走る。

**FiftyOne DB を正本にしない**という方針が成立するために必要な性質:

```
判定を入力 → review export → DB削除 → review build → review import → 判定が戻る
```

これが成立していれば、閾値を変えて再検出しても、新しいJSONが追加されても、
DB を消しても、過去の目視の成果を失わない。**目視は一番高い工数なので、
ここが壊れると取り返しがつかない。**

あわせて、`review build` が export していない判定を消すことも
（それを止めるゲートがあることも）ここで固定する。

合成データだけで動く。実データも実際の目視結果も使わない。
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from conftest import make_group, make_issue, make_record
from PIL import Image

from segmentation_validation.checks.base import Severity
from segmentation_validation.config import Config
from segmentation_validation.review.export_assets import AssetPaths
from segmentation_validation.review.manifest import build_manifest, write_manifest
from segmentation_validation.selection.decisions import (
    Decision,
    build_decisions,
)
from segmentation_validation.selection.image_decisions import build_image_decisions

pytestmark = pytest.mark.fiftyone

DATASET_NAME = "pytest-roundtrip"
REVIEWER = "pytest@example.com"


@pytest.fixture(scope="module")
def fiftyone_available():
    pytest.importorskip("fiftyone", reason="fiftyone が無い（uv sync --group review）")


@pytest.fixture
def review_config(tmp_path, fiftyone_available) -> Config:
    """テスト専用の dataset 名を使う。実運用の DB を壊さないため。"""
    return replace(
        Config(project_root=tmp_path),
        review=replace(Config().review, dataset_name=DATASET_NAME),
    )


@pytest.fixture
def synthetic(tmp_path, review_config):
    """画像2枚・annotation 3件の合成 manifest を実コードで組む。

    - 画像1: brush 2件（1件は目視対象）
    - 画像2: annotation なし（未アノテーションのビュー = 画像単位の目視対象）
    - annotation の1件は**マスクを持たない bbox**（過去に manifest から落ちていた）
    """
    images_dir = tmp_path / "images"
    masks_dir = tmp_path / "masks"
    images_dir.mkdir()
    masks_dir.mkdir()

    def png(path, array):
        Image.fromarray(array, mode="L").save(path)
        return path

    blank = np.zeros((40, 40), dtype=np.uint8)
    img1 = png(images_dir / "F1.png", blank)
    img2 = png(images_dir / "F2.png", blank)
    mask_a = png(masks_dir / "A.png", np.full((10, 10), 255, dtype=np.uint8))
    mask_b = png(masks_dir / "B.png", np.full((10, 10), 255, dtype=np.uint8))

    record_a = make_record("A", file="F1")
    record_b = make_record("B", file="F1")
    record_c = make_record("C", file="F1", annotation_type="bbox", has_mask=False)
    group1 = make_group((record_a, record_b, record_c), file="F1")
    group2 = make_group((), file="F2")
    groups = [group1, group2]

    assets = {
        group1.file_uid: AssetPaths(
            file_uid=group1.file_uid,
            image=img1,
            masks={"A": mask_a, "B": mask_b},
            boxes={
                "A": [0.1, 0.1, 0.2, 0.2],
                "B": [0.5, 0.5, 0.2, 0.2],
                "C": [0.3, 0.3, 0.1, 0.1],
            },
        ),
        group2.file_uid: AssetPaths(file_uid=group2.file_uid, image=img2),
    }

    # A は目視対象、B は問題なし、F2 は画像単位の目視対象。
    issues = [
        make_issue(
            "S05_OUTSIDE_BODY",
            geometry_uid="A",
            severity=Severity.WARNING,
            file="F1",
        ),
        make_issue("M09_UNANNOTATED_VIEW", geometry_uid=None, file="F2"),
    ]
    records = [record_a, record_b, record_c]
    decisions = build_decisions(records, issues, review_config)
    image_decisions = build_image_decisions(groups, issues, review_config)
    manifest = build_manifest(
        groups, assets, decisions, image_decisions, issues, review_config
    )
    write_manifest(tmp_path / "review_manifest.json", manifest)
    return manifest


@pytest.fixture
def built(review_config, synthetic):
    """FiftyOne dataset を作る。テスト後は必ず消す。"""
    import fiftyone as fo

    from segmentation_validation.review.fiftyone_builder import build_dataset

    build_dataset(synthetic, review_config)
    yield fo.load_dataset(DATASET_NAME)
    if DATASET_NAME in fo.list_datasets():
        fo.delete_dataset(DATASET_NAME)


# --------------------------------------------------------------- 構築


def test_マスクの無いbboxもdatasetに載る(built, synthetic):
    """★過去に11件が manifest から落ちていた。座標そのものが目視対象。"""
    uids = {
        det["geometry_uid"]
        for sample in built
        for det in (sample["final"].detections if sample["final"] else [])
    }
    assert uids == {"A", "B", "C"}


def test_目視待ちの件数が採否マスタと一致する(built):
    pending = [
        det
        for sample in built
        for det in (sample["final"].detections if sample["final"] else [])
        if det["review_status"] == Decision.PENDING.value
    ]
    assert [d["geometry_uid"] for d in pending] == ["A"]

    pending_images = [s for s in built if s["review_status"] == Decision.PENDING.value]
    assert [s["file_id"] for s in pending_images] == ["F2"]


def test_autoタグが貼られる(built):
    tags = {
        t
        for sample in built
        for det in (sample["final"].detections if sample["final"] else [])
        for t in det.tags
    }
    assert "auto:s05_outside_body" in tags


def test_保存ビューが全部作られる(built):
    """★日本語だけの名前は slug 化で失敗する。ASCII 名であること。"""
    assert len(built.list_saved_views()) == 6


# --------------------------------------------------------------- 往復


def input_judgments(dataset) -> None:
    """App で目視入力するのと同じことをする。"""
    for sample in dataset:
        dets = sample["final"]
        for det in dets.detections if dets else []:
            if det["geometry_uid"] != "A":
                continue
            det["review_status"] = Decision.EXCLUDE.value
            det["review_reason"] = "outside_body"
            det["reviewer"] = REVIEWER
            det["reviewed_at"] = "2026-09-04T09:00:00+00:00"
            sample["final"] = dets
        if sample["file_id"] == "F2":
            sample["review_status"] = Decision.EXCLUDE.value
            sample["review_reason"] = "lateral_view"
            sample["reviewer"] = REVIEWER
            sample["reviewed_at"] = "2026-09-04T09:00:00+00:00"
        sample.save()


def test_人間の判定だけがexportされる(built, review_config, synthetic):
    """★機械の判定（周辺 annotation の keep）を混ぜてはいけない。"""
    from segmentation_validation.review.export_decisions import collect_decisions

    input_judgments(built)
    rows = collect_decisions(review_config, synthetic)

    assert {(r["kind"], r["key"]) for r in rows} == {
        ("annotation", "A"),
        ("image", synthetic["images"][1]["file_uid"]),
    }
    assert all(r["reviewer"] == REVIEWER for r in rows)


def test_DBを消して再構築してimportすると判定が戻る(
    built, review_config, synthetic, tmp_path
):
    """★これが成立していれば FiftyOne DB を正本にしなくてよい。"""
    import fiftyone as fo

    from segmentation_validation.review.export_decisions import (
        collect_decisions,
        write_decisions,
    )
    from segmentation_validation.review.fiftyone_builder import build_dataset
    from segmentation_validation.review.import_decisions import import_decisions

    input_judgments(built)
    path = tmp_path / "review_decisions.json"
    write_decisions(path, collect_decisions(review_config, synthetic), review_config)

    fo.delete_dataset(DATASET_NAME)
    build_dataset(synthetic, review_config)
    counts = import_decisions(path, review_config)
    assert counts == {"annotations": 1, "images": 1}

    restored = fo.load_dataset(DATASET_NAME)
    by_uid = {
        det["geometry_uid"]: det
        for sample in restored
        for det in (sample["final"].detections if sample["final"] else [])
    }
    assert by_uid["A"]["review_status"] == Decision.EXCLUDE.value
    assert by_uid["A"]["review_reason"] == "outside_body"
    assert by_uid["A"]["reviewer"] == REVIEWER
    # 判定していない annotation は基準線のまま。
    assert by_uid["B"]["review_status"] == Decision.KEEP.value

    images = {s["file_id"]: s for s in restored}
    assert images["F2"]["review_status"] == Decision.EXCLUDE.value
    assert images["F2"]["review_reason"] == "lateral_view"

    # ★auto: タグは再構築で貼り直されている。
    assert "auto:s05_outside_body" in by_uid["A"].tags


def test_importしてもautoタグは残る(built, review_config, synthetic, tmp_path):
    """人間の判定を書き戻すときに機械のタグを消さないこと。"""
    from segmentation_validation.review.export_decisions import (
        collect_decisions,
        write_decisions,
    )
    from segmentation_validation.review.import_decisions import import_decisions

    input_judgments(built)
    path = tmp_path / "review_decisions.json"
    write_decisions(path, collect_decisions(review_config, synthetic), review_config)
    import_decisions(path, review_config)

    built.reload()
    by_uid = {
        det["geometry_uid"]: det
        for sample in built
        for det in (sample["final"].detections if sample["final"] else [])
    }
    tags = by_uid["A"].tags
    assert "auto:s05_outside_body" in tags
    assert "review:exclude" in tags


# --------------------------------------------------------------- ゲート


def test_export前の判定はunexportedとして検出される(
    built, review_config, synthetic, tmp_path
):
    """★これが緩むと `review build` が目視の成果を黙って消す（実際に消した）。"""
    from segmentation_validation.review.export_decisions import (
        unexported_human_decisions,
    )

    input_judgments(built)

    lost = unexported_human_decisions(review_config, synthetic, tmp_path / "none.json")
    assert {(r["kind"], r["key"]) for r in lost} == {
        ("annotation", "A"),
        ("image", synthetic["images"][1]["file_uid"]),
    }


def test_export済みなら検出されない(built, review_config, synthetic, tmp_path):
    """指示どおり `review export` した後は `review build` が通ること。"""
    from segmentation_validation.review.export_decisions import (
        collect_decisions,
        unexported_human_decisions,
        write_decisions,
    )

    input_judgments(built)
    exported = tmp_path / "review_decisions.json"
    rows = collect_decisions(review_config, synthetic)
    write_decisions(exported, rows, review_config)

    assert unexported_human_decisions(review_config, synthetic, exported) == []


def test_判定が無ければ検出されない(built, review_config, synthetic, tmp_path):
    """機械の判定しか無い状態で止めてはいけない（初回の `review build`）。"""
    from segmentation_validation.review.export_decisions import (
        unexported_human_decisions,
    )

    lost = unexported_human_decisions(review_config, synthetic, tmp_path / "x.json")
    assert lost == []
