"""元JSON形式の知識をここに閉じ込める。

チェック側は ``core.records.AnnotationRecord`` / ``FileGroup`` しか見ない。
別形式のデータセットが増えたらこのパッケージにアダプタを1つ足す。
"""

from .base import Capability, DatasetAdapter
from .engineer_set import EngineerSetAdapter, open_adapters

__all__ = ["Capability", "DatasetAdapter", "EngineerSetAdapter", "open_adapters"]
