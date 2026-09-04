"""PI6 の胸郭系参照マスクの読み込みと、利用可否の判定。

``/mnt/medicaldb/processed/{lung,thorax,mediastinum}-mask/`` にある
別パイプラインの生成物。
整備状況が施設・バッチ日ごとに違うので、**「無い」を明示的な状態として扱う**。
無言で合格させると「問題なし」と「未検査」が区別できなくなる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

import numpy as np

from ...core.imageio import MaskReadError, load_reference_mask

logger = logging.getLogger(__name__)

KINDS = ("lung", "thorax", "mediastinum")


class ReferenceStatus:
    """参照マスクの利用可否。``cannot_determine`` の理由になる。"""

    AVAILABLE = "available"
    NO_REFERENCE = "cannot_determine:no_reference"
    LUNG_ONLY = "cannot_determine:lung_only"
    CORRUPT = "cannot_determine:reference_corrupt"
    SHAPE_MISMATCH = "cannot_determine:reference_shape_mismatch"


@dataclass
class ReferenceSet:
    """1ファイル分の参照マスク。ファイル単位で1度だけ読む。"""

    status: str
    masks: dict[str, np.ndarray] = field(default_factory=dict, repr=False)
    paths: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    available: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == ReferenceStatus.AVAILABLE


def load_references(
    paths: Mapping[str, Path | None],
    required: tuple[str, ...] | list[str],
    expected_shape: tuple[int, int] | None = None,
) -> ReferenceSet:
    """参照マスクを読み、利用可否を判定する。

    ``required`` が揃わなければ使わない。lung だけで代替判定は**しない** ——
    気胸・胸水は原理的に含気肺野の外にあるので、lung 基準の包含率は意味を持たない
    （実測でも胸水131件中111件が containment<0.5 になる）。
    """
    masks: dict[str, np.ndarray] = {}
    recorded: dict[str, str] = {}
    errors: dict[str, str] = {}
    corrupt = False
    shape_mismatch = False

    for kind in KINDS:
        path = paths.get(kind)
        if path is None:
            continue
        recorded[kind] = str(path)
        if not path.exists():
            continue
        try:
            mask = load_reference_mask(path)
        except MaskReadError as error:
            # 21バイトの "Internal Server Error" が実在する。
            # 「無い」ではなく「壊れている」= 参照側の再生成が要る、として区別する。
            errors[kind] = str(error)
            corrupt = True
            continue
        except OSError as error:
            errors[kind] = f"{type(error).__name__}: {error}"
            continue

        if expected_shape is not None and mask.shape != expected_shape:
            errors[kind] = f"shape {mask.shape} != {expected_shape}"
            shape_mismatch = True
            continue
        masks[kind] = mask

    available = tuple(kind for kind in KINDS if kind in masks)
    if all(kind in masks for kind in required):
        status = ReferenceStatus.AVAILABLE
    elif corrupt:
        status = ReferenceStatus.CORRUPT
    elif shape_mismatch:
        status = ReferenceStatus.SHAPE_MISMATCH
    elif "lung" in masks:
        status = ReferenceStatus.LUNG_ONLY
    else:
        status = ReferenceStatus.NO_REFERENCE

    return ReferenceSet(
        status=status,
        masks=masks,
        paths=recorded,
        errors=errors,
        available=available,
    )
