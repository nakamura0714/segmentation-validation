"""ネイティブライブラリの内部スレッド数を絞る。

**なぜ必要か。** 走査はファイル単位で ``ThreadPoolExecutor(--jobs)`` に並列化して
いるが、OpenCV は既定で**マシンの全コアぶんのスレッドプール**を持つ。
このサーバーは 256 コアなので、``--jobs 8`` で走らせると
``connectedComponents`` / ``distanceTransform`` の呼び出しごとに
8 × 数十本のスレッドが立ち、実測で **454スレッド / load average 143** になった。

並列化はすでにファイル単位で効いているので、OpenCV の内部並列は
オーバーヘッドにしかならない。しかも共有サーバーなので、他の人の学習ジョブまで
巻き込んで全体を遅くする。1枚のマスクは 2000x2400 程度で、1スレッドでも
93ms/枚（実測）なので絞って困らない。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def limit_native_threads(jobs: int = 1) -> None:
    """OpenCV の内部スレッド数を1にする。

    ``jobs`` は記録用（どの並列度で走らせたかをログに残すため）。
    ワーカー側では呼ばない —— OpenCV のスレッド数はプロセス全体の設定なので、
    走査を始める前に1度呼べばよい。
    """
    try:
        import cv2
    except ImportError:  # cv2 が無い環境でも走査以外は動く
        return
    before = cv2.getNumThreads()
    if before <= 1:
        return
    cv2.setNumThreads(1)
    logger.debug(
        "OpenCV の内部スレッドを %d -> 1 に制限（並列度は jobs=%d 側で確保する）",
        before,
        jobs,
    )
