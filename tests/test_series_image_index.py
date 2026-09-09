"""``series_image_index`` / ``series_image_count`` の実データ検証。

事前調査（development-json-partitioned-metcalfe.md 参照）で、複数ファイルを持つ
series は全47,036件中39件のみで、この39件全てにおいて ``file_key`` の末尾連番を
昇順ソートした順序と DICOM の ``InstanceNumber`` の順序が完全一致することを
確認済み（アノテーションがある方が常に先頭）。ここではそのうち数件を使って、
アダプタが実際に正しい ``series_image_index``/``series_image_count`` を
計算することを固定化する。

``uv run pytest -m realdata`` で走る（既定の ``uv run pytest`` からは除外）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from segmentation_validation.adapters.engineer_set import EngineerSetAdapter
from segmentation_validation.config import Config

pytestmark = pytest.mark.realdata

SOURCE = Path(
    "/mnt/project/chest/metry/pi6/dataset/source/"
    "engineer-set-ANN_EIRLPRJ_01272-20260709_004526.json"
)

# (institution, series_key) -> 期待する (annotationあり側のfile_key, 無し側のfile_key)。
# 事前調査で DICOM InstanceNumber 昇順と一致することを確認済みの3件。
KNOWN_TWO_FILE_SERIES = {
    ("asahikawa", "CXASW00001546_000_000"): (
        "CXASW00001546_000_000_000",
        "CXASW00001546_000_000_001",
    ),
    ("okayama_chuo", "CXOKC00001821_001_000"): (
        "CXOKC00001821_001_000_000",
        "CXOKC00001821_001_000_001",
    ),
    ("nagoya_daiichi", "LTNGF00001288_000_000"): (
        "LTNGF00001288_000_000_000",
        "LTNGF00001288_000_000_001",
    ),
}


@pytest.fixture(scope="module")
def groups_by_series():
    if not SOURCE.exists():
        pytest.skip(f"実データが無い: {SOURCE}")
    adapter = EngineerSetAdapter(source_path=SOURCE, config=Config())
    by_series: dict[tuple[str, str], dict[str, object]] = {}
    for group in adapter.iter_files():
        key = (group.institution, group.series)
        if key in KNOWN_TWO_FILE_SERIES:
            by_series.setdefault(key, {})[group.file] = group
    return by_series


@pytest.mark.parametrize("key", list(KNOWN_TWO_FILE_SERIES))
def test_2枚series内の位置が正しく計算される(groups_by_series, key):
    annotated_file, unannotated_file = KNOWN_TWO_FILE_SERIES[key]
    files = groups_by_series[key]
    assert set(files) == {annotated_file, unannotated_file}

    annotated_group = files[annotated_file]
    unannotated_group = files[unannotated_file]

    # series_image_count はどちらのfileから見ても同じ（series全体の総数）。
    assert annotated_group.series_image_count == 2
    assert unannotated_group.series_image_count == 2

    # アノテーションがある方（file_keyの末尾連番が小さい方）が index=0、
    # 無い方が index=1。DICOMのInstanceNumber順と一致することは事前調査で確認済み。
    assert annotated_group.series_image_index == 0
    assert unannotated_group.series_image_index == 1
    assert len(annotated_group.records) > 0
    assert len(unannotated_group.records) == 0


def test_1枚だけのseriesはindex0count1になる(groups_by_series):
    """複数ファイルseriesは全体の中でも僅少（39/47,036）。大多数はこちら。"""
    adapter = EngineerSetAdapter(source_path=SOURCE, config=Config())
    groups = list(adapter.iter_files())
    single_file_series = {(g.institution, g.series) for g in groups} - set(
        KNOWN_TWO_FILE_SERIES
    )
    assert single_file_series  # このデータセットにも1枚だけのseriesがあるはず
    sample = next(g for g in groups if (g.institution, g.series) in single_file_series)
    assert sample.series_image_index == 0
    assert sample.series_image_count == 1
