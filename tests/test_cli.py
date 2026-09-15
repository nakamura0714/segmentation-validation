"""``cli.py`` の純関数（fiftyone・実データ不要）だけを対象にした単体テスト。"""

from __future__ import annotations

from segmentation_validation.cli import _pending_diff_message


def _summary(pending: int) -> dict:
    return {"pending": pending}


def test_初回実行では前回が無いのでメッセージを出さない():
    assert _pending_diff_message(None, _summary(10), None, _summary(0)) is None


def test_pendingが減った分を差分として出す():
    message = _pending_diff_message(
        _summary(2280), _summary(2260), _summary(0), _summary(0)
    )

    assert message is not None
    assert "2280 → 2260" in message
    assert "-20" in message


def test_pendingが変わらなければ0で出す():
    message = _pending_diff_message(
        _summary(2187), _summary(2187), _summary(5), _summary(5)
    )

    assert "2187 → 2187" in message
    assert "+0" in message


def test_annotationと画像の差分を両方出す():
    message = _pending_diff_message(
        _summary(100), _summary(90), _summary(10), _summary(12)
    )

    assert "100 → 90" in message
    assert "-10" in message
    assert "10 → 12" in message
    assert "+2" in message


def test_export_lpdataがサブコマンドとして登録されている():
    """``build_parser`` と ``main`` の handlers は別々に書くので、
    片方だけ足すと実行時に「未知のサブコマンド」で落ちる。
    """
    from segmentation_validation.cli import _export_lpdata, build_parser

    args = build_parser().parse_args(["export-lpdata", "--limit", "3"])

    assert args.command == "export-lpdata"
    assert args.limit == 3
    # 既定値。変えると下見と本番の意味が入れ替わるので固定しておく。
    assert args.image_mode == "convert"
    assert args.mask_mode == "planned"
    assert args.on_missing_dicom == "skip"
    assert args.path_style == "relative"
    assert callable(_export_lpdata)


def test_only_datasetは繰り返し指定できる():
    from segmentation_validation.cli import build_parser

    args = build_parser().parse_args(
        ["export-lpdata", "--only-dataset", "A", "--only-dataset", "B"]
    )

    assert args.only_dataset == ["A", "B"]
