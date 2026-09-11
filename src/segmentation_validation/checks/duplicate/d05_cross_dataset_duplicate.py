"""D05 クロスデータセット重複（PTE/PTR/mask136 のような二重エクスポート）。

D01-D04 は「同一ファイル（同一 dataset の同一画像）内」でしか比較しない
（``core/measure.py::_measure_pairs`` が ``FileGroup`` 単位でしかペアを作らない
ため）。そのため、同じ物理DICOMを複数の dataset JSON が別々にエクスポートして
いるケース（同一 annotation の再エクスポート）は D01-D04 では一切検出できない。

判定は **内容一致**（マスクの内容ハッシュ or bbox、および所見ラベル）を主基準に
する。``geometry_uid`` が同じかどうかは判定に使わない —— ただし
「``geometry_uid`` は同じなのに内容が食い違う」ケースは別枠（Tier1）で必ず検出し、
黙ってスキップしない。

- **Tier1（``D05_CROSS_DATASET_MISMATCH``）**: 同じ ``geometry_uid`` を持つのに
  内容（マスク/bbox またはラベル）が食い違う。データの前提（``geometry_uid`` は
  DB主キーで全域一意）が壊れている疑いが強いので、常に目視必須・自動exclude禁止。
- **Tier2 内容一致（``D05_CROSS_DATASET_DUPLICATE``）**: 実質同一annotation。
  まず D01 と同じ「``timestamp`` が新しい方を残す」を試みる。
  ★実データで確認済み: クロスデータセット重複は同一DBレコードの再エクスポートな
  ので ``timestamp`` は**全件で完全に同点**になる（271件中271件）。そのため
  ``timestamp`` で決着しない場合は、元データセットJSONのファイル名に埋め込まれた
  生成日時（``-YYYYMMDD_HHMMSS.json``）を見て、**最初に登録された（＝生成日時が
  最も古い）データセットを代表として残す**。これも決め手にならない場合だけ
  ``D05_CROSS_DATASET_DUPLICATE_TIE`` として自動採否せず全員を目視に回す
  （D01のD01_EXACT_DUPLICATE同様、この場合はグループ全員が通常のcheckも受ける）。

検出そのものは ``checks`` 一般の「純関数として ``CheckContext`` だけを見る」
という形にはできない —— 非代表側を M01-M09/D01-D04 から隠す（skipする）には、
``CheckContext.records`` が確定する前、``cli._load_context`` の中で検出を
済ませておく必要がある。そのため ``detect()`` は ``CheckContext`` 抜きで直接
呼べる形にしてあり、``run(ctx)`` は事前計算済みの ``ctx.cross_dataset_issues``
をそのまま返すだけの薄い皮になっている。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from ...core.measure import ROLE_MASK, MaskMeasurement
from ...core.records import AnnotationRecord
from ...selection.decisions import AutomaticDecision, Decision, Reason
from ..base import Category, CheckContext, Issue, ReviewPriority, Severity, build_issue

CHECK_ID = "D05_CROSS_DATASET_DUPLICATE"
CATEGORY = Category.DUPLICATE
DEFAULT_SEVERITY = Severity.ERROR
TITLE = "クロスデータセット重複"
DESCRIPTION = (
    "同一DICOM画像に対して複数データセットが実質同一のannotationを重複して"
    "エクスポートしているケース。内容一致（マスク・bbox・所見ラベル）で判定し、"
    "timestampが新しい方を代表として残す。geometry_uidの一致だけでは判定しない。"
)

MISMATCH_CHECK_ID = "D05_CROSS_DATASET_MISMATCH"
TIE_CHECK_ID = "D05_CROSS_DATASET_DUPLICATE_TIE"

GROUP_PREFIX = "XDUP"

#: 自動決定できなかった理由（D01 の UNDECIDABLE_* と同じ意味）。
UNDECIDABLE_TIE = "timestamp_tie"
UNDECIDABLE_MISSING = "timestamp_missing"

#: newest 側の情報専用の reason（final_decision には影響しない。D01 と同じ扱い）。
NEWEST_OF_GROUP = "newest_of_cross_dataset_duplicate_group"

#: engineer-set の元JSONファイル名の末尾（例:
#: ``engineer-set-PTE_CX_MT_PI3_pneumothorax-20260904_061537.json``）に
#: 埋め込まれた生成日時。
_SOURCE_TIMESTAMP_RE = re.compile(r"(\d{8}_\d{6})\.json$")


def _source_generated_at(source_json: str) -> datetime | None:
    """元データセットJSONの生成日時。ファイル名から取れなければ None。"""
    match = _SOURCE_TIMESTAMP_RE.search(source_json)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _pick_survivor(
    group_records: Sequence[AnnotationRecord],
) -> tuple[AnnotationRecord | None, str]:
    """代表（生存者）を選ぶ。決められなければ ``(None, 未決着の理由)``。

    優先順位:

    1. ``timestamp``（annotation自体の更新日時）が新しい方
    2. 1で決着しない場合（★実データでは常にこちら。クロスデータセット重複は
       同一DBレコードの再エクスポートなので ``timestamp`` が全件同点になる）、
       元データセットJSONの生成日時が最も古い（＝最初に登録された）方
    3. どちらでも決着しなければ未決着（人の目視へ）
    """
    ann_stamps = [r.parsed_timestamp for r in group_records]
    if any(stamp is None for stamp in ann_stamps):
        return None, UNDECIDABLE_MISSING
    if len({stamp for stamp in ann_stamps}) == len(group_records):
        newest = max(range(len(group_records)), key=lambda i: ann_stamps[i])
        return group_records[newest], ""

    # timestampが全員同点。元データセットJSONの生成日時で決着を試みる。
    source_stamps = [_source_generated_at(r.source_json) for r in group_records]
    if any(stamp is None for stamp in source_stamps):
        return None, UNDECIDABLE_MISSING
    if len({stamp for stamp in source_stamps}) < len(group_records):
        return None, UNDECIDABLE_TIE
    oldest = min(range(len(group_records)), key=lambda i: source_stamps[i])
    return group_records[oldest], ""


@dataclass(frozen=True)
class CrossDatasetResult:
    """検出結果一式。``cli._load_context`` がこれを使って ``ctx.records`` を
    確定させ、``cli._select`` がこれを自動採否のマージに使う。
    """

    issues: tuple[Issue, ...] = ()
    # キーは annotation_uid（f"{dataset_id}::{geometry_uid}"）。
    automatic: dict[str, AutomaticDecision] = field(default_factory=dict)
    # 非代表側として checks から隠す annotation_uid の集合。
    excluded_annotation_uids: frozenset[str] = frozenset()


def _content_key(
    record: AnnotationRecord,
    masks: Mapping[tuple[str, str, str], MaskMeasurement],
) -> tuple[str, object]:
    """内容一致判定に使うキー。マスクがあればその内容ハッシュ、無ければ bbox。"""
    mask = masks.get((record.dataset_id, record.geometry_uid, ROLE_MASK))
    if mask is not None and mask.mask_hash is not None:
        return ("mask_hash", mask.mask_hash)
    return ("bbox", record.json_bbox)


class _UnionFind:
    """``annotation_uid`` を要素とする単純な union-find。

    D01-D04 の ``grouping.build_groups`` は同一ファイル内のペアしか束ねないので
    ``grouping.pair_key``（``file_uid`` 前置）を要素にしているが、D05 は
    データセットを跨いでペアを作るため ``file_uid`` では束ねられない。
    そこで別実装として ``annotation_uid`` を要素にする（``XDUP_%04d`` という
    別 prefix も、D01-D04 の ``DUP_%04d`` と ID 空間が衝突しないようにするため）。
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, uid: str) -> str:
        self._parent.setdefault(uid, uid)
        while self._parent[uid] != uid:
            self._parent[uid] = self._parent[self._parent[uid]]
            uid = self._parent[uid]
        return uid

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_a] = root_b


def detect(
    records: Sequence[AnnotationRecord],
    masks: Mapping[tuple[str, str, str], MaskMeasurement],
) -> CrossDatasetResult:
    """クロスデータセット重複を検出する。

    ``records`` は ``cli._load_context`` が in-scope 判定した直後の全レコード
    （まだ ``cross_dataset_excluded`` で絞る前）。
    """
    by_image: dict[Path, list[AnnotationRecord]] = defaultdict(list)
    for record in records:
        by_image[record.resolved_image_path].append(record)

    issues: list[Issue] = []
    match_pairs: list[tuple[AnnotationRecord, AnnotationRecord]] = []
    uf = _UnionFind()
    by_annotation_uid: dict[str, AnnotationRecord] = {}

    for image_records in by_image.values():
        if len({r.dataset_id for r in image_records}) < 2:
            continue
        for i in range(len(image_records)):
            for j in range(i + 1, len(image_records)):
                a, b = image_records[i], image_records[j]
                if a.dataset_id == b.dataset_id:
                    continue
                same_geometry_uid = a.geometry_uid == b.geometry_uid
                same_label = a.label_keys == b.label_keys
                content_match = same_label and _content_key(a, masks) == _content_key(
                    b, masks
                )

                if same_geometry_uid and not content_match:
                    issues.append(_mismatch_issue(a, b))
                    issues.append(_mismatch_issue(b, a))
                    continue
                if not content_match:
                    continue

                match_pairs.append((a, b))
                by_annotation_uid[a.annotation_uid] = a
                by_annotation_uid[b.annotation_uid] = b
                uf.union(a.annotation_uid, b.annotation_uid)

    if not match_pairs:
        return CrossDatasetResult(issues=tuple(issues))

    # グループ番号は出現順で振る（実行ごとに同じIDになるように）。
    group_numbers: dict[str, str] = {}
    group_of: dict[str, str] = {}
    for a, b in match_pairs:
        for uid in (a.annotation_uid, b.annotation_uid):
            root = uf.find(uid)
            if root not in group_numbers:
                group_numbers[root] = f"{GROUP_PREFIX}_{len(group_numbers) + 1:04d}"
            group_of[uid] = group_numbers[root]

    members: dict[str, list[str]] = defaultdict(list)
    for uid, group_id in group_of.items():
        members[group_id].append(uid)

    automatic: dict[str, AutomaticDecision] = {}
    excluded: set[str] = set()

    for group_id, uids in members.items():
        group_records = [by_annotation_uid[uid] for uid in uids]
        kept_record, tie_reason = _pick_survivor(group_records)

        if tie_reason:
            for record in group_records:
                issues.append(_tie_issue(record, group_id, tie_reason))
            continue

        assert kept_record is not None  # tie_reason が空なら必ず決まっている
        for record in group_records:
            if record is kept_record:
                continue
            excluded.add(record.annotation_uid)
            automatic[record.annotation_uid] = AutomaticDecision(
                geometry_uid=record.geometry_uid,
                decision=Decision.EXCLUDE,
                reason=Reason.CROSS_DATASET_DUPLICATE.value,
                kept_geometry_uid=kept_record.geometry_uid,
                related_geometry_uid=kept_record.geometry_uid,
                duplicate_group_id=group_id,
                detail={
                    "group_size": len(uids),
                    "kept_dataset_id": kept_record.dataset_id,
                },
            )
            # 1ペア=2 Issue（除外側・代表側それぞれの視点）。
            issues.append(_duplicate_issue(record, kept_record, group_id))
            issues.append(_duplicate_issue(kept_record, record, group_id))
        automatic[kept_record.annotation_uid] = AutomaticDecision(
            geometry_uid=kept_record.geometry_uid,
            decision=Decision.KEEP,
            reason=NEWEST_OF_GROUP,
            kept_geometry_uid=kept_record.geometry_uid,
            duplicate_group_id=group_id,
            detail={"group_size": len(uids)},
        )

    return CrossDatasetResult(
        issues=tuple(issues),
        automatic=automatic,
        excluded_annotation_uids=frozenset(excluded),
    )


def _duplicate_issue(
    record: AnnotationRecord, other: AnnotationRecord, group_id: str
) -> Issue:
    """内容一致ペアの片側分の Issue（``emit_pair`` と同様、1ペア=2 Issue）。"""
    return build_issue(
        CHECK_ID,
        record,
        f"{other.dataset_id} の annotation と内容が一致するクロスデータセット重複"
        "（マスク/bbox・所見ラベルが同一）",
        category=CATEGORY,
        severity=DEFAULT_SEVERITY,
        review_priority=ReviewPriority.HIGH,
        related_geometry_uids=(other.geometry_uid,),
        duplicate_group_id=group_id,
        cross_dataset=True,
        same_geometry_uid=record.geometry_uid == other.geometry_uid,
        other_dataset_id=other.dataset_id,
        other_source_json=other.source_json,
    )


def _tie_issue(record: AnnotationRecord, group_id: str, tie_reason: str) -> Issue:
    return build_issue(
        TIE_CHECK_ID,
        record,
        "他データセットに内容一致するannotationがあるが、timestampが同値・欠損"
        "のため自動で代表を選べない（目視が必要）",
        category=CATEGORY,
        severity=Severity.WARNING,
        review_priority=ReviewPriority.HIGH,
        duplicate_group_id=group_id,
        cross_dataset=True,
        undecidable_reason=tie_reason,
    )


def _mismatch_issue(record: AnnotationRecord, other: AnnotationRecord) -> Issue:
    return build_issue(
        MISMATCH_CHECK_ID,
        record,
        f"{other.dataset_id} に同じgeometry_uidのannotationがあるが、"
        "マスク/bboxまたは所見ラベルの内容が一致しない（主キーの前提が壊れている疑い）",
        category=CATEGORY,
        severity=Severity.ERROR,
        review_priority=ReviewPriority.CRITICAL,
        related_geometry_uids=(other.geometry_uid,),
        cross_dataset=True,
        same_geometry_uid=True,
        other_dataset_id=other.dataset_id,
        other_source_json=other.source_json,
    )


def run(ctx: CheckContext) -> Iterator[Issue]:
    """事前計算済みの ``ctx.cross_dataset_issues`` をそのまま返すだけ。

    検出自体は ``cli._load_context`` が ``detect()`` を直接呼んで済ませている
    （``ctx.records`` が確定する前に非代表側を除く必要があるため）。
    """
    yield from ctx.cross_dataset_issues
