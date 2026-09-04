"""JSON内の相対パスを絶対パスへ解決する。

フィールドごとに基準ルートが違うので、単一のルートでは解決できない。

===================== ==================== ================================
フィールド             先頭                 基準ルート
===================== ==================== ================================
``image_path``         ``medical2/``        ``/mnt``
``path_mask``          ``annotation/``      ``/mnt/medicaldb``
``path_original_mask`` ``annotation/``      ``/mnt/medicaldb``
===================== ==================== ================================

参照マスク（lung / thorax / mediastinum）は ``image_path`` から導出する。
"""

from __future__ import annotations

from pathlib import Path

# 設定を渡さない場合の既定。config.RootsConfig と同じ値を持つ。
DEFAULT_IMAGE_ROOT = Path("/mnt")
DEFAULT_ANNOTATION_ROOT = Path("/mnt/medicaldb")


def resolve_under(root: Path | str, raw: str) -> Path:
    """``root`` を基準に相対パスを解決する。

    一部のレコードは先頭スラッシュ付きで格納されており（jsrt の2件）、
    そのまま ``Path / raw`` すると絶対パス扱いになって基準ルートが捨てられる。
    正規化は必須。
    """
    return Path(root) / raw.lstrip("/")


def resolve_image_path(raw: str, root: Path | str = DEFAULT_IMAGE_ROOT) -> Path:
    """``image_path`` を絶対パスに解決する。"""
    return resolve_under(root, raw)


def resolve_annotation_path(
    raw: str, root: Path | str = DEFAULT_ANNOTATION_ROOT
) -> Path:
    """``path_mask`` / ``path_original_mask`` を絶対パスに解決する。"""
    return resolve_under(root, raw)


def reference_mask_path(
    image_path_raw: str,
    root: Path | str,
    drop_components: list[int] | tuple[int, ...] = (0, 2, 5, 6),
) -> Path:
    """``image_path`` から参照マスク（胸郭系）のパスを導く。

    ``medical2/<task>/<split>/<site>/<date>/dcm_cr/<series>/<file_id>.dcm``
    から 0(medical2) / 2(split) / 5(dcm_cr) / 6(series) を落とすと
    ``<task>/<site>/<date>/<file_id>.png`` になり、これが参照マスク側の階層と一致する。

    アノテーションマスクのパスからは導けない。あちらのファイル名は ``geometry_uid``
    だが参照マスクのファイル名は DICOM の file_id なので、必ず ``image_path`` を使う。
    """
    parts = image_path_raw.lstrip("/").split("/")
    drop = set(drop_components)
    kept = [part for index, part in enumerate(parts) if index not in drop]
    if not kept:
        raise ValueError(f"参照マスクのパスを導けない image_path: {image_path_raw!r}")
    return (Path(root).joinpath(*kept)).with_suffix(".png")
