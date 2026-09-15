"""``development_merged.json`` を lp-data 標準形式へ書き出す。

検証・採否（``checks/`` / ``selection/``）とは関心事が別なので分離してある。
入力は統合JSON（engineer-set 形式）、出力は
``docs/dataset_format.md`` の標準 Dataset 形式の JSON。

**属性の型と意味の正典はこのパッケージには無い。** med-chest-metry-pi6 の
``dataset_template_pneumothorax.yaml``（PR #68 で確定）を読んで ``meta.structure`` を
そのまま出し、こちらは ``samples`` を組み立てるだけ。structure をコード側に書き直すと、
テンプレートが更新されたときに黙って古いスキーマを出し続けることになる。

ラベルは**既存 annotation と明示的な正常情報だけ**から作る。読影レポートは使わない
（後日CSVで受け取り、独立した「ラベル更新処理」が既存の出力を更新する想定。
その seam として ``reader`` / ``writer`` / ``invariants`` を
builder から独立させてある）。
"""

from __future__ import annotations

from .export import ExportError, export_lpdata
from .invariants import InvariantViolation, check_sample, check_samples
from .labels import SampleLabels, build_labels
from .options import ExportOptions, ExportResult
from .reader import read_dataset, sample_ids
from .report import build_summary
from .sample import FIELD_BUILDERS
from .template import Template, TemplateDriftError, load_template, validate_coverage
from .writer import build_dataset, write_dataset

__all__ = [
    "FIELD_BUILDERS",
    "ExportError",
    "ExportOptions",
    "ExportResult",
    "InvariantViolation",
    "SampleLabels",
    "Template",
    "TemplateDriftError",
    "build_dataset",
    "build_labels",
    "build_summary",
    "check_sample",
    "check_samples",
    "export_lpdata",
    "load_template",
    "read_dataset",
    "sample_ids",
    "validate_coverage",
    "write_dataset",
]
