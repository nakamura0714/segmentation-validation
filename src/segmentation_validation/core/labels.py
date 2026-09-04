"""アノテーションに付くラベル。

ラベルの同一性は ``(code_system, code)`` の組で判断する。
``code_text`` / ``code_text_eng`` は同一性判定に使わない。
特に ``code_text_eng`` は複数の日本語表記を同じ英語名にまとめることがあり
（``Findings/010`` に「気胸（塗りつぶし）」294件と「気胸（縁取り）」1件があるが
どちらも ``pneumothorax``）、英語名だけ見ていると差が完全に見えない。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# 微小領域の閾値をクラス族ごとに変えるための対応表。
# 対応する閾値は config.thresholds.small_by_class_mm2。
# 結節系は正当に小さいので、一律閾値だと検出予算をここに食われる。
LABEL_FAMILY: dict[str, str] = {
    "nodule": "focal",
    "multiple_granular_shadows": "focal",
    "atelectasis": "localized",
    "bulla_bleb": "localized",
    "cavity": "localized",
    "hilar_expansion": "localized",
    "hyperinflation": "localized",
    "pneumothorax": "regional",
    "pleural_effusion": "regional",
    "infiltrative_shadow": "regional",
    "interstitial_opacity": "regional",
    "pneumomediastinum": "regional",
    "mediastinum_enlargement": "regional",
    "aortic_protrusion": "regional",
    "fracture": "localized",
    "other": "regional",
}
DEFAULT_LABEL_FAMILY = "regional"


@dataclass(frozen=True)
class Label:
    """アノテーションに付いたラベル1件。"""

    code_system: str
    code: str
    code_text: str
    code_text_eng: str
    confidence: float | None
    # データベース上の一意ID。同一性判定には使わないが、取り込み経緯の追跡に残す。
    label_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        """ラベルの同一性判断に使うキー。"""
        return (self.code_system, self.code)

    @property
    def qualified_code(self) -> str:
        """``Findings/010`` のような修飾付きコード表記。"""
        return f"{self.code_system}/{self.code}"

    @property
    def family(self) -> str:
        """微小領域の閾値を選ぶためのクラス族。"""
        return LABEL_FAMILY.get(self.code_text_eng, DEFAULT_LABEL_FAMILY)

    def display(self, japanese: bool = True, with_confidence: bool = True) -> str:
        """図やログに出す1行表記。

        ``japanese=False`` の環境（CJKフォント無し）では英語名のみを使う。
        語彙一覧のように confidence が邪魔な場面では ``with_confidence=False``。
        """
        name = self.code_text_eng or "?"
        if japanese and self.code_text and self.code_text != self.code_text_eng:
            name = f"{self.code_text} ({name})"
        conf = ""
        if with_confidence and self.confidence is not None:
            conf = f" conf={self.confidence:g}"
        return f"{self.qualified_code} {name}{conf}"


def parse_labels(raw_labels: list[dict[str, Any]]) -> tuple[Label, ...]:
    """``labels`` を ``Label`` へ変換する。

    ``label_id`` 以外が同一のエントリは同じラベルの二重登録とみなして1件に畳む。
    ただし ``code_text_eng`` だけで畳むと code_system や code_text の差を潰すので、
    畳む条件は ``label_id`` を除く全項目の一致とする。
    """
    seen: dict[tuple[str, str, str, str, float | None], Label] = {}
    for raw in raw_labels:
        label = Label(
            code_system=raw["code_system"],
            code=str(raw["code"]),
            code_text=raw.get("code_text") or "",
            code_text_eng=raw.get("code_text_eng") or "",
            confidence=raw.get("confidence"),
            label_id=raw.get("label_id"),
        )
        identity = (
            label.code_system,
            label.code,
            label.code_text,
            label.code_text_eng,
            label.confidence,
        )
        seen.setdefault(identity, label)
    return tuple(seen.values())


def label_keys(labels: tuple[Label, ...]) -> frozenset[tuple[str, str]]:
    """``(code_system, code)`` の集合。重複マスクの同一ラベル判定に使う。"""
    return frozenset(label.key for label in labels)


def label_selectors(raw: Iterable[str]) -> tuple[frozenset[str], frozenset[str]]:
    """設定の ``target_labels`` を照合用の2集合へ分解する。

    ``"Findings/010"`` は ``(code_system, code)`` の正式な指定、
    それ以外は ``code_text_eng`` として扱う。
    """
    keys, names = set(), set()
    for token in raw:
        token = token.strip()
        if not token:
            continue
        (keys if "/" in token else names).add(token)
    return frozenset(keys), frozenset(names)


def matches_target(
    labels: tuple[Label, ...], keys: frozenset[str], names: frozenset[str]
) -> bool:
    """このラベル群が検証対象に含まれるか。

    どちらの集合も空なら全件が対象（絞り込み無し）。
    複数ラベルを持つ annotation は、1つでも該当すれば対象に含める
    —— 対象病変が写っている annotation を取りこぼさないため。
    """
    if not keys and not names:
        return True
    for label in labels:
        if label.qualified_code in keys or label.code_text_eng in names:
            return True
    return False
