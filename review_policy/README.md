# image_decision_overrides.json

未アノテーション画像の自動判定（`unannotated_orphan_unused` など、
`src/segmentation_validation/selection/image_decisions.py` 参照）を、
データ管理者の承認を経て条件付きで `exclude → keep` に一括変更するためのファイル。

- このディレクトリの `image_decision_overrides.json` を編集して git commit することで、
  承認内容をレビュー可能な形で残す（PRとして差分レビューできる）。
- ファイルが無い、または `overrides` が空でも `select` は問題なく動く
  （何も上書きされないだけ）。
- **既定では何もこのファイルを使わない。** `uv run segmentation-validation select` を
  実行するたびに読み込まれる。

## 手順

1. `uv run segmentation-validation review build --sample-unannotated 30` で
   未アノテーション画像をデータセット別にサンプルし、FiftyOneの保存ビュー
   `11-spot-check-unannotated` で目視確認する
2. `dataset_id` / `image_class` / `auto_decision_reason` / `series_image_index` を
   見て、ある組み合わせが「実は正しくkeepすべきだった」と判断したら、
   このファイルに規則を追記する
3. `uv run segmentation-validation select` を再実行すると、対象画像の
   `final_decision` が `keep` に変わる（`image_decisions.json`/`csv`）

## フォーマット

```jsonc
{
  "overrides": [
    {
      "id": "2026-09-12-normal-orphan",     // 監査用の一意なID。生成日+内容が分かる名前を推奨
      "match": {
        // 各キーは省略可（省略＝ワイルドカード）。指定時は値のリストに対する
        // 完全一致（複数キーはAND条件）。
        "dataset_id": ["ChestMetry_PI6px_normal"],
        "auto_decision_reason": ["unannotated_orphan_unused"],
        "image_class": ["unannotated_orphan"],
        "series_image_index": null
      },
      "action": "keep",
      "approved_by": "yamada@lpixel.net",
      "approved_at": "2026-09-12T03:00:00+00:00",
      "note": "スポットチェック30件/データセットで確認。実質正常例と判断"
    }
  ]
}
```

## 安全性についての注意

- **人間が明示的に `exclude` と判断した画像には絶対に適用されない**
  （`decision_source == human` のものはoverride対象外。安全側ガード）。
  機械の自動判定（`decision_source == default`）だけが対象。
- override適用後も `reason`（＝ `auto_decision_reason`）は変更しない。
  「元々なぜexcludeだったか」は `image_decisions.csv` の `reason` 列に残り続け、
  「誰の承認で今keepなのか」は `override_id` / `override_approved_by` /
  `override_approved_at` / `override_note` 列で別途追跡できる。
