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
    from ..selection.decisions import AutomaticDecision


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


def build_issue(
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

    ``CheckContext.issue`` の実体。モジュール関数にしてあるのは、
    ``CheckContext`` がまだ組み上がっていない場面（``cli._load_context`` 内での
    クロスデータセット重複の事前検出など）でも同じ生成ロジックを使うため。
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


@dataclass(frozen=True)
class CheckContext:
    """チェックへの入力一式。``cli`` が1度だけ組む。"""

    config: Config
    # 検証対象の annotation のみ。config.validation.target_labels で絞られる。
    # ここで絞ることで、15個のチェックを触らずに全てがスコープに従う。
    # ペア系も相手が対象外なら ``by_uid`` に無いので自動的に片側だけ報告される。
    records: tuple[AnnotationRecord, ...]
    # 対象外の annotation。チェックは走らせないが採否マスタには1行残す。
    out_of_scope: tuple[AnnotationRecord, ...] = ()
    # クロスデータセット重複の非代表側。out_of_scope と同様にチェックは走らせないが
    # 採否マスタには1行残す（reason=cross_dataset_duplicate で自動 exclude 済み）。
    cross_dataset_excluded: tuple[AnnotationRecord, ...] = ()
    groups: tuple[FileGroup, ...] = ()
    # dataset_id -> パス規約（M06 が使う。規約は形式固有なのでアダプタが持つ）
    path_invariants: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    # 以下は scan 済みのときだけ埋まる。JSONだけで判定できるチェックは空でも動く。
    # キーは (dataset_id, geometry_uid, role)。geometry_uid だけだとデータセットを
    # 跨いだ再エクスポートで衝突するため dataset_id を含める。
    masks: Mapping[tuple[str, str, str], "MaskMeasurement"] = field(
        default_factory=dict
    )
    files: Mapping[str, "FileMeasurement"] = field(default_factory=dict)
    pairs: tuple["PairMeasurement", ...] = ()
    # クロスデータセット重複検出（D05）は _load_context 内で ctx.records が
    # 確定する前に済ませる必要がある（非代表側を checks から隠すため）ので、
    # 検出結果の Issue はここに事前に持たせておき、D05 チェックモジュールは
    # これをそのまま yield するだけの薄い皮になる。
    cross_dataset_issues: tuple[Issue, ...] = ()
    # D05 が計算した自動採否（annotation_uid キー）。select がそのまま
    # merge_automatic に渡せるよう、検出と同時に計算済みのものを保持する。
    cross_dataset_automatic: Mapping[str, "AutomaticDecision"] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "by_uid", {record.geometry_uid: record for record in self.records}
        )
        # ``by_uid`` は bare geometry_uid キーなので、データセットを跨いで同じ
        # geometry_uid が再エクスポートされていると衝突する（後勝ちで上書きされる）。
        # ペア系チェック（D01-D04）は同一 dataset 内でしかペアを作らないため、
        # 相手レコードの引き当ては dataset_id まで含めた annotation_uid で行う。
        object.__setattr__(
            self,
            "by_annotation_uid",
            {record.annotation_uid: record for record in self.records},
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
        生成はここに集約する。実体は ``build_issue``（``CheckContext`` が
        まだ無い場面、例えば ``_load_context`` 内での事前検出でも使えるように
        モジュール関数として切り出してある）。
        """
        return build_issue(
            check_id,
            record,
            message,
            category=category,
            severity=severity,
            status=status,
            review_priority=review_priority,
            related_geometry_uids=related_geometry_uids,
            duplicate_group_id=duplicate_group_id,
            cannot_determine_reason=cannot_determine_reason,
            **detail,
        )

    def file_issue(
        self,
        check_id: str,
        group: FileGroup,
        message: str,
        *,
        category: Category,
        severity: Severity = Severity.WARNING,
        status: CheckStatus = CheckStatus.CHECKED,
        review_priority: ReviewPriority = ReviewPriority.NORMAL,
        **detail: Any,
    ) -> Issue:
        """画像単位の Issue を作る。``geometry_uid`` は None。

        annotation を持たない画像について報告するために必要。
        annotation が無いのだから ``geometry_uid`` は存在しない。
        採否は ``selection.image_decisions`` が画像単位で持つ。
        """
        return Issue(
            check_id=check_id,
            category=category,
            severity=severity,
            status=status,
            review_priority=review_priority,
            dataset_id=group.dataset_id,
            source_json=group.source_json,
            institution=group.institution,
            study=group.study,
            series=group.series,
            file=group.file,
            file_uid=group.file_uid,
            geometry_uid=None,
            annotation_type=None,
            image_path=group.image_path,
            mask_path=None,
            message=message,
            detail=detail,
        )


#: 画像の分類。annotation を持たない画像の正体を区別する。
class ImageClass(StrEnum):
    ANNOTATED = "annotated"  # annotation を持つ
    NEGATIVE_CASE = "negative_case"  # 正常例（No Findings）
    UNANNOTATED_VIEW = "unannotated_view"  # 同じ series の他画像はアノテーション済み
    UNANNOTATED_ORPHAN = "unannotated_orphan"  # series 内に1件もアノテーションが無い


IMAGE_CLASS_JA = {
    ImageClass.ANNOTATED: "annotation あり",
    ImageClass.NEGATIVE_CASE: "正常例（No Findings）",
    ImageClass.UNANNOTATED_VIEW: "未アノテーション（他ビューは済み）",
    ImageClass.UNANNOTATED_ORPHAN: "未アノテーション（series全体）",
}


def classify_image(group: FileGroup, series_annotated: bool) -> ImageClass:
    """画像1枚の分類。

    ``series_annotated`` は同じ series の他の画像にアノテーションがあるか。
    「アノテーション漏れ」と「正常例」と「そもそも対象外」を区別するため。
    """
    if group.records:
        return ImageClass.ANNOTATED
    if group.is_negative_case:
        return ImageClass.NEGATIVE_CASE
    return (
        ImageClass.UNANNOTATED_VIEW
        if series_annotated
        else ImageClass.UNANNOTATED_ORPHAN
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
