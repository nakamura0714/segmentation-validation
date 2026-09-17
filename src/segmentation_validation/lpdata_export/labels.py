"""ラベル4属性を既存 annotation から作る。純関数。ファイルI/Oはしない。

**今回の初期生成は既存 annotation と明示的な正常情報だけを使う。**
**読影レポートは使わない。**
読影レポート解析結果は後日CSVで受け取り、独立した「ラベル更新処理」が
``abnormal_finding_status`` / ``finding_labels`` / ``pneumothorax_case`` /
``pneumothorax_side`` / ``bulla_bleb_status`` を上書きする想定。ここはその初期値を作る。

安全側の原則（これを崩すと未アノテーション陽性を陰性の教師信号にしてしまう）:

- **annotation が無いことだけを理由に ``absent`` にしない**
- **マスクが無いことだけを理由に正常とみなさない**
- **データセット名を根拠にしない**
  （``ChestMetry_PI6px_normal`` でも明示ラベルは142件だけ）
- 異常所見ではない annotation（``Difficulty`` / ``Grade`` / ``Location`` /
  ``Body Parts`` /
  ``FP`` / ``Disease``）を ``present`` の根拠にしない
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Iterable

from ..core.labels import Label
from ..core.records import FileGroup

#: 気胸を表す所見名。``code`` では判定できない —— 実データで ``Findings/001`` が
#: ``nodule``（6,567件）と ``pneumothorax``（2,667件）の**両方**に使われており、
#: 気胸ラベル自体も ``Findings/001`` と ``Findings/010`` の2種類にまたがる。
#: ``code_text_eng`` はデータセットを跨いで表記が統一されている唯一の手掛かり。
PNEUMOTHORAX = "pneumothorax"

#: ブラ / ブレブの所見名。元データは1語に結合されている。
BULLA_BLEB = "bulla_bleb"

PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"


class CaseEvidence(StrEnum):
    """``pneumothorax_case`` の値が**何を根拠にしているか**。

    ``false`` には「陰性だと分かっている」と「確認できていない」の2種類があり、
    従来はどちらも同じ ``false`` に潰れていた。**読影レポートで補完してよいのは
    後者だけ**なので、両者を区別する。

    優先順位は **明示的な陰性/陽性GT > レポート > 未確認**。

    ⚠️ **出力属性ではない。** テンプレートに無い属性を足すと
    ``template.validate_coverage`` が落ちる（それが意図した歯止め）。
    分析用CSVと統合の裁定一覧にだけ出す。
    """

    #: 気胸 annotation / mask がある。**レポートで覆さない。**
    EXPLICIT_POSITIVE_ANNOTATION = "explicit_positive_annotation"
    #: ``normal_evidence`` の明示ラベルがある。**レポートで覆さない。**
    EXPLICIT_NEGATIVE_NORMAL = "explicit_negative_normal"
    #: レポートが在り、気胸を含め陽性の異常所見が無いことを確認できた。
    EXPLICIT_NEGATIVE_REPORT = "explicit_negative_report"
    #: 未確認だったところへレポートの ``present`` を適用した。
    REPORT_POSITIVE = "report_positive"
    #: 気胸 annotation / mask が無いだけ。**陰性ではない。**
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class SampleLabels:
    """1画像ぶんのラベル属性。

    ``finding_labels`` は**出力属性ではない**（テンプレートから削除された）。
    ``abnormal_finding_status`` / ``pneumothorax_case`` / ``bulla_bleb_status`` を
    決める材料として内部に持ち、summary の所見語彙一覧にも使う。
    """

    #: 拾えた所見名。出力JSONには載せない（判定の材料と summary 用）。
    finding_labels: tuple[str, ...]
    abnormal_finding_status: str
    pneumothorax_case: bool
    bulla_bleb_status: str
    #: 気胸の側（``left`` / ``right`` / ``bilateral``）。**既存 annotation からは
    #: 作らない**（テンプレートは患者基準の解剖学的左右と明記しており、画像座標から
    #: 起こすと全サンプルが反転する）。値が入るのは構造化読影レポート経由だけ。
    pneumothorax_side: str | None = None
    #: ``absent`` の根拠になったラベル（``"<code_system>/<code_text_eng>"``）。
    #: 判定の説明用で、出力JSONには載せない。
    normal_evidence_hits: tuple[str, ...] = field(default=())
    #: ``pneumothorax_case`` が何を根拠にしているか。**出力JSONには載せない。**
    #: レポートで補完してよいかをこれで判断する（``UNCONFIRMED`` のときだけ）。
    case_evidence: CaseEvidence = CaseEvidence.UNCONFIRMED

    def with_case(
        self,
        *,
        pneumothorax_case: bool,
        case_evidence: CaseEvidence,
    ) -> "SampleLabels":
        """``pneumothorax_case`` と根拠だけを差し替えた複製を返す。"""
        return replace(
            self, pneumothorax_case=pneumothorax_case, case_evidence=case_evidence
        )


def qualified(label: Label) -> str:
    """``normal_evidence`` の照合に使う ``"<code_system>/<code_text_eng>"``。

    ``Label.qualified_code`` は ``(code_system, code)`` 側で、データセットごとに
    割り当てが違うため横断的な照合には使えない。
    """
    return f"{label.code_system}/{label.code_text_eng}"


def build_labels(
    group: FileGroup,
    *,
    finding_code_systems: Iterable[str] = ("Findings",),
    normal_evidence: Iterable[str] = ("No Findings/normal",),
) -> SampleLabels:
    """``FileGroup`` からラベル4属性を作る。

    ``finding_code_systems`` / ``normal_evidence`` は設定
    （``config.lpdata_export``）から渡す。コードに埋め込まないのは、
    「どのラベルを信用するか」がデータ側の判断でありツールの判断ではないため。
    """
    systems = frozenset(finding_code_systems)
    evidence = frozenset(normal_evidence)

    # --- finding_labels: geometry annotation のうち所見 code_system のものだけ ---
    findings = {
        label.code_text_eng
        for record in group.records
        for label in record.labels
        if label.code_system in systems and label.code_text_eng
    }
    finding_labels = tuple(sorted(findings))

    # --- abnormal_finding_status ---
    # 正常の根拠は geometry / series / study のどの階層に付いていても採る
    # （``case_labels`` が study と series の分類ラベルをまとめて持っている）。
    candidates: list[Label] = list(group.case_labels)
    for record in group.records:
        candidates.extend(record.labels)
    hits = tuple(sorted({q for q in map(qualified, candidates) if q in evidence}))

    if finding_labels:
        status = PRESENT
    elif hits:
        status = ABSENT
    else:
        # annotation が無いだけ / マスクが無いだけでは正常と断定しない。
        status = UNKNOWN

    # --- pneumothorax_case: 気胸アノテーション由来。status からは導出しない ---
    # PR #68 が両者を意図的に切り離している（``unknown`` かつ ``true`` かつ
    # ``finding_labels: []`` は正当な組み合わせ）。初期生成ではその形は出ないが、
    # 後日のCSV更新処理が出せるように依存を作らない。
    pneumothorax_case = PNEUMOTHORAX in findings

    # --- case_evidence: false が「陰性」か「未確認」かを分ける ---
    # **annotation が無いだけの false を陰性と呼ばない。** ここを潰すと、
    # 読影レポートによる補完が「確認済みの陰性」まで上書きしてしまう。
    # 他の所見がアノテーション済みであることは**気胸についての陰性根拠ではない**
    # （その画像について気胸を否定したのではなく、単に付けていないだけ）。
    if pneumothorax_case:
        case_evidence = CaseEvidence.EXPLICIT_POSITIVE_ANNOTATION
    elif hits:
        case_evidence = CaseEvidence.EXPLICIT_NEGATIVE_NORMAL
    else:
        case_evidence = CaseEvidence.UNCONFIRMED

    # --- bulla_bleb_status: present か unknown だけ ---
    # ``absent`` は出さない。bulla / bleb がアノテーション対象だったかを既存
    # annotation からは判定できず、「無い」と「対象外」を区別する根拠が無いため。
    bulla_bleb_status = PRESENT if BULLA_BLEB in findings else UNKNOWN

    return SampleLabels(
        finding_labels=finding_labels,
        abnormal_finding_status=status,
        pneumothorax_case=pneumothorax_case,
        bulla_bleb_status=bulla_bleb_status,
        normal_evidence_hits=hits,
        case_evidence=case_evidence,
    )
