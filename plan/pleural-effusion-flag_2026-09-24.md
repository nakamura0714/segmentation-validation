# 胸水フラグ（`pleural_effusion_status`）対応と 2026-09-24 版の統合 lp-data 生成

**状態: 完了**（統合JSONの生成と検証まで。Train/Val/Test の分割は未実施）

## Context

med-chest-metry-pi6 のテンプレート `dataset_template_pneumothorax.yaml` に
`pleural_effusion_status`（3値 str）が追加された（`0d26dca`）。テンプレートが属性の正典で、
`lpdata_export` 側が同名の属性を出さない限り `template.validate_coverage` が落ちて
エクスポート自体ができない。あわせて気胸の定義を**水気胸を含む**ものに確定した。

胸水は気胸の偽陽性要因なので、誤検出の層別分析に使う（テンプレートの description）。

## 確定した規則（2026-09-24）

| 条件 | `pleural_effusion_status` |
|---|---|
| 胸水 annotation がある（塗りつぶし / 矩形 / 縁取りを問わない）、またはレポートが胸水陽性 | `present` |
| annotation の明示正常（`No Findings/normal` **かつ所見 annotation が0件**）、またはレポートの**明示陰性**（`structured_negative`） | `absent` |
| それ以外 | `unknown` |

- **記載が無いことを陰性に読み替えない。** annotation が無いだけ、レポートに記載が無いだけ
  （`no_mention`）、レポートの陽性所見が0件（`explicit_negative_report`）はいずれも `unknown`。
- **血胸（`hemothorax`）は胸水 `present` に含める。** 上流が畳んでおり（68 study / 70画像、
  すべて `structured_positive`）、畳む前の所見名は `pleural_effusion_findings` に残して
  根拠（`evidence` / `certainty_max` / `flags`）とともに分析用CSVへ出す。
- **水気胸は気胸 `present` に含める。** 上流が `pneumothorax_status: present` +
  `pneumothorax_subtype: hydropneumothorax` で返し、取り込み側は status しか見ない。
  胸水フラグは水気胸から自動的には立てず、胸水の根拠を見て独立に判定する。
- 正常ラベルと所見 annotation が同居して矛盾している画像（実データ 166 枚）は `unknown`。
  明示正常だけを条件にすると「所見あり」と「胸水は無い」を同時に主張することになる。
- 値が確定した後は動かさない。食い違いは primary を維持して `LabelConflict` に残す
  （`absent` を維持＝`explicit_negative_wins` / `present` を維持＝`never_downgrade`）。

### `absent` の2つの由来は必ず区別する

| 由来 | 件数（統合後） | 性質 |
|---|---:|---|
| annotation の明示正常 | 866 | `abnormal_finding_status: absent` と必ず一致。気胸 `true` にはならない |
| レポートの明示陰性 | 719 | 所見の有無と独立。**気胸 `true` かつ胸水 `absent` は正当**（219件） |

## 実装

| ファイル | 変更 |
|---|---|
| `core/records.py` | `ReportLabels` に `pleural_effusion_status` / `_evidence` / `_certainty_max` / `_flags` / `_findings` |
| `adapters/engineer_set.py` | 上流 schema 2 の専用キーを読む。`PLEURAL_EFFUSION_FINDINGS`（`pleural_effusion` / `hemothorax`）の2語だけを `finding_labels_observed` から照合して元の所見名を残す |
| `lpdata_export/labels.py` | `PLEURAL_EFFUSION` / `HYDROPNEUMOTHORAX_SUBTYPE`、`SampleLabels.pleural_effusion_status`、`build_labels` の3値判定 |
| `lpdata_export/sample.py` | `FIELD_BUILDERS` に追加（テンプレートとのキー一致） |
| `lpdata_export/invariants.py` | `PLEURAL_KEY` を label_map 検査へ |
| `lpdata_export/report_labels.py` | `SUPPORTED_SCHEMA_VERSION = 2`（版1は**受け付けない**）、レポートによる補強、`_effusion_conflict_reason` |
| `lpdata_export/merge.py` | 統合時の補強、`effusion_counts_after`、summary 節 |
| `lpdata_export/export.py` | 分析用CSVの列（status / evidence / certainty / flags / findings） |
| `scripts/verify_lpdata.py` | 出来上がったJSONを読み戻す検証（新規） |
| `README.md` | ラベル規則の表と不変条件 |
| `tests/` | conftest の `effusion` 系引数、labels / report_labels / merge / export に 15 本追加 |

**版1を受け付けない理由**: 版1には胸水の専用キーが無く、黙って全サンプル `unknown` の
データセットができてしまう。`check_schema` で止める（`template.validate_coverage` と同じ思想）。

## 生成した 2026-09-24 版

### 入力（すべて絶対パス。glob も「最新自動選択」も使っていない）

| 役割 | パス |
|---|---|
| primary | `/mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation/output/development/20260918b/development_merged.json` |
| secondary | `/mnt/project/chest/metry/pi6/work/nakamura/ofuna_chuo_report/output/dataset/merged/engineer-set-ETR_ChestMetry_OFC_report-20260924_053010.json` |
| テンプレート | `/mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6/src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml` |

**primary に `20260918b` を選んだ理由**: `20260918` と fingerprint は同じ（`v_78b89657d448`、
入力13本は同一）だが、`b` は 2026-09-18 の目視判定「呼気撮影の疑い」7件（`expiration_suspected`、
`review/review_decisions.csv` に記録）を反映した後の版。`20260918`（10:41 生成）はこの判定より前で
7件を含んだままなので、`b`（42,590画像 / 17,904 annotation）を採る。

**secondary の同一ビルド確認**: `output/csv/sample_labels.csv`（184,919行）と
`output/provenance/provenance.json` は engineer-set（05:30:10）と同じ実行の成果物
（`generated_at` 2026-09-24T05:30:59Z、`rules_version` 全行 `r3`、`inputs_fingerprint` 記録あり、
`verification.violations` 空、`n_reports_read` 180,877）。

### 実行コマンド

```bash
cd /mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation

uv run segmentation-validation export-lpdata \
  --merged output/development/20260918b/development_merged.json \
  --out output/lpdata/20260924/chest_metry_pi6_pneumothorax.json \
  --image-output-dir /mnt/project/chest/metry/pi6/dataset/img_png \
  --mask-output-dir /mnt/project/chest/metry/pi6/dataset/masks/pneumothorax_20260924 \
  --mask-mode generate \
  --dataset-id chest_metry_pi6_pneumothorax_2026_008 \
  --owner kosuke.nakamura

uv run segmentation-validation export-lpdata-ofc \
  --source /mnt/project/chest/metry/pi6/work/nakamura/ofuna_chuo_report/output/dataset/merged/engineer-set-ETR_ChestMetry_OFC_report-20260924_053010.json \
  --out output/lpdata/20260924/ofc_report.lpdata.json \
  --image-output-dir /mnt/project/chest/metry/pi6/dataset/img_png \
  --mask-output-dir output/lpdata/20260924/masks/pneumothorax_ofc \
  --artifact-prefix ofc_ \
  --dataset-id chest_metry_pi6_pneumothorax_2026_009 \
  --owner kosuke.nakamura

uv run segmentation-validation merge-lpdata \
  --primary output/lpdata/20260924/chest_metry_pi6_pneumothorax.json \
  --secondary output/lpdata/20260924/ofc_report.lpdata.json \
  --out output/lpdata/20260924_merged/chest_metry_pi6_pneumothorax.json \
  --template /mnt/.../dataset_template_pneumothorax.yaml \
  --dataset-id chest_metry_pi6_pneumothorax_2026_010 \
  --owner kosuke.nakamura        # 先に --dry-run で裁定を確認してから実行
```

`--image-output-dir` は**必ず指定する**。省くと出力ディレクトリ配下へ 42,590 枚を
再変換し始める（既存の共有PNG 48,847 枚を使わない）。

`dataset_id` は `_2026_001`〜`_2026_007` が使用済みだったので 008 / 009 / 010 を採った
（`_2026_006` は `output/lpdata/20260918b/`、`_2026_007` は
`output/lpdata/20260918b_merged/chest_metry_pi6_pneumothorax_ver2.json` が使用中）。

### 成果物

| 用途 | パス | dataset ID | サンプル数 |
|---|---|---|---:|
| primary | `output/lpdata/20260924/chest_metry_pi6_pneumothorax.json` | `..._2026_008` | 42,590 |
| secondary | `output/lpdata/20260924/ofc_report.lpdata.json` | `..._2026_009` | 6,824 |
| **統合（分割ツールへ渡すもの）** | **`output/lpdata/20260924_merged/chest_metry_pi6_pneumothorax.json`** | **`..._2026_010`** | **48,837** |
| 気胸マスク | `/mnt/project/chest/metry/pi6/dataset/masks/pneumothorax_20260924` | — | 3,524 |

### 検証結果

統合後（`scripts/verify_lpdata.py`）:

```
[structure] テンプレート一致 True / ビルダ一致 True / キー違い 0
[pneumothorax_case]       False 40,823 / True 8,014（うちマスクあり 3,524）
[pleural_effusion_status] absent 1,585 / present 3,258 / unknown 43,994
[abnormal_finding_status] absent 866 / present 6,691 / unknown 41,280
[気胸 false でマスクあり] 0
[patient_id / facility_id / image_file / dicom_file] 欠損 0
[患者] 42,608 / [施設] 35 / [不変条件] 違反 0
```

- **ラベル衝突 0 件。** 裁定 386 件はすべて補強
  （`pneumothorax_side` 335 / `pleural_effusion_status` 169 / `pneumothorax_case` 70 /
  `bulla_bleb_status` 8）。気胸マスクの食い違い 6 件は従来どおり primary 採用。
- primary 単体の胸水は `present 474 / absent 855` で事前の目安と一致。
- 画素重複は前版（`20260918_merged`）と**完全一致**（バイト一致 15 組、増減なし）。
  結果は `output/research/data/duplicate-images_20260924_merged.csv`。

## 上流（ofuna_chuo_report）へ戻す修正

エクスポーターにその場限りの例外処理は入れていない。以下は上流側の修正が要る。

| sample_id | 状況 |
|---|---|
| `CXOFC00045055_002_002_000` | `pleural_effusion_flags` に `resolution_wording` が立っている（本文「みぎ胸水は消失したと考える」）のに `pleural_effusion_status: present` のまま。**消失表現を status へ反映する修正が要る** |
| `CXOFC00005931_025_001_000` | 画像確認で正常と確定した一方、レポートは胸水 `present`（`text_only_mention`）。レポート側のラベルが誤りの可能性 |

どちらも `needs_review: true` なので上流のレビュー対象には載っている。
**両者とも上流の `pneumothorax_status` が `unknown` のため取り込み範囲外となり secondary に入らず、
最終値は primary の明示正常（気胸 `false` / 胸水 `absent`）がそのまま残った。**
レポートの胸水 `present` を裁定で退けた結果ではない点に注意。

## 未解決

- 正常ラベルと所見 annotation が同居する 166 枚は元データの矛盾。胸水では `unknown` に
  逃がしたが、元データ側の是正は別件。
- `output/research/` の分布レポートは 20260917 / 20260918_merged 版のまま。新版で作り直すなら
  `uv run python scripts/lpdata_distribution.py output/lpdata/20260924_merged/chest_metry_pi6_pneumothorax.json`。
