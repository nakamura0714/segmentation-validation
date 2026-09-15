"""データセットテンプレート（``dataset_template_pneumothorax.yaml``）を読む。

**このテンプレートが属性の型と意味の正典**で、本リポジトリはそれを解釈せずに
``meta.structure`` を丸ごと写す。structure を Python 側に書き直すと、テンプレートが
更新されたときに黙って古いスキーマを出し続けることになる —— 実際、本機能の設計中だけで
テンプレートは4回更新され、``lung_rect`` の算出規則が「片方のマスクでも使う」から
「両方そろったときだけ」へ変わっている。

丸写しが要るもう1つの理由は ``defaults: {keep_margin: true}``。これは lp-data v2.0.0 の
仕様だが、本リポジトリの ``docs/dataset_format.md`` は
``defaults`` / ``length`` を含まない
古いスナップショットなので、手で書くと確実に落とす。

代わりに ``validate_coverage`` で「サンプル組み立て側が structure のキーを過不足なく
埋めているか」を起動時に検査する。テンプレートに属性が増えたら即座に落ちる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: テンプレートのダミー値（``owner: <owner>`` など）。解決せずに出力すると、
#: 「作成者不明のデータセット」が標準形式に準拠した顔をして流通してしまう。
PLACEHOLDER = re.compile(r"<[^<>]+>")

#: 出力側が常に自分で決めるキー。テンプレートから引き継がない。
#: ``content_type`` は書き出すファイル種別そのもの、``date`` は生成日、
#: ``split`` は本エクスポータが分割を行わないため書かない。
NOT_INHERITED = frozenset({"structure", "content_type", "date", "split"})


class TemplateDriftError(RuntimeError):
    """テンプレートの structure とサンプル組み立て側のキーが食い違っている。

    テンプレート側が正典なので、**直すのは常にこちら**（``sample.FIELD_BUILDERS``）。
    """


@dataclass(frozen=True)
class Template:
    """テンプレートYAMLから取り出した、出力に必要なものだけ。"""

    path: Path
    #: ``meta.structure``。**解釈せずそのまま出力する。**
    structure: dict[str, Any]
    #: ``structure`` 等を除いた ``meta`` の管理情報。出力の meta の土台になる。
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[str]:
        """structure に宣言された属性名。全サンプルがこの集合をキーに持つ。"""
        return frozenset(self.structure)

    def multiple_keys(self) -> frozenset[str]:
        """``multiple: true`` の属性。値が無いとき ``null`` ではなく ``[]`` にする。"""
        return frozenset(
            key
            for key, schema in self.structure.items()
            if isinstance(schema, dict) and schema.get("multiple") is True
        )


def load_template(path: Path | str) -> Template:
    """テンプレートYAMLを読む。

    ``meta.structure`` が空（または無い）なら不正。lp-data は空の structure を
    持つデータセットファイルを読み戻せない（Issue #238 / #239）。
    """
    path = Path(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise TemplateDriftError(f"テンプレートを読めない: {path} ({error})") from error
    except yaml.YAMLError as error:
        raise TemplateDriftError(
            f"テンプレートがYAMLとして壊れている: {path}"
        ) from error

    if not isinstance(payload, dict):
        raise TemplateDriftError(
            f"テンプレートのトップレベルがマッピングでない: {path}"
        )

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        raise TemplateDriftError(f"テンプレートに meta が無い: {path}")

    structure = meta.get("structure")
    if not isinstance(structure, dict) or not structure:
        raise TemplateDriftError(
            f"テンプレートの meta.structure が空: {path}。"
            "属性が1つも無い structure はサンプルを解釈できない"
        )

    inherited = {k: v for k, v in meta.items() if k not in NOT_INHERITED}
    return Template(path=path, structure=structure, meta=inherited)


def validate_coverage(
    template: Template, builder_keys: set[str] | frozenset[str]
) -> None:
    """structure の属性と、サンプルを組み立てるビルダのキーが一致するか検査する。

    ずれを**黙って通さない**のがこの関数の役目。
    足りなければ lp-data が読めないファイルを出し、余っていれば structure に無い
    キーを持つサンプルになる。どちらも出力してから気づくと手戻りが大きい。
    """
    declared = template.keys
    provided = frozenset(builder_keys)
    missing = sorted(declared - provided)
    extra = sorted(provided - declared)
    if not missing and not extra:
        return

    lines = [f"テンプレートの structure とビルダのキーが一致しない: {template.path}"]
    if missing:
        lines.append(
            f"  ビルダに無い属性（テンプレート側が正典なのでビルダを足す）: {missing}"
        )
    if extra:
        lines.append(f"  structure に無いのにビルダが作っている属性: {extra}")
    raise TemplateDriftError("\n".join(lines))


def unresolved_placeholders(meta: dict[str, Any]) -> list[str]:
    """``<owner>`` のような未解決のダミー値が残っているキーを返す。

    トップレベルのスカラー値だけを見る（テンプレートのダミーはすべてそこにある）。
    """
    found = []
    for key, value in meta.items():
        if isinstance(value, str) and PLACEHOLDER.search(value):
            found.append(f"{key}={value}")
    return sorted(found)
