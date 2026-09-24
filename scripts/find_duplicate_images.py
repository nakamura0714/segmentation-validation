"""lp-data データセット内の重複画像を探す。

2 段階で見る。

1. 完全一致（バイト単位）: ファイルサイズが同じものだけ SHA-256 を取る。
2. 近重複（画素）: 全画像を 32x32 に縮小して指紋を作り、dHash が一致するペアを
   正規化相関で確認する。解像度違い・再エンコード違いの同一画像を拾うため。

    uv run python scripts/find_duplicate_images.py \
        output/lpdata/20260917/chest_metry_pi6_pneumothorax.json --workers 16

出力: ``output/research/data/duplicate-images_<tag>.csv``
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

THUMB = 32
#: dHash が一致した候補を「同じ画像」と認めるしきい値（正規化した 32x32 の相関）
CORR_THRESHOLD = 0.98


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(item: tuple[str, str]) -> tuple[str, int, list[float]] | None:
    """(sample_id, path) -> (sample_id, dHash, 正規化した 32x32 サムネイル)"""
    sample_id, path = item
    try:
        with Image.open(path) as image:
            thumb = np.asarray(
                image.resize((THUMB, THUMB), Image.BILINEAR), dtype=np.float64
            )
    except Exception:
        return None
    if thumb.ndim == 3:
        thumb = thumb.mean(axis=2)
    std = thumb.std()
    normalized = (thumb - thumb.mean()) / std if std > 0 else np.zeros_like(thumb)
    # dHash: 横方向の隣接差の符号を 8x8 で取る（一様な明るさ・コントラスト変化に強い）
    small = np.asarray(
        Image.fromarray(thumb).resize((9, 8), Image.BILINEAR), dtype=np.float64
    )
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    dhash = int("".join("1" if bit else "0" for bit in bits), 2)
    return sample_id, dhash, normalized.flatten().tolist()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("output/research/data"))
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--tag", default=None)
    args = parser.parse_args()

    tag = args.tag or args.dataset.parent.name
    samples: dict[str, dict] = json.loads(args.dataset.read_text())["samples"]
    print(f"{len(samples):,} samples")

    # ---- 1. 完全一致 -------------------------------------------------------
    by_size: dict[int, list[str]] = defaultdict(list)
    for sample_id, sample in samples.items():
        try:
            by_size[os.stat(sample["image_file"]).st_size].append(sample_id)
        except OSError:
            print(f"missing: {sample['image_file']}")
    candidates = [ids for ids in by_size.values() if len(ids) > 1]
    print(f"same-size groups: {len(candidates)} ({sum(len(g) for g in candidates)} images)")

    by_digest: dict[str, list[str]] = defaultdict(list)
    for group in candidates:
        for sample_id in group:
            by_digest[sha256(samples[sample_id]["image_file"])].append(sample_id)
    exact = [sorted(ids) for ids in by_digest.values() if len(ids) > 1]
    print(f"byte-identical groups: {len(exact)}")

    # ---- 2. 近重複 ---------------------------------------------------------
    items = [(sample_id, sample["image_file"]) for sample_id, sample in samples.items()]
    hashes: dict[str, int] = {}
    thumbs: dict[str, np.ndarray] = {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for index, result in enumerate(pool.map(fingerprint, items, chunksize=16), 1):
            if index % 2000 == 0:
                print(f"  fingerprinted {index:,}/{len(items):,}", flush=True)
            if result is None:
                continue
            sample_id, dhash, thumb = result
            hashes[sample_id] = dhash
            thumbs[sample_id] = np.array(thumb)

    by_hash: dict[int, list[str]] = defaultdict(list)
    for sample_id, dhash in hashes.items():
        by_hash[dhash].append(sample_id)
    near_groups = [sorted(ids) for ids in by_hash.values() if len(ids) > 1]
    print(f"dhash collision groups: {len(near_groups)}")

    near: list[tuple[list[str], float]] = []
    for group in near_groups:
        # グループ内の全ペアを相関で確認し、しきい値を超えたものだけ残す
        kept: dict[str, set[str]] = defaultdict(set)
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                a, b = thumbs[left], thumbs[right]
                corr = float(np.dot(a, b) / len(a))
                if corr >= CORR_THRESHOLD:
                    kept[left].add(right)
                    kept[right].add(left)
        seen: set[str] = set()
        for sample_id, linked in kept.items():
            if sample_id in seen:
                continue
            cluster = sorted({sample_id} | linked)
            seen.update(cluster)
            pairs = [
                float(np.dot(thumbs[x], thumbs[y]) / len(thumbs[x]))
                for i, x in enumerate(cluster)
                for y in cluster[i + 1 :]
            ]
            near.append((cluster, min(pairs)))
    print(f"near-duplicate groups (corr >= {CORR_THRESHOLD}): {len(near)}")

    # ---- 出力 --------------------------------------------------------------
    exact_index = {sample_id: i for i, group in enumerate(exact, 1) for sample_id in group}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"duplicate-images_{tag}.csv"
    with out_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "kind",
                "group",
                "min_corr",
                "sample_id",
                "facility_id",
                "patient_id",
                "pneumothorax_case",
                "pneumothorax_mask",
                "pneumothorax_area_pix2",
                "pneumothorax_side",
                "abnormal_finding_status",
                "bulla_bleb_status",
                "image_shape",
                "dicom_file",
            ]
        )
        rows = [("exact", i, "", group) for i, group in enumerate(exact, 1)]
        rows += [
            ("near", i, f"{corr:.4f}", group)
            for i, (group, corr) in enumerate(sorted(near), 1)
            # 完全一致で既に出したペアは near から除く
            if not all(exact_index.get(s) and exact_index[s] == exact_index[group[0]] for s in group)
        ]
        for kind, index, corr, group in rows:
            for sample_id in group:
                sample = samples[sample_id]
                mask = sample.get("pneumothorax_mask") or {}
                writer.writerow(
                    [
                        kind,
                        index,
                        corr,
                        sample_id,
                        sample["facility_id"],
                        sample["patient_id"],
                        sample["pneumothorax_case"],
                        bool(mask.get("pixel_array")),
                        sample.get("pneumothorax_area_pix2"),
                        sample.get("pneumothorax_side"),
                        sample["abnormal_finding_status"],
                        sample["bulla_bleb_status"],
                        f"{sample['image_shape']['width']}x{sample['image_shape']['height']}",
                        sample["dicom_file"],
                    ]
                )
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
