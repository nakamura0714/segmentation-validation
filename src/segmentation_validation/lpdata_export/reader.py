"""書き出した lp-data 形式JSONを読み戻す。

後日の「読影レポートCSVによるラベル更新処理」の入口になる seam。
``read_dataset`` → ラベルを更新 → ``invariants.check_samples`` →
``writer.write_dataset``
で往復できるようにしてある。

``lpdata`` には依存しない（``opencv-python`` を引いてしまい、本リポジトリが
``opencv-python-headless`` に統一している方針と衝突するため。pyproject.toml 参照）。
型付きオブジェクトが要る場面は med-chest-metry-pi6 側の環境で ``load_dataset`` を使う。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DatasetFormatError(ValueError):
    """読み込んだJSONが lp-data のデータセット形式になっていない。"""


def read_dataset(path: Path | str) -> dict[str, Any]:
    """データセットJSONを plain dict として読む。

    ``meta`` / ``samples`` の存在と ``content_type`` だけを検査する。属性値の
    型検査はしない —— そこは lp-data の仕事で、二重に持つと必ず食い違う。
    """
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DatasetFormatError(f"トップレベルがマッピングでない: {path}")

    meta = payload.get("meta")
    samples = payload.get("samples")
    if not isinstance(meta, dict):
        raise DatasetFormatError(f"meta が無い: {path}")
    if not isinstance(samples, dict):
        raise DatasetFormatError(
            f"samples セクションが無い: {path}。"
            "meta を持つファイルはサンプル本体を samples: で与える"
        )

    content_type = meta.get("content_type")
    if content_type is not None and content_type != "dataset":
        raise DatasetFormatError(
            f"データセットとして読もうとしたが content_type={content_type!r}: {path}"
        )
    return payload


def sample_ids(dataset: dict[str, Any]) -> list[str]:
    """サンプルIDの一覧。レポートCSVの結合キーはこれ（＝DICOMファイル名の stem）。"""
    return sorted(dataset["samples"])
