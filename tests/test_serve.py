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
from segmentation_validation.report.validation_meta import (
    Measurements,
    write_meta,
)


@pytest.fixture
def project(tmp_path: Path) -> Config:
    """``output/validation/`` を持つだけの空プロジェクト。"""
    (tmp_path / "output" / "validation").mkdir(parents=True)
    return replace(Config(), project_root=tmp_path)


def make_out_dir(
    config: Config,
    name: str,
    dashboard: str | None = "<html>old</html>",
    measured: bool = True,
) -> Path:
    """成果物が揃った出力ディレクトリを1つ作る。

    ``measured=True`` のときは走査キャッシュと ``meta.json`` も作る。
    「再生成してよい健全な状態」を表すにはそこまで要る（計測が欠けていれば
    面積が空のHTMLを作ってしまうので、再生成は止まる）。
    """
    out_dir = config.validation_dir / name
    (out_dir / "review").mkdir(parents=True)
    (out_dir / "issues.json").write_text("{}", encoding="utf-8")
    (out_dir / "selection_decisions.json").write_text("{}", encoding="utf-8")
    (out_dir / "image_decisions.json").write_text("{}", encoding="utf-8")
    (out_dir / "issues.csv").write_text("check_id\n", encoding="utf-8")
    if dashboard is not None:
        (out_dir / "dashboard.html").write_text(dashboard, encoding="utf-8")
    if measured:
        make_cache(config, name)
        write_meta(
            out_dir,
            fingerprint=name,
            sources=["a.json"],
            measurements=Measurements(n_files=1, n_measured=1, scan_required=True),
        )
    return out_dir


def make_cache(config: Config, name: str) -> Path:
    """走査キャッシュを実在させる（中身は問わない。有無だけを見ている）。"""
    cache = config.cache_dir / name
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "files.jsonl").write_text("", encoding="utf-8")
    return cache


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


def test_フル更新はscanからguiまでを順に回す():
    """★``scan`` と ``check`` が要る。

    無いと fingerprint が変わった直後は ``select`` が「issues.json が無い」で
    必ず落ち、**ボタンでは絶対に解消できない**（実際にそうなっていた）。
    並びが崩れると、目視結果を取り込む前に select してしまう。
    """
    steps = [step for step, _ in serve_mod.FULL_STEPS]

    assert steps == [
        ("scan",),
        ("check",),
        ("review", "export"),
        ("select",),
        ("report",),
        ("gui",),
    ]


def test_checkのexit1は成功扱いでselectのexit1は失敗扱い():
    """★``check`` は error を見つけると 1 を返すのが正常。

    実データには error が204件ある。1 を失敗扱いにすると連鎖が毎回 check で
    止まる。逆に ``select`` の 1（部分的な issues）は止めなければならない。
    """
    accepted = dict(serve_mod.FULL_STEPS)

    assert 1 in accepted[("check",)]
    assert 1 not in accepted[("select",)]
    assert 1 not in accepted[("scan",)]
    assert 1 not in accepted[("gui",)]


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


# ------------------------------- 更新してよいか（混成と静かな劣化を止める）


def test_別構成を配信中は再生成しない(project):
    """★これが今回の実害。混成HTMLで正常な成果物を潰していた。

    config が6本構成を指し、そこに成果物が無いので12本構成へフォールバック
    している状態で ``GET /dashboard.html`` すると、以前は
    ``renderer(self.config, out_dir)`` を呼んで「12本の採否に6本の母集団と
    （存在しない）計測値を貼ったHTML」を作り、正常な 20MB を上書きしていた。
    """
    other = make_out_dir(project, "v_twelve")
    os.utime(other / "dashboard.html", (1000, 1000))  # 陳腐化させる
    dashboard, calls = make_server(project, "v_six")  # config は別の fingerprint

    out_dir, reason = dashboard.target_dir()
    assert (out_dir, reason) == (other, "newest")
    assert dashboard.is_stale(out_dir), "前提: 陳腐化している"

    dashboard.ensure_fresh(out_dir)

    assert calls == [], "別構成を config で再生成してはいけない"
    writable = dashboard.writability()
    assert writable.can_rebuild_html is False
    assert writable.can_run_full is False
    assert any("別の構成" in b for b in writable.blockers)


def test_計測が欠けていれば一致していても再生成しない(project):
    """★fingerprint が一致していても、計測が無ければ止める。

    面積も包含率も空のダッシュボードを作って正常な成果物を潰すので、
    別構成を混ぜるのと同じ静かな劣化になる。
    """
    out_dir = make_out_dir(project, "v_test", measured=False)
    os.utime(out_dir / "dashboard.html", (1000, 1000))
    dashboard, calls = make_server(project, "v_test")

    dashboard.ensure_fresh(out_dir)

    writable = dashboard.writability()
    assert calls == []
    assert writable.can_rebuild_html is False
    assert any("計測" in b for b in writable.blockers)


def test_計測が欠けていてもフル更新は走れる(project):
    """★ここを塞ぐと新しい構成から復旧できなくなる。

    フル更新は ``scan``→``check``→``select`` で足りないものを自分で作る。
    """
    make_out_dir(project, "v_test", measured=False)
    dashboard, _ = make_server(project, "v_test")

    writable = dashboard.writability()

    assert writable.can_rebuild_html is False
    assert writable.can_run_full is True


def test_成果物が1つも無くてもフル更新は走れる(project):
    """データセットを足した直後がこの状態。ここから復旧させる。"""
    dashboard, _ = make_server(project, "v_brand_new")

    writable = dashboard.writability()

    assert writable.can_run_full is True
    assert writable.can_rebuild_html is False


def test_成果物のmetaが別のfingerprintなら再生成しない(project):
    """別ディレクトリからコピーされた成果物を config で作り直させない。"""
    out_dir = make_out_dir(project, "v_test")
    write_meta(
        out_dir,
        fingerprint="v_somewhere_else",
        sources=["a.json"],
        measurements=Measurements(n_files=1, n_measured=1, scan_required=True),
    )
    dashboard, _ = make_server(project, "v_test")

    writable = dashboard.writability()

    assert writable.can_rebuild_html is False
    assert any("meta" in b for b in writable.blockers)


def test_閲覧専用ならどちらも走れない(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test", allow_refresh=False)

    writable = dashboard.writability()

    assert (writable.can_rebuild_html, writable.can_run_full) == (False, False)


def test_更新できないときは理由つきの409(project):
    """UI が「なぜ押せないか」を出せるように、理由を返す。"""
    make_out_dir(project, "v_twelve")
    dashboard, _ = make_server(project, "v_six")

    status, _, body = request(dashboard, "POST", "/api/refresh?mode=html")

    assert status == 409
    payload = json.loads(body)
    assert payload["blockers"], "理由が空だと UI に出せない"


def test_不足している成果物を列挙する(project):
    """★以前は「select の成果物が無い」としか言わず、scan と check が
    足りないことが読めなかった（それで「select したのに」になった）。
    """
    dashboard, _ = make_server(project, "v_brand_new")

    missing = dashboard.status()["missing_artifacts"]

    joined = " ".join(missing)
    assert "scan" in joined
    assert "check" in joined
    assert "select" in joined


# ----------------------------------------------- fingerprint の明示選択


def test_pin中はフォールバックしない(project):
    """★選んだものが見えないなら選択の意味が無い。黙って別のものを出さない。"""
    make_out_dir(project, "v_other")
    dashboard, _ = make_server(project, "v_config")
    dashboard.pin("v_missing")

    out_dir, reason = dashboard.target_dir()

    assert reason == "pinned"
    assert out_dir is None


def test_pin中はconfigに成果物があってもpinを配信する(project):
    make_out_dir(project, "v_config")
    make_out_dir(project, "v_older")
    dashboard, _ = make_server(project, "v_config")
    dashboard.pin("v_older")

    out_dir, reason = dashboard.target_dir()

    assert (out_dir.name, reason) == ("v_older", "pinned")


def test_pin先が無ければ503で理由を出す(project):
    make_out_dir(project, "v_other")
    dashboard, _ = make_server(project, "v_config")
    dashboard.pin("v_missing")

    status, _, body = request(dashboard, "GET", "/dashboard.html")

    assert status == 503
    assert "v_missing" in body.decode("utf-8")


def test_pinを解除すると自動解決に戻る(project):
    make_out_dir(project, "v_config")
    make_out_dir(project, "v_older")
    dashboard, _ = make_server(project, "v_config")

    dashboard.pin("v_older")
    assert dashboard.target_dir()[1] == "pinned"
    dashboard.pin(None)

    assert dashboard.target_dir()[1] == "config"


def test_pinした別構成は読み取り専用(project):
    make_out_dir(project, "v_config")
    make_out_dir(project, "v_older")
    dashboard, calls = make_server(project, "v_config")
    dashboard.pin("v_older")

    out_dir, _ = dashboard.target_dir()
    os.utime(out_dir / "dashboard.html", (1000, 1000))
    dashboard.ensure_fresh(out_dir)

    assert calls == []
    assert dashboard.writability().can_rebuild_html is False


def test_fingerprint一覧を返す(project):
    make_out_dir(project, "v_config")
    make_out_dir(project, "v_older", measured=False)
    dashboard, _ = make_server(project, "v_config")

    status, _, body = request(dashboard, "GET", "/api/fingerprints")

    assert status == 200
    options = {o["fingerprint"]: o for o in json.loads(body)["options"]}
    assert set(options) == {"v_config", "v_older"}
    assert options["v_config"]["is_config"] is True
    assert options["v_older"]["is_config"] is False
    assert options["v_config"]["artifacts"]["selection_decisions.json"] is True


def test_metaが無くてもキャッシュmanifestで構成が分かる(project):
    """古い出力ディレクトリでも一覧に構成を出せること。"""
    make_out_dir(project, "v_old_style", measured=False)
    cache = make_cache(project, "v_old_style")
    (cache / "manifest.json").write_text(
        json.dumps({"sources": [{"name": "a.json"}, {"name": "b.json"}]}),
        encoding="utf-8",
    )
    dashboard, _ = make_server(project, "v_old_style")

    options = {o["fingerprint"]: o for o in dashboard.fingerprint_options()}

    assert options["v_old_style"]["n_sources"] == 2
    assert options["v_old_style"]["sources"] == ["a.json", "b.json"]


def test_知らないfingerprintはpinできない(project):
    make_out_dir(project, "v_test")
    dashboard, _ = make_server(project, "v_test")

    status, _, _ = request(dashboard, "POST", "/api/fingerprint?fp=v_nope")

    assert status == 404
    assert dashboard.pinned() is None


def test_APIからpinと解除ができる(project):
    make_out_dir(project, "v_test")
    make_out_dir(project, "v_other")
    dashboard, _ = make_server(project, "v_test")

    assert request(dashboard, "POST", "/api/fingerprint?fp=v_other")[0] == 200
    assert dashboard.pinned() == "v_other"
    assert request(dashboard, "POST", "/api/fingerprint?fp=auto")[0] == 200
    assert dashboard.pinned() is None
