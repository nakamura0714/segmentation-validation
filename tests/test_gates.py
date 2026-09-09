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


def write_issues(
    path: Path,
    *,
    executed: list[str],
    partial: bool,
    measurements: dict | None = None,
) -> Path:
    meta = {"checks_executed": executed, "checks_partial": partial}
    if measurements is not None:
        meta["measurements"] = measurements
    path.write_text(
        json.dumps({"meta": meta, "summary": {}, "issues": []}),
        encoding="utf-8",
    )
    return path


def measured(n_files: int, n_measured: int, scan_required: bool = True) -> dict:
    return {
        "n_files": n_files,
        "n_measured": n_measured,
        "scan_required": scan_required,
    }


class FakeContext:
    """``_gate_measurements`` が推定に使う分だけを持つ偽の CheckContext。"""

    def __init__(self, n_files: int, n_measured: int) -> None:
        self.groups = tuple(range(n_files))
        self.files = {i: None for i in range(n_measured)}


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


def fake_db(monkeypatch, rows):
    """``human_decisions_in_db`` を差し替える。

    実 FiftyOne DB を触らせない（既定のテストは fiftyone も mongod も要らない
    という約束がある）。``rows`` が例外なら送出する。
    """
    from segmentation_validation.review import export_decisions

    def stub(_config):
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(export_decisions, "human_decisions_in_db", stub)


def test_manifestが無くDBも無ければ初回なので通る(tmp_path, monkeypatch):
    """守るものが無い状態でユーザーを止めない。"""
    config = Config(project_root=tmp_path)
    args = argparse.Namespace(discard_unexported=False)
    fake_db(monkeypatch, RuntimeError("dataset が無い"))

    assert _check_unexported(config, args) is True


def test_manifestが無くてもDBに判定があれば止まる(tmp_path, monkeypatch):
    """★これが通ると review build が目視の成果を消す。

    manifest が無い理由は「初回」と「fingerprint が変わった」の2つある。
    FiftyOne dataset 名は fingerprint 非依存の単一名なので、後者では DB に
    前の構成の判定が残っている。「初回だから守るものは無い」と決めつけると
    ``overwrite=True`` がそれを消す。
    """
    config = Config(project_root=tmp_path)
    args = argparse.Namespace(discard_unexported=False)
    fake_db(monkeypatch, [{"kind": "annotation", "key": "A", "reviewer": "me"}])

    assert _check_unexported(config, args) is False


def test_manifestが無くDBに判定があってもdiscardなら通る(tmp_path, monkeypatch):
    config = Config(project_root=tmp_path)
    args = argparse.Namespace(discard_unexported=True)
    fake_db(monkeypatch, [{"kind": "annotation", "key": "A", "reviewer": "me"}])

    assert _check_unexported(config, args) is True


def test_manifestが無くDBの判定が0件なら通る(tmp_path, monkeypatch):
    config = Config(project_root=tmp_path)
    args = argparse.Namespace(discard_unexported=False)
    fake_db(monkeypatch, [])

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


# --------------------------------------------- ゲート3: 計測が欠けた issues.json


def test_計測が揃っていれば通る(tmp_path):
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M01_MASK_RESOLUTION"],
        partial=False,
        measurements=measured(n_files=100, n_measured=100),
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is True


def test_計測が1件も無ければ止まる(tmp_path):
    """★これが通ると画素依存の14チェックが「検出なし」になり、
    壊れたマスクが keep で通る。部分実行より静かで危ない。
    """
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M01_MASK_RESOLUTION"],
        partial=False,
        measurements=measured(n_files=33422, n_measured=0),
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is False


def test_計測が途中までなら止まる(tmp_path):
    """走査は中断・再開できるので、半端な状態が普通に起きる。"""
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M01_MASK_RESOLUTION"],
        partial=False,
        measurements=measured(n_files=1000, n_measured=999),
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is False


def test_計測が欠けてもallow_unmeasuredなら通る(tmp_path):
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M01_MASK_RESOLUTION"],
        partial=False,
        measurements=measured(n_files=100, n_measured=0),
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=True)

    assert _check_issues_complete(path, args) is True


def test_allow_partialでは計測欠落を通さない(tmp_path):
    """★別のリスクなので兼用しない。

    「一部のチェックだけ回した」を許したつもりで「画素を読んでいない」まで
    許してしまうのを避ける。
    """
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M01_MASK_RESOLUTION"],
        partial=False,
        measurements=measured(n_files=100, n_measured=0),
    )
    args = argparse.Namespace(allow_partial=True, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is False


def test_画素を要らないチェックだけなら計測が無くても通る(tmp_path):
    """M06/M07/S02 は JSON だけで判定できる。ここを止めると使えなくなる。"""
    path = write_issues(
        tmp_path / "issues.json",
        executed=["M06_PATH_FORMAT"],
        partial=False,
        measurements=measured(n_files=100, n_measured=0, scan_required=False),
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is True


def test_計測の記録が無い古いissuesはその場の実測で判定する(tmp_path):
    """★「記録が無い」を「問題なし」と解釈しない。

    この形式より前に作られた issues.json が対象。計測キャッシュを実測して
    足りなければ止める（推定であることは警告に出す）。
    """
    path = write_issues(
        tmp_path / "issues.json", executed=["M01_MASK_RESOLUTION"], partial=False
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args, FakeContext(100, 0)) is False
    assert _check_issues_complete(path, args, FakeContext(100, 100)) is True


def test_contextが無ければ計測は判定しない(tmp_path):
    """既存の呼び出し（2引数）を壊さない。"""
    path = write_issues(
        tmp_path / "issues.json", executed=["M01_MASK_RESOLUTION"], partial=False
    )
    args = argparse.Namespace(allow_partial=False, allow_unmeasured=False)

    assert _check_issues_complete(path, args) is True
