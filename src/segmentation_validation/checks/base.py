"""チェックの共通型。

Issue は「問題の候補」であって、開発データからの除外を意味しない。
採否は ``selection`` が別モデル（SelectionDecision）で決める。
tiny region として検出されても目視で妥当なら keep になる。

チェックはすべて純関数で、``CheckContext``（レコードと計測値）だけを入力とする。
画素ファイルを開くのは ``core.measure`` に一本化してあるので、
このパッケージは PIL / pydicom / Path.exists を使わない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Protocol

from ..config import Config
from ..core.records import AnnotationRecord, FileGroup

if TYPE_CHECKING:  # 実行時の循環importを避ける
    from ..core.measure import FileMeasurement, MaskMeasurement, PairMeasurement


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Category(StrEnum):
    MACHINE = "machine"
    DUPLICATE = "duplicate"
    SUSPICIOUS = "suspicious"


class CheckStatus(StrEnum):
    """チェックの実行結果の3値。

    情報が無いときに ``pass`` にはしない。「問題なし」と「未検査」を
    区別できなくなると、参照マスクの無い症例が黙って合格扱いになる。
    """

    CHECKED = "checked"
    CANNOT_DETERMINE = "cannot_determine"
    NOT_APPLICABLE = "not_applicable"


class ReviewPriority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


@dataclass(frozen=True)
class Issue:
    """検出された問題の候補、または判定不能の記録。"""

    check_id: str
    category: Category
    severity: Severity
    status: CheckStatus
    review_priority: ReviewPriority

    # --- 所在。CSVでそのまま列になるよう平坦に持つ ---
    dataset_id: str
    source_json: str
    institution: str
    study: str
    series: str
    file: str
    file_uid: str
    geometry_uid: str | None
    annotation_type: str | None
    image_path: str
    mask_path: str | None

    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    # ペア系のチェックで相手を指す。同じ ``duplicate_group_id`` で束ねる。
    related_geometry_uids: tuple[str, ...] = ()
    duplicate_group_id: str | None = None
    # status が CANNOT_DETERMINE のとき、なぜ判定できなかったか。
    cannot_determine_reason: str | None = None

    @property
    def module_id(self) -> str:
        """``M06_PATH_LEADING_SLASH`` → ``M06``。

        設定のポリシー照合と ``--only`` の指定に使う。
        """
        return self.check_id.split("_", 1)[0]


@dataclass(frozen=True)
class CheckContext:
    """チェックへの入力一式。``cli`` が1度だけ組む。"""

    config: Config
    records: tuple[AnnotationRecord, ...]
    groups: tuple[FileGroup, ...]
    # dataset_id -> パス規約（M06 が使う。規約は形式固有なのでアダプタが持つ）
    path_invariants: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # 以下は scan 済みのときだけ埋まる。JSONだけで判定できるチェックは空でも動く。
    masks: Mapping[tuple[str, str], "MaskMeasurement"] = field(default_factory=dict)
    files: Mapping[str, "FileMeasurement"] = field(default_factory=dict)
    pairs: tuple["PairMeasurement", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "by_uid", {record.geometry_uid: record for record in self.records}
        )
        by_file: dict[str, list[AnnotationRecord]] = {}
        for record in self.records:
            by_file.setdefault(record.file_uid, []).append(record)
        object.__setattr__(
            self, "by_file", {key: tuple(value) for key, value in by_file.items()}
        )

    @property
    def has_measurements(self) -> bool:
        return bool(self.masks) or bool(self.files)

    def issue(
        self,
        check_id: str,
        record: AnnotationRecord,
        message: str,
        *,
        category: Category,
        severity: Severity = Severity.WARNING,
        status: CheckStatus = CheckStatus.CHECKED,
        review_priority: ReviewPriority = ReviewPriority.NORMAL,
        related_geometry_uids: tuple[str, ...] = (),
        duplicate_group_id: str | None = None,
        cannot_determine_reason: str | None = None,
        **detail: Any,
    ) -> Issue:
        """レコードから所在フィールドを埋めて Issue を作る。

        8個の所在フィールドを各チェックで手写しすると必ずずれるので、
        生成はここに集約する。
        """
        return Issue(
            check_id=check_id,
            category=category,
            severity=severity,
            status=status,
            review_priority=review_priority,
            dataset_id=record.dataset_id,
            source_json=record.source_json,
            institution=record.institution,
            study=record.study,
            series=record.series,
            file=record.file,
            file_uid=record.file_uid,
            geometry_uid=record.geometry_uid,
            annotation_type=record.annotation_type,
            image_path=record.image_path,
            mask_path=record.path_mask,
            message=message,
            detail=detail,
            related_geometry_uids=related_geometry_uids,
            duplicate_group_id=duplicate_group_id,
            cannot_determine_reason=cannot_determine_reason,
        )


class Check(Protocol):
    """チェックはクラスではなくモジュール。

    ``import`` が登録そのものであり、状態を持たず、
    ``python -c "from ...m06_path_format import run"`` だけで単体実行できる。
    """

    CHECK_ID: str
    CATEGORY: Category
    DEFAULT_SEVERITY: Severity
    TITLE: str
    DESCRIPTION: str

    def run(self, ctx: CheckContext) -> Iterator[Issue]: ...


#: ``match_rank`` の戻り値。大きいほど指定が具体的。
MATCH_EXACT = 2
MATCH_MODULE = 1


def match_rank(check_id: str, selectors: list[str] | tuple[str, ...]) -> int:
    """check_id と指定の一致度を返す。0 は不一致。

    1つのモジュールは複数のIDを出す（``M07_BBOX_GEOMETRY`` が
    ``M07_BBOX_DEGENERATE`` などを出す）。そこで2段階で照合する:

    - ``MATCH_EXACT``  : IDが完全一致。派生IDだけを個別に扱いたいとき
    - ``MATCH_MODULE`` : 先頭の ``M07`` が一致。モジュール単位でまとめて扱うとき

    具体的な指定を優先させたいので、真偽値ではなく一致度を返す。
    """
    target = check_id.upper()
    module = target.split("_", 1)[0]
    best = 0
    for selector in selectors:
        token = selector.strip().upper()
        if not token:
            continue
        if token == target:
            return MATCH_EXACT
        if token == module or token.split("_", 1)[0] == module:
            best = max(best, MATCH_MODULE)
    return best


def matches(check_id: str, selectors: list[str] | tuple[str, ...]) -> bool:
    """``--only M06,S03`` の照合。モジュール単位で拾う。"""
    return match_rank(check_id, selectors) > 0
