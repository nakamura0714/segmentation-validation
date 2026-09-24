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

**器（series / study / institution）は file entry とは別の規則で扱う。**
画像を1枚も持たなくなった器は統合JSONから取り除く（:func:`prune_empty_containers`）。
上の「0件でも残す」規則は file entry（＝実在する画像1枚）についてのものであり、
器は中身が0なら何も表していない。剪定しても画像と annotation は1件も動かないので
母集団は不変。★剪定は**全データセットの merge が終わってから**行う —— それより前だと
``_record_conflicts`` が「片方のデータセットで空」の属性食い違いを取り逃す。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import Config
from ..core.records import source_generated_at
from .decisions import Decision

logger = logging.getLogger(__name__)


class PendingPolicy:
    """pending / uncertain を override したときの扱い。既定は無し（停止）。"""

    EXCLUDE = "exclude"
    KEEP = "keep"


def gate_message(
    kind: str, count: int, subject: str, allow: bool, treat_as: str | None
) -> str | None:
    """未確定が残っているとき生成を止めるべきか判断する。

    止めるなら理由の文言、進めてよいなら ``None`` を返す。

    **override は「許可」と「扱い」の両方が揃って初めて成立する。**
    片方だけで通すと、保留が黙って開発データへ入る（または黙って落ちる）ので、
    ``--allow-pending`` だけでは進まない。

    ``kind`` は ``pending`` / ``uncertain``、``subject`` は ``annotation`` / ``画像``。
    annotation 側と画像側で同じ規則を使うため、判断をここに一本化している。
    """
    if not count:
        return None
    if allow and treat_as:
        return None
    flag = f"--allow-{kind}"
    return (
        f"{subject} {count} 件が {kind} のまま。"
        f"目視を進めるか `{flag} --{kind}-as keep|exclude` を明示すること"
    )


def file_uid_for(
    source_json: str,
    institution: str,
    study_key: str,
    series_key: str,
    file_key: str,
) -> str:
    """画像1枚を指す ``file_uid``。

    ``image_decisions.csv`` / ``image_duplicates.csv`` / lpdata の
    ``measurements.jsonl`` が同じ書式を使う。**突合できることが価値の中心**なので、
    書式を1か所に閉じて散らさない。
    """
    return f"{source_json}::{institution}/{study_key}/{series_key}/{file_key}"


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
    #: 元JSONの sha256（読み込み時点）。書き出し後の再読み込みと突き合わせて
    #: 「元JSONを触っていない」ことを確認するのに使う。
    source_sha256: str = ""


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
    """1つの元JSONから開発用JSONを作って書き出す。

    ``build_development_payload`` と ``write_development_json`` の薄いラッパ。
    payload を使い回したい（統合JSONを作る）場合は分割した方を直接呼ぶ。
    """
    payload, result = build_development_payload(
        source_path,
        decisions,
        out_path,
        config,
        pending_as=pending_as,
        uncertain_as=uncertain_as,
        meta_extra=meta_extra,
        image_decisions=image_decisions,
    )
    write_development_json(payload, out_path, result, source_path)
    return result


def build_development_payload(
    source_path: Path,
    decisions: Mapping[str, str],
    out_path: Path,
    config: Config,
    pending_as: str | None = None,
    uncertain_as: str | None = None,
    meta_extra: dict[str, Any] | None = None,
    image_decisions: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], BuildResult]:
    """元JSONを読み、keep だけに絞った payload を組み立てる。**書き出さない。**

    ``decisions`` は ``geometry_uid -> final_decision``、
    ``image_decisions`` は ``file_uid -> final_decision``。
    ``pending_as`` / ``uncertain_as`` は override が指定されたときだけ渡す。
    未指定のまま該当が残っていれば呼び出し側で止めるのが前提だが、
    ここでも黙って落とさないよう ``unknown`` として数える。

    書き出しと分けてあるのは、同じ payload を per-dataset の
    ``development.json`` と統合JSONの両方に使うため。統合側は返ってきた
    payload の中身を**参照ごと移す**ので、この関数を2回呼んではいけない
    （元JSONを2回パースすることになり、メモリも時間も倍かかる）。
    """
    raw = source_path.read_bytes()
    before_digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw.decode("utf-8"))

    result = BuildResult(path=out_path, source_sha256=before_digest)
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
                    uid = file_uid_for(
                        source_json, institution, study_key, series_key, file_key
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
    return payload, result


def write_development_json(
    payload: dict[str, Any],
    out_path: Path,
    result: BuildResult,
    source_path: Path,
) -> BuildResult:
    """payload を書き出し、元JSONが変化していないことを確かめる。

    ハッシュ照合は書き出しの**後**に行う。出力先を元JSONと取り違えた場合を
    捕まえるのが目的なので、書く前に確認しても意味がない。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 元JSONを触っていないことをハッシュで確認する。
    after_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    result.original_unchanged = after_digest == result.source_sha256
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


# ---------------------------------------------------------------- 統合（横断）

#: 統合JSONの file entry に足す唯一のキー。由来データセットを追跡できないと
#: train/test の別が失われる（PTE/PTR の二重エクスポート問題の再発）。
DATASET_ID_KEY = "dataset_id"

#: 衝突を記録するときに見る study / series のスカラー属性。
#: ``series_list`` / ``file_list`` は下の階層なので比較しない。
_STUDY_SCALARS = ("patient_id", "study_name", "study_date")
_SERIES_SCALARS = ("spacing", "shape", "manufacturer")


class MergeCollision(RuntimeError):
    """file 単位で衝突した。衝突ゼロが前提なので、黙って上書きしない。"""


@dataclass
class MergeResult:
    """統合の結果。件数と、解決しなかった属性の食い違い。"""

    datasets: list[str] = field(default_factory=list)
    institutions: int = 0
    studies: int = 0
    series: int = 0
    files: int = 0
    annotations: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    #: study / series の階層を最初に持ち込んだ dataset_id。衝突を記録するとき
    #: 「どちらのデータセットの値か」を示すのに要る（study/series の dict 自体は
    #: dataset_id を持たない —— 複数データセットで共有されるため）。
    owners: dict[str, str] = field(default_factory=dict)

    def as_totals(self) -> dict[str, int]:
        return {
            "datasets": len(self.datasets),
            "institutions": self.institutions,
            "studies": self.studies,
            "series": self.series,
            "files": self.files,
            "annotations": self.annotations,
        }


def new_merged_payload() -> dict[str, Any]:
    """統合の accumulator。最初の payload を重ねた時点で version_id が埋まる。"""
    return {"dataset": {}, "meta_development": {"merged": True, "datasets": {}}}


def merge_into(
    accumulator: dict[str, Any],
    payload: dict[str, Any],
    dataset_id: str,
    result: MergeResult,
) -> None:
    """``payload`` を ``accumulator`` へ再帰的に union する。

    **浅い ``dict.update()`` では壊れる。** 実データでは institution が 35個中
    23個、``(institution, study)`` が 42,690組中 4,274組、``(inst, study, series)``
    が 42,708組中 4,277組で複数データセットに跨る。上位階層で上書きすると、
    片方のデータセットにしか無い series / file を取りこぼす（実例:
    ``ofuna_chuo/CXOFC00003778_002/CXOFC00003778_002_001`` の file_list が
    2データセットに分かれている）。

    一方 ``(inst, study, series, file_id)`` は 42,574件すべてで衝突しない
    （``resolve_image_duplicates`` がクロスデータセット重複を解消済みのため）。
    そこを前提にしているので、衝突したら :class:`MergeCollision` で止める。

    ★移送は**参照の move**で、deepcopy しない。100MB 近い payload を12本ぶん
    複製するとメモリが持たない。呼び出し側は merge 後に元 payload を捨てること。

    属性の食い違いは値を選ばず ``result.conflicts`` に**両方**記録する。
    採用値は dataset_id 昇順の先頭（決定的・再現可能）。
    """
    result.datasets.append(dataset_id)
    # version_id / version_date は12本で一致しているのでトップレベルに1つ持つ。
    for key in ("version_id", "version_date"):
        if key in payload and key not in accumulator:
            accumulator[key] = payload[key]

    target = accumulator["dataset"]
    for institution, studies in (payload.get("dataset") or {}).items():
        if institution not in target:
            target[institution] = {}
        for study_key, study in studies.items():
            study_path = f"{institution}/{study_key}"
            existing_study = target[institution].get(study_key)
            if existing_study is None:
                target[institution][study_key] = study
                result.owners[study_path] = dataset_id
                _tag_files(study, dataset_id, result)
                continue
            _record_conflicts(
                result,
                level="study",
                path=study_path,
                scalars=_STUDY_SCALARS,
                existing=existing_study,
                incoming=study,
                dataset_id=dataset_id,
            )
            existing_series = existing_study.setdefault("series_list", {})
            for series_key, series in (study.get("series_list") or {}).items():
                series_path = f"{study_path}/{series_key}"
                current = existing_series.get(series_key)
                if current is None:
                    existing_series[series_key] = series
                    result.owners[series_path] = dataset_id
                    _tag_series_files(series, dataset_id, result)
                    continue
                _record_conflicts(
                    result,
                    level="series",
                    path=series_path,
                    scalars=_SERIES_SCALARS,
                    existing=current,
                    incoming=series,
                    dataset_id=dataset_id,
                )
                _merge_file_list(
                    current.setdefault("file_list", {}),
                    series.get("file_list") or {},
                    dataset_id,
                    series_path,
                    result,
                )


def _merge_file_list(
    target: dict[str, Any],
    incoming: dict[str, Any],
    dataset_id: str,
    path: str,
    result: MergeResult,
) -> None:
    for file_key, file_rec in incoming.items():
        if file_key in target:
            raise MergeCollision(
                f"file entry が衝突した: {path}/{file_key} "
                f"(dataset_id={dataset_id} と "
                f"{target[file_key].get(DATASET_ID_KEY)})。"
                "クロスデータセット重複の解消が効いていない可能性がある"
            )
        target[file_key] = _tag_file(file_rec, dataset_id, result)


def _tag_files(study: dict[str, Any], dataset_id: str, result: MergeResult) -> None:
    for series in (study.get("series_list") or {}).values():
        _tag_series_files(series, dataset_id, result)


def _tag_series_files(
    series: dict[str, Any], dataset_id: str, result: MergeResult
) -> None:
    for file_rec in (series.get("file_list") or {}).values():
        _tag_file(file_rec, dataset_id, result)


def _tag_file(
    file_rec: dict[str, Any], dataset_id: str, result: MergeResult
) -> dict[str, Any]:
    file_rec[DATASET_ID_KEY] = dataset_id
    result.annotations += len(file_rec.get("annotations") or [])
    return file_rec


def _record_conflicts(
    result: MergeResult,
    level: str,
    path: str,
    scalars: tuple[str, ...],
    existing: dict[str, Any],
    incoming: dict[str, Any],
    dataset_id: str,
) -> None:
    """食い違った属性を記録する。**値は選び直さない。**

    先に入っている方（dataset_id 昇順の先頭）の値をそのまま残し、両方の値を
    ``conflicts`` に控える。どちらが正かはツールが決められない —— 元データ側の
    不整合なので、データ管理側へ報告する材料として残すのが正しい扱い。
    """
    for name in scalars:
        if name not in existing and name not in incoming:
            continue
        left, right = existing.get(name), incoming.get(name)
        if left == right:
            continue
        for entry in result.conflicts:
            if entry["path"] == path and entry["field"] == name:
                entry["values"][dataset_id] = right
                break
        else:
            result.conflicts.append(
                {
                    "level": level,
                    "path": path,
                    "field": name,
                    "values": {result.owners.get(path, ""): left, dataset_id: right},
                    "adopted": result.owners.get(path, ""),
                    "rule": "dataset_id 昇順の先頭",
                }
            )


def finalize_merged(
    accumulator: dict[str, Any],
    result: MergeResult,
    per_dataset: Mapping[str, dict[str, Any]],
    meta_extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """統合 payload に meta を載せて仕上げる。

    ``per_dataset`` は dataset_id ごとの ``meta_development``。
    統合版では単一の件数ではなくデータセット別の内訳を持つ（どの元JSONから
    何件 keep されたかを追えないと、統合JSONから元へ戻れない）。
    """
    result.institutions = len(accumulator["dataset"])
    result.studies = sum(len(s) for s in accumulator["dataset"].values())
    result.series = sum(
        len(study.get("series_list") or {})
        for studies in accumulator["dataset"].values()
        for study in studies.values()
    )
    result.files = sum(
        len(series.get("file_list") or {})
        for studies in accumulator["dataset"].values()
        for study in studies.values()
        for series in (study.get("series_list") or {}).values()
    )

    meta = accumulator["meta_development"]
    meta["generated_at"] = datetime.now(timezone.utc).isoformat()
    meta["tool_version"] = _tool_version()
    meta["datasets"] = dict(per_dataset)
    meta["totals"] = result.as_totals()
    meta["conflicts"] = result.conflicts
    meta.update(meta_extra or {})
    return accumulator


def write_merged_json(payload: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out_path


# -------------------------------------------------- 空の器の剪定（統合後）


@dataclass
class PruneResult:
    """取り除いた器の数。**画像と annotation は1件も動かない。**"""

    institutions: int = 0
    studies: int = 0
    series: int = 0
    #: 取り除いた study のうち study レベルの分類ラベル（``annotations`` キー）を
    #: 持っていたもの。画像が0枚なので ``FileGroup.case_labels`` 経由で下流へは
    #: 元から届いていないが、黙って捨てないために数だけ残す（実データで48件、
    #: 内訳は ``StudyAnno/002 異常あり`` と ``TB_label/002``。``No Findings``
    #: ＝意図的な陰性症例は1件も含まれない）。
    studies_with_case_labels: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "institutions": self.institutions,
            "studies": self.studies,
            "series": self.series,
            "studies_with_case_labels": self.studies_with_case_labels,
        }

    def __bool__(self) -> bool:
        """1件でも取り除いたか。剪定の冪等性チェックに使う。"""
        return bool(self.institutions or self.studies or self.series)


def prune_empty_containers(payload: dict[str, Any]) -> PruneResult:
    """file entry を1件も持たない series / study / institution を取り除く。

    空の器は判断の結果ではなく**副作用**。元JSON（engineer-set）13本には空の
    series も空の study も1件も無く、``build_development_payload`` が
    ``image_decisions`` に従って file entry を落とした結果として生まれる
    （実データでは大半がクロスデータセット重複による skip）。器を残したまま
    ``totals.studies`` を出すと、中身の無い study を数えた不正確な報告になる。

    ★**全データセットの merge が終わってから**呼ぶこと。merge の前や途中で呼ぶと
    ``_record_conflicts`` が「片方のデータセットで空」の属性食い違いを記録できなく
    なる（実データで現在検出されている唯一の食い違いがまさにこの形 ——
    ``ofuna_chuo/CXOFC00000041_004/..._001`` の ``shape`` が、画像を出す側で z=2、
    画像0枚の側で z=1）。食い違いは元データ側の登録バグであり、どちらが画像を
    出したかとは独立に報告し続ける必要がある。

    ``finalize_merged`` の**前**に呼ぶこと。``totals`` は剪定後の構造を数える。

    **剪定しないもの**: ``annotations: []`` の file entry。画像は実在するので
    「0件でも残す」規則が優先する。剪定の対象はあくまで画像が0枚になった器。

    ``patient`` は器ではない（``patient_id`` は study のスカラー属性）。その患者の
    study が全部消えれば患者も自然に消える。
    """
    result = PruneResult()
    dataset = payload.get("dataset") or {}
    # dict を反復しながら削除できないので、キーのスナップショットを取る。
    for institution in list(dataset):
        studies = dataset[institution]
        for study_key in list(studies):
            study = studies[study_key]
            series_list = study.get("series_list") or {}
            for series_key in list(series_list):
                if not (series_list[series_key].get("file_list") or {}):
                    del series_list[series_key]
                    result.series += 1
            # series を消した直後に見るので、ボトムアップで1パスに収まる。
            if not series_list:
                if study.get("annotations"):
                    result.studies_with_case_labels += 1
                del studies[study_key]
                result.studies += 1
        if not studies:
            del dataset[institution]
            result.institutions += 1
    return result


# ------------------------------------------------ 統合の中身（明細と内訳）

#: 明細CSVの列。1行 = 統合JSONの file entry 1件（= 画像1枚）。
#: ``image_duplicates.csv`` と同じく監査用で、正本ではない
#: （統合JSONからいつでも再生成できる）。列を足すときは README 4章の表も直すこと。
DEVELOPMENT_MANIFEST_COLUMNS = (
    "dataset_id",
    "institution",
    "patient_id",
    "study_key",
    "study_date",
    "series_key",
    "file_id",
    "file_uid",
    "image_path",
    "n_annotations",
    "lesion_classes",
)

#: ``lesion_classes`` 列の区切り。1画像に複数クラスが付く。
#: ``detected_checks`` 等と同じ慣習。
CLASS_SEPARATOR = "|"


def build_development_manifest(
    merged: Mapping[str, Any],
    per_dataset: Mapping[str, Mapping[str, Any]],
    row_sink: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """統合payloadを**1回だけ**走査し、明細行と内訳を同時に作る。

    ``row_sink`` には ``csv.DictWriter.writerow`` をそのまま渡せる。行を list に
    溜めないのは、実データ16,685行ぶんの dict を保持すると10MB強をピークへ
    積み増すため。集計だけ欲しいときは ``None``。

    ★呼ぶのは ``write_merged_json`` の後、payload を手放す前。走査は**読むだけ**で
    payload を書き換えない（``dataset_id`` は ``merge_into`` が付与済み）。

    ``per_dataset`` は dataset_id ごとの ``meta_development``。``file_uid`` を
    組み立てる ``source_json`` をここから引く —— 統合payloadの file entry は
    ``dataset_id`` しか持っておらず、元JSON名は分からないため。

    戻り値は JSON 化できる素の dict（set も dataclass も返さない）。
    report 層が元JSONの階層構造を知らずに表を書けるようにするため。

    ★``patients`` / ``studies``（重複を畳んだ実数）は **file entry を1件でも持つ**
    ものだけを数える。統合JSONには画像を1枚も持たない study が実데ータで170件あり
    （空の ``file_list`` は共有 series でありふれた形）、それを合計側だけが数えると
    「合計 < 各データセットの総和」という読めない表になる。データセット別の行と
    同じ基準で数えることを優先する —— ``meta_development.totals.studies``
    （構造上の総数）とは意図的に別の値。
    """
    source_jsons = {
        dataset_id: str(meta.get("source_json") or "")
        for dataset_id, meta in per_dataset.items()
    }
    stats: dict[str, dict[str, Any]] = {}
    patients_all: set[str] = set()
    studies_all: set[tuple[str, str]] = set()

    for institution, studies in (merged.get("dataset") or {}).items():
        for study_key, study in studies.items():
            patient_id = str(study.get("patient_id") or study_key)
            study_date = str(study.get("study_date") or "")
            for series_key, series in (study.get("series_list") or {}).items():
                for file_key, file_rec in (series.get("file_list") or {}).items():
                    dataset_id = str(file_rec.get(DATASET_ID_KEY) or "")
                    source_json = source_jsons.get(dataset_id, "")
                    entry = stats.get(dataset_id)
                    if entry is None:
                        entry = stats[dataset_id] = _new_manifest_entry(source_json)
                    annotations = file_rec.get("annotations") or []
                    classes = _class_codes(annotations)

                    # 施設 / 患者 / study / series はデータセットを跨いで
                    # 重複するので、件数ではなく集合で持ってから畳む。
                    patients_all.add(patient_id)
                    studies_all.add((institution, study_key))
                    entry["institutions"].add(institution)
                    entry["patients"].add(patient_id)
                    entry["studies"].add((institution, study_key))
                    entry["series"].add((institution, study_key, series_key))
                    entry["files"] += 1
                    entry["annotations"] += len(annotations)
                    if not annotations:
                        entry["files_without_annotations"] += 1
                    for code, count in classes.items():
                        entry["classes"][code] = entry["classes"].get(code, 0) + count

                    if row_sink is not None:
                        row_sink(
                            {
                                "dataset_id": dataset_id,
                                "institution": institution,
                                "patient_id": patient_id,
                                "study_key": study_key,
                                "study_date": study_date,
                                "series_key": series_key,
                                "file_id": file_key,
                                "file_uid": file_uid_for(
                                    source_json,
                                    institution,
                                    study_key,
                                    series_key,
                                    file_key,
                                ),
                                "image_path": file_rec.get("image_path") or "",
                                "n_annotations": len(annotations),
                                "lesion_classes": CLASS_SEPARATOR.join(classes),
                            }
                        )

    return _finalize_manifest(stats, patients_all, studies_all)


def _new_manifest_entry(source_json: str) -> dict[str, Any]:
    return {
        "source_json": source_json,
        "institutions": set(),
        "patients": set(),
        "studies": set(),
        "series": set(),
        "files": 0,
        "annotations": 0,
        "files_without_annotations": 0,
        "classes": {},
    }


def _class_codes(annotations: list[dict[str, Any]]) -> dict[str, int]:
    """画像1枚に付いた病変クラスと、その annotation 件数。

    ``core.labels.parse_labels`` は呼ばない —— :class:`~..core.labels.Label` を
    1.7万件ぶん作る必要がなく、同一性は ``Label.key`` の定義どおり
    ``(code_system, code)`` だけで決まるため。
    挿入順を保つので、そのまま ``|`` で繋げば列の値になる。
    """
    codes: dict[str, int] = {}
    for annotation in annotations:
        seen: set[str] = set()
        for label in annotation.get("labels") or []:
            code_system = label.get("code_system")
            code = label.get("code")
            if not code_system or not code:
                continue
            qualified = f"{code_system}/{code}"
            if qualified in seen:
                continue
            seen.add(qualified)
            codes[qualified] = codes.get(qualified, 0) + 1
    return codes


def _finalize_manifest(
    stats: Mapping[str, dict[str, Any]],
    patients_all: set[str],
    studies_all: set[tuple[str, str]],
) -> dict[str, Any]:
    """集合を件数へ畳む。``patients`` / ``studies`` は実数（総和ではない）。"""
    datasets: dict[str, Any] = {}
    for dataset_id in sorted(stats):
        entry = stats[dataset_id]
        datasets[dataset_id] = {
            "dataset_id": dataset_id,
            "source_json": entry["source_json"],
            "institutions": len(entry["institutions"]),
            "patients": len(entry["patients"]),
            "studies": len(entry["studies"]),
            "series": len(entry["series"]),
            "files": entry["files"],
            "annotations": entry["annotations"],
            "files_without_annotations": entry["files_without_annotations"],
            # 件数降順→コード昇順。summary 側で上位N件を取るだけで済むように。
            "classes": dict(
                sorted(entry["classes"].items(), key=lambda kv: (-kv[1], kv[0]))
            ),
        }
    return {
        "patients": len(patients_all),
        "studies": len(studies_all),
        "datasets": datasets,
    }


# ------------------------------------------------------ 画像重複の解消（横断）

#: 監査用CSV/レポートの列。``keep``/``exclude`` という語は使わない —— 通常の
#: 採否（品質判断）とは別軸の「development.json 出力からのskip」であることを
#: 混同させないため。
DUPLICATE_REPORT_COLUMNS = (
    "image_path",
    "dataset_id",
    "file_uid",
    "has_kept_annotations",
    "n_kept_annotations",
    "decision",
    "reason",
)

#: `decision` 列の値。
SELECTED = "selected"
SKIPPED_DUPLICATE = "skipped_duplicate"


@dataclass(frozen=True)
class DuplicateCandidate:
    """同一 ``image_path`` を持つ、画像単位で keep 確定している候補1件。"""

    dataset_id: str
    source_json: str
    file_uid: str
    image_path: str
    has_kept_annotations: bool
    # 優先順位の判定には使わない（監査用の参考表示のみ）。
    n_kept_annotations: int


@dataclass
class DedupResult:
    """``resolve_image_duplicates`` の結果。

    ``skipped_file_uids`` は development.json への出力から**この回だけ**
    外す file_uid の集合。**品質上の exclude ではない** —— 「同じ物理画像を
    別データセット側で採用したため、今回の development.json 生成では
    見送った」件のみが入る。呼び出し側はこれを ``image_decisions.json`` 等の
    永続ファイルへ一切書き戻してはならない（`build-dataset` を実行するたびに
    その時点の状態から毎回計算し直す一時的な値）。
    """

    skipped_file_uids: frozenset[str]
    # 監査用の全候補一覧（採用/skipと理由込み）。重複が無かった image_path は
    # 含めない。CSV/ログ出力に使う。
    report_rows: list[dict[str, Any]] = field(default_factory=list)


def resolve_image_duplicates(
    image_rows: list[dict[str, Any]],
    selection_rows: list[dict[str, Any]],
    by_image: Mapping[str, str],
) -> DedupResult:
    """データセットを横断して同一画像（同じ ``image_path``）の重複を解消する。

    ``image_decisions.py``（単一データセット内の画像採否）にも
    ``build_development_json``（1元JSONにつき1回しか呼ばれない）にも
    他データセットとの重複は見えない。ここが唯一「全データセットの画像を
    横断して見る」場所になる。

    優先順位（同じ ``image_path`` を持つ複数 file_uid が候補になったとき）:

    1. keep となる annotation を1件以上持つ方を、持たない方より常に優先する
       （annotation件数の多寡は使わない — 「あり/なし」の二値だけを見る）
    2. 同率（両方あり、または両方なし）なら、元データセットJSONの生成日時
       （``source_generated_at``）が**新しい**方を優先する
       （D05のクロスデータセット重複判定とは意図的に逆向き。D05は
       「同一annotationの再エクスポート」なので最初に登録された方を残すが、
       ここでは「より新しく登録されたデータセットの状態を残す」という
       別の運用判断のため）
    3. 生成日時まで同一なら ``dataset_id`` の昇順で決定的に決める

    ``by_image`` は override 適用後（``image_decision_overrides.json``による
    exclude→keep反映後）の file_uid -> final_decision。ここで ``"keep"``
    でない候補（画像自体が最終的に exclude のもの）は、そもそも
    development.json に出ないため重複判定の対象にしない。

    戻り値の ``skipped_file_uids`` は development.json 生成時にだけ使う
    一時的な集合であり、``image_decisions.json`` 等へは絶対に書き戻さない
    （呼び出し側の責務。ここでは何のファイルも読み書きしない）。
    """
    kept_annotation_counts: dict[str, int] = {}
    for row in selection_rows:
        if row["final_decision"] == Decision.KEEP.value:
            file_uid = row["file_uid"]
            kept_annotation_counts[file_uid] = (
                kept_annotation_counts.get(file_uid, 0) + 1
            )

    candidates_by_path: dict[str, list[DuplicateCandidate]] = {}
    for row in image_rows:
        file_uid = row["file_uid"]
        if by_image.get(file_uid) != Decision.KEEP.value:
            continue
        n_kept = kept_annotation_counts.get(file_uid, 0)
        candidate = DuplicateCandidate(
            dataset_id=row["dataset_id"],
            source_json=row["source_json"],
            file_uid=file_uid,
            image_path=row["image_path"],
            has_kept_annotations=n_kept > 0,
            n_kept_annotations=n_kept,
        )
        candidates_by_path.setdefault(candidate.image_path, []).append(candidate)

    skipped: set[str] = set()
    report_rows: list[dict[str, Any]] = []
    for image_path, candidates in candidates_by_path.items():
        if len(candidates) < 2:
            continue
        winner = _pick_winner(candidates)
        for candidate in candidates:
            is_winner = candidate.file_uid == winner.file_uid
            report_rows.append(
                {
                    "image_path": image_path,
                    "dataset_id": candidate.dataset_id,
                    "file_uid": candidate.file_uid,
                    "has_kept_annotations": candidate.has_kept_annotations,
                    "n_kept_annotations": candidate.n_kept_annotations,
                    "decision": SELECTED if is_winner else SKIPPED_DUPLICATE,
                    "reason": _dedup_reason(candidate, candidates, is_winner),
                }
            )
            if not is_winner:
                skipped.add(candidate.file_uid)

    return DedupResult(skipped_file_uids=frozenset(skipped), report_rows=report_rows)


def _pick_winner(candidates: list[DuplicateCandidate]) -> DuplicateCandidate:
    """同一 ``image_path`` の候補群から1件を選ぶ。

    ``resolve_image_duplicates`` のdocstringにある優先順位そのもの。
    生成日時が取れない（ファイル名から解析できない）候補は、常に
    「取れる候補」より劣後させる —— 不明を有利に扱わない安全側の設計。

    実装上の工夫: 候補をあらかじめ ``dataset_id`` 昇順に並べてから
    ``max()`` で最優先の1件を選ぶ。``max()`` は同点のとき**最初に出会った
    要素**を返す（後から出てきた同点要素で置き換えない）ので、事前に
    dataset_id 昇順で並べておけば「(1)annotationの有無 (2)生成日時」が
    同点だったときに自動的に dataset_id 昇順の1件目が勝ち残る
    ——最終フォールバックのために別途分岐を書く必要が無い。
    """

    def sort_key(candidate: DuplicateCandidate) -> tuple[bool, float]:
        generated_at = source_generated_at(candidate.source_json)
        timestamp = (
            generated_at.timestamp() if generated_at is not None else float("-inf")
        )
        return (candidate.has_kept_annotations, timestamp)

    ordered = sorted(candidates, key=lambda c: c.dataset_id)
    return max(ordered, key=sort_key)


def _dedup_reason(
    candidate: DuplicateCandidate,
    group: list[DuplicateCandidate],
    is_winner: bool,
) -> str:
    """1候補ぶんの採用/skip理由を、グループ全体の構成から説明する。

    ``candidate`` と勝者だけを比べると、勝者自身については「自分自身との
    比較」になり常に一致してしまい、本当の決め手（annotationの有無）が
    埋もれる。そのため常に**グループ全体**の構成（annotationの有無が
    割れているか、生成日時が割れているか）を見て理由を決める。
    """
    mixed_annotations = len({c.has_kept_annotations for c in group}) > 1
    if mixed_annotations:
        if candidate.has_kept_annotations:
            return "annotationあり（他候補はannotationなし）"
        return "annotationなし（他候補にannotationありが存在）"

    reason = "annotationあり" if candidate.has_kept_annotations else "annotationなし"
    reason += "で同率、"
    dates = {source_generated_at(c.source_json) for c in group}
    if len(dates) > 1:
        return reason + (
            "登録日時が最も新しい" if is_winner else "登録日時が採用側より古い"
        )
    return reason + "登録日時も同一のためdataset_id昇順"
