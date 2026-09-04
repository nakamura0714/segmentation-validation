"""コマンドライン入口。

実運用のループは ``check`` → （目視）→ ``review export`` → ``select`` → ``report``。
``select`` を独立させてあるのは、目視のたびに検出をやり直す必要が無いから。
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import open_adapters
from .checks import ALL_CHECKS, requires_scan, selected_checks
from .checks.base import CheckContext, Issue, Severity
from .config import Config, dump_config, load_config
from .core.records import AnnotationRecord, FileGroup
from .report.issues_json import summarize as summarize_issues
from .report.issues_json import write_issues_csv, write_issues_json
from .report.selection_output import write_selection_csv, write_selection_json
from .selection.automatic import build_automatic_decisions, summarize_automatic
from .selection.decisions import (
    Decision,
    HumanDecision,
    assert_invariants,
    build_decisions,
)
from .selection.decisions import summarize as summarize_decisions

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

    sub.add_parser("report", help="summary.md を書き出す")

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
        "build-dataset": _build_dataset,
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
    write_issues_json(out_dir / "issues.json", issues, config, meta)
    write_issues_csv(out_dir / "issues.csv", issues, config)

    summary = summarize_issues(issues)
    logger.info("issues: %d 件 -> %s", summary["total"], out_dir)
    logger.info("  severity: %s", summary["by_severity"])
    logger.info("  status  : %s", summary["by_status"])
    return EXIT_ISSUES if summary["by_severity"].get("error") else EXIT_OK


def _select(config: Config, args: argparse.Namespace) -> int:
    context = _load_context(config)
    if context is None:
        return EXIT_FAILURE

    out_dir = config.validation_dir / _fingerprint(config)
    issues_path = out_dir / "issues.json"
    if not issues_path.exists():
        logger.error("issues.json が無い。先に check を実行する: %s", issues_path)
        return EXIT_FAILURE

    issues = _read_issues(issues_path)

    # 自動採否は D01（完全一致・同一ラベル）だけ。timestamp が新しい方を残す。
    automatic, undecidable = build_automatic_decisions(
        context.records, context.pairs, config
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
    human = _read_review_decisions(
        args.review_decisions or out_dir / "review" / "review_decisions.json"
    )
    if human:
        logger.info("人間の判定を読み込んだ: %d 件", len(human))

    decisions = build_decisions(context.records, issues, config, automatic, human)
    assert_invariants(decisions, list(context.records))

    meta = _meta(config, context)
    write_selection_json(out_dir / "selection_decisions.json", decisions, meta)
    write_selection_csv(out_dir / "selection_decisions.csv", decisions)

    summary = summarize_decisions(decisions)
    logger.info("selection_decisions: %d 行 -> %s", summary["total"], out_dir)
    logger.info("  採否      : %s", summary["by_decision"])
    logger.info("  理由      : %s", summary["by_reason"])
    logger.info(
        "  review対象: %d / 未検証: %d",
        summary["review_targets"],
        summary["unverified"],
    )
    if summary["ready_to_build"]:
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


def _build_dataset(config: Config, args: argparse.Namespace) -> int:
    from .report.selection_output import read_selection_json
    from .report.summary_md import write_selection_summary
    from .selection.build_dataset import build_development_json

    out_dir = config.validation_dir / _fingerprint(config)
    selection_path = out_dir / "selection_decisions.json"
    if not selection_path.exists():
        logger.error("selection_decisions.json が無い。先に select を実行する")
        return EXIT_FAILURE

    rows = read_selection_json(selection_path)
    by_uid = {row["geometry_uid"]: row["final_decision"] for row in rows}
    pending = sum(1 for v in by_uid.values() if v == "pending")
    uncertain = sum(1 for v in by_uid.values() if v == "uncertain")

    # override は「許可」と「扱い」の両方を明示させる。
    # 片方だけでは通さない —— 保留が黙って入る／落ちるのを防ぐため。
    if pending and not (args.allow_pending and args.pending_as):
        logger.error(
            "pending が %d 件残っている。目視を進めるか "
            "`--allow-pending --pending-as keep|exclude` を明示すること",
            pending,
        )
        return EXIT_ISSUES
    if uncertain and not (args.allow_uncertain and args.uncertain_as):
        logger.error(
            "uncertain が %d 件残っている。`--allow-uncertain "
            "--uncertain-as keep|exclude` を明示すること",
            uncertain,
        )
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
            )
        )
        logger.info(
            "%s: keep %d / exclude %d / 0件になったfile %d -> %s",
            dataset_id,
            results[-1].kept,
            results[-1].excluded,
            results[-1].files_emptied,
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
    write_selection_summary(
        summary_path,
        config,
        decisions,
        issues,
        empty_files=sum(r.files_emptied for r in results),
        meta={"fingerprint": _fingerprint(config), "overrides": overrides},
    )
    logger.info("selection_summary.md -> %s", summary_path)
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

    logger.info(
        "対象: %d データセット / %d ファイル / %d annotation",
        len(adapters),
        len(groups),
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
        records=tuple(records),
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
    return {
        "fingerprint": _fingerprint(config),
        "sources": [str(path) for path in config.dataset_sources()],
        "n_annotations": len(context.records),
        "n_files": len(context.groups),
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


def _read_review_decisions(path: Path) -> dict[str, HumanDecision]:
    """``review_decisions.json`` を読む。無ければ空。

    FiftyOne の DB を正本にしないので、人間の判定は必ずこのファイル経由で流す。
    """
    import json

    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["decisions"] if isinstance(payload, dict) else payload
    result: dict[str, HumanDecision] = {}
    for row in rows:
        uid = row["geometry_uid"]
        result[uid] = HumanDecision(
            geometry_uid=uid,
            decision=Decision(row["decision"]),
            reason=row.get("reason") or "",
            reviewer=row.get("reviewer"),
            reviewed_at=row.get("reviewed_at"),
            comment=row.get("comment"),
        )
    return result


def _split(value: str | None) -> list[str] | None:
    return [token for token in value.split(",") if token.strip()] if value else None


if __name__ == "__main__":
    raise SystemExit(main())
