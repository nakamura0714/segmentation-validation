"""manifest から FiftyOne dataset を組む。**fiftyone を import する唯一のモジュール**。

ここと ``export_decisions`` / ``import_decisions`` の3つ以外は fiftyone に依存しない。
FiftyOne が入っていなくても ``scan`` / ``check`` / ``select`` / ``report`` /
``build-dataset`` はすべて動く。

再構築の規則:

- ``auto:`` タグは**毎回貼り直す**（閾値を変えて再検出したら付け替わるべき）
- ``review:`` タグと ``review_*`` フィールドは**人間のものなので上書きしない**。
  manifest 経由で ``review_decisions.json`` の内容を流し込む形にしてあるので、
  DB を消して作り直しても判定が戻る
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..config import Config
from .review_schema import (
    FIELD_BAND,
    FIELD_FINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
    ReviewStatusTag,
    review_tag,
)

logger = logging.getLogger(__name__)


def configure_database(config: Config) -> str:
    """mongod のデータ置き場を決める。**NFS 上に置くと壊れる**。

    優先順位は config → 環境変数 → ``$HOME/.fiftyone/var/lib/mongo``。
    ``/mnt`` は NFS なので既定では選ばない。
    """
    target = config.review.database_dir or os.environ.get("FIFTYONE_DATABASE_DIR")
    if not target:
        target = str(Path.home() / ".fiftyone" / "var" / "lib" / "mongo")
    Path(target).mkdir(parents=True, exist_ok=True)
    os.environ["FIFTYONE_DATABASE_DIR"] = target
    return target


def build_dataset(manifest: dict[str, Any], config: Config, overwrite: bool = True):
    """manifest から dataset を作る。

    - 1 Sample = 1画像。``review_status`` は**画像自体の採否**
      （未アノテーションのビューが側面像かどうか）
    - 1 Detection = 1 annotation。``review_status`` は**その annotation の採否**。
      1枚に複数あるので、annotation の良否は Sample ではなく Label に持たせる
    """
    configure_database(config)
    import fiftyone as fo

    name = config.review.dataset_name
    dataset = fo.Dataset(name, overwrite=overwrite, persistent=True)
    dataset.description = (
        "ChestMetry PI6 セグメンテーション検証の目視レビュー。"
        "auto: タグは機械の検出、review: タグと review_* フィールドは人間の判定。"
    )

    samples = []
    for image in manifest["images"]:
        sample = fo.Sample(filepath=image["filepath"])
        for key in (
            "file_uid",
            "dataset_id",
            "institution",
            "patient_id",
            "study",
            "series",
            "file_id",
            "image_class",
            "image_class_ja",
        ):
            sample[key] = image[key]

        # 画像単位の判定。annotation を持たない画像はここでしか扱えない。
        sample[FIELD_REVIEW_STATUS] = image["review_status"]
        sample[FIELD_REVIEW_REASON] = image["review_reason"]
        sample[FIELD_REVIEWER] = image["reviewer"]
        sample[FIELD_REVIEWED_AT] = image["reviewed_at"]
        sample[FIELD_REVIEW_COMMENT] = ""
        sample["n_annotations"] = len(image["annotations"])
        sample.tags = list(image["auto_tags"]) + [review_tag(image["review_status"])]

        if image.get("band_path"):
            sample[FIELD_BAND] = fo.Segmentation(mask_path=image["band_path"])

        detections = []
        for annotation in image["annotations"]:
            detection = fo.Detection(
                label=annotation["label"],
                bounding_box=annotation["bounding_box"],
            )
            if annotation.get("mask_path"):
                detection.mask_path = annotation["mask_path"]
            detection.tags = list(annotation["auto_tags"]) + [
                review_tag(annotation["review_status"])
            ]
            detection[FIELD_REVIEW_STATUS] = annotation["review_status"]
            detection[FIELD_REVIEW_REASON] = annotation["review_reason"]
            detection[FIELD_REVIEWER] = annotation["reviewer"]
            detection[FIELD_REVIEWED_AT] = annotation["reviewed_at"]
            detection[FIELD_REVIEW_COMMENT] = ""
            for key, value in annotation["attributes"].items():
                detection[key] = value
            # Issue の中身を1つの文字列にして App から読めるようにする。
            detection["issues"] = " / ".join(
                f"{i['check_id']}({i['severity']}): {i['message']}"
                for i in annotation["issues"]
            )
            detections.append(detection)

        sample[FIELD_FINAL] = fo.Detections(detections=detections)
        samples.append(sample)

    dataset.add_samples(samples)
    _save_views(dataset, config)
    logger.info(
        "FiftyOne dataset '%s': %d Sample / %d Detection",
        name,
        len(dataset),
        dataset.count(f"{FIELD_FINAL}.detections"),
    )
    return dataset


def _save_views(dataset, config: Config) -> None:
    """App の左サイドバーに出る保存ビュー。目視の入口を用意する。

    ★名前は ASCII を含めること。FiftyOne はビュー名を slug 化するので、
    日本語だけの名前は空の slug になって ``ValueError`` で失敗する。
    日本語は description 側に置く。
    """
    from fiftyone import ViewField as F

    pending = ReviewStatusTag.PENDING.value

    def save(name: str, view, description: str) -> None:
        try:
            dataset.save_view(name, view, description=description, overwrite=True)
        except Exception as error:
            # 黙って落とすと「ビューが無い」ことに気付けない。
            logger.warning("保存ビューを作れない %s: %s", name, error)

    save(
        "1-pending-annotations",
        dataset.filter_labels(FIELD_FINAL, F(FIELD_REVIEW_STATUS) == pending),
        "目視待ちの annotation だけを残した Detection ビュー",
    )
    save(
        "2-pending-images",
        dataset.match(F(FIELD_REVIEW_STATUS) == pending),
        "目視待ちの画像（未アノテーションのビュー）。側面像なら除外が必要",
    )
    save(
        "3-outside-body",
        dataset.filter_labels(FIELD_FINAL, F("tags").contains("auto:s05_outside_body")),
        "体外領域。胸郭の側方バンドからはみ出している annotation",
    )
    save(
        "4-tiny-region",
        dataset.filter_labels(
            FIELD_FINAL,
            F("tags").contains("auto:s03_suspiciously_small")
            | F("tags").contains("auto:s03_tiny_annotation")
            | F("tags").contains("auto:s03_stray_component"),
        ),
        "微小領域。ここでの判定が S03 の閾値を決める",
    )
    save(
        "5-contained-different-label",
        dataset.filter_labels(
            FIELD_FINAL, F("tags").contains("auto:d04_contained_different_label")
        ),
        "包含された重複（別ラベル）。入れ子の所見として正当な可能性が高い",
    )
    save(
        "6-broken-bbox",
        dataset.filter_labels(FIELD_FINAL, F("display_adjusted") != None),  # noqa: E711
        "座標が壊れている bbox。表示のために枠を広げてある（original_bbox が原座標）",
    )
    logger.info("保存ビュー: %s", dataset.list_saved_views())


def dataset_summary(config: Config) -> dict[str, Any]:
    """現在の dataset の状態。``review status`` で表示する。"""
    configure_database(config)
    import fiftyone as fo

    name = config.review.dataset_name
    if name not in fo.list_datasets():
        return {}
    dataset = fo.load_dataset(name)
    return {
        "name": name,
        "n_samples": len(dataset),
        "n_detections": dataset.count(f"{FIELD_FINAL}.detections"),
        "label_tags": dataset.count_label_tags(),
        "sample_tags": dataset.count_sample_tags(),
        "annotation_status": dataset.count_values(
            f"{FIELD_FINAL}.detections.{FIELD_REVIEW_STATUS}"
        ),
        "image_status": dataset.count_values(FIELD_REVIEW_STATUS),
    }


def launch_app(config: Config, wait: bool = True) -> None:
    """App を localhost で起動する。

    インターネットへ直接公開しない。Windows からは SSH port forwarding で見る::

        ssh -L 5151:localhost:5151 <踏み台> -t ssh -L 5151:localhost:5151 <本サーバー>
    """
    configure_database(config)
    import fiftyone as fo

    port = config.review.app_port
    dataset = fo.load_dataset(config.review.dataset_name)
    logger.info("App を起動する: http://localhost:%d", port)
    logger.info(
        "Windows からは SSH port forwarding で見る "
        "（ssh -L %d:localhost:%d ...）。インターネットへ公開しないこと",
        port,
        port,
    )
    session = fo.launch_app(dataset, port=port, remote=True)
    if wait:
        session.wait()
