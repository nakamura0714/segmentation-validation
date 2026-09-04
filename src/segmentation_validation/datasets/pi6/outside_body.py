"""PI6 の体外領域判定。**このデータセット固有のロジック**。

前提の訂正: PI6 の ``thorax-mask`` は塗りつぶし領域ではなく、
左右の胸壁内縁をなぞる**2本の細い曲線**（前景 1.26%、bbox充填率 0.058）。
そのまま ``|mask ∩ thorax| / |mask|`` を計算すると中央値 0.0295 になり、
正常なマスクまで全件が「体外」になる。素の thorax PNG を使う設計は成立しない。

``lung-mask`` 単独も使えない。胸水・気胸は定義上、含気肺野の外にある
（実測で胸水131件中111件 = 85% が containment < 0.5）。

そこで thorax / lung / mediastinum から領域を組み直す::

    rowfill(M)     各行 y について M の画素がある行を [min_x(y), max_x(y)] で埋める
    THORAX_REGION  rowfill(thorax) ∪ lung ∪ mediastinum を穴埋めして最大成分
    LATERAL_BAND   同じ行方向の範囲を全ての行へ外挿（上下は端の行を継承）

``LATERAL_BAND`` は垂直方向に無限なので、肺尖への伸展や肋横角への胸水は罰しない。
一方で胸壁より外側 —— 上腕・肩の軟部組織へのはみ出し —— は確実に外になる。
実測の裏付け: THORAX_REGION の外に出ている面積の 93.3% が側方、下方は 6.7%。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import cv2
import numpy as np

# 距離変換のマスクサイズ。5 は L2 距離の近似精度と速度の釣り合いが良い。
_DIST_MASK = 5


@dataclass(frozen=True)
class BodyRegion:
    """1ファイル分の体外判定の下ごしらえ。

    ``distance_mm`` は「側方バンドの外へどれだけ出ているか」を mm で持つ。
    マージン m mm の膨張は ``distance_mm <= m`` と等価なので、
    マージンを変えても ``cv2.dilate`` をやり直す必要がない。
    """

    lateral_band: np.ndarray
    thorax_region: np.ndarray
    distance_mm: np.ndarray
    band_ratio: float
    region_ratio: float


@dataclass(frozen=True)
class OutsideBodyMetrics:
    """マスク1枚の体外らしさ。比率と実面積の両方を出す。

    比率だけだと巨大マスクの30mmのトゲを過小評価し、
    実面積だけだと巨大マスクが軒並み引っかかる。
    """

    containment: float
    outside_mm2: float | None
    outside_pixels: int
    max_distance_mm: float
    margin_mm: float


def _row_extent(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """各行の前景の左端・右端と、前景があるかを返す。

    行ごとの Python ループを避ける。2400行 × 1083ファイルでは差が大きい。
    """
    has = mask.any(axis=1)
    first = mask.argmax(axis=1)
    last = mask.shape[1] - 1 - mask[:, ::-1].argmax(axis=1)
    return first, last, has


def _span_to_mask(
    first: np.ndarray, last: np.ndarray, has: np.ndarray, width: int
) -> np.ndarray:
    """行ごとの [first, last] 区間を bool 配列へ展開する。"""
    columns = np.arange(width)[None, :]
    return has[:, None] & (columns >= first[:, None]) & (columns <= last[:, None])


def rowfill(mask: np.ndarray) -> np.ndarray:
    """各行について、前景がある範囲を左端から右端まで埋める。"""
    first, last, has = _row_extent(mask)
    return _span_to_mask(first, last, has, mask.shape[1])


def build_region(
    references: Mapping[str, np.ndarray], spacing_y: float | None
) -> BodyRegion | None:
    """参照マスクから胸郭領域と側方バンドを組む。

    ``spacing_y`` が無いと距離を mm に換算できないので None を返す
    （画素のままでは施設間で 5.2 倍ぶれるので比較にならない）。
    """
    thorax = references.get("thorax")
    if thorax is None or spacing_y is None:
        return None

    region = rowfill(thorax)
    for kind in ("lung", "mediastinum"):
        extra = references.get(kind)
        if extra is not None and extra.shape == region.shape:
            region |= extra
    region = _largest_component(_fill_holes(region))
    if not region.any():
        return None

    band = _extrapolate_rows(region)
    # 側方バンドの外側について、境界からの距離[mm]を求める。
    distance = cv2.distanceTransform(
        (~band).astype(np.uint8), cv2.DIST_L2, _DIST_MASK
    ) * float(spacing_y)

    return BodyRegion(
        lateral_band=band,
        thorax_region=region,
        distance_mm=distance,
        band_ratio=float(band.mean()),
        region_ratio=float(region.mean()),
    )


def evaluate(
    mask: np.ndarray,
    region: BodyRegion,
    margin_mm: float,
    area_mm2: float | None,
) -> OutsideBodyMetrics:
    """マスクが側方バンドからどれだけ出ているかを測る。"""
    distances = region.distance_mm[mask]
    if distances.size == 0:
        return OutsideBodyMetrics(1.0, 0.0, 0, 0.0, margin_mm)

    inside = distances <= margin_mm
    containment = float(inside.mean())
    outside_pixels = int((~inside).sum())
    outside_mm2 = (1.0 - containment) * area_mm2 if area_mm2 is not None else None
    return OutsideBodyMetrics(
        containment=containment,
        outside_mm2=outside_mm2,
        outside_pixels=outside_pixels,
        max_distance_mm=float(distances.max()),
        margin_mm=margin_mm,
    )


# ------------------------------------------------------------------ helpers


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """外側から到達できない穴を埋める。"""
    binary = mask.astype(np.uint8)
    height, width = binary.shape
    flood = binary.copy()
    # floodFill の作業用マスクは上下左右に1画素ずつ大きい必要がある。
    work = np.zeros((height + 2, width + 2), dtype=np.uint8)
    cv2.floodFill(flood, work, (0, 0), 1)
    # 外側から届かなかった 0 が穴。
    return mask | (flood == 0)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """最大の連結成分だけを残す。参照マスクのノイズを落とす。"""
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    if n_labels <= 1:
        return mask
    areas = stats[1:, cv2.CC_STAT_AREA]
    return labels == (int(np.argmax(areas)) + 1)


def _extrapolate_rows(region: np.ndarray) -> np.ndarray:
    """行方向の範囲を全ての行へ広げる。

    領域が定義されていない行（肺尖より上、横隔膜より下）は、
    最も近い定義済みの行の範囲を継承する。これにより垂直方向には
    罰を与えず、側方のはみ出しだけを見ることになる。
    """
    height, width = region.shape
    left, right, has = _row_extent(region)
    if not has.any():
        return np.zeros_like(region, dtype=bool)

    # 未定義の行を、最も近い定義済みの行の区間で埋める。
    # 前方継承（直前の定義済み行）→ 後方継承（最初の定義済み行より上）の順。
    rows = np.arange(height)
    forward = np.maximum.accumulate(np.where(has, rows, -1))
    backward = np.minimum.accumulate(np.where(has, rows, height)[::-1])[::-1]
    source = np.where(forward >= 0, forward, backward)

    return _span_to_mask(left[source], right[source], np.ones(height, bool), width)
