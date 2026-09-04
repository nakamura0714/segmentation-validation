"""二値マスクの形状計測と、2枚のマスクの関係。

すべて純粋な計算。ファイルは読まない（読むのは ``core.imageio`` と ``core.measure``）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import cv2
import numpy as np

# 連結成分は8近傍で数える。JSONの ``region_count`` と 1665/1665 一致することを確認済み。
CONNECTIVITY = 8
# 成分の面積はマスクあたり上位いくつまで保持するか。飛びカス検出には十分で、
# 極端な症例でキャッシュ行が膨らむのを防ぐ。
MAX_COMPONENT_AREAS = 32


@dataclass(frozen=True)
class MaskStats:
    """マスク1枚の形状量。"""

    pixels: int
    total_pixels: int
    bbox: tuple[int, int, int, int] | None
    n_components: int
    # 面積の降順。MAX_COMPONENT_AREAS で打ち切る。
    component_areas: tuple[int, ...] = field(default=())
    component_areas_truncated: bool = False
    touches_border: bool = False

    @property
    def fg_ratio(self) -> float:
        return self.pixels / self.total_pixels if self.total_pixels else 0.0

    @property
    def largest_component(self) -> int:
        return self.component_areas[0] if self.component_areas else 0

    @property
    def smallest_component(self) -> int:
        return self.component_areas[-1] if self.component_areas else 0


def mask_stats(mask: np.ndarray) -> MaskStats:
    """マスクの画素数・bbox・連結成分（数と面積）を求める。

    ``connectedComponentsWithStats`` を使い、成分数と成分ごとの面積を1回で得る。
    飛びカス検出（大きな本体に付いた数pxの成分）には成分ごとの面積が要る。
    """
    total = int(mask.size)
    pixels = int(np.count_nonzero(mask))
    if pixels == 0:
        return MaskStats(pixels=0, total_pixels=total, bbox=None, n_components=0)

    binary = mask.astype(np.uint8)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=CONNECTIVITY
    )
    # 行0は背景なので除く。
    areas = sorted(
        (int(stats[i, cv2.CC_STAT_AREA]) for i in range(1, n_labels)), reverse=True
    )

    ys, xs = np.nonzero(mask)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    height, width = mask.shape[:2]
    touches = bool(
        bbox[0] == 0 or bbox[1] == 0 or bbox[2] == width - 1 or bbox[3] == height - 1
    )

    return MaskStats(
        pixels=pixels,
        total_pixels=total,
        bbox=bbox,
        n_components=n_labels - 1,
        component_areas=tuple(areas[:MAX_COMPONENT_AREAS]),
        component_areas_truncated=len(areas) > MAX_COMPONENT_AREAS,
        touches_border=touches,
    )


def iou(a: np.ndarray, b: np.ndarray) -> float:
    """2つのマスクの IoU。空同士は 1.0 とする。"""
    union = int(np.count_nonzero(a | b))
    if union == 0:
        return 1.0
    return int(np.count_nonzero(a & b)) / union


def containment(inner: np.ndarray, outer: np.ndarray) -> float:
    """``inner`` のうち ``outer`` に含まれる割合。

    小さいマスクが大きいマスクに埋没しているケースは IoU が低く出るので、
    重複判定には IoU と包含率の両方が要る。
    """
    total = int(np.count_nonzero(inner))
    if total == 0:
        return 1.0
    return int(np.count_nonzero(inner & outer)) / total


def mask_hash(mask: np.ndarray) -> str:
    """マスクの内容ハッシュ。完全一致判定の事前絞り込みに使う。

    形状もハッシュに含めるので、サイズ違いが偶然衝突することはない。
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(mask.shape).encode("ascii"))
    digest.update(np.packbits(mask.astype(bool), axis=None).tobytes())
    return digest.hexdigest()


def pixels_to_mm2(
    pixels: int, spacing_x: float | None, spacing_y: float | None
) -> float | None:
    """画素数を mm² へ換算する。spacing が無ければ None。

    spacing は施設・機種で 0.0875〜0.2 と5.2倍振れるので、面積の比較は必ず mm² で行う。
    px のままだと同じ画素数が施設によって5倍違う面積を指すことになる。
    """
    if spacing_x is None or spacing_y is None:
        return None
    return pixels * float(spacing_x) * float(spacing_y)


def crop_box(
    masks: list[np.ndarray], image_shape: tuple[int, int], margin: int = 120
) -> tuple[int, int, int, int]:
    """全マスクを含む拡大表示用の範囲 (x0, y0, x1, y1) を返す。"""
    height, width = image_shape
    boxes = [stats.bbox for stats in (mask_stats(m) for m in masks) if stats.bbox]
    if not boxes:
        return 0, 0, width - 1, height - 1
    x0 = max(min(b[0] for b in boxes) - margin, 0)
    y0 = max(min(b[1] for b in boxes) - margin, 0)
    x1 = min(max(b[2] for b in boxes) + margin, width - 1)
    y1 = min(max(b[3] for b in boxes) + margin, height - 1)
    return x0, y0, x1, y1
