"""検証が扱う共通レコード。

元JSONの階層構造を知っているのは ``adapters/`` だけで、チェック側はここで定義する
``AnnotationRecord`` / ``FileGroup`` しか見ない。別形式のデータセットが増えても
アダプタを1つ足せば既存のチェックがそのまま動く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .labels import Label, label_keys


@dataclass(frozen=True)
class AnnotationRecord:
    """geometry annotation 1件。

    主キーは ``geometry_uid``（データセット全体で一意であることを確認済み）。
    データセットを跨ぐ場合は ``(dataset_id, geometry_uid)`` で識別する。
    """

    # --- 出所 ---
    dataset_id: str
    source_json: str
    institution: str
    study: str
    series: str
    file: str
    patient_id: str
    study_date: str

    # --- 画像側 ---
    image_path: str
    resolved_image_path: Path
    series_shape: tuple[int, int] | None
    spacing: tuple[float | None, float | None]
    manufacturer: str | None

    # --- アノテーション本体 ---
    annotation_type: str
    geometry_id: int
    geometry_uid: str
    path_mask: str | None
    resolved_path_mask: Path | None
    path_original_mask: str | None
    resolved_path_original_mask: Path | None
    json_bbox: tuple[int, int, int, int]
    region_count: int | None
    declared_size: tuple[int, int] | None
    is_latest: int
    version: int
    user: str | None
    timestamp: str | None
    annotation_request: int | None
    group_id: Any | None
    comment: str | None
    labels: tuple[Label, ...]
    # 重複排除前の label_id。データセット間の provenance 確認用に残す。
    raw_label_ids: tuple[int, ...] = field(default=())
    # file_list 内での並び順。ログや図の [n] 表示に使う。
    index: int = 0

    @property
    def file_uid(self) -> str:
        """ファイルの識別子。

        ``source_json`` を必ず含める。同じ ``file_id`` が2つのJSONに現れる例が実在し、
        含めないと別ファイルのマスク同士が同じグループに入って偽の重複ペアが出る。
        """
        return (
            f"{self.source_json}::{self.institution}"
            f"/{self.study}/{self.series}/{self.file}"
        )

    @property
    def annotation_uid(self) -> str:
        """データセットを跨いでも一意になる識別子。"""
        return f"{self.dataset_id}::{self.geometry_uid}"

    @property
    def short_uid(self) -> str:
        return self.geometry_uid[:8]

    @property
    def has_mask(self) -> bool:
        return self.resolved_path_mask is not None

    @property
    def has_original_mask(self) -> bool:
        return self.resolved_path_original_mask is not None

    @property
    def label_keys(self) -> frozenset[tuple[str, str]]:
        """``(code_system, code)`` の集合。重複マスクの同一ラベル判定に使う。"""
        return label_keys(self.labels)

    @property
    def parsed_timestamp(self) -> datetime | None:
        """``timestamp`` を datetime へ。壊れていれば None。

        重複の自動採否は「新しい方を残す」なので、ここが None のペアは
        自動決定せず目視へ回す。現データでは 1817/1817 が解釈できる。
        """
        if not self.timestamp:
            return None
        try:
            return datetime.fromisoformat(str(self.timestamp))
        except ValueError:
            return None

    @property
    def label_family(self) -> str:
        """微小領域の閾値を選ぶためのクラス族。複数ラベルなら最初のものに従う。"""
        return self.labels[0].family if self.labels else "regional"

    def label_lines(self, japanese: bool = True) -> list[str]:
        return [label.display(japanese) for label in self.labels]


@dataclass(frozen=True)
class FileGroup:
    """1画像ぶんのアノテーション一式。

    走査・計測の単位。同じファイルのマスクを同時に読むので、
    ペアIoUと参照マスクとの突き合わせが追加のディスクI/Oなしで済む。
    """

    dataset_id: str
    source_json: str
    institution: str
    study: str
    series: str
    file: str
    patient_id: str
    study_date: str
    image_path: str
    resolved_image_path: Path
    series_shape: tuple[int, int] | None
    spacing: tuple[float | None, float | None]
    manufacturer: str | None
    records: tuple[AnnotationRecord, ...]

    @property
    def file_uid(self) -> str:
        return (
            f"{self.source_json}::{self.institution}"
            f"/{self.study}/{self.series}/{self.file}"
        )

    @property
    def mask_records(self) -> tuple[AnnotationRecord, ...]:
        """マスクを持つアノテーション。bbox / elliptical は含まない。"""
        return tuple(record for record in self.records if record.has_mask)

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.annotation_type] = counts.get(record.annotation_type, 0) + 1
        return counts
