"""ラベル属性間の不変条件。純関数。

正典は med-chest-metry-pi6 の ``dataset_template_pneumothorax.yaml``
（PR #68。``abnormal_finding_status`` のコメント）。同リポジトリの
``data/README.md`` にも不変条件の記述があるが、そちらは無条件版・旧属性名のまま
陳腐化しており（PR #68 が SD-1 で追随を見送った）、**参照しない**。

**条件付きである点が肝。** 気胸の有無は気胸アノテーション由来で
``pneumothorax_case`` が独立に持ち、``finding_labels`` は読影所見由来なので、
``unknown``（所見未取得）のときは両者を結び付けられない。
前身の無条件版は「unknown ⇒ pneumothorax_case: false」を強制しており、
規則どおりGTを作ると**感度が黙って下がる**経路になっていた。

初期エクスポート（``labels.build_labels``）では 1〜6 がすべて構成上成立し、
違反は0件になる。
それでもこの検査を資産として持つのは、後日の「読影レポートCSVによるラベル更新処理」が
そのまま再利用するためと、回帰を検知するため。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .labels import ABSENT, PNEUMOTHORAX, PRESENT, UNKNOWN

STATUS_KEY = "abnormal_finding_status"
LABELS_KEY = "finding_labels"
CASE_KEY = "pneumothorax_case"

#: ``pneumothorax_case`` との対応を課す status。``unknown`` は含めない。
COUPLED_STATUSES = frozenset({PRESENT, ABSENT})


@dataclass(frozen=True)
class InvariantViolation:
    """不変条件の違反1件。"""

    sample_id: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.sample_id}: {self.rule} — {self.detail}"


def check_sample(sample_id: str, sample: Mapping[str, Any]) -> list[InvariantViolation]:
    """サンプル1件を検査する。

    ラベル3属性を持たないサンプル（更新処理が一部だけ触った場合など）は
    検査対象外として静かに飛ばす —— 「持っていない」と「矛盾している」は別物。
    """
    if not {STATUS_KEY, LABELS_KEY, CASE_KEY} <= set(sample):
        return []

    status = sample[STATUS_KEY]
    labels = sample[LABELS_KEY] or []
    case = sample[CASE_KEY]
    has_labels = bool(labels)
    found: list[InvariantViolation] = []

    def violate(rule: str, detail: str) -> None:
        found.append(InvariantViolation(sample_id, rule, detail))

    if status == PRESENT and not has_labels:
        violate("1: present ⇔ finding_labels 非空", "present だが finding_labels が空")
    if status != PRESENT and has_labels:
        violate(
            "1/2/3: present 以外は finding_labels 空",
            f"{status} だが finding_labels={sorted(labels)}",
        )

    # 4 は present / absent のときだけ。unknown では課さない
    # （「気胸ラベルはあるが読影所見は未取得」を表現できなくなるため）。
    if status in COUPLED_STATUSES:
        expected = PNEUMOTHORAX in labels
        if bool(case) is not expected:
            violate(
                "4: present/absent 時の気胸ラベル対応",
                f"{status} で pneumothorax_case={case} だが "
                f"'{PNEUMOTHORAX}' in finding_labels = {expected}",
            )

    if status == ABSENT and case:
        violate(
            "5: absent かつ pneumothorax_case: true は不正",
            "正常と判定されているのに気胸症例になっている",
        )

    if status not in (PRESENT, ABSENT, UNKNOWN):
        violate("label_map 外の値", f"{STATUS_KEY}={status!r}")

    return found


def check_samples(
    samples: Mapping[str, Mapping[str, Any]] | Iterable[tuple[str, Mapping[str, Any]]],
) -> list[InvariantViolation]:
    """全サンプルを検査する。``dict`` でも ``(sample_id, sample)`` の列でもよい。"""
    items = samples.items() if isinstance(samples, Mapping) else samples
    violations: list[InvariantViolation] = []
    for sample_id, sample in items:
        violations.extend(check_sample(sample_id, sample))
    return violations
