"""コマンドライン入口。

実運用のループは ``check`` → （目視）→ ``review export`` → ``select`` → ``report``。
``select`` を独立させてあるのは、目視のたびに検出をやり直す必要が無いから。
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import open_adapters
from .checks import ALL_CHECKS, requires_scan, selected_checks
from .checks.base import CheckContext, Issue, Severity
from .config import Config, dump_config, load_config
from .core.labels import label_selectors, matches_target
from .core.records import AnnotationRecord, FileGroup
from .report.issues_json import summarize as summarize_issues
from .report.issues_json import write_issues_csv, write_issues_json
from .report.selection_output import (
    write_image_csv,
    write_image_json,
    write_selection_csv,
    write_selection_json,
)
from .report.validation_meta import Measurements, write_meta
from .selection.automatic import (
    build_automatic_decisions,
    build_broken_decisions,
    merge_automatic,
    summarize_automatic,
)
from .selection.decisions import (
    Decision,
    HumanDecision,
    assert_invariants,
    build_decisions,
)
from .selection.decisions import summarize as summarize_decisions
from .selection.image_decisions import (
    assert_image_invariants,
    build_image_decisions,
    load_image_decision_overrides,
    summarize_images,
)

logger = logging.getLogger(__name__)

# 終了コード: 0=errorなし / 1=errorあり / 2=ツール障害
EXIT_OK = 0
EXIT_ISSUES = 1
EXIT_FAILURE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="segmentation-validation",
        description="ChestMetry PI6 セグメンテーションデータセットのバリデーション",
    )
    parser.add_argument("--config", type=Path, default=None, help="設定JSON")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="設定の単発上書き（例 --set thresholds.duplicate_iou_near=0.9）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUGログを出す")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show-config", help="解決後の設定を表示する")
    sub.add_parser("list-sources", help="検証対象として解決されたJSONを一覧する")
    sub.add_parser("list-checks", help="登録されているチェックを一覧する")

    check = sub.add_parser("check", help="チェックを実行して issues を出力する")
    check.add_argument("--only", help="実行するチェック（カンマ区切り。例 M06,M07）")
    check.add_argument("--skip", help="除外するチェック（カンマ区切り）")

    scan = sub.add_parser("scan", help="画素を走査して計測キャッシュを作る")
    scan.add_argument("--jobs", type=int, default=8, help="並列数")
    scan.add_argument("--force", action="store_true", help="キャッシュを捨てて作り直す")
    scan.add_argument("--limit", type=int, default=None, help="先頭N件だけ走査する")

    select = sub.add_parser(
        "select", help="issues と review 結果から全annotationの採否を確定する"
    )
    select.add_argument(
        "--review-decisions", type=Path, default=None, help="人間の判定JSON"
    )
    select.add_argument(
        "--allow-partial",
        action="store_true",
        help="check --only で作った部分的な issues.json でも採否を作る（既定は停止）",
    )
    select.add_argument(
        "--allow-unmeasured",
        action="store_true",
        help="計測キャッシュが足りないまま採否を作る（既定は停止）。"
        "画素を要するチェック14種が「検出なし」になっていることを承知のうえで使う",
    )

    sub.add_parser("report", help="summary.md を書き出す")
    sub.add_parser("gui", help="ブラウザで見るダッシュボードHTMLを書き出す")

    serve = sub.add_parser(
        "serve", help="ダッシュボードを常駐サーバーで配信する（上げっぱなしにする）"
    )
    serve.add_argument(
        "--port", type=int, default=None, help="既定は config の gui.port（8899）"
    )
    serve.add_argument("--host", default=None, help="既定は 127.0.0.1")
    serve.add_argument(
        "--no-refresh",
        action="store_true",
        help="ブラウザからの更新を無効にする（閲覧専用にする）",
    )
    serve.add_argument(
        "--fingerprint",
        default=None,
        metavar="v_xxxxxxxxxxxx",
        help="配信する fingerprint を明示する（既定は設定から自動算出）。"
        "設定と不一致なら混成を避けるため読み取り専用になる",
    )

    build = sub.add_parser(
        "build-dataset", help="採否マスタから development.json を生成する"
    )
    build.add_argument(
        "--allow-pending",
        action="store_true",
        help="pending が残っていても生成する（--pending-as と併用必須）",
    )
    build.add_argument(
        "--pending-as", choices=("keep", "exclude"), default=None, help="pending の扱い"
    )
    build.add_argument(
        "--allow-uncertain",
        action="store_true",
        help="uncertain が残っていても生成する（--uncertain-as と併用必須）",
    )
    build.add_argument(
        "--uncertain-as",
        choices=("keep", "exclude"),
        default=None,
        help="uncertain の扱い",
    )
    build.add_argument("--version-tag", default=None, help="出力先のバージョン名")
    build.add_argument(
        "--no-merged",
        action="store_true",
        help="12本を1本にまとめた統合JSON（学習パイプライン入力）を作らない。"
        "既定は作る",
    )
    build.add_argument(
        "--merged-name",
        default="development_merged.json",
        help="統合JSONのファイル名（既定 development_merged.json）",
    )
    build.add_argument(
        "--strict-conflicts",
        action="store_true",
        help="元データ間で study/series の属性が食い違っていたら停止する。"
        "既定は meta_development.conflicts に両方の値を記録して続行",
    )

    lpdata_common = _lpdata_common_parser()
    lpdata = sub.add_parser(
        "export-lpdata",
        parents=[lpdata_common],
        help="統合JSONを lp-data 標準形式のデータセットJSONへ書き出す",
    )
    lpdata.add_argument(
        "--merged",
        type=Path,
        default=None,
        help="入力の統合JSON（既定は development_dir の最新 development_merged.json）",
    )

    ofc = sub.add_parser(
        "export-lpdata-ofc",
        parents=[lpdata_common],
        help="構造化読影レポート付き engineer-set JSON を lp-data 形式へ書き出す",
    )
    ofc.add_argument(
        "--source",
        type=Path,
        required=True,
        help="入力の engineer-set JSON（study に report_labels を持つもの）",
    )
    ofc.add_argument(
        "--report-status",
        action="append",
        default=None,
        metavar="STATUS",
        help="取り込む report_labels.pneumothorax_status（繰り返し可）。"
        "既定は config の lpdata_export.report_label_statuses。"
        "unknown は「気胸でない」ではなく「主張していない」の意味なので既定に入れない",
    )

    merge = sub.add_parser(
        "merge-lpdata",
        help="2つの lp-data データセットJSONを1つに統合する",
    )
    merge.add_argument(
        "--primary",
        type=Path,
        required=True,
        help="重複時に残す側（通常は development 由来）",
    )
    merge.add_argument(
        "--secondary",
        type=Path,
        required=True,
        help="ラベルの補強に使う側（通常は読影レポート由来）",
    )
    merge.add_argument("--out", type=Path, required=True, help="統合後のJSON")
    merge.add_argument(
        "--template",
        type=Path,
        default=None,
        help="属性の型と意味の正典となるテンプレートYAML（指定すると突合する）",
    )
    merge.add_argument(
        "--dry-run",
        action="store_true",
        help="データセットJSONを書かず、件数と裁定結果だけ出す",
    )
    merge.add_argument(
        "--no-enrich",
        action="store_true",
        help="重複時にラベル補強をせず primary をそのまま残す",
    )
    merge.add_argument("--dataset-name", default=None, help="meta.dataset_name")
    merge.add_argument("--dataset-id", default=None, help="meta.dataset_id")
    merge.add_argument("--owner", default=None, help="meta.owner")

    review = sub.add_parser("review", help="FiftyOne での目視レビュー")
    rsub = review.add_subparsers(dest="review_command", required=True)
    rbuild = rsub.add_parser(
        "build", help="アセットを書き出して FiftyOne dataset を作る"
    )
    rbuild.add_argument(
        "--all",
        dest="all_images",
        action="store_true",
        help="目視対象だけでなく全画像を書き出す（DICOM全画素読みで時間がかかる。"
        "config の review.export_all_files でも指定できる）",
    )
    rbuild.add_argument("--force", action="store_true", help="既存のアセットも作り直す")
    rbuild.add_argument(
        "--assets-only",
        action="store_true",
        help="アセットと manifest だけ作る（fiftyone を使わない）",
    )
    rbuild.add_argument(
        "--discard-unexported",
        action="store_true",
        help="review export していない人間の判定を捨てて作り直す（既定は停止する）",
    )
    rbuild.add_argument(
        "--jobs",
        type=int,
        default=8,
        help="アセット書き出しの並列数（scan --jobs と同じ既定値）",
    )
    rbuild.add_argument(
        "--sample-unannotated",
        type=int,
        default=None,
        help="未アノテーション画像を (dataset_id, image_class, series_image_index) "
        "ごとにN件サンプルしてFiftyOneに含める（採否は変えない。目視での妥当性確認用）",
    )
    rsub.add_parser("launch", help="App を localhost で起動する")
    rsub.add_parser("status", help="目視の進捗を表示する")
    rsub.add_parser("export", help="判定を review_decisions.json へ書き出す")
    rsub.add_parser(
        "flag-report",
        help="flag:needs_reportの付いた項目をflagged_for_report.csvへ書き出す",
    )
    rsub.add_parser("precision", help="自動ルールの Precision を集計する")
    rimport = rsub.add_parser("import", help="review_decisions.json を FiftyOne へ戻す")
    rimport.add_argument("--path", type=Path, default=None, help="読み込むJSON")
    rmigrate = rsub.add_parser(
        "migrate-decisions",
        help="fingerprint配下の目視判定を review/review_decisions.json（正本）へ移す",
    )
    rmigrate.add_argument(
        "--dry-run",
        action="store_true",
        help="書き込まずに移行結果だけ表示する",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        config = load_config(args.config, args.overrides)
    except (ValueError, OSError) as error:
        logger.error("設定を読めない: %s", error)
        return EXIT_FAILURE

    handler = HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"未知のサブコマンド: {args.command}")
        return EXIT_FAILURE
    return handler(config, args)


# ------------------------------------------------------------------ commands


def _show_config(config: Config, args: argparse.Namespace) -> int:
    print(dump_config(config))
    return EXIT_OK


def _list_sources(config: Config, args: argparse.Namespace) -> int:
    sources = config.dataset_sources()
    if not sources:
        logger.error("対象JSONが1件も見つからない: %s", config.datasets.sources)
        return EXIT_FAILURE
    for path in sources:
        marker = "" if path.exists() else "  <== 存在しない"
        print(f"{path}{marker}")
    return EXIT_OK


def _list_checks(config: Config, args: argparse.Namespace) -> int:
    for check in ALL_CHECKS:
        scan = "" if not requires_scan((check,)) else "  [scan必要]"
        print(f"{check.CHECK_ID:32s} {check.CATEGORY.value:10s} {check.TITLE}{scan}")
    return EXIT_OK


def _scan(config: Config, args: argparse.Namespace) -> int:
    from .core.cache import cache_for, source_fingerprints
    from .core.measure import scan as run_scan

    adapters = open_adapters(config)
    if not adapters:
        logger.error("対象JSONが1件も読めない: %s", config.datasets.sources)
        return EXIT_FAILURE

    groups: list[FileGroup] = []
    resolvers = {}
    for adapter in adapters:
        resolvers[adapter.dataset_id] = adapter.reference_mask_path
        groups.extend(adapter.iter_files())

    cache = cache_for(config)
    try:
        cache.check_schema()
    except RuntimeError as error:
        logger.error("%s", error)
        return EXIT_FAILURE

    cache.prepare(
        {
            "fingerprint": _fingerprint(config),
            "sources": [
                {
                    "name": item.name,
                    "path": item.path,
                    "real_path": item.real_path,
                    "size": item.size,
                    "mtime_ns": item.mtime_ns,
                    "sha256": item.sha256,
                }
                for item in source_fingerprints(config)
            ],
            "n_files": len(groups),
            "jobs": args.jobs,
        },
        force=args.force,
    )

    scanned = run_scan(
        groups, config, cache, resolvers, jobs=args.jobs, limit=args.limit
    )
    logger.info("走査したファイル: %d -> %s", scanned, cache.root)
    return EXIT_OK


def _check(config: Config, args: argparse.Namespace) -> int:
    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE

    checks = selected_checks(_split(args.only), _split(args.skip))
    if not checks:
        logger.error("実行するチェックが無い（--only / --skip の指定を確認）")
        return EXIT_FAILURE
    if requires_scan(checks) and not context.has_measurements:
        logger.warning(
            "計測キャッシュが無いため、画素を必要とするチェックは結果が空になる"
            "（先に scan を実行する）"
        )

    issues: list[Issue] = []
    for check in checks:
        found = list(check.run(context))
        logger.info("%-32s %4d 件", check.CHECK_ID, len(found))
        issues.extend(found)

    out_dir = config.validation_dir / _fingerprint(config)
    meta = _meta(config, context)
    # ★ --only / --skip で一部だけ回すと issues.json は部分結果になる。
    # そのまま select すると「検出されなかった」と区別できず採否が壊れるので、
    # 何を実行したかを成果物に刻む（select がこれを見て止める）。
    executed = [check.CHECK_ID for check in checks]
    meta["checks_executed"] = executed
    meta["checks_partial"] = len(checks) < len(ALL_CHECKS)
    if meta["checks_partial"]:
        logger.warning(
            "一部のチェックだけを実行した（%d/%d）。issues.json は部分結果なので "
            "select は既定で停止する",
            len(checks),
            len(ALL_CHECKS),
        )
    # ★計測の網羅性も同じ理由で刻む。scan していない／途中で止めた状態でも
    # check は完走するので、後段が「実行したが計測が無かった」ことを
    # 判定できないと、画素依存の14チェックが「検出なし」として採否に流れる。
    measurements = Measurements(
        n_files=len(context.groups),
        n_measured=len(context.files),
        scan_required=requires_scan(checks),
    )
    meta["measurements"] = measurements.as_dict()
    if not measurements.usable:
        logger.warning(
            "計測キャッシュが足りない（%s）。画素を要するチェックが「検出なし」に "
            "なっているので、select は既定で停止する",
            measurements.shortfall(),
        )
    write_issues_json(out_dir / "issues.json", issues, config, meta)
    write_issues_csv(out_dir / "issues.csv", issues, config)
    write_meta(
        out_dir,
        fingerprint=_fingerprint(config),
        sources=[Path(s).name for s in meta.get("sources", [])],
        scale=meta.get("scale") or {},
        checks_executed=executed,
        checks_partial=bool(meta["checks_partial"]),
        measurements=measurements,
    )

    summary = summarize_issues(issues)
    logger.info("issues: %d 件 -> %s", summary["total"], out_dir)
    logger.info("  severity: %s", summary["by_severity"])
    logger.info("  status  : %s", summary["by_status"])
    return EXIT_ISSUES if summary["by_severity"].get("error") else EXIT_OK


def _check_issues_complete(
    issues_path: Path,
    args: argparse.Namespace,
    context: CheckContext | None = None,
) -> bool:
    """``issues.json`` が採否を作れる完全性を持つか確かめる。

    止める理由は2つあり、**別のリスクなので逃げ道も別**にしてある。

    1. ``check --only M06,M07`` のように一部だけ回すと ``issues.json`` は
       上書きされて部分結果になる。そのまま ``select`` すると**検出されなかった
       のか実行していないのかが区別できず**、pending が消えて採否が壊れる。
       → ``--allow-partial``
    2. ``scan`` していない／途中で止めた状態でも ``check`` は完走する。画素を
       要らないのは M06/M07/S02 だけで 17 のうち 14 は計測依存なので、計測が
       無いまま採否を作ると M01〜M05・S03/S05・D01〜D04 が全部「検出なし」に
       なり、**壊れたマスクが keep で通る**。部分実行より静かで危ない。
       → ``--allow-unmeasured``
    """
    try:
        meta = json.loads(issues_path.read_text(encoding="utf-8")).get("meta", {})
    except (OSError, json.JSONDecodeError):
        return True  # 読めなければ後段の読み込みでエラーになる

    return _gate_partial_checks(meta, args) and _gate_measurements(meta, args, context)


def _gate_partial_checks(meta: dict[str, Any], args: argparse.Namespace) -> bool:
    if not meta.get("checks_partial"):
        return True
    executed = meta.get("checks_executed") or []
    if getattr(args, "allow_partial", False):
        logger.warning(
            "--allow-partial: 部分的な issues.json（%s）のまま採否を作る",
            ",".join(executed),
        )
        return True
    logger.error(
        "issues.json は一部のチェックだけの結果（%s）。"
        "このまま select すると未実行のチェックが「検出なし」になり採否が壊れる",
        ",".join(executed),
    )
    logger.error("`segmentation-validation check` を（--only なしで）実行し直す")
    logger.error("承知の上なら `select --allow-partial`")
    return False


def _gate_measurements(
    meta: dict[str, Any],
    args: argparse.Namespace,
    context: CheckContext | None,
) -> bool:
    """計測が欠けた ``issues.json`` から採否を作らせない。"""
    measurements = Measurements.from_dict(meta.get("measurements"))
    inferred = False
    if measurements is None:
        # この形式より前に作られた issues.json。**「問題なし」と解釈してはいけない**
        # ので、その場の計測キャッシュから推定する（推定であることは明示する）。
        if context is None:
            return True
        measurements = Measurements(
            n_files=len(context.groups),
            n_measured=len(context.files),
            scan_required=True,
        )
        inferred = True

    if measurements.usable:
        return True

    if inferred:
        logger.warning(
            "issues.json に計測の記録が無い（古い版）。いまの計測キャッシュから "
            "推定した: %s",
            measurements.shortfall(),
        )
    if getattr(args, "allow_unmeasured", False):
        logger.warning(
            "--allow-unmeasured: 計測が足りない（%s）まま採否を作る。"
            "画素を要するチェックは「検出なし」になっている",
            measurements.shortfall(),
        )
        return True
    logger.error(
        "計測キャッシュが足りない（%s）。画素を要するチェックが「検出なし」に "
        "なっているので、この issues.json から採否は作れない",
        measurements.shortfall(),
    )
    logger.error("  先に: `segmentation-validation <同じ --set> scan --jobs 8`")
    logger.error("  そのうえで: `check`")
    logger.error("承知の上なら `select --allow-unmeasured`")
    return False


def _select(config: Config, args: argparse.Namespace) -> int:
    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE

    out_dir = config.validation_dir / _fingerprint(config)
    issues_path = out_dir / "issues.json"
    if not issues_path.exists():
        logger.error("issues.json が無い。先に check を実行する: %s", issues_path)
        return EXIT_FAILURE

    if not _check_issues_complete(issues_path, args, context):
        return EXIT_ISSUES

    issues = _read_issues(issues_path)

    # 自動採否は3種類。
    #   D01: 完全一致の重複 -> timestamp が新しい方を残す（同一データセット内のみ）
    #   D05: クロスデータセット重複 -> 同じくtimestampが新しい方を残す
    #        （_load_context で ctx.records 確定前に計算済み）
    #   M01-M05: 機械が確定的に「使えない」と判定した欠陥 -> exclude
    # 複数に当たった場合は exclude を優先する（merge_automatic）。
    duplicates, undecidable = build_automatic_decisions(
        context.records, context.pairs, config
    )
    broken = build_broken_decisions(issues, config)
    automatic = merge_automatic(duplicates, broken, context.cross_dataset_automatic)
    if broken:
        logger.info("機械が確定した欠陥による自動exclude: %d 件", len(broken))
    if context.cross_dataset_excluded:
        logger.info(
            "クロスデータセット重複による自動exclude: %d 件",
            len(context.cross_dataset_excluded),
        )
    auto_summary = summarize_automatic(automatic, undecidable)
    logger.info(
        "自動採否: exclude %d / keep %d（重複グループ %d）自動決定不能 %d",
        auto_summary["automatic_exclude"],
        auto_summary["automatic_keep"],
        auto_summary["duplicate_groups"],
        auto_summary["undecidable"],
    )

    # 人間の判定。無ければ review 対象は pending のままになるだけで正しく動く。
    review_path = args.review_decisions or _resolve_review_decisions(config)
    try:
        human, human_images = _read_review_decisions(review_path)
    except ValueError as error:
        logger.error("%s", error)
        return EXIT_FAILURE
    if human or human_images:
        logger.info(
            "人間の判定を読み込んだ: annotation %d 件 / 画像 %d 件",
            len(human),
            len(human_images),
        )

    # 採否マスタは「全 annotation が1行」なので対象外・クロスデータセット重複の
    # 非代表側も含める。
    all_records = (
        list(context.records)
        + list(context.out_of_scope)
        + list(context.cross_dataset_excluded)
    )
    decisions = build_decisions(
        all_records,
        issues,
        config,
        automatic,
        human,
        out_of_scope=frozenset(r.annotation_uid for r in context.out_of_scope),
    )
    assert_invariants(decisions, all_records)

    # 画像単位の採否。annotation を持たない画像（正常例187 / 未アノテーション33）は
    # selection_decisions に行を持てないので、別ファイルで採否を明示する。
    # データ管理者が承認した一括ルール（無ければ空で何も変わらない）を読み込む。
    overrides = load_image_decision_overrides(config.image_decision_overrides_path)
    if overrides:
        logger.info(
            "exclude→keep の一括ルールを読み込んだ: %d 件 (%s)",
            len(overrides),
            config.image_decision_overrides_path,
        )
    image_decisions = build_image_decisions(
        context.groups, issues, config, human_images, overrides
    )
    assert_image_invariants(image_decisions, list(context.groups))

    # 前回の select 結果（上書きする前に読む）。「今回のセッションで何件
    # 判定が進んだか」は reviewer/reviewed_at が運用上ずっと空なので
    # review_decisions.json からは追えない。前回との pending 差分だけが
    # 確実に取れる進捗指標なので、ここで比較用に読んでおく。
    previous_decisions = _read_selection(out_dir / "selection_decisions.json")
    previous_summary = (
        summarize_decisions(previous_decisions) if previous_decisions else None
    )
    previous_image_decisions = _read_image_decisions(out_dir / "image_decisions.json")
    previous_images = (
        summarize_images(previous_image_decisions) if previous_image_decisions else None
    )

    meta = _meta(config, context)
    write_selection_json(out_dir / "selection_decisions.json", decisions, meta)
    write_selection_csv(out_dir / "selection_decisions.csv", decisions)
    write_image_json(out_dir / "image_decisions.json", image_decisions, meta)
    write_image_csv(out_dir / "image_decisions.csv", image_decisions)
    # check が刻んだ checks_executed / measurements は引き継がれる（write_meta が
    # 既存を読んで重ねる）。ここでは規模と対象JSONを最新にする。
    write_meta(
        out_dir,
        fingerprint=_fingerprint(config),
        sources=[Path(s).name for s in meta.get("sources", [])],
        scale=meta.get("scale") or {},
    )

    summary = summarize_decisions(decisions)
    c = summary["cases"]
    logger.info(
        "selection_decisions: %d 行 -> %s",
        summary["total"],
        out_dir,
    )
    logger.info(
        "  規模      : 患者 %d / study %d / 画像 %d / annotation %d",
        c["patients"],
        c["studies"],
        c["images"],
        c["annotations"],
    )
    logger.info("  採否      : %s", summary["by_decision"])
    logger.info("  理由      : %s", summary["by_reason"])
    cp = summary["cases_pending"]
    logger.info(
        "  review対象: %d annotation / 未検証: %d",
        summary["review_targets"],
        summary["unverified"],
    )
    logger.info(
        "  目視の実作業量: 患者 %d / study %d / 画像 %d"
        "（annotation %d 件を含む画像を開く）",
        cp["patients"],
        cp["studies"],
        cp["images"],
        cp["annotations"],
    )
    images = summarize_images(image_decisions)
    logger.info("image_decisions: %d 行（1画像=1行）", images["total"])
    logger.info("  画像の分類: %s", images["by_class"])
    logger.info("  画像の採否: %s", images["by_decision"])
    if images["pending"]:
        logger.warning(
            "  画像 %d 枚が目視待ち（未アノテーションのビュー。側面像なら除外が必要）",
            images["pending"],
        )

    diff_message = _pending_diff_message(
        previous_summary, summary, previous_images, images
    )
    if diff_message is not None:
        logger.info("  %s", diff_message)

    if summary["ready_to_build"] and not images["pending"] and not images["uncertain"]:
        logger.info("  pending/uncertain なし。development.json を生成できる状態")
    else:
        logger.warning(
            "  pending=%d uncertain=%d が残っている（build-dataset は既定で停止する）",
            summary["pending"],
            summary["uncertain"],
        )
    return EXIT_OK


def _report(config: Config, args: argparse.Namespace) -> int:
    from .report.summary_md import write_summary

    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE
    out_dir = config.validation_dir / _fingerprint(config)
    issues_path = out_dir / "issues.json"
    if not issues_path.exists():
        logger.error("issues.json が無い。先に check を実行する: %s", issues_path)
        return EXIT_FAILURE

    issues = _read_issues(issues_path)
    decisions = _read_selection(out_dir / "selection_decisions.json")
    write_summary(
        out_dir / "summary.md",
        config,
        issues,
        decisions,
        list(context.files.values()),
        list(context.masks.values()),
        _meta(config, context),
    )
    logger.info("summary.md -> %s", out_dir / "summary.md")
    return EXIT_OK


def build_dashboard(config: Config, out_dir: Path) -> Path:
    """``out_dir`` の成果物から ``dashboard.html`` を作り直してパスを返す。

    ``gui`` サブコマンドと常駐サーバー（``serve``）の共通経路。``serve`` からは
    この関数を注入して呼ぶ（``report/serve.py`` が cli を import しないため）。

    成果物が足りなければ :class:`FileNotFoundError` を投げる。``serve`` は
    ブラウザへ 409 として返し、``gui`` はメッセージにしてから exit する。
    """
    from .checks import ALL_CHECKS
    from .report.gui import build_payload, write_dashboard

    if not (out_dir / "issues.json").exists():
        raise FileNotFoundError(f"issues.json が無い。先に check を実行する: {out_dir}")
    decisions = _read_selection(out_dir / "selection_decisions.json")
    if not decisions:
        raise FileNotFoundError(
            f"selection_decisions.json が無い。先に select を実行する: {out_dir}"
        )

    context = _load_context(config)
    if context is None:
        raise FileNotFoundError("対象JSONを読めない。list-sources で確認する")

    payload = build_payload(
        config,
        _read_issues(out_dir / "issues.json"),
        decisions,
        ALL_CHECKS,
        dict(context.masks),
        dict(context.files),
        out_dir.name,
        population=_population(context.groups),
        image_decisions=_read_image_decisions(out_dir / "image_decisions.json"),
    )
    target = out_dir / "dashboard.html"
    write_dashboard(target, payload, config)
    return target


def _gui(config: Config, args: argparse.Namespace) -> int:
    out_dir = config.validation_dir / _fingerprint(config)
    try:
        target = build_dashboard(config, out_dir)
    except FileNotFoundError as error:
        logger.error("%s", error)
        return EXIT_FAILURE
    size = target.stat().st_size / 1024
    logger.info("dashboard.html (%.0f KB) -> %s", size, target)
    logger.info("  `serve` で常駐サーバーから開くか、ブラウザで直接開く")
    return EXIT_OK


def _serve(config: Config, args: argparse.Namespace) -> int:
    from .report.serve import serve_forever

    started = serve_forever(
        config,
        renderer=build_dashboard,
        overrides=list(args.overrides),
        host=args.host or config.gui.host,
        port=args.port or config.gui.port,
        allow_refresh=config.gui.allow_refresh and not args.no_refresh,
        pinned=args.fingerprint,
    )
    return EXIT_OK if started else EXIT_FAILURE


def _build_dataset(config: Config, args: argparse.Namespace) -> int:
    from .report.selection_output import read_image_json, read_selection_json
    from .report.summary_md import write_selection_summary
    from .selection.build_dataset import (
        MergeCollision,
        MergeResult,
        build_development_payload,
        finalize_merged,
        gate_message,
        merge_into,
        new_merged_payload,
        resolve_image_duplicates,
        write_development_json,
        write_merged_json,
    )

    out_dir = config.validation_dir / _fingerprint(config)
    selection_path = out_dir / "selection_decisions.json"
    if not selection_path.exists():
        logger.error("selection_decisions.json が無い。先に select を実行する")
        return EXIT_FAILURE

    rows = read_selection_json(selection_path)
    # geometry_uid は本来データセット全体で一意という前提だが、再エクスポートにより
    # 複数データセットで同じ geometry_uid が使われている実例がある。1つの by_uid を
    # 全データセットで使い回すと、衝突時に別データセットの採否を誤って適用しかねない
    # ので、dataset_id ごとに分けて持つ。
    by_uid_per_dataset: dict[str, dict[str, str]] = {}
    for row in rows:
        by_uid_per_dataset.setdefault(row["dataset_id"], {})[row["geometry_uid"]] = row[
            "final_decision"
        ]
    pending = sum(1 for row in rows if row["final_decision"] == "pending")
    uncertain = sum(1 for row in rows if row["final_decision"] == "uncertain")

    # 画像単位の採否。annotation を持たない画像の扱いはこちらで決まる。
    image_rows = read_image_json(out_dir / "image_decisions.json")
    by_image = {row["file_uid"]: row["final_decision"] for row in image_rows}
    img_pending = sum(1 for v in by_image.values() if v == "pending")
    img_uncertain = sum(1 for v in by_image.values() if v == "uncertain")
    # annotation 側と画像側で同じ規則。判断は gate_message に一本化してある。
    gates = (
        ("pending", img_pending, "画像", args.allow_pending, args.pending_as),
        ("uncertain", img_uncertain, "画像", args.allow_uncertain, args.uncertain_as),
    )
    for kind, count, subject, allow, treat_as in gates:
        message = gate_message(kind, count, subject, allow, treat_as)
        if message:
            logger.error("%s", message)
            return EXIT_ISSUES
    # override 時は画像側にも同じ扱いを適用する。
    if args.pending_as:
        by_image = {
            k: (args.pending_as if v == "pending" else v) for k, v in by_image.items()
        }
    if args.uncertain_as:
        by_image = {
            k: (args.uncertain_as if v == "uncertain" else v)
            for k, v in by_image.items()
        }

    # override は「許可」と「扱い」の両方を明示させる。
    # 片方だけでは通さない —— 保留が黙って入る／落ちるのを防ぐため。
    gates = (
        ("pending", pending, "annotation", args.allow_pending, args.pending_as),
        ("uncertain", uncertain, "annotation", args.allow_uncertain, args.uncertain_as),
    )
    for kind, count, subject, allow, treat_as in gates:
        message = gate_message(kind, count, subject, allow, treat_as)
        if message:
            logger.error("%s", message)
            return EXIT_ISSUES

    version_tag = args.version_tag or datetime.now(timezone.utc).strftime("%Y%m%d")

    # データセットを横断した画像重複の解消。★品質上の exclude ではない
    # ―― 「同じ物理画像を別データセット側で既に採用しているため、今回の
    # development.json 生成では見送る」件だけを扱う。image_decisions.json /
    # selection_decisions.json / review_decisions.json のいずれも書き換えない
    # （build_by_image は build_development_json への入力を作るためだけの
    # ローカル変数で、build-dataset を実行するたびに毎回計算し直す）。
    dedup = resolve_image_duplicates(image_rows, rows, by_image)
    build_by_image = dict(by_image)
    if dedup.skipped_file_uids:
        for uid in dedup.skipped_file_uids:
            # build_development_json が理解できる語彙が keep/exclude しか
            # 無いための一時的な代入（file entryを丸ごと落とす動作を流用する）。
            # ログ/CSV側では「exclude」ではなく「重複によるskip」であることを
            # 明示している。
            build_by_image[uid] = Decision.EXCLUDE.value
        dup_report_path = config.development_dir / version_tag / "image_duplicates.csv"
        _write_duplicate_report(dup_report_path, dedup.report_rows)
        logger.warning(
            "データセット横断で同一画像の重複を検出し、development.json生成時に"
            "%d 件をskipした（品質上のexcludeではない。詳細: %s）",
            len(dedup.skipped_file_uids),
            dup_report_path,
        )

    # 統合JSON（学習パイプライン入力）。per-dataset の payload をそのまま
    # 移し替えるので、元JSONを2回パースしない。
    merged = None if args.no_merged else new_merged_payload()
    merge_result = MergeResult()
    per_dataset_meta: dict[str, Any] = {}
    merged_summary: dict[str, Any] | None = None

    results = []
    for source in config.dataset_sources():
        from .adapters.engineer_set import dataset_id_for

        dataset_id = dataset_id_for(source)
        target = config.development_dir / dataset_id / version_tag / "development.json"
        payload, result = build_development_payload(
            source,
            by_uid_per_dataset.get(dataset_id, {}),
            target,
            config,
            pending_as=args.pending_as,
            uncertain_as=args.uncertain_as,
            meta_extra={"fingerprint": _fingerprint(config)},
            image_decisions=build_by_image,
        )
        write_development_json(payload, target, result, source)
        results.append(result)
        if merged is not None:
            try:
                merge_into(merged, payload, dataset_id, merge_result)
            except MergeCollision as error:
                logger.error("%s", error)
                return EXIT_FAILURE
            per_dataset_meta[dataset_id] = payload["meta_development"]
        # payload は merge 側へ参照ごと移した。ここで手放してGCに任せる。
        del payload
        logger.info(
            "%s: keep %d / exclude %d / 0件になったfile %d / 画像を落とした %d -> %s",
            dataset_id,
            result.kept,
            result.excluded,
            result.files_emptied,
            result.images_dropped,
            target,
        )

    if not all(r.original_unchanged for r in results):
        logger.error("元JSONが変化した。生成物を信用しないこと")
        return EXIT_FAILURE

    if merged is not None:
        finalize_merged(
            merged,
            merge_result,
            per_dataset_meta,
            meta_extra={"fingerprint": _fingerprint(config)},
        )
        merged_path = config.development_dir / version_tag / args.merged_name
        write_merged_json(merged, merged_path)
        totals = merge_result.as_totals()
        merged_summary = {
            "path": merged_path,
            "totals": totals,
            "conflicts": merge_result.conflicts,
        }
        logger.info(
            "統合JSON: データセット %d / 施設 %d / study %d / series %d / "
            "画像 %d / annotation %d -> %s",
            totals["datasets"],
            totals["institutions"],
            totals["studies"],
            totals["series"],
            totals["files"],
            totals["annotations"],
            merged_path,
        )
        if merge_result.conflicts:
            # 値を選ばず両方を記録してある。どちらが正かは元データ側の問題。
            logger.warning(
                "元データ間で属性が食い違う箇所が %d 件ある"
                "（meta_development.conflicts に両方の値を記録した）",
                len(merge_result.conflicts),
            )
            for conflict in merge_result.conflicts[:10]:
                logger.warning(
                    "  - %s %s: %s",
                    conflict["path"],
                    conflict["field"],
                    conflict["values"],
                )
            if args.strict_conflicts:
                logger.error("--strict-conflicts: 食い違いを解消してから再実行する")
                return EXIT_ISSUES

    issues = _read_issues(out_dir / "issues.json")
    decisions = _read_selection(selection_path)
    summary_path = config.development_dir / version_tag / "selection_summary.md"
    overrides = {}
    if args.pending_as:
        overrides["pending_as"] = args.pending_as
    if args.uncertain_as:
        overrides["uncertain_as"] = args.uncertain_as
    context = _load_context(config)
    image_rows_full = _read_image_decisions(out_dir / "image_decisions.json")
    write_selection_summary(
        summary_path,
        config,
        decisions,
        issues,
        image_decisions=image_rows_full,
        empty_files=sum(r.files_emptied for r in results),
        images_dropped=sum(r.images_dropped for r in results),
        meta={
            "fingerprint": _fingerprint(config),
            "overrides": overrides,
            "population": _population(context.groups) if context else {},
        },
        merged=merged_summary,
    )
    logger.info("selection_summary.md -> %s", summary_path)
    return EXIT_OK


def _write_duplicate_report(path: Path, rows: list[dict[str, Any]]) -> None:
    """データセット横断の画像重複解消結果をCSVへ書く（監査用）。

    ``resolve_image_duplicates`` の戻り値をそのまま書き出すだけ。呼ぶたびに
    上書きする（``image_decisions.json`` 等と違って累積させる必要は無い —
    今回の `build-dataset` 実行1回ぶんの結果だけを表す）。
    """
    import csv

    from .selection.build_dataset import DUPLICATE_REPORT_COLUMNS

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DUPLICATE_REPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _lpdata_common_parser() -> argparse.ArgumentParser:
    """``export-lpdata`` と ``export-lpdata-ofc`` が共有するオプション。

    ``parents=`` で共有するのは、20個超のフラグを2か所で二重管理すると
    必ず片方だけが更新されて drift するため（``--path-style`` や
    ``--on-missing-dicom`` は両方で同じ意味でなければならない）。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="属性の型と意味の正典となるテンプレートYAML"
        "（既定は config の lpdata_export.template_path）",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="出力するデータセットJSON"
    )
    parser.add_argument(
        "--image-output-dir", type=Path, default=None, help="DICOM変換PNGの出力先"
    )
    parser.add_argument(
        "--mask-output-dir",
        type=Path,
        default=None,
        help="結合気胸マスクの出力先。**入力ごとに別のディレクトリにすること**"
        "（ファイル名が <sample_id>.png なので、共有すると既存のGTマスクを"
        "上書きする）",
    )
    parser.add_argument(
        "--artifact-prefix",
        default="",
        help="summary / manifest / 計測キャッシュのファイル名に付ける接頭辞。"
        "同じディレクトリへ2回書き出すときに潰し合わないために使う（例 ofc_）",
    )
    parser.add_argument(
        "--path-style",
        choices=("relative", "absolute"),
        default="relative",
        help="JSONに書くパスの表記。relative は出力JSONの親を基準にする"
        "（基準の外にあるものは絶対のまま）",
    )
    parser.add_argument(
        "--image-mode",
        choices=("convert", "planned", "none"),
        default="convert",
        help="convert=DICOMを16bit PNGへ変換して実ファイルを参照 / "
        "planned=変換せず予定パスだけ記録 / none=image_file を null にする",
    )
    parser.add_argument(
        "--mask-mode",
        choices=("generate", "planned", "none"),
        default="planned",
        help="generate=結合マスクPNGを書き出す / planned=予定パスだけ記録（既定） / "
        "none=pixel_array を null にする",
    )
    parser.add_argument(
        "--on-missing-dicom",
        choices=("skip", "error"),
        default="skip",
        help="DICOMが無いとき。skip は image_file を null にして続行、error は停止。"
        "どちらでも空画像は作らない",
    )
    parser.add_argument(
        "--no-measure",
        action="store_true",
        help="画素を読まず導出値を全てnullにする（下見用。学習には使えない）",
    )
    parser.add_argument("--jobs", type=int, default=8, help="並列数")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="先頭N件だけ処理する（--report-status で絞った**後**の先頭N件）",
    )
    parser.add_argument(
        "--only-dataset",
        action="append",
        default=[],
        metavar="DATASET_ID",
        help="対象データセットを絞る（繰り返し可）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="計測キャッシュを捨て、既存のPNGも作り直す",
    )
    parser.add_argument("--dataset-name", default=None, help="meta.dataset_name")
    parser.add_argument("--dataset-id", default=None, help="meta.dataset_id")
    parser.add_argument("--owner", default=None, help="meta.owner")
    return parser


def _export_lpdata(config: Config, args: argparse.Namespace) -> int:
    """統合JSONを lp-data 標準形式へ書き出す。

    判断も処理もここには置かない（Notebook が同じことを再実装しないで済むように、
    正本は ``lpdata_export`` パッケージ側に置いてある）。ここは引数を
    ``ExportOptions`` に組み替えるだけ。
    """
    merged = args.merged or _latest_merged(config)
    if merged is None:
        logger.error(
            "統合JSONが見つからない。"
            "先に build-dataset を実行するか --merged で指定する"
        )
        return EXIT_FAILURE

    # 既定の置き場は統合JSONのバージョンタグごと。3つとも個別に上書きできる。
    out_dir = config.lpdata_dir / merged.parent.name
    return _run_export_lpdata(config, args, source=merged, default_out_dir=out_dir)


def _export_lpdata_ofc(config: Config, args: argparse.Namespace) -> int:
    """構造化読影レポート付き engineer-set JSON を lp-data 形式へ書き出す。

    ``export-lpdata`` と同じエンジンを通し、違いは2つだけ。

    - ``report_labels`` でラベルを補強する（``label_source``）
    - 取り込む ``pneumothorax_status`` を絞る（``--report-status``）
    """
    from .lpdata_export.options import LABEL_SOURCE_REPORT

    source: Path = args.source
    if not source.is_file():
        logger.error("入力の engineer-set JSON が無い: %s", source)
        return EXIT_FAILURE

    settings = config.lpdata_export
    statuses = tuple(
        args.report_status
        if args.report_status is not None
        else settings.report_label_statuses
    )
    # 既定の置き場は入力ファイル名のタグではなく実行日。development 側の
    # 出力と同じディレクトリに混ざらないよう --out の明示を促す。
    return _run_export_lpdata(
        config,
        args,
        source=source,
        default_out_dir=config.lpdata_dir / date.today().strftime("%Y%m%d"),
        label_source=LABEL_SOURCE_REPORT,
        report_statuses=statuses,
        default_out_name="ofc_report.lpdata.json",
    )


def _run_export_lpdata(
    config: Config,
    args: argparse.Namespace,
    *,
    source: Path,
    default_out_dir: Path,
    label_source: str = "annotations",
    report_statuses: tuple[str, ...] = (),
    default_out_name: str = "chest_metry_pi6_pneumothorax.json",
) -> int:
    """``ExportOptions`` を組み立てて ``export_lpdata`` を呼ぶだけ。

    判断も処理もここには置かない（Notebook が同じことを再実装しないで済むように、
    正本は ``lpdata_export`` パッケージ側に置いてある）。
    """
    from .lpdata_export import ExportError, ExportOptions, export_lpdata
    from .lpdata_export.images import MissingDicomError
    from .lpdata_export.template import TemplateDriftError

    settings = config.lpdata_export
    out = args.out or default_out_dir / default_out_name
    options = ExportOptions(
        merged_path=source,
        template_path=args.template or config.resolve(settings.template_path),
        out_path=out,
        image_output_dir=args.image_output_dir or out.parent / settings.image_dirname,
        mask_output_dir=args.mask_output_dir or out.parent / settings.mask_dirname,
        dataset_name=args.dataset_name,
        dataset_id=args.dataset_id,
        owner=args.owner,
        image_mode=args.image_mode,
        mask_mode=args.mask_mode,
        on_missing_dicom=args.on_missing_dicom,
        path_style=args.path_style,
        measure=not args.no_measure,
        label_source=label_source,
        report_statuses=report_statuses,
        jobs=args.jobs,
        limit=args.limit,
        only_datasets=tuple(args.only_dataset),
        force=args.force,
        artifact_prefix=args.artifact_prefix,
    )

    try:
        result = export_lpdata(config, options)
    except (ExportError, TemplateDriftError, MissingDicomError, ValueError) as error:
        logger.error("%s", error)
        return EXIT_FAILURE

    logger.info(
        "サンプル %d / %s",
        result.samples,
        " ".join(f"{k}={v}" for k, v in sorted(result.status_counts.items())),
    )
    if result.violations:
        logger.error(
            "不変条件の違反が %d 件ある（詳細は %s）",
            len(result.violations),
            result.artifacts.get("summary"),
        )
        return EXIT_ISSUES
    return EXIT_OK


def _merge_lpdata(config: Config, args: argparse.Namespace) -> int:
    """2つの lp-data データセットJSONを統合する。

    ``--dry-run`` なら統合JSONを書かず、件数と裁定結果だけを出す
    （**統合前に人が確認するための経路**）。
    """
    from .lpdata_export.merge import MergeError, MergeOptions, merge_lpdata
    from .lpdata_export.template import TemplateDriftError

    options = MergeOptions(
        primary_path=args.primary,
        secondary_path=args.secondary,
        out_path=args.out,
        template_path=args.template,
        dataset_name=args.dataset_name,
        dataset_id=args.dataset_id,
        owner=args.owner,
        dry_run=args.dry_run,
        no_enrich=args.no_enrich,
    )
    try:
        report = merge_lpdata(options)
    except (MergeError, TemplateDriftError, ValueError) as error:
        logger.error("%s", error)
        return EXIT_FAILURE

    logger.info(
        "重複 %d / 補強 %d / 追加 %d → 統合後 %d サンプル（詳細は %s）",
        report.duplicates,
        report.enriched,
        report.added_from_secondary,
        report.merged_samples,
        report.artifacts.get("summary"),
    )
    if report.mask_conflicts:
        logger.warning(
            "気胸マスクの食い違いが %d 件ある（primary を採用した。%s を見ること）",
            len(report.mask_conflicts),
            report.artifacts.get("summary"),
        )
    if report.violations or report.identity_conflicts:
        logger.error(
            "不変条件の違反 %d 件 / 同一性の食い違い %d 件",
            len(report.violations),
            len(report.identity_conflicts),
        )
        return EXIT_ISSUES
    return EXIT_OK


def _latest_merged(config: Config) -> Path | None:
    """``development_merged.json`` を新しいバージョンタグから探す。

    ``build-dataset --version-tag`` は既定で ``YYYYMMDD`` なので辞書順の降順が
    新しい順になる。手で付けたタグが混ざっても mtime で決めない —— 再実行で
    順序が入れ替わると、どの世代を書き出したのか追えなくなる。
    """
    if not config.development_dir.is_dir():
        return None
    for directory in sorted(config.development_dir.iterdir(), reverse=True):
        candidate = directory / "development_merged.json"
        if candidate.is_file():
            return candidate
    return None


#: サブコマンド名 → ハンドラ。``build_parser`` の add_parser と対にする
#: （片方だけ足すと実行時に「未知のサブコマンド」で落ちるので、
#: ``tests/test_cli.py`` が両者の一致を検査する）。
HANDLERS: dict[str, Any] = {}


def _review(config: Config, args: argparse.Namespace) -> int:
    handlers = {
        "build": _review_build,
        "launch": _review_launch,
        "status": _review_status,
        "export": _review_export,
        "flag-report": _review_flag_report,
        "precision": _review_precision,
        "import": _review_import,
        "migrate-decisions": _review_migrate_decisions,
    }
    return handlers[args.review_command](config, args)


def _review_dir(config: Config) -> Path:
    return config.validation_dir / _fingerprint(config) / "review"


def _check_unexported(config: Config, args: argparse.Namespace) -> bool:
    """export していない人間の判定が DB に残っていないか確かめる。

    ``review build`` は FiftyOne dataset を ``overwrite=True`` で作り直すので、
    **``review export`` していない判定は黙って消える**。閾値を変えて再検出する
    運用があるので、ここで止めないと目視の成果を失う。
    """
    review_dir = _review_dir(config)
    manifest_path = review_dir / "review_manifest.json"
    if not manifest_path.exists():
        # ★「初回だから守るものは無い」と決めつけてはいけない。
        # manifest が無い理由は2つあり、後者では DB に前の構成の判定が残る。
        #   (a) 本当に初回
        #   (b) fingerprint が変わった（データセットを足した／symlink が
        #       張り替わった）。FiftyOne dataset 名は fingerprint 非依存の
        #       単一名なので、DB の中身は前の構成のまま
        # (b) で素通りすると overwrite=True が目視の成果を消す。DB を直接見て
        # 人間の判定（reviewer が入っているもの）があれば止める。
        return _check_unexported_without_manifest(config, args)
    try:
        from .review.export_decisions import unexported_human_decisions
    except ImportError:
        return True  # fiftyone が無ければ dataset も無い

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 比較先は正本。fingerprint 配下のスナップショットは書き出しの写しであって、
    # 「export 済みか」の判断基準にはならない。
    lost = unexported_human_decisions(config, manifest, config.review_decisions_path)
    if not lost:
        return True

    if args.discard_unexported:
        logger.warning(
            "--discard-unexported: export していない判定 %d 件を捨てて作り直す",
            len(lost),
        )
        return True

    logger.error(
        "FiftyOne DB に review export していない人間の判定が %d 件ある。"
        "review build は dataset を作り直すのでこれらは消える",
        len(lost),
    )
    for row in lost[:10]:
        logger.error("  %s %s -> %s", row["kind"], row["key"], row["decision"])
    if len(lost) > 10:
        logger.error("  ... 他 %d 件", len(lost) - 10)
    logger.error("先に `segmentation-validation review export` を実行する")
    logger.error("捨ててよいなら `review build --discard-unexported`")
    return False


def _check_unexported_without_manifest(
    config: Config, args: argparse.Namespace
) -> bool:
    """manifest が無いときに、DB の人間の判定だけを見てゲートする。

    基準線（manifest）が無いので「機械の判定から変わったか」は判定できない。
    ``reviewer`` が入っているものだけを人間の判定として数える —— 取りこぼしは
    あるが、**あるものを「無い」と言わない**方向に倒す。
    """
    try:
        from .review.export_decisions import human_decisions_in_db
    except ImportError:
        return True  # fiftyone が無ければ dataset も無い

    try:
        found = human_decisions_in_db(config)
    except RuntimeError:
        return True  # dataset が無い。本当の初回

    if not found:
        return True

    if args.discard_unexported:
        logger.warning(
            "--discard-unexported: export していない判定 %d 件を捨てて作り直す",
            len(found),
        )
        return True

    logger.error(
        "FiftyOne dataset に人間の判定が %d 件あるが、この出力先には "
        "review_manifest.json が無い（fingerprint が変わった直後がこの状態）。"
        "このまま review build すると DB を作り直して判定を失う",
        len(found),
    )
    logger.error("  先に: `segmentation-validation review export`")
    logger.error(
        "  （判定は正本 %s へ入るので、fingerprint が変わっても手動コピーは要らない）",
        config.review_decisions_path,
    )
    logger.error("承知の上なら `review build --discard-unexported`")
    return False


def _review_build(config: Config, args: argparse.Namespace) -> int:
    from .review.export_assets import (
        export_assets,
        select_review_groups,
        select_spot_check_groups,
    )
    from .review.manifest import build_manifest, write_manifest

    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE
    out_dir = config.validation_dir / _fingerprint(config)
    if not (out_dir / "selection_decisions.json").exists():
        logger.error("selection_decisions.json が無い。先に select を実行する")
        return EXIT_FAILURE

    # ★ manifest を上書きする前に確認する。``review build`` は dataset を
    # 作り直すので、export していない人間の判定は消える。
    # ツール障害ではなく「ゲートで止めた」なので build-dataset と同じ exit 1。
    if not _check_unexported(config, args):
        return EXIT_ISSUES

    issues = _read_issues(out_dir / "issues.json")
    decisions = _read_selection(out_dir / "selection_decisions.json")
    image_decisions = _read_image_decisions(out_dir / "image_decisions.json")

    # 目視対象だけを書き出す。DICOMの全画素読みは1枚1〜3秒かかる。
    pending_uids = {
        d.geometry_uid for d in decisions if d.final_decision is Decision.PENDING
    }
    pending_images = {
        d.file_uid for d in image_decisions if d.final_decision is Decision.PENDING
    }
    # 全画像を載せるかは config でも指定できる（`review.export_all_files`）。
    # keep になった画像も FiftyOne で見たいときに使う。`--all` は config を上書きする。
    include_all = args.all_images or config.review.export_all_files
    source = (
        "--all"
        if args.all_images
        else "review.export_all_files=true"
        if config.review.export_all_files
        else ""
    )
    groups = select_review_groups(
        context.groups, pending_uids, pending_images, include_all=include_all
    )
    logger.info(
        "目視対象: annotation %d 件 / 画像 %d 枚 -> 書き出す画像 %d 枚%s",
        len(pending_uids),
        len(pending_images),
        len(groups),
        f"（全画像: {source}）" if include_all else "",
    )
    if include_all:
        # 未書き出しの枚数で警告する。2回目以降は既存をスキップするので速い。
        logger.warning(
            "全 %d 画像を書き出す。DICOMの全画素読みが1枚1〜3秒かかるので"
            "初回は25〜30分・約2GB になる（既存アセットはスキップする）",
            len(groups),
        )

    # 未アノテーション画像のデータセット別スポットチェック。採否は変えず、
    # 自動判定（image_decisions.py）が妥当かの確認用に追加でサンプルする。
    spot_check_file_uids: frozenset[str] = frozenset()
    if args.sample_unannotated is not None:
        spot_check_groups = select_spot_check_groups(
            context.groups, image_decisions, args.sample_unannotated
        )
        existing_uids = {g.file_uid for g in groups}
        added = [g for g in spot_check_groups if g.file_uid not in existing_uids]
        groups = list(groups) + added
        spot_check_file_uids = frozenset(g.file_uid for g in spot_check_groups)
        logger.info(
            "スポットチェック: 未アノテーション画像 %d 枚を追加"
            "（bucketあたり最大 %d 件）",
            len(added),
            args.sample_unannotated,
        )

    adapters = {a.dataset_id: a.reference_mask_path for a in open_adapters(config)}
    review_dir = _review_dir(config)
    assets = export_assets(
        groups, config, review_dir, adapters, force=args.force, jobs=args.jobs
    )

    manifest = build_manifest(
        groups,
        assets,
        decisions,
        image_decisions,
        issues,
        config,
        spot_check_file_uids=spot_check_file_uids,
    )
    write_manifest(review_dir / "review_manifest.json", manifest)
    logger.info(
        "review_manifest.json: 画像 %d / annotation %d"
        "（目視待ち annotation %d / 画像 %d）",
        manifest["meta"]["n_images"],
        manifest["meta"]["n_annotations"],
        manifest["meta"]["n_pending_annotations"],
        manifest["meta"]["n_pending_images"],
    )
    if args.assets_only:
        logger.info("--assets-only なので FiftyOne dataset は作らない")
        return EXIT_OK

    try:
        from .review.fiftyone_builder import build_dataset
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE

    build_dataset(manifest, config)
    logger.info("次: `segmentation-validation review launch` で App を起動する")
    return EXIT_OK


def _review_launch(config: Config, args: argparse.Namespace) -> int:
    try:
        from .review.fiftyone_builder import launch_app
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE
    launch_app(config)
    return EXIT_OK


def _review_status(config: Config, args: argparse.Namespace) -> int:
    # 正本の状況は fiftyone が無くても出せる（DB より先に出す）。
    _report_store_status(config)

    try:
        from .review.fiftyone_builder import dataset_summary
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE

    summary = dataset_summary(config)
    if not summary:
        logger.error(
            "FiftyOne dataset '%s' が無い。`review build` を実行する",
            config.review.dataset_name,
        )
        return EXIT_FAILURE
    logger.info("dataset '%s'", summary["name"])
    logger.info(
        "  Sample %d / Detection %d", summary["n_samples"], summary["n_detections"]
    )
    logger.info("  annotation の判定: %s", summary["annotation_status"])
    logger.info("  画像の判定      : %s", summary["image_status"])
    auto = {k: v for k, v in summary["label_tags"].items() if k.startswith("auto:")}
    logger.info("  auto: タグ      : %s", auto)
    # 理由の内訳。exclude したのに理由を入れていないものを見つけるために出す。
    from .review.review_schema import REASON_JA, REASON_PREFIX

    reasons = {
        REASON_JA.get(k[len(REASON_PREFIX) :], k[len(REASON_PREFIX) :]): v
        for k, v in sorted(summary["label_tags"].items())
        if k.startswith(REASON_PREFIX)
    }
    logger.info("  理由            : %s", reasons or "（まだ無い）")
    excluded = summary["annotation_status"].get("exclude", 0)
    if excluded and sum(reasons.values()) < excluded:
        logger.warning(
            "  exclude %d 件に対して理由は %d 件。理由の入力漏れがある"
            "（保存ビュー 10-reasoned-decisions で入っている分を確認できる）",
            excluded,
            sum(reasons.values()),
        )
    pending = summary["annotation_status"].get("pending", 0)
    pending += summary["image_status"].get("pending", 0)
    if pending:
        logger.warning("  目視未完了 %d 件（pending = 0 が完了条件）", pending)
    else:
        logger.info("  目視完了")
    return EXIT_OK


def _report_store_status(config: Config) -> None:
    """目視判定の正本の件数と、現在の構成に当たらない行（孤児）を表示する。

    孤児は**消さない**。データセットを一時的に外しただけかもしれないし、
    人間の判定は再生成できないので、こちらから捨てる判断はしない。
    ただし「元JSONのファイル名が日時以外の部分で変わった」場合はここに現れる
    （dataset_id が変わって引き当てられなくなる唯一のケース）ので、気づける
    ようにしておく。
    """
    from .review import decision_store as store

    path = config.review_decisions_path
    if not path.exists():
        logger.warning(
            "目視判定の正本が無い: %s（`review migrate-decisions` で作る）", path
        )
        return
    try:
        rows = store.load_rows(path)
    except store.DecisionStoreError as error:
        logger.error("%s", error)
        return

    counts = {
        "annotations": sum(1 for r in rows if r["kind"] == store.KIND_ANNOTATION),
        "images": sum(1 for r in rows if r["kind"] == store.KIND_IMAGE),
    }
    logger.info(
        "正本 %s: annotation %d / 画像 %d",
        path,
        counts["annotations"],
        counts["images"],
    )

    context = _load_context(config)
    if context is None:
        return
    known = {
        store.KIND_ANNOTATION: {r.annotation_uid for r in context.records}
        | {r.annotation_uid for r in context.out_of_scope}
        | {r.annotation_uid for r in context.cross_dataset_excluded},
        store.KIND_IMAGE: {g.stable_file_uid for g in context.groups},
    }
    stray = store.orphans(rows, known)
    if not stray:
        logger.info("  すべて現在の構成に対応している")
        return
    logger.warning(
        "  現在の構成に当たらない判定が %d 件ある（消さずに残す）", len(stray)
    )
    for row in stray[:5]:
        logger.warning("    - %s %s", row["kind"], store.stable_uid(row))


def _review_export(config: Config, args: argparse.Namespace) -> int:
    try:
        from .review.export_decisions import collect_decisions, write_decisions
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE

    from .review.manifest import read_manifest

    manifest_path = _review_dir(config) / "review_manifest.json"
    manifest = read_manifest(manifest_path) if manifest_path.exists() else None
    if manifest is None:
        logger.warning(
            "review_manifest.json が無いので reviewer の有無だけで人間の判定を判別する"
        )
    try:
        rows = collect_decisions(config, manifest)
    except RuntimeError as error:
        logger.error("%s", error)
        return EXIT_FAILURE

    from .review import decision_store as store

    # 正本（fingerprint 非依存・git管理）へマージする。
    target = config.review_decisions_path
    try:
        all_rows = write_decisions(target, rows, config)
    except store.DecisionStoreError as error:
        logger.error("%s", error)
        return EXIT_ISSUES
    logger.info(
        "review_decisions: 今回 annotation %d / 画像 %d -> %s（累計 %d 件）",
        sum(1 for r in rows if r["kind"] == "annotation"),
        sum(1 for r in rows if r["kind"] == "image"),
        target,
        len(all_rows),
    )

    # fingerprint 配下にはスナップショットを残す。正本ではないが、その時点の
    # 構成で何が判定済みだったかを後から確かめられるようにしておく（既存の
    # review_decisions.csv を見る手順やダッシュボード表示も壊さない）。
    snapshot_path = _review_dir(config) / "review_decisions.json"
    store.snapshot(all_rows, snapshot_path, config.review.dataset_name)
    logger.info("スナップショット: %s", snapshot_path)
    logger.info("次: `segmentation-validation select` で採否へ反映する")
    logger.info("正本が更新された。`git add %s` して commit すること", target)
    return EXIT_OK


def _review_flag_report(config: Config, args: argparse.Namespace) -> int:
    """flag:needs_report の付いた項目を flagged_for_report.csv へ書き出す。

    採否とは無関係のレポート専用の出力で、``review_decisions.json`` とは
    別ファイル。マスクの実ファイルはコピーせず、パス（path_mask/
    path_original_mask）だけをこのCSVに書き足していく運用にするため、
    出力先を fingerprint 直下（review/ の外）に置く。
    """
    try:
        from .review.export_decisions import collect_flagged, write_flagged_report
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE

    try:
        rows = collect_flagged(config)
    except RuntimeError as error:
        logger.error("%s", error)
        return EXIT_FAILURE

    target = config.validation_dir / _fingerprint(config) / "flagged_for_report.csv"
    added = write_flagged_report(target, rows)
    logger.info(
        "flag:needs_report: annotation %d / 画像 %d（今回新規 %d 件）-> %s",
        sum(1 for r in rows if r["kind"] == "annotation"),
        sum(1 for r in rows if r["kind"] == "image"),
        added,
        target,
    )
    return EXIT_OK


def _review_precision(config: Config, args: argparse.Namespace) -> int:
    """auto: × review: のクロス集計。fiftyone は使わない。"""
    from .report.precision import (
        compute_precision,
        read_verdicts,
        summarize,
        write_precision,
    )

    out_dir = config.validation_dir / _fingerprint(config)
    if not (out_dir / "issues.json").exists():
        logger.error("issues.json が無い。先に check を実行する")
        return EXIT_FAILURE

    issues = _read_issues(out_dir / "issues.json")
    # 目視判定の正本（fingerprint 非依存）から読む。
    verdicts = read_verdicts(_resolve_review_decisions(config))
    results = compute_precision(issues, verdicts, config)

    target = out_dir / "precision.md"
    write_precision(target, results, {"fingerprint": _fingerprint(config)})
    stats = summarize(results)
    logger.info(
        "Precision: 検出 %d / 目視済 %d（exclude %d / keep %d / uncertain %d）-> %s",
        stats["detected"],
        stats["reviewed"],
        stats["excluded"],
        stats["kept"],
        stats["uncertain"],
        target,
    )
    for r in results:
        if r.precision is not None:
            logger.info(
                "  %-36s 検出 %3d / 目視済 %3d -> Precision %.2f",
                r.check_id,
                r.detected,
                r.reviewed,
                r.precision,
            )
    if not stats["reviewed"]:
        logger.warning("判定が1件も無いので Precision は計算できない")
    return EXIT_OK


def _review_import(config: Config, args: argparse.Namespace) -> int:
    try:
        from .review.import_decisions import import_decisions
    except ImportError:
        logger.error("fiftyone が無い。`uv sync --group review` を実行する")
        return EXIT_FAILURE

    path = args.path or _resolve_review_decisions(config)
    if not path.exists():
        logger.error("判定ファイルが無い: %s", path)
        return EXIT_FAILURE
    try:
        import_decisions(path, config)
    except RuntimeError as error:
        logger.error("%s", error)
        return EXIT_FAILURE
    return EXIT_OK


def _review_migrate_decisions(config: Config, args: argparse.Namespace) -> int:
    """fingerprint 配下に散った目視判定を Git 管理の正本へ集める。

    fingerprint は対象JSONの mtime/sha256 から決まるので、データセットを
    1本足すだけで変わる。そのたびに目視結果が新しい空ディレクトリを指して
    しまい、手動 ``cp`` で引き継ぐ運用になっていた。これを一度きりの移行で
    ``review/review_decisions.json`` に集約する。

    画像側のキーは ``file_uid``（source_json 込み）から
    ``dataset_id + image_key`` へ昇格する。``dataset_id_for()`` に通すだけの
    機械的な変換なので情報は失われない。
    """
    from .review import decision_store as store

    target = config.review_decisions_path
    sources = sorted(config.validation_dir.glob("*/review/review_decisions.json"))
    sources = [path for path in sources if path.resolve() != target.resolve()]
    if not sources:
        logger.error(
            "移行元が無い: %s", config.validation_dir / "*/review/review_decisions.json"
        )
        return EXIT_FAILURE

    merged: list[dict[str, Any]] = store.load_rows(target) if target.exists() else []
    if merged:
        logger.info("既存の正本 %d 件に重ねる: %s", len(merged), target)

    ignored = 0
    for path in sources:
        try:
            rows = store.load_rows(path)
        except store.DecisionStoreError as error:
            logger.error("%s", error)
            return EXIT_FAILURE
        # dataset_id を持たない annotation 行は突合に使えない。誤って別データ
        # セットへ適用するくらいなら目視漏れの方がまし（select と同じ方針）。
        usable = [
            row
            for row in rows
            if row["kind"] != store.KIND_ANNOTATION or row.get("dataset_id")
        ]
        ignored += len(rows) - len(usable)
        merged, conflicts = store.merge_rows(merged, usable)
        unresolved = [c for c in conflicts if not c.resolved_by]
        if unresolved:
            logger.error(
                "%s の判定が既存と食い違い、reviewed_at でも決められない（%d 件）",
                path,
                len(unresolved),
            )
            for conflict in unresolved[:10]:
                logger.error("  - %s", conflict.describe())
            logger.error("どちらを正とするか決めてから再実行する")
            return EXIT_ISSUES
        for conflict in conflicts:
            logger.warning("reviewed_at で解決: %s", conflict.describe())
        logger.info(
            "%s: annotation %d / 画像 %d",
            path.parent.parent.name,
            sum(1 for r in usable if r["kind"] == store.KIND_ANNOTATION),
            sum(1 for r in usable if r["kind"] == store.KIND_IMAGE),
        )

    n_annotations = sum(1 for r in merged if r["kind"] == store.KIND_ANNOTATION)
    n_images = sum(1 for r in merged if r["kind"] == store.KIND_IMAGE)
    if ignored:
        logger.warning(
            "dataset_id を持たない旧形式の annotation 行 %d 件を読み飛ばした", ignored
        )

    if args.dry_run:
        logger.info(
            "--dry-run: 書き込まない。移行後は annotation %d / 画像 %d になる",
            n_annotations,
            n_images,
        )
        return EXIT_OK

    try:
        store.write_rows(target, merged, config.review.dataset_name)
    except store.DecisionStoreError as error:
        logger.error("%s", error)
        return EXIT_ISSUES
    logger.info(
        "正本へ移行: annotation %d / 画像 %d -> %s", n_annotations, n_images, target
    )
    logger.info(
        "次: `git add %s %s` して commit する",
        target.relative_to(config.project_root),
        config.image_decision_overrides_path.relative_to(config.project_root),
    )
    return EXIT_OK


# ------------------------------------------------------------------ helpers


def _load_context(config: Config) -> CheckContext | None:
    adapters = open_adapters(config)
    if not adapters:
        logger.error("対象JSONが1件も読めない: %s", config.datasets.sources)
        return None

    records: list[AnnotationRecord] = []
    groups: list[FileGroup] = []
    invariants: dict[str, dict[str, str]] = {}
    for adapter in adapters:
        invariants[adapter.dataset_id] = dict(adapter.path_invariants())
        for group in adapter.iter_files():
            groups.append(group)
            records.extend(group.records)

    # 検証対象の絞り込み。既定は空 = 全病変。
    keys, names = label_selectors(config.validation.target_labels)
    in_scope = [r for r in records if matches_target(r.labels, keys, names)]
    out_scope = [r for r in records if not matches_target(r.labels, keys, names)]
    if out_scope:
        logger.info(
            "検証対象 %s -> 対象 %d / 対象外 %d annotation"
            "（対象外はチェックを走らせない）",
            config.validation.target_labels,
            len(in_scope),
            len(out_scope),
        )

    patients = {(r.dataset_id, r.patient_id) for r in records}
    studies = {(r.dataset_id, r.study) for r in records}
    annotated = {r.file_uid for r in records}
    logger.info(
        "対象: %d データセット / 患者 %d / study %d / 画像 %d"
        "（うち annotation あり %d） / annotation %d",
        len(adapters),
        len(patients),
        len(studies),
        len(groups),
        len(annotated),
        len(records),
    )
    masks, files, pairs = _load_measurements(config)
    if files:
        logger.info(
            "計測キャッシュ: %d ファイル / %d マスク / %d ペア",
            len(files),
            len(masks),
            len(pairs),
        )

    # クロスデータセット重複（D05）の事前検出。ctx.records が確定する前に
    # 非代表側を抜いておく必要がある（M01-M09/D01-D04 から隠して check を
    # スキップするため）。out_of_scope の判定より後（ラベルで対象外に
    # なったものは D05 の対象にしない）。
    from .checks.duplicate.d05_cross_dataset_duplicate import (
        detect as detect_cross_dataset,
    )

    cross_result = detect_cross_dataset(in_scope, masks)
    if cross_result.excluded_annotation_uids:
        logger.info(
            "クロスデータセット重複により自動除外: %d 件",
            len(cross_result.excluded_annotation_uids),
        )
    final_records = [
        r
        for r in in_scope
        if r.annotation_uid not in cross_result.excluded_annotation_uids
    ]
    cross_excluded = [
        r for r in in_scope if r.annotation_uid in cross_result.excluded_annotation_uids
    ]

    return CheckContext(
        config=config,
        records=tuple(final_records),
        out_of_scope=tuple(out_scope),
        cross_dataset_excluded=tuple(cross_excluded),
        groups=tuple(groups),
        path_invariants=invariants,
        masks=masks,
        files=files,
        pairs=tuple(pairs),
        cross_dataset_issues=cross_result.issues,
        cross_dataset_automatic=cross_result.automatic,
    )


def _load_measurements(config: Config):
    """計測キャッシュを読む。無ければ空を返す（JSONのみのチェックは動く）。"""
    from .core.cache import cache_for
    from .core.measure import (
        FileMeasurement,
        MaskMeasurement,
        PairMeasurement,
        rows_to_measurements,
    )

    cache = cache_for(config)
    if not cache.files_path.exists():
        return {}, {}, []
    try:
        file_rows, mask_rows, pair_rows = cache.load()
    except RuntimeError as error:
        logger.warning("計測キャッシュを使えない: %s", error)
        return {}, {}, []

    masks = {
        (m.dataset_id, m.geometry_uid, m.role): m
        for m in rows_to_measurements(mask_rows, MaskMeasurement)
    }
    files = {f.file_uid: f for f in rows_to_measurements(file_rows, FileMeasurement)}
    pairs = rows_to_measurements(pair_rows, PairMeasurement)
    return masks, files, pairs


def _fingerprint(config: Config) -> str:
    """出力先を決めるキー。

    ``version_id`` は3ファイルとも同じなので単独では使えない。
    Phase 5 で計測キャッシュと同じフィンガープリントに揃える。
    """
    from .core.cache import fingerprint

    return fingerprint(config)


def _meta(config: Config, context: CheckContext) -> dict[str, Any]:
    records = (
        tuple(context.records)
        + tuple(context.out_of_scope)
        + tuple(context.cross_dataset_excluded)
    )
    return {
        "fingerprint": _fingerprint(config),
        "sources": [str(path) for path in config.dataset_sources()],
        "n_annotations": len(records),
        "n_files": len(context.groups),
        # annotation 数だけでは規模が伝わらないので症例単位も持つ。
        "scale": {
            "patients": len({(r.dataset_id, r.patient_id) for r in records}),
            "studies": len({(r.dataset_id, r.study) for r in records}),
            "series": len({(r.dataset_id, r.study, r.series) for r in records}),
            "images": len({r.file_uid for r in records}),
            "annotations": len(records),
        },
        # データセットには annotation を持たない画像も含まれる。
        # そのうち大半は「正常例（No Findings）」で、意図的な陰性症例。
        # 「アノテーション漏れ」と区別できるようにここで分類する。
        "population": _population(context.groups),
        "decision_policy": {
            "cannot_determine_as_review_required": (
                config.decision_policy.cannot_determine_as_review_required
            ),
        },
    }


def _read_issues(path: Path) -> list[Issue]:
    """``issues.json`` を Issue へ戻す。``select`` は check を再実行しない。"""
    import json

    from .checks.base import Category, CheckStatus, ReviewPriority

    payload = json.loads(path.read_text(encoding="utf-8"))
    issues: list[Issue] = []
    for row in payload["issues"]:
        issues.append(
            Issue(
                check_id=row["check_id"],
                category=Category(row["category"]),
                severity=Severity(row["severity"]),
                status=CheckStatus(row["status"]),
                review_priority=ReviewPriority(row["review_priority"]),
                dataset_id=row["dataset_id"],
                source_json=row["source_json"],
                institution=row["institution"],
                study=row["study"],
                series=row["series"],
                file=row["file"],
                file_uid=row["file_uid"],
                geometry_uid=row["geometry_uid"],
                annotation_type=row["annotation_type"],
                image_path=row["image_path"],
                mask_path=row["mask_path"],
                message=row["message"],
                detail=row.get("detail") or {},
                related_geometry_uids=tuple(row.get("related_geometry_uids") or []),
                duplicate_group_id=row.get("duplicate_group_id"),
                cannot_determine_reason=row.get("cannot_determine_reason"),
            )
        )
    return issues


def _population(groups: tuple[FileGroup, ...]) -> dict[str, Any]:
    """データセットに含まれる画像・study・患者の全体像。

    ``scale`` は annotation を持つものしか数えない（採否マスタ由来）。
    こちらは JSON に載っている全エントリを数えて、
    annotation が無い画像の正体（正常例か未アノテーションか）まで分ける。
    """
    annotated = [g for g in groups if g.records]
    negative = [g for g in groups if not g.records and g.is_negative_case]
    unlabeled = [g for g in groups if not g.records and not g.is_negative_case]

    def levels(subset: list[FileGroup]) -> dict[str, int]:
        return {
            "images": len(subset),
            "studies": len({(g.dataset_id, g.study) for g in subset}),
            "patients": len({(g.dataset_id, g.patient_id) for g in subset}),
        }

    return {
        "total": levels(list(groups)),
        "annotated": levels(annotated),
        "negative": levels(negative),
        "unannotated": levels(unlabeled),
    }


def _read_image_decisions(path: Path) -> list:
    """``image_decisions.json`` を ImageDecision へ戻す。"""
    import json

    from .selection.decisions import DecisionSource, ReviewStatus
    from .selection.image_decisions import ImageDecision

    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for row in payload["decisions"]:
        data = dict(row)
        data["review_status"] = ReviewStatus(data["review_status"])
        data["final_decision"] = Decision(data["final_decision"])
        data["decision_source"] = DecisionSource(data["decision_source"])
        for key in ("reviewer", "reviewed_at", "comment"):
            if data.get(key) == "":
                data[key] = None
        result.append(ImageDecision(**data))
    return result


def _pending_diff_message(
    previous_summary: dict[str, Any] | None,
    summary: dict[str, Any],
    previous_images: dict[str, Any] | None,
    images: dict[str, Any],
) -> str | None:
    """前回の ``select`` からの pending 差分メッセージ。

    ``review_decisions.json`` の reviewer/reviewed_at は運用上ずっと空になり
    がちで、そこから「今回のセッションで何件判定したか」は追えない。前回の
    ``selection_decisions.json``/``image_decisions.json``（上書きする前に
    読んだもの）との pending 件数の差分だけが確実に取れる進捗指標なので、
    ``select`` のたびにログへ出す。初回実行（前回のファイルが無い）では
    比較対象が無いので ``None`` を返す。
    """
    if previous_summary is None or previous_images is None:
        return None
    return (
        "前回からの変化: pending annotation {} → {}（{:+d}） / 画像 {} → {}（{:+d}）"
    ).format(
        previous_summary["pending"],
        summary["pending"],
        summary["pending"] - previous_summary["pending"],
        previous_images["pending"],
        images["pending"],
        images["pending"] - previous_images["pending"],
    )


def _read_selection(path: Path) -> list:
    """``selection_decisions.json`` を SelectionDecision へ戻す。"""
    import json

    from .selection.decisions import DecisionSource, ReviewStatus, SelectionDecision

    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for row in payload["decisions"]:
        data = dict(row)
        data["review_status"] = ReviewStatus(data["review_status"])
        data["final_decision"] = Decision(data["final_decision"])
        data["decision_source"] = DecisionSource(data["decision_source"])
        for key in (
            "user",
            "timestamp",
            "related_geometry_uid",
            "kept_geometry_uid",
            "duplicate_group_id",
            "reviewer",
            "reviewed_at",
            "comment",
        ):
            if data.get(key) == "":
                data[key] = None
        result.append(SelectionDecision(**data))
    return result


def _resolve_review_decisions(config: Config) -> Path:
    """人間の判定をどこから読むか決める。

    正本は ``review/review_decisions.json``（fingerprint 非依存・git管理）。
    まだ移行していないリポジトリでも動くよう、正本が無いときだけ従来の
    fingerprint 配下へ落ちる。
    """
    target = config.review_decisions_path
    if target.exists():
        return target
    legacy = _review_dir(config) / "review_decisions.json"
    if legacy.exists():
        logger.warning(
            "目視判定の正本 %s が無いので %s を読む。"
            "`review migrate-decisions` で正本へ移すこと"
            "（fingerprint が変わると判定を引き継げない）",
            target,
            legacy,
        )
        return legacy
    return target


def _read_review_decisions(
    path: Path,
) -> tuple[dict[str, HumanDecision], dict[str, HumanDecision]]:
    """目視判定を読む。無ければ空。

    FiftyOne の DB を正本にしないので、人間の判定は必ずこのファイル経由で流す。
    戻り値は ``(annotation単位, 画像単位)``。

    annotation のキーは ``annotation_uid``（``dataset_id::geometry_uid``）。
    geometry_uid は cross-dataset 重複でデータセットをまたいで再利用される
    ため、bare geometry_uid をキーにすると無関係な別データセットの
    annotation に人間の判定が誤って適用される
    （``selection/decisions.py::build_decisions`` 参照）。

    画像のキーは ``stable_file_uid``（``dataset_id::inst/study/series/file``）。
    旧形式の ``file_uid`` は ``source_json``（日時スタンプ込みのファイル名）を
    含むため、元JSONを再エクスポートすると全件が引き当て不能になる。
    読み込み時に ``decision_store`` が昇格する。
    """
    from .review import decision_store as store

    try:
        rows = store.load_rows(path)
    except store.DecisionStoreError as error:
        # 読めないファイルを「判定が無い」と解釈してはいけない。人間の判定を
        # 丸ごと落としたまま select が通ると、目視の成果が静かに消える。
        raise ValueError(f"目視判定を読めない: {error}") from error

    annotations: dict[str, HumanDecision] = {}
    images: dict[str, HumanDecision] = {}
    skipped = 0
    for row in rows:
        dataset_id = row.get("dataset_id")
        human = HumanDecision(
            geometry_uid=row["key"],
            decision=Decision(row["decision"]),
            reason=row["reason"],
            reviewer=row["reviewer"] or None,
            reviewed_at=row["reviewed_at"] or None,
            comment=row["comment"] or None,
            dataset_id=dataset_id,
        )
        if row["kind"] == store.KIND_ANNOTATION:
            if not dataset_id:
                # 古い形式。誤って別データセットへ適用するくらいなら
                # 目視漏れの方がまし。
                skipped += 1
                continue
            annotations[store.annotation_key(dataset_id, row["key"])] = human
        else:
            images[store.stable_uid(row)] = human

    if skipped:
        logger.warning(
            "dataset_id を持たない旧形式の annotation 判定 %d 件を無視した"
            "（誤って別データセットへ適用されるのを防ぐため）",
            skipped,
        )
    return annotations, images


def _split(value: str | None) -> list[str] | None:
    return [token for token in value.split(",") if token.strip()] if value else None


HANDLERS.update(
    {
        "show-config": _show_config,
        "list-sources": _list_sources,
        "list-checks": _list_checks,
        "scan": _scan,
        "check": _check,
        "select": _select,
        "report": _report,
        "gui": _gui,
        "serve": _serve,
        "build-dataset": _build_dataset,
        "export-lpdata": _export_lpdata,
        "export-lpdata-ofc": _export_lpdata_ofc,
        "merge-lpdata": _merge_lpdata,
        "review": _review,
    }
)


if __name__ == "__main__":
    raise SystemExit(main())
