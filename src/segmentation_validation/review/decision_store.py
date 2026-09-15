"""目視判定の正本（fingerprint 非依存）。

``output/validation/<fingerprint>/review/review_decisions.json`` は fingerprint が
変わるたびに空の新ディレクトリを指してしまい、目視結果が引き継がれない。
fingerprint は対象JSONの ``name:size:mtime_ns:sha256`` から決まる
（``core/cache.py::fingerprint``）ので、データセットを1本足すだけ・symlink を
張り替えるだけで変わる。

人間の目視結果は再生成できない唯一の資産なので、``<project_root>/review/
review_decisions.json`` を正本として Git 管理し、このモジュールがその読み書きを
一手に引き受ける。fingerprint 配下のファイルはスナップショット（派生物）。

突合キー:

- annotation … ``dataset_id`` + ``geometry_uid``
- image      … ``dataset_id`` + ``institution/study/series/file_id``

どちらも再エクスポート（ファイル名の日時スタンプだけが変わる）を跨いで不変。
``dataset_id`` は ``adapters/engineer_set.py::dataset_id_for`` が末尾の
``-YYYYMMDD_HHMMSS`` を落とすため安定している。

schema_version 1 は画像側のキーが ``file_uid``（= ``source_json::inst/study/
series/file``）だった。``source_json`` は日時スタンプ込みのファイル名そのもの
なので、元JSONを再エクスポートすると画像側の判定が**全件**引き当て不能になる。
これが v2 で ``image_key`` へ移した理由。v1 は読み込み時に自動で昇格する
（``source_json`` を ``dataset_id_for()`` に通すだけの機械的な変換）。

このモジュールは fiftyone に依存しない（``tests/test_architecture.py`` 参照）。
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

KIND_ANNOTATION = "annotation"
KIND_IMAGE = "image"

# CSV の列。``review/export_decisions.py`` が書いていたものと同じ並びに
# ``legacy_file_uid`` を足しただけ（既存の閲覧手順を壊さない）。
COLUMNS = (
    "kind",
    "dataset_id",
    "key",
    "decision",
    "reason",
    "reviewer",
    "reviewed_at",
    "comment",
    "legacy_file_uid",
)

Row = dict[str, Any]
RowKey = tuple[str, str, str]


class DecisionStoreError(RuntimeError):
    """正本を読めない/解決できない衝突がある。呼び出し側は停止する。"""


@dataclass(frozen=True)
class Conflict:
    """同一キーに対して食い違う判定があった。"""

    key: RowKey
    kept: Row
    dropped: Row
    resolved_by: str  # "reviewed_at" なら自動解決、"" なら未解決

    def describe(self) -> str:
        kind, dataset_id, key = self.key
        return (
            f"{kind} {dataset_id}::{key}: "
            f"{self.kept['decision']}({self.kept.get('reviewed_at') or '日時なし'}) "
            f"vs {self.dropped['decision']}"
            f"({self.dropped.get('reviewed_at') or '日時なし'})"
        )


# --------------------------------------------------------------------- キー


def annotation_key(dataset_id: str, geometry_uid: str) -> str:
    """annotation の突合キー。``AnnotationRecord.annotation_uid`` と同じ形。"""
    return f"{dataset_id}::{geometry_uid}"


def image_key(
    dataset_id: str, institution: str, study: str, series: str, file_id: str
) -> str:
    """画像の突合キー。``FileGroup.stable_file_uid`` と同じ形。"""
    return f"{dataset_id}::{institution}/{study}/{series}/{file_id}"


def stable_uid(row: Row) -> str:
    """行から突合キー（``dataset_id::key``）を組み立てる。"""
    return f"{row.get('dataset_id') or ''}::{row['key']}"


def row_key(row: Row) -> RowKey:
    """マージ・重複判定に使う3つ組。

    annotation は ``geometry_uid`` が cross-dataset 重複でデータセットをまたいで
    再利用されるため、``dataset_id`` を含めないと別データセットの判定を
    上書きしてしまう。
    """
    return (row["kind"], row.get("dataset_id") or "", row["key"])


def split_file_uid(file_uid: str) -> tuple[str, str] | None:
    """``file_uid`` を ``(source_json, inst/study/series/file)`` へ分解する。"""
    source_json, sep, rest = file_uid.partition("::")
    if not sep or not source_json or not rest:
        return None
    return source_json, rest


def image_key_parts_from_file_uid(file_uid: str) -> tuple[str, str] | None:
    """schema v1 の ``file_uid`` を ``(dataset_id, key)`` へ変換する。

    ``source_json`` を ``dataset_id_for()`` に通すだけの機械的な変換なので、
    情報は失われない（元の ``file_uid`` は ``legacy_file_uid`` に残す）。
    解決できなければ None。
    """
    from ..adapters.engineer_set import dataset_id_for

    parts = split_file_uid(file_uid)
    if parts is None:
        return None
    source_json, rest = parts
    dataset_id = dataset_id_for(Path(source_json))
    if not dataset_id:
        return None
    return dataset_id, rest


# ------------------------------------------------------------------- 読み書き


def make_row(
    kind: str,
    dataset_id: str | None,
    key: str,
    decision: str,
    reason: str = "",
    reviewer: str = "",
    reviewed_at: str = "",
    comment: str = "",
    legacy_file_uid: str = "",
) -> Row:
    return {
        "kind": kind,
        "dataset_id": dataset_id,
        "key": key,
        "decision": decision,
        "reason": reason,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "comment": comment,
        "legacy_file_uid": legacy_file_uid,
    }


def load_rows(path: Path) -> list[Row]:
    """正本を読む。schema v1 / v2 のどちらでも受け取る。

    ファイルが無ければ空。**読めない場合は例外**（``_exported_keys`` と同じ考えで、
    読めないファイルを「判定が無い」と解釈してはいけない。黙って空を返すと
    人間の判定を丸ごと落とした状態で select が通ってしまう）。
    """
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionStoreError(f"{path} を読めない: {exc}") from exc
    if not isinstance(payload, dict):
        # ごく初期の形式（配列そのまま）。
        payload = {"decisions": payload, "image_decisions": []}

    version = payload.get("schema_version", 1)
    rows: list[Row] = []
    unresolved: list[str] = []

    for entry in payload.get("decisions") or []:
        rows.append(
            make_row(
                kind=KIND_ANNOTATION,
                dataset_id=entry.get("dataset_id"),
                key=entry["geometry_uid"],
                decision=entry["decision"],
                reason=entry.get("reason") or "",
                reviewer=entry.get("reviewer") or "",
                reviewed_at=entry.get("reviewed_at") or "",
                comment=entry.get("comment") or "",
            )
        )

    for entry in payload.get("image_decisions") or []:
        dataset_id = entry.get("dataset_id")
        key = entry.get("image_key")
        legacy = entry.get("legacy_file_uid") or ""
        if not key:
            # v1: file_uid しかない。dataset_id を導出して昇格する。
            legacy = entry.get("file_uid") or ""
            parts = image_key_parts_from_file_uid(legacy) if legacy else None
            if parts is None:
                unresolved.append(legacy)
                continue
            dataset_id, key = parts
        rows.append(
            make_row(
                kind=KIND_IMAGE,
                dataset_id=dataset_id,
                key=key,
                decision=entry["decision"],
                reason=entry.get("reason") or "",
                reviewer=entry.get("reviewer") or "",
                reviewed_at=entry.get("reviewed_at") or "",
                comment=entry.get("comment") or "",
                legacy_file_uid=legacy,
            )
        )

    if unresolved:
        logger.warning(
            "%s: file_uid から dataset_id を導出できない画像判定が %d 件ある。"
            "突合に使えないので読み飛ばす（例: %s）",
            path,
            len(unresolved),
            unresolved[0],
        )
    if version < SCHEMA_VERSION:
        logger.info(
            "%s は schema_version %s。v%d として読み込んだ（画像キーを昇格）",
            path,
            version,
            SCHEMA_VERSION,
        )
    return rows


def merge_rows(
    base: Iterable[Row], incoming: Iterable[Row]
) -> tuple[list[Row], list[Conflict]]:
    """``base`` に ``incoming`` を重ねる。既存キーは消さない。

    ``review build``（``--all`` 無し）は pending の無い項目を FiftyOne から
    外すため、一度確定した判定が次のスキャン範囲から消えることがある。
    見えなくなっただけで無効になったわけではないので**削除はしない**。

    同じキーで判定が食い違う場合は ``reviewed_at`` が新しい方を採る。
    日時が無い/同じで判定が違うものは解決できないので ``Conflict`` として
    返し、呼び出し側が停止する。
    """
    merged: dict[RowKey, Row] = {row_key(row): row for row in base}
    conflicts: list[Conflict] = []

    for row in incoming:
        key = row_key(row)
        current = merged.get(key)
        if current is None:
            merged[key] = row
            continue
        if current["decision"] == row["decision"]:
            # 判定が同じなら情報量の多い方（新しい日時）を残す。
            if _reviewed_at(row) >= _reviewed_at(current):
                merged[key] = row
            continue
        newer = _newer(current, row)
        if newer is None:
            conflicts.append(
                Conflict(key=key, kept=current, dropped=row, resolved_by="")
            )
            continue
        older = row if newer is current else current
        merged[key] = newer
        conflicts.append(
            Conflict(key=key, kept=newer, dropped=older, resolved_by="reviewed_at")
        )

    return list(merged.values()), conflicts


def _reviewed_at(row: Row) -> str:
    return str(row.get("reviewed_at") or "")


def _newer(a: Row, b: Row) -> Row | None:
    """``reviewed_at`` が新しい方。決められなければ None。"""
    left, right = _reviewed_at(a), _reviewed_at(b)
    if not left or not right or left == right:
        return None
    return a if left > right else b


def write_rows(
    path: Path,
    rows: Iterable[Row],
    dataset_name: str,
    merge: bool = True,
) -> list[Row]:
    """正本へ書く。既定では既存ファイルとマージする。

    戻り値は実際に書いた全行。解決できない衝突があれば
    ``DecisionStoreError`` を投げ、**何も書かない**。
    """
    incoming = list(rows)
    base = load_rows(path) if merge and path.exists() else []
    all_rows, conflicts = merge_rows(base, incoming)

    unresolved = [c for c in conflicts if not c.resolved_by]
    if unresolved:
        lines = "\n".join(f"  - {c.describe()}" for c in unresolved[:10])
        raise DecisionStoreError(
            f"判定が食い違い、reviewed_at でも決められない項目が "
            f"{len(unresolved)} 件ある。人が決める必要がある:\n{lines}"
        )
    for conflict in conflicts:
        logger.warning("判定の衝突を reviewed_at で解決した: %s", conflict.describe())

    payload = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset_name": dataset_name,
            "n_annotations": sum(1 for r in all_rows if r["kind"] == KIND_ANNOTATION),
            "n_images": sum(1 for r in all_rows if r["kind"] == KIND_IMAGE),
        },
        "decisions": [
            {
                "dataset_id": r["dataset_id"],
                "geometry_uid": r["key"],
                "decision": r["decision"],
                "reason": r["reason"],
                "reviewer": r["reviewer"],
                "reviewed_at": r["reviewed_at"],
                "comment": r["comment"],
            }
            for r in _sorted(all_rows, KIND_ANNOTATION)
        ],
        "image_decisions": [
            {
                "dataset_id": r["dataset_id"],
                "image_key": r["key"],
                "decision": r["decision"],
                "reason": r["reason"],
                "reviewer": r["reviewer"],
                "reviewed_at": r["reviewed_at"],
                "comment": r["comment"],
                "legacy_file_uid": r.get("legacy_file_uid") or "",
            }
            for r in _sorted(all_rows, KIND_IMAGE)
        ],
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    # Git 管理の正本なので、行の並びを安定させて差分を読めるようにする。
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

    csv_path = path.with_suffix(".csv")
    csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with csv_tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in _sorted(all_rows, KIND_ANNOTATION) + _sorted(all_rows, KIND_IMAGE):
            writer.writerow({name: row.get(name, "") for name in COLUMNS})
    csv_tmp.replace(csv_path)

    return all_rows


def _sorted(rows: Iterable[Row], kind: str) -> list[Row]:
    return sorted(
        (r for r in rows if r["kind"] == kind),
        key=lambda r: (r.get("dataset_id") or "", r["key"]),
    )


def snapshot(rows: Iterable[Row], out_path: Path, dataset_name: str) -> None:
    """fingerprint 配下へスナップショットを書く。

    正本ではないので**マージしない**（そのときの正本の写しであることを保つ）。
    """
    write_rows(out_path, rows, dataset_name, merge=False)


# ------------------------------------------------------------------- 参照用


def exported_keys(path: Path) -> set[RowKey]:
    """正本に既に書き出されている判定のキー。未export判定ゲートが使う。"""
    try:
        return {row_key(row) for row in load_rows(path)}
    except DecisionStoreError:
        logger.warning("%s を読めない。未 export 扱いにする", path)
        return set()


def counts(path: Path) -> dict[str, int]:
    """正本の件数。``review status`` と serve の表示が使う。"""
    rows = load_rows(path)
    return {
        "annotations": sum(1 for r in rows if r["kind"] == KIND_ANNOTATION),
        "images": sum(1 for r in rows if r["kind"] == KIND_IMAGE),
    }


def orphans(rows: Iterable[Row], known: Mapping[str, set[str]]) -> list[Row]:
    """現在の構成のどれにも当たらない行。

    ``known`` は種別ごとの突合キー集合
    （``{"annotation": {annotation_uid...}, "image": {stable_file_uid...}}``）。
    孤児は**削除しない**（データセットを一時的に外しただけかもしれない）。
    報告だけして人に気づかせる。
    """
    return [row for row in rows if stable_uid(row) not in known.get(row["kind"], set())]
