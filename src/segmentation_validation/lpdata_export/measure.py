"""導出値を出すための画素パス。

テンプレートの「導出値（マスクと pixel_spacing から再計算できるキャッシュ）」を
実際にマスクを読んで求める。読むのは

- 気胸マスク（合成して面積を出す。``mask_mode == "generate"`` なら書き出しも）
- 肺野・胸郭の参照マスク（``lung_rect`` の材料）

``core.measure.scan`` と同じ形にしてある（スレッド並列 + ネイティブスレッド抑制 +
進捗表示 + JSONL キャッシュ）。NFS の I/O 待ちが支配的で、PIL も NumPy も OpenCV も
重い部分で GIL を解放するため、プロセスではなくスレッドを使う。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..config import Config
from ..core.cpu import limit_native_threads
from ..core.geometry import mask_stats, pixels_to_mm2
from ..core.imageio import load_reference_mask, save_binary_mask
from ..core.paths import reference_mask_path
from ..core.progress import Progress
from ..core.records import FileGroup
from .labels import PNEUMOTHORAX
from .masks import merge_masks
from .paths import mask_path_for, sample_id_for

logger = logging.getLogger(__name__)

#: ``lung_rect`` の材料。テンプレートは
#: 「**両方**から BBox が取れるときだけ算出」と定める。
REFERENCE_KINDS = ("lung", "thorax")


@dataclass
class SampleMeasurement:
    """1画像ぶんの導出値。すべて「測れなかった」を None で表す。"""

    sample_id: str
    file_uid: str

    # --- 気胸マスク（合成後） ---
    #: 非ゼロ画素数。マスクが無い / 全ゼロなら None。
    pneumothorax_pixels: int | None = None
    pneumothorax_area_mm2: float | None = None
    #: 合成に使った元マスクの絶対パス。manifest に残す。
    mask_sources: list[str] = field(default_factory=list)
    #: 結合マスクPNGを実際に書き出したか。
    mask_written: bool = False

    # --- 参照マスク ---
    lung_mask_path: str | None = None
    thorax_mask_path: str | None = None
    #: 閉区間 ``(x_min, y_min, x_max, y_max)``。``core.geometry.mask_stats`` の向き。
    lung_bbox: list[int] | None = None
    thorax_bbox: list[int] | None = None

    errors: list[str] = field(default_factory=list)

    @property
    def has_pneumothorax_mask(self) -> bool:
        return bool(self.pneumothorax_pixels)

    def lung_rect(self) -> dict[str, int] | None:
        """``lung_rect``（lp-data の ``Rect``）。両方の BBox が要る。

        **半開区間へ変換する。** ``mask_stats.bbox`` は max を含む閉区間だが、
        lp-data の ``Rect`` は ``x_max`` / ``y_max`` を含まない。変換を忘れても
        lp-data は例外を出さず、1px 小さい矩形が黙って通る。

        片方だけで代用しないのはテンプレートの規約。肺野のみ由来の矩形（胸郭より
        系統的に小さい）が同じ属性に混ざると、由来を記録する手段が無いまま
        属性の意味がサンプルごとに変わる。
        """
        if self.lung_bbox is None or self.thorax_bbox is None:
            return None
        lung, thorax = self.lung_bbox, self.thorax_bbox
        return {
            "x_min": min(lung[0], thorax[0]),
            "y_min": min(lung[1], thorax[1]),
            "x_max": max(lung[2], thorax[2]) + 1,
            "y_max": max(lung[3], thorax[3]) + 1,
        }


def measure_group(
    group: FileGroup,
    config: Config,
    *,
    mask_output_dir: Path,
    write_mask: bool = False,
) -> SampleMeasurement:
    """1画像ぶんの導出値を求める。マスクを実際に読む。"""
    sample_id = sample_id_for(group)
    measurement = SampleMeasurement(sample_id=sample_id, file_uid=group.file_uid)

    _measure_pneumothorax(
        measurement, group, mask_output_dir=mask_output_dir, write_mask=write_mask
    )
    _measure_references(measurement, group, config)
    return measurement


def _measure_pneumothorax(
    measurement: SampleMeasurement,
    group: FileGroup,
    *,
    mask_output_dir: Path,
    write_mask: bool,
) -> None:
    paths = [
        record.resolved_path_mask
        for record in group.records
        if record.resolved_path_mask is not None
        and any(label.code_text_eng == PNEUMOTHORAX for label in record.labels)
    ]
    if not paths:
        return

    merged = merge_masks(paths)
    measurement.mask_sources = merged.sources
    measurement.errors.extend(merged.errors)
    if merged.mask is None:
        return

    stats = mask_stats(merged.mask)
    measurement.pneumothorax_pixels = stats.pixels
    spacing_x, spacing_y = group.spacing
    measurement.pneumothorax_area_mm2 = pixels_to_mm2(
        stats.pixels, spacing_x, spacing_y
    )

    if write_mask:
        try:
            save_binary_mask(
                mask_path_for(mask_output_dir, measurement.sample_id), merged.mask
            )
            measurement.mask_written = True
        except OSError as error:
            measurement.errors.append(f"結合マスクを書けない: {error}")


def _measure_references(
    measurement: SampleMeasurement, group: FileGroup, config: Config
) -> None:
    """肺野・胸郭の参照マスクを読み、パスと BBox を記録する。

    参照マスクは別パイプラインの生成物で、実データでは全体の1/3程度にしか無い。
    無いこと自体は異常ではないのでエラーにはせず、``lung_rect`` を null にする。
    """
    for kind in REFERENCE_KINDS:
        root = config.reference_masks.roots.get(kind)
        if not root:
            continue
        try:
            path = reference_mask_path(
                group.image_path, root, config.reference_masks.drop_path_components
            )
        except ValueError:
            continue
        if not path.exists():
            continue
        try:
            mask = load_reference_mask(path)
        except (FileNotFoundError, OSError, RuntimeError) as error:
            measurement.errors.append(f"{kind} 参照マスクを読めない {path}: {error}")
            continue

        stats = mask_stats(mask)
        if stats.bbox is None:
            # 実体はあるが全ゼロ。非ゼロ領域が無いので「マスク無し」と同じ扱いにする。
            continue
        setattr(measurement, f"{kind}_mask_path", str(path))
        setattr(measurement, f"{kind}_bbox", list(stats.bbox))


# --------------------------------------------------------------------- 一括


def measure_all(
    groups: Sequence[FileGroup],
    config: Config,
    *,
    mask_output_dir: Path,
    write_mask: bool = False,
    cache_path: Path | None = None,
    jobs: int = 8,
    force: bool = False,
) -> dict[str, SampleMeasurement]:
    """全画像を走査する。``file_uid`` をキーに返す。

    キャッシュがあれば走査済みを飛ばす（全件で10万枚規模のマスクを読むので、
    途中で落ちたときに最初からやり直さないため）。``write_mask`` のときは
    PNG も書くので、キャッシュに当たった分は書き直さない —— ``--force`` を使う。
    """
    done: dict[str, SampleMeasurement] = {}
    if cache_path is not None and not force and cache_path.exists():
        done = _load_cache(cache_path)
        logger.info("計測キャッシュを読んだ: %d 件 (%s)", len(done), cache_path)
    if force and cache_path is not None:
        cache_path.unlink(missing_ok=True)

    pending = [g for g in groups if g.file_uid not in done]
    if not pending:
        return done

    logger.info(
        "導出値の走査開始: %d 画像（済 %d / 全 %d）jobs=%d",
        len(pending),
        len(done),
        len(groups),
        jobs,
    )
    # OpenCV の内部スレッドと jobs が掛け算になるのを防ぐ（core/cpu.py 参照）。
    limit_native_threads(jobs)
    progress = Progress(len(pending), label="lpdata導出値")

    def work(group: FileGroup) -> SampleMeasurement:
        return measure_group(
            group, config, mask_output_dir=mask_output_dir, write_mask=write_mask
        )

    handle = None
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        handle = cache_path.open("a", encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
            for measurement in pool.map(work, pending):
                done[measurement.file_uid] = measurement
                if handle is not None:
                    handle.write(
                        json.dumps(asdict(measurement), ensure_ascii=False) + "\n"
                    )
                progress.advance()
    finally:
        if handle is not None:
            handle.close()
    progress.finish()
    return done


def _load_cache(path: Path) -> dict[str, SampleMeasurement]:
    """JSONL キャッシュを読む。壊れた行は捨てる（追記中に落ちた最終行）。"""
    rows: dict[str, SampleMeasurement] = {}
    fields = {f for f in SampleMeasurement.__dataclass_fields__}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        known = {k: v for k, v in raw.items() if k in fields}
        try:
            measurement = SampleMeasurement(**known)
        except TypeError:
            continue
        rows[measurement.file_uid] = measurement
    return rows


def empty_measurements(groups: Iterable[FileGroup]) -> dict[str, SampleMeasurement]:
    """``--no-measure`` 用。画素を読まず、導出値をすべて None のままにする。"""
    return {
        group.file_uid: SampleMeasurement(
            sample_id=sample_id_for(group), file_uid=group.file_uid
        )
        for group in groups
    }
