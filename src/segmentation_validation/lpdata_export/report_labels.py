"""構造化読影レポート由来ラベルの取り込み規則。純関数。ファイルI/Oはしない。

``labels.build_labels`` が既存 annotation から作った初期値の**上に載せる**。
置き換えではなく**補強**で、下げる方向には一切動かない。

## 優先順位

**明示的な陰性/陽性GT > 読影レポート > annotation/mask が無いだけの未確認状態**

``pneumothorax_case: false`` には2種類ある。

- **陰性だと分かっている**（``EXPLICIT_NEGATIVE_NORMAL`` /
  ``EXPLICIT_NEGATIVE_REPORT``）→ レポートで覆さない
- **確認できていない**（``UNCONFIRMED``）→ レポートが ``present`` なら ``true`` へ補完

従来の exporter は「気胸 annotation / mask が無い」だけで ``false`` にしていたので
両者が混ざっていた。区別しないまま補完すると確認済みの陰性を壊し、区別しないまま
補完を諦めると未確認の陽性を陰性の教師信号にする。

## 「明示的な陰性根拠」と認めるもの（この2つだけ）

1. annotation 側に ``normal_evidence`` の明示ラベルがある（既定
   ``No Findings/normal``）
2. **レポートが存在し、気胸を含めて陽性の異常所見が無いことを確認できる**
   （``pneumothorax_status == "absent"`` かつ観測された陽性所見が0件）

## 陰性根拠と認めないもの

- 気胸 annotation / mask が無いこと**だけ**
- **他の所見がアノテーション済みであること** —— その画像について気胸を否定したのでは
  なく、単に付けていないだけ。気胸については何も述べていない
- レポート未取得 / 判定不能 / ``unknown`` / ヘッジされた否定
- データセット名（``*_abnormal_non_pneumothorax`` 等）—— 従来どおり根拠にしない

## certainty は使わない

``definite`` / ``probable`` / ``possible`` / ``unlikely`` は**層別評価・分析のための
メタ情報**であって学習GTではない。``pneumothorax_status`` が ``present`` なら
``unlikely`` でも ``pneumothorax_case: true`` にする。上流 ``ofuna_chuo_report`` の
``labels/rules.py`` も「規則と certainty は分離されている」と明記しており、
それに揃える。

**このモジュールは ``ReportLabels`` の ``*_certainty_*`` を一切参照しない。**
参照しているのは ``pneumothorax_status`` / ``pneumothorax_side`` /
``bulla_bleb_status`` / ``pleural_effusion_status`` / ``observed_finding_count`` /
``has_report`` の6つだけ。certainty の保持は ``report.py`` の分析用CSVの仕事。

## 水気胸は気胸に含む（2026-09-24 確定）

上流は水気胸を ``pneumothorax_status: present`` +
``pneumothorax_subtype: hydropneumothorax`` として返す。このモジュールは
``pneumothorax_status`` しか見ないので、**水気胸はそのまま
``pneumothorax_case: true`` になる**。subtype による除外はしない。
分けたくなったら subtype は分析用CSVに残っているのでそこから引ける。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable

from ..core.records import ReportLabels
from .labels import ABSENT, PRESENT, UNKNOWN, CaseEvidence, SampleLabels

#: 取り込む ``pneumothorax_status``。空なら絞り込まない。
DEFAULT_STATUSES: tuple[str, ...] = (PRESENT, ABSENT)

#: 対応する ``report_labels`` のブロック形式版。上流が上げたら追随するまで止める
#: （``template.validate_coverage`` と同じ「黙って古い解釈を続けない」思想）。
#:
#: 2（rules r3 / 2026-09-24）で胸水の専用キー群が入った。**1 は受け付けない** ——
#: 版 1 のファイルには ``pleural_effusion_status`` が無く、黙って全サンプル
#: ``unknown`` の胸水フラグを出すことになるため。
SUPPORTED_SCHEMA_VERSION = 2

#: テンプレートが ``pneumothorax_side`` に許す値。患者から見た解剖学的左右。
SIDES = frozenset({"left", "right", "bilateral"})


class ReportLabelError(RuntimeError):
    """``report_labels`` の形式が想定と違う。"""


@dataclass(frozen=True)
class LabelChange:
    """レポートで実際に変わった値1件。"""

    field: str
    before: Any
    after: Any
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: {self.before!r} -> {self.after!r} ({self.reason})"


@dataclass(frozen=True)
class LabelConflict:
    """レポートと annotation が食い違い、**annotation を優先した**1件。"""

    field: str
    annotation: Any
    report: Any
    kept: Any
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.field}: annotation={self.annotation!r} report={self.report!r} "
            f"→ {self.kept!r} を維持 ({self.reason})"
        )


@dataclass(frozen=True)
class EnrichResult:
    """補強の結果。``changes`` と ``conflicts`` は summary と裁定一覧に出す。"""

    labels: SampleLabels
    changes: tuple[LabelChange, ...] = ()
    conflicts: tuple[LabelConflict, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)


# --------------------------------------------------------------- 検査・絞り込み


def check_schema(report: ReportLabels | None) -> None:
    """``schema_version`` が対応済みかを検査する。

    上流が形式を変えたら黙って古い解釈を続けず落とす。
    """
    if report is None or report.schema_version is None:
        return
    if report.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ReportLabelError(
            f"report_labels の schema_version が非対応: "
            f"{report.schema_version}（対応は {SUPPORTED_SCHEMA_VERSION}）。"
            "上流 ofuna_chuo_report の変更に追随してから再実行する"
        )


def in_scope(
    report: ReportLabels | None,
    statuses: Iterable[str] = DEFAULT_STATUSES,
) -> bool:
    """この study を取り込み対象にするか。

    ``statuses`` が空なら絞り込まない（既存の単一入力経路と同じ挙動）。
    ``report_labels`` を持たない group は、レポート由来の判定ができないので
    絞り込みが有効なときは対象外にする。
    """
    allowed = frozenset(statuses)
    if not allowed:
        return True
    if report is None:
        return False
    return report.pneumothorax_status in allowed


# ------------------------------------------------------------------- 補強本体


def _effusion_conflict_reason(kept: str) -> str:
    """``pleural_effusion_status`` の食い違いの理由。**維持した側の根拠で決まる。**

    ``pneumothorax_case`` の裁定（``_enrich_case``）と同じ語彙にそろえる。

    - ``absent`` を維持した → ``normal_evidence`` の明示正常がレポートに勝った
    - ``present`` を維持した → 下げる方向には動かさない
    """
    return "explicit_negative_wins" if kept == ABSENT else "never_downgrade"


def _enrich_case(
    base: SampleLabels, report: ReportLabels
) -> tuple[bool, CaseEvidence, list[LabelChange], list[LabelConflict]]:
    """``pneumothorax_case`` と ``case_evidence`` を決める。

    **certainty を見ない。** ``pneumothorax_status`` だけで判断する。
    """
    changes: list[LabelChange] = []
    conflicts: list[LabelConflict] = []
    status = report.pneumothorax_status
    evidence = base.case_evidence

    # --- 気胸 annotation / mask がある。レポートでは覆さない ---
    if base.pneumothorax_case:
        if status == ABSENT:
            conflicts.append(
                LabelConflict(
                    "pneumothorax_case", True, status, True, "never_downgrade"
                )
            )
        return True, evidence, changes, conflicts

    # --- 明示的な陰性根拠がある false。レポートでは覆さない ---
    if evidence is CaseEvidence.EXPLICIT_NEGATIVE_NORMAL:
        if status == PRESENT:
            conflicts.append(
                LabelConflict(
                    "pneumothorax_case", False, status, False, "explicit_negative_wins"
                )
            )
        return False, evidence, changes, conflicts

    # --- 未確認の false。ここだけレポートで動かせる ---
    if status == PRESENT:
        changes.append(LabelChange("pneumothorax_case", False, True, "report_positive"))
        return True, CaseEvidence.REPORT_POSITIVE, changes, conflicts

    # レポートが「気胸なし」かつ陽性所見が1件も無い ＝ 明示的な陰性根拠。
    # 値は false のままで根拠だけ格上げする（学習ラベルは変わらない）。
    if status == ABSENT and report.has_report and report.observed_finding_count == 0:
        changes.append(
            LabelChange(
                "case_evidence",
                evidence.value,
                CaseEvidence.EXPLICIT_NEGATIVE_REPORT.value,
                "report_confirmed_negative",
            )
        )
        return False, CaseEvidence.EXPLICIT_NEGATIVE_REPORT, changes, conflicts

    # レポートが気胸陰性でも他の所見があるなら、気胸について確認できたとは言えない
    # （所見の取りこぼしと区別できない）。未確認のまま据え置く。
    return False, evidence, changes, conflicts


def enrich_labels(base: SampleLabels, report: ReportLabels | None) -> EnrichResult:
    """既存 annotation 由来のラベルを読影レポートで補強する。

    ``report`` が ``None`` のとき、および ``pneumothorax_status`` が ``unknown``
    のときは**何も変えない**（``unknown`` を陰性に変換しない）。
    """
    if report is None:
        return EnrichResult(labels=base)
    check_schema(report)

    labels = base
    changes: list[LabelChange] = []
    conflicts: list[LabelConflict] = []

    # --- pneumothorax_case（certainty は見ない） ---
    case, evidence, case_changes, case_conflicts = _enrich_case(base, report)
    changes.extend(case_changes)
    conflicts.extend(case_conflicts)
    labels = labels.with_case(pneumothorax_case=case, case_evidence=evidence)

    # --- pneumothorax_side ---
    # **補強後の case が true のときだけ**入れる。こうしておくと
    # invariants の「side は気胸症例のときだけ」が構造的に破れない。
    side = report.pneumothorax_side
    if side is not None and side not in SIDES:
        raise ReportLabelError(
            f"pneumothorax_side が想定外の値: {side!r}（許すのは {sorted(SIDES)}）"
        )
    if side is not None and case:
        if labels.pneumothorax_side is None:
            changes.append(LabelChange("pneumothorax_side", None, side, "report_side"))
            labels = replace(labels, pneumothorax_side=side)
        elif labels.pneumothorax_side != side:
            conflicts.append(
                LabelConflict(
                    "pneumothorax_side",
                    labels.pneumothorax_side,
                    side,
                    labels.pneumothorax_side,
                    "side_already_set",
                )
            )

    # --- bulla_bleb_status ---
    # unknown からだけ上げる。present → absent の降格はしない。
    bulla = report.bulla_bleb_status
    if bulla in (PRESENT, ABSENT):
        if labels.bulla_bleb_status == UNKNOWN:
            changes.append(
                LabelChange("bulla_bleb_status", UNKNOWN, bulla, "report_bulla_bleb")
            )
            labels = replace(labels, bulla_bleb_status=bulla)
        elif labels.bulla_bleb_status != bulla:
            conflicts.append(
                LabelConflict(
                    "bulla_bleb_status",
                    labels.bulla_bleb_status,
                    bulla,
                    labels.bulla_bleb_status,
                    "never_downgrade",
                )
            )

    # --- pleural_effusion_status ---
    # 上流 schema 2 から ``present`` / ``absent`` / ``unknown`` を返す。``absent`` は
    # レポートが明示的に「胸水なし」と書いたもの（``structured_negative``）だけで、
    # 記載が無いだけの ``no_mention`` は ``unknown`` のまま来る。
    # **記載が無いことを陰性に読み替えるのは上流でもここでもしていない。**
    # 動かせるのは unknown からの1方向だけ。確定した値は present / absent とも維持する
    # （annotation の明示正常による ``absent`` はレポートの ``present`` に勝つ）。
    effusion = report.pleural_effusion_status
    if effusion in (PRESENT, ABSENT):
        if labels.pleural_effusion_status == UNKNOWN:
            changes.append(
                LabelChange(
                    "pleural_effusion_status",
                    UNKNOWN,
                    effusion,
                    "report_pleural_effusion",
                )
            )
            labels = replace(labels, pleural_effusion_status=effusion)
        elif labels.pleural_effusion_status != effusion:
            conflicts.append(
                LabelConflict(
                    "pleural_effusion_status",
                    labels.pleural_effusion_status,
                    effusion,
                    labels.pleural_effusion_status,
                    _effusion_conflict_reason(labels.pleural_effusion_status),
                )
            )

    # --- abnormal_finding_status は触らない ---
    # 上流が policy 未確定として常に unknown を返すので、写すと annotation 由来の
    # 判定（present / absent）を unknown で潰すことになる。
    # finding_labels_observed からの導出もしない（語彙が統制されていない。
    # ただし胸水の1語だけは例外的に拾っている —— adapters.engineer_set 参照）。

    return EnrichResult(
        labels=labels, changes=tuple(changes), conflicts=tuple(conflicts)
    )
