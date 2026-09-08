"""ダッシュボードの常駐サーバー（``report/serve.py``）。

**担保したいのは3つ。**

1. **配信ディレクトリをリクエストごとに解決する。** データセットを足すと
   fingerprint が変わって出力先ディレクトリが変わる。ここが起動時固定だと
   古い成果物を配信し続ける（notebook 版の欠陥そのもの）。
2. **陳腐化したら作り直す。** ``select`` を回したあとブラウザを再読み込みする
   だけで最新になること。
3. **配下の外を配信しない。** ローカル限定でもパストラバーサルは塞ぐ。

ネットワークもソケットも使わない。HTTPハンドラは ``BaseHTTPRequestHandler`` の
入出力だけを差し替えて呼ぶ。fiftyone も mongod も要らない。
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from segmentation_validation.config import Config
from segmentation_validation.report import serve as serve_mod


@pytest.fixture
def project(tmp_path: Path) -> Config:
    """``output/validation/`` を持つだけの空プロジェクト。"""
    (tmp_path / "output" / "validation").mkdir(parents=True)
    return replace(Config(), project_root=tmp_path)


def make_out_dir(
    config: Config, name: str, dashboard: str | None = "<html>old</html>"
) -> Path:
    """成果物が揃った出力ディレクトリを1つ作る。"""
    out_dir = config.validation_dir / name
    (out_dir / "review").mkdir(parents=True)
    (out_dir / "issues.json").write_text("{}", encoding="utf-8")
    (out_dir / "selection_decisions.json").write_text("{}", encoding="utf-8")
    (out_dir / "image_decisions.json").write_text("{}", encoding="utf-8")
    (out_dir / "issues.csv").write_text("check_id\n", encoding="utf-8")
    if dashboard is not None:
        (out_dir / "dashboard.html").write_text(dashboard, encoding="utf-8")
    return out_dir


def make_server(config: Config, fingerprint: str = "v_test", **kwargs):
    """``DashboardServer``。fingerprint は固定し、renderer は呼ばれた回数を数える。"""
    calls: list[Path] = []

    def renderer(_config: Config, out_dir: Path) -> Path:
        calls.append(out_dir)
        target = out_dir / "dashboard.html"
        target.write_text(f"<html>built {len(calls)}</html>", encoding="utf-8")
        return target

    dashboard = serve_mod.DashboardServer(config, renderer, **kwargs)
    # 実データJSONが無い環境なので、fingerprint の計算だけ差し替える。
    dashboard.fingerprint = lambda: fingerprint  # type: ignore[method-assign]
    return dashboard, calls


# ----------------------------------------------------------- 配信先の解決


def test_configのfingerprintが指す先を配信する(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    out_dir, reason = dashboard.target_dir()

    assert out_dir.name == "v_test"
    assert reason == "config"


def test_fingerprintが変わると配信先も変わる(project):
    """★これが起動時固定だとデータセット追加で古い方を配信し続ける。"""
    make_out_dir(project, "v_old")
    make_out_dir(project, "v_new")
    dashboard, _ = make_server(project, "v_old")
    assert dashboard.target_dir()[0].name == "v_old"

    # データセットを足した状況。config が別の fingerprint を指すようになる。
    dashboard.fingerprint = lambda: "v_new"  # type: ignore[method-assign]

    assert dashboard.target_dir()[0].name == "v_new"


def test_成果物が無いfingerprintなら最新へフォールバックする(project):
    """select していない fingerprint を指していても、見られる方を出す。"""
    older = make_out_dir(project, "v_older")
    newer = make_out_dir(project, "v_newer")
    os.utime(older / "selection_decisions.json", (1000, 1000))
    os.utime(newer / "selection_decisions.json", (2000, 2000))
    dashboard, _ = make_server(project, "v_nothing_here")

    out_dir, reason = dashboard.target_dir()

    assert out_dir.name == "v_newer"
    assert reason == "newest"


def test_成果物が1つも無ければmissingを返す(project):
    dashboard, _ = make_server(project, "v_test")

    out_dir, reason = dashboard.target_dir()

    assert out_dir is None
    assert reason == "missing"


def test_selectしていないディレクトリは候補にしない(project):
    """``check`` だけ通した中途半端なディレクトリを「配信できる」と言わない。"""
    half = project.validation_dir / "v_half"
    half.mkdir()
    (half / "issues.json").write_text("{}", encoding="utf-8")
    dashboard, _ = make_server(project, "v_half")

    assert dashboard.target_dir() == (half, "missing")


# --------------------------------------------------------------- 陳腐化


def test_成果物が新しければ作り直す(project):
    out_dir = make_out_dir(project, "v_test")
    dashboard, calls = make_server(project, "v_test")
    assert not dashboard.is_stale(out_dir)

    # select を回した状況
    os.utime(out_dir / "selection_decisions.json", None)
    os.utime(
        out_dir / "dashboard.html",
        (1000, 1000),
    )
    assert dashboard.is_stale(out_dir)

    dashboard.ensure_fresh(out_dir)

    assert calls == [out_dir]
    assert not dashboard.is_stale(out_dir)


def test_目視結果が新しくても作り直す(project):
    """review export したのに dashboard が古いまま、が一番起きやすい。"""
    out_dir = make_out_dir(project, "v_test")
    (out_dir / "review" / "review_decisions.json").write_text("{}", encoding="utf-8")
    os.utime(out_dir / "dashboard.html", (1000, 1000))
    dashboard, calls = make_server(project, "v_test")

    dashboard.ensure_fresh(out_dir)

    assert calls == [out_dir]


def test_dashboardが無ければ陳腐(project):
    out_dir = make_out_dir(project, "v_test", dashboard=None)
    dashboard, calls = make_server(project, "v_test")

    assert dashboard.is_stale(out_dir)
    dashboard.ensure_fresh(out_dir)
    assert calls == [out_dir]


def test_最新なら作り直さない(project):
    out_dir = make_out_dir(project, "v_test")
    dashboard, calls = make_server(project, "v_test")

    dashboard.ensure_fresh(out_dir)

    assert calls == []


# ------------------------------------------------------------- 更新ジョブ


def test_HTML再構成ジョブはrendererを呼ぶ(project):
    out_dir = make_out_dir(project, "v_test")
    dashboard, calls = make_server(project, "v_test")

    job = serve_mod.Job(mode="html", step="準備中")
    dashboard._run_job(job)

    assert job.ok is True
    assert job.finished_at is not None
    assert calls == [out_dir]


def test_成果物が無ければジョブは失敗として終わる(project):
    """サーバーは落とさない。UI にログを出して終わる。"""
    dashboard, calls = make_server(project, "v_test")

    job = serve_mod.Job(mode="html", step="準備中")
    dashboard._run_job(job)

    assert job.ok is False
    assert job.step == "失敗"
    assert calls == []
    assert any("成果物" in line for line in job.lines)


def test_ログは末尾だけ持つ(project):
    """フル更新は数百行出る。全部持つとステータスJSONが膨らむ。"""
    job = serve_mod.Job(mode="full", step="select")
    for i in range(serve_mod.LOG_TAIL + 50):
        job.say(f"line {i}")

    assert len(job.lines) == serve_mod.LOG_TAIL
    assert job.lines[-1] == f"line {serve_mod.LOG_TAIL + 49}"


def test_フル更新はreviewexportからguiまでを順に回す():
    """段の並びが崩れると、目視結果を取り込む前に select してしまう。"""
    assert serve_mod.FULL_STEPS == (
        ("review", "export"),
        ("select",),
        ("report",),
        ("gui",),
    )


# ------------------------------------------------------------ ルーティング


class _FakeSocket:
    def __init__(self, request: bytes) -> None:
        self._request = request

    def makefile(self, mode: str, *args, **kwargs):
        return io.BytesIO(self._request) if "r" in mode else io.BytesIO()

    def getsockname(self):
        return ("127.0.0.1", 8899)


def request(dashboard, method: str, path: str):
    """ハンドラを直接叩いて (status, headers, body) を返す。"""
    handler_cls = serve_mod._make_handler(dashboard)
    raw = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
    sock = _FakeSocket(raw)
    sent = io.BytesIO()

    class _Bound(handler_cls):  # type: ignore[misc, valid-type]
        def setup(self) -> None:
            self.rfile = io.BytesIO(raw)
            self.wfile = sent

        def finish(self) -> None:
            pass

        def log_message(self, *args, **kwargs) -> None:
            pass

    _Bound(sock, ("127.0.0.1", 12345), None)
    head, _, body = sent.getvalue().partition(b"\r\n\r\n")
    lines = head.decode("latin-1").splitlines()
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


def test_ルートはdashboardへリダイレクトする(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, headers, _ = request(dashboard, "GET", "/")

    assert status == 302
    assert headers["location"] == "/dashboard.html"


def test_statusは状態JSONを返す(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, _, body = request(dashboard, "GET", "/api/status")

    assert status == 200
    payload = json.loads(body)
    assert payload["config_fingerprint"] == "v_test"
    assert payload["served_fingerprint"] == "v_test"
    assert payload["served_reason"] == "config"
    assert payload["allow_refresh"] is True


def test_dashboardのGETで陳腐化を直す(project):
    out_dir = make_out_dir(project, "v_test")
    os.utime(out_dir / "dashboard.html", (1000, 1000))
    dashboard, calls = make_server(project, "v_test")

    status, _, body = request(dashboard, "GET", "/dashboard.html")

    assert status == 200
    assert calls == [out_dir]
    assert b"built" in body


def test_許可した成果物は配信する(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, _, body = request(dashboard, "GET", "/issues.csv")

    assert status == 200
    assert body == b"check_id\n"


@pytest.mark.parametrize(
    "path",
    [
        "/../../CLAUDE.md",
        "/..%2f..%2fCLAUDE.md",
        "/review/review_decisions.json",
        "/review/images/x.png",
        "/nope.txt",
    ],
)
def test_配下の外と許可外は配信しない(project, path):
    """★``review/`` のPNGは391MBあるので、名前で塞いでおく。"""
    make_out_dir(project, "v_test")
    (project.project_root / "CLAUDE.md").write_text("秘密", encoding="utf-8")
    dashboard, _ = make_server(project, "v_test")

    status, _, body = request(dashboard, "GET", path)

    assert status == 404
    assert "秘密".encode() not in body


def test_成果物が無ければ503を返す(project):
    dashboard, _ = make_server(project, "v_test")

    status, _, _ = request(dashboard, "GET", "/dashboard.html")

    assert status == 503


# ------------------------------------------------------------------ POST


def test_更新は202で受け付ける(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, _, body = request(dashboard, "POST", "/api/refresh?mode=html")

    assert status == 202
    assert json.loads(body) == {"started": "html"}


def test_実行中の更新は409で断る(project):
    """同じジョブを2本走らせると同じファイルを2回書く。"""
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")
    # 走っている状態を作る（finished_at が None のジョブ）
    dashboard._job = serve_mod.Job(mode="full", step="select")

    status, _, body = request(dashboard, "POST", "/api/refresh?mode=html")

    assert status == 409
    assert "走っている" in json.loads(body)["error"]


def test_終わったジョブのあとは再度受け付ける(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")
    done = serve_mod.Job(mode="html", step="完了")
    done.finished_at = "2026-09-08T00:00:00+00:00"
    done.ok = True
    dashboard._job = done

    status, _, _ = request(dashboard, "POST", "/api/refresh?mode=html")

    assert status == 202


def test_知らないmodeは400(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, _, _ = request(dashboard, "POST", "/api/refresh?mode=bogus")

    assert status == 400


def test_閲覧専用なら更新を403で断る(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test", allow_refresh=False)

    status, _, body = request(dashboard, "POST", "/api/refresh?mode=html")

    assert status == 403
    assert "閲覧専用" in body.decode("utf-8")
    assert request(dashboard, "GET", "/dashboard.html")[0] == 200, "閲覧はできる"


def test_知らないエンドポイントは404(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    assert request(dashboard, "POST", "/api/nope")[0] == 404


# ------------------------------------------------------- 目視判定の引き継ぎ


def test_目視判定の件数を状態に出す(project):
    """fingerprint が変わると review_decisions.json も空になる。

    引き継がれていないことに気づけるよう、件数をUIへ出す。
    """
    out_dir = make_out_dir(project, "v_test")
    (out_dir / "review" / "review_decisions.json").write_text(
        json.dumps({"decisions": [1, 2, 3], "image_decisions": [1]}), encoding="utf-8"
    )
    dashboard, _ = make_server(project, "v_test")

    counts = dashboard.status()["review_decisions"]

    assert counts == {"annotations": 3, "images": 1}


def test_目視判定が無ければ0件として出す(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    assert dashboard.status()["review_decisions"] == {"annotations": 0, "images": 0}
