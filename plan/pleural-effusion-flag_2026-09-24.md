# 胸水フラグ（`pleural_effusion_status`）対応と、上流付け直し後の再エクスポート

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
| `normal_evidence`（`No Findings/normal`）の明示正常があり、**かつ所見 annotation が1件も無い** | `absent` |
| それ以外 | `unknown` |

- **`absent` は annotation の明示正常だけが根拠。** アノテーションが無いだけ、または
  レポートの陽性所見が0件（`case_evidence=explicit_negative_report`）というだけでは `absent` にしない。
- 正常ラベルと所見 annotation が同居して矛盾している画像（実データ 166 枚）は **`unknown`**。
  明示正常だけを条件にすると「所見あり」と「胸水は無い」を同時に主張することになる。
  この条件により、胸水 `absent` は `abnormal_finding_status: absent` の部分集合になる。
- **水気胸は気胸 `present` に含める**（上流が `pneumothorax_status: present` +
  `pneumothorax_subtype: hydropneumothorax` で返し、取り込み側は status しか見ない）。
  胸水フラグは水気胸から自動的には立てず、胸水の根拠を見て独立に判定する。
- 値が確定した後は動かさない。レポートが作れるのは `present` だけで、
  `absent` との食い違いは値を維持して `LabelConflict` に残す
  （理由は維持した側で決まる: `absent` を維持＝`explicit_negative_wins` /
  `present` を維持＝`never_downgrade`）。

## 実装（完了。再エクスポートは未実施）

| ファイル | 変更 |
|---|---|
| `core/records.py` | `ReportLabels.pleural_effusion_status` |
| `adapters/engineer_set.py` | `PLEURAL_EFFUSION_OBSERVED`。上流に専用キーが無いので `finding_labels_observed` に `pleural_effusion` が在るかだけを畳む（リストは従来どおり下流へ渡さない） |
| `lpdata_export/labels.py` | `PLEURAL_EFFUSION` / `HYDROPNEUMOTHORAX_SUBTYPE`、`SampleLabels.pleural_effusion_status`、`build_labels` の3値判定 |
| `lpdata_export/sample.py` | `FIELD_BUILDERS` に追加（テンプレートとのキー一致） |
| `lpdata_export/invariants.py` | `PLEURAL_KEY` を label_map 検査へ |
| `lpdata_export/report_labels.py` | レポートによる補強、`_effusion_conflict_reason` |
| `lpdata_export/merge.py` | 統合時の補強、`effusion_counts_after`、summary 節 |
| `lpdata_export/export.py` | 分析用CSVの列（`pleural_effusion_status` / `report_pleural_effusion_status`） |
| `README.md` | ラベル規則の表と不変条件 |
| `tests/` | conftest の `effusion` 引数、labels / report_labels / merge / export に 11 本追加 |

### 実データでの検算（読み取りのみ。書き出しはしていない）

`output/development/20260918/development_merged.json`（42,597画像）を `build_labels` に通した結果:

| 値 | 件数 |
|---|---:|
| `present` | 474 |
| `absent` | **855** |
| `unknown` | 41,268 |

`absent` 855 は `No Findings/normal` のヒット 1,024 枚から、所見 annotation と同居する
166 枚と胸水そのものが付いている 3 枚を除いた数（`abnormal_finding_status: absent` と同数になる）。
施設別の `present` は ofuna 230 / segmed 111 / kajinoki 26 / tokyo_medical 25 / asahikawa 19 ほか。

## 上流（ofuna_chuo_report）待ち

**再エクスポートはしない。** 大船中央レポジトリで気胸・胸水のレポートラベルを付け直しているため、
いま走らせても捨てる版ができる。上流が確定してから下記を実行する。

上流に確認すること:

1. `report_labels` に `pleural_effusion_status` 相当の**専用キー**が入るか。
   入るなら `adapters/engineer_set.py` の `finding_labels_observed` からの畳み込みを
   そちらへ切り替える（`PLEURAL_EFFUSION_OBSERVED` の定数と `_report_labels` の1行だけ）。
2. `schema_version` が上がるか。上がるなら `report_labels.SUPPORTED_SCHEMA_VERSION` を
   追随させるまでエクスポートは意図的に止まる。
3. 胸水の陰性（明示的に「胸水なし」）を返すようになるか。返すなら
   レポート由来の `absent` を認めるかどうかを再判断する（現行は認めない）。

## 上流確定後の再エクスポート手順

**原本・シンボリックリンク先・既存の出力版は変更しない。** 出力は新しいディレクトリと新しい版名に出す
（`<NEW>` は実行日、`dataset_id` は現行の `..._2026_005` の次を採る）。
マスク出力先は入力ごとに必ず分ける（ファイル名が `<sample_id>.png` なので共有すると前版を上書きする）。

```bash
cd /mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation
NEW=2026MMDD    # 実行日

# 1. primary（development 由来）
uv run segmentation-validation export-lpdata \
  --merged output/development/20260918/development_merged.json \
  --out output/lpdata/$NEW/chest_metry_pi6_pneumothorax.json \
  --mask-output-dir /mnt/project/chest/metry/pi6/dataset/masks/pneumothorax_$NEW \
  --mask-mode generate

# 2. secondary（OFC 読影レポート。上流の確定版に差し替える）
uv run segmentation-validation export-lpdata-ofc \
  --source <上流が出した engineer-set-ETR_ChestMetry_OFC_report-*.json の確定版> \
  --out output/lpdata/$NEW/ofc_report.lpdata.json \
  --mask-output-dir output/lpdata/$NEW/masks/pneumothorax_ofc \
  --artifact-prefix ofc_

# 3. 統合
uv run segmentation-validation merge-lpdata \
  --primary output/lpdata/$NEW/chest_metry_pi6_pneumothorax.json \
  --secondary output/lpdata/$NEW/ofc_report.lpdata.json \
  --out output/lpdata/${NEW}_merged/chest_metry_pi6_pneumothorax.json \
  --template /mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6/src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml \
  --dataset-id chest_metry_pi6_pneumothorax_2026_006 \
  --dry-run   # まず裁定だけ見る。問題なければ --dry-run を外す
```

### 検証項目

1. `meta.structure` に `pleural_effusion_status` があり、全サンプルが 3 値のいずれかを持つ
   （`merge` の不変条件チェックが違反0件で通ること）。
2. `pleural_effusion_status` の分布。**primary 側は present 474 / absent 855 が目安**
   （上流の付け直しは secondary にしか効かないので、primary はこの数字から動かないはず。
   動いたら `development_merged.json` の作り直しが混ざっている）。
3. `absent` が `abnormal_finding_status: absent` の部分集合であること。
4. `pleural_effusion_status: absent` かつ `pneumothorax_case: true` が0件であること
   （明示正常は気胸陰性でもあるので、出たら `normal_evidence` の扱いが壊れている）。
5. merge summary の「胸水の食い違い」（`explicit_negative_wins` / `never_downgrade`）を目視。
   件数が多ければ上流の陰性定義とこちらの明示正常が食い違っている。
6. 水気胸: `ofc_report_labels.csv` の `pneumothorax_subtype=hydropneumothorax` が
   すべて `pneumothorax_case: true` で入っていること。胸水フラグは独立に判定されるので、
   水気胸だからといって `present` になっていないこと。
7. `output/research/` の分布レポートを新版で作り直す
   （`scripts/lpdata_distribution.py <新しいJSON>`）。

## 未解決

### 現時点で出る胸水の食い違い（2 件）

上流の付け直し前の版（`...OFC_report-20260917_004313.json`）との突き合わせで、
primary が `absent`（明示正常）／レポートが胸水陽性になるのは **2 件だけ**。
両方とも**元データ側の問題**で、こちらの裁定規則の問題ではない。
（一致する側は 181 件: primary `present` × レポート陽性。）

| sample_id | 内容 |
|---|---|
| `CXOFC00005931_025_001_000` | レポートは「ひだり肺門下部の液面形成 hydro-pneumothorax」「chest tube 挿入」「みぎに微量の胸水」と書いている一方、**annotation 側には `No Findings/normal` が付いていて所見が1件も無い**。明示正常のほうが誤っている可能性が高い。`pneumothorax_case` も `explicit_negative_wins` で `false` に固定されるので、**胸水だけでなく気胸の判定にも影響する**。annotation の正常ラベルを見直す対象 |
| `CXOFC00045055_002_002_000` | レポートの胸水は `probable` で、本文が「**みぎ胸水は消失したと考える**」。上流が消失表現を拾えずに present を立てている（`output/research/lpdata_distribution_20260917.md` §12 の `resolution_wording` 取りこぼしと同じ問題）。annotation の `absent` のほうが妥当 |

上流の付け直しで 2 の類は減るはず。再エクスポート後にこの 2 件が残るかを確認する。

### そのほか

- レポート由来の胸水は現状 `finding_labels_observed` の1語に依存している。上流が専用キーを
  持つまでは、上流の語彙が変わると黙って `unknown` に倒れる（テストは合成データで固定しているが、
  実データ側の検知手段が無い）。上流確定時に 1 の確認を必ず行う。
- 正常ラベルと所見 annotation が同居する 166 枚は元データの矛盾。胸水では `unknown` に
  逃がしたが、元データ側の是正は別件。
