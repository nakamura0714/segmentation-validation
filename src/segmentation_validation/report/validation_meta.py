"""出力ディレクトリの ``meta.json``。**fiftyone を import しない。**

``output/validation/<fingerprint>/meta.json`` は数KBの副産物で、
「このディレクトリの成果物は何者か」を安く読めるようにするためだけにある。

**なぜ要るのか。** 同じ情報は ``issues.json`` の ``meta`` にも入っているが、
そちらは実データで60MBある。``serve`` は状態表示のたびに
「この成果物はこの fingerprint のものか」「計測は揃っているか」を判定するので、
60MB を開くわけにはいかない。

これ1つで3つを賄う。

- ``serve`` の fingerprint 一覧（どの構成の成果物がどこにあるか）
- ``serve`` の再生成可否（別構成の成果物を config で作り直すと混成になる）
- ``select`` の計測ゲート（計測が欠けた ``issues.json`` から採否を作らせない）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

META_FILENAME = "meta.json"

#: この形式を変えたら上げる。読む側は不一致なら「分からない」として扱う。
META_VERSION = 1


@dataclass(frozen=True)
class Measurements:
    """計測キャッシュの網羅性。

    ``scan`` していない／途中で止めた状態でも ``check`` は完走して
    ``issues.json`` を書く。画素を要らないのは M06/M07/S02 だけで、17 のうち
    14 は計測依存なので、計測が無いまま採否を作ると **壊れたマスクが keep で
    通る**。それを後段が判定できるように、``check`` の時点の事実を残す。
    """

    n_files: int
    n_measured: int
    scan_required: bool

    @property
    def complete(self) -> bool:
        """対象ファイル全部に計測があるか。

        ``>=`` で見るのは、キャッシュが追記型で同じ ``file_uid`` の行が
        再開時に重複しうるため（数が上回ることはあっても足りないのは異常）。
        """
        return self.n_measured >= self.n_files

    @property
    def usable(self) -> bool:
        """この計測状態で採否を作ってよいか。"""
        return self.complete or not self.scan_required

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_files": self.n_files,
            "n_measured": self.n_measured,
            "scan_required": self.scan_required,
            "complete": self.complete,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Measurements | None:
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                n_files=int(data["n_files"]),
                n_measured=int(data["n_measured"]),
                scan_required=bool(data.get("scan_required", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def shortfall(self) -> str:
        return f"{self.n_files} 件中 {self.n_measured} 件"


@dataclass(frozen=True)
class ValidationMeta:
    """``meta.json`` の中身。"""

    fingerprint: str
    generated_at: str
    sources: tuple[str, ...]
    scale: dict[str, int]
    checks_executed: tuple[str, ...]
    checks_partial: bool
    measurements: Measurements | None

    @property
    def n_sources(self) -> int:
        return len(self.sources)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta_version": META_VERSION,
            "fingerprint": self.fingerprint,
            "generated_at": self.generated_at,
            "sources": list(self.sources),
            "n_sources": self.n_sources,
            "scale": dict(self.scale),
            "checks_executed": list(self.checks_executed),
            "checks_partial": self.checks_partial,
            "measurements": (
                self.measurements.as_dict() if self.measurements else None
            ),
        }


def write_meta(
    out_dir: Path,
    fingerprint: str,
    sources: list[str] | tuple[str, ...],
    scale: dict[str, int] | None = None,
    checks_executed: list[str] | tuple[str, ...] = (),
    checks_partial: bool | None = None,
    measurements: Measurements | None = None,
) -> Path:
    """``meta.json`` を書く。**渡さなかったキーは既存の値を引き継ぐ**。

    ``check`` は checks_executed / checks_partial / measurements を、``select``
    は scale を埋める。段ごとに別のキーを持つので、後の段が前の段の記録を
    消さないように読み込んでから重ねる（``select`` が checks_partial を
    False で潰すと、部分実行のゲートが次から効かなくなる）。
    """
    path = out_dir / META_FILENAME
    existing = read_meta(out_dir)
    merged = ValidationMeta(
        fingerprint=fingerprint,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        sources=tuple(sources) if sources else (existing.sources if existing else ()),
        scale=dict(scale) if scale else (dict(existing.scale) if existing else {}),
        checks_executed=(
            tuple(checks_executed)
            if checks_executed
            else (existing.checks_executed if existing else ())
        ),
        checks_partial=(
            checks_partial
            if checks_partial is not None
            else (existing.checks_partial if existing else False)
        ),
        measurements=(
            measurements
            if measurements is not None
            else (existing.measurements if existing else None)
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # 同じディレクトリへ書いてから rename する（配信中に読まれても壊れない）。
    temporary = path.with_name(f".{META_FILENAME}.tmp")
    temporary.write_text(
        json.dumps(merged.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


def read_meta(out_dir: Path) -> ValidationMeta | None:
    """``meta.json`` を読む。無い／壊れている／版が違うなら ``None``。

    **無いことは異常ではない。** この形式より前に作られた出力ディレクトリには
    存在しない。呼び出し側は「分からない」として代替の判定に落とすこと
    （黙って「問題なし」と解釈してはいけない）。
    """
    path = out_dir / META_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("meta.json を読めない: %s (%s)", path, error)
        return None
    if not isinstance(data, dict) or data.get("meta_version") != META_VERSION:
        return None
    return ValidationMeta(
        fingerprint=str(data.get("fingerprint") or ""),
        generated_at=str(data.get("generated_at") or ""),
        sources=tuple(data.get("sources") or ()),
        scale=dict(data.get("scale") or {}),
        checks_executed=tuple(data.get("checks_executed") or ()),
        checks_partial=bool(data.get("checks_partial")),
        measurements=Measurements.from_dict(data.get("measurements")),
    )
