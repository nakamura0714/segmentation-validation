"""ラベル属性間の不変条件。純関数。

正典は med-chest-metry-pi6 の ``dataset_template_pneumothorax.yaml``。
同リポジトリの ``data/README.md`` にも不変条件の記述があるが、そちらは
削除済みの属性（``finding_labels`` / ``abnormal_case``）を参照したまま陳腐化しており、
**参照しない**。

``feat(data)!: finding_labels を削除しラベルを独立属性に整理する``（2026-09-16）で
**ラベル属性どうしの結合が外れた**。テンプレートは次を明示している。

- ``pneumothorax_case`` は**気胸アノテーション由来**で、マスクの有無とは独立。
  ``true`` かつ ``pneumothorax_mask`` が ``pixel_array: null``（マスク未アノテーションの
  気胸症例）は正当な組み合わせ
- ``abnormal_finding_status`` は**読影所見由来**。``pneumothorax_case`` とは由来が違うので、
  ``unknown`` かつ ``pneumothorax_case: true`` も正当

したがって残る不変条件は「値が label_map の範囲にあること」と、
``pneumothorax_side`` が気胸症例のときだけ入ること。**由来の違う属性どうしを
勝手に結び付けない**のがこの版の要点で、前版はそれをやって
「unknown ⇒ pneumothorax_case: false」を強制し、規則どおりGTを作ると
感度が黙って下がる経路になっていた。

初期エクスポート（``labels.build_labels``）では違反0件になる。それでもこの検査を
資産として持つのは、後日の「読影レポートCSVによるラベル更新処理」がそのまま
再利用するためと、回帰を検知するため。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .labels import ABSENT, PRESENT, UNKNOWN

STATUS_KEY = "abnormal_finding_status"
CASE_KEY = "pneumothorax_case"
SIDE_KEY = "pneumothorax_side"
BULLA_KEY = "bulla_bleb_status"

#: ``abnormal_finding_status`` / ``bulla_bleb_status`` の label_map。
THREE_VALUED = frozenset({PRESENT, ABSENT, UNKNOWN})
#: ``pneumothorax_side`` に入れてよい値。患者から見た解剖学的左右。
SIDES = frozenset({"left", "right", "bilateral"})


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

    持っていない属性は検査対象外として静かに飛ばす（更新処理が一部だけ触った場合など）
    —— 「持っていない」と「矛盾している」は別物。
    """
    found: list[InvariantViolation] = []

    def violate(rule: str, detail: str) -> None:
        found.append(InvariantViolation(sample_id, rule, detail))

    for key in (STATUS_KEY, BULLA_KEY):
        if key in sample and sample[key] not in THREE_VALUED:
            violate("label_map 外の値", f"{key}={sample[key]!r}")

    # 気胸の側は気胸症例にしか付かない。false や不明な症例に left/right が付いていたら、
    # 由来の違う情報が紛れ込んでいる。
    if SIDE_KEY in sample and sample[SIDE_KEY] is not None:
        if sample[SIDE_KEY] not in SIDES:
            violate("pneumothorax_side の値", f"{SIDE_KEY}={sample[SIDE_KEY]!r}")
        if CASE_KEY in sample and not sample[CASE_KEY]:
            violate(
                "pneumothorax_side は気胸症例のときだけ",
                f"{CASE_KEY}=False なのに {SIDE_KEY}={sample[SIDE_KEY]!r}",
            )

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
