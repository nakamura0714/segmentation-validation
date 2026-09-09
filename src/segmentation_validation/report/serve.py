"""``dashboard.html`` を配信する常駐サーバー。

**なぜ常駐させるのか。** 以前は ``notebooks/pipeline.ipynb`` のセルが jupyter
カーネル内に ``ThreadingHTTPServer`` を立てていた。これには2つの構造的な問題が
あった。

1. サーバーの参照がカーネル内変数にしか無いので、**カーネルを再起動すると
   止められなくなる**。ポートが埋まったまま notebook からは回復できない。
2. ``SimpleHTTPRequestHandler(directory=...)`` は**起動時点の**ディレクトリを握る。
   出力先は ``output/validation/<fingerprint>/`` で、fingerprint は対象JSONの
   sha256/size/mtime から決まる（:func:`..core.cache.fingerprint`）。
   データセットを1本足すと fingerprint が変わり、古いディレクトリを配信し続ける。

そこで notebook から切り離し、**配信ディレクトリをリクエストごとに解決する**
プロセスとして常駐させる。データセットを足しても再起動が要らない。

**外部依存を足さない。** flask も uvicorn も使わず標準ライブラリの ``http.server``
だけで書く。グラフを外部ライブラリなしのSVGで描いているのと同じ理由で、
ダッシュボードを見るためだけに依存を増やしたくない。

**fiftyone を import しない。** ``review export`` を含むフル更新は必ず
サブプロセス（``python -m segmentation_validation …``）で走らせる。
``tests/test_architecture.py`` が「fiftyone を import してよいのは review/ の
3モジュールだけ」を AST で機械検査しており、その約束を壊さないため。
"""

from __future__ import annotations

import http.server
import json
import logging
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Config

logger = logging.getLogger(__name__)

#: フル更新で回す段と、**成功とみなす exit code**。
#:
#: ``scan`` と ``check`` が要る。無いと fingerprint が変わった直後は
#: ``select`` が「issues.json が無い」で必ず落ち、**ボタンでは絶対に解消
#: できない**（実際にそうなっていた）。``scan`` は走査済みを飛ばすので
#: 通常は1秒未満で終わり、新しい構成のときだけ時間がかかる。
#:
#: ★``check`` の 1 を許すのが必須。error severity があれば ``EXIT_ISSUES=1``
#: を返す仕様で、実データには error が204件ある。1 を失敗扱いにすると
#: 連鎖が毎回 check で止まる。``select`` の 1（部分的な issues）は逆に
#: 止めなければならない。
FULL_STEPS: tuple[tuple[tuple[str, ...], frozenset[int]], ...] = (
    (("scan",), frozenset({0})),
    (("check",), frozenset({0, 1})),
    (("review", "export"), frozenset({0})),
    (("select",), frozenset({0})),
    (("report",), frozenset({0})),
    (("gui",), frozenset({0})),
)

#: 再生成に要る成果物。1つでも欠ければ ``dashboard.html`` は作り直せない。
REQUIRED_FOR_REBUILD = (
    "issues.json",
    "selection_decisions.json",
    "image_decisions.json",
)

#: 配信ディレクトリの中で「これより dashboard.html が古ければ作り直す」対象。
#: ``review/review_decisions.json`` を含めるのは、目視結果の反映漏れが
#: いちばん起きやすいため。
SOURCE_ARTIFACTS = (
    "issues.json",
    "selection_decisions.json",
    "image_decisions.json",
    "review/review_decisions.json",
)

#: 配信を許すファイル名。``review/`` 配下のPNG（391MB）は載せない。
SERVABLE = {
    "dashboard.html",
    "issues.csv",
    "issues.json",
    "selection_decisions.csv",
    "selection_decisions.json",
    "image_decisions.csv",
    "image_decisions.json",
    "summary.md",
    "precision.md",
}

#: UIに出すログの行数。フル更新は数百行出るので末尾だけ持つ。
LOG_TAIL = 200

Renderer = Callable[[Config, Path], Path]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Writability:
    """更新してよいか。だめなら理由を持つ（そのままUIに出す）。

    **fingerprint 一致だけでは足りない。** 一致していても計測が欠けていれば、
    再生成は「面積も包含率も空のダッシュボード」を作って正常な成果物を
    上書きする。別構成を混ぜるのと同じ静かな劣化になる。

    2つに分けているのは前提が違うから。フル更新は ``scan``→``check``→``select``
    で足りないものを自分で作るので、成果物が無くても走ってよい（ここを塞ぐと
    新しい構成から復旧できなくなる）。HTML再構成は既にある成果物を読むだけなので、
    揃っていて整合していることが前提。
    """

    can_rebuild_html: bool
    can_run_full: bool
    blockers: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "can_rebuild_html": self.can_rebuild_html,
            "can_run_full": self.can_run_full,
            "blockers": list(self.blockers),
        }


@dataclass
class Job:
    """更新ジョブ1回分。1プロセスに同時1つだけ走る。"""

    mode: str  # "html" | "full"
    step: str
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    ok: bool | None = None
    lines: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-LOG_TAIL]

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "step": self.step,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "lines": list(self.lines),
        }


class DashboardServer:
    """配信ディレクトリの解決と更新ジョブの実行を持つ。

    HTTPの都合（ルーティング・ステータスコード）は :class:`_Handler` 側にある。
    """

    def __init__(
        self,
        config: Config,
        renderer: Renderer,
        overrides: Sequence[str] = (),
        allow_refresh: bool = True,
        pinned: str | None = None,
    ) -> None:
        self.config = config
        self.renderer = renderer
        self.overrides = list(overrides)
        self.allow_refresh = allow_refresh
        # 明示的に選ばれた fingerprint。プロセス内のメモリだけに持つ。
        self._pinned = pinned or None
        self.log_path = config.output_dir / "dashboard_serve.log"
        self._job: Job | None = None
        self._job_lock = threading.Lock()
        # 再構成そのものの排他。陳腐化による自動再構成と更新ボタンが
        # 同時に走ると同じファイルを2回書くので直列化する。
        self._render_lock = threading.Lock()
        self._fingerprint_memo: tuple[Any, str] | None = None

    # ------------------------------------------------------------ 配信先の解決

    def fingerprint(self) -> str:
        """現在の config が指す fingerprint。

        毎リクエスト計算し直すのでデータセット追加に自動追従する。sha256 は
        43MB/9本で0.11秒だが、NFS を毎回読むのは無駄なので各ソースの
        ``(name, size, mtime_ns)`` が変わらない限り前回の結果を使う。
        """
        from ..core.cache import fingerprint as compute

        stats: list[tuple[str, int, int]] = []
        for path in self.config.dataset_sources():
            if not path.exists():
                continue
            stat = path.stat()
            stats.append((path.name, stat.st_size, stat.st_mtime_ns))
        key = tuple(sorted(stats))
        if self._fingerprint_memo is not None and self._fingerprint_memo[0] == key:
            return self._fingerprint_memo[1]
        value = compute(self.config)
        self._fingerprint_memo = (key, value)
        return value

    def pinned(self) -> str | None:
        return self._pinned

    def pin(self, fingerprint: str | None) -> None:
        """配信する fingerprint を明示する。``None`` で自動（config に従う）へ戻す。"""
        self._pinned = fingerprint or None

    def target_dir(self) -> tuple[Path | None, str]:
        """配信するディレクトリと、そう選んだ理由を返す。

        1. pin されていればそこ。**フォールバックしない** —— 選んだものが
           見えないなら選択の意味が無いので、無ければ 503 で理由を出す
        2. config の fingerprint が指す先に ``select`` の成果物があればそこ
        3. 無ければ ``selection_decisions.json`` の mtime が最新のところへ

        ディレクトリ自体の mtime は、既存ファイルを上書きするだけの ``select``
        では更新されないことがあるので見ない。
        """
        if self._pinned:
            target = self.config.validation_dir / self._pinned
            return (target if target.exists() else None), "pinned"

        expected = self.config.validation_dir / self.fingerprint()
        if (expected / "selection_decisions.json").exists():
            return expected, "config"

        candidates = [
            path.parent
            for path in self.config.validation_dir.glob("*/selection_decisions.json")
        ]
        if not candidates:
            return (expected if expected.exists() else None), "missing"
        newest = max(
            candidates, key=lambda p: (p / "selection_decisions.json").stat().st_mtime
        )
        return newest, "newest"

    # --------------------------------------------------------- 更新してよいか

    def writability(self) -> Writability:
        """いま更新してよいかを、理由つきで返す。"""
        from .validation_meta import read_meta

        out_dir, reason = self.target_dir()
        config_fp = self.fingerprint()
        blockers: list[str] = []

        if not self.allow_refresh:
            return Writability(
                False, False, ("閲覧専用で起動している（--no-refresh）",)
            )

        if reason == "missing" or out_dir is None:
            # まだ何も無い。データセットを足した直後がこの状態で、**ここを
            # 塞ぐと復旧手段が無くなる**（フル更新が scan→check→select で
            # 作る）。再生成は読むものが無いのでできない。
            return Writability(
                can_rebuild_html=False,
                can_run_full=True,
                blockers=(
                    f"この設定（{config_fp}）の成果物がまだ無い。"
                    "フル更新（scan → check → select）で作れる",
                ),
            )

        # フル更新は config の fingerprint に書く。別構成を見ている状態で
        # 押すと「押したのに表示が変わらない」になるので、揃っているときだけ。
        served_fp = out_dir.name
        if served_fp != config_fp:
            blockers.append(
                f"表示中の {served_fp} は現在の設定 {config_fp} と"
                "別の構成。混ぜて再生成しないため読み取り専用"
            )
            return Writability(False, False, tuple(blockers))
        missing = [n for n in REQUIRED_FOR_REBUILD if not (out_dir / n).exists()]
        if missing:
            blockers.append(f"成果物が無い: {', '.join(missing)}")

        meta = read_meta(out_dir)
        if meta is not None and meta.fingerprint and meta.fingerprint != served_fp:
            blockers.append(
                f"成果物の meta が別の fingerprint（{meta.fingerprint}）を指している"
            )

        measurements = meta.measurements if meta else None
        if measurements is not None:
            if not measurements.usable:
                blockers.append(
                    f"計測キャッシュが足りない（{measurements.shortfall()}）"
                )
        elif self._scan_never_ran():
            # meta.json が無い古い出力。**「問題なし」と解釈しない。**
            # ただし対象ファイル数は元JSONを読まないと分からず、状態表示の
            # たびに読むのは重い。「1件も走査していない」ことだけは安く確実に
            # 言えるので、そこだけを見る（部分走査は meta.json を持つ新しい
            # 出力で判定できる）。
            blockers.append("計測キャッシュが1件も無い（scan していない）")

        return Writability(
            can_rebuild_html=not blockers,
            can_run_full=True,  # 足りないものは連鎖が作る
            blockers=tuple(blockers),
        )

    def _cache_files_path(self) -> Path:
        """この config の計測キャッシュの files.jsonl。

        ``cache_for(config)`` を使わずに ``self.fingerprint()`` から組むのは、
        fingerprint の算出をこのクラスの1箇所に寄せるため（メモ化も効く）。
        """
        from ..core.cache import FILES_JSONL

        return self.config.cache_dir / self.fingerprint() / FILES_JSONL

    def _scan_never_ran(self) -> bool:
        """この config の計測キャッシュが空か。判定できなければ偽（止めない）。"""
        try:
            return not self._cache_files_path().exists()
        except Exception as error:  # 判定できないなら既存運用を壊さない
            logger.debug("計測キャッシュを読めない: %s", error)
            return False

    def is_stale(self, out_dir: Path) -> bool:
        """``dashboard.html`` が成果物より古いか。"""
        dashboard = out_dir / "dashboard.html"
        if not dashboard.exists():
            return True
        built = dashboard.stat().st_mtime
        return any(
            (out_dir / name).exists() and (out_dir / name).stat().st_mtime > built
            for name in SOURCE_ARTIFACTS
        )

    def ensure_fresh(self, out_dir: Path) -> None:
        """陳腐化していれば作り直す。GET のついでに呼ぶ。

        これがあるので、データセット追加＋パイプライン再実行のあとは
        **ブラウザを再読み込みするだけ**で最新になる。

        ★**再生成してよい状態のときだけ**。以前はここが無条件で、
        別構成を配信中でも ``renderer(self.config, out_dir)`` を呼んでいた。
        その結果「12本構成の採否に6本構成の母集団と（存在しない）計測値を
        貼った混成HTML」を作り、正常な成果物を上書きしていた。
        fingerprint が一致していても計測が欠けていれば、面積・包含率が空の
        HTMLで正常な成果物を潰す。どちらも何もしない。
        """
        writable = self.writability()
        if not writable.can_rebuild_html:
            logger.debug(
                "再生成しない（%s）: %s", out_dir, "; ".join(writable.blockers)
            )
            return
        with self._render_lock:
            if not self.is_stale(out_dir):
                return
            logger.info("dashboard.html が成果物より古い。作り直す: %s", out_dir)
            self.renderer(self.config, out_dir)

    # ---------------------------------------------------------------- 更新ジョブ

    def job(self) -> Job | None:
        return self._job

    def start_job(self, mode: str) -> bool:
        """更新ジョブを始める。すでに走っていれば ``False``。"""
        with self._job_lock:
            if self._job is not None and self._job.finished_at is None:
                return False
            self._job = Job(mode=mode, step="準備中")
            job = self._job
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return True

    def _run_job(self, job: Job) -> None:
        try:
            if job.mode == "html":
                self._run_html(job)
            else:
                self._run_full(job)
        except Exception as error:  # サーバーを落とさない。UIへ出して終わる
            logger.exception("更新ジョブが失敗した")
            job.say(f"失敗: {error}")
            job.ok = False
        finally:
            job.step = "完了" if job.ok else "失敗"
            job.finished_at = _now()

    def _run_html(self, job: Job) -> None:
        out_dir, _ = self.target_dir()
        if out_dir is None:
            raise FileNotFoundError(
                "配信できる成果物が無い。先に check / select を実行する"
            )
        job.step = "gui"
        job.say(f"dashboard.html を再構成する: {out_dir}")
        with self._render_lock:
            target = self.renderer(self.config, out_dir)
        job.say(f"完了 {target.stat().st_size / 1024:.0f} KB -> {target}")
        job.ok = True

    def _run_full(self, job: Job) -> None:
        """``scan`` から ``gui`` まで順に回す。

        サブプロセスで走らせるのは、``review export`` が fiftyone を import する
        から。このモジュールが直接呼ぶと層の約束が壊れる。

        **段ごとに許容 exit code が違う**（``FULL_STEPS`` 参照）。``check`` は
        error を見つけると 1 を返すのが正常なので、そこで止めてはいけない。
        """
        set_args: list[str] = []
        for override in self.overrides:
            set_args += ["--set", override]

        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n===== フル更新 {job.started_at} =====\n")
            for step, accepted in FULL_STEPS:
                argv = list(step) + self._step_options(step)
                name = " ".join(step)
                job.step = name
                job.say(f"$ segmentation-validation {' '.join(set_args + argv)}")
                log.write(f"--- {name} ---\n")
                log.flush()
                code = self._run_step(argv, set_args, job, log)
                if code not in accepted:
                    job.say(f"`{name}` が exit {code} で終わった。ここで中断する。")
                    job.ok = False
                    return
                if code:
                    job.say(f"（`{name}` は exit {code}。想定内なので続ける）")
        job.ok = True

    def _step_options(self, step: tuple[str, ...]) -> list[str]:
        """段ごとの追加引数。設定から取れるものはここで足す。"""
        if step == ("scan",):
            return ["--jobs", str(self.config.gui.scan_jobs)]
        return []

    def _run_step(
        self, step: Sequence[str], set_args: Sequence[str], job: Job, log: Any
    ) -> int:
        command = [
            sys.executable,
            "-m",
            "segmentation_validation",
            *set_args,
            *step,
        ]
        process = subprocess.Popen(
            command,
            cwd=self.config.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            log.write(line + "\n")
            job.say(line)
        log.flush()
        return process.wait()

    # -------------------------------------------------------------------- 状態

    def status(self) -> dict[str, Any]:
        out_dir, reason = self.target_dir()
        dashboard = (out_dir / "dashboard.html") if out_dir else None
        job = self._job
        return {
            "config_fingerprint": self.fingerprint(),
            "served_fingerprint": out_dir.name if out_dir else None,
            "served_reason": reason,
            "dir": str(out_dir) if out_dir else None,
            "dashboard_mtime": (
                datetime.fromtimestamp(
                    dashboard.stat().st_mtime, timezone.utc
                ).isoformat(timespec="seconds")
                if dashboard and dashboard.exists()
                else None
            ),
            "stale": self.is_stale(out_dir) if out_dir else True,
            "review_decisions": _review_counts(out_dir),
            "allow_refresh": self.allow_refresh,
            "poll_interval_sec": self.config.gui.poll_interval_sec,
            "pinned": self._pinned,
            "n_sources": len(self.config.dataset_sources()),
            "served_n_sources": _n_sources(out_dir),
            "missing_artifacts": self._missing_artifacts(),
            "writability": self.writability().as_dict(),
            "job": job.as_dict() if job else None,
        }

    def _missing_artifacts(self) -> list[str]:
        """いまの設定で作られていない成果物。UIに「何が足りないか」を出すため。

        以前は「select の成果物が無い」としか言わなかったので、``select`` だけが
        足りないように読めた。実際は ``scan`` と ``check`` も要る。
        """
        out_dir = self.config.validation_dir / self.fingerprint()
        missing: list[str] = []
        if self._scan_never_ran():
            missing.append("走査キャッシュ（scan）")
        for name, made_by in (
            ("issues.json", "check"),
            ("selection_decisions.json", "select"),
            ("image_decisions.json", "select"),
            ("dashboard.html", "gui"),
        ):
            if not (out_dir / name).exists():
                missing.append(f"{name}（{made_by}）")
        return missing

    def fingerprint_options(self) -> list[dict[str, Any]]:
        """選べる fingerprint の一覧。

        構成名は ``output/cache/<fp>/manifest.json``（数KB）と ``meta.json``
        から読む。``issues.json`` は実データで60MBあるので一覧のために開けない。
        """
        from .validation_meta import read_meta

        config_fp = self.fingerprint()
        options: list[dict[str, Any]] = []
        root = self.config.validation_dir
        if not root.exists():
            return options
        for out_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            meta = read_meta(out_dir)
            sources = (
                list(meta.sources)
                if meta and meta.sources
                else _cache_sources(self.config, out_dir.name)
            )
            selection = out_dir / "selection_decisions.json"
            options.append(
                {
                    "fingerprint": out_dir.name,
                    "is_config": out_dir.name == config_fp,
                    "n_sources": len(sources) if sources is not None else None,
                    "sources": sources,
                    "artifacts": {
                        name: (out_dir / name).exists()
                        for name in ("issues.json", "dashboard.html")
                    }
                    | {"selection_decisions.json": selection.exists()},
                    "mtime": (
                        datetime.fromtimestamp(
                            selection.stat().st_mtime, timezone.utc
                        ).isoformat(timespec="seconds")
                        if selection.exists()
                        else None
                    ),
                    "review_decisions": _review_counts(out_dir),
                }
            )
        # 選べるもの（select 済み）を先に、その中で新しい順。
        options.sort(
            key=lambda o: (
                not o["artifacts"]["selection_decisions.json"],
                o["mtime"] or "",
            ),
            reverse=False,
        )
        return options


def _cache_sources(config: Config, fingerprint: str) -> list[str] | None:
    """走査キャッシュの manifest から対象JSON名を読む。無ければ ``None``。"""
    path = config.cache_dir / fingerprint / "manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return [str(s.get("name") or "") for s in (data.get("sources") or [])]


def _n_sources(out_dir: Path | None) -> int | None:
    """配信中の成果物が何本のデータセットで作られたか。"""
    if out_dir is None:
        return None
    from .validation_meta import read_meta

    meta = read_meta(out_dir)
    return meta.n_sources if meta and meta.sources else None


def _review_counts(out_dir: Path | None) -> dict[str, int] | None:
    """配信中のディレクトリが持つ人間の判定の件数。

    fingerprint が変わると ``review/review_decisions.json`` も新しい空ディレクトリ
    を指すため、**データセットを1本足すと過去の目視判定が引き継がれない**。
    この件数をUIに出しておくと、0件になっていることに気づける。
    """
    if out_dir is None:
        return None
    path = out_dir / "review" / "review_decisions.json"
    if not path.exists():
        return {"annotations": 0, "images": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "annotations": len(payload.get("decisions") or []),
        "images": len(payload.get("image_decisions") or []),
    }


class _Handler(http.server.BaseHTTPRequestHandler):
    """ルーティングだけを持つ。判断は :class:`DashboardServer` 側。"""

    server_version = "segmentation-validation-dashboard"
    dashboard: DashboardServer  # ``_make_handler`` が差し込む

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    # ------------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802  http.server の規約
        path, _, _ = self.path.partition("?")
        path = urllib.parse.unquote(path)

        if path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/dashboard.html")
            self.end_headers()
            return
        if path == "/api/status":
            self._send_json(200, self.dashboard.status())
            return
        if path == "/api/fingerprints":
            self._send_json(200, {"options": self.dashboard.fingerprint_options()})
            return

        name = path.lstrip("/")
        if name not in SERVABLE:
            self._send_text(404, f"配信していない: {name}")
            return

        out_dir, reason = self.dashboard.target_dir()
        if out_dir is None:
            if reason == "pinned":
                # pin 先が無いなら**黙って別のものを出さない**。選んだものが
                # 見えていないことに気づけるようにする。
                self._send_text(
                    503,
                    f"選ばれた fingerprint {self.dashboard.pinned()} の"
                    "ディレクトリが無い。選択を『自動』に戻すか、別のものを選ぶ。",
                )
                return
            self._send_text(
                503, "配信できる成果物がまだ無い。scan → check → select を実行する。"
            )
            return

        if name == "dashboard.html":
            try:
                self.dashboard.ensure_fresh(out_dir)
            except FileNotFoundError as error:
                self._send_text(503, str(error))
                return

        target = (out_dir / name).resolve()
        # SERVABLE で名前を絞ってあるが、配下であることも確かめる（二重の防御）。
        if not target.is_relative_to(out_dir.resolve()) or not target.exists():
            self._send_text(404, f"見つからない: {name}")
            return
        self._send_file(target)

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:  # noqa: N802  http.server の規約
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)

        if path == "/api/fingerprint":
            requested = params.get("fp", ["auto"])[0]
            if requested in ("", "auto"):
                self.dashboard.pin(None)
                self._send_json(200, {"pinned": None})
                return
            known = {o["fingerprint"] for o in self.dashboard.fingerprint_options()}
            if requested not in known:
                self._send_text(404, f"知らない fingerprint: {requested}")
                return
            self.dashboard.pin(requested)
            self._send_json(200, {"pinned": requested})
            return

        if path != "/api/refresh":
            self._send_text(404, f"不明なエンドポイント: {path}")
            return
        if not self.dashboard.allow_refresh:
            self._send_text(403, "このサーバーは閲覧専用で起動している（--no-refresh）")
            return

        mode = params.get("mode", ["html"])[0]
        if mode not in ("html", "full"):
            self._send_text(400, f"mode は html か full: {mode!r}")
            return

        # ★更新してよい状態かを先に見る。別構成を見ている状態で走らせると、
        # 混成HTMLを作るか（html）、表示と関係ない出力先に書くか（full）になる。
        writable = self.dashboard.writability()
        allowed = writable.can_rebuild_html if mode == "html" else writable.can_run_full
        if not allowed:
            self._send_json(
                409,
                {
                    "error": "いまは更新できない",
                    "blockers": list(writable.blockers),
                },
            )
            return

        if not self.dashboard.start_job(mode):
            self._send_json(409, {"error": "更新がすでに走っている"})
            return
        self._send_json(202, {"started": mode})

    # ----------------------------------------------------------------- 送信部

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target: Path) -> None:
        body = target.read_bytes()
        types = {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
        }
        self.send_response(200)
        self.send_header(
            "Content-Type", types.get(target.suffix, "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        # 更新ボタンで作り直した直後に古いHTMLを見せないため、キャッシュさせない。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _make_handler(dashboard: DashboardServer) -> type[_Handler]:
    return type("_BoundHandler", (_Handler,), {"dashboard": dashboard})


class _Server(http.server.ThreadingHTTPServer):
    # 再起動時に TIME_WAIT のソケットで bind に失敗しないようにする。
    allow_reuse_address = True
    daemon_threads = True


def _port_holder(port: int) -> str:
    """ポートを掴んでいるプロセスの説明。二重起動時に出す。"""
    try:
        result = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line for line in result.stdout.splitlines()[1:] if line.strip()]
    return "\n".join(lines)


def serve_forever(
    config: Config,
    renderer: Renderer,
    overrides: Sequence[str] = (),
    host: str = "127.0.0.1",
    port: int = 8899,
    allow_refresh: bool = True,
    pinned: str | None = None,
) -> bool:
    """常駐サーバーを起動する。停止まで戻らない。

    起動できたかを返す（終了コードへの変換は cli 側で行う。
    このモジュールが cli を import すると循環するため）。
    """
    dashboard = DashboardServer(
        config,
        renderer,
        overrides=overrides,
        allow_refresh=allow_refresh,
        pinned=pinned,
    )
    try:
        httpd = _Server((host, port), _make_handler(dashboard))
    except OSError as error:
        logger.error("ポート %d で待ち受けられない: %s", port, error)
        holder = _port_holder(port)
        if holder:
            logger.error("ポート %d を掴んでいるプロセス:\n%s", port, holder)
            logger.error(
                "すでに serve が上がっているなら、それをそのまま使う"
                "（http://localhost:%d/dashboard.html）。"
                "入れ替えるなら上の PID を kill するか --port で別ポートを指定する。",
                port,
            )
        else:
            logger.error("`ss -ltnp | grep %d` で使用中のプロセスを確認する", port)
        return False

    out_dir, reason = dashboard.target_dir()
    logger.info("ダッシュボードを配信する: http://localhost:%d/dashboard.html", port)
    logger.info("  配信元 %s（選定 %s）", out_dir, reason)
    if reason == "newest":
        logger.warning(
            "  config の fingerprint (%s) には select の成果物が無いので、"
            "最新の成果物があるディレクトリを配信している",
            dashboard.fingerprint(),
        )
    if reason == "missing":
        logger.warning("  成果物がまだ無い。check → select を実行してから開くこと")
    logger.info("  更新: %s", "有効" if allow_refresh else "無効（閲覧専用）")
    logger.info(
        "  Windows からは SSH port forwarding で見る（ssh -L %d:localhost:%d ...）。"
        "インターネットへ公開しないこと",
        port,
        port,
    )
    logger.info("  止めるときは Ctrl-C（nohup 起動なら kill <PID>）")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("停止する")
    finally:
        httpd.server_close()
    return True


__all__ = ["DashboardServer", "Job", "serve_forever"]
