"""``review_decisions.json`` を FiftyOne へ流し込む。fiftyone を import する。

``FiftyOne DB削除 -> dataset再構築 -> import -> 判定が戻る`` を成立させるための片側。
（もう片側は ``review build`` が manifest 経由で判定を載せる経路。
manifest は ``select`` が反映済みの採否を持つので、通常はそちらで戻る。
このコマンドは「DBだけ作り直した」「別環境の判定を持ち込む」場合に使う。）

``review:`` タグと ``review_*`` フィールドの両方を更新する。
フィールドが正、タグは絞り込み用の写し。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import Config
from . import decision_store
from .fiftyone_builder import configure_database
from .review_schema import (
    FIELD_FINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_REASONS,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
    effective_reasons,
    is_review,
    reason_tags,
    review_tag,
)

logger = logging.getLogger(__name__)


def import_decisions(path: Path, config: Config) -> dict[str, int]:
    """判定を dataset へ書き戻す。``auto:`` タグは触らない。"""
    configure_database(config)
    import fiftyone as fo

    # annotation は (dataset_id, geometry_uid) で突合する。geometry_uid は
    # cross-dataset 重複でデータセットをまたいで再利用されるため、bare
    # geometry_uid だけで突合すると無関係な別データセットの annotation に
    # 判定が誤って書き戻される。dataset_id を持たない旧形式の行は無視する。
    #
    # 画像は stable_file_uid（dataset_id + inst/study/series/file）で突合する。
    # 旧形式の file_uid は decision_store が読み込み時に昇格する。
    rows = decision_store.load_rows(path)
    by_uid = {
        (row["dataset_id"], row["key"]): row
        for row in rows
        if row["kind"] == decision_store.KIND_ANNOTATION and row.get("dataset_id")
    }
    by_file = {
        decision_store.stable_uid(row): row
        for row in rows
        if row["kind"] == decision_store.KIND_IMAGE
    }

    name = config.review.dataset_name
    if name not in fo.list_datasets():
        raise RuntimeError(f"FiftyOne dataset '{name}' が無い")
    dataset = fo.load_dataset(name)

    applied = {"annotations": 0, "images": 0}
    for sample in dataset.iter_samples(autosave=True, progress=False):
        # ★dataset_id は Sample 側のフィールド。Detection には無い。
        dataset_id = sample.get_field("dataset_id") or ""

        parts = decision_store.split_file_uid(sample.file_uid)
        image_uid = f"{dataset_id}::{parts[1]}" if parts else sample.file_uid
        verdict = by_file.get(image_uid)
        if verdict is not None:
            _apply(sample, verdict)
            applied["images"] += 1

        detections = sample.get_field(FIELD_FINAL)
        for detection in detections.detections if detections else []:
            uid = detection.get_field("geometry_uid")
            found = by_uid.get((dataset_id, uid)) if uid else None
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
    # 理由は候補に当てはまるものを review_reasons へ、当てはまらない記述だけを
    # 自由記述欄へ入れる。``review_decisions.json`` の reason は "a|b" の形。
    raw_reason = verdict.get("reason") or ""
    reasons = effective_reasons(raw_reason)
    holder[FIELD_REVIEW_STATUS] = status
    holder[FIELD_REVIEW_REASONS] = reasons
    holder[FIELD_REVIEW_REASON] = "" if reasons else raw_reason
    holder[FIELD_REVIEWER] = verdict.get("reviewer") or ""
    holder[FIELD_REVIEWED_AT] = verdict.get("reviewed_at") or ""
    holder[FIELD_REVIEW_COMMENT] = verdict.get("comment") or ""
    # review: と reason: のタグは張り替える。auto: は機械のものなので保持する。
    # ★reason: を貼り直すのが要る。is_review() は reason: も人間のものと見なす
    # ので、貼り直さないと import のたびに理由タグが消えていた。
    kept = [t for t in (holder.tags or []) if not is_review(t)]
    holder.tags = kept + [review_tag(status)] + reason_tags(reasons)
