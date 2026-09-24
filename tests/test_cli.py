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


def test_export_lpdata_ofcがサブコマンドとして登録されている():
    """共有パーサ（``parents=``）で既存のフラグを引き継いでいること。

    20個超のオプションを2か所で書くと必ず片方だけ更新されて drift する。
    """
    from segmentation_validation.cli import _export_lpdata_ofc, build_parser

    args = build_parser().parse_args(
        [
            "export-lpdata-ofc",
            "--source",
            "ofc.json",
            "--report-status",
            "present",
            "--report-status",
            "absent",
            "--artifact-prefix",
            "ofc_",
        ]
    )

    assert args.command == "export-lpdata-ofc"
    assert args.report_status == ["present", "absent"]
    assert args.artifact_prefix == "ofc_"
    # 共有パーサ由来の既定値が export-lpdata と揃っていること。
    assert args.image_mode == "convert"
    assert args.mask_mode == "planned"
    assert args.path_style == "relative"
    assert callable(_export_lpdata_ofc)


def test_report_statusを指定しなければconfigの既定が使われる():
    """既定は present / absent。``unknown`` を既定に入れると
    「主張していない」が陰性の教師信号になる。
    """
    from segmentation_validation.config import Config

    assert Config().lpdata_export.report_label_statuses == ["present", "absent"]


def test_merge_lpdataがサブコマンドとして登録されている():
    from segmentation_validation.cli import _merge_lpdata, build_parser

    args = build_parser().parse_args(
        [
            "merge-lpdata",
            "--primary",
            "a.json",
            "--secondary",
            "b.json",
            "--out",
            "c.json",
            "--dry-run",
        ]
    )

    assert args.command == "merge-lpdata"
    assert args.dry_run is True
    assert args.no_enrich is False
    assert callable(_merge_lpdata)


def test_全サブコマンドにhandlerがある():
    """``build_parser`` と ``main`` の handlers のずれを機械的に検出する。"""
    import argparse

    from segmentation_validation.cli import HANDLERS, build_parser

    parser = build_parser()
    subparsers = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ][0]
    assert set(subparsers.choices) == set(HANDLERS)


def test_export_lpdata_ofcのmask_mode既定はplanned():
    """OFC 側の価値はマスクではなく「マスクが無くても気胸症例と分かる」こと。

    マスクを持つ画像は例外なく development 側にもあり、統合は重複時に必ず
    primary（人手整備済みのGT）を採るので、OFC 段で書き出した PNG は
    1枚も参照されない（実測273枚すべて未参照）。
    """
    from segmentation_validation.cli import build_parser

    args = build_parser().parse_args(["export-lpdata-ofc", "--source", "ofc.json"])

    assert args.mask_mode == "planned"


def test_export_lpdataのmask_mode既定は変えていない():
    """共有パーサ（``parents=``）を壊していないこと。

    development 側は結合マスクの唯一の供給源なので既定を動かさない。
    """
    from segmentation_validation.cli import build_parser

    args = build_parser().parse_args(["export-lpdata"])

    assert args.mask_mode == "planned"


def test_export_lpdata_ofcでもgenerateを明示できる():
    """将来 OFC 単独の気胸マスクが増えたときに塞がない。"""
    from segmentation_validation.cli import build_parser

    args = build_parser().parse_args(
        ["export-lpdata-ofc", "--source", "ofc.json", "--mask-mode", "generate"]
    )

    assert args.mask_mode == "generate"
