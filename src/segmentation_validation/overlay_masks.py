"""1症例分のDICOMとマスクを重畳表示して目視確認するためのスクリプト。

同一領域に対する重複アノテーションの有無を確認する目的で作成した。
データセットは読み取りのみで、出力はプロジェクト内にしか書き込まない。

一括レビューは FiftyOne（``review/``）が本体で、これは1症例を図として
残したいときの debug / fallback 用途。

使い方::

    uv run python -m segmentation_validation.overlay_masks
    uv run python -m segmentation_validation.overlay_masks --institution jsrt
    uv run python -m segmentation_validation.overlay_masks --study CXASW00000278_002
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .config import Config, load_config
from .core.geometry import iou, mask_stats
from .core.imageio import MaskReadError, load_binary_mask, load_dicom_image
from .core.labels import Label, label_keys, parse_labels
from .core.paths import resolve_annotation_path, resolve_image_path
from .viz.overlay import JAPANESE_FONT, render

logger = logging.getLogger(__name__)

DEFAULT_JSON = Path(
    "/mnt/project/chest/metry/pi6/dataset/source/"
    "engineer-set-ETR_ChestMetry_PI6px_with_mask136-20260624_080014.json"
)


@dataclass(frozen=True)
class MaskEntry:
    """1件のアノテーション（brushマスク）を表す。"""

    index: int
    geometry_uid: str
    geometry_id: int
    path_mask: Path | None
    path_original_mask: Path | None
    json_bbox: tuple[int, int, int, int]
    is_latest: int
    version: int
    user: str
    timestamp: str
    labels: tuple[Label, ...]

    @property
    def short_uid(self) -> str:
        return self.geometry_uid[:8]

    @property
    def has_original_mask(self) -> bool:
        return self.path_original_mask is not None

    @property
    def label_keys(self) -> frozenset[tuple[str, str]]:
        """``(code_system, code)`` の集合。ラベル一致判定に使う。"""
        return label_keys(self.labels)

    def label_lines(self, japanese: bool = True) -> list[str]:
        """ラベルを1件1行の表記で返す。"""
        return [label.display(japanese) for label in self.labels]


@dataclass(frozen=True)
class CaseTarget:
    """描画対象となる1ファイル分の情報。"""

    institution: str
    study_key: str
    patient_id: str
    study_date: str
    series_key: str
    file_key: str
    image_path: Path
    shape: dict[str, Any]
    spacing: dict[str, Any]
    manufacturer: str
    masks: tuple[MaskEntry, ...]

    def header(self) -> dict[str, Any]:
        """図のタイトルに出す項目。"""
        return {
            "institution": self.institution,
            "patient_id": self.patient_id,
            "study_key": self.study_key,
            "study_date": self.study_date,
            "series_key": self.series_key,
            "file_key": self.file_key,
            "shape": f"{self.shape['w']}x{self.shape['h']}",
            "spacing": f"{self.spacing['x']}x{self.spacing['y']}",
            "manufacturer": self.manufacturer,
        }


def iter_files(
    dataset: dict[str, Any],
) -> Iterator[tuple[str, str, str, str, dict, dict]]:
    """``dataset`` を走査する。

    (施設, study, series, file, series辞書, file辞書) を順に返す。
    """
    for institution, studies in dataset.items():
        for study_key, study in studies.items():
            for series_key, series in study["series_list"].items():
                for file_key, file_rec in series["file_list"].items():
                    yield institution, study_key, series_key, file_key, series, file_rec


def build_target(
    dataset: dict[str, Any],
    institution: str | None,
    study_key: str | None,
    config: Config,
) -> CaseTarget:
    """指定された症例（未指定なら先頭）の描画対象を組み立てる。"""
    image_root = config.roots.image_root
    annotation_root = config.roots.annotation_root

    for inst, s_key, series_key, file_key, series, file_rec in iter_files(dataset):
        if institution is not None and inst != institution:
            continue
        if study_key is not None and s_key != study_key:
            continue

        study = dataset[inst][s_key]
        masks = tuple(
            MaskEntry(
                index=i,
                geometry_uid=ann["geometry_uid"],
                geometry_id=ann["geometry_id"],
                path_mask=(
                    resolve_annotation_path(ann["path_mask"], annotation_root)
                    if ann.get("path_mask")
                    else None
                ),
                path_original_mask=(
                    resolve_annotation_path(ann["path_original_mask"], annotation_root)
                    if ann.get("path_original_mask")
                    else None
                ),
                json_bbox=(ann["min_x"], ann["min_y"], ann["max_x"], ann["max_y"]),
                is_latest=ann["is_latest"],
                version=ann["version"],
                user=ann["user"],
                timestamp=ann["timestamp"],
                labels=parse_labels(ann["labels"]),
            )
            for i, ann in enumerate(file_rec["annotations"])
        )
        return CaseTarget(
            institution=inst,
            study_key=s_key,
            patient_id=study["patient_id"],
            study_date=str(study["study_date"]),
            series_key=series_key,
            file_key=file_key,
            image_path=resolve_image_path(file_rec["image_path"], image_root),
            shape=series["shape"],
            spacing=series["spacing"],
            manufacturer=series["manufacturer"],
            masks=masks,
        )

    raise LookupError(
        f"対象が見つからない: institution={institution!r} study={study_key!r}"
    )


def report_label_vocabulary(pairs: list[tuple[MaskEntry, np.ndarray]]) -> None:
    """描画したマスクに出現するラベル語彙を出力し、紛らわしい組み合わせを警告する。"""
    by_code: dict[str, set[str]] = {}
    by_key: dict[tuple[str, str], set[str]] = {}
    for entry, _ in pairs:
        for label in entry.labels:
            by_code.setdefault(label.code, set()).add(label.code_system)
            by_key.setdefault(label.key, set()).add(label.code_text)

    logger.info("--- ラベル語彙 ---")
    for key, texts in sorted(by_key.items()):
        logger.info("%s/%s : %s", key[0], key[1], " / ".join(sorted(texts)))

    for code, systems in sorted(by_code.items()):
        if len(systems) > 1:
            logger.warning(
                "code=%s が複数の code_system に跨る: %s"
                "（同じ code でも別の病名の可能性がある）",
                code,
                sorted(systems),
            )
    for key, texts in sorted(by_key.items()):
        if len(texts) > 1:
            logger.warning(
                "%s/%s に複数の code_text がある: %s",
                key[0],
                key[1],
                sorted(texts),
            )


def report_overlap(pairs: list[tuple[MaskEntry, np.ndarray]]) -> None:
    """マスク単体の統計とペアごとの重なりをログ出力する。"""
    logger.info("--- マスク単体の統計 ---")
    for entry, mask in pairs:
        stats = mask_stats(mask)
        logger.info(
            "[%d] %s px=%d bbox=%s comps=%d is_latest=%d ver=%d %s labels=%s",
            entry.index,
            entry.short_uid,
            stats.pixels,
            stats.bbox,
            stats.n_components,
            entry.is_latest,
            entry.version,
            entry.timestamp,
            " | ".join(entry.label_lines()),
        )
        if stats.bbox != tuple(entry.json_bbox):
            logger.warning(
                "[%d] %s JSONのbbox %s と実マスクのbbox %s が不一致",
                entry.index,
                entry.short_uid,
                tuple(entry.json_bbox),
                stats.bbox,
            )

    report_label_vocabulary(pairs)

    if len(pairs) < 2:
        return

    logger.info("--- ペアごとの重なり ---")
    for i, j in itertools.combinations(range(len(pairs)), 2):
        entry_i, mask_i = pairs[i]
        entry_j, mask_j = pairs[j]
        value = iou(mask_i, mask_j)
        note = ""
        if value > 0.9:
            hit = "完全一致" if value == 1.0 else "ほぼ一致"
            if entry_i.label_keys == entry_j.label_keys:
                note = f"  <== {hit}・ラベルも同一（重複アノテーションの疑い）"
            else:
                note = (
                    f"  <== {hit}だがラベルが異なる"
                    f"（{sorted(entry_i.label_keys)} vs {sorted(entry_j.label_keys)}）"
                )
        logger.info(
            "IoU([%d] %s, [%d] %s) = %.5f%s",
            i,
            entry_i.short_uid,
            j,
            entry_j.short_uid,
            value,
            note,
        )


def load_pairs(
    target: CaseTarget, image_shape: tuple[int, int], use_original: bool
) -> list[tuple[MaskEntry, np.ndarray]]:
    """描画できるマスクを ``(メタデータ, 配列)`` の組で集める。

    読めないマスクはスキップするが、必ず組で持つのでメタデータとの対応がずれない。
    ``--original`` では97件が ``path_original_mask`` を持たずスキップされるため、
    ここがずれると図とログの中身が全て1つずつずれる。
    """
    pairs: list[tuple[MaskEntry, np.ndarray]] = []
    for entry in target.masks:
        path = entry.path_original_mask if use_original else entry.path_mask
        if path is None:
            field = "path_original_mask" if use_original else "path_mask"
            logger.warning("[%d] %s %s が無い", entry.index, entry.short_uid, field)
            continue
        try:
            mask = load_binary_mask(path)
        except FileNotFoundError:
            logger.error(
                "[%d] %s マスクが存在しない: %s", entry.index, entry.short_uid, path
            )
            continue
        except MaskReadError as error:
            logger.error(
                "[%d] %s マスクを読めない: %s", entry.index, entry.short_uid, error
            )
            continue
        if mask.shape != image_shape:
            logger.error(
                "[%d] %s 形状不一致 画像%s vs マスク%s のためスキップ",
                entry.index,
                entry.short_uid,
                image_shape,
                mask.shape,
            )
            continue
        logger.info("[%d] %s %s", entry.index, entry.short_uid, path)
        pairs.append((entry, mask))
    return pairs


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", type=Path, default=DEFAULT_JSON, help="データセットJSON"
    )
    parser.add_argument("--institution", help="施設ID（未指定なら先頭）")
    parser.add_argument("--study", help="study名（未指定なら先頭）")
    parser.add_argument("--out-dir", type=Path, default=None, help="出力先")
    parser.add_argument("--config", type=Path, default=None, help="設定JSON")
    parser.add_argument(
        "--original",
        action="store_true",
        help="path_mask ではなく path_original_mask を描画する",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    config = load_config(args.config)
    out_dir = args.out_dir or (config.output_dir / "overlay")

    dataset = json.loads(args.json.read_text())["dataset"]
    target = build_target(dataset, args.institution, args.study, config)

    logger.info(
        "対象: %s / %s / %s (%s)",
        target.institution,
        target.patient_id,
        target.study_key,
        target.study_date,
    )
    logger.info("DICOM: %s", target.image_path)
    if not target.image_path.exists():
        logger.error("DICOMが存在しない: %s", target.image_path)
        return 1

    image = load_dicom_image(target.image_path)
    pairs = load_pairs(target, image.shape, args.original)
    if not pairs:
        logger.error("描画できるマスクが無い")
        return 1

    report_overlap(pairs)

    suffix = "_original" if args.original else ""
    out_path = out_dir / f"{target.institution}_{target.file_key}{suffix}.png"
    render(target.header(), image, pairs, out_path)
    if not JAPANESE_FONT:
        logger.info("CJKフォントが無いため図中のラベルは英語名のみ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
