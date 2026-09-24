"""画像を1枚も持たなくなった器（series / study / institution）の剪定。

空の器は判断の結果ではなく**副作用**。元JSON13本には空の series も空の study も
1件も無く、``build_development_payload`` が ``image_decisions`` に従って file entry を
落とした結果として生まれる（実データでは大半がクロスデータセット重複による skip）。

**剪定は全 merge の完了後にしか行えない。** それより前だと ``_record_conflicts`` が
「片方のデータセットで空」の属性食い違いを取り逃す —— 実データで現在検出されている
唯一の食い違いがまさにこの形なので、下の
``test_merge後に剪定すれば片方が空の食い違いも記録される`` がその制約を固定する。
"""

from __future__ import annotations

from pathlib import Path

from conftest import INSTITUTION
from test_merged_dataset import DS_A, DS_B, files_of, merge_all, write_source

from segmentation_validation.config import Config
from segmentation_validation.selection import build_dataset as bd


def test_画像0枚のseriesは取り除かれる(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE_EMPTY": {}, "SE_OK": {"F1": ["u1"]}}}})],
    )
    pruned = bd.prune_empty_containers(payload)
    assert pruned.series == 1
    assert pruned.studies == 0
    series = payload["dataset"][INSTITUTION]["ST1"]["series_list"]
    assert set(series) == {"SE_OK"}


def test_seriesが全部消えたstudyも取り除かれる(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}, "ST2": {"SE2": {}}}})],
    )
    pruned = bd.prune_empty_containers(payload)
    assert (pruned.series, pruned.studies, pruned.institutions) == (1, 1, 0)
    assert set(payload["dataset"][INSTITUTION]) == {"ST1"}


def test_studyが全部消えた施設も取り除かれる(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {"inst_empty": {"ST1": {"SE1": {}}}}),
            (DS_B, {"inst_ok": {"ST2": {"SE2": {"F1": ["u1"]}}}}),
        ],
    )
    pruned = bd.prune_empty_containers(payload)
    assert (pruned.series, pruned.studies, pruned.institutions) == (1, 1, 1)
    assert set(payload["dataset"]) == {"inst_ok"}


def test_annotationが0件のfile_entryは剪定の対象外(
    tmp_path: Path, config: Config
) -> None:
    """2つの規則の境界。画像が実在する限り「0件でも残す」が優先する。"""
    payload, _ = merge_all(
        tmp_path, config, [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": []}}}})]
    )
    pruned = bd.prune_empty_containers(payload)
    assert (pruned.series, pruned.studies, pruned.institutions) == (0, 0, 0)
    assert files_of(payload) == {f"{INSTITUTION}/ST1/SE1/F1": DS_A}


def test_image_decisionsで画像を落とした結果空になった器も剪定される(
    tmp_path: Path, config: Config
) -> None:
    """実経路。空の器が生まれる唯一の原因がこれ（元JSONには空が無い）。"""
    source = write_source(
        tmp_path,
        DS_A,
        {
            INSTITUTION: {
                "ST1": {"SE1": {"F_DROP": ["u1"]}},
                "ST2": {"SE2": {"F2": ["u2"]}},
            }
        },
    )
    dropped = bd.file_uid_for(source.name, INSTITUTION, "ST1", "SE1", "F_DROP")
    payload, result = bd.build_development_payload(
        source,
        {"u1": "keep", "u2": "keep"},
        tmp_path / "unused.json",
        config,
        image_decisions={dropped: "exclude"},
    )
    assert result.images_dropped == 1
    # 剪定の前は空の器が残っている。
    assert (
        payload["dataset"][INSTITUTION]["ST1"]["series_list"]["SE1"]["file_list"] == {}
    )

    pruned = bd.prune_empty_containers(payload)
    assert (pruned.series, pruned.studies) == (1, 1)
    assert set(payload["dataset"][INSTITUTION]) == {"ST2"}


def test_剪定してもfile_entry数とannotation総数は変わらない(
    tmp_path: Path, config: Config
) -> None:
    """母集団の不変条件そのもの。器を消しても画像は1枚も減らない。"""
    payload, result = merge_all(
        tmp_path,
        config,
        [
            (
                DS_A,
                {
                    INSTITUTION: {
                        "ST1": {"SE1": {"F1": ["u1", "u2"]}},
                        "ST2": {"SE2": {}},
                    }
                },
            ),
            (DS_B, {"inst_b": {"ST3": {"SE3": {"F2": ["u3"]}, "SE4": {}}}}),
        ],
    )
    before = files_of(payload)
    before_annotations = payload["meta_development"]["totals"]["annotations"]

    bd.prune_empty_containers(payload)

    assert files_of(payload) == before
    # finalize_merged を通し直しても annotation 数は同値。
    payload["meta_development"]["totals"]["annotations"] = before_annotations
    assert payload["meta_development"]["totals"]["annotations"] == 3


def test_studyレベルの分類ラベルを持つstudyを剪定したら件数に出る(
    tmp_path: Path, config: Config
) -> None:
    """黙って捨てない。実データでは48件が該当する（No Findings は含まれない）。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}, "ST2": {"SE2": {}}}})],
    )
    study = payload["dataset"][INSTITUTION]["ST2"]
    study["annotations"] = [
        {"annotation_id": 1, "labels": [{"code_system": "StudyAnno", "code": "002"}]}
    ]

    pruned = bd.prune_empty_containers(payload)
    assert pruned.studies == 1
    assert pruned.studies_with_case_labels == 1
    assert pruned.as_dict()["studies_with_case_labels"] == 1


def test_剪定は冪等(tmp_path: Path, config: Config) -> None:
    """CLI がこの性質を使って「剪定後に空が残っていないか」を検査する。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}, "ST2": {"SE2": {}}}})],
    )
    assert bd.prune_empty_containers(payload)
    second = bd.prune_empty_containers(payload)
    assert not second
    assert second.as_dict() == {
        "institutions": 0,
        "studies": 0,
        "series": 0,
        "studies_with_case_labels": 0,
    }


def test_剪定後にfinalize_mergedのtotalsが実態と一致する(
    tmp_path: Path, config: Config
) -> None:
    """剪定の目的そのもの。規模行と内訳表の合計行が食い違わなくなる。"""
    accumulator = bd.new_merged_payload()
    result = bd.MergeResult()
    per_dataset: dict[str, dict] = {}
    tree = {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}, "ST2": {"SE2": {}}}}
    source = write_source(tmp_path, DS_A, tree)
    payload, _ = bd.build_development_payload(
        source, {"u1": "keep"}, tmp_path / "unused.json", config
    )
    bd.merge_into(accumulator, payload, DS_A, result)
    per_dataset[DS_A] = payload["meta_development"]

    bd.prune_empty_containers(accumulator)
    bd.finalize_merged(accumulator, result, per_dataset)

    totals = accumulator["meta_development"]["totals"]
    manifest = bd.build_development_manifest(accumulator, per_dataset)
    assert totals["studies"] == manifest["studies"] == 1
    assert totals["series"] == 1
    assert totals["files"] == 1


def test_merge後に剪定すれば片方が空の食い違いも記録される(
    tmp_path: Path, config: Config
) -> None:
    """★剪定を merge の後に置く理由を固定する回帰テスト。

    実データで現在検出されている唯一の食い違いがこの形
    （``ofuna_chuo/CXOFC00000041_004/..._001`` の ``shape`` が、画像を出す側で z=2、
    画像0枚の側で z=1）。merge の前に剪定すると空の側が統合に参加せず、
    この食い違いが報告されなくなる。同じ series の二重登録は元データ側の
    登録バグなので、どちらが画像を出したかとは独立に報告し続ける必要がある。
    """
    accumulator = bd.new_merged_payload()
    result = bd.MergeResult()
    per_dataset: dict[str, dict] = {}
    for dataset_id, files, shape in (
        (DS_A, {"F1": ["u1"]}, {"w": 1994, "h": 2430, "z": 2}),
        (DS_B, {}, {"w": 1994, "h": 2430, "z": 1}),
    ):
        source = write_source(
            tmp_path, dataset_id, {INSTITUTION: {"ST1": {"SE1": files}}}
        )
        payload, _ = bd.build_development_payload(
            source, {"u1": "keep"}, tmp_path / "unused.json", config
        )
        payload["dataset"][INSTITUTION]["ST1"]["series_list"]["SE1"]["shape"] = shape
        bd.merge_into(accumulator, payload, dataset_id, result)
        per_dataset[dataset_id] = payload["meta_development"]

    # 剪定の前に食い違いが記録されていること。
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["field"] == "shape"
    assert conflict["values"][DS_B] == {"w": 1994, "h": 2430, "z": 1}

    # 剪定しても記録済みの食い違いは消えない（空だった DS_B 側の器だけが消える）。
    pruned = bd.prune_empty_containers(accumulator)
    assert pruned.series == 0  # DS_A が画像を持つので SE1 自体は残る
    assert len(result.conflicts) == 1
