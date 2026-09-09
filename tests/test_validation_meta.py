"""出力ディレクトリの ``meta.json``（``report/validation_meta.py``）。

**なぜ要るのか。** 同じ情報は ``issues.json`` の ``meta`` にもあるが、実データで
60MB ある。``serve`` は状態表示のたびに「この成果物はこの fingerprint のものか」
「計測は揃っているか」を判定するので、そこを安く読めないと成立しない。

**段ごとに別のキーを埋める**（``check`` が計測、``select`` が規模）ので、
後の段が前の段の記録を消さないことが要件になる。
"""

from __future__ import annotations

from pathlib import Path

from segmentation_validation.report.validation_meta import (
    META_FILENAME,
    Measurements,
    read_meta,
    write_meta,
)

# --------------------------------------------------------------- 計測の判定


def test_全部計測できていれば完全():
    m = Measurements(n_files=100, n_measured=100, scan_required=True)

    assert m.complete is True
    assert m.usable is True


def test_1件でも足りなければ不完全():
    """走査は中断・再開できるので半端な状態が普通に起きる。"""
    m = Measurements(n_files=100, n_measured=99, scan_required=True)

    assert m.complete is False
    assert m.usable is False


def test_行が重複していても足りていれば完全():
    """キャッシュは追記型で、再開時に同じ file_uid の行が重複しうる。"""
    m = Measurements(n_files=100, n_measured=105, scan_required=True)

    assert m.complete is True


def test_画素を要らないなら計測ゼロでも使える():
    """M06/M07/S02 だけを回したときにここで止めてはいけない。"""
    m = Measurements(n_files=100, n_measured=0, scan_required=False)

    assert m.complete is False
    assert m.usable is True


def test_不足を人が読める形で言える():
    m = Measurements(n_files=33422, n_measured=0, scan_required=True)

    assert m.shortfall() == "33422 件中 0 件"


def test_壊れた辞書からは作らない():
    """★ここで嘘の値を作ると、計測が無いのに「揃っている」ことになる。"""
    assert Measurements.from_dict(None) is None
    assert Measurements.from_dict({}) is None
    assert Measurements.from_dict({"n_files": "たくさん"}) is None
    assert Measurements.from_dict([1, 2]) is None


def test_辞書と往復する():
    m = Measurements(n_files=10, n_measured=3, scan_required=True)

    restored = Measurements.from_dict(m.as_dict())

    assert restored == m


# ------------------------------------------------------- 読み書きと引き継ぎ


def test_書いて読める(tmp_path: Path):
    write_meta(
        tmp_path,
        fingerprint="v_abc",
        sources=["a.json", "b.json"],
        scale={"images": 10},
        checks_executed=["M01_MASK_RESOLUTION"],
        checks_partial=False,
        measurements=Measurements(n_files=10, n_measured=10, scan_required=True),
    )

    meta = read_meta(tmp_path)

    assert meta is not None
    assert meta.fingerprint == "v_abc"
    assert meta.n_sources == 2
    assert meta.scale == {"images": 10}
    assert meta.checks_executed == ("M01_MASK_RESOLUTION",)
    assert meta.measurements is not None
    assert meta.measurements.complete is True


def test_渡さなかったキーは引き継ぐ(tmp_path: Path):
    """★``select`` が ``check`` の記録を消すと、次から計測ゲートが効かない。"""
    write_meta(
        tmp_path,
        fingerprint="v_abc",
        sources=["a.json"],
        checks_executed=["M01_MASK_RESOLUTION"],
        checks_partial=True,
        measurements=Measurements(n_files=10, n_measured=2, scan_required=True),
    )

    # select 相当。規模だけを更新する。
    write_meta(tmp_path, fingerprint="v_abc", sources=["a.json"], scale={"images": 9})

    meta = read_meta(tmp_path)
    assert meta is not None
    assert meta.scale == {"images": 9}
    assert meta.checks_executed == ("M01_MASK_RESOLUTION",), "消えてはいけない"
    assert meta.checks_partial is True, "部分実行の印が消えてはいけない"
    assert meta.measurements is not None
    assert meta.measurements.n_measured == 2


def test_無ければNone(tmp_path: Path):
    """この形式より前の出力ディレクトリ。**異常ではない。**"""
    assert read_meta(tmp_path) is None


def test_壊れていてもNoneを返して落ちない(tmp_path: Path):
    (tmp_path / META_FILENAME).write_text("{ではないJSON", encoding="utf-8")

    assert read_meta(tmp_path) is None


def test_版が違えば分からないものとして扱う(tmp_path: Path):
    """★将来形式を変えたときに、古い内容を新しい意味で読まない。"""
    import json

    (tmp_path / META_FILENAME).write_text(
        json.dumps({"meta_version": 999, "fingerprint": "v_abc"}), encoding="utf-8"
    )

    assert read_meta(tmp_path) is None


def test_書き込みは原子的(tmp_path: Path):
    """配信中に読まれても壊れた JSON を見せない。"""
    write_meta(tmp_path, fingerprint="v_abc", sources=["a.json"])

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != META_FILENAME]

    assert leftovers == [], "一時ファイルが残っている"
