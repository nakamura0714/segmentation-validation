# ダッシュボードを常駐サーバー化し、更新ボタンと症例一覧を足す

## Context

いま `dashboard.html` を見る唯一の経路は [pipeline.ipynb](notebooks/pipeline.ipynb) の
cell 27 で、jupyter カーネル内に `ThreadingHTTPServer` を daemon thread で立てている。
これに3つの問題がある。

1. **jupyter から再起動できない。** サーバーの参照は `dashboard_httpd` というカーネル内
   変数にしか無い。カーネルを再起動すると参照を失い、cell 28（`shutdown()`）が動かない。
   ポートが埋まったまま notebook からは回復できない。
2. **配信先が起動時点で固定される。** `SimpleHTTPRequestHandler(directory=str(latest_validation_dir))`
   は起動時に解決したディレクトリを握る。出力先は `output/validation/<fingerprint>/` で、
   fingerprint は対象JSONの sha256/size/mtime から決まる
   （[core/cache.py:82](src/segmentation_validation/core/cache.py#L82)）。
   **データセットを1本足すと fingerprint が変わり、サーバーは古いディレクトリを配信し続ける。**
3. **更新のたびに notebook へ戻る必要がある。** FiftyOne の目視結果を反映するには
   `review export` → `select` → `report` → `gui` をセルで回さないといけない。

そこでダッシュボードを notebook から切り離して常駐プロセスにし、ブラウザ側から更新を
叩けるようにする。あわせて「exclude になった画像がどの施設か」を追えるよう、
症例（患者）単位の一覧セクションを新設する。

### 決定事項（確認済み）

- 起動は新CLI `serve` を **nohup で手動起動**して上げっぱなし。二重起動はポート占有を
  検知して PID を表示する。notebook からは HTTP サーバーのコードを消す。
- 最新の validation ディレクトリは **リクエスト時点で解決**する（再起動不要）。
- 更新ボタンは2種類。「HTML再構成のみ」（数秒）と「フル更新」
  （`review export` → `select` → `report` → `gui`、数分、進捗表示つき）。
- 症例検索は **患者単位の一覧セクションを新設**する。

---

## 1. `serve` サブコマンド

### 1.1 新モジュール `src/segmentation_validation/report/serve.py`

標準ライブラリ `http.server` のみ。外部依存を増やさない
（グラフを自作SVGで描いている方針と揃える）。
**fiftyone を import しない** ——
[tests/test_architecture.py](tests/test_architecture.py) の `FIFTYONE_ALLOWED` を守るため、
`review export` を含むフル更新は必ず `sys.executable -m segmentation_validation …` の
サブプロセスで走らせる。

```python
@dataclass
class Job:
    """更新ジョブ1回分。1プロセスに同時1つだけ。"""
    mode: str                  # "html" | "full"
    step: str                  # 進行中の段。"select" など
    started_at: str
    finished_at: str | None
    ok: bool | None
    lines: list[str]           # ログ末尾（UI表示用に最新200行だけ持つ）


class DashboardServer:
    def __init__(
        self,
        config: Config,
        renderer: Callable[[Config, Path], Path],   # cli._build_dashboard を注入
        overrides: Sequence[str],                   # フル更新の subprocess へ渡す --set
        allow_refresh: bool = True,
    ) -> None: ...

    def target_dir(self) -> tuple[Path, str]: ...   # 配信するディレクトリと選定理由
    def ensure_fresh(self, out_dir: Path) -> None: ...
    def start_job(self, mode: str) -> bool: ...     # 実行中なら False
    def status(self) -> dict[str, Any]: ...
```

`renderer` を引数で受けるのは循環 import を避けるため。payload 構築には
`cli._load_context` / `_read_issues` / `_read_selection` / `_read_image_decisions` /
`_population` が必要で、これらは cli.py にある。cli → serve の一方向依存にする。

### 1.2 エンドポイント

| method | path | 動作 |
|---|---|---|
| GET | `/` | `/dashboard.html` へ 302 |
| GET | `/dashboard.html` | 陳腐化していれば再構成してから返す（下記 1.4） |
| GET | `/api/status` | 状態JSON（下記 1.5） |
| GET | `/<name>` | 配信ディレクトリ配下の静的ファイル（`issues.csv` など） |
| POST | `/api/refresh?mode=html` | payload 再構成のみ。202 か 409（実行中） |
| POST | `/api/refresh?mode=full` | `review export`→`select`→`report`→`gui`。202 か 409 |

- GET `/<name>` は `(out_dir / name).resolve()` が `out_dir.resolve()` 配下にあることを
  検査してから開く（パストラバーサル拒否）。`review/` 配下のPNGは配信しない（不要・重い）。
- `--no-refresh` 起動時と `config.gui.allow_refresh = false` のとき POST は 403。

### 1.3 配信ディレクトリの解決（リクエストごと）

1. `config_dir = config.validation_dir / fingerprint(config)`
   —— fingerprint を毎回計算し直すので、データセット追加に自動追従する。
   実測 43MB / 9本の sha256 が 0.11秒。それでもNFSを毎回叩くのは無駄なので、
   各ソースの `(size, mtime_ns)` をキーにした memo キャッシュを serve 側に持つ。
2. `config_dir/selection_decisions.json` があればそこを配信する。
3. 無ければ `validation_dir/*/selection_decisions.json` の mtime 最大のディレクトリへ
   フォールバックし、選定理由を `/api/status` に載せて UI に出す。
   （ディレクトリ自体の mtime は上書き書き込みで更新されないことがある。
   notebook cell 24 が `summary.md` の mtime を見ているのと同じ理由。
   ただし `summary.md` は `report` を実行しないと生まれないので、
   `select` の成果物である `selection_decisions.json` を基準にする。）

### 1.4 陳腐化の自動判定

GET `/dashboard.html` のとき、`dashboard.html` の mtime が
`issues.json` / `selection_decisions.json` / `image_decisions.json` /
`review/review_decisions.json` の最大 mtime より古ければ、その場で HTML を再構成する。

→ **データセット追加＋パイプライン再実行のあとは、ブラウザを再読み込みするだけで最新になる。**

`write_dashboard`（[report/gui.py:394](src/segmentation_validation/report/gui.py#L394)）を
一時ファイル + `os.replace` に変える。生成中の 709KB を読まれる事故を防ぐ。

### 1.5 `/api/status` のレスポンス

```json
{
  "config_fingerprint": "v_ca12bdfb70e9",
  "served_fingerprint": "v_ca12bdfb70e9",
  "served_reason": "config",
  "dir": "output/validation/v_ca12bdfb70e9",
  "dashboard_mtime": "2026-09-08T14:03:11Z",
  "stale": false,
  "n_review_decisions": {"annotations": 10, "images": 33},
  "allow_refresh": true,
  "job": {"mode": "full", "step": "select", "started_at": "...",
          "finished_at": null, "ok": null, "lines": ["..."]}
}
```

`config_fingerprint != served_fingerprint` のときは UI に警告を出す。
これは下の Caveat（目視判定が fingerprint を跨がない）を見落とさないため。

### 1.6 フル更新の実行

worker thread 1本 + `threading.Lock`。各段を順に subprocess で回す。

```python
STEPS = (("review", "export"), ("select",), ("report",), ("gui",))
```

`[sys.executable, "-m", "segmentation_validation", *set_args, *step]` を
`output/dashboard_serve.log` へ追記しつつ、行を `Job.lines` にも積む。
`set_args` は serve 起動時の `--set` をそのまま引き継ぐ
（notebook の `CONFIG_OVERRIDES` 相当。現在
`datasets.exclude=["01331","01333","01334","01336","PTE_CX_MT_PI3","PTR_CX_MT_PI3"]`）。
どこかで非0終了したら `ok=False` にして中断し、UI に段名とログ末尾を出す。
`review export` は fiftyone / mongod が要るので失敗しうる —— それが正しく表示されること。

### 1.7 CLI 登録と設定

[cli.py](src/segmentation_validation/cli.py) の `build_parser()` に追加、`handlers` に `"serve": _serve`。

```python
serve = sub.add_parser("serve", help="ダッシュボードを常駐サーバーで配信する")
serve.add_argument("--port", type=int, default=None, help="既定は config の gui.port")
serve.add_argument("--host", default="127.0.0.1")
serve.add_argument("--no-refresh", action="store_true", help="更新ボタンを無効にする")
```

- `_gui()` の payload 構築部（[cli.py:503-535](src/segmentation_validation/cli.py#L503)）を
  `_build_dashboard(config, out_dir) -> Path` に切り出す。`_gui` はこれを呼ぶだけにする。
  `serve` は同じ関数を `renderer` として渡す。
- ポート占有時は `ss -ltnp "sport = :<port>"` の出力から PID を拾って表示し、
  `EXIT_FAILURE` で終わる（黙って落ちない）。`allow_reuse_address = True` で TIME_WAIT を回復。
- [config.py](src/segmentation_validation/config.py) に `GuiConfig` を追加
  （`ReviewConfig.app_port = 5151` の書き方に倣う）。

```python
@dataclass(frozen=True)
class GuiConfig:
    """ダッシュボード常駐サーバーの設定。"""
    # SSHトンネル設定を使い回せるよう固定。notebook / docs と同じ値。
    port: int = 8899
    allow_refresh: bool = True
    poll_interval_sec: int = 2
```

## 2. フロント側（`assets/dashboard/`）

- [shell-body.html](assets/dashboard/shell-body.html) のヘッダ右列
  （`.top-in` の `theme-btn` / `.verdict` がある側）に `<div id="refresh" hidden>` を追加。
  「HTML再構成」「フル更新」の2ボタン、状態行、`最終生成 <時刻>`、fingerprint 不一致警告。
- [app.js](assets/dashboard/app.js) 起動時に `fetch("/api/status")` を試し、成功したときだけ
  `#refresh` を表示する。`file://` で直接開いた場合は隠したまま
  —— **dashboard.html 単体で開いても動く**という README の約束を守る。
- フル更新は確認ダイアログ（数分かかる／FiftyOne と mongod に触る旨）を出す。
- ジョブ実行中は `poll_interval_sec` ごとに `/api/status` をポーリングして `step` を表示し、
  `finished_at` が入って `ok` が真なら `location.reload()`。偽ならログ末尾を出して止まる。

## 3. 症例（患者）単位の一覧セクション

### 3.1 payload

[report/gui.py](src/segmentation_validation/report/gui.py) に追加し、`build_payload` の
戻り値に `"case_rows"` として載せる。

```python
def case_rows(
    decisions: Sequence[SelectionDecision],
    image_decisions: Sequence[Any],
) -> dict[str, Any]:
    """患者1人 = 1行。exclude がどの施設に出ているかを追えるようにする。"""
```

1行の内容: `institution` / `dataset_id` / `patient_id` / study数 / 画像数 /
annotation数 / keep / exclude / pending / uncertain / 症例状態 / 除外理由の集合 /
画像単位の exclude 数。

- 症例状態は [`case_status()`](src/segmentation_validation/selection/decisions.py#L484) を再利用する
  （JS 側で再実装しない。`countCases` の二重実装が既にあるので、これ以上増やさない）。
- annotation を持たない患者も `image_decisions` から拾う（状態は `NO_ANNOTATION`）。
  `image_decisions.csv` の exclude 33枚がここに出ることが目的。
- 文字列は既存の `_Vocab` で圧縮する。実測 982患者 × 20施設なので payload 増は数十KB
  （現状 709KB）に収まる。

### 3.2 セクション

`shell-body.html` に `<section id="sec-cases">` を追加（`#sec-images` の直後）。
既存 `.filters` と `.tbl-scroll` のパターンを流用する。

- 絞り込み: 施設 / 症例状態 / データセット の `<select>` と `patient_id` 検索
- 列: 施設 / patient_id / study数 / 画像 / annotation / keep / **exclude** / 要目視 / 状態
- 既定の並びは exclude 数の降順（「どの施設に exclude が出ているか」が先に見えるように）
- 行クリックでその患者の annotation と画像の内訳を開く
  （`annRow` の detail 展開パターンを流用）
- 表の下に施設別の集計行（施設ごとの患者数 / exclude を含む患者数）

### 3.3 annotation 表の検索に patient_id を足す

[app.js:413-415](assets/dashboard/app.js#L413) の `matches()` の検索対象に `r[R.PAT]` を追加し、
`#f-q` の placeholder を `patient / geometry_uid / study / file で検索` に変える。
症例セクションから annotation 表へ落ちてくる導線として必要。

## 4. notebook

[pipeline.ipynb](notebooks/pipeline.ipynb)

- cell 26（markdown）を書き換え。常駐サーバーの運用（nohup 起動、SSHトンネル、
  更新ボタンの使い方、止め方）にする。既存の `~/.ssh/config` / `LocalForward` の
  説明は有用なので残す。
- cell 27（サーバー起動）→ **HTTPサーバーを立てるコードを削除**し、
  `urllib.request` で `http://localhost:8899/api/status` を叩いて
  「起きている → リンクを表示」「落ちている → nohup 起動コマンドを表示」にする。
- cell 28（`shutdown()`）→ 削除。

## 5. docs

- README 3.5 に `serve` の節を追記。「コマンド一覧」（README:1139 付近）に1行足す。
- [docs/review_procedure.md](docs/review_procedure.md) §4 の手動
  `python -m http.server 8899` 手順を `serve` に差し替える。
- この計画をリポジトリの慣習どおり `plan/dashboard-serve_2026-09-08.md` にも残す
  （既存の `plan/segmentation-validation-tool_2026-09-04.md` と同じ命名）。

## Caveat（実装中に必ず突き当たる。今回の範囲外だが表示で気づけるようにする）

fingerprint が変わると `output/validation/<fp>/review/review_decisions.json` も
新しい空ディレクトリを指す（`_review_dir()`）。
**データセットを1本足すと過去の目視判定が引き継がれない。**
救済は `select --review-decisions <旧fpのパス>` のみで、README/docs に手順の記述が無い。
`/api/status` に `config_fingerprint` / `served_fingerprint` / `n_review_decisions` を
出して取り違えに気づけるようにする。引き継ぎ自体の自動化は別タスク。

## 検証

1. `uv run pytest` が通る（合成データのみ・数秒）。
   `tests/test_architecture.py` が新モジュールにも走り、fiftyone を import していないこと。
2. 新テスト `tests/test_serve.py`（合成データ・ネットワーク不要）:
   ルーティング、パストラバーサル拒否、陳腐化判定、ジョブの排他（409）、`--no-refresh` で 403。
3. 新テスト `tests/test_gui_cases.py`: `case_rows()` の集計
   —— annotation なし患者、keep/exclude 混在患者、複数 study を持つ患者。
4. `uv run segmentation-validation serve --port 8899` を前景で起動して手で確認:
   - `curl -s localhost:8899/api/status | jq` に fingerprint と dir が出る
   - `curl -o /dev/null -w '%{http_code}' localhost:8899/dashboard.html` → 200
   - `curl 'localhost:8899/../../CLAUDE.md'` → 403/404（配下限定）
   - `touch output/validation/<fp>/selection_decisions.json` → 再 GET で自動再構成が走り、
     `dashboard.html` の mtime が更新される
   - `curl -X POST 'localhost:8899/api/refresh?mode=html'` → 202、`/api/status` が running → ok
   - `curl -X POST 'localhost:8899/api/refresh?mode=full'` → `output/dashboard_serve.log` に
     `review export` 以降が流れる（fiftyone 未同期なら失敗が UI に出ることを確認）
   - もう1つ `serve` を起動して、PID 付きエラーで `EXIT_FAILURE` になる
5. `file://` で `dashboard.html` を直接開き、更新パネルが出ないこと。
6. 症例セクション: 施設で絞ると exclude を持つ患者が並ぶ。patient_id で検索できる。
   `image_decisions.csv` の exclude 33枚が症例表に現れる（現行データでの実測値と照合）。
7. `uv run ruff check` / `uv run ruff format --check`。
