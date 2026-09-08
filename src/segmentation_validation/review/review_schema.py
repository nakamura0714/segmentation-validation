"""目視レビューの語彙。**fiftyone を import しない**。

FiftyOne のタグとフィールドの命名をここに集約する。
検証本体からもレポートからも参照するので、fiftyone に依存させない。

設計の要点:

**自動検出と人間の判定を絶対に混ぜない。**
機械は ``auto:`` タグしか付けず、人間は ``review:`` タグと ``review_*`` フィールドしか
触らない。これにより「自動ルールの Precision」を後から計算できる::

    auto:outside_body 26件 -> 目視 -> exclude 18 / keep 8  => Precision 0.69

**正式な判定はフィールド、タグは絞り込み用。**
タグは文字列の集合なので取り違えやすい。採否そのものは ``review_status``
フィールドが持ち、``review:`` タグはApp上でのフィルタのために併記する。

**annotation の判定は Label に、画像の判定は Sample に。**
1枚の画像に複数の annotation があるので、annotation の良否を Sample タグに
付けるとどれがダメだったのか分からなくなる。逆に「この画像自体を使うか」
（未アノテーションのビューが側面像かどうか）は画像の属性なので Sample に付ける。
"""

from __future__ import annotations

from enum import StrEnum

#: 機械が付けるタグの接頭辞。人間は絶対に触らない。
AUTO_PREFIX = "auto:"
#: 人間が付けるタグの接頭辞。機械は絶対に触らない（再構築でも消さない）。
REVIEW_PREFIX = "review:"
#: 理由タグ。人間が付ける補助情報。
REASON_PREFIX = "reason:"

#: annotation を載せるフィールド。``path_mask`` 由来で、これがレビュー対象。
FIELD_FINAL = "final"
#: ``path_original_mask`` 由来。final と重ねて目視比較するための表示専用レイヤーで、
#: 採否判定（``review_status`` 等）は持たない（判定は常に final 側に対して行う）。
FIELD_ORIGINAL = "original"
#: 体外判定に使った側方バンド。S05 がなぜ発火したかを画面で見るため。
FIELD_BAND = "thorax_band"

#: 正式な判定を持つフィールド。Label と Sample の両方で同じ名前を使う。
FIELD_REVIEW_STATUS = "review_status"
FIELD_REVIEW_REASON = "review_reason"
FIELD_REVIEW_COMMENT = "review_comment"
FIELD_REVIEWER = "reviewer"
FIELD_REVIEWED_AT = "reviewed_at"

REVIEW_FIELDS = (
    FIELD_REVIEW_STATUS,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEWER,
    FIELD_REVIEWED_AT,
)


class ReviewStatusTag(StrEnum):
    """人間が付ける判定。``selection.Decision`` と1対1で対応する。"""

    PENDING = "pending"
    KEEP = "keep"
    EXCLUDE = "exclude"
    UNCERTAIN = "uncertain"


#: 判定の意味。App上のヘルプとレポートで共有する。
STATUS_JA = {
    ReviewStatusTag.PENDING: "まだ見ていない",
    ReviewStatusTag.KEEP: "見た結果、開発データに使う",
    ReviewStatusTag.EXCLUDE: "見た結果、除外する",
    ReviewStatusTag.UNCERTAIN: "見たが判断できなかった（要相談）",
}

#: 理由の候補。自由記述も許すが、集計できるよう推奨値を用意する。
SUGGESTED_REASONS = {
    "keep": (
        "visually_valid",
        "true_small_lesion",
        "valid_nested_annotation",
        "boundary_tolerance",
        "frontal_view",
    ),
    "exclude": (
        "invalid_annotation",
        "invalid_duplicate",
        "stray_component",
        "outside_body",
        "lateral_view",
        "wrong_label",
    ),
}

#: keep/exclude を問わず候補として出す1本の並び。FiftyOne Appの入力候補は
#: 実データに存在する値しか出せない（宣言的な choices/classes はApp側から
#: 一切参照されない。実機検証済み）ので、``fiftyone_builder.py`` がこれを
#: 使って捨てSample/Detectionを作り、値を実在させる。
ALL_SUGGESTED_REASONS: tuple[str, ...] = tuple(
    dict.fromkeys(SUGGESTED_REASONS["keep"] + SUGGESTED_REASONS["exclude"])
)

#: 候補値を実在させるためだけの捨てSample/Detectionに付く目印タグ。
#: export/import・保存ビュー・件数集計はこのタグを見て必ず除外する。
SCHEMA_SEED_TAG = "system:schema_seed"

#: 検証対象外のannotationで気になるものを見つけたとき、人間が付けるタグ。
#: auto:/review: とは別の名前空間にして、機械判定・人間の採否判定と混ざらないようにする。
FLAG_NEEDS_REPORT = "flag:needs_report"


def auto_tag(check_id: str) -> str:
    """check_id から機械のタグを作る。

    ``M09_UNANNOTATED_VIEW`` → ``auto:m09_unannotated_view``
    """
    return f"{AUTO_PREFIX}{check_id.lower()}"


def review_tag(status: str) -> str:
    return f"{REVIEW_PREFIX}{status}"


def reason_tag(reason: str) -> str:
    return f"{REASON_PREFIX}{reason}"


def is_auto(tag: str) -> bool:
    return tag.startswith(AUTO_PREFIX)


def is_review(tag: str) -> bool:
    return tag.startswith(REVIEW_PREFIX) or tag.startswith(REASON_PREFIX)


def parse_review_tag(tag: str) -> str | None:
    """``review:keep`` → ``keep``。それ以外は None。"""
    if not tag.startswith(REVIEW_PREFIX):
        return None
    value = tag[len(REVIEW_PREFIX) :]
    return value if value in set(ReviewStatusTag) else None
