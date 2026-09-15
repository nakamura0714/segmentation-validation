"""目視レビュー層のうち fiftyone を必要としない部分。

**過去にここで3件の不具合が出た。** いずれも「目視の成果が静かに壊れる」種類:

1. マスクを持たない bbox 11件が manifest から落ちていた。
   ところがこの11件は M07（座標が壊れている）で、**座標そのものが目視対象**だった。
2. `review export` が機械の判定まで人間の判定として書き出していた（273件 → 実際は3件）。
   `select` で `decision_source=human` になり、reviewer 不在で不変条件が落ちた。
3. `build_manifest`/`build_decisions` が ``geometry_uid`` だけで判定を引いていた。
   cross-dataset 重複（D05）では同じ ``geometry_uid`` が複数データセットに
   再利用されるため、無関係な別データセットの annotation の判定
   （exclude/pending 等）が誤って割り当てられていた（実データで2172件確認）。

`auto:` と `review:` を混ぜないことも Precision 計算の前提なので固定する。
"""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_group, make_record

from segmentation_validation.review import review_schema as rs
from segmentation_validation.review.export_assets import (
    MIN_DISPLAY_PX,
    AssetPaths,
    _json_box,
)
from segmentation_validation.review.export_decisions import (
    _baseline,
    _effective_status,
    _exported_keys,
    _is_human,
    write_flagged_report,
)
from segmentation_validation.review.manifest import (
    _duplicate_hint,
    _elapsed_ja,
    _human_note,
    _human_reasons,
    build_manifest,
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
from segmentation_validation.selection.decisions import Decision, build_decisions

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


DATASET_ID = "DS"
#: annotation側のbaselineキーは annotation_uid（f"{dataset_id}::{geometry_uid}"）。
#: geometry_uid は cross-dataset 重複でデータセットをまたいで再利用されるため、
#: bare geometry_uid だけをキーにすると別データセットの annotation の
#: baseline を誤って参照してしまう（_baseline 参照）。
KEY_A = f"{DATASET_ID}::A"


def make_manifest(annotation_status: str, image_status: str) -> dict:
    return {
        "images": [
            {
                "file_uid": "IMG",
                "dataset_id": DATASET_ID,
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
    assert _is_human(row, ann[KEY_A], manifest) is True
    assert _is_human(row, img["IMG"], manifest) is True


def test_基準線と同じなら機械の判定():
    """★dataset には周辺 annotation も文脈として載る。それを human にしない。"""
    manifest = make_manifest("keep", "keep")
    ann, _ = _baseline(manifest)

    row = {"decision": "keep", "reviewer": ""}
    assert _is_human(row, ann[KEY_A], manifest) is False


def test_reviewerが入っていれば無条件で人間の判定():
    manifest = make_manifest("keep", "keep")
    ann, _ = _baseline(manifest)

    row = {"decision": "keep", "reviewer": "me@example.com"}
    assert _is_human(row, ann[KEY_A], manifest) is True


def test_manifestが無ければreviewerだけで判断する():
    assert _is_human({"decision": "keep", "reviewer": ""}, None, None) is False
    assert _is_human({"decision": "keep", "reviewer": "me@x.com"}, None, None) is True


# --------------------------------------------------------------- export 済みの判定


def test_export済みのキーを読める(tmp_path):
    """画像のキーは dataset_id + inst/study/series/file。

    旧形式（``file_uid``）で書かれていても、``source_json`` を dataset_id へ
    畳んで同じキーになる。これが無いと再エクスポートのたびに画像側の判定が
    「未 export」に見えて `review build` が止まる。
    """
    path = tmp_path / "review_decisions.json"
    path.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "dataset_id": DATASET_ID,
                        "geometry_uid": "A",
                        "decision": "keep",
                    }
                ],
                "image_decisions": [
                    {
                        "file_uid": f"engineer-set-{DATASET_ID}-20260624_080014.json"
                        "::inst/study/series/file",
                        "decision": "exclude",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert _exported_keys(path) == {
        ("annotation", DATASET_ID, "A"),
        ("image", DATASET_ID, "inst/study/series/file"),
    }


def test_ファイルが無ければ空(tmp_path):
    assert _exported_keys(tmp_path / "missing.json") == set()


def test_読めないファイルを全部export済みと解釈しない(tmp_path):
    """★ここで嘘をつくと `review build` が判定を消してしまう。"""
    path = tmp_path / "review_decisions.json"
    path.write_text("{壊れている", encoding="utf-8")

    assert _exported_keys(path) == set()


# --------------------------------------------------------- flag:needs_report


def _flag_row(key: str, **overrides: str) -> dict[str, str]:
    row = {
        "kind": "annotation",
        "dataset_id": DATASET_ID,
        "key": key,
        "institution": "synth_hospital",
        "patient_id": "SYNTH00000001",
        "study": "SYNTH00000001_000",
        "series": "SYNTH00000001_000_000",
        "file_id": "SYNTH00000001_000_000_000",
        "image_path": "/mnt/medical2/synth/F1.dcm",
        "path_mask": f"/synth/mask/{key}.png",
        "path_original_mask": "",
    }
    row.update(overrides)
    return row


def test_flagged_reportは同じキーを重複させない(tmp_path):
    """ボタンを何度押しても行は増え続けるだけで、既存の行は書き換わる。"""
    path = tmp_path / "flagged_for_report.csv"

    assert write_flagged_report(path, [_flag_row("A")]) == 1
    assert write_flagged_report(path, [_flag_row("A")]) == 0  # 同じキーは増えない
    assert write_flagged_report(path, [_flag_row("B")]) == 1

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["key"] for r in rows} == {"A", "B"}


def test_flagged_reportは実ファイルパスを持つ(tmp_path):
    """マスクの実ファイルはコピーせず、パスだけを控える運用が前提。"""
    path = tmp_path / "flagged_for_report.csv"
    write_flagged_report(path, [_flag_row("A")])

    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["path_mask"] == "/synth/mask/A.png"
    assert row["image_path"] == "/mnt/medical2/synth/F1.dcm"


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


# ------------------------------------------------- 重複ペアの「どちらが新しいか」


def make_pair_decision(config, related: str | None):
    """``related_geometry_uid`` だけが要る採否を1件作る。"""
    from segmentation_validation.selection.decisions import build_decisions

    decision = build_decisions([make_record("A")], [], config)[0]
    return replace(decision, related_geometry_uid=related)


def hint(config, own_ts, partner_ts, *, partner_user="partner@example.com"):
    """自分と相手の timestamp を指定して ``_duplicate_hint`` を呼ぶ。"""
    own = replace(make_record("A"), timestamp=own_ts)
    partner = replace(make_record("B"), timestamp=partner_ts, user=partner_user)
    return _duplicate_hint(
        own, make_pair_decision(config, "B"), {"A": own, "B": partner}
    )


def test_相手が新しければ相手が新しいと言う(config):
    """★ここを逆にすると目視の判断が反転する。実データの DUP_0025 と同じ14秒差。"""
    result = hint(config, "2026-07-02 07:50:38+00:00", "2026-07-02 07:50:52+00:00")

    assert result["newer_in_pair"] is False
    assert result["pair_verdict"] == "相手が新しい（14秒差）"


def test_自分が新しければ自分が新しいと言う(config):
    result = hint(config, "2026-07-02 07:50:52+00:00", "2026-07-02 07:50:38+00:00")

    assert result["newer_in_pair"] is True
    assert result["pair_verdict"] == "自分が新しい（14秒差）"


def test_相手の日時と作業者を相手の値で返す(config):
    """★自分の値を返すバグは「相手を見に行かなくてよい」という前提を壊す。"""
    result = hint(config, "2026-07-02 07:50:38+00:00", "2021-10-31 06:11:51+00:00")

    assert result["partner_timestamp"] == "2021-10-31 06:11:51+00:00"
    assert result["partner_annotator"] == "partner@example.com"


def test_同時刻なら判定しない(config):
    """D01 の自動採否と同じ基準。同値は timestamp_tie で決められない。"""
    result = hint(config, "2026-07-02 07:50:38+00:00", "2026-07-02 07:50:38+00:00")

    assert result["newer_in_pair"] is None
    assert result["pair_verdict"] == "同時刻"


def test_timestampが欠けていれば比較不能(config):
    result = hint(config, None, "2026-07-02 07:50:38+00:00")

    assert result["newer_in_pair"] is None
    assert result["pair_verdict"] == "比較不能"
    # 相手の値は分かるので出す。片方が欠けていることが見えるべき。
    assert result["partner_timestamp"] == "2026-07-02 07:50:38+00:00"


def test_解釈できない日時も比較不能(config):
    result = hint(config, "いつか", "2026-07-02 07:50:38+00:00")

    assert result["pair_verdict"] == "比較不能"


def test_相手が同じ画像に居なければ何も返さない(config):
    """D05（クロスデータセット重複）の相手は別画像にいるので引けない。"""
    own = make_record("A")
    result = _duplicate_hint(own, make_pair_decision(config, "B"), {"A": own})

    assert result == {}


def test_重複ペアでなければ何も返さない(config):
    own = make_record("A")
    result = _duplicate_hint(own, make_pair_decision(config, None), {"A": own})

    assert result == {}


def test_採否がまだ無ければ何も返さない(config):
    assert _duplicate_hint(make_record("A"), None, {}) == {}


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0秒"),
        (14, "14秒"),
        (59, "59秒"),
        (60, "1分"),
        (3599, "59分"),
        (3600, "1時間"),
        (86399, "23時間"),
        (86400, "1日"),
        (86400 * 3, "3日"),
        (-14, "14秒"),  # 符号は pair_verdict の文言側で表す
    ],
)
def test_経過時間の丸め(seconds, expected):
    """「14秒差」と「3日差」の違いが判断を分ける。桁は揃えない。"""
    assert _elapsed_ja(seconds) == expected


# --------------------------------------------------- exclude の理由（チェックボックス）


def test_候補と機械の理由は重ならない():
    """★``effective_reasons`` は「候補に絞る」ことで機械の理由を落としている。

    どちらかに同じ値を足すとその前提が崩れ、機械の理由が人間の理由として
    書き出されるようになる（実際に ``review_required`` でそれが起きていた）。
    """
    assert not set(rs.ALL_SUGGESTED_REASONS) & rs.MACHINE_REASONS


def test_機械の理由は人間の理由として返さない():
    """★これが今回の不具合の核。

    manifest が初期値として流し込んだ ``review_required`` が人間の理由として
    export され、目視102件の理由が全部それになっていた。
    """
    for machine in ("review_required", "no_issue_detected", "older_exact_duplicate"):
        assert rs.effective_reasons(None, [], machine) == [], machine
        assert rs.effective_reasons([machine], [], "") == [], machine
        assert rs.effective_reasons(None, [rs.reason_tag(machine)], "") == [], machine


def test_採否そのものを理由として返さない():
    """``build_decisions`` は人間の理由が空だと採否を reason に入れる。

    それは「理由が無い」ことの表現なので、理由として読み戻してはいけない。
    """
    assert rs.effective_reasons(None, [], "exclude") == []
    assert rs.effective_reasons(None, [], "keep") == []


def test_フィールドが理由の正本():
    """タグはフィールドの写し。食い違ったらフィールドを採る。"""
    reasons = rs.effective_reasons(
        ["invalid_duplicate"], [rs.reason_tag("outside_body")], "wrong_label"
    )
    assert reasons == ["invalid_duplicate"]


def test_フィールドが空ならタグを見る():
    """App でタグだけ付けた場合を拾う（``effective_status`` と同じ構え）。"""
    reasons = rs.effective_reasons(
        [], ["auto:s05_outside_body", rs.reason_tag("outside_body")], ""
    )
    assert reasons == ["outside_body"]


def test_タグも空なら自由記述を見る():
    """`review_reasons` を入れる前に自由記述で運用していた分の後方互換。"""
    assert rs.effective_reasons([], [], "lateral_view") == ["lateral_view"]


def test_理由は候補の並び順に正規化する():
    """入力順で ``a|b`` と ``b|a`` に割れると集計が分かれる。"""
    order = rs.effective_reasons(["older_duplicate", "invalid_duplicate"])
    assert order == ["invalid_duplicate", "older_duplicate"]
    assert rs.effective_reasons(["invalid_duplicate", "older_duplicate"]) == order
    # タグ経路でも同じ（集合を経由するので順序が不定になりやすい）。
    assert rs.effective_reasons([], rs.reason_tags(order[::-1])) == order


def test_候補外の値は拾わない():
    """App では tags に何でも打てる。知らない値を理由にすると集計が壊れる。"""
    assert rs.effective_reasons(["nonsense"], [], "") == []
    assert rs.effective_reasons([], ["reason:nonsense"], "") == []
    assert rs.parse_reason_tag("reason:nonsense") is None
    assert rs.reason_tags(["invalid_duplicate", "nonsense"]) == [
        "reason:invalid_duplicate"
    ]


def test_reasonタグは往復する():
    for reason in rs.ALL_SUGGESTED_REASONS:
        assert rs.parse_reason_tag(rs.reason_tag(reason)) == reason


def test_reviewタグはreasonタグとして解釈されない():
    assert rs.parse_reason_tag("review:exclude") is None
    assert rs.parse_reason_tag("auto:s05_outside_body") is None
    assert rs.parse_review_tag(rs.reason_tag("outside_body")) is None


def test_reasonタグは人間のものとして扱われる():
    """★``is_review()`` が False になると ``review import`` が消さなくなる代わりに、
    古い理由タグが貼り替えられず残る。True であることが前提。
    """
    tag = rs.reason_tag("invalid_duplicate")
    assert rs.is_review(tag) is True
    assert rs.is_auto(tag) is False


def test_区切りの往復():
    reasons = ["invalid_duplicate", "older_duplicate"]
    assert rs.join_reasons(reasons) == "invalid_duplicate|older_duplicate"
    assert rs.split_reasons("invalid_duplicate|older_duplicate") == reasons
    assert rs.split_reasons(None) == []
    assert rs.split_reasons("") == []
    assert rs.split_reasons(reasons) == reasons


def test_古いと重複は別の理由():
    """同じマスクが2枚あることと、自分が古い方であることは独立した事実。"""
    assert "invalid_duplicate" in rs.SUGGESTED_REASONS["exclude"]
    assert "older_duplicate" in rs.SUGGESTED_REASONS["exclude"]


def test_全候補に日本語表示がある():
    """ダッシュボードと summary.md が引くので、欠けると値が生で出る。"""
    missing = [r for r in rs.ALL_SUGGESTED_REASONS if r not in rs.REASON_JA]
    assert missing == []


# ------------------------------------------- manifest が機械の理由を流し込まない


def make_decision(config, reason: str, source=None):
    from segmentation_validation.selection.decisions import DecisionSource

    decision = build_decisions([make_record("A")], [], config)[0]
    return replace(
        decision,
        reason=reason,
        decision_source=source or DecisionSource.HUMAN,
    )


def test_機械の理由は理由欄に流し込まない(config):
    """★目視待ちの annotation には機械の review_required が入っている。

    それを App の理由欄に初期値として置くと、人間が触らないまま
    「人間が review_required と入力した」ことになる。
    """
    decision = make_decision(config, "review_required")

    assert _human_reasons(decision) == []
    assert _human_note(decision) == ""


def test_候補の理由はチェックボックス側に入る(config):
    decision = make_decision(config, "invalid_duplicate|older_duplicate")

    assert _human_reasons(decision) == ["invalid_duplicate", "older_duplicate"]
    # 二重に持つと食い違うので自由記述欄には残さない。
    assert _human_note(decision) == ""


def test_候補外の記述は自由記述欄に残る(config):
    """候補で表せないことを書きたいときの逃げ道。"""
    decision = make_decision(config, "Dr確認済みだが要再確認")

    assert _human_reasons(decision) == []
    assert _human_note(decision) == "Dr確認済みだが要再確認"


def test_採否が無ければ理由も空(config):
    assert _human_reasons(None) == []
    assert _human_note(None) == ""


# --------------------- cross-dataset 重複でgeometry_uidが衝突しても混同しない


def test_manifestはgeometry_uid衝突時も正しいannotationに判定を割り当てる(
    config,
):
    """★実データで2172件確認した不具合の再現。

    D05（cross-dataset 重複）では同じ ``geometry_uid`` が別データセットの
    annotation に再利用される。``build_manifest`` が bare geometry_uid で
    決定/Issueを引いていたため、片方の決定（exclude）がもう片方
    （本来 pending のはず）へ誤って割り当てられていた。
    """
    record_a = make_record("X", dataset_id="DS1", file="F1")
    record_b = make_record("X", dataset_id="DS2", file="F2")
    group_a = make_group((record_a,), dataset_id="DS1", file="F1")
    group_b = make_group((record_b,), dataset_id="DS2", file="F2")

    # 実際には D05 が決める採否だが、ここでは manifest 側の突き合わせだけを
    # 見たいので、build_decisions の出力を直接書き換えて2通りの決定を作る。
    decision_a = replace(
        build_decisions([record_a], [], config)[0],
        final_decision=Decision.PENDING,
        reason="review_required",
    )
    decision_b = replace(
        build_decisions([record_b], [], config)[0],
        final_decision=Decision.EXCLUDE,
        reason="cross_dataset_duplicate",
    )

    box = [0.1, 0.1, 0.2, 0.2]
    assets = {
        group_a.file_uid: AssetPaths(
            file_uid=group_a.file_uid, image=Path("F1.png"), boxes={"X": box}
        ),
        group_b.file_uid: AssetPaths(
            file_uid=group_b.file_uid, image=Path("F2.png"), boxes={"X": box}
        ),
    }

    manifest = build_manifest(
        [group_a, group_b],
        assets,
        [decision_a, decision_b],
        [],
        [],
        config,
    )

    by_file = {image["file_uid"]: image for image in manifest["images"]}
    ann_a = by_file[group_a.file_uid]["annotations"][0]
    ann_b = by_file[group_b.file_uid]["annotations"][0]

    assert ann_a["review_status"] == "pending", "DS1 側は本来 pending のまま"
    assert ann_b["review_status"] == "exclude", "DS2 側は本来 exclude のまま"
