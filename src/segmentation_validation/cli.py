"""コマンドライン入口。

実運用のループは ``check`` → （目視）→ ``review export`` → ``select`` → ``report``。
``select`` を独立させてあるのは、目視のたびに検出をやり直す必要が無いから。
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
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

    sub.add_parser("report", help="summary.md を書き出す")
    sub.add_parser("gui", help="ブラウザで見るダッシュボードHTMLを書き出す")

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
    rsub.add_parser("launch", help="App を localhost で起動する")
    rsub.add_parser("status", help="目視の進捗を表示する")
    rsub.add_parser("export", help="判定を review_decisions.json へ書き出す")
    rsub.add_parser("precision", help="自動ルールの Precision を集計する")
    rimport = rsub.add_parser("import", help="review_decisions.json を FiftyOne へ戻す")
    rimport.add_argument("--path", type=Path, default=None, help="読み込むJSON")
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

    handlers = {
        "show-config": _show_config,
        "list-sources": _list_sources,
        "list-checks": _list_checks,
        "scan": _scan,
        "check": _check,
        "select": _select,
        "report": _report,
        "gui": _gui,
        "build-dataset": _build_dataset,
        "review": _review,
    }
    handler = handlers.get(args.command)
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
    write_issues_json(out_dir / "issues.json", issues, config, meta)
    write_issues_csv(out_dir / "issues.csv", issues, config)

    summary = summarize_issues(issues)
    logger.info("issues: %d 件 -> %s", summary["total"], out_dir)
    logger.info("  severity: %s", summary["by_severity"])
    logger.info("  status  : %s", summary["by_status"])
    return EXIT_ISSUES if summary["by_severity"].get("error") else EXIT_OK


def _check_issues_complete(issues_path: Path, args: argparse.Namespace) -> bool:
    """``issues.json`` が全チェックの結果か確かめる。

    ``check --only M06,M07`` のように一部だけ回すと ``issues.json`` は上書きされて
    部分結果になる。そのまま ``select`` すると**検出されなかったのか実行して
    いないのかが区別できず**、pending が消えて採否が壊れる。
    """
    try:
        meta = json.loads(issues_path.read_text(encoding="utf-8")).get("meta", {})
    except (OSError, json.JSONDecodeError):
        return True  # 読めなければ後段の読み込みでエラーになる
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


def _select(config: Config, args: argparse.Namespace) -> int:
    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE

    out_dir = config.validation_dir / _fingerprint(config)
    issues_path = out_dir / "issues.json"
    if not issues_path.exists():
        logger.error("issues.json が無い。先に check を実行する: %s", issues_path)
        return EXIT_FAILURE

    if not _check_issues_complete(issues_path, args):
        return EXIT_ISSUES

    issues = _read_issues(issues_path)

    # 自動採否は2種類。
    #   D01: 完全一致の重複 -> timestamp が新しい方を残す
    #   M01-M05: 機械が確定的に「使えない」と判定した欠陥 -> exclude
    # 両方に当たった場合は exclude を優先する（merge_automatic）。
    duplicates, undecidable = build_automatic_decisions(
        context.records, context.pairs, config
    )
    broken = build_broken_decisions(issues, config)
    automatic = merge_automatic(duplicates, broken)
    if broken:
        logger.info("機械が確定した欠陥による自動exclude: %d 件", len(broken))
    auto_summary = summarize_automatic(automatic, undecidable)
    logger.info(
        "自動採否: exclude %d / keep %d（重複グループ %d）自動決定不能 %d",
        auto_summary["automatic_exclude"],
        auto_summary["automatic_keep"],
        auto_summary["duplicate_groups"],
        auto_summary["undecidable"],
    )

    # 人間の判定。無ければ review 対象は pending のままになるだけで正しく動く。
    review_path = args.review_decisions or out_dir / "review" / "review_decisions.json"
    human, human_images = _read_review_decisions(review_path)
    if human or human_images:
        logger.info(
            "人間の判定を読み込んだ: annotation %d 件 / 画像 %d 件",
            len(human),
            len(human_images),
        )

    # 採否マスタは「全 annotation が1行」なので対象外も含める。
    all_records = list(context.records) + list(context.out_of_scope)
    decisions = build_decisions(
        all_records,
        issues,
        config,
        automatic,
        human,
        out_of_scope=frozenset(r.geometry_uid for r in context.out_of_scope),
    )
    assert_invariants(decisions, all_records)

    # 画像単位の採否。annotation を持たない画像（正常例187 / 未アノテーション33）は
    # selection_decisions に行を持てないので、別ファイルで採否を明示する。
    image_decisions = build_image_decisions(
        context.groups, issues, config, human_images
    )
    assert_image_invariants(image_decisions, list(context.groups))

    meta = _meta(config, context)
    write_selection_json(out_dir / "selection_decisions.json", decisions, meta)
    write_selection_csv(out_dir / "selection_decisions.csv", decisions)
    write_image_json(out_dir / "image_decisions.json", image_decisions, meta)
    write_image_csv(out_dir / "image_decisions.csv", image_decisions)

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


def _gui(config: Config, args: argparse.Namespace) -> int:
    from .checks import ALL_CHECKS
    from .report.gui import build_payload, write_dashboard

    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE
    out_dir = config.validation_dir / _fingerprint(config)
    if not (out_dir / "issues.json").exists():
        logger.error("issues.json が無い。先に check を実行する")
        return EXIT_FAILURE
    decisions = _read_selection(out_dir / "selection_decisions.json")
    if not decisions:
        logger.error("selection_decisions.json が無い。先に select を実行する")
        return EXIT_FAILURE

    payload = build_payload(
        config,
        _read_issues(out_dir / "issues.json"),
        decisions,
        ALL_CHECKS,
        dict(context.masks),
        dict(context.files),
        _fingerprint(config),
        population=_population(context.groups),
        image_decisions=_read_image_decisions(out_dir / "image_decisions.json"),
    )
    target = out_dir / "dashboard.html"
    write_dashboard(target, payload, config)
    size = target.stat().st_size / 1024
    logger.info("dashboard.html (%.0f KB) -> %s", size, target)
    logger.info("  ブラウザで開くか、Artifact として公開する")
    return EXIT_OK


def _build_dataset(config: Config, args: argparse.Namespace) -> int:
    from .report.selection_output import read_image_json, read_selection_json
    from .report.summary_md import write_selection_summary
    from .selection.build_dataset import build_development_json, gate_message

    out_dir = config.validation_dir / _fingerprint(config)
    selection_path = out_dir / "selection_decisions.json"
    if not selection_path.exists():
        logger.error("selection_decisions.json が無い。先に select を実行する")
        return EXIT_FAILURE

    rows = read_selection_json(selection_path)
    by_uid = {row["geometry_uid"]: row["final_decision"] for row in rows}
    pending = sum(1 for v in by_uid.values() if v == "pending")
    uncertain = sum(1 for v in by_uid.values() if v == "uncertain")

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
    results = []
    for source in config.dataset_sources():
        from .adapters.engineer_set import dataset_id_for

        dataset_id = dataset_id_for(source)
        target = config.development_dir / dataset_id / version_tag / "development.json"
        results.append(
            build_development_json(
                source,
                by_uid,
                target,
                config,
                pending_as=args.pending_as,
                uncertain_as=args.uncertain_as,
                meta_extra={"fingerprint": _fingerprint(config)},
                image_decisions=by_image,
            )
        )
        logger.info(
            "%s: keep %d / exclude %d / 0件になったfile %d / 画像を落とした %d -> %s",
            dataset_id,
            results[-1].kept,
            results[-1].excluded,
            results[-1].files_emptied,
            results[-1].images_dropped,
            target,
        )

    if not all(r.original_unchanged for r in results):
        logger.error("元JSONが変化した。生成物を信用しないこと")
        return EXIT_FAILURE

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
    )
    logger.info("selection_summary.md -> %s", summary_path)
    return EXIT_OK


def _review(config: Config, args: argparse.Namespace) -> int:
    handlers = {
        "build": _review_build,
        "launch": _review_launch,
        "status": _review_status,
        "export": _review_export,
        "precision": _review_precision,
        "import": _review_import,
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
        return True  # 初回。守るものが無い
    try:
        from .review.export_decisions import unexported_human_decisions
    except ImportError:
        return True  # fiftyone が無ければ dataset も無い

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lost = unexported_human_decisions(
        config, manifest, review_dir / "review_decisions.json"
    )
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


def _review_build(config: Config, args: argparse.Namespace) -> int:
    from .review.export_assets import export_assets, select_review_groups
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

    adapters = {a.dataset_id: a.reference_mask_path for a in open_adapters(config)}
    review_dir = _review_dir(config)
    assets = export_assets(groups, config, review_dir, adapters, force=args.force)

    manifest = build_manifest(
        groups, assets, decisions, image_decisions, issues, config
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
    pending = summary["annotation_status"].get("pending", 0)
    pending += summary["image_status"].get("pending", 0)
    if pending:
        logger.warning("  目視未完了 %d 件（pending = 0 が完了条件）", pending)
    else:
        logger.info("  目視完了")
    return EXIT_OK


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

    target = _review_dir(config) / "review_decisions.json"
    write_decisions(target, rows, config)
    logger.info(
        "review_decisions: annotation %d / 画像 %d -> %s",
        sum(1 for r in rows if r["kind"] == "annotation"),
        sum(1 for r in rows if r["kind"] == "image"),
        target,
    )
    logger.info("次: `segmentation-validation select` で採否へ反映する")
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
    verdicts = read_verdicts(_review_dir(config) / "review_decisions.json")
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

    path = args.path or _review_dir(config) / "review_decisions.json"
    if not path.exists():
        logger.error("判定ファイルが無い: %s", path)
        return EXIT_FAILURE
    try:
        import_decisions(path, config)
    except RuntimeError as error:
        logger.error("%s", error)
        return EXIT_FAILURE
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
    return CheckContext(
        config=config,
        records=tuple(in_scope),
        out_of_scope=tuple(out_scope),
        groups=tuple(groups),
        path_invariants=invariants,
        masks=masks,
        files=files,
        pairs=tuple(pairs),
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
        (m.geometry_uid, m.role): m
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
    records = tuple(context.records) + tuple(context.out_of_scope)
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


def _read_review_decisions(
    path: Path,
) -> tuple[dict[str, HumanDecision], dict[str, HumanDecision]]:
    """``review_decisions.json`` を読む。無ければ空。

    FiftyOne の DB を正本にしないので、人間の判定は必ずこのファイル経由で流す。
    戻り値は ``(annotation単位, 画像単位)``。画像単位は annotation を持たない
    画像（未アノテーションのビュー）の採否で、キーは ``file_uid``。
    """
    import json

    if not path.exists():
        return {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        payload = {"decisions": payload, "image_decisions": []}

    def build(rows: list, key: str) -> dict[str, HumanDecision]:
        result: dict[str, HumanDecision] = {}
        for row in rows:
            uid = row[key]
            result[uid] = HumanDecision(
                geometry_uid=uid,
                decision=Decision(row["decision"]),
                reason=row.get("reason") or "",
                reviewer=row.get("reviewer"),
                reviewed_at=row.get("reviewed_at"),
                comment=row.get("comment"),
            )
        return result

    return (
        build(payload.get("decisions") or [], "geometry_uid"),
        build(payload.get("image_decisions") or [], "file_uid"),
    )


def _split(value: str | None) -> list[str] | None:
    return [token for token in value.split(",") if token.strip()] if value else None


if __name__ == "__main__":
    raise SystemExit(main())
