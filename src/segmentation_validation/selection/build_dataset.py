"""開発用データセット JSON の生成。

**元JSONは変更しない。** 入力は元JSONと ``selection_decisions`` の2つだけで、
``issues.csv`` も FiftyOne DB も見ない —— 採否の正本を1つに絞るため。

``final_decision = keep`` の annotation だけを残す。
``exclude`` の結果 annotation が0件になった file entry は**削除せず
``annotations: []`` として残す**。取り除くと annotation の除外によって
元の画像母集団が黙って変わってしまう。

★ただし ``image_decisions`` で画像そのものが ``exclude`` と判定された場合は
**file entry ごと落とす**。これは「画像を落とす」という明示的な判断があった場合
（側面像など）だけで、annotation の除外による副作用ではない。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config import Config
from .decisions import Decision

logger = logging.getLogger(__name__)


class PendingPolicy:
    """pending / uncertain を override したときの扱い。既定は無し（停止）。"""

    EXCLUDE = "exclude"
    KEEP = "keep"


@dataclass
class BuildResult:
    """生成結果。件数は必ずレポートへ出す。"""

    path: Path
    kept: int = 0
    images_total: int = 0
    images_dropped: int = 0
    images_pending: int = 0
    excluded: int = 0
    pending: int = 0
    uncertain: int = 0
    unknown: int = 0
    files_total: int = 0
    files_emptied: int = 0
    files_already_empty: int = 0
    original_unchanged: bool = True
    overrides: dict[str, str] = field(default_factory=dict)


def build_development_json(
    source_path: Path,
    decisions: Mapping[str, str],
    out_path: Path,
    config: Config,
    pending_as: str | None = None,
    uncertain_as: str | None = None,
    meta_extra: dict[str, Any] | None = None,
    image_decisions: Mapping[str, str] | None = None,
) -> BuildResult:
    """1つの元JSONから開発用JSONを作る。

    ``decisions`` は ``geometry_uid -> final_decision``、
    ``image_decisions`` は ``file_uid -> final_decision``。
    ``pending_as`` / ``uncertain_as`` は override が指定されたときだけ渡す。
    未指定のまま該当が残っていれば呼び出し側で止めるのが前提だが、
    ここでも黙って落とさないよう ``unknown`` として数える。
    """
    raw = source_path.read_bytes()
    before_digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))

    result = BuildResult(path=out_path)
    if pending_as:
        result.overrides["pending_as"] = pending_as
    if uncertain_as:
        result.overrides["uncertain_as"] = uncertain_as

    images = image_decisions or {}
    source_json = source_path.name
    for institution, studies in payload.get("dataset", {}).items():
        for study_key, study in studies.items():
            for series_key, series in study.get("series_list", {}).items():
                file_list = series.get("file_list", {})
                # 画像そのものを落とすと判定されたものを先に取り除く。
                dropped = []
                for file_key in list(file_list):
                    uid = (
                        f"{source_json}::{institution}"
                        f"/{study_key}/{series_key}/{file_key}"
                    )
                    verdict = images.get(uid)
                    result.images_total += 1
                    if verdict == Decision.PENDING.value:
                        result.images_pending += 1
                    if verdict == Decision.EXCLUDE.value:
                        dropped.append(file_key)
                for file_key in dropped:
                    del file_list[file_key]
                    result.images_dropped += 1

                for file_rec in file_list.values():
                    result.files_total += 1
                    original = file_rec.get("annotations") or []
                    kept: list[dict[str, Any]] = []
                    for annotation in original:
                        uid = str(annotation.get("geometry_uid"))
                        if _keep(decisions.get(uid), pending_as, uncertain_as, result):
                            kept.append(annotation)
                    # 0件になっても file entry は残す（母集団を変えないため）。
                    file_rec["annotations"] = kept
                    if original and not kept:
                        # 元から空だったファイルは数えない。
                        # 「除外の結果0件になった」件数でなければ意味がない。
                        result.files_emptied += 1
                    elif not original:
                        result.files_already_empty += 1

    payload["meta_development"] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_json": source_path.name,
        "source_sha256": before_digest,
        "tool_version": _tool_version(),
        "kept": result.kept,
        "excluded": result.excluded,
        "pending": result.pending,
        "uncertain": result.uncertain,
        "unknown": result.unknown,
        "files_total": result.files_total,
        "files_emptied": result.files_emptied,
        "files_already_empty": result.files_already_empty,
        "images_total": result.images_total,
        "images_dropped": result.images_dropped,
        "overrides": result.overrides,
        **(meta_extra or {}),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 元JSONを触っていないことをハッシュで確認する。
    after_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    result.original_unchanged = after_digest == before_digest
    if not result.original_unchanged:
        logger.error("元JSONが変化している: %s", source_path)
    return result


def _keep(
    decision: str | None,
    pending_as: str | None,
    uncertain_as: str | None,
    result: BuildResult,
) -> bool:
    if decision == Decision.KEEP.value:
        result.kept += 1
        return True
    if decision == Decision.EXCLUDE.value:
        result.excluded += 1
        return False
    if decision == Decision.PENDING.value:
        result.pending += 1
        return _apply_override(pending_as, result)
    if decision == Decision.UNCERTAIN.value:
        result.uncertain += 1
        return _apply_override(uncertain_as, result)

    # 採否マスタに無い annotation。全件1行あるはずなので、あってはならない。
    result.unknown += 1
    logger.error("採否が不明な annotation を除外した（decision=%r）", decision)
    return False


def _apply_override(policy: str | None, result: BuildResult) -> bool:
    if policy == PendingPolicy.KEEP:
        return True
    if policy == PendingPolicy.EXCLUDE:
        return False
    # override 無しでここに来るのは呼び出し側の不備。安全側に落とす。
    result.unknown += 1
    return False


def _tool_version() -> str:
    from .. import __version__

    return __version__
