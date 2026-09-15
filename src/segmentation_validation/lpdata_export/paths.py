"""サンプルIDと、出力JSONに書くパスの決め方。

lp-data は相対パスを ``load_dataset(root_dir=...)`` 基準で解決し、**データセット
ファイルの所在ディレクトリへはフォールバックしない**。学習側の ``load_dataset_file``
は ``data.image_root`` かデータセットファイルの親を ``root_dir`` に渡すので、
こちらは**出力JSONの親ディレクトリ**を基準に相対化しておけば噛み合う。

基準の外にあるもの（読み取り専用の原本DICOM、``/mnt/medicaldb`` の参照マスク）は
相対化しても意味が無いので絶対パスのまま書く。lp-data は絶対パスをそのまま使う。
"""

from __future__ import annotations

from pathlib import Path

from ..core.records import FileGroup


def sample_id_for(group: FileGroup) -> str:
    """サンプルID。統合JSONの ``file_list`` のキーをそのまま使う。

    このキーは DICOM ファイル名の stem と一致し、統合JSON全体で一意
    （実測 42,574件すべて）。med-chest-metry-pi6 の ``tests/test_dataset_template.py``
    が ``Path(sample["image_file"]).stem == sample_id`` を検査するので、
    ``image_file`` / マスクのファイル名もこれに揃える。

    後日の読影レポートCSVによるラベル更新も、このIDを結合キーにする。
    """
    return group.file


def image_path_for(image_dir: Path, sample_id: str) -> Path:
    """``image_file`` の出力先（16bit PNG）。"""
    return Path(image_dir) / f"{sample_id}.png"


def mask_path_for(mask_dir: Path, sample_id: str) -> Path:
    """``pneumothorax_mask`` の出力先（結合済みマスクPNG）。"""
    return Path(mask_dir) / f"{sample_id}.png"


def record_path(path: Path | None, base: Path, style: str = "relative") -> str | None:
    """出力JSONに書くパス表記へ変換する。

    ``style == "relative"`` でも ``base`` の配下でなければ絶対パスにする
    （``../../..`` を並べても壊れやすいだけで、原本の所在が読めなくなる）。
    """
    if path is None:
        return None
    path = Path(path)
    if style == "relative":
        resolved = _normalized(path)
        root = _normalized(base)
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    return _normalized(path).as_posix()


def _normalized(path: Path) -> Path:
    """絶対化するが symlink は辿らない。

    ``dataset/source/`` の中身は共有データセットへのリンクで、辿ると成果物に
    記録される出所が参照した側のパスと食い違う（``config.Config.resolve`` と同じ理由）。
    """
    import os

    return Path(os.path.normpath(path.absolute()))
