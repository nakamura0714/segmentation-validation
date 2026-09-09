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
from segmentation_validation.review.review_schema import SCHEMA_SEED_TAG
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


def _real_samples(built):
    """候補値シード（``SCHEMA_SEED_TAG``）を除いた、本物のSampleだけを返す。"""
    return [sample for sample in built if SCHEMA_SEED_TAG not in sample.tags]


def test_マスクの無いbboxもdatasetに載る(built, synthetic):
    """★過去に11件が manifest から落ちていた。座標そのものが目視対象。"""
    uids = {
        det["geometry_uid"]
        for sample in _real_samples(built)
        for det in (sample["final"].detections if sample["final"] else [])
    }
    assert uids == {"A", "B", "C"}


def test_目視待ちの件数が採否マスタと一致する(built):
    pending = [
        det
        for sample in _real_samples(built)
        for det in (sample["final"].detections if sample["final"] else [])
        if det["review_status"] == Decision.PENDING.value
    ]
    assert [d["geometry_uid"] for d in pending] == ["A"]

    pending_images = [
        s for s in _real_samples(built) if s["review_status"] == Decision.PENDING.value
    ]
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
    """★日本語だけの名前は slug 化で失敗する。ASCII 名であること。

    ``0-all-real`` は長らく作られていなかった（``Dataset`` を ``save_view`` に
    渡すと ``include_uuids`` で落ちる）。件数だけ見ていると気づけないので、
    名前で確かめる。
    """
    names = built.list_saved_views()

    # ★件数ではなく名前で見る。件数だと「1本作られていない」ことに
    # 気づけないまま数だけ合ってしまう（0-all-real が実際にそうだった）。
    # ビューを足したときにここを直す必要が無いよう、上限は見ない。
    assert "0-all-real" in names, "Dataset ではなく Dataset.view() を渡すこと"
    assert "10-reasoned-decisions" in names
    assert len(names) == len(set(names)), "同じ名前が二重に作られている"
    assert all(name.isascii() for name in names), "日本語だけの名前は slug 化で失敗する"


# --------------------------------------------------------------- 往復


def input_judgments(dataset) -> None:
    """App で目視入力するのと同じことをする。"""
    for sample in dataset:
        dets = sample["final"]
        for det in dets.detections if dets else []:
            if det["geometry_uid"] != "A":
                continue
            det["review_status"] = Decision.EXCLUDE.value
            # 候補から2つ選ぶ（重複であり、かつ古い方）。
            det["review_reasons"] = ["invalid_duplicate", "older_duplicate"]
            det["reviewer"] = REVIEWER
            det["reviewed_at"] = "2026-09-04T09:00:00+00:00"
            sample["final"] = dets
        if sample["file_id"] == "F2":
            sample["review_status"] = Decision.EXCLUDE.value
            sample["review_reasons"] = ["lateral_view"]
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
    assert by_uid["A"]["review_reasons"] == ["invalid_duplicate", "older_duplicate"]
    # 候補で表せる理由は自由記述欄に残さない（二重に持つと食い違う）。
    assert by_uid["A"]["review_reason"] == ""
    # ★reason: タグも貼り直されている。貼り直さないと import のたびに消えていた。
    assert "reason:invalid_duplicate" in by_uid["A"].tags
    assert "reason:older_duplicate" in by_uid["A"].tags
    assert by_uid["A"]["reviewer"] == REVIEWER
    # 判定していない annotation は基準線のまま。
    assert by_uid["B"]["review_status"] == Decision.KEEP.value

    images = {s["file_id"]: s for s in restored}
    assert images["F2"]["review_status"] == Decision.EXCLUDE.value
    assert images["F2"]["review_reasons"] == ["lateral_view"]
    assert "reason:lateral_view" in images["F2"].tags

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


# ------------------------------------------------- App に出るフィールドの宣言


def _panel_names(dataset) -> list[str]:
    """Edit パネルが並べる属性名。並び順のまま返す。"""
    return [a["name"] for a in dataset.label_schemas["final"]["attributes"]]


def test_動的属性がfieldschemaに宣言される(built):
    """★宣言しないと、値が入っているのに App のどこにも出ない。

    ``add_samples`` の既定は ``dynamic=False``。それだと Edit パネルも
    サイドバーも宣言済みフィールドしか見ないので、`timestamp` が
    全 Detection に入っているのに一切表示されない（実際にそうなっていた）。
    """
    flat = built.get_field_schema(flat=True)

    for name in ("timestamp", "annotator", "geometry_uid", "review_status"):
        assert f"final.detections.{name}" in flat, name


def test_作成日時がEditパネルに並ぶ(built):
    """重複のどちらを exclude するかの判断材料。これが本題。"""
    names = _panel_names(built)

    assert "timestamp" in names
    # 判定欄のすぐ下に置く（判断してから review_status に戻る動きになる）。
    assert names.index("timestamp") > names.index("review_status")


def test_判定フィールドは編集できて検証結果は読み取り専用(built):
    """検証結果を App で書き換えても export は読まない。誤解を生むので固定する。"""
    by_name = {a["name"]: a for a in built.label_schemas["final"]["attributes"]}

    assert not by_name["review_status"].get("read_only")
    assert not by_name["review_reason"].get("read_only")
    assert by_name["timestamp"]["read_only"] is True
    assert by_name["geometry_uid"]["read_only"] is True


def test_読み取り専用の属性はドロップダウンにしない(built):
    """timestamp や issues を候補一覧にすると長文が並んで読めなくなる。

    component は型ごとに許される値が決まっている（bool に ``text`` は不正で、
    label schema の検証が落ちる）ので、型の既定に戻っていることを見る。
    """
    from fiftyone.core.annotation.constants import DEFAULT_COMPONENTS

    for attribute in built.label_schemas["final"]["attributes"]:
        if not attribute.get("read_only"):
            continue
        assert "values" not in attribute, attribute["name"]
        assert attribute["component"] == DEFAULT_COMPONENTS[attribute["type"]], (
            attribute["name"]
        )


def test_常に空のフィールドはパネルに出さない(built):
    """confidence / index / mask_path はこのツールでは使わない。"""
    names = _panel_names(built)

    for name in ("confidence", "index", "id", "mask_path"):
        assert name not in names, name


def test_labelのドロップダウン候補が残る(built):
    """attributes を差し替えるときに classes を落とすと label が選べなくなる。"""
    assert "pneumothorax" in built.label_schemas["final"]["classes"]


# ------------------------------------------------- 重複ペアの相手の日時


@pytest.fixture
def duplicate_pair(tmp_path, review_config):
    """重複ペア（A と B が互いに related）を持つ manifest。"""
    from segmentation_validation.review.manifest import build_manifest

    images_dir = tmp_path / "dup"
    images_dir.mkdir()
    blank = np.zeros((40, 40), dtype=np.uint8)
    image = images_dir / "F9.png"
    Image.fromarray(blank, mode="L").save(image)
    mask = images_dir / "mask.png"
    Image.fromarray(np.full((10, 10), 255, dtype=np.uint8), mode="L").save(mask)

    record_a = replace(
        make_record("A", file="F9"), timestamp="2026-07-02 07:50:38+00:00"
    )
    record_b = replace(
        make_record("B", file="F9"),
        timestamp="2026-07-02 07:50:52+00:00",
        user="partner@example.com",
    )
    group = make_group((record_a, record_b), file="F9")
    assets = {
        group.file_uid: AssetPaths(
            file_uid=group.file_uid,
            image=image,
            masks={"A": mask, "B": mask},
            boxes={"A": [0.1, 0.1, 0.2, 0.2], "B": [0.1, 0.1, 0.2, 0.2]},
        )
    }
    issues = [
        make_issue("D04_CONTAINED_DUPLICATE", geometry_uid=uid, file="F9")
        for uid in ("A", "B")
    ]
    decisions = [
        replace(d, related_geometry_uid="B" if d.geometry_uid == "A" else "A")
        for d in build_decisions([record_a, record_b], issues, review_config)
    ]
    image_decisions = build_image_decisions([group], issues, review_config)
    return build_manifest(
        [group], assets, decisions, image_decisions, issues, review_config
    )


def test_相手の日時がDetectionに載る(duplicate_pair, review_config):
    """★片方を開くだけで判断できるようにするのが目的。

    実データの DUP_0025 と同じ14秒差。ペア相手を開き直さずに済む。
    """
    import fiftyone as fo

    from segmentation_validation.review.fiftyone_builder import build_dataset

    build_dataset(duplicate_pair, review_config)
    dataset = fo.load_dataset(DATASET_NAME)
    try:
        by_uid = {
            det["geometry_uid"]: det
            for sample in dataset
            if SCHEMA_SEED_TAG not in sample.tags and sample["final"]
            for det in sample["final"].detections
        }
        own = by_uid["A"]

        assert own["timestamp"] == "2026-07-02 07:50:38+00:00"
        assert own["partner_timestamp"] == "2026-07-02 07:50:52+00:00"
        assert own["partner_annotator"] == "partner@example.com"
        assert own["newer_in_pair"] is False
        assert own["pair_verdict"] == "相手が新しい（14秒差）"

        names = _panel_names(dataset)
        assert "partner_timestamp" in names
        assert "pair_verdict" in names
    finally:
        if DATASET_NAME in fo.list_datasets():
            fo.delete_dataset(DATASET_NAME)


# ------------------------------------------- 理由はチェックボックスで候補固定


def test_理由はチェックボックスで候補固定(built):
    """★これがこの機能の狙い。自由入力を許すと綴りミスが集計を割る。

    ``list<str>`` + ``checkboxes`` のとき App は CheckboxList を描き、
    自由入力の口が無い。``dropdown`` / ``text`` は AutocompleteView +
    ``allow_user_input`` で自由入力が残ってしまう。
    """
    from segmentation_validation.review.review_schema import (
        ALL_SUGGESTED_REASONS,
        FIELD_REVIEW_REASONS,
    )

    by_name = {a["name"]: a for a in built.label_schemas["final"]["attributes"]}
    spec = by_name[FIELD_REVIEW_REASONS]

    assert spec["type"] == "list<str>"
    assert spec["component"] == "checkboxes"
    assert spec["values"] == list(ALL_SUGGESTED_REASONS)
    assert not spec.get("read_only")


def test_理由は判定欄の直下に並ぶ(built):
    """判定を決めてから理由を選ぶ動きになるように。"""
    names = [a["name"] for a in built.label_schemas["final"]["attributes"]]

    assert names.index("review_reasons") == names.index("review_status") + 1


def test_自由記述の理由欄も残る(built):
    """候補で表せないことを書きたいときの逃げ道。"""
    names = [a["name"] for a in built.label_schemas["final"]["attributes"]]

    assert "review_reason" in names


def test_機械の理由が理由欄に流し込まれていない(built):
    """★目視前の annotation は機械の review_required を持っている。

    それが理由欄に入っていると、人間が触らないまま「理由あり」として
    export されてしまう（実データ102件でそうなっていた）。
    """
    for sample in _real_samples(built):
        dets = sample["final"]
        for det in dets.detections if dets else []:
            assert det["review_reasons"] == [], det["geometry_uid"]
            assert det["review_reason"] == "", det["geometry_uid"]
        assert sample["review_reasons"] == []
        assert sample["review_reason"] == ""


def test_理由フィールドはlist_strとして宣言される(built):
    """★これが崩れると Edit パネルから理由欄が消える。

    ``add_samples(dynamic=True)`` は最初に見た値で型を決めて広げないので、
    理由を持たない annotation の ``[]`` が先に来ると要素の型が決まらない
    ``ListField`` になり、``list<str>`` と見なされない。
    ``_declare_reason_field()`` が順序に依存せず型を固定している。
    """
    import fiftyone.core.fields as fof

    from segmentation_validation.review.review_schema import FIELD_REVIEW_REASONS

    field = built.get_field_schema(flat=True)[
        f"final.detections.{FIELD_REVIEW_REASONS}"
    ]

    assert isinstance(field, fof.ListField)
    assert isinstance(field.field, fof.StringField), (
        "要素の型が決まっていないと list<str> として扱われない"
    )


def test_理由が区切りつきでexportされる(built, review_config, synthetic):
    """``selection_decisions.csv`` の読み手が detected_checks と同じ扱いをできる形。"""
    from segmentation_validation.review.export_decisions import collect_decisions

    input_judgments(built)
    collected = collect_decisions(review_config, synthetic)
    rows = {(r["kind"], r["key"]): r for r in collected}

    assert rows[("annotation", "A")]["reason"] == "invalid_duplicate|older_duplicate"
    image_key = synthetic["images"][1]["file_uid"]
    assert rows[("image", image_key)]["reason"] == "lateral_view"


def test_理由ビューが理由入りのannotationだけを集める(built):
    input_judgments(built)
    view = built.load_saved_view("10-reasoned-decisions")

    uids = [
        det["geometry_uid"]
        for sample in view
        for det in (sample["final"].detections if sample["final"] else [])
    ]
    assert uids == ["A"]
