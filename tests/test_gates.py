"""成果物が黙って壊れるのを止める3つのゲート。

いずれも「手順どおり操作すると成果物が壊れる」経路を塞いだもの。
**ゲートが緩んだことに気づけないと、壊れた開発データが出荷される。**

1. `check --only` の部分結果で `select` すると採否が壊れる
   （実測: pending 251 → 11、keep 1465 → 1705）
2. `review export` していない目視結果を `review build` が消す
3. `build-dataset` の override は「許可」と「扱い」の両方が必要
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from segmentation_validation.cli import _check_issues_complete, _check_unexported
from segmentation_validation.config import Config
from segmentation_validation.selection.build_dataset import gate_message


def write_issues(path: Path, *, executed: list[str], partial: bool) -> Path:
    path.write_text(
        json.dumps(
            {
                "meta": {"checks_executed": executed, "checks_partial": partial},
                "summary": {},
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    return path


# ------------------------------------------------- ゲート1: 部分的な issues.json


def test_全チェックの結果なら通る(tmp_path):
    path = write_issues(
        tmp_path / "issues.json", executed=["M06_PATH_FORMAT"], partial=False
    )
    args = argparse.Namespace(allow_partial=False)

    assert _check_issues_complete(path, args) is True


def test_部分結果なら止まる(tmp_path):
    """★これが通ると未実行のチェックが「検出なし」になり採否が壊れる。"""
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M06_PATH_FORMAT", "M07_BBOX_GEOMETRY"],
        partial=True,
    )
    args = argparse.Namespace(allow_partial=False)

    assert _check_issues_complete(path, args) is False


def test_部分結果でもallow_partialなら通る(tmp_path):
    path = write_issues(tmp_path / "issues.json", executed=["M06"], partial=True)
    args = argparse.Namespace(allow_partial=True)

    assert _check_issues_complete(path, args) is True


def test_metaが無い古いissuesは通す(tmp_path):
    """印の無い成果物を「部分結果」と決めつけない（後方互換）。"""
    path = tmp_path / "issues.json"
    path.write_text(json.dumps({"issues": []}), encoding="utf-8")
    args = argparse.Namespace(allow_partial=False)

    assert _check_issues_complete(path, args) is True


def test_壊れたjsonは後段に任せる(tmp_path):
    """ここで嘘の判断をせず、読み込み側でエラーにする。"""
    path = tmp_path / "issues.json"
    path.write_text("{ではないJSON", encoding="utf-8")
    args = argparse.Namespace(allow_partial=False)

    assert _check_issues_complete(path, args) is True


# ------------------------------------- ゲート2: export していない目視結果


def test_manifestが無ければ初回なので通る(tmp_path):
    """守るものが無い状態でユーザーを止めない。"""
    config = Config(project_root=tmp_path)
    args = argparse.Namespace(discard_unexported=False)

    assert _check_unexported(config, args) is True


# ------------------------------------- ゲート3: build-dataset の override


def test_未確定が無ければ通る():
    assert gate_message("pending", 0, "annotation", False, None) is None


def test_未確定があればoverrideなしで止まる():
    message = gate_message("pending", 251, "annotation", False, None)

    assert message is not None
    assert "251" in message
    assert "--allow-pending" in message
    assert "--pending-as" in message


def test_許可だけでは通らない():
    """★`--allow-pending` だけで通すと保留が黙って開発データへ入る。"""
    assert gate_message("pending", 251, "annotation", True, None) is not None


def test_扱いだけでは通らない():
    """`--pending-as` だけで通すと、許可していないのに落ちる。"""
    assert gate_message("pending", 251, "annotation", False, "exclude") is not None


def test_許可と扱いの両方があれば通る():
    assert gate_message("pending", 251, "annotation", True, "exclude") is None
    assert gate_message("pending", 251, "annotation", True, "keep") is None


def test_uncertainにも同じ規則が効く():
    assert gate_message("uncertain", 3, "annotation", False, None) is not None
    assert gate_message("uncertain", 3, "annotation", True, None) is not None
    assert gate_message("uncertain", 3, "annotation", True, "exclude") is None


def test_画像側にも同じ規則が効く():
    """annotation と画像で判断が食い違わないこと。"""
    message = gate_message("pending", 33, "画像", False, None)

    assert message is not None
    assert "画像 33" in message
