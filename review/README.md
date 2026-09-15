# 目視判定の正本

`review_decisions.json` は FiftyOne で人が下した採否の**正本**。git 管理する。

## なぜここに置くか

このプロジェクトで唯一**再生成できない資産**だから。

チェック結果も採否マスタも development.json も、元JSONと設定さえあれば何度でも
作り直せる。人が画像を見て下した判断だけは作り直せない。

以前は `output/validation/<fingerprint>/review/` にしか無く、`output/` は
gitignore されていた。fingerprint は対象JSONの `name:size:mtime_ns:sha256` から
決まるので、**データセットを1本足すだけ・symlink が張り替わるだけ**で変わる。
そのたびに判定が空の新ディレクトリを指し、手で `cp` して引き継ぐ運用になっていた。

`review_policy/image_decision_overrides.json`（承認済みの一括ルール）と同じ理由で、
fingerprint に依存しない場所へ置いて git で追跡する。

## 突合キー

fingerprint にも元JSONのファイル名にも依存しない。

| 対象 | キー |
|---|---|
| annotation | `dataset_id` + `geometry_uid` |
| 画像 | `dataset_id` + `institution/study/series/file_id` |

`dataset_id` は `adapters/engineer_set.py::dataset_id_for` がファイル名末尾の
`-YYYYMMDD_HHMMSS` を落とすので、元JSONを再エクスポートしても変わらない。

**`file_uid` は使わない。** `file_uid` は `source_json::inst/study/series/file` で、
`source_json` は日時スタンプ込みのファイル名そのもの。再エクスポートすると画像側の
判定が全件引き当て不能になる（しかも書き出しは追記マージなので行は残り、
「件数はあるのに1件も適用されない」という気づきにくい壊れ方をする）。

`geometry_uid` 単独も使わない。再エクスポートでデータセットを跨いで再利用される
実例があり（D05）、別データセットの判定を誤って適用してしまう。

## ファイルの形

`schema_version: 2`。

```json
{
  "schema_version": 2,
  "meta": {"generated_at": "...", "dataset_name": "pi6-validation",
           "n_annotations": 2422, "n_images": 33},
  "decisions": [
    {"dataset_id": "ANN_EIRLPRJ_01272", "geometry_uid": "ac09f322-...",
     "decision": "exclude", "reason": "invalid_duplicate",
     "reviewer": "...", "reviewed_at": "...", "comment": ""}
  ],
  "image_decisions": [
    {"dataset_id": "ChestMetry_PI6px_normal",
     "image_key": "asahikawa/CXASW00001466_002/CXASW00001466_002_000/CXASW00001466_002_000_000",
     "decision": "keep", "reason": "frontal_view",
     "reviewer": "...", "reviewed_at": "...", "comment": "",
     "legacy_file_uid": "engineer-set-...-20260624_080014.json::asahikawa/..."}
  ]
}
```

`legacy_file_uid` は移行の追跡用。突合には使わない。

schema_version 1（画像側が `file_uid`）も読める。`source_json` を `dataset_id_for()`
に通すだけの機械的な変換で昇格するので、情報は失われない。

`review_decisions.csv` は同じ内容のフラット版。閲覧用で、読み込み元にはしない。

## 誰が読み書きするか

```
FiftyOne（目視・操作用UI）
        │  review export
        ▼
review/review_decisions.json  ← 正本（git）
        │  select                       │  review import
        ▼                               ▼
selection_decisions.json            FiftyOne DB
image_decisions.json
```

| コマンド | 動作 |
|---|---|
| `review export` | DB → 正本へ**マージ**。既存キーは消さない。fingerprint 配下にスナップショットも書く |
| `select` | 正本から読む（`--review-decisions` で差し替え可） |
| `review import` | 正本 → DB。DB を作り直したあとの復元経路 |
| `review precision` | 正本から読み、`dataset_id::geometry_uid` で突合 |
| `review status` | 件数と、現在の構成に当たらない判定（孤児）を出す |
| `review migrate-decisions` | fingerprint 配下の判定を集めて正本を作る（移行時に一度だけ） |

読み書きの実体は `src/segmentation_validation/review/decision_store.py` に集約
してある。fiftyone には依存しない。

## 運用上の決めごと

- **削除しない。** 書き出しは追記マージ。`review build`（`--all` 無し）は pending の
  無い項目を FiftyOne から外すので、確定済みの判定はスキャン範囲から消える。
  消えた＝無効ではない。
- **読めないファイルを空として扱わない。** 壊れていれば `select` は止まる。空として
  扱うと、人間の判定を丸ごと落とした状態で成果物が作られてしまう。
- **判定の食い違いは `reviewed_at` で解決する。** 日時が無い/同じで判定が違う場合は
  停止して両方を出す。ツールが勝手に選ばない。
- **孤児は消さない。** データセットを一時的に外しただけかもしれない。`review status`
  で報告するだけ。
- **FiftyOne DB は正本ではない。** 消しても `review build` → `review import` で戻る。

## 変更したら commit する

`review export` は「正本が更新された」とログに出す。目視した内容は git の履歴に
残す運用にしておくと、誰がいつ何を判断したかを後から追える。

```bash
git add review/review_decisions.json
git commit -m "目視: <対象> を判定"
```
