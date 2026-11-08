"""Trin C2 (8.11.2026): den interne adresse til Fangarr. Svarene testes direkte, og én rigtig webserver på en
tilfældig port testes for nøglen. Intet netværk ud af maskinen.
"""

import json
import os
import socket
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest

from oversaetter import api
from oversaetter.db import Database, iso, month_key
from tests.helpers import cfg as make_cfg


@pytest.fixture
def setup(tmp_path):
    cfg = make_cfg(data_dir=str(tmp_path), monthly_budget_usd=100.0, translate_all_since="2026-11-10")
    db = Database(os.path.join(str(tmp_path), "oversaetter.db"))
    db.upsert(
        "episode:5",
        media_type="episode",
        media_id=5,
        title="Gold Rush 17x06",
        status="faerdig",
        cost_usd=0.54,
        flags_before=12,
        flags_after=2,
        fixes=10,
        video_path="/mnt/unionfs/Media/TV/Serier/Gold Rush/Season 17/Gold.Rush.S17E06.mkv",
    )
    db.upsert("movie:9", media_type="movie", media_id=9, title="Film", status="fejlet", reason="ingen engelsk")
    today = datetime.now(UTC)
    db.conn.execute(
        "INSERT INTO spend (ts, month, item_key, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, 1, 1, ?)",
        (iso(today), month_key(today), "episode:5", 0.54),
    )
    db.conn.execute(
        "INSERT INTO spend (ts, month, item_key, input_tokens, output_tokens, cost_usd) VALUES (?, ?, ?, 1, 1, ?)",
        (iso(today), month_key(today), None, 3.0),  # test uden titel tæller ikke pr. dag
    )
    db.conn.commit()
    return cfg, db


def test_items_hides_full_path_and_filters_status(setup):
    cfg, _ = setup
    out = api.Api(cfg).items({"status": "faerdig"})
    assert [i["key"] for i in out["items"]] == ["episode:5"]
    item = out["items"][0]
    assert item["file"] == "Gold.Rush.S17E06.mkv" and "video_path" not in item
    assert item["cost_usd"] == 0.54 and item["fixes"] == 10
    assert out["counts"] == {"faerdig": 1, "fejlet": 1}


def test_spend_per_day_ignores_test_rows(setup):
    cfg, _ = setup
    out = api.Api(cfg).spend({"days": "7"})
    assert out["days"] == [{"day": datetime.now(UTC).strftime("%Y-%m-%d"), "type": "episode", "titles": 1, "usd": 0.54}]
    assert out["month_usd"] == pytest.approx(3.54) and out["budget_usd"] == 100.0


def test_status_and_config_have_no_secrets(setup):
    cfg, _ = setup
    cfg.bazarr_api_key, cfg.api_token = "bazarr-hemmelig", "api-hemmelig"
    a = api.Api(cfg)
    status, config = a.status(), a.config()
    assert status["budget_usd"] == 100.0 and status["month_usd"] == pytest.approx(3.54) and status["failed"] == 1
    assert config["translate_all_since"] == "2026-11-10" and config["monthly_budget_usd"] == 100.0
    text = json.dumps([status, config])
    assert "hemmelig" not in text


def test_unknown_path_is_404(setup):
    cfg, _ = setup
    assert api.Api(cfg).handle("/api/nej", {})[0] == 404


def test_no_database_yet(tmp_path):
    a = api.Api(make_cfg(data_dir=str(tmp_path / "tom")))
    assert a.items({}) == {"items": [], "counts": {}}
    assert a.spend({})["days"] == []


def test_start_needs_port_and_token(tmp_path):
    assert api.start(make_cfg(data_dir=str(tmp_path), api_port=0), None) is None
    assert api.start(make_cfg(data_dir=str(tmp_path), api_port=8090, api_token=""), None) is None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_requires_token(setup):
    cfg, _ = setup
    cfg.api_port, cfg.api_token = _free_port(), "rigtig-noegle"

    class R:
        wake = None

    runner = R()
    server = api.start(cfg, runner)
    try:
        assert runner.wake is not None  # løkken kan vækkes (bruges af knapperne senere)
        base = f"http://127.0.0.1:{cfg.api_port}"
        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            assert json.load(r) == {"ok": True}
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/status", timeout=5)
        assert e.value.code == 401
        req = urllib.request.Request(base + "/api/items?limit=1", headers={"X-Api-Token": "rigtig-noegle"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert len(json.load(r)["items"]) == 1
    finally:
        server.shutdown()
        server.server_close()
