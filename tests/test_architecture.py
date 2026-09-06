"""構造の不変条件を AST で検査する。

コメントや docstring での約束は破られても気づけない。ここで機械的に固定する。

- **画素ファイルを開くのは限られた場所だけ。** 1665枚のマスクを1度しか読まない
  という設計がこれで成立している。`checks/` に1つ Path.exists を書いた瞬間に、
  純関数だったチェックが 829 ファイルぶんの I/O を始める
- **`import fiftyone` は review/ の3モジュールだけ。** FiftyOne 無しで検証本体が
  動くという前提（`test_no_fiftyone.py` が実行時にも確かめている）
- **元JSON形式の知識は adapters/ だけ。** 別形式のデータセットが増えても
  チェックを再利用できるようにするため
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "segmentation_validation"

# fiftyone を import してよいモジュール。
FIFTYONE_ALLOWED = {
    "review/fiftyone_builder.py",
    "review/export_decisions.py",
    "review/import_decisions.py",
}
# 画素ファイルを開くライブラリ（PIL / pydicom）を import してよいモジュール。
PIXEL_IO_ALLOWED = {
    "core/imageio.py",  # 読み込みの実装そのもの
    "review/export_assets.py",  # 目視用の書き出し（DICOM全画素を読む唯一の場所）
}
# 元JSONの階層構造（``dataset[inst][study].series_list[...].file_list[...]``）を
# 知ってよいモジュール。``annotations`` は manifest 等でも使う一般的な語なので
# 判定に使わない —— 階層のナビゲーションに固有な2つだけを見る。
RAW_JSON_KEYS = ("series_list", "file_list")
RAW_JSON_ALLOWED = {
    "adapters/engineer_set.py",  # 形式知識の置き場
    "selection/build_dataset.py",  # 元JSONと同一スキーマで書き出す
    # ★既知の例外。Phase 1 から残る1症例の重畳図CLIで、アダプタを経由せず
    # 元JSONを直接歩いている。debug/fallback 専用なので許容しているが、
    # 触るときはアダプタ経由へ寄せる。
    "overlay_masks.py",
}
# ファイルI/Oを表すメソッド呼び出し。``measurement.exists`` のような
# **計測値のフィールド参照**は対象外（走査時に済ませた結果を読むだけなので正しい）。
IO_CALLS = ("exists", "is_file", "read_bytes", "read_text", "iterdir", "glob")


def modules() -> list[tuple[str, ast.Module]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found.append((rel, ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return found


def imported_roots(tree: ast.Module) -> set[str]:
    """この module が実際に import している最上位パッケージ名。

    docstring や文字列は数えない —— AST を使う理由がこれ。
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


ALL_MODULES = modules()


def test_モジュールを1つ以上見つけている():
    """パス指定を間違えて0件を全部通してしまわないこと。"""
    assert len(ALL_MODULES) > 30


@pytest.mark.parametrize("rel,tree", ALL_MODULES, ids=[m[0] for m in ALL_MODULES])
def test_fiftyoneのimportはreviewの3モジュールだけ(rel, tree):
    if "fiftyone" not in imported_roots(tree):
        return
    assert rel in FIFTYONE_ALLOWED, (
        f"{rel} が fiftyone を import している。"
        "FiftyOne 無しで検証本体が動くという前提が壊れる"
    )


@pytest.mark.parametrize("rel,tree", ALL_MODULES, ids=[m[0] for m in ALL_MODULES])
def test_画素ファイルを開くのは限られた場所だけ(rel, tree):
    roots = imported_roots(tree)
    offenders = roots & {"PIL", "pydicom"}
    if not offenders:
        return
    assert rel in PIXEL_IO_ALLOWED, (
        f"{rel} が {sorted(offenders)} を import している。"
        "画素ファイルの読み込みは core/imageio.py に一本化する"
    )


def test_checksは純関数でファイルを触らない():
    """`checks/` は計測値だけを入力とする。I/O を1つ入れると設計が崩れる。

    ``measurement.exists`` のようなフィールド参照は I/O ではないので通す
    （走査時に読んだ結果を見ているだけ）。**呼び出し**だけを違反とみなす。
    """
    offenders = []
    for rel, tree in ALL_MODULES:
        if not rel.startswith("checks/"):
            continue
        if imported_roots(tree) & {"PIL", "pydicom"}:
            offenders.append(f"{rel}: PIL/pydicom を import している")
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in IO_CALLS
            ):
                offenders.append(f"{rel}: .{node.func.attr}() を呼んでいる")
    assert offenders == []


def test_元JSONの階層構造を知るのはadaptersだけ():
    """``dataset[inst][study].series_list[...]`` の形を知るのはアダプタの責務。

    ここが広がると、別形式のデータセットを足したときにチェックを再利用できない。
    """
    offenders = []
    for rel, tree in ALL_MODULES:
        if rel in RAW_JSON_ALLOWED:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in RAW_JSON_KEYS:
                offenders.append(f"{rel}: {node.value!r} を直接参照している")
    assert offenders == []


def test_coreはmatplotlibを使わない():
    """描画は viz/ と report/ に閉じる。core だけで GUI 依存を引かないこと。"""
    offenders = [
        rel
        for rel, tree in ALL_MODULES
        if rel.startswith("core/") and "matplotlib" in imported_roots(tree)
    ]
    assert offenders == []
