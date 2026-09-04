"""``summary.md`` / ``selection_summary.md``。

狙いは1つ: **このファイルを見るだけで**
「validationは終わったのか」「FiftyOne目視は終わったのか」
「Development JSON を作ってよい状態なのか」が判断できること。

参照マスクのカバレッジは設定の参照ルートに依存するので、
値をどこにも固定せず**毎回実測して**書き出す。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..checks.base import CheckStatus, Issue, Severity
from ..config import Config
from ..core.measure import ROLE_MASK, FileMeasurement, MaskMeasurement
from ..selection.decisions import (
    Decision,
    DecisionSource,
    Reason,
    ReviewStatus,
    SelectionDecision,
)
from .issues_json import summarize as summarize_issues


def reference_coverage(
    files: Sequence[FileMeasurement], masks: Sequence[MaskMeasurement]
) -> dict[str, Any]:
    """参照マスクのカバレッジを実測する。

    整備状況は施設・バッチ日単位で変わるので、レポートするたびに数え直す。
    """
    by_dataset: dict[str, dict[str, int]] = {}
    status_files: dict[str, int] = {}
    status_annotations: dict[str, int] = {}
    file_status = {f.file_uid: (f.dataset_id, f.reference_status) for f in files}

    for measurement in files:
        if measurement.n_masks == 0:
            continue
        entry = by_dataset.setdefault(
            measurement.dataset_id,
            {
                "files": 0,
                "files_available": 0,
                "annotations": 0,
                "annotations_available": 0,
            },
        )
        entry["files"] += 1
        if measurement.reference_status == "available":
            entry["files_available"] += 1
        status_files[measurement.reference_status or "unknown"] = (
            status_files.get(measurement.reference_status or "unknown", 0) + 1
        )

    for mask in masks:
        if mask.role != ROLE_MASK:
            continue
        dataset_id, status = file_status.get(mask.file_uid, (mask.dataset_id, None))
        entry = by_dataset.setdefault(
            dataset_id,
            {
                "files": 0,
                "files_available": 0,
                "annotations": 0,
                "annotations_available": 0,
            },
        )
        entry["annotations"] += 1
        if status == "available":
            entry["annotations_available"] += 1
        status_annotations[status or "unknown"] = (
            status_annotations.get(status or "unknown", 0) + 1
        )

    return {
        "by_dataset": by_dataset,
        "status_files": status_files,
        "status_annotations": status_annotations,
    }


def write_summary(
    path: Path,
    config: Config,
    issues: Sequence[Issue],
    decisions: Sequence[SelectionDecision],
    files: Sequence[FileMeasurement],
    masks: Sequence[MaskMeasurement],
    meta: dict[str, Any],
) -> None:
    """検出結果の要約。"""
    summary = summarize_issues(issues)
    coverage = reference_coverage(files, masks)
    lines: list[str] = []
    add = lines.append

    add("# バリデーション要約")
    add("")
    add(f"- 生成: {datetime.now(timezone.utc).isoformat()}")
    add(f"- fingerprint: `{meta.get('fingerprint')}`")
    add(f"- 対象JSON: {len(meta.get('sources', []))} 件")
    for source in meta.get("sources", []):
        add(f"  - `{source}`")
    add(f"- annotation: {meta.get('n_annotations')} / ファイル: {meta.get('n_files')}")
    if meta.get("n_non_geometry"):
        add(f"- 対象外（study/seriesレベルの分類ラベル）: {meta['n_non_geometry']} 件")
    add("")

    add("## 検出件数")
    add("")
    add("| check_id | 件数 |")
    add("|---|---|")
    for check_id, count in summary["by_check"].items():
        add(f"| `{check_id}` | {count} |")
    add("")
    add(f"- severity: {_fmt(summary['by_severity'])}")
    add(f"- status: {_fmt(summary['by_status'])}")
    add("")

    add("## 検査できなかったもの (cannot_determine)")
    add("")
    if summary["cannot_determine_reasons"]:
        add("| 理由 | 件数 |")
        add("|---|---|")
        for reason, count in summary["cannot_determine_reasons"].items():
            add(f"| `{reason}` | {count} |")
    else:
        add("なし。")
    add("")
    policy = config.decision_policy.cannot_determine_as_review_required
    add(f"現在の policy: `cannot_determine_as_review_required = {policy}`")
    add(
        "  -> これらは keep のまま。review に回すには設定を `true` にする"
        if not policy
        else "  -> これらも目視対象に含めている"
    )
    add("")

    add("## 参照マスクのカバレッジ（毎回実測）")
    add("")
    add("| データセット | ファイル | 3種そろい | annotation | 判定可 |")
    add("|---|---|---|---|---|")
    for dataset_id, entry in sorted(coverage["by_dataset"].items()):
        add(
            f"| {dataset_id} | {entry['files']} | "
            f"{entry['files_available']} "
            f"({_pct(entry['files_available'], entry['files'])}) | "
            f"{entry['annotations']} | "
            f"{entry['annotations_available']} "
            f"({_pct(entry['annotations_available'], entry['annotations'])}) |"
        )
    add("")
    add(f"- ファイル単位の status: {_fmt(coverage['status_files'])}")
    add(f"- annotation単位の status: {_fmt(coverage['status_annotations'])}")
    add("")

    add("## 施設別の検出件数（上位15）")
    add("")
    top = sorted(summary["by_institution"].items(), key=lambda item: -item[1])[:15]
    add("| 施設 | 件数 |")
    add("|---|---|")
    for institution, count in top:
        add(f"| {institution} | {count} |")
    add("")

    add("## アノテータ別（体外領域の warning 以上）")
    add("")
    annotators: dict[str, int] = {}
    for issue in issues:
        if issue.check_id.startswith("S05_OUTSIDE_BODY") and issue.severity in (
            Severity.ERROR,
            Severity.WARNING,
        ):
            user = str(issue.detail.get("annotator") or "?")
            annotators[user] = annotators.get(user, 0) + 1
    if annotators:
        add("| アノテータ | 件数 |")
        add("|---|---|")
        for user, count in sorted(annotators.items(), key=lambda item: -item[1]):
            add(f"| {user} | {count} |")
        add("")
        add("系統的なアノテータ起因の誤りが疑われる場合はここに偏りが出る。")
    else:
        add("該当なし。")
    add("")

    _write(path, lines)


def write_selection_summary(
    path: Path,
    config: Config,
    decisions: Sequence[SelectionDecision],
    issues: Sequence[Issue],
    empty_files: int,
    meta: dict[str, Any],
) -> None:
    """採否の要約。Development JSON を作ってよいかの判定を必ず出す。"""
    counts = _decision_counts(decisions)
    pending = counts["decision"].get(Decision.PENDING.value, 0)
    uncertain = counts["decision"].get(Decision.UNCERTAIN.value, 0)
    ready = pending == 0 and uncertain == 0

    lines: list[str] = []
    add = lines.append

    add("# 採否サマリ")
    add("")
    add(f"- 生成: {datetime.now(timezone.utc).isoformat()}")
    add(f"- fingerprint: `{meta.get('fingerprint')}`")
    if meta.get("overrides"):
        add(f"- **override を使用**: {meta['overrides']}")
    add("")

    total = len(decisions)
    add(f"total annotations              {total}   ({_fmt(counts['type'])})")
    add(f"  keep                         {counts['decision'].get('keep', 0)}")
    add(
        f"    no_issue_detected          "
        f"{counts['reason'].get(Reason.NO_ISSUE.value, 0)}"
        "     全checkが checked で問題なし"
    )
    add(
        f"    kept_without_full_check    "
        f"{counts['reason'].get(Reason.KEPT_WITHOUT_FULL_CHECK.value, 0)}"
        "  <- 判定不能のまま keep（未検証）"
    )
    add(f"    human keep                 {counts['human_keep']}")
    add(f"  exclude                      {counts['decision'].get('exclude', 0)}")
    add(f"  pending                      {pending}")
    add(f"  uncertain                    {uncertain}")
    add("")

    add("## 判断の内訳")
    add("")
    add(
        f"automatic decisions            {counts['source'].get('automatic', 0)}"
        f"   (うち D01 による自動exclude "
        f"{counts['reason'].get(Reason.OLDER_EXACT_DUPLICATE.value, 0)})"
    )
    add(f"human decisions                {counts['source'].get('human', 0)}")
    add(f"default (keep)                 {counts['source'].get('default', 0)}")
    add(f"自動判定を人が覆した            {counts['overrides']}")
    add("")

    add("## 検査できなかったもの (cannot_determine)")
    add("")
    unverified: dict[str, int] = {}
    for issue in issues:
        if issue.status is CheckStatus.CANNOT_DETERMINE:
            reason = issue.cannot_determine_reason or issue.check_id
            unverified[reason] = unverified.get(reason, 0) + 1
    for reason, count in sorted(unverified.items(), key=lambda item: -item[1]):
        add(f"{reason:44s} {count}")
    add("")
    policy = config.decision_policy.cannot_determine_as_review_required
    add(f"現在の policy: cannot_determine_as_review_required = {policy}")
    if not policy:
        add("  -> これらは keep のまま。review に回すには設定を true にする")
        add("     対象は selection_decisions.csv の unverified_checks != none で絞れる")
    add("")

    add("## FiftyOne 目視の進捗")
    add("")
    targets = sum(1 for d in decisions if d.review_required)
    reviewed = counts["review_status"].get(ReviewStatus.REVIEWED.value, 0)
    add(f"review対象                      {targets}")
    add(f"  review完了                    {reviewed}")
    add(f"  review未完了 (pending)        {pending}     <- 0 が完了条件")
    add("")

    add("## 影響")
    add("")
    add(f"annotationが0件になったfile     {empty_files}")
    add("")
    add("除外により失われた病変クラス:")
    add("")
    lost = _lost_by_class(decisions)
    if lost:
        add("| code_system/code | 元 | 除外 | 残 |")
        add("|---|---|---|---|")
        for key, (before, excluded) in sorted(lost.items()):
            add(f"| {key} | {before} | {excluded} | {before - excluded} |")
    else:
        add("なし。")
    add("")

    add("## 判定")
    add("")
    if ready:
        add("[OK] pending / uncertain なし")
        add("Development JSON を生成してよいか: **yes**")
    else:
        add(f"[BLOCKED] pending {pending} 件 / uncertain {uncertain} 件 が残っています")
        add("Development JSON を生成してよいか: **no**")
        add("")
        add(
            "目視を進めるか、override を明示すること"
            "（`--allow-pending --pending-as exclude|keep`）。"
        )
    kept_without = counts["reason"].get(Reason.KEPT_WITHOUT_FULL_CHECK.value, 0)
    if kept_without:
        add("")
        add(
            f"注記: kept_without_full_check が {kept_without} 件あります"
            "（体外判定が未実施のまま keep している）"
        )
    add("")

    _write(path, lines)


# ------------------------------------------------------------------ helpers


def _decision_counts(decisions: Sequence[SelectionDecision]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "decision": {},
        "reason": {},
        "source": {},
        "type": {},
        "review_status": {},
        "overrides": 0,
        "human_keep": 0,
    }
    for decision in decisions:
        _bump(result["decision"], decision.final_decision.value)
        _bump(result["reason"], decision.reason)
        _bump(result["source"], decision.decision_source.value)
        _bump(result["type"], decision.annotation_type)
        _bump(result["review_status"], decision.review_status.value)
        if decision.overrides_automatic:
            result["overrides"] += 1
        if (
            decision.decision_source is DecisionSource.HUMAN
            and decision.final_decision is Decision.KEEP
        ):
            result["human_keep"] += 1
    return result


def _lost_by_class(
    decisions: Sequence[SelectionDecision],
) -> dict[str, tuple[int, int]]:
    """病変クラスごとの元件数と除外件数。除外が偏っていないかを見る。"""
    result: dict[str, tuple[int, int]] = {}
    for decision in decisions:
        if not decision.code_system:
            continue
        key = f"{decision.code_system}/{decision.code}"
        before, excluded = result.get(key, (0, 0))
        before += 1
        if decision.final_decision is Decision.EXCLUDE:
            excluded += 1
        result[key] = (before, excluded)
    return {key: value for key, value in result.items() if value[1] > 0}


def _bump(target: dict[str, int], key: str) -> None:
    target[key] = target.get(key, 0) + 1


def _fmt(mapping: dict[str, int]) -> str:
    return " / ".join(f"{key} {value}" for key, value in sorted(mapping.items()))


def _pct(part: int, whole: int) -> str:
    return f"{100.0 * part / whole:.1f}%" if whole else "-"


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
