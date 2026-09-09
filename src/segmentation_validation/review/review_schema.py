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
from typing import Any, Iterable, Sequence

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
#: 理由の**正本**。``list[str]`` で、App ではチェックボックスとして出す。
#: 候補を固定できる（自由入力を許さない）のでスペルミスが構造的に起きない。
FIELD_REVIEW_REASONS = "review_reasons"
#: 自由記述の理由。候補に無いことを書きたいときの逃げ道として残す。
#: 集計に乗らないので、候補にあるものは ``FIELD_REVIEW_REASONS`` を使う。
FIELD_REVIEW_REASON = "review_reason"
FIELD_REVIEW_COMMENT = "review_comment"
FIELD_REVIEWER = "reviewer"
FIELD_REVIEWED_AT = "reviewed_at"

REVIEW_FIELDS = (
    FIELD_REVIEW_STATUS,
    FIELD_REVIEW_REASONS,
    FIELD_REVIEW_REASON,
    FIELD_REVIEW_COMMENT,
    FIELD_REVIEWER,
    FIELD_REVIEWED_AT,
)

#: 理由を連結する区切り。``detected_checks`` / ``unverified_checks`` と同じ形式に
#: しておくと、``selection_decisions.csv`` の読み手が同じ扱いをできる。
REASON_SEPARATOR = "|"


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

#: 理由の候補。``FIELD_REVIEW_REASONS`` のチェックボックスに並ぶ値。
#:
#: 「重複」と「古い」を分けているのは別の事実だから。同じマスクが2枚あること
#: （重複）と、そのうち自分が古い方であること（古い）は独立していて、
#: 両方を付ける場面がある。片方だけでは「なぜこちらを消したのか」が残らない。
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
        "older_duplicate",
        "stray_component",
        "outside_body",
        "lateral_view",
        "wrong_label",
    ),
}

#: 理由の日本語表示。値は英語 snake_case のまま（CSV の集計キーを ASCII に保つ）で、
#: ダッシュボードと summary.md の表示だけ日本語にする。``STATUS_JA`` と同じ方針。
REASON_JA = {
    "visually_valid": "目視で妥当",
    "true_small_lesion": "実際に小さい病変",
    "valid_nested_annotation": "入れ子の所見として正当",
    "boundary_tolerance": "境界の許容範囲",
    "frontal_view": "正面像",
    "invalid_annotation": "不適切",
    "invalid_duplicate": "重複",
    "older_duplicate": "古い（ペアの古い方）",
    "stray_component": "飛び地",
    "outside_body": "体外",
    "lateral_view": "側面像",
    "wrong_label": "ラベル違い",
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
#: auto:/review:/reason: とは別の名前空間にして、機械判定・人間の採否判定・
#: 採否の理由と混ざらないようにする（これは「報告したい」という別の軸）。
FLAG_NEEDS_REPORT = "flag:needs_report"


def _machine_reasons() -> frozenset[str]:
    """機械が付ける理由の全値。

    ``SelectionDecision.reason`` は機械の理由と人間の理由が**同じ1列を共有**して
    いるので、App から読み戻すときに区別する手段が要る。区別しないと、
    manifest が初期値として流し込んだ ``review_required`` が
    「人間が入れた理由」として書き出されてしまう（実際にそうなっていて、
    目視102件の理由が全部 ``review_required`` になっている）。

    enum に入っていない文字列直書きの理由もあるので、そこは明示的に並べる。
    """
    from ..selection.decisions import Reason
    from ..selection.image_decisions import ImageReason

    literals = {
        # selection/automatic.py
        "broken_mask",
        "newest_of_exact_duplicate_group",
        # checks/duplicate/d05_cross_dataset_duplicate.py
        "newest_of_cross_dataset_duplicate_group",
        # build_decisions は人間の理由が空のとき採否そのものを reason に入れる。
        # それは「理由が無い」ことの表現なので、人間の理由として扱わない。
        *(status.value for status in ReviewStatusTag),
    }
    return frozenset(
        {r.value for r in Reason} | {r.value for r in ImageReason} | literals
    )


#: 機械が付ける理由。人間の理由として書き出してはいけない。
MACHINE_REASONS: frozenset[str] = _machine_reasons()


def auto_tag(check_id: str) -> str:
    """check_id から機械のタグを作る。

    ``M09_UNANNOTATED_VIEW`` → ``auto:m09_unannotated_view``
    """
    return f"{AUTO_PREFIX}{check_id.lower()}"


def review_tag(status: str) -> str:
    return f"{REVIEW_PREFIX}{status}"


def reason_tag(reason: str) -> str:
    return f"{REASON_PREFIX}{reason}"


def reason_tags(reasons: Iterable[str]) -> list[str]:
    """理由の一覧を ``reason:`` タグの一覧にする。候補外は落とす。"""
    known = set(ALL_SUGGESTED_REASONS)
    return [reason_tag(r) for r in reasons if r in known]


def parse_reason_tag(tag: str) -> str | None:
    """``reason:invalid_duplicate`` → ``invalid_duplicate``。候補外は None。

    ``parse_review_tag`` が ``ReviewStatusTag`` で絞るのと同じ理由で候補に絞る。
    App では ``tags`` に何でも打てるので、知らない値を理由として採ってしまうと
    集計が静かに壊れる。
    """
    if not tag.startswith(REASON_PREFIX):
        return None
    value = tag[len(REASON_PREFIX) :]
    return value if value in set(ALL_SUGGESTED_REASONS) else None


def split_reasons(value: Any) -> list[str]:
    """``"a|b"`` → ``["a", "b"]``。``list`` はそのまま通す。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(REASON_SEPARATOR)
    else:
        parts = list(value)
    return [str(p).strip() for p in parts if str(p).strip()]


def join_reasons(reasons: Iterable[str]) -> str:
    return REASON_SEPARATOR.join(reasons)


def effective_reasons(
    reasons_field: Any,
    tags: Sequence[str] = (),
    legacy_reason: Any = None,
) -> list[str]:
    """人間が入れた理由だけを、候補の並び順で返す。

    優先順位は ``review_reasons`` フィールド → ``reason:`` タグ →
    ``review_reason``（自由記述）。**フィールド優先ではなくフィールドが空のときに
    タグを見る**という形は ``effective_status`` と同じだが、こちらは
    **機械の理由を必ず捨てる**点が違う。``review_reason`` には manifest が
    初期値として流し込んだ機械の理由（``review_required`` など）が残っている
    可能性があり、それを人間の理由として扱うと理由が記録されないまま
    「記録されている」ように見えてしまう。

    並びを候補順に正規化するのは、入力順やタグの集合順で
    ``invalid_duplicate|older_duplicate`` と ``older_duplicate|invalid_duplicate``
    に分かれると集計が割れるため。
    """
    known = list(ALL_SUGGESTED_REASONS)
    # 候補に絞ることが機械の理由を落とすことでもある（候補と機械の理由は
    # 互いに素。``test_候補と機械の理由は重ならない`` で固定してある）。
    for source in (
        split_reasons(reasons_field),
        [t for t in (parse_reason_tag(t) for t in tags) if t is not None],
        split_reasons(legacy_reason),
    ):
        found = {r for r in source if r in known}
        if found:
            return [r for r in known if r in found]
    return []


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


#: 「判定済み」とみなす値。``pending`` は含まない。
DECIDED: frozenset[str] = frozenset(
    {
        ReviewStatusTag.KEEP.value,
        ReviewStatusTag.EXCLUDE.value,
        ReviewStatusTag.UNCERTAIN.value,
    }
)


def effective_status(field_value: Any, tags: list[str]) -> str:
    """フィールドを正とし、フィールドが未判定ならタグを見る。

    App でタグ付けだけして ``review_status`` フィールドを触っていない場合でも
    判定を拾えるようにするためのフォールバック。``export_decisions`` /
    ``fiftyone_builder.dataset_summary`` の両方が同じ基準で判定を数えるよう、
    ここに一本化する。
    """
    value = str(field_value or ReviewStatusTag.PENDING.value)
    if value in DECIDED:
        return value
    for tag in tags:
        parsed = parse_review_tag(tag)
        if parsed in DECIDED:
            return parsed
    return ReviewStatusTag.PENDING.value
