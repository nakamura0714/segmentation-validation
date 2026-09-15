"""エクスポート結果の要約（``lpdata_export_summary.md``）。

数字を並べるだけの場所ではなく、**次に人間が何を決めるべきかを出す**のが主目的。
ラベルの初期生成は既存 annotation と明示的な正常情報しか使わないので、
判断が要る箇所が必ず残る。それを summary に集めておく。

- ``normal_evidence`` の allowlist に入っていない「正常らしきラベル」と件数
  → 由来が確認できたら config に1行足す
- データセット名に ``pneumothorax`` を含むのに気胸 annotation が0件の画像
  → 後日の読影レポートCSV更新で ``pneumothorax_case`` を補正する対象
- 所見語彙の一覧 → 学習側の期待と突き合わせる
  （``bulla_bleb`` vs ``bulla`` / ``bleb`` 等）
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .options import ExportOptions, ExportResult

#: 一覧を出す上限。これを超えたら件数だけにする。
MAX_ROWS = 20


def build_summary(
    result: ExportResult, options: ExportOptions, meta: Mapping[str, Any]
) -> str:
    """summary の Markdown を組み立てる。"""
    lines: list[str] = ["# lp-data エクスポート結果", ""]

    lines += [
        "## 出力",
        "",
        f"- データセットJSON: `{result.out_path}`",
        f"- サンプル数: **{result.samples:,}**",
        f"- `dataset_id`: `{meta.get('dataset_id')}` / `date`: {meta.get('date')}",
        "",
        "| 設定 | 値 |",
        "| --- | --- |",
        f"| 入力（統合JSON） | `{options.merged_path}` |",
        f"| テンプレート | `{options.template_path}` |",
        f"| `image_mode` | {options.image_mode} → `{options.image_output_dir}` |",
        f"| `mask_mode` | {options.mask_mode} → `{options.mask_output_dir}` |",
        f"| `path_style` | {options.path_style} |",
        f"| 導出値の計測 | {'した' if options.measure else '**していない（下見）**'} |",
        "",
    ]

    if not options.measure:
        lines += [
            "> ⚠️ `--no-measure` で作ったファイル。`lung_rect` と面積はすべて null で、",
            "> 「実体を置くマスクは非ゼロ領域を持つものに限る」という規約も",
            "> 満たせていない。",
            "> **学習には使わないこと。**",
            "",
        ]

    lines += _label_section(result)
    lines += _null_section(result)
    lines += _vocabulary_section(result)
    lines += _asset_section(result)
    lines += _invariant_section(result)
    lines += _followup_section(result)
    return "\n".join(lines) + "\n"


def _label_section(result: ExportResult) -> list[str]:
    lines = ["## ラベル", "", "| abnormal_finding_status | 件数 |", "| --- | ---: |"]
    for status in ("present", "absent", "unknown"):
        lines.append(f"| {status} | {result.status_counts.get(status, 0):,} |")
    lines += ["", "`abnormal_finding_status` × `pneumothorax_case`:", ""]
    lines += ["| status | case | 件数 |", "| --- | --- | ---: |"]
    for key in sorted(result.status_case_counts):
        status, case = key.split("/", 1)
        lines.append(f"| {status} | {case} | {result.status_case_counts[key]:,} |")
    lines += [
        "",
        "> `unknown` × `true`（気胸ラベルはあるが読影所見は未取得）は、",
        "> 既存 annotation だけで作る初期エクスポートでは **0件が正しい**。",
        "> この組み合わせは後日の読影レポートCSV更新で初めて現れる。",
        "",
    ]
    return lines


def _null_section(result: ExportResult) -> list[str]:
    if not result.null_counts:
        return []
    total = max(result.samples, 1)
    lines = [
        "## 値が無い属性",
        "",
        "| 属性 | null / [] | 割合 |",
        "| --- | ---: | ---: |",
    ]
    for key in sorted(result.null_counts, key=lambda k: -result.null_counts[k]):
        count = result.null_counts[key]
        if count == 0:
            continue
        lines.append(f"| `{key}` | {count:,} | {count / total:.1%} |")
    return lines + [""]


def _vocabulary_section(result: ExportResult) -> list[str]:
    if not result.finding_vocabulary:
        return []
    lines = [
        "## 所見語彙（`finding_labels` に出た値）",
        "",
        "元データの `code_text_eng` をそのまま出している。学習側の期待と綴りが",
        "一致しているか突き合わせること（テンプレートの例は `bulla` / `bleb` と分けて",
        "書いているが、元データは `bulla_bleb` の1語）。",
        "",
        "| 所見 | 画像数 |",
        "| --- | ---: |",
    ]
    for name, count in sorted(
        result.finding_vocabulary.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append(f"| `{name}` | {count:,} |")
    return lines + [""]


def _asset_section(result: ExportResult) -> list[str]:
    lines: list[str] = []
    if result.image_counts:
        lines += ["## 画像変換", "", "| 状態 | 件数 |", "| --- | ---: |"]
        for key in sorted(result.image_counts):
            lines.append(f"| {key} | {result.image_counts[key]:,} |")
        lines.append("")
    if result.mask_counts:
        lines += ["## 気胸マスク", "", "| 状態 | 件数 |", "| --- | ---: |"]
        for key in sorted(result.mask_counts):
            lines.append(f"| {key} | {result.mask_counts[key]:,} |")
        lines.append("")
    if result.artifacts:
        lines += ["## 付随ファイル", ""]
        for name, path in sorted(result.artifacts.items()):
            lines.append(f"- {name}: `{path}`")
        lines.append("")
    return lines


def _invariant_section(result: ExportResult) -> list[str]:
    lines = ["## 不変条件", ""]
    if not result.violations:
        lines += ["違反なし。", ""]
        return lines
    lines += [
        f"**{len(result.violations)} 件の違反**"
        "（med-chest-metry-pi6 のテンプレートが正典）。",
        "",
    ]
    for violation in result.violations[:MAX_ROWS]:
        lines.append(f"- {violation}")
    if len(result.violations) > MAX_ROWS:
        lines.append(f"- …ほか {len(result.violations) - MAX_ROWS} 件")
    return lines + [""]


def _followup_section(result: ExportResult) -> list[str]:
    if not result.followups:
        return []
    lines = [
        "## 判断が要るもの（後続の作業）",
        "",
        "初期エクスポートは既存 annotation と明示的な正常情報しか使わないので、",
        "ここに挙がるものは人間の判断か読影レポートCSVでしか埋まらない。",
        "",
    ]

    unlisted: Counter[str] = Counter(result.followups.get("unlisted_normal_like", {}))
    if unlisted:
        lines += [
            "### `normal_evidence` に入っていない「正常らしき」ラベル",
            "",
            "由来が確認できたら `config.lpdata_export.normal_evidence` に足す",
            "（足した分だけ `unknown` が `absent` に変わる）。",
            "",
            "| ラベル | 画像数 |",
            "| --- | ---: |",
        ]
        for name, count in unlisted.most_common(MAX_ROWS):
            lines.append(f"| `{name}` | {count:,} |")
        lines.append("")

    unannotated = result.followups.get("pneumothorax_named_without_annotation", {})
    if unannotated:
        lines += [
            "### データセット名に `pneumothorax` を含むが気胸 annotation が0件の画像",
            "",
            "`pneumothorax_case: false` になっている。"
            "**データセット名を根拠に true にはしない**（推測になる）。",
            "後日の読影レポートCSV更新で補正する対象。",
            "",
            "| dataset_id | 画像数 |",
            "| --- | ---: |",
        ]
        for name in sorted(unannotated):
            lines.append(f"| `{name}` | {unannotated[name]:,} |")
        lines.append("")

    errors = result.followups.get("measurement_errors", [])
    if errors:
        lines += [f"### 計測時のエラー（{len(errors)} 件）", ""]
        for error in errors[:MAX_ROWS]:
            lines.append(f"- {error}")
        if len(errors) > MAX_ROWS:
            lines.append(f"- …ほか {len(errors) - MAX_ROWS} 件")
        lines.append("")
    return lines


def write_summary(path: Path, text: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path
