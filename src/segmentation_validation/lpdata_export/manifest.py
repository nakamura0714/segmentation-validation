"""結合マスクの再現情報（``mask_merge_manifest.json``）。

``--mask-mode planned`` では ``pneumothorax_mask.pixel_array`` に出力予定パスだけを
書き、PNG は作らない。このとき**面積はメモリ上で合成したマスクから数えている**ので、
後から別工程が作る PNG がそれと一致する保証が要る。

そのために「どの元マスクを OR したか」と「期待される画素数」を残す。後工程はこれを
入力にすれば同じ合成を再現でき、出来上がった PNG の画素数を突き合わせて検証できる。
``--mask-mode generate`` のときも同じものを残す
（書き出し済みかは ``written`` で分かる）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .measure import SampleMeasurement
from .paths import mask_path_for

SCHEMA_VERSION = 1


def build_manifest(
    measurements: Iterable[SampleMeasurement],
    *,
    mask_output_dir: Path,
    mask_mode: str,
) -> dict[str, Any]:
    """マニフェストの中身を作る。マスクを持つサンプルだけを載せる。"""
    entries = []
    for measurement in measurements:
        if not measurement.has_pneumothorax_mask:
            continue
        entries.append(
            {
                "sample_id": measurement.sample_id,
                "output": str(mask_path_for(mask_output_dir, measurement.sample_id)),
                "written": measurement.mask_written,
                # OR 合成の入力。順序は出力に影響しないが、再現性のため保存順のまま。
                "sources": list(measurement.mask_sources),
                # 合成後の非ゼロ画素数。後工程の検算用。
                "expected_pixels": measurement.pneumothorax_pixels,
                "expected_area_mm2": measurement.pneumothorax_area_mm2,
            }
        )
    entries.sort(key=lambda entry: entry["sample_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "mask_mode": mask_mode,
        "mask_output_dir": str(mask_output_dir),
        "n_samples": len(entries),
        "n_multi_source": sum(1 for e in entries if len(e["sources"]) > 1),
        "entries": entries,
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path
