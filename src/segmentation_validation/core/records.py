"""検証が扱う共通レコード。

元JSONの階層構造を知っているのは ``adapters/`` だけで、チェック側はここで定義する
``AnnotationRecord`` / ``FileGroup`` しか見ない。別形式のデータセットが増えても
アダプタを1つ足せば既存のチェックがそのまま動く。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .labels import Label, label_keys

#: engineer-set の元JSONファイル名の末尾（例:
#: ``engineer-set-PTE_CX_MT_PI3_pneumothorax-20260904_061537.json``）に
#: 埋め込まれた生成日時。
_SOURCE_TIMESTAMP_RE = re.compile(r"(\d{8}_\d{6})\.json$")


def source_generated_at(source_json: str) -> datetime | None:
    """元データセットJSONの生成日時。ファイル名から取れなければ None。

    ``checks/duplicate/d05_cross_dataset_duplicate.py``（クロスデータセット
    重複の代表選び）と ``selection/build_dataset.py``（development.json
    生成時の画像重複解消）の両方から使う共有ユーティリティ。「どちらの
    生成日時を残すか」の向き（古い方/新しい方）は呼び出し側の判断に委ねる。
    """
    match = _SOURCE_TIMESTAMP_RE.search(source_json)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


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
    def stable_file_uid(self) -> str:
        """再エクスポートを跨いでも変わらない画像の識別子。

        ``file_uid`` は ``source_json``（``engineer-set-...-20260624_080014.json``
        のように生成日時が埋まったファイル名そのもの）を含むため、元JSONを
        再エクスポートするだけで全画像ぶんが別物になる。``dataset_id`` は
        ``adapters/engineer_set.py::dataset_id_for`` が末尾の日時スタンプを
        落とすので安定している。人間の目視判定のように fingerprint を跨いで
        引き継ぎたいものは必ずこちらを使う。

        走査中のグループ化には引き続き ``file_uid`` を使う。同一 fingerprint
        内では両者とも一意で、既存の突合を変えない方が安全なため。
        """
        return (
            f"{self.dataset_id}::{self.institution}"
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
class ReportLabels:
    """構造化読影レポート由来の study レベルラベル（``study.report_labels``）。

    元の25キーのうち、**判定に使うもの**と**分析用に保持するもの**だけを写す。
    生の dict を持たないのは、``FileGroup`` を hashable に保つためと、
    ``finding_labels_observed``（語彙が統制されておらず、37,458通り・生の日本語
    自由文を含む）に下流から手が届かないようにするため。

    ⚠️ **``*_certainty_*`` を判定条件に使わないこと。**
    ``definite`` / ``probable`` / ``possible`` / ``unlikely`` は層別評価・分析の
    ためのメタ情報であって学習GTではない。``pneumothorax_case`` は
    ``pneumothorax_status`` だけで決まる（上流 ``ofuna_chuo_report`` の
    ``labels/rules.py`` も「規則と certainty は分離されている」と明記している）。
    ここに保持するのは、後から certainty 別に抽出・性能評価できるようにするため。

    ``abnormal_finding_status`` は上流が policy 未確定として常に ``unknown`` を
    返す。値は写すが**採用側は使わない**。
    """

    # --- 判定に使う ---
    pneumothorax_status: str = "unknown"
    pneumothorax_side: str | None = None
    bulla_bleb_status: str = "unknown"
    #: 胸水の有無。上流の ``report_labels`` には ``bulla_bleb_status`` のような
    #: 専用キーが**無い**ので、``finding_labels_observed`` に ``pleural_effusion``
    #: が出たかどうかだけを 2 値（``present`` / ``unknown``）で写す。
    #: ``absent`` は出さない —— 観測リストに無いのは「記載なし」と「明示的に陰性」の
    #: 両方を含み、区別できないため。上流が専用キーを持ったらそちらへ切り替える。
    pleural_effusion_status: str = "unknown"
    #: 観測された陽性所見の数。「レポートは在るが陽性所見が1つも無い」＝
    #: 明示的な陰性根拠かどうかの判定にだけ使う（所見名そのものは持たない）。
    observed_finding_count: int = 0

    # --- 分析用に保持する（判定には使わない） ---
    pneumothorax_subtype: str | None = None
    pneumothorax_evidence: str | None = None
    pneumothorax_certainty_max: str | None = None
    pneumothorax_certainty_counts: tuple[tuple[str, int], ...] = ()
    pneumothorax_absent_certainty_counts: tuple[tuple[str, int], ...] = ()
    bulla_bleb_evidence: str | None = None
    bulla_bleb_certainty_max: str | None = None
    bulla_bleb_certainty_counts: tuple[tuple[str, int], ...] = ()
    abnormal_finding_status: str = "unknown"
    needs_review: bool = False
    flags: tuple[str, ...] = ()

    # --- 出所 ---
    study_name: str | None = None
    schema_version: int | None = None
    rules_version: str | None = None
    label_source: str | None = None
    source_dataset_id: str | None = None
    source_json: str | None = None
    report_sha256: str | None = None

    @property
    def has_report(self) -> bool:
        """レポート本体が存在するか。

        上流は「レポートが無い」を ``pneumothorax_evidence == "no_report"`` で
        表す。**レポート未取得を陰性の根拠にしない**ための判定に使う。
        """
        return self.pneumothorax_evidence not in (None, "no_report")


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
    # study / series レベルの分類ラベル（geometry ではない）。
    # annotation を持たない画像が「正常例」か「単に未アノテーション」かは
    # これでしか判別できない。実測では未アノテーション187 study の全てが
    # ``No Findings/001 normal`` を持っており、意図的な陰性症例だった。
    case_labels: tuple[Label, ...] = ()
    # 同一 series 内でのこのファイルの位置（0始まり）と series 内の総ファイル数。
    # ``file_key`` の辞書順ソートで決める（adapters/engineer_set.py参照）。
    # 実データで全39件の複数ファイルseriesにおいて DICOM の InstanceNumber の
    # 順序と完全一致することを確認済み。``UNANNOTATED_VIEW``（同じseries内の
    # 他ビューはアノテーション済み）が「series内の何枚目か」を区別するために使う。
    series_image_index: int = 0
    series_image_count: int = 1
    # 構造化読影レポート由来の study レベルラベル。持たないデータセットでは None。
    # study レベルの値を配下の全ファイルに複製する（上流 ofuna_chuo_report の
    # emit.py が既に同じ複製をしており、multi_file_study / view_discordant の
    # フラグで追跡できるようにしてある）。
    report_labels: ReportLabels | None = None

    @property
    def file_uid(self) -> str:
        return (
            f"{self.source_json}::{self.institution}"
            f"/{self.study}/{self.series}/{self.file}"
        )

    @property
    def stable_file_uid(self) -> str:
        """再エクスポートを跨いでも変わらない画像の識別子。

        ``AnnotationRecord.stable_file_uid`` と同じ規則。両者は同じ画像に
        対して必ず同じ値を返す。
        """
        return (
            f"{self.dataset_id}::{self.institution}"
            f"/{self.study}/{self.series}/{self.file}"
        )

    @property
    def is_negative_case(self) -> bool:
        """アノテーションが無く、正常例として明示されているか。

        「検証すべき対象が無い」のと「アノテーション漏れ」を区別する。
        """
        if self.records:
            return False
        return any(
            label.code_system == "No Findings" or label.code_text_eng == "normal"
            for label in self.case_labels
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
