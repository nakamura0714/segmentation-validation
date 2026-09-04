"""ChestMetry PI6 セグメンテーションデータセットのバリデーション。

パッケージの構成:

``config``     設定（対象JSON・参照マスクのルート・全閾値）
``adapters``   元JSON形式の知識。ここ以外は元JSONの階層構造を直接参照しない
``core``       データセット非依存の走査・計測
``datasets``   データセット固有のロジック（PI6の体外領域判定など）
``checks``     計測値を入力とする純関数のチェック群
``selection``  採否判断と開発用データセット生成
``report``     issues / summary の出力
``review``     FiftyOne 目視レビュー。fiftyone を import するのはここだけ
``viz``        matplotlib による描画
"""

__version__ = "0.1.0"
