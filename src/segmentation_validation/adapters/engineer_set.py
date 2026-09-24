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

``selection/build_dataset.py`` が作る統合JSON（``development_merged.json``）も同じ形で、
**file entry にだけ ``dataset_id`` が増える**（file 単位が唯一の衝突しない階層なので、
由来はそこに持たせてある）。本アダプタは file entry の ``dataset_id`` が
あればそれを優先し、
無ければファイル名由来の ``dataset_id`` を使うので、統合JSONもそのまま読める。

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
from ..core.records import AnnotationRecord, FileGroup, ReportLabels
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

#: 構造化読影レポート由来のラベルを載せる study レベルのキー。
#: ofuna_chuo_report が名前空間キーとして足すもので、通常の engineer-set には無い。
REPORT_LABELS_KEY = "report_labels"

#: 胸水 ``present`` の根拠になりうる所見名。**上流はこの2つを胸水に畳む**
#: （血胸も胸水として扱う規則。2026-09-24 確定）。``finding_labels_observed``
#: からはこの2語だけを照合して元の所見名として残す。詳細は ``_report_labels``。
PLEURAL_EFFUSION_FINDINGS = ("pleural_effusion", "hemothorax")


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
    #: 統合JSONのときだけ中身がある（生成時刻・fingerprint・データセット別内訳）。
    #: 出力物に出所を記録するために持つ。元JSONを2回パースしないで済ませるのが目的。
    meta_development: dict[str, Any] = field(default_factory=dict, repr=False)
    _dataset: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        payload = json.loads(self.source_path.read_text(encoding="utf-8"))
        self._dataset = payload.get("dataset", {})
        self.version_id = payload.get("version_id")
        self.version_date = payload.get("version_date")
        self.meta_development = payload.get("meta_development") or {}
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
                # study 単位で1回だけ読み、配下の全ファイルで共有する。
                report_labels = _report_labels(study, study_key)
                for series_key, series in study.get("series_list", {}).items():
                    case_labels = study_labels + _case_labels(series)
                    shape = _shape_of(series)
                    spacing = _spacing_of(series)
                    manufacturer = series.get("manufacturer")
                    file_list = series.get("file_list", {})
                    # series内の並び順。file_key の辞書順ソートが DICOM の
                    # InstanceNumber 順と一致することを実データで確認済み
                    # （core/records.py の FileGroup.series_image_index 参照）。
                    # 元の走査順（JSON内の出現順）は他で使われている可能性があるので
                    # 変えず、順位だけを辞書引きで求める。
                    series_order = {
                        key: index for index, key in enumerate(sorted(file_list))
                    }
                    series_image_count = len(file_list)
                    for file_key, file_rec in file_list.items():
                        image_path = file_rec.get("image_path") or ""
                        common = {
                            # 統合JSONは file entry に由来を持つ。
                            # 無ければファイル名由来。
                            "dataset_id": file_rec.get("dataset_id") or self.dataset_id,
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
                            **common,
                            records=records,
                            case_labels=case_labels,
                            series_image_index=series_order[file_key],
                            series_image_count=series_image_count,
                            report_labels=report_labels,
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


def _counts(raw: Any) -> tuple[tuple[str, int], ...]:
    """``{"definite": 2}`` を hashable なタプルにする（キー順で安定化）。"""
    if not isinstance(raw, dict):
        return ()
    return tuple(sorted((str(k), int(v)) for k, v in raw.items()))


def _report_labels(study: dict[str, Any], study_key: str) -> ReportLabels | None:
    """study の ``report_labels`` ブロックを読む。

    **持ってこないもの**: ``finding_labels_observed`` の中身（語彙が統制されて
    いない）。陽性所見が0件かどうかだけを ``observed_finding_count`` に残す。
    「レポートは在るが陽性所見が1件も無い」は明示的な陰性根拠になるが、
    所見名そのものは判定にも出力にも使わないため。

    唯一の例外が ``PLEURAL_EFFUSION_FINDINGS`` の2語。胸水の判定そのものは上流の
    ``pleural_effusion_status``（schema_version 2 以降の専用キー）を使うが、
    上流は**血胸を胸水 present に畳む**ので、畳む前の所見名を残さないと
    「胸水なのか血胸なのか」が下流から分からなくなる。リスト全体は渡さず、
    **この2語だけ**を照合して ``pleural_effusion_findings`` に写す
    （語彙が統制されていない問題は「決め打ちの語だけを照合する」ことで回避する）。
    観測リストに載るのは ``present: true`` の所見だけである（実レポート 80 件で
    確認: 載っている 40 件は全て ``present: true``、載っていない 40 件は
    finding 自体が無いか ``present: false`` のみ）。
    """
    raw = study.get(REPORT_LABELS_KEY)
    if not isinstance(raw, dict):
        return None
    observed = raw.get("finding_labels_observed")
    observed_list = observed if isinstance(observed, list) else []
    return ReportLabels(
        pneumothorax_status=str(raw.get("pneumothorax_status") or "unknown"),
        pneumothorax_side=raw.get("pneumothorax_side"),
        bulla_bleb_status=str(raw.get("bulla_bleb_status") or "unknown"),
        pleural_effusion_status=str(raw.get("pleural_effusion_status") or "unknown"),
        pleural_effusion_evidence=raw.get("pleural_effusion_evidence"),
        pleural_effusion_certainty_max=raw.get("pleural_effusion_certainty_max"),
        pleural_effusion_flags=tuple(
            str(f) for f in (raw.get("pleural_effusion_flags") or ())
        ),
        pleural_effusion_findings=tuple(
            name for name in PLEURAL_EFFUSION_FINDINGS if name in observed_list
        ),
        observed_finding_count=len(observed) if isinstance(observed, list) else 0,
        pneumothorax_subtype=raw.get("pneumothorax_subtype"),
        pneumothorax_evidence=raw.get("pneumothorax_evidence"),
        pneumothorax_certainty_max=raw.get("pneumothorax_certainty_max"),
        pneumothorax_certainty_counts=_counts(raw.get("pneumothorax_certainty_counts")),
        pneumothorax_absent_certainty_counts=_counts(
            raw.get("pneumothorax_absent_certainty_counts")
        ),
        bulla_bleb_evidence=raw.get("bulla_bleb_evidence"),
        bulla_bleb_certainty_max=raw.get("bulla_bleb_certainty_max"),
        bulla_bleb_certainty_counts=_counts(raw.get("bulla_bleb_certainty_counts")),
        abnormal_finding_status=str(raw.get("abnormal_finding_status") or "unknown"),
        needs_review=bool(raw.get("needs_review")),
        flags=tuple(str(f) for f in (raw.get("flags") or ())),
        study_name=str(raw.get("study_name") or study_key),
        schema_version=_optional_int(raw.get("schema_version")),
        rules_version=raw.get("rules_version"),
        label_source=raw.get("label_source"),
        source_dataset_id=raw.get("source_dataset_id"),
        source_json=raw.get("source_json"),
        report_sha256=raw.get("report_sha256"),
    )


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
