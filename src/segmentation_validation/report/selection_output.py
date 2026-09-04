"""``selection_decisions.json`` / ``.csv`` の書き出しと読み込み。

採否の唯一の正本。``build-dataset`` はこれだけを見る
（issues.csv も FiftyOne DB も参照しない）。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..selection.decisions import COLUMNS, SelectionDecision, summarize


def write_selection_csv(path: Path, decisions: Sequence[SelectionDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for decision in decisions:
            writer.writerow(_to_row(decision))


def write_selection_json(
    path: Path, decisions: Sequence[SelectionDecision], meta: dict[str, Any]
) -> None:
    payload = {
        "meta": {**meta, "generated_at": datetime.now(timezone.utc).isoformat()},
        "summary": summarize(list(decisions)),
        "decisions": [_to_row(decision) for decision in decisions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_selection_json(path: Path) -> list[dict[str, Any]]:
    """``build-dataset`` が採否を読み戻す。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["decisions"]


def _to_row(decision: SelectionDecision) -> dict[str, Any]:
    data = asdict(decision)
    for key in ("review_status", "final_decision", "decision_source"):
        data[key] = getattr(decision, key).value
    return {column: _blank_none(data[column]) for column in COLUMNS}


def _blank_none(value: Any) -> Any:
    """CSVで ``None`` が文字列 ``"None"`` になるのを防ぐ。"""
    return "" if value is None else value
