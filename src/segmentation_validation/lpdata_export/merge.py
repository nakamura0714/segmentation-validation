"""2つの lp-data データセットJSONを1つに統合する。

入力は**どちらも lp-data 標準形式**（engineer-set ではない）。
``reader.read_dataset`` で読み、``writer.write_dataset`` で書く。

## 重複したサンプルの扱い

``sample_id`` が衝突したら **primary を残す**。secondary のサンプルは追加しない。
ただし secondary のラベルで primary を**補強**する。

優先順位は **明示的な陰性/陽性GT > 読影レポート > annotation/mask が無いだけの
未確認状態**（``report_labels.py`` の根拠モデルと同じ）。

**画素由来の値（``image_file`` / マスク3種 / 面積 / ``lung_rect``）は primary の
ものを必ず採る。** secondary 側のマスクを参照すると、人手で整備したGTが
黙って別のファイルに差し替わる。食い違いは ``mask_conflicts`` に出して人が見る。

## 触らないもの

``abnormal_finding_status`` は統合で書き換えない。読影レポート側は policy 未確定で
常に ``unknown`` を返すので、写すと annotation 由来の判定（present / absent）を
``unknown`` で潰すことになる。

## certainty

``definite`` / ``probable`` / ``possible`` / ``unlikely`` は**裁定に一切使わない**。
裁定一覧には参考値として載せるが、``reason`` が示すとおり判断材料にはしていない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .labels import ABSENT, PRESENT, UNKNOWN
from .reader import read_dataset
from .report_labels import SIDES, LabelChange, LabelConflict
from .writer import build_dataset, write_dataset

logger = logging.getLogger(__name__)

TOOL_VERSION = "0.1.0"

#: 裁定一覧の形式版。
RESOLUTIONS_SCHEMA_VERSION = 1

#: 同じ画像なら一致していなければならない属性。食い違ったら値を変えずに記録する。
IDENTITY_FIELDS: tuple[str, ...] = (
    "patient_id",
    "dicom_file",
    "facility_id",
    "vendor_id",
    "image_shape",
    "pixel_spacing",
)

#: 画素由来の値。**必ず primary を採る。**
PIXEL_FIELDS: tuple[str, ...] = (
    "image_file",
    "pneumothorax_mask",
    "lung_mask",
    "thorax_mask",
    "lung_rect",
    "pneumothorax_area_pix2",
    "pneumothorax_area_mm2",
)

CASE_KEY = "pneumothorax_case"
SIDE_KEY = "pneumothorax_side"
BULLA_KEY = "bulla_bleb_status"
STATUS_KEY = "abnormal_finding_status"
MASK_KEY = "pneumothorax_mask"
AREA_KEY = "pneumothorax_area_pix2"


class MergeError(RuntimeError):
    """統合を続けられない（構造の不一致・入力の不備）。"""


@dataclass(frozen=True)
class MergeOptions:
    """1回の統合の入力一式。"""

    primary_path: Path
    secondary_path: Path
    out_path: Path
    template_path: Path | None = None
    dataset_name: str | None = None
    dataset_id: str | None = None
    owner: str | None = None
    #: True なら**データセットJSONを書かない**。裁定の要約と一覧だけ出す。
    dry_run: bool = False
    #: True なら重複時にラベル補強をせず primary をそのまま残す。
    no_enrich: bool = False


@dataclass
class Resolution:
    """重複したサンプル1件の裁定。"""

    sample_id: str
    changes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    #: 参考値。**裁定には使っていない。**
    certainty_max: str | None = None
    report_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "changes": self.changes,
            "conflicts": self.conflicts,
            "report_status": self.report_status,
            # 参考値。certainty は裁定に使っていない（reason を見ること）。
            "certainty_max": self.certainty_max,
        }


@dataclass
class MergeReport:
    """統合1回の結果。summary と裁定一覧はここから作る。"""

    out_path: Path
    dry_run: bool = False
    primary_path: Path | None = None
    secondary_path: Path | None = None
    primary_samples: int = 0
    secondary_samples: int = 0
    merged_samples: int = 0
    duplicates: int = 0
    added_from_secondary: int = 0
    enriched: int = 0
    #: primary の「明示的な気胸陰性」の件数（昇格から守った対象の母数）。
    protected: int = 0
    change_counts: dict[str, int] = field(default_factory=dict)
    conflict_counts: dict[str, int] = field(default_factory=dict)
    resolutions: list[Resolution] = field(default_factory=list)
    identity_conflicts: list[str] = field(default_factory=list)
    mask_conflicts: list[str] = field(default_factory=list)
    case_counts_before: dict[str, int] = field(default_factory=dict)
    case_counts_after: dict[str, int] = field(default_factory=dict)
    status_counts_after: dict[str, int] = field(default_factory=dict)
    side_counts_after: dict[str, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    artifacts: dict[str, Path] = field(default_factory=dict)


# ------------------------------------------------------------ 純関数（統合本体）


def _structure_of(dataset: Mapping[str, Any], label: str) -> dict[str, Any]:
    structure = (dataset.get("meta") or {}).get("structure")
    if not isinstance(structure, dict) or not structure:
        raise MergeError(f"{label} に meta.structure が無い")
    return structure


def check_structures(
    primary: Mapping[str, Any], secondary: Mapping[str, Any]
) -> dict[str, Any]:
    """両入力の ``meta.structure`` が同一か検査し、統合後の structure を返す。

    別のテンプレートで作られたものを黙って混ぜない。属性の意味がサンプルごとに
    変わるファイルができてしまう。
    """
    left = _structure_of(primary, "primary")
    right = _structure_of(secondary, "secondary")
    if left == right:
        return left

    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    differing = sorted(k for k in set(left) & set(right) if left[k] != right[k])
    lines = ["2つの入力の meta.structure が一致しない（統合できない）"]
    if only_left:
        lines.append(f"  primary だけにある属性: {only_left}")
    if only_right:
        lines.append(f"  secondary だけにある属性: {only_right}")
    if differing:
        lines.append(f"  定義が違う属性: {differing}")
    raise MergeError("\n".join(lines))


def check_sample_keys(
    samples: Mapping[str, Mapping[str, Any]],
    structure: Mapping[str, Any],
    label: str,
) -> None:
    """全サンプルのキー集合が structure と一致するか検査する。

    欠けたまま書き出すと lp-data が読めないファイルになる。
    """
    expected = set(structure)
    for sample_id, sample in samples.items():
        actual = set(sample)
        if actual == expected:
            continue
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise MergeError(
            f"{label} の {sample_id} が structure と食い違う"
            f"（不足: {missing} / 余分: {extra}）"
        )


#: 対応する ``provenance.case_evidence`` の形式版。
CASE_EVIDENCE_SCHEMA_VERSION = 1

#: 保護対象の根拠。どちらも「気胸について陰性だと分かっている」を意味する。
PROTECTED_EVIDENCE: tuple[str, ...] = (
    "explicit_negative_normal",
    "explicit_negative_report",
)


def protected_ids(dataset: Mapping[str, Any], label: str) -> frozenset[str]:
    """``pneumothorax_case`` を ``true`` へ上げてはいけないサンプル。

    **明示的な気胸陰性**（annotation 側に ``No Findings/normal`` がある、または
    レポートが陽性所見0件を確認した）のIDを ``provenance.case_evidence`` から読む。

    ⚠️ **``abnormal_finding_status`` で代用しないこと。** あれは「気胸以外も含む
    異常所見の有無」であって気胸の陰性根拠ではない。他の所見が併存すると
    ``present`` になり、明示的な正常ラベルがあっても隠れる（実測で明示的陰性
    880 件のうち 167 件がこの形で、代用すると 19% を取りこぼす）。

    根拠が無い入力は**黙って通さない**。根拠が無いまま昇格させるのは、
    確認済みの陰性をレポートで塗り潰す経路そのものだから。
    """
    provenance = (dataset.get("meta") or {}).get("provenance") or {}
    evidence = provenance.get("case_evidence")
    if not isinstance(evidence, dict):
        raise MergeError(
            f"{label} に meta.provenance.case_evidence が無い。\n"
            "  pneumothorax_case の補完が安全か判定できない"
            "（明示的な気胸陰性を上書きする恐れがある）。\n"
            "  export-lpdata を再実行して作り直すか、--no-enrich で"
            "ラベル補強を止める"
        )
    version = evidence.get("schema_version")
    if version != CASE_EVIDENCE_SCHEMA_VERSION:
        raise MergeError(
            f"{label} の case_evidence が非対応の形式版: {version}"
            f"（対応は {CASE_EVIDENCE_SCHEMA_VERSION}）。"
            "export 側の変更に追随してから再実行する"
        )
    ids: set[str] = set()
    for key in PROTECTED_EVIDENCE:
        ids.update(evidence.get(key) or ())
    return frozenset(ids)


def _has_mask(sample: Mapping[str, Any]) -> bool:
    mask = sample.get(MASK_KEY)
    return isinstance(mask, dict) and mask.get("pixel_array") is not None


def enrich_sample(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    is_explicit_negative: bool = False,
) -> tuple[dict[str, Any], list[LabelChange], list[LabelConflict]]:
    """重複したサンプル1件を補強する。

    ``primary`` を土台にし、**下げる方向には一切動かさない**。
    画素由来の値は触らない（``PIXEL_FIELDS``）。

    ``is_explicit_negative`` は「primary の ``pneumothorax_case: false`` が
    **確認済みの陰性**か」。``protected_ids`` が ``provenance.case_evidence``
    から作る。True なら secondary が ``true`` でも上げない。
    """
    merged = dict(primary)
    changes: list[LabelChange] = []
    conflicts: list[LabelConflict] = []

    # --- pneumothorax_case: false -> true だけ。certainty は見ない ---
    # **primary の false が「明示的な陰性」なら上げない。**
    # 判定は ``provenance.case_evidence`` 由来の ``is_explicit_negative`` で行う
    # （``abnormal_finding_status`` は気胸の陰性根拠ではないので使わない。
    # ``protected_ids`` の docstring 参照）。
    base_case = bool(primary.get(CASE_KEY))
    other_case = bool(secondary.get(CASE_KEY))
    if other_case and not base_case:
        if is_explicit_negative:
            conflicts.append(
                LabelConflict(CASE_KEY, False, True, False, "explicit_negative_wins")
            )
        else:
            merged[CASE_KEY] = True
            changes.append(LabelChange(CASE_KEY, False, True, "report_positive"))
    elif base_case and not other_case:
        conflicts.append(LabelConflict(CASE_KEY, True, False, True, "never_downgrade"))

    # --- pneumothorax_side: null のとき、かつ補強後 case が true のときだけ ---
    side = secondary.get(SIDE_KEY)
    if side is not None and side not in SIDES:
        raise MergeError(f"secondary の {SIDE_KEY} が想定外の値: {side!r}")
    if side is not None and merged.get(CASE_KEY):
        if primary.get(SIDE_KEY) is None:
            merged[SIDE_KEY] = side
            changes.append(LabelChange(SIDE_KEY, None, side, "report_side"))
        elif primary.get(SIDE_KEY) != side:
            conflicts.append(
                LabelConflict(
                    SIDE_KEY,
                    primary.get(SIDE_KEY),
                    side,
                    primary.get(SIDE_KEY),
                    "side_already_set",
                )
            )

    # --- bulla_bleb_status: unknown からだけ上げる ---
    bulla = secondary.get(BULLA_KEY)
    if bulla in (PRESENT, ABSENT):
        if primary.get(BULLA_KEY) == UNKNOWN:
            merged[BULLA_KEY] = bulla
            changes.append(LabelChange(BULLA_KEY, UNKNOWN, bulla, "report_bulla_bleb"))
        elif primary.get(BULLA_KEY) != bulla:
            conflicts.append(
                LabelConflict(
                    BULLA_KEY,
                    primary.get(BULLA_KEY),
                    bulla,
                    primary.get(BULLA_KEY),
                    "never_downgrade",
                )
            )

    # --- abnormal_finding_status は触らない ---
    # secondary（読影レポート由来）は policy 未確定で常に unknown を返すので、
    # 写すと annotation 由来の present / absent を潰す。

    # --- 画素由来の値は必ず primary ---
    for key in PIXEL_FIELDS:
        if key in primary:
            merged[key] = primary[key]

    return merged, changes, conflicts


def merge_samples(
    primary: Mapping[str, Mapping[str, Any]],
    secondary: Mapping[str, Mapping[str, Any]],
    report: MergeReport,
    *,
    enrich: bool = True,
    protected: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """サンプルを統合する。``report`` に裁定の内訳を書き込む。

    ``protected`` は ``pneumothorax_case`` を上げてはいけないサンプルID
    （``protected_ids`` が primary の ``provenance.case_evidence`` から作る）。
    """
    merged: dict[str, dict[str, Any]] = {}
    change_counts: Counter[str] = Counter()
    conflict_counts: Counter[str] = Counter()

    for sample_id, sample in primary.items():
        other = secondary.get(sample_id)
        if other is None:
            merged[sample_id] = dict(sample)
            continue

        report.duplicates += 1
        _check_identity(sample_id, sample, other, report)
        _check_mask(sample_id, sample, other, report)

        if not enrich:
            merged[sample_id] = dict(sample)
            continue

        new_sample, changes, conflicts = enrich_sample(
            sample, other, is_explicit_negative=sample_id in protected
        )
        merged[sample_id] = new_sample
        if changes or conflicts:
            resolution = Resolution(
                sample_id=sample_id,
                changes=[str(c) for c in changes],
                conflicts=[str(c) for c in conflicts],
            )
            report.resolutions.append(resolution)
        if changes:
            report.enriched += 1
        for change in changes:
            change_counts[f"{change.field}: {change.before!r} -> {change.after!r}"] += 1
        for conflict in conflicts:
            conflict_counts[f"{conflict.field} ({conflict.reason})"] += 1

    for sample_id, sample in secondary.items():
        if sample_id in merged:
            continue
        merged[sample_id] = dict(sample)
        report.added_from_secondary += 1

    report.change_counts = dict(change_counts)
    report.conflict_counts = dict(conflict_counts)
    return merged


def _check_identity(
    sample_id: str,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    report: MergeReport,
) -> None:
    """同じ画像を指しているか。値は変えず、食い違いだけ記録する。"""
    for key in IDENTITY_FIELDS:
        if key not in primary or key not in secondary:
            continue
        if primary[key] != secondary[key]:
            report.identity_conflicts.append(
                f"{sample_id}: {key} が違う "
                f"(primary={primary[key]!r} / secondary={secondary[key]!r})"
            )


def _check_mask(
    sample_id: str,
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    report: MergeReport,
) -> None:
    """気胸マスクの食い違いを記録する。**primary を採るのは変えない。**

    マスクのファイル名は ``<sample_id>.png`` で置き場が共有なので、ここを
    見落とすと人手で整備したGTが黙って差し替わる。
    """
    if _has_mask(primary) != _has_mask(secondary):
        report.mask_conflicts.append(
            f"{sample_id}: 気胸マスクの有無が違う "
            f"(primary={'あり' if _has_mask(primary) else 'なし'} / "
            f"secondary={'あり' if _has_mask(secondary) else 'なし'}) "
            "→ primary を採用"
        )
        return
    if primary.get(AREA_KEY) != secondary.get(AREA_KEY):
        report.mask_conflicts.append(
            f"{sample_id}: 気胸マスクの面積が違う "
            f"(primary={primary.get(AREA_KEY)} / secondary={secondary.get(AREA_KEY)}) "
            "→ primary を採用"
        )


# --------------------------------------------------------------------- I/O


def merge_lpdata(options: MergeOptions) -> MergeReport:
    """2つの lp-data JSON を統合する。"""
    from .invariants import check_samples
    from .template import load_template, validate_coverage

    for path, label in (
        (options.primary_path, "primary"),
        (options.secondary_path, "secondary"),
    ):
        if not path.exists():
            raise MergeError(f"{label} のデータセットJSONが無い: {path}")

    primary = read_dataset(options.primary_path)
    secondary = read_dataset(options.secondary_path)
    structure = check_structures(primary, secondary)
    if options.template_path is not None:
        # テンプレート側が正典。統合結果の属性集合がテンプレートと一致するか見る。
        validate_coverage(load_template(options.template_path), set(structure))

    check_sample_keys(primary["samples"], structure, "primary")
    check_sample_keys(secondary["samples"], structure, "secondary")

    report = MergeReport(
        out_path=options.out_path,
        dry_run=options.dry_run,
        primary_path=options.primary_path,
        secondary_path=options.secondary_path,
        primary_samples=len(primary["samples"]),
        secondary_samples=len(secondary["samples"]),
    )
    report.case_counts_before = _case_counts(primary["samples"])

    # 明示的な気胸陰性の保護対象。``--no-enrich`` ならラベルを触らないので要らない
    # （根拠を持たない古い出力でも統合だけはできるようにしておく）。
    protected = frozenset() if options.no_enrich else protected_ids(primary, "primary")
    report.protected = len(protected)

    samples = merge_samples(
        primary["samples"],
        secondary["samples"],
        report,
        enrich=not options.no_enrich,
        protected=protected,
    )
    report.merged_samples = len(samples)
    report.case_counts_after = _case_counts(samples)
    report.status_counts_after = dict(
        Counter(str(s.get(STATUS_KEY)) for s in samples.values())
    )
    report.side_counts_after = dict(
        Counter(str(s.get(SIDE_KEY)) for s in samples.values())
    )
    report.violations = [str(v) for v in check_samples(samples)]

    # 裁定一覧と要約は dry-run でも必ず書く（統合前に人が確認するための材料）。
    report.artifacts["resolutions"] = _write_resolutions(options, report)

    if options.dry_run:
        report.artifacts["summary"] = _write_summary(options, report)
        logger.info(
            "dry-run: データセットJSONは書いていない（裁定は %s）",
            report.artifacts["summary"],
        )
        return report

    meta = _build_meta(primary, secondary, options, report, structure)
    write_dataset(options.out_path, build_dataset(meta, samples))
    logger.info("統合 lp-data JSON -> %s (%d サンプル)", options.out_path, len(samples))
    report.artifacts["summary"] = _write_summary(options, report)
    return report


def _case_counts(samples: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    """``pneumothorax_case`` の分布と、うちマスク無しの件数。"""
    counts: Counter[str] = Counter()
    for sample in samples.values():
        case = bool(sample.get(CASE_KEY))
        counts["true" if case else "false"] += 1
        if case and not _has_mask(sample):
            counts["true(マスク無し)"] += 1
    return dict(counts)


def _build_meta(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    options: MergeOptions,
    report: MergeReport,
    structure: Mapping[str, Any],
) -> dict[str, Any]:
    """統合後の ``meta``。primary を土台に provenance を差し替える。"""
    from .template import unresolved_placeholders

    meta: dict[str, Any] = dict(primary["meta"])
    if options.dataset_name:
        meta["dataset_name"] = options.dataset_name
    if options.dataset_id:
        meta["dataset_id"] = options.dataset_id
    if options.owner:
        meta["owner"] = options.owner

    meta["content_type"] = "dataset"
    meta["date"] = date.today().isoformat()
    meta["source"] = f"{options.primary_path.name} + {options.secondary_path.name}"
    meta["provenance"] = {
        "tool": "segmentation-validation",
        "tool_version": TOOL_VERSION,
        "label_source": "annotations+structured_report",
        "inputs": [
            _input_provenance("primary", options.primary_path, primary),
            _input_provenance("secondary", options.secondary_path, secondary),
        ],
        "merge": {
            "duplicates": report.duplicates,
            "enriched": report.enriched,
            "added_from_secondary": report.added_from_secondary,
            "changes": report.change_counts,
            "conflicts": report.conflict_counts,
            "identity_conflicts": len(report.identity_conflicts),
            "mask_conflicts": len(report.mask_conflicts),
            # 明示的な気胸陰性は provenance.case_evidence を根拠に守っている
            # （abnormal_finding_status では代用しない）。
            "protected_explicit_negative": report.protected,
            # 重複時は primary の画素由来の値を必ず採る（GTマスクを守るため）。
            "pixel_fields_from": "primary",
            # certainty は裁定に使っていない。
            "certainty_used_in_resolution": False,
        },
    }

    placeholders = unresolved_placeholders(meta)
    if placeholders:
        raise MergeError(
            "テンプレートのダミー値が解決されていない: "
            + ", ".join(placeholders)
            + "（--dataset-name / --dataset-id / --owner で指定する）"
        )
    meta["structure"] = dict(structure)
    return meta


def _input_provenance(
    role: str, path: Path, dataset: Mapping[str, Any]
) -> dict[str, Any]:
    meta = dataset.get("meta") or {}
    return {
        "role": role,
        "path": str(path),
        "sha256": _sha256(path),
        "samples": len(dataset.get("samples") or {}),
        "dataset_id": meta.get("dataset_id"),
        "provenance": meta.get("provenance"),
    }


def _write_resolutions(options: MergeOptions, report: MergeReport) -> Path:
    """裁定一覧。dry-run でも書く。"""
    path = options.out_path.parent / "lpdata_merge_resolutions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RESOLUTIONS_SCHEMA_VERSION,
        "dry_run": options.dry_run,
        "primary": str(options.primary_path),
        "secondary": str(options.secondary_path),
        "duplicates": report.duplicates,
        "enriched": report.enriched,
        "added_from_secondary": report.added_from_secondary,
        # certainty は参考値として載せるが、裁定には使っていない。
        "certainty_used_in_resolution": False,
        "identity_conflicts": report.identity_conflicts,
        "mask_conflicts": report.mask_conflicts,
        "entries": [r.to_dict() for r in report.resolutions],
    }
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


#: 一覧を出す上限。これを超えたら件数だけにする。
MAX_ROWS = 20


def build_summary(report: MergeReport) -> str:
    """``lpdata_merge_summary.md``。**統合前に裁定を確認するための1枚もの。**"""
    lines = ["# lp-data データセットの統合結果", ""]

    if report.dry_run:
        lines += [
            "> ⚠️ **DRY RUN** —— データセットJSONは書いていない。",
            "> 以下は「書いた場合にこうなる」という内容。",
            "",
        ]

    lines += [
        "## 入力",
        "",
        "| 役割 | パス | サンプル数 |",
        "| --- | --- | ---: |",
        f"| primary（重複時に残す側） | `{report.primary_path}` "
        f"| {report.primary_samples:,} |",
        f"| secondary（ラベル補強に使う側） | `{report.secondary_path}` "
        f"| {report.secondary_samples:,} |",
        "",
        "## 件数",
        "",
        "| 指標 | 件数 |",
        "| --- | ---: |",
        f"| 重複した sample_id | {report.duplicates:,} |",
        f"| └ 補強で値が変わったもの | {report.enriched:,} |",
        f"| primary の明示的な気胸陰性（昇格から保護） | {report.protected:,} |",
        f"| secondary から追加 | {report.added_from_secondary:,} |",
        f"| **統合後のサンプル数** | **{report.merged_samples:,}** |",
        "",
    ]

    lines += _changes_section(report)
    lines += _conflict_section(report)
    lines += _distribution_section(report)
    lines += _invariant_section(report)

    if report.artifacts:
        lines += ["## 付随ファイル", ""]
        for name, path in sorted(report.artifacts.items()):
            lines.append(f"- {name}: `{path}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def _changes_section(report: MergeReport) -> list[str]:
    lines = ["## 補強した値", ""]
    if not report.change_counts:
        lines += ["変更なし。", ""]
    else:
        lines += ["| 変更 | 件数 |", "| --- | ---: |"]
        for key in sorted(report.change_counts):
            lines.append(f"| `{key}` | {report.change_counts[key]:,} |")
        lines.append("")
    lines += [
        "### 変更しなかったもの（方針）",
        "",
        "- `abnormal_finding_status` は統合で**書き換えない**。"
        "secondary（読影レポート由来）は policy 未確定で常に `unknown` を返すので、"
        "写すと annotation 由来の判定を潰す",
        "- `image_file` / マスク3種 / 面積 / `lung_rect` は**必ず primary を採る**。"
        "人手で整備したGTを差し替えないため",
        "- 確信度（`definite` / `probable` / `possible` / `unlikely`）は"
        "**裁定に一切使っていない**",
        "",
    ]
    return lines


def _conflict_section(report: MergeReport) -> list[str]:
    lines: list[str] = []
    if report.conflict_counts:
        lines += [
            "## primary を優先した食い違い",
            "",
            "**明示的な陰性/陽性GT が読影レポートより優先される。**"
            "自動では直さないので、必要なら個別に確認する。",
            "",
            "| 種類 | 件数 |",
            "| --- | ---: |",
        ]
        for key in sorted(report.conflict_counts):
            lines.append(f"| `{key}` | {report.conflict_counts[key]:,} |")
        lines.append("")
        shown = [r for r in report.resolutions if r.conflicts][:MAX_ROWS]
        for resolution in shown:
            for conflict in resolution.conflicts:
                lines.append(f"- {resolution.sample_id}: {conflict}")
        lines.append("")

    if report.mask_conflicts:
        lines += [
            f"## 気胸マスクの食い違い（{len(report.mask_conflicts)} 件）",
            "",
            "マスクのファイル名は `<sample_id>.png` で置き場が共有なので、"
            "**secondary 側のマスク出力先を primary と分けること**。"
            "統合では primary のマスクを採っている。",
            "",
        ]
        for line in report.mask_conflicts[:MAX_ROWS]:
            lines.append(f"- {line}")
        if len(report.mask_conflicts) > MAX_ROWS:
            lines.append(f"- …ほか {len(report.mask_conflicts) - MAX_ROWS} 件")
        lines.append("")

    if report.identity_conflicts:
        lines += [
            f"## ⚠️ 同一性の食い違い（{len(report.identity_conflicts)} 件）",
            "",
            "同じ `sample_id` なのに `patient_id` や `dicom_file` が違う。"
            "**別の画像を同一視している疑いがある。**",
            "",
        ]
        for line in report.identity_conflicts[:MAX_ROWS]:
            lines.append(f"- {line}")
        if len(report.identity_conflicts) > MAX_ROWS:
            lines.append(f"- …ほか {len(report.identity_conflicts) - MAX_ROWS} 件")
        lines.append("")
    return lines


def _distribution_section(report: MergeReport) -> list[str]:
    lines = [
        "## ラベル分布",
        "",
        "`pneumothorax_case`（統合の前後）:",
        "",
        "| 値 | primary のみ | 統合後 |",
        "| --- | ---: | ---: |",
    ]
    keys = sorted(set(report.case_counts_before) | set(report.case_counts_after))
    for key in keys:
        before = report.case_counts_before.get(key, 0)
        after = report.case_counts_after.get(key, 0)
        lines.append(f"| {key} | {before:,} | {after:,} |")
    lines += [
        "",
        "> `true(マスク無し)` は「気胸症例だがマスク未アノテーション」。"
        "テンプレートが正当な組み合わせと定めており、**マスクの有無で気胸の"
        "有無を判断しない**。",
        "",
    ]

    if report.status_counts_after:
        lines += [
            "`abnormal_finding_status`（統合後）:",
            "",
            "| 値 | 件数 |",
            "| --- | ---: |",
        ]
        for key in sorted(report.status_counts_after):
            lines.append(f"| {key} | {report.status_counts_after[key]:,} |")
        lines.append("")

    if report.side_counts_after:
        lines += [
            "`pneumothorax_side`（統合後）:",
            "",
            "| 値 | 件数 |",
            "| --- | ---: |",
        ]
        for key in sorted(report.side_counts_after):
            lines.append(f"| {key} | {report.side_counts_after[key]:,} |")
        lines.append("")
    return lines


def _invariant_section(report: MergeReport) -> list[str]:
    lines = ["## 不変条件", ""]
    if not report.violations:
        return lines + ["違反なし。", ""]
    lines += [f"**{len(report.violations)} 件の違反**。", ""]
    for violation in report.violations[:MAX_ROWS]:
        lines.append(f"- {violation}")
    if len(report.violations) > MAX_ROWS:
        lines.append(f"- …ほか {len(report.violations) - MAX_ROWS} 件")
    return lines + [""]


def _write_summary(options: MergeOptions, report: MergeReport) -> Path:
    path = options.out_path.parent / "lpdata_merge_summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(build_summary(report), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
