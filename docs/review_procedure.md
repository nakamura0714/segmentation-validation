# 目視レビュー・GUI確認 手順書

チェック・採否確定の詳しい仕組みは [README.md](../README.md) を参照。
このドキュメントは「実際に目視レビューとGUI確認を行うときに何を押すか」だけに絞った
実務用の手順書。コマンドをセルに分けて実行したいなら
[notebooks/pipeline.ipynb](../notebooks/pipeline.ipynb) から進めてもよい
（内容はこの手順書と同じ）。

---

## 0. 前提

```bash
cd /mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation
uv sync --group review   # fiftyone 一式（目視レビューまでやる場合。README 2章）
export FIFTYONE_DATABASE_DIR="$HOME/.fiftyone/var/lib/mongo"   # 毎回打つのが面倒なら ~/.bashrc へ
```

## 1. パイプラインを流す

```bash
uv run segmentation-validation scan --jobs 8   # 初回2〜3分。2回目以降は数秒
uv run segmentation-validation check
uv run segmentation-validation select
```

`select`実行後に出る警告を必ず読む。

- `pending=N が残っている` → 2章のFiftyOneで目視する
- `geometry_uid の重複がある`（AssertionError） → [5. データセットに異常が見つかったら](#5-データセットに異常が見つかったら) へ

## 2. FiftyOneで目視する

```bash
uv run segmentation-validation review build     # 目視対象の画像を書き出す（初回6〜9分）
uv run segmentation-validation review launch    # http://localhost:5151 でApp起動
```

手元のPCから見る（ブラウザで`http://localhost:5151`を開く）。VS Code Remoteなら
「ポート」パネルで転送、それ以外なら新しいターミナルで
`ssh -L 5151:localhost:5151 <本サーバー>`。詳細はREADME 3.6。

### 2.1 保存ビュー（左サイドバー）

| ビュー | 内容 |
|---|---|
| `0-all-real` | 全件。目視対象以外も見たいときの入口（候補シードも含む。後述） |
| `1-pending-annotations` | 目視待ちのannotation |
| `2-pending-images` | 目視待ちの画像（未アノテーションのビュー） |
| `3-outside-body` | 体外領域（S05） |
| `4-tiny-region` | 微小領域（S03） |
| `5-contained-different-label` | 包含された重複・別ラベル（D04） |
| `6-broken-bbox` | 座標が壊れているbbox |
| `7-flagged-for-report` | 目視中に気になった（`flag:needs_report`タグを付けた）annotation/画像 |
| `10-reasoned-decisions` | 理由（`review_reasons`）を入れたannotation。入れ忘れの洗い出しに使う |

### 2.2 判定の入力

annotationを選択 → 属性パネルで以下を入力する。

| フィールド | 値 |
|---|---|
| `review_status` | `keep` / `exclude` / `uncertain` |
| `review_reasons` | **理由。チェックボックスから選ぶ（複数可）。下表参照** |
| `review_reason` | 自由記述。候補で表せないことを書きたいときだけ使う |
| `reviewer` | 自分のメールアドレス |
| `review_comment` | 補足コメント（任意） |

画像自体の採否（未アノテーションのビューが側面像かどうか）はSample側の同名フィールドに入力する。

#### 理由（`review_reasons`）

`review_status`のすぐ下に**チェックボックス**で並ぶ。**自由入力の口が無いので
綴りミスが起きない**（`list<str>` + `checkboxes` のとき App は候補固定のUIを描く）。
複数選べる。

| 値 | 意味 | 側 |
|---|---|---|
| `invalid_duplicate` | 重複 | exclude |
| `older_duplicate` | 古い（ペアの古い方） | exclude |
| `invalid_annotation` | 不適切 | exclude |
| `stray_component` | 飛び地 | exclude |
| `outside_body` | 体外 | exclude |
| `lateral_view` | 側面像 | exclude |
| `wrong_label` | ラベル違い | exclude |
| `visually_valid` | 目視で妥当 | keep |
| `true_small_lesion` | 実際に小さい病変 | keep |
| `valid_nested_annotation` | 入れ子の所見として正当 | keep |
| `boundary_tolerance` | 境界の許容範囲 | keep |
| `frontal_view` | 正面像 | keep |

「重複」と「古い」は**別の事実**なので両方付けてよい（同じマスクが2枚あり、
かつ自分が古い方 → 両方チェック）。片方だけだと「なぜこちらを消したか」が残らない。

選んだ理由は`reason:`タグにも自動で写る（絞り込みと保存ビュー
`10-reasoned-decisions`用）。**正本はフィールド側**で、タグは写し。
`review_status`と`review:`タグの関係と同じ。

> **`review_reason`（単数・自由記述）に機械の理由が残っていても無視される。**
> 以前は目視待ちのannotationに機械の`review_required`が初期値として入っており、
> それがそのまま「人間の理由」として書き出されていた（実データ102件が全部
> `review_required`）。いまは機械の理由を人間の理由として書き出さない。
> **過去102件の理由は上書き済みで復元できない**ので、必要なら付け直す。

**`pending` と `uncertain` の違い**: `pending`は「まだ見ていない」、`uncertain`は
「見たが自分では判断できない（Drに要相談等）」。判断できないものは`pending`のまま
放置せず`uncertain`にする。理由は2つ:

1. 「誰も見ていない」と「見たが判断できない」を後から区別できるようにするため
2. `precision.md`の集計は「目視済（exclude + keep + uncertain）」を分母にするため。
   `pending`のままだと目視した実績としてカウントされない

`uncertain`は候補一覧にも出る（下記「候補シード」参照）。ドロップダウンから選ぶことは
できるが、**FiftyOne標準の入力欄は自由入力もできてしまう**（候補以外を防ぐ強制力は無い）。
候補にない値を打っても保存はされるので、綴りミスに注意する。

### 2.3 重複ペアの「どちらが新しいか」

D03/D04（近似重複・包含重複）のannotationには`newer_in_pair`属性が付く
（`true`=ペア相手より新しい、`false`=古い、`None`=timestampが同値・欠損で比較不能）。
「原則、新しい方を採用する」という方針の判断材料として使う。ただし最終判断は目視で
行うこと（作業ミスに見える画像はそのまま新しい方を採用しない）。

### 2.4 対象外のannotationで気になるものを見つけたら

このツールの検証対象は既定で気胸のみ（`validation.target_labels`）。対象外の
annotationで明らかにおかしいものを偶然見つけた場合は、`flag:needs_report`タグを
付けておく（タグ入力欄の候補に出る）。**このタグにはexport経路が無く、
`review build`で消える**ので、放置せずCSVへ反映しておく。

```bash
uv run segmentation-validation review flag-report
```

`flag:needs_report`が付いたannotation/画像を、識別情報
（`institution`/`patient_id`/`study`/`series`/`file_id`/`dataset_id`）と
実ファイルパス（`path_mask`/`path_original_mask`/`image_path`。`/mnt`・
`/mnt/medicaldb`基準）付きで`output/validation/<fingerprint>/flagged_for_report.csv`
へ書き足す。**マスクの実ファイルはコピーせず、このパスをそのままデータ管理担当へ
伝える。** 何度実行しても行は増え続けるだけで、既存の行は消えない（キーは
`dataset_id`+`geometry_uid`/`file_uid`）ので、目視のたびに気軽に実行してよい。

`serve`（4章）で開いているダッシュボードからは、更新パネルの
「flag:needs_report をCSVへ反映」ボタンでも同じことができる。

**`reason:` と `flag:` は別の軸。** `reason:`は「なぜその採否にしたか」（`review_reasons`
の写し）、`flag:`は「データ管理担当に報告したい」という印。前者は採否の記録として
`selection_decisions.csv`まで残り、後者はApp上の目印でしかない（export経路が無いので
`review build`で消える）。

### 2.5 候補シードについて

`review_status`の`uncertain`や`review_reason`の推奨値は、目視前は実データに
1件も存在しないため、何もしないとFiftyOne Appの候補一覧に出てこない
（FiftyOne 1.21のAppは実データの値をその場で拾って候補にするだけで、
`choices`のような宣言的な候補リストは一切見ない。実機検証済み）。
これを解消するため、`review build`は候補値を実在させるためだけの捨てSample/Detection
（`system:schema_seed`タグ付き、ラベル`__schema_seed__`）を12件追加している
（件数は`ALL_SUGGESTED_REASONS`の数。理由を増やすと増える）。

ただし**`review_reasons`のチェックボックスはこの仕組みに依存しない**。label schemaの
`values`に候補を直接書き込んでいるので、実データに1件も無くても候補が出る
（「宣言的な候補リストは見ない」のは`dataset.classes`/`StringField(choices=)`の話で、
label schemaの`values`はAppが見る）。
`review status`・`review export`・`10-reasoned-decisions`はこの捨て行を除外するので、
件数が狂うことはない。ただし`0-all-real`は**あえて含めている**
（除外すると、実データから値が消えた瞬間に選択式が自由入力に見えてしまう）。

## 3. 判定を採否へ反映する

```bash
uv run segmentation-validation review export    # review_decisions.json / .csv
uv run segmentation-validation select           # 採否へ反映（pending が減る）
uv run segmentation-validation review status    # 進捗確認
uv run segmentation-validation review precision # 自動ルールの答え合わせ
```

`review launch → 目視 → review export → select → review status` を pending が
0になるまで繰り返す。

### DBを作り直した後（`review build`をもう一度実行する前）

```bash
uv run segmentation-validation review export    # ★先に必ず export（消える前に書き出す）
uv run segmentation-validation review build      # DB再構築
uv run segmentation-validation review import     # review_decisions.json から判定を復元
```

## 4. ダッシュボードGUIで確認する

```bash
uv run segmentation-validation report   # summary.md
uv run segmentation-validation gui      # dashboard.html
```

`dashboard.html`は`output/validation/<fingerprint>/`に生成される単一HTML。
ブラウザで直接開けば見られる（サーバー不要）。手元のブラウザから開くには、
本サーバー上で`serve`を上げてポート転送する。

```bash
# プロジェクトルートで1回上げるだけ。上げっぱなしにする。
# 絞り込んでいるなら同じ --set を渡す（フル更新のサブプロセスへ引き継がれる）
nohup uv run segmentation-validation \
  --set 'datasets.exclude=["01331","PTE_CX_MT_PI3"]' \
  serve > output/dashboard_serve.log 2>&1 &

# 止めるとき: ポートを掴んでいるプロセスだけを落とす
ss -ltnp 'sport = :8899'      # PID を確認してから
kill <PID>
```

> **`pkill -f 'segmentation-validation.*serve'` は使わないこと。** プロジェクトの
> パスに `segmentation-validation` が含まれ、`server` という語も `.*serve` に
> 引っかかるため、実機では **`ruff server` と FiftyOne App のサービス（5151）まで
> 巻き込んで5プロセスにマッチする**。目視中に App が落ちる。
> ポート番号は `config.gui.port`（既定 8899）。`--port` を変えているならそこを読み替える。

一息で落とすなら:

```bash
kill "$(ss -ltnp 'sport = :8899' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)"
```

`uv run` の親プロセスは子が終われば一緒に終わるので、ポートを掴んでいる子だけを
落とせばよい。

手元PCの新しいターミナルで（または`~/.ssh/config`に`LocalForward`を書いておく）:

```bash
ssh -L 8899:localhost:8899 <本サーバー>
```

ブラウザで`http://localhost:8899/dashboard.html`を開く。
**インターネットへ公開しないこと。**

`cd output/validation/<fingerprint> && python -m http.server` でも見られるが、
`serve`のほうが2点で優れている。

- **配信先をリクエストごとに解決する。** データセットを足すとfingerprintが変わって
  出力先ディレクトリも変わる。`http.server`は起動時のディレクトリに張り付くので、
  古い成果物を配信し続ける。
- **ページから更新を叩ける。** 右上の「HTMLを再構成」（数秒）と「フル更新」
  （`scan`→`check`→`review export`→`select`→`report`→`gui`）。目視のあと
  `select`を自分で回した場合は、ページを再読み込みするだけで作り直される。
  `scan`と`check`が入っているので、**データセット構成を変えた直後でも
  ボタンだけで一周できる**（ただし新しい構成の`scan`は1時間以上かかりうる）。
  もう1つ「flag:needs_report をCSVへ反映」ボタンがあり、これは
  dashboard.htmlを作り直さず`review flag-report`（2.4節）だけを走らせる。
- **表示する fingerprint を選べる。** 更新パネルの「表示」に、
  データセット本数・最終更新・目視判定の件数つきで一覧が出る。
  **現在の設定と別のものを選ぶと読み取り専用になり、更新ボタンは押せない**
  （別構成の成果物を現在の設定で再生成すると混成HTMLになり、正常な成果物を
  上書きしてしまうため）。更新したいときは「自動（設定に従う）」に戻す。

ノートブックから開く場合は
[notebooks/pipeline.ipynb](../notebooks/pipeline.ipynb) の3.5節に、
`serve`が上がっているかを見てリンクを出すセルがある（**セルはサーバーを立てない**。
カーネル内に立てるとカーネル再起動で止められなくなるため）。

ダッシュボードで確認できるもの: 採否の流れ（症例数つき）/ check別のpending /
面積分布と閾値 / 体外領域の裾50点 / M・D・S系統ごとの check台帳 /
**症例（患者）単位の一覧** / annotationの絞り込み表。
**最終的な合否（annotation単位のkeep/exclude/pending）はここで一覧できる**。

### 4.1 exclude がどの施設に出ているかを見る

「症例で探す」の節を使う。患者1人=1行で、施設・症例状態・データセットで絞り込み、
patient_id（study / file / geometry_uid でも引ける）で検索できる。列見出しで並べ替わり、
既定は exclude が多い順。表の下に**絞り込みに連動する施設別の内訳**が出る。

annotation を持たない患者もここには行がある。`selection_decisions`は
annotation単位（1817行）なので annotation の無い患者は1行も持たないが、
`image_decisions`側でexcludeになった画像（実測33枚。27枚が`nagoya_daiichi`）は
ここでしか追えない。annotationのexcludeと画像のexcludeは母数が違う（1817 vs 1083）
ので、表では別カラムにしてある（`3 +1画像`のような表記）。

行をクリックすると、その患者のkeepでないannotationとkeepでない画像が開く。
「この患者のannotationを下の一覧で見る」を押すとannotation表がその患者で絞り込まれる。

## 5. データセットに異常が見つかったら

### 5.1 一時的に対象から外す

`select`が特定のデータセットのせいで壊れる、または明らかにおかしい場合、
`datasets.exclude`でファイル名の部分一致により一時的に除外できる
（コード変更不要）。

```bash
uv run segmentation-validation --set 'datasets.exclude=["01331","01333","01334","01336","PTE_CX_MT_PI3","PTR_CX_MT_PI3"]' select
```

除外は暫定処置。原因がデータ側にある場合はデータ管理担当に報告し、
修正版が来たら`datasets.exclude`を外して再検証する。

### 5.2 `geometry_uid の重複がある` で `select` が落ちたら

`geometry_uid`は全データセットを跨いで一意という前提でこのツールは組まれている
（README 4章）。複数データセットに同一annotationが重複して存在すると壊れる。
原因を特定する手順（読み取り専用。`uv run python -c "..."`で十分、コード変更は不要）:

```python
from segmentation_validation.config import load_config
from segmentation_validation.cli import _load_context
import collections

config = load_config(None, [])
ctx = _load_context(config)
records = list(ctx.records) + list(ctx.out_of_scope)

by_uid = collections.defaultdict(list)
for r in records:
    by_uid[r.geometry_uid].append(r)
dupes = {uid: rs for uid, rs in by_uid.items() if len(rs) > 1}

print("重複しているgeometry_uidの数:", len(dupes))
# どのデータセットの組み合わせで重複しているか
combo_counts = collections.Counter(
    tuple(sorted(r.dataset_id for r in rs)) for rs in dupes.values()
)
for combo, n in combo_counts.most_common():
    print(n, combo)
```

`patient_id` / `path_mask` などのフィールドまで全部一致していれば、
同一annotationの二重エクスポート（データ管理側のバグの可能性が高い）。
[2026-09実例](#2026-09-実例-pteptrの二重エクスポート) を参考に、
影響範囲（患者数・annotation数）を添えて報告する。

### 5.3 fingerprint が変わったとき（データセットを足した・除外を変えた）

fingerprint は**対象JSONの `size` / `mtime_ns` / `sha256`** から決まる。つまり
`datasets.exclude` を変えたときだけでなく、**共有ディレクトリの symlink が
張り替わっただけでも変わる**。変わると出力先ディレクトリが丸ごと新しくなり、
走査キャッシュも issues.json も目視結果も**そこには何も無い**。

まず自分がどの構成を見ているか確かめる。

```bash
EX='datasets.exclude=["01331","PTE_CX_MT_PI3"]'   # 使っている絞り込み
segmentation-validation --set "$EX" list-sources   # 対象JSON
segmentation-validation --set "$EX" show-config    # 解決後の設定
```

**`--set` は全コマンドで同一にすること。** 1つ違うと fingerprint が変わり、
別のディレクトリに書かれる。「select したのに表示が変わらない」の原因はほぼこれ。

#### 必要なコマンドの列

```bash
segmentation-validation --set "$EX" scan --jobs 8   # ★新しい構成では1時間以上かかる
segmentation-validation --set "$EX" check           # --only は付けない
segmentation-validation --set "$EX" review build    # ★先に下記の引き継ぎを済ませる
segmentation-validation --set "$EX" review launch   # 目視
segmentation-validation --set "$EX" review export
segmentation-validation --set "$EX" select
segmentation-validation --set "$EX" report
segmentation-validation --set "$EX" gui
```

`scan` を飛ばすと `check` は WARNING 1行で完走してしまうが、**画素を要する
14チェックが「検出なし」になる**（画素を要らないのは M06/M07/S02 だけ）。
そのままでは `select` が止まる（`計測キャッシュが足りない`）。承知のうえで
進めるなら `select --allow-unmeasured` だが、**壊れたマスクが keep で通る**。

#### 過去の目視結果を引き継ぐ

**何もしなくてよい。** 目視判定の正本は fingerprint に依存しない
`review/review_decisions.json`（git 管理）にあり、`select` はそこから読む。
データセットを足しても、symlink が張り替わっても、元JSONを再エクスポートしても
判定は引き継がれる。

突合キーは fingerprint と元JSONのファイル名のどちらにも依存しない。

| 対象 | キー | 変わらない理由 |
|---|---|---|
| annotation | `dataset_id::geometry_uid` | `dataset_id_for()` がファイル名末尾の `-YYYYMMDD_HHMMSS` を落とす |
| 画像 | `dataset_id::institution/study/series/file_id` | 同上。`source_json` を含めない |

`output/validation/<fp>/review/review_decisions.json` は正本のスナップショット
（その時点の写し）で、読み込み元にはしない。

> **注意: `dataset_id` が変わると引き継げない。** 元JSONのファイル名が日時以外の
> 部分で変わった場合がこれにあたる。`review status` が「現在の構成に当たらない
> 判定」として件数と例を出すので、そこで気づける。判定は消さずに残る。

まだ移行していないリポジトリなら、一度だけ次を実行する。

```bash
segmentation-validation review migrate-decisions --dry-run   # 件数を確認
segmentation-validation review migrate-decisions
git add review/review_decisions.json review_policy/image_decision_overrides.json
git commit -m "目視判定と承認ルールをGit管理の正本へ移行"
```

`output/validation/*/review/review_decisions.json` を全世代ぶん集めてマージする。
同じキーで判定が食い違う場合は `reviewed_at` が新しい方を採り、日時が無い/同じで
食い違う場合は**停止して両方を出す**（人が決める）。

> **`review build` を先に走らせないこと。** dataset を `overwrite=True` で作り直す。
> 未 export の判定を守るゲートは `review_manifest.json` を基準線にしているので、
> fingerprint が変わった直後は基準線が無い。いまは DB を直接見て
> `reviewer` の入った判定があれば止めるようにしてあるが、`reviewer` 未入力の
> 判定は取りこぼす。**先に `review export`** を済ませる。

### 5.4 未アノテーション画像が異常に多いとき

`image_class`（annotated / negative_case / unannotated_view / unannotated_orphan）を
データセット単位で集計すると、性質の違うデータセットが混ざっていないか分かる
（`unannotated_orphan`は本来ほぼ0件のはずの区分）。

```python
from segmentation_validation.config import load_config
from segmentation_validation.cli import _load_context
from segmentation_validation.selection.image_decisions import build_image_decisions
from segmentation_validation.checks import ALL_CHECKS
import collections

config = load_config(None, [])
ctx = _load_context(config)
issues = [i for check in ALL_CHECKS for i in check.run(ctx)]
image_decisions = build_image_decisions(ctx.groups, issues, config)

by_dataset = collections.defaultdict(collections.Counter)
for d in image_decisions:
    by_dataset[d.dataset_id][d.image_class] += 1
for ds, counter in sorted(by_dataset.items()):
    print(ds, dict(counter))
```

`institution`まで割ると、公開データセット（Kaggle等）由来の画像が意図せず
大量に混ざっていないかも確認できる。

### 2026-09 実例: PTE/PTRの二重エクスポート

参考として、実際に遭遇した事例を残す。

- `PTE_CX_MT_PI3_pneumothorax`（テスト用のはず）と`PTR_CX_MT_PI3_pneumothorax`
  （学習用のはず）が、`dataset_id`以外の全フィールド（`patient_id`/`study`/`series`/
  `image_path`/`path_mask`/`code`/`timestamp`等）が完全一致するannotationを271件、
  さらにそのうち85件は既存の`ETR_ChestMetry_PI6px_with_mask136`とも重複していた
- train/testが排他になっているべきところ完全一致していたため、
  エクスポート処理側のバグと判断してデータ管理担当へ報告し、
  修正を待つ間は`datasets.exclude`で両方とも一時的に除外して進めた
- 別途、新規追加された`ANN_EIRLPRJ_01331/01333/01334/01336`は
  `institution=kaggle_pneumothorax`（公開データセット由来）の画像を大量に含み、
  そのうち`annotations: []`（未アノテーション）の画像が77〜84%を占めていた。
  「陰性所見」を意味するラベルは付いておらず、目視だけでは陰性なのか
  単に未着手なのか判別できなかったため、こちらもデータ管理側に確認を依頼した
