"""計測キャッシュ。

1665枚のマスクを1度だけ読むための保存先。形式は JSON Lines の追記専用で、
これにより中断耐性と再開が構造的に成立する（ロック不要）。

再開の規則: ``measure_file`` は **mask行 → pair行 → file行** の順で書く。
途中で落ちると file 行を持たない孤児行が残るので、読み込み時にそれを捨てるだけで
整合が取れる。一時ファイルもロックもいらない。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import Config

logger = logging.getLogger(__name__)

# 計測行の構造を変えたら上げる。不一致なら再スキャンを促す。
SCHEMA_VERSION = 1

FILES_JSONL = "files.jsonl"
MASKS_JSONL = "masks.jsonl"
PAIRS_JSONL = "pairs.jsonl"
MANIFEST_JSON = "manifest.json"

# sha256 を計算する際の読み出し単位。JSONは最大3MB程度なので一括でもよいが、
# 将来もっと大きい入力が来ても効くようにしておく。
_CHUNK = 1 << 20


@dataclass(frozen=True)
class SourceFingerprint:
    """入力JSON1件の同定情報。"""

    name: str
    path: str
    real_path: str
    size: int
    mtime_ns: int
    sha256: str

    def canonical(self) -> str:
        return f"{self.name}:{self.size}:{self.mtime_ns}:{self.sha256}"


def source_fingerprints(config: Config) -> list[SourceFingerprint]:
    """対象JSONの同定情報を集める。

    シンボリックリンクの実体も記録する。``dataset/source/`` の中身は
    共有データセットへのリンクなので、どの実体を見たかを残しておく。
    """
    results: list[SourceFingerprint] = []
    for path in config.dataset_sources():
        if not path.exists():
            continue
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
        results.append(
            SourceFingerprint(
                name=path.name,
                path=str(path),
                real_path=str(path.resolve()),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                sha256=digest.hexdigest(),
            )
        )
    return sorted(results, key=lambda item: item.name)


def fingerprint(config: Config, version_id: str | None = None) -> str:
    """キャッシュと出力先を決めるキー。

    ``version_id`` は3ファイルとも ``2.2`` で同じなので**単独では使えない**。
    キーにしてしまうと別データセットの計測値を取り違える。
    実際の同定は各JSONのサイズ・mtime・sha256 の要約で行う。
    """
    sources = source_fingerprints(config)
    if not sources:
        return "empty"
    canonical = "\n".join(item.canonical() for item in sources)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    prefix = version_id or "v"
    return f"{prefix}_{digest}"


@dataclass
class MeasurementCache:
    """走査結果の保存先。JSONL 3本と manifest を持つ。"""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    @property
    def files_path(self) -> Path:
        return self.root / FILES_JSONL

    @property
    def masks_path(self) -> Path:
        return self.root / MASKS_JSONL

    @property
    def pairs_path(self) -> Path:
        return self.root / PAIRS_JSONL

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_JSON

    # ------------------------------------------------------------- 書き込み
    def prepare(self, meta: dict[str, Any], force: bool = False) -> None:
        """キャッシュを使える状態にする。``force`` で内容を捨てる。"""
        self.root.mkdir(parents=True, exist_ok=True)
        if force:
            for path in (self.files_path, self.masks_path, self.pairs_path):
                path.unlink(missing_ok=True)
        self.write_manifest(meta)

    def write_manifest(self, meta: dict[str, Any]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **meta,
        }
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def append(self, path: Path, rows: list[dict[str, Any]]) -> None:
        """JSONL へ追記する。``fsync`` はしない（中断時は孤児行として捨てられる）。"""
        if not rows:
            return
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=_default))
                handle.write("\n")

    # --------------------------------------------------------------- 読み出し
    def completed_file_uids(self) -> set[str]:
        """走査済みのファイル。``scan`` の再開はこれを見て飛ばす。"""
        return {row["file_uid"] for row in self.iter_rows(self.files_path)}

    def iter_rows(self, path: Path) -> Iterator[dict[str, Any]]:
        """JSONL を1行ずつ読む。壊れた行は警告して飛ばす。

        中断時に書きかけの行が残り得るので、末尾が壊れていても読み進められること。
        """
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("壊れた行を飛ばす: %s:%d", path, number)

    def load(self) -> tuple[list[dict], list[dict], list[dict]]:
        """(files, masks, pairs) を読む。

        file 行を持たない ``file_uid`` の mask / pair 行は**中断の残骸**なので捨てる。
        これが再開の整合性を保証する唯一の規則。
        """
        self.check_schema()
        files = list(self.iter_rows(self.files_path))
        complete = {row["file_uid"] for row in files}

        masks = [
            r for r in self.iter_rows(self.masks_path) if r.get("file_uid") in complete
        ]
        pairs = [
            r for r in self.iter_rows(self.pairs_path) if r.get("file_uid") in complete
        ]

        dropped = sum(1 for _ in self.iter_rows(self.masks_path)) - len(masks)
        dropped += sum(1 for _ in self.iter_rows(self.pairs_path)) - len(pairs)
        if dropped:
            logger.info("中断時の孤児行を %d 件捨てた（再スキャンで埋まる）", dropped)
        return files, masks, pairs

    def check_schema(self) -> None:
        if not self.manifest_path.exists():
            return
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        version = manifest.get("schema_version")
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"計測キャッシュのスキーマ版が違う（{version} != {SCHEMA_VERSION}）。"
                f"`scan --force` で作り直す: {self.root}"
            )


def cache_for(config: Config, version_id: str | None = None) -> MeasurementCache:
    return MeasurementCache(config.cache_dir / fingerprint(config, version_id))


def _default(value: Any) -> Any:
    """JSON にできない値の変換。Path と set が主。"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"JSONにできない値: {type(value).__name__}")
