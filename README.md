# ChestMetry PI6 セグメンテーションデータセット バリデーション

PI6 のセグメンテーションデータセットについて

1. 機械的に判定できる不整合を検出し
2. 判断できないものを FiftyOne で目視レビューし
3. **元JSONを変更せずに**開発用データセット JSON を生成する

ためのツール。

**このREADMEを上から順に実行すれば1周できる。** 数値は 2026-09-04 時点の実測値。
閾値・ポリシーを変えたら数値は変わる。

---

## 目次

- [0. 5分で全体像](#0-5分で全体像)
- [1. 用語 — これだけ押さえれば読める](#1-用語--これだけ押さえれば読める)
- [2. 環境構築](#2-環境構築)
- [3. 手順 — 上から順に実行する](#3-手順--上から順に実行する)
- [4. 成果物の読み方](#4-成果物の読み方)
- [5. バリデーション項目一覧](#5-バリデーション項目一覧)
- [6. データの中身](#6-データの中身)
- [7. 設定](#7-設定)
- [8. 困ったとき](#8-困ったとき)
- [9. 設計上の約束](#9-設計上の約束)

---

## 0. 5分で全体像

```
元JSON 3本（1083画像 / 1817 annotation）
   │
   │  scan     … 画素を1度だけ読んで計測値をキャッシュ（約2分）
   │  check    … 各チェックを実行 → issues.json
   │  select   … 全 annotation の採否を確定 → selection_decisions.csv
   ▼
  keep    1465 ┐
  exclude  101 ├ 機械が決めた分
  pending  251 ┘ ← 人が見ないと決まらない分
   │
   │  review build   … 目視用の画像を書き出して FiftyOne に載せる
   │  review launch  … App を開いて目視、判定を入力
   │  review export  … 判定を review/review_decisions.json（正本）へ
   │  select         … 採否へ反映（pending が減る）
   ▼
  pending 0 になったら
   │
   │  build-dataset  … keep だけを残した development.json
   ▼
  開発用データセット
```

**pending = 0 が目視完了の条件。** それまで `build-dataset` は既定で止まる。

---

## 1. 用語 — これだけ押さえれば読める

### 1.1 チェックの3系統

| 系統 | 何を見るか | たとえば |
|---|---|---|
| **M系**（M01–M09） | **機械的な整合性。** ファイルが読めるか、マスクの形式が規定どおりか、JSONの宣言値と実データが合っているか | マスクの解像度が DICOM と違う / ファイルが無い / 空マスク |
| **D系**（D01–D04） | **重複アノテーション。** 同じ画像の中に同じ領域が二重に登録されていないか | 別の人が同じ場所を塗り直して2件になっている |
| **S系**（S03–S05） | **医学的・形状的に疑わしいもの。** 人が画像を見ないと妥当性を判断できないもの | 面積が小さすぎる / 体外にはみ出している |

> S01・S02 は欠番。旧設計の S01（重複マスク）は D01–D04 に分割し、S02（label_id重複）は
> 削除した（`label_id` は全域で一意で、ラベルメタデータの重複はマスク品質の問題ではないため）。

### 1.2 Issue と SelectionDecision — 最重要

**ここを混同すると全部読めなくなる。**

| | Issue | SelectionDecision |
|---|---|---|
| 答える問い | **プログラムが何に気づいたか** | **最終的に開発データに使うか** |
| ファイル | `issues.csv` | `selection_decisions.csv` |
| 行の粒度 | 1 annotation に複数行あり得る | **必ず 1 annotation = 1行** |

**Issue があること ≠ 除外。**

微小領域として検出された annotation を目視して「医学的に妥当な小さい病変だ」と判断したら:

```
issues.csv              : S03_TINY_ANNOTATION が1行     ← 検出した事実は消えない
selection_decisions.csv : final_decision = keep         ← 開発データには使う
                          reason = visually_valid
                          decision_source = human
```

検出の記録が消えないので、後から「**この自動ルールは何割が本当に不正だったか**」
（Precision）を計算できる。これがルール改善の根拠になる。

### 1.3 採否の4状態（`final_decision`）

| 値 | 意味 | 開発データに入るか |
|---|---|---|
| `keep` | 使う | 入る |
| `exclude` | 使わない | 入らない |
| `pending` | **まだ人が見ていない。目視待ち** | 未確定（`build-dataset` が止まる） |
| `uncertain` | 見たが判断できなかった（要相談） | 未確定（同上） |

### 1.4 誰が決めたか（`decision_source`）

| 値 | 意味 |
|---|---|
| `automatic` | 機械が確定させた（完全一致の重複、明確に壊れたマスク） |
| `human` | FiftyOne で人が判断した |
| `default` | 誰も判断していない。Issue が無い、または検査できなかった |
| `-` | 未決定（`pending`） |

### 1.5 なぜその採否になったか（`reason`）

| 値 | 意味 | 現在 |
|---|---|---|
| `no_issue_detected` | 全チェックを通って問題なし | 984 |
| **`kept_without_full_check`** | **一部のチェックが実行できないまま keep した** | 481 |
| `review_required` | 目視が必要なので `pending` にした | 251 |
| `older_exact_duplicate` | 完全一致の重複のうち古い方（自動 exclude） | 101 |
| `broken_mask` | 機械が確定的に「使えない」と判定（自動 exclude） | 0 |
| `out_of_scope` | 検証対象ラベルの外（既定は全病変なので0） | 0 |
| `invalid_duplicate` / `older_duplicate` / `invalid_annotation` 等 | 人が見て判断した理由（`review_reasons` のチェックボックス。複数なら `\|` 区切り） | 0（未入力） |

**機械の理由と人間の理由が同じ列を共有している。** 区別は併記される `decision_source`（`automatic` / `human` / `default`）で行う。`review_required` のような機械の値が人間の理由として入ることは無い（`review export` が落とす）。

#### `cannot_determine` なのに `keep` なのはなぜか

体外領域チェック（S05）には胸郭の参照マスクが必要だが、参照マスクは全体の約3割で
整備されていない（`01272` は 0/296 ファイル）。取れる選択肢は3つ:

1. 黙って keep にする → **禁止。**「問題なし」と「未検査」が区別できなくなる
2. 全部 pending にして目視する → 509件の工数が乗る
3. **keep にするが「未検証」と明記する** ← 現在の方針

なので該当する annotation は `reason = kept_without_full_check`、
`unverified_checks = S05_REFERENCE_UNAVAILABLE` と記録される。
つまり **「検査して問題なかった」のではなく「検査できなかった」** ことが CSV 上で分かる。

`unverified_checks != none` で絞れば **後から目視に回すのは1コマンド**（[7章](#7-設定)）。
現在 `unverified_checks != none` は509件で、うち481件が `kept_without_full_check`。
差の28件は判定不能に加えて目視が必要な Issue も持つため `pending` が優先されている。

### 1.6 成果物いちらん

| ファイル | 答える問い | 行 |
|---|---|---|
| `issues.json` / `.csv` | プログラムが何を検出したか | 1364（annotation 単位 1144 + 画像単位 220） |
| **`selection_decisions.json` / `.csv`** | **annotation を開発データに使うか（採否の正本）** | **1817（必ず1 annotation=1行）** |
| **`image_decisions.json` / `.csv`** | **その画像を開発データに含めるか** | **1083（必ず1画像=1行）** |
| **`review/review_decisions.json`** | **人が何と判断したか（目視判定の正本・git管理）** | 目視した分だけ |
| `summary.md` | 検出件数・判定不能の理由・参照マスクのカバレッジ・施設別/アノテータ別の偏り | — |
| `selection_summary.md` | **開発データを作ってよい状態か** | — |
| `precision.md` | 自動ルールのうち何割が本当に不正だったか | check ごと |
| `dashboard.html` | 以上をブラウザで辿る（`serve` で常駐配信できる） | — |
| `development.json` | keep だけを反映した開発用データ | 元JSONと同一スキーマ |
| **`development_merged.json`** | **12本を1本にまとめた学習パイプライン入力** | 元JSON互換 + file entry に `dataset_id` |
| **`development_manifest.csv`** | **最終開発データに実際に入った画像の明細（症例IDレベル）** | **統合JSONの画像数（必ず1画像=1行）** |
| `image_duplicates.csv` | データセット横断の同一画像をどちら側で採ったか | 重複した分だけ |
| **`chest_metry_pi6_pneumothorax.json`** | **学習側（med-chest-metry-pi6）が読む lp-data 標準形式** | 1画像=1サンプル |
| `lpdata_export_summary.md` | lp-data 書き出しの結果と、**次に人間が決めるべきこと** | — |
| `mask_merge_manifest.json` | 結合気胸マスクの再現情報（元マスクと期待画素数） | マスクを持つサンプル分 |

出力先は `output/validation/<fingerprint>/`、`output/development/<dataset>/<version>/`、
`output/lpdata/<version>/`。
`<fingerprint>` は対象JSONのサイズ・mtime・sha256 から作る
（`version_id` は3本とも `2.2` なので、それ単独ではキーにできない）。

### 1.7 正本と生成物

`output/` は丸ごと gitignore。**git に置くのは再生成できないものだけ。**

```
git管理（正本・再生成不可）              自動生成（派生物・再生成可能）
├─ src/                                  ├─ output/validation/<fp>/issues.json
├─ review/review_decisions.json  ──▶     ├─ output/validation/<fp>/selection_decisions.csv
│    目視判定。fingerprint非依存          ├─ output/validation/<fp>/image_decisions.csv
└─ review_policy/                        ├─ output/validation/<fp>/dashboard.html
     image_decision_overrides.json       ├─ output/development/<ds>/<ver>/development.json × 12
     承認済みの exclude→keep ルール       └─ output/development/<ver>/development_merged.json
```

`output/validation/<fp>/review/review_decisions.json` は正本のスナップショット
（その時点の写し）で、読み込み元にはしない。詳細は
[`review/README.md`](review/README.md)。

**fingerprint が変わっても目視結果は引き継がれる。** 突合キーが fingerprint にも
元JSONのファイル名にも依存しないため（annotation は `dataset_id::geometry_uid`、
画像は `dataset_id::institution/study/series/file_id`）。

既存のJSONが新版に差し替わったときの手順は
[`docs/review_procedure.md` 5.5](docs/review_procedure.md#55-既存のjsonが新版に差し替わったとき)。
**新版が「施設を落としただけ」なら当てる必要が無い**ので、その見分け方もそこにある。

---

## 2. 環境構築

```bash
cd /mnt/project/chest/metry/pi6/work/nakamura/segmentation-validation

# 検証本体だけ（16パッケージ）
uv sync
```

これだけで `scan` / `check` / `select` / `report` / `gui` / `build-dataset` が動く。

### 目視レビューまでやる場合

```bash
uv sync --group review
```

**122パッケージ**（16 + 106）+ mongod（Linux はホイールが無く、インストール時に
`fastdl.mongodb.org` から 86MB をダウンロードする）。
`uv sync`（`--group review` なし）に戻すと 106個がアンインストールされるので、
目視レビューを続ける間は `--group review` を付けたままにする。

**mongod のデータ置き場はローカルディスクにすること。** NFS 上（`/mnt` 配下）だと壊れる。

```bash
findmnt -T "$HOME" -o TARGET,SOURCE,FSTYPE
#   FSTYPE が ext4 / xfs などならOK。nfs なら別のローカルディスクを指定する

export FIFTYONE_DATABASE_DIR="$HOME/.fiftyone/var/lib/mongo"
#   毎回打つのが面倒なら ~/.bashrc に書く。config の review.database_dir でも指定できる
```

### 環境の確認

```bash
# cv2 が1種類だけか（opencv-python と headless が共存すると壊れる）
uv run python -c "import importlib.metadata as m, cv2; \
  print(cv2.__version__, [d.metadata['Name'] for d in m.distributions() \
  if d.metadata['Name'] and 'opencv' in d.metadata['Name'].lower()])"
#   -> 4.14.0 ['opencv-python-headless']

# テストを流す（合成データだけ。数秒で終わる）
uv run pytest
#   -> 567 passed
```

### テスト

```bash
uv run pytest                  # 既定。合成データだけで 221件 / 約1秒
uv run pytest -m realdata      # 実データの既知値 38件（走査キャッシュが必要）
uv run pytest -m fiftyone      # FiftyOne の往復 10件（uv sync --group review が必要）
uv run pytest -m 'realdata or fiftyone'   # 全部
```

**閾値やポリシーを意図的に変えたら `-m realdata` も一緒に更新する。**
既知値の一覧は [tests/test_realdata_known_values.py](tests/test_realdata_known_values.py)
の先頭にまとまっている。更新せずに落ちたら、それは意図しない変更が入ったということ。

| ファイル | 何を守っているか |
|---|---|
| [test_d01_automatic.py](tests/test_d01_automatic.py) | D01 の自動採否。**`timestamp` の同値/欠損/解釈不能は実データ0件なのでここでしか担保できない** |
| [test_broken_masks.py](tests/test_broken_masks.py) | M01–M05 の自動 exclude。**実データ0件。**severity/status/category の3条件が緩むと大量誤除外 |
| [test_decision_ladder.py](tests/test_decision_ladder.py) | 採否の優先順位。**「D01 の keep は最終keepではない」**を含む |
| [test_image_decisions.py](tests/test_image_decisions.py) | 正常例187枚と未アノテーション33枚の取り違え防止 |
| [test_gates.py](tests/test_gates.py) | 3つのゲート（部分 issues / 未export判定 / override）|
| [test_review_layer.py](tests/test_review_layer.py) | `auto:`/`review:` の分離、機械の判定を human にしない、壊れた bbox の表示 |
| [test_architecture.py](tests/test_architecture.py) | 構造の不変条件を AST で検査（[9章](#9-設計上の約束)） |
| [test_no_fiftyone.py](tests/test_no_fiftyone.py) | fiftyone を実行時に遮断して検証本体が動くことを確認 |
| [test_review_roundtrip.py](tests/test_review_roundtrip.py) | **DB削除 → 再構築 → import で判定が戻る**（`-m fiftyone`）|
| [test_realdata_known_values.py](tests/test_realdata_known_values.py) | 実データの既知値（`-m realdata`）|

---

## 3. 手順 — 上から順に実行する

> コマンドラインで1つずつ打つ代わりに [notebooks/pipeline.ipynb](notebooks/pipeline.ipynb) から
> セル実行することもできる（`uv sync --group notebook` が必要）。内容はこの3章と同じ。

### 3.1 対象を確認する

```bash
uv run segmentation-validation list-sources   # 対象JSON 3本
uv run segmentation-validation list-checks    # 登録チェック一覧
uv run segmentation-validation show-config    # 解決後の設定
```

### 3.2 走査（画素を読む）

```bash
uv run segmentation-validation scan --jobs 8
```

- 1665枚のマスクと1083件のDICOMヘッダを**1度だけ**読んで計測値をキャッシュする
  （マスク 3233行 = 修正後1665 + 修正前1568 / ペア 1446行）
- **2〜3分**（`--jobs 8`。実測 1分53秒〜2分35秒）。キャッシュは 6.9MB。
  2回目以降は走査済みを飛ばして**1秒未満**で終わる
- 途中で中断しても再開できる（`--force` で作り直し、`--limit 20` でスモークテスト）

> 所要時間は**データが乗っている NFS の混み具合で大きくぶれる**。共有サーバーなので
> `cat /proc/loadavg` と `uptime` を見て、他の人の学習ジョブが張り付いていないか
> 確認してから流すこと。

### 3.3 チェックを実行する

```bash
uv run segmentation-validation check
```

→ `issues.json` / `issues.csv`（1364行 = annotation 単位 1144 + 画像単位 220）。
数秒で終わる。**error があると exit 1** を返す（夜間バッチ用）。

パス規約や bbox の閾値を詰めているときは、JSONだけで判定できるチェックを1秒で回せる:

```bash
uv run segmentation-validation check --only M06,M07
```

> **`--only` / `--skip` を使ったら、`select` の前に `--only` なしで回し直すこと。**
> `issues.json` は毎回上書きされるので、部分実行した結果のまま `select` すると
> 未実行のチェックが「検出なし」と区別できず**採否が壊れる**
> （実測: pending 251 → 11、keep 1465 → 1705 になる）。
> このため成果物に「何を実行したか」を刻んであり、**`select` は部分結果を見ると停止する**。
> 承知の上で進めるなら `select --allow-partial`。

### 3.4 採否を確定する

```bash
uv run segmentation-validation select
```

→ `selection_decisions.csv`（1817行）と `image_decisions.csv`（1083行）。

初回は目視判定がまだ無いので、目視対象は `pending` のままになる。それが正しい。

### 3.5 結果を見る

```bash
uv run segmentation-validation report   # → summary.md
uv run segmentation-validation gui      # → dashboard.html（単一HTML・807KB）
```

`summary.md` に出るもの: 検出件数 / 判定不能の理由の内訳 / **参照マスクのカバレッジ（毎回実測）** /
施設別の検出件数 / **体外領域のアノテータ別の偏り**。

`dashboard.html` はブラウザで開く。JSON/CSV より速く全体が掴める。
含まれるもの: 採否の流れ（症例数つき）/ 症例単位の合格数 / check 別の pending
（annotation 数と画像枚数の両方）/ **面積分布と閾値** / 体外領域の裾50点 /
M・D・S 系統ごとに開閉できる check 台帳 / **症例（患者）単位の一覧** /
annotation の絞り込み / 用語解説。
外部依存は Google Fonts のみ（面積分布などのグラフはここにしか出ない）。

「**exclude がどの施設に出ているか**」は「症例で探す」の節で追う。患者1人=1行で、
施設・症例状態・データセットで絞り込み、patient_id で検索できる。
annotation を持たない患者もここには行があるので、`image_decisions` 側で
exclude になった画像（実測33枚。27枚が `nagoya_daiichi`）もここから辿れる。

### 3.5.1 常駐サーバーで見る（`serve`）

`dashboard.html` はファイルを直接開いても動くが、**上げっぱなしにする常駐
サーバー**を用意してある。ターミナルから1回起動するだけでよい。

```bash
nohup uv run segmentation-validation serve > output/dashboard_serve.log 2>&1 &
```

→ <http://localhost:8899/dashboard.html>（ポートは config の `gui.port`）。
`ssh -L 8899:localhost:8899 <本サーバー>` でトンネルを張って開く。
**インターネットへ公開しないこと。**

止めるときは**ポートを掴んでいるプロセスだけ**を落とす。

```bash
ss -ltnp 'sport = :8899'      # PID を確認してから
kill <PID>
```

> **`pkill -f 'segmentation-validation.*serve'` は使わないこと。** プロジェクトの
> パスに `segmentation-validation` が含まれ、`server` も `.*serve` に引っかかるため、
> 実機では **`ruff server` と FiftyOne App のサービス（5151）まで巻き込む**。
> コードを変えたあとは、この停止 → 起動で反映される（`uv run` は作業ツリーを実行する）。

jupyter のセルからは起動しない。**カーネル内にサーバーを立てるとカーネル再起動で
止められなくなり**（`Address already in use` のまま回復できない）、配信先が
起動時点のディレクトリに固定されるので**データセットを足すと古い成果物を
配信し続ける**。`serve` は配信先を**リクエストごとに**解決するので、
データセットを足しても再起動が要らない。

サーバー経由で開くとページ右上に更新ボタンが2つ出る（ファイルを直接開いたときは
出ない）。

| ボタン | 中身 | 目安 |
|---|---|---|
| HTMLを再構成 | ディスク上の成果物から `dashboard.html` を作り直す | 数秒 |
| フル更新 | `scan` → `check` → `review export` → `select` → `report` → `gui` | 数分 |

**フル更新には `scan` と `check` が入っている。** 無いと、データセット構成を変えた
直後は `select` が「issues.json が無い」で必ず落ち、ボタンでは解消できない。
`scan` は走査済みを飛ばすので通常は1秒未満で終わるが、**新しい構成では
33,000枚のDICOMを実際に読むため1時間以上かかることがある**
（走査キャッシュは fingerprint 単位で分離されており、構成を変えると流用されない）。

自分で `select` を回したあとは、**ページを再読み込みするだけ**でよい
（成果物のほうが新しいことを検知して作り直す）。ログは `output/dashboard_serve.log`。

`datasets.exclude` などで絞り込んでいるなら、**起動時に同じ `--set` を渡す**こと。
フル更新のサブプロセスへそのまま引き継がれる。

```bash
nohup uv run segmentation-validation \
  --set 'datasets.exclude=["01331","PTE_CX_MT_PI3"]' \
  serve > output/dashboard_serve.log 2>&1 &
```

主なオプション: `--port` / `--host` / `--no-refresh`（閲覧専用にする）。
ポートが埋まっていれば掴んでいるプロセスの PID を出して exit 2 で止まる。

> **fingerprint が変わっても目視判定は引き継がれる。** 正本は fingerprint に
> 依存しない `review/review_decisions.json`（git 管理）にあり、突合キーも
> fingerprint と元JSONのファイル名のどちらにも依存しない。`serve` は状態表示に
> 「設定が指す fingerprint」「配信中の fingerprint」「人間の判定の件数」を出すので、
> 想定と違えば気づける。詳細は [review/README.md](review/README.md)。

#### 表示する fingerprint を選ぶ

更新パネルの「表示」で fingerprint を選べる（`serve --fingerprint <fp>` でも
起動時に固定できる）。一覧には**データセット本数・最終更新・目視判定の件数**が出る
—— hash だけでは「古い版」と「別構成」の区別が付かないため。

**現在の設定と別の fingerprint を選ぶと読み取り専用になる。** 更新ボタンは
両方とも押せない。別構成の成果物を現在の設定で再生成すると、
「12本構成の採否に6本構成の母集団と計測値を貼った混成HTML」ができて、
しかも正常な成果物を上書きしてしまうため。更新したいときは「自動（設定に従う）」
に戻す。

更新ボタンが押せない理由は状態表示に出る。よくあるのは次の2つ。

| 表示 | 意味 |
|---|---|
| 別の構成。混ぜて再生成しないため読み取り専用 | 表示中の fingerprint が設定と違う |
| 計測キャッシュが1件も無い（scan していない） | 再生成すると面積・包含率が空のHTMLになる |

### 3.6 FiftyOne で目視する

> 実務用の手順書（保存ビューの使い方、候補シード、`newer_in_pair`、GUI確認、
> データ異常を見つけたときの対応まで）は
> [docs/review_procedure.md](docs/review_procedure.md) にまとめてある。

```bash
export FIFTYONE_DATABASE_DIR="$HOME/.fiftyone/var/lib/mongo"

uv run segmentation-validation review build
```

- 目視対象の画像を PNG に変換し、マスクを bbox で切り出して FiftyOne dataset を作る
- **初回は6〜9分**（実測 6分29秒 / 8分46秒）。210 Sample / 521 Detection /
  画像210・マスク473・バンド150 で **397MB**。DICOM の全画素読みが1枚1〜3秒
- 2回目以降は既存アセットを飛ばして**約1分**（実測 54秒）

> **既定では「目視対象の画像」しか載らない。** ただしその210枚に含まれる
> **keep の annotation は既に載っている** —— manifest はその画像の全 annotation を
> 入れるので、pending 251 に対して Detection は 521 ある。
> 目視対象の隣にある正常な annotation を文脈として見られる。

#### keep になった画像も全部 FiftyOne で見たいとき

```bash
# 単発で
uv run segmentation-validation review build --all

# ずっとそうしたいなら config で（--all と同じ意味。--all が config を上書きする）
uv run segmentation-validation --set review.export_all_files=true review build
```

| | 既定 | 全画像 |
|---|---|---|
| 画像 | 210 | **1083** |
| 容量 | 397MB | **約2GB** |
| 初回 | 6〜9分 | **約25〜30分** |

既存アセットはスキップするので、既定で1周したあとに切り替えても差分だけ書き出す。
**保存ビュー6つは pending で絞っている**ので、keep の画像を見るときは App 側で
フィルタを外すか `image_class` / `review_status` で絞る。

> ★**`review build` の前に必ず `review export` する。**
> `review build` は FiftyOne dataset を作り直すので、**export していない判定は消える**。
> 判定の正本は `review/review_decisions.json`（git 管理）であって DB ではない。
> 消える判定があるときは**停止する**ので、指示どおり `review export` してから
> やり直せばよい（捨ててよいなら `review build --discard-unexported`）。

```bash
uv run segmentation-validation review launch
```

App が本サーバーの `localhost:5151` で起動する。**インターネットへ公開しない。**
手元の端末から SSH トンネルで見る:

```bash
# 踏み台を経由する場合
ssh -L 5151:localhost:5151 <踏み台> -t ssh -L 5151:localhost:5151 <本サーバー>

# ~/.ssh/config に ProxyJump を書いておくなら
#   Host pi6
#     HostName <本サーバー>
#     ProxyJump <踏み台>
ssh -L 5151:localhost:5151 pi6
```

ブラウザで `http://localhost:5151` を開く。

### 3.7 App での目視のやりかた

左サイドバーの保存ビューから入る。**名前に ASCII を含めているのは、FiftyOne が
ビュー名を slug 化するため**（日本語だけの名前は空 slug になって作成に失敗する）。

| ビュー | 何を見るか |
|---|---|
| `1-pending-annotations` | 目視待ちの annotation（251件） |
| `2-pending-images` | 目視待ちの画像（33枚）。**側面像なら除外** |
| `3-outside-body` | 体外領域（50件） |
| `4-tiny-region` | 微小領域。**ここでの判定が S03 の閾値を決める** |
| `5-contained-different-label` | 包含された重複（別ラベル）。入れ子の所見の可能性 |
| `6-broken-bbox` | 座標が壊れている bbox。`original_bbox` に原座標 |

判定は次の3つを入力する。

| 入力先 | フィールド | 値 |
|---|---|---|
| **annotation の良否** | Detection（Label）の `review_status` | `keep` / `exclude` / `uncertain` |
| | `review_reasons` | **理由。チェックボックスから複数選ぶ**（`invalid_duplicate` = 重複、`older_duplicate` = 古い、`invalid_annotation` = 不適切 など12種） |
| | `review_reason` | 自由記述。候補で表せないことだけ |
| | `reviewer` | 自分のメールアドレス |
| **画像自体を使うか** | Sample の `review_status` | `keep` / `exclude` |
| | `review_reason` | `frontal_view` / `lateral_view` など |
| | `reviewer` | 同上 |

> **annotation の判定は必ず Detection 側に入れる。** 1枚の画像に複数の annotation が
> あるので、Sample に付けるとどれがダメだったのか分からなくなる。
> 逆に「この画像自体を使うか」は画像の属性なので Sample 側
> （annotation を持たない33枚はここでしか判定できない）。

> `reviewer` は空でも判定は有効だが、後から追えなくなる。埋めることを推奨
> （空のままだと `review export` が警告を出して `unknown` で記録する）。

> **理由は `review_reasons`（チェックボックス）で入れる。** 自由入力の口が無いので
> 綴りミスが起きず、`summary.md` と `dashboard.html` の集計に乗る。
> 以前は自由記述の `review_reason` に機械の `review_required` が初期値として
> 入っており、それが「人間の理由」として書き出されていた（実測102件が全部それ）。
> **過去分は上書き済みで復元できない**ので、必要なら App で付け直す。
> 詳細は [docs/review_procedure.md](docs/review_procedure.md) 2.2。

### 3.8 判定を採否へ反映する

```bash
uv run segmentation-validation review export   # → review/review_decisions.json（正本・git管理）
uv run segmentation-validation select          # → 採否へ反映（pending が減る）
uv run segmentation-validation review status   # 進捗
uv run segmentation-validation review precision  # → precision.md
```

**このループを pending が 0 になるまで繰り返す。**
`select` は検出をやり直さないので、目視のたびに回しても速い。

```
review launch →（目視）→ review export → select → report
```

`review build` で DB を作り直したあとは、判定を DB へ戻しておく:

```bash
uv run segmentation-validation review export    # ★先に必ず export
uv run segmentation-validation review build     # DB 再構築（export 済みなら安全）
uv run segmentation-validation review import    # 正本から判定を復元
```

往復は実機で確認済み。4 annotation・2画像の判定を入れて
`export → select → import` を回すと、DB の状態が採否マスタと一致するところまで戻る
（annotation pending 251→247 / 画像 pending 33→31・exclude 1）。

### 3.9 開発用データセットを生成する

```bash
uv run segmentation-validation build-dataset
```

`pending` / `uncertain` が残っていると**既定で止まる**（exit 1）。それが正しい。

先に進める必要がある場合は「許可」と「扱い」の**両方**を明示する:

```bash
uv run segmentation-validation build-dataset \
  --allow-pending --pending-as exclude \
  --version-tag 20260904
```

`--allow-pending` だけではエラーになる。保留のまま黙って入る／落ちるのを防ぐため。
override を使うと `development.json` の meta と `selection_summary.md` に記録される。

→ `output/development/<dataset>/<version>/development.json` と
`output/development/<version>/selection_summary.md`、そして
`output/development/<version>/development_merged.json`（統合JSON）。
統合JSONを作った場合は同じディレクトリに
`development_manifest.csv`（**中身の明細**）が、クロスデータセット重複があれば
`image_duplicates.csv`（採否の監査）が並ぶ。

#### 統合JSON（学習パイプライン入力）

12本の `development.json` を1本へまとめたもの。**元JSONと同じスキーマ**のまま、
各 file entry に由来を示す `dataset_id` を1つ足しただけ。

```json
"file_list": {
  "CXSGM00040183_003_000_000": {
    "dataset_id": "ChestMetry_PI6px_normal",
    "image_path": "medical2/...",
    "annotations": [ ... ]
  }
}
```

`dataset_id` を file entry に置くのは、**file 単位が衝突しない唯一の階層**だから。
実測では institution が 35個中23個、`(institution, study)` が 42,690組中4,274組、
`(inst, study, series)` が 42,708組中4,277組で複数データセットに跨る。一方
`(inst, study, series, file_id)` は 42,574件すべてで衝突しない
（`resolve_image_duplicates` がクロスデータセット重複を解消済みのため）。
上位階層に `dataset_id` を置くと train/test の由来を表現できない。

統合は4階層を**再帰的に union** する。浅い `dict.update()` だと、同じ series の下で
データセットごとに別の file_id を持つケース（実在する）を取りこぼす。

`meta_development` にはデータセット別の内訳（`source_sha256` 込み）と `totals`、
そして元データ間で食い違った属性が入る。

```json
"conflicts": [
  {"level": "study", "path": "segmed/CXSGM00040183", "field": "study_date",
   "values": {"ChestMetry_PI6px_normal": "2020-01-11 00:00:00+00:00",
              "ETR_ChestMetry_PI6px_abnormal_non_pneumothorax": "2019-01-05 00:00:00+00:00"},
   "adopted": "ChestMetry_PI6px_normal", "rule": "dataset_id 昇順の先頭"}
]
```

**どちらが正しいかツールは決めない。** 両方の値を残し、採用値は決定的な規則
（dataset_id 昇順の先頭）で選ぶ。元データ側の不整合なので、`selection_summary.md`
に毎回出してデータ管理側への報告材料にする。現状この1件だけ。

★**食い違いの記録は剪定より前**に行う。現状の1件はまさに「画像を出す側で `shape` の z=2、
画像0枚の側で z=1」という形で、先に剪定すると空の側が統合に参加せず報告されなくなる。
同じ series が二重登録されているのは元データ側の登録バグであり、
どちらが画像を出したかとは独立に報告し続ける必要がある。
これが**剪定を全 merge の完了後に行う理由**で、per-dataset 側を剪定できない理由でもある
（per-dataset payload は merge へ参照ごと渡して直後に手放す設計なので、
全 merge の完了まで生き残れない）。

#### 何が入っているか（内訳と明細）

統合JSONは raw 約100MB あり、中身を直接読むものではない。**何が入ったかは2か所で見る。**

1. `selection_summary.md` 末尾の **「何が入っているか（データセット別）」** 表。
   データセットごとの 施設 / 患者 / study / 画像 / annotation / 0件の画像 / 主な病変クラス。
2. `development_manifest.csv` —— **1画像=1行の明細**。列は
   `dataset_id` / `institution` / `patient_id` / `study_key` / `study_date` /
   `series_key` / `file_id` / `file_uid` / `image_path` / `n_annotations` / `lesion_classes`。

`file_uid` は `image_decisions.csv` / `image_duplicates.csv` / lpdata の
`measurements.jsonl` と**同じキー**なので、そのまま突合できる
（「この画像はなぜ入ったのか」を採否まで遡れる）。
病変クラス × データセットの全行列が要るときは、この明細を
`dataset_id` × `lesion_classes` で集計する —— 表に載せると横に伸びて読めないため、
summary 側は上位3クラスだけに留めてある。

**画像と annotation の列は足し上げると合計と一致する**（1画像は必ず1データセットに属する）。
一方 **施設 / 患者 / study は足し上げても合計と一致しない**。同じ患者・study が複数
データセットに跨るため（上記の実測: `(institution, study)` は 42,690組中4,274組が跨る）。
各行は「そのデータセット由来の画像を1枚以上含む患者/study の数」。

内訳表の患者 / study は**画像を1枚以上持つもの**だけを数える。統合JSONは
**画像を1枚も持たない器（series / study / institution）を剪定してある**ので、
規模行の study と合計行の study は一致する（食い違ったら剪定が効いていないということなので、
summary に警告が出る）。

#### 画像0枚の器を剪定する

`build-dataset` は統合JSONを書き出す前に、**file entry を1件も持たない
series / study / institution を取り除く**。件数は `meta_development.pruned` と
`selection_summary.md` に必ず出す。実測では series 171 / study 171 / 施設 0。

空の器は判断の結果ではなく**副作用**。元JSON（engineer-set）13本には空の series も
空の study も1件も無く、`image_decisions` に従って file entry を落とした結果として生まれる
（実測では images_dropped 3039枚のうち 2833枚がクロスデータセット重複による skip）。
器を残したまま `totals.studies` を出すと、中身の無い study を数えた不正確な報告になる。

「annotation が0件になっても file entry は残す」規則とは**別の話**。
file entry は画像が実在するので残す。器は中身が0なら何も表していない。
**剪定で画像数と annotation 数は1件も動かない**ので母集団は不変。

★**剪定するのは統合JSONだけ**で、per-dataset の `development.json` には空の器が残る
（実測 合計3005 study。とくに `ETR_ChestMetry_PI6px_pneumothorax_add` は 2682 study 中 2681 が空）。
統合すると他データセットが同じ study を埋めるので171件まで減る。
per-dataset 側を剪定しないのは、剪定が**全データセットの merge 完了後**にしか行えないため
（下の「属性の食い違い」を参照）。per-dataset JSON はどこからも読まれない中間・監査成果物で、
学習パイプラインへ渡るのは統合JSONの方。

| オプション | 効果 |
|---|---|
| `--no-merged` | 統合JSONを作らない（既定は作る）。`development_manifest.csv` も作らない —— 明細は「統合JSONに何が入ったか」を表すものなので、統合JSONが無いときに出すと存在しないファイルの目録になる。データセット別の件数は各 `development.json` の `meta_development` にある |
| `--merged-name NAME` | ファイル名を変える（既定 `development_merged.json`） |
| `--strict-conflicts` | 属性の食い違いがあれば停止する（CI 向け） |

規模は raw 約 100MB。生成に約30秒、ピークメモリ約 1.9GB。

**生成物の不変条件**（実測で確認済み）:

- `development.json` の annotation 総数 == `keep` の件数
- `file_list` のエントリ数 == 元JSON −（画像単位で落とした枚数）
- `meta_development` に元JSONの **sha256**・fingerprint・ツール版・override が入る
- **元JSONの sha256 は生成前後で変わらない**（`build-dataset` が毎回照合する）
- 統合JSONの annotation 総数 == 12本の `development.json` の keep 合計
  （採否サマリの keep 件数との差は、クロスデータセット画像重複で統合時に
  skip した分。`image_duplicates.csv` を参照）
- 統合JSONの `(inst, study, series, file_id)` は全件一意。衝突したら**止まる**
- `development_manifest.csv` の行数 == 統合JSONの画像数（`totals.files`）
- データセット別の annotation 合計 == 統合の annotation 総数
  == 12本の `development.json` の keep 合計
- 統合JSONに **file entry を1件も持たない series / study / institution は存在しない**
  （剪定は冪等。2回目で何か取れたら `build-dataset` が**止まる**）
- 剪定で **画像数と annotation 数は1件も変わらない**
  （`totals.files` / `totals.annotations` は剪定の前後で同値）
- 統合JSONの `totals.studies` == 内訳表の合計 study（食い違ったら summary に警告が出る）

### 3.10 単一症例の重畳図（debug 用）

FiftyOne を使わずに1症例だけ図で確認したいとき。

```bash
uv run python -m segmentation_validation.overlay_masks --institution kajinoki
uv run python -m segmentation_validation.overlay_masks --study CXASW00000278_002 --original
```

### 3.11 学習側へ渡す（lp-data 形式へ書き出す）

3.9 で作った統合JSONは engineer-set 形式のままなので、学習リポジトリ
[med-chest-metry-pi6](/mnt/project/chest/metry/pi6/work/nakamura/med-chest-metry-pi6) は読めない。
`export-lpdata` が lp-data 標準形式（`docs/dataset_format.md`）へ変換し、
あわせて DICOM を 16bit PNG へ変換する。

```bash
# 1) 下見。画素を読まず画像も作らないので数十秒で終わる
uv run segmentation-validation export-lpdata \
    --no-measure --image-mode none --mask-mode none \
    --dataset-name chest_metry_pi6_pneumothorax \
    --dataset-id chest_metry_pi6_pneumothorax_2026_001 --owner <あなた>

# 2) 少量で本番経路を通す（画像変換とマスク生成を含む）
uv run segmentation-validation export-lpdata --limit 200 \
    --image-mode convert --mask-mode generate ...

# 3) 全件（長時間。先に df -h /mnt/project で空きを見る）
uv run segmentation-validation export-lpdata --jobs 16 --image-mode convert ...
```

同じことは [`notebooks/lpdata_export.ipynb`](notebooks/lpdata_export.ipynb) からも実行できる
（**ロジックは持たず**、CLI と同じ `export_lpdata()` を呼ぶだけ）。

**属性の型と意味の正典は本リポジトリに無い。** med-chest-metry-pi6 の
`src/chest_metry_pi6/data/dataset_template_pneumothorax.yaml`（PR #68 で確定）を読んで
`meta.structure` をそのまま写す。コード側に structure を書き直すと、テンプレートが
更新されたときに黙って古いスキーマを出し続けるため。ずれたら起動時に落ちる。

**出力先は3つとも独立に指定できる。** 既定は `output/lpdata/<version_tag>/` 配下。

| オプション | 中身 |
| --- | --- |
| `--out` | データセットJSON（既定 `chest_metry_pi6_pneumothorax.json`） |
| `--image-output-dir` | DICOM を変換した 16bit PNG（既定 `images/`） |
| `--mask-output-dir` | 結合した気胸マスク（既定 `masks/pneumothorax/`） |

`--path-style relative`（既定）では、出力JSONの親ディレクトリ配下にあるものだけ相対パスで
記録し、原本DICOM（`/mnt/medical2`）と参照マスク（`/mnt/medicaldb`）は絶対パスのまま残す。

#### ラベルは既存 annotation と明示的な正常情報だけから作る

**読影レポートは使わない。** 後日CSVで受け取り、既存の出力を更新する独立した処理として足す想定。

| 属性 | 規則 | 実測 |
| --- | --- | ---: |
| `abnormal_finding_status` | `Findings` の geometry annotation あり→`present` / 明示の `No Findings/normal` あり→`absent` / それ以外→`unknown` | 6,680 / 853 / 35,041 |
| `pneumothorax_case` | 気胸 annotation が1件以上 | true 3,521 |
| `bulla_bleb_status` | `bulla_bleb` があれば `present`、他は `unknown`（`absent` は出さない） | present 234 |
| `pleural_effusion_status` | 胸水 annotation があれば `present` / 明示の `No Findings/normal` があり所見 annotation が0件→`absent` / それ以外→`unknown`（血胸も胸水に含む。レポート由来の明示陰性も `absent`） | 再エクスポート待ち |
| `pneumothorax_side` | 常に `null`（患者基準の解剖学的左右で、画像からは起こせない） | — |

拾った所見名（`pneumothorax` / `nodule` 等16語）は**出力属性ではない**。
テンプレートが `finding_labels` を削除したため、判定の材料と summary の語彙一覧に使うだけ。

**annotation が無いことだけを理由に `absent` にはしない。** 未アノテーションと正常例を
混同すると、未アノテーション陽性を陰性の教師信号にしてしまう。データセット名
（`ChestMetry_PI6px_normal` 等）も根拠にしない —— 同データセット 24,539件のうち明示の
`No Findings/normal` は 142件だけで、逆に `StudyAnno/abnormal` が 665件ある。

異常所見ではない geometry annotation（`Difficulty` / `Grade` / `Location` / `Body Parts` /
`FP` / `Disease`）は `present` の根拠にしない。どれを所見として採るか、どれを「明確な正常」と
認めるかは `config.lpdata_export` の2つの allowlist（`finding_code_systems` /
`normal_evidence`）で決まる。根拠が確認できたものだけを足していく。

**ラベル属性どうしは結び付けない**（med-chest-metry-pi6 のテンプレートが正典）。
`pneumothorax_case` は気胸アノテーション由来、`abnormal_finding_status` は読影所見由来で、
**由来が違うので対応を課さない**。`unknown` × `pneumothorax_case: true`（気胸ラベルは
あるが読影所見は未取得）も、`true` かつマスクが空（マスク未アノテーションの気胸症例）も
正当な組み合わせ。

前版はここを結び付けており、「`unknown` ⇒ `pneumothorax_case: false`」を強制していた。
規則どおりGTを作ると**感度が黙って下がる**ため、テンプレート側が結合を外している。

残る不変条件は値の範囲だけ（実測で違反0件）:

- `abnormal_finding_status` / `bulla_bleb_status` / `pleural_effusion_status` は
  `present` / `absent` / `unknown` のいずれか
- `pneumothorax_side` が非 null なのは `pneumothorax_case: true` のときだけ

**`lung_rect` は肺野と胸郭の両方のマスクが揃ったときだけ入る。** 片方で代用すると由来の違う
矩形が同じ属性に混ざる（テンプレートの規約）。参照マスクの実測被覆から、埋まるのは全体の
1/3以下になる。

**判断が要るものは `lpdata_export_summary.md` に出る**（allowlist に入っていない正常らしき
ラベル、気胸データセット名なのに気胸 annotation が0件のもの、所見語彙の一覧）。

**出力は必ず 16bit グレースケール PNG。** 元DICOMのうち 444件は画素が
JPEG Lossless（`1.2.840.10008.1.2.4.70`）で圧縮されており、これを読むために
`pylibjpeg` / `pylibjpeg-libjpeg` を依存に入れてある（無いと該当分だけ
`RuntimeError` で変換できず `image_file: null` になる）。**圧縮方式は元DICOM側の話**で、
書き出しは全件 `cv2.imwrite` による PNG。JPEG Lossless は可逆なので画素も失われない。

> ⚠️ 画像変換は全件で uint16 生データ 251 GiB を読み、PNG を約 127 GiB 書く。
> 既存のPNGは skip するので途中で止めても再開できるが、まず `--limit` で所要時間を測ること。
> DICOM が無い画像は `image_file: null` にして続行する（**空画像は作らない**）。
> 変換処理は med-chest-metry-pi6 の `dataprep/dicom_to_png.py` からの移植で、
> 既存PNG（med-dicom 経由）と画素値が一致するかは未確認（向こうの Issue #27）。

### 3.12 読影レポート由来のデータを足す（OFC）

大船中央の構造化読影レポートを結合した engineer-set JSON
（[ofuna_chuo_report](/mnt/project/chest/metry/pi6/work/nakamura/ofuna_chuo_report)
が出力する `engineer-set-ETR_ChestMetry_OFC_report-*.json`）を取り込む。

**価値は「マスクが無くても気胸症例だと分かる」こと。** レポートで気胸ありと
判定された 4,534 study のうち気胸マスクを持つのは 397 study しかない。
3.11 の経路は気胸 annotation の有無だけで `pneumothorax_case` を決めるので、
この差分がまるごと取りこぼされている。

3段階で実行する。**統合は2つの lp-data JSON を作ってから行う**ので、
統合前に件数と裁定結果を確認できる。

```bash
TAG=20260917; OUT=output/lpdata/$TAG
OFC=../ofuna_chuo_report/output/dataset/merged/engineer-set-ETR_ChestMetry_OFC_report-20260917_004313.json

# 1) OFC 側を lp-data 形式へ
uv run segmentation-validation export-lpdata-ofc --source "$OFC" \
    --out $OUT/ofc_report.lpdata.json --artifact-prefix ofc_ \
    --image-mode convert --jobs 16 \
    --dataset-name chest_metry_pi6_pneumothorax \
    --dataset-id chest_metry_pi6_pneumothorax_2026_003 --owner <あなた>

# 2) 統合の下見（JSONは書かない）。ここで裁定を人が確認する
uv run segmentation-validation merge-lpdata \
    --primary output/lpdata/20260916/chest_metry_pi6_pneumothorax.json \
    --secondary $OUT/ofc_report.lpdata.json \
    --out $OUT/chest_metry_pi6_pneumothorax.json --dry-run
cat $OUT/lpdata_merge_summary.md

# 3) 本番の統合
uv run segmentation-validation merge-lpdata ... （--dry-run を外す）
```

#### OFC 段はマスクを作らない（`--mask-mode` の既定が `planned`）

OFC 側の価値は「**マスクが無くても気胸症例だと分かる**」ことであって、
マスクの供給源ではない。**気胸マスクを持つ画像は例外なく development 側にもあり**、
統合は重複時に必ず primary（人手整備済みのGT）のマスクを採るので、
OFC 段で書き出した PNG は1枚も参照されない（実測273枚すべて未参照だった）。

`planned` でも**画素は読む**ので、面積・`lung_rect`・統合時のマスク食い違い検出
（6件）はそのまま効く。将来 OFC 単独のマスクが増えたら
`--mask-mode generate` を明示すればよい。

> ⚠️ **`--mask-mode generate` を OFC 段で使うなら、必ず別の `--mask-output-dir` を
> 与えること。** マスクのファイル名は `<sample_id>.png` で、development と置き場を
> 共有すると**人手で整備したGTマスクを黙って差し替える**。
> `--artifact-prefix` も同じ理由（summary / manifest / 計測キャッシュの潰し合いを防ぐ）。

#### どれが最終成果物か

| ファイル | 位置づけ |
| --- | --- |
| `<統合タグ>/chest_metry_pi6_pneumothorax.json` | **最終成果物。学習側へ渡すのはこれ** |
| `<統合タグ>/ofc_report.lpdata.json` | 中間生成物（統合の入力・OFC側） |
| `<前タグ>/chest_metry_pi6_pneumothorax.json` | 中間生成物（統合の入力・development側） |
| `lpdata_merge_summary.md` / `lpdata_merge_resolutions.json` | 統合の監査ログ（裁定577件の内訳） |
| `ofc_report_labels.csv` | certainty 保持用の分析データ（範囲外も含む全件） |
| `*_export_summary.md` / `*_manifest.json` / `*measurements.jsonl` | export の監査ログ・キャッシュ |

> ⚠️ **`--image-mode planned` で作った入力を統合したJSONは学習に使えない。**
> `image_file` が「変換予定のパス」を指すだけで実体が無い。
> `lpdata_merge_summary.md` の冒頭に**参照先が実在しない件数**が出るので、
> そこが 0 でなければ `--image-mode convert` で作り直して統合し直すこと。
>
> 中間生成物の `ofc_report.lpdata.json` は `--mask-mode planned` のとき
> `pneumothorax_mask` に実体の無いパスを持つ（273件）。これらは統合時に
> すべて primary のマスクへ差し替わるので**最終成果物には伝播しない**。

#### テンプレートとの差分（意図的なもの）

`meta.structure` は 18属性すべてキー・型・`description`・`label_map`・`defaults` が
テンプレートと**完全一致**する（丸写ししているので当然だが、ずれたら起動時に落ちる）。
`meta` の他のキーだけが意図的に違う。

| キー | 扱い |
| --- | --- |
| `split` | **書かない。** 本エクスポータは分割をしないため |
| `description` | テンプレートの文言は `split: train` 前提（「（train split）」）なので、`split` を書かない以上そのままだと矛盾する。**split に触れない文へ差し替える**（`--description` で明示指定も可） |
| `provenance` | 本エクスポータが足す（出所・裁定・`case_evidence`） |
| `dataset_name` / `dataset_id` / `owner` / `date` / `source` | 実行時に決める |

> ⚠️ **学習側 `chest_metry_pi6.data.load_dataset_file` では現状読めない。**
> あちらは `pneumothorax_mask_merged` を要求するが、テンプレート（正典）の属性名は
> `pneumothorax_mask`。テンプレート冒頭が「コード・README・テストはまだ追随していない
> （追随は Issue #52）」と明記しているとおり **med-chest-metry-pi6 側の未追随**で、
> こちらを合わせるとテンプレート違反になる。
> lp-data 標準の `lpdata.io.load_dataset` では正常に読める。

#### 取り込む範囲

既定は `pneumothorax_status` が `present` / `absent` のものだけ（実測 6,823画像）。
`unknown` の 178,096画像は**入れない** —— レポートの `pneumothorax_case: false` は
「気胸でない」ではなく「**主張していない**」の意味で、入れると未検証の陰性を
17万件ぶん教師信号にすることになる。

範囲は `config.lpdata_export.report_label_statuses` か `--report-status` で広げられる。
**除外した分も `ofc_report_labels.csv` に `in_scope: False` で残る**ので、
広げる前に確信度別の内訳を確認できる。

#### `pneumothorax_case` の根拠（`false` は2種類ある）

優先順位は **明示的な陰性/陽性GT > 読影レポート > annotation/mask が無いだけの未確認状態**。

| 根拠 | 意味 | レポートで動くか |
| --- | --- | --- |
| `explicit_positive_annotation` | 気胸 annotation / mask がある | **動かない**（降格しない） |
| `explicit_negative_normal` | `No Findings/normal` が明示されている | **動かない** |
| `explicit_negative_report` | レポートが在り、気胸含め陽性所見が無いと確認できた | 動かない（根拠のみ格上げ） |
| `unconfirmed` | 気胸 annotation / mask が無いだけ。**陰性ではない** | レポートが `present` なら `true` へ補完 |

**「明示的な陰性」と認めるのは2つだけ** —— annotation 側の `No Findings/normal` と、
「レポートが在り陽性所見が0件」。**他の所見がアノテーション済みであることは
陰性根拠にしない**（その画像について気胸を否定したのではなく、付けていないだけ）。
レポート未取得・`unknown`・ヘッジされた否定も根拠にしない。
データセット名（`*_abnormal_non_pneumothorax` 等）も従来どおり根拠にしない。

食い違ったものは**上書きせず** `lpdata_merge_resolutions.json` と summary に出る。

#### 根拠は `meta.provenance.case_evidence` で引き継ぐ

`case_evidence` はテンプレートに無い属性なのでサンプルには載せられない
（`validate_coverage` が弾く）。そのままだと統合側が根拠を見られないので、
**明示的な気胸陰性の `sample_id` を `meta.provenance.case_evidence` に列挙する**。
`export-lpdata` / `export-lpdata-ofc` の**どちらも常に書く**。

```json
"case_evidence": {
  "schema_version": 1,
  "explicit_negative_normal": ["...", ...],   // 実測 880 件
  "explicit_negative_report": ["...", ...]
}
```

> ⚠️ **`abnormal_finding_status` を気胸陰性の代理に使わないこと。**
> あれは「気胸以外も含む異常所見の有無」で、意味が違う。他の所見が併存すると
> `present` になり、明示的な正常ラベルがあっても隠れる —— 実測で明示的陰性
> 880件のうち **167件（19%）がこの形**で、代用すると保護漏れになる。

`case_evidence` を持たないJSON（この節を入れる前に作った出力）を `--primary` に
渡すと **`merge-lpdata` はエラーで止まる**。`export-lpdata` を再実行して作り直すか、
ラベル補強が不要なら `--no-enrich` を付ける。再 export は計測キャッシュと既存PNGを
再利用するので数十秒で終わる（**`--force` は付けない**）。

#### 確信度（certainty）は学習GTではない

`definite` / `probable` / `possible` / `unlikely` は**層別評価・分析のためのメタ情報**で、
**判定条件には一切使わない**。`pneumothorax_status` が `present` なら `unlikely` でも
`pneumothorax_case: true` にする（上流 `ofuna_chuo_report` の `labels/rules.py` も
「規則と certainty は分離されている」と明記しており、それに揃えている）。

最終JSONに certainty 属性は**足さない**（テンプレートに無い属性を足すと
`validate_coverage` が落ちる。これが構造的な歯止めになっている）。
代わりに捨てずに残す先が3つある。

| 出力 | 中身 |
| --- | --- |
| `ofc_report_labels.csv` | 1行1サンプル。`sample_id` / `study_name` / `pneumothorax_status` / `pneumothorax_certainty_max` / `*_certainty_counts` / `case_evidence` / `flags` ほか。**範囲外の分も含む全 184,919行** |
| `meta.provenance.report_labels` | 確信度の分布と `certainty_is_metadata: true` |
| `lpdata_merge_resolutions.json` | 裁定1件ごと。certainty は参考値で、`certainty_used_in_resolution: false` |

#### 実測（20260916 の development と統合した場合）

| 指標 | 値 |
| --- | ---: |
| OFC 取り込み対象 / うち重複 / 新規 | 6,823 / 577 / 6,246 |
| 統合後のサンプル数 | 22,931 |
| `pneumothorax_case: true` | 7,847（既存 3,358 + 補完 70 + OFC新規 4,419） |
| **うちマスク無し** | **4,489**（現行の出力では0件） |
| `pneumothorax_side` 補完 | 335 |
| `bulla_bleb_status` 補完 | 8 |
| 上書きしなかった食い違い | 3（明示的陰性2 + 降格拒否1） |
| 気胸マスクの食い違い | 6（primary を採用） |
| 不変条件違反 | 0 |

---

## 4. 成果物の読み方

### `selection_summary.md` — まずここを見る

「validationは終わったのか」「目視は終わったのか」「**開発データを作ってよいのか**」が
このファイルだけで分かるようにしてある。末尾に判定が出る:

```
## 判定

[BLOCKED] pending 251 件 / uncertain 0 件 が残っています
Development JSON を生成してよいか: **no**
```

統合JSONを作った場合は、さらに末尾に **「何が入っているか（データセット別）」** 表が付く
（[3.9](#39-開発用データセットを生成する)）。最終開発データの中身はここで分かる。

### `selection_decisions.csv` — 採否の正本

`build-dataset` はこれだけを見る（`issues.csv` も FiftyOne DB も参照しない）。

主な列:

| 列 | 意味 |
|---|---|
| `geometry_uid` | 主キー。全域で一意 |
| `patient_id` / `study` / `file_id` | 症例の所在。CSV単体で追える |
| `detected_checks` | 検出された check_id（`\|` 区切り。無ければ `none`） |
| `unverified_checks` | **検査できなかった** check_id（同上） |
| `final_decision` | `keep` / `exclude` / `pending` / `uncertain` |
| `reason` / `decision_source` | なぜ・誰が |
| `review_status` | `not_needed` / `pending` / `reviewed` |
| `kept_geometry_uid` / `duplicate_group_id` | 重複の対応関係 |
| `reviewer` / `reviewed_at` / `comment` | 人が判断したときだけ |

### `image_decisions.csv` — 画像単位の採否

`selection_decisions` は annotation 単位なので、**annotation を持たない画像は1行も持たない**。
しかし「この画像を開発データに含めるか」は別の判断が要るので、こちらを別に持つ。

| `image_class` | 画像 | 採否 | 理由 |
|---|---|---|---|
| `annotated` | 863 | keep | 採否は annotation 単位で判定済み |
| `negative_case` | 187 | keep | **正常例（No Findings）。陰性サンプルとして残す** |
| `unannotated_view` | **33** | **pending** | 側面像なら除外が必要 |
| `unannotated_orphan` | 0 | — | series 全体が未アノテーションで正常例ラベルも無い（防御） |

`build-dataset` は両方を見る。**画像が `exclude` なら file entry ごと落とす。**
これは「annotation が0件になっても file entry は残す」規則とは別で、
**画像を落とすという明示的な判断があったときだけ**母集団を変える。
画像に `pending` が残っていても既定で停止する。

第3の規則として、**画像を1枚も持たなくなった器（series / study / institution）は
統合JSONから取り除く**（[3.9](#画像0枚の器を剪定する)）。これは判断ではなく副作用の掃除で、
画像も annotation も1件も増減しない。file entry は画像が実在するから残し、
器は中身が0なら何も表していないから残さない。

### `development_manifest.csv` — 最終データに何が入ったか

`build-dataset` が統合JSONと同時に出す、**1画像=1行の明細**。
「採否がどうなったか」（上2つのCSV）ではなく、
「**結局どの画像が最終開発データに入ったのか**」に答える。

| 列 | 意味 |
|---|---|
| `dataset_id` | 由来データセット（train/test の別を失わないための軸） |
| `institution` / `patient_id` / `study_key` / `study_date` | 症例の所在 |
| `series_key` / `file_id` | 画像の所在 |
| `file_uid` | `image_decisions.csv` / `image_duplicates.csv` / lpdata の `measurements.jsonl` と**同じキー** |
| `image_path` | 元DICOMのパス（`/mnt` を基準に解決する） |
| `n_annotations` | この画像に残った annotation 件数（**0 の行もある**） |
| `lesion_classes` | 病変クラス（`Findings/001\|Findings/010` の `\|` 区切り。無ければ空欄） |

使い分け:

- **データセット別の集計** → `selection_summary.md` の内訳表（人が読む用）
- **1件ずつ追う / 好きな軸で集計する** → このCSV。
  `dataset_id` × `lesion_classes` で pivot すれば、病変クラス × データセットの全行列が出る
- **なぜこの画像が入った/入らなかったのか** → `file_uid` で `image_decisions.csv` と
  `selection_decisions.csv` へ遡る

`n_annotations == 0` の行を消していないのは意図的で、
「annotation が0件でも file entry は残す」という母集団の不変条件を明細でも保つため。
**行（file entry）は残し、器（series / study / 施設）は残さない。**
前者は画像が実在し、後者は画像が0枚 —— ここがいちばん混同しやすい。

### `precision.md` — 自動ルールの答え合わせ

`auto:`（機械の検出）と `review:`（人間の判定）を分けているので、
自動検出のうち何割が本当に不正だったかが check ごとに出る。

```
auto:s05_outside_body  50件 → 目視 → exclude 18 / keep 8   Precision 0.69
```

- **Precision が高い** = そのルールは的を射ている。自動採否の候補にできるか検討する
- **Precision が低い** = 検出の大半が正当。閾値を緩めるか、ルールごと外す
- `uncertain` が多い = 判断基準そのものが曖昧。定義を決め直す

分母は「目視済（exclude + keep + uncertain）」なので、途中でも意味のある値が出る。

### 現在の採否（目視前）

```
規模                患者 982 / study 1044 / 画像 1083
                    （annotation あり 863 / 正常例 187 / 未アノテーション 33）

total annotations   1817   (brush 1665 / bbox 151 / elliptical 1)
  keep              1465   患者 753 / study 808 / 画像 814
    no_issue_detected        984   全チェック実行して問題なし
    kept_without_full_check  481   体外判定が未実施のまま keep（未検証）
  exclude            101   患者  75 / study  88 / 画像  88   全て D01 の古い方（自動）
  pending            251   患者 177 / study 177 / 画像 177   FiftyOne 目視待ち
  uncertain            0

画像の採否          keep 1050 / pending 33
Issue 合計          1364   annotation 単位 1144 / 画像単位 220（M09）
                           severity  error 219 / warning 95 / info 1050
                           status    checked 516 / cannot_determine 509 / not_applicable 339
目視工数            251 annotation = 開く画像 177枚（＋未アノテーション 33枚は別途）
```

check 別の目視作業量。**annotation 数と画像枚数が最大2倍ちがう**ので、
工数の見積りには画像枚数を使う。

| check_id | annotation | 画像 |
|---|---|---|
| `S03_SUSPICIOUSLY_SMALL` | 121 | 105 |
| `D04_CONTAINED_DIFFERENT_LABEL` | 69 | **32** |
| `S05_OUTSIDE_BODY` | 47 | 44 |
| `M07_BBOX_DEGENERATE` | 7 | 7 |
| `D04_CONTAINED_DUPLICATE` | 6 | **3** |
| `S04_ORIGINAL_FINAL_DIVERGENCE` | 6 | 6 |
| `M07_BBOX_OUT_OF_IMAGE` | 4 | 4 |
| `D03_NEAR_DUPLICATE` | 4 | **2** |
| `S03_STRAY_COMPONENT` | 2 | 2 |
| `S03_TINY_ANNOTATION` | 1 | 1 |
| **重複を除いた合計** | **251** | **177** |

D系で差が大きいのは、重複ペアが同じ画像内の annotation 同士だから（1画像に2件そろって出る）。
除外による病変クラスの変化は `Findings/010`（気胸）915 → 814（-101）。
全て画素完全一致の重複なので実質的な損失はない。

### 何症例が合格したか

annotation の採否は**症例単位では排他にならない**。1画像に keep と pending が混在する例が
**216件**あるため、採否ごとの画像数を足すと実画像数を超える
（keep 814 + exclude 88 + pending 177 = 1079 > 863）。

そこで症例が持つ annotation 全体から状態を1つ決める。**1件でも未確定なら「要目視」**（安全側）。

| 階層 | 合格（全keep） | 一部除外して確定 | 全除外 | 要目視 | 合計 |
|---|---|---|---|---|---|
| 患者 | **553** | 67 | 0 | 177 | 797 |
| study | **599** | 81 | 0 | 177 | 857 |
| 画像 | **605** | 81 | 0 | 177 | 863 |

**全除外が0件**なのは、自動除外が完全一致の重複だけで、各ペアが同じ画像に2件そろって
出るため必ず1件が残るから。annotation を全て失う画像は発生しない。

データセット別（annotation を持つ分）:

| データセット | 患者 | study | 画像 | annotation |
|---|---|---|---|---|
| ANN_EIRLPRJ_01272 | 264 | 307 | 313 | 559 |
| ANN_EIRLPRJ_1298 | 414 | 414 | 414 | 982 |
| ETR_..._mask136 | 119 | 136 | 136 | 276 |

---

## 5. バリデーション項目一覧

### 検出された場合どうなるか（5分類）

| 分類 | 意味 | 採否への影響 |
|---|---|---|
| **自動処理** | 機械が採否を確定 | `exclude` / `decision_source=automatic` |
| **FiftyOne目視** | 人が画像を見て判断 | `pending` → 目視後に確定 |
| **記録のみ** | 記録するだけ | `keep`（`no_issue_detected`） |
| **判定不能** | 検査できなかった | `keep`（`kept_without_full_check`）＋ `unverified_checks` に記録 |

（D01 の「自動 keep」は「このチェックでは除外しない」だけで最終keepではない。他チェックの判定へ進む。）

### M系 — 機械的な整合性

| check_id | 何を確認するか | 分類 | 件数 |
|---|---|---|---|
| `M01_MASK_RESOLUTION` | マスクPNGのサイズが DICOM の Rows/Columns と一致するか | 自動処理 | 0 |
| `M01_MASK_RESOLUTION_JSON` | JSONの `series.shape` / `width,height` が DICOM と一致するか | 自動処理 | 0 |
| `M02_MASK_NOT_GRAYSCALE` | `path_mask` が単チャンネル（mode=L）か | 自動処理 | 0 |
| `M02_MASK_NOT_8BIT` | `path_mask` の dtype が uint8 か | 自動処理 | 0 |
| `M02_ORIGINAL_AMBIGUOUS_CHANNELS` | `path_original_mask` が alpha を持たない多チャンネルでないか | 自動処理 | 0 |
| `M03_MASK_NOT_BINARY` | `path_mask` の画素値が 0 と 255 だけか（**二値化前**に確認） | 自動処理 | 0 |
| `M03_MASK_INVERTED` | 前景と背景が逆になっていないか | 自動処理 | 0 |
| `M04_MASK_EMPTY` | 空マスクでないか（前景0px / 全画素同値） | 自動処理 | 0 |
| `M04_MASK_FULL` | 画像のほぼ全面が前景になっていないか（既定95%超） | 自動処理 | 0 |
| `M05_FILE_MISSING` | DICOM / マスクが存在するか | 自動処理 | 0 |
| `M05_FILE_UNREADABLE` | **存在するが開けない**か（21バイトの `Internal Server Error` 等） | 自動処理 | 0 |
| `M06_PATH_LEADING_SLASH` | パスが先頭スラッシュ付きで格納されていないか | 記録のみ | **2** |
| `M06_PATH_UNEXPECTED_ROOT` | `image_path` が `medical2/`、マスクが `annotation/` 始まりか | 記録のみ | 0 |
| `M06_PATH_UNEXPECTED_DEPTH` | `image_path` が8階層か（参照マスクのパス導出に必要） | 記録のみ | 0 |
| `M06_PATH_NAME_MISMATCH` | **マスクのファイル名が `geometry_uid` と一致するか** | 記録のみ | 0 |
| `M07_BBOX_DEGENERATE` | bbox が退化していないか（`min_x >= max_x` 等） | **目視** | **7** |
| `M07_BBOX_OUT_OF_IMAGE` | bbox が画像範囲をはみ出していないか | **目視** | **4** |
| `M07_SIZE_FIELDS_NULL` | 非brushで `width`/`height` が null（**正常。件数の記録のみ**） | 記録のみ | **152** |
| `M08_JSON_BBOX_MISMATCH` | JSONの `min/max` が実マスクの bbox と一致するか | 記録のみ | 0 |
| `M08_REGION_COUNT_MISMATCH` | `region_count` が連結成分数と一致するか | 記録のみ | 0 |
| `M09_UNANNOTATED_VIEW` | annotation を持たない画像（他ビューは済み） | **目視**（画像単位） | **33** |
| `M09_NEGATIVE_CASE` | annotation を持たないが正常例として明示されている | 記録のみ | **187** |
| `M09_UNANNOTATED_SERIES` | series 全体が未アノテーションで正常例ラベルも無い | **目視**（画像単位） | 0 |

> **M01–M05 と M08 が全て0件なのは正常。** `path_mask` は 1665/1665 が
> mode=L・uint8・値{0,255}・DICOMとサイズ一致・bbox一致・region_count一致で、
> 規定を完全に満たしている。**将来のエクスポートで壊れたときに気付くための回帰検知**として稼働中。
>
> **M01–M05 が「目視」でなく「自動処理」なのはなぜか。** 解像度が合わないマスクや
> 存在しないファイルは、人が画像を見て判断する余地が無い。とくに `M05_FILE_MISSING` は
> **画像もマスクも存在しないので FiftyOne に表示できない**。機械が確定的に
> 「使えない」と判定できるので自動 exclude にしてある（severity=ERROR かつ
> status=checked のときのみ。original マスクの INFO や判定不能は巻き込まない）。
> 実データでは0件なので、この挙動は合成データ8ケースで検証した。
>
> **M06 / M08 が「記録のみ」なのはなぜか。** パス規約違反と JSON宣言値のズレは
> 取り込み側の問題で、マスク自体の品質とは別。除外ではなく修正を依頼すべきもの。
>
> **M07 は分けている。** モジュール全体は「記録のみ」だが、座標が明確に壊れている
> `M07_BBOX_DEGENERATE` / `M07_BBOX_OUT_OF_IMAGE` の11件だけを目視に上げている
> （config は「具体的な指定が勝つ」2段階照合）。

### D系 — 重複アノテーション（形 × ラベルの2軸）

同じ画像の中の異なる `geometry_uid` を比較する。軸は2つ:

- **幾何**: 画素完全一致 / IoU ≥ 0.95 / 包含率 ≥ 0.98
- **ラベル**: `(code_system, code)` が同じか違うか

| check_id | 何を確認するか | 分類 | 件数 |
|---|---|---|---|
| `D01_EXACT_DUPLICATE` | 画素**完全一致**かつ**ラベルも同じ** | **自動処理** | **202**（=101ペア×2） |
| `D02_EXACT_MASK_LABEL_CONFLICT` | 完全一致だが**ラベルが違う** | 目視 | 0 |
| `D03_NEAR_DUPLICATE` | IoU ≥ 0.95 だが完全一致でない。ラベルは同じ | 目視 | **4**（=2ペア×2） |
| `D03_OVERLAPPING_DIFFERENT_FINDING` | IoU ≥ 0.95 で**ラベルが違う** | 目視 | 0 |
| `D04_CONTAINED_DUPLICATE` | 一方が他方にほぼ完全に包含される。ラベルは同じ | 目視 | **6**（=3ペア×2） |
| `D04_CONTAINED_DIFFERENT_LABEL` | 包含されているが**ラベルが違う** | 目視 | **72**（=36ペア×2） |

**ペアは1組につき2つの Issue を出す**（成果物の粒度を annotation に揃えるため）。
相手は `related_geometry_uids`、同じ組は `duplicate_group_id` で束ねられる。
`A≒B` かつ `B≒C` なら A/B/C が1グループになる（サイズ3のグループが実データに3件ある）。

#### D01 だけ自動採否できる理由

社内確認の結果:

> データが完全一致なのでどちらでも学習上の影響はない。日付が新しい方を使うのが自然。

なので `timestamp` が新しい方を keep、古い方を exclude する。

- `geometry_uid` の大小は使わない
- **`is_latest` は候補の絞り込みに使わない**（両方 1 でも存在し得る。実測でも brush は全件 1）。
  severity の判定にだけ使う
- `timestamp` が同値・欠損・解釈不能なら**自動決定せず目視へ回す**
  （実データでは0件だが再エクスポートでは起こり得る。合成データで検証済み）

**D01 の keep は最終keepではない。**「D01では除外されない」だけなので、
残った側が体外領域にも引っかかっていれば `pending` になる。
片方のチェックだけで採用を決めない。

#### D04 でラベルが違う72件の注意

IoU が 0.002〜0.09 と非常に低いのに包含率が 1.0。つまり**小さい所見が大きい所見の
内側にある「入れ子」**で、結節が浸潤影の中にあるといった**正当なケースが大半**の見込み。
だから severity は `info` に留め、自動除外せず目視の判断に委ねている。
Precision が低ければルールごと外せばよい。

### S系 — 疑わしいもの

| check_id | 何を確認するか | 分類 | 件数 |
|---|---|---|---|
| `S03_TINY_ANNOTATION` | annotation 全体の面積が 20mm² 未満 | 目視 | **1** |
| `S03_STRAY_COMPONENT` | **本体に付いた微小な連結成分（飛びカス）。** 20mm² 未満または全体の1%未満 | 目視 | **5** |
| `S03_SUSPICIOUSLY_SMALL` | クラス族ごとの下限を下回る、またはその3倍までのレビュー帯 | 目視 | **124** |
| `S03_NO_SPACING` | `spacing` が無く面積を mm² で評価できない | 判定不能 | 0 |
| `S04_ORIGINAL_FINAL_DIVERGENCE` | **修正前マスクを穴埋めした結果が修正後と一致しない** | 目視 | **6** |
| `S05_OUTSIDE_BODY` | 胸郭から再構成した領域の外にマスクがはみ出している | 目視 | **50** |
| `S05_REFERENCE_UNAVAILABLE` | 参照マスクが無く体外判定ができない | 判定不能 | **509** |

#### S03 が2段構えな理由

annotation 全体の面積だけを見ると、**38万pxの本体に1〜2pxの飛びカスが付いている**
ケースが絶対に見えない。実際にそういう annotation が1件ある
（morinomiyako、8621mm² の本体に 0.0225 / 0.045 / 0.045 mm² の3つ）。
だから連結成分単位でも検査する。

面積は原則 **mm²**。`spacing` が施設・機種で 0.0875〜0.2 と5.2倍ぶれるので、
px のままでは同じ画素数が施設によって5倍違う面積を指してしまう。

クラス族ごとに閾値を変えているのは、面積分布が log空間で滑らかな連続分布で
**恣意的でない切れ目が存在しない**ため。一律「100mm²未満」にすると58件挙がるが、
そのうち43件は結節（正当に小さい病変）で検出予算をそこに食われる。

| クラス族 | 対象ラベル | 下限 | レビュー帯 |
|---|---|---|---|
| `focal` | nodule 系 | 20 mm² | 20〜60 mm² |
| `localized` | atelectasis / bulla_bleb / cavity / fracture 等 | 50 mm² | 50〜150 mm² |
| `regional` | 気胸 / 胸水 / 浸潤影 / 間質影 等 | 150 mm² | 150〜450 mm² |

現在の124件は「下限未満 19件」＋「レビュー帯 105件」。
**この閾値は目視の結果で確定させる前提の暫定値。** 20mm² は仕様上の禁止値ではなく
実測分布上の外れ値（全クラスの実測最小 22.88mm² を下回り、1px と 747px の間に
747倍の空白がある、という統計的な根拠しかない）。だから error にせず自動除外もしない。

#### S04 が何を見ているか

`path_original_mask`（修正前）の輪郭を `cv2.floodFill` で穴埋めした結果を
`path_mask`（修正後）と比較する。一致すれば「修正後は修正前の塗りつぶし版」であり正常。
食い違う場合だけを目視対象にする。

**「修正前と違う → error」ではない。** 修正が正常に行われた結果である可能性が高い。

| dataset | geometry_uid | IoU | 差分 |
|---|---|---|---|
| ANN_EIRLPRJ_01272 | `eeb4e8a2…` | 0.144 | 661,739 px (85.6%) |
| ANN_EIRLPRJ_1298 | `a5b4711a…` | 0.176 | 498,097 px (82.4%) |
| ETR_..._mask136 | `04d9b185…` | 0.616 | 137,563 px (38.4%) |
| ANN_EIRLPRJ_01272 | `ae28a32b…` | 0.404 | 161,269 px (59.6%) |
| ANN_EIRLPRJ_1298 | `6c0e1706…` | 0.261 | 116,633 px (73.9%) |
| ETR_..._mask136 | `55b774ff…` | 0.99993 | 67 px（再エンコード誤差） |

#### S05 の指標と閾値

参照マスクから作った側方バンド（[6.4](#64-reference-maskthorax--lung--mediastinum)）に対して
3つの値を出す:

```
containment  マスクのうちバンド内（マージン10mm以内）にある画素の割合
outside_mm2  バンド外にある実面積
max_mm       バンド外への最大逸脱距離
```

| severity | 条件 | 件数 |
|---|---|---|
| error | `containment < 0.80` | 6 |
| warning | `containment < 0.98` かつ `outside_mm2 > 100mm²` | 20 |
| info | `containment < 1.0` | 24 |

比率だけだと巨大マスクの30mmのトゲを過小評価し、実面積だけだと巨大マスクが軒並み
引っかかるので両方を使う。「1画素でも外なら検出」は使えない（マージン0mmだと
731/1156 = 63.2% が該当）。マージン10mmの根拠は実測: 0mm=731件 / 5mm=126件 /
**10mm=50件** / 20mm=19件で、膝が5〜10mmの間にある。参照曲線は肋骨・胸膜の**内縁**を
なぞっており、気胸は壁側胸膜に接するので数mmの超過は境界誤差。

**注目所見:** warning 以上の26件はアノテータに偏りがある。

| アノテータ | 件数 |
|---|---|
| m.shinzato@hotmail.co.jp | 17 |
| kolive23@gmail.com | 5（担当14件中の5件 = 35.7%） |
| 他3名 | 各1 |

`kolive23` の2件を描画確認したところ、いずれも**気胸マスクが右鎖骨上窩から
肩・上腕の軟部組織に塗り出している**同じパターンだった。系統的な誤りの可能性がある。

---

## 6. データの中身

### 6.1 規模

| 階層 | データセット全体 | うち annotation あり |
|---|---|---|
| データセット（JSON） | 3 | 3 |
| 患者 | **982** | 797 |
| study（1回の検査） | **1044** | 857 |
| 画像 | **1083** | **863** |
| annotation | — | **1817** |

**annotation を持たない 220 画像の正体:**

| 区分 | 画像 | study | 扱い | 内容 |
|---|---|---|---|---|
| **正常例（No Findings）** | **187** | 187 | keep（陰性サンプル） | **187/187 が `No Findings/001 normal` を持ち、DICOM も実在する意図的な陰性症例** |
| **未アノテーションのビュー** | **33** | — | **pending（目視）** | アノテーション済み study の2枚目以降（末尾 `_001`/`_002`）。分類ラベルも無い |

JSON別:

| データセット | file_list | ann あり | 正常例 | 未アノテーションのビュー |
|---|---|---|---|---|
| ANN_EIRLPRJ_1298 | 590 | 414 | **176**（すべて `ofuna_chuo`） | 0 |
| ANN_EIRLPRJ_01272 | 357 | 313 | 11（`asahikawa` 7 / `okayama_chuo` 4） | **33** |
| ETR_..._mask136 | 136 | 136 | 0 | 0 |

未アノテーションのビュー33枚は**施設に強く偏っている**。

| 施設 | series | うち2枚組 | 割合 |
|---|---|---|---|
| `nagoya_daiichi` | 41 | **27** | **65.9%** |
| `asahikawa` | 230 | 5 | 2.2% |
| `okayama_chuo` | 53 | 1 | 1.9% |

**33 series すべてが「1枚目にアノテーションあり、2枚目なし」で完全に一貫**している。
`nagoya_daiichi` は2方向撮影が標準で、アノテーションは1枚目にのみ付ける運用と読める。
**側面像なら開発データから除外が必要**なので目視対象にしてある。

`patient` と `study` を別に数えているのは、**1患者が複数の study を持つ例が実在する**ため
（51患者が2件以上、最大5件）。1画像に複数の annotation があるので、
**annotation 数は目視で開く枚数ではない** —— pending の 251 annotation は **177 画像**に含まれる。

### 6.2 検証対象の病変

**この3つのデータセットは気胸だけではない。**

| データセット | brush | 気胸 | 気胸率 | 主な非気胸ラベル |
|---|---|---|---|---|
| ANN_EIRLPRJ_01272 | 491 | 320 | 65.2% | 間質性陰影 71 / ブラ・ブレブ 34 / 胸水 24 |
| ANN_EIRLPRJ_1298 | 898 | 319 | **35.5%** | 間質性陰影 209 / 胸水 131 / 結節 98 |
| ETR_..._mask136 | 276 | 276 | **100%** | — |
| **合計** | **1665** | **915** | **55.0%** | |

`mask136` だけが気胸専用。**既定は全病変を検証する。** 絞りかたは [7章](#7-設定)。

### 6.3 `path_mask` と `path_original_mask` の違い

社内確認の結果:

```
path_mask          = 修正後。開発データとして使用するもの
path_original_mask = Annotation Tool が作成した修正前
```

修正の例は「閉曲線になっていない」「消しゴムの消し残り」。

| | `path_mask` | `path_original_mask` |
|---|---|---|
| 件数 | 1665（brush 全件） | 1568（94%）。97件は無い |
| 置き場 | `annotation/<task>/<site>/<date>/mask/` | `annotation/eirl_viewer_pjt/<pjt>/<timestamp>/brush/` |
| ファイル名 | `<geometry_uid>.png` | `<geometry_uid>.png`（同じUID） |
| PIL mode | **L のみ（1665/1665）** | RGBA 1462 / L 106 |
| 画素値 | **0 / 255 の二値（例外なし）** | RGBA は alpha が 0〜255 の連続値 |
| 中身 | 塗りつぶし済み | 生のブラシストローク（輪郭のみのことがある） |

- **検証・学習には `path_mask` を使う**
- **`path_original_mask` は比較用。** 存在すること自体は異常ではない（94%が持つ）ので
  「存在する」だけでは Issue にしない。**穴埋め結果が食い違うものだけ**を S04 で拾う
- RGBA を `path_mask` と同じ条件で検査しては**いけない**。実際に M02/M03 を
  そのまま両方へ適用すると1400件超のノイズが出て本物の検出が埋まる
  （RGBA は alpha が全件存在し二値化は一意なので正常）

### 6.4 reference mask（thorax / lung / mediastinum）

体外領域チェック（S05）で「どこまでが体内か」を決めるための、**別パイプラインの生成物**。
データセットには含まれない。

```
/mnt/medicaldb/processed/lung-mask/<task>/<site>/<date>/<file_id>.png
/mnt/medicaldb/processed/thorax-mask/...
/mnt/medicaldb/processed/mediastinum-mask/...
```

`image_path` から導出する（`medical2` / split / `dcm_cr` / series の4成分を落とす）。
ルートとこの規則は config の `reference_masks` で変更できる。

**そのまま使えない。**

| 参照 | 実測の姿 | 前景率 |
|---|---|---|
| `lung-mask` | 左右の肺野（塗りつぶし） | 28.1% |
| **`thorax-mask`** | **左右の胸壁内縁をなぞる2本の細い曲線**（塗りつぶしではない） | **1.26%** |
| `mediastinum-mask` | 縦隔（塗りつぶし） | 12.3% |

- `thorax-mask` をそのまま「胸郭領域」として包含率を計算すると中央値 0.0295 になり、
  **正常なマスクまで全件が「体外」になる**
- `lung-mask` 単独も使えない。胸水・気胸は定義上、含気肺野の**外**にある
  （胸水131件中111件 = 85% が包含率 < 0.5）

そこで領域を組み直す（`datasets/pi6/outside_body.py`）:

```
rowfill(M)     各行 y について M の画素がある行を [min_x(y), max_x(y)] で埋める
THORAX_REGION  rowfill(thorax) ∪ lung ∪ mediastinum を穴埋めして最大成分
LATERAL_BAND   同じ行方向の範囲を全ての行へ外挿（上下は端の行を継承）
```

`LATERAL_BAND` は**垂直方向に無限**なので、肺尖への伸展や肋横角への胸水は罰しない。
一方で胸壁より外側 —— **上腕・肩の軟部組織へのはみ出し** —— は確実に外になる。
実測の裏付け: `THORAX_REGION` の外に出ている面積の **93.3% が側方**、下方は 6.7%。

このロジックは PI6 固有なので `datasets/pi6/` に隔離してある。

**整備状況**（毎回実測して `summary.md` に出る）:

| データセット | brushを持つファイル | 3種そろい | annotation | 判定可 |
|---|---|---|---|---|
| ANN_EIRLPRJ_01272 | 296 | **0 (0.0%)** | 491 | **0 (0.0%)** |
| ANN_EIRLPRJ_1298 | 397 | 397 (100%) | 898 | 898 (100%) |
| ETR_..._mask136 | 136 | 122 (89.7%) | 276 | 258 (93.5%) |
| **合計** | **829** | **519 (62.6%)** | **1665** | **1156 (69.4%)** |

`01272` は丸ごと未整備。除けば 519/533 = 97.4%。
欠損はファイル単位ではなく**バッチ日ディレクトリ単位**で起きている。

判定できないときの内訳:

| 理由 | annotation数 | 意味 |
|---|---|---|
| `cannot_determine:no_reference` | 500 | 参照マスクが1つも無い |
| `cannot_determine:lung_only` | 8 | thorax が無く lung だけある |
| `cannot_determine:reference_corrupt` | 1 | 存在するが壊れている（21バイトのテキスト） |
| `cannot_determine:reference_shape_mismatch` | 0 | サイズが画像と合わない（防御） |

**`lung_only` で lung ベースにフォールバックはしない。** 気胸・胸水は原理的に肺野の外に
あるので lung 基準の包含率は意味を持たない（実測で 0.00〜0.93 とノイズ）。
`reference_corrupt` を分けているのは対処が違うから（参照パイプラインの再実行が必要）。

---

## 7. 設定

既定値は `config.py`。JSONファイルで上書きし、`--set` で単発上書きする。

```bash
uv run segmentation-validation show-config              # 解決後の設定を見る
uv run segmentation-validation --set KEY=VALUE <cmd>    # 単発上書き
uv run segmentation-validation --config config/my.json <cmd>
```

存在しないキーを指定するとエラーになる（設定ミスが黙って無視されるのを防ぐため）。

### よく変えるもの

```bash
# 判定不能の509件も目視対象に加える（目視対象 251 → 約760）
--set decision_policy.cannot_determine_as_review_required=true

# 正常例187枚も目視対象に加える（「所見の見落としが無いか」まで確認したいとき）
--set 'decision_policy.review_image_classes=["unannotated_view","unannotated_orphan","negative_case"]'

# 全病変を検証する（気胸だけの絞り込みが既定なので、外すときに指定する）
--set 'validation.target_labels=[]'

# 微小領域の閾値
--set thresholds.tiny_annotation_mm2=15
--set 'thresholds.small_by_class_mm2={"focal":20,"localized":50,"regional":150}'

# 重複の閾値
--set thresholds.duplicate_iou_near=0.9
--set thresholds.duplicate_containment=0.95

# 体外領域のマージン
--set reference_masks.margin_mm=15

# 対象JSONを絞る・除外する
# 新しいデータセットの追加そのものは config 変更不要。
# datasets.sources は既定で `dataset/source/*.json` という glob なので、
# 新しい engineer-set JSON を dataset/source/ にシンボリックリンクするだけで
# 次回 scan から自動的に対象へ入る。config を触るのは絞り込み・除外したいときだけ。
--set 'datasets.exclude=["01272"]'
--set 'datasets.sources=["../../../dataset/source/*1298*.json"]'

# 参照マスクの置き場を変える
--set 'reference_masks.roots={"lung":"/path/lung","thorax":"/path/thorax","mediastinum":"/path/med"}'

# FiftyOne
--set review.dataset_name=pi6-validation
--set review.app_port=5151
--set review.database_dir=/local/disk/mongo
--set review.image_format=jpeg   # PNG(可逆)→JPEGで容量1/5。既定はPNG

# keep になった画像も含めて全1083枚を FiftyOne に載せる（約2GB / 初回25〜30分）
--set review.export_all_files=true
```

**閾値を変えたら `check` → `select` → `report` → `gui` を回す。** `scan` は不要
（計測値は閾値に依存しないので）。

### 検証対象の病変を絞る

**`code_text_eng`（例: `"pneumothorax"`）を使うこと。既定もこれ。**

`"Findings/010"` のような `(code_system, code)` 指定も文法上は使えるが、
**`(code_system, code)` の割り当てはデータセット（アノテーションツール／プロジェクト）
ごとに別の対応表を持ち、データセットを跨いで同一性を保証しない**ことが実データで
確認されている。

| データセット | `Findings/010` の意味 |
|---|---|
| `ANN_EIRLPRJ_01272` / `ANN_EIRLPRJ_1298` | 気胸（pneumothorax） |
| `ANN_EIRLPRJ_01331` / `01333` / `01334` / `01336` | 結節性陰影（nodule）。気胸は `Findings/001` |

つまり `target_labels=["Findings/010"]` は一部のデータセットでは気胸を正しく拾うが、
別のデータセットでは**結節性陰影を拾い、気胸を取りこぼす**。`code_text_eng` は
全データセットを横断して表記ゆれが無く（`code_text` の日本語表記が複数あっても
`code_text_eng` は必ず `"pneumothorax"` に統一される。実測で
「気胸（塗りつぶし）」「気胸（縁取り）」の2表記があるが両方とも `pneumothorax`）、
ツール非依存で唯一正しい指定方法。`code_text`（日本語表記そのもの）は表記揺れが
あるので受け付けない。

- **対象内は全件検証する**（部分的に検証しない）
- **対象外はチェックを一切走らせず**、`reason = out_of_scope` として1行残す。
  `no_issue_detected`（検証して問題なし）と混ぜない
- 対象外も `keep` のまま。開発データから落とすかは属性定義が固まってから決める

> **bbox / elliptical 152件に気胸ラベルは1件もない**（fracture 42 / 縦隔拡大 14 / other 82 等）。
> そのため気胸のみモードでは、座標が壊れている11件も全て対象外になる。
> **全病変モードでしか M07 の異常は拾えない**ことは意識しておく。

---

## 8. 困ったとき

| 症状 | 原因と対処 |
|---|---|
| `対象JSONが1件も見つからない` | `datasets.sources` の相対パスがずれている。`list-sources` で確認。既定は `../../../dataset/source/*.json` |
| `計測キャッシュのスキーマ版が違う` | 計測値の構造が変わった。`scan --force` で作り直す |
| **scan が異常に遅い**（数分で終わるはずが数十分） | 共有サーバーの NFS 競合。`cat /proc/loadavg` と `ps aux --sort=-%cpu \| head` で他ジョブを確認する。`--jobs` を増やしても改善しない（I/O 待ちなので逆効果）。なお OpenCV の内部スレッドは 1 に固定してある（`core/cpu.py`）—— 外すと 256コア × `--jobs` で数百スレッドになり全体が止まる |
| `issues.json が無い` | `check` を先に実行する |
| `selection_decisions.json が無い` | `select` を先に実行する |
| **`issues.json は一部のチェックだけの結果`** | `check --only` で作った部分結果。`check` を `--only` なしで回し直す（`select --allow-partial` で強行もできるが採否は信用できない） |
| **`review export していない人間の判定がある`** | `review export` してから `review build` する。捨ててよいなら `review build --discard-unexported` |
| `pending が N 件残っている` | 目視を進める。急ぐなら `--allow-pending --pending-as exclude` を**両方**指定 |
| `画像 N 枚が目視待ち` | 未アノテーションのビュー。`2-pending-images` ビューで判定する |
| `fiftyone が無い` | `uv sync --group review` |
| `FiftyOne dataset が無い` | `review build` を先に実行する |
| App が開かない | SSH トンネルが張れているか。本サーバーで `ss -ltn \| grep 5151` |
| mongod が起動しない / DBが壊れる | `FIFTYONE_DATABASE_DIR` が NFS 上にある。`findmnt -T` で確認してローカルディスクへ |
| `保存ビューを作れない` | ビュー名に ASCII が無い。FiftyOne はビュー名を slug 化するので日本語だけの名前は失敗する |
| `reviewer が未入力の判定が N 件ある` | App で `reviewer` を埋める。判定自体は有効で `unknown` として記録される |
| cv2 の挙動が変 | `opencv-python` と `opencv-python-headless` が共存している。[2章](#2-環境構築)の確認コマンド |
| DB を消してしまった | `review build` → `review import` で判定が戻る |

### 終了コード

`0` = error なし / `1` = error あり、またはゲートで停止 / `2` = ツール障害

---

## 9. 設計上の約束

コードを触るときに壊してはいけないもの。

| 約束 | なぜ |
|---|---|
| **画素ファイルを開くのは `core/measure.py` だけ** | 1665枚を1度だけ読むため。`checks/` は純関数 |
| **`checks/` は `PIL` / `pydicom` / `Path.exists` を使わない** | 同上 |
| **`import fiftyone` は `review/` の3モジュールだけ** | FiftyOne 無しで検証本体が動くため。`test_architecture.py`（AST）と `test_no_fiftyone.py`（実行時）が検査 |
| **`selection_decisions` の行数 == annotation の件数** | 「全 annotation が必ず1行」が成果物の意味そのもの。毎回 assert |
| **`image_decisions` の行数 == 画像の件数** | 同上 |
| **元JSONは変更しない** | `build-dataset` が生成前後で sha256 を照合する |
| **画像を1枚も持たない器は統合JSONに残さない** | 器を残すと `totals.studies` が中身の無い study を数えてしまう。剪定は画像を1枚も減らさないので母集団は不変。ただし**全 merge の完了後**に行う —— 先に剪定すると「片方が空」の属性食い違いを取り逃す |
| **`auto:` は機械のみ、`review:` は人間のみ** | Precision を計算できるようにするため。再構築で `auto:` は貼り直し、人間の判定は保持 |
| **FiftyOne の DB を正本にしない** | DB削除 → `review build` → `review import` で判定が戻ることを実機検証済み |
| **annotation の判定は Label、画像の判定は Sample** | 1枚に複数 annotation があるので、Sample に付けるとどれがダメか分からない |
| **元JSON形式の知識は `adapters/` だけ** | 別形式のデータセットが増えてもチェックを再利用できる |
| **`checked` / `cannot_determine` / `not_applicable` の3値** | 「問題なし」と「未検査」を混同させない |
| **ネイティブライブラリの内部スレッドは1に固定する**（`core/cpu.py`） | 並列化は `--jobs` のファイル単位で効かせる。OpenCV の既定は全コア（このサーバーは256）で、`--jobs` と掛け算になると数百スレッドになり共有サーバー全体を止める |

### ディレクトリ構成

```
src/segmentation_validation/
├── config.py         設定（対象JSON・参照マスク・全閾値・ポリシー）
├── cli.py            コマンドライン入口
├── adapters/         元JSON形式の知識。ここ以外は元JSONの階層を直接見ない
├── core/             データセット非依存の走査・計測
│   └── measure.py    ★画素ファイルを開くのはここだけ
├── datasets/pi6/     PI6固有のロジック（体外領域の領域再構成）
├── checks/           計測値を入力とする純関数。1チェック1ファイル
│   ├── machine/      M01-M09
│   ├── duplicate/    D01-D04
│   └── suspicious/   S03-S05
├── selection/        採否判断（自動判定 / 画像単位 / development.json 生成）
├── report/           issues / selection / summary / precision / dashboard
├── review/           FiftyOne（fiftyone を import するのは3モジュールだけ）
└── viz/              matplotlib による重畳図

assets/dashboard/     dashboard.html のテンプレート
tests/                pytest。合成データ / 実データ既知値 / FiftyOne往復
plan/                 設計と実測の記録
```

上の表の約束のうち、AST で機械的に検査しているものは
[tests/test_architecture.py](tests/test_architecture.py) にある
（`import fiftyone` の場所、画素I/Oの場所、`checks/` の純粋性、
元JSON形式の知識の場所、`core/` が matplotlib を引かないこと）。
docstring の約束は破られても気づけないので、守りたいものはここへ足す。

### コマンド一覧

```
list-sources / list-checks / show-config    確認
scan                                        画素の走査（キャッシュ）
check                                       チェック実行 → issues
select                                      採否確定 → selection / image decisions
report                                      summary.md / area_distribution
gui                                         dashboard.html
serve                                       dashboard.html を常駐サーバーで配信（更新ボタンつき）
review build / launch / status              目視レビュー
review export / import / precision          判定の往復と答え合わせ
review migrate-decisions                    目視判定を git 管理の正本へ移す（移行時に一度）
build-dataset                               development.json × 12 + development_merged.json
                                            + development_manifest.csv（中身の明細）
export-lpdata                               統合JSON → lp-data 形式（既存 annotation 由来のラベル）
export-lpdata-ofc                           読影レポート付き engineer-set → lp-data 形式
merge-lpdata                                2つの lp-data JSON を統合（--dry-run で裁定だけ確認）
```
