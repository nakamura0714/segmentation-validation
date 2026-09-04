"""FiftyOne による目視レビュー。

**``import fiftyone`` はこのパッケージの中だけ**、さらにその中でも
``fiftyone_builder`` / ``export_decisions`` / ``import_decisions`` の3つだけ。
FiftyOne が入っていなくても検証本体（scan / check / select / report /
build-dataset）はすべて動く。

``review_schema`` / ``export_assets`` / ``manifest`` は fiftyone に依存しないので、
レビュー用の材料（画像・マスク・manifest）は fiftyone 無しでも作れる。
"""

from .export_assets import export_assets, select_review_groups
from .manifest import build_manifest, read_manifest, write_manifest
from .review_schema import AUTO_PREFIX, REVIEW_PREFIX, ReviewStatusTag

__all__ = [
    "AUTO_PREFIX",
    "REVIEW_PREFIX",
    "ReviewStatusTag",
    "build_manifest",
    "export_assets",
    "read_manifest",
    "select_review_groups",
    "write_manifest",
]
