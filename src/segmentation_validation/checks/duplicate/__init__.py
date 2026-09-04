"""② 重複アノテーションのチェック（D01-D04）。

重複の中心は labels ではなく path_mask / geometry。
同一画像内の異なる geometry_uid を、形（幾何）とラベルの2軸で分類する。
"""
