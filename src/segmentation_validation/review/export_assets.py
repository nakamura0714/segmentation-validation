"""目視レビュー用の画像アセットを書き出す。**fiftyone を import しない**。

FiftyOne は DICOM を表示できないので、8bit の PNG へ変換する必要がある。
マスクは ``fo.Detection.mask_path`` で参照するため **bbox で切り出して**保存する
（2000x2400 の配列をそのまま DB へ入れると即座に破綻する）。

ここは走査（``core.measure``）とは別に **DICOM の全画素を読む唯一の場所**。
1枚あたり1〜3秒かかるので、既定では**目視対象の画像だけ**を書き出す。
冪等で、既存ファイルはスキップする。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image

from ..checks.base import ImageClass
from ..config import Config
from ..core.cpu import limit_native_threads
from ..core.imageio import MaskReadError, load_binary_mask, load_dicom_image
from ..core.progress import Progress
from ..core.records import AnnotationRecord, FileGroup
from ..datasets.pi6 import outside_body as pi6_outside_body
from ..datasets.pi6.references import load_references
from ..selection.image_decisions import ImageDecision

logger = logging.getLogger(__name__)

IMAGES_DIR = "images"
MASKS_DIR = "masks"
ORIGINALS_DIR = "originals"
BANDS_DIR = "bands"

# bbox を切り出すときの余白[px]。輪郭が枠に張り付くと形が読めない。
CROP_MARGIN = 4
# 退化した bbox（min >= max）を画面に出すための最小サイズ[px]。
# 座標が壊れていること自体が目視対象なので、見えないと確認できない。
MIN_DISPLAY_PX = 8


@dataclass
class AssetPaths:
    """1画像ぶんの書き出し結果。"""

    file_uid: str
    image: Path | None = None
    band: Path | None = None
    masks: dict[str, Path] = field(default_factory=dict)
    # bbox は FiftyOne の相対座標 [x, y, w, h]
    boxes: dict[str, list[float]] = field(default_factory=dict)
    # S04 目視用。``path_original_mask`` を final とは別レイヤーとして同じ形で書き出す。
    originals: dict[str, Path] = field(default_factory=dict)
    original_boxes: dict[str, list[float]] = field(default_factory=dict)
    # 表示のために枠を広げた annotation（退化した bbox）。
    # 目視する人に「元の座標はこうだった」と伝えるため記録する。
    adjusted: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


def export_group(
    group: FileGroup,
    config: Config,
    out_dir: Path,
    reference_path: Any = None,
    force: bool = False,
) -> AssetPaths:
    """1画像とその全マスクを書き出す。"""
    result = AssetPaths(file_uid=group.file_uid)
    stem = f"{group.dataset_id}__{group.file}"

    image_path = out_dir / IMAGES_DIR / f"{stem}.{_ext(config)}"
    try:
        if force or not image_path.exists():
            array = load_dicom_image(group.resolved_image_path)
            _save_gray(array, image_path, config)
        result.image = image_path
    except Exception as error:  # 1枚失敗しても他は続ける
        result.error = f"{type(error).__name__}: {error}"
        logger.warning("画像を書き出せない %s: %s", group.file, result.error)
        return result

    shape = _size_of(image_path)
    for record in group.records:
        if record.resolved_path_mask is None:
            # マスクを持たない annotation（bbox / elliptical）。
            # JSONの座標そのものが目視対象（M07 の退化・範囲外）なので、
            # マスクが無くても枠だけ出す。出さないと確認できない。
            box, adjusted = _json_box(record, shape)
            if box is not None:
                result.boxes[record.geometry_uid] = box
                if adjusted:
                    result.adjusted[record.geometry_uid] = adjusted
            continue
        mask_path = out_dir / MASKS_DIR / f"{record.geometry_uid}.png"
        try:
            box = _export_mask(record.resolved_path_mask, mask_path, shape, force)
        except (FileNotFoundError, MaskReadError) as error:
            logger.warning("マスクを書き出せない %s: %s", record.short_uid, error)
            continue
        if box is None:
            continue
        result.masks[record.geometry_uid] = mask_path
        result.boxes[record.geometry_uid] = box

        # S04（修正前後の食い違い）を目視できるよう、original も final と同じ形で
        # 別レイヤーに書き出す。final が書き出せた場合にのみ意味を持つ比較対象。
        if record.resolved_path_original_mask is not None:
            original_path = out_dir / ORIGINALS_DIR / f"{record.geometry_uid}.png"
            try:
                original_box = _export_mask(
                    record.resolved_path_original_mask, original_path, shape, force
                )
            except (FileNotFoundError, MaskReadError) as error:
                logger.warning(
                    "originalマスクを書き出せない %s: %s", record.short_uid, error
                )
                original_box = None
            if original_box is not None:
                result.originals[record.geometry_uid] = original_path
                result.original_boxes[record.geometry_uid] = original_box

    band_path = out_dir / BANDS_DIR / f"{stem}.png"
    if reference_path is not None:
        try:
            if force or not band_path.exists():
                _export_band(group, config, reference_path, band_path)
            if band_path.exists():
                result.band = band_path
        except Exception as error:
            logger.debug("側方バンドを書き出せない %s: %s", group.file, error)

    return result


def export_assets(
    groups: Sequence[FileGroup],
    config: Config,
    out_dir: Path,
    reference_paths: dict[str, Any] | None = None,
    force: bool = False,
    jobs: int = 1,
) -> dict[str, AssetPaths]:
    """必要な画像を書き出す。DICOM の全画素を読むので時間がかかる。

    ``jobs`` はスレッド並列数（``scan --jobs`` と同じパターン）。DICOMの
    フル画素読みはNFS I/O待ちが支配的でPIL/OpenCV/NumPyがGILを解放するため、
    逐次実行だった従来（``jobs=1``）よりスレッド並列化が素直に効く。
    ``export_group`` は1つの ``FileGroup`` だけを見る純粋な関数で、
    書き込み先ファイルパスも group ごとに独立しているため競合しない。
    """
    for name in (IMAGES_DIR, MASKS_DIR, ORIGINALS_DIR, BANDS_DIR):
        (out_dir / name).mkdir(parents=True, exist_ok=True)
    # 側方バンドの生成やDICOMデコードで OpenCV を使う。共有サーバーなので
    # 内部スレッドと jobs の掛け算にならないよう絞る（core/cpu.py 参照）。
    limit_native_threads(jobs)

    logger.info(
        "アセットを書き出す: %d 画像 -> %s"
        "（DICOMの全画素を読むので1枚1〜3秒、jobs=%d）",
        len(groups),
        out_dir,
        jobs,
    )
    progress = Progress(len(groups), label="書き出し", every=25)
    resolvers = reference_paths or {}
    results: dict[str, AssetPaths] = {}

    def work(group: FileGroup) -> AssetPaths:
        reference = resolvers.get(group.dataset_id)
        return export_group(group, config, out_dir, reference, force)

    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for group, result in zip(groups, pool.map(work, groups)):
            results[group.file_uid] = result
            progress.advance()
    progress.finish()

    failed = [r for r in results.values() if r.error]
    if failed:
        logger.warning("書き出せなかった画像: %d 枚", len(failed))
    return results


# ------------------------------------------------------------------ internal


def _export_mask(
    mask_source: Path | None,
    out_path: Path,
    image_shape: tuple[int, int],
    force: bool,
) -> list[float] | None:
    """マスクを bbox で切り出して保存し、相対座標の bbox を返す。

    ``fo.Detection.mask_path`` は **bbox 内の**マスクを期待するので、
    全画面のマスクをそのまま渡してはいけない。``path_mask`` / ``path_original_mask``
    のどちらでも使えるよう経路は呼び出し側が渡す（``load_binary_mask`` は RGBA の
    alpha も自動で拾うので original でも共通化できる）。
    """
    assert mask_source is not None
    mask = load_binary_mask(mask_source)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None

    height, width = mask.shape[:2]
    x0 = max(int(xs.min()) - CROP_MARGIN, 0)
    y0 = max(int(ys.min()) - CROP_MARGIN, 0)
    x1 = min(int(xs.max()) + CROP_MARGIN + 1, width)
    y1 = min(int(ys.max()) + CROP_MARGIN + 1, height)

    if force or not out_path.exists():
        crop = mask[y0:y1, x0:x1]
        Image.fromarray((crop * 255).astype(np.uint8), mode="L").save(out_path)

    # 相対座標は表示画像の大きさで割る。書き出しで縮小していないので同じ。
    img_h, img_w = image_shape
    return [x0 / img_w, y0 / img_h, (x1 - x0) / img_w, (y1 - y0) / img_h]


def _json_box(
    record: AnnotationRecord, image_shape: tuple[int, int]
) -> tuple[list[float] | None, dict[str, Any]]:
    """JSONの ``min_x/min_y/max_x/max_y`` から表示用の相対 bbox を作る。

    退化（min >= max）や範囲外を**そのまま出すと画面に現れない**ので、
    表示できる最小サイズまで広げる。ただし元の座標は ``adjusted`` に残して
    目視する人に伝える —— 座標が壊れていること自体が確認対象だから。
    """
    height, width = image_shape
    if not width or not height:
        return None, {}
    x0, y0, x1, y1 = record.json_bbox
    adjusted: dict[str, Any] = {}

    box_w, box_h = x1 - x0, y1 - y0
    if box_w < MIN_DISPLAY_PX or box_h < MIN_DISPLAY_PX:
        adjusted["original_bbox"] = [x0, y0, x1, y1]
        adjusted["reason"] = "degenerate_widened_for_display"
        x1 = x0 + max(box_w, MIN_DISPLAY_PX)
        y1 = y0 + max(box_h, MIN_DISPLAY_PX)

    # 範囲外はクリップせずそのまま出す（はみ出していることが見えるべき）。
    # ただし FiftyOne が扱える範囲に収める。
    rel = [x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height]
    if any(v < -1 or v > 2 for v in rel):
        adjusted.setdefault("original_bbox", [x0, y0, x1, y1])
        adjusted["reason"] = "out_of_image_clamped_for_display"
        rel = [min(max(v, -0.5), 1.5) for v in rel]
    return rel, adjusted


def _export_band(
    group: FileGroup, config: Config, reference_path: Any, out_path: Path
) -> None:
    """側方バンドを PNG で保存する。S05 がなぜ発火したかを画面で見るため。"""
    paths = {
        kind: reference_path(group.image_path, kind)
        for kind in config.reference_masks.roots
    }
    references = load_references(
        paths, config.reference_masks.required, group.series_shape
    )
    if not references.usable:
        return
    spacing_y = group.spacing[1]
    region = pi6_outside_body.build_region(references.masks, spacing_y)
    if region is None:
        return
    # 1 = バンド内。FiftyOne の Segmentation は非ゼロを領域として扱う。
    Image.fromarray(region.lateral_band.astype(np.uint8), mode="L").save(out_path)


def _save_gray(array: np.ndarray, out_path: Path, config: Config) -> None:
    """0.0-1.0 の float を 8bit 画像として保存する。"""
    image = Image.fromarray((np.clip(array, 0, 1) * 255).astype(np.uint8), mode="L")
    if config.review.image_format == "jpeg":
        image.save(out_path, quality=config.review.jpeg_quality, optimize=True)
    else:
        image.save(out_path, optimize=True)


def _size_of(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        width, height = image.size
    return (height, width)


def _ext(config: Config) -> str:
    return "jpg" if config.review.image_format == "jpeg" else "png"


def select_review_groups(
    groups: Iterable[FileGroup],
    annotation_uids: set[str],
    image_uids: set[str],
    include_all: bool = False,
) -> list[FileGroup]:
    """書き出す画像を選ぶ。

    既定は**目視対象だけ**（pending の annotation を含む画像 + pending の画像）。
    DICOM の全画素読みは1枚1〜3秒かかるので、1083枚すべて書き出すと数分かかる。
    全体を FiftyOne で見たい場合は ``include_all``。
    """
    selected = []
    for group in groups:
        if include_all:
            selected.append(group)
            continue
        if group.file_uid in image_uids:
            selected.append(group)
            continue
        if any(r.geometry_uid in annotation_uids for r in group.records):
            selected.append(group)
    return selected


#: スポットチェックの対象になる画像分類（＝ annotation を持たない画像）。
#: ``ANNOTATED`` は対象外（annotation側で個別に採否・目視するため）。
SPOT_CHECK_IMAGE_CLASSES = frozenset(
    {
        ImageClass.NEGATIVE_CASE.value,
        ImageClass.UNANNOTATED_VIEW.value,
        ImageClass.UNANNOTATED_ORPHAN.value,
    }
)


def select_spot_check_groups(
    groups: Iterable[FileGroup],
    image_decisions: Sequence[ImageDecision],
    per_bucket: int,
) -> list[FileGroup]:
    """未アノテーション画像を ``(dataset_id, image_class, series_image_index)``
    ごとに最大 ``per_bucket`` 件、決定的にサンプルする。

    未アノテーション画像は自動で keep/exclude が決まり（``image_decisions.py``）
    pending にはならないため、既定の ``select_review_groups`` では一切書き出されない。
    ここでの選定は**採否を一切変えない**——あくまで自動判定が妥当かをデータセット
    単位で目視確認するためだけの、追加のサンプル抽出。

    バケット内は ``file_uid`` でソートしてから先頭N件を取る。乱数にすると
    再実行のたびに違う画像が選ばれ、「既存アセットをスキップ」の恩恵も薄れるため
    決定的にしてある。
    """
    by_file_uid = {d.file_uid: d for d in image_decisions}
    buckets: dict[tuple[str, str, int], list[FileGroup]] = {}
    for group in groups:
        decision = by_file_uid.get(group.file_uid)
        if decision is None or decision.image_class not in SPOT_CHECK_IMAGE_CLASSES:
            continue
        key = (group.dataset_id, decision.image_class, group.series_image_index)
        buckets.setdefault(key, []).append(group)

    selected: list[FileGroup] = []
    for members in buckets.values():
        members.sort(key=lambda g: g.file_uid)
        selected.extend(members[:per_bucket])
    return selected
