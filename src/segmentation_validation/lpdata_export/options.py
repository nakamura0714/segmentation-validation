"""エクスポートの入力と結果。

CLI と Notebook の**共通の入口**。CLI はここを組み立てるだけにし、判断も処理も
持たない（Notebook が同じことを再実装しないで済むようにするため）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: ``image_file`` の作り方。
#: ``convert``  … DICOM を 16bit PNG へ変換し、生成した実ファイルを参照する
#: ``planned``  … 変換せず出力予定パスだけを記録する（下見用）
#: ``none``     … ``image_file: null``
IMAGE_MODES = ("convert", "planned", "none")

#: ``pneumothorax_mask`` の作り方。
#: ``generate`` … 気胸マスクを OR 合成した PNG を書き出す
#: ``planned``  … 書き出さず出力予定パスだけを記録する
#: ``none``     … ``pixel_array: null``
MASK_MODES = ("generate", "planned", "none")

#: DICOM が実在しないときの扱い。空画像は**どちらでも作らない**。
ON_MISSING_DICOM = ("skip", "error")

PATH_STYLES = ("relative", "absolute")

#: ラベルの作り方。
#: ``annotations``        … 既存 annotation と明示的な正常情報だけ（既定・従来の挙動）
#: ``annotations+report`` … 上に構造化読影レポートの ``report_labels`` を重ねる
LABEL_SOURCE_ANNOTATIONS = "annotations"
LABEL_SOURCE_REPORT = "annotations+report"
LABEL_SOURCES = (LABEL_SOURCE_ANNOTATIONS, LABEL_SOURCE_REPORT)


@dataclass(frozen=True)
class ExportOptions:
    """1回のエクスポートの入力一式。"""

    merged_path: Path
    template_path: Path
    out_path: Path
    image_output_dir: Path
    mask_output_dir: Path

    # --- meta の必須項目。テンプレートのダミー値を上書きする ---
    dataset_name: str | None = None
    dataset_id: str | None = None
    owner: str | None = None
    #: ``meta.description``。未指定ならテンプレートの文言を引き継ぐが、
    #: あちらは ``split: train`` 前提の文（「（train split）」）なので、
    #: ``split`` を書かない本エクスポータではそのままだと矛盾する。
    #: ``_split_agnostic_description`` が既定で差し替える。
    description: str | None = None

    # --- モード ---
    image_mode: str = "convert"
    mask_mode: str = "planned"
    on_missing_dicom: str = "skip"
    path_style: str = "relative"
    #: False なら画素を一切読まず、導出値をすべて null にする（下見用）。
    measure: bool = True

    # --- ラベルの作り方 ---
    #: ``annotations``（既定・従来の挙動）か ``annotations+report``。
    label_source: str = LABEL_SOURCE_ANNOTATIONS
    #: 取り込む ``report_labels.pneumothorax_status``。空なら絞り込まない。
    #: ここに無い status の study はサンプルを1件も出さない（除外分も
    #: ``<prefix>report_labels.csv`` には残るので後から層別分析できる）。
    report_statuses: tuple[str, ...] = ()

    # --- 実行制御 ---
    jobs: int = 8
    limit: int | None = None
    only_datasets: tuple[str, ...] = ()
    force: bool = False
    #: summary / manifest / 計測キャッシュのファイル名に付ける接頭辞。
    #: 同じディレクトリへ2回書き出すとき（development と OFC）に潰し合わないため。
    artifact_prefix: str = ""

    def __post_init__(self) -> None:
        for value, allowed, name in (
            (self.image_mode, IMAGE_MODES, "image_mode"),
            (self.mask_mode, MASK_MODES, "mask_mode"),
            (self.on_missing_dicom, ON_MISSING_DICOM, "on_missing_dicom"),
            (self.path_style, PATH_STYLES, "path_style"),
            (self.label_source, LABEL_SOURCES, "label_source"),
        ):
            if value not in allowed:
                raise ValueError(f"{name} は {allowed} のいずれか: {value!r}")
        if self.jobs < 1:
            raise ValueError(f"jobs は1以上: {self.jobs}")

    @property
    def measures_pixels(self) -> bool:
        """マスクの画素を読む必要があるか。

        ``mask_mode == "generate"`` は書き出しのために必ず読む。
        """
        return self.measure or self.mask_mode == "generate"


@dataclass
class ExportResult:
    """エクスポート1回の結果。summary と Notebook の集計はここから作る。"""

    out_path: Path
    samples: int = 0
    #: ラベル分布 ``{"present": n, ...}``
    status_counts: dict[str, int] = field(default_factory=dict)
    #: ``(abnormal_finding_status, pneumothorax_case)`` のクロス集計。
    status_case_counts: dict[str, int] = field(default_factory=dict)
    #: 属性ごとの「値が無い」件数（``null`` または ``[]``）。
    null_counts: dict[str, int] = field(default_factory=dict)
    #: 出力に現れた所見語彙とその件数。学習側と突き合わせるために出す。
    finding_vocabulary: dict[str, int] = field(default_factory=dict)
    #: 不変条件違反（``invariants.InvariantViolation`` の文字列表現）。
    violations: list[str] = field(default_factory=list)
    #: 画像変換の内訳 ``{"converted": n, "reused": n, "missing_dicom": n, ...}``
    image_counts: dict[str, int] = field(default_factory=dict)
    #: マスク生成の内訳。
    mask_counts: dict[str, int] = field(default_factory=dict)
    #: 後日の読影レポートCSV更新で補正すべき候補。
    followups: dict[str, Any] = field(default_factory=dict)
    #: 出力した補助ファイル（summary / manifest / provenance）。
    artifacts: dict[str, Path] = field(default_factory=dict)

    # --- 読影レポート由来（``label_source == "annotations+report"`` のときだけ） ---
    #: 取り込み範囲外として落とした画像数（status 別）。
    out_of_scope_counts: dict[str, int] = field(default_factory=dict)
    #: レポートで実際に変わった値の内訳 ``{"pneumothorax_case: False -> True": n}``。
    report_changes: dict[str, int] = field(default_factory=dict)
    #: ``pneumothorax_case`` の根拠の分布（``CaseEvidence`` 別）。
    case_evidence_counts: dict[str, int] = field(default_factory=dict)
    #: **明示的な気胸陰性**のサンプルID（根拠別）。統合がこれを読んで、
    #: 読影レポートによる ``false`` → ``true`` の昇格から守る。
    explicit_negative_ids: dict[str, list[str]] = field(default_factory=dict)
    #: レポートの ``pneumothorax_certainty_max`` の分布。
    #: **判定には使っていない**（層別評価のための記録）。
    certainty_counts: dict[str, int] = field(default_factory=dict)
