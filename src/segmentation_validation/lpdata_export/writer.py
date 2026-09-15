"""lp-data 形式のデータセットJSONを書き出す。

**組み立て側から独立させてある。** 統合JSON由来の builder だけでなく、後日の
「読影レポートCSVによるラベル更新処理」も同じ関数で書き出せるようにするため
（``reader.read_dataset`` → 更新 → ``write_dataset`` で往復できる）。

書式は lp-data の ``io/structured.py::save_json`` に合わせて
``indent=4, sort_keys=True``。
lp-data で読んで保存し直しても差分が出ないようにするため。

サンプルを溜め込まず逐次書き出す構成も検討したが、入力の統合JSON（100MB）を
アダプタが丸ごとパースして保持している時点でメモリはそちらが支配的で、
出力側だけ流しても意味が無い。素直に dict を受け取る。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def write_dataset(path: Path | str, dataset: Mapping[str, Any]) -> Path:
    """データセットJSONを書き出す（同一ディレクトリの一時ファイル経由で atomic）。

    途中で落ちたときに壊れたJSONを残さない。``build-dataset`` 等と同じ流儀。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(dataset, handle, ensure_ascii=False, indent=4, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return path


def build_dataset(
    meta: Mapping[str, Any], samples: Mapping[str, Any]
) -> dict[str, Any]:
    """トップレベルを組み立てる。

    ``meta`` / ``samples`` は予約されたセクション名で、``sample_id`` には使えない。
    ここで衝突を弾いておかないと、読み込み側が (B) の samples キー省略形と
    取り違える余地が残る。
    """
    reserved = {"meta", "samples"} & set(samples)
    if reserved:
        raise ValueError(f"sample_id に予約名は使えない: {sorted(reserved)}")
    return {"meta": dict(meta), "samples": dict(samples)}
