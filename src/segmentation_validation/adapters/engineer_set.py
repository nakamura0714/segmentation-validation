"""``engineer-set-*.json`` 形式のアダプタ。

この形式の構造::

    root
    ├── version_id / version_date
    └── dataset[施設][study]
        ├── patient_id / study_name / study_date
        ├── annotations[]            study レベルの分類ラベル（geometry ではない）
        └── series_list[series]
            ├── spacing / shape / manufacturer
            ├── annotations[]        series レベルの分類ラベル（geometry ではない）
            └── file_list[file]
                ├── image_path
                └── annotations[]    ★これが geometry annotation

study / series レベルの ``annotations`` は ``annotation_id`` を持つ別スキーマの
分類ラベルで、マスクを持たない。検証対象は file レベルの geometry annotation のみ。

なお ``docs/dataset_format.md`` の標準 Dataset/Prediction 形式とは別物。
本アダプタはレガシーな engineer-set 形式だけを扱う。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..config import Config
from ..core.labels import parse_labels
from ..core.paths import reference_mask_path, resolve_under
from ..core.records import AnnotationRecord, FileGroup
from .base import Capability

logger = logging.getLogger(__name__)

# エクスポートのたびに変わる末尾のタイムスタンプを落として、
# 再エクスポートでも同じデータセットだと分かる ID にする。
_EXPORT_STAMP = re.compile(r"-\d{8}_\d{6}$")
_PREFIX = "engineer-set-"

# この形式のパス規約。M06 が検査する。
IMAGE_PATH_ROOT = "medical2"
ANNOTATION_PATH_ROOT = "annotation"
IMAGE_PATH_DEPTH = 8


def dataset_id_for(path: Path) -> str:
    """JSONファイル名からデータセットIDを作る。

    ``engineer-set-ANN_EIRLPRJ_01272-20260709_004526.json`` → ``ANN_EIRLPRJ_01272``。
    """
    stem = path.stem
    if stem.startswith(_PREFIX):
        stem = stem[len(_PREFIX) :]
    return _EXPORT_STAMP.sub("", stem)


@dataclass
class EngineerSetAdapter:
    """``engineer-set-*.json`` 1ファイルを共通レコードへ変換する。"""

    source_path: Path
    config: Config
    dataset_id: str = ""
    version_id: str | None = None
    version_date: str | None = None
    _dataset: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        payload = json.loads(self.source_path.read_text(encoding="utf-8"))
        self._dataset = payload.get("dataset", {})
        self.version_id = payload.get("version_id")
        self.version_date = payload.get("version_date")
        if not self.dataset_id:
            self.dataset_id = dataset_id_for(self.source_path)

    # ------------------------------------------------------------- パス解決
    def resolve_image_path(self, raw: str) -> Path:
        return resolve_under(self.config.roots.image_root, raw)

    def resolve_mask_path(self, raw: str) -> Path:
        return resolve_under(self.config.roots.annotation_root, raw)

    def resolve_original_mask_path(self, raw: str) -> Path:
        return resolve_under(self.config.roots.annotation_root, raw)

    def reference_mask_path(self, image_path_raw: str, kind: str) -> Path | None:
        root = self.config.reference_masks.roots.get(kind)
        if root is None:
            return None
        try:
            return reference_mask_path(
                image_path_raw,
                root,
                self.config.reference_masks.drop_path_components,
            )
        except ValueError:
            return None

    # --------------------------------------------------------------- 申告
    def capabilities(self) -> frozenset[Capability]:
        """このデータセットで実際に使える情報を返す。

        参照マスクは施設・バッチ日ごとに整備状況が違うので、ここでは
        「1件でも存在するか」を返し、ファイル単位の有無は計測時に判定する。
        """
        available = {Capability.DICOM, Capability.SPACING, Capability.ORIGINAL_MASK}
        for kind, capability in (
            ("lung", Capability.LUNG_REFERENCE),
            ("thorax", Capability.THORAX_REFERENCE),
            ("mediastinum", Capability.MEDIASTINUM_REFERENCE),
        ):
            root = self.config.reference_masks.roots.get(kind)
            if root and Path(root).is_dir():
                available.add(capability)
        return frozenset(available)

    def path_invariants(self) -> dict[str, str]:
        return {
            "image_path_root": IMAGE_PATH_ROOT,
            "annotation_path_root": ANNOTATION_PATH_ROOT,
            "image_path_depth": str(IMAGE_PATH_DEPTH),
            # マスクのファイル名は geometry_uid と一致する（1665/1665 で成立）。
            # 破れていればアノテーションの紐付け間違いを意味する。
            "mask_stem_equals": "geometry_uid",
            "image_stem_equals": "file_id",
        }

    # --------------------------------------------------------------- 走査
    def iter_files(self) -> Iterator[FileGroup]:
        source_json = self.source_path.name
        for institution, studies in self._dataset.items():
            for study_key, study in studies.items():
                study_labels = _case_labels(study)
                for series_key, series in study.get("series_list", {}).items():
                    case_labels = study_labels + _case_labels(series)
                    shape = _shape_of(series)
                    spacing = _spacing_of(series)
                    manufacturer = series.get("manufacturer")
                    for file_key, file_rec in series.get("file_list", {}).items():
                        image_path = file_rec.get("image_path") or ""
                        common = {
                            "dataset_id": self.dataset_id,
                            "source_json": source_json,
                            "institution": institution,
                            "study": study_key,
                            "series": series_key,
                            "file": file_key,
                            "patient_id": study.get("patient_id") or "",
                            "study_date": str(study.get("study_date") or ""),
                            "image_path": image_path,
                            "resolved_image_path": self.resolve_image_path(image_path),
                            "series_shape": shape,
                            "spacing": spacing,
                            "manufacturer": manufacturer,
                        }
                        records = tuple(
                            self._build_record(common, index, annotation)
                            for index, annotation in enumerate(
                                file_rec.get("annotations") or []
                            )
                        )
                        yield FileGroup(
                            **common, records=records, case_labels=case_labels
                        )

    def iter_annotations(self) -> Iterator[AnnotationRecord]:
        for group in self.iter_files():
            yield from group.records

    def count_non_geometry_annotations(self) -> int:
        """study / series レベルの分類ラベル数。

        検証対象ではないが、「対象外がいくつあったか」を summary に出すために数える。
        """
        total = 0
        for studies in self._dataset.values():
            for study in studies.values():
                total += len(study.get("annotations") or [])
                for series in study.get("series_list", {}).values():
                    total += len(series.get("annotations") or [])
        return total

    # -------------------------------------------------------------- 内部
    def _build_record(
        self, common: dict[str, Any], index: int, annotation: dict[str, Any]
    ) -> AnnotationRecord:
        path_mask = annotation.get("path_mask")
        path_original = annotation.get("path_original_mask")
        width, height = annotation.get("width"), annotation.get("height")
        return AnnotationRecord(
            **common,
            annotation_type=annotation.get("annotation_type") or "",
            geometry_id=int(annotation["geometry_id"]),
            geometry_uid=str(annotation["geometry_uid"]),
            path_mask=path_mask,
            resolved_path_mask=(
                self.resolve_mask_path(path_mask) if path_mask else None
            ),
            path_original_mask=path_original,
            resolved_path_original_mask=(
                self.resolve_original_mask_path(path_original)
                if path_original
                else None
            ),
            json_bbox=(
                _as_int(annotation.get("min_x")),
                _as_int(annotation.get("min_y")),
                _as_int(annotation.get("max_x")),
                _as_int(annotation.get("max_y")),
            ),
            region_count=_optional_int(annotation.get("region_count")),
            declared_size=(
                (int(width), int(height))
                if width is not None and height is not None
                else None
            ),
            is_latest=_as_int(annotation.get("is_latest")),
            version=_as_int(annotation.get("version")),
            user=annotation.get("user"),
            timestamp=annotation.get("timestamp"),
            annotation_request=_optional_int(annotation.get("annotation_request")),
            group_id=annotation.get("group_id"),
            comment=annotation.get("comment"),
            labels=parse_labels(annotation.get("labels") or []),
            raw_label_ids=tuple(
                int(label["label_id"])
                for label in (annotation.get("labels") or [])
                if label.get("label_id") is not None
            ),
            index=index,
        )


def open_adapters(config: Config) -> list[EngineerSetAdapter]:
    """設定で指定された全JSONのアダプタを開く。

    読めないJSONは警告して飛ばす。1本壊れていても他の検証は進められる方がよい。
    """
    adapters: list[EngineerSetAdapter] = []
    for path in config.dataset_sources():
        if not path.exists():
            logger.error("対象JSONが存在しない: %s", path)
            continue
        try:
            adapters.append(EngineerSetAdapter(source_path=path, config=config))
        except (json.JSONDecodeError, OSError, KeyError) as error:
            logger.error("対象JSONを読めない: %s (%s)", path, error)
    return adapters


# ------------------------------------------------------------------ helpers


def _case_labels(container: dict[str, Any]) -> tuple:
    """study / series レベルの分類ラベルを集める。

    これらは ``annotation_id`` を持つ別スキーマで geometry ではないが、
    「この症例は正常例か」を知る唯一の手掛かりなので捨てない。
    """
    labels: list = []
    for annotation in container.get("annotations") or []:
        labels.extend(parse_labels(annotation.get("labels") or []))
    return tuple(dict.fromkeys(labels))


def _shape_of(series: dict[str, Any]) -> tuple[int, int] | None:
    """``series.shape`` を ``(height, width)`` で返す。

    ``ndarray.shape`` と直接比較できる向きに揃える。JSON側は ``{w, h, z}``。
    """
    shape = series.get("shape") or {}
    width, height = shape.get("w"), shape.get("h")
    if width is None or height is None:
        return None
    return (int(height), int(width))


def _spacing_of(series: dict[str, Any]) -> tuple[float | None, float | None]:
    """``series.spacing`` を ``(x, y)`` で返す。null のシリーズが実在する。"""
    spacing = series.get("spacing") or {}
    x, y = spacing.get("x"), spacing.get("y")
    return (
        float(x) if x is not None else None,
        float(y) if y is not None else None,
    )


def _as_int(value: Any) -> int:
    return 0 if value is None else int(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
