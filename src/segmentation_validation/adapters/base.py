"""データセットアダプタの契約。

アダプタは元JSONを ``AnnotationRecord`` / ``FileGroup`` へ変換し、
そのデータセットで何が利用可能か（``Capability``）を申告する。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from ..core.records import AnnotationRecord, FileGroup


class Capability(StrEnum):
    """データセットが提供できる情報。

    チェックは必要な capability が無いとき ``pass`` ではなく
    ``cannot_determine`` を返す。「問題なし」と「未検査」を混同させないため。
    """

    DICOM = "has_dicom"
    SPACING = "has_spacing"
    ORIGINAL_MASK = "has_original_mask"
    LUNG_REFERENCE = "has_lung_reference"
    THORAX_REFERENCE = "has_thorax_reference"
    MEDIASTINUM_REFERENCE = "has_mediastinum_reference"


@runtime_checkable
class DatasetAdapter(Protocol):
    """元JSON1ファイルぶんを共通レコードへ変換する。"""

    dataset_id: str
    source_path: Path

    def iter_files(self) -> Iterator[FileGroup]:
        """画像1枚ぶんのまとまりを順に返す。走査・計測の単位。"""
        ...

    def iter_annotations(self) -> Iterator[AnnotationRecord]:
        """geometry annotation を順に返す。"""
        ...

    def resolve_image_path(self, raw: str) -> Path: ...

    def resolve_mask_path(self, raw: str) -> Path: ...

    def resolve_original_mask_path(self, raw: str) -> Path: ...

    def reference_mask_path(self, image_path_raw: str, kind: str) -> Path | None:
        """胸郭系の参照マスクのパス。導けなければ None。"""
        ...

    def capabilities(self) -> frozenset[Capability]:
        """このデータセットで利用可能な情報。"""
        ...

    def path_invariants(self) -> dict[str, str]:
        """``M06_PATH_FORMAT`` が検査する、この形式固有のパス規約。

        規約自体がデータセット固有なので、チェック側ではなくアダプタが持つ。
        """
        ...
