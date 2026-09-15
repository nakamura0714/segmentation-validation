"""目視判定の正本（``review/decision_store.py``）を守る。

ここが壊れると、再生成できない唯一の資産である人間の目視結果が静かに
失われる。特に押さえているのは次の3つ。

1. 画像側のキーが ``source_json``（日時スタンプ込みのファイル名）を含まない
   こと。v1 の ``file_uid`` はこれを含んでいたため、元JSONを再エクスポート
   するだけで画像側の判定が全件迷子になっていた。
2. 読めないファイルを「判定が無い」と解釈しないこと。空として扱うと、人間の
   判定を丸ごと落とした状態で select が通ってしまう。
3. 保存が追記マージであること。``review build`` は pending の無い項目を
   FiftyOne から外すので、確定済みの判定はスキャン範囲から消える。消えた＝
   無効ではない。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DATASET, FILE, INSTITUTION, SERIES, SOURCE, STUDY, make_group

from segmentation_validation.core.records import FileGroup
from segmentation_validation.review import decision_store as store


def annotation_row(
    key: str = "uid-1",
    dataset_id: str = DATASET,
    decision: str = "exclude",
    reviewed_at: str = "2026-09-01T00:00:00+00:00",
    **extra: str,
) -> dict:
    return store.make_row(
        kind=store.KIND_ANNOTATION,
        dataset_id=dataset_id,
        key=key,
        decision=decision,
        reason=extra.get("reason", "invalid_duplicate"),
        reviewer=extra.get("reviewer", "reviewer@example.com"),
        reviewed_at=reviewed_at,
        comment=extra.get("comment", ""),
    )


def image_row(
    key: str = f"{INSTITUTION}/{STUDY}/{SERIES}/{FILE}",
    dataset_id: str = DATASET,
    decision: str = "keep",
    reviewed_at: str = "2026-09-01T00:00:00+00:00",
) -> dict:
    return store.make_row(
        kind=store.KIND_IMAGE,
        dataset_id=dataset_id,
        key=key,
        decision=decision,
        reason="frontal_view",
        reviewer="reviewer@example.com",
        reviewed_at=reviewed_at,
    )


# ------------------------------------------------------------------- キー


def test_annotation_keyはdataset_idを含む() -> None:
    assert store.annotation_key("DS_A", "uid-1") == "DS_A::uid-1"


def test_image_keyはFileGroupのstable_file_uidと一致する() -> None:
    group = make_group([])
    assert isinstance(group, FileGroup)
    assert (
        store.image_key(
            group.dataset_id, group.institution, group.study, group.series, group.file
        )
        == group.stable_file_uid
    )


def test_stable_file_uidはsource_jsonを含まない() -> None:
    """画像キーの安定性の根拠。file_uid との違いをここで固定する。"""
    group = make_group([])
    assert SOURCE in group.file_uid
    assert SOURCE not in group.stable_file_uid
    assert group.stable_file_uid.startswith(f"{DATASET}::")


def test_再エクスポートでsource_jsonが変わってもimage_keyは不変() -> None:
    """★中心の要件。

    ``engineer-set-<id>-<日時>.json`` の日時だけが変わる再エクスポートでは
    dataset_id が変わらないので、画像キーも変わってはいけない。
    """
    old = "engineer-set-ANN_EIRLPRJ_01272-20260624_080014.json::inst/st/se/f"
    new = "engineer-set-ANN_EIRLPRJ_01272-20260709_004526.json::inst/st/se/f"

    assert store.image_key_parts_from_file_uid(old) == (
        "ANN_EIRLPRJ_01272",
        "inst/st/se/f",
    )
    assert store.image_key_parts_from_file_uid(
        old
    ) == store.image_key_parts_from_file_uid(new)


def test_分解できないfile_uidはNoneになる() -> None:
    assert store.image_key_parts_from_file_uid("区切りが無い") is None
    assert store.image_key_parts_from_file_uid("::rest") is None
    assert store.image_key_parts_from_file_uid("only.json::") is None


def test_row_keyはdataset_idを含む() -> None:
    """geometry_uid が cross-dataset で再利用されるので複合キーが要る。"""
    a = annotation_row(key="shared-uid", dataset_id="DS_A")
    b = annotation_row(key="shared-uid", dataset_id="DS_B")
    assert store.row_key(a) != store.row_key(b)


# ------------------------------------------------------------------- 読み書き


def test_v2を書いて読み戻せる(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(path, [annotation_row(), image_row()], "synth")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == store.SCHEMA_VERSION
    assert payload["meta"]["n_annotations"] == 1
    assert payload["meta"]["n_images"] == 1
    # 画像行は image_key を持ち、file_uid は持たない。
    assert "image_key" in payload["image_decisions"][0]
    assert "file_uid" not in payload["image_decisions"][0]

    rows = store.load_rows(path)
    assert {store.row_key(r) for r in rows} == {
        (store.KIND_ANNOTATION, DATASET, "uid-1"),
        (store.KIND_IMAGE, DATASET, f"{INSTITUTION}/{STUDY}/{SERIES}/{FILE}"),
    }


def test_csvも一緒に書かれる(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(path, [annotation_row()], "synth")
    csv_path = path.with_suffix(".csv")
    assert csv_path.exists()
    header = csv_path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(store.COLUMNS)


def test_保存は追記マージで既存キーを消さない(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(path, [annotation_row(key="uid-1")], "synth")
    store.write_rows(path, [annotation_row(key="uid-2")], "synth")

    rows = store.load_rows(path)
    assert {r["key"] for r in rows} == {"uid-1", "uid-2"}


def test_同じキーは新しいreviewed_atが勝つ(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(
        path,
        [annotation_row(decision="keep", reviewed_at="2026-09-01T00:00:00+00:00")],
        "synth",
    )
    store.write_rows(
        path,
        [annotation_row(decision="exclude", reviewed_at="2026-09-02T00:00:00+00:00")],
        "synth",
    )
    rows = store.load_rows(path)
    assert len(rows) == 1
    assert rows[0]["decision"] == "exclude"


def test_reviewed_atが無く判定が食い違えば例外になる(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(path, [annotation_row(decision="keep", reviewed_at="")], "synth")
    with pytest.raises(store.DecisionStoreError) as excinfo:
        store.write_rows(
            path, [annotation_row(decision="exclude", reviewed_at="")], "synth"
        )
    assert "人が決める" in str(excinfo.value)


def test_衝突で例外が出たとき正本は書き換わらない(tmp_path: Path) -> None:
    """壊れかけの状態で上書きしない。人が直すまで元のまま残す。"""
    path = tmp_path / "review_decisions.json"
    store.write_rows(path, [annotation_row(decision="keep", reviewed_at="")], "synth")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(store.DecisionStoreError):
        store.write_rows(
            path, [annotation_row(decision="exclude", reviewed_at="")], "synth"
        )
    assert path.read_text(encoding="utf-8") == before


def test_無いファイルは空として読める(tmp_path: Path) -> None:
    assert store.load_rows(tmp_path / "missing.json") == []


def test_壊れた正本は空として扱わずエラーになる(tmp_path: Path) -> None:
    """空として扱うと人間の判定を丸ごと落としたまま select が通ってしまう。"""
    path = tmp_path / "review_decisions.json"
    path.write_text("{壊れている", encoding="utf-8")
    with pytest.raises(store.DecisionStoreError):
        store.load_rows(path)


def test_snapshotはマージしない(tmp_path: Path) -> None:
    """スナップショットはその時点の正本の写し。過去の行を混ぜない。"""
    path = tmp_path / "snapshot.json"
    store.write_rows(path, [annotation_row(key="uid-1")], "synth")
    store.snapshot([annotation_row(key="uid-2")], path, "synth")
    rows = store.load_rows(path)
    assert {r["key"] for r in rows} == {"uid-2"}


def test_行の並びは安定している(tmp_path: Path) -> None:
    """Git 管理の正本なので、書くたびに差分が暴れてはいけない。"""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    rows = [annotation_row(key=f"uid-{i}") for i in (3, 1, 2)]
    store.write_rows(first, rows, "synth")
    store.write_rows(second, list(reversed(rows)), "synth")

    def decisions(path: Path) -> list[str]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [d["geometry_uid"] for d in payload["decisions"]]

    assert decisions(first) == decisions(second) == ["uid-1", "uid-2", "uid-3"]


# ------------------------------------------------------------------- 参照


def test_exported_keysは読めないファイルを未export扱いにする(tmp_path: Path) -> None:
    """ゲート側は安全側（未 export）に倒す。ここだけ例外を飲む。"""
    path = tmp_path / "review_decisions.json"
    path.write_text("{壊れている", encoding="utf-8")
    assert store.exported_keys(path) == set()


def test_countsは種別ごとに数える(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    store.write_rows(
        path,
        [annotation_row(key="uid-1"), annotation_row(key="uid-2"), image_row()],
        "synth",
    )
    assert store.counts(path) == {"annotations": 2, "images": 1}


def test_orphansは現在の構成に無い行を返す() -> None:
    rows = [annotation_row(key="uid-1"), annotation_row(key="uid-missing")]
    known = {store.KIND_ANNOTATION: {f"{DATASET}::uid-1"}}
    assert [r["key"] for r in store.orphans(rows, known)] == ["uid-missing"]
