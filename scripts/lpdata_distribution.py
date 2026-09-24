"""lp-data データセットのラベル分布を集計し、図とCSVを書き出す。

使い方:

    uv run python scripts/lpdata_distribution.py \
        output/lpdata/20260917/chest_metry_pi6_pneumothorax.json

出力先は ``output/research/`` 配下（``--out-dir`` で変更可）。図は matplotlib に
日本語フォントが無いため英語ラベルで描く。日本語の解釈は同時に出す Markdown に置く。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

# dataviz スキルの reference palette（検証済みの既定値）から使う分だけ
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#52514e"
GRID = "#e6e5e1"
POSITIVE = "#2a78d6"  # categorical slot 1 (blue)
NEGATIVE = "#eb6834"  # categorical slot 2 (orange)
NEUTRAL = "#c9c8c3"  # unknown / null は系列色ではなくグレー
SEQ = ["#9ec5f4", "#5598e7", "#2a78d6", "#184f95"]  # 単一色相ランプ

MASK_KEYS = ("pneumothorax_mask", "lung_mask", "thorax_mask")


def has_mask(sample: dict, key: str) -> bool:
    mask = sample.get(key)
    return bool(mask and mask.get("pixel_array"))


def thousands(value: float, _pos: int = 0) -> str:
    return f"{int(value):,}"


def style_axes(ax, xlabel: str | None = None) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9, length=0)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=9)


def new_figure(width: float, height: float, title: str, subtitle: str = "", left: float = 0.22):
    fig, ax = plt.subplots(figsize=(width, height), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    fig.subplots_adjust(top=0.78, left=left, right=0.98, bottom=0.22)
    fig.text(0.012, 0.955, title, ha="left", va="top", color=INK, fontsize=13, fontweight="bold")
    if subtitle:
        fig.text(0.012, 0.87, subtitle, ha="left", va="top", color=INK_MUTED, fontsize=9)
    style_axes(ax)
    return fig, ax


def save(fig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path}")


def stacked_rows(
    ax,
    rows,
    colors,
    labels,
    total_hint: float | None = None,
    value_format=lambda v: f"{v:,}",
    callouts: bool = True,
) -> None:
    """rows: [(行ラベル, [各セグメントの件数])] を横積み棒で描く。"""
    y = range(len(rows))
    scale = total_hint or max(sum(row[1]) for row in rows)
    lefts = [0.0] * len(rows)
    small_labels: dict[int, list[str]] = defaultdict(list)
    for seg_index, (color, label) in enumerate(zip(colors, labels)):
        values = [row[1][seg_index] for row in rows]
        ax.barh(
            list(y),
            values,
            left=lefts,
            height=0.56,
            color=color,
            label=label,
            edgecolor=SURFACE,
            linewidth=2,
        )
        for i, value in enumerate(values):
            if value <= 0:
                continue
            if value / scale > 0.07:
                ax.text(
                    lefts[i] + value / 2,
                    i,
                    value_format(value),
                    ha="center",
                    va="center",
                    color="#ffffff" if color != NEUTRAL else INK,
                    fontsize=9,
                    fontweight="bold",
                )
            elif callouts:
                # 細すぎて中に数字を置けない区間は、棒の右側に「ラベル: 件数」で添える
                small_labels[i].append(f"{label.split(' /')[0]} {value_format(value)}")
        lefts = [left + value for left, value in zip(lefts, values)]
    for i, parts in small_labels.items():
        ax.text(
            scale * 1.02,
            i,
            "  ·  ".join(parts),
            ha="left",
            va="center",
            color=INK_MUTED,
            fontsize=8.5,
        )
    ax.set_yticks(list(y), [row[0] for row in rows], color=INK, fontsize=10)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(FuncFormatter(thousands))


@dataclass
class Counts:
    rows: list[tuple[str, str, str, int, str]]

    def add(self, chart: str, field: str, value: str, count: int, note: str = "") -> None:
        self.rows.append((chart, field, value, count, note))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="lp-data 形式の dataset JSON")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("output/research"),
        help="出力先ディレクトリ（既定: output/research）",
    )
    parser.add_argument("--tag", default=None, help="出力名に付ける識別子（既定: JSONの日付ディレクトリ名）")
    args = parser.parse_args()

    tag = args.tag or args.dataset.parent.name
    fig_dir = args.out_dir / "figures" / f"lpdata_{tag}"
    data_dir = args.out_dir / "data"

    dataset = json.loads(args.dataset.read_text())
    samples: dict[str, dict] = dataset["samples"]
    total = len(samples)
    counts = Counts(rows=[])

    # ---- 集計 -------------------------------------------------------------
    ptx_true = sum(1 for s in samples.values() if s["pneumothorax_case"])
    ptx_mask = sum(1 for s in samples.values() if has_mask(s, "pneumothorax_mask"))
    ptx_true_no_mask = sum(
        1 for s in samples.values() if s["pneumothorax_case"] and not has_mask(s, "pneumothorax_mask")
    )
    ptx_false_with_mask = sum(
        1 for s in samples.values() if not s["pneumothorax_case"] and has_mask(s, "pneumothorax_mask")
    )
    bulla = Counter(s["bulla_bleb_status"] for s in samples.values())
    abnormal = Counter(s["abnormal_finding_status"] for s in samples.values())
    side = Counter(s["pneumothorax_side"] for s in samples.values())
    view = Counter(s.get("view_position") for s in samples.values())
    vendor = Counter(s.get("vendor_id") for s in samples.values())

    patients = {(s["facility_id"], s["patient_id"]) for s in samples.values()}
    ptx_patients = {
        (s["facility_id"], s["patient_id"]) for s in samples.values() if s["pneumothorax_case"]
    }

    # 図1: 主要ラベルの内訳
    label_rows = [
        ("Pneumothorax case", [ptx_true, total - ptx_true, 0]),
        ("Pneumothorax mask", [ptx_mask, 0, total - ptx_mask]),
        ("Abnormal finding", [abnormal["present"], abnormal["absent"], abnormal["unknown"]]),
        ("Bulla / bleb", [bulla["present"], bulla["absent"], bulla["unknown"]]),
    ]
    for name, (pos, neg, unk) in label_rows:
        counts.add("01_label_counts", name, "present/true", pos)
        counts.add("01_label_counts", name, "absent/false", neg)
        counts.add("01_label_counts", name, "unknown/null", unk)

    fig, ax = new_figure(
        9.2,
        4.0,
        "Images per label",
        f"chest_metry_pi6_pneumothorax ({tag}) — {total:,} images, "
        f"{len(patients):,} patients, {len({s['facility_id'] for s in samples.values()})} facilities",
    )
    stacked_rows(
        ax,
        label_rows,
        [POSITIVE, NEGATIVE, NEUTRAL],
        ["present / true", "absent / false", "unknown / not annotated"],
        total_hint=total,
    )
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.1),
        ncol=3,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    ax.set_xlim(0, total * 1.28)
    save(fig, fig_dir / "01_label_counts.png")

    # 図2: 気胸ラベル × マスクの有無
    mask_rows = [
        ("Pneumothorax case = true", [ptx_mask, ptx_true_no_mask]),
        ("Pneumothorax case = false", [ptx_false_with_mask, total - ptx_true - ptx_false_with_mask]),
    ]
    for name, (with_mask, without) in mask_rows:
        counts.add("02_case_vs_mask", name, "mask present", with_mask)
        counts.add("02_case_vs_mask", name, "mask absent", without)

    fig, ax = new_figure(
        9.2,
        3.0,
        "Pneumothorax label vs. segmentation mask",
        f"{ptx_true_no_mask:,} of {ptx_true:,} pneumothorax images have no mask "
        f"({ptx_true_no_mask / ptx_true:.0%} not usable for segmentation training)",
    )
    stacked_rows(
        ax,
        mask_rows,
        [POSITIVE, NEUTRAL],
        ["mask present", "mask absent"],
        total_hint=total,
    )
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.14),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    ax.set_xlim(0, total * 1.28)
    save(fig, fig_dir / "02_case_vs_mask.png")

    # 図3: 気胸の側
    side_rows = [
        ("right", side["right"]),
        ("left", side["left"]),
        ("bilateral", side["bilateral"]),
        ("unknown (null)", ptx_true - side["right"] - side["left"] - side["bilateral"]),
    ]
    for name, value in side_rows:
        counts.add("03_side", "pneumothorax_side", name, value, "denominator = pneumothorax_case true")

    fig, ax = new_figure(
        8.4,
        3.2,
        "Pneumothorax side",
        f"within {ptx_true:,} pneumothorax images",
    )
    colors = [SEQ[2], SEQ[1], SEQ[0], NEUTRAL]
    bars = ax.barh(
        [row[0] for row in side_rows],
        [row[1] for row in side_rows],
        height=0.56,
        color=colors,
        edgecolor=SURFACE,
        linewidth=2,
    )
    for bar, (_, value) in zip(bars, side_rows):
        ax.text(
            bar.get_width() + ptx_true * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{value:,}  ({value / ptx_true:.0%})",
            va="center",
            color=INK,
            fontsize=9,
        )
    ax.invert_yaxis()
    ax.set_xlim(0, ptx_true * 1.15)
    ax.xaxis.set_major_formatter(FuncFormatter(thousands))
    save(fig, fig_dir / "03_side.png")

    # 図4: マスク整備状況
    mask_cover = [(key, sum(1 for s in samples.values() if has_mask(s, key))) for key in MASK_KEYS]
    cover_rows = [
        (key.replace("_", " "), [present, total - present]) for key, present in mask_cover
    ]
    for key, present in mask_cover:
        counts.add("04_mask_coverage", key, "present", present)
        counts.add("04_mask_coverage", key, "missing", total - present)

    fig, ax = new_figure(
        9.2,
        3.4,
        "Mask coverage",
        f"out of {total:,} images",
    )
    stacked_rows(ax, cover_rows, [POSITIVE, NEUTRAL], ["present", "missing"], total_hint=total)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    ax.set_xlim(0, total * 1.28)
    save(fig, fig_dir / "04_mask_coverage.png")

    # 図5: 気胸面積の分布
    areas = sorted(
        s["pneumothorax_area_mm2"] for s in samples.values() if s.get("pneumothorax_area_mm2")
    )
    import math

    bins = [10 ** x for x in [i / 4 for i in range(0, 4 * 5 + 1)]]
    hist = Counter()
    for area in areas:
        index = min(int(math.log10(area) * 4), len(bins) - 2)
        index = max(index, 0)
        hist[index] += 1
    for index, count in sorted(hist.items()):
        counts.add(
            "05_area",
            "pneumothorax_area_mm2",
            f"[{bins[index]:.0f}, {bins[index + 1]:.0f}) mm2",
            count,
        )

    median = areas[len(areas) // 2]
    fig, ax = new_figure(
        9.2,
        3.6,
        "Pneumothorax mask area",
        f"{len(areas):,} masked images — median {median:,.0f} mm², "
        f"min {areas[0]:.1f} mm², max {areas[-1]:,.0f} mm²",
        left=0.10,
    )
    ax.hist(areas, bins=bins, color=POSITIVE, edgecolor=SURFACE, linewidth=1)
    ax.set_xscale("log")
    ax.axvline(median, color=NEGATIVE, linewidth=2)
    ax.text(median * 1.1, ax.get_ylim()[1] * 0.9, f"median {median:,.0f} mm²", color=NEGATIVE, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_xlabel("area [mm²], log scale", color=INK_MUTED, fontsize=9)
    ax.set_ylabel("images", color=INK_MUTED, fontsize=9)
    save(fig, fig_dir / "05_area.png")

    # 図6: 施設別
    per_facility: dict[str, Counter] = defaultdict(Counter)
    for sample in samples.values():
        bucket = per_facility[sample["facility_id"]]
        bucket["n"] += 1
        if sample["pneumothorax_case"]:
            bucket["ptx"] += 1
        if has_mask(sample, "pneumothorax_mask"):
            bucket["mask"] += 1
    ordered = sorted(per_facility.items(), key=lambda kv: -kv[1]["n"])
    top = ordered[:10]
    rest = ordered[10:]
    facility_rows = [
        (name, [bucket["mask"], bucket["ptx"] - bucket["mask"], bucket["n"] - bucket["ptx"]])
        for name, bucket in top
    ]
    if rest:
        facility_rows.append(
            (
                f"other ({len(rest)} facilities)",
                [
                    sum(b["mask"] for _, b in rest),
                    sum(b["ptx"] - b["mask"] for _, b in rest),
                    sum(b["n"] - b["ptx"] for _, b in rest),
                ],
            )
        )
    for name, bucket in ordered:
        counts.add("06_facility", name, "images", bucket["n"])
        counts.add("06_facility", name, "pneumothorax true", bucket["ptx"])
        counts.add("06_facility", name, "pneumothorax mask", bucket["mask"])

    # 6a: 施設ごとの枚数（桁が違うので構成比とは分けて描く）
    fig, ax = new_figure(
        9.6,
        5.0,
        "Images per facility",
        f"top {len(top)} facilities of {len(ordered)}",
        left=0.26,
    )
    totals = [sum(row[1]) for row in facility_rows]
    bars = ax.barh(
        [row[0] for row in facility_rows],
        totals,
        height=0.56,
        color=POSITIVE,
        edgecolor=SURFACE,
        linewidth=2,
    )
    for bar, value in zip(bars, totals):
        ax.text(
            bar.get_width() + max(totals) * 0.012,
            bar.get_y() + bar.get_height() / 2,
            f"{value:,}  ({value / total:.1%})",
            va="center",
            color=INK,
            fontsize=9,
        )
    ax.invert_yaxis()
    ax.set_xlim(0, max(totals) * 1.25)
    ax.xaxis.set_major_formatter(FuncFormatter(thousands))
    save(fig, fig_dir / "06_facility_counts.png")

    # 6b: 施設ごとの構成比
    fig, ax = new_figure(
        9.6,
        5.0,
        "Facility composition",
        "share of each facility's images — pneumothorax with mask / without mask / non-pneumothorax",
        left=0.26,
    )
    share_rows = [
        (name, [value / sum(segments) * 100 for value in segments])
        for name, segments in facility_rows
    ]
    stacked_rows(
        ax,
        share_rows,
        [POSITIVE, SEQ[0], NEUTRAL],
        ["pneumothorax + mask", "pneumothorax, no mask", "non-pneumothorax"],
        total_hint=100,
        value_format=lambda v: f"{v:.0f}%",
        callouts=False,
    )
    ax.set_xlim(0, 100)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{int(v)}%"))
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.07),
        ncol=3,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    save(fig, fig_dir / "06b_facility_composition.png")

    # 図7: 撮影メタデータの欠損
    spacing_missing = sum(1 for s in samples.values() if not s.get("pixel_spacing"))
    meta_rows = [
        ("view_position", [total - view[None], view[None]]),
        ("vendor_id", [total - vendor[None], vendor[None]]),
        ("pixel_spacing", [total - spacing_missing, spacing_missing]),
    ]
    for name, (known, missing) in meta_rows:
        counts.add("07_metadata", name, "present", known)
        counts.add("07_metadata", name, "missing (null)", missing)

    fig, ax = new_figure(
        9.2,
        3.2,
        "Acquisition metadata availability",
        f"out of {total:,} images",
    )
    stacked_rows(ax, meta_rows, [POSITIVE, NEUTRAL], ["present", "missing (null)"], total_hint=total)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.13),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    ax.set_xlim(0, total * 1.28)
    save(fig, fig_dir / "07_metadata.png")

    # 図8: 異常所見 × 気胸
    cross = Counter(
        (s["abnormal_finding_status"], bool(s["pneumothorax_case"])) for s in samples.values()
    )
    cross_rows = [
        (f"abnormal finding: {status}", [cross[(status, True)], cross[(status, False)]])
        for status in ("present", "absent", "unknown")
    ]
    for status in ("present", "absent", "unknown"):
        counts.add("08_finding_vs_case", status, "pneumothorax true", cross[(status, True)])
        counts.add("08_finding_vs_case", status, "pneumothorax false", cross[(status, False)])

    fig, ax = new_figure(
        9.2,
        3.4,
        "Abnormal finding vs. pneumothorax label",
        "the two labels come from different sources (radiology report / annotation)",
    )
    stacked_rows(
        ax,
        cross_rows,
        [POSITIVE, NEUTRAL],
        ["pneumothorax = true", "pneumothorax = false"],
        total_hint=total,
    )
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_MUTED,
    )
    ax.set_xlim(0, total * 1.28)
    save(fig, fig_dir / "08_finding_vs_case.png")

    # 図9: image_shape × pixel_spacing から計算した画像の物理幅
    widths = [
        (s["facility_id"], s["image_shape"]["width"] * s["pixel_spacing"]["x"])
        for s in samples.values()
        if s.get("pixel_spacing") and s.get("image_shape")
    ]
    buckets = [(0, 200), (200, 250), (250, 300), (300, 400), (400, 10_000)]
    for low, high in buckets:
        count = sum(1 for _, width in widths if low <= width < high)
        counts.add(
            "09_physical_width",
            "image_shape.width * pixel_spacing.x",
            f"[{low}, {high}) mm",
            count,
            "chest radiographs are physically ~350 mm wide",
        )

    suspect = sum(1 for _, width in widths if width < 250)
    fig, ax = new_figure(
        9.2,
        3.6,
        "Physical image width (width × pixel_spacing.x)",
        f"{suspect:,} of {len(widths):,} images compute to < 250 mm — a chest radiograph is ~350 mm wide, "
        "so their pixel_spacing looks un-rescaled",
        left=0.10,
    )
    ax.hist(
        [width for _, width in widths],
        bins=[x * 10 for x in range(0, 51)],
        color=POSITIVE,
        edgecolor=SURFACE,
        linewidth=0.5,
    )
    ax.axvline(250, color=NEGATIVE, linewidth=2)
    ax.text(255, ax.get_ylim()[1] * 0.88, "250 mm", color=NEGATIVE, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_xlabel("computed physical width [mm]", color=INK_MUTED, fontsize=9)
    ax.set_ylabel("images", color=INK_MUTED, fontsize=9)
    save(fig, fig_dir / "09_physical_width.png")

    # ---- CSV --------------------------------------------------------------
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / f"lpdata-distribution_{tag}.csv"
    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["chart", "field", "value", "images", "note"])
        writer.writerow(["00_overview", "samples", "total", total, ""])
        writer.writerow(["00_overview", "patients", "unique (facility, patient)", len(patients), ""])
        writer.writerow(["00_overview", "patients", "with pneumothorax", len(ptx_patients), ""])
        writer.writerows(counts.rows)
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
