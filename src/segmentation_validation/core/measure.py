"""単一走査パス。**画素ファイルを開くのはこのモジュールだけ**。

1665枚のマスク（各 ~2000x2400、実測93ms/枚）をディスクから1度だけ読む。
走査の単位は**ファイル**で、同じファイルのマスクを同時にメモリへ載せるので、
ペアIoU（重複検出）と参照マスクとの突き合わせ（体外領域）が追加I/Oなしで手に入る。

DICOM はヘッダのみ（``stop_before_pixels=True``、22ms/件）。解像度チェックに
画素データは要らないので、ここが実質タダになる。全画素読みは目視用の画像を
書き出すときだけ。

``checks/`` 配下はここが出した計測値だけを入力とする純関数で、
PIL も pydicom も import しない。
"""

from __future__ import annotations

import itertools
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np

from ..config import Config
from ..datasets.pi6 import outside_body as pi6_outside_body
from ..datasets.pi6.references import ReferenceSet, ReferenceStatus, load_references
from .cache import MeasurementCache
from .cpu import limit_native_threads
from .geometry import (
    MaskStats,
    containment,
    iou,
    mask_hash,
    mask_stats,
    pixels_to_mm2,
)
from .imageio import MaskReadError, RawMask, load_dicom_header, load_mask_raw
from .progress import Progress
from .records import AnnotationRecord, FileGroup

logger = logging.getLogger(__name__)

# マスクの役割。``path_mask`` が検証の主対象で、``path_original_mask`` は比較用。
ROLE_MASK = "mask"
ROLE_ORIGINAL = "original"

#: ``image_path`` から参照マスクのパスを導く関数（アダプタが提供する）。
ReferencePathResolver = Callable[[str, str], "Path | None"]


@dataclass
class MaskMeasurement:
    """マスク1枚の計測値。``role`` で final / original を区別する。"""

    file_uid: str
    geometry_uid: str
    role: str
    dataset_id: str
    source_json: str
    annotation_type: str
    mask_path: str | None
    exists: bool
    file_bytes: int | None = None
    read_error: str | None = None

    # --- 二値化前の生属性（M02/M03 はこれしか見ない） ---
    pil_mode: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    ndim: int | None = None
    n_channels: int | None = None
    dtype: str | None = None
    value_min: int | None = None
    value_max: int | None = None
    n_unique_values: int | None = None
    unique_values: list[int] = field(default_factory=list)
    unique_values_truncated: bool = False
    binarized_from: str | None = None

    # --- 二値化後の形状量 ---
    fg_pixels: int | None = None
    total_pixels: int | None = None
    fg_ratio: float | None = None
    bbox: list[int] | None = None
    n_components: int | None = None
    component_areas: list[int] = field(default_factory=list)
    component_areas_truncated: bool = False
    touches_border: bool | None = None
    area_mm2: float | None = None
    component_areas_mm2: list[float] = field(default_factory=list)
    mask_hash: str | None = None

    # --- 体外領域（参照マスクがファイル単位で1度だけ読まれる） ---
    reference_status: str | None = None
    lat_containment: float | None = None
    lat_outside_mm2: float | None = None
    lat_outside_pixels: int | None = None
    lat_max_mm: float | None = None
    lat_margin_mm: float | None = None

    # --- original 専用。修正前後の食い違いを測る ---
    filled_iou_vs_mask: float | None = None
    filled_pixels: int | None = None
    difference_pixels: int | None = None
    difference_ratio: float | None = None


@dataclass
class FileMeasurement:
    """ファイル1件の計測値。DICOMヘッダと参照マスクの状態。"""

    file_uid: str
    dataset_id: str
    source_json: str
    institution: str
    study: str
    series: str
    file: str
    image_path: str
    image_exists: bool
    dicom_read_error: str | None = None
    dicom_rows: int | None = None
    dicom_columns: int | None = None
    dicom_bits_allocated: int | None = None
    dicom_bits_stored: int | None = None
    dicom_photometric: str | None = None
    dicom_spacing_x: float | None = None
    dicom_spacing_y: float | None = None
    dicom_manufacturer: str | None = None
    series_shape: list[int] | None = None
    spacing_x: float | None = None
    spacing_y: float | None = None
    n_annotations: int = 0
    n_masks: int = 0
    counts_by_type: dict[str, int] = field(default_factory=dict)
    reference_status: str | None = None
    reference_available: list[str] = field(default_factory=list)
    reference_paths: dict[str, str] = field(default_factory=dict)
    reference_errors: dict[str, str] = field(default_factory=dict)
    band_ratio: float | None = None
    region_ratio: float | None = None
    scan_seconds: float | None = None


@dataclass
class PairMeasurement:
    """同一ファイル内のマスク2枚の関係。

    IoU だけでなく包含率も持つ。小さいマスクが大きいマスクに埋没している場合は
    IoU が低く出るが、重複としては同じくらい疑わしい。
    """

    file_uid: str
    uid_a: str
    uid_b: str
    iou: float
    intersection_pixels: int
    union_pixels: int
    containment_a_in_b: float
    containment_b_in_a: float
    pixel_identical: bool
    same_label_keys: bool
    # 検出条件には使わず、severity の判定と絞り込みのために残す。
    both_latest: bool
    same_user: bool
    same_request: bool
    gid_a: int
    gid_b: int
    user_a: str | None
    user_b: str | None
    timestamp_a: str | None
    timestamp_b: str | None
    version_a: int
    version_b: int


def measure_file(
    group: FileGroup,
    config: Config,
    reference_path: ReferencePathResolver | None = None,
) -> tuple[FileMeasurement, list[MaskMeasurement], list[PairMeasurement]]:
    """ファイル1件を走査する。並列化・再開の単位。

    処理順:

    1. DICOM ヘッダのみ読む（画素には触れない）
    2. 参照マスクをファイル単位で1度だけ読み、側方バンドを組む
    3. 各マスクを1度だけ開き、生属性 → 二値化 → 形状量 → 体外指標
    4. ``path_original_mask`` を1度だけ開き、穴埋め結果を final と比べる
    5. 全マスクが同時に載っているのでペアIoUをその場で計算
    6. 配列を捨てて、スカラだけを返す
    """
    import time

    started = time.monotonic()
    header = load_dicom_header(group.resolved_image_path)
    spacing_x, spacing_y = _effective_spacing(group, header)

    references = _load_reference_set(group, config, reference_path, header)
    region = None
    if references.usable:
        region = pi6_outside_body.build_region(references.masks, spacing_y)

    masks: list[MaskMeasurement] = []
    arrays: list[tuple[AnnotationRecord, np.ndarray]] = []

    for record in group.records:
        if record.resolved_path_mask is None:
            continue
        raw = load_mask_raw(record.resolved_path_mask)
        measurement = _measure_mask(
            record, raw, ROLE_MASK, spacing_x, spacing_y, references, region, config
        )
        masks.append(measurement)
        if raw.array is not None and raw.read_error is None:
            binary = raw.binarize()
            arrays.append((record, binary))

            if record.resolved_path_original_mask is not None:
                masks.append(
                    _measure_original(
                        record, binary, spacing_x, spacing_y, references, region, config
                    )
                )

    pairs = _measure_pairs(group, arrays)

    file_measurement = FileMeasurement(
        file_uid=group.file_uid,
        dataset_id=group.dataset_id,
        source_json=group.source_json,
        institution=group.institution,
        study=group.study,
        series=group.series,
        file=group.file,
        image_path=group.image_path,
        image_exists=header.exists,
        dicom_read_error=header.read_error,
        dicom_rows=header.rows,
        dicom_columns=header.columns,
        dicom_bits_allocated=header.bits_allocated,
        dicom_bits_stored=header.bits_stored,
        dicom_photometric=header.photometric,
        dicom_spacing_x=header.spacing_x,
        dicom_spacing_y=header.spacing_y,
        dicom_manufacturer=header.manufacturer,
        series_shape=list(group.series_shape) if group.series_shape else None,
        spacing_x=spacing_x,
        spacing_y=spacing_y,
        n_annotations=len(group.records),
        n_masks=len(group.mask_records),
        counts_by_type=group.counts_by_type(),
        reference_status=references.status,
        reference_available=list(references.available),
        reference_paths=references.paths,
        reference_errors=references.errors,
        band_ratio=region.band_ratio if region else None,
        region_ratio=region.region_ratio if region else None,
        scan_seconds=round(time.monotonic() - started, 3),
    )
    return file_measurement, masks, pairs


def scan(
    groups: Sequence[FileGroup],
    config: Config,
    cache: MeasurementCache,
    reference_paths: Mapping[str, ReferencePathResolver] | None = None,
    jobs: int = 8,
    limit: int | None = None,
) -> int:
    """全ファイルを走査してキャッシュへ追記する。走査済みは飛ばす。

    スレッドを使うのは、NFS の I/O 待ちが支配的で、PIL のデコードも NumPy も
    OpenCV も重い部分で GIL を解放するから。プロセスにすると
    レコードの pickle と cv2 の再 import が毎回のしかかる。

    書き込みは完了した future を**メインスレッドで**順に処理する。
    ``mask行 → pair行 → file行`` の順に書くので、途中で落ちても
    file 行を持たない孤児行を読み込み時に捨てるだけで整合が取れる。
    """
    done = cache.completed_file_uids()
    pending = [group for group in groups if group.file_uid not in done]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        logger.info("走査済み（%d ファイル）。やることが無い", len(done))
        return 0

    logger.info(
        "走査開始: %d ファイル（済 %d / 全 %d）jobs=%d",
        len(pending),
        len(done),
        len(groups),
        jobs,
    )
    # OpenCV の内部スレッドと jobs が掛け算になるのを防ぐ（core/cpu.py 参照）。
    limit_native_threads(jobs)
    progress = Progress(len(pending), label="走査")
    resolvers = reference_paths or {}
    scanned = 0

    def work(group: FileGroup):
        return measure_file(group, config, resolvers.get(group.dataset_id))

    with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
        for group, result in zip(pending, pool.map(work, pending)):
            file_measurement, masks, pairs = result
            cache.append(cache.masks_path, [asdict(m) for m in masks])
            cache.append(cache.pairs_path, [asdict(p) for p in pairs])
            # file 行は必ず最後。これが「このファイルは完了した」の印になる。
            cache.append(cache.files_path, [asdict(file_measurement)])
            scanned += 1
            progress.advance()

    progress.finish()
    return scanned


# ------------------------------------------------------------------ internal


def _measure_mask(
    record: AnnotationRecord,
    raw: RawMask,
    role: str,
    spacing_x: float | None,
    spacing_y: float | None,
    references: ReferenceSet,
    region: pi6_outside_body.BodyRegion | None,
    config: Config,
) -> MaskMeasurement:
    measurement = MaskMeasurement(
        file_uid=record.file_uid,
        geometry_uid=record.geometry_uid,
        role=role,
        dataset_id=record.dataset_id,
        source_json=record.source_json,
        annotation_type=record.annotation_type,
        mask_path=record.path_mask if role == ROLE_MASK else record.path_original_mask,
        exists=raw.exists,
        file_bytes=raw.file_bytes,
        read_error=raw.read_error,
        pil_mode=raw.pil_mode,
        image_width=raw.width,
        image_height=raw.height,
        ndim=raw.ndim,
        n_channels=raw.n_channels,
        dtype=raw.dtype,
        value_min=raw.value_min,
        value_max=raw.value_max,
        n_unique_values=raw.n_unique_values,
        unique_values=list(raw.unique_values),
        unique_values_truncated=raw.unique_values_truncated,
        binarized_from=raw.binarized_from,
        reference_status=references.status,
    )
    if raw.array is None or raw.read_error is not None:
        return measurement

    binary = raw.binarize()
    _fill_shape(measurement, mask_stats(binary), spacing_x, spacing_y)
    measurement.mask_hash = mask_hash(binary)
    _fill_outside_body(measurement, binary, region, config)
    return measurement


def _measure_original(
    record: AnnotationRecord,
    final_mask: np.ndarray,
    spacing_x: float | None,
    spacing_y: float | None,
    references: ReferenceSet,
    region: pi6_outside_body.BodyRegion | None,
    config: Config,
) -> MaskMeasurement:
    """``path_original_mask``（修正前）を読み、final との食い違いを測る。

    original は Annotation Tool が保存した生のブラシストロークで、
    RGBA のアンチエイリアス付きのことがある（仕様上正常）。
    輪郭を穴埋めした結果が final と食い違う場合だけが確認対象になる。
    """
    assert record.resolved_path_original_mask is not None
    raw = load_mask_raw(record.resolved_path_original_mask)
    measurement = _measure_mask(
        record, raw, ROLE_ORIGINAL, spacing_x, spacing_y, references, region, config
    )
    if raw.array is None or raw.read_error is not None:
        return measurement

    brush = raw.binarize()
    if brush.shape != final_mask.shape:
        measurement.read_error = f"shape {brush.shape} != final {final_mask.shape}"
        return measurement

    filled = _fill_outline(brush)
    measurement.filled_pixels = int(np.count_nonzero(filled))
    measurement.filled_iou_vs_mask = iou(filled, final_mask)
    difference = int(np.count_nonzero(filled ^ final_mask))
    measurement.difference_pixels = difference
    final_pixels = int(np.count_nonzero(final_mask))
    measurement.difference_ratio = difference / final_pixels if final_pixels else None
    return measurement


def _measure_pairs(
    group: FileGroup, arrays: list[tuple[AnnotationRecord, np.ndarray]]
) -> list[PairMeasurement]:
    """同一ファイル内の全ペアの関係。

    全マスクが同時にメモリに載っているので追加I/Oは無い。最大は14マスクの
    ファイルで91ペア、bool配列で67MB。
    """
    pairs: list[PairMeasurement] = []
    for (record_a, mask_a), (record_b, mask_b) in itertools.combinations(arrays, 2):
        if mask_a.shape != mask_b.shape:
            continue
        intersection = int(np.count_nonzero(mask_a & mask_b))
        union = int(np.count_nonzero(mask_a | mask_b))
        pairs.append(
            PairMeasurement(
                file_uid=group.file_uid,
                uid_a=record_a.geometry_uid,
                uid_b=record_b.geometry_uid,
                iou=iou(mask_a, mask_b),
                intersection_pixels=intersection,
                union_pixels=union,
                containment_a_in_b=containment(mask_a, mask_b),
                containment_b_in_a=containment(mask_b, mask_a),
                pixel_identical=bool(np.array_equal(mask_a, mask_b)),
                same_label_keys=record_a.label_keys == record_b.label_keys,
                both_latest=bool(record_a.is_latest == 1 and record_b.is_latest == 1),
                same_user=record_a.user == record_b.user,
                same_request=record_a.annotation_request == record_b.annotation_request,
                gid_a=record_a.geometry_id,
                gid_b=record_b.geometry_id,
                user_a=record_a.user,
                user_b=record_b.user,
                timestamp_a=record_a.timestamp,
                timestamp_b=record_b.timestamp,
                version_a=record_a.version,
                version_b=record_b.version,
            )
        )
    return pairs


def _fill_shape(
    measurement: MaskMeasurement,
    stats: MaskStats,
    spacing_x: float | None,
    spacing_y: float | None,
) -> None:
    measurement.fg_pixels = stats.pixels
    measurement.total_pixels = stats.total_pixels
    measurement.fg_ratio = stats.fg_ratio
    measurement.bbox = list(stats.bbox) if stats.bbox else None
    measurement.n_components = stats.n_components
    measurement.component_areas = list(stats.component_areas)
    measurement.component_areas_truncated = stats.component_areas_truncated
    measurement.touches_border = stats.touches_border
    measurement.area_mm2 = pixels_to_mm2(stats.pixels, spacing_x, spacing_y)
    measurement.component_areas_mm2 = [
        value
        for value in (
            pixels_to_mm2(area, spacing_x, spacing_y) for area in stats.component_areas
        )
        if value is not None
    ]


def _fill_outside_body(
    measurement: MaskMeasurement,
    binary: np.ndarray,
    region: pi6_outside_body.BodyRegion | None,
    config: Config,
) -> None:
    if region is None:
        return
    if binary.shape != region.distance_mm.shape:
        measurement.reference_status = ReferenceStatus.SHAPE_MISMATCH
        return
    metrics = pi6_outside_body.evaluate(
        binary,
        region,
        config.reference_masks.margin_mm,
        measurement.area_mm2,
    )
    measurement.lat_containment = metrics.containment
    measurement.lat_outside_mm2 = metrics.outside_mm2
    measurement.lat_outside_pixels = metrics.outside_pixels
    measurement.lat_max_mm = metrics.max_distance_mm
    measurement.lat_margin_mm = metrics.margin_mm


def _fill_outline(brush: np.ndarray) -> np.ndarray:
    """ブラシの輪郭を外側からの flood fill で穴埋めする。"""
    import cv2

    binary = brush.astype(np.uint8)
    height, width = binary.shape
    flood = binary.copy()
    work = np.zeros((height + 2, width + 2), dtype=np.uint8)
    cv2.floodFill(flood, work, (0, 0), 1)
    return brush | (flood == 0)


def _load_reference_set(
    group: FileGroup,
    config: Config,
    reference_path: ReferencePathResolver | None,
    header: Any,
) -> ReferenceSet:
    if reference_path is None:
        return ReferenceSet(status=ReferenceStatus.NO_REFERENCE)
    paths = {
        kind: reference_path(group.image_path, kind)
        for kind in config.reference_masks.roots
    }
    expected = header.shape if header.shape else group.series_shape
    return load_references(paths, config.reference_masks.required, expected)


def _effective_spacing(
    group: FileGroup, header: Any
) -> tuple[float | None, float | None]:
    """spacing は JSON を優先し、無ければ DICOM から補う。

    JSON 側が null のシリーズが実在する（segmed 2件）。DICOM から拾えれば
    mm² を計算できるので、``cannot_determine`` を避けられる。
    """
    spacing_x, spacing_y = group.spacing
    if spacing_x is None:
        spacing_x = header.spacing_x
    if spacing_y is None:
        spacing_y = header.spacing_y
    return spacing_x, spacing_y


def rows_to_measurements(rows: Iterable[dict[str, Any]], cls: type) -> list[Any]:
    """JSONL の行を計測値のオブジェクトへ戻す。

    古いキャッシュに新しいフィールドが無くても落ちないよう、未知のキーは捨て、
    足りないキーは既定値に任せる。
    """
    from dataclasses import fields as dataclass_fields

    known = {f.name for f in dataclass_fields(cls)}
    return [cls(**{k: v for k, v in row.items() if k in known}) for row in rows]


__all__ = [
    "FileMeasurement",
    "MaskMeasurement",
    "MaskReadError",
    "PairMeasurement",
    "ROLE_MASK",
    "ROLE_ORIGINAL",
    "measure_file",
    "rows_to_measurements",
    "scan",
]
