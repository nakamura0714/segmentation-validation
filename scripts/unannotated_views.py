"""``unannotated_view`` の画像（アノテーション済み series の2枚目）を拾い出す。

用途は**呼気像かどうかの判断**。M09 は「側面像なら除外」という前提でこれらを
目視へ回しているが、同じ体位をもう1枚撮っているだけなら**呼気像**の可能性があり、
その場合は気胸の学習データとして価値がある（呼気で気胸が顕在化するため）。

DICOM から3種類の材料を取る。

- **撮影法の記述** —— ``SeriesDescription`` に ``最大呼気`` / ``呼気（こき）`` と
  そのまま入っている施設がある。``AcquisitionDeviceProcessingDescription``
  （例 ``Chest,Frn,P-A``）には正面/側面が入る。
- **撮影間隔** —— ``AcquisitionTime``、無ければ ``ContentTime``。同一 study 内で
  数十秒差なら、体位を変えずにもう1枚撮ったと読める。
- **画像サイズと向き** —— 側面像は正面像と寸法が変わることが多い。

``nagoya_daiichi``（27枚）はどの撮影法タグも空なので、**メタデータだけでは
判断できない**。``verdict_hint`` に ``要目視`` を立てて区別する。

出力は1行=1枚（未アノテーション側）。同じ series のアノテーション済み画像を
``paired_`` 列に並べ、突合せずに判断できるようにしてある。

使い方::

    uv run python scripts/unannotated_views.py \
        output/validation/v_78b89657d448/image_decisions.csv \
        --manifest output/development/20260918/development_manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import pydicom

IMAGE_ROOT = Path("/mnt")

#: 撮影法を読むタグ。実データで中身が入っていたものに絞ってある。
DICOM_TAGS = (
    "ViewPosition",
    "AcquisitionDeviceProcessingDescription",
    "SeriesDescription",
    "BodyPartExamined",
    "CassetteOrientation",
    "InstanceNumber",
    "AcquisitionNumber",
    "AcquisitionTime",
    "ContentTime",
    "Rows",
    "Columns",
)

#: 撮影法の記述が入りうるテキストタグ（キーワード判定の対象）。
TEXT_TAGS = (
    "ViewPosition",
    "AcquisitionDeviceProcessingDescription",
    "SeriesDescription",
)

LATERAL_HINTS = ("lat", "側面", "l-r", "r-l")
EXPIRATION_HINTS = ("呼気", "こき", "expir", "max-exp")
INSPIRATION_HINTS = ("吸気", "inspir", "max-insp")

#: 同一検査内の撮り直しとみなす上限。撮影間隔がこれを超えたら別物として扱う。
SAME_SESSION_SECONDS = 600


def read_tags(image_path: str) -> dict[str, str]:
    """DICOM を1枚読んで :data:`DICOM_TAGS` を文字列で返す。読めなければ空。"""
    path = IMAGE_ROOT / image_path
    if not path.exists():
        return {"dicom_status": "missing"}
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
    except Exception as exc:  # 壊れたDICOMでも他の行は出す
        return {"dicom_status": f"error: {type(exc).__name__}"}
    out = {"dicom_status": "ok"}
    for tag in DICOM_TAGS:
        value = getattr(ds, tag, None)
        out[tag] = "" if value is None else str(value)
    return out


def timestamp(tags: dict[str, str]) -> float | None:
    """``AcquisitionTime``、無ければ ``ContentTime`` を秒に直す。"""
    for key in ("AcquisitionTime", "ContentTime"):
        value = tags.get(key, "")
        if not value:
            continue
        try:
            return int(value[0:2]) * 3600 + int(value[2:4]) * 60 + float(value[4:])
        except ValueError:
            continue
    return None


def haystack(tags: dict[str, str]) -> str:
    return " ".join(tags.get(tag, "") for tag in TEXT_TAGS).lower()


def phase_of(tags: dict[str, str]) -> str:
    """撮影法の記述から ``expiration`` / ``inspiration`` / ``""`` を返す。"""
    text = haystack(tags)
    if any(hint in text for hint in EXPIRATION_HINTS):
        return "expiration"
    if any(hint in text for hint in INSPIRATION_HINTS):
        return "inspiration"
    return ""


def classify(tags: dict[str, str], paired: dict[str, str], delta: float | None) -> str:
    """1行ぶんの判定。**メタデータで言えることしか言わない。**"""
    if any(hint in haystack(tags) for hint in LATERAL_HINTS):
        return "側面像 -> 除外のまま"

    phase = phase_of(tags)
    if phase == "expiration":
        return "呼気像（記述あり）-> train候補"
    if phase == "inspiration":
        return "吸気像（記述あり）-> 呼気ではない"
    if phase_of(paired) == "expiration":
        return "ペアが呼気像 -> この枚は吸気側"

    same_view = bool(tags.get("AcquisitionDeviceProcessingDescription")) and (
        tags["AcquisitionDeviceProcessingDescription"]
        == paired.get("AcquisitionDeviceProcessingDescription")
    )
    same_size = (
        bool(tags.get("Rows"))
        and tags.get("Rows") == paired.get("Rows")
        and tags.get("Columns") == paired.get("Columns")
    )
    close = delta is not None and abs(delta) <= SAME_SESSION_SECONDS
    if same_view and close:
        return "同一体位を連続撮影 -> 呼気像の候補"
    if same_size and close:
        return "要目視（同サイズ・連続撮影だが撮影法タグが空）"
    return "要目視（判別材料なし）"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_decisions", type=Path, help="image_decisions.csv")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="development_manifest.csv。ペア画像の病変クラスを引くのに使う",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="出力CSV（既定: output/research/data/unannotated-views_<fingerprint>.csv）",
    )
    args = parser.parse_args()

    rows = list(csv.DictReader(args.image_decisions.open()))
    by_series: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_series[(row["dataset_id"], row["series"])].append(row)

    lesions: dict[str, str] = {}
    if args.manifest and args.manifest.exists():
        for row in csv.DictReader(args.manifest.open()):
            lesions[row["file_uid"]] = row["lesion_classes"]

    targets = [r for r in rows if r["image_class"] == "unannotated_view"]
    if not targets:
        sys.exit("unannotated_view が1件も無い")

    out_path = args.out or (
        Path("output/research/data") / f"unannotated-views_{args.image_decisions.parent.name}.csv"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "verdict_hint", "institution", "patient_id", "study", "series", "file_id",
        "dataset_id", "file_uid", "image_path",
        "series_image_index", "series_image_count",
        "current_decision", "decision_source", "reviewer",
        "dicom_status", *DICOM_TAGS,
        "paired_file_id", "paired_image_path", "paired_n_annotations",
        "paired_lesion_classes", "paired_SeriesDescription",
        "paired_AcquisitionDeviceProcessingDescription",
        "paired_Rows", "paired_Columns",
        "seconds_after_paired", "same_size_as_paired",
    ]

    records = []
    for row in targets:
        siblings = [
            s for s in by_series[(row["dataset_id"], row["series"])]
            if s["file_id"] != row["file_id"] and int(s["n_annotations"] or 0) > 0
        ]
        paired = siblings[0] if siblings else {}
        tags = read_tags(row["image_path"])
        paired_tags = read_tags(paired["image_path"]) if paired else {}

        t_self, t_pair = timestamp(tags), timestamp(paired_tags)
        delta = t_self - t_pair if t_self is not None and t_pair is not None else None
        same_size = (
            bool(tags.get("Rows"))
            and tags.get("Rows") == paired_tags.get("Rows")
            and tags.get("Columns") == paired_tags.get("Columns")
        )

        records.append({
            "verdict_hint": classify(tags, paired_tags, delta),
            **{k: row.get(k, "") for k in (
                "institution", "patient_id", "study", "series", "file_id",
                "dataset_id", "file_uid", "image_path",
                "series_image_index", "series_image_count")},
            "current_decision": row.get("final_decision", ""),
            "decision_source": row.get("decision_source", ""),
            "reviewer": row.get("reviewer", ""),
            **tags,
            "paired_file_id": paired.get("file_id", ""),
            "paired_image_path": paired.get("image_path", ""),
            "paired_n_annotations": paired.get("n_annotations", ""),
            "paired_lesion_classes": lesions.get(paired.get("file_uid", ""), ""),
            "paired_SeriesDescription": paired_tags.get("SeriesDescription", ""),
            "paired_AcquisitionDeviceProcessingDescription":
                paired_tags.get("AcquisitionDeviceProcessingDescription", ""),
            "paired_Rows": paired_tags.get("Rows", ""),
            "paired_Columns": paired_tags.get("Columns", ""),
            "seconds_after_paired": "" if delta is None else round(delta, 1),
            "same_size_as_paired": "yes" if same_size else "no",
        })

    records.sort(key=lambda r: (r["verdict_hint"], r["institution"], r["patient_id"]))
    with out_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    print(f"wrote {out_path} ({len(records)} rows)")
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record["verdict_hint"]] += 1
    for hint, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {hint}")


if __name__ == "__main__":
    main()
