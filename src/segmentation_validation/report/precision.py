"""自動ルールの Precision。**fiftyone を import しない**。

``auto:`` タグ（機械の検出）と ``review:``（人間の判定）を分けておいた見返りが
ここで回収される。自動検出のうち何割が本当に不正だったかを check ごとに出す::

    auto:s05_outside_body  26件 -> exclude 18 / keep 8   Precision 0.69
    auto:d04_contained_different_label 69件 -> exclude 1 / keep 35  Precision 0.03

Precision が極端に低い check は「そのルール自体が的を外している」ということなので、
閾値を変えるか、ルールごと外す判断ができる。

読むのは ``issues.json`` と ``review_decisions.json`` だけ。
FiftyOne の DB は見ない（正本にしないという方針の一部）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..checks.base import CheckStatus, Issue
from ..config import Config
from ..selection.decisions import Decision, effective_review_required

#: 「不正だった」とみなす判定。
INVALID = {Decision.EXCLUDE.value}
#: 「妥当だった」とみなす判定。
VALID = {Decision.KEEP.value}


@dataclass(frozen=True)
class CheckPrecision:
    """1つの check_id についての Precision。"""

    check_id: str
    detected: int
    excluded: int
    kept: int
    uncertain: int
    unreviewed: int

    @property
    def reviewed(self) -> int:
        return self.excluded + self.kept + self.uncertain

    @property
    def precision(self) -> float | None:
        """判定済みのうち exclude になった割合。未レビューなら None。

        uncertain は分母に入れる（「判断できなかった」も検出の結果なので）。
        """
        if self.reviewed == 0:
            return None
        return self.excluded / self.reviewed

    @property
    def progress(self) -> float:
        return self.reviewed / self.detected if self.detected else 1.0


def compute_precision(
    issues: Sequence[Issue],
    verdicts: dict[str, str],
    config: Config,
) -> list[CheckPrecision]:
    """check_id ごとに Precision を出す。

    ``verdicts`` は ``geometry_uid -> decision``（人間の判定のみ）。
    自動判定を混ぜてはいけない —— 混ぜると「機械が機械を採点する」ことになる。
    """
    detected: dict[str, set[str]] = {}
    for issue in issues:
        uid = issue.geometry_uid
        if uid is None:
            continue
        # 目視対象にならなかった検出は Precision の対象外
        # （記録のみ・判定不能は人間が判定していないので分母に入らない）。
        if not effective_review_required(issue.check_id, issue.status, config):
            continue
        if issue.status is not CheckStatus.CHECKED:
            continue
        detected.setdefault(issue.check_id, set()).add(uid)

    results = []
    for check_id, uids in sorted(detected.items()):
        excluded = sum(1 for u in uids if verdicts.get(u) in INVALID)
        kept = sum(1 for u in uids if verdicts.get(u) in VALID)
        uncertain = sum(1 for u in uids if verdicts.get(u) == Decision.UNCERTAIN.value)
        results.append(
            CheckPrecision(
                check_id=check_id,
                detected=len(uids),
                excluded=excluded,
                kept=kept,
                uncertain=uncertain,
                unreviewed=len(uids) - excluded - kept - uncertain,
            )
        )
    return sorted(results, key=lambda r: -r.detected)


def read_verdicts(path: Path) -> dict[str, str]:
    """``review_decisions.json`` から人間の判定だけを読む。"""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {d["geometry_uid"]: d["decision"] for d in payload.get("decisions", [])}


def write_precision(
    path: Path, results: Sequence[CheckPrecision], meta: dict[str, Any]
) -> None:
    lines: list[str] = []
    add = lines.append
    add("# 自動ルールの Precision")
    add("")
    add(f"- 生成: {datetime.now(timezone.utc).isoformat()}")
    add(f"- fingerprint: `{meta.get('fingerprint')}`")
    add("")
    add(
        "`auto:` タグ（機械の検出）と `review:`（人間の判定）を分けているので、"
        "自動検出のうち何割が本当に不正だったかを check ごとに出せる。"
    )
    add("")
    reviewed = sum(r.reviewed for r in results)
    total = sum(r.detected for r in results)
    add(
        f"目視の進捗: **{reviewed} / {total}**"
        "（延べ。1 annotation が複数 check に該当し得る）"
    )
    add("")

    if not reviewed:
        add("まだ判定が1件も無いので Precision は計算できない。")
        add("`review build` → App で目視 → `review export` の後にもう一度実行する。")
        add("")

    add("| check_id | 検出 | 目視済 | exclude | keep | uncertain | Precision |")
    add("|---|---|---|---|---|---|---|")
    for r in results:
        p = "—" if r.precision is None else f"**{r.precision:.2f}**"
        add(
            f"| `{r.check_id}` | {r.detected} | {r.reviewed} "
            f"({r.progress:.0%}) | {r.excluded} | {r.kept} | {r.uncertain} | {p} |"
        )
    add("")
    add("## 読み方")
    add("")
    add(
        "- **Precision が高い** = そのルールは的を射ている。"
        "自動採否の候補にできるか検討する価値がある"
    )
    add(
        "- **Precision が低い** = 検出しているものの大半が正当。"
        "閾値を緩めるか、ルールごと外す"
    )
    add("- `uncertain` が多い = 判断基準そのものが曖昧。判定の定義を決め直す必要がある")
    add("")
    add(
        "分母は「目視済（exclude + keep + uncertain）」。"
        "未レビュー分は含めないので、途中でも意味のある値が出る。"
    )
    add("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize(results: Sequence[CheckPrecision]) -> dict[str, Any]:
    return {
        "checks": len(results),
        "detected": sum(r.detected for r in results),
        "reviewed": sum(r.reviewed for r in results),
        "excluded": sum(r.excluded for r in results),
        "kept": sum(r.kept for r in results),
        "uncertain": sum(r.uncertain for r in results),
        "by_check": {
            r.check_id: {
                "detected": r.detected,
                "reviewed": r.reviewed,
                "excluded": r.excluded,
                "kept": r.kept,
                "uncertain": r.uncertain,
                "precision": r.precision,
            }
            for r in results
        },
    }
