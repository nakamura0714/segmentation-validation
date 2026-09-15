"""気胸マスクの結合。

テンプレートの規約は「連続領域が複数ある場合も **1ファイルへ結合済み**のものを与える」。
元データは1画像に複数の brush annotation が付くことがある（実測 113画像: 2枚95 / 3枚16 /
4枚2）ので、OR 合成して1枚にする。

**面積は必ず合成後に数える。** 各マスクの面積を足すと、領域が重なったときに
二重計上になる（med-chest-metry-pi6 の ``data/README.md`` も同じ理由で
「面積は結合マスク由来とする」と定めている）。

テンプレートのもう1つの規約「**実体を置くマスクは非ゼロ領域を持つものに限る**」も
ここで担保する。全ゼロになったら「マスク無し」（``pixel_array: null``）として扱い、
PNG は書かない。こうすると空マスクの表現が1通りに定まり、面積を null にする条件も
「非ゼロ領域があるか」で一意に書ける。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from ..core.imageio import MaskReadError, load_binary_mask

logger = logging.getLogger(__name__)


@dataclass
class MergeResult:
    """結合の結果。``mask`` が None なら「非ゼロ領域のあるマスクが無い」。"""

    mask: np.ndarray | None = None
    #: 合成に使えた元マスクの絶対パス。manifest に残して後工程が再現できるようにする。
    sources: list[str] = field(default_factory=list)
    #: 読めなかった・形が合わなかったマスク。件数を summary に出す。
    errors: list[str] = field(default_factory=list)


def merge_masks(paths: Sequence[Path]) -> MergeResult:
    """マスクPNG群を OR 合成する。

    形の違うマスクが混ざったら**合成せずエラーとして記録する**。リサイズや
    パディングで辻褄を合わせると、GTの座標が黙ってずれる。
    """
    result = MergeResult()
    merged: np.ndarray | None = None
    for path in paths:
        try:
            mask = load_binary_mask(Path(path))
        except (FileNotFoundError, MaskReadError, OSError) as error:
            result.errors.append(f"{path}: {type(error).__name__}: {error}")
            continue
        if merged is None:
            merged = mask.copy()
        elif merged.shape != mask.shape:
            result.errors.append(
                f"{path}: 形が合わない {mask.shape} != {merged.shape}（合成しない）"
            )
            continue
        else:
            merged |= mask
        result.sources.append(str(path))

    # 全ゼロは「マスク無し」。実体を置かないという規約を守る。
    if merged is not None and not merged.any():
        merged = None
    result.mask = merged
    return result
