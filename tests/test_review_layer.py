"""目視レビュー層のうち fiftyone を必要としない部分。

**過去にここで2件の不具合が出た。** 両方とも「目視の成果が静かに壊れる」種類:

1. マスクを持たない bbox 11件が manifest から落ちていた。
   ところがこの11件は M07（座標が壊れている）で、**座標そのものが目視対象**だった。
2. `review export` が機械の判定まで人間の判定として書き出していた（273件 → 実際は3件）。
   `select` で `decision_source=human` になり、reviewer 不在で不変条件が落ちた。

`auto:` と `review:` を混ぜないことも Precision 計算の前提なので固定する。
"""

from __future__ import annotations

import json

from conftest import make_record

from segmentation_validation.review.export_assets import MIN_DISPLAY_PX, _json_box
from segmentation_validation.review.export_decisions import (
    _baseline,
    _effective_status,
    _exported_keys,
    _is_human,
)
from segmentation_validation.review.review_schema import (
    AUTO_PREFIX,
    REVIEW_PREFIX,
    ReviewStatusTag,
    auto_tag,
    is_auto,
    is_review,
    parse_review_tag,
    review_tag,
)

SHAPE = (2400, 2000)  # (height, width)


# --------------------------------------------------------------- タグ


def test_autoとreviewのタグは混ざらない():
    """★機械の検出と人間の判定が混ざると Precision が計算できなくなる。"""
    machine = auto_tag("S05_OUTSIDE_BODY")
    human = review_tag(ReviewStatusTag.EXCLUDE.value)

    assert machine.startswith(AUTO_PREFIX)
    assert human.startswith(REVIEW_PREFIX)
    assert is_auto(machine) and not is_review(machine)
    assert is_review(human) and not is_auto(human)


def test_reviewタグは往復する():
    for status in ReviewStatusTag:
        assert parse_review_tag(review_tag(status.value)) == status.value


def test_autoタグはreviewとして解釈されない():
    assert parse_review_tag(auto_tag("S05_OUTSIDE_BODY")) is None


# --------------------------------------------------------------- 判定の読み取り


def test_フィールドを正とする():
    """タグは絞り込み用の写し。取り違えやすいのでフィールドを優先する。"""
    status = _effective_status("exclude", ["review:keep"])
    assert status == "exclude"


def test_フィールドが未判定ならタグを見る():
    """App でタグ付けだけした場合を拾う。"""
    status = _effective_status("pending", ["auto:s05_outside_body", "review:exclude"])
    assert status == "exclude"


def test_どちらも未判定ならpending():
    assert _effective_status(None, []) == "pending"
    assert _effective_status("pending", ["auto:s03_tiny_annotation"]) == "pending"


# --------------------------------------------------------------- 人間の判定の切り分け


def make_manifest(annotation_status: str, image_status: str) -> dict:
    return {
        "images": [
            {
                "file_uid": "IMG",
                "review_status": image_status,
                "annotations": [
                    {"geometry_uid": "A", "review_status": annotation_status}
                ],
            }
        ]
    }


def test_基準線から変わっていれば人間の判定():
    """★reviewer の入力漏れに強い方式。manifest を基準線にする。"""
    manifest = make_manifest("pending", "pending")
    ann, img = _baseline(manifest)

    row = {"decision": "exclude", "reviewer": ""}
    assert _is_human(row, ann["A"], manifest) is True
    assert _is_human(row, img["IMG"], manifest) is True


def test_基準線と同じなら機械の判定():
    """★dataset には周辺 annotation も文脈として載る。それを human にしない。"""
    manifest = make_manifest("keep", "keep")
    ann, _ = _baseline(manifest)

    row = {"decision": "keep", "reviewer": ""}
    assert _is_human(row, ann["A"], manifest) is False


def test_reviewerが入っていれば無条件で人間の判定():
    manifest = make_manifest("keep", "keep")
    ann, _ = _baseline(manifest)

    row = {"decision": "keep", "reviewer": "me@example.com"}
    assert _is_human(row, ann["A"], manifest) is True


def test_manifestが無ければreviewerだけで判断する():
    assert _is_human({"decision": "keep", "reviewer": ""}, None, None) is False
    assert _is_human({"decision": "keep", "reviewer": "me@x.com"}, None, None) is True


# --------------------------------------------------------------- export 済みの判定


def test_export済みのキーを読める(tmp_path):
    path = tmp_path / "review_decisions.json"
    path.write_text(
        json.dumps(
            {
                "decisions": [{"geometry_uid": "A", "decision": "keep"}],
                "image_decisions": [{"file_uid": "IMG", "decision": "exclude"}],
            }
        ),
        encoding="utf-8",
    )

    assert _exported_keys(path) == {("annotation", "A"), ("image", "IMG")}


def test_ファイルが無ければ空(tmp_path):
    assert _exported_keys(tmp_path / "missing.json") == set()


def test_読めないファイルを全部export済みと解釈しない(tmp_path):
    """★ここで嘘をつくと `review build` が判定を消してしまう。"""
    path = tmp_path / "review_decisions.json"
    path.write_text("{壊れている", encoding="utf-8")

    assert _exported_keys(path) == set()


# --------------------------------------------------------------- マスクの無い bbox


def test_通常のbboxはそのまま相対座標になる():
    record = make_record("A", has_mask=False, json_bbox=(200, 400, 600, 1000))
    box, adjusted = _json_box(record, SHAPE)

    assert adjusted == {}
    assert box == [200 / 2000, 400 / 2400, 400 / 2000, 600 / 2400]


def test_退化したbboxは表示できる大きさまで広げる():
    """★広げないと画面に現れず、座標が壊れていることを確認できない。"""
    record = make_record("A", has_mask=False, json_bbox=(500, 500, 500, 500))
    box, adjusted = _json_box(record, SHAPE)

    assert adjusted["original_bbox"] == [500, 500, 500, 500]
    assert adjusted["reason"] == "degenerate_widened_for_display"
    assert box[2] == MIN_DISPLAY_PX / 2000
    assert box[3] == MIN_DISPLAY_PX / 2400


def test_min_maxが逆転したbboxも表示できる():
    record = make_record("A", has_mask=False, json_bbox=(600, 600, 500, 500))
    box, adjusted = _json_box(record, SHAPE)

    assert adjusted["original_bbox"] == [600, 600, 500, 500]
    assert box is not None
    assert box[2] > 0 and box[3] > 0


def test_範囲外のbboxはクリップせず外に出す():
    """はみ出していることが見えるべきなので、画像内に押し込めない。"""
    record = make_record("A", has_mask=False, json_bbox=(1900, 2300, 2400, 2900))
    box, adjusted = _json_box(record, SHAPE)

    assert adjusted == {}
    assert box[0] + box[2] > 1.0


def test_極端に範囲外ならfiftyoneが扱える範囲に収める():
    record = make_record("A", has_mask=False, json_bbox=(0, 0, 99999, 99999))
    box, adjusted = _json_box(record, SHAPE)

    assert adjusted["reason"] == "out_of_image_clamped_for_display"
    assert adjusted["original_bbox"] == [0, 0, 99999, 99999]
    assert all(-0.5 <= v <= 1.5 for v in box)


def test_画像サイズが不明なら枠を出さない():
    record = make_record("A", has_mask=False)
    box, adjusted = _json_box(record, (0, 0))

    assert box is None
    assert adjusted == {}
