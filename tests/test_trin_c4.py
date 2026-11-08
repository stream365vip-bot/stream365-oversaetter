"""Trin C4 (8.11.2026): knapperne i Fangarr - overslag, "Oversæt nu", "Prøv igen" og "Opgiv". Alt med fakes."""

import json
import os
import socket
import urllib.error
import urllib.request

import pytest

from oversaetter import api, knap
from oversaetter.db import Database
from tests.fakes import FakeArr, en_sub
from tests.test_direct import EN, FakeAnthropic, runner
from tests.test_trin_c1 import messages


@pytest.fixture
def arr(tmp_path):
    return FakeArr(str(tmp_path / "Media"))


def with_english(arr, ep_id, series_id=70, season=1, profile=False, **extra):
    video = arr.add_episode(ep_id, series_id, 700 + series_id, {"days": 300}, [], season=season)
    en = os.path.splitext(video)[0] + ".en.srt"
    with open(en, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.episodes[ep_id]["subtitles"] = [en_sub(en)]
    arr.episodes[ep_id].update(extra)
    if not profile:
        arr.no_profile.add(f"series:{series_id}")
    return video


def make_api(arr, tmp_path, r=None, **kw):
    r, db = r or runner(arr, tmp_path, claude=FakeAnthropic(), **kw)
    a = api.Api(r.cfg, r)
    a._clients = (r.sonarr, r.radarr, r.bazarr)
    return a, r, db


def da(video):
    return os.path.exists(os.path.splitext(video)[0] + ".da.srt")


# --- overslag ------------------------------------------------------------------------------------------------
def test_estimate_per_season(arr, tmp_path):
    with_english(arr, 1, season=1)
    with_english(arr, 2, season=1, audio=["Danish"])  # dansk tale
    with_english(arr, 3, season=2)
    with_english(arr, 4, season=2, has_file=False)  # ingen fil tæller ikke
    a, r, db = make_api(arr, tmp_path)
    db.upsert("episode:3", media_type="episode", media_id=3, status="soeger")
    code, out = a.handle("/api/estimate", {"series_id": "70"})
    assert code == 200 and out["title"] == "serie 70"  # falsk Sonarr har ingen titel
    s1, s2 = out["seasons"]
    assert (s1["season"], s1["episodes"], s1["dansk"], s1["mangler"]) == (1, 2, 1, 1)
    assert (s2["episodes"], s2["i_gang"], s2["mangler"]) == (1, 1, 0)
    assert s1["est_usd"] == 0.54 and out["price_per_item_usd"] == 0.54  # for få tal endnu - fast pris
    assert out["budget_usd"] == r.cfg.monthly_budget_usd


def test_estimate_uses_measured_price(arr, tmp_path):
    with_english(arr, 1)
    a, r, db = make_api(arr, tmp_path)
    for i in range(4):
        db.conn.execute(
            "INSERT INTO spend (ts, month, item_key, input_tokens, output_tokens, cost_usd) "
            "VALUES (datetime('now'), strftime('%Y-%m', 'now'), ?, 1, 1, 0.5)",
            (f"episode:{100 + i}",),
        )
    db.conn.commit()
    assert knap.avg_cost(db, "episode") == 0.5


def test_estimate_movie(arr, tmp_path):
    arr.add_movie(9, 909, {"days": 400}, [], requested=False)
    a, r, db = make_api(arr, tmp_path)
    code, out = a.handle("/api/estimate", {"movie_id": "9"})
    assert code == 200 and out["state"] == "mangler" and out["est_usd"] == 0.83
    assert a.handle("/api/estimate", {})[0] == 400


# --- Oversæt nu ----------------------------------------------------------------------------------------------
def test_translate_chosen_seasons_only(arr, tmp_path, caplog):
    caplog.set_level("INFO", logger="oversaetter")
    s1 = with_english(arr, 1, season=1)
    s2 = with_english(arr, 3, season=2)
    a, r, db = make_api(arr, tmp_path)
    r.wake = __import__("threading").Event()
    code, out = a.handle_post("/api/translate", {"series_id": 70, "seasons": [1]})
    assert code == 200 and out["added"] == 1 and r.wake.is_set()
    for _ in range(2):
        r.run_once()
    assert da(s1) and not da(s2)
    assert arr.profile_sets == [("series", 70, 1)]  # serien fik sprogprofilen, så Bazarr kan søge og modtage
    assert db.get("episode:1")["status"] == "faerdig"
    assert any("Knap: 1 film/afsnit valgt i Fangarr" in m for m in messages(caplog))
    # Trykkes der igen, lægges intet nyt i kø
    assert a.handle_post("/api/translate", {"series_id": 70, "seasons": [1]})[1]["added"] == 0


def test_translate_movie_works_without_translate_all_since(arr, tmp_path):
    video = arr.add_movie(9, 909, {"days": 400}, [], requested=False)
    en = os.path.splitext(video)[0] + ".en.srt"
    with open(en, "w", encoding="utf-8") as f:
        f.write(EN)
    arr.movies[9]["subtitles"] = [en_sub(en)]
    a, r, db = make_api(arr, tmp_path)
    assert a.handle_post("/api/translate", {"movie_id": 9})[1]["added"] == 1
    for _ in range(2):
        r.run_once()
    assert da(video)


def test_translate_bad_body(arr, tmp_path):
    a, r, db = make_api(arr, tmp_path)
    assert a.handle_post("/api/translate", {"series_id": 70, "seasons": []})[0] == 400
    assert a.handle_post("/api/translate", {"nej": 1})[0] == 400
    assert a.handle_post("/api/slet-alt", {})[0] == 404


# --- Prøv igen og Opgiv --------------------------------------------------------------------------------------
def test_retry_failed_title(arr, tmp_path):
    video = with_english(arr, 1, profile=True)
    a, r, db = make_api(arr, tmp_path)
    db.upsert("episode:1", media_type="episode", media_id=1, series_id=70, title="Serie 70 1x01", status="fejlet")
    code, out = a.handle_post("/api/items/retry", {"key": "episode:1"})
    assert code == 200 and out["title"] == "Serie 70 1x01"
    for _ in range(2):
        r.run_once()
    assert da(video) and db.get("episode:1")["status"] == "faerdig"


def test_retry_and_skip_refuse_finished(arr, tmp_path):
    a, r, db = make_api(arr, tmp_path)
    db.upsert("episode:1", media_type="episode", media_id=1, title="X", status="faerdig")
    assert a.handle_post("/api/items/retry", {"key": "episode:1"})[0] == 409
    assert a.handle_post("/api/items/skip", {"key": "episode:1"})[0] == 409
    assert a.handle_post("/api/items/retry", {"key": "slet:alt"})[0] == 400
    assert a.handle_post("/api/items/skip", {"key": "episode:99"})[0] == 409


def test_skip_is_permanent(arr, tmp_path):
    video = with_english(arr, 1, profile=True)
    a, r, db = make_api(arr, tmp_path)
    db.upsert("episode:1", media_type="episode", media_id=1, title="X", status="mangler_engelsk")
    a.handle_post("/api/translate", {"series_id": 70, "seasons": [1]})  # står i kø fra en knap
    assert a.handle_post("/api/items/skip", {"key": "episode:1"})[0] == 200
    assert db.conn.execute("SELECT COUNT(*) FROM manual").fetchone()[0] == 0
    r.run_once()
    assert db.get("episode:1")["status"] == "opgivet" and not da(video)


def test_old_manual_rows_are_cleaned(arr, tmp_path):
    a, r, db = make_api(arr, tmp_path)
    db.conn.execute(
        "INSERT INTO manual (key, media_type, media_id, added_at, source) VALUES ('movie:1', 'movie', 1, "
        "'2020-01-01T00:00:00+00:00', 'oversaet-nu')"
    )
    db.conn.commit()
    r.run_once()
    assert db.conn.execute("SELECT COUNT(*) FROM manual").fetchone()[0] == 0


# --- webserveren -----------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_post_needs_token_and_json(arr, tmp_path):
    a, r, db = make_api(arr, tmp_path)
    r.cfg.api_port, r.cfg.api_token = _free_port(), "noegle"
    server = api.start(r.cfg, r)
    try:
        base = f"http://127.0.0.1:{r.cfg.api_port}"

        def post(path, body, token="noegle"):
            req = urllib.request.Request(
                base + path,
                data=body,
                method="POST",
                headers={"X-Api-Token": token, "Content-Type": "application/json"},
            )
            return urllib.request.urlopen(req, timeout=5)

        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/items/skip", b'{"key": "episode:1"}', token="forkert")
        assert e.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as e:
            post("/api/items/skip", b"ikke json")
        assert e.value.code == 400
        db.upsert("episode:1", media_type="episode", media_id=1, title="X", status="fejlet")
        with post("/api/items/skip", json.dumps({"key": "episode:1"}).encode()) as resp:
            assert json.load(resp)["key"] == "episode:1"
        assert Database(os.path.join(r.cfg.data_dir, "oversaetter.db")).get("episode:1")["status"] == "opgivet"
    finally:
        server.shutdown()
        server.server_close()
