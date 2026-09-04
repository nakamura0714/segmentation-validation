# Dataset / Prediction 標準形式

## 目的

本ドキュメントは、機械学習用データセットを YAML または JSON で表現するための標準形式を定義する。

この形式は、次を明示的に記述できるように設計されている。

- データセット全体のメタ情報
- 各サンプル属性のスキーマ（型定義）
- 各サンプルの実データ
- ファイルパス、画像、動画、3D volume、DICOM、ラベル、位置情報、マスクの表現方法

---

## 全体構造

データセットファイルのトップレベルは、各属性のスキーマ定義 `structure` を内包する `meta` と、 サンプル本体 `samples` から成る。`samples` は次の 2 通りで記述できる。

```yaml
# (A) samples セクション形式
meta:
  structure:
    # samples に含まれる各属性のスキーマ定義（後述）
samples:
  # サンプルIDごとのデータ本体（後述）

# (B) samples キー省略形式
# meta / samples の両方が無い場合のみ、トップレベルに sample_id を直接置く
# structure はファイル内に持てないため、スキーマは別途外部から与える
case_001:
  ...
case_002:
  ...
```

`meta` / `samples` は予約されたトップレベルセクション名であり、`sample_id` には使用できない。 (B) の省略形は `meta` も `samples` も持たない素のファイルに限る。`meta` を記述する場合は、 サンプル本体を必ず `samples:` セクション（A）で与えること。

---

## トップレベルキー

| key | 必須 | 説明 |
| --- | --- | --- |
| `meta` | ※ | データセット全体のメタ情報（`structure` を内包）。省略時は `structure` を別途外部から与える。(B) の省略形では `meta` を持てない |
| `samples` | ※ | サンプルIDごとのデータ本体。`meta` も `samples` も無い場合に限り、トップレベルの `sample_id` で与える (B) の省略形が使える |

> `meta` / `samples` はいずれも `※`（条件付き）。`samples` セクションが無い場合はトップレベル `sample_id` の省略形（B）で与え、その省略形では `meta` を持てない。`meta` を省略する場合は `structure` を別途外部から与える（`meta.structure` も外部指定も無ければ、属性を解釈できず不正）。

`meta` の中の主なキー:

| key | 必須 | 説明 |
| --- | --- | --- |
| `meta.structure` | ※ | サンプル属性のスキーマ定義（ファイル外から与えることも可）。**1 つ以上の属性が必要**（空の `{}` は不可。Prediction ファイルには適用しない） |
| `meta.content_type` | No | ファイル種別（`dataset` / `predictions`）。省略可。下記「ファイル種別（content_type）」節を参照 |
| （その他 `meta.*`） | - | データセット管理情報（下記「meta」節を参照） |

**ファイル種別の判定**: `meta.content_type` が明示されていれば、そのファイルが Dataset / Prediction の どちらであるかを自己申告する（下記「ファイル種別（content_type）」節）。省略・明示 `null` なら判定せず、 読み込む側が想定する種別に従う。これは下記「`samples` 記法の判定」（サンプル本体をどう与えたか）とは別軸である。

`samples` **記法の判定**: `samples` セクションがあればそれをサンプル本体とする。`meta` も `samples` も無い場合に 限り、`meta` / `samples` 以外のトップレベルキーを `sample_id`（B の省略形）とみなす。`meta` があるのに `samples` が無く他のトップレベルキーが並ぶ形はエラー（`meta` を書くなら `samples:` を明示すること）。

**管理情報の欠落**: `meta` 直下の管理情報（`dataset_name` など）はデータ本体の解釈には不要であり、 欠落していてもサンプルの解釈は可能である。ただし管理情報を一部でも持つ場合は、必須フィールド （`dataset_name` / `dataset_id` / `date` / `project` / `owner`）をすべてそろえること（欠けていると 標準形式に準拠しない）。管理情報を全く持たない形（`structure` を外部から与える、または `structure` のみの `meta`）はこの対象外である。管理情報フィールドの明示的な `null` は「欠落（未設定）」と同義に扱う （`date: null` も未設定。ただし `null` 以外の不正な日付形式は不正）。

---

## meta

`meta` には、データセット全体に関する情報と、サンプル属性のスキーマ定義 `structure` を記述する。

```yaml
meta:
  dataset_name: example_dataset
  dataset_id: dataset_001
  date: 2026-06-24
  description: Example dataset for image classification and detection.
  project: example_project
  owner: taro_yamada
  structure:
 # samples に含まれる各属性のスキーマ定義（後述）
 # ...
```

### 必須項目

| key | 型 | 必須 | 説明 |
| --- | --- | --- | --- |
| `dataset_name` | `str` | Yes | データセットの名前 |
| `dataset_id` | `str` | Yes | データセットのID。社内でユニークであること |
| `date` | `date` | Yes | 作成日（`YYYY-MM-DD`）。YAML では非クオートで記述し日付として解釈される。JSON では `"YYYY-MM-DD"` 文字列で記述する |
| `project` | `str` | Yes | 利用プロジェクト名 |
| `owner` | `str` | Yes | データセットの作成者名 |
| `structure` | `mapping` | Yes | サンプル属性のスキーマ定義（後述「structure」節） |

### 任意項目

| key | 型 | 必須 | 説明 |
| --- | --- | --- | --- |
| `content_type` | `str` | No | ファイル種別（`dataset` / `predictions`）。管理情報ではなく**形式情報**であり、明示した場合は読み込む側が想定する種別と一致しなければならない。下記「ファイル種別（content_type）」節を参照 |
| `description` | `str` | No | データセットの説明 |
| その他任意のkey | 任意 | No | データセット管理に必要な追加情報（`version`, `source` 等） |

例:

```yaml
meta:
  content_type: dataset
  dataset_name: chest_ct_nodule_dataset
  dataset_id: chest_ct_nodule_2026_001
  date: 2026-06-24
  description: Chest CT dataset for nodule detection and classification.
  project: chest_ct_ai
  owner: hanako_suzuki
  version: 1.0.0
  source: internal_annotation_project
```

---

## ファイル種別（content_type）

データセットファイルと Prediction ファイルは同じトップレベル構造（`meta` / `samples`）を持つため、 ファイル単体では区別できず取り違えのリスクがある。`meta.content_type` は、そのファイルがどちらであるかを ファイル自身に自己申告させる任意キーである。

> HTTP の `Content-Type` ヘッダとは無関係。ファイルの MIME タイプや YAML / JSON の別を表すものでもない。

### 値

| 値 | 意味 |
| --- | --- |
| `dataset` | データセットファイル |
| `predictions` | Prediction ファイル |

### 規則

- 任意項目。欠落、または明示 `null` の場合は種別の判定を行わない（この宣言を持たない既存ファイルもそのまま 有効である）。
- 明示されていて読み込む側が想定する種別と矛盾する場合はエラー（データセットとして読もうとしたファイルが `content_type: predictions` を宣言している、およびその逆）。
- 上記 2 値以外の未知の値（`prediction` のような単数形のタイポ、大文字表記など）もエラー（許容すると タイポで種別の整合性チェックが黙って無効化されるため）。
- 本形式を書き出すツールは、出力の `meta` に対応する `content_type` を常に自動付与することを推奨する。 これにより書き出されたファイルは以後すべて自己識別可能になる。
- `meta` を持てない samples キー省略形（「全体構造」の (B)）では宣言できない。その形は従来どおり読み込む側が 想定する種別に従う。
- 管理情報ではなく形式情報として扱うため、必須メタフィールドの欠落（「トップレベルキー」節）の対象には 含めない。

### 例

データセットファイル:

```yaml
meta:
  content_type: dataset
  dataset_name: example_dataset
  structure:
 # ...
samples:
 # ...
```

Prediction ファイル:

```yaml
meta:
  content_type: predictions
  structure:
 # ...
samples:
 # ...
```

---

## structure

`structure` には、`samples` に含まれる各属性のスキーマを定義する。`structure` は `meta` の中に置く （`meta.structure`）。以降のスキーマ説明では `structure:` ブロック単体で示すが、実ファイルでは `meta` の 配下に記述する。

データセットファイルの `structure` には**最低 1 つの属性が必要**である（空の `{}` は不可）。属性が 1 つも 無い `structure` はサンプルの実データを一切解釈できず、内容を黙って失わせるためである。この規則は Prediction ファイルの `structure` には適用しない（下記「Prediction 形式」節）。

```yaml
meta:
  structure:
    <sample_key>:
      type: <type_name>
      multiple: false
      description: <description>
      label_map:
        0: Negative
        1: Positive
```

### 属性定義の項目

| key | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| `type` | `str` | Yes | - | 属性のデータ型 |
| `multiple` | `bool` | No | `false` | `true` の場合、サンプル側の値はリストとして扱う |
| `description` | `str` | No | - | 属性の説明 |
| `label_map` | `dict` | No | - | ラベル値と表示名の対応表 |
| `extra_attributes` | `dict` | No | - | 座標・領域・マスク型に付与する補助属性の型宣言（後述「extra_attributes」） |
| `fields` | `dict` | No | - | `type: dict` 属性の各キーのスキーマ定義。`type: dict` にのみ指定できる（後述「type: dict と fields」） |
| `spatial_resolution_key` | `str` | No | - | 座標・領域・マスク型に適用する `SpatialResolution` 属性名の明示指定（後述「座標・領域・マスク属性への適用」） |

> `data_type` **は** `type` **のエイリアス**: 型指定フィールドとして `type` の代わりに `data_type` を 指定してもよい。`data_type` のみ指定 → `type` として扱う。`type` と `data_type` の両方を指定 → `type` を優先し、`data_type` は無視される（重複指定は避けること）。いずれも無い場合はエラー。`data_type` 以外の 未知キー（タイポ等）はエラーとなる。このエイリアスは `extra_attributes` 内の補助属性、および `type: dict` の `fields` 内の入れ子属性にも同様に適用される。

### type に指定可能な値

#### 基本型

| type | 説明 |
| --- | --- |
| `int` | 整数 |
| `float` | 浮動小数点数 |
| `bool` | 真偽値 |
| `str` | 文字列 |
| `dict` | 辞書。`fields` で各キーの型を宣言でき、宣言キーは宣言型で解釈される。宣言の無いキーは型宣言の対象外（後述「type: dict と fields」） |

#### ファイルパス型

| type | 説明 |
| --- | --- |
| `Path` | ファイルまたはディレクトリのパス |

YAML / JSON 上では `Path` は文字列として記述する。

```yaml
image_file: images/case_001.png
dicom_dir: dicom/case_001/
```

#### 座標・領域・マスク型

| type | 次元 | 種別 | 主な引数 |
| --- | --- | --- | --- |
| `Point2D` | 2D | 点 | `x`, `y` |
| `Point3D` | 3D | 点 | `x`, `y`, `z` |
| `Rect` | 2D | 矩形領域 | `x_min`, `y_min`, `x_max`, `y_max` |
| `Circle` | 2D | 円領域 | `x`, `y`, `r` |
| `Ellipse` | 2D | 楕円領域 | `x`, `y`, `r_x`, `r_y` |
| `Cuboid` | 3D | 直方体領域 | `x_min`, `y_min`, `z_min`, `x_max`, `y_max`, `z_max` |
| `Sphere` | 3D | 球領域 | `x`, `y`, `z`, `r` |
| `Ellipsoid` | 3D | 楕円体領域 | `x`, `y`, `z`, `r_x`, `r_y`, `r_z` |
| `Mask2D` | 2D | マスク領域 | `pixel_array`, `x_0`, `y_0` |
| `Mask3D` | 3D | マスク領域 | `pixel_array`, `z_0`, `y_0`, `x_0` |

> **次元の定まらない型名は指定できない**: 点は次元の定まる `Point2D` / `Point3D` を使う。次元が定まらない 汎用の点・位置を表す型名は `type` に指定できない（エラーになる）。

座標・領域・マスク型では、必要に応じて以下の共通オプションも記述できる。

| key | 型 | 説明 |
| --- | --- | --- |
| `uid` | `str` | アノテーション識別子（例: `"bbox_001"`）。指定する場合は文字列 |
| `score` | `float` | 信頼度スコア。範囲は `0.0 <= score <= 1.0` |
| `spatial_resolution` | `SpatialResolution` | ピクセル座標を実空間座標へ変換するための空間解像度 |
| `t_min` / `t_max` | `int` | 時間範囲（動画フレーム番号など）。整数で記述し、`t_min <= t_max` であること。両方そろえて指定する（片方のみは不可） |
| `t` | `int` | 単一フレーム時刻の糖衣。`t` だけ指定すると `t_min == t_max == t`。`t_min` / `t_max` との同時指定は不可 |
| その他任意のkey | 任意 | フィルタリングやメタ情報付与のための任意属性。型を宣言する場合は `extra_attributes` を用いる（後述） |

#### 標準独自型

| type | 説明 |
| --- | --- |
| `SpatialResolution` | 空間解像度。`x`, `y`, `z` の軸名をkey、float値をvalueに持つdict（時間軸 `t` は仕様外。後述「SpatialResolution」） |

### multiple

`multiple: true` の場合、サンプル側の値は必ずリストとして記述する。

```yaml
meta:
  structure:
    lesion_bbox:
      type: Rect
      multiple: true
      description: Lesion bounding boxes.
samples:
  case_001:
    lesion_bbox:
      - x_min: 10
        y_min: 20
        x_max: 100
        y_max: 120
      - x_min: 130
        y_min: 50
        x_max: 180
        y_max: 90
```

`multiple` を省略した場合は `false`（単一値）とみなす。

### extra_attributes

座標・領域・マスク型には、型の引数や共通オプション（`uid`, `score`, `spatial_resolution`）に加えて、 任意の補助属性を付与できる。このうち既定に無いもの（アノテータ、フェーズ、カスタムスコア等）について、 `structure.<key>.extra_attributes` で**名前と型を宣言**できる。

```yaml
structure:
  lesion_bbox:
    type: Rect
    multiple: true
    description: Lesion bounding boxes.
    extra_attributes:
      annotator:
        type: str
      annotation_phase:
        type: str
      custom_score:
        type: float
      reviewers:
        type: str
        multiple: true
        description: レビュア一覧
      malignancy:
        type: int
        description: 良悪性のサブラベル。
        label_map:
          0: benign
          1: malignant
```

各補助属性のスキーマで指定できる項目は次の通り。

| key | 型 | 必須 | デフォルト | 説明 |
| --- | --- | --- | --- | --- |
| `type` | `str` | Yes | - | 補助属性のデータ型（`type` に指定可能な値と同じ） |
| `multiple` | `bool` | No | `false` | `true` の場合、補助属性の値はリストとして扱う |
| `description` | `str` | No | - | 補助属性の説明 |
| `label_map` | `dict` | No | - | ラベル値と表示名の対応表（後述「label_map」と同じ） |

- `extra_attributes` で宣言されたキーの値は、宣言された型に従うこと（型不一致は不正）。
- 宣言の無い補助属性は**型宣言の対象外**であり、任意の値として記述できる。
- `extra_attributes` を宣言できるのは座標・領域・マスク型の属性のみ。基本型（`int` / `str` / `Path` 等）に 宣言するとエラーとなる。

```yaml
samples:
  case_001:
    lesion_bbox:
      - x_min: 10
        y_min: 20
        x_max: 100
        y_max: 120
        annotator: doctor_a
 # str として宣言済み
        custom_score: 0.95
 # float として宣言済み
        memo: "follow-up"
 # 宣言の無い任意属性
```

### label_map

`label_map` は、ラベル値と意味の対応を記述する。

```yaml
structure:
  label:
    type: int
    description: Sample-level classification label.
    label_map:
      0: Negative
      1: Positive
```

JSON互換性を重視する場合、`label_map` のkeyは文字列として扱うことを推奨する（JSON ではキーは必ず文字列）。

```yaml
structure:
  label:
    type: int
    label_map:
      "0": Negative
      "1": Positive
```

`label_map` はスキーマ上のメタ情報であり、サンプル値の解釈には用いられない （値は `type` に従う）。

---

## type: dict と fields

`type: dict` の属性は、構造化した複数の値をまとめて1つの属性として持たせるために用いる。 `structure.<key>.fields` で、dict 内の各キーのスキーマ（`type` / `multiple` / `description` 等）を宣言できる。

`fields` を宣言しない `type: dict` は、キーの型を宣言しない通常の dict として扱う。

```yaml
meta:
  structure:
    measurement:
      type: dict
      description: Lesion measurement.
      fields:
        diameter_mm:
          type: float
          description: Long-axis diameter [mm].
        bbox:
          type: Rect
          description: Bounding box on the key slice.
        tags:
          type: str
          multiple: true
          description: Free-form tags.
samples:
  case_001:
    measurement:
      diameter_mm: 12.5
      bbox:
        x_min: 10
        y_min: 20
        x_max: 40
        y_max: 50
        uid: bbox_001
      tags:
        - solid
        - spiculated
```

記述規則:

- `fields` で宣言した各キーは、宣言された型で**dict 外のトップレベル属性と同じ規則**で解釈される （`multiple`、座標・領域・マスク型、`Path`、`SpatialResolution` 等がすべて適用される）。`type: dict` の フィールドはさらに `fields` を持てる（再帰的な入れ子）。
- `fields` で宣言したキーは**必須**であり、サンプル側の dict に存在しないとエラーとなる。キーが存在し値が `null` の場合は、単値なら空（未設定）、`multiple: true` なら空リスト `[]` になる。
- `fields` に宣言の無いキーは**型宣言の対象外**であり、任意の値として記述できる。
- `fields` を宣言できるのは `type: dict` の属性のみ。他の型に宣言するとエラーとなる。

> **予約名の禁止**: 属性キー／`fields` のキーには一部の予約名（`to_dict` / `keys` / `values` / `items` / `get` など）を使用できず、指定するとエラーになる。予約名以外の任意の名前を使うこと。

---

## 既定のファイル属性

ファイルまたはディレクトリは、以下の命名規則で表現する。`type` は `Path` とする。

`<prefix>_file: <path> <prefix>_dir: <path> `

```yaml
structure:
  image_file:
    type: Path
    description: Image file path.
  dicom_dir:
    type: Path
    description: DICOM directory path.
```

標準prefix:

| prefix | 説明 | 典型的な形式 |
| --- | --- | --- |
| `dicom` | DICOM | directory |
| `image` | 画像。原則PNG | `.png` |
| `volume` | 3次元画像 | `.npy` |
| `video` | 動画 | `.mp4` など |

```yaml
samples:
  case_001:
    image_file: images/case_001.png
  case_002:
    dicom_dir: dicom/case_002/
  case_003:
    volume_file: volumes/case_003.npy
  case_004:
    video_file: videos/case_004.mp4
```

---

## samples

`samples` には、サンプルIDごとの実データを記述する。

`samples:   <sample_id>:     <sample_key>: <sample_value> `

### 必須属性と値の有無

`structure` に定義された属性は、各サンプルにキーとして**必ず存在**しなければならない。 値が存在しない（該当なし）場合は、その意図を明示するために次のように記述する。

- 単一値（`multiple: false`）の属性で値が無い場合は `null` を記述する。
- リスト（`multiple: true`）の属性で要素が無い場合は空リスト `[]` を記述する。

```yaml
samples:
  case_001:
 # 陽性: 病変あり
    label: 1
    lesion_mask:
      pixel_array: masks/case_001.npy
      z_0: 5
      y_0: 20
      x_0: 10
    lesion_bbox:
      - x_min: 10
        y_min: 20
        x_max: 100
        y_max: 120
  case_002:
 # 陰性: 病変なし（属性は省略せず null / [] で明示）
    label: 0
    lesion_mask: null
    lesion_bbox: [ ]
```

`null` は「属性は定義されているが値が空である」ことを表す。座標・領域・マスク型の属性は、値が `null` でない場合に限り対象型の引数を満たす必要がある。

### サンプルID

サンプルIDは、データセット内でユニークであること。原則として、画像や動画ファイル名と一致させることを 推奨する。

| データ種別 | 推奨サンプルID |
| --- | --- |
| 画像 | 画像ファイル名のstem |
| 動画 | 動画ファイル名のstem |
| CT画像 | DICOMフォルダ名またはvolumeファイル名のstem |
| ペアデータ | `{画像ID}-{画像ID}` |

```yaml
samples:
  image_001:
    image_file: images/image_001.png
  ct_case_001:
    dicom_dir: dicom/ct_case_001/
  image_001-image_002:
    source_image_file: images/image_001.png
    target_image_file: images/image_002.png
```

### サンプル属性の記法

`samples` の各属性は、値の持ち方に応じて次のいずれかの形式で記述する。

- **省略表記**: 属性が単一の値だけで表現できる場合に使う（例: ラベル、ファイルパス）。
- **詳細表記**: 引数・座標・`uid`・`score` など複数の要素を持つ場合に、dict として記述する。

いずれの形式でも、属性は `structure` で定義済みのキーでなければならない。

#### 省略表記

属性が単一の値だけで表現できる場合は、`<sample_key>: <sample_value>` の形式で記述する。

```yaml
samples:
  case_001:
    label: 1
    image_file: images/case_001.png
```

#### 詳細表記

属性が複数の引数、座標、UID、score、任意属性などを持つ場合は、dict として記述する。詳細表記が使えるのは **座標・領域・マスク型と** `type: dict` の属性である。dict のキーには次を記述できる。

- 型が受け付ける引数名（座標を表すkey など）
- `uid` / `score` / `spatial_resolution`
- その他任意の補助属性
- `type: dict` の場合は `fields` で宣言した各キー（および任意のキー）

```yaml
samples:
  case_001:
    lesion_bbox:
      x_min: 10
      y_min: 20
      x_max: 100
      y_max: 120
      uid: bbox_001
      score: 0.95
      annotator: doctor_a
 # 任意の補助属性
      annotation_phase: initial_review
```

> **基本型はスカラーのみ**: `int` / `float` / `bool` / `str` の属性は省略表記（スカラー）でのみ記述する。 これらに `uid` 等を付けるための詳細表記（dict）は**サポート外**である（実値を表す key 名が属性ごとに 任意でスキーマから一意に特定できないため）。複数の値や補助情報を構造化したい場合は `type: dict` と `fields` を用いる。

---

## 座標表現

座標軸には以下を使用する。

| axis | 説明 |
| --- | --- |
| `x` | 横方向 |
| `y` | 縦方向 |
| `z` | スライス方向 / 奥行き方向 |
| `t` | 時間方向 / フレーム方向 |

**点**は、軸名をそのままkeyとして表現する。

```yaml
point_2d: { x: 100, y: 120 } point_3d: { x: 100, y: 120, z: 30 } point_in_video: { x: 100, y: 120, t: 42 }
```

**領域**や範囲は、`<axis>_min` / `<axis>_max` を使用する。

```yaml
bbox_2d: { x_min: 10, y_min: 20, x_max: 100, y_max: 120 } bbox_3d: { x_min: 10, y_min: 20, z_min: 5, x_max: 100, y_max: 120, z_max: 30 }
```

**時間範囲**を持つ場合は、`t_min` / `t_max` を使用する。両方そろえて指定する（片方のみはエラー）。 時間軸上の単一時刻（`t_min == t_max`）は `t` を 1 つ指定するだけでよい（`t` と `t_min` / `t_max` の 同時指定は不可）。

```yaml
temporal_region: { t_min: 10, t_max: 50 } single_time_region: { t: 42 }
 # t_min == t_max == 42 と等価
```

---

## SpatialResolution

`SpatialResolution` は、空間解像度を表す専用型である。YAML / JSON 上では、float値を持つ dict として記述する。

`spacing:   x: 0.7   y: 0.7   z: 1.0 `

後方互換として、座標順 `(x, y[, z])` の list 表現も許容される（`z` の有無〔長さ 2 / 3〕で 2D / 3D を判定）。 dict 形式の記述を推奨する。

`spacing: [0.7, 0.7, 1.0]   # 座標順 (x, y, z) `

基本形式:

```yaml
meta:
  structure:
    spacing:
      type: SpatialResolution
      description: Spatial resolution of the sample.
samples:
  case_001:
    spacing: { x: 0.7, y: 0.7, z: 1.0 }
```

- **2Dデータ**では通常 `x`, `y` を使用する。
- **3D volume**では通常 `x`, `y`, `z` を使用する。

### 時間軸 t は扱わない

`SpatialResolution` は空間解像度を表す専用型であり、\*\*時間軸 `t` は対象外（仕様外）\*\*である。 動画や時系列データであっても `spacing` に `t` を含めてはならない（`x` / `y`〔/ `z`〕のみ）。 `x` / `y`〔/ `z`〕以外のキー（`t` を含む）を与えるとエラーになる。時刻・時間範囲は `spacing` ではなく、 各座標・領域の `t_min` / `t_max`（単一時刻は `t`）で表す（「座標表現」を参照）。

### 複数データが同一サンプル内に存在する場合

同一サンプル内に spacing の異なる複数のデータがある場合、対象データの prefix を付ける。

```yaml
meta:
  structure:
    image_spacing:
      type: SpatialResolution
      description: Spatial resolution of image_file.
    volume_spacing:
      type: SpatialResolution
      description: Spatial resolution of volume_file.
samples:
  case_001:
    image_file: images/case_001.png
    image_spacing: { x: 0.5, y: 0.5 }
    volume_file: volumes/case_001.npy
    volume_spacing: { x: 0.7, y: 0.7, z: 1.0 }
```

### 座標・領域・マスク属性への適用

詳細表記に `spatial_resolution` を直接書かない場合、各座標・領域・マスク属性の `spatial_resolution` には サンプル内の `SpatialResolution` 属性の値が適用される。どの属性を参照するかは `structure.<key>.spatial_resolution_key` で制御する。

| `spatial_resolution_key` | 挙動 |
| --- | --- |
| 指定（属性名） | その名前の `SpatialResolution` 属性の値が適用される |
| `null`（明示） | 適用しない（`spatial_resolution` は空） |
| 省略（未指定） | `structure` に宣言された単一値の `SpatialResolution` 属性を参照する（後述） |

省略時の参照解決の規則。母集団は `structure` **に宣言された単一値（**`multiple: false`**）の** `SpatialResolution` **属性**であり、サンプルごとの値の有無では数えない。

- 宣言が無い → 適用しない
- 1 つだけ宣言されている → その属性のサンプル値が適用される（値が `null` の場合は適用しない）
- 複数宣言されている → 特定できないためエラー（`spatial_resolution_key` で明示するか、 `spatial_resolution_key: null` で参照を無効にする）。ただしこのエラーは参照解決が実際に必要なときだけ 発生する（値が `null`、`multiple: true` の空リスト、詳細表記に `spatial_resolution` を直接指定済みの 要素では発生しない）。

`SpatialResolution` 属性が複数あるとき、参照先を明示する例:

```yaml
meta:
  structure:
    image_spacing:
      type: SpatialResolution
    volume_spacing:
      type: SpatialResolution
    bbox:
      type: Rect
      spatial_resolution_key: volume_spacing
 # volume_spacing の値を bbox に与える
samples:
  case_001:
    image_spacing: { x: 0.5, y: 0.5 }
    volume_spacing: { x: 0.7, y: 0.7 }
    bbox: { x_min: 0, y_min: 0, x_max: 10, y_max: 10 }
```

なお、詳細表記の dict 内に直接 `spatial_resolution` を書いた場合はそちらが最優先される （上記の参照解決は inline 指定が無いときだけ働く）。

---

## Location 属性

Location 属性（点・領域）は、型ごとの引数で表現する。代表的な型と記法例を示す。

`Point2D`:

```yaml
samples:
  case_001:
    landmark: { x: 100, y: 120, uid: landmark_001 }
```

`Point3D`:

```yaml
samples:
  case_001:
    center_point: { x: 100, y: 120, z: 30, uid: point_001 }
```

`Rect`:

```yaml
samples:
  case_001:
    bbox: { x_min: 10, y_min: 20, x_max: 100, y_max: 120, uid: bbox_001, score: 0.98 }
```

`Circle`:

```yaml
samples:
  case_001:
    circle: { x: 100, y: 120, r: 15, uid: circle_001 }
```

`Ellipse`:

```yaml
samples:
  case_001:
    ellipse: { x: 100, y: 120, r_x: 20, r_y: 10, uid: ellipse_001 }
```

`Cuboid`:

```yaml
samples:
  case_001:
    cuboid: { x_min: 10, y_min: 20, z_min: 5, x_max: 100, y_max: 120, z_max: 30, uid: cuboid_001 }
```

`Sphere`:

```yaml
samples:
  case_001:
    sphere: { x: 100, y: 120, z: 30, r: 10, uid: sphere_001 }
```

`Ellipsoid`:

```yaml
samples:
  case_001:
    ellipsoid: { x: 100, y: 120, z: 30, r_x: 20, r_y: 10, r_z: 8, uid: ellipsoid_001 }
```

---

## Mask 属性

Mask 属性は、`Mask2D` または `Mask3D` で表現する。

| type | 主な引数 | 説明 |
| --- | --- | --- |
| `Mask2D` | `pixel_array`, `x_0`, `y_0` | 2Dマスク |
| `Mask3D` | `pixel_array`, `z_0`, `y_0`, `x_0` | 3Dマスク |

`pixel_array` は、YAML / JSON 上ではファイルパスとして記述する。`x_0` / `y_0`〔/ `z_0`〕はマスク配列の 原点（オフセット）を表す。

> **座標の正規化に関する注意**: マスクは整数格子上のラスタデータとして表現する。値は対象／対象外の 二値であり、原点 `x_0` / `y_0`〔/ `z_0`〕も整数格子上の位置として扱う。したがって 1px 未満の サブピクセルなオフセットは表現できず、原点に非整数値を書いても保持されない。

### 2D Mask

2D Mask では、標準仕様として PNG または npy を許容する。

```yaml
meta:
  structure:
    mask:
      type: Mask2D
      description: 2D binary mask.
samples:
  case_001:
    image_file: images/case_001.png
    mask:
      pixel_array: masks/case_001.png
      x_0: 10
      y_0: 20
      uid: mask_001
```

npy を使う場合:

```yaml
samples:
  case_001:
    mask:
      pixel_array: masks/case_001.npy
      x_0: 10
      y_0: 20
```

### 3D Mask

3D Mask では、3D に対応した PNG は存在しないため、原則として npy を使用する。

```yaml
meta:
  structure:
    lesion_mask:
      type: Mask3D
      description: 3D lesion mask.
samples:
  ct_case_001:
    volume_file: volumes/ct_case_001.npy
    volume_spacing: { x: 0.7, y: 0.7, z: 1.0 }
    lesion_mask:
      pixel_array: masks/ct_case_001.npy
      z_0: 5
      y_0: 20
      x_0: 10
      uid: lesion_mask_001
```

### 空マスク（Negative sample）

対象領域が存在しない（全画素が対象外の）マスクは、**空マスク**として扱う。Negative sample の Segmentation GT のように「対象なし」を正しく表現するための入力である。

- **全ゼロのマスク画像／npy** は不正ではなく、空マスクを意味する。
- `pixel_array` **を明示的に** `null` とした場合も空マスクになる（矩形境界 `x_min` 等を伴わない場合）。 `x_0` / `y_0`〔/ `z_0`〕は任意で指定できる。

```yaml
meta:
  structure:
    lesion_mask:
      type: Mask2D
      description: 2D lesion mask.
samples:
  case_negative:
    lesion_mask:
      pixel_array: null
 # 対象なし（空マスク）。x_0/y_0 は任意で指定可能
      x_0: 0
      y_0: 0
```

- **書き出し時**は、空マスクは画像／npy ファイルを出力せず、出力 JSON / YAML には `pixel_array: null` を 記録することを推奨する（座標・共通属性は通常どおり記録）。上記の読み方と対応し、往復しても情報が失われない。
- **区別**: サンプル属性そのものを `null`（例: `lesion_mask: null`）とした場合は「マスクが無い」を意味し、 空マスク（領域なしのマスク）とは異なる。また `pixel_array` キー自体を省略した場合は空マスクにはならない （空マスクにしたいときは `pixel_array: null` を明示する）。

---

## Label 属性

サンプル単位のラベルは、通常のサンプル属性と同様に、基本型の省略表記（スカラー）で記述する。

```yaml
meta:
  structure:
    label:
      type: int
      description: Sample-level label.
      label_map:
        0: Negative
        1: Positive
samples:
  case_001:
    label: 1
```

ラベル（基本型）に `uid` 等の補助情報を dict で付与する詳細表記は**サポート外**である （前述「基本型はスカラーのみ」を参照）。ラベルに付随する情報を構造化して持たせたい場合は、 `type: dict` と `fields` でスキーマを宣言する。

---

## Prediction 形式

Prediction 形式は、認識アルゴリズムが出力した結果（Detection の検出領域や Classification のスコア等）を、 対応する Dataset を基準に表現する形式である。データセット本体と同じく `meta`（`structure` を内包）/ `samples` のトップレベルキーを持つが、次の点がデータセット形式と異なる。

- `meta.structure` **で独自属性を宣言できる**: `meta.structure` を宣言すれば、対応する Dataset の `meta.structure` に**無い独自属性**（`confidence` 等）を含めてよい（サブセットでなくてよい）。予測と Dataset の**両方に存在する共通キー**は `type` / `multiple` が一致すること。`meta.structure` を **省略したフラット予測**は、従来どおり Dataset の `meta.structure` の**サブセット**に限る（Dataset に 無いキーを含めるには `meta.structure` を宣言する）。
- `SpatialResolution` **を持たない**: 空間解像度は認識結果には含めない。座標・領域・マスク属性の `spatial_resolution` は**対応する Dataset サンプルの** `SpatialResolution` を参照するため、予測ファイル 自体には空間解像度を書かない。
- `meta.content_type: predictions` **で種別を明示できる**: データセットファイルと構造が同じため、 取り違え防止に種別を宣言できる（任意。上記「ファイル種別（content_type）」節）。予測として読み込む場面で `content_type: dataset` を宣言したファイルはエラーになる。

### トップレベルキー

| key | 必須 | 説明 |
| --- | --- | --- |
| `meta` | No | メタ情報（欠落・null を許容）。`structure` を内包する |
| `meta.structure` | No | 予測属性のスキーマ定義（宣言時は Dataset に無い独自属性も可。省略時はサブセット限定） |
| `meta.content_type` | No | ファイル種別。明示するなら `predictions`（`dataset` を指定するとエラー） |
| `samples` | Yes | サンプルIDごとの予測データ本体 |

`meta` は完全に省略可能で、`samples` のみの定義を許容する（Dataset と異なり、必須 meta フィールドは そろえなくてよい）。`meta` も `samples` も無い場合に限り、トップレベルに `sample_id` を直接置く省略形も 使える（`meta` を書く場合は `samples:` を明示すること）。`meta.structure` を省略した場合は、各サンプル 共通のキー集合を Dataset の `meta.structure` から抽出して用いる（サブセット限定）。

Dataset の「`structure` は 1 つ以上の属性が必要」という規則は Prediction には適用せず、**空の** `structure`**（**`{}`**）を許容する**（「予測なし」を表現できるようにするため）。ただし `meta.structure` を 明示する場合はサンプルのキー集合と一致することが求められるため、空の `structure` が成立するのは `samples` が空か、各サンプルが属性を 1 つも持たない場合に限る。

### バリデーション

- 全サンプルが**同一のキー集合**を持つこと（サンプルごとに異なるキーを持つことは許容しない）。
- `meta.structure` を**省略したフラット予測**は Dataset の `meta.structure` の**サブセット**であること。 Dataset に無い属性キーを含む場合はエラー（独自属性を含めるには `meta.structure` を宣言する）。
- `meta.structure` を明示する場合、サンプルのキー集合と一致すること（`samples` が空の場合はこの照合は 行わず、宣言したキーがそのまま維持される）。予測と Dataset の**両方に存在する共通キー**は `type` / `multiple` が Dataset と一致すること（Dataset に無い独自属性は含めてよい）。
- 各 `sample_id` が対応する Dataset の `samples` に存在すること（`SpatialResolution` の参照・正解との 対応が取れないため）。
- Prediction の `meta.structure` に `SpatialResolution` 型属性を含めないこと（空間解像度は Dataset 側を 参照する）。
- `meta.content_type` を明示する場合は `predictions` であること（`dataset` および未知の値はエラー）。 省略・明示 `null` は判定しない。
- サンプルは `meta.structure`（省略時はサンプル共通キー）に無い属性キーを持たないこと。Prediction は 「どの属性を予測したか」を明示する用途のため、宣言外のキーは不正とする。

### 例

Dataset（抜粋。`image_spacing` は `SpatialResolution`、`lesion_bbox` は `Rect` のリスト）:

```yaml
meta:
  content_type: dataset
  structure:
    image_file: { type: Path }
    image_spacing: { type: SpatialResolution }
    label: { type: int }
    lesion_bbox: { type: Rect, multiple: true }
samples:
  case_001:
    image_file: images/case_001.png
    image_spacing: { x: 0.5, y: 0.5 }
    label: 1
    lesion_bbox: [ ... ]
```

Prediction（`image_file` / `image_spacing` は持たず、認識器が出力した `label` / `lesion_bbox` のみ）:

```yaml
meta:
  content_type: predictions
 # 任意（取り違え防止に明示できる）
  structure:
 # 省略可（省略時はサンプル共通キーから抽出）
    label: { type: int }
    lesion_bbox: { type: Rect, multiple: true }
samples:
  case_001:
    label: 1
    lesion_bbox:
      - { x_min: 12, y_min: 22, x_max: 98, y_max: 118, score: 0.91 }
  case_002:
    label: 0
    lesion_bbox: [ ]
```

`lesion_bbox` の各 `Rect` には、対応する Dataset サンプルの `image_spacing`（空間解像度）が適用される。

---

## バリデーションルール

### meta

`meta` はデータセット管理のための情報であり、欠落してもサンプルの解釈は可能である（欠落時は 空として扱う）。以下は標準フォーマットに準拠するための作成側のチェック項目である。

- `dataset_name` / `dataset_id` / `project` / `owner` が存在する
- `dataset_id` が社内でユニークである
- `date` が `YYYY-MM-DD` 形式である（YAML は非クオート、JSON は文字列。いずれも同じ日付を表す）
- `content_type` を記述する場合、`dataset` / `predictions` のいずれかである

> `content_type` は `meta` の中で唯一、読み込む側が想定する種別との整合まで問われるキーである （矛盾、および未知の値はエラー）。省略・明示 `null` は判定しない。詳細は上記 「ファイル種別（content_type）」節を参照。なお `date` は値の妥当性のみが問われる （`null` 以外の不正な日付形式はエラー）。それ以外の管理情報は、値・存在ともデータ本体の解釈には影響しない。

### structure

- `structure` に 1 つ以上の属性が定義されている（空の `{}` は不可。データセットファイルのみの規則で、Prediction ファイルには適用しない）
- 各属性に `type`（または `data_type`）が存在する
- `multiple` が省略された場合は `false` として扱う
- `type` が許可された型である（次元の定まらない汎用型名は指定不可）
- `label_map` がある場合、サンプル側のラベル値と対応している
- `extra_attributes` を宣言できるのは座標・領域・マスク型の属性のみ
- `fields` を宣言できるのは `type: dict` の属性のみ。各フィールドのスキーマも本ルールに従う（再帰）
- `spatial_resolution_key` を宣言できるのは座標・領域・マスク型の属性のみ。参照先は同 `structure` 内に 存在する単一値（`multiple: false`）の `SpatialResolution` 型属性であること

### samples

- サンプルIDがデータセット内でユニークである
- 各サンプル属性が `structure` に定義されている
- `structure` に定義された全属性が、各サンプルにキーとして存在する（必須）
- 値が無い（該当なし）属性は `null` で明示する（`multiple: true` の場合は空リスト `[]`）
- `multiple: true` の属性値がリストである（要素なしは `[]`）
- `multiple: false` の属性値がリストでない（ただし `type: dict` を除く）
- `Path` 型の値が文字列として記述されている
- `SpatialResolution` の各値が float であり、かつ正の有限数である（0・負値・`inf`・`nan` は不可）
- 座標・領域・マスク型の属性は、値が `null` でない場合に対象型の引数を満たしている
- 基本型（`int` / `float` / `bool` / `str`）の属性値はスカラーである（dict による詳細表記はサポート外）
- `type: dict` の属性で `fields` が宣言されている場合、各フィールド値が宣言型を満たしている
- `uid` が指定される場合は文字列であり、必要なアノテーションで一意になる
- `score` が存在する場合、`0.0 <= score <= 1.0` を満たす

### Prediction

- 前述「Prediction 形式 > バリデーション」を参照。

---

## 完全な例

### 2D画像分類 + 検出

```yaml
meta:
  content_type: dataset
  dataset_name: example_2d_image_dataset
  dataset_id: example_2d_image_2026_001
  date: 2026-06-24
  description: Example dataset for 2D image classification and detection.
  project: example_project
  owner: taro_yamada
  structure:
    image_file:
      type: Path
      description: Input image file path. PNG is recommended.
    image_spacing:
      type: SpatialResolution
      description: Pixel spacing of image_file.
    label:
      type: int
      description: Sample-level binary classification label.
      label_map:
        0: Negative
        1: Positive
    lesion_bbox:
      type: Rect
      multiple: true
      description: Lesion bounding boxes.
samples:
  case_001:
    image_file: images/case_001.png
    image_spacing: { x: 0.5, y: 0.5 }
    label: 1
    lesion_bbox:
      - x_min: 10
        y_min: 20
        x_max: 100
        y_max: 120
        uid: bbox_001
      - x_min: 130
        y_min: 50
        x_max: 180
        y_max: 90
        uid: bbox_002
  case_002:
    image_file: images/case_002.png
    image_spacing: { x: 0.5, y: 0.5 }
    label: 0
    lesion_bbox: [ ]
```

### 3D CT + Mask

```yaml
meta:
  content_type: dataset
  dataset_name: chest_ct_segmentation_dataset
  dataset_id: chest_ct_segmentation_2026_001
  date: 2026-06-24
  description: Chest CT dataset for 3D lesion segmentation.
  project: chest_ct_ai
  owner: hanako_suzuki
  structure:
    dicom_dir:
      type: Path
      description: DICOM directory path.
    volume_file:
      type: Path
      description: 3D volume file path. npy is expected.
    volume_spacing:
      type: SpatialResolution
      description: Spatial resolution of volume_file.
    lesion_mask:
      type: Mask3D
      description: 3D lesion mask.
    label:
      type: int
      description: Sample-level label.
      label_map:
        0: Negative
        1: Positive
samples:
  ct_case_001:
    dicom_dir: dicom/ct_case_001/
    volume_file: volumes/ct_case_001.npy
    volume_spacing: { x: 0.7, y: 0.7, z: 1.0 }
    lesion_mask:
      pixel_array: masks/ct_case_001.npy
      z_0: 5
      y_0: 20
      x_0: 10
      uid: lesion_mask_001
    label: 1
  ct_case_002:
    dicom_dir: dicom/ct_case_002/
    volume_file: volumes/ct_case_002.npy
    volume_spacing: { x: 0.8, y: 0.8, z: 1.25 }
    lesion_mask: null
 # 該当なし。属性キー自体は省略しない
    label: 0
```

### 動画データ（単一フレーム時刻 `t`）

```yaml
meta:
  content_type: dataset
  dataset_name: example_video_dataset
  dataset_id: example_video_2026_001
  date: 2026-06-24
  description: Example dataset for video annotation.
  project: video_ai_project
  owner: taro_yamada
  structure:
    video_file:
      type: Path
      description: Video file path.
    video_spacing:
      type: SpatialResolution
      description: Spatial resolution of video_file (x / y only; t は扱わない).
    event_region:
      type: Rect
      multiple: true
      description: 2D bounding boxes with a single-frame time t.
      extra_attributes:
        event_type:
          type: int
          label_map:
            0: normal
            1: abnormal
    label:
      type: int
      description: Sample-level video label.
      label_map:
        0: Negative
        1: Positive
samples:
  video_001:
    video_file: videos/video_001.mp4
    video_spacing: { x: 1.0, y: 1.0 }
    label: 1
    event_region:
      - x_min: 10
        y_min: 20
        x_max: 100
        y_max: 120
        t: 42
 # 単一フレーム時刻（t_min == t_max == 42）
        event_type: 1
        uid: event_001
```

---

## JSON で表現する場合の注意

YAML と JSON は同じ論理構造を持つ。ただし、JSON では以下に注意する。

- コメントは書けない
- `label_map` のkeyは文字列になる
- `Path` は文字列として表現する
- `date` は `YYYY-MM-DD` の文字列として表現する
- `true` / `false` / `null` は JSON の表記に従う

JSON例:

```json
{
  "meta": {
    "dataset_name": "example_dataset",
    "dataset_id": "example_2026_001",
    "date": "2026-06-24",
    "description": "Example dataset.",
    "project": "example_project",
    "owner": "taro_yamada",
    "structure": {
      "image_file": {
        "type": "Path",
        "description": "Input image file path."
      },
      "label": {
        "type": "int",
        "description": "Sample-level label.",
        "label_map": {
          "0": "Negative",
          "1": "Positive"
        }
      }
    }
  },
  "samples": {
    "case_001": {
      "image_file": "images/case_001.png",
      "label": 1
    }
  } }
```
