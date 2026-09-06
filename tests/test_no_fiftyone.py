"""fiftyone が無い環境でも検証本体が動くことを実行時に確かめる。

静的検査（import 文の走査）だけでは、遅延 import や再エクスポート経由の
依存を見落とす。``sys.modules`` に fiftyone を import できない偽物を差し込んで
実際に import・実行してみる。

実行:
    uv run pytest tests/test_no_fiftyone.py    # 別プロセスで走らせる
    uv run python tests/test_no_fiftyone.py    # 単体でも走る

★**pytest からは必ず別プロセスで呼ぶ。** ``builtins.__import__`` を差し替えて
``sys.modules`` から fiftyone を消すので、同じプロセスの他のテストを壊す。
"""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path

BLOCKED = ("fiftyone",)


def _block_fiftyone() -> None:
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in BLOCKED:
            raise ImportError(f"fiftyone は使えない環境（テストで遮断）: {name}")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = guarded
    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]


def main() -> int:
    _block_fiftyone()
    failures: list[str] = []

    # 検証本体が import できること
    modules = [
        "segmentation_validation.cli",
        "segmentation_validation.config",
        "segmentation_validation.adapters",
        "segmentation_validation.checks",
        "segmentation_validation.core.measure",
        "segmentation_validation.selection.decisions",
        "segmentation_validation.selection.image_decisions",
        "segmentation_validation.selection.build_dataset",
        "segmentation_validation.report.gui",
        "segmentation_validation.report.precision",
        "segmentation_validation.report.summary_md",
        # fiftyone を使わないレビュー用モジュール
        "segmentation_validation.review.review_schema",
        "segmentation_validation.review.export_assets",
        "segmentation_validation.review.manifest",
        "segmentation_validation.review",
    ]
    for name in modules:
        try:
            __import__(name)
            print(f"  OK  import {name}")
        except Exception as error:
            failures.append(f"import {name}: {type(error).__name__}: {error}")
            print(f"  NG  import {name}: {error}")

    # コマンドが実際に走ること（JSONだけで判定できるチェック）
    try:
        from segmentation_validation.cli import main as cli_main

        code = cli_main(["list-checks"])
        print(f"  {'OK ' if code == 0 else 'NG '} list-checks -> exit {code}")
        if code != 0:
            failures.append(f"list-checks exit {code}")
    except Exception as error:
        failures.append(f"list-checks: {type(error).__name__}: {error}")
        print(f"  NG  list-checks: {error}")

    # fiftyone を使うモジュールは import 時点では通り、呼んだときに失敗すべき
    try:
        import segmentation_validation.review.fiftyone_builder as fb

        print("  OK  import review.fiftyone_builder（遅延importなので通る）")
        try:
            fb.dataset_summary(
                __import__(
                    "segmentation_validation.config", fromlist=["load_config"]
                ).load_config()
            )
            failures.append("fiftyone_builder が fiftyone 無しで動いてしまった")
            print("  NG  dataset_summary が例外を出さなかった")
        except ImportError:
            print("  OK  dataset_summary は ImportError（想定通り）")
    except Exception as error:
        failures.append(f"review.fiftyone_builder: {type(error).__name__}: {error}")
        print(f"  NG  import review.fiftyone_builder: {error}")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL OK: fiftyone が無くても検証本体は動く")
    return 0


def test_fiftyoneが無くても検証本体が動く():
    """このファイル自身を別プロセスで走らせる。

    import フックをプロセスごと隔離しないと、同じ pytest セッションの
    他のテストが fiftyone を import できなくなる。
    """
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL OK" in result.stdout


if __name__ == "__main__":
    raise SystemExit(main())
