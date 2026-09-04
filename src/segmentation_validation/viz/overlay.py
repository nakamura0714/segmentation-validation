"""DICOMとマスクの重畳図。

呼び出し側は ``(entry, mask)`` の組を渡す。マスクを個別にスキップし得るので、
配列とメタデータを別々のリストで持つと対応がずれる。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Protocol, Sequence

import matplotlib

matplotlib.use("Agg")

import cv2  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from ..core.geometry import crop_box  # noqa: E402
from ..core.labels import Label  # noqa: E402

logger = logging.getLogger(__name__)

try:  # 図中に code_text（日本語）を出すためのフォント
    import matplotlib_fontja  # noqa: F401

    JAPANESE_FONT = True
except ImportError:  # フォントが無い環境では英語名のみで描画を続ける
    JAPANESE_FONT = False

# 重畳表示用の色（マスクの並び順に割り当てる）。
MASK_COLORS = [
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#ff7f00",
    "#984ea3",
    "#a65628",
]

# 下段に並べるマスク単体パネルの上限。最大14マスクの症例があり、
# 全部並べると図が横60インチになるうえ MASK_COLORS が一周して色が重複する。
MAX_DETAIL_PANELS = 6


class MaskLike(Protocol):
    """描画に必要な最小限のメタデータ。"""

    index: int
    geometry_id: int
    is_latest: int
    version: int
    json_bbox: tuple[int, int, int, int]
    labels: tuple[Label, ...]

    @property
    def short_uid(self) -> str: ...

    @property
    def has_original_mask(self) -> bool: ...


def draw_outline(
    ax: plt.Axes, mask: np.ndarray, color: str, linewidth: float = 1.2
) -> None:
    """マスクの輪郭を ax に描画する。"""
    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    for contour in contours:
        points = contour.reshape(-1, 2)
        if len(points) < 2:
            continue
        closed = np.vstack([points, points[:1]])
        ax.plot(closed[:, 0], closed[:, 1], color=color, linewidth=linewidth)


def render(
    header: dict[str, Any],
    image: np.ndarray,
    pairs: Sequence[tuple[MaskLike, np.ndarray]],
    out_path: Path,
) -> None:
    """DICOMとマスクの重畳図を描いて保存する。

    ``pairs`` は ``(メタデータ, マスク配列)`` の組。組で受けることで
    「スキップされたマスクがあると以降の対応がずれる」問題が構造的に起きなくなる。
    """
    masks = [mask for _, mask in pairs]
    n_masks = len(masks)
    n_detail = min(n_masks, MAX_DETAIL_PANELS)
    n_cols = max(n_detail, 3)
    fig, axes = plt.subplots(2, n_cols, figsize=(4.2 * n_cols, 10.5))
    axes = np.atleast_2d(axes)

    for ax in axes.ravel():
        ax.axis("off")

    # 上段左: 元画像
    ax = axes[0, 0]
    ax.imshow(image, cmap="gray")
    ax.set_title(f"DICOM\n{header['file_key']}", fontsize=9)
    _bare_axis(ax)

    # 上段中央: 全マスク重畳（全体像）
    ax = axes[0, 1]
    ax.imshow(image, cmap="gray")
    for i, mask in enumerate(masks):
        color = MASK_COLORS[i % len(MASK_COLORS)]
        overlay = np.zeros((*mask.shape, 4), dtype=np.float32)
        overlay[mask] = (*matplotlib.colors.to_rgb(color), 0.28)
        ax.imshow(overlay)
        draw_outline(ax, mask, color)
    ax.set_title(f"all masks overlaid (n={n_masks})", fontsize=9)
    _bare_axis(ax)

    # 上段右: 重畳の拡大表示
    x0, y0, x1, y1 = crop_box(masks, image.shape) if masks else (0, 0, 1, 1)
    ax = axes[0, 2]
    ax.imshow(image, cmap="gray")
    for i, mask in enumerate(masks):
        color = MASK_COLORS[i % len(MASK_COLORS)]
        draw_outline(ax, mask, color, linewidth=1.8)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)
    ax.set_title("overlaid outlines (zoom)", fontsize=9)
    _bare_axis(ax)

    handles = [
        plt.Line2D(
            [],
            [],
            color=MASK_COLORS[i % len(MASK_COLORS)],
            linewidth=2.5,
            label=(
                f"[{i}] {entry.short_uid} {int(mask.sum())}px  "
                + ", ".join(label.qualified_code for label in entry.labels)
            ),
        )
        for i, (entry, mask) in enumerate(pairs)
    ]
    if handles:
        axes[0, 2].legend(
            handles=handles, fontsize=7, loc="lower right", framealpha=0.85
        )

    # 下段: マスク単体
    for i, (entry, mask) in enumerate(pairs[:n_detail]):
        ax = axes[1, i]
        color = MASK_COLORS[i % len(MASK_COLORS)]
        ax.imshow(image, cmap="gray")
        overlay = np.zeros((*mask.shape, 4), dtype=np.float32)
        overlay[mask] = (*matplotlib.colors.to_rgb(color), 0.45)
        ax.imshow(overlay)
        bx0, by0, bx1, by1 = entry.json_bbox
        ax.add_patch(
            Rectangle(
                (bx0, by0),
                bx1 - bx0,
                by1 - by0,
                fill=False,
                edgecolor="yellow",
                linestyle="--",
                linewidth=1.0,
            )
        )
        ax.set_xlim(x0, x1)
        ax.set_ylim(y1, y0)
        original = "original: yes" if entry.has_original_mask else "original: none"
        label_text = (
            "\n".join(label.display(JAPANESE_FONT) for label in entry.labels)
            or "(no label)"
        )
        ax.set_title(
            f"[{i}] {entry.short_uid}\n"
            f"gid={entry.geometry_id} latest={entry.is_latest} v{entry.version}\n"
            f"{int(mask.sum())}px / {original}\n"
            f"{label_text}",
            fontsize=8,
        )
        _bare_axis(ax)

    vocabulary = dict.fromkeys(
        label.display(JAPANESE_FONT, with_confidence=False)
        for entry, _ in pairs
        for label in entry.labels
    )
    omitted = ""
    if n_masks > n_detail:
        omitted = f"   (+{n_masks - n_detail} more masks not shown below)"
    fig.suptitle(
        f"{header['institution']} / {header['patient_id']} / {header['study_key']}"
        f"  ({header['study_date']})\n"
        f"{header['series_key']}  {header['shape']}  "
        f"spacing={header['spacing']}mm  {header['manufacturer']}"
        f"   (yellow dashed = bbox from JSON){omitted}\n"
        "labels in this case:  " + "   |   ".join(vocabulary),
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    logger.info("保存した: %s", out_path)


def _bare_axis(ax: plt.Axes) -> None:
    ax.axis("on")
    ax.set_xticks([])
    ax.set_yticks([])
