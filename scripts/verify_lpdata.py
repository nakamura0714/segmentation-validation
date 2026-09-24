"""出来上がった lp-data JSON を読み戻して検証する（読み取りのみ）。

``export-lpdata`` / ``merge-lpdata`` の後に走らせて、件数・ラベル3値・
テンプレート適合・不変条件・欠損を一度に見る。

    uv run python scripts/verify_lpdata.py \
        output/lpdata/<tag>/chest_metry_pi6_pneumothorax.json
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from segmentation_validation.lpdata_export.invariants import check_samples
from segmentation_validation.lpdata_export.sample import FIELD_BUILDERS
from segmentation_validation.lpdata_export.template import load_template

TEMPLATE = (
    "/mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6/"
    "src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml"
)


def has_mask(sample: dict, key: str) -> bool:
    mask = sample.get(key)
    return bool(mask and mask.get("pixel_array"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--template", type=Path, default=Path(TEMPLATE))
    args = parser.parse_args()

    payload = json.loads(args.dataset.read_text())
    meta = payload["meta"]
    samples: dict[str, dict] = payload["samples"]

    print(f"== {args.dataset}")
    print(f"dataset_id : {meta.get('dataset_id')}")
    print(f"date       : {meta.get('date')} / owner: {meta.get('owner')}")
    print(f"サンプル数 : {len(samples):,}")

    # --- テンプレート適合 ---
    template = load_template(args.template)
    declared = set(template.keys)
    print(f"\n[structure] テンプレートと一致: {declared == set(meta['structure'])}")
    print(f"[structure] ビルダと一致      : {declared == set(FIELD_BUILDERS)}")
    bad = [sid for sid, s in samples.items() if set(s) != declared]
    print(f"[structure] キーが違うサンプル: {len(bad)}")

    # --- ラベル3値 ---
    print()
    for key in ("pneumothorax_case", "abnormal_finding_status", "bulla_bleb_status",
                "pleural_effusion_status", "pneumothorax_side"):
        counts = collections.Counter(str(s.get(key)) for s in samples.values())
        print(f"[{key}] {dict(sorted(counts.items()))}")

    # --- マスク ---
    print()
    for key in ("pneumothorax_mask", "lung_mask", "thorax_mask"):
        present = sum(1 for s in samples.values() if has_mask(s, key))
        print(f"[{key}] あり {present:,} / なし {len(samples) - present:,}")

    ptx = [s for s in samples.values() if s["pneumothorax_case"]]
    print(f"[気胸 true] {len(ptx):,} / うちマスクあり "
          f"{sum(1 for s in ptx if has_mask(s, 'pneumothorax_mask')):,}")
    false_with_mask = sum(
        1
        for s in samples.values()
        if not s["pneumothorax_case"] and has_mask(s, "pneumothorax_mask")
    )
    print(f"[気胸 false でマスクあり] {false_with_mask:,}  ← 0 であること")

    # --- 胸水 absent の由来を分ける ---
    # absent には2つの根拠がある。
    #   1. annotation の明示正常（``No Findings/normal`` かつ所見0件）
    #      → 必ず ``abnormal_finding_status: absent`` と一致し、気胸 true にはならない
    #   2. レポートの明示陰性（``structured_negative``）
    #      → 所見の有無とは独立。**気胸 true かつ胸水 absent は正当な組み合わせ**
    eff_absent = {sid for sid, s in samples.items()
                  if s.get("pleural_effusion_status") == "absent"}
    from_annotation = {sid for sid in eff_absent
                       if samples[sid].get("abnormal_finding_status") == "absent"}
    from_report = eff_absent - from_annotation
    bad = [sid for sid in from_annotation if samples[sid]["pneumothorax_case"]]
    print(f"\n[胸水 absent] {len(eff_absent):,}")
    print(f"  annotation の明示正常由来: {len(from_annotation):,}"
          f" → うち気胸 true {len(bad)} 件 ← 0 であること {bad[:5]}")
    print(f"  レポートの明示陰性由来    : {len(from_report):,}"
          f" → うち気胸 true "
          f"{sum(1 for sid in from_report if samples[sid]['pneumothorax_case']):,}"
          "（気胸に胸水を伴わないのは正当）")
    eff_present = [sid for sid, s in samples.items()
                   if s.get("pleural_effusion_status") == "present"]
    print(f"[胸水 present] {len(eff_present):,} / うち気胸 true "
          f"{sum(1 for sid in eff_present if samples[sid]['pneumothorax_case']):,}")

    # --- 欠損 ---
    print()
    for key in ("patient_id", "facility_id", "image_file", "dicom_file"):
        missing = sum(1 for s in samples.values() if not s.get(key))
        print(f"[{key}] 欠損 {missing:,}")
    patients = {(s["facility_id"], s["patient_id"]) for s in samples.values()}
    facilities = {s["facility_id"] for s in samples.values()}
    print(f"[患者] {len(patients):,} / [施設] {len(facilities)}")

    # --- 不変条件 ---
    violations = check_samples(samples)
    print(f"\n[不変条件] 違反 {len(violations)} 件")
    for v in violations[:10]:
        print("   ", v)


if __name__ == "__main__":
    main()
