# ダッシュボードのテンプレート

`segmentation-validation gui` がこの4ファイルと検証結果を組み立てて
`output/validation/<fingerprint>/dashboard.html` を作る。

- `shell-head.html`  `<title>` / Google Fonts の読み込み / CSS トークン
- `shell-body.html`  静的なマークアップと用語解説
- `app.js`           フィルタと表の描画
- `charts.js`        SVGの図（外部ライブラリを使わない）

データは `report/gui.py` が JSON にして `<script type="application/json">` へ埋める。
Artifact として公開する際は CSP の都合で外部読み込みが fonts.googleapis.com に限られる。
