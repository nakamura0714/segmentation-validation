"""エクスポートの本体。CLI と Notebook はここを呼ぶだけ。

処理の順序:

1. テンプレートを読み、``structure`` とビルダのキーが一致するか検査する
2. 統合JSONをアダプタで開き、対象の画像を集める
3. ``image_file`` を用意する（DICOM→PNG 変換 / 予定パス / なし）
4. 導出値を計測する（マスクを読む。``--no-measure`` なら飛ばす）
5. ラベルを作り、サンプルを組み立てる
6. 書き出し → 不変条件の検査 → manifest / summary

ラベルは**既存 annotation と明示的な正常情報だけ**から作る。読影レポートは使わない。
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from ..adapters.engineer_set import EngineerSetAdapter
from ..config import Config
from ..core.records import FileGroup
from . import images as images_mod
from . import manifest as manifest_mod
from . import measure as measure_mod
from . import report as report_mod
from .invariants import check_samples
from .labels import CaseEvidence, build_labels, qualified
from .options import LABEL_SOURCE_REPORT, ExportOptions, ExportResult
from .paths import mask_path_for, sample_id_for
from .report_labels import ReportLabelError, check_schema, enrich_labels, in_scope
from .sample import FIELD_BUILDERS, SampleContext, build_sample
from .template import (
    Template,
    load_template,
    unresolved_placeholders,
    validate_coverage,
)
from .writer import build_dataset, write_dataset

logger = logging.getLogger(__name__)

TOOL_VERSION = "0.1.0"
#: ``normal_evidence`` に入っていない「正常らしき」ラベルを拾う語。
NORMAL_LIKE = "normal"
#: 「気胸データセットなのに気胸 annotation が0件」の候補を拾う語と、その除外語。
#: ``ETR_ChestMetry_PI6px_abnormal_non_pneumothorax`` は名前が
#: **気胸でないこと**を明示しているので、部分一致だけだと逆の意味で拾ってしまう。
PNEUMOTHORAX_IN_NAME = "pneumothorax"
NOT_PNEUMOTHORAX_IN_NAME = "non_pneumothorax"


class ExportError(RuntimeError):
    """エクスポートを続けられない（設定・入力の不備）。"""


def export_lpdata(config: Config, options: ExportOptions) -> ExportResult:
    """統合JSONを lp-data 形式へ書き出す。"""
    template = load_template(options.template_path)
    validate_coverage(template, set(FIELD_BUILDERS))

    if not options.merged_path.exists():
        raise ExportError(f"統合JSONが無い: {options.merged_path}")

    adapter = EngineerSetAdapter(source_path=options.merged_path, config=config)
    result = ExportResult(out_path=options.out_path)
    groups = _select_groups(adapter, options, result)
    if not groups:
        raise ExportError(
            "対象の画像が1件も無い"
            "（--only-dataset / --limit / --report-status を確認する）"
        )
    logger.info("対象: %d 画像", len(groups))

    if options.label_source == LABEL_SOURCE_REPORT:
        if not any(group.report_labels is not None for group in groups):
            raise ExportError(
                f"--label-source {LABEL_SOURCE_REPORT} を指定したが、"
                f"report_labels を持つ study が1件も無い: {options.merged_path}。"
                "構造化読影レポートを結合した engineer-set JSON を指定する"
            )
        # 形式が変わっていたら重い処理の前に落とす。
        check_schema(next(g.report_labels for g in groups if g.report_labels))

    # meta は**重い処理の前に**組み立てる。全件変換は数時間かかるので、
    # --owner の指定漏れのような設定の不備はその前に落とす。
    meta = _build_meta(template, options, adapter)

    image_results = images_mod.prepare_images(
        groups,
        image_output_dir=options.image_output_dir,
        mode=options.image_mode,
        on_missing_dicom=options.on_missing_dicom,
        jobs=options.jobs,
        force=options.force,
        provenance_path=_sidecar(options, "image_provenance.jsonl"),
    )

    if options.measures_pixels:
        measurements = measure_mod.measure_all(
            groups,
            config,
            mask_output_dir=options.mask_output_dir,
            write_mask=options.mask_mode == "generate",
            cache_path=_sidecar(options, "measurements.jsonl"),
            jobs=options.jobs,
            force=options.force,
        )
    else:
        measurements = measure_mod.empty_measurements(groups)

    samples = _build_samples(
        groups, options, config, image_results, measurements, result
    )

    # 集計はサンプルを組み立てないと出ないので、meta へは後から載せる
    # （meta 自体は --owner の指定漏れを重い処理の前に落とすため先に作ってある）。
    meta["provenance"]["case_evidence"] = _case_evidence_provenance(result)
    if options.label_source == LABEL_SOURCE_REPORT:
        meta["provenance"]["report_labels"] = _report_provenance(options, result)

    write_dataset(options.out_path, build_dataset(meta, samples))
    logger.info("lp-data JSON -> %s (%d サンプル)", options.out_path, len(samples))

    result.samples = len(samples)
    result.violations = [str(v) for v in check_samples(samples)]
    if result.violations:
        logger.warning("不変条件の違反が %d 件ある", len(result.violations))

    _write_artifacts(result, options, measurements, meta)
    return result


# ------------------------------------------------------------------ 内部


def _select_groups(
    adapter: EngineerSetAdapter, options: ExportOptions, result: ExportResult
) -> list[FileGroup]:
    """対象の画像を選ぶ。

    読影レポートの取り込み範囲（``--report-status``）は **``--limit`` より前**に
    効かせる。そうしないと ``--limit N`` が「先頭N件のうち対象のもの」になり、
    件数が読めなくなる。
    """
    wanted = frozenset(options.only_datasets)
    groups = [
        group
        for group in adapter.iter_files()
        if not wanted or group.dataset_id in wanted
    ]

    if options.report_statuses:
        kept: list[FileGroup] = []
        dropped: Counter[str] = Counter()
        # 範囲外のサンプルも分析用CSVには残す。除外した理由と certainty を
        # 捨てると、後から「どの確信度の症例を落としたのか」が追えなくなる。
        out_of_scope_rows: list[dict[str, Any]] = []
        for group in groups:
            if in_scope(group.report_labels, options.report_statuses):
                kept.append(group)
                continue
            report = group.report_labels
            status = report.pneumothorax_status if report else "(report_labelsなし)"
            dropped[status] += 1
            if report is not None:
                out_of_scope_rows.append(_out_of_scope_row(group))
        result.out_of_scope_counts = dict(dropped)
        result.followups["out_of_scope_rows"] = out_of_scope_rows
        if dropped:
            logger.info(
                "取り込み範囲外: %d 画像（%s）",
                sum(dropped.values()),
                " ".join(f"{k}={v}" for k, v in sorted(dropped.items())),
            )
        groups = kept

    if options.limit is not None:
        groups = groups[: options.limit]
    return groups


def _build_samples(
    groups: list[FileGroup],
    options: ExportOptions,
    config: Config,
    image_results: dict[str, images_mod.ImageResult],
    measurements: dict[str, measure_mod.SampleMeasurement],
    result: ExportResult,
) -> dict[str, dict[str, Any]]:
    """全サンプルを組み立てつつ、summary 用の集計も同時に取る。"""
    base = options.out_path.parent
    settings = config.lpdata_export
    samples: dict[str, dict[str, Any]] = {}

    status_counts: Counter[str] = Counter()
    status_case: Counter[str] = Counter()
    nulls: Counter[str] = Counter()
    vocabulary: Counter[str] = Counter()
    unlisted_normal: Counter[str] = Counter()
    pneumothorax_named: Counter[str] = Counter()
    errors: list[str] = []
    evidence = frozenset(settings.normal_evidence)

    # --- 読影レポート由来の集計（label_source が annotations+report のときだけ） ---
    use_report = options.label_source == LABEL_SOURCE_REPORT
    report_changes: Counter[str] = Counter()
    case_evidence_counts: Counter[str] = Counter()
    certainty_counts: Counter[str] = Counter()
    report_conflicts: list[str] = []
    needs_review: list[str] = []
    report_rows: list[dict[str, Any]] = []
    # ``pneumothorax_case: false`` のうち**明示的な気胸陰性**のID。
    # 統合（merge-lpdata）がこれを読んで、レポートによる false→true の昇格から
    # 守る。``label_source`` に関わらず常に集める —— development 側の出力にも要る。
    explicit_negative_ids: dict[str, list[str]] = {
        CaseEvidence.EXPLICIT_NEGATIVE_NORMAL.value: [],
        CaseEvidence.EXPLICIT_NEGATIVE_REPORT.value: [],
    }

    for group in groups:
        sample_id = sample_id_for(group)
        if sample_id in samples:
            # file_list のキーは統合JSON全体で一意（実測済み）。破れたら黙って
            # 上書きせず止める —— 1画像ぶんのGTが消えるため。
            raise ExportError(f"sample_id が重複している: {sample_id}")

        labels = build_labels(
            group,
            finding_code_systems=settings.finding_code_systems,
            normal_evidence=settings.normal_evidence,
        )
        if use_report:
            try:
                enriched = enrich_labels(labels, group.report_labels)
            except ReportLabelError as error:
                raise ExportError(f"{sample_id}: {error}") from error
            labels = enriched.labels
            for change in enriched.changes:
                report_changes[
                    f"{change.field}: {change.before!r} -> {change.after!r}"
                ] += 1
            report_conflicts.extend(f"{sample_id}: {c}" for c in enriched.conflicts)
            if group.report_labels is not None:
                report_rows.append(_report_row(sample_id, group, labels))
                if group.report_labels.needs_review:
                    needs_review.append(sample_id)
                # certainty は判定に使っていない。層別評価のために数えるだけ。
                certainty_counts[
                    group.report_labels.pneumothorax_certainty_max or "(なし)"
                ] += 1
        case_evidence_counts[labels.case_evidence.value] += 1
        if labels.case_evidence.value in explicit_negative_ids:
            explicit_negative_ids[labels.case_evidence.value].append(sample_id)

        measurement = measurements.get(group.file_uid) or measure_mod.SampleMeasurement(
            sample_id=sample_id, file_uid=group.file_uid
        )
        image = image_results.get(group.file_uid)
        mask_path = (
            mask_path_for(options.mask_output_dir, sample_id)
            if measurement.has_pneumothorax_mask and options.mask_mode != "none"
            else None
        )
        sample = build_sample(
            SampleContext(
                group=group,
                labels=labels,
                measurement=measurement,
                image_path=image.png_path if image else None,
                mask_path=mask_path,
                base=base,
                path_style=options.path_style,
            )
        )
        samples[sample_id] = sample

        # --- 集計 ---
        status_counts[labels.abnormal_finding_status] += 1
        status_case[f"{labels.abnormal_finding_status}/{labels.pneumothorax_case}"] += 1
        vocabulary.update(labels.finding_labels)
        for key, value in sample.items():
            if value is None or value == [] or value == {"pixel_array": None}:
                nulls[key] += 1
        errors.extend(f"{sample_id}: {e}" for e in measurement.errors)

        # --- 判断が要るものの収集 ---
        # 同じラベルが1画像に複数付いていても1件と数える（summary は「画像数」を出す）。
        normal_like = {
            qualified(label)
            for label in list(group.case_labels)
            + [label for record in group.records for label in record.labels]
            if label.code_text_eng == NORMAL_LIKE
        }
        for name in normal_like:
            if name not in evidence:
                unlisted_normal[name] += 1
        if not labels.pneumothorax_case and _named_pneumothorax(group.dataset_id):
            pneumothorax_named[group.dataset_id] += 1

    result.status_counts = dict(status_counts)
    result.status_case_counts = dict(status_case)
    result.null_counts = dict(nulls)
    result.finding_vocabulary = dict(vocabulary)
    result.image_counts = dict(Counter(r.status for r in image_results.values()))
    result.mask_counts = dict(
        Counter(
            "written"
            if m.mask_written
            else ("planned" if m.has_pneumothorax_mask else "none")
            for m in measurements.values()
        )
    )
    # ``_select_groups`` が入れた範囲外の行を消さないよう update する。
    result.followups.update(
        {
            "unlisted_normal_like": dict(unlisted_normal),
            "pneumothorax_named_without_annotation": dict(pneumothorax_named),
            "measurement_errors": errors,
        }
    )
    result.case_evidence_counts = dict(case_evidence_counts)
    result.explicit_negative_ids = explicit_negative_ids
    if use_report:
        result.report_changes = dict(report_changes)
        result.certainty_counts = dict(certainty_counts)
        result.followups["report_label_conflicts"] = report_conflicts
        result.followups["report_needs_review"] = needs_review
        result.followups["report_rows"] = report_rows
    return samples


def _out_of_scope_row(group: FileGroup) -> dict[str, Any]:
    """取り込み範囲外のサンプル1行。**ラベルは作っていないので空**にする。

    出力JSONには入らないが、除外した症例の ``pneumothorax_status`` と certainty を
    残しておかないと「どの確信度の症例を何件落としたか」が後から追えない。
    """
    report = group.report_labels
    assert report is not None
    row = _report_fields(report)
    row.update(
        {
            "sample_id": group.file,
            "study_name": report.study_name or group.study,
            "source_dataset_id": report.source_dataset_id,
            "source_json": report.source_json,
            "in_scope": False,
            # 範囲外なのでサンプルを組み立てていない。判定結果は空にする。
            "pneumothorax_case": None,
            "case_evidence": None,
            "pneumothorax_side": None,
            "bulla_bleb_status": None,
            "abnormal_finding_status": None,
        }
    )
    return row


def _report_fields(report: Any) -> dict[str, Any]:
    """レポート由来の素の値（判定結果を含まない部分）。

    **certainty をここで保持する。** 学習GTではないので出力JSONの属性にはしないが、
    後から certainty 別に抽出・性能評価できるよう捨てない。
    """
    return {
        "sample_id": "",
        "study_name": None,
        "source_dataset_id": None,
        "source_json": None,
        "in_scope": True,
        "pneumothorax_status": report.pneumothorax_status,
        "pneumothorax_case": None,
        "case_evidence": None,
        "pneumothorax_side": None,
        "pneumothorax_subtype": report.pneumothorax_subtype,
        "pneumothorax_evidence": report.pneumothorax_evidence,
        "pneumothorax_certainty_max": report.pneumothorax_certainty_max,
        "pneumothorax_certainty_counts": dict(report.pneumothorax_certainty_counts),
        "pneumothorax_absent_certainty_counts": dict(
            report.pneumothorax_absent_certainty_counts
        ),
        "bulla_bleb_status": None,
        "bulla_bleb_evidence": report.bulla_bleb_evidence,
        "bulla_bleb_certainty_max": report.bulla_bleb_certainty_max,
        "bulla_bleb_certainty_counts": dict(report.bulla_bleb_certainty_counts),
        "abnormal_finding_status": None,
        "observed_finding_count": report.observed_finding_count,
        "flags": list(report.flags),
        "needs_review": report.needs_review,
    }


def _report_row(sample_id: str, group: FileGroup, labels: Any) -> dict[str, Any]:
    """分析用CSV ``<prefix>report_labels.csv`` の1行。

    **certainty をここで保持する。** 学習GTではないので出力JSONの属性にはしないが、
    後から certainty 別に層別評価・性能評価できるよう捨てずに残す
    （``pneumothorax_case`` の判定には一切使っていない）。
    """
    report = group.report_labels
    assert report is not None
    row = _report_fields(report)
    row.update(
        {
            "sample_id": sample_id,
            "study_name": report.study_name or group.study,
            "source_dataset_id": report.source_dataset_id,
            "source_json": report.source_json,
            "in_scope": True,
            "pneumothorax_case": labels.pneumothorax_case,
            "case_evidence": labels.case_evidence.value,
            "pneumothorax_side": labels.pneumothorax_side,
            "bulla_bleb_status": labels.bulla_bleb_status,
            "abnormal_finding_status": labels.abnormal_finding_status,
        }
    )
    return row


#: ``provenance.case_evidence`` ブロックの形式版。増やすときは上げる
#: （``merge`` が未知の版を黙って解釈しないようにするため）。
CASE_EVIDENCE_SCHEMA_VERSION = 1


def _case_evidence_provenance(result: ExportResult) -> dict[str, Any]:
    """``provenance.case_evidence``。**明示的な気胸陰性のIDだけ**を載せる。

    ``pneumothorax_case: false`` には「陰性だと分かっている」と「確認できていない」
    の2種類があり、統合（``merge-lpdata``）が読影レポートで ``true`` へ昇格させて
    よいのは後者だけ。その区別は ``labels.build_labels`` が作る ``case_evidence``
    でしか付かないが、テンプレートに無い属性はサンプルに載せられない
    （``template.validate_coverage`` が弾く）ので、ここへ出す。

    **``abnormal_finding_status`` で代用しないこと。** あれは「気胸以外も含む
    異常所見の有無」で、気胸の陰性根拠ではない。他の所見が併存すると ``present``
    になり、明示的な正常ラベルがあっても隠れる（実測で明示的陰性 880 件のうち
    167 件がこの形）。

    陽性（``explicit_positive_annotation``）は載せない —— ``pneumothorax_case:
    true`` で自明であり、統合側は ``true`` を下げないため。
    """
    return {
        "schema_version": CASE_EVIDENCE_SCHEMA_VERSION,
        **{
            key: sorted(ids)
            for key, ids in sorted(result.explicit_negative_ids.items())
        },
    }


def _report_provenance(options: ExportOptions, result: ExportResult) -> dict[str, Any]:
    """``provenance.report_labels``。**集計だけ**を載せる。

    1行1サンプルの記録は ``<prefix>report_labels.csv`` が正本。meta に全件入れると
    データセットJSONが読めない大きさになる。

    ``certainty_is_metadata`` を明示するのは、この値が**学習GTではなく層別評価用**
    だと後から読む人に分かるようにするため。
    """
    return {
        "certainty_is_metadata": True,
        "certainty_max_counts": dict(sorted(result.certainty_counts.items())),
        "case_evidence_counts": dict(sorted(result.case_evidence_counts.items())),
        "report_statuses": list(options.report_statuses),
        "out_of_scope_counts": dict(sorted(result.out_of_scope_counts.items())),
        "csv": f"{options.artifact_prefix}report_labels.csv",
    }


def _build_meta(
    template: Template, options: ExportOptions, adapter: EngineerSetAdapter
) -> dict[str, Any]:
    """出力の ``meta`` を作る。

    テンプレートの管理情報を土台にし、このエクスポートが決めるもの
    （``content_type`` / ``date`` / ``source`` / ``provenance`` /
    ``structure``）を載せる。
    ``split`` は書かない（本エクスポータは分割をしない）。
    """
    meta: dict[str, Any] = dict(template.meta)
    if options.dataset_name:
        meta["dataset_name"] = options.dataset_name
    if options.dataset_id:
        meta["dataset_id"] = options.dataset_id
    if options.owner:
        meta["owner"] = options.owner

    meta["content_type"] = "dataset"
    meta["date"] = date.today().isoformat()
    meta["source"] = options.merged_path.name
    meta["provenance"] = {
        "tool": "segmentation-validation",
        "tool_version": TOOL_VERSION,
        "merged_json": str(options.merged_path),
        "merged_sha256": _sha256(options.merged_path),
        "merged_fingerprint": adapter.meta_development.get("fingerprint"),
        "template": str(options.template_path),
        # ラベルを何から作ったか。"annotations" は既存 annotation と明示的な
        # 正常情報だけ、"annotations+report" はそこへ構造化読影レポートを重ねたもの。
        "label_source": options.label_source,
        "image_mode": options.image_mode,
        "mask_mode": options.mask_mode,
        "path_style": options.path_style,
        "measured": options.measure,
    }

    placeholders = unresolved_placeholders(meta)
    if placeholders:
        raise ExportError(
            "テンプレートのダミー値が解決されていない: "
            + ", ".join(placeholders)
            + "（--dataset-name / --dataset-id / --owner で指定する）"
        )
    meta["structure"] = template.structure
    return meta


def _write_artifacts(
    result: ExportResult,
    options: ExportOptions,
    measurements: dict[str, measure_mod.SampleMeasurement],
    meta: dict[str, Any],
) -> None:
    if options.mask_mode != "none":
        manifest = manifest_mod.build_manifest(
            measurements.values(),
            mask_output_dir=options.mask_output_dir,
            mask_mode=options.mask_mode,
        )
        result.artifacts["mask_merge_manifest"] = manifest_mod.write_manifest(
            _sidecar(options, "mask_merge_manifest.json"), manifest
        )

    # 範囲内・範囲外の両方を1つのCSVに出す（in_scope 列で区別する）。
    # 除外した分の certainty を捨てないため。
    rows = list(result.followups.get("report_rows") or [])
    rows += list(result.followups.get("out_of_scope_rows") or [])
    if rows:
        # certainty を残す分析用CSV。学習GTではないので出力JSONには入れない。
        result.artifacts["report_labels_csv"] = report_mod.write_report_labels_csv(
            _sidecar(options, "report_labels.csv"), rows
        )
        logger.info(
            "レポートラベルCSV -> %s（%d 行）",
            result.artifacts["report_labels_csv"],
            len(rows),
        )

    summary_path = _sidecar(options, "lpdata_export_summary.md")
    result.artifacts["summary"] = report_mod.write_summary(
        summary_path, report_mod.build_summary(result, options, meta)
    )
    logger.info("summary -> %s", summary_path)


def _named_pneumothorax(dataset_id: str) -> bool:
    """データセット名が気胸症例だと言っているか。

    ``*_abnormal_non_pneumothorax`` は**気胸でないこと**を名前で明示しているので、
    部分一致だけだと逆の意味の3,702件を「気胸なのに未アノテーション」として
    挙げてしまう。これはあくまで人間に確認を促す候補一覧なので、
    確実に違うものは出さない。
    """
    name = dataset_id.lower()
    if NOT_PNEUMOTHORAX_IN_NAME in name:
        return False
    return PNEUMOTHORAX_IN_NAME in name


def _sidecar(options: ExportOptions, name: str) -> Path:
    """出力JSONの隣に置く作業ファイル（キャッシュ・provenance・summary）。

    ``artifact_prefix`` を付けるのは、同じタグディレクトリへ2回書き出すとき
    （development と OFC）に summary / manifest を潰し合わせないため。
    計測キャッシュも分けないと ``--force`` が相手のキャッシュごと消す。
    """
    return options.out_path.parent / f"{options.artifact_prefix}{name}"


def _sha256(path: Path) -> str:
    """100MB 級のファイルなので分割して読む。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
