"""development.json 生成時の、データセットを横断した画像重複解消。

実データで確認した2パターンを再現する:

1. `ChestMetry_PI6px_normal` の未アノテーション孤立画像37件が、
   `ETR_ChestMetry_PI6px_abnormal_non_pneumothorax` 側では既に
   annotation付きで keep 済み（annotationの有無で決着するケース）
2. `ChestMetry_PI6px_normal` と `PTE_CX_MT_PI3_pneumothorax` の間で、
   同一DICOMが両方とも未アノテーション孤立として重複している
   （annotationの有無では決着せず、登録日時の新しい方を残すケース）

★重複によるskipは品質上の exclude ではない。``resolve_image_duplicates`` は
``image_decisions.json``/``selection_decisions.json``/``review_decisions.json``
のいずれも読み書きしない（呼び出し側が渡した辞書・リストだけを見る純関数）。
"""

from __future__ import annotations

from segmentation_validation.selection.build_dataset import (
    SELECTED,
    SKIPPED_DUPLICATE,
    resolve_image_duplicates,
)


def image_row(file_uid: str, dataset_id: str, source_date: str, image_path: str) -> dict:
    return {
        "file_uid": file_uid,
        "dataset_id": dataset_id,
        "source_json": f"engineer-set-{dataset_id}-{source_date}.json",
        "image_path": image_path,
    }


def selection_row(file_uid: str, final_decision: str = "keep") -> dict:
    return {"file_uid": file_uid, "final_decision": final_decision}


def test_重複が無ければ何もskipしない():
    rows = [image_row("f1", "DS", "20260101_000000", "unique.dcm")]
    result = resolve_image_duplicates(rows, [], {"f1": "keep"})

    assert result.skipped_file_uids == frozenset()
    assert result.report_rows == []


def test_annotationあり側が勝つ():
    """★37件のケースの再現。annotationの有無が最優先。"""
    rows = [
        image_row("f_normal", "ChestMetry_PI6px_normal", "20260301_000000", "same.dcm"),
        image_row(
            "f_abn",
            "ETR_ChestMetry_PI6px_abnormal_non_pneumothorax",
            "20260101_000000",  # あえて登録日時を古くする。annotationの有無が勝つことを確認するため。
            "same.dcm",
        ),
    ]
    selection_rows = [selection_row("f_abn")]
    by_image = {"f_normal": "keep", "f_abn": "keep"}

    result = resolve_image_duplicates(rows, selection_rows, by_image)

    assert result.skipped_file_uids == frozenset({"f_normal"})
    decisions = {r["file_uid"]: r["decision"] for r in result.report_rows}
    assert decisions == {"f_normal": SKIPPED_DUPLICATE, "f_abn": SELECTED}


def test_annotation件数の多寡はtie_breakに使わない():
    """annotationが2件と1件でも、両方annotationありなら「あり」同士として扱う
    （annotation件数そのものでは決めない。登録日時のみで決着する）。
    """
    rows = [
        image_row("f_many", "DS_old", "20260101_000000", "same.dcm"),
        image_row("f_one", "DS_new", "20260301_000000", "same.dcm"),
    ]
    selection_rows = [
        selection_row("f_many"),
        selection_row("f_many"),
        selection_row("f_one"),
    ]
    result = resolve_image_duplicates(
        rows, selection_rows, {"f_many": "keep", "f_one": "keep"}
    )

    # annotation件数(2 vs 1)ではなく、登録日時が新しい f_one が勝つ。
    assert result.skipped_file_uids == frozenset({"f_many"})


def test_両方annotationなしなら登録日時が新しい方が勝つ():
    """★1,190件のケースの再現。D05（最も古い方を残す）とは逆向き。"""
    rows = [
        image_row("f_old", "ChestMetry_PI6px_normal", "20260101_000000", "same.dcm"),
        image_row("f_new", "PTE_CX_MT_PI3_pneumothorax", "20260301_000000", "same.dcm"),
    ]
    by_image = {"f_old": "keep", "f_new": "keep"}

    result = resolve_image_duplicates(rows, [], by_image)

    assert result.skipped_file_uids == frozenset({"f_old"})
    reasons = {r["file_uid"]: r["reason"] for r in result.report_rows}
    assert "新しい" in reasons["f_new"]
    assert "古い" in reasons["f_old"]


def test_登録日時まで同一ならdataset_id昇順で決まる():
    rows = [
        image_row("f_z", "DS_z", "20260101_000000", "same.dcm"),
        image_row("f_a", "DS_a", "20260101_000000", "same.dcm"),
    ]
    result = resolve_image_duplicates(rows, [], {"f_z": "keep", "f_a": "keep"})

    assert result.skipped_file_uids == frozenset({"f_z"})


def test_image単位でexclude確定のものは重複判定に含めない():
    rows = [
        image_row("f_keep", "DS_a", "20260101_000000", "same.dcm"),
        image_row("f_excluded", "DS_b", "20260201_000000", "same.dcm"),
    ]
    by_image = {"f_keep": "keep", "f_excluded": "exclude"}

    result = resolve_image_duplicates(rows, [], by_image)

    assert result.skipped_file_uids == frozenset()
    assert result.report_rows == []


def test_report_rowsに採用とskipの両方が理由付きで記録される():
    rows = [
        image_row("f1", "DS_a", "20260101_000000", "same.dcm"),
        image_row("f2", "DS_b", "20260301_000000", "same.dcm"),
    ]
    result = resolve_image_duplicates(rows, [], {"f1": "keep", "f2": "keep"})

    assert len(result.report_rows) == 2
    for row in result.report_rows:
        assert row["reason"]
        assert row["decision"] in (SELECTED, SKIPPED_DUPLICATE)
        # 通常の採否(keep/exclude)という語をそのまま流用していないこと。
        assert row["decision"] not in ("keep", "exclude")


def test_三者以上の重複でも1件だけ勝つ():
    rows = [
        image_row("f1", "DS_a", "20260101_000000", "same.dcm"),
        image_row("f2", "DS_b", "20260201_000000", "same.dcm"),
        image_row("f3", "DS_c", "20260301_000000", "same.dcm"),
    ]
    by_image = {"f1": "keep", "f2": "keep", "f3": "keep"}

    result = resolve_image_duplicates(rows, [], by_image)

    assert result.skipped_file_uids == frozenset({"f1", "f2"})
    decisions = {r["file_uid"]: r["decision"] for r in result.report_rows}
    assert decisions["f3"] == SELECTED
