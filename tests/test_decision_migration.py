"""fingerprint を跨いだ目視判定の引き継ぎを守る。

このプロジェクトで最も壊れやすかったのがここ。fingerprint は対象JSONの
``name:size:mtime_ns:sha256`` から決まるので、**データセットを1本足すだけ・
symlink を張り替えるだけ**で変わる。以前は判定が
``output/validation/<fingerprint>/review/`` にしか無く、引き継ぎ機構も無かった
ため、そのたびに目視結果が pending へ戻っていた。

さらに画像側は ``file_uid``（= ``source_json::inst/study/series/file``）を
キーにしており、``source_json`` は日時スタンプ込みのファイル名そのものなので、
元JSONを再エクスポートするだけで**全件**引き当て不能になっていた。しかも
書き出しは追記マージで古い行が残るため、「件数はあるのに1件も適用されない」
という気づきにくい壊れ方をする。

ここで固定するのは次の2つ。

1. 旧形式（schema v1）の ``file_uid`` から ``dataset_id`` + ``image_key`` を
   機械的に導出できること（移行が可逆で、情報を落とさない）。
2. **一度判定した annotation / 画像が、データセット追加や再エクスポートだけを
   理由に pending へ戻らないこと。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import DATASET, FILE, INSTITUTION, SERIES, STUDY, make_group

from segmentation_validation.config import Config
from segmentation_validation.review import decision_store as store
from segmentation_validation.selection.decisions import (
    Decision,
    DecisionSource,
    ReviewStatus,
)
from segmentation_validation.selection.image_decisions import build_image_decisions

#: 同じデータセットの、日時スタンプだけが違う2つのエクスポート。
OLD_SOURCE = f"engineer-set-{DATASET}-20260624_080014.json"
NEW_SOURCE = f"engineer-set-{DATASET}-20260709_004526.json"

IMAGE_PATH_KEY = f"{INSTITUTION}/{STUDY}/{SERIES}/{FILE}"


def write_v1(path: Path, decisions: list[dict], image_decisions: list[dict]) -> None:
    """schema_version を持たない旧形式のファイルを書く。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "meta": {"dataset_name": "synth"},
                "decisions": decisions,
                "image_decisions": image_decisions,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------------ v1 → v2


def test_v1のfile_uidからdataset_idとimage_keyを導出できる(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    write_v1(
        path,
        [{"dataset_id": DATASET, "geometry_uid": "A", "decision": "exclude"}],
        [{"file_uid": f"{OLD_SOURCE}::{IMAGE_PATH_KEY}", "decision": "keep"}],
    )

    rows = store.load_rows(path)
    images = [r for r in rows if r["kind"] == store.KIND_IMAGE]
    assert len(images) == 1
    assert images[0]["dataset_id"] == DATASET
    assert images[0]["key"] == IMAGE_PATH_KEY
    # 元の file_uid は追跡用に残る（突合には使わない）。
    assert images[0]["legacy_file_uid"] == f"{OLD_SOURCE}::{IMAGE_PATH_KEY}"


def test_再エクスポートした2世代のv1が同じキーへ畳まれる(tmp_path: Path) -> None:
    """★これが成り立たないと、再エクスポートのたびに画像の判定が増殖する。"""
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old_row = {"file_uid": f"{OLD_SOURCE}::{IMAGE_PATH_KEY}", "decision": "keep"}
    new_row = {"file_uid": f"{NEW_SOURCE}::{IMAGE_PATH_KEY}", "decision": "keep"}
    write_v1(old, [], [old_row])
    write_v1(new, [], [new_row])

    merged, conflicts = store.merge_rows(store.load_rows(old), store.load_rows(new))
    assert len(merged) == 1
    assert conflicts == []


def test_dataset_idを持たないannotation行はそのまま読まれる(tmp_path: Path) -> None:
    """読み込みは情報を落とさない。捨てる判断は使う側（select / 移行）が行う。"""
    path = tmp_path / "review_decisions.json"
    write_v1(path, [{"geometry_uid": "A", "decision": "keep"}], [])
    rows = store.load_rows(path)
    assert len(rows) == 1
    assert rows[0]["dataset_id"] is None


def test_導出できないfile_uidは読み飛ばされる(tmp_path: Path) -> None:
    path = tmp_path / "review_decisions.json"
    write_v1(path, [], [{"file_uid": "区切りの無いキー", "decision": "keep"}])
    assert store.load_rows(path) == []


def test_v1を読んでv2で書き直せる(tmp_path: Path) -> None:
    src = tmp_path / "v1.json"
    dst = tmp_path / "v2.json"
    write_v1(
        src,
        [{"dataset_id": DATASET, "geometry_uid": "A", "decision": "exclude"}],
        [{"file_uid": f"{OLD_SOURCE}::{IMAGE_PATH_KEY}", "decision": "keep"}],
    )
    store.write_rows(dst, store.load_rows(src), "synth")

    payload = json.loads(dst.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["image_decisions"][0]["image_key"] == IMAGE_PATH_KEY
    assert payload["image_decisions"][0]["dataset_id"] == DATASET


def test_複数世代の判定がreviewed_at順にマージされる(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_v1(
        a,
        [
            {
                "dataset_id": DATASET,
                "geometry_uid": "A",
                "decision": "keep",
                "reviewed_at": "2026-09-01T00:00:00+00:00",
            }
        ],
        [],
    )
    write_v1(
        b,
        [
            {
                "dataset_id": DATASET,
                "geometry_uid": "A",
                "decision": "exclude",
                "reviewed_at": "2026-09-05T00:00:00+00:00",
            }
        ],
        [],
    )
    merged, conflicts = store.merge_rows(store.load_rows(a), store.load_rows(b))
    assert [r["decision"] for r in merged] == ["exclude"]
    assert [c.resolved_by for c in conflicts] == ["reviewed_at"]


# ------------------------------------------------- fingerprint を跨ぐ引き継ぎ


def test_再エクスポートで画像の判定がpendingに戻らない(config: Config) -> None:
    """★中心の要件（画像側）。

    ``file_uid`` は source_json を含むので再エクスポートで変わる。それでも
    人間の判定が当たり続けることを、新旧2世代の FileGroup で確かめる。
    """
    human = {
        store.image_key(DATASET, INSTITUTION, STUDY, SERIES, FILE): _human(
            Decision.KEEP, "frontal_view"
        )
    }

    for source_json in (OLD_SOURCE, NEW_SOURCE):
        group = make_group((), source_json=source_json)
        decision = build_image_decisions([group], [], config, human=human)[0]
        assert decision.final_decision is Decision.KEEP
        assert decision.decision_source is DecisionSource.HUMAN
        assert decision.review_status is ReviewStatus.REVIEWED

    # file_uid は世代ごとに違うが、突合キーは同じ。
    old_group = make_group((), source_json=OLD_SOURCE)
    new_group = make_group((), source_json=NEW_SOURCE)
    assert old_group.file_uid != new_group.file_uid
    assert old_group.stable_file_uid == new_group.stable_file_uid


def test_旧キーのままでは引き当てられないことを明示する(config: Config) -> None:
    """回帰の向きを固定する。

    ``file_uid`` をキーにした判定は、再エクスポート後には当たらない。
    この挙動こそが直した対象なので、逆戻りしたら気づけるようにしておく。
    """
    human = {make_group((), source_json=OLD_SOURCE).file_uid: _human(Decision.KEEP)}
    group = make_group((), source_json=NEW_SOURCE)
    decision = build_image_decisions([group], [], config, human=human)[0]
    assert decision.decision_source is not DecisionSource.HUMAN


@pytest.mark.parametrize("source_json", [OLD_SOURCE, NEW_SOURCE])
def test_annotationの突合キーは再エクスポートで変わらない(source_json: str) -> None:
    from conftest import make_record

    record = make_record("A", source_json=source_json)
    assert record.annotation_uid == f"{DATASET}::A"


def _human(decision: Decision, reason: str = "visually_valid"):
    from segmentation_validation.selection.decisions import HumanDecision

    return HumanDecision(
        geometry_uid="unused",
        decision=decision,
        reason=reason,
        reviewer="reviewer@example.com",
        reviewed_at="2026-09-01T00:00:00+00:00",
        dataset_id=DATASET,
    )
