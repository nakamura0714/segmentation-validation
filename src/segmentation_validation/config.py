"""バリデーション全体の設定。

既定値をこのモジュールに持ち、JSONファイルで上書きし、``--set a.b=c`` で単発上書きする。
対象JSONパス・参照マスクのルート・全閾値をここに集約してあるので、
新しいデータセットの追加は設定の1行追加で済む。

YAMLではなくJSONにしてあるのは、閾値10個程度の設定にパーサ依存を足す必要がなく、
入力データが元々すべてJSONだから。各値の根拠はこのモジュールのコメントに書く。
"""

from __future__ import annotations

import json
import os
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DatasetsConfig:
    """検証対象のデータセットJSON。

    ``sources`` はglobでも明示リストでもよい。相対パスはプロジェクトルート基準。
    同じ形式のJSONが今後追加されるので、ここに1行足すだけで対象を増やせる。
    """

    # プロジェクトルートから見て pi6/ は3つ上。
    # segmentation-validation -> nakamura -> work -> pi6
    sources: list[str] = field(
        default_factory=lambda: ["../../../dataset/source/*.json"]
    )
    exclude: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationConfig:
    """検証の対象範囲。

    **既定は気胸のみ**（``target_labels: ["pneumothorax"]``）。全病変を検証したい
    ときは ``target_labels: []`` を明示する。

    指定方法は2通りだが、**``code_text_eng``（例: ``"pneumothorax"``）を使うこと**。
    ``"Findings/010"`` のような ``(code_system, code)`` 指定も文法上は使えるが、
    ``(code_system, code)`` の割り当ては**データセット（アノテーションツール／
    プロジェクト）ごとに別の対応表を持ち、データセットを跨いで同一性を保証しない**
    ことが実データで確認されている（例: ``Findings/010`` は一部のデータセットでは
    気胸だが、別のデータセットでは結節性陰影で、そちらでは気胸は ``Findings/001``）。
    ``code_text_eng`` は全データセットを横断して表記ゆれが無く
    （``code_text`` の日本語表記が複数あっても ``code_text_eng`` は
    ``"pneumothorax"`` に統一される）、ツール非依存で唯一正しい指定方法。

    対象外の annotation はチェックを一切走らせず、採否マスタに
    ``reason = out_of_scope`` として1行残す。黙って ``no_issue_detected`` に
    混ぜない —— 「検証して問題なし」と「検証対象外」は別物。

    例: 全病変を検証する
        "target_labels": []
    """

    target_labels: list[str] = field(default_factory=lambda: ["pneumothorax"])


@dataclass(frozen=True)
class RootsConfig:
    """JSON内の相対パスを解決する基準ルート。

    フィールドごとに基準が違う。``image_path`` は ``medical2/`` 始まりで ``/mnt`` 基準、
    ``path_mask`` / ``path_original_mask`` は ``annotation/`` 始まりで
    ``/mnt/medicaldb`` 基準。
    """

    image_root: str = "/mnt"
    annotation_root: str = "/mnt/medicaldb"


@dataclass(frozen=True)
class ReferenceMasksConfig:
    """体外領域判定に使う胸郭系マスク。

    ``drop_path_components`` は ``image_path`` から参照マスクのパスを導く規則。
    ``medical2/<task>/<split>/<site>/<date>/dcm_cr/<series>/<file_id>.dcm``
    の 0(medical2) / 2(split) / 5(dcm_cr) / 6(series) を落とすと
    ``<task>/<site>/<date>/<file_id>.png`` になる。
    """

    roots: dict[str, str] = field(
        default_factory=lambda: {
            "lung": "/mnt/medicaldb/processed/lung-mask",
            "thorax": "/mnt/medicaldb/processed/thorax-mask",
            "mediastinum": "/mnt/medicaldb/processed/mediastinum-mask",
        }
    )
    drop_path_components: list[int] = field(default_factory=lambda: [0, 2, 5, 6])
    # 3種そろわなければ cannot_determine にする。lung だけでの代替判定はしない
    # （気胸・胸水は原理的に肺野の外にあるため、lung基準の包含率は意味を持たない）。
    required: list[str] = field(
        default_factory=lambda: ["lung", "thorax", "mediastinum"]
    )
    # 参照曲線は肋骨・胸膜の内縁をなぞっており、気胸は壁側胸膜に接する。
    # 数mmの超過は境界誤差なので許容する。実測で 0mm=731件 / 5mm=126件 / 10mm=50件。
    margin_mm: float = 10.0


@dataclass(frozen=True)
class ThresholdsConfig:
    """各チェックの閾値。すべて実測分布に基づく。根拠は plan/ の該当節を参照。"""

    # --- 微小領域 (S03) ---
    # 全クラスの実測最小面積 22.88mm² を下回る値。1px と 747px の間に747倍の空白がある。
    tiny_annotation_mm2: float = 20.0
    # 飛びカスは実測 0.02-0.79mm²、正当な副成分の最小は 174.2mm²。
    # この間ならどこに引いても同じ集合になる。
    stray_component_mm2: float = 20.0
    stray_component_ratio: float = 0.01
    # 面積分布は連続なので、クラス族ごとに下限を変える。
    # focal は結節系（正当に小さい）、regional は気胸・胸水・浸潤影・間質影。
    small_by_class_mm2: dict[str, float] = field(
        default_factory=lambda: {"focal": 20.0, "localized": 50.0, "regional": 150.0}
    )
    small_review_band_factor: float = 3.0

    # --- 重複 (D03 / D04) ---
    duplicate_iou_near: float = 0.95
    duplicate_containment: float = 0.98

    # --- 体外領域 (S05) ---
    outside_body_error: float = 0.80
    outside_body_warn: float = 0.98
    # 比率だけだと巨大マスクの薄い縁が引っかかるので、はみ出し実面積も条件に入れる。
    outside_body_warn_min_mm2: float = 100.0

    # --- マスク形状 (M04) ---
    mask_full_ratio: float = 0.95


@dataclass(frozen=True)
class DecisionPolicyConfig:
    """検出結果を採否にどう繋ぐか。

    check_id をコードへ埋めずここに置くのは、判断を後から変えられるようにするため。
    """

    # 自動で採否を決めてよいもの。
    # D01 は「完全一致・同一ラベル・timestamp差あり」なので新しい方を残せる。
    # M01-M05 は解像度不一致・非二値・RGB混入・空マスク・ファイル欠損で、
    # いずれも機械が確定的に判定でき、人が画像を見て判断する余地が無い
    # （M05_FILE_MISSING は画像もマスクも存在しないので目視自体が不可能）。
    # ERROR かつ checked のものだけを自動 exclude する。
    # original マスクの INFO や判定不能は対象にしない。
    auto_decidable: list[str] = field(
        default_factory=lambda: [
            "D01_EXACT_DUPLICATE",
            # クロスデータセット重複（内容一致でtimestampが新しい方を残せる場合のみ）。
            # 同点・欠損（D05_CROSS_DATASET_DUPLICATE_TIE）や内容不一致
            # （D05_CROSS_DATASET_MISMATCH）は下の review_required に具体名で
            # 登録してあり、そちらが優先される（match_rank は具体的な指定が勝つ）。
            "D05_CROSS_DATASET_DUPLICATE",
            "M01_MASK_RESOLUTION",
            "M02_MASK_CHANNELS",
            "M03_MASK_BINARY",
            "M04_MASK_NOT_EMPTY",
            "M05_FILE_EXISTS",
        ]
    )
    # 検出されたら人間の確認へ回すもの。machine error も自動除外はしない
    # （壊れたマスクを「除外する」のか「修正を依頼する」のかは人間が決める）。
    review_required: list[str] = field(
        default_factory=lambda: [
            "D02_EXACT_MASK_LABEL_CONFLICT",
            "D03_NEAR_DUPLICATE",
            "D04_CONTAINED_DUPLICATE",
            # クロスデータセット重複のうち、自動で代表を選べなかったもの
            # （timestamp同点・欠損）と、geometry_uidが同じなのに内容が
            # 食い違うもの。どちらも自動exclude禁止で必ず人が見る。
            "D05_CROSS_DATASET_DUPLICATE_TIE",
            "D05_CROSS_DATASET_MISMATCH",
            "S03_TINY_ANNOTATION",
            "S03_STRAY_COMPONENT",
            "S03_SUSPICIOUSLY_SMALL",
            "S04_ORIGINAL_FINAL_DIVERGENCE",
            "S05_OUTSIDE_BODY",
            # M07 全体は記録のみだが、座標が明確に壊れているこの2種だけは人が見る。
            # 具体的な指定が informational の "M07_BBOX_GEOMETRY" に優先する。
            "M07_BBOX_DEGENERATE",
            "M07_BBOX_OUT_OF_IMAGE",
            # 未アノテーションのビュー。側面像なら除外が必要なので目視で判断する。
            "M09_UNANNOTATED_VIEW",
        ]
    )
    # 記録だけして採否には影響させないもの。
    informational: list[str] = field(
        default_factory=lambda: [
            "M06_PATH_FORMAT",
            "M07_BBOX_GEOMETRY",
            "M08_JSON_MASK_CONSISTENCY",
            # 非brushで width/height が null なのは正常。記録のみ。
            "M07_SIZE_FIELDS_NULL",
        ]
    )
    # 判定不能を目視対象に含めるか。現状は false。
    # true にすると参照マスクの無い501件が目視キューに加わる。
    cannot_determine_as_review_required: bool = False
    # annotation を持たない画像のうち、どの分類を目視に回すか。
    #
    #   unannotated_view    アノテーション済み study の2枚目以降（実測33枚）。
    #                       側面像なら開発データから除外が必要なので目視で判断する
    #   unannotated_orphan  series 全体が未アノテーションで正常例ラベルも無い（実測0枚）
    #   negative_case       正常例（No Findings）。実測187枚。
    #                       既定では目視に回さない —— DICOMも実在し陰性症例として
    #                       明示されているため。「所見の見落としが無いか」まで
    #                       確認したい場合はここに追加する
    review_image_classes: list[str] = field(
        default_factory=lambda: ["unannotated_view", "unannotated_orphan"]
    )


@dataclass(frozen=True)
class ReviewConfig:
    """FiftyOne 目視レビュー用の設定。"""

    # 医用レビューなので既定は可逆。jpeg にすると容量が 1/5 程度になる。
    image_format: str = "png"
    jpeg_quality: int = 92
    # 目視対象だけでなく全画像を FiftyOne に載せるか。既定は載せない ——
    # DICOM の全画素読みが1枚1〜3秒なので、1083枚だと 25〜30分 / 約2GB かかる。
    # keep になった画像も見たくなったら true にする（`--all` でも同じ）。
    export_all_files: bool = False
    dataset_name: str = "pi6-validation"
    app_port: int = 5151
    # 未設定なら $FIFTYONE_DATABASE_DIR、それも無ければ FiftyOne の既定に従う。
    # NFS 上に置くと mongod が壊れるため、ローカルディスクを指すこと。
    database_dir: str | None = None


@dataclass(frozen=True)
class GuiConfig:
    """ダッシュボードを配信する常駐サーバー（``serve``）の設定。"""

    # SSHトンネル設定を1回書けば使い回せるように固定ポートにしてある
    # （FiftyOne の 5151 と同じ理由）。README 3.5 / docs/review_procedure.md と同値。
    port: int = 8899
    host: str = "127.0.0.1"
    # ブラウザから更新（HTML再構成 / フル更新）を叩けるようにするか。
    # false にすると閲覧専用になり、POST は 403 を返す。
    allow_refresh: bool = True
    # 更新ジョブ実行中にブラウザが状態を取りに来る間隔。
    poll_interval_sec: int = 2


@dataclass(frozen=True)
class Config:
    """全設定のルート。"""

    project_root: Path = PROJECT_ROOT
    datasets: DatasetsConfig = field(default_factory=DatasetsConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    roots: RootsConfig = field(default_factory=RootsConfig)
    reference_masks: ReferenceMasksConfig = field(default_factory=ReferenceMasksConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    decision_policy: DecisionPolicyConfig = field(default_factory=DecisionPolicyConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)

    # ------------------------------------------------------------------ paths
    @property
    def output_dir(self) -> Path:
        return self.project_root / "output"

    @property
    def cache_dir(self) -> Path:
        return self.output_dir / "cache"

    @property
    def validation_dir(self) -> Path:
        return self.output_dir / "validation"

    @property
    def development_dir(self) -> Path:
        return self.output_dir / "development"

    def resolve(self, path_like: str) -> Path:
        """設定中の相対パスをプロジェクトルート基準で解決する。

        ``..`` は畳むがシンボリックリンクは辿らない。``dataset/source/`` の中身は
        共有データセットへのリンクなので、辿ってしまうと成果物に記録される出所が
        参照した側のパスと食い違う。
        """
        path = Path(path_like)
        absolute = path if path.is_absolute() else self.project_root / path
        return Path(os.path.normpath(absolute))

    def dataset_sources(self) -> list[Path]:
        """``datasets.sources`` を展開して実在するJSONの一覧を返す。

        glob と明示パスの両方を受ける。``exclude`` はファイル名の部分一致で除く。
        """
        found: list[Path] = []
        for pattern in self.datasets.sources:
            if _has_glob(pattern):
                found.extend(self._expand_glob(pattern))
            else:
                found.append(self.resolve(pattern))

        seen: dict[Path, None] = {}
        for path in found:
            resolved = Path(os.path.normpath(path))
            if any(token in resolved.name for token in self.datasets.exclude):
                continue
            seen.setdefault(resolved, None)
        return list(seen)

    def _expand_glob(self, pattern: str) -> list[Path]:
        """globを展開する。

        ``Path.glob`` はパターン内の ``..`` を扱えないので、
        先頭のglobを含まない部分をベースとして先に解決してから展開する
        （既定の ``../../../dataset/source/*.json`` がまさにこの形）。
        """
        parts = Path(pattern).parts
        first_magic = next(
            (i for i, part in enumerate(parts) if _has_glob(part)), len(parts)
        )
        base_parts, rest_parts = parts[:first_magic], parts[first_magic:]
        if not rest_parts:
            return [self.resolve(pattern)]

        base = self.resolve(str(Path(*base_parts))) if base_parts else self.project_root
        return sorted(base.glob(str(Path(*rest_parts))))


# ---------------------------------------------------------------- loading


def _has_glob(text: str) -> bool:
    return any(char in text for char in "*?[")


def _to_dict(obj: Any) -> dict[str, Any]:
    """dataclass を素の dict へ（上書きを dict 上で行うため）。"""
    result: dict[str, Any] = {}
    for f in fields(obj):
        value = getattr(obj, f.name)
        result[f.name] = _to_dict(value) if is_dataclass(value) else value
    return result


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` を ``base`` へ再帰的に重ねる。dict は要素ごとに、他は置き換え。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls: type, data: dict[str, Any]) -> Any:
    """dict から dataclass を組み立てる。

    未知のキーは弾く。設定ミスを黙って無視すると閾値が効かない事故になる。
    """
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"{cls.__name__} に未知の設定キー: {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        spec = known[name]
        # ``from __future__ import annotations`` により f.type は文字列なので、
        # 型ではなく既定値の実体を見てネストとPathを判定する。
        if spec.default_factory is not MISSING:
            default = spec.default_factory()
        elif spec.default is not MISSING:
            default = spec.default
        else:
            default = None

        if is_dataclass(default) and isinstance(value, dict):
            kwargs[name] = _build(type(default), value)
        elif isinstance(default, Path):
            kwargs[name] = Path(value)
        else:
            kwargs[name] = value
    return cls(**kwargs)


def apply_override(data: dict[str, Any], expression: str) -> dict[str, Any]:
    """``a.b.c=value`` 形式の1件を dict へ適用する。

    値は JSON として解釈し、失敗したら文字列として扱う
    （``--set review.image_format=jpeg`` をクォート無しで書けるようにするため）。
    """
    if "=" not in expression:
        raise ValueError(f"--set は key=value 形式で指定する: {expression!r}")
    key, _, raw_value = expression.partition("=")
    try:
        value: Any = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value

    parts = key.strip().split(".")
    cursor = data
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            raise ValueError(f"--set の経路が存在しない: {key!r}")
        cursor = cursor[part]
    if parts[-1] not in cursor:
        raise ValueError(f"--set のキーが存在しない: {key!r}")
    cursor[parts[-1]] = value
    return data


def load_config(path: Path | None = None, overrides: list[str] | None = None) -> Config:
    """設定を読み込む。既定値 → JSONファイル → ``--set`` の順に重ねる。"""
    data = _to_dict(Config())
    data["project_root"] = str(PROJECT_ROOT)

    if path is not None:
        data = _merge(data, json.loads(Path(path).read_text(encoding="utf-8")))

    for expression in overrides or []:
        data = apply_override(data, expression)

    return _build(Config, data)


def dump_config(config: Config) -> str:
    """現在の設定をJSONとして出力する（実行時の設定を成果物に残すため）。"""
    data = _to_dict(config)
    data["project_root"] = str(config.project_root)
    return json.dumps(data, indent=2, ensure_ascii=False)
