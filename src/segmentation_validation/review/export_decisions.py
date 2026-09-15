"""FiftyOne の判定を ``review_decisions.json`` へ書き出す。fiftyone を import する。

**FiftyOne の MongoDB を正本にしない。** これが成立するために必要な性質:

    FiftyOne DB削除 -> dataset再構築 -> review_decisions.json import -> 判定が戻る

突合キーは annotation が ``dataset_id`` + ``geometry_uid``、画像が ``dataset_id`` +
``institution/study/series/file_id``。どちらも元JSONの再エクスポート（ファイル名の
日時スタンプだけが変わる）を跨いで不変で、新しいJSONが追加されて fingerprint が
変わっても対応が壊れない。書き出し先は ``review/decision_store.py`` が持つ正本。

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
import logging
from pathlib import Path
from typing import Any

from ..config import Config
from . import decision_store
from .decision_store import split_file_uid
from .fiftyone_builder import configure_database
from .review_schema import (
    DECIDED,
    FIELD_FINAL,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_REASONS,
    FIELD_REVIEW_STATUS,
    FIELD_REVIEWED_AT,
    FIELD_REVIEWER,
    FLAG_NEEDS_REPORT,
    SCHEMA_SEED_TAG,
    effective_reasons,
    join_reasons,
)
from .review_schema import effective_status as _effective_status

logger = logging.getLogger(__name__)

#: 後方互換のための再エクスポート。列の正本は ``decision_store.COLUMNS``。
COLUMNS = decision_store.COLUMNS

#: ``flagged_for_report.csv`` の列。採否とは別軸のレポート専用出力なので
#: ``review_decisions`` とは別の並び（実ファイルパスを含む）。
FLAGGED_COLUMNS = (
    "kind",
    "dataset_id",
    "key",
    "institution",
    "patient_id",
    "study",
    "series",
    "file_id",
    "image_path",
    "path_mask",
    "path_original_mask",
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

    # ``review_reasons`` は後から入れたフィールド。**この版より前に作った DB には
    # 無い**。select_fields に無いフィールドを渡すと ValueError で落ちるので、
    # スキーマを見て在るときだけ渡す。ここが落ちると
    # 「作り直す前に必ず export」という手順そのものが実行できず、
    # 過去の目視結果を失う。
    fields = [
        "file_uid",
        FIELD_REVIEW_STATUS,
        FIELD_REVIEW_REASON,
        FIELD_REVIEWER,
        FIELD_REVIEWED_AT,
        FIELD_REVIEW_COMMENT,
        "tags",
        FIELD_FINAL,
        # ★dataset_id は Sample 側のフィールド。Detection には無いので、
        # annotation の突合キーはここから取る。
        "dataset_id",
    ]
    if FIELD_REVIEW_REASONS in dataset.get_field_schema():
        fields.append(FIELD_REVIEW_REASONS)
    else:
        logger.info(
            "この dataset には %s が無い（古い版で作られた DB）。"
            "理由は reason: タグと自由記述から読む。`review build` で作り直すと"
            "チェックボックスが使えるようになる",
            FIELD_REVIEW_REASONS,
        )

    rows: list[dict[str, Any]] = []
    for sample in dataset.select_fields(fields):
        status = _effective_status(
            sample.get_field(FIELD_REVIEW_STATUS), list(sample.tags or [])
        )
        dataset_id = _optional(sample, "dataset_id") or ""
        # ★画像の突合キーは stable_file_uid（dataset_id + inst/study/series/file）。
        # file_uid は source_json（日時スタンプ込みのファイル名）を含むため、
        # 元JSONを再エクスポートすると判定が全件引き当て不能になる。
        # baseline（manifest 側）は同一 fingerprint 内の突合なので file_uid のまま。
        parts = split_file_uid(sample.file_uid)
        image_key = parts[1] if parts else sample.file_uid
        row = _row(
            "image",
            image_key,
            status,
            sample,
            dataset_id=dataset_id,
            legacy_file_uid=sample.file_uid,
        )
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
            row = _row(
                "annotation", uid, label_status, detection, dataset_id=dataset_id
            )
            annotation_uid = f"{dataset_id}::{uid}"
            if label_status in DECIDED and _is_human(
                row, baseline_ann.get(annotation_uid), manifest
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
    return [row for row in rows if _merge_key(row) not in exported]


def human_decisions_in_db(config: Config) -> list[dict[str, Any]]:
    """DB に入っている人間の判定を返す。**manifest 無しでも使える。**

    ``unexported_human_decisions`` は manifest（DB を作ったときの基準線）を
    前提にしているが、fingerprint が変わると manifest が見つからない。
    そのとき「守るものが無い」と解釈すると、``review build`` が前の構成の
    判定を消してしまう。

    基準線が無いので「機械の判定から変わったか」は判定できない。
    ``manifest=None`` のとき :func:`_is_human` は ``reviewer`` が入っている行だけ
    を通すので、そのまま「DB にある人間の判定」になる —— 取りこぼしはあるが、
    **あるものを「無い」と言わない**方向に倒す。

    dataset が無ければ :class:`RuntimeError`（``collect_decisions`` と同じ）。
    """
    return collect_decisions(config, manifest=None)


def write_decisions(
    path: Path, rows: list[dict[str, Any]], config: Config
) -> list[dict[str, Any]]:
    """``rows``（今回スキャンできた人間の判定）を正本へマージして書く。

    ``review build``（``--all`` 無し）は pending の無い画像/annotationを
    FiftyOne から除外するため、一度確定した判定が次の ``collect_decisions``
    のスキャン範囲から外れて見えなくなることがある。見えなくなったからと
    いって判定が無効になったわけではないので、**上書きしない**。
    既存ファイルにしか無いキーはそのまま残す。

    実体は :mod:`segmentation_validation.review.decision_store`。突合キーと
    スキーマの面倒はすべてそちらが見る（fingerprint 非依存の正本と、
    fingerprint 配下のスナップショットで同じ規則を使うため）。

    戻り値はマージ後の全行（今回スキャンできなかった既存の判定も含む）。
    呼び出し側がスナップショットを書くのに使う。
    """
    return decision_store.write_rows(path, rows, config.review.dataset_name)


def collect_flagged(config: Config) -> list[dict[str, Any]]:
    """``flag:needs_report`` の付いた annotation/画像を、実ファイルパス込みで集める。

    採否（``review_status``）とは別軸なので ``effective_status`` は使わず、
    タグの有無だけで拾う。``system:schema_seed``（候補値を実在させるための
    捨て行）は必ず除外する —— ``flag:needs_report`` は候補一覧に出すため、
    その捨て行1件にもあらかじめ付けてある（`fiftyone_builder._seed_choice_candidates`）。
    """
    configure_database(config)
    import fiftyone as fo
    from fiftyone import ViewField as F

    name = config.review.dataset_name
    if name not in fo.list_datasets():
        raise RuntimeError(
            f"FiftyOne dataset '{name}' が無い。先に `review build` を実行する"
        )
    dataset = fo.load_dataset(name)
    real = dataset.match(~F("tags").contains(SCHEMA_SEED_TAG))

    fields = [
        "file_uid",
        "dataset_id",
        "institution",
        "patient_id",
        "study",
        "series",
        "file_id",
        "image_path",
        "tags",
        FIELD_FINAL,
    ]
    rows: list[dict[str, Any]] = []
    for sample in real.select_fields(fields):
        if FLAG_NEEDS_REPORT in (sample.tags or []):
            rows.append(_flag_row("image", sample.file_uid, sample))
        detections = sample.get_field(FIELD_FINAL)
        for detection in detections.detections if detections else []:
            if FLAG_NEEDS_REPORT in (detection.tags or []):
                uid = detection.get_field("geometry_uid") or ""
                rows.append(_flag_row("annotation", uid, sample, detection))
    return rows


def _flag_row(
    kind: str, key: str, sample: Any, detection: Any | None = None
) -> dict[str, Any]:
    """報告用の1行。識別情報は常に Sample 側から取る（Detection には無い）。

    実ファイルパス（``path_mask``/``path_original_mask``）はannotationのときだけ
    Detection側から取る。画像単位のflagには対応するannotationが無いので空にする。
    """
    holder = detection if detection is not None else sample
    return {
        "kind": kind,
        "dataset_id": str(sample.get_field("dataset_id") or ""),
        "key": key,
        "institution": str(sample.get_field("institution") or ""),
        "patient_id": str(sample.get_field("patient_id") or ""),
        "study": str(sample.get_field("study") or ""),
        "series": str(sample.get_field("series") or ""),
        "file_id": str(sample.get_field("file_id") or ""),
        "image_path": str(sample.get_field("image_path") or ""),
        "path_mask": str(_optional(holder, "path_mask") or ""),
        "path_original_mask": str(_optional(holder, "path_original_mask") or ""),
    }


def write_flagged_report(path: Path, rows: list[dict[str, Any]]) -> int:
    """flag:needs_report が付いた項目を CSV へ書き足す。

    ``write_decisions`` と同じ理由でマージにする（キーは
    ``(kind, dataset_id, key)``）。実ファイルはコピーせず、このCSVで
    パスだけを控えてデータ管理担当へ報告する運用を想定している
    （マスクの実ファイルをコピー/exportする運用は避ける）。
    ボタンを何度押しても行は増え続けるだけで、既存の行は消えない。

    戻り値は今回**新規に追加**された行数（呼び出し側が「今回何件増えたか」を
    出すため）。
    """
    existing: dict[tuple[str, str, str], dict[str, Any]] = {}
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                existing[(row["kind"], row["dataset_id"], row["key"])] = row

    added = 0
    for row in rows:
        key = (row["kind"], row["dataset_id"], row["key"])
        if key not in existing:
            added += 1
        existing[key] = row

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FLAGGED_COLUMNS)
        writer.writeheader()
        for row in existing.values():
            writer.writerow(row)
    return added


# ------------------------------------------------------------------ internal


def _merge_key(row: dict[str, Any]) -> tuple[str, str, str]:
    """マージ/突合キー。annotation も画像も ``dataset_id`` を含める。"""
    return decision_store.row_key(row)


def _exported_keys(path: Path) -> set[tuple[str, str, str]]:
    """正本に既に書き出されている判定のキー。"""
    return decision_store.exported_keys(path)


def _baseline(
    manifest: dict[str, Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """manifest に記録された採否。これと違えば人間が変えたということ。

    annotation側のキーは ``f"{dataset_id}::{geometry_uid}"``（annotation_uid）。
    geometry_uid は cross-dataset 重複でデータセットをまたいで再利用されるため、
    bare geometry_uid だけをキーにすると別データセットの annotation の
    baseline を誤って参照してしまう。
    """
    if not manifest:
        return {}, {}
    annotations: dict[str, str] = {}
    images: dict[str, str] = {}
    for image in manifest.get("images", []):
        images[image["file_uid"]] = image["review_status"]
        dataset_id = image.get("dataset_id")
        for annotation in image.get("annotations", []):
            key = f"{dataset_id}::{annotation['geometry_uid']}"
            annotations[key] = annotation["review_status"]
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


def _optional(holder: Any, name: str) -> Any:
    """設定されていないかもしれないフィールドを読む。

    FiftyOne の ``get_field()`` は ``getattr`` の薄いラッパーで、その
    インスタンスに一度も設定していない動的属性には ``None`` ではなく
    ``AttributeError`` を投げる。古い版で作った DB を読むときに要る。
    """
    try:
        return holder.get_field(name)
    except AttributeError:
        return None


def _row(
    kind: str,
    key: str,
    status: str,
    holder: Any,
    dataset_id: str | None = None,
    legacy_file_uid: str = "",
) -> dict[str, Any]:
    # 理由は「review_reasons フィールド → reason: タグ → 自由記述」の順に見て、
    # **機械の理由は捨てる**。manifest が初期値として流し込んだ review_required が
    # 人間の理由として書き出されるのを防ぐ（それで目視102件の理由が全部
    # review_required になっていた）。
    reasons = effective_reasons(
        _optional(holder, FIELD_REVIEW_REASONS),
        list(holder.tags or []),
        _optional(holder, FIELD_REVIEW_REASON),
    )
    return {
        "kind": kind,
        "key": key,
        # annotation / 画像のどちらも dataset_id を突合キーに含める。
        # annotation は geometry_uid が cross-dataset 重複でデータセットを
        # またいで再利用されるため。画像は key が inst/study/series/file だけで
        # データセット間で重複しうるため（同じDICOMが複数データセットに登録
        # されている実例が 4,298 件ある）。
        "dataset_id": dataset_id,
        "decision": status,
        "reason": join_reasons(reasons),
        "reviewer": str(holder.get_field(FIELD_REVIEWER) or ""),
        "reviewed_at": str(holder.get_field(FIELD_REVIEWED_AT) or ""),
        "comment": str(holder.get_field(FIELD_REVIEW_COMMENT) or ""),
        # 移行の追跡用。突合には使わない。
        "legacy_file_uid": legacy_file_uid,
    }
