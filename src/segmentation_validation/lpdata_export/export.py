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
from .labels import build_labels, qualified
from .options import ExportOptions, ExportResult
from .paths import mask_path_for, sample_id_for
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
    groups = _select_groups(adapter, options)
    if not groups:
        raise ExportError(
            "対象の画像が1件も無い（--only-dataset / --limit を確認する）"
        )
    logger.info("対象: %d 画像", len(groups))

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

    result = ExportResult(out_path=options.out_path)
    samples = _build_samples(
        groups, options, config, image_results, measurements, result
    )

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
    adapter: EngineerSetAdapter, options: ExportOptions
) -> list[FileGroup]:
    wanted = frozenset(options.only_datasets)
    groups = [
        group
        for group in adapter.iter_files()
        if not wanted or group.dataset_id in wanted
    ]
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
    result.followups = {
        "unlisted_normal_like": dict(unlisted_normal),
        "pneumothorax_named_without_annotation": dict(pneumothorax_named),
        "measurement_errors": errors,
    }
    return samples


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
        # 後日の「読影レポートCSVによるラベル更新処理」が書き換える印。
        # 初期エクスポートは既存 annotation だけを使う。
        "label_source": "annotations",
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
    out_dir = options.out_path.parent
    if options.mask_mode != "none":
        manifest = manifest_mod.build_manifest(
            measurements.values(),
            mask_output_dir=options.mask_output_dir,
            mask_mode=options.mask_mode,
        )
        result.artifacts["mask_merge_manifest"] = manifest_mod.write_manifest(
            out_dir / "mask_merge_manifest.json", manifest
        )
    summary_path = out_dir / "lpdata_export_summary.md"
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
    """出力JSONの隣に置く作業ファイル（キャッシュ・provenance）。"""
    return options.out_path.parent / name


def _sha256(path: Path) -> str:
    """100MB 級のファイルなので分割して読む。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
