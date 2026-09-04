"""``review_decisions.json`` を FiftyOne へ流し込む。fiftyone を import する。

``FiftyOne DB削除 -> dataset再構築 -> import -> 判定が戻る`` を成立させるための片側。
（もう片側は ``review build`` が manifest 経由で判定を載せる経路。
manifest は ``select`` が反映済みの採否を持つので、通常はそちらで戻る。
このコマンドは「DBだけ作り直した」「別環境の判定を持ち込む」場合に使う。）

``review:`` タグと ``review_*`` フィールドの両方を更新する。
フィールドが正、タグは絞り込み用の写し。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import Config
from .fiftyone_builder import configure_database
from .review_schema import (
    FIELD_FINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
    is_review,
    review_tag,
)

logger = logging.getLogger(__name__)


def import_decisions(path: Path, config: Config) -> dict[str, int]:
    """判定を dataset へ書き戻す。``auto:`` タグは触らない。"""
    configure_database(config)
    import fiftyone as fo

    payload = json.loads(path.read_text(encoding="utf-8"))
    by_uid = {d["geometry_uid"]: d for d in payload.get("decisions", [])}
    by_file = {d["file_uid"]: d for d in payload.get("image_decisions", [])}

    name = config.review.dataset_name
    if name not in fo.list_datasets():
        raise RuntimeError(f"FiftyOne dataset '{name}' が無い")
    dataset = fo.load_dataset(name)

    applied = {"annotations": 0, "images": 0}
    for sample in dataset.iter_samples(autosave=True, progress=False):
        verdict = by_file.get(sample.file_uid)
        if verdict is not None:
            _apply(sample, verdict)
            applied["images"] += 1

        detections = sample.get_field(FIELD_FINAL)
        for detection in detections.detections if detections else []:
            uid = detection.get_field("geometry_uid")
            found = by_uid.get(uid) if uid else None
            if found is not None:
                _apply(detection, found)
                applied["annotations"] += 1

    logger.info(
        "判定を書き戻した: annotation %d / 画像 %d",
        applied["annotations"],
        applied["images"],
    )
    return applied


def _apply(holder: Any, verdict: dict[str, Any]) -> None:
    """1件の判定をフィールドとタグへ反映する。"""
    status = verdict["decision"]
    holder[FIELD_REVIEW_STATUS] = status
    holder[FIELD_REVIEW_REASON] = verdict.get("reason") or ""
    holder[FIELD_REVIEWER] = verdict.get("reviewer") or ""
    holder[FIELD_REVIEWED_AT] = verdict.get("reviewed_at") or ""
    holder[FIELD_REVIEW_COMMENT] = verdict.get("comment") or ""
    # review: タグは張り替える。auto: タグは機械のものなので保持する。
    kept = [t for t in (holder.tags or []) if not is_review(t)]
    holder.tags = kept + [review_tag(status)]
