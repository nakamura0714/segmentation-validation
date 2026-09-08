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

#: フル更新で回す段。目視結果の取り込みから採否・レポートまで一周する。
FULL_STEPS: tuple[tuple[str, ...], ...] = (
    ("review", "export"),
    ("select",),
    ("report",),
    ("gui",),
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
    ) -> None:
        self.config = config
        self.renderer = renderer
        self.overrides = list(overrides)
        self.allow_refresh = allow_refresh
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

    def target_dir(self) -> tuple[Path | None, str]:
        """配信するディレクトリと、そう選んだ理由を返す。

        config の fingerprint が指す先を優先する。そこに ``select`` の成果物が
        無ければ、``selection_decisions.json`` の mtime が最新のディレクトリへ
        フォールバックする。ディレクトリ自体の mtime は、既存ファイルを上書き
        するだけの ``select`` では更新されないことがあるので見ない。
        """
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
        """
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
        """``review export`` → ``select`` → ``report`` → ``gui`` を順に回す。

        サブプロセスで走らせるのは、``review export`` が fiftyone を import する
        から。このモジュールが直接呼ぶと層の約束が壊れる。
        """
        set_args: list[str] = []
        for override in self.overrides:
            set_args += ["--set", override]

        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n===== フル更新 {job.started_at} =====\n")
            for step in FULL_STEPS:
                name = " ".join(step)
                job.step = name
                job.say(f"$ segmentation-validation {' '.join(set_args + list(step))}")
                log.write(f"--- {name} ---\n")
                log.flush()
                code = self._run_step(step, set_args, job, log)
                if code != 0:
                    job.say(f"`{name}` が exit {code} で終わった。ここで中断する。")
                    job.ok = False
                    return
        job.ok = True

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
            "job": job.as_dict() if job else None,
        }


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

        name = path.lstrip("/")
        if name not in SERVABLE:
            self._send_text(404, f"配信していない: {name}")
            return

        out_dir, _ = self.dashboard.target_dir()
        if out_dir is None:
            self._send_text(
                503, "配信できる成果物がまだ無い。check / select を実行してから開く。"
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
        if path != "/api/refresh":
            self._send_text(404, f"不明なエンドポイント: {path}")
            return
        if not self.dashboard.allow_refresh:
            self._send_text(403, "このサーバーは閲覧専用で起動している（--no-refresh）")
            return

        mode = urllib.parse.parse_qs(query).get("mode", ["html"])[0]
        if mode not in ("html", "full"):
            self._send_text(400, f"mode は html か full: {mode!r}")
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
) -> bool:
    """常駐サーバーを起動する。停止まで戻らない。

    起動できたかを返す（終了コードへの変換は cli 側で行う。
    このモジュールが cli を import すると循環するため）。
    """
    dashboard = DashboardServer(
        config, renderer, overrides=overrides, allow_refresh=allow_refresh
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
