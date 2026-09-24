"""lp-data データセットJSONを FiftyOne で目視するためのデータセットを作る。

使い方:

    # 1) プレビューPNGを作って FiftyOne dataset を組む
    uv run python scripts/lpdata_fiftyone.py build \
        output/lpdata/20260918_merged/chest_metry_pi6_pneumothorax.json \
        --tag 20260918_merged --jobs 16

    # 2) App を起動する
    uv run python scripts/lpdata_fiftyone.py launch --tag 20260918_merged --port 5151

FiftyOne は 16bit PNG を表示できないので、``output/lpdata_review/<tag>/images/``
へ 8bit のプレビューを書き出してそれを ``filepath`` にする。変換は
**1–99パーセンタイル伸長 + 切り捨て**で、20260917 の既存プレビューと
ビット単位で一致することを確認済み（だから ``--seed-dir`` でハードリンク
再利用してよい）。

``pneumothorax_mask`` と ``lung_rect`` は lp-data 側のパス・座標をそのまま
指すだけで、画素もマスクも**作り直さない**。目視は成果物そのものを見るための
ものなので、ここで加工すると見ているものが変わる。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger("lpdata_fiftyone")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVIEW_ROOT = PROJECT_ROOT / "output" / "lpdata_review"

# 伸長に使うパーセンタイル。20260917 のプレビューと一致する値。
PCT_LOW = 1.0
PCT_HIGH = 99.0


def resolve(base: Path, value: str | None) -> Path | None:
    """lp-data JSON のパスを解決する。相対ならJSONの親を基準にする。"""
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else base / path


def to_preview(src_path: Path, dst_path: Path) -> None:
    """16bit PNG を 8bit のプレビューへ落とす。

    ``cv2.normalize`` の単純な min-max では外れ値1画素で全体が潰れるので、
    1–99 パーセンタイルで伸長する。丸めではなく切り捨て（``astype``）なのは
    既存プレビューに合わせるため。
    """
    image = cv2.imread(str(src_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"読めない: {src_path}")
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    data = image.astype(np.float64)
    low, high = np.percentile(data, [PCT_LOW, PCT_HIGH])
    scaled = np.clip((data - low) / max(high - low, 1.0) * 255.0, 0, 255).astype(np.uint8)
    # cv2.imwrite は拡張子で形式を決めるので、一時ファイルも .png で終わらせる。
    # 中断した残骸が sample_id と混ざらないよう先頭にドットを付けて隠す。
    tmp = dst_path.with_name(f".{dst_path.stem}.tmp.png")
    if not cv2.imwrite(str(tmp), scaled):
        raise RuntimeError(f"書けない: {dst_path}")
    os.replace(tmp, dst_path)


def _preview_job(args: tuple[str, str, str]) -> tuple[str, str | None]:
    sample_id, src, dst = args
    try:
        to_preview(Path(src), Path(dst))
        return sample_id, None
    except Exception as exc:  # noqa: BLE001 - 1件の失敗で全体を止めない
        return sample_id, f"{type(exc).__name__}: {exc}"


def seed_from(seed_dir: Path, image_dir: Path, sample_ids: list[str]) -> int:
    """既存プレビューをハードリンクで持ち込む。同じ FS なので実体は増えない。"""
    linked = 0
    for sample_id in sample_ids:
        src = seed_dir / f"{sample_id}.png"
        dst = image_dir / f"{sample_id}.png"
        if dst.exists() or not src.exists():
            continue
        try:
            os.link(src, dst)
            linked += 1
        except OSError:
            # ハードリンクが張れない置き場なら普通にコピーへ倒す
            dst.write_bytes(src.read_bytes())
            linked += 1
    return linked


def build_previews(
    samples: dict, base: Path, image_dir: Path, jobs: int, seed_dir: Path | None
) -> list[tuple[str, str, str]]:
    """プレビューを揃える。戻り値は失敗した (sample_id, facility, error)。"""
    image_dir.mkdir(parents=True, exist_ok=True)

    if seed_dir and seed_dir.is_dir():
        linked = seed_from(seed_dir, image_dir, list(samples))
        logger.info("既存プレビューを再利用: %d 件 <- %s", linked, seed_dir)

    todo: list[tuple[str, str, str]] = []
    skipped = 0
    missing: list[tuple[str, str, str]] = []
    for sample_id, sample in samples.items():
        dst = image_dir / f"{sample_id}.png"
        if dst.exists():
            skipped += 1
            continue
        src = resolve(base, sample.get("image_file"))
        if src is None or not src.exists():
            missing.append((sample_id, sample.get("facility_id", ""), "image_file が無い"))
            continue
        todo.append((sample_id, str(src), str(dst)))

    logger.info("プレビュー: 既存 %d / 生成 %d / 元画像なし %d", skipped, len(todo), len(missing))
    if not todo:
        return missing

    failures = list(missing)
    done = 0
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(_preview_job, item) for item in todo]
        for future in as_completed(futures):
            sample_id, error = future.result()
            done += 1
            if error:
                failures.append((sample_id, samples[sample_id].get("facility_id", ""), error))
            if done % 500 == 0 or done == len(todo):
                rate = done / max(time.monotonic() - started, 1e-9)
                remain = (len(todo) - done) / max(rate, 1e-9)
                logger.info(
                    "プレビュー変換 %d/%d (%d%%) %.1f件/秒 残り %d分%02d秒",
                    done, len(todo), done * 100 // len(todo), rate, remain // 60, remain % 60,
                )
    return failures


def origin_of(sample_id: str, primary: set[str], secondary: set[str]) -> str:
    """統合前のどちら由来かを示す。OFC の寄与を App で絞れるようにする。"""
    in_p, in_s = sample_id in primary, sample_id in secondary
    if in_p and in_s:
        return "both"
    if in_p:
        return "development"
    if in_s:
        return "ofc_report"
    return "unknown"


def _declare_schema(fo, dataset) -> None:
    """フィールドを先に宣言する。

    FiftyOne は値から型を推論するので、絞り込んだ部分集合で組むと
    **全件 None の属性（``pneumothorax_side`` など）が schema に現れない**。
    App の絞り込み項目が入力次第で消えるのを防ぐため明示する。
    """
    string_fields = (
        "lpdata_id", "facility_id", "patient_id", "source", "pneumothorax_side",
        "abnormal_finding_status", "bulla_bleb_status", "view_position",
        "vendor_id", "dicom_file", "lpdata_image_file",
    )
    for field in string_fields:
        dataset.add_sample_field(field, fo.StringField)
    dataset.add_sample_field("pneumothorax_case", fo.BooleanField)
    dataset.add_sample_field("pneumothorax_area_mm2", fo.FloatField)
    for field in ("pneumothorax_area_pix2", "image_width", "image_height"):
        dataset.add_sample_field(field, fo.IntField)
    for field in ("spacing_x", "spacing_y"):
        dataset.add_sample_field(field, fo.FloatField)
    dataset.add_sample_field(
        "pneumothorax_mask", fo.EmbeddedDocumentField, embedded_doc_type=fo.Segmentation
    )
    dataset.add_sample_field(
        "lung_rect", fo.EmbeddedDocumentField, embedded_doc_type=fo.Detections
    )


def cmd_build(args: argparse.Namespace) -> int:
    import fiftyone as fo

    dataset_json = Path(args.dataset_json).resolve()
    base = dataset_json.parent
    payload = json.loads(dataset_json.read_text(encoding="utf-8"))
    samples: dict = payload["samples"]

    review_dir = REVIEW_ROOT / args.tag
    image_dir = review_dir / "images"
    review_dir.mkdir(parents=True, exist_ok=True)

    name = args.name or f"pi6-lpdata-{args.tag}"
    logger.info("対象 %d 件 / dataset=%s / jobs=%d", len(samples), name, args.jobs)

    # 由来の判定材料。meta.provenance.inputs のJSONを読めるときだけ使う。
    primary_ids: set[str] = set()
    secondary_ids: set[str] = set()
    for index, entry in enumerate(payload.get("meta", {}).get("provenance", {}).get("inputs", [])):
        path = resolve(PROJECT_ROOT, entry.get("path"))
        if path is None or not path.is_file():
            logger.warning("由来の判定に使う入力が読めない: %s", entry.get("path"))
            continue
        ids = set(json.loads(path.read_text(encoding="utf-8"))["samples"])
        (primary_ids if index == 0 else secondary_ids).update(ids)

    failures = build_previews(samples, base, image_dir, args.jobs, args.seed_dir)

    if failures:
        failures_csv = review_dir / "failures.csv"
        with failures_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "facility_id", "error"])
            writer.writerows(failures)
        logger.warning("プレビューを作れなかった %d 件 -> %s", len(failures), failures_csv)

    failed_ids = {row[0] for row in failures}

    if fo.dataset_exists(name):
        logger.info("既存の dataset を作り直す: %s", name)
    dataset = fo.Dataset(name, overwrite=True, persistent=True)
    _declare_schema(fo, dataset)

    fo_samples = []
    for sample_id, sample in samples.items():
        if sample_id in failed_ids:
            continue
        preview = image_dir / f"{sample_id}.png"
        if not preview.exists():
            continue
        shape = sample.get("image_shape") or {}
        width = shape.get("width")
        height = shape.get("height")
        spacing = sample.get("pixel_spacing") or {}
        case = bool(sample.get("pneumothorax_case"))
        mask = sample.get("pneumothorax_mask") or {}
        mask_path = resolve(base, mask.get("pixel_array"))
        has_mask = mask_path is not None and mask_path.exists()
        origin = origin_of(sample_id, primary_ids, secondary_ids)

        fo_sample = fo.Sample(
            filepath=str(preview),
            tags=[
                f"facility:{sample.get('facility_id')}",
                f"pnx:{str(case).lower()}",
                f"mask:{'yes' if has_mask else 'no'}",
                f"src:{origin}",
            ],
            lpdata_id=sample_id,
            facility_id=sample.get("facility_id"),
            patient_id=sample.get("patient_id"),
            source=origin,
            pneumothorax_case=case,
            pneumothorax_side=sample.get("pneumothorax_side"),
            pneumothorax_area_mm2=sample.get("pneumothorax_area_mm2"),
            pneumothorax_area_pix2=sample.get("pneumothorax_area_pix2"),
            abnormal_finding_status=sample.get("abnormal_finding_status"),
            bulla_bleb_status=sample.get("bulla_bleb_status"),
            view_position=sample.get("view_position"),
            vendor_id=sample.get("vendor_id"),
            image_width=width,
            image_height=height,
            spacing_x=spacing.get("x"),
            spacing_y=spacing.get("y"),
            dicom_file=sample.get("dicom_file"),
            lpdata_image_file=str(resolve(base, sample.get("image_file")) or ""),
        )

        if has_mask:
            fo_sample["pneumothorax_mask"] = fo.Segmentation(mask_path=str(mask_path))

        rect = sample.get("lung_rect")
        if rect and width and height:
            x0, y0 = rect["x_min"], rect["y_min"]
            fo_sample["lung_rect"] = fo.Detections(
                detections=[
                    fo.Detection(
                        label="lung_rect",
                        bounding_box=[
                            x0 / width,
                            y0 / height,
                            (rect["x_max"] - x0) / width,
                            (rect["y_max"] - y0) / height,
                        ],
                    )
                ]
            )

        fo_samples.append(fo_sample)

    dataset.add_samples(fo_samples)
    dataset.save()
    logger.info("FiftyOne dataset: %s / %d サンプル", name, len(dataset))
    logger.info("起動: python scripts/lpdata_fiftyone.py launch --tag %s --port %d", args.tag, 5151)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    import fiftyone as fo

    name = args.name or f"pi6-lpdata-{args.tag}"
    if not fo.dataset_exists(name):
        logger.error("dataset が無い: %s（先に build を実行する）", name)
        return 1
    dataset = fo.load_dataset(name)
    print(f"dataset = {name} / samples = {len(dataset)} / port = {args.port}", flush=True)
    session = fo.launch_app(dataset, port=args.port, remote=True)
    print(f"App 起動: http://localhost:{args.port}", flush=True)
    # remote セッションはブロックしないとプロセスが即終了して App が落ちる。
    session.wait(-1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="プレビューを作って FiftyOne dataset を組む")
    build.add_argument("dataset_json", help="lp-data データセットJSON")
    build.add_argument("--tag", required=True, help="output/lpdata_review/<tag>/ の名前")
    build.add_argument("--name", default=None, help="FiftyOne dataset 名（既定 pi6-lpdata-<tag>）")
    build.add_argument("--jobs", type=int, default=16, help="プレビュー変換の並列数")
    build.add_argument(
        "--seed-dir",
        type=Path,
        default=None,
        help="既存プレビューの置き場。同じ変換で作られたものをハードリンクで再利用する",
    )

    launch = sub.add_parser("launch", help="App を起動する")
    launch.add_argument("--tag", required=True)
    launch.add_argument("--name", default=None)
    launch.add_argument("--port", type=int, default=5151)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    return {"build": cmd_build, "launch": cmd_launch}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
