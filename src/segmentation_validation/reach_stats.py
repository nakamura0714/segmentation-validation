"""``dataset/source/`` 配下の全JSONから、データセット概況シート
（``output/research/data/reach_9_9.csv``）の空欄セルを埋めるスクリプト。

新しいデータセットJSONが ``dataset/source/`` に追加されるたびに実行し直せるように
してある（対象JSONは ``config.dataset_sources()`` の既定globが自動で拾うため、
コード変更は不要）。

この集計はあくまで **現状把握** が目的で、「未アノテーション画像を開発データとして
どう扱うか」の判断は行わない。「正常」は JSON 上に明示的な陰性ラベルがある画像
（``checks.base.ImageClass.NEGATIVE_CASE``）だけを数え、annotation が無くそれ以外の
画像は理由を問わず全て「未アノテーション」に計上する
（``annotationが無い＝正常`` という推定はしない）。

気胸の判定は ``code_text_eng == "pneumothorax"`` で行う。データセットによって
``(code_system, code)`` が異なる（例: ``Findings/010`` と ``Findings/001``。
``ETR_ChestMetry_PI6px_pneumothorax_add`` は同一JSON内に両方混在）ため、
固定の code では判定できない。

マスクカバー率（肺野/胸郭）は ``image_path`` から導いた
``category/institution/date/file_id`` で
``/mnt/medicaldb/processed/{lung,thorax}-mask/`` を引く。``<date>`` ディレクトリ名は
実際のマスク側と字面一致しないことがある（``20220112_normal``/``_abnormal`` のような
接尾辞違い）ため、施設ディレクトリ直下を1階層 ``os.listdir`` して候補を探す
（``core/paths.py::reference_mask_path`` の厳密一致とは別物。全件 ``os.walk`` は
``ofuna_chuo`` 等で数十万ファイルあり非現実的なため、listdir だけをキャッシュする）。

使い方::

    uv run python -m segmentation_validation.reach_stats          # dry-run
    uv run python -m segmentation_validation.reach_stats --write  # reach_9_9.csv 更新
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .adapters.engineer_set import open_adapters
from .checks.base import ImageClass, classify_image
from .config import Config, load_config
from .core.records import FileGroup

logger = logging.getLogger(__name__)

DEFAULT_CSV = Path("output/research/data/reach_9_9.csv")
DEFAULT_DETAIL_CSV = Path("output/research/data/reach-stats-detail_v2.2_2026-09-09.csv")

DETAIL_CSV_COLUMNS = [
    "dataset_id",
    "source_json",
    "dicom_count",
    "pneumothorax_files",
    "pneumothorax_annotations",
    "negative_images",
    "unannotated_view_images",
    "unannotated_orphan_images",
    "lung_mask_hits",
    "thorax_mask_hits",
    "lung_coverage_pct",
    "thorax_coverage_pct",
]


@dataclass
class DatasetStats:
    """1データセットJSON分の集計結果。"""

    dataset_id: str
    source_json: str
    dicom_count: int = 0
    pneumothorax_files: int = 0
    pneumothorax_annotations: int = 0
    negative_images: int = 0
    unannotated_view_images: int = 0
    unannotated_orphan_images: int = 0
    lung_mask_hits: int = 0
    thorax_mask_hits: int = 0

    @property
    def unannotated_images(self) -> int:
        """annotationが無く、正常を示すラベルも無い画像の総数。

        「未アノテーションだから正常」という推定はしないので、単純に
        ``UNANNOTATED_VIEW + UNANNOTATED_ORPHAN`` の合計になる。
        """
        return self.unannotated_view_images + self.unannotated_orphan_images

    def _coverage_cell(self, hits: int) -> str:
        if self.dicom_count == 0:
            return ""
        pct = 100 * hits / self.dicom_count
        return f"{hits}/{self.dicom_count} ({pct:.1f}%)"

    def _coverage_pct(self, hits: int) -> str:
        if self.dicom_count == 0:
            return ""
        return f"{100 * hits / self.dicom_count:.1f}"

    @property
    def lung_coverage_cell(self) -> str:
        return self._coverage_cell(self.lung_mask_hits)

    @property
    def thorax_coverage_cell(self) -> str:
        return self._coverage_cell(self.thorax_mask_hits)

    @property
    def lung_coverage_pct(self) -> str:
        return self._coverage_pct(self.lung_mask_hits)

    @property
    def thorax_coverage_pct(self) -> str:
        return self._coverage_pct(self.thorax_mask_hits)


class ReferenceMaskIndex:
    """参照マスク（lung/thorax）が実際にどの日付ディレクトリにあるかを調べる。

    ``core/paths.py::reference_mask_path`` は ``image_path`` の日付ディレクトリ名を
    そのまま使う厳密一致で、本番の検証チェック（M01等）はこれで正しい
    （「無ければ ``cannot_determine``」という設計なので、字面が違えば検出漏れではなく
    仕様通りの「不明」になる）。一方この集計は「実際にどれだけ整備されているか」を
    知りたいので、findings.md §8 が指摘する日付接尾辞違い（``20220112_normal`` 等）も
    拾えるよう、施設ディレクトリ直下を1階層 ``os.listdir`` して候補を探す方式にする。
    """

    def __init__(self, root: str, drop_components: list[int]):
        self.root = root
        self.drop = set(drop_components)
        self._listdir_cache: dict[str, list[str]] = {}

    def _date_dir_candidates(
        self, category: str, institution: str, date: str
    ) -> list[str]:
        inst_dir = os.path.join(self.root, category, institution)
        if inst_dir not in self._listdir_cache:
            try:
                self._listdir_cache[inst_dir] = os.listdir(inst_dir)
            except OSError:
                self._listdir_cache[inst_dir] = []
        return [
            name
            for name in self._listdir_cache[inst_dir]
            if name == date or name.startswith(date + "_")
        ]

    def has_mask(self, image_path_raw: str) -> bool:
        parts = image_path_raw.lstrip("/").split("/")
        kept = [part for index, part in enumerate(parts) if index not in self.drop]
        if len(kept) != 4:
            return False
        category, institution, date, filename = kept
        file_id = filename.rsplit(".", 1)[0]
        direct = os.path.join(self.root, category, institution, date, f"{file_id}.png")
        if os.path.exists(direct):
            return True
        for candidate in self._date_dir_candidates(category, institution, date):
            if candidate == date:
                continue
            path = os.path.join(
                self.root, category, institution, candidate, f"{file_id}.png"
            )
            if os.path.exists(path):
                return True
        return False


def compute_stats(config: Config) -> list[DatasetStats]:
    adapters = open_adapters(config)

    groups_by_dataset: dict[str, list[FileGroup]] = {}
    all_groups: list[FileGroup] = []
    for adapter in adapters:
        groups = list(adapter.iter_files())
        groups_by_dataset[adapter.dataset_id] = groups
        all_groups.extend(groups)

    # 「同じseriesの他画像はannotation済みか」はデータセットを跨いでも
    # dataset_idをキーに含めるので混ざらない（M09と同じ計算方法）。
    annotated_series = {
        (g.dataset_id, g.study, g.series) for g in all_groups if g.records
    }

    lung_index = ReferenceMaskIndex(
        config.reference_masks.roots["lung"],
        config.reference_masks.drop_path_components,
    )
    thorax_index = ReferenceMaskIndex(
        config.reference_masks.roots["thorax"],
        config.reference_masks.drop_path_components,
    )

    stats: list[DatasetStats] = []
    for adapter in adapters:
        groups = groups_by_dataset[adapter.dataset_id]
        s = DatasetStats(
            dataset_id=adapter.dataset_id, source_json=adapter.source_path.name
        )
        s.dicom_count = len(groups)
        for group in groups:
            pneumo_annos = sum(
                1
                for record in group.records
                for label in record.labels
                if label.code_text_eng == "pneumothorax"
            )
            if pneumo_annos:
                s.pneumothorax_files += 1
                s.pneumothorax_annotations += pneumo_annos

            key = (group.dataset_id, group.study, group.series)
            image_class = classify_image(group, key in annotated_series)
            if image_class is ImageClass.NEGATIVE_CASE:
                s.negative_images += 1
            elif image_class is ImageClass.UNANNOTATED_VIEW:
                s.unannotated_view_images += 1
            elif image_class is ImageClass.UNANNOTATED_ORPHAN:
                s.unannotated_orphan_images += 1

            if lung_index.has_mask(group.image_path):
                s.lung_mask_hits += 1
            if thorax_index.has_mask(group.image_path):
                s.thorax_mask_hits += 1
        stats.append(s)
    return stats


LEGACY_UNANNOTATED_COLUMN = "未アノテーション"
# checks/base.py::IMAGE_CLASS_JA と同じ表記に揃える。
UNANNOTATED_VIEW_COLUMN = "未アノテーション（他ビューは済み）"
UNANNOTATED_ORPHAN_COLUMN = "未アノテーション（series全体）"

COLUMN_GETTERS = {
    "気胸件数/DICOM件数": lambda s: f"{s.pneumothorax_files}/{s.dicom_count}",
    "気胸のアノテーション数": lambda s: str(s.pneumothorax_annotations),
    "正常": lambda s: str(s.negative_images),
    UNANNOTATED_VIEW_COLUMN: lambda s: str(s.unannotated_view_images),
    UNANNOTATED_ORPHAN_COLUMN: lambda s: str(s.unannotated_orphan_images),
    "肺野マスクカバー率": lambda s: s.lung_coverage_cell,
    "胸郭マスクカバー率": lambda s: s.thorax_coverage_cell,
}


def _migrate_unannotated_column(
    header: list[str],
    data_rows: list[list[str]],
    stats_by_json: dict[str, "DatasetStats"],
) -> None:
    """旧「未アノテーション」1列を「他ビュー済み」/「series全体」の2列に分割する。

    「２枚目でアノテーションが付いていないのか、そもそも1枚も無いのか」を区別
    したいという要望を受けての1回限りの移行。分割後の値は（元の手書き注記が
    あっても）JSONから再計算した値で置き換える —— 1列に畳んだ時点で情報が
    失われているものを、注記から推測して埋め直すことはしない。
    """
    if LEGACY_UNANNOTATED_COLUMN not in header or UNANNOTATED_VIEW_COLUMN in header:
        return
    legacy_idx = header.index(LEGACY_UNANNOTATED_COLUMN)
    json_idx = header.index("jsonファイル")
    for row in data_rows:
        s = stats_by_json.get(Path(row[json_idx].strip()).name)
        view = str(s.unannotated_view_images) if s else ""
        orphan = str(s.unannotated_orphan_images) if s else row[legacy_idx]
        row[legacy_idx : legacy_idx + 1] = [view, orphan]
    header[legacy_idx : legacy_idx + 1] = [
        UNANNOTATED_VIEW_COLUMN,
        UNANNOTATED_ORPHAN_COLUMN,
    ]
    logger.info(
        "「%s」列を「%s」「%s」の2列に分割した",
        LEGACY_UNANNOTATED_COLUMN,
        UNANNOTATED_VIEW_COLUMN,
        UNANNOTATED_ORPHAN_COLUMN,
    )


def update_reach_csv(csv_path: Path, stats: list[DatasetStats], write: bool) -> None:
    """``reach_9_9.csv`` の空欄セルだけを埋める。

    既に値が入っているセル（手書きの注記を含む）は上書きしない。計算値と食い違う
    場合は警告だけ出す（今後別データセットでラベル語彙が変わった場合などの検知用）。
    """
    stats_by_json = {s.source_json: s for s in stats}

    with csv_path.open("r", encoding="cp932", newline="") as f:
        rows = list(csv.reader(f))
    header, data_rows = rows[0], rows[1:]
    _migrate_unannotated_column(header, data_rows, stats_by_json)
    col_index = {name: index for index, name in enumerate(header)}

    filled = 0
    for row in data_rows:
        dataset_label = row[col_index["データセット"]]
        json_cell = row[col_index["jsonファイル"]].strip()
        s = stats_by_json.get(Path(json_cell).name)
        if s is None:
            logger.warning(
                "対応するJSONが見つからない: %s (%s)", json_cell, dataset_label
            )
            continue

        for column, getter in COLUMN_GETTERS.items():
            idx = col_index[column]
            existing = row[idx].strip()
            computed = getter(s)
            if not existing:
                row[idx] = computed
                filled += 1
            elif existing != computed:
                logger.warning(
                    "%s / %s: 既存値 %r と計算値 %r が食い違う（上書きしない）",
                    dataset_label,
                    column,
                    existing,
                    computed,
                )

    logger.info("%d セルを埋めた", filled)
    if write:
        with csv_path.open("w", encoding="cp932", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(data_rows)
        logger.info("書き込み: %s", csv_path)
    else:
        logger.info("dry-run のため書き込みなし（--write で反映）")


def write_detail_csv(path: Path, stats: list[DatasetStats]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(DETAIL_CSV_COLUMNS)
        for s in stats:
            writer.writerow(
                [
                    s.dataset_id,
                    s.source_json,
                    s.dicom_count,
                    s.pneumothorax_files,
                    s.pneumothorax_annotations,
                    s.negative_images,
                    s.unannotated_view_images,
                    s.unannotated_orphan_images,
                    s.lung_mask_hits,
                    s.thorax_mask_hits,
                    s.lung_coverage_pct,
                    s.thorax_coverage_pct,
                ]
            )
    logger.info("detail CSV -> %s", path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="設定JSON")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="更新対象のCSV")
    parser.add_argument(
        "--detail-csv", type=Path, default=DEFAULT_DETAIL_CSV, help="詳細内訳の出力先"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="指定した場合のみ実際にCSVを書き換える（既定はdry-run）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    config = load_config(args.config)

    stats = compute_stats(config)
    for s in stats:
        logger.info(
            "%s: DICOM=%d 気胸=%d/%d annotation=%d 正常=%d "
            "未アノテーション=%d(view=%d/orphan=%d) 肺野=%s 胸郭=%s",
            s.dataset_id,
            s.dicom_count,
            s.pneumothorax_files,
            s.dicom_count,
            s.pneumothorax_annotations,
            s.negative_images,
            s.unannotated_images,
            s.unannotated_view_images,
            s.unannotated_orphan_images,
            s.lung_coverage_cell,
            s.thorax_coverage_cell,
        )

    csv_path = config.resolve(str(args.csv))
    update_reach_csv(csv_path, stats, args.write)

    if args.write:
        detail_path = config.resolve(str(args.detail_csv))
        write_detail_csv(detail_path, stats)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
