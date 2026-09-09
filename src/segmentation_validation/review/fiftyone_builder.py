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
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import Config
from .review_schema import (
    ALL_SUGGESTED_REASONS,
    FIELD_BAND,
    FIELD_FINAL,
    FIELD_ORIGINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_REASONS,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
    FLAG_NEEDS_REPORT,
    SCHEMA_SEED_TAG,
    ReviewStatusTag,
    effective_status,
    reason_tags,
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
            "series_image_index",
            "series_image_count",
            "auto_decision_reason",
            "override_id",
            "override_note",
            "override_approved_by",
            "override_approved_at",
        ):
            sample[key] = image[key]

        # 画像単位の判定。annotation を持たない画像はここでしか扱えない。
        sample[FIELD_REVIEW_STATUS] = image["review_status"]
        sample[FIELD_REVIEW_REASONS] = list(image["review_reasons"])
        sample[FIELD_REVIEW_REASON] = image["review_reason"]
        sample[FIELD_REVIEWER] = image["reviewer"]
        sample[FIELD_REVIEWED_AT] = image["reviewed_at"]
        sample[FIELD_REVIEW_COMMENT] = ""
        sample["n_annotations"] = len(image["annotations"])
        # reason: タグは review_reasons の写し。絞り込みと保存ビューのために併記する
        # （正本はフィールド側。review_status と review: タグの関係と同じ）。
        # spot_check: 未アノテーション画像のデータセット別スポットチェックで
        # 選ばれた画像の印（採否には影響しない）。
        sample.tags = (
            list(image["auto_tags"])
            + [review_tag(image["review_status"])]
            + reason_tags(image["review_reasons"])
            + (["spot_check"] if image["spot_check"] else [])
        )

        if image.get("band_path"):
            sample[FIELD_BAND] = fo.Segmentation(mask_path=image["band_path"])

        detections = []
        original_detections = []
        for annotation in image["annotations"]:
            detection = fo.Detection(
                label=annotation["label"],
                bounding_box=annotation["bounding_box"],
            )
            if annotation.get("mask_path"):
                detection.mask_path = annotation["mask_path"]
            detection.tags = (
                list(annotation["auto_tags"])
                + [review_tag(annotation["review_status"])]
                + reason_tags(annotation["review_reasons"])
            )
            detection[FIELD_REVIEW_STATUS] = annotation["review_status"]
            detection[FIELD_REVIEW_REASONS] = list(annotation["review_reasons"])
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

            # S04（修正前後の食い違い）目視用。final とは別レイヤーなので
            # 採否フィールドは持たない —— 判定は final 側の annotation に対して行う。
            if annotation.get("original_mask_path"):
                original_detection = fo.Detection(
                    label=annotation["label"],
                    bounding_box=annotation["original_bounding_box"],
                    mask_path=annotation["original_mask_path"],
                )
                original_detection["geometry_uid"] = annotation["geometry_uid"]
                original_detections.append(original_detection)

        sample[FIELD_FINAL] = fo.Detections(detections=detections)
        if original_detections:
            sample[FIELD_ORIGINAL] = fo.Detections(detections=original_detections)
        samples.append(sample)

    if manifest["images"]:
        samples.extend(_seed_choice_candidates(manifest["images"][0]["filepath"]))

    _declare_reason_field(dataset)
    # ``dynamic=True`` が要る。既定の False だと timestamp / review_status /
    # newer_in_pair などの動的属性が **field schema に宣言されない**。App の
    # Edit パネルもサイドバーも宣言済みフィールドしか見ないので、値が入っている
    # のに一切表示されない（実機で確認済み。まさにこれが起きていた）。
    dataset.add_samples(samples, dynamic=True)
    _apply_label_schema(dataset)
    _save_views(dataset, config)
    # FIELD_ORIGINAL は1件も無ければスキーマに現れない動的フィールドなので、
    # 無条件に count するとエラーになる。
    n_original = (
        dataset.count(f"{FIELD_ORIGINAL}.detections")
        if FIELD_ORIGINAL in dataset.get_field_schema()
        else 0
    )
    logger.info(
        "FiftyOne dataset '%s': %d Sample / %d Detection（うち original 併記 %d）",
        name,
        len(dataset),
        dataset.count(f"{FIELD_FINAL}.detections"),
        n_original,
    )
    return dataset


def _declare_reason_field(dataset) -> None:
    """理由フィールドの型を**サンプルを入れる前に**宣言する。

    ``add_samples(dynamic=True)`` は最初に見た値から型を推論し、あとから広げない。
    理由を持たない annotation の ``[]`` が先に来ると、要素の型が決まらない
    ``ListField`` として宣言され、``list<str>`` と見なされずに Edit パネルから
    消える（実機で確認済み）。順序に依存させないため明示的に宣言する。

    ``final`` はまだ存在しないので、親の ``Detections`` から作る。
    """
    import fiftyone as fo
    import fiftyone.core.labels as fol

    try:
        dataset.add_sample_field(
            FIELD_FINAL, fo.EmbeddedDocumentField, embedded_doc_type=fol.Detections
        )
        dataset.add_sample_field(
            f"{FIELD_FINAL}.detections.{FIELD_REVIEW_REASONS}",
            fo.ListField,
            subfield=fo.StringField,
        )
    except Exception as error:  # 宣言できなくてもビルドは続ける
        logger.warning("理由フィールドを宣言できなかった: %s", error)


#: Edit Detection パネルに出す属性と並び。``(名前, 読み取り専用か)``。
#:
#: 並びは**上から判断に使う順**。日時を判定欄のすぐ下に置くのは、重複ペアの
#: どちらを exclude するかを決めてから ``review_status`` に戻る動きになるため。
#:
#: 読み取り専用にするものは検証結果の写しで、App で書き換えても
#: ``review export`` は読まないし次の ``review build`` で上書きされる。
#: 編集できると「直したのに反映されない」という誤解を生む。
PANEL_ATTRIBUTES: tuple[tuple[str, bool], ...] = (
    (FIELD_REVIEW_STATUS, False),
    # 判定を決めた直後に理由を選ぶ動きになるよう、status の真下に置く。
    (FIELD_REVIEW_REASONS, False),
    (FIELD_REVIEW_REASON, False),
    (FIELD_REVIEWER, False),
    (FIELD_REVIEW_COMMENT, False),
    # 重複ペアの判断材料。自分と相手を並べて出す。
    ("timestamp", True),
    ("partner_timestamp", True),
    ("pair_verdict", True),
    ("newer_in_pair", True),
    ("annotator", True),
    ("partner_annotator", True),
    ("duplicate_group_id", True),
    ("related_geometry_uid", True),
    # 検証結果との突き合わせキーと、何が検出されたか。
    ("geometry_uid", True),
    ("issues", True),
    # flag:needs_report を付けるために編集可能にしておく。
    ("tags", False),
)

#: パネルから外す属性。
#:
#: ``id`` / ``confidence`` / ``index`` はこのツールが一度も設定しないので常に空。
#: ``mask_path`` は自動生成すると100件超のドロップダウンになって邪魔（マスク自体は
#: パネルの Mask プレビューで見える）。施設・患者などは Sample 側に同じものがあり、
#: 宣言はされるのでサイドバーのフィルタには出る。
_PANEL_KEEP = {name for name, _ in PANEL_ATTRIBUTES}


def _apply_label_schema(dataset) -> None:
    """Edit パネルに出す属性をコードから決めて有効化する。

    宣言しただけでは足りない。パネルが描くのは
    「編集中ドラフト ?? 保存済み ``label_schema`` ?? ``default_label_schema``」で、
    **保存済みスキーマが優先される**。App の Schema manager で手作業すると
    そこに残るが、``review build`` は DB を作り直すので消える。コードから
    設定しておけば、作り直しても同じパネルが再現される。

    失敗してもビルド自体は続ける。パネルの見た目は目視の便宜であって、
    dataset そのものは無くても使える。
    """
    from fiftyone.core.annotation.constants import DEFAULT_COMPONENTS

    try:
        generated = dataset.generate_label_schemas(fields=[FIELD_FINAL])
        schema = generated[FIELD_FINAL]
        available = {a["name"]: a for a in schema.get("attributes", [])}
        attributes = []
        for name, read_only in PANEL_ATTRIBUTES:
            spec = available.get(name)
            if spec is None:
                # その属性を持つ annotation が1件も無い場合（重複が0件なら
                # partner_timestamp は宣言されない）。黙って飛ばす。
                continue
            spec = dict(spec)
            if read_only:
                spec["read_only"] = True
                # 読み取り専用の値をドロップダウンにすると、timestamp や issues の
                # 長文が候補として並んで読めなくなる。素の入力欄に戻す。
                # component は型ごとに許される値が決まっている（bool に "text" は
                # 不正）ので、FiftyOne 自身の既定表を引く。
                spec.pop("values", None)
                spec["component"] = DEFAULT_COMPONENTS.get(
                    spec.get("type"), spec.get("component")
                )
            elif name == FIELD_REVIEW_REASONS:
                # ★理由だけは候補を固定する。``list<str>`` + ``checkboxes`` のとき
                # App は CheckboxList を描き、**自由入力の口が無い**
                # （``dropdown`` / ``text`` は AutocompleteView +
                # allow_user_input で自由入力が残り、綴りミスが集計を割る）。
                # values はビルド時に焼き込めるので、捨て Sample で distinct() を
                # 膨らませる必要はない。
                spec["component"] = "checkboxes"
                spec["values"] = list(ALL_SUGGESTED_REASONS)
            attributes.append(spec)
        schema["attributes"] = attributes
        dataset.update_label_schema(FIELD_FINAL, schema)
        dataset.activate_label_schemas([FIELD_FINAL])
        logger.info(
            "Edit パネルの属性を設定した（%d 件）: %s",
            len(attributes),
            ", ".join(a["name"] for a in attributes),
        )
        missing = _PANEL_KEEP - set(available)
        if missing:
            logger.debug("パネル候補のうち未宣言で飛ばした属性: %s", sorted(missing))
    except Exception as error:  # App の見た目のためにビルドを失敗させない
        logger.warning(
            "Edit パネルの属性を設定できなかった（目視自体は可能）: %s", error
        )


def _seed_choice_candidates(reference_filepath: str) -> list:
    """review_status / review_reason の全候補値を、FiftyOne App の値編集ボックスの
    候補（サジェスト）として実在させるためだけの捨てSample群。

    FiftyOne 1.21 の App は候補値を MongoDB の distinct() をその場で引いて出す
    （``dataset.classes`` はアノテーション連携（CVAT等）専用でApp からは一切
    参照されず、``StringField(choices=...)`` もAppからは無視されることを実機で
    確認済み）。つまり「一度も出たことのない値」は宣言では出せず、実データとして
    最低1回存在させる以外に方法が無い。``uncertain`` や ``SUGGESTED_REASONS`` は
    人間しか付けない値なので、目視前は実データにまだ一度も現れない。

    ``SCHEMA_SEED_TAG`` を付け、``file_uid`` / ``geometry_uid`` / ``reviewer`` は
    空文字にする（**未設定のままにはしない**）。``reviewer``未入力かつmanifestに無い
    ``file_uid``なら人間の判定と見なされないので export/import からは無視されるが、
    それは``get_field()``が値を返せる場合の話。FiftyOneの``Document.get_field()``は
    ``getattr(self, field_name)``の薄いラッパーで、その**インスタンスに一度も
    設定したことのない動的属性**に対しては``None``ではなく``AttributeError``を
    投げる（実機で確認済み）。空文字を明示しておけば`if not uid: continue`のような
    既存の防御がそのまま効く。``_save_views`` / ``dataset_summary`` 側では
    ``SCHEMA_SEED_TAG`` を除外して集計する。
    """
    import fiftyone as fo

    statuses = list(ReviewStatusTag)
    reasons = ALL_SUGGESTED_REASONS
    n = max(len(statuses), len(reasons))

    seeds = []
    for i in range(n):
        status = statuses[i % len(statuses)].value
        reason = reasons[i % len(reasons)]

        # flag:needs_report も同じ理由で候補に出ないので、
        # シードのついでに1回だけ実在させる。
        extra_tags = [FLAG_NEEDS_REPORT] if i == 0 else []

        sample = fo.Sample(
            filepath=reference_filepath, tags=[SCHEMA_SEED_TAG, *extra_tags]
        )
        sample["file_uid"] = ""
        sample[FIELD_REVIEW_STATUS] = status
        # 空でも**必ず設定する**。get_field() は一度も設定していない動的属性に
        # 対して None ではなく AttributeError を投げるので、export が落ちる。
        # 型は _declare_reason_field() が決めるので、ここに実値は要らない。
        sample[FIELD_REVIEW_REASONS] = []
        sample[FIELD_REVIEW_REASON] = reason

        detection = fo.Detection(
            label="__schema_seed__", bounding_box=[0.0, 0.0, 0.001, 0.001]
        )
        detection.tags = [SCHEMA_SEED_TAG, *extra_tags]
        detection["geometry_uid"] = ""
        detection[FIELD_REVIEW_STATUS] = status
        detection[FIELD_REVIEW_REASONS] = []
        detection[FIELD_REVIEW_REASON] = reason
        sample[FIELD_FINAL] = fo.Detections(detections=[detection])
        seeds.append(sample)

    return seeds


def _save_views(dataset, config: Config) -> None:
    """App の左サイドバーに出る保存ビュー。目視の入口を用意する。

    ★名前は ASCII を含めること。FiftyOne はビュー名を slug 化するので、
    日本語だけの名前は空の slug になって ``ValueError`` で失敗する。
    日本語は description 側に置く。
    """
    from fiftyone import ViewField as F

    pending = ReviewStatusTag.PENDING.value
    # 以前はここで SCHEMA_SEED_TAG を除外していたが、それだと保存ビュー経由では
    # _seed_choice_candidates() が実在させた候補値（review_status / review_reason /
    # flag:needs_report）が一度も見えず、実データの多様性が失われた瞬間に
    # 選択式が事実上の自由入力に見えてしまう（画像のkeep/exclude判定が消えた際に発生）。
    # 件数集計（dataset_summary）は別途 real 変数で除外しているので、
    # ここでシードを混ぜても集計・review export/import の正しさには影響しない。
    #
    # ``.view()`` が必要。``Dataset`` をそのまま save_view へ渡すと
    # ``Dataset._serialize() got an unexpected keyword argument 'include_uuids'``
    # で落ちる（10本のうち 0-all-real だけが作られていなかった原因）。
    base = dataset.view()

    def save(name: str, view, description: str) -> None:
        try:
            dataset.save_view(name, view, description=description, overwrite=True)
        except Exception as error:
            # 黙って落とすと「ビューが無い」ことに気付けない。
            logger.warning("保存ビューを作れない %s: %s", name, error)

    save(
        "0-all-real",
        base,
        "全件（候補シード system:schema_seed を含む。__schema_seed__ という捨て"
        "ラベルが数件混ざるが、選択肢を切らさないためなので無視してよい）",
    )
    save(
        "1-pending-annotations",
        base.filter_labels(FIELD_FINAL, F(FIELD_REVIEW_STATUS) == pending),
        "目視待ちの annotation だけを残した Detection ビュー。"
        "review_status==pending で絞るため候補シードのうち pending 以外は出ない",
    )
    save(
        "2-pending-images",
        base.match(F(FIELD_REVIEW_STATUS) == pending),
        "目視待ちの画像（未アノテーションのビュー）。側面像なら除外が必要。"
        "review_status==pending で絞るため候補シードのうち pending 以外は出ない",
    )
    save(
        "3-outside-body",
        base.filter_labels(FIELD_FINAL, F("tags").contains("auto:s05_outside_body")),
        "体外領域。胸郭の側方バンドからはみ出している annotation",
    )
    save(
        "4-tiny-region",
        base.filter_labels(
            FIELD_FINAL,
            F("tags").contains("auto:s03_suspiciously_small")
            | F("tags").contains("auto:s03_tiny_annotation")
            | F("tags").contains("auto:s03_stray_component"),
        ),
        "微小領域。ここでの判定が S03 の閾値を決める",
    )
    save(
        "5-contained-different-label",
        base.filter_labels(
            FIELD_FINAL, F("tags").contains("auto:d04_contained_different_label")
        ),
        "包含された重複（別ラベル）。入れ子の所見として正当な可能性が高い",
    )
    save(
        "6-broken-bbox",
        base.filter_labels(FIELD_FINAL, F("display_adjusted") != None),  # noqa: E711
        "座標が壊れている bbox。表示のために枠を広げてある（original_bbox が原座標）",
    )
    save(
        "7-flagged-for-report",
        base.match(F("tags").contains(FLAG_NEEDS_REPORT)),
        "検証対象外だが気になったannotation/画像。データ管理担当への報告用",
    )
    save(
        "8-original-final-divergence",
        base.filter_labels(
            FIELD_FINAL, F("tags").contains("auto:s04_original_final_divergence")
        ),
        "修正前後の食い違い。サイドバーで original も表示すると重ねて比較できる",
    )
    save(
        "9-needs-report-first",
        # tags は ListField なので App のフィールドソートでは並べ替えられない。
        # sort_by() はフィールド名だけでなく ViewExpression も受け付けるので、
        # 絞り込まずに真偽値で並べ替えるだけの全件ビューにする。
        base.sort_by(F("tags").contains(FLAG_NEEDS_REPORT), reverse=True),
        "flag:needs_report を先頭に寄せた全件ビュー（絞り込まず順序だけ変える）",
    )
    save(
        "10-reasoned-decisions",
        # ``match`` ではなく ``filter_labels``。7-flagged-for-report は match なので
        # Sample タグしか見ておらず、Detection に付けた印を拾えない。同じ罠を踏まない。
        # ここは候補シードを除く —— 実際の判定を見るビューなので、
        # 候補を実在させるための捨て行が混ざると数が読めなくなる。
        dataset.match(~F("tags").contains(SCHEMA_SEED_TAG)).filter_labels(
            FIELD_FINAL, F(FIELD_REVIEW_REASONS).length() > 0, only_matches=True
        ),
        "理由を入れた annotation。入れ忘れの洗い出しと、理由別の見直しに使う",
    )
    save(
        "11-spot-check-unannotated",
        # pending ではない（自動決定済み）ので 1-pending-*/2-pending-* には出ない。
        # dataset_id でまず束ね、その中で image_class → series_image_index の順に
        # 並べると、同じ自動判定理由の画像が近くに集まって見比べやすい。
        base.match(F("tags").contains("spot_check")).sort_by(
            [("dataset_id", 1), ("image_class", 1), ("series_image_index", 1)]
        ),
        "未アノテーション画像のデータセット別スポットチェック（--sample-unannotated）。"
        "採否は変えず、自動判定（auto_decision_reason）が妥当か確認する用途。"
        "dataset_id/image_class/series_image_index/auto_decision_reason で絞り込める",
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
    from fiftyone import ViewField as F

    real = dataset.match(~F("tags").contains(SCHEMA_SEED_TAG))

    # count_values() はフィールドの生値しか見ないので、タグだけで判定した分を
    # 拾えない（review_status が pending のまま残って見える）。export_decisions
    # と同じ effective_status() で判定してから集計する。
    annotation_status: Counter[str] = Counter()
    image_status: Counter[str] = Counter()
    for sample in real.select_fields([FIELD_REVIEW_STATUS, "tags", FIELD_FINAL]):
        image_status[
            effective_status(
                sample.get_field(FIELD_REVIEW_STATUS), list(sample.tags or [])
            )
        ] += 1
        detections = sample.get_field(FIELD_FINAL)
        for detection in detections.detections if detections else []:
            annotation_status[
                effective_status(
                    detection.get_field(FIELD_REVIEW_STATUS), list(detection.tags or [])
                )
            ] += 1

    return {
        "name": name,
        "n_samples": len(real),
        "n_detections": real.count(f"{FIELD_FINAL}.detections"),
        "label_tags": real.count_label_tags(),
        "sample_tags": real.count_sample_tags(),
        "annotation_status": dict(annotation_status),
        "image_status": dict(image_status),
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
