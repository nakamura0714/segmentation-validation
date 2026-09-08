"""D05（クロスデータセット重複）の検出・自動採否。

PTE/PTR/mask136 実例（``docs/review_procedure.md``）を模した合成データで、
D01-D04 が構造的に検出できないケース（``FileGroup``/``file_uid`` を跨いだ
重複）を D05 が拾えることを担保する。
"""

from __future__ import annotations

from conftest import NODULE, make_mask_measurement, make_record

from segmentation_validation.checks.duplicate.d05_cross_dataset_duplicate import (
    MISMATCH_CHECK_ID,
    TIE_CHECK_ID,
    UNDECIDABLE_MISSING,
    UNDECIDABLE_TIE,
    detect,
)
from segmentation_validation.checks.duplicate.d05_cross_dataset_duplicate import (
    CHECK_ID as DUPLICATE_CHECK_ID,
)
from segmentation_validation.selection.decisions import Decision, Reason

OLD = "2026-01-01 00:00:00+00:00"
NEW = "2026-06-01 00:00:00+00:00"


def _masks(*pairs):
    """``(record, mask_hash)`` の並びから ``ctx.masks`` 形式の辞書を作る。"""
    return {
        (r.dataset_id, r.geometry_uid, m.role): m
        for r, mh in pairs
        for m in [make_mask_measurement(r, mask_hash=mh)]
    }


def test_内容一致の2データセット間重複は新しい方だけ残す():
    a = make_record("A", dataset_id="PTE", timestamp=OLD)
    b = make_record("A", dataset_id="PTR", timestamp=NEW)
    masks = _masks((a, "same-hash"), (b, "same-hash"))

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == {a.annotation_uid}
    assert result.automatic[a.annotation_uid].decision is Decision.EXCLUDE
    assert result.automatic[a.annotation_uid].reason == Reason.CROSS_DATASET_DUPLICATE.value
    assert result.automatic[a.annotation_uid].kept_geometry_uid == "A"
    assert b.annotation_uid not in result.excluded_annotation_uids
    assert all(i.check_id == DUPLICATE_CHECK_ID for i in result.issues)
    # 両側から1件ずつ（emit_pairと同じ「1ペア=2 Issue」）。
    assert len(result.issues) == 2


def test_同一datasetどうしは対象にしない():
    """同一dataset内の重複はD01-D04の仕事。D05は別データセットどうしだけ見る。"""
    a = make_record("A", dataset_id="PTE", timestamp=OLD)
    b = make_record("B", dataset_id="PTE", timestamp=NEW)
    masks = _masks((a, "same-hash"), (b, "same-hash"))

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == frozenset()
    assert result.issues == ()


def test_3データセットにまたがる重複は連結成分で束ねて最新1件だけ残す():
    """PTE/PTR/mask136 の3データセットにまたがる実例を模したケース。"""
    etr = make_record("A", dataset_id="ETR", timestamp="2026-01-01 00:00:00+00:00")
    pte = make_record("A", dataset_id="PTE", timestamp="2026-02-01 00:00:00+00:00")
    ptr = make_record("A", dataset_id="PTR", timestamp="2026-03-01 00:00:00+00:00")
    masks = _masks((etr, "same-hash"), (pte, "same-hash"), (ptr, "same-hash"))

    result = detect([etr, pte, ptr], masks)

    assert result.excluded_annotation_uids == {etr.annotation_uid, pte.annotation_uid}
    assert result.automatic[etr.annotation_uid].kept_geometry_uid == "A"
    assert result.automatic[pte.annotation_uid].kept_geometry_uid == "A"
    assert ptr.annotation_uid not in result.excluded_annotation_uids
    # 代表(ptr)にも情報用のKEEPエントリが入る。
    assert result.automatic[ptr.annotation_uid].decision is Decision.KEEP


def test_同一geometry_uidなのに内容が食い違うのはmismatchとして必ず目視():
    """geometry_uidの一致だけでは同一扱いにしない、の安全網。"""
    a = make_record("SAME_UID", dataset_id="PTE", timestamp=OLD)
    b = make_record("SAME_UID", dataset_id="PTR", timestamp=NEW)
    masks = _masks((a, "hash-a"), (b, "hash-b"))  # 内容（マスク）が違う

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == frozenset()
    assert result.automatic == {}
    assert len(result.issues) == 2
    assert all(i.check_id == MISMATCH_CHECK_ID for i in result.issues)
    assert all(i.detail["same_geometry_uid"] is True for i in result.issues)


def test_ラベルが違えば内容一致とみなさない():
    """geometry_uidが同じでもラベルが違うなら、それもmismatch扱い。"""
    a = make_record("SAME_UID", dataset_id="PTE", timestamp=OLD)
    b = make_record("SAME_UID", dataset_id="PTR", timestamp=NEW, labels=(NODULE,))
    masks = _masks((a, "same-hash"), (b, "same-hash"))

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == frozenset()
    assert all(i.check_id == MISMATCH_CHECK_ID for i in result.issues)


def test_マスクが無いbboxのみのannotationはbboxで内容一致を判定する():
    bbox = (5, 5, 50, 50)
    a = make_record(
        "A", dataset_id="PTE", timestamp=OLD, has_mask=False, json_bbox=bbox
    )
    b = make_record(
        "A", dataset_id="PTR", timestamp=NEW, has_mask=False, json_bbox=bbox
    )

    result = detect([a, b], masks={})

    assert result.excluded_annotation_uids == {a.annotation_uid}


def test_bboxが違うbboxのみのannotationは重複とみなさない():
    a = make_record(
        "A", dataset_id="PTE", timestamp=OLD, has_mask=False, json_bbox=(0, 0, 10, 10)
    )
    b = make_record(
        "B", dataset_id="PTR", timestamp=NEW, has_mask=False, json_bbox=(50, 50, 60, 60)
    )

    result = detect([a, b], masks={})

    assert result.excluded_annotation_uids == frozenset()
    assert result.issues == ()


def test_timestampが同点なら自動決定せず全員を目視に回す():
    """annotationのtimestampも、元データセットJSONの生成日時も同点で、
    どちらでも決着しないケース（両方とも本当に同時に生成された場合）。"""
    same_source = "engineer-set-SAME-20260101_000000.json"
    a = make_record(
        "A", dataset_id="PTE", source_json=same_source, timestamp=OLD
    )
    b = make_record(
        "A", dataset_id="PTR", source_json=same_source, timestamp=OLD
    )
    masks = _masks((a, "same-hash"), (b, "same-hash"))

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == frozenset()
    assert result.automatic == {}
    assert len(result.issues) == 2
    assert all(i.check_id == TIE_CHECK_ID for i in result.issues)
    assert all(i.detail["undecidable_reason"] == UNDECIDABLE_TIE for i in result.issues)


def test_timestamp同点でも元データセットJSONの生成日時が古い方を代表にする():
    """★実データの実際のパターン: クロスデータセット重複は同一DBレコードの

    再エクスポートなので annotation の ``timestamp`` は271件中271件が完全同点。
    その場合は元データセットJSON（``engineer-set-...-YYYYMMDD_HHMMSS.json``）の
    生成日時が最も古い方＝最初に登録された方を代表として残す。
    """
    mask136 = make_record(
        "A",
        dataset_id="ETR_ChestMetry_PI6px_with_mask136",
        source_json="engineer-set-ETR_ChestMetry_PI6px_with_mask136-20260624_080014.json",
        timestamp=OLD,
    )
    pte = make_record(
        "A",
        dataset_id="PTE_CX_MT_PI3_pneumothorax",
        source_json="engineer-set-PTE_CX_MT_PI3_pneumothorax-20260904_061537.json",
        timestamp=OLD,
    )
    masks = _masks((mask136, "same-hash"), (pte, "same-hash"))

    result = detect([mask136, pte], masks)

    assert result.excluded_annotation_uids == {pte.annotation_uid}
    assert result.automatic[pte.annotation_uid].decision is Decision.EXCLUDE
    assert result.automatic[pte.annotation_uid].kept_geometry_uid == "A"
    assert result.automatic[mask136.annotation_uid].decision is Decision.KEEP


def test_timestampが欠損なら自動決定せず全員を目視に回す():
    a = make_record("A", dataset_id="PTE", timestamp=None)
    b = make_record("A", dataset_id="PTR", timestamp=NEW)
    masks = _masks((a, "same-hash"), (b, "same-hash"))

    result = detect([a, b], masks)

    assert result.excluded_annotation_uids == frozenset()
    assert result.automatic == {}
    assert all(i.check_id == TIE_CHECK_ID for i in result.issues)
    assert all(i.detail["undecidable_reason"] == UNDECIDABLE_MISSING for i in result.issues)


def test_重複が無ければ何も検出しない():
    a = make_record("A", dataset_id="PTE", timestamp=OLD)
    masks = _masks((a, "hash1"))

    result = detect([a], masks)

    assert result.issues == ()
    assert result.automatic == {}
    assert result.excluded_annotation_uids == frozenset()
