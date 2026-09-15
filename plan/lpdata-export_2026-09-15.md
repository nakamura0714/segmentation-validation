# development_merged.json → lp-data 形式 JSON エクスポータ

## Context

`build-dataset` が生成する `development_merged.json`（42,574画像 / 17,933 annotation / 12データセット統合）は
レガシーな engineer-set 形式のままで、学習リポジトリ
[med-chest-metry-pi6](/mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6) が読める形ではない。
学習側は lp-data (`lpdata` v2.0.0) の標準データセット形式を前提としており、
属性の型と意味は **PR #68 で確定した `dataset_template_pneumothorax.yaml` が正典**。

本変更は、そのテンプレートの `meta.structure` に準拠した lp-data 形式 JSON を
`development_merged.json` から生成する。あわせて対応する DICOM から 16bit PNG を変換する工程も
同じエクスポートから実行できるようにする。
`src/segmentation_validation/lpdata_export/` を新規パッケージとして、検証・採否（`checks/` / `selection/`）
から分離する。

**今回のラベルは既存 annotation と明示的な正常情報だけで作る。読影レポートは使わない。**
読影レポート解析結果は後日CSVで受け取る想定で、それを入力に既存の lp-data JSON のラベルを
更新する処理は**独立した後続機能**として分離する（本変更では seam だけ用意し、実装しない）。

---

## ラベルの初期生成規則

### `finding_labels`（所見の列挙）

file レベル geometry annotation のうち **`code_system == "Findings"`** のものの `code_text_eng` を
重複排除・ソートして並べる。無ければ `[]`。

元データの `Findings` 語彙は16語（`nodule` 6,567 / `interstitial_opacity` 4,432 /
`pneumothorax` 3,721 / `infiltrative_shadow` 2,553 / `multiple_granular_shadows` 2,238 /
`pleural_effusion` 1,174 / `atelectasis` 968 / `fracture` 600 / `bulla_bleb` 599 / `cavity` 575 /
`mediastinum_enlargement` 411 / `hyperinflation` 257 / `hilar_expansion` 139 /
`mediastinal_lymphadenopathy` 133 / `aortic_protrusion` 92 / `pneumomediastinum` 2）。

**所見として採らない code_system**（geometry annotation に付いているが異常所見ではない）:

| code_system | 中身 | 除外理由 |
|---|---|---|
| `Grade` | code_text_eng なし（5,242件） | 重症度の修飾。所見名ではない |
| `Location` | `lung_apex` / `wall` / `heart` / `diaphragm` | 位置の修飾 |
| `Difficulty`（`" Difficulty"` の前置空白タイポ含む） | `difficulty_00`〜`05` | 読影難易度。**異常所見ではない** |
| `Body Parts` | `body_lung` / `fp_body_heart` / … | 解剖構造 |
| `FP` | `nipple` / `calcification` / `fp_*` | **偽陽性として明示されたもの**。むしろ所見でない記録 |
| `Disease` / `Disease Evolution` | `tb_active` / `disease_progress` 等 | 疾患名・経過であって所見名ではない。読影レポート側の情報に近く、今回は保留 |
| `No Findings` | `normal` / `other` / `findings_in_CT` 等 | 正常側 |

採用する code_system は **config の allowlist**（既定 `["Findings"]`）で持ち、コードに埋め込まない。

### `abnormal_finding_status`

| 条件 | 値 | 実測 |
|---|---|---|
| `finding_labels != []`（＝信頼できる異常所見の geometry annotation が1件以上） | `present` | 6,680 |
| `finding_labels == []` かつ 明確な正常情報がある | `absent` | 853 |
| それ以外 | `unknown` | 35,041 |

**「明確な正常情報」＝ 正常エビデンスの allowlist に一致するラベルが geometry / series / study の
いずれかに付いていること。** 既定は `["No Findings/normal"]` のみ（config で変更可）。

`No Findings` の他の値は正常の根拠に**しない**（`bad_image_unreadable` 読影不能 /
`findings_in_CT` CTでは所見あり / `other` / `non_target_finding` / `dr_interest` / `dr_no_interest` /
code_text_eng なし）。いずれも「正常」を意味しないため `unknown` に倒す。

`StudyAnno/normal`（1,806件）は allowlist に**入れない**（既定）。由来が確認できておらず、
`StudyAnno/abnormal` を `present` の根拠にしない以上、正常側だけ採ると非対称になるため。
根拠が確認できたら config に1行足すだけで `absent` が 853 → 1,500 に増える（実測済み）。
summary に「allowlist に入っていない正常らしきラベルの件数」を出して判断材料にする。

**厳守**:
- annotation が無いことだけを理由に `absent` にしない
- マスクが無いことだけを理由に正常とみなさない
- データセット名（`ChestMetry_PI6px_normal` 等）を根拠にしない
- `StudyAnno/abnormal`（700件）を `present` の根拠にしない
  （これを present にすると `finding_labels` は空のままなので不変条件1を破る）

### `pneumothorax_case`

`finding_labels` に `pneumothorax` を含めば `true`（3,521件）、それ以外は `false`。

⚠️ **既知の取りこぼし**: 気胸症例だがマスク未アノテーションの画像は `false` になる。
`PTE_CX_MT_PI3_pneumothorax` は 1,228件中 1,200件が geometry annotation を持たない。
データセット名からは気胸症例に見えるが、名前を根拠にはしない（推測になる）。
summary に「データセット名に `pneumothorax` を含むが気胸 annotation 0件」の一覧と件数を必ず出し、
**後日のレポートCSV更新処理で補正する対象**として明示する。

### `bulla_bleb_status`

`finding_labels` に `bulla_bleb` を含めば `present`（234件）、それ以外は `unknown`。
**`absent` は今回出さない**（bulla / bleb がアノテーション対象だったかを既存 annotation からは
判定できず、`absent` と `unknown` を区別する根拠が無いため）。

### `pneumothorax_side`

常に `null`。テンプレートは**患者基準の解剖学的左右**と明記しており、画像座標から起こすと全反転する。
後日のレポートCSV更新処理の担当。

### 不変条件（PR #68 のテンプレート lines 122-138 が正典）

1. `abnormal_finding_status == "present"` ⇔ `finding_labels != []`
2. `abnormal_finding_status == "absent"` ⇔ `finding_labels == []`
3. `abnormal_finding_status == "unknown"` ⇒ `finding_labels == []`
4. **`abnormal_finding_status ∈ {present, absent}` のときに限り** `pneumothorax_case == ("pneumothorax" in finding_labels)`
5. `abnormal_finding_status == "absent"` かつ `pneumothorax_case: true` は不正
6. **`unknown` のときは 4 を課さない。** `unknown` × `true` × `[]` は正当な組み合わせ

上記の初期生成規則では **1〜6 はすべて構成上自動的に成立する**（実測の (status, case) 分布は
`present/true` 3,521 / `present/false` 3,159 / `absent/false` 853 / `unknown/false` 35,041 で、
`absent/true` と `unknown/true` は 0件）。

**不変条件6が成立する組み合わせ（`unknown` × `pneumothorax_case: true`）は初期エクスポートでは出ない。**
これは正常であって不具合ではない ―― 「気胸ラベルはあるが読影所見は未取得」を表現できるのは
レポートCSV更新処理が入ってからになる。誤解を招かないよう summary と README に明記する。

検査コード自体は `invariants.py` に純関数として置く。初期エクスポートでは常に0件だが、
**後続のラベル更新処理が同じ検査を再利用する**ための資産であり、回帰の検知にもなる。

---

## 後続機能との分離（本変更では seam だけ用意）

後日「読影レポート解析結果CSV → 既存 lp-data JSON のラベル更新」を独立機能として足せるようにする。
更新対象は `abnormal_finding_status` / `finding_labels` / `pneumothorax_case` /
`pneumothorax_side` / `bulla_bleb_status`。

今回用意しておく seam:

| seam | 内容 |
|---|---|
| 結合キー | `sample_id`（merged JSON の file_list キー ＝ DICOM ファイル名の stem）。全42,574件一意を実測で確認済み。CSV の結合キーはこれにすると README に明記する |
| `reader.py` | 自分が書いた lp-data JSON を plain dict として読み戻す（`lpdata` に依存しない） |
| `writer.py` | **どこで組み立てた dataset dict でも書ける**ようにし、merged JSON 由来の builder から独立させる |
| `invariants.py` | 任意の sample dict を検査する純関数。更新処理がそのまま使う |
| `meta.provenance.label_source` | 初期エクスポートは `"annotations"`。更新処理が `"annotations+report_csv"` 等に書き換えられるようにしておく |

**今回は CSV の読み込み・マッピング・更新ロジックを一切実装しない。**

---

## 決定事項

| 論点 | 決定 |
|---|---|
| ラベル | 既存 annotation ＋ 明示的な正常情報のみ。読影レポートは使わない |
| `image_file` | **今回の工程で DICOM→16bit PNG を変換し、生成した実ファイルを参照する。** 出力先は `--image-output-dir` |
| DICOM が無い場合 | **skip**（`image_file: null` でサンプルは出す）。空画像は絶対に作らない。`--on-missing-dicom error` で停止に切替可 |
| `dicom_file` | `image_path` を `/mnt` 基準で解決した絶対パス |
| 結合気胸マスク | 既定は生成せず予定パスのみ記録。`--mask-mode generate` で `--mask-output-dir` へ書き出す |
| 出力先 | JSON / 画像 / マスクを**独立に指定**（`--out` / `--image-output-dir` / `--mask-output-dir`） |
| 対象範囲 | merged JSON の全 42,574 画像を 1 ファイルへ。split は扱わず `meta.split` は書かない |
| 導出値 | マスクを実際に読んで計算する |
| Notebook | `notebooks/lpdata_export.ipynb` を追加。**ロジックは持たず**公開APIを呼ぶだけ |

---

## 設計の要点

### 1. `meta.structure` はコードで宣言せず、テンプレートから丸写しする

`structure` を Python 側に書き直すと、テンプレート更新時に**黙って古いスキーマを出し続ける**。
実際、本プラン作成中にテンプレートは4回更新され（`af4de52` `f14d46e` `217d945` `f98c633`）、
`lung_rect` の算出規則が「片方だけでも使う」→「**両方取れるときだけ**」に変わっている。

テンプレート YAML を読んで `meta.structure` をそのまま出力し、サンプル組み立て側が
structure のキーを過不足なく埋めているかを起動時に検証する（`validate_coverage`）。
属性が増えたら実行が即座に落ちる。

`defaults: {keep_margin: true}` を正しく引き継ぐためにも必要。`defaults` は lp-data v2.0.0 の仕様だが、
本リポジトリの [`docs/dataset_format.md`](docs/dataset_format.md) は `defaults` / `length` を含まない
古いスナップショットなので、手で書くと確実に落とす。

→ **`pyyaml>=6.0` を依存に追加**（テンプレート読み込み用。出力は JSON のみ）。

### 2. `lpdata` にも `chest-metry-pi6` にも依存しない

- `lpdata` は `opencv-python>=4.10,<5` を要求する。本リポジトリは
  [`pyproject.toml:11-13`](pyproject.toml#L11-L13) のコメントどおり `opencv-python-headless` に統一しており、
  同一環境に両方を入れない方針
- `chest-metry-pi6` は torch / lightning / lpdata を引く。さらに
  `chest_metry_pi6.dataprep.__init__` が `dicom_to_png` を eager import するため pydicom extra も要る

→ どちらも import せず、`docs/dataset_format.md` + テンプレートに沿って plain dict → `json.dump`。
`lpdata` での読み戻し検証は med-chest-metry-pi6 側の環境で行う（後述「検証」）。

### 3. DICOM→PNG は既存実装を移植する（独自仕様を作らない）

[`dataprep/dicom_to_png.py`](/mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6/src/chest_metry_pi6/dataprep/dicom_to_png.py)
の `convert_dicom_to_png()` が唯一の正典。ライブラリ関数として import 可能だが、上記2の理由で
パッケージ依存は張れないため、**同等の実装を `core/imageio.py` へ移植**する
（pydicom を import してよいのは [`tests/test_architecture.py:30-33`](tests/test_architecture.py#L30-L33) により
`core/imageio.py` と `review/export_assets.py` だけ）。

移植する処理（順序が意味を持つので変えない）:

1. `pydicom.dcmread` → `ds.pixel_array`
2. `PhotometricInterpretation == "MONOCHROME1"` なら **modality LUT 適用前の stored 値空間で**
   `((1 << BitsStored) - 1) - raw` で反転（per-image の max ではなく bit 深度由来の固定レンジ）
3. `pydicom.pixels.apply_modality_lut(raw, ds)`
4. `np.clip(array, 0, 65535).astype(np.uint16)`（スケール変換・min-max 正規化はしない）
5. `cv2.imwrite`（uint16 PNG）

移植元と同じ provenance dict（`sop_instance_uid` / `bits_stored` / `photometric_interpretation` /
`inverted_monochrome1` / `pixel_spacing` / `output_dtype`）を返し JSONL へ追記する。

**drift 検知**: 移植元ファイルを読み、`convert_dicom_to_png` の本体が変わっていたら落ちるテストを
`realdata` マーカーで置く。

**既知の未解決点（本変更では解消しない）**: テンプレートの `image_file` 記述より、サーバー上の
現行 PNG は社内ライブラリ med-dicom 経由で作られており、`prepare_data dicom2png` とは別経路で
**画素値が一致するかは未確認（med-chest-metry-pi6 Issue #27）**。本エクスポータは
`prepare_data` 側（リポジトリ内に実装があり文書化されている方）に揃え、provenance に記録する。

**規模**: 全 42,574 枚で uint16 生データ 251 GiB、PNG 出力は**約 113 GiB**（圧縮率0.45見積）。
`/mnt/project` の空きは 126T なので容量は足りるが、NFS 越しに 251 GiB を読む長時間ジョブになる。
→ 既存出力を skip する冪等実装、`--jobs` 並列、`--limit` / `--only-dataset` での部分実行を必須にする。

### 4. 元JSONの階層は adapters/ にだけ知らせる

[`tests/test_architecture.py:125`](tests/test_architecture.py#L125) が `"series_list"` / `"file_list"` の
直接参照を `adapters/` 等以外で禁止している。`lpdata_export/` は `EngineerSetAdapter` 経由で
`FileGroup` / `AnnotationRecord` を受け取る。

`development_merged.json` は engineer-set とほぼ同形だが、**file entry に `dataset_id` が付く**点だけが違う。
[`adapters/engineer_set.py`](src/segmentation_validation/adapters/engineer_set.py) の `iter_files()` で
`file_rec.get("dataset_id") or self.dataset_id` を使うよう最小の変更をする（新規アダプタは作らない）。

---

## フィールド対応表

sample_id = merged JSON の **file_list のキー**（例 `CXASW00001466_002_000_000`）。
42,574件すべて一意かつ DICOM ファイル名の stem と一致することを実測で確認済み。
med-chest-metry-pi6 の `tests/test_dataset_template.py` が
`Path(sample["image_file"]).stem == sample_id` を検査するので、この一致は必須。

| lp-data 属性 | type | 由来 | 規則・実測 |
|---|---|---|---|
| `patient_id` | `str` | `study.patient_id` | 空文字は `null` |
| `dicom_file` | `Path` | `file.image_path` | `core/paths.resolve_image_path`（`/mnt` 基準）の**絶対パス** |
| `image_file` | `Path` | DICOM 変換結果 | `--image-output-dir` 配下の `<sample_id>.png`。変換できなければ `null` |
| `pixel_spacing` | `SpatialResolution` | `series.spacing` | `{x, y}` のみ（`z` を書くと 3D 扱い）。x/y 欠損の series 3件は属性ごと `null` |
| `image_shape` | `dict` | `series.shape` | `{height: h, width: w}` |
| `facility_id` | `str` | institution キー | 35施設 |
| `vendor_id` | `str` | `series.manufacturer` | `null` 15,845件はそのまま `null` |
| `view_position` | `str` | series の `CRSeriesTypes` | PA 22,689 / AP 4,393 / 残り 15,492件は `null` |
| `pneumothorax_case` | `bool` | Findings/pneumothorax | `true` 3,521 |
| `abnormal_finding_status` | `str` | Findings ＋ 正常エビデンス | present 6,680 / absent 853 / unknown 35,041 |
| `finding_labels` | `str[]` | Findings のみ | 非空 6,680 |
| `pneumothorax_side` | `str` | — | 常に `null` |
| `bulla_bleb_status` | `str` | Findings/bulla_bleb | present 234 / 他は `unknown` |
| `pneumothorax_mask` | `Mask2D` | 気胸マスク | 非ゼロ領域があれば `{"pixel_array": "<パス>"}`、無ければ `{"pixel_array": null}` |
| `lung_mask` | `Mask2D` | 参照マスク | 実在かつ非ゼロなら `{"pixel_array": "<絶対パス>"}`、他は `{"pixel_array": null}` |
| `thorax_mask` | `Mask2D` | 参照マスク | 同上 |
| `lung_rect` | `Rect` | lung + thorax | **両方から BBox が取れるときだけ**算出。片方でも欠ければ属性ごと `null` |
| `pneumothorax_area_pix2` | `int` | 結合マスク | OR 合成後の非ゼロ画素数。空なら `null` |
| `pneumothorax_area_mm2` | `float` | 同上 × spacing | `core/geometry.pixels_to_mm2`。spacing 欠損なら `null` |

### 落とし穴（すべてテストで固定する）

1. **ラベルは `code` ではなく `code_text_eng` で判定する。**
   実データで `Findings/001` が `nodule`（6,567件）と `pneumothorax`（2,667件）の**両方**に使われている。
   気胸ラベルは `Findings/001` と `Findings/010` の2種類にまたがる。

2. **`Rect` は半開区間、`mask_stats.bbox` は閉区間。**
   [`core/geometry.py:68`](src/segmentation_validation/core/geometry.py#L68) の bbox は max を含む。
   → `x_max = bbox[2] + 1`, `y_max = bbox[3] + 1`。忘れても lp-data は例外を出さない
   （1px 小さい矩形が黙って通る）ので専用テストを置く。

3. **`lung_rect` は lung と thorax の両方が要る**（テンプレート更新で規則が変わった箇所）。
   片方だけで代用すると、肺野のみ由来の矩形が同じ属性に混ざり、由来を記録する手段が無いまま
   属性の意味がサンプルごとに変わる。実測被覆は lung+thorax+mediastinum 13,606 /
   lung のみ 3,619（走査キャッシュ全52,160件）→ **`lung_rect` が埋まるのは全体の1/3以下**。

4. **実体を置くマスクは非ゼロ領域を持つものに限る**（テンプレートの規約）。
   全ゼロの PNG は置かず `pixel_array: null` にする。マスクを読むまで判別できないので、
   `--no-measure` 時はこの規約を満たせない（下見専用である旨を meta と summary に明記）。

5. **`multiple: false` の属性値はリストにしない。** `finding_labels` だけが `multiple: true`。

6. **`pixel_array: null` と属性ごとの `null` は別物。**
   マスク3種は必ず `{"pixel_array": ...}` の dict で書く（キーを省くと lp-data が `TypeError`）。
   `lung_rect` だけは `Rect` に空表現が無いため属性ごと `null`。

7. **面積は結合後に数える。** 1画像に気胸マスクが複数ある例が 113件（2枚95 / 3枚16 / 4枚2）。
   各マスクの面積を足すと重なり分を二重計上する。

8. **所見語彙はそのまま出す。** `pneumothorax` は不変条件4が文字列一致で見るのでこの綴りのまま出す
   （元データの `code_text_eng` と一致することを確認済み）。一方テンプレートの例は `bulla` / `bleb` と
   分けて書いており、元データの `bulla_bleb`（結合語）と食い違う。**語彙の対応付けは推測せず**、
   元の `code_text_eng` をそのまま出したうえで summary に語彙一覧を出し、学習側と突き合わせる。

---

## モジュール分割

`src/segmentation_validation/lpdata_export/`

| モジュール | 責務 |
|---|---|
| `__init__.py` | 公開API。`ExportOptions` / `export_lpdata(config, options) -> ExportResult` と、Notebook の確認セル用に `load_template` / `summarize` を再export |
| `options.py` | `ExportOptions` dataclass（CLI と Notebook の共通入力）と `ExportResult` |
| `template.py` | テンプレート YAML を読み `meta.structure` と meta 既定値を取り出す。`validate_coverage(structure, builder_keys)` で過不足を検査し `TemplateDriftError`。未解決の `<...>` placeholder 検出 |
| `labels.py` | **純関数・I/Oなし。** `FileGroup` + allowlist 設定 → 4つのラベル属性 |
| `invariants.py` | **純関数。** 不変条件1〜6の検査。`unknown` のときの緩和をここに閉じる。後続のラベル更新処理が再利用する |
| `paths.py` | `sample_id_for` / 出力パスの組み立て / `--path-style` に従った記録形式の決定 |
| `images.py` | DICOM→16bit PNG 変換のドライバ。`core/imageio.convert_dicom_to_png` を呼び、既存出力を skip、provenance JSONL を追記。DICOM 欠損時の方針（skip / error）を持つ |
| `masks.py` | 気胸マスクの OR 合成。`--mask-mode generate` なら PNG 書き出し、それ以外は測定のみ。全ゼロは「マスク無し」扱い |
| `measure.py` | 画素パスの取りまとめ。1 `FileGroup` → `SampleMeasurement`（結合マスクの画素数・面積、lung/thorax の bbox、`lung_rect`）。`core/measure.scan` と同じ `ThreadPoolExecutor` + `core/cpu.limit_native_threads` + `core/progress.Progress`。JSONL キャッシュで再実行時に再走査しない |
| `sample.py` | フィールドビルダのレジストリ `FIELD_BUILDERS: dict[str, Callable]`。`template.validate_coverage` が見るのはこのキー集合 |
| `writer.py` | dataset dict を JSON へ逐次書き出し（42,574件を溜め込まない）。`lpdata.io.save_json` に揃え `indent=4, sort_keys=True`。**merged JSON 由来の builder から独立**（後続のラベル更新処理が再利用する） |
| `reader.py` | 自分が書いた lp-data JSON を plain dict として読み戻す。後続のラベル更新処理の入口になる seam |
| `manifest.py` | `mask_merge_manifest.json`。各 sample の結合マスク出力パスと元マスクの絶対パス一覧・期待画素数。`--mask-mode planned` でも後工程が再現できる |
| `report.py` | `lpdata_export_summary.md`。属性別 null 率 / ラベル分布 / 不変条件違反 / 参照マスク被覆 / 所見語彙一覧 / 画像変換の成否内訳 / **後続CSV更新で補正すべき候補**（気胸データセット名だが annotation 0件、allowlist 外の正常らしきラベル） |

`core/imageio.py` に追加: `convert_dicom_to_png(dicom_path, png_path) -> dict`（移植）と `UINT16_MAX`。

**ポリシーJSONファイルは作らない**（前案の `review_policy/lpdata_label_policy.json` は撤回）。
データセット別の判断を持ち込まない方針になったため、必要な設定は config の2つの allowlist だけで足りる。

再利用する既存資産（新規に書かない）:
[`adapters/engineer_set.py`](src/segmentation_validation/adapters/engineer_set.py) /
[`core/records.py`](src/segmentation_validation/core/records.py) /
[`core/paths.py`](src/segmentation_validation/core/paths.py)（`resolve_image_path` / `reference_mask_path`）/
[`core/imageio.py`](src/segmentation_validation/core/imageio.py)（`load_binary_mask` / `load_reference_mask`）/
[`core/geometry.py`](src/segmentation_validation/core/geometry.py)（`mask_stats` / `pixels_to_mm2`）/
[`core/cpu.py`](src/segmentation_validation/core/cpu.py) /
[`core/progress.py`](src/segmentation_validation/core/progress.py) /
[`core/labels.py`](src/segmentation_validation/core/labels.py)

---

## CLI

[`cli.py`](src/segmentation_validation/cli.py) の `build_parser()` に1サブコマンド、
`main()` の `handlers` dict に1エントリ。ハンドラは既存の流儀どおり
`_export_lpdata(config, args) -> int` で、`ExportOptions` を組み立てて
`lpdata_export.export_lpdata()` を呼ぶだけにする（**ロジックは CLI に置かない** — Notebook と共有するため）。

```
segmentation-validation export-lpdata
    # --- 入力 ---
    --merged PATH              # 既定: config.development_dir/<最新tag>/development_merged.json
    --template PATH            # 既定: config.lpdata_export.template_path
    # --- 出力先（3つとも独立） ---
    --out PATH                 # lp-data JSON
    --image-output-dir PATH    # DICOM変換PNG
    --mask-output-dir PATH     # 結合気胸マスク
    --path-style {relative,absolute}      # 既定 relative
    # --- モード ---
    --image-mode {convert,planned,none}   # 既定 convert
    --mask-mode  {generate,planned,none}  # 既定 planned
    --on-missing-dicom {skip,error}       # 既定 skip
    --no-measure               # 画素を読まず導出値を全てnullにする（下見用）
    # --- 実行制御 ---
    --jobs N                   # 既定 8（scan と揃える）
    --limit N / --only-dataset ID（繰り返し可） / --force
    # --- meta の必須項目 ---
    --dataset-name / --dataset-id / --owner
```

`config.py` に `LpdataExportConfig` を追加し、`Config.lpdata_dir` プロパティを生やす
（`validation_dir` / `development_dir` と同じ形）。

```python
@dataclass(frozen=True)
class LpdataExportConfig:
    template_path: str = "../med-chest-metry-pi6/src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml"
    output_dirname: str = "lpdata"
    image_dirname: str = "images"
    mask_dirname: str = "masks/pneumothorax"
    # 所見として採る code_system。Grade/Location/Difficulty/Body Parts/FP/Disease は所見名ではない。
    finding_code_systems: list[str] = field(default_factory=lambda: ["Findings"])
    # 「明確な正常」と認める "<code_system>/<code_text_eng>"。根拠が確認できたものだけを足す。
    # StudyAnno/normal は由来未確認のため既定では入れない（足すと absent が 853 → 1,500 になる）。
    normal_evidence: list[str] = field(default_factory=lambda: ["No Findings/normal"])
```

### パスの記録形式

`--path-style relative`（既定）のとき、**JSON 出力ファイルの親ディレクトリ**を基準に相対化する。
基準の外にあるものは絶対パスのままにし、その旨を summary に出す。

| 属性 | 既定での記録 |
|---|---|
| `dicom_file` | 常に絶対（`/mnt/medical2/...` は読み取り専用の原本で出力先の外） |
| `image_file` | `--image-output-dir` が JSON と同階層なら `images/<id>.png`、外なら絶対 |
| `pneumothorax_mask.pixel_array` | 同上（`masks/pneumothorax/<id>.png`） |
| `lung_mask` / `thorax_mask` | 常に絶対（`/mnt/medicaldb/processed/...`） |

学習側 `load_dataset_file` は `data.image_root` かデータセットファイルの親を `root_dir` に渡すので、
相対パスはその基準で解決される（`data/README.md`）。

### meta

テンプレートの meta を引き継ぎつつ `content_type: dataset` を常に付け、`split` は書かない。
`owner: <owner>` のような未解決 placeholder が残っていたらエラーで停止。`date` は生成日。
任意キーとして `provenance` を足す（merged JSON のパス・`fingerprint`・sha256・各モードの値・
`measure` の有無・ツール版・**`label_source: "annotations"`**）。

終了コード: `EXIT_OK` / `EXIT_ISSUES`（不変条件違反あり） / `EXIT_FAILURE`（設定・入力の不備）。

---

## Notebook

`notebooks/lpdata_export.ipynb`。既存 `notebooks/pipeline.ipynb` / `inspect_case.ipynb` と同じ流儀
（先頭で `os.chdir(PROJECT_ROOT)`、以降は公開APIを呼ぶだけ）。

**変換ロジックは1行も置かない。** 正本は `lpdata_export` パッケージ側。

冒頭の設定セル（ここだけ書き換えれば以降の全セルに反映される）:

```python
MERGED_JSON_PATH  = "output/development/20260914_merged/development_merged.json"
TEMPLATE_PATH     = ".../med-chest-metry-pi6/src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml"
JSON_OUTPUT_PATH  = "output/lpdata/20260914_merged/chest_metry_pi6_pneumothorax.json"
IMAGE_OUTPUT_DIR  = "output/lpdata/20260914_merged/images"
MASK_OUTPUT_DIR   = "output/lpdata/20260914_merged/masks/pneumothorax"
JOBS              = 8
IMAGE_MODE        = "planned"    # convert / planned / none
MASK_MODE         = "planned"    # generate / planned / none
NO_MEASURE        = True         # 高速確認モード
LIMIT             = 200          # None で全件
```

セル構成:

1. 設定（上記）+ `ExportOptions` の組み立て
2. 入力件数・データセット一覧の確認（`EngineerSetAdapter` で数えるだけ。画素は読まない）
3. **ラベル規則の確認** — 採用する `finding_code_systems` / `normal_evidence` の現在値と、
   元データに実在する code_system 一覧・正常らしきラベルの件数を並べて表示する
   （allowlist に入れるか判断する材料。ポリシーJSONの代わり）
4. エクスポート実行（`export_lpdata(config, options)`）
5. DICOM画像変換（`IMAGE_MODE="convert"` に変えて再実行）。
   所要時間と容量の目安〔全件で約113 GiB〕を markdown で明記し、まず `LIMIT` 付きで試すよう促す
6. 結合マスク生成（`MASK_MODE="generate"`）
7. summary 表示（`lpdata_export_summary.md` を Markdown レンダリング）
8. 集計確認 — `abnormal_finding_status` × `pneumothorax_case` のクロス集計、属性別 null 率、
   不変条件違反の件数、**後続CSV更新で補正すべき候補の一覧**
9. 出力ファイルを数件確認（先頭数件を pretty print、参照先ファイルの存在確認）

---

## テスト構成

既存の流儀に合わせる: 合成データを `tmp_path` に書く / テスト関数名は日本語 /
`tests/conftest.py` の `make_group` / `make_record` / `Label` 定数を再利用。

`tests/test_lpdata_labels.py`（純関数。ラベル規則の担保はここが主）

- `test_Findings注釈があればpresent`
- `test_明示のNoFindingsNormalがあればabsent`（geometry / series / study の3経路）
- `test_annotationが無いだけではabsentにしない` ★ 既定は `unknown`
- `test_マスクが無いだけでは正常とみなさない`
- `test_Difficultyはpresentの根拠にしない` — `Grade` / `Location` / `Body Parts` / `FP` /
  `Disease` も同様にパラメトライズ
- `test_FPラベルは所見に入らない`
- `test_StudyAnnoのabnormalはpresentにしない`
- `test_NoFindingsのnormal以外は正常の根拠にしない` — `bad_image_unreadable` / `findings_in_CT` / `other`
- `test_normal_evidenceのallowlistで判定が変わる` — `StudyAnno/normal` を足すと `absent` になること
- `test_気胸ラベルはcode_text_engで判定する` — `Findings/001 nodule` と `Findings/001 pneumothorax` を
  混ぜ、`code` に引きずられないこと
- `test_bulla_blebはpresentかunknownだけでabsentを出さない`
- `test_pneumothorax_sideは常にnull`

`tests/test_lpdata_invariants.py`（後続のラベル更新処理と共用する検査）

- `test_初期エクスポートの全サンプルが不変条件を満たす`（合成データで網羅）
- `test_absentかつpneumothorax_case_trueは違反として検出される`
- `test_presentのときだけ気胸ラベルの対応を課す`
- `test_unknownかつpneumothorax_case_trueかつfinding_labels空は違反にしない` ★PR #68 の核心。
  初期エクスポートでは出ないが、検査側は許容すること

`tests/test_lpdata_export.py`（組み立て）

- `test_structureの全キーがサンプルに存在する`（`multiple: true` は `[]`、他は `null`）
- `test_finding_labelsだけがリストになる`
- `test_lung_rectは半開区間になる` — 閉区間 `(10,20,99,119)` → `x_max: 100, y_max: 120`
- `test_lung_rectは両方のマスクが要る` — 片方だけなら属性ごと `null`
- `test_複数の気胸マスクはOR合成してから面積を数える` — 重なりのある2枚で和にならないこと
- `test_全ゼロのマスクはpixel_array_nullになる`
- `test_マスクなしでも属性は消えずpixel_array_nullになる`
- `test_spacingが無ければarea_mm2はnull`
- `test_sample_idはimage_fileのstemと一致する`
- `test_統合JSONのfile単位dataset_idが引き当てに使われる` —
  `tests/test_merged_dataset.py:35` の `write_source` / `merge_all` を流用
- `test_書き出したJSONをreaderで読み戻せる` — 後続のラベル更新処理の seam

`tests/test_lpdata_paths.py`

- `test_出力先配下は相対パスで記録される` / `test_配下でなければ絶対パスになる`
- `test_dicom_fileと参照マスクは常に絶対` / `test_path_style_absoluteなら全て絶対`

`tests/test_lpdata_images.py`

- `test_16bitPNGとして書き出される` — 合成 DICOM を `tmp_path` に作り、round-trip が `uint16` で画素一致
- `test_MONOCHROME1はBitsStored由来の固定レンジで反転する` — per-image max でないこと
- `test_DICOMが無ければskipしてimage_fileはnull` / `test_on_missing_dicom_errorなら停止する`
- `test_既存PNGは再変換しない` / `test_forceなら作り直す`
- `test_変換失敗時に出力ファイルを残さない` — 空画像を作らないこと

`tests/test_lpdata_template.py`

- `test_テンプレートのstructureをそのまま出力する`（`defaults: {keep_margin: true}` が落ちないこと）
- `test_structureに属性が増えたら落ちる` / `test_ビルダが余分なキーを持っていたら落ちる`
- `test_未解決のplaceholderがあれば止まる`
- `@pytest.mark.realdata test_実テンプレートとビルダのキーが一致する` — PR #68 のテンプレートへの drift 検知
- `@pytest.mark.realdata test_DICOM変換が移植元と同じ実装である`

`tests/test_cli.py` に追記: `test_export_lpdataがhandlersに登録されている`

`tests/test_architecture.py` は**変更不要**（`core/imageio` 経由で読み書きし、`series_list` を直接参照しない）。
逆に変更が必要になったら設計が崩れた合図。

---

## 実装順序

1. `adapters/engineer_set.py` に file 単位 `dataset_id` の尊重を足す（+ テスト）
2. `config.py` に `LpdataExportConfig` / `lpdata_dir`、`pyproject.toml` に `pyyaml>=6.0`
3. `lpdata_export/template.py` + `labels.py` + `invariants.py`（純関数・I/Oなし）とそのテスト
4. `lpdata_export/paths.py` + `sample.py` + `writer.py` + `reader.py`。
   `--no-measure --image-mode none` で end-to-end が通る状態にする
5. `core/imageio.convert_dicom_to_png` の移植 + `lpdata_export/images.py`（+ テスト）
6. `lpdata_export/masks.py` + `measure.py`（画素パス・キャッシュ・並列）+ `manifest.py`
7. `lpdata_export/report.py`、CLI 配線
8. `notebooks/lpdata_export.ipynb`
9. README に節を追加（既存の章立てに合わせる）。ラベルの初期生成規則と、
   後日のレポートCSV更新処理との責任分界を明記する

---

## 検証

```bash
# 1. 単体テスト（合成データのみ。数秒）
uv run pytest tests/test_lpdata_labels.py tests/test_lpdata_invariants.py \
              tests/test_lpdata_export.py tests/test_lpdata_paths.py \
              tests/test_lpdata_images.py tests/test_lpdata_template.py -q
uv run pytest -q                       # 既存テストの回帰（特に test_architecture.py）
uv run pytest -q -m realdata           # テンプレート/変換実装の drift 検知
uv run ruff check src tests && uv run ruff format --check src tests

# 2. 下見（画素を読まない・画像も作らない。数十秒）
uv run segmentation-validation export-lpdata \
    --no-measure --image-mode none --mask-mode none \
    --dataset-name chest_metry_pi6_pneumothorax \
    --dataset-id chest_metry_pi6_pneumothorax_2026_001 --owner kosuke.nakamura
#   → summary で所見語彙一覧・ラベル分布・allowlist 外の正常らしきラベル件数・
#     後続CSV更新で補正すべき候補を確認する

# 3. 少量で本番経路を通す（画像変換を含む）
uv run segmentation-validation export-lpdata --limit 200 --jobs 8 \
    --image-mode convert --mask-mode generate \
    --image-output-dir output/lpdata/smoke/images \
    --mask-output-dir  output/lpdata/smoke/masks/pneumothorax ...

# 4. 全件（長時間・約113 GiB。空き容量とNFS負荷を確認してから）
df -h /mnt/project
uv run segmentation-validation export-lpdata --jobs 16 --image-mode convert ...
```

**5. 出力の自己検査**

```bash
python - <<'PY'
import json, collections
d = json.load(open("output/lpdata/<tag>/chest_metry_pi6_pneumothorax.json"))
keys = set(d["meta"]["structure"])
assert d["meta"]["content_type"] == "dataset"
assert all(set(s) == keys for s in d["samples"].values()), "structure とキー集合が不一致"

bad = collections.Counter()
for sid, s in d["samples"].items():
    st, fl, pc = s["abnormal_finding_status"], s["finding_labels"], s["pneumothorax_case"]
    if (st == "present") != (fl != []):            bad["1: present ⇔ finding_labels非空"] += 1
    if st in ("absent", "unknown") and fl != []:   bad["2/3: absent・unknownはfinding_labels空"] += 1
    if st in ("present", "absent") and pc != ("pneumothorax" in fl):
                                                   bad["4: present/absent時の気胸対応"] += 1
    if st == "absent" and pc:                      bad["5: absent×pneumothorax_case:true"] += 1
    if s["pneumothorax_side"] is not None:         bad["side is not null"] += 1
    if s["bulla_bleb_status"] == "absent":         bad["初期エクスポートでabsentは出さない"] += 1
    for k in ("pneumothorax_mask", "lung_mask", "thorax_mask"):
        if s[k] is None or "pixel_array" not in s[k]: bad[f"{k}の表現"] += 1
print("違反:", dict(bad) or "なし")
print("サンプル数", len(d["samples"]))
print(collections.Counter(
    (s["abnormal_finding_status"], s["pneumothorax_case"]) for s in d["samples"].values()))
PY
```

**6. lp-data での読み戻し**（本リポジトリに `lpdata` を入れないので隣の環境で行う）

```bash
cd /mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6
uv run python -c "
from lpdata.io import load_dataset
ds = load_dataset('<出力JSON>', root_dir='<出力ディレクトリ>', lazy=True)
print(len(ds.samples), ds.meta.missing_required_fields())
"
```

`--image-mode convert` で作ったファイルなら eager 読み込みも通るはず。
`planned` で作ったものは `lazy=True` が必須（実体が無い）。

**7. 期待値の確認**（実データ。summary の数字が下記と合うこと）

- サンプル数 42,574
- `abnormal_finding_status`: `present` 6,680 / `absent` 853 / `unknown` 35,041
- `(status, case)`: `present/true` 3,521 / `present/false` 3,159 / `absent/false` 853 / `unknown/false` 35,041
  （`absent/true` と `unknown/true` は **0件が正しい**）
- `finding_labels` 非空 6,680 / `bulla_bleb_status: present` 234 / `absent` 0
- `view_position` 非null 27,082（PA 22,689 / AP 4,393）
- `lung_mask` 非null ≦17,225 / `thorax_mask` 非null ≦13,606 → **`lung_rect` 非null は ≦13,606**
- 不変条件違反 0件
- 画像変換: 走査キャッシュ上は DICOM 52,160件すべて実在するので skip は 0件が期待値。
  1件でも出たらその一覧を確認する

---

## スコープ外（今回やらない）

- **読影レポート由来のラベル抽出・判定。** 後日CSVで受け取り、独立した「ラベル更新処理」として追加する。
  本変更では seam（`sample_id` を結合キーにする / `reader.py` / builder から独立した `writer.py` /
  再利用可能な `invariants.py` / `meta.provenance.label_source`）だけを用意する
- **`pneumothorax_side` の推定** — 患者基準の解剖学的左右であり、画像座標から起こすと全反転する
- **`bulla_bleb_status: absent` の判定** — アノテーション対象だったかを既存 annotation から判定できない
- **マスク未アノテーションの気胸症例の補正** — `pneumothorax_case: false` になる。候補一覧を summary に出す
- **`Disease` / `Disease Evolution` の所見への取り込み** — 疾患名・経過であって所見名ではない
- **train/val/test の split**
- **med-chest-metry-pi6 Issue #27** — 既存 PNG（med-dicom 経由）と本エクスポータの PNG
  （`prepare_data` 経由）の画素値一致は未確認。provenance にどちらで作ったかを残すだけにする
- **med-chest-metry-pi6 側のコード追随（Issue #52）** —
  `dataset_file.py` の `MERGED_MASK_ATTRIBUTE = "pneumothorax_mask_merged"` はテンプレートの
  `pneumothorax_mask` と食い違い、`data/README.md` の不変条件記述も無条件版のまま。
  **現状のままでは学習側の `load_dataset_file` が `ValueError` を出す。**
  本エクスポータはテンプレート（正典）に従うので、追随は向こうの Issue で解消される
