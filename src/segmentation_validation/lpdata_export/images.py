"""DICOM → 16bit PNG 変換のドライバ。

変換そのものは ``core.imageio.convert_dicom_to_png``（med-chest-metry-pi6 の
``dataprep/dicom_to_png.py`` からの移植）。ここは「どれを変換するか・既にあるものを
どう扱うか・無いものをどうするか」だけを持つ。

**DICOM が無いときに空画像を作らない。** 全ゼロのPNGを置くと、学習側から見て
「真っ黒な胸部X線」という正当な入力に見えてしまい、どのバリデーションにも掛からない。
``skip``（``image_file: null`` でサンプルは出す）か ``error``（停止）のどちらかにする。

**規模に注意**: 全 42,574 枚で uint16 生データ 251 GiB を NFS 越しに読み、PNG を
約 113 GiB 書く。既存出力を skip する冪等実装と ``--limit`` / ``--only-dataset`` での
部分実行が前提。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..core.cpu import limit_native_threads
from ..core.imageio import convert_dicom_to_png
from ..core.progress import Progress
from ..core.records import FileGroup
from .paths import image_path_for, sample_id_for

logger = logging.getLogger(__name__)

#: 変換の結果。summary にそのまま内訳として出す。
CONVERTED = "converted"
REUSED = "reused"
PLANNED = "planned"
MISSING_DICOM = "missing_dicom"
FAILED = "failed"
DISABLED = "none"


class MissingDicomError(FileNotFoundError):
    """``--on-missing-dicom error`` で DICOM が見つからなかった。"""


@dataclass
class ImageResult:
    """1画像ぶんの変換結果。"""

    sample_id: str
    file_uid: str
    status: str
    #: ``image_file`` に書くパス。実体が無い / 作らない場合は None。
    png_path: Path | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (CONVERTED, REUSED, PLANNED)


def prepare_images(
    groups: Sequence[FileGroup],
    *,
    image_output_dir: Path,
    mode: str = "convert",
    on_missing_dicom: str = "skip",
    jobs: int = 8,
    force: bool = False,
    provenance_path: Path | None = None,
) -> dict[str, ImageResult]:
    """``image_file`` を用意する。``file_uid`` をキーに結果を返す。

    ``mode``:

    - ``convert`` … DICOM を読んで PNG を書き、生成した実ファイルを参照する
    - ``planned`` … 変換せず出力予定パスだけを返す（下見用。実体は無い）
    - ``none``    … ``image_file: null``
    """
    if mode == DISABLED:
        return {
            g.file_uid: ImageResult(sample_id_for(g), g.file_uid, DISABLED)
            for g in groups
        }
    if mode == PLANNED:
        return {
            g.file_uid: ImageResult(
                sample_id_for(g),
                g.file_uid,
                PLANNED,
                png_path=image_path_for(image_output_dir, sample_id_for(g)),
            )
            for g in groups
        }

    results: dict[str, ImageResult] = {}
    todo: list[FileGroup] = []
    for group in groups:
        sample_id = sample_id_for(group)
        png_path = image_path_for(image_output_dir, sample_id)
        if not group.resolved_image_path.exists():
            if on_missing_dicom == "error":
                raise MissingDicomError(
                    f"DICOM が存在しない: {group.resolved_image_path}"
                    f"（sample_id={sample_id}）"
                )
            results[group.file_uid] = ImageResult(
                sample_id,
                group.file_uid,
                MISSING_DICOM,
                error=str(group.resolved_image_path),
            )
            continue
        if png_path.exists() and not force:
            results[group.file_uid] = ImageResult(
                sample_id, group.file_uid, REUSED, png_path=png_path
            )
            continue
        todo.append(group)

    if not todo:
        return results

    logger.info(
        "DICOM→16bit PNG 変換: %d 枚（既存 %d / 全 %d）jobs=%d",
        len(todo),
        sum(1 for r in results.values() if r.status == REUSED),
        len(groups),
        jobs,
    )
    limit_native_threads(jobs)
    progress = Progress(len(todo), label="画像変換")

    def work(group: FileGroup) -> tuple[ImageResult, dict[str, Any] | None]:
        sample_id = sample_id_for(group)
        png_path = image_path_for(image_output_dir, sample_id)
        try:
            provenance = convert_dicom_to_png(group.resolved_image_path, png_path)
        except Exception as error:  # 1枚壊れていても残りは変換したい
            return (
                ImageResult(
                    sample_id,
                    group.file_uid,
                    FAILED,
                    error=f"{type(error).__name__}: {error}",
                ),
                None,
            )
        return (
            ImageResult(sample_id, group.file_uid, CONVERTED, png_path=png_path),
            provenance | {"sample_id": sample_id},
        )

    handle = None
    if provenance_path is not None:
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        handle = provenance_path.open("a", encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=max(jobs, 1)) as pool:
            for result, provenance in pool.map(work, todo):
                results[result.file_uid] = result
                if handle is not None and provenance is not None:
                    handle.write(json.dumps(provenance, ensure_ascii=False) + "\n")
                progress.advance()
    finally:
        if handle is not None:
            handle.close()
    progress.finish()

    failed = [r for r in results.values() if r.status == FAILED]
    if failed:
        logger.warning(
            "変換に失敗した画像が %d 枚ある（image_file は null）", len(failed)
        )
        for result in failed[:10]:
            logger.warning("  - %s: %s", result.sample_id, result.error)
    return results
