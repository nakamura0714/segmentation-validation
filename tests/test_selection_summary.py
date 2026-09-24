"""``selection_summary.md`` の統合節（何が入っているか）。

``_add_merged_section`` は ``(add, merged)`` だけを取る純関数なので、
``SelectionDecision`` を組み立てずに単体で叩ける。
"""

from __future__ import annotations

from segmentation_validation.report import summary_md

TOTALS = {
    "datasets": 2,
    "institutions": 3,
    "studies": 4,
    "series": 4,
    "files": 5,
    "annotations": 7,
}


def render(**extra) -> str:
    merged: dict = {
        "path": "output/development/20260917/development_merged.json",
        "totals": TOTALS,
        "conflicts": [],
    }
    merged.update(extra)
    lines: list[str] = []
    summary_md._add_merged_section(lines.append, merged)
    return "\n".join(lines)


def by_dataset() -> dict:
    return {
        "DS_A": {
            "dataset_id": "DS_A",
            "source_json": "engineer-set-DS_A-20260624_080014.json",
            "institutions": 2,
            "patients": 3,
            "studies": 3,
            "series": 3,
            "files": 3,
            "annotations": 5,
            "files_without_annotations": 1,
            "classes": {"Findings/001": 3, "Findings/010": 2},
        },
        "DS_B": {
            "dataset_id": "DS_B",
            "source_json": "engineer-set-DS_B-20260624_080014.json",
            "institutions": 1,
            "patients": 2,
            "studies": 2,
            "series": 1,
            "files": 2,
            "annotations": 2,
            "files_without_annotations": 0,
            "classes": {},
        },
    }


def test_データセット別の内訳表が出る() -> None:
    text = render(by_dataset=by_dataset(), patients=4)
    assert "### 何が入っているか（データセット別）" in text
    assert "| DS_A | `engineer-set-DS_A-20260624_080014.json` |" in text
    assert "Findings/001 3 / Findings/010 2" in text
    # ラベルが1件も無いデータセットは空欄ではなくダッシュ。
    assert "| DS_B |" in text and "| — |" in text


def test_明細CSVへの誘導が出る() -> None:
    text = render(
        by_dataset=by_dataset(),
        patients=4,
        manifest_path="output/development/20260917/development_manifest.csv",
    )
    assert "`development_manifest.csv`" in text
    assert "file_uid" in text
    assert "image_decisions.csv" in text


def test_明細パスが無ければ誘導文を出さない() -> None:
    text = render(by_dataset=by_dataset(), patients=4)
    assert "development_manifest.csv" not in text


def test_by_datasetが無い旧形式でも例外にならず節は出る() -> None:
    """``merged`` のキーは全て任意。増やしたキーに依存して壊さない。"""
    text = render()
    assert "## 統合JSON（学習パイプライン入力）" in text
    assert "何が入っているか" not in text
    # 患者数は統合payloadを走査しないと出ないので、規模行にも出さない。
    assert "患者" not in text


def test_合計行は内訳の総和ではなく畳んだ実数を使う() -> None:
    """患者 / study はデータセットを跨ぐので、総和では実数にならない。"""
    text = render(by_dataset=by_dataset(), patients=4, studies=4)
    # 内訳の総和は 3+2=5 だが、畳んだ実数は 4。
    assert "**4**" in text
    assert "**5**" in text  # 画像（こちらは総和と一致する）
    assert "足し上げても合計と一致しない" in text


def test_studyが食い違ったら剪定を疑う警告を出す() -> None:
    """剪定してあれば規模行と合計行の study は一致する。出たら実装のバグ。"""
    text = render(by_dataset=by_dataset(), patients=4, studies=3)
    assert "⚠" in text
    assert "差 1 件" in text
    assert "剪定が効いていない可能性" in text


def test_規模行は構造上の総数だけで組む() -> None:
    """患者は画像を持つものしか数えられない。基準の違う値を1行に混ぜない。"""
    text = render(by_dataset=by_dataset(), patients=4, studies=3)
    scale = next(line for line in text.splitlines() if line.startswith("- 規模:"))
    assert "患者" not in scale
    assert "study 4" in scale


def test_studyが一致すれば警告を出さない() -> None:
    """剪定が効いている既定の状態。ここが通常。"""
    text = render(by_dataset=by_dataset(), patients=4, studies=4)
    assert "⚠" not in text


def test_剪定した件数を出す() -> None:
    text = render(
        by_dataset=by_dataset(),
        patients=4,
        studies=4,
        pruned={
            "institutions": 0,
            "studies": 171,
            "series": 171,
            "studies_with_case_labels": 48,
        },
    )
    assert "画像を1枚も持たない series / study / institution は含まない" in text
    assert "series 171 / study 171 / 施設 0" in text
    assert "study 48 件" in text
    assert "画像と annotation を1件も動かさない" in text


def test_分類ラベル付きstudyを剪定しなければその注記は出さない() -> None:
    text = render(
        by_dataset=by_dataset(),
        patients=4,
        studies=4,
        pruned={
            "institutions": 0,
            "studies": 2,
            "series": 2,
            "studies_with_case_labels": 0,
        },
    )
    assert "剪定: series 2 / study 2 / 施設 0）" in text
    assert "分類ラベル" not in text


def test_prunedが無い旧形式では剪定行を出さない() -> None:
    text = render(by_dataset=by_dataset(), patients=4, studies=4)
    assert "剪定" not in text


def test_conflictsの表は従来どおり出る() -> None:
    text = render(
        conflicts=[
            {
                "level": "study",
                "path": "segmed/CXSGM00040183",
                "field": "study_date",
                "values": {"DS_A": "2026-01-01", "DS_B": "2026-01-02"},
                "adopted": "DS_A",
                "rule": "dataset_id 昇順の先頭",
            }
        ]
    )
    assert "### 元データ間の属性の食い違い（1 件）" in text
    assert "`study_date`" in text
    assert "`DS_A`=2026-01-01" in text
