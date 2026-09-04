"""1症例分のDICOMとマスクを重畳表示して目視確認するためのスクリプト。

同一領域に対する重複アノテーションの有無を確認する目的で作成した。
データセットは読み取りのみで、出力はプロジェクト内にしか書き込まない。

使い方::

    uv run python -m segmentation_validation.overlay_masks
    uv run python -m segmentation_validation.overlay_masks --institution jsrt
    uv run python -m segmentation_validation.overlay_masks --study CXASW00000278_002
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import matplotlib

matplotlib.use("Agg")

import cv2
import matplotlib.pyplot as plt
import numpy as np
import pydicom
from matplotlib.patches import Rectangle
from PIL import Image
from pydicom.pixels import apply_voi_lut

logger = logging.getLogger(__name__)

try:  # 図中に code_text（日本語）を出すためのフォント
    import matplotlib_fontja  # noqa: F401

    JAPANESE_FONT = True
except ImportError:  # フォントが無い環境では英語名のみで描画を続ける
    JAPANESE_FONT = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON = Path(
    "/mnt/project/chest/metry/pi6/dataset/source/"
    "engineer-set-ETR_ChestMetry_PI6px_with_mask136-20260624_080014.json"
)
DEFAULT_OUT_DIR = PROJECT_ROOT / "output" / "overlay"

# JSON内の相対パスは種類ごとに基準ルートが異なる。
IMAGE_ROOT = Path("/mnt")
ANNOTATION_ROOT = Path("/mnt/medicaldb")

# 重畳表示用の色（マスクの並び順に割り当てる）。
MASK_COLORS = [
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#ff7f00",
    "#984ea3",
    "#a65628",
]


def resolve_image_path(raw: str) -> Path:
    """``image_path`` を絶対パスに解決する。"""
    return IMAGE_ROOT / raw.lstrip("/")


def resolve_annotation_path(raw: str) -> Path:
    """``path_mask`` / ``path_original_mask`` を絶対パスに解決する。

    一部のレコードは先頭スラッシュ付きで格納されており、そのまま
    ``Path / raw`` すると基準ルートが捨てられるため必ず正規化する。
    """
    return ANNOTATION_ROOT / raw.lstrip("/")


@dataclass(frozen=True)
class Label:
    """アノテーションに付いたラベル1件。

    同じ ``code`` でも ``code_system`` が異なれば別の病名を指し得るため、
    ``code`` 単独ではなく ``(code_system, code)`` の組で同一性を判断する。
    """

    code_system: str
    code: str
    code_text: str
    code_text_eng: str
    confidence: float | None

    @property
    def key(self) -> tuple[str, str]:
        """ラベルの同一性判断に使うキー。"""
        return (self.code_system, self.code)

    @property
    def qualified_code(self) -> str:
        """``Findings/010`` のような修飾付きコード表記。"""
        return f"{self.code_system}/{self.code}"

    def display(self, japanese: bool = True, with_confidence: bool = True) -> str:
        """図やログに出す1行表記。

        ``japanese=False`` の環境（CJKフォント無し）では英語名のみを使う。
        語彙一覧のように confidence が邪魔な場面では ``with_confidence=False``。
        """
        name = self.code_text_eng or "?"
        if japanese and self.code_text and self.code_text != self.code_text_eng:
            name = f"{self.code_text} ({name})"
        conf = ""
        if with_confidence and self.confidence is not None:
            conf = f" conf={self.confidence:g}"
        return f"{self.qualified_code} {name}{conf}"


def parse_labels(raw_labels: list[dict[str, Any]]) -> tuple[Label, ...]:
    """``labels`` を重複排除しつつ ``Label`` に変換する。

    ``code_text_eng`` だけで重複排除すると code_system や
    code_text の違いを潰してしまうため、全項目一致のみ重複扱いにする。
    """
    labels = [
        Label(
            code_system=lb["code_system"],
            code=str(lb["code"]),
            code_text=lb.get("code_text") or "",
            code_text_eng=lb.get("code_text_eng") or "",
            confidence=lb.get("confidence"),
        )
        for lb in raw_labels
    ]
    return tuple(dict.fromkeys(labels))


@dataclass(frozen=True)
class MaskEntry:
    """1件のアノテーション（brushマスク）を表す。"""

    index: int
    geometry_uid: str
    geometry_id: int
    path_mask: Path
    path_original_mask: Path | None
    json_bbox: tuple[int, int, int, int]
    is_latest: int
    version: int
    user: str
    timestamp: str
    labels: tuple[Label, ...]

    @property
    def short_uid(self) -> str:
        return self.geometry_uid[:8]

    @property
    def label_keys(self) -> frozenset[tuple[str, str]]:
        """``(code_system, code)`` の集合。ラベル一致判定に使う。"""
        return frozenset(lb.key for lb in self.labels)

    def label_lines(self, japanese: bool = True) -> list[str]:
        """ラベルを1件1行の表記で返す。"""
        return [lb.display(japanese) for lb in self.labels]


@dataclass(frozen=True)
class CaseTarget:
    """描画対象となる1ファイル分の情報。"""

    institution: str
    study_key: str
    patient_id: str
    study_date: str
    series_key: str
    file_key: str
    image_path: Path
    shape: dict[str, Any]
    spacing: dict[str, Any]
    manufacturer: str
    masks: tuple[MaskEntry, ...]


def iter_files(
    dataset: dict[str, Any],
) -> Iterator[tuple[str, str, str, str, dict, dict]]:
    """``dataset`` を走査して (施設, study, series, file, series辞書, file辞書) を返す。

    データセット全体をメモリに展開しないよう、必要な単位で逐次取り出す。
    """
    for institution, studies in dataset.items():
        for study_key, study in studies.items():
            for series_key, series in study["series_list"].items():
                for file_key, file_rec in series["file_list"].items():
                    yield institution, study_key, series_key, file_key, series, file_rec


def build_target(
    dataset: dict[str, Any],
    institution: str | None,
    study_key: str | None,
) -> CaseTarget:
    """指定された症例（未指定なら先頭）の描画対象を組み立てる。"""
    for inst, s_key, series_key, file_key, series, file_rec in iter_files(dataset):
        if institution is not None and inst != institution:
            continue
        if study_key is not None and s_key != study_key:
            continue

        study = dataset[inst][s_key]
        masks = tuple(
            MaskEntry(
                index=i,
                geometry_uid=ann["geometry_uid"],
                geometry_id=ann["geometry_id"],
                path_mask=resolve_annotation_path(ann["path_mask"]),
                path_original_mask=(
                    resolve_annotation_path(ann["path_original_mask"])
                    if ann.get("path_original_mask")
                    else None
                ),
                json_bbox=(ann["min_x"], ann["min_y"], ann["max_x"], ann["max_y"]),
                is_latest=ann["is_latest"],
                version=ann["version"],
                user=ann["user"],
                timestamp=ann["timestamp"],
                labels=parse_labels(ann["labels"]),
            )
            for i, ann in enumerate(file_rec["annotations"])
        )
        return CaseTarget(
            institution=inst,
            study_key=s_key,
            patient_id=study["patient_id"],
            study_date=str(study["study_date"]),
            series_key=series_key,
            file_key=file_key,
            image_path=resolve_image_path(file_rec["image_path"]),
            shape=series["shape"],
            spacing=series["spacing"],
            manufacturer=series["manufacturer"],
            masks=masks,
        )

    raise LookupError(
        f"対象が見つからない: institution={institution!r} study={study_key!r}"
    )


def load_dicom_image(path: Path) -> np.ndarray:
    """DICOMを表示用の float 配列 (0.0-1.0) として読み込む。

    VOI LUT と MONOCHROME1 の反転を適用する。元配列は破壊しない。
    """
    dcm = pydicom.dcmread(path)
    array = dcm.pixel_array

    try:
        array = apply_voi_lut(array, dcm)
    except Exception:  # LUTが壊れている症例があり得るので描画は続行する
        logger.warning("apply_voi_lut に失敗したため生画素を使用する: %s", path)

    array = array.astype(np.float32)
    if getattr(dcm, "PhotometricInterpretation", "") == "MONOCHROME1":
        array = array.max() - array

    lo, hi = np.percentile(array, [0.5, 99.5])
    if hi <= lo:
        lo, hi = float(array.min()), float(array.max())
    if hi <= lo:
        return np.zeros_like(array)
    return np.clip((array - lo) / (hi - lo), 0.0, 1.0)


def load_binary_mask(path: Path) -> np.ndarray:
    """マスクPNGを bool 配列として読み込む。

    ``path_mask`` は L モードだが ``path_original_mask`` は RGBA の場合があり、
    RGBA ではアルファチャンネルが描画領域を表す。
    """
    array = np.asarray(Image.open(path))
    if array.ndim == 3:
        channel = array[..., 3] if array.shape[2] in (2, 4) else array.max(axis=2)
    else:
        channel = array
    return channel > 0


def mask_stats(mask: np.ndarray) -> dict[str, Any]:
    """マスクの画素数・bbox・連結成分数を求める。"""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return {"pixels": 0, "bbox": None, "components": 0}
    n_labels, _ = cv2.connectedComponents(mask.astype(np.uint8))
    return {
        "pixels": int(mask.sum()),
        "bbox": (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())),
        "components": n_labels - 1,
    }


def iou(a: np.ndarray, b: np.ndarray) -> float:
    """2つのマスクの IoU。空同士は 1.0 とする。"""
    union = int((a | b).sum())
    if union == 0:
        return 1.0
    return int((a & b).sum()) / union


def draw_outline(
    ax: plt.Axes, mask: np.ndarray, color: str, linewidth: float = 1.2
) -> None:
    """マスクの輪郭を ax に描画する。"""
    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    for contour in contours:
        pts = contour.reshape(-1, 2)
        if len(pts) < 2:
            continue
        closed = np.vstack([pts, pts[:1]])
        ax.plot(closed[:, 0], closed[:, 1], color=color, linewidth=linewidth)


def crop_box(
    masks: list[np.ndarray], image_shape: tuple[int, int], margin: int = 120
) -> tuple[int, int, int, int]:
    """全マスクを含む拡大表示用の範囲 (x0, y0, x1, y1) を返す。"""
    height, width = image_shape
    boxes = [mask_stats(m)["bbox"] for m in masks]
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return 0, 0, width - 1, height - 1
    x0 = max(min(b[0] for b in boxes) - margin, 0)
    y0 = max(min(b[1] for b in boxes) - margin, 0)
    x1 = min(max(b[2] for b in boxes) + margin, width - 1)
    y1 = min(max(b[3] for b in boxes) + margin, height - 1)
    return x0, y0, x1, y1


def report_label_vocabulary(target: CaseTarget) -> None:
    """この症例に出現するラベル語彙を出力し、紛らわしい組み合わせを警告する。"""
    by_code: dict[str, set[str]] = {}
    by_key: dict[tuple[str, str], set[str]] = {}
    for entry in target.masks:
        for label in entry.labels:
            by_code.setdefault(label.code, set()).add(label.code_system)
            by_key.setdefault(label.key, set()).add(label.code_text)

    logger.info("--- ラベル語彙 ---")
    for key, texts in sorted(by_key.items()):
        logger.info("%s/%s : %s", key[0], key[1], " / ".join(sorted(texts)))

    for code, systems in sorted(by_code.items()):
        if len(systems) > 1:
            logger.warning(
                "code=%s が複数の code_system に跨る: %s"
                "（同じ code でも別の病名の可能性がある）",
                code,
                sorted(systems),
            )
    for key, texts in sorted(by_key.items()):
        if len(texts) > 1:
            logger.warning(
                "%s/%s に複数の code_text がある: %s",
                key[0],
                key[1],
                sorted(texts),
            )


def report_overlap(target: CaseTarget, masks: list[np.ndarray]) -> None:
    """マスク単体の統計とペアごとの重なりをログ出力する。"""
    logger.info("--- マスク単体の統計 ---")
    for entry, mask in zip(target.masks, masks):
        stats = mask_stats(mask)
        logger.info(
            "[%d] %s px=%d bbox=%s comps=%d is_latest=%d ver=%d %s labels=%s",
            entry.index,
            entry.short_uid,
            stats["pixels"],
            stats["bbox"],
            stats["components"],
            entry.is_latest,
            entry.version,
            entry.timestamp,
            " | ".join(entry.label_lines()),
        )
        if stats["bbox"] != tuple(entry.json_bbox):
            logger.warning(
                "[%d] %s JSONのbbox %s と実マスクのbbox %s が不一致",
                entry.index,
                entry.short_uid,
                tuple(entry.json_bbox),
                stats["bbox"],
            )

    report_label_vocabulary(target)

    if len(masks) < 2:
        return

    logger.info("--- ペアごとの重なり ---")
    for i, j in itertools.combinations(range(len(masks)), 2):
        value = iou(masks[i], masks[j])
        note = ""
        if value > 0.9:
            keys_i = target.masks[i].label_keys
            keys_j = target.masks[j].label_keys
            hit = "完全一致" if value == 1.0 else "ほぼ一致"
            if keys_i == keys_j:
                note = f"  <== {hit}・ラベルも同一（重複アノテーションの疑い）"
            else:
                note = (
                    f"  <== {hit}だがラベルが異なる"
                    f"（{sorted(keys_i)} vs {sorted(keys_j)}）"
                )
        logger.info(
            "IoU([%d] %s, [%d] %s) = %.5f%s",
            i,
            target.masks[i].short_uid,
            j,
            target.masks[j].short_uid,
            value,
            note,
        )


def render(
    target: CaseTarget, image: np.ndarray, masks: list[np.ndarray], out_path: Path
) -> None:
    """DICOMとマスクの重畳図を描いて保存する。"""
    n_masks = len(masks)
    n_cols = max(n_masks, 3)
    fig, axes = plt.subplots(2, n_cols, figsize=(4.2 * n_cols, 10.5))
    axes = np.atleast_2d(axes)

    for ax in axes.ravel():
        ax.axis("off")

    # 上段左: 元画像
    ax = axes[0, 0]
    ax.imshow(image, cmap="gray")
    ax.set_title(f"DICOM\n{target.file_key}", fontsize=9)
    ax.axis("on")
    ax.set_xticks([])
    ax.set_yticks([])

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
    ax.axis("on")
    ax.set_xticks([])
    ax.set_yticks([])

    # 上段右: 重畳の拡大表示
    x0, y0, x1, y1 = crop_box(masks, image.shape)
    ax = axes[0, 2]
    ax.imshow(image, cmap="gray")
    for i, mask in enumerate(masks):
        color = MASK_COLORS[i % len(MASK_COLORS)]
        draw_outline(ax, mask, color, linewidth=1.8)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y1, y0)
    ax.set_title("overlaid outlines (zoom)", fontsize=9)
    ax.axis("on")
    ax.set_xticks([])
    ax.set_yticks([])

    handles = [
        plt.Line2D(
            [],
            [],
            color=MASK_COLORS[i % len(MASK_COLORS)],
            linewidth=2.5,
            label=(
                f"[{i}] {e.short_uid} {int(m.sum())}px  "
                + ", ".join(lb.qualified_code for lb in e.labels)
            ),
        )
        for i, (e, m) in enumerate(zip(target.masks, masks))
    ]
    if handles:
        axes[0, 2].legend(
            handles=handles, fontsize=7, loc="lower right", framealpha=0.85
        )

    # 下段: マスク単体
    for i, (entry, mask) in enumerate(zip(target.masks, masks)):
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
        orig = "original: yes" if entry.path_original_mask else "original: none"
        label_text = "\n".join(entry.label_lines(JAPANESE_FONT)) or "(no label)"
        ax.set_title(
            f"[{i}] {entry.short_uid}\n"
            f"gid={entry.geometry_id} latest={entry.is_latest} v{entry.version}\n"
            f"{int(mask.sum())}px / {orig}\n"
            f"{label_text}",
            fontsize=8,
        )
        ax.axis("on")
        ax.set_xticks([])
        ax.set_yticks([])

    vocabulary = dict.fromkeys(
        lb.display(JAPANESE_FONT, with_confidence=False)
        for entry in target.masks
        for lb in entry.labels
    )
    spacing = target.spacing
    fig.suptitle(
        f"{target.institution} / {target.patient_id} / {target.study_key}"
        f"  ({target.study_date})\n"
        f"{target.series_key}  {target.shape['w']}x{target.shape['h']}  "
        f"spacing={spacing['x']}x{spacing['y']}mm  {target.manufacturer}"
        "   (yellow dashed = bbox from JSON)\n"
        "labels in this case:  " + "   |   ".join(vocabulary),
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    logger.info("保存した: %s", out_path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", type=Path, default=DEFAULT_JSON, help="データセットJSON"
    )
    parser.add_argument("--institution", help="施設ID（未指定なら先頭）")
    parser.add_argument("--study", help="study名（未指定なら先頭）")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="出力先")
    parser.add_argument(
        "--original",
        action="store_true",
        help="path_mask ではなく path_original_mask を描画する",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    dataset = json.loads(args.json.read_text())["dataset"]
    target = build_target(dataset, args.institution, args.study)

    logger.info(
        "対象: %s / %s / %s (%s)",
        target.institution,
        target.patient_id,
        target.study_key,
        target.study_date,
    )
    logger.info("DICOM: %s", target.image_path)
    if not target.image_path.exists():
        logger.error("DICOMが存在しない: %s", target.image_path)
        return 1

    image = load_dicom_image(target.image_path)

    masks: list[np.ndarray] = []
    for entry in target.masks:
        path = entry.path_original_mask if args.original else entry.path_mask
        if path is None:
            logger.warning(
                "[%d] %s path_original_mask が無い", entry.index, entry.short_uid
            )
            continue
        if not path.exists():
            logger.error(
                "[%d] %s マスクが存在しない: %s", entry.index, entry.short_uid, path
            )
            continue
        mask = load_binary_mask(path)
        if mask.shape != image.shape:
            logger.error(
                "[%d] %s 形状不一致 画像%s vs マスク%s のためスキップ",
                entry.index,
                entry.short_uid,
                image.shape,
                mask.shape,
            )
            continue
        logger.info("[%d] %s %s", entry.index, entry.short_uid, path)
        masks.append(mask)

    if not masks:
        logger.error("描画できるマスクが無い")
        return 1

    report_overlap(target, masks)

    suffix = "_original" if args.original else ""
    out_path = args.out_dir / f"{target.institution}_{target.file_key}{suffix}.png"
    render(target, image, masks, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
