# 統合development.json と 目視判定のGit正本化

> 実装着手時、この計画を `plan/merged-dataset-and-review-store_2026-09-14.md` として
> リポジトリにコミットする（`plan/` 配下に `<name>_<日付>.md` で残す運用に合わせる）。

## Context

2つの独立した課題を1つの設計としてまとめる。

**(1) 学習パイプライン入力の統合JSON**
`build-dataset` は 12本の `development.json` をデータセット別ディレクトリに書く。
学習パイプラインが1ファイルで読める統合版が無く、由来データセット（train/test の別）を
追跡する手段も無い。統合JSON自体は再生成可能な派生物であり Git 管理の正本にはしない。

**(2) 目視判定の fingerprint 非依存化**
`review_decisions.json` は `output/validation/<fingerprint>/review/` にあり、`output/` は
gitignore 済み。fingerprint は対象JSONの `name:size:mtime_ns:sha256` から決まる
（`core/cache.py:82-95`）ため、**データセットを1本足すだけ・symlink を張り替えるだけで変わる**。
コード上に引き継ぎ機構は一切存在せず、`docs/review_procedure.md:367-375` が手動 `cp` を
案内しているのみ。人間の目視結果は再生成不可能な唯一の資産なので、Git 管理の正本へ移す。

目指す責務分離:

```
Git管理（正本・再生成不可）          自動生成（派生物・再生成可能）
├─ src/                              ├─ output/development/<dataset>/<ver>/development.json × 12
├─ review/review_decisions.json  ──▶ ├─ output/development/<ver>/development_merged.json  ★新規
└─ review_policy/                    ├─ output/validation/<fp>/selection_decisions.csv
   image_decision_overrides.json     ├─ output/validation/<fp>/dashboard.html
                                     └─ output/validation/<fp>/review/review_decisions.json（スナップショット）
```

---

## 1. 現状調査結果

### 1.1 統合の可否（実測済み・確定）

| 階層 | ユニーク数 | 複数dataset間で共有 |
|---|---:|---:|
| institution | 35 | 23 |
| (inst, study) | 42,690 | 4,274 |
| (inst, study, series) | 42,708 | 4,277 |
| **(inst, study, series, file_id)** | **42,574** | **0** |
| image_path | 42,574 | 0 |
| geometry_uid | 17,933 | 0 |

- `build-dataset` のクロスデータセット重複排除（`selection/build_dataset.py:277` の
  `resolve_image_duplicates`、selected 4,266 / skipped 4,298）が効いており、**file/画像/annotation
  単位の衝突はゼロ**。
- 上位階層は共有されるため浅い `dict.update()` は不可。取りこぼしの実例:
  `ofuna_chuo/CXOFC00003778_002/CXOFC00003778_002_001` の file_list が
  `ANN_EIRLPRJ_1298`(`..._000`) と `ETR_..._abnormal_non_pneumothorax`(`..._002`) に分かれている。
- series メタ（spacing / shape / manufacturer / groups）の食い違いは 4,277件すべてで **0件**。
- study メタの食い違いは **1件のみ**: `segmed/CXSGM00040183` の `study_date`
  （`ChestMetry_PI6px_normal` = 2020-01-11 / `ETR_..._abnormal_non_pneumothorax` = 2019-01-05）。
- `version_id`(2.2) / `version_date`(2026-05-19) / `fingerprint`(v_701e8639deb8) は12本一致。
- サイズ: raw 合計 99.35 MiB（gzip 5.00 MiB）。

### 1.2 目視判定の読み書き経路（実読確認）

| 経路 | 実装 | キー |
|---|---|---|
| 書き | `review export` のみ（`cli.py:1199-1229` → `review/export_decisions.py:194`） | annotation `(kind, dataset_id, geometry_uid)` / image `(kind, "", file_uid)` |
| 読み `select` | `cli.py:520` → `_read_review_decisions`（`cli.py:1634-1693`） | `dataset_id::geometry_uid` / `file_uid` |
| 読み `review build` | **直読みしない**。`selection_decisions.json` 経由（`review/manifest.py:117-118`） | 同上 |
| 読み `review import` | `import_decisions.py:38-82` | `(dataset_id, geometry_uid)` / `file_uid` |
| 読み FiftyOne build | manifest の値をそのまま書くだけ。復元は `review import` が担う | — |
| 読み `precision` / `gui` | `report/precision.py:105-110`、`report/gui.py:451-459` | **bare `geometry_uid`（バグ）** |

- `write_decisions` は**追記専用マージ**（`export_decisions.py:210-215`）。既存キーは消えず、
  今回スキャン分が勝つ。削除経路は無い。
- 出力先は `_review_dir(config)` = `config.validation_dir / fingerprint / "review"`
  （`cli.py:922-923`）が唯一の組み立て箇所。
- **fingerprint 跨ぎの引き継ぎ機構はコード上に存在しない**（`src/` 全体で `shutil` の使用が0件）。
- `review build` の未exportゲートは、manifest が無いとき DB を直接見るが、
  `reviewer` 未入力の判定は取りこぼす（`export_decisions.py:481-485`）。

### 1.3 識別キーの安定性 ★最重要

| キー | 構成 | 再エクスポート（日付だけ変化）時 |
|---|---|---|
| `dataset_id` | ファイル名から `-YYYYMMDD_HHMMSS$` を除去（`adapters/engineer_set.py:52-60`） | **不変** |
| `geometry_uid` | 元JSONの値（`engineer_set.py:214`） | **不変** |
| `annotation_uid` = `dataset_id::geometry_uid` | `core/records.py:102-105` | **不変 ✓** |
| `file_uid` = `source_json::inst/study/series/file` | `core/records.py:90-100`, `:182-187` | **全件変化 ✗** |

`file_uid` は `source_json`（= `source_path.name`、**日付スタンプ込みのフルファイル名**、
`engineer_set.py:136`）を含み、`dataset_id_for()` のような日付除去が一切かかっていない。

**結論: annotation 側の判定は再エクスポートを生き延びるが、画像側の判定は全件引き当て不能になる。**
`write_decisions` はマージなので古い行は消えず、「件数はあるのに1件も適用されない」状態になる。
`serve.py:619-638` の件数表示は行数を数えるだけなのでこれを検出できない。

→ **Git 正本では画像キーを差し替える必要がある**（§5）。

---

## 2. 新しいディレクトリ構成

```
<project_root>/
├─ review/                          ★新規・Git管理
│   ├─ review_decisions.json           目視判定の正本（fingerprint非依存、schema_version 2）
│   └─ README.md                       正本の責務と運用
├─ review_policy/                    Git管理（既存。image_decision_overrides.json は未追跡→追跡へ）
│   ├─ image_decision_overrides.json
│   └─ README.md
└─ output/                           .gitignore 済み（変更なし）
    ├─ validation/<fp>/review/
    │   ├─ review_decisions.json        スナップショット（正本のコピー。読み取り専用扱い）
    │   └─ review_manifest.json         変更なし
    └─ development/
        ├─ <dataset_id>/<version_tag>/development.json   × 12（変更なし）
        └─ <version_tag>/
            ├─ development_merged.json   ★新規（統合JSON）
            ├─ selection_summary.md      既存（節を追加）
            └─ image_duplicates.csv      既存
```

`.gitignore` は `output/` のみを除外しているので `review/` は追加設定なしで追跡される。

---

## 3. JSONの責務

| ファイル | 正本か | 責務 | キー |
|---|---|---|---|
| `review/review_decisions.json` | **正本** | 人間が下した keep/exclude/uncertain。再生成不可 | ann: `dataset_id::geometry_uid`<br>img: `dataset_id::inst/study/series/file` |
| `review_policy/image_decision_overrides.json` | **正本** | 未アノテーション画像の承認済み例外規則 | `match` 条件 |
| `output/validation/<fp>/review/review_decisions.json` | 派生 | その fingerprint 時点のスナップショット。読み込み元にしない | 正本と同じ |
| `output/validation/<fp>/selection_decisions.json` | 派生 | 自動判定＋人間判定を合成した採否 | `dataset_id::geometry_uid` |
| `output/development/<ver>/development_merged.json` | 派生 | 学習パイプライン入力 | `dataset_id` を file entry に保持 |

---

## 4. 統合JSON

### 4.1 スキーマ（元JSON互換 + dataset_id）

既存の `dataset → institution → study → series_list → file_list` を維持し、
**file entry にのみ `dataset_id` を追加**する。file 単位が衝突ゼロの唯一の階層であり、
series 階層は 4,277件が複数データセットで共有されるため、上位に置くと由来を表現できない。

```json
{
  "dataset": {
    "segmed": {
      "CXSGM00040183": {
        "patient_id": "...", "study_name": "...", "study_date": "2019-01-05",
        "series_list": {
          "CXSGM00040183_003_000": {
            "spacing": {...}, "shape": {...}, "manufacturer": "...",
            "file_list": {
              "CXSGM00040183_003_000_000": {
                "dataset_id": "ChestMetry_PI6px_normal",   // ★追加（唯一の追加キー）
                "image_path": "medical2/...",
                "annotations": [ ... ]
              }
            },
            "groups": []
          }
        }
      }
    }
  },
  "version_id": "2.2",
  "version_date": "2026-05-19",
  "meta_development": {
    "generated_at": "...", "tool_version": "...", "fingerprint": "v_701e8639deb8",
    "merged": true,
    "datasets": {
      "<dataset_id>": {
        "source_json": "engineer-set-...-20260624_080014.json",
        "source_sha256": "...",
        "kept": 559, "excluded": 0, "pending": 0, "uncertain": 0, "unknown": 0,
        "files_total": 324, "files_emptied": 0, "files_already_empty": 11,
        "images_total": 357, "images_dropped": 33
      }
    },
    "totals": { "datasets": 12, "institutions": 35, "studies": 42690,
                "series": 42708, "files": 42574, "annotations": 17933 },
    "conflicts": [
      { "level": "study", "path": "segmed/CXSGM00040183", "field": "study_date",
        "values": { "ChestMetry_PI6px_normal": "2020-01-11",
                    "ETR_ChestMetry_PI6px_abnormal_non_pneumothorax": "2019-01-05" },
        "adopted": "ChestMetry_PI6px_normal",
        "rule": "dataset_id 昇順の先頭" }
    ]
  }
}
```

`dataset_id` → `source_json` の対応は `meta_development.datasets` に持たせ、file entry には
置かない（42,574件ぶんの冗長を避ける）。これで元の `file_uid` は再構成できる。

### 4.2 生成フロー

```
_build_dataset (cli.py:738)
  ├─ selection_decisions.json / image_decisions.json を読む        （既存）
  ├─ ゲート: pending / uncertain                                   （既存）
  ├─ resolve_image_duplicates() → build_by_image, image_duplicates.csv （既存）
  ├─ for source in config.dataset_sources():                       （既存ループを拡張）
  │     payload, result = build_development_payload(...)      ★分割した新関数
  │     write_development_json(payload, <dataset>/<ver>/development.json)  （既存の出力）
  │     if merged: merge_into(accumulator, payload, dataset_id)  ★新規
  ├─ 元JSON sha256 検証（全件）                                     （既存）
  ├─ if merged: write_merged_json(accumulator, <ver>/development_merged.json) ★新規
  └─ selection_summary.md（統合の節を追加）                          （既存＋追記）
```

### 4.3 再帰マージのアルゴリズム

`merge_into(acc, payload, dataset_id)` は4階層を union する。

```
institution:  無ければ丸ごと移す / あれば study 階層へ降りる
study:        無ければ丸ごと移す / あれば
                - スカラー属性（patient_id, study_name, study_date）を比較し、
                  食い違えば conflicts に記録（値は dataset_id 昇順の先頭を採用）
                - series_list へ降りる
series:       無ければ丸ごと移す / あれば
                - spacing / shape / manufacturer を比較（実データでは全件一致）
                - groups は空リスト同士なので触らない。非空で食い違えば conflict
                - file_list へ降りる
file:         file_id が既にあれば **AssertionError で停止**（衝突ゼロが前提。
              壊れたら黙って上書きせず落とす）。無ければ dataset_id を付けて移す
```

- 移送は**参照の move**（deepcopy しない）。同じ dict を共有するのでメモリ二重化を避ける。
- 移した後は元 payload への参照を落とし、GC に回す。

**メモリ**: raw 99MiB を Python オブジェクト化すると概ね 0.7〜1.5GB。ピークは
「accumulator（最終的に全量）＋ 処理中の1データセット」。実装時に最大の
`ChestMetry_PI6px_normal`（40MB）を含む順序で実測する。超過するなら
`--no-merged` で回避できるようにし、将来的に institution 単位のストリーミング書き出しへ。

### 4.4 CLI

```
build-dataset
  --no-merged            統合JSONを作らない（既定は作る）
  --merged-name NAME     出力ファイル名（既定 development_merged.json）
  --strict-conflicts     属性衝突が1件でもあれば EXIT_ISSUES で停止（CI用。既定は継続）
```

---

## 5. review判定の読込・保存フロー

### 5.1 安定キーの導入

`core/records.py` に追加（`file_uid` は互換のため残す）:

```python
# AnnotationRecord / FileGroup 共通
@property
def stable_file_uid(self) -> str:
    """再エクスポートを跨いで安定する画像の識別子。

    file_uid は source_json（日付スタンプ込みのファイル名）を含むため、
    元JSONを再エクスポートすると全件が別物になる。dataset_id は
    dataset_id_for() が日付を落とすので安定している。
    """
    return f"{self.dataset_id}::{self.institution}/{self.study}/{self.series}/{self.file}"
```

annotation 側は既存の `annotation_uid` をそのまま使う（§1.3 で安定性を確認済み）。

### 5.2 正本のスキーマ（schema_version 2）

```json
{
  "schema_version": 2,
  "meta": { "generated_at": "...", "dataset_name": "pi6-validation",
            "n_annotations": 2557, "n_images": 0 },
  "decisions": [
    { "dataset_id": "ANN_EIRLPRJ_01272", "geometry_uid": "ac09f322-...",
      "decision": "exclude", "reason": "invalid_duplicate",
      "reviewer": "...", "reviewed_at": "...", "comment": "" }
  ],
  "image_decisions": [
    { "dataset_id": "ChestMetry_PI6px_normal",
      "image_key": "asahikawa/CXASW00001466_002/CXASW00001466_002_000/CXASW00001466_002_000_000",
      "decision": "keep", "reason": "frontal_view",
      "reviewer": "...", "reviewed_at": "...", "comment": "",
      "legacy_file_uid": "engineer-set-...-20260624_080014.json::asahikawa/..." }
  ]
}
```

`legacy_file_uid` は移行時の追跡用に残すだけで、突合には使わない。

### 5.3 新モジュール `review/decision_store.py`

fiftyone 非依存（`test_architecture.py:24-28` の制約を満たす）。正本の読み書きを1箇所に集約する。

| 関数 | 役割 |
|---|---|
| `load(path) -> DecisionStore` | schema v1/v2 を吸収して読む。v1 は §6 の規則で昇格 |
| `save(store, path)` | v2 で書く。既存とマージ（`write_decisions` の追記専用方針を踏襲） |
| `merge(base, incoming) -> (merged, conflicts)` | キー衝突の解決（§7） |
| `annotation_key(dataset_id, geometry_uid)` | `f"{dataset_id}::{geometry_uid}"` |
| `image_key(dataset_id, inst, study, series, file)` | `f"{dataset_id}::{inst}/{study}/{series}/{file}"` |
| `image_key_from_file_uid(file_uid)` | v1 移行用。`source_json` を `dataset_id_for()` に通す |
| `snapshot(store, out_dir)` | fingerprint 配下へコピーを書く |

### 5.4 各コマンドの変更後の挙動

| コマンド | 変更 |
|---|---|
| `review export` | DB → **正本 `review/review_decisions.json`** へマージ書き込み。そのあと `output/validation/<fp>/review/` にスナップショットを書く（既存の閲覧・CSV 運用を壊さない） |
| `select` | 既定の読み込み元を**正本**に変更。`--review-decisions PATH` は従来どおり上書き可 |
| `review build` | 判定の読み込み経路は変更なし（`selection_decisions.json` 経由）。未exportゲートの比較先を**正本**に変更 |
| `review import` | 既定の読み込み元を**正本**に変更（`--path` で上書き可） |
| FiftyOne build | 変更なし。復元は `review import` が担う |
| `review precision` | 正本から読み、**`annotation_uid` で突合**（bare `geometry_uid` のバグを同時に修正） |
| `gui` | fingerprint を無視した glob（`report/gui.py:453-457`）をやめ、正本を読む |
| `review migrate-decisions` | ★新規。§6 |
| `review status` | 正本の件数と、**現在の構成に当たらない孤児行**（orphan）の件数を表示 |

### 5.5 判定が pending に戻らないことの保証

```
review/review_decisions.json（Git）
        │  annotation: dataset_id::geometry_uid   ← 再エクスポートで不変
        │  image:      dataset_id::inst/study/series/file ← 再エクスポートで不変
        ▼
select（fingerprint に関係なく正本を読む）
        ▼
selection_decisions.json: decision_source=human, review_status=reviewed
```

`config.review_decisions_path` を `project_root / "review" / "review_decisions.json"` として
`config.py:293-301` の `image_decision_overrides_path` と同じ形の property にする
（プロジェクトルート基準・fingerprint 非依存）。

---

## 6. fingerprint変更時の挙動 と 既存データの移行

### 6.1 変更後の挙動

| 事象 | 変更前 | 変更後 |
|---|---|---|
| データセット1本追加 | 判定が全件 pending に戻る | **引き継がれる**（正本は fingerprint 非依存） |
| symlink 張り替え / touch | 同上 | **引き継がれる** |
| 元JSON再エクスポート（日付のみ変化） | annotation は生存、**画像は全件消失** | **両方引き継がれる**（安定キー） |
| 元JSONのファイル名が日付以外で変化 | dataset_id が変わり全件消失 | 同左。`review status` が孤児として警告 |
| FiftyOne DB 削除 | 正本があれば `review import` で復元 | 同左（正本が Git 上にあるので確実） |

### 6.2 移行手順

`review migrate-decisions` を新設する。

```
uv run segmentation-validation review migrate-decisions [--dry-run]
```

1. `output/validation/*/review/review_decisions.json` を全て列挙（現在2世代: `v_701e8639deb8`,
   `v_ca12bdfb70e9`。前者に 2,557件）
2. 各行を v2 へ昇格
   - annotation: `dataset_id` があればそのまま。無ければ**従来どおり無視**して件数を報告
     （`cli.py:1660-1670` の既存方針を維持）
   - image: `file_uid` を `source_json::rest` に分解し、`source_json` を
     `dataset_id_for(Path(source_json))` に通して `dataset_id` を得る → `image_key` を構成。
     `legacy_file_uid` を保存。**この変換は機械的かつ可逆で、情報損失は無い**
3. `reviewed_at` の新しい順にマージ（§7）
4. `review/review_decisions.json` へ書く
5. 件数・衝突・無視した行数をレポート。`--dry-run` は書かずにレポートのみ

移行後の手作業:

```bash
git add review/review_decisions.json review_policy/image_decision_overrides.json
git commit -m "目視判定と承認ルールをGit管理の正本へ移行"
```

`review_policy/image_decision_overrides.json` は現在**未追跡**なので、この機会に追跡する。

### 6.3 後方互換

- 正本が存在しない場合、`select` は従来どおり `output/validation/<fp>/review/` を読む
  （移行前でも動く）。1回だけ「正本が無い。`review migrate-decisions` を実行せよ」と警告。
- `--review-decisions PATH` は v1/v2 どちらのファイルも受け付ける。
- fingerprint 配下のスナップショットは書き続けるので、既存の `review_decisions.csv` を
  見ている手順・ダッシュボード表示は壊れない。

---

## 7. エラー・衝突時の扱い

| 状況 | 扱い |
|---|---|
| 統合時に file_id が衝突 | **AssertionError で停止**（EXIT_FAILURE）。衝突ゼロが前提なので黙って上書きしない |
| 統合時に study/series の属性が食い違う | `meta_development.conflicts[]` に**両方の値を dataset_id 付きで記録**。採用値は `dataset_id` 昇順の先頭（決定的・再現可能）。`selection_summary.md` に節を出す。`--strict-conflicts` で EXIT_ISSUES |
| `segmed/CXSGM00040183` の `study_date` | 上記の一般規則で処理。**どちらが正かはツールが決めない**。両値と採用規則を記録し、`selection_summary.md` で毎回可視化してデータ管理側へのバグ報告材料にする（PTE/PTR の二重エクスポート問題と同じ扱い） |
| 正本マージで同一キー・異なる decision | `reviewed_at` が新しい方を採用し warning |
| 同上で `reviewed_at` が無い/同値かつ decision が異なる | **EXIT_ISSUES で停止**し両方を列挙。人が決める |
| 正本の行が現在の構成のどれにも当たらない（孤児） | 削除しない。`review status` で件数と代表例を表示 |
| 正本が壊れている | EXIT_FAILURE。**空として扱わない**（`_exported_keys` の既存方針と同じく、読めないものを「無い」と解釈しない） |
| 移行で `dataset_id` を導出できない `file_uid` | その行を残したまま `unresolved` として報告。突合には使わない |
| 統合JSON生成中の MemoryError | EXIT_FAILURE。`--no-merged` を案内 |

---

## 8. 変更するファイル

### 新規

| ファイル | 内容 |
|---|---|
| `src/segmentation_validation/review/decision_store.py` | 正本の読み書き・キー導出・マージ（§5.3）。fiftyone 非依存 |
| `review/README.md` | 正本の責務と運用 |
| `tests/test_decision_store.py` | 正本のスキーマ・マージ・キー導出 |
| `tests/test_decision_migration.py` | v1→v2 移行 |
| `tests/test_merged_dataset.py` | 統合JSONの再帰マージ |

### 変更

| ファイル | 変更点 |
|---|---|
| `core/records.py:90-105`, `:182-187` | `stable_file_uid` property を `AnnotationRecord` / `FileGroup` に追加 |
| `selection/build_dataset.py:85-182` | `build_development_json` を `build_development_payload` + `write_development_json` に分割（既存シグネチャは薄いラッパで維持）。`merge_into` / `write_merged_json` を追加。**`RAW_JSON_ALLOWED`（`test_architecture.py:38-45`）に既に入っているのでアーキテクチャテストの変更は不要** |
| `config.py:293-301` 付近 | `review_decisions_path` property を追加 |
| `cli.py:130-152` | `build-dataset` に `--no-merged` / `--merged-name` / `--strict-conflicts` |
| `cli.py:192` 付近 | `review migrate-decisions` サブコマンド |
| `cli.py:738-886` | 統合の組み込み |
| `cli.py:520`, `:1634-1693` | `select` の既定読み込み元を正本へ。image 側キーを `image_key` へ |
| `cli.py:922-923`, `:952`, `:1208-1220`, `:1315` | `review export` / `import` / ゲートの参照先を正本へ。スナップショット書き出しを追加 |
| `review/export_decisions.py:194-260`, `:421-429`, `:501-529` | 正本へ書く。image 行に `dataset_id` と `image_key` を持たせる |
| `review/import_decisions.py:38-82` | image 側キーを `image_key` へ |
| `review/manifest.py:117-118` | `by_file` のキーを `stable_file_uid` へ |
| `selection/image_decisions.py:272` | human 判定の引き当てキーを `stable_file_uid` へ |
| `report/precision.py:74-110` | 正本を読み、`annotation_uid` で突合（bare geometry_uid のバグ修正） |
| `report/gui.py:451-459` | fingerprint 跨ぎの glob をやめ正本を読む |
| `report/summary_md.py:262-` | `selection_summary.md` に「統合JSON」「属性衝突」の節 |
| `docs/review_procedure.md:365-386` | 手動 `cp` の手順を削除し、正本ベースの手順へ |
| `README.md` | 責務分離の図・コマンド一覧・出力先を更新 |

### 変更しない

- `.gitignore`（`output/` のみ除外。`review/` は追加設定なしで追跡される）
- `tests/test_architecture.py`（統合ロジックを `build_dataset.py` に置くため `RAW_JSON_ALLOWED` 変更不要）
- `core/cache.py` の fingerprint 計算（fingerprint の意味は変えない）

---

## 9. テスト

### 9.1 単体テスト（合成データ・既定の `uv run pytest` で走る）

`tests/conftest.py` には元JSON（engineer-set 形式）を作るヘルパが無いので、
`make_source_json(tmp_path, dataset_id, tree)` を追加する。関数名は既存の慣習に従い日本語。

**`tests/test_merged_dataset.py`**
- `test_異なるinstitutionはそのまま結合される`
- `test_同じstudyを持つ2データセットのseriesがunionされる`（浅いマージでは落ちるケース）
- `test_同じseriesを持つ2データセットのfile_listがunionされる`（`ofuna_chuo` の実例を縮小再現）
- `test_file_entryにdataset_idが付く`
- `test_file_id衝突はAssertionErrorで止まる`
- `test_study_dateの食い違いがconflictsに両方記録される`（`segmed` の実例を縮小再現）
- `test_strict_conflictsで停止する`
- `test_統合後のannotation総数が各development_jsonの合計と一致する`
- `test_version_idとversion_dateがトップレベルに1つだけ残る`
- `test_meta_development_datasetsに全データセットのsource_sha256が入る`

**`tests/test_decision_store.py`**
- `test_annotation_keyはdataset_idを含む`
- `test_image_keyはsource_jsonを含まない`
- `test_再エクスポートでsource_jsonが変わってもimage_keyは不変` ★中心の要件
- `test_保存は追記マージで既存キーを消さない`
- `test_壊れた正本は空として扱わずエラーになる`

**`tests/test_decision_migration.py`**
- `test_v1のfile_uidからdataset_idとimage_keyを導出できる`
- `test_dataset_idを持たないannotation行は無視され件数が報告される`
- `test_複数fingerprintの判定がreviewed_at順にマージされる`
- `test_reviewed_atが無く判定が食い違えば停止する`
- `test_dry_runはファイルを書かない`

**`tests/test_gates.py` に追加**
- `test_正本が壊れていればselectが止まる`
- `test_統合時のfile_id衝突でbuild_datasetが止まる`

### 9.2 回帰テスト

**`tests/test_review_roundtrip.py`（`fiftyone` マーカー）に追加**
- `test_DB削除後にGit正本からimportして判定が戻る`
- `test_fingerprintが変わっても判定がpendingに戻らない` ★最重要要件
  - 合成データで source JSON のファイル名の日付だけを変えて fingerprint を変え、
    `select` を回して `decision_source=human` / `review_status=reviewed` が維持されることを確認
  - annotation 側と **image 側の両方**を検証する（image 側が現行の穴なので必須）

**`tests/test_review_layer.py` に追加**
- `test_bare_geometry_uidでprecisionが誤爆しない`（既存の同種テストの並びに追加）

**`tests/test_realdata_known_values.py`（`realdata` マーカー）に追加**
- `test_統合JSONの規模が既知値と一致する`
  - files 42,574 / annotations 17,933 / institutions 35 / studies 42,690 / series 42,708
  - conflicts は1件のみ（`segmed/CXSGM00040183` の `study_date`）
  - 各 dataset の `kept` 合計 = 17,933。`selection_summary.md` の keep 18,303 との差 370 は
    クロスデータセット重複 skip 分であることを明示的に検証する

### 9.3 CLIでの確認手順

```bash
# 0. 現状を退避（正本移行前のスナップショットを残す）
cp -r output/validation/v_701e8639deb8/review/review_decisions.json /tmp/before.json

# 1. 目視判定の移行（まず dry-run）
uv run segmentation-validation review migrate-decisions --dry-run
uv run segmentation-validation review migrate-decisions
#   期待: annotation 2557件前後、image 側は legacy_file_uid から dataset_id を導出できた件数、
#         無視した旧形式行の件数がレポートされる

# 2. 正本の内容を目視確認して Git へ
git diff --stat
git add review/review_decisions.json review_policy/image_decision_overrides.json
git commit -m "目視判定と承認ルールをGit管理の正本へ移行"

# 3. 正本から select して判定が維持されることを確認
uv run segmentation-validation select
#   期待: decision_source=human の件数が移行前と一致（selection_summary.md / dashboard で確認）
python - <<'PY'
import json
d=json.load(open("output/validation/v_701e8639deb8/selection_decisions.json"))
print(d["summary"]["by_source"], d["summary"]["pending"])
PY

# 4. fingerprint を変えても pending に戻らないことを確認（★中心要件）
touch /mnt/project/chest/metry/pi6/dataset/source/engineer-set-*mask136*.json
uv run segmentation-validation show-config | grep -i fingerprint   # fingerprint が変わる
uv run segmentation-validation scan --jobs 8
uv run segmentation-validation check
uv run segmentation-validation select
#   期待: 新 fingerprint でも by_source の human 件数が手順3と同じ。pending が増えない

# 5. 統合JSONの生成
uv run segmentation-validation build-dataset --version-tag 20260914_merged
ls -la output/development/20260914_merged/
python - <<'PY'
import json
p="output/development/20260914_merged/development_merged.json"
d=json.load(open(p))
files=anns=0; dsids=set()
for inst,studies in d["dataset"].items():
    for st in studies.values():
        for se in st.get("series_list",{}).values():
            for f in se.get("file_list",{}).values():
                files+=1; dsids.add(f["dataset_id"]); anns+=len(f.get("annotations") or [])
m=d["meta_development"]
print("files",files,"annotations",anns,"datasets",len(dsids))
print("totals",m["totals"]); print("conflicts",m["conflicts"])
assert files==42574 and anns==17933 and len(dsids)==12
PY
#   期待: files 42574 / annotations 17933 / datasets 12 / conflicts 1件（segmed の study_date）

# 6. FiftyOne DB を捨てても Git 正本から復元できることを確認
uv run segmentation-validation review status          # 件数と孤児行を確認
# （DB を削除）
uv run segmentation-validation review build
uv run segmentation-validation review import
uv run segmentation-validation review status          # 判定件数が戻る

# 7. テスト
uv run pytest                       # 合成データのみ（既定）
uv run pytest -m realdata           # 実データの既知値
uv run pytest -m fiftyone           # ラウンドトリップ
```

---

## 10. 実装順序

1. `core/records.py` に `stable_file_uid` を追加 + 単体テスト（他に影響しない）
2. `review/decision_store.py` を新設 + `test_decision_store.py`（読み書きのみ、まだ誰も使わない）
3. `review migrate-decisions` + `test_decision_migration.py` → **実データで移行して Git へコミット**
4. 読み込み側を正本へ切り替え（`select` / `review import` / ゲート / `precision` / `gui`）+ 回帰テスト
5. 書き込み側を正本へ切り替え（`review export` + スナップショット）
6. `build_development_json` の分割（既存の振る舞いを一切変えないリファクタ）+ 既存テストで確認
7. 統合マージの実装 + `test_merged_dataset.py`
8. `build-dataset` への組み込みと CLI オプション
9. `summary_md.py` / `README.md` / `docs/review_procedure.md` の更新

3 までを先に通せば、その時点で「fingerprint 変更で判定が消える」現在の最大のリスクが止まる。
統合JSON（6以降）は独立しているので並行または後追いで進められる。
