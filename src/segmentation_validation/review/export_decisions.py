"""FiftyOne の判定を ``review_decisions.json`` へ書き出す。fiftyone を import する。

**FiftyOne の MongoDB を正本にしない。** これが成立するために必要な性質:

    FiftyOne DB削除 -> dataset再構築 -> review_decisions.json import -> 判定が戻る

キーは ``geometry_uid``（annotation）と ``file_uid``（画像）。どちらも全域一意なので、
新しいJSONが追加されて再スキャンしても対応が壊れない。

判定の読み取りは **``review_status`` フィールドを正**とし、``review:`` タグは
フィルタ用の写しとして扱う。タグは文字列の集合で取り違えやすいため。
ただしフィールドが ``pending`` のままタグだけ変わっている場合は
（App でタグ付けだけした場合）タグを採用する。

★**機械の判定を「人間の判定」として書き出してはいけない。**
dataset には目視対象の周辺 annotation も文脈として載っており、それらは
D01 の自動 exclude や既定の keep を持っている。全部書き出すと ``select`` 側で
``decision_source=human`` になってしまう。そこで **manifest を基準線**にして、
そこから変わったもの（または reviewer が入っているもの）だけを人間の判定とする。
reviewer に依存しないのは、名前の入力を忘れても判定は有効であるべきだから。
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from .fiftyone_builder import configure_database
from .review_schema import (
    DECIDED,
    FIELD_FINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
)
from .review_schema import effective_status as _effective_status

logger = logging.getLogger(__name__)

COLUMNS = (
    "kind",
    "key",
    "decision",
    "reason",
    "reviewer",
    "reviewed_at",
    "comment",
)


def collect_decisions(
    config: Config, manifest: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """dataset から**人間の判定だけ**を集める。

    ``pending`` は「まだ判定していない」なので書き出さない
    （書き出すと ``select`` 側で「人間が pending と判定した」ことになってしまう）。

    ``manifest`` を渡すと、そこに記録された採否を基準線として
    **変化した分だけ**を人間の判定とみなす。渡さない場合は reviewer の有無で判断する。
    """
    baseline_ann, baseline_img = _baseline(manifest)
    configure_database(config)
    import fiftyone as fo

    name = config.review.dataset_name
    if name not in fo.list_datasets():
        raise RuntimeError(
            f"FiftyOne dataset '{name}' が無い。先に `review build` を実行する"
        )
    dataset = fo.load_dataset(name)

    rows: list[dict[str, Any]] = []
    for sample in dataset.select_fields(
        [
            "file_uid",
            FIELD_REVIEW_STATUS,
            FIELD_REVIEW_REASON,
            FIELD_REVIEWER,
            FIELD_REVIEWED_AT,
            FIELD_REVIEW_COMMENT,
            "tags",
            FIELD_FINAL,
        ]
    ):
        status = _effective_status(
            sample.get_field(FIELD_REVIEW_STATUS), list(sample.tags or [])
        )
        row = _row("image", sample.file_uid, status, sample)
        if status in DECIDED and _is_human(
            row, baseline_img.get(sample.file_uid), manifest
        ):
            rows.append(row)

        detections = sample.get_field(FIELD_FINAL)
        for detection in detections.detections if detections else []:
            uid = detection.get_field("geometry_uid")
            if not uid:
                continue
            label_status = _effective_status(
                detection.get_field(FIELD_REVIEW_STATUS), list(detection.tags or [])
            )
            row = _row("annotation", uid, label_status, detection)
            if label_status in DECIDED and _is_human(
                row, baseline_ann.get(uid), manifest
            ):
                rows.append(row)

    unnamed = [r for r in rows if not r["reviewer"]]
    if unnamed:
        # 判定自体は有効なので落とさない。ただし誰が判定したか分からないと
        # 後から追えないので警告し、埋めておく。
        logger.warning(
            "reviewer が未入力の判定が %d 件ある（'unknown' で記録する）", len(unnamed)
        )
        for row in unnamed:
            row["reviewer"] = "unknown"
    return rows


def unexported_human_decisions(
    config: Config, manifest: dict[str, Any] | None, exported_path: Path
) -> list[dict[str, Any]]:
    """DB にはあるが ``review_decisions.json`` に無い人間の判定を返す。

    ``review build`` は dataset を ``overwrite=True`` で作り直すので、
    **export していない判定は消える**。呼び出し側はこれが空でないときに停止する。

    ``manifest`` は **DB を作ったときのもの**（作り直す前にディスクにある版）を渡す。
    新しい manifest を基準線にすると「変化なし」に見えて判定を検出できない。
    """
    try:
        rows = collect_decisions(config, manifest)
    except (RuntimeError, ImportError):
        # dataset がまだ無い / fiftyone が無い。守るものが無いので空。
        return []
    exported = _exported_keys(exported_path)
    return [row for row in rows if (row["kind"], row["key"]) not in exported]


def write_decisions(path: Path, rows: list[dict[str, Any]], config: Config) -> None:
    """``rows``（今回スキャンできた人間の判定）を既存ファイルとマージして書く。

    ``review build``（``--all`` 無し）は pending の無い画像/annotationを
    FiftyOne から除外するため、一度確定した判定が次の ``collect_decisions``
    のスキャン範囲から外れて見えなくなることがある。見えなくなったからと
    いって判定が無効になったわけではないので、**上書きしない**。
    既存ファイルにしか無いキーはそのまま残し、両方にあるキーは今回スキャン
    した方（＝より新しい状態）を優先する。これでモジュール冒頭の docstring
    にある不変条件（DB削除→再構築→import で判定が戻る）を rebuild を挟んでも
    保てる。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {
        (row["kind"], row["key"]): row for row in _read_existing_rows(path)
    }
    for row in rows:
        merged[(row["kind"], row["key"])] = row
    all_rows = list(merged.values())

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset_name": config.review.dataset_name,
            "n_annotations": sum(1 for r in all_rows if r["kind"] == "annotation"),
            "n_images": sum(1 for r in all_rows if r["kind"] == "image"),
        },
        # ``select`` が読む形。annotation は geometry_uid、画像は file_uid がキー。
        "decisions": [
            {
                "geometry_uid": r["key"],
                "decision": r["decision"],
                "reason": r["reason"],
                "reviewer": r["reviewer"],
                "reviewed_at": r["reviewed_at"],
                "comment": r["comment"],
            }
            for r in all_rows
            if r["kind"] == "annotation"
        ],
        "image_decisions": [
            {
                "file_uid": r["key"],
                "decision": r["decision"],
                "reason": r["reason"],
                "reviewer": r["reviewer"],
                "reviewed_at": r["reviewed_at"],
                "comment": r["comment"],
            }
            for r in all_rows
            if r["kind"] == "image"
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_path = path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)


def _read_existing_rows(path: Path) -> list[dict[str, Any]]:
    """既存の ``review_decisions.json`` を ``collect_decisions`` と同じ行の形へ戻す。

    マージ前提の読み戻しなので、読めない/無い場合は空扱いにする
    （``_exported_keys`` と同じ防御）。
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("%s を読めない。既存の判定は無いものとして扱う", path)
        return []

    def to_row(kind: str, key_field: str, d: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": kind,
            "key": d[key_field],
            "decision": d["decision"],
            "reason": d.get("reason", ""),
            "reviewer": d.get("reviewer", ""),
            "reviewed_at": d.get("reviewed_at", ""),
            "comment": d.get("comment", ""),
        }

    rows = [to_row("annotation", "geometry_uid", d) for d in payload.get("decisions", [])]
    rows += [to_row("image", "file_uid", d) for d in payload.get("image_decisions", [])]
    return rows


# ------------------------------------------------------------------ internal


def _exported_keys(path: Path) -> set[tuple[str, str]]:
    """``review_decisions.json`` に既に書き出されている判定のキー。"""
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 読めないファイルを「全部 export 済み」と解釈してはいけない。
        logger.warning("%s を読めない。未 export 扱いにする", path)
        return set()
    keys = {("annotation", d["geometry_uid"]) for d in payload.get("decisions", [])}
    keys |= {("image", d["file_uid"]) for d in payload.get("image_decisions", [])}
    return keys


def _baseline(
    manifest: dict[str, Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """manifest に記録された採否。これと違えば人間が変えたということ。"""
    if not manifest:
        return {}, {}
    annotations: dict[str, str] = {}
    images: dict[str, str] = {}
    for image in manifest.get("images", []):
        images[image["file_uid"]] = image["review_status"]
        for annotation in image.get("annotations", []):
            annotations[annotation["geometry_uid"]] = annotation["review_status"]
    return annotations, images


def _is_human(
    row: dict[str, Any], baseline: str | None, manifest: dict[str, Any] | None
) -> bool:
    """この判定は人間が入れたものか。

    manifest があれば基準線からの変化で判断する（reviewer の入力漏れに強い）。
    無ければ reviewer が入っているかで判断する。
    """
    if row["reviewer"]:
        return True
    if manifest is None:
        return False
    return baseline is not None and row["decision"] != baseline


def _row(kind: str, key: str, status: str, holder: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "decision": status,
        "reason": str(holder.get_field(FIELD_REVIEW_REASON) or ""),
        "reviewer": str(holder.get_field(FIELD_REVIEWER) or ""),
        "reviewed_at": str(holder.get_field(FIELD_REVIEWED_AT) or ""),
        "comment": str(holder.get_field(FIELD_REVIEW_COMMENT) or ""),
    }
