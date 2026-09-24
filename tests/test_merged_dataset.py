"""12本の development.json を1本へ統合する処理を守る。

**浅い ``dict.update()`` では壊れる。** 実データでは上位階層が複数データセットで
共有されている。

    institution                       35 中 23 が跨る
    (institution, study)          42,690 中 4,274 が跨る
    (inst, study, series)         42,708 中 4,277 が跨る
    (inst, study, series, file)   42,574 中 **0**（衝突なし）

衝突が無いのは file 単位だけなので、統合は institution → study → series →
file_list を再帰的に union し、``dataset_id`` は file entry に持たせる。
上位階層に持たせると由来（train/test の別）を表現できない。

属性の食い違い（実データでは ``segmed/CXSGM00040183`` の ``study_date`` 1件のみ）は
**値を選ばず両方を記録する**。どちらが正かは元データ側の問題で、ツールが決めて
よいことではない。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import INSTITUTION

from segmentation_validation.config import Config
from segmentation_validation.selection import build_dataset as bd

DS_A = "DS_A"
DS_B = "DS_B"


def write_source(
    tmp_path: Path,
    dataset_id: str,
    tree: dict,
    version_id: str = "2.2",
    version_date: str = "2026-05-19",
    labels: dict[str, list[str]] | None = None,
) -> Path:
    """engineer-set 形式の最小の元JSONを書く。

    ``tree`` は ``{institution: {study: {series: {file: [geometry_uid, ...]}}}}``。
    ファイル名は実データと同じ ``engineer-set-<id>-<日時>.json`` にする
    （``dataset_id_for`` が日時を落とすことに依存しているため）。

    ``labels`` は ``geometry_uid -> ["Findings/010", ...]``。指定の無い uid には
    ``labels`` キーを**付けない**（実データにもラベル無しの annotation がある）。
    """
    dataset: dict = {}
    for institution, studies in tree.items():
        dataset[institution] = {}
        for study_key, series_map in studies.items():
            dataset[institution][study_key] = {
                "patient_id": study_key.split("_")[0],
                "study_name": study_key,
                "study_date": "2026-01-01",
                "series_list": {
                    series_key: {
                        "spacing": {"x": 0.15, "y": 0.15, "z": 1.0},
                        "shape": {"w": 2000, "h": 2400, "z": 1},
                        "manufacturer": "SYNTH",
                        "file_list": {
                            file_key: {
                                "image_path": f"medical2/synth/{file_key}.dcm",
                                "annotations": [
                                    _annotation(uid, labels) for uid in uids
                                ],
                            }
                            for file_key, uids in files.items()
                        },
                        "groups": [],
                    }
                    for series_key, files in series_map.items()
                },
            }
    path = tmp_path / f"engineer-set-{dataset_id}-20260624_080014.json"
    path.write_text(
        json.dumps(
            {
                "dataset": dataset,
                "version_id": version_id,
                "version_date": version_date,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _annotation(uid: str, labels: dict[str, list[str]] | None) -> dict:
    record: dict = {"geometry_uid": uid, "annotation_type": "brush"}
    qualified = (labels or {}).get(uid)
    if qualified:
        record["labels"] = [
            {
                "label_id": 1,
                "code_system": code.split("/")[0],
                "code": code.split("/")[1],
                "code_text": code,
                "code_text_eng": code,
                "confidence": 90.0,
            }
            for code in qualified
        ]
    return record


def merge_all(
    tmp_path: Path, config: Config, sources: list[tuple[str, dict]], **kwargs
) -> tuple[dict, bd.MergeResult]:
    """``sources`` を順に統合して payload と結果を返す。"""
    accumulator = bd.new_merged_payload()
    result = bd.MergeResult()
    per_dataset: dict[str, dict] = {}
    for dataset_id, tree in sources:
        path = write_source(tmp_path, dataset_id, tree, **kwargs)
        keep_all = {
            uid: "keep"
            for studies in tree.values()
            for series_map in studies.values()
            for files in series_map.values()
            for uids in files.values()
            for uid in uids
        }
        payload, build = bd.build_development_payload(
            path, keep_all, tmp_path / "unused.json", config
        )
        bd.merge_into(accumulator, payload, dataset_id, result)
        per_dataset[dataset_id] = payload["meta_development"]
    return bd.finalize_merged(accumulator, result, per_dataset), result


def files_of(payload: dict) -> dict[str, str]:
    """``inst/study/series/file -> dataset_id``。"""
    found = {}
    for institution, studies in payload["dataset"].items():
        for study_key, study in studies.items():
            for series_key, series in (study.get("series_list") or {}).items():
                for file_key, rec in (series.get("file_list") or {}).items():
                    key = f"{institution}/{study_key}/{series_key}/{file_key}"
                    found[key] = rec[bd.DATASET_ID_KEY]
    return found


# ------------------------------------------------------------------- 再帰union


def test_異なるinstitutionはそのまま結合される(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {"inst_a": {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u2"]}}}}),
        ],
    )
    assert set(payload["dataset"]) == {"inst_a", "inst_b"}
    assert files_of(payload) == {
        "inst_a/ST1/SE1/F1": DS_A,
        "inst_b/ST2/SE2/F2": DS_B,
    }


def test_同じstudyを持つ2データセットのseriesがunionされる(
    tmp_path: Path, config: Config
) -> None:
    """浅い update だと後勝ちで片方の series が消える。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE_A": {"F1": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE_B": {"F2": ["u2"]}}}}),
        ],
    )
    series = payload["dataset"][INSTITUTION]["ST1"]["series_list"]
    assert set(series) == {"SE_A", "SE_B"}


def test_同じseriesを持つ2データセットのfile_listがunionされる(
    tmp_path: Path, config: Config
) -> None:
    """実データの ``ofuna_chuo/CXOFC00003778_002/..._001`` の縮小再現。

    同じ series の下で、データセットごとに別の file_id を持つ。浅い merge だと
    片方の file を丸ごと落とす。
    """
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F_000": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F_002": ["u2"]}}}}),
        ],
    )
    assert files_of(payload) == {
        f"{INSTITUTION}/ST1/SE1/F_000": DS_A,
        f"{INSTITUTION}/ST1/SE1/F_002": DS_B,
    }


def test_片方が空のfile_listでも取りこぼさない(tmp_path: Path, config: Config) -> None:
    """実データでは共有 series 4,277件のうち 4,276件がこの形。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
        ],
    )
    assert files_of(payload) == {f"{INSTITUTION}/ST1/SE1/F1": DS_B}


def test_annotationが0件のfile_entryも残る(tmp_path: Path, config: Config) -> None:
    """母集団を黙って変えない、という per-dataset 側の不変条件を統合でも保つ。"""
    payload, result = merge_all(
        tmp_path, config, [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": []}}}})]
    )
    assert files_of(payload) == {f"{INSTITUTION}/ST1/SE1/F1": DS_A}
    assert result.annotations == 0


# ------------------------------------------------------------------- dataset_id


def test_file_entryにdataset_idが付く(tmp_path: Path, config: Config) -> None:
    """★これが無いと train/test の由来が失われる（PTE/PTR 問題の再発）。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F2": ["u2"]}}}}),
        ],
    )
    assert set(files_of(payload).values()) == {DS_A, DS_B}


def test_dataset_idはseries階層には付かない(tmp_path: Path, config: Config) -> None:
    """series は複数データセットで共有されるので、そこに書くと嘘になる。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F2": ["u2"]}}}}),
        ],
    )
    series = payload["dataset"][INSTITUTION]["ST1"]["series_list"]["SE1"]
    assert bd.DATASET_ID_KEY not in series
    assert bd.DATASET_ID_KEY not in payload["dataset"][INSTITUTION]["ST1"]


# --------------------------------------------------------------------- 衝突


def test_file_id衝突は例外で止まる(tmp_path: Path, config: Config) -> None:
    """衝突ゼロが前提。黙って上書きすると片方の annotation が消える。"""
    with pytest.raises(bd.MergeCollision) as excinfo:
        merge_all(
            tmp_path,
            config,
            [
                (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
                (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u2"]}}}}),
            ],
        )
    assert "F1" in str(excinfo.value)


def test_study_dateの食い違いが両方記録される(tmp_path: Path, config: Config) -> None:
    """実データの ``segmed/CXSGM00040183`` の縮小再現。

    2020-01-11（normal）と 2019-01-05（abnormal_non_pneumothorax）。
    ツールはどちらが正か決めない。両方を残して人へ回す。
    """
    accumulator = bd.new_merged_payload()
    result = bd.MergeResult()
    for dataset_id, date in ((DS_A, "2020-01-11"), (DS_B, "2019-01-05")):
        path = write_source(
            tmp_path, dataset_id, {INSTITUTION: {"ST1": {f"SE_{dataset_id}": {}}}}
        )
        payload, _ = bd.build_development_payload(
            path, {}, tmp_path / "unused.json", config
        )
        payload["dataset"][INSTITUTION]["ST1"]["study_date"] = date
        bd.merge_into(accumulator, payload, dataset_id, result)

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict["level"] == "study"
    assert conflict["field"] == "study_date"
    assert conflict["values"] == {DS_A: "2020-01-11", DS_B: "2019-01-05"}
    # 採用値は決定的（dataset_id 昇順の先頭）。
    assert conflict["adopted"] == DS_A
    assert accumulator["dataset"][INSTITUTION]["ST1"]["study_date"] == "2020-01-11"


def test_series属性が一致していれば衝突として記録しない(
    tmp_path: Path, config: Config
) -> None:
    """実データでは共有 series 4,277件すべてでメタが一致する。誤検出しないこと。"""
    _, result = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F2": ["u2"]}}}}),
        ],
    )
    assert result.conflicts == []


# ----------------------------------------------------------------------- meta


def test_version_idはトップレベルに1つだけ残る(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u2"]}}}}),
        ],
    )
    assert payload["version_id"] == "2.2"
    assert payload["version_date"] == "2026-05-19"


def test_meta_developmentにデータセット別の内訳が入る(
    tmp_path: Path, config: Config
) -> None:
    """統合JSONから元へ戻れるように、source_sha256 を含めて残す。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u2", "u3"]}}}}),
        ],
    )
    meta = payload["meta_development"]
    assert meta["merged"] is True
    assert set(meta["datasets"]) == {DS_A, DS_B}
    for entry in meta["datasets"].values():
        assert entry["source_sha256"]
        assert entry["source_json"].startswith("engineer-set-")
    assert meta["totals"] == {
        "datasets": 2,
        "institutions": 2,
        "studies": 2,
        "series": 2,
        "files": 2,
        "annotations": 3,
    }


def test_統合後のannotation総数が各データセットのkeepの合計と一致する(
    tmp_path: Path, config: Config
) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1", "u2"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u3"]}}}}),
        ],
    )
    meta = payload["meta_development"]
    assert meta["totals"]["annotations"] == sum(
        entry["kept"] for entry in meta["datasets"].values()
    )


def test_書き出したJSONを読み戻せる(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path, config, [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}})]
    )
    out = bd.write_merged_json(payload, tmp_path / "development_merged.json")
    assert json.loads(out.read_text(encoding="utf-8")) == payload


# ------------------------------------------------------- 中身の明細と内訳


def manifest_of(payload: dict, collect: bool = True) -> tuple[dict, list[dict]]:
    """``build_development_manifest`` を per_dataset 付きで呼ぶ。

    ``per_dataset`` は ``finalize_merged`` が ``meta_development.datasets`` に
    そのまま入れているので、そこから取れる（``merge_all`` の戻り値を変えない）。
    """
    rows: list[dict] = []
    summary = bd.build_development_manifest(
        payload,
        payload["meta_development"]["datasets"],
        rows.append if collect else None,
    )
    return summary, rows


def test_明細の行数が統合JSONの画像数と一致する(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"], "F2": ["u2"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F3": ["u3"]}}}}),
        ],
    )
    _, rows = manifest_of(payload)
    assert len(rows) == payload["meta_development"]["totals"]["files"] == 3
    assert set(rows[0]) == set(bd.DEVELOPMENT_MANIFEST_COLUMNS)


def test_明細行のfile_uidがimage_decisionsと同じ書式になる(
    tmp_path: Path, config: Config
) -> None:
    """突合できることが明細CSVの価値の中心。書式をここで固定する。"""
    payload, _ = merge_all(
        tmp_path, config, [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}})]
    )
    source_json = payload["meta_development"]["datasets"][DS_A]["source_json"]
    _, rows = manifest_of(payload)
    assert rows[0]["file_uid"] == f"{source_json}::{INSTITUTION}/ST1/SE1/F1"
    assert rows[0]["dataset_id"] == DS_A
    assert rows[0]["patient_id"] == "ST1"
    assert rows[0]["image_path"] == "medical2/synth/F1.dcm"


def test_病変クラスが列挙される(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1", "u2"], "F2": ["u3"]}}}})],
        labels={"u1": ["Findings/001"], "u2": ["Findings/010", "Findings/001"]},
    )
    summary, rows = manifest_of(payload)
    by_file = {row["file_id"]: row for row in rows}
    assert by_file["F1"]["lesion_classes"] == "Findings/001|Findings/010"
    # ラベルの無い annotation は空欄。「不明」と書いて水増ししない。
    assert by_file["F2"]["lesion_classes"] == ""
    # 件数降順→コード昇順。u1 と u2 が Findings/001、u2 だけが Findings/010。
    assert summary["datasets"][DS_A]["classes"] == {
        "Findings/001": 2,
        "Findings/010": 1,
    }


def test_annotationが0件の画像も明細に出る(tmp_path: Path, config: Config) -> None:
    """除外の結果0件になった画像を明細から消すと、母集団が黙って変わる。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": [], "F2": ["u1"]}}}})],
    )
    summary, rows = manifest_of(payload)
    empty = next(row for row in rows if row["file_id"] == "F1")
    assert empty["n_annotations"] == 0
    assert empty["lesion_classes"] == ""
    assert summary["datasets"][DS_A]["files"] == 2
    assert summary["datasets"][DS_A]["files_without_annotations"] == 1


def test_データセット別の画像とannotationの合計が統合totalsと一致する(
    tmp_path: Path, config: Config
) -> None:
    """1画像は必ず1データセットに属するので、この2列だけは総和が合う。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1", "u2"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u3"]}}}}),
        ],
    )
    summary, _ = manifest_of(payload)
    totals = payload["meta_development"]["totals"]
    datasets = summary["datasets"].values()
    assert sum(entry["files"] for entry in datasets) == totals["files"]
    assert sum(entry["annotations"] for entry in datasets) == totals["annotations"]


def test_内訳のannotation数がmeta_developmentのkeptと一致する(
    tmp_path: Path, config: Config
) -> None:
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1", "u2"]}}}}),
            (DS_B, {"inst_b": {"ST2": {"SE2": {"F2": ["u3"]}}}}),
        ],
    )
    summary, _ = manifest_of(payload)
    meta = payload["meta_development"]["datasets"]
    for dataset_id, entry in summary["datasets"].items():
        assert entry["annotations"] == meta[dataset_id]["kept"]


def test_同じstudyを跨ぐと患者とstudyは両データセットで数える(
    tmp_path: Path, config: Config
) -> None:
    """二重計上は**仕様**。各行は「そのデータセット由来の画像を含む数」。"""
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (DS_A, {INSTITUTION: {"ST1": {"SE1": {"F_000": ["u1"]}}}}),
            (DS_B, {INSTITUTION: {"ST1": {"SE1": {"F_002": ["u2"]}}}}),
        ],
    )
    summary, _ = manifest_of(payload)
    assert summary["datasets"][DS_A]["studies"] == 1
    assert summary["datasets"][DS_B]["studies"] == 1
    assert summary["datasets"][DS_A]["patients"] == 1
    assert summary["datasets"][DS_B]["patients"] == 1
    # 合計は畳んだ実数なので1。総和（2）とは一致しない。
    assert payload["meta_development"]["totals"]["studies"] == 1
    assert summary["patients"] == 1
    assert summary["studies"] == 1


def test_画像を1枚も持たないstudyは合計に数えない(
    tmp_path: Path, config: Config
) -> None:
    """合計を「全study」で数えると、**合計 < 各行の総和**という読めない表になる。

    実データの統合JSONには画像を1枚も持たない study が170件ある（空の
    ``file_list`` は共有 series でありふれた形）。データセット別の行は file entry
    からしか数えられないので、合計も同じ基準に揃える。
    """
    payload, _ = merge_all(
        tmp_path,
        config,
        [
            (
                DS_A,
                {
                    INSTITUTION: {
                        "ST1": {"SE1": {"F1": ["u1"]}},
                        "ST2": {"SE2": {}},  # 画像を1枚も持たない
                    }
                },
            )
        ],
    )
    summary, rows = manifest_of(payload)
    # 構造上の study 数は2だが、画像を持つのは1つだけ。
    assert payload["meta_development"]["totals"]["studies"] == 2
    assert summary["studies"] == 1
    assert summary["patients"] == 1
    assert summary["studies"] >= summary["datasets"][DS_A]["studies"]
    assert {row["study_key"] for row in rows} == {"ST1"}


def test_row_sinkを渡さなくても集計だけ取れる(tmp_path: Path, config: Config) -> None:
    payload, _ = merge_all(
        tmp_path, config, [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"]}}}})]
    )
    summary, rows = manifest_of(payload, collect=False)
    assert rows == []
    assert summary["datasets"][DS_A]["files"] == 1


def test_明細CSVのヘッダが列定数と一致する(tmp_path: Path, config: Config) -> None:
    """CLI側の書き出し。列定数とCSVがずれると突合スクリプトが黙って壊れる。"""
    import csv

    from segmentation_validation.cli import _write_development_manifest

    payload, _ = merge_all(
        tmp_path,
        config,
        [(DS_A, {INSTITUTION: {"ST1": {"SE1": {"F1": ["u1"], "F2": []}}}})],
    )
    out = tmp_path / "development_manifest.csv"
    summary = _write_development_manifest(
        out, payload, payload["meta_development"]["datasets"]
    )
    with out.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == bd.DEVELOPMENT_MANIFEST_COLUMNS
        rows = list(reader)
    assert len(rows) == payload["meta_development"]["totals"]["files"] == 2
    assert summary["datasets"][DS_A]["files"] == 2
