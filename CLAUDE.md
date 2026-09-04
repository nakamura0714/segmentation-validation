# プロジェクト概要

このプロジェクトでは、ChestMetry PI6 のセグメンテーションデータセットの
バリデーションを行う。

主な目的は、データセットのメタデータ、DICOM画像、
セグメンテーションマスクを調査し、
データの不整合、破損、アノテーション上の問題を検出することである。


# プロジェクトルート

プロジェクトルートは以下。

`/mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation`

生成する出力ファイル、一時ファイル、レポート、解析結果などは、
明示的な指示がない限り、このプロジェクトディレクトリ内に保存すること。


# データセットの場所

データセットのメタデータJSONは以下に配置されている。

`/mnt/project/chest/metry/pi6/dataset/source/`

例:

`/mnt/project/chest/metry/pi6/dataset/source/engineer-set-ETR_ChestMetry_PI6px_with_mask136-20260624_080014.json`

`dataset/source/` 内のファイルは共有データセットへの
シンボリックリンクであり、リンク先には以下のような場所が含まれる。

`/mnt/medicaldb/annotation/datasets/`

JSONには以下のようなデータへのパスが含まれる。

- DICOMファイル
- セグメンテーションマスク画像
- その他の医用画像関連ファイル

# Dataset conventions

このプロジェクトで扱う Dataset / Prediction の YAML・JSON は、
`docs/dataset_format.md` の仕様に従うこと。

データセットの読み込み、変換、バリデーション処理を実装・修正する際は、
必ず `docs/dataset_format.md` を参照すること。

特に以下を自己判断で変更しないこと。

- meta / samples / structure の構造
- type / multiple の解釈
- null と [] の扱い
- Mask2D / Mask3D の表現
- SpatialResolution の扱い
- Dataset と Prediction の違い
- validation rules
## JSON内のデータパスの解決方法

JSON内に記載されているファイルパスは相対パスだが、
フィールドによって基準となるルートディレクトリが異なる。

### DICOM画像

`image_path` は `/mnt` を基準として解決する。

例:

`medical2/chest_cr_screening/.../example.dcm`

↓

`/mnt/medical2/chest_cr_screening/.../example.dcm`

### マスク画像

`path_mask` および `path_original_mask` は
`/mnt/medicaldb` を基準として解決する。

例:

`annotation/chest_cr_screening/.../mask/example.png`

↓

`/mnt/medicaldb/annotation/chest_cr_screening/.../mask/example.png`

Pythonでパスを解決する場合は、フィールドの種類に応じて
明示的にルートを使い分けること。

例:

```python
from pathlib import Path

IMAGE_ROOT = Path("/mnt")
ANNOTATION_ROOT = Path("/mnt/medicaldb")

image_path = IMAGE_ROOT / image_path_from_json
mask_path = ANNOTATION_ROOT / path_mask_from_json

if path_original_mask_from_json is not None:
    original_mask_path = ANNOTATION_ROOT / path_original_mask_from_json

