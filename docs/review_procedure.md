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

### 5.5 既存のJSONが新版に差し替わったとき

データ管理側が不備を直して再エクスポートすると、**新しい日時スタンプ付きの別ファイル名**で
`/mnt/medicaldb/annotation/datasets/` に置かれる。`dataset/source/` の symlink を
張り替えるのが「差し替え」。古い実体は共有先に残るので、**新旧を突き合わせられる**。

fingerprint が変わること自体の扱いは [5.3](#53-fingerprint-が変わったときデータセットを足した除外を変えた) と同じ。
ここでは差し替え固有の話だけを書く。

| # | やること | 落とし穴 |
|---|---|---|
| 0 | 現構成を控える | `output/`はgitignore。記録は自分で残す |
| 1 | **何が変わったかを先に測る** | 張り替えてからでは新旧が混ざる |
| 2 | `review export` して commit | 張り替えるとゲートの基準線が消える |
| 3 | symlink を張り替える | **新旧を同時に置かない**／**要らない版は当てない** |
| 4〜10 | 走査〜成果物 | 下記 |

#### 0. 現構成を控える

```bash
segmentation-validation list-sources
cat "output/cache/$(segmentation-validation show-config | grep -o 'v_[0-9a-f]*' | head -1)/manifest.json"
```

`manifest.json`の`sources`に12本の`name`/`real_path`/`size`/`sha256`が入っている。
これが「差し替え前は何を見ていたか」の記録。

#### 1. 何が変わったかを先に測る（張り替える前）

新旧JSONを直接突き合わせる。使い捨てスクリプトでよい。

```bash
uv run python - <<'EOF'
import json, collections, copy

D = "/mnt/medicaldb/annotation/datasets/"
OLD = D + "engineer-set-<id>-<旧スタンプ>.json"
NEW = D + "engineer-set-<id>-<新スタンプ>.json"

def walk(path):
    """(画像キー -> 施設, geometry_uid -> annotation) を返す。"""
    with open(path) as handle:
        root = json.load(handle)["dataset"]
    images, anns = {}, {}
    for inst, studies in root.items():
        for study, sd in studies.items():
            for series, fd in (sd.get("series_list") or {}).items():
                for fid, fe in (fd.get("file_list") or {}).items():
                    images[f"{inst}/{study}/{series}/{fid}"] = inst
                    for a in fe.get("annotations") or []:
                        if a.get("geometry_uid"):
                            anns[a["geometry_uid"]] = a

    return images, anns

oi, oa = walk(OLD)
ni, na = walk(NEW)
print(f"画像 {len(oi)} -> {len(ni)}  (消 {len(set(oi)-set(ni))} / 増 {len(set(ni)-set(oi))})")
print(f"annotation {len(oa)} -> {len(na)}  (消 {len(set(oa)-set(na))} / 増 {len(set(na)-set(oa))})")

# 消えた画像を施設別に割る。施設の全数と一致するなら「施設まるごと」
removed = collections.Counter(oi[k] for k in set(oi) - set(ni))
total = collections.Counter(oi.values())
for inst, n in removed.most_common():
    mark = "  ★施設まるごと" if n == total[inst] else ""
    print(f"  消えた {n:5d} / その施設の全数 {total[inst]:5d}  {inst}{mark}")

# ★最重要: 生き残ったUIDの中身が変わっていないか
changed = collections.Counter()
for g in set(oa) & set(na):
    for f in set(oa[g]) | set(na[g]):
        if oa[g].get(f) != na[g].get(f):
            changed[f] += 1
print("共通UIDで変化したフィールド:", dict(changed.most_common()) or "なし")
EOF
```

判定:

- **共通UIDの中身が変わっていない** → 生き残った目視判定はそのまま有効。増えた分だけ目視すればよい
- **変わっている** → その`geometry_uid`の判定は無効。**ツールは気づかない**
  （キーが同じなので黙って古い判定を適用する）。変わったUIDの一覧を残し、目視で付け直す

#### 1.5 「施設を落としただけ」の新版は当てなくてよい

消えた画像がその施設の全数と一致したら、**旧版からその施設を落として新版と比較する**。

```python
stripped = copy.deepcopy(old_payload)
for inst in 消えた施設:
    stripped["dataset"].pop(inst)
print(stripped["dataset"] == new_payload["dataset"])   # True なら削除のみ
```

`True` なら、その新版には**施設の削除以外の修正が1件も入っていない**。
差し替えても得るものが無く、その施設を失うだけなので、**その1本は据え置いてよい**。
新版が共有先に出たからといって全部当てる必要はない。

`False` なら削除と修正が混ざっている。修正を取るか削除を拒むかの判断が要るので、
データ管理担当へ「この施設の削除は意図的か」を確認する
（材料は施設名・枚数・その施設の全数と一致すること）。

#### 1.6 「いったんkeepして後で除外」ができるかの見分け

| 除外したのは誰か | 後から戻せるか |
|---|---|
| **データ管理側**（新版から消えている） | 差し替えた時点で対象から消えるので`keep`も`exclude`も付けられない。**据え置く以外に残す手は無い** |
| **我々の側**（目視判定 / `datasets.exclude`） | いつでも戻せる |

旧版と新版を**両方置くことはできない**。同じ`dataset_id`が2本になり、D05
（クロスデータセット重複）でそのデータセットのannotationが丸ごと重複扱いになる。

新版が「施設を落としただけ」のときは、この2つが一致する。**据え置いておき、落とすと
決めた時点で差し替えれば、それがそのまま「その施設を除外する」操作になる。**

なお**施設単位のkeep/excludeをツールで直接指定することはできない。**
`datasets.exclude`はファイル名の部分一致＝データセット単位。
`review_policy/image_decision_overrides.json`の`match`は
`dataset_id`/`auto_decision_reason`/`image_class`/`series_image_index`だけで
`institution`が無く、しかも`action: keep`しか効かない
（`selection/image_decisions.py`）。施設単位でやるなら目視判定で1枚ずつ付けるか、
コード変更が要る。

#### 2. 目視判定を書き出して commit する（張り替える前）

```bash
segmentation-validation review export
git add review/review_decisions.json review/review_decisions.csv
git commit -m "目視: 差し替え前の判定を書き出し"
```

張り替えると fingerprint が変わり、`review build`の未export判定ゲートの基準線
（`output/validation/<fp>/review/review_manifest.json`）が消える。基準線が無いと
`reviewer`未入力の判定を取りこぼす（5.3の注記と同じ理由）。

#### 3. symlink を張り替える

1.5で「削除のみ」と分かった版は据え置く。**差し替えるのは必要な1本だけ。**

```bash
cd /mnt/project/chest/metry/pi6/dataset/source
ln -s /mnt/medicaldb/annotation/datasets/engineer-set-<id>-<新>.json .
rm engineer-set-<id>-<旧>.json
```

**新旧を同時に置かない**（前述のD05）。張り替えの途中で他のコマンドを打たないこと。

#### 4. 新構成を確認する

```bash
segmentation-validation list-sources    # 本数が想定どおりか
segmentation-validation show-config     # fingerprint が変わったか
```

#### 5. 走査とチェック

```bash
segmentation-validation scan --jobs 8   # 新 fingerprint なので全件
segmentation-validation check           # --only は付けない
```

**旧 fingerprint のキャッシュを新 fingerprint のディレクトリへコピーしないこと。**
変わっていないデータセットは`file_uid`が同じなので一見飛ばせるが、`pairs.jsonl`は
絞り込みなしでD01〜D04に渡るため、旧版の行が残ると生き残った`geometry_uid`について
**重複issueが二重に出る**。時間を惜しんで静かに壊すより素直に流す。

#### 6. 目視判定の引き継ぎを確認する

```bash
segmentation-validation review status
```

「現在の構成に当たらない判定がN件ある（消さずに残す）」が出る。これが消えた
annotation/画像のうち**判定が付いていたもの**。1で数えた削除数のうちどれだけが
判定済みかを先に数えておき、**その数と一致することを確認する**。

- 一致する → 正常。孤児は消さずに残す（データセットが一時的に外れただけの可能性があるため）
- 極端に多い → `dataset_id`が変わっている疑い。元JSONのファイル名が日時以外の部分で
  変わると`dataset_id_for()`の結果が変わり、判定が全件引き当て不能になる

ログには先頭5件しか出ない。全件見たいときは`review/review_decisions.json`を
削除された`geometry_uid`/画像キーと突き合わせる。

#### 7. 採否を出す

```bash
segmentation-validation select
```

pendingの増分 = 追加されたannotation。

#### 8. 増えた分だけ目視する

```bash
# flagged_for_report.csv は fp 配下なので新 fp には無い。報告待ちの控えを引き継ぐ
cp output/validation/<旧fp>/flagged_for_report.csv output/validation/<新fp>/ 2>/dev/null

df -h /mnt/project                      # ★先に空きを見る
segmentation-validation review build    # 新 fp なのでアセットを全件書き直す
segmentation-validation review launch
# 目視（2章）
segmentation-validation review export
segmentation-validation select
segmentation-validation review status
```

`output/validation/<fp>/review/`は実測で53GB。fingerprint世代が増えるたびに積み上がるので、
**不要になった旧世代を先に消す**。消してよいのは`output/validation/<旧fp>/`と
`output/cache/<旧fp>/`。正本（`review/review_decisions.json`、
`review_policy/image_decision_overrides.json`）はgit側にある。

#### 9. 成果物を作り直す

```bash
segmentation-validation report
segmentation-validation gui
segmentation-validation build-dataset --version-tag <新しいタグ>
segmentation-validation export-lpdata \
    --image-output-dir output/lpdata/<前の版>/images \
    ...   # 他は README 3.11 と同じ
```

- **`--version-tag`は必ず新しくする。** `output/lpdata/<tag>/measurements.jsonl`のキーは
  `development_merged.json::inst/study/series/file`で、**差し替えを跨いで同じ値になる**。
  同じタグを使い回すと、annotationが増減した画像でも古い導出値と古い結合マスクを黙って拾う
- **`--image-output-dir`は前の版を指してよい。** PNG名はDICOMのファイル名stemそのもので、
  中身もDICOMだけで決まる。既存PNGはskipされるので113GiBの再変換を丸ごと回避できる。
  逆に`masks/pneumothorax/`はannotation由来なので**使い回してはいけない**
- 消えた画像のPNGは参照されなくなるが残る。JSONには出ないので害は無い。容量が要るときだけ、
  出力JSONの`sample_id`集合と突き合わせて消す

#### 10. 既知値テストを更新して commit する

```bash
uv run pytest -m realdata
```

`POPULATION`/`ANNOTATED_IMAGES`/`EXPECTED_ISSUES`/`EXPECTED_ZERO`が必ず落ちる。
**落ちること自体は正常。** 1で測った増減と照らして変化が説明できることを確認してから
新しい実測値へ更新し、**どの差し替えでどう変わったかを行コメントで残す**
（既存の`★2026-09-09に対象が3データセットから12へ増えた`と同じ様式）。

```bash
git add review/review_decisions.json review/review_decisions.csv \
        tests/test_realdata_known_values.py
git commit -m "<id> を <日付> 版へ差し替え"
```

#### やってはいけないこと

1. **新旧のsymlinkを同時に置く** — D05でそのデータセットが丸ごと重複扱いになる
2. **旧fingerprintのキャッシュを新fingerprintへコピーする** — `pairs.jsonl`が
   絞り込みなしでD01〜D04に渡るので、重複issueが二重に出る
3. **`build-dataset`/`export-lpdata`で同じ`--version-tag`を使い回す** —
   `measurements.jsonl`が古い導出値を返す（`--image-output-dir`だけは例外で使い回してよい）
4. **`review export`より先に`review build`を走らせる** — 未export判定ゲートの基準線が
   新fingerprintには無いので、`reviewer`未入力の判定を取りこぼす

### 2026-09 実例: 3本の新版のうち2本は当てなかった

2026-09-15に共有先へ3本の新版が置かれた。1と1.5の手順で測った結果:

| dataset | 新版の中身（実測） | 判断 |
|---|---|---|
| `ChestMetry_PI6px_normal` | 旧版 − `fukui_sekijuji` 31枚（この施設の全数）。**他は完全一致** | **据え置き** |
| `ETR_ChestMetry_PI6px_with_mask136` | 旧版 − `jsrt` 3枚（この施設の全数）。**他は完全一致** | **据え置き** |
| `ETR_ChestMetry_PI6px_abnormal_non_pneumothorax` | annotation +4,509 / −623、画像 −557 / +9。消えた画像は10施設に散在 | 差し替え対象 |

- 前2本は`旧版 − 該当施設 == 新版`が`True`で、`version_id`/`version_date`も
  `2.2`/`2026-05-19`のまま。**施設の削除以外の修正が1件も入っていない**ため、
  当てる理由が無い。福井赤十字を残す方針だったので据え置いた
- 落とすと決めた時点で`normal`を20260915版へ差し替えれば、それがそのまま
  「福井赤十字を除外する」操作になる
- 3本目は削除が10施設に散っている（`segmed` 224 / `tokyo_medical` 177 /
  `ishikawa_health_service` 79 ほか）ので「残す」選択ができない。annotation +4,509と
  引き換えに557枚を失う点をデータ管理担当と握ってから当てる
- 3本とも**共通`geometry_uid`のフィールドは1件も変化していない**（file階層・study階層とも）。
  再エクスポートはleafの追加・削除だけで、既存annotationの書き換えは起きていなかった
- 孤児の見込み（6で照合する数）: `abnormal_non_pneumothorax`はこのデータセットの
  目視判定が0件なので**孤児0件**。将来`with_mask136`を当てる場合は消える5件のうち
  **2件が孤児**、`normal`は判定0件なので**0件**

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
