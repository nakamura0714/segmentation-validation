"""ブラウザで結果を見るためのダッシュボードを組み立てる。

``output/validation/<fingerprint>/`` の JSON/CSV は人が直感的に読める形ではないので、
同じ内容を絞り込みと図で辿れる単一のHTMLにする。テンプレートは
``assets/dashboard/`` にあり、このモジュールはデータを JSON にして埋め込むだけ。

数値の精度に注意。面積を小数第1位で丸めると **0.031mm² の annotation が 0 になって
図から消える**（それがまさに見せたい対象）。包含率を4桁で丸めると 0.99999 が 1.0 に
なって体外領域の点が脱落する。どちらも有効数字・小数6桁で保持する。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..checks.base import CheckStatus, Issue
from ..config import Config
from ..core.labels import label_selectors
from ..core.measure import ROLE_MASK, FileMeasurement, MaskMeasurement
from ..selection.decisions import (
    CASE_STATUS_JA,
    CaseStatus,
    Decision,
    SelectionDecision,
    count_case_status,
    count_cases,
    effective_review_required,
    policy_for,
)

ASSET_DIR = "assets/dashboard"
# 派生IDを含めた全 check_id をコードから拾うための並び。
_ID_RE = re.compile(r'^[A-Z_]+ = "((?:M|D|S)\d\d_[A-Z_0-9]+)"', re.M)

DECISIONS = ["keep", "exclude", "pending", "uncertain"]
SOURCES = ["automatic", "human", "default", "-"]
TYPES = ["brush", "bbox", "elliptical"]


def significant(value: float | None, digits: int = 4) -> float | None:
    """有効数字で丸める。小さい値を潰さないため固定小数では丸めない。"""
    if value is None:
        return None
    if value == 0:
        return 0.0
    shift = digits - 1 - math.floor(math.log10(abs(value)))
    return round(value, shift) if shift > 0 else round(value, 0)


class _Vocab:
    """繰り返す文字列をインデックス化してペイロードを小さくする。"""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._index: dict[str, int] = {}

    def __call__(self, value: Any) -> int:
        text = "" if value is None else str(value)
        if text not in self._index:
            self._index[text] = len(self.values)
            self.values.append(text)
        return self._index[text]


def check_inventory(
    checks: Sequence[Any],
    issues: Sequence[Issue],
    decisions: Sequence[SelectionDecision],
    config: Config,
) -> list[dict[str, Any]]:
    """全 check_id の台帳。

    0件のものも載せる。回帰検知として稼働中であることが分かるように。
    """
    modules = {c.CHECK_ID: c for c in checks}
    owner: dict[str, str] = {}
    for module in checks:
        for check_id in _ID_RE.findall(
            Path(module.__file__).read_text(encoding="utf-8")
        ):
            owner[check_id] = module.CHECK_ID

    counts = Counter(issue.check_id for issue in issues)
    status = {issue.check_id: issue.status for issue in issues}
    by_uid = {d.geometry_uid: d for d in decisions}

    # 目視対象になった check ごとに、pending の annotation と症例を集める
    scope: dict[str, list[SelectionDecision]] = {}
    for issue in issues:
        uid = issue.geometry_uid
        if uid is None:
            continue
        if not effective_review_required(issue.check_id, issue.status, config):
            continue
        decision = by_uid.get(uid)
        if decision is None or decision.final_decision is not Decision.PENDING:
            continue
        scope.setdefault(issue.check_id, []).append(decision)

    inventory = []
    for check_id, module_id in sorted(owner.items()):
        module = modules[module_id]
        state = status.get(check_id, CheckStatus.CHECKED)
        policy = policy_for(check_id, config).value
        review = effective_review_required(check_id, state, config)
        kind = (
            "automatic"
            if policy == "auto_decidable"
            else "cannot_determine"
            if state is CheckStatus.CANNOT_DETERMINE
            else "review"
            if review
            else "record"
        )
        pend = _dedupe(scope.get(check_id, []))
        inventory.append(
            {
                "id": check_id,
                "mod": module_id,
                "cat": module.CATEGORY.value,
                "pol": policy,
                "kind": kind,
                "n": counts.get(check_id, 0),
                "pend": pend["annotations"],
                "cases": pend,
                "title": module.TITLE,
                "desc": module.DESCRIPTION,
                "sev": dict(
                    Counter(i.severity.value for i in issues if i.check_id == check_id)
                ),
            }
        )
    return inventory


def family_summary(
    inventory: Sequence[dict[str, Any]],
    issues: Sequence[Issue],
    decisions: Sequence[SelectionDecision],
    config: Config,
) -> dict[str, dict[str, int]]:
    """M系 / D系 / S系 ごとの集計。

    画像枚数は check ごとの値を足すと**同じ画像を二重に数える**
    （1画像が同じ系統の複数 check に該当する）。必ず集合で数える。
    """
    owner = {c["id"]: c["cat"] for c in inventory}
    by_uid = {d.geometry_uid: d for d in decisions}
    scope: dict[str, dict[str, list[SelectionDecision]]] = {}

    for issue in issues:
        cat = owner.get(issue.check_id)
        uid = issue.geometry_uid
        if cat is None or uid is None:
            continue
        if not effective_review_required(issue.check_id, issue.status, config):
            continue
        decision = by_uid.get(uid)
        if decision is None or decision.final_decision is not Decision.PENDING:
            continue
        scope.setdefault(cat, {}).setdefault(uid, decision)

    result: dict[str, dict[str, int]] = {}
    for cat in {c["cat"] for c in inventory}:
        members = list(scope.get(cat, {}).values())
        cases = count_cases(members)
        result[cat] = {
            "checks": sum(1 for c in inventory if c["cat"] == cat),
            "issues": sum(c["n"] for c in inventory if c["cat"] == cat),
            "pending": cases["annotations"],
            "images": cases["images"],
            "studies": cases["studies"],
            "patients": cases["patients"],
        }
    return result


def label_composition(
    decisions: Sequence[SelectionDecision], config: Config
) -> dict[str, Any]:
    """データセットごとの病変ラベルの構成。

    「この3つのデータセットは気胸だけなのか」が一目で分かるようにする。
    実測では mask136 のみ100%気胸で、他2つは混在している。
    """
    keys, names = label_selectors(config.validation.target_labels)
    per: dict[str, dict[str, Any]] = {}
    for d in decisions:
        if d.annotation_type != "brush":
            continue
        entry = per.setdefault(d.dataset_id, {"total": 0, "in_target": 0, "labels": {}})
        entry["total"] += 1
        label = f"{d.code_system}/{d.code}" if d.code_system else "(ラベルなし)"
        text = d.code_text or label
        counts = entry["labels"].setdefault(label, {"n": 0, "text": text})
        counts["n"] += 1
        if not keys and not names:
            entry["in_target"] += 1
        elif label in keys or text in names:
            entry["in_target"] += 1
    for entry in per.values():
        entry["labels"] = dict(
            sorted(entry["labels"].items(), key=lambda kv: -kv[1]["n"])
        )
    return {
        "target_labels": list(config.validation.target_labels),
        "by_dataset": per,
    }


def build_payload(
    config: Config,
    issues: Sequence[Issue],
    decisions: Sequence[SelectionDecision],
    checks: Sequence[Any],
    masks: dict[tuple[str, str, str], MaskMeasurement],
    files: dict[str, FileMeasurement],
    fingerprint: str,
    population: dict[str, Any] | None = None,
    image_decisions: Sequence[Any] = (),
) -> dict[str, Any]:
    ds, inst, usr, code_text, reason, cid = (_Vocab() for _ in range(6))
    rows: list[list[Any]] = []
    for d in decisions:
        mask = masks.get((d.dataset_id, d.geometry_uid, ROLE_MASK))
        file_measurement = files.get(d.file_uid)
        rows.append(
            [
                d.geometry_uid,
                ds(d.dataset_id),
                inst(d.institution),
                d.study,
                d.file_id,
                TYPES.index(d.annotation_type),
                code_text(d.code_text or d.code),
                usr(d.user),
                (d.timestamp or "")[:10],
                [cid(c) for c in d.detected_checks.split("|") if c != "none"],
                [cid(c) for c in d.unverified_checks.split("|") if c != "none"],
                DECISIONS.index(d.final_decision.value),
                reason(d.reason),
                SOURCES.index(d.decision_source.value),
                d.max_severity,
                significant(mask.area_mm2) if mask else None,
                mask.n_components if mask else None,
                round(mask.lat_containment, 6)
                if mask and mask.lat_containment is not None
                else None,
                significant(mask.lat_outside_mm2) if mask else None,
                (d.kept_geometry_uid or "")[:8],
                d.duplicate_group_id or "",
                file_measurement.reference_status if file_measurement else "",
                d.patient_id,
            ]
        )

    inventory = check_inventory(checks, issues, decisions, config)
    thresholds = config.thresholds
    return {
        "meta": {
            "fingerprint": fingerprint,
            "scale": count_cases(decisions),
            "n_file_entries": len(files),
            # annotation を持たない画像の正体（正常例か未アノテーションか）も含む
            "population": population or {},
            "n_issues": len(issues),
            "sources": [p.name for p in config.dataset_sources()],
        },
        "vocab": {
            "ds": ds.values,
            "inst": inst.values,
            "usr": usr.values,
            "ct": code_text.values,
            "rsn": reason.values,
            "cid": cid.values,
            "fd": DECISIONS,
            "src": SOURCES,
            "ty": TYPES,
        },
        "cases": {
            "all": count_cases(decisions),
            # 症例単位の排他的な状態。「何症例が合格したか」の答え。
            # 採否ごとの症例数（by_decision）は重複するので合計が実数を超える。
            "status": count_case_status(decisions),
            "status_labels": {s.value: CASE_STATUS_JA[s] for s in CaseStatus},
            "status_order": [
                CaseStatus.PASSED.value,
                CaseStatus.PARTIAL.value,
                CaseStatus.ALL_EXCLUDED.value,
                CaseStatus.NEEDS_REVIEW.value,
            ],
            "pending": count_cases(
                [d for d in decisions if d.final_decision is Decision.PENDING]
            ),
            "by_decision": {
                name: count_cases(
                    [d for d in decisions if d.final_decision.value == name]
                )
                for name in DECISIONS
            },
        },
        "thresholds": {
            "tiny": thresholds.tiny_annotation_mm2,
            "byclass": thresholds.small_by_class_mm2,
            "band": thresholds.small_review_band_factor,
            "iou": thresholds.duplicate_iou_near,
            "cont": thresholds.duplicate_containment,
            "ob_err": thresholds.outside_body_error,
            "ob_warn": thresholds.outside_body_warn,
            "ob_mm2": thresholds.outside_body_warn_min_mm2,
            "margin": config.reference_masks.margin_mm,
            "cd_review": config.decision_policy.cannot_determine_as_review_required,
        },
        # 画像単位の採否。annotation を持たない画像はこちらでしか扱えない。
        "images": _image_block(image_decisions),
        "precision": _precision_block(issues, config),
        "labels": label_composition(decisions, config),
        "checks": inventory,
        "families": family_summary(inventory, issues, decisions, config),
        "rows": rows,
        "issues": [
            [
                cid(i.check_id),
                i.geometry_uid or "",
                i.severity.value[0],
                i.status.value[0],
                i.message,
                json.dumps(i.detail, ensure_ascii=False),
            ]
            for i in issues
        ],
    }


def _precision_block(issues: Sequence[Issue], config: Config) -> dict[str, Any]:
    """自動ルールの Precision。目視前は分母だけが埋まる。"""
    from .precision import compute_precision, read_verdicts, summarize

    review = config.validation_dir / "review" / "review_decisions.json"
    # fingerprint 配下が正だが、無ければ空で計算する（目視前は0件）。
    for candidate in config.validation_dir.glob("*/review/review_decisions.json"):
        review = candidate
        break
    verdicts = read_verdicts(review)
    results = compute_precision(issues, verdicts, config)
    return {
        "summary": summarize(results),
        "rows": [
            {
                "check_id": r.check_id,
                "detected": r.detected,
                "reviewed": r.reviewed,
                "excluded": r.excluded,
                "kept": r.kept,
                "uncertain": r.uncertain,
                "precision": r.precision,
            }
            for r in results
        ],
    }


def _image_block(image_decisions: Sequence[Any]) -> dict[str, Any]:
    """画像単位の採否をダッシュボード用に整える。"""
    if not image_decisions:
        return {}
    from ..selection.image_decisions import summarize_images

    summary = summarize_images(list(image_decisions))
    pending = [
        {
            "file_uid": d.file_uid,
            "ds": d.dataset_id,
            "inst": d.institution,
            "patient": d.patient_id,
            "study": d.study,
            "file": d.file_id,
            "cls": d.image_class,
            "cls_ja": d.image_class_ja,
            "checks": d.detected_checks,
        }
        for d in image_decisions
        if d.final_decision.value in ("pending", "uncertain")
    ]
    labels = {d.image_class: d.image_class_ja for d in image_decisions}
    return {"summary": summary, "pending": pending, "class_labels": labels}


def write_dashboard(path: Path, payload: dict[str, Any], config: Config) -> None:
    """テンプレートとデータを1枚のHTMLへ組み立てる。"""
    assets = config.project_root / ASSET_DIR
    missing = [
        name
        for name in ("shell-head.html", "shell-body.html", "app.js", "charts.js")
        if not (assets / name).exists()
    ]
    if missing:
        raise FileNotFoundError(f"{assets} に不足: {missing}")

    parts = [
        (assets / "shell-head.html").read_text(encoding="utf-8"),
        (assets / "shell-body.html").read_text(encoding="utf-8"),
        '<script type="application/json" id="payload">',
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "</script>",
        "<script>",
        (assets / "app.js").read_text(encoding="utf-8"),
        (assets / "charts.js").read_text(encoding="utf-8"),
        "</script>",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def _dedupe(decisions: Iterable[SelectionDecision]) -> dict[str, int]:
    """同じ annotation を二重に数えないようにしてから症例数を出す。"""
    seen: dict[str, SelectionDecision] = {}
    for d in decisions:
        seen.setdefault(d.geometry_uid, d)
    return count_cases(seen.values())
