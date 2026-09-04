# ChestMetry PI6 セグメンテーションデータセット バリデーション 再設計Plan

- 作成 2026-09-04 / 更新 2026-09-04（v5: cannot_determine は review 対象外を既定に。unverified_checks 列と kept_without_full_check を追加）

---

## 1. 目的

PI6のセグメンテーションデータセットに対して、

1. 機械的に判定可能な不整合を検出する
2. 目視確認が必要な疑わしいannotationを抽出する
3. FiftyOneでannotation単位の目視レビューを行う
4. review結果を SelectionDecision として保存する
5. **元JSONを変更せず**、不要annotationを除外した Development Dataset JSON を別途生成する

一回限りのvalidation scriptではなく、将来新しいdatasetが追加された際にも既存checkを再利用できる構成とする。
ただしPhase 0として過剰な汎用frameworkにはしない。

### 全体workflow

```
Original Dataset JSON
   -> DatasetAdapter
   -> AnnotationRecord / FileGroup
   -> scan / measure  -> measurement cache
   -> validation checks
   -> issues.json / issues.csv
        |
        +-- 自動採否可能 (D01 exact duplicate のみ) --------------------+
        |                                                              |
        +-- 目視確認必要 -> FiftyOne -> Human Review                    |
        |                                 -> review_decisions.json ----+
        |                                                              |
        +-- Issueなし                        -> default keep ---------+
        |                                                              |
        +-- 判定不能 (cannot_determine 501) -> kept_without_full_check +
                                               ※既定では review に回さない
                                                                       |
                                                                       v
                                              selection_decisions.json / .csv
                                              ★全annotationの採否確定 (1817行)
                                                                       |
                                                                       v
                                                development.json + selection_summary.md
```

原則:
- 元JSONは変更しない
- **validation結果（Issue）と採否判断（SelectionDecision）を分ける。Issueあり ≠ exclude**
- **`selection_decisions` が採否の唯一の正本**。全annotationが必ず1行存在する
- FiftyOne DBを唯一の正本にしない。FiftyOne は「人間が SelectionDecision を入力する UI」
- FiftyOneが利用できなくても core validation は成立する

---

## 2. ★ 実装前確認（§46）— 全7項目 実測済み・すべてクリア

| # | 確認事項 | 実測結果 |
|---|---|---|
| 1 | `annotation.timestamp` の形式 | **1817/1817 が `YYYY-MM-DD HH:MM:SS+00:00`**、tzinfo は全件 **UTC**、`datetime.fromisoformat` で全件 parse 可 |
| 2 | timestamp の存在 | **欠損 0件 / parse不能 0件** |
| 3 | timestamp tie 件数 | **exact & same-label 101ペアで tie = 0** → ★ **101ペアすべてが自動解決可能**。自動決定不能ケースは現データに存在しない |
| 4 | `$HOME` のfilesystem | `/dev/md0` **ext4（ローカル）**, `rw,relatime,nodelalloc`。mongod 配置可。ただし**パスはハードコードせず `Path.home()` / `$HOME` を使う** |
| 5 | OpenCV dependency resolution | core群を `opencv-python-headless>=4.14,<5` に統一 → 解決結果の cv2 provider は **`opencv-python-headless==4.14.0.94` 1種類のみ**（15パッケージ） |
| 6 | review群で cv2 conflict が起きないこと | core + `fiftyone>=1.21.0` を解決 → cv2 provider は同じく **1種類のみ**（120パッケージ）。★ conflict 解消を確認 |
| 7 | FiftyOne App の SSH tunnel 利用 | port **5151 未使用**、sshd に `AllowTcpForwarding` の明示設定なし（既定 `yes`）→ tunnel 可 |

### #3 が設計に与える影響

`timestamp` は全件存在し全件一意に比較できるため、**D01 の自動採否は現データの101ペア全部に適用できる**。
「timestamp同値 / 欠損 / parse不能 → review_required」の分岐は**現データでは1件も発火しない**。
実装はする（将来のdataset・再エクスポートで必ず必要になる）が、**実データでは検証できない**ので
§21 の synthetic test で担保する。

---

## 3. 事前調査で確定した事実（設計の前提）

全て実データで実測済み。要件の文面と食い違うものは ★。

| 事実 | 値 | 影響 |
|---|---|---|
| ★ `thorax-mask` は塗りつぶし領域ではない | 前景 **1.26%**、左右2本の**細い曲線**（胸壁内縁）、bbox充填率 0.058/0.057 | 素の containment は使えない。領域を**再構成**する。PI6固有ロジック |
| `mediastinum-mask` が存在する | 同じパス規約、塗りつぶし、前景 12.3% | 胸郭領域の再構成に使う |
| ★ 参照マスクのカバレッジ | ファイル **519/829 = 62.6%** / annotation **1156/1665 = 69.4%** | 残り501件は `cannot_determine`。既定では review に回さず `kept_without_full_check` として keep し、summary に明示（§14.6） |
| ★ `path_original_mask` は多数派 | 1568/1665 (94%) | 存在自体は正常。issueとして出さない |
| ★ 3JSONすべて `version_id=2.2` / `version_date=2026-05-19` | 同一 | キャッシュキーに version_id 単独は使えない |
| ★ `file_id` が2JSONに跨る | 1083件中 unique 1082 | ファイル単位グルーピングに `source_json` 必須 |
| ★ brush annotation は全て `is_latest=1` | 1665/1665 | is_latest は候補filterに使えない（方針と一致） |
| `geometry_uid` は全域一意 | 1817/1817 | 主キー。FiftyOne・SelectionDecision を結ぶキー |
| `label_id` は全域で一意 | 1953/1953、重複0件 | duplicate判定の中心にしない（§5） |
| `region_count` == 連結成分数 | 1665/1665 一致 | 成分レベル検査のゼロI/O事前フィルタ（多成分6件のみ） |
| `path_mask` は全て mode=L / 値 {0,255} | 例外なし | M02/M03 は `path_mask` では全green見込み。RGBAは `path_original_mask` 側で正常 |
| マスクファイル名 stem == `geometry_uid` | 1665/1665 | M06 の実効的な不変条件 |
| 壊れた参照PNG | 21バイトの `Internal Server Error` が実在（mask136で1件） | `UnidentifiedImageError` を握らないと落ちる |
| `spacing` が null の series | 2件 (segmed) | mm²計算不能 → `cannot_determine_mm2` |
| `apply_voi_lut` が例外を投げる症例 | ofuna_chuo の Konica | 既存の try/except を維持 |
| DICOM画素ベースの「体外＝背景領域」検査 | 全1665件走査して**実質0件** | 常設チェックにしない |

参照マスクのカバレッジ内訳:

| JSON | ファイル | 3種そろい | lungのみ | なし | annotation | 判定可 |
|---|---|---|---|---|---|---|
| ANN_EIRLPRJ_01272 | 296 | **0 (0.0%)** | 0 | 296 | 491 | **0** |
| ANN_EIRLPRJ_1298 | 397 | 397 (100%) | 0 | 0 | 898 | 898 |
| ETR_..._mask136 | 136 | 122 (89.7%) | 6 | 8 | 276 | 258 |
| **合計** | **829** | **519 (62.6%)** | 6 | 304 | **1665** | **1156 (69.4%)** |

> ご指摘の77%とは合いません（実測 62.6% / 69.4%）。構造は「**01272が丸ごと未整備 (0/296)**、
> 除けば 519/533 = 97.4%」で、欠損は**バッチ日ディレクトリ単位**。
> カバレッジは config の参照ルートに依存するので**ツールが毎回実測して summary に出す**設計にし、計画に数字を固定しない。

---

## 3.5 ★データセットの母集団（当初の報告が不正確だった点）

`file_list` は **1083エントリ**あり、annotation を持つのは 863画像。
当初この220枚を「annotation がゼロ」としか報告しておらず、
**画像枚数を863として提示していたため規模が伝わらなかった**。実態は3種類:

| 区分 | 画像 | study | 内容 |
|---|---|---|---|
| annotation あり | **863** | 857 | 検証対象 |
| **正常例（No Findings）** | **187** | 187 | ★**187/187 が `No Findings/001 normal` を持ち DICOM も実在**。意図的な陰性症例で、開発データの陰性サンプルとして使える |
| 未アノテーション | 33 | — | アノテーション済み study の2枚目以降（末尾 `_001`/`_002`）。分類ラベルも無い |
| **合計** | **1083** | **1044** | 患者 982 |

「検証すべきマスクが無い」のと「アノテーション漏れ」を区別できるよう、
`FileGroup.case_labels`（study/series レベルの分類ラベル）と
`FileGroup.is_negative_case` を持たせた。アダプタが従来これらの分類ラベルを
件数だけ数えて捨てていたのが原因。

レポートは**データセット全体を主に出し、annotation を持つ分を内訳として示す**。
採否の数字は annotation を持つ分が母数であることを明記する。

---

## 3.6 ★画像単位の採否（annotation 単位では表せないもの）

`selection_decisions` は annotation 単位なので、**annotation を持たない画像は1行も持てない**。
しかし判断は必要:

- **正常例（No Findings）187枚** → keep（陰性サンプル）
- **未アノテーションのビュー33枚** → **pending**。側面像なら開発データから除外が必要

そこで `image_decisions.csv`（**1画像 = 1行**、1083行）を新設した。
annotation 単位の不変条件（1817行）を壊さずに画像の採否を明示できる。

新チェック `M09_UNANNOTATED_VIEW` は **annotation ではなく画像**について報告する
（`geometry_uid` は None）。`CheckContext.file_issue()` を追加。

`build-dataset` は両方を見る。**画像が exclude なら file entry ごと落とす** ——
「annotation が0件になっても file entry は残す」規則とは別で、
画像を落とすという明示的な判断があったときだけ母集団を変える。

### M01-M05 を自動 exclude に変更

当初 `review_required` にしていたが、**目視の余地が無い**ため誤りだった。
特に `M05_FILE_MISSING` は画像もマスクも存在しないので **FiftyOne に表示できない**。
「自動採否は D01 のみ」という方針は*判断を要するもの*（重複・微小領域）についての話で、
*客観的に壊れているファイル*の話ではなかった。

条件は **severity=ERROR かつ status=checked** のみ。
これにより original マスクの INFO（RGBAは仕様上正常）と判定不能を巻き込まない。
D01 の keep と衝突したら **exclude を優先**する（`merge_automatic`）。

実データでは M01-M05 が全て0件なので現在の数字は変わらない。
合成データ8ケースで挙動を検証した。

---

## 4. アーキテクチャ

維持する設計思想:
- 画像/maskを**1度だけ読む**
- measurement を cache
- checks は measurement を入力とする **pure function**
- core / checks / report の責務分離

```
src/segmentation_validation/
├── __init__.py
├── __main__.py
├── cli.py
├── config.py
│
├── adapters/                    ★元JSON形式の知識をここに閉じ込める
│   ├── base.py                  DatasetAdapter プロトコル / Capability
│   └── engineer_set.py          engineer-set 形式（現行の唯一の実装）
│
├── core/                        dataset非依存。matplotlib も fiftyone も import しない
│   ├── records.py               AnnotationRecord / FileGroup
│   ├── paths.py                 config駆動のパス解決
│   ├── labels.py                Label / label identity = (code_system, code)
│   ├── imageio.py               load_dicom_header / load_dicom_image / load_mask_raw / load_reference
│   ├── geometry.py              mask_stats / iou / containment / mask_hash / crop_box
│   ├── measure.py               ★単一走査パス。ここだけが画素ファイルを開く
│   ├── cache.py                 JSONL cache / fingerprint / resume
│   └── progress.py              logging ベースの進捗
│
├── datasets/                    ★dataset固有ロジックの隔離
│   └── pi6/
│       ├── references.py        lung/thorax/mediastinum のパス導出と読み込み
│       └── outside_body.py      LATERAL_BAND 構成と体外判定（PI6固有）
│
├── checks/
│   ├── base.py                  Issue / CheckContext / CheckStatus / Check プロトコル
│   ├── machine/                 M01-M08
│   ├── duplicate/               D01-D04 + duplicate group
│   └── suspicious/              S03 tiny / S04 original-final / S05 outside body
│
├── review/                      FiftyOne。core validation からは完全にoptional
│   ├── fiftyone_builder.py      ★fiftyone を import する唯一のモジュール群
│   ├── review_schema.py         review field / tag の語彙定義（fiftyone非依存）
│   ├── export_decisions.py      FiftyOne -> review_decisions.json/csv
│   └── import_decisions.py      review_decisions.json -> FiftyOne
│
├── selection/                   ★採否判断と開発データ生成
│   ├── decisions.py             SelectionDecision model / 自動決定 / マージ
│   └── build_dataset.py         Original JSON + decisions -> Development JSON
│
├── report/
│   ├── issues_json.py
│   ├── issues_csv.py
│   ├── summary_md.py
│   └── area_distribution.py
│
└── viz/
    └── overlay.py               既存の1症例重畳CLI用。debug/fallback 用途のみ
```

**削除**: `validate.py`（0バイトのスタブ）。`viz/samples.py`（静的PNGコンタクトシート）は**作らない**。

### 依存関係（実測で確定）

```toml
dependencies = [                          # uv sync  -> 15パッケージ
  "matplotlib>=3.11.1", "matplotlib-fontja>=1.1.0", "numpy>=2.5.2",
  "opencv-python-headless>=4.14,<5",      # ★ opencv-python から変更
  "pandas>=3.0.5", "pillow>=12.3.0", "pydicom>=3.0.2",
]
[dependency-groups]
review = ["fiftyone>=1.21.0"]             # uv sync --group review -> 120パッケージ
```

同一environmentに `opencv-python` と `opencv-python-headless` を共存させない。
解決結果は両群とも cv2 provider が `opencv-python-headless==4.14.0.94` の1種類のみ（実測確認済み）。

`fiftyone-db` は Linux ホイールを配布しておらず、8KB の sdist が **インストール時に
`fastdl.mongodb.org` から mongod 86MB をダウンロード**する（Ubuntu 20.04 x86_64 は対応表内、HTTP 200 到達確認済み）。

---

## 5. DatasetAdapter

engineer-set 形式の知識を validation 全体へ拡散させない。**check側は元JSONの階層構造を直接参照しない。**

```python
class DatasetAdapter(Protocol):
    dataset_id: str
    def iter_files(self) -> Iterator[FileGroup]: ...
    def iter_annotations(self) -> Iterator[AnnotationRecord]: ...
    def resolve_image_path(self, raw: str) -> Path: ...
    def resolve_mask_path(self, raw: str) -> Path: ...
    def resolve_original_mask_path(self, raw: str) -> Path: ...
    def capabilities(self) -> frozenset[Capability]: ...
```

現時点では `adapters/engineer_set.py` のみ実装する。将来の別形式は `adapters/<name>.py` を追加して
同じ `AnnotationRecord` へ変換する。

### Capability と3値ステータス

```
has_dicom / has_spacing / has_original_mask
has_lung_reference / has_thorax_reference / has_mediastinum_reference
```

必要な情報が無いとき **`pass` にはしない**。checkの結果は3値:

```
checked            判定した
cannot_determine   情報が無いので判定できない（理由を添える）
not_applicable     このdatasetには原理的に適用されない
```

summary に3値それぞれの件数を出す。「issueなし」と「未検査」を絶対に混同させない。

---

## 6. AnnotationRecord

```
dataset_id, source_json, institution, study, series, file, file_uid
image_path, resolved_image_path
annotation_type
geometry_id, geometry_uid
path_mask, resolved_path_mask
path_original_mask, resolved_path_original_mask
json_bbox, region_count, declared_size
is_latest, version
user, timestamp, annotation_request
labels, raw_label_ids
```

主キー = `geometry_uid`（全域一意を確認済み）。dataset跨ぎを想定して `(dataset_id, geometry_uid)` でも識別できる形を保持。
`file_uid` は **`source_json` を含める**（1件のfile_idが2JSONに跨るため。含めないと偽の重複ペアが出る）。

---

## 6.5 ★検証対象の病変（当初の計画に抜けていた判断）

元の要望で「気胸」が出てくるのは **S03 の閾値決定の根拠**としてのみで
（「微小領域の閾値は恣意的に設定せず、気胸のマスク面積分布を確認した上で決定する」）、
**検証対象を絞る指定ではなかった**。計画はこれを「クラス族ごとの閾値」で処理したが、
そもそも非気胸を検証すべきかという判断を書いていなかった。

実測（brush annotation のラベル構成）:

| データセット | brush | 気胸 | 気胸率 | 主な非気胸 |
|---|---|---|---|---|
| ANN_EIRLPRJ_01272 | 491 | 320 | 65.2% | 間質性陰影71 / ブラ34 / 胸水24 |
| ANN_EIRLPRJ_1298 | 898 | 319 | **35.5%** | 間質性陰影209 / 胸水131 / 結節98 |
| ETR_..._mask136 | 276 | 276 | **100%** | — |
| **合計** | **1665** | **915** | **55.0%** | |

**mask136 だけが気胸専用**で、他2つは混在している。
現在の pending 251件のうち**気胸は102件だけで、149件（59%）が非気胸**。

### 決定: config で切り替え、既定は全病変

```jsonc
"validation": {
  // 空なら全病変を検証（既定）。絞るなら列挙する。
  // 同一性は (code_system, code) なので "Findings/010" が正式。
  // "pneumothorax"（code_text_eng）でも指定できる。
  // code_text は表記揺れがあるので受け付けない
  // （Findings/010 に「気胸（塗りつぶし）」と「気胸（縁取り）」が混在する）。
  "target_labels": []
}
```

- **対象内は全件検証する**（部分的に検証しない）
- **対象外はチェックを一切走らせず**、採否マスタに `reason = out_of_scope` として1行残す。
  `no_issue_detected`（検証して問題なし）と混ぜない
- 対象外も `keep` のまま。開発データから落とすかは属性定義が固まってから決める

実装は **`CheckContext` の構築時に対象内/対象外を分ける**。こうすると15個のチェックを
1つも触らずに全てがスコープに従い、ペア系も「片側でも対象内なら報告」という正しい挙動になる
（`_emit` が `by_uid` に無い側を飛ばすため）。

気胸のみに絞った場合の実測:

```
対象 915 / 対象外 902 annotation
pending 251 -> 102 、目視する画像 177枚 -> 87枚
out_of_scope 902 件を採否マスタに明示
```

★**bbox / elliptical 152件に気胸ラベルは1件も無い**（fracture 42 / 縦隔拡大 14 / other 82 等）。
そのため気胸のみモードでは、座標が壊れている11件（degenerate 7 / 範囲外 4）も全て対象外になる。
気胸専用の開発データには無関係なので筋は通っているが、
**全病変モードでしか M07 の異常は拾えない**ことは意識しておく。

---

## 7. labels の扱い

labels は annotation 作業上のラベル情報。**今回のvalidationでは
「同じ `(code_system, code)` が複数ある」こと自体を主要な異常として扱わない。**
セグメンテーション mask/geometry の品質確認が目的であり、labels metadata の重複だけでは採否に直結しないため。

→ **旧 S02 duplicate label check は主要validationから削除**（§14）。

labels の用途:
- annotation の semantic 情報
- duplicate mask の解釈（同一ラベルか否かで D01 と D02 を分ける）
- 対象病変の抽出

厳密な label identity は **`(code_system, code)`** とする。
`code_text` / `code_text_eng` は同一性判定に**使わない**。
特に `code_text_eng` は複数の日本語annotation形式を同一英語表記にまとめる場合がある
（実測: `Findings/010` に `気胸（塗りつぶし）` 294件と `気胸（縁取り）` 1件があり、
どちらも `code_text_eng = pneumothorax`。英語名だけ見ていると完全に見えない）。

`label_id` は保持するが duplicate 判定の中心にはしない。将来的に dataset 間 provenance 確認に利用可能。

---

## 8. Machine checks（Mxx）

| ID | 内容 |
|---|---|
| `M01_MASK_RESOLUTION` | PNG mask size と DICOM Rows/Columns を比較。`series.shape` / `annotation.width,height` / PNG / DICOM の整合も確認。PNG↔DICOM を ERROR、JSON↔DICOM を別IDの WARNING |
| `M02_MASK_CHANNELS` | `path_mask` が単channel形式か。`path_original_mask` は仕様上RGBA等があり得るので**同じseverityにしない**（`path_mask`=ERROR / `path_original_mask`=INFO） |
| `M03_MASK_BINARY` | `path_mask` の生画素値を**二値化前**に確認。0/255以外・反転を検出。→ `load_mask_raw()` を別に用意する（現行 `load_binary_mask` は生属性を捨てている） |
| `M04_MASK_NOT_EMPTY` | fg=0 / 全画素同値 / 極端な全塗り |
| `M05_FILE_EXISTS` | DICOM / path_mask / path_original_mask の存在と可読性。**存在するが壊れている場合を別IDで区別**（21バイトの `Internal Server Error` が実在） |
| `M06_PATH_FORMAT` | dataset固有のpath invariant。**Adapter側から提供**する。例: `mask filename stem == geometry_uid`（1665/1665 成立） |
| `M07_BBOX_GEOMETRY` | bbox/elliptical について `min < max` / image bounds内。既知で degenerate 7件・範囲外 4件 |
| `M08_JSON_MASK_CONSISTENCY` | JSON上の `bbox` / `region_count` と実mask計測値を比較。現在一致（1665/1665）でも**回帰検知として残す** |

---

## 9. Duplicate annotation checks（Dxx）

重複の中心は labels ではなく **path_mask / geometry** とする。同一画像内の異なる `geometry_uid` を比較する。
**478ファイル（brush 2件以上）の全ペアを実測済み。**

| ID | 条件 | severity | 自動採否 | **実測** |
|---|---|---|---|---|
| `D01_EXACT_DUPLICATE` | same file / 異なる geometry_uid / mask **pixel完全一致** / `(code_system,code)` 同一 | ERROR | ★**可**（§10） | **101ペア** |
| `D02_EXACT_MASK_LABEL_CONFLICT` | 同上だが `(code_system,code)` **異なる** | ERROR | **禁止**。高優先度 review | **0ペア**（回帰検知） |
| `D03_NEAR_DUPLICATE` | same file / same label / IoU ≥ threshold / pixel完全一致ではない | WARNING | **禁止**。目視 | **2ペア** (IoU≥0.95) |
| `D04_CONTAINED_DUPLICATE` | `containment_a_in_b` または `containment_b_in_a` ≥ threshold | WARNING/INFO | **禁止**。目視 | **39ペア** (≥0.98) |

threshold は configurable（既定 IoU 0.95 / containment 0.98）。実データ分布を確認して確定する。

**exact判定の効率化**: `shape一致 → fg count一致 → mask hash一致 → pixel equality` の順で絞る。
`core/geometry.py` に `mask_hash()`（例: `hashlib.blake2b(packbits(mask))`）を置く。

`detail` に保持: `geometry_uid` / `related_geometry_uid` / `user` / `timestamp` / `is_latest` /
`annotation_request` / `version` / `code_system` / `code` / `iou` / `containment` / `mask_hash`。

### D04 実測に基づく注意

`D04` の39ペアのうち**36ペアはラベルが異なり、IoU が 0.0019〜0.088**（containment は 1.0）。
つまり**小さい所見が大きい所見に完全に含まれる「入れ子」**で、結節が浸潤影の内側にある等の
医学的に正当なケースが大半と思われる。だからこそ自動exclude禁止で、FiftyOne の判定に委ねる。
Precision が極端に低ければルールごと外す判断ができる（§12）。

### duplicate group

pair だけでなく group 単位でも扱う。`A≒B`, `B≒C`, `A≒C` を connected component として束ね、
各annotationに `duplicate_group_id` と `related_geometry_uids` を持たせる。

**実測**: 全duplicate pairで **group 139個**（サイズ2が136個、**サイズ3が3個**）。
サイズ3が実在するので group ロジックは必要。
exact&same-label に限ると **group 101個、全てサイズ2**。

---

## 10. D01 の自動採否

社内確認結果:

> データが完全一致なのでどちらでも学習上の影響はない。採用基準としては日付が新しい方を使うのが自然。

annotation の時系列fieldは `timestamp` のみ。したがって:

```
pixel完全一致 + same (code_system, code) + 異なる geometry_uid
  -> timestamp 新しい方  : keep
  -> timestamp 古い方    : exclude, reason = exact_duplicate_older_annotation
```

- **`geometry_uid` の大小は使用しない**
- **`is_latest` は候補filterに使わない**（両方 `is_latest=1` でも存在可能。実測でも brush は全件 `is_latest=1`）

group が3件以上なら **timestamp 最大の1件を keep、残りを exclude**。

### 自動決定不能ケース → `review_required = true`

```
timestamp 同値 / timestamp 欠損 / timestamp parse不能
```

★**実測: 現データではこの分岐は0件**（1817/1817 が存在・parse可、101ペアで tie=0）。
実装はするが実データで検証できないため **synthetic test で担保**する（§13）。

**実測される自動採否**: exact&same-label group 101個 × 各1件exclude = **101 annotation が自動 exclude**。
各groupはサイズ2かつ同一ファイル内なので、**自動exclusionだけでファイルのannotationが0件になることはない**。

---

## 11. path_mask / path_original_mask と original-final比較

社内確認:

```
path_mask          = 修正後 = 開発データとして使用
path_original_mask = Annotation Tool で作成された修正前
```

修正例: 閉曲線になっていない / 消しゴムの消し残り。

→ **validation の主対象は `path_mask`。**
→ `path_original_mask` が存在すること自体は異常ではない（1568/1665 = 94%）。
   **旧 `HAS_ORIGINAL_MASK` の大量出力は削除**（§14）。

### `S04_ORIGINAL_FINAL_DIVERGENCE`

original と final の差が異常に大きい場合**のみ** review 候補にする。計測:

```
filled_original_vs_final_iou     original を穴埋め(cv2.floodFill)した結果と final の IoU
difference_pixels                差分画素数
difference_ratio                 差分 / final面積
```

**「original と違う → error」とはしない。** 修正が正常に行われた結果である可能性が高いため
`review_required` として扱う（severity は WARNING）。
mask136 での実測では 179ペア中 **2件**のみが不一致（`55b774ff-…`, `04d9b185-…`）。

FiftyOne 上で `path_mask` と `path_original_mask` を**比較可能**にする（§12）。

---

## 12. S03 tiny / stray component

### 二段構え

annotation全体の面積だけでなく、**connected component 単位でも**検査する。
実測例: 383,163px の本体 + 1px / 2px / 2px の飛びカス —— annotation単位では絶対に見えない。

面積は原則 **mm²**（spacing が 0.0875〜0.2 と 5.2倍振れるので px は施設間で比較不能）。
spacing が無い場合は `cannot_determine_mm2`（実測2件）。

| ID | 条件 | severity | review priority | **実測** |
|---|---|---|---|---|
| `S03_TINY_ANNOTATION` | `area_mm2 < 20` | **WARNING** | critical | **1件** |
| `S03_STRAY_COMPONENT` | `n_comp>1 かつ (comp_mm2 < 20 または comp_px/ann_px < 0.01)` | **WARNING** | critical | **5成分 / 3 annotation** |
| `S03_SUSPICIOUSLY_SMALL` | クラス別下限 F 未満（focal 20 / localized 50 / regional 150 mm²） | WARNING | high | **20件** |
| （レビュー帯） | `F ≤ area < 3F` | INFO | normal | 105件 |

### ★ severity を error から warning に変更した理由

20mm² は**仕様上の禁止値ではなく実測データ上の外れ値**（全クラスの実測最小 22.88 mm² を下回り、
1px と 747px の間に747倍のギャップがある、という統計的な根拠しかない）。
したがって `error = 自動的に不正` とはせず、**自動exclude禁止・FiftyOne確認**とする。

面積分布は log空間で**滑らかな単峰の連続分布**（mode 3000-5600 mm²、左裾 23 mm² まで途切れなし）で、
恣意的でない切れ目は無い —— その事実をそのまま summary に書く。
一律閾値がダメな根拠も実測済み: `<100 mm²` で58件挙がるが**うち43件が nodule**（正当に小さい病変）。

---

## 13. S05 outside-body（PI6固有）

PI6 では `thorax-mask` が塗りつぶしではなく曲線（前景1.26%、bbox充填率0.058）。
そのまま containment を計算すると中央値 0.0295 で全1156件が「体外」になる。
`lung-mask` 単独も失格（**胸水131件中111件=85% が containment<0.5**。胸水は定義上、含気肺野の外）。

→ `thorax` / `lung` / `mediastinum` から **LATERAL_BAND** を構成する。
**このロジックは PI6 固有**なので `datasets/pi6/outside_body.py` に隔離し、全dataset共通の core rule にはしない。

```
rowfill(M)    : 各行 y で M の画素がある行は [min_x(y), max_x(y)] を true
THORAX_REGION = rowfill(thorax) ∪ lung ∪ mediastinum を穴埋めして最大成分
LATERAL_BAND  = 同じ行方向の範囲を全ての行に外挿（最上/最下行を継承、行間欠損は前方継承）
```

LATERAL_BAND は垂直方向に無限なので、肺尖への伸展や肋横角への胸水は罰しない。
一方 **上腕・肩の軟部組織へのはみ出し**（「周辺画素との微分では検知できない」と要件が指摘したケース）は確実に外になる。
実測の裏付け: THORAX_REGION の外に出ている面積の **93.3%が側方**、下方は6.7%。

指標（ファイルごとに `D = distanceTransform(~LATERAL_BAND) * spacing_y` を1度だけ計算。
マージン m mm の膨張は `D <= m` と等価なので dilate ループ不要）:

```
lat_cont(m)  = mean(D[mask] <= m)
lat_out(m)   = (1 - lat_cont(m)) * area_mm2   [mm²]
lat_max_mm   = max(D[mask])
```

「1画素でも外なら検出」は使えない（margin 0mm で 731/1156 = 63.2%）。実測:

| margin | ≥1px外 | cont<0.98 | cont<0.90 |
|---|---|---|---|
| 0 mm | 731 | 321 | 81 |
| 5 mm | 126 | 50 | 22 |
| **10 mm** | **50** | **29** | **14** |
| 20 mm | 19 | 12 | 3 |

**margin 10mm**（膝は5-10mmの間）。全て configurable。

```
severity ERROR   : lat_cont(10) < 0.80                            ->  6件
severity WARNING : lat_cont(10) < 0.98 かつ lat_out(10) > 100 mm²  -> 26件
severity INFO    : lat_cont(10) < 1.0                             -> 50件
```

**このcheckも自動exclude禁止。FiftyOne レビュー対象。**

参照maskが無ければ `cannot_determine`（理由付き）として summary へ:

| status | annotation | 備考 |
|---|---|---|
| `checked` | 1156 (69.4%) | |
| `cannot_determine:lung_only` | 8 | lung へのフォールバックは**しない**（気胸は肺野外にあるので原理的に無意味） |
| `cannot_determine:no_reference` | **500** | 01272 が丸ごと (0/296ファイル) |
| `cannot_determine:reference_corrupt` | 1件実在 | 参照パイプラインの再実行対象 |
| `cannot_determine:reference_shape_mismatch` | 現状0 | 防御 |

★**この501件は現時点では FiftyOne review 対象に含めない**（`cannot_determine_as_review_required: false`）。
`final_decision = keep` / `reason = kept_without_full_check` / `unverified_checks = S05_OUTSIDE_BODY` として
`selection_decisions.csv` に必ず1行残り、`selection_summary.md` に件数を理由別で明示する。
無言で「問題なし」に混ぜない。後から review に回すのは config 1行（§14.6）。

**注目所見**: 検出26件のアノテータ別内訳で `kolive23@gmail.com` が **14件中5件(35.7%)**（全体平均2.25%）。
描画確認した2件はいずれも**気胸マスクが右鎖骨上窩から肩・上腕に塗り出している**同じパターン（作業日2025-10-09〜10-28）。
FiftyOne で `auto:outside_body` を絞れば一気に確認できる。

### DICOM画素ベースの補助チェックは入れない

全1665マスクで実測して**実質0件**（最大0.36%、境界のなめ1件）。かつ最も高価（829ファイルで4分40秒）。
さらに**上腕のマスクは明るい軟部組織にあるので原理的に検知できない** —— 要件が「微分では検知できない」と
指摘したケースをこのチェックも同様に取りこぼす。`--check-dicom-background` の opt-in のみ、既定OFF。

---

## 14. Issue と SelectionDecision を分ける（最終採否マスタ）

### 14.0 3つの成果物の役割

| 成果物 | 答える問い | 行の粒度 |
|---|---|---|
| `issues.csv` / `issues.json` | **プログラムが何を検出したか** | 1 annotation に複数 Issue があり得る。**1:1ではない** |
| `review_decisions.csv` / `.json` | **FiftyOneで人間が何と判断したか** | **review対象のみ**。1 annotation = 1行 |
| `selection_decisions.csv` / `.json` | **最終的に開発データとして使うか** | ★**必ず 1 annotation = 1行。全annotationが存在する** |
| `development.json` | `keep` のannotationだけを反映した実際の開発用dataset | Original と同一スキーマ |

★ **Issueあり ≠ exclude。** tiny region として検出されても、FiftyOneで確認して医学的に妥当なら `final_decision = keep` になる。
Issue が一度も出なかった正常annotationも `final_decision = keep` として**必ず1行出す**。

### 14.1 目視後の流れ

```
Original annotations
      |
      v
Automatic validation  ->  Issues (issues.csv)
      |
      +--------------------------------+
      |                                |
 自動決定可能                     人間の確認が必要
 D01 exact duplicate              D02 / D03 / D04
      |                           tiny / outside-body / original-final / machine error
      |                                |
      |                                v
      |                            FiftyOne  (review UI)
      |                                |
      |                                v
      |                          Human review
      |                        keep / exclude / uncertain
      |                                |
      |                                v
      |                    review_decisions.json / .csv
      |                                |
      +----------------+---------------+
                       v
        automatic decision と human decision を統合
                       v
        selection_decisions.json / .csv   ★全annotationの採否確定
                       v
                 development.json
```

FiftyOne は最終成果物ではなく、**人間が SelectionDecision を入力するための review UI**。

### Issue = 問題**候補**

```
check_id, category, severity, status(checked|cannot_determine|not_applicable)
review_required, review_priority
dataset_id, source_json, file_uid, geometry_uid
message, detail
related_geometry_uids, duplicate_group_id
```

**Issue は開発データからの除外を意味しない。**

### 14.2 SelectionDecision = 最終採否マスタ（`selection_decisions.csv`）

★**dataset内の全 geometry annotation について必ず1行**。現データでは **1817行**
（brush 1665 + bbox 151 + elliptical 1）。
study/series レベルの分類 annotation（**332件** = study 271 + series 61、`annotation_id` を持つ別スキーマ）は
geometry ではないので対象外。件数だけ summary に出す。

| 列 | 内容 |
|---|---|
| `dataset_id`, `source_json` | 出所 |
| `institution`, `study`, `series`, `file_id`, `file_uid` | 所在（CSV単体で追える） |
| **`geometry_uid`** | ★主キー |
| `annotation_type` | brush / bbox / elliptical |
| `code_system`, `code`, `code_text` | 何の病変か |
| `user`, `timestamp` | 誰がいつ |
| **`detected_checks`** | 検出された check_id をパイプ区切り（例 `D04\|S05`）。無ければ `none` |
| **`unverified_checks`** | ★`cannot_determine` を返した check_id をパイプ区切り。無ければ `none`（§14.6） |
| `issue_count`, `max_severity` | `error` / `warning` / `info` / `none` |
| `review_required` | bool |
| **`review_status`** | `not_needed` / `pending` / `reviewed` |
| **`final_decision`** | `keep` / `exclude` / `uncertain` / `pending` |
| **`reason`** | `no_issue_detected` / `older_exact_duplicate` / `visually_valid` / `true_small_lesion` / `invalid_annotation` / `review_required` / ... |
| **`decision_source`** | `automatic` / `human` / `default` / `-`(pending) |
| `overrides_automatic` | 人間が自動判定を覆したか（監査用） |
| `related_geometry_uid`, `kept_geometry_uid`, `duplicate_group_id` | duplicate の対応関係 |
| `reviewer`, `reviewed_at`, `comment` | human のときのみ |

イメージ:

| geometry_uid | detected_checks | review_status | final_decision | reason | decision_source |
|---|---|---|---|---|---|
| A | none | not_needed | keep | no_issue_detected | default |
| A' | S05_REFERENCE_UNAVAILABLE | not_needed | keep | **kept_without_full_check** | default |
| B | D01 | not_needed | exclude | older_exact_duplicate | automatic |
| C | D03 | reviewed | keep | visually_valid | human |
| D | D04 | reviewed | exclude | invalid_annotation | human |
| E | S03_TINY_ANNOTATION | reviewed | keep | true_small_lesion | human |
| F | S05_OUTSIDE_BODY | pending | pending | review_required | - |

### 14.3 決定の優先順位（この順で最初に当たったものが確定）

```
1. human decision が存在する
     -> その decision。decision_source=human
        自動判定を覆した場合は overrides_automatic=true（監査可能にする）

2. automatic decision (D01) が exclude
     -> exclude / reason=older_exact_duplicate / decision_source=automatic  ここで確定

3. automatic decision (D01) が keep
     -> ★これは「D01では除外されない」だけで最終keepではない。3へ進む

4. review_required な Issue が1つでもある
     -> pending / reason=review_required / decision_source="-"

5. cannot_determine を返した check がある（既定では review に回さない。§14.6）
     -> keep / reason=kept_without_full_check / decision_source=default
        unverified_checks に該当 check_id を記録

6. それ以外
     -> keep / reason=no_issue_detected / decision_source=default
```

**3が重要**: D01 で残った側（新しい方）が S05 outside_body にも引っかかっていれば、
D01 の keep では確定させず pending にする。片方のcheckだけで採用を決めない。

### 14.4 自動決定できるのは D01 のみ

自動化可能なのは **D01 exact duplicate**（完全一致 + same label + timestamp差あり）だけ。

```json
{
  "geometry_uid": "<old>", "decision": "exclude",
  "reason": "older_exact_duplicate", "decision_source": "automatic",
  "kept_geometry_uid": "<new>", "duplicate_group_id": "DUP_0001"
}
```

実測: **101 annotation が自動exclude**。それ以外は原則 human review。

### 14.5 どのcheckを review 対象にするかは config の policy table で決める

check_id をハードコードせず `config` に置く。判断を後から変えられるようにするため。

```jsonc
"decision_policy": {
  // 自動採否できるもの
  "auto_decidable": ["D01_EXACT_DUPLICATE"],
  // 検出されたら review_required=true にするもの
  "review_required": [
    "D02_EXACT_MASK_LABEL_CONFLICT", "D03_NEAR_DUPLICATE", "D04_CONTAINED_DUPLICATE",
    "S03_TINY_ANNOTATION", "S03_STRAY_COMPONENT", "S03_SUSPICIOUSLY_SMALL",
    "S04_ORIGINAL_FINAL_DIVERGENCE", "S05_OUTSIDE_BODY",
    "M01_MASK_RESOLUTION", "M02_MASK_CHANNELS", "M03_MASK_BINARY",
    "M04_MASK_NOT_EMPTY", "M05_FILE_EXISTS"
  ],
  // 記録のみ。keep のまま review にも回さない
  "informational": ["M06_PATH_FORMAT", "M07_BBOX_GEOMETRY", "M08_JSON_MASK_CONSISTENCY"],
  // cannot_determine を review 対象にするか（§14.6。既定は false）
  "cannot_determine_as_review_required": false
}
```

★**machine check の error（M01-M05）も自動excludeにはしない。** 「自動採否は D01 のみ」の方針に従い、
壊れたマスクであっても「除外するのか修正を依頼するのか」は人間が決める。review_required として上げる。

### 14.6 `cannot_determine` の扱い ★方針決定済み

**既定 `cannot_determine_as_review_required: false`。**
`S05` の `cannot_determine`（参照マスクが無い）**501 annotation は、現時点では FiftyOne review 対象に含めない。**

| フェーズ | policy | FiftyOne 目視対象 |
|---|---|---|
| **まず これ** | `false`（既定） | **最大約140 annotation**（自動検出されたもの） |
| 必要になったら | `true` | +501 → 約640 annotation |

config 1行で切り替えられるようにしておく。

内訳（重複があるため実数はこれより少ない。正確な値は Phase 4 の出力で確定）:
D02 0 / D03 最大4 / D04 最大78 / S03 25 / S04 2 / S05 error+warning 32 / M07 bbox 11。

### ★ 「検査して問題なし」と「検査できないまま keep」を区別する

501件を review に回さない以上、**`keep` の中身を1種類にしてはいけない。**
`selection_decisions.csv` に列を1つ足し、reason を分ける:

| 追加列 | 内容 |
|---|---|
| **`unverified_checks`** | この annotation について `cannot_determine` を返した check_id をパイプ区切り。無ければ `none` |

`keep` の reason は3種類:

| reason | 意味 | 実測 |
|---|---|---|
| `no_issue_detected` | 全checkが `checked` で問題なし | |
| **`kept_without_full_check`** | ★**一部checkが判定不能のまま keep**（体外判定が未実施 等） | **501**（S05 参照マスク無し） |
| `visually_valid` / `true_small_lesion` / ... | 人間が見て keep と判断 | |

これにより:
- `selection_decisions.csv` を `unverified_checks != none` で絞れば **後から501件を review に回すのは1コマンド**
- 「issueが無かった」と「検査できなかった」が CSV 上で混ざらない
- policy を `true` に切り替えたとき、対象が `unverified_checks` そのものになる（実装の一貫性）

summary にも `keep` を内訳付きで出す（§18）。

---

## 15. FiftyOne review

FiftyOne は目視レビューの主手段。ただし **validation本体から完全にoptional**。
FiftyOne なしでも `scan` / `check` / `report` / `automatic selection` まで動作する。
`import fiftyone` は `review/` パッケージ内部のみ。

### 表示する情報（annotation単位）

`geometry_uid` / `code_system` / `code` / `code_text` / `user` / `timestamp` / `is_latest` /
`duplicate_group_id` / issue list。加えて `path_mask` と `path_original_mask` を比較可能にする。

### 実装（FiftyOne 1.21.0 の wheel ソースで実在確認済み）

| 必要な機能 | 確認結果 |
|---|---|
| Label単位のtags | `fiftyone/core/labels.py:336` `tags = fof.ListField(fof.StringField())`（`_HasID` 経由で `Detection` も保持）✓ |
| インスタンスマスクをディスクに置く | `Detection._MEDIA_FIELD = "mask_path"` ✓ → 2000x2400 の配列を MongoDB に入れずに済む |
| tag操作API | `tag_labels` / `untag_labels` / `count_label_tags` / `filter_labels` / `match_labels` / `select_labels` 実在 ✓ |
| Python 3.12 | `requires_python >=3.10` ✓ |

```
Sample(filepath=<DICOM由来PNG>)
  ├─ tags       : ファイル単位の事実のみ (auto:cannot_determine 等)
  ├─ Sample field: institution / study / series / file_id / source_json / spacing / manufacturer
  ├─ thorax_band : fo.Segmentation(mask_path=...)   S05がなぜ発火したかが見える
  ├─ final       : fo.Detections   path_mask 由来。★レビュー対象
  └─ original    : fo.Detections   path_original_mask 由来。比較用
```

各 `fo.Detection` に `mask_path`（bboxで切り出したPNG、数KB）とカスタム属性を持たせる。
**画像Sample全体ではなく、可能な限り annotation/label 単位で扱う。**

### review field と tag の使い分け

**正式なreview結果は field で管理する**（`review/review_schema.py` に語彙を定義）:

```
review_status   : pending | keep | exclude | uncertain
review_reason   : duplicate | invalid_mask | stray_component | outside_body | ...
review_comment
reviewer
reviewed_at
```

**tags はフィルタ用**:

```
auto:exact_duplicate / auto:label_conflict / auto:near_duplicate / auto:contained_duplicate
auto:tiny_region / auto:stray_component / auto:outside_body / auto:original_final_divergence
review:pending / review:keep / review:exclude / review:uncertain
reason:duplicate / reason:invalid_mask / reason:stray_component / reason:outside_body
```

`auto:` は builder が issues から機械生成し、再構築のたびに**貼り直す**。
`review_*` field と `review:` tag は**絶対に触らない**。
これにより閾値を変えて再検出しても人間の判定が保存される。

### ★ FiftyOne DB を正本にしない

必ず `review_decisions.json` / `review_decisions.csv` へ export でき、import もできる。**`geometry_uid` をキー**にする。

目標（§13 の回帰項目でもある）:

```
FiftyOne DB削除 -> dataset再構築 -> review_decisions.json import -> review状態復元
```

これが成立すれば、新JSONが追加されて再スキャンしても過去のレビュー結果を失わない。

### Precision の算出

`auto:` と `review:` を分けたので、自動ルールの Precision を後から計算できる:

```
auto:outside_body        26件 -> exclude 18 / keep 8 / 未レビュー 0   Precision 0.69
auto:tiny_region          1件 -> ...
auto:contained_duplicate 39件 -> ...
```

`D04` の Precision が極端に低ければ「入れ子の所見は正常」と結論してルールごと外す、
`S03` のレビュー帯105件の結果から**閾値を実データで確定させる**、といった判断ができる。

### ★ 目視完了の判定

FiftyOne で一部を確認しただけで Development JSON を確定生成させない。
review対象annotation について 4状態を管理する:

```
pending    : まだ人が見ていない
keep       : 見た結果、開発データに使う
exclude    : 見た結果、除外する
uncertain  : 見たが判断できなかった（要相談・保留）
```

**目視レビュー完了の基本条件は `pending == 0`。**

`review export` と `report` は毎回この進捗を出す:

```
review対象      138      (policy: cannot_determine_as_review_required=false)
  pending        110      <- 0 になれば目視完了
  keep            18
  exclude          8
  uncertain        2

参考: 判定不能のまま keep されているもの  501  (unverified_checks != none)
      policy を true にすると review対象に加わる
```

### review 結果の export / import

FiftyOne の MongoDB 内だけに保存しない。目視後に必ず `review_decisions.json` / `.csv` へ export する。
**`geometry_uid` をキー**にする（全域一意なので、JSONが追加・再エクスポートされても対応が壊れない）。

```json
{"geometry_uid": "A", "decision": "keep",    "reason": "valid_nested_annotation",
 "decision_source": "human", "reviewer": "...", "reviewed_at": "...", "comment": ""}
{"geometry_uid": "B", "decision": "exclude", "reason": "invalid_duplicate",
 "decision_source": "human", "reviewer": "...", "reviewed_at": "...", "comment": ""}
```

import も可能にする。これが成立していれば:

```
FiftyOne DB削除 -> dataset再構築 -> review_decisions.json import -> review状態復元
```

が成り立ち、閾値を変えて再検出しても、新JSONが追加されて再スキャンしても、過去の判定を失わない。

---

## 16. 環境（Linux / SSH / uv / FiftyOne DB）

### 接続経路とApp

```
Windows -> SSH -> 踏み台 -> SSH -> 本サーバー
```

validation と FiftyOne は本サーバーで実行。**FiftyOne App をインターネットへ直接公開しない。**
本サーバーで `localhost:5151` に起動し、SSH port forwarding で Windows の `localhost:5151` から見る。
README に起動方法と tunnel 方法を記載する。実測: port 5151 未使用、`AllowTcpForwarding` 既定 `yes`。

### uv / パス

Python dependency は uv で管理し、OS system Python を直接使用しない。
プロジェクトは `/mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation/` で実行する。

ユーザー単位のツール/cache/database は `$HOME` 基準。
★**`/home/nakamura` をコードへハードコードしない。** Python では `Path.home()`、shell では `$HOME` を使う。

### FiftyOne database

```bash
export FIFTYONE_DATABASE_DIR="$HOME/.fiftyone/var/lib/mongo"
```

実測: `findmnt -T "$HOME"` → `/dev/md0` **ext4（ローカル）** なので使用可。
NFS等の場合は別のローカルディスクへ変更する。**database path は configurable、ハードコードしない。**
（README に `findmnt -T "$HOME"` での確認手順を記載する。）

画像/DICOM/mask/JSON は `/mnt` 側をそのまま参照する。

### cache の分離

```
validation cache : output/cache/<fingerprint>/     JSONL、追記専用、resume可
FiftyOne MongoDB : $FIFTYONE_DATABASE_DIR
```

は**完全に別物**。FiftyOne DB が無くても validation 結果は再現可能。

---

## 17. output

```
output/
├── cache/<fingerprint>/
│   ├── manifest.json          schema版・各JSONの実体パス/サイズ/mtime/sha256・進捗
│   ├── files.jsonl
│   ├── masks.jsonl
│   └── pairs.jsonl
│
├── validation/<fingerprint>/
│   ├── issues.json                  何を検出したか。1 annotation に複数行あり得る
│   ├── issues.csv
│   ├── summary.md
│   ├── area_distribution.json / .png
│   ├── selection_decisions.json     ★全annotationの最終採否マスタ。1 annotation = 1行
│   ├── selection_decisions.csv      ★同上（1817行）
│   └── review/
│       ├── review_manifest.json     FiftyOne構築用（fiftyone非依存の中間形式）
│       ├── review_decisions.json    ★人間の判定。review対象のみ。geometry_uid キー
│       └── review_decisions.csv
│
└── development/<dataset>/<version>/
    ├── development.json
    └── selection_summary.md
```

`selection_decisions` は `validation/` 側に置く（`development/` は「確定して生成した結果」なので、
まだ pending が残っている段階の採否マスタはこちらが正しい置き場）。

`fingerprint = f"{version_id}_{sha1(各JSONのname:size:mtime:sha256)[:12]}"`
（★3ファイルとも `version_id="2.2"` なので version_id 単独では使えない）

cache は **JSON Lines / 追記専用**。`mask行 → pair行 → file行` の順に書き、file行の無い孤児行は
読み込み時に捨てる —— これだけで crash 耐性と resume が成立する（ロック不要）。
`--force` で truncate、`--limit N` でスモークテスト、`SCHEMA_VERSION` 不一致なら再スキャンを促す。

---

## 18. Development Dataset 生成

**元JSONは変更しない。** 入力は Original JSON と `selection_decisions` の2つだけ
（issues.csv や FiftyOne DB は参照しない —— 採否の正本は `selection_decisions` に一本化する）。

```
Original Dataset JSON + selection_decisions  ->  development.json
```

```bash
uv run segmentation-validation build-dataset
```

原則、`final_decision = keep` の annotation だけを残し、`exclude` は除外する。

### ★ pending / uncertain が残っていたら確定生成しない

デフォルトでは以下の場合に**生成を停止**する:

```
pending   > 0    -> 停止
uncertain > 0    -> 停止
```

明示 override を用意する。**override 時も暗黙のデフォルトを置かず、扱いを明示させる**:

```bash
build-dataset --allow-pending   --pending-as exclude|keep     # 両方必須
build-dataset --allow-uncertain --uncertain-as exclude|keep   # 両方必須
```

`--allow-pending` だけを渡してもエラーにする。「保留のまま黙って入った/落ちた」を起こさないため。
override を使った場合は `development.json` の meta と `selection_summary.md` に**その旨を必ず記録**する。

### 実装上の決定事項

- 出力は Original JSON と**同一のスキーマ形状**（`dataset[inst][study].series_list[...].file_list[...]`、
  `version_id` / `version_date` も保持）とし、`exclude` された annotation を `annotations[]` から取り除く
- **excludeの結果 annotation が0件になった file entry は削除せず `annotations: []` として残す。**
  取り除くと annotation除外によって元の画像母集団が黙って変わるため。件数を `selection_summary.md` に出す
  （実測: 自動exclusionだけなら各exact groupはサイズ2・同一ファイル内で1件keepするので**0件になるfileは発生しない**）
- `development.json` の meta に、元JSONのfingerprint / `selection_decisions` のハッシュ /
  decision の件数内訳 / override の有無 / 生成日時 / ツール版を埋める
- **Original JSON の不変性をハッシュで検証**してから書き出す（§21 の回帰項目）

### `selection_summary.md` の内容

「validationは終わったのか」「FiftyOne目視は終わったのか」「Development JSONを作ってよい状態なのか」を
**このファイルを見るだけで判断できる**ようにする。

```markdown
## 採否サマリ
total annotations              1817   (brush 1665 / bbox 151 / elliptical 1)
  keep                         ####
    no_issue_detected          ####     全checkが checked で問題なし
    kept_without_full_check    ####  <- ★判定不能のまま keep（未検証）
    human keep                 ####
  exclude                      ####
  pending                      ####
  uncertain                    ####

## 判断の内訳
automatic decisions            ####   (うち D01 による自動exclude  101)
human decisions                ####
default (keep)                 ####

## ★ 検査できなかったもの (cannot_determine)
S05 参照マスク無し              501   (01272 は 0/296 ファイル)
  cannot_determine:no_reference          ####
  cannot_determine:lung_only             ####
  cannot_determine:reference_corrupt     ####
  cannot_determine:reference_shape_mismatch ####
S03 spacing無しで mm² 算出不可   ####
現在の policy: cannot_determine_as_review_required = false
  -> これらは keep のまま。review に回すには config を true にする

## 参照マスクのカバレッジ（毎回実測）
ファイル  ###/### (##.#%)   annotation  ####/#### (##.#%)
JSONごとの内訳表

## FiftyOne 目視の進捗
review対象                      ####
  review完了                    ####
  review未完了 (pending)        ####     <- 0 が完了条件

## 影響
annotationが0件になったfile     ####
除外により失われた病変クラス     (code_systemごとの増減)

## 判定
[OK] / [BLOCKED: pending が #### 件残っています]
Development JSON を生成してよいか: yes / no（理由）
注記: kept_without_full_check が #### 件あります（体外判定が未実施）
```

---

## 19. CLI

```
segmentation-validation scan
segmentation-validation check                 # -> issues.json / issues.csv
segmentation-validation select                # ★ issues + review_decisions -> selection_decisions
segmentation-validation report                # -> summary.md / area_distribution
segmentation-validation run                   # scan -> check -> select -> report

segmentation-validation review build          # selection_decisions + assets -> FiftyOne dataset
segmentation-validation review launch         # localhost:5151 で App 起動
segmentation-validation review export         # FiftyOne -> review_decisions.json/csv
segmentation-validation review import         # review_decisions.json -> FiftyOne
segmentation-validation review status         # pending / keep / exclude / uncertain の進捗

segmentation-validation build-dataset         # selection_decisions -> development.json
```

`select` を独立コマンドにする理由: 目視のたびに
`review export` → `select` → `report` を回すのが実運用のループになるため。
`review_decisions.json` が無ければ全review対象が `pending` になるだけで、正しく動く。

`--config` 未指定なら `config/default.json`。`--set thresholds.duplicate_iou_near=0.9` で単発上書き。
終了コード: 0=errorなし / 1=errorあり / 2=ツール障害。
`build-dataset` は pending/uncertain が残っていれば override なしで **exit 1**。

`--config` 未指定なら `config/default.json`。`--set thresholds.duplicate_iou_near=0.9` で単発上書き。
終了コード: 0=errorなし / 1=errorあり / 2=ツール障害。

config で外出しするもの: **対象JSONパス（リスト or glob）**、`image_root` / `annotation_root`、
**参照マスクのルート3種とパス導出規則**、全 threshold、`FIFTYONE_DATABASE_DIR`、review画像フォーマット。
→ **新しいdatasetの追加は config の1行追加**で済む。

---

## 20. 実装順序

| Phase | 内容 |
|---|---|
| **1** | 既存 `overlay_masks.py` の整理と非退行確認。`opencv-python-headless` へ差し替え。**潜在バグ修正**: `main()` がフィルタ済み `masks` を未フィルタの `target.masks` と `zip` していて、マスクを1枚でもスキップすると対応がずれる（`--original` で97件が null になるので実際に踏む）→ `(entry, mask)` のペアを持ち回る |
| **2** | `AnnotationRecord` / `FileGroup` / `DatasetAdapter` / `adapters/engineer_set.py` |
| **3** | JSONのみで実行可能な machine checks（M06 / M07）。この時点で degenerate bbox 7件・範囲外4件・先頭スラッシュ2件が出る |
| **4** | `Issue` / `SelectionDecision` / report schema を確定（producer が増える前に固める）。**`select` コマンドの骨格もここで作る** —— この時点で全1817 annotation が `final_decision=keep, reason=no_issue_detected` の `selection_decisions.csv` を出せるようにする。以降のPhaseは「keep から pending / exclude へ移す」だけになり、**1 annotation = 1行の不変条件が最初から守られる** |
| **5** | `scan` / `measure` / `cache` |
| **6** | M01-M05 / M08 |
| **7** | duplicate checks: D01 exact / D02 label conflict / D03 near / D04 containment / duplicate group |
| **8** | area distribution / tiny / stray component |
| **9** | `path_original_mask` comparison / PI6 outside body（`datasets/pi6/`） |
| **10** | FiftyOne dependency / environment 導入。**core validation が完成してから行う** |
| **11** | FiftyOne dataset builder / review workflow |
| **12** | review decision export / import。`select` に human decision のマージと §14.3 の優先順位ラダーを実装 |
| **13** | Development Dataset JSON builder + `selection_summary.md` + pending/uncertain のゲート |

Phase 9 まででレビュー用の材料は全部揃うので、**FiftyOne が使えなくても検証結果は成立する**。

---

## 20.5 ★Phase 10-12 実装後の実測（FiftyOne）

| 項目 | 実測 |
|---|---|
| 環境 | fiftyone 1.21.0 / mongod 起動成功（`$HOME/.fiftyone/var/lib/mongo`、ext4）|
| cv2 provider | `opencv-python-headless` **1種類のみ**（衝突は起きなかった）|
| アセット書き出し | 210画像 / 473マスク / 150バンド / **397MB** / **8分46秒**（2回目は45秒）|
| dataset | 210 Sample / **521 Detection** |
| 目視待ち | annotation **251** / 画像 **33**（採否マスタと完全一致）|
| 往復テスト | **成功**。DB削除→再構築→import で annotation 3件・画像2件の判定が完全復元、`auto:` タグ12種も保持 |
| fiftyone の隔離 | AST で静的検査。`review/` の3モジュールのみ（違反0）|

**★実装中に見つけた不具合**

1. **マスクを持たない bbox 11件が目視対象から漏れていた。**
   `path_mask` が無いと `asset.boxes` に入らず manifest から落ちる。
   ところがこの11件は M07（座標が壊れている）で、**座標そのものが目視対象**。
   JSONの `min_x/min_y/max_x/max_y` から枠を出すようにした。
   退化した bbox は表示できる最小サイズまで広げ、`original_bbox` に原座標を残す。
   → 目視待ちが 240 → **251** になり採否マスタと一致した。
2. **保存ビューが1つしか作られていなかった。**
   FiftyOne はビュー名を slug 化するので、**日本語だけの名前は空 slug で `ValueError`**。
   `① 目視待ちの annotation` だけ通っていたのは "annotation" が残ったから。
   ASCII 名 + 日本語の description に変更。例外を debug ログに落としていたのも警告に上げた。
3. **`review export` が機械の判定まで人間の判定として書き出していた。**
   dataset には目視対象の周辺 annotation も文脈として載っており、
   D01 の自動 exclude や既定の keep を持つ。全部書き出すと `select` で
   `decision_source=human` になり、`assert_invariants`（human なら reviewer 必須）で落ちた。
   **manifest を基準線にして変化した分だけ**を人間の判定とする方式に変更
   （reviewer の入力漏れに強い）。273件 → **3件**（実際に入力した数）。
4. **`select` が画像単位の人間の判定を読んでいなかった。**
   `review_decisions.json` の `image_decisions` を無視していたので、
   側面像と判定しても画像の採否に反映されなかった。

---

## 20.6 ★README通り1周した際に見つけた性能上の不具合（2026-09-04）

**OpenCV の内部スレッドが `--jobs` と掛け算になっていた。**

走査はファイル単位で `ThreadPoolExecutor(--jobs)` に並列化しているが、
OpenCV は既定でマシンの全コアぶんのスレッドプールを使う。
このサーバーは **256コア**なので、`connectedComponents` / `distanceTransform` の
呼び出しごとに `--jobs 8` × 数十本が立ち上がる。

実測（他ユーザーの学習ジョブが走っている状態で）:

| | スレッド数 | CPU | load average | 走査の進み |
|---|---|---|---|---|
| 修正前 | **454** | 1428% | **143** | ~2件/分（1083件で数十分） |
| 修正後 | 199（大半は import 時の待機プール） | 515% | 37 | **7〜8件/秒 → 全1083件 2分35秒** |

単体で確認した機序:

```
cv2 の既定スレッド数 = 256（nproc と同じ）
connectedComponents + distanceTransform を2回呼ぶだけで OSスレッド 446本
cv2.setNumThreads(1) 後に8並列で同じ処理 → 191本（呼び出しでは増えない）
```

対処: `core/cpu.py` の `limit_native_threads()` を追加し、
`measure.scan()` と `review.export_assets()` の入口で `cv2.setNumThreads(1)` を呼ぶ。
**並列度は `--jobs` 側だけで担保する**という不変条件にした。

> 上表の速度差には他ユーザーのジョブ終了による負荷低下も混じっているため、
> 30倍という倍率そのものは修正の効果とは言い切れない。
> ただし「256スレッド × jobs」という機序と、それが共有サーバーの
> 他ジョブまで巻き込むことは単体測定で確認済み。

## 20.7 ★README通り1周した際に見つけた成果物の欠陥（2026-09-04）

いずれも「手順どおり操作すると成果物が黙って壊れる」もの。ゲートを追加した。

### 1. `review build` が export していない目視結果を消していた

`build_dataset()` は `fo.Dataset(name, overwrite=True)` で dataset を作り直し、
`review_status` を manifest（= `selection_decisions` の機械の採否）で塗り直す。
つまり **`review export` していない判定は消える**。
実際に往復テストで入れた annotation 3件・画像2件の判定が消えた。

「`auto:` は貼り直し、人間の判定は保持」という方針が成立するのは
**`review export` 済みである場合だけ**で、そこが手順にも実装にも書かれていなかった。
閾値を変えて再検出する運用があるので、目視の成果を失う事故になり得る。

対処: `review/export_decisions.py` に `unexported_human_decisions()` を追加し、
**manifest を上書きする前**に DB と `review_decisions.json` を突き合わせる。
差があれば `review build` を **exit 1 で停止**する（`--discard-unexported` で強行）。
基準線には**作り直す前のディスク上の manifest** を使う —— 新しい manifest を
基準にすると「変化なし」に見えて検出できない。

### 2. `check --only` の部分結果で `select` が採否を壊していた

`issues.json` は毎回上書きされるので、`check --only M06,M07` の後に `select` すると
**未実行のチェックが「検出なし」と区別できない**。実測の被害:

```
正しい採否   keep 1465 / pending 251 / exclude 101
部分結果から keep 1705 / pending  11 / exclude 101   ← 240件の目視対象が消える
```

対処: `issues.json` の meta に `checks_executed` / `checks_partial` を刻み、
`select` は部分結果を見たら **exit 1 で停止**する（`--allow-partial` で強行）。

### 3. `selection_summary.md` が override 時に落とした画像枚数を過少報告していた

「画像そのものを落とした枚数」を `image_decisions` の `exclude` 数だけで出していた。
`--pending-as exclude` を使うと pending の画像も落ちるので、
**実際に32枚落としているのに 1 と表示**していた。
`selection_summary.md` は「これを出荷してよいか」を判断する文書なので致命的。

あわせて、override で生成した場合の末尾の判定が `[BLOCKED] ... 生成してよいか: no`
のままで、**生成物が既に存在することが読み取れなかった**。
`[OVERRIDE]` に分け、目視未完了の状態のものであると明記するようにした。

---

## 21. 回帰テストと synthetic test

### 既存実データの既知値（実測済み。合わなければ実装バグ）

| 項目 | 期待値 |
|---|---|
| D01 exact & same label | **101ペア** / group **101個**（全てサイズ2）/ 自動exclude **101 annotation** |
| D01 timestamp tie / 欠損 / parse不能 | **0 / 0 / 0** |
| D02 exact & label conflict | **0ペア**（回帰検知） |
| D03 near (IoU≥0.95) | **2ペア** |
| D04 containment (≥0.98) | **39ペア**（うち36がラベル相違、IoU 0.002-0.09） |
| duplicate group（全体） | **139個**（サイズ2:136、サイズ3:3） |
| 旧条件の再現（IoU≥0.9 & both_latest & user相違） | **85ペア**（既存CSVと一致） |
| S03 tiny annotation | **1件**（jsrt `323fe9b4-…`、1px） |
| S03 stray component | **5成分 / 3 annotation**（`db2e2140` / `42fef131` / `2a4eec84`） |
| S04 original-final 不一致 | mask136 で **2件** |
| S05 outside body | ERROR **6件** / WARNING **26件** |
| S05 cannot_determine | **合計 509 annotation** = no_reference 500 / lung_only 8 / reference_corrupt 1（01272 は 0/296 ファイル） |
| 参照カバレッジ | ファイル **519/829 (62.6%)** / annotation **1156/1665 (69.4%)** |
| M07 bbox | degenerate **7件** / 範囲外 **4件** |
| M08 | bbox・region_count とも**不一致0件**（回帰検知） |
| M06 先頭スラッシュ | **2件**（jsrt `CXJSR00000140_000`） |
| timestamp | 1817/1817 が `YYYY-MM-DD HH:MM:SS+00:00` / UTC / parse可 |
| **`selection_decisions.csv` の行数** | ★**1817行**（brush 1665 / bbox 151 / elliptical 1）。`geometry_uid` は重複なし。**AnnotationRecord の件数と厳密に一致すること** |
| **automatic exclude** | **101 annotation**（D01 のみ） |
| **annotationが0件になるfile** | 自動exclusionだけなら **0件** |
| **`kept_without_full_check`** | **501 annotation**（S05 参照マスク無し）。`unverified_checks` が `none` でない行数と一致すること |
| **FiftyOne review対象** | 既定 policy (`cannot_determine_as_review_required=false`) で **最大約140**。policy を `true` にすると **+501** |

### selection_decisions の不変条件（毎回assertする）

```
len(selection_decisions) == len(annotation_records) == 1817
geometry_uid は一意、欠損なし
final_decision は {keep, exclude, uncertain, pending} のいずれか
review_status=not_needed なら final_decision in {keep, exclude}
reason == "kept_without_full_check"  ==>  unverified_checks != "none"
  （逆は成立しない。判定不能を持つ annotation でも review_required な Issue が
    あれば pending が優先される —— ラダーの4が5より前だから）
policy を true にしたとき、review対象 == 従来の対象 ∪ {unverified_checks != "none"}
decision_source=human なら reviewer と reviewed_at が非null
development.json の annotation 総数 == keep の件数
development.json の file entry 数 == Original の file entry 数（0件fileも残すため）
```

### 実装後の実測値（Phase 1-9 / 13 完了時点、2026-09-04）

パイプラインを全走査で通した結果。計画の期待値と食い違ったものは ★。

| 項目 | 実測 | 備考 |
|---|---|---|
| 走査時間 | **1分53秒**（1083ファイル / jobs=8） | キャッシュ 6.9MB / files 1083・masks 3233・pairs 1446行 |
| mask行 | final 1665 + original 1568 = 3233 | `path_original_mask` の件数と一致 |
| `M01`-`M05` / `M08` | **全て0件** | `path_mask` は 1665/1665 が mode=L・uint8・値{0,255}・bbox一致・region_count一致 |
| ★ `M02`/`M03` を original に同条件で適用すると | 1462 / 1459 件のノイズ | RGBA の alpha は全件存在し二値化は一意。**original を同じ土俵で検査してはいけない**。M02 は「alphaの無い多チャンネル」のみ（実測0件）、M03 は `path_mask` 限定に修正 |
| `D01` | 202 issue = **101ペア × 2** | 全て mask136。施設別 eiju_sogo 41 / ishikawa_health_service 29 / ofuna_chuo 9 |
| `D02` | 0 | 回帰検知として稼働 |
| `D03` | 4 issue = 2ペア × 2 | |
| `D04` | 78 issue = 39ペア × 2（同ラベル3 / 異ラベル36） | |
| `S03` | tiny 1 / stray **5成分・3 annotation** / 下限未満 19 / レビュー帯 105 | ★下限未満19 + tiny 1 = 計画の20 |
| ★ `S04` | **6件**（mask136 に2件、01272 に2件、1298 に2件） | 計画は mask136 の2件のみ既知。**他2JSONで新たに4件**見つかった（IoU 0.14〜0.40 で差分が大きい） |
| `S05` | error 6 / warning 20 / info 24 = 50 | ★計画の「warning 26」は error を含む数。error 6 + warning 20 = 26 で一致 |
| `S05` 判定不能 | 509 = no_reference 500 / lung_only 8 / corrupt 1 | |
| S05 アノテータ別（warning以上） | m.shinzato 17 / kolive23 5 / 他3名 各1 | kolive23 は 14件中5件 = 35.7% |
| 自動採否 | **exclude 101 / keep 101 / 自動決定不能 0** | timestamp tie・欠損・parse不能は0件 |
| `selection_decisions` | **1817行**、geometry_uid 一意 | keep 1465 / exclude 101 / pending 251 |
| review 対象 | **258**（うち pending 251） | 差の7件は D01 で exclude 確定済み |
| `kept_without_full_check` | **481** | 509 のうち28件は review_required も持つため pending が優先 |
| 除外による病変クラスの減少 | Findings/010 915 → 814（-101） | 全て完全一致の重複なので実質的な損失なし |

**★実装で判明した設計上の修正**

1. **`policy_for` の照合は2段階が必要だった。** 派生ID（`M07_BBOX_DEGENERATE`）が
   モジュールID（`M07_BBOX_GEOMETRY`）と文字列一致しないため、当初の実装では
   未分類扱いで安全側の pending に落ちていた。
   「具体的な指定が勝つ」二段階照合にした結果、`M07` 全体を informational にしつつ
   座標が明確に壊れている `M07_BBOX_DEGENERATE` / `M07_BBOX_OUT_OF_IMAGE` の
   11件だけを review_required に上げられるようになった。
2. **`original` を `path_mask` と同じ条件で検査してはいけない**（上表参照）。
3. **`build-dataset` の「0件になったfile」は元から空のファイルを除外して数える。**
   元から annotation を持たないファイルが多数あり、含めると意味のない数になる。
4. `unverified_checks != none ⟹ kept_without_full_check` は**片方向のみ**成立する
   （pending が優先されるため）。

### synthetic test（実データに存在しないケースを担保する）

★ 実測で **tie=0 / 欠損=0 / parse不能=0 / D02=0** と分かったため、
これらの分岐は**実データでは一切検証できない**。synthetic test が唯一の担保になる。

**Duplicate**: exact同一mask+同label / exact同一mask+異label / timestamp A<B / A>B / **tie** /
**欠損** / **不正形式** / both is_latest=1 / latest+historical / near duplicate / containment duplicate / A-B-C group

**Mask**: empty / non-binary / wrong channel / resolution mismatch / bbox mismatch / region_count mismatch

**Tiny**: 1px / small annotation / huge mask + 1px stray component

**Review**: keep / exclude / uncertain / pending

**Development build**: automatic exact duplicate exclusion / human exclusion / pendingあり / uncertainあり /
**original JSON不変**（ハッシュで確認）

**Environment**:
- FiftyOne なしで core validation が動く（`import fiftyone` が `review/` 外に無いことを静的に検査）
- opencv provider が1種類
- FiftyOne DB と validation cache が分離
- review_decisions export/import 可能
- **FiftyOne DB を再構築して review 状態を復元可能**

最後に `uv run ruff check src/ && uv run ruff format --check src/`。

---

## 22. 前Planから削除・変更したもの

### 削除

| 対象 | 理由 |
|---|---|
| **S02 duplicate label check** | labels内の `(code_system,code)` 重複を主要validation defectとして扱う設計を削除。目的はmask/geometryの品質確認であり、labels metadataの重複だけでは採否に直結しない |
| **HAS_ORIGINAL_MASK の大量出力** | `path_original_mask` の存在自体は正常なannotation workflowの結果 |
| **静的PNGを主review手段とする設計**（`viz/samples.py`） | FiftyOne を主review UIとする。PNG overlay は debug/fallback のみ |

### 変更

| 対象 | 以前 | 変更後 |
|---|---|---|
| duplicate | IoU中心の suspicious check | **exact / exact label conflict / near / containment / duplicate group** に分類 |
| exact duplicate | 目視 | **完全一致+same label+timestamp比較可能 → 最新を自動keep、古いものを自動exclude** |
| tiny | `<20mm² = error` | **warning + high review priority**。仕様違反ではなく実測分布上の外れ値であるため |
| outside body | `core/thorax.py` | **PI6 dataset-specific** (`datasets/pi6/outside_body.py`) |
| review | `samples.py` の PNG目視 | **FiftyOne + portable review_decisions** |
| output | issues を出して終了 | **issues → review → SelectionDecision → Development Dataset JSON** まで |
| 採否の正本 | 暗黙（issues から推測） | ★**`selection_decisions.csv` に一本化。全annotationが必ず1行**。`build-dataset` はこれだけを見る |
| 「Issueあり」の意味 | 曖昧 | ★**Issueあり ≠ exclude**。tiny でも目視で妥当なら keep |
| 目視の完了判定 | なし | ★**`pending == 0`**。pending/uncertain が残ると `build-dataset` は既定で停止 |
| 形式知識 | `core/records.py` に直書き | **DatasetAdapter** を介す |
| 「情報が無い」 | 暗黙に pass | **checked / cannot_determine / not_applicable** の3値 |

### 維持

1回scan → measurement cache / checks は pure function / JSONL cache / fingerprint / resume /
`--force` / `--limit` / machine checks / bbox・region_count consistency / IoU + containment /
connected component detection / mm² による tiny evaluation / cannot_determine /
PI6 での outside-body 実測結果 / issues.json・issues.csv・summary.md / Ruff

---

## 最終方針

```
自動で明らかなものを判定
  -> 判断できないものだけ FiftyOne
  -> 人間の判断を portable に保存
  -> 全annotationの採否を selection_decisions に確定
  -> 元JSONを変更せず Development Dataset を生成
```

成果物の関係:

```
issues.csv              何を検出したか        1 annotation に複数行あり得る
review_decisions.csv    人間が何と判断したか   review対象のみ
selection_decisions.csv 最終的に使うか        ★必ず 1 annotation = 1行（1817行）= 採否のmaster table
development.json        keep だけを反映        Original と同一スキーマ、0件fileも残す
```

duplicate については明確な線引きを設ける:

```
完全一致          -> timestamp で自動解決   (実測 101ペア、tie 0)
完全一致ではない  -> FiftyOne で確認        (near 2 / containment 39)
```

FiftyOne は review UI であって、validation結果や review結果の唯一の保存先にはしない。
新しい dataset を追加する際は `DatasetAdapter` を追加し、既存 check を可能な範囲で再利用する。
